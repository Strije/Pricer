"""Заказ из файла -> черновик -> перепроверка -> отправка поставщикам (режим демонстрации).

Поставщиков в отправке подменяем на поддельные корзины: в записи нет ответов на оформление,
а настоящие заказы из тестов уходить не должны.
"""
import io

import pytest

from test_server import H, read_events, register  # noqa: F401  (фикстура app_factory ниже)
from test_server import app_factory  # noqa: F401

CSV = "Бренд;Артикул;Наименование;Количество\nZIC;162622;Масло;1\nMANN;W712/95;Фильтр;1\nAIRLINE;AHR12D01;Сигнал;1\n"


def job_events(client, job_id):
    return read_events_any(client, f"/api/jobs/{job_id}/events")


def read_events_any(client, url):
    import json
    events, kind = [], None
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                events.append((kind, json.loads(line[6:])))
    return events


@pytest.fixture(scope="module")
def flow(app_factory):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "orders@example.com", org="Заказы")
    parsed = client.post("/api/order-file/parse", headers=H,
                         files={"file": ("order.csv", io.BytesIO(CSV.encode("utf-8")), "text/csv")})
    assert parsed.status_code == 200, parsed.text
    rows = parsed.json()["rows"]
    job = client.post("/api/order-file/search", headers=H, json={"rows": rows}).json()["job_id"]
    events = job_events(client, job)
    return {"client": client, "rows": rows, "job": job, "events": events, "app_factory": app_factory}


def test_parse_file(flow):
    assert [r["article"] for r in flow["rows"]] == ["162622", "W712/95", "AHR12D01"]


def test_file_search_matches_desktop(flow):
    rows = [data for kind, data in flow["events"] if kind == "row"]
    assert flow["events"][-1] == ("done", {"ready": 3, "total": 3})
    picked = [(r["offer"]["provider"], r["offer"]["price"], r["offer"]["sale_price"]) for r in rows]
    assert picked == [("Avtoto", 1954.0, 2700), ("Forum-Auto", 481.2, 730), ("Автоформула", 663.0, 990)]
    assert all(len(r["alternatives"]) <= 30 for r in rows)


@pytest.fixture(scope="module")
def order(flow):
    client = flow["client"]
    created = client.post("/api/orders", headers=H, json={"job_id": flow["job"], "rows": [0, 1, 2], "client": "Иванов"})
    assert created.status_code == 200, created.text
    return created.json()["order"]


def test_order_created_with_desktop_totals(order, flow):
    assert order["order_id"].startswith("ORD-") and order["order_id"].endswith("-0001")
    assert order["status"] == "draft" and order["grouping_mode"] == "file"
    assert order["totals"]["purchase_total"] == pytest.approx(1954 + 481.2 + 663)
    assert order["totals"]["sale_total"] == pytest.approx(2700 + 730 + 990)
    assert all(item["verification_status"] == "valid" for item in order["items"])  # проверено при подборе
    assert "snapshot" not in order["items"][0]  # сырые ответы поставщиков в браузер не уходят
    listed = flow["client"].get("/api/orders").json()
    assert listed[0]["order_id"] == order["order_id"] and listed[0]["items_count"] == 3


def test_selection_of_other_variant(flow):
    client = flow["client"]
    second = [d for k, d in flow["events"] if k == "row"][1]
    other = next(a for a in second["alternatives"] if a["internal_offer_id"] != second["offer"]["internal_offer_id"])
    created = client.post("/api/orders", headers=H, json={
        "job_id": flow["job"], "rows": [1], "client": "Петров",
        "selections": {"1": other["internal_offer_id"]}}).json()["order"]
    assert created["items"][0]["internal_offer_id"] == other["internal_offer_id"]
    assert created["order_id"].endswith("-0002")


def test_recheck_keeps_items_valid(order, flow):
    client = flow["client"]
    job = client.post(f"/api/orders/{order['order_id']}/recheck", headers=H, json={}).json()["job_id"]
    kind, data = job_events(client, job)[-1]
    assert kind == "done", data
    rechecked = data["order"]
    assert rechecked["verification_status"] == "valid"
    assert [i["provider"] for i in rechecked["items"]] == ["Avtoto", "Forum-Auto", "Автоформула"]


