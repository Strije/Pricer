"""Прайс-листы: источники (ссылка, FTP; почта — отдельным шагом), загрузка на сервере по расписанию,
разметка колонок, хранение строк в базе и поиск по ним как по обычному поставщику.

Скачивание, FTP-каталоги, архивы (ZIP, GZ, 7z), CSV/XLSX и угадывание колонок по заголовкам —
из десктопа (core/url_csv_provider.py: _load_rows, _normalize_row), чтобы разбор совпадал.
Новое: строки — в базе с индексом «организация + артикул» вместо файлового кэша, у каждого
источника своё расписание, разметка колонок вручную, отчёт загрузки, устаревание.
"""
import datetime
import os
import re
from urllib.parse import unquote, urlparse

from sqlalchemy import delete, insert

from app import db
from url_csv_provider import UrlCsvProvider

# Поля строки прайса (как читает десктоп): (код, подпись, обязательное)
FIELDS = [("article", "Артикул", True), ("brand", "Бренд", False), ("name", "Наименование", False),
          ("price", "Цена", True), ("quantity", "Остаток", False), ("multiplicity", "Кратность", False),
          ("days", "Срок поставки", False), ("warehouse", "Склад", False)]
# Заголовки, которые десктоп узнаёт сам (url_csv_provider._normalize_row)
ALIASES = {
    "article": ["article", "art", "articul", "partnumber", "code", "number", "articlecode", "code1", "артикул"],
    "brand": ["brand", "manuf", "manufacturer", "producer", "brandname", "производитель", "бренд"],
    "name": ["name", "title", "description", "itemname", "productname", "номенклатура", "наименование"],
    "price": ["price", "cost", "price_rub", "price_rur", "price_rus", "value", "цена"],
    "quantity": ["quantity", "qty", "stockqty", "count", "available", "остаток"],
    "multiplicity": ["multiplicity", "minqty", "min_count", "кратность"],
    "days": ["days", "delivery", "delivery_days", "srok", "days_to_delivery", "срок"],
    "warehouse": ["warehouse", "warehouse_name", "stock", "storage", "supplier", "place", "склад"],
}
WARN_DAYS = 2   # прайс не обновлялся — предупреждение
STALE_DAYS = 7  # старше — в выдачу не попадает
INSERT_CHUNK = 2000
SCHEDULES = [1, 2, 4, 6, 12, 24, 48, 168]


