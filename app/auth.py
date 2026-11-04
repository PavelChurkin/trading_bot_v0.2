"""Авторизация по ключам-паролям.

* Ключ админа задаётся в файле (см. :mod:`app.config`) и при старте
  синхронизируется в таблицу ``access_keys`` с ролью ``admin``.
* Админ генерирует ключи пользователей. В БД хранится только SHA-256 хэш
  ключа и короткая подсказка (последние символы) — сам ключ показывается
  один раз при генерации.
* Ключ можно деактивировать/активировать/удалить; деактивация сразу
  закрывает все сессии этого ключа.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from .config import generate_key
from .db import Database
from .timeutil import fmt_ts, now_msk

SESSION_DAYS = 30


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def key_hint(key: str) -> str:
    return f"…{key.strip()[-4:]}"


def sync_admin_key(db: Database, admin_key: str) -> int:
    """Гарантирует, что ключ из файла — активный ключ админа. Возвращает его id."""
    h = hash_secret(admin_key)
    row = db.query_one("SELECT id FROM access_keys WHERE key_hash = ?", (h,))
    if row:
        db.execute("UPDATE access_keys SET role = 'admin', active = 1 WHERE id = ?", (row["id"],))
        admin_id = row["id"]
    else:
        # Ключ в файле сменили: старый админский ключ переводим в неактивные.
        db.execute("UPDATE access_keys SET active = 0 WHERE role = 'admin'")
        cur = db.execute(
            "INSERT INTO access_keys (key_hash, key_hint, label, role, active, created_at) VALUES (?,?,?,?,1,?)",
            (h, key_hint(admin_key), "Администратор (из файла)", "admin", fmt_ts(now_msk())),
        )
        admin_id = int(cur.lastrowid)
    # Сессии старых админских ключей больше не действуют.
    db.execute(
        "DELETE FROM sessions WHERE key_id IN (SELECT id FROM access_keys WHERE role = 'admin' AND active = 0)"
    )
    return admin_id


def create_user_key(db: Database, label: str = "") -> dict[str, Any]:
    key = generate_key("tb")
    cur = db.execute(
        "INSERT INTO access_keys (key_hash, key_hint, label, role, active, created_at) VALUES (?,?,?,?,1,?)",
        (hash_secret(key), key_hint(key), label.strip()[:100], "user", fmt_ts(now_msk())),
    )
    return {"id": int(cur.lastrowid), "key": key, "label": label, "hint": key_hint(key)}


def list_keys(db: Database) -> list[dict[str, Any]]:
    return db.query(
        "SELECT k.id, k.key_hint AS hint, k.label, k.role, k.active, k.created_at, k.last_used_at, "
        "(SELECT COUNT(*) FROM push_subscriptions p WHERE p.key_id = k.id) AS devices "
        "FROM access_keys k ORDER BY k.id"
    )


def set_key_active(db: Database, key_id: int, active: bool) -> bool:
    row = db.query_one("SELECT role FROM access_keys WHERE id = ?", (key_id,))
    if not row or row["role"] == "admin":
        return False
    db.execute("UPDATE access_keys SET active = ? WHERE id = ?", (1 if active else 0, key_id))
    if not active:
        db.execute("DELETE FROM sessions WHERE key_id = ?", (key_id,))
    return True


def delete_key(db: Database, key_id: int) -> bool:
    row = db.query_one("SELECT role FROM access_keys WHERE id = ?", (key_id,))
    if not row or row["role"] == "admin":
        return False
    with db.transaction() as conn:
        for table in ("sessions", "push_subscriptions", "favorites", "paper_trades"):
            conn.execute(f"DELETE FROM {table} WHERE key_id = ?", (key_id,))
        conn.execute("DELETE FROM settings WHERE scope = ?", (f"user:{key_id}",))
        conn.execute("DELETE FROM alerts WHERE key_id = ?", (key_id,))
        conn.execute("DELETE FROM daily_reports WHERE key_id = ?", (key_id,))
        conn.execute("DELETE FROM access_keys WHERE id = ?", (key_id,))
    return True


def login(db: Database, key: str) -> tuple[str, dict[str, Any]] | None:
    """Проверяет ключ, создаёт сессию. Возвращает (session_token, user) или None."""
    row = db.query_one(
        "SELECT id, role, label, key_hint, active FROM access_keys WHERE key_hash = ?", (hash_secret(key or ""),)
    )
    if not row or not row["active"]:
        return None
    token = secrets.token_urlsafe(32)
    now = now_msk()
    db.execute(
        "INSERT INTO sessions (token_hash, key_id, created_at, expires_at) VALUES (?,?,?,?)",
        (hash_secret(token), row["id"], fmt_ts(now), fmt_ts(now + timedelta(days=SESSION_DAYS))),
    )
    db.execute("UPDATE access_keys SET last_used_at = ? WHERE id = ?", (fmt_ts(now), row["id"]))
    return token, {"id": row["id"], "role": row["role"], "label": row["label"], "hint": row["key_hint"]}


def user_by_session(db: Database, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = db.query_one(
        "SELECT k.id, k.role, k.label, k.key_hint AS hint, s.expires_at FROM sessions s "
        "JOIN access_keys k ON k.id = s.key_id WHERE s.token_hash = ? AND k.active = 1",
        (hash_secret(token),),
    )
    if not row or row["expires_at"] < fmt_ts(now_msk()):
        return None
    return {"id": row["id"], "role": row["role"], "label": row["label"], "hint": row["hint"]}


def logout(db: Database, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_secret(token),))


def active_user_ids(db: Database) -> list[int]:
    return [r["id"] for r in db.query("SELECT id FROM access_keys WHERE active = 1 ORDER BY id")]
