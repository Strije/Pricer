"""Справочник клиентов и машин: запоминается при оформлении заказа, ищется по имени, телефону и VIN."""
import pytest

from test_server import H, app_factory, read_events, register  # noqa: F401


@pytest.fixture(scope="module")
def shop(app_factory):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "clients@example.com", org="Клиенты")

    def order_via_cart(**fields):
        job = client.post("/api/search", headers=H, json={"brand": "ZIC", "article": "162622"}).json()["job_id"]
        offer = read_events(client, job)[-1][1]["offer"]
        client.post("/api/cart", headers=H, json={"job_id": job, "internal_offer_id": offer["internal_offer_id"], "quantity": 1})
        return client.post("/api/cart/checkout", headers=H, json=fields)

    return client, order_via_cart, make_client


def test_client_and_car_are_remembered_on_checkout(shop):
    client, order_via_cart, _ = shop
    r = order_via_cart(client="Иванов Иван", phone="8 (900) 123-45-67", vin="wvwzzz1jzxw000001")
    assert r.status_code == 200, r.text
    order_client = r.json()["order"]["client"]
    assert order_client["name"] == "Иванов Иван" and order_client["phone"] == "79001234567"
    assert order_client["vin"] == "WVWZZZ1JZXW000001" and order_client["id"] and order_client["vehicle_id"]
    clients = client.get("/api/clients").json()
    assert len(clients) == 1 and clients[0]["vehicles"][0]["vin"] == "WVWZZZ1JZXW000001"


def test_same_client_found_by_name_and_phone_second_car_added(shop):
    client, order_via_cart, _ = shop
    by_name = order_via_cart(client="иванов иван", vin="XTA21099012345678").json()["order"]["client"]
    by_phone = order_via_cart(client="Ваня", phone="+7 900 123 45 67").json()["order"]["client"]
    clients = client.get("/api/clients").json()
    assert len(clients) == 1  # тот же клиент, дубль не создан
    assert by_name["id"] == by_phone["id"] == clients[0]["id"]
    assert {v["vin"] for v in clients[0]["vehicles"]} == {"WVWZZZ1JZXW000001", "XTA21099012345678"}


def test_choose_client_and_vehicle_by_id(shop):
    client, order_via_cart, _ = shop
    existing = client.get("/api/clients").json()[0]
    vehicle = existing["vehicles"][1]
    order = order_via_cart(client="", client_id=existing["id"], vehicle_id=vehicle["id"]).json()["order"]
    assert order["client"]["name"] == existing["name"]
    assert order["client"]["vehicle_id"] == vehicle["id"] and order["client"]["vin"] == vehicle["vin"]
    assert order_via_cart(client="").status_code == 400  # без клиента заказ не оформить


def test_search_by_vin_phone_and_history(shop):
    client, _, _ = shop
    assert [c["name"] for c in client.get("/api/clients", params={"q": "XTA2109"}).json()] == ["Иванов Иван"]
    assert len(client.get("/api/clients", params={"q": "9001234"}).json()) == 1
    assert client.get("/api/clients", params={"q": "Петров"}).json() == []
    card = client.get(f"/api/clients/{client.get('/api/clients').json()[0]['id']}").json()
    assert len(card["orders"]) >= 3 and card["orders"][0]["items"][0]["article"] == "162622"


def test_manual_client_and_vehicle_editing(shop):
    client, _, _ = shop
    created = client.post("/api/clients", headers=H, json={"name": "Петров", "phone": "+79990000000"}).json()
    with_car = client.post(f"/api/clients/{created['id']}/vehicles", headers=H,
                           json={"vin": "JTDBR32E720123456", "make": "Toyota", "model": "Corolla", "year": 2002, "plate": "а123вс92"}).json()
    car = with_car["vehicles"][0]
    assert car["make"] == "Toyota" and car["plate"] == "А123ВС92"
    assert client.post(f"/api/clients/{created['id']}/vehicles", headers=H, json={"vin": "JTDBR32E720123456"}).status_code == 409
    assert client.post(f"/api/clients/{created['id']}/vehicles", headers=H, json={"vin": "JTDBR32E72O123456"}).status_code == 400  # буква O
    assert client.put(f"/api/vehicles/{car['id']}", headers=H, json={"vin": car["vin"], "year": 2003}).json()["vehicles"][0]["year"] == 2003
    assert client.delete(f"/api/vehicles/{car['id']}", headers=H).json()["vehicles"] == []


def test_clients_are_isolated(shop):
    client, _, make_client = shop
    stranger = register(make_client(), "clients-stranger@example.com", org="Другая")
    some_id = client.get("/api/clients").json()[0]["id"]
    assert stranger.get("/api/clients").json() == []
    assert stranger.get(f"/api/clients/{some_id}").status_code == 404
