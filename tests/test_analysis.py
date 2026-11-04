"""Снапшот P1, детектор плиты (п. 22), памп-фильтр (п. 15) и кейс ETLN (п. 16)."""

from app import analysis as an
from app import indicators as ind

from conftest import flat_minutes, pulse_day


# ------------------------------------------------------------------ окна
def test_aggregate_aligns_and_drops_incomplete_window():
    minutes = flat_minutes("2026-11-02 10:00:00", 37)  # 10:00..10:36
    wc = an.aggregate(minutes, 15)
    assert [c["ts"][11:16] for c in wc] == ["10:00", "10:15"]  # 10:30 ещё не закрыта
    assert wc[0]["n"] == 15 and wc[0]["volume"] == 15 * 1000
    wc_all = an.aggregate(minutes, 15, complete_only=False)
    assert len(wc_all) == 3 and wc_all[-1]["n"] == 7


def test_aggregate_keeps_window_closed_by_data_time():
    minutes = flat_minutes("2026-11-02 10:00:00", 30)  # последняя минутка 10:29 закрывает окно 10:15
    assert len(an.aggregate(minutes, 15)) == 2


# --------------------------------------------------------------- снапшот
def test_snapshot_detects_pulse(settings):
    snap = an.compute_snapshot("TEST", pulse_day(), settings)
    assert snap["amplitude_atr"] >= settings["amplitude_atr_min"]
    assert snap["volume_ratio"] == 4.0
    assert snap["level_break"] == 1
    assert snap["direction"] == 1
    assert snap["impulse_age_min"] == 15  # одна свеча окна 15 мин (п. 25)
    assert snap["window"] == 15


def test_snapshot_needs_data(settings):
    assert an.compute_snapshot("TEST", flat_minutes("2026-11-02 10:00:00", 20), settings) is None


# ---------------------------------------------------------------- плита
def _wc(seg):
    flat = [{"ts": f"2026-11-02 {10 + i // 4:02d}:{(i % 4) * 15:02d}:00", "open": 100.0, "close": 100.05,
             "high": 100.2, "low": 99.9, "volume": 1000.0} for i in range(21)]
    wc = flat + seg
    atr = ind.atr([c["high"] for c in wc], [c["low"] for c in wc], [c["close"] for c in wc])
    return wc, atr


def _seg(volume=3000.0, wick=0.0, start=100.05, step=1.0):
    out, p = [], start
    for _ in range(3):
        out.append({"ts": "2026-11-02 15:30:00", "open": p, "close": p + step, "high": p + step + wick,
                    "low": p - 0.02, "volume": volume})
        p += step
    return out


def test_plate_confident_4_of_4(settings):
    wc, atr = _wc(_seg())
    pl = an.detect_plate(wc, atr, settings)
    assert pl["score"] == 4 and pl["is_plate"] and pl["confident"]
    assert pl["direction"] == 1 and pl["pullback_pct"] < settings["plate_pullback_max_pct"]


def test_plate_3_of_4_without_volume(settings):
    wc, atr = _wc(_seg(volume=1000.0))
    pl = an.detect_plate(wc, atr, settings)
    assert pl["score"] == 3 and pl["is_plate"] and not pl["confident"]
    assert pl["volume"] is False


def test_stepped_trend_is_not_plate(settings):
    """Органический ступенчатый тренд: свечи вверх, объём есть, ход большой,
    но внутри серии откаты — без «нет коррекции» это не плита."""
    wc, atr = _wc(_seg(wick=0.6))
    pl = an.detect_plate(wc, atr, settings)
    assert pl["score"] == 3
    assert pl["no_correction"] is False
    assert pl["is_plate"] is False


def test_down_plate_direction(settings):
    wc, atr = _wc(_seg(step=-1.0))
    # для падения встречная тень — снизу; у _seg low = open-0.02, это мелочь
    pl = an.detect_plate(wc, atr, settings)
    assert pl["direction"] == -1 and pl["is_plate"]


# ----------------------------------------------------------- памп-фильтр
def test_overheat_two_of_four_labels_three_mutes(settings):
    snap = {"direction": 1, "price": 105.0, "change_hour_atr": 0.5, "volume_ratio": 2.0}
    two = an.overheat(snap, {"rsi": 50, "bb_upper": 104.0}, settings)
    assert two["score"] == 2 and two["label"] == "перегрето" and not two["mute_entry"]
    three = an.overheat(snap, {"rsi": 75, "bb_upper": 104.0}, settings)
    assert three["score"] == 3 and three["mute_entry"]
    four = an.overheat({**snap, "change_hour_atr": 4.0}, {"rsi": 75, "bb_upper": 104.0}, settings)
    assert four["score"] == 4


