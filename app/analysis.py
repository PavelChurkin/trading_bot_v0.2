"""Расчёт метрик, детекторы и вердикт.

* :func:`aggregate` — минутные свечи ISS -> свечи окна (по умолчанию 15 мин).
* :func:`compute_snapshot` — дешёвые метрики P1 для всех тикеров: амплитуда/ATR,
  объём к среднему, пробой уровня, изменение за час, возраст импульса,
  детектор плиты (п. 7, 13, 22, 25).
* :func:`intraday_indicators`, :func:`daily_indicators` — индикаторы, которые
  считаются только на P0 (п. 27).
* :func:`overheat` — памп-фильтр как связка из четырёх признаков (п. 15).
* :func:`make_verdict` — перенос логики ``generate_trading_ranges`` из старого
  бота + фильтр «поздно, ждать откат» (п. 16). «Уверенность» заменена на
  «силу сигнала» — это не вероятность.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional, Sequence

from . import indicators as ind
from .timeutil import fmt_ts, parse_ts


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------------------- свечи
def aggregate(candles: Sequence[dict[str, Any]], window_min: int, complete_only: bool = True) -> list[dict[str, Any]]:
    """Собирает минутные свечи в свечи окна ``window_min``.

    Границы окон выравниваются по часам (10:00, 10:15, ...). Если
    ``complete_only``, последняя незакрытая свеча окна отбрасывается.
    Закрытость определяется по времени данных (последняя минутка + 1 мин),
    а не по часам компьютера — ISS может отдавать данные с задержкой.
    """
    if not candles:
        return []
    window_min = max(1, int(window_min))
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    cur_key = None
    for c in candles:
        dt = parse_ts(c["ts"])
        minute_of_day = dt.hour * 60 + dt.minute
        start_min = (minute_of_day // window_min) * window_min
        key = (dt.date(), start_min)
        if key != cur_key:
            if cur is not None:
                out.append(cur)
            start = dt.replace(hour=start_min // 60, minute=start_min % 60, second=0)
            cur = {"ts": fmt_ts(start), "end": fmt_ts(start + timedelta(minutes=window_min)),
                   "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"],
                   "volume": c.get("volume") or 0, "value": c.get("value") or 0, "n": 1}
            cur_key = key
        else:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["volume"] += c.get("volume") or 0
            cur["value"] += c.get("value") or 0
            cur["n"] += 1
    if cur is not None:
        out.append(cur)
    if complete_only and out:
        data_time = parse_ts(candles[-1]["ts"]) + timedelta(minutes=1)
        if parse_ts(out[-1]["end"]) > data_time:
            out.pop()
    return out


# ------------------------------------------------------------------- снапшот P1
def detect_plate(wc: Sequence[dict[str, Any]], atr_series: Sequence[float], s: dict[str, Any]) -> dict[str, Any]:
    """Детектор плиты (п. 22) по последним N свечам окна.

    Четыре признака (каждый можно отключить в настройках группы «Плита»):

    1. ``series`` — N свечей подряд в одну сторону или одиночный рывок
       телом ≥ ``plate_jerk_atr`` × ATR;
    2. ``no_correction`` — встречные движения (откаты close-to-close плюс
       встречные тени) составляют < ``plate_pullback_max_pct`` % пройденного
       пути. Если признак учитывается — он обязательный (отличает плиту от
       органического ступенчатого тренда);
    3. ``volume`` — объём каждой свечи серии ≥ ``plate_volume_mult`` × среднего;
    4. ``move`` — ход серии ≥ ``plate_move_atr_k`` × ATR.

    «Плита» — выполнено ≥ ``plate_min_score`` из учитываемых признаков,
    «уверенно» — выполнены все учитываемые.
    """
    n = int(s["plate_series_n"])
    use = {
        "series": bool(s.get("plate_use_series", True)),
        "no_correction": bool(s.get("plate_use_no_correction", True)),
        "volume": bool(s.get("plate_use_volume", True)),
        "move": bool(s.get("plate_use_move", True)),
    }
    enabled = sum(use.values())
    need = min(int(s.get("plate_min_score", 3)), enabled)
    empty = {"score": 0, "is_plate": False, "confident": False, "direction": 0, "move_pct": 0.0,
             "move_atr": 0.0, "pullback_pct": 100.0, "series": False, "no_correction": False,
             "volume": False, "move": False, "n": n, "enabled": enabled, "need": need}
    if len(wc) < n + 1 or enabled == 0:
        return empty
    seg = wc[-n:]
    before = wc[:-n]
    atr_ref = atr_series[len(wc) - n - 1] or 0.0
    move = seg[-1]["close"] - seg[0]["open"]
    direction = _sign(move)
    if direction == 0 or atr_ref <= 0:
        return {**empty, "direction": direction}
    signs = [_sign(c["close"] - c["open"]) for c in seg]
    series = all(x == direction for x in signs)
    jerk = any(abs(c["close"] - c["open"]) >= s["plate_jerk_atr"] * atr_ref for c in seg)
    # Откаты: встречные движения close-to-close + встречные тени внутри свечей
    pullback = 0.0
    prev_close = seg[0]["open"]
    for c in seg:
        adverse = -direction * (c["close"] - prev_close)
        pullback += max(adverse, 0.0)
        pullback += (c["high"] - c["close"]) if direction > 0 else (c["close"] - c["low"])
        prev_close = c["close"]
    # Доля встречных движений во всём пройденном пути: 0..100 %. Раньше делили
    # на чистый ход серии, и при ходе ≈ 0 получались 500 %…11800 % (issue #3).
    path = abs(move) + pullback
    pullback_pct = pullback / path * 100 if path > 0 else 0.0
    vol_window = before[-int(s["volume_avg_window"]):]
    vol_avg = sum(c["volume"] for c in vol_window) / len(vol_window) if vol_window else 0.0
    volume_ok = vol_avg > 0 and all(c["volume"] >= s["plate_volume_mult"] * vol_avg for c in seg)
    move_atr = abs(move) / atr_ref
    flags = {
        "series": bool(series or jerk),
        "no_correction": pullback_pct < s["plate_pullback_max_pct"],
        "volume": bool(volume_ok),
        "move": move_atr >= s["plate_move_atr_k"],
    }
    score = sum(1 for k, v in flags.items() if v and use[k])
    mandatory_ok = flags["no_correction"] or not use["no_correction"]
    is_plate = mandatory_ok and score >= need
    return {
        **flags, "score": score, "is_plate": is_plate, "confident": is_plate and score == enabled,
        "direction": direction, "jerk": bool(jerk), "move_pct": _r(move / seg[0]["open"] * 100, 3),
        "move_atr": _r(move_atr, 3), "pullback_pct": _r(pullback_pct, 2), "n": n,
        "enabled": enabled, "need": need, "use": use,
    }


def approach_metrics(wc: Sequence[dict[str, Any]], atr: float, level_high: Optional[float],
                     level_low: Optional[float], k: int = 3) -> dict[str, Any]:
    """Гипотеза issue #3 (п. 21): интенсивность движения к уровню.

    ``approach_speed_atr`` — средняя скорость цены за последние ``k`` окон в
    ATR за окно, со знаком «к ближайшему уровню по ходу движения» (вверх —
    к сопротивлению, вниз — к поддержке). ``approach_intensity`` — скорость ×
    отношение объёма последних окон к предыдущим (ускорение на объёме).

    ``pump_peak_forecast`` — экспериментальная оценка пика: если интенсивность
    растёт, импульс по инерции проходит ещё ~скорость × (intensity/скорость)
    окон; ограничиваем сверху расстоянием до уровня + 1 ATR (пробой) — пампы
    часто «выдыхаются» у сопротивления либо через ATR после пробоя.
    """
    out = {"approach_speed_atr": 0.0, "approach_intensity": 0.0, "approach_level": None,
           "approach_distance_atr": None, "pump_peak_forecast": None}
    if len(wc) < k + 1 or atr <= 0:
        return out
    seg = wc[-k:]
    prev = wc[-2 * k:-k] or wc[:1]
    speed = (seg[-1]["close"] - wc[-k - 1]["close"]) / atr / k
    vol_now = sum(c["volume"] for c in seg) / len(seg)
    vol_prev = sum(c["volume"] for c in prev) / len(prev)
    vol_k = vol_now / vol_prev if vol_prev > 0 else 1.0
    price = seg[-1]["close"]
    if speed >= 0:
        level = level_high
        dist = (level - price) / atr if level is not None else None
    else:
        level = level_low
        dist = (price - level) / atr if level is not None else None
    intensity = abs(speed) * vol_k
    forecast = None
    if speed > 0:
        # Инерция: чем выше интенсивность — тем дальше «доедет» цена; потолок —
        # уровень сопротивления + 1 ATR (если уже выше уровня — цена + 1 ATR).
        run = abs(speed) * max(1.0, min(vol_k, 5.0)) * atr
        ceiling = (max(level, price) if level is not None else price) + atr
        forecast = min(price + run, ceiling)
    out.update({
        "approach_speed_atr": _r(speed, 3), "approach_intensity": _r(intensity, 3),
        "approach_level": level, "approach_distance_atr": _r(dist, 3) if dist is not None else None,
        "pump_peak_forecast": _r(forecast, 4) if forecast is not None else None,
    })
    return out


def compute_snapshot(ticker: str, candles: Sequence[dict[str, Any]], s: dict[str, Any],
                     security: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Дешёвые метрики по одному тикеру (без «тяжёлых» индикаторов)."""
    window = int(s["candle_window_min"])
    wc = aggregate(candles, window)
    if len(wc) < 3:
        return None
    highs = [c["high"] for c in wc]
    lows = [c["low"] for c in wc]
    closes = [c["close"] for c in wc]
    atr_series = ind.atr(highs, lows, closes, 14)
    last = wc[-1]
    atr_ref = atr_series[-2] or atr_series[-1]  # волатильность ДО текущей свечи
    if not atr_ref or atr_ref <= 0:
        return None
    price = last["close"]
    rng = last["high"] - last["low"]
    amplitude_pct = rng / last["low"] * 100 if last["low"] else 0.0
    amplitude_atr = rng / atr_ref

    prev = wc[:-1]
    vol_window = prev[-int(s["volume_avg_window"]):]
    vol_avg = sum(c["volume"] for c in vol_window) / len(vol_window) if vol_window else 0.0
    volume_ratio = last["volume"] / vol_avg if vol_avg > 0 else 0.0

    lvl_window = prev[-int(s["level_window"]):]
    level_high = max(c["high"] for c in lvl_window)
    level_low = min(c["low"] for c in lvl_window)
    if price > level_high:
        level_break = 1
    elif price < level_low:
        level_break = -1
    else:
        level_break = 0
    level_distance_atr = 0.0 if level_break else min(level_high - price, price - level_low) / atr_ref

    # Изменение за последний час — по минуткам
    last_dt = parse_ts(candles[-1]["ts"])
    hour_ago = fmt_ts(last_dt - timedelta(minutes=60))
    base_1h = None
    for c in reversed(candles):
        if c["ts"] <= hour_ago:
            base_1h = c["close"]
            break
    if base_1h is None:
        base_1h = candles[0]["open"]
    last_1m_close = candles[-1]["close"]
    change_hour_pct = (last_1m_close - base_1h) / base_1h * 100 if base_1h else 0.0
    change_hour_atr = (last_1m_close - base_1h) / atr_ref

    # Направление и возраст импульса (п. 25)
    direction = _sign(last["close"] - last["open"]) or level_break or _sign(change_hour_pct)
    impulse_candles = 0
    for c in reversed(wc):
        if direction and _sign(c["close"] - c["open"]) == direction:
            impulse_candles += 1
        else:
            break
    impulse_start = wc[-impulse_candles]["open"] if impulse_candles else last["open"]
    impulse_move_pct = (price - impulse_start) / impulse_start * 100 if impulse_start else 0.0

    plate = detect_plate(wc, atr_series, s)
    approach = approach_metrics(wc, atr_ref, level_high, level_low)
    sec = security or {}
    return {
        "ticker": ticker,
        "name": sec.get("name"),
        "ts": last["ts"],
        "data_ts": candles[-1]["ts"],
        "window": window,
        "price": price,
        "open": last["open"], "high": last["high"], "low": last["low"], "close": last["close"],
        "volume": last["volume"],
        "volume_avg": _r(vol_avg, 2),
        "volume_ratio": _r(volume_ratio, 3),
        "atr": _r(atr_ref, 6),
        "atr_pct": _r(atr_ref / price * 100 if price else 0.0, 4),
        "amplitude_pct": _r(amplitude_pct, 4),
        "amplitude_atr": _r(amplitude_atr, 3),
        "level_high": level_high, "level_low": level_low,
        "level_break": level_break,
        "level_distance_atr": _r(level_distance_atr, 3),
        "change_hour_pct": _r(change_hour_pct, 3),
        "change_hour_atr": _r(change_hour_atr, 3),
        "direction": direction,
        "impulse_candles": impulse_candles,
        "impulse_age_min": impulse_candles * window,
        "impulse_move_pct": _r(impulse_move_pct, 3),
        "pullback_pct": plate["pullback_pct"],
        "plate": plate,
        "plate_score": plate["score"],
        **approach,
        "day_change_pct": sec.get("day_change_pct"),
        "volume_today": sec.get("volume_today"),
        "value_today": sec.get("value_today"),
        "candles": len(wc),
    }


