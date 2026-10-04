import json
import tempfile
import unittest
from pathlib import Path

from article_suggestions import (
    ArticleSuggestionEngine,
    article_from_suggestion_label,
    format_suggestion_label,
)


class FakeCsvProvider:
    def __init__(self, suggestions):
        self.suggestions = suggestions

    def suggest_articles(self, query, limit=10):
        return self.suggestions[:limit]


class ArticleSuggestionTests(unittest.TestCase):
    def test_returns_history_order_and_csv_suggestions(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.json"
            history_path.write_text(json.dumps(["OC90", "A-100"]), encoding="utf-8")
            engine = ArticleSuggestionEngine(str(history_path))
            engine.refresh_orders([
                {
                    "groups": [
                        {
                            "requested": {
                                "brand": "KNECHT",
                                "article": "OC-90",
                                "name": "Фильтр масляный",
                            }
                        }
                    ],
                    "items": [
                        {
                            "display_brand": "MAHLE",
                            "article": "OC 90",
                            "snapshot": {"name": "Фильтр"},
                        }
                    ],
                }
            ])
            provider = FakeCsvProvider([
                {
                    "article": "OC90",
                    "brand": "KNECHT/MAHLE",
                    "name": "Фильтр масляный",
                    "source": "локальный прайс",
                    "offer_count": 4,
                }
            ])

            suggestions = engine.suggest("oc9", csv_providers=[provider], limit=10)

        self.assertGreaterEqual(len(suggestions), 3)
        self.assertEqual(suggestions[0]["article"], "OC90")
        self.assertIn("KNECHT", {item.get("brand") for item in suggestions})

    def test_short_query_does_not_return_noise(self):
        engine = ArticleSuggestionEngine("")

        self.assertEqual(engine.suggest("oc"), [])

    def test_formats_and_extracts_article_from_label(self):
        label = format_suggestion_label({
            "article": "OC-90",
            "brand": "KNECHT",
            "name": "Фильтр масляный",
            "source": "локальный прайс",
            "offer_count": 2,
        })

        self.assertEqual(article_from_suggestion_label(label), "OC-90")
        self.assertIn("локальный прайс: 2", label)


if __name__ == "__main__":
    unittest.main()
