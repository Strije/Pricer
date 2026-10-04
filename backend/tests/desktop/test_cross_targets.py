import unittest

from cross_targets import build_cross_targets


def clean_num(value):
    return "".join(char for char in str(value or "").upper() if char.isalnum())


def brand_key(value):
    return clean_num(value)


def same_brand(left, right):
    return brand_key(left) == brand_key(right)


class CrossTargetsTests(unittest.TestCase):
    def test_groups_by_brand_and_article_not_article_only(self):
        rows = [
            {
                "provider": "A",
                "brand": "MANN",
                "article": "W6103",
                "is_cross": True,
                "price": 400,
                "days": 2,
            },
            {
                "provider": "B",
                "brand": "BOSCH",
                "article": "W6103",
                "is_cross": True,
                "price": 350,
                "days": 1,
            },
        ]

        targets, stats = build_cross_targets(
            rows,
            "OC90",
            "KNECHT",
            clean_num=clean_num,
            brand_key=brand_key,
            same_brand=same_brand,
        )

        self.assertEqual(stats["raw_cross_rows"], 2)
        self.assertEqual(stats["unique_candidates"], 2)
        self.assertEqual({(item["brand"], item["clean_article"]) for item in targets}, {("MANN", "W6103"), ("BOSCH", "W6103")})

    def test_ranks_provider_confirmed_crosses_first(self):
        rows = [
            {"provider": "A", "brand": "UNKNOWN", "article": "U1", "is_cross": True, "price": 100, "days": 0},
            {"provider": "A", "brand": "MANN", "article": "W6103", "is_cross": True, "price": 500, "days": 3},
            {"provider": "B", "brand": "MANN", "article": "W6103", "is_cross": True, "price": 600, "days": 4},
        ]

        targets, stats = build_cross_targets(
            rows,
            "OC90",
            "KNECHT",
            clean_num=clean_num,
            brand_key=brand_key,
            same_brand=same_brand,
        )

        self.assertEqual(targets[0]["brand"], "MANN")
        self.assertEqual(targets[0]["provider_confirm_count"], 2)
        self.assertEqual(targets[0]["confirmed_by"], ["A", "B"])
        self.assertEqual(stats["top_targets"][0]["provider_confirm_count"], 2)

    def test_skips_original_selected_brand_but_keeps_same_article_other_brand(self):
        rows = [
            {"provider": "A", "brand": "MANDO", "article": "MOF4459", "is_cross": True, "price": 100},
            {"provider": "B", "brand": "HL MANDO", "article": "MOF4459", "is_cross": True, "price": 120},
            {"provider": "C", "brand": "HYUNDAI", "article": "MOF4459", "is_cross": True, "price": 150},
        ]

        targets, stats = build_cross_targets(
            rows,
            "MOF4459",
            "MANDO",
            clean_num=clean_num,
            brand_key=brand_key,
            same_brand=lambda left, right: brand_key(left).replace("HL", "") == brand_key(right),
        )

        self.assertEqual(stats["skipped_original"], 2)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["brand"], "HYUNDAI")

    def test_limit_is_optional_and_logged_when_used(self):
        rows = [
            {"provider": "A", "brand": f"B{i}", "article": f"A{i}", "is_cross": True, "price": i}
            for i in range(5)
        ]

        targets, stats = build_cross_targets(
            rows,
            "OC90",
            clean_num=clean_num,
            brand_key=brand_key,
            same_brand=same_brand,
            max_targets=2,
        )

        self.assertEqual(len(targets), 2)
        self.assertEqual(stats["dropped_by_limit"], 3)


if __name__ == "__main__":
    unittest.main()
