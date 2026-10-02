import datetime
import json
import math
import os
import re
import threading
from collections.abc import Mapping


class OrderStore:
    def __init__(self, orders_dir):
        self.orders_dir = orders_dir
        self._lock = threading.Lock()
        self._summary_cache = {}

    def create_draft(self, manager, client, ship_date, entries, comment="", verified_at="", client_vin="", grouping_mode="manual"):
        with self._lock:
            os.makedirs(self.orders_dir, exist_ok=True)
            order_id = self._next_order_id()
            now = _now()
            entry_rows = list(entries or [])
            items = [self._item_from_entry(entry) for entry in entry_rows]
            verified_at = str(verified_at or "")
            if verified_at:
                for item in items:
                    item["verification_status"] = "valid"
                    item["verification_message"] = "проверено при подборе"
                    item["last_checked_at"] = verified_at
            elif items and all(
                str(item.get("verification_status") or "") == "valid"
                and str(item.get("last_checked_at") or "")
                for item in items
            ):
                verified_at = min(str(item.get("last_checked_at") or "") for item in items)
            grouping_mode = str(grouping_mode or "manual")
            order = {
                "order_id": order_id,
                "status": "draft",
                "created_at": now,
                "updated_at": now,
                "grouping_mode": grouping_mode,
                "manager": {
                    "id": _manager_id(manager),
                    "name": str(manager or "").strip(),
                },
                "client": {
                    "name": str(client or "").strip(),
                    "phone": "",
                    "vin": _normalize_vin(client_vin),
                    "comment": "",
                },
                "client_ship_date": str(ship_date or ""),
                "calculated_ready_date": self.calculated_ready_date(entry_rows),
                "comment": str(comment or ""),
                "verification_status": "valid" if verified_at and items else "stale",
                "verified_at": verified_at,
                "items": items,
                "groups": [] if grouping_mode == "file" else self.build_groups(entry_rows),
                "totals": self.calculate_totals(items),
                "supplier_totals": self.calculate_supplier_totals(items),
                "provider_results": [],
            }
            self._write(order)
            return _json_safe(order)

    def list_orders(self):
        rows = []
        for name in sorted(os.listdir(self.orders_dir)) if os.path.isdir(self.orders_dir) else []:
            if not name.lower().endswith(".json"):
                continue
            order = self.read(name[:-5])
            if order:
                rows.append(order)
        return rows

    def list_order_summaries(self):
        rows = []
        for name in sorted(os.listdir(self.orders_dir)) if os.path.isdir(self.orders_dir) else []:
            if not name.lower().endswith(".json"):
                continue
            summary = self._summary_for_file(os.path.join(self.orders_dir, name))
            if summary:
                rows.append(summary)
        return rows

    def read(self, order_id):
        path = self._path(order_id)
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                return None
            self._ensure_totals(data)
            self._cache_summary(path, data)
            return data
        except Exception:
            return None

    def update(self, order):
        with self._lock:
            order = dict(order or {})
            order["updated_at"] = _now()
            self._refresh_totals(order)
            self._write(order)
            return _json_safe(order)

    def _write(self, order):
        path = self._path(order["order_id"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        safe_order = _json_safe(order)
        try:
            with open(tmp_path, "w", encoding="utf-8") as file:
                json.dump(safe_order, file, ensure_ascii=False, indent=2, allow_nan=False)
            os.replace(tmp_path, path)
            self._cache_summary(path, safe_order)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            finally:
                raise

    def _summary_for_file(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        cache_key = self._summary_cache_key(path)
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        cached = self._summary_cache.get(cache_key)
        if cached and cached[0] == fingerprint:
            return _json_safe(cached[1])
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                return None
            self._ensure_totals(data)
            summary = self.summary_from_order(data)
            self._summary_cache[cache_key] = (fingerprint, summary)
            return _json_safe(summary)
        except Exception:
            self._summary_cache.pop(cache_key, None)
            return None

    def _cache_summary(self, path, order):
        try:
            stat = os.stat(path)
            self._summary_cache[self._summary_cache_key(path)] = (
                (stat.st_mtime_ns, stat.st_size),
                self.summary_from_order(order),
            )
        except OSError:
            self._summary_cache.pop(self._summary_cache_key(path), None)

    @staticmethod
    def _summary_cache_key(path):
        return os.path.normcase(os.path.abspath(path))

    def _path(self, order_id):
        safe = re.sub(r"[^A-Z0-9_-]+", "", str(order_id or "").upper())
        return os.path.join(self.orders_dir, f"{safe}.json")

    def _ensure_totals(self, order):
        items = order.get("items") or []
        if "totals" not in order:
            order["totals"] = self.calculate_totals(items)
        if "supplier_totals" not in order:
            order["supplier_totals"] = self.calculate_supplier_totals(items)

    def _refresh_totals(self, order):
        items = order.get("items") or []
        order["totals"] = self.calculate_totals(items)
        order["supplier_totals"] = self.calculate_supplier_totals(items)

    def _next_order_id(self):
        today = datetime.date.today().strftime("%Y%m%d")
        prefix = f"ORD-{today}-"
        max_number = 0
        if os.path.isdir(self.orders_dir):
            for name in os.listdir(self.orders_dir):
                if not name.startswith(prefix) or not name.lower().endswith(".json"):
                    continue
                try:
                    max_number = max(max_number, int(name[len(prefix):len(prefix) + 4]))
                except ValueError:
                    continue
        return f"{prefix}{max_number + 1:04d}"

    @staticmethod
    def summary_from_order(order):
        order = dict(order or {})
        items = [OrderStore._item_summary(item) for item in order.get("items") or []]
        totals = order.get("totals") or OrderStore.calculate_totals(items)
        supplier_totals = order.get("supplier_totals") or OrderStore.calculate_supplier_totals(items)
        client = dict(order.get("client") or {})
        manager = dict(order.get("manager") or {})
        groups = [
            OrderStore._group_summary(group)
            for group in order.get("groups") or []
        ]
        return _json_safe({
            "summary_only": True,
            "order_id": order.get("order_id"),
            "status": order.get("status"),
            "created_at": order.get("created_at"),
            "updated_at": order.get("updated_at"),
            "grouping_mode": order.get("grouping_mode"),
            "manager": {
                "id": manager.get("id"),
                "name": manager.get("name"),
            },
            "client": {
                "name": client.get("name"),
                "phone": client.get("phone"),
                "vin": client.get("vin"),
                "comment": client.get("comment"),
            },
            "client_ship_date": order.get("client_ship_date"),
            "calculated_ready_date": order.get("calculated_ready_date"),
            "comment": order.get("comment"),
            "verification_status": order.get("verification_status"),
            "verified_at": order.get("verified_at"),
            "items": items,
            "groups": groups,
            "totals": totals,
            "supplier_totals": supplier_totals,
        })

    @staticmethod
    def _item_summary(item):
        item = dict(item or {})
        summary = {}
        for key in (
            "internal_offer_id",
            "supplier_offer_id",
            "provider",
            "brand",
            "display_brand",
            "article",
            "name",
            "warehouse",
            "price",
            "purchase_price",
            "sale_price",
            "purchase_total",
            "sale_total",
            "margin",
            "quantity",
            "requested_quantity",
            "available_quantity",
            "minimum_quantity",
            "quantity_step",
            "package_quantity",
            "delivery_hours",
            "ready_date",
            "snapshot_at",
            "verification_status",
            "verification_message",
            "last_checked_at",
            "submit_status",
            "skip_reason",
        ):
            if key in item:
                summary[key] = item.get(key)
        response = item.get("supplier_response")
        if isinstance(response, dict):
            summary["supplier_response"] = {
                key: response.get(key)
                for key in ("success", "error", "message", "status", "order_id", "external_order_id", "code")
                if key in response
            }
        search = item.get("search")
        if isinstance(search, dict):
            summary["search"] = {
                key: search.get(key)
                for key in ("requested_article", "selected_brand", "requested_name", "provider", "exact_match", "strategy", "max_days")
                if key in search
            }
        return summary

    @staticmethod
    def _group_summary(group):
        group = dict(group or {})
        requested = dict(group.get("requested") or {})
        return {
            "group_id": str(group.get("group_id") or ""),
            "requested": {
                "brand": requested.get("brand"),
                "article": requested.get("article"),
                "article_key": requested.get("article_key"),
                "quantity": requested.get("quantity"),
                "name": requested.get("name"),
            },
            "selected_quantity": group.get("selected_quantity"),
            "offers": [],
        }

    @staticmethod
    def calculated_ready_date(entries):
        max_hours = 0
        for entry in entries or []:
            item = entry.get("item") or {}
            try:
                hours = int(item.get("delivery_hours", item.get("delivery_total_hours", 0)) or 0)
            except (TypeError, ValueError):
                hours = 0
            max_hours = max(max_hours, hours)
        days = int(math.ceil(max_hours / 24)) if max_hours > 0 else 0
        return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

    @staticmethod
    def _item_from_entry(entry):
        item = dict((entry or {}).get("item") or {})
        quantity = int((entry or {}).get("qty") or item.get("actual_order_quantity") or 1)
        purchase_price = _float(item.get("purchase_price", item.get("price")))
        sale_price = _float(item.get("order_sale_price", item.get("sale_price", purchase_price)))
        purchase_total = round(purchase_price * quantity, 2)
        sale_total = round(sale_price * quantity, 2)
        return {
            "internal_offer_id": str(item.get("internal_offer_id") or ""),
            "supplier_offer_id": str(item.get("supplier_offer_id") or ""),
            "provider": str(item.get("provider_name") or item.get("provider") or ""),
            "brand": str(item.get("brand") or item.get("normalized_brand") or ""),
            "display_brand": str(item.get("display_brand") or item.get("brand") or item.get("normalized_brand") or ""),
            "article": str(item.get("article") or item.get("original_article") or ""),
            # Наименование поставщика; если его нет — то, что стояло в файле заказа.
            "name": str(item.get("name") or item.get("source_name") or ""),
            "warehouse": str(item.get("warehouse") or item.get("logo") or ""),
            "price": purchase_price,
            "purchase_price": purchase_price,
            "sale_price": sale_price,
            "purchase_total": purchase_total,
            "sale_total": sale_total,
            "margin": round(sale_total - purchase_total, 2),
            "quantity": quantity,
            "requested_quantity": item.get("requested_quantity"),
            "available_quantity": item.get("available_quantity"),
            "minimum_quantity": item.get("minimum_quantity"),
            "quantity_step": item.get("quantity_step") or item.get("multiplicity"),
            "package_quantity": item.get("package_quantity"),
            "delivery_hours": item.get("delivery_hours", item.get("delivery_total_hours")),
            "ready_date": OrderStore.calculated_ready_date([entry]),
            "snapshot_at": _now(),
            "verification_status": str(item.get("verification_status") or ""),
            "verification_message": str(item.get("verification_message") or ""),
            "last_checked_at": str(item.get("last_checked_at") or ""),
            "submit_status": "not_submitted",
            "supplier_response": None,
            "search": {
                "requested_article": str(item.get("source_code") or item.get("article") or ""),
                "selected_brand": str(item.get("source_brand") or item.get("brand") or ""),
                "requested_name": str(item.get("source_name") or item.get("name") or ""),
                "provider": str(item.get("provider_name") or item.get("provider") or ""),
                "exact_match": item.get("order_file_exact_match"),
                # По этой же стратегии позицию будет выбирать перепроверка.
                "strategy": str(item.get("selection_strategy") or ""),
                "max_days": item.get("selection_max_days"),
            },
            "snapshot": _json_safe(item),
        }

    @staticmethod
    def build_groups(entries):
        groups = []
        index = {}
        offer_index = {}
        for entry in entries or []:
            item = dict((entry or {}).get("item") or {})
            quantity = int((entry or {}).get("qty") or item.get("actual_order_quantity") or 1)
            requested_brand = str(item.get("source_brand") or item.get("brand") or item.get("normalized_brand") or "").strip()
            requested_article = str(item.get("source_code") or item.get("article") or item.get("original_article") or "").strip()
            key = (_key(requested_brand), _key(requested_article))
            if key not in index:
                group = {
                    "group_id": f"REQ-{len(groups) + 1:03d}",
                    "requested": {
                        "brand": requested_brand,
                        "article": requested_article,
                        "article_key": _key(requested_article),
                        "quantity": quantity,
                        "name": str(item.get("source_name") or item.get("name") or "").strip(),
                    },
                    "selected_quantity": 0,
                    "offers": [],
                }
                index[key] = group
                groups.append(group)
            group = index[key]
            group["requested"]["quantity"] = max(int(group["requested"].get("quantity") or 0), quantity)
            if not str(group["requested"].get("name") or "").strip():
                group["requested"]["name"] = str(item.get("source_name") or item.get("name") or "").strip()
            group["selected_quantity"] += quantity
            seen = offer_index.setdefault(key, {})
            for offer in OrderStore._group_offers_from_entry(entry):
                offer_key = OrderStore._group_offer_key(offer)
                if offer_key in seen:
                    existing = group["offers"][seen[offer_key]]
                    if offer.get("selected"):
                        existing["selected"] = True
                        existing["display_in_order"] = True
                        existing["quantity"] = offer.get("quantity", existing.get("quantity"))
                    continue
                seen[offer_key] = len(group["offers"])
                group["offers"].append(offer)
        return _json_safe(groups)

    @staticmethod
    def _group_offers_from_entry(entry):
        item = dict((entry or {}).get("item") or {})
        quantity = int((entry or {}).get("qty") or item.get("actual_order_quantity") or 1)
        selected = OrderStore._group_offer_from_item(item, quantity, selected=True)
        offers = [selected]
        selected_key = OrderStore._group_offer_key(selected)
        for raw_offer in item.get("selection_group_offers") or item.get("draft_group_offers") or []:
            if not isinstance(raw_offer, dict):
                continue
            offer_quantity = int(raw_offer.get("actual_order_quantity") or quantity or 1)
            offer = OrderStore._group_offer_from_item(raw_offer, offer_quantity, selected=False)
            if OrderStore._group_offer_key(offer) == selected_key:
                continue
            offers.append(offer)
        return offers

    @staticmethod
    def _group_offer_from_entry(entry):
        item = dict((entry or {}).get("item") or {})
        quantity = int((entry or {}).get("qty") or item.get("actual_order_quantity") or 1)
        return OrderStore._group_offer_from_item(item, quantity, selected=True)

    @staticmethod
    def _group_offer_from_item(item, quantity, selected=False):
        item = dict(item or {})
        snapshot = dict(item)
        for key in ("selection_group_offers", "draft_group_offers", "group_offers"):
            snapshot.pop(key, None)
        quantity = int(quantity or item.get("actual_order_quantity") or 1)
        purchase_price = _float(item.get("purchase_price", item.get("price")))
        sale_price = _float(item.get("order_sale_price", item.get("sale_price", purchase_price)))
        purchase_total = round(purchase_price * quantity, 2)
        sale_total = round(sale_price * quantity, 2)
        return {
            "selected": bool(selected),
            "display_in_order": bool(selected) or bool(item.get("display_in_order", True)),
            "internal_offer_id": str(item.get("internal_offer_id") or ""),
            "supplier_offer_id": str(item.get("supplier_offer_id") or ""),
            "provider": str(item.get("provider_name") or item.get("provider") or ""),
            "brand": str(item.get("brand") or item.get("normalized_brand") or ""),
            "display_brand": str(item.get("display_brand") or item.get("brand") or item.get("normalized_brand") or ""),
            "article": str(item.get("article") or item.get("original_article") or ""),
            "name": str(item.get("name") or ""),
            "warehouse": str(item.get("warehouse") or item.get("logo") or ""),
            "purchase_price": purchase_price,
            "sale_price": sale_price,
            "purchase_total": purchase_total,
            "sale_total": sale_total,
            "margin": round(sale_total - purchase_total, 2),
            "quantity": quantity,
            "requested_quantity": item.get("requested_quantity"),
            "available_quantity": item.get("available_quantity"),
            "minimum_quantity": item.get("minimum_quantity"),
            "quantity_step": item.get("quantity_step") or item.get("multiplicity"),
            "delivery_hours": item.get("delivery_hours", item.get("delivery_total_hours")),
            "is_cross": bool(item.get("is_cross")),
            "offer_relation": str(item.get("offer_relation") or item.get("cross_relation") or ""),
            "provider_confirm_count": item.get("provider_confirm_count") or item.get("analog_provider_count"),
            "returnable": item.get("returnable"),
            "snapshot_at": _now(),
            "snapshot": _json_safe(snapshot),
        }

    @staticmethod
    def _group_offer_key(offer):
        offer = offer or {}
        internal_offer_id = str(offer.get("internal_offer_id") or "").strip()
        if internal_offer_id:
            return ("internal", internal_offer_id)
        provider = str(offer.get("provider") or "").strip()
        supplier_offer_id = str(offer.get("supplier_offer_id") or "").strip()
        if provider and supplier_offer_id:
            return ("supplier", provider, supplier_offer_id)
        return (
            "natural",
            provider,
            _key(offer.get("brand") or offer.get("display_brand") or ""),
            _key(offer.get("article") or ""),
            str(offer.get("warehouse") or "").strip(),
            str(offer.get("purchase_price", offer.get("price", ""))).replace(" ", "").replace(",", "."),
        )

    @staticmethod
    def calculate_totals(items):
        purchase_total = 0.0
        sale_total = 0.0
        quantity = 0
        for item in items or []:
            qty = int(item.get("quantity") or 0)
            quantity += qty
            purchase_total += _float(item.get("purchase_total", _float(item.get("purchase_price", item.get("price"))) * qty))
            sale_total += _float(item.get("sale_total", _float(item.get("sale_price", item.get("purchase_price", item.get("price"))) * qty)))
        purchase_total = round(purchase_total, 2)
        sale_total = round(sale_total, 2)
        margin = round(sale_total - purchase_total, 2)
        margin_percent = round((margin / purchase_total * 100), 2) if purchase_total else 0.0
        return {
            "items_count": len(items or []),
            "quantity": quantity,
            "purchase_total": purchase_total,
            "sale_total": sale_total,
            "margin": margin,
            "margin_percent": margin_percent,
        }

    @staticmethod
    def calculate_supplier_totals(items):
        grouped = {}
        for item in items or []:
            provider = str(item.get("provider") or "Без поставщика")
            row = grouped.setdefault(provider, {
                "provider": provider,
                "items_count": 0,
                "quantity": 0,
                "purchase_total": 0.0,
                "sale_total": 0.0,
                "margin": 0.0,
                "margin_percent": 0.0,
            })
            qty = int(item.get("quantity") or 0)
            row["items_count"] += 1
            row["quantity"] += qty
            row["purchase_total"] += _float(item.get("purchase_total", _float(item.get("purchase_price", item.get("price"))) * qty))
            row["sale_total"] += _float(item.get("sale_total", _float(item.get("sale_price", item.get("purchase_price", item.get("price"))) * qty)))
        rows = []
        for row in grouped.values():
            row["purchase_total"] = round(row["purchase_total"], 2)
            row["sale_total"] = round(row["sale_total"], 2)
            row["margin"] = round(row["sale_total"] - row["purchase_total"], 2)
            row["margin_percent"] = round((row["margin"] / row["purchase_total"] * 100), 2) if row["purchase_total"] else 0.0
            rows.append(row)
        return sorted(rows, key=lambda row: row["provider"].lower())


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _manager_id(name):
    return re.sub(r"[^A-Z0-9]+", "_", str(name or "").strip().upper()).strip("_").lower()


def _normalize_vin(value):
    return "".join(ch for ch in str(value or "").strip().upper() if ch.isalnum())


def _key(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _float(value, default=0.0):
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def _json_safe(value, seen=None):
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return None
        seen.add(marker)
        result = {}
        for key, item in value.items():
            key = str(key)
            if key.startswith("_"):
                continue
            result[key] = _json_safe(item, seen)
        seen.remove(marker)
        return result
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in seen:
            return []
        seen.add(marker)
        result = [_json_safe(item, seen) for item in value]
        seen.remove(marker)
        return result
    if isinstance(value, set):
        return [_json_safe(item, seen) for item in sorted(value, key=lambda item: str(item))]
    return str(value)
