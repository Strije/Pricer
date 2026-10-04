# -*- coding: utf-8 -*-
"""Пакетное создание заказа у ABSTD: один api-create_order на все позиции."""
import unittest
from unittest.mock import Mock, patch

from requests import exceptions as request_exceptions

from abstd import AbstdProvider


class AbstdBatchOrderTests(unittest.TestCase):
    def _provider(self, response=None, error=None):
        provider = AbstdProvider("login", "password", "agreement-1", timeout=10)
        provider.delivery_address_id = "address-1"
        if error is not None:
            provider._get = Mock(side_effect=error)
        else:
            provider._get = Mock(return_value=response)
        return provider

    def _rows(self, count=3, quantity=1):
        return [
            {"item": {"product_id": f"p{index}", "price": 100 + index}, "quantity": quantity}
            for index in range(count)
        ]

    def _ok(self, product_ids):
        return {
            "status": "OK",
            "data": {pid: {"status": {"code": "0", "description": ""}} for pid in product_ids},
        }

    def test_all_positions_go_in_one_request(self):
        provider = self._provider(self._ok(["p0", "p1", "p2"]))

        results = provider.add_to_basket_batch(self._rows(3), comment="заказ")

        provider._get.assert_called_once()
        params = provider._get.call_args.args[1]
        self.assertEqual(params["prods[p0]"], "1")
        self.assertEqual(params["prods[p2]"], "1")
        self.assertEqual(params["initial_price[p1]"], "101.0")
        self.assertEqual(params["p_desc[p0]"], "заказ")
        self.assertTrue(all(result["success"] for result in results))

    def test_order_call_uses_the_longer_timeout_and_reports_transport_loss(self):
        provider = self._provider(self._ok(["p0"]))
        provider.add_to_basket_batch(self._rows(1))

        self.assertEqual(provider._get.call_args.kwargs["timeout"], provider.order_timeout)
        self.assertTrue(provider._get.call_args.kwargs["raise_transport"])
        self.assertEqual(provider.order_timeout, 60)

    def test_refused_product_fails_alone(self):
        provider = self._provider({
            "status": "OK",
            "data": {
                "p0": {"status": {"code": "0", "description": ""}},
                "p1": {"status": {"code": "12", "description": "нет на складе"}},
                "p2": {"status": {"code": "0", "description": ""}},
            },
        })

        results = provider.add_to_basket_batch(self._rows(3))

        self.assertEqual([result["success"] for result in results], [True, False, True])
        self.assertIn("нет на складе", results[1]["error"])

    def test_rejected_order_fails_every_position(self):
        provider = self._provider({"status": "ERROR", "data": {}})

        results = provider.add_to_basket_batch(self._rows(2))

        self.assertEqual([result["success"] for result in results], [False, False])
        self.assertTrue(all(result["error"] for result in results))
        self.assertFalse(any(result.get("uncertain") for result in results))

    def test_two_rows_on_one_product_are_merged(self):
        provider = self._provider(self._ok(["p0"]))
        rows = [
            {"item": {"product_id": "p0", "price": 100}, "quantity": 2},
            {"item": {"product_id": "p0", "price": 100}, "quantity": 3},
        ]

        results = provider.add_to_basket_batch(rows)

        params = provider._get.call_args.args[1]
        self.assertEqual(params["prods[p0]"], "5")
        self.assertTrue(all(result["success"] for result in results))

    def test_position_without_product_id_fails_alone(self):
        provider = self._provider(self._ok(["p1"]))
        rows = [
            {"item": {"product_id": ""}, "quantity": 1},
            {"item": {"product_id": "p1", "price": 50}, "quantity": 1},
        ]

        results = provider.add_to_basket_batch(rows)

        self.assertEqual(results[0]["error"], "Нет product_id")
        self.assertTrue(results[1]["success"])
        params = provider._get.call_args.args[1]
        self.assertNotIn("prods[]", params)

    def test_missing_delivery_address_stops_before_the_call(self):
        provider = self._provider(self._ok(["p0"]))
        provider.delivery_address_id = ""

        results = provider.add_to_basket_batch(self._rows(2))

        provider._get.assert_not_called()
        self.assertTrue(all("адрес доставки" in result["error"] for result in results))

    def test_lost_answer_is_unknown_not_failure(self):
        # Проверяем весь путь, а не заглушку: реальный _get обязан превратить
        # обрыв чтения в «исход неизвестен» только для заказа.
        provider = AbstdProvider("login", "password", "agreement-1", timeout=10)
        provider.delivery_address_id = "address-1"
        attempts = []

        class Session:
            trust_env = True

            def get(self, url, params=None, timeout=None, proxies=None):
                attempts.append(timeout)
                raise request_exceptions.ReadTimeout("read timeout")

        with patch("abstd.requests.Session", return_value=Session()):
            results = provider.add_to_basket_batch(self._rows(2))
            search = provider._get("api-search", {})

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], provider.order_timeout)
        self.assertTrue(all(result["uncertain"] for result in results))
        self.assertTrue(all("проверьте заказы" in result["error"] for result in results))
        self.assertIsNone(search, "поиск по таймауту остаётся обычной неудачей")

    def test_connect_timeout_is_a_plain_failure(self):
        # Соединение не установилось — запрос до ABSTD не дошёл, заказа нет.
        provider = AbstdProvider("login", "password", "agreement-1", timeout=10)
        provider.delivery_address_id = "address-1"

        class Session:
            trust_env = True

            def get(self, *args, **kwargs):
                raise request_exceptions.ConnectTimeout("connect timeout")

        with patch("abstd.requests.Session", return_value=Session()):
            results = provider.add_to_basket_batch(self._rows(2))

        self.assertFalse(any(result.get("uncertain") for result in results))
        self.assertTrue(all(result["success"] is False for result in results))

    def test_single_order_keeps_its_external_id_format(self):
        provider = self._provider(self._ok(["p0"]))

        provider.add_to_basket({"product_id": "p0", "price": 100}, quantity=1)

        params = provider._get.call_args.args[1]
        self.assertTrue(params["external_id"].startswith("PP"))
        self.assertTrue(params["external_id"].endswith("p0"))

    def test_batch_external_id_marks_the_batch(self):
        provider = self._provider(self._ok(["p0", "p1"]))

        provider.add_to_basket_batch(self._rows(2))

        params = provider._get.call_args.args[1]
        self.assertTrue(params["external_id"].endswith("B2"))


if __name__ == "__main__":
    unittest.main()
