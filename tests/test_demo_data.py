"""Офлайн-демо (examples/demo_iss.py): синтетика детерминирована и даёт
задуманные сценарии на настоящем конвейере анализа — импульсы ловятся,
«почти» и спокойные бумаги молчат, ETLN получает «⏳ ПОЗДНО, ЖДАТЬ ОТКАТ»."""

from __future__ import annotations

import sys
from datetime import datetime

from app import alerts as al
from app import analysis as an
from app import settings_schema as ss
from app.scanner import candles_needed
from conftest import ROOT

sys.path.insert(0, str(ROOT / "examples"))
from demo_iss import DemoIss  # noqa: E402

NOW = datetime(2026, 11, 4, 18, 45)  # среда, сессия почти закрыта


def p0(iss: DemoIss, ticker: str, s: dict):
    rows = iss.minute[ticker]
    sec = {"daily_atr_pct": an.daily_indicators(iss.daily[ticker])["atr_pct"], "lot": 10}
    snap = an.compute_snapshot(ticker, rows[-candles_needed(int(s["candle_window_min"])):], s, sec)
    intraday = an.intraday_indicators(an.aggregate(rows, int(s["candle_window_min"])))
    day = an.daily_indicators(iss.daily[ticker], iss.last_day.isoformat())
    prev = iss._prev_close(ticker)
    verdict = an.make_verdict(snap["price"], (snap["price"] / prev - 1) * 100, day, s, snap, intraday)
    return snap, al.evaluate(snap, s, intraday, day, verdict), verdict


def test_demo_is_deterministic():
    a, b = DemoIss(now=NOW), DemoIss(now=NOW)
    assert a.minute["SBER"][-1] == b.minute["SBER"][-1]
    assert a.last_day.isoformat() == "2026-11-04" and len(a.days) == 6


def test_demo_scenarios_on_real_pipeline():
    iss, s = DemoIss(now=NOW), ss.defaults()
    kinds = {}
    for t in ("SBER", "GAZP", "MGNT", "VTBR", "YDEX", "LKOH"):
        _, ev, _ = p0(iss, t, s)
        kinds[t] = ev.alert.direction if ev.alert else None
    assert kinds["SBER"] == "up_pulse" and kinds["GAZP"] == "down_pulse"
    assert kinds["MGNT"] == "up_plate" and kinds["VTBR"] == "down_plate"
    assert kinds["YDEX"] is None and kinds["LKOH"] is None  # «почти» и спокойная


def test_demo_etln_is_late():
    _, _, verdict = p0(DemoIss(now=NOW), "ETLN", ss.defaults())
    assert verdict["base_verdict"] == "BUY" and verdict["verdict"] == "LATE"
    assert verdict["late_reasons"]
