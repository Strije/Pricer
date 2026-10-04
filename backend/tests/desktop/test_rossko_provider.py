# -*- coding: utf-8 -*-
"""Пакетное оформление у Rossko: один GetCheckout на все позиции."""
import unittest
from unittest.mock import Mock

import requests

from rossko import RosskoProvider


class _Service:
    def __init__(self, result=None, error=None):
        self.calls = []
        self._result = result
        self._error = error

    def GetCheckout(self, **params):
        self.calls.append(params)
        if self._error:
            raise self._error
        return self._result


class _Client:
    def __init__(self, service):
        self.service = service


class RosskoBatchOrderTests(unittest.TestCase):
    def _provider(self, service):
        provider = RosskoProvider(
            "key1", "key2", contact_name="Иван", contact_phone="+70000000000", timeout=15
        )
        provider._client = Mock(return_value=_Client(service))
        return provider

    def _rows(self, count=3):
        return [
            {
                "item": {"article": f"MF12{index}", "brand": "MANN", "stock_id": f"HST{index}"},
                "quantity": index + 1,
            }
            for index in range(count)
        ]

    def test_all_positions_go_in_one_checkout(self):
        service = _Service(result={"success": True, "OrderIDS": {"OrderID": ["A-1"]}})
        provider = self._provider(service)

        results = provider.add_to_basket_batch(self._rows(3), comment="заказ")

        self.assertEqual(len(service.calls), 1)
        parts = service.calls[0]["PARTS"]["Part"]
        self.assertEqual([part["partnumber"] for part in parts], ["MF120", "MF121", "MF122"])
        self.assertEqual([part["count"] for part in parts], [1, 2, 3])
        self.assertTrue(all(result["success"] for result in results))
        self.assertIn("A-1", results[0]["data"])

    def test_checkout_uses_the_longer_order_timeout(self):
        service = _Service(result={"success": True})
        provider = self._provider(service)

        provider.add_to_basket_batch(self._rows(2))

        self.assertEqual(provider._client.call_args.kwargs["timeout"], provider.order_timeout)
        self.assertEqual(provider.order_timeout, 60)

    def test_position_without_stock_fails_before_the_call(self):
        service = _Service(result={"success": True})
        provider = self._provider(service)
        rows = self._rows(2)
        rows[0]["item"]["stock_id"] = ""

        results = provider.add_to_basket_batch(rows)

        parts = service.calls[0]["PARTS"]["Part"]
        self.assertEqual(len(parts), 1)
        self.assertFalse(results[0]["success"])
        self.assertIn("склада", results[0]["error"])
        self.assertTrue(results[1]["success"])

    def test_rejected_checkout_fails_every_sent_position(self):
        service = _Service(result={"success": False, "message": "нет реквизитов"})
        provider = self._provider(service)

        results = provider.add_to_basket_batch(self._rows(2))

        self.assertEqual([result["success"] for result in results], [False, False])
        self.assertTrue(all(result["error"] == "нет реквизитов" for result in results))

    def test_item_error_is_matched_to_its_own_position(self):
        service = _Service(result={
            "success": True,
            "OrderIDS": {"OrderID": ["A-1"]},
            "ItemsErrorList": {"ItemError": [
                {"partnumber": "MF121", "brand": "MANN", "message": "нет на складе"},
            ]},
        })
        provider = self._provider(service)

        results = provider.add_to_basket_batch(self._rows(3))

        self.assertEqual([result["success"] for result in results], [True, False, True])
        self.assertIn("нет на складе", results[1]["error"])

    def test_unmatched_item_error_stays_visible_in_the_answer(self):
        service = _Service(result={
            "success": True,
            "OrderIDS": {"OrderID": ["A-1"]},
            "ItemsErrorList": {"ItemError": [{"message": "часть позиций сдвинута по сроку"}]},
        })
        provider = self._provider(service)

        results = provider.add_to_basket_batch(self._rows(2))

        self.assertTrue(all(result["success"] for result in results))
        self.assertIn("сдвинута по сроку", results[0]["data"])

    def test_lost_answer_is_unknown_not_failure(self):
        service = _Service(error=requests.exceptions.ReadTimeout("read timeout"))
        provider = self._provider(service)

        results = provider.add_to_basket_batch(self._rows(2))

        self.assertTrue(all(result["uncertain"] for result in results))
        self.assertTrue(all("проверьте заказы" in result["error"] for result in results))

    def test_other_errors_are_plain_failures(self):
        service = _Service(error=RuntimeError("wsdl сломался"))
        provider = self._provider(service)

        results = provider.add_to_basket_batch(self._rows(2))

        self.assertFalse(any(result.get("uncertain") for result in results))
        self.assertTrue(all("wsdl" in result["error"] for result in results))

    def test_single_add_goes_through_the_batch_path(self):
        service = _Service(result={"success": True, "OrderIDS": {"OrderID": ["A-9"]}})
        provider = self._provider(service)

        result = provider.add_to_basket(
            {"article": "MF122", "brand": "MANN", "stock_id": "HST1"}, quantity=2
        )

        self.assertTrue(result["success"])
        self.assertEqual(len(service.calls[0]["PARTS"]["Part"]), 1)


if __name__ == "__main__":
    unittest.main()
