import unittest

from result_limiter import limit_provider_results, normalize_provider_limits


def clean_num(value):
    return "".join(char for char in str(value or "").upper() if char.isalnum())


def brand_key(value):
    return clean_num(value)


def same_brand(left, right):
    return brand_key(left) == brand_key(right)


def offer(article, brand="MANN", price=100, days=1, quantity=1, **extra):
    data = {
        "provider": "Supplier",
        "article": article,
        "brand": brand,
        "price": price,
        "days": days,
        "quantity": quantity,
    }
    data.update(extra)
    return data


class ResultLimiterTests(unittest.TestCase):
    def test_normalizes_limits_and_clamps_values(self):
        limits = normalize_provider_limits(
            {
                "direct_result_limit": "500000000",
                "repeat_result_limit": "500000000",
                "analog_result_limit": "500000000",
                "result_limit_sort": "bad-mode",
            }
        )

        self.assertEqual(limits["direct_result_limit"], 50)
        self.assertEqual(limits["repeat_result_limit"], 5)
        self.assertEqual(limits["analog_result_limit"], 250)
        self.assertEqual(limits["result_limit_sort"], "delivery_price")

    def test_limits_direct_requested_article_by_delivery_then_price(self):
        rows = [
            offer("OC90", price=500, days=2),
            offer("OC90", price=450, days=1),
            offer("OC90", price=300, days=3),
        ]

        limited, stats = limit_provider_results(
            rows,
            "OC90",
            "MANN",
            same_brand=same_brand,
            brand_key=brand_key,
            clean_num=clean_num,
            config={"direct_result_limit": 2, "result_limit_sort": "delivery_price"},
        )

        self.assertEqual([item["price"] for item in limited], [450, 500])
        self.assertEqual(stats["direct_input"], 3)
        self.assertEqual(stats["direct_output"], 2)
        self.assertEqual(stats["dropped_count"], 1)

    def test_price_mode_prefers_cheaper_direct_offer(self):
        rows = [
            offer("OC90", price=500, days=0),
            offer("OC90", price=300, days=3),
        ]

        limited, _stats = limit_provider_results(
            rows,
            "OC90",
            "MANN",
            same_brand=same_brand,
            brand_key=brand_key,
            clean_num=clean_num,
            config={"direct_result_limit": 1, "result_limit_sort": "price_delivery"},
        )

        self.assertEqual(limited[0]["price"], 300)

    def test_limits_analog_groups_and_repeats_inside_each_group(self):
        rows = [
            offer("OC90", price=500, days=0),
            offer("W6103", brand="MANN", price=410, days=0),
            offer("W6103", brand="MANN", price=400, days=2),
            offer("W6103", brand="MANN", price=390, days=3),
            offer("W71275", brand="MANN", price=350, days=1),
            offer("LF1005", brand="LYNXAUTO", price=300, days=2),
        ]

        limited, stats = limit_provider_results(
            rows,
            "OC90",
            "MANN",
            same_brand=same_brand,
            brand_key=brand_key,
            clean_num=clean_num,
            config={
                "direct_result_limit": 5,
                "repeat_result_limit": 2,
                "analog_result_limit": 2,
                "result_limit_sort": "delivery_price",
            },
        )

        analog_keys = [(item["brand"], item["article"]) for item in limited if item["article"] != "OC90"]
        self.assertIn(("MANN", "W6103"), analog_keys)
        self.assertIn(("MANN", "W71275"), analog_keys)
        self.assertEqual(sum(1 for item in limited if item["article"] == "W6103"), 2)
        self.assertNotIn(("LYNXAUTO", "LF1005"), analog_keys)
        self.assertEqual(stats["analog_group_input"], 3)
        self.assertEqual(stats["analog_group_output"], 2)

    def test_same_article_different_selected_brand_is_analog(self):
        rows = [
            offer("OC90", brand="KNECHT", price=500, days=0),
            offer("OC90", brand="MAHLE", price=450, days=0),
        ]

        limited, stats = limit_provider_results(
            rows,
            "OC90",
            "KNECHT",
            same_brand=same_brand,
            brand_key=brand_key,
            clean_num=clean_num,
            config={"direct_result_limit": 5, "repeat_result_limit": 5, "analog_result_limit": 5},
        )

        self.assertEqual([item["brand"] for item in limited], ["MAHLE", "KNECHT"])
        self.assertEqual(stats["direct_input"], 1)
        self.assertEqual(stats["analog_group_input"], 1)

    def test_stock_mode_prefers_more_available_quantity(self):
        rows = [
            offer("OC90", price=300, days=1, quantity=1),
            offer("OC90", price=400, days=2, quantity=">10"),
        ]

        limited, _stats = limit_provider_results(
            rows,
            "OC90",
            "MANN",
            same_brand=same_brand,
            brand_key=brand_key,
            clean_num=clean_num,
            config={"direct_result_limit": 1, "result_limit_sort": "stock_delivery_price"},
        )

        self.assertEqual(limited[0]["quantity"], ">10")


if __name__ == "__main__":
    unittest.main()
