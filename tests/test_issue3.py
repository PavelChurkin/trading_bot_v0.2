"""Доработки issue #3 (оффлайн, без сети).

Часы торгов и прогрев, «Откаты, %» в пределах 0..100, признаки плиты в
настройках, памп-фильтр/поиск пампов, фильтры пушей по действию и цене,
сопровождение (уникальный/ваш кандидат на вход) вне дневного лимита,
paper: количество, профит в ₽, цели в % и ₽, отключаемые авто стоп/тейк и
пуши, кнопка-переключатель Paper, фильтры/сортировка сигналов, очистка
по сроку хранения, графики 14 дней/месяц.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import alerts as al
from app import analysis as an
from app import auth
from app import settings_schema as ss
from app.scanner import WARMUP_REASON, CycleContext, Scanner
from app.timeutil import MSK, fmt_ts, market_closed_reason, next_market_open, now_msk
from conftest import flat_minutes, next_ts, pulse_day, trend_minutes
from test_scanner import TODAY, FakeIss, FakePush, make_scanner, run
from test_web import ADMIN_KEY, env, login  # noqa: F401 — фикстура env

# --------------------------------------------------------------- хелперы


def bar(o: float, c: float, volume: float = 1000.0, wick: float = 0.0) -> dict:
    return {"open": o, "close": c, "high": max(o, c) + wick, "low": min(o, c) - wick, "volume": volume}


def flat_bars(n: int = 20, base: float = 100.0) -> list[dict]:
    return [bar(base, base + (0.05 if i % 2 else -0.05), 1000, 0.05) for i in range(n)]


def mk_alert(ticker="PULS", ts=None, action="enter", direction="up_pulse", price=100.0, **kw) -> al.Alert:
    return al.Alert(ticker=ticker, ts=ts or fmt_ts(now_msk()), direction=direction, score=70, amplitude_atr=2.0,
                    volume_ratio=2.0, level_break=0, reason="тест", action=action, price=price, **kw)


def user_settings(db, kid, **over):
    db.save_settings(f"user:{kid}", {"quiet_from": "", "quiet_to": "", **over})
    return db.effective_settings(kid)


# ------------------------------------------------- п. 1: часы торгов 07:00–23:50

def at(hh: int, mm: int, day: str = "2026-11-10") -> datetime:  # вторник
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").replace(tzinfo=MSK)


def test_market_hours_skip_night():
    assert market_closed_reason(at(23, 49), "07:00", "23:50") is None
    assert "закрыта" in market_closed_reason(at(23, 50), "07:00", "23:50")
    assert "закрыта" in market_closed_reason(at(3, 0), "07:00", "23:50")
    assert market_closed_reason(at(7, 0), "07:00", "23:50") is None
    assert next_market_open(at(23, 55), "07:00", "23:50") == at(7, 0, "2026-11-11")
    # выходные — опционально
    sat = at(12, 0, "2026-11-14")
    assert market_closed_reason(sat, "07:00", "23:50") is None
    assert market_closed_reason(sat, "07:00", "23:50", skip_weekends=True) == "выходной день"


def test_scanner_market_closed_uses_global_settings(db):
    scanner, _ = make_scanner(db)
    assert scanner.market_closed(at(2, 0)) is not None  # по умолчанию включено
    db.save_settings("global", {"market_hours_only": False})
    assert scanner.market_closed(at(2, 0)) is None


# --------------------------------- п. 2: первый анализ после запуска/перерыва — без пушей

def test_warmup_first_cycle_suppressed_then_pushes(db):
    user = auth.create_user_key(db, "trader")
    user_settings(db, user["id"])
    scanner, sched = make_scanner(db)
    assert scanner.warmup_reason() == "первый запуск"

    async def scenario():
        sched.start()
        try:
            first = await scanner.run_cycle()
            pushes_after_first = len(scanner.push.sent)
            # новый импульс в следующем цикле — уже не прогрев
            old = scanner.iss.minute["PULS"]
            flat = flat_minutes(next_ts(old), 15 * 20, old[-1]["close"])
            scanner.iss.minute["PULS"] = old + flat + trend_minutes(next_ts(flat), 15, flat[-1]["close"],
                                                                    0.6 / 15, 4000)
            second = await scanner.run_cycle()
            return first, pushes_after_first, second
        finally:
            await sched.stop()

    first, pushes_first, second = run(scenario())
    assert first.get("warmup") == "первый запуск"
    assert pushes_first == 0
    mine = db.query("SELECT * FROM alerts WHERE key_id = ? ORDER BY id", (user["id"],))
    assert mine and mine[0]["suppressed"] == WARMUP_REASON  # алерт виден в истории, но без пуша
    assert not second.get("warmup")
    assert len(scanner.push.sent) >= 1

    # перерыв дольше порога — снова прогрев
    db.set_state("last_scan_at", fmt_ts(now_msk() - timedelta(hours=3)))
    assert scanner.warmup_reason().startswith("перерыв")
    db.save_settings("global", {"warmup_enabled": False})
    assert scanner.warmup_reason() is None


# ------------------------------------------- п. 19: «Откаты, %» без 500 % / 11800 %

def test_pullback_pct_is_bounded():
    s = ss.defaults()
    # ход серии ≈ 0, но внутри — большие встречные движения: раньше 500 %+
    wc = flat_bars() + [bar(100, 101, 3000), bar(101, 100.02, 3000), bar(100.02, 100.03, 3000, 0.5)]
    p = an.detect_plate(wc, [0.5] * len(wc), s)
    assert 0 <= p["pullback_pct"] <= 100
    assert p["no_correction"] is False and p["is_plate"] is False


# ---------------------------------------------- п. 5: признаки плиты настраиваются

def plate_wc(volume: float) -> list[dict]:
    return flat_bars() + [bar(100, 101, volume), bar(101, 102, volume), bar(102, 103, volume)]


def test_plate_criteria_toggles():
    s = ss.defaults()
    atr = [0.5] * 23
    strong = an.detect_plate(plate_wc(3000), atr, s)
    assert strong["is_plate"] and strong["confident"] and strong["score"] == 4 and strong["pullback_pct"] == 0

    weak = an.detect_plate(plate_wc(1000), atr, s)  # объём не аномальный: 3 из 4
    assert weak["is_plate"] and not weak["confident"] and weak["volume"] is False

    strict = {**s, "plate_min_score": 4}
    assert an.detect_plate(plate_wc(1000), atr, strict)["is_plate"] is False
    no_vol = an.detect_plate(plate_wc(1000), atr, {**strict, "plate_use_volume": False})
    assert no_vol["enabled"] == 3 and no_vol["need"] == 3 and no_vol["is_plate"] and no_vol["confident"]

    # «без коррекции» обязателен, пока учитывается; отключили — не обязателен
    choppy = flat_bars() + [bar(100, 101.5, 3000, 0.6), bar(101.5, 103, 3000, 0.6), bar(103, 104.5, 3000, 0.6)]
    p = an.detect_plate(choppy, [0.5] * 23, s)
    assert p["no_correction"] is False and p["is_plate"] is False
    p = an.detect_plate(choppy, [0.5] * 23, {**s, "plate_use_no_correction": False})
    assert p["is_plate"] is True


def test_plate_settings_described_in_schema():
    by_key = {x.key: x for x in ss.SETTINGS}
    for k in ("plate_use_series", "plate_use_no_correction", "plate_use_volume", "plate_use_move",
              "plate_min_score", "plate_pullback_max_pct"):
        assert k in by_key and by_key[k].help, k


# -------------------------------------- п. 20: памп-фильтр и поиск пампов — раздельно

def pump_snapshot(**over) -> dict:
    snap = {"ticker": "PULS", "ts": f"{TODAY} 15:00:00", "price": 103.0, "amplitude_atr": 2.0, "amplitude_pct": 1.0,
            "volume_ratio": 3.0, "level_break": 0, "direction": 1, "change_hour_atr": 4.0, "change_hour_pct": 3.5,
            "impulse_age_min": 10, "window": 15, "plate": {}}
    snap.update(over)
    return snap


def test_pump_filter_toggle_and_pump_search():
    s = ss.defaults()
    hot = {"rsi": 80, "bb_upper": 101.0}
    ev = al.evaluate(pump_snapshot(), s, intraday=hot)
    assert ev.alert.action == "filtered" and ev.alert.mute_push  # фильтр включён по умолчанию

    off = {**s, "pump_filter_enabled": False}
    ev = al.evaluate(pump_snapshot(), off, intraday=hot)
    assert ev.alert.action != "filtered" and not ev.alert.mute_push

    # поиск пампов: рост за час ≥ k×ATR без импульса окна — отдельный триггер
    quiet = pump_snapshot(amplitude_atr=0.3, volume_ratio=0.8)
    assert al.evaluate(quiet, s, intraday={}).triggered is False
    ev = al.evaluate(quiet, {**s, "pump_search_enabled": True}, intraday={})
    assert ev.triggered and ev.alert.direction == "pump" and ev.alert.action == "pump"
    assert any(c["cond"] == "pump" for c in ev.checks)


# ----------------------------------------- п. 7, 12: фильтр пушей по действию и цене

def test_push_filters_by_action_and_price(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    s = user_settings(db, user["id"], push_actions=["enter", "rebound"], push_price_min=50, push_price_max=500)
    assert scanner.suppression_reason(user["id"], s, mk_alert(action="enter")) is None
    assert scanner.suppression_reason(user["id"], s, mk_alert(action="late")) == "действие отключено"
    assert scanner.suppression_reason(user["id"], s, mk_alert(price=10.0)) == "цена вне диапазона"
    assert scanner.suppression_reason(user["id"], s, mk_alert(price=900.0)) == "цена вне диапазона"
    # п. 6: «Не входить / фиксировать» (avoid) — отдельное действие, его можно выключить
    s2 = {**s, "push_actions": ["enter"]}
    assert scanner.suppression_reason(user["id"], s2, mk_alert(action="avoid", direction="up_plate")) == \
        "действие отключено"


# --------------------------------- п. 9, 10: сопровождение — уникальный/ваш кандидат

def test_tracking_unique_and_your_entry_bypass_daily_limit(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    s = user_settings(db, user["id"], daily_push_limit=1, cooldown_min=0)
    now = now_msk()
    ev_ts = fmt_ts(now - timedelta(days=1))
    # общая фиксация (плита вверх) вчера по PULS и распродажа по CALM
    db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action) "
               "VALUES (NULL, 'PULS', ?, 'up_plate', 80, 'плита', 'avoid')", (ev_ts,))
    db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action) "
               "VALUES (NULL, 'CALM', ?, 'down_pulse', 80, 'вниз', 'selloff')", (ev_ts,))
    db.execute("INSERT INTO favorites (key_id, ticker) VALUES (?, 'CALM')", (user["id"],))
    # дневной лимит уже выбран обычным пушем
    db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action) "
               "VALUES (?, 'OTHER', ?, 'up_pulse', 60, 'x', 'enter')", (user["id"], fmt_ts(now)))
    assert scanner.suppression_reason(user["id"], s, mk_alert(ticker="NEW")) == "лимит 1 в день"

    a1 = mk_alert(ticker="PULS")
    run(scanner.deliver(user["id"], s, a1, None))
    a2 = mk_alert(ticker="CALM")
    run(scanner.deliver(user["id"], s, a2, None))
    assert a1.action == "unique_enter" and a2.action == "your_enter"
    assert any("после фиксации" in x for x in a1.labels) and any("после распродажи" in x for x in a2.labels)
    assert len(scanner.push.sent) == 2  # мимо дневного лимита
    assert al.ALERT_ACTIONS["unique_enter"] and al.ALERT_ACTIONS["your_enter"]

    # повторный «вход» после уже отправленного уникального — обычный
    a3 = mk_alert(ticker="PULS", ts=fmt_ts(now + timedelta(minutes=30)))
    assert scanner.track_entry(user["id"], s, a3) is None and a3.action == "enter"
    # трекинг выключен
    a4 = mk_alert(ticker="CALM", ts=fmt_ts(now + timedelta(minutes=30)))
    assert scanner.track_entry(user["id"], {**s, "track_enabled": False}, a4) is None


# ------------------------------- п. 9, 10, 15, 16, 17, 18: paper — цели, ₽, пуши

def open_trade(db, kid, entry=100.0, qty=10, stop=None, take=None, ticker="PULS", opened_at=None) -> dict:
    cur = db.execute("INSERT INTO paper_trades (key_id, ticker, side, entry_price, stop, take, opened_at, qty) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (kid, ticker, "long", entry, stop, take, opened_at or f"{TODAY} 09:00:00", qty))
    return db.query_one("SELECT * FROM paper_trades WHERE id = ?", (cur.lastrowid,))


def test_paper_pnl_in_rub():
    t = {"side": "long", "entry_price": 100.0, "qty": 10}
    assert Scanner.paper_pnl(t, 105.0) == (5.0, 50.0)
    assert Scanner.paper_pnl({**t, "side": "short"}, 105.0) == (-5.0, -50.0)
    assert Scanner.paper_pnl({**t, "qty": None}, 105.0) == (5.0, None)


def test_paper_targets_pct_and_rub_once(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    s = user_settings(db, user["id"], paper_target_pct=5, paper_target_rub=40)
    t = open_trade(db, user["id"], entry=100.0, qty=10)
    scanner.securities["PULS"] = {"last": 104.0}
    assert run(scanner.paper_targets(t, s)) == ["target_rub"]  # +4 % / +40 ₽: цель в ₽ достигнута
    t = db.query_one("SELECT * FROM paper_trades WHERE id = ?", (t["id"],))
    assert set(json.loads(t["notified"])) == {"target_rub"}

    scanner.securities["PULS"] = {"last": 106.0}
    run(scanner.paper_targets(t, s))
    t = db.query_one("SELECT * FROM paper_trades WHERE id = ?", (t["id"],))
    assert set(json.loads(t["notified"])) == {"target_rub", "target_pct"}
    n = len(scanner.push.sent)
    assert run(scanner.paper_targets(t, s)) == []  # повторно не шлём
    assert len(scanner.push.sent) == n
    titles = [x[1] for x in scanner.push.sent]
    assert any("цель 5%" in x for x in titles) and any("₽" in x for x in titles)


def test_paper_notify_can_be_disabled(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    s = user_settings(db, user["id"], paper_notify_enabled=False)
    t = open_trade(db, user["id"])
    scanner.securities["PULS"] = {"last": 120.0}
    run(scanner.paper_targets(t, s))
    assert scanner.push.sent == []


def test_paper_exit_signal_push_ignores_daily_limit(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    s = user_settings(db, user["id"], daily_push_limit=1, push_actions=["enter"])
    db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action) "
               "VALUES (?, 'OTHER', ?, 'up_pulse', 60, 'x', 'enter')", (user["id"], fmt_ts(now_msk())))
    open_trade(db, user["id"], entry=100.0, qty=5, opened_at=fmt_ts(now_msk() - timedelta(hours=1)))
    a = mk_alert(action="selloff", direction="down_pulse", price=98.0)
    run(scanner.deliver(user["id"], s, a, None))
    assert len(scanner.push.sent) == 1  # обычный пуш подавлен, пуш сопровождения — нет
    _, title, body, _ = scanner.push.sent[0]
    assert "распродажа" in title and "-2.00%" in body and "₽" in body


def test_paper_auto_close_off_keeps_trade_open(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    candles = pulse_day(TODAY)
    db.upsert_candles("PULS", candles)
    entry = candles[-20]["close"]
    t = open_trade(db, user["id"], entry=entry, qty=10, stop=entry - 5, take=entry + 0.3, opened_at=candles[-20]["ts"])
    user_settings(db, user["id"], paper_auto_close=False, paper_target_pct=0)
    run(scanner._update_paper_trades())
    assert db.query_one("SELECT status FROM paper_trades WHERE id = ?", (t["id"],))["status"] == "open"

    user_settings(db, user["id"], paper_auto_close=True)
    run(scanner._update_paper_trades())
    row = db.query_one("SELECT * FROM paper_trades WHERE id = ?", (t["id"],))
    assert row["status"] == "take" and row["profit_rub"] == round(0.3 * 10, 2)
    assert any("закрыта по тейку" in x[1] for x in scanner.push.sent)


# ---------------------------------------------------- веб: Paper, сигналы, очистка

def test_paper_toggle_and_qty(env):
    c = login(env["client"], ADMIN_KEY)
    r = c.post("/api/paper/toggle", json={"ticker": "puls"}).json()
    assert r["ticker"] == "PULS" and r["paper"]["qty"] == 1  # по умолчанию — один лот
    table = c.get("/api/table").json()
    row = next(x for x in table["rows"] if x["ticker"] == "PULS")
    assert row["paper"]["id"] == r["paper"]["id"]
    assert c.get("/api/ticker/PULS").json()["paper"]["id"] == r["paper"]["id"]

    tid = r["paper"]["id"]
    assert c.patch(f"/api/paper/{tid}", json={"qty": 0}).status_code == 422
    p = c.patch(f"/api/paper/{tid}", json={"qty": 10}).json()
    tr = next(x for x in p["trades"] if x["id"] == tid)
    assert tr["qty"] == 10 and tr["unrealized_rub"] is not None
    assert "paper_target_pct" in p["settings"]

    entry = tr["entry_price"]
    p = c.post(f"/api/paper/{tid}/close", json={"price": entry + 2}).json()
    closed = next(x for x in p["trades"] if x["id"] == tid)
    assert closed["profit_rub"] == 20.0
    p = c.patch(f"/api/paper/{tid}", json={"qty": 3}).json()  # пересчёт профита закрытой
    assert next(x for x in p["trades"] if x["id"] == tid)["profit_rub"] == 6.0

    # отжатие кнопки: открытая позиция удаляется
    r = c.post("/api/paper/toggle", json={"ticker": "CALM"}).json()
    r2 = c.post("/api/paper/toggle", json={"ticker": "CALM"}).json()
    assert r2["paper"] is None and r2["removed"] == r["paper"]["id"]
    assert c.post("/api/paper/toggle", json={"ticker": "NOPE"}).status_code == 404


def test_signals_filters_sort_and_enrichment(env):
    c, db = login(env["client"], ADMIN_KEY), env["db"]
    me = c.get("/api/me").json()["user"]["id"]
    ts = f"{TODAY} 12:00:00"
    for t, price, action, sup in (("PULS", 100.6, "enter", None), ("CALM", 50.0, "late", "кулдаун 30 мин")):
        db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action, price, suppressed) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (me, t, ts, "up_pulse", 70, "x", action, price, sup))
    db.execute("INSERT INTO favorites (key_id, ticker) VALUES (?, 'CALM')", (me,))

    r = c.get("/api/alerts").json()
    assert {"directions", "actions", "kinds"} <= set(r)
    a = {x["ticker"]: x for x in r["alerts"]}
    assert a["PULS"]["value_today"] == 1e8 and a["PULS"]["current_price"] and a["PULS"]["sectype"] == "share"

    tick = lambda q: [x["ticker"] for x in c.get(f"/api/alerts?{q}").json()["alerts"]]  # noqa: E731
    assert tick("sort=price_asc") == ["CALM", "PULS"]
    assert tick("sort=price_desc") == ["PULS", "CALM"]
    assert tick("fav=true") == ["CALM"]
    assert tick("action=enter") == ["PULS"]
    assert tick("triggered=true") == ["PULS"]
    assert set(tick("kind=share")) == {"PULS", "CALM"}
    assert tick("kind=bond") == []


def test_admin_purge_rolling_retention(env):
    c, db = login(env["client"], ADMIN_KEY), env["db"]
    old = fmt_ts(now_msk() - timedelta(days=30))
    db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason) VALUES (NULL,'PULS',?,'up_pulse',1,'x')",
               (old,))
    db.save_settings("global", {"retention_days": 7})
    r = c.post("/api/admin/purge").json()
    assert r["ok"] and r["days"] == 7 and r["deleted"]["alerts"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM alerts WHERE ts = ?", (old,))["n"] == 0
    assert db.query("SELECT 1 FROM candles WHERE ticker = 'PULS' LIMIT 1")  # свежее не тронуто
    with TestClient(c.app) as uc:
        login(uc, auth.create_user_key(db, "u")["key"])
        assert uc.post("/api/admin/purge").status_code == 403


def test_ticker_month_chart_uses_hourly_candles(env):
    c = login(env["client"], ADMIN_KEY)
    scanner = env["scanner"]
    start = datetime.fromisoformat(f"{TODAY}T10:00:00") - timedelta(days=29)
    hourly = []
    for i in range(30 * 9):
        ts = start + timedelta(days=i // 9, hours=i % 9)
        p = 100 + (i % 7) * 0.3
        hourly.append({"ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": p, "close": p + 0.1, "high": p + 0.3,
                       "low": p - 0.2, "volume": 5000, "value": 5000 * p})
    asked = []

    async def candles(ticker, interval, frm, till=None):
        asked.append(interval)
        return hourly

    scanner.iss.candles = candles

    def submit(priority, name, fn, dedup_key=None):  # планировщик в TestClient не запущен
        import asyncio
        return asyncio.ensure_future(fn())

    scanner.scheduler.submit = submit
    r = c.get("/api/ticker/PULS?days=30").json()
    assert asked == [60]
    assert r["series"]["source"] == "iss_hourly" and r["series"]["days"] == 30
    assert len(r["series"]["candles"]) > 100  # длинная серия — горизонтальная прокрутка на телефоне
    # неделя — по-прежнему из локальных минуток
    assert c.get("/api/ticker/PULS?days=7").json()["series"]["source"] == "local"


def test_hold_actions_can_be_muted_in_schema():
    """п. 6: «держать/фиксировать» — отдельные действия, их можно выключить в push_actions."""
    by_key = {x.key: x for x in ss.SETTINGS}
    assert "фиксировать" in al.ALERT_ACTIONS["avoid"]
    assert by_key["push_actions"].help


def test_cycle_context_has_warmup_flag():
    assert CycleContext(1, []).warmup is False
    assert FakePush().sent == [] and FakeIss().orderbook_available is False


def test_approach_metrics_intensity_and_peak_forecast():
    """п. 21 (гипотеза): ускорение к сопротивлению на растущем объёме."""
    wc = flat_bars(10) + [bar(100, 100.5, 2000), bar(100.5, 101.2, 3000), bar(101.2, 102.1, 4500)]
    m = an.approach_metrics(wc, 0.5, level_high=102.6, level_low=99.0)
    assert m["approach_speed_atr"] > 0 and m["approach_intensity"] > m["approach_speed_atr"]
    assert m["approach_level"] == 102.6 and m["approach_distance_atr"] == 1.0
    assert 102.1 < m["pump_peak_forecast"] <= 102.6 + 0.5  # потолок: уровень + 1 ATR
    down = an.approach_metrics(list(reversed(wc)), 0.5, level_high=102.6, level_low=99.0)
    assert down["approach_speed_atr"] < 0 and down["pump_peak_forecast"] is None
