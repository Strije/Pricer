"""Движок поиска для заказа из файла, вынесенный из main.py (SkitchenApp) без Qt.

Методы перенесены дословно из десктопа (версия 1.0.3), чтобы поведение не разошлось:
опрос поставщиков, нормализация, фильтры складов и возврата, точный отбор, ранжирование,
наценка и итог по строке. Замены относительно оригинала:
- сигнал интерфейса log_signal.emit -> обратный вызов log;
- флажок наценки в боковой панели -> атрибут markup_enabled;
- настройка поставщиков из load_settings -> configure(settings).
Сгенерировано скриптом tools/gen_engine.py из main.py: перегенерация перезаписывает файл, поэтому
правки логики вносятся в генератор (как замены) или в main.py десктопа.
"""
import datetime
import math
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from abcp_reference import AbcpReferenceProvider
from abcp_supplier import AbcpSupplierProvider, abcp_supplier_class
from abstd import AbstdProvider
from armtek import ArmtekProvider
from avtoto import AvtotoProvider
from brand_aliases import BrandAliasResolver
from bulk_order import filter_exact_offers, rank_offers, status_for_selection
from favorit import FavoritProvider
from forum_auto import ForumAutoProvider
from mikado import MikadoProvider
from offer_normalizer import is_requested_part, normalize_offer
from order_quantity import quantity_from_item
from order_store import OrderStore
from pr_lg import PrLgProvider
from pricing import DEFAULT_MARKUP_RULES, calculate_sale_price
from provider_adapter import ProviderAdapter
from provider_health import ProviderCircuitBreaker
from provider_result_cache import ProviderResultCache
from result_limiter import DEFAULT_PROVIDER_LIMITS, normalize_provider_limits
from rossko import RosskoProvider
from tiss_tmparts import TissTmpartsProvider
from tradesoft import TradesoftProvider
from url_csv_provider import UrlCsvProvider

PROVIDER_DISPLAY_NAMES = {
    "PrLgProvider": "Profit-League",
    "FavoritProvider": "Фаворит",
    "ArmtekProvider": "Armtek",
    "ForumAutoProvider": "Forum-Auto",
    "MikadoProvider": "Mikado",
    "AbstdProvider": "ABSTD",
    "RosskoProvider": "Rossko",
    "AvtotoProvider": "Avtoto",
    "TissTmpartsProvider": "TISS",
    "TradesoftProvider": "Автоформула",
    "UrlCsvProvider": "Freno CSV",
    "AbcpReferenceProvider": "ABCP справочник",
}

CROSS_RELATION_CODES = {"analog", "cross", "replacement", "unconfirmed_cross"}
ORDER_VERIFICATION_TTL_SECONDS = 0
ORDER_RECHECK_WORKERS = 8
PROCESSED_SUBMIT_STATUSES = ("submitted", "skipped", "unknown")
DRAFT_GROUP_OFFER_LIMIT = 50


def _abcp_supplier_configured(cfg):
    cfg = cfg or {}
    return bool(cfg.get("enabled", False) and cfg.get("host") and cfg.get("login") and cfg.get("password"))


def _tradesoft_configured(cfg):
    cfg = cfg or {}
    return bool(
        cfg.get("enabled", False)
        and cfg.get("user")
        and cfg.get("password")
        and cfg.get("provider_login")
        and cfg.get("provider_password")
    )


def default_settings():
    default = {
        "profit_league": {"api_key": "", "timeout": 12, "create_order": True, "order_method": "", "order_payment": "", "order_point": "", "order_address": "", "order_pickup_point": "", **DEFAULT_PROVIDER_LIMITS},
        "favorit": {"enabled": False, "api_key": "", "developer_key": "", "trade_point": "", "payment_type": "", "delivery_type": "", "transport_type": "", "include_analogues": True, "timeout": 12, **DEFAULT_PROVIDER_LIMITS},
        "armtek": {"login": "", "password": "", "vkorg": "", "kunnr": "", "kunnr_we": "", "kunnr_za": "", "use_pickup": False, "incoterms": "", "parnr": "", "vbeln": "", "allowed_warehouses": "MOV0045404,MOV0006959,MOV0007296,MOV0006942,MOV0006930,MOV0007276", "timeout": 12, **DEFAULT_PROVIDER_LIMITS},
        "forum_auto": {"login": "", "password": "", "include_crosses": True, "external_order_id": "", "allowed_warehouses": "MSK,MSK-CD,RST,CRI", "timeout": 10, **DEFAULT_PROVIDER_LIMITS},
        "mikado": {"login": "", "password": "", "timeout": 10, **DEFAULT_PROVIDER_LIMITS},
        "abstd": {"login": "", "password": "", "agreement_id": "", "cart_id": "0", "delivery_address_id": "", "delivery_type_id": "1", "allowed_warehouses": "ABS MSK,ABS Sochi,ABS VLD,ABS STV,ABS Adler,ABS PTG,ABS RND,ABS KRD,ABS KRYM", "timeout": 10, **DEFAULT_PROVIDER_LIMITS},
        "rossko": {"key1": "", "key2": "", "delivery_id": "000000001", "address_id": "", "payment_id": "1", "requisite_id": "", "contact_name": "", "contact_phone": "", "allowed_warehouses": "HST179720381,HST224,HST142374868,HST588592306,HST834", "timeout": 15, **DEFAULT_PROVIDER_LIMITS},
        "avtoto": {"client_id": "", "login": "", "password": "", "include_crosses": True, "excluded_warehouses": "Эмираты,США", "timeout": 20, "warehouse_extra_days": "", **DEFAULT_PROVIDER_LIMITS},
        "tiss_tmparts": {"enabled": False, "api_key": "", "legal_organization_id": "", "legal_organization_id_label": "", "contract_id": "", "contract_id_label": "", "outlet_id": "", "outlet_id_label": "", "warehouse_mode": 0, "allowed_warehouses": "", "include_analogues": True, "delivery_type": "ToOutlet", "phone_number": "", "one_time_delivery": True, "not_group_reserves": False, "express_delivery": False, "timeout": 20, **DEFAULT_PROVIDER_LIMITS},
        "tradesoft": {"enabled": False, "user": "", "password": "", "provider_id": "AVTOFORMULA", "provider_login": "", "provider_password": "", "include_analogues": True, "timeout": 20, "warehouse_extra_days": "", **DEFAULT_PROVIDER_LIMITS},
        "url_csv": {"enabled": False, "name": "Прайс-листы", "urls": ["https://chatbots.freno.ru/soft/1C/price_rests.csv", "https://chatbots.freno.ru/soft/1C/price_rests_por.csv"], "timeout": 20, "cache_hours": 24, "column_map": {"article": "Артикул", "brand": "Производитель", "name": "Номенклатура", "price": "Цена", "quantity": "Остаток"}, "default_days": 0, "url_profiles": {"https://chatbots.freno.ru/soft/1C/price_rests.csv": {"name": "Freno основной", "enabled": True, "default_days": 0}, "https://chatbots.freno.ru/soft/1C/price_rests_por.csv": {"name": "Freno заказной", "enabled": True, "default_days": 0}}, **DEFAULT_PROVIDER_LIMITS},
        "abcp_reference": {"enabled": False, "host": "", "login": "", "password": "", "include_crosses": False, "include_images": False, "allow_articles_info": False, "articles_info_daily_limit": 8, "brand_reference_path": "", "timeout": 8},
        "default_markup": 0,
        "markup_rules": DEFAULT_MARKUP_RULES,
        "rounding_mode": "rubles_10",
        "rounding_from": 0,
        "rounding_to": 10000000,
        "default_comment": "",
        "expected_ip": "",
        "hide_no_return": False,
        "max_crosses": 0,
        # Запас должен перекрывать заказ целиком: каждая строка кладёт по
        # ответу на поставщика, а прежние 120 записей вытеснялись за ~12 строк.
        "provider_cache": {"enabled": True, "ttl_seconds": 3600, "max_entries": 2000},
        "first_run": True
    }
    return default


class _ProgressList(list):
    """Список итогов по поставщикам, который сообщает о каждом новом итоге (для прогресса в вебе)."""

    def __init__(self, callback=None):
        super().__init__()
        self._callback = callback

    def append(self, item):
        super().append(item)
        if self._callback:
            try:
                self._callback(dict(item))
            except Exception:
                pass  # сбой показа прогресса не должен ломать поиск