def test_submit_with_fake_baskets(order, flow):
    client, factory = flow["client"], flow["app_factory"]
    app_state = client.app.state.pricer
    me = client.get("/api/me").json()
    engine = app_state["engine_for"](me["organization_id"])
    calls = []

    def basket(result):
        def add_to_basket(item, quantity=1, comment=""):
            calls.append((item.get("provider") or item.get("provider_name"), item.get("article"), quantity))
            return result
        return add_to_basket

    outcomes = {
        "Avtoto": {"success": True, "data": "принято"},
        "Forum-Auto": {"success": False, "uncertain": True,
                       "error": "ошибка сети: https://api.forum-auto.ru/v2/addGoodsToOrder?login=REPLAYSECRET&pass=REPLAYSECRETPW"},
        "Автоформула": {"success": False, "error": "нет в наличии"},
    }
    from engine import PROVIDER_DISPLAY_NAMES
    for provider in engine.providers:
        name = PROVIDER_DISPLAY_NAMES.get(type(provider).__name__)
        if name in outcomes:
            provider.add_to_basket = basket(outcomes[name])
            provider.add_to_basket_batch = None

    order_id = order["order_id"]
    preview = client.get(f"/api/orders/{order_id}/submit-preview").json()
    assert preview["items"] == 3 and set(preview["providers"]) == set(outcomes)
    assert client.post(f"/api/orders/{order_id}/submit", headers=H, json={}).status_code == 400  # без подтверждения
    job = client.post(f"/api/orders/{order_id}/submit", headers=H, json={"confirm": True}).json()["job_id"]
    kind, data = job_events(client, job)[-1]
    assert kind == "done", data
    statuses = {i["provider"]: i["submit_status"] for i in data["order"]["items"]}
    assert statuses == {"Avtoto": "submitted", "Forum-Auto": "unknown", "Автоформула": "failed"}
    assert len(calls) == 3  # каждая позиция отправлена ровно один раз

    import json as _json
    stored = client.get(f"/api/orders/{order_id}").json()
    assert "REPLAYSECRET" not in _json.dumps(stored, ensure_ascii=False)  # пароль из ответа поставщика скрыт
    assert "addGoodsToOrder?login=***&pass=***" in _json.dumps(stored, ensure_ascii=False)
    log = client.get(f"/api/orders/{order_id}/log").json()
    assert "REPLAYSECRET" not in _json.dumps(log, ensure_ascii=False)
    assert sorted(row["success"] for row in log) == [False, False, True]

    # Повтор (как в десктопе): принятая (submitted) и неподтверждённая (unknown) позиции больше не
    # уходят — иначе дубль у поставщика. Отказ поставщика (failed) отправить снова можно.
    again = client.post(f"/api/orders/{order_id}/submit", headers=H, json={"confirm": True}).json()["job_id"]
    kind, data = job_events(client, again)[-1]
    assert kind == "done", data
    resent = calls[3:]
    assert [c[0] for c in resent] == ["Автоформула"]
    assert sum(1 for c in calls if c[0] == "Avtoto") == 1 and sum(1 for c in calls if c[0] == "Forum-Auto") == 1


def test_db_lock_blocks_parallel_submit(order, flow):
    from app import orders as order_service

    app_state = flow["client"].app.state.pricer
    Session = app_state["Session"]
    me = flow["client"].get("/api/me").json()
    assert order_service._lock(Session, me["organization_id"], order["order_id"], "k", [0]) is True
    assert order_service._lock(Session, me["organization_id"], order["order_id"], "k", [0]) is False
    order_service._unlock(Session, me["organization_id"], order["order_id"])


def test_recovery_marks_interrupted_items_unknown(flow):
    from app import db
    from app import orders as order_service

    client = flow["client"]
    created = client.post("/api/orders", headers=H, json={"job_id": flow["job"], "rows": [0, 2], "client": "Сбой"}).json()["order"]
    Session = client.app.state.pricer["Session"]
    me = client.get("/api/me").json()
    assert order_service._lock(Session, me["organization_id"], created["order_id"], "k", [1])
    recovered = order_service.recover_interrupted_submits(Session)
    assert (me["organization_id"], created["order_id"]) in recovered
    after = client.get(f"/api/orders/{created['order_id']}").json()
    assert after["items"][0]["submit_status"] == "not_submitted"  # не уходила — не трогаем
    assert after["items"][1]["submit_status"] == "unknown"
    with Session() as session:
        row = session.query(db.Order).filter_by(order_id=created["order_id"], organization_id=me["organization_id"]).one()
        assert row.submitting is False


