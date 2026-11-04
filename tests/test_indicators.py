from app import indicators as ind


def test_sma_and_ema():
    assert ind.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ind.ema([10, 10, 10], 5)
    assert e == [10, 10, 10]


def test_rsi_extremes():
    up = list(range(1, 30))
    assert ind.last(ind.rsi(up)) == 100.0
    down = list(range(30, 1, -1))
    assert ind.last(ind.rsi(down)) == 0.0
    assert ind.rsi([1, 2, 3], 14) == [None, None, None]


def test_atr_true_range_with_gap():
    highs, lows, closes = [10, 12], [9, 11], [9.5, 11.5]
    tr = ind.true_range(highs, lows, closes)
    assert tr == [1, 2.5]  # гэп: |12 - 9.5|
    assert ind.atr(highs, lows, closes, 14) == [1.0, 1.75]


def test_macd_trims_unreliable_start():
    closes = [100 + i for i in range(40)]
    line, sig, hist = ind.macd(closes)
    assert line[24] is None and line[25] is not None
    assert hist[-1] is not None


def test_bollinger_constant_series_has_zero_width():
    mid, up, lo = ind.bollinger([5.0] * 25, 20, 2)
    assert mid[-1] == up[-1] == lo[-1] == 5.0
    assert up[18] is None


def test_stochastic_range():
    highs = [i + 1 for i in range(20)]
    lows = [i for i in range(20)]
    closes = [i + 1 for i in range(20)]
    k, d = ind.stochastic(highs, lows, closes)
    assert k[-1] == 100.0 and d[-1] == 100.0


def test_pivots_formula():
    pv = ind.pivots(110, 90, 100)
    assert pv == {"pivot": 100, "r1": 110, "s1": 90, "r2": 120, "s2": 80}


def test_weekly_trend_up():
    daily = [{"ts": f"2026-10-{d:02d}", "close": 100 + d} for d in range(1, 29)]
    wt = ind.weekly_trend(daily)
    assert wt["trend"] == "восходящий" and wt["strength"] > 0
