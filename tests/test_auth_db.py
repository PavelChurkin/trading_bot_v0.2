"""Ключи доступа (админ из файла + пользовательские active/inactive) и настройки."""

from __future__ import annotations

import pytest

from app import auth
from app import settings_schema as ss
from app.config import load_or_create_admin_key


def test_admin_key_file_created_once(tmp_path):
    path = tmp_path / "config" / "admin_key.txt"
    key, created = load_or_create_admin_key(path)
    assert created and key.startswith("admin-")
    again, created2 = load_or_create_admin_key(path)
    assert again == key and not created2


def test_admin_login_and_rotation(db):
    admin_id = auth.sync_admin_key(db, "admin-OLD")
    token, user = auth.login(db, "admin-OLD")
    assert user["role"] == "admin" and user["id"] == admin_id
    assert auth.user_by_session(db, token)["id"] == admin_id
    # Повторная синхронизация того же ключа — тот же id
    assert auth.sync_admin_key(db, "admin-OLD") == admin_id
    # Ключ в файле сменили: старый не пускает, старые сессии гаснут
    new_id = auth.sync_admin_key(db, "admin-NEW")
    assert new_id != admin_id
    assert auth.login(db, "admin-OLD") is None
    assert auth.user_by_session(db, token) is None
    assert auth.login(db, "admin-NEW")[1]["role"] == "admin"


def test_user_key_lifecycle(db):
    admin_id = auth.sync_admin_key(db, "admin-KEY")
    created = auth.create_user_key(db, "Трейдер")
    assert created["key"].startswith("tb-") and created["hint"] == "…" + created["key"][-4:]
    assert auth.login(db, "wrong-key") is None
    token, user = auth.login(db, created["key"])
    assert user["role"] == "user" and user["label"] == "Трейдер"
    assert sorted(auth.active_user_ids(db)) == sorted([admin_id, created["id"]])

    # Деактивация: сессия сразу недействительна, вход запрещён
    assert auth.set_key_active(db, created["id"], False)
    assert auth.user_by_session(db, token) is None
    assert auth.login(db, created["key"]) is None
    assert auth.active_user_ids(db) == [admin_id]
    # Активация обратно
    assert auth.set_key_active(db, created["id"], True)
    assert auth.login(db, created["key"]) is not None

    # Админский ключ нельзя отключить или удалить через API
    assert not auth.set_key_active(db, admin_id, False)
    assert not auth.delete_key(db, admin_id)

    keys = {k["id"]: k for k in auth.list_keys(db)}
    assert keys[created["id"]]["active"] == 1 and "key_hash" not in keys[created["id"]]

    assert auth.delete_key(db, created["id"])
    assert auth.login(db, created["key"]) is None
    assert created["id"] not in {k["id"] for k in auth.list_keys(db)}


def test_logout(db):
    auth.sync_admin_key(db, "admin-KEY")
    token, _ = auth.login(db, "admin-KEY")
    auth.logout(db, token)
    assert auth.user_by_session(db, token) is None
    assert auth.user_by_session(db, None) is None


def test_settings_effective_override_reset(db):
    user = auth.create_user_key(db, "u")
    uid = user["id"]
    assert db.effective_settings(uid)["volume_mult"] == ss.defaults()["volume_mult"]
    # Глобальная (админская) настройка
    db.save_settings("global", {"volume_mult": 2.0, "scan_interval_min": 30})
    assert db.effective_settings(uid)["volume_mult"] == 2.0
    # Пользовательское переопределение
    db.save_settings(f"user:{uid}", {"volume_mult": 3.5, "cooldown_min": 5})
    eff = db.effective_settings(uid)
    assert eff["volume_mult"] == 3.5 and eff["cooldown_min"] == 5
    # Другие пользователи не затронуты
    assert db.effective_settings(None)["volume_mult"] == 2.0
    # Админские ключи пользователь переопределить не может, даже если запись попала в БД
    db.save_settings(f"user:{uid}", {"scan_interval_min": 15})
    assert db.effective_settings(uid)["scan_interval_min"] == 30
    db.reset_user_settings(uid, ["volume_mult"])
    eff = db.effective_settings(uid)
    assert eff["volume_mult"] == 2.0 and eff["cooldown_min"] == 5
    db.reset_user_settings(uid)
    assert db.effective_settings(uid)["cooldown_min"] == ss.defaults()["cooldown_min"]


def test_state_roundtrip(db):
    assert db.get_state("x", "def") == "def"
    db.set_state("x", {"a": [1, 2]})
    assert db.get_state("x") == {"a": [1, 2]}


@pytest.mark.parametrize("key,value,expected", [
    ("volume_mult", "2.5", 2.5),
    ("cooldown_min", "10", 10),
    ("level_break_enabled", "да", True),
    ("level_break_enabled", "0", False),
    ("quiet_from", "7:5", "07:05"),
    ("quiet_from", "", ""),
    ("alert_types", "up_pulse, down_pulse", ["up_pulse", "down_pulse"]),
])
def test_coerce_ok(key, value, expected):
    assert ss.coerce(key, value) == expected


@pytest.mark.parametrize("key,value", [
    ("unknown_key", 1),
    ("volume_mult", "abc"),
    ("volume_mult", 0),          # ниже минимума
    ("scan_interval_min", 5),    # цикл не чаще 15 минут (п. 1)
    ("quiet_from", "25:00"),
    ("alert_types", ["nope"]),
    ("charts", ["pie"]),
])
def test_coerce_rejects(key, value):
    with pytest.raises(ValueError):
        ss.coerce(key, value)


def test_schema_for_ui_scopes():
    admin = {s["key"] for s in ss.schema_for_ui(True)}
    user = {s["key"] for s in ss.schema_for_ui(False)}
    assert "scan_interval_min" in admin and "scan_interval_min" not in user
    assert "volume_mult" in user and user < admin
