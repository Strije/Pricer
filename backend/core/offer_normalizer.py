import hashlib
import json

from order_quantity import quantity_from_item


OFFER_ID_KEYS = (
    "offer_id",
    "article_id",
    "goods_id",
    "gid",
    "part_id",
    "product_id",
    "stock_id",
    "keyzak",
    "zakaz_code",
)


UNKNOWN_DELIVERY_HOURS = 999999 * 24


def normalize_offer(item, clean_article, brand_key, requested_quantity=1):
    source = {
        key: value
        for key, value in dict(item or {}).items()
        if not str(key).startswith("_") and key != "source_data"
    }
    result = dict(item or {})

    article = str(result.get("article") or "")
    brand = str(result.get("brand") or "").strip()
    provider = str(result.get("provider") or "").strip()
    warehouse = str(result.get("warehouse") or result.get("logo") or "")

    normalized_article = clean_article(article)
    normalized_brand_key = brand_key(brand)
    price = _float_value(result.get("price"), 0.0)
    sale_price = _float_value(
        result.get("sale_price", result.get("retail_price", result.get("price_sale", price))),
        price,
    )
    delivery_hours = _delivery_hours(result)
    quantity_info = quantity_from_item(result, requested_quantity)

    supplier_offer_id = result.get("supplier_offer_id") or _first_value(result, OFFER_ID_KEYS)
    result.setdefault("original_article", article)
    result["provider_code"] = clean_article(provider)
    result["provider_name"] = provider
    result["normalized_article"] = normalized_article
    result["normalized_brand"] = brand
    result["normalized_brand_key"] = normalized_brand_key
    result["purchase_price"] = price
    result["sale_price"] = sale_price
    result.setdefault("price", price)
    result["delivery_hours"] = delivery_hours
    result["delivery_text_original"] = str(
        result.get("delivery_display")
        or result.get("delivery_text")
        or result.get("delivery_duration")
        or result.get("days")
        or ""
    )
    result["warehouse"] = warehouse
    result["available_quantity"] = (
        quantity_info.available_int() if quantity_info.available_quantity is not None else None
    )
    result["minimum_quantity"] = _plain_number(quantity_info.minimum_quantity)
    result["quantity_step"] = _plain_number(quantity_info.quantity_step)
    result["package_quantity"] = _plain_number(quantity_info.package_quantity)
    result["requested_quantity"] = _plain_number(quantity_info.requested_quantity)
    result["actual_order_quantity"] = _plain_number(quantity_info.actual_order_quantity)
    result["can_order_quantity"] = quantity_info.can_order
    result["availability_is_lower_bound"] = quantity_info.availability_is_lower_bound
    result["quantity_reason"] = quantity_info.reason
    result["supplier_offer_id"] = str(supplier_offer_id or "")
    result["returnable"] = _returnable(result)
    result["is_original"] = _bool_value(result.get("is_original", result.get("original", False)))
    result["is_cross"] = _bool_value(result.get("is_cross", result.get("is_analog", False)))
    result["cross_type"] = str(result.get("cross_type") or result.get("cross_relation") or "")
    relation = classify_offer_relation(result)
    result["offer_relation"] = relation["code"]
    result["offer_relation_label"] = relation["label"]
    result["offer_relation_reason"] = relation["reason"]
    result["updated_at"] = str(result.get("updated_at") or result.get("updated") or "")
    result["source_data"] = source
    result["internal_offer_id"] = _internal_offer_id(
        provider,
        normalized_brand_key,
        normalized_article,
        price,
        warehouse,
        supplier_offer_id,
    )
    return result


def offer_sort_key(item):
    return (
        _int_value(item.get("delivery_hours"), UNKNOWN_DELIVERY_HOURS),
        _float_value(item.get("purchase_price", item.get("price")), 999999999.0),
        -_int_value(item.get("available_quantity"), 0),
        str(item.get("provider_name") or item.get("provider") or ""),
        str(item.get("normalized_brand_key") or item.get("brand") or ""),
        str(item.get("normalized_article") or item.get("article") or ""),
        str(item.get("internal_offer_id") or ""),
    )


def offer_unique_key(item, clean_filter_text=lambda value: str(value or "")):
    return offer_duplicate_key(item, clean_filter_text) + (
        _int_value(item.get("delivery_hours"), UNKNOWN_DELIVERY_HOURS),
        _int_value(item.get("available_quantity"), 0),
    )