def article_key(value):
    """Как в десктопе (_normalize_article): только латиница и цифры, без регистра."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())[:80]


def location_hint(location):
    """Что показать вместо адреса: схема, сервер и имя файла — без логина, пароля и токенов."""
    parsed = urlparse(str(location or ""))
    name = os.path.basename(unquote(parsed.path.rstrip("/"))) or "/"
    return f"{parsed.scheme}://{parsed.hostname or '?'}/…/{name}"[:300] if parsed.scheme else "?"


def _provider(location, source_settings, name="Прайс", timeout=90):
    settings = source_settings or {}
    profile = {"name": name, "enabled": True, "has_header": settings.get("has_header", "auto"),
               "column_map": dict(settings.get("column_map") or {}), "default_days": settings.get("default_days", "")}
    return UrlCsvProvider(urls=[location], name=name, timeout=timeout, column_map=profile["column_map"],
                          default_days=settings.get("default_days") or 0, url_profiles={location: profile},
                          cache_hours=1)


def read_rows(location, source_settings, name="Прайс"):
    """Скачать и разобрать файл: строки «заголовок -> значение» (или «номер колонки -> значение»)."""
    provider = _provider(location, source_settings, name)
    rows = provider._load_rows(location, refresh=True)
    if rows is None:
        raise RuntimeError(provider.last_message or "не удалось загрузить прайс")
    return provider, rows


def guess_mapping(headers):
    """Какая колонка за какое поле — по названиям заголовков, как угадывает десктоп."""
    def norm(value):
        return re.sub(r"[^a-zа-я0-9]+", "", str(value or "").lower())
    out = {}
    for field, aliases in ALIASES.items():
        keys = [norm(a) for a in aliases]
        for header in headers:
            h = norm(header)
            if h and (h in keys or any(h.startswith(k) for k in keys if len(k) >= 4)) and header not in out.values():
                out[field] = header
                break
    return out


def preview(location, source_settings, limit=20):
    _, rows = read_rows(location, source_settings)
    headers = []
    for row in rows[:200]:
        for key in row:
            if key not in headers and not str(key).startswith("__"):
                headers.append(key)
    return {"headers": headers, "rows": [[str(row.get(h, ""))[:80] for h in headers] for row in rows[:limit]],
            "total": len(rows), "guess": guess_mapping(headers),
            "mapping": dict((source_settings or {}).get("column_map") or {})}


def normalize(provider, rows, location, warehouse=""):
    """Строки файла -> предложения движка; отчёт: сколько принято и почему пропущены остальные."""
    items, reasons = [], {}
    for row in rows:
        item = provider._normalize_row(row, location)
        if item is None:
            reasons["нет артикула"] = reasons.get("нет артикула", 0) + 1
            continue
        if not item.get("price") or float(item["price"]) <= 0:
            reasons["нет цены"] = reasons.get("нет цены", 0) + 1
            continue
        if warehouse and not item.get("warehouse"):
            item["warehouse"] = item["logo"] = warehouse
        item.pop("source_url", None)  # адрес с паролем в предложение не кладём
        items.append(item)
    return items, reasons


def load(session, box, source):
    """Загрузка одного источника: скачать, разобрать, заменить строки в базе, записать отчёт."""
    started = datetime.datetime.now()
    location = box.open(source.location_sealed).get("location", "") if source.location_sealed else ""
    try:
        if not location:
            raise RuntimeError("не указан адрес прайса")
        provider, rows = read_rows(location, source.settings, source.name)
        items, reasons = normalize(provider, rows, location, (source.settings or {}).get("warehouse", ""))
        if rows and not items:
            raise RuntimeError("ни одной строки не разобрано — проверьте колонки (артикул и цена)")
        session.execute(delete(db.PriceRow).where(db.PriceRow.source_id == source.id))
        values = [{"organization_id": source.organization_id, "source_id": source.id,
                   "article_key": article_key(item.get("article")), "brand": str(item.get("brand") or "")[:100],
                   "item": item} for item in items]
        for start in range(0, len(values), INSERT_CHUNK):
            session.execute(insert(db.PriceRow), values[start:start + INSERT_CHUNK])
        source.loaded_at = db.utcnow()
        status = {"ok": True, "rows": len(items), "skipped": sum(reasons.values()), "reasons": reasons,
                  "message": f"загружено {len(items)} строк" + (f", пропущено {sum(reasons.values())}" if reasons else "")}
    except Exception as exc:
        status = {"ok": False, "message": str(exc)[:300]}
    status["at"] = db.utcnow().isoformat(timespec="seconds")
    status["seconds"] = round((datetime.datetime.now() - started).total_seconds(), 1)
    source.status = status
    session.commit()
    return status


def due(source, now=None):
    if not source.enabled:
        return False
    if source.loaded_at is None:
        return not (source.status or {}).get("at") or _retry_due(source, now)
    return (now or db.utcnow()) - source.loaded_at >= datetime.timedelta(hours=max(1, source.schedule_hours or 24))


def _retry_due(source, now):
    """После ошибки не долбим поставщика каждую минуту: повтор через час."""
    try:
        at = datetime.datetime.fromisoformat(source.status["at"])
    except (KeyError, TypeError, ValueError):
        return True
    return (now or db.utcnow()) - at >= datetime.timedelta(hours=1)


def age_days(source, now=None):
    return None if source.loaded_at is None else ((now or db.utcnow()) - source.loaded_at).total_seconds() / 86400


def view(source):
    age = age_days(source)
    state = ("off" if not source.enabled else "bad" if (source.status or {}).get("ok") is False and source.loaded_at is None
             else "stale" if age is not None and age > STALE_DAYS else "warn" if age is not None and age > WARN_DAYS
             else "ok" if source.loaded_at else "new")
    return {"id": source.id, "name": source.name, "kind": source.kind, "enabled": source.enabled,
            "location_hint": source.location_hint, "has_location": bool(source.location_sealed),
            "settings": source.settings or {}, "schedule_hours": source.schedule_hours, "status": source.status or {},
            "loaded_at": source.loaded_at.isoformat(timespec="minutes") if source.loaded_at else None,
            "age_days": round(age, 1) if age is not None else None, "state": state}


def import_desktop(session, box, organization_id, settings):
    """Источники из раздела url_csv десктопа (urls + url_profiles): названия, сроки, вкл./выкл."""
    urls = [str(u).strip() for u in (settings.get("urls") or []) if str(u).strip()]
    profiles = settings.get("url_profiles") or {}
    existing = {box.open(s.location_sealed).get("location") for s in session.query(db.PriceSource)
                .filter_by(organization_id=organization_id) if s.location_sealed}
    added = 0
    for url in urls:
        if url in existing:
            continue
        profile = profiles.get(url) or {}
        name = str(profile.get("name") or "").strip() or os.path.basename(urlparse(url).path).rsplit(".", 1)[0] or "Прайс"
        session.add(db.PriceSource(
            organization_id=organization_id, name=name[:200], kind="ftp" if url.lower().startswith("ftp") else "url",
            enabled=profile.get("enabled", True) is not False, location_sealed=box.seal({"location": url}),
            location_hint=location_hint(url), schedule_hours=max(1, int(settings.get("cache_hours") or 24)),
            settings={"has_header": profile.get("has_header", "auto"), "column_map": profile.get("column_map") or {},
                      "default_days": profile.get("default_days") or settings.get("default_days") or ""}))
        added += 1
    session.commit()
    return added


class PriceDbProvider(UrlCsvProvider):
    """Поставщик движка «Прайс-листы»: ищет в загруженных прайсах организации (вместо файлов десктопа).
    Подкласс UrlCsvProvider — чтобы поиск аналогов по локальным прайсам (_search_crosses) шёл и сюда."""

    DISPLAY_NAME = "Прайс-листы"

    def __init__(self, sessionmaker, organization_id):
        super().__init__(urls=["db://prices"], name="Прайс-листы")
        self.sessionmaker, self.organization_id = sessionmaker, organization_id

    def _rows(self, keys):
        fresh = db.utcnow() - datetime.timedelta(days=STALE_DAYS)
        with self.sessionmaker() as session:
            return (session.query(db.PriceRow.article_key, db.PriceRow.item)
                    .join(db.PriceSource, db.PriceSource.id == db.PriceRow.source_id)
                    .filter(db.PriceRow.organization_id == self.organization_id, db.PriceRow.article_key.in_(list(keys)),
                            db.PriceSource.enabled.is_(True), db.PriceSource.loaded_at >= fresh).limit(5000).all())

    def get_prices(self, article):
        self.last_message = ""
        key = article_key(article)
        if not key:
            return []
        rows = [dict(item) for _, item in self._rows([key])]
        if not rows:
            self.last_message = ""  # пусто — не ошибка
        return rows

    def get_prices_many(self, articles):
        wanted = {}
        for article in articles or []:
            key = article_key(article)
            if key:
                wanted.setdefault(key, []).append(str(article))
        out = {}
        for key, item in self._rows(wanted):
            for alias in wanted.get(key, []):
                out.setdefault(alias, []).append(dict(item))
        return out

    def get_brand_candidates(self, article):
        seen, out = set(), []
        for item in self.get_prices(article):
            brand = str(item.get("brand") or "").strip()
            marker = (brand.upper(), str(item.get("provider") or ""))
            if brand and marker not in seen:
                seen.add(marker)
                out.append({"brand": brand, "article": item.get("article") or article, "name": item.get("name") or "",
                            "provider": item.get("provider") or self.name, "source": "прайс-лист"})
        return out

    def get_brands(self, article):
        return list(dict.fromkeys(c["brand"] for c in self.get_brand_candidates(article)))
