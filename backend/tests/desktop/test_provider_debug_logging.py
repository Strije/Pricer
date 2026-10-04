import threading
import unittest

import pytest

from ._compat import SkitchenApp

# Пробел переноса: в движок не попал _attach_provider_debug (приёмник пошаговой диагностики
# поставщика), а веб-сервер не передаёт движку detailed_logger. Поэтому событий provider_debug
# при отправке заказа в вебе нет. Когда перенесём — xfail снять (strict: тест сам сообщит).
pytestmark = pytest.mark.xfail(
    strict=True, raises=AttributeError,
    reason="диагностика поставщиков при отправке заказа ещё не перенесена в веб",
)


class _DebuggingProvider:
    """Поставщик с диагностикой: пишет шаг только когда включён контекст."""

    def __init__(self, fail=False):
        self.debug_sink = None
        self.debug_context = None
        self.context_during_call = None
        self._fail = fail

    def _debug(self, step, payload):
        if not callable(self.debug_sink) or not self.debug_context:
            return
        self.debug_sink(step, payload, dict(self.debug_context))

    def add_to_basket(self, item, quantity=1, comment=""):
        self.context_during_call = dict(self.debug_context or {})
        self._debug("create_order:start", '{"Code": "OC47"}')
        if self._fail:
            raise RuntimeError("поставщик упал")
        self._debug("_call:timeout", '{"method": "AddToOrdersFromBasket"}')
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


class ProviderDebugLoggingTest(unittest.TestCase):
    def _app(self, provider):
        records = []
        order = {
            "id": "ORD-1",
            "verification_status": "valid",
            "items": [{
                "provider": "Avtoto",
                "brand": "BOSCH",
                "article": "1987301001",
                "quantity": 1,
                "verification_status": "valid",
                "internal_offer_id": "offer-1",
            }],
        }
        app = SkitchenApp.__new__(SkitchenApp)
        app.order_history = _FakeHistory()
        app.order_store = _FakeStore(order)
        app.order_action_finished = _Signal()
        app.log_signal = _Signal()
        app._detail_log = lambda event, **fields: records.append((event, fields))
        app._provider_for_display_name = lambda name: provider
        app._order_item_for_submit = lambda item: dict(item)
        app._provider_cart_state = lambda name: ("order", "")
        app._missing_cart_fields = lambda name, item: []
        app._order_submit_preflight = lambda item, send_item, qty: (True, "")
        app._response_log_sample = lambda value: value
        app._offer_log_sample = lambda rows, limit=1: list(rows)[:limit]
        app._refresh_order_submit_status = lambda order, not_ready_count=0: "sent"
        app._refresh_order_verification_status = lambda order: None
        app._attach_provider_debug(provider, "Avtoto")
        return app, records

    def test_submit_writes_provider_debug_with_order_context(self):
        provider = _DebuggingProvider()
        app, records = self._app(provider)

        app._submit_order_thread("ORD-1", submit_key="ORD-1:0")

        debug = [fields for event, fields in records if event == "provider_debug"]
        self.assertEqual([row["step"] for row in debug], ["create_order:start", "_call:timeout"])
        for row in debug:
            self.assertEqual(row["provider"], "Avtoto")
            self.assertEqual(row["order_id"], "ORD-1")
            self.assertEqual(row["article"], "1987301001")
            self.assertEqual(row["idempotency_key"], "ORD-1:0")
        # По этой строке видно, что оборвалось именно оформление, а не добавление
        # в корзину: товар остался висеть у поставщика.
        self.assertIn("AddToOrdersFromBasket", debug[1]["payload"])

    def test_context_is_open_during_the_call_and_closed_after(self):
        provider = _DebuggingProvider()
        app, _records = self._app(provider)

        app._submit_order_thread("ORD-1", submit_key="ORD-1:0")

        self.assertEqual(provider.context_during_call.get("order_id"), "ORD-1")
        self.assertIsNone(provider.debug_context)

    def test_context_is_closed_even_when_provider_raises(self):
        provider = _DebuggingProvider(fail=True)
        app, records = self._app(provider)

        app._submit_order_thread("ORD-1", submit_key="ORD-1:0")

        self.assertIsNone(provider.debug_context)
        self.assertTrue(any(event == "provider_debug" for event, _ in records))
        item = app.order_store.order["items"][0]
        self.assertEqual(item.get("submit_status"), "failed")

    def test_debug_is_silent_outside_the_order_scope(self):
        provider = _DebuggingProvider()
        _app, records = self._app(provider)

        provider._debug("get_prices:start", '{"article": "OC47"}')

        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
