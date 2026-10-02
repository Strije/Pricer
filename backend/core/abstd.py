import hashlib
import requests
import re
import time
from requests import exceptions as request_exceptions

from provider_adapter import TRANSPORT_EXCEPTIONS, OrderDeliveryUnknown
from response_text import decode_response_text


class AbstdProvider:
    def __init__(self, login, password, agreement_id, cart_id="0", delivery_address_id="", delivery_type_id="1", timeout=10):
        self.login = login
        self.password = password
        self.agreement_id = agreement_id
        self.cart_id = cart_id
        self.delivery_address_id = delivery_address_id
        self.delivery_type_id = str(delivery_type_id or "1")
        self.timeout = int(timeout)
        # Оформление идёт пакетом и без повторов: таймаут длиннее поискового,
        # а обрыв здесь означает неизвестный исход заказа, а не отказ.
        self.order_timeout = max(60, int(timeout) * 3)
        self.base_url = "https://abstd.ru"
        self._auth_hash = None
        self._auth_login = login
        self.last_message = ""

    def _calc_auth(self, login=None):
        auth_login = self.login if login is None else login
        if self._auth_hash and auth_login == self._auth_login:
            return self._auth_hash
        pwd_hash = hashlib.md5(self.password.encode()).hexdigest().lower()
        auth_hash = hashlib.md5((auth_login + pwd_hash).encode()).hexdigest().lower()
        if auth_login == self.login:
            self._auth_hash = auth_hash
            self._auth_login = auth_login
        return auth_hash

    def _auth_logins(self):
        logins = [str(self.login)]
        stripped = str(self.login).lstrip("0")
        if stripped and stripped not in logins:
            logins.append(stripped)
        return logins

    def _get(self, endpoint, params=None, timeout=None, raise_transport=False):
        """raise_transport=True — для создания заказа: обрыв связи там нельзя
        молча считать неудачей, заказ уже мог уйти."""
        self.last_message = ""
        if params is None:
            params = {}
        session = requests.Session()
        session.trust_env = False
        try:
            resp = None
            for auth_login in self._auth_logins():
                request_params = dict(params)
                request_params["auth"] = self._calc_auth(auth_login)
                resp = session.get(
                    f"{self.base_url}/{endpoint}",
                    params=request_params,
                    timeout=float(timeout or self.timeout),
                    proxies={"http": None, "https": None}
                )
                if resp.status_code != 403:
                    if auth_login != self.login:
                        self.last_message = f"Авторизация прошла с логином {auth_login}"
                    break
            if resp.status_code != 200:
                self.last_message = self._format_response_error(resp)
                return None
            data = resp.json()
            if isinstance(data, dict) and data.get("status") not in (None, "OK"):
                self.last_message = str(data.get("status"))
            return data
        except request_exceptions.ConnectTimeout:
            # Соединение не установилось — запрос до ABSTD не дошёл,
            # это обычная неудача даже для заказа.
            self.last_message = "ABSTD не ответил: таймаут подключения"
            return None
        except request_exceptions.ReadTimeout as e:
            self.last_message = "ABSTD не ответил: таймаут чтения"
            if raise_transport:
                raise OrderDeliveryUnknown(str(e) or "таймаут чтения") from e
            return None
        except request_exceptions.SSLError as e:
            self.last_message = f"Ошибка SSL/TLS ABSTD: {str(e)[:80]}"
            if raise_transport:
                raise OrderDeliveryUnknown(str(e) or "ошибка SSL") from e
            return None
        except request_exceptions.ConnectionError as e:
            self.last_message = f"Ошибка соединения с ABSTD: {str(e)[:80]}"
            if raise_transport:
                raise OrderDeliveryUnknown(str(e) or "обрыв соединения") from e
            return None
        except ValueError as e:
            self.last_message = f"ABSTD вернул не JSON: {str(e)[:80]}"
            return None
        except Exception as e:
            self.last_message = str(e)
            return None

    def get_user_context(self):
        return self._get("api-get_user_context", {"format": "json"})

    def is_user_context(self, context):
        if not isinstance(context, dict):
            return False
        return any(key in context for key in (
            "user_agreements",
            "carts",
            "delivery_types",
            "user_delivery_addresses",
        ))

    def check_auth_with_brands(self):
        return self._get("api-brands", {"article": "oc470", "format": "json"})

    def context_defaults(self, context):
        defaults = {}
        agreements = context.get("user_agreements", []) if isinstance(context, dict) else []
        carts = context.get("carts", []) if isinstance(context, dict) else []
        addresses = context.get("user_delivery_addresses", []) if isinstance(context, dict) else []

        if agreements:
            agreement = agreements[0]
            defaults["agreement_id"] = str(agreement.get("ua_id", ""))
        if carts:
            cart = carts[0]
            defaults["cart_id"] = str(cart.get("cart_id", ""))
        if addresses:
            address = addresses[0]
            defaults["delivery_address_id"] = str(address.get("uda_id", ""))
        return defaults

    def context_message(self, context):
        agreements = context.get("user_agreements", []) if isinstance(context, dict) else []
        addresses = context.get("user_delivery_addresses", []) if isinstance(context, dict) else []
        carts = context.get("carts", []) if isinstance(context, dict) else []
        return f"OK: договоров {len(agreements)}, адресов {len(addresses)}, корзин {len(carts)}"

    def _format_response_error(self, resp):
        if resp.status_code == 403:
            return "403: неверный логин/пароль, API-доступ не включен или не тот аккаунт"
        if resp.status_code == 401:
            return "401: неверный логин/пароль"
        try:
            data = resp.json()
            if isinstance(data, dict):
                return str(data.get("status") or data.get("message") or data.get("error") or f"HTTP {resp.status_code}")
        except Exception:
            pass
        text = decode_response_text(resp).strip()
        return f"HTTP {resp.status_code}: {text[:80]}" if text else f"HTTP {resp.status_code}"

    def get_prices(self, article, brand=None):
        params = {
            "article": article,
            "agreement_id": self.agreement_id,
            "show_unavailable": "0",
            "format": "json"
        }
        if brand:
            params["brand"] = brand

        data = self._get("api-search", params)
        if not data or data.get("status") != "OK":
            return []

        items = data.get("data", [])
        results = []
        for item in items:
            price = self._to_float(item.get("price"))
            if price <= 0:
                continue

            delivery_str = item.get("delivery_duration", "0")
            if delivery_str and isinstance(delivery_str, str) and "-" in delivery_str:
                parts = delivery_str.split("-")
                try:
                    days = int(parts[1].strip())
                except:
                    days = int(parts[0].strip())
            else:
                try:
                    days = int(delivery_str)
                except:
                    days = 0

            fail_percent = item.get("fail_percent")
            if fail_percent is not None:
                try:
                    delivery_percent = 100 - int(fail_percent)
                except:
                    delivery_percent = ""
            else:
                delivery_percent = ""

            qty_str = item.get("quantity", "0")
            by_request = item.get("by_request", "0")
            qty_num = self._to_int(qty_str, 0)
            if qty_num <= 0 and by_request != "1":
                continue
            if by_request == "1" and (qty_str == "0" or not qty_str):
                qty_str = "под заказ"

            return_type = item.get("return_type") or {}
            if isinstance(return_type, dict):
                return_type_id = return_type.get("id", "")
                return_type_name = return_type.get("name", "")
            else:
                return_type_id = ""
                return_type_name = str(return_type or "")

            results.append({
                "provider": "ABSTD",
                "brand": item.get("brand", ""),
                "article": item.get("article", ""),
                "price": price,
                "days": days,
                "quantity": qty_str,
                "name": item.get("product_name", ""),
                "multiplicity": self._to_int(item.get("mult_sale"), 1),
                "delivery_percent": delivery_percent,
                "warehouse": item.get("warehouse_name", ""),
                "logo": item.get("warehouse_name", ""),
                "product_id": item.get("product_id", ""),
                "nomenclature_id": item.get("nomenclature_id", ""),
                "warehouse_id": item.get("warehouse_id", ""),
                "return_type_id": return_type_id,
                "return_type_name": return_type_name,
                "delivery_duration": item.get("delivery_duration", ""),
                "delivery_time": item.get("delivery_time", []),
                "delivery_expires": item.get("delivery_expires", ""),
                "quantity_on_the_way": item.get("quantity_on_the_way", ""),
                "by_request": by_request,
                "special_order": item.get("special_order", "0"),
                "updated": item.get("updated", ""),
                "is_cross": item.get("is_cross", 0)
            })
        return results

    def _to_float(self, value, default=0.0):
        if value is None or value == "":
            return default
        try:
            return float(str(value).replace(",", "."))
        except Exception:
            return default

    def _to_int(self, value, default=0):
        if value is None or value == "":
            return default
        try:
            return int(float(str(value).replace(",", ".")))
        except Exception:
            return default

    def get_brands(self, article):
        data = self._get("api-brands", {"article": article, "format": "json"})
        if data and isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("brands", "data", "result"):
                values = data.get(key)
                if isinstance(values, list):
                    return values
        return []

    def add_to_basket(self, item, quantity=1, comment=""):
        return self.create_order(item, quantity, comment)

    def create_order(self, item, quantity=1, comment=""):
        return self.add_to_basket_batch([{"item": item, "quantity": quantity}], comment=comment)[0]

    def add_to_basket_batch(self, rows, comment=""):
        """Один api-create_order на все позиции: prods[...] — словарь товаров.

        ABSTD отвечает построчно (data[product_id][status]), поэтому исход
        раскладываем по позициям: отказ по одному товару не топит остальные.
        rows — список {"item": предложение, "quantity": количество};
        ответ — список той же длины и в том же порядке.
        """
        rows = list(rows or [])
        if not rows:
            return []
        results = [None] * len(rows)
        if not self.delivery_address_id:
            return [{"success": False, "error": "ABSTD: не выбран адрес доставки"} for _ in rows]

        # Один товар — одна запись prods[...]: ключ словаря всё равно один,
        # две строки на одно предложение сложились бы молча.
        merged = {}
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            product_id = str(item.get("product_id") or "").strip()
            if not product_id:
                results[index] = {"success": False, "error": "Нет product_id"}
                continue
            entry = merged.setdefault(product_id, {"item": item, "quantity": 0, "indexes": []})
            entry["quantity"] += quantity
            entry["indexes"].append(index)

        if not merged:
            return results

        product_ids = list(merged)
        params = {
            "ua_id": self.agreement_id,
            "uda_id": self.delivery_address_id,
            "dt_id": self.delivery_type_id,
            "desc": comment,
            "external_id": self._external_id(product_ids[0]) if len(product_ids) == 1
            else self._batch_external_id(product_ids),
            "format": "json",
        }
        for product_id, entry in merged.items():
            params[f"prods[{product_id}]"] = str(entry["quantity"])
            if comment:
                params[f"p_desc[{product_id}]"] = comment
            price = self._to_float(entry["item"].get("price"))
            if price > 0:
                params[f"initial_price[{product_id}]"] = str(price)

        sent_indexes = [index for entry in merged.values() for index in entry["indexes"]]
        try:
            data = self._get(
                "api-create_order",
                params,
                timeout=self.order_timeout,
                raise_transport=True,
            )
        except OrderDeliveryUnknown as exc:
            unknown = {
                "success": False,
                "uncertain": True,
                "error": f"ABSTD: ответ на заказ не получен ({exc}); проверьте заказы у поставщика",
            }
            for index in sent_indexes:
                results[index] = dict(unknown)
            return results

        order_ok = bool(data) and data.get("status") == "OK"
        common_error = self._order_error(data) if not order_ok else ""
        statuses = self._product_statuses(data)
        for product_id, entry in merged.items():
            product_ok, message = statuses.get(product_id, (order_ok, ""))
            for index in entry["indexes"]:
                if order_ok and product_ok:
                    results[index] = {"success": True, "data": data}
                else:
                    results[index] = {
                        "success": False,
                        "error": message or common_error or "ABSTD: заказ не создан",
                    }
        return results

    def _product_statuses(self, data):
        """Построчные статусы заказа: {product_id: (принят, текст ошибки)}."""
        statuses = {}
        details = data.get("data") if isinstance(data, dict) else None
        if not isinstance(details, dict):
            return statuses
        for product_id, row in details.items():
            row_status = row.get("status") if isinstance(row, dict) else None
            if not isinstance(row_status, dict):
                continue
            code = str(row_status.get("code") or "")
            description = str(row_status.get("description") or "")
            accepted = code in ("0", "")
            statuses[str(product_id)] = (
                accepted,
                "" if accepted else f"ABSTD: {description or code}",
            )
        return statuses

    def _batch_external_id(self, product_ids):
        return f"PP{int(time.time())}B{len(list(product_ids))}"[:32]

    def _order_error(self, data):
        if not data:
            return self.last_message or "ABSTD: ошибка создания заказа"
        errors = []
        if isinstance(data, dict):
            status = data.get("status")
            if status and status != "OK":
                errors.append(str(status))
            details = data.get("data")
            if isinstance(details, dict):
                for product_id, row in details.items():
                    row_status = row.get("status") if isinstance(row, dict) else None
                    if isinstance(row_status, dict):
                        code = row_status.get("code")
                        desc = row_status.get("description")
                        if str(code) not in ("0", "") or desc:
                            errors.append(f"{product_id}: {desc or code}")
            result = data.get("result")
            if isinstance(result, list):
                for row in result:
                    if isinstance(row, dict) and row.get("status") == "error":
                        errors.append(str(row.get("status_msg") or row))
        return "; ".join(errors) or "ABSTD: заказ не создан"

    def _external_id(self, product_id):
        return f"PP{int(time.time())}{str(product_id)[-8:]}"[:32]
