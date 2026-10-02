import re

import requests

from order_quantity import parse_available_quantity
from provider_adapter import TRANSPORT_EXCEPTIONS, OrderDeliveryUnknown
from response_text import decode_response_text


class TradesoftProvider:
    """Поставщик через сервис ТрейдСофт (service.tradesoft.ru/3).

    Сейчас так подключена Автоформула (провайдер AVTOFORMULA). Авторизация
    двойная: учётка ТрейдСофт (user/password) и учётка сайта поставщика
    (login/password в каждом контейнере запроса).

    Заказ идёт через услугу «Онлайн-заказ»: GetPriceList даёт itemHash,
    PreOrderSearch по нему — свежий itemId, MakeOrderOffline по itemId
    оформляет заказ и возвращает orderItemId для GetItemsStatus.
    """

    URL = "https://service.tradesoft.ru/3/"
    DISPLAY_NAME = "Автоформула"

    def __init__(
        self,
        user,
        password,
        provider_id="AVTOFORMULA",
        provider_login="",
        provider_password="",
        timeout=10,
        include_analogues=True,
        max_producers=5,
    ):
        self.user = str(user or "").strip()
        self.password = str(password or "")
        self.provider_id = str(provider_id or "AVTOFORMULA").strip()
        self.provider_login = str(provider_login or "").strip()
        self.provider_password = str(provider_password or "")
        self.timeout = int(timeout or 10)
        # Оформление без повторов: таймаут длиннее поискового, обрыв здесь —
        # неизвестный исход заказа, а не отказ.
        self.order_timeout = max(60, self.timeout * 3)
        self.include_analogues = bool(include_analogues)
        self.max_producers = max(1, int(max_producers or 5))
        self.last_message = ""
        self.session = requests.Session()
        self.session.trust_env = False

    # ---------- транспорт ----------

    def _container(self, **extra):
        row = {
            "provider": self.provider_id,
            "login": self.provider_login,
            "password": self.provider_password,
        }
        row.update(extra)
        return row

    def _request(self, action, timeout=None, raise_transport=False, **body):
        """POST JSON на единую точку. Возвращает dict ответа или None."""
        self.last_message = ""
        if not self.user or not self.password:
            self.last_message = "Укажите логин и пароль ТрейдСофт"
            return None
        if not self.provider_login or not self.provider_password:
            self.last_message = f"Укажите логин и пароль сайта {self.DISPLAY_NAME}"
            return None
        payload = {
            "service": "provider",
            "action": action,
            "user": self.user,
            "password": self.password,
        }
        payload.update(body)
        try:
            response = self.session.post(
                self.URL,
                json=payload,
                timeout=float(timeout or self.timeout),
                proxies={"http": None, "https": None},
            )
        except requests.exceptions.Timeout as exc:
            self.last_message = f"{self.DISPLAY_NAME}: ТрейдСофт не ответил (таймаут)"
            if raise_transport:
                raise OrderDeliveryUnknown(str(exc) or "таймаут") from exc
            return None
        except requests.exceptions.RequestException as exc:
            self.last_message = f"{self.DISPLAY_NAME}: ошибка соединения с ТрейдСофт: {str(exc)[:120]}"
            if raise_transport and isinstance(exc, TRANSPORT_EXCEPTIONS):
                raise OrderDeliveryUnknown(str(exc) or exc.__class__.__name__) from exc
            return None
        try:
            data = response.json()
        except ValueError:
            self.last_message = (
                f"{self.DISPLAY_NAME}: HTTP {response.status_code}: "
                + decode_response_text(response, 160).strip()
            )
            return None
        if not isinstance(data, dict):
            self.last_message = f"{self.DISPLAY_NAME}: неожиданный ответ ТрейдСофт"
            return None
        error = str(data.get("error") or "").strip()
        if response.status_code == 401:
            self.last_message = f"{self.DISPLAY_NAME}: неверный логин или пароль ТрейдСофт"
            return None
        if response.status_code != 200 or error:
            self.last_message = f"{self.DISPLAY_NAME}: {error or 'HTTP ' + str(response.status_code)}"[:200]
            return None
        return data

    def _containers(self, data):
        """Контейнеры ответа нашего провайдера; ошибка поставщика — в last_message."""
        result = []
        for container in (data or {}).get("container") or []:
            if not isinstance(container, dict):
                continue
            error = str(container.get("error") or "").strip()
            if error:
                self.last_message = f"{self.DISPLAY_NAME}: {error}"[:200]
                continue
            result.append(container)
        return result

    # ---------- поиск ----------

    def get_brands(self, article):
        article = str(article or "").strip()
        if not article:
            return []
        data = self._request("GetProducerList", container=[self._container(code=article)])
        brands = []
        for container in self._containers(data):
            for row in container.get("data") or []:
                producer = str((row or {}).get("producer") or "").strip()
                if producer and producer not in brands:
                    brands.append(producer)
        return brands

    def get_prices(self, article, brand=None):
        article = str(article or "").strip()
        if not article:
            return []
        brands = [str(brand).strip()] if brand else self.get_brands(article)[: self.max_producers]
        brands = [b for b in brands if b]
        if not brands:
            return []
        # В одном запросе можно несколько контейнеров: все бренды за один вызов.
        data = self._request(
            "GetPriceList",
            container=[self._container(code=article, producer=b) for b in brands],
        )
        return self._normalize_offers(data, article, brand or "")

    def _normalize_offers(self, data, requested_article, requested_brand):
        results = []
        seen = set()
        for container in self._containers(data):
            for row in container.get("data") or []:
                if not isinstance(row, dict):
                    continue
                price = _float(row.get("price"))
                if price <= 0:
                    continue
                currency = str(row.get("currencycode") or "RUB").upper()
                if currency not in ("RUB", "RUR", ""):
                    continue
                article = str(row.get("code") or requested_article)
                brand = str(row.get("producer") or requested_brand)
                item_type = str(row.get("itemtype") or "").lower()
                is_cross = item_type in ("analog", "replace")
                if _key(article) != _key(requested_article):
                    is_cross = True
                if requested_brand and _key(brand) != _key(requested_brand):
                    is_cross = True
                if is_cross and not self.include_analogues:
                    continue
                days_min = _int(row.get("deliverydays_min"))
                days_max = _int(row.get("deliverydays_max"))
                if not days_max:
                    days_min, days_max = _days_range(row.get("deliverydays"))
                direction = str(row.get("direction") or "").strip()
                item_hash = str(row.get("itemHash") or "").strip()
                # Остаток как есть: ">10" «Проценка» понимает как нижнюю границу.
                rest = str(row.get("rest") or "").strip()
                marker = (item_hash or (_key(brand), _key(article), direction), round(price, 2))
                if marker in seen:
                    continue
                seen.add(marker)
                results.append(
                    {
                        "provider": self.DISPLAY_NAME,
                        "brand": brand,
                        "article": article,
                        "name": str(row.get("caption") or ""),
                        "price": price,
                        "quantity": rest,
                        "days": days_max or days_min,
                        "days_min": days_min,
                        "multiplicity": max(1, _int(row.get("amount")) or 1),
                        "min_quantity": max(1, _int(row.get("minquantity")) or 1),
                        "warehouse": direction or "-",
                        "logo": direction or "-",
                        "item_hash": item_hash,
                        "offer_id": item_hash,
                        "direction": direction,
                        "delivery": str(row.get("delivery") or ""),
                        "return": str(row.get("return") or ""),
                        "not_returnable": str(row.get("return") or "").lower() == "impossible",
                        "return_policy": "возврат ограничен" if str(row.get("return") or "").lower() == "limited" else "",
                        "is_cross": is_cross,
                    }
                )
        return results

    def check_connection(self):
        data = self._request("GetOptionsList", container=[self._container()])
        if data is None:
            return False, self.last_message or "ТрейдСофт не ответил"
        containers = self._containers(data)
        if not containers:
            return False, self.last_message or f"{self.DISPLAY_NAME}: провайдер не ответил"
        make_order = str(containers[0].get("makeOrder") or "").strip()
        if make_order:
            return True, f"Подключено, заказ: {make_order}"
        return True, "Подключено"

    # ---------- заказ ----------

    def _fresh_item(self, item, quantity):
        """PreOrderSearch по itemHash: свежий itemId и проверка цены/остатка.

        Возвращает (itemId, None) или (None, текст ошибки).
        """
        code = str(item.get("article") or "").strip()
        producer = str(item.get("brand") or "").strip()
        item_hash = str(item.get("item_hash") or item.get("offer_id") or "").strip()
        if not code or not producer or not item_hash:
            return None, f"{self.DISPLAY_NAME}: нет артикула, бренда или itemHash позиции"
        data = self._request(
            "PreOrderSearch",
            container=[self._container(code=code, producer=producer, itemHash=item_hash)],
        )
        if data is None:
            return None, self.last_message or f"{self.DISPLAY_NAME}: предзаказ не выполнен"
        candidates = []
        for container in self._containers(data):
            for row in container.get("items") or container.get("data") or []:
                if isinstance(row, dict) and row.get("itemId"):
                    candidates.append(row)
        if not candidates:
            return None, self.last_message or f"{self.DISPLAY_NAME}: предложение больше недоступно"
        same = [r for r in candidates if str(r.get("itemHash") or "") == item_hash]
        row = (same or candidates)[0]
        old_price = _float(item.get("price"))
        new_price = _float(row.get("price"))
        # Небольшой допуск на округление; рост цены решает оператор.
        if old_price > 0 and new_price > old_price * 1.02 + 1:
            return None, (
                f"{self.DISPLAY_NAME}: цена выросла с {old_price:.2f} до {new_price:.2f}, "
                "обновите проценку"
            )
        rest, lower_bound, _ = parse_available_quantity(row.get("rest"))
        if rest is not None and not lower_bound and rest < int(quantity or 1):
            return None, f"{self.DISPLAY_NAME}: остаток {rest} меньше заказа {quantity}"
        return str(row.get("itemId")), None

    def add_to_basket(self, item, quantity=1, comment=""):
        return self.add_to_basket_batch([{"item": item, "quantity": quantity}], comment=comment)[0]

    def add_to_basket_batch(self, rows, comment=""):
        """Один MakeOrderOffline на все позиции; ответ раскладывается построчно по itemId.

        rows — список {"item": предложение, "quantity": количество};
        ответ — список той же длины и в том же порядке.
        """
        rows = list(rows or [])
        if not rows:
            return []
        results = [None] * len(rows)
        order_items = []
        index_by_item = {}
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            item_id, error = self._fresh_item(item, quantity)
            if error:
                results[index] = {"success": False, "error": error}
                continue
            if item_id in index_by_item:
                # Одно предложение в двух строках: одна позиция заказа на сумму.
                index_by_item[item_id].append(index)
                for entry in order_items:
                    if entry["itemId"] == item_id:
                        entry["quantity"] += quantity
                continue
            index_by_item[item_id] = [index]
            order_items.append(
                {
                    "itemId": item_id,
                    "quantity": quantity,
                    "reference": str(item.get("internal_offer_id") or "")[:64],
                    "comment": str(comment or "")[:255],
                }
            )
        if not order_items:
            return results

        sent_indexes = [i for indexes in index_by_item.values() for i in indexes]
        param = self._container(comment=str(comment or "")[:255], items=order_items)
        try:
            data = self._request(
                "MakeOrderOffline",
                timeout=self.order_timeout,
                raise_transport=True,
                param=[param],
            )
        except OrderDeliveryUnknown as exc:
            for index in sent_indexes:
                results[index] = {
                    "success": False,
                    "uncertain": True,
                    "error": (
                        f"{self.DISPLAY_NAME}: ответ на заказ не получен ({exc}); "
                        "проверьте заказы в ТрейдСофт"
                    ),
                }
            return results

        if data is None:
            for index in sent_indexes:
                results[index] = {
                    "success": False,
                    "error": self.last_message or f"{self.DISPLAY_NAME}: заказ не создан",
                }
            return results

        result = data.get("result") or {}
        if isinstance(result, list):
            result = result[0] if result else {}
        order_status = str(result.get("orderStatus") or "")
        by_item = {}
        for row in result.get("items") or []:
            if isinstance(row, dict) and row.get("itemId"):
                by_item[str(row.get("itemId"))] = row
        for item_id, indexes in index_by_item.items():
            row = by_item.get(item_id) or {}
            error = str(row.get("error") or "").strip()
            order_item_id = str(row.get("orderItemId") or "").strip()
            if order_status == "MakeOrderError" or error or not order_item_id:
                outcome = {
                    "success": False,
                    "error": f"{self.DISPLAY_NAME}: {error or 'заказ не создан'}",
                }
            else:
                # Ответ приходит сразу, а оформление на сайте поставщика идёт
                # дальше: итог видно по GetItemsStatus с этим orderItemId.
                outcome = {
                    "success": True,
                    "data": f"{self.DISPLAY_NAME}: отправлено в заказ, позиция {order_item_id}",
                    "order_item_id": order_item_id,
                }
            for index in indexes:
                results[index] = dict(outcome)
        return results

    def get_items_status(self, order_item_ids):
        """Статусы заказанных позиций: {orderItemId: {stateId, stateName, ...}}."""
        ids = [str(i).strip() for i in order_item_ids or [] if str(i or "").strip()]
        if not ids:
            return {}
        data = self._request("GetItemsStatus", container=[self._container(items=ids)])
        statuses = {}
        for container in self._containers(data):
            items = container.get("items") or {}
            values = items.values() if isinstance(items, dict) else items
            for row in values:
                if isinstance(row, dict) and row.get("providerItemId"):
                    statuses[str(row.get("providerItemId"))] = row
        return statuses


def _float(value):
    try:
        return float(str(value if value is not None else "0").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _int(value):
    try:
        return int(float(str(value if value is not None else "0").replace(",", ".")))
    except (TypeError, ValueError):
        return 0


def _days_range(value):
    numbers = [int(n) for n in re.findall(r"\d+", str(value or ""))]
    if not numbers:
        return 0, 0
    return numbers[0], numbers[-1]


def _key(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
