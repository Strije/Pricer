"""Клиенты организации и их машины."""
import re

from fastapi import Depends, HTTPException

from app import clients as client_service
from app import db
from app.schemas import ClientRequest, VehicleRequest


def setup(app, ctx):
    Session, current_user, engine_for = ctx.Session, ctx.current_user, ctx.engine_for

    # ----- клиенты -----

    def resolve_client(user, request):
        """Клиент и машина для заказа: выбранные, найденные или созданные заново."""
        if re.search(r"[А-Яа-яЁё]", request.vin or ""):
            raise HTTPException(status_code=400, detail="VIN набран кириллицей — переключите раскладку на латиницу")
        warning = client_service.vin_warning(client_service.normalize_vin(request.vin))
        if warning:
            raise HTTPException(status_code=400, detail=warning)
        with Session() as session:
            try:
                client, vehicle = client_service.resolve(
                    session, user["organization_id"], client_id=request.client_id, vehicle_id=request.vehicle_id,
                    name=request.client, phone=request.phone, vin=request.vin)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            fields = client_service.order_client_fields(client, vehicle)
            session.commit()
        return fields

    def link_client(store, order, fields):
        order = dict(order)
        order["client"] = {**(order.get("client") or {}), **fields}
        return store.update(order)

    @app.get("/api/clients")
    def list_clients(q: str = "", user=Depends(current_user)):
        with Session() as session:
            return [client_service.client_view(c) for c in client_service.search(session, user["organization_id"], q)]

    @app.post("/api/clients")
    def create_client(request: ClientRequest, user=Depends(current_user)):
        with Session() as session:
            client = db.Client(organization_id=user["organization_id"], name=request.name.strip(),
                               phone=client_service.normalize_phone(request.phone), email=request.email.strip(),
                               comment=request.comment)
            session.add(client)
            session.commit()
            return client_service.client_view(client)

    def _client(session, user, client_id):
        client = session.get(db.Client, client_id)
        if client is None or client.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="клиент не найден")
        return client

    @app.get("/api/clients/{client_id}")
    def get_client(client_id: int, user=Depends(current_user)):
        with Session() as session:
            view = client_service.client_view(_client(session, user, client_id))
        history = []
        for order in engine_for(user["organization_id"]).order_store.list_orders():
            client = order.get("client") or {}
            if client.get("id") != client_id:
                continue
            history.append({
                "order_id": order.get("order_id"), "created_at": order.get("created_at"), "status": order.get("status"),
                "vehicle_id": client.get("vehicle_id"), "vin": client.get("vin"), "totals": order.get("totals"),
                "items": [{k: i.get(k) for k in ("brand", "article", "name", "quantity", "sale_price", "submit_status")}
                          for i in order.get("items") or []],
            })
        return {**view, "orders": history}

    @app.put("/api/clients/{client_id}")
    def update_client(client_id: int, request: ClientRequest, user=Depends(current_user)):
        with Session() as session:
            client = _client(session, user, client_id)
            client.name, client.email, client.comment = request.name.strip(), request.email.strip(), request.comment
            client.phone = client_service.normalize_phone(request.phone)
            session.commit()
            return client_service.client_view(client)

    @app.post("/api/clients/{client_id}/vehicles")
    def add_vehicle(client_id: int, request: VehicleRequest, user=Depends(current_user)):
        vin = client_service.normalize_vin(request.vin)
        if re.search(r"[А-Яа-яЁё]", request.vin) or client_service.vin_warning(vin):
            raise HTTPException(status_code=400, detail="VIN: латиница и цифры, без букв I, O, Q")
        with Session() as session:
            client = _client(session, user, client_id)
            if vin and any(v.vin == vin for v in client.vehicles):
                raise HTTPException(status_code=409, detail="эта машина уже есть у клиента")
            session.add(db.Vehicle(organization_id=user["organization_id"], client=client, vin=vin,
                                   plate=request.plate.strip().upper(), make=request.make.strip(),
                                   model=request.model.strip(), year=request.year, comment=request.comment))
            session.commit()
            return client_service.client_view(client)

    def _vehicle(session, user, vehicle_id):
        vehicle = session.get(db.Vehicle, vehicle_id)
        if vehicle is None or vehicle.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="машина не найдена")
        return vehicle

    @app.put("/api/vehicles/{vehicle_id}")
    def update_vehicle(vehicle_id: int, request: VehicleRequest, user=Depends(current_user)):
        with Session() as session:
            vehicle = _vehicle(session, user, vehicle_id)
            vin = client_service.normalize_vin(request.vin)
            if re.search(r"[А-Яа-яЁё]", request.vin) or client_service.vin_warning(vin):
                raise HTTPException(status_code=400, detail="VIN: латиница и цифры, без букв I, O, Q")
            vehicle.vin, vehicle.plate = vin, request.plate.strip().upper()
            vehicle.make, vehicle.model, vehicle.year, vehicle.comment = (
                request.make.strip(), request.model.strip(), request.year, request.comment)
            session.commit()
            return client_service.client_view(vehicle.client)

    @app.delete("/api/vehicles/{vehicle_id}")
    def delete_vehicle(vehicle_id: int, user=Depends(current_user)):
        with Session() as session:
            vehicle = _vehicle(session, user, vehicle_id)
            client = vehicle.client
            session.delete(vehicle)
            session.commit()
            session.refresh(client)
            return client_service.client_view(client)

    ctx.link_client = link_client
    ctx.resolve_client = resolve_client
