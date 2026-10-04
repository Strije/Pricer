import unittest
import json
import os
import tempfile
from unittest import mock

import brand_aliases
from brand_aliases import BrandAliasResolver


class BrandAliasResolverTests(unittest.TestCase):
    def test_groups_common_supplier_spellings(self):
        resolver = BrandAliasResolver(paths=[])
        self.assertTrue(resolver.same("MANN", "MANN-FILTER"))
        self.assertTrue(resolver.same("MANN FILTER", "MANN+HUMMEL"))
        self.assertEqual(len(resolver.group(["MANN", "MANN-FILTER"])), 1)
        self.assertEqual(
            resolver.for_provider("HYUNDAI", "PrLgProvider"),
            "HYUNDAI/KIA/MOBIS",
        )
        self.assertEqual(resolver.title("RVI"), "RENAULT")
        self.assertEqual(
            resolver.for_provider("RVI", "PrLgProvider"),
            "RENAULT",
        )
        renault_candidates = resolver.candidates_for_provider(
            "RVI", "PrLgProvider", limit=5
        )
        self.assertEqual(renault_candidates[0], "RENAULT")
        self.assertIn("RVI", renault_candidates)
        renault_variants = [
            "Renault", "RENAULT", "RENAU", "RVI",
            "RENAULT/DACIA", "RENAULT, RENAULT KOREA",
            "RENAULT-SAMSUNG",
        ]
        self.assertEqual(
            {resolver.key(value) for value in renault_variants},
            {resolver.key("RENAULT")},
        )

    def test_loads_brand_id_aliases_and_skips_suspicious_huge_groups(self):
        rows = [
            {"BrandID": 10, "BrandName": "Sample Brand"},
            {"BrandID": 10, "BrandName": "SAMPLE-BRAND"},
        ]
        rows.extend(
            {"BrandID": 99, "BrandName": f"Unrelated {index}"}
            for index in range(101)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "brands.txt")
            with open(path, "w", encoding="utf-8") as file:
                json.dump(rows, file)
            resolver = BrandAliasResolver(paths=[], extra_paths=[path])

        self.assertTrue(resolver.same("Sample Brand", "SAMPLE-BRAND"))
        self.assertFalse(resolver.same("Unrelated 1", "Unrelated 2"))

    def test_loads_reference_brand_alias_json(self):
        rows = [
            {
                "name": "Sample Canon",
                "is_original": False,
                "aliases": ["Sample-Canon", "Sample Alias"],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "brands.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump(rows, file)
            resolver = BrandAliasResolver(paths=[path])

        self.assertTrue(resolver.same("Sample Canon", "Sample Alias"))
        self.assertEqual(resolver.title("Sample-Canon"), "Sample Canon")

    def test_title_returns_parent_name_for_aliases(self):
        rows = [
            {
                "name": "LYNXauto",
                "is_original": False,
                "aliases": ["lynx", "LYNX AUTO", "lynxauto"],
            },
            {
                "name": "Denso",
                "is_original": False,
                "aliases": ["DENSO", "denso"],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "brands.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump(rows, file)
            resolver = BrandAliasResolver(paths=[path])

        self.assertEqual(resolver.title("LYNX AUTO"), "LYNXauto")
        self.assertEqual(resolver.title("lynx"), "LYNXauto")
        self.assertEqual(resolver.title("DENSO"), "Denso")
        self.assertEqual(resolver.title("denso"), "Denso")

    def test_project_aliases_cover_order_file_supplier_spellings(self):
        resolver = BrandAliasResolver()

        self.assertTrue(resolver.same("FEBI BILSTEIN", "Febi"))
        self.assertTrue(resolver.same("HYUNDAI/KIA/MOBIS", "Hyundai-KIA"))
        self.assertTrue(resolver.same("3TON", "3 ton"))
        self.assertTrue(resolver.same("GRASS", "GraSS"))
        self.assertTrue(resolver.same("TOYOTA/LEXUS", "TOYOTA"))
        self.assertTrue(resolver.same("STARTVOLT", "СТАРТВОЛЬТ"))

    def test_catalog_labels_and_broken_encodings_do_not_merge_manufacturers(self):
        """Ярлык каталога не должен склеивать двух разных производителей.

        В боевом справочнике "OPEL (PSA)" после снятия скобок превращался в
        "OPEL", а битое "CITRO?N" — в "CITRON", и это объединяло General Motors,
        PSA и «Цитрон» в группу на 112 названий: деталь Citroen считалась
        искомой по запросу Chevrolet. Обычные алиасы объединять по-прежнему можно.
        """
        rows = [
            {"name": "General Motors", "aliases": ["GM", "CHEVROLET", "OPEL"]},
            {"name": "Peugeot-Citroen", "aliases": ["PSA", "CITROEN", "OPEL (PSA)"]},
            {"name": "TSN", "aliases": ["Цитрон", "CITRO?N"]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "brands.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump(rows, file)
            empty = os.path.join(directory, "brand_groups.json")
            # изолируемся от пользовательских групп проекта
            with mock.patch.object(brand_aliases, "get_custom_groups_path", lambda: empty):
                resolver = BrandAliasResolver(paths=[path])

        self.assertFalse(resolver.same("CHEVROLET", "CITROEN"))
        self.assertFalse(resolver.same("GM", "Цитрон"))
        self.assertFalse(resolver.same("CHEVROLET", "TSN"))
        # внутри своей записи всё по-прежнему связано
        self.assertTrue(resolver.same("GM", "CHEVROLET"))
        self.assertTrue(resolver.same("PSA", "CITROEN"))
        # ярлык остаётся вариантом написания, просто не объединяет производителей
        self.assertEqual(resolver.title("OPEL (PSA)"), "General Motors")

    def test_plain_alias_still_merges_two_reference_entries(self):
        """Обычный общий алиас объединять можно: так связаны Parts-Mall и PMC."""
        rows = [
            {"name": "P.M.C.", "aliases": []},
            {"name": "Parts-Mall", "aliases": ["PMC", "PARTSMALL"]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "brands.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump(rows, file)
            empty = os.path.join(directory, "brand_groups.json")
            with mock.patch.object(brand_aliases, "get_custom_groups_path", lambda: empty):
                resolver = BrandAliasResolver(paths=[path])

        self.assertTrue(resolver.same("Parts-Mall", "PMC"))
        self.assertTrue(resolver.same("P.M.C.", "PARTSMALL"))

    def test_real_reference_keeps_manufacturer_families_apart(self):
        resolver = BrandAliasResolver()

        self.assertFalse(resolver.same("CHEVROLET", "CITROEN"))
        self.assertFalse(resolver.same("OPEL", "PEUGEOT"))
        self.assertFalse(resolver.same("DAEWOO", "CITROEN"))
        # а осознанные объединения из brand_groups.json и MANUAL_FAMILIES живы
        self.assertTrue(resolver.same("HYUNDAI", "KIA"))
        self.assertTrue(resolver.same("MAHLE", "KNECHT"))


if __name__ == "__main__":
    unittest.main()
