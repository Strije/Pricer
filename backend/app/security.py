"""Пароли пользователей, шифрование секретов поставщиков, токены сессий."""
import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets

from cryptography.fernet import Fernet, InvalidToken

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password, stored):
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except (ValueError, AttributeError):
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def new_token():
    return _secrets.token_urlsafe(32)


def token_hash(token):
    """В базе храним только хэш токена сессии: утечка базы не даёт войти."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_master_key(var_dir):
    """Главный ключ шифрования: из PRICER_SECRET_KEY, иначе из файла var/secret.key (создаётся)."""
    key = os.environ.get("PRICER_SECRET_KEY", "").strip()
    if key:
        return key.encode()
    path = os.path.join(var_dir, "secret.key")
    if os.path.exists(path):
        with open(path, "rb") as file:
            return file.read().strip()
    os.makedirs(var_dir, exist_ok=True)
    key = Fernet.generate_key()
    with open(path, "wb") as file:
        file.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


class SecretBox:
    def __init__(self, key):
        self._fernet = Fernet(key)

    def seal(self, data):
        return self._fernet.encrypt(json.dumps(data or {}, ensure_ascii=False).encode("utf-8")).decode()

    def open(self, token):
        if not token:
            return {}
        try:
            return json.loads(self._fernet.decrypt(token.encode()).decode("utf-8"))
        except InvalidToken as exc:
            raise ValueError("секреты не расшифровываются: сменился ключ PRICER_SECRET_KEY?") from exc


class LoginLimiter:
    """Защита входа от подбора пароля: после `limit` неудач за `window` секунд по одному ключу
    (почта, адрес) вход по нему закрыт до конца окна. Память процесса — сервис работает в одном процессе."""

    def __init__(self, limit, window=900, clock=None):
        import threading
        import time

        self.limit, self.window = limit, window
        self.clock = clock or time.monotonic
        self.failures = {}
        self.lock = threading.Lock()

    def wait(self, key):
        """Сколько секунд ждать до следующей попытки (0 — можно)."""
        now = self.clock()
        with self.lock:
            recent = [t for t in self.failures.get(key, []) if now - t < self.window]
            self.failures[key] = recent
            if len(recent) >= self.limit:
                return int(self.window - (now - recent[0])) + 1
            return 0

    def fail(self, key):
        with self.lock:
            self.failures.setdefault(key, []).append(self.clock())
            if len(self.failures) > 50000:  # не растём бесконечно от перебора разных адресов
                self.failures.clear()

    def reset(self, key):
        with self.lock:
            self.failures.pop(key, None)
