"""Веб-сервис в режиме демонстрации (PRICER_REPLAY): вход, поставщики организации, поиск."""
import io
import json
import os

import pytest

H = {"X-Requested-With": "pricer"}
SETTINGS_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "recordings", "engine_settings.json")


@pytest.fixture(scope="module")
def app_factory(tmp_path_factory):
    import time

    import armtek
    import favorit
    from fastapi.testclient import TestClient
    from requests.adapters import HTTPAdapter

    mp = pytest.MonkeyPatch()
    mp.setenv("PRICER_REPLAY", "1")
    # create_app подменяет сеть и время глобально — вернём всё после модуля
    mp.setattr(HTTPAdapter, "send", HTTPAdapter.send)
    mp.setattr(time, "sleep", time.sleep)
    mp.setattr(favorit, "datetime", favorit.datetime)
    mp.setattr(armtek, "datetime", armtek.datetime)
    from app.server import create_app

    var = tmp_path_factory.mktemp("var")
    app = create_app(var_dir=str(var))
    clients = []

    def make_client():
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make_client, var
    for client in clients:
        client.__exit__(None, None, None)
    mp.undo()


def register(client, email, org="Тест"):
    r = client.post("/api/auth/register", headers=H,
                    json={"email": email, "password": "password123", "organization": org})
    assert r.status_code == 200, r.text
    return client


def read_events(client, job_id):
    events = []
    with client.stream("GET", f"/api/search/{job_id}/events") as response:
        assert response.status_code == 200
        kind = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                events.append((kind, json.loads(line[6:])))
    return events


def search(client, brand, article):
    r = client.post("/api/search", headers=H, json={"brand": brand, "article": article})
    assert r.status_code == 200, r.text
    return read_events(client, r.json()["job_id"])


def test_requires_login_and_csrf_header(app_factory):
    make_client, _ = app_factory
    client = make_client()
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/search", headers=H, json={"brand": "A", "article": "1"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "a@b", "password": "x"}).status_code == 403
    assert client.get("/").status_code == 200


def test_register_login_logout(app_factory):
    make_client, _ = app_factory
    client = register(make_client(), "owner@example.com")
    me = client.get("/api/me").json()
    assert me["role"] == "admin" and me["replay_mode"] is True
    assert "Avtoto" in me["providers"]  # без своих поставщиков в демо-режиме — тестовые настройки
    assert client.post("/api/auth/logout", headers=H).status_code == 200
    assert client.get("/api/me").status_code == 401
    bad = client.post("/api/auth/login", headers=H, json={"email": "owner@example.com", "password": "wrong-pass"})
    assert bad.status_code == 401
    good = client.post("/api/auth/login", headers=H, json={"email": "OWNER@example.com", "password": "password123"})
    assert good.status_code == 200 and client.get("/api/me").status_code == 200
    duplicate = make_client().post("/api/auth/register", headers=H,
                                   json={"email": "owner@example.com", "password": "password123", "organization": "X"})
    assert duplicate.status_code == 409


@pytest.mark.parametrize("brand,article,provider,price,sale", [
    ("ZIC", "162622", "Avtoto", 1954.0, 2700),
    ("MANN", "W712/95", "Forum-Auto", 481.2, 730),
])
def test_search_streams_providers_and_result(app_factory, brand, article, provider, price, sale):
    make_client, _ = app_factory
    client = register(make_client(), f"s-{brand.lower()}@example.com")
    events = search(client, brand, article)
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "result" and kinds.count("provider") >= 10
    result = events[-1][1]
    assert result["status"] == "Готово"
    assert result["offer"]["provider"] == provider
    assert result["offer"]["price"] == price and result["offer"]["sale_price"] == sale


def test_import_settings_encrypts_secrets_and_keeps_search(app_factory):
    make_client, var = app_factory
    client = register(make_client(), "import@example.com", org="Импорт")
    with open(SETTINGS_FIXTURE, encoding="utf-8") as file:
        settings = json.load(file)
    upload = client.post("/api/import/settings", headers=H,
                         files={"file": ("settings.json", io.BytesIO(json.dumps(settings).encode()), "application/json")})
    assert upload.status_code == 200, upload.text
    assert upload.json()["accounts"] >= 11

    suppliers = client.get("/api/suppliers").json()
    text = json.dumps(suppliers, ensure_ascii=False)
    assert "REPLAYSECRET" not in text  # значения секретов наружу не уходят
    armtek = next(s for s in suppliers if s["section"] == "armtek")
    assert {"login", "password"} <= set(armtek["secrets_set"])
    assert armtek["config"]["allowed_warehouses"]  # открытые поля видны

    from sqlalchemy import text

    with client.app.state.pricer["Session"]() as session:  # и в самой базе они зашифрованы (SQLite или PostgreSQL)
        raw_rows = session.execute(text("SELECT config, secrets_sealed FROM supplier_accounts")).all()
    assert raw_rows and "REPLAYSECRET" not in str(raw_rows)

    # Поиск теперь идёт по поставщикам из базы — результат тот же, что у десктопа
    result = search(client, "AIRLINE", "AHR12D01")[-1][1]
    assert result["offer"]["provider"] == "Автоформула" and result["offer"]["sale_price"] == 990


