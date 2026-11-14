#!/usr/bin/env python3
"""Офлайн-демо: весь сайт + сканер на синтетических данных (без MOEX).

    python examples/demo_server.py         # http://127.0.0.1:8000, остановка — «0»

Данные и ключ демо хранятся отдельно от боевых: ``data-demo/``,
``config-demo/`` (переопределяются TB_DATA_DIR / TB_CONFIG_DIR).
При первом запуске база наполняется через настоящий конвейер:

* минутные свечи за неделю → таблица ``candles``;
* «перемотка» последних двух дней по 15-мин окнам → метрики для графиков
  и история сигналов (как если бы сканер работал всё это время);
* один настоящий цикл сканера (P1 обход → P0 для сработавших → журнал
  решений, алерты, пуши по правилам анти-спама);
* отчёт дня и пара paper-сделок.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

os.environ.setdefault("TB_DATA_DIR", str(ROOT / "data-demo"))
os.environ.setdefault("TB_CONFIG_DIR", str(ROOT / "config-demo"))

from app import alerts as al  # noqa: E402
from app import analysis as an  # noqa: E402
from app.scanner import candles_needed  # noqa: E402
from app.timeutil import fmt_ts, now_msk  # noqa: E402
from demo_iss import DemoIss  # noqa: E402

import start  # noqa: E402

logger = logging.getLogger("demo")


async def seed(db, scanner) -> None:
    if db.get_state("demo_seeded"):
        return
    iss: DemoIss = scanner.iss
    logger.info("Демо: наполняю базу синтетическими свечами (%s бумаг)…", len(iss.minute))
    for t, rows in iss.minute.items():
        db.upsert_candles(t, rows)
    scanner.apply_board(await iss.securities(), await iss.marketdata())
    for t in iss.minute:  # дневной ATR, как это делает P2-фон
        await scanner._daily_atr(t)
    await scanner._imoex_daily()  # история IMOEX для корреляции (issue #5)

    # «Перемотка» двух последних дней: метрики для графиков и история сигналов
    sets = scanner.settings_sets()
    g = sets[0][1]
    w = int(g["candle_window_min"])
    need = candles_needed(w)
    replay_days = {d.isoformat() for d in iss.days[-2:]}
    for t, rows in iss.minute.items():
        for end in range(len(rows)):
            ts = rows[end]["ts"]
            if ts[:10] not in replay_days or (int(ts[14:16]) + 1) % w != 0 or end + 1 == len(rows):
                continue
            part = rows[max(0, end + 1 - need): end + 1]
            snap = an.compute_snapshot(t, part, g, scanner.securities.get(t))
            if not snap:
                continue
            ev = al.evaluate(snap, g)
            scanner._store_metrics(t, snap, ev, None, w)
            if not ev.triggered:
                continue
            # P0 на момент окна: дневные свечи до этого дня + индикаторы окна
            day_ind = an.daily_indicators([d for d in iss.daily[t] if d["ts"][:10] <= ts[:10]], ts[:10])
            intraday = an.intraday_indicators(an.aggregate(part, w))
            for kid, s in sets:
                verdict = an.make_verdict(snap["price"], snap.get("day_change_pct"), day_ind, s, snap, intraday)
                sev = al.evaluate(snap, s, intraday, day_ind, verdict)
                if sev.alert is not None:
                    await scanner.deliver(kid, s, sev.alert, None)

    info = await scanner.run_cycle()  # настоящий цикл сканера
    logger.info("Демо: цикл %s — триггеров %s, алертов %s", info["cycle_id"], info["triggered"], info["alerts"])
    for t in ("ETLN", "SBER"):  # последний P0-анализ для карточек тикеров
        db.set_state(f"analysis:{t}", await scanner.analyze(t, None, refresh=False))

    day = iss.last_day.isoformat()
    await scanner.make_daily_report(None, g, day, send=False)
    for a in db.query("SELECT * FROM alerts WHERE key_id IS NULL AND direction IN ('up_pulse','down_pulse') "
                      "AND score >= 60 ORDER BY ts LIMIT 4"):
        admin = db.query_one("SELECT id FROM access_keys WHERE role = 'admin' AND active = 1")
        side = "long" if a["direction"] == "up_pulse" else "short"
        sign = 1 if side == "long" else -1
        atr = (scanner.securities[a["ticker"]].get("daily_atr_pct") or 2.0) / 100
        db.execute("INSERT INTO paper_trades (key_id, ticker, side, entry_price, stop, take, alert_id, note, "
                   "opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (admin["id"], a["ticker"], side, a["price"], a["price"] * (1 - sign * 0.35 * atr),
                    a["price"] * (1 + sign * 0.7 * atr), a["id"], "демо-сделка по сигналу", a["ts"]))
    await scanner._update_paper_trades()
    db.set_state("demo_seeded", fmt_ts(now_msk()))
    logger.info("Демо: готово")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    try:
        asyncio.run(start.main(make_iss=lambda sched, cfg: DemoIss(sched), prepare=seed,
                               banner="ДЕМО-РЕЖИМ: синтетические данные, MOEX не запрашивается"))
    except KeyboardInterrupt:
        pass
