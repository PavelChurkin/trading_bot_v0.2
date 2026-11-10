"""Сканер: цикл обхода всех тикеров, P0-детализация, алерты, отчёты.

Цикл (п. 1, 21):

1. P1: доска TQBR одним запросом (котировки дня всех бумаг) + индекс IMOEX
   + список бумаг (кэш сутки).
2. P1: хвост минутных свечей по каждому тикеру ``from=последняя свеча``
   (в первый раз — весь день).
3. Как только хвост тикера пришёл — дешёвый снапшот (амплитуда/ATR, объём,
   уровень, плита, возраст импульса) по каждому набору порогов и
   предварительная проверка. Сработал триггер — сразу ставится P0-задача:
   дневные свечи, стакан и все индикаторы (п. 27). P0 обгоняет оставшиеся
   P1 в очереди.
4. По результату P0 — финальная проверка (памп-фильтр, «поздно»), единый
   alert-объект, анти-спам и пуш каждому пользователю по его порогам.
5. Журнал решений (п. 24) и таблица metrics (п. 28) — каждый цикл.
6. P2: догрузка истории, дневной ATR, отчёты, очистка старых данных.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any, Optional

from . import alerts as al
from . import analysis as an
from .auth import active_user_ids
from .db import Database
from .moex import IssClient, OrderbookUnavailable, security_kind
from .push import PushService
from .scheduler import P0, P1, P2, RequestScheduler
from .timeutil import (fmt_ts, in_quiet_hours, market_closed_reason, next_market_open, now_msk, parse_hhmm,
                       parse_ts)

logger = logging.getLogger(__name__)

# Настройки, от которых зависит снапшот (наборы с одинаковыми значениями считаются один раз)
SNAPSHOT_KEYS = ("candle_window_min", "volume_avg_window", "level_window", "plate_series_n", "plate_jerk_atr",
                 "plate_pullback_max_pct", "plate_volume_mult", "plate_move_atr_k", "plate_use_series",
                 "plate_use_no_correction", "plate_use_volume", "plate_use_move", "plate_min_score")
# Моменты фиксации/распродажи, после которых сопровождение ждёт новый вход (issue #3)
TRACK_EVENT_ACTIONS = ("avoid", "rebound", "selloff", "market")
TRACK_ENTER_ACTIONS = ("unique_enter", "your_enter")
# Сигналы, по которым шлём пуш по открытой paper-позиции
PAPER_EXIT_ACTIONS = {"avoid": "фиксировать", "rebound": "кандидат на откуп", "selloff": "распродажа",
                      "market": "рыночный шок"}
WARMUP_REASON = "прогрев: первый цикл после запуска/перерыва"
BLUE_CHIPS = ["SBER", "GAZP", "LKOH", "ROSN", "NVTK", "TATN", "GMKN", "PLZL", "ALRS", "MGNT"]
SNAPSHOT_METRICS = ("price", "amplitude_atr", "amplitude_pct", "volume_ratio", "atr", "change_hour_pct",
                    "impulse_age_min", "plate_score", "level_distance_atr", "pullback_pct", "approach_intensity")
P0_METRICS = ("rsi", "macd_line", "macd_signal", "macd_hist", "stoch_k", "stoch_d", "bb_upper", "bb_lower",
              "bb_middle")


def snapshot_group_key(s: dict[str, Any]) -> tuple:
    return tuple(s[k] for k in SNAPSHOT_KEYS)


def candles_needed(window_min: int) -> int:
    """Сколько минуток нужно для снапшота и индикаторов P0 (≈80 свечей окна)."""
    return min(max(80 * int(window_min), 600), 8000)


class Scanner:
    def __init__(self, db: Database, iss: IssClient, scheduler: RequestScheduler, push: PushService):
        self.db = db
        self.iss = iss
        self.scheduler = scheduler
        self.push = push
        # Доска из БД: после рестарта ночью (биржа закрыта, цикла нет до 07:00)
        # таблица не должна быть пустой — восстанавливаем флаг активности, как в apply_board.
        self.securities: dict[str, dict[str, Any]] = {
            r["ticker"]: {**r, "active": bool(r.get("prev_price") and r["prev_price"] > 0)}
            for r in db.query("SELECT * FROM securities")}
        self.index: Optional[dict[str, Any]] = db.get_state("index")
        self.cycle_id: int = int(db.get_state("cycle_id", 0) or 0)
        self.cycle_running = False
        self.cycle_info: dict[str, Any] = db.get_state("last_cycle", {}) or {}
        self._wake = asyncio.Event()
        self._stopping = False
        self._last_metric_ts: dict[tuple[str, int], str] = {}
        self._backfilled: set[str] = set()
        self._daily_atr_day: Optional[str] = None
        self._purge_day: Optional[str] = None
        self.started_at = now_msk()
        self._force_cycle = False

    # ================================================================ запуск
    def request_cycle(self) -> None:
        """Кнопка «Обновить сейчас» в админке: не ждать интервал (и часы торгов)."""
        self._force_cycle = True
        self._wake.set()

    def market_closed(self, now=None, g: Optional[dict[str, Any]] = None) -> Optional[str]:
        """Причина пропуска цикла вне часов торгов (issue #3, п. 1) или ``None``."""
        g = g or self.db.global_settings()
        if not g.get("market_hours_only"):
            return None
        return market_closed_reason(now or now_msk(), g.get("market_open"), g.get("market_close"),
                                    bool(g.get("skip_weekends")))

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def run(self) -> None:
        housekeeping = asyncio.create_task(self._housekeeping_loop(), name="housekeeping")
        try:
            while not self._stopping:
                started = time.monotonic()
                g = self.db.global_settings()
                interval = max(int(g["scan_interval_min"]), 15) * 60
                closed = None if self._force_cycle else self.market_closed(g=g)
                self._force_cycle = False
                if closed:
                    now = now_msk()
                    reopen = next_market_open(now, g.get("market_open"), g.get("market_close"),
                                              bool(g.get("skip_weekends")))
                    wait = max(min(interval, (reopen - now).total_seconds()), 5)
                    if self.cycle_info.get("status") != "closed":
                        logger.info("Сканер спит: %s, до %s", closed, fmt_ts(reopen))
                    self.cycle_info = {**self.db.get_state("last_cycle", {}), "status": "closed",
                                       "closed_reason": closed, "reopen_at": fmt_ts(reopen)}
                else:
                    try:
                        await self.run_cycle()
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        logger.exception("Цикл сканера упал")
                    wait = max(interval - (time.monotonic() - started), 5)
                self.cycle_info["next_cycle_at"] = fmt_ts(now_msk() + timedelta(seconds=wait))
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            housekeeping.cancel()

    # ================================================================= цикл
    def settings_sets(self) -> list[tuple[Optional[int], dict[str, Any]]]:
        """[(None, глобальные), (key_id, эффективные настройки пользователя), ...]"""
        out: list[tuple[Optional[int], dict[str, Any]]] = [(None, self.db.global_settings())]
        for kid in active_user_ids(self.db):
            out.append((kid, self.db.effective_settings(kid)))
        return out

    async def run_cycle(self) -> dict[str, Any]:
        if self.cycle_running:
            return self.cycle_info
        self.cycle_running = True
        self.cycle_id += 1
        self.db.set_state("cycle_id", self.cycle_id)
        cycle_started = now_msk()
        t0 = time.monotonic()
        info: dict[str, Any] = {"cycle_id": self.cycle_id, "started_at": fmt_ts(cycle_started), "status": "running"}
        warmup = self.warmup_reason(cycle_started)
        if warmup:
            info["warmup"] = warmup
        self.cycle_info = info
        try:
            await self._refresh_board()
            sets = self.settings_sets()
            tickers = sorted(t for t, sec in self.securities.items() if sec.get("active"))
            info["tickers"] = len(tickers)
            ctx = CycleContext(self.cycle_id, sets)
            ctx.warmup = bool(warmup)
            jobs = [self._process_ticker(t, ctx) for t in tickers]
            await asyncio.gather(*jobs, return_exceptions=False)
            await self._sector_dump(ctx)
            await self._update_paper_trades()
            info.update({
                "status": "ok", "finished_at": fmt_ts(now_msk()), "duration_sec": round(time.monotonic() - t0, 1),
                "triggered": ctx.triggered, "alerts": ctx.alerts, "pushed": ctx.pushed, "errors": ctx.errors,
                "no_data": ctx.no_data,
            })
            self.scheduler.p1_cycle = {k: info.get(k) for k in ("cycle_id", "started_at", "finished_at",
                                                                 "duration_sec", "tickers")}
            self.db.set_state("last_scan_at", info["finished_at"])
            # P2: фон, не мешает P0/P1
            self._schedule_background(tickers)
        except Exception as exc:  # noqa: BLE001
            info.update({"status": "error", "error": str(exc), "finished_at": fmt_ts(now_msk())})
            raise
        finally:
            self.cycle_running = False
            self.db.set_state("last_cycle", info)
        logger.info("Цикл %s: %s тикеров, триггеров %s, алертов %s, пушей %s за %.0f с", self.cycle_id,
                    info.get("tickers"), info.get("triggered"), info.get("alerts"), info.get("pushed"),
                    time.monotonic() - t0)
        return info

    def warmup_reason(self, now=None) -> Optional[str]:
        """Первый цикл после запуска или перерыва (ночь, выключенный сканер) —
        «прогрев»: хвост свечей догружается за весь пропуск, и старые импульсы
        выглядят как новые. Такие алерты сохраняются, но без пушей (issue #3, п. 2)."""
        g = self.db.global_settings()
        if not g.get("warmup_enabled"):
            return None
        last = self.db.get_state("last_scan_at")
        if not last:
            return "первый запуск"
        gap = max(int(g.get("warmup_gap_min") or 45), 2 * max(int(g.get("scan_interval_min") or 15), 15))
        try:
            idle = ((now or now_msk()) - parse_ts(last)).total_seconds() / 60
        except ValueError:
            return "первый запуск"
        if idle > gap:
            return f"перерыв {idle:.0f} мин"
        return None

    async def _refresh_board(self) -> None:
        secs_f = self.scheduler.submit(P1, "securities", self.iss.securities, dedup_key="securities")
        md_f = self.scheduler.submit(P1, "marketdata", self.iss.marketdata, dedup_key="marketdata")
        idx_f = self.scheduler.submit(P1, "index IMOEX", self.iss.index, dedup_key="index")
        secs, md = await secs_f, await md_f
        try:
            self.index = await idx_f
            if self.index:
                self.db.set_state("index", self.index)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Индекс недоступен: %s", exc)
        self.apply_board(secs, md)

    def apply_board(self, secs: list[dict[str, Any]], md: list[dict[str, Any]]) -> None:
        by_md = {r["SECID"]: r for r in md}
        now = fmt_ts(now_msk())
        rows = []
        for sec in secs:
            t = sec["SECID"]
            m = by_md.get(t, {})
            prev = sec.get("PREVPRICE")
            last = m.get("LAST")
            change = m.get("LASTTOPREVPRICE")
            if change is None and last and prev:
                change = (last - prev) / prev * 100
            row = {
                "ticker": t, "name": sec.get("SHORTNAME"), "full_name": sec.get("SECNAME"), "lot": sec.get("LOTSIZE"),
                "prev_price": prev, "last": last, "open": m.get("OPEN"), "high": m.get("HIGH"), "low": m.get("LOW"),
                "volume_today": m.get("VOLTODAY"), "value_today": m.get("VALTODAY"), "day_change_pct": change,
                "updated_at": now, "sectype": security_kind(sec.get("SECTYPE"), sec.get("SECNAME")),
            }
            rows.append(row)
            old = self.securities.get(t, {})
            # Активная бумага — есть цена (отсекает делистинг и бумаги без торгов)
            self.securities[t] = {**old, **row, "prev_date": sec.get("PREVDATE") or old.get("prev_date"),
                                  "active": bool(prev and prev > 0)}
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT INTO securities (ticker, name, full_name, lot, prev_price, last, open, high, low, volume_today, "
                "value_today, day_change_pct, updated_at, sectype) VALUES (:ticker,:name,:full_name,:lot,:prev_price,"
                ":last,:open,:high,:low,:volume_today,:value_today,:day_change_pct,:updated_at,:sectype) "
                "ON CONFLICT(ticker) DO UPDATE "
                "SET name=excluded.name, full_name=excluded.full_name, lot=excluded.lot, prev_price=excluded.prev_price, "
                "last=excluded.last, open=excluded.open, high=excluded.high, low=excluded.low, "
                "volume_today=excluded.volume_today, value_today=excluded.value_today, "
                "day_change_pct=excluded.day_change_pct, updated_at=excluded.updated_at, sectype=excluded.sectype",
                rows,
            )

    # ------------------------------------------------------------ свечи (P1)
    def tail_from(self, ticker: str) -> str:
        """Откуда грузить хвост: с последней свечи в БД, иначе — весь сегодняшний день."""
        last = self.db.last_candle_ts(ticker)
        today = now_msk().strftime("%Y-%m-%d")
        if last and last[:10] == today:
            return last
        return today

    async def fetch_tail(self, ticker: str, priority: int = P1) -> int:
        frm = self.tail_from(ticker)
        cold = self.db.last_candle_ts(ticker) is None
        prev_date = (self.securities.get(ticker) or {}).get("prev_date")

        async def job() -> int:
            rows = await self.iss.candles(ticker, 1, frm)
            if not rows and cold and prev_date and prev_date < frm[:10]:
                # Холодный старт до открытия/в день без торгов: берём прошлую
                # сессию (PREVDATE из ISS), иначе таблица пуста до первой сделки.
                rows = await self.iss.candles(ticker, 1, prev_date, prev_date)
            return self.db.upsert_candles(ticker, rows)

        return await self.scheduler.submit(priority, f"candles {ticker} from={frm}", job,
                                           dedup_key=f"tail:{ticker}")

    async def _process_ticker(self, ticker: str, ctx: "CycleContext") -> None:
        try:
            await self.fetch_tail(ticker, P1)
        except Exception as exc:  # noqa: BLE001
            ctx.errors += 1
            logger.warning("Хвост свечей %s: %s", ticker, exc)
        try:
            await self.evaluate_ticker(ticker, ctx)
        except Exception:  # noqa: BLE001
            ctx.errors += 1
            logger.exception("Оценка %s упала", ticker)

    # ----------------------------------------------------------- оценка тикера
    def build_snapshots(self, ticker: str, sets: list[tuple[Optional[int], dict[str, Any]]]
                        ) -> dict[tuple, Optional[dict[str, Any]]]:
        out: dict[tuple, Optional[dict[str, Any]]] = {}
        cache: dict[int, list[dict[str, Any]]] = {}
        for _, s in sets:
            gk = snapshot_group_key(s)
            if gk in out:
                continue
            w = int(s["candle_window_min"])
            if w not in cache:
                cache[w] = self.db.last_candles(ticker, candles_needed(w))
            out[gk] = an.compute_snapshot(ticker, cache[w], s, self.securities.get(ticker))
        return out

    async def evaluate_ticker(self, ticker: str, ctx: "CycleContext") -> None:
        snaps = self.build_snapshots(ticker, ctx.sets)
        glob_s = ctx.sets[0][1]
        glob_snap = snaps.get(snapshot_group_key(glob_s))
        saved_windows: set[int] = set()
        for _, s in ctx.sets:  # сохраняем снапшот для каждого окна (глобальный — первым)
            snap = snaps.get(snapshot_group_key(s))
            w = int(s["candle_window_min"])
            if snap and w not in saved_windows:
                saved_windows.add(w)
                self.db.save_snapshot(ticker, w, snap["ts"], snap)

        # Предварительная проверка (без индикаторов) по каждому набору порогов
        pre: list[tuple[Optional[int], dict[str, Any], dict[str, Any], al.Evaluation]] = []
        for kid, s in ctx.sets:
            snap = snaps.get(snapshot_group_key(s))
            if snap is None:
                continue
            pre.append((kid, s, snap, al.evaluate(snap, s)))
        glob_ev = next((ev for kid, _, _, ev in pre if kid is None), None)

        if glob_snap is None:
            ctx.no_data += 1
            self.db.execute(
                "INSERT INTO decisions (cycle_id, ts, ticker, passed, failed, closeness, details) VALUES (?,?,?,?,?,?,?)",
                (ctx.cycle_id, fmt_ts(now_msk()), ticker, 0, "no_data", 0.0,
                 json.dumps({"checks": [], "failed": ["no_data"], "note": "мало свечей за окно"}, ensure_ascii=False)),
            )
            return

        detail: Optional[dict[str, Any]] = None
        if any(ev.triggered for *_, ev in pre):
            ctx.triggered += 1
            try:
                detail = await self.p0_detail(ticker)
            except Exception as exc:  # noqa: BLE001
                ctx.errors += 1
                logger.warning("P0 %s: %s", ticker, exc)

        final_glob: Optional[al.Evaluation] = glob_ev
        for kid, s, snap, ev in pre:
            if not ev.triggered or detail is None:
                continue
            intraday = an.intraday_indicators(an.aggregate(detail["minute"], int(s["candle_window_min"])))
            verdict = an.make_verdict(snap["price"], snap.get("day_change_pct"), detail["daily"], s, snap, intraday,
                                      detail["signals"])
            fev = al.evaluate(snap, s, intraday, detail["daily"], verdict)
            if kid is None:
                final_glob = fev
            if fev.alert is not None:
                ctx.candidates.append((kid, s, fev.alert))
                await self.deliver(kid, s, fev.alert, ctx)

        # Журнал решений — глобальные пороги
        ev = final_glob or glob_ev
        if ev is not None:
            self.db.execute(
                "INSERT INTO decisions (cycle_id, ts, ticker, passed, failed, closeness, details) VALUES (?,?,?,?,?,?,?)",
                (ctx.cycle_id, glob_snap["ts"], ticker, 1 if ev.alert is not None and not ev.alert.mute_push else 0,
                 ",".join(ev.failed), ev.closeness,
                 json.dumps({**al.decision_details(ev), "snapshot": _slim(glob_snap)}, ensure_ascii=False, default=str)),
            )
        self._store_metrics(ticker, glob_snap, ev, detail, int(glob_s["candle_window_min"]))

    def _store_metrics(self, ticker: str, snap: dict[str, Any], ev: Optional[al.Evaluation],
                       detail: Optional[dict[str, Any]], window: int) -> None:
        key = (ticker, window)
        if self._last_metric_ts.get(key) == snap["ts"] and detail is None:
            return
        self._last_metric_ts[key] = snap["ts"]
        ts = snap["ts"]
        rows = [(ticker, ts, m, _num(snap.get(m))) for m in SNAPSHOT_METRICS]
        if ev is not None:
            rows.append((ticker, ts, "threshold_closeness", ev.closeness))
            rows.append((ticker, ts, "threshold_distance", round(1 - min(ev.closeness, 1.0), 4)))
        if detail is not None:
            intraday = an.intraday_indicators(an.aggregate(detail["minute"], window))
            rows += [(ticker, ts, m, _num(intraday.get(m))) for m in P0_METRICS]
        self.db.execute("DELETE FROM metrics WHERE ticker = ? AND ts = ?", (ticker, ts))
        self.db.add_metrics([r for r in rows if r[3] is not None])

    # ------------------------------------------------------------------- P0
    async def p0_detail(self, ticker: str, refresh_tail: bool = False) -> dict[str, Any]:
        """P0: доп. свечи (дневные), стакан, индикаторы. Индикаторы считаются только здесь."""
        if refresh_tail:
            await self.fetch_tail(ticker, P0)
        g = self.db.global_settings()

        async def job() -> dict[str, Any]:
            today = now_msk().date()
            frm = (today - timedelta(days=int(g["daily_history_days"]))).isoformat()
            till = (today - timedelta(days=1)).isoformat()
            daily = await self.iss.candles(ticker, 24, frm, till)  # кэш до полуночи
            daily = [c for c in daily if c["ts"][:10] < today.isoformat()]
            sec = self.securities.get(ticker) or {}
            if sec.get("last") and sec.get("open"):
                daily = daily + [{"ts": today.isoformat() + " 00:00:00", "open": sec["open"],
                                  "high": sec.get("high") or sec["last"], "low": sec.get("low") or sec["last"],
                                  "close": sec["last"], "volume": sec.get("volume_today") or 0,
                                  "value": sec.get("value_today") or 0}]
            orderbook = None
            if g.get("orderbook_enabled") and self.iss.orderbook_available:
                try:
                    orderbook = await self.iss.orderbook(ticker)
                except OrderbookUnavailable:
                    orderbook = None
            minute = self.db.last_candles(ticker, candles_needed(int(g["candle_window_min"])) * 2)
            day_ind = an.daily_indicators(daily, today.isoformat())
            price = (minute[-1]["close"] if minute else None) or sec.get("last") or (daily[-1]["close"] if daily else 0)
            signals = an.text_signals(price, day_ind, sec.get("volume_today"), orderbook, daily)
            return {"ticker": ticker, "daily": day_ind, "daily_candles": daily, "orderbook": orderbook,
                    "minute": minute, "signals": signals, "price": price, "ts": fmt_ts(now_msk())}

        return await self.scheduler.submit(P0, f"detail {ticker}", job, dedup_key=f"p0:{ticker}")

    async def analyze(self, ticker: str, key_id: Optional[int] = None, refresh: bool = True) -> dict[str, Any]:
        """Кнопка дашборда «Анализ» (бывший пункт меню 1): P0-задача по тикеру."""
        ticker = ticker.strip().upper()
        if ticker not in self.securities:
            raise KeyError(f"Тикер {ticker} не найден на TQBR")
        detail = await self.p0_detail(ticker, refresh_tail=refresh)
        s = self.db.effective_settings(key_id)
        w = int(s["candle_window_min"])
        snap = an.compute_snapshot(ticker, detail["minute"], s, self.securities.get(ticker))
        intraday = an.intraday_indicators(an.aggregate(detail["minute"], w)) if detail["minute"] else {}
        sec = self.securities.get(ticker) or {}
        price = (snap or {}).get("price") or detail["price"]
        verdict = an.make_verdict(price, sec.get("day_change_pct"), detail["daily"], s, snap, intraday or None,
                                  detail["signals"])
        ev = al.evaluate(snap, s, intraday, detail["daily"], verdict) if snap else None
        return {
            "ticker": ticker, "name": sec.get("name"), "full_name": sec.get("full_name"), "price": price,
            "day_change_pct": sec.get("day_change_pct"), "volume_today": sec.get("volume_today"),
            "snapshot": snap, "intraday": intraday, "daily": detail["daily"], "orderbook": detail["orderbook"],
            "orderbook_available": self.iss.orderbook_available, "signals": detail["signals"], "verdict": verdict,
            "evaluation": _ev_dict(ev), "ts": detail["ts"],
        }

    async def analyze_many(self, tickers: list[str], key_id: Optional[int] = None) -> list[dict[str, Any]]:
        res = await asyncio.gather(*(self.analyze(t, key_id, refresh=False) for t in tickers), return_exceptions=True)
        return [r if not isinstance(r, Exception) else {"ticker": t, "error": str(r)} for t, r in zip(tickers, res)]

    # ---------------------------------------------------------------- доставка
    async def deliver(self, key_id: Optional[int], s: dict[str, Any], alert: al.Alert, ctx: Optional["CycleContext"],
                      ) -> Optional[int]:
        """Сохраняет алерт и (для пользователя) отправляет пуш с учётом анти-спама (п. 11, 23)."""
        dup = self.db.query_one(
            "SELECT id FROM alerts WHERE key_id IS ? AND ticker = ? AND ts = ? AND direction = ?",
            (key_id, alert.ticker, alert.ts, alert.direction))
        if dup:
            return None
        warmup = ctx is not None and ctx.warmup
        if key_id is not None and not warmup:
            self.track_entry(key_id, s, alert)
        suppressed = WARMUP_REASON if warmup else self.suppression_reason(key_id, s, alert)
        pushed = False
        if key_id is not None and suppressed is None:
            msg = al.format_push(alert, s)
            res = await self.push.send_to_key(key_id, msg["title"], msg["body"],
                                              {"ticker": alert.ticker, "url": f"/#ticker/{alert.ticker}"})
            pushed = res["sent"] > 0
            # анти-спам пройден; если устройств нет — алерт всё равно виден в истории дашборда
            alert.extra["push"] = {"devices": res["devices"], "sent": res["sent"], "errors": res["errors"][:3]}
        cur = self.db.execute(al.ALERT_INSERT_SQL, alert.db_row(key_id, pushed, suppressed))
        if ctx is not None:
            ctx.alerts += 1
            ctx.pushed += int(pushed)
        if key_id is not None and not warmup and alert.action in PAPER_EXIT_ACTIONS:
            await self.paper_alert_notify(key_id, s, alert)
        return int(cur.lastrowid)

    def is_favorite(self, key_id: int, ticker: str) -> bool:
        return self.db.query_one("SELECT 1 AS x FROM favorites WHERE key_id = ? AND ticker = ?",
                                 (key_id, ticker)) is not None

    def track_entry(self, key_id: int, s: dict[str, Any], alert: al.Alert, now=None) -> Optional[str]:
        """Сопровождение (issue #3, п. 9): после фиксации (плита вверх) или
        распродажи (импульс/плита вниз) первый «Кандидат на вход» по бумаге
        становится «Уникальным кандидатом на вход», а для избранного — «Вашим
        кандидатом на вход». Такие пуши не учитываются в дневном лимите."""
        if alert.action != "enter" or not s.get("track_enabled", True):
            return None
        since = fmt_ts((now or now_msk()) - timedelta(days=int(s.get("track_window_days") or 5)))
        marks = ",".join("?" * len(TRACK_EVENT_ACTIONS))
        event = self.db.query_one(
            f"SELECT ts, action FROM alerts WHERE (key_id = ? OR key_id IS NULL) AND ticker = ? AND action IN ({marks}) "
            "AND ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (key_id, alert.ticker, *TRACK_EVENT_ACTIONS, since, alert.ts))
        if not event:
            return None
        done = self.db.query_one(
            "SELECT id FROM alerts WHERE key_id = ? AND ticker = ? AND action IN (?, ?) AND suppressed IS NULL "
            "AND ts > ? LIMIT 1", (key_id, alert.ticker, *TRACK_ENTER_ACTIONS, event["ts"]))
        if done:
            return None
        alert.action = "your_enter" if self.is_favorite(key_id, alert.ticker) else "unique_enter"
        what = "фиксации" if event["action"] == "avoid" else "распродажи"
        alert.labels.append(f"после {what} {event['ts'][5:16]}")
        alert.extra["track"] = {"event_ts": event["ts"], "event_action": event["action"]}
        return alert.action

    def suppression_reason(self, key_id: Optional[int], s: dict[str, Any], alert: al.Alert,
                           now=None) -> Optional[str]:
        now = now or now_msk()
        if alert.direction not in (s.get("alert_types") or []):
            return "тип отключён"
        if alert.mute_push:
            return "памп-фильтр"
        actions = s.get("push_actions")
        if actions is not None and alert.action not in actions:
            return "действие отключено"
        price = alert.price
        pmin, pmax = float(s.get("push_price_min") or 0), float(s.get("push_price_max") or 0)
        if price is not None and ((pmin > 0 and price < pmin) or (pmax > 0 and price > pmax)):
            return "цена вне диапазона"
        tracking = alert.action in TRACK_ENTER_ACTIONS
        cd = int(s.get("cooldown_min") or 0)
        if cd > 0 and not tracking:
            since = fmt_ts(now - timedelta(minutes=cd))
            recent = self.db.query_one(
                "SELECT id FROM alerts WHERE key_id IS ? AND ticker = ? AND suppressed IS NULL AND ts >= ? LIMIT 1",
                (key_id, alert.ticker, since))
            if recent:
                return f"кулдаун {cd} мин"
        limit = int(s.get("daily_push_limit") or 0)
        if key_id is not None and limit > 0 and not tracking:
            day = now.strftime("%Y-%m-%d")
            n = self.db.query_one(
                "SELECT COUNT(*) AS n FROM alerts WHERE key_id = ? AND suppressed IS NULL AND substr(ts,1,10) = ? "
                "AND COALESCE(action, '') NOT IN (?, ?)", (key_id, day, *TRACK_ENTER_ACTIONS))["n"]
            if n >= limit:
                return f"лимит {limit} в день"
        if key_id is not None and in_quiet_hours(now, s.get("quiet_from"), s.get("quiet_to")):
            return "тихие часы"
        return None

    # ----------------------------------------------------------- массовый снос
    async def _sector_dump(self, ctx: "CycleContext") -> None:
        """Опциональный алерт (п. 22): много бумаг одновременно сносит вниз —
        это рыночный/отраслевой шок, а не плита в отдельной бумаге.
        ISS не даёт сектора, поэтому считаем по всему рынку."""
        g = ctx.sets[0][1]
        min_n = int(self.db.global_settings()["sector_min_tickers"])
        for kid, s in ctx.sets:
            down = [a for k, _, a in ctx.candidates if k == kid and a.direction in ("down_pulse", "down_plate")]
            if len(down) < min_n:
                continue
            tickers = sorted({a.ticker for a in down})
            idx = self.index or {}
            ts = max(a.ts for a in down)
            reason = (f"вниз одновременно {len(tickers)} бумаг: {', '.join(tickers[:12])}"
                      + (f"; IMOEX {idx.get('LASTCHANGEPRC'):+.2f}%" if idx.get("LASTCHANGEPRC") is not None else ""))
            alert = al.Alert(ticker="РЫНОК", ts=ts, direction="sector_dump",
                             score=min(100, 50 + 5 * len(tickers)), amplitude_atr=None, volume_ratio=None,
                             level_break=-1, reason=reason, action="market",
                             labels=["не плита в отдельной бумаге"], extra={"tickers": tickers, "index": idx})
            for a in down:
                if a.direction == "down_plate":
                    self.db.execute(
                        "UPDATE alerts SET action = 'market' WHERE key_id IS ? AND ticker = ? AND ts = ? AND direction = ?",
                        (kid, a.ticker, a.ts, a.direction))
            await self.deliver(kid, s if kid is not None else g, alert, ctx)

    # ---------------------------------------------------------- paper trading
    async def _update_paper_trades(self) -> None:
        """Paper trading (п. 26 + issue #3): стоп/тейк по минуткам (если включено
        авто-закрытие) и пуши сопровождения: цель в % и в ₽, закрытие."""
        settings: dict[int, dict[str, Any]] = {}
        for t in self.db.query("SELECT * FROM paper_trades WHERE status = 'open'"):
            kid = t["key_id"]
            if kid not in settings:
                settings[kid] = self.db.effective_settings(kid)
            s = settings[kid]
            closed = self.check_paper_trade(t) if s.get("paper_auto_close", True) else None
            if closed:
                sign = "+" if closed["result_pct"] >= 0 else ""
                await self.paper_notify(t, s, "closed",
                                        f"Paper {t['ticker']}: закрыта по {'тейку' if closed['status'] == 'take' else 'стопу'}",
                                        f"{sign}{closed['result_pct']:.2f}%" + _rub(closed.get("profit_rub")))
                continue
            await self.paper_targets(t, s)

    def paper_price(self, ticker: str) -> Optional[float]:
        sec = self.securities.get(ticker) or {}
        price = sec.get("last")
        if not price:
            last = self.db.last_candles(ticker, 1)
            price = last[-1]["close"] if last else None
        return price

    @staticmethod
    def paper_pnl(t: dict[str, Any], price: Optional[float]) -> tuple[Optional[float], Optional[float]]:
        """(прибыль в %, прибыль в ₽) по цене ``price``; ₽ — если задано количество."""
        if not price or not t.get("entry_price"):
            return None, None
        sign = 1 if t["side"] == "long" else -1
        pct = (price - t["entry_price"]) / t["entry_price"] * 100 * sign
        qty = t.get("qty")
        rub = (price - t["entry_price"]) * qty * sign if qty else None
        return round(pct, 3), (round(rub, 2) if rub is not None else None)

    async def paper_targets(self, t: dict[str, Any], s: dict[str, Any]) -> list[str]:
        """Пуш «пора выходить», когда прибыль достигла цели в % или в ₽ (один раз на цель)."""
        pct, rub = self.paper_pnl(t, self.paper_price(t["ticker"]))
        if pct is None:
            return []
        sent = []
        target_pct = float(s.get("paper_target_pct") or 0)
        target_rub = float(s.get("paper_target_rub") or 0)
        if target_pct > 0 and pct >= target_pct:
            if await self.paper_notify(t, s, "target_pct", f"Paper {t['ticker']}: +{pct:.2f}% — цель {target_pct:g}%",
                                       "Прибыль достигла цели — пора выходить?" + _rub(rub)):
                sent.append("target_pct")
        if target_rub > 0 and rub is not None and rub >= target_rub:
            if await self.paper_notify(t, s, "target_rub", f"Paper {t['ticker']}: +{rub:,.0f} ₽ — цель {target_rub:,.0f} ₽",
                                       f"Прибыль {pct:+.2f}% достигла цели в рублях — пора выходить?"):
                sent.append("target_rub")
        return sent

    async def paper_alert_notify(self, key_id: int, s: dict[str, Any], alert: al.Alert) -> int:
        """Сопровождение paper (issue #3, п. 9): позиция отслеживается до сигнала
        «кандидат на откуп» / «распродажа» (и «фиксировать») — тогда пуш."""
        n = 0
        for t in self.db.query("SELECT * FROM paper_trades WHERE key_id = ? AND ticker = ? AND status = 'open' "
                               "AND opened_at <= ?", (key_id, alert.ticker, alert.ts)):
            pct, rub = self.paper_pnl(t, alert.price or self.paper_price(t["ticker"]))
            body = f"{alert.direction_label}: {alert.action_label}."
            if pct is not None:
                body += f" Позиция {pct:+.2f}%" + _rub(rub)
            if await self.paper_notify(t, s, f"alert:{alert.ts}:{alert.action}",
                                       f"Paper {t['ticker']}: {PAPER_EXIT_ACTIONS[alert.action]}", body):
                n += 1
        return n

    async def paper_notify(self, t: dict[str, Any], s: dict[str, Any], key: str, title: str, body: str) -> bool:
        """Один пуш на событие ``key`` по сделке. Не учитывается в дневном лимите,
        тихие часы соблюдаются. Отметка — в ``paper_trades.notified``."""
        if not s.get("paper_notify_enabled", True):
            return False
        notified = json.loads(t.get("notified") or "{}")
        if key in notified:
            return False
        now = now_msk()
        if in_quiet_hours(now, s.get("quiet_from"), s.get("quiet_to")):
            return False
        res = await self.push.send_to_key(t["key_id"], title, body, {"ticker": t["ticker"], "url": "/#paper"})
        notified[key] = fmt_ts(now)
        t["notified"] = json.dumps(notified, ensure_ascii=False)
        self.db.execute("UPDATE paper_trades SET notified = ? WHERE id = ?", (t["notified"], t["id"]))
        return bool(res.get("sent"))

    def check_paper_trade(self, t: dict[str, Any]) -> Optional[dict[str, Any]]:
        candles = self.db.candles_since(t["ticker"], t["opened_at"])
        long = t["side"] == "long"
        for c in candles:
            hit = None
            if t["stop"] is not None and ((long and c["low"] <= t["stop"]) or (not long and c["high"] >= t["stop"])):
                hit = ("stop", t["stop"])
            elif t["take"] is not None and ((long and c["high"] >= t["take"]) or (not long and c["low"] <= t["take"])):
                hit = ("take", t["take"])
            if hit:
                status, price = hit
                pct, rub = self.paper_pnl(t, price)
                self.db.execute(
                    "UPDATE paper_trades SET status = ?, exit_price = ?, closed_at = ?, result_pct = ?, profit_rub = ? "
                    "WHERE id = ?", (status, price, c["ts"], pct, rub, t["id"]))
                return {"status": status, "exit_price": price, "result_pct": pct, "profit_rub": rub}
        return None

    # ------------------------------------------------------------- фон (P2)
    def _schedule_background(self, tickers: list[str]) -> None:
        today = now_msk().strftime("%Y-%m-%d")
        # Догрузка предыдущего торгового дня — для среднего объёма/уровней с утра
        for t in tickers:
            if t in self._backfilled:
                continue
            self._backfilled.add(t)
            self.scheduler.submit(P2, f"backfill {t}", lambda t=t: self._backfill(t), dedup_key=f"backfill:{t}")
        # Дневной ATR по всем бумагам раз в день (прогревает кэш дневных свечей для P0)
        if self._daily_atr_day != today:
            self._daily_atr_day = today
            for t in tickers:
                self.scheduler.submit(P2, f"daily ATR {t}", lambda t=t: self._daily_atr(t), dedup_key=f"datr:{t}")
        if self._purge_day != today:
            self._purge_day = today

            async def purge() -> dict[str, int]:
                return self.db.purge_older_than(int(self.db.global_settings()["retention_days"]))

            self.scheduler.submit(P2, "retention purge", purge, dedup_key="purge")

    async def _backfill(self, ticker: str) -> int:
        days = self.db.candle_days(ticker)
        today = now_msk().date()
        have_prev = any(d < today.isoformat() for d in days)
        if have_prev:
            return 0
        # ищем последний торговый день до сегодняшнего (до 5 дней назад)
        first = parse_ts(days[0] + " 00:00:00").date() if days else today
        for back in range(1, 6):
            day = (first - timedelta(days=back)).isoformat()
            rows = await self.iss.candles(ticker, 1, day, day)
            if rows:
                return self.db.upsert_candles(ticker, rows)
        return 0

    async def _daily_atr(self, ticker: str) -> Optional[float]:
        g = self.db.global_settings()
        today = now_msk().date()
        frm = (today - timedelta(days=int(g["daily_history_days"]))).isoformat()
        daily = await self.iss.candles(ticker, 24, frm, (today - timedelta(days=1)).isoformat())
        ind = an.daily_indicators(daily) if daily else {}
        if ind.get("atr_pct") is not None:
            sec = self.securities.setdefault(ticker, {})
            sec["daily_atr_pct"] = ind["atr_pct"]
        return ind.get("atr_pct")

    # ------------------------------------------------------- дневной отчёт (п. 8)
    async def _housekeeping_loop(self) -> None:
        while not self._stopping:
            try:
                await self.check_daily_reports()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Дневной отчёт упал")
            await asyncio.sleep(20)

    async def check_daily_reports(self, now=None) -> list[dict[str, Any]]:
        now = now or now_msk()
        day = now.strftime("%Y-%m-%d")
        done = []
        for kid, s in self.settings_sets():
            t = parse_hhmm(s.get("daily_report_time"))
            if t is None or (now.hour, now.minute) < (t.hour, t.minute):
                continue
            state_key = f"report:{kid if kid is not None else 'global'}:{day}"
            if self.db.get_state(state_key):
                continue
            self.db.set_state(state_key, True)

            async def job(kid=kid, s=s) -> dict[str, Any]:
                return await self.make_daily_report(kid, s, day, send=True)

            done.append(await self.scheduler.submit(P2, f"daily report {kid or 'global'}", job))
        return done

    def daily_top(self, day: str, key_id: Optional[int] = None, n: int = 3) -> dict[str, list[dict[str, Any]]]:
        """Топ-3 за день отдельными блоками вверх и вниз (п. 8, 23).
        Берётся лучший алерт по каждому тикеру; если алертов мало — добор по
        изменению дня из котировок."""
        rows = self.db.query(
            "SELECT * FROM alerts WHERE key_id IS NULL AND substr(ts,1,10) = ? AND ticker != 'РЫНОК' ORDER BY score DESC",
            (day,))
        up: dict[str, dict[str, Any]] = {}
        down: dict[str, dict[str, Any]] = {}
        for r in rows:
            bucket = up if r["direction"] in ("up_pulse", "up_plate") else down
            if r["ticker"] not in bucket:
                bucket[r["ticker"]] = al.alert_from_row(r)
        out = {"up": list(up.values())[:n], "down": list(down.values())[:n], "source": "alerts"}
        if len(out["up"]) < n or len(out["down"]) < n:
            movers = [s for s in self.securities.values() if s.get("day_change_pct") is not None and s.get("active")]
            movers.sort(key=lambda x: x["day_change_pct"])
            have = {a["ticker"] for a in out["up"] + out["down"]}
            for sec in reversed(movers):
                if len(out["up"]) >= n or sec["day_change_pct"] <= 0:
                    break
                if sec["ticker"] not in have:
                    out["up"].append(_mover(sec))
            for sec in movers:
                if len(out["down"]) >= n or sec["day_change_pct"] >= 0:
                    break
                if sec["ticker"] not in have:
                    out["down"].append(_mover(sec))
            out["source"] = "alerts+quotes"
        return out

    async def make_daily_report(self, key_id: Optional[int], s: dict[str, Any], day: str,
                                send: bool = True) -> dict[str, Any]:
        top = self.daily_top(day, key_id)
        counts = self.db.query(
            "SELECT direction, COUNT(*) AS n FROM alerts WHERE key_id IS ? AND substr(ts,1,10) = ? GROUP BY direction",
            (key_id, day))
        report = {"day": day, "top": top, "counts": {r["direction"]: r["n"] for r in counts},
                  "index": self.index, "created_at": fmt_ts(now_msk())}
        self.db.execute("INSERT INTO daily_reports (key_id, day, created_at, data) VALUES (?,?,?,?)",
                        (key_id, day, report["created_at"], json.dumps(report, ensure_ascii=False, default=str)))
        if send and key_id is not None and s.get("daily_report_enabled"):
            title, body = format_report(report)
            report["push"] = await self.push.send_to_key(key_id, title, body, {"url": "/#report"})
        return report


class CycleContext:
    def __init__(self, cycle_id: int, sets: list[tuple[Optional[int], dict[str, Any]]]):
        self.cycle_id = cycle_id
        self.sets = sets
        self.triggered = 0
        self.alerts = 0
        self.pushed = 0
        self.errors = 0
        self.no_data = 0
        self.warmup = False
        self.candidates: list[tuple[Optional[int], dict[str, Any], al.Alert]] = []


def format_report(report: dict[str, Any]) -> tuple[str, str]:
    def line(items: list[dict[str, Any]]) -> str:
        if not items:
            return "—"
        out = []
        for a in items:
            if a.get("score") is not None and a.get("direction") != "mover":
                out.append(f"{a['ticker']} ({a.get('direction_label', '')}, сила {int(a['score'])})")
            else:
                out.append(f"{a['ticker']} {a.get('day_change_pct', 0):+.2f}%")
        return ", ".join(out)

    top = report["top"]
    title = f"Итоги дня {report['day']}"
    body = f"▲ Вверх: {line(top['up'])}\n▼ Вниз: {line(top['down'])}"
    return title, body


def _rub(v: Optional[float]) -> str:
    return f" ({v:+,.0f} ₽)".replace(",", " ") if v is not None else ""


def _mover(sec: dict[str, Any]) -> dict[str, Any]:
    return {"ticker": sec["ticker"], "name": sec.get("name"), "direction": "mover",
            "direction_label": "изменение дня", "day_change_pct": round(sec["day_change_pct"], 2),
            "price": sec.get("last"), "score": None, "reason": f"{sec['ticker']}: {sec['day_change_pct']:+.2f}% за день"}


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _slim(snap: dict[str, Any]) -> dict[str, Any]:
    keys = ("price", "amplitude_pct", "amplitude_atr", "volume_ratio", "level_break", "level_distance_atr",
            "change_hour_pct", "impulse_age_min", "plate_score", "pullback_pct", "atr", "direction", "window", "ts")
    return {k: snap.get(k) for k in keys}


def _ev_dict(ev: Optional[al.Evaluation]) -> Optional[dict[str, Any]]:
    if ev is None:
        return None
    return {"triggered": ev.triggered, "closeness": ev.closeness, "checks": ev.checks, "failed": ev.failed,
            "alert": ev.alert.to_dict() if ev.alert else None}
