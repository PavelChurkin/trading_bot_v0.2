"""Пути и секреты.

* Ключ-пароль админа лежит в отдельном файле ``config/admin_key.txt``.
  Если файла нет, он создаётся при первом запуске со случайным ключом,
  который печатается в консоль. Ключ можно заменить вручную — достаточно
  перезапуска.
* VAPID-пара генерируется один раз и лежит в ``config/vapid_private.pem``.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class AppConfig:
    config_dir: Path
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    vapid_subject: str = "mailto:admin@localhost"
    iss_base_url: str = "https://iss.moex.com/iss"
    requests_per_second: float = 1.0
    scanner_enabled: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trading_bot.sqlite3"

    @property
    def admin_key_path(self) -> Path:
        return self.config_dir / "admin_key.txt"

    @property
    def vapid_key_path(self) -> Path:
        return self.config_dir / "vapid_private.pem"

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            config_dir=Path(os.environ.get("TB_CONFIG_DIR", ROOT_DIR / "config")),
            data_dir=Path(os.environ.get("TB_DATA_DIR", ROOT_DIR / "data")),
            host=os.environ.get("TB_HOST", "127.0.0.1"),
            port=int(os.environ.get("TB_PORT", "8000")),
            vapid_subject=os.environ.get("TB_VAPID_SUBJECT", "mailto:admin@localhost"),
            iss_base_url=os.environ.get("TB_ISS_URL", "https://iss.moex.com/iss"),
            requests_per_second=float(os.environ.get("TB_RPS", "1.0")),
            scanner_enabled=os.environ.get("TB_SCANNER", "1") not in {"0", "false", "no"},
        )

    def ensure_dirs(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


def generate_key(prefix: str = "tb") -> str:
    """Человекочитаемый случайный ключ: tb-XXXX-XXXX-XXXX-XXXX (~80 бит энтропии)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return f"{prefix}-" + "-".join(groups)


def load_or_create_admin_key(path: Path) -> tuple[str, bool]:
    """Возвращает (ключ, создан_ли_сейчас)."""
    if path.exists():
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            return lines[0], False
    key = generate_key("admin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows
        pass
    return key, True
