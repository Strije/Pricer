import ast, re, subprocess, sys
S = sys.argv[1]; OUT = sys.argv[2]  # S — папка, где лежит src/main.py и callgraph.py
src = open(f"{S}/src/main.py", encoding="utf-8").read(); L = src.split("\n")
tree = ast.parse(src)
cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SkitchenApp"][0]
methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
ROOTS = [
    "_search_order_file_row", "_parse_filter_list", "_parse_warehouse_days",
    # заказы: черновик из файла, перепроверка, отправка, ручные действия с позицией
    "_prepared_order_file_entry", "_order_entries_with_prices", "_entries_verified_at",
    "_recheck_order_thread", "_submit_order_thread_guarded", "_submittable_order_items",
    "_mark_order_stale_if_expired", "_order_submit_idempotency_key", "_try_lock_order_submit",
    "_skip_order_item", "_restore_order_item", "_replace_order_item_with_variant",
    # корзина: добавление с запасом вариантов, статус позиции
    "_add_item_to_draft_cart", "_cart_status_text",
    # интерактивный поиск «Проценки»: бренды по ответам поставщиков, опрос всех, кроссы, ★
    "_resolve_brand_and_run", "run_query",
]
# Методы интерфейса: вместо них в движке свои реализации (см. шапку класса ниже).
UI_METHODS = {"add_log", "_refresh_orders_page", "_refresh_article_suggestion_orders",
              "_refresh_draft_cart_table", "_update_draft_cart_summary", "_refresh_pricing_cart_marks"}
graph = subprocess.run([sys.executable, f"{S}/callgraph.py", f"{S}/src/main.py", *ROOTS,
                        "--stop=" + ",".join(sorted(UI_METHODS))],
                       capture_output=True, text=True).stdout
names = [l.split()[2] for l in graph.splitlines() if l.strip() and l.split()[0].isdigit()]
names = [n for n in names if n not in UI_METHODS]
def block(a, b): return "\n".join(L[a - 1:b])
def top(name):
    n = [x for x in tree.body if getattr(x, "name", None) == name or
         (isinstance(x, ast.Assign) and any(getattr(t, "id", None) == name for t in x.targets))][0]
    return block(n.lineno, n.end_lineno)
import textwrap
_ls = methods["load_settings"]
_dn = [x for x in ast.walk(_ls) if isinstance(x, ast.Assign) and getattr(x.targets[0], "id", None) == "default"][0]
default_body = textwrap.indent(textwrap.dedent(block(_dn.lineno, _dn.end_lineno)), "    ")
configure_body = block(885, 1146) + "\n" + block(1165, 1170)
copied = []
for m in sorted(set(names), key=lambda m: methods[m].lineno):
    n = methods[m]
    copied.append(block(n.lineno, n.end_lineno))
body = "\n\n".join(copied)
body = body.replace("self.log_signal.emit(", "self._log(")
# Сигналы окна поиска -> обратные вызовы движка (веб получает их через SSE).
for _sig in ("search_results_ready", "search_provider_status", "search_progress", "search_completed_for"):
    body = body.replace(f"self.{_sig}.emit(", f"self._{_sig}(")
# Выбор бренда: в десктопе — диалог посреди потока поиска; в вебе — отдельный шаг. Метод
# возвращает варианты (BrandChoice) и статистику, а поиск по выбранному запускает search_offers.
_tail = """        if not labels:
            self._selected_brand_variants = {}
            self.run_query(search_id, raw_article, clean_article, "")
            return
        self._brand_selection_event = threading.Event()
        self._selected_brand = ""
        self.brand_selection_requested.emit(raw_article, labels)
        self._brand_selection_event.wait()
        if not self.search_state.is_current(search_id):
            return
        self.run_query(search_id, raw_article, clean_article, self._selected_brand)"""
