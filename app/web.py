"""FastAPI: REST API дашборда + раздача SPA/PWA.

Авторизация — только ключом (п. «Авторизация»): ``POST /api/login`` с ключом
создаёт сессию (httpOnly cookie ``tb_session``; для скриптов можно передать
``Authorization: Bearer <session>``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import alerts as al
from . import analysis as an
from . import auth
from . import charts
from . import paper
from . import settings_schema as ss
from .db import Database
from .push import PushService
from .scanner import BLUE_CHIPS, Scanner
from .scheduler import P0, RequestScheduler
from .timeutil import fmt_ts, now_msk

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "tb_session"


@dataclass
class AppContext:
    db: Database
    scheduler: RequestScheduler
    push: PushService
    scanner: Scanner
    stop_callback: Any = None  # кнопка «Остановить» в админке (то же, что «0» в консоли)
    background: set = field(default_factory=set)


def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(title="Trading bot dashboard", version="0.2.0", docs_url="/api/docs", redoc_url=None)
    db, scanner, push = ctx.db, ctx.scanner, ctx.push

    # ================================================================ auth
    def session_token(request: Request) -> Optional[str]:
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return request.cookies.get(COOKIE)

    def current_user(request: Request) -> dict[str, Any]:
        user = auth.user_by_session(db, session_token(request))
        if not user:
            raise HTTPException(401, "Нужен вход по ключу")
        return user

    def admin_user(user: dict = Depends(current_user)) -> dict[str, Any]:
        if user["role"] != "admin":
            raise HTTPException(403, "Только для администратора")
        return user

    def spawn(coro) -> None:
        task = asyncio.get_event_loop().create_task(coro)
        ctx.background.add(task)
        task.add_done_callback(ctx.background.discard)

    @app.post("/api/login")
    async def api_login(body: dict, response: Response):
        res = auth.login(db, str(body.get("key") or ""))
        if not res:
            raise HTTPException(401, "Ключ не найден или деактивирован")
        token, user = res
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=auth.SESSION_DAYS * 86400,
                            secure=bool(body.get("secure")))
        return {"user": user}

    @app.post("/api/logout")
    async def api_logout(request: Request, response: Response):
        auth.logout(db, session_token(request))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    async def api_me(user: dict = Depends(current_user)):
        return {"user": user, "is_admin": user["role"] == "admin", "vapid_public_key": push.public_key,
                "push_available": push.available, "push_error": push.error,
                "devices": len(push.subscriptions(user["id"])), "away_since": scanner.away_since(user["id"]),
                "disk_paused": db.get_state("disk_paused")}

    @app.post("/api/away")
    async def api_away(body: dict, user: dict = Depends(current_user)):
        """Режим «Отошёл» (issue #5): пуши замораживаются; при возвращении
        досылаются сигналы двух последних сканирований (≤ 10 + сводный)."""
        return await scanner.set_away(user["id"], bool(body.get("on")))

    # ============================================================== статус
    @app.get("/api/status")
    async def api_status(user: dict = Depends(current_user)):
        return {"now": fmt_ts(now_msk()), "cycle": scanner.cycle_info, "cycle_running": scanner.cycle_running,
                "index": scanner.index, "tickers": sum(1 for s in scanner.securities.values() if s.get("active")),
                "scheduler": {"queue": ctx.scheduler.queue_lengths(), "current": ctx.scheduler.current},
                "orderbook_available": scanner.iss.orderbook_available, "started_at": fmt_ts(scanner.started_at)}

    @app.get("/api/scheduler")
    async def api_scheduler(user: dict = Depends(current_user)):
        m = ctx.scheduler.metrics()
        m["cache"] = {"items": len(scanner.iss.cache), "hits": scanner.iss.cache.hits,
                      "misses": scanner.iss.cache.misses}
        m["orderbook_available"] = scanner.iss.orderbook_available
        m["recent_jobs"] = [{"priority": f"P{p}", "name": n} for p, n in list(ctx.scheduler.order_log)[-30:]][::-1]
        return m

    # ============================================================ настройки
    @app.get("/api/settings")
    async def api_settings(user: dict = Depends(current_user)):
        is_admin = user["role"] == "admin"
        return {"schema": ss.schema_for_ui(is_admin), "values": db.effective_settings(user["id"]),
                "overrides": db.user_overrides(user["id"]), "global": db.global_settings() if is_admin else None}

    def _coerce_all(values: dict[str, Any], allow_admin: bool) -> dict[str, Any]:
        out, errors = {}, {}
        for k, v in (values or {}).items():
            spec = ss.SETTINGS_BY_KEY.get(k)
            if spec is None:
                errors[k] = "неизвестная настройка"
                continue
            if spec.scope == "admin" and not allow_admin:
                errors[k] = "только администратор"
                continue
            try:
                out[k] = ss.coerce(k, v)
            except (ValueError, TypeError) as exc:
                errors[k] = str(exc)
        if errors:
            raise HTTPException(422, {"errors": errors})
        return out

    @app.put("/api/settings")
    async def api_save_settings(body: dict, user: dict = Depends(current_user)):
        values = _coerce_all(body.get("values") or {}, allow_admin=False)
        db.save_settings(f"user:{user['id']}", values)
        return {"values": db.effective_settings(user["id"]), "overrides": db.user_overrides(user["id"])}

    @app.delete("/api/settings")
    async def api_reset_settings(key: Optional[str] = None, user: dict = Depends(current_user)):
        db.reset_user_settings(user["id"], [key] if key else None)
        return {"values": db.effective_settings(user["id"]), "overrides": db.user_overrides(user["id"])}

    @app.put("/api/admin/settings")
    async def api_admin_settings(body: dict, user: dict = Depends(admin_user)):
        values = _coerce_all(body.get("values") or {}, allow_admin=True)
        db.save_settings("global", values)
        if body.get("scan_now"):
            scanner.request_cycle()
        return {"global": db.global_settings()}

    # ================================================================ ключи
    @app.get("/api/admin/keys")
    async def api_keys(user: dict = Depends(admin_user)):
        return {"keys": auth.list_keys(db)}

    @app.post("/api/admin/keys")
    async def api_create_key(body: dict, user: dict = Depends(admin_user)):
        return auth.create_user_key(db, str(body.get("label") or ""))

    @app.patch("/api/admin/keys/{key_id}")
    async def api_patch_key(key_id: int, body: dict, user: dict = Depends(admin_user)):
        if not auth.set_key_active(db, key_id, bool(body.get("active"))):
            raise HTTPException(400, "Ключ не найден или это ключ администратора")
        return {"keys": auth.list_keys(db)}

    @app.delete("/api/admin/keys/{key_id}")
    async def api_delete_key(key_id: int, user: dict = Depends(admin_user)):
        if not auth.delete_key(db, key_id):
            raise HTTPException(400, "Ключ не найден или это ключ администратора")
        return {"keys": auth.list_keys(db)}

    @app.post("/api/admin/scan-now")
    async def api_scan_now(user: dict = Depends(admin_user)):
        scanner.request_cycle()
        return {"ok": True, "cycle_running": scanner.cycle_running}

    @app.post("/api/admin/purge")
    async def api_purge(user: dict = Depends(admin_user)):
        """«Освободить память»: удалить данные старше срока хранения и сжать
        базу (VACUUM), чтобы место вернулось на диск. Размер до/после."""
        days = int(db.global_settings()["retention_days"])
        before = db.size_bytes()
        deleted = db.purge_older_than(days)
        await asyncio.to_thread(db.vacuum)
        return {"ok": True, "days": days, "deleted": deleted, "before_bytes": before, "after_bytes": db.size_bytes(),
                "storage": scanner.storage()}

    @app.get("/api/admin/storage")
    async def api_storage(user: dict = Depends(admin_user)):
        """Размер базы и свободное место на диске."""
        return scanner.storage()

    @app.post("/api/admin/storage/check")
    async def api_storage_check(user: dict = Depends(admin_user)):
        """«Проверить память»: места достаточно — сканер снимается с паузы."""
        return scanner.check_storage()

    @app.post("/api/admin/stop")
    async def api_stop(user: dict = Depends(admin_user)):
        if ctx.stop_callback is None:
            raise HTTPException(400, "Остановка недоступна")
        asyncio.get_event_loop().call_later(0.5, ctx.stop_callback)
        return {"ok": True}

    # ================================================================= пуши
    @app.get("/api/push/key")
    async def api_push_key():
        return {"public_key": push.public_key, "available": push.available, "error": push.error}

    @app.post("/api/push/subscribe")
    async def api_subscribe(body: dict, request: Request, user: dict = Depends(current_user)):
        try:
            sid = push.subscribe(user["id"], body.get("subscription") or body, request.headers.get("user-agent", ""))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": sid, "devices": len(push.subscriptions(user["id"]))}

    @app.post("/api/push/unsubscribe")
    async def api_unsubscribe(body: dict, user: dict = Depends(current_user)):
        n = push.unsubscribe(user["id"], body.get("endpoint"))
        return {"removed": n, "devices": len(push.subscriptions(user["id"]))}

    async def _delayed_push(key_ids: list[int], delay: float, title: str, text: str) -> None:
        await asyncio.sleep(delay)
        for kid in key_ids:
            res = await push.send_to_key(kid, title, text, {"url": "/#settings", "test": True})
            db.set_state(f"test_push:{kid}", {**res, "ts": fmt_ts(now_msk())})

    @app.post("/api/push/test")
    async def api_test_push(body: dict, user: dict = Depends(current_user)):
        """Тестовый пуш себе. Админ может указать ``key_id`` или ``all``.
        Таймер ``delay_sec`` (по умолчанию из настроек, 1 с) — чтобы успеть
        свернуть браузер/заблокировать телефон."""
        is_admin = user["role"] == "admin"
        delay = body.get("delay_sec")
        delay = float(db.global_settings()["test_push_delay_sec"] if delay in (None, "") else delay)
        delay = min(max(delay, 0.0), 3600.0)
        targets = [user["id"]]
        if is_admin and body.get("all"):
            targets = auth.active_user_ids(db)
        elif is_admin and body.get("key_id"):
            targets = [int(body["key_id"])]
        devices = sum(len(push.subscriptions(k)) for k in targets)
        title = str(body.get("title") or "Тестовое уведомление")[:200]
        text = str(body.get("body") or f"Пуш работает · {fmt_ts(now_msk())} МСК")[:600]
        spawn(_delayed_push(targets, delay, title, text))
        return {"scheduled_in_sec": delay, "targets": targets, "devices": devices,
                "push_available": push.available, "error": push.error}

    @app.post("/api/push/preview")
    async def api_push_preview(body: dict, user: dict = Depends(current_user)):
        """Предпросмотр формата пуша на примере последнего алерта."""
        s = db.effective_settings(user["id"])
        for k in ("push_title_template", "push_body_template"):
            if body.get(k) is not None:
                s[k] = str(body[k])
        row = db.query_one("SELECT * FROM alerts WHERE ticker != 'РЫНОК' ORDER BY id DESC LIMIT 1")
        sample = al.alert_from_row(row) if row else al.Alert(
            ticker="SBER", ts=fmt_ts(now_msk()), direction="up_pulse", score=72, amplitude_atr=2.1,
            volume_ratio=2.4, level_break=1, reason="SBER: амплитуда 2.10×ATR, объём 2.40× среднего, пробой max 20 свечей",
            impulse_age_min=15, action="enter", price=312.5).to_dict()
        return al.format_push(sample, s)

    # ============================================================== таблица
    def _row(ticker: str, snap: Optional[dict[str, Any]], s: dict[str, Any]) -> dict[str, Any]:
        sec = scanner.securities.get(ticker) or {}
        row = {"ticker": ticker, "name": sec.get("name"), "price": sec.get("last"), "sectype": sec.get("sectype"),
               "day_change_pct": sec.get("day_change_pct"), "volume_today": sec.get("volume_today"),
               "value_today": sec.get("value_today"), "daily_atr_pct": sec.get("daily_atr_pct"),
               "liquidity_score": sec.get("liquidity_score")}
        if snap:
            ev = al.evaluate(snap, s)
            row.update({k: snap.get(k) for k in ("amplitude_pct", "amplitude_atr", "atr", "volume", "volume_ratio",
                                                  "change_hour_pct", "level_break", "impulse_age_min",
                                                  "pullback_pct", "plate_score", "direction", "level_distance_atr",
                                                  "approach_intensity", "approach_speed_atr", "pump_peak_forecast")})
            row["price"] = snap.get("price") or row["price"]
            row["updated"] = snap.get("ts")
            row["threshold_closeness"] = ev.closeness
            row["triggered"] = ev.triggered
            row["late"] = (snap.get("impulse_age_min") or 0) > s["impulse_age_max_min"]
        return row

    @app.get("/api/table")
    async def api_table(user: dict = Depends(current_user)):
        s = db.effective_settings(user["id"])
        snaps = db.snapshots(int(s["candle_window_min"]))
        favs = {r["ticker"] for r in db.query("SELECT ticker FROM favorites WHERE key_id = ?", (user["id"],))}
        papers = _open_papers(user["id"])
        last = _last_actions(user["id"])
        rows = []
        for t, sec in scanner.securities.items():
            if not sec.get("active"):
                continue
            r = _row(t, snaps.get(t), s)
            r["favorite"] = t in favs
            r["paper"] = papers.get(t)
            if t in last:
                la = last[t]
                r["last_action"] = la["action"]
                r["last_action_label"] = al.ACTION_LABELS.get(la["action"], la["action"])
                r["last_action_ts"] = la["ts"]
            rows.append(r)
        rows.sort(key=lambda r: (-(r.get("threshold_closeness") or 0), r["ticker"]))
        return {"columns": s["dashboard_columns"], "labels": ss.DASHBOARD_COLUMNS, "kinds": ss.SECURITY_KINDS,
                "rows": rows,
                "window": s["candle_window_min"], "cycle": scanner.cycle_info}

    @app.get("/api/ticker/{ticker}")
    async def api_ticker(ticker: str, days: int = 7, user: dict = Depends(current_user)):
        ticker = ticker.upper()
        if ticker not in scanner.securities:
            raise HTTPException(404, "Тикер не найден")
        s = db.effective_settings(user["id"])
        w = int(s["candle_window_min"])
        snap = db.snapshot(ticker, w)
        series = await _ticker_series(ticker, w, max(1, min(days, 31)))
        # Свои алерты (с отметкой пуша) + общий журнал там, где своего нет — без дублей
        alerts_rows = db.query(
            "SELECT * FROM alerts a WHERE a.ticker = ? AND (a.key_id = ? OR (a.key_id IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM alerts m WHERE m.key_id = ? AND m.ticker = a.ticker AND m.ts = a.ts "
            "AND m.direction = a.direction))) ORDER BY a.ts DESC, a.id DESC LIMIT 30",
            (ticker, user["id"], user["id"]))
        last_detail = db.get_state(f"analysis:{ticker}")
        return {"ticker": ticker, "security": scanner.securities[ticker], "row": _row(ticker, snap, s),
                "snapshot": snap, "evaluation": _ev(al.evaluate(snap, s)) if snap else None,
                "series": series, "charts": s["charts"], "chart_labels": ss.CHARTS,
                "alerts": [al.alert_from_row(r) for r in alerts_rows], "analysis": last_detail,
                "favorite": bool(db.query_one("SELECT 1 FROM favorites WHERE key_id = ? AND ticker = ?",
                                              (user["id"], ticker))),
                "paper": _open_papers(user["id"]).get(ticker),
                "levels": scanner.pinbar_levels(ticker, fmt_ts(now_msk() - timedelta(days=max(1, min(days, 31)))))}

    def _last_actions(key_id: int) -> dict[str, dict[str, Any]]:
        """Рекомендованное действие из последнего алерта пользователя по тикеру
        (колонка «Действия», issue #5)."""
        rows = db.query("SELECT a.ticker, a.action, a.ts FROM alerts a JOIN (SELECT ticker, MAX(id) AS id FROM alerts "
                        "WHERE key_id = ? AND ticker != 'РЫНОК' AND action IS NOT NULL GROUP BY ticker) m "
                        "ON a.id = m.id", (key_id,))
        return {r["ticker"]: r for r in rows}

    def _open_papers(key_id: int) -> dict[str, dict[str, Any]]:
        """Последняя открытая paper-позиция по каждому тикеру пользователя."""
        out: dict[str, dict[str, Any]] = {}
        for r in db.query("SELECT * FROM paper_trades WHERE key_id = ? AND status = 'open' ORDER BY id", (key_id,)):
            out[r["ticker"]] = r
        return out

    async def _ticker_series(ticker: str, window: int, days: int) -> dict[str, Any]:
        """14 дней / месяц: если локальных минуток за период нет (срок хранения
        короче) — часовые свечи из ISS одним запросом на P0 (кэш ISS)."""
        if days < charts.LONG_RANGE_DAYS or charts.local_covers(db, ticker, days):
            return charts.ticker_series(db, ticker, window, days)
        frm = (now_msk() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            fut = ctx.scheduler.submit(P0, f"hourly {ticker}", lambda: scanner.iss.candles(ticker, 60, frm),
                                       dedup_key=f"hourly:{ticker}:{frm}")
            # shield: таймаут не отменяет общий (dedup) future — запрос дойдёт и закэшируется
            hourly = await asyncio.wait_for(asyncio.shield(fut), timeout=30)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Часовые свечи %s недоступны: %s", ticker, exc)
            out = charts.ticker_series(db, ticker, window, days)
            out["note"] = f"ISS недоступен ({exc}); показана локальная история"
            return out
        if not hourly:
            return charts.ticker_series(db, ticker, window, days)
        return charts.ticker_series(db, ticker, window, days, candles=hourly, source="iss_hourly")

    @app.get("/api/metrics/{metric}")
    async def api_metric(metric: str, hours: float = 24, user: dict = Depends(current_user)):
        return charts.metric_all_tickers(db, metric, max(0.5, min(hours, 24 * 14)))

    # ======================================================== P0 — кнопки
    def _remember(res: dict[str, Any]) -> dict[str, Any]:
        if res.get("ticker") and not res.get("error"):
            db.set_state(f"analysis:{res['ticker']}", res)
        return res

    @app.post("/api/analyze")
    async def api_analyze(body: dict, user: dict = Depends(current_user)):
        try:
            return _remember(await scanner.analyze(str(body.get("ticker") or ""), user["id"]))
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc

    @app.post("/api/analyze-many")
    async def api_analyze_many(body: dict, user: dict = Depends(current_user)):
        tickers = body.get("tickers") or []
        if isinstance(tickers, str):
            tickers = tickers.replace(";", ",").replace(" ", ",").split(",")
        if body.get("favorites"):
            tickers = [r["ticker"] for r in db.query("SELECT ticker FROM favorites WHERE key_id = ?", (user["id"],))]
        tickers = [t.strip().upper() for t in tickers if t.strip()][:30]
        unknown = [t for t in tickers if t not in scanner.securities]
        tickers = [t for t in tickers if t in scanner.securities]
        res = [_remember(r) for r in await scanner.analyze_many(tickers, user["id"])]
        return {"results": res, "unknown": unknown}

    @app.post("/api/top10")
    async def api_top10(user: dict = Depends(current_user)):
        """Топ-10 голубых фишек (бывший пункт меню 5): группы BUY/SELL/HOLD по силе."""
        res = [_remember(r) for r in await scanner.analyze_many(BLUE_CHIPS, user["id"])]
        groups: dict[str, list[dict[str, Any]]] = {k: [] for k in an.VERDICT_LABELS}
        for r in res:
            if r.get("verdict"):
                groups[r["verdict"]["verdict"]].append(r)
        for g in groups.values():
            g.sort(key=lambda r: -r["verdict"]["strength"])
        return {"groups": groups, "labels": an.VERDICT_LABELS, "errors": [r for r in res if r.get("error")]}

    @app.post("/api/pulsating")
    async def api_pulsating(body: dict | None = None, user: dict = Depends(current_user)):
        """Пульсирующие акции (бывший пункт меню 6): амплитуда окна 1–5%,
        топ по амплитуде, детальный P0-анализ топ-3, индекс IMOEX."""
        body = body or {}
        s = db.effective_settings(user["id"])
        lo, hi = float(body.get("min_pct", 1.0)), float(body.get("max_pct", 5.0))
        snaps = db.snapshots(int(s["candle_window_min"]))
        rows = [_row(t, snap, s) for t, snap in snaps.items()
                if lo <= (snap.get("amplitude_pct") or 0) <= hi and scanner.securities.get(t, {}).get("active")]
        rows.sort(key=lambda r: -(r.get("amplitude_pct") or 0))
        rows = rows[: int(body.get("limit", 20))]
        detail = [_remember(r) for r in await scanner.analyze_many([r["ticker"] for r in rows[:3]], user["id"])]
        return {"rows": rows, "detail": detail, "index": scanner.index, "range": [lo, hi]}

    @app.get("/api/search")
    async def api_search(q: str = "", user: dict = Depends(current_user)):
        q = q.strip().upper()
        if not q:
            return {"results": []}
        out = []
        for t, sec in scanner.securities.items():
            name = (sec.get("name") or "").upper() + " " + (sec.get("full_name") or "").upper()
            if q in t or q in name:
                out.append({"ticker": t, "name": sec.get("name"), "full_name": sec.get("full_name"),
                            "price": sec.get("last"), "day_change_pct": sec.get("day_change_pct"),
                            "active": sec.get("active")})
        out.sort(key=lambda r: (not r["ticker"].startswith(q), r["ticker"]))
        return {"results": out[:30]}

    # ============================================================ избранное
    @app.get("/api/favorites")
    async def api_favorites(user: dict = Depends(current_user)):
        rows = db.query("SELECT ticker FROM favorites WHERE key_id = ? ORDER BY ticker", (user["id"],))
        return {"tickers": [r["ticker"] for r in rows]}

    @app.post("/api/favorites")
    async def api_add_favorite(body: dict, user: dict = Depends(current_user)):
        t = str(body.get("ticker") or "").upper()
        if t not in scanner.securities:
            raise HTTPException(404, "Тикер не найден")
        db.execute("INSERT OR IGNORE INTO favorites (key_id, ticker) VALUES (?, ?)", (user["id"], t))
        return await api_favorites(user)

    @app.delete("/api/favorites/{ticker}")
    async def api_del_favorite(ticker: str, user: dict = Depends(current_user)):
        db.execute("DELETE FROM favorites WHERE key_id = ? AND ticker = ?", (user["id"], ticker.upper()))
        return await api_favorites(user)

    # =============================================================== алерты
    @app.get("/api/alerts")
    async def api_alerts(scope: str = "mine", ticker: Optional[str] = None, direction: Optional[str] = None,
                         day: Optional[str] = None, limit: int = 200, fav: bool = False, kind: Optional[str] = None,
                         action: Optional[str] = None, sort: str = "time", triggered: bool = False,
                         time: Optional[str] = None, user: dict = Depends(current_user)):
        """История сигналов (п. 9). ``scope=mine`` — алерты по моим порогам
        (с отметкой отправлен/подавлен), ``scope=all`` — общий журнал сканера.

        Фильтры issue #3: ``fav`` — только избранное, ``kind`` — тип бумаги
        (share/fund/receipt/bond), ``action`` — действие, ``triggered`` — только
        прошедшие анти-спам (без подавленных); сортировка ``sort``: time,
        price_asc, price_desc, value_desc (оборот дня)."""
        where, params = [], []
        if scope == "all":
            where.append("a.key_id IS NULL")
        else:
            where.append("a.key_id = ?")
            params.append(user["id"])
        if ticker:
            where.append("a.ticker = ?")
            params.append(ticker.upper())
        if direction:
            where.append("a.direction = ?")
            params.append(direction)
        if action:
            where.append("a.action = ?")
            params.append(action)
        if day:
            where.append("substr(a.ts,1,10) = ?")
            params.append(day)
        if time:
            # Фильтр по времени (issue #5): сигналы начиная с HH:MM выбранного дня (или сегодня)
            if not re.fullmatch(r"\d{1,2}:\d{2}", time):
                raise HTTPException(422, "Время в формате ЧЧ:ММ")
            hh, mm = time.split(":")
            where.append("a.ts >= ?")
            params.append(f"{day or now_msk().strftime('%Y-%m-%d')} {int(hh):02d}:{mm}:00")
        if fav:
            where.append("a.ticker IN (SELECT ticker FROM favorites WHERE key_id = ?)")
            params.append(user["id"])
        if triggered:
            where.append("a.suppressed IS NULL")
        if kind:
            where.append("COALESCE(s.sectype, 'share') = ?")
            params.append(kind)
        order = {"price_asc": "a.price IS NULL, a.price ASC", "price_desc": "a.price IS NULL, a.price DESC",
                 "value_desc": "s.value_today IS NULL, s.value_today DESC"}.get(sort, "a.ts DESC")
        rows = db.query(
            f"SELECT a.*, s.value_today AS value_today, s.last AS current_price, s.sectype AS sectype, "
            f"s.name AS name FROM alerts a LEFT JOIN securities s ON s.ticker = a.ticker "
            f"WHERE {' AND '.join(where)} ORDER BY {order}, a.ts DESC, a.id DESC LIMIT ?",
            (*params, max(1, min(limit, 1000))))
        out = []
        for r in rows:
            d = al.alert_from_row(r)
            sec = scanner.securities.get(r["ticker"]) or {}
            d["value_today"] = sec.get("value_today", r.get("value_today"))
            d["current_price"] = sec.get("last", r.get("current_price"))
            d["sectype"] = sec.get("sectype") or r.get("sectype") or "share"
            out.append(d)
        return {"alerts": out, "directions": ss.ALERT_DIRECTIONS, "actions": ss.ALERT_ACTIONS,
                "kinds": ss.SECURITY_KINDS}

    # ======================================================= журнал решений
    @app.get("/api/decisions")
    async def api_decisions(cycle_id: Optional[int] = None, ticker: Optional[str] = None, limit: int = 300,
                            user: dict = Depends(current_user)):
        """Журнал решений (п. 24): строка на тикер × цикл, глобальные пороги."""
        if ticker:
            rows = db.query("SELECT * FROM decisions WHERE ticker = ? ORDER BY id DESC LIMIT ?",
                            (ticker.upper(), max(1, min(limit, 2000))))
        else:
            if cycle_id is None:
                r = db.query_one("SELECT MAX(cycle_id) AS c FROM decisions")
                cycle_id = r["c"] if r else None
            rows = db.query("SELECT * FROM decisions WHERE cycle_id = ? ORDER BY closeness DESC, ticker",
                            (cycle_id,))
        for r in rows:
            r["details"] = json.loads(r.get("details") or "{}")
        cycles = db.query("SELECT cycle_id, MIN(ts) AS ts, COUNT(*) AS n, SUM(passed) AS passed FROM decisions "
                          "GROUP BY cycle_id ORDER BY cycle_id DESC LIMIT 30")
        return {"cycle_id": cycle_id, "decisions": rows, "cycles": cycles}

    @app.get("/api/silent")
    async def api_silent(limit: int = 50, user: dict = Depends(current_user)):
        """«Почему молчит» (п. 24): по последним снапшотам и МОИМ порогам —
        кто ближе всего к срабатыванию и какого условия не хватило."""
        s = db.effective_settings(user["id"])
        snaps = db.snapshots(int(s["candle_window_min"]))
        out = []
        for t, snap in snaps.items():
            if not scanner.securities.get(t, {}).get("active"):
                continue
            ev = al.evaluate(snap, s)
            out.append({"ticker": t, "name": scanner.securities[t].get("name"), "ts": snap.get("ts"),
                        "triggered": ev.triggered, "closeness": ev.closeness, "failed": ev.failed,
                        "checks": ev.checks, "price": snap.get("price"),
                        "amplitude_atr": snap.get("amplitude_atr"), "volume_ratio": snap.get("volume_ratio")})
        out.sort(key=lambda r: (r["triggered"], -r["closeness"]))
        silent = [r for r in out if not r["triggered"]][:limit]
        return {"silent": silent, "triggered": [r for r in out if r["triggered"]],
                "thresholds": {k: s[k] for k in ("amplitude_atr_min", "amplitude_atr_max", "volume_mult",
                                                  "level_break_enabled", "candle_window_min")}}

    # ========================================================== отчёт дня
    @app.get("/api/report")
    async def api_report(day: Optional[str] = None, user: dict = Depends(current_user)):
        day = day or now_msk().strftime("%Y-%m-%d")
        stored = db.query_one("SELECT * FROM daily_reports WHERE (key_id = ? OR key_id IS NULL) AND day = ? "
                              "ORDER BY id DESC LIMIT 1", (user["id"], day))
        live = scanner.daily_top(day, user["id"])
        reports = db.query("SELECT day, MAX(created_at) AS created_at FROM daily_reports WHERE key_id = ? OR "
                           "key_id IS NULL GROUP BY day ORDER BY day DESC LIMIT 30", (user["id"],))
        return {"day": day, "live": live, "stored": json.loads(stored["data"]) if stored else None,
                "days": reports, "report_time": db.effective_settings(user["id"])["daily_report_time"],
                "market_stats": scanner.market_stats()}

    @app.post("/api/report/send")
    async def api_report_send(user: dict = Depends(current_user)):
        s = db.effective_settings(user["id"])
        day = now_msk().strftime("%Y-%m-%d")
        rep = await scanner.make_daily_report(user["id"], {**s, "daily_report_enabled": True}, day, send=True)
        return rep

    # ======================================================= paper trading
    def _paper_row(r: dict[str, Any], s: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        sec = scanner.securities.get(r["ticker"]) or {}
        r["lot"] = sec.get("lot")
        r["notified"] = json.loads(r.get("notified") or "{}")
        r["name"] = sec.get("name")
        if r["status"] == "open" and sec.get("last"):
            pct, rub = scanner.paper_pnl(r, sec["last"])
            r["current_price"] = sec["last"]
            r["unrealized_pct"] = pct
            r["unrealized_rub"] = rub
        if s is not None:  # деньги с комиссиями брокера и налогом (issue #5, п. 4)
            price = r.get("exit_price") if r["status"] != "open" else r.get("current_price")
            r["money"] = paper.money(r, s, price)
        r["levels_valid"] = paper.levels_ok(r["side"], r["entry_price"], r.get("stop"), r.get("take")) \
            or bool(r.get("levels_at"))
        return r

    @app.get("/api/paper")
    async def api_paper(user: dict = Depends(current_user)):
        s = db.effective_settings(user["id"])
        rows = [_paper_row(r, s) for r in db.query(
            "SELECT * FROM paper_trades WHERE key_id = ? ORDER BY id DESC LIMIT 300", (user["id"],))]
        closed = [r for r in rows if r["status"] != "open" and r.get("result_pct") is not None]
        opened = [r for r in rows if r["status"] == "open"]

        def total(items: list[dict[str, Any]], k: str) -> float:
            return round(sum((r.get("money") or {}).get(k) or 0 for r in items), 2)

        stats = {"open": len(opened), "closed": len(closed),
                 "wins": sum(1 for r in closed if r["result_pct"] > 0),
                 "total_rub": round(sum(r.get("profit_rub") or 0 for r in closed), 2),
                 "open_rub": round(sum(r.get("unrealized_rub") or 0 for r in opened), 2),
                 # Потрачено на покупку бумаг с комиссией входа: всего и в открытых позициях
                 "spent_rub": total(rows, "cost_rub"), "spent_open_rub": total(opened, "cost_rub"),
                 "fees_rub": total(rows, "fee_rub"), "tax_rub": total(closed, "tax_rub"),
                 "net_rub": total(closed, "net_rub")}
        stats["winrate"] = round(stats["wins"] / len(closed) * 100, 1) if closed else None
        return {"trades": rows, "stats": stats,
                "settings": {k: s.get(k) for k in ("paper_auto_close", "paper_notify_enabled", "paper_target_pct",
                                                    "paper_target_rub", "paper_default_side", "fee_entry_pct",
                                                    "fee_exit_pct", "tax_profit_pct")}}

    @app.delete("/api/paper")
    async def api_paper_clear(user: dict = Depends(current_user)):
        """«Очистить статистику» (issue #5, п. 13): удалить все свои paper-сделки."""
        n = db.execute("DELETE FROM paper_trades WHERE key_id = ?", (user["id"],)).rowcount
        return {"deleted": n, **(await api_paper(user))}

    def _qty(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            q = float(value)
        except (TypeError, ValueError):
            raise HTTPException(422, "Количество — число") from None
        if q <= 0:
            raise HTTPException(422, "Количество должно быть больше 0")
        return q

    def _level(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise HTTPException(422, "Стоп/тейк — число") from None

    def _default_side(alert: Optional[dict[str, Any]], s: dict[str, Any]) -> str:
        """Сторона по умолчанию: настройка «Paper: сторона» (лонг/шорт) или
        «авто» — шорт по «плите вверх», иначе лонг (issue #5, п. 5)."""
        pref = s.get("paper_default_side") or "auto"
        if pref in ("long", "short"):
            return pref
        return "short" if alert and alert["direction"] in ("up_plate",) else "long"

    def _open_paper(user: dict, body: dict) -> int:
        alert = None
        if body.get("alert_id"):
            row = db.query_one("SELECT * FROM alerts WHERE id = ?", (int(body["alert_id"]),))
            alert = al.alert_from_row(row) if row else None
        ticker = str(body.get("ticker") or (alert or {}).get("ticker") or "").upper()
        if ticker not in scanner.securities:
            raise HTTPException(404, "Тикер не найден")
        sec = scanner.securities[ticker]
        s = db.effective_settings(user["id"])
        side = body.get("side") or _default_side(alert, s)
        if side not in ("long", "short"):
            raise HTTPException(422, "side: long или short")
        try:
            entry = float(body.get("entry_price") or sec.get("last") or (alert or {}).get("price") or 0)
        except (TypeError, ValueError):
            raise HTTPException(422, "Цена входа — число") from None
        if entry <= 0:
            raise HTTPException(422, "Нет цены входа")
        qty = _qty(body.get("qty"))
        if qty is None:
            qty = float(sec.get("lot") or 1)  # по умолчанию — один лот
        extra = (alert or {}).get("extra") or {}
        # Уровни из вердикта алерта — от цены алерта и, возможно, для другой стороны:
        # переносим к цене входа и проверяем ориентацию (корень бага MGKL/TATN, issue #5)
        stop, take, note = paper.default_levels(
            side, entry, _level(extra.get("stop")), _level(extra.get("take")),
            anchor=(alert or {}).get("price"), atr_pct=sec.get("daily_atr_pct"),
            stop_mult=s["stop_atr_mult"], take_mult=s["take_atr_mult"])
        for kind in ("stop", "take"):  # уровни, заданные вручную, проверяются от цены входа
            if body.get(kind) not in (None, ""):
                v = _level(body[kind])
                err = paper.manual_level_error(side, kind, v, entry)
                if err:
                    raise HTTPException(422, err)
                if kind == "stop":
                    stop = v
                else:
                    take = v
        user_note = str(body.get("note") or "")
        if note and not user_note:
            user_note = note
        cur = db.execute(
            "INSERT INTO paper_trades (key_id, ticker, side, entry_price, stop, take, alert_id, note, opened_at, qty) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user["id"], ticker, side, entry, stop, take, body.get("alert_id"),
             user_note[:300], fmt_ts(now_msk()), qty))
        return int(cur.lastrowid)

    @app.post("/api/paper")
    async def api_paper_open(body: dict, user: dict = Depends(current_user)):
        return {"id": _open_paper(user, body), **(await api_paper(user))}

    @app.post("/api/paper/toggle")
    async def api_paper_toggle(body: dict, user: dict = Depends(current_user)):
        """Кнопка «Paper» в таблице и карточке тикера (issue #3): нет открытой
        позиции — открыть по текущей цене, есть — отменить (удалить) её."""
        ticker = str(body.get("ticker") or "").upper()
        open_t = _open_papers(user["id"]).get(ticker)
        if open_t:
            db.execute("DELETE FROM paper_trades WHERE id = ? AND key_id = ?", (open_t["id"], user["id"]))
            return {"ticker": ticker, "paper": None, "removed": open_t["id"]}
        tid = _open_paper(user, {**body, "ticker": ticker})
        return {"ticker": ticker, "paper": _paper_row(db.query_one("SELECT * FROM paper_trades WHERE id = ?", (tid,)))}

    @app.patch("/api/paper/{trade_id}")
    async def api_paper_edit(trade_id: int, body: dict, user: dict = Depends(current_user)):
        """Количество / стоп / тейк / сторона / заметка по сделке (количество
        можно задать и у закрытой — тогда пересчитывается профит в ₽).

        Стоп и тейк правятся вручную (issue #5, п. 3): уровень проверяется от
        текущей цены, а проверка срабатывания идёт только по свечам ПОСЛЕ
        правки (``levels_at``) — прошлые минутки сделку не закрывают."""
        t = db.query_one("SELECT * FROM paper_trades WHERE id = ? AND key_id = ?", (trade_id, user["id"]))
        if not t:
            raise HTTPException(404, "Сделка не найдена")
        sets, params = [], []
        if "qty" in body:
            t["qty"] = _qty(body["qty"])
            sets.append("qty = ?")
            params.append(t["qty"])
        levels_changed = False
        if "side" in body and body["side"] != t["side"]:
            if t["status"] != "open":
                raise HTTPException(422, "Сторону можно сменить только у открытой сделки")
            if body["side"] not in ("long", "short"):
                raise HTTPException(422, "side: long или short")
            stop, take = paper.flip_levels(t["entry_price"], t.get("stop"), t.get("take"))
            t.update({"side": body["side"], "stop": stop, "take": take})
            sets += ["side = ?", "stop = ?", "take = ?"]
            params += [t["side"], stop, take]
            levels_changed = True
        ref = (scanner.securities.get(t["ticker"]) or {}).get("last") or t["entry_price"]
        for k in ("stop", "take"):
            if k in body:
                v = _level(body[k])
                if t["status"] == "open":
                    err = paper.manual_level_error(t["side"], k, v, ref)
                    if err:
                        raise HTTPException(422, err)
                sets.append(f"{k} = ?")
                params.append(v)
                levels_changed = True
        if levels_changed and t["status"] == "open":
            sets.append("levels_at = ?")
            params.append(fmt_ts(now_msk()))
        if "note" in body:
            sets.append("note = ?")
            params.append(str(body["note"] or "")[:300])
        if t["status"] != "open" and t.get("exit_price") is not None and "qty" in body:
            _, rub = scanner.paper_pnl(t, t["exit_price"])
            sets.append("profit_rub = ?")
            params.append(rub)
        if sets:
            db.execute(f"UPDATE paper_trades SET {', '.join(sets)} WHERE id = ?", (*params, trade_id))
        return await api_paper(user)

    @app.post("/api/paper/{trade_id}/close")
    async def api_paper_close(trade_id: int, body: dict | None = None, user: dict = Depends(current_user)):
        t = db.query_one("SELECT * FROM paper_trades WHERE id = ? AND key_id = ?", (trade_id, user["id"]))
        if not t or t["status"] != "open":
            raise HTTPException(404, "Открытая сделка не найдена")
        price = float((body or {}).get("price") or (scanner.securities.get(t["ticker"]) or {}).get("last") or 0)
        if price <= 0:
            raise HTTPException(422, "Нет цены выхода")
        pct, rub = scanner.paper_pnl(t, price)
        db.execute("UPDATE paper_trades SET status = 'manual', exit_price = ?, closed_at = ?, result_pct = ?, "
                   "profit_rub = ? WHERE id = ?", (price, fmt_ts(now_msk()), pct, rub, trade_id))
        return await api_paper(user)

    @app.delete("/api/paper/{trade_id}")
    async def api_paper_delete(trade_id: int, user: dict = Depends(current_user)):
        db.execute("DELETE FROM paper_trades WHERE id = ? AND key_id = ?", (trade_id, user["id"]))
        return await api_paper(user)

    # ============================================================ статика
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.get("/sw.js")
    async def sw():
        # Service worker с корня — scope на весь сайт
        return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")
    return app


class NoCacheStatic(StaticFiles):
    """Статика с ``Cache-Control: no-cache``: браузер каждый раз сверяет ETag
    (304 без тела, если файл не менялся), поэтому после обновления бота
    сразу получает новые app.js/style.css, а не эвристически закэшированные."""

    def file_response(self, *args: Any, **kwargs: Any):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _ev(ev: al.Evaluation) -> dict[str, Any]:
    return {"triggered": ev.triggered, "closeness": ev.closeness, "checks": ev.checks, "failed": ev.failed}
