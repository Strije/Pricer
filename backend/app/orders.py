"""Заказы в вебе: хранение в базе и действия, которые в десктопе запускались кнопками окна.

Логика заказа (состав, итоги, группы, перепроверка, отправка) — дословно из десктопа:
OrderStore (core/order_store.py) и методы движка (core/engine.py). Здесь только то, что
в десктопе делало окно: проверки перед действием (вместо диалогов — ошибки и предпросмотр),
хранение в базе вместо JSON-файлов и блокировка отправки в базе вместо памяти процесса.
"""
import datetime
import tempfile

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app import db
from app.redact import Redactor
from engine import PROCESSED_SUBMIT_STATUSES
from order_history_store import OrderHistoryStore
from order_store import OrderStore, _json_safe

INTERRUPTED_REASON = "сервер перезапускался во время отправки — проверьте у поставщика, ушла ли позиция"


class OrderActionError(Exception):
    """Действие с заказом нельзя выполнить (аналог предупреждений окна десктопа)."""


class DbOrderStore(OrderStore):
    """OrderStore десктопа, который хранит заказы организации в базе, а не в папке с JSON."""

    def __init__(self, sessionmaker, organization_id, user_id=None, redactor=None):
        self.redactor = redactor or Redactor()
        # Папка десктопу больше не нужна (заказы в базе), но create_draft её создаёт — даём временную.
        super().__init__(orders_dir=tempfile.gettempdir())
        self.Session = sessionmaker
        self.organization_id = organization_id
        self.user_id = user_id

    def _row(self, session, order_id):
        return session.query(db.Order).filter_by(organization_id=self.organization_id,
                                                 order_id=str(order_id or "")).first()

    def read(self, order_id):
        with self.Session() as session:
            row = self._row(session, order_id)
            if row is None:
                return None
            data = dict(row.data or {})
        self._ensure_totals(data)
        return data

    def _write(self, order):
        # Ответы поставщиков могут содержать логин и пароль в адресе запроса — в базу их не пишем.
        safe = self.redactor.messages(_json_safe(order))
        with self.Session() as session:
            row = self._row(session, safe["order_id"])
            if row is None:
                row = db.Order(organization_id=self.organization_id, order_id=safe["order_id"],
                               created_by=self.user_id)
                session.add(row)
            row.data = safe
            row.status = str(safe.get("status") or "draft")
            session.commit()

    def _next_order_id(self):
        prefix = f"ORD-{datetime.date.today():%Y%m%d}-"
        with self.Session() as session:
            ids = [r[0] for r in session.query(db.Order.order_id).filter(
                db.Order.organization_id == self.organization_id, db.Order.order_id.like(prefix + "%"))]
        numbers = [int(i[len(prefix):len(prefix) + 4]) for i in ids if i[len(prefix):len(prefix) + 4].isdigit()]
        return f"{prefix}{max(numbers, default=0) + 1:04d}"

    def create_draft(self, *args, **kwargs):
        # Номер уникален в базе; при гонке двух процессов повторяем с новым номером.
        for _attempt in range(5):
            try:
                return super().create_draft(*args, **kwargs)
            except IntegrityError:
                continue
        raise OrderActionError("не удалось выдать номер заказа, повторите")

    def list_orders(self):
        with self.Session() as session:
            rows = session.query(db.Order).filter_by(organization_id=self.organization_id).order_by(db.Order.id.desc())
            return [dict(r.data or {}) for r in rows]

    def list_order_summaries(self):
        return [self.summary_from_order(order) for order in self.list_orders()]


class DbOrderHistory(OrderHistoryStore):
    """Журнал отправок в базе (в десктопе — order_history.json)."""

    def __init__(self, sessionmaker, organization_id, redactor=None):
        self.Session = sessionmaker
        self.organization_id = organization_id
        self.redactor = redactor or Redactor()

    def append(self, item, quantity, response):
        record = self.redactor.messages(_json_safe(self._record(item or {}, quantity, response)))
        with self.Session() as session:
            session.add(db.SubmissionLog(
                organization_id=self.organization_id,
                order_id=str((response or {}).get("order_id") or ""),
                provider=record["provider"][:100], brand=record["brand"][:100], article=record["article"][:100],
                quantity=record["quantity"], success=bool((response or {}).get("success")),
                response=record,
            ))
            session.commit()
        return record

    def rows(self):
        with self.Session() as session:
            rows = session.query(db.SubmissionLog).filter_by(organization_id=self.organization_id)
            return [r.response for r in rows.order_by(db.SubmissionLog.id)]


# ---------- заказ из файла ----------

def prepare_entries(engine, results, row_indexes, selections=None):
    """Строки результата подбора -> позиции для черновика (как «Создать заказ» в десктопе).

    selections: {номер строки: internal_offer_id} — вариант, выбранный вместо предложенного.
    """
    selections = {int(k): str(v) for k, v in (selections or {}).items()}
    entries, errors = [], []
    for index in row_indexes:
        if not (0 <= index < len(results)) or results[index] is None:
            errors.append(f"строка {index + 1}: нет результата подбора")
            continue
        row = dict(results[index])
        chosen = selections.get(index)
        if chosen:
            variant = next((o for o in row.get("alternatives") or [] if str(o.get("internal_offer_id")) == chosen), None)
            if variant is None:
                errors.append(f"строка {index + 1}: выбранный вариант не найден")
                continue
            row["offer"] = variant
        if not row.get("offer"):
            errors.append(f"строка {index + 1}: {row.get('status') or 'нет предложения'}")
            continue
        entry, error = engine._prepared_order_file_entry(row)
        if entry:
            entries.append(entry)
        else:
            errors.append(f"строка {index + 1}: {error}")
    return engine._order_entries_with_prices(entries) if entries else [], errors


