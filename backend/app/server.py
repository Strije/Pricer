"""Веб-сервис Pricer: организации, вход, поставщики организации и поиск с прогрессом.

Запуск (из папки backend):
    uvicorn app.server:app --port 8000

Переменные окружения:
    PRICER_VAR_DIR        — рабочая папка (база, ключ шифрования, бренды); по умолчанию backend/var.
    PRICER_DATABASE_URL   — база; по умолчанию SQLite в PRICER_VAR_DIR/pricer.db.
    PRICER_SECRET_KEY     — ключ шифрования секретов поставщиков (Fernet). Если не задан, создаётся
                            файл PRICER_VAR_DIR/secret.key — его нельзя терять и нельзя публиковать.
    PRICER_ALLOW_SIGNUP   — 0, чтобы запретить регистрацию новых организаций.
    PRICER_SECURE_COOKIES — 1 при работе по HTTPS.
    PRICER_REPLAY         — 1 или путь к записи .jsonl.gz: демонстрация без сети на записанных
                            ответах; организации без поставщиков получают тестовые настройки.
"""
import asyncio
import datetime
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

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app import db  # noqa: E402
from app import supplier_catalog as catalog  # noqa: E402
from app.security import (  # noqa: E402
    SecretBox, hash_password, load_master_key, new_token, token_hash, verify_password,
)

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "pricer_session"
SESSION_DAYS = 14
CSRF_HEADER = "x-requested-with"
RESULT_LIMIT = 200
OFFER_FIELDS = (
    "provider", "brand", "display_brand", "article", "name", "price", "sale_price", "available_quantity",
    "actual_order_quantity", "delivery_hours", "delivery_text", "warehouse", "is_cross", "returnable",
    "internal_offer_id",
)
SETTINGS_UPLOAD_LIMIT = 2 * 1024 * 1024


# ---------- схемы запросов ----------

class SearchRequest(BaseModel):
    brand: str = Field(min_length=1, max_length=80)
    article: str = Field(min_length=1, max_length=80)
    quantity: int = Field(default=1, ge=1, le=10000)
    with_analogs: bool = False
    strategy: str = Field(default="price", pattern="^(price|fastest|price_within_days)$")
    max_days: int | None = Field(default=None, ge=1, le=365)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(default="", max_length=200)
    organization: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class AccountRequest(BaseModel):
    section: str | None = None
    config: dict = Field(default_factory=dict)
    # Пустая строка или отсутствие поля — оставить сохранённое; null — удалить значение.
    secrets: dict = Field(default_factory=dict)


# ---------- вспомогательное ----------

class Job:
    def __init__(self, request, organization_id):
        self.id = uuid.uuid4().hex
        self.request = request
        self.organization_id = organization_id
        self.events = []
        self.done = False
        self.created = time.time()

    def push(self, kind, data):
        self.events.append({"event": kind, "data": data})


class _Patch:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)


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


def _account_view(account, box):
    secrets = box.open(account.secrets_sealed)
    title = catalog.CATALOG.get(account.section, {}).get("title", account.section)
    return {
        "id": account.id,
        "section": account.section,
        "title": title,
        "name": (account.config or {}).get("name") or title,
        "config": account.config or {},
        # Наружу уходят только имена заполненных секретных полей, не значения.
        "secrets_set": sorted(key for key, value in secrets.items() if value not in (None, "", [], {})),
    }


