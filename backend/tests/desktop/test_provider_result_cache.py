import unittest

from provider_result_cache import ProviderResultCache


class ProviderResultCacheTests(unittest.TestCase):
    def test_returns_deep_copy(self):
        now = [10.0]
        cache = ProviderResultCache(ttl_seconds=60, max_entries=10, clock=lambda: now[0])
        cache.set(("offers", "A"), [{"article": "OC90", "price": 100}])

        hit, first, _age = cache.get(("offers", "A"))
        first[0]["price"] = 999
        hit_again, second, _age = cache.get(("offers", "A"))

        self.assertTrue(hit)
        self.assertTrue(hit_again)
        self.assertEqual(second[0]["price"], 100)

    def test_expires_by_ttl(self):
        now = [10.0]
        cache = ProviderResultCache(ttl_seconds=5, max_entries=10, clock=lambda: now[0])
        cache.set(("offers", "A"), [{"article": "OC90"}])

        now[0] = 16.0
        hit, value, _age = cache.get(("offers", "A"))

        self.assertFalse(hit)
        self.assertIsNone(value)

    def test_evicts_least_recently_used_entry(self):
        now = [10.0]
        cache = ProviderResultCache(ttl_seconds=60, max_entries=2, clock=lambda: now[0])
        cache.set("A", [1])
        now[0] += 1
        cache.set("B", [2])
        cache.get("A")
        now[0] += 1
        cache.set("C", [3])

        self.assertTrue(cache.get("A")[0])
        self.assertFalse(cache.get("B")[0])
        self.assertTrue(cache.get("C")[0])

    def test_disabled_cache_does_not_store(self):
        cache = ProviderResultCache(ttl_seconds=0, max_entries=10)

        self.assertFalse(cache.set("A", [1]))
        self.assertFalse(cache.get("A")[0])


if __name__ == "__main__":
    unittest.main()
