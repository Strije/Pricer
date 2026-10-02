import html
import json
import os
import re
import shutil
import sys
import threading

from config_path import get_config_dir


def get_brand_storage_dir():
    """Keep user-editable brand data beside the script/executable."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_bundled_resource_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "resources")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")


def get_custom_groups_path():
    return os.path.join(get_brand_storage_dir(), "brand_groups.json")


def get_provider_mappings_path():
    return os.path.join(get_brand_storage_dir(), "brand_provider_mappings.json")


MANUAL_FAMILIES = [
    ("MANN", "MANN-FILTER", "MANN FILTER", "MANN+HUMMEL"),
    ("BMW", "BMW AG"),
    ("MAHLE", "KNECHT", "KNECHT FILTER", "MAHLE/KNECHT", "MAHLE KNECHT", "BEHR", "MAHLE BEHR", "BEHRMAHLE"),
    ("HYUNDAI", "KIA", "MOBIS", "HYUNDAI/KIA", "HYUNDAI/KIA/MOBIS", "KIA-HYUNDAI", "MOBIS/KIA/HYUNDAI"),
    (
        "RENAULT", "RENAU", "RENAULT GROUP", "RENAULT ORIJINAL", "MAIS",
        "OE RENAULT", "RVI", "RENAULT/DACIA", "RENAULT S.A.",
        "RENAULT, RENAULT KOREA", "RENAULT KOREA", "RENAULT-SAMSUNG",
    ),
]


class BrandAliasResolver:
    MAX_ALIASES_PER_ID = 100

    def __init__(self, paths=None, extra_paths=None):
        self.alias_to_group = {}
        self.alias_names = {}
        self.group_titles = {}
        self._mapping_lock = threading.Lock()
        self._provider_mapping_path = get_provider_mappings_path()
        self.provider_mappings = self._load_provider_mappings()
        self._load_manual_families()
        load_paths = self._default_paths() if paths is None else list(paths)
        for path in load_paths:
            self.load_file(path)
        self._load_manual_families()
        for path in extra_paths or []:
            self.load_file(path)
        # User-edited groups have the highest priority.
        self._load_custom_groups()

    def key(self, brand):
        normalized = self.normalize(brand)
        if not normalized:
            return ""
        return self.alias_to_group.get(normalized, normalized)

    def same(self, left, right):
        return bool(left and right and self.key(left) == self.key(right))

    def title(self, brand):
        group = self.key(brand)
        return self.group_titles.get(group) or str(brand or "").strip()

    def group(self, brands):
        grouped = {}
        for brand in brands:
            brand = str(brand or "").strip()
            if not brand:
                continue
            grouped.setdefault(self.key(brand), []).append(brand)
        return grouped

    def for_provider(self, brand, provider_name):
        learned = self.provider_mappings.get(provider_name, {}).get(
            self._mapping_key(brand)
        )
        if learned:
            return learned
        if provider_name == "PrLgProvider" and self.same(
            brand, "HYUNDAI/KIA/MOBIS"
        ):
            return "HYUNDAI/KIA/MOBIS"
        if provider_name == "PrLgProvider" and self.same(brand, "RENAULT"):
            return "RENAULT"
        return str(brand or "").strip()

    def candidates_for_provider(self, brand, provider_name, limit=5):
        candidates = [
            self.for_provider(brand, provider_name),
            self.title(brand),
            str(brand or "").strip(),
        ]
        group = self.key(brand)
        candidates.extend(
            self.alias_names.get(alias, alias)
            for alias, alias_group in self.alias_to_group.items()
            if alias_group == group
        )
        result = []
        seen = set()
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            normalized = self.normalize(candidate)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(candidate)
            if len(result) >= max(1, int(limit)):
                break
        return result

    def remember_provider_name(self, brand, provider_name, provider_brand):
        provider_brand = str(provider_brand or "").strip()
        if not brand or not provider_name or not provider_brand:
            return
        key = self._mapping_key(brand)
        with self._mapping_lock:
            provider_map = self.provider_mappings.setdefault(provider_name, {})
            if provider_map.get(key) == provider_brand:
                return
            provider_map[key] = provider_brand
            try:
                self._write_provider_mappings(self.provider_mappings)
            except OSError:
                pass

    def _mapping_key(self, brand):
        return self.normalize(self.title(brand) or brand)

    def _load_provider_mappings(self):
        """Читает выученные написания брендов из одного файла.

        Раньше их было два — рядом с программой и в config/, — причём писался
        только первый. Второй тихо участвовал в слиянии и со временем устаревал,
        поэтому переносим его содержимое один раз и больше к нему не обращаемся.
        """
        legacy_path = os.path.join(get_config_dir(), "brand_provider_mappings.json")
        result = self._read_provider_mapping_file(self._provider_mapping_path)
        if not os.path.exists(legacy_path):
            return result
        legacy = self._read_provider_mapping_file(legacy_path)
        added = 0
        for provider, values in legacy.items():
            target = result.setdefault(provider, {})
            for key, value in values.items():
                if key not in target:
                    target[key] = value
                    added += 1
        try:
            if added:
                self._write_provider_mappings(result)
            os.replace(legacy_path, f"{legacy_path}.migrated")
        except OSError:
            pass
        return result

    @staticmethod
    def _read_provider_mapping_file(path):
        result = {}
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError):
            return result
        if not isinstance(data, dict):
            return result
        for provider, values in data.items():
            if isinstance(values, dict):
                result.setdefault(str(provider), {}).update(values)
        return result

    def _write_provider_mappings(self, mappings):
        os.makedirs(os.path.dirname(self._provider_mapping_path), exist_ok=True)
        temp_path = f"{self._provider_mapping_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(mappings, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self._provider_mapping_path)

    def _load_custom_groups(self):
        try:
            with open(get_custom_groups_path(), encoding="utf-8") as file:
                groups = json.load(file)
        except (OSError, ValueError):
            return
        if not isinstance(groups, list):
            return
        for index, item in enumerate(groups):
            if not isinstance(item, dict):
                continue
            title = str(item.get("canonical", "")).strip()
            aliases = [title] + list(item.get("aliases") or [])
            normalized = [self.normalize(value) for value in aliases if self.normalize(value)]
            if not title or not normalized:
                continue
            related = {self.alias_to_group.get(value, value) for value in normalized}
            group = f"custom:{index}:{self.normalize(title)}"
            for alias, old_group in list(self.alias_to_group.items()):
                if old_group in related:
                    self.alias_to_group[alias] = group
            for original, alias in zip(aliases, normalized):
                self.alias_to_group[alias] = group
                self.alias_names[alias] = str(original).strip()
            self.group_titles[group] = title
            mapping_key = self.normalize(title)
            for provider, provider_brand in (item.get("providers") or {}).items():
                provider_brand = str(provider_brand or "").strip()
                if provider and provider_brand:
                    self.provider_mappings.setdefault(str(provider), {})[mapping_key] = provider_brand

    def normalize(self, brand):
        text = html.unescape(str(brand or "")).upper()
        text = re.sub(r"\([^)]*\)", " ", text)
        return re.sub(r"[^A-ZА-ЯЁ0-9]+", "", text)

    def load_file(self, path):
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            items = self._parse_items(text)
            loaded = self._load_reference_alias_items(items)
            by_id = {}
            for item in items:
                brand_id = item.get("BrandID")
                brand_name = item.get("BrandName")
                if brand_id in (None, "") or not brand_name:
                    continue
                by_id.setdefault(str(brand_id), []).append(str(brand_name))
            for brand_id, names in by_id.items():
                unique_names = list(dict.fromkeys(names))
                if len(unique_names) > self.MAX_ALIASES_PER_ID:
                    continue
                group = f"id:{brand_id}"
                title = self._best_title(unique_names)
                self.group_titles[group] = title
                for name in unique_names:
                    normalized = self.normalize(name)
                    if normalized:
                        self.alias_names.setdefault(normalized, str(name).strip())
                    if normalized and normalized not in self.alias_to_group:
                        self.alias_to_group[normalized] = group
                loaded = True
            return loaded
        except Exception:
            return False

    def _load_reference_alias_items(self, items):
        loaded = False
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            title = str(item.get("name") or item.get("canonical") or "").strip()
            aliases = item.get("aliases") or item.get("alias") or []
            if isinstance(aliases, str):
                aliases = re.split(r"[\n;,]+", aliases)
            if not isinstance(aliases, list):
                aliases = []
            values = [title] + [str(value).strip() for value in aliases]
            pairs = [
                (value, self.normalize(value))
                for value in values
                if self.normalize(value)
            ]
            if not title or not pairs:
                continue
            # Сливать группы можно не по любому общему названию. Уточняющие
            # ярлыки вида "OPEL (PSA)" после снятия скобок превращаются в чужое
            # имя "OPEL", а битые кодировки "CITRO?N" — в "CITRON". Из-за этого
            # General Motors, PSA и «Цитрон» слипались в группу на 112 названий,
            # и деталь Citroen считалась искомой по запросу Chevrolet.
            # Такие названия остаются вариантами написания, но не объединяют
            # производителей.
            # Собственное имя записи объединять можно всегда: ограничение
            # касается только алиасов, среди которых встречаются ярлыки каталога.
            merge_values = [
                alias
                for position, (original, alias) in enumerate(pairs)
                if position == 0 or self._can_merge_on(original)
            ]
            related = {
                self.alias_to_group[value]
                for value in merge_values
                if value in self.alias_to_group
            }
            group = f"ref:{index}:{self.normalize(title)}"
            if related:
                for alias, old_group in list(self.alias_to_group.items()):
                    if old_group in related:
                        self.alias_to_group[alias] = group
            for position, (original, alias) in enumerate(pairs):
                self.alias_names.setdefault(alias, str(original).strip())
                if position == 0 or self._can_merge_on(original) or alias not in self.alias_to_group:
                    self.alias_to_group[alias] = group
            self.group_titles[group] = title
            loaded = True
        return loaded

    @staticmethod
    def _can_merge_on(value):
        """Можно ли объединять производителей по этому написанию.

        Нельзя, если название уточнено скобками (это ярлык каталога, а не имя
        бренда) или содержит следы битой кодировки.
        """
        text = str(value or "")
        if "(" in text or ")" in text:
            return False
        return "?" not in text and "�" not in text

    def _parse_items(self, text):
        text = html.unescape(text or "")
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = text.strip()
        if not text:
            return []
        data = json.loads(text)
        return data if isinstance(data, list) else []

    def _load_manual_families(self):
        for family in MANUAL_FAMILIES:
            normalized = [self.normalize(name) for name in family if self.normalize(name)]
            if not normalized:
                continue
            related_groups = {self.alias_to_group.get(name, name) for name in normalized}
            group = sorted(related_groups)[0]
            for alias, existing_group in list(self.alias_to_group.items()):
                if existing_group in related_groups:
                    self.alias_to_group[alias] = group
            for name in normalized:
                self.alias_to_group[name] = group
            for original, name in zip(family, normalized):
                self.alias_names[name] = original
            self.group_titles[group] = family[0]

    def _best_title(self, names):
        clean_names = [str(name).strip() for name in names if str(name).strip()]
        if not clean_names:
            return ""
        return clean_names[0]

    def _default_paths(self):
        local_path = os.path.join(get_brand_storage_dir(), "brands.txt")
        bundled_reference = os.path.join(get_bundled_resource_dir(), "brand_reference_abcp.json")
        legacy_paths = [
            os.path.join(get_config_dir(), "brands.txt"),
            os.path.join(os.path.expanduser("~"), "Desktop", "brands.txt"),
            os.path.join(os.path.expanduser("~"), "Downloads", "Telegram Desktop", "brands.txt"),
        ]
        if not os.path.exists(local_path):
            for source in legacy_paths:
                if os.path.exists(source):
                    try:
                        shutil.copy2(source, local_path)
                    except OSError:
                        pass
                    break
        return [bundled_reference, local_path] + legacy_paths
