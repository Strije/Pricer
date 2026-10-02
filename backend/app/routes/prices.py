"""Прайс-листы: источники, предпросмотр и разметка колонок, загрузка вручную и по расписанию."""
import os
import re
import threading

from fastapi import Depends, HTTPException

from app import db
from app import prices as price_service
from app.redact import Redactor
from app.routes.common import Job
from app.schemas import PriceSourceRequest


def setup(app, ctx):
    Session, admin_user, box = ctx.Session, ctx.admin_user, ctx.box
    invalidate, replay_mode, staff_user = ctx.invalidate, ctx.replay_mode, ctx.staff_user
    state, stop_background = ctx.state, ctx.stop_background

    # ----- прайс-листы -----

    PRICE_SETTING_KEYS = {"column_map", "has_header", "default_days", "warehouse", "site_search_url", "mail"}

    def _price_source(session, user, source_id):
        source = session.get(db.PriceSource, source_id)
        if source is None or source.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="прайс-лист не найден")
        return source

    def import_desktop_prices(session, organization_id):
        account = session.query(db.SupplierAccount).filter_by(organization_id=organization_id, section="url_csv").first()
        if account is None:
            return 0
        return price_service.import_desktop(session, box, organization_id,
                                            {**(account.config or {}), **box.open(account.secrets_sealed)})

    @app.get("/api/prices")
    def list_prices(user=Depends(staff_user)):
        with Session() as session:
            if not session.query(db.PriceSource.id).filter_by(organization_id=user["organization_id"]).first():
                if import_desktop_prices(session, user["organization_id"]):  # первый вход: источники из settings.json
                    invalidate(user["organization_id"])
            rows = session.query(db.PriceSource).filter_by(organization_id=user["organization_id"]).order_by(db.PriceSource.name)
            return {"sources": [price_service.view(s) for s in rows], "schedules": price_service.SCHEDULES,
                    "fields": [{"code": c, "label": t, "required": r} for c, t, r in price_service.FIELDS],
                    "warn_days": price_service.WARN_DAYS, "stale_days": price_service.STALE_DAYS}

    @app.post("/api/prices")
    def create_price(request: PriceSourceRequest, user=Depends(admin_user)):
        with Session() as session:
            source = db.PriceSource(organization_id=user["organization_id"], name=(request.name or "Новый прайс").strip(),
                                    kind=request.kind or "url", schedule_hours=request.schedule_hours or 24, settings={})
            session.add(source)
            apply_price(source, request)
            session.commit()
            view = price_service.view(source)
        invalidate(user["organization_id"])
        return view

    def apply_price(source, request):
        for field in ("name", "kind", "enabled", "schedule_hours"):
            value = getattr(request, field)
            if value is not None:
                setattr(source, field, value.strip() if isinstance(value, str) else value)
        if request.location:
            location = request.location.strip()
            if not re.match(r"(?i)^(https?|ftp)://", location):
                raise HTTPException(status_code=400, detail="адрес прайса начинается с https://, http:// или ftp://")
            source.location_sealed = box.seal({"location": location})
            source.location_hint = price_service.location_hint(location)
            if location.lower().startswith("ftp"):
                source.kind = "ftp"
        if request.settings is not None:
            unknown = set(request.settings) - PRICE_SETTING_KEYS
            if unknown:
                raise HTTPException(status_code=400, detail=f"неизвестные настройки: {', '.join(sorted(unknown))}")
            source.settings = {**(source.settings or {}), **request.settings}

    @app.put("/api/prices/{source_id}")
    def update_price(source_id: int, request: PriceSourceRequest, user=Depends(admin_user)):
        with Session() as session:
            source = _price_source(session, user, source_id)
            apply_price(source, request)
            session.commit()
            view = price_service.view(source)
        invalidate(user["organization_id"])
        return view

    @app.delete("/api/prices/{source_id}")
    def delete_price(source_id: int, user=Depends(admin_user)):
        with Session() as session:
            source = _price_source(session, user, source_id)
            session.query(db.PriceRow).filter_by(source_id=source.id).delete()
            session.delete(source)
            session.commit()
        invalidate(user["organization_id"])
        return {"ok": True}

    @app.post("/api/prices/import-desktop")
    def import_prices(user=Depends(admin_user)):
        with Session() as session:
            added = import_desktop_prices(session, user["organization_id"])
        invalidate(user["organization_id"])
        return {"added": added}

    @app.post("/api/prices/{source_id}/preview")
    def preview_price(source_id: int, user=Depends(admin_user)):
        """Первые строки файла и колонки — для разметки (файл скачивается заново, в базу не пишется)."""
        with Session() as session:
            source = _price_source(session, user, source_id)
            location = box.open(source.location_sealed).get("location", "") if source.location_sealed else ""
            settings = dict(source.settings or {})
            secrets_list = [location]
            if source.kind == "email":
                try:
                    _, _, rows, letter = price_service.open_mail(session, box, source)
                    return {**price_service.preview(rows, settings), "letter": letter}
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=str(exc)[:300])
        if not location:
            raise HTTPException(status_code=400, detail="сначала укажите адрес прайса")
        try:
            return price_service.preview(price_service.open_rows(location, settings)[1], settings)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=Redactor(secrets_list).text(str(exc))[:300])

    price_lock = threading.Lock()  # загрузки прайсов — по одной: файлы бывают по сотне мегабайт

    def load_price(source_id, force=False):
        with price_lock, Session() as session:
            source = session.get(db.PriceSource, source_id)
            if source is None:
                return {}
            location = box.open(source.location_sealed).get("location", "") if source.location_sealed else ""
            status = price_service.load(session, box, source, force=force)
            if location and status.get("message"):
                status["message"] = Redactor([location]).text(status["message"])
                source.status = status
                session.commit()
            invalidate(source.organization_id)
            return status

    @app.post("/api/prices/{source_id}/load")
    def load_price_now(source_id: int, user=Depends(admin_user)):
        with Session() as session:
            _price_source(session, user, source_id)
        job = Job(None, user["organization_id"], kind="price")

        def work():
            job.push("done", load_price(source_id, force=True))  # вручную — и то же письмо загрузить заново
            job.done = True
        queued = price_lock.locked()  # идёт другая загрузка — эта начнётся следом
        state["jobs"][job.id] = job
        threading.Thread(target=work, daemon=True).start()
        return {"job_id": job.id, "queued": queued}

    # Загрузка прайсов по расписанию источника (раз в сутки по умолчанию, местные склады — хоть раз в час).
    price_seconds = float(os.environ.get("PRICER_PRICE_CHECK_SECONDS", "0" if replay_mode else "60") or 0)

    def price_watch():
        while not stop_background.wait(price_seconds):
            try:
                with Session() as session:
                    due = [s.id for s in session.query(db.PriceSource).filter_by(enabled=True) if price_service.due(s)]
                for source_id in due:
                    if stop_background.is_set():
                        return
                    load_price(source_id)
            except Exception as exc:  # фоновая загрузка не должна ронять сервис
                print(f"[pricer] загрузка прайсов: {type(exc).__name__}: {str(exc)[:200]}")
    if price_seconds > 0:
        threading.Thread(target=price_watch, daemon=True, name="price-watch").start()
