import os
import time

import requests

from abcp_reference import AbcpReferenceProvider
from abstd import AbstdProvider
from armtek import ArmtekProvider
from avtoto import AvtotoProvider
from favorit import FavoritProvider
from forum_auto import ForumAutoProvider
from mikado import MikadoProvider
from pr_lg import PrLgProvider
from rossko import RosskoProvider
from tiss_tmparts import TissTmpartsProvider
from tradesoft import TradesoftProvider
from abcp_supplier import abcp_supplier_class
from url_csv_provider import UrlCsvProvider


PROVIDER_SPECS = [
    ("profit_league", "Profit-League"),
    ("favorit", "Фаворит"),
    ("armtek", "Armtek"),
    ("forum_auto", "Forum-Auto"),
    ("mikado", "Mikado"),
    ("abstd", "ABSTD"),
    ("rossko", "Rossko"),
    ("avtoto", "Avtoto"),
    ("tiss_tmparts", "TISS"),
    ("tradesoft", "Автоформула"),
    ("url_csv", "Прайсы"),
    ("abcp_reference", "ABCP справочник"),
]

REQUIRED_FIELDS = {
    "profit_league": ("api_key",),
    "favorit": ("api_key",),
    "armtek": ("login", "password", "vkorg", "kunnr"),
    "forum_auto": ("login", "password"),
    "mikado": ("login", "password"),
    "abstd": ("login", "password", "agreement_id"),
    "rossko": ("key1", "key2", "delivery_id"),
    "avtoto": ("client_id", "login", "password"),
    "tiss_tmparts": ("api_key",),
    "tradesoft": ("user", "password", "provider_login", "provider_password"),
    "abcp_supplier": ("host", "login", "password"),
    "url_csv": ("urls",),
    "abcp_reference": ("host", "login", "password"),
}

SECRET_FIELDS = {
    "api_key",
    "developer_key",
    "password",
    "key1",
    "key2",
    "provider_password",
}


def diagnose_ip(expected_ip="", timeout=5):
    started_at = time.monotonic()
    try:
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            "https://api.ipify.org",
            timeout=timeout,
            proxies={"http": None, "https": None},
        )
        if response.status_code != 200:
            return _result("ip", "Текущий IP", "error", f"HTTP {response.status_code}", started_at)
        current_ip = response.text.strip()
        expected_ip = str(expected_ip or "").strip()
        if expected_ip and current_ip != expected_ip:
            return _result(
                "ip",
                "Текущий IP",
                "warn",
                f"{current_ip}; ожидался {expected_ip}",
                started_at,
            )
        suffix = "совпадает с ожидаемым" if expected_ip else "получен"
        return _result("ip", "Текущий IP", "ok", f"{current_ip}; {suffix}", started_at)
    except Exception as exc:
        return _result("ip", "Текущий IP", "error", f"не удалось определить: {str(exc)[:100]}", started_at)


def diagnose_provider(config_key, display_name, cfg):
    started_at = time.monotonic()
    cfg = cfg if isinstance(cfg, dict) else {}
    if not cfg.get("enabled", False):
        return _result(config_key, display_name, "off", "выключен в настройках", started_at)

    missing = _missing_required(config_key, cfg)
    if missing:
        return _result(
            config_key,
            display_name,
            "warn",
            "не заполнено: " + ", ".join(missing),
            started_at,
        )

    try:
        status, message = _call_provider_check(config_key, cfg)
        return _result(config_key, display_name, status, _redact(str(message or ""), cfg), started_at)
    except Exception as exc:
        return _result(config_key, display_name, "error", _redact(str(exc)[:160], cfg), started_at)


