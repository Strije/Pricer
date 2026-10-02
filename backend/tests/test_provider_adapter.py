import pytest
from requests import exceptions as rex

from provider_adapter import OrderDeliveryUnknown, ProviderAdapter, call_order_once


def test_retries_transport_errors_then_succeeds():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise rex.ConnectionError("boom")
        return "ok"

    assert ProviderAdapter(retries=3, backoff=0).call(flaky) == "ok"
    assert len(calls) == 3


def test_does_not_retry_other_errors():
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("x")

    with pytest.raises(ValueError):
        ProviderAdapter(retries=3, backoff=0).call(bad)
    assert len(calls) == 1


def test_order_call_is_never_retried_and_marks_unknown():
    calls = []

    def send():
        calls.append(1)
        raise rex.ReadTimeout("slow")

    with pytest.raises(OrderDeliveryUnknown):
        call_order_once(send)
    assert len(calls) == 1
