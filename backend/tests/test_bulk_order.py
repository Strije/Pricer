import unittest

from bulk_order import filter_exact_offers, rank_offers, select_best_offer, status_for_selection


def clean_num(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def same_brand(left, right):
    return clean_num(left).replace("FILTER", "") == clean_num(right).replace("FILTER", "")


class BulkOrderTest(unittest.TestCase):
    def test_exact_filter_rejects_wrong_brand_or_article(self):
        row = {"article": "W 712/95", "article_key": "W71295", "brand": "MANN-FILTER"}
        offers = [
            {"article": "W-712/95", "brand": "MANN FILTER", "can_order_quantity": True},
            {"article": "W71295", "brand": "KNECHT", "can_order_quantity": True},
            {"article": "W71296", "brand": "MANN", "can_order_quantity": True},
            {"article": "W71295", "brand": "MANN", "can_order_quantity": False},
        ]

        exact, stats = filter_exact_offers(row, offers, clean_num, same_brand)

        self.assertEqual(len(exact), 1)
        self.assertEqual(stats["wrong_brand"], 1)
        self.assertEqual(stats["wrong_article"], 1)
        self.assertEqual(stats["unavailable"], 1)
        self.assertEqual(stats["exact_count"], 2)
        self.assertEqual(stats["accepted_count"], 1)
        self.assertEqual(stats["samples"]["wrong_article"][0]["article_key"], "W71296")

    def test_matching_article_and_brand_win_over_provider_cross_flag(self):
        """Регресс: поставщик пометил кроссом позицию, совпавшую с запросом.

        Так Profit-League отдавал точное совпадение из эндпоинта кроссов, и
        самое дешёвое предложение выбрасывалось из отбора.
        """
        row = {"article": "W 712/95", "article_key": "W71295", "brand": "MANN-FILTER"}
        offers = [
            {"article": "W71295", "brand": "MANN", "price": 900, "can_order_quantity": True},
            {"article": "W71295", "brand": "MANN", "price": 700, "is_cross": True, "can_order_quantity": True},
        ]

        exact, stats = filter_exact_offers(row, offers, clean_num, same_brand)

        self.assertEqual(len(exact), 2)
        self.assertEqual(stats["exact_count"], 2)
        # флаг поставщика по-прежнему виден в диагностике, но ничего не решает
        self.assertEqual(stats["cross"], 1)
        self.assertEqual(select_best_offer(exact, "price")["price"], 700)

    def test_non_exact_filter_allows_provider_analogs(self):
        row = {"article": "W 712/95", "article_key": "W71295", "brand": "MANN-FILTER"}
        offers = [
            {"article": "W71295", "brand": "MANN FILTER", "can_order_quantity": True},
            {"article": "OP570", "brand": "FILTRON", "is_cross": True, "can_order_quantity": True},
        ]

        accepted, stats = filter_exact_offers(
            row,
            offers,
            clean_num,
            same_brand,
            exact_match=False,
        )

        self.assertEqual(len(accepted), 2)
        self.assertEqual(stats["mode"], "with_analogs")
        self.assertEqual(stats["wrong_article"], 1)
        self.assertEqual(stats["wrong_brand"], 1)
        self.assertEqual(stats["cross"], 1)
        self.assertEqual(stats["accepted_count"], 2)

    def test_select_best_by_price_or_fastest(self):
        offers = [
            {"provider": "A", "price": 350, "delivery_hours": 72, "available_quantity": 5},
            {"provider": "B", "price": 390, "delivery_hours": 24, "available_quantity": 5},
        ]

        self.assertEqual(select_best_offer(offers, "price")["provider"], "A")
        self.assertEqual(select_best_offer(offers, "fastest")["provider"], "B")

    def test_select_best_respects_delivery_limit(self):
        offers = [
            {"provider": "A", "price": 350, "delivery_hours": 72, "available_quantity": 5},
            {"provider": "B", "price": 390, "delivery_hours": 24, "available_quantity": 5},
        ]

        selected = select_best_offer(offers, "price_within_days", max_delivery_hours=48)

        self.assertEqual(selected["provider"], "B")

    def test_select_best_prefers_requested_quantity_over_bulk_pack(self):
        offers = [
            {
                "provider": "bulk",
                "price": 138,
                "delivery_hours": 288,
                "available_quantity": 504,
                "requested_quantity": 2,
                "actual_order_quantity": 24,
            },
            {
                "provider": "exact",
                "price": 161,
                "delivery_hours": 120,
                "available_quantity": 92,
                "requested_quantity": 2,
                "actual_order_quantity": 2,
            },
        ]

        selected = select_best_offer(offers, "price")

        self.assertEqual(selected["provider"], "exact")

    def test_select_best_prefers_requested_quantity_even_for_fastest_strategy(self):
        offers = [
            {
                "provider": "fast-bulk",
                "price": 100,
                "delivery_hours": 24,
                "available_quantity": 100,
                "requested_quantity": 2,
                "actual_order_quantity": 24,
            },
            {
                "provider": "slow-exact",
                "price": 180,
                "delivery_hours": 72,
                "available_quantity": 5,
                "requested_quantity": 2,
                "actual_order_quantity": 2,
            },
        ]

        selected = select_best_offer(offers, "fastest")
        ranked = rank_offers(offers, "fastest")

        self.assertEqual(selected["provider"], "slow-exact")
        self.assertEqual([offer["provider"] for offer in ranked], ["slow-exact", "fast-bulk"])

    def test_status_texts(self):
        self.assertEqual(status_for_selection({}, 0, 0, None)[1], "Нет точного совпадения")
        self.assertEqual(
            status_for_selection({}, 1, 0, None)[1],
            "Недостаточно количества или данных заказа",
        )
        self.assertEqual(status_for_selection({}, 1, 1, {"provider": "A"})[1], "Готово")


if __name__ == "__main__":
    unittest.main()