# ------------------------------------------------------------- индикаторы P0
def intraday_indicators(wc: Sequence[dict[str, Any]]) -> dict[str, Any]:
    closes = [c["close"] for c in wc]
    highs = [c["high"] for c in wc]
    lows = [c["low"] for c in wc]
    mid, up, lo = ind.bollinger(closes, 20, 2)
    k, d = ind.stochastic(highs, lows, closes)
    line, sig, hist = ind.macd(closes)
    return {
        "rsi": _r(ind.last(ind.rsi(closes)), 2),
        "bb_upper": _r(ind.last(up)), "bb_middle": _r(ind.last(mid)), "bb_lower": _r(ind.last(lo)),
        "stoch_k": _r(ind.last(k), 2), "stoch_d": _r(ind.last(d), 2),
        "macd_line": _r(ind.last(line), 5), "macd_signal": _r(ind.last(sig), 5), "macd_hist": _r(ind.last(hist), 5),
        "atr": _r(ind.last(ind.atr(highs, lows, closes)), 6),
    }


def daily_indicators(daily: Sequence[dict[str, Any]], today: Optional[str] = None) -> dict[str, Any]:
    """Индикаторы по дневным свечам (как в исходном боте): RSI, SMA20/50, MACD,
    Боллинджер, стохастик, пивоты S1/S2/R1/R2, ATR, недельный тренд."""
    if not daily:
        return {}
    closes = [c["close"] for c in daily]
    highs = [c["high"] for c in daily]
    lows = [c["low"] for c in daily]
    mid, up, lo = ind.bollinger(closes, 20, 2)
    k, d = ind.stochastic(highs, lows, closes)
    line, sig, hist = ind.macd(closes)
    atr_series = ind.atr(highs, lows, closes, 14)
    # Пивоты — по последнему завершённому дню
    ref = daily[-1]
    if today and str(ref["ts"])[:10] == today and len(daily) >= 2:
        ref = daily[-2]
    pv = ind.pivots(ref["high"], ref["low"], ref["close"])
    sup, res = ind.extremes(highs, lows)
    sma20, sma50 = ind.last(ind.sma(closes, 20)), ind.last(ind.sma(closes, 50))
    vols = [c.get("volume") or 0 for c in daily]
    atr_val = ind.last(atr_series)
    price = closes[-1]
    return {
        "rsi": _r(ind.last(ind.rsi(closes)), 2),
        "sma20": _r(sma20 if sma20 is not None else sum(closes) / len(closes)),
        "sma50": _r(sma50 if sma50 is not None else sum(closes) / len(closes)),
        "macd_line": _r(ind.last(line), 5), "macd_signal": _r(ind.last(sig), 5), "macd_hist": _r(ind.last(hist), 5),
        "bb_upper": _r(ind.last(up)), "bb_middle": _r(ind.last(mid)), "bb_lower": _r(ind.last(lo)),
        "stoch_k": _r(ind.last(k), 2), "stoch_d": _r(ind.last(d), 2),
        "pivot": _r(pv["pivot"]), "r1": _r(pv["r1"]), "s1": _r(pv["s1"]), "r2": _r(pv["r2"]), "s2": _r(pv["s2"]),
        "supports": sup[-3:], "resistances": res[-3:],
        "atr": _r(atr_val, 6), "atr_pct": _r(atr_val / price * 100 if price and atr_val else None, 4),
        "volume_avg20": _r(sum(vols[-20:]) / len(vols[-20:]), 2),
        "weekly_trend": ind.weekly_trend(daily),
        "days": len(daily),
    }


