"""Прогон seed() офлайн-демо без веб-сервера и сводка: алерты, вердикты.

    python experiments/demo_tune.py [tmpdir]
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "examples")]

from app import auth  # noqa: E402
from app.db import Database  # noqa: E402
from app.push import PushService  # noqa: E402
from app.scanner import Scanner  # noqa: E402
from app.scheduler import RequestScheduler, TokenBucket  # noqa: E402
from demo_iss import DemoIss  # noqa: E402
import demo_server  # noqa: E402


async def main(tmp: Path) -> None:
    db = Database(tmp / "tb.sqlite")
    auth.sync_admin_key(db, "admin-DEMO")
    sched = RequestScheduler(TokenBucket(1000, 50))
    scanner = Scanner(db, DemoIss(sched), sched, PushService(db, tmp / "vapid.pem"))
    sched.start()
    try:
        await demo_server.seed(db, scanner)
    finally:
        await sched.stop()
    print("--- alerts (global)")
    for a in db.query("SELECT ts, ticker, direction, score, suppressed, reason FROM alerts WHERE key_id IS NULL "
                      "ORDER BY ts"):
        print(a["ts"], a["ticker"], a["direction"], a["score"], a["suppressed"], "|", a["reason"][:90])
    for t in ("ETLN", "SBER"):
        r = db.get_state(f"analysis:{t}") or {}
        v = r.get("verdict") or {}
        d = r.get("daily") or {}
        print(f"--- {t}: {v.get('verdict')} base={v.get('base_verdict')} buy={v.get('buy_score')} "
              f"sell={v.get('sell_score')} late={v.get('late_reasons')}")
        print("   daily:", {k: d.get(k) for k in ("rsi", "stoch_k", "sma20", "sma50", "r1", "s1", "bb_upper",
                                                  "macd_hist", "atr_pct")}, "price", r.get("price"))
    print("--- paper:", [(t["ticker"], t["status"], t["result_pct"]) for t in db.query("SELECT * FROM paper_trades")])


if __name__ == "__main__":
    tmp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="tbtune-"))
    try:
        asyncio.run(main(tmp))
    finally:
        if len(sys.argv) <= 1:
            shutil.rmtree(tmp, ignore_errors=True)