def offer_duplicate_key(item, clean_filter_text=lambda value: str(value or "")):
    warehouse = (
        item.get("warehouse_id")
        or item.get("stock_id")
        or item.get("keyzak")
        or item.get("warehouse")
        or item.get("logo")
        or ""
    )
    return (
        str(item.get("provider_code") or item.get("provider") or "").strip(),
        str(item.get("normalized_brand_key") or item.get("brand") or "").strip(),
        str(item.get("normalized_article") or item.get("article") or "").strip(),
        round(_float_value(item.get("purchase_price", item.get("price")), 0.0), 2),
        clean_filter_text(warehouse),
        str(item.get("supplier_offer_id") or ""),
    )


def offer_duplicate_preference_key(item):
    return (
        _int_value(item.get("delivery_hours"), UNKNOWN_DELIVERY_HOURS),
        -_int_value(item.get("available_quantity"), 0),
        -_probability(item),
        _updated_rank(item),
        0 if item.get("returnable", True) else 1,
        str(item.get("internal_offer_id") or ""),
    )


def deduplicate_offers(items, clean_filter_text=lambda value: str(value or "")):
    unique = {}
    for item in items or []:
        key = offer_duplicate_key(item, clean_filter_text)
        if key not in unique or offer_duplicate_preference_key(item) < offer_duplicate_preference_key(unique[key]):
            unique[key] = dict(item)
    return sorted(unique.values(), key=offer_sort_key)


def mark_best_offers(items):
    rows = [dict(item) for item in items or []]
    if not rows:
        return []
    _mark_popular_analogs(rows)
    best_id = min(rows, key=best_offer_key).get("internal_offer_id")
    for item in rows:
        is_best = item.get("internal_offer_id") == best_id
        item["is_best_offer"] = bool(is_best)
        item["best_offer_reasons"] = best_offer_reasons(item, rows) if is_best else []
    return rows


def best_offer_key(item):
    return (
        0 if item.get("can_order_quantity", True) else 1,
        _int_value(item.get("delivery_hours"), UNKNOWN_DELIVERY_HOURS),
        -_int_value(item.get("provider_confirm_count", item.get("analog_provider_count")), 0),
        _float_value(item.get("purchase_price", item.get("price")), 999999999.0),
        -_int_value(item.get("available_quantity"), 0),
        -_probability(item),
        0 if item.get("returnable", True) else 1,
        str(item.get("provider_name") or item.get("provider") or ""),
        str(item.get("internal_offer_id") or ""),
    )


def best_offer_reasons(item, all_items):
    reasons = []
    prices = [_float_value(row.get("purchase_price", row.get("price")), 0.0) for row in all_items]
    prices = [price for price in prices if price > 0]
    delivery_values = [
        _int_value(row.get("delivery_hours"), UNKNOWN_DELIVERY_HOURS)
        for row in all_items
        if _int_value(row.get("delivery_hours"), UNKNOWN_DELIVERY_HOURS) < UNKNOWN_DELIVERY_HOURS
    ]
    price = _float_value(item.get("purchase_price", item.get("price")), 0.0)
    delivery = _int_value(item.get("delivery_hours"), UNKNOWN_DELIVERY_HOURS)
    if prices and price == min(prices):
        reasons.append("минимальная цена")
    elif prices and price <= (sum(prices) / len(prices)):
        reasons.append("цена ниже средней")
    if delivery_values and delivery == min(delivery_values):
        reasons.append("минимальный срок")
    if _int_value(item.get("available_quantity"), 0) >= _int_value(item.get("actual_order_quantity"), 1):
        reasons.append("подтверждённый остаток")
    if _probability(item) >= 90:
        reasons.append("высокая вероятность")
    confirm_count = _int_value(item.get("provider_confirm_count", item.get("analog_provider_count")), 0)
    if confirm_count >= 2:
        reasons.append(f"подтвердили поставщики: {confirm_count}")
    if item.get("returnable", True):
        reasons.append("возвратное")
    return reasons


def _mark_popular_analogs(rows):
    groups = {}
    for item in rows:
        key = (
            str(item.get("normalized_brand_key") or ""),
            str(item.get("normalized_article") or ""),
        )
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, set()).add(str(item.get("provider_name") or item.get("provider") or ""))
    for item in rows:
        key = (
            str(item.get("normalized_brand_key") or ""),
            str(item.get("normalized_article") or ""),
        )
        count = len(groups.get(key, set()))
        item["provider_confirm_count"] = count
        item["is_confirmed_by_suppliers"] = count >= 2
        item["analog_provider_count"] = count
        item["is_popular_analog"] = bool(item.get("is_cross") and count >= 2)


