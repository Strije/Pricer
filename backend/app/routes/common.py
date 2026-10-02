"""Общее для эндпоинтов: константы, задача с прогрессом (Job), представления заказов и предложений."""
import os
import time
import uuid

from app import supplier_catalog as catalog

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.dirname(APP_DIR)
STATIC = os.path.join(APP_DIR, "static")
COOKIE = "pricer_session"
SESSION_DAYS = 14
CSRF_HEADER = "x-requested-with"
RESULT_LIMIT = 200
OFFER_FIELDS = (
    "provider", "brand", "display_brand", "article", "name", "price", "sale_price", "available_quantity",
    "actual_order_quantity", "delivery_hours", "delivery_text", "warehouse", "is_cross", "returnable",
    "internal_offer_id",
)
SETTINGS_UPLOAD_LIMIT = 2 * 1024 * 1024
ORDER_FILE_LIMIT = 5 * 1024 * 1024
ORDER_FILE_ROWS = 1000
FILE_ALTERNATIVES = 30
FIND_LIMIT = 5000  # предложений в выдаче поиска (запись 02.10.2026: у 162622 — 2407)


class Job:
    def __init__(self, request, organization_id, kind="search"):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.request = request
        self.organization_id = organization_id
        self.events = []
        self.results = []
        self.done = False
        self.created = time.time()

    def push(self, kind, data):
        self.events.append({"event": kind, "data": data})


class _Patch:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def _offer(item):
    item = item or {}
    return {field: item.get(field) for field in OFFER_FIELDS if field in item}


def _result_payload(result):
    alternatives = result.get("alternatives") or []
    return {
        "status_code": result.get("status_code"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "quantity": result.get("quantity"),
        "offer": _offer(result.get("offer")) if result.get("offer") else None,
        "alternatives": [_offer(o) for o in alternatives[:RESULT_LIMIT]],
        "alternatives_total": len(alternatives),
    }


def _order_view(order):
    """Заказ для браузера: без сырых снимков ответов поставщиков (они остаются в базе)."""
    order = dict(order or {})
    order["items"] = [{k: v for k, v in item.items() if k != "snapshot"} for item in order.get("items") or []]
    order["groups"] = [{**g, "offers": [{k: v for k, v in o.items() if k != "snapshot"} for o in g.get("offers") or []]}
                       for g in order.get("groups") or []]
    return order


def _file_row_payload(index, result):
    payload = _result_payload(result)
    payload["alternatives"] = payload["alternatives"][:FILE_ALTERNATIVES]
    payload["index"] = index
    payload["source"] = {k: (result.get("source") or {}).get(k) for k in ("brand", "article", "name", "quantity")}
    return payload


def _account_view(account, box):
    secrets = box.open(account.secrets_sealed)
    title = catalog.CATALOG.get(account.section, {}).get("title", account.section)
    return {
        "id": account.id,
        "section": account.section,
        "title": title,
        "name": (account.config or {}).get("name") or title,
        "config": account.config or {},
        # Наружу уходят только имена заполненных секретных полей, не значения.
        "secrets_set": sorted(key for key, value in secrets.items() if value not in (None, "", [], {})),
        "status": account.status or {},
    }
