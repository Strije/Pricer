# -*- coding: utf-8 -*-
"""Неподтверждённая отправка: ответ на оформление не дошёл, заказ мог уйти."""
import threading
import unittest

import engine as main
from ._compat import SkitchenApp


class _UncertainProvider:
    def __init__(self, results=None):
        self.calls = []
        self._results = results

    def add_to_basket(self, item, quantity=1, comment=""):
        self.calls.append((item, quantity))
        return {
            "success": False,
            "uncertain": True,
            "error": "Avtoto: ответ на оформление не получен",
        }

    def add_to_basket_batch(self, rows, comment=""):
        self.calls.append(list(rows))
        if self._results is not None:
            return self._results
        return [
            {"success": False, "uncertain": True, "error": "ответ на оформление не получен"}
            for _ in rows
        ]


class _FakeStore:
    def __init__(self, order):
        self.order = order

    def read(self, _order_id):
        return self.order

    def update(self, order):
        self.order = order
        return order


class _Signal:
    def __init__(self):
        self.messages = []

    def emit(self, *args, **kwargs):
        self.messages.append(args)


class _FakeHistory:
    def __init__(self):
        self.rows = []
        self._lock = threading.Lock()

    def append(self, item, quantity, response):
        with self._lock:
            self.rows.append((item, quantity, response))


class OrderSubmitUnknownTests(unittest.TestCase):
    def _app(self, provider, count=2, name="Avtoto"):
        items = [
            {
                "provider": name,
                "brand": "MANN",
                "article": f"{name}-{number}",
                "quantity": 1,
                "verification_status": "valid",
                "internal_offer_id": f"{name}{number}",
            }
            for number in range(count)
        ]
        order = {"id": "ORD-1", "items": items, "verification_status": "valid"}

        app = SkitchenApp.__new__(SkitchenApp)
        app.order_history = _FakeHistory()
        app.order_store = _FakeStore(order)
        app.order_action_finished = _Signal()
        app.log_signal = _Signal()
        app._detail_log = lambda *args, **kwargs: None
        app._provider_for_display_name = {name: provider}.get
        app._order_item_for_submit = lambda item: dict(item)
        app._provider_cart_state = lambda display_name: ("order", "")
        app._missing_cart_fields = lambda display_name, item: []
        app._order_submit_preflight = lambda item, send_item, qty: (True, "")
        app._response_log_sample = lambda value: value
        app._offer_log_sample = lambda rows, limit=1: list(rows)[:limit]
        app._is_order_item_local_processed = lambda item: False
        app._is_order_item_manual_excluded = lambda item: False
        app._refresh_order_verification_status = lambda order: None
        return app

    def test_uncertain_answer_marks_item_unknown_not_failed(self):
        app = self._app(_UncertainProvider())

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        items = app.order_store.order["items"]
        self.assertEqual([item["submit_status"] for item in items], ["unknown", "unknown"])
        self.assertIn("не получен", items[0]["submit_unknown_reason"])

    def test_unknown_item_is_not_resent_automatically(self):
        provider = _UncertainProvider()
        app = self._app(provider)
        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")
        calls_after_first = len(provider.calls)

        # Повторная отправка того же заказа: дубль у поставщика недопустим.
        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        self.assertEqual(len(provider.calls), calls_after_first)
        order = app.order_store.order
        self.assertEqual(
            app._submittable_order_items(order),
            [],
            "неподтверждённая позиция не должна попадать в отправку",
        )

    def test_order_status_stays_problematic(self):
        app = self._app(_UncertainProvider())

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        self.assertEqual(app.order_store.order["status"], "failed")

    def test_mixed_batch_splits_submitted_and_unknown(self):
        provider = _UncertainProvider(results=[
            {"success": True, "data": "ok"},
            {"success": False, "uncertain": True, "error": "ответ не получен"},
        ])
        app = self._app(provider)

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        items = app.order_store.order["items"]
        self.assertEqual([item["submit_status"] for item in items], ["submitted", "unknown"])
        self.assertEqual(app.order_store.order["status"], "partial")

    def test_operator_can_return_unknown_item_to_work(self):
        app = self._app(_UncertainProvider())
        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")
        app.add_log = lambda *args, **kwargs: None
        app._refresh_orders_page = lambda *args, **kwargs: None
        app._load_order_details_for_page = lambda *args, **kwargs: None
        app.order_action_finished = _Signal()

        app._restore_order_item("ORD-1", 0)

        item = app.order_store.order["items"][0]
        self.assertEqual(item["submit_status"], "not_submitted")
        self.assertNotIn("submit_unknown_reason", item)
        self.assertEqual(item["verification_status"], "stale")

    def test_processed_statuses_cover_unknown(self):
        self.assertIn("unknown", main.PROCESSED_SUBMIT_STATUSES)


if __name__ == "__main__":
    unittest.main()
