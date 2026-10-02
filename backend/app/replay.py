"""Проигрывание записанных ответов поставщиков (tools/record_provider_responses.py).

Живой запрос адаптера маскируется так же, как при записи, и сопоставляется с записанным:
сначала точно (метод, адрес, параметры, тело), затем по порядку среди запросов на тот же
адрес. Сеть не используется: незнакомый запрос даёт ошибку.
"""
import datetime as _dt
import gzip
import importlib.util
import json
import os
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import HTTPAdapter

__all__ = ["DUMMY", "RECORDED_AT", "RECORDINGS", "FrozenDateTime", "Replayer", "frozen_datetime_module", "install", "load", "record_tool"]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RECORDINGS = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "recordings")

_spec = importlib.util.spec_from_file_location(
    "record_tool", os.path.join(ROOT, "tools", "record_provider_responses.py")
)
record_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record_tool)

# Фиктивные учётные данные: в запросах адаптеров они заменяются на *** как при записи.
DUMMY = "REPLAYSECRET"
DUMMY_SETTINGS = {"login": DUMMY, "password": DUMMY + "PW", "api_key": DUMMY + "KEY"}
SECRETS = record_tool.collect_secrets(DUMMY_SETTINGS)

RECORDED_AT = _dt.datetime(2026, 10, 2, 15, 4, 58)


def load(name="search-2026-10-02.jsonl.gz"):
    path = name if os.path.isabs(name) else os.path.join(RECORDINGS, name)
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def _mask(text):
    return record_tool.mask(record_tool.mask_params(text or ""), SECRETS)


def _key(method, url, body):
    parts = urlsplit(url)
    query = tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return method.upper(), parts.netloc.lower(), parts.path, query, (body or "").strip()


def _route(method, url):
    parts = urlsplit(url)
    return method.upper(), parts.netloc.lower(), parts.path


class Replayer:
    def __init__(self, records, reuse=False):
        # reuse=True — для демонстрации: перед каждым поиском вызывается reset(), и запись
        # проигрывается заново, так что один и тот же артикул можно искать много раз.
        self.reuse = reuse
        self.http = [r for r in records if r.get("type") == "http"]
        self.used = set()
        self.fallbacks = []
        self.unmatched = []

    def reset(self):
        self.used.clear()

    def find(self, method, url, body):
        url, body = _mask(url), _mask(body)
        wanted = _key(method, url, body)
        candidates = [i for i, r in enumerate(self.http) if i not in self.used]
        for i in candidates:
            r = self.http[i]
            if _key(r["method"], r["url"], r.get("request_body")) == wanted:
                self.used.add(i)
                return r
        route = _route(method, url)
        for i in candidates:
            r = self.http[i]
            if _route(r["method"], r["url"]) == route:
                self.used.add(i)
                self.fallbacks.append((method, url))
                return r
        self.unmatched.append((method, url))
        return None

    def send(self, adapter, request, *args, **kwargs):
        body = request.body
        if isinstance(body, bytes):
            body = record_tool._decode(body)
        record = self.find(request.method, request.url, body)
        if record is None:
            raise requests.exceptions.ConnectionError(f"нет записи для {request.method} {request.url}")
        response = requests.Response()
        response.status_code = int(record.get("status_code") or 200)
        response.headers.update(record.get("response_headers") or {})
        response.headers.pop("Content-Encoding", None)  # тело записано уже распакованным
        response.headers.pop("Transfer-Encoding", None)
        charset = "utf-8"
        content_type = response.headers.get("Content-Type", "")
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=")[1].split(";")[0].strip() or "utf-8"
        try:
            response._content = (record.get("response_body") or "").encode(charset, errors="replace")
        except LookupError:
            charset = "utf-8"
            response._content = (record.get("response_body") or "").encode(charset)
        response.encoding = charset
        response.url = request.url
        response.request = request
        return response


class FrozenDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return RECORDED_AT if tz is None else RECORDED_AT.replace(tzinfo=tz)


def frozen_datetime_module():
    """Копия модуля datetime, где datetime.now() возвращает время записи.

    Подставляется в модули, которые делают `import datetime` (Armtek): менять сам datetime.datetime
    нельзя — подмена расползается на всю программу (на ней падало чтение Excel в openpyxl).
    """
    import types

    module = types.ModuleType("datetime")
    module.__dict__.update({k: v for k, v in vars(_dt).items() if not k.startswith("__")})
    module.datetime = FrozenDateTime
    return module


def install(monkeypatch, records, reuse=False):
    replayer = Replayer(records, reuse=reuse)
    monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, *a, **k: replayer.send(self, request, *a, **k))
    return replayer
