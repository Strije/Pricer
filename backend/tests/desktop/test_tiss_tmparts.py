import unittest
from unittest.mock import Mock, patch

import requests

from tiss_tmparts import TissTmpartsProvider


class TissTmpartsProviderTests(unittest.TestCase):
    def test_uses_supplier_http_endpoint(self):
        provider = TissTmpartsProvider("token")
        self.assertEqual(provider.base_url, "https://api.tiss.ru/external")

    def test_get_prices_resolves_brands_and_normalizes_offers(self):
        provider = TissTmpartsProvider("token", contract_id="contract-1", outlet_id="outlet-1")
        provider._request = Mock(
            side_effect=[
                [{"brandName": "MAHLE"}],
                {
                    "OriginalProduct": {
                        "items": [{
                            "brandName": "MAHLE",
                            "displayProductCode": "OC727",
                            "productName": "Oil filter",
                            "productId": "product-1",
                            "offeringBlockType": "OriginalProduct",
                            "offers": [{
                                "priceTemplateId": "offer-1",
                                "price": "123.45",
                                "amount": "7",
                                "minPackSize": "2",
                                "warehouseName": "В наличии",
                                "deliveryInfo": {"workDays": "3"},
                            }],
                        }]
                    }
                },
            ]
        )
        result = provider.get_prices("OC727")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider"], "TISS")
        self.assertEqual(result[0]["price"], 123.45)
        self.assertEqual(result[0]["quantity"], "7")
        self.assertEqual(result[0]["days"], 3)
        self.assertEqual(result[0]["multiplicity"], 2)
        self.assertEqual(result[0]["offer_id"], "offer-1")
        self.assertFalse(result[0]["is_cross"])

    def test_selected_brand_skips_brand_lookup(self):
        provider = TissTmpartsProvider("token", warehouse_mode=1, contract_id="contract-1", outlet_id="outlet-1")
        provider._request = Mock(return_value={})
        provider.get_prices("A100", brand="BOSCH")
        provider._request.assert_called_once_with(
            "POST",
            "v1/product-offers/by-brand-and-product-code",
            payload={
                "products": [{"productCode": "A100", "brandName": "BOSCH"}],
                "contractId": "contract-1",
                "outletId": "outlet-1",
                "priceFrom": None,
                "priceTo": None,
                "deliveryMinDays": None,
                "deliveryMaxDays": None,
                "offersMaxNum": 200,
                "orderByPrice": True,
                "enableAnalog": True,
                "warehouses": None,
                "isInStockInHomeWarehousesOnly": True,
            },
        )

    def test_check_connection_rejects_empty_key(self):
        ok, message = TissTmpartsProvider("").check_connection()
        self.assertFalse(ok)
        self.assertIn("API", message)



class TissBatchOrderTests(unittest.TestCase):
    def _provider(self):
        return TissTmpartsProvider(
            "token",
            contract_id="contract-1",
            outlet_id="outlet-1",
            phone_number="+70000000000",
            timeout=20,
        )

    def _row(self, product_id, source_id="src", quantity=1, price=100):
        return {
            "item": {
                "product_id": product_id,
                "source_id": source_id,
                "source_type": 0,
                "price": price,
            },
            "quantity": quantity,
        }

    def test_all_positions_go_in_one_order(self):
        provider = self._provider()
        provider._request = Mock(return_value={"id": "order-1"})

        results = provider.add_to_basket_batch(
            [self._row("p1"), self._row("p2", quantity=3), self._row("p3")],
            comment="заказ",
        )

        provider._request.assert_called_once()
        payload = provider._request.call_args.kwargs["payload"]
        self.assertEqual([row["productId"] for row in payload["items"]], ["p1", "p2", "p3"])
        self.assertEqual([row["amountInCart"] for row in payload["items"]], [1, 3, 1])
        self.assertEqual(payload["contractId"], "contract-1")
        self.assertTrue(all(result["success"] for result in results))

    def test_order_call_uses_its_own_timeout_and_asks_for_transport_errors(self):
        provider = self._provider()
        provider._request = Mock(return_value={"id": "order-1"})

        provider.add_to_basket_batch([self._row("p1"), self._row("p2")])

        self.assertEqual(provider._request.call_args.kwargs["timeout"], provider.order_timeout)
        self.assertTrue(provider._request.call_args.kwargs["raise_transport"])
        self.assertEqual(provider.order_timeout, 60)

    def test_two_rows_on_one_offer_are_merged(self):
        provider = self._provider()
        provider._request = Mock(return_value={"id": "order-1"})

        results = provider.add_to_basket_batch([self._row("p1", quantity=2), self._row("p1", quantity=4)])

        payload = provider._request.call_args.kwargs["payload"]
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["amountInCart"], 6)
        self.assertTrue(all(result["success"] for result in results))

    def test_position_without_ids_fails_alone(self):
        provider = self._provider()
        provider._request = Mock(return_value={"id": "order-1"})

        results = provider.add_to_basket_batch([self._row("", source_id=""), self._row("p2")])

        self.assertIn("product_id/source_id", results[0]["error"])
        self.assertTrue(results[1]["success"])
        payload = provider._request.call_args.kwargs["payload"]
        self.assertEqual([row["productId"] for row in payload["items"]], ["p2"])

    def test_rejected_order_fails_every_position(self):
        provider = self._provider()
        provider._request = Mock(return_value=None)
        provider.last_message = "TISS: договор не активен"

        results = provider.add_to_basket_batch([self._row("p1"), self._row("p2")])

        self.assertEqual([result["success"] for result in results], [False, False])
        self.assertTrue(all("договор не активен" in result["error"] for result in results))
        self.assertFalse(any(result.get("uncertain") for result in results))

    def test_missing_settings_stop_before_the_call(self):
        provider = self._provider()
        provider.phone_number = ""
        provider._request = Mock()

        results = provider.add_to_basket_batch([self._row("p1"), self._row("p2")])

        provider._request.assert_not_called()
        self.assertTrue(all("телефон" in result["error"] for result in results))

    def test_lost_answer_is_unknown_not_failure(self):
        # Реальный _request обязан превратить обрыв в «исход неизвестен»
        # только для оформления: поиск по таймауту остаётся неудачей.
        provider = self._provider()
        attempts = []

        def _request(method, url, **kwargs):
            attempts.append(kwargs.get("timeout"))
            raise requests.exceptions.ReadTimeout("read timeout")

        provider.session.request = _request

        results = provider.add_to_basket_batch([self._row("p1"), self._row("p2")])
        search = provider._request("GET", "v1/legal-organizations")

        self.assertEqual(attempts, [provider.order_timeout, provider.timeout])
        self.assertTrue(all(result["uncertain"] for result in results))
        self.assertTrue(all("проверьте заказы" in result["error"] for result in results))
        self.assertIsNone(search)

    def test_single_add_goes_through_the_batch_path(self):
        provider = self._provider()
        provider._request = Mock(return_value={"id": "order-1"})

        result = provider.add_to_basket(
            {"product_id": "p1", "source_id": "src", "price": 100}, quantity=2
        )

        self.assertTrue(result["success"])
        payload = provider._request.call_args.kwargs["payload"]
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["amountInCart"], 2)


if __name__ == "__main__":
    unittest.main()
