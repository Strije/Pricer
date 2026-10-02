"""Заказ из файла и заказы: черновик, перепроверка, отправка поставщикам, пропуск, замена позиции, журнал."""
import os

from fastapi import Depends, File, HTTPException, UploadFile

from app import db
from app import orders as order_service
from app import supplier_lines as lines_service
from app.routes.common import _file_row_payload, Job, _offer, ORDER_FILE_LIMIT, ORDER_FILE_ROWS, _order_view
from app.schemas import CreateOrderRequest, FileSearchRequest, ItemsRequest, ReplaceRequest


def setup(app, ctx):
    Session, current_user, engine_for = ctx.Session, ctx.current_user, ctx.engine_for
    state = ctx.state
    link_client, replay_reset, resolve_client = ctx.link_client, ctx.replay_reset, ctx.resolve_client
    start_job = ctx.start_job

    # ----- заказ из файла -----

    @app.post("/api/order-file/parse")
    async def parse_order_file(file: UploadFile = File(...), user=Depends(current_user)):
        import tempfile

        from sales_report_importer import read_sales_report

        raw = await file.read(ORDER_FILE_LIMIT + 1)
        if len(raw) > ORDER_FILE_LIMIT:
            raise HTTPException(status_code=413, detail="файл больше 5 МБ")
        suffix = os.path.splitext(file.filename or "")[1].lower()
        if suffix not in (".csv", ".txt", ".xlsx", ".xlsm", ".xls"):
            raise HTTPException(status_code=400, detail="поддерживаются CSV, TXT, XLSX и выгрузки 1С в XLS")
        if suffix == ".xls" and not raw.lstrip()[:1] == b"<":
            raise HTTPException(status_code=400, detail="старый двоичный XLS не поддерживается — сохраните файл как XLSX")
        # ignore_cleanup_errors: на Windows файл, который библиотека не закрыла после сбоя, нельзя
        # удалить сразу — это не должно превращать понятную ошибку 400 в 500.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "order" + suffix)
            with open(path, "wb") as handle:
                handle.write(raw)
            try:
                if suffix == ".xls":
                    # Выгрузка 1С: внутри HTML-таблица (читает bulk_pricing десктопа).
                    from bulk_pricing import _read_html_table
                    from sales_report_importer import merge_sales_report_rows, parse_sales_report_rows

                    parsed, errors = parse_sales_report_rows(_read_html_table(path))
                    rows = merge_sales_report_rows(parsed)
                else:
                    rows, errors = read_sales_report(path)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"файл не читается: {exc}"[:300])
        if len(rows) > ORDER_FILE_ROWS:
            raise HTTPException(status_code=400, detail=f"в файле больше {ORDER_FILE_ROWS} строк")
        return {"rows": [{k: r.to_dict()[k] for k in ("brand", "article", "name", "quantity")} for r in rows],
                "errors": list(errors or [])[:50]}

    @app.post("/api/order-file/search")
    def search_order_file(request: FileSearchRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        if not engine.providers:
            raise HTTPException(status_code=400, detail="у организации не подключено ни одного поставщика")
        job = Job(request, user["organization_id"], kind="file")
        options = {"exact_match": not request.with_analogs, "include_no_return": request.include_no_return,
                   "ignore_warehouse_filters": False, "strategy": request.strategy, "max_days": request.max_days}

        def work(engine):
            ready = 0
            for index, row in enumerate(request.rows):
                replay_reset(engine)
                try:
                    result = engine.search_order_row(row.model_dump(), options)
                except Exception as exc:
                    result = {"status_code": "error", "status": "Ошибка", "reason": str(exc)[:240],
                              "offer": None, "alternatives": [], "quantity": row.quantity, "source": row.model_dump()}
                job.results.append(result)
                ready += result.get("status_code") == "ready"
                job.push("row", engine.redactor.messages(_file_row_payload(index, result)))
            job.push("done", {"ready": ready, "total": len(request.rows)})

        start_job(job, work)
        return {"job_id": job.id, "total": len(request.rows)}

    @app.post("/api/orders")
    def create_order(request: CreateOrderRequest, user=Depends(current_user)):
        job = state["jobs"].get(request.job_id)
        if job is None or job.organization_id != user["organization_id"] or job.kind != "file":
            raise HTTPException(status_code=404, detail="подбор не найден — повторите поиск по файлу")
        if not job.done:
            raise HTTPException(status_code=409, detail="подбор ещё идёт")
        engine = engine_for(user["organization_id"])
        with engine.lock:
            entries, errors = order_service.prepare_entries(engine, job.results, request.rows, request.selections)
            if not entries:
                raise HTTPException(status_code=400, detail="нет строк, готовых к заказу")
            client_fields = resolve_client(user, request)
            engine.order_store.user_id = user["id"]
            try:
                order = order_service.create_order_from_file(
                    engine, engine.order_store, entries, manager=request.manager or user["name"] or user["email"],
                    client=client_fields["name"], ship_date=request.ship_date, comment=request.comment,
                    vin=client_fields["vin"])
                order = link_client(engine.order_store, order, client_fields)
            except order_service.OrderActionError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            finally:
                engine.order_store.user_id = None
        return {"order": _order_view(order), "warnings": errors}

    # ----- заказы -----

    def _store(user):
        return engine_for(user["organization_id"]).order_store

    @app.get("/api/orders")
    def list_orders(user=Depends(current_user)):
        rows = []
        for summary in _store(user).list_order_summaries():
            items = summary.get("items") or []
            rows.append({k: summary.get(k) for k in ("order_id", "status", "created_at", "updated_at", "client",
                                                     "manager", "totals", "verification_status", "client_ship_date")}
                        | {"items_count": len(items),
                           "unknown_count": sum(1 for i in items if i.get("submit_status") == "unknown")})
        return rows

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: str, user=Depends(current_user)):
        order = _store(user).read(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="заказ не найден")
        return _order_view(order)

    @app.get("/api/orders/{order_id}/submit-preview")
    def preview_submit(order_id: str, items: str | None = None, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        indexes = [int(x) for x in items.split(",") if x.strip()] if items else None
        with engine.lock:
            try:
                return order_service.submit_preview(engine, engine.order_store, order_id, indexes)
            except order_service.OrderActionError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/orders/{order_id}/recheck")
    def recheck_order(order_id: str, request: ItemsRequest, user=Depends(current_user)):
        job = Job(request, user["organization_id"], kind="recheck")

        def work(engine):
            replay_reset(engine)
            order = order_service.recheck(engine, engine.order_store, order_id, request.items)
            job.push("done", {"order": _order_view(order)})

        start_job(job, work)
        return {"job_id": job.id}

    @app.post("/api/orders/{order_id}/submit")
    def submit_order(order_id: str, request: ItemsRequest, user=Depends(current_user)):
        if not request.confirm:
            raise HTTPException(status_code=400, detail="подтвердите отправку (confirm: true)")
        job = Job(request, user["organization_id"], kind="submit")

        def work(engine):
            messages = []
            engine.on_order_action = lambda order, message: messages.append(message)
            preview, order = order_service.submit(engine, engine.order_store, Session, order_id, request.items)
            with Session() as session:
                lines_service.sync_order(session, job.organization_id, order)
                session.commit()
            job.push("done", engine.redactor.messages({"order": _order_view(order), "message": " ".join(messages), "preview": preview}))

        start_job(job, work)
        return {"job_id": job.id}

    def _item_action(user, order_id, index, action):
        engine = engine_for(user["organization_id"])
        with engine.lock:
            if not engine.order_store.read(order_id):
                raise HTTPException(status_code=404, detail="заказ не найден")
            messages = []
            engine._log_callback = messages.append
            try:
                action(engine)
            finally:
                engine._log_callback = None
            return engine.redactor.messages({"order": _order_view(engine.order_store.read(order_id)), "message": " ".join(messages)})

    @app.post("/api/orders/{order_id}/items/{index}/skip")
    def skip_item(order_id: str, index: int, user=Depends(current_user)):
        return _item_action(user, order_id, index, lambda engine: engine._skip_order_item(order_id, index))

    @app.post("/api/orders/{order_id}/items/{index}/restore")
    def restore_item(order_id: str, index: int, user=Depends(current_user)):
        return _item_action(user, order_id, index, lambda engine: engine._restore_order_item(order_id, index))

    @app.get("/api/orders/{order_id}/log")
    def order_log(order_id: str, user=Depends(current_user)):
        with Session() as session:
            rows = session.query(db.SubmissionLog).filter_by(
                organization_id=user["organization_id"], order_id=order_id).order_by(db.SubmissionLog.id)
            return [{"created_at": r.created_at.isoformat(timespec="seconds"), "provider": r.provider,
                     "brand": r.brand, "article": r.article, "quantity": r.quantity, "success": r.success,
                     "response": (r.response or {}).get("response")} for r in rows]

    # ----- замена варианта позиции (из групп заказа, как кнопка «Выбрать вариант» в десктопе) -----

    def _variant_group(order, index):
        items = order.get("items") or []
        if not (0 <= index < len(items)):
            raise HTTPException(status_code=404, detail="позиция не найдена")
        current = str(items[index].get("internal_offer_id") or "")
        for group in order.get("groups") or []:
            if any(str(o.get("internal_offer_id") or "") == current for o in group.get("offers") or []):
                return group, current
        return None, current

    @app.get("/api/orders/{order_id}/items/{index}/variants")
    def item_variants(order_id: str, index: int, user=Depends(current_user)):
        order = _store(user).read(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="заказ не найден")
        group, current = _variant_group(order, index)
        if group is None:
            return []
        return [{**_offer({**(o.get("snapshot") or {}), **o}), "price": o.get("purchase_price"),
                 "sale_price": o.get("sale_price")}
                for o in group.get("offers") or [] if str(o.get("internal_offer_id") or "") != current]

    @app.post("/api/orders/{order_id}/items/{index}/replace")
    def replace_item(order_id: str, index: int, request: ReplaceRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        order = engine.order_store.read(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="заказ не найден")
        group, _current = _variant_group(order, index)
        offer = next((o for o in (group or {}).get("offers") or []
                      if str(o.get("internal_offer_id") or "") == request.internal_offer_id), None)
        if offer is None:
            raise HTTPException(status_code=404, detail="вариант не найден среди сохранённых в заказе")
        variant = {**(offer.get("snapshot") or {}), **{k: v for k, v in offer.items() if k != "snapshot"}}
        return _item_action(user, order_id, index,
                            lambda eng: eng._replace_order_item_with_variant(order_id, index, variant))
