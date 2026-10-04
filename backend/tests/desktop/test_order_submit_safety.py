import unittest

from ._compat import SkitchenApp


class OrderSubmitSafetyTest(unittest.TestCase):
    def test_order_submit_lock_blocks_duplicate_order(self):
        app = SkitchenApp.__new__(SkitchenApp)
        app._submitting_order_ids = set()

        self.assertTrue(app._try_lock_order_submit("ORD-1"))
        self.assertFalse(app._try_lock_order_submit("ORD-1"))

        app._unlock_order_submit("ORD-1")

        self.assertTrue(app._try_lock_order_submit("ORD-1"))

    def test_order_submit_idempotency_key_is_stable_for_selected_indexes(self):
        app = SkitchenApp.__new__(SkitchenApp)

        self.assertEqual(app._order_submit_idempotency_key("ORD-1"), "ORD-1:all")
        self.assertEqual(
            app._order_submit_idempotency_key("ORD-1", [3, 1, 3]),
            "ORD-1:1,3",
        )


if __name__ == "__main__":
    unittest.main()
