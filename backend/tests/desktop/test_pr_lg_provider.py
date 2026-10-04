import unittest
from unittest.mock import Mock

import requests

from pr_lg import PrLgProvider


class PrLgProviderTests(unittest.TestCase):
    def test_get_brand_candidates_reads_search_items(self):
        provider = PrLgProvider("secret")
        provider._get_json = Mock(return_value=[
            {"brand": "NGK", "article": "2382", "description": "Свеча"},
            {"brand": "DENSO", "article": "2382", "description": "Свеча"},
        ])

        result = provider.get_brand_candidates("2382")

        self.assertEqual([item["brand"] for item in result], ["NGK", "DENSO"])
        provider._get_json.assert_called_once()

    def test_brand_prices_combine_products_and_crosses(self):
        provider = PrLgProvider("secret")
        provider.get_product_prices = Mock(return_value=[
            {"article_id": "1", "warehouse_id": "10", "article": "2382", "brand": "NGK", "price": 100, "warehouse": "A"}
        ])
        provider.get_cross_prices = Mock(return_value=[
            {"article_id": "2", "warehouse_id": "10", "article": "A100", "brand": "DENSO", "price": 90, "warehouse": "B"}
        ])

        result = provider.get_brand_prices("2382", "NGK")

        self.assertEqual(len(result), 2)
        provider.get_product_prices.assert_called_once_with("2382", "NGK")
        provider.get_cross_prices.assert_called_once_with("2382", "NGK")

    def test_delivery_time_is_parsed_as_hours(self):
        provider = PrLgProvider("secret")
        data = [{
            "brand": "MANDO",
            "article": "MOF4459",
            "description": "Фильтр масляный",
            "products": [{
                "price": 230.07,
                "quantity": 5,
                "show_date": "42 ч.",
                "delivery_time": 42,
                "delivery_date": "2026-07-30 09:00:00",
                "custom_warehouse_name": "Ростов 1 (Батайск)",
            }],
        }]

        result = provider._parse_search_response(data, "MOF4459", "MANDO")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["delivery_hours"], 42)
        self.assertEqual(result[0]["delivery_total_hours"], 42)
        self.assertEqual(result[0]["days"], 2)
        self.assertEqual(result[0]["delivery_display"], "42 ч.")
        self.assertEqual(result[0]["delivery_date"], "2026-07-30 09:00:00")

    def test_show_date_days_fallback_is_converted_to_hours(self):
        provider = PrLgProvider("secret")
        data = [{
            "brand": "MANDO",
            "article": "MOF4459",
            "products": [{
                "price": 230.07,
                "quantity": 5,
                "show_date": "3 д.",
                "custom_warehouse_name": "Ростов 2 (хут.Ленина)",
            }],
        }]

        result = provider._parse_search_response(data, "MOF4459", "MANDO")

        self.assertEqual(result[0]["delivery_hours"], 72)
        self.assertEqual(result[0]["days"], 3)

    def test_exact_article_from_crosses_endpoint_is_not_marked_as_cross(self):
        provider = PrLgProvider("secret")
        data = [{
            "brand": "ADDINOL",
            "article": "4014766251022",
            "description": "ADDINOL SUPER LIGHT 5W40 Масло моторное (4L)",
            "products": [{
                "price": 6495.68,
                "quantity": 161,
                "delivery_time": 43,
                "custom_warehouse_name": "Москва 1 (Основной)",
            }],
        }]

        result = provider._parse_search_response(
            data, "4014766251022", "Addinol", crosses=True
        )

        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["is_cross"])
        self.assertEqual(result[0]["source_brand"], "")
        self.assertEqual(result[0]["source_code"], "")

    def test_other_article_from_crosses_endpoint_stays_cross(self):
        provider = PrLgProvider("secret")
        data = [{
            "brand": "ROWE",
            "article": "203671772A",
            "products": [{"price": 1189.42, "quantity": 18, "delivery_time": 43}],
        }]

        result = provider._parse_search_response(
            data, "4014766251022", "Addinol", crosses=True
        )

        self.assertTrue(result[0]["is_cross"])
        self.assertEqual(result[0]["source_code"], "4014766251022")

    def test_same_article_other_brand_from_crosses_stays_cross(self):
        provider = PrLgProvider("secret")
        data = [{
            "brand": "METACO",
            "article": "4014766251022",
            "products": [{"price": 2518.0, "quantity": 8, "delivery_time": 43}],
        }]

        result = provider._parse_search_response(
            data, "4014766251022", "Addinol", crosses=True
        )

        self.assertTrue(result[0]["is_cross"])

    def test_show_date_range_uses_upper_bound_not_joined_digits(self):
        provider = PrLgProvider("secret")
        data = [{
            "brand": "MANDO",
            "article": "MOF4459",
            "products": [{
                "price": 230.07,
                "quantity": 5,
                "show_date": "4-6 д.",
                "custom_warehouse_name": "Ростов 1 (Батайск)",
            }],
        }]

        result = provider._parse_search_response(data, "MOF4459", "MANDO")

        self.assertEqual(result[0]["delivery_hours"], 144)
        self.assertEqual(result[0]["days"], 6)