class ProcurementEngine:
    def __init__(self, settings=None, *, brand_aliases=None, cross_store=None, detailed_logger=None,
                 log=None, providers=None, order_store=None, order_history=None):
        self.settings = dict(settings or default_settings())
        self.brand_aliases = brand_aliases or BrandAliasResolver()
        self.cross_store = cross_store
        self.detailed_logger = detailed_logger
        self._log_callback = log
        self.provider_result_cache = ProviderResultCache()
        self.provider_circuit = ProviderCircuitBreaker(threshold=5, cooldown_seconds=45)
        self._order_file_search_id = 0
        self._selected_brand_variants = {}
        self.on_provider_progress = None
        self.on_order_action = None
        self.order_store = order_store
        self.order_history = order_history
        self.configure(self.settings)
        if providers is not None:
            self.providers = list(providers)

    def _log(self, text):
        if self._log_callback:
            self._log_callback(text)

    # Заменители методов окна десктопа: лог, обновление страницы заказов, подсказки артикулов.
    def add_log(self, text):
        self._log(text)

    def _refresh_orders_page(self):
        pass

    def _refresh_article_suggestion_orders(self, orders):
        pass

    def _order_action_finished(self, order, message):
        self._log(message)
        if self.on_order_action:
            self.on_order_action(order, message)

    def search_order_row(self, row, options=None):
        """Подбор одной строки заказа: то же, что кнопка «Файл заказа» в десктопе."""
        self._order_file_search_id += 1
        return self._search_order_file_row(row, options or {}, search_id=self._order_file_search_id)

    def configure(self, cfg):
        default = default_settings()
        p = cfg.get("profit_league", {})
        favorit = cfg.get("favorit", {})
        ar = cfg.get("armtek", {})
        fa = cfg.get("forum_auto", {})
        mk = cfg.get("mikado", {})
        ab = cfg.get("abstd", {})
        ro = cfg.get("rossko", {})
        av = cfg.get("avtoto", {})
        tiss = cfg.get("tiss_tmparts", {})
        ts = cfg.get("tradesoft", {})
        abcp_suppliers = [
            entry for entry in (cfg.get("abcp_suppliers") or [])
            if isinstance(entry, dict) and str(entry.get("name") or "").strip()
        ]
        url_csv = cfg.get("url_csv", {})
        abcp_ref = cfg.get("abcp_reference", {})
        cache_cfg = cfg.get("provider_cache", {}) if isinstance(cfg.get("provider_cache", {}), dict) else {}
        cache_enabled = bool(cache_cfg.get("enabled", default["provider_cache"]["enabled"]))
        try:
            cache_ttl = int(cache_cfg.get("ttl_seconds", default["provider_cache"]["ttl_seconds"]) or 0)
        except (TypeError, ValueError):
            cache_ttl = default["provider_cache"]["ttl_seconds"]
        try:
            cache_size = int(cache_cfg.get("max_entries", default["provider_cache"]["max_entries"]) or 0)
        except (TypeError, ValueError):
            cache_size = default["provider_cache"]["max_entries"]
        if hasattr(self, "provider_result_cache"):
            self.provider_result_cache.configure(
                ttl_seconds=cache_ttl if cache_enabled else 0,
                max_entries=cache_size if cache_enabled else 0,
            )
        self.hide_no_return = bool(cfg.get("hide_no_return", False))
        self.profit_league_create_order = True
        try:
            max_crosses_value = int(cfg.get("max_crosses", 0) or 0)
        except (TypeError, ValueError):
            max_crosses_value = 0
        # Старый дефолт был 200. После перехода на пакетный SQLite-поиск
        # он больше не нужен, поэтому считаем старое значение отсутствием лимита.
        self.max_crosses = 0 if max_crosses_value == 200 else max(0, max_crosses_value)
        self.provider_filters = {
            "ABSTD": {"allowed": self._parse_filter_list(ab.get("allowed_warehouses", default["abstd"].get("allowed_warehouses", ""))), "excluded": []},
            "Armtek": {"allowed": self._parse_filter_list(ar.get("allowed_warehouses", default["armtek"].get("allowed_warehouses", ""))), "excluded": []},
            "Rossko": {"allowed": self._parse_filter_list(ro.get("allowed_warehouses", default["rossko"].get("allowed_warehouses", ""))), "excluded": []},
            "Forum-Auto": {"allowed": self._parse_filter_list(fa.get("allowed_warehouses", default["forum_auto"].get("allowed_warehouses", ""))), "excluded": []},
            "Avtoto": {"allowed": [], "excluded": self._parse_filter_list(av.get("excluded_warehouses", default["avtoto"].get("excluded_warehouses", "")))},
            "TISS": {"allowed": self._parse_filter_list(tiss.get("allowed_warehouses", default["tiss_tmparts"].get("allowed_warehouses", ""))), "excluded": []},
        }
        self.warehouse_extra_days = {
            "Profit-League": self._parse_warehouse_days(p.get("warehouse_extra_days", "")),
            "Фаворит": self._parse_warehouse_days(favorit.get("warehouse_extra_days", "")),
            "Armtek": self._parse_warehouse_days(ar.get("warehouse_extra_days", "")),
            "Forum-Auto": self._parse_warehouse_days(fa.get("warehouse_extra_days", "")),
            "Mikado": self._parse_warehouse_days(mk.get("warehouse_extra_days", "")),
            "ABSTD": self._parse_warehouse_days(ab.get("warehouse_extra_days", "")),
            "Rossko": self._parse_warehouse_days(ro.get("warehouse_extra_days", "")),
            "Avtoto": self._parse_warehouse_days(av.get("warehouse_extra_days", "")),
            "TISS": self._parse_warehouse_days(tiss.get("warehouse_extra_days", "")),
            "Автоформула": self._parse_warehouse_days(ts.get("warehouse_extra_days", "")),
            **{
                str(entry["name"]).strip(): self._parse_warehouse_days(entry.get("warehouse_extra_days", ""))
                for entry in abcp_suppliers
            },
            "Freno CSV": self._parse_warehouse_days(url_csv.get("warehouse_extra_days", "")),
        }
        self.provider_result_limits = {
            "Profit-League": normalize_provider_limits(p),
            "Фаворит": normalize_provider_limits(favorit),
            "Armtek": normalize_provider_limits(ar),
            "Forum-Auto": normalize_provider_limits(fa),
            "Mikado": normalize_provider_limits(mk),
            "ABSTD": normalize_provider_limits(ab),
            "Rossko": normalize_provider_limits(ro),
            "Avtoto": normalize_provider_limits(av),
            "TISS": normalize_provider_limits(tiss),
            "Автоформула": normalize_provider_limits(ts),
            **{str(entry["name"]).strip(): normalize_provider_limits(entry) for entry in abcp_suppliers},
            "Freno CSV": normalize_provider_limits(url_csv),
        }
        self.providers = []
        self.reference_sources = []
        if (
            abcp_ref.get("enabled", False)
            and abcp_ref.get("host")
            and abcp_ref.get("login")
            and abcp_ref.get("password")
        ):
            self.reference_sources.append(
                AbcpReferenceProvider(
                    abcp_ref.get("host", ""),
                    abcp_ref.get("login", ""),
                    abcp_ref.get("password", ""),
                    include_crosses=abcp_ref.get("include_crosses", False),
                    include_images=abcp_ref.get("include_images", False),
                    allow_articles_info=abcp_ref.get("allow_articles_info", False),
                    articles_info_daily_limit=abcp_ref.get("articles_info_daily_limit", 8),
                    timeout=int(abcp_ref.get("timeout") or 8),
                )
            )
        if p.get("enabled", True) and p.get("api_key"):
            self.providers.append(
                PrLgProvider(
                    api_key=p.get("api_key", ""),
                    timeout=int(p.get("timeout") or 12),
                    create_order=True,
                    order_method=p.get("order_method", ""),
                    order_payment=p.get("order_payment", ""),
                    order_point=p.get("order_point", ""),
                    order_address=p.get("order_address", ""),
                    order_pickup_point=p.get("order_pickup_point", ""),
                )
            )
        if favorit.get("enabled", False) and favorit.get("api_key"):
            self.providers.append(
                FavoritProvider(
                    favorit.get("api_key", ""),
                    favorit.get("include_analogues", True),
                    timeout=int(favorit.get("timeout") or 12),
                    developer_key=favorit.get("developer_key", ""),
                    trade_point=favorit.get("trade_point", ""),
                    payment_type=favorit.get("payment_type", ""),
                    delivery_type=favorit.get("delivery_type", ""),
                    transport_type=favorit.get("transport_type", ""),
                    brand_aliases=self.brand_aliases,
                )
            )
        if ar.get("enabled", True) and ar.get("login") and ar.get("password") and ar.get("vkorg") and ar.get("kunnr"):
            self.providers.append(
                ArmtekProvider(
                    ar["login"],
                    ar["password"],
                    ar["vkorg"],
                    ar["kunnr"],
                    ar.get("kunnr_we", ""),
                    ar.get("kunnr_za", ""),
                    ar.get("incoterms", ""),
                    ar.get("parnr", ""),
                    ar.get("vbeln", ""),
                    ar.get("use_pickup", False),
                    timeout=int(ar.get("timeout") or 12),
                )
            )
        if fa.get("enabled", True) and fa.get("login") and fa.get("password"):
            self.providers.append(
                ForumAutoProvider(
                    fa["login"],
                    fa["password"],
                    fa.get("include_crosses", True),
                    timeout=int(fa.get("timeout") or 10),
                    external_order_id=fa.get("external_order_id", ""),
                )
            )
        if mk.get("enabled", True) and mk.get("login") and mk.get("password"):
            self.providers.append(
                MikadoProvider(mk["login"], mk["password"], timeout=int(mk.get("timeout") or 10))
            )
        if ab.get("enabled", True) and ab.get("login") and ab.get("password") and ab.get("agreement_id"):
            self.providers.append(
                AbstdProvider(
                    ab["login"],
                    ab["password"],
                    ab["agreement_id"],
                    ab.get("cart_id", "0"),
                    ab.get("delivery_address_id", ""),
                    ab.get("delivery_type_id", "1"),
                    timeout=int(ab.get("timeout") or 10),
                )
            )
        if ro.get("enabled", True) and ro.get("key1") and ro.get("key2") and ro.get("delivery_id"):
            self.providers.append(
                RosskoProvider(
                    ro.get("key1", ""),
                    ro.get("key2", ""),
                    ro.get("delivery_id", "000000001"),
                    ro.get("address_id", ""),
                    ro.get("payment_id", "1"),
                    ro.get("requisite_id", ""),
                    ro.get("contact_name", ""),
                    ro.get("contact_phone", ""),
                    timeout=int(ro.get("timeout") or 15),
                )
            )
        if av.get("enabled", True) and av.get("client_id") and av.get("login") and av.get("password"):
            self.providers.append(
                AvtotoProvider(
                    av.get("client_id", ""),
                    av.get("login", ""),
                    av.get("password", ""),
                    av.get("include_crosses", True),
                    timeout=int(av.get("timeout") or 10),
                )
            )
        if tiss.get("enabled", False) and tiss.get("api_key"):
            self.providers.append(
                TissTmpartsProvider(
                    tiss.get("api_key", ""),
                    tiss.get("warehouse_mode", 0),
                    timeout=int(tiss.get("timeout") or 6),
                    legal_organization_id=tiss.get("legal_organization_id", ""),
                    contract_id=tiss.get("contract_id", ""),
                    outlet_id=tiss.get("outlet_id", ""),
                    warehouses=tiss.get("allowed_warehouses", ""),
                    include_analogues=tiss.get("include_analogues", True),
                    delivery_type=tiss.get("delivery_type", "ToOutlet"),
                    phone_number=tiss.get("phone_number", ""),
                    one_time_delivery=tiss.get("one_time_delivery", True),
                    not_group_reserves=tiss.get("not_group_reserves", False),
                    express_delivery=tiss.get("express_delivery", False),
                )
            )
        if _tradesoft_configured(ts):
            self.providers.append(
                TradesoftProvider(
                    ts.get("user", ""),
                    ts.get("password", ""),
                    provider_id=ts.get("provider_id") or "AVTOFORMULA",
                    provider_login=ts.get("provider_login", ""),
                    provider_password=ts.get("provider_password", ""),
                    timeout=int(ts.get("timeout") or 10),
                    include_analogues=ts.get("include_analogues", True),
                )
            )
        self.abcp_supplier_names = []
        for entry in abcp_suppliers:
            name = str(entry["name"]).strip()
            cls = abcp_supplier_class(name)
            # Программа различает поставщиков по имени класса: у каждого ABCP-поставщика свой.
            PROVIDER_DISPLAY_NAMES[cls.__name__] = name
            self.abcp_supplier_names.append(name)
            if _abcp_supplier_configured(entry):
                self.providers.append(
                    cls(
                        entry.get("host", ""),
                        entry.get("login", ""),
                        entry.get("password", ""),
                        payment_method=entry.get("payment_method", ""),
                        shipment_method=entry.get("shipment_method", ""),
                        shipment_address=entry.get("shipment_address", "0"),
                        shipment_office=entry.get("shipment_office", ""),
                        include_analogues=entry.get("include_analogues", True),
                        timeout=int(entry.get("timeout") or 10),
                    )
                )
        if url_csv.get("enabled", False):
            urls = url_csv.get("urls") or []
            name = url_csv.get("name") or "Freno CSV"
            timeout = int(url_csv.get("timeout") or 20)
            column_map = url_csv.get("column_map") or {}
            default_days = url_csv.get("default_days") or "0,0"
            url_profiles = url_csv.get("url_profiles") or {}
            provider = UrlCsvProvider(
                urls=urls,
                name=name,
                timeout=timeout,
                column_map=column_map,
                default_days=default_days,
                url_profiles=url_profiles,
                cache_hours=int(url_csv.get("cache_hours") or 24),
                runtime_refresh=False,
            )
            if provider.urls:
                self.providers.append(provider)
        self.current_markup = cfg.get("default_markup", 0)
        self.markup_rules = cfg.get("markup_rules") or DEFAULT_MARKUP_RULES
        self.rounding_mode = cfg.get("rounding_mode", "rubles_10")
        self.rounding_from = float(cfg.get("rounding_from", 0) or 0)
        self.rounding_to = float(cfg.get("rounding_to", 10000000) or 10000000)
        self.expected_ip = cfg.get("expected_ip", "")
        self.markup_enabled = bool(self.markup_rules) or float(self.current_markup or 0) > 0

    def _search_order_file_row(self, row, options, search_id=None):
        row = dict(row or {})
        options = dict(options or {})
        raw_article = str(row.get("article") or "").strip()
        clean_article = self.clean_num(raw_article)
        brand = str(row.get("brand") or "").strip()
        quantity = max(1, int(row.get("quantity") or 1))
        exact_match = bool(options.get("exact_match", True))
        if not brand:
            return self._order_file_result(
                row,
                "needs_review",
                "Требуется проверка",
                "в файле не указан бренд",
                [],
                None,
                quantity,
            )
        include_no_return = bool(options.get("include_no_return", False))
        ignore_warehouse_filters = bool(options.get("ignore_warehouse_filters", False))
        offers, provider_stats = self._query_order_file_offers(
            raw_article,
            clean_article,
            brand,
            quantity,
            include_no_return=include_no_return,
            exact_match=exact_match,
            ignore_warehouse_filters=ignore_warehouse_filters,
            search_id=search_id,
        )
        self._record_cross_pairs(brand, raw_article, offers)
        exact, stats = filter_exact_offers(
            {**row, "article_key": clean_article},
            offers,
            self.clean_num,
            self.same_brand_group,
            exact_match=exact_match,
        )
        selectable, reject_stats = self._order_file_selectable_offers(exact, quantity)
        max_days = options.get("max_days")
        max_hours = int(max_days) * 24 if max_days else None
        ranked_selectable = rank_offers(
            selectable,
            options.get("strategy") or "price",
            max_delivery_hours=max_hours,
        )
        selected = ranked_selectable[0] if ranked_selectable else None
        limit_applied = bool(max_hours is not None and selectable and not selected)
        code, status = status_for_selection(
            row,
            stats.get("exact_count", 0),
            len(selectable),
            selected,
            brand_missing=False,
            limit_applied=limit_applied,
        )
        if selected and not exact_match:
            selected_article = self.clean_num(selected.get("article") or selected.get("original_article") or "")
            selected_brand = str(selected.get("brand") or selected.get("normalized_brand") or "").strip()
            if (
                selected.get("is_cross")
                or selected_article != clean_article
                or (selected_brand and not self.same_brand_group(selected_brand, brand))
            ):
                status = "Подобран вариант"
        reason = self._order_file_reason(
            stats,
            reject_stats,
            provider_stats,
            limit_applied,
            exact_match=exact_match,
            selectable_count=len(selectable),
        )
        if selected:
            info = self._quantity_info(selected, quantity)
            quantity = info.actual_int()
            selected = dict(selected)
            selected["requested_quantity"] = int(info.requested_quantity)
            selected["actual_order_quantity"] = quantity
            selected["source_brand"] = brand
            selected["source_code"] = raw_article
            selected["source_name"] = str(row.get("name") or selected.get("source_name") or selected.get("name") or "")
            selected["order_file_exact_match"] = exact_match
            # Стратегия подбора едет вместе с предложением: перепроверка обязана
            # выбирать по тому же правилу, иначе заказ, собранный по цене,
            # молча пересобирается по сроку.
            selected["selection_strategy"] = str(options.get("strategy") or "price")
            selected["selection_max_days"] = options.get("max_days")
            self._mark_offer_checked_now(selected, "проверено при поиске из файла")
            self._apply_sale_price(selected)
        self._detail_log(
            "order_file_row_finish",
            article=raw_article,
            clean_article=clean_article,
            brand=brand,
            brand_key=getattr(self, "brand_group_key", lambda value: value)(brand),
            quantity=row.get("quantity"),
            exact_match=exact_match,
            include_no_return=include_no_return,
            selection_strategy=options.get("strategy") or "price",
            max_days=options.get("max_days"),
            status=code,
            raw_count=stats.get("raw_count"),
            exact_count=stats.get("exact_count"),
            accepted_count=stats.get("accepted_count"),
            selectable_count=len(selectable),
            selected_provider=(selected or {}).get("provider_name") or (selected or {}).get("provider"),
            selected_offer=(self._offer_log_sample([selected], 1)[0] if selected else None),
            selectable_sample=self._offer_log_sample(ranked_selectable, 8),
            offers_sample=self._offer_log_sample(offers, 8),
            filter_samples=stats.get("samples"),
            provider_stats=provider_stats,
            reject_stats=reject_stats,
        )
        return self._order_file_result(
            row,
            code,
            status,
            reason,
            ranked_selectable,
            selected,
            quantity,
        )

    def _query_order_file_offers(self, raw_article, clean_article, brand, quantity, *, include_no_return=False, exact_match=True, ignore_warehouse_filters=False, search_id=None):
        providers = list(getattr(self, "providers", []) or [])
        if not providers:
            return [], []
        offers = []
        provider_stats = _ProgressList(getattr(self, "on_provider_progress", None))
        executor = ThreadPoolExecutor(max_workers=max(1, len(providers)))
        started_at = time.monotonic()
        futures = {}
        try:
            for provider in providers:
                if not self._order_file_search_is_current(search_id):
                    break
                cls_name = provider.__class__.__name__
                display_name = self._provider_display_name(cls_name)
                cart_state, _ = self._provider_cart_state(display_name)
                if cart_state == "local":
                    provider_stats.append({
                        "provider": display_name,
                        "status": "local_skipped",
                        "reason": "локальный прайс не участвует в заказе из файла",
                        "count": 0,
                        "raw_count": 0,
                        "elapsed": 0,
                        "cache_hit": False,
                        "message": "",
                        "sample": [],
                    })
                    continue
                allowed, remaining = self.provider_circuit.allow(display_name)
                if not allowed:
                    provider_stats.append({
                        "provider": display_name,
                        "status": "skipped",
                        "reason": f"временно отключён, повтор через {self._format_seconds(remaining)}",
                        "count": 0,
                    })
                    continue
                query_kwargs = {}
                if cls_name == "AvtotoProvider" and exact_match:
                    query_kwargs["include_crosses"] = False
                future = executor.submit(
                    self._query_provider,
                    provider,
                    cls_name,
                    raw_article,
                    clean_article,
                    brand,
                    # У каждой строки файла свой бренд. Без этого подставлялся
                    # бренд, выбранный в последнем обычном поиске, и весь заказ
                    # уходил поставщикам под чужим именем производителя.
                    use_selected_variants=False,
                    **query_kwargs,
                )
                futures[future] = (provider, cls_name, display_name)
            pending = set(futures)
            deadlines = {
                future: started_at + self._provider_timeout(provider)
                for future, (provider, _, _) in futures.items()
            }
            while pending:
                if not self._order_file_search_is_current(search_id):
                    for future in list(pending):
                        future.cancel()
                        provider, _, display_name = futures[future]
                        provider_stats.append({
                            "provider": display_name,
                            "status": "cancelled",
                            "reason": "поиск остановлен",
                            "count": 0,
                        })
                    pending.clear()
                    break
                now = time.monotonic()
                timed_out = [future for future in pending if now >= deadlines[future]]
                for future in timed_out:
                    pending.remove(future)
                    provider, _, display_name = futures[future]
                    future.cancel()
                    provider_stats.append({
                        "provider": display_name,
                        "status": "timeout",
                        "reason": "таймаут",
                        "count": 0,
                    })
                    self.provider_circuit.record_failure(display_name, "таймаут")
                if not pending:
                    break
                wait_for = max(0.0, min(deadlines[future] for future in pending) - time.monotonic())
                done, _ = wait(pending, timeout=wait_for, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.remove(future)
                    provider, cls_name, display_name = futures[future]
                    try:
                        rows, elapsed, cache_hit = future.result()
                    except Exception as exc:
                        provider_stats.append({
                            "provider": display_name,
                            "status": "error",
                            "reason": str(exc)[:160],
                            "count": 0,
                        })
                        self.provider_circuit.record_failure(display_name, str(exc))
                        continue
                    rows = rows or []
                    prepared = []
                    skipped_warehouse = 0
                    skipped_no_return = 0
                    for item in rows:
                        item = dict(item or {})
                        if not item.get("article"):
                            item["article"] = raw_article
                        self.normalize_item_article(item)
                        self._apply_offer_relation(
                            item, clean_article, brand, raw_article, display_name
                        )
                        self.normalize_offer_item(item, quantity)
                        self._apply_warehouse_extra_days(display_name, item)
                        allowed, reason = self._filter_provider_item(
                            display_name,
                            item,
                            include_no_return=include_no_return,
                            ignore_warehouse_filters=ignore_warehouse_filters,
                        )
                        if not allowed:
                            if reason == "без возврата":
                                skipped_no_return += 1
                            else:
                                skipped_warehouse += 1
                            continue
                        prepared.append(item)
                    offers.extend(prepared)
                    provider_stats.append({
                        "provider": display_name,
                        "status": "ok" if prepared else "empty",
                        "count": len(prepared),
                        "raw_count": len(rows),
                        "elapsed": round(float(elapsed or 0), 3),
                        "cache_hit": bool(cache_hit),
                        "message": getattr(provider, "last_message", ""),
                        "skipped_warehouse": skipped_warehouse,
                        "skipped_no_return": skipped_no_return,
                        "sample": self._offer_log_sample(prepared, 5),
                    })
                    if prepared:
                        self.provider_circuit.record_success(display_name)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return offers, provider_stats

    def _order_file_search_is_current(self, search_id):
        if search_id is None:
            return True
        return int(search_id or 0) == int(getattr(self, "_order_file_search_id", 0) or 0)

    def _order_file_selectable_offers(self, offers, quantity):
        selectable = []
        stats = {
            "local": 0,
            "disabled": 0,
            "missing_fields": 0,
            "quantity": 0,
            "requested_quantity": int(quantity or 1),
            "max_available_quantity": 0,
            "quantity_providers": {},
            "missing_fields_map": {},
            "samples": {
                "quantity": [],
                "missing_fields": [],
                "local": [],
                "disabled": [],
            },
        }
        for offer in offers or []:
            offer = dict(offer or {})
            info = self._quantity_info(offer, quantity)
            if not info.can_order:
                stats["quantity"] += 1
                available = info.available_int() if info.available_quantity is not None else 0
                stats["max_available_quantity"] = max(stats["max_available_quantity"], available)
                provider_name = str(offer.get("provider_name") or offer.get("provider") or "")
                if provider_name:
                    stats["quantity_providers"][provider_name] = stats["quantity_providers"].get(provider_name, 0) + 1
                self._add_order_file_reject_sample(stats, "quantity", offer, info.reason)
                continue
            offer["actual_order_quantity"] = info.actual_int()
            offer["requested_quantity"] = int(info.requested_quantity)
            provider_name = str(offer.get("provider_name") or offer.get("provider") or "")
            state, _ = self._provider_cart_state(provider_name)
            if state == "local":
                stats["local"] += 1
                self._add_order_file_reject_sample(stats, "local", offer, "локальный прайс")
                continue
            if state == "disabled":
                stats["disabled"] += 1
                self._add_order_file_reject_sample(stats, "disabled", offer, "корзина отключена")
                continue
            missing = self._missing_cart_fields(provider_name, offer)
            if missing:
                stats["missing_fields"] += 1
                for field in missing:
                    stats["missing_fields_map"][field] = stats["missing_fields_map"].get(field, 0) + 1
                self._add_order_file_reject_sample(stats, "missing_fields", offer, "нет данных: " + ", ".join(missing))
                continue
            self._apply_sale_price(offer)
            selectable.append(offer)
        return selectable, stats

    def _add_order_file_reject_sample(self, stats, bucket, offer, reason=""):
        samples = (stats or {}).get("samples") or {}
        rows = samples.get(bucket)
        if rows is None or len(rows) >= 5:
            return
        sample = self._offer_log_sample([offer], 1)
        if sample:
            row = dict(sample[0])
            row["reject_reason"] = str(reason or "")
            rows.append(row)

    def _apply_sale_price(self, item):
        try:
            purchase_price = float(item.get("purchase_price", item.get("price") or 0) or 0)
        except (TypeError, ValueError):
            purchase_price = 0.0
        sale_price, _, _ = self.apply_markup(purchase_price)
        item["purchase_price"] = purchase_price
        item["price"] = purchase_price
        item["sale_price"] = sale_price
        item["order_sale_price"] = sale_price
        item["display_brand"] = item.get("display_brand") or self.display_brand_name(item.get("brand"))
        return item

    def _order_file_result(self, row, code, status, reason, alternatives, selected, quantity):
        return {
            "source": dict(row or {}),
            "status_code": code,
            "status": status,
            "reason": reason,
            "offer": selected,
            "alternatives": list(alternatives or []),
            "quantity": int(quantity or 1),
        }

    def _order_file_reason(self, stats, reject_stats, provider_stats, limit_applied, *, exact_match=True, selectable_count=0):
        if limit_applied:
            return "предложения есть, но они дольше указанного срока"
        exact_count = int((stats or {}).get("exact_count") or 0)
        accepted_count = int((stats or {}).get("accepted_count") or 0)
        selectable_count = int(selectable_count or 0)
        raw_count = int((stats or {}).get("raw_count") or 0)
        if not exact_match and selectable_count > 0:
            return (
                f"подходящих предложений: {accepted_count}; "
                f"точных {exact_count}, аналоги {stats.get('cross', 0)}"
            )
        if exact_match and selectable_count > 0:
            return f"точных предложений: {exact_count}"
        if exact_count <= 0:
            if raw_count:
                prefix = "без точного бренда/артикула" if exact_match else "без подходящего варианта"
                return (
                    f"получено {raw_count}, но {prefix}; "
                    f"чужой бренд {stats.get('wrong_brand', 0)}, другой артикул {stats.get('wrong_article', 0)}, "
                    f"аналоги {stats.get('cross', 0)}"
                )
            errors = [
                f"{item.get('provider')}: {item.get('reason')}"
                for item in provider_stats or []
                if item.get("status") in ("error", "timeout", "skipped") and item.get("reason")
            ]
            return "; ".join(errors[:3]) if errors else "поставщики не вернули предложений"
        details = []
        quantity_rejects = int((reject_stats or {}).get("quantity") or 0)
        if quantity_rejects:
            requested = int((reject_stats or {}).get("requested_quantity") or 0)
            max_available = int((reject_stats or {}).get("max_available_quantity") or 0)
            if requested and max_available:
                details.append(
                    f"не хватает остатка одной строкой: {quantity_rejects}, нужно {requested}, максимум {max_available}"
                )
            else:
                details.append(f"не проходит нужное количество: {quantity_rejects}")
        missing_fields = int((reject_stats or {}).get("missing_fields") or 0)
        if missing_fields:
            field_counts = (reject_stats or {}).get("missing_fields_map") or {}
            if field_counts and set(field_counts) == {"part_id"}:
                details.append(
                    f"Avtoto нашёл точное предложение, но не вернул part_id для API-заказа: {missing_fields}"
                )
            else:
                fields = ", ".join(
                    f"{name} {count}" for name, count in sorted(field_counts.items())[:4]
                )
                details.append(
                    f"не хватает данных для заказа: {missing_fields}" + (f" ({fields})" if fields else "")
                )
        local_count = int((reject_stats or {}).get("local") or 0)
        if local_count:
            details.append(f"локальные прайсы не участвуют: {local_count}")
        disabled_count = int((reject_stats or {}).get("disabled") or 0)
        if disabled_count:
            details.append(f"корзина поставщика отключена: {disabled_count}")
        if details:
            return f"точных предложений: {exact_count}; " + "; ".join(details[:3])
        if not exact_match:
            return f"подходящих предложений: {accepted_count}"
        return f"точных предложений: {exact_count}"

    def _prepared_order_file_entry(self, row):
        item = dict((row or {}).get("offer") or {})
        source = dict((row or {}).get("source") or {})
        if source.get("name"):
            item["source_name"] = str(source.get("name") or "")
        requested_quantity = int(
            source.get("quantity")
            or item.get("requested_quantity")
            or (row or {}).get("quantity")
            or item.get("actual_order_quantity")
            or 1
        )
        info = self._quantity_info(item, requested_quantity)
        if not info.can_order:
            return None, info.reason or "нельзя заказать нужное количество"
        quantity = info.actual_int()
        if not item.get("internal_offer_id"):
            self.normalize_offer_item(item, quantity)
        self._apply_sale_price(item)
        item["requested_quantity"] = int(info.requested_quantity)
        item["available_quantity"] = info.available_int() if info.available_quantity is not None else None
        item["minimum_quantity"] = int(info.minimum_quantity)
        item["quantity_step"] = int(info.quantity_step)
        item["actual_order_quantity"] = quantity
        return {
            "key": self._cart_key(item),
            "item": item,
            "qty": quantity,
            "stock": self._quantity_limit_for_cart(info),
        }, ""

    def _cart_key(self, item):
        if item.get("internal_offer_id"):
            return str(item.get("internal_offer_id"))
        return "|".join(
            str(value)
            for value in (
                item.get("provider") or "",
                self.brand_group_key(item.get("brand", "")),
                self.clean_num(item.get("article", "")),
                item.get("warehouse") or item.get("logo") or "",
                item.get("price") or "",
            )
        )

    def _order_entries_with_prices(self, entries):
        prepared = []
        for entry in entries or []:
            item = dict(entry.get("item") or {})
            qty = int(entry.get("qty") or item.get("actual_order_quantity") or 1)
            try:
                purchase_price = float(item.get("purchase_price", item.get("price") or 0) or 0)
            except (TypeError, ValueError):
                purchase_price = 0.0
            sale_price, _, _ = self.apply_markup(purchase_price)
            item["purchase_price"] = purchase_price
            item["sale_price"] = sale_price
            item["order_sale_price"] = sale_price
            item["display_brand"] = item.get("display_brand") or self.display_brand_name(item.get("brand"))
            item["order_purchase_total"] = round(purchase_price * qty, 2)
            item["order_sale_total"] = round(sale_price * qty, 2)
            item["order_margin"] = round((sale_price - purchase_price) * qty, 2)
            group_offers = []
            for offer in item.get("selection_group_offers") or []:
                offer = dict(offer or {})
                if not offer:
                    continue
                self._apply_sale_price(offer)
                group_offers.append(offer)
            if group_offers:
                item["selection_group_offers"] = group_offers
                item["selection_group_offer_count"] = len(group_offers)
                item["selection_group_visible_offer_count"] = sum(
                    1 for offer in group_offers if offer.get("display_in_order", True)
                )
            prepared.append({"key": entry.get("key"), "qty": qty, "item": item})
        return prepared

    def _mark_offer_checked_now(self, item, message="проверено при поиске"):
        checked_at = datetime.datetime.now().isoformat(timespec="seconds")
        item["verification_status"] = "valid"
        item["verification_message"] = str(message or "проверено при поиске")
        item["last_checked_at"] = checked_at
        return item

    def _entries_verified_at(self, entries):
        checked = []
        for entry in entries or []:
            item = dict((entry or {}).get("item") or {})
            if str(item.get("verification_status") or "") != "valid":
                return ""
            checked_at = str(item.get("last_checked_at") or "")
            if not checked_at or self._verification_expired(checked_at):
                return ""
            checked.append(checked_at)
        return min(checked) if checked else ""

    def _mark_order_summary_stale_if_expired(self, summary):
        summary = dict(summary or {})
        if (
            str(summary.get("status") or "") not in ("sent",)
            and str(summary.get("verification_status") or "") == "valid"
            and self._verification_expired(str(summary.get("verified_at") or ""))
        ):
            full_order = self.order_store.read(summary.get("order_id"))
            if full_order:
                full_order = self._mark_order_stale_if_expired(full_order, update_store=True)
                return self.order_store.summary_from_order(full_order)
        return summary

    def _mark_order_stale_if_expired(self, order, update_store=False):
        order = dict(order or {})
        if not order or str(order.get("status") or "") in ("sent",):
            return order
        if str(order.get("verification_status") or "") != "valid":
            return order
        verified_at = str(order.get("verified_at") or "")
        if not self._verification_expired(verified_at):
            return order
        for item in order.get("items") or []:
            if item.get("submit_status") in PROCESSED_SUBMIT_STATUSES:
                continue
            if str(item.get("verification_status") or "") == "valid":
                item["verification_status"] = "stale"
                item["verification_message"] = "снимок устарел, нужна перепроверка"
        order["verification_status"] = "stale"
        if update_store:
            return self.order_store.update(order)
        return order

    def _verification_expired(self, verified_at):
        if ORDER_VERIFICATION_TTL_SECONDS <= 0:
            return False
        if not verified_at:
            return True
        try:
            checked_at = datetime.datetime.fromisoformat(str(verified_at))
        except ValueError:
            return True
        age = (datetime.datetime.now() - checked_at).total_seconds()
        return age > ORDER_VERIFICATION_TTL_SECONDS

    def _order_submit_idempotency_key(self, order_id, item_indexes=None):
        order_id = str(order_id or "").strip()
        if item_indexes is None:
            suffix = "all"
        else:
            suffix = ",".join(str(index) for index in sorted({int(index) for index in item_indexes}))
        return f"{order_id}:{suffix}"

    def _try_lock_order_submit(self, order_id):
        order_id = str(order_id or "").strip()
        if not order_id:
            return False
        if not hasattr(self, "_submitting_order_ids"):
            self._submitting_order_ids = set()
        if order_id in self._submitting_order_ids:
            return False
        self._submitting_order_ids.add(order_id)
        return True

    def _unlock_order_submit(self, order_id):
        if hasattr(self, "_submitting_order_ids"):
            self._submitting_order_ids.discard(str(order_id or "").strip())

    def _submittable_order_items(self, order, item_indexes=None):
        order_verified = str((order or {}).get("verification_status") or "") == "valid"
        rows = []
        selected_indexes = set(int(index) for index in item_indexes) if item_indexes is not None else None
        for index, item in enumerate((order or {}).get("items") or []):
            if selected_indexes is not None and index not in selected_indexes:
                continue
            if item.get("submit_status") in PROCESSED_SUBMIT_STATUSES:
                continue
            item_verification = str(item.get("verification_status") or "")
            if item_verification == "valid" or (order_verified and not item_verification):
                rows.append(item)
        return rows

    def _provider_for_display_name(self, provider_name):
        for provider in getattr(self, "providers", []):
            if self._provider_display_name(provider.__class__.__name__) == provider_name:
                return provider
        return None

    def _order_item_skip_text(self, item):
        item = item or {}
        response = item.get("supplier_response") if isinstance(item.get("supplier_response"), dict) else {}
        return " ".join(
            str(part or "").lower()
            for part in (
                item.get("skip_reason"),
                item.get("verification_message"),
                response.get("error"),
            )
        )

    def _is_order_item_local_processed(self, item):
        return (
            str((item or {}).get("submit_status") or "") == "skipped"
            and "локальная позиция" in self._order_item_skip_text(item)
        )

    def _is_order_item_manual_excluded(self, item):
        return (
            str((item or {}).get("submit_status") or "") == "skipped"
            and not self._is_order_item_local_processed(item)
        )

    def _refresh_order_submit_status(self, order, not_ready_count=0):
        items = order.get("items") or []
        has_pending = any(
            item.get("submit_status") not in PROCESSED_SUBMIT_STATUSES
            for item in items
        )
        has_submitted = any(item.get("submit_status") == "submitted" for item in items)
        has_local = any(self._is_order_item_local_processed(item) for item in items)
        has_manual = any(self._is_order_item_manual_excluded(item) for item in items)
        # unknown требует ровно того же внимания, что и ошибка: пока оператор
        # не проверит заказ у поставщика, заказ закрытым считать нельзя.
        has_failed = any(item.get("submit_status") in ("failed", "unknown") for item in items)
        if has_failed:
            order["status"] = "partial" if (has_submitted or has_local) else "failed"
        elif has_pending or not_ready_count:
            order["status"] = "partial" if (has_submitted or has_local or has_manual) else "draft"
        elif has_manual:
            order["status"] = "partial" if (has_submitted or has_local) else "draft"
        elif has_submitted and not has_local:
            order["status"] = "sent"
        elif has_submitted or has_local:
            order["status"] = "processed"
        else:
            order["status"] = "draft"
        return order["status"]

    def _skip_order_item(self, order_id, item_index):
        order = self.order_store.read(order_id)
        if not order:
            self.add_log(f"ОШИБКА: заказ {order_id} не найден")
            return
        items = order.get("items") or []
        if not (0 <= int(item_index) < len(items)):
            self.add_log(f"ОШИБКА: позиция заказа {order_id} не найдена")
            return
        item = items[int(item_index)]
        if item.get("submit_status") == "submitted":
            self.add_log(f"{order_id}: уже отправленную позицию нельзя пропустить")
            return
        if item.get("submit_status") == "skipped":
            self.add_log(f"{order_id}: позиция уже пропущена")
            return
        skipped_at = datetime.datetime.now().isoformat(timespec="seconds")
        item["submit_status"] = "skipped"
        item["skip_reason"] = "manual_excluded"
        item["skipped_at"] = skipped_at
        item["verification_message"] = "пропущено вручную"
        order["provider_results"] = order.get("provider_results") or []
        order["provider_results"].append({
            "type": "manual_skip",
            "created_at": skipped_at,
            "internal_offer_id": item.get("internal_offer_id"),
            "supplier_offer_id": item.get("supplier_offer_id"),
            "provider": item.get("provider"),
            "brand": item.get("brand"),
            "article": item.get("article"),
            "quantity": item.get("quantity"),
        })
        self._refresh_order_submit_status(order)
        self._refresh_order_verification_status(order)
        order = self.order_store.update(order)
        self._refresh_orders_page()
        self.add_log(
            f"{order_id}: позиция пропущена вручную: {item.get('brand', '')} {item.get('article', '')}"
        )
        self._detail_log(
            "order_item_skipped",
            order_id=order_id,
            item_index=int(item_index),
            provider=item.get("provider"),
            brand=item.get("brand"),
            article=item.get("article"),
            quantity=item.get("quantity"),
        )

    def _restore_order_item(self, order_id, item_index):
        order = self.order_store.read(order_id)
        if not order:
            self.add_log(f"ОШИБКА: заказ {order_id} не найден")
            return
        items = order.get("items") or []
        if not (0 <= int(item_index) < len(items)):
            self.add_log(f"ОШИБКА: позиция заказа {order_id} не найдена")
            return
        item = items[int(item_index)]
        # Неподтверждённую позицию (ответ на оформление не дошёл) оператор
        # возвращает в работу сам — после того, как убедился у поставщика,
        # что заказа там нет.
        if not self._is_order_item_manual_excluded(item) and item.get("submit_status") != "unknown":
            self.add_log(f"{order_id}: вернуть можно только исключённую или неподтверждённую позицию")
            return
        restored_at = datetime.datetime.now().isoformat(timespec="seconds")
        item["submit_status"] = "not_submitted"
        item.pop("skip_reason", None)
        item.pop("skipped_at", None)
        item.pop("submit_unknown_reason", None)
        item["verification_status"] = "stale"
        item["verification_message"] = "возвращено в работу, нужна перепроверка"
        order["verification_status"] = "stale"
        order["verified_at"] = ""
        self._refresh_order_submit_status(order)
        order["provider_results"] = order.get("provider_results") or []
        order["provider_results"].append({
            "type": "restore_item",
            "created_at": restored_at,
            "item_index": int(item_index),
            "internal_offer_id": item.get("internal_offer_id"),
            "supplier_offer_id": item.get("supplier_offer_id"),
            "provider": item.get("provider"),
            "brand": item.get("brand"),
            "article": item.get("article"),
            "quantity": item.get("quantity"),
        })
        order = self.order_store.update(order)
        self._refresh_orders_page()
        self.add_log(
            f"{order_id}: позиция возвращена в работу: {item.get('brand', '')} {item.get('article', '')}"
        )
        self._detail_log(
            "order_item_restored",
            order_id=order_id,
            item_index=int(item_index),
            provider=item.get("provider"),
            brand=item.get("brand"),
            article=item.get("article"),
            quantity=item.get("quantity"),
        )

    def _replace_order_item_with_variant(self, order_id, item_index, offer):
        order = self.order_store.read(order_id)
        if not order:
            self.add_log(f"ОШИБКА: заказ {order_id} не найден")
            return
        if str(order.get("status") or "") == "sent":
            self.add_log(f"{order_id}: отправленный заказ нельзя менять")
            return
        items = order.get("items") or []
        if not (0 <= int(item_index) < len(items)):
            self.add_log(f"ОШИБКА: позиция заказа {order_id} не найдена")
            return
        order_item = items[int(item_index)]
        if str(order_item.get("submit_status") or "") in ("submitted", "skipped"):
            self.add_log(f"{order_id}: обработанную позицию нельзя заменить")
            return

        variant = self._order_variant_snapshot(offer)
        if not variant:
            self.add_log(f"ОШИБКА: вариант для замены в заказе {order_id} пустой")
            return
        requested_quantity = self._replacement_requested_quantity(order_item, variant)
        if not variant.get("internal_offer_id"):
            self.normalize_offer_item(variant, requested_quantity)
        q_info = self._quantity_info(variant, requested_quantity)
        if not q_info.can_order:
            self.add_log(
                f"ОШИБКА: нельзя выбрать вариант {variant.get('brand', '')} {variant.get('article', '')}: {q_info.reason}"
            )
            return

        replaced_at = datetime.datetime.now().isoformat(timespec="seconds")
        old_internal_offer_id = str(order_item.get("internal_offer_id") or "")
        old_sample = self._offer_log_sample([order_item], 1)
        self._apply_variant_to_order_item(order_item, variant, q_info, replaced_at)
        self._mark_group_variant_selected(order, order_item, old_internal_offer_id, variant)
        order["verification_status"] = "stale"
        order["verified_at"] = ""
        order["calculated_ready_date"] = self.order_store.calculated_ready_date(
            [{"item": item, "qty": item.get("quantity", 1)} for item in order.get("items") or []]
        )
        order["provider_results"] = order.get("provider_results") or []
        order["provider_results"].append({
            "type": "replace_variant",
            "created_at": replaced_at,
            "item_index": int(item_index),
            "old_internal_offer_id": old_internal_offer_id,
            "new_internal_offer_id": order_item.get("internal_offer_id"),
            "provider": order_item.get("provider"),
            "brand": order_item.get("brand"),
            "article": order_item.get("article"),
            "quantity": order_item.get("quantity"),
        })
        order = self.order_store.update(order)
        self._refresh_orders_page()
        self.add_log(
            f"{order_id}: выбран вариант {order_item.get('provider', '')} "
            f"{order_item.get('brand', '')} {order_item.get('article', '')}; нужна перепроверка"
        )
        self._detail_log(
            "order_item_variant_selected",
            order_id=order_id,
            item_index=int(item_index),
            old_item=old_sample[0] if old_sample else None,
            new_item=self._offer_log_sample([order_item], 1)[0],
        )

    def _order_variant_snapshot(self, offer):
        offer = dict(offer or {})
        snapshot = offer.get("snapshot") if isinstance(offer.get("snapshot"), dict) else {}
        variant = dict(snapshot)
        for key, value in offer.items():
            if key == "snapshot":
                continue
            if value not in (None, ""):
                variant[key] = value
        for key in ("selection_group_offers", "draft_group_offers", "group_offers"):
            variant.pop(key, None)
        return variant

    def _replacement_requested_quantity(self, order_item, variant):
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        for source in (order_item, snapshot, variant):
            value = (source or {}).get("requested_quantity")
            if value not in (None, ""):
                return value
        return order_item.get("quantity") or variant.get("quantity") or 1

    def _apply_variant_to_order_item(self, order_item, variant, q_info, changed_at):
        requested_quantity = q_info.requested_quantity
        actual_qty = q_info.actual_int()
        self._apply_sale_price(variant)
        purchase_price = float(variant.get("purchase_price", variant.get("price") or 0) or 0)
        sale_price = float(variant.get("order_sale_price", variant.get("sale_price", purchase_price)) or purchase_price)
        delivery_hours = self._delivery_hours(variant)
        search = dict(order_item.get("search") or {})
        previous = {
            "internal_offer_id": order_item.get("internal_offer_id"),
            "supplier_offer_id": order_item.get("supplier_offer_id"),
            "provider": order_item.get("provider"),
            "brand": order_item.get("brand"),
            "article": order_item.get("article"),
            "quantity": order_item.get("quantity"),
            "purchase_price": order_item.get("purchase_price"),
            "sale_price": order_item.get("sale_price"),
            "warehouse": order_item.get("warehouse"),
        }
        history = list(order_item.get("replacement_history") or [])
        history.append({"replaced_at": changed_at, "previous": previous})
        snapshot = dict(variant)
        snapshot.pop("snapshot", None)
        snapshot.pop("selection_group_offers", None)
        snapshot.pop("draft_group_offers", None)
        snapshot.pop("group_offers", None)
        snapshot["order_sale_price"] = sale_price
        order_item.clear()
        order_item.update({
            "internal_offer_id": str(variant.get("internal_offer_id") or ""),
            "supplier_offer_id": str(variant.get("supplier_offer_id") or ""),
            "provider": str(variant.get("provider_name") or variant.get("provider") or ""),
            "brand": str(variant.get("brand") or variant.get("normalized_brand") or ""),
            "display_brand": str(
                variant.get("display_brand")
                or self.display_brand_name(variant.get("brand") or variant.get("normalized_brand") or "")
            ),
            "article": str(variant.get("article") or variant.get("original_article") or ""),
            "warehouse": str(variant.get("warehouse") or variant.get("logo") or ""),
            "price": purchase_price,
            "purchase_price": purchase_price,
            "sale_price": sale_price,
            "name": str(variant.get("name") or variant.get("source_name") or order_item.get("name") or ""),
            "purchase_total": round(purchase_price * actual_qty, 2),
            "sale_total": round(sale_price * actual_qty, 2),
            "margin": round((sale_price - purchase_price) * actual_qty, 2),
            "quantity": actual_qty,
            "requested_quantity": str(requested_quantity),
            "available_quantity": q_info.available_int() if q_info.available_quantity is not None else None,
            "minimum_quantity": int(q_info.minimum_quantity),
            "quantity_step": int(q_info.quantity_step),
            "package_quantity": int(q_info.package_quantity),
            "delivery_hours": delivery_hours,
            "ready_date": (
                datetime.date.today() + datetime.timedelta(days=math.ceil(max(0, delivery_hours) / 24))
            ).isoformat(),
            "snapshot_at": changed_at,
            "submit_status": "not_submitted",
            "supplier_response": None,
            "search": search,
            "snapshot": snapshot,
            "verification_status": "stale",
            "verification_message": "выбран сохранённый вариант, нужна перепроверка",
            "last_checked_at": "",
            "replaced_at": changed_at,
            "replacement_history": history[-10:],
            "is_cross": bool(variant.get("is_cross")),
            "offer_relation": str(variant.get("offer_relation") or variant.get("cross_relation") or ""),
            "returnable": variant.get("returnable"),
        })

    def _mark_group_variant_selected(self, order, order_item, old_internal_offer_id, variant):
        groups = order.get("groups") or []
        if not groups:
            return
        selected_offer = OrderStore._group_offer_from_item(
            self._order_variant_snapshot(variant),
            int(order_item.get("quantity") or 1),
            selected=True,
        )
        selected_offer["verification_status"] = order_item.get("verification_status")
        selected_offer["verification_message"] = order_item.get("verification_message")
        new_key = OrderStore._group_offer_key(selected_offer)
        old_key = ("internal", old_internal_offer_id) if old_internal_offer_id else None
        group = self._find_replacement_group(groups, order_item, new_key, old_key)
        if not group:
            return
        found_new = False
        for offer in group.get("offers") or []:
            key = OrderStore._group_offer_key(offer)
            if old_key and key == old_key:
                offer["selected"] = False
            if key == new_key:
                offer.update(selected_offer)
                offer["selected"] = True
                found_new = True
        if not found_new:
            group.setdefault("offers", []).append(selected_offer)
        group["selected_quantity"] = sum(
            int(offer.get("quantity") or 0)
            for offer in group.get("offers") or []
            if offer.get("selected")
        )

    def _find_replacement_group(self, groups, order_item, new_key, old_key):
        for group in groups or []:
            keys = {OrderStore._group_offer_key(offer) for offer in (group or {}).get("offers") or []}
            if new_key in keys or (old_key and old_key in keys):
                return group
        search = order_item.get("search") if isinstance(order_item.get("search"), dict) else {}
        requested_brand = str(search.get("selected_brand") or "").strip()
        requested_article = self.clean_num(search.get("requested_article") or "")
        for group in groups or []:
            requested = (group or {}).get("requested") or {}
            group_article = self.clean_num(requested.get("article") or "")
            group_brand = str(requested.get("brand") or "").strip()
            if requested_article and group_article != requested_article:
                continue
            if requested_brand and group_brand and not self.same_brand_group(group_brand, requested_brand):
                continue
            return group
        return None

    def _recheck_order_thread(self, order_id, item_indexes=None):
        order = self.order_store.read(order_id)
        if not order:
            self._order_action_finished({}, f"ОШИБКА: заказ {order_id} не найден")
            return
        selected_indexes = set(int(index) for index in item_indexes) if item_indexes is not None else None
        checked_at = datetime.datetime.now().isoformat(timespec="seconds")
        ok_count = 0
        failed_count = 0
        changed_count = 0
        messages = []

        jobs = []
        for index, item in enumerate(order.get("items") or []):
            if selected_indexes is not None and index not in selected_indexes:
                continue
            if item.get("submit_status") in PROCESSED_SUBMIT_STATUSES:
                continue
            provider_name = str(item.get("provider") or "")
            article = str((item.get("search") or {}).get("requested_article") or item.get("article") or "")
            brand = str((item.get("search") or {}).get("selected_brand") or item.get("brand") or "")
            quantity = int(item.get("quantity") or 1)
            provider = self._provider_for_display_name(provider_name)
            if not provider:
                failed_count += 1
                self._mark_order_item_failed(item, checked_at, f"{provider_name}: поставщик отключён или не настроен")
                continue
            jobs.append((item, provider, article, brand, quantity))

        # Запросы к поставщикам идут параллельно: раньше 185 позиций опрашивались
        # по очереди примерно по секунде каждая и проверка занимала около четырёх
        # минут. Меняем данные заказа только после того, как всё получено, и строго
        # по порядку — так результат не зависит от того, кто ответил первым.
        fetched = []
        if jobs:
            with ThreadPoolExecutor(max_workers=min(ORDER_RECHECK_WORKERS, len(jobs))) as executor:
                futures = [
                    executor.submit(
                        self._current_order_offers, provider, article, brand, quantity, item
                    )
                    for item, provider, article, brand, quantity in jobs
                ]
                for future in futures:
                    try:
                        fetched.append((future.result(), None))
                    except Exception as exc:
                        fetched.append((None, exc))

        for (item, _provider, _article, _brand, quantity), (offers, error) in zip(jobs, fetched):
            if error is not None:
                failed_count += 1
                self._mark_order_item_failed(item, checked_at, f"ошибка перепроверки: {str(error)[:160]}")
                continue
            try:
                match = self._match_order_offer(item, offers)
                if not match:
                    failed_count += 1
                    self._mark_order_item_failed(item, checked_at, self._order_recheck_failure_reason(item, offers))
                    continue
                changed = self._apply_rechecked_offer(item, match, quantity, checked_at)
                if item.get("verification_status") == "failed":
                    failed_count += 1
                    continue
                if changed:
                    changed_count += 1
                ok_count += 1
            except Exception as exc:
                failed_count += 1
                self._mark_order_item_failed(item, checked_at, f"ошибка перепроверки: {str(exc)[:160]}")
        # Срок годности проверки отсчитываем от её ОКОНЧАНИЯ, а не от начала:
        # проверка 185 позиций занимает около 200 с и раньше съедала две трети
        # своего же пятиминутного окна, не оставляя времени на отправку.
        order["verified_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        self._refresh_order_verification_status(order)
        order["provider_results"] = order.get("provider_results") or []
        order["provider_results"].append({
            "type": "recheck_item" if selected_indexes is not None else "recheck",
            "created_at": checked_at,
            "ok": ok_count,
            "failed": failed_count,
            "changed": changed_count,
            "item_indexes": sorted(selected_indexes) if selected_indexes is not None else None,
            "messages": messages,
        })
        order = self.order_store.update(order)
        if failed_count:
            message = f"{order_id}: перепроверка завершена, ошибок {failed_count}, проверено {ok_count}"
        elif order.get("verification_status") == "failed":
            message = f"{order_id}: позиция проверена, но в заказе ещё есть проблемные строки"
        else:
            message = f"{order_id}: перепроверка успешна, проверено {ok_count}, изменений {changed_count}"
        self._order_action_finished(order, message)

    def _order_item_identity_filter(self, order_item):
        """Отбирает предложения, которые вообще могут подойти позиции заказа.

        Все три способа сопоставления в _match_order_offer требуют совпадения
        артикула и бренда, поэтому остальное можно отбросить до нормализации:
        раньше перепроверка 185 позиций прогоняла через неё 12 190 предложений
        и занимала около 200 секунд.
        """
        if not isinstance(order_item, dict):
            return None
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        article_key = self.clean_num(order_item.get("article") or snapshot.get("article") or "")
        brand = str(order_item.get("brand") or snapshot.get("brand") or "").strip()
        if not article_key and not brand:
            return None

        def keep(offer):
            if article_key and self.clean_num(
                offer.get("article") or offer.get("original_article") or ""
            ) != article_key:
                return False
            if brand and not self.same_brand_group(offer.get("brand") or "", brand):
                return False
            return True

        return keep

    def _current_order_offers(self, provider, article, brand, quantity, order_item=None):
        cls_name = provider.__class__.__name__
        display_name = self._provider_display_name(cls_name)
        raw_article = str(article or "")
        clean_article = self.clean_num(raw_article)
        result, _elapsed, _cache_hit = self._query_provider(
            provider,
            cls_name,
            raw_article,
            clean_article,
            brand,
            use_selected_variants=False,
        )
        keep = self._order_item_identity_filter(order_item)
        if keep is not None:
            result = [offer for offer in result or [] if keep(offer)]
        offers = []
        for offer in result or []:
            if "article" not in offer or not offer["article"]:
                offer["article"] = raw_article
            self.normalize_item_article(offer)
            self._apply_offer_relation(
                offer, clean_article, brand, raw_article, display_name
            )
            self.normalize_offer_item(offer, quantity)
            self._apply_warehouse_extra_days(display_name, offer)
            offers.append(dict(offer))
        return offers

    def _match_order_offer(self, order_item, offers):
        if not offers:
            return None
        strategy = self._order_item_strategy(order_item)
        max_hours = self._order_item_max_hours(order_item)
        exact_required = self._order_item_requires_exact_match(order_item)
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        part_id = str(order_item.get("part_id") or snapshot.get("part_id") or "").strip()
        if part_id:
            part_matches = [
                offer for offer in offers
                if str(offer.get("part_id") or "").strip() == part_id
                and self._order_offer_same_selected_identity(order_item, offer)
                and self._order_offer_allowed_for_match(order_item, offer, exact_required)
            ]
            if part_matches:
                return self._best_recheck_offer(part_matches, strategy, max_hours)
        supplier_offer_id = str(order_item.get("supplier_offer_id") or "")
        if not supplier_offer_id:
            supplier_offer_id = str(snapshot.get("supplier_offer_id") or "")
        if supplier_offer_id:
            id_matches = [
                offer for offer in offers
                if str(offer.get("supplier_offer_id") or "") == supplier_offer_id
                and self._order_offer_same_selected_identity(order_item, offer)
                and self._order_offer_allowed_for_match(order_item, offer, exact_required)
            ]
            if id_matches:
                return self._best_recheck_offer(id_matches, strategy, max_hours)
        article_key = self.clean_num(order_item.get("article") or snapshot.get("article") or "")
        brand = str(order_item.get("brand") or snapshot.get("brand") or "")
        candidates = [
            offer for offer in offers
            if self.clean_num(offer.get("article") or "") == article_key
            and self.same_brand_group(offer.get("brand") or "", brand)
            and self._order_offer_allowed_for_match(order_item, offer, exact_required)
        ]
        if not candidates:
            return None
        warehouse_key = self._order_warehouse_key(order_item)
        if warehouse_key:
            warehouse_matches = [
                offer for offer in candidates
                if self._order_warehouse_key(offer) == warehouse_key
            ]
            if warehouse_matches:
                candidates = warehouse_matches
        return self._best_recheck_offer(candidates, strategy, max_hours)

    def _order_item_requires_exact_match(self, order_item):
        search = order_item.get("search") if isinstance(order_item.get("search"), dict) else {}
        return search.get("exact_match") is True

    def _order_offer_same_selected_identity(self, order_item, offer):
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        article_key = self.clean_num(order_item.get("article") or snapshot.get("article") or "")
        if article_key and self.clean_num(offer.get("article") or offer.get("original_article") or "") != article_key:
            return False
        brand = str(order_item.get("brand") or snapshot.get("brand") or "").strip()
        if brand and not self.same_brand_group(offer.get("brand") or "", brand):
            return False
        return True

    def _order_offer_allowed_for_match(self, order_item, offer, exact_required):
        if offer.get("can_order_quantity") is False:
            return False
        offer_is_cross = self._offer_is_cross(offer)
        if offer_is_cross and (exact_required or not self._order_item_is_cross(order_item)):
            return False
        if not exact_required:
            return True
        search = order_item.get("search") if isinstance(order_item.get("search"), dict) else {}
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        requested_article = self.clean_num(
            search.get("requested_article")
            or order_item.get("article")
            or snapshot.get("article")
            or ""
        )
        requested_brand = str(
            search.get("selected_brand")
            or order_item.get("brand")
            or snapshot.get("brand")
            or ""
        ).strip()
        offer_article = self.clean_num(offer.get("article") or offer.get("original_article") or "")
        if requested_article and offer_article != requested_article:
            return False
        if requested_brand and not self.same_brand_group(offer.get("brand") or "", requested_brand):
            return False
        return True

    def _offer_is_cross(self, offer):
        relation = str((offer or {}).get("offer_relation") or (offer or {}).get("cross_relation") or "").strip()
        return bool((offer or {}).get("is_cross")) or relation in CROSS_RELATION_CODES

    def _order_item_is_cross(self, order_item):
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        relation = str(
            order_item.get("offer_relation")
            or snapshot.get("offer_relation")
            or order_item.get("cross_relation")
            or snapshot.get("cross_relation")
            or ""
        ).strip()
        return bool(order_item.get("is_cross") or snapshot.get("is_cross")) or relation in CROSS_RELATION_CODES

    def _order_recheck_failure_reason(self, order_item, offers):
        if not offers:
            return "предложение не найдено при перепроверке"
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        ids = {
            str(value).strip()
            for value in (
                order_item.get("part_id"),
                snapshot.get("part_id"),
                order_item.get("supplier_offer_id"),
                snapshot.get("supplier_offer_id"),
            )
            if value not in (None, "")
        }
        if ids:
            same_id = [
                offer for offer in offers
                if str(offer.get("part_id") or "").strip() in ids
                or str(offer.get("supplier_offer_id") or "").strip() in ids
            ]
            if same_id and not any(self._order_offer_same_selected_identity(order_item, offer) for offer in same_id):
                return "поставщик вернул другой товар по сохранённому ID"
        selected_article = self.clean_num(order_item.get("article") or snapshot.get("article") or "")
        selected_brand = str(order_item.get("brand") or snapshot.get("brand") or "").strip()
        same_identity = [
            offer for offer in offers
            if (not selected_article or self.clean_num(offer.get("article") or "") == selected_article)
            and (not selected_brand or self.same_brand_group(offer.get("brand") or "", selected_brand))
        ]
        if same_identity and any(self._offer_is_cross(offer) for offer in same_identity) and not self._order_item_is_cross(order_item):
            return "поставщик вернул аналог/кросс вместо выбранного товара"
        return "выбранный бренд/артикул не найден при перепроверке"

    def _best_recheck_offer(self, candidates, strategy="price", max_hours=None):
        """Выбирает предложение по той же стратегии, что и подбор в заказ.

        Раньше здесь всегда стоял «минимальный срок». На заказе 18.08 это
        подменило 17 позиций на более быстрые и дорогие — плюс 3142 рубля
        к закупке, хотя заказ собирали по минимальной цене. Срок остаётся
        первым ключом только для стратегии «минимальный срок».
        """
        # Лимит срока был жёстким условием при подборе (rank_offers его
        # отбрасывает), поэтому и здесь предложения дольше лимита берём только
        # если других не осталось — иначе выбор по цене нарушил бы срок.
        if max_hours:
            within = [offer for offer in candidates if self._delivery_hours(offer) <= max_hours]
            if within:
                candidates = within
        if str(strategy or "") == "fastest":
            return min(candidates, key=lambda offer: (
                self._delivery_hours(offer),
                self._offer_price_for_match(offer),
                -int(offer.get("available_quantity") or 0),
            ))
        return min(candidates, key=lambda offer: (
            self._offer_price_for_match(offer),
            self._delivery_hours(offer),
            -int(offer.get("available_quantity") or 0),
        ))

    def _order_item_strategy(self, order_item):
        """Стратегия, которой позицию выбирали. Старые заказы её не хранят —
        там по умолчанию «минимальная цена», как в подборе."""
        search = order_item.get("search") if isinstance((order_item or {}).get("search"), dict) else {}
        snapshot = order_item.get("snapshot") if isinstance((order_item or {}).get("snapshot"), dict) else {}
        strategy = (
            str(search.get("strategy") or "").strip()
            or str(snapshot.get("selection_strategy") or "").strip()
        )
        return strategy or "price"

    def _order_item_max_hours(self, order_item):
        """Лимит срока, с которым позицию подбирали, в часах."""
        search = order_item.get("search") if isinstance((order_item or {}).get("search"), dict) else {}
        snapshot = order_item.get("snapshot") if isinstance((order_item or {}).get("snapshot"), dict) else {}
        value = search.get("max_days")
        if value in (None, ""):
            value = snapshot.get("selection_max_days")
        try:
            days = int(value)
        except (TypeError, ValueError):
            return None
        return days * 24 if days > 0 else None

    def _offer_price_for_match(self, offer):
        try:
            return float(str(offer.get("purchase_price", offer.get("price") or 999999999)).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return 999999999.0

    def _order_warehouse_key(self, item):
        for key in ("warehouse_id", "stock_id", "keyzak", "warehouse", "logo"):
            value = item.get(key)
            if value not in (None, ""):
                return self.clean_filter_text(value)
        snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        for key in ("warehouse_id", "stock_id", "keyzak", "warehouse", "logo"):
            value = snapshot.get(key)
            if value not in (None, ""):
                return self.clean_filter_text(value)
        return ""

    def _apply_rechecked_offer(self, order_item, offer, quantity, checked_at):
        old_purchase = float(order_item.get("purchase_price", order_item.get("price") or 0) or 0)
        old_delivery = int(order_item.get("delivery_hours") or 0)
        old_qty = int(order_item.get("quantity") or quantity or 1)
        q_info = self._quantity_info(offer, old_qty)
        if not q_info.can_order:
            self._mark_order_item_failed(order_item, checked_at, q_info.reason or "нельзя заказать нужное количество")
            return False
        actual_qty = q_info.actual_int()
        purchase_price = float(offer.get("purchase_price", offer.get("price") or 0) or 0)
        sale_price, _, _ = self.apply_markup(purchase_price)
        delivery_hours = self._delivery_hours(offer)
        order_item["supplier_offer_id"] = str(offer.get("supplier_offer_id") or order_item.get("supplier_offer_id") or "")
        order_item["provider"] = str(offer.get("provider_name") or offer.get("provider") or order_item.get("provider") or "")
        order_item["brand"] = str(offer.get("brand") or order_item.get("brand") or "")
        order_item["display_brand"] = str(offer.get("display_brand") or self.display_brand_name(order_item["brand"]))
        order_item["article"] = str(offer.get("article") or order_item.get("article") or "")
        order_item["warehouse"] = str(offer.get("warehouse") or offer.get("logo") or order_item.get("warehouse") or "")
        order_item["name"] = str(offer.get("name") or order_item.get("name") or offer.get("source_name") or "")
        order_item["price"] = purchase_price
        order_item["purchase_price"] = purchase_price
        order_item["sale_price"] = sale_price
        order_item["quantity"] = actual_qty
        order_item["purchase_total"] = round(purchase_price * actual_qty, 2)
        order_item["sale_total"] = round(sale_price * actual_qty, 2)
        order_item["margin"] = round(order_item["sale_total"] - order_item["purchase_total"], 2)
        order_item["delivery_hours"] = delivery_hours
        order_item["ready_date"] = (datetime.date.today() + datetime.timedelta(days=math.ceil(max(0, delivery_hours) / 24))).isoformat()
        order_item["last_checked_at"] = checked_at
        order_item["verification_status"] = "valid"
        order_item["verification_message"] = "проверено"
        snapshot = dict(offer)
        snapshot["internal_offer_id"] = order_item.get("internal_offer_id") or snapshot.get("internal_offer_id", "")
        snapshot["order_sale_price"] = sale_price
        order_item["snapshot"] = snapshot
        return (
            round(old_purchase, 2) != round(purchase_price, 2)
            or old_delivery != delivery_hours
            or old_qty != actual_qty
        )

    def _refresh_order_verification_status(self, order):
        items = [
            item for item in (order or {}).get("items") or []
            if item.get("submit_status") != "skipped"
        ]
        if not items:
            order["verification_status"] = "valid"
            return "valid"
        statuses = [str(item.get("verification_status") or "") for item in items]
        if any(status == "checking" for status in statuses):
            order["verification_status"] = "checking"
        elif any(status == "failed" for status in statuses):
            order["verification_status"] = "failed"
        elif all(status == "valid" for status in statuses):
            order["verification_status"] = "valid"
        else:
            order["verification_status"] = "stale"
        return order["verification_status"]

    def _mark_order_item_failed(self, item, checked_at, message):
        item["last_checked_at"] = checked_at
        item["verification_status"] = "failed"
        item["verification_message"] = str(message or "ошибка проверки")

    def _submit_order_thread_guarded(self, order_id, item_indexes=None, submit_key=""):
        try:
            self._submit_order_thread(order_id, item_indexes, submit_key=submit_key)
        except Exception as exc:
            self._detail_log(
                "order_submit_thread_failed",
                order_id=order_id,
                idempotency_key=submit_key,
                error=str(exc),
            )
            self._order_action_finished({}, f"ОШИБКА: заказ {order_id}: {str(exc)}")
        finally:
            self._unlock_order_submit(order_id)

    def _submit_order_thread(self, order_id, item_indexes=None, submit_key=""):
        order = self.order_store.read(order_id)
        if not order:
            self._order_action_finished({}, f"ОШИБКА: заказ {order_id} не найден")
            return
        selected_indexes = set(int(index) for index in item_indexes) if item_indexes is not None else None
        order_items = order.get("items") or []
        target_indexes = [
            index for index, _ in enumerate(order_items)
            if selected_indexes is None or index in selected_indexes
        ]
        submitted_at = datetime.datetime.now().isoformat(timespec="seconds")
        ok_count = 0
        failed_count = 0
        skipped_count = 0
        not_ready_count = 0
        provider_results = order.get("provider_results") or []
        comment = str(order.get("comment") or "")
        order_verified = str(order.get("verification_status") or "") == "valid"
        self._detail_log(
            "order_submit_start",
            order_id=order_id,
            idempotency_key=submit_key,
            item_count=len(target_indexes),
            order_item_count=len(order_items),
            selected_item_indexes=target_indexes if selected_indexes is not None else None,
            verification_status=order.get("verification_status"),
        )
        provider_results.append({
            "type": "submit_attempt",
            "created_at": submitted_at,
            "idempotency_key": submit_key,
            "selected_item_indexes": target_indexes if selected_indexes is not None else None,
            "item_count": len(target_indexes),
            "verification_status": order.get("verification_status"),
        })
        unknown_count = 0
        pending = []
        for index, item in enumerate(order_items):
            if selected_indexes is not None and index not in selected_indexes:
                continue
            if item.get("submit_status") in PROCESSED_SUBMIT_STATUSES:
                skipped_count += 1
                self._detail_log(
                    "order_submit_item_skipped",
                    order_id=order_id,
                    idempotency_key=submit_key,
                    provider=item.get("provider"),
                    brand=item.get("brand"),
                    article=item.get("article"),
                    quantity=item.get("quantity"),
                    internal_offer_id=item.get("internal_offer_id"),
                    supplier_offer_id=item.get("supplier_offer_id"),
                    reason=f"уже {item.get('submit_status')}",
                )
                continue
            item_verification = str(item.get("verification_status") or "")
            if item_verification != "valid" and not (order_verified and not item_verification):
                not_ready_count += 1
                self._detail_log(
                    "order_submit_item_not_ready",
                    order_id=order_id,
                    idempotency_key=submit_key,
                    provider=item.get("provider"),
                    brand=item.get("brand"),
                    article=item.get("article"),
                    quantity=item.get("quantity"),
                    internal_offer_id=item.get("internal_offer_id"),
                    supplier_offer_id=item.get("supplier_offer_id"),
                    verification_status=item_verification,
                    verification_message=item.get("verification_message"),
                )
                continue
            provider_name = str(item.get("provider") or "")
            provider = self._provider_for_display_name(provider_name)
            qty = int(item.get("quantity") or 1)
            send_item = self._order_item_for_submit(item)
            state, _ = self._provider_cart_state(provider_name)
            if state == "local":
                item["submit_status"] = "skipped"
                item["skip_reason"] = "локальная позиция"
                item["local_processed_at"] = submitted_at
                item["supplier_response"] = {"success": False, "error": "локальная позиция, поставщику не отправляется"}
                skipped_count += 1
                self._detail_log(
                    "order_submit_item_skipped",
                    order_id=order_id,
                    idempotency_key=submit_key,
                    provider=provider_name,
                    brand=item.get("brand"),
                    article=item.get("article"),
                    quantity=qty,
                    internal_offer_id=item.get("internal_offer_id"),
                    supplier_offer_id=item.get("supplier_offer_id"),
                    reason="локальная позиция",
                )
                continue
            if not provider:
                item["submit_status"] = "failed"
                item["supplier_response"] = {"success": False, "error": "поставщик отключён или не настроен"}
                failed_count += 1
                self._detail_log(
                    "order_submit_item_finish",
                    order_id=order_id,
                    idempotency_key=submit_key,
                    provider=provider_name,
                    brand=item.get("brand"),
                    article=item.get("article"),
                    quantity=qty,
                    internal_offer_id=item.get("internal_offer_id"),
                    supplier_offer_id=item.get("supplier_offer_id"),
                    success=False,
                    response=self._response_log_sample(item["supplier_response"]),
                )
                continue
            missing = self._missing_cart_fields(provider_name, send_item)
            if missing:
                item["submit_status"] = "failed"
                item["supplier_response"] = {"success": False, "error": "не хватает данных: " + ", ".join(missing)}
                failed_count += 1
                self._detail_log(
                    "order_submit_item_finish",
                    order_id=order_id,
                    idempotency_key=submit_key,
                    provider=provider_name,
                    brand=item.get("brand"),
                    article=item.get("article"),
                    quantity=qty,
                    internal_offer_id=item.get("internal_offer_id"),
                    supplier_offer_id=item.get("supplier_offer_id"),
                    success=False,
                    response=self._response_log_sample(item["supplier_response"]),
                )
                continue
            allowed, block_reason = self._order_submit_preflight(item, send_item, qty)
            if not allowed:
                item["submit_status"] = "failed"
                item["verification_status"] = "failed"
                item["verification_message"] = block_reason
                item["supplier_response"] = {"success": False, "error": block_reason}
                failed_count += 1
                self._log(
                    f"{order_id}: позиция заблокирована перед отправкой: "
                    f"{item.get('brand', '')} {item.get('article', '')} — {block_reason}"
                )
                self._detail_log(
                    "order_submit_item_blocked",
                    order_id=order_id,
                    idempotency_key=submit_key,
                    provider=provider_name,
                    brand=item.get("brand"),
                    article=item.get("article"),
                    quantity=qty,
                    internal_offer_id=item.get("internal_offer_id"),
                    supplier_offer_id=item.get("supplier_offer_id"),
                    reason=block_reason,
                    item=self._offer_log_sample([send_item], 1)[0] if send_item else None,
                )
                provider_results.append({
                    "type": "submit_blocked",
                    "created_at": submitted_at,
                    "internal_offer_id": item.get("internal_offer_id"),
                    "supplier_offer_id": item.get("supplier_offer_id"),
                    "provider": provider_name,
                    "quantity": qty,
                    "idempotency_key": submit_key,
                    "response": item["supplier_response"],
                })
                continue
            pending.append((item, provider, provider_name, send_item, qty))

        # Отправку разным поставщикам ведём одновременно, но позиции одного
        # поставщика — строго по очереди: у Profit-League каждая позиция очищает
        # корзину и оформляет заказ, поэтому параллельные вызовы затёрли бы друг
        # друга. Данные заказа меняем после того, как все ответы получены.
        results = [None] * len(pending)

        def _send_provider_batch(positions):
            """Отдаёт поставщику все позиции одним вызовом.

            Возвращает True, если пакет отработал и ответы разложены по
            позициям. False — значит пакетный путь не применим и нужно идти
            по одной позиции.
            """
            _item, queue_provider, queue_name, _send_item, _qty = pending[positions[0]]
            batch_call = getattr(queue_provider, "add_to_basket_batch", None)
            if not callable(batch_call) or len(positions) < 2:
                return False
            rows = []
            for position in positions:
                _row_item, _p, _n, row_send_item, row_qty = pending[position]
                rows.append({"item": row_send_item, "quantity": row_qty})
            self._detail_log(
                "order_submit_batch_start",
                order_id=order_id,
                idempotency_key=submit_key,
                provider=queue_name,
                item_count=len(rows),
                quantity_total=sum(int(row["quantity"] or 0) for row in rows),
                items=self._offer_log_sample([row["item"] for row in rows], 25),
            )
            try:
                with self._provider_debug_scope(
                    queue_provider,
                    order_id=order_id,
                    idempotency_key=submit_key,
                    batch_item_count=len(rows),
                ):
                    batch_results = batch_call(rows, comment=comment)
            except Exception as exc:
                batch_results = [{"success": False, "error": str(exc)}] * len(rows)
            batch_results = list(batch_results or [])
            if len(batch_results) != len(rows):
                # Ответ, который нельзя разложить по позициям, считаем неудачей
                # целиком: молча пометить часть заказа отправленной опаснее,
                # чем показать ошибку и дать отправить повторно.
                error = (
                    f"{queue_name}: пакетный ответ не совпал с числом позиций "
                    f"({len(batch_results)} на {len(rows)})"
                )
                self._detail_log(
                    "order_submit_batch_mismatch",
                    order_id=order_id,
                    idempotency_key=submit_key,
                    provider=queue_name,
                    item_count=len(rows),
                    result_count=len(batch_results),
                )
                batch_results = [{"success": False, "error": error}] * len(rows)
            for position, result in zip(positions, batch_results):
                results[position] = result if isinstance(result, dict) else {
                    "success": False,
                    "error": f"{queue_name}: неожиданный ответ пакета: {str(result)[:160]}",
                }
            self._detail_log(
                "order_submit_batch_finish",
                order_id=order_id,
                idempotency_key=submit_key,
                provider=queue_name,
                item_count=len(rows),
                ok_count=sum(1 for row in batch_results if isinstance(row, dict) and row.get("success")),
            )
            return True

        def _send_provider_queue(positions):
            if _send_provider_batch(positions):
                return
            for position in positions:
                _item, queue_provider, queue_name, queue_send_item, queue_qty = pending[position]
                self._detail_log(
                    "order_submit_item_start",
                    order_id=order_id,
                    idempotency_key=submit_key,
                    provider=queue_name,
                    brand=queue_send_item.get("brand"),
                    article=queue_send_item.get("article"),
                    quantity=queue_qty,
                    warehouse=queue_send_item.get("warehouse") or queue_send_item.get("logo"),
                    internal_offer_id=queue_send_item.get("internal_offer_id"),
                    supplier_offer_id=queue_send_item.get("supplier_offer_id"),
                    search_id=queue_send_item.get("search_id"),
                    part_id=queue_send_item.get("part_id"),
                    offer_id=queue_send_item.get("offer_id"),
                    is_cross=bool(queue_send_item.get("is_cross")),
                    offer_relation=queue_send_item.get("offer_relation"),
                    item=self._offer_log_sample([queue_send_item], 1)[0] if queue_send_item else None,
                )
                try:
                    # Диагностику провайдера включаем только на время отправки:
                    # по ней видно, на каком именно вызове оборвалась цепочка
                    # и остался ли товар висеть в корзине поставщика.
                    with self._provider_debug_scope(
                        queue_provider,
                        order_id=order_id,
                        idempotency_key=submit_key,
                        brand=queue_send_item.get("brand"),
                        article=queue_send_item.get("article"),
                        quantity=queue_qty,
                        internal_offer_id=queue_send_item.get("internal_offer_id"),
                    ):
                        results[position] = queue_provider.add_to_basket(
                            queue_send_item, quantity=queue_qty, comment=comment
                        )
                except Exception as exc:
                    results[position] = {"success": False, "error": str(exc)}

        if pending:
            queues = {}
            for position, job in enumerate(pending):
                queues.setdefault(job[2], []).append(position)
            if len(queues) > 1:
                with ThreadPoolExecutor(max_workers=len(queues)) as executor:
                    futures = [
                        executor.submit(_send_provider_queue, positions)
                        for positions in queues.values()
                    ]
                    for future in futures:
                        future.result()
            else:
                for positions in queues.values():
                    _send_provider_queue(positions)

        for position, (item, provider, provider_name, send_item, qty) in enumerate(pending):
            result = results[position] or {"success": False, "error": "поставщик не ответил"}
            if hasattr(self, "order_history"):
                self.order_history.append(send_item, qty, {"order_id": order_id, **result})
            item["supplier_response"] = result
            item["submitted_at"] = submitted_at
            if result.get("success"):
                item["submit_status"] = "submitted"
            elif result.get("uncertain"):
                # Ответ на оформление не дошёл: заказ мог уйти поставщику.
                # Ни отправленной, ни готовой к повтору такую позицию считать
                # нельзя — решение за оператором.
                item["submit_status"] = "unknown"
                item["submit_unknown_reason"] = str(result.get("error") or "")
            else:
                item["submit_status"] = "failed"
            self._detail_log(
                "order_submit_item_finish",
                order_id=order_id,
                idempotency_key=submit_key,
                provider=provider_name,
                brand=item.get("brand"),
                article=item.get("article"),
                quantity=qty,
                internal_offer_id=item.get("internal_offer_id"),
                supplier_offer_id=item.get("supplier_offer_id"),
                success=bool(result.get("success")),
                item=self._offer_log_sample([send_item], 1)[0] if send_item else None,
                response=self._response_log_sample(result),
            )
            provider_results.append({
                "type": "submit",
                "created_at": submitted_at,
                "internal_offer_id": item.get("internal_offer_id"),
                "supplier_offer_id": item.get("supplier_offer_id"),
                "provider": provider_name,
                "quantity": qty,
                "idempotency_key": submit_key,
                "response": result,
            })
            if result.get("success"):
                ok_count += 1
            elif item["submit_status"] == "unknown":
                unknown_count += 1
            else:
                failed_count += 1
        order["provider_results"] = provider_results
        order["last_submit_idempotency_key"] = submit_key
        processed_count = ok_count + failed_count + skipped_count + unknown_count
        new_status = self._refresh_order_submit_status(order, not_ready_count=not_ready_count)
        if new_status == "sent":
            order["verification_status"] = "submitted"
        else:
            self._refresh_order_verification_status(order)
        if processed_count:
            order["processed_at"] = submitted_at
        order["submitted_at"] = submitted_at if ok_count else order.get("submitted_at", "")
        order = self.order_store.update(order)
        remaining_count = sum(
            1 for item in order.get("items") or []
            if item.get("submit_status") not in PROCESSED_SUBMIT_STATUSES
        )
        message = (
            f"{order_id}: отправка завершена, успешно {ok_count}, ошибок {failed_count}"
            + (f", без подтверждения {unknown_count}" if unknown_count else "")
            + (f", пропущено {skipped_count}" if skipped_count else "")
            + (f", не готово {not_ready_count}" if not_ready_count else "")
            + (f", осталось {remaining_count}" if remaining_count else "")
        )
        self._detail_log(
            "order_submit_finish",
            order_id=order_id,
            idempotency_key=submit_key,
            ok_count=ok_count,
            failed_count=failed_count,
            unknown_count=unknown_count,
            skipped_count=skipped_count,
            not_ready_count=not_ready_count,
            remaining_count=remaining_count,
            status=order.get("status"),
        )
        self._order_action_finished(order, message)

    def _order_submit_preflight(self, order_item, send_item, quantity):
        order_item = order_item or {}
        send_item = send_item or {}
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        selected_article = self.clean_num(order_item.get("article") or snapshot.get("article") or "")
        selected_brand = str(order_item.get("brand") or snapshot.get("brand") or "").strip()

        snapshot_article = self.clean_num(snapshot.get("article") or snapshot.get("original_article") or "")
        if selected_article and snapshot_article and snapshot_article != selected_article:
            return False, "сохранённый API-id относится к другому артикулу"

        snapshot_brand = str(snapshot.get("brand") or snapshot.get("normalized_brand") or "").strip()
        if selected_brand and snapshot_brand and not self.same_brand_group(snapshot_brand, selected_brand):
            return False, "сохранённый API-id относится к другому бренду"

        exact_required = self._order_item_requires_exact_match(order_item)
        if exact_required and self._offer_is_cross(snapshot):
            return False, "сохранённый API-id относится к аналогу/кроссу"

        if not self._order_offer_same_selected_identity(order_item, send_item):
            return False, "данные отправки не совпадают с выбранным брендом/артикулом"
        if not self._order_offer_allowed_for_match(order_item, send_item, exact_required):
            return False, "данные отправки не проходят правило точного совпадения"

        requested_quantity = self._order_submit_requested_quantity(order_item, quantity)
        info = self._quantity_info(send_item, requested_quantity)
        if not info.can_order:
            return False, info.reason or "нельзя заказать нужное количество"
        actual_quantity = info.actual_int()
        try:
            expected_quantity = int(quantity or 1)
        except (TypeError, ValueError):
            expected_quantity = 1
        if actual_quantity != expected_quantity:
            return (
                False,
                f"количество изменилось после проверки: было {expected_quantity}, стало {actual_quantity}",
            )
        return True, ""

    def _order_submit_requested_quantity(self, order_item, fallback_quantity=1):
        snapshot = order_item.get("snapshot") if isinstance(order_item.get("snapshot"), dict) else {}
        for source in (order_item, snapshot):
            value = (source or {}).get("requested_quantity")
            if value not in (None, ""):
                return value
        return fallback_quantity

    def _order_item_for_submit(self, order_item):
        item = dict(order_item.get("snapshot") or {})
        for key in (
            "internal_offer_id", "supplier_offer_id", "provider", "brand", "article",
            "warehouse", "price", "purchase_price", "sale_price", "delivery_hours",
        ):
            if order_item.get(key) not in (None, ""):
                item[key] = order_item.get(key)
        item.setdefault("logo", order_item.get("warehouse") or item.get("warehouse") or "")
        return item

    def _detail_log(self, event, provider="", message="", **fields):
        logger = getattr(self, "detailed_logger", None)
        if not logger:
            return
        try:
            logger.log(event, provider=provider, message=message, **fields)
        except Exception:
            pass

    def _provider_debug_scope(self, provider, **context):
        """Контекстный менеджер: включает debug провайдера на время блока."""

        class _Scope:
            def __enter__(scope_self):
                if hasattr(provider, "debug_context"):
                    provider.debug_context = context
                return provider

            def __exit__(scope_self, *exc_info):
                if hasattr(provider, "debug_context"):
                    provider.debug_context = None
                return False

        return _Scope()

    def _record_cross_pairs(self, brand, article, offers):
        """Складывает аналоги поиска в базу пар. Молча: сбор данных не должен
        мешать ни поиску, ни отправке."""
        # Через __dict__, а не getattr: у QWidget обращение к любому атрибуту
        # до вызова родительского __init__ бросает RuntimeError, и сбор данных
        # уронил бы поиск в тестовых сборках приложения.
        store = self.__dict__.get("cross_store")
        if store is None or not offers:
            return 0
        if not (self.__dict__.get("settings") or {}).get("collect_cross_pairs", True):
            return 0
        try:
            return store.record(
                brand, article, offers,
                clean_num=self.clean_num,
                brand_key=self.brand_group_key,
            )
        except Exception as exc:
            self._detail_log("cross_store_failed", message=str(exc)[:200])
            return 0

    def _offer_log_sample(self, offers, limit=8):
        sample = []
        for item in list(offers or [])[:max(0, int(limit or 0))]:
            if not isinstance(item, dict):
                continue
            sample.append(
                {
                    "provider": item.get("provider_name") or item.get("provider"),
                    "brand": item.get("brand"),
                    "display_brand": item.get("display_brand"),
                    "article": item.get("article"),
                    "name": item.get("name"),
                    "price": item.get("price"),
                    "sale_price": item.get("sale_price"),
                    "quantity": item.get("quantity"),
                    "available_quantity": item.get("available_quantity"),
                    "max_count": item.get("max_count"),
                    "availability_is_lower_bound": item.get("availability_is_lower_bound"),
                    "minimum_quantity": item.get("minimum_quantity"),
                    "actual_order_quantity": item.get("actual_order_quantity"),
                    "requested_quantity": item.get("requested_quantity"),
                    "multiplicity": item.get("multiplicity") or item.get("quantity_step"),
                    "delivery_hours": item.get("delivery_hours"),
                    "days": item.get("days"),
                    "delivery_total_hours": item.get("delivery_total_hours"),
                    "delivery_text": (
                        item.get("delivery_text_original")
                        or item.get("delivery_display")
                        or item.get("delivery_text")
                        or ""
                    ),
                    "warehouse": item.get("warehouse") or item.get("logo"),
                    "is_cross": bool(item.get("is_cross")),
                    "relation": item.get("offer_relation_reason") or item.get("cross_relation"),
                    "internal_offer_id": item.get("internal_offer_id"),
                    "supplier_offer_id": item.get("supplier_offer_id"),
                    "search_id": item.get("search_id"),
                    "part_id": item.get("part_id"),
                    "offer_id": item.get("offer_id"),
                    "remote_id": item.get("remote_id"),
                    "article_id": item.get("article_id"),
                    "code": item.get("code"),
                    "goods_id": item.get("goods_id"),
                    "warehouse_id": item.get("warehouse_id"),
                    "gid": item.get("gid"),
                    "zakaz_code": item.get("zakaz_code"),
                    "stock_id": item.get("stock_id"),
                    "product_id": item.get("product_id"),
                    "source_id": item.get("source_id"),
                    "keyzak": item.get("keyzak"),
                }
            )
        return sample

    def _response_log_sample(self, response):
        if not isinstance(response, dict):
            return str(response or "")[:500]
        result = {
            "success": bool(response.get("success")),
        }
        for key in ("error", "message", "status", "code"):
            value = response.get(key)
            if value not in (None, ""):
                result[key] = str(value)[:500]
        for key in ("data", "response"):
            value = response.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (str, int, float, bool)):
                result[key] = str(value)[:500]
            elif isinstance(value, dict):
                result[key] = {
                    str(nested_key): str(nested_value)[:240]
                    for nested_key, nested_value in list(value.items())[:8]
                    if nested_value not in (None, "")
                }
            elif isinstance(value, list):
                result[key] = f"list[{len(value)}]"
        return result

    def clean_num(self, text):
        if not text: return ""
        return re.sub(r'[^A-Z0-9]', '', str(text).upper())

    def clean_filter_text(self, text):
        return "".join(char for char in str(text or "").upper() if char.isalnum())

    def _parse_filter_list(self, text):
        if isinstance(text, list):
            raw_items = text
        else:
            raw_items = re.split(r'[\n,;]+', str(text or ""))
        return [self.clean_filter_text(item) for item in raw_items if self.clean_filter_text(item)]

    def _item_filter_values(self, item):
        keys = (
            "warehouse_id", "stock_id", "keyzak", "logo", "warehouse",
            "storage", "Storage", "custom_warehouse_name", "return_type_name",
        )
        values = []
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                values.append(str(value))
        return values

    def _item_matches_filter_values(self, item, filters):
        if not filters:
            return True
        normalized_values = [self.clean_filter_text(value) for value in self._item_filter_values(item)]
        return any(
            flt and any(flt == value or flt in value or value in flt for value in normalized_values)
            for flt in filters
        )

    def _parse_warehouse_days(self, text):
        rules = {}
        if isinstance(text, dict):
            items = text.items()
        else:
            items = []
            for line in re.split(r'[\n;]+', str(text or "")):
                line = line.strip()
                if not line:
                    continue
                key, value = "", ""
                for separator in ("=", ":", "|", "\t"):
                    if separator in line:
                        key, value = line.rsplit(separator, 1)
                        break
                if not key:
                    match = re.match(r"^(.*?)[\s,]+(-?\d+)\s*$", line)
                    if match:
                        key, value = match.group(1), match.group(2)
                if key:
                    items.append((key, value))
        for key, value in items:
            normalized = self.clean_filter_text(key)
            if not normalized:
                continue
            hours = self._parse_extra_delivery_hours(value)
            if hours is None:
                continue
            rules[normalized] = hours
        return rules

    def _parse_extra_delivery_hours(self, value):
        text = str(value or "").strip()
        if not text:
            return None
        if "," in text:
            parts = [part.strip() for part in text.split(",", 1)]
            if len(parts) == 2 and all(re.fullmatch(r"-?\d+", part or "0") for part in parts):
                hours = int(parts[0] or 0)
                days = int(parts[1] or 0)
                return max(0, days * 24 + hours)
        return None

    def _warehouse_extra_for_item(self, display_name, item):
        rules = getattr(self, "warehouse_extra_days", {}).get(display_name, {})
        if not rules:
            return 0
        values = [self.clean_filter_text(value) for value in self._item_filter_values(item)]
        for value in values:
            if value in rules:
                return rules[value]
        for value in values:
            for warehouse_key, days in rules.items():
                if warehouse_key and (warehouse_key in value or value in warehouse_key):
                    return days
        return 0

    def _delivery_hours(self, item):
        if item.get("delivery_hours") not in (None, ""):
            try:
                return int(item.get("delivery_hours"))
            except (TypeError, ValueError):
                pass
        if item.get("delivery_total_hours") not in (None, ""):
            try:
                return int(item.get("delivery_total_hours"))
            except (TypeError, ValueError):
                pass
        try:
            return int(float(item.get("days", 999999) or 0) * 24)
        except (TypeError, ValueError):
            return 999999 * 24

    def _supplier_delivery_hours(self, item):
        for key in ("delivery_total_hours", "delivery_hours", "delivery_time"):
            if item.get(key) in (None, ""):
                continue
            try:
                return max(0, int(math.ceil(float(item.get(key)))))
            except (TypeError, ValueError):
                continue
        try:
            return max(0, int(math.ceil(float(item.get("days", 0) or 0) * 24)))
        except (TypeError, ValueError):
            return 0

    def _format_delivery_hours(self, hours):
        try:
            hours = max(0, int(hours))
        except (TypeError, ValueError):
            return ""
        if hours <= 24:
            return f"{hours} ч"
        return f"{math.ceil(hours / 24)} дн."

    def _apply_warehouse_extra_days(self, display_name, item):
        extra_hours = self._warehouse_extra_for_item(display_name, item)
        supplier_hours = self._supplier_delivery_hours(item)
        supplier_days = max(0, math.ceil(supplier_hours / 24)) if supplier_hours else 0
        total_hours = supplier_hours + max(0, int(extra_hours or 0))
        original_display = str(
            item.get("delivery_text_original")
            or item.get("delivery_display")
            or item.get("delivery_text")
            or ""
        ).strip()
        item["supplier_days"] = supplier_days
        item["supplier_hours"] = supplier_hours
        item["warehouse_extra_hours"] = max(0, int(extra_hours or 0))
        item["delivery_total_hours"] = total_hours
        item["delivery_hours"] = total_hours
        if original_display and not item.get("delivery_text_original"):
            item["delivery_text_original"] = original_display
        item["delivery_display"] = self._format_delivery_hours(total_hours)

    def _item_has_no_return(self, item):
        # Одна проверка на всё: раньше здесь не видели not_returnable (Фаворит,
        # Форум), BackPercent=-1 (Авто-то), RETDAYS=0 (Армтек), return=impossible
        # (Автоформула), и фильтр пропускал треть их предложений.
        from offer_normalizer import is_no_return
        return is_no_return(item)

    def _filter_provider_item(self, display_name, item, *, include_no_return=False, ignore_warehouse_filters=False):
        if self.hide_no_return and not include_no_return and self._item_has_no_return(item):
            return False, "без возврата"
        if ignore_warehouse_filters:
            # Массовая закупка на витрину: настройки складов рассчитаны на
            # обычную работу и скрывают часть самых дешёвых предложений.
            return True, ""
        rules = getattr(self, "provider_filters", {}).get(display_name, {})
        allowed = rules.get("allowed") or []
        excluded = rules.get("excluded") or []
        if allowed and not self._item_matches_filter_values(item, allowed):
            return False, "склад"
        if excluded and self._item_matches_filter_values(item, excluded):
            return False, "склад"
        return True, ""

    def brand_group_key(self, brand):
        if hasattr(self, "brand_aliases"):
            return self.brand_aliases.key(brand)
        return self.clean_num(brand)

    def display_brand_name(self, brand):
        brand = str(brand or "").strip()
        if not brand:
            return ""
        if hasattr(self, "brand_aliases") and hasattr(self.brand_aliases, "title"):
            return self.brand_aliases.title(brand) or brand
        return brand

    def same_brand_group(self, left, right):
        if hasattr(self, "brand_aliases"):
            return self.brand_aliases.same(left, right)
        return self.clean_num(left) == self.clean_num(right)

    def _provider_display_name(self, cls_name):
        return PROVIDER_DISPLAY_NAMES.get(cls_name, cls_name)

    def normalize_item_article(self, item):
        article = self.clean_num(item.get("article", ""))
        if article:
            item["article"] = article
        if item.get("source_code"):
            item["source_code"] = self.clean_num(item["source_code"])
        return item

    def _apply_offer_relation(self, item, clean_article, brand, raw_article, display_name=""):
        """Решаем сами, искомая это деталь или аналог.

        Раньше признак приходил от поставщика (у каждого своя семантика) и мог
        быть только выставлен в True, но никогда не снят, поэтому ошибочный флаг
        доживал до отбора и выбрасывал точное совпадение. Теперь ответ даёт
        сверка артикула и бренда с запросом, а флаг поставщика не участвует.
        """
        is_cross = not is_requested_part(
            item,
            clean_article,
            brand,
            self.clean_num,
            self.same_brand_group,
        )
        item["is_cross"] = is_cross
        if is_cross:
            item.setdefault("source_brand", brand)
            item.setdefault("source_code", raw_article)
            if display_name:
                item.setdefault("cross_source_provider", display_name)
        return item

    def normalize_offer_item(self, item, requested_quantity=1):
        normalized = normalize_offer(
            item,
            self.clean_num,
            self.brand_group_key,
            requested_quantity=requested_quantity,
        )
        item.clear()
        item.update(normalized)
        display_brand = self.display_brand_name(item.get("brand"))
        if display_brand:
            item["display_brand"] = display_brand
        return item

    def _quantity_info(self, item, requested_quantity=1):
        return quantity_from_item(item, requested_quantity)

    def _quantity_limit_for_cart(self, info):
        if info.available_quantity is None or info.availability_is_lower_bound:
            return 0
        return info.available_int()

    def _provider_timeout(self, provider, default=12):
        try:
            return max(1.0, float(getattr(provider, "timeout", default) or default))
        except (TypeError, ValueError):
            return float(default)

    def _provider_cache_key(self, kind, cls_name, raw_article, clean_article, brand="", variant=""):
        return (
            str(kind or ""),
            str(cls_name or ""),
            self.clean_num(raw_article),
            self.clean_num(clean_article),
            self.brand_group_key(brand),
            str(variant or ""),
        )

    def _provider_cache_get(self, key):
        cache = getattr(self, "provider_result_cache", None)
        if not cache:
            return False, None, 0
        return cache.get(key)

    def _provider_cache_set(self, key, value):
        cache = getattr(self, "provider_result_cache", None)
        if not cache:
            return False
        return cache.set(key, value)

    def _provider_result_cacheable(self, rows, message=""):
        if not isinstance(rows, list) or not rows:
            return False
        text = str(message or "").lower()
        if "обработке" in text or "processing" in text or "pending" in text:
            return False
        return not ProviderCircuitBreaker.should_count_failure(text)

    def _format_seconds(self, value):
        try:
            value = float(value)
            return str(int(value)) if value.is_integer() else f"{value:.1f}"
        except (TypeError, ValueError):
            return str(value)

    def _query_provider(self, provider, cls_name, raw_article, clean_article, brand="", use_selected_variants=True, include_crosses=None):
        import time as _time
        adapter = ProviderAdapter(
            retries=0 if cls_name == "TissTmpartsProvider" else 2,
            backoff=0.3,
        )
        t0 = _time.time()
        if use_selected_variants:
            provider_brand = getattr(self, "_selected_brand_variants", {}).get(cls_name, brand)
        else:
            provider_brand = brand
        provider_brand = self.brand_aliases.for_provider(provider_brand, cls_name)
        display_name = self._provider_display_name(cls_name)
        cache_variant = ""
        if cls_name == "AvtotoProvider" and include_crosses is not None:
            cache_variant = f"cross:{int(bool(include_crosses))}"
        cache_key = self._provider_cache_key(
            "offers",
            cls_name,
            raw_article,
            clean_article,
            provider_brand,
            cache_variant,
        )
        cache_hit, cached_result, cache_age = self._provider_cache_get(cache_key)
        if cache_hit:
            try:
                provider.last_message = ""
            except Exception:
                pass
            self._detail_log(
                "provider_cache_hit",
                provider=display_name,
                provider_class=cls_name,
                article=raw_article,
                clean_article=clean_article,
                selected_brand=brand,
                provider_brand=provider_brand,
                cache_variant=cache_variant,
                age_seconds=round(cache_age, 3),
                result_count=len(cached_result or []),
            )
            return cached_result or [], 0.0, True

        def _request():
            if cls_name == "PrLgProvider":
                if not provider_brand:
                    return provider.get_prices_parallel(
                        raw_article,
                        clean_article,
                        "",
                    )
                candidates = self.brand_aliases.candidates_for_provider(
                    provider_brand, cls_name, limit=5
                ) or [provider_brand]
                for candidate in candidates:
                    result = provider.get_prices_parallel(
                        raw_article, clean_article, candidate
                    )
                    if result:
                        self.brand_aliases.remember_provider_name(
                            brand or provider_brand, cls_name, candidate
                        )
                        if candidate != provider_brand:
                            self._log(
                                f"Profit-League: подобрано написание бренда «{candidate}»"
                            )
                        return result
                provider.last_message = (
                    "не найдены предложения; проверены варианты бренда: "
                    + ", ".join(candidates)
                )
                return []
            if cls_name == "RosskoProvider":
                return provider.get_prices(raw_article)
            if cls_name == "ArmtekProvider":
                return provider.get_prices(clean_article, brand=provider_brand or "")
            if cls_name == "MikadoProvider":
                return provider.get_prices(clean_article, brand=provider_brand or "")
            if cls_name == "AbstdProvider":
                return provider.get_prices(clean_article, brand=provider_brand or None)
            if cls_name == "TissTmpartsProvider":
                return provider.get_prices(clean_article, brand=provider_brand or None)
            if cls_name == "TradesoftProvider":
                return provider.get_prices(clean_article, brand=provider_brand or None)
            if isinstance(provider, AbcpSupplierProvider):
                return provider.get_prices(clean_article, brand=provider_brand or None)
            if cls_name == "FavoritProvider":
                return provider.get_prices(clean_article, brand=provider_brand or None)
            if cls_name == "AvtotoProvider":
                return provider.get_prices(
                    clean_article,
                    brand=provider_brand or "",
                    include_crosses=include_crosses,
                )
            return provider.get_prices(clean_article)

        res = adapter.call(_request)
        elapsed = _time.time() - t0
        msg = getattr(provider, "last_message", "")
        if self._provider_result_cacheable(res, msg):
            if self._provider_cache_set(cache_key, res):
                self._detail_log(
                    "provider_cache_store",
                    provider=display_name,
                    provider_class=cls_name,
                    article=raw_article,
                    clean_article=clean_article,
                    selected_brand=brand,
                    provider_brand=provider_brand,
                    cache_variant=cache_variant,
                    result_count=len(res),
                )
        return res, elapsed, False

    def apply_markup(self, price):
        price = float(price)
        if not self.markup_enabled:
            return price, 0, 0
        return calculate_sale_price(
            price,
            self.markup_rules or [],
            self.rounding_mode,
            self.rounding_from,
            self.rounding_to,
            self.current_markup,
        )

    def _provider_cart_state(self, provider_name):
        supported = {
            "Profit-League": (
                "order",
                "Оформить Profit-League",
            ),
            "Фаворит": ("order", "Оформить Фаворит"),
            "Forum-Auto": ("cart", "В корзину Forum-Auto"),
            "Mikado": ("cart", "В корзину Mikado"),
            "ABSTD": ("order", "Оформить ABSTD"),
            "Armtek": ("order", "Оформить Armtek"),
            "Rossko": ("order", "Оформить Rossko"),
            "Avtoto": ("order", "Оформить Avtoto"),
            "TISS": ("order", "Оформить TISS"),
            "Автоформула": ("order", "Оформить Автоформулу"),
        }
        if str(provider_name or "").startswith("Прайс") or provider_name in ("Freno CSV",):
            return "local", "Локальная позиция"
        if provider_name in getattr(self, "abcp_supplier_names", []):
            return "order", f"Оформить {provider_name}"
        return supported.get(provider_name, ("disabled", "Корзина отключена"))

    def _missing_cart_fields(self, provider_name, item):
        required = {
            "Profit-League": ("article_id", "warehouse_id", "code"),
            "Фаворит": ("goods_id", "warehouse_id"),
            "Forum-Auto": ("gid",),
            "Mikado": ("zakaz_code", "stock_id"),
            "ABSTD": ("product_id",),
            "Rossko": ("stock_id",),
            "TISS": ("product_id", "source_id"),
            "Автоформула": ("item_hash",),
        }
        if provider_name in getattr(self, "abcp_supplier_names", []):
            required = {provider_name: ("supplier_code",)}
        missing = [field for field in required.get(provider_name, ()) if not item.get(field)]
        if provider_name == "Avtoto":
            missing = []
            if not item.get("search_id"):
                missing.append("search_id")
            if not (item.get("part_id") or item.get("offer_id")):
                missing.append("part_id")
        return missing
