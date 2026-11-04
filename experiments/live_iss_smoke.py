#!/usr/bin/env python3
"""Живая проверка клиента ISS MOEX (≈6 запросов через общий token bucket 1 req/s).

    python experiments/live_iss_smoke.py [TICKER]

Проверяет то, что тесты делают на моках: формат ответов ISS (список бумаг,
батч котировок, индекс, минутные и дневные свечи, стакан) и что конвейер
analysis → alerts → verdict работает на реальных свечах.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import alerts as al  # noqa: E402
from app import analysis as an  # noqa: E402
from app import settings_schema as ss  # noqa: E402
from app.moex import IssClient, OrderbookUnavailable  # noqa: E402
from app.scheduler import RequestScheduler, TokenBucket  # noqa: E402
from app.timeutil import now_msk  # noqa: E402


async def main(ticker: str) -> None:
    sched = RequestScheduler(TokenBucket(1.0, 1.0))
    iss = IssClient(sched)
    t0 = time.monotonic()
    try:
        secs = await iss.securities()
        print(f"securities: {len(secs)} бумаг TQBR, пример {secs[0]}")
        md = await iss.marketdata([ticker, "GAZP", "LKOH"])
        print(f"marketdata (батч 3): {[(r['SECID'], r['LAST'], r['LASTTOPREVPRICE']) for r in md]}")
        print(f"index: {await iss.index()}")
        today = now_msk().date()
        minute = await iss.candles(ticker, 1, (today - timedelta(days=7)).isoformat())
        print(f"1-мин свечи {ticker} за 7 дней: {len(minute)}, последняя {minute[-1] if minute else None}")
        daily = await iss.candles(ticker, 24, (today - timedelta(days=200)).isoformat())
        print(f"дневные свечи: {len(daily)}")
        try:
            book = await iss.orderbook(ticker)
            print(f"стакан: {book and {k: len(v) for k, v in book.items() if isinstance(v, list)}}")
        except OrderbookUnavailable as exc:
            print(f"стакан недоступен без подписки (ожидаемо): {str(exc)[:80]}")

        s = ss.defaults()
        sec = next((x for x in secs if x["SECID"] == ticker), None)
        last_day = minute[-1]["ts"][:10]
        day_rows = [c for c in minute if c["ts"][:10] <= last_day]
        snap = an.compute_snapshot(ticker, day_rows, s, {"name": sec and sec.get("SHORTNAME")})
        print("снапшот:", {k: snap.get(k) for k in ("ts", "price", "amplitude_atr", "volume_ratio", "level_break",
                                                    "change_hour_pct", "impulse_age_min", "plate_score")})
        ev = al.evaluate(snap, s)
        print(f"триггер: {ev.triggered}, не прошло: {ev.failed}, близость {ev.closeness:.2f}")
        day_ind = an.daily_indicators(daily, last_day)
        verdict = an.make_verdict(snap["price"], snap.get("day_change_pct"), day_ind, s, snap,
                                  an.intraday_indicators(an.aggregate(day_rows, 15)))
        print(f"вердикт: {verdict.get('label')} · сила {verdict.get('strength')}/100 · "
              f"стоп {verdict.get('stop')} тейк {verdict.get('take')}")
        print(f"запросов: {sched.metrics().get('requests_total', '?')}, {time.monotonic() - t0:.1f} с")
    finally:
        await iss.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "SBER"))
