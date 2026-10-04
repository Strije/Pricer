import threading
import unittest
from unittest.mock import Mock, patch

import requests

from avtoto import AvtotoProvider
from provider_adapter import OrderDeliveryUnknown


class AvtotoProviderTests(unittest.TestCase):
    def test_get_prices_uses_json_api_and_parses_search_results(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)

        with patch("avtoto.requests.post") as mock_post, patch("avtoto.time.sleep", return_value=None):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.side_effect = [
                {"ProcessSearchId": "abc123"},
                {
                    "Parts": [
                        {
                            "Code": "OC47",
                            "Manuf": "Bosch",
                            "Name": "Filter",
                            "Price": 1250.5,
                            "Storage": "Москва",
                            "Delivery": "2",
                            "MaxCount": 10,
                            "BaseCount": 1,
                            "Availability": 1,
                            "DeliveryPercent": 95,
                            "AvtotoData": {"PartId": 77},
                        }
                    ],
                    "Info": {"SearchStatus": 4, "SearchID": "search-1"},
                },
            ]

            results = provider.get_prices("OC47")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provider"], "Avtoto")
        self.assertEqual(results[0]["article"], "OC47")
        self.assertEqual(results[0]["price"], 1250.5)
        self.assertEqual(results[0]["quantity"], "10")
        self.assertEqual(results[0]["search_id"], "search-1")

    def test_normalize_parts_uses_max_count_as_available_quantity(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)

        results = provider._normalize_parts(
            [
                {
                    "Code": "101171",
                    "Manuf": "Febi",
                    "Name": "Oil",
                    "Price": "2502",
                    "Storage": "Москва",
                    "Delivery": "9",
                    "BaseCount": 1,
                    "MaxCount": "103 шт.",
                    "Availability": 1,
                }
            ],
            "101171",
        )

        self.assertEqual(results[0]["quantity"], "103")
        self.assertEqual(results[0]["max_count"], "103 шт.")

    def test_normalize_parts_uses_part_id_as_supplier_offer_id(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)

        results = provider._normalize_parts(
            [
                {
                    "Code": "101171",
                    "Manuf": "Febi",
                    "Name": "Oil",
                    "Price": "2502",
                    "Storage": "Москва",
                    "Delivery": "9",
                    "BaseCount": 1,
                    "MaxCount": 103,
                    "Availability": 1,
                    "OfferID": "1",
                    "AvtotoData": {"PartId": 77},
                }
            ],
            "101171",
            "search-1",
        )

        self.assertEqual(results[0]["supplier_offer_id"], "77")
        self.assertEqual(results[0]["offer_id"], "1")
        self.assertEqual(results[0]["part_id"], "77")

    def test_search_payload_can_include_brand_and_force_crosses_off(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=True)

        payloads = provider._build_search_payload_variants(
            "MF114",
            brand="MAXI FRESH",
            include_crosses=False,
        )

        self.assertEqual(payloads[0]["search_code"], "MF114")
        self.assertEqual(payloads[0]["search_cross"], "off")
        self.assertEqual(payloads[0]["brand"], "MAXI FRESH")
        self.assertEqual(payloads[1]["SearchCross"], "off")
        self.assertTrue(all(payload.get("brand") == "MAXI FRESH" for payload in payloads))

    def test_normalize_parts_treats_unknown_max_count_as_orderable(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)

        results = provider._normalize_parts(
            [
                {
                    "Code": "40022",
                    "Manuf": "3Ton",
                    "Name": "Cleaner",
                    "Price": "174",
                    "Storage": "Москва",
                    "Delivery": "2",
                    "BaseCount": 1,
                    "MaxCount": "-1",
                    "Availability": 1,
                }
            ],
            "40022",
        )

        self.assertEqual(results[0]["quantity"], ">999")
        self.assertTrue(results[0]["availability_is_lower_bound"])

    def test_get_prices_retries_when_search_is_still_processing(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)

        with patch("avtoto.requests.post") as mock_post, patch("avtoto.time.sleep", return_value=None):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.side_effect = [
                {"ProcessSearchId": "abc123"},
                {"Parts": [], "Info": {"SearchStatus": 2, "Errors": []}},
                {"Parts": [], "Info": {"SearchStatus": 2, "Errors": []}},
                {
                    "Parts": [
                        {
                            "Code": "OC47",
                            "Manuf": "Bosch",
                            "Name": "Filter",
                            "Price": 1250.5,
                            "Storage": "Москва",
                            "Delivery": "2",
                            "BaseCount": 1,
                            "Availability": 1,
                        }
                    ],
                    "Info": {"SearchStatus": 4, "SearchID": "search-2"},
                },
            ]

            results = provider.get_prices("OC47")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["search_id"], "search-2")

    def test_get_prices_reads_nested_process_id(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)

        with patch("avtoto.requests.post") as mock_post, patch("avtoto.time.sleep", return_value=None):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.side_effect = [
                {"result": {"ProcessSearchId": "abc123"}},
                {"result": {"Parts": [{"Code": "OC47", "Price": 100, "BaseCount": 1, "Availability": 1}], "Info": {"SearchStatus": 4, "SearchID": "search-3"}}},
            ]

            results = provider.get_prices("OC47")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["search_id"], "search-3")

    def test_get_prices_retries_with_alternative_payload_when_auth_error(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)

        with patch("avtoto.requests.post") as mock_post, patch("avtoto.time.sleep", return_value=None):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.side_effect = [
                {"result": {"Error": "Неверно указан логин/пароль"}},
                {"ProcessSearchId": "abc123"},
                {"Parts": [{"Code": "OC47", "Price": 100, "BaseCount": 1, "Availability": 1}], "Info": {"SearchStatus": 4, "SearchID": "search-4"}},
            ]

            results = provider.get_prices("OC47")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["search_id"], "search-4")
        self.assertEqual(mock_post.call_count, 3)

    def test_check_connection_rejects_auth_error_payload(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)

        with patch("avtoto.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "result": {"Error": "Неверно указан логин/пароль"}
            }

            ok, message = provider.check_connection()

        self.assertFalse(ok)
        self.assertIn("логин/пароль", message)

    def test_sanitize_payload_redacts_credentials(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        payload = {"user_login": "secret-login", "user_password": "secret-pass", "search_code": "OC90"}

        sanitized = provider._sanitize_payload(payload)

        self.assertEqual(sanitized["user_login"], "***")
        self.assertEqual(sanitized["user_password"], "***")
        self.assertEqual(sanitized["search_code"], "OC90")

    def test_get_brand_candidates_uses_get_brands_by_code(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        provider._call = Mock(return_value={
            "Brands": [
                {"Code": "OC90", "Manuf": "KNECHT", "Name": "Фильтр"},
                {"Code": "OC 90", "Manuf": "MAHLE", "Name": "Фильтр"},
            ]
        })

        result = provider.get_brand_candidates("OC90")

        provider._call.assert_called_once_with("GetBrandsByCode", {
            "user_id": "1",
            "user_login": "user",
            "user_password": "pass",
            "search_code": "OC90",
        })
        self.assertEqual([item["brand"] for item in result], ["KNECHT", "MAHLE"])
        self.assertEqual(result[1]["article"], "OC 90")

    def test_get_brand_candidates_falls_back_after_input_format_error(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        provider._call = Mock(side_effect=[
            {"Info": {"Errors": ["Не верный формат входных данных"]}},
            {"Brands": [{"Manuf": "KNECHT", "Name": "Фильтр"}]},
        ])

        result = provider.get_brand_candidates("OC90")

        self.assertEqual(provider._call.call_args_list[0].args[0], "GetBrandsByCode")
        self.assertEqual(provider._call.call_args_list[0].args[1]["search_code"], "OC90")
        self.assertEqual(provider._call.call_args_list[1].args[0], "GetBrandsByCode")
        self.assertEqual(provider._call.call_args_list[1].args[1]["SearchCode"], "OC90")
        self.assertEqual(result[0]["brand"], "KNECHT")

    def test_brand_resolver_timeout_allows_documented_five_seconds(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False, timeout=10)

        self.assertEqual(provider.brand_resolver_timeout, 5)

    def test_create_order_does_not_ask_availability_after_its_own_reserve(self):
        """Проверка наличия между добавлением и оформлением снята намеренно.

        AddToBasket на своих складах Avtoto ставит резерв на 10 минут, и
        CheckAvailabilityInBasket через секунду отвечал «указанного количества
        нет в наличии» на товар, который сам же и зарезервировал.
        """
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        provider._call = Mock(side_effect=[
            {"DoneInnerId": [{"InnerID": "inner-1", "RemoteID": "remote-1", "Count": 2}]},
            {"DoneInnerId": [{"InnerID": "inner-1", "RemoteID": "remote-1"}]},
        ])

        result = provider.create_order(
            {
                "article": "101171",
                "brand": "Febi",
                "name": "Oil",
                "price": 2502,
                "warehouse": "Москва",
                "days": 9,
                "multiplicity": 1,
                "search_id": "search-1",
                "part_id": "77",
            },
            quantity=2,
        )

        self.assertTrue(result["success"])
        methods = [call.args[0] for call in provider._call.call_args_list]
        self.assertEqual(methods, ["AddToBasket", "AddToOrdersFromBasket"])

    def test_debug_stays_silent_without_context(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        calls = []
        provider.debug_sink = lambda step, payload, context: calls.append(step)

        provider._debug("create_order:start", {"part": {"Code": "101171"}})

        self.assertEqual(calls, [])

    def test_debug_reaches_sink_with_context_and_survives_broken_sink(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        calls = []
        provider.debug_sink = lambda step, payload, context: calls.append((step, payload, context))
        provider.debug_context = {"order_id": "ORD-1", "article": "101171"}

        provider._debug("create_order:start", {"part": {"Code": "101171"}})

        self.assertEqual(len(calls), 1)
        step, payload, context = calls[0]
        self.assertEqual(step, "create_order:start")
        self.assertIn("101171", payload)
        self.assertEqual(context["order_id"], "ORD-1")

        provider.debug_sink = Mock(side_effect=RuntimeError("лог упал"))
        provider._debug("create_order:ordered", {"ok": True})

    def test_timeout_debug_names_the_failed_method(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        calls = []
        provider.debug_sink = lambda step, payload, context: calls.append((step, payload))
        provider.debug_context = {"order_id": "ORD-1"}

        with patch("avtoto.requests.post", side_effect=requests.exceptions.Timeout()):
            result = provider._call("AddToOrdersFromBasket", {"parts": []})

        self.assertIsNone(result)
        timeouts = [payload for step, payload in calls if step == "_call:timeout"]
        self.assertEqual(len(timeouts), 1)
        self.assertIn("AddToOrdersFromBasket", timeouts[0])

    def test_debug_payload_is_trimmed(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        calls = []
        provider.debug_sink = lambda step, payload, context: calls.append(payload)
        provider.debug_context = {"order_id": "ORD-1"}

        provider._debug("_call:response", {"data": "x" * 9000})

        self.assertLess(len(calls[0]), 4200)
        self.assertIn("обрезано", calls[0])

    def test_search_errors_reach_the_sink_without_an_order_context(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        calls = []
        provider.debug_sink = lambda step, payload, context: calls.append((step, context))

        with patch("avtoto.requests.post", side_effect=requests.exceptions.Timeout()):
            provider._call("GetSearchResult", {})

        self.assertEqual([step for step, _ in calls], ["_call:timeout"])
        self.assertEqual(calls[0][1]["scope"], "search")
        self.assertEqual(calls[0][1]["level"], "error")

    def test_ordinary_search_steps_stay_silent_without_context(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        calls = []
        provider.debug_sink = lambda step, payload, context: calls.append(step)

        provider._debug("get_prices:poll_attempt", {"attempt": 1})
        provider._debug("_call:response", {"method": "GetSearchResult"})

        self.assertEqual(calls, [])

    def test_offers_without_part_id_are_reported(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        calls = []
        provider.debug_sink = lambda step, payload, context: calls.append((step, payload))

        provider._normalize_parts(
            [
                {"Code": "OC47", "Manuf": "Bosch", "Price": 100, "MaxCount": 5, "AvtotoData": {"PartId": 7}},
                {"Code": "OC47", "Manuf": "Bosch", "Price": 90, "MaxCount": 40, "Storage": "Ростов"},
            ],
            "OC47",
        )

        reports = [payload for step, payload in calls if step == "normalize:no_part_id"]
        self.assertEqual(len(reports), 1)
        self.assertIn('"count": 1', reports[0])
        self.assertIn('"of_total": 2', reports[0])
        self.assertIn("Ростов", reports[0])

    def test_debug_context_does_not_leak_between_threads(self):
        provider = AvtotoProvider("1", "user", "pass", include_crosses=False)
        seen = {}
        provider.debug_sink = lambda step, payload, context: seen.setdefault(
            threading.current_thread().name, context
        )
        provider.debug_context = {"order_id": "ORD-1"}

        def worker():
            provider._debug("_call:timeout", {"method": "GetSearchResult"})

        thread = threading.Thread(target=worker, name="search")
        thread.start()
        thread.join()
        provider._debug("_call:timeout", {"method": "AddToOrdersFromBasket"})

        self.assertEqual(seen["search"]["scope"], "search")
        self.assertNotIn("order_id", seen["search"])
        self.assertEqual(seen[threading.main_thread().name]["order_id"], "ORD-1")



class AvtotoBatchOrderTests(unittest.TestCase):
    def _provider(self):
        return AvtotoProvider("1", "user", "pass", include_crosses=False)

    def _row(self, article, part_id, quantity=1):
        return {
            "item": {
                "article": article,
                "brand": "Bosch",
                "price": 100,
                "search_id": "s1",
                "part_id": part_id,
                "warehouse": "Москва",
                "multiplicity": 1,
            },
            "quantity": quantity,
        }

    def test_batch_sends_all_parts_in_one_call_and_splits_answers(self):
        provider = self._provider()
        rows = [self._row("OC47", 11), self._row("OC48", 12), self._row("OC49", 13)]
        remote_ids = [
            provider._local_remote_id(row["item"], salt=str(index))
            for index, row in enumerate(rows)
        ]
        calls = []

        def _fake_call(method, params, timeout=None, raise_transport=False):
            calls.append((method, params))
            if method == "AddToBasket":
                return {"DoneInnerId": [
                    {"InnerID": f"inner{index}", "RemoteID": remote_id, "Count": 1}
                    for index, remote_id in enumerate(remote_ids)
                ]}
            return {
                "DoneInnerId": [
                    {"InnerID": "inner0", "RemoteID": remote_ids[0]},
                    {"InnerID": "inner2", "RemoteID": remote_ids[2]},
                ],
                "Errors": ["Указанного количества нет в наличии.<br />Возможно заказать меньшее количество."],
            }

        provider._call = _fake_call
        results = provider.add_to_basket_batch(rows, comment="заказ")

        # Проверки наличия между шагами больше нет: она отвечала на наш же резерв.
        self.assertEqual([method for method, _ in calls], ["AddToBasket", "AddToOrdersFromBasket"])
        self.assertEqual(len(calls[0][1]["parts"]), 3)
        self.assertEqual([result["success"] for result in results], [True, False, True])
        self.assertIn("Указанного количества нет в наличии", results[1]["error"])

    def test_order_step_renumbers_inner_ids_and_we_still_map_by_remote_id(self):
        """Живой случай 15.09.2026: 73 позиции заказаны, все 74 помечены ошибкой.

        В корзине InnerID — номер строки корзины, в ответе на оформление уже
        номер строки заказа. Сопоставление по InnerID давало ноль совпадений.
        """
        provider = self._provider()
        rows = [self._row("OC47", 11), self._row("OC48", 12), self._row("OC49", 13)]
        remote_ids = [
            provider._local_remote_id(row["item"], salt=str(index))
            for index, row in enumerate(rows)
        ]

        def _fake_call(method, params, timeout=None, raise_transport=False):
            if method == "AddToBasket":
                return {
                    "Done": [int(remote_id) for remote_id in remote_ids],
                    "DoneInnerId": [
                        {"InnerID": 144523906 + index, "RemoteID": int(remote_id), "Count": 1}
                        for index, remote_id in enumerate(remote_ids)
                    ],
                }
            return {
                # Поставщик подтвердил две позиции из трёх и выдал СВОИ InnerID.
                "Done": [int(remote_ids[0]), int(remote_ids[2])],
                "DoneInnerId": [
                    {"InnerID": 124668038, "RemoteID": int(remote_ids[0])},
                    {"InnerID": 124668039, "RemoteID": int(remote_ids[2])},
                ],
                "Errors": ["Склад временно недоступен."],
            }

        provider._call = _fake_call
        results = provider.add_to_basket_batch(rows)

        self.assertEqual([result["success"] for result in results], [True, False, True])
        self.assertEqual(results[1]["error"], "Склад временно недоступен.")

    def test_two_rows_on_one_offer_keep_separate_answers(self):
        # RemoteID — наш локальный ключ; без соли дубли строк склеились бы
        # в один и ответ нельзя было бы разложить обратно.
        provider = self._provider()
        rows = [self._row("OC47", 11), self._row("OC47", 11, quantity=2)]
        remote_ids = [
            provider._local_remote_id(row["item"], salt=str(index))
            for index, row in enumerate(rows)
        ]
        self.assertNotEqual(remote_ids[0], remote_ids[1])

        def _fake_call(method, params, timeout=None, raise_transport=False):
            if method == "AddToBasket":
                self.assertEqual(
                    [part["RemoteID"] for part in params["parts"]], remote_ids
                )
                return {"DoneInnerId": [
                    {"InnerID": "inner0", "RemoteID": remote_ids[0], "Count": 1},
                    {"InnerID": "inner1", "RemoteID": remote_ids[1], "Count": 2},
                ]}
            return {"DoneInnerId": [{"InnerID": "inner1", "RemoteID": remote_ids[1]}]}

        provider._call = _fake_call
        results = provider.add_to_basket_batch(rows)

        self.assertEqual([result["success"] for result in results], [False, True])

    def test_order_calls_use_a_longer_timeout_than_search(self):
        provider = self._provider()
        provider.timeout = 20
        provider.order_timeout = max(60, provider.timeout * 3)
        seen = []

        def _fake_call(method, params, timeout=None, raise_transport=False):
            seen.append((method, timeout))
            return None

        provider._call = _fake_call
        provider.add_to_basket_batch([self._row("OC47", 11), self._row("OC48", 12)])

        self.assertEqual(seen, [("AddToBasket", 60)])

    def test_lost_answer_on_order_step_is_unknown_for_the_whole_batch(self):
        provider = self._provider()
        rows = [self._row("OC47", 11), self._row("OC48", 12)]
        remote_ids = [
            provider._local_remote_id(row["item"], salt=str(index))
            for index, row in enumerate(rows)
        ]

        def _fake_call(method, params, timeout=None, raise_transport=False):
            if method == "AddToBasket":
                return {"DoneInnerId": [
                    {"InnerID": f"inner{index}", "RemoteID": remote_id, "Count": 1}
                    for index, remote_id in enumerate(remote_ids)
                ]}
            self.assertTrue(raise_transport, "шаг оформления обязан сообщать об обрыве")
            raise OrderDeliveryUnknown("таймаут")

        provider._call = _fake_call
        results = provider.add_to_basket_batch(rows)

        self.assertTrue(all(result["uncertain"] for result in results))
        self.assertTrue(all("проверьте заказы" in result["error"] for result in results))

    def test_lost_answer_before_the_order_step_is_a_plain_failure(self):
        # На AddToBasket заказ ещё не создан — это обычная ошибка, не «неизвестно».
        provider = self._provider()
        provider.last_message = "Avtoto не ответил: таймаут"
        provider._call = lambda method, params, timeout=None, raise_transport=False: None

        results = provider.add_to_basket_batch([self._row("OC47", 11), self._row("OC48", 12)])

        self.assertTrue(all(result["success"] is False for result in results))
        self.assertFalse(any(result.get("uncertain") for result in results))

    def test_call_raises_only_when_asked_to(self):
        provider = self._provider()
        with patch("avtoto.requests.post", side_effect=requests.exceptions.ReadTimeout("timeout")):
            self.assertIsNone(provider._call("AddToBasket", {}))
            with self.assertRaises(OrderDeliveryUnknown):
                provider._call("AddToOrdersFromBasket", {}, raise_transport=True)

    def test_refusal_reason_from_supplier_reaches_the_position(self):
        # Avtoto пишет причину в Errors с HTML внутри; раньше она попадала
        # в ответ сырым списком и была нечитаемой.
        provider = self._provider()
        rows = [self._row("OC47", 11), self._row("OC48", 12)]
        remote_ids = [
            provider._local_remote_id(row["item"], salt=str(index))
            for index, row in enumerate(rows)
        ]

        def _fake_call(method, params, timeout=None, raise_transport=False):
            if method == "AddToBasket":
                return {"DoneInnerId": [
                    {"InnerID": f"inner{index}", "RemoteID": remote_id, "Count": 1}
                    for index, remote_id in enumerate(remote_ids)
                ]}
            return {
                "DoneInnerId": [],
                "Errors": ["Указанного количества нет в наличии.<br />Возможно заказать меньшее количество."],
            }

        provider._call = _fake_call
        results = provider.add_to_basket_batch(rows)

        self.assertEqual([result["success"] for result in results], [False, False])
        self.assertIn("Указанного количества нет в наличии", results[0]["error"])
        self.assertNotIn("<br", results[0]["error"])

    def test_single_order_reports_the_same_reason(self):
        provider = self._provider()
        item = self._row("OC47", 11)["item"]
        remote_id = provider._local_remote_id(item)

        def _fake_call(method, params, timeout=None, raise_transport=False):
            if method == "AddToBasket":
                return {"DoneInnerId": [{"InnerID": "inner0", "RemoteID": remote_id, "Count": 1}]}
            return {"DoneInnerId": [], "Errors": ["Склад временно недоступен."]}

        provider._call = _fake_call
        result = provider.add_to_basket(item, quantity=3)

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Склад временно недоступен.")

    def test_basket_failure_marks_every_row(self):
        provider = self._provider()
        rows = [self._row("OC47", 11), self._row("OC48", 12)]
        provider._call = lambda method, params, timeout=None, raise_transport=False: None
        provider.last_message = "Avtoto: сервер не ответил"

        results = provider.add_to_basket_batch(rows)

        self.assertEqual([result["success"] for result in results], [False, False])
        self.assertTrue(all("не ответил" in result["error"] for result in results))

    def test_row_without_part_id_fails_without_breaking_the_batch(self):
        provider = self._provider()
        broken = self._row("OC47", 11)
        broken["item"]["part_id"] = ""
        rows = [broken, self._row("OC48", 12)]
        remote_id = provider._local_remote_id(rows[1]["item"], salt="1")

        def _fake_call(method, params, timeout=None, raise_transport=False):
            if method == "AddToBasket":
                self.assertEqual(len(params["parts"]), 1)
                return {"DoneInnerId": [{"InnerID": "inner1", "RemoteID": remote_id, "Count": 1}]}
            return {"DoneInnerId": [{"InnerID": "inner1", "RemoteID": remote_id}]}

        provider._call = _fake_call
        results = provider.add_to_basket_batch(rows)

        self.assertFalse(results[0]["success"])
        self.assertIn("не хватает данных", results[0]["error"])
        self.assertTrue(results[1]["success"])


if __name__ == "__main__":
    unittest.main()
