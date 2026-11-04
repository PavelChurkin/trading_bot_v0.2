#!/usr/bin/env python3
"""Кейс ETLN из issue (п. 16): исходный скоринг дал бы «ПОКУПАТЬ 95%»
на росте дня +6.37%, у верхней полосы Боллинджера и со стопом −20%.
Новый вердикт — «⏳ ПОЗДНО, ЖДАТЬ ОТКАТ», стоп/тейк от ATR.

    python examples/etln_verdict.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analysis as an  # noqa: E402
from app import settings_schema as ss  # noqa: E402

# Дневные индикаторы ETLN со скриншота в issue
DAY = {
    "rsi": 59.0, "stoch_k": 56.0, "bb_upper": 22.39, "bb_middle": 20.9, "bb_lower": 19.4,
    "sma20": 20.9, "sma50": 20.1, "macd_line": 0.35, "macd_signal": 0.2, "macd_hist": 0.15,
    "s1": 20.6, "r1": 23.0, "pivot": 21.0, "atr": 0.62, "atr_pct": 2.77,
    "weekly_trend": {"trend": "восходящий", "strength": 2.1},
}
SIGNALS = ["📈 НЕДЕЛЬНЫЙ ТРЕНД ВВЕРХ (сила: 2.1%)", "📈 MACD БЫЧИЙ СИГНАЛ", "📈 ЦЕНА ВЫШЕ SMA20 > SMA50",
           "💥 СИЛЬНЫЙ ОБЪЕМ", "🚀 ПРИБЛИЖЕНИЕ К МАКСИМУМАМ"]

if __name__ == "__main__":
    price, day_change = 22.36, 6.37
    v = an.make_verdict(price, day_change, DAY, ss.defaults(), signals=SIGNALS)
    print(f"ETLN {price} ({day_change:+.2f}% за день)")
    print(f"  исходный скоринг:  {an.VERDICT_LABELS[v['base_verdict']]} {v['strength']} "
          f"(покупка {v['buy_score']} / продажа {v['sell_score']})")
    print(f"  итоговый вердикт:  {v['label']}  — {v['strength_note']}")
    for r in v["late_reasons"]:
        print(f"    • {r}")
    print(f"  стоп {v['stop']} / тейк {v['take']} (R/R {v['risk_reward']}) — вместо стопа 17.94 (−20%)")
