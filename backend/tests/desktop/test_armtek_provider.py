import unittest
from unittest.mock import Mock, patch

import requests

from armtek import ArmtekProvider


class ArmtekProviderTests(unittest.TestCase):
    def test_get_brand_candidates_uses_assortment_search(self):
        provider = ArmtekProvider("user", "pass", "4000", "customer")

        class Response:
            status_code = 200

            def json(self):
                return {
                    "STATUS": 200,
                    "RESP": [
                        {"PIN": "OC90", "BRAND": "KNECHT", "NAME": "Фильтр"},
                        {"PIN": "OC 90", "BRAND": "MAHLE", "NAME": "Фильтр"},
                    ],
                }

        provider.adapter.call = Mock(return_value=Response())
        result = provider.get_brand_candidates("OC90")

        self.assertEqual([item["brand"] for item in result], ["KNECHT", "MAHLE"])
        self.assertEqual(result[1]["article"], "OC 90")



class ArmtekBatchOrderTests(unittest.TestCase):
    def _provider(self):
        return ArmtekProvider("user", "pass", "4000", "customer", timeout=12)

    def _rows(self, count):
        return [
            {"item": {"article": f"OC{index}", "brand": "KNECHT", "keyzak": "MOV1"}, "quantity": index + 1}
            for index in range(count)
        ]

    def test_batch_sends_one_createorder_with_items_table(self):
        provider = self._provider()
        provider.adapter.call = Mock(side_effect=AssertionError("оформление не должно идти через повторы"))
        sent = {}

        class Response:
            status_code = 200
            text = "{}"
            content = b"{}"

            def json(self):
                return {"STATUS": 200, "MESSAGES": [{"TYPE": "S", "TEXT": "Успешно"}]}

        class Session:
            trust_env = True

            def post(self, url, data=None, headers=None, timeout=None, proxies=None):
                sent["url"] = url
                sent["data"] = data
                sent["timeout"] = timeout
                return Response()

        with patch("armtek.requests.Session", return_value=Session()):
            results = provider.add_to_basket_batch(self._rows(3), comment="заказ")

        self.assertEqual(len(results), 3)
        self.assertTrue(all(result["success"] for result in results))
        self.assertEqual(sent["data"]["ITEMS[2][PIN]"], "OC2")
        self.assertEqual(sent["data"]["ITEMS[2][KWMENG]"], "3")
        self.assertEqual(sent["data"]["TEXT_ORD"], "заказ")
        self.assertEqual(sent["timeout"], provider.order_timeout)

    def test_rejected_order_fails_every_position(self):
        provider = self._provider()

        class Response:
            status_code = 200

            def json(self):
                return {"STATUS": 400, "MESSAGES": [{"TYPE": "E", "TEXT": "Нет договора"}]}

        class Session:
            trust_env = True

            def post(self, *args, **kwargs):
                return Response()

        with patch("armtek.requests.Session", return_value=Session()):
            results = provider.add_to_basket_batch(self._rows(2))

        self.assertEqual([result["success"] for result in results], [False, False])
        self.assertTrue(all(result["error"] == "Нет договора" for result in results))
        self.assertFalse(any(result.get("uncertain") for result in results))

    def test_lost_answer_is_reported_as_unknown_without_retry(self):
        provider = self._provider()
        attempts = []

        class Session:
            trust_env = True

            def post(self, *args, **kwargs):
                attempts.append(1)
                raise requests.exceptions.ReadTimeout("read timeout")

        with patch("armtek.requests.Session", return_value=Session()):
            results = provider.add_to_basket_batch(self._rows(2))

        self.assertEqual(len(attempts), 1, "заказ нельзя переспрашивать: будет дубль")
        self.assertTrue(all(result["uncertain"] for result in results))
        self.assertTrue(all("проверьте заказы" in result["error"] for result in results))

    def test_single_add_goes_through_the_same_path(self):
        provider = self._provider()
        provider.add_to_basket_batch = Mock(return_value=[{"success": True, "data": "ok"}])

        result = provider.add_to_basket({"article": "OC90", "brand": "KNECHT"}, quantity=2, comment="c")

        provider.add_to_basket_batch.assert_called_once()
        rows = provider.add_to_basket_batch.call_args.args[0]
        self.assertEqual(rows, [{"item": {"article": "OC90", "brand": "KNECHT"}, "quantity": 2}])
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
