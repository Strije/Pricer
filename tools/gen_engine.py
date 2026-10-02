import ast, re, subprocess, sys
S = sys.argv[1]; OUT = sys.argv[2]
src = open(f"{S}/src/main.py", encoding="utf-8").read(); L = src.split("\n")
tree = ast.parse(src)
cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SkitchenApp"][0]
methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
graph = subprocess.run([sys.executable, f"{S}/callgraph.py", f"{S}/src/main.py", "_search_order_file_row",
                        "_parse_filter_list", "_parse_warehouse_days"], capture_output=True, text=True).stdout
names = [l.split()[2] for l in graph.splitlines() if l.strip() and l.split()[0].isdigit()]
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
body = body.replace("if not self.markup_toggle.isChecked():", "if not self.markup_enabled:")
out = f'''"""Движок поиска для заказа из файла, вынесенный из main.py (SkitchenApp) без Qt.

Методы перенесены дословно из десктопа (версия 1.0.3), чтобы поведение не разошлось:
опрос поставщиков, нормализация, фильтры складов и возврата, точный отбор, ранжирование,
наценка и итог по строке. Замены относительно оригинала:
- сигнал интерфейса log_signal.emit -> обратный вызов log;
- флажок наценки в боковой панели -> атрибут markup_enabled;
- настройка поставщиков из load_settings -> configure(settings).
Сгенерировано скриптом из main.py; правки логики делаются уже здесь, с тестами.
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

{top("PROVIDER_DISPLAY_NAMES")}


{top("_abcp_supplier_configured")}


{top("_tradesoft_configured")}


def default_settings():
{default_body}
    return default


class ProcurementEngine:
    def __init__(self, settings=None, *, brand_aliases=None, cross_store=None, detailed_logger=None,
                 log=None, providers=None):
        self.settings = dict(settings or default_settings())
        self.brand_aliases = brand_aliases or BrandAliasResolver()
        self.cross_store = cross_store
        self.detailed_logger = detailed_logger
        self._log_callback = log
        self.provider_result_cache = ProviderResultCache()
        self.provider_circuit = ProviderCircuitBreaker(threshold=5, cooldown_seconds=45)
        self._order_file_search_id = 0
        self._selected_brand_variants = {{}}
        self.configure(self.settings)
        if providers is not None:
            self.providers = list(providers)

    def _log(self, text):
        if self._log_callback:
            self._log_callback(text)

    def search_order_row(self, row, options=None):
        """Подбор одной строки заказа: то же, что кнопка «Файл заказа» в десктопе."""
        self._order_file_search_id += 1
        return self._search_order_file_row(row, options or {{}}, search_id=self._order_file_search_id)

    def configure(self, cfg):
        default = default_settings()
{configure_body}
        self.markup_enabled = bool(self.markup_rules) or float(self.current_markup or 0) > 0

'''
out += "\n".join(("" if not l.strip() else l) for l in body.split("\n")) + "\n"
open(OUT, "w", encoding="utf-8").write(out)
print("methods", len(set(names)))
