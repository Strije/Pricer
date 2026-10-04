import unittest

from brand_resolver import BrandResolver


class FakeBrandAliases:
    group_titles = {"mahlegroup": "MAHLE"}

    def key(self, brand):
        clean = "".join(char for char in str(brand or "").upper() if char.isalnum())
        if clean in {"KNECHT", "MAHLE", "MAHLEKNECHT"}:
            return "mahlegroup"
        return clean


class BrandResolverTests(unittest.TestCase):
    def test_groups_supplier_spellings_and_keeps_provider_names(self):
        resolver = BrandResolver(FakeBrandAliases())
        candidates = []
        candidates.extend(resolver.normalize_candidates(
            [{"BRAND": "KNECHT", "PIN": "OC90", "NAME": "Фильтр масляный"}],
            requested_article="OC90",
            provider="Armtek",
            provider_class="ArmtekProvider",
        ))
        candidates.extend(resolver.normalize_candidates(
            [{"brand": "MAHLE", "article": "OC 90", "name": "Oil filter"}],
            requested_article="OC90",
            provider="ABSTD",
            provider_class="AbstdProvider",
        ))

        choices = resolver.build_choices(candidates, "OC90")

        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].canonical_brand, "MAHLE")
        self.assertEqual(choices[0].provider_brands["ArmtekProvider"], "KNECHT")
        self.assertEqual(choices[0].provider_brands["AbstdProvider"], "MAHLE")
        self.assertIn("OC90", choices[0].label)

    def test_marks_article_mismatch_as_analog_choice(self):
        resolver = BrandResolver(FakeBrandAliases())
        candidates = resolver.normalize_candidates(
            [{"brand": "BOSCH", "article": "A100", "name": "Analog item"}],
            requested_article="OC90",
            provider="Mikado",
            provider_class="MikadoProvider",
        )

        choices = resolver.build_choices(candidates, "OC90")

        self.assertEqual(len(choices), 1)
        self.assertTrue(choices[0].candidates[0].is_cross)
        self.assertIn("аналог", choices[0].label)


if __name__ == "__main__":
    unittest.main()
