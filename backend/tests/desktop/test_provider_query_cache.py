import re
import unittest

from ._compat import SkitchenApp
from provider_result_cache import ProviderResultCache


class _Aliases:
    def for_provider(self, brand, _provider):
        return brand

    def key(self, value):
        return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


class _FakeProvider:
    timeout = 3

    def __init__(self):
        self.calls = 0
        self.last_message = ""

    def get_prices(self, article):
        self.calls += 1
        return [{"provider": "Fake", "article": article, "brand": "MANN", "price": 100}]


class _PendingProvider(_FakeProvider):
    def get_prices(self, article):
        self.calls += 1
        self.last_message = "поиск в обработке"
        return [{"provider": "Fake", "article": article, "brand": "MANN", "price": 100}]


class _AvtotoLikeProvider:
    timeout = 3

    def __init__(self):
        self.calls = []
        self.last_message = ""

    def get_prices(self, article, brand="", include_crosses=None):
        self.calls.append((article, brand, include_crosses))
        return [{"provider": "Avtoto", "article": article, "brand": brand, "price": 100}]


class ProviderQueryCacheTests(unittest.TestCase):
    def _app(self):
        app = SkitchenApp.__new__(SkitchenApp)
        app.brand_aliases = _Aliases()
        app.provider_result_cache = ProviderResultCache(ttl_seconds=300, max_entries=20)
        app._selected_brand_variants = {}
        app._detail_log = lambda *args, **kwargs: None
        return app

    def test_query_provider_reuses_cached_successful_result(self):
        app = self._app()
        provider = _FakeProvider()

        first, _elapsed, first_cache_hit = app._query_provider(
            provider,
            provider.__class__.__name__,
            "OC-90",
            "OC90",
            "MANN",
        )
        first[0]["price"] = 999
        second, _elapsed, second_cache_hit = app._query_provider(
            provider,
            provider.__class__.__name__,
            "OC-90",
            "OC90",
            "MANN",
        )

        self.assertFalse(first_cache_hit)
        self.assertTrue(second_cache_hit)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(second[0]["price"], 100)

    def test_query_provider_does_not_cache_pending_result(self):
        app = self._app()
        provider = _PendingProvider()

        app._query_provider(
            provider,
            provider.__class__.__name__,
            "OC90",
            "OC90",
            "MANN",
        )
        _second, _elapsed, second_cache_hit = app._query_provider(
            provider,
            provider.__class__.__name__,
            "OC90",
            "OC90",
            "MANN",
        )

        self.assertFalse(second_cache_hit)
        self.assertEqual(provider.calls, 2)

    def test_avtoto_query_provider_passes_brand_and_separates_cross_cache(self):
        app = self._app()
        provider = _AvtotoLikeProvider()

        app._query_provider(
            provider,
            "AvtotoProvider",
            "MF-114",
            "MF114",
            "MAXI FRESH",
            include_crosses=False,
        )
        app._query_provider(
            provider,
            "AvtotoProvider",
            "MF-114",
            "MF114",
            "MAXI FRESH",
            include_crosses=True,
        )

        self.assertEqual(
            provider.calls,
            [("MF114", "MAXI FRESH", False), ("MF114", "MAXI FRESH", True)],
        )


if __name__ == "__main__":
    unittest.main()
