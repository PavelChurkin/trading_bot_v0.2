"""Серии для графиков карточки тикера (п. 14, 28).

Свечи берутся из локальной БД (минутки, агрегированные в окно пользователя),
поэтому открытие карточки не тратит запросы к ISS. Индикаторы считаются по
этим свечам на лету; расчётные метрики сканера (amp/ATR, volume_ratio,
impulse_age, расстояние до порога) — из таблицы ``metrics``
(ticker, ts, metric, value).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from . import analysis as an
from . import indicators as ind
from .db import Database
from .timeutil import fmt_ts, now_msk

METRIC_CHARTS = {
    "amplitude_atr": ["amplitude_atr"],
    "volume_ratio": ["volume_ratio"],
    "impulse_age": ["impulse_age_min"],
    "threshold_distance": ["threshold_distance", "threshold_closeness"],
}


def _round(series: list[Optional[float]], nd: int = 4) -> list[Optional[float]]:
    return [None if v is None else round(v, nd) for v in series]


# Длинные периоды (14 дней / месяц, issue #3) рисуем часовыми свечами — иначе
# тысячи 15-минуток; график листается по горизонтали.
LONG_RANGE_DAYS = 14
LONG_RANGE_WINDOW = 60


def chart_window(window_min: int, days: int) -> int:
    return max(window_min, LONG_RANGE_WINDOW) if days >= LONG_RANGE_DAYS else window_min


def local_covers(db: Database, ticker: str, days: int) -> bool:
    """Покрывают ли локальные минутки период (с запасом в 2 дня на выходные)."""
    row = db.query_one("SELECT MIN(ts) AS ts FROM candles WHERE ticker = ?", (ticker,))
    if not row or not row["ts"]:
        return False
    return row["ts"] <= fmt_ts(now_msk() - timedelta(days=max(days - 2, 1)))


def ticker_series(db: Database, ticker: str, window_min: int, days: int = 7,
                  candles: Optional[list[dict[str, Any]]] = None, source: str = "local") -> dict[str, Any]:
    """Серии для карточки тикера. ``candles`` — готовые свечи (например,
    часовые из ISS для 14/30 дней, когда локальная история короче)."""
    since = fmt_ts(now_msk() - timedelta(days=days))
    window_min = chart_window(window_min, days)
    if candles is None:
        minute = db.candles_since(ticker, since)
        wc = an.aggregate(minute, window_min, complete_only=False)
    else:
        wc = [c for c in candles if c["ts"] >= since]
    closes = [c["close"] for c in wc]
    highs = [c["high"] for c in wc]
    lows = [c["low"] for c in wc]
    mid, up, lo = ind.bollinger(closes, 20, 2)
    k, d = ind.stochastic(highs, lows, closes)
    line, sig, hist = ind.macd(closes)
    out: dict[str, Any] = {
        "ticker": ticker,
        "window": window_min,
        "days": days,
        "source": source,
        "since": since,
        "candles": [[c["ts"], c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in wc],
        "indicators": {
            "bb_upper": _round(up), "bb_middle": _round(mid), "bb_lower": _round(lo),
            "atr": _round(ind.atr(highs, lows, closes), 6),
            "rsi": _round(ind.rsi(closes), 2),
            "macd_line": _round(line, 5), "macd_signal": _round(sig, 5), "macd_hist": _round(hist, 5),
            "stoch_k": _round(k, 2), "stoch_d": _round(d, 2),
        },
        "metrics": {},
    }
    for names in METRIC_CHARTS.values():
        for m in names:
            out["metrics"][m] = [[r["ts"], r["value"]] for r in db.metric_series(ticker, m, since)]
    return out


def metric_all_tickers(db: Database, metric: str, hours: float = 24, max_points: int = 400) -> dict[str, Any]:
    """Метрика по всем тикерам (например, расстояние до порога): последнее
    значение и короткая серия для каждого тикера."""
    since = fmt_ts(now_msk() - timedelta(hours=hours))
    rows = db.query("SELECT ticker, ts, value FROM metrics WHERE metric = ? AND ts >= ? ORDER BY ticker, ts",
                    (metric, since))
    series: dict[str, list[list[Any]]] = {}
    for r in rows:
        series.setdefault(r["ticker"], []).append([r["ts"], r["value"]])
    out = []
    for t, pts in series.items():
        step = max(1, len(pts) // max_points)
        out.append({"ticker": t, "last": pts[-1][1], "ts": pts[-1][0], "series": pts[::step]})
    return {"metric": metric, "since": since, "tickers": out}
