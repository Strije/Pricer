"""Профиль заказа поставщика: варианты для выпадающих списков из справочников самого поставщика.

Перенесено из десктопа (settings_page._do_check_*): при проверке подключения он загружал у
поставщика способы доставки и оплаты, адреса, реквизиты, договоры и заполнял ими списки.
Здесь то же без Qt. load_options(section, cfg) возвращает
{"ok", "message", "options": {поле: [{"value", "label"}]}, "defaults": {поле: значение}}.
Только чтение: у поставщика ничего не меняется, в базе ничего не сохраняется — выбор
сохраняет пользователь кнопкой «Сохранить» в карточке поставщика.

defaults — то, что десктоп ставил в пустые поля (первый вариант или подсказка поставщика);
интерфейс применяет их только к незаполненным полям, сохранённый выбор не трогает.
"""


def obj_value(item, *names):
    """Первое непустое значение по именам: словарь или объект zeep (как _obj_value десктопа)."""
    for name in names:
        if isinstance(item, dict):
            if item.get(name) not in (None, ""):
                return item.get(name)
        elif hasattr(item, name):
            value = getattr(item, name)
            if value not in (None, ""):
                return value
    return ""


def as_items(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "data", "list", "values"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
        return list(value.values())
    return []


def options(items, id_names=("id",), label_names=("name", "title", "address")):
    out, seen = [], set()
    for item in items or []:
        value = str(obj_value(item, *id_names) or "")
        if not value or value in seen:
            continue
        seen.add(value)
        out.append({"value": value, "label": str(obj_value(item, *label_names) or value)})
    return out


def _pick(current, opts):
    """Выбранное значение для зависимых списков: сохранённое, если поставщик его знает, иначе первое."""
    values = [item["value"] for item in opts]
    current = str(current or "")
    if current in values:
        return current
    return values[0] if values else ""


def _first(opts):
    return opts[0]["value"] if opts else ""


def _timeout(cfg, default):
    try:
        return max(1, int(cfg.get("timeout") or default))
    except (TypeError, ValueError):
        return default


def _fail(message, **extra):
    return {"ok": False, "message": str(message), "options": {}, "defaults": {}, **extra}


def profit_league(cfg):
    from pr_lg import PrLgProvider

    if not cfg.get("api_key"):
        return _fail("не задан API-ключ")
    provider = PrLgProvider(cfg["api_key"], timeout=_timeout(cfg, 12))
    ok, message = provider.check_connection()
    if not ok:
        return _fail(message)
    params = provider.get_order_params() or {}
    methods = as_items(params.get("methods") or params.get("delivery") or params.get("deliveries"))
    payments = as_items(params.get("payment") or params.get("payments"))
    points = as_items(params.get("points"))
    pickups = as_items(params.get("pickup_points") or params.get("pickupPoints"))
    labels = ("name", "title", "label", "description")
    opts = {
        "order_method": options(methods, ("id", "ID", "method", "code"), labels),
        "order_payment": options(payments, ("id", "ID", "payment", "code"), labels),
        "order_point": options(points, ("code", "id", "ID"), ("name", "title", "address", "label")),
        "order_address": options(points, ("address", "name", "title"), ("address", "name", "title")),
        "order_pickup_point": options(pickups, ("code", "id", "ID"), ("name", "title", "address", "label")),
    }
    # десктоп выбирал первый вариант в каждом пустом списке
    defaults = {field: _first(items) for field, items in opts.items() if items}
    counts = (f"доставок: {len(methods)}, оплат: {len(payments)}, точек: {len(points)}, "
              f"самовывозов: {len(pickups)}")
    if not params:
        counts = provider.last_message or "параметры заказа не получены"
    return {"ok": True, "message": f"{message}; {counts}", "options": opts, "defaults": defaults}


def armtek(cfg):
    from armtek import ArmtekProvider

    if not (cfg.get("login") and cfg.get("password")):
        return _fail("не заданы логин и пароль")
    provider = ArmtekProvider(cfg["login"], cfg["password"], cfg.get("vkorg") or "", cfg.get("kunnr") or "",
                              timeout=_timeout(cfg, 12))
    vkorg_items = provider.get_user_vkorg_list()
    vkorgs = options(vkorg_items, ("VKORG",), ("PROGRAM_NAME", "VKORG"))
    if not vkorgs:
        return _fail("сбытовые организации (VKORG) не вернулись")
    vkorg = _pick(cfg.get("vkorg"), vkorgs)
    info = provider.get_user_info(vkorg)
    customers = provider.user_info_customers(info)
    addresses = provider.user_info_addresses(info)
    contacts = provider.user_info_contacts(info)
    opts = {
        "vkorg": vkorgs,
        "kunnr": options(customers, ("KUNNR",), ("SNAME", "FNAME", "KUNNR")),
        "kunnr_we": options(provider.user_info_consignees(info), ("KUNNR",), ("SNAME", "FNAME", "NAME", "KUNNR")),
        "kunnr_za": options(addresses, ("KUNNR_ZA", "KUNNR", "ID", "CODE"),
                            ("ADDRESS", "ADDR", "ADRES", "SNAME", "FNAME", "NAME", "KUNNR")),
        "incoterms": options(provider.user_info_pickups(info), ("INCOTERMS", "ID", "CODE", "STORE"),
                             ("NAME", "SNAME", "FNAME", "ADDRESS", "ADDR", "INCOTERMS", "ID", "CODE")),
        "parnr": options(contacts, ("PARNR", "ID", "CODE"), ("NAME", "SNAME", "FNAME", "PARNR", "ID")),
        "vbeln": options(provider.user_info_contracts(info), ("VBELN", "ID", "CODE"),
                         ("NAME", "SNAME", "FNAME", "VBELN", "ID")),
    }
    defaults = provider.defaults_from_user_data(vkorg_items, info)
    defaults["vkorg"] = vkorg  # списки ниже построены для этой организации
    message = (f"VKORG: {len(vkorgs)}, покупателей: {len(customers)}, адресов: {len(addresses)}, "
               f"контактов: {len(contacts)}")
    return {"ok": True, "message": message, "options": opts, "defaults": defaults}


def abstd(cfg):
    from abstd import AbstdProvider

    if not (cfg.get("login") and cfg.get("password")):
        return _fail("не заданы логин и пароль")
    provider = AbstdProvider(cfg["login"], cfg["password"], cfg.get("agreement_id") or "", timeout=_timeout(cfg, 10))
    data = provider.get_user_context()
    if not provider.is_user_context(data):
        status = data.get("status") if isinstance(data, dict) else ""
        return _fail(provider.last_message or status or "ошибка авторизации")
    opts = {
        "delivery_address_id": options(data.get("user_delivery_addresses"), ("uda_id", "id"),
                                       ("uda_name", "address", "name")),
        "delivery_type_id": options(data.get("delivery_types"), ("dt_id", "id"), ("name", "title")),
    }
    # договор и корзина — текстовые поля: десктоп подставлял их, только если они пусты
    defaults = {key: value for key, value in provider.context_defaults(data).items() if value}
    return {"ok": True, "message": provider.context_message(data), "options": opts, "defaults": defaults}


def rossko(cfg):
    from rossko import RosskoProvider

    if not (cfg.get("key1") and cfg.get("key2")):
        return _fail("не заданы KEY1 и KEY2")
    provider = RosskoProvider(cfg["key1"], cfg["key2"], timeout=_timeout(cfg, 15))
    details = provider.get_checkout_details()
    if not details.get("success"):
        return _fail(details.get("message") or "справочники заказа не получены")
    opts = {
        "delivery_id": options(details.get("deliveries"), ("id", "Id", "ID", "delivery_id"),
                               ("name", "Name", "title", "Title")),
        "address_id": options(details.get("addresses"), ("id", "Id", "ID", "address_id"),
                              ("name", "Name", "address", "Address", "title", "Title")),
        "payment_id": options(details.get("payments"), ("id", "Id", "ID", "payment_id"),
                              ("name", "Name", "title", "Title")),
        "requisite_id": options(details.get("companies"), ("id", "Id", "ID", "requisite_id"),
                                ("name", "Name", "company_name", "CompanyName", "company", "Company", "title", "Title")),
    }
    defaults = {key: value for key, value in provider.checkout_defaults(details).items() if value}
    return {"ok": True, "message": provider.checkout_details_message(details), "options": opts, "defaults": defaults}


def tiss_tmparts(cfg):
    from tiss_tmparts import TissTmpartsProvider

    if not cfg.get("api_key"):
        return _fail("не задан API-ключ")
    try:
        warehouse_mode = 1 if int(cfg.get("warehouse_mode") or 0) == 1 else 0
    except (TypeError, ValueError):
        warehouse_mode = 0
    provider = TissTmpartsProvider(cfg["api_key"], warehouse_mode, timeout=_timeout(cfg, 6))
    # Цепочка как в десктопе: юрлицо -> договоры -> точки доставки -> склады точки.
    legal = options(provider.get_legal_organizations(), ("id",), ("name", "tin", "id"))
    if not legal:
        return _fail(provider.last_message or "юрлица не найдены")
    legal_id = _pick(cfg.get("legal_organization_id"), legal)
    provider.legal_organization_id = legal_id
    opts = {"legal_organization_id": legal}
    defaults = {"legal_organization_id": legal_id}
    contracts = options(provider.get_contracts(legal_id), ("id",), ("name", "legalOrganizationName", "id"))
    opts["contract_id"] = contracts
    if not contracts:
        return {**_fail(provider.last_message or "договоры не найдены"), "options": opts, "defaults": defaults}
    contract_id = defaults["contract_id"] = _pick(cfg.get("contract_id"), contracts)
    outlets = options(provider.get_outlets(contract_id), ("id",), ("address", "id"))
    opts["outlet_id"] = outlets
    if not outlets:
        return {**_fail(provider.last_message or "точки доставки не найдены"), "options": opts, "defaults": defaults}
    outlet_id = defaults["outlet_id"] = _pick(cfg.get("outlet_id"), outlets)
    warehouses = [str(obj_value(item, "id")) for item in provider.get_warehouses(outlet_id) or [] if obj_value(item, "id")]
    if warehouses:
        defaults["allowed_warehouses"] = ",".join(warehouses)
    parts = [f"юрлиц: {len(legal)}", f"договоров: {len(contracts)}", f"точек: {len(outlets)}"]
    if warehouses:
        parts.append(f"складов: {len(warehouses)}")
    return {"ok": True, "message": ", ".join(parts), "options": opts, "defaults": defaults}


LOADERS = {
    "profit_league": profit_league,
    "armtek": armtek,
    "abstd": abstd,
    "rossko": rossko,
    "tiss_tmparts": tiss_tmparts,
}


def load_options(section, cfg):
    loader = LOADERS.get(section)
    if loader is None:
        return _fail("у этого поставщика нет справочников для заказа")
    result = loader(dict(cfg or {}))
    # только непустые значения: пустое поле в выдаче не нужно
    result["defaults"] = {key: str(value) for key, value in (result.get("defaults") or {}).items() if value not in (None, "")}
    return result
