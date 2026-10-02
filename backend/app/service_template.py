"""Подбор «ТО по машине»: по шаблону (фильтры, свечи, колодки…) — оригинальные номера из каталога
Laximo для конкретной машины, поиск каждого номера у всех поставщиков и три варианта в позицию
подбора: оригинал подешевле, проверенный аналог (★ от 2) подешевле, самый быстрый.

Масла в каталогах по VIN нет (там номера деталей, а не жидкости) — его менеджер добавляет поиском.
"""
import re

DEFAULT_ITEMS = ["Фильтр масляный", "Фильтр воздушный", "Фильтр салона", "Фильтр топливный",
                 "Свечи зажигания", "Колодки тормозные передние"]
MAX_ITEMS = 15
UNKNOWN_HOURS = 999999 * 24


def items_for(org_settings):
    items = (org_settings or {}).get("service_template") or DEFAULT_ITEMS
    return [str(x).strip()[:80] for x in items if str(x).strip()][:MAX_ITEMS]


def _stems(text):
    """Основы слов для сверки названий («Фильтр масляный» ~ «ФИЛЬТР МАСЛЯНЫЙ ДВИГАТЕЛЯ»)."""
    return [w[:5] for w in re.findall(r"[а-яёa-z]+", str(text or "").lower()) if len(w) >= 3]


def oem_numbers(categories, item, limit=2):
    """Оригинальные номера из ответа Laximo на поиск по названию: деталь, в названии которой есть
    все основы слов пункта шаблона (поиск Laximo находит и «прокладку фильтра»)."""
    stems = _stems(item)
    matched = []
    for category in categories or []:
        details = list(category.get("details") or [])
        for unit in category.get("units") or []:
            details += unit.get("details") or []
        for detail in details:
            oem = str(detail.get("oem") or "").strip()
            name = str(detail.get("name") or "").lower().strip()
            if oem and all(s in name for s in stems):
                matched.append((oem, name))
    # «Прокладка фильтра масляного» тоже содержит оба слова: если есть детали, название которых
    # начинается с главного слова пункта («фильтр…»), берём только их.
    head = [m for m in matched if stems and m[1].startswith(stems[0])]
    found = []
    for oem, _name in head or matched:
        if oem not in found:
            found.append(oem)
    return found[:limit]


def _hours(item):
    value = item.get("delivery_hours")
    return UNKNOWN_HOURS if value is None else int(value)


def _price(item):
    return float(item.get("purchase_price", item.get("price")) or 0) or float("inf")


def pick_variants(results):
    """Оригинал подешевле, аналог с ★ от 2 подешевле (иначе просто дешёвый аналог), самый быстрый."""
    orderable = [r for r in results if r.get("can_order_quantity", True) and r.get("internal_offer_id")]
    own = [r for r in orderable if not r.get("is_cross")]
    analogs = [r for r in orderable if r.get("is_cross")]
    trusted = [r for r in analogs if int(r.get("provider_confirm_count") or 0) >= 2] or analogs
    picks = []
    if own:
        picks.append(min(own, key=lambda r: (_price(r), _hours(r))))
    if trusted:
        picks.append(min(trusted, key=lambda r: (_price(r), _hours(r))))
    if orderable:
        picks.append(min(orderable, key=lambda r: (_hours(r), -int(r.get("provider_confirm_count") or 0), _price(r))))
    out, seen = [], set()
    for pick in picks:
        if pick["internal_offer_id"] not in seen:
            seen.add(pick["internal_offer_id"])
            out.append(pick)
    return out


def collect(engine, laximo, vehicle, items, progress=lambda item, status, data=None: None):
    """[(пункт, номер, варианты)] для каждого пункта шаблона; пункт без номера или предложений —
    с пустыми вариантами (менеджер добавит вручную)."""
    from app.search import choose_brand, match_hint

    out = []
    for item in items:
        progress(item, "catalog")
        try:
            numbers = oem_numbers(laximo.quick_details(vehicle, query=item), item)
        except Exception as exc:
            progress(item, "error", {"message": str(exc)[:200]})
            out.append((item, "", []))
            continue
        variants, used = [], ""
        for number in numbers:
            progress(item, "search", {"oem": number})
            choices, answered = engine.search_brands(number)
            picked = match_hint(choices, vehicle.get("oem_brand") or "") or choose_brand(choices, answered)
            if picked is None and choices:
                picked = choices[0]  # оригинальный номер из каталога машины: берём бренд, названный чаще всего
            results = engine.search_offers(number, picked["choice"] if picked else None)
            variants = pick_variants(results)
            used = number
            if variants:
                break
        progress(item, "done", {"oem": used, "variants": len(variants)})
        out.append((item, used, variants))
    return out
