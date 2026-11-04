"""Схема настроек.

Все пороги живут здесь в одном месте. Админ задаёт глобальные значения
(они же — значения по умолчанию для пользователей), пользователь может
переопределить у себя всё, что помечено scope="user".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ALERT_DIRECTIONS = {
    "up_pulse": "Пульсация вверх",
    "down_pulse": "Пульсация вниз",
    "up_plate": "Плита вверх",
    "down_plate": "Плита вниз",
    "sector_dump": "Массовый снос",
}

DASHBOARD_COLUMNS = {
    "ticker": "Тикер",
    "name": "Название",
    "price": "Цена",
    "day_change_pct": "Изм. дня, %",
    "amplitude_pct": "Амплитуда, %",
    "amplitude_atr": "Амплитуда/ATR",
    "atr": "ATR (окно)",
    "volume": "Объём свечи",
    "volume_ratio": "Объём / средний",
    "volume_today": "Объём дня",
    "value_today": "Оборот дня, ₽",
    "change_hour_pct": "Изм. за час, %",
    "level_break": "Пробой уровня",
    "impulse_age_min": "Возраст импульса, мин",
    "pullback_pct": "Откаты, %",
    "plate_score": "Плита (из 4)",
    "threshold_closeness": "Близость к порогу",
    "updated": "Обновлено",
}

CHARTS = {
    "price": "Цена + объём (свечи)",
    "bollinger": "Боллинджер поверх цены",
    "atr": "ATR",
    "rsi": "RSI (30/70)",
    "macd": "MACD (hist + signal)",
    "stoch": "Стохастик K/D",
    "amplitude_atr": "Амплитуда / ATR",
    "volume_ratio": "Объём / средний",
    "impulse_age": "Возраст импульса",
    "threshold_distance": "Расстояние до порога",
}

DEFAULT_PUSH_TITLE = "{ticker} · {direction_label}"
DEFAULT_PUSH_BODY = "{reason}. {action_label}. Сила сигнала {score}/100 (не вероятность)"


@dataclass(frozen=True)
class Setting:
    key: str
    type: str  # int | float | bool | str | time | list
    default: Any
    label: str
    group: str
    scope: str = "user"  # user — переопределяется пользователем, admin — только админ
    min: float | None = None
    max: float | None = None
    help: str = ""
    choices: dict[str, str] = field(default_factory=dict)


SETTINGS: list[Setting] = [
    # --- Сканер (только админ) ---
    Setting("scan_interval_min", "int", 15, "Интервал цикла сканера, мин", "Сканер", "admin", 15, 1440,
            "15 минут — минимум (п. 1, 17)."),
    Setting("orderbook_enabled", "bool", True, "Запрашивать стакан на P0", "Сканер", "admin",
            help="ISS отдаёт стакан не всем; если недоступен — сканер сам отключит запросы до перезапуска."),
    Setting("daily_history_days", "int", 120, "Глубина дневных свечей для индикаторов P0, дней", "Сканер", "admin", 30, 400),
    Setting("retention_days", "int", 7, "Хранить свечи/метрики/журнал, дней", "Сканер", "admin", 1, 60),
    Setting("sector_min_tickers", "int", 5, "Массовый снос: минимум бумаг в одну сторону", "Сканер", "admin", 2, 100),
    Setting("test_push_delay_sec", "float", 1.0, "Таймер тестового пуша по умолчанию, сек", "Уведомления", "admin", 0, 3600),
    # --- Сигнал ---
    Setting("candle_window_min", "int", 15, "Окно свечи, мин", "Сигнал", "user", 1, 60,
            "Свечи собираются из минуток ISS в окно нужной длины."),
    Setting("amplitude_atr_min", "float", 1.5, "Кратность ATR: амплитуда свечи / ATR ≥ k", "Сигнал", "user", 0.1, 20,
            "Главный порог (п. 13). Типичное значение 1.5–2."),
    Setting("amplitude_atr_max", "float", 6.0, "Максимум амплитуда/ATR (0 — без ограничения)", "Сигнал", "user", 0, 50),
    Setting("volume_mult", "float", 1.5, "Объёмный множитель (объём ≥ k × среднего)", "Сигнал", "user", 0.1, 50),
    Setting("volume_avg_window", "int", 20, "Окно среднего объёма, свечей", "Сигнал", "user", 3, 200),
    Setting("level_window", "int", 20, "Окно локального уровня, свечей", "Сигнал", "user", 3, 200),
    Setting("level_break_enabled", "bool", True, "Учитывать пробой локального уровня", "Сигнал", "user"),
    Setting("rsi_overbought", "float", 70, "RSI: перекупленность", "Сигнал", "user", 50, 100),
    Setting("rsi_oversold", "float", 30, "RSI: перепроданность", "Сигнал", "user", 0, 50),
    # --- Памп-фильтр / плита ---
    Setting("pump_hour_atr_k", "float", 3.0, "Памп-фильтр: рост за час > k × ATR", "Памп-фильтр", "user", 0.1, 50),
    Setting("pump_hour_pct_exclude", "float", 3.0, "Исключать бумаги, выросшие за час > X% (0 — выкл.)", "Памп-фильтр", "user", 0, 100,
            "Защита от входа на хаях (п. 10)."),
    Setting("plate_series_n", "int", 3, "Длина импульса: свечей подряд в одну сторону", "Памп-фильтр", "user", 2, 20),
    Setting("plate_jerk_atr", "float", 3.0, "Порог одиночного рывка, × ATR", "Памп-фильтр", "user", 0.5, 50),
    Setting("plate_pullback_max_pct", "float", 20, "Плита: максимум откатов внутри серии, % хода", "Памп-фильтр", "user", 0, 100),
    Setting("plate_volume_mult", "float", 1.5, "Плита: объём каждой свечи ≥ k × среднего", "Памп-фильтр", "user", 0.1, 50),
    Setting("plate_move_atr_k", "float", 2.5, "Плита: суммарный ход серии ≥ k × ATR", "Памп-фильтр", "user", 0.1, 50),
    Setting("impulse_age_max_min", "int", 45, "Возраст импульса, после которого «поздно», мин", "Памп-фильтр", "user", 1, 1440),
    Setting("late_day_atr", "float", 1.5, "«Поздно»: рост дня ≥ k × дневного ATR", "Памп-фильтр", "user", 0.1, 50),
    Setting("late_bb_atr", "float", 0.25, "«Поздно»: цена ближе k × ATR к верхней полосе Боллинджера", "Памп-фильтр", "user", 0, 10),
    Setting("stop_atr_mult", "float", 1.5, "Стоп = цена ∓ k × дневного ATR", "Риск", "user", 0.1, 20),
    Setting("take_atr_mult", "float", 3.0, "Тейк = цена ± k × дневного ATR", "Риск", "user", 0.1, 50),
    # --- Уведомления ---
    Setting("alert_types", "list", list(ALERT_DIRECTIONS), "Типы уведомлений", "Уведомления", "user",
            choices=ALERT_DIRECTIONS),
    Setting("cooldown_min", "int", 30, "Кулдаун на тикер, мин", "Уведомления", "user", 0, 1440),
    Setting("daily_push_limit", "int", 30, "Лимит пушей в день", "Уведомления", "user", 0, 10000),
    Setting("quiet_from", "time", "23:00", "Тихие часы: с (МСК, пусто — выкл.)", "Уведомления", "user"),
    Setting("quiet_to", "time", "07:00", "Тихие часы: до (МСК)", "Уведомления", "user"),
    Setting("daily_report_enabled", "bool", True, "Итоговый пуш за день", "Уведомления", "user"),
    Setting("daily_report_time", "time", "19:00", "Время дневного отчёта (МСК)", "Уведомления", "user"),
    Setting("push_title_template", "str", DEFAULT_PUSH_TITLE, "Формат пуша: заголовок", "Уведомления", "user",
            help="Поля: {ticker} {direction} {direction_label} {arrow} {score} {amplitude_atr} {volume_ratio} "
                 "{level_break} {reason} {action_label} {impulse_age_min} {price} {ts}"),
    Setting("push_body_template", "str", DEFAULT_PUSH_BODY, "Формат пуша: текст", "Уведомления", "user"),
    # --- Дашборд ---
    Setting("dashboard_columns", "list",
            ["ticker", "name", "price", "day_change_pct", "amplitude_atr", "volume_ratio", "change_hour_pct",
             "level_break", "impulse_age_min", "plate_score"],
            "Колонки таблицы", "Дашборд", "user", choices=DASHBOARD_COLUMNS),
    Setting("charts", "list", list(CHARTS), "Графики в карточке тикера", "Дашборд", "user", choices=CHARTS),
]

SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}


def defaults() -> dict[str, Any]:
    return {s.key: (list(s.default) if isinstance(s.default, list) else s.default) for s in SETTINGS}


def coerce(key: str, value: Any) -> Any:
    """Проверка и приведение значения настройки. Бросает ValueError."""
    spec = SETTINGS_BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"Неизвестная настройка: {key}")
    if spec.type == "int":
        out: Any = int(float(value))
    elif spec.type == "float":
        out = float(value)
    elif spec.type == "bool":
        out = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on", "да"}
    elif spec.type == "time":
        out = str(value or "").strip()
        if out:
            hh, mm = out.split(":")[:2]
            if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise ValueError(f"{spec.label}: время должно быть ЧЧ:ММ")
            out = f"{int(hh):02d}:{int(mm):02d}"
    elif spec.type == "list":
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        out = [str(v) for v in value]
        if spec.choices:
            unknown = [v for v in out if v not in spec.choices]
            if unknown:
                raise ValueError(f"{spec.label}: неизвестные значения {unknown}")
    else:
        out = str(value)
        if len(out) > 500:
            raise ValueError(f"{spec.label}: слишком длинная строка")
    if spec.type in {"int", "float"}:
        if spec.min is not None and out < spec.min:
            raise ValueError(f"{spec.label}: минимум {spec.min}")
        if spec.max is not None and out > spec.max:
            raise ValueError(f"{spec.label}: максимум {spec.max}")
    return out


def schema_for_ui(is_admin: bool) -> list[dict[str, Any]]:
    out = []
    for s in SETTINGS:
        if s.scope == "admin" and not is_admin:
            continue
        out.append({
            "key": s.key, "type": s.type, "label": s.label, "group": s.group, "scope": s.scope,
            "min": s.min, "max": s.max, "help": s.help, "choices": s.choices, "default": s.default,
        })
    return out
