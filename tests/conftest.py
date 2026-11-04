"""Общие фикстуры и генераторы синтетических свечей для тестов."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import settings_schema as ss  # noqa: E402
from app.db import Database  # noqa: E402
from app.timeutil import TS_FORMAT  # noqa: E402


def flat_minutes(start: str, n: int, base: float = 100.0, volume: float = 1000.0) -> list[dict]:
    """Боковик: цена колеблется ±0.05 вокруг base, тени ±0.1, объём постоянный."""
    t0 = datetime.strptime(start, TS_FORMAT)
    out = []
    for i in range(n):
        wiggle = 0.05 if (i // 3) % 2 == 0 else -0.05
        o = base
        c = base + wiggle
        out.append({"ts": (t0 + timedelta(minutes=i)).strftime(TS_FORMAT), "open": o, "close": c,
                    "high": max(o, c) + 0.05, "low": min(o, c) - 0.05, "volume": volume, "value": volume * c})
    return out


def trend_minutes(start: str, n: int, base: float, step: float, volume: float, wick: float = 0.0) -> list[dict]:
    """Однонаправленное движение: каждую минуту цена меняется на ``step``."""
    t0 = datetime.strptime(start, TS_FORMAT)
    out = []
    price = base
    for i in range(n):
        o, c = price, price + step
        out.append({"ts": (t0 + timedelta(minutes=i)).strftime(TS_FORMAT), "open": o, "close": c,
                    "high": max(o, c) + wick, "low": min(o, c), "volume": volume, "value": volume * c})
        price = c
    return out


def next_ts(candles: list[dict]) -> str:
    return (datetime.strptime(candles[-1]["ts"], TS_FORMAT) + timedelta(minutes=1)).strftime(TS_FORMAT)


def pulse_day(day: str = "2026-11-02", base: float = 100.0, flat_windows: int = 20, move: float = 0.6,
              volume_mult: float = 4.0) -> list[dict]:
    """20 окон по 15 мин боковика, затем одно окно импульса вверх с объёмом ×volume_mult."""
    flat = flat_minutes(f"{day} 10:00:00", 15 * flat_windows, base)
    pulse = trend_minutes(next_ts(flat), 15, flat[-1]["close"], move / 15, 1000 * volume_mult)
    return flat + pulse


@pytest.fixture
def settings() -> dict:
    return ss.defaults()


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "test.sqlite3")
    yield d
    d.close()
