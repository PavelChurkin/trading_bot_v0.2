"""Офлайн-двойник ISS MOEX для демо, скриншотов и разработки без сети.

Генерирует детерминированные синтетические данные (НЕ реальные котировки):
неделю минутных свечей по десятку бумаг с типовыми сценариями —
импульс вверх/вниз, «плита» 4/4, перегретый рост дня (как ETLN в issue),
плюс дневные свечи для индикаторов P0. Интерфейс совпадает с
:class:`app.moex.IssClient`, поэтому весь конвейер сканера работает как есть.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.moex import OrderbookUnavailable
from app.scheduler import TTLCache
from app.timeutil import TS_FORMAT, now_msk

SESSION_START = (10, 0)
SESSION_MINUTES = 525  # 10:00 … 18:44

# тикер: (имя, базовая цена, базовый объём/мин, сценарий на последний день)
UNIVERSE: dict[str, tuple[str, float, float, str]] = {
    "SBER": ("Сбербанк", 305.0, 9000, "up_pulse"),
    "GAZP": ("Газпром", 128.0, 12000, "down_pulse"),
    "LKOH": ("Лукойл", 6900.0, 300, "calm"),
    "ETLN": ("Эталон", 21.0, 20000, "late_rally"),
    "MGNT": ("Магнит", 5200.0, 250, "up_plate"),
    "VTBR": ("ВТБ", 92.0, 15000, "down_plate"),
    "YDEX": ("Яндекс", 4100.0, 400, "near"),
    "ROSN": ("Роснефть", 520.0, 2500, "calm"),
    "NVTK": ("Новатэк", 1050.0, 900, "calm"),
    "GMKN": ("Норникель", 125.0, 8000, "near"),
    "PLZL": ("Полюс", 2100.0, 500, "calm"),
    "TATN": ("Татнефть", 640.0, 1800, "calm"),
    "MOEX": ("Мосбиржа", 205.0, 4000, "calm"),
    "AFLT": ("Аэрофлот", 62.0, 14000, "up_pulse_old"),
}

# События прошлых дней и середины текущего — чтобы в журнале была история:
# тикер → [(дней назад, минута сессии, ход %, множитель объёма, длина, мин)]
HISTORY: dict[str, list[tuple[int, int, float, float, int]]] = {
    "LKOH": [(1, 90, 0.65, 4.0, 15)],
    "ROSN": [(1, 240, -0.65, 4.0, 15)],
    "NVTK": [(1, 405, -0.6, 3.5, 15)],
    "AFLT": [(1, 330, 0.6, 4.0, 15)],
    "TATN": [(0, 180, 0.6, 3.5, 15)],
    "PLZL": [(0, 270, -0.6, 3.5, 15)],
    # «ETLN»: накануне — рост +3% и распродажа −4.5% (широкий диапазон дня)
    "ETLN": [(1, 0, 3.0, 1.2, 240), (1, 240, -4.5, 1.3, 270)],
}


def trading_days(n: int, until: date) -> list[date]:
    out, d = [], until
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def last_session_day(now: datetime) -> tuple[date, int]:
    """Последний торговый день и сколько минут сессии в нём прошло."""
    d = now.date()
    if d.weekday() < 5:
        elapsed = (now.hour - SESSION_START[0]) * 60 + now.minute - SESSION_START[1]
        if elapsed >= 30:
            return d, min(elapsed, SESSION_MINUTES)
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d, SESSION_MINUTES


class DemoIss:
    def __init__(self, scheduler=None, days: int = 6, seed: int = 7, now: Optional[datetime] = None):
        self.scheduler = scheduler
        self.cache = TTLCache()
        self.orderbook_available = False  # как у ISS без авторизации: стакан недоступен
        now = now or now_msk()
        self.last_day, minutes_today = last_session_day(now)
        self.days = trading_days(days, self.last_day)
        self.minute: dict[str, list[dict[str, Any]]] = {}
        self.daily: dict[str, list[dict[str, Any]]] = {}
        rnd = random.Random(seed)
        for ticker, (_, base, vol, scenario) in UNIVERSE.items():
            self.minute[ticker] = self._gen_minutes(rnd, ticker, base, vol, scenario, minutes_today)
            self.daily[ticker] = self._gen_daily(rnd, ticker, base)

    # ------------------------------------------------------------ генерация
    def _gen_minutes(self, rnd: random.Random, ticker: str, base: float, vol: float, scenario: str,
                     minutes_today: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        price = base
        sigma = 0.00035
        for di, day in enumerate(self.days):
            last = di == len(self.days) - 1
            n = minutes_today if last else SESSION_MINUTES
            t0 = datetime(day.year, day.month, day.day, *SESSION_START)
            events = self._events(scenario, n) if last else {}
            for ago, start, pct, vmult, length in HISTORY.get(ticker, []):
                if ago == len(self.days) - 1 - di:
                    for i in range(start, min(start + length, n)):
                        events.setdefault(i, (pct / 100 / length, vmult))
            for i in range(n):
                u_shape = 1.6 - math.sin(math.pi * i / SESSION_MINUTES) * 0.9
                drift, vmult = events.get(i, (0.0, 1.0))
                o = price
                c = o * (1 + rnd.gauss(0, sigma) + drift)
                hi = max(o, c) * (1 + abs(rnd.gauss(0, sigma * 0.6)))
                lo = min(o, c) * (1 - abs(rnd.gauss(0, sigma * 0.6)))
                if drift:  # импульсные свечи почти без теней — как у «плиты»
                    hi, lo = max(o, c) * (1 + sigma * 0.1), min(o, c) * (1 - sigma * 0.1)
                v = max(1.0, vol * u_shape * rnd.lognormvariate(0, 0.35) * vmult)
                ts = (t0 + timedelta(minutes=i)).strftime(TS_FORMAT)
                out.append({"ts": ts, "open": round(o, 4), "high": round(hi, 4), "low": round(lo, 4),
                            "close": round(c, 4), "volume": round(v), "value": round(v * c, 2)})
                price = c
        return out

    @staticmethod
    def _events(scenario: str, n: int) -> dict[int, tuple[float, float]]:
        """Минута → (снос цены за минуту, множитель объёма) для сценария дня."""
        last_window = (n // 15 - 1) * 15  # начало последнего полного 15-мин окна
        ev: dict[int, tuple[float, float]] = {}

        def window(start: int, total_pct: float, vmult: float, length: int = 15) -> None:
            for i in range(start, min(start + length, n)):
                ev[i] = (total_pct / 100 / length, vmult)

        if scenario == "up_pulse":
            window(last_window, 0.6, 4.0)
        elif scenario == "down_pulse":
            window(last_window, -0.6, 4.0)
        elif scenario == "up_pulse_old":
            window(max(0, last_window - 150), 0.65, 4.0)
        elif scenario == "near":  # чуть-чуть не дотянули до порога
            window(last_window, 0.22, 1.3)
        elif scenario == "up_plate":
            for k in range(4):
                window(last_window - 15 * (3 - k), 0.75, 2.5)
        elif scenario == "down_plate":
            for k in range(3):
                window(last_window - 15 * (2 - k), -0.7, 2.2)
        elif scenario == "late_rally":  # «ETLN»: отскок весь день ~+3%, свежий импульс поздно
            for i in range(0, last_window):
                ev[i] = (0.022 / max(last_window, 1), 1.3)
            window(last_window, 0.6, 3.5)
        return ev

    def _gen_daily(self, rnd: random.Random, ticker: str, base: float, n: int = 140) -> list[dict[str, Any]]:
        """Дневные свечи: история до первой минутной даты + агрегаты минуток."""
        minute = self.minute[ticker]
        first_day = self.days[0]
        hist_days = trading_days(n + 1, first_day)[:-1]
        trend = {"late_rally": 0.003, "up_plate": 0.0015}.get(UNIVERSE[ticker][3], 0.0)
        closes, price = [], base
        for _ in hist_days:
            closes.append(price)
            price /= (1 + trend + rnd.gauss(0, 0.017))
        closes = closes[::-1]
        scale = minute[0]["open"] / closes[-1]
        out = []
        for d, c in zip(hist_days, closes):
            c *= scale
            o = c * (1 + rnd.gauss(0, 0.008))
            out.append({"ts": f"{d.isoformat()} 00:00:00", "open": round(o, 4),
                        "high": round(max(o, c) * (1 + abs(rnd.gauss(0, 0.009))), 4),
                        "low": round(min(o, c) * (1 - abs(rnd.gauss(0, 0.009))), 4), "close": round(c, 4),
                        "volume": round(UNIVERSE[ticker][2] * 520 * rnd.lognormvariate(0, 0.3)), "value": 0})
        for day in self.days:
            rows = [c for c in minute if c["ts"][:10] == day.isoformat()]
            if rows:
                out.append({"ts": f"{day.isoformat()} 00:00:00", "open": rows[0]["open"],
                            "high": max(r["high"] for r in rows), "low": min(r["low"] for r in rows),
                            "close": rows[-1]["close"], "volume": sum(r["volume"] for r in rows),
                            "value": sum(r["value"] for r in rows)})
        return out

    # ----------------------------------------------------------- API как у ISS
    async def close(self) -> None:
        return None

    async def securities(self) -> list[dict[str, Any]]:
        out = []
        for t, (name, *_rest) in UNIVERSE.items():
            prev = self._prev_close(t)
            out.append({"SECID": t, "SHORTNAME": name, "SECNAME": f"ПАО «{name}» (демо)", "LOTSIZE": 10,
                        "PREVPRICE": prev})
        return out

    def _prev_close(self, ticker: str) -> float:
        days = [d for d in self.daily[ticker] if d["ts"][:10] < self.last_day.isoformat()]
        return days[-1]["close"]

    async def marketdata(self, tickers: Optional[list[str]] = None) -> list[dict[str, Any]]:
        out = []
        for t in tickers or UNIVERSE:
            rows = [c for c in self.minute[t] if c["ts"][:10] == self.last_day.isoformat()]
            prev = self._prev_close(t)
            last = rows[-1]["close"]
            out.append({"SECID": t, "LAST": last, "OPEN": rows[0]["open"], "HIGH": max(r["high"] for r in rows),
                        "LOW": min(r["low"] for r in rows), "VOLTODAY": sum(r["volume"] for r in rows),
                        "VALTODAY": round(sum(r["value"] for r in rows)),
                        "LASTTOPREVPRICE": round((last / prev - 1) * 100, 2)})
        return out

    async def index(self, secid: str = "IMOEX") -> Optional[dict[str, Any]]:
        return {"SECID": secid, "CURRENTVALUE": 2875.4, "LASTCHANGEPRC": -0.62, "LASTCHANGE": -17.9,
                "OPENVALUE": 2893.3, "UPDATETIME": "18:44:59"}

    async def index_candles(self, date_from: str, date_till: Optional[str] = None, secid: str = "IMOEX",
                            interval: int = 24) -> list[dict[str, Any]]:
        """Синтетический IMOEX (issue #5): средняя дневная доходность первых
        бумаг вселенной + свой шум — часть бумаг коррелирует сильно, часть слабо."""
        till = (date_till or "9999-12-31") + " 23:59:59"
        core = list(UNIVERSE)[:6]
        days = sorted({d["ts"] for t in core for d in self.daily[t]})
        closes = {t: {d["ts"]: d["close"] for d in self.daily[t]} for t in core}
        rnd = random.Random(f"imoex-{secid}")
        out, value, prev = [], 2800.0, None
        for ts in days:
            if prev is not None:
                rets = [closes[t][ts] / closes[t][prev] - 1 for t in core if ts in closes[t] and prev in closes[t]]
                value *= 1 + (sum(rets) / len(rets) if rets else 0) + rnd.gauss(0, 0.003)
            prev = ts
            if date_from <= ts <= till:
                out.append({"ts": ts, "open": value, "high": value * 1.004, "low": value * 0.996, "close": round(value, 2)})
        return out

    async def candles(self, ticker: str, interval: int, date_from: str, date_till: Optional[str] = None,
                      **_: Any) -> list[dict[str, Any]]:
        till = (date_till or "9999-12-31") + " 23:59:59"
        if interval == 60:  # графики 14 дней / месяц (issue #3): часовые из дневных + свои минутки
            return [c for c in self._hourly(ticker) if date_from <= c["ts"] <= till]
        src = self.daily if interval == 24 else self.minute
        return [c for c in src.get(ticker, []) if date_from <= c["ts"] <= till]

    def _hourly(self, ticker: str) -> list[dict[str, Any]]:
        """Часовые свечи: прошлые дни — синтетика внутри дневной свечи (9 часов
        10:00–18:00), дни с минутками — агрегат минуток."""
        minute = self.minute.get(ticker, [])
        minute_days = {c["ts"][:10] for c in minute}
        rnd = random.Random(f"h-{ticker}")
        out: list[dict[str, Any]] = []
        for d in self.daily.get(ticker, []):
            day = d["ts"][:10]
            if day in minute_days:
                continue
            price = d["open"]
            for h in range(9):
                close = d["close"] if h == 8 else price + (d["close"] - d["open"]) / 9 + rnd.gauss(0, 0.002) * price
                close = min(max(close, d["low"]), d["high"])
                out.append({"ts": f"{day} {10 + h:02d}:00:00", "open": price, "close": close,
                            "high": max(price, close) * (1 + rnd.uniform(0, 0.002)),
                            "low": min(price, close) * (1 - rnd.uniform(0, 0.002)),
                            "volume": d["volume"] / 9, "value": d["volume"] / 9 * close})
                price = close
        buckets: dict[str, list[dict[str, Any]]] = {}
        for c in minute:
            buckets.setdefault(c["ts"][:13], []).append(c)
        for key in sorted(buckets):
            b = buckets[key]
            out.append({"ts": f"{key}:00:00", "open": b[0]["open"], "close": b[-1]["close"],
                        "high": max(c["high"] for c in b), "low": min(c["low"] for c in b),
                        "volume": sum(c["volume"] for c in b), "value": sum(c.get("value") or 0 for c in b)})
        return sorted(out, key=lambda c: c["ts"])

    async def orderbook(self, ticker: str, depth: int = 10) -> Optional[dict[str, Any]]:
        raise OrderbookUnavailable("демо: стакан недоступен")
