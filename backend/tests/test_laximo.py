"""Подбор по VIN/госномеру (Laximo) и статусы ABCP — по образцу приложения Strije/Abcp."""
import pytest
import requests

import laximo
from laximo import LaximoClient, LaximoError, normalize_ru_plate, oem_brand_for, parse_quick_details, parse_vehicles


def test_ru_plate_normalization():
    assert normalize_ru_plate("н207вн 154") == "Н207ВН154"
    assert normalize_ru_plate("H207BH154") == "Н207ВН154"  # латиница-двойник
    assert normalize_ru_plate("XTA21099012345678") is None


def test_oem_brand():
    assert oem_brand_for("Volkswagen") == "VAG" and oem_brand_for("SKODA") == "VAG"
    assert oem_brand_for("Lexus") == "TOYOTA" and oem_brand_for("Opel") == "GENERAL MOTORS"
    assert oem_brand_for("Kia") == "Kia"


VEHICLE = {"rows": [{"catalog": "VW1", "vehicleId": "7", "ssd": "$SSD$", "brand": "VOLKSWAGEN", "name": "Golf",
                     "attributes": [{"key": "date", "name": "Дата выпуска", "value": "03.2012"}, {"name": "без ключа"}]}]}


def test_parse_vehicle_and_year():
    vehicles = parse_vehicles(VEHICLE)
    assert vehicles[0]["catalog"] == "VW1" and vehicles[0]["brand"] == "VOLKSWAGEN"
    assert len(vehicles[0]["attributes"]) == 1 and laximo.vehicle_year(vehicles[0]) == 2012
    assert parse_vehicles({"rows": [{"catalog": "X"}]}) == []  # без ssd машину не берём


def test_parse_quick_details():
    data = {"categories": [{"name": "Фильтры", "units": [{"unitId": "11", "name": "Масляный фильтр",
                                                          "details": [{"name": "Фильтр", "oem": " 03C115561H "}]}],
                            "details": [{"name": "Прокладка", "oem": "N0138157"}]}]}
    parsed = parse_quick_details(data, "$SSD$")
    assert parsed[0]["units"][0]["details"][0]["oem"] == "03C115561H"
    assert parsed[0]["details"][0]["oem"] == "N0138157" and parsed[0]["units"][0]["ssd"] == "$SSD$"


class FakeSession:
    def __init__(self, status=200, body="{}"):
        self.status, self.body, self.trust_env, self.calls = status, body, True, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs["params"]))
        response = requests.Response()
        response.status_code = self.status
        response._content = self.body.encode()
        return response


def test_client_routes_plate_and_vin_and_errors():
    import json
    session = FakeSession(body=json.dumps(VEHICLE))
    client = LaximoClient("u", "p", session=session)
    plate, vehicles = client.find_vehicle("н 207 вн154")
    assert plate == "Н207ВН154" and session.calls[-1][0].endswith("findVehicleByPlateNumber")
    client.find_vehicle("wvwzzz1kzcw000001")
    assert session.calls[-1][1] == {"identString": "WVWZZZ1KZCW000001"}
    with pytest.raises(LaximoError, match="запрещён"):
        LaximoClient("u", "p", session=FakeSession(403, "E_ACCESSDENIED: bad login")).find_vehicle("WVW")


def test_abcp_status_refresh_from_orders():
    """Ответ orders в формате из OrderStatusTest приложения Abcp."""
    from app import db
    from app.supplier_lines import refresh_abcp
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session() as session:
        org = db.Organization(name="o", settings={})
        session.add(org)
        session.flush()
        line = db.SupplierLine(organization_id=org.id, order_id="ORD-1", item_index=0, provider="Элит Ойл",
                               brand="VAG", article="04E115561H", supplier_ref="1001", status="submitted")
        session.add(line)
        session.commit()

        class Provider:
            def get_orders(self, limit=100):
                return {"1001": {"number": "1001", "positions": {
                    "0": {"brand": "VAG", "number": "04E115561H", "status": "Готово к выдаче"},
                    "1": {"brand": "NGK", "number": "LZKR6B10E", "status": "В пути"}}}}

        assert refresh_abcp(session, org.id, Provider(), "Элит Ойл") == 1
        assert line.status == "arrived" and line.status_text == "Готово к выдаче"
        assert refresh_abcp(session, org.id, Provider(), "Элит Ойл") == 0  # без изменений — без событий


