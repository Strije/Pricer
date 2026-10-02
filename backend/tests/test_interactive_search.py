"""Интерактивный поиск «Проценки» (run_query десктопа): бренд по голосам поставщиков, опрос всех
поставщиков по выбранному бренду, ★ — сколько поставщиков подтвердили номер, признак возврата."""
from engine import ProcurementEngine


def offer(provider, brand, article, price, hours=24, qty=5, **extra):
    return {"provider": provider, "brand": brand, "article": article, "name": f"{brand} {article}", "price": price,
            "delivery_total_hours": hours, "days": hours // 24, "quantity": str(qty), "warehouse": "Склад",
            **extra}


class Fake:
    """Поставщик: бренды по артикулу и предложения по артикулу и бренду."""

    def __init__(self, brands, offers):
        self.brands, self.offers, self.last_message, self.calls = brands, offers, "", []

    def get_brands(self, article):
        return [{"brand": brand, "article": article, "name": "Масло"} for brand in self.brands]

    def get_prices(self, article, brand=None):
        self.calls.append((article, brand))
        return [dict(o) for o in self.offers if not brand or o["brand"].upper() == str(brand).upper()]


def make(*providers):
    classes = [type(f"Fake{i}Provider", (Fake,), {}) for i in range(len(providers))]
    objects = [cls(*args) for cls, args in zip(classes, providers)]
    engine = ProcurementEngine(providers=objects)
    return engine, objects


def test_brand_votes_and_search_by_choice():
    engine, (a, b, c) = make(
        (["ZIC"], [offer("A", "ZIC", "162622", 700), offer("A", "MANN", "W712", 400, is_cross=True)]),
        (["ZIC", "Mobil"], [offer("B", "ZIC", "162622", 650, hours=48)]),
        (["ZIC"], [offer("C", "ZIC", "162622", 720, hours=0, not_returnable=True)]),
    )
    choices, answered = engine.search_brands("162622")
    assert [(ch["brand"].upper(), ch["votes"]) for ch in choices] == [("ZIC", 3), ("MOBIL", 1)]
    assert len(answered) == 3

    events = []
    engine.on_search_event = lambda kind, data: events.append(kind)
    results = engine.search_offers("162622", choices[0]["choice"])
    assert {"results", "provider", "progress"} <= set(events)
    own = [r for r in results if not r.get("is_cross")]
    assert len(own) == 3 and {r["provider_confirm_count"] for r in own} == {3}  # ★3
    returnable = {r["purchase_price"]: r["returnable"] for r in own}
    assert returnable[720] is False and returnable[700] is True  # невозвратное видно, а не скрыто


def test_choose_brand_threshold():
    from app.search import choose_brand

    def c(votes):
        return [{"label": str(i), "votes": v} for i, v in enumerate(votes)]

    answered = list("abcdefghijkl")  # 12 поставщиков ответили
    assert choose_brand(c([6, 2]), answered)["label"] == "0"  # 162622: ZIC у 6 из 12
    assert choose_brand(c([4, 3]), answered[:8]) is None  # нет явного лидера — спросить
    assert choose_brand(c([3, 1]), answered) is None  # меньше половины ответивших
    assert choose_brand(c([3, 1]), answered, share=0.2)["label"] == "0"  # порог — настройка
    assert choose_brand(c([1]), answered)["label"] == "0" and choose_brand([], answered) is None


def test_highlights_and_customer_view():
    from app.search import highlights, offer_view

    class E:
        def apply_markup(self, price):
            return price * 1.3, 30, 0

    rows = [offer_view(i, E()) for i in (
        {"purchase_price": 700, "delivery_hours": 48, "internal_offer_id": "a", "provider": "A"},
        {"purchase_price": 650, "delivery_hours": 96, "internal_offer_id": "b", "provider": "B"},
        {"purchase_price": 400, "delivery_hours": 24, "is_cross": True, "internal_offer_id": "c"},
        {"purchase_price": 300, "delivery_hours": 0, "is_cross": True, "can_order_quantity": False},
    )]
    assert highlights(rows) == {"cheapest": 1, "cheapest_analog": 2, "fastest": 2}
    assert rows[0]["sale_price"] == 910.0
    customer = offer_view({"purchase_price": 700, "provider": "A", "warehouse": "W", "internal_offer_id": "a"}, E(), True)
    assert set(customer) & {"purchase_price", "provider", "warehouse", "internal_offer_id"} == set()


from test_server import H, app_factory, read_events, register  # noqa: E402,F401


