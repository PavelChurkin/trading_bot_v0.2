"""Асинхронный клиент MOEX ISS.

Каждый HTTP-запрос берёт токен из общего :class:`~app.scheduler.TokenBucket`
(1 запрос/сек на весь процесс) и пишет время ответа в метрики планировщика.
429/5xx/сетевые ошибки превращаются в :class:`~app.scheduler.RetryableError`,
планировщик повторяет задачу с экспоненциальной паузой.

TTL-кэш (п. 21): свечи — до следующей свечи, список бумаг — сутки,
индекс — минута.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Optional

import httpx

from .scheduler import RequestScheduler, RetryableError, TTLCache
from .timeutil import now_msk

logger = logging.getLogger(__name__)

BOARD = "TQBR"
SHARES = "/engines/stock/markets/shares/boards/" + BOARD
PAGE_SIZE = 500  # ISS отдаёт свечи страницами по 500 строк

TTL_SECURITIES = 24 * 3600
TTL_INDEX = 60
TTL_MARKETDATA = 30


class OrderbookUnavailable(Exception):
    """ISS не отдаёт стакан без подписки — запросы стакана отключаются."""


def table(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    block = payload.get(name) or {}
    cols = block.get("columns") or []
    return [dict(zip(cols, row)) for row in block.get("data") or []]


def seconds_to_next_candle(interval: int, now=None) -> float:
    """Сколько секунд до закрытия текущей свечи интервала ISS ``interval``
    (1/10/60 — минуты; 24/7/31/4 — день/неделя/месяц/квартал: кэш до полуночи)."""
    now = now or now_msk()
    if interval in (24, 7, 31, 4):
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        minutes = now.hour * 60 + now.minute
        nxt_min = (minutes // interval + 1) * interval
        nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=nxt_min)
    return max((nxt - now).total_seconds(), 1.0)


class IssClient:
    def __init__(self, scheduler: RequestScheduler, base_url: str = "https://iss.moex.com/iss",
                 client: Optional[httpx.AsyncClient] = None, timeout: float = 20.0):
        self.scheduler = scheduler
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": "trading-bot-dashboard/0.2"}, follow_redirects=True)
        self.cache = TTLCache()
        self.orderbook_available = True

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ транспорт
    async def get(self, path: str, params: dict[str, Any] | None = None, *, cache_ttl: float = 0,
                  expect_json: bool = True) -> Any:
        params = {"iss.meta": "off", **(params or {})}
        key = (path, tuple(sorted(params.items())))
        if cache_ttl > 0:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        await self.scheduler.bucket.acquire()
        started = time.perf_counter()
        status: Optional[int] = None
        try:
            resp = await self._client.get(self.base_url + path + ".json", params=params)
            status = resp.status_code
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableError(f"сеть: {exc.__class__.__name__}") from exc
        finally:
            self.scheduler.record_request((time.perf_counter() - started) * 1000, status)
        if status == 429 or status >= 500:
            raise RetryableError(f"HTTP {status} {path}", status=status)
        resp.raise_for_status()
        if expect_json and "json" not in resp.headers.get("content-type", ""):
            raise ValueError(f"ISS вернул не JSON ({resp.headers.get('content-type')}) для {path}")
        data = resp.json()
        if cache_ttl > 0:
            self.cache.set(key, data, cache_ttl)
        return data

    # ---------------------------------------------------------------- бумаги
    async def securities(self) -> list[dict[str, Any]]:
        """Список акций режима TQBR (кэш — сутки)."""
        data = await self.get(SHARES + "/securities", {
            "iss.only": "securities",
            "securities.columns": "SECID,SHORTNAME,SECNAME,PREVPRICE,PREVDATE,LOTSIZE,SECTYPE",
        }, cache_ttl=TTL_SECURITIES)
        return table(data, "securities")

    async def marketdata(self, tickers: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Котировки дня. Без ``tickers`` — вся доска одним запросом;
        с ``tickers`` — батчем через ``securities=A,B,C``."""
        params = {
            "iss.only": "marketdata",
            "marketdata.columns": "SECID,LAST,OPEN,HIGH,LOW,VOLTODAY,VALTODAY,LASTTOPREVPRICE,UPDATETIME,SYSTIME",
        }
        if tickers:
            params["securities"] = ",".join(tickers)
        data = await self.get(SHARES + "/securities", params, cache_ttl=TTL_MARKETDATA)
        return table(data, "marketdata")

    async def index(self, secid: str = "IMOEX") -> Optional[dict[str, Any]]:
        data = await self.get(f"/engines/stock/markets/index/securities/{secid}", {
            "iss.only": "marketdata",
            "marketdata.columns": "SECID,CURRENTVALUE,LASTCHANGEPRC,UPDATETIME",
        }, cache_ttl=TTL_INDEX)
        rows = table(data, "marketdata")
        return rows[0] if rows else None

    async def index_candles(self, date_from: str, date_till: Optional[str] = None, secid: str = "IMOEX",
                            interval: int = 24) -> list[dict[str, Any]]:
        """Дневные свечи индекса (issue #5, задача 3 — корреляция с IMOEX)."""
        params: dict[str, Any] = {"interval": interval, "from": date_from}
        if date_till:
            params["till"] = date_till
        data = await self.get(f"/engines/stock/markets/index/securities/{secid}/candles", params,
                              cache_ttl=seconds_to_next_candle(interval))
        return [{"ts": r["begin"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
                for r in table(data, "candles")]

    # ----------------------------------------------------------------- свечи
    async def candles_page(self, ticker: str, interval: int, date_from: str, date_till: Optional[str] = None,
                           start: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"interval": interval, "from": date_from}
        if date_till:
            params["till"] = date_till
        if start:
            params["start"] = start
        data = await self.get(f"{SHARES}/securities/{ticker}/candles", params,
                              cache_ttl=seconds_to_next_candle(interval))
        out = []
        for r in table(data, "candles"):
            out.append({"ts": r["begin"], "open": r["open"], "high": r["high"], "low": r["low"],
                        "close": r["close"], "volume": r.get("volume") or 0, "value": r.get("value") or 0})
        return out

    async def candles(self, ticker: str, interval: int, date_from: str, date_till: Optional[str] = None,
                      max_pages: int = 10) -> list[dict[str, Any]]:
        """Свечи с пагинацией (``start=``). ``date_from`` — 'YYYY-MM-DD' или
        'YYYY-MM-DD HH:MM:SS' (хвост с последней свечи, п. 21)."""
        out: list[dict[str, Any]] = []
        for page in range(max_pages):
            rows = await self.candles_page(ticker, interval, date_from, date_till, start=page * PAGE_SIZE)
            out.extend(rows)
            if len(rows) < PAGE_SIZE:
                break
        return out

    # ---------------------------------------------------------------- стакан
    async def orderbook(self, ticker: str, depth: int = 10) -> Optional[dict[str, Any]]:
        """Стакан (только на P0). Если ISS отвечает HTML/пусто — стакан недоступен."""
        if not self.orderbook_available:
            return None
        try:
            data = await self.get(f"{SHARES}/securities/{ticker}/orderbook", {"iss.only": "orderbook"})
        except ValueError as exc:  # HTML вместо JSON — нужна подписка
            self.orderbook_available = False
            logger.info("Стакан ISS недоступен, запросы стакана отключены: %s", exc)
            raise OrderbookUnavailable(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            self.orderbook_available = False
            raise OrderbookUnavailable(str(exc)) from exc
        rows = table(data, "orderbook")
        bids = [{"price": r["PRICE"], "quantity": r["QUANTITY"]} for r in rows if r.get("BUYSELL") == "B"][:depth]
        asks = [{"price": r["PRICE"], "quantity": r["QUANTITY"]} for r in rows if r.get("BUYSELL") == "S"][:depth]
        if not bids and not asks:
            return None
        spread_pct = None
        if bids and asks:
            best_bid, best_ask = max(b["price"] for b in bids), min(a["price"] for a in asks)
            if best_bid:
                spread_pct = (best_ask - best_bid) / best_bid * 100
        return {"bids": bids, "asks": asks, "spread_pct": spread_pct}


# SECTYPE из справочника ISS: 1/2 — акции обыкн./прив., D — депозитарные
# расписки, 9/A/B/J — паи ПИФов, E — иностранные ETF, 3..8/C — облигации.
_SECTYPE_KINDS = {"1": "share", "2": "share", "D": "receipt", "9": "fund", "A": "fund", "B": "fund",
                  "E": "fund", "J": "fund", "I": "fund", "3": "bond", "4": "bond", "5": "bond", "6": "bond",
                  "7": "bond", "8": "bond", "C": "bond"}


def security_kind(sectype: Any, name: Optional[str] = None) -> str:
    """Тип бумаги для фильтров (акции / фонды / расписки / облигации)."""
    kind = _SECTYPE_KINDS.get(str(sectype or "").strip().upper())
    if kind:
        return kind
    n = (name or "").upper()
    if any(x in n for x in ("ПИФ", "ETF", "ФОНД")):
        return "fund"
    if any(x in n for x in ("ОБЛ", "ОФЗ")):
        return "bond"
    return "share" if sectype in (None, "") else "other"
