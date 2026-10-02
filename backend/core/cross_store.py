# -*- coding: utf-8 -*-
"""Накопление пар «запрошенная деталь → аналог» из ответов поставщиков.

Поставщики присылают аналоги при каждом поиске, но до сих пор мы их выбрасывали:
кеш результатов живёт пять минут в памяти, в лог попадают только сэмплы, в заказ —
единственное выбранное предложение. Один прогон заказа из 251 строки даёт около
400 тысяч предложений, из них ~380 тысяч — не точные совпадения.

Поэтому храним не журнал, а свёртку: одна строка на пару «что искали → что
предложили», со счётчиком встреч и списком поставщиков, которые эту связь
подтвердили. Аналог, который дали четыре поставщика, надёжнее того, что дал один.

Запись идёт в фоновом потоке: поиск не должен ждать диск.
"""
import contextlib
import datetime
import json
import os
import queue
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS cross_pairs (
    request_brand_key   TEXT NOT NULL,
    request_article_key TEXT NOT NULL,
    analog_brand_key    TEXT NOT NULL,
    analog_article_key  TEXT NOT NULL,
    request_brand       TEXT NOT NULL DEFAULT '',
    request_article     TEXT NOT NULL DEFAULT '',
    analog_brand        TEXT NOT NULL DEFAULT '',
    analog_article      TEXT NOT NULL DEFAULT '',
    name                TEXT NOT NULL DEFAULT '',
    providers           TEXT NOT NULL DEFAULT '[]',
    confirmations       INTEGER NOT NULL DEFAULT 0,
    seen_count          INTEGER NOT NULL DEFAULT 0,
    ordered_count       INTEGER NOT NULL DEFAULT 0,
    first_seen          TEXT NOT NULL DEFAULT '',
    last_seen           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (request_brand_key, request_article_key, analog_brand_key, analog_article_key)
);
CREATE INDEX IF NOT EXISTS idx_cross_pairs_request
    ON cross_pairs (request_brand_key, request_article_key, confirmations DESC);
CREATE INDEX IF NOT EXISTS idx_cross_pairs_analog
    ON cross_pairs (analog_brand_key, analog_article_key);
