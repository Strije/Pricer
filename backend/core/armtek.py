import requests
import base64
import re
import datetime

from provider_adapter import OrderDeliveryUnknown, ProviderAdapter, call_order_once
from response_text import decode_response_text


class ArmtekProvider:
    def __init__(
        self,
        login,
        password,
        vkorg,
        kunnr,
        kunnr_we="",
        kunnr_za="",
        incoterms="",
        parnr="",
        vbeln="",
        use_pickup=False,
        timeout=12,
    ):
        self.login = login
        self.password = password
        self.vkorg = vkorg
        self.kunnr = kunnr
        self.kunnr_we = kunnr_we
        self.kunnr_za = kunnr_za
        self.incoterms = incoterms
        self.parnr = parnr
        self.vbeln = vbeln
        self.use_pickup = bool(use_pickup)
        self.timeout = int(timeout)
        self.base_url = "http://ws.armtek.ru/api"
        self.ping_base_url = "http://ws.armtek.ru/api"
        self.user_base_url = "http://ws.armtek.ru/api"
        self.user_base_urls = [
            "https://ws.armtek.ru/api",
            "http://ws.armtek.ru/api",
            "https://ws.speautoparts.com/api",
            "http://ws.speautoparts.com/api",
        ]
        self.adapter = ProviderAdapter(retries=2, backoff=0.4)
        # Заказ пакетом тяжелее одиночного запроса поиска, а повторов у него
        # нет — таймаут тут означает «исход неизвестен», не «не получилось».
        self.order_timeout = max(60, int(self.timeout) * 3)
        self.last_message = ""
        creds = base64.b64encode(f"{login}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
        }

    def ping(self):
        def _request():
            session = requests.Session()
            session.trust_env = False
            return session.get(
                f"{self.ping_base_url}/ws_ping/index",
                params={"format": "json"},
                headers=self.headers,
                timeout=min(self.timeout, 10),
                proxies={"http": None, "https": None},
            )

        resp = self.adapter.call(_request)
        if resp.status_code in (401, 403):
            raise RuntimeError("Неверный логин/пароль или нет доступа к API")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {self._message_from_response(resp)}")
        return True

    def get_user_vkorg_list(self):
        last_error = None
        for method in ("get", "post"):
            try:
                resp = self._request_user("getUserVkorgList", method=method)
                items = self._response_array(resp)
                if items:
                    return items
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return []

    def get_user_info(self, vkorg):
        resp = self._request_user("getUserInfo", data={
            "VKORG": vkorg,
            "STRUCTURE": "1",
            "FTPDATA": "1",
            "format": "json",
        })
        return self._response(resp)

    def user_info_table(self, info, *table_names):
        items = []
        self._collect_tables(info, set(table_names), items)
        return items

    def user_info_customers(self, info):
        return self._unique_items(
            self.user_info_table(info, "RG_TAB", "KUNNR_RG_TAB"),
            "KUNNR",
        )

    def user_info_consignees(self, info):
        items = self.user_info_table(info, "WE_TAB", "KUNNR_WE_TAB")
        items.extend(self._records_with_keys(info, "KUNNR_WE"))
        return self._unique_items(items, "KUNNR", "KUNNR_WE")

    def user_info_addresses(self, info):
        items = self.user_info_table(info, "ZA_TAB", "KUNNR_ZA_TAB", "ADDR_TAB", "ADDRESS_TAB")
        items.extend(self._records_with_keys(info, "KUNNR_ZA"))
        return self._unique_items(items, "KUNNR_ZA", "KUNNR", "ID", "CODE")

    def user_info_pickups(self, info):
        items = self.user_info_table(info, "INCOTERMS_TAB", "PICKUP_TAB", "STORE_TAB", "PVZ_TAB")
        items.extend(self._records_with_keys(info, "INCOTERMS"))
        return self._unique_items(items, "INCOTERMS", "ID", "CODE", "STORE")

    def user_info_contacts(self, info):
        items = self.user_info_table(info, "PARNR_TAB", "CONTACT_TAB", "CONTACTS_TAB")
        items.extend(self._records_with_keys(info, "PARNR"))
        return self._unique_items(items, "PARNR", "ID", "CODE")

    def user_info_contracts(self, info):
        items = self.user_info_table(info, "VBELN_TAB", "CONTRACT_TAB", "DOGOVOR_TAB")
        items.extend(self._records_with_keys(info, "VBELN"))
        return self._unique_items(items, "VBELN", "ID", "CODE")

    def _collect_tables(self, value, table_names, result):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in table_names:
                    result.extend(self._as_list(nested))
                self._collect_tables(nested, table_names, result)
        elif isinstance(value, list):
            for item in value:
                self._collect_tables(item, table_names, result)

    def _records_with_keys(self, value, *keys):
        found = []
        self._collect_records_with_keys(value, set(keys), found)
        return found

    def _collect_records_with_keys(self, value, keys, result):
        if isinstance(value, dict):
            if any(value.get(key) not in (None, "") for key in keys):
                result.append(value)
            for nested in value.values():
                self._collect_records_with_keys(nested, keys, result)
        elif isinstance(value, list):
            for item in value:
                self._collect_records_with_keys(item, keys, result)

    def _unique_items(self, items, *id_names):
        unique = []
        seen = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            value = str(self._value(item, *id_names) or "")
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(item)
        return unique

    def _default_from_items(self, items, *id_names):
        default_item = None
        for item in items:
            if str(self._value(item, "DEFAULT", "IS_DEFAULT") or "") in ("1", "true", "True", "Y"):
                default_item = item
                break
        if default_item is None and items:
            default_item = items[0]
        if default_item is None:
            return ""
        return str(self._value(default_item, *id_names) or "")

    def _effective_incoterms(self):
        return self.incoterms if self.use_pickup else ""

    def defaults_from_user_data(self, vkorg_items, user_info):
        defaults = {}
        if vkorg_items:
            defaults["vkorg"] = str(self._value(vkorg_items[0], "VKORG") or "")
        defaults["kunnr"] = self._default_from_items(self.user_info_customers(user_info), "KUNNR")
        defaults["kunnr_we"] = self._default_from_items(self.user_info_consignees(user_info), "KUNNR")
        defaults["kunnr_za"] = self._default_from_items(self.user_info_addresses(user_info), "KUNNR_ZA", "KUNNR", "ID", "CODE")
        defaults["incoterms"] = self._default_from_items(self.user_info_pickups(user_info), "INCOTERMS", "ID", "CODE", "STORE")
        defaults["parnr"] = self._default_from_items(self.user_info_contacts(user_info), "PARNR", "ID", "CODE")
        defaults["vbeln"] = self._default_from_items(self.user_info_contracts(user_info), "VBELN", "ID", "CODE")
        return {key: value for key, value in defaults.items() if value}

    def _legacy_defaults_from_user_data(self, vkorg_items, user_info):
        structures = self._as_list(self._value(user_info, "STRUCTURE"))
        customers = []
        for structure in structures:
            for item in self._as_list(self._value(structure, "RG_TAB")):
                customers.append(item)
        return customers

    def _request_user(self, action, method="post", data=None):
        params = {"format": "json"}
        if data:
            params.update(data)

        last_error = None
        for base_url in self.user_base_urls:
            try:
                resp = self._request_user_once(base_url, action, method, params)
                if resp.status_code == 200:
                    self.user_base_url = base_url
                    return resp.json()
                message = self._message_from_response(resp)
                last_error = RuntimeError(f"HTTP {resp.status_code}: {message}")
                if resp.status_code == 404 and "сервис не найден" in message.lower():
                    continue
                raise last_error
            except Exception as exc:
                last_error = exc
                if isinstance(exc, requests.exceptions.RequestException):
                    continue
                if "сервис не найден" not in str(exc).lower() and "HTTP 404" not in str(exc):
                    raise
        if last_error:
            raise last_error
        raise RuntimeError("Armtek ws_user не ответил")

    def _request_user_once(self, base_url, action, method, params):
        action = str(action or "").lower()
        def _request():
            session = requests.Session()
            session.trust_env = False
            if method == "get":
                return session.get(
                    f"{base_url}/ws_user/{action}",
                    params=params,
                    headers=self.headers,
                    timeout=self.timeout,
                    proxies={"http": None, "https": None},
                )
            return session.post(
                f"{base_url}/ws_user/{action}",
                data=params,
                headers=self.headers,
                timeout=self.timeout,
                proxies={"http": None, "https": None},
            )
        return self.adapter.call(_request)

    def _message_from_response(self, resp):
        try:
            data = resp.json()
            message = self._extract_message(data)
            if message:
                return message[:160]
        except Exception:
            pass
        return decode_response_text(resp, 160).strip()

    def _response(self, data):
        if not isinstance(data, dict):
            return {}
        status = data.get("STATUS")
        if status not in (None, 200, "200"):
            message = self._extract_message(data)
            if message:
                raise RuntimeError(message)
            raise RuntimeError(str(data)[:120])
        return data.get("RESP") or data

    def _response_array(self, data):
        resp = self._response(data)
        for key in ("ARRAY", "VKORG_LIST", "VKORG_TAB", "LIST", "DATA"):
            value = self._value(resp, key)
            if value:
                return self._as_list(value)
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict) and self._value(resp, "VKORG"):
            return [resp]
        return []

    def _extract_message(self, data):
        for key in ("MESSAGE", "message", "ERROR", "error", "TEXT", "text"):
            value = self._value(data, key)
            if value:
                return str(value)
        messages = self._value(data, "MESSAGES")
        for item in self._as_list(messages):
            value = self._value(item, "TEXT", "MESSAGE", "ERROR")
            if value:
                return str(value)
        resp = self._value(data, "RESP")
        if isinstance(resp, dict):
            return self._extract_message(resp)
        if isinstance(resp, list):
            for item in resp:
                message = self._extract_message(item)
                if message:
                    return message
        return ""

    def _value(self, obj, *names):
        for name in names:
            if isinstance(obj, dict) and name in obj:
                return obj.get(name)
            if hasattr(obj, name):
                return getattr(obj, name)
        return None

    def _as_list(self, value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def _parse_date_days(self, date_str):
        if not date_str or len(date_str) < 8:
            return 0
        try:
            dt = datetime.datetime.strptime(date_str[:14], "%Y%m%d%H%M%S")
            delta = dt - datetime.datetime.now()
            return max(1, round(delta.total_seconds() / 86400))
        except:
            try:
                dt = datetime.datetime.strptime(date_str[:8], "%Y%m%d")
                delta = dt - datetime.datetime.now()
                return max(1, round(delta.total_seconds() / 86400))
            except:
                return 0

    def get_prices(self, article, brand=""):
        self.last_message = ""
        results = []
        try:
            params = {
                "VKORG": self.vkorg,
                "KUNNR_RG": self.kunnr,
                "PIN": article,
                "QUERY_TYPE": "2",
                "format": "json",
            }
            if self.kunnr_za:
                params["KUNNR_ZA"] = self.kunnr_za
            if self._effective_incoterms():
                params["INCOTERMS"] = self._effective_incoterms()
            if self.vbeln:
                params["VBELN"] = self.vbeln
            if brand:
                params["BRAND"] = brand

            def _request():
                session = requests.Session()
                session.trust_env = False
                return session.post(
                    f"{self.base_url}/ws_search/search",
                    data=params,
                    headers=self.headers,
                    timeout=self.timeout,
                    proxies={"http": None, "https": None},
                )
            resp = self.adapter.call(_request)
            if resp.status_code != 200:
                self.last_message = f"HTTP {resp.status_code}: {self._message_from_response(resp)}"
                return results

            data = resp.json()
            if not isinstance(data, dict):
                self.last_message = "Armtek вернул не JSON-объект"
                return results
            status = data.get("STATUS")
            if status not in (None, 200, "200"):
                self.last_message = self._extract_message(data) or str(data)[:120]
                return results

            resp_data = data.get("RESP", {})
            if isinstance(resp_data, list):
                items = resp_data
            elif isinstance(resp_data, dict):
                items = resp_data.get("ARRAY", [])
            else:
                items = []
            if not isinstance(items, list):
                self.last_message = "Armtek: в ответе нет списка ARRAY"
                return results

            for item in items:
                if not isinstance(item, dict):
                    continue
                price_raw = item.get("PRICE", "0")
                try:
                    price = float(price_raw)
                except:
                    price = 0.0

                if price <= 0:
                    continue

                raw_qty = item.get("RVALUE", "0")
                try:
                    qty = int(float(raw_qty))
                except:
                    qty = 0

                mult_raw = item.get("RDPRF", "1")
                try:
                    mult = int(float(mult_raw))
                except:
                    mult = 1

                dp_raw = item.get("VENSL", "")
                try:
                    dp = float(dp_raw) if dp_raw else 0
                except:
                    dp = 0

                days = self._parse_date_days(item.get("DLVDT", ""))

                results.append({
                    "provider": "Armtek",
                    "article": item.get("PIN", article),
                    "brand": item.get("BRAND", brand),
                    "price": price,
                    "days": days,
                    "quantity": str(qty),
                    "logo": item.get("KEYZAK", "-"),
                    "name": item.get("NAME", "No name"),
                    "delivery_percent": dp,
                    "multiplicity": mult,
                    "is_original": item.get("ANALOG", "") == "",
                    "is_analog": item.get("ANALOG", "") != "",
                    "keyzak": item.get("KEYZAK", ""),
                    "artid": item.get("ARTID", ""),
                    # RETDAYS — дней на возврат; 0 = без возврата.
                    "return_days": item.get("RETDAYS", ""),
                    "not_returnable": str(item.get("RETDAYS", "")).strip() in ("0", "00"),
                })

        except Exception as e:
            self.last_message = str(e)
            print(f"Ошибка Armtek: {e}")

        return results

    def get_brand_candidates(self, article):
        self.last_message = ""
        article = str(article or "").strip()
        if not article:
            return []
        if not self.vkorg:
            self.last_message = "Armtek: не указан VKORG"
            return []

        params = {
            "VKORG": self.vkorg,
            "PIN": article,
            "PROGRAM": "",
            "format": "json",
        }

        try:
            def _request():
                session = requests.Session()
                session.trust_env = False
                return session.post(
                    f"{self.base_url}/ws_search/assortment_search",
                    data=params,
                    headers=self.headers,
                    timeout=min(self.timeout, 5),
                    proxies={"http": None, "https": None},
                )

            resp = self.adapter.call(_request)
            if resp.status_code != 200:
                self.last_message = f"HTTP {resp.status_code}: {self._message_from_response(resp)}"
                return []
            data = resp.json()
            items = self._response_array(data)
        except Exception as exc:
            self.last_message = str(exc)
            return []

        result = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            brand = str(self._value(item, "BRAND") or "").strip()
            if not brand:
                continue
            item_article = str(self._value(item, "PIN") or article or "").strip()
            name = str(self._value(item, "NAME") or "").strip()
            marker = (brand.upper(), re.sub(r"[^A-Z0-9]", "", item_article.upper()), name.upper())
            if marker in seen:
                continue
            seen.add(marker)
            result.append({
                "brand": brand,
                "article": item_article,
                "name": name,
                "source": "assortment_search",
            })
        return result

    def get_brands(self, article):
        brands = []
        for item in self.get_brand_candidates(article):
            brand = item.get("brand")
            if brand and brand not in brands:
                brands.append(brand)
        return brands

    def add_to_basket(self, item, quantity=1, comment=""):
        return self.add_to_basket_batch([{"item": item, "quantity": quantity}], comment=comment)[0]

    def add_to_basket_batch(self, rows, comment=""):
        """Один createorder на все позиции: таблица ITEMS[i][...] так и задумана.

        Armtek отвечает общим STATUS и MESSAGES, без привязки к строкам,
        поэтому исход у пакета общий: заказ либо создан целиком, либо нет.
        rows — список {"item": предложение, "quantity": количество};
        ответ — список той же длины и в том же порядке.
        """
        rows = list(rows or [])
        if not rows:
            return []
        payload = {
            "VKORG": self.vkorg,
            "KUNRG": self.kunnr,
            "KUNWE": self.kunnr_we,
            "KUNZA": self.kunnr_za,
            "INCOTERMS": self._effective_incoterms(),
            "PARNR": self.parnr,
            "VBELN": self.vbeln,
            "format": "json",
        }
        for index, row in enumerate(rows):
            item = dict((row or {}).get("item") or {})
            quantity = int((row or {}).get("quantity") or 1)
            payload[f"ITEMS[{index}][PIN]"] = item.get("article", "")
            payload[f"ITEMS[{index}][BRAND]"] = item.get("brand", "")
            payload[f"ITEMS[{index}][KWMENG]"] = str(quantity)
            payload[f"ITEMS[{index}][KEYZAK]"] = item.get("keyzak", "")
        if comment:
            payload["TEXT_ORD"] = comment

        def _request():
            session = requests.Session()
            session.trust_env = False
            return session.post(
                f"{self.base_url}/ws_order/createorder",
                data=payload,
                headers=self.headers,
                timeout=self.order_timeout,
                proxies={"http": None, "https": None},
            )

        def _all(result):
            return [dict(result) for _ in rows]

        try:
            # Без повторов: createorder по таймауту чтения мог уже создать
            # заказ, и вторая попытка сделала бы дубль на весь пакет.
            resp = call_order_once(_request)
        except OrderDeliveryUnknown as exc:
            return _all({
                "success": False,
                "uncertain": True,
                "error": f"Armtek: ответ на заказ не получен ({exc}); проверьте заказы у поставщика",
            })
        except Exception as exc:
            return _all({"success": False, "error": str(exc)})

        text = decode_response_text(resp).strip()
        if resp.status_code != 200:
            return _all({"success": False, "error": f"HTTP {resp.status_code}: {text[:200]}"})
        try:
            json_data = resp.json()
        except Exception:
            return _all({"success": True, "data": text[:200]})
        if json_data.get("STATUS", 0) == 200:
            return _all({"success": True, "data": text[:200]})
        msgs = json_data.get("MESSAGES", [])
        err_text = msgs[0].get("TEXT", text[:200]) if msgs else text[:200]
        return _all({"success": False, "error": err_text})
