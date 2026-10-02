import datetime
import json
import os
import threading


class OrderHistoryStore:
    def __init__(self, path, limit=1000):
        self.path = path
        self.limit = max(1, int(limit or 1000))
        self._lock = threading.Lock()

    def append(self, item, quantity, response):
        record = self._record(item or {}, quantity, response)
        with self._lock:
            rows = self.rows()
            rows.append(record)
            rows = rows[-self.limit:]
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp_path = f"{self.path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as file:
                json.dump(rows, file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        return record

    def rows(self):
        try:
            with open(self.path, encoding="utf-8") as file:
                rows = json.load(file)
            return rows if isinstance(rows, list) else []
        except FileNotFoundError:
            return []
        except Exception:
            return []

    @staticmethod
    def _record(item, quantity, response):
        return {
            "internal_offer_id": str(item.get("internal_offer_id") or ""),
            "supplier_offer_id": str(item.get("supplier_offer_id") or ""),
            "provider": str(item.get("provider_name") or item.get("provider") or ""),
            "brand": str(item.get("brand") or item.get("normalized_brand") or ""),
            "article": str(item.get("article") or item.get("original_article") or ""),
            "warehouse": str(item.get("warehouse") or item.get("logo") or ""),
            "price": item.get("purchase_price", item.get("price")),
            "quantity": int(quantity or 0),
            "submitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "response": response,
        }
