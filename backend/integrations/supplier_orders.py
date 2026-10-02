"""Заказы у поставщиков и статусы их позиций (перенесено из 1С-обработки «Загрузка поступлений
поставщиков» v0.14.11: функции ЗаказыАрмтека, ЗаказыРосскоЗаПериод, ЗаказыФаворитаДляКонтроля,
ЗаказыФорумаДляКонтроля, ОтгрузкиАвтото, ЗаказыАБСДляКонтроля, ЗаказыABCP, ЗаказыЛиги,
ЗаказыМикадоДляКонтроля).

Каждая функция берёт учётные данные у адаптера поставщика (core/*.py) и возвращает строки
одного вида: {order, date, article, brand, name, quantity, status, refused, comment}.
Частичный отказ (отклонено/снято меньше заказанного) — отдельной строкой с refused=True,
как в 1С. Все запросы только читают: ничего не заказывают и не меняют.
"""
import datetime
import json
import xml.etree.ElementTree as ET

import requests

TIMEOUT = 60


def _session():
    session = requests.Session()
    session.trust_env = False
    return session


def _num(value):
    try:
        return float(str(value if value is not None else "0").replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def _s(obj, key):
    value = (obj or {}).get(key) if isinstance(obj, dict) else None
    return "" if value is None else str(value).strip()


def _items(value):
    """Списки приходят то массивом, то объектом {"0": {...}}."""
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        return [x for x in value.values() if isinstance(x, dict)]
    return []


def _row(order, date, article, brand, name, quantity, status, refused=False, comment=""):
    return {"order": str(order or ""), "date": date, "article": str(article or ""), "brand": str(brand or ""),
            "name": str(name or ""), "quantity": quantity, "status": str(status or ""), "refused": bool(refused),
            "comment": str(comment or "")}


def _date(text, *formats):
    """Дата из строки поставщика в ISO; хвост (доли секунд, зона) отбрасывается."""
    text = str(text or "").strip()
    for fmt in formats:
        for candidate in (text, text[:19], text[:14], text[:10], text[:8]):
            try:
                return datetime.datetime.strptime(candidate, fmt).isoformat()
            except ValueError:
                continue
    return None


def _split_partial(rows_out, order, date, article, brand, name, ordered, cancelled, status, cancel_status, comment=""):
    """Как в 1С: отменённая часть — отдельной строкой-отказом, живая — своим статусом."""
    cancelled = min(max(cancelled, 0), ordered) if ordered else cancelled
    if 0 < cancelled < ordered:
        rows_out.append(_row(order, date, article, brand, name, cancelled, cancel_status, True, comment))
        ordered -= cancelled
        cancelled = 0
    fully = cancelled >= ordered > 0
    rows_out.append(_row(order, date, article, brand, name, ordered, cancel_status if fully else status, fully, comment))


# ---------- Армтек ----------

def armtek_orders(provider, since):
    """ws_reports/getOrderPositionsReportByDate2: позиции заказов за период (ЗаказыАрмтека)."""
    data = {"VKORG": provider.vkorg, "KUNNR_RG": provider.kunnr, "SCRDATE": since.strftime("%Y%m%d"),
            "ECRDATE": datetime.date.today().strftime("%Y%m%d"), "TYPEZK_SALE": "1", "format": "json"}
    response = _session().post("https://ws.armtek.ru/api/ws_reports/getOrderPositionsReportByDate2",
                               data=data, headers=provider.headers, timeout=TIMEOUT)
    response.raise_for_status()
    rows = []
    data = response.json() or {}
    resp = data.get("RESP") if isinstance(data.get("RESP"), dict) else data
    for p in _items(resp.get("DATA")):
        ordered = _num(p.get("ZZKWMENG")) or _num(p.get("KWMENG"))
        rejected = _num(p.get("REJECTED"))
        status = _s(p, "STATUS") or _s(p, "ORDER_STATUS")
        date = _date(_s(p, "ORDER_DATE"), "%Y%m%d%H%M%S", "%Y%m%d")
        _split_partial(rows, _s(p, "ORDER"), date, _s(p, "PIN"), _s(p, "BRAND"), _s(p, "NAME"), ordered, rejected,
                       status, "отклонено: " + _s(p, "ABGRU_TXT"), _s(p, "NOTE"))
    return rows


# ---------- Росско ----------

ROSSKO_STATUSES = {"0": "ждет подтверждения", "1": "комплектуется", "2": "отгружено", "3": "готово к отгрузке",
                   "5": "ожидаем поступление", "6": "на складе филиала", "7": "нет в наличии", "8": "отменен клиентом",
                   "9": "просрочен", "31": "ожидаем товар на складе", "36": "товар возвращен"}
ROSSKO_REFUSED = {"7", "8", "9", "36"}
_ROSSKO_NS = "https://api.rossko.ru/"


def _rossko_call(method, key1, key2, params_xml):
    def element(name, value):
        value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<ns0:{name}>{value}</ns0:{name}>"
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap-env:Body><ns0:{method} xmlns:ns0="{_ROSSKO_NS}">'
            + element("KEY1", key1) + element("KEY2", key2) + "".join(element(k, v) for k, v in params_xml)
            + f"</ns0:{method}></soap-env:Body></soap-env:Envelope>")
    response = _session().post(f"https://api.rossko.ru/service/v2.1/{method}", data=body.encode("utf-8"),
                               headers={"Content-Type": "text/xml; charset=utf-8",
                                        "SOAPAction": f'"https://api.rossko.ru/service/v2.1/{method}"'}, timeout=90)
    response.raise_for_status()
    return ET.fromstring(response.content)