def test_find_api(app_factory):  # noqa: F811
    """/api/find: бренд выбран сам (большинство), выдача с ★ и невозвратностью; при спорном бренде —
    вопрос пользователю; «В корзину» берёт предложение из этой выдачи."""
    make_client, _ = app_factory
    client = register(make_client(), "find@example.com", org="Поиск")
    pricer = client.app.state.pricer
    with pricer["Session"]() as session:
        from app import db
        org_id = session.query(db.User).filter_by(email="find@example.com").one().organization_id
    engine = pricer["engine_for"](org_id)
    fakes, _ = make(
        (["ZIC"], [offer("A", "ZIC", "162622", 700), offer("A", "MANN", "W712", 400, is_cross=True)]),
        (["ZIC"], [offer("B", "ZIC", "162622", 650, hours=48)]),
        (["ZIC", "Mobil"], [offer("C", "ZIC", "162622", 720, hours=0, not_returnable=True)]),
    )
    original = engine.providers
    engine.providers = fakes.providers
    try:
        job = client.post("/api/find", headers=H, json={"article": "162622"}).json()["job_id"]
        events = read_events(client, job)
        kinds = [k for k, _ in events]
        brands = next(d for k, d in events if k == "brands")
        assert brands["selected"] == "ZIC" or brands["selected"].startswith("ZIC")
        assert brands["auto"] and brands["choices"][0]["votes"] == 3
        done = next(d for k, d in events if k == "done")
        assert "provider" in kinds and done["total"] == 4
        own = [o for o in done["offers"] if not o.get("is_cross")]
        assert {o["provider_confirm_count"] for o in own} == {3}
        assert any(o["returnable"] is False for o in own) and done["hide_no_return"] in (True, False)
        assert done["offers"][done["highlights"]["cheapest"]]["purchase_price"] == 650
        assert done["offers"][done["highlights"]["cheapest_analog"]]["article"] == "W712"
        pick = done["offers"][done["highlights"]["cheapest"]]
        cart = client.post("/api/cart", headers=H, json={"job_id": job, "internal_offer_id": pick["internal_offer_id"],
                                                        "quantity": 2})
        assert cart.status_code == 200 and cart.json()["quantity"] == 2

        # спорный бренд: 1 голос против 1 — спросить
        engine.providers = make((["ZIC"], []), (["Mobil"], []))[0].providers
        events = read_events(client, client.post("/api/find", headers=H, json={"article": "X1"}).json()["job_id"])
        assert [k for k, _ in events][-1] == "need_brand"
        label = next(d for k, d in events if k == "brands")["choices"][1]["label"]
        events = read_events(client, client.post("/api/find", headers=H, json={"article": "X1", "brand": label}).json()["job_id"])
        assert next(d for k, d in events if k == "brands")["selected"] == label and events[-1][0] == "done"
    finally:
        engine.providers = original


def test_favorites_popular_warranty(app_factory):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "fav@example.com", org="Избранное")
    other = register(make_client(), "fav-other@example.com", org="Другая")
    assert client.get("/api/me/favorites").json() == {"brands": [], "providers": []}
    saved = client.put("/api/me/favorites", headers=H, json={"brands": ["ZIC", " MANN ", "ZIC", ""], "providers": ["Rossko"]}).json()
    assert saved == {"brands": ["MANN", "ZIC"], "providers": ["Rossko"]}
    assert client.get("/api/me/favorites").json() == saved
    assert other.get("/api/me/favorites").json()["brands"] == []  # избранное у каждого своё

    pricer = client.app.state.pricer
    from app import db
    with pricer["Session"]() as session:
        org_id = session.query(db.User).filter_by(email="fav@example.com").one().organization_id
        for i, (brand, provider) in enumerate([("ZIC", "Rossko"), ("ZIC", "Armtek"), ("MANN", "Rossko")]):
            session.add(db.SupplierLine(organization_id=org_id, order_id="ORD-P", item_index=i, provider=provider,
                                        brand=brand, submitted_at=db.utcnow()))
        session.commit()
    popular = client.get("/api/stats/popular").json()
    assert popular["brands"][0] == {"name": "ZIC", "count": 2} and popular["providers"][0]["name"] == "Rossko"
    assert other.get("/api/stats/popular").json() == {"brands": [], "providers": []}

    warranty = client.get("/api/brands/warranty").json()
    assert warranty["page"].startswith("https://") and len(warranty["brands"]) > 100
    some = next(iter(warranty["brands"].values()))
    assert {"name", "warranty", "rating", "conditions"} <= set(some)


def test_static_search_script(app_factory):  # noqa: F811
    make_client, _ = app_factory
    client = make_client()
    page = client.get("/").text
    assert '<script src="/static/search.js"></script>' in page and 'id="article"' in page and 'id="brand"' not in page
    script = client.get("/static/search.js")
    assert script.status_code == 200 and "findArticle" in script.text and "detectKind" in script.text
