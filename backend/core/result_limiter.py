import re


DEFAULT_PROVIDER_LIMITS = {
    "direct_result_limit": 50,
    "repeat_result_limit": 3,
    "analog_result_limit": 250,
    "result_limit_sort": "delivery_price",
}

RESULT_LIMIT_RANGES = {
    "direct_result_limit": (1, 50),
    "repeat_result_limit": (1, 5),
    "analog_result_limit": (1, 250),
}

RESULT_LIMIT_SORT_OPTIONS = (
    ("delivery_price", "Сначала срок, затем цена"),
    ("price_delivery", "Сначала цена, затем срок"),
    ("stock_delivery_price", "Сначала остаток, затем срок"),
)

UNKNOWN_DELIVERY_HOURS = 999999 * 24


def normalize_provider_limits(config=None):
    config = config or {}
    result = dict(DEFAULT_PROVIDER_LIMITS)
    aliases = {
        "direct_result_limit": ("direct_result_limit", "direct_limit"),
        "repeat_result_limit": ("repeat_result_limit", "repeat_limit"),
        "analog_result_limit": ("analog_result_limit", "analog_limit"),
    }
    for key, names in aliases.items():
        for name in names:
            if name in config:
                result[key] = _clamp_int(config.get(name), *RESULT_LIMIT_RANGES[key])
                break
    sort_mode = str(
        config.get("result_limit_sort", config.get("sort_mode", result["result_limit_sort"]))
        or ""
    )
    allowed_modes = {value for value, _label in RESULT_LIMIT_SORT_OPTIONS}
    result["result_limit_sort"] = sort_mode if sort_mode in allowed_modes else DEFAULT_PROVIDER_LIMITS["result_limit_sort"]
    return result


def limit_provider_results(
    items,
    requested_article,
    selected_brand="",
    *,
    same_brand=None,
    brand_key=None,
    clean_num=None,
    config=None,
):
    limits = normalize_provider_limits(config)
    rows = list(items or [])
    stats = {
        "input_count": len(rows),
        "output_count": 0,
        "direct_input": 0,
        "direct_output": 0,
        "analog_offer_input": 0,
        "analog_offer_output": 0,
        "analog_group_input": 0,
        "analog_group_output": 0,
        "dropped_count": 0,
        "sort_mode": limits["result_limit_sort"],
    }
    if not rows:
        return [], stats

    clean = clean_num or _clean_num
    key_brand = brand_key or clean
    same = same_brand or (lambda left, right: key_brand(left) == key_brand(right))
    requested_clean = clean(requested_article)
    selected_brand = str(selected_brand or "").strip()

    direct = []
    analog_groups = {}
    unknown_groups = 0

    for index, item in enumerate(rows):
        article = _get(item, "normalized_article", "article")
        article_clean = clean(article)
        brand = _get(item, "normalized_brand", "brand")
        is_requested_article = bool(article_clean and requested_clean and article_clean == requested_clean)
        is_selected_brand = True
        if selected_brand and str(brand or "").strip():
            is_selected_brand = same(brand, selected_brand)

        if is_requested_article and is_selected_brand:
            direct.append((index, item))
            continue

        group_key = (key_brand(brand), article_clean)
        if not group_key[0] or not group_key[1]:
            unknown_groups += 1
            group_key = ("__unknown__", str(unknown_groups))
        analog_groups.setdefault(group_key, []).append((index, item))

    direct_sorted = sorted(
        direct,
        key=lambda pair: _offer_rank(pair[1], pair[0], limits["result_limit_sort"]),
    )
    limited_direct = direct_sorted[: limits["direct_result_limit"]]

    ranked_groups = sorted(
        analog_groups.items(),
        key=lambda entry: _best_group_rank(entry[1], limits["result_limit_sort"]),
    )
    selected_groups = ranked_groups[: limits["analog_result_limit"]]

    limited_analogs = []
    for _group_key, group_rows in selected_groups:
        group_sorted = sorted(
            group_rows,
            key=lambda pair: _offer_rank(pair[1], pair[0], limits["result_limit_sort"]),
        )
        limited_analogs.extend(group_sorted[: limits["repeat_result_limit"]])

    limited_pairs = sorted(
        [*limited_direct, *limited_analogs],
        key=lambda pair: _offer_rank(pair[1], pair[0], limits["result_limit_sort"]),
    )
    limited = [item for _index, item in limited_pairs]

    analog_input = sum(len(group) for group in analog_groups.values())
    analog_output = len(limited_analogs)
    stats.update(
        {
            "output_count": len(limited),
            "direct_input": len(direct),
            "direct_output": len(limited_direct),
            "analog_offer_input": analog_input,
            "analog_offer_output": analog_output,
            "analog_group_input": len(analog_groups),
            "analog_group_output": len(selected_groups),
            "dropped_count": max(0, len(rows) - len(limited)),
        }
    )
    return limited, stats


def _clamp_int(value, minimum, maximum):
    try:
        number = int(float(str(value).replace(" ", "").replace(",", ".")))
    except (TypeError, ValueError):
        number = minimum
    return max(minimum, min(maximum, number))


def _clean_num(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _get(item, *keys):
    for key in keys:
        if isinstance(item, dict):
            value = item.get(key)
        else:
            value = getattr(item, key, None)
        if value not in (None, ""):
            return value
    return ""


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
    return _float_value(_get(item, "purchase_price", "price"), 999999999.0)


def _quantity(item):
    value = _get(item, "available_quantity", "quantity")
    if value in (None, ""):
        return 0
    match = re.search(r"\d+(?:[.,]\d+)?", str(value))
    if not match:
        return 0
    return _int_value(match.group(0), 0)


def _offer_rank(item, index, sort_mode):
    delivery = _delivery_hours(item)
    price = _price(item)
    quantity = _quantity(item)
    stable = (
        str(_get(item, "provider_name", "provider")),
        str(_get(item, "normalized_brand_key", "brand")),
        str(_get(item, "normalized_article", "article")),
        str(_get(item, "warehouse_id", "stock_id", "warehouse", "logo")),
        str(_get(item, "supplier_offer_id", "offer_id")),
        index,
    )
    if sort_mode == "price_delivery":
        return (price, delivery, -quantity, *stable)
    if sort_mode == "stock_delivery_price":
        return (-quantity, delivery, price, *stable)
    return (delivery, price, -quantity, *stable)


def _best_group_rank(group_rows, sort_mode):
    return min(_offer_rank(item, index, sort_mode) for index, item in group_rows)
