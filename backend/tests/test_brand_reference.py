"""Справочник синонимов брендов ABCP: чтение выгрузки, установка в папку данных, подключение к резолверу."""
import json

import pytest
from openpyxl import Workbook

from app import brand_reference


def _brands(count, extra=()):
    rows = [{"name": f"Brand{i}", "aliases": [f"BRAND-{i} GMBH"]} for i in range(count)]
    return rows + list(extra)


def test_reads_abcp_export_xlsx(tmp_path):
    book = Workbook()
    groups = book.active
    groups.title = "Группы брендов"  # лист групп первым: брать нужно лист «Бренды»
    groups.append(["Название группы", "Бренды"])
    groups.append(["TOYOTA", "TOYOTA, DAIHATSU"])
    sheet = book.create_sheet("Бренды")
    sheet.append(["Бренд", "Алиас(Синоним)"])
    sheet.append(["1-56 (Maruichi)", "Maruichi, MARUICHI(1-56), 1-56"])
    sheet.append(["101 Octane", None])
    sheet.append(["1-56 (MARUICHI)", "дубль"])
    path = tmp_path / "report.xlsx"
    book.save(path)
    assert brand_reference.read_reference(str(path)) == [
        {"name": "1-56 (Maruichi)", "aliases": ["Maruichi", "MARUICHI(1-56)", "1-56"]},
        {"name": "101 Octane", "aliases": []},
    ]


def test_reads_desktop_json(tmp_path):
    path = tmp_path / "brand_reference_abcp.json"
    path.write_text(json.dumps([{"name": "+20% Free", "is_original": False, "aliases": ["+20% FREEE", "20%FREEE"]}]),
                    encoding="utf-8")
    assert brand_reference.read_reference(str(path)) == [{"name": "+20% Free", "aliases": ["+20% FREEE", "20%FREEE"]}]
    with pytest.raises(ValueError):
        brand_reference.read_reference(str(tmp_path / "brands.csv"))


def test_install_replaces_file_and_reports_changes(tmp_path):
    source = tmp_path / "ref.json"
    source.write_text(json.dumps([{"name": "x", "aliases": []}]), encoding="utf-8")
    with pytest.raises(ValueError):  # десяток строк — не справочник ABCP, старый файл не трогаем
        brand_reference.install(str(source), str(tmp_path / "brands"))
    source.write_text(json.dumps(_brands(150)), encoding="utf-8")
    first = brand_reference.install(str(source), str(tmp_path / "brands"))
    assert first["brands"] == 150 and first["aliases"] == 150 and first["added"] is None
    source.write_text(json.dumps(_brands(149, [{"name": "Новый", "aliases": []}])), encoding="utf-8")
    second = brand_reference.install(str(source), str(tmp_path / "brands"))
    assert (second["added"], second["removed"]) == (1, 1)


def test_resolver_uses_installed_reference(tmp_path, monkeypatch):
    import brand_aliases

    brands = tmp_path / "brands"
    brands.mkdir()
    monkeypatch.setattr(brand_aliases, "get_brand_storage_dir", lambda: str(brands))
    monkeypatch.setattr(brand_aliases, "get_bundled_resource_dir", lambda: str(brands))
    monkeypatch.setenv("PROCENKA_CONFIG_DIR", str(tmp_path / "config"))
    assert not brand_aliases.BrandAliasResolver().same("Zzyzx Parts", "ZZYZX-PARTS GMBH")
    source = tmp_path / "ref.json"
    source.write_text(json.dumps(_brands(120, [{"name": "Zzyzx Parts", "aliases": ["ZZYZX-PARTS GMBH"]}])),
                      encoding="utf-8")
    brand_reference.install(str(source), str(brands))
    resolver = brand_aliases.BrandAliasResolver()
    assert resolver.same("Zzyzx Parts", "ZZYZX-PARTS GMBH") and resolver.title("zzyzx-parts gmbh") == "Zzyzx Parts"


def test_server_reads_reference_from_its_data_dir(tmp_path, monkeypatch):
    import brand_aliases

    # create_app подменяет функции модуля — monkeypatch вернёт их после теста
    monkeypatch.setattr(brand_aliases, "get_brand_storage_dir", brand_aliases.get_brand_storage_dir)
    monkeypatch.setattr(brand_aliases, "get_bundled_resource_dir", brand_aliases.get_bundled_resource_dir)
    monkeypatch.delenv("PRICER_REPLAY", raising=False)
    monkeypatch.delenv("PRICER_DATA_DIR", raising=False)
    from app.server import create_app

    create_app(var_dir=str(tmp_path))
    assert brand_aliases.get_bundled_resource_dir() == str(tmp_path / "brands")


def test_manage_import_brands(tmp_path, monkeypatch, capsys):
    from app import manage

    source = tmp_path / "ref.json"
    source.write_text(json.dumps(_brands(130)), encoding="utf-8")
    monkeypatch.setenv("PRICER_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("PRICER_DATA_DIR", raising=False)
    assert manage.main(["import-brands", str(source)]) == 0
    assert (tmp_path / "var" / "brands" / brand_reference.FILE_NAME).exists()
    assert "130 брендов" in capsys.readouterr().out
