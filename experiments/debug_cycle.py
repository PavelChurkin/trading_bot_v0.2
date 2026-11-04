"""Отладка: цикл сканера на фейковом ISS, печать стеков при зависании."""
import asyncio, faulthandler, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_scanner import make_scanner  # noqa
from app.db import Database

async def main():
    db = Database(Path(tempfile.mkdtemp()) / "d.sqlite3")
    sc, sched = make_scanner(db)
    sched.start()
    try:
        info = await asyncio.wait_for(sc.run_cycle(), 5)
        print(info)
    except asyncio.TimeoutError:
        print("TIMEOUT; current:", sched.current, "order:", list(sched.order_log)[-10:], "pending:", list(sched._pending))
        for t in asyncio.all_tasks():
            t.print_stack(limit=3)
    await sched.stop()
asyncio.run(main())
