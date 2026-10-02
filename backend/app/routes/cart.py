"""Корзина и оформление заказа из неё."""

from fastapi import Depends, HTTPException

from app import db
from app.routes.common import _offer, _order_view
from app.schemas import CartAddRequest, CartQuantityRequest, CheckoutRequest


def setup(app, ctx):
    Session, current_user, engine_for = ctx.Session, ctx.current_user, ctx.engine_for
    state = ctx.state
    link_client, resolve_client = ctx.link_client, ctx.resolve_client

    # ----- корзина -----

    from cart_store import DraftCart

    def load_cart(session, user_id):
        row = session.get(db.Cart, user_id)
        cart = DraftCart()
        cart._entries = [dict(e) for e in (row.entries if row else [])]
        return cart

    def save_cart(session, user_id, cart):
        row = session.get(db.Cart, user_id)
        if row is None:
            row = db.Cart(user_id=user_id, entries=[])
            session.add(row)
        from order_store import _json_safe
        row.entries = _json_safe(cart.rows())
        session.commit()

    def cart_view(engine, cart):
        rows, purchase, sale, quantity = [], 0.0, 0.0, 0
        for entry in cart.rows():
            item = entry.get("item") or {}
            qty = int(entry.get("qty") or 0)
            price = float(item.get("purchase_price", item.get("price")) or 0)
            sale_price = float(engine.apply_markup(price)[0])
            purchase += price * qty
            sale += sale_price * qty
            quantity += qty
            rows.append({"key": entry.get("key"), "qty": qty, "status": engine._cart_status_text(item),
                         "variants": int(item.get("selection_group_offer_count") or 1),
                         "item": {**_offer(item), "sale_price": sale_price}})
        return {"entries": rows, "positions": len(rows), "quantity": quantity,
                "purchase_total": round(purchase, 2), "sale_total": round(sale, 2)}

    @app.get("/api/cart")
    def get_cart(user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        with Session() as session:
            return cart_view(engine, load_cart(session, user["id"]))

    @app.post("/api/cart")
    def add_to_cart(request: CartAddRequest, user=Depends(current_user)):
        job = state["jobs"].get(request.job_id)
        if job is None or job.organization_id != user["organization_id"] or job.kind != "search" or not job.results:
            raise HTTPException(status_code=404, detail="результат поиска устарел — повторите поиск")
        result = job.results[0]
        alternatives = list(result.get("alternatives") or [])
        offer = next((o for o in alternatives if str(o.get("internal_offer_id")) == request.internal_offer_id), None)
        if offer is None:
            raise HTTPException(status_code=404, detail="предложение не найдено в результате поиска")
        engine = engine_for(user["organization_id"])
        messages = []
        with engine.lock, Session() as session:
            cart = load_cart(session, user["id"])
            engine.draft_cart, engine.displayed_data, engine._log_callback = cart, alternatives, messages.append
            try:
                engine._add_item_to_draft_cart(dict(offer), request.quantity)
            finally:
                engine.draft_cart, engine.displayed_data, engine._log_callback = None, [], None
            save_cart(session, user["id"], cart)
            view = cart_view(engine, cart)
        message = engine.redactor.text(" ".join(messages))
        if message.startswith("ОШИБКА"):
            raise HTTPException(status_code=400, detail=message.replace("ОШИБКА: ", ""))
        return {**view, "message": message}

    @app.put("/api/cart/{key}")
    def set_cart_quantity(key: str, request: CartQuantityRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        with engine.lock, Session() as session:
            cart = load_cart(session, user["id"])
            entry = next((e for e in cart.rows() if str(e.get("key")) == key), None)
            if entry is None:
                raise HTTPException(status_code=404, detail="позиции нет в корзине")
            info = engine._quantity_info(entry["item"], request.quantity)
            if not info.can_order:
                raise HTTPException(status_code=400, detail=info.reason or "нельзя заказать такое количество")
            cart.set_quantity(key, entry["item"], info.actual_int(), engine._quantity_limit_for_cart(info))
            save_cart(session, user["id"], cart)
            return cart_view(engine, cart)

    @app.delete("/api/cart/{key}")
    def remove_from_cart(key: str, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        with Session() as session:
            cart = load_cart(session, user["id"])
            cart.remove_keys([key])
            save_cart(session, user["id"], cart)
            return cart_view(engine, cart)

    @app.delete("/api/cart")
    def clear_cart(user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        with Session() as session:
            cart = load_cart(session, user["id"])
            cart.clear()
            save_cart(session, user["id"], cart)
            return cart_view(engine, cart)

    @app.post("/api/cart/checkout")
    def checkout(request: CheckoutRequest, user=Depends(current_user)):
        """Как «Оформить заказ» в корзине десктопа (_save_draft_order) + справочник клиентов."""
        engine = engine_for(user["organization_id"])
        with Session() as session:
            if not load_cart(session, user["id"]).rows():
                raise HTTPException(status_code=400, detail="корзина пуста")
        client_fields = resolve_client(user, request)
        with engine.lock, Session() as session:
            cart = load_cart(session, user["id"])
            if not cart.rows():
                raise HTTPException(status_code=400, detail="корзина пуста")
            order = order_from_cart(engine, user, cart, client_fields, manager=request.manager,
                                    ship_date=request.ship_date, comment=request.comment)
            cart.clear()
            save_cart(session, user["id"], cart)
        return {"order": _order_view(order)}

    def order_from_cart(engine, user, cart, client_fields, manager="", ship_date="", comment=""):
        """Заказ-черновик из строк корзины (DraftCart), как «Оформить заказ» десктопа. Под engine.lock."""
        entries = engine._order_entries_with_prices(cart.rows())
        engine.order_store.user_id = user["id"]
        try:
            order = engine.order_store.create_draft(
                manager=manager or user["name"] or user["email"], client=client_fields["name"],
                ship_date=ship_date or engine.order_store.calculated_ready_date(entries),
                entries=entries, comment=comment, client_vin=client_fields["vin"], grouping_mode="manual",
                verified_at=engine._entries_verified_at(entries))
            return link_client(engine.order_store, order, client_fields)
        finally:
            engine.order_store.user_id = None

    ctx.order_from_cart = order_from_cart
