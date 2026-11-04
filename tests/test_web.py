"""HTTP API дашборда (FastAPI TestClient): вход по ключу, роли, настройки,
управление ключами, пуши (VAPID, подписка, тест с таймером), таблица,
карточка тикера, «почему молчит», paper trading, раздача PWA."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import analysis as an
from app import auth
from app import settings_schema as ss
from app.push import PushService
from app.scanner import Scanner
from app.scheduler import RequestScheduler, TokenBucket
from app.web import AppContext, create_app
from conftest import flat_minutes, pulse_day
from test_scanner import TODAY, FakeIss

ADMIN_KEY = "admin-TEST-KEY"


@pytest.fixture
def env(db, tmp_path):
    auth.sync_admin_key(db, ADMIN_KEY)
    iss = FakeIss()
    sched = RequestScheduler(TokenBucket(1000, 50))
    push = PushService(db, tmp_path / "vapid.pem")
    scanner = Scanner(db, iss, sched, push)
    scanner.apply_board([
        {"SECID": t, "SHORTNAME": t.title(), "SECNAME": f"ПАО {t}", "LOTSIZE": 1, "PREVPRICE": c[0]["open"]}
        for t, c in iss.minute.items()],
        [{"SECID": t, "LAST": c[-1]["close"], "OPEN": c[0]["open"], "HIGH": c[-1]["close"], "LOW": c[0]["low"],
          "VOLTODAY": 1e6, "VALTODAY": 1e8, "LASTTOPREVPRICE": 0.6} for t, c in iss.minute.items()])
    s = ss.defaults()
    for t, candles in (("PULS", pulse_day(TODAY)), ("CALM", flat_minutes(f"{TODAY} 10:00:00", 315, 50.0))):
        db.upsert_candles(t, candles)
        snap = an.compute_snapshot(t, candles, s, scanner.securities[t])
        db.save_snapshot(t, 15, snap["ts"], snap)
        db.add_metrics([(t, snap["ts"], "amplitude_atr", snap["amplitude_atr"])])
    stopped = []
    ctx = AppContext(db=db, scheduler=sched, push=push, scanner=scanner, stop_callback=lambda: stopped.append(1))
    with TestClient(create_app(ctx)) as client:
        yield {"client": client, "db": db, "push": push, "scanner": scanner, "stopped": stopped}


def login(client: TestClient, key: str) -> TestClient:
    r = client.post("/api/login", json={"key": key})
    assert r.status_code == 200, r.text
    return client


def test_login_required_and_bad_key(env):
    c = env["client"]
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/login", json={"key": "nope"}).status_code == 401
    login(c, ADMIN_KEY)
    me = c.get("/api/me").json()
    assert me["is_admin"] and me["push_available"] and me["vapid_public_key"]
    c.post("/api/logout")
    assert c.get("/api/me").status_code == 401


def test_user_key_management_and_roles(env):
    c = env["client"]
    login(c, ADMIN_KEY)
    created = c.post("/api/admin/keys", json={"label": "Трейдер"}).json()
    keys = c.get("/api/admin/keys").json()["keys"]
    assert any(k["id"] == created["id"] and k["active"] == 1 for k in keys)

    with TestClient(c.app) as uc:
        login(uc, created["key"])
        assert uc.get("/api/me").json()["is_admin"] is False
        assert uc.get("/api/admin/keys").status_code == 403
        assert uc.put("/api/admin/settings", json={"values": {"scan_interval_min": 30}}).status_code == 403
        assert uc.post("/api/admin/stop").status_code == 403
        # Деактивация админом: сессия пользователя гаснет, вход запрещён
        assert c.patch(f"/api/admin/keys/{created['id']}", json={"active": False}).status_code == 200
        assert uc.get("/api/me").status_code == 401
        assert uc.post("/api/login", json={"key": created["key"]}).status_code == 401
        # Активация обратно
        c.patch(f"/api/admin/keys/{created['id']}", json={"active": True})
        login(uc, created["key"])
    # Ключ админа отключить нельзя
    admin_id = next(k["id"] for k in keys if k["role"] == "admin")
    assert c.patch(f"/api/admin/keys/{admin_id}", json={"active": False}).status_code == 400
    assert c.delete(f"/api/admin/keys/{created['id']}").status_code == 200


def test_settings_validation(env):
    c = env["client"]
    login(c, ADMIN_KEY)
    created = c.post("/api/admin/keys", json={"label": "u"}).json()
    with TestClient(c.app) as uc:
        login(uc, created["key"])
        schema = {s["key"] for s in uc.get("/api/settings").json()["schema"]}
        assert "volume_mult" in schema and "scan_interval_min" not in schema
        r = uc.put("/api/settings", json={"values": {"volume_mult": 2.5, "cooldown_min": "10"}})
        assert r.status_code == 200 and r.json()["values"]["volume_mult"] == 2.5
        assert r.json()["overrides"] == {"volume_mult": 2.5, "cooldown_min": 10}
        bad = uc.put("/api/settings", json={"values": {"volume_mult": "x", "scan_interval_min": 30}})
        assert bad.status_code == 422
        errors = bad.json()["detail"]["errors"]
        assert set(errors) == {"volume_mult", "scan_interval_min"}
        # Сброс к настройкам админа
        assert uc.delete("/api/settings?key=volume_mult").json()["values"]["volume_mult"] == 1.5
    # Админ меняет глобальные; цикл чаще 15 мин запрещён
    assert c.put("/api/admin/settings", json={"values": {"scan_interval_min": 10}}).status_code == 422
    assert c.put("/api/admin/settings", json={"values": {"scan_interval_min": 30}}).json()["global"][
        "scan_interval_min"] == 30


def test_push_subscribe_and_test_timer(env, monkeypatch):
    c, push, db = env["client"], env["push"], env["db"]
    login(c, ADMIN_KEY)
    sub = {"endpoint": "https://push.example.invalid/abc", "keys": {"p256dh": "BPk", "auth": "xyz"}}
    assert c.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://x"}}).status_code == 422
    assert c.post("/api/push/subscribe", json={"subscription": sub}).json()["devices"] == 1

    sent = []

    async def fake_send(key_id, title, body, data=None, **_):
        sent.append((key_id, title, time.monotonic()))
        return {"sent": 1, "failed": 0, "devices": 1, "errors": []}

    monkeypatch.setattr(push, "send_to_key", fake_send)
    t0 = time.monotonic()
    r = c.post("/api/push/test", json={}).json()
    assert r["scheduled_in_sec"] == 1.0  # таймер по умолчанию (п. 5)
    assert r["devices"] == 1
    me = c.get("/api/me").json()["user"]["id"]
    deadline = time.monotonic() + 5
    while not sent and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sent and sent[0][0] == me and sent[0][2] - t0 >= 0.9
    for _ in range(40):
        if db.get_state(f"test_push:{me}"):
            break
        time.sleep(0.05)
    assert db.get_state(f"test_push:{me}")["sent"] == 1

    r = c.post("/api/push/test", json={"delay_sec": 0, "all": True}).json()
    assert r["scheduled_in_sec"] == 0 and me in r["targets"]

    preview = c.post("/api/push/preview", json={"push_title_template": "{ticker}!"}).json()
    assert preview["title"] == "SBER!"
    assert c.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]}).json()["devices"] == 0


def test_table_ticker_silent_and_metrics(env):
    c = env["client"]
    login(c, ADMIN_KEY)
    table = c.get("/api/table").json()
    rows = {r["ticker"]: r for r in table["rows"]}
    assert set(rows) == {"PULS", "CALM"}
    assert rows["PULS"]["triggered"] is True and rows["CALM"]["triggered"] is False
    assert table["rows"][0]["ticker"] == "PULS"  # ближе всех к порогу — сверху
    assert "amplitude_atr" in table["columns"]

    card = c.get("/api/ticker/puls").json()
    assert card["ticker"] == "PULS" and card["evaluation"]["triggered"]
    assert card["series"]["candles"] and "price" in card["charts"] and "rsi" in card["charts"]
    assert c.get("/api/ticker/NOPE").status_code == 404

    # Свой алерт и тот же алерт в общем журнале — в карточке один раз, свой
    db, me = env["db"], c.get("/api/me").json()["user"]["id"]
    for kid in (None, me):
        db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action) VALUES "
                   "(?, 'PULS', '2026-01-05 12:00:00', 'up_pulse', 70, 'тест', 'enter')", (kid,))
    db.execute("INSERT INTO alerts (key_id, ticker, ts, direction, score, reason, action) VALUES "
               "(NULL, 'PULS', '2026-01-05 12:15:00', 'down_pulse', 60, 'только журнал', 'selloff')")
    card_alerts = c.get("/api/ticker/PULS").json()["alerts"]
    assert [(a["ts"], a["direction"]) for a in card_alerts] == [
        ("2026-01-05 12:15:00", "down_pulse"), ("2026-01-05 12:00:00", "up_pulse")]

    silent = c.get("/api/silent").json()
    assert [r["ticker"] for r in silent["triggered"]] == ["PULS"]
    calm = silent["silent"][0]
    assert calm["ticker"] == "CALM" and calm["failed"]

    assert c.get("/api/search?q=pul").json()["results"][0]["ticker"] == "PULS"
    assert c.post("/api/favorites", json={"ticker": "PULS"}).json()["tickers"] == ["PULS"]
    metric = c.get("/api/metrics/amplitude_atr?hours=48").json()
    assert {t["ticker"] for t in metric["tickers"]} == {"PULS", "CALM"}


def test_paper_trading_api(env):
    c = env["client"]
    login(c, ADMIN_KEY)
    r = c.post("/api/paper", json={"ticker": "PULS", "side": "long"}).json()
    trade = next(t for t in r["trades"] if t["id"] == r["id"])
    assert trade["status"] == "open" and trade["stop"] < trade["entry_price"] < trade["take"]
    assert c.post("/api/paper", json={"ticker": "PULS", "side": "up"}).status_code == 422
    closed = c.post(f"/api/paper/{r['id']}/close", json={"price": trade["entry_price"] * 1.01}).json()
    assert closed["stats"]["closed"] == 1 and closed["stats"]["wins"] == 1


def test_admin_stop_and_scan_now(env):
    c = env["client"]
    login(c, ADMIN_KEY)
    assert c.post("/api/admin/scan-now").json()["ok"]
    assert c.post("/api/admin/stop").json()["ok"]
    deadline = time.monotonic() + 3
    while not env["stopped"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert env["stopped"]


def test_pwa_assets_served(env):
    c = env["client"]
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert sw.headers["service-worker-allowed"] == "/"
    assert "push" in sw.text
    manifest = c.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.json()["display"] == "standalone"
    index = c.get("/")
    assert index.status_code == 200 and "manifest.webmanifest" in index.text
    js = c.get("/static/app.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert c.get("/static/app.js", headers={"If-None-Match": js.headers["etag"]}).status_code == 304
