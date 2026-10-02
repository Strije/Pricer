import json
import os
import re
import threading


def clean_article(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def article_from_suggestion_label(label):
    return str(label or "").split(" | ", 1)[0].strip()


def format_suggestion_label(suggestion):
    article = str(suggestion.get("article") or "").strip()
    if not article:
        return ""
    parts = [article]
    brand = str(suggestion.get("brand") or "").strip()
    name = str(suggestion.get("name") or "").strip()
    source = str(suggestion.get("source") or "").strip()
    offer_count = int(suggestion.get("offer_count") or 0)
    if brand:
        parts.append(_clip(brand, 32))
    if name:
        parts.append(_clip(name, 58))
    if source:
        if offer_count > 1:
            source = f"{source}: {offer_count}"
        parts.append(source)
    return " | ".join(parts)


class ArticleSuggestionEngine:
    def __init__(self, history_path="", clean_article_func=None, max_order_suggestions=700):
        self.history_path = str(history_path or "")
        self.clean_article = clean_article_func or clean_article
        self.max_order_suggestions = max(50, int(max_order_suggestions or 700))
        self._lock = threading.Lock()
        self._order_suggestions = []

    def refresh_orders(self, orders):
        suggestions = []
        seen = set()
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            for suggestion in self._order_suggestions_from_order(order):
                key = self._dedupe_key(suggestion)
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append(suggestion)
                if len(suggestions) >= self.max_order_suggestions:
                    break
            if len(suggestions) >= self.max_order_suggestions:
                break
        with self._lock:
            self._order_suggestions = suggestions

    def suggest(self, query, csv_providers=None, limit=10):
        query = str(query or "").strip()
        query_key = self.clean_article(query)
        if len(query_key) < 3:
            return []
        limit = max(1, int(limit or 10))
        result = []
        seen = set()
        for suggestion in self._history_suggestions(query_key):
            self._append_match(result, seen, suggestion, query, query_key, limit)
            if len(result) >= limit:
                return result
        with self._lock:
            cached_orders = list(self._order_suggestions)
        for suggestion in cached_orders:
            self._append_match(result, seen, suggestion, query, query_key, limit)
            if len(result) >= limit:
                return result
        for provider in csv_providers or []:
            if len(result) >= limit:
                break
            try:
                provider_items = provider.suggest_articles(query, limit=limit - len(result))
            except Exception:
                provider_items = []
            for suggestion in provider_items or []:
                suggestion = dict(suggestion or {})
                suggestion.setdefault("source", "локальный прайс")
                self._append_match(result, seen, suggestion, query, query_key, limit, assume_match=True)
                if len(result) >= limit:
                    break
        return result

    def labels(self, query, csv_providers=None, limit=10):
        return [
            label
            for label in (format_suggestion_label(item) for item in self.suggest(query, csv_providers, limit))
            if label
        ]

    def _history_suggestions(self, query_key):
        items = []
        if not self.history_path or not os.path.exists(self.history_path):
            return items
        try:
            with open(self.history_path, encoding="utf-8") as file:
                raw_items = json.load(file)
        except Exception:
            return items
        if not isinstance(raw_items, list):
            return items
        for article in raw_items[:30]:
            article = str(article or "").strip()
            if not article:
                continue
            article_key = self.clean_article(article)
            if article_key.startswith(query_key) or query_key in article_key:
                items.append({
                    "article": article,
                    "brand": "",
                    "name": "",
                    "source": "история",
                    "source_rank": 0,
                })
        return items

    def _append_match(self, result, seen, suggestion, query, query_key, limit, assume_match=False):
        if len(result) >= limit:
            return
        article = str(suggestion.get("article") or "").strip()
        if not article:
            return
        article_key = self.clean_article(article)
        if not article_key:
            return
        if not assume_match and not self._matches(suggestion, query, query_key, article_key):
            return
        key = self._dedupe_key(suggestion, article_key=article_key)
        if key in seen:
            return
        seen.add(key)
        result.append(suggestion)

    def _matches(self, suggestion, query, query_key, article_key):
        if article_key.startswith(query_key) or query_key in article_key:
            return True
        text = " ".join(
            str(suggestion.get(key) or "")
            for key in ("article", "brand", "name", "source")
        ).lower()
        return str(query or "").strip().lower() in text

    def _dedupe_key(self, suggestion, article_key=None):
        article_key = article_key or self.clean_article(suggestion.get("article"))
        brand_key = re.sub(r"[^A-Z0-9]+", "", str(suggestion.get("brand") or "").upper())
        return article_key, brand_key

    def _order_suggestions_from_order(self, order):
        for group in order.get("groups") or []:
            if not isinstance(group, dict):
                continue
            requested = group.get("requested") or {}
            yield self._suggestion(
                requested.get("article"),
                requested.get("brand"),
                requested.get("name"),
                "подбор",
            )
        for item in order.get("items") or []:
            if not isinstance(item, dict):
                continue
            search = item.get("search") or {}
            yield self._suggestion(
                search.get("requested_article"),
                search.get("selected_brand"),
                search.get("requested_name"),
                "подбор",
            )
            snapshot = item.get("snapshot") or {}
            yield self._suggestion(
                item.get("article"),
                item.get("display_brand") or item.get("brand"),
                item.get("name") or snapshot.get("name"),
                "заказы",
            )

    @staticmethod
    def _suggestion(article, brand="", name="", source=""):
        return {
            "article": str(article or "").strip(),
            "brand": str(brand or "").strip(),
            "name": str(name or "").strip(),
            "source": str(source or "").strip(),
        }


def _clip(value, max_length):
    value = " ".join(str(value or "").split())
    max_length = max(4, int(max_length or 4))
    if len(value) <= max_length:
        return value
    return value[:max_length - 3].rstrip() + "..."
