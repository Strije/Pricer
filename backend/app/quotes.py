"""Подбор для клиента: менеджер собирает позиции (что просил клиент) с вариантами из выдачи,
отправляет ссылку; клиент без входа выбирает вариант по каждой позиции или отказывается от неё;
выбранное становится заказом (перепроверка цен — как у любого заказа).

Документ подбора (Quote.data):
    {"lines": [{"id", "request", "qty", "variants": [{"key", "offer", "sale_price", "added_at"}],
                "choice": key | "skip" | None}],
     "client_comment": "", "contact": ""}
offer — снимок предложения движка (для заказа); клиенту уходит только public_variant.
"""
import copy
import datetime
import secrets
import uuid

from app import db

MAX_LINES = 50
MAX_VARIANTS = 8
DEFAULT_HOURS = 24  # сколько действуют цены подбора: дальше — перепроверка при заказе

STATUS_LABELS = {"draft": "черновик", "sent": "отправлен", "viewed": "открыт клиентом", "chosen": "клиент выбрал",
                 "ordered": "заказ оформлен", "cancelled": "отменён"}


class QuoteError(ValueError):
    pass


def new_token():
    return secrets.token_urlsafe(18)


def lines(quote):
    # Глубокая копия: SQLAlchemy сохраняет JSON, только если новое значение отличается от прежнего,
    # а правка вложенных словарей на месте меняла бы и «прежнее».
    return copy.deepcopy(list((quote.data or {}).get("lines") or []))


def _save(quote, data_lines, **extra):
    quote.data = {**(quote.data or {}), "lines": data_lines, **extra}


def find_line(data_lines, line_id):
    for line in data_lines:
        if line["id"] == line_id:
            return line
    raise QuoteError("позиция подбора не найдена")


def add_line(quote, request, qty=1, search=""):
    data_lines = lines(quote)
    if len(data_lines) >= MAX_LINES:
        raise QuoteError(f"в подборе не больше {MAX_LINES} позиций")
    line = {"id": uuid.uuid4().hex[:10], "request": str(request or "").strip()[:200] or "Позиция",
            "qty": max(1, int(qty or 1)), "variants": [], "choice": None, "search": search}
    data_lines.append(line)
    _save(quote, data_lines)
    return line


def update_line(quote, line_id, request=None, qty=None):
    data_lines = lines(quote)
    line = find_line(data_lines, line_id)
    if request is not None:
        line["request"] = str(request).strip()[:200] or line["request"]
    if qty is not None:
        line["qty"] = max(1, int(qty))
    _save(quote, data_lines)
    return line


def remove_line(quote, line_id):
    data_lines = [line for line in lines(quote) if line["id"] != line_id]
    _save(quote, data_lines)


def add_variant(quote, line_id, offer, sale_price):
    """Вариант из выдачи поиска; повторное добавление того же предложения ничего не меняет."""
    data_lines = lines(quote)
    line = find_line(data_lines, line_id)
    key = str(offer.get("internal_offer_id") or "")
    if not key:
        raise QuoteError("у предложения нет кода — повторите поиск")
    if any(v["key"] == key for v in line["variants"]):
        return line
    if len(line["variants"]) >= MAX_VARIANTS:
        raise QuoteError(f"в позиции не больше {MAX_VARIANTS} вариантов")
    from order_store import _json_safe

    line["variants"].append({"key": key, "offer": _json_safe(dict(offer)), "sale_price": round(float(sale_price or 0), 2),
                             "added_at": db.utcnow().isoformat(timespec="seconds")})
    _save(quote, data_lines)
    return line


def remove_variant(quote, line_id, key):
    data_lines = lines(quote)
    line = find_line(data_lines, line_id)
    line["variants"] = [v for v in line["variants"] if v["key"] != key]
    if line.get("choice") == key:
        line["choice"] = None
    _save(quote, data_lines)


def send(quote, hours=DEFAULT_HOURS):
    if not any(line["variants"] for line in lines(quote)):
        raise QuoteError("добавьте хотя бы один вариант")
    now = db.utcnow()
    quote.sent_at = now
    quote.expires_at = now + datetime.timedelta(hours=max(1, min(int(hours or DEFAULT_HOURS), 24 * 14)))
    if quote.status in ("draft", "cancelled"):
        quote.status = "sent"


def expired(quote):
    return bool(quote.expires_at and db.utcnow() > quote.expires_at)


def _variant_label(offer):
    return "аналог" if offer.get("is_cross") else "оригинальный номер"


