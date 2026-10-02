"""Веб-сервис Pricer: поиск позиции по всем поставщикам с прогрессом в реальном времени.

Запуск (из папки backend):
    PRICER_SETTINGS=/путь/к/settings.json uvicorn app.server:app --reload

Переменные окружения:
    PRICER_SETTINGS  — settings.json десктопа (секреты; в репозиторий не кладётся).
    PRICER_DATA_DIR  — папка справочника брендов (по умолчанию backend/var/brands,
                       при первом запуске копируется из backend/data).
    PRICER_REPLAY    — 1 или путь к записи .jsonl.gz: демонстрация без сети на записанных
                       ответах поставщиков (с тестовыми настройками из tests/fixtures).

Это первый шаг веб-версии: один движок на процесс, поиски выполняются по очереди.
"""
import asyncio
import json
import os
import shutil
import sys
import threading
import time
import uuid

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(BACKEND, "core"), os.path.join(BACKEND, "integrations")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
RESULT_LIMIT = 200
OFFER_FIELDS = (
    "provider", "brand", "display_brand", "article", "name", "price", "sale_price", "available_quantity",
    "actual_order_quantity", "delivery_hours", "delivery_text", "warehouse", "is_cross", "returnable",
    "internal_offer_id",
)


class SearchRequest(BaseModel):
    brand: str = Field(min_length=1, max_length=80)
    article: str = Field(min_length=1, max_length=80)
    quantity: int = Field(default=1, ge=1, le=10000)
    with_analogs: bool = False
    strategy: str = Field(default="price", pattern="^(price|fastest|price_within_days)$")
    max_days: int | None = Field(default=None, ge=1, le=365)


class Job:
    def __init__(self, request):
        self.id = uuid.uuid4().hex
        self.request = request
        self.events = []
        self.done = False
        self.created = time.time()

    def push(self, kind, data):
        self.events.append({"event": kind, "data": data})


def _brand_dir():
    target = os.environ.get("PRICER_DATA_DIR") or os.path.join(BACKEND, "var", "brands")
    os.makedirs(target, exist_ok=True)
    for name in ("brand_groups.json", "brand_provider_mappings.json", "brands.txt"):
        dst = os.path.join(target, name)
        if not os.path.exists(dst):
            shutil.copy(os.path.join(BACKEND, "data", name), dst)
    return target


def _load_settings(replay_mode):
    path = os.environ.get("PRICER_SETTINGS")
    if not path and replay_mode:
        path = os.path.join(BACKEND, "tests", "fixtures", "recordings", "engine_settings.json")
    if not path:
        return None
    with open(path, encoding="utf-8") as file:
        return json.load(file)


class _Patch:
    """Мини-аналог pytest.MonkeyPatch для режима демонстрации."""

    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def build_engine():
    import brand_aliases

    replay_value = os.environ.get("PRICER_REPLAY", "")
    replay_mode = bool(replay_value)
    replayer = None
    brand_dir = _brand_dir()
    brand_aliases.get_brand_storage_dir = lambda: brand_dir
    os.environ.setdefault("PROCENKA_CONFIG_DIR", os.path.join(BACKEND, "var", "config"))
    if replay_mode:
        import armtek
        import favorit

        from app import replay

        records = replay.load(replay_value if replay_value not in ("1", "true") else "search-2026-10-02.jsonl.gz")
        replayer = replay.install(_Patch(), records, reuse=True)
        time.sleep = lambda *_: None  # опрос Avtoto в записи не ждёт
        favorit.datetime = replay.FrozenDateTime
        armtek.datetime.datetime = replay.FrozenDateTime

    from engine import PROVIDER_DISPLAY_NAMES, ProcurementEngine

    engine = ProcurementEngine(_load_settings(replay_mode), brand_aliases=brand_aliases.BrandAliasResolver())
    engine.replay_mode = replay_mode
    engine.replayer = replayer
    engine.provider_names = [
        PROVIDER_DISPLAY_NAMES.get(type(p).__name__, type(p).__name__) for p in engine.providers
    ]
    return engine


def _offer(item):
    item = item or {}
    return {field: item.get(field) for field in OFFER_FIELDS if field in item}


def _result_payload(result):
    alternatives = result.get("alternatives") or []
    return {
        "status_code": result.get("status_code"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "quantity": result.get("quantity"),
        "offer": _offer(result.get("offer")) if result.get("offer") else None,
        "alternatives": [_offer(o) for o in alternatives[:RESULT_LIMIT]],
        "alternatives_total": len(alternatives),
    }


def create_app():
    app = FastAPI(title="Pricer", version="0.1.0")
    state = {"engine": None, "lock": threading.Lock(), "jobs": {}}

    def engine():
        if state["engine"] is None:
            state["engine"] = build_engine()
        return state["engine"]

    def run(job):
        eng = engine()
        with state["lock"]:
            eng.on_provider_progress = lambda stat: job.push("provider", {
                key: stat.get(key) for key in ("provider", "status", "reason", "count", "raw_count", "elapsed", "message")
            })
            try:
                if eng.replayer is not None:
                    eng.replayer.reset()
                    eng.provider_result_cache.clear()  # иначе повтор возьмёт кэш, а не запись
                req = job.request
                result = eng.search_order_row(
                    {"brand": req.brand, "article": req.article, "name": "", "quantity": req.quantity},
                    {
                        "exact_match": not req.with_analogs,
                        "include_no_return": False,
                        "ignore_warehouse_filters": False,
                        "strategy": req.strategy,
                        "max_days": req.max_days,
                    },
                )
                job.push("result", _result_payload(result))
            except Exception as exc:  # ошибка показывается пользователю, сервис продолжает работать
                job.push("error", {"message": f"{type(exc).__name__}: {exc}"[:300]})
            finally:
                eng.on_provider_progress = None
                job.done = True

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/api/status")
    def status():
        eng = engine()
        return {"providers": eng.provider_names, "replay_mode": eng.replay_mode}

    @app.post("/api/search")
    def start_search(request: SearchRequest):
        job = Job(request)
        state["jobs"][job.id] = job
        # Старые поиски не копим: в памяти держим последние 50.
        for old in sorted(state["jobs"].values(), key=lambda j: j.created)[:-50]:
            state["jobs"].pop(old.id, None)
        threading.Thread(target=run, args=(job,), daemon=True).start()
        return {"job_id": job.id, "providers": engine().provider_names}

    @app.get("/api/search/{job_id}/events")
    async def events(job_id: str):
        job = state["jobs"].get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="поиск не найден")

        async def stream():
            sent = 0
            while True:
                while sent < len(job.events):
                    item = job.events[sent]
                    sent += 1
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                if job.done and sent >= len(job.events):
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app()
