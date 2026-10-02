import requests

from response_text import decode_response_text
import time
import zlib
from urllib.parse import urlencode

class ForumAutoProvider:
    ERROR_MESSAGES = {
        0: "Веб-сервис недоступен",
        1: "Неверный тип переменной",
        2: "Метод не найден",
        5: "Превышен лимит запросов в минуту",
        6: "Превышен суточный лимит запросов",
        10: "Неверный логин/пароль",
        11: "Доступ запрещен",
        12: "Клиент заблокирован",
        13: "Превышен доступный лимит",
        20: "Товар не найден",
        21: "Заказ не найден",
        22: "Заказ нельзя аннулировать в текущем статусе",
        23: "Ошибка аннулирования заказа",
        24: "Заказ уже аннулирован",
        25: "Ошибка создания заказа",
        26: "Активные заказы не найдены",
        27: "Товары не найдены",
        28: "Неверное количество товара",
        29: "Неверный eid или tid",
        30: "Заказ забронирован частично",
        31: "Предложение устарело, повторите поиск",
    }

    METHOD_ALIASES = {
        "clientInfo": "clientInfo",
        "listGoods": "listGoods",
        "listBrands": "listBrands",
        "addGoodsToOrder": "addGoodsToOrder",
        "listOrders": "listOrders",
        "cancelOrder": "cancelOrder",
    }

    def __init__(self, login, password, include_crosses=True, timeout=10, external_order_id=""):
        self.login = login
        self.password = password
        self.include_crosses = bool(include_crosses)
        self.timeout = int(timeout)
        self.external_order_id = str(external_order_id or "").strip()
        self.base_url = "https://api.forum-auto.ru/v2"
        self.last_message = ""
        self.last_http_status = None
        self.debug_log = []

    def _get(self, method, params, encoding="utf-8"):
        full_params = {"login": self.login, "pass": self.password, **params}
        session = requests.Session()
        session.trust_env = False
        request_params = (
            urlencode(full_params, encoding=encoding)
            if str(encoding or "").lower() != "utf-8"
            else full_params
        )
        endpoint = self.METHOD_ALIASES.get(method, method)
        url = f"{self.base_url}/{endpoint}"
        self._debug(f"метод {method} -> /{endpoint}")
        self._debug(f"параметры: {self._safe_params(full_params)}")
        self.last_http_status = None
        started = time.monotonic()
        try:
            resp = session.get(
                url,
                params=request_params,
                timeout=self.timeout,
                proxies={"http": None, "https": None},
            )
        except requests.exceptions.Timeout:
            self.last_message = f"Forum-Auto: таймаут после {self.timeout}с"
            self._debug(self.last_message)
            return None
        except requests.exceptions.RequestException as exc:
            self.last_message = f"Forum-Auto: ошибка сети {exc.__class__.__name__}: {exc}"
            self._debug(self._redact_text(self.last_message))
            return None
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.last_http_status = resp.status_code
        self._debug(f"HTTP {resp.status_code} за {elapsed_ms} мс")
        self._debug(f"адрес ответа: {self._redact_url(getattr(resp, 'url', url))}")
        content_type = resp.headers.get("content-type", "") if hasattr(resp, "headers") else ""
        if content_type:
            self._debug(f"content-type: {content_type}")
        if resp.status_code != 200:
            text = decode_response_text(resp)
            self.last_message = f"HTTP {resp.status_code}: {self._redact_text(text[:300])}"
            self._debug(f"ответ: {self._redact_text(text[:500])}")
            return None
        try:
            data = resp.json()
        except Exception:
            text = decode_response_text(resp)
            self.last_message = f"Forum-Auto вернул не JSON: {self._redact_text(text[:300])}"
            self._debug(f"ответ не JSON: {self._redact_text(text[:500])}")
            return None
        self._debug(f"JSON: {self._describe_payload(data)}")
        error = self._extract_error(data)
        if error:
            self.last_message = error
            self._debug(f"ошибка API: {error}")
            return None
        self.last_message = ""
        return data

    def check_connection(self):
        data = self._get("clientInfo", {})
        if data is None:
            return False, self.last_message or "Forum-Auto не ответил"
        return True, "OK"

    def get_prices(self, article):
        self._reset_debug()
        self._debug(f"поиск артикула: {article}")
        data = self._search_goods(article, 1 if self.include_crosses else 0)
        if data is None and self.include_crosses and self.last_http_status == 500:
            self._debug("повторяю поиск как в старой версии: cross=0")
            data = self._search_goods(article, 0)
        if data is None:
            return []
        if isinstance(data, dict):
            error = data.get("error") or data.get("message") or data.get("msg")
            if error:
                self.last_message = str(error)
                return []
            data = (
                data.get("goods")
                or data.get("items")
                or data.get("data")
                or data.get("list")
                or data.get("rows")
                or data.get("result")
                or data.get("return")
                or []
            )
            self._debug(f"список предложений после распаковки: {self._describe_payload(data)}")
        if not data or not isinstance(data, list):
            if data:
                self.last_message = f"Forum-Auto: неожиданный формат ответа {type(data).__name__}"
                self._debug(self.last_message)
            else:
                self.last_message = "Forum-Auto: пустой список от listGoods"
                self._debug(self.last_message)
            return []
        results = []
        skipped_bad_row = 0
        skipped_no_price = 0
        skipped_no_qty = 0
        for item in data:
            if not isinstance(item, dict):
                skipped_bad_row += 1
                continue
            raw_days = self._to_int(item.get("d_deliv"), 0)
            raw_hours = self._to_int(item.get("h_deliv"), 0)
            item_article = str(item.get("art") or item.get("nr") or "")
            clean_item = "".join(char for char in item_article.upper() if char.isalnum())
            clean_requested = "".join(char for char in str(article).upper() if char.isalnum())
            price = self._to_float(item.get("price"), 0.0)
            qty = self._to_int(item.get("num"), 0)
            if price <= 0:
                skipped_no_price += 1
                continue
            if qty <= 0:
                skipped_no_qty += 1
                continue
            results.append({
                "provider": "Forum-Auto",
                "brand": item.get("brand", ""),
                "article": item_article,
                "price": price,
                "days": raw_days,
                "hours": raw_days * 24 + raw_hours,
                "quantity": str(qty),
                "logo": item.get("whse", "-"),
                "warehouse": item.get("whse", "-"),
                "name": item.get("name", "No name"),
                "gid": str(item.get("gid", "")),
                "multiplicity": max(1, self._to_int(item.get("kr"), 1)),
                # _to_int(0) даёт default (0 or "" -> ""), поэтому 0 сравниваем строкой.
                "not_returnable": str(item.get("is_returnable", "1")).strip().lower() in ("0", "false"),
                "is_cross": bool(clean_item and clean_item != clean_requested),
            })
        if skipped_bad_row or skipped_no_price or skipped_no_qty:
            self._debug(
                "отброшено: "
                f"не строка {skipped_bad_row}, без цены {skipped_no_price}, без остатка {skipped_no_qty}"
            )
        self._debug(f"готово к показу: {len(results)} позиций")
        if not results and not self.last_message:
            self.last_message = "Forum-Auto: все предложения отфильтрованы по цене или остатку"
        return results

    def _search_goods(self, article, cross):
        return self._get("listGoods", {
            "art": article,
            "cross": int(cross),
        })

    def get_brand_candidates(self, article):
        self._reset_debug()
        self._debug(f"поиск брендов артикула: {article}")
        data = self._get("listBrands", {"art": article})
        if data is None:
            return []
        items = self._extract_list(data)
        result = []
        seen = set()
        for item in items:
            if isinstance(item, str):
                brand = item.strip()
                item_article = str(article or "").strip()
                name = ""
            elif isinstance(item, dict):
                brand = str(
                    item.get("brand")
                    or item.get("BRAND")
                    or item.get("name")
                    or item.get("Name")
                    or ""
                ).strip()
                item_article = str(
                    item.get("art")
                    or item.get("nr")
                    or item.get("article")
                    or item.get("code")
                    or article
                    or ""
                ).strip()
                name = str(item.get("description") or item.get("name") or item.get("Name") or "").strip()
            else:
                continue
            if not brand:
                continue
            marker = (brand.upper(), "".join(ch for ch in item_article.upper() if ch.isalnum()), name.upper())
            if marker in seen:
                continue
            seen.add(marker)
            result.append({
                "brand": brand,
                "article": item_article,
                "name": name,
                "source": "listBrands",
            })
        self._debug(f"найдено брендов: {len(result)}")
        return result

    def get_brands(self, article):
        brands = []
        for item in self.get_brand_candidates(article):
            brand = item.get("brand")
            if brand and brand not in brands:
                brands.append(brand)
        return brands

    def add_to_basket(self, item, quantity=1, comment=""):
        try:
            self._reset_debug()
            gid = str(item.get("gid", "") or "").strip()
            if not gid:
                return {"success": False, "error": "Forum-Auto: нет tid/gid товара"}
            params = {
                "tid": gid,
                "num": int(quantity),
            }
            params["eid"] = self._external_line_id(item)
            if self.external_order_id:
                params["eoid"] = self.external_order_id
            data = self._get("addGoodsToOrder", params, encoding="cp1251")
            if data is None:
                return {"success": False, "error": self.last_message or "Forum-Auto: заказ не создан"}
            return {"success": True, "data": data}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def debug_summary(self):
        if not self.debug_log:
            return ""
        return "\n".join(f"Forum-Auto debug: {line}" for line in self.debug_log[-40:])

    def _reset_debug(self):
        self.debug_log = []

    def _debug(self, message):
        self.debug_log.append(self._redact_text(message))
        if len(self.debug_log) > 80:
            self.debug_log = self.debug_log[-80:]

    def _safe_params(self, params):
        safe = {}
        for key, value in params.items():
            lowered = str(key).lower()
            if lowered in {"pass", "password", "pwd"}:
                safe[key] = "***"
            elif lowered in {"login", "user", "username"}:
                safe[key] = self._mask_value(value)
            else:
                safe[key] = value
        return safe

    def _mask_value(self, value):
        text = str(value or "")
        if len(text) <= 2:
            return "***"
        return f"{text[:1]}***{text[-1:]}"

    def _redact_url(self, url):
        return self._redact_text(str(url or ""))

    def _redact_text(self, text):
        value = str(text or "")
        for secret in (self.password, self.login):
            secret = str(secret or "")
            if secret:
                value = value.replace(secret, "***")
        return value

    def _describe_payload(self, data):
        if isinstance(data, list):
            sample = ""
            if data and isinstance(data[0], dict):
                sample = f", поля первой строки: {list(data[0].keys())[:12]}"
            return f"list, строк: {len(data)}{sample}"
        if isinstance(data, dict):
            return f"dict, ключи: {list(data.keys())[:20]}"
        return type(data).__name__

    def _extract_list(self, data):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in ("brands", "items", "data", "list", "rows", "result", "return", "goods"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = self._extract_list(value)
                if nested:
                    return nested
        return [data] if any(key in data for key in ("brand", "BRAND", "name", "Name")) else []

    def _external_line_id(self, item):
        value = item.get("eid") or item.get("external_id")
        if value:
            return self._to_int(value, 0)
        marker = "|".join(str(item.get(key, "")) for key in ("gid", "article", "brand", "warehouse"))
        return (zlib.crc32(marker.encode("utf-8")) & 0x7fffffff) or 1

    def _extract_error(self, data):
        if not isinstance(data, dict):
            return ""
        fault_code = data.get("FaultCode") or data.get("faultCode") or data.get("faultcode")
        fault_string = data.get("FaultString") or data.get("faultString") or data.get("faultstring")
        detail = data.get("Detail") or data.get("detail")
        if fault_code not in (None, ""):
            code = self._to_int(fault_code, None)
            message = self.ERROR_MESSAGES.get(code, str(fault_code))
            extra = str(fault_string or detail or "").strip()
            return f"{message}: {extra}" if extra and extra != message else message
        error = data.get("error") or data.get("message") or data.get("msg")
        if error:
            if isinstance(error, dict):
                return self._extract_error(error) or str(error)
            code = self._to_int(error, None)
            if code in self.ERROR_MESSAGES:
                return self.ERROR_MESSAGES[code]
            return str(error)
        return ""

    def _to_float(self, value, default=0.0):
        try:
            return float(str(value or "").replace(",", "."))
        except (TypeError, ValueError):
            return default

    def _to_int(self, value, default=0):
        try:
            return int(float(str(value or "").replace(",", ".")))
        except (TypeError, ValueError):
            return default
