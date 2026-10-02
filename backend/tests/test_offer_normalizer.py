import re

from offer_normalizer import (
    UNKNOWN_DELIVERY_HOURS,
    deduplicate_offers,
    is_no_return,
    is_requested_part,
    mark_best_offers,
    normalize_offer,
)


def clean(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def make(**kw):
    item = {"article": "W 712/95", "brand": "MANN", "provider": "Prov", "price": "700,5", "warehouse": "MSK", "days": 2}
    item.update(kw)
    return normalize_offer(item, clean, clean, requested_quantity=1)


def test_internal_offer_id_is_stable_golden():
    # Значение зафиксировано: по этому id корзина, заказы и перепроверка находят предложение.
    # Менять алгоритм нельзя, иначе старые заказы перестанут совпадать.
    assert make()["internal_offer_id"] == "d5a0461b2ba77d343c3cc67a"


def test_internal_offer_id_depends_on_price_and_warehouse():
    base = make()["internal_offer_id"]
    assert make(price="701")["internal_offer_id"] != base
    assert make(warehouse="SPB")["internal_offer_id"] != base


def test_price_and_delivery_normalization():
    offer = make()
    assert offer["purchase_price"] == 700.5
    assert offer["delivery_hours"] == 48
    assert make(days=None, delivery_hours=None)["delivery_hours"] == UNKNOWN_DELIVERY_HOURS


def test_is_requested_part_ignores_supplier_flags():
    same_brand = lambda a, b: clean(a) == clean(b)  # noqa: E731
    assert is_requested_part({"article": "W71295", "brand": "MANN"}, "W71295", "MANN", clean, same_brand)
    assert not is_requested_part({"article": "OP570", "brand": "FILTRON"}, "W71295", "MANN", clean, same_brand)


def test_no_return_markers():
    assert is_no_return({"allow_return": "0"})
    assert is_no_return({"return_percent": "-1"})
    assert is_no_return({"return": "impossible"})
    assert is_no_return({"return_type_name": "Без возврата"})
    assert not is_no_return({"return_type_name": "Возврат 14 дней"})


def test_deduplicate_keeps_faster_duplicate():
    slow = make(days=5)
    fast = make(days=1)
    result = deduplicate_offers([slow, fast])
    assert len(result) == 1 and result[0]["delivery_hours"] == 24


def test_best_offer_marked_once():
    rows = mark_best_offers([make(price="900"), make(price="700", warehouse="B")])
    assert sum(1 for r in rows if r["is_best_offer"]) == 1
