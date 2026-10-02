"""Подборы для клиента: варианты, ссылка, выбор, оплата ЮKassa (опрос платежей), «ТО по машине»."""
import datetime
import os
import threading

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app import clients as client_service
from app import db
from app import quotes as quote_service
from app import search as search_service
from app.routes.common import Job, _order_view, STATIC
from app.schemas import (
    QuoteChoiceRequest, QuoteLineRequest, QuoteRequest, QuoteSendRequest, QuoteVariantRequest,
    ServiceQuoteRequest,
)


def setup(app, ctx):
    Session, box, engine_for = ctx.Session, ctx.box, ctx.engine_for
    replay_mode, staff_user, state = ctx.replay_mode, ctx.staff_user, ctx.state
    stop_background = ctx.stop_background
    laximo_client, order_from_cart, replay_reset = ctx.laximo_client, ctx.order_from_cart, ctx.replay_reset
    start_job = ctx.start_job

    # ----- подбор для клиента: варианты по ссылке, выбор клиента, заказ -----


    def _quote(session, user, quote_id):
        quote = session.get(db.Quote, quote_id)
        if quote is None or quote.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="подбор не найден")
        return quote

    def quote_call(fn):
        try:
            return fn()
        except quote_service.QuoteError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def quote_client(user, request):
        if not (request.client or request.client_id or request.phone):
            return None, None, ""
        with Session() as session:
            try:
                client, vehicle = client_service.resolve(
                    session, user["organization_id"], client_id=request.client_id, vehicle_id=request.vehicle_id,
                    name=request.client, phone=request.phone, vin=request.vin)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            session.commit()
            return client.id if client else None, vehicle.id if vehicle else None, client.name if client else ""

    @app.get("/api/quotes")
    def list_quotes(user=Depends(staff_user)):
        with Session() as session:
            rows = (session.query(db.Quote).filter_by(organization_id=user["organization_id"])
                    .order_by(db.Quote.id.desc()).limit(200).all())
            return [{k: v for k, v in quote_service.manager_view(q).items() if k != "lines"} for q in rows]

    @app.post("/api/quotes")
    def create_quote(request: QuoteRequest, user=Depends(staff_user)):
        client_id, vehicle_id, client_name = quote_client(user, request)
        with Session() as session:
            quote = db.Quote(organization_id=user["organization_id"], token=quote_service.new_token(),
                             title=request.title.strip() or f"Подбор от {db.utcnow():%d.%m.%Y %H:%M}",
                             client_id=client_id, vehicle_id=vehicle_id, client_name=client_name,
                             created_by=user["id"], data={"lines": []})
            session.add(quote)
            session.commit()
            return quote_service.manager_view(quote)

    @app.get("/api/quotes/{quote_id}")
    def get_quote(quote_id: int, user=Depends(staff_user)):
        with Session() as session:
            quote = _quote(session, user, quote_id)
            refresh_payment(session, quote)  # ожидает оплаты — спросим ЮKassa сейчас
            return quote_service.manager_view(quote)

    @app.put("/api/quotes/{quote_id}")
    def update_quote(quote_id: int, request: QuoteRequest, user=Depends(staff_user)):
        client_id, vehicle_id, client_name = quote_client(user, request)
        with Session() as session:
            quote = _quote(session, user, quote_id)
            if request.title.strip():
                quote.title = request.title.strip()
            if client_id:
                quote.client_id, quote.vehicle_id, quote.client_name = client_id, vehicle_id, client_name
            session.commit()
            return quote_service.manager_view(quote)

    @app.delete("/api/quotes/{quote_id}")
    def delete_quote(quote_id: int, user=Depends(staff_user)):
        with Session() as session:
            session.delete(_quote(session, user, quote_id))
            session.commit()
        return {"ok": True}

    @app.post("/api/quotes/{quote_id}/lines")
    def add_quote_line(quote_id: int, request: QuoteLineRequest, user=Depends(staff_user)):
        with Session() as session:
            quote = _quote(session, user, quote_id)
            quote_call(lambda: quote_service.add_line(quote, request.request or "", request.qty or 1))
            session.commit()
            return quote_service.manager_view(quote)

    @app.put("/api/quotes/{quote_id}/lines/{line_id}")
    def update_quote_line(quote_id: int, line_id: str, request: QuoteLineRequest, user=Depends(staff_user)):
        with Session() as session:
            quote = _quote(session, user, quote_id)
            quote_call(lambda: quote_service.update_line(quote, line_id, request.request, request.qty))
            session.commit()
            return quote_service.manager_view(quote)

    @app.delete("/api/quotes/{quote_id}/lines/{line_id}")
    def delete_quote_line(quote_id: int, line_id: str, user=Depends(staff_user)):
        with Session() as session:
            quote = _quote(session, user, quote_id)
            quote_service.remove_line(quote, line_id)
            session.commit()
            return quote_service.manager_view(quote)

    @app.post("/api/quotes/{quote_id}/variants")
    def add_quote_variant(quote_id: int, request: QuoteVariantRequest, user=Depends(staff_user)):
        """Вариант из выдачи поиска. Без line_id позиция ищется по запросу поиска (или создаётся):
        все варианты одного поиска попадают в одну позицию подбора."""
        job = state["jobs"].get(request.job_id)
        if job is None or job.organization_id != user["organization_id"] or not job.results:
            raise HTTPException(status_code=404, detail="результат поиска устарел — повторите поиск")
        offer = next((o for o in job.results[0].get("alternatives") or []
                      if str(o.get("internal_offer_id")) == request.internal_offer_id), None)
        if offer is None:
            raise HTTPException(status_code=404, detail="предложение не найдено в результате поиска")
        engine = engine_for(user["organization_id"])
        sale = float(engine.apply_markup(float(offer.get("purchase_price", offer.get("price")) or 0))[0])
        searched = getattr(job.request, "article", "") or ""
        brand = (job.payload or {}).get("brand", "") if getattr(job, "payload", None) else ""
        with Session() as session:
            quote = _quote(session, user, quote_id)

            def add():
                line_id = request.line_id
                if not line_id:
                    title = request.request.strip() or " ".join(x for x in (brand, searched) if x)
                    found = next((ln for ln in quote_service.lines(quote) if ln.get("search") == searched.upper()), None)
                    if found is None:
                        found = quote_service.add_line(quote, title, request.qty, search=searched.upper())
                    line_id = found["id"]
                return quote_service.add_variant(quote, line_id, offer, sale)
            quote_call(add)
            session.commit()
            return quote_service.manager_view(quote)

    @app.delete("/api/quotes/{quote_id}/lines/{line_id}/variants/{key}")
    def delete_quote_variant(quote_id: int, line_id: str, key: str, user=Depends(staff_user)):
        with Session() as session:
            quote = _quote(session, user, quote_id)
            quote_call(lambda: quote_service.remove_variant(quote, line_id, key))
            session.commit()
            return quote_service.manager_view(quote)

    @app.post("/api/quotes/{quote_id}/send")
    def send_quote(quote_id: int, request: QuoteSendRequest, user=Depends(staff_user)):
        with Session() as session:
            quote = _quote(session, user, quote_id)
            quote_call(lambda: quote_service.send(quote, request.hours))
            session.commit()
            return quote_service.manager_view(quote)

    @app.post("/api/quotes/{quote_id}/order")
    def order_from_quote(quote_id: int, user=Depends(staff_user)):
        """Выбор клиента -> заказ-черновик: выбранный вариант — позиция, остальные варианты позиции —
        запасные (замена варианта в карточке заказа). Цены перепроверяются, как у любого заказа."""
        from cart_store import DraftCart

        with Session() as session:
            quote = _quote(session, user, quote_id)
            picked = quote_service.chosen_offers(quote)
            if not picked:
                raise HTTPException(status_code=400, detail="клиент ещё ничего не выбрал")
            if quote.order_id:
                raise HTTPException(status_code=400, detail=f"заказ уже оформлен: {quote.order_id}")
            client_fields = {"name": quote.client_name, "vin": ""}
            if quote.client_id:
                client = session.get(db.Client, quote.client_id)
                vehicle = session.get(db.Vehicle, quote.vehicle_id) if quote.vehicle_id else None
                client_fields = client_service.order_client_fields(client, vehicle)
            comment = " ".join(x for x in (f"Подбор «{quote.title}»", (quote.data or {}).get("client_comment", "")) if x)
        engine = engine_for(user["organization_id"])
        cart = DraftCart()
        with engine.lock:
            engine.draft_cart, engine._log_callback = cart, None
            try:
                for _line, offer, qty, alternatives in picked:
                    engine.displayed_data = alternatives
                    engine._add_item_to_draft_cart(offer, qty)
            finally:
                engine.draft_cart, engine.displayed_data = None, []
            if not cart.rows():
                raise HTTPException(status_code=400, detail="выбранные варианты не удалось добавить в заказ")
            order = order_from_cart(engine, user, cart, client_fields, comment=comment)
        with Session() as session:
            quote = _quote(session, user, quote_id)
            quote.order_id, quote.status = order["order_id"], "ordered"
            session.commit()
        return {"order": _order_view(order)}

    # оплата подбора через ЮKassa

    def yookassa_settings(organization_id):
        with Session() as session:
            account = session.query(db.SupplierAccount).filter_by(organization_id=organization_id, section="yookassa").first()
            if account is None or (account.config or {}).get("enabled") is False:
                return None
            return {**(account.config or {}), **box.open(account.secrets_sealed)}

    def yookassa_client(cfg):
        from yookassa import YooKassaClient

        test = bool(cfg.get("test_mode"))
        return YooKassaClient(cfg.get("test_shop_id") if test else cfg.get("shop_id"),
                              cfg.get("test_secret_key") if test else cfg.get("secret_key"))

    def refresh_payment(session, quote):
        """Статус платежа берём у ЮKassa (не верим ни странице возврата, ни телу уведомления)."""
        payment = (quote.data or {}).get("payment") or {}
        if not payment.get("id") or payment.get("status") in ("succeeded", "canceled"):
            return payment
        cfg = yookassa_settings(quote.organization_id)
        if not cfg:
            return payment
        try:
            fresh = yookassa_client(cfg).get_payment(payment["id"])
        except Exception:
            return payment
        if fresh["status"] != payment.get("status"):
            payment = {**payment, "status": fresh["status"]}
            if fresh["status"] == "succeeded":
                payment["paid_at"] = db.utcnow().isoformat(timespec="seconds")
                session.add(db.Notification(organization_id=quote.organization_id, kind="quote_paid", payload={
                    "quote_id": quote.id, "title": quote.title, "client": quote.client_name, "amount": payment.get("amount")}))
            quote.data = {**(quote.data or {}), "payment": payment}
            session.commit()
        return payment

    def public_base(request):
        return (os.environ.get("PRICER_PUBLIC_URL") or str(request.base_url)).rstrip("/")

    # публичная часть: клиент по ссылке, без входа

    @app.get("/q/{token}")
    def quote_page(token: str):
        return FileResponse(os.path.join(STATIC, "quote.html"))

    def _public_quote(session, token):
        quote = session.query(db.Quote).filter_by(token=token).first()
        if quote is None or quote.status == "draft":
            raise HTTPException(status_code=404, detail="подбор не найден или ещё не отправлен")
        return quote

    def _warranty_lookup(org):
        table = ((org.settings or {}).get("brand_warranty") or search_service.default_warranty()).get("brands", {})

        def lookup(brand):
            w = table.get(search_service._brand_key(brand))
            return {"warranty": w["warranty"], "rating": w["rating"]} if w else None
        return lookup

    def public_payload(session, quote):
        org = session.get(db.Organization, quote.organization_id)
        view = quote_service.public_view(quote, org.name, _warranty_lookup(org))
        payment = refresh_payment(session, quote)
        view["payment"] = {k: payment.get(k) for k in ("status", "amount", "paid_at")} if payment else None
        view["can_pay"] = bool(yookassa_settings(quote.organization_id)) and quote.status in ("chosen", "ordered") \
            and view["total"] > 0 and (payment or {}).get("status") != "succeeded"
        if (payment or {}).get("status") == "succeeded":
            view["locked"] = True  # оплаченный выбор не меняется
        return view

    @app.get("/api/public/quote/{token}")
    def public_quote(token: str):
        with Session() as session:
            quote = _public_quote(session, token)
            if quote.viewed_at is None:
                quote.viewed_at = db.utcnow()
                if quote.status == "sent":
                    quote.status = "viewed"
                session.commit()
            return public_payload(session, quote)

    @app.post("/api/public/quote/{token}/pay")
    def public_pay(token: str, http_request: Request):
        """Платёж ЮKassa на сумму выбранного; клиент уходит на страницу ЮKassa (карта, СБП, SberPay)."""
        from yookassa import YooKassaError, receipt

        with Session() as session:
            quote = _public_quote(session, token)
            view = public_payload(session, quote)
            if not view["can_pay"]:
                raise HTTPException(status_code=400, detail="оплата сейчас недоступна — выберите варианты или свяжитесь с менеджером")
            cfg = yookassa_settings(quote.organization_id)
            items = [(f"{v['brand']} {v['article']} {v['name']}".strip(), line["qty"], v["sale_price"])
                     for line in view["lines"] for v in line["variants"] if v["key"] == line.get("choice")]
            payment = (quote.data or {}).get("payment") or {}
            if payment.get("id") and payment.get("status") == "pending" and abs(float(payment.get("amount") or 0) - view["total"]) < 0.01:
                return {"url": payment.get("url")}  # уже создан на эту сумму — тот же платёж
            try:
                receipt_data = None
                if cfg.get("receipts"):
                    contact = (quote.data or {}).get("contact") or ""
                    if not contact and quote.client_id:
                        client = session.get(db.Client, quote.client_id)
                        contact = (client.email or client.phone) if client else ""
                    receipt_data = receipt(items, contact, tax_system_code=cfg.get("tax_system_code"),
                                           vat_code=cfg.get("vat_code") or 1,
                                           payment_subject=cfg.get("payment_subject") or "commodity",
                                           payment_mode=cfg.get("payment_mode") or "full_prepayment")
                created = yookassa_client(cfg).create_payment(
                    view["total"], f"Оплата: {quote.title}"[:128], f"{public_base(http_request)}/q/{token}?paid=1",
                    metadata={"quote_id": str(quote.id), "organization_id": str(quote.organization_id)},
                    receipt_data=receipt_data, idempotence_key=f"quote-{quote.id}-{view['total']:.2f}-{len(quote.data.get('payments_tried', []))}")
            except YooKassaError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            quote.data = {**quote.data, "payment": {**created, "created_at": db.utcnow().isoformat(timespec="seconds")},
                          "payments_tried": [*quote.data.get("payments_tried", []), created["id"]]}
            session.commit()
            return {"url": created["url"]}

    @app.post("/api/yookassa/webhook")
    async def yookassa_webhook(request: Request):
        """Уведомление ЮKassa (payment.succeeded и т.п.): тело — только подсказка, какой платёж
        перепроверить; статус берём запросом к ЮKassa. Адрес указывается в личном кабинете ЮKassa."""
        try:
            body = await request.json()
        except ValueError:
            return {"ok": True}
        obj = (body or {}).get("object") or {}
        quote_id = str((obj.get("metadata") or {}).get("quote_id") or "")
        if quote_id.isdigit():
            with Session() as session:
                quote = session.get(db.Quote, int(quote_id))
                if quote is not None and ((quote.data or {}).get("payment") or {}).get("id") == obj.get("id"):
                    refresh_payment(session, quote)
        return {"ok": True}

    @app.post("/api/public/quote/{token}/choose")
    def public_choose(token: str, request: QuoteChoiceRequest):
        with Session() as session:
            quote = _public_quote(session, token)
            if (((quote.data or {}).get("payment") or {}).get("status")) == "succeeded":
                raise HTTPException(status_code=400, detail="подбор оплачен — выбор уже не меняется")
            quote_call(lambda: quote_service.choose(quote, request.choices, request.comment, request.contact))
            chosen = sum(1 for ln in quote_service.lines(quote) if ln.get("choice") and ln["choice"] != "skip")
            session.add(db.Notification(organization_id=quote.organization_id, kind="quote_chosen", payload={
                "quote_id": quote.id, "title": quote.title, "client": quote.client_name, "chosen": chosen,
                "positions": len(quote_service.lines(quote)), "comment": request.comment[:200]}))
            session.commit()
            return public_payload(session, quote)

    @app.post("/api/quotes/service")
    def service_quote(request: ServiceQuoteRequest, user=Depends(staff_user)):
        """«ТО по машине»: по шаблону — номера из Laximo, поиск у поставщиков, три варианта в позицию.
        Идёт фоном (несколько поисков подряд); прогресс — события item, итог — done с подбором."""
        from app import service_template

        laximo = laximo_client(user)
        with Session() as session:
            org = session.get(db.Organization, user["organization_id"])
            items = [str(x).strip()[:80] for x in (request.items or []) if str(x).strip()] or \
                service_template.items_for(org.settings)
            if request.quote_id:
                quote = _quote(session, user, request.quote_id)
            else:
                quote = db.Quote(organization_id=user["organization_id"], token=quote_service.new_token(),
                                 title=request.title.strip() or "ТО по машине", created_by=user["id"], data={"lines": []})
                session.add(quote)
            session.commit()
            quote_id = quote.id
        job = Job(request, user["organization_id"], kind="service")
        vehicle = {**request.vehicle.model_dump(), "oem_brand": request.oem_brand}

        def work(engine):
            replay_reset(engine)
            found = service_template.collect(
                engine, laximo, vehicle, items,
                lambda item, status, data=None: job.push("item", {"item": item, "status": status, **(data or {})}))
            with Session() as session:
                quote = session.get(db.Quote, quote_id)
                for item, oem, variants in found:
                    line = quote_service.add_line(quote, item, 1, search=oem.upper())
                    for offer in variants:
                        sale = float(engine.apply_markup(float(offer.get("purchase_price", offer.get("price")) or 0))[0])
                        quote_service.add_variant(quote, line["id"], offer, sale)
                session.commit()
                view = quote_service.manager_view(quote)
            job.push("done", {"quote": view, "filled": sum(1 for _, _, v in found if v), "items": len(found)})

        start_job(job, work)
        return {"job_id": job.id, "quote_id": quote_id, "items": items}

    # Оплаты подборов: уведомления ЮKassa не обязательны (адрес уведомлений у магазина один, и его
    # может занимать сайт на ABCP) — незавершённые платежи последних 2 суток сервер проверяет сам.
    payment_seconds = float(os.environ.get("PRICER_PAYMENT_CHECK_SECONDS", "0" if replay_mode else "120") or 0)

    def payment_watch():
        while not stop_background.wait(payment_seconds):
            try:
                since = db.utcnow() - datetime.timedelta(days=2)
                with Session() as session:
                    for quote in session.query(db.Quote).filter(db.Quote.chosen_at >= since):
                        if ((quote.data or {}).get("payment") or {}).get("status") == "pending":
                            refresh_payment(session, quote)
            except Exception as exc:  # проверка оплат не должна ронять сервис
                print(f"[pricer] проверка оплат: {type(exc).__name__}: {str(exc)[:200]}")

    if payment_seconds > 0:
        threading.Thread(target=payment_watch, daemon=True, name="payment-watch").start()

    ctx.yookassa_client = yookassa_client