def _call_provider_check(config_key, cfg):
    timeout = _int(cfg.get("timeout"), 10)
    if config_key == "profit_league":
        ok, message = PrLgProvider(cfg.get("api_key", ""), timeout=timeout).check_connection()
        return ("ok" if ok else "error"), message
    if config_key == "favorit":
        ok, message = FavoritProvider(
            cfg.get("api_key", ""),
            cfg.get("include_analogues", True),
            timeout=timeout,
            developer_key=cfg.get("developer_key", ""),
        ).check_connection()
        return ("ok" if ok else "error"), message
    if config_key == "armtek":
        provider = ArmtekProvider(
            cfg.get("login", ""),
            cfg.get("password", ""),
            cfg.get("vkorg", ""),
            cfg.get("kunnr", ""),
            cfg.get("kunnr_we", ""),
            cfg.get("kunnr_za", ""),
            cfg.get("incoterms", ""),
            cfg.get("parnr", ""),
            cfg.get("vbeln", ""),
            bool(cfg.get("use_pickup", False)),
            timeout=timeout,
        )
        provider.ping()
        if not cfg.get("kunnr"):
            return "warn", "доступ есть, но покупатель KUNNR не выбран"
        return "ok", "доступ к API есть"
    if config_key == "forum_auto":
        ok, message = ForumAutoProvider(
            cfg.get("login", ""),
            cfg.get("password", ""),
            cfg.get("include_crosses", True),
            timeout=timeout,
            external_order_id=cfg.get("external_order_id", ""),
        ).check_connection()
        return ("ok" if ok else "error"), message
    if config_key == "mikado":
        provider = MikadoProvider(cfg.get("login", ""), cfg.get("password", ""), timeout=timeout)
        brands = provider.get_brand_candidates("12345")
        return ("ok" if brands else "error"), f"тестовый поиск вернул брендов: {len(brands)}"
    if config_key == "abstd":
        provider = AbstdProvider(
            cfg.get("login", ""),
            cfg.get("password", ""),
            cfg.get("agreement_id", ""),
            cfg.get("cart_id", "0"),
            cfg.get("delivery_address_id", ""),
            cfg.get("delivery_type_id", "1"),
            timeout=timeout,
        )
        data = provider.get_user_context()
        if provider.is_user_context(data):
            return "ok", provider.context_message(data)
        if isinstance(data, dict):
            return "error", provider.last_message or data.get("status") or "ошибка авторизации"
        return "error", provider.last_message or "ошибка авторизации"
    if config_key == "rossko":
        ok, message = RosskoProvider(
            cfg.get("key1", ""),
            cfg.get("key2", ""),
            delivery_id=cfg.get("delivery_id", ""),
            address_id=cfg.get("address_id", ""),
            payment_id=cfg.get("payment_id", "1"),
            requisite_id=cfg.get("requisite_id", ""),
            contact_name=cfg.get("contact_name", ""),
            contact_phone=cfg.get("contact_phone", ""),
            timeout=timeout,
        ).check_connection()
        return ("ok" if ok else "error"), message
    if config_key == "avtoto":
        ok, message = AvtotoProvider(
            cfg.get("client_id", ""),
            cfg.get("login", ""),
            cfg.get("password", ""),
            cfg.get("include_crosses", True),
            timeout=timeout,
        ).check_connection()
        return ("ok" if ok else "error"), message
    if config_key == "tiss_tmparts":
        ok, message = TissTmpartsProvider(
            cfg.get("api_key", ""),
            int(cfg.get("warehouse_mode") or 0),
            timeout=timeout,
            legal_organization_id=cfg.get("legal_organization_id", ""),
            contract_id=cfg.get("contract_id", ""),
            outlet_id=cfg.get("outlet_id", ""),
            warehouses=cfg.get("allowed_warehouses", ""),
            include_analogues=cfg.get("include_analogues", True),
            delivery_type=cfg.get("delivery_type", "ToOutlet"),
            phone_number=cfg.get("phone_number", ""),
            one_time_delivery=cfg.get("one_time_delivery", True),
            not_group_reserves=cfg.get("not_group_reserves", False),
            express_delivery=cfg.get("express_delivery", False),
        ).check_connection()
        return ("ok" if ok else "error"), message
    if config_key == "abcp_supplier":
        ok, message = abcp_supplier_class(display_name)(
            cfg.get("host", ""),
            cfg.get("login", ""),
            cfg.get("password", ""),
            timeout=timeout,
        ).check_connection()
        return ("ok" if ok else "error"), message
    if config_key == "tradesoft":
        ok, message = TradesoftProvider(
            cfg.get("user", ""),
            cfg.get("password", ""),
            provider_id=cfg.get("provider_id") or "AVTOFORMULA",
            provider_login=cfg.get("provider_login", ""),
            provider_password=cfg.get("provider_password", ""),
            timeout=timeout,
        ).check_connection()
        return ("ok" if ok else "error"), message
    if config_key == "url_csv":
        return _check_url_csv(cfg)
    if config_key == "abcp_reference":
        provider = AbcpReferenceProvider(
            cfg.get("host", ""),
            cfg.get("login", ""),
            cfg.get("password", ""),
            include_crosses=cfg.get("include_crosses", False),
            include_images=cfg.get("include_images", False),
            allow_articles_info=cfg.get("allow_articles_info", False),
            articles_info_daily_limit=cfg.get("articles_info_daily_limit", 8),
            timeout=timeout,
        )
        ok, message = provider.check_connection()
        suffix = "; articles/info выключен" if not cfg.get("allow_articles_info", False) else ""
        return ("ok" if ok else "error"), f"{message}{suffix}"
    return "warn", "проверка пока не описана"


