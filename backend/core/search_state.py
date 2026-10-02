import threading


class SearchState:
    def __init__(self):
        self._lock = threading.Lock()
        self._current_id = 0
        self._active_key = None
        self._cancelled = set()

    def start(self, key):
        normalized_key = str(key or "").strip().upper()
        with self._lock:
            if self._active_key == normalized_key and self._current_id not in self._cancelled:
                return self._current_id, False
            self._current_id += 1
            self._active_key = normalized_key
            self._cancelled.discard(self._current_id)
            return self._current_id, True

    def cancel(self, search_id=None):
        with self._lock:
            target_id = self._current_id if search_id is None else int(search_id)
            if target_id <= 0:
                return
            self._cancelled.add(target_id)
            if target_id == self._current_id:
                self._active_key = None

    def finish(self, search_id):
        with self._lock:
            if int(search_id) == self._current_id:
                self._active_key = None

    def is_current(self, search_id):
        with self._lock:
            return int(search_id) == self._current_id and int(search_id) not in self._cancelled

    @property
    def current_id(self):
        with self._lock:
            return self._current_id
