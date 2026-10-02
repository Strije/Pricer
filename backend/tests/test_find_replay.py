"""Поиск «Проценки» на записанных ответах поставщиков (record_provider_responses.py --find, 02.10.2026):
веб-движок называет те же бренды, выбирает тот же и находит столько же предложений, сколько десктоп
(без локальных прайсов — в вебе они подключаются отдельным шагом)."""
import json
import os
import shutil
import time
from collections import Counter

import pytest

import replay
from app.search import choose_brand

SETTINGS = os.path.join(replay.RECORDINGS, "engine_settings.json")


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    import armtek
    import brand_aliases
    import favorit

    from conftest import DATA

    tmp = tmp_path_factory.mktemp("brands")
    for name in ("brand_groups.json", "brand_provider_mappings.json", "brands.txt"):
        shutil.copy(os.path.join(DATA, name), tmp / name)
    mp = pytest.MonkeyPatch()
    mp.setattr(brand_aliases, "get_brand_storage_dir", lambda: str(tmp))
    mp.setenv("PROCENKA_CONFIG_DIR", str(tmp / "config"))
    records = replay.load("find-2026-10-02.jsonl.gz")
    replay.install(mp, records)
    mp.setattr(time, "sleep", lambda *_: None)
    mp.setattr(favorit, "datetime", replay.FrozenDateTime)
    mp.setattr(armtek, "datetime", replay.frozen_datetime_module())
    from engine import ProcurementEngine

    with open(SETTINGS, encoding="utf-8") as file:
        engine = ProcurementEngine(json.load(file), brand_aliases=brand_aliases.BrandAliasResolver())
    out = {}
    try:
        for record in records:
            if record.get("type") != "find_result":
                continue
            choices, answered = engine.search_brands(record["article"])
            picked = choose_brand(choices, answered)
            target = picked or next(c for c in choices if c["brand"].upper() == record["selected_brand"].upper())
            out[record["article"]] = (record, choices, picked, engine.search_offers(record["article"], target["choice"]))
    finally:
        mp.undo()
    return out


def test_brand_votes_like_desktop(runs):
    for article, (record, choices, picked, _) in runs.items():
        assert choices[0]["brand"].upper() == record["selected_brand"].upper(), article


def test_auto_choice_uses_mentions(runs):
    # 162622 — ZIC с явным перевесом; W71295 — MANN (7 голосов против 4 у Redskin — берём сами);
    # OC90 — MAHLE и AM POINT почти поровну: спрашиваем пользователя.
    assert runs["162622"][2]["brand"] == "ZIC"
    assert runs["W71295"][2]["brand"] == "MANN" and runs["W71295"][1][0]["mentions"] >= 1
    assert runs["OC90"][2] is None
    # и без упоминаний MANN берётся: перевес 1,5 раза, а не вдвое
    w = runs["W71295"][1]
    assert choose_brand([{**c, "score": c["votes"]} for c in w], w[0]["providers"] + w[1]["providers"])["brand"] == "MANN"


def test_offers_like_desktop(runs):
    for article, (record, _, _, results) in runs.items():
        own = sum(not r.get("is_cross") for r in results)
        assert own == record["own"], article
        per = Counter(r["provider"] for r in results)
        for provider, count in record["per_provider"].items():
            assert per.get(provider, 0) >= count * 0.95, (article, provider)  # Avtoto в повторе — плюс-минус несколько строк


def test_stars_and_returnable(runs):
    _, _, _, results = runs["162622"]
    assert max(r["provider_confirm_count"] for r in results) >= 5  # ★ — у ходовых аналогов много подтверждений
    assert all("returnable" in r for r in results)
