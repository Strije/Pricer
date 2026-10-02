"""Прайсы из почты (IMAP): самое новое письмо по правилу источника и его вложение-прайс.

Правило: папка, часть адреса отправителя, часть темы, часть имени файла вложения, срок давности
письма. Ящик — например Яндекс (imap.yandex.ru:993, пароль приложения). Ящик только читается:
письма не удаляются и не помечаются прочитанными (BODY.PEEK, папка — только для чтения).
"""
import datetime
import email
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

PRICE_EXT = (".csv", ".txt", ".xlsx", ".xls", ".zip", ".7z", ".gz", ".rar")
MAX_SCAN = 60  # писем с конца, которые просматриваем


class MailError(RuntimeError):
    pass


def _text(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _quote(folder):
    return '"' + str(folder or "INBOX").replace("\\", "\\\\").replace('"', '\\"') + '"'


def connect(mailbox, timeout=30):
    host = mailbox.get("host") or "imap.yandex.ru"
    port = int(mailbox.get("port") or 993)
    try:
        imap = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        imap.login(mailbox.get("login") or "", mailbox.get("password") or "")
    except imaplib.IMAP4.error as exc:
        raise MailError(f"почта: вход не выполнен ({exc}). Для Яндекса нужен пароль приложения и включённый IMAP") from exc
    except OSError as exc:
        raise MailError(f"почта: нет связи с {host}:{port} ({type(exc).__name__})") from exc
    return imap


def folders(mailbox):
    imap = connect(mailbox)
    try:
        typ, data = imap.list()
        out = []
        for line in data or []:
            match = re.search(rb'"([^"]*)"\s*$|(\S+)\s*$', line or b"")
            if match:
                out.append((match.group(1) or match.group(2)).decode("utf-8", "replace"))
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _attachments(message):
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        name = _text(filename)
        if name.lower().endswith(PRICE_EXT):
            payload = part.get_payload(decode=True)
            if payload:
                yield name, payload


def _headers(imap, ids):
    """Заголовки писем одним запросом (без тел и вложений): {номер: письмо только с заголовками}."""
    typ, parts = imap.fetch(b",".join(ids), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
    out = {}
    for part in parts or []:
        if isinstance(part, tuple) and part[0]:
            out[part[0].split()[0]] = email.message_from_bytes(part[1] or b"")
    return out


def find_latest(mailbox, rule, now=None, skip_message_id=None):
    """(вложение, имя файла, Message-ID, дата) самого нового подходящего письма или MailError.

    Сначала читаются только заголовки (быстро и без памяти), письмо целиком — лишь подходящее.
    Если самое новое подходящее письмо — skip_message_id (уже загружено), возвращается (None, None, id, дата).
    """
    rule = rule or {}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    days = max(1, int(rule.get("max_age_days") or 7))
    since = (now - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
    sender = str(rule.get("sender") or "").strip().lower()
    subject = str(rule.get("subject") or "").strip().lower()
    filename = str(rule.get("filename") or "").strip().lower()
    imap = connect(mailbox)
    try:
        typ, _ = imap.select(_quote(rule.get("folder") or "INBOX"), readonly=True)
        if typ != "OK":
            raise MailError(f"почта: папки «{rule.get('folder') or 'INBOX'}» нет")
        typ, data = imap.search(None, "SINCE", since)
        ids = (data[0] or b"").split()[-MAX_SCAN:] if typ == "OK" and data else []
        headers = _headers(imap, ids) if ids else {}
        for msg_id in reversed(ids):  # с самого нового
            head = headers.get(msg_id)
            if head is None:
                continue
            if sender and sender not in _text(head.get("From")).lower():
                continue
            if subject and subject not in _text(head.get("Subject")).lower():
                continue
            message_id = str(head.get("Message-ID") or msg_id.decode()).strip()
            try:
                sent = parsedate_to_datetime(head.get("Date")).isoformat(timespec="minutes")
            except Exception:
                sent = ""
            if skip_message_id and message_id == skip_message_id:
                return None, None, message_id, sent
            typ, parts = imap.fetch(msg_id, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if not raw:
                continue
            for name, payload in _attachments(email.message_from_bytes(raw)):
                if filename and filename not in name.lower():
                    continue
                return payload, name, message_id, sent
        raise MailError(f"почта: за {days} дн. нет письма по правилу"
                        + "".join(f" · {k}: «{v}»" for k, v in (("от", sender), ("тема", subject), ("файл", filename)) if v))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
