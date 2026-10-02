import datetime

import requests
import zeep
from zeep.transports import Transport

from provider_adapter import OrderDeliveryUnknown, call_order_once


class RosskoProvider:
    def __init__(
        self,
        key1,
        key2,
        delivery_id="000000001",
        address_id="",
        payment_id="1",
        requisite_id="",
        contact_name="",
        contact_phone="",
        delivery_parts=True,
        timeout=15,
    ):
        self.key1 = key1
        self.key2 = key2
        self.delivery_id = delivery_id or "000000001"
        self.address_id = address_id
        self.payment_id = payment_id or "1"
        self.requisite_id = requisite_id
        self.contact_name = contact_name
        self.contact_phone = contact_phone
        # Оформление пакетом идёт без повторов и дольше поиска, поэтому у него
        # свой таймаут: обрыв здесь означает неизвестный исход заказа.
        self.order_timeout = max(60, int(timeout or 15) * 3)
        self.delivery_parts = delivery_parts
        self.timeout = int(timeout)
        self.search_wsdl = "https://api.rossko.ru/service/v2.1/GetSearch?wsdl"
        self.checkout_wsdl = "https://api.rossko.ru/service/v2.1/GetCheckout?wsdl"
        self.details_wsdl = "https://api.rossko.ru/service/v2.1/GetCheckoutDetails?wsdl"
        self.last_message = ""

    def _client(self, wsdl_url, timeout=None):
        session = requests.Session()
        session.trust_env = False
        timeout = self.timeout if timeout is None else timeout
        transport = Transport(session=session, timeout=timeout)
        return zeep.Client(wsdl=wsdl_url, transport=transport)

    def _auth_params(self):
        return {"KEY1": self.key1, "KEY2": self.key2}

    def _call_with_params(self, service_method, params):
        try:
            return service_method(**params)
        except (TypeError, ValueError):
            return service_method(params)

    def check_connection(self):
        details = self.get_checkout_details()
        if details.get("success"):
            return True, self.checkout_details_message(details)
        return False, details.get("message") or "No checkout details"

    def get_checkout_details(self):
        client = self._client(self.details_wsdl, timeout=min(self.timeout, 10))
        result = client.service.GetCheckoutDetails(self.key1, self.key2)
        if not self._value(result, "success", "Success"):
            message = self._value(result, "message", "Message") or "No checkout details"
            return {"success": False, "message": str(message)}

        return {
            "success": True,
            "deliveries": self._items_from_container(
                self._value(result, "DeliveryType"), "delivery"
            ),
            "payments": self._items_from_container(
                self._value(result, "PaymentType"), "payment"
            ),
            "addresses": self._items_from_container(
                self._value(result, "DeliveryAddress"), "address"
            ),
            "companies": self._items_from_container(
                self._value(result, "CompanyList"), "company"
            ),
        }

    def checkout_defaults(self, details):
        defaults = {}
        delivery = self._first_item(details.get("deliveries"))
        payment = self._first_item(details.get("payments"))
        address = self._first_item(details.get("addresses"))
        company = self._first_item(details.get("companies"))

        if delivery:
            defaults["delivery_id"] = str(self._value(delivery, "id") or "")
        if payment:
            defaults["payment_id"] = str(self._value(payment, "id") or "")
        if address:
            defaults["address_id"] = str(self._value(address, "id") or "")
        if company:
            defaults["requisite_id"] = str(self._value(company, "id") or "")
        return defaults

    def checkout_details_message(self, details):
        return (
            f"OK: доставок {len(details.get('deliveries', []))}, "
            f"оплат {len(details.get('payments', []))}, "
            f"адресов {len(details.get('addresses', []))}, "
            f"реквизитов {len(details.get('companies', []))}"
        )

    def get_prices(self, article, brand=None):
        self.last_message = ""
        if not self.key1 or not self.key2 or not self.delivery_id:
            self.last_message = "KEY1/KEY2/delivery_id are required"
            return []

        text = f"{brand} {article}".strip() if brand else article
        try:
            client = self._client(self.search_wsdl)
            data = client.service.GetSearch(
                self.key1,
                self.key2,
                text,
                self.delivery_id,
                self.address_id or None,
            )
        except Exception as e:
            self.last_message = str(e)
            print(f"Rossko provider error: {e}")
            return []

        success = self._value(data, "success", "Success")
        if success is False:
            self.last_message = str(self._value(data, "message", "Message") or "Search failed")
            return []

        results = []
        for part in self._iter_parts(self._value(data, "PartsList", "partsList")):
            self._append_part_results(results, part, article, is_cross=False)
            crosses = self._value(part, "crosses", "Crosses")
            for cross in self._iter_parts(crosses):
                self._append_part_results(results, cross, article, is_cross=True)
        return results

    def add_to_basket(self, item, quantity=1, comment=""):
        return self.add_to_basket_batch([{"item": item, "quantity": quantity}], comment=comment)[0]

    def add_to_basket_batch(self, rows, comment=""):
        """Один GetCheckout на все позиции: PARTS/Part в схеме и так массив.

        rows — список {"item": предложение, "quantity": количество};
        ответ — список той же длины и в том же порядке.
        """
        rows = list(rows or [])
        if not rows:
            return []
        results = [None] * len(rows)
        if not self.contact_name or not self.contact_phone:
            return [
                {"success": False, "error": "Rossko: fill contact_name and contact_phone in settings"}
                for _ in rows
            ]

        parts = []
        indexes_by_key = {}
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            if not item.get("stock_id"):
                results[index] = {"success": False, "error": "Rossko: нет кода склада для выбранной позиции"}
                continue
            part = {
                "partnumber": item.get("article", ""),
                "brand": item.get("brand", ""),
                "stock": item.get("stock_id", ""),
                "count": quantity,
                "comment": comment[:50] if comment else "",
            }
            indexes_by_key.setdefault(self._part_key(part["partnumber"], part["brand"]), []).append(index)
            parts.append((index, part))

        if not parts:
            return results

        delivery = {"delivery_id": self.delivery_id}
        if self.address_id:
            delivery["address_id"] = self._to_int(self.address_id, None)

        payment = {"payment_id": self._to_int(self.payment_id, 1)}
        if self.requisite_id:
            payment["requisite_id"] = self._to_int(self.requisite_id, None)

        params = {
            **self._auth_params(),
            "delivery": delivery,
            "payment": payment,
            "contact": {
                "name": self.contact_name,
                "phone": self.contact_phone,
                "comment": comment[:255] if comment else "",
            },
            "delivery_parts": bool(self.delivery_parts),
            "PARTS": {"Part": [part for _index, part in parts]},
        }

        sent_indexes = [index for index, _part in parts]
        try:
            client = self._client(self.checkout_wsdl, timeout=self.order_timeout)
            # Без повторов: обрыв после отправки checkout означает не отказ,
            # а неизвестный исход — заказ мог уже создаться.
            result = call_order_once(self._call_with_params, client.service.GetCheckout, params)
        except OrderDeliveryUnknown as exc:
            unknown = {
                "success": False,
                "uncertain": True,
                "error": f"Rossko: ответ на заказ не получен ({exc}); проверьте заказы у поставщика",
            }
            for index in sent_indexes:
                results[index] = dict(unknown)
            return results
        except Exception as exc:
            for index in sent_indexes:
                results[index] = {"success": False, "error": str(exc)}
            return results

        if not self._value(result, "success", "Success"):
            message = (
                self._value(result, "message", "Message")
                or self._extract_item_errors(result)
                or "Rossko checkout error"
            )
            for index in sent_indexes:
                results[index] = {"success": False, "error": str(message)}
            return results

        order_ids = self._extract_order_ids(result)
        data = f"Rossko: заказ создан {order_ids}".strip()
        for index in sent_indexes:
            results[index] = {"success": True, "data": data}

        # Заказ принят, но по отдельным строкам могли прийти отказы. Те, что
        # удалось сопоставить с позицией, помечаем ошибкой поимённо, остальные
        # оставляем текстом в ответе — иначе отказ потеряется в пакете.
        unmatched = []
        for failed in self._item_error_rows(result):
            key = self._part_key(
                self._value(failed, "partnumber", "PartNumber", "part_number"),
                self._value(failed, "brand", "Brand"),
            )
            message = str(self._value(failed, "message", "Message", "error", "Error") or failed)
            matched = [index for index in indexes_by_key.get(key, []) if index in sent_indexes]
            if not matched:
                unmatched.append(message)
                continue
            for index in matched:
                results[index] = {"success": False, "error": f"Rossko: {message}"}
        if unmatched:
            note = "; ".join(unmatched)
            for index in sent_indexes:
                if results[index].get("success"):
                    results[index] = {"success": True, "data": f"{data} | замечания поставщика: {note}"}
        return results

    def _part_key(self, partnumber, brand):
        return self._clean_key(partnumber), self._clean_key(brand)

    def _clean_key(self, value):
        return "".join(char for char in str(value or "").upper() if char.isalnum())

    def _item_error_rows(self, result):
        errors = self._value(result, "ItemsErrorList", "itemsErrorList")
        if not errors:
            return []
        rows = self._value(errors, "ItemError", "itemError", "ItemsError")
        return [row for row in self._as_list(rows if rows is not None else errors) if row is not None]

    def _append_part_results(self, results, part, requested_article, is_cross=False):
        brand = str(self._value(part, "brand") or "")
        partnumber = str(self._value(part, "partnumber") or requested_article)
        name = str(self._value(part, "name") or "")

        for stock in self._iter_stocks(self._value(part, "stocks")):
            price = self._to_float(self._value(stock, "price"))
            if price <= 0:
                continue

            stock_id = str(self._value(stock, "id") or "")
            warehouse_name = str(
                self._value(stock, "description", "name", "warehouse", "title") or ""
            )
            results.append({
                "provider": "Rossko",
                "brand": brand,
                "article": partnumber,
                "price": price,
                "days": self._to_int(self._value(stock, "delivery"), 0),
                "quantity": str(self._value(stock, "count") or "0"),
                "logo": warehouse_name or stock_id or "-",
                "name": name,
                "multiplicity": self._to_int(self._value(stock, "multiplicity"), 1),
                "warehouse": warehouse_name,
                "stock_id": stock_id,
                "rossko_guid": str(self._value(part, "guid") or ""),
                "is_cross": is_cross,
                "delivery_start": self._format_date(self._value(stock, "deliveryStart")),
                "delivery_end": self._format_date(self._value(stock, "deliveryEnd")),
            })

    def _iter_parts(self, container):
        if not container:
            return []
        parts = self._value(container, "Part", "part")
        return self._as_list(parts if parts is not None else container)

    def _iter_stocks(self, container):
        if not container:
            return []
        stocks = self._value(container, "stock")
        return self._as_list(stocks if stocks is not None else container)

    def _items_from_container(self, container, item_name):
        if not container:
            return []
        items = self._value(container, item_name)
        return self._as_list(items if items is not None else container)

    def _first_item(self, items):
        items = self._as_list(items)
        return items[0] if items else None

    def _extract_order_ids(self, result):
        order_ids = self._value(result, "OrderIDS", "order_ids", "orderIDS")
        items = self._as_list(self._value(order_ids, "OrderID", "order_id") or order_ids)
        values = []
        for item in items:
            value = self._value(item, "id", "ID", "order_id", "OrderID") or item
            if value:
                values.append(str(value))
        return ", ".join(values) if values else ""

    def _extract_item_errors(self, result):
        errors = self._value(result, "ItemsErrorList", "itemsErrorList")
        items = self._as_list(self._value(errors, "ItemError", "itemError", "ItemsError") or errors)
        messages = []
        for item in items:
            message = self._value(item, "message", "Message", "error", "Error") or item
            if message:
                messages.append(str(message))
        return "; ".join(messages)

    def _as_list(self, value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def _value(self, obj, *names):
        if obj is None:
            return None
        for name in names:
            if isinstance(obj, dict) and name in obj:
                return obj.get(name)
            if hasattr(obj, name):
                return getattr(obj, name)
        return None

    def _to_float(self, value, default=0.0):
        try:
            return float(str(value).replace(",", "."))
        except Exception:
            return default

    def _to_int(self, value, default=0):
        try:
            return int(float(str(value).replace(",", ".")))
        except Exception:
            return default

    def _format_date(self, value):
        if not value:
            return ""
        if isinstance(value, datetime.datetime):
            return value.isoformat()
        return str(value)
