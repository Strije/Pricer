"""Окружение для тестов, перенесённых из десктопа (C:\\Price\\tests).

Десктоп запускал их из папки проекта, где лежат справочники брендов, а настройки писались
в config\\. Здесь то же самое во временной папке: справочник из backend/data (резолвер
дописывает в него сопоставления, репозиторий трогать нельзя) и отдельная папка настроек.
"""
import os
import shutil

import pytest

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")


@pytest.fixture(autouse=True)
def desktop_project_dirs(tmp_path, monkeypatch):
    import brand_aliases

    brands = tmp_path / "brands"
    brands.mkdir()
    for name in ("brand_groups.json", "brand_provider_mappings.json", "brands.txt"):
        shutil.copy(os.path.join(DATA, name), brands / name)
    monkeypatch.setattr(brand_aliases, "get_brand_storage_dir", lambda: str(brands))
    monkeypatch.setenv("PROCENKA_CONFIG_DIR", str(tmp_path / "config"))
