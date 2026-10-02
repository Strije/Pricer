"""Корзина из обычного поиска -> оформление заказа (как корзина десктопа)."""
import pytest

from test_server import H, app_factory, read_events, register  # noqa: F401


@pytest.fixture(scope="module")
def searched(app_factory):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "cart@example.com", org="Корзина")
    job = client.post("/api/search", headers=H, json={"brand": "ZIC", "article": "162622"}).json()["job_id"]
    result = read_events(client, job)[-1][1]
    return client, job, result, make_client


def test_add_to_cart_from_search(searched):
    client, job, result, _ = searched
    offer = result["offer"]
    cart = client.post("/api/cart", headers=H, json={"job_id": job, "internal_offer_id": offer["internal_offer_id"], "quantity": 2})
    assert cart.status_code == 200, cart.text
    data = cart.json()
    assert data["positions"] == 1 and data["quantity"] == 2
    entry = data["entries"][0]
    assert entry["item"]["provider"] == "Avtoto" and entry["item"]["sale_price"] == 2700
    assert entry["variants"] > 1  # как в десктопе: к позиции сохранены другие варианты той же детали
    assert data["sale_total"] == 5400


def test_change_quantity_and_remove(searched):
    client, job, result, _ = searched
    key = client.get("/api/cart").json()["entries"][0]["key"]
    assert client.put(f"/api/cart/{key}", headers=H, json={"quantity": 3}).json()["quantity"] == 3
    other = result["alternatives"][1]
    client.post("/api/cart", headers=H, json={"job_id": job, "internal_offer_id": other["internal_offer_id"], "quantity": 1})
    assert client.get("/api/cart").json()["positions"] == 2
    assert client.delete(f"/api/cart/{other['internal_offer_id']}", headers=H).json()["positions"] == 1


def test_rejects_unknown_offer_and_foreign_job(searched):
    client, job, _, make_client = searched
    assert client.post("/api/cart", headers=H, json={"job_id": job, "internal_offer_id": "fake", "quantity": 1}).status_code == 404
    stranger = register(make_client(), "cart-stranger@example.com", org="Чужие")
    offer_id = client.get("/api/cart").json()["entries"][0]["key"]
    assert stranger.post("/api/cart", headers=H, json={"job_id": job, "internal_offer_id": offer_id, "quantity": 1}).status_code == 404
    assert stranger.get("/api/cart").json()["positions"] == 0  # у каждого пользователя своя корзина


def test_checkout_creates_order_and_clears_cart(searched):
    client, _, _, _ = searched
    assert client.post("/api/cart/checkout", headers=H, json={"client": "Сидоров", "vin": "ЖЖЖ"}).status_code == 400
    response = client.post("/api/cart/checkout", headers=H, json={"client": "Сидоров", "vin": "WVWZZZ1JZXW000001"})
    assert response.status_code == 200, response.text
    order = response.json()["order"]
    assert order["grouping_mode"] == "manual" and len(order["items"]) == 1
    assert order["items"][0]["quantity"] == 3 and order["client"]["vin"] == "WVWZZZ1JZXW000001"
    assert len(order["groups"]) == 1 and len(order["groups"][0]["offers"]) > 1  # варианты для замены
    assert client.get("/api/cart").json()["positions"] == 0
    assert client.post("/api/cart/checkout", headers=H, json={"client": "Сидоров"}).status_code == 400
