import datetime

import pytest
import requests

from onec_odata import (
    OneCConfig, OneCError, OneCODataClient, OneCUnavailable,
    client_order_document, supplier_order_documents,
)
from outbox import MAX_ATTEMPTS, OutboxEvent, deliver, is_due


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = str(self._data)

    def json(self):
        return self._data


class FakeSession:
    """Хранит документы в памяти и повторяет поведение OData: поиск по $filter и создание."""

    def __init__(self, lose_first_post_response=False):
        self.docs = []
        self.auth = None
        self.trust_env = True
        self.calls = []
        self.lose = lose_first_post_response

    def request(self, method, url, json=None, **kwargs):
        self.calls.append(method)
        if method == "POST":
            self.docs.append(dict(json))
            if self.lose:
                self.lose = False
                raise requests.exceptions.ReadTimeout("lost")
            return FakeResponse(201, {"Ref_Key": f"ref-{len(self.docs)}"})
        if "$filter" in url:
            ext = url.split("eq%20'")[1].split("'")[0] if "eq%20'" in url else url.split("eq '")[1].split("'")[0]
            found = [d for d in self.docs if d.get("ВнешнийИдентификатор") == ext]
            return FakeResponse(200, {"value": found})
        return FakeResponse(200, {})


def make_client(session):
    return OneCODataClient(OneCConfig(base_url="https://h/odata/standard.odata", user="u", password="p"), session)


ORDER = {
    "order_id": "ORD-20260101-0001",
    "manager": {"name": "Менеджер"},
    "client": {"name": "Клиент"},
    "comment": "",
    "items": [
        {"provider": "A", "article": "X1", "brand": "B", "name": "n", "quantity": 2,
         "purchase_price": 100.0, "purchase_total": 200.0, "sale_price": 150.0, "sale_total": 300.0,
         "submit_status": "submitted"},
        {"provider": "A", "article": "X2", "brand": "B", "name": "n", "quantity": 1,
         "purchase_price": 50.0, "sale_price": 70.0, "submit_status": "unknown"},
        {"provider": "B", "article": "X3", "brand": "B", "name": "n", "quantity": 1,
         "purchase_price": 10.0, "sale_price": 15.0, "submit_status": "not_submitted"},
    ],
}


def test_client_does_not_use_system_proxy():
    session = FakeSession()
    make_client(session)
    assert session.trust_env is False and session.auth == ("u", "p")


def test_create_is_idempotent():
    session = FakeSession()
    client = make_client(session)
    _, created1 = client.create_document("Document_X", {"a": 1}, "EXT-1")
    _, created2 = client.create_document("Document_X", {"a": 1}, "EXT-1")
    assert created1 is True and created2 is False
    assert len(session.docs) == 1


def test_lost_response_does_not_create_duplicate():
    session = FakeSession(lose_first_post_response=True)
    client = make_client(session)
    doc, created = client.create_document("Document_X", {"a": 1}, "EXT-2")
    assert created is False and len(session.docs) == 1


def test_client_order_uses_sale_prices():
    external_id, doc = client_order_document(ORDER)
    assert external_id == ORDER["order_id"]
    assert doc["Контрагент"] == "Клиент"
    assert doc["Товары"][0]["Цена"] == 150.0 and doc["Товары"][0]["Сумма"] == 300.0


def test_supplier_orders_only_submitted_and_unknown_flagged():
    docs = supplier_order_documents(ORDER)
    assert [d["provider"] for d in docs] == ["A"]  # поставщик B ничего не получил
    assert docs[0]["external_id"] == "ORD-20260101-0001:A"
    assert len(docs[0]["payload"]["Товары"]) == 1  # unknown в документ не попадает
    assert docs[0]["needs_review"] == ["X2"]
    assert docs[0]["payload"]["Товары"][0]["Цена"] == 100.0  # закупочная цена


class DownClient:
    def create_document(self, *a):
        raise OneCUnavailable("down")


class BadClient:
    def create_document(self, *a):
        raise OneCError("HTTP 400")


def test_outbox_retries_with_growing_delay_then_fails():
    event = OutboxEvent("supplier_order", "Document_X", "E", {})
    now = datetime.datetime(2026, 1, 1)
    delays = []
    for _ in range(MAX_ATTEMPTS):
        before = event.next_attempt_at
        deliver(event, DownClient(), now)
        delays.append((event.next_attempt_at - now).total_seconds())
    assert event.status == "failed"
    assert delays[0] == 30 and delays[1] == 60


def test_outbox_data_error_fails_immediately_and_success_marks_sent():
    event = OutboxEvent("client_order", "Document_X", "E", {})
    deliver(event, BadClient())
    assert event.status == "failed" and "400" in event.last_error

    event = OutboxEvent("client_order", "Document_X", "E2", {})
    assert is_due(event)
    deliver(event, make_client(FakeSession()))
    assert event.status == "sent" and event.onec_ref == "ref-1"