def test_vin_api(app_factory):  # noqa: F811
    from test_server import H, register

    make_client, _ = app_factory
    client = register(make_client(), "vin@example.com", org="VIN")
    assert client.post("/api/vin/find", headers=H, json={"query": "WVWZZZ1KZCW000001"}).status_code == 400  # нет Laximo
    client.post("/api/suppliers", headers=H, json={"section": "laximo", "secrets": {"login": "L", "password": "P"}})
    calls = []
    orig = LaximoClient.find_vehicle
    LaximoClient.find_vehicle = lambda self, q: (calls.append(q), (None, parse_vehicles(VEHICLE)))[1]
    try:
        found = client.post("/api/vin/find", headers=H, json={"query": "WVWZZZ1KZCW000001"}).json()
        again = client.post("/api/vin/find", headers=H, json={"query": "wvwzzz1kzcw000001"}).json()
    finally:
        LaximoClient.find_vehicle = orig
    assert found["vehicles"][0]["oem_brand"] == "VAG" and found["vehicles"][0]["year"] == 2012
    assert again == found and len(calls) == 1  # повтор — из кэша, лимит Laximo бережём
    assert client.post("/api/vin/details", headers=H, json={"vehicle": {"catalog": "VW1", "vehicleId": "7", "ssd": "s"}}).status_code == 400


from test_server import app_factory  # noqa: E402,F401


def test_abcp_match_by_position_id_and_supplier_code():
    """Как _position_key десктопа: один бренд и номер с двух складов в одном заказе — разные статусы."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db
    from app.supplier_lines import refresh_abcp

    engine = create_engine("sqlite://")
    db.Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        org = db.Organization(name="o", settings={})
        session.add(org)
        session.flush()
        common = dict(organization_id=org.id, order_id="ORD-1", provider="Элит Ойл", brand="ZIC",
                      article="162622", supplier_ref="2002", status="submitted")
        by_code = db.SupplierLine(item_index=0, supplier_code="S1", **common)
        by_id = db.SupplierLine(item_index=1, supplier_code="S2", position_id="P-9", **common)
        session.add_all([by_code, by_id])
        session.commit()

        class Provider:
            def get_orders(self, limit=100):
                return [{"number": "2002", "positions": [
                    {"brand": "ZIC", "number": "162622", "supplierCode": "S2", "positionId": "P-9", "status": "Отказ поставщика"},
                    {"brand": "ZIC", "number": "162622", "supplierCode": "S1", "positionId": "P-8", "status": "В пути"}]}]

        assert refresh_abcp(session, org.id, Provider(), "Элит Ойл") == 2
        assert by_code.status == "in_transit" and by_id.status == "refused"


def test_migration_adds_columns_to_old_database(tmp_path):
    """База прошлой версии (журнал без position_id/supplier_code) открывается и дополняется."""
    import sqlite3

    from app import db

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE supplier_lines (id INTEGER PRIMARY KEY, organization_id INTEGER, order_id VARCHAR(40), "
                    "item_index INTEGER, provider VARCHAR(100), brand VARCHAR(100), article VARCHAR(100), name VARCHAR(300), "
                    "warehouse VARCHAR(120), quantity INTEGER, purchase_price FLOAT, client_name VARCHAR(200), "
                    "supplier_ref VARCHAR(100), promised_hours INTEGER, submitted_at DATETIME, status VARCHAR(20), "
                    "status_text VARCHAR(500), status_at DATETIME, arrived_at DATETIME, closed BOOLEAN)")
        con.execute("INSERT INTO supplier_lines (id, organization_id, order_id, item_index, provider, status, closed) "
                    "VALUES (1, 1, 'ORD-OLD', 0, 'Avtoto', 'submitted', 0)")
    Session = db.make_sessionmaker(f"sqlite:///{path}")
    with Session() as session:
        line = session.get(db.SupplierLine, 1)
        assert line.order_id == "ORD-OLD" and line.position_id == "" and line.supplier_code == ""