# --------------------------------------------------------------- памп-фильтр
def overheat(snapshot: dict[str, Any], intraday: dict[str, Any], s: dict[str, Any]) -> dict[str, Any]:
    """Памп-фильтр как связка (п. 15). Для движения вниз — зеркальная связка.

    up:   рост за час > k×ATR, RSI > 70, цена выше верхней полосы, объём аномальный
    down: падение за час > k×ATR, RSI < 30, цена ниже нижней полосы, объём аномальный
    2 из 4 — метка «перегрето»/«перепродано», 3+ — глушить пуш на вход.
    """
    direction = snapshot.get("direction") or 0
    price = snapshot["price"]
    rsi = intraday.get("rsi")
    if direction >= 0:
        flags = {
            "hour_move": (snapshot.get("change_hour_atr") or 0) > s["pump_hour_atr_k"],
            "rsi": rsi is not None and rsi > s["rsi_overbought"],
            "bollinger": intraday.get("bb_upper") is not None and price > intraday["bb_upper"],
            "volume": (snapshot.get("volume_ratio") or 0) >= s["volume_mult"],
        }
        label = "перегрето"
    else:
        flags = {
            "hour_move": (snapshot.get("change_hour_atr") or 0) < -s["pump_hour_atr_k"],
            "rsi": rsi is not None and rsi < s["rsi_oversold"],
            "bollinger": intraday.get("bb_lower") is not None and price < intraday["bb_lower"],
            "volume": (snapshot.get("volume_ratio") or 0) >= s["volume_mult"],
        }
        label = "перепродано"
    score = sum(flags.values())
    return {"flags": flags, "score": score, "label": label if score >= 2 else None, "mute_entry": score >= 3}