"""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class CrossStore:
    """Хранилище пар аналогов. Пишет в фоне, читает синхронно."""

    def __init__(self, path, async_writes=True, queue_limit=64):
        self.path = str(path or "")
        self.last_error = ""
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=max(1, int(queue_limit))) if async_writes else None
        self._worker = None
        self._closed = False
        self._dropped = 0
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._prepare()
        if self._queue is not None:
            self._worker = threading.Thread(target=self._run_worker, daemon=True)
            self._worker.start()

    # ---------- запись ----------

    def record(self, request_brand, request_article, offers, *, clean_num, brand_key, now=None):
        """Складывает аналоги одного поиска. Возвращает число пар в порции."""
        pairs = self.build_pairs(
            request_brand, request_article, offers,
            clean_num=clean_num, brand_key=brand_key, now=now,
        )
        if not pairs:
            return 0
        if self._queue is None:
            self._write(pairs)
            return len(pairs)
        try:
            self._queue.put_nowait(pairs)
        except queue.Full:
            # Очередь забита — теряем порцию, но не тормозим поиск.
            # Данные накопительные, пропуск одной порции ничего не ломает.
            self._dropped += 1
            return 0
        return len(pairs)

    @staticmethod
    def build_pairs(request_brand, request_article, offers, *, clean_num, brand_key, now=None):
        request_article_key = str(clean_num(request_article) or "")
        request_brand_key = str(brand_key(request_brand) or "")
        if not request_article_key or not request_brand_key:
            return []
        stamp = now or _now()
        pairs = {}
        for offer in offers or []:
            if not isinstance(offer, dict):
                continue
            analog_article = str(offer.get("article") or offer.get("original_article") or "").strip()
            analog_brand = str(offer.get("brand") or offer.get("normalized_brand") or "").strip()
            analog_article_key = str(clean_num(analog_article) or "")
            analog_brand_key = str(brand_key(analog_brand) or "")
            if not analog_article_key or not analog_brand_key:
                continue
            if analog_article_key == request_article_key and analog_brand_key == request_brand_key:
                continue  # точное совпадение — это не аналог
            key = (request_brand_key, request_article_key, analog_brand_key, analog_article_key)
            provider = str(offer.get("provider_name") or offer.get("provider") or "").strip()
            row = pairs.get(key)
            if row is None:
                row = {
                    "request_brand": str(request_brand or "").strip(),
                    "request_article": str(request_article or "").strip(),
                    "analog_brand": analog_brand,
                    "analog_article": analog_article,
                    "name": str(offer.get("name") or "").strip(),
                    "providers": set(),
                    "seen": stamp,
                }
                pairs[key] = row
            if provider:
                row["providers"].add(provider)
            if not row["name"]:
                row["name"] = str(offer.get("name") or "").strip()
        return [(key, row) for key, row in pairs.items()]

    def flush(self, timeout=10.0):
        """Ждёт, пока фоновый писатель разберёт очередь."""
        if self._queue is None:
            return True
        deadline = threading.Event()
        self._queue.put((deadline, None))
        return deadline.wait(timeout)

    def close(self):
        self._closed = True
        if self._queue is not None:
            try:
                self._queue.put_nowait((None, None))
            except queue.Full:
                pass

    # ---------- чтение ----------

    def analogs_for(self, brand, article, *, clean_num, brand_key, limit=20, min_confirmations=1):
        article_key = str(clean_num(article) or "")
        request_brand_key = str(brand_key(brand) or "")
        if not article_key or not request_brand_key:
            return []
        rows = self._query(
            "SELECT analog_brand, analog_article, name, providers, confirmations, seen_count,"
            " ordered_count, first_seen, last_seen"
            " FROM cross_pairs"
            " WHERE request_brand_key = ? AND request_article_key = ? AND confirmations >= ?"
            " ORDER BY confirmations DESC, seen_count DESC, analog_brand, analog_article"
            " LIMIT ?",
            (request_brand_key, article_key, int(min_confirmations), int(limit)),
        )
        return [
            {
                "brand": row[0], "article": row[1], "name": row[2],
                "providers": json.loads(row[3] or "[]"),
                "confirmations": row[4], "seen_count": row[5], "ordered_count": row[6],
                "first_seen": row[7], "last_seen": row[8],
            }
            for row in rows
        ]

    def stats(self):
        rows = self._query(
            "SELECT COUNT(*), COALESCE(SUM(seen_count), 0), COALESCE(MAX(confirmations), 0),"
            " COALESCE(SUM(CASE WHEN confirmations >= 2 THEN 1 ELSE 0 END), 0)"
            " FROM cross_pairs",
            (),
        )
        pairs, seen, best, confirmed = rows[0] if rows else (0, 0, 0, 0)
        return {
            "pairs": int(pairs or 0),
            "seen_total": int(seen or 0),
            "max_confirmations": int(best or 0),
            "confirmed_pairs": int(confirmed or 0),
            "dropped_batches": self._dropped,
            "path": self.path,
        }

    # ---------- внутреннее ----------

    def _prepare(self):
        try:
            with self._connect() as connection:
                connection.executescript(SCHEMA)
        except sqlite3.Error as exc:
            self.last_error = str(exc)

    @contextlib.contextmanager
    def _connect(self):
        """Соединение на операцию, обязательно с закрытием: на Windows
        незакрытый файл базы не даёт её ни удалить, ни перенести."""
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _run_worker(self):
        while True:
            batch = self._queue.get()
            if isinstance(batch, tuple) and len(batch) == 2 and not isinstance(batch[0], tuple):
                event, _ = batch
                if event is None:
                    return
                event.set()
                continue
            try:
                self._write(batch)
            except Exception as exc:  # писатель не должен падать вместе с потоком
                self.last_error = str(exc)

    def _write(self, pairs):
        if not pairs:
            return
        with self._lock:
            try:
                with self._connect() as connection:
                    existing = self._existing_providers(connection, [key for key, _row in pairs])
                    rows = []
                    for key, row in pairs:
                        providers = set(existing.get(key) or ()) | set(row["providers"])
                        rows.append((
                            key[0], key[1], key[2], key[3],
                            row["request_brand"], row["request_article"],
                            row["analog_brand"], row["analog_article"], row["name"],
                            json.dumps(sorted(providers), ensure_ascii=False),
                            len(providers), row["seen"], row["seen"],
                        ))
                    connection.executemany(
                        "INSERT INTO cross_pairs ("
                        " request_brand_key, request_article_key, analog_brand_key, analog_article_key,"
                        " request_brand, request_article, analog_brand, analog_article, name,"
                        " providers, confirmations, seen_count, first_seen, last_seen"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
                        " ON CONFLICT (request_brand_key, request_article_key, analog_brand_key, analog_article_key)"
                        " DO UPDATE SET"
                        "  providers = excluded.providers,"
                        "  confirmations = excluded.confirmations,"
                        "  seen_count = cross_pairs.seen_count + 1,"
                        "  name = CASE WHEN excluded.name != '' THEN excluded.name ELSE cross_pairs.name END,"
                        "  analog_brand = excluded.analog_brand,"
                        "  analog_article = excluded.analog_article,"
                        "  last_seen = excluded.last_seen",
                        rows,
                    )
            except sqlite3.Error as exc:
                self.last_error = str(exc)

    def _existing_providers(self, connection, keys):
        existing = {}
        for chunk_start in range(0, len(keys), 400):
            chunk = keys[chunk_start:chunk_start + 400]
            placeholders = ",".join(["(?, ?, ?, ?)"] * len(chunk))
            flat = [value for key in chunk for value in key]
            cursor = connection.execute(
                "SELECT request_brand_key, request_article_key, analog_brand_key, analog_article_key, providers"
                " FROM cross_pairs WHERE (request_brand_key, request_article_key, analog_brand_key,"
                f" analog_article_key) IN ({placeholders})",
                flat,
            )
            for row in cursor.fetchall():
                try:
                    existing[(row[0], row[1], row[2], row[3])] = json.loads(row[4] or "[]")
                except ValueError:
                    existing[(row[0], row[1], row[2], row[3])] = []
        return existing

    def _query(self, sql, params):
        with self._lock:
            try:
                with self._connect() as connection:
                    return connection.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                self.last_error = str(exc)
                return []
