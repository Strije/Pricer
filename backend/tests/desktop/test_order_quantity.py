import unittest

from order_quantity import format_quantity, parse_available_quantity, quantity_from_item


class OrderQuantityTests(unittest.TestCase):
    def test_rounds_requested_to_minimum_and_step(self):
        cases = [
            (1, 1, 1, 1),
            (1, 5, 1, 5),
            (1, 5, 5, 5),
            (6, 5, 5, 10),
            (10, 5, 5, 10),
            (11, 5, 5, 15),
        ]
        for requested, minimum, step, expected in cases:
            with self.subTest(requested=requested, minimum=minimum, step=step):
                info = quantity_from_item(
                    {
                        "quantity": "100",
                        "minimum_quantity": minimum,
                        "multiplicity": step,
                    },
                    requested,
                )
                self.assertTrue(info.can_order)
                self.assertEqual(info.actual_int(), expected)

    def test_marks_offer_unavailable_when_rounded_quantity_exceeds_stock(self):
        info = quantity_from_item(
            {"quantity": "7", "minimum_quantity": 5, "multiplicity": 5},
            6,
        )

        self.assertFalse(info.can_order)
        self.assertEqual(info.actual_int(), 10)
        self.assertIn("доступно 7", info.reason)

    def test_greater_than_quantity_is_not_zero_stock(self):
        available, lower_bound, orderable = parse_available_quantity(">10")

        self.assertEqual(format_quantity(available), "10")
        self.assertTrue(lower_bound)
        self.assertTrue(orderable)

    def test_unknown_stock_is_not_infinite(self):
        info = quantity_from_item({"quantity": ""}, 1)

        self.assertFalse(info.can_order)
        self.assertEqual(info.reason, "остаток неизвестен")

    def test_fractional_step_is_supported_by_core_calculation(self):
        info = quantity_from_item(
            {"quantity": "3", "minimum_quantity": "0.5", "quantity_step": "0.5"},
            "1.2",
        )

        self.assertTrue(info.can_order)
        self.assertEqual(format_quantity(info.actual_order_quantity), "1.5")


if __name__ == "__main__":
    unittest.main()