def create_app(var_dir=None, database_url=None):
    var_dir = var_dir or os.environ.get("PRICER_VAR_DIR") or os.path.join(BACKEND, "var")
    os.makedirs(var_dir, exist_ok=True)
    database_url = (database_url or os.environ.get("PRICER_DATABASE_URL")
                    or f"sqlite:///{os.path.join(var_dir, 'pricer.db')}")
    Session = db.make_sessionmaker(database_url)
    box = SecretBox(load_master_key(var_dir))
    allow_signup = os.environ.get("PRICER_ALLOW_SIGNUP", "1") != "0"
    secure_cookies = os.environ.get("PRICER_SECURE_COOKIES", "0") == "1"
    state = {"engines": {}, "jobs": {}, "brand_aliases": None, "replayer": None, "lock": threading.Lock()}

    # ----- справочник брендов и режим демонстрации -----

    import brand_aliases as brand_aliases_module

    brand_dir = os.environ.get("PRICER_DATA_DIR") or os.path.join(var_dir, "brands")
    os.makedirs(brand_dir, exist_ok=True)
    for name in ("brand_groups.json", "brand_provider_mappings.json", "brands.txt"):
        dst = os.path.join(brand_dir, name)
        if not os.path.exists(dst):
            shutil.copy(os.path.join(BACKEND, "data", name), dst)
    brand_aliases_module.get_brand_storage_dir = lambda: brand_dir
    os.environ.setdefault("PROCENKA_CONFIG_DIR", os.path.join(var_dir, "config"))

    replay_value = os.environ.get("PRICER_REPLAY", "")
    replay_mode = bool(replay_value)
    demo_settings = None
    if replay_mode:
        import armtek
        import favorit

        from app import replay

        name = replay_value if replay_value not in ("1", "true") else "search-2026-10-02.jsonl.gz"
        state["replayer"] = replay.install(_Patch(), replay.load(name), reuse=True)
        time.sleep = lambda *_: None  # опрос Avtoto в записи не ждёт
        favorit.datetime = replay.FrozenDateTime
        armtek.datetime.datetime = replay.FrozenDateTime
        demo_path = os.path.join(BACKEND, "tests", "fixtures", "recordings", "engine_settings.json")
        with open(demo_path, encoding="utf-8") as file:
            demo_settings = json.load(file)

    def brand_resolver():
        if state["brand_aliases"] is None:
            state["brand_aliases"] = brand_aliases_module.BrandAliasResolver()
        return state["brand_aliases"]

    # ----- настройки организации -> движок -----

    def org_settings(session, organization_id):
        org = session.get(db.Organization, organization_id)
        accounts = [(a.section, a.config, box.open(a.secrets_sealed)) for a in org.accounts]
        if replay_mode and not accounts:
            return demo_settings
        return catalog.compose_settings(org.settings or {}, accounts)

    def invalidate(organization_id):
        state["engines"].pop(organization_id, None)

    def engine_for(organization_id):
        cached = state["engines"].get(organization_id)
        if cached is not None:
            return cached
        from engine import PROVIDER_DISPLAY_NAMES, ProcurementEngine

        with Session() as session:
            settings = org_settings(session, organization_id)
        engine = ProcurementEngine(settings, brand_aliases=brand_resolver())
        engine.provider_names = [PROVIDER_DISPLAY_NAMES.get(type(p).__name__, type(p).__name__)
                                 for p in engine.providers]
        engine.lock = threading.Lock()
        state["engines"][organization_id] = engine
        return engine

    # ----- сессии -----

    def start_session(response, user_id):
        token = new_token()
        with Session() as session:
            session.add(db.Session(token_hash=token_hash(token), user_id=user_id,
                                   expires_at=db.utcnow() + datetime.timedelta(days=SESSION_DAYS)))
            session.commit()
        response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="lax", secure=secure_cookies)

    def current_user(request: Request):
        token = request.cookies.get(COOKIE)
        if not token:
            raise HTTPException(status_code=401, detail="нужно войти")
        with Session() as session:
            row = session.get(db.Session, token_hash(token))
            if row is None or row.expires_at < db.utcnow():
                raise HTTPException(status_code=401, detail="сессия истекла, войдите снова")
            user = session.get(db.User, row.user_id)
            return {"id": user.id, "email": user.email, "name": user.name, "role": user.role,
                    "organization_id": user.organization_id, "organization": user.organization.name}

    def admin_user(user=Depends(current_user)):
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="нужны права администратора организации")
        return user

    app = FastAPI(title="Pricer", version="0.2.0")

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # Изменяющие запросы принимаем только со своим заголовком: чужой сайт не может его
        # добавить без разрешения CORS, поэтому подделать запрос из браузера пользователя нельзя.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
            if request.headers.get(CSRF_HEADER, "").lower() != "pricer":
                return JSONResponse({"detail": "запрос отклонён (нет заголовка X-Requested-With)"}, status_code=403)
        return await call_next(request)

    # ----- страницы -----

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    # ----- вход -----

    @app.post("/api/auth/register")
    def register(payload: RegisterRequest, response: Response):
        if not allow_signup:
            raise HTTPException(status_code=403, detail="регистрация закрыта")
        email = payload.email.strip().lower()
        with Session() as session:
            if session.query(db.User).filter_by(email=email).first():
                raise HTTPException(status_code=409, detail="такой email уже зарегистрирован")
            org = db.Organization(name=payload.organization.strip(), settings={})
            user = db.User(email=email, name=payload.name.strip(), password_hash=hash_password(payload.password),
                           role="admin", organization=org)
            session.add_all([org, user])
            session.commit()
            user_id = user.id
        start_session(response, user_id)
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, response: Response):
        with Session() as session:
            user = session.query(db.User).filter_by(email=payload.email.strip().lower()).first()
            if user is None or not verify_password(payload.password, user.password_hash):
                raise HTTPException(status_code=401, detail="неверный email или пароль")
            user_id = user.id
        start_session(response, user_id)
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(COOKIE)
        if token:
            with Session() as session:
                row = session.get(db.Session, token_hash(token))
                if row:
                    session.delete(row)
                    session.commit()
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        return {**user, "providers": engine.provider_names, "replay_mode": replay_mode}

    @app.get("/api/public")
    def public_info():
        return {"allow_signup": allow_signup, "replay_mode": replay_mode}

    # ----- поставщики организации -----

    @app.get("/api/catalog")
    def get_catalog(user=Depends(current_user)):
        return catalog.public_catalog()

    @app.get("/api/suppliers")
    def list_suppliers(user=Depends(current_user)):
        with Session() as session:
            org = session.get(db.Organization, user["organization_id"])
            return [_account_view(a, box) for a in sorted(org.accounts, key=lambda a: a.id)]

    def _apply_account(account, payload):
        config = dict(account.config or {})
        for key, value in (payload.config or {}).items():
            if key in catalog.secret_fields(account.section):
                continue  # секретное поле в открытой части не храним
            config[key] = value
        secrets = box.open(account.secrets_sealed)
        for key, value in (payload.secrets or {}).items():
            if value is None:
                secrets.pop(key, None)
            elif value != "":
                secrets[key] = value
        account.config = config
        account.secrets_sealed = box.seal(secrets)

    @app.post("/api/suppliers")
    def create_supplier(payload: AccountRequest, user=Depends(admin_user)):
        if payload.section not in catalog.CATALOG:
            raise HTTPException(status_code=400, detail="неизвестный поставщик")
        with Session() as session:
            existing = session.query(db.SupplierAccount).filter_by(
                organization_id=user["organization_id"], section=payload.section).first()
            if existing and not catalog.CATALOG[payload.section].get("multiple"):
                raise HTTPException(status_code=409, detail="этот поставщик уже подключён")
            account = db.SupplierAccount(organization_id=user["organization_id"], section=payload.section,
                                         config={"enabled": True}, secrets_sealed=box.seal({}))
            _apply_account(account, payload)
            session.add(account)
            session.commit()
            view = _account_view(account, box)
        invalidate(user["organization_id"])
        return view

    @app.put("/api/suppliers/{account_id}")
    def update_supplier(account_id: int, payload: AccountRequest, user=Depends(admin_user)):
        with Session() as session:
            account = session.get(db.SupplierAccount, account_id)
            if account is None or account.organization_id != user["organization_id"]:
                raise HTTPException(status_code=404, detail="не найдено")
            _apply_account(account, payload)
            session.commit()
            view = _account_view(account, box)
        invalidate(user["organization_id"])
        return view

    @app.delete("/api/suppliers/{account_id}")
    def delete_supplier(account_id: int, user=Depends(admin_user)):
        with Session() as session:
            account = session.get(db.SupplierAccount, account_id)
            if account is None or account.organization_id != user["organization_id"]:
                raise HTTPException(status_code=404, detail="не найдено")
            session.delete(account)
            session.commit()
        invalidate(user["organization_id"])
        return {"ok": True}

    @app.post("/api/import/settings")
    async def import_settings(file: UploadFile = File(...), user=Depends(admin_user)):
        raw = await file.read(SETTINGS_UPLOAD_LIMIT + 1)
        if len(raw) > SETTINGS_UPLOAD_LIMIT:
            raise HTTPException(status_code=413, detail="файл слишком большой")
        try:
            settings = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(settings, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="это не settings.json")
        org_part, accounts = catalog.split_settings(settings)
        with Session() as session:
            org = session.get(db.Organization, user["organization_id"])
            org.settings = {**(org.settings or {}), **org_part}
            org.accounts.clear()
            for section, config, secrets in accounts:
                org.accounts.append(db.SupplierAccount(section=section, config=config,
                                                       secrets_sealed=box.seal(secrets)))
            session.commit()
        invalidate(user["organization_id"])
        return {"ok": True, "accounts": len(accounts), "settings": sorted(org_part)}

    @app.get("/api/org/settings")
    def get_org_settings(user=Depends(current_user)):
        with Session() as session:
            return session.get(db.Organization, user["organization_id"]).settings or {}

    @app.put("/api/org/settings")
    def put_org_settings(payload: dict, user=Depends(admin_user)):
        unknown = set(payload) - set(catalog.ORG_KEYS)
        if unknown:
            raise HTTPException(status_code=400, detail=f"неизвестные настройки: {', '.join(sorted(unknown))}")
        with Session() as session:
            org = session.get(db.Organization, user["organization_id"])
            org.settings = {**(org.settings or {}), **payload}
            session.commit()
            result = org.settings
        invalidate(user["organization_id"])
        return result

    # ----- поиск -----

    def run(job):
        engine = engine_for(job.organization_id)
        with engine.lock:
            engine.on_provider_progress = lambda stat: job.push("provider", {
                key: stat.get(key) for key in ("provider", "status", "reason", "count", "raw_count", "elapsed", "message")
            })
            try:
                if state["replayer"] is not None:
                    with state["lock"]:
                        state["replayer"].reset()
                    engine.provider_result_cache.clear()  # иначе повтор возьмёт кэш, а не запись
                req = job.request
                result = engine.search_order_row(
                    {"brand": req.brand, "article": req.article, "name": "", "quantity": req.quantity},
                    {"exact_match": not req.with_analogs, "include_no_return": False,
                     "ignore_warehouse_filters": False, "strategy": req.strategy, "max_days": req.max_days},
                )
                job.push("result", _result_payload(result))
            except Exception as exc:  # ошибка показывается пользователю, сервис продолжает работать
                job.push("error", {"message": f"{type(exc).__name__}: {exc}"[:300]})
            finally:
                engine.on_provider_progress = None
                job.done = True

    @app.post("/api/search")
    def start_search(request: SearchRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        if not engine.providers:
            raise HTTPException(status_code=400, detail="у организации не подключено ни одного поставщика")
        job = Job(request, user["organization_id"])
        state["jobs"][job.id] = job
        for old in sorted(state["jobs"].values(), key=lambda j: j.created)[:-200]:
            state["jobs"].pop(old.id, None)
        threading.Thread(target=run, args=(job,), daemon=True).start()
        return {"job_id": job.id, "providers": engine.provider_names}

    @app.get("/api/search/{job_id}/events")
    async def events(job_id: str, user=Depends(current_user)):
        job = state["jobs"].get(job_id)
        if job is None or job.organization_id != user["organization_id"]:
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


def __getattr__(name):
    # uvicorn app.server:app — приложение создаётся при первом обращении, а не при импорте модуля
    # (тесты создают своё через create_app с временной папкой).
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
