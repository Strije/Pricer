import time
from requests import exceptions as request_exceptions


TRANSPORT_EXCEPTIONS = (
    request_exceptions.Timeout,
    request_exceptions.ConnectionError,
    request_exceptions.SSLError,
    request_exceptions.ReadTimeout,
)


class OrderDeliveryUnknown(Exception):
    """Запрос оформления ушёл, а ответ не дошёл: заказ мог уйти поставщику."""


def call_order_once(func, *args, **kwargs):
    """Выполняет запрос оформления заказа ровно один раз, без повторов.

    Повтор здесь опаснее ошибки: по таймауту чтения заказ у поставщика уже
    мог создаться, и вторая попытка сделает дубль. Транспортный обрыв
    превращаем в OrderDeliveryUnknown — исход неизвестен, и решать должен
    оператор, а не автоматика.
    """
    try:
        return func(*args, **kwargs)
    except TRANSPORT_EXCEPTIONS as exc:
        raise OrderDeliveryUnknown(str(exc) or exc.__class__.__name__) from exc


class ProviderAdapter:
    """Общий адаптер для сетевых вызовов провайдеров."""

    def __init__(self, retries=3, backoff=0.5, timeout=20, retryable_exceptions=None):
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.retryable_exceptions = retryable_exceptions or (
            request_exceptions.Timeout,
            request_exceptions.ConnectionError,
            request_exceptions.SSLError,
            request_exceptions.ReadTimeout,
        )

    def call(self, func, *args, **kwargs):
        return run_with_retries(
            func,
            retries=self.retries,
            backoff=self.backoff,
            timeout=self.timeout,
            retryable_exceptions=self.retryable_exceptions,
            args=args,
            kwargs=kwargs,
        )


def run_with_retries(func, retries=3, backoff=0.5, timeout=None, retryable_exceptions=None, args=None, kwargs=None):
    args = args or ()
    kwargs = kwargs or {}
    retryable_exceptions = retryable_exceptions or (
        request_exceptions.Timeout,
        request_exceptions.ConnectionError,
        request_exceptions.SSLError,
        request_exceptions.ReadTimeout,
    )

    last_error = None
    for attempt in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            if not isinstance(exc, retryable_exceptions):
                raise
            if attempt >= retries:
                raise
            if backoff:
                time.sleep(backoff * (attempt + 1))
    raise last_error
