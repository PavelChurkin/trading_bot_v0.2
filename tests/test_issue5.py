"""Доработки issue #5 (оффлайн, без сети).

Paper: баг MGKL/TATN (стоп/тейк не той стороны, цена, которой не было),
ручная правка стопа/тейка, смена стороны, комиссии и налог, очистка
статистики; триггеры и фильтры (исторический потолок, потенциал движения,
ликвидность), плита после плато, пин-бары и уровни скрытой ликвидности,
режим «Отошёл», фильтр сигналов по времени, место на диске, отчёт со
статистикой рынка и корреляцией с IMOEX.
"""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app import alerts as al
from app import analysis as an
from app import auth
from app import paper
from app import settings_schema as ss
from app.timeutil import fmt_ts, now_msk
from conftest import flat_minutes, pulse_day
from test_issue3 import mk_alert, open_trade, user_settings
from test_scanner import TODAY, make_scanner, run
from test_web import ADMIN_KEY, env, login  # noqa: F401 — фикстура env


# ============================================== п. 15: баг paper (MGKL / TATN)

def test_mgkl_sell_verdict_levels_not_copied_to_long_trade():
    """MGKL: вердикт SELL даёт уровни шорта (стоп выше, тейк ниже); при
    огромном ATR тейк уходил в минус. Для лонга уровни пересчитываются."""
    entry = 0.37
    # Уровни вердикта «SELL» для шорта при ATR ≈ 25 % цены: стоп выше, тейк < 0
    sell_stop, sell_take = entry * 1.74, entry - 3 * entry * 0.5
    assert sell_take < 0
    stop, take, note = paper.default_levels("long", entry, sell_stop, sell_take, anchor=entry, atr_pct=50.0,
                                            stop_mult=1.5, take_mult=3.0)
    assert 0 < stop < entry < take
    assert note and "пересчитаны" in note
    assert paper.levels_ok("long", entry, stop, take)
    # ATR ограничен: стоп не дальше 90 % — цена > 0
    assert stop >= entry * 0.1 - 1e-9


def test_tatn_stale_anchor_levels_moved_to_entry():
    """TATN: уровни считались от цены алерта, а вход — по текущей: стоп
    оказывался «пробит» сразу. Теперь расстояние в % переносится к входу."""
    stop, take, note = paper.default_levels("long", 110.0, 97.0, 106.0, anchor=100.0, atr_pct=2.0,
                                            stop_mult=1.5, take_mult=3.0)
    assert note is None
    assert round(stop, 4) == 106.7 and round(take, 4) == 116.6  # −3 % / +6 % от 110


