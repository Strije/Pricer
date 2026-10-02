"""Заказы поставщикам: журнал позиций, статусы, история движений, надёжность поставщиков."""
import io

import pytest

from app.supplier_lines import normalize_status
from test_orders import CSV, job_events
from test_server import H, app_factory, read_events, register  # noqa: F401


def test_normalize_supplier_status_texts():
    assert normalize_status("Отказ поставщика") == "refused"
    assert normalize_status("Нет в наличии") == "refused"
    assert normalize_status("В пути на склад") == "in_transit"
    assert normalize_status("Пришло на склад") == "arrived"
    assert normalize_status("Готов к выдаче") == "arrived"
    assert normalize_status("Выдан клиенту") == "arrived"  # текст поставщика: товар у нас
    assert normalize_status("Заказ подтверждён") == "confirmed"
    assert normalize_status("что-то непонятное") == ""


@pytest.fixture(scope="module")
def submitted(app_factory):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "lines@example.com", org="Журнал")
    rows = client.post("/api/order-file/parse", headers=H,
                       files={"file": ("o.csv", io.BytesIO(CSV.encode()), "text/csv")}).json()["rows"]
    job = client.post("/api/order-file/search", headers=H, json={"rows": rows}).json()["job_id"]
    job_events(client, job)
    order = client.post("/api/orders", headers=H, json={"job_id": job, "rows": [0, 1, 2], "client": "Клиент журнала"}).json()["order"]
    me = client.get("/api/me").json()
    engine = client.app.state.pricer["engine_for"](me["organization_id"])
    from engine import PROVIDER_DISPLAY_NAMES
    outcomes = {
        "Avtoto": {"success": True, "data": "принято"},
        "Forum-Auto": {"success": False, "uncertain": True, "error": "ответ не дошёл"},
        "Автоформула": {"success": True, "data": "отправлено", "order_item_id": "T-777"},
    }
    tradesoft = None
    for provider in engine.providers:
        name = PROVIDER_DISPLAY_NAMES.get(type(provider).__name__)
        if name in outcomes:
            provider.add_to_basket = (lambda result: lambda item, quantity=1, comment="": result)(outcomes[name])
            provider.add_to_basket_batch = None
        if name == "Автоформула":
            tradesoft = provider
    submit = client.post(f"/api/orders/{order['order_id']}/submit", headers=H, json={"confirm": True}).json()["job_id"]
    assert job_events(client, submit)[-1][0] == "done"
    return client, order, tradesoft, make_client


def test_lines_created_with_submit_statuses(submitted):
    client, order, _, _ = submitted
    data = client.get("/api/supplier-lines").json()
    by_provider = {r["provider"]: r for r in data["rows"]}
    assert by_provider["Avtoto"]["status"] == "submitted"
    assert by_provider["Forum-Auto"]["status"] == "unknown"
    assert by_provider["Автоформула"]["supplier_ref"] == "T-777"
    assert by_provider["Avtoto"]["client"] == "Клиент журнала" and by_provider["Avtoto"]["expected_at"]
    assert set(data["facets"]["providers"]) == {"Avtoto", "Forum-Auto", "Автоформула"}
    # повторный запрос не плодит строки
    assert len(client.get("/api/supplier-lines").json()["rows"]) == 3


def test_filters(submitted):
    client, _, _, _ = submitted
    assert [r["provider"] for r in client.get("/api/supplier-lines", params={"provider": "Avtoto"}).json()["rows"]] == ["Avtoto"]
    assert [r["provider"] for r in client.get("/api/supplier-lines", params={"status": "unknown"}).json()["rows"]] == ["Forum-Auto"]
    assert len(client.get("/api/supplier-lines", params={"brand": "ZIC"}).json()["rows"]) == 1
    assert len(client.get("/api/supplier-lines", params={"q": "W712"}).json()["rows"]) == 1
    assert client.get("/api/supplier-lines", params={"date_from": "2099-01-01"}).json()["rows"] == []


def test_manual_status_and_history(submitted):
    client, _, _, _ = submitted
    line = next(r for r in client.get("/api/supplier-lines").json()["rows"] if r["provider"] == "Avtoto")
    client.post(f"/api/supplier-lines/{line['id']}/status", headers=H, json={"status": "in_transit", "text": "трек 123"})
    updated = client.post(f"/api/supplier-lines/{line['id']}/status", headers=H, json={"status": "arrived"}).json()
    assert updated["status"] == "arrived" and updated["arrived_at"]
    history = client.get(f"/api/supplier-lines/{line['id']}").json()["events"]
    assert [e["status"] for e in history] == ["submitted", "in_transit", "arrived"]
    assert line["id"] in [r["id"] for r in client.get("/api/supplier-lines", params={"status": "open"}).json()["rows"]]
    assert history[1]["text"] == "трек 123" and history[1]["source"] == "manual"
    assert client.post(f"/api/supplier-lines/{line['id']}/status", headers=H, json={"status": "bogus"}).status_code == 422