# ------------------------------------------------------------------- вердикт
VERDICT_LABELS = {
    "BUY": "🟢 ПОКУПАТЬ",
    "SELL": "🔴 ПРОДАВАТЬ",
    "HOLD": "🟡 ДЕРЖАТЬ",
    "LATE": "⏳ ПОЗДНО, ЖДАТЬ ОТКАТ",
}


def text_signals(price: float, day: dict[str, Any], volume_today: Optional[float],
                 orderbook: Optional[dict[str, Any]], daily: Sequence[dict[str, Any]] | None = None) -> list[str]:
    """Текстовые сигналы из исходного бота (analyze_intentionality)."""
    out: list[str] = []
    avg = day.get("volume_avg20")
    if avg and volume_today:
        if volume_today > avg * 2:
            out.append("💥 СИЛЬНЫЙ ОБЪЕМ")
        elif volume_today > avg * 1.5:
            out.append("📈 ПОВЫШЕННЫЙ ОБЪЕМ")
    if orderbook and (orderbook.get("bids") or orderbook.get("asks")):
        bid = sum(b["quantity"] for b in orderbook.get("bids", []))
        ask = sum(a["quantity"] for a in orderbook.get("asks", []))
        if bid + ask > 0:
            imb = (bid - ask) / (bid + ask)
            if imb > 0.3:
                out.append("🟢 ПРЕОБЛАДАЮТ ПОКУПКИ")
            elif imb < -0.3:
                out.append("🔴 ПРЕОБЛАДАЮТ ПРОДАЖИ")
        if orderbook.get("spread_pct") is not None:
            if orderbook["spread_pct"] < 0.1:
                out.append("💰 УЗКИЙ СПРЕД (высокая ликвидность)")
            elif orderbook["spread_pct"] > 1:
                out.append("📏 ШИРОКИЙ СПРЕД (низкая ликвидность)")
    wt = day.get("weekly_trend") or {}
    if wt.get("trend") == "восходящий":
        out.append(f"📈 НЕДЕЛЬНЫЙ ТРЕНД ВВЕРХ (сила: {wt.get('strength')}%)")
    elif wt.get("trend") == "нисходящий":
        out.append(f"📉 НЕДЕЛЬНЫЙ ТРЕНД ВНИЗ (сила: {wt.get('strength')}%)")
    if daily and len(daily) > 5:
        hi = max(c["high"] for c in daily[-5:])
        lo = min(c["low"] for c in daily[-5:])
        if price >= hi * 0.995:
            out.append("🚀 ПРИБЛИЖЕНИЕ К МАКСИМУМАМ")
        elif price <= lo * 1.005:
            out.append("📉 ПРИБЛИЖЕНИЕ К МИНИМУМАМ")
    rsi = day.get("rsi")
    if rsi is not None:
        if rsi > 70:
            out.append(f"⚠️ RSI ПЕРЕКУПЛЕННОСТЬ ({rsi:.1f})")
        elif rsi < 30:
            out.append(f"⚠️ RSI ПЕРЕПРОДАННОСТЬ ({rsi:.1f})")
        elif rsi > 60:
            out.append(f"📈 RSI ВЫШЕ 60 ({rsi:.1f})")
        elif rsi < 40:
            out.append(f"📉 RSI НИЖЕ 40 ({rsi:.1f})")
    hist = day.get("macd_hist")
    if hist is not None and day.get("macd_line") is not None and day.get("macd_signal") is not None:
        if day["macd_line"] > day["macd_signal"] and hist > 0:
            out.append("📈 MACD БЫЧИЙ СИГНАЛ")
        elif day["macd_line"] < day["macd_signal"] and hist < 0:
            out.append("📉 MACD МЕДВЕЖИЙ СИГНАЛ")
    if day.get("bb_upper") is not None and day.get("bb_lower") is not None:
        if price >= day["bb_upper"]:
            out.append("⚠️ ЦЕНА У ВЕРХНЕЙ ГРАНИЦЫ BOLLINGER")
        elif price <= day["bb_lower"]:
            out.append("⚠️ ЦЕНА У НИЖНЕЙ ГРАНИЦЫ BOLLINGER")
    k = day.get("stoch_k")
    if k is not None:
        if k > 80:
            out.append(f"⚠️ STOCH ПЕРЕКУПЛЕННОСТЬ ({k:.1f})")
        elif k < 20:
            out.append(f"⚠️ STOCH ПЕРЕПРОДАННОСТЬ ({k:.1f})")
        elif k > 70:
            out.append(f"📈 STOCH ВЫШЕ 70 ({k:.1f})")
        elif k < 30:
            out.append(f"📉 STOCH НИЖЕ 30 ({k:.1f})")
    s20, s50 = day.get("sma20"), day.get("sma50")
    if s20 and s50:
        if price > s20 > s50:
            out.append("📈 ЦЕНА ВЫШЕ SMA20 > SMA50")
        elif price > s20 and s20 < s50:
            out.append("⚠️ ЦЕНА ВЫШЕ SMA20, НО SMA20 < SMA50")
        elif price < s20 < s50:
            out.append("📉 ЦЕНА НИЖЕ SMA20 < SMA50")
    return out


