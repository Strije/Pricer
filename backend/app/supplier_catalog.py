"""Справочник поставщиков: какие поля у каждого, какие из них секретные.

Формат настроек движка совпадает с settings.json десктопа (разделы profit_league, armtek, …,
список abcp_suppliers). В базе каждый раздел хранится отдельной учётной записью поставщика:
обычные поля открыто, секретные — зашифрованными. Здесь же разбор settings.json на учётные
записи и обратная сборка для движка.
"""
import re

# Поле: (имя, подпись, тип). Тип "secret" — шифруется и никогда не отдаётся через API.
CATALOG = {
    "profit_league": {
        "title": "Profit-League",
        "fields": [("api_key", "API-ключ", "secret")],
    },
    "favorit": {
        "title": "Фаворит",
        "fields": [("api_key", "API-ключ", "secret"), ("developer_key", "Ключ разработчика", "secret"),
                   ("include_analogues", "Искать аналоги", "bool")],
    },
    "armtek": {
        "title": "Armtek",
        "fields": [("login", "Логин", "secret"), ("password", "Пароль", "secret"),
                   ("vkorg", "Сбытовая организация (VKORG)", "text"), ("kunnr", "Покупатель (KUNNR)", "text"),
                   ("allowed_warehouses", "Разрешённые склады", "text")],
    },
    "forum_auto": {
        "title": "Forum-Auto",
        "fields": [("login", "Логин", "secret"), ("password", "Пароль", "secret"),
                   ("include_crosses", "Искать аналоги", "bool"), ("allowed_warehouses", "Разрешённые склады", "text")],
    },
    "mikado": {
        "title": "Mikado",
        "fields": [("login", "Логин (ClientID)", "secret"), ("password", "Пароль", "secret")],
    },
    "abstd": {
        "title": "ABSTD",
        "fields": [("login", "Логин", "secret"), ("password", "Пароль", "secret"),
                   ("agreement_id", "Договор", "text"), ("allowed_warehouses", "Разрешённые склады", "text")],
    },
    "rossko": {
        "title": "Rossko",
        "fields": [("key1", "KEY1", "secret"), ("key2", "KEY2", "secret"), ("delivery_id", "Доставка", "text"),
                   ("allowed_warehouses", "Разрешённые склады", "text")],
    },
    "avtoto": {
        "title": "Avtoto",
        "fields": [("client_id", "Номер клиента", "secret"), ("login", "Логин", "secret"),
                   ("password", "Пароль", "secret"), ("excluded_warehouses", "Исключённые склады", "text")],
    },
    "tiss_tmparts": {
        "title": "TISS",
        "fields": [("api_key", "API-ключ", "secret"), ("contract_id", "Договор", "text"),
                   ("outlet_id", "Точка доставки", "text"), ("allowed_warehouses", "Разрешённые склады", "text")],
    },
    "tradesoft": {
        "title": "Tradesoft (Автоформула)",
        "fields": [("user", "Пользователь Tradesoft", "secret"), ("password", "Пароль Tradesoft", "secret"),
                   ("provider_id", "Код поставщика в Tradesoft", "text"),
                   ("provider_login", "Логин у поставщика", "secret"), ("provider_password", "Пароль у поставщика", "secret")],
    },
    "abcp_suppliers": {
        "title": "Поставщик на платформе ABCP",
        "multiple": True,
        "fields": [("name", "Название", "text"), ("host", "Адрес API (idNNNNN.public.api.abcp.ru)", "text"),
                   ("login", "Логин", "secret"), ("password", "Пароль", "secret")],
    },
    "abcp_reference": {
        "title": "Справочник ABCP",
        "fields": [("host", "Адрес API", "text"), ("login", "Логин", "secret"), ("password", "Пароль", "secret")],
    },
    "url_csv": {
        "title": "Прайс-листы по ссылкам",
        # Адреса прайсов часто содержат логин и пароль FTP или токен, поэтому они секретные.
        "fields": [("urls", "Ссылки на прайсы", "secret"), ("url_profiles", "Настройки ссылок", "secret")],
    },
}

# Общие для всех поставщиков поля (видны в форме, не секретные).
COMMON_FIELDS = [("enabled", "Включён", "bool"), ("timeout", "Таймаут, с", "int"),
                 ("warehouse_extra_days", "Доп. дни по складам", "text")]

# Настройки организации (не относятся к конкретному поставщику).
ORG_KEYS = ("default_markup", "markup_rules", "rounding_mode", "rounding_from", "rounding_to",
            "hide_no_return", "max_crosses", "provider_cache", "default_comment")

# Запасной признак секрета для полей, которых нет в справочнике (на случай новых версий десктопа).
_SECRET_NAME = re.compile(r"(?i)(key|pass|login|user|token|secret|phone|client_id|contact_name)")
# Поля, которые не переносим вовсе: подписи с личными данными и служебное состояние десктопа.
_SKIP = re.compile(r"(?i)(_label$|^first_run$|^expected_ip$|^managers$)")


def secret_fields(section):
    spec = CATALOG.get(section, {})
    return {name for name, _label, kind in spec.get("fields", []) if kind == "secret"}


def is_secret(section, key, value):
    if key in secret_fields(section):
        return True
    return isinstance(value, str) and bool(_SECRET_NAME.search(key)) and bool(value)


def split_account(section, data):
    """Раздел настроек -> (открытые поля, секретные поля)."""
    config, secrets = {}, {}
    for key, value in (data or {}).items():
        if _SKIP.search(key):
            continue
        (secrets if is_secret(section, key, value) else config)[key] = value
    return config, secrets


def split_settings(settings):
    """settings.json десктопа -> (настройки организации, список (раздел, открытые, секретные))."""
    settings = dict(settings or {})
    org = {key: settings[key] for key in ORG_KEYS if key in settings}
    accounts = []
    for section in CATALOG:
        value = settings.get(section)
        if CATALOG[section].get("multiple"):
            for entry in value or []:
                if isinstance(entry, dict):
                    accounts.append((section,) + split_account(section, entry))
        elif isinstance(value, dict):
            accounts.append((section,) + split_account(section, value))
    return org, accounts


def compose_settings(org, accounts):
    """Обратная сборка: настройки организации + учётные записи -> формат settings.json для движка."""
    settings = dict(org or {})
    for section, config, secrets in accounts:
        merged = {**(config or {}), **(secrets or {})}
        if CATALOG.get(section, {}).get("multiple"):
            settings.setdefault(section, []).append(merged)
        else:
            settings[section] = merged
    return settings


def public_catalog():
    return {
        section: {
            "title": spec["title"],
            "multiple": bool(spec.get("multiple")),
            "fields": [{"name": n, "label": label, "type": kind} for n, label, kind in COMMON_FIELDS + spec["fields"]],
        }
        for section, spec in CATALOG.items()
    }
