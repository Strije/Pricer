import re


UNKNOWN_DELIVERY_HOURS = 999999 * 24

# Подтипы связи убраны: всё, что не совпало с запросом, — аналог. Порядок
# кросс-целей задают подтверждения поставщиков, срок и цена, а не тип связи.
RELATION_PRIORITY = {
    "analog": 0,
    "offer": 1,
    "": 1,
}


def build_cross_targets(
    results,
    original_article,
    selected_brand="",
    *,
    clean_num=None,
    brand_key=None,
    same_brand=None,
    max_targets=0,
    top_limit=20,
):
    clean = clean_num or _clean_num
    brand_key_fn = brand_key or clean
    same_brand_fn = same_brand or (lambda left, right: brand_key_fn(left) == brand_key_fn(right))
    original_clean = clean(original_article)
    selected_brand = str(selected_brand or "").strip()

    candidates = {}
    stats = {
        "raw_cross_rows": 0,
        "unique_candidates": 0,
        "selected_candidates": 0,
        "skipped_original": 0,
        "skipped_without_article": 0,
        "skipped_without_brand": 0,
        "dropped_by_limit": 0,
        "limit": max(0, int(max_targets or 0)),
        "top_targets": [],
    }

    for item in results or []:
        if not _truthy(_get(item, "is_cross", "is_analog")):
            continue
        stats["raw_cross_rows"] += 1

        article = str(_get(item, "article", "normalized_article") or "").strip()
        clean_article = clean(article)
        if not clean_article:
            stats["skipped_without_article"] += 1
            continue

        brand = str(_get(item, "brand", "normalized_brand") or "").strip()
        normalized_brand = brand_key_fn(brand)
        if not normalized_brand:
            stats["skipped_without_brand"] += 1
            continue

        if original_clean and clean_article == original_clean and selected_brand and same_brand_fn(brand, selected_brand):
            stats["skipped_original"] += 1
            continue

        key = (clean_article, normalized_brand)
        provider = str(_get(item, "cross_source_provider", "provider_name", "provider") or "").strip()
        relation = _relation_code(item)
        price = _price(item)
        delivery = _delivery_hours(item)

        candidate = candidates.setdefault(
            key,
            {
                "article": article or clean_article,
                "clean_article": clean_article,
                "brand": brand,
                "brand_key": normalized_brand,
                "providers": set(),
                "raw_offer_count": 0,
                "best_price": None,
                "best_delivery_hours": UNKNOWN_DELIVERY_HOURS,
                "relation": relation,
                "relation_priority": RELATION_PRIORITY.get(relation, 4),
                "source_examples": [],
            },
        )
        candidate["raw_offer_count"] += 1
        if provider:
            candidate["providers"].add(provider)
        if price is not None:
            candidate["best_price"] = price if candidate["best_price"] is None else min(candidate["best_price"], price)
        candidate["best_delivery_hours"] = min(candidate["best_delivery_hours"], delivery)
        relation_priority = RELATION_PRIORITY.get(relation, 4)
        if relation_priority < candidate["relation_priority"]:
            candidate["relation"] = relation
            candidate["relation_priority"] = relation_priority
        if len(candidate["source_examples"]) < 3:
            candidate["source_examples"].append(
                {
                    "provider": provider,
                    "price": price,
                    "delivery_hours": delivery if delivery < UNKNOWN_DELIVERY_HOURS else None,
                }
            )

    ranked = sorted(candidates.values(), key=_candidate_sort_key)
    stats["unique_candidates"] = len(ranked)
    limit = stats["limit"]
    if limit > 0:
        selected = ranked[:limit]
        stats["dropped_by_limit"] = max(0, len(ranked) - len(selected))
    else:
        selected = ranked
    stats["selected_candidates"] = len(selected)
    stats["top_targets"] = [_target_log_item(item) for item in selected[: max(0, int(top_limit or 0))]]
    return [_target_from_candidate(item) for item in selected], stats


def _target_from_candidate(candidate):
    providers = sorted(candidate["providers"])
    cross_source = ", ".join(providers[:4])
    if len(providers) > 4:
        cross_source += f" +{len(providers) - 4}"
    return {
        "article": candidate["article"],
        "clean_article": candidate["clean_article"],
        "brand": candidate["brand"],
        "brand_key": candidate["brand_key"],
        "cross_source": cross_source,
        "confirmed_by": providers,
        "provider_confirm_count": len(providers),
        "raw_offer_count": candidate["raw_offer_count"],
        "best_price": candidate["best_price"],
        "best_delivery_hours": candidate["best_delivery_hours"],
        "cross_relation": candidate["relation"],
    }


def _target_log_item(candidate):
    target = _target_from_candidate(candidate)
    return {
        "brand": target["brand"],
        "article": target["article"],
        "provider_confirm_count": target["provider_confirm_count"],
        "confirmed_by": target["confirmed_by"][:6],
        "raw_offer_count": target["raw_offer_count"],
        "best_price": target["best_price"],
        "best_delivery_hours": (
            target["best_delivery_hours"]
            if target["best_delivery_hours"] < UNKNOWN_DELIVERY_HOURS
            else None
        ),
        "cross_relation": target["cross_relation"],
    }


def _candidate_sort_key(candidate):
    price = candidate["best_price"] if candidate["best_price"] is not None else 999999999.0
    return (
        -len(candidate["providers"]),
        candidate["relation_priority"],
        candidate["best_delivery_hours"],
        price,
        -candidate["raw_offer_count"],
        candidate["brand_key"],
        candidate["clean_article"],
    )


def _relation_code(item):
    code = str(_get(item, "offer_relation") or "").strip()
    if code:
        return code
    if _truthy(_get(item, "is_cross", "is_analog")):
        return "analog"
    return "offer"


def _delivery_hours(item):
    for key in ("delivery_total_hours", "delivery_hours"):
        value = _get(item, key)
        if value not in (None, ""):
            return max(0, _int_value(value, UNKNOWN_DELIVERY_HOURS))
    days = _get(item, "days")
    if days in (None, ""):
        return UNKNOWN_DELIVERY_HOURS
    return max(0, _int_value(days, 999999) * 24)


def _price(item):
    for key in ("purchase_price", "price"):
        value = _get(item, key)
        if value not in (None, ""):
            return _float_value(value, None)
    return None


def _get(item, *keys):
    for key in keys:
        value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
        if value not in (None, ""):
            return value
    return ""


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "да", "on", "analog", "cross"}


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


def _clean_num(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
