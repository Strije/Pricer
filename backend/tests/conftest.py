import json
import os
import re
import shutil

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")


def clean_num(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper()) if value else ""


@pytest.fixture
def brand_resolver(tmp_path, monkeypatch):
    """Справочник брендов на реальных данных. Файлы копируются во временную папку:
    резолвер дописывает сопоставления брендов в файл, репозиторий трогать нельзя."""
    import brand_aliases

    for name in ("brand_groups.json", "brand_provider_mappings.json", "brands.txt"):
        shutil.copy(os.path.join(DATA, name), tmp_path / name)
    monkeypatch.setattr(brand_aliases, "get_brand_storage_dir", lambda: str(tmp_path))
    monkeypatch.setenv("PROCENKA_CONFIG_DIR", str(tmp_path / "config"))
    return brand_aliases.BrandAliasResolver()


@pytest.fixture(scope="session")
def order_file_rows():
    with open(os.path.join(HERE, "fixtures", "order_file_rows.json"), encoding="utf-8") as file:
        return json.load(file)
