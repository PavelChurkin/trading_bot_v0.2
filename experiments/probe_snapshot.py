"""Печатает снапшот для синтетического дня с импульсом (подбор тест-данных)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from conftest import pulse_day  # noqa: E402
from app import analysis as an, settings_schema as ss, alerts as al  # noqa: E402

s = ss.defaults()
snap = an.compute_snapshot("TEST", pulse_day(), s)
for k in ("price", "amplitude_atr", "volume_ratio", "level_break", "atr", "change_hour_pct", "impulse_age_min",
          "plate_score", "direction"):
    print(k, snap[k])
ev = al.evaluate(snap, s)
print("triggered", ev.triggered, ev.failed, ev.closeness)
