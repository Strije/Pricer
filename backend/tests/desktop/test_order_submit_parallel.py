import threading
import time
import unittest

from ._compat import SkitchenApp


class _RecordingProvider:
    """Поставщик, запоминающий, когда началась и закончилась каждая отправка."""

    def __init__(self, name, calls, lock, delay=0.05):
        self.name = name
        self._calls = calls
        self._lock = lock
        self._delay = delay

    def add_to_basket(self, item, quantity=1, comment=""):
        started = time.monotonic()
        time.sleep(self._delay)
        finished = time.monotonic()
        with self._lock:
            self._calls.append((self.name, started, finished))
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


def _overlap(first, second):
    return first[1] < second[2] and second[1] < first[2]


class OrderSubmitParallelTest(unittest.TestCase):
    def _app(self, item_counts):
        calls = []
        lock = threading.Lock()
        providers = {
            name: _RecordingProvider(name, calls, lock)
            for name in item_counts
        }

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
        return app, calls

    def test_items_of_one_provider_never_overlap(self):
        """У Profit-League каждая позиция очищает корзину и оформляет заказ.

        Параллельные вызовы одного поставщика затёрли бы друг друга.
        """
        app, calls = self._app({"Profit-League": 4})

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        self.assertEqual(len(calls), 4)
        ordered = sorted(calls, key=lambda row: row[1])
        for earlier, later in zip(ordered, ordered[1:]):
            self.assertFalse(_overlap(earlier, later), "позиции одного поставщика ушли параллельно")

    def test_different_providers_are_sent_in_parallel(self):
        app, calls = self._app({"Profit-League": 3, "Avtoto": 3})

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        self.assertEqual(len(calls), 6)
        cross = [
            (first, second)
            for first in calls
            for second in calls
            if first[0] != second[0] and _overlap(first, second)
        ]
        # при последовательной отправке пересечений не было бы ни одного
        self.assertTrue(cross, "разные поставщики отправлялись по очереди")

    def test_every_item_gets_a_supplier_response(self):
        app, _calls = self._app({"Profit-League": 2, "Avtoto": 2})

        app._submit_order_thread("ORD-1", submit_key="ORD-1:all")

        items = app.order_store.order["items"]
        self.assertEqual(len(items), 4)
        for item in items:
            self.assertEqual(item.get("submit_status"), "submitted")
            self.assertTrue(item.get("supplier_response", {}).get("success"))


if __name__ == "__main__":
    unittest.main()