def make_verdict(price: float, day_change_pct: Optional[float], day: dict[str, Any], s: dict[str, Any],
                 snapshot: Optional[dict[str, Any]] = None, intraday: Optional[dict[str, Any]] = None,
                 signals: Sequence[str] = ()) -> dict[str, Any]:
    """Скоринг из исходного бота + фильтр «поздно» (п. 15, 16, 25).

    Возвращает ``strength`` 0..100 — силу сигнала в текущих условиях.
    Это НЕ вероятность успеха.
    """
    atr = day.get("atr") or price * 0.02
    s1, r1 = day.get("s1") or price, day.get("r1") or price
    buy_lo, buy_hi = max(0.0, s1 - atr * 1.5), s1
    sell_lo, sell_hi = r1, r1 + atr * 1.5

    buy = sell = 0
    if price <= buy_hi:
        buy += 30
    elif price >= sell_lo:
        sell += 30
    trend = (day.get("weekly_trend") or {}).get("trend")
    if trend == "восходящий":
        buy += 20
    elif trend == "нисходящий":
        sell += 20
    rsi = day.get("rsi") if day.get("rsi") is not None else 50
    if rsi < s["rsi_oversold"]:
        buy += 15
    elif rsi > s["rsi_overbought"]:
        sell += 15
    hist = day.get("macd_hist") or 0
    if hist > 0:
        buy += 10
    elif hist < 0:
        sell += 10
    k = day.get("stoch_k") if day.get("stoch_k") is not None else 50
    if k < 20:
        buy += 10
    elif k > 80:
        sell += 10
    s20, s50 = day.get("sma20") or price, day.get("sma50") or price
    if price > s20 > s50:
        buy += 15
    elif price < s20 < s50:
        sell += 15
    bb_up, bb_lo = day.get("bb_upper") or price * 1.05, day.get("bb_lower") or price * 0.95
    if price <= bb_lo:
        buy += 10
    elif price >= bb_up:
        sell += 10
    buy += 5 * sum(1 for x in signals if any(e in x for e in ("📈", "🟢", "💥", "🚀", "📊")))
    sell += 5 * sum(1 for x in signals if any(e in x for e in ("📉", "🔴", "⚠️")))

    if buy > sell + 20:
        base = "BUY"
        strength = min(50 + (buy - sell), 95)
    elif sell > buy + 20:
        base = "SELL"
        strength = min(50 + (sell - buy), 95)
    else:
        base = "HOLD"
        strength = 50

    # --- Фильтр «поздно, ждать откат» для покупок
    late_reasons: list[str] = []
    atr_pct = day.get("atr_pct") or (atr / price * 100 if price else None)
    if day_change_pct is not None and atr_pct:
        day_change_atr = day_change_pct / atr_pct
        if day_change_atr >= s["late_day_atr"]:
            late_reasons.append(f"рост дня {day_change_pct:+.2f}% = {day_change_atr:.1f}×ATR")
    if day.get("bb_upper") is not None and price >= day["bb_upper"] - s["late_bb_atr"] * atr:
        late_reasons.append(f"цена {price:g} у верхней полосы Боллинджера {day['bb_upper']:g}")
    oh = None
    if snapshot is not None:
        if (snapshot.get("direction") or 0) > 0 and snapshot.get("impulse_age_min", 0) > s["impulse_age_max_min"]:
            late_reasons.append(f"импульс длится {snapshot['impulse_age_min']} мин")
        x = s.get("pump_hour_pct_exclude") or 0
        if x > 0 and (snapshot.get("change_hour_pct") or 0) > x:
            late_reasons.append(f"уже +{snapshot['change_hour_pct']:.2f}% за час (> {x}%)")
        if intraday:
            oh = overheat(snapshot, intraday, s)
            if (snapshot.get("direction") or 0) > 0 and oh["score"] >= 2:
                late_reasons.append(f"перегрето ({oh['score']}/4)")
    verdict = base
    if base == "BUY" and late_reasons:
        verdict = "LATE"

    if verdict in ("BUY", "LATE"):
        stop, take = price - s["stop_atr_mult"] * atr, price + s["take_atr_mult"] * atr
    elif verdict == "SELL":
        stop, take = price + s["stop_atr_mult"] * atr, price - s["take_atr_mult"] * atr
    else:
        stop = take = None
    rr = abs(take - price) / abs(stop - price) if stop is not None and stop != price else None
    return {
        "verdict": verdict,
        "base_verdict": base,
        "label": VERDICT_LABELS[verdict],
        "strength": int(round(strength)),
        "strength_note": "сила сигнала в текущих условиях, не вероятность",
        "buy_score": buy, "sell_score": sell,
        "late_reasons": late_reasons,
        "overheat": oh,
        "zones": {"buy": [_r(buy_lo, 4), _r(buy_hi, 4)], "sell": [_r(sell_lo, 4), _r(sell_hi, 4)]},
        "stop": _r(stop, 4), "take": _r(take, 4), "risk_reward": _r(rr, 2),
    }



