"""Цикл сканера целиком на фейковом ISS и фейковом пуше (без сети).

Проверяем: P1 грузит хвост минуток по всем бумагам, индикаторы (P0)
считаются только для сработавшей бумаги, пишутся алерты/журнал решений/
метрики, пользователю уходит пуш, дневной отчёт и paper trading работают.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from app import auth
from app.scanner import Scanner, format_report
from app.scheduler import RequestScheduler, TokenBucket, TTLCache
from app.timeutil import now_msk
from conftest import flat_minutes, pulse_day

TODAY = now_msk().strftime("%Y-%m-%d")


def run(coro, timeout: float = 20):
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


def daily_history(base: float, days: int = 60) -> list[dict]:
    out = []
    d = date.fromisoformat(TODAY) - timedelta(days=days)
    price = base * 0.95
    for i in range(days):
        day = d + timedelta(days=i)
        o = price
        c = price * (1.001 if i % 3 else 0.998)
        out.append({"ts": f"{day.isoformat()} 00:00:00", "open": o, "close": c, "high": max(o, c) * 1.01,
                    "low": min(o, c) * 0.99, "volume": 400_000, "value": 400_000 * c})
        price = c
    return out


class FakeIss:
    """Минимальный двойник IssClient: те же методы, данные — синтетика."""

    orderbook_available = False

    def __init__(self):
        self.cache = TTLCache()
        self.minute = {"PULS": pulse_day(TODAY), "CALM": flat_minutes(f"{TODAY} 10:00:00", 315, 50.0)}
        self.calls: list[tuple] = []

    async def securities(self):
        return [{"SECID": t, "SHORTNAME": t.title(), "SECNAME": f"ПАО {t}", "LOTSIZE": 1,
                 "PREVPRICE": c[0]["open"]} for t, c in self.minute.items()] + \
               [{"SECID": "DEAD", "SHORTNAME": "Делистинг", "SECNAME": "", "LOTSIZE": 1, "PREVPRICE": None}]

    async def marketdata(self):
        out = []
        for t, c in self.minute.items():
            out.append({"SECID": t, "LAST": c[-1]["close"], "OPEN": c[0]["open"],
                        "HIGH": max(x["high"] for x in c), "LOW": min(x["low"] for x in c),
                        "VOLTODAY": sum(x["volume"] for x in c), "VALTODAY": sum(x["value"] for x in c),
                        "LASTTOPREVPRICE": (c[-1]["close"] / c[0]["open"] - 1) * 100})
        return out

    async def index(self):
        return {"SECID": "IMOEX", "CURRENTVALUE": 3000.0, "LASTCHANGEPRC": -0.4}

    async def candles(self, ticker, interval, frm, till=None):
        self.calls.append((ticker, interval, frm, till))
        if interval == 24:
            return daily_history(self.minute[ticker][0]["open"])
        if till is not None and till < TODAY:  # догрузка прошлых дней (P2)
            return []
        return [c for c in self.minute.get(ticker, []) if c["ts"] >= frm]

    async def orderbook(self, ticker):  # pragma: no cover — стакан выключен
        raise AssertionError("стакан не должен запрашиваться")


class FakePush:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_to_key(self, key_id, title, body, data=None, **_):
        self.sent.append((key_id, title, body, data))
        return {"sent": 1, "failed": 0, "devices": 1, "errors": []}

    def subscriptions(self, key_id):
        return []


def make_scanner(db):
    sched = RequestScheduler(TokenBucket(1000, 50))
    return Scanner(db, FakeIss(), sched, FakePush()), sched


def test_full_cycle(db):
    user = auth.create_user_key(db, "trader")
    db.save_settings(f"user:{user['id']}", {"quiet_from": "", "quiet_to": ""})
    db.save_settings("global", {"warmup_enabled": False})  # прогрев проверяется в test_issue3.py
    scanner, sched = make_scanner(db)
    p0_calls: list[str] = []
    orig = scanner.p0_detail

    async def spy(ticker, refresh_tail=False):
        p0_calls.append(ticker)
        return await orig(ticker, refresh_tail)

    scanner.p0_detail = spy

    async def scenario():
        sched.start()
        try:
            first = await scanner.run_cycle()
            await asyncio.sleep(0.2)  # даём P2-фону отработать
            snapshot = {"alerts": len(db.query("SELECT id FROM alerts WHERE ticker = 'PULS'")),
                        "pushes": len(scanner.push.sent)}
            second = await scanner.run_cycle()  # те же свечи ещё раз
            return first, snapshot, second
        finally:
            await sched.stop()

    info, after_first, second = run(scenario())
    assert info["status"] == "ok" and info["tickers"] == 2  # DEAD без цены — неактивна
    assert info["triggered"] == 1
    # Индикаторы (P0) — только для сработавшей бумаги (п. 27)
    assert set(p0_calls) == {"PULS"}

    # Минутки сохранены по обеим бумагам
    assert db.last_candle_ts("PULS")[:10] == TODAY and db.last_candle_ts("CALM")[:10] == TODAY

    # Алерт в общем журнале и у пользователя, с причиной в цифрах
    alerts = db.query("SELECT * FROM alerts WHERE ticker = 'PULS'")
    assert {a["key_id"] for a in alerts} == {None, user["id"]}
    mine = next(a for a in alerts if a["key_id"] == user["id"])
    assert mine["direction"] == "up_pulse" and "ATR" in mine["reason"]
    assert mine["pushed"] == 1 and mine["suppressed"] is None
    assert scanner.push.sent and scanner.push.sent[0][0] == user["id"]
    assert "PULS" in scanner.push.sent[0][1] + scanner.push.sent[0][2]

    # Журнал решений: строка на каждый тикер цикла, у CALM — что не прошло
    dec = {d["ticker"]: d for d in db.query("SELECT * FROM decisions WHERE cycle_id = ?", (info["cycle_id"],))}
    assert set(dec) == {"PULS", "CALM"}
    assert dec["PULS"]["passed"] == 1
    assert dec["CALM"]["passed"] == 0 and "amplitude" in dec["CALM"]["failed"]

    # Метрики для графиков: снапшотные для всех, индикаторные (P0) — только PULS
    metrics = {(r["ticker"], r["metric"]) for r in db.query("SELECT DISTINCT ticker, metric FROM metrics")}
    assert ("CALM", "amplitude_atr") in metrics and ("PULS", "volume_ratio") in metrics
    assert ("PULS", "rsi") in metrics and ("CALM", "rsi") not in metrics

    # Повторный цикл с теми же свечами — дубль алерта и пуша не создаётся (п. 11)
    assert second["status"] == "ok" and second["cycle_id"] == info["cycle_id"] + 1
    assert after_first == {"alerts": 2, "pushes": 1}
    assert len(db.query("SELECT * FROM alerts WHERE ticker = 'PULS'")) == 2
    assert len(scanner.push.sent) == 1


def test_daily_report(db):
    user = auth.create_user_key(db, "trader")
    scanner, sched = make_scanner(db)
    scanner.apply_board(run(scanner.iss.securities()), run(scanner.iss.marketdata()))
    ts = f"{TODAY} 15:00:00"
    db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action) VALUES "
               "(NULL, 'PULS', ?, 'up_pulse', 80, 'тест', 'watch')", (ts,))
    s = {**db.effective_settings(user["id"]), "daily_report_enabled": True}
    rep = run(scanner.make_daily_report(user["id"], s, TODAY))
    assert rep["top"]["up"][0]["ticker"] == "PULS"
    assert rep["push"]["sent"] == 1
    title, body = format_report(rep)
    assert TODAY in title and "PULS" in body and "▼" in body
    assert db.query_one("SELECT COUNT(*) AS n FROM daily_reports WHERE key_id = ?", (user["id"],))["n"] == 1

    # Плановая отправка в 19:00: до времени — ничего, после — один раз на пользователя
    async def scheduled():
        sched.start()
        try:
            before = await scanner.check_daily_reports(now_msk().replace(hour=18, minute=59))
            after = await scanner.check_daily_reports(now_msk().replace(hour=19, minute=1))
            again = await scanner.check_daily_reports(now_msk().replace(hour=19, minute=5))
            return before, after, again
        finally:
            await sched.stop()

    before, after, again = run(scheduled())
    assert before == [] and len(after) == 2 and again == []  # глобальный + пользователь


def test_paper_trade_take_and_stop(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    candles = pulse_day(TODAY)
    db.upsert_candles("PULS", candles)
    opened = candles[-20]["ts"]
    entry = candles[-20]["close"]
    db.execute("INSERT INTO paper_trades (key_id, ticker, side, entry_price, stop, take, opened_at) "
               "VALUES (?,?,?,?,?,?,?)", (user["id"], "PULS", "long", entry, entry - 5, entry + 0.3, opened))
    db.execute("INSERT INTO paper_trades (key_id, ticker, side, entry_price, stop, take, opened_at) "
               "VALUES (?,?,?,?,?,?,?)", (user["id"], "PULS", "short", entry, entry + 0.3, entry - 5, opened))
    for t in db.query("SELECT * FROM paper_trades WHERE status = 'open' ORDER BY id"):
        scanner.check_paper_trade(t)
    long_t, short_t = db.query("SELECT * FROM paper_trades ORDER BY id")
    assert long_t["status"] == "take" and long_t["result_pct"] > 0
    assert short_t["status"] == "stop" and short_t["result_pct"] < 0


def test_cold_start_without_today_candles_uses_prev_session(db):
    """Запуск до открытия / в день без торгов: сегодня свечей нет — берём
    прошлую сессию по PREVDATE, чтобы таблица не была пустой."""
    yesterday = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()

    class PrevDayIss(FakeIss):
        def __init__(self):
            super().__init__()
            self.minute = {"PULS": pulse_day(yesterday), "CALM": flat_minutes(f"{yesterday} 10:00:00", 315, 50.0)}

        async def securities(self):
            return [{**s, "PREVDATE": yesterday} for s in await super().securities()]

        async def candles(self, ticker, interval, frm, till=None):
            self.calls.append((ticker, interval, frm, till))
            if interval == 24:
                return daily_history(self.minute[ticker][0]["open"])
            till = (till or "9999-12-31") + " 23:59:59"
            return [c for c in self.minute.get(ticker, []) if frm <= c["ts"] <= till]

    sched = RequestScheduler(TokenBucket(1000, 50))
    scanner = Scanner(db, PrevDayIss(), sched, FakePush())

    async def scenario():
        sched.start()
        try:
            first = await scanner.run_cycle()
            calls_after_first = list(scanner.iss.calls)
            await scanner.run_cycle()
            return first, calls_after_first
        finally:
            await sched.stop()

    info, calls = run(scenario())
    assert info["no_data"] == 0
    assert db.last_candle_ts("CALM")[:10] == yesterday
    assert ("CALM", 1, yesterday, yesterday) in calls  # догрузка прошлой сессии
    # Второй цикл: свечи уже есть — прошлую сессию повторно не тянем
    later = scanner.iss.calls[len(calls):]
    assert not [c for c in later if c[0] == "CALM" and c[1] == 1 and c[2] == yesterday]
