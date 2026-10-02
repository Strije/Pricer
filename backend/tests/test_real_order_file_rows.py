"""Характеризующие тесты на данных реального прогона заказа из файла (лог от 2026-10-02).

В логе лежат предложения уже после обработки приложением, поэтому проверяем не сырые ответы
поставщиков, а отбор и ранжирование: то, что должно пережить вынос движка из main.py.
"""
import pytest

from bulk_order import filter_exact_offers, rank_offers, select_best_offer
from conftest import clean_num
from offer_normalizer import deduplicate_offers, mark_best_offers


def _row(request):
    return {"article": request["article"], "article_key": clean_num(request["article"]), "brand": request["brand"]}


def test_fixture_is_not_empty(order_file_rows):
    assert len(order_file_rows) >= 20
    assert all(case["offers"] for case in order_file_rows)


def test_recorded_selection_passes_exact_filter(order_file_rows, brand_resolver):
    """Предложение, которое приложение выбрало, должно проходить точный отбор по тому же запросу."""
    checked = 0
    for case in order_file_rows:
        selected = case["selected_offer"]
        if not selected:
            continue
        accepted, _ = filter_exact_offers(_row(case["request"]), [selected], clean_num, brand_resolver.same)
        assert accepted, f"{case['request']['article']}: выбранное предложение не прошло отбор"
        checked += 1
    assert checked >= 20


def test_exact_filter_only_returns_requested_part(order_file_rows, brand_resolver):
    for case in order_file_rows:
        row = _row(case["request"])
        accepted, stats = filter_exact_offers(row, case["offers"], clean_num, brand_resolver.same)
        for offer in accepted:
            assert clean_num(offer["article"]) == row["article_key"]
            assert brand_resolver.same(offer["brand"], row["brand"])
        assert stats["accepted_count"] == len(accepted)
        assert stats["raw_count"] == len(case["offers"])


@pytest.mark.parametrize("strategy", ["price", "fastest"])
def test_ranking_is_deterministic_and_complete(order_file_rows, strategy):
    for case in order_file_rows:
        first = rank_offers(case["offers"], strategy)
        second = rank_offers(list(reversed(case["offers"])), strategy)
        assert [o["internal_offer_id"] for o in first] == [o["internal_offer_id"] for o in second]
        assert len(first) == len(case["offers"])


def test_best_by_price_is_not_pricier_than_any_equal_quantity_offer(order_file_rows):
    for case in order_file_rows:
        best = select_best_offer(case["offers"], "price")
        same_quantity = [
            o for o in case["offers"]
            if o.get("actual_order_quantity") == best.get("actual_order_quantity")
            and o.get("requested_quantity") == o.get("actual_order_quantity")
        ]
        for offer in same_quantity:
            if best.get("requested_quantity") == best.get("actual_order_quantity"):
                assert best["price"] <= offer["price"] + 1e-9


def test_dedup_is_idempotent_on_real_offers(order_file_rows):
    for case in order_file_rows:
        once = deduplicate_offers(case["offers"])
        twice = deduplicate_offers(once)
        assert [o["internal_offer_id"] for o in once] == [o["internal_offer_id"] for o in twice]


def test_exactly_one_best_offer_marked(order_file_rows):
    for case in order_file_rows:
        marked = mark_best_offers(case["offers"])
        assert sum(1 for o in marked if o["is_best_offer"]) == 1
