import datetime
import json
import os
import re
import threading

from config_path import get_config_dir


class DetailedLogger:
    SECRET_KEYS = {
        "api_key",
        "developer_key",
        "password",
        "pass",
        "secret",
        "token",
        "auth",
        "key1",
        "key2",
        "userpsw",
        "userpassword",
        "provider_password",
    }

    QUERY_SECRET_PATTERN = re.compile(
        r"(?i)([?&](?:pass|password|api_key|apikey|secret|token|auth|key1|key2|userpsw)=)([^&#\s]+)"
    )
    JSON_SECRET_PATTERN = re.compile(
        r'(?i)("?(?:pass|password|api_key|apikey|secret|token|auth|key1|key2|userpsw)"?\s*[:=]\s*)("[^"]*"|[^,\s}]+)'
    )
    AUTH_HEADER_PATTERN = re.compile(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+")

    ERROR_PREFIX = "procenka-errors-"
    DETAIL_PREFIX = "procenka-details-"

    # Тяжёлые срезы предложений в поток ошибок не идут: они нужны при разборе
    # конкретной строки, а поток ошибок должен оставаться маленьким и жить долго.
    ERROR_STREAM_SKIP_KEYS = {
        "provider_stats",
        "filter_samples",
        "offers_sample",
        "selectable_sample",
        "items",
        "selected_item_indexes",
    }

    ERROR_STATUSES = {"failed", "partial", "error", "not_found", "unavailable", "cancelled"}
    ERROR_EVENT_MARKERS = ("error", "fail", "cancel", "blocked", "not_ready")
    ERROR_STEP_MARKERS = ("timeout", "error", "exception", "empty", "no_part_id", "no_search_id")

    FILE_DATE_PATTERN = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

    def __init__(self, log_dir=None, max_days=90, max_bytes=5 * 1024 * 1024, max_files=None):
        self.log_dir = log_dir or os.path.join(get_config_dir(), "logs")
        # Срок хранения считаем днями, а не числом файлов: один прогон поиска
        # выбивает несколько ротаций, и лимит по файлам съедал историю за сутки.
        self.max_days = max(1, int(max_days or 90))
        self.max_bytes = max(256 * 1024, int(max_bytes or 0))
        self.max_files = int(max_files) if max_files else None
        self._secrets = set()
        self._lock = threading.Lock()
        os.makedirs(self.log_dir, exist_ok=True)

    def set_secrets_from_settings(self, settings):
        secrets = set()
        self._collect_secrets(settings, secrets)
        with self._lock:
            self._secrets = {value for value in secrets if len(value) >= 4}

    def log(self, event, provider="", message="", **fields):
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "event": str(event or ""),
            "provider": str(provider or ""),
            "message": str(message or ""),
        }
        record.update(fields)
        safe_record = self._compact(self._redact(record))
        line = json.dumps(safe_record, ensure_ascii=False, default=str)
        is_error = self._is_error_record(safe_record)
        with self._lock:
            os.makedirs(self.log_dir, exist_ok=True)
            self._rotate_current_log_if_needed()
            with open(self.current_path(), "a", encoding="utf-8") as file:
                file.write(line + "\n")
            if is_error:
                self._write_error_line(safe_record)
            self._cleanup_old_logs()

    def current_path(self):
        day = datetime.date.today().isoformat()
        return os.path.join(self.log_dir, f"{self.DETAIL_PREFIX}{day}.jsonl")

    def errors_path(self):
        """Отдельный поток только для сбоев.

        Лежит рядом с подробным логом, но не ротируется по объёму: подробный
        лог за сутки способен вытеснить сам себя, а причины ошибок должны
        оставаться под рукой весь срок хранения.
        """
        day = datetime.date.today().isoformat()
        return os.path.join(self.log_dir, f"{self.ERROR_PREFIX}{day}.jsonl")

    def _write_error_line(self, record):
        compact = {
            key: value
            for key, value in record.items()
            if key not in self.ERROR_STREAM_SKIP_KEYS
        }
        try:
            with open(self.errors_path(), "a", encoding="utf-8") as file:
                file.write(json.dumps(compact, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _is_error_record(self, record):
        if str(record.get("level") or "").lower() == "error":
            return True
        if record.get("success") is False:
            return True
        if str(record.get("status") or "").lower() in self.ERROR_STATUSES:
            return True
        event = str(record.get("event") or "").lower()
        if any(marker in event for marker in self.ERROR_EVENT_MARKERS):
            return True
        if event == "provider_debug":
            step = str(record.get("step") or "").lower()
            if any(marker in step for marker in self.ERROR_STEP_MARKERS):
                return True
        if str(record.get("error") or "").strip():
            return True
        response = record.get("response")
        if isinstance(response, dict):
            if response.get("success") is False or str(response.get("error") or "").strip():
                return True
        return False

    def _compact(self, value):
        """Выбрасывает пустые поля.

        Отсутствие ключа читается так же, как null, поэтому диагностика не
        страдает. False и 0 остаются: они несут смысл (success, cache_hit,
        raw_count) и молча превратились бы в «нет данных».
        """
        if isinstance(value, dict):
            result = {}
            for key, nested in value.items():
                nested = self._compact(nested)
                if nested is None or nested == "":
                    continue
                if isinstance(nested, (list, dict, tuple)) and not nested:
                    continue
                result[key] = nested
            return result
        if isinstance(value, list):
            return [self._compact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._compact(item) for item in value)
        return value

    def _rotate_current_log_if_needed(self):
        path = self.current_path()
        try:
            if not os.path.exists(path) or os.path.getsize(path) < self.max_bytes:
                return
            base, ext = os.path.splitext(path)
            index = 1
            while True:
                rotated_path = f"{base}-{index:03d}{ext}"
                if not os.path.exists(rotated_path):
                    os.replace(path, rotated_path)
                    return
                index += 1
        except Exception:
            pass

    def _collect_secrets(self, value, secrets, current_key=""):
        if isinstance(value, dict):
            for key, nested in value.items():
                self._collect_secrets(nested, secrets, str(key or ""))
            return
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                self._collect_secrets(nested, secrets, current_key)
            return
        if current_key.lower() not in self.SECRET_KEYS:
            return
        text = str(value or "").strip()
        if text:
            secrets.add(text)

    def _redact(self, value):
        if isinstance(value, dict):
            result = {}
            for key, nested in value.items():
                if str(key).lower() in self.SECRET_KEYS:
                    result[key] = "***" if str(nested or "") else ""
                else:
                    result[key] = self._redact(nested)
            return result
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact(item) for item in value)
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def _redact_text(self, text):
        result = str(text or "")
        with self._lock:
            secrets = list(self._secrets)
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                result = result.replace(secret, "***")
        result = self.QUERY_SECRET_PATTERN.sub(lambda m: m.group(1) + "***", result)
        result = self.JSON_SECRET_PATTERN.sub(lambda m: m.group(1) + "***", result)
        result = self.AUTH_HEADER_PATTERN.sub(lambda m: m.group(1) + " ***", result)
        return result

    def _cleanup_old_logs(self):
        """Чистит по дате в имени файла, а не по их количеству.

        Один прогон поиска даёт несколько ротаций за сутки, поэтому старый
        лимит в 14 файлов оставлял историю всего за три-пять дней.
        """
        try:
            names = [
                name
                for name in os.listdir(self.log_dir)
                if name.endswith(".jsonl")
                and (name.startswith(self.DETAIL_PREFIX) or name.startswith(self.ERROR_PREFIX))
            ]
        except Exception:
            return
        edge = datetime.date.today() - datetime.timedelta(days=self.max_days - 1)
        undated = []
        for name in names:
            path = os.path.join(self.log_dir, name)
            day = self._file_date(name)
            if day is None:
                undated.append(path)
                continue
            if day < edge:
                self._remove_quietly(path)
        # Файл без разбираемой даты чужой формату: держим его по старому
        # правилу количества, чтобы каталог не рос молча.
        if self.max_files and len(undated) > self.max_files:
            undated.sort(key=self._mtime_or_zero, reverse=True)
            for path in undated[self.max_files:]:
                self._remove_quietly(path)

    def _file_date(self, name):
        match = self.FILE_DATE_PATTERN.search(name)
        if not match:
            return None
        try:
            return datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    def _mtime_or_zero(self, path):
        try:
            return os.path.getmtime(path)
        except Exception:
            return 0

    def _remove_quietly(self, path):
        try:
            os.remove(path)
        except Exception:
            pass
