"""Alert-объект (п. 18), типы алертов (п. 23), памп-фильтр/«поздно», анти-спам."""

from datetime import datetime

from app import alerts as al
from app import analysis as an
from app.scanner import Scanner
from app.timeutil import MSK

from conftest import pulse_day

SNAP = {
    "ticker": "SBER", "ts": "2026-11-02 12:00:00", "window": 15, "price": 100.0, "amplitude_pct": 0.9,
    "amplitude_atr": 2.0, "volume_ratio": 2.5, "level_break": 1, "level_high": 99.5, "level_low": 98.0,
    "level_distance_atr": 0.0, "change_hour_pct": 0.8, "change_hour_atr": 1.0, "direction": 1,
    "impulse_age_min": 15, "impulse_move_pct": 0.9, "pullback_pct": 5.0, "atr": 0.4,
    "plate": {"score": 1, "is_plate": False, "no_correction": False},
}
CALM = {"rsi": 55, "bb_upper": 110.0, "bb_lower": 90.0}


def test_p1_trigger_and_failed_list(settings):
    ev = al.evaluate(SNAP, settings)
    assert ev.triggered and ev.alert is None  # без индикаторов (P1) алерт не строится
    weak = al.evaluate({**SNAP, "amplitude_atr": 0.75, "volume_ratio": 0.5, "level_break": 0,
                        "level_distance_atr": 0.4}, settings)
    assert not weak.triggered
    assert {"amplitude", "volume", "level", "plate"} <= set(weak.failed)
    assert 0 < weak.closeness < 1


def test_amplitude_above_max_is_not_pulse(settings):
    ev = al.evaluate({**SNAP, "amplitude_atr": 9.0}, settings)
    assert not ev.triggered and "amplitude" in ev.failed


def test_alert_object_fields_and_up_pulse(settings):
    ev = al.evaluate(SNAP, settings, CALM)
    a = ev.alert
    assert a.direction == "up_pulse" and a.action == "enter" and not a.mute_push
    d = a.to_dict()
    for f in ("ticker", "ts", "direction", "score", "amplitude_atr", "volume_ratio", "level_break", "reason"):
        assert f in d
    assert 0 <= a.score <= 100
    # причина — с цифрами
    assert "2.0×ATR" in a.reason and "×2.5" in a.reason and "пробой уровня 99.5" in a.reason


def test_down_pulse_and_plates(settings):
    down = al.evaluate({**SNAP, "direction": -1, "level_break": -1}, settings, CALM).alert
    assert down.direction == "down_pulse" and down.action == "selloff"
    plate = {"score": 4, "is_plate": True, "confident": True, "no_correction": True, "direction": 1,
             "move_pct": 3.0, "move_atr": 7.0, "pullback_pct": 2.0, "n": 3}
    up = al.evaluate({**SNAP, "plate": plate}, settings, CALM).alert
    assert up.direction == "up_plate" and up.action == "avoid" and "плита уверенно (4/4)" in up.labels
    dn = al.evaluate({**SNAP, "direction": -1, "plate": {**plate, "direction": -1, "score": 3, "confident": False}},
                     settings, CALM).alert
    assert dn.direction == "down_plate" and dn.action == "rebound" and "плита (3/4)" in dn.labels


def test_hour_exclusion_filters_entry(settings):
    """П. 10: исключать бумаги, выросшие за час больше X%."""
    ev = al.evaluate({**SNAP, "change_hour_pct": 4.2}, settings, CALM)
    assert ev.alert.action == "filtered" and ev.alert.mute_push
    assert "pump_filter" in ev.failed
    assert "4.20% за час" in ev.alert.reason


def test_pump_bundle_three_of_four_mutes_two_of_four_labels(settings):
    hot = {"rsi": 75, "bb_upper": 99.0}
    muted = al.evaluate(SNAP, settings, hot).alert  # rsi + bb + volume = 3/4
    assert muted.action == "filtered" and muted.mute_push and "перегрето (3/4)" in muted.labels
    warm = al.evaluate(SNAP, settings, {"rsi": 60, "bb_upper": 99.0}).alert  # bb + volume = 2/4
    assert warm.action == "late" and not warm.mute_push and "перегрето (2/4)" in warm.labels