def test_update_keeps_blank_secret_and_deletes_null(app_factory):
    make_client, _ = app_factory
    client = register(make_client(), "edit@example.com", org="Правка")
    created = client.post("/api/suppliers", headers=H, json={
        "section": "mikado", "config": {"timeout": 10, "password": "не сюда"},
        "secrets": {"login": "L1", "password": "P1"},
    })
    assert created.status_code == 200, created.text
    account = created.json()
    assert "password" not in account["config"]  # секретное поле в открытую часть не попало
    assert account["secrets_set"] == ["login", "password"]
    updated = client.put(f"/api/suppliers/{account['id']}", headers=H,
                         json={"config": {"timeout": 15}, "secrets": {"login": "", "password": None}}).json()
    assert updated["config"]["timeout"] == 15
    assert updated["secrets_set"] == ["login"]
    again = client.post("/api/suppliers", headers=H, json={"section": "mikado"})
    assert again.status_code == 409  # одиночный поставщик подключается один раз
    abcp1 = client.post("/api/suppliers", headers=H, json={"section": "abcp_suppliers", "config": {"name": "A"}})
    abcp2 = client.post("/api/suppliers", headers=H, json={"section": "abcp_suppliers", "config": {"name": "B"}})
    assert abcp1.status_code == abcp2.status_code == 200  # поставщиков ABCP может быть сколько угодно


def test_organizations_are_isolated(app_factory):
    make_client, _ = app_factory
    first = register(make_client(), "first@example.com", org="Первая")
    second = register(make_client(), "second@example.com", org="Вторая")
    account = first.post("/api/suppliers", headers=H, json={"section": "rossko", "secrets": {"key1": "K"}}).json()
    assert second.get("/api/suppliers").json() == []
    assert second.put(f"/api/suppliers/{account['id']}", headers=H, json={"config": {}}).status_code == 404
    assert second.delete(f"/api/suppliers/{account['id']}", headers=H).status_code == 404
    # У первой организации неполный Rossko (нет KEY2 и доставки) — поставщиков для поиска нет
    refused = first.post("/api/search", headers=H, json={"brand": "ZIC", "article": "162622"})
    assert refused.status_code == 400
    # Поиск второй организации (демо-настройки) первая не видит
    job = second.post("/api/search", headers=H, json={"brand": "ZIC", "article": "162622"}).json()["job_id"]
    assert first.get(f"/api/search/{job}/events").status_code == 404
    assert read_events(second, job)[-1][0] == "result"


def test_org_settings(app_factory):
    make_client, _ = app_factory
    client = register(make_client(), "settings@example.com", org="Настройки")
    assert client.put("/api/org/settings", headers=H, json={"hide_no_return": True}).json()["hide_no_return"] is True
    assert client.put("/api/org/settings", headers=H, json={"armtek": {}}).status_code == 400


def test_validation(app_factory):
    make_client, _ = app_factory
    client = register(make_client(), "valid@example.com")
    assert client.post("/api/search", headers=H, json={"brand": "", "article": "1"}).status_code == 422
    assert client.get("/api/search/nope/events").status_code == 404
    short = make_client().post("/api/auth/register", headers=H,
                               json={"email": "x@y", "password": "short", "organization": "Z"})
    assert short.status_code == 422


def test_supplier_list_active_and_check(app_factory):
    """Список поставщиков: построился ли поставщик из подключения; «Проверить» — пробный запрос."""
    make_client, _ = app_factory
    client = register(make_client(), "check@example.com", org="Проверка")
    created = client.post("/api/suppliers", headers=H, json={"section": "rossko"}).json()
    row = next(a for a in client.get("/api/suppliers").json() if a["id"] == created["id"])
    assert row["active"] is False and row["status"] == {}  # ключей нет
    status = client.post(f"/api/suppliers/{created['id']}/check", headers=H).json()
    assert status["ok"] is False and "не хватает" in status["message"] and status["at"]
    assert next(a for a in client.get("/api/suppliers").json() if a["id"] == created["id"])["status"]["ok"] is False
    other = register(make_client(), "check-other@example.com", org="Чужая")
    assert other.post(f"/api/suppliers/{created['id']}/check", headers=H).status_code == 404


