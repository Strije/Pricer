import json
import tempfile
import unittest
from pathlib import Path

from order_history_store import OrderHistoryStore


class OrderHistoryStoreTests(unittest.TestCase):
    def test_append_saves_required_order_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.json"
            store = OrderHistoryStore(str(path))
            record = store.append(
                {
                    "internal_offer_id": "local-1",
                    "supplier_offer_id": "supplier-1",
                    "provider": "Supplier",
                    "brand": "MANN",
                    "article": "W6103",
                    "warehouse": "MSK",
                    "purchase_price": 391.6,
                },
                2,
                {"success": True, "data": {"order": "42"}},
            )

            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(record["internal_offer_id"], "local-1")
        self.assertEqual(saved[0]["supplier_offer_id"], "supplier-1")
        self.assertEqual(saved[0]["provider"], "Supplier")
        self.assertEqual(saved[0]["brand"], "MANN")
        self.assertEqual(saved[0]["article"], "W6103")
        self.assertEqual(saved[0]["warehouse"], "MSK")
        self.assertEqual(saved[0]["price"], 391.6)
        self.assertEqual(saved[0]["quantity"], 2)
        self.assertEqual(saved[0]["response"]["success"], True)
        self.assertTrue(saved[0]["submitted_at"])


if __name__ == "__main__":
    unittest.main()
