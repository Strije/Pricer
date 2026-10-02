UNKNOWN_DELIVERY_HOURS = 999999 * 24
CROSS_RELATIONS = {"analog"}
SAMPLE_LIMIT = 5

STRATEGIES = {
    "price": "Минимальная цена",
    "fastest": "Минимальный срок",
    "price_within_days": "Цена в пределах срока",
}


def filter_exact_offers(row, offers, clean_num, same_brand, *, exact_match=True):
    article_key = str(row.get("article_key") or clean_num(row.get("article") or ""))
    requested_brand = str(row.get("brand") or "").strip()
    accepted = []
    stats = {
        "raw_count": 0,
        "wrong_article": 0,
        "wrong_brand": 0,
        "cross": 0,
        "unavailable": 0,
        "exact_count": 0,
        "accepted_count": 0,
        "mode": "exact" if exact_match else "with_analogs",
        "samples": {
            "accepted": [],
            "wrong_article": [],
            "wrong_brand": [],
            "cross": [],
            "unavailable": [],
        },
    }
    for offer in offers or []:
        stats["raw_count"] += 1
        offer_article = clean_num(offer.get("article") or offer.get("original_article") or "")
        same_article = offer_article == article_key
        relation = str(offer.get("offer_relation") or "").strip()
        is_cross = bool(offer.get("is_cross")) or relation in CROSS_RELATIONS
        offer_brand = str(offer.get("brand") or offer.get("normalized_brand") or "").strip()
        brand_matches = not requested_brand or same_brand(offer_brand, requested_brand)

        if not same_article:
            stats["wrong_article"] += 1
            _add_sample(stats, "wrong_article", offer, clean_num)
        if not brand_matches:
            stats["wrong_brand"] += 1
            _add_sample(stats, "wrong_brand", offer, clean_num)
        if is_cross:
            stats["cross"] += 1
            _add_sample(stats, "cross", offer, clean_num)

        # Артикул и бренд уже полностью отвечают на вопрос «та ли это деталь».
        # Флаг is_cross сюда не входит: он производный от этой же сверки, а
        # раньше приходил от поставщика и мог ошибочно отбросить точное совпадение.
        strict_exact = same_article and brand_matches
        if strict_exact:
            stats["exact_count"] += 1

        if exact_match and not strict_exact:
            continue

        if offer.get("can_order_quantity") is False:
            stats["unavailable"] += 1
            _add_sample(stats, "unavailable", offer, clean_num)
            continue
        accepted.append(offer)
        stats["accepted_count"] += 1
        _add_sample(stats, "accepted", offer, clean_num)
    return accepted, stats


def _add_sample(stats, bucket, offer, clean_num):
    samples = stats.get("samples") or {}
    rows = samples.get(bucket)
    if rows is None or len(rows) >= SAMPLE_LIMIT:
        return
    rows.append(_offer_sample(offer, clean_num))


def _offer_sample(offer, clean_num):
    offer = offer or {}
    relation = str(offer.get("offer_relation") or offer.get("cross_relation") or "").strip()
    return {
        "provider": offer.get("provider_name") or offer.get("provider"),
        "brand": offer.get("brand") or offer.get("normalized_brand"),
        "display_brand": offer.get("display_brand"),
        "article": offer.get("article") or offer.get("original_article"),
        "article_key": clean_num(offer.get("article") or offer.get("original_article") or ""),
        "name": offer.get("name"),
        "price": offer.get("purchase_price", offer.get("price")),
        "quantity": offer.get("available_quantity", offer.get("quantity")),
        "max_count": offer.get("max_count"),
        "availability_is_lower_bound": offer.get("availability_is_lower_bound"),
        "minimum_quantity": offer.get("minimum_quantity"),
        "actual_order_quantity": offer.get("actual_order_quantity"),
        "requested_quantity": offer.get("requested_quantity"),
        "quantity_step": offer.get("quantity_step") or offer.get("multiplicity"),
        "delivery_hours": offer.get("delivery_hours") or offer.get("delivery_total_hours"),
        "warehouse": offer.get("warehouse") or offer.get("logo"),
        "order_total": round(_total_price(offer), 2),
        "over_order_quantity": _over_order_quantity(offer),
        "is_cross": bool(offer.get("is_cross")),
        "relation": relation,
        "can_order_quantity": offer.get("can_order_quantity"),
        "internal_offer_id": offer.get("internal_offer_id"),
        "supplier_offer_id": offer.get("supplier_offer_id"),
    }


def rank_offers(offers, strategy="price", max_delivery_hours=None):
    rows = list(offers or [])
    if max_delivery_hours is not None:
        rows = [
            offer for offer in rows
            if _delivery_hours(offer) <= max_delivery_hours
        ]
    return sorted(rows, key=_selection_key(strategy))


def select_best_offer(offers, strategy="price", max_delivery_hours=None):
    rows = rank_offers(offers, strategy=strategy, max_delivery_hours=max_delivery_hours)
    if not rows:
        return None
    return rows[0]


def _selection_key(strategy):
    if strategy == "fastest":
        return lambda offer: (
            _over_order_quantity(offer),
            _actual_order_quantity(offer),
            _delivery_hours(offer),
            _total_price(offer),
            _price(offer),
            -_available_quantity(offer),
            _provider(offer),
            str(offer.get("internal_offer_id") or ""),
        )
    return lambda offer: (
        _over_order_quantity(offer),
        _actual_order_quantity(offer),
        _total_price(offer),
        _price(offer),
        _delivery_hours(offer),
        -_available_quantity(offer),
        _provider(offer),
        str(offer.get("internal_offer_id") or ""),
    )


def status_for_selection(row, exact_count, selectable_count, selected, *, brand_missing=False, limit_applied=False):
    if brand_missing:
        return "needs_review", "Бренд не указан"
    if selected:
        return "ready", "Готово"
    if exact_count <= 0:
        return "not_found", "Нет точного совпадения"
    if selectable_count <= 0:
        return "unavailable", "Недостаточно количества или данных заказа"
    if limit_applied:
        return "limit", "Срок выше лимита"
    return "not_found", "Не найдено допустимое предложение"


def _price(offer):
    try:
        return float(str(offer.get("purchase_price", offer.get("price"))).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return 999999999.0


def _actual_order_quantity(offer):
    try:
        return int(float(str(offer.get("actual_order_quantity") or offer.get("requested_quantity") or 1).replace(",", ".")))
    except (TypeError, ValueError):
        return 1


def _requested_quantity(offer):
    try:
        return int(float(str(offer.get("requested_quantity") or 1).replace(",", ".")))
    except (TypeError, ValueError):
        return 1


def _over_order_quantity(offer):
    return max(0, _actual_order_quantity(offer) - _requested_quantity(offer))


def _total_price(offer):
    return _price(offer) * max(1, _actual_order_quantity(offer))


def _delivery_hours(offer):
    try:
        return int(offer.get("delivery_hours", offer.get("delivery_total_hours", UNKNOWN_DELIVERY_HOURS)) or UNKNOWN_DELIVERY_HOURS)
    except (TypeError, ValueError):
        return UNKNOWN_DELIVERY_HOURS


def _available_quantity(offer):
    try:
        return int(offer.get("available_quantity", offer.get("quantity", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _provider(offer):
    return str(offer.get("provider_name") or offer.get("provider") or "")