def _local(element):
    return element.tag.rsplit("}", 1)[-1]


def _children(node, name):
    return [c for c in list(node) if _local(c) == name]


def _text(node, name):
    found = _children(node, name)
    return (found[0].text or "").strip() if found else ""


def rossko_orders(provider, since):
    """GetOrders за период (ЗаказыРосскоЗаПериод); коды статусов — СтатусРосско."""
    root = _rossko_call("GetOrders", provider.key1, provider.key2,
                        [("limit", "500"), ("start_date", since.strftime("%Y-%m-%d")),
                         ("end_date", datetime.date.today().strftime("%Y-%m-%d"))])
    result = next((e for e in root.iter() if _local(e) == "OrdersResult"), None)
    rows = []
    if result is None or _text(result, "success").lower() != "true":
        return rows
    for orders_list in _children(result, "OrdersList"):
        for order in _children(orders_list, "Order"):
            number = _text(order, "id")
            date = _date(_text(order, "created_date"), "%d.%m.%Y %H:%M:%S", "%d.%m.%Y")
            for parts in _children(order, "parts"):
                for part in _children(parts, "part"):
                    code = _text(part, "status")
                    if _num(_text(part, "count")) <= 0:
                        continue  # служебный остаток строки (count 0, напр. статус 34)
                    rows.append(_row(number, date, _text(part, "partnumber"), _text(part, "brand"), _text(part, "name"),
                                     _num(_text(part, "count")), ROSSKO_STATUSES.get(code, f"статус {code}"),
                                     code in ROSSKO_REFUSED, _text(part, "comment")))
    return rows


# ---------- Фаворит ----------