def public_variant(variant, warranty_of):
    offer = variant.get("offer") or {}
    images = offer.get("image_urls") or []
    brand = offer.get("brand") or offer.get("display_brand") or ""
    return {
        "key": variant["key"], "brand": brand, "article": offer.get("article") or "", "name": offer.get("name") or "",
        "sale_price": variant.get("sale_price"), "delivery_hours": offer.get("delivery_hours"),
        "returnable": offer.get("returnable", True) is not False, "kind": _variant_label(offer),
        "image_url": (images[0] if isinstance(images, list) else str(images)) if images else "",
        "warranty": warranty_of(brand),
    }


def public_view(quote, org_name, warranty_of=lambda brand: None):
    """То, что видит клиент по ссылке: без закупки, поставщиков, складов и кодов предложений поставщика."""
    out_lines = []
    total = 0.0
    for line in lines(quote):
        variants = [public_variant(v, warranty_of) for v in line["variants"]]
        chosen = next((v for v in variants if v["key"] == line.get("choice")), None)
        if chosen:
            total += float(chosen["sale_price"] or 0) * int(line["qty"])
        out_lines.append({"id": line["id"], "request": line["request"], "qty": line["qty"],
                          "choice": line.get("choice"), "variants": variants})
    return {
        "title": quote.title, "organization": org_name, "client": quote.client_name, "status": quote.status,
        "status_label": STATUS_LABELS.get(quote.status, quote.status), "lines": out_lines,
        "expires_at": quote.expires_at.isoformat(timespec="minutes") if quote.expires_at else None,
        "expired": expired(quote), "total": round(total, 2),
        "client_comment": (quote.data or {}).get("client_comment", ""),
        "locked": quote.status in ("ordered", "cancelled"),
    }


def choose(quote, choices, comment="", contact=""):
    """Выбор клиента: {line_id: key | "skip"}; позиции без выбора остаются невыбранными."""
    if quote.status in ("ordered", "cancelled", "draft"):
        raise QuoteError("подбор уже закрыт" if quote.status != "draft" else "подбор ещё не отправлен")
    data_lines = lines(quote)
    for line in data_lines:
        value = (choices or {}).get(line["id"])
        if value is None:
            continue
        if value != "skip" and not any(v["key"] == value for v in line["variants"]):
            raise QuoteError("такого варианта в подборе нет — обновите страницу")
        line["choice"] = value
    if not any(line.get("choice") for line in data_lines):
        raise QuoteError("выберите вариант хотя бы для одной позиции")
    _save(quote, data_lines, client_comment=str(comment or "")[:1000], contact=str(contact or "")[:200])
    quote.status = "chosen"
    quote.chosen_at = db.utcnow()


def chosen_offers(quote):
    """[(line, offer, qty, alternatives)] — выбранные варианты для заказа (остальные варианты позиции —
    запасные: из них можно будет заменить позицию в заказе, как группы вариантов десктопа)."""
    out = []
    for line in lines(quote):
        key = line.get("choice")
        if not key or key == "skip":
            continue
        variant = next((v for v in line["variants"] if v["key"] == key), None)
        if variant:
            out.append((line, dict(variant["offer"]), int(line["qty"]), [dict(v["offer"]) for v in line["variants"]]))
    return out


def manager_view(quote):
    data_lines = lines(quote)
    return {
        "id": quote.id, "token": quote.token, "url": f"/q/{quote.token}", "title": quote.title,
        "client_id": quote.client_id, "vehicle_id": quote.vehicle_id, "client": quote.client_name,
        "status": quote.status, "status_label": STATUS_LABELS.get(quote.status, quote.status),
        "created_at": quote.created_at.isoformat(timespec="minutes") if quote.created_at else None,
        "sent_at": quote.sent_at.isoformat(timespec="minutes") if quote.sent_at else None,
        "expires_at": quote.expires_at.isoformat(timespec="minutes") if quote.expires_at else None,
        "viewed_at": quote.viewed_at.isoformat(timespec="minutes") if quote.viewed_at else None,
        "chosen_at": quote.chosen_at.isoformat(timespec="minutes") if quote.chosen_at else None,
        "expired": expired(quote), "order_id": quote.order_id,
        "client_comment": (quote.data or {}).get("client_comment", ""), "contact": (quote.data or {}).get("contact", ""),
        "payment": {k: ((quote.data or {}).get("payment") or {}).get(k) for k in ("status", "amount", "paid_at")}
        if (quote.data or {}).get("payment") else None,
        "lines": [{**{k: v for k, v in line.items() if k != "variants"},
                   "variants": [{"key": v["key"], "sale_price": v["sale_price"], "added_at": v.get("added_at"),
                                 **{f: (v.get("offer") or {}).get(f) for f in (
                                     "provider", "brand", "article", "name", "warehouse", "purchase_price",
                                     "delivery_hours", "returnable", "is_cross", "available_quantity")}}
                                for v in line["variants"]]} for line in data_lines],
        "positions": len(data_lines), "variants": sum(len(line["variants"]) for line in data_lines),
    }
