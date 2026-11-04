"""Web Push (VAPID) — п. 5.

* VAPID-пара генерируется один раз и хранится в ``config/vapid_private.pem``.
* Публичный ключ (base64url, несжатая точка P-256) отдаётся фронтенду
  для ``pushManager.subscribe``.
* Подписка браузера (endpoint, p256dh, auth) хранится в ``push_subscriptions``
  под ключом пользователя.
* Отправка через ``pywebpush`` (синхронный requests) выполняется в потоке,
  чтобы не блокировать event loop. Подписки, на которые сервис отвечает
  404/410, удаляются.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .db import Database
from .timeutil import fmt_ts, now_msk

logger = logging.getLogger(__name__)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class PushService:
    def __init__(self, db: Database, key_path: Path, subject: str = "mailto:admin@localhost"):
        self.db = db
        self.key_path = Path(key_path)
        self.subject = subject
        self._vapid = None
        self.public_key: Optional[str] = None
        self.error: Optional[str] = None
        try:
            self._load_or_create()
        except Exception as exc:  # noqa: BLE001 — без pywebpush сайт работает, только без пушей
            self.error = f"VAPID недоступен: {exc}"
            logger.warning(self.error)

    @property
    def available(self) -> bool:
        return self._vapid is not None

    def _load_or_create(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid

        if self.key_path.exists():
            vapid = Vapid.from_pem(self.key_path.read_bytes())
        else:
            vapid = Vapid()
            vapid.generate_keys()
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(vapid.private_pem())
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
            logger.info("Сгенерирована VAPID-пара: %s", self.key_path)
        raw = vapid.public_key.public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
        self._vapid = vapid
        self.public_key = _b64url(raw)

    # ------------------------------------------------------------ подписки
    def subscribe(self, key_id: int, sub: dict[str, Any], user_agent: str = "") -> int:
        endpoint = str(sub.get("endpoint") or "")
        keys = sub.get("keys") or {}
        p256dh, auth = keys.get("p256dh") or sub.get("p256dh"), keys.get("auth") or sub.get("auth")
        if not endpoint.startswith("https://") or not p256dh or not auth:
            raise ValueError("Подписка должна содержать endpoint (https), p256dh и auth")
        self.db.execute(
            "INSERT INTO push_subscriptions (key_id, endpoint, p256dh, auth, user_agent, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET key_id = excluded.key_id, "
            "p256dh = excluded.p256dh, auth = excluded.auth, user_agent = excluded.user_agent",
            (key_id, endpoint, p256dh, auth, user_agent[:300], fmt_ts(now_msk())),
        )
        row = self.db.query_one("SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        return int(row["id"]) if row else 0

    def unsubscribe(self, key_id: int, endpoint: Optional[str] = None) -> int:
        if endpoint:
            cur = self.db.execute("DELETE FROM push_subscriptions WHERE key_id = ? AND endpoint = ?", (key_id, endpoint))
        else:
            cur = self.db.execute("DELETE FROM push_subscriptions WHERE key_id = ?", (key_id,))
        return cur.rowcount

    def subscriptions(self, key_id: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT p.id, p.key_id, p.endpoint, p.p256dh, p.auth, p.user_agent, p.created_at "
            "FROM push_subscriptions p JOIN access_keys k ON k.id = p.key_id WHERE p.key_id = ? AND k.active = 1",
            (key_id,),
        )

    # ------------------------------------------------------------- отправка
    def _send_one(self, sub: dict[str, Any], payload: str, ttl: int) -> tuple[bool, Optional[str]]:
        from pywebpush import WebPushException, webpush

        info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            webpush(info, payload, vapid_private_key=self._vapid, vapid_claims={"sub": self.subject},
                    ttl=ttl, timeout=15)
            return True, None
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                self.db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub["id"],))
                return False, f"подписка устарела ({status}), удалена"
            return False, f"{status}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def send_to_key(self, key_id: int, title: str, body: str, data: Optional[dict[str, Any]] = None,
                          ttl: int = 3600) -> dict[str, Any]:
        """Отправляет пуш на все устройства ключа. Возвращает {sent, failed, errors}."""
        subs = self.subscriptions(key_id)
        result: dict[str, Any] = {"sent": 0, "failed": 0, "devices": len(subs), "errors": []}
        if not self.available:
            result["errors"].append(self.error or "VAPID не настроен")
            return result
        payload = json.dumps({"title": title, "body": body, "data": data or {}, "ts": fmt_ts(now_msk())},
                             ensure_ascii=False, default=str)
        for sub in subs:
            ok, err = await asyncio.to_thread(self._send_one, sub, payload, ttl)
            if ok:
                result["sent"] += 1
            else:
                result["failed"] += 1
                result["errors"].append(err)
        if result["errors"]:
            logger.warning("Пуш ключу %s: %s", key_id, result["errors"])
        return result
