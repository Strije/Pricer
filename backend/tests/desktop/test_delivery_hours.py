import unittest

from ._compat import SkitchenApp


class DeliveryHoursTests(unittest.TestCase):
    def _window(self, rules=None):
        window = SkitchenApp.__new__(SkitchenApp)
        window.warehouse_extra_days = rules or {}
        return window

    def test_warehouse_extra_preserves_exact_provider_hours_without_extra(self):
        window = self._window({"Profit-League": {}})
        item = {
            "provider": "Profit-League",
            "warehouse": "Ростов 1 (Батайск)",
            "days": 2,
            "delivery_hours": 42,
            "delivery_total_hours": 42,
            "delivery_display": "42 ч.",
        }

        window._apply_warehouse_extra_days("Profit-League", item)

        self.assertEqual(item["supplier_hours"], 42)
        self.assertEqual(item["delivery_hours"], 42)
        self.assertEqual(item["delivery_total_hours"], 42)
        self.assertEqual(item["delivery_display"], "2 дн.")

    def test_warehouse_extra_adds_to_exact_provider_hours(self):
        window = self._window({"Profit-League": {"РОСТОВ1БАТАЙСК": 24}})
        item = {
            "provider": "Profit-League",
            "warehouse": "Ростов 1 (Батайск)",
            "days": 2,
            "delivery_hours": 42,
            "delivery_total_hours": 42,
            "delivery_display": "42 ч.",
        }

        window._apply_warehouse_extra_days("Profit-League", item)

        self.assertEqual(item["supplier_hours"], 42)
        self.assertEqual(item["warehouse_extra_hours"], 24)
        self.assertEqual(item["delivery_hours"], 66)
        self.assertEqual(item["delivery_display"], "3 дн.")

    def test_delivery_display_uses_hours_until_full_day_then_days(self):
        window = self._window()

        self.assertEqual(window._format_delivery_hours(0), "0 ч")
        self.assertEqual(window._format_delivery_hours(24), "24 ч")
        self.assertEqual(window._format_delivery_hours(25), "2 дн.")
        self.assertEqual(window._format_delivery_hours(68), "3 дн.")


if __name__ == "__main__":
    unittest.main()
