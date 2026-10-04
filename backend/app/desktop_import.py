"""Перенос истории десктопа в веб: заказы (config/orders/ORD-*.json) и журнал отправок (order_history.json).

Нужно для месяца параллельной работы и отказа от десктопа. Из позиций, отправленных поставщикам,
веб строит «Заказы поставщикам» (supplier_lines.sync_order), а из них — статусы из кабинетов, надёжность
поставщиков и «популярное». Без импорта всё это начинается с нуля. Хуже того, статус из кабинета по
заказу десктопа веб мог бы приписать своей позиции той же детали: сопоставление идёт по бренду, артикулу
и дате. Поэтому в «Заказы поставщикам» попадают и отправленные десктопом позиции.

Формат заказа у десктопа и веба один (OrderStore), заказ кладётся как есть. Из ответов поставщиков
вычищаются секреты (Forum-Auto отдаёт адрес запроса с логином и паролем), в документ дописывается
source=desktop. Повторный импорт безопасен: тот же заказ обновляется, журнал не задваивается.
Десктоп и веб нумеруют заказы одинаково (ORD-ГГГГММДД-NNNN), поэтому номер, уже занятый заказом самого
веба, получает суффикс «-D».

Позиции «Заказов поставщикам» заводятся только для отправленного за последние LINE_WINDOW_DAYS дней —
за столько статусы из кабинетов и подтягиваются (supplier_lines.refresh_from_supplier). Более старые
позиции навсегда остались бы «отправлен» и засоряли бы список, поэтому старые заказы — только архив.
"""
import datetime
import io
import json
import re
import zipfile

from app import db
from app import supplier_lines as lines_service
from order_store import _json_safe

ORDER_ID = re.compile(r"^ORD-\d{8}-\d{4}$")
LINE_WINDOW_DAYS = 60
MAX_ENTRY = 60 * 1024 * 1024       # один JSON внутри архива
MAX_UNPACKED = 500 * 1024 * 1024   # весь архив в распакованном виде (защита от zip-бомбы)
MAX_FILES = 20000


class DesktopImportError(ValueError):
    """Файлы не подходят для импорта (понятная причина для пользователя)."""


def _parse_dt(value):
    try:
        return datetime.datetime.fromisoformat(str(value or "")[:19])
    except ValueError:
        return None


def read_files(files):
    """[(имя, байты)] — JSON заказов, order_history.json или ZIP с ними -> (заказы, строки журнала, пропущенные имена).
    Прочие файлы (settings.json, история поиска) пропускаются: настройки переносит свой импорт."""
    orders, journal, skipped = [], [], []
    unpacked = 0

    def handle(name, raw):
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            skipped.append(name)
            return
        if isinstance(data, dict) and ORDER_ID.match(str(data.get("order_id") or "")) and isinstance(data.get("items"), list):
            orders.append(data)
        elif isinstance(data, list) and any(isinstance(r, dict) and "submitted_at" in r and "provider" in r for r in data):
            journal.extend(r for r in data if isinstance(r, dict))
        else:
            skipped.append(name)

    for name, raw in files:
        lower = str(name or "").lower()
        if lower.endswith(".zip"):
            try:
                archive = zipfile.ZipFile(io.BytesIO(raw))
            except zipfile.BadZipFile:
                raise DesktopImportError(f"{name}: это не ZIP-архив")
            with archive:
                entries = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(".json")]
                if len(entries) > MAX_FILES:
                    raise DesktopImportError(f"{name}: слишком много файлов в архиве")
                for info in entries:
                    with archive.open(info) as file:
                        data = file.read(MAX_ENTRY + 1)  # заголовкам архива не верим: читаем с ограничением
                    unpacked += len(data)
                    if len(data) > MAX_ENTRY or unpacked > MAX_UNPACKED:
                        raise DesktopImportError(f"{name}: архив слишком большой в распакованном виде")
                    handle(info.filename, data)
        elif lower.endswith(".json"):
            handle(name, raw)
        else:
            skipped.append(name)
    return orders, journal, skipped


def _for_lines(order, since):
    """Копия заказа для supplier_lines.sync_order: позиции, отправленные раньше since, без статуса отправки
    (sync_order их пропустит). Номера позиций сохраняются — по ним строки журнала связаны с заказом."""
    items = []
    for item in order.get("items") or []:
        submitted = _parse_dt(item.get("submitted_at"))
        if submitted is None or submitted < since:
            item = {key: value for key, value in item.items() if key != "submit_status"}
        items.append(item)
    return {**order, "items": items}


def import_history(Session, organization_id, orders, journal, redactor, now=None):
    """Кладёт заказы и журнал десктопа в базу организации. Возвращает сводку для экрана."""
    now = now or db.utcnow()
    since = now - datetime.timedelta(days=LINE_WINDOW_DAYS)
    result = {"orders_new": 0, "orders_updated": 0, "orders_same": 0, "orders_renamed": 0,
              "journal_new": 0, "journal_same": 0, "lines": 0}
    renamed = {}
    with Session() as session:
        rows = {r.order_id: r for r in session.query(db.Order).filter_by(organization_id=organization_id)}
        for order in sorted(orders, key=lambda o: str(o.get("order_id"))):
            order = redactor.messages(_json_safe(order))
            order_id = str(order["order_id"])
            row = rows.get(order_id)
            if row is not None and row.source != "desktop":
                renamed[order_id] = order_id = f"{order_id}-D"  # номер занят заказом веба
                row = rows.get(order_id)
                result["orders_renamed"] += 1
            order = {**order, "order_id": order_id, "source": "desktop"}
            if row is None:
                row = db.Order(organization_id=organization_id, order_id=order_id, data=order, source="desktop",
                               status=str(order.get("status") or "draft"))
                session.add(row)
                rows[order_id] = row
                result["orders_new"] += 1
            elif row.data == order:
                result["orders_same"] += 1
            else:
                row.data, row.status = order, str(order.get("status") or "draft")
                result["orders_updated"] += 1
            session.flush()
            before = session.query(db.SupplierLine).filter_by(organization_id=organization_id, order_id=order_id).count()
            lines_service.sync_order(session, organization_id, _for_lines(order, since))
            session.flush()
            result["lines"] += session.query(db.SupplierLine).filter_by(
                organization_id=organization_id, order_id=order_id).count() - before

        seen = {(r.order_id, r.provider, r.brand, r.article, r.created_at)
                for r in session.query(db.SubmissionLog).filter_by(organization_id=organization_id)}
        for record in journal:
            record = redactor.messages(_json_safe(record))
            response = record.get("response") if isinstance(record.get("response"), dict) else {}
            order_id = str(response.get("order_id") or "")
            order_id = renamed.get(order_id, order_id)
            created = _parse_dt(record.get("submitted_at"))
            if created is None:
                continue
            key = (order_id[:40], str(record.get("provider") or "")[:100], str(record.get("brand") or "")[:100],
                   str(record.get("article") or "")[:100], created)
            if key in seen:
                result["journal_same"] += 1
                continue
            seen.add(key)
            try:
                quantity = int(record.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0
            session.add(db.SubmissionLog(organization_id=organization_id, order_id=key[0], provider=key[1],
                                         brand=key[2], article=key[3], quantity=quantity,
                                         success=bool(response.get("success")), response=record, created_at=created))
            result["journal_new"] += 1
        session.commit()
    return result
