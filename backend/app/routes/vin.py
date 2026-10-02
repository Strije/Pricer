"""Подбор по VIN, FRAME и госномеру (Laximo)."""
import time

from fastapi import Depends, HTTPException

from app import db
from app.schemas import VehicleRef, VinDetailsRequest, VinFindRequest


def setup(app, ctx):
    Session, box, current_user = ctx.Session, ctx.box, ctx.current_user
    state = ctx.state

    # ----- подбор по VIN / госномеру (Laximo, перенесено из приложения Strije/Abcp) -----

    def laximo_client(user):
        from laximo import LaximoClient

        with Session() as session:
            account = session.query(db.SupplierAccount).filter_by(
                organization_id=user["organization_id"], section="laximo").first()
            secrets = box.open(account.secrets_sealed) if account else {}
        if not (account and (account.config or {}).get("enabled", True) and secrets.get("login") and secrets.get("password")):
            raise HTTPException(status_code=400, detail="подключите каталог Laximo на вкладке «Поставщики»")
        return LaximoClient(secrets["login"], secrets["password"])

    def laximo_call(fn):
        from laximo import LaximoError

        try:
            return fn()
        except LaximoError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/vin/find")
    def vin_find(request: VinFindRequest, user=Depends(current_user)):
        from laximo import oem_brand_for, vehicle_year

        client = laximo_client(user)
        cache = state.setdefault("vin_cache", {})
        key = (user["organization_id"], request.query.strip().upper())
        cached = cache.get(key)
        if cached and time.time() - cached[0] < 3600:  # Laximo ограничивает число запросов
            plate, vehicles = cached[1]
        else:
            plate, vehicles = laximo_call(lambda: client.find_vehicle(request.query))
            cache[key] = (time.time(), (plate, vehicles))
        return {"plate": plate, "vehicles": [{**v, "year": vehicle_year(v), "oem_brand": oem_brand_for(v["brand"])}
                                             for v in vehicles]}

    @app.post("/api/vin/groups")
    def vin_groups(vehicle: VehicleRef, user=Depends(current_user)):
        client = laximo_client(user)
        return laximo_call(lambda: client.quick_groups(vehicle.model_dump())) or {}

    @app.post("/api/vin/details")
    def vin_details(request: VinDetailsRequest, user=Depends(current_user)):
        if request.quickGroupId is None and not request.query:
            raise HTTPException(status_code=400, detail="выберите группу или введите название детали")
        client = laximo_client(user)
        return laximo_call(lambda: client.quick_details(request.vehicle.model_dump(), request.quickGroupId, request.query))

    ctx.laximo_client = laximo_client
