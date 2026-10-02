from datetime import datetime
from urllib.parse import urlencode

import requests

from provider_adapter import OrderDeliveryUnknown, call_order_once
from response_text import decode_response_text
from brand_aliases import BrandAliasResolver


class FavoritProvider:
    """Price search provider for Favorit Parts."""

    def __init__(
        self,
        api_key,
        include_analogues=True,
        timeout=12,
        developer_key="",
        trade_point="",
        payment_type="",
        delivery_type="",
        transport_type="",
        brand_aliases=None,
    ):
        self.api_key = str(api_key or "").strip()
        self.developer_key = str(developer_key or "").strip()
        self.trade_point = str(trade_point or "").strip()
        self.payment_type = str(payment_type or "").strip()
        self.delivery_type = str(delivery_type or "").strip()
        self.transport_type = str(transport_type or "").strip()
        self.include_analogues = bool(include_analogues)
        self.timeout = int(timeout)
        self.base_url = "http://api.favorit-parts.ru/hs/hsprice/"
        self.order_base_url = "https://api.favorit-parts.ru/ws/v1"
        # Оформление идёт без повторов и пакетом, поэтому у него свой таймаут:
        # обрыв здесь означает неизвестный исход заказа, а не отказ.
        self.order_timeout = max(60, int(timeout or 12) * 3)
        self.last_message = ""
        self.session = requests.Session()
        self.session.trust_env = False
        # Таблица алиасов весит около 2.8 МБ и строится ~0.5 с, поэтому берём
        # общую из приложения; свою создаём только если её не передали.
        self.brand_aliases = brand_aliases if brand_aliases is not None else BrandAliasResolver()

    def _request(self, article, brand=None, analogues=False):
        params = {
            "key": self.api_key,
            "number": str(article or "").strip(),
            "info": "on",
        }
        if brand:
            params["brand"] = str(brand).strip()
        if analogues and brand:
            params["analogues"] = "on"
        response = self.session.get(
            self.base_url,
            params=params,
            timeout=self.timeout,
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            self.last_message = str(data["error"])
            return []
        self.last_message = ""
        if isinstance(data, dict):
            return data.get("goods") or []
        return data if isinstance(data, list) else []

    def get_prices(self, article, brand=None):
        article = str(article or "").strip()
        if not article:
            return []
        goods = self._request(
            article,
            brand=brand,
            analogues=self.include_analogues and bool(brand),
        )
        if not goods and self.include_analogues and brand:
            discovered = self.get_brands(article)
            matched = next(
                (name for name in discovered if self.brand_aliases.same(name, brand)),
                None,
            )
            if matched and matched != brand:
                goods = self._request(article, brand=matched, analogues=True)
        results = []
        seen = set()
        for item in goods:
            self._append_item(results, seen, item, article, brand, is_cross=False)
            if self.include_analogues:
                for analogue in item.get("analogues") or []:
                    self._append_item(results, seen, analogue, article, brand, is_cross=True)
        return results

    def get_brands(self, article):
        result = []
        for item in self.get_brand_candidates(article):
            brand = item.get("brand")
            if brand and brand not in result:
                result.append(brand)
        return result

    def get_brand_candidates(self, article):
        goods = self._request(article, brand=None, analogues=False)
        result = []
        seen = set()
        for item in goods:
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").strip()
            if not brand:
                continue
            number = str(item.get("number") or article or "").strip()
            name = str(item.get("name") or "").strip()
            marker = (brand.upper(), _key(number), name.upper())
            if marker in seen:
                continue
            seen.add(marker)
            result.append({
                "brand": brand,
                "article": number,
                "name": name,
                "source": "hsprice",
            })
        return result

    def _append_item(self, results, seen, item, requested_article, requested_brand, is_cross):
        if not isinstance(item, dict):
            return
        article = str(item.get("number") or "").strip()
        brand = str(item.get("brand") or "").strip()
        goods_id = str(item.get("goodsID") or "")
        item_no_return = bool(item.get("notRefund", False))
        for warehouse in item.get("warehouses") or []:
            if not isinstance(warehouse, dict):
                continue
            stock = _int(warehouse.get("stock"))
            if stock <= 0:
                continue
            warehouse_id = str(warehouse.get("id") or "")
            marker = (goods_id, warehouse_id, article, brand)
            if marker in seen:
                continue
            seen.add(marker)
            warehouse_name = str(warehouse.get("code") or warehouse_id or "-")
            results.append(
                {
                    "provider": "Фаворит",
                    "brand": brand,
                    "article": article,
                    "name": str(item.get("name") or ""),
                    "price": _float(warehouse.get("price")),
                    "quantity": str(stock),
                    "days": _delivery_days(warehouse.get("shipmentDate")),
                    "multiplicity": max(1, _int(item.get("rate") or 1)),
                    "warehouse": warehouse_name,
                    "logo": warehouse_name,
                    "goods_id": goods_id,
                    "warehouse_id": warehouse_id,
                    "not_returnable": item_no_return or bool(warehouse.get("notRefund", False)),
                    "is_cross": bool(
                        is_cross
                        or (_key(article) != _key(requested_article))
                        or (requested_brand and _key(brand) != _key(requested_brand))
                    ),
                }
            )

    def check_connection(self):
        if not self.api_key:
            return False, "Укажите API-ключ"
        try:
            self._request("OC727")
            if self.last_message:
                return False, self.last_message
            if not self.developer_key:
                return True, "Поиск OK; для отправки заказов нужен ключ разработчика"
            return True, "Подключение успешно"
        except requests.RequestException as exc:
            return False, f"Ошибка HTTP: {exc}"
        except ValueError as exc:
            return False, f"Некорректный ответ API: {exc}"

    def add_to_basket(self, item, quantity=1, comment=""):
        return self.create_order(item, quantity, comment)

    def create_order(self, item, quantity=1, comment=""):
        return self.add_to_basket_batch([{"item": item, "quantity": quantity}], comment=comment)[0]

    def add_to_basket_batch(self, rows, comment=""):
        """Кладёт позиции в корзину и оформляет их заказами по складам отгрузки.

        GoodsList в /order/ — массив, но WarehouseShipping и ShippingDate у
        Фаворита относятся ко всему заказу, а не к строке. Поэтому пакет режем
        по паре «склад отгрузки + дата»: внутри группы один запрос на все
        позиции, между группами — по запросу на группу.
        rows — список {"item": предложение, "quantity": количество};
        ответ — список той же длины и в том же порядке.
        """
        rows = list(rows or [])
        if not rows:
            return []
        results = [None] * len(rows)
        if not self.developer_key:
            return [
                {"success": False, "error": "Фаворит: для оформления заказа нужен ключ разработчика"}
                for _ in rows
            ]

        # Две строки заказа могут указывать на одно предложение. В корзину такое
        # кладём одной записью с суммарным количеством: иначе второй cart/add
        # удвоит остаток, а строка корзины всё равно будет одна.
        merged = {}
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            goods_id = str(item.get("goods_id") or "").strip()
            warehouse_id = str(item.get("warehouse_id") or "").strip()
            if not goods_id or not warehouse_id:
                results[index] = {
                    "success": False,
                    "error": "Нет идентификатора товара или склада Фаворит",
                }
                continue
            key = (goods_id, warehouse_id)
            entry = merged.setdefault(key, {"item": item, "quantity": 0, "indexes": []})
            entry["quantity"] += quantity
            entry["indexes"].append(index)

        if not merged:
            return results

        for key, entry in list(merged.items()):
            added = self.add_to_remote_cart_only(entry["item"], entry["quantity"], comment)
            if not added.get("success"):
                for index in entry["indexes"]:
                    results[index] = dict(added)
                merged.pop(key, None)
        if not merged:
            return results

        try:
            cart_data = self.get_cart()
            profile = self.get_profile()
        except requests.RequestException as exc:
            for entry in merged.values():
                for index in entry["indexes"]:
                    results[index] = {"success": False, "error": str(exc)}
            return results

        groups = {}
        for (goods_id, warehouse_id), entry in merged.items():
            cart_row = self._find_cart_row(cart_data, goods_id, warehouse_id)
            if not cart_row:
                for index in entry["indexes"]:
                    results[index] = {
                        "success": False,
                        "error": "Фаворит: позиция не найдена в корзине после добавления",
                    }
                continue
            shipping_date = _shipment_date(cart_row.get("dateShipment"))
            warehouse_shipping = str(cart_row.get("warehouseShipping") or "").strip()
            if not shipping_date or not warehouse_shipping:
                for index in entry["indexes"]:
                    results[index] = {
                        "success": False,
                        "error": "Фаворит: в корзине нет даты или склада отгрузки",
                    }
                continue
            group = groups.setdefault((warehouse_shipping, shipping_date), {"goods": [], "indexes": []})
            group["goods"].append({
                "Goods": str(cart_row.get("goods") or goods_id),
                "WarehouseGroup": str(cart_row.get("warehouseGroup") or warehouse_id),
                # Количество берём своё, а не из корзины: в корзине мог остаться
                # хвост с прошлой отправки, и заказ ушёл бы на лишнее.
                "Count": int(entry["quantity"]),
                "Comment": str(cart_row.get("comment") or comment or ""),
            })
            group["indexes"].extend(entry["indexes"])

        for (warehouse_shipping, shipping_date), group in groups.items():
            try:
                payload = self._order_payload(
                    profile, warehouse_shipping, shipping_date, group["goods"], comment
                )
            except ValueError as exc:
                for index in group["indexes"]:
                    results[index] = {"success": False, "error": str(exc)}
                continue
            result = self._post_order(payload)
            for index in group["indexes"]:
                results[index] = dict(result)

        return [
            result or {"success": False, "error": "Фаворит: позиция не попала в заказ"}
            for result in results
        ]

    def _post_order(self, payload):
        def _request():
            return self.session.post(
                f"{self.order_base_url}/order/",
                params=self._auth_params(),
                json=payload,
                timeout=self.order_timeout,
                proxies={"http": None, "https": None},
                headers={"Content-Type": "application/json"},
            )

        try:
            # Без повторов: по обрыву ответа заказ у Фаворита мог уже создаться.
            response = call_order_once(_request)
        except OrderDeliveryUnknown as exc:
            return {
                "success": False,
                "uncertain": True,
                "error": f"Фаворит: ответ на заказ не получен ({exc}); проверьте заказы у поставщика",
            }
        except requests.RequestException as exc:
            return {"success": False, "error": str(exc)}
        if response.status_code != 200:
            return {"success": False, "error": _response_error(response)}
        try:
            data = response.json()
        except ValueError:
            data = decode_response_text(response)
        return {"success": True, "data": data}

    def add_to_remote_cart_only(self, item, quantity=1, comment=""):
        goods_id = str(item.get("goods_id") or "").strip()
        warehouse_id = str(item.get("warehouse_id") or "").strip()
        if not goods_id or not warehouse_id:
            return {"success": False, "error": "Нет идентификатора товара или склада Фаворит"}
        params = {
            "key": self.api_key,
            "goods": goods_id,
            "warehouseGroup": warehouse_id,
            "count": int(quantity),
            "comment": str(comment or "")[:100],
        }
        if self.developer_key:
            params["developerKey"] = self.developer_key
        try:
            response = self.session.get(
                f"{self.order_base_url}/cart/add/",
                params=urlencode(params, encoding="cp1251"),
                timeout=self.timeout,
                proxies={"http": None, "https": None},
            )
            if response.status_code == 200:
                return {"success": True, "data": response.json() if response.content else None}
            return {"success": False, "error": _response_error(response)}
        except requests.RequestException as exc:
            return {"success": False, "error": str(exc)}

    def get_cart(self):
        response = self.session.get(
            f"{self.order_base_url}/cart/",
            params=self._auth_params(),
            timeout=self.timeout,
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        return response.json()

    def get_profile(self):
        response = self.session.get(
            f"{self.order_base_url}/references/profile/",
            params=self._auth_params(),
            timeout=self.timeout,
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        return response.json()

    def _auth_params(self):
        params = {"key": self.api_key}
        if self.developer_key:
            params["developerKey"] = self.developer_key
        return params

    def _find_cart_row(self, cart_data, goods_id, warehouse_id):
        rows = []
        if isinstance(cart_data, dict):
            rows = cart_data.get("cart") or cart_data.get("Cart") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("goods") or "") == goods_id and str(row.get("warehouseGroup") or "") == warehouse_id:
                return row
        return None

    def _build_order_payload(self, profile, cart_row, comment):
        shipping_date = _shipment_date(cart_row.get("dateShipment"))
        warehouse_shipping = str(cart_row.get("warehouseShipping") or "").strip()
        if not shipping_date or not warehouse_shipping:
            raise ValueError("Фаворит: в корзине нет даты или склада отгрузки")
        goods = [{
            "Goods": str(cart_row.get("goods") or ""),
            "WarehouseGroup": str(cart_row.get("warehouseGroup") or ""),
            "Count": _int(cart_row.get("count") or 1),
            "Comment": str(cart_row.get("comment") or comment or ""),
        }]
        return self._order_payload(profile, warehouse_shipping, shipping_date, goods, comment)

    def _order_payload(self, profile, warehouse_shipping, shipping_date, goods_list, comment):
        trade_point = self.trade_point or self._profile_value(profile, "tradePointDef", "tradePoint", "TradePoint")
        if not trade_point:
            trade_points = profile.get("tradePoints") if isinstance(profile, dict) else None
            if isinstance(trade_points, dict) and trade_points:
                trade_point = next(iter(trade_points.keys()))
        if not trade_point:
            raise ValueError("Фаворит: в профиле не найдена торговая точка")

        delivery_type = _int(self.delivery_type or self._profile_value(profile, "deliveryType", "DeliveryType") or 2)
        transport_type = _int(self.transport_type or self._profile_value(profile, "transportType", "TransportType") or (0 if delivery_type == 2 else 1))
        return {
            "WarehouseShipping": str(warehouse_shipping),
            "ShippingDate": shipping_date,
            "TradePoint": str(trade_point),
            "PaymentType": _int(self.payment_type or self._profile_value(profile, "paymentType", "PaymentType") or 1),
            "DeliveryType": delivery_type,
            "TransportType": transport_type,
            "Comment": str(comment or ""),
            "GoodsList": list(goods_list or []),
        }

    def _profile_value(self, profile, *keys):
        if not isinstance(profile, dict):
            return None
        for key in keys:
            value = profile.get(key)
            if value not in (None, ""):
                return value
        return None


def _delivery_days(value):
    if not value:
        return 0
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            target = datetime.strptime(text[:19], fmt)
            return max(0, (target.date() - datetime.now().date()).days)
        except ValueError:
            continue
    return 0


def _float(value):
    try:
        return float(str(value or "0").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _int(value):
    try:
        return int(float(str(value or "0").replace(",", ".")))
    except (TypeError, ValueError):
        return 0


def _shipment_date(value):
    if not value:
        return ""
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:19], fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _key(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _response_error(response):
    text = decode_response_text(response).strip()
    return text[:500] or f"HTTP {response.status_code}"
