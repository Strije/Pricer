import unittest

from price_names import humanize_price_name


class PriceNamesTests(unittest.TestCase):
    def test_humanizes_machine_price_names(self):
        self.assertEqual(
            humanize_price_name("Прайс armtek-rostov"),
            "Прайс Армтек Ростов",
        )
        self.assertEqual(
            humanize_price_name("profit-liga-msk", force_prefix=True),
            "Прайс Profit Лига Москва",
        )

    def test_keeps_custom_names(self):
        self.assertEqual(
            humanize_price_name("Armtek Ростов"),
            "Armtek Ростов",
        )


if __name__ == "__main__":
    unittest.main()
