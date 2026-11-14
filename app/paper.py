"""Paper trading: стоп/тейк, комиссии брокера и налог (issue #5).

Чистые функции без БД — их используют и веб-API, и сканер, и тесты.

Корень бага «стоп с разницей 74 %, цена 0.7, которой не было, и
отрицательный тейк» (MGKL) и «стоп на скачке 9 %, которого не было» (TATN):

* стоп/тейк брались из вердикта алерта как есть. Вердикт «SELL» даёт
  уровни для шорта (стоп ВЫШЕ цены, тейк НИЖЕ), а paper по «плите вниз»
  открывался лонгом — стоп оказывался выше входа и «срабатывал» на первой
  же минутке по своей цене (которой на бирже не было), а при огромном
  дневном ATR (MGKL упала с 1.9 до 0.37 ₽ за три недели, ATR ≈ 15–50 %
  цены) тейк = цена − 3×ATR уходил в минус;
* уровни считались от цены алерта, а вход — по текущей цене: после
  движения цены стоп оказывался уже «пробит»;
* при правке стопа/тейка проверка заново проходила по всем минуткам с
  момента открытия и закрывала сделку по прошлым свечам.
"""

from __future__ import annotations

from typing import Any, Optional

ATR_PCT_MIN, ATR_PCT_MAX = 0.1, 25.0  # разумные пределы дневного ATR, % цены
MAX_DIST_PCT = 90.0  # дальше стоп/тейк не ставим — цена должна остаться > 0


def sign(side: str) -> int:
    return 1 if side == "long" else -1


def levels_ok(side: str, entry: float, stop: Optional[float], take: Optional[float]) -> bool:
    """Лонг: 0 < стоп < вход < тейк; шорт: 0 < тейк < вход < стоп (пустые уровни допустимы)."""
    if not entry or entry <= 0:
        return False
    for v in (stop, take):
        if v is not None and v <= 0:
            return False
    if side == "long":
        return (stop is None or stop < entry) and (take is None or take > entry)
    return (stop is None or stop > entry) and (take is None or take < entry)


def atr_levels(side: str, entry: float, atr_pct: Optional[float], stop_mult: float, take_mult: float
               ) -> tuple[float, float]:
    """Стоп/тейк от дневного ATR (в % цены) с ограничением расстояния."""
    a = min(max(float(atr_pct or 2.0), ATR_PCT_MIN), ATR_PCT_MAX)
    k = sign(side)
    stop_d = min(float(stop_mult) * a, MAX_DIST_PCT)
    take_d = min(float(take_mult) * a, MAX_DIST_PCT)
    return round(entry * (1 - k * stop_d / 100), 6), round(entry * (1 + k * take_d / 100), 6)


def default_levels(side: str, entry: float, stop: Optional[float], take: Optional[float], *,
                   anchor: Optional[float], atr_pct: Optional[float], stop_mult: float, take_mult: float
                   ) -> tuple[float, float, Optional[str]]:
    """Стоп/тейк новой paper-сделки по умолчанию (из алерта или от ATR).

    ``stop``/``take`` из вердикта алерта посчитаны от цены алерта ``anchor``:
    сначала переносим их к цене входа (сохраняя расстояние в %), затем
    проверяем ориентацию для стороны сделки. Если уровни из алерта для
    другой стороны (вердикт SELL, а сделка — лонг) или вырождены — считаем
    заново от дневного ATR. Возвращает (стоп, тейк, пояснение | None)."""
    note = None
    if anchor and anchor > 0 and anchor != entry:
        stop = stop * entry / anchor if stop is not None else None
        take = take * entry / anchor if take is not None else None
    if stop is None or take is None or not levels_ok(side, entry, stop, take):
        if stop is not None or take is not None:
            note = "уровни алерта не подходят для этой стороны сделки — пересчитаны от дневного ATR"
        a_stop, a_take = atr_levels(side, entry, atr_pct, stop_mult, take_mult)
        if stop is None or not levels_ok(side, entry, stop, None):
            stop = a_stop
        if take is None or not levels_ok(side, entry, None, take):
            take = a_take
    return round(stop, 6), round(take, 6), note


