"""SQLite-хранилище (локально). Одна база на всех пользователей.

Все обращения идут через один объект :class:`Database` с блокировкой —
этого достаточно для одного процесса (сканер + веб). Для сервера схему
можно перенести в Postgres без изменения логики.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import settings_schema
from .timeutil import fmt_ts, now_msk

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    key_hint TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    key_id INTEGER NOT NULL REFERENCES access_keys(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER NOT NULL REFERENCES access_keys(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS securities (
    ticker TEXT PRIMARY KEY,
    name TEXT, full_name TEXT, lot INTEGER,
    prev_price REAL, last REAL, open REAL, high REAL, low REAL,
    volume_today REAL, value_today REAL, day_change_pct REAL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS candles (
    ticker TEXT NOT NULL, ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, value REAL,
    PRIMARY KEY (ticker, ts)
);
CREATE TABLE IF NOT EXISTS snapshots (
    ticker TEXT NOT NULL, window INTEGER NOT NULL, ts TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (ticker, window)
);
CREATE TABLE IF NOT EXISTS metrics (
    ticker TEXT NOT NULL, ts TEXT NOT NULL, metric TEXT NOT NULL, value REAL
);
CREATE INDEX IF NOT EXISTS idx_metrics ON metrics (ticker, metric, ts);
CREATE INDEX IF NOT EXISTS idx_metrics_metric_ts ON metrics (metric, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER,
    ticker TEXT NOT NULL, ts TEXT NOT NULL, direction TEXT NOT NULL,
    score REAL, amplitude_atr REAL, volume_ratio REAL, level_break INTEGER,
    reason TEXT, impulse_age_min REAL, action TEXT, price REAL,
    pushed INTEGER NOT NULL DEFAULT 0, suppressed TEXT, extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_key_ts ON alerts (key_id, ts);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts (key_id, ticker, ts);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER, ts TEXT NOT NULL, ticker TEXT NOT NULL,
    passed INTEGER NOT NULL, failed TEXT, closeness REAL, details TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_cycle ON decisions (cycle_id);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts);
CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER, day TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
    entry_price REAL NOT NULL, stop REAL, take REAL, alert_id INTEGER, note TEXT,
    opened_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    exit_price REAL, closed_at TEXT, result_pct REAL
);
CREATE TABLE IF NOT EXISTS favorites (
    key_id INTEGER NOT NULL, ticker TEXT NOT NULL, PRIMARY KEY (key_id, ticker)
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ----------------------------------------------------------------- settings
    def global_settings(self) -> dict[str, Any]:
        out = settings_schema.defaults()
        for row in self.query("SELECT key, value FROM settings WHERE scope = 'global'"):
            if row["key"] in out:
                out[row["key"]] = json.loads(row["value"])
        return out

    def user_overrides(self, key_id: int) -> dict[str, Any]:
        rows = self.query("SELECT key, value FROM settings WHERE scope = ?", (f"user:{key_id}",))
        return {r["key"]: json.loads(r["value"]) for r in rows if r["key"] in settings_schema.SETTINGS_BY_KEY}

    def effective_settings(self, key_id: int | None) -> dict[str, Any]:
        """Глобальные (админские) настройки + пользовательские переопределения."""
        out = self.global_settings()
        if key_id is not None:
            for k, v in self.user_overrides(key_id).items():
                if settings_schema.SETTINGS_BY_KEY[k].scope == "user":
                    out[k] = v
        return out

    def save_settings(self, scope: str, values: dict[str, Any]) -> None:
        with self.transaction() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings (scope, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value",
                    (scope, key, json.dumps(value, ensure_ascii=False)),
                )

    def reset_user_settings(self, key_id: int, keys: Iterable[str] | None = None) -> None:
        if keys is None:
            self.execute("DELETE FROM settings WHERE scope = ?", (f"user:{key_id}",))
        else:
            for k in keys:
                self.execute("DELETE FROM settings WHERE scope = ? AND key = ?", (f"user:{key_id}", k))

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM settings WHERE scope = 'state' AND key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        self.save_settings("state", {key: value})

    # ------------------------------------------------------------------ candles
    def upsert_candles(self, ticker: str, candles: list[dict[str, Any]]) -> int:
        if not candles:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO candles (ticker, ts, open, high, low, close, volume, value) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ticker, ts) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume, value=excluded.value",
                [(ticker, c["ts"], c["open"], c["high"], c["low"], c["close"], c["volume"], c.get("value", 0))
                 for c in candles],
            )
        return len(candles)

    def last_candle_ts(self, ticker: str) -> str | None:
        row = self.query_one("SELECT MAX(ts) AS ts FROM candles WHERE ticker = ?", (ticker,))
        return row["ts"] if row else None

    def candles_since(self, ticker: str, since_ts: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT ts, open, high, low, close, volume, value FROM candles WHERE ticker = ? AND ts >= ? ORDER BY ts",
            (ticker, since_ts),
        )

    def candle_days(self, ticker: str) -> list[str]:
        rows = self.query("SELECT DISTINCT substr(ts, 1, 10) AS d FROM candles WHERE ticker = ? ORDER BY d", (ticker,))
        return [r["d"] for r in rows]

    # ------------------------------------------------------------------ metrics
    def add_metrics(self, rows: list[tuple[str, str, str, float | None]]) -> None:
        if not rows:
            return
        with self.transaction() as conn:
            conn.executemany("INSERT INTO metrics (ticker, ts, metric, value) VALUES (?,?,?,?)", rows)

    def metric_series(self, ticker: str, metric: str, since_ts: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT ts, value FROM metrics WHERE ticker = ? AND metric = ? AND ts >= ? ORDER BY ts",
            (ticker, metric, since_ts),
        )

    def save_snapshot(self, ticker: str, window: int, ts: str, data: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO snapshots (ticker, window, ts, data) VALUES (?,?,?,?) "
            "ON CONFLICT(ticker, window) DO UPDATE SET ts = excluded.ts, data = excluded.data",
            (ticker, window, ts, json.dumps(data, ensure_ascii=False, default=str)),
        )

    def snapshots(self, window: int) -> dict[str, dict[str, Any]]:
        rows = self.query("SELECT ticker, data FROM snapshots WHERE window = ?", (window,))
        return {r["ticker"]: json.loads(r["data"]) for r in rows}

    def snapshot(self, ticker: str, window: int) -> dict[str, Any] | None:
        row = self.query_one("SELECT data FROM snapshots WHERE ticker = ? AND window = ?", (ticker, window))
        return json.loads(row["data"]) if row else None

    # ---------------------------------------------------------------- retention
    def purge_older_than(self, days: int) -> dict[str, int]:
        cutoff = fmt_ts(now_msk() - timedelta(days=max(days, 1)))
        out = {}
        for table in ("candles", "metrics", "decisions"):
            cur = self.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            out[table] = cur.rowcount
        self.execute("DELETE FROM sessions WHERE expires_at < ?", (fmt_ts(now_msk()),))
        return out
