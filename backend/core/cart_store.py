class DraftCart:
    def __init__(self):
        self._entries = []

    def rows(self):
        return list(self._entries)

    def clear(self):
        self._entries.clear()

    def remove_keys(self, keys):
        keys = {str(key) for key in keys if key is not None}
        if not keys:
            return 0
        before = len(self._entries)
        self._entries = [entry for entry in self._entries if str(entry.get("key")) not in keys]
        return before - len(self._entries)

    def remove_rows(self, rows):
        rows = {int(row) for row in rows if row is not None}
        if not rows:
            return 0
        before = len(self._entries)
        self._entries = [entry for idx, entry in enumerate(self._entries) if idx not in rows]
        return before - len(self._entries)

    def remove_matching(self, item):
        item = item or {}
        internal_offer_id = str(item.get("internal_offer_id") or "")
        provider = str(item.get("provider") or "")
        article = str(item.get("article") or "")
        brand = str(item.get("brand") or "")
        warehouse = str(item.get("warehouse") or item.get("logo") or "")
        before = len(self._entries)
        self._entries = [
            entry for entry in self._entries
            if not self._same_item(entry.get("item") or {}, internal_offer_id, provider, brand, article, warehouse)
        ]
        return before - len(self._entries)

    @staticmethod
    def _same_item(item, internal_offer_id, provider, brand, article, warehouse):
        if internal_offer_id:
            item_offer_id = str(item.get("internal_offer_id") or "")
            if item_offer_id:
                return item_offer_id == internal_offer_id
        return (
            str(item.get("provider") or "") == provider
            and str(item.get("article") or "") == article
            and str(item.get("brand") or "") == brand
            and str(item.get("warehouse") or item.get("logo") or "") == warehouse
        )

    def add(self, key, item, qty, stock=0):
        qty = max(1, int(qty or 1))
        stock = max(0, int(stock or 0))
        for entry in self._entries:
            if entry["key"] == key:
                before = int(entry.get("qty") or 0)
                next_qty = before + qty
                entry["qty"] = min(next_qty, stock) if stock > 0 else next_qty
                return max(0, entry["qty"] - before)
        actual_qty = min(qty, stock) if stock > 0 else qty
        self._entries.append({"key": key, "item": dict(item), "qty": actual_qty})
        return actual_qty

    def set_quantity(self, key, item, qty, stock=0):
        qty = max(1, int(qty or 1))
        stock = max(0, int(stock or 0))
        actual_qty = min(qty, stock) if stock > 0 else qty
        for entry in self._entries:
            if entry["key"] == key:
                before = int(entry.get("qty") or 0)
                entry["item"] = dict(item)
                entry["qty"] = actual_qty
                return max(0, actual_qty - before)
        self._entries.append({"key": key, "item": dict(item), "qty": actual_qty})
        return actual_qty

    def count(self):
        return sum(int(entry.get("qty") or 0) for entry in self._entries)

    def total(self, price_func):
        amount = 0.0
        for entry in self._entries:
            item = entry["item"]
            try:
                price = float(item.get("price") or 0)
            except (TypeError, ValueError):
                price = 0
            amount += float(price_func(price)) * int(entry.get("qty") or 0)
        return amount
