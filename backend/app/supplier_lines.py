"""Заказы поставщикам: каждая отправленная позиция, её текущий статус и история движений.

В десктопе этого не было: после отправки позиция жила только внутри заказа клиента. Здесь
каждая позиция, ушедшая поставщику, становится строкой журнала. Статус меняется:
- автоматически при отправке (принята, без подтверждения, не принята);
- запросом статусов у поставщика: Tradesoft (GetItemsStatus) и заказы из личных кабинетов
  остальных (integrations/supplier_orders.py, перенесено из 1С-обработки); у TISS источника нет;
- вручную оператором («пришло на склад», «отказ» и т.п.), пока у поставщика нет API статусов.

По истории считается надёжность поставщиков: доля отказов, позиций без подтверждения и
фактический срок до прихода на склад против обещанного.
"""
import datetime
import re

from app import db

# Код -> (подпись, завершённый ли статус)
STATUSES = {
    "submitted": ("отправлен поставщику", False),
    "unknown": ("нет подтверждения", False),
    "failed": ("не принят при отправке", True),
    "confirmed": ("подтверждён поставщиком", False),
    "in_transit": ("в пути", False),
    "arrived": ("пришёл на склад", False),
    "issued": ("выдан клиенту", True),
    "refused": ("отказ поставщика", True),
    "returned": ("возврат поставщику", True),
}
SUBMIT_TO_STATUS = {"submitted": "submitted", "unknown": "unknown", "failed": "failed"}

# Тексты статусов поставщиков -> наш код. Порядок важен: сначала более конкретные.
_KEYWORDS = [
    ("refused", ("отказ", "нет в налич", "снят", "аннул", "отмен", "не поставл", "недопостав")),
    ("returned", ("возврат", "возвращ")),
    ("issued", ("выдан", "получен клиент", "отгружен клиент")),
    # «ждет подтверждения» (Росско) — поставщик заказ видит, но ещё не подтвердил
    ("submitted", ("ждет подтвержд", "ждёт подтвержд", "ожидает подтвержд")),
    # «ожидаем поступление», «ожидаем товар на складе» (Росско) — ещё не пришло
    ("confirmed", ("ожидаем", "ожидается")),
    # «в пути на склад» — ещё в пути: явные признаки пути проверяем раньше «на склад»
    ("in_transit", ("в пути", "в доставке", "передан в доставку")),
    ("arrived", ("на склад", "пришл", "пришёл", "пришел", "поступ", "прибыл", "готов к выдаче", "к выдаче")),
    ("in_transit", ("отгружен", "отправлен")),
    ("in_transit", ("задерж",)),  # «Задерживается» (ABCP) — ещё едет
    ("confirmed", ("подтвержд", "принят", "в работе", "заказан", "обработ", "оформлен", "ожидает оплаты")),
]


def normalize_status(text):
    value = str(text or "").lower()
    for code, words in _KEYWORDS:
        if any(word in value for word in words):
            return code
    return ""


def _parse_dt(value):
    try:
        return datetime.datetime.fromisoformat(str(value or "")[:19])
    except ValueError:
        return None