def test_old_impulse_marked_late(settings):
    a = al.evaluate({**SNAP, "impulse_age_min": 60}, settings, CALM).alert
    assert a.action == "late"
    assert any("возраст импульса 60 мин" in x for x in a.labels)


def test_late_verdict_propagates(settings):
    a = al.evaluate(SNAP, settings, CALM, verdict={"verdict": "LATE", "label": "⏳", "stop": 99, "take": 102}).alert
    assert a.action == "late" and a.extra["stop"] == 99


def test_full_pipeline_from_candles(settings):
    snap = an.compute_snapshot("TEST", pulse_day(), settings)
    ev = al.evaluate(snap, settings, CALM)
    assert ev.alert.direction == "up_pulse"


# ---------------------------------------------------------- формат пуша
def test_push_template_user_configurable(settings):
    a = al.evaluate(SNAP, settings, CALM).alert
    msg = al.format_push(a, settings)
    assert msg["title"] == "SBER · Пульсация вверх"
    assert "не вероятность" in msg["body"]
    custom = {**settings, "push_title_template": "{arrow} {ticker} {score}", "push_body_template":
              "amp {amplitude_atr} vol {volume_ratio} lvl {level_break} {unknown}"}
    msg = al.format_push(a, custom)
    assert msg["title"].startswith("▲ SBER ")
    assert msg["body"] == "amp 2 vol 2.5 lvl вверх {unknown}"  # неизвестное поле не роняет пуш


def test_broken_template_falls_back(settings):
    a = al.evaluate(SNAP, settings, CALM).alert
    msg = al.format_push(a, {**settings, "push_title_template": "{ticker", "push_body_template": "x"})
    assert msg["title"] == "SBER · Пульсация вверх"


# ------------------------------------------------------------ анти-спам
def _scanner(db):
    return Scanner(db, iss=None, scheduler=None, push=None)


def _alert(ts="2026-11-02 12:00:00", ticker="SBER", direction="up_pulse"):
    return al.Alert(ticker=ticker, ts=ts, direction=direction, score=70, amplitude_atr=2, volume_ratio=2,
                    level_break=1, reason="r")


def test_suppression_cooldown_limit_quiet_hours(db, settings):
    sc = _scanner(db)
    noon = datetime(2026, 11, 2, 12, 5, tzinfo=MSK)
    s = {**settings, "cooldown_min": 30, "daily_push_limit": 2, "quiet_from": "23:00", "quiet_to": "07:00"}
    assert sc.suppression_reason(1, s, _alert(), now=noon) is None
    db.execute(al.ALERT_INSERT_SQL, _alert().db_row(1, True, None))
    # тот же тикер через 5 минут — кулдаун (дедуп N минут на тикер, п. 11)
    assert sc.suppression_reason(1, s, _alert("2026-11-02 12:05:00"), now=noon) == "кулдаун 30 мин"
    # другой тикер — проходит; после двух пушей — дневной лимит
    assert sc.suppression_reason(1, s, _alert(ticker="GAZP"), now=noon) is None
    db.execute(al.ALERT_INSERT_SQL, _alert(ticker="GAZP").db_row(1, True, None))
    assert sc.suppression_reason(1, s, _alert(ticker="LKOH"), now=noon) == "лимит 2 в день"
    night = datetime(2026, 11, 2, 23, 30, tzinfo=MSK)
    s2 = {**s, "daily_push_limit": 0}
    assert sc.suppression_reason(1, s2, _alert(ticker="LKOH", ts="2026-11-02 23:30:00"), now=night) == "тихие часы"


def test_suppression_types_and_pump(db, settings):
    sc = _scanner(db)
    s = {**settings, "alert_types": ["down_pulse"]}
    assert sc.suppression_reason(1, s, _alert()) == "тип отключён"
    muted = _alert()
    muted.mute_push = True
    assert sc.suppression_reason(1, settings, muted) == "памп-фильтр"
