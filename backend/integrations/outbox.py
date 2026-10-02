"""Очередь исходящих событий (outbox) для выгрузки в 1С.

Модуль описывает только жизненный цикл события, без БД: хранилище (таблица
integration_outbox) подключается снаружи. События создаются в той же транзакции, что и заказ,
поэтому недоступность 1С никогда не блокирует оформление заказа поставщику.

Статусы: pending -> sent (подтверждена 1С) | failed (ошибка данных, нужен человек).
Временные ошибки (1С недоступна) оставляют событие в pending с увеличенной паузой.
"""
import datetime
from dataclasses import dataclass, field

from onec_odata import OneCError, OneCUnavailable

MAX_ATTEMPTS = 8
BASE_DELAY_SECONDS = 30
MAX_DELAY_SECONDS = 3600


@dataclass
class OutboxEvent:
    kind: str  # client_order | supplier_order
    entity: str
    external_id: str
    payload: dict
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    last_error: str = ""
    onec_ref: str = ""


def is_due(event, now=None):
    now = now or datetime.datetime.now()
    return event.status == "pending" and event.next_attempt_at <= now


def deliver(event, client, now=None):
    """Одна попытка доставки. Меняет и возвращает событие."""
    now = now or datetime.datetime.now()
    event.attempts += 1
    try:
        document, _created = client.create_document(event.entity, event.payload, event.external_id)
    except OneCUnavailable as exc:
        event.last_error = f"1С недоступна: {exc}"
        if event.attempts >= MAX_ATTEMPTS:
            event.status = "failed"
        else:
            delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (event.attempts - 1))
            event.next_attempt_at = now + datetime.timedelta(seconds=delay)
        return event
    except OneCError as exc:
        event.status = "failed"
        event.last_error = str(exc)
        return event
    event.status = "sent"
    event.last_error = ""
    event.onec_ref = str((document or {}).get("Ref_Key") or (document or {}).get("Number") or "")
    return event
