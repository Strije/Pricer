import hashlib
import json
import os
import re
import datetime
from urllib.parse import quote

import requests
from requests import exceptions as request_exceptions

from config_path import get_config_dir


class AbcpReferenceProvider:
    """Reference-only ABCP source for brands, crosses and image links."""

    IMAGE_BASE_URL = "https://imgcdn.abcp.ru/p/"
    CROSS_TYPE_NAMES = {
        "1": "замена",
        "2": "входит в комплект",
        "3": "часть комплекта",
        "4": "односторонняя замена",
        "5": "односторонняя замена",
    }

    def __init__(
        self,
        host,
        login,
        password,
        include_crosses=True,
        include_images=True,
        allow_articles_info=False,
        articles_info_daily_limit=8,
        timeout=8,
    ):
        self.host = self._normalize_host(host)
        self.login = str(login or "").strip()
        self.password = str(password or "").strip()
        self.include_crosses = bool(include_crosses)
        self.include_images = bool(include_images)
        self.allow_articles_info = bool(allow_articles_info)
        self.articles_info_daily_limit = max(0, int(articles_info_daily_limit or 0))
        self.timeout = int(timeout or 8)
        self.last_message = ""
        self.brand_resolver_timeout = min(max(3, self.timeout), 5)
        self._state_path = os.path.join(get_config_dir(), "abcp_articles_info_cache.json")

    def check_connection(self):
        if not self.host or not self.login or not self.password:
            return False, "Хост/логин/MD5 пароль пусты"
        brands = self.get_brand_candidates("01089")
        if brands:
            return True, f"OK: брендов по тесту {len(brands)}"
        return False, self.last_message or "ABCP справочник не ответил"

    def get_brand_candidates(self, article):
        self.last_message = ""
        article = str(article or "").strip()
        if not article:
            return []
        data = self._get("search/brands", {"number": article})
        rows = self._as_list(data)
        result = []
        seen = set()
        for row in rows:
            if isinstance(row, str):
                brand = row.strip()
                item_article = article
                name = ""
            elif isinstance(row, dict):
                brand = self._first_value(row, "brand", "Brand", "BRAND", "name", "Name")
                item_article = (
                    self._first_value(row, "number", "Number", "numberFix", "article", "Article")
                    or article
                )
                name = self._first_value(row, "description", "Description", "name", "Name") or ""
            else:
                continue
            brand = str(brand or "").strip()
            item_article = str(item_article or article).strip()
            if not brand:
                continue
            marker = (brand.upper(), self._article_key(item_article), str(name).upper())
            if marker in seen:
                continue
            seen.add(marker)
            result.append(
                {
                    "brand": brand,
                    "article": item_article,
                    "name": str(name or "").strip(),
                    "source": "ABCP search/brands",
                }
            )
        if not result and not self.last_message:
            self.last_message = "ABCP: бренды не найдены"
        return result

    def get_brands(self, article):
        brands = []
        seen = set()
        for item in self.get_brand_candidates(article):
            brand = str(item.get("brand", "")).strip()
            key = brand.upper()
            if brand and key not in seen:
                seen.add(key)
                brands.append(brand)
        return brands

    def get_article_info(self, article, brand):
        self.last_message = ""
        article = str(article or "").strip()
        brand = str(brand or "").strip()
        if not article or not brand:
            self.last_message = "ABCP: для карточки нужен артикул и бренд"
            return {}
        cache_key = self._article_info_cache_key(article, brand)
        cached = self._cached_article_info(cache_key)
        if cached:
            return cached
        if not self.allow_articles_info:
            self.last_message = "ABCP: articles/info отключён, чтобы не расходовать лимит тарифа"
            return {}
        if not self._reserve_articles_info_call():
            return {}
        params = {
            "brand": brand,
            "number": article,
            "format": "bnpict",
            "locale": "ru_RU",
        }
        if self.include_images:
            params["cross_image"] = "1"
        data = self._get("articles/info", params)
        info = self._as_article_info(data)
        if info:
            self._save_article_info(cache_key, info)
        return info

    def get_cross_targets(self, article, brand):
        if not self.include_crosses:
            return []
        info = self.get_article_info(article, brand)
        rows = self._as_list(self._first_value(info, "crosses", "Crosses") or [])
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            cross_brand = str(
                self._first_value(row, "brand", "Brand", "BRAND") or ""
            ).strip()
            cross_article = str(
                self._first_value(
                    row,
                    "number",
                    "Number",
                    "numberFix",
                    "article",
                    "Article",
                    "code",
                    "Code",
                )
                or ""
            ).strip()
            if not cross_brand or not cross_article:
                continue
            marker = (self._article_key(cross_article), cross_brand.upper())
            if marker in seen:
                continue
            seen.add(marker)
            cross_type = self._first_value(row, "crossType", "CrossType", "type", "Type")
            images = self._image_urls(self._first_value(row, "images", "Images") or [])
            result.append(
                {
                    "brand": cross_brand,
                    "article": cross_article,
                    "name": str(
                        self._first_value(row, "description", "Description", "name", "Name")
                        or ""
                    ).strip(),
                    "is_cross": True,
                    "source": "ABCP articles/info",
                    "relation": self.CROSS_TYPE_NAMES.get(str(cross_type or ""), ""),
                    "cross_type": cross_type,
                    "image_urls": images,
                    "raw": row,
                }
            )
        return result

    def get_images(self, article, brand):
        if not self.include_images:
            return []
        info = self.get_article_info(article, brand)
        return self._image_urls(self._first_value(info, "images", "Images") or [])

    def get_brand_alias_items(self):
        data = self._get("articles/brands", {})
        return [
            item
            for item in self._as_list(data)
            if isinstance(item, dict) and item.get("name")
        ]

    def _get(self, path, params=None):
        if not self.host:
            self.last_message = "ABCP: хост пуст"
            return None
        request_params = self._auth_params(params or {})
        try:
            with requests.Session() as session:
                session.trust_env = False
                resp = session.get(
                    self._url(path),
                    params=request_params,
                    headers={"Accept": "application/json"},
                    timeout=self.timeout,
                    proxies={"http": None, "https": None},
                )
            if resp.status_code != 200:
                self.last_message = self._format_response_error(resp)
                return None
            data = resp.json()
            error = self._extract_error_message(data)
            if error:
                self.last_message = error
                return None
            return data
        except request_exceptions.ConnectTimeout:
            self.last_message = "ABCP не ответил: таймаут подключения"
            return None
        except request_exceptions.ReadTimeout:
            self.last_message = "ABCP не ответил: таймаут чтения"
            return None
        except request_exceptions.SSLError as exc:
            self.last_message = f"Ошибка SSL/TLS ABCP: {str(exc)[:80]}"
            return None
        except request_exceptions.ConnectionError as exc:
            self.last_message = f"Ошибка соединения с ABCP: {str(exc)[:80]}"
            return None
        except ValueError as exc:
            self.last_message = f"ABCP вернул не JSON: {str(exc)[:80]}"
            return None
        except Exception as exc:
            self.last_message = str(exc)[:120]
            return None

    def _auth_params(self, params):
        result = dict(params or {})
        result["userlogin"] = self.login
        result["userpsw"] = self._password_hash()
        return result

    def _article_info_cache_key(self, article, brand):
        image_mode = "img1" if self.include_images else "img0"
        raw = f"{self.host}|{self._article_key(article)}|{str(brand or '').strip().upper()}|{image_mode}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cached_article_info(self, key):
        state = self._load_state()
        item = (state.get("items") or {}).get(key)
        if not isinstance(item, dict):
            return {}
        data = item.get("data")
        return data if isinstance(data, dict) else {}

    def _save_article_info(self, key, info):
        state = self._load_state()
        items = state.setdefault("items", {})
        items[key] = {
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "data": info,
        }
        self._save_state(state)

    def _reserve_articles_info_call(self):
        if self.articles_info_daily_limit <= 0:
            self.last_message = "ABCP: лимит articles/info установлен 0, запросы отключены"
            return False
        today = datetime.date.today().isoformat()
        state = self._load_state()
        calls = state.setdefault("calls", {})
        used = int(calls.get(today) or 0)
        if used >= self.articles_info_daily_limit:
            self.last_message = (
                f"ABCP: дневной лимит articles/info исчерпан "
                f"({used}/{self.articles_info_daily_limit})"
            )
            return False
        calls[today] = used + 1
        for day in list(calls):
            if day != today:
                calls.pop(day, None)
        self._save_state(state)
        return True

    def _load_state(self):
        try:
            with open(self._state_path, encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state):
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _password_hash(self):
        if re.fullmatch(r"[0-9a-fA-F]{32}", self.password or ""):
            return self.password.lower()
        return hashlib.md5(self.password.encode("utf-8")).hexdigest()

    def _url(self, path):
        return f"{self.host}/{str(path or '').strip('/')}"

    def _normalize_host(self, host):
        value = str(host or "").strip().rstrip("/")
        if value and "://" not in value:
            value = f"https://{value}"
        return value

    def _format_response_error(self, resp):
        try:
            data = resp.json()
            message = self._extract_error_message(data)
            if message:
                return message
        except Exception:
            pass
        text = (resp.text or "").strip()
        if text:
            return f"HTTP {resp.status_code}: {text[:80]}"
        return f"HTTP {resp.status_code}"

    def _extract_error_message(self, data):
        if not isinstance(data, dict):
            return ""
        code = data.get("errorCode") or data.get("error_code")
        message = data.get("errorMessage") or data.get("error") or data.get("message")
        if code or message:
            return f"{code}: {message}" if code and message else str(message or code)
        return ""

    def _as_article_info(self, data):
        if isinstance(data, dict):
            for key in ("data", "result", "article", "item"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    return nested
            return data
        rows = self._as_list(data)
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def _as_list(self, data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in (
                "data",
                "items",
                "result",
                "results",
                "brands",
                "Brands",
                "crosses",
                "Crosses",
                "articles",
                "Articles",
            ):
                value = data.get(key)
                if isinstance(value, list):
                    return value
            return [data] if data else []
        return []

    def _image_urls(self, images):
        if not self.include_images:
            return []
        rows = images if isinstance(images, list) else [images]
        result = []
        seen = set()
        for item in rows:
            if isinstance(item, dict):
                value = self._first_value(item, "url", "URL", "src", "name", "file", "image")
            else:
                value = item
            value = str(value or "").strip()
            if not value:
                continue
            if value.lower().startswith(("http://", "https://")):
                url = value
            else:
                url = self.IMAGE_BASE_URL + quote(value.lstrip("/"))
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    def _first_value(self, data, *keys):
        if not isinstance(data, dict):
            return None
        for key in keys:
            if key in data and data.get(key) not in (None, ""):
                return data.get(key)
        return None

    def _article_key(self, value):
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
