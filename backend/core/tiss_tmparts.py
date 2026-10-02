import datetime

import requests

from provider_adapter import TRANSPORT_EXCEPTIONS, OrderDeliveryUnknown

from response_text import decode_response_text


class TissTmpartsProvider:
    """TISS provider for the new external API at api.tiss.ru."""

    def __init__(
        self,
        api_key,
        warehouse_mode=0,
        timeout=6,
        legal_organization_id="",
        contract_id="",
        outlet_id="",
        warehouses="",
        include_analogues=True,
        delivery_type="ToOutlet",
        phone_number="",
        one_time_delivery=True,
        not_group_reserves=False,
        express_delivery=False,
    ):
        self.api_key = str(api_key or "").strip()
        self.warehouse_mode = 1 if int(warehouse_mode or 0) == 1 else 0
        self.timeout = int(timeout)
        # Оформление идёт пакетом и без повторов: таймаут длиннее поискового,
        # а обрыв здесь означает неизвестный исход заказа, а не отказ.
        self.order_timeout = max(60, int(timeout) * 3)
        self.legal_organization_id = str(legal_organization_id or "").strip()
        self.contract_id = str(contract_id or "").strip()
        self.outlet_id = str(outlet_id or "").strip()
        self.warehouses = _split_values(warehouses)
        self.include_analogues = bool(include_analogues)
        self.delivery_type = str(delivery_type or "ToOutlet").strip() or "ToOutlet"
        self.phone_number = str(phone_number or "").strip()
        self.one_time_delivery = bool(one_time_delivery)
        self.not_group_reserves = bool(not_group_reserves)
        self.express_delivery = bool(express_delivery)
        self.base_url = "https://api.tiss.ru/external"
        self.last_message = ""
        self.session = requests.Session()
        self.session.trust_env = False

    @property
    def headers(self):
        return {
            "X-External-Api-Key": self.api_key,
            "Accept": "application/json",
            "Accept-Language": "ru-RU",
        }

    def _request(self, method, endpoint, params=None, payload=None, timeout=None, raise_transport=False):
        """raise_transport=True — для оформления заказа: обрыв связи там нельзя
        молча считать неудачей, заказ уже мог уйти."""
        self.last_message = ""
        if not self.api_key:
            self.last_message = "Укажите API-ключ"
            return None
        try:
            response = self.session.request(
                method,
                f"{self.base_url}/{endpoint.lstrip('/')}",
                params=params or {},
                json=payload,
                headers=self.headers,
                timeout=float(timeout or self.timeout),
                proxies={"http": None, "https": None},
            )
            if response.status_code in (401, 403):
                self.last_message = "TISS: неверный API-ключ или нет доступа"
                return None
            if response.status_code != 200:
                self.last_message = f"HTTP {response.status_code}: {self._error_message(response)}"
                return None
            return response.json()
        except requests.exceptions.Timeout as exc:
            self.last_message = "TISS не ответил: таймаут"
            if raise_transport:
                raise OrderDeliveryUnknown(str(exc) or "таймаут") from exc
            return None
        except requests.exceptions.RequestException as exc:
            self.last_message = f"Ошибка соединения с TISS: {str(exc)[:120]}"
            if raise_transport and isinstance(exc, TRANSPORT_EXCEPTIONS):
                raise OrderDeliveryUnknown(str(exc) or exc.__class__.__name__) from exc
            return None
        except ValueError as exc:
            self.last_message = f"TISS вернул не JSON: {str(exc)[:120]}"
            return None

    def get_legal_organizations(self):
        data = self._request("GET", "v1/legal-organizations")
        return data.get("items") if isinstance(data, dict) else (data or [])

    def get_contracts(self, legal_organization_id=""):
        legal_organization_id = str(legal_organization_id or self.legal_organization_id or "").strip()
        params = {}
        if legal_organization_id:
            params["legalOrganizationId"] = legal_organization_id
        data = self._request("GET", "v1/contracts", params=params)
        return data if isinstance(data, list) else []

    def _error_message(self, response):
        try:
            data = response.json()
        except ValueError:
            return decode_response_text(response, 160).strip()
        messages = []
        for item in data.get("messages") or [] if isinstance(data, dict) else []:
            if isinstance(item, str):
                messages.append(item)
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict):
                    prop = item.get("propertyName") or item.get("PropertyName") or ""
                    msg = item.get("errorMessage") or item.get("ErrorMessage") or ""
                    if msg:
                        messages.append(f"{prop}: {msg}" if prop else msg)
                elif isinstance(item, str):
                    messages.append(item)
        if isinstance(errors, dict):
            for key, value in errors.items():
                if isinstance(value, list):
                    messages.extend(f"{key}: {msg}" for msg in value)
                elif value:
                    messages.append(f"{key}: {value}")
        return "; ".join(messages)[:160] or str(data)[:160]

    def get_outlets(self, contract_id=""):
        params = {}
        if contract_id:
            params["contractId"] = contract_id
        data = self._request("GET", "v1/outlets", params=params)
        return data if isinstance(data, list) else []

    def get_warehouses(self, outlet_id=""):
        outlet_id = str(outlet_id or self.outlet_id or "").strip()
        if not outlet_id:
            return []
        data = self._request("GET", f"v1/users/profile/warehouses/{outlet_id}")
        return data if isinstance(data, list) else []

    def get_brands(self, article):
        article = str(article or "").strip()
        if not article:
            return []
        payload = {"productCode": article, "isList": False}
        data = self._request("POST", "v1/product-offers/brand-by-product", payload=payload)
        result = []
        for item in data if isinstance(data, list) else []:
            brand = _value(item, "brandName", "name")
            if brand and brand not in result:
                result.append(str(brand))
        if 0 < len(result) <= 30:
            return result
        if len(result) > 30:
            result = []

        data = self._request(
            "GET",
            "v1/brands",
            params={"productCode": article, "withoutAlternativeNames": False},
        )
        items = data.get("item") if isinstance(data, dict) else []
        for item in items or []:
            brand = _value(item, "name")
            if brand and brand not in result:
                result.append(str(brand))
            if len(result) > 30:
                self.last_message = "TISS: справочник брендов слишком широкий, уточнение пропущено"
                return []
        return result

    def get_prices(self, article, brand=None):
        article = str(article or "").strip()
        if not article:
            return []
        if not self.contract_id or not self.outlet_id:
            self.last_message = "TISS: выберите договор и точку доставки"
            return []

        brands = [str(brand).strip()] if brand else self.get_brands(article)
        products = [
            {"productCode": article, "brandName": found_brand}
            for found_brand in brands
            if found_brand
        ]
        if not products:
            return []

        payload = {
            "products": products,
            "contractId": self.contract_id,
            "outletId": self.outlet_id,
            "priceFrom": None,
            "priceTo": None,
            "deliveryMinDays": None,
            "deliveryMaxDays": None,
            "offersMaxNum": 200,
            "orderByPrice": True,
            "enableAnalog": self.include_analogues,
            "warehouses": self.warehouses or None,
            "isInStockInHomeWarehousesOnly": self.warehouse_mode == 1,
        }
        data = self._request("POST", "v1/product-offers/by-brand-and-product-code", payload=payload)
        return self._normalize_offers(data, article, brand or "")

    def _normalize_offers(self, data, requested_article, requested_brand):
        results = []
        seen = set()
        containers = data.values() if isinstance(data, dict) else []
        for container in containers:
            for item in _as_list(_value(container, "items")):
                if not isinstance(item, dict):
                    continue
                article = _value(item, "displayProductCode", "productCode") or requested_article
                brand = _value(item, "brandName") or requested_brand
                name = _value(item, "productName") or ""
                product_id = _value(item, "productId") or ""
                block_type = str(_value(item, "offeringBlockType") or "")
                is_cross = block_type in ("AnalogProduct", "AnalogOnOrderProduct")
                if _key(article) != _key(requested_article):
                    is_cross = True
                if requested_brand and _key(brand) != _key(requested_brand):
                    is_cross = True

                for offer in _as_list(_value(item, "offers")):
                    if not isinstance(offer, dict):
                        continue
                    price = _float(_value(offer, "price"))
                    qty = _int(_value(offer, "amount"))
                    if price <= 0:
                        continue
                    delivery_info = _value(offer, "deliveryInfo") or {}
                    warehouse_id = str(_value(offer, "warehouseId") or "")
                    warehouse_name = str(_value(offer, "warehouseName") or "")
                    price_template_id = str(_value(offer, "priceTemplateId") or "")
                    delivery_days = _int(_value(delivery_info, "workDays"))
                    unique_key = (
                        _key(brand),
                        _key(article),
                        round(price, 2),
                        qty,
                        delivery_days,
                        warehouse_id or warehouse_name,
                    )
                    if unique_key in seen:
                        continue
                    seen.add(unique_key)
                    results.append(
                        {
                            "provider": "TISS",
                            "brand": str(brand or ""),
                            "article": str(article or ""),
                            "name": str(name or ""),
                            "price": price,
                            "quantity": str(qty),
                            "days": delivery_days,
                            "multiplicity": max(1, _int(_value(offer, "minPackSize")) or 1),
                            "warehouse": warehouse_name or warehouse_id or "-",
                            "logo": warehouse_name or warehouse_id or "-",
                            "warehouse_id": warehouse_id,
                            "price_template_id": price_template_id,
                            "offer_id": str(
                                price_template_id
                                or _value(offer, "priceTemplateUniqueCode")
                                or ""
                            ),
                            "source_id": warehouse_id or price_template_id,
                            "source_type": 0 if warehouse_id else 1,
                            "product_id": str(product_id),
                            "is_cross": is_cross,
                            "delivery_date": str(_value(delivery_info, "date") or ""),
                            "delivery_time_frame": str(_value(delivery_info, "timeFrame") or ""),
                            "expected_amount": _value(offer, "expectedAmount"),
                            "expected_arrival_date": _value(offer, "expectedArrivalDate"),
                            "is_markdown": bool(_value(item, "isMarkdown")),
                            "offering_block_type": block_type,
                        }
                    )
        return results

    def check_connection(self):
        if not self.api_key:
            return False, "Укажите API-ключ"
        legal = self.get_legal_organizations()
        if legal is None:
            return False, self.last_message or "TISS не ответил"
        contracts = self.get_contracts()
        if not contracts:
            return False, self.last_message or "TISS: договоры не найдены"
        return True, f"Договоров: {len(contracts)}"

    def add_to_basket(self, item, quantity=1, comment=""):
        return self.add_to_basket_batch([{"item": item, "quantity": quantity}], comment=comment)[0]

    def add_to_basket_batch(self, rows, comment=""):
        """Один POST v1/ordering/orders на все позиции: items в схеме и так массив.

        TISS отвечает объектом заказа без построчных статусов, поэтому исход
        у пакета общий: заказ либо создан целиком, либо не создан.
        rows — список {"item": предложение, "quantity": количество};
        ответ — список той же длины и в том же порядке.
        """
        rows = list(rows or [])
        if not rows:
            return []
        results = [None] * len(rows)
        if not self.contract_id or not self.outlet_id:
            return [{"success": False, "error": "TISS: выберите договор и точку доставки"} for _ in rows]
        if not self.phone_number:
            return [{"success": False, "error": "TISS: укажите телефон для заказа"} for _ in rows]

        # Две строки заказа могут указывать на одно предложение: в items кладём
        # одну запись с суммарным количеством, ответ раскладываем на обе строки.
        merged = {}
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            product_id = str(item.get("product_id") or "").strip()
            source_id = str(item.get("source_id") or "").strip()
            if not product_id or not source_id:
                results[index] = {"success": False, "error": "TISS: нет product_id/source_id для позиции"}
                continue
            entry = merged.setdefault(
                (product_id, source_id),
                {"item": item, "quantity": 0, "indexes": []},
            )
            entry["quantity"] += quantity
            entry["indexes"].append(index)

        if not merged:
            return results

        payload = {
            "contractId": self.contract_id,
            "outletId": self.outlet_id,
            "deliveryType": self.delivery_type,
            "expressDelivery": self.express_delivery,
            "oneTimeDelivery": self.one_time_delivery,
            "phoneNumber": self.phone_number,
            "comment": str(comment or "")[:255],
            "notGroupReserves": self.not_group_reserves,
            "items": [
                {
                    "productId": product_id,
                    "sourceType": int(entry["item"].get("source_type", 0) or 0),
                    "sourceId": source_id,
                    "actualPrice": _float(entry["item"].get("price")),
                    "amountInCart": int(entry["quantity"]),
                    "usedBonuses": None,
                    "comment": str(comment or "")[:255],
                }
                for (product_id, source_id), entry in merged.items()
            ],
        }

        sent_indexes = [index for entry in merged.values() for index in entry["indexes"]]
        try:
            data = self._request(
                "POST",
                "v1/ordering/orders",
                payload=payload,
                timeout=self.order_timeout,
                raise_transport=True,
            )
        except OrderDeliveryUnknown as exc:
            unknown = {
                "success": False,
                "uncertain": True,
                "error": f"TISS: ответ на заказ не получен ({exc}); проверьте заказы у поставщика",
            }
            for index in sent_indexes:
                results[index] = dict(unknown)
            return results

        if data:
            created = {"success": True, "data": f"TISS: заказ создан {data}"}
            for index in sent_indexes:
                results[index] = dict(created)
            return results
        error = {"success": False, "error": self.last_message or "TISS: заказ не создан"}
        for index in sent_indexes:
            results[index] = dict(error)
        return results


def _value(obj, *names):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj.get(name)
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _split_values(value):
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").replace(";", ",").split(",")
    return [str(item).strip() for item in raw if str(item).strip()]


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


def _key(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
