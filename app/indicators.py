"""Технические индикаторы на чистом Python (без pandas/numpy).

Все функции принимают списки и возвращают серии той же длины, где
недоопределённые точки равны ``None`` — так серии сразу годятся для графиков.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Optional, Sequence

Series = list[Optional[float]]


def sma(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= n:
            acc -= values[i - n]
        if i >= n - 1:
            out[i] = acc / n
    return out


def ema(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if not values:
        return out
    alpha = 2.0 / (n + 1)
    prev = float(values[0])
    for i, v in enumerate(values):
        prev = v if i == 0 else alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    out = []
    for i in range(len(highs)):
        hl = highs[i] - lows[i]
        if i == 0:
            out.append(hl)
        else:
            pc = closes[i - 1]
            out.append(max(hl, abs(highs[i] - pc), abs(lows[i] - pc)))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> Series:
    """ATR как скользящее среднее True Range. Пока точек меньше n — расширяющееся среднее."""
    tr = true_range(highs, lows, closes)
    out: Series = []
    acc = 0.0
    for i, v in enumerate(tr):
        acc += v
        if i >= n:
            acc -= tr[i - n]
        out.append(acc / min(i + 1, n))
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    """RSI Уайлдера."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[n] = value(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = value(avg_g, avg_l)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [(a - b) if a is not None and b is not None else None for a, b in zip(ef, es)]
    sig = ema([v or 0.0 for v in line], signal)
    hist = [(l - s) if l is not None and s is not None else None for l, s in zip(line, sig)]
    # Первые slow-1 точек недостоверны
    for i in range(min(slow - 1, len(closes))):
        line[i] = sig[i] = hist[i] = None
    return line, sig, hist


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[Series, Series, Series]:
    mid = sma(closes, n)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)
        upper[i], lower[i] = m + k * sd, m - k * sd
    return mid, upper, lower


def stochastic(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               n: int = 14, d: int = 3) -> tuple[Series, Series]:
    k: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        hh, ll = max(highs[i - n + 1:i + 1]), min(lows[i - n + 1:i + 1])
        k[i] = 50.0 if hh == ll else 100 * (closes[i] - ll) / (hh - ll)
    d_series: Series = [None] * len(closes)
    for i in range(len(closes)):
        window = [x for x in k[max(0, i - d + 1):i + 1] if x is not None]
        if len(window) == d:
            d_series[i] = sum(window) / d
    return k, d_series


def pivots(high: float, low: float, close: float) -> dict[str, float]:
    p = (high + low + close) / 3
    return {"pivot": p, "r1": 2 * p - low, "s1": 2 * p - high, "r2": p + (high - low), "s2": p - (high - low)}


def extremes(highs: Sequence[float], lows: Sequence[float], window: int = 10) -> tuple[list[float], list[float]]:
    """Локальные экстремумы (поддержки/сопротивления), как в исходном боте."""
    w = min(window, max(1, len(highs) // 3))
    sup, res = set(), set()
    for i in range(w, len(highs) - w):
        if lows[i] == min(lows[i - w:i + w]):
            sup.add(round(lows[i], 4))
        if highs[i] == max(highs[i - w:i + w]):
            res.add(round(highs[i], 4))
    return sorted(sup), sorted(res)


def linear_slope(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(values) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def weekly_trend(daily: Sequence[dict[str, Any]], weeks: int = 4) -> dict[str, Any]:
    """Недельный тренд по дневным свечам: наклон регрессии последних недельных закрытий."""
    if len(daily) < 7:
        return {"trend": "недостаточно данных", "strength": 0.0, "slope": 0.0, "weeks": 0}
    by_week: dict[tuple[int, int], float] = {}
    for c in daily:
        iso = _to_date(c["ts"]).isocalendar()
        by_week[(iso[0], iso[1])] = c["close"]  # последнее закрытие недели
    closes = [by_week[k] for k in sorted(by_week)][-weeks:]
    if len(closes) < 2:
        return {"trend": "недостаточно данных", "strength": 0.0, "slope": 0.0, "weeks": len(closes)}
    slope = linear_slope(closes)
    base = closes[0] or 1.0
    if slope > 0.001 * base:
        trend = "восходящий"
    elif slope < -0.001 * base:
        trend = "нисходящий"
    else:
        trend = "боковой"
    strength = 0.0 if trend == "боковой" else min(abs(slope) / base * 100, 5.0)
    return {"trend": trend, "strength": round(strength, 2), "slope": round(slope, 4), "weeks": len(closes)}


def last(series: Series) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return v
    return None
