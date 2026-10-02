import re
from dataclasses import dataclass, field


def article_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _clean_text(value):
    return " ".join(str(value or "").replace("\\", " ").split())


def _truthy(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "analog", "analogproduct", "analogonorderproduct"}


def _shorten(value, limit):
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


@dataclass
class BrandCandidate:
    provider: str
    provider_class: str
    brand: str
    article: str = ""
    name: str = ""
    source: str = ""
    is_cross: bool = False
    order_code: str = ""
    raw: object = field(default=None, repr=False, compare=False)

    def is_exact_article(self, requested_article):
        current = article_key(self.article)
        requested = article_key(requested_article)
        return bool(current and requested and current == requested)


@dataclass
class BrandChoice:
    label: str
    canonical_brand: str
    provider_brands: dict
    candidates: list
    group_key: str


class BrandResolver:
    BRAND_KEYS = (
        "brand", "Brand", "BRAND",
        "brandName", "BrandName", "BRANDNAME",
        "manufacturer", "Manufacturer", "MANUFACTURER",
        "producer", "Producer", "PRODUCER",
        "ProducerBrand", "PRODUCERBRAND",
        "Manuf", "MANUF", "MakeName",
    )
    ARTICLE_KEYS = (
        "article", "Article", "ARTICLE",
        "number", "Number", "NUMBER",
        "code", "Code", "CODE",
        "art", "Art", "ART",
        "nr", "NR",
        "PIN", "ProducerCode", "PRODUCERCODE",
        "PartNumber", "partnumber", "productCode", "displayProductCode",
        "SearchCode",
    )
    NAME_KEYS = (
        "productName", "ProductName",
        "name", "Name", "NAME",
        "description", "Description", "DESCRIPTION",
        "tovname", "TovName",
    )
    SOURCE_KEYS = ("source", "Source", "SOURCE", "method", "Method")
    ORDER_CODE_KEYS = ("order_code", "ZakazCode", "zakaz_code", "orderCode")
    CROSS_KEYS = ("is_cross", "isCross", "IsCross", "cross", "Cross", "analog", "Analog")

    def __init__(self, brand_aliases):
        self.brand_aliases = brand_aliases

    def supports(self, provider):
        return callable(getattr(provider, "get_brand_candidates", None)) or callable(
            getattr(provider, "get_brands", None)
        )

    def collect_from_provider(self, provider, display_name, requested_article):
        method = getattr(provider, "get_brand_candidates", None)
        raw_items = method(requested_article) if callable(method) else provider.get_brands(requested_article)
        return self.normalize_candidates(
            raw_items,
            requested_article=requested_article,
            provider=display_name,
            provider_class=provider.__class__.__name__,
        )

    def normalize_candidates(self, raw_items, requested_article="", provider="", provider_class=""):
        candidates = []
        seen = set()
        for item in self._flatten_items(raw_items):
            candidate = self._candidate_from_raw(
                item,
                requested_article=requested_article,
                provider=provider,
                provider_class=provider_class,
            )
            if not candidate:
                continue
            marker = (
                candidate.provider_class,
                self._brand_key(candidate.brand),
                article_key(candidate.article),
                _clean_text(candidate.name).upper(),
                bool(candidate.is_cross),
            )
            if marker in seen:
                continue
            seen.add(marker)
            candidates.append(candidate)
        return candidates

    def build_choices(self, candidates, requested_article):
        grouped = {}
        for candidate in candidates or []:
            brand = _clean_text(candidate.brand)
            if not brand:
                continue
            group = self._brand_key(brand)
            if not group:
                continue
            grouped.setdefault(group, []).append(candidate)

        choices = []
        used_labels = set()
        for group, group_candidates in grouped.items():
            ordered = sorted(
                group_candidates,
                key=lambda item: self._candidate_priority(item, requested_article),
            )
            aliases = self._unique(item.brand for item in ordered)
            title = self.brand_aliases.group_titles.get(group) or min(
                aliases,
                key=lambda value: (len(self._brand_key(value)), self._brand_key(value)),
            )
            provider_brands = self._provider_brands(ordered, requested_article)
            label = self._choice_label(title, ordered, requested_article)
            base_label = label
            suffix = 2
            while label in used_labels:
                label = f"{base_label} ({suffix})"
                suffix += 1
            used_labels.add(label)
            choices.append(
                BrandChoice(
                    label=label,
                    canonical_brand=title,
                    provider_brands=provider_brands,
                    candidates=ordered,
                    group_key=group,
                )
            )

        choices.sort(key=lambda choice: self._choice_priority(choice, requested_article))
        return choices

    def _candidate_from_raw(self, item, requested_article, provider, provider_class):
        if isinstance(item, str):
            brand = _clean_text(item)
            if not brand:
                return None
            return BrandCandidate(
                provider=provider,
                provider_class=provider_class,
                brand=brand,
                article=str(requested_article or ""),
                raw=item,
            )
        if not isinstance(item, dict):
            return None

        brand = self._first_value(item, self.BRAND_KEYS)
        if not brand:
            name_value = self._first_value(item, ("name", "Name"))
            if name_value and not self._has_any(item, self.ARTICLE_KEYS):
                brand = name_value
        brand = _clean_text(brand)
        if not brand:
            return None

        article = _clean_text(self._first_value(item, self.ARTICLE_KEYS) or requested_article)
        name = _clean_text(self._first_value(item, self.NAME_KEYS))
        source = _clean_text(self._first_value(item, self.SOURCE_KEYS))
        order_code = _clean_text(self._first_value(item, self.ORDER_CODE_KEYS))
        code_type = _clean_text(self._first_value(item, ("CodeType", "code_type", "offeringBlockType")))
        is_cross = any(_truthy(self._first_value(item, (key,))) for key in self.CROSS_KEYS)
        if code_type.lower() in {"analog", "analogoem", "analogproduct", "analogonorderproduct"}:
            is_cross = True
        if article and requested_article and article_key(article) != article_key(requested_article):
            is_cross = True

        return BrandCandidate(
            provider=provider,
            provider_class=provider_class,
            brand=brand,
            article=article,
            name=name,
            source=source,
            is_cross=is_cross,
            order_code=order_code,
            raw=item,
        )

    def _provider_brands(self, candidates, requested_article):
        result = {}
        for candidate in candidates:
            current = result.get(candidate.provider_class)
            if current is None:
                result[candidate.provider_class] = candidate.brand
                continue
            current_candidate = next(
                (item for item in candidates if item.provider_class == candidate.provider_class and item.brand == current),
                None,
            )
            if current_candidate is None or (
                self._candidate_priority(candidate, requested_article)
                < self._candidate_priority(current_candidate, requested_article)
            ):
                result[candidate.provider_class] = candidate.brand
        return result

    def _choice_label(self, title, candidates, requested_article):
        preferred = [
            item for item in candidates
            if item.is_exact_article(requested_article) and not item.is_cross
        ] or [
            item for item in candidates
            if item.is_exact_article(requested_article)
        ] or candidates

        articles = self._unique(item.article for item in preferred if item.article)
        names = self._unique(item.name for item in preferred if item.name)
        providers = self._unique(item.provider for item in candidates if item.provider)

        parts = [str(title or "").strip()]
        if articles:
            parts.append(", ".join(articles[:2]))
        if names:
            parts.append(_shorten(names[0], 58))
        label = " - ".join(part for part in parts if part)

        suffix_parts = []
        if providers:
            suffix_parts.append(", ".join(providers[:3]) + (" +" if len(providers) > 3 else ""))
        if preferred and all(item.is_cross for item in preferred):
            suffix_parts.append("аналог")
        if suffix_parts:
            label += f" ({'; '.join(suffix_parts)})"
        return _shorten(label, 120)

    def _choice_priority(self, choice, requested_article):
        exact = any(item.is_exact_article(requested_article) and not item.is_cross for item in choice.candidates)
        exact_any = any(item.is_exact_article(requested_article) for item in choice.candidates)
        providers = {item.provider_class for item in choice.candidates}
        return (
            0 if exact else 1 if exact_any else 2,
            -len(providers),
            self._brand_key(choice.canonical_brand),
        )

    def _candidate_priority(self, candidate, requested_article):
        exact = candidate.is_exact_article(requested_article)
        return (
            0 if exact and not candidate.is_cross else 1 if exact else 2,
            len(self._brand_key(candidate.brand)),
            self._brand_key(candidate.brand),
            _clean_text(candidate.name).upper(),
        )

    def _brand_key(self, brand):
        return self.brand_aliases.key(brand) if self.brand_aliases else re.sub(
            r"[^A-ZА-ЯЁ0-9]", "", str(brand or "").upper()
        )

    def _first_value(self, item, keys):
        for key in keys:
            if key in item and item[key] not in (None, ""):
                return item[key]
        return ""

    def _has_any(self, item, keys):
        return any(key in item and item[key] not in (None, "") for key in keys)

    def _flatten_items(self, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return list(value)
        if isinstance(value, dict):
            for key in (
                "brands", "Brands", "items", "Items", "data", "Data",
                "result", "Result", "rows", "Rows", "list", "List",
                "goods", "Goods", "RESP", "ARRAY", "return",
            ):
                nested = value.get(key)
                if nested:
                    return self._flatten_items(nested)
            return [value]
        return [value]

    def _unique(self, values):
        result = []
        seen = set()
        for value in values:
            text = _clean_text(value)
            marker = text.upper()
            if not text or marker in seen:
                continue
            seen.add(marker)
            result.append(text)
        return result