assert body.count(_tail) == 1, "gen_engine: конец _resolve_brand_and_run изменился"
body = body.replace(_tail, "        return choices, stats")
body = body.replace("def _resolve_brand_and_run(", "def _resolve_brand_choices(")
body = body.replace("self.order_action_finished.emit(", "self._order_action_finished(")
# Исправление ошибки десктопа 1.0.3: в _apply_variant_to_order_item нет переменной offer
# (NameError при замене варианта позиции в заказе). Имя берём у нового варианта, иначе прежнее.
_bug = '"name": str(offer.get("name") or offer.get("source_name") or ""),'
assert _bug in body, "исправление offer->variant больше не применимо, проверьте main.py"
body = body.replace(_bug, '"name": str(variant.get("name") or variant.get("source_name") or order_item.get("name") or ""),')
body = body.replace("if not self.markup_toggle.isChecked():", "if not self.markup_enabled:")
_c = 'comment = str(order.get("comment") or "")'
assert body.count(_c) == 1, "gen_engine: строка комментария к отправке не найдена"
body = body.replace(_c, 'comment = supplier_comment(order_id, order.get("comment"))')
out = f'''"""Движок поиска для заказа из файла, вынесенный из main.py (SkitchenApp) без Qt.

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
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from abcp_reference import AbcpReferenceProvider
from abcp_supplier import AbcpSupplierProvider, abcp_supplier_class
from abstd import AbstdProvider
from armtek import ArmtekProvider
from avtoto import AvtotoProvider
from brand_aliases import BrandAliasResolver
from brand_resolver import BrandResolver
from bulk_order import filter_exact_offers, rank_offers, status_for_selection
from cross_targets import UNKNOWN_DELIVERY_HOURS, build_cross_targets
from favorit import FavoritProvider
from forum_auto import ForumAutoProvider
from mikado import MikadoProvider
from offer_normalizer import (deduplicate_offers, is_requested_part, mark_best_offers, normalize_offer,
                              offer_sort_key, offer_unique_key)
from order_quantity import quantity_from_item
from order_store import OrderStore
from pr_lg import PrLgProvider
from pricing import DEFAULT_MARKUP_RULES, calculate_sale_price
from provider_adapter import ProviderAdapter
from provider_health import ProviderCircuitBreaker
from provider_result_cache import ProviderResultCache
from result_limiter import DEFAULT_PROVIDER_LIMITS, limit_provider_results, normalize_provider_limits
from rossko import RosskoProvider
from search_state import SearchState
from tiss_tmparts import TissTmpartsProvider
from tradesoft import TradesoftProvider
from url_csv_provider import UrlCsvProvider

{top("PROVIDER_DISPLAY_NAMES")}

{top("CROSS_RELATION_CODES")}
{top("ORDER_VERIFICATION_TTL_SECONDS")}
{top("ORDER_RECHECK_WORKERS")}
{top("PROCESSED_SUBMIT_STATUSES")}
{top("DRAFT_GROUP_OFFER_LIMIT")}


{top("_abcp_supplier_configured")}


{top("_tradesoft_configured")}


def default_settings():
{default_body}
    return default


def supplier_comment(order_id, comment):
    """Комментарий поставщику с меткой нашего заказа (ORD-…) в начале: по ней статус из личного
    кабинета находит позицию однозначно (как метки в 1С); в начале — чтобы пережить обрезку длины
    (у Росско 50 знаков). Текст оператора сохраняется после метки."""
    comment = str(comment or "").strip()
    label = str(order_id or "").strip()
    if not label or label in comment:
        return comment
    return f"{{label}} {{comment}}".strip()


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
        self.brand_resolver = BrandResolver(self.brand_aliases)
        self.cross_store = cross_store
        self.detailed_logger = detailed_logger
        self._log_callback = log
        self.provider_result_cache = ProviderResultCache()
        self.provider_circuit = ProviderCircuitBreaker(threshold=5, cooldown_seconds=45)
        self._order_file_search_id = 0
        self._selected_brand_variants = {{}}
        self.search_state = SearchState()
        self.on_search_event = None  # (вид, данные): results / provider / progress — для SSE
        self._search_lock = threading.Lock()
        self.on_provider_progress = None
        self.on_order_action = None
        self.order_store = order_store
        self.order_history = order_history
        self.configure(self.settings)
        if providers is not None:
            self.providers = list(providers)

    # ----- интерактивный поиск (вкладка «Поиск») -----

    def _emit_search(self, kind, data):
        if self.on_search_event:
            try:
                self.on_search_event(kind, data)
            except Exception:
                pass  # сбой показа не должен ломать поиск

    def _search_results_ready(self, search_id, results):
        self._last_search_results = list(results or [])
        self._emit_search("results", self._last_search_results)

    def _search_provider_status(self, search_id, name, status):
        self._emit_search("provider", {{"provider": name, "status": status}})

    def _search_progress(self, search_id, value):
        self._emit_search("progress", int(value))

    def _search_completed_for(self, search_id):
        self.search_state.finish(search_id)

    def search_brands(self, article):
        """Шаг 1: какие бренды знают поставщики для артикула. Возвращает варианты с голосами:
        [{{label, brand, votes, providers, choice}}], где votes — сколько поставщиков назвали бренд."""
        raw = str(article or "").strip()
        clean = self.clean_num(raw)
        search_id, _ = self.search_state.start("brands:" + clean + ":" + str(time.monotonic()))
        resolved = self._resolve_brand_choices(search_id, raw, clean)
        self.search_state.finish(search_id)
        choices, stats = resolved or ([], {{}})
        answered = sorted(name for name, count in (stats or {{}}).items() if count)
        out = []
        for choice in choices:
            providers = sorted({{c.provider for c in choice.candidates if c.provider}})
            names = sorted({{c.name for c in choice.candidates if c.name}}, key=len)
            out.append({{"label": choice.label, "brand": choice.canonical_brand, "votes": len(providers),
                        "providers": providers, "name": names[0] if names else "", "choice": choice}})
        out.sort(key=lambda row: -row["votes"])  # устойчиво: при равенстве — порядок десктопа
        return out, answered

    def search_offers(self, article, choice=None):
        """Шаг 2: поиск у всех поставщиков по артикулу и выбранному бренду (BrandChoice или None),
        затем кроссы по локальным прайсам. Возвращает итоговую выдачу (как таблица десктопа)."""
        raw = str(article or "").strip()
        clean = self.clean_num(raw)
        brand = ""
        self._selected_brand_variants = {{}}
        if choice is not None:
            brand = choice.canonical_brand
            self._selected_brand_variants = dict(choice.provider_brands)
            for provider_name, provider_brand in self._selected_brand_variants.items():
                self.brand_aliases.remember_provider_name(brand, provider_name, provider_brand)
        self._last_search_results = []
        search_id, _ = self.search_state.start("offers:" + clean + ":" + str(time.monotonic()))
        # Невозвратные не прячем: в вебе у них признак, а скрыть их — переключатель в выдаче
        # (по умолчанию — как настройка hide_no_return).
        hide, self.hide_no_return = self.hide_no_return, False
        try:
            self.run_query(search_id, raw, clean, brand)
        finally:
            self.hide_no_return = hide
        return list(self._last_search_results)

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

    def _refresh_draft_cart_table(self):
        pass

    def _update_draft_cart_summary(self):
        pass

    def _refresh_pricing_cart_marks(self):
        pass

    def _order_action_finished(self, order, message):
        self._log(message)
        if self.on_order_action:
            self.on_order_action(order, message)

    def search_order_row(self, row, options=None):
        """Подбор одной строки заказа: то же, что кнопка «Файл заказа» в десктопе."""
        self._order_file_search_id += 1
        return self._search_order_file_row(row, options or {{}}, search_id=self._order_file_search_id)

    def configure(self, cfg):
        default = default_settings()
{configure_body}
        self.markup_enabled = bool(self.markup_rules) or float(self.current_markup or 0) > 0

'''
body = body.replace("        provider_stats = []\n",
                    "        provider_stats = _ProgressList(getattr(self, \"on_provider_progress\", None))\n", 1)
out += "\n".join(("" if not l.strip() else l) for l in body.split("\n")) + "\n"
open(OUT, "w", encoding="utf-8").write(out)
print("methods", len(set(names)))
