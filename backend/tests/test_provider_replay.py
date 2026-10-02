"""Адаптеры поставщиков на записанных ответах (запись 2026-10-02, 3 позиции, 10 поставщиков).

Эталон — снимок того, что текущий код адаптеров извлёк из ответов. Любое изменение разбора
при переносе в веб будет видно в тесте. Пересоздать снимок: UPDATE_SNAPSHOTS=1 pytest.
"""
import json
import os
import re

import pytest

import replay
from replay import DUMMY

SNAPSHOT = os.path.join(replay.RECORDINGS, "snapshot-2026-10-02.json")
ITEMS = [("ZIC", "162622"), ("MANN", "W712/95"), ("AIRLINE", "AHR12D01")]
FIELDS = ("article", "brand", "price", "warehouse", "quantity", "days", "delivery_hours",
          "delivery_total_hours", "is_cross", "supplier_offer_id")


def clean(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def build_providers():
    from abstd import AbstdProvider
    from armtek import ArmtekProvider
    from avtoto import AvtotoProvider
    from favorit import FavoritProvider
    from forum_auto import ForumAutoProvider
    from mikado import MikadoProvider
    from pr_lg import PrLgProvider
    from rossko import RosskoProvider
    from tiss_tmparts import TissTmpartsProvider
    from tradesoft import TradesoftProvider

    login, password, key = DUMMY, DUMMY + "PW", DUMMY + "KEY"
    return {
        "Profit-League": PrLgProvider(api_key=key, timeout=12, create_order=True),
        "Фаворит": FavoritProvider(key, True, timeout=12, developer_key=key),
        "Armtek": ArmtekProvider(login, password, "4000", "1", timeout=12),
        "Forum-Auto": ForumAutoProvider(login, password, True, timeout=10),
        "Mikado": MikadoProvider(login, password, timeout=10),
        "ABSTD": AbstdProvider(login, password, "1", "0", "", "2", timeout=10),
        "Rossko": RosskoProvider(key, key, "000000002", "", "1", "", "", "", timeout=15),
        "Avtoto": AvtotoProvider(login, login, password, True, timeout=20),
        "TISS": TissTmpartsProvider(
            key, "1", timeout=20, include_analogues=True,
            legal_organization_id="org", contract_id="contract", outlet_id="outlet",
        ),
        "Автоформула": TradesoftProvider(
            login, password, provider_id="AVTOFORMULA", provider_login=login,
            provider_password=password, timeout=20, include_analogues=True,
        ),
    }


def search(name, provider, brand, article):
    article_key = clean(article)
    if name == "Profit-League":
        return provider.get_prices_parallel(article, article_key, brand)
    if name == "Rossko":
        return provider.get_prices(article)
    if name == "Forum-Auto":
        return provider.get_prices(article_key)
    if name == "Avtoto":
        return provider.get_prices(article_key, brand=brand, include_crosses=True)
    return provider.get_prices(article_key, brand=brand)


def summarize(rows):
    rows = list(rows or [])
    picked = sorted(rows, key=lambda r: (float(r.get("price") or 0), str(r.get("warehouse") or ""),
                                         str(r.get("supplier_offer_id") or "")))[:5]
    return {
        "count": len(rows),
        "exact": sum(1 for r in rows if clean(r.get("article")) in {clean(a) for _, a in ITEMS}),
        "cheapest": [{f: r.get(f) for f in FIELDS if f in r} for r in picked],
    }


@pytest.fixture(scope="module")
def results():
    import time

    import armtek
    import favorit

    mp = pytest.MonkeyPatch()
    records = replay.load()
    replayer = replay.install(mp, records)
    mp.setattr(time, "sleep", lambda *_: None)
    mp.setattr(favorit, "datetime", replay.FrozenDateTime)
    mp.setattr(armtek.datetime, "datetime", replay.FrozenDateTime)
    providers = build_providers()
    out = {}
    for brand, article in ITEMS:
        for name, provider in providers.items():
            out[f"{brand}:{article}:{name}"] = summarize(search(name, provider, brand, article))
    mp.undo()
    return {"rows": out, "replayer": replayer, "records": records}


def test_no_network_request_left_unmatched(results):
    assert results["replayer"].unmatched == []


def test_every_provider_parsed_something(results):
    for name in build_providers():
        total = sum(v["count"] for k, v in results["rows"].items() if k.endswith(":" + name))
        assert total > 0, name


def test_recorded_selection_is_among_parsed_offers(results):
    """Итог подбора десктопа (search_result) должен найтись в разборе ответа того же поставщика."""
    checked = 0
    for record in results["records"]:
        if record.get("type") != "search_result":
            continue
        offer = (record.get("result") or {}).get("offer") or {}
        row = record["row"]
        key = f"{row['brand']}:{row['article']}:{offer.get('provider')}"
        summary = results["rows"][key]
        prices = [o["price"] for o in summary["cheapest"]]
        assert summary["count"] > 0
        assert min(float(p) for p in prices) <= float(offer["price"]) + 1e-6
        checked += 1
    assert checked == 3


def test_matches_snapshot(results):
    rows = json.loads(json.dumps(results["rows"], ensure_ascii=False, default=str))
    if os.environ.get("UPDATE_SNAPSHOTS") or not os.path.exists(SNAPSHOT):
        with open(SNAPSHOT, "w", encoding="utf-8") as file:
            json.dump(rows, file, ensure_ascii=False, indent=1, sort_keys=True)
    with open(SNAPSHOT, encoding="utf-8") as file:
        assert rows == json.load(file)
