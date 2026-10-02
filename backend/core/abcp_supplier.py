import hashlib
import math
import re

import requests

from provider_adapter import TRANSPORT_EXCEPTIONS, OrderDeliveryUnknown
from response_text import decode_response_text


class AbcpSupplierProvider:
    """Поставщик на платформе ABCP (сайт поставщика, клиентский API).

    У всех сайтов на ABCP один и тот же API на хосте вида
    idNNNNN.public.api.abcp.ru: search/brands, search/articles, basket/*,
    orders. Авторизация — userlogin + md5(пароля) в каждом запросе.

    Каждый поставщик в «Проценке» — свой подкласс (см. abcp_supplier_class),
    потому что программа различает поставщиков по имени класса.
    """

    DISPLAY_NAME = "ABCP"

    def __init__(
        self,
        host,
        login,
        password,
        payment_method="",
        shipment_method="",
        shipment_address="0",
        shipment_office="",
        include_analogues=True,
        timeout=10,
        max_brands=5,
    ):
        self.host = _normalize_host(host)
        self.login = str(login or "").strip()
        self.password = str(password or "").strip()
        self.payment_method = str(payment_method or "").strip()
        self.shipment_method = str(shipment_method or "").strip()
        self.shipment_address = str(shipment_address if shipment_address not in (None, "") else "0").strip()
        self.shipment_office = str(shipment_office or "").strip()
        self.include_analogues = bool(include_analogues)
        self.timeout = int(timeout or 10)
        self.order_timeout = max(60, self.timeout * 3)
        self.max_brands = max(1, int(max_brands or 5))
        self.last_message = ""
        self.session = requests.Session()
        self.session.trust_env = False

    @property
    def name(self):
        return self.DISPLAY_NAME

    # ---------- транспорт ----------

    def _password_hash(self):
        if re.fullmatch(r"[0-9a-fA-F]{32}", self.password or ""):
            return self.password.lower()
        return hashlib.md5(self.password.encode("utf-8")).hexdigest()

    def _request(self, method, path, params=None, data=None, timeout=None, raise_transport=False):
        self.last_message = ""
        if not self.host or not self.login or not self.password:
            self.last_message = f"{self.name}: укажите адрес API, логин и пароль"
            return None
        auth = {"userlogin": self.login, "userpsw": self._password_hash()}
        try:
            if method == "GET":
                response = self.session.get(
                    f"{self.host}/{path}",
                    params={**auth, **(params or {})},
                    headers={"Accept": "application/json"},
                    timeout=float(timeout or self.timeout),
                    proxies={"http": None, "https": None},
                )
            else:
                response = self.session.post(
                    f"{self.host}/{path}",
                    data=list(auth.items()) + list(data or []),
                    headers={"Accept": "application/json"},
                    timeout=float(timeout or self.timeout),
                    proxies={"http": None, "https": None},
                )
        except requests.exceptions.Timeout as exc:
            self.last_message = f"{self.name}: не ответил (таймаут)"
            if raise_transport:
                raise OrderDeliveryUnknown(str(exc) or "таймаут") from exc
            return None
        except requests.exceptions.RequestException as exc:
            self.last_message = f"{self.name}: ошибка соединения: {str(exc)[:120]}"
            if raise_transport and isinstance(exc, TRANSPORT_EXCEPTIONS):
                raise OrderDeliveryUnknown(str(exc) or exc.__class__.__name__) from exc
            return None
        try:
            payload = response.json()
        except ValueError:
            self.last_message = (
                f"{self.name}: HTTP {response.status_code}: " + decode_response_text(response, 160).strip()
            )
            return None
        error = _error_message(payload)
        if response.status_code != 200 or error:
            # ABCP отдаёт ошибки и с кодом 200, и с 4xx: {"errorCode": 102, "errorMessage": ...}
            self.last_message = f"{self.name}: {error or 'HTTP ' + str(response.status_code)}"[:200]
            # У basket/order ошибка не значит, что заказ не создан: отдаём тело.
            if path == "basket/order" and isinstance(payload, dict):
                return payload
            return None
        return payload

    # ---------- поиск ----------

    def get_brands(self, article):
        article = str(article or "").strip()
        if not article:
            return []
        data = self._request("GET", "search/brands", {"number": article})
        rows = data.values() if isinstance(data, dict) else (data or [])
        brands = []
        for row in rows:
            brand = str((row or {}).get("brand") or "").strip() if isinstance(row, dict) else ""
            if brand and brand not in brands:
                brands.append(brand)
        return brands

    def get_prices(self, article, brand=None):
        article = str(article or "").strip()
        if not article:
            return []
        brands = [str(brand).strip()] if brand else self.get_brands(article)[: self.max_brands]
        results = []
        for found_brand in [b for b in brands if b]:
            data = self._request(
                "GET",
                "search/articles",
                {
                    "number": article,
                    "brand": found_brand,
                    "useOnlineStocks": 1,
                    "withOutAnalogs": 0 if self.include_analogues else 1,
                },
            )
            results.extend(self._normalize_offers(data, article, found_brand))
        return results

    def _normalize_offers(self, data, requested_article, requested_brand):
        rows = data.values() if isinstance(data, dict) else (data or [])
        results = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            price = _float(row.get("price"))
            if price <= 0:
                continue
            article = str(row.get("number") or requested_article)
            brand = str(row.get("brand") or requested_brand)
            is_cross = _truthy(row.get("isAnalog")) or _key(article) != _key(requested_article) or (
                bool(requested_brand) and _key(brand) != _key(requested_brand)
            )
            if is_cross and not self.include_analogues:
                continue
            supplier_code = str(row.get("supplierCode") or "").strip()
            item_key = str(row.get("itemKey") or "").strip()
            hours_min = _int(row.get("deliveryPeriod"))
            hours_max = _int(row.get("deliveryPeriodMax")) or hours_min
            warehouse = _warehouse_name(row)
            marker = (_key(brand), _key(article), supplier_code, item_key, round(price, 2))
            if marker in seen:
                continue
            seen.add(marker)
            name = str(row.get("description") or "")
            if _truthy(row.get("isUsed")):
                name = (name + " (б/у)").strip()
            results.append(
                {
                    "provider": self.name,
                    "brand": brand,
                    "article": article,
                    "name": name,
                    "price": price,
                    "quantity": _availability(row.get("availability")),
                    "days": int(math.ceil(hours_max / 24)) if hours_max else 0,
                    "delivery_hours": hours_max,
                    "multiplicity": max(1, _int(row.get("packing")) or 1),
                    "warehouse": warehouse,
                    "logo": warehouse,
                    "supplier_code": supplier_code,
                    "item_key": item_key,
                    "code": str(row.get("code") or ""),
                    "offer_id": f"{supplier_code}:{item_key}" if item_key else supplier_code,
                    "not_returnable": _truthy(row.get("noReturn")),
                    "delivery_probability": row.get("deliveryProbability"),
                    "is_cross": is_cross,
                }
            )
        return results

    # ---------- настройки заказа ----------

    def get_order_options(self):
        """Списки для настроек: способы оплаты, доставки, адреса, офисы."""
        options = {}
        for key, path in (
            ("payment_method", "basket/paymentMethods"),
            ("shipment_method", "basket/shipmentMethods"),
            ("shipment_address", "basket/shipmentAddresses"),
            ("shipment_office", "basket/shipmentOffices"),
        ):
            data = self._request("GET", path)
            rows = data.values() if isinstance(data, dict) else (data or [])
            options[key] = [
                (str(row.get("id")), str(row.get("name") or row.get("id")))
                for row in rows
                if isinstance(row, dict) and row.get("id") not in (None, "")
            ]
        return options

    def check_connection(self):
        brands = self._request("GET", "search/brands", {"number": "01089"})
        if brands is None:
            return False, self.last_message or f"{self.name}: не ответил"
        options = self.get_order_options()
        parts = [f"оплата {len(options['payment_method'])}", f"доставка {len(options['shipment_method'])}",
                 f"адреса {len(options['shipment_address'])}"]
        return True, "Подключено: " + ", ".join(parts)

    # ---------- заказ ----------

    def _basket_content(self):
        data = self._request("GET", "basket/content")
        if data is None:
            return None
        rows = data.values() if isinstance(data, dict) else data
        return [row for row in rows or [] if isinstance(row, dict)]

    def add_to_basket(self, item, quantity=1, comment=""):
        return self.add_to_basket_batch([{"item": item, "quantity": quantity}], comment=comment)[0]

    def add_to_basket_batch(self, rows, comment=""):
        """Корзина поставщика -> заказ. Ответ — список той же длины, что rows.

        Корзина на сайте общая с менеджерами: basket/order отправляет её целиком.
        Поэтому оформляем только в пустую корзину, иначе чужие позиции ушли бы
        в наш заказ.
        """
        rows = list(rows or [])
        if not rows:
            return []
        results = [None] * len(rows)
        before = self._basket_content()
        if before is None:
            return [{"success": False, "error": self.last_message or f"{self.name}: корзина не прочитана"}
                    for _ in rows]
        if before:
            return [{"success": False, "error": (
                f"{self.name}: в корзине на сайте уже {len(before)} поз. — оформите или очистите их, "
                "иначе они уйдут в этот заказ")} for _ in rows]

        form = []
        sent = []
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            if not item.get("supplier_code"):
                results[index] = {"success": False, "error": f"{self.name}: нет supplierCode позиции"}
                continue
            n = len(sent)
            form += [
                (f"positions[{n}][number]", str(item.get("article") or "")),
                (f"positions[{n}][brand]", str(item.get("brand") or "")),
                (f"positions[{n}][supplierCode]", str(item.get("supplier_code") or "")),
                (f"positions[{n}][itemKey]", str(item.get("item_key") or "")),
                (f"positions[{n}][quantity]", str(quantity)),
                (f"positions[{n}][comment]", str(comment or "")[:250]),
            ]
            sent.append((index, item, quantity))
        if not sent:
            return results

        added = self._request("POST", "basket/add", data=form)
        if added is None:
            for index, _item, _qty in sent:
                results[index] = {"success": False, "error": self.last_message or f"{self.name}: не добавлено в корзину"}
            self._clear_basket()
            return results

        # Цена в корзине — та, по которой уйдёт заказ: рост сверх 2% не оформляем.
        content = self._basket_content() or []
        by_key = {}
        for row in content:
            by_key.setdefault(_position_key(row.get("brand"), row.get("number"), row.get("supplierCode")), row)
        to_order = []
        for index, item, quantity in sent:
            row = by_key.get(_position_key(item.get("brand"), item.get("article"), item.get("supplier_code")))
            if not row:
                results[index] = {"success": False, "error": f"{self.name}: позиция не попала в корзину"}
                continue
            old_price = _float(item.get("price"))
            new_price = _float(row.get("priceInSiteCurrency") or row.get("price"))
            if old_price > 0 and new_price > old_price * 1.02 + 1:
                results[index] = {"success": False, "error": (
                    f"{self.name}: цена выросла с {old_price:.2f} до {new_price:.2f}, обновите проценку")}
                continue
            to_order.append((index, row))
        if not to_order:
            self._clear_basket()
            return results
        if len(to_order) < len(content):
            # Есть что не оформляем (цена выросла) — убираем это из корзины.
            keep = {str(row.get("positionId")) for _i, row in to_order}
            for row in content:
                if str(row.get("positionId")) not in keep:
                    self._request("POST", "basket/add", data=[
                        ("positions[0][number]", str(row.get("number") or "")),
                        ("positions[0][brand]", str(row.get("brand") or "")),
                        ("positions[0][supplierCode]", str(row.get("supplierCode") or "")),
                        ("positions[0][itemKey]", str(row.get("itemKey") or "")),
                        ("positions[0][quantity]", "0"),
                    ])

        # Способы оплаты/доставки обязательны, только если сайт их вообще отдаёт
        # (у части сайтов списки пустые) — передаём то, что выбрано в настройках.
        order_form = [("shipmentAddress", self.shipment_address or "0")]
        if self.payment_method:
            order_form.append(("paymentMethod", self.payment_method))
        if self.shipment_method:
            order_form.append(("shipmentMethod", self.shipment_method))
        order_form += [
            ("comment", str(comment or "")[:250]),
            ("wholeOrderOnly", "0"),
        ]
        if self.shipment_office:
            order_form.append(("shipmentOffice", self.shipment_office))
        shipment_date = self._shipment_date(to_order)
        if shipment_date:
            order_form.append(("shipmentDate", shipment_date))
        try:
            data = self._request("POST", "basket/order", data=order_form,
                                 timeout=self.order_timeout, raise_transport=True)
        except OrderDeliveryUnknown as exc:
            for index, _row in to_order:
                results[index] = {"success": False, "uncertain": True, "error": (
                    f"{self.name}: ответ на заказ не получен ({exc}); проверьте заказы на сайте")}
            return results

        # По документации часть позиций может уйти и при ошибке: смотрим orders всегда.
        ordered = {}
        for order in (data or {}).get("orders") or []:
            for position in order.get("positions") or []:
                key = _position_key(position.get("brand"), position.get("number"), position.get("supplierCode"))
                ordered.setdefault(key, (order, position))
        error = self.last_message
        for index, row in to_order:
            found = ordered.get(_position_key(row.get("brand"), row.get("number"), row.get("supplierCode")))
            if found:
                order, position = found
                results[index] = {
                    "success": True,
                    "data": f"{self.name}: заказ {order.get('number')}",
                    "order_number": str(order.get("number") or ""),
                    "position_id": str(position.get("positionId") or position.get("id") or ""),
                }
            else:
                results[index] = {"success": False, "error": error or f"{self.name}: заказ не создан"}
        if any(not results[index].get("success") for index, _row in to_order):
            # Корзина была пустой до нас: неоформленное — наше, убираем, чтобы
            # оно не уехало со следующим заказом.
            self._clear_basket()
        return results

    def _shipment_date(self, to_order):
        """Дата отгрузки нужна только при включённых «Днях отгрузки»; берём ближайшую."""
        hours = [_int(row.get("deadline")) for _i, row in to_order] or [0]
        params = {"minDeadlineTime": min(hours), "maxDeadlineTime": max(hours)}
        if self.shipment_address and self.shipment_address != "0":
            params["shipmentAddress"] = self.shipment_address
        data = self._request("GET", "basket/shipmentDates", params)
        self.last_message = ""
        rows = data.values() if isinstance(data, dict) else (data or [])
        for row in rows:
            value = row.get("date") if isinstance(row, dict) else row
            if value:
                return str(value)
        return ""

    def _clear_basket(self):
        message = self.last_message
        self._request("POST", "basket/clear")
        self.last_message = message

    def get_orders(self, limit=100, skip=0):
        data = self._request("GET", "orders", {"format": "p", "limit": limit, "skip": skip})
        return (data or {}).get("items") or [] if isinstance(data, dict) else []


