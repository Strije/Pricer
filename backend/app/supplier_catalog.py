"""Справочник поставщиков: какие поля у каждого, какие из них секретные.

Формат настроек движка совпадает с settings.json десктопа (разделы profit_league, armtek, …,
список abcp_suppliers). В базе каждый раздел хранится отдельной учётной записью поставщика:
обычные поля открыто, секретные — зашифрованными. Здесь же разбор settings.json на учётные
записи и обратная сборка для движка.
"""
import re

# Поле: (имя, подпись, тип). Тип "secret" — шифруется и никогда не отдаётся через API.
# Тип "choice" — значение из справочника поставщика: варианты загружает кнопка «Загрузить у поставщика»
# (app/order_profile.py), сохраняется код варианта, как в выпадающих списках десктопа.
# order_fields — профиль заказа (доставка, оплата, адрес, реквизиты): поля и подписи — из settings_page десктопа.
CATALOG = {
    "profit_league": {
        "title": "Profit-League",
        "fields": [("api_key", "API-ключ", "secret")],
        "order_fields": [("create_order", "Оформлять заказ после добавления в корзину", "bool"),
                         ("order_method", "Способ доставки", "choice"), ("order_payment", "Способ оплаты", "choice"),
                         ("order_point", "Торговая точка", "choice"), ("order_address", "Адрес торговой точки", "choice"),
                         ("order_pickup_point", "Точка самовывоза", "choice")],
    },
    "favorit": {
        "title": "Фаворит",
        "fields": [("api_key", "API-ключ", "secret"), ("developer_key", "Ключ разработчика", "secret"),
                   ("include_analogues", "Искать аналоги", "bool")],
        # у Фаворита справочников для этих полей десктоп не загружает — вводятся кодами
        "order_fields": [("trade_point", "Торговая точка", "text"), ("payment_type", "Способ оплаты (1/2/3)", "text"),
                         ("delivery_type", "Способ получения (1/2)", "text"),
                         ("transport_type", "Способ доставки (0/1/2/3)", "text")],
    },
    "armtek": {
        "title": "Armtek",
        "fields": [("login", "Логин", "secret"), ("password", "Пароль", "secret"),
                   ("vkorg", "Сбытовая организация (VKORG)", "choice"), ("kunnr", "Покупатель (KUNNR)", "choice"),
                   ("allowed_warehouses", "Разрешённые склады", "text")],
        "order_fields": [("kunnr_we", "Грузополучатель", "choice"), ("use_pickup", "Самовывоз", "bool"),
                         ("incoterms", "Пункт выдачи для самовывоза", "choice"), ("kunnr_za", "Адрес доставки", "choice"),
                         ("parnr", "Контактное лицо", "choice"), ("vbeln", "Договор", "choice")],
    },
    "forum_auto": {
        "title": "Forum-Auto",
        "fields": [("login", "Логин", "secret"), ("password", "Пароль", "secret"),
                   ("include_crosses", "Искать аналоги", "bool"), ("allowed_warehouses", "Разрешённые склады", "text")],
        "order_fields": [("external_order_id", "ID заказа в вашей системе", "text")],
    },
    "mikado": {
        "title": "Mikado",
        "fields": [("login", "Логин (ClientID)", "secret"), ("password", "Пароль", "secret")],
    },
    "abstd": {
        "title": "ABSTD",
        "fields": [("login", "Логин", "secret"), ("password", "Пароль", "secret"),
                   ("agreement_id", "Договор", "text"), ("allowed_warehouses", "Разрешённые склады", "text")],
        "order_fields": [("cart_id", "ID корзины", "text"), ("delivery_address_id", "Адрес доставки", "choice"),
                         ("delivery_type_id", "Способ доставки", "choice")],
    },
    "rossko": {
        "title": "Rossko",
        "fields": [("key1", "KEY1", "secret"), ("key2", "KEY2", "secret"),
                   ("allowed_warehouses", "Разрешённые склады", "text")],
        # контакт и телефон — личные данные: хранятся зашифрованными, как при импорте settings.json
        "order_fields": [("delivery_id", "Способ доставки", "choice"), ("address_id", "Адрес доставки", "choice"),
                         ("payment_id", "Способ оплаты", "choice"), ("requisite_id", "Реквизиты", "choice"),
                         ("contact_name", "Контакт", "secret"), ("contact_phone", "Телефон", "secret")],
    },
    "avtoto": {
        "title": "Avtoto",
        "fields": [("client_id", "Номер клиента", "secret"), ("login", "Логин", "secret"),
                   ("password", "Пароль", "secret"), ("include_crosses", "Искать аналоги", "bool"),
                   ("excluded_warehouses", "Исключённые склады", "text")],
    },
    "tiss_tmparts": {
        "title": "TISS",
        "fields": [("api_key", "API-ключ", "secret"), ("legal_organization_id", "Юрлицо", "choice"),
                   ("contract_id", "Договор", "choice"), ("outlet_id", "Точка доставки", "choice"),
                   ("warehouse_mode", "Склады: 0 — все, 1 — только домашние", "text"),
                   ("allowed_warehouses", "Разрешённые склады", "text"), ("include_analogues", "Искать аналоги", "bool")],
        "order_fields": [("delivery_type", "Тип доставки заказа", "text"), ("phone_number", "Телефон для заказа", "secret"),
                         ("one_time_delivery", "Единая доставка", "bool"),
                         ("not_group_reserves", "Не группировать резервы", "bool"),
                         ("express_delivery", "Экспресс-доставка", "bool")],
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
    "laximo": {
        "title": "Каталог Laximo (подбор по VIN и госномеру)",
        "service": True,  # не поставщик: в поиске цен не участвует
        "fields": [("login", "Логин Laximo", "secret"), ("password", "Пароль Laximo", "secret")],
    },
    "yookassa": {
        "title": "Оплата ЮKassa (карта, СБП, SberPay) для подборов",
        "service": True,
        "fields": [("shop_id", "Идентификатор магазина (shopId)", "text"), ("secret_key", "Секретный ключ (secretKey)", "secret"),
                   ("test_mode", "Демо-режим (тестовый магазин)", "bool"), ("test_shop_id", "testShopId", "text"),
                   ("test_secret_key", "testSecretKey", "secret"),
                   ("receipts", "Отправлять данные для чеков (54-ФЗ)", "bool"),
                   ("tax_system_code", "Система налогообложения: 1 ОСН, 2 УСН доходы, 3 УСН доходы минус расходы, 6 патент", "int"),
                   ("vat_code", "Код НДС ЮKassa (1 — без НДС; код для 22% — по документации ЮKassa)", "int"),
                   ("payment_subject", "Признак предмета расчёта (commodity — товар)", "text"),
                   ("payment_mode", "Признак способа расчёта (full_prepayment — полная предоплата)", "text")],
    },
    "mailbox": {
        "title": "Почтовый ящик для прайсов (IMAP)",
        "service": True,
        "multiple": True,
        "fields": [("name", "Название", "text"), ("host", "Сервер IMAP", "text"), ("port", "Порт", "int"),
                   ("login", "Адрес почты (логин)", "text"), ("password", "Пароль приложения", "secret")],
    },
    "url_csv": {
        "title": "Прайс-листы по ссылкам",
        # Адреса прайсов часто содержат логин и пароль FTP или токен, поэтому они секретные.
        "fields": [("urls", "Ссылки на прайсы", "secret"), ("url_profiles", "Настройки ссылок", "secret")],
    },
}

# Общие для всех поставщиков поля (видны в форме, не секретные).
COMMON_FIELDS = [("enabled", "Включён", "bool"), ("timeout", "Таймаут, с", "int"),
                 ("warehouse_extra_days", "Доп. дни по складам", "text"),
                 # ссылка на поиск этого артикула на сайте поставщика: {article}, {brand}
                 ("site_search_url", "Поиск на сайте поставщика: ссылка с {article}", "text")]

# Настройки организации (не относятся к конкретному поставщику).
ORG_KEYS = ("default_markup", "markup_rules", "rounding_mode", "rounding_from", "rounding_to",
            "hide_no_return", "max_crosses", "provider_cache", "default_comment",
            # порог автовыбора бренда по голосам поставщиков (app/search.py)
            "brand_auto_share", "brand_auto_lead",
            # справочник гарантий брендов организации (по умолчанию — data/brand_warranty.json)
            "brand_warranty",
            # состав «ТО по машине» (app/service_template.py)
            "service_template")

# Запасной признак секрета для полей, которых нет в справочнике (на случай новых версий десктопа).
_SECRET_NAME = re.compile(r"(?i)(key|pass|login|user|token|secret|phone|client_id|contact_name)")
# Поля, которые не переносим вовсе: подписи с личными данными и служебное состояние десктопа.
_SKIP = re.compile(r"(?i)(_label$|^first_run$|^expected_ip$|^managers$)")


def section_fields(section):
    """Все поля раздела: подключение и профиль заказа."""
    spec = CATALOG.get(section, {})
    return list(spec.get("fields", [])) + list(spec.get("order_fields", []))


def secret_fields(section):
    return {name for name, _label, kind in section_fields(section) if kind == "secret"}


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
    from app.order_profile import LOADERS

    return {
        section: {
            "title": spec["title"],
            "multiple": bool(spec.get("multiple")),
            "service": bool(spec.get("service")),
            # у сервисов (Laximo, ЮKassa) общих полей поставщика нет — свои карточки в «Сервисах»
            "fields": [{"name": n, "label": label, "type": kind}
                       for n, label, kind in ([] if spec.get("service") else COMMON_FIELDS) + spec["fields"]]
                      + [{"name": n, "label": label, "type": kind, "group": "order"}
                         for n, label, kind in spec.get("order_fields", [])],
            # варианты для полей "choice" можно загрузить у поставщика
            "order_options": section in LOADERS,
        }
        for section, spec in CATALOG.items()
    }
