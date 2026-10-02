import json
import re
import threading
import time
import zlib

import requests

from provider_adapter import TRANSPORT_EXCEPTIONS, OrderDeliveryUnknown
from response_text import decode_response_text


class AvtotoProvider:
    # Шаги, которые пишутся всегда, даже вне оформления заказа. Поиск делает
    # десятки вызовов на строку, поэтому наружу выпускаем только сбои — по ним
    # видно, почему поставщик молчит или отдаёт позиции без part_id.
    ERROR_DEBUG_STEPS = (
        "_call:timeout",
        "_call:exception",
        "_call:http_error",
        "_call:error_payload",
        "get_prices:search_start_auth_error",
        "get_prices:no_search_id",
        "get_prices:empty_result",
        "normalize:no_part_id",
    )

    def __init__(self, client_id, login, password, include_crosses=True, timeout=10):
        self.client_id = str(client_id or "")
        self.login = str(login or "")
        self.password = str(password or "")
        self.include_crosses = include_crosses
        self.timeout = int(timeout)
        self.base_url = "https://www.avtoto.ru/?soap_server=json_mode"
        self.last_message = ""
        self.brand_resolver_timeout = min(max(3, self.timeout), 5)
        # Таймаут поиска для оформления заказа мал: пакет из 70 позиций
        # обрабатывается заметно дольше одиночной строки, а обрыв на
        # AddToOrdersFromBasket оставляет заказ в неизвестном состоянии.
        self.order_timeout = max(60, self.timeout * 3)
        # Куда писать пошаговую диагностику и в каком контексте. Пока контекст
        # пуст, _debug молчит: поиск делает десятки вызовов на строку и залил бы
        # лог. Контекст выставляет вызывающий код вокруг оформления заказа.
        self.debug_sink = None
        self._debug_local = threading.local()

    def check_connection(self):
        if not self.client_id or not self.login or not self.password:
            return False, "Номер клиента/логин/пароль пусты"
        brands = self.get_brand_candidates("OC47")
        if brands:
            return True, "OK"
        return False, self.last_message or "Avtoto не ответил"

    def get_brand_candidates(self, article):
        self.last_message = ""
        article = str(article or "").strip()
        if not article:
            return []
        if not self.client_id or not self.login or not self.password:
            self.last_message = "Номер клиента/логин/пароль пусты"
            return []

        data = None
        last_error = ""
        used_method = ""
        for method, payload in self._brand_lookup_variants(article):
            data = self._call(method, payload)
            if data is None:
                last_error = self.last_message
                continue
            error = self._extract_error_message(data) or self._extract_info_errors(data)
            if error:
                last_error = error
                if self._is_input_format_error(error):
                    continue
                self.last_message = error
                return []
            rows = self._extract_parts(data) or self._extract_brand_rows(data)
            if rows:
                used_method = method
                break
            last_error = f"Avtoto: {method} вернул 0 брендов"
        else:
            self.last_message = last_error or "Avtoto: бренды не найдены"
            return []

        rows = self._extract_parts(data) or self._extract_brand_rows(data)
        result = []
        seen = set()
        for row in rows:
            if isinstance(row, str):
                brand = row.strip()
                item_article = article
                name = ""
            elif isinstance(row, dict):
                brand = self._first_value(
                    row,
                    "Manuf", "Manufacturer", "Brand", "brand", "MakeName", "manufacturer", "name",
                )
                item_article = self._first_value(
                    row,
                    "Code", "code", "Article", "article", "SearchCode", "search_code", "PartNumber", "partnumber",
                ) or article
                name = self._first_value(row, "Name", "Description", "description", "ProductName", "productName") or ""
            else:
                continue
            brand = str(brand or "").strip()
            if not brand:
                continue
            item_article = str(item_article or article or "").strip()
            marker = (brand.upper(), "".join(ch for ch in item_article.upper() if ch.isalnum()), str(name).upper())
            if marker in seen:
                continue
            seen.add(marker)
            result.append({
                "brand": brand,
                "article": item_article,
                "name": str(name or "").strip(),
                "source": used_method or "get_brands_by_code",
            })
        return result

    def _brand_lookup_variants(self, article):
        payload = {
            "user_id": self.client_id,
            "user_login": self.login,
            "user_password": self.password,
            "search_code": article,
        }
        alt_payload = {
            "UserID": self.client_id,
            "UserLogin": self.login,
            "UserPassword": self.password,
            "SearchCode": article,
        }
        return [
            ("GetBrandsByCode", payload),
            ("GetBrandsByCode", alt_payload),
            ("get_brands_by_code", payload),
            ("avtoto_parts::get_brands_by_code", article),
            ("GetBrandsByCode", {"SearchCode": article, "Code": article}),
        ]

    def _is_input_format_error(self, message):
        text = str(message or "").lower()
        return (
            "формат вход" in text
            or "неверный формат" in text
            or "не верный формат" in text
            or "invalid input" in text
            or "input format" in text
        )

    def get_brands(self, article):
        brands = []
        for item in self.get_brand_candidates(article):
            brand = item.get("brand")
            if brand and brand not in brands:
                brands.append(brand)
        return brands

    def get_prices(self, article, brand="", include_crosses=None):
        self.last_message = ""
        if not self.client_id or not self.login or not self.password:
            self.last_message = "Номер клиента/логин/пароль пусты"
            return []

        brand = str(brand or "").strip()
        use_crosses = self.include_crosses if include_crosses is None else bool(include_crosses)
        self._debug("get_prices:start", {"article": article, "brand": brand, "include_crosses": use_crosses})
        search_id = None
        response_search_id = None
        started = None
        parts_data = None

        for payload in self._build_search_payload_variants(article, brand=brand, include_crosses=use_crosses):
            self._debug("get_prices:search_start_attempt", {"payload": self._sanitize_payload(payload)})
            started = self._call("SearchStart", payload)
            if started is None:
                continue
            if self._is_auth_error(started):
                self._debug("get_prices:search_start_auth_error", {"payload": self._sanitize_payload(started)})
                continue

            search_id = self._first_value(
                started,
                "ProcessSearchId",
                "process_id",
                "ProcessSearchID",
                "SearchID",
                "SearchId",
                "search_id",
                "id",
                "ID",
                "SearchGuid",
                "guid",
            )
            if not search_id:
                nested_result = self._first_value(started, "result", "Result")
                if isinstance(nested_result, dict):
                    search_id = self._first_value(
                        nested_result,
                        "ProcessSearchId",
                        "process_id",
                        "ProcessSearchID",
                        "SearchID",
                        "SearchId",
                        "search_id",
                        "id",
                        "ID",
                        "SearchGuid",
                        "guid",
                    )
            if not search_id and isinstance(started, (str, int)):
                search_id = started
            self._debug("get_prices:search_start_response", {"search_id": search_id, "payload": started})

            if search_id:
                break

            parts = self._extract_parts(started)
            if parts:
                self._debug("get_prices:search_start_direct_parts", {"parts_count": len(parts)})
                return self._normalize_parts(parts, article)
            break

        if not search_id:
            parts = self._extract_parts(started)
            self._debug("get_prices:no_search_id", {"parts": parts})
            return self._normalize_parts(parts, article)

        for attempt in range(12):
            self._debug("get_prices:poll_attempt", {"attempt": attempt + 1, "search_id": search_id})
            parts_data = self._call("SearchGetParts2", {
                "ProcessSearchId": search_id,
                "process_search_id": search_id,
                "SearchID": search_id,
                "SearchId": search_id,
                "search_id": search_id,
                "Limit": 2000,
            })
            if parts_data is not None:
                nested_result = self._first_value(parts_data, "result", "Result") if isinstance(parts_data, dict) else None
                payload = parts_data if not isinstance(nested_result, dict) else nested_result
                info = self._first_value(payload, "Info", "info")
                status = self._first_value(info, "SearchStatus", "search_status") if isinstance(info, dict) else None
                response_search_id = self._first_value(info, "SearchID", "SearchId", "search_id") if isinstance(info, dict) else None
                self._debug("get_prices:poll_response", {"attempt": attempt + 1, "status": status, "response_search_id": response_search_id, "payload": payload})
                if status in (4, "4"):
                    parts = self._extract_parts(payload)
                    self._debug("get_prices:ready", {"parts_count": len(parts)})
                    if parts:
                        return self._normalize_parts(parts, article, response_search_id or search_id)
                if status in (2, "2"):
                    self.last_message = f"Avtoto: поиск в обработке, попытка {attempt + 1}/12"
                    self._debug("get_prices:still_processing", {"attempt": attempt + 1})
                    time.sleep(1.0)
                    continue
                if status in (0, 1, 3, "0", "1", "3"):
                    break
            time.sleep(0.5)

        if parts_data is not None:
            nested_result = self._first_value(parts_data, "result", "Result") if isinstance(parts_data, dict) else None
            payload = parts_data if not isinstance(nested_result, dict) else nested_result
            parts = self._extract_parts(payload)
            self._debug("get_prices:final_parts", {"parts_count": len(parts), "payload": payload})
            if parts:
                return self._normalize_parts(parts, article, response_search_id or search_id)

        self.last_message = self.last_message or "Avtoto: не получил результаты поиска"
        self._debug("get_prices:empty_result", {"last_message": self.last_message})
        return []

    def add_to_basket(self, item, quantity=1, comment=""):
        return self.create_order(item, quantity, comment)

    def create_order(self, item, quantity=1, comment=""):
        part = self._build_order_part(item, quantity, comment)
        missing = [name for name in ("PartId", "SearchID", "RemoteID", "Count") if not part.get(name)]
        if missing:
            return {"success": False, "error": "Avtoto: не хватает данных для заказа: " + ", ".join(missing)}

        # Проверки наличия между добавлением и оформлением нет намеренно,
        # см. комментарий в add_to_basket_batch: наш же резерв обнулял её.
        self._debug("create_order:start", {"part": self._sanitize_payload(part)})
        added = self._call("AddToBasket", {"user": self._user_payload(), "parts": [part]}, timeout=self.order_timeout)
        if added is None:
            return {"success": False, "error": self.last_message or "Avtoto: ошибка добавления в корзину"}

        basket_parts = self._done_inner_parts(added, default_count=part["Count"])
        if not basket_parts:
            return {"success": False, "error": self._format_action_error(added, "Avtoto: товар не добавлен в корзину")}

        try:
            ordered = self._call(
                "AddToOrdersFromBasket",
                {"user": self._user_payload(), "parts": basket_parts},
                timeout=self.order_timeout,
                raise_transport=True,
            )
        except OrderDeliveryUnknown as exc:
            return {
                "success": False,
                "uncertain": True,
                "error": f"Avtoto: ответ на оформление не получен ({exc}); проверьте заказы у поставщика",
            }
        if ordered is None:
            return {"success": False, "error": self.last_message or "Avtoto: ошибка оформления заказа"}
        order_parts = self._done_inner_parts(ordered)
        self._debug("create_order:ordered", {"order_parts": order_parts, "response": self._sanitize_payload(ordered)})
        if order_parts:
            return {"success": True, "data": ordered}
        return {"success": False, "error": self._format_action_error(ordered, "Avtoto: заказ не создан")}

    def add_to_basket_batch(self, rows, comment=""):
        """Оформляет все позиции одним пакетом.

        API Avtoto изначально принимает массив parts во всех трёх шагах, мы
        просто клали в него по одному элементу: 74 позиции стоили 222 вызова.
        rows — список {"item": предложение, "quantity": количество}.
        Ответ — список той же длины и в том же порядке, по одному на позицию.
        """
        rows = list(rows or [])
        if not rows:
            return []
        results = [None] * len(rows)
        parts = []
        index_by_remote = {}
        count_by_remote = {}
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            part = self._build_order_part(item, quantity, comment)
            part["RemoteID"] = self._local_remote_id(item, salt=str(index))
            missing = [name for name in ("PartId", "SearchID", "RemoteID", "Count") if not part.get(name)]
            if missing:
                results[index] = {
                    "success": False,
                    "error": "Avtoto: не хватает данных для заказа: " + ", ".join(missing),
                }
                continue
            index_by_remote[str(part["RemoteID"])] = index
            count_by_remote[str(part["RemoteID"])] = part["Count"]
            parts.append(part)

        if not parts:
            return [result or {"success": False, "error": "Avtoto: нет позиций для заказа"} for result in results]

        # Шага CheckAvailabilityInBasket здесь намеренно нет. AddToBasket на
        # собственных складах Avtoto ставит резерв на 10 минут, а проверка
        # считает свободный остаток уже без него и через секунду после
        # успешного добавления отвечает «указанного количества нет в наличии»
        # (в логах так во всех 12 разобранных случаях, 13 отказов из 16 —
        # склад Ростов). Отказ по существу ловим на оформлении: там причина
        # настоящая, а не наш же резерв.
        self._debug("batch:start", {"parts_count": len(parts), "parts": self._sanitize_payload(parts)})
        added = self._call("AddToBasket", {"user": self._user_payload(), "parts": parts}, timeout=self.order_timeout)
        if added is None:
            error = self.last_message or "Avtoto: ошибка добавления в корзину"
            return [result or {"success": False, "error": error} for result in results]

        basket_parts = []
        for part in self._done_inner_parts(added):
            remote_id = str(part.get("RemoteID"))
            if remote_id not in index_by_remote:
                continue
            if not part.get("Count"):
                part["Count"] = count_by_remote.get(remote_id)
            basket_parts.append(part)
        not_added = self._format_action_error(added, "Avtoto: товар не добавлен в корзину")
        in_basket = {str(part.get("RemoteID")) for part in basket_parts}
        for remote_id, index in index_by_remote.items():
            if remote_id not in in_basket:
                results[index] = {"success": False, "error": not_added}
        if not basket_parts:
            return [result or {"success": False, "error": not_added} for result in results]

        try:
            ordered = self._call(
                "AddToOrdersFromBasket",
                {"user": self._user_payload(), "parts": basket_parts},
                timeout=self.order_timeout,
                raise_transport=True,
            )
        except OrderDeliveryUnknown as exc:
            # Пакет мог уйти поставщику целиком: помечаем неизвестным исходом
            # только те позиции, которые в него попали.
            unknown = {
                "success": False,
                "uncertain": True,
                "error": f"Avtoto: ответ на оформление не получен ({exc}); проверьте заказы у поставщика",
            }
            for part in basket_parts:
                results[index_by_remote[str(part.get("RemoteID"))]] = dict(unknown)
            return [result or dict(unknown) for result in results]
        if ordered is None:
            error = self.last_message or "Avtoto: ошибка оформления заказа"
            return [result or {"success": False, "error": error} for result in results]
        # Раскладываем ТОЛЬКО по RemoteID. InnerID на шаге оформления другой:
        # в корзине это номер строки корзины (144523906), а в ответе на заказ —
        # уже номер строки заказа (124668038). Сопоставление по InnerID давало
        # ноль совпадений, и 73 реально заказанные позиции были помечены
        # ошибкой — с риском заказать их повторно.
        done_remote_ids = self._done_remote_ids(ordered)
        not_ordered = self._format_action_error(ordered, "Avtoto: заказ не создан")
        self._debug(
            "batch:ordered",
            {
                "done_count": len(done_remote_ids),
                "sent_count": len(basket_parts),
                "response": self._sanitize_payload(ordered),
            },
        )
        for part in basket_parts:
            remote_id = str(part.get("RemoteID"))
            index = index_by_remote[remote_id]
            if remote_id in done_remote_ids:
                results[index] = {"success": True, "data": ordered}
            else:
                results[index] = {"success": False, "error": not_ordered}
        return [result or {"success": False, "error": not_ordered} for result in results]

    def _build_order_part(self, item, quantity, comment):
        remote_id = self._local_remote_id(item)
        part_id = (
            item.get("part_id")
            or item.get("PartId")
            or item.get("PartID")
            or self._deep_first_value(item, "PartId", "PartID")
        )
        return {
            "Code": item.get("article", ""),
            "Manuf": item.get("brand", ""),
            "Name": item.get("name", ""),
            "Price": item.get("price", 0),
            "Storage": item.get("warehouse", ""),
            "Delivery": item.get("days", 0),
            "BaseCount": item.get("multiplicity", 1),
            "SearchID": item.get("search_id", ""),
            "PartId": part_id,
            "RemoteID": remote_id,
            "Count": int(quantity),
            "Comment": comment,
        }

    def _user_payload(self):
        return {
            "user_id": self.client_id,
            "user_login": self.login,
            "user_password": self.password,
        }

    def _local_remote_id(self, item, salt=""):
        marker = "|".join(str(item.get(key, "")) for key in ("search_id", "part_id", "article", "brand", "warehouse"))
        if salt:
            # В пакете две строки заказа могут указывать на одно и то же
            # предложение. RemoteID — наш локальный ключ, поставщик только
            # возвращает его обратно, поэтому подмешиваем номер строки:
            # без этого ответы по дублям не разложить обратно по позициям.
            marker = f"{marker}|{salt}"
        return str(zlib.crc32(marker.encode("utf-8")) & 0x7fffffff)

    def _done_inner_parts(self, data, default_count=None):
        rows = self._first_value(data, "DoneInnerId", "DoneInnerID", "doneInnerId", "Done")
        if isinstance(rows, dict):
            rows = [rows]
        result = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            inner_id = row.get("InnerID") or row.get("InnerId") or row.get("inner_id")
            remote_id = row.get("RemoteID") or row.get("RemoteId") or row.get("remote_id")
            if not inner_id or not remote_id:
                continue
            part = {"InnerID": inner_id, "RemoteID": remote_id}
            count = row.get("Count") or default_count
            if count:
                part["Count"] = int(count)
            result.append(part)
        return result

    def _done_remote_ids(self, data):
        """RemoteID позиций, которые поставщик подтвердил.

        RemoteID генерируем мы сами, и он один и тот же во всех ответах —
        в отличие от InnerID, который Avtoto перевыдаёт при оформлении.
        Берём и плоский список Done, и RemoteID внутри DoneInnerId.
        """
        done = set()
        rows = self._first_value(data, "Done", "done") or []
        if isinstance(rows, (str, int)):
            rows = [rows]
        for row in rows:
            if isinstance(row, dict):
                value = row.get("RemoteID") or row.get("RemoteId") or row.get("remote_id")
            else:
                value = row
            if value not in (None, ""):
                done.add(str(value))
        for part in self._done_inner_parts(data):
            remote_id = part.get("RemoteID")
            if remote_id not in (None, ""):
                done.add(str(remote_id))
        return done

    def _format_action_error(self, data, fallback):
        error = self._clean_messages(self._extract_error_message(data))
        if error:
            return error
        errors = self._first_value(data, "Errors", "errors")
        text = self._clean_messages(errors)
        if text:
            return text
        if errors:
            return str(errors)
        return fallback

    def _clean_messages(self, values):
        """Человекочитаемый текст из Errors: Avtoto шлёт их с HTML внутри."""
        if values in (None, "", [], {}):
            return ""
        if isinstance(values, (str, dict)):
            values = [values]
        messages = []
        for value in values:
            text = re.sub(r"<[^>]+>", " ", str(value))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                messages.append(text)
        return "; ".join(messages)

    def add_to_remote_basket_only(self, item, quantity=1, comment=""):
        payload = {
            "user": self._user_payload(),
            "parts": [self._build_order_part(item, quantity, comment)],
        }
        data = self._call("AddToBasket", payload)
        if data is None:
            return {"success": False, "error": self.last_message or "Avtoto: ошибка корзины"}
        if isinstance(data, dict) and (
            data.get("DoneInnerId") or data.get("Done") or data.get("DoneInnerID")
        ):
            return {"success": True, "data": data}
        errors = self._first_value(data, "Errors", "errors")
        return {"success": False, "error": str(errors or data)}

    def _call(self, method, params, timeout=None, raise_transport=False):
        """raise_transport=True — для шага, после которого заказ уже мог уйти:
        обрыв связи там нельзя молча считать неудачей."""
        payload = {"action": method, "data": json.dumps(params)}
        timeout = float(timeout or self.timeout)
        try:
            resp = requests.post(
                self.base_url,
                data=payload,
                timeout=timeout,
                proxies={"http": None, "https": None},
                headers={"Accept": "application/json", "User-Agent": "price_parcer/1.0"},
            )
            self._debug("_call:request", {"method": method, "payload": self._sanitize_payload(payload)})
            if resp.status_code != 200:
                text = decode_response_text(resp)
                self.last_message = f"HTTP {resp.status_code}: {text[:120]}"
                self._debug("_call:http_error", {"method": method, "status_code": resp.status_code, "text": text[:200]})
                return None
            try:
                data = resp.json()
            except Exception:
                text = decode_response_text(resp).strip()
                self.last_message = f"Avtoto вернул не JSON: {text[:120]}"
                return None
            if isinstance(data, dict) and data.get("error"):
                self.last_message = str(data.get("error"))
                self._debug("_call:error_payload", {"method": method, "data": self._sanitize_payload(data)})
                return None
            self._debug("_call:response", {"method": method, "data": self._sanitize_payload(data)})
            return data
        except requests.exceptions.Timeout as exc:
            self.last_message = "Avtoto не ответил: таймаут"
            # Метод обязателен: по таймауту на AddToOrdersFromBasket заказ мог
            # уже уйти поставщику, а по таймауту на AddToBasket — точно нет.
            self._debug("_call:timeout", {"method": method, "timeout": timeout})
            if raise_transport:
                raise OrderDeliveryUnknown(str(exc) or "таймаут") from exc
            return None
        except Exception as e:
            self.last_message = f"Ошибка Avtoto: {str(e)[:120]}"
            self._debug("_call:exception", {"method": method, "error": str(e)})
            if raise_transport and isinstance(e, TRANSPORT_EXCEPTIONS):
                raise OrderDeliveryUnknown(str(e) or e.__class__.__name__) from e
            return None

    def _extract_error_message(self, data):
        if not isinstance(data, dict):
            return ""

        for key in ("error", "Error", "Errors", "errors", "Message", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
                    if isinstance(item, dict):
                        nested = self._extract_error_message(item)
                        if nested:
                            return nested
            if isinstance(value, dict):
                nested = self._extract_error_message(value)
                if nested:
                    return nested

        nested_result = self._first_value(data, "result", "Result")
        if isinstance(nested_result, dict):
            return self._extract_error_message(nested_result)
        return ""

    def _extract_info_errors(self, data):
        if not isinstance(data, dict):
            return ""
        info = self._first_value(data, "Info", "info")
        if isinstance(info, dict):
            errors = self._first_value(info, "Errors", "errors")
            if isinstance(errors, list):
                return "; ".join(str(item) for item in errors if str(item).strip())
            if errors:
                return str(errors)
        nested_result = self._first_value(data, "result", "Result")
        if isinstance(nested_result, dict):
            return self._extract_info_errors(nested_result)
        return ""

    def _extract_parts(self, data):
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in ("Parts", "parts", "Brands", "brands", "SearchGetParts2Result", "data", "Data", "items", "Items", "offers", "Offers", "rows", "Rows", "list", "List", "result", "Result"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = self._extract_parts(value)
                if nested:
                    return nested
        return []

    def _extract_brand_rows(self, data):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        if any(key in data and data[key] not in (None, "") for key in ("Brand", "brand", "Manuf", "MakeName")):
            return [data]
        for value in data.values():
            nested = self._extract_brand_rows(value)
            if nested:
                return nested
        return []

    def _normalize_parts(self, parts, requested_article, search_id=None):
        results = []
        for item in parts:
            if not isinstance(item, dict):
                continue
            price = self._to_float(self._first_value(item, "Price", "price", "Cost", "cost", "SalePrice"))
            if price <= 0:
                continue
            qty_value = self._quantity_value(item)
            qty = self._quantity_int(qty_value, 1)
            if qty <= 0:
                continue
            quantity_text = str(qty_value if self._quantity_is_lower_bound(qty_value) else (qty or "0"))

            brand = self._first_value(item, "Manuf", "Manufacturer", "Brand", "brand", "MakeName", "manufacturer")
            article = self._first_value(item, "Code", "Article", "article", "code", "PartNumber", "partnumber")
            warehouse = self._first_value(item, "Storage", "Warehouse", "warehouse", "Stock", "stock", "Supplier", "supplier")
            days = self._to_int(self._first_value(item, "Delivery", "DeliveryDays", "delivery_days", "Days", "days"), 0)
            is_cross = bool(self._to_int(self._first_value(item, "IsCross", "is_cross", "Cross", "cross"), 0))
            part_id = self._deep_first_value(item, "PartId", "PartID", "part_id", "partid", "id", "ID")
            remote_id = self._deep_first_value(item, "RemoteID", "RemoteId", "remote_id", "remoteid")
            offer_id = self._deep_first_value(item, "OfferID", "OfferId", "offer_id")
            supplier_offer_id = part_id or offer_id
            search_value = self._first_value(item, "SearchID", "SearchId", "search_id") or search_id
            multiplicity = self._to_int(self._first_value(item, "BaseCount", "Multiplicity", "MinCount", "min_count"), 1)

            results.append({
                "provider": "Avtoto",
                "brand": str(brand or ""),
                "article": str(article or requested_article),
                "price": price,
                "days": days,
                "quantity": quantity_text,
                "max_count": self._first_value(item, "MaxCount", "max_count", "MaxCnt", "maxCnt"),
                "availability_is_lower_bound": self._quantity_is_lower_bound(qty_value),
                "logo": str(warehouse or "-"),
                "warehouse": str(warehouse or ""),
                "name": str(self._first_value(item, "Name", "name", "Description", "description") or ""),
                "multiplicity": multiplicity,
                "is_cross": is_cross,
                "search_id": str(search_value or ""),
                "part_id": str(part_id or ""),
                "remote_id": str(remote_id or ""),
                "offer_id": str(offer_id or ""),
                "supplier_offer_id": str(supplier_offer_id or ""),
                "return_percent": self._first_value(item, "BackPercent", "back_percent", "ReturnPercent", "return_percent"),
                # BackPercent=-1 у Авто-то — возврат невозможен.
                "not_returnable": str(self._first_value(item, "BackPercent", "back_percent", "ReturnPercent", "return_percent")).strip() == "-1",
                "availability": self._first_value(item, "Availability", "availability"),
                "being_used": self._first_value(item, "BeingUsed", "being_used"),
                "delivery_percent": self._first_value(item, "DeliveryPercent", "delivery_percent"),
            })
        without_part_id = [row for row in results if not (row.get("part_id") or row.get("offer_id"))]
        if without_part_id:
            self._debug(
                "normalize:no_part_id",
                {
                    "article": requested_article,
                    "count": len(without_part_id),
                    "of_total": len(results),
                    "sample": [
                        {
                            "brand": row.get("brand"),
                            "article": row.get("article"),
                            "price": row.get("price"),
                            "quantity": row.get("quantity"),
                            "warehouse": row.get("warehouse"),
                            "is_cross": row.get("is_cross"),
                        }
                        for row in without_part_id[:3]
                    ],
                    "raw_keys": sorted({key for part in parts if isinstance(part, dict) for key in part}),
                },
            )
        return results

    def _quantity_value(self, item):
        # Avtoto documents MaxCount as the stock/order limit; Availability is only a stock flag.
        max_count = self._first_value(item, "MaxCount", "max_count", "MaxCnt", "maxCnt")
        if self._quantity_int(max_count, 0) > 0:
            return max_count
        for key in (
            "Quantity",
            "quantity",
            "Qty",
            "qty",
            "Count",
            "count",
            "AvailableQuantity",
            "available_quantity",
            "Available",
            "available",
            "Rest",
            "rest",
            "Rests",
            "rests",
        ):
            value = self._first_value(item, key)
            if self._quantity_int(value, 0) > 0:
                return value
        if self._quantity_int(max_count, 0) == -1:
            return ">999"
        return 1

    def _quantity_int(self, value, default=0):
        text = str(value or "").strip()
        if text.startswith(">"):
            text = text[1:].strip()
        match = re.search(r"-?\d+(?:[.,]\d+)?", text)
        if not match:
            return default
        return self._to_int(match.group(0), default)

    def _quantity_is_lower_bound(self, value):
        return str(value or "").strip().startswith(">")

    def _build_search_payload_variants(self, article, brand="", include_crosses=None):
        use_crosses = self.include_crosses if include_crosses is None else bool(include_crosses)
        search_cross = "on" if use_crosses else "off"
        brand = str(brand or "").strip()

        def with_brand(payload):
            if brand:
                payload["brand"] = brand
            return payload

        variants = [
            with_brand({
                "search_code": article,
                "search_cross": search_cross,
                "user_id": self.client_id,
                "user_login": self.login,
                "user_password": self.password,
            }),
            with_brand({
                "SearchCode": article,
                "SearchCross": search_cross,
                "UserID": self.client_id,
                "UserLogin": self.login,
                "UserPassword": self.password,
            }),
            with_brand({
                "search_code": article,
                "search_cross": search_cross,
                "client_id": self.client_id,
                "login": self.login,
                "password": self.password,
            }),
            with_brand({
                "SearchCode": article,
                "SearchCross": search_cross,
                "ClientID": self.client_id,
                "Login": self.login,
                "Password": self.password,
            }),
            with_brand({
                "search_code": article,
                "search_cross": search_cross,
                "user": {"id": self.client_id, "login": self.login, "password": self.password},
            }),
        ]
        seen = set()
        result = []
        for payload in variants:
            marker = json.dumps(payload, sort_keys=True)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(payload)
        return result

    def _is_auth_error(self, data):
        error = self._extract_error_message(data)
        if not error:
            return False
        lowered = error.lower()
        return ("логин" in lowered and "пароль" in lowered) or ("login" in lowered and "password" in lowered) or "auth" in lowered

    @property
    def debug_context(self):
        """Контекст диагностики отдельный на поток.

        Поиск и отправка заказа могут идти одновременно и делят один экземпляр
        поставщика: общий атрибут приклеил бы к ошибке поиска номер чужого
        заказа.
        """
        return getattr(self._debug_local, "context", None)

    @debug_context.setter
    def debug_context(self, value):
        self._debug_local.context = value

    def _debug(self, step, payload):
        sink = self.debug_sink
        if not callable(sink):
            return None
        context = self.debug_context
        if not context:
            if step not in self.ERROR_DEBUG_STEPS:
                return None
            context = {"scope": "search", "level": "error"}
        try:
            sink(step, self._trim_debug_payload(payload), dict(context))
        except Exception:
            pass
        return None

    def _trim_debug_payload(self, payload, limit=4000):
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            text = str(payload)
        if len(text) > limit:
            text = text[:limit] + f"...[обрезано, всего {len(text)} символов]"
        return text

    def _sanitize_payload(self, payload):
        if isinstance(payload, dict):
            result = {}
            for key, value in payload.items():
                if key in ("user_password", "password", "user_login", "login"):
                    result[key] = "***"
                elif key in ("data", "payload") and isinstance(value, str):
                    try:
                        result[key] = json.dumps(self._sanitize_payload(json.loads(value)), ensure_ascii=False)
                    except (TypeError, ValueError):
                        result[key] = value
                elif isinstance(value, dict):
                    result[key] = self._sanitize_payload(value)
                elif isinstance(value, list):
                    result[key] = [self._sanitize_payload(item) if isinstance(item, dict) else item for item in value]
                else:
                    result[key] = value
            return result
        if isinstance(payload, list):
            return [self._sanitize_payload(item) if isinstance(item, dict) else item for item in payload]
        return payload

    def _first_value(self, obj, *names):
        if not isinstance(obj, dict):
            return None
        for name in names:
            if name in obj and obj[name] not in (None, ""):
                return obj[name]
        return None

    def _deep_first_value(self, obj, *names):
        direct = self._first_value(obj, *names)
        if direct not in (None, ""):
            return direct
        if isinstance(obj, dict):
            for value in obj.values():
                if isinstance(value, dict):
                    found = self._deep_first_value(value, *names)
                    if found not in (None, ""):
                        return found
                elif isinstance(value, list):
                    for item in value:
                        found = self._deep_first_value(item, *names)
                        if found not in (None, ""):
                            return found
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
