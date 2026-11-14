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
    "pump": "Памп (рост за час)",
    "sector_dump": "Массовый снос",
    "pinbar_level": "Возврат к уровню пин-бара",
}

# Действие (рекомендация) алерта — по нему фильтруются пуши (настройка push_actions)
ALERT_ACTIONS = {
    "enter": "Кандидат на вход",
    "unique_enter": "Уникальный кандидат на вход",
    "your_enter": "Ваш кандидат на вход",
    "late": "Поздно, ждать откат",
    "filtered": "Памп-фильтр: вход не рекомендуется",
    "pump": "Памп — не догонять, ждать пик/откат",
    "avoid": "Не входить / фиксировать",
    "rebound": "Кандидат на откуп/усреднение",
    "selloff": "Распродажа — наблюдать",
    "market": "Отраслевой/рыночный шок — не плита в отдельной бумаге",
    "plateau_buy": "Плита вниз после плато — кандидат на покупку",
    "pinbar_level": "Цена у уровня скрытой ликвидности — вероятен отскок/продолжение",
}

# Периоды истории для фильтров «Исторический потолок» и «Потенциал движения» (issue #5)
HISTORY_INTERVALS = {"1d": "1 день", "5d": "5 дней", "1m": "1 месяц", "3m": "3 месяца"}
HISTORY_DAYS = {"1d": 1, "5d": 5, "1m": 21, "3m": 63}  # торговых дней
PAPER_SIDES = {"auto": "Авто (по сигналу)", "long": "Лонг", "short": "Шорт"}

