"""Подбор для клиента: менеджер добавляет варианты из выдачи, клиент выбирает по ссылке без входа
(видит только продажную цену, срок, бренд — без закупки и поставщиков), выбор становится заказом."""
from test_interactive_search import make, offer
from test_server import H, app_factory, read_events, register  # noqa: F401


def setup(app_factory, email):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), email, org="Подбор " + email)
    pricer = client.app.state.pricer
    from app import db
    with pricer["Session"]() as session:
        org_id = session.query(db.User).filter_by(email=email).one().organization_id
    engine = pricer["engine_for"](org_id)
    fakes, _ = make(
        (["ZIC"], [offer("A", "ZIC", "162622", 700), offer("A", "MANN", "W712", 400, is_cross=True)]),
        (["ZIC"], [offer("B", "ZIC", "162622", 650, hours=48)]),
    )
    engine.providers = fakes.providers
    job = client.post("/api/find", headers=H, json={"article": "162622"}).json()["job_id"]
    read_events(client, job)
    results = client.get(f"/api/find/{job}/results").json()["offers"]
    return make_client, client, job, results


def test_quote_flow(app_factory):  # noqa: F811
    make_client, client, job, offers = setup(app_factory, "quote@example.com")
    quote = client.post("/api/quotes", headers=H, json={"title": "Иванов, ТО", "client": "Иванов", "phone": "89780000000"}).json()
    assert quote["status"] == "draft" and quote["client"] == "Иванов"
    own = next(o for o in offers if not o.get("is_cross"))
    analog = next(o for o in offers if o.get("is_cross"))
    for o in (own, analog, own):  # повторное добавление ничего не меняет
        quote = client.post(f"/api/quotes/{quote['id']}/variants", headers=H,
                            json={"job_id": job, "internal_offer_id": o["internal_offer_id"]}).json()
    assert quote["positions"] == 1 and quote["variants"] == 2  # варианты одного поиска — одна позиция
    line = quote["lines"][0]
    quote = client.put(f"/api/quotes/{quote['id']}/lines/{line['id']}", headers=H, json={"request": "Масло моторное", "qty": 2}).json()
    assert quote["lines"][0]["request"] == "Масло моторное"

    public = make_client()  # клиент без входа
    assert public.get(f"/api/public/quote/{quote['token']}").status_code == 404  # черновик не виден
    quote = client.post(f"/api/quotes/{quote['id']}/send", headers=H, json={"hours": 24}).json()
    assert quote["status"] == "sent" and quote["expires_at"]
    assert public.get(f"/q/{quote['token']}").status_code == 200
    view = public.get(f"/api/public/quote/{quote['token']}").json()
    text = str(view)
    assert view["lines"][0]["request"] == "Масло моторное" and len(view["lines"][0]["variants"]) == 2
    assert "purchase_price" not in text and "'provider'" not in text and "warehouse" not in text
    assert client.get(f"/api/quotes/{quote['id']}").json()["status"] == "viewed"

    before = client.get("/api/notifications/other").json()["last_id"]
    pick = view["lines"][0]["variants"][1]["key"]
    chosen = public.post(f"/api/public/quote/{quote['token']}/choose", headers=H,
                         json={"choices": {line["id"]: pick}, "comment": "Возьму аналог"}).json()
    assert chosen["status"] == "chosen" and chosen["total"] == round(view["lines"][0]["variants"][1]["sale_price"] * 2, 2)
    note = client.get("/api/notifications/other", params={"after": before}).json()["items"]
    assert note and note[0]["kind"] == "quote_chosen" and note[0]["chosen"] == 1

    order = client.post(f"/api/quotes/{quote['id']}/order", headers=H).json()["order"]
    assert order["items"][0]["quantity"] == 2 and order["items"][0]["article"] == "W712"
    assert "Возьму аналог" in order.get("comment", "")
    assert client.get(f"/api/quotes/{quote['id']}").json()["status"] == "ordered"
    assert client.post(f"/api/quotes/{quote['id']}/order", headers=H).status_code == 400  # второй раз — нет
    assert public.post(f"/api/public/quote/{quote['token']}/choose", headers=H,
                       json={"choices": {line["id"]: "skip"}}).status_code == 400  # заказ оформлен — подбор закрыт


def test_quote_validation_and_isolation(app_factory):  # noqa: F811
    make_client, client, job, offers = setup(app_factory, "quote2@example.com")
    quote = client.post("/api/quotes", headers=H, json={}).json()
    assert quote["title"].startswith("Подбор от")
    assert client.post(f"/api/quotes/{quote['id']}/send", headers=H, json={}).status_code == 400  # пустой
    client.post(f"/api/quotes/{quote['id']}/variants", headers=H, json={"job_id": job, "internal_offer_id": offers[0]["internal_offer_id"]})
    client.post(f"/api/quotes/{quote['id']}/send", headers=H, json={})
    public = make_client()
    bad = public.post(f"/api/public/quote/{quote['token']}/choose", headers=H, json={"choices": {"nope": "x"}})
    assert bad.status_code == 400  # ничего не выбрано
    assert public.get("/api/public/quote/неттакого").status_code == 404
    other = register(make_client(), "quote-other@example.com", org="Чужой подбор")
    assert other.get(f"/api/quotes/{quote['id']}").status_code == 404
    assert other.get("/api/quotes").json() == []
