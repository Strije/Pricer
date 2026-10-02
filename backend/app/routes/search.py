"""Поиск: задачи с прогрессом (SSE), новый поиск /api/find, старый /api/search, избранное, популярное, гарантии."""
import asyncio
import datetime
import json
import threading
import time

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from app import db
from app import orders as order_service
from app import search as search_service
from app.routes.common import FIND_LIMIT, Job, _result_payload
from app.schemas import FavoritesRequest, FindRequest, SearchRequest


def setup(app, ctx):
    Session, current_user, engine_for = ctx.Session, ctx.current_user, ctx.engine_for
    state = ctx.state

    # ----- фоновые задачи -----

    def replay_reset(engine):
        if state["replayer"] is not None:
            with state["lock"]:
                state["replayer"].reset()
            engine.provider_result_cache.clear()  # иначе повтор возьмёт кэш, а не запись

    def start_job(job, target):
        state["jobs"][job.id] = job
        for old in sorted(state["jobs"].values(), key=lambda j: j.created)[:-200]:
            state["jobs"].pop(old.id, None)

        def runner():
            engine = engine_for(job.organization_id)
            with engine.lock:
                try:
                    target(engine)
                except order_service.OrderActionError as exc:
                    job.push("error", {"message": engine.redactor.text(str(exc))})
                except Exception as exc:  # ошибка показывается пользователю, сервис продолжает работать
                    job.push("error", {"message": engine.redactor.text(f"{type(exc).__name__}: {exc}")[:300]})
                finally:
                    engine.on_provider_progress = None
                    engine.on_order_action = None
                    job.done = True

        threading.Thread(target=runner, daemon=True).start()
        return job

    # ----- поиск -----

    def run(job):
        engine = engine_for(job.organization_id)
        with engine.lock:
            engine.on_provider_progress = lambda stat: job.push("provider", engine.redactor.messages({
                key: stat.get(key) for key in ("provider", "status", "reason", "count", "raw_count", "elapsed", "message")
            }))
            try:
                if state["replayer"] is not None:
                    with state["lock"]:
                        state["replayer"].reset()
                    engine.provider_result_cache.clear()  # иначе повтор возьмёт кэш, а не запись
                req = job.request
                result = engine.search_order_row(
                    {"brand": req.brand, "article": req.article, "name": "", "quantity": req.quantity},
                    {"exact_match": not req.with_analogs, "include_no_return": False,
                     "ignore_warehouse_filters": False, "strategy": req.strategy, "max_days": req.max_days},
                )
                job.results = [result]  # для «В корзину»: предложение берём отсюда, не из браузера
                job.push("result", engine.redactor.messages(_result_payload(result)))
            except Exception as exc:  # ошибка показывается пользователю, сервис продолжает работать
                job.push("error", {"message": engine.redactor.text(f"{type(exc).__name__}: {exc}")[:300]})
            finally:
                engine.on_provider_progress = None
                job.done = True

    @app.post("/api/search")
    def start_search(request: SearchRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        if not engine.providers:
            raise HTTPException(status_code=400, detail="у организации не подключено ни одного поставщика")
        job = Job(request, user["organization_id"])
        state["jobs"][job.id] = job
        for old in sorted(state["jobs"].values(), key=lambda j: j.created)[:-200]:
            state["jobs"].pop(old.id, None)
        threading.Thread(target=run, args=(job,), daemon=True).start()
        return {"job_id": job.id, "providers": engine.provider_names}

    # ----- поиск «Проценки»: бренд голосованием, все поставщики, кроссы, ★ -----

    def run_find(job, customer):
        engine = engine_for(job.organization_id)
        req = job.request
        with engine.lock:
            last_push = [0.0]

            def on_event(kind, data):
                if kind == "provider":
                    job.push("provider", engine.redactor.messages(data))
                elif kind == "progress":
                    job.push("progress", {"percent": data})
                elif kind == "results" and time.monotonic() - last_push[0] > 0.7:
                    # промежуточная выдача — не чаще раза в 0,7 с; после завершения убирается из job
                    last_push[0] = time.monotonic()
                    # промежуточно — только сам искомый номер (его немного), аналоги придут в итоге
                    own = [i for i in data if not i.get("is_cross")][:300]
                    rows = [search_service.offer_view(i, engine, customer) for i in own]
                    drop_partials()
                    job.push("partial", engine.redactor.messages({"offers": rows, "total": len(data)}))

            def drop_partials():
                # Прежние промежуточные выдачи больше не нужны: заменяем их пустышкой на месте, чтобы
                # не держать в памяти и не сдвинуть номера событий у уже открытых потоков.
                for event in job.events:
                    if event["event"] == "partial" and not event["data"].get("stale"):
                        event["data"] = {"stale": True}

            try:
                replay_reset(engine)
                with Session() as session:
                    share, lead = search_service.brand_settings(session.get(db.Organization, job.organization_id).settings)
                choices, answered = engine.search_brands(req.article)
                if req.brand:
                    picked = next((c for c in choices if c["label"] == req.brand), None)
                elif req.brand_hint:
                    picked = search_service.match_hint(choices, req.brand_hint) or \
                        search_service.choose_brand(choices, answered, share, lead)
                else:
                    picked = search_service.choose_brand(choices, answered, share, lead)
                job.push("brands", {"choices": [search_service.brand_view(c) for c in choices],
                                    "answered": len(answered), "selected": picked["label"] if picked else "",
                                    "auto": bool(picked and not req.brand)})
                if choices and picked is None:
                    job.push("need_brand", {"article": req.article})
                    return
                engine.on_search_event = on_event
                results = engine.search_offers(req.article, picked["choice"] if picked else None)
                job.results = [{"alternatives": results}]  # для «В корзину»: предложение берём отсюда
                # Итог отдаётся отдельным сжатым запросом (/api/find/{id}/results): у ходовых номеров
                # тысячи предложений (162622 — 2407 в 359 группах), в потоке событий это мегабайты.
                ordered = sorted(results, key=lambda i: bool(i.get("is_cross")))  # искомый номер — всегда целиком
                rows = [search_service.offer_view(i, engine, customer) for i in ordered[:FIND_LIMIT]]
                job.payload = engine.redactor.messages({
                    "article": req.article, "brand": picked["brand"] if picked else "",
                    "offers": rows, "total": len(results), "highlights": search_service.highlights(rows),
                    "hide_no_return": bool(engine.hide_no_return)})
                drop_partials()
                job.push("done", {"total": len(results), "shown": len(rows)})
            except Exception as exc:  # ошибка показывается пользователю, сервис продолжает работать
                job.push("error", {"message": engine.redactor.text(f"{type(exc).__name__}: {exc}")[:300]})
            finally:
                engine.on_search_event = None
                engine.on_provider_progress = None
                job.done = True

    @app.post("/api/find")
    def start_find(request: FindRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        if not engine.providers:
            raise HTTPException(status_code=400, detail="у организации не подключено ни одного поставщика")
        job = Job(request, user["organization_id"])
        state["jobs"][job.id] = job
        for old in sorted(state["jobs"].values(), key=lambda j: j.created)[:-200]:
            state["jobs"].pop(old.id, None)
        threading.Thread(target=run_find, args=(job, user["role"] == "customer"), daemon=True).start()
        return {"job_id": job.id, "providers": engine.provider_names}

    @app.get("/api/find/{job_id}/results")
    def find_results(job_id: str, user=Depends(current_user)):
        job = state["jobs"].get(job_id)
        if job is None or job.organization_id != user["organization_id"] or getattr(job, "payload", None) is None:
            raise HTTPException(status_code=404, detail="результат поиска устарел — повторите поиск")
        return job.payload

    @app.get("/api/me/favorites")
    def get_favorites(user=Depends(current_user)):
        with Session() as session:
            prefs = session.get(db.User, user["id"]).prefs or {}
            return {"brands": prefs.get("favorite_brands", []), "providers": prefs.get("favorite_providers", [])}

    @app.put("/api/me/favorites")
    def put_favorites(request: FavoritesRequest, user=Depends(current_user)):
        clean = lambda values: sorted({str(v).strip()[:120] for v in values if str(v).strip()})  # noqa: E731
        with Session() as session:
            row = session.get(db.User, user["id"])
            row.prefs = {**(row.prefs or {}), "favorite_brands": clean(request.brands),
                         "favorite_providers": clean(request.providers)}
            session.commit()
            return {"brands": row.prefs["favorite_brands"], "providers": row.prefs["favorite_providers"]}

    @app.get("/api/stats/popular")
    def popular(days: int = 180, user=Depends(current_user)):
        """«Популярные» в фильтрах выдачи: что организация чаще всего заказывала у поставщиков."""
        from sqlalchemy import func

        since = db.utcnow() - datetime.timedelta(days=max(1, min(days, 3650)))
        with Session() as session:
            def top(column):
                rows = (session.query(column, func.count(db.SupplierLine.id))
                        .filter(db.SupplierLine.organization_id == user["organization_id"],
                                db.SupplierLine.submitted_at >= since, column != "")
                        .group_by(column).order_by(func.count(db.SupplierLine.id).desc()).limit(50).all())
                return [{"name": name, "count": count} for name, count in rows]
            return {"brands": top(db.SupplierLine.brand), "providers": top(db.SupplierLine.provider)}

    @app.get("/api/brands/warranty")
    def brand_warranty(user=Depends(current_user)):
        """Гарантии и рейтинг брендов (кубки): справочник организации или стартовый из приложения Abcp."""
        with Session() as session:
            own = (session.get(db.Organization, user["organization_id"]).settings or {}).get("brand_warranty")
        return own or search_service.default_warranty()

    @app.get("/api/jobs/{job_id}/events")
    @app.get("/api/search/{job_id}/events")
    async def events(job_id: str, user=Depends(current_user)):
        job = state["jobs"].get(job_id)
        if job is None or job.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="поиск не найден")

        async def stream():
            sent = 0
            while True:
                while sent < len(job.events):
                    item = job.events[sent]
                    sent += 1
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                if job.done and sent >= len(job.events):
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    ctx.replay_reset = replay_reset
    ctx.start_job = start_job