class PrLgBatchOrderTests(unittest.TestCase):
    def _provider(self, create_order=True):
        provider = PrLgProvider("secret")
        provider.create_order = create_order
        return provider

    def _rows(self, count):
        return [
            {"item": {"article_id": str(index), "warehouse_id": "10"}, "quantity": index + 1}
            for index in range(count)
        ]

    def test_cart_is_cleared_once_and_order_created_once(self):
        provider = self._provider()
        provider.clear_cart = Mock(return_value={"success": True})
        provider._cart_add = Mock(return_value={"success": True, "data": "added"})
        provider.create_cart_order = Mock(return_value={"success": True, "data": "order-1"})

        results = provider.add_to_basket_batch(self._rows(3), comment="заказ")

        provider.clear_cart.assert_called_once()
        provider.create_cart_order.assert_called_once()
        self.assertEqual(provider._cart_add.call_count, 3)
        self.assertEqual([result["success"] for result in results], [True, True, True])
        self.assertEqual(results[0]["data"], "order-1")

    def test_position_that_did_not_reach_the_cart_keeps_its_own_error(self):
        provider = self._provider()
        provider.clear_cart = Mock(return_value={"success": True})
        provider._cart_add = Mock(side_effect=[
            {"success": True, "data": "added"},
            {"success": False, "error": "нет на складе"},
            {"success": True, "data": "added"},
        ])
        provider.create_cart_order = Mock(return_value={"success": True, "data": "order-1"})

        results = provider.add_to_basket_batch(self._rows(3))

        self.assertEqual([result["success"] for result in results], [True, False, True])
        self.assertEqual(results[1]["error"], "нет на складе")
        provider.create_cart_order.assert_called_once()

    def test_order_failure_is_reported_for_every_position_in_the_cart(self):
        provider = self._provider()
        provider.clear_cart = Mock(return_value={"success": True})
        provider._cart_add = Mock(return_value={"success": True, "data": "added"})
        provider.create_cart_order = Mock(return_value={"success": False, "error": "лимит кредита"})

        results = provider.add_to_basket_batch(self._rows(2))

        self.assertEqual([result["success"] for result in results], [False, False])
        self.assertTrue(all(result["error"] == "лимит кредита" for result in results))

    def test_failed_clear_stops_the_batch_before_any_add(self):
        provider = self._provider()
        provider.clear_cart = Mock(return_value={"success": False, "error": "корзина недоступна"})
        provider._cart_add = Mock()
        provider.create_cart_order = Mock()

        results = provider.add_to_basket_batch(self._rows(2))

        provider._cart_add.assert_not_called()
        provider.create_cart_order.assert_not_called()
        self.assertEqual([result["error"] for result in results], ["корзина недоступна"] * 2)

    def test_cart_only_mode_does_not_create_an_order(self):
        provider = self._provider(create_order=False)
        provider.clear_cart = Mock()
        provider._cart_add = Mock(return_value={"success": True, "data": "added"})
        provider.create_cart_order = Mock()

        results = provider.add_to_basket_batch(self._rows(2))

        provider.clear_cart.assert_not_called()
        provider.create_cart_order.assert_not_called()
        self.assertEqual([result["success"] for result in results], [True, True])

    def test_single_add_still_clears_and_orders(self):
        provider = self._provider()
        provider.clear_cart = Mock(return_value={"success": True})
        provider._cart_add = Mock(return_value={"success": True, "data": "added"})
        provider.create_cart_order = Mock(return_value={"success": True, "data": "order-1"})

        result = provider.add_to_basket({"article_id": "1", "warehouse_id": "10"}, quantity=2)

        provider.clear_cart.assert_called_once()
        provider._cart_add.assert_called_once()
        provider.create_cart_order.assert_called_once()
        self.assertEqual(result["data"], "order-1")



class PrLgOrderRetryTests(unittest.TestCase):
    def _provider(self):
        provider = PrLgProvider("secret")
        provider.create_order = True
        provider.order_method = "2"
        provider.order_payment = "2"
        return provider

    def test_order_is_sent_once_and_lost_answer_is_unknown(self):
        provider = self._provider()
        attempts = []

        class Session:
            def post(self, *args, **kwargs):
                attempts.append(kwargs.get("timeout"))
                raise requests.exceptions.ReadTimeout("read timeout")

        provider._session = Session()
        provider.adapter.call = Mock(side_effect=AssertionError("cart/order не должен повторяться"))

        result = provider.create_cart_order()

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0], provider.order_timeout)
        self.assertTrue(result["uncertain"])
        self.assertIn("проверьте заказы", result["error"])

    def test_unknown_order_marks_every_position_of_the_batch(self):
        provider = self._provider()
        provider.clear_cart = Mock(return_value={"success": True})
        provider._cart_add = Mock(return_value={"success": True, "data": "added"})
        provider.create_cart_order = Mock(return_value={
            "success": False,
            "uncertain": True,
            "error": "Profit-League: ответ на оформление не получен",
        })

        results = provider.add_to_basket_batch([
            {"item": {"article_id": "1", "warehouse_id": "10"}, "quantity": 1},
            {"item": {"article_id": "2", "warehouse_id": "10"}, "quantity": 1},
        ])

        self.assertTrue(all(result["uncertain"] for result in results))


if __name__ == "__main__":
    unittest.main()
