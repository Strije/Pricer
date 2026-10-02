"""Веб-сервис в режиме демонстрации (PRICER_REPLAY): поиск, поток событий, итог как у десктопа."""
import json

import pytest


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import time

    import armtek
    import favorit
    from fastapi.testclient import TestClient
    from requests.adapters import HTTPAdapter

    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("srv")
    mp.setenv("PRICER_REPLAY", "1")
    mp.setenv("PRICER_DATA_DIR", str(tmp / "brands"))
    mp.setenv("PROCENKA_CONFIG_DIR", str(tmp / "config"))
    # build_engine подменяет сеть и время глобально — вернём всё после модуля
    mp.setattr(HTTPAdapter, "send", HTTPAdapter.send)
    mp.setattr(time, "sleep", time.sleep)
    mp.setattr(favorit, "datetime", favorit.datetime)
    mp.setattr(armtek.datetime, "datetime", armtek.datetime.datetime)
    from app.server import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    mp.undo()


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


def test_status_lists_providers(client):
    data = client.get("/api/status").json()
    assert data["replay_mode"] is True
    assert "Armtek" in data["providers"] and "Avtoto" in data["providers"]


def test_index_page(client):
    response = client.get("/")
    assert response.status_code == 200 and "Pricer" in response.text


@pytest.mark.parametrize("brand,article,provider,price,sale", [
    ("ZIC", "162622", "Avtoto", 1954.0, 2700),
    ("MANN", "W712/95", "Forum-Auto", 481.2, 730),
])
def test_search_streams_providers_and_result(client, brand, article, provider, price, sale):
    started = client.post("/api/search", json={"brand": brand, "article": article, "quantity": 1})
    assert started.status_code == 200
    events = read_events(client, started.json()["job_id"])
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "result"
    assert kinds.count("provider") >= 10  # итог по каждому поставщику пришёл отдельным событием
    result = events[-1][1]
    assert result["status"] == "Готово"
    assert result["offer"]["provider"] == provider
    assert result["offer"]["price"] == price and result["offer"]["sale_price"] == sale
    assert result["alternatives"] and len(result["alternatives"]) <= 200


def test_validation(client):
    assert client.post("/api/search", json={"brand": "", "article": "1"}).status_code == 422
    assert client.get("/api/search/nope/events").status_code == 404


def test_repeated_search_gives_same_result(client):
    results = []
    for _ in range(2):
        job = client.post("/api/search", json={"brand": "AIRLINE", "article": "AHR12D01"}).json()["job_id"]
        results.append(read_events(client, job)[-1][1])
    assert results[0]["alternatives_total"] == results[1]["alternatives_total"]
    assert results[0]["offer"] == results[1]["offer"]
