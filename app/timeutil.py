"""Время: всё считается по Москве (UTC+3, без перехода на летнее время).

Используем фиксированное смещение вместо zoneinfo, чтобы не зависеть от пакета
tzdata на Windows.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

MSK = timezone(timedelta(hours=3), name="MSK")
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_msk() -> datetime:
    return datetime.now(MSK).replace(microsecond=0)


def parse_ts(value: str) -> datetime:
    """Строка ISS 'YYYY-MM-DD HH:MM:SS' (московское время) -> aware datetime."""
    return datetime.strptime(value[:19], TS_FORMAT).replace(tzinfo=MSK)


def fmt_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK).strftime(TS_FORMAT)


def parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    try:
        hh, mm = str(value).strip().split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def in_quiet_hours(now: datetime, quiet_from: str | None, quiet_to: str | None) -> bool:
    """True, если now попадает в «тихие часы» [from, to). Интервал может переходить через полночь."""
    start, end = parse_hhmm(quiet_from), parse_hhmm(quiet_to)
    if start is None or end is None or start == end:
        return False
    current = now.astimezone(MSK).time()
    if start < end:
        return start <= current < end
    return current >= start or current < end
