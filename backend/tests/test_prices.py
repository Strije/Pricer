"""Прайс-листы: источник по ссылке (локальный веб-сервер с файлами), предпросмотр и разметка колонок,
загрузка в базу с отчётом, поиск по прайсам как у поставщика, перенос из settings.json десктопа,
устаревание и то, что адрес с паролем не уходит в браузер."""
import datetime
import functools
import http.server
import io
import threading
import zipfile

import pytest

from test_server import H, app_factory, register  # noqa: F401

CSV = ("Артикул;Производитель;Наименование;Цена;Остаток\n"
       "OC90;KNECHT;Фильтр масляный;450,50;12\n"
       "W712/95;MANN;Фильтр масляный;380;>10\n"
       ";MANN;Без артикула;100;1\n"
       "X1;NONAME;Без цены;;5\n")
NO_HEADER = "162622,ZIC,Масло ZIC X5 10W-40 4л,1990,7\n"


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    root = tmp_path_factory.mktemp("prices")
    (root / "price.csv").write_bytes(CSV.encode("cp1251"))
    (root / "noheader.csv").write_text(NO_HEADER, encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("inner.csv", CSV)
    (root / "price.zip").write_bytes(buf.getvalue())
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://user:Secret123@127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def wait_load(client, source_id):
    job = client.post(f"/api/prices/{source_id}/load", headers=H).json()["job_id"]
    from test_server import read_events
    return next(d for k, d in read_events(client, job) if k == "done")


def org_of(client, email):
    from app import db
    with client.app.state.pricer["Session"]() as session:
        return session.query(db.User).filter_by(email=email).one().organization_id


def test_price_source_flow(app_factory, files):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "prices@example.com", org="Прайсы")
    source = client.post("/api/prices", headers=H, json={"name": "Прайс Тест", "location": f"{files}/price.csv",
                                                       "schedule_hours": 1, "settings": {"warehouse": "Склад Тест"}}).json()
    assert source["location_hint"].endswith("/…/price.csv") and "Secret123" not in str(source)
    assert source["schedule_hours"] == 1 and source["state"] == "new"

    preview = client.post(f"/api/prices/{source['id']}/preview", headers=H).json()
    assert preview["headers"][:4] == ["Артикул", "Производитель", "Наименование", "Цена"]
    assert preview["guess"]["article"] == "Артикул" and preview["guess"]["price"] == "Цена"
    assert preview["total"] == 4

    status = wait_load(client, source["id"])
    assert status["ok"] and status["rows"] == 2 and status["skipped"] == 2
    assert status["reasons"] == {"нет артикула": 1, "нет цены": 1}
    listed = client.get("/api/prices").json()["sources"][0]
    assert listed["state"] == "ok" and listed["status"]["rows"] == 2 and "Secret123" not in str(listed)

    engine = client.app.state.pricer["engine_for"](org_of(client, "prices@example.com"))
    provider = next(p for p in engine.providers if type(p).__name__ == "PriceDbProvider")
    offers = provider.get_prices("oc-90")
    assert len(offers) == 1 and offers[0]["price"] == 450.5 and offers[0]["warehouse"] == "Склад Тест"
    assert offers[0]["provider"] == "Прайс Тест" and "source_url" not in offers[0]
    assert provider.get_brands("W71295") == ["MANN"]
    assert provider.get_prices_many(["OC90", "nope"]) == {"OC90": offers}
    assert "Прайс-листы" in engine.provider_names


def test_manual_columns_zip_and_errors(app_factory, files):  # noqa: F811
    make_client, _ = app_factory
    client = register(make_client(), "prices2@example.com", org="Прайсы 2")
    src = client.post("/api/prices", headers=H, json={"name": "Без заголовков", "location": f"{files}/noheader.csv",
                      "settings": {"has_header": False, "column_map": {"article": "0", "brand": "1", "name": "2", "price": "3", "quantity": "4"}}}).json()
    assert wait_load(client, src["id"])["rows"] == 1
    z = client.post("/api/prices", headers=H, json={"name": "Архив", "location": f"{files}/price.zip"}).json()
    assert wait_load(client, z["id"])["rows"] == 2
    bad = client.post("/api/prices", headers=H, json={"name": "Нет файла", "location": f"{files}/missing.csv"}).json()
    status = wait_load(client, bad["id"])
    assert status["ok"] is False and "Secret123" not in status["message"]
    assert client.post("/api/prices", headers=H, json={"location": "file:///etc/passwd"}).status_code == 400
    assert client.put(f"/api/prices/{src['id']}", headers=H, json={"settings": {"evil": 1}}).status_code == 400
    other = register(make_client(), "prices-other@example.com", org="Чужие прайсы")
    assert other.get("/api/prices").json()["sources"] == [] and other.post(f"/api/prices/{src['id']}/preview", headers=H).status_code == 404


def test_import_from_desktop_and_stale(app_factory, files):  # noqa: F811
    from app import db, prices

    make_client, _ = app_factory
    client = register(make_client(), "prices3@example.com", org="Прайсы 3")
    client.post("/api/suppliers", headers=H, json={"section": "url_csv", "config": {"cache_hours": 6},
                "secrets": {"urls": [f"{files}/price.csv", "ftp://u:p@ftp.example.ru/dir/"],
                            "url_profiles": {f"{files}/price.csv": {"name": "Прайс Хрусталева", "enabled": True}}}})
    sources = client.get("/api/prices").json()["sources"]  # первый вход — источники из settings.json
    assert sorted(s["name"] for s in sources) == ["dir", "Прайс Хрусталева"] or len(sources) == 2
    assert {s["kind"] for s in sources} == {"url", "ftp"} and all(s["schedule_hours"] == 6 for s in sources)
    assert client.post("/api/prices/import-desktop", headers=H).json()["added"] == 0  # повтор не дублирует
    csv_source = next(s for s in sources if s["kind"] == "url")
    wait_load(client, csv_source["id"])
    org_id = org_of(client, "prices3@example.com")
    Session = client.app.state.pricer["Session"]
    provider = prices.PriceDbProvider(Session, org_id)
    assert provider.get_prices("OC90")
    with Session() as session:  # прайс не обновлялся 8 дней — в выдачу не попадает
        row = session.get(db.PriceSource, csv_source["id"])
        row.loaded_at = db.utcnow() - datetime.timedelta(days=8)
        session.commit()
        assert prices.view(row)["state"] == "stale" and prices.due(row)
    assert provider.get_prices("OC90") == []


def test_helpers():
    from app import prices

    assert prices.article_key("W 712/95") == "w71295"
    assert prices.location_hint("https://u:p@host.ru/a/b/price.csv?token=1") == "https://host.ru/…/price.csv"
    assert prices.guess_mapping(["Код", "Номер детали", "Бренд", "Цена, руб", "Кол-во"])["brand"] == "Бренд"