# ------------------------------------------------- пин-бары (issue #5, задача 5)
BULLISH_SWEEP, BEARISH_SWEEP = "BULLISH_SWEEP", "BEARISH_SWEEP"
PINBAR_LABELS = {BULLISH_SWEEP: "бычий пин-бар (хвост вниз)", BEARISH_SWEEP: "медвежий пин-бар (хвост вверх)"}


def detect_pinbar(candles: Sequence[dict[str, Any]], shadow_ratio: float, max_body_ratio: float,
                  volume_z: Optional[float] = None, volume_window: int = 20) -> list[dict[str, Any]]:
    """Свечи-«стрекозы» (liquidity sweep): резкий вынос цены с возвратом.

    Для каждой закрытой свечи: ``body = |close − open|``, ``range = high − low``,
    тени ``lower = min(open, close) − low``, ``upper = high − max(open, close)``.

    * BULLISH_SWEEP (хвост вниз, цену откупили): ``lower ≥ body × shadow_ratio``,
      ``body ≤ range × max_body_ratio`` и нижняя тень длиннее верхней;
      ключевой уровень — ``low``;
    * BEARISH_SWEEP (хвост вверх, цену слили) — зеркально, уровень — ``high``.

    ``volume_z`` (> 0) — подтверждение объёмом: z-score объёма свечи
    относительно ``volume_window`` предыдущих ≥ ``volume_z``.
    Возвращает ``[{"ts", "type", "key_level", "shadow_ratio", "volume_z"}]``."""
    out: list[dict[str, Any]] = []
    for i, c in enumerate(candles):
        try:
            o, h, lo, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        except (KeyError, TypeError, ValueError):
            continue
        full = h - lo
        if full <= 0 or lo <= 0:
            continue
        body = abs(cl - o)
        lower, upper = min(o, cl) - lo, h - max(o, cl)
        if body > full * max_body_ratio:
            continue
        if lower > upper and lower > 0 and lower >= body * shadow_ratio:
            kind, level, shadow = BULLISH_SWEEP, lo, lower
        elif upper > lower and upper > 0 and upper >= body * shadow_ratio:
            kind, level, shadow = BEARISH_SWEEP, h, upper
        else:
            continue
        z = None
        prev = [float(x.get("volume") or 0) for x in candles[max(0, i - volume_window):i]]
        if len(prev) >= 5:
            mean = sum(prev) / len(prev)
            sd = (sum((v - mean) ** 2 for v in prev) / len(prev)) ** 0.5
            vol = float(c.get("volume") or 0)
            z = (vol - mean) / sd if sd > 0 else (0.0 if vol <= mean else float("inf"))
        if volume_z and volume_z > 0 and (z is None or z < volume_z):
            continue
        out.append({"ts": c["ts"], "type": kind, "key_level": level,
                    "shadow_ratio": round(shadow / body, 2) if body > 0 else None,
                    "volume_z": None if z is None or z == float("inf") else round(z, 2)})
    return out