def test_tradesoft_status_refresh(submitted):
    client, _, tradesoft, _ = submitted
    tradesoft.get_items_status = lambda ids: {"T-777": {"providerItemId": "T-777", "stateName": "Отказ поставщика"}}
    job = client.post("/api/supplier-lines/refresh", headers=H).json()["job_id"]
    kind, data = job_events(client, job)[-1]
    assert kind == "done" and {"provider": "Автоформула", "changed": 1} in data["providers"]
    line = next(r for r in client.get("/api/supplier-lines").json()["rows"] if r["provider"] == "Автоформула")
    assert line["status"] == "refused" and line["status_text"] == "Отказ поставщика" and line["final"]


def test_reliability_stats(submitted):
    client, _, _, _ = submitted
    stats = {r["provider"]: r for r in client.get("/api/supplier-stats").json()}
    assert stats["Автоформула"]["refused_pct"] == 100.0
    assert stats["Forum-Auto"]["unknown_pct"] == 100.0
    assert stats["Avtoto"]["arrived"] == 1 and stats["Avtoto"]["avg_days"] is not None
    assert stats["Avtoto"]["enough_data"] is False  # по одной позиции выводы не делаем


def test_lines_isolated(submitted):
    client, _, _, make_client = submitted
    other = register(make_client(), "lines-other@example.com", org="Чужой журнал")
    line_id = client.get("/api/supplier-lines").json()["rows"][0]["id"]
    assert other.get("/api/supplier-lines").json()["rows"] == []
    assert other.get(f"/api/supplier-lines/{line_id}").status_code == 404
    assert other.post(f"/api/supplier-lines/{line_id}/status", headers=H, json={"status": "arrived"}).status_code == 404


def test_replace_variant_in_cart_order(app_factory):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "replace@example.com", org="Замена")
    job = client.post("/api/search", headers=H, json={"brand": "ZIC", "article": "162622"}).json()["job_id"]
    offer = read_events(client, job)[-1][1]["offer"]
    client.post("/api/cart", headers=H, json={"job_id": job, "internal_offer_id": offer["internal_offer_id"], "quantity": 1})
    order = client.post("/api/cart/checkout", headers=H, json={"client": "Замена"}).json()["order"]
    variants = client.get(f"/api/orders/{order['order_id']}/items/0/variants").json()
    assert len(variants) >= 2 and all(v["internal_offer_id"] != offer["internal_offer_id"] for v in variants)
    chosen = variants[1]
    replaced = client.post(f"/api/orders/{order['order_id']}/items/0/replace", headers=H,
                           json={"internal_offer_id": chosen["internal_offer_id"]}).json()["order"]
    assert replaced["items"][0]["internal_offer_id"] == chosen["internal_offer_id"]
    assert client.post(f"/api/orders/{order['order_id']}/items/0/replace", headers=H,
                       json={"internal_offer_id": "fake"}).status_code == 404


def test_refusal_notifications(submitted):
    client, _, tradesoft, make_client = submitted
    tradesoft.get_items_status = lambda ids: {"T-777": {"providerItemId": "T-777", "stateName": "Отказ поставщика"}}
    job_events(client, client.post("/api/supplier-lines/refresh", headers=H).json()["job_id"])
    first = client.get("/api/notifications").json()  # первый вход: отказы за 3 дня
    refusal = next(n for n in first["items"] if n["provider"] == "Автоформула")
    assert refusal["status"] == "refused" and refusal["text"] == "Отказ поставщика" and refusal["order_id"]
    assert client.get("/api/notifications", params={"after": first["last_id"]}).json()["items"] == []
    # ручная отметка оператора — не уведомление
    line = next(r for r in client.get("/api/supplier-lines").json()["rows"] if r["provider"] == "Avtoto")
    client.post(f"/api/supplier-lines/{line['id']}/status", headers=H, json={"status": "refused"})
    assert client.get("/api/notifications", params={"after": first["last_id"]}).json()["items"] == []
    other = register(make_client(), "notify-other@example.com", org="Чужие уведомления")
    assert other.get("/api/notifications").json()["items"] == []


def test_background_refresh_runs(tmp_path, monkeypatch, capsys):
    """Фоновая проверка статусов идёт сама и не падает на организации без поставщиков."""
    import time as time_module

    from app import db as dbm
    from app.server import create_app

    monkeypatch.delenv("PRICER_REPLAY", raising=False)
    monkeypatch.setenv("PRICER_STATUS_REFRESH_MINUTES", "0.001")  # ~0.06 с
    app = create_app(var_dir=str(tmp_path))
    with app.state.pricer["Session"]() as session:
        org = dbm.Organization(name="bg", settings={})
        session.add(org)
        session.flush()
        session.add(dbm.SupplierLine(organization_id=org.id, order_id="ORD-1", item_index=0, provider="X",
                                     status="submitted"))
        session.commit()
    try:
        deadline = time_module.time() + 10
        while app.state.background_rounds < 3 and time_module.time() < deadline:
            time_module.sleep(0.05)
    finally:
        app.state.stop_background.set()
    assert app.state.background_rounds >= 3
    assert "фоновая проверка статусов" not in capsys.readouterr().out  # без ошибок