def favorit_orders(provider, since):
    """order/ за период постранично по 40 (ЗаказыФаворитаЗаПериод); статусы 6 и 8 — отмена заказа."""
    headers = {"Accept": "application/json", "X-Favorit-ClientKey": provider.api_key,
               "X-Favorit-DeveloperKey": provider.developer_key}
    orders, seen = [], set()
    for page in range(50):
        params = {"dateStart": since.strftime("%Y%m%d"), "dateEnd": datetime.date.today().strftime("%Y%m%d"),
                  "rowsorder": "1", "count": str(page * 40)}
        response = _session().get("https://api.favorit-parts.ru/ws/v1/order/", params=params, headers=headers, timeout=TIMEOUT)
        if response.status_code >= 500:  # как в 1С: при 5xx — ключи в параметрах
            response = _session().get("https://api.favorit-parts.ru/ws/v1/order/",
                                      params={**params, "key": provider.api_key, "developerKey": provider.developer_key},
                                      headers={"Accept": "application/json"}, timeout=TIMEOUT)
        response.raise_for_status()
        batch = _items(response.json())
        for order in batch:
            if _s(order, "id") not in seen:
                seen.add(_s(order, "id"))
                orders.append(order)
        if len(batch) < 40:
            break
    rows = []
    for order in orders:
        status = order.get("status") or {}
        code, name = _s(status, "id"), _s(status, "name")
        cancelled_order = code in ("6", "8")
        number = _s(order, "number") or "без номера"
        date = _date(_s(order, "date"), "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
        for position in _items(order.get("goodsList")):
            goods = position.get("goods") or {}
            ordered = _num(position.get("count"))
            cancelled = ordered if cancelled_order else _num(position.get("countCancel"))
            _split_partial(rows, number, date, _s(goods, "number"), _s(goods, "brand"), _s(goods, "name"),
                           ordered, cancelled, name, "снято: " + name, _s(position, "comment"))
    return rows


# ---------- Форум-Авто ----------

def forum_orders(provider, since):
    """listOrders — активные заказы (ЗаказыФорумаДляКонтроля); FaultCode 26 — активных нет.
    Позиции, не подтверждённые к статусу 50+, — отказ (k1 заказано, k3 подтверждено)."""
    response = _session().get(f"{provider.base_url}/listOrders", params={"login": provider.login, "pass": provider.password},
                              timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, dict):
        if _s(errors, "FaultCode") == "26":
            return []
        raise RuntimeError(f"Форум-Авто listOrders: {_s(errors, 'FaultCode')} {_s(errors, 'FaultString')}")
    rows = []
    for order in _items(data):
        code = int(_num(order.get("statusId")))
        name = _s(order, "status")
        number = _s(order, "nr") or _s(order, "did")
        date = _date(_s(order, "dtc"), "%d.%m.%Y %H:%M:%S", "%d.%m.%Y")
        for position in _items(order.get("arTovs")):
            ordered, confirmed = _num(position.get("k1")), _num(position.get("k3"))
            cancelled = max(ordered - confirmed, 0) if code >= 50 else 0
            _split_partial(rows, number, date, _s(position, "nr"), _s(position, "brand"), _s(position, "tovname"),
                           ordered, cancelled, name, "не подтверждено: " + name)
    return rows


# ---------- Авто-то ----------

def avtoto_orders(provider, since):
    """GetShippingList — отгрузки (ОтгрузкиАвтото): позиция отгружена, приходит через ~2 дня."""
    rows = []
    for page in range(1, 51):
        payload = {"user": provider._user_payload(), "from": since.strftime("%d.%m.%Y"),
                   "to": datetime.date.today().strftime("%d.%m.%Y"), "page_num": page}
        response = _session().post(provider.base_url, data={"action": "GetShippingList", "data": json.dumps(payload)},
                                   timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json() or {}
        for shipping in _items(data.get("Shippings")):
            date = _date(_s(shipping, "Date"), "%d-%m-%Y %H:%M:%S")
            for position in _items(shipping.get("Orders")):
                rows.append(_row(_s(position, "OrderId"), date, _s(position, "Code"), _s(position, "Manuf"),
                                 _s(position, "Name"), _num(position.get("Count")),
                                 f"отгружено ({_s(shipping, 'Type') or 'отгрузка'} {_s(shipping, 'Id')})", False,
                                 _s(position, "Comment")))
        if page >= int(_num((data.get("Pagination") or {}).get("CountPages")) or 1):
            break
    return rows


# ---------- АБС (ABSTD) ----------

ABSTD_CANCEL = {"-3", "-7", "11", "12", "19"}


def abstd_orders(provider, since):
    """api-get_orders за период (ЗаказыАБСДляКонтроля); отменные статусы: -3, -7, 11, 12, 19."""
    params = {"date_begin": since.strftime("%d.%m.%Y"), "date_end": datetime.date.today().strftime("%d.%m.%Y"),
              "format": "json"}
    response = None
    for login in provider._auth_logins():
        response = _session().get(f"{provider.base_url}/api-get_orders", params={**params, "auth": provider._calc_auth(login)},
                                  timeout=90)
        if response.status_code != 403:
            break
    response.raise_for_status()
    data = response.json() or {}
    if _s(data, "status") and _s(data, "status").upper() != "OK":
        raise RuntimeError(f"АБС api-get_orders: {_s(data, 'status')}")
    rows = []
    for order in _items(data.get("orders")):
        number = _s(order, "order_id")
        date = _date(_s(order, "create_date"), "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")
        for position in _items(order.get("order_products")):
            alive = cancelled = 0.0
            alive_names, cancel_names = [], []
            for status in _items(position.get("product_statuses")):
                quantity = _num(status.get("quantity"))
                target = cancel_names if _s(status, "status_id") in ABSTD_CANCEL else alive_names
                if _s(status, "status_id") in ABSTD_CANCEL:
                    cancelled += quantity
                else:
                    alive += quantity
                if _s(status, "status_name") and _s(status, "status_name") not in target:
                    target.append(_s(status, "status_name"))
            if alive == 0 and cancelled == 0:
                alive = _num(position.get("quantity"))
            comment = (_s(position, "product_description") + " " + _s(order, "order_description")).strip()
            if cancelled > 0:
                rows.append(_row(number, date, _s(position, "article"), _s(position, "brand"), _s(position, "product_name"),
                                 cancelled, ", ".join(cancel_names), True, comment))
            if alive > 0:
                rows.append(_row(number, date, _s(position, "article"), _s(position, "brand"), _s(position, "product_name"),
                                 alive, ", ".join(alive_names), False, comment))
    return rows


# ---------- ABCP ----------

ABCP_REFUSAL_WORDS = ("отказ", "отмен", "снят", "нет в наличии", "аннулир", "возврат")


def abcp_is_refusal(status):
    """Названия статусов у каждого магазина ABCP свои — отказ узнаётся по словам (ЭтоОтказABCP)."""
    text = str(status or "").lower()
    return any(word in text for word in ABCP_REFUSAL_WORDS)


def abcp_orders(provider, since):
    """orders постранично (ЗаказыABCP, СтрокиЗаказаABCP): недопоставленное и отказные статусы — отказом."""
    rows = []
    for page in range(20):
        if callable(getattr(provider, "get_orders", None)):
            data = provider.get_orders(limit=100, skip=page * 100)
        else:
            data = provider._request("GET", "orders", {"format": "p", "limit": 100, "skip": page * 100})
        if isinstance(data, dict) and "items" in data:
            data = data["items"]
        orders = _items(data)
        further = False
        for order in orders:
            date = _date(_s(order, "date"), "%Y-%m-%d %H:%M:%S")
            if date and date < datetime.datetime.combine(since, datetime.time()).isoformat():
                continue
            further = True
            for p in _items(order.get("positions")):
                ordered, final = _num(p.get("quantityOrdered")), _num(p.get("quantity"))
                ordered = ordered or final
                status = _s(p, "status")
                refused = abcp_is_refusal(status)
                cancelled = ordered if refused else max(ordered - final, 0)
                row_comment = (_s(p, "comment") + " " + _s(order, "comment")).strip()
                article = _s(p, "numberFix") or _s(p, "number")
                if not ordered:  # количества нет — строка без количества (сопоставитель так и поймёт)
                    row = _row(_s(order, "number"), date, article, _s(p, "brand"), _s(p, "description"), 0,
                               status, refused, row_comment)
                    row["position_id"], row["supplier_code"] = _s(p, "positionId") or _s(p, "id"), _s(p, "supplierCode")
                    rows.append(row)
                    continue
                if cancelled > 0:
                    row = _row(_s(order, "number"), date, article, _s(p, "brand"), _s(p, "description"), cancelled,
                               status if refused else "снято: " + status, True, row_comment)
                    row["position_id"], row["supplier_code"] = _s(p, "positionId") or _s(p, "id"), _s(p, "supplierCode")
                    rows.append(row)
                if ordered - cancelled > 0:
                    row = _row(_s(order, "number"), date, article, _s(p, "brand"), _s(p, "description"),
                               ordered - cancelled, status, False, row_comment)
                    row["position_id"], row["supplier_code"] = _s(p, "positionId") or _s(p, "id"), _s(p, "supplierCode")
                    rows.append(row)
        if not further or len(orders) < 100:
            break
    return rows


# ---------- Профит-Лига ----------

def prlg_orders(provider, since):
    """orders/list постранично (ЗаказыЛиги); статусы позиции 6 и 20 — отказ."""
    rows, seen = [], set()
    for page in range(1, 21):
        response = _session().get("https://api.pr-lg.ru/orders/list", params={"secret": provider.api_key, "page": page},
                                  timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json() or {}
        if str(data.get("status") or "").lower() in ("error", "auth-error", "false"):
            raise RuntimeError(f"Профит-Лига orders/list: {_s(data, 'err')}{_s(data, 'message')}")
        orders = _items(data.get("data"))
        if not orders:
            break
        further = False
        for order in orders:
            date = _date(_s(order, "datetime"), "%Y-%m-%d %H:%M:%S")
            if date and date < datetime.datetime.combine(since, datetime.time()).isoformat():
                continue
            further = True
            if _s(order, "order_id") in seen:
                continue
            seen.add(_s(order, "order_id"))
            for p in _items(order.get("products")):
                rows.append(_row(_s(order, "order_id"), date, _s(p, "article"), _s(p, "brand"), _s(p, "description"),
                                 _num(p.get("quantity")), _s(p, "status"), _s(p, "status_id") in ("6", "20"),
                                 _s(p, "comment")))
        if not further or page >= int(_num(data.get("pages")) or 1):
            break
    return rows


# ---------- Микадо ----------

def _mikado_records(text, record_name):
    root = ET.fromstring(text)
    out = []
    for element in root.iter():
        if _local(element) == record_name:
            out.append({_local(child): (child.text or "").strip() for child in list(element)})
    return out


def mikado_orders(provider, since):
    """Basket_List (ЗаказыМикадоДляКонтроля): Zakaz — под заказ, Stock — со склада Микадо, Otkaz — отказ.
    Бренда в ответе нет: ZakazCode вида «ПРЕФИКС-код», сопоставление по артикулу."""
    response = _session().post("https://www.mikado-parts.ru/ws1/basket.asmx/Basket_List",
                               data={"ClientID": provider.client_id, "Password": provider.password}, timeout=TIMEOUT)
    response.raise_for_status()
    rows = []
    labels = {"Zakaz": "заказано, срок ", "Stock": "со склада Микадо, срок "}
    for record in _mikado_records(response.text, "BasketItem"):
        status = record.get("Status", "")
        if status not in ("Zakaz", "Stock", "Otkaz"):
            continue
        code = record.get("ZakazCode", "")
        article = code.split("-", 1)[1] if "-" in code else code
        text = "отказ Микадо" if status == "Otkaz" else labels[status] + record.get("Srok", "")
        rows.append(_row("корзина " + record.get("ID", ""), None, article, "", record.get("Name", ""),
                         _num(record.get("QTY")), text, status == "Otkaz", record.get("Notes", "")))
    return rows


# Поставщик (имя класса адаптера) -> функция. Tradesoft — свой GetItemsStatus в app/supplier_lines.py.
FETCHERS = {
    "ArmtekProvider": armtek_orders, "RosskoProvider": rossko_orders, "FavoritProvider": favorit_orders,
    "ForumAutoProvider": forum_orders, "AvtotoProvider": avtoto_orders, "AbstdProvider": abstd_orders,
    "PrLgProvider": prlg_orders, "MikadoProvider": mikado_orders,
}


def fetcher_for(provider):
    names = [cls.__name__ for cls in type(provider).__mro__]
    for name in names:
        if name in FETCHERS:
            return FETCHERS[name]
    if "AbcpSupplierProvider" in names:
        return abcp_orders
    return None