def create_order_from_file(engine, store, entries, *, manager, client, ship_date="", comment="", vin=""):
    if not entries:
        raise OrderActionError("нет строк, готовых к заказу")
    if not str(client or "").strip():
        raise OrderActionError("укажите клиента для заказа")
    return store.create_draft(
        manager=manager, client=client, ship_date=ship_date or store.calculated_ready_date(entries),
        entries=entries, comment=comment, client_vin=vin, grouping_mode="file",
        verified_at=engine._entries_verified_at(entries),
    )


# ---------- перепроверка и отправка ----------

def recheck(engine, store, order_id, item_indexes=None):
    """Как кнопка «Перепроверить»: проверки окна + перепроверка движком (синхронно)."""
    order = store.read(order_id)
    if not order:
        raise OrderActionError(f"заказ {order_id} не найден")
    if not order.get("items"):
        raise OrderActionError(f"заказ {order_id} пустой")
    if item_indexes is not None and not list(item_indexes):
        raise OrderActionError("отметьте позиции для перепроверки")
    order["verification_status"] = "checking"
    store.update(order)
    engine._recheck_order_thread(order_id, item_indexes)
    return store.read(order_id)


def submit_preview(engine, store, order_id, item_indexes=None):
    """То, что десктоп показывал в диалоге подтверждения перед отправкой."""
    order = store.read(order_id)
    if not order:
        raise OrderActionError(f"заказ {order_id} не найден")
    if order.get("status") == "sent":
        raise OrderActionError("этот заказ уже отправлен")
    if item_indexes is not None and not list(item_indexes):
        raise OrderActionError("отметьте позиции для отправки")
    order = engine._mark_order_stale_if_expired(order, update_store=True)
    if order.get("verification_status") == "stale":
        raise OrderActionError("данные устарели, выполните перепроверку перед отправкой")
    pending = engine._submittable_order_items(order, item_indexes)
    if not pending:
        if order.get("verification_status") == "checking":
            raise OrderActionError("дождитесь завершения проверки")
        raise OrderActionError("нет проверенных позиций для отправки — запустите перепроверку")
    selected = set(int(i) for i in item_indexes) if item_indexes is not None else None
    all_pending = [item for index, item in enumerate(order.get("items") or [])
                   if (selected is None or index in selected) and item.get("submit_status") not in PROCESSED_SUBMIT_STATUSES]
    return {
        "items": len(pending),
        "quantity": sum(int(item.get("quantity") or 0) for item in pending),
        "providers": sorted({str(item.get("provider") or "без поставщика") for item in pending}),
        "problems": max(0, len(all_pending) - len(pending)),
        "purchase_total": round(sum(float(item.get("purchase_total") or 0) for item in pending), 2),
    }


def _lock(sessionmaker, organization_id, order_id, submit_key, targets):
    with sessionmaker() as session:
        result = session.execute(
            update(db.Order)
            .where(db.Order.organization_id == organization_id, db.Order.order_id == order_id,
                   db.Order.submitting.is_(False))
            .values(submitting=True, submit_key=submit_key, submit_targets=targets,
                    submit_started_at=db.utcnow())
        )
        session.commit()
        return result.rowcount == 1


def _unlock(sessionmaker, organization_id, order_id):
    with sessionmaker() as session:
        session.execute(update(db.Order).where(db.Order.organization_id == organization_id,
                                                db.Order.order_id == order_id).values(submitting=False))
        session.commit()


def submit(engine, store, sessionmaker, order_id, item_indexes=None):
    """Отправка поставщикам (синхронно). Повторный запуск во время отправки отклоняется базой."""
    preview = submit_preview(engine, store, order_id, item_indexes)
    order = store.read(order_id)
    targets = sorted(int(i) for i in item_indexes) if item_indexes is not None else list(range(len(order.get("items") or [])))
    submit_key = engine._order_submit_idempotency_key(order_id, item_indexes)
    if not _lock(sessionmaker, store.organization_id, order_id, submit_key, targets):
        raise OrderActionError("отправка этого заказа уже выполняется")
    try:
        engine._submit_order_thread_guarded(order_id, item_indexes, submit_key)
    finally:
        _unlock(sessionmaker, store.organization_id, order_id)
    return preview, store.read(order_id)


def recover_interrupted_submits(sessionmaker):
    """После перезапуска: позиции, которые уходили в момент сбоя, получают статус unknown.

    Повторно отправлять их автоматически нельзя (заказ мог уйти поставщику) — решает оператор,
    как и для unknown после обрыва связи в десктопе.
    """
    recovered = []
    with sessionmaker() as session:
        for row in session.query(db.Order).filter_by(submitting=True):
            data = dict(row.data or {})
            items = list(data.get("items") or [])
            for index in row.submit_targets or []:
                if 0 <= index < len(items) and items[index].get("submit_status") not in PROCESSED_SUBMIT_STATUSES:
                    items[index] = {**items[index], "submit_status": "unknown",
                                    "submit_unknown_reason": INTERRUPTED_REASON}
            data["items"] = items
            row.data = _json_safe(data)
            row.submitting = False
            recovered.append((row.organization_id, row.order_id))
        session.commit()
    return recovered