def _supplier_ref(item):
    response = item.get("supplier_response") or {}
    for key in ("order_item_id", "order_number", "external_order_id", "order_id", "number"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def sync_order(session, organization_id, order):
    """Позиции заказа, ушедшие поставщику (или неудачно), -> строки журнала. Идемпотентно."""
    order_id = str(order.get("order_id") or "")
    client = order.get("client") or {}
    changed = 0
    for index, item in enumerate(order.get("items") or []):
        status = SUBMIT_TO_STATUS.get(str(item.get("submit_status") or ""))
        if not status:
            continue
        line = session.query(db.SupplierLine).filter_by(
            organization_id=organization_id, order_id=order_id, item_index=index).first()
        submitted_at = _parse_dt(item.get("submitted_at")) or db.utcnow()
        if line is None:
            hours = item.get("delivery_hours")
            try:
                hours = int(hours) if hours is not None else None
            except (TypeError, ValueError):
                hours = None
            if hours is not None and hours >= 999999 * 24:
                hours = None
            line = db.SupplierLine(
                organization_id=organization_id, order_id=order_id, item_index=index,
                provider=str(item.get("provider") or ""), brand=str(item.get("display_brand") or item.get("brand") or ""),
                article=str(item.get("article") or ""), name=str(item.get("name") or "")[:300],
                warehouse=str(item.get("warehouse") or "")[:120], quantity=int(item.get("quantity") or 0),
                purchase_price=float(item.get("purchase_price") or 0), client_name=str(client.get("name") or "")[:200],
                promised_hours=hours, submitted_at=submitted_at, status="", status_text="",
            )
            session.add(line)
            session.flush()
        ref = _supplier_ref(item)
        if ref and not line.supplier_ref:
            line.supplier_ref = ref
        response = item.get("supplier_response") or {}
        if response.get("position_id") and not line.position_id:
            line.position_id = str(response["position_id"])[:100]
        snapshot = item.get("snapshot") or {}
        code = snapshot.get("supplier_code") or item.get("supplier_code")
        if code and not line.supplier_code:
            line.supplier_code = str(code)[:100]
        # Статус от отправки меняем только пока поставщик не сообщил ничего дальше отправки.
        if line.status in ("", "submitted", "unknown", "failed") and line.status != status:
            response = item.get("supplier_response") or {}
            text = str(response.get("error") or response.get("message") or response.get("data") or "")[:500]
            add_event(session, line, status, text, source="submit", at=submitted_at)
            changed += 1
    return changed


def add_event(session, line, status, text="", source="manual", user_id=None, at=None):
    at = at or db.utcnow()
    session.add(db.SupplierLineEvent(line=line, at=at, status=status, text=str(text or "")[:500],
                                     source=source, user_id=user_id))
    line.status, line.status_text, line.status_at = status, str(text or "")[:500], at
    if status == "arrived" and not line.arrived_at:
        line.arrived_at = at
    # Закрыта ли позиция — по текущему статусу: оператор может вернуть её в работу.
    line.closed = STATUSES.get(status, ("", False))[1]
    return line


def line_view(line):
    label, final = STATUSES.get(line.status, (line.status, False))
    expected = (line.submitted_at + datetime.timedelta(hours=line.promised_hours)
                if line.submitted_at and line.promised_hours is not None else None)
    overdue = bool(expected and not line.arrived_at and not final and db.utcnow() > expected)
    return {
        "id": line.id, "order_id": line.order_id, "item_index": line.item_index, "provider": line.provider,
        "brand": line.brand, "article": line.article, "name": line.name, "warehouse": line.warehouse,
        "quantity": line.quantity, "purchase_price": line.purchase_price, "client": line.client_name,
        "supplier_ref": line.supplier_ref, "status": line.status, "status_label": label, "status_text": line.status_text,
        "final": final, "overdue": overdue,
        "submitted_at": line.submitted_at.isoformat(timespec="seconds") if line.submitted_at else None,
        "expected_at": expected.isoformat(timespec="seconds") if expected else None,
        "arrived_at": line.arrived_at.isoformat(timespec="seconds") if line.arrived_at else None,
        "status_at": line.status_at.isoformat(timespec="seconds") if line.status_at else None,
    }


def query(session, organization_id, *, provider="", brand="", status="", date_from=None, date_to=None, q="", limit=500):
    rows = session.query(db.SupplierLine).filter(db.SupplierLine.organization_id == organization_id)
    if provider:
        rows = rows.filter(db.SupplierLine.provider == provider)
    if brand:
        rows = rows.filter(db.SupplierLine.brand == brand)
    if status == "open":
        rows = rows.filter(db.SupplierLine.closed.is_(False))
    elif status:
        rows = rows.filter(db.SupplierLine.status == status)
    if date_from:
        rows = rows.filter(db.SupplierLine.submitted_at >= date_from)
    if date_to:
        rows = rows.filter(db.SupplierLine.submitted_at < date_to + datetime.timedelta(days=1))
    rows = rows.order_by(db.SupplierLine.submitted_at.desc(), db.SupplierLine.id.desc()).all()
    if q:
        key = re.sub(r"[^a-zа-я0-9]", "", q.lower())
        rows = [r for r in rows if key in re.sub(r"[^a-zа-я0-9]", "", f"{r.article}{r.name}{r.client_name}{r.order_id}{r.brand}".lower())]
    return rows[:limit]


def stats(session, organization_id, days=180):
    """Надёжность поставщиков по журналу: отказы, без подтверждения, фактический срок и опоздания."""
    since = db.utcnow() - datetime.timedelta(days=days)
    result = {}
    for line in session.query(db.SupplierLine).filter(db.SupplierLine.organization_id == organization_id,
                                                      db.SupplierLine.submitted_at >= since):
        row = result.setdefault(line.provider, {"provider": line.provider, "lines": 0, "refused": 0, "unknown": 0,
                                                "failed": 0, "arrived": 0, "late": 0, "_days": [], "_promised": []})
        row["lines"] += 1
        if line.status in ("refused", "returned"):
            row["refused"] += 1
        elif line.status == "unknown":
            row["unknown"] += 1
        elif line.status == "failed":
            row["failed"] += 1
        if line.arrived_at and line.submitted_at:
            actual = (line.arrived_at - line.submitted_at).total_seconds() / 86400
            row["arrived"] += 1
            row["_days"].append(actual)
            if line.promised_hours is not None:
                row["_promised"].append(line.promised_hours / 24)
                if actual > line.promised_hours / 24 + 0.5:
                    row["late"] += 1
    out = []
    for row in result.values():
        sent = max(1, row["lines"] - row["failed"])
        out.append({
            "provider": row["provider"], "lines": row["lines"], "arrived": row["arrived"],
            "refused_pct": round(100 * row["refused"] / sent, 1),
            "unknown_pct": round(100 * row["unknown"] / sent, 1),
            "failed_pct": round(100 * row["failed"] / max(1, row["lines"]), 1),
            "late_pct": round(100 * row["late"] / row["arrived"], 1) if row["arrived"] else None,
            "avg_days": round(sum(row["_days"]) / len(row["_days"]), 1) if row["_days"] else None,
            "avg_promised_days": round(sum(row["_promised"]) / len(row["_promised"]), 1) if row["_promised"] else None,
            "enough_data": row["lines"] >= 5,
        })
    return sorted(out, key=lambda r: r["provider"].lower())


def refresh_tradesoft(session, organization_id, provider, provider_name):
    """Статусы позиций Автоформулы через Tradesoft GetItemsStatus (по orderItemId из ответа на заказ)."""
    lines = session.query(db.SupplierLine).filter(
        db.SupplierLine.organization_id == organization_id, db.SupplierLine.provider == provider_name,
        db.SupplierLine.closed.is_(False), db.SupplierLine.supplier_ref != "").all()
    if not lines:
        return 0
    statuses = provider.get_items_status([line.supplier_ref for line in lines])
    changed = 0
    for line in lines:
        row = statuses.get(line.supplier_ref)
        if not row:
            continue
        text = str(row.get("stateName") or row.get("state") or "").strip()
        code = normalize_status(text)
        if code and (code != line.status or text != line.status_text):
            add_event(session, line, code, text, source="supplier")
            changed += 1
    return changed


def _key(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _article_key(value):
    """Артикул для сравнения: только буквы и цифры, без ведущих нулей (АртикулыСовпадают в 1С)."""
    return _key(value).lstrip("0") or _key(value)


def _brands_match(a, b):
    """Пустой бренд (Микадо его не отдаёт) не мешает; «MAHLE» и «MAHLE ORIGINAL» — один бренд."""
    a, b = _key(a), _key(b)
    return not a or not b or a == b or a.startswith(b) or b.startswith(a)


# Насколько далеко продвинулась позиция: статус поставщика не откатывает её назад
# (оператор отметил «пришло на склад», а поставщик всё ещё пишет «отгружено»).
_RANK = {"": 0, "submitted": 0, "unknown": 0, "failed": 0, "confirmed": 1, "in_transit": 2, "arrived": 3,
         "issued": 4, "refused": 4, "returned": 4}


def match_rows(lines, rows):
    """Строки поставщика -> позиции журнала: {line.id: (alive_rows, refused_rows)}.

    Как ВзятьУПоставщика в 1С: позиция забирает у строк поставщика нужное количество, частичный
    отказ виден отдельно. Ключи: positionId, номер заказа у поставщика (supplier_ref), метка
    нашего заказа в комментарии, supplierCode, затем бренд + артикул и близость даты к отправке.
    """
    pool = []
    for row in rows:
        quantity = float(row.get("quantity") or 0)
        pool.append({"row": row, "left": quantity if quantity > 0 else 1.0, "article": _article_key(row.get("article")),
                     "date": _parse_dt(row.get("date")), "order": str(row.get("order") or "").strip()})
    known_orders = {item["order"] for item in pool if item["order"]}
    result = {}
    # Сначала позиции с точными ключами: им достаются «свои» строки.
    ordered_lines = sorted(lines, key=lambda l: (not l.position_id, not l.supplier_ref, l.submitted_at or db.utcnow(), l.id))
    for line in ordered_lines:
        article = _article_key(line.article)
        ref = str(line.supplier_ref or "").strip()
        strict_ref = ref in known_orders  # номер заказа у поставщика узнан — чужие заказы не берём
        scored = []
        for item in pool:
            row = item["row"]
            if item["left"] <= 0 or item["article"] != article or not _brands_match(line.brand, row.get("brand")):
                continue
            if line.position_id and row.get("position_id") and str(row["position_id"]) != line.position_id:
                continue
            if line.supplier_code and row.get("supplier_code") and str(row["supplier_code"]) != line.supplier_code:
                continue
            if strict_ref and item["order"] != ref:
                continue
            if item["date"] and line.submitted_at and item["date"] < line.submitted_at - datetime.timedelta(days=1):
                continue  # заказ у поставщика раньше нашей отправки — не наш
            score = 0.0
            if line.position_id and str(row.get("position_id") or "") == line.position_id:
                score += 100
            if ref and item["order"] == ref:
                score += 50
            if line.order_id and line.order_id in str(row.get("comment") or ""):
                score += 40
            if line.supplier_code and str(row.get("supplier_code") or "") == line.supplier_code:
                score += 5
            if item["date"] and line.submitted_at:
                score -= min(abs((item["date"] - line.submitted_at).total_seconds()) / 86400, 30) / 10
            scored.append((score, item))
        if not scored:
            continue
        scored.sort(key=lambda pair: -pair[0])
        best_order = scored[0][1]["order"]
        need = float(line.quantity or 0) or 1.0
        alive, refused = [], []
        for _, item in scored:
            if item["order"] != best_order:
                continue
            take = min(item["left"], need)
            if take <= 0:
                break
            item["left"] -= take
            need -= take
            (refused if item["row"].get("refused") else alive).append((item["row"], take))
        result[line.id] = (alive, refused)
    return result


def _status_from(alive, refused):
    if not alive:
        texts = list(dict.fromkeys(str(row.get("status") or "").strip() for row, _ in refused))
        return "refused", "; ".join(t for t in texts if t) or "отказ поставщика"
    texts = list(dict.fromkeys(str(row.get("status") or "").strip() for row, _ in alive))
    text = "; ".join(t for t in texts if t)
    code = ""
    for candidate in texts:  # самый «продвинутый» из статусов живой части
        found = normalize_status(candidate)
        if found and _RANK.get(found, 0) >= _RANK.get(code, -1):
            code = found
    if refused:
        cancelled = sum(take for _, take in refused)
        text = (text + "; " if text else "") + f"отказ {cancelled:g} шт."
    # Поставщик позицию видит, но статус не узнан — значит, как минимум принята.
    return code or "confirmed", text or "есть у поставщика"


def apply_rows(session, organization_id, provider_name, rows):
    """Обновляет статусы открытых позиций поставщика по его строкам заказов. Возвращает число изменений."""
    lines = session.query(db.SupplierLine).filter(
        db.SupplierLine.organization_id == organization_id, db.SupplierLine.provider == provider_name,
        db.SupplierLine.closed.is_(False)).all()
    if not lines or not rows:
        return 0
    matched = match_rows(lines, rows)
    changed = 0
    for line in lines:
        if line.id not in matched:
            continue
        code, text = _status_from(*matched[line.id])
        if code == "refused" or _RANK.get(code, 0) >= _RANK.get(line.status, 0):
            if code != line.status or text[:500] != line.status_text:
                add_event(session, line, code, text, source="supplier")
                changed += 1
    return changed


def refresh_from_supplier(fetch_rows, days=60):
    """refresh-функция для поставщика со строками заказов (integrations/supplier_orders.py)."""
    def refresh(session, organization_id, provider, provider_name):
        has_open = session.query(db.SupplierLine.id).filter(
            db.SupplierLine.organization_id == organization_id, db.SupplierLine.provider == provider_name,
            db.SupplierLine.closed.is_(False)).first()
        if not has_open:
            return 0  # не дёргаем поставщика зря
        since = (db.utcnow() - datetime.timedelta(days=days)).date()
        return apply_rows(session, organization_id, provider_name, fetch_rows(provider, since))
    return refresh


def refresh_abcp(session, organization_id, provider, provider_name):
    """Статусы позиций поставщика на ABCP (orders): positionId и supplierCode десктопа, номер заказа,
    бренд и номер; частичная поставка (quantity меньше quantityOrdered) — частичный отказ."""
    from supplier_orders import abcp_orders

    return refresh_from_supplier(abcp_orders)(session, organization_id, provider, provider_name)


def fetcher_for(provider):
    """Функция запроса статусов для поставщика или None, если он статусы не отдаёт."""
    names = {cls.__name__ for cls in type(provider).__mro__}
    if "TradesoftProvider" in names:
        return refresh_tradesoft
    if "AbcpSupplierProvider" in names:
        return refresh_abcp
    import supplier_orders

    fetch_rows = supplier_orders.fetcher_for(provider)
    return refresh_from_supplier(fetch_rows) if fetch_rows else None
