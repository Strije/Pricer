"""Запись сырых ответов поставщиков для тестов веб-версии.

Положите файл в папку программы (рядом с main.py) и запустите из неё:

    .venv\\Scripts\\python.exe record_provider_responses.py ZIC:162622 MANN:W712/95

Скрипт выполняет тот же поиск, что кнопка «Файл заказа» (движок main.py, ваши
настройки), и пишет в JSONL каждый HTTP-запрос и ответ поставщиков, а в конце
итоговый результат подбора по каждой строке. Заказы НЕ оформляются: методы
корзины у поставщиков на время записи заблокированы.

Секреты маскируются: значения ключей, паролей, логинов, телефонов и токенов из
settings.json заменяются на ***, заголовки Authorization и Cookie не пишутся.
После записи файл проверяется ещё раз, и если в нём нашёлся секрет, он удаляется.
"""
import argparse
import base64
import datetime
import json
import os
import re
import sys
import threading
from urllib.parse import quote, quote_plus

SECRET_KEY_RE = re.compile(r"(key|pass|login|user|token|secret|phone|client_id|auth)", re.I)
HIDDEN_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
MAX_BODY_BYTES = 2 * 1024 * 1024
MASK = "***"


def collect_secrets(settings):
    """Все строковые значения секретных полей, плюс логин и пароль из ftp://user:pass@ адресов."""
    found = set()

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str):
            if SECRET_KEY_RE.search(key) and len(node.strip()) >= 3:
                found.add(node.strip())
            for user, password in re.findall(r"//([^:/@\s]+):([^@/\s]+)@", node):
                found.update({user, password})

    walk(settings)
    # В адресах и формах значения бывают URL-кодированы: маскируем и такие варианты.
    for value in list(found):
        found.update({quote(value, safe=""), quote_plus(value)})
    # Basic-авторизация (Armtek и др.) кодирует логин и пароль в base64 — маскируем и такие строки.
    plain = [s for s in found if "%" not in s]
    for user in plain:
        for password in plain:
            if user != password:
                found.add(base64.b64encode(f"{user}:{password}".encode()).decode())
    return sorted(found, key=len, reverse=True)


def mask(text, secrets):
    if not text:
        return text
    for secret in secrets:
        text = text.replace(secret, MASK)
    return text


def _decode(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    for encoding in ("utf-8", "cp1251"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


class Recorder:
    def __init__(self, path, secrets):
        self.path = path
        self.secrets = secrets
        self.lock = threading.Lock()
        self.seq = 0
        self.file = open(path, "w", encoding="utf-8")

    def write(self, record):
        text = mask(json.dumps(record, ensure_ascii=False, default=str), self.secrets)
        try:
            record = json.loads(text)
        except ValueError:
            record = {"type": "raw", "text": text}
        with self.lock:
            self.seq += 1
            record["seq"] = self.seq
            self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.file.flush()

    def close(self):
        self.file.close()

    def http(self, request, response, elapsed, error=""):
        body = request.body
        if isinstance(body, bytes):
            body = _decode(body)
        record = {
            "type": "http",
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "thread": threading.current_thread().name,
            "method": request.method,
            "url": request.url,
            "request_headers": {k: v for k, v in request.headers.items() if k.lower() not in HIDDEN_HEADERS},
            "request_body": body or "",
            "elapsed_seconds": round(elapsed, 3),
        }
        if error:
            record["error"] = error
        if response is not None:
            content = response.content or b""
            record["status_code"] = response.status_code
            record["response_headers"] = {
                k: v for k, v in response.headers.items() if k.lower() not in HIDDEN_HEADERS
            }
            if len(content) > MAX_BODY_BYTES:
                record["response_body"] = ""
                record["response_truncated_bytes"] = len(content)
            else:
                record["response_body"] = _decode(content)
        self.write(record)


def install_http_recorder(recorder):
    """Перехватывает все запросы requests (zeep для SOAP тоже ходит через requests)."""
    import time

    from requests.adapters import HTTPAdapter

    original_send = HTTPAdapter.send

    def send(self, request, *args, **kwargs):
        started = time.time()
        try:
            response = original_send(self, request, *args, **kwargs)
        except Exception as exc:
            recorder.http(request, None, time.time() - started, error=f"{type(exc).__name__}: {exc}")
            raise
        recorder.http(request, response, time.time() - started)
        return response

    HTTPAdapter.send = send
    return original_send


def block_ordering(providers):
    def refuse(*_args, **_kwargs):
        raise RuntimeError("запись ответов: оформление заказа заблокировано")

    for provider in providers:
        for name in ("add_to_basket", "add_to_basket_batch", "create_order", "submit_order"):
            if hasattr(provider, name):
                setattr(provider, name, refuse)


def verify_no_secrets(path, secrets):
    with open(path, encoding="utf-8") as file:
        text = file.read()
    return [s[:2] + "…" for s in secrets if s in text]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Запись сырых ответов поставщиков")
    parser.add_argument("items", nargs="+", help="БРЕНД:АРТИКУЛ, например ZIC:162622")
    parser.add_argument("-o", "--output", default="", help="файл JSONL (по умолчанию recordings-ДАТА.jsonl)")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--with-analogs", action="store_true", help="искать с аналогами")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)

    from config_path import get_settings_path

    with open(get_settings_path(), encoding="utf-8") as file:
        secrets = collect_secrets(json.load(file))

    output = args.output or os.path.join(
        here, f"recordings-{datetime.datetime.now():%Y%m%d-%H%M%S}.jsonl"
    )
    recorder = Recorder(output, secrets)
    install_http_recorder(recorder)

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)  # noqa: F841 - Qt-объектам нужен живой QApplication
    import main as pp

    window = pp.SkitchenApp()
    window._order_file_search_id = 1
    if not window.providers:
        print("нет включённых поставщиков — проверьте настройки программы")
        return 1
    block_ordering(window.providers)
    options = {
        "exact_match": not args.with_analogs,
        "include_no_return": False,
        "ignore_warehouse_filters": False,
        "strategy": "price",
        "max_days": None,
    }
    print(f"поставщиков: {len(window.providers)}, запись в {output}")

    for item in args.items:
        brand, _, article = item.partition(":")
        if not article:
            print(f"пропуск «{item}»: нужен формат БРЕНД:АРТИКУЛ")
            continue
        row = {"brand": brand, "article": article, "name": "", "quantity": args.quantity}
        recorder.write({"type": "search_start", "row": row, "options": options})
        try:
            result = window._search_order_file_row(row, options, search_id=1)
        except Exception as exc:
            result = {"status": f"Ошибка: {exc}"}
        recorder.write({"type": "search_result", "row": row, "result": result})
        offer = (result or {}).get("offer") or {}
        print(f"{brand} {article}: {result.get('status')} {offer.get('provider', '')} {offer.get('price', '')}")

    recorder.close()
    leaked = verify_no_secrets(output, secrets)
    if leaked:
        os.remove(output)
        print(f"В записи нашлись секреты ({len(leaked)} шт.), файл удалён. Сообщите об этом.")
        return 2
    print(f"Готово: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
