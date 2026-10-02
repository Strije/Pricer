"""Клиент 1С через стандартный интерфейс OData (замена COM-коннектора one_c_provider).

Названия сущностей и реквизитов зависят от конфигурации 1С (УТ, Розница, БП, самописная),
поэтому все они задаются в OneCConfig и FieldMap. Модуль не знает ничего о Qt и FastAPI.

Идемпотентность: у каждого документа есть внешний ключ (номер заказа Pricer). Перед созданием
документа клиент ищет в 1С документ с таким ключом. Если ответ на создание потерялся
(таймаут), повторная отправка не создаст дубль, а найдёт уже созданный документ.
"""
from dataclasses import dataclass, field
from urllib.parse import quote

import requests


class OneCError(Exception):
    """Ошибка ответа 1С: документ не создан, повторять бессмысленно без исправления данных."""


class OneCUnavailable(Exception):
    """1С недоступна или не ответила: безопасно повторить позже."""


@dataclass
class OneCConfig:
    base_url: str  # например https://host/base/odata/standard.odata
    user: str = ""
    password: str = ""
    timeout: int = 20
    verify_tls: bool = True
    client_order_entity: str = "Document_ЗаказКлиента"
    supplier_order_entity: str = "Document_ЗаказПоставщику"
    # Реквизит документа, в который пишется внешний ключ Pricer (создаётся в 1С отдельно).
    external_id_field: str = "ВнешнийИдентификатор"


@dataclass
class FieldMap:
    """Соответствие полей Pricer реквизитам документа 1С. Меняется под конфигурацию."""
    header: dict = field(default_factory=lambda: {
        "external_id": "ВнешнийИдентификатор",
        "order_number": "Комментарий",
        "counterparty": "Контрагент",
        "manager": "Ответственный",
        "comment": "Комментарий",
    })
    row: dict = field(default_factory=lambda: {
        "article": "Артикул",
        "brand": "Бренд",
        "name": "Наименование",
        "quantity": "Количество",
        "price": "Цена",
        "sum": "Сумма",
    })
    rows_section: str = "Товары"


class OneCODataClient:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()
        self.session.trust_env = False
        if config.user:
            self.session.auth = (config.user, config.password)

    # ---------- публичные операции ----------

    def ping(self):
        return self._request("GET", f"{self._base()}/$metadata", expect_json=False) is not None

    def find_by_external_id(self, entity, external_id):
        field_name = self.config.external_id_field
        flt = quote(f"{field_name} eq '{_escape(external_id)}'", safe="=' ")
        data = self._request("GET", f"{self._base()}/{entity}?$format=json&$top=1&$filter={flt}")
        rows = (data or {}).get("value") or []
        return rows[0] if rows else None

    def create_document(self, entity, payload, external_id):
        """Создаёт документ один раз. Возвращает (документ, created)."""
        existing = self.find_by_external_id(entity, external_id)
        if existing is not None:
            return existing, False
        body = dict(payload)
        body[self.config.external_id_field] = external_id
        try:
            created = self._request("POST", f"{self._base()}/{entity}?$format=json", json_body=body)
        except OneCUnavailable:
            # Ответ мог потеряться при уже созданном документе: проверяем перед повтором.
            existing = self.find_by_external_id(entity, external_id)
            if existing is not None:
                return existing, False
            raise
        return created, True

    # ---------- внутреннее ----------

    def _base(self):
        return self.config.base_url.rstrip("/")

    def _request(self, method, url, json_body=None, expect_json=True):
        try:
            response = self.session.request(
                method, url, json=json_body, timeout=self.config.timeout, verify=self.config.verify_tls,
                headers={"Accept": "application/json"},
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            raise OneCUnavailable(exc.__class__.__name__) from exc
        if response.status_code >= 500:
            raise OneCUnavailable(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise OneCError(f"HTTP {response.status_code}: {response.text[:300]}")
        if not expect_json:
            return True
        try:
            return response.json()
        except ValueError as exc:
            raise OneCError("ответ 1С не является JSON") from exc


def _escape(value):
    return str(value).replace("'", "''")


# ---------- сборка документов из заказа Pricer ----------

def client_order_document(order, field_map=None):
    """Заказ клиента: продажные цены."""
    field_map = field_map or FieldMap()
    rows = []
    for item in order.get("items") or []:
        qty = item.get("quantity") or 0
        price = item.get("sale_price") or 0
        rows.append(_row(field_map, item, qty, price, item.get("sale_total")))
    header = _header(field_map, order, order["order_id"])
    header[field_map.header["counterparty"]] = (order.get("client") or {}).get("name", "")
    header[field_map.rows_section] = rows
    return order["order_id"], header


def supplier_order_documents(order, field_map=None):
    """Заказы поставщикам: по одному документу на поставщика, закупочные цены.

    Отправлять в 1С имеет смысл только позиции, которые реально ушли поставщику (submitted).
    Позиции со статусом unknown передаются отдельным списком на ручную проверку.
    """
    field_map = field_map or FieldMap()
    by_provider = {}
    for item in order.get("items") or []:
        status = item.get("submit_status") or ""
        if status not in ("submitted", "unknown"):
            continue
        by_provider.setdefault(item.get("provider") or "", []).append(item)
    documents = []
    for provider, items in sorted(by_provider.items()):
        external_id = f"{order['order_id']}:{provider}"
        confirmed = [i for i in items if i.get("submit_status") == "submitted"]
        unknown = [i for i in items if i.get("submit_status") == "unknown"]
        header = _header(field_map, order, external_id)
        header[field_map.header["counterparty"]] = provider
        header[field_map.rows_section] = [
            _row(field_map, i, i.get("quantity") or 0, i.get("purchase_price") or 0, i.get("purchase_total"))
            for i in confirmed
        ]
        documents.append({
            "external_id": external_id,
            "provider": provider,
            "payload": header,
            "needs_review": [i.get("article") for i in unknown],
        })
    return documents


def _header(field_map, order, external_id):
    manager = (order.get("manager") or {}).get("name", "")
    return {
        field_map.header["external_id"]: external_id,
        field_map.header["manager"]: manager,
        field_map.header["comment"]: f"Pricer {order.get('order_id', '')} {order.get('comment', '')}".strip(),
    }


def _row(field_map, item, qty, price, total):
    cols = field_map.row
    return {
        cols["article"]: item.get("article", ""),
        cols["brand"]: item.get("brand", ""),
        cols["name"]: item.get("name", ""),
        cols["quantity"]: qty,
        cols["price"]: price,
        cols["sum"]: total if total is not None else round(float(price) * float(qty), 2),
    }
