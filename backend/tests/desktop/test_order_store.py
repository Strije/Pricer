import tempfile
import unittest
from pathlib import Path

from order_store import OrderStore


class OrderStoreTests(unittest.TestCase):
    def test_create_draft_writes_one_json_file_with_verification_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            order = store.create_draft(
                manager="Иван",
                client="ООО Ромашка",
                ship_date="2026-07-30",
                comment="Клиент думает",
                client_vin="xta 123 456",
                entries=[
                    {
                        "qty": 2,
                        "item": {
                            "internal_offer_id": "local-1",
                            "supplier_offer_id": "supplier-1",
                            "provider": "Supplier",
                            "brand": "MANN",
                            "article": "W6103",
                            "warehouse": "MSK",
                            "purchase_price": 391.6,
                            "order_sale_price": 500.0,
                            "delivery_hours": 48,
                        },
                    }
                ],
            )
            files = list(Path(directory).glob("*.json"))
            loaded = store.read(order["order_id"])

        self.assertEqual(len(files), 1)
        self.assertEqual(order["status"], "draft")
        self.assertEqual(order["verification_status"], "stale")
        self.assertEqual(order["verified_at"], "")
        self.assertEqual(order["manager"]["name"], "Иван")
        self.assertEqual(order["client"]["name"], "ООО Ромашка")
        self.assertEqual(order["client"]["vin"], "XTA123456")
        self.assertEqual(order["comment"], "Клиент думает")
        self.assertEqual(order["items"][0]["internal_offer_id"], "local-1")
        self.assertEqual(order["items"][0]["submit_status"], "not_submitted")
        self.assertEqual(order["items"][0]["purchase_price"], 391.6)
        self.assertEqual(order["items"][0]["sale_price"], 500.0)
        self.assertEqual(order["items"][0]["purchase_total"], 783.2)
        self.assertEqual(order["items"][0]["sale_total"], 1000.0)
        self.assertEqual(order["items"][0]["margin"], 216.8)
        self.assertEqual(order["totals"]["purchase_total"], 783.2)
        self.assertEqual(order["totals"]["sale_total"], 1000.0)
        self.assertEqual(order["totals"]["margin"], 216.8)
        self.assertEqual(order["supplier_totals"][0]["provider"], "Supplier")
        self.assertEqual(order["supplier_totals"][0]["margin"], 216.8)
        self.assertIn("snapshot", order["items"][0])
        self.assertEqual(loaded["order_id"], order["order_id"])
        self.assertEqual(loaded["client"]["vin"], "XTA123456")

    def test_order_ids_increment_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            first = store.create_draft("A", "Client", "2026-07-30", [])
            second = store.create_draft("A", "Client", "2026-07-30", [])

        self.assertNotEqual(first["order_id"], second["order_id"])
        self.assertTrue(first["order_id"].endswith("0001"))
        self.assertTrue(second["order_id"].endswith("0002"))

    def test_create_draft_can_mark_fresh_entries_as_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            order = store.create_draft(
                manager="Иван",
                client="ООО Ромашка",
                ship_date="2026-07-30",
                verified_at="2026-07-30T12:00:00",
                entries=[
                    {
                        "qty": 1,
                        "item": {
                            "provider": "Supplier",
                            "brand": "MANN",
                            "article": "W6103",
                            "price": 100,
                        },
                    }
                ],
            )

        self.assertEqual(order["verification_status"], "valid")
        self.assertEqual(order["verified_at"], "2026-07-30T12:00:00")
        self.assertEqual(order["items"][0]["verification_status"], "valid")
        self.assertEqual(order["items"][0]["last_checked_at"], "2026-07-30T12:00:00")

    def test_create_draft_uses_entry_verification_time_when_all_items_are_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            order = store.create_draft(
                manager="Иван",
                client="ООО Ромашка",
                ship_date="2026-07-30",
                entries=[
                    {
                        "qty": 1,
                        "item": {
                            "provider": "Supplier",
                            "brand": "MANN",
                            "article": "W6103",
                            "price": 100,
                            "verification_status": "valid",
                            "verification_message": "проверено при поиске",
                            "last_checked_at": "2026-07-30T12:00:00",
                        },
                    }
                ],
            )

        self.assertEqual(order["verification_status"], "valid")
        self.assertEqual(order["verified_at"], "2026-07-30T12:00:00")
        self.assertEqual(order["items"][0]["verification_message"], "проверено при поиске")

    def test_create_draft_groups_manual_selection_by_requested_article(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            order = store.create_draft(
                manager="Иван",
                client="ООО Ромашка",
                ship_date="2026-07-30",
                entries=[
                    {
                        "qty": 1,
                        "item": {
                            "provider": "Avtoto",
                            "source_brand": "HYUNDAI-KIA",
                            "source_code": "0510000441",
                            "brand": "HYUNDAI-KIA",
                            "article": "0510000441",
                            "price": 3990,
                        },
                    },
                    {
                        "qty": 1,
                        "item": {
                            "provider": "Фаворит",
                            "source_brand": "HYUNDAI-KIA",
                            "source_code": "0510000441",
                            "brand": "HYUNDAI-KIA",
                            "article": "0510000441",
                            "price": 4603,
                        },
                    },
                ],
                grouping_mode="manual",
            )

        self.assertEqual(order["grouping_mode"], "manual")
        self.assertEqual(len(order["groups"]), 1)
        self.assertEqual(order["groups"][0]["requested"]["brand"], "HYUNDAI-KIA")
        self.assertEqual(order["groups"][0]["requested"]["article"], "0510000441")
        self.assertEqual(len(order["groups"][0]["offers"]), 2)
        self.assertEqual({offer["provider"] for offer in order["groups"][0]["offers"]}, {"Avtoto", "Фаворит"})

    def test_create_draft_keeps_selection_group_alternatives_outside_order_items(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            order = store.create_draft(
                manager="Иван",
                client="ООО Ромашка",
                ship_date="2026-07-30",
                entries=[
                    {
                        "qty": 2,
                        "item": {
                            "internal_offer_id": "chosen",
                            "provider": "Avtoto",
                            "source_brand": "FEBI BILSTEIN",
                            "source_code": "101171",
                            "source_name": "масло трансмиссионное",
                            "brand": "FEBI",
                            "article": "101171",
                            "purchase_price": 2502,
                            "order_sale_price": 3100,
                            "actual_order_quantity": 2,
                            "selection_group_offers": [
                                {
                                    "internal_offer_id": "chosen",
                                    "provider": "Avtoto",
                                    "brand": "FEBI",
                                    "article": "101171",
                                    "purchase_price": 2502,
                                    "sale_price": 3100,
                                    "actual_order_quantity": 2,
                                },
                                {
                                    "internal_offer_id": "alternative",
                                    "display_in_order": False,
                                    "provider": "Armtek",
                                    "brand": "FEBI BILSTEIN",
                                    "article": "101171",
                                    "part_id": "armtek-part-1",
                                    "purchase_price": 2985.52,
                                    "sale_price": 3600,
                                    "actual_order_quantity": 2,
                                    "delivery_hours": 24,
                                    "warehouse": "MSK",
                                    "selection_group_offers": [{"internal_offer_id": "nested"}],
                                },
                            ],
                        },
                    }
                ],
                grouping_mode="manual",
            )

        self.assertEqual(len(order["items"]), 1)
        offers = order["groups"][0]["offers"]
        self.assertEqual([offer["internal_offer_id"] for offer in offers], ["chosen", "alternative"])
        self.assertEqual([offer["selected"] for offer in offers], [True, False])
        self.assertEqual([offer["display_in_order"] for offer in offers], [True, False])
        self.assertEqual(offers[1]["provider"], "Armtek")
        self.assertEqual(offers[1]["snapshot"]["part_id"], "armtek-part-1")
        self.assertNotIn("selection_group_offers", offers[1]["snapshot"])
        self.assertEqual(order["groups"][0]["requested"]["name"], "масло трансмиссионное")
        self.assertEqual(order["items"][0]["search"]["requested_name"], "масло трансмиссионное")

    def test_file_order_draft_keeps_flat_mode_without_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            order = store.create_draft(
                manager="Иван",
                client="ООО Ромашка",
                ship_date="2026-07-30",
                entries=[
                    {
                        "qty": 1,
                        "item": {
                            "provider": "Avtoto",
                            "source_brand": "MAXI FRESH",
                            "source_code": "MF114",
                            "brand": "MAXI FRESH",
                            "article": "MF114",
                            "price": 220,
                        },
                    }
                ],
                grouping_mode="file",
            )

        self.assertEqual(order["grouping_mode"], "file")
        self.assertEqual(order["groups"], [])

    def test_create_draft_drops_ui_fields_and_serializes_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            order = store.create_draft(
                manager="Иван",
                client="ООО Ромашка",
                ship_date="2026-07-30",
                entries=[
                    {
                        "qty": 1,
                        "item": {
                            "provider": "Supplier",
                            "brand": "MANN",
                            "article": "W6103",
                            "price": 100,
                            "_bold_cols": {1, 2},
                            "public_tags": {"fast", "stock"},
                        },
                    }
                ],
            )
            loaded = store.read(order["order_id"])

        snapshot = loaded["items"][0]["snapshot"]
        self.assertNotIn("_bold_cols", snapshot)
        self.assertEqual(snapshot["public_tags"], ["fast", "stock"])

    def test_order_summaries_keep_filter_fields_without_heavy_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(directory)
            order = store.create_draft(
                manager="Иван",
                client="ООО Ромашка",
                ship_date="2026-07-30",
                client_vin="xta123",
                entries=[
                    {
                        "qty": 2,
                        "item": {
                            "internal_offer_id": "chosen",
                            "supplier_offer_id": "supplier-1",
                            "provider": "Avtoto",
                            "source_brand": "FEBI BILSTEIN",
                            "source_code": "101171",
                            "source_name": "масло трансмиссионное",
                            "brand": "FEBI",
                            "display_brand": "FEBI",
                            "article": "101171",
                            "name": "масло",
                            "purchase_price": 2502,
                            "order_sale_price": 3100,
                            "selection_group_offers": [
                                {
                                    "internal_offer_id": "archived",
                                    "provider": "Armtek",
                                    "brand": "FEBI",
                                    "article": "101171",
                                    "purchase_price": 2985.52,
                                }
                            ],
                            "raw_api_payload": {"large": ["x"] * 10},
                        },
                    }
                ],
            )
            summaries = store.list_order_summaries()

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertTrue(summary["summary_only"])
        self.assertEqual(summary["order_id"], order["order_id"])
        self.assertEqual(summary["client"]["vin"], "XTA123")
        self.assertEqual(summary["totals"]["quantity"], 2)
        self.assertEqual(summary["supplier_totals"][0]["provider"], "Avtoto")
        self.assertEqual(summary["items"][0]["provider"], "Avtoto")
        self.assertEqual(summary["items"][0]["brand"], "FEBI")
        self.assertEqual(summary["items"][0]["search"]["selected_brand"], "FEBI BILSTEIN")
        self.assertEqual(summary["items"][0]["search"]["requested_name"], "масло трансмиссионное")
        self.assertNotIn("snapshot", summary["items"][0])
        self.assertNotIn("raw_api_payload", summary["items"][0])
        self.assertEqual(summary["groups"][0]["requested"]["article"], "101171")
        self.assertEqual(summary["groups"][0]["offers"], [])



class OrderItemNameTests(unittest.TestCase):
    def test_item_keeps_the_product_name_from_the_offer(self):
        # Наименование приходит с поиском, но в заказ не попадало:
        # в файле оставались только бренд и артикул.
        entry = {
            "item": {
                "provider": "Avtoto",
                "brand": "MANN",
                "article": "W7015",
                "name": "Фильтр масляный",
                "price": 100,
                "quantity": "5",
            },
            "qty": 2,
        }

        item = OrderStore._item_from_entry(entry)

        self.assertEqual(item["name"], "Фильтр масляный")
        self.assertEqual(OrderStore._item_summary(item)["name"], "Фильтр масляный")

    def test_name_falls_back_to_the_order_file_line(self):
        entry = {
            "item": {
                "provider": "Avtoto",
                "brand": "MANN",
                "article": "W7015",
                "source_name": "Фильтр масляный из файла",
                "price": 100,
                "quantity": "5",
            },
            "qty": 1,
        }

        item = OrderStore._item_from_entry(entry)

        self.assertEqual(item["name"], "Фильтр масляный из файла")

if __name__ == "__main__":
    unittest.main()