def is_requested_part(item, clean_article, requested_brand, clean_num, same_brand):
    """True, если позиция — та самая деталь: артикул и бренд совпали с запросом.

    Единственный источник истины для is_cross. Флаги поставщиков сюда не входят:
    у каждого из них своя семантика, и ошибочный флаг раньше доживал до отбора.
    Пустые значения расхождением не считаем — сверять нечего.
    """
    article = clean_num((item or {}).get("article", ""))
    brand = str((item or {}).get("brand") or "").strip()
    article_matches = not article or article == clean_article
    brand_matches = (
        not requested_brand
        or not brand
        or same_brand(brand, requested_brand)
    )
    return bool(article_matches and brand_matches)


def classify_offer_relation(item):
    """Тип связи определяем своей сверкой с запросом, а не флагами поставщиков.

    Либо это искомая деталь (артикул и бренд совпали), либо аналог. Подтипы
    «замена/кросс/оригинал» не используем: у одиннадцати поставщиков они
    означают разное и раньше приводили к тому, что точное совпадение
    отбраковывалось как кросс.
    """
    if item.get("is_cross"):
        return {"code": "analog", "label": "АН", "reason": "аналог"}
    return {"code": "exact", "label": "", "reason": ""}


def relation_style(code, is_best=False):
    colors = {
        "analog": "#7C3AED",
        "exact": "#64748B",
    }
    return {
        "color": "#B45309" if is_best else colors.get(str(code or ""), "#64748B"),
        "bold": bool(is_best or code == "analog"),
        "background": "#FFF7ED" if is_best else "",
    }


def _float_value(value, default):
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def _int_value(value, default):
    try:
        return int(float(str(value).replace(" ", "").replace(",", ".")))
    except (TypeError, ValueError):
        return default


def _probability(item):
    for key in ("delivery_probability", "delivery_percent", "delivery_percent_probability", "probability"):
        value = _float_value(item.get(key), None)
        if value is not None:
            return value
    return 0.0


def _updated_rank(item):
    text = str(item.get("updated_at") or item.get("updated") or "")
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 8:
        return -int(digits[:14].ljust(14, "0"))
    return 0


def _delivery_hours(item):
    if item.get("delivery_total_hours") not in (None, ""):
        return _int_value(item.get("delivery_total_hours"), UNKNOWN_DELIVERY_HOURS)
    if item.get("delivery_hours") not in (None, ""):
        return _int_value(item.get("delivery_hours"), UNKNOWN_DELIVERY_HOURS)
    if item.get("days") in (None, ""):
        return UNKNOWN_DELIVERY_HOURS
    return max(0, _int_value(item.get("days"), 999999) * 24)


def _bool_value(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "да", "on", "analog", "cross"}


def is_no_return(item):
    """Признак «без возврата» у предложения любого поставщика.

    Поставщики отдают его по-разному: Profit-League allow_return=0, Фаворит и
    Форум not_returnable, Армтек RETDAYS=0, Авто-то BackPercent=-1, ТрейдСофт
    return=impossible, ABSTD — текстом типа возврата. Единая проверка нужна и
    фильтру «Скрывать без возврата», и сортировке, и заказу из файла.
    """
    return not _returnable(item or {})


def _returnable(item):
    if item.get("not_returnable") is True or str(item.get("not_returnable", "")).strip().lower() in {"1", "true"}:
        return False
    if str(item.get("return_percent", "")).strip() == "-1":
        return False
    if str(item.get("return", "")).strip().lower() == "impossible":
        return False
    if str(item.get("allow_return", "")).strip().lower() in {"0", "false", "no", "нет"}:
        return False
    if str(item.get("return_allowed", "")).strip().lower() in {"0", "false", "no", "нет"}:
        return False
    text = " ".join(
        str(item.get(key, ""))
        for key in ("return_type_name", "return_type", "return_policy", "comment", "availability")
    ).upper()
    # Без пробелов и знаков: «Без возврата», «БЕЗ-ВОЗВРАТА», «Возврат невозможен».
    text = "".join(ch for ch in text if ch.isalnum())
    return not any(marker in text for marker in ("БЕЗВОЗВРАТ", "ВОЗВРАТНЕВОЗМОЖ", "NORETURN"))


def _plain_number(value):
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _first_value(item, keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def _internal_offer_id(provider, brand, article, price, warehouse, supplier_offer_id):
    payload = {
        "provider": provider,
        "brand": brand,
        "article": article,
        "price": round(float(price or 0), 4),
        "warehouse": warehouse,
        "supplier_offer_id": str(supplier_offer_id or ""),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
