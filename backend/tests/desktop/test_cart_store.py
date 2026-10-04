import unittest

from cart_store import DraftCart


class DraftCartTests(unittest.TestCase):
    def test_internal_offer_id_is_used_for_matching(self):
        cart = DraftCart()
        first = {"internal_offer_id": "offer-1", "provider": "A", "brand": "MANN", "article": "W6103"}
        second = {"internal_offer_id": "offer-2", "provider": "A", "brand": "MANN", "article": "W6103"}

        cart.add("offer-1", first, 1)
        cart.add("offer-2", second, 1)
        removed = cart.remove_matching(first)

        self.assertEqual(removed, 1)
        self.assertEqual(len(cart.rows()), 1)
        self.assertEqual(cart.rows()[0]["key"], "offer-2")


if __name__ == "__main__":
    unittest.main()
