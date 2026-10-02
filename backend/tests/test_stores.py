from cart_store import DraftCart
from cross_store import CrossStore
from order_store import OrderStore


def entry(provider="P", article="A1", brand="B", price=100, qty=2, **extra):
    item = {"provider": provider, "article": article, "brand": brand, "price": price,
            "purchase_price": price, "internal_offer_id": f"{provider}-{article}", **extra}
    return {"key": f"{provider}-{article}", "item": item, "qty": qty}


def test_cart_respects_stock_limit():
    cart = DraftCart()
    assert cart.add("k", {"article": "A"}, 5, stock=3) == 3
    assert cart.add("k", {"article": "A"}, 5, stock=3) == 0
    assert cart.count() == 3


def test_cart_remove_matching_by_offer_id():
    cart = DraftCart()
    cart.add("k", {"internal_offer_id": "X", "article": "A"}, 1)
    assert cart.remove_matching({"internal_offer_id": "X"}) == 1
    assert cart.count() == 0


def test_order_ids_increment_and_totals(tmp_path):
    store = OrderStore(str(tmp_path))
    first = store.create_draft("Менеджер", "Клиент", "2026-01-01", [entry(price=100, qty=2, sale_price=150)])
    second = store.create_draft("Менеджер", "Клиент", "2026-01-01", [entry()])
    assert first["order_id"].endswith("-0001") and second["order_id"].endswith("-0002")
    assert first["totals"]["purchase_total"] == 200.0
    assert first["totals"]["sale_total"] == 300.0
    assert first["totals"]["margin"] == 100.0
    assert store.read(first["order_id"])["status"] == "draft"
    assert len(store.list_order_summaries()) == 2


def test_order_groups_collect_requested_position(tmp_path):
    store = OrderStore(str(tmp_path))
    order = store.create_draft("М", "К", "", [entry(), entry(provider="Q")])
    assert len(order["groups"]) == 1
    assert len(order["groups"][0]["offers"]) == 2


def clean(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def test_cross_store_counts_provider_confirmations(tmp_path):
    store = CrossStore(str(tmp_path / "cross.sqlite3"), async_writes=False)
    kw = {"clean_num": clean, "brand_key": clean}
    store.record("MANN", "W 712/95", [{"article": "OP570", "brand": "FILTRON", "provider": "A"}], **kw)
    store.record("MANN", "W 712/95", [{"article": "OP570", "brand": "FILTRON", "provider": "B"}], **kw)
    store.record("MANN", "W 712/95", [{"article": "W71295", "brand": "MANN", "provider": "A"}], **kw)  # точное — не аналог
    rows = store.analogs_for("MANN", "W71295", **kw)
    assert len(rows) == 1
    assert rows[0]["confirmations"] == 2 and rows[0]["seen_count"] == 2