def test_check_paper_trade_ignores_inverted_levels_and_fills_gap_at_open(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    candles = pulse_day(TODAY)
    db.upsert_candles("PULS", candles)
    entry = candles[-20]["close"]
    # «Перевёрнутая» сделка из старой версии: лонг со стопом выше входа — не закрывается по фантомной цене
    bad = open_trade(db, user["id"], entry=entry, stop=entry * 1.74, take=-0.1, opened_at=candles[-20]["ts"])
    assert scanner.check_paper_trade(bad) is None
    assert db.query_one("SELECT status FROM paper_trades WHERE id = ?", (bad["id"],))["status"] == "open"
    # Гэп: свеча открылась уже за стопом — исполнение по открытию, а не по уровню
    t = {"side": "long", "stop": 100.0, "take": 120.0}
    assert paper.level_hit(t, {"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.5}) == ("stop", 95.0)
    assert paper.level_hit(t, {"open": 101.0, "high": 101.5, "low": 99.0, "close": 100.5}) == ("stop", 100.0)
    assert paper.level_hit({**t, "side": "short", "stop": 120.0, "take": 100.0},
                           {"open": 98.0, "high": 99.0, "low": 97.0, "close": 98.0}) == ("take", 98.0)
    assert not paper.candle_ok({"open": 0, "high": 1, "low": 0, "close": 1})


def test_edited_levels_checked_only_after_edit(db):
    """Правка стопа не закрывает сделку по прошлым свечам (levels_at)."""
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    candles = pulse_day(TODAY)
    db.upsert_candles("PULS", candles)
    entry = candles[-20]["close"]
    t = open_trade(db, user["id"], entry=entry, stop=entry - 5, take=entry + 0.3, opened_at=candles[-20]["ts"])
    db.execute("UPDATE paper_trades SET levels_at = ? WHERE id = ?", (candles[-1]["ts"] + "Z", t["id"]))
    t = db.query_one("SELECT * FROM paper_trades WHERE id = ?", (t["id"],))
    assert scanner.check_paper_trade(t) is None  # тейк был бы взят на прошлых свечах


def test_open_paper_from_sell_alert_long_side(env):
    c, db = login(env["client"], ADMIN_KEY), env["db"]
    price = env["scanner"].securities["PULS"]["last"]
    extra = {"stop": price * 1.05, "take": price * 0.9, "verdict": "SELL"}
    cur = db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action, price, extra) "
                     "VALUES (NULL,'PULS',?,'down_plate',70,'x','rebound',?,?)",
                     (fmt_ts(now_msk()), price, json.dumps(extra)))
    r = c.post("/api/paper", json={"alert_id": cur.lastrowid, "side": "long"}).json()
    tr = next(x for x in r["trades"] if x["id"] == r["id"])
    assert tr["side"] == "long" and 0 < tr["stop"] < tr["entry_price"] < tr["take"]
    assert "пересчитаны" in tr["note"]
    # Ручные уровни не той стороны — 422
    assert c.post("/api/paper", json={"ticker": "PULS", "side": "long", "stop": price * 1.1}).status_code == 422
    assert c.post("/api/paper", json={"ticker": "PULS", "side": "long", "take": -1}).status_code == 422


# ================================================= п. 3, 4, 5, 13: Paper в вебе

def test_paper_manual_levels_side_flip_and_money(env):
    c, db = login(env["client"], ADMIN_KEY), env["db"]
    price = env["scanner"].securities["PULS"]["last"]
    tid = c.post("/api/paper/toggle", json={"ticker": "PULS"}).json()["paper"]["id"]
    # Ручная правка стопа/тейка: проверка от текущей цены, отметка levels_at
    assert c.patch(f"/api/paper/{tid}", json={"stop": price * 1.01}).status_code == 422
    assert c.patch(f"/api/paper/{tid}", json={"take": price * 0.99}).status_code == 422
    p = c.patch(f"/api/paper/{tid}", json={"stop": round(price * 0.97, 4), "take": round(price * 1.05, 4)}).json()
    tr = next(x for x in p["trades"] if x["id"] == tid)
    assert tr["stop"] == round(price * 0.97, 4) and tr["take"] == round(price * 1.05, 4) and tr["levels_at"]
    # Смена стороны: уровни зеркалятся относительно входа
    p = c.patch(f"/api/paper/{tid}", json={"side": "short"}).json()
    tr = next(x for x in p["trades"] if x["id"] == tid)
    assert tr["side"] == "short" and tr["take"] < tr["entry_price"] < tr["stop"]
    # Деньги: потрачено с комиссией входа 0.3 %, без «Суммы результатов»
    c.patch(f"/api/paper/{tid}", json={"qty": 10})
    p = c.get("/api/paper").json()
    tr = next(x for x in p["trades"] if x["id"] == tid)
    assert tr["money"]["cost_rub"] == round(tr["entry_price"] * 10 * 1.003, 2)
    assert p["stats"]["spent_rub"] == tr["money"]["cost_rub"] and "total_pct" not in p["stats"]
    assert p["settings"]["fee_entry_pct"] == 0.3 and p["settings"]["tax_profit_pct"] == 13


def test_paper_money_fees_and_tax():
    s = {"fee_entry_pct": 0.3, "fee_exit_pct": 0.3, "tax_profit_pct": 13}
    t = {"side": "long", "entry_price": 100.0, "qty": 10}
    m = paper.money(t, s, 110.0)
    assert m["cost_rub"] == 1003.0 and m["fee_rub"] == 6.3
    assert m["tax_rub"] == round(93.7 * 0.13, 2) and m["net_rub"] == round(93.7 * 0.87, 2)
    loss = paper.money(t, s, 90.0)
    assert loss["tax_rub"] == 0 and loss["net_rub"] == -105.7


def test_paper_default_side_setting(env):
    c, db = login(env["client"], ADMIN_KEY), env["db"]
    assert c.put("/api/settings", json={"values": {"paper_default_side": "short"}}).status_code == 200
    r = c.post("/api/paper/toggle", json={"ticker": "PULS"}).json()
    assert r["paper"]["side"] == "short" and r["paper"]["take"] < r["paper"]["entry_price"] < r["paper"]["stop"]
    assert c.put("/api/settings", json={"values": {"paper_default_side": "sideways"}}).status_code == 422
    # «Сбросить по умолчанию» — авто
    c.delete("/api/settings?key=paper_default_side")
    assert c.get("/api/settings").json()["values"]["paper_default_side"] == "auto"


def test_paper_clear_statistics(env):
    c = login(env["client"], ADMIN_KEY)
    c.post("/api/paper/toggle", json={"ticker": "PULS"})
    c.post("/api/paper/toggle", json={"ticker": "CALM"})
    r = c.delete("/api/paper").json()
    assert r["deleted"] == 2 and r["trades"] == [] and r["stats"]["open"] == 0


def test_paper_auto_close_moved_to_risk_and_fees_in_schema():
    spec = {s.key: s for s in ss.SETTINGS}
    assert spec["paper_auto_close"].group == "Риск"
    assert (spec["fee_entry_pct"].default, spec["fee_exit_pct"].default, spec["tax_profit_pct"].default) == (0.3, 0.3, 13.0)
    assert spec["pump_search_enabled"].group == "Триггеры"
    for k in ("trig_up_pulse", "trig_down_pulse", "trig_up_plate", "trig_down_plate", "trig_sector_dump",
              "trig_plateau_dump", "trig_pinbar"):
        assert spec[k].group == "Триггеры" and spec[k].default is True


# ========================================== п. 16 / задачи 1, 2, 4: триггеры и фильтры

SNAP = {
    "ticker": "SBER", "ts": "2026-11-02 12:00:00", "window": 15, "price": 100.0, "amplitude_pct": 0.9,
    "amplitude_atr": 2.0, "volume_ratio": 2.5, "level_break": 1, "level_high": 99.5, "level_low": 98.0,
    "level_distance_atr": 0.0, "change_hour_pct": 0.8, "change_hour_atr": 1.0, "direction": 1,
    "impulse_age_min": 15, "impulse_move_pct": 0.9, "pullback_pct": 5.0, "atr": 0.4,
    "plate": {"score": 1, "is_plate": False, "no_correction": False},
}
CALM = {"rsi": 55, "bb_upper": 110.0, "bb_lower": 90.0}


def test_trigger_toggle_disables_pulse(settings):
    assert al.evaluate(SNAP, settings, CALM).triggered
    ev = al.evaluate(SNAP, {**settings, "trig_up_pulse": False}, CALM)
    assert not ev.triggered and ev.alert is None and "trigger_off" in ev.failed
    # Обратное направление по-прежнему работает
    down = al.evaluate({**SNAP, "direction": -1, "level_break": -1}, {**settings, "trig_up_pulse": False}, CALM)
    assert down.triggered and down.alert.direction == "down_pulse"
    assert al.trigger_on(settings, "pinbar_level") and not al.trigger_on({"trig_pinbar": False}, "pinbar_level")


def test_potential_min_range_blocks_flat_history(settings):
    flat = {**SNAP, "hist_ranges": {"5d": [100.5, 100.0]}}  # размах 0.5 % < 1 %
    ev = al.evaluate(flat, settings, CALM)
    assert not ev.triggered and "potential" in ev.failed
    wide = al.evaluate({**SNAP, "hist_ranges": {"5d": [110.0, 95.0]}}, settings, CALM)
    assert wide.triggered and "potential" not in wide.failed
    # Без истории фильтр не применяется
    assert al.evaluate(SNAP, settings, CALM).triggered


def test_liquidity_filter_mutes_and_blocks_buy(settings):
    ev = al.evaluate({**SNAP, "liquidity_score": 0.4}, settings, CALM)
    a = ev.alert
    assert a.mute_push and a.action == "filtered" and a.extra["mute_reason"] == "низкая ликвидность"
    assert "Низколиквидная (0.40)" in a.labels and "liquidity" in ev.failed
    ok = al.evaluate({**SNAP, "liquidity_score": 0.95}, settings, CALM).alert
    assert not ok.mute_push and ok.action == "enter" and ok.extra["liquidity_score"] == 0.95


def test_hist_ceiling_filter(settings):
    s = {**settings, "hist_filter_enabled": True, "correction_threshold_coef": 0.7}
    # max 102, min 92 → опасная зона ≥ 102 − 10 × 0.7 = 95; цена 100 — в зоне
    ev = al.evaluate({**SNAP, "hist_ranges": {"5d": [102.0, 92.0]}}, s, CALM)
    a = ev.alert
    assert a.action == "filtered" and a.mute_push and "hist_ceiling" in ev.failed
    assert a.extra["hist"]["danger"] == 95.0 and "исторический потолок" in a.labels
    # Цена у дна диапазона — покупка разрешена
    low = al.evaluate({**SNAP, "price": 93.0, "hist_ranges": {"5d": [102.0, 92.0]}}, s, CALM).alert
    assert low.action == "enter" and not low.mute_push
    # Выключено по умолчанию
    assert al.evaluate({**SNAP, "hist_ranges": {"5d": [102.0, 92.0]}}, settings, CALM).alert.action == "enter"
    # Идемпотентность: повторный вызов не дублирует метки
    al.apply_filters(a, {**SNAP, "hist_ranges": {"5d": [102.0, 92.0]}}, s)
    assert a.labels.count("исторический потолок") == 1


def test_hist_ranges_and_liquidity_score_in_scanner(db):
    scanner, _ = make_scanner(db)
    scanner.securities["PULS"] = {"active": True, "high": 105.0, "low": 99.0, "last": 104.0,
                                  "daily_hist": [["2026-01-0%d" % i, 100.0 + i, 95.0 + i, 98.0] for i in range(1, 8)]}
    r = scanner.hist_ranges("PULS")
    assert r["1d"] == [105.0, 99.0] and r["5d"] == [107.0, 99.0]
    assert r["3m"] == r["1m"] == [107.0, 96.0]  # короткая история — по тому, что есть (≥ 2 дней)
    scanner.securities["PULS"]["daily_hist"] = scanner.securities["PULS"]["daily_hist"][:1]
    assert set(scanner.hist_ranges("PULS")) == {"1d"}
    # Эталон ликвидности — голубая фишка; у бумаги половина минуток без объёма → 0.5
    db.upsert_candles("SBER", flat_minutes(f"{TODAY} 10:00:00", 100))
    thin = flat_minutes(f"{TODAY} 10:00:00", 100)
    for i, c in enumerate(thin):
        if i % 2:
            c["volume"] = 0
    db.upsert_candles("THIN", thin)
    ref = scanner.liquidity_reference()
    assert ref == (f"{TODAY} 10:00:00", 100)
    assert scanner.liquidity_score("THIN", ref) == 0.5 and scanner.liquidity_score("SBER", ref) == 1.0
    assert scanner.liquidity_score("THIN", None) is None


def test_alert_threshold_percent_suppresses_small_moves(db, settings):
    scanner, _ = make_scanner(db)
    s = {**settings, "alert_threshold_percent": 2.0, "quiet_from": "", "quiet_to": "", "cooldown_min": 0}
    a = mk_alert(extra={"amplitude_pct": 0.9, "day_change_pct": -1.5})
    assert scanner.suppression_reason(1, s, a) == "изменение 1.50% < 2%"
    big = mk_alert(extra={"amplitude_pct": 0.9, "day_change_pct": -2.5})
    assert scanner.suppression_reason(1, s, big) is None


# ======================================================= задача 2: плита после плато

def test_plateau_buy_after_long_flat(db, settings):
    scanner, _ = make_scanner(db)
    s = {**settings, "plateau_min_hours": 3.0, "plateau_max_range_pct": 1.5, "candle_window_min": 15}
    db.upsert_candles("PULS", flat_minutes(f"{TODAY} 07:00:00", 4 * 60))  # 07:00–10:59 боковик ±0.1
    a = mk_alert(ts=f"{TODAY} 11:45:00", action="rebound", direction="down_plate", price=97.0)
    info = scanner.plateau_check(a, {"plate": {"n": 3}}, s)
    assert info and info["hours"] == 3.0 and info["range_pct"] < 1.5
    assert a.action == "plateau_buy" and "кандидат на покупку" in a.reason
    # Плато короче нужного (минуток < 50 % от 10 ч) / выключенный триггер — без изменений
    b = mk_alert(ts=f"{TODAY} 11:45:00", action="rebound", direction="down_plate")
    assert scanner.plateau_check(b, {"plate": {"n": 3}}, {**s, "plateau_min_hours": 10.0}) is None
    assert scanner.plateau_check(b, {"plate": {"n": 3}}, {**s, "trig_plateau_dump": False}) is None
    assert b.action == "rebound"


# ========================================= задачи 5, 6: пин-бары и уровни ликвидности

def _c(ts, o, h, lo, c, v=1000.0):
    return {"ts": ts, "open": o, "high": h, "low": lo, "close": c, "volume": v}


def test_detect_pinbar_bullish_and_bearish():
    candles = [_c(f"2026-11-02 10:{i:02d}:00", 100, 100.2, 99.8, 100.1) for i in range(10)]
    candles.append(_c("2026-11-02 10:10:00", 100.0, 100.3, 97.0, 100.2, 5000))  # хвост вниз
    candles.append(_c("2026-11-02 10:11:00", 100.0, 103.0, 99.9, 99.8, 5000))  # хвост вверх
    found = an.detect_pinbar(candles, shadow_ratio=2.0, max_body_ratio=0.3)
    kinds = {p["ts"][-8:-3]: (p["type"], p["key_level"]) for p in found}
    assert kinds["10:10"] == (an.BULLISH_SWEEP, 97.0) and kinds["10:11"] == (an.BEARISH_SWEEP, 103.0)
    # Подтверждение объёмом: при одинаковом объёме z-score не проходит
    same = candles[:10] + [{**candles[10], "volume": 1000.0}]
    assert not an.detect_pinbar(same, 2.0, 0.3, volume_z=1.5)
    assert an.detect_pinbar(candles[:11], 2.0, 0.3, volume_z=1.5)
    assert an.pinbar_touched(an.BULLISH_SWEEP, 97.0, {"low": 96.9, "high": 99})


def test_pinbar_level_approach_push_once(db):
    from app.scanner import CycleContext
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    db.upsert_candles("PULS", flat_minutes(f"{TODAY} 10:00:00", 90))
    g = db.global_settings()
    s = user_settings(db, user["id"])
    old = f"{TODAY} 08:00:00"  # уровень старше окна, цена от него уже уходила
    db.execute("INSERT INTO pinbar_levels (ticker, ts, pattern_type, key_level, created_at, departed) "
               "VALUES ('PULS', ?, ?, 99.6, ?, 1)", (old, an.BULLISH_SWEEP, old))
    ctx = CycleContext(1, [(None, g), (user["id"], s)])
    snap = {"ts": f"{TODAY} 11:15:00", "price": 100.0}
    run(scanner.pinbar_scan("PULS", g, snap, ctx))
    pushes = [p for p in scanner.push.sent if p[0] == user["id"]]
    assert len(pushes) == 1 and "уровню закупки" in pushes[0][2]
    rows = db.query("SELECT key_id, action FROM alerts WHERE direction = 'pinbar_level'")
    assert {r["key_id"] for r in rows} == {None, user["id"]}
    run(scanner.pinbar_scan("PULS", g, {**snap, "ts": f"{TODAY} 11:30:00"}, ctx))
    assert len([p for p in scanner.push.sent if p[0] == user["id"]]) == 1  # один раз
    lv = scanner.pinbar_levels("PULS")
    assert lv[0]["label"].startswith("бычий") and lv[0]["key_level"] == 99.6


def test_pinbar_level_not_departed_yet_no_push(db):
    from app.scanner import CycleContext
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    db.upsert_candles("PULS", flat_minutes(f"{TODAY} 10:00:00", 90))
    g = db.global_settings()
    db.execute("INSERT INTO pinbar_levels (ticker, ts, pattern_type, key_level, created_at) "
               "VALUES ('PULS', ?, ?, 99.6, ?)", (f"{TODAY} 08:00:00", an.BULLISH_SWEEP, TODAY))
    ctx = CycleContext(1, [(None, g), (user["id"], user_settings(db, user["id"]))])
    run(scanner.pinbar_scan("PULS", g, {"ts": f"{TODAY} 11:15:00", "price": 100.0}, ctx))
    assert not scanner.push.sent  # цена не уходила от уровня — это не «возврат»
    run(scanner.pinbar_scan("PULS", g, {"ts": f"{TODAY} 11:15:00", "price": 103.0}, ctx))
    assert db.query_one("SELECT departed FROM pinbar_levels")["departed"] == 1


# ============================================================= п. 9: «Отошёл»

def test_away_freezes_pushes_and_redelivers_last_two_scans(db):
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    s = user_settings(db, user["id"], cooldown_min=0, daily_push_limit=0)
    assert run(scanner.set_away(user["id"], True))["away"]
    scanner.cycle_id = 5
    for i in range(13):
        a = mk_alert(ticker=f"T{i:02d}", ts=fmt_ts(now_msk() - timedelta(minutes=i)))
        a.extra["cycle_id"] = 5 if i < 12 else 2  # последний — старое сканирование
        assert scanner.suppression_reason(user["id"], s, a) == "отошёл"
        db.execute(al.ALERT_INSERT_SQL, a.db_row(user["id"], False, "отошёл"))
    assert not scanner.push.sent
    r = run(scanner.set_away(user["id"], False))
    assert r == {"away": False, "since": None, "redelivered": 12, "pushed": 10, "summary": 2}
    assert len(scanner.push.sent) == 11 and "ещё 2 сигналов" in scanner.push.sent[-1][1]
    assert db.query_one("SELECT COUNT(*) AS n FROM alerts WHERE suppressed = 'отошёл'")["n"] == 1
    assert scanner.away_since(user["id"]) is None


# ======================================================= п. 8: место на диске

def test_disk_guard_pauses_once_and_check_resumes(db, monkeypatch):
    import shutil
    from collections import namedtuple
    user = auth.create_user_key(db, "trader")
    scanner, _ = make_scanner(db)
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: du(100 * 1024 ** 3, 99.5 * 1024 ** 3, 0.5 * 1024 ** 3))
    assert run(scanner.disk_guard()) and run(scanner.disk_guard())
    assert len([p for p in scanner.push.sent if p[0] == user["id"]]) == 1  # один пуш
    assert scanner.storage()["low"] and scanner.storage()["paused"]
    assert scanner.check_storage()["paused"]  # места всё ещё мало
    monkeypatch.setattr(shutil, "disk_usage", lambda p: du(100 * 1024 ** 3, 50 * 1024 ** 3, 50 * 1024 ** 3))
    st = scanner.check_storage()
    assert st["resumed"] and not st["paused"] and not run(scanner.disk_guard())


