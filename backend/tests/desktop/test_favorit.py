import time
import unittest
from unittest.mock import Mock

import requests

from favorit import FavoritProvider


class FavoritProviderTests(unittest.TestCase):
    def test_normalizes_warehouse_price_and_stock(self):
        provider = FavoritProvider("secret", include_analogues=False)
        provider._request = Mock(
            return_value=[
                {
                    "goodsID": "g1",
                    "brand": "MEYLE",
                    "number": "7160500039S",
                    "name": "Detail",
                    "rate": 2,
                    "notRefund": False,
                    "warehouses": [
                        {"id": "w1", "code": "МС2", "price": 123.5, "stock": 20}
                    ],
                }
            ]
        )
        result = provider.get_prices("7160500039S")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider"], "Фаворит")
        self.assertEqual(result[0]["price"], 123.5)
        self.assertEqual(result[0]["quantity"], "20")
        self.assertEqual(result[0]["multiplicity"], 2)
        self.assertEqual(result[0]["warehouse"], "МС2")

    def test_brand_enables_analogues_request(self):
        provider = FavoritProvider("secret", include_analogues=True)
        provider._request = Mock(return_value=[])
        provider.get_prices("0189", brand="CAFFARO")
        self.assertEqual(
            provider._request.call_args_list[0].kwargs,
            {"brand": "CAFFARO", "analogues": True},
        )

    def test_retries_with_favorit_brand_alias(self):
        provider = FavoritProvider("secret", include_analogues=True)
        provider._request = Mock(side_effect=[
            [],
            [{"brand": "MANN-FILTER"}],
            [],
        ])

        provider.get_prices("A100", brand="MANN")

        self.assertEqual(provider._request.call_args_list[-1].kwargs["brand"], "MANN-FILTER")

    def test_empty_key_is_rejected(self):
        ok, message = FavoritProvider("").check_connection()
        self.assertFalse(ok)
        self.assertIn("API", message)

    def test_add_to_remote_cart_uses_goods_and_warehouse_ids(self):
        provider = FavoritProvider("client", developer_key="developer")
        response = Mock(status_code=200, content=b"", text="")
        provider.session.get = Mock(return_value=response)
        result = provider.add_to_remote_cart_only(
            {"goods_id": "g1", "warehouse_id": "w1"}, quantity=3, comment="test"
        )
        self.assertTrue(result["success"])
        _, kwargs = provider.session.get.call_args
        params = kwargs["params"]
        self.assertIn("goods=g1", params)
        self.assertIn("warehouseGroup=w1", params)
        self.assertIn("count=3", params)
        self.assertIn("developerKey=developer", params)

    def test_add_to_basket_creates_order_after_cart_add(self):
        provider = FavoritProvider("client", developer_key="developer")
        provider.add_to_remote_cart_only = Mock(return_value={"success": True})
        provider.get_cart = Mock(return_value={
            "cart": [{
                "goods": "g1",
                "warehouseGroup": "w1",
                "count": 3,
                "warehouseShipping": "main",
                "dateShipment": "2026-07-25",
            }]
        })
        provider.get_profile = Mock(return_value={"tradePointDef": "tp1"})
        response = Mock(status_code=200, content=b"{}", text="{}")
        response.json = Mock(return_value={"order": "ok"})
        provider.session.post = Mock(return_value=response)

        result = provider.add_to_basket(
            {"goods_id": "g1", "warehouse_id": "w1"}, quantity=3, comment="test"
        )

        self.assertTrue(result["success"])
        _, kwargs = provider.session.post.call_args
        self.assertEqual(kwargs["json"]["TradePoint"], "tp1")
        self.assertEqual(kwargs["json"]["GoodsList"][0]["Goods"], "g1")

    def test_reuses_shared_brand_aliases_instead_of_building_its_own(self):
        """Своя таблица алиасов весит ~2.8 МБ и строится ~0.5 с на провайдера."""
        shared = object()

        started = time.monotonic()
        provider = FavoritProvider("key", brand_aliases=shared)
        elapsed = time.monotonic() - started

        self.assertIs(provider.brand_aliases, shared)
        self.assertLess(elapsed, 0.2)



