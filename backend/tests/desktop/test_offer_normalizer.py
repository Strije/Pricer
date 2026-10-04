import unittest

from offer_normalizer import (
    UNKNOWN_DELIVERY_HOURS,
    deduplicate_offers,
    is_requested_part,
    mark_best_offers,
    normalize_offer,
    offer_duplicate_key,
    offer_sort_key,
    offer_unique_key,
    relation_style,
)


def clean_article(value):
    return "".join(char for char in str(value or "").upper() if char.isalnum())


def brand_key(value):
    return clean_article(value)


class OfferNormalizerTests(unittest.TestCase):
    def test_adds_stable_normalized_fields_and_source_snapshot(self):
        item = {
            "provider": "Supplier A",
            "brand": "MANN-FILTER",
            "article": "W 610/3",
            "price": "391,60",
            "quantity": "2",
            "minimum_quantity": 1,
            "multiplicity": 1,
            "warehouse": "MSK",
            "offer_id": "abc",
        }

        normalized = normalize_offer(item, clean_article, brand_key, requested_quantity=1)

        self.assertEqual(normalized["normalized_article"], "W6103")
        self.assertEqual(normalized["normalized_brand_key"], "MANNFILTER")
        self.assertEqual(normalized["purchase_price"], 391.60)
        self.assertEqual(normalized["available_quantity"], 2)
        self.assertEqual(normalized["actual_order_quantity"], 1)
        self.assertEqual(normalized["supplier_offer_id"], "abc")
        self.assertTrue(normalized["internal_offer_id"])
        self.assertEqual(normalized["source_data"]["article"], "W 610/3")

    def test_internal_offer_id_is_deterministic(self):
        item = {
            "provider": "Supplier A",
            "brand": "MANN",
            "article": "W6103",
            "price": 391.6,
            "quantity": "2",
            "warehouse": "MSK",
        }

        first = normalize_offer(item, clean_article, brand_key)
        second = normalize_offer(dict(item), clean_article, brand_key)

        self.assertEqual(first["internal_offer_id"], second["internal_offer_id"])

    def test_explicit_supplier_offer_id_is_not_overwritten_by_offer_id(self):
        item = {
            "provider": "Avtoto",
            "brand": "FEBI",
            "article": "101171",
            "price": 2502,
            "quantity": 5,
            "warehouse": "Москва",
            "supplier_offer_id": "part-77",
            "offer_id": "1",
        }

        normalized = normalize_offer(item, clean_article, brand_key)

        self.assertEqual(normalized["supplier_offer_id"], "part-77")

    def test_unknown_delivery_is_sorted_after_known_delivery(self):
        fast = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W1", "price": 1000, "quantity": 1, "days": 2},
            clean_article,
            brand_key,
        )
        unknown = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W2", "price": 900, "quantity": 1},
            clean_article,
            brand_key,
        )

        self.assertEqual(unknown["delivery_hours"], UNKNOWN_DELIVERY_HOURS)
        self.assertEqual(sorted([unknown, fast], key=offer_sort_key), [fast, unknown])

    def test_price_sort_uses_numbers_not_text(self):
        cheap = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W1", "price": "900", "quantity": 1, "days": 1},
            clean_article,
            brand_key,
        )
        expensive = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W2", "price": "1 000", "quantity": 1, "days": 1},
            clean_article,
            brand_key,
        )

        self.assertEqual(sorted([expensive, cheap], key=offer_sort_key), [cheap, expensive])

    def test_sort_order_is_deterministic_for_same_data(self):
        rows = [
            normalize_offer(
                {"provider": "B", "brand": "MANN", "article": "W2", "price": 100, "quantity": 3, "days": 1},
                clean_article,
                brand_key,
            ),
            normalize_offer(
                {"provider": "A", "brand": "MANN", "article": "W1", "price": 100, "quantity": 3, "days": 1},
                clean_article,
                brand_key,
            ),
        ]

        first = [item["internal_offer_id"] for item in sorted(rows, key=offer_sort_key)]
        second = [item["internal_offer_id"] for item in sorted(reversed(rows), key=offer_sort_key)]

        self.assertEqual(first, second)

    def test_unique_key_uses_normalized_fields(self):
        item = normalize_offer(
            {
                "provider": "Supplier A",
                "brand": "MANN-FILTER",
                "article": "W 610/3",
                "price": "391,60",
                "quantity": "2",
                "days": 1,
                "warehouse": "MSK",
            },
            clean_article,
            brand_key,
        )

        key = offer_unique_key(item, clean_article)

        self.assertIn("W6103", key)
        self.assertIn("MANNFILTER", key)

    def test_technical_duplicate_same_warehouse_keeps_faster_offer(self):
        slow = normalize_offer(
            {
                "provider": "Supplier A",
                "brand": "MANN",
                "article": "W6103",
                "price": "391.60",
                "quantity": "1",
                "days": 1,
                "warehouse": "MSK",
            },
            clean_article,
            brand_key,
        )
        fast = normalize_offer(
            {
                "provider": "Supplier A",
                "brand": "MANN",
                "article": "W6103",
                "price": "391.60",
                "quantity": "2",
                "delivery_total_hours": 0,
                "warehouse": "MSK",
            },
            clean_article,
            brand_key,
        )

        result = deduplicate_offers([slow, fast], clean_article)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["delivery_hours"], 0)
        self.assertEqual(result[0]["available_quantity"], 2)

    def test_same_offer_from_different_warehouses_is_not_merged(self):
        first = normalize_offer(
            {
                "provider": "Supplier A",
                "brand": "MANN",
                "article": "W6103",
                "price": "391.60",
                "quantity": "2",
                "delivery_total_hours": 0,
                "warehouse": "MSK",
            },
            clean_article,
            brand_key,
        )
        second = normalize_offer(
            {
                "provider": "Supplier A",
                "brand": "MANN",
                "article": "W6103",
                "price": "391.60",
                "quantity": "1",
                "days": 1,
                "warehouse": "SPB",
            },
            clean_article,
            brand_key,
        )

        result = deduplicate_offers([first, second], clean_article)

        self.assertEqual(len(result), 2)
        self.assertEqual({item["warehouse"] for item in result}, {"MSK", "SPB"})

    def test_same_warehouse_different_supplier_offer_id_is_not_merged(self):
        first = normalize_offer(
            {
                "provider": "Supplier A",
                "brand": "MANN",
                "article": "W6103",
                "price": "391.60",
                "quantity": "2",
                "delivery_total_hours": 0,
                "warehouse": "MSK",
                "offer_id": "one",
            },
            clean_article,
            brand_key,
        )
        second = normalize_offer(
            {
                "provider": "Supplier A",
                "brand": "MANN",
                "article": "W6103",
                "price": "391.60",
                "quantity": "2",
                "delivery_total_hours": 0,
                "warehouse": "MSK",
                "offer_id": "two",
            },
            clean_article,
            brand_key,
        )

        self.assertNotEqual(offer_duplicate_key(first, clean_article), offer_duplicate_key(second, clean_article))
        self.assertEqual(len(deduplicate_offers([first, second], clean_article)), 2)

    def test_duplicate_preference_uses_quantity_probability_freshness_and_returnable(self):
        base = {
            "provider": "Supplier A",
            "brand": "MANN",
            "article": "W6103",
            "price": "391.60",
            "delivery_total_hours": 24,
            "warehouse": "MSK",
        }
        low_stock = normalize_offer({**base, "quantity": "1", "delivery_probability": "99"}, clean_article, brand_key)
        high_stock = normalize_offer({**base, "quantity": "4", "delivery_probability": "90"}, clean_article, brand_key)
        chosen = deduplicate_offers([low_stock, high_stock], clean_article)
        self.assertEqual(chosen[0]["available_quantity"], 4)

        low_probability = normalize_offer({**base, "quantity": "4", "delivery_probability": "50"}, clean_article, brand_key)
        high_probability = normalize_offer({**base, "quantity": "4", "delivery_probability": "98"}, clean_article, brand_key)
        chosen = deduplicate_offers([low_probability, high_probability], clean_article)
        self.assertEqual(chosen[0]["delivery_probability"], "98")

        old = normalize_offer({**base, "quantity": "4", "delivery_probability": "98", "updated": "2026-07-20"}, clean_article, brand_key)
        fresh = normalize_offer({**base, "quantity": "4", "delivery_probability": "98", "updated": "2026-07-26"}, clean_article, brand_key)
        chosen = deduplicate_offers([old, fresh], clean_article)
        self.assertEqual(chosen[0]["updated_at"], "2026-07-26")

        no_return = normalize_offer({**base, "quantity": "4", "delivery_probability": "98", "updated": "2026-07-26", "allow_return": "0"}, clean_article, brand_key)
        returnable = normalize_offer({**base, "quantity": "4", "delivery_probability": "98", "updated": "2026-07-26", "allow_return": "1"}, clean_article, brand_key)
        chosen = deduplicate_offers([no_return, returnable], clean_article)
        self.assertTrue(chosen[0]["returnable"])

    def test_best_offer_prefers_fast_reliable_offer_over_cheapest_slow_offer(self):
        slow = normalize_offer(
            {
                "provider": "A",
                "brand": "MANN",
                "article": "W6103",
                "price": 350,
                "quantity": 10,
                "days": 8,
                "delivery_probability": "55",
            },
            clean_article,
            brand_key,
        )
        fast = normalize_offer(
            {
                "provider": "B",
                "brand": "MANN",
                "article": "W6103",
                "price": 390,
                "quantity": 10,
                "days": 1,
                "delivery_probability": "98",
            },
            clean_article,
            brand_key,
        )

        marked = mark_best_offers([slow, fast])
        best = [item for item in marked if item["is_best_offer"]]

        self.assertEqual(len(best), 1)
        self.assertEqual(best[0]["provider"], "B")
        self.assertIn("минимальный срок", best[0]["best_offer_reasons"])
        self.assertIn("высокая вероятность", best[0]["best_offer_reasons"])

    def test_popular_analog_is_marked_when_multiple_suppliers_return_it(self):
        first = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W6103", "price": 500, "quantity": 5, "days": 2, "is_cross": True},
            clean_article,
            brand_key,
        )
        second = normalize_offer(
            {"provider": "B", "brand": "MANN", "article": "W6103", "price": 510, "quantity": 5, "days": 2, "is_cross": True},
            clean_article,
            brand_key,
        )
        single = normalize_offer(
            {"provider": "C", "brand": "FILTRON", "article": "OP570", "price": 450, "quantity": 5, "days": 2, "is_cross": True},
            clean_article,
            brand_key,
        )

        marked = mark_best_offers([first, second, single])
        popular = [item for item in marked if item["normalized_article"] == "W6103"]
        other = [item for item in marked if item["normalized_article"] == "OP570"][0]

        self.assertTrue(all(item["is_popular_analog"] for item in popular))
        self.assertEqual({item["analog_provider_count"] for item in popular}, {2})
        self.assertEqual({item["provider_confirm_count"] for item in popular}, {2})
        self.assertFalse(other["is_popular_analog"])
        self.assertIn("подтвердили поставщики: 2", popular[0]["best_offer_reasons"])

    def test_exact_offer_is_marked_when_multiple_suppliers_confirm_it(self):
        first = normalize_offer(
            {"provider": "A", "brand": "ZIC", "article": "162622", "price": 850, "quantity": 5, "days": 2},
            clean_article,
            brand_key,
        )
        second = normalize_offer(
            {"provider": "B", "brand": "ZIC", "article": "162622", "price": 880, "quantity": 5, "days": 2},
            clean_article,
            brand_key,
        )

        marked = mark_best_offers([first, second])

        self.assertTrue(all(item["is_confirmed_by_suppliers"] for item in marked))
        self.assertEqual({item["provider_confirm_count"] for item in marked}, {2})

    def test_requested_part_is_decided_by_article_and_brand_only(self):
        same_brand = lambda left, right: brand_key(left) == brand_key(right)
        request = ("W71295", "MANN")

        exact = {"article": "W-712/95", "brand": "MANN"}
        cross_flagged = {"article": "W-712/95", "brand": "MANN", "is_cross": True}
        other_article = {"article": "W71296", "brand": "MANN"}
        other_brand = {"article": "W71295", "brand": "KNECHT"}

        self.assertTrue(is_requested_part(exact, *request, clean_article, same_brand))
        # флаг поставщика не участвует в решении
        self.assertTrue(is_requested_part(cross_flagged, *request, clean_article, same_brand))
        self.assertFalse(is_requested_part(other_article, *request, clean_article, same_brand))
        self.assertFalse(is_requested_part(other_brand, *request, clean_article, same_brand))

    def test_requested_part_tolerates_missing_values(self):
        same_brand = lambda left, right: brand_key(left) == brand_key(right)

        # бренд не выбран — решает только артикул
        self.assertTrue(is_requested_part({"article": "W71295", "brand": "KNECHT"}, "W71295", "", clean_article, same_brand))
        # у позиции нет бренда — не обвиняем её в несовпадении
        self.assertTrue(is_requested_part({"article": "W71295", "brand": ""}, "W71295", "MANN", clean_article, same_brand))

    def test_offer_relation_is_either_exact_or_analog(self):
        exact = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W1", "price": 1, "quantity": 1},
            clean_article,
            brand_key,
        )
        analog = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W3", "price": 1, "quantity": 1, "is_cross": True},
            clean_article,
            brand_key,
        )

        self.assertEqual(exact["offer_relation"], "exact")
        self.assertEqual(exact["offer_relation_label"], "")
        self.assertEqual(analog["offer_relation"], "analog")
        self.assertEqual(analog["offer_relation_label"], "АН")

    def test_provider_subtypes_no_longer_split_the_relation(self):
        replacement = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W2", "price": 1, "quantity": 1, "is_cross": True, "cross_relation": "замена"},
            clean_article,
            brand_key,
        )
        cross = normalize_offer(
            {"provider": "A", "brand": "MANN", "article": "W4", "price": 1, "quantity": 1, "is_cross": True, "source_code": "W0"},
            clean_article,
            brand_key,
        )

        self.assertEqual(replacement["offer_relation"], "analog")
        self.assertEqual(cross["offer_relation"], "analog")

    def test_provider_is_original_flag_does_not_override_our_comparison(self):
        """Регресс: Armtek отдаёт is_original для позиции, которую мы сочли аналогом.

        Раньше подтип «original» выигрывал и выпадал из приоритетов кросс-целей.
        """
        item = normalize_offer(
            {"provider": "Armtek", "brand": "MANN", "article": "W9", "price": 1, "quantity": 1,
             "is_original": True, "is_cross": True},
            clean_article,
            brand_key,
        )

        self.assertEqual(item["offer_relation"], "analog")

    def test_relation_style_makes_best_and_analog_labels_visible(self):
        best = relation_style("exact", is_best=True)
        analog = relation_style("analog", is_best=False)

        self.assertTrue(best["bold"])
        self.assertEqual(best["background"], "#FFF7ED")
        self.assertTrue(analog["bold"])
        self.assertEqual(analog["color"], "#7C3AED")


if __name__ == "__main__":
    unittest.main()