def test_overheat_mirror_for_down(settings):
    snap = {"direction": -1, "price": 95.0, "change_hour_atr": -4.0, "volume_ratio": 2.0}
    oh = an.overheat(snap, {"rsi": 25, "bb_lower": 96.0}, settings)
    assert oh["score"] == 4 and oh["label"] == "перепродано"


def test_overheat_one_flag_no_label(settings):
    oh = an.overheat({"direction": 1, "price": 100.0, "volume_ratio": 2.0}, {"rsi": 50, "bb_upper": 110}, settings)
    assert oh["score"] == 1 and oh["label"] is None


# -------------------------------------------------------- ETLN (п. 16)
ETLN_DAY = {
    # Значения со скрина в issue: RSI 59, стохастик 56, верхняя полоса 22.39.
    "rsi": 59.0, "stoch_k": 56.0, "bb_upper": 22.39, "bb_middle": 20.9, "bb_lower": 19.4,
    "sma20": 20.9, "sma50": 20.1, "macd_line": 0.35, "macd_signal": 0.2, "macd_hist": 0.15,
    "s1": 20.6, "r1": 23.0, "pivot": 21.0, "atr": 0.62, "atr_pct": 2.77,
    "weekly_trend": {"trend": "восходящий", "strength": 2.1},
}
ETLN_SIGNALS = ["📈 НЕДЕЛЬНЫЙ ТРЕНД ВВЕРХ (сила: 2.1%)", "📈 MACD БЫЧИЙ СИГНАЛ", "📈 ЦЕНА ВЫШЕ SMA20 > SMA50",
                "💥 СИЛЬНЫЙ ОБЪЕМ", "🚀 ПРИБЛИЖЕНИЕ К МАКСИМУМАМ"]


def test_etln_base_scoring_is_buy_but_filter_says_late(settings):
    v = an.make_verdict(22.36, 6.37, ETLN_DAY, settings, signals=ETLN_SIGNALS)
    # Исходный скоринг бота дал бы «ПОКУПАТЬ, 95%»
    assert v["base_verdict"] == "BUY" and v["strength"] == 95
    # После фильтра — «поздно, ждать откат»
    assert v["verdict"] == "LATE"
    assert v["label"] == "⏳ ПОЗДНО, ЖДАТЬ ОТКАТ"
    reasons = " | ".join(v["late_reasons"])
    assert "+6.37%" in reasons  # рост дня 6.37% ≈ 2.3×ATR
    assert "22.39" in reasons  # у верхней полосы Боллинджера
    # «уверенность» подписана как сила сигнала, не вероятность
    assert v["strength_note"] == "сила сигнала в текущих условиях, не вероятность"


def test_etln_stop_is_atr_based_not_17_94(settings):
    v = an.make_verdict(22.36, 6.37, ETLN_DAY, settings, signals=ETLN_SIGNALS)
    # Старый бот ставил стоп 17.94 (−20%); теперь стоп = цена − 1.5×ATR
    assert v["stop"] == round(22.36 - 1.5 * 0.62, 4)
    assert v["stop"] > 21
    assert v["take"] == round(22.36 + 3.0 * 0.62, 4)
    assert v["risk_reward"] == 2.0


def test_verdict_buy_without_late_reasons(settings):
    day = {**ETLN_DAY, "bb_upper": 25.0}
    v = an.make_verdict(22.36, 0.5, day, settings, signals=ETLN_SIGNALS)
    assert v["verdict"] == "BUY" and v["late_reasons"] == []


def test_verdict_late_from_snapshot_impulse_age_and_overheat(settings):
    day = {**ETLN_DAY, "bb_upper": 25.0}
    snap = {"direction": 1, "price": 22.36, "impulse_age_min": 60, "change_hour_pct": 1.0,
            "change_hour_atr": 4.0, "volume_ratio": 3.0}
    v = an.make_verdict(22.36, 0.5, day, settings, snap, {"rsi": 60, "bb_upper": 30}, ETLN_SIGNALS)
    assert v["verdict"] == "LATE"
    assert any("импульс длится 60 мин" in r for r in v["late_reasons"])
    assert any("перегрето" in r for r in v["late_reasons"])


def test_daily_indicators_full_set():
    daily = [{"ts": f"2026-{8 + i // 28:02d}-{1 + i % 28:02d}", "open": 100 + i * 0.1, "high": 101 + i * 0.1,
              "low": 99 + i * 0.1, "close": 100.5 + i * 0.1, "volume": 1000} for i in range(80)]
    d = an.daily_indicators(daily)
    for k in ("rsi", "sma20", "sma50", "macd_hist", "bb_upper", "bb_lower", "stoch_k", "pivot", "s1", "r1",
              "atr", "atr_pct", "weekly_trend"):
        assert d[k] is not None, k
    assert d["sma20"] > d["sma50"]