def pinbar_touched(kind: str, level: float, c: dict[str, Any]) -> bool:
    """Свеча коснулась ключевого уровня пин-бара."""
    return c["low"] <= level if kind == BULLISH_SWEEP else c["high"] >= level


# ------------------------------------- статистика рынка (issue #5, задача 3)
def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def _returns(closes: dict[str, float], dates: list[str]) -> list[float]:
    return [closes[b] / closes[a] - 1 for a, b in zip(dates, dates[1:])]


def market_stats(hist: dict[str, list[list[Any]]], imoex: list[list[Any]], days: int = 30, week: int = 5,
                 min_points: int = 10, high: float = 0.6, low: float = 0.3) -> dict[str, Any]:
    """Общая картина рынка и связь бумаг с индексом.

    ``hist`` — ``{тикер: [[дата, high, low, close], ...]}`` (дневные свечи),
    ``imoex`` — ``[[дата, close], ...]``.

    * среднее внутридневное колебание: (high − low) / low × 100 по дням за
      ``days``, среднее по бумаге, затем по рынку;
    * среднее недельное колебание: (max high − min low) / min low × 100 за
      последние ``week`` торговых дней;
    * корреляция Пирсона дневных доходностей бумаги и IMOEX за ``days`` дней
      (по общим датам, минимум ``min_points`` доходностей); список по убыванию,
      «высокая» ≥ ``high``, «низкая» < ``low``."""
    idx = {str(d): float(c) for d, c in imoex if c}
    rows: list[dict[str, Any]] = []
    for t, h in hist.items():
        h = [x for x in h if x[1] and x[2] and x[3] and x[2] > 0][-days:]
        if not h:
            continue
        intraday = sum((x[1] - x[2]) / x[2] * 100 for x in h) / len(h)
        wk = h[-week:]
        weekly = (max(x[1] for x in wk) - min(x[2] for x in wk)) / min(x[2] for x in wk) * 100
        row: dict[str, Any] = {"ticker": t, "intraday_pct": round(intraday, 3), "weekly_pct": round(weekly, 3),
                               "corr": None, "n": 0}
        closes = {str(x[0]): float(x[3]) for x in h}
        common = sorted(d for d in closes if d in idx)[-(days + 1):]
        if len(common) - 1 >= min_points:
            r = pearson(_returns(closes, common), _returns(idx, common))
            row.update({"corr": None if r is None else round(r, 3), "n": len(common) - 1})
        rows.append(row)
    corr = sorted((r for r in rows if r["corr"] is not None), key=lambda r: -r["corr"])

    def avg(k: str) -> Optional[float]:
        return round(sum(r[k] for r in rows) / len(rows), 3) if rows else None

    return {"days": days, "week": week, "tickers": len(rows), "avg_intraday_pct": avg("intraday_pct"),
            "avg_weekly_pct": avg("weekly_pct"), "imoex_points": len(idx), "correlation": corr,
            "high": [r["ticker"] for r in corr if r["corr"] >= high],
            "low": [r["ticker"] for r in corr if r["corr"] < low], "high_threshold": high, "low_threshold": low}
