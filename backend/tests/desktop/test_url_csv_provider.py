import unittest
import tempfile
import threading
from pathlib import Path

from url_csv_provider import UrlCsvProvider


class UrlCsvProviderTests(unittest.TestCase):
    def test_headerless_profile_uses_numeric_columns_and_source_name(self):
        url = "https://example.com/headerless.csv"
        provider = UrlCsvProvider(
            urls=[url],
            url_profiles={
                url: {
                    "name": "Armtek Ростов",
                    "has_header": False,
                    "column_map": {
                        "article": "0",
                        "brand": "1",
                        "name": "2",
                        "quantity": "3",
                        "price": "4",
                        "multiplicity": "5",
                        "days": "6",
                        "warehouse": "7",
                    },
                }
            },
        )
        rows = provider._parse_csv(
            "A100\tBOSCH\tFilter\t5\t123.45\t1\t3\tРостов\n",
            provider.url_profiles[url],
        )
        provider._load_rows = lambda source_url: rows
        result = provider.get_prices("A100")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider"], "Armtek Ростов")
        self.assertEqual(result[0]["price"], 123.45)
        self.assertEqual(result[0]["warehouse"], "Ростов")

    def test_zip_content_is_unpacked(self):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("price.csv", "article;price\nA100;10\n")
        self.assertIn(b"A100;10", UrlCsvProvider._unpack_content(buffer.getvalue()))

    def test_headerless_source_uses_default_freno_positions(self):
        url = "https://example.com/armtek-rostov.csv"
        provider = UrlCsvProvider(urls=[url])
        rows = provider._parse_csv("A100\tBOSCH\tFilter\t5\t123.45\t1\t3\tРостов\n")
        provider._load_rows = lambda source_url: rows
        result = provider.get_prices("A100")
        self.assertEqual(result[0]["provider"], "Прайс Армтек Ростов")
        self.assertEqual(result[0]["quantity"], "5")

    def test_headerless_tab_price_is_not_glued_when_file_contains_commas_later(self):
        provider = UrlCsvProvider()
        good_rows = [
            f"A{i}\tBOSCH\tFilter {i}\t5\t123.45\t1\t3-4дн.\tРостов"
            for i in range(300)
        ]
        noisy_tail = [
            f"N{i}\tBOSCH\tОписание, с, запятыми {i}\t2\t99.00\t1\t1дн.\tМосква"
            for i in range(300)
        ]

        rows = provider._parse_csv("\n".join(good_rows + noisy_tail))

        self.assertEqual(rows[0]["0"], "A0")
        self.assertEqual(rows[0]["1"], "BOSCH")
        self.assertEqual(rows[0]["2"], "Filter 0")

    def test_text_delivery_range_is_parsed_as_upper_bound_days(self):
        provider = UrlCsvProvider()

        self.assertEqual(provider._parse_delivery_hours("3-4дн."), 96)
        self.assertEqual(provider._parse_delivery_hours("42 ч."), 42)

    def test_7z_content_is_unpacked(self):
        import io
        import os
        import tempfile
        import py7zr

        archive_buffer = io.BytesIO()
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "price.csv")
            with open(source, "wb") as file:
                file.write(b"article;price\nA100;10\n")
            with py7zr.SevenZipFile(archive_buffer, "w") as archive:
                archive.write(source, "price.csv")
        self.assertIn(b"A100;10", UrlCsvProvider._unpack_content(archive_buffer.getvalue()))

    def test_check_sources_reports_progress_and_skips_disabled(self):
        urls = ["https://example.com/one.csv", "https://example.com/two.csv"]
        provider = UrlCsvProvider(
            urls=urls,
            url_profiles={urls[1]: {"enabled": False}},
        )
        provider._load_rows = lambda url, refresh=False: [{"article": "A100"}]
        progress = []
        ok, _ = provider.check_sources(
            lambda current, total, source, success:
                progress.append((current, total, success))
        )
        self.assertTrue(ok)
        self.assertEqual(progress, [(0, 1, None), (1, 1, True)])

    def test_parses_quoted_field_with_embedded_newline(self):
        provider = UrlCsvProvider()
        rows = provider._parse_csv(
            'article;name;price\r\nA100;"Long\r\nname";100\r\n'
        )
        self.assertEqual(rows[0]["article"], "A100")
        self.assertEqual(rows[0]["name"], "Long\r\nname")

    def test_accepts_fields_larger_than_default_csv_limit(self):
        provider = UrlCsvProvider()
        large_name = "X" * 200000
        rows = provider._parse_csv(f"article;name\nA100;{large_name}\n")
        self.assertEqual(rows[0]["name"], large_name)

    def test_uses_configured_columns_and_default_days(self):
        provider = UrlCsvProvider(
            urls=[],
            name="Freno CSV",
            timeout=20,
            column_map={
                "article": "Артикул",
                "brand": "Бренд",
                "name": "Описание",
                "price": "Цена",
            },
            default_days=5,
        )

        item = provider._normalize_row(
            {
                "Артикул": "12345",
                "Бренд": "Bosch",
                "Описание": "Фильтр масляный",
                "Цена": "499,50",
            },
            "https://example.com/price.csv",
        )

        self.assertIsNotNone(item)
        self.assertEqual(item["article"], "12345")
        self.assertEqual(item["brand"], "Bosch")
        self.assertEqual(item["name"], "Фильтр масляный")
        self.assertEqual(item["price"], 499.5)
        self.assertEqual(item["days"], 5)

    def test_falls_back_to_aliases_when_mapping_is_missing(self):
        provider = UrlCsvProvider(urls=[], name="Freno CSV", timeout=20)
        item = provider._normalize_row(
            {
                "art": "A-100",
                "manufacturer": "MANN",
                "productname": "Фильтр",
                "cost": "999",
            },
            "https://example.com/price.csv",
        )

        self.assertIsNotNone(item)
        self.assertEqual(item["article"], "A-100")
        self.assertEqual(item["brand"], "MANN")
        self.assertEqual(item["name"], "Фильтр")
        self.assertEqual(item["price"], 999.0)

    def test_uses_profile_for_matching_url(self):
        provider = UrlCsvProvider(
            urls=["https://example.com/first.csv"],
            name="Freno CSV",
            timeout=20,
            url_profiles={
                "https://example.com/first.csv": {
                    "column_map": {"article": "Артикул", "brand": "Бренд", "name": "Описание", "price": "Цена"},
                    "default_days": 7,
                }
            },
        )
        item = provider._normalize_row(
            {
                "Артикул": "555",
                "Бренд": "Bosch",
                "Описание": "Пружина",
                "Цена": "100,25",
            },
            "https://example.com/first.csv",
        )

        self.assertIsNotNone(item)
        self.assertEqual(item["article"], "555")
        self.assertEqual(item["brand"], "Bosch")
        self.assertEqual(item["name"], "Пружина")
        self.assertEqual(item["price"], 100.25)
        self.assertEqual(item["days"], 7)

    def test_parses_freno_utf8_headers_and_quantity(self):
        provider = UrlCsvProvider()
        rows = provider._parse_csv(
            "Артикул;Производитель;Номенклатура;Остаток;Цена\n"
            "OC-47;MANN;Фильтр;12;499,50\n"
        )

        item = provider._normalize_row(rows[0], "https://example.com/price.csv")

        self.assertEqual(item["article"], "OC-47")
        self.assertEqual(item["brand"], "MANN")
        self.assertEqual(item["name"], "Фильтр")
        self.assertEqual(item["quantity"], "12")
        self.assertEqual(item["price"], 499.5)

    def test_combines_matches_and_uses_days_per_url(self):
        first_url = "https://example.com/first.csv"
        second_url = "https://example.com/second.csv"
        provider = UrlCsvProvider(
            urls=[first_url, second_url],
            url_profiles={
                first_url: {"default_days": 2},
                second_url: {"default_days": 5},
            },
        )
        rows_by_url = {
            first_url: [{"Артикул": "A-100", "Цена": "100"}],
            second_url: [{"Артикул": "A100", "Цена": "110"}],
        }
        provider._load_rows = lambda url: rows_by_url[url]

        items = provider.get_prices("A100")

        self.assertEqual(len(items), 2)
        self.assertEqual([item["days"] for item in items], [2, 5])
        self.assertEqual([item["source_url"] for item in items], [first_url, second_url])

    def test_builds_article_index_once_for_main_and_cross_searches(self):
        url = "https://example.com/large.csv"
        provider = UrlCsvProvider(urls=[url])
        calls = []

        def load_rows(source_url):
            calls.append(source_url)
            return [
                {"article": "MAIN", "price": "100"},
                {"article": "CROSS1", "price": "110"},
                {"article": "CROSS2", "price": "120"},
            ]

        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            provider._load_rows = load_rows

            self.assertEqual(len(provider.get_prices("MAIN")), 1)
            self.assertEqual(len(provider.get_prices("CROSS1")), 1)
            self.assertEqual(len(provider.get_prices("CROSS2")), 1)
            self.assertEqual(calls, [url])

    def test_reuses_persistent_article_index_after_provider_recreation(self):
        url = "https://example.com/persistent.csv"
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / (
                __import__("hashlib").sha256(url.encode("utf-8")).hexdigest() + ".csv"
            )
            cache_file.write_text("article;price\nA100;10\n", encoding="utf-8")

            first = UrlCsvProvider(urls=[url])
            first.cache_dir = directory
            self.assertEqual(len(first.get_prices("A100")), 1)

            second = UrlCsvProvider(urls=[url])
            second.cache_dir = directory
            second._load_rows = lambda source_url: self.fail(
                "CSV should not be parsed when the persistent index is valid"
            )
            self.assertEqual(len(second.get_prices("A100")), 1)

    def test_decodes_utf8_bom_before_parsing(self):
        class Response:
            content = "\ufeffАртикул;Цена\nA-100;100\n".encode("utf-8")
            text = "искажённый текст"

        self.assertEqual(
            UrlCsvProvider._decode_response(Response()),
            "Артикул;Цена\nA-100;100\n",
        )

    def test_search_uses_saved_csv_cache(self):
        url = "https://example.com/price.csv"
        provider = UrlCsvProvider(urls=[url])
        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            Path(provider._cache_path(url)).write_bytes(
                "\ufeffАртикул;Производитель;Номенклатура;Остаток;Цена\nA-100;MANN;Фильтр;2;700\n".encode("utf-8")
            )
            items = provider.get_prices("A100")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["price"], 700.0)

    def test_batch_price_search_uses_single_sqlite_index_for_many_articles(self):
        url = "https://example.com/price.csv"
        provider = UrlCsvProvider(urls=[url])
        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            Path(provider._cache_path(url)).write_bytes(
                "\ufeffАртикул;Производитель;Номенклатура;Остаток;Цена\n"
                "A-100;MANN;Фильтр;2;700\n"
                "B-200;BOSCH;Свеча;3;800\n"
                "C-300;LYNX;Колодки;4;900\n".encode("utf-8")
            )

            found = provider.get_prices_many(["A100", "B200", "missing"])

        self.assertEqual([item["article"] for item in found["A100"]], ["A-100"])
        self.assertEqual([item["article"] for item in found["B200"]], ["B-200"])
        self.assertEqual(found["MISSING"], [])

    def test_local_cache_search_finds_text_terms_in_sqlite_index(self):
        url = "https://example.com/price.csv"
        provider = UrlCsvProvider(urls=[url])
        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            Path(provider._cache_path(url)).write_bytes(
                "\ufeffАртикул;Производитель;Номенклатура;Остаток;Цена\n"
                "A-100;MANN;Фильтр масляный двигателя;2;700\n"
                "B-200;LYNX;Фильтр топливный;3;800\n".encode("utf-8")
            )

            provider.get_prices("A100")
            items = provider.search_local_cache("масл, фильтр топливный")

        self.assertEqual({item["article"] for item in items}, {"A-100", "B-200"})

    def test_local_cache_search_does_not_download_missing_cache(self):
        url = "https://example.com/missing.csv"
        provider = UrlCsvProvider(urls=[url])
        provider._download_content = lambda source_url: self.fail("local search must not download prices")
        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            items = provider.search_local_cache("масл")

        self.assertEqual(items, [])
        self.assertIn("обновите прайсы", provider.last_message)

    def test_suggest_articles_uses_saved_sqlite_index_without_download(self):
        url = "https://example.com/price.csv"
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / (
                __import__("hashlib").sha256(url.encode("utf-8")).hexdigest() + ".csv"
            )
            cache_path.write_bytes(
                "\ufeffАртикул;Производитель;Номенклатура;Остаток;Цена\n"
                "A-100;MANN;Фильтр масляный;2;700\n".encode("utf-8")
            )
            first = UrlCsvProvider(urls=[url])
            first.cache_dir = directory
            self.assertEqual(len(first.get_prices("A100")), 1)

            second = UrlCsvProvider(urls=[url])
            second.cache_dir = directory
            second._download_content = lambda source_url: self.fail("suggestions must not download prices")
            second._load_cached_rows = lambda source_url: self.fail("suggestions must not reindex prices")
            suggestions = second.suggest_articles("A10")

        self.assertEqual(suggestions[0]["article"], "A-100")
        self.assertEqual(suggestions[0]["brand"], "MANN")
        self.assertEqual(suggestions[0]["offer_count"], 1)

    def test_suggest_articles_groups_brands_by_normalized_article(self):
        url = "https://example.com/price.csv"
        provider = UrlCsvProvider(urls=[url])
        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            Path(provider._cache_path(url)).write_bytes(
                "\ufeffАртикул;Производитель;Номенклатура;Остаток;Цена\n"
                "A-100;MANN;Фильтр масляный;2;700\n"
                "A100;BOSCH;Фильтр масляный;3;720\n"
                "A101;LYNX;Фильтр топливный;1;900\n".encode("utf-8")
            )
            provider.get_prices("A100")
            suggestions = provider.suggest_articles("A10")

        first = suggestions[0]
        self.assertEqual(first["article"], "A-100")
        self.assertEqual(first["offer_count"], 2)
        self.assertIn("MANN", first["brand"])
        self.assertIn("BOSCH", first["brand"])

    def test_local_cache_search_does_not_reindex_stale_sources(self):
        url = "https://example.com/price.csv"
        provider = UrlCsvProvider(urls=[url])
        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            cache_path = Path(provider._cache_path(url))
            cache_path.write_bytes("article;price;name\nA100;10;Old\n".encode("utf-8"))
            self.assertEqual(provider.get_prices("A100")[0]["name"], "Old")
            cache_path.write_bytes("article;price;name\nA100;20;New\n".encode("utf-8"))

            second = UrlCsvProvider(urls=[url])
            second.cache_dir = directory
            second._load_cached_rows = lambda source_url: self.fail("local UI search must not reindex")
            items = second.search_local_cache("A100")

        self.assertEqual(items[0]["price"], 10.0)
        self.assertEqual(items[0]["name"], "Old")

    def test_brand_candidates_use_saved_csv_cache(self):
        url = "https://example.com/price.csv"
        provider = UrlCsvProvider(urls=[url])
        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            Path(provider._cache_path(url)).write_bytes(
                "\ufeffАртикул;Производитель;Номенклатура;Остаток;Цена\n"
                "OC-90;KNECHT;Фильтр масляный;2;700\n"
                "OC90;MAHLE;Фильтр масляный;1;710\n".encode("utf-8")
            )
            candidates = provider.get_brand_candidates("OC90")

        self.assertEqual([item["brand"] for item in candidates], ["KNECHT", "MAHLE"])
        self.assertEqual(candidates[0]["article"], "OC-90")
        self.assertEqual(candidates[0]["source"], "CSV локальный прайс")

    def test_runtime_refresh_disabled_does_not_download_during_price_search(self):
        url = "https://example.com/slow-price.csv"
        provider = UrlCsvProvider(urls=[url], runtime_refresh=False)
        provider._download_content = lambda source_url: self.fail("price search must not refresh CSV")
        with tempfile.TemporaryDirectory() as directory:
            provider.cache_dir = directory
            result = provider.get_prices("OC90")

        self.assertEqual(result, [])
        self.assertIn("нет данных", provider.last_message)

    def test_runtime_refresh_disabled_does_not_wait_for_busy_index(self):
        url = "https://example.com/price.csv"
        provider = UrlCsvProvider(urls=[url], runtime_refresh=False)
        provider._index_lock.acquire()
        released = threading.Event()
        try:
            result = provider.get_brand_candidates("OC90")
        finally:
            provider._index_lock.release()
            released.set()

        self.assertTrue(released.is_set())
        self.assertEqual(result, [])
        self.assertIn("занята обновлением", provider.last_message)


if __name__ == "__main__":
    unittest.main()
