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
