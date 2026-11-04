#!/usr/bin/env python3
"""Единая точка запуска (п. 2): сайт + сканер + планировщик в одном
asyncio-процессе. Остановка — ввести «0» и Enter в консоли (или Ctrl+C,
или кнопка «Остановить сервер» в админке).

    python start.py                 # http://127.0.0.1:8000
    TB_HOST=0.0.0.0 TB_PORT=8080 python start.py
    TB_SCANNER=0 python start.py    # только сайт, без обращений к MOEX
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

import uvicorn

from app import auth
from app.config import AppConfig, load_or_create_admin_key
from app.db import Database
from app.moex import IssClient
from app.push import PushService
from app.scanner import Scanner
from app.scheduler import RequestScheduler, TokenBucket
from app.web import AppContext, create_app

logger = logging.getLogger("start")


def stdin_watcher(loop: asyncio.AbstractEventLoop, stop) -> None:
    """Отдельный поток: ждёт «0» в консоли."""
    while True:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return
        if line == "":  # stdin закрыт (запуск без консоли) — работаем дальше
            return
        if line.strip() == "0":
            print("Остановка…", flush=True)
            loop.call_soon_threadsafe(stop)
            return
        if line.strip():
            print("Введите 0 и Enter, чтобы остановить сервер.", flush=True)


async def main(make_iss=None, prepare=None, banner: str = "") -> None:
    """``make_iss(scheduler, cfg)`` — подмена клиента ISS (офлайн-демо),
    ``prepare(db, scanner)`` — корутина, выполняется после старта планировщика."""
    # httpx пишет INFO на каждый запрос (~500 строк за цикл) — оставляем только проблемы;
    # 429/5xx/ретраи логирует сам планировщик.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = AppConfig.from_env()
    cfg.ensure_dirs()
    admin_key, created = load_or_create_admin_key(cfg.admin_key_path)
    db = Database(cfg.db_path)
    auth.sync_admin_key(db, admin_key)

    scheduler = RequestScheduler(TokenBucket(cfg.requests_per_second, 1.0))
    iss = make_iss(scheduler, cfg) if make_iss else IssClient(scheduler, cfg.iss_base_url)
    push = PushService(db, cfg.vapid_key_path, cfg.vapid_subject)
    scanner = Scanner(db, iss, scheduler, push)

    server: uvicorn.Server | None = None

    def stop() -> None:
        scanner.stop()
        if server is not None:
            server.should_exit = True

    ctx = AppContext(db=db, scheduler=scheduler, push=push, scanner=scanner, stop_callback=stop)
    app = create_app(ctx)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning",
                                           access_log=False))

    url = f"http://{'127.0.0.1' if cfg.host in ('0.0.0.0', '::') else cfg.host}:{cfg.port}"
    print("=" * 64)
    if banner:
        print(f"  {banner}")
    print(f"  Дашборд:      {url}")
    print(f"  Ключ админа:  {admin_key}" + ("  (создан сейчас)" if created else ""))
    print(f"  Файл ключа:   {cfg.admin_key_path}")
    print(f"  База:         {cfg.db_path}")
    print(f"  Пуши (VAPID): {'готовы' if push.available else push.error}")
    print(f"  Сканер MOEX:  {'включён' if cfg.scanner_enabled else 'выключен (TB_SCANNER=0)'}, "
          f"{cfg.requests_per_second:g} запрос/с")
    print("  Остановка:    введите 0 и Enter (или Ctrl+C)")
    print("=" * 64, flush=True)

    loop = asyncio.get_running_loop()
    threading.Thread(target=stdin_watcher, args=(loop, stop), daemon=True, name="stdin").start()

    scheduler.start()
    if prepare is not None:
        await prepare(db, scanner)
    tasks = [asyncio.create_task(server.serve(), name="web")]
    if cfg.scanner_enabled:
        tasks.append(asyncio.create_task(scanner.run(), name="scanner"))
    try:
        await tasks[0]  # сервер завершается по «0», Ctrl+C или кнопке в админке
    finally:
        stop()
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        for t in list(ctx.background):
            t.cancel()
        await scheduler.stop()
        await iss.close()
        db.close()
        print("Сервер остановлен.", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
