import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import requests

from provider_adapter import OrderDeliveryUnknown, ProviderAdapter, call_order_once
from response_text import decode_response_text


def _decode_response(resp):
    return decode_response_text(resp)


class PrLgProvider:
    def __init__(
        self,
        api_key,
        timeout=12,
        order_method="",
        order_payment="",
        order_point="",
        order_address="",
        order_pickup_point="",
        create_order=False,
    ):
        self.api_key = api_key
        self.timeout = int(timeout)
        # Оформление корзины из двух десятков позиций тяжелее одиночного
        # запроса поиска, поэтому у заказа свой, более длинный таймаут.
        self.order_timeout = max(60, int(timeout) * 3)
        self.api_root = "https://api.pr-lg.ru"
        self.items_url = f"{self.api_root}/search/items"
        self.products_url = f"{self.api_root}/search/products"
        self.crosses_url = f"{self.api_root}/search/crosses"
        self.warehouses_url = f"{self.api_root}/search/warehouses"
        self.cart_url = f"{self.api_root}/cart/add"
        self.cart_list_url = f"{self.api_root}/cart/list"
        self.cart_remove_url = f"{self.api_root}/cart/remove"
        self.cart_params_url = f"{self.api_root}/cart/params"
        self.order_url = f"{self.api_root}/cart/order"
        self.order_method = str(order_method or "").strip()
        self.order_payment = str(order_payment or "").strip()
        self.order_point = str(order_point or "").strip()
        self.order_address = str(order_address or "").strip()
        self.order_pickup_point = str(order_pickup_point or "").strip()
        self.create_order = bool(create_order)
        self._session = None
        self.adapter = ProviderAdapter(retries=2, backoff=0.4)
        self.last_message = ""

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
            self._session.trust_env = False
        return self._session

    def _headers(self):
        return {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    def _get_json(self, url, params, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        def _request():
            return self.session.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=timeout,
                proxies={"http": None, "https": None},
            )

        resp = self.adapter.call(_request)
        if resp.status_code != 200:
            self.last_message = f"HTTP {resp.status_code}: {_decode_response(resp)[:160]}"
            return None
        try:
            return json.loads(_decode_response(resp))
        except Exception as exc:
            self.last_message = f"Ответ не похож на JSON: {exc}"
            return None

    def check_connection(self):
        self.last_message = ""
        data = self._get_json(
            self.warehouses_url,
            {"secret": self.api_key, "action": "list"},
            timeout=min(self.timeout, 10),
        )
        if data is None:
            return False, self.last_message or "нет ответа"
        if isinstance(data, dict) and data.get("status") in ("error", "auth-error"):
            return False, data.get("err") or data.get("message") or str(data)
        if isinstance(data, list):
            return True, f"складов: {len(data)}"
        return True, "OK"

    def get_order_params(self):
        data = self._get_json(
            self.cart_params_url,
            {"secret": self.api_key},
            timeout=min(self.timeout, 10),
        )
        if isinstance(data, dict) and data.get("status") in ("error", "auth-error"):
            self.last_message = data.get("err") or data.get("message") or str(data)
            return {}
        return data if isinstance(data, dict) else {}

    def get_prices(self, article, brand=""):
        self.last_message = ""
        try:
            data = self._get_json(
                self.items_url,
                {"secret": self.api_key, "article": article},
            )
            return self._parse_search_response(data, article, requested_brand=brand)
        except Exception as exc:
            self.last_message = str(exc)
            print(f"Ошибка внутри PrLgProvider: {exc}")
            return []

    def get_brand_candidates(self, article):
        self.last_message = ""
        data = self._get_json(
            self.items_url,
            {"secret": self.api_key, "article": article},
            timeout=min(self.timeout, 5),
        )
        if data is None:
            return []
        if isinstance(data, dict):
            if data.get("status") in ("error", "auth-error"):
                self.last_message = data.get("err") or data.get("message") or str(data)
                return []
            data = data.get("data") or data.get("items") or []
        if not isinstance(data, list):
            return []

        result = []
        seen = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").strip()
            if not brand:
                continue
            item_article = str(item.get("article") or article or "").strip()
            name = str(item.get("description") or item.get("name") or "").strip()
            marker = (brand.upper(), self._clean(item_article), name.upper())
            if marker in seen:
                continue
            seen.add(marker)
            result.append({
                "brand": brand,
                "article": item_article,
                "name": name,
                "source": "search/items",
            })
        return result

    def get_brands(self, article):
        brands = []
        for item in self.get_brand_candidates(article):
            brand = item.get("brand")
            if brand and brand not in brands:
                brands.append(brand)
        return brands

    def get_product_prices(self, article, brand):
        self.last_message = ""
        if not brand:
            return []
        data = self._get_json(
            self.products_url,
            {
                "secret": self.api_key,
                "article": article,
                "brand": brand,
            },
        )
        return self._parse_search_response(data, article, requested_brand=brand, crosses=False)

    def get_cross_prices(self, article, brand):
        self.last_message = ""
        if not brand:
            return []
        data = self._get_json(
            self.crosses_url,
            {
                "secret": self.api_key,
                "article": article,
                "brand": brand,
                "replaces": 1,
            },
        )
        return self._parse_search_response(data, article, requested_brand=brand, crosses=True)

    def get_brand_prices(self, article, brand):
        results = []
        seen = set()
        for part in (self.get_product_prices(article, brand), self.get_cross_prices(article, brand)):
            for item in part or []:
                marker = (
                    item.get("article_id"),
                    item.get("warehouse_id"),
                    self._clean(item.get("article")),
                    self._clean(item.get("brand")),
                    item.get("price"),
                    item.get("warehouse"),
                )
                if marker in seen:
                    continue
                seen.add(marker)
                results.append(item)
        return results

    def get_prices_parallel(self, raw_article, clean_article, brand=""):
        method = self.get_brand_prices if brand else self.get_prices
        if str(raw_article).strip().upper() == str(clean_article).strip().upper():
            return method(raw_article, brand)
        executor = ThreadPoolExecutor(max_workers=2)
        try:
            raw_future = executor.submit(method, raw_article, brand)
            clean_future = executor.submit(method, clean_article, brand)

            try:
                raw_result = raw_future.result(timeout=self.timeout)
                if raw_result:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return raw_result
            except TimeoutError:
                pass
            except Exception:
                pass

            try:
                clean_result = clean_future.result(timeout=self.timeout)
                return clean_result or []
            except Exception:
                return []
        finally:
            executor.shutdown(wait=False)

    def _parse_search_response(self, data, requested_article="", requested_brand="", crosses=False):
        if data is None:
            return []
        if isinstance(data, dict):
            if data.get("status") in ("error", "auth-error"):
                self.last_message = data.get("err") or data.get("message") or str(data)
                return []
            data = data.get("data") or data.get("items") or []
        if not isinstance(data, list):
            self.last_message = f"Неожиданный ответ: {type(data).__name__}"
            return []

        results = []
        for brand_group in data:
            if not isinstance(brand_group, dict):
                continue
            brand_name = brand_group.get("brand", requested_brand or "Unknown")
            group_article = brand_group.get("article") or requested_article
            group_name = brand_group.get("description") or "No name"
            products_raw = brand_group.get("products", {})
            if isinstance(products_raw, dict):
                products = products_raw.values()
            elif isinstance(products_raw, list):
                products = products_raw
            else:
                products = [brand_group] if brand_group.get("price") is not None else []
            for product in products:
                parsed = self._parse_product_item(
                    product,
                    brand_name,
                    group_article,
                    group_name,
                    requested_article,
                    requested_brand,
                    crosses,
                )
                if parsed:
                    results.append(parsed)
        return results

    def _parse_product_item(self, item, brand_name, group_article, group_name, requested_article, requested_brand, crosses):
        if not isinstance(item, dict):
            return None
        try:
            price = float(item.get("price", 0) or 0)
        except Exception:
            price = 0
        if price <= 0:
            return None

        article = item.get("article") or group_article
        raw_delivery_text = str(item.get("show_date") or "").strip()
        delivery_hours = self._parse_delivery_hours(item)
        days = max(0, math.ceil(delivery_hours / 24)) if delivery_hours else 0

        # search/crosses возвращает не только замены, но и сам запрошенный
        # артикул, поэтому признак кросса определяем по данным позиции,
        # а не по тому, из какого эндпоинта она пришла.
        article_matches = bool(requested_article) and bool(article) and (
            self._clean(article) == self._clean(requested_article)
        )
        brand_matches = bool(requested_brand) and bool(brand_name) and (
            self._clean(brand_name) == self._clean(requested_brand)
        )

        is_cross = bool(crosses)
        if article_matches and brand_matches:
            is_cross = False
        if requested_article and article and not article_matches:
            is_cross = True
        if requested_brand and brand_name and not brand_matches:
            is_cross = True

        warehouse = item.get("custom_warehouse_name") or item.get("warehouse") or "-"
        return {
            "provider": "Profit-League",
            "brand": brand_name,
            "article": article,
            "price": price,
            "days": days,
            "delivery_hours": delivery_hours,
            "delivery_total_hours": delivery_hours,
            "delivery_display": raw_delivery_text,
            "delivery_text": raw_delivery_text,
            "delivery_time": item.get("delivery_time", ""),
            "delivery_date": item.get("delivery_date", ""),
            "quantity": str(item.get("quantity", "0")),
            "logo": warehouse,
            "warehouse": warehouse,
            "name": item.get("description") or group_name,
            "article_id": str(item.get("article_id") or item.get("id") or ""),
            "warehouse_id": str(item.get("warehouse_id") or ""),
            "code": str(item.get("product_code") or ""),
            "multiplicity": int(float(item.get("multi", 1) or 1)),
            "delivery_probability": item.get("delivery_probability", ""),
            "allow_return": item.get("allow_return", ""),
            "return_days": item.get("return_days", ""),
            "is_cross": is_cross,
            "source_brand": requested_brand if is_cross else "",
            "source_code": requested_article if is_cross else "",
        }

    def _parse_delivery_hours(self, item):
        hours = self._number_or_none(item.get("delivery_time"))
        if hours is not None:
            return max(0, int(math.ceil(hours)))

        show_date_hours = self._parse_show_date_hours(item.get("show_date"))
        if show_date_hours is not None:
            return show_date_hours

        return 0

    def _parse_show_date_hours(self, value):
        text = str(value or "").strip().lower().replace("ё", "е")
        if not text:
            return None
        numbers = [
            self._number_or_none(match)
            for match in re.findall(r"\d+(?:[.,]\d+)?", text)
        ]
        numbers = [number for number in numbers if number is not None]
        if not numbers:
            return None
        number = max(numbers)
        if "ч" in text or "hour" in text:
            return max(0, int(math.ceil(number)))
        if "д" in text or "day" in text:
            return max(0, int(math.ceil(number * 24)))
        return max(0, int(math.ceil(number * 24)))

    def _number_or_none(self, value):
        if value in (None, ""):
            return None
        try:
            return float(str(value).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return None

    def add_to_basket(self, item, quantity=1, comment=""):
        try:
            if self.create_order:
                cleared = self.clear_cart()
                if not cleared.get("success"):
                    return cleared
            added = self._cart_add(item, quantity, comment)
            if not added.get("success"):
                return added
            if self.create_order:
                return self.create_cart_order()
            return added
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def add_to_basket_batch(self, rows, comment=""):
        """Кладёт все позиции в корзину и оформляет их ОДНИМ заказом.

        По одной позиции за раз получалось «очистить корзину → положить одну
        → оформить»: 23 позиции превращались в 23 отдельных заказа у
        поставщика. Корзину чистим один раз, оформляем один раз.
        rows — список {"item": предложение, "quantity": количество};
        ответ — список той же длины и в том же порядке.
        """
        rows = list(rows or [])
        if not rows:
            return []
        results = [None] * len(rows)
        if self.create_order:
            try:
                cleared = self.clear_cart()
            except Exception as exc:
                cleared = {"success": False, "error": str(exc)}
            if not cleared.get("success"):
                return [dict(cleared) for _ in rows]

        in_cart = []
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            added = self._cart_add(item, quantity, comment)
            results[index] = added
            if added.get("success"):
                in_cart.append(index)

        if not self.create_order or not in_cart:
            return results

        try:
            order_result = self.create_cart_order()
        except Exception as exc:
            order_result = {"success": False, "error": str(exc)}
        # Заказ оформляется на всю корзину разом, поэтому его исход — общий
        # для всех позиций, которые в неё попали. Те, что в корзину не легли,
        # сохраняют собственную ошибку.
        for index in in_cart:
            results[index] = dict(order_result)
        return results

    def _cart_add(self, item, quantity=1, comment=""):
        params = {
            "secret": self.api_key,
            "id": item.get("article_id", ""),
            "warehouse": item.get("warehouse_id", ""),
            "quantity": quantity,
            "code": item.get("code", ""),
            "comment": str(comment or "")[:255],
        }

        def _request():
            return self.session.post(
                self.cart_url,
                data=params,
                headers=self._headers(),
                timeout=self.timeout,
                proxies={"http": None, "https": None},
            )

        try:
            resp = self.adapter.call(_request)
            if resp.status_code != 200:
                return {"success": False, "error": f"HTTP {resp.status_code}: {_decode_response(resp)[:200]}"}
            data = json.loads(_decode_response(resp))
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        if isinstance(data, dict) and data.get("status") != "success":
            return {"success": False, "error": data.get("err") or str(data)}
        return {"success": True, "data": data}

    def get_cart_items(self):
        data = self._get_json(self.cart_list_url, {"secret": self.api_key}, timeout=min(self.timeout, 10))
        if data is None:
            return None
        if isinstance(data, dict) and data.get("status") in ("error", "auth-error"):
            self.last_message = data.get("err") or data.get("message") or str(data)
            return None
        return self._extract_cart_items(data)

    def clear_cart(self):
        items = self.get_cart_items()
        if items is None:
            return {"success": False, "error": self.last_message or "Profit-League: не удалось проверить корзину перед заказом"}
        for cart_item in items:
            article_id = cart_item.get("article_id") or cart_item.get("id") or cart_item.get("articleId")
            warehouse_id = cart_item.get("warehouse_id") or cart_item.get("warehouse") or cart_item.get("warehouseId")
            if not article_id or not warehouse_id:
                continue
            removed = self.remove_cart_item(article_id, warehouse_id)
            if not removed.get("success"):
                return removed
        return {"success": True, "data": f"очищено позиций: {len(items)}"}

    def remove_cart_item(self, article_id, warehouse_id):
        params = {"secret": self.api_key, "id": article_id, "warehouse": warehouse_id}

        def _request():
            return self.session.post(
                self.cart_remove_url,
                data=params,
                headers=self._headers(),
                timeout=self.timeout,
                proxies={"http": None, "https": None},
            )

        resp = self.adapter.call(_request)
        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}: {_decode_response(resp)[:200]}"}
        try:
            data = json.loads(_decode_response(resp))
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("status") not in (None, "success"):
            return {"success": False, "error": data.get("err") or str(data)}
        return {"success": True, "data": data or "OK"}

    def _extract_cart_items(self, data):
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("items", "data", "cart", "products", "goods"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = self._extract_cart_items(value)
                if nested:
                    return nested
        return []

    def create_cart_order(self):
        if not self.order_method or not self.order_payment:
            return {"success": False, "error": "Profit-League: укажите способ доставки и способ оплаты для заказа"}
        params = {
            "secret": self.api_key,
            "method": self.order_method,
            "payment": self.order_payment,
        }
        if self.order_point:
            params["point"] = self.order_point
        if self.order_address:
            params["address"] = self.order_address
        if self.order_pickup_point:
            params["pickup_point"] = self.order_pickup_point

        def _request():
            return self.session.post(
                self.order_url,
                data=params,
                headers=self._headers(),
                timeout=self.order_timeout,
                proxies={"http": None, "https": None},
            )

        # Без повторов: adapter переспросил бы cart/order ещё дважды, а по
        # таймауту чтения заказ у поставщика мог уже создаться — вышел бы дубль.
        try:
            resp = call_order_once(_request)
        except OrderDeliveryUnknown as exc:
            return {
                "success": False,
                "uncertain": True,
                "error": f"Profit-League: ответ на оформление не получен ({exc}); проверьте заказы у поставщика",
            }
        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}: {_decode_response(resp)[:200]}"}
        data = json.loads(_decode_response(resp))
        if isinstance(data, dict) and data.get("status") == "success":
            orders = data.get("orders")
            return {"success": True, "data": f"Profit-League: заказ создан {orders or data}"}
        return {"success": False, "error": data.get("err") if isinstance(data, dict) else str(data)}

    def _clean(self, value):
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