def test_login_limiter_and_health(app_factory):
    make_client, _ = app_factory
    register(make_client(), "limit@example.com", org="Лимит")
    client = make_client()
    for _ in range(5):
        assert client.post("/api/auth/login", headers=H, json={"email": "limit@example.com", "password": "wrong"}).status_code == 401
    blocked = client.post("/api/auth/login", headers=H, json={"email": "limit@example.com", "password": "password123"})
    assert blocked.status_code == 429  # даже верный пароль — после 5 неудач подождать
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/docs").status_code == 404  # документация API закрыта по умолчанию


def test_login_limiter_window():
    from app.security import LoginLimiter

    now = [0.0]
    limiter = LoginLimiter(limit=2, window=60, clock=lambda: now[0])
    limiter.fail("a"); limiter.fail("a")
    assert limiter.wait("a") > 0 and limiter.wait("b") == 0
    now[0] = 61
    assert limiter.wait("a") == 0


def test_manage_commands(tmp_path, monkeypatch, capsys):
    from app import manage

    monkeypatch.setenv("PRICER_VAR_DIR", str(tmp_path))
    monkeypatch.delenv("PRICER_DATABASE_URL", raising=False)
    assert manage.main(["create-org", "Автодруг", "Admin@Example.com"]) == 0
    out = capsys.readouterr().out
    password = out.split("пароль: ")[1].split()[0]
    from app.security import verify_password
    with manage.sessionmaker()() as session:
        from app import db
        user = session.query(db.User).filter_by(email="admin@example.com").one()
        assert user.role == "admin" and verify_password(password, user.password_hash)
    assert manage.main(["reset-password", "admin@example.com"]) == 0
    assert "Новый пароль" in capsys.readouterr().out
    manage.main(["list"])
    assert "Автодруг" in capsys.readouterr().out
    assert manage.main(["what"]) == 2


def test_change_password_closes_other_sessions(app_factory):
    make_client, _ = app_factory
    first = register(make_client(), "pw@example.com", org="Пароль")
    second = make_client()
    second.post("/api/auth/login", headers=H, json={"email": "pw@example.com", "password": "password123"})
    assert first.post("/api/auth/password", headers=H, json={"current": "bad", "new": "newpassword1"}).status_code == 400
    assert first.post("/api/auth/password", headers=H, json={"current": "password123", "new": "short"}).status_code == 422
    assert first.post("/api/auth/password", headers=H, json={"current": "password123", "new": "newpassword1"}).json()["ok"]
    assert first.get("/api/me").status_code == 200  # свой вход остаётся
    assert second.get("/api/me").status_code == 401  # другие — закрыты
    assert make_client().post("/api/auth/login", headers=H, json={"email": "pw@example.com", "password": "newpassword1"}).status_code == 200


def test_services_check_and_site_links(app_factory, monkeypatch):
    """Сервисы: проверка ЮKassa («информация о магазине») и Laximo; ссылка поиска на сайте поставщика."""
    import requests

    from laximo import LaximoClient, LaximoError

    make_client, _ = app_factory
    client = register(make_client(), "svc@example.com", org="Сервисы")
    assert client.post("/api/services/yookassa/check", headers=H).status_code == 404  # не подключена
    yk = client.post("/api/suppliers", headers=H, json={"section": "yookassa", "config": {"shop_id": "1", "receipts": True},
                                                         "secrets": {"secret_key": "live_x"}}).json()

    class Me:
        status_code = 200

        def json(self):
            return {"account_id": "1", "test": False, "fiscalization_enabled": False, "status": "enabled"}

    class S:
        def request(self, *a, **k):
            return Me()
    monkeypatch.setattr(requests, "Session", lambda: S())
    status = client.post("/api/services/yookassa/check", headers=H).json()
    assert status["ok"] is False and "чеки" in status["message"]  # в Pricer чеки включены, в ЮKassa — нет
    client.put(f"/api/suppliers/{yk['id']}", headers=H, json={"config": {"shop_id": "1", "receipts": False}, "secrets": {}})
    assert client.post("/api/services/yookassa/check", headers=H).json()["ok"] is True

    client.post("/api/suppliers", headers=H, json={"section": "laximo", "secrets": {"login": "L", "password": "P"}})

    def denied(self, query):
        raise LaximoError("доступ запрещён")
    monkeypatch.setattr(LaximoClient, "find_vehicle", denied)
    assert client.post("/api/services/laximo/check", headers=H).json() == {**client.post("/api/services/laximo/check", headers=H).json(), "ok": False}

    from app.search import offer_view

    class E:
        site_links = {"Rossko": "https://rossko.ru/search?text={article}"}

        def apply_markup(self, price):
            return price, 0, 0
    row = offer_view({"provider": "Rossko", "article": "W712/95", "purchase_price": 1}, E())
    assert row["site_url"] == "https://rossko.ru/search?text=W712/95"
    assert "site_url" not in offer_view({"provider": "Rossko", "article": "1", "purchase_price": 1}, E(), customer=True)
