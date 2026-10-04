# -*- coding: utf-8 -*-
"""Пакетная отправка: поставщик получает все свои позиции одним вызовом."""
import threading
import unittest

from ._compat import SkitchenApp


class _BatchProvider:
    def __init__(self, results=None, raises=None):
        self.batch_calls = []
        self.single_calls = []
        self._results = results
        self._raises = raises

    def add_to_basket(self, item, quantity=1, comment=""):
        self.single_calls.append((item, quantity))
        return {"success": True, "data": "single"}

    def add_to_basket_batch(self, rows, comment=""):
        self.batch_calls.append(list(rows))
        if self._raises:
            raise self._raises
        if self._results is not None:
            return self._results
        return [{"success": True, "data": "batch"} for _ in rows]


class _SingleProvider:
    def __init__(self):
        self.single_calls = []

    def add_to_basket(self, item, quantity=1, comment=""):
        self.single_calls.append((item, quantity))
        return {"success": True}


class _FakeStore:
    def __init__(self, order):
        self.order = order

    def read(self, _order_id):
        return self.order

    def update(self, order):
        self.order = order
        return order


class _Signal:
    def emit(self, *args, **kwargs):
        return None


class _FakeHistory:
    def __init__(self):
        self.rows = []
        self._lock = threading.Lock()

    def append(self, item, quantity, response):
        with self._lock:
            self.rows.append((item, quantity, response))


class OrderSubmitBatchTests(unittest.TestCase):
    def _app(self, providers, item_counts):
        items = []
        for name, count in item_counts.items():
            for number in range(count):
                items.append({
                    "provider": name,
                    "brand": "MANN",
                    "article": f"{name}-{number}",
                    "quantity": 1,
                    "verification_status": "valid",
                    "internal_offer_id": f"{name}{number}",
                })
        order = {"id": "ORD-1", "items": items, "verification_status": "valid"}

        app = SkitchenApp.__new__(SkitchenApp)
        app.order_history = _FakeHistory()
        app.order_store = _FakeStore(order)
        app.order_action_finished = _Signal()
        app.log_signal = _Signal()
        app._detail_log = lambda *args, **kwargs: None
        app._provider_for_display_name = providers.get
        app._order_item_for_submit = lambda item: dict(item)
        app._provider_cart_state = lambda name: ("order", "")
        app._missing_cart_fields = lambda name, item: []
        app._order_submit_preflight = lambda item, send_item, qty: (True, "")
        app._response_log_sample = lambda value: value
        app._offer_log_sample = lambda rows, limit=1: list(rows)[:limit]
        app._refresh_order_submit_status = lambda order, not_ready_count=0: "sent"
        app._refresh_order_verification_status = lambda order: None
        return app

    def test_provider_positions_go_in_one_batch_call(self):
        provider = _BatchProvider()
        app = self._app({"Avtoto": provider}, {"Avtoto": 4})

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        self.assertEqual(len(provider.batch_calls), 1)
        self.assertEqual(len(provider.batch_calls[0]), 4)
        self.assertEqual(provider.single_calls, [])
        items = app.order_store.order["items"]
        self.assertTrue(all(item["submit_status"] == "submitted" for item in items))

    def test_batch_results_are_mapped_to_their_own_items(self):
        provider = _BatchProvider(results=[
            {"success": True, "data": "ok-0"},
            {"success": False, "error": "нет в наличии"},
            {"success": True, "data": "ok-2"},
        ])
        app = self._app({"Avtoto": provider}, {"Avtoto": 3})

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        items = app.order_store.order["items"]
        self.assertEqual([item["submit_status"] for item in items], ["submitted", "failed", "submitted"])
        self.assertEqual(items[1]["supplier_response"]["error"], "нет в наличии")

    def test_short_answer_fails_every_item_instead_of_guessing(self):
        # Ответ, который нельзя разложить по позициям, нельзя считать успехом:
        # иначе часть заказа будет помечена отправленной вслепую.
        provider = _BatchProvider(results=[{"success": True}])
        app = self._app({"Avtoto": provider}, {"Avtoto": 3})

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        items = app.order_store.order["items"]
        self.assertTrue(all(item["submit_status"] == "failed" for item in items))
        self.assertIn("пакетный ответ", items[0]["supplier_response"]["error"])

    def test_batch_exception_fails_every_item(self):
        provider = _BatchProvider(raises=RuntimeError("сеть отвалилась"))
        app = self._app({"Avtoto": provider}, {"Avtoto": 2})

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        items = app.order_store.order["items"]
        self.assertTrue(all(item["submit_status"] == "failed" for item in items))
        self.assertIn("сеть отвалилась", items[0]["supplier_response"]["error"])

    def test_single_position_keeps_the_old_path(self):
        provider = _BatchProvider()
        app = self._app({"Avtoto": provider}, {"Avtoto": 1})

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        self.assertEqual(provider.batch_calls, [])
        self.assertEqual(len(provider.single_calls), 1)

    def test_provider_without_batch_is_sent_one_by_one(self):
        batch_provider = _BatchProvider()
        single_provider = _SingleProvider()
        app = self._app(
            {"Avtoto": batch_provider, "Mikado": single_provider},
            {"Avtoto": 2, "Mikado": 2},
        )

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        self.assertEqual(len(batch_provider.batch_calls), 1)
        self.assertEqual(len(single_provider.single_calls), 2)


if __name__ == "__main__":
    unittest.main()