def test_skip_and_restore_item(flow):
    client = flow["client"]
    created = client.post("/api/orders", headers=H, json={"job_id": flow["job"], "rows": [0, 1], "client": "Пропуск"}).json()["order"]
    order_id = created["order_id"]
    skipped = client.post(f"/api/orders/{order_id}/items/1/skip", headers=H).json()["order"]
    assert skipped["items"][1]["submit_status"] == "skipped"
    restored = client.post(f"/api/orders/{order_id}/items/1/restore", headers=H).json()["order"]
    assert restored["items"][1]["submit_status"] != "skipped"


def test_orders_are_isolated(flow, order):
    make_client, _ = flow["app_factory"]
    other = register(make_client(), "other-orders@example.com", org="Чужая")
    assert other.get("/api/orders").json() == []
    assert other.get(f"/api/orders/{order['order_id']}").status_code == 404
    assert other.post("/api/orders", headers=H, json={"job_id": flow["job"], "rows": [0], "client": "X"}).status_code == 404


def test_replace_variant_fixed_desktop_bug(flow):
    """В десктопе 1.0.3 замена варианта падала с NameError (offer). В переносе исправлено."""
    client = flow["client"]
    created = client.post("/api/orders", headers=H, json={"job_id": flow["job"], "rows": [1], "client": "Замена"}).json()["order"]
    me = client.get("/api/me").json()
    engine = client.app.state.pricer["engine_for"](me["organization_id"])
    job = client.app.state.pricer["state"]["jobs"][flow["job"]]
    current = created["items"][0]["internal_offer_id"]
    variant = next(o for o in job.results[1]["alternatives"] if o["internal_offer_id"] != current)
    with engine.lock:
        engine._replace_order_item_with_variant(created["order_id"], 0, dict(variant))
    after = client.get(f"/api/orders/{created['order_id']}").json()["items"][0]
    assert after["internal_offer_id"] == variant["internal_offer_id"]
    assert after["name"] == (variant.get("name") or created["items"][0]["name"])


def test_xlsx_upload_in_demo_mode(flow):
    """Регресс: в демо-режиме подмена времени ломала openpyxl, загрузка XLSX давала 500."""
    import datetime

    from openpyxl import Workbook

    assert datetime.datetime.now().year >= 2026 and datetime.datetime.__name__ == "datetime"
    book = Workbook()
    book.active.append(["Бренд", "Артикул", "Наименование", "Количество"])
    book.active.append(["ZIC", "162622", "Масло", 2])
    data = io.BytesIO()
    book.save(data)
    response = flow["client"].post("/api/order-file/parse", headers=H,
                                   files={"file": ("order.xlsx", io.BytesIO(data.getvalue()), "application/octet-stream")})
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [{"brand": "ZIC", "article": "162622", "name": "Масло", "quantity": 2}]


def test_broken_xlsx_gives_400_not_500(flow):
    response = flow["client"].post("/api/order-file/parse", headers=H,
                                   files={"file": ("order.xlsx", io.BytesIO(b"not a zip"), "application/octet-stream")})
    assert response.status_code == 400 and "не читается" in response.json()["detail"]


def test_1c_html_xls_export(flow):
    html = ("<html><meta charset='utf-8'><table><tr><th>Бренд</th><th>Артикул</th><th>Наименование</th><th>Количество</th></tr>"
            "<tr><td>ZIC</td><td>162622</td><td>Масло&nbsp;ZIC</td><td>3</td></tr></table></html>").encode("utf-8")
    response = flow["client"].post("/api/order-file/parse", headers=H,
                                   files={"file": ("Склад.xls", io.BytesIO(html), "application/vnd.ms-excel")})
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [{"brand": "ZIC", "article": "162622", "name": "Масло ZIC", "quantity": 3}]
    binary = flow["client"].post("/api/order-file/parse", headers=H,
                                 files={"file": ("old.xls", io.BytesIO(b"\xd0\xcf\x11\xe0binary"), "application/vnd.ms-excel")})
    assert binary.status_code == 400 and "XLSX" in binary.json()["detail"]
