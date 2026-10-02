"""Вкладка «Поиск»: выбор бренда голосованием поставщиков и выдача для браузера.

Поиск — run_query десктопа (core/engine.py: search_brands -> search_offers). Здесь — то, что
десктоп решал диалогом и таблицей: какой бренд взять самим, какие поля отдать в браузер и
три лучших предложения над выдачей.
"""

import json
import os
import re

UNKNOWN_HOURS = 999999 * 24
WARRANTY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "brand_warranty.json")
_warranty = None
# Порог автовыбора: лидер назван не меньше чем половиной ответивших и в 1,5 раза чаще второго
# (с упоминаниями в названиях). Запись 02.10.2026: W71295 — MANN 7 против Redskin 4 берём сами,
# OC90 — MAHLE 7+2 против AM POINT 7 спрашиваем.
DEFAULT_BRAND_SHARE = 0.5
DEFAULT_BRAND_LEAD = 1.5

SEARCH_FIELDS = (
    "provider", "brand", "display_brand", "article", "name", "available_quantity", "minimum_quantity",
    "quantity_step", "actual_order_quantity", "can_order_quantity", "availability_is_lower_bound",
    "delivery_hours", "delivery_display", "warehouse", "is_cross", "returnable", "provider_confirm_count",
    "internal_offer_id", "is_best_offer", "original_article", "normalized_brand_key",
)
# Покупателю не отдаём то, по чему он закажет сам: закупку, поставщика, склад, коды предложения.
PURCHASE_FIELDS = ("purchase_price", "provider", "warehouse", "internal_offer_id")


def brand_settings(org_settings):
    settings = org_settings or {}
    try:
        share = float(settings.get("brand_auto_share", DEFAULT_BRAND_SHARE))
        lead = float(settings.get("brand_auto_lead", DEFAULT_BRAND_LEAD))
    except (TypeError, ValueError):
        share, lead = DEFAULT_BRAND_SHARE, DEFAULT_BRAND_LEAD
    return min(max(share, 0.0), 1.0), max(lead, 1.0)


def choose_brand(choices, answered, share=DEFAULT_BRAND_SHARE, lead=DEFAULT_BRAND_LEAD):
    """Бренд, который берём без вопроса, или None — тогда выбирает пользователь.

    Один вариант — берём. Иначе лидер должен набрать долю `share` ответивших поставщиков и быть в
    `lead` раз впереди второго: 162622 — ZIC у 6 из 12 при 1–2 голосах за остальные — ищем ZIC.
    """
    if not choices:
        return None
    if len(choices) == 1:
        return choices[0]
    first, second = choices[0], choices[1]
    total = max(len(answered or []), 1)
    # голоса поставщиков плюс упоминания бренда в названиях других вариантов («ан. MAHLE OC90»)
    score = lambda c: c.get("score", c["votes"])  # noqa: E731
    if first["votes"] >= share * total and score(first) >= lead * max(score(second), 0.5):
        return first
    return None


def _brand_key(value):
    return re.sub(r"[^A-ZА-ЯЁ0-9]", "", str(value or "").upper())


def match_hint(choices, hint):
    """Вариант, совпадающий с заранее известным брендом (VAG из Laximo), или None."""
    key = _brand_key(hint)
    if not key:
        return None
    return next((c for c in choices if _brand_key(c["brand"]) == key), None) or \
        next((c for c in choices if key in _brand_key(c["label"])), None)


def default_warranty():
    """Стартовый справочник гарантий (данные avtodrug92 из приложения Abcp: срок, рейтинг 0–5, условия)."""
    global _warranty
    if _warranty is None:
        with open(WARRANTY_FILE, encoding="utf-8") as file:
            _warranty = json.load(file)
    return _warranty


def brand_view(choice):
    return {key: choice.get(key) for key in ("label", "brand", "votes", "mentions", "providers", "name")}


def offer_view(item, engine, customer=False):
    row = {field: item.get(field) for field in SEARCH_FIELDS if field in item}
    purchase = float(item.get("purchase_price", item.get("price")) or 0)
    row["purchase_price"] = round(purchase, 2)
    row["sale_price"] = round(float(engine.apply_markup(purchase)[0]), 2) if purchase else None
    images = item.get("image_urls") or []
    row["image_url"] = (images[0] if isinstance(images, list) else str(images)) if images else ""
    template = (getattr(engine, "site_links", None) or {}).get(item.get("provider") or "")
    if template and not customer:  # ссылка раскрывает поставщика — покупателю её не даём
        from urllib.parse import quote

        row["site_url"] = template.replace("{article}", quote(str(item.get("article") or ""))) \
            .replace("{brand}", quote(str(item.get("brand") or "")))
    if customer:
        for field in PURCHASE_FIELDS:
            row.pop(field, None)
    return row


def _hours(row):
    value = row.get("delivery_hours")
    return UNKNOWN_HOURS if value is None else int(value)


def _price(row):
    return float(row.get("sale_price") or row.get("purchase_price") or 0) or float("inf")


def highlights(rows):
    """Три карточки над выдачей: самая низкая цена искомого номера, самый дешёвый аналог, лучший срок.
    Возвращает номера строк в rows (у покупателя кодов предложений нет)."""
    orderable = [r for r in rows if r.get("can_order_quantity", True)]
    own = [r for r in orderable if not r.get("is_cross")]
    analogs = [r for r in orderable if r.get("is_cross")]
    out = {}
    if own:
        out["cheapest"] = min(own, key=lambda r: (_price(r), _hours(r)))
    if analogs:
        out["cheapest_analog"] = min(analogs, key=lambda r: (_price(r), _hours(r)))
    if orderable:
        # при равном сроке — больше ★, потом дешевле (как сортировка «Быстрее» в приложении Abcp)
        out["fastest"] = min(orderable, key=lambda r: (_hours(r), -int(r.get("provider_confirm_count") or 0), _price(r)))
    return {key: next(i for i, r in enumerate(rows) if r is value) for key, value in out.items()}