def _check_url_csv(cfg):
    urls = cfg.get("urls") or []
    if isinstance(urls, str):
        urls = [line.strip() for line in urls.splitlines() if line.strip()]
    provider = UrlCsvProvider(
        urls=urls,
        name=cfg.get("name", "Прайсы"),
        timeout=_int(cfg.get("timeout"), 20),
        column_map=cfg.get("column_map", {}),
        default_days=cfg.get("default_days", 0),
        url_profiles=cfg.get("url_profiles", {}),
        cache_hours=cfg.get("cache_hours", 24),
    )
    active_urls = [
        url for url in provider.urls
        if (provider._profile_for_url(url) or {}).get("enabled", True) is not False
    ]
    if not active_urls:
        return "warn", "нет включённых прайсов"
    if not os.path.exists(provider._index_path()):
        return "warn", f"включено источников: {len(active_urls)}; SQLite-база ещё не создана"
    return "ok", f"включено источников: {len(active_urls)}; SQLite-база найдена"


def _missing_required(config_key, cfg):
    missing = []
    for field in REQUIRED_FIELDS.get(config_key, ()):
        value = cfg.get(field)
        if isinstance(value, list):
            empty = not any(str(item).strip() for item in value)
        else:
            empty = not str(value or "").strip()
        if empty:
            missing.append(_human_field(field))
    return missing


def _human_field(field):
    labels = {
        "api_key": "API-ключ",
        "developer_key": "ключ разработчика",
        "password": "пароль",
        "login": "логин",
        "vkorg": "VKORG",
        "kunnr": "KUNNR",
        "agreement_id": "договор",
        "delivery_id": "способ доставки",
        "client_id": "номер клиента",
        "host": "хост",
        "urls": "прайсы",
    }
    return labels.get(field, field)


def _redact(message, cfg):
    result = str(message or "")
    for key, value in (cfg or {}).items():
        if key not in SECRET_FIELDS:
            continue
        value = str(value or "")
        if value and len(value) >= 4:
            result = result.replace(value, "***")
    return result


def _result(config_key, display_name, status, message, started_at):
    return {
        "key": config_key,
        "name": display_name,
        "status": status,
        "message": str(message or ""),
        "elapsed": round(time.monotonic() - started_at, 2),
    }


def _int(value, default):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return int(default)