def flip_levels(entry: float, stop: Optional[float], take: Optional[float]
                ) -> tuple[Optional[float], Optional[float]]:
    """Смена стороны (лонг ↔ шорт): стоп и тейк зеркалятся относительно входа
    с тем же расстоянием в %, чтобы риск остался прежним."""
    def mirror(v: Optional[float]) -> Optional[float]:
        return None if v is None else round(max(2 * entry - v, entry * 0.01), 6)
    return mirror(stop), mirror(take)


def manual_level_error(side: str, kind: str, value: Optional[float], ref_price: Optional[float]) -> Optional[str]:
    """Ручная правка стопа/тейка: уровень > 0 и по «правильную» сторону от
    текущей цены (иначе сделка закрылась бы сразу по цене, которой не было).
    Стоп в безубыток/в прибыль (выше входа у лонга) — допустим."""
    if value is None:
        return None
    if value <= 0:
        return f"{'Стоп' if kind == 'stop' else 'Тейк'} должен быть больше 0"
    if not ref_price:
        return None
    long = side == "long"
    if kind == "stop" and ((long and value >= ref_price) or (not long and value <= ref_price)):
        return (f"Стоп {value:g} {'не ниже' if long else 'не выше'} текущей цены {ref_price:g} — "
                f"для {'лонга' if long else 'шорта'} сделка закрылась бы сразу")
    if kind == "take" and ((long and value <= ref_price) or (not long and value >= ref_price)):
        return (f"Тейк {value:g} {'не выше' if long else 'не ниже'} текущей цены {ref_price:g} — "
                f"для {'лонга' if long else 'шорта'} сделка закрылась бы сразу")
    return None


def candle_ok(c: dict[str, Any]) -> bool:
    """Битая минутка (нулевые/отрицательные цены, high < low) не закрывает сделку."""
    try:
        o, h, lo, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
    except (KeyError, TypeError, ValueError):
        return False
    return min(o, h, lo, cl) > 0 and h >= lo and h >= max(o, cl) and lo <= min(o, cl)


def level_hit(t: dict[str, Any], c: dict[str, Any]) -> Optional[tuple[str, float]]:
    """Сработал ли стоп/тейк на свече ``c``: ("stop"|"take", цена исполнения).

    Если свеча открылась уже за уровнем (гэп) — исполнение по цене открытия,
    а не по уровню: так в журнале не появляется цена, которой не было."""
    long = t["side"] == "long"
    stop, take = t.get("stop"), t.get("take")
    o = float(c["open"])
    if stop is not None and ((long and c["low"] <= stop) or (not long and c["high"] >= stop)):
        gap = (long and o < stop) or (not long and o > stop)
        return "stop", (o if gap else stop)
    if take is not None and ((long and c["high"] >= take) or (not long and c["low"] <= take)):
        gap = (long and o > take) or (not long and o < take)
        return "take", (o if gap else take)
    return None


# ---------------------------------------------------- комиссии и налог (п. 4)
def fees(s: dict[str, Any]) -> tuple[float, float, float]:
    return (float(s.get("fee_entry_pct") or 0) / 100, float(s.get("fee_exit_pct") or 0) / 100,
            float(s.get("tax_profit_pct") or 0) / 100)


def money(t: dict[str, Any], s: dict[str, Any], price: Optional[float]) -> dict[str, Optional[float]]:
    """Деньги по сделке с учётом брокера и налога.

    * ``cost_rub`` — потрачено на вход: вход × кол-во + комиссия входа;
    * ``fee_rub`` — комиссии (вход + выход по ``price``);
    * ``tax_rub`` — налог с положительного результата после комиссий;
    * ``net_rub`` — чистый результат по цене ``price``."""
    qty = t.get("qty")
    entry = t.get("entry_price")
    if not qty or not entry:
        return {"cost_rub": None, "fee_rub": None, "tax_rub": None, "net_rub": None}
    f_in, f_out, tax = fees(s)
    notional = entry * qty
    out: dict[str, Optional[float]] = {"cost_rub": round(notional * (1 + f_in), 2)}
    if price:
        gross = (price - entry) * qty * sign(t["side"])
        fee = notional * f_in + price * qty * f_out
        pre_tax = gross - fee
        tax_rub = pre_tax * tax if pre_tax > 0 else 0.0
        out.update({"fee_rub": round(fee, 2), "tax_rub": round(tax_rub, 2), "net_rub": round(pre_tax - tax_rub, 2)})
    else:
        out.update({"fee_rub": round(notional * f_in, 2), "tax_rub": None, "net_rub": None})
    return out