# ===================================================== задача 3: статистика рынка

def test_pearson_and_market_stats():
    assert round(an.pearson([1, 2, 3], [2, 4, 6]), 6) == 1.0
    assert round(an.pearson([1, 2, 3], [3, 2, 1]), 6) == -1.0
    assert an.pearson([1, 1, 1], [1, 2, 3]) is None
    days = [f"2026-10-{d:02d}" for d in range(1, 26)]
    idx = [[d, 1000 + (i % 4) * 10 + i] for i, d in enumerate(days)]
    same = [[d, c * 1.01, c * 0.99, c] for d, c in idx]  # ходит как индекс
    anti = [[d, 2.02 * 2000 - c, 1.98 * 2000 - c, 2 * 2000 - c] for d, c in idx]
    st = an.market_stats({"IDX": same, "ANTI": anti}, idx)
    assert st["tickers"] == 2 and st["correlation"][0]["ticker"] == "IDX"
    assert st["high"] == ["IDX"] and st["low"] == ["ANTI"]
    assert round(st["correlation"][0]["corr"], 3) == 1.0
    assert 1.9 < [r for r in st["correlation"] if r["ticker"] == "IDX"][0]["intraday_pct"] < 2.1
    # Без индекса — только колебания, без корреляции
    assert an.market_stats({"IDX": same}, [])["correlation"] == []


