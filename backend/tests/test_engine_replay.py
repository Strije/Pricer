"""Сквозной тест вынесенного движка (core/engine.py) на записанных ответах поставщиков.

Десктоп при записи сам подобрал предложение по каждой строке (search_result в записи).
Вынесенный движок с теми же настройками складов, возврата и наценки должен выбрать то же.
"""
import json
import os
import time

import pytest

import replay

SETTINGS = os.path.join(replay.RECORDINGS, "engine_settings.json")


@pytest.fixture(scope="module")
def engine_results(tmp_path_factory):
    import armtek
    import brand_aliases
    import favorit
    import shutil

    from conftest import DATA

    tmp = tmp_path_factory.mktemp("brands")
    for name in ("brand_groups.json", "brand_provider_mappings.json", "brands.txt"):
        shutil.copy(os.path.join(DATA, name), tmp / name)

    mp = pytest.MonkeyPatch()
    mp.setattr(brand_aliases, "get_brand_storage_dir", lambda: str(tmp))
    mp.setenv("PROCENKA_CONFIG_DIR", str(tmp / "config"))
    records = replay.load()
    replayer = replay.install(mp, records)
    mp.setattr(time, "sleep", lambda *_: None)
    mp.setattr(favorit, "datetime", replay.FrozenDateTime)
    mp.setattr(armtek, "datetime", replay.frozen_datetime_module())

    from engine import ProcurementEngine

    with open(SETTINGS, encoding="utf-8") as file:
        settings = json.load(file)
    engine = ProcurementEngine(settings, brand_aliases=brand_aliases.BrandAliasResolver())
    out = []
    for record in records:
        if record.get("type") != "search_start":
            continue
        out.append((record, engine.search_order_row(record["row"], record["options"])))
    mp.undo()
    expected = {json.dumps(r["row"], sort_keys=True): r["result"] for r in records if r.get("type") == "search_result"}
    return out, expected, replayer, engine


def test_engine_builds_all_recorded_providers(engine_results):
    _, _, _, engine = engine_results
    names = {type(p).__name__ for p in engine.providers}
    assert {"ArmtekProvider", "MikadoProvider", "RosskoProvider", "AvtotoProvider", "TradesoftProvider"} <= names


def test_engine_selects_same_offer_as_desktop(engine_results):
    results, expected, _, _ = engine_results
    assert len(results) == 3
    for start, result in results:
        want = expected[json.dumps(start["row"], sort_keys=True)]
        got_offer, want_offer = result.get("offer") or {}, want.get("offer") or {}
        label = f"{start['row']['brand']} {start['row']['article']}"
        assert result.get("status") == want.get("status"), label
        assert got_offer.get("provider") == want_offer.get("provider"), label
        assert float(got_offer.get("price")) == float(want_offer.get("price")), label
        for field in ("article", "brand", "warehouse", "delivery_hours", "sale_price", "actual_order_quantity"):
            assert got_offer.get(field) == want_offer.get(field), f"{label}: {field}"


def test_engine_result_shape_matches_desktop(engine_results):
    results, expected, _, _ = engine_results
    for start, result in results:
        want = expected[json.dumps(start["row"], sort_keys=True)]
        assert set(result) == set(want)


def test_engine_requests_are_all_in_recording(engine_results):
    _, _, replayer, _ = engine_results
    assert replayer.unmatched == []
