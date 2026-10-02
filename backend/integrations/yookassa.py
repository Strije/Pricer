"""ЮKassa (API v3): оплата подбора по ссылке — карта, СБП, SberPay на странице ЮKassa.

Как в настройках ABCP: shopId и secretKey (Basic-авторизация), тестовый магазин, чеки 54-ФЗ
(receipt: система налогообложения, НДС, признаки предмета и способа расчёта), способ оплаты
выбирает клиент на стороне ЮKassa (payment_method_data не передаём).
Документация: https://yookassa.ru/developers/api
"""
import re
import uuid

import requests

API = "https://api.yookassa.ru/v3"

TAX_SYSTEMS = {1: "ОСН", 2: "УСН доходы", 3: "УСН доходы минус расходы", 4: "ЕНВД", 5: "ЕСХН", 6: "Патент"}


class YooKassaError(RuntimeError):
    pass


def _money(value):
    return f"{float(value):.2f}"


def normalize_phone(text):
    digits = re.sub(r"\D", "", str(text or ""))
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return digits if len(digits) == 11 and digits[0] == "7" else ""


def receipt(items, contact, *, tax_system_code=None, vat_code=1, payment_subject="commodity",
            payment_mode="full_prepayment"):
    """Чек 54-ФЗ: items — [(название, количество, цена за штуку)]; contact — телефон или e-mail покупателя."""
    contact = str(contact or "").strip()
    customer = {"email": contact} if "@" in contact else {"phone": normalize_phone(contact)}
    if not any(customer.values()):
        raise YooKassaError("для чека нужен телефон или e-mail покупателя")
    out = {"customer": customer, "items": [{
        "description": str(name)[:128] or "Товар", "quantity": f"{int(qty)}",
        "amount": {"value": _money(price), "currency": "RUB"}, "vat_code": int(vat_code),
        "payment_subject": payment_subject, "payment_mode": payment_mode,
    } for name, qty, price in items]}
    if tax_system_code:
        out["tax_system_code"] = int(tax_system_code)
    return out


class YooKassaClient:
    def __init__(self, shop_id, secret_key, session=None, timeout=20):
        if not shop_id or not secret_key:
            raise YooKassaError("не заданы shopId и secretKey ЮKassa")
        self.auth = (str(shop_id).strip(), str(secret_key).strip())
        self.session = session or requests.Session()
        self.timeout = timeout

    def _call(self, method, path, body=None, idempotence_key=None):
        headers = {"Content-Type": "application/json"}
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key  # повтор запроса не создаст второй платёж
        try:
            response = self.session.request(method, API + path, json=body, auth=self.auth, headers=headers,
                                            timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise YooKassaError("нет связи с ЮKassa") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            raise YooKassaError(f"ЮKassa: {data.get('description') or data.get('code') or response.status_code}")
        return data

    def create_payment(self, amount, description, return_url, metadata=None, receipt_data=None, idempotence_key=None):
        body = {"amount": {"value": _money(amount), "currency": "RUB"}, "capture": True,
                "confirmation": {"type": "redirect", "return_url": return_url},
                "description": str(description)[:128], "metadata": metadata or {}}
        if receipt_data:
            body["receipt"] = receipt_data
        data = self._call("POST", "/payments", body, idempotence_key or uuid.uuid4().hex)
        return {"id": data.get("id"), "status": data.get("status"),
                "url": (data.get("confirmation") or {}).get("confirmation_url"),
                "amount": float((data.get("amount") or {}).get("value") or amount)}

    def get_payment(self, payment_id):
        data = self._call("GET", f"/payments/{payment_id}")
        return {"id": data.get("id"), "status": data.get("status"), "paid": bool(data.get("paid")),
                "amount": float((data.get("amount") or {}).get("value") or 0), "metadata": data.get("metadata") or {}}