class FavoritBatchOrderTests(unittest.TestCase):
    def _provider(self, cart_rows, post=None):
        provider = FavoritProvider("client", developer_key="developer", timeout=12)
        provider.add_to_remote_cart_only = Mock(return_value={"success": True})
        provider.get_cart = Mock(return_value={"cart": list(cart_rows)})
        provider.get_profile = Mock(return_value={"tradePointDef": "tp1"})
        if post is None:
            response = Mock(status_code=200, content=b"{}", text="{}")
            response.json = Mock(return_value={"order": "ok"})
            post = Mock(return_value=response)
        provider.session.post = post
        return provider

    def _cart_row(self, goods, warehouse, shipping="main", date="2026-07-25", count=1):
        return {
            "goods": goods,
            "warehouseGroup": warehouse,
            "count": count,
            "warehouseShipping": shipping,
            "dateShipment": date,
        }

    def _row(self, goods, warehouse, quantity=1):
        return {"item": {"goods_id": goods, "warehouse_id": warehouse}, "quantity": quantity}

    def test_one_shipping_warehouse_gives_one_order(self):
        provider = self._provider([
            self._cart_row("g1", "w1"),
            self._cart_row("g2", "w1"),
            self._cart_row("g3", "w1"),
        ])

        results = provider.add_to_basket_batch(
            [self._row("g1", "w1"), self._row("g2", "w1", 2), self._row("g3", "w1")],
            comment="заказ",
        )

        self.assertEqual(provider.session.post.call_count, 1)
        goods_list = provider.session.post.call_args.kwargs["json"]["GoodsList"]
        self.assertEqual([row["Goods"] for row in goods_list], ["g1", "g2", "g3"])
        self.assertEqual([row["Count"] for row in goods_list], [1, 2, 1])
        self.assertTrue(all(result["success"] for result in results))
        self.assertEqual(provider.add_to_remote_cart_only.call_count, 3)

    def test_different_shipping_dates_go_in_separate_orders(self):
        # WarehouseShipping и ShippingDate — поля заказа, а не строки,
        # поэтому пакет режется по ним.
        provider = self._provider([
            self._cart_row("g1", "w1", shipping="main", date="2026-07-25"),
            self._cart_row("g2", "w2", shipping="main", date="2026-07-28"),
            self._cart_row("g3", "w3", shipping="south", date="2026-07-25"),
        ])

        results = provider.add_to_basket_batch(
            [self._row("g1", "w1"), self._row("g2", "w2"), self._row("g3", "w3")]
        )

        self.assertEqual(provider.session.post.call_count, 3)
        self.assertTrue(all(result["success"] for result in results))
        payloads = [call.kwargs["json"] for call in provider.session.post.call_args_list]
        self.assertEqual(
            sorted((payload["WarehouseShipping"], payload["ShippingDate"]) for payload in payloads),
            [("main", "20260725"), ("main", "20260728"), ("south", "20260725")],
        )

    def test_two_rows_on_one_offer_are_merged_into_one_cart_record(self):
        provider = self._provider([self._cart_row("g1", "w1", count=5)])

        results = provider.add_to_basket_batch([self._row("g1", "w1", 2), self._row("g1", "w1", 3)])

        self.assertEqual(provider.add_to_remote_cart_only.call_count, 1)
        self.assertEqual(provider.add_to_remote_cart_only.call_args.args[1], 5)
        goods_list = provider.session.post.call_args.kwargs["json"]["GoodsList"]
        self.assertEqual(len(goods_list), 1)
        self.assertEqual(goods_list[0]["Count"], 5)
        self.assertTrue(all(result["success"] for result in results))

    def test_order_count_comes_from_us_not_from_the_cart(self):
        # В корзине мог остаться хвост с прошлой отправки — заказывать нужно
        # ровно то количество, которое просили в этом заказе.
        provider = self._provider([self._cart_row("g1", "w1", count=9)])

        provider.add_to_basket_batch([self._row("g1", "w1", 2)])

        goods_list = provider.session.post.call_args.kwargs["json"]["GoodsList"]
        self.assertEqual(goods_list[0]["Count"], 2)

    def test_position_without_ids_fails_alone(self):
        provider = self._provider([self._cart_row("g2", "w2")])
        rows = [self._row("", ""), self._row("g2", "w2")]

        results = provider.add_to_basket_batch(rows)

        self.assertFalse(results[0]["success"])
        self.assertIn("идентификатора", results[0]["error"])
        self.assertTrue(results[1]["success"])
        self.assertEqual(provider.add_to_remote_cart_only.call_count, 1)

    def test_position_missing_in_cart_fails_alone(self):
        provider = self._provider([self._cart_row("g1", "w1")])

        results = provider.add_to_basket_batch([self._row("g1", "w1"), self._row("g2", "w2")])

        self.assertTrue(results[0]["success"])
        self.assertFalse(results[1]["success"])
        self.assertIn("не найдена в корзине", results[1]["error"])

    def test_failed_cart_add_keeps_its_own_error(self):
        provider = self._provider([self._cart_row("g2", "w2")])
        provider.add_to_remote_cart_only = Mock(side_effect=[
            {"success": False, "error": "нет на складе"},
            {"success": True},
        ])

        results = provider.add_to_basket_batch([self._row("g1", "w1"), self._row("g2", "w2")])

        self.assertEqual(results[0]["error"], "нет на складе")
        self.assertTrue(results[1]["success"])
        goods_list = provider.session.post.call_args.kwargs["json"]["GoodsList"]
        self.assertEqual([row["Goods"] for row in goods_list], ["g2"])

    def test_lost_answer_is_unknown_and_not_retried(self):
        attempts = []

        def _post(*args, **kwargs):
            attempts.append(kwargs.get("timeout"))
            raise requests.exceptions.ReadTimeout("read timeout")

        provider = self._provider(
            [self._cart_row("g1", "w1"), self._cart_row("g2", "w1")], post=Mock(side_effect=_post)
        )

        results = provider.add_to_basket_batch([self._row("g1", "w1"), self._row("g2", "w1")])

        self.assertEqual(len(attempts), 1, "заказ нельзя переспрашивать: будет дубль")
        self.assertEqual(attempts[0], provider.order_timeout)
        self.assertTrue(all(result["uncertain"] for result in results))
        self.assertTrue(all("проверьте заказы" in result["error"] for result in results))

    def test_failed_group_does_not_touch_the_other_group(self):
        responses = []
        ok = Mock(status_code=200, content=b"{}", text="{}")
        ok.json = Mock(return_value={"order": "ok"})
        bad = Mock(status_code=400, content=b"{}", text="отказ")
        bad.json = Mock(return_value={"error": "отказ"})

        def _post(*args, **kwargs):
            payload = kwargs["json"]
            responses.append(payload["ShippingDate"])
            return bad if payload["ShippingDate"] == "20260728" else ok

        provider = self._provider([
            self._cart_row("g1", "w1", date="2026-07-25"),
            self._cart_row("g2", "w2", date="2026-07-28"),
        ], post=Mock(side_effect=_post))

        results = provider.add_to_basket_batch([self._row("g1", "w1"), self._row("g2", "w2")])

        self.assertTrue(results[0]["success"])
        self.assertFalse(results[1]["success"])

    def test_missing_developer_key_stops_before_the_cart(self):
        provider = self._provider([self._cart_row("g1", "w1")])
        provider.developer_key = ""

        results = provider.add_to_basket_batch([self._row("g1", "w1"), self._row("g2", "w1")])

        self.assertEqual(len(results), 2)
        self.assertTrue(all("ключ разработчика" in result["error"] for result in results))
        provider.add_to_remote_cart_only.assert_not_called()


if __name__ == "__main__":
    unittest.main()
