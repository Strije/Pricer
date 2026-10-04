"""Позиции у поставщиков: статусы из кабинетов (кнопка и фоновая проверка), уведомления, статистика."""
import datetime
import os
import threading

from fastapi import Depends, HTTPException

from app import db
from app import supplier_lines as lines_service
from app.routes.common import Job
from app.schemas import LineStatusRequest


def setup(app, ctx):
    Session, build_engine, current_user = ctx.Session, ctx.build_engine, ctx.current_user
    engine_for, replay_mode, staff_user = ctx.engine_for, ctx.replay_mode, ctx.staff_user
    stop_background = ctx.stop_background
    replay_reset, start_job = ctx.replay_reset, ctx.start_job

    # ----- заказы поставщикам -----

    def sync_all_lines(organization_id):
        # Заказы из десктопа сведены в позиции при импорте и только за последние 60 дней
        # (app/desktop_import.py): здесь их не трогаем — иначе завелись бы и старые позиции без статусов.
        from sqlalchemy import or_

        with Session() as session:
            rows = session.query(db.Order.data).filter(
                db.Order.organization_id == organization_id,
                or_(db.Order.source.is_(None), db.Order.source != "desktop")).order_by(db.Order.id.desc()).all()
            for (data,) in rows:
                lines_service.sync_order(session, organization_id, dict(data or {}))
            session.commit()

    @app.get("/api/supplier-lines")
    def supplier_lines(provider: str = "", brand: str = "", status: str = "", date_from: str = "", date_to: str = "",
                       q: str = "", user=Depends(current_user)):
        sync_all_lines(user["organization_id"])

        def parse(value):
            try:
                return datetime.datetime.fromisoformat(value) if value else None
            except ValueError:
                raise HTTPException(status_code=400, detail="дата в формате ГГГГ-ММ-ДД")

        with Session() as session:
            rows = lines_service.query(session, user["organization_id"], provider=provider, brand=brand, status=status,
                                       date_from=parse(date_from), date_to=parse(date_to), q=q)
            everything = session.query(db.SupplierLine).filter_by(organization_id=user["organization_id"])
            facets = {
                "providers": sorted({r.provider for r in everything if r.provider}),
                "brands": sorted({r.brand for r in everything if r.brand}),
                "statuses": [{"code": k, "label": v[0]} for k, v in lines_service.STATUSES.items()],
            }
            return {"rows": [lines_service.line_view(r) for r in rows], "facets": facets}

    def _line(session, user, line_id):
        line = session.get(db.SupplierLine, line_id)
        if line is None or line.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="позиция не найдена")
        return line

    @app.get("/api/supplier-lines/{line_id}")
    def supplier_line(line_id: int, user=Depends(current_user)):
        with Session() as session:
            line = _line(session, user, line_id)
            labels = {k: v[0] for k, v in lines_service.STATUSES.items()}
            return {**lines_service.line_view(line), "events": [
                {"at": e.at.isoformat(timespec="seconds"), "status": e.status, "label": labels.get(e.status, e.status),
                 "text": e.text, "source": e.source} for e in line.events]}

    @app.post("/api/supplier-lines/{line_id}/status")
    def set_line_status(line_id: int, request: LineStatusRequest, user=Depends(current_user)):
        with Session() as session:
            line = _line(session, user, line_id)
            lines_service.add_event(session, line, request.status, request.text, source="manual", user_id=user["id"])
            session.commit()
            return lines_service.line_view(line)

    def refresh_statuses(engine, organization_id):
        """Статусы открытых позиций у всех поставщиков организации; отчёт по каждому."""
        from engine import PROVIDER_DISPLAY_NAMES

        replay_reset(engine)
        report = []
        for provider in engine.providers:
            cls = type(provider).__name__
            fetch = lines_service.fetcher_for(provider)
            if not fetch:
                continue
            name = PROVIDER_DISPLAY_NAMES.get(cls, getattr(provider, "DISPLAY_NAME", cls))
            with Session() as session:
                try:
                    changed = fetch(session, organization_id, provider, name)
                    session.commit()
                    report.append({"provider": name, "changed": changed})
                except Exception as exc:
                    session.rollback()
                    report.append({"provider": name, "error": engine.redactor.text(str(exc))[:200]})
        return report

    @app.post("/api/supplier-lines/refresh")
    def refresh_lines(user=Depends(current_user)):
        job = Job(None, user["organization_id"], kind="refresh")
        sync_all_lines(user["organization_id"])

        def work(engine):
            job.push("done", {"providers": refresh_statuses(engine, job.organization_id)})

        start_job(job, work)
        return {"job_id": job.id}

    @app.get("/api/notifications/other")
    def other_notifications(after: int = -1, user=Depends(staff_user)):
        """Прочие уведомления организации (клиент выбрал варианты в подборе): after — последний показанный id;
        при первом входе — только номер последнего, без старых сообщений."""
        with Session() as session:
            rows = session.query(db.Notification).filter(db.Notification.organization_id == user["organization_id"])
            last = rows.order_by(db.Notification.id.desc()).first()
            items = [] if after < 0 else rows.filter(db.Notification.id > after).order_by(db.Notification.id).limit(20).all()
            return {"last_id": last.id if last else 0, "items": [
                {"id": n.id, "kind": n.kind, "at": n.at.isoformat(timespec="seconds"), **(n.payload or {})} for n in items]}

    @app.get("/api/notifications")
    def notifications(after: int = -1, user=Depends(current_user)):
        """Отказы и возвраты, о которых сообщил поставщик: для всплывающих уведомлений.

        after — последний показанный id (браузер помнит его); при первом входе — отказы за 3 дня.
        """
        with Session() as session:
            events = (session.query(db.SupplierLineEvent, db.SupplierLine)
                      .join(db.SupplierLine, db.SupplierLineEvent.line_id == db.SupplierLine.id)
                      .filter(db.SupplierLine.organization_id == user["organization_id"],
                              db.SupplierLineEvent.source == "supplier",
                              db.SupplierLineEvent.status.in_(("refused", "returned"))))
            if after >= 0:
                events = events.filter(db.SupplierLineEvent.id > after)
            else:
                events = events.filter(db.SupplierLineEvent.at >= db.utcnow() - datetime.timedelta(days=3))
            events = events.order_by(db.SupplierLineEvent.id.desc()).limit(50).all()
            last = session.query(db.SupplierLineEvent.id).join(db.SupplierLine).filter(
                db.SupplierLine.organization_id == user["organization_id"]).order_by(db.SupplierLineEvent.id.desc()).first()
            labels = {k: v[0] for k, v in lines_service.STATUSES.items()}
            return {"last_id": max(after, last[0] if last else 0), "items": [{
                "id": e.id, "at": e.at.isoformat(timespec="seconds"), "status": e.status,
                "label": labels.get(e.status, e.status), "text": e.text, "line_id": line.id,
                "order_id": line.order_id, "provider": line.provider, "brand": line.brand, "article": line.article,
                "name": line.name, "quantity": line.quantity, "client": line.client_name,
            } for e, line in reversed(events)]}

    # Фоновая проверка статусов: отказ виден, даже если никто не нажимал «Обновить статусы».
    refresh_minutes = float(os.environ.get("PRICER_STATUS_REFRESH_MINUTES", "0" if replay_mode else "30") or 0)

    def background_refresh():
        while not stop_background.wait(refresh_minutes * 60):
            app.state.background_rounds += 1
            with Session() as session:
                org_ids = [row[0] for row in session.query(db.SupplierLine.organization_id)
                           .filter(db.SupplierLine.closed.is_(False)).distinct()]
            for org_id in org_ids:
                if stop_background.is_set():
                    return
                try:
                    sync_all_lines(org_id)
                    report = refresh_statuses(build_engine(org_id), org_id)
                    changed = sum(r.get("changed", 0) for r in report)
                    if changed:
                        print(f"[pricer] статусы организации {org_id}: изменений {changed}")
                except Exception as exc:  # фоновая проверка не должна ронять сервис
                    print(f"[pricer] фоновая проверка статусов {org_id}: {type(exc).__name__}: {str(exc)[:200]}")

    if refresh_minutes > 0:
        threading.Thread(target=background_refresh, daemon=True, name="status-refresh").start()

    @app.get("/api/supplier-stats")
    def supplier_stats(days: int = 180, user=Depends(current_user)):
        sync_all_lines(user["organization_id"])
        with Session() as session:
            return lines_service.stats(session, user["organization_id"], days=max(1, min(days, 3650)))
