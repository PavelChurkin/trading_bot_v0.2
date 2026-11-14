"""Единый alert-объект (п. 18, 23), проверка порогов и журнал «почему молчит» (п. 24).

Из одного :class:`Alert` собирается и пуш, и строка истории, и дневной топ-3.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .analysis import overheat
from .settings_schema import ALERT_ACTIONS, ALERT_DIRECTIONS, HISTORY_INTERVALS

ARROWS = {"up_pulse": "▲", "down_pulse": "▼", "up_plate": "⏫", "down_plate": "⏬", "pump": "🚀",
          "sector_dump": "⚠", "pinbar_level": "🎯"}
# Действия «на покупку» — к ним применяется фильтр «исторический потолок» (issue #5, задача 1)
BUY_ACTIONS = ("enter", "late", "rebound", "plateau_buy")
# Тумблеры триггеров (issue #5, п. 16): тип сигнала → настройка
TRIGGER_KEYS = {"up_pulse": "trig_up_pulse", "down_pulse": "trig_down_pulse", "up_plate": "trig_up_plate",
                "down_plate": "trig_down_plate", "sector_dump": "trig_sector_dump",
                "pinbar_level": "trig_pinbar"}


def trigger_on(s: dict[str, Any], direction: str) -> bool:
    key = TRIGGER_KEYS.get(direction)
    return True if key is None else bool(s.get(key, True))


def hist_range(snapshot: dict[str, Any], s: dict[str, Any]) -> Optional[tuple[float, float]]:
    """(max, min) цены за период «Период истории для фильтров» — из дневных
    свечей (P2, раз в день) и сегодняшней сессии; ``None`` — истории нет."""
    ranges = snapshot.get("hist_ranges") or {}
    r = ranges.get(str(s.get("history_interval") or "5d"))
    if not r or not r[0] or not r[1] or r[1] <= 0:
        return None
    return float(r[0]), float(r[1])


ACTION_LABELS = ALERT_ACTIONS


@dataclass
class Alert:
    ticker: str
    ts: str
    direction: str  # up_pulse | down_pulse | up_plate | down_plate | pump | sector_dump
    score: int  # сила сигнала 0..100 (не вероятность)
    amplitude_atr: Optional[float]
    volume_ratio: Optional[float]
    level_break: int
    reason: str
    impulse_age_min: Optional[float] = None
    action: str = "enter"
    price: Optional[float] = None
    labels: list[str] = field(default_factory=list)
    mute_push: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def direction_label(self) -> str:
        return ALERT_DIRECTIONS.get(self.direction, self.direction)

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["direction_label"] = self.direction_label
        d["action_label"] = self.action_label
        d["arrow"] = ARROWS.get(self.direction, "")
        return d

    def db_row(self, key_id: Optional[int], pushed: bool, suppressed: Optional[str]) -> tuple:
        extra = dict(self.extra)
        extra["labels"] = self.labels
        return (key_id, self.ticker, self.ts, self.direction, self.score, self.amplitude_atr, self.volume_ratio,
                self.level_break, self.reason, self.impulse_age_min, self.action, self.price, 1 if pushed else 0,
                suppressed, json.dumps(extra, ensure_ascii=False, default=str))


ALERT_INSERT_SQL = (
    "INSERT INTO alerts (key_id, ticker, ts, direction, score, amplitude_atr, volume_ratio, level_break, reason, "
    "impulse_age_min, action, price, pushed, suppressed, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def alert_from_row(row: dict[str, Any]) -> dict[str, Any]:
    extra = json.loads(row.get("extra") or "{}")
    out = dict(row)
    out["extra"] = extra
    out["labels"] = extra.get("labels", [])
    out["direction_label"] = ALERT_DIRECTIONS.get(row["direction"], row["direction"])
    out["action_label"] = ACTION_LABELS.get(row.get("action") or "", row.get("action"))
    out["arrow"] = ARROWS.get(row["direction"], "")
    return out


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def format_push(alert: Alert | dict[str, Any], s: dict[str, Any]) -> dict[str, str]:
    """Пуш по шаблону из настроек (кабинет пользователя / админа)."""
    d = alert.to_dict() if isinstance(alert, Alert) else dict(alert)
    lb = d.get("level_break") or 0
    values = _SafeDict({
        **{k: ("" if v is None else v) for k, v in d.items() if not isinstance(v, (dict, list))},
        "level_break": "вверх" if lb > 0 else ("вниз" if lb < 0 else "нет"),
        "amplitude_atr": _fmt(d.get("amplitude_atr"), 2),
        "volume_ratio": _fmt(d.get("volume_ratio"), 2),
        "price": _fmt(d.get("price"), 4),
    })
    try:
        title = str(s.get("push_title_template") or "{ticker}").format_map(values)
        body = str(s.get("push_body_template") or "{reason}").format_map(values)
    except (ValueError, IndexError):
        title, body = f"{d.get('ticker')} · {d.get('direction_label')}", str(d.get("reason"))
    return {"title": title[:200], "body": body[:600]}


def _fmt(v: Any, nd: int) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.{nd}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


# --------------------------------------------------------------- проверки
@dataclass
class Evaluation:
    ticker: str
    triggered: bool  # прошёл базовый триггер (амплитуда/объём/уровень или плита)
    closeness: float  # 0..1+ — насколько близко к порогу (для «почему молчит»)
    checks: list[dict[str, Any]]
    failed: list[str]
    alert: Optional[Alert] = None


def _check(name: str, label: str, value: float | None, threshold: float, ok: bool) -> dict[str, Any]:
    ratio = (value / threshold) if value is not None and threshold else (1.0 if ok else 0.0)
    return {"cond": name, "label": label, "value": value, "threshold": threshold, "passed": bool(ok),
            "ratio": round(ratio, 3), "shortfall": None if ok or value is None else round(threshold - value, 4)}


def evaluate(snapshot: dict[str, Any], s: dict[str, Any], intraday: Optional[dict[str, Any]] = None,
             daily: Optional[dict[str, Any]] = None, verdict: Optional[dict[str, Any]] = None) -> Evaluation:
    """Проверка снапшота по порогам пользователя/админа.

    Без ``intraday`` (этап P1) — только базовый триггер. С ``intraday`` (P0) —
    дополнительно памп-фильтр и «поздно», формируется :class:`Alert`.
    """
    amp = snapshot.get("amplitude_atr") or 0.0
    vol = snapshot.get("volume_ratio") or 0.0
    lb = int(snapshot.get("level_break") or 0)
    plate = snapshot.get("plate") or {}
    plate_need = int(plate.get("need") or min(int(s.get("plate_min_score", 3)), 4))
    amp_max = s.get("amplitude_atr_max") or 0
    amp_ok = amp >= s["amplitude_atr_min"] and (amp_max <= 0 or amp <= amp_max)
    vol_ok = vol >= s["volume_mult"]
    lvl_ok = bool(s.get("level_break_enabled")) and lb != 0
    level_closeness = 1.0 if lb else max(0.0, 1.0 - float(snapshot.get("level_distance_atr") or 0))
    checks = [
        _check("amplitude", "Амплитуда/ATR", amp, s["amplitude_atr_min"], amp_ok),
        _check("volume", "Объём/средний", vol, s["volume_mult"], vol_ok),
        {"cond": "level", "label": "Пробой уровня", "value": lb, "threshold": 1, "passed": lvl_ok,
         "ratio": round(level_closeness, 3), "shortfall": snapshot.get("level_distance_atr"),
         "note": "расстояние до уровня в ATR"},
        {"cond": "plate", "label": f"Плита (признаков из {plate.get('enabled', 4)})", "value": plate.get("score", 0),
         "threshold": plate_need, "passed": bool(plate.get("is_plate")),
         "ratio": round(plate.get("score", 0) / plate_need, 3) if plate_need else 0.0,
         "shortfall": None if plate.get("is_plate") else max(0, plate_need - plate.get("score", 0)),
         "note": "без коррекции — обязательный признак" if s.get("plate_use_no_correction", True)
         else "признак «без коррекции» отключён"},
    ]
    # Поиск пампов (п. 20): рост за час > k×ATR — отдельный, независимый триггер
    hour_atr = float(snapshot.get("change_hour_atr") or 0.0)
    pump_k = float(s.get("pump_hour_atr_k") or 0)
    pump_search = bool(s.get("pump_search_enabled"))
    pump_hit = pump_search and pump_k > 0 and hour_atr >= pump_k
    if pump_search:
        checks.append(_check("pump", "Памп: рост за час/ATR", round(hour_atr, 3), pump_k, pump_hit))
    if amp_max and amp > amp_max:
        checks[0]["note"] = f"выше максимума {amp_max}"
    pulse = amp_ok and (vol_ok or lvl_ok)
    is_plate = bool(plate.get("is_plate"))
    # Тумблеры «Триггеры» (issue #5, п. 16): выключенный тип сигнала не срабатывает
    snap_dir = int(snapshot.get("direction") or 0)
    off = []
    if pulse and not trigger_on(s, "up_pulse" if snap_dir >= 0 else "down_pulse"):
        pulse = False
        off.append(ALERT_DIRECTIONS["up_pulse" if snap_dir >= 0 else "down_pulse"])
    if is_plate and not trigger_on(s, "up_plate" if int(plate.get("direction") or snap_dir) > 0 else "down_plate"):
        is_plate = False
        off.append(ALERT_DIRECTIONS["up_plate" if int(plate.get("direction") or snap_dir) > 0 else "down_plate"])
    if off:
        checks.append({"cond": "trigger_off", "label": "Триггер выключен в настройках", "value": ", ".join(off),
                       "threshold": None, "passed": False, "ratio": 0.0, "shortfall": None})
    triggered = pulse or is_plate or pump_hit
    # Потенциал движения (issue #5, задача 2): бумага, которая стоит на месте, не триггерит
    min_rng = float(s.get("min_range_percent") or 0)
    rng = hist_range(snapshot, s)
    if min_rng > 0 and rng:
        potential = (rng[0] - rng[1]) / rng[1] * 100
        ok = potential >= min_rng
        checks.append({**_check("potential", f"Размах за {HISTORY_INTERVALS.get(s.get('history_interval'), '')}, %",
                                round(potential, 3), min_rng, ok), "note": "(max − min) / min × 100"})
        if not ok:
            triggered = False
    amp_r = min(checks[0]["ratio"], 1.0) if not (amp_max and amp > amp_max) else 0.0
    pulse_closeness = min(amp_r if not amp_ok else 1.0, max(min(checks[1]["ratio"], 1.0), level_closeness))
    plate_closeness = plate.get("score", 0) / max(plate.get("enabled", 4) or 4, 1) * (
        1.0 if plate.get("no_correction") or not s.get("plate_use_no_correction", True) else 0.5)
    pump_closeness = min(max(hour_atr / pump_k, 0.0), 1.0) if pump_search and pump_k else 0.0
    closeness = 1.0 if triggered else round(min(max(pulse_closeness, plate_closeness, pump_closeness),
                                                    0.99 if (pulse or is_plate or pump_hit or off) else 1.0), 3)
    failed = [c["cond"] for c in checks if not c["passed"]]
    ev = Evaluation(snapshot["ticker"], triggered, closeness, checks, failed)
    if not triggered or intraday is None:
        return ev

    # ---------------- P0: тип алерта, памп-фильтр, «поздно»
    direction = int(snapshot.get("direction") or 0)
    if is_plate:
        direction = int(plate.get("direction") or direction)
        kind = "up_plate" if direction > 0 else "down_plate"
    elif pulse:
        kind = "up_pulse" if direction >= 0 else "down_pulse"
    else:
        kind = "pump"
    oh = overheat(snapshot, intraday, s)
    labels: list[str] = []
    if oh["label"]:
        labels.append(f"{oh['label']} ({oh['score']}/4)")
    if is_plate:
        n_on = plate.get("enabled", 4)
        labels.append(f"плита уверенно ({plate.get('score', 0)}/{n_on})" if plate.get("confident")
                      else f"плита ({plate.get('score', 0)}/{n_on})")
    if pump_hit and kind != "pump":
        labels.append(f"памп: +{hour_atr:.1f}×ATR за час")
    mute = False
    filters: list[str] = []
    if kind == "pump":
        action = "pump"
    elif kind == "up_pulse":
        action = "enter"
        if s.get("pump_filter_enabled", True):
            x = s.get("pump_hour_pct_exclude") or 0
            if x > 0 and (snapshot.get("change_hour_pct") or 0) > x:
                filters.append(f"уже вырос на {snapshot['change_hour_pct']:.2f}% за час (> {x}%)")
            if oh["mute_entry"]:
                filters.append(f"памп-фильтр {oh['score']}/4")
        if filters:
            action, mute = "filtered", True
        elif (snapshot.get("impulse_age_min") or 0) > s["impulse_age_max_min"] or oh["score"] >= 2 or (
                verdict and verdict.get("verdict") == "LATE"):
            action = "late"
    elif kind == "up_plate":
        action = "avoid"
    elif kind == "down_plate":
        action = "rebound"
    else:
        action = "selloff"
    if (snapshot.get("impulse_age_min") or 0) > s["impulse_age_max_min"]:
        labels.append(f"возраст импульса {snapshot['impulse_age_min']} мин — поздно")

    score = _score(amp / s["amplitude_atr_min"], vol / s["volume_mult"], lb != 0, plate.get("score", 0))
    reason = build_reason(snapshot, kind, filters)
    extra = {
        "window": snapshot.get("window"), "atr": snapshot.get("atr"), "amplitude_pct": snapshot.get("amplitude_pct"),
        "change_hour_pct": snapshot.get("change_hour_pct"), "impulse_move_pct": snapshot.get("impulse_move_pct"),
        "pullback_pct": snapshot.get("pullback_pct"), "plate": plate, "overheat": oh, "filters": filters,
        "intraday": intraday, "day_change_pct": snapshot.get("day_change_pct"),
        "approach_speed_atr": snapshot.get("approach_speed_atr"),
        "approach_intensity": snapshot.get("approach_intensity"),
        "pump_peak_forecast": snapshot.get("pump_peak_forecast"),
        "change_hour_atr": snapshot.get("change_hour_atr"),
    }
    if verdict:
        extra.update({"verdict": verdict.get("verdict"), "verdict_label": verdict.get("label"),
                      "stop": verdict.get("stop"), "take": verdict.get("take"),
                      "risk_reward": verdict.get("risk_reward"), "strength": verdict.get("strength")})
    if daily:
        extra["daily"] = {k: daily.get(k) for k in ("rsi", "atr", "atr_pct", "bb_upper", "bb_lower", "stoch_k")}
    ev.alert = Alert(
        ticker=snapshot["ticker"], ts=snapshot["ts"], direction=kind, score=score,
        amplitude_atr=snapshot.get("amplitude_atr"), volume_ratio=snapshot.get("volume_ratio"), level_break=lb,
        reason=reason, impulse_age_min=snapshot.get("impulse_age_min"), action=action, price=snapshot.get("price"),
        labels=labels, mute_push=mute, extra=extra,
    )
    if mute:
        ev.failed.append("pump_filter")
        ev.alert.extra["mute_reason"] = "памп-фильтр"
    apply_filters(ev.alert, snapshot, s)
    if ev.alert.extra.get("mute_reason") and ev.alert.extra["mute_reason"] != "памп-фильтр":
        ev.failed.append({"низкая ликвидность": "liquidity"}.get(ev.alert.extra["mute_reason"], "hist_ceiling"))
    return ev


def apply_filters(alert: Alert, snapshot: dict[str, Any], s: dict[str, Any]) -> Alert:
    """Фильтры P0 после выбора действия (issue #5): ликвидность (задача 4) и
    исторический потолок (задача 1). Вызывается и повторно — после смены
    действия на «плита после плато» (покупка) — поэтому идемпотентна."""
    done = alert.extra.setdefault("filters_done", [])
    filters = alert.extra.setdefault("filters", [])
    # Ликвидность: доля минуток с ненулевым объёмом относительно эталона (SBER)
    liq = snapshot.get("liquidity_score")
    liq_min = float(s.get("liquidity_min_score") or 0)
    alert.extra["liquidity_score"] = liq
    if "liquidity" not in done and liq is not None and liq_min > 0 and liq < liq_min:
        done.append("liquidity")
        alert.labels.append(f"Низколиквидная ({liq:.2f})")
        filters.append(f"ликвидность {liq:.2f} < {liq_min:g}")
        alert.mute_push = True
        alert.extra.setdefault("mute_reason", "низкая ликвидность")
        if alert.action in BUY_ACTIONS:
            alert.action = "filtered"
        alert.reason += f"; фильтр: низкая ликвидность ({liq:.2f} < {liq_min:g})"
    # Исторический потолок: не покупать в «опасной зоне» у максимума периода
    rng = hist_range(snapshot, s)
    if ("hist" not in done and s.get("hist_filter_enabled") and rng and alert.action in BUY_ACTIONS
            and alert.price is not None):
        hmax, hmin = rng
        danger = hmax - (hmax - hmin) * float(s.get("correction_threshold_coef") or 0)
        alert.extra["hist"] = {"max": hmax, "min": hmin, "danger": round(danger, 6),
                               "interval": s.get("history_interval")}
        if alert.price >= danger:
            done.append("hist")
            period = HISTORY_INTERVALS.get(str(s.get("history_interval")), s.get("history_interval"))
            text = f"фильтр: исторический потолок (цена {alert.price:g} ≥ {danger:g}, max {hmax:g} за {period})"
            filters.append(text)
            alert.labels.append("исторический потолок")
            alert.action = "filtered"
            alert.mute_push = True
            alert.extra.setdefault("mute_reason", "исторический потолок")
            if text not in alert.reason:
                alert.reason += "; " + text
    return alert


def _score(amp_n: float, vol_n: float, level: bool, plate_score: int) -> int:
    """Сила сигнала 0..100 — нормированная сумма признаков, не вероятность."""
    v = 35 * min(max(amp_n, 0), 2) / 2 + 35 * min(max(vol_n, 0), 2) / 2 + (15 if level else 0) + 15 * plate_score / 4
    return int(round(min(v, 100)))


def build_reason(snapshot: dict[str, Any], kind: str, filters: list[str] | None = None) -> str:
    """«SBER: амплитуда 1.2% (2.1×ATR), объём ×2.3 к среднему, ход +1.8% за 10 мин, откатов 4%»."""
    parts = [
        f"амплитуда {snapshot.get('amplitude_pct', 0):.2f}% ({snapshot.get('amplitude_atr', 0):.1f}×ATR)",
        f"объём ×{snapshot.get('volume_ratio', 0):.1f} к среднему",
    ]
    plate = snapshot.get("plate") or {}
    if kind == "pump":
        parts = [f"рост за час {snapshot.get('change_hour_pct', 0):+.2f}% ({snapshot.get('change_hour_atr', 0):.1f}×ATR)",
                 f"объём ×{snapshot.get('volume_ratio', 0):.1f} к среднему"]
        if snapshot.get("approach_intensity"):
            parts.append(f"интенсивность подхода {snapshot.get('approach_intensity'):.2f}")
        if snapshot.get("pump_peak_forecast"):
            parts.append(f"прогноз пика ≈{snapshot['pump_peak_forecast']:g} (гипотеза)")
    elif kind in ("up_plate", "down_plate") and plate:
        parts.append(f"ход {plate.get('move_pct', 0):+.2f}% за {plate.get('n', 0) * snapshot.get('window', 0)} мин "
                     f"({plate.get('move_atr', 0):.1f}×ATR)")
        parts.append(f"откатов {plate.get('pullback_pct', 0):.0f}%")
    elif snapshot.get("impulse_age_min"):
        parts.append(f"ход {snapshot.get('impulse_move_pct', 0):+.2f}% за {snapshot.get('impulse_age_min')} мин")
        parts.append(f"откатов {snapshot.get('pullback_pct', 0):.0f}%")
    lb = snapshot.get("level_break") or 0
    if lb > 0:
        parts.append(f"пробой уровня {snapshot.get('level_high'):g} вверх")
    elif lb < 0:
        parts.append(f"пробой уровня {snapshot.get('level_low'):g} вниз")
    if filters:
        parts.append("; ".join(filters))
    return f"{snapshot['ticker']}: " + ", ".join(parts)


def decision_details(ev: Evaluation, extra_failed: list[str] | None = None) -> dict[str, Any]:
    return {"checks": ev.checks, "failed": ev.failed + (extra_failed or []), "triggered": ev.triggered}
