import copy
import threading
import time


class ProviderResultCache:
    def __init__(self, ttl_seconds=300, max_entries=120, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._entries = {}
        self._counter = 0
        self.configure(ttl_seconds=ttl_seconds, max_entries=max_entries)

    def configure(self, ttl_seconds=300, max_entries=120):
        self.ttl_seconds = max(0, int(ttl_seconds or 0))
        self.max_entries = max(0, int(max_entries or 0))
        if not self.ttl_seconds or not self.max_entries:
            self.clear()

    def get(self, key):
        if not self.ttl_seconds or not self.max_entries:
            return False, None, 0
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return False, None, 0
            age = now - entry["created_at"]
            if age > self.ttl_seconds:
                self._entries.pop(key, None)
                return False, None, 0
            entry["last_access_at"] = now
            entry["last_access_order"] = self._next_order_locked()
            return True, copy.deepcopy(entry["value"]), age

    def set(self, key, value):
        if not self.ttl_seconds or not self.max_entries:
            return False
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            self._entries[key] = {
                "created_at": now,
                "last_access_at": now,
                "last_access_order": self._next_order_locked(),
                "value": copy.deepcopy(value),
            }
            self._evict_locked()
            return True

    def clear(self):
        with self._lock:
            removed = len(self._entries)
            self._entries.clear()
            return removed

    def stats(self):
        with self._lock:
            return {
                "entries": len(self._entries),
                "ttl_seconds": self.ttl_seconds,
                "max_entries": self.max_entries,
            }

    def _purge_expired_locked(self, now):
        expired = [
            key for key, entry in self._entries.items()
            if now - entry["created_at"] > self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _evict_locked(self):
        while len(self._entries) > self.max_entries:
            oldest_key = min(
                self._entries,
                key=lambda key: (
                    self._entries[key]["last_access_at"],
                    self._entries[key].get("last_access_order", 0),
                ),
            )
            self._entries.pop(oldest_key, None)

    def _next_order_locked(self):
        self._counter += 1
        return self._counter
