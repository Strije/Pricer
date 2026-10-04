import os
import tempfile
import unittest

from abcp_reference import AbcpReferenceProvider


class AbcpReferenceProviderTests(unittest.TestCase):
    def test_hashes_plain_password_and_keeps_md5_password(self):
        plain = AbcpReferenceProvider("example.public.api.abcp.ru", "user", "secret")
        hashed = AbcpReferenceProvider(
            "https://example.public.api.abcp.ru/",
            "user",
            "5ebe2294ecd0e0f08eab7690d2a6ee69",
        )

        self.assertEqual(
            plain._auth_params({})["userpsw"],
            "5ebe2294ecd0e0f08eab7690d2a6ee69",
        )
        self.assertEqual(
            hashed._auth_params({})["userpsw"],
            "5ebe2294ecd0e0f08eab7690d2a6ee69",
        )
        self.assertEqual(plain.host, "https://example.public.api.abcp.ru")
        self.assertEqual(hashed.host, "https://example.public.api.abcp.ru")

    def test_brand_candidates_from_search_brands(self):
        provider = AbcpReferenceProvider("https://example.public.api.abcp.ru", "user", "secret")
        provider._get = lambda path, params: [
            {
                "brand": "FEBI",
                "number": "01089",
                "numberFix": "01089",
                "description": "Антифриз",
            }
        ]

        candidates = provider.get_brand_candidates("01089")

        self.assertEqual(candidates[0]["brand"], "FEBI")
        self.assertEqual(candidates[0]["article"], "01089")
        self.assertEqual(candidates[0]["name"], "Антифриз")
        self.assertEqual(candidates[0]["source"], "ABCP search/brands")

    def test_cross_targets_and_image_urls_from_articles_info(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = AbcpReferenceProvider(
                "https://example.public.api.abcp.ru",
                "user",
                "secret",
                allow_articles_info=True,
            )
            provider._state_path = os.path.join(temp_dir, "abcp_state.json")
            provider._get = lambda path, params: {
                "brand": "MERCEDES-BENZ",
                "number": "A1130940048",
                "images": [{"name": "main.jpeg"}],
                "crosses": [
                    {
                        "brand": "ASHUKI",
                        "number": "116617A",
                        "numberFix": "116617A",
                        "crossType": 1,
                        "images": [{"name": "cross.jpeg"}],
                    }
                ],
            }

            crosses = provider.get_cross_targets("A1130940048", "MERCEDES-BENZ")
            images = provider.get_images("A1130940048", "MERCEDES-BENZ")

        self.assertEqual(crosses[0]["brand"], "ASHUKI")
        self.assertEqual(crosses[0]["article"], "116617A")
        self.assertEqual(crosses[0]["relation"], "замена")
        self.assertEqual(
            crosses[0]["image_urls"],
            ["https://imgcdn.abcp.ru/p/cross.jpeg"],
        )
        self.assertEqual(images, ["https://imgcdn.abcp.ru/p/main.jpeg"])

    def test_articles_info_is_disabled_by_default(self):
        provider = AbcpReferenceProvider(
            "https://example.public.api.abcp.ru",
            "user",
            "secret",
            include_crosses=True,
            include_images=True,
        )
        calls = []
        provider._get = lambda path, params: calls.append(path) or {}

        self.assertEqual(provider.get_cross_targets("NOINFO123", "TESTBRAND"), [])
        self.assertEqual(calls, [])
        self.assertIn("articles/info отключ", provider.last_message)

    def test_articles_info_cache_and_daily_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = AbcpReferenceProvider(
                "https://example.public.api.abcp.ru",
                "user",
                "secret",
                include_crosses=True,
                include_images=False,
                allow_articles_info=True,
                articles_info_daily_limit=1,
            )
            provider._state_path = os.path.join(temp_dir, "abcp_state.json")
            calls = []

            def fake_get(path, params):
                calls.append((path, params))
                return {
                    "crosses": [
                        {
                            "brand": "BRAND2",
                            "number": "A-200",
                            "crossType": 1,
                        }
                    ]
                }

            provider._get = fake_get

            first = provider.get_cross_targets("A100", "BRAND1")
            second = provider.get_cross_targets("A100", "BRAND1")
            blocked = provider.get_cross_targets("A101", "BRAND1")

        self.assertEqual(first[0]["brand"], "BRAND2")
        self.assertEqual(second[0]["article"], "A-200")
        self.assertEqual(blocked, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "articles/info")
        self.assertIn("дневной лимит", provider.last_message)


if __name__ == "__main__":
    unittest.main()