# Тип бумаги по SECTYPE из ISS (доска TQBR: акции, расписки, паи фондов)
SECURITY_KINDS = {
    "share": "Акции",
    "fund": "Фонды (ПИФ/ETF)",
    "receipt": "Депозитарные расписки",
    "bond": "Облигации",
    "other": "Прочее",
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
    "approach_intensity": "Интенсивность к уровню",
    "plate_score": "Плита (из 4)",
    "threshold_closeness": "Близость к порогу",
    "updated": "Обновлено",
    "liquidity_score": "Ликвидность (0–1)",
    "actions": "Действия",
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
    Setting("market_hours_only", "bool", True, "Сканировать только в часы торгов", "Сканер", "admin",
            help="Вне часов торгов (по умолчанию 23:50–07:00 МСК биржа закрыта) сканер не ходит в ISS "
                 "и не шлёт сигналов."),
    Setting("market_open", "time", "07:00", "Начало торгов (МСК)", "Сканер", "admin"),
    Setting("market_close", "time", "23:50", "Конец торгов (МСК)", "Сканер", "admin"),
    Setting("skip_weekends", "bool", False, "Не сканировать в субботу и воскресенье", "Сканер", "admin",
            help="MOEX проводит торги выходного дня не по всем бумагам; включите, если они не нужны."),
    Setting("warmup_enabled", "bool", True, "Игнорировать первый цикл после запуска/перерыва", "Сканер", "admin",
            help="Первый цикл после старта, ночи или простоя догружает свечи за весь пропуск и видит «старые» "
                 "импульсы как новые. Такие алерты пишутся в историю с пометкой «прогрев», но пуш не уходит."),
    Setting("warmup_gap_min", "int", 45, "Перерыв, после которого нужен прогрев, мин", "Сканер", "admin", 1, 10080),
    Setting("orderbook_enabled", "bool", True, "Запрашивать стакан на P0", "Сканер", "admin",
            help="ISS отдаёт стакан не всем; если недоступен — сканер сам отключит запросы до перезапуска."),
    Setting("daily_history_days", "int", 120, "Глубина дневных свечей для индикаторов P0, дней", "Сканер", "admin", 30, 400),
    Setting("retention_days", "int", 7, "Хранить данные в базе (свечи, метрики, журнал, сигналы), дней",
            "Сканер", "admin", 1, 365,
            "Раз в сутки (и кнопкой «Освободить память» в админке) удаляются записи старше N дней — база "
            "«перезаписывается со смещением» к новым данным, а кнопка ещё и сжимает файл базы (VACUUM). "
            "Графики на 14/30 дней старше этого срока догружают часовые свечи из ISS."),
    Setting("min_free_disk_gb", "float", 1.0, "Минимум свободного места на диске, ГБ", "Сканер", "admin", 0, 10000,
            "Проверяется перед каждым циклом. Если свободно меньше — всем пользователям уходит пуш, сканирование "
            "приостанавливается до кнопки «Проверить память» в админке (0 — не проверять)."),
    Setting("liquidity_test_candles", "int", 1000, "Ликвидность: эталонных минутных свечей", "Сканер", "admin",
            60, 20000, "Эталон — последние N минуток голубой фишки (SBER): ожидаемое число свечей. Ликвидность "
            "бумаги = минутки с ненулевым объёмом за тот же период / N."),
    Setting("sector_min_tickers", "int", 5, "Массовый снос: минимум бумаг в одну сторону", "Сканер", "admin", 2, 100),
    Setting("pinbar_shadow_ratio", "float", 2.0, "Пин-бар: тень ≥ k × тела свечи", "Пин-бары", "admin", 0.5, 20,
            "Свеча-«стрекоза» (Liquidity Sweep): длинный хвост — вынос цены с мгновенным возвратом."),
    Setting("pinbar_max_body_ratio", "float", 0.3, "Пин-бар: тело ≤ доля диапазона свечи", "Пин-бары", "admin",
            0.01, 1),
    Setting("pinbar_volume_z", "float", 1.5, "Пин-бар: объём, z-score ≥ (0 — не проверять)", "Пин-бары", "admin",
            0, 10, "Подтверждение объёмом: (объём − средний) / ст. отклонение по окну среднего объёма."),
    Setting("pinbar_work_candles", "int", 10, "Пин-бар: «отработан», если цена коснулась уровня за N свечей",
            "Пин-бары", "admin", 1, 200),
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
    # --- Памп-фильтр / поиск пампов ---
    Setting("pump_filter_enabled", "bool", True, "Памп-фильтр включён", "Памп-фильтр", "user",
            help="Выключите, если нужны сигналы входа и по бумагам, которые уже резко выросли "
                 "(некоторые бумаги после пампа продолжают расти)."),
    Setting("pump_search_enabled", "bool", False, "Памп: искать пампы (независимо от остальных триггеров)", "Триггеры", "user",
            help="Отдельный сигнал «Памп»: рост за час > k × ATR, даже если амплитуда/объём текущей свечи "
                 "не прошли порог. С прогнозом пика по интенсивности подхода к уровню (гипотеза)."),
    Setting("pump_hour_atr_k", "float", 3.0, "Памп: рост за час > k × ATR", "Памп-фильтр", "user", 0.1, 50),
    Setting("pump_hour_pct_exclude", "float", 3.0, "Исключать бумаги, выросшие за час > X% (0 — выкл.)", "Памп-фильтр", "user", 0, 100,
            "Защита от входа на хаях (п. 10)."),
    Setting("impulse_age_max_min", "int", 45, "Возраст импульса, после которого «поздно», мин", "Памп-фильтр", "user", 1, 1440),
    Setting("late_day_atr", "float", 1.5, "«Поздно»: рост дня ≥ k × дневного ATR", "Памп-фильтр", "user", 0.1, 50),
    Setting("late_bb_atr", "float", 0.25, "«Поздно»: цена ближе k × ATR к верхней полосе Боллинджера", "Памп-фильтр", "user", 0, 10),
    # --- Плита: 4 признака, каждый настраивается ---
    Setting("plate_use_series", "bool", True, "Признак 1 — серия: учитывать", "Плита", "user",
            help="N свечей окна подряд в одну сторону ИЛИ одиночный рывок одной свечой ≥ k × ATR."),
    Setting("plate_series_n", "int", 3, "Признак 1 — длина серии, свечей подряд", "Плита", "user", 2, 20),
    Setting("plate_jerk_atr", "float", 3.0, "Признак 1 — одиночный рывок, тело свечи ≥ k × ATR", "Плита", "user", 0.5, 50),
    Setting("plate_use_no_correction", "bool", True, "Признак 2 — без коррекции: учитывать (обязательный)", "Плита", "user",
            help="Встречные движения внутри серии (откаты между закрытиями + встречные тени) малы. Отличает "
                 "плиту от органического ступенчатого тренда; включённый признак обязателен."),
    Setting("plate_pullback_max_pct", "float", 16.7, "Признак 2 — максимум откатов, % пройденного пути", "Плита", "user", 0, 100,
            "Откаты / (ход серии + откаты) × 100: 0% — ни одного встречного движения, 100% — только откаты. "
            "16.7% ≈ «откаты не больше 20% чистого хода серии»."),
    Setting("plate_use_volume", "bool", True, "Признак 3 — объём: учитывать", "Плита", "user",
            help="Объём каждой свечи серии ≥ k × среднего объёма до серии."),
    Setting("plate_volume_mult", "float", 1.5, "Признак 3 — объём каждой свечи ≥ k × среднего", "Плита", "user", 0.1, 50),
    Setting("plate_use_move", "bool", True, "Признак 4 — ход: учитывать", "Плита", "user",
            help="Суммарный ход серии (закрытие последней − открытие первой) ≥ k × ATR."),
    Setting("plate_move_atr_k", "float", 2.5, "Признак 4 — суммарный ход серии ≥ k × ATR", "Плита", "user", 0.1, 50),
    Setting("plate_min_score", "int", 3, "Плита: минимум выполненных признаков", "Плита", "user", 1, 4,
            "Из учитываемых признаков. Если учитывается меньше признаков, чем минимум, нужны все учитываемые."),
    # --- Триггеры: что вообще ищет сканер (issue #5) ---
    Setting("trig_up_pulse", "bool", True, "Пульсация вверх", "Триггеры", "user",
            help="Свеча окна с амплитудой ≥ k×ATR и объёмом ≥ k×среднего (или пробоем уровня), рост."),
    Setting("trig_down_pulse", "bool", True, "Пульсация вниз", "Триггеры", "user",
            help="То же, но падение: распродажа, наблюдать."),
    Setting("trig_up_plate", "bool", True, "Плита вверх", "Триггеры", "user",
            help="Серия свечей вверх без коррекции на объёме — момент фиксации, не догонять."),
    Setting("trig_down_plate", "bool", True, "Плита вниз", "Триггеры", "user",
            help="Серия свечей вниз без коррекции — кандидат на откуп/усреднение."),
    Setting("trig_sector_dump", "bool", True, "Массовый снос (рыночный шок)", "Триггеры", "user",
            help="Много бумаг одновременно падают — шок рынка, а не проблема отдельной бумаги."),
    Setting("trig_plateau_dump", "bool", True, "Плита вниз после длительного плато → покупка", "Триггеры", "user",
            help="Бумага долго стояла в узком диапазоне, затем её резко «снесли» плитой вниз — частый "
                 "сценарий выноса стопов; сигнал «Плита вниз после плато — кандидат на покупку»."),
    Setting("plateau_min_hours", "float", 3.0, "Плато: длительность перед плитой, часов", "Триггеры", "user", 0.5, 72),
    Setting("plateau_max_range_pct", "float", 1.5, "Плато: максимальный диапазон цены, %", "Триггеры", "user",
            0.1, 20, "(max − min) / min × 100 по минуткам за время плато."),
    Setting("trig_pinbar", "bool", True, "Пин-бары: уровни скрытой ликвидности", "Триггеры", "user",
            help="Свечи с аномально длинной тенью запоминаются как уровни; при возврате цены к уровню — пуш."),
    Setting("pinbar_approach_pct", "float", 1.0, "Пин-бар: пуш, когда цена в пределах X % от уровня", "Триггеры",
            "user", 0.05, 10),
    # --- Фильтры по истории и ликвидности (issue #5, задачи 1, 2, 4) ---
    Setting("history_interval", "str", "5d", "Период истории для фильтров", "Фильтры", "user",
            choices=HISTORY_INTERVALS,
            help="Дневные свечи за период + сегодняшняя сессия: исторический максимум и минимум."),
    Setting("hist_filter_enabled", "bool", False, "Фильтр «исторический потолок»: включён", "Фильтры", "user",
            help="Сигнал на покупку не даётся, если цена в «опасной зоне» у максимума периода: "
                 "цена ≥ max − (max − min) × коэффициент. Такой сигнал помечается «фильтр» и глушится."),
    Setting("correction_threshold_coef", "float", 0.7, "Потолок: коэффициент коррекции (0–1)", "Фильтры", "user", 0, 1,
            "Опасная зона = max − (max − min) × k. При k = 0.7 опасны верхние 70 % диапазона; "
            "меньше k — мягче фильтр."),
    Setting("min_range_percent", "float", 1.0, "Потенциал: минимум размаха за период, % (0 — выкл.)", "Фильтры",
            "user", 0, 100, "(max − min) / min × 100. Бумаги, которые стоят на месте, не триггерят совсем."),
    Setting("alert_threshold_percent", "float", 0, "Пуш, только если изменение свечи или дня ≥ X % (0 — выкл.)",
            "Фильтры", "user", 0, 100),
    Setting("liquidity_min_score", "float", 0.8, "Ликвидность: минимум (0–1, 0 — выкл.)", "Фильтры", "user", 0, 1,
            "Доля минуток с ненулевым объёмом относительно эталона (SBER). Ниже порога — «Низколиквидная»: "
            "сигналы глушатся, чтобы не войти туда, откуда трудно выйти."),
    # --- Риск, paper и комиссии ---
    Setting("stop_atr_mult", "float", 1.5, "Стоп = цена ∓ k × дневного ATR", "Риск", "user", 0.1, 20),
    Setting("take_atr_mult", "float", 3.0, "Тейк = цена ± k × дневного ATR", "Риск", "user", 0.1, 50),
    Setting("paper_auto_close", "bool", True, "Стоп и тейк в paper: закрывать автоматически", "Риск", "user",
            help="Выключите — сделки не будут закрываться сами по стопу/тейку, закрывайте вручную по уведомлениям."),
    Setting("paper_default_side", "str", "auto", "Paper: сторона сделки по умолчанию", "Риск", "user",
            choices=PAPER_SIDES,
            help="Авто — шорт по «плите вверх», иначе лонг. Сторону открытой сделки можно сменить кнопкой в Paper."),
    Setting("fee_entry_pct", "float", 0.3, "Комиссия брокера на вход, %", "Риск", "user", 0, 10),
    Setting("fee_exit_pct", "float", 0.3, "Комиссия брокера на выход, %", "Риск", "user", 0, 10),
    Setting("tax_profit_pct", "float", 13.0, "Налог на прибыль, %", "Риск", "user", 0, 50,
            "Удерживается с положительного результата после комиссий."),
    # --- Уведомления ---
    Setting("alert_types", "list", list(ALERT_DIRECTIONS), "Типы уведомлений", "Уведомления", "user",
            choices=ALERT_DIRECTIONS),
    Setting("push_actions", "list", list(ALERT_ACTIONS), "Пуши по действиям", "Уведомления", "user",
            choices=ALERT_ACTIONS,
            help="Снимите галочку, чтобы глушить пуши с этим действием (например «фиксировать» и «поздно, "
                 "ждать откат»). Сигналы всё равно видны во вкладке «Сигналы»."),
    Setting("push_price_min", "float", 0, "Пуш только если цена ≥ X ₽ (0 — без ограничения)", "Уведомления", "user", 0, 1e9),
    Setting("push_price_max", "float", 0, "Пуш только если цена ≤ X ₽ (0 — без ограничения)", "Уведомления", "user", 0, 1e9),
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
    # --- Сопровождение и paper ---
    Setting("track_enabled", "bool", True, "Сопровождение: уникальный / ваш кандидат на вход", "Сопровождение", "user",
            help="После момента фиксации (плита вверх) или распродажи (импульс/плита вниз) бот сам следит за бумагой "
                 "и при следующем сигнале входа шлёт «Уникальный кандидат на вход», а для избранного — «Ваш "
                 "кандидат на вход». Такие пуши не учитываются в дневном лимите."),
    Setting("track_window_days", "int", 5, "Сопровождение: сколько дней помнить фиксацию/распродажу", "Сопровождение",
            "user", 1, 60),
    Setting("paper_notify_enabled", "bool", True, "Пуши сопровождения paper", "Сопровождение", "user",
            help="По открытым paper-позициям: «кандидат на откуп» / «распродажа», достижение цели в % и в ₽. "
                 "Не учитываются в дневном лимите пушей."),
    Setting("paper_target_pct", "float", 5.0, "Paper: пуш при прибыли ≥ X % (0 — выкл.)", "Сопровождение", "user", 0, 1000),
    Setting("paper_target_rub", "float", 0, "Paper: пуш при прибыли ≥ N ₽ (0 — выкл.)", "Сопровождение", "user", 0, 1e12,
            "Профит = (цена − цена входа) × количество акций (для шорта — наоборот)."),
    # --- Дашборд ---
    Setting("dashboard_columns", "list",
            ["ticker", "name", "price", "day_change_pct", "amplitude_atr", "volume_ratio", "change_hour_pct",
             "level_break", "impulse_age_min", "plate_score", "actions"],
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
        if spec.choices and out not in spec.choices:
            raise ValueError(f"{spec.label}: допустимо {', '.join(spec.choices)}")
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