# ===================================================== веб: новые эндпоинты issue #5

def test_web_table_last_action_liquidity_and_ticker_levels(env):
    c, db, sc = login(env["client"], ADMIN_KEY), env["db"], env["scanner"]
    kid = c.get("/api/me").json()["user"]["id"]
    db.execute(al.ALERT_INSERT_SQL, mk_alert(ticker="PULS", action="exit").db_row(kid, True, None))
    sc.securities["PULS"]["liquidity_score"] = 0.42
    rows = {r["ticker"]: r for r in c.get("/api/table").json()["rows"]}
    assert rows["PULS"]["last_action"] == "exit" and rows["PULS"]["last_action_label"]
    assert rows["PULS"]["liquidity_score"] == 0.42 and "last_action" not in rows["CALM"]
    db.execute("INSERT INTO pinbar_levels (ticker, ts, pattern_type, key_level, created_at) VALUES "
               "('PULS', ?, ?, 99.0, ?)", (fmt_ts(now_msk()), an.BEARISH_SWEEP, fmt_ts(now_msk())))
    t = c.get("/api/ticker/PULS").json()
    assert t["levels"][0]["key_level"] == 99.0 and t["levels"][0]["label"].startswith("медвежий")


def test_web_alerts_time_filter(env):
    c, db = login(env["client"], ADMIN_KEY), env["db"]
    kid = c.get("/api/me").json()["user"]["id"]
    for hhmm in ("09:30", "12:15", "15:45"):
        db.execute(al.ALERT_INSERT_SQL, mk_alert(ts=f"2026-11-02 {hhmm}:00").db_row(kid, True, None))
    got = c.get("/api/alerts?day=2026-11-02&time=12:00").json()["alerts"]
    assert sorted(a["ts"][11:16] for a in got) == ["12:15", "15:45"]
    assert c.get("/api/alerts?time=noon").status_code == 422


def test_web_away_storage_purge_and_report(env):
    c, db = login(env["client"], ADMIN_KEY), env["db"]
    assert c.post("/api/away", json={"on": True}).json()["away"]
    assert c.get("/api/me").json()["away_since"]
    assert c.post("/api/away", json={"on": False}).json()["away"] is False
    st = c.get("/api/admin/storage").json()
    assert st["db_bytes"] > 0 and st["disk_free"] > 0 and "min_free_gb" in st
    assert "low" in c.post("/api/admin/storage/check").json()
    p = c.post("/api/admin/purge").json()
    assert p["ok"] and "before_bytes" in p and "after_bytes" in p and "pinbar_levels" in p["deleted"]
    rep = c.get("/api/report").json()
    assert "correlation" in rep["market_stats"] and "avg_intraday_pct" in rep["market_stats"]
    # Пользователь без прав администратора не видит хранилище
    user = auth.create_user_key(db, "u")
    c2 = TestClient(c.app)
    login(c2, user["key"])
    assert c2.get("/api/admin/storage").status_code == 403
