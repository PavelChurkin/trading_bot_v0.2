"""Гипотеза issue #3, п. 21: интенсивность подхода к сопротивлению/поддержке
как прогноз вершины пампа.

Оффлайн-эксперимент на синтетике (без сети, детерминированный seed):
генерируем N пампов — боковик, разгон к сопротивлению с ростом объёма,
вершина (часть пампов останавливается у уровня, часть пробивает его на
~1 ATR, часть «выдыхается» раньше), затем откат. На каждом окне разгона
считаем ``analysis.approach_metrics`` и сравниваем ``pump_peak_forecast`` с
фактической вершиной. Базовая линия — «цена + 1 ATR».

    python experiments/approach_intensity.py [N]

Метрики: средняя абсолютная ошибка вершины в ATR (MAE) и корреляция
интенсивности с оставшимся ходом до вершины. Это проверка гипотезы, а не
торговая система: синтетика заложена так, что пик связан с уровнем, — на
реальных минутках MOEX вывод нужно перепроверять (``experiments/live_iss_smoke.py``).
"""

from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import approach_metrics  # noqa: E402


def make_pump(rng: random.Random) -> tuple[list[dict], float, float, int]:
    atr = 0.5
    base = 100.0
    wc, price = [], base
    for _ in range(20):  # боковик
        o = price
        price = base + rng.uniform(-0.15, 0.15)
        wc.append({"open": o, "close": price, "high": max(o, price) + 0.05, "low": min(o, price) - 0.05,
                   "volume": 1000 * rng.uniform(0.8, 1.2)})
    level = max(c["high"] for c in wc) + rng.uniform(1.0, 3.0) * atr  # сопротивление выше боковика
    scenario = rng.choice(["stop_at_level", "break_1atr", "early_fade"])
    peak = {"stop_at_level": level, "break_1atr": level + atr * rng.uniform(0.6, 1.2),
            "early_fade": price + (level - price) * rng.uniform(0.4, 0.8)}[scenario]
    start_rise = len(wc)
    steps = rng.randint(5, 9)
    for i in range(steps):  # разгон: ускорение + рост объёма
        o = price
        frac = ((i + 1) / steps) ** 1.6
        price = wc[start_rise - 1]["close"] + (peak - wc[start_rise - 1]["close"]) * frac
        wc.append({"open": o, "close": price, "high": price + 0.03, "low": o - 0.03,
                   "volume": 1000 * (1.5 + i * rng.uniform(0.4, 0.9))})
    for _ in range(4):  # откат
        o = price
        price -= atr * rng.uniform(0.3, 0.7)
        wc.append({"open": o, "close": price, "high": o + 0.03, "low": price - 0.03, "volume": 1500})
    return wc, level, peak, start_rise


def main(n: int = 300) -> dict:
    rng = random.Random(21)
    err_model, err_base, intens, remaining = [], [], [], []
    for _ in range(n):
        wc, level, peak, start = make_pump(rng)
        atr = 0.5
        peak_i = max(range(len(wc)), key=lambda i: wc[i]["close"])
        for i in range(start + 2, peak_i):  # окна разгона до вершины
            m = approach_metrics(wc[: i + 1], atr, level, 99.0)
            if m["pump_peak_forecast"] is None:
                continue
            price = wc[i]["close"]
            err_model.append(abs(m["pump_peak_forecast"] - peak) / atr)
            err_base.append(abs(price + atr - peak) / atr)
            intens.append(m["approach_intensity"])
            remaining.append((peak - price) / atr)
    corr = statistics.correlation(intens, remaining) if len(intens) > 2 else float("nan")
    res = {"pumps": n, "points": len(err_model), "mae_model_atr": round(statistics.mean(err_model), 3),
           "mae_baseline_atr": round(statistics.mean(err_base), 3), "corr_intensity_remaining": round(corr, 3)}
    print("Гипотеза п. 21 — интенсивность подхода → вершина пампа")
    for k, v in res.items():
        print(f"  {k:26s} {v}")
    better = res["mae_model_atr"] < res["mae_baseline_atr"]
    print("  вывод:", "прогноз точнее базовой линии «цена + 1 ATR»" if better
          else "прогноз НЕ лучше базовой линии — гипотеза не подтверждена")
    return res


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
