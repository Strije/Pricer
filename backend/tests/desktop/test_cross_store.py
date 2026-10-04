# -*- coding: utf-8 -*-
"""Накопление пар «запрошенная деталь → аналог»."""
import os
import re
import tempfile
import unittest

from cross_store import CrossStore


def clean_num(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def brand_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


class CrossStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = CrossStore(os.path.join(self.dir.name, "cross.sqlite3"), async_writes=False)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _record(self, offers, brand="MANN", article="W 712/95"):
        return self.store.record(brand, article, offers, clean_num=clean_num, brand_key=brand_key)

    def _analogs(self, brand="MANN", article="W 712/95", **kwargs):
        return self.store.analogs_for(brand, article, clean_num=clean_num, brand_key=brand_key, **kwargs)

    def test_exact_match_is_not_an_analog(self):
        written = self._record([
            {"brand": "MANN", "article": "W 712/95", "provider": "Avtoto", "name": "Фильтр"},
            {"brand": "KNECHT", "article": "OC90", "provider": "Avtoto", "name": "Фильтр KNECHT"},
        ])

        self.assertEqual(written, 1)
        analogs = self._analogs()
        self.assertEqual([row["article"] for row in analogs], ["OC90"])

    def test_brand_aliases_decide_what_counts_as_exact(self):
        """Сравнение идёт по ключу бренда, а не по строке.

        В приложении сюда приходит brand_group_key с таблицей алиасов, поэтому
        MANN-FILTER и MANN — один бренд и в аналоги не попадают.
        """
        def alias_key(value):
            return brand_key(value).replace("FILTER", "")

        written = self.store.record(
            "MANN", "W 712/95",
            [{"brand": "MANN-FILTER", "article": "W71295", "provider": "Avtoto"}],
            clean_num=clean_num, brand_key=alias_key,
        )

        self.assertEqual(written, 0)

    def test_same_pair_from_two_providers_counts_as_two_confirmations(self):
        self._record([{"brand": "KNECHT", "article": "OC90", "provider": "Avtoto"}])
        self._record([{"brand": "KNECHT", "article": "OC90", "provider": "Rossko"}])

        row = self._analogs()[0]
        self.assertEqual(row["confirmations"], 2)
        self.assertEqual(row["providers"], ["Avtoto", "Rossko"])
        self.assertEqual(row["seen_count"], 2)

    def test_repeat_from_one_provider_does_not_inflate_confirmations(self):
        self._record([{"brand": "KNECHT", "article": "OC90", "provider": "Avtoto"}])
        self._record([{"brand": "KNECHT", "article": "OC90", "provider": "Avtoto"}])

        row = self._analogs()[0]
        self.assertEqual(row["confirmations"], 1)
        self.assertEqual(row["seen_count"], 2)

    def test_one_search_counts_a_pair_once_even_with_many_offers(self):
        # Один и тот же аналог приходит десятками предложений с разных складов.
        written = self._record([
            {"brand": "KNECHT", "article": "OC90", "provider": "Avtoto", "warehouse": "Москва"},
            {"brand": "KNECHT", "article": "OC 90", "provider": "Avtoto", "warehouse": "Ростов"},
            {"brand": "KNECHT", "article": "OC90", "provider": "Mikado"},
        ])

        self.assertEqual(written, 1)
        row = self._analogs()[0]
        self.assertEqual(row["seen_count"], 1)
        self.assertEqual(row["confirmations"], 2)

    def test_analogs_are_ranked_by_confirmations(self):
        self._record([
            {"brand": "KNECHT", "article": "OC90", "provider": "Avtoto"},
            {"brand": "FILTRON", "article": "OP520", "provider": "Avtoto"},
        ])
        self._record([{"brand": "FILTRON", "article": "OP520", "provider": "Rossko"}])
        self._record([{"brand": "FILTRON", "article": "OP520", "provider": "Mikado"}])

        self.assertEqual([row["article"] for row in self._analogs()], ["OP520", "OC90"])
        self.assertEqual([row["article"] for row in self._analogs(min_confirmations=2)], ["OP520"])

    def test_name_is_kept_and_refreshed_only_when_supplier_sends_one(self):
        self._record([{"brand": "KNECHT", "article": "OC90", "provider": "Avtoto", "name": "Фильтр масляный"}])
        self._record([{"brand": "KNECHT", "article": "OC90", "provider": "Rossko", "name": ""}])

        self.assertEqual(self._analogs()[0]["name"], "Фильтр масляный")

    def test_offers_without_article_or_brand_are_skipped(self):
        written = self._record([
            {"brand": "", "article": "OC90", "provider": "Avtoto"},
            {"brand": "KNECHT", "article": "", "provider": "Avtoto"},
            {"provider": "Avtoto"},
        ])

        self.assertEqual(written, 0)
        self.assertEqual(self._analogs(), [])

    def test_request_without_brand_is_not_recorded(self):
        # Без бренда запроса пара бессмысленна: один артикул бывает у разных
        # производителей, и связь получилась бы ложной.
        written = self.store.record(
            "", "W 712/95",
            [{"brand": "KNECHT", "article": "OC90", "provider": "Avtoto"}],
            clean_num=clean_num, brand_key=brand_key,
        )

        self.assertEqual(written, 0)

    def test_stats_report_what_is_collected(self):
        self._record([{"brand": "KNECHT", "article": "OC90", "provider": "Avtoto"}])
        self._record([{"brand": "KNECHT", "article": "OC90", "provider": "Rossko"}])
        self._record([{"brand": "FILTRON", "article": "OP520", "provider": "Avtoto"}])

        stats = self.store.stats()
        self.assertEqual(stats["pairs"], 2)
        self.assertEqual(stats["seen_total"], 3)
        self.assertEqual(stats["max_confirmations"], 2)
        self.assertEqual(stats["confirmed_pairs"], 1)

    def test_background_writer_persists_the_same_data(self):
        path = os.path.join(self.dir.name, "async.sqlite3")
        store = CrossStore(path, async_writes=True)
        try:
            store.record(
                "MANN", "W 712/95",
                [{"brand": "KNECHT", "article": "OC90", "provider": "Avtoto"}],
                clean_num=clean_num, brand_key=brand_key,
            )
            self.assertTrue(store.flush(timeout=10))
            self.assertEqual(store.stats()["pairs"], 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
