"""Очистка текстов от секретов поставщиков перед сохранением и показом.

Поставщики возвращают в сообщениях об ошибках адрес запроса целиком — вместе с логином и паролем
(например, Forum-Auto: «ошибка сети … listGoods?login=…&pass=…»). Такие тексты попадают в заказ,
журнал отправок и на экран. Здесь их чистят: по значениям секретов организации (и их URL-кодированным
вариантам) и по именам параметров, значение которых всегда секретно.
"""
import re
from urllib.parse import quote, quote_plus

MASK = "***"
_PARAM = (r"(?:auth\w*|userpsw|psw|pwd|pass\w*|password|secret|token|api_?key|apikey|key\d?|sign\w*|hash"
          r"|session_?id|sid|login|userlogin|user(?:_?name|_?login|_?password)?|client_?id|developer_?key)")
_QUERY = re.compile(r"(?i)([?&;]" + _PARAM + r"=)([^&#\s\"']+)")
_JSON = re.compile(r'(?i)("' + _PARAM + r'"\s*:\s*")([^"]*)(")')


# Поля с текстами для людей: только их и чистим. Идентификаторы, цены и коды складов не трогаем —
# короткий числовой логин (номер клиента) мог бы совпасть с частью кода и испортить заказ.
TEXT_KEYS = {"reason", "message", "error", "data", "verification_message", "submit_unknown_reason",
             "skip_reason", "last_message", "status_text"}
RESPONSE_KEYS = {"supplier_response", "response"}  # ответ поставщика целиком


class Redactor:
    def __init__(self, secret_values=()):
        values = set()
        for value in secret_values:
            for item in (value if isinstance(value, (list, tuple)) else [value]):
                if isinstance(item, str) and len(item.strip()) >= 3:
                    item = item.strip()
                    values.update({item, quote(item, safe=""), quote_plus(item)})
        ordered = sorted(values, key=len, reverse=True)
        self._values = re.compile("|".join(re.escape(v) for v in ordered)) if ordered else None

    def text(self, value):
        if not value:
            return value
        if self._values is not None:
            value = self._values.sub(MASK, value)
        value = _QUERY.sub(lambda m: m.group(1) + MASK, value)
        return _JSON.sub(lambda m: m.group(1) + (MASK if m.group(2) else "") + m.group(3), value)

    def messages(self, data):
        """Чистит текстовые поля и ответы поставщиков в любой вложенности, остальное не меняет."""
        if isinstance(data, dict):
            return {key: (self.deep(value) if key in TEXT_KEYS or key in RESPONSE_KEYS else self.messages(value))
                    for key, value in data.items()}
        if isinstance(data, (list, tuple)):
            return [self.messages(value) for value in data]
        return data

    def deep(self, data):
        """Рекурсивно чистит все строки в словарях и списках."""
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, dict):
            return {key: self.deep(value) for key, value in data.items()}
        if isinstance(data, (list, tuple)):
            return [self.deep(value) for value in data]
        return data
