"""Перенос истории десктопа: заказы (config/orders) и журнал отправок (order_history.json)."""
import datetime
import io
import json
import zipfile

import pytest

from app import db, desktop_import
from app.redact import Redactor
from test_server import H, app_factory, register  # noqa: F401

NOW = datetime.datetime(2026, 10, 4, 12, 0, 0)


def _item(provider, article, submitted_at, status="submitted", response=None):
    return {"provider": provider, "brand": "MANN", "display_brand": "MANN", "article": article, "name": "Фильтр",
            "quantity": 2, "purchase_price": 300.0, "warehouse": "Ростов", "delivery_hours": 48,
            "submit_status": status, "submitted_at": submitted_at,
            "supplier_response": response or {"success": True, "data": {"Done": [1]}}}


def desktop_order(order_id, submitted_at, items=None):
    return {"order_id": order_id, "status": "sent", "created_at": submitted_at, "client": {"name": "Иванов"},
            "comment": "", "items": items or [_item("Avtoto", "W71295", submitted_at)]}


def journal_row(order_id, submitted_at, article="W71295"):
    return {"provider": "Avtoto", "brand": "MANN", "article": article, "quantity": 2, "submitted_at": submitted_at,
            "response": {"order_id": order_id, "success": True}}


def zipped(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data if isinstance(data, (str, bytes)) else json.dumps(data, ensure_ascii=False))
    return buffer.getvalue()


def test_read_files_sorts_orders_journal_and_the_rest():
    archive = zipped({
        "orders/ORD-20260930-0001.json": desktop_order("ORD-20260930-0001", "2026-09-30T10:00:00"),
        "order_history.json": [journal_row("ORD-20260930-0001", "2026-09-30T10:00:05")],
        "settings.json": {"armtek": {"login": "u", "password": "p"}},  # настройки — свой импорт, здесь пропуск
        "history.json": ["W71295", "OC90"],
        "readme.txt": "не JSON",
    })
    orders, journal, skipped = desktop_import.read_files([("orders.zip", archive)])
    assert [o["order_id"] for o in orders] == ["ORD-20260930-0001"] and len(journal) == 1
    assert sorted(skipped) == ["history.json", "settings.json"]  # .txt внутри архива даже не читается
    with pytest.raises(desktop_import.DesktopImportError):
        desktop_import.read_files([("broken.zip", b"not a zip")])


def test_read_files_limits_unpacked_size(monkeypatch):
    monkeypatch.setattr(desktop_import, "MAX_ENTRY", 1000)
    archive = zipped({"orders/ORD-20260930-0001.json": " " * 5000 + "{}"})
    with pytest.raises(desktop_import.DesktopImportError):
        desktop_import.read_files([("big.zip", archive)])


@pytest.fixture
def Session(tmp_path):
    return db.make_sessionmaker(f"sqlite:///{tmp_path / 'import.db'}")


def _org(Session):
    with Session() as session:
        org = db.Organization(name="Тест", settings={})
        session.add(org)
        session.commit()
        return org.id


def test_import_renames_taken_number_keeps_window_and_is_repeatable(Session):
    org_id = _org(Session)
    with Session() as session:  # заказ самого веба с тем же номером, что у десктопа
        session.add(db.Order(organization_id=org_id, order_id="ORD-20260930-0001", status="draft",
                             data={"order_id": "ORD-20260930-0001", "items": []}))
        session.commit()
    recent = desktop_order("ORD-20260930-0001", "2026-09-30T10:00:00", [
        _item("Avtoto", "W71295", "2026-09-30T10:00:00"),
        _item("Rossko", "OC90", "2026-09-30T10:00:00", status="failed",
              response={"success": False, "error": "ошибка https://api.forum-auto.ru/x?login=u1&pass=topsecret"}),
        _item("Rossko", "C30005", "", status="not_submitted"),
    ])
    old = desktop_order("ORD-20260710-0003", "2026-07-10T09:00:00")  # раньше окна статусов: только архив
    journal = [journal_row("ORD-20260930-0001", "2026-09-30T10:00:05"), journal_row("ORD-20260710-0003", "2026-07-10T09:00:01")]
    result = desktop_import.import_history(Session, org_id, [recent, old], journal, Redactor(), now=NOW)
    assert result == {"orders_new": 2, "orders_updated": 0, "orders_same": 0, "orders_renamed": 1,
                      "journal_new": 2, "journal_same": 0, "lines": 2}
    with Session() as session:
        ids = sorted(r.order_id for r in session.query(db.Order).filter_by(organization_id=org_id))
        assert ids == ["ORD-20260710-0003", "ORD-20260930-0001", "ORD-20260930-0001-D"]
        native = session.query(db.Order).filter_by(order_id="ORD-20260930-0001").one()
        assert native.data.get("source") is None  # заказ веба не тронут
        lines = session.query(db.SupplierLine).filter_by(organization_id=org_id).order_by(db.SupplierLine.item_index).all()
        assert [(l.order_id, l.item_index, l.status) for l in lines] == [
            ("ORD-20260930-0001-D", 0, "submitted"), ("ORD-20260930-0001-D", 1, "failed")]
        log = {r.order_id for r in session.query(db.SubmissionLog)}
        assert log == {"ORD-20260930-0001-D", "ORD-20260710-0003"}  # журнал идёт за переименованным заказом
        stored = json.dumps(session.query(db.Order).filter_by(order_id="ORD-20260930-0001-D").one().data, ensure_ascii=False)
        assert "topsecret" not in stored and "u1" not in stored
    again = desktop_import.import_history(Session, org_id, [recent, old], journal, Redactor(), now=NOW)
    assert again["orders_same"] == 2 and again["journal_same"] == 2
    assert again["orders_new"] == again["journal_new"] == again["lines"] == 0
    changed = {**old, "status": "processed"}
    assert desktop_import.import_history(Session, org_id, [changed], [], Redactor(), now=NOW)["orders_updated"] == 1


def test_import_endpoint(app_factory):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "history@example.com", org="История")
    now = datetime.datetime.now().replace(microsecond=0)
    recent = (now - datetime.timedelta(days=3)).isoformat()
    old = (now - datetime.timedelta(days=90)).isoformat()
    archive = zipped({"orders/ORD-20261001-0007.json": desktop_order("ORD-20261001-0007", recent),
                      "orders/ORD-20260701-0002.json": desktop_order("ORD-20260701-0002", old),
                      "order_history.json": [journal_row("ORD-20261001-0007", recent)]})
    upload = {"files": ("orders.zip", archive, "application/zip")}
    r = client.post("/api/import/desktop-history", headers=H, files=upload)
    assert r.status_code == 200, r.text
    assert (r.json()["orders_new"], r.json()["lines"], r.json()["journal_new"]) == (2, 1, 1)
    order = client.get("/api/orders/ORD-20261001-0007").json()
    assert order["order_id"] == "ORD-20261001-0007"
    lines = client.get("/api/supplier-lines").json()
    rows = lines["rows"]
    assert any(row["order_id"] == "ORD-20261001-0007" for row in rows)
    # список при открытии сводит заказы в позиции — старый заказ из десктопа он не трогает
    assert not any(row["order_id"] == "ORD-20260701-0002" for row in rows)
    assert client.get("/api/orders/ORD-20261001-0007/log").json()[0]["provider"] == "Avtoto"
    r = client.post("/api/import/desktop-history", headers=H,
                    files={"files": ("notes.json", json.dumps({"x": 1}), "application/json")})
    assert r.status_code == 400