_CLASSES = {}


def abcp_supplier_class(name):
    """Подкласс под конкретного поставщика: имя класса уникально, DISPLAY_NAME — его имя."""
    name = str(name or "").strip() or "ABCP"
    cls = _CLASSES.get(name)
    if cls is None:
        slug = re.sub(r"\W+", "_", name, flags=re.UNICODE).strip("_") or "x"
        cls = type(f"AbcpSupplier_{slug}", (AbcpSupplierProvider,), {"DISPLAY_NAME": name})
        _CLASSES[name] = cls
    return cls


def abcp_supplier_names():
    return list(_CLASSES)


def _warehouse_name(row):
    """Название склада: distributorCode, иначе текст/подсказка из supplierDescription.

    supplierDescription на сайтах ABCP бывает HTML-значком (<div><i title="Надежный
    поставщик">), поэтому теги снимаем; к общему названию добавляем id поставщика,
    чтобы разные склады не слипались.
    """
    code = str(row.get("distributorCode") or "").strip()
    if code:
        return code
    description = str(row.get("supplierDescription") or "")
    titles = re.findall(r'title="([^"]+)"', description)
    text = re.sub(r"<[^>]+>", " ", description)
    text = re.sub(r"\s+", " ", text).strip()
    if text:
        return text
    label = titles[0].strip() if titles else "склад"
    ident = str(row.get("distributorId") or row.get("supplierCode") or "").strip()
    return f"{label} #{ident}" if ident else label


def _position_key(brand, number, supplier_code):
    return (_key(brand), _key(number), str(supplier_code or "").strip())


def _error_message(data):
    if not isinstance(data, dict):
        return ""
    code = data.get("errorCode")
    message = data.get("errorMessage")
    if code or message:
        return f"{code}: {message}" if code and message else str(message or code)
    if "status" in data and str(data.get("status")) == "0":
        return "операция не выполнена"
    return ""


def _availability(value):
    """ABCP: положительное — остаток; 0/отрицательное — наличие без точного числа."""
    number = _int(value)
    if number > 0:
        return str(number)
    if str(value or "").strip().startswith("-"):
        return ">1"
    return "0"


def _normalize_host(host):
    value = str(host or "").strip().rstrip("/")
    if value and "://" not in value:
        value = f"https://{value}"
    return value


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "да")


def _float(value):
    try:
        return float(str(value if value is not None else "0").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _int(value):
    try:
        return int(float(str(value if value is not None else "0").replace(" ", "").replace(",", ".")))
    except (TypeError, ValueError):
        return 0


def _key(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
