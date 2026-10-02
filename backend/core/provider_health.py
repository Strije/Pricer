import time


class ProviderCircuitBreaker:
    OPERATIONAL_MARKERS = (
        "timeout",
        "таймаут",
        "не ответил",
        "ошибка сети",
        "ошибка соединения",
        "connection",
        "ssl",
        "tls",
        "dns",
        "http 401",
        "http 403",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "вернул не json",
        "не json",
        "unauthorized",
        "forbidden",
        "too many requests",
    )

    def __init__(self, threshold=5, cooldown_seconds=45, clock=None):
        self.threshold = max(1, int(threshold or 1))
        self.cooldown_seconds = max(1, int(cooldown_seconds or 1))
        self._clock = clock or time.monotonic
        self._state = {}

    def allow(self, provider_name):
        state = self._state.get(provider_name, {})
        open_until = float(state.get("open_until") or 0)
        now = self._clock()
        if open_until > now:
            return False, int(open_until - now + 0.999)
        if open_until:
            # Пауза вышла — возвращаем поставщика с чистым счётчиком.
            # Раньше счётчик оставался на пороге, и первый же хвостовой таймаут
            # снова закрывал поставщика на полную паузу: на заказе из файла
            # Avtoto так выпадал из торгов на десятки строк подряд.
            state["open_until"] = 0
            state["failures"] = 0
            self._state[provider_name] = state
        return True, 0

    def record_success(self, provider_name):
        self._state[provider_name] = {
            "failures": 0,
            "open_until": 0,
            "last_error": "",
        }

    def record_failure(self, provider_name, message=""):
        state = self._state.setdefault(
            provider_name,
            {"failures": 0, "open_until": 0, "last_error": ""},
        )
        state["failures"] = int(state.get("failures") or 0) + 1
        state["last_error"] = str(message or "")[:300]
        if state["failures"] >= self.threshold:
            state["open_until"] = self._clock() + self.cooldown_seconds
            return self.cooldown_seconds
        return 0

    def reset(self):
        self._state.clear()

    @classmethod
    def should_count_failure(cls, message):
        text = str(message or "").lower()
        if not text:
            return False
        return any(marker in text for marker in cls.OPERATIONAL_MARKERS)
