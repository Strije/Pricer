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
    PRICER_STATUS_REFRESH_MINUTES — как часто сервер сам спрашивает статусы заказов у поставщиков
                            (по умолчанию 30 минут, 0 — только по кнопке; в демо-режиме выключено).
"""
import asyncio
import datetime
import json
import os
import re
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

from app import clients as client_service  # noqa: E402
from app import db  # noqa: E402
from app import orders as order_service  # noqa: E402
from app import supplier_catalog as catalog  # noqa: E402
from app import supplier_lines as lines_service  # noqa: E402
from app.redact import Redactor  # noqa: E402
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
ORDER_FILE_LIMIT = 5 * 1024 * 1024
ORDER_FILE_ROWS = 1000
FILE_ALTERNATIVES = 30


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


class FileRow(BaseModel):
    brand: str = Field(default="", max_length=80)
    article: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=300)
    quantity: int = Field(default=1, ge=1, le=100000)


class FileSearchRequest(BaseModel):
    rows: list[FileRow] = Field(min_length=1, max_length=ORDER_FILE_ROWS)
    with_analogs: bool = False
    include_no_return: bool = False
    strategy: str = Field(default="price", pattern="^(price|fastest|price_within_days)$")
    max_days: int | None = Field(default=None, ge=1, le=365)


class CreateOrderRequest(BaseModel):
    job_id: str
    rows: list[int] = Field(min_length=1)
    selections: dict[str, str] = Field(default_factory=dict)  # номер строки -> internal_offer_id
    client: str = Field(default="", max_length=200)  # можно не указывать, если выбран client_id
    manager: str = Field(default="", max_length=200)
    ship_date: str = Field(default="", max_length=20)
    comment: str = Field(default="", max_length=1000)
    vin: str = Field(default="", max_length=30)
    phone: str = Field(default="", max_length=30)
    client_id: int | None = None
    vehicle_id: int | None = None


class CartAddRequest(BaseModel):
    job_id: str
    internal_offer_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(default=1, ge=1, le=100000)


class CartQuantityRequest(BaseModel):
    quantity: int = Field(ge=1, le=100000)


class CheckoutRequest(BaseModel):
    client: str = Field(default="", max_length=200)  # можно не указывать, если выбран client_id
    manager: str = Field(default="", max_length=200)
    ship_date: str = Field(default="", max_length=20)
    comment: str = Field(default="", max_length=1000)
    vin: str = Field(default="", max_length=30)
    phone: str = Field(default="", max_length=30)
    client_id: int | None = None
    vehicle_id: int | None = None


class ClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=254)
    comment: str = Field(default="", max_length=2000)


class VehicleRequest(BaseModel):
    vin: str = Field(default="", max_length=30)
    plate: str = Field(default="", max_length=20)
    make: str = Field(default="", max_length=60)
    model: str = Field(default="", max_length=60)
    year: int | None = Field(default=None, ge=1950, le=2100)
    comment: str = Field(default="", max_length=2000)


class LineStatusRequest(BaseModel):
    status: str = Field(pattern="^(confirmed|in_transit|arrived|issued|refused|returned|submitted)$")
    text: str = Field(default="", max_length=500)


class ReplaceRequest(BaseModel):
    internal_offer_id: str = Field(min_length=1, max_length=64)


class VinFindRequest(BaseModel):
    query: str = Field(min_length=3, max_length=40)


class VehicleRef(BaseModel):
    catalog: str = Field(max_length=60)
    vehicleId: str = Field(max_length=60)
    ssd: str = Field(max_length=4000)


class VinDetailsRequest(BaseModel):
    vehicle: VehicleRef
    quickGroupId: int | None = None
    query: str | None = Field(default=None, max_length=100)


class ItemsRequest(BaseModel):
    items: list[int] | None = None  # None — все позиции заказа
    confirm: bool = False


# ---------- вспомогательное ----------

class Job:
    def __init__(self, request, organization_id, kind="search"):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.request = request
        self.organization_id = organization_id
        self.events = []
        self.results = []
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


def _order_view(order):
    """Заказ для браузера: без сырых снимков ответов поставщиков (они остаются в базе)."""
    order = dict(order or {})
    order["items"] = [{k: v for k, v in item.items() if k != "snapshot"} for item in order.get("items") or []]
    order["groups"] = [{**g, "offers": [{k: v for k, v in o.items() if k != "snapshot"} for o in g.get("offers") or []]}
                       for g in order.get("groups") or []]
    return order


def _file_row_payload(index, result):
    payload = _result_payload(result)
    payload["alternatives"] = payload["alternatives"][:FILE_ALTERNATIVES]
    payload["index"] = index
    payload["source"] = {k: (result.get("source") or {}).get(k) for k in ("brand", "article", "name", "quantity")}
    return payload


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
        armtek.datetime = replay.frozen_datetime_module()  # только внутри модуля Armtek
        demo_path = os.path.join(BACKEND, "tests", "fixtures", "recordings", "engine_settings.json")
        with open(demo_path, encoding="utf-8") as file:
            demo_settings = json.load(file)

    def replay_module_dummy():
        from app import replay

        return replay.DUMMY

    def brand_resolver():
        if state["brand_aliases"] is None:
            state["brand_aliases"] = brand_aliases_module.BrandAliasResolver()
        return state["brand_aliases"]

    # ----- настройки организации -> движок -----

    def org_settings(session, organization_id):
        org = session.get(db.Organization, organization_id)
        accounts = [(a.section, a.config, box.open(a.secrets_sealed)) for a in org.accounts]
        suppliers = [a for a in accounts if not catalog.CATALOG.get(a[0], {}).get("service")]
        if replay_mode and not suppliers:
            # служебные подключения (Laximo) не поставщики: демо-поставщики остаются
            return {**demo_settings, **{section: {**config, **secrets} for section, config, secrets in accounts}}
        return catalog.compose_settings(org.settings or {}, accounts)

    def invalidate(organization_id):
        state["engines"].pop(organization_id, None)

    def engine_for(organization_id):
        cached = state["engines"].get(organization_id)
        if cached is not None:
            return cached
        engine = build_engine(organization_id)
        state["engines"][organization_id] = engine
        return engine

    def build_engine(organization_id):
        """Новый движок организации (engine_for кэширует его; фоновой проверке статусов нужен свой,
        чтобы не занимать блокировку поиска пользователей)."""
        from engine import PROVIDER_DISPLAY_NAMES, ProcurementEngine

        with Session() as session:
            settings = org_settings(session, organization_id)
            org = session.get(db.Organization, organization_id)
            secret_values = [value for account in org.accounts
                             for value in box.open(account.secrets_sealed).values()]
        if replay_mode:
            secret_values.append(replay_module_dummy())
        redactor = Redactor(secret_values)
        engine = ProcurementEngine(settings, brand_aliases=brand_resolver())
        engine.provider_names = [PROVIDER_DISPLAY_NAMES.get(type(p).__name__, type(p).__name__)
                                 for p in engine.providers]
        engine.lock = threading.Lock()
        engine.order_store = order_service.DbOrderStore(Session, organization_id, redactor=redactor)
        engine.order_history = order_service.DbOrderHistory(Session, organization_id, redactor=redactor)
        engine.redactor = redactor
        return engine

    for org_id, order_id in order_service.recover_interrupted_submits(Session):
        print(f"[pricer] {order_id}: отправка была прервана перезапуском, позиции помечены как unknown")
        with Session() as session:
            row = session.query(db.Order).filter_by(organization_id=org_id, order_id=order_id).first()
            if row is not None:
                lines_service.sync_order(session, org_id, dict(row.data or {}))
                session.commit()

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

    app = FastAPI(title="Pricer", version="0.3.0")
    app.state.pricer = {"state": state, "engine_for": engine_for, "Session": Session}  # для тестов

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

    # ----- фоновые задачи -----

    def replay_reset(engine):
        if state["replayer"] is not None:
            with state["lock"]:
                state["replayer"].reset()
            engine.provider_result_cache.clear()  # иначе повтор возьмёт кэш, а не запись

    def start_job(job, target):
        state["jobs"][job.id] = job
        for old in sorted(state["jobs"].values(), key=lambda j: j.created)[:-200]:
            state["jobs"].pop(old.id, None)

        def runner():
            engine = engine_for(job.organization_id)
            with engine.lock:
                try:
                    target(engine)
                except order_service.OrderActionError as exc:
                    job.push("error", {"message": engine.redactor.text(str(exc))})
                except Exception as exc:  # ошибка показывается пользователю, сервис продолжает работать
                    job.push("error", {"message": engine.redactor.text(f"{type(exc).__name__}: {exc}")[:300]})
                finally:
                    engine.on_provider_progress = None
                    engine.on_order_action = None
                    job.done = True

        threading.Thread(target=runner, daemon=True).start()
        return job

    # ----- поиск -----

    def run(job):
        engine = engine_for(job.organization_id)
        with engine.lock:
            engine.on_provider_progress = lambda stat: job.push("provider", engine.redactor.messages({
                key: stat.get(key) for key in ("provider", "status", "reason", "count", "raw_count", "elapsed", "message")
            }))
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
                job.results = [result]  # для «В корзину»: предложение берём отсюда, не из браузера
                job.push("result", engine.redactor.messages(_result_payload(result)))
            except Exception as exc:  # ошибка показывается пользователю, сервис продолжает работать
                job.push("error", {"message": engine.redactor.text(f"{type(exc).__name__}: {exc}")[:300]})
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

    # ----- заказ из файла -----

    @app.post("/api/order-file/parse")
    async def parse_order_file(file: UploadFile = File(...), user=Depends(current_user)):
        import tempfile

        from sales_report_importer import read_sales_report

        raw = await file.read(ORDER_FILE_LIMIT + 1)
        if len(raw) > ORDER_FILE_LIMIT:
            raise HTTPException(status_code=413, detail="файл больше 5 МБ")
        suffix = os.path.splitext(file.filename or "")[1].lower()
        if suffix not in (".csv", ".txt", ".xlsx", ".xlsm", ".xls"):
            raise HTTPException(status_code=400, detail="поддерживаются CSV, TXT, XLSX и выгрузки 1С в XLS")
        if suffix == ".xls" and not raw.lstrip()[:1] == b"<":
            raise HTTPException(status_code=400, detail="старый двоичный XLS не поддерживается — сохраните файл как XLSX")
        # ignore_cleanup_errors: на Windows файл, который библиотека не закрыла после сбоя, нельзя
        # удалить сразу — это не должно превращать понятную ошибку 400 в 500.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "order" + suffix)
            with open(path, "wb") as handle:
                handle.write(raw)
            try:
                if suffix == ".xls":
                    # Выгрузка 1С: внутри HTML-таблица (читает bulk_pricing десктопа).
                    from bulk_pricing import _read_html_table
                    from sales_report_importer import merge_sales_report_rows, parse_sales_report_rows

                    parsed, errors = parse_sales_report_rows(_read_html_table(path))
                    rows = merge_sales_report_rows(parsed)
                else:
                    rows, errors = read_sales_report(path)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"файл не читается: {exc}"[:300])
        if len(rows) > ORDER_FILE_ROWS:
            raise HTTPException(status_code=400, detail=f"в файле больше {ORDER_FILE_ROWS} строк")
        return {"rows": [{k: r.to_dict()[k] for k in ("brand", "article", "name", "quantity")} for r in rows],
                "errors": list(errors or [])[:50]}

    @app.post("/api/order-file/search")
    def search_order_file(request: FileSearchRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        if not engine.providers:
            raise HTTPException(status_code=400, detail="у организации не подключено ни одного поставщика")
        job = Job(request, user["organization_id"], kind="file")
        options = {"exact_match": not request.with_analogs, "include_no_return": request.include_no_return,
                   "ignore_warehouse_filters": False, "strategy": request.strategy, "max_days": request.max_days}

        def work(engine):
            ready = 0
            for index, row in enumerate(request.rows):
                replay_reset(engine)
                try:
                    result = engine.search_order_row(row.model_dump(), options)
                except Exception as exc:
                    result = {"status_code": "error", "status": "Ошибка", "reason": str(exc)[:240],
                              "offer": None, "alternatives": [], "quantity": row.quantity, "source": row.model_dump()}
                job.results.append(result)
                ready += result.get("status_code") == "ready"
                job.push("row", engine.redactor.messages(_file_row_payload(index, result)))
            job.push("done", {"ready": ready, "total": len(request.rows)})

        start_job(job, work)
        return {"job_id": job.id, "total": len(request.rows)}

    @app.post("/api/orders")
    def create_order(request: CreateOrderRequest, user=Depends(current_user)):
        job = state["jobs"].get(request.job_id)
        if job is None or job.organization_id != user["organization_id"] or job.kind != "file":
            raise HTTPException(status_code=404, detail="подбор не найден — повторите поиск по файлу")
        if not job.done:
            raise HTTPException(status_code=409, detail="подбор ещё идёт")
        engine = engine_for(user["organization_id"])
        with engine.lock:
            entries, errors = order_service.prepare_entries(engine, job.results, request.rows, request.selections)
            if not entries:
                raise HTTPException(status_code=400, detail="нет строк, готовых к заказу")
            client_fields = resolve_client(user, request)
            engine.order_store.user_id = user["id"]
            try:
                order = order_service.create_order_from_file(
                    engine, engine.order_store, entries, manager=request.manager or user["name"] or user["email"],
                    client=client_fields["name"], ship_date=request.ship_date, comment=request.comment,
                    vin=client_fields["vin"])
                order = link_client(engine.order_store, order, client_fields)
            except order_service.OrderActionError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            finally:
                engine.order_store.user_id = None
        return {"order": _order_view(order), "warnings": errors}

    # ----- заказы -----

    def _store(user):
        return engine_for(user["organization_id"]).order_store

    @app.get("/api/orders")
    def list_orders(user=Depends(current_user)):
        rows = []
        for summary in _store(user).list_order_summaries():
            items = summary.get("items") or []
            rows.append({k: summary.get(k) for k in ("order_id", "status", "created_at", "updated_at", "client",
                                                     "manager", "totals", "verification_status", "client_ship_date")}
                        | {"items_count": len(items),
                           "unknown_count": sum(1 for i in items if i.get("submit_status") == "unknown")})
        return rows

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: str, user=Depends(current_user)):
        order = _store(user).read(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="заказ не найден")
        return _order_view(order)

    @app.get("/api/orders/{order_id}/submit-preview")
    def preview_submit(order_id: str, items: str | None = None, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        indexes = [int(x) for x in items.split(",") if x.strip()] if items else None
        with engine.lock:
            try:
                return order_service.submit_preview(engine, engine.order_store, order_id, indexes)
            except order_service.OrderActionError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/orders/{order_id}/recheck")
    def recheck_order(order_id: str, request: ItemsRequest, user=Depends(current_user)):
        job = Job(request, user["organization_id"], kind="recheck")

        def work(engine):
            replay_reset(engine)
            order = order_service.recheck(engine, engine.order_store, order_id, request.items)
            job.push("done", {"order": _order_view(order)})

        start_job(job, work)
        return {"job_id": job.id}

    @app.post("/api/orders/{order_id}/submit")
    def submit_order(order_id: str, request: ItemsRequest, user=Depends(current_user)):
        if not request.confirm:
            raise HTTPException(status_code=400, detail="подтвердите отправку (confirm: true)")
        job = Job(request, user["organization_id"], kind="submit")

        def work(engine):
            messages = []
            engine.on_order_action = lambda order, message: messages.append(message)
            preview, order = order_service.submit(engine, engine.order_store, Session, order_id, request.items)
            with Session() as session:
                lines_service.sync_order(session, job.organization_id, order)
                session.commit()
            job.push("done", engine.redactor.messages({"order": _order_view(order), "message": " ".join(messages), "preview": preview}))

        start_job(job, work)
        return {"job_id": job.id}

    def _item_action(user, order_id, index, action):
        engine = engine_for(user["organization_id"])
        with engine.lock:
            if not engine.order_store.read(order_id):
                raise HTTPException(status_code=404, detail="заказ не найден")
            messages = []
            engine._log_callback = messages.append
            try:
                action(engine)
            finally:
                engine._log_callback = None
            return engine.redactor.messages({"order": _order_view(engine.order_store.read(order_id)), "message": " ".join(messages)})

    @app.post("/api/orders/{order_id}/items/{index}/skip")
    def skip_item(order_id: str, index: int, user=Depends(current_user)):
        return _item_action(user, order_id, index, lambda engine: engine._skip_order_item(order_id, index))

    @app.post("/api/orders/{order_id}/items/{index}/restore")
    def restore_item(order_id: str, index: int, user=Depends(current_user)):
        return _item_action(user, order_id, index, lambda engine: engine._restore_order_item(order_id, index))

    @app.get("/api/orders/{order_id}/log")
    def order_log(order_id: str, user=Depends(current_user)):
        with Session() as session:
            rows = session.query(db.SubmissionLog).filter_by(
                organization_id=user["organization_id"], order_id=order_id).order_by(db.SubmissionLog.id)
            return [{"created_at": r.created_at.isoformat(timespec="seconds"), "provider": r.provider,
                     "brand": r.brand, "article": r.article, "quantity": r.quantity, "success": r.success,
                     "response": (r.response or {}).get("response")} for r in rows]

    # ----- клиенты -----

    def resolve_client(user, request):
        """Клиент и машина для заказа: выбранные, найденные или созданные заново."""
        if re.search(r"[А-Яа-яЁё]", request.vin or ""):
            raise HTTPException(status_code=400, detail="VIN набран кириллицей — переключите раскладку на латиницу")
        warning = client_service.vin_warning(client_service.normalize_vin(request.vin))
        if warning:
            raise HTTPException(status_code=400, detail=warning)
        with Session() as session:
            try:
                client, vehicle = client_service.resolve(
                    session, user["organization_id"], client_id=request.client_id, vehicle_id=request.vehicle_id,
                    name=request.client, phone=request.phone, vin=request.vin)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            fields = client_service.order_client_fields(client, vehicle)
            session.commit()
        return fields

    def link_client(store, order, fields):
        order = dict(order)
        order["client"] = {**(order.get("client") or {}), **fields}
        return store.update(order)

    @app.get("/api/clients")
    def list_clients(q: str = "", user=Depends(current_user)):
        with Session() as session:
            return [client_service.client_view(c) for c in client_service.search(session, user["organization_id"], q)]

    @app.post("/api/clients")
    def create_client(request: ClientRequest, user=Depends(current_user)):
        with Session() as session:
            client = db.Client(organization_id=user["organization_id"], name=request.name.strip(),
                               phone=client_service.normalize_phone(request.phone), email=request.email.strip(),
                               comment=request.comment)
            session.add(client)
            session.commit()
            return client_service.client_view(client)

    def _client(session, user, client_id):
        client = session.get(db.Client, client_id)
        if client is None or client.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="клиент не найден")
        return client

    @app.get("/api/clients/{client_id}")
    def get_client(client_id: int, user=Depends(current_user)):
        with Session() as session:
            view = client_service.client_view(_client(session, user, client_id))
        history = []
        for order in engine_for(user["organization_id"]).order_store.list_orders():
            client = order.get("client") or {}
            if client.get("id") != client_id:
                continue
            history.append({
                "order_id": order.get("order_id"), "created_at": order.get("created_at"), "status": order.get("status"),
                "vehicle_id": client.get("vehicle_id"), "vin": client.get("vin"), "totals": order.get("totals"),
                "items": [{k: i.get(k) for k in ("brand", "article", "name", "quantity", "sale_price", "submit_status")}
                          for i in order.get("items") or []],
            })
        return {**view, "orders": history}

    @app.put("/api/clients/{client_id}")
    def update_client(client_id: int, request: ClientRequest, user=Depends(current_user)):
        with Session() as session:
            client = _client(session, user, client_id)
            client.name, client.email, client.comment = request.name.strip(), request.email.strip(), request.comment
            client.phone = client_service.normalize_phone(request.phone)
            session.commit()
            return client_service.client_view(client)

    @app.post("/api/clients/{client_id}/vehicles")
    def add_vehicle(client_id: int, request: VehicleRequest, user=Depends(current_user)):
        vin = client_service.normalize_vin(request.vin)
        if re.search(r"[А-Яа-яЁё]", request.vin) or client_service.vin_warning(vin):
            raise HTTPException(status_code=400, detail="VIN: латиница и цифры, без букв I, O, Q")
        with Session() as session:
            client = _client(session, user, client_id)
            if vin and any(v.vin == vin for v in client.vehicles):
                raise HTTPException(status_code=409, detail="эта машина уже есть у клиента")
            session.add(db.Vehicle(organization_id=user["organization_id"], client=client, vin=vin,
                                   plate=request.plate.strip().upper(), make=request.make.strip(),
                                   model=request.model.strip(), year=request.year, comment=request.comment))
            session.commit()
            return client_service.client_view(client)

    def _vehicle(session, user, vehicle_id):
        vehicle = session.get(db.Vehicle, vehicle_id)
        if vehicle is None or vehicle.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="машина не найдена")
        return vehicle

    @app.put("/api/vehicles/{vehicle_id}")
    def update_vehicle(vehicle_id: int, request: VehicleRequest, user=Depends(current_user)):
        with Session() as session:
            vehicle = _vehicle(session, user, vehicle_id)
            vin = client_service.normalize_vin(request.vin)
            if re.search(r"[А-Яа-яЁё]", request.vin) or client_service.vin_warning(vin):
                raise HTTPException(status_code=400, detail="VIN: латиница и цифры, без букв I, O, Q")
            vehicle.vin, vehicle.plate = vin, request.plate.strip().upper()
            vehicle.make, vehicle.model, vehicle.year, vehicle.comment = (
                request.make.strip(), request.model.strip(), request.year, request.comment)
            session.commit()
            return client_service.client_view(vehicle.client)

    @app.delete("/api/vehicles/{vehicle_id}")
    def delete_vehicle(vehicle_id: int, user=Depends(current_user)):
        with Session() as session:
            vehicle = _vehicle(session, user, vehicle_id)
            client = vehicle.client
            session.delete(vehicle)
            session.commit()
            session.refresh(client)
            return client_service.client_view(client)

    # ----- корзина -----

    from cart_store import DraftCart

    def load_cart(session, user_id):
        row = session.get(db.Cart, user_id)
        cart = DraftCart()
        cart._entries = [dict(e) for e in (row.entries if row else [])]
        return cart

    def save_cart(session, user_id, cart):
        row = session.get(db.Cart, user_id)
        if row is None:
            row = db.Cart(user_id=user_id, entries=[])
            session.add(row)
        from order_store import _json_safe
        row.entries = _json_safe(cart.rows())
        session.commit()

    def cart_view(engine, cart):
        rows, purchase, sale, quantity = [], 0.0, 0.0, 0
        for entry in cart.rows():
            item = entry.get("item") or {}
            qty = int(entry.get("qty") or 0)
            price = float(item.get("purchase_price", item.get("price")) or 0)
            sale_price = float(engine.apply_markup(price)[0])
            purchase += price * qty
            sale += sale_price * qty
            quantity += qty
            rows.append({"key": entry.get("key"), "qty": qty, "status": engine._cart_status_text(item),
                         "variants": int(item.get("selection_group_offer_count") or 1),
                         "item": {**_offer(item), "sale_price": sale_price}})
        return {"entries": rows, "positions": len(rows), "quantity": quantity,
                "purchase_total": round(purchase, 2), "sale_total": round(sale, 2)}

    @app.get("/api/cart")
    def get_cart(user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        with Session() as session:
            return cart_view(engine, load_cart(session, user["id"]))

    @app.post("/api/cart")
    def add_to_cart(request: CartAddRequest, user=Depends(current_user)):
        job = state["jobs"].get(request.job_id)
        if job is None or job.organization_id != user["organization_id"] or job.kind != "search" or not job.results:
            raise HTTPException(status_code=404, detail="результат поиска устарел — повторите поиск")
        result = job.results[0]
        alternatives = list(result.get("alternatives") or [])
        offer = next((o for o in alternatives if str(o.get("internal_offer_id")) == request.internal_offer_id), None)
        if offer is None:
            raise HTTPException(status_code=404, detail="предложение не найдено в результате поиска")
        engine = engine_for(user["organization_id"])
        messages = []
        with engine.lock, Session() as session:
            cart = load_cart(session, user["id"])
            engine.draft_cart, engine.displayed_data, engine._log_callback = cart, alternatives, messages.append
            try:
                engine._add_item_to_draft_cart(dict(offer), request.quantity)
            finally:
                engine.draft_cart, engine.displayed_data, engine._log_callback = None, [], None
            save_cart(session, user["id"], cart)
            view = cart_view(engine, cart)
        message = engine.redactor.text(" ".join(messages))
        if message.startswith("ОШИБКА"):
            raise HTTPException(status_code=400, detail=message.replace("ОШИБКА: ", ""))
        return {**view, "message": message}

    @app.put("/api/cart/{key}")
    def set_cart_quantity(key: str, request: CartQuantityRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        with engine.lock, Session() as session:
            cart = load_cart(session, user["id"])
            entry = next((e for e in cart.rows() if str(e.get("key")) == key), None)
            if entry is None:
                raise HTTPException(status_code=404, detail="позиции нет в корзине")
            info = engine._quantity_info(entry["item"], request.quantity)
            if not info.can_order:
                raise HTTPException(status_code=400, detail=info.reason or "нельзя заказать такое количество")
            cart.set_quantity(key, entry["item"], info.actual_int(), engine._quantity_limit_for_cart(info))
            save_cart(session, user["id"], cart)
            return cart_view(engine, cart)

    @app.delete("/api/cart/{key}")
    def remove_from_cart(key: str, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        with Session() as session:
            cart = load_cart(session, user["id"])
            cart.remove_keys([key])
            save_cart(session, user["id"], cart)
            return cart_view(engine, cart)

    @app.delete("/api/cart")
    def clear_cart(user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        with Session() as session:
            cart = load_cart(session, user["id"])
            cart.clear()
            save_cart(session, user["id"], cart)
            return cart_view(engine, cart)

    @app.post("/api/cart/checkout")
    def checkout(request: CheckoutRequest, user=Depends(current_user)):
        """Как «Оформить заказ» в корзине десктопа (_save_draft_order) + справочник клиентов."""
        engine = engine_for(user["organization_id"])
        with Session() as session:
            if not load_cart(session, user["id"]).rows():
                raise HTTPException(status_code=400, detail="корзина пуста")
        client_fields = resolve_client(user, request)
        with engine.lock, Session() as session:
            cart = load_cart(session, user["id"])
            if not cart.rows():
                raise HTTPException(status_code=400, detail="корзина пуста")
            entries = engine._order_entries_with_prices(cart.rows())
            engine.order_store.user_id = user["id"]
            try:
                order = engine.order_store.create_draft(
                    manager=request.manager or user["name"] or user["email"], client=client_fields["name"],
                    ship_date=request.ship_date or engine.order_store.calculated_ready_date(entries),
                    entries=entries, comment=request.comment, client_vin=client_fields["vin"], grouping_mode="manual",
                    verified_at=engine._entries_verified_at(entries))
                order = link_client(engine.order_store, order, client_fields)
            finally:
                engine.order_store.user_id = None
            cart.clear()
            save_cart(session, user["id"], cart)
        return {"order": _order_view(order)}

    # ----- замена варианта позиции (из групп заказа, как кнопка «Выбрать вариант» в десктопе) -----

    def _variant_group(order, index):
        items = order.get("items") or []
        if not (0 <= index < len(items)):
            raise HTTPException(status_code=404, detail="позиция не найдена")
        current = str(items[index].get("internal_offer_id") or "")
        for group in order.get("groups") or []:
            if any(str(o.get("internal_offer_id") or "") == current for o in group.get("offers") or []):
                return group, current
        return None, current

    @app.get("/api/orders/{order_id}/items/{index}/variants")
    def item_variants(order_id: str, index: int, user=Depends(current_user)):
        order = _store(user).read(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="заказ не найден")
        group, current = _variant_group(order, index)
        if group is None:
            return []
        return [{**_offer({**(o.get("snapshot") or {}), **o}), "price": o.get("purchase_price"),
                 "sale_price": o.get("sale_price")}
                for o in group.get("offers") or [] if str(o.get("internal_offer_id") or "") != current]

    @app.post("/api/orders/{order_id}/items/{index}/replace")
    def replace_item(order_id: str, index: int, request: ReplaceRequest, user=Depends(current_user)):
        engine = engine_for(user["organization_id"])
        order = engine.order_store.read(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="заказ не найден")
        group, _current = _variant_group(order, index)
        offer = next((o for o in (group or {}).get("offers") or []
                      if str(o.get("internal_offer_id") or "") == request.internal_offer_id), None)
        if offer is None:
            raise HTTPException(status_code=404, detail="вариант не найден среди сохранённых в заказе")
        variant = {**(offer.get("snapshot") or {}), **{k: v for k, v in offer.items() if k != "snapshot"}}
        return _item_action(user, order_id, index,
                            lambda eng: eng._replace_order_item_with_variant(order_id, index, variant))

    # ----- заказы поставщикам -----

    def sync_all_lines(organization_id):
        orders = engine_for(organization_id).order_store.list_orders()
        with Session() as session:
            for order in orders:
                lines_service.sync_order(session, organization_id, order)
            session.commit()

    @app.get("/api/supplier-lines")
    def supplier_lines(provider: str = "", brand: str = "", status: str = "", date_from: str = "", date_to: str = "",
                       q: str = "", user=Depends(current_user)):
        sync_all_lines(user["organization_id"])

        def parse(value):
            try:
                return datetime.datetime.fromisoformat(value) if value else None
            except ValueError:
                raise HTTPException(status_code=400, detail="дата в формате ГГГГ-ММ-ДД")

        with Session() as session:
            rows = lines_service.query(session, user["organization_id"], provider=provider, brand=brand, status=status,
                                       date_from=parse(date_from), date_to=parse(date_to), q=q)
            everything = session.query(db.SupplierLine).filter_by(organization_id=user["organization_id"])
            facets = {
                "providers": sorted({r.provider for r in everything if r.provider}),
                "brands": sorted({r.brand for r in everything if r.brand}),
                "statuses": [{"code": k, "label": v[0]} for k, v in lines_service.STATUSES.items()],
            }
            return {"rows": [lines_service.line_view(r) for r in rows], "facets": facets}

    def _line(session, user, line_id):
        line = session.get(db.SupplierLine, line_id)
        if line is None or line.organization_id != user["organization_id"]:
            raise HTTPException(status_code=404, detail="позиция не найдена")
        return line

    @app.get("/api/supplier-lines/{line_id}")
    def supplier_line(line_id: int, user=Depends(current_user)):
        with Session() as session:
            line = _line(session, user, line_id)
            labels = {k: v[0] for k, v in lines_service.STATUSES.items()}
            return {**lines_service.line_view(line), "events": [
                {"at": e.at.isoformat(timespec="seconds"), "status": e.status, "label": labels.get(e.status, e.status),
                 "text": e.text, "source": e.source} for e in line.events]}

    @app.post("/api/supplier-lines/{line_id}/status")
    def set_line_status(line_id: int, request: LineStatusRequest, user=Depends(current_user)):
        with Session() as session:
            line = _line(session, user, line_id)
            lines_service.add_event(session, line, request.status, request.text, source="manual", user_id=user["id"])
            session.commit()
            return lines_service.line_view(line)

    def refresh_statuses(engine, organization_id):
        """Статусы открытых позиций у всех поставщиков организации; отчёт по каждому."""
        from engine import PROVIDER_DISPLAY_NAMES

        replay_reset(engine)
        report = []
        for provider in engine.providers:
            cls = type(provider).__name__
            fetch = lines_service.fetcher_for(provider)
            if not fetch:
                continue
            name = PROVIDER_DISPLAY_NAMES.get(cls, getattr(provider, "DISPLAY_NAME", cls))
            with Session() as session:
                try:
                    changed = fetch(session, organization_id, provider, name)
                    session.commit()
                    report.append({"provider": name, "changed": changed})
                except Exception as exc:
                    session.rollback()
                    report.append({"provider": name, "error": engine.redactor.text(str(exc))[:200]})
        return report

    @app.post("/api/supplier-lines/refresh")
    def refresh_lines(user=Depends(current_user)):
        job = Job(None, user["organization_id"], kind="refresh")
        sync_all_lines(user["organization_id"])

        def work(engine):
            job.push("done", {"providers": refresh_statuses(engine, job.organization_id)})

        start_job(job, work)
        return {"job_id": job.id}

    @app.get("/api/notifications")
    def notifications(after: int = -1, user=Depends(current_user)):
        """Отказы и возвраты, о которых сообщил поставщик: для всплывающих уведомлений.

        after — последний показанный id (браузер помнит его); при первом входе — отказы за 3 дня.
        """
        with Session() as session:
            events = (session.query(db.SupplierLineEvent, db.SupplierLine)
                      .join(db.SupplierLine, db.SupplierLineEvent.line_id == db.SupplierLine.id)
                      .filter(db.SupplierLine.organization_id == user["organization_id"],
                              db.SupplierLineEvent.source == "supplier",
                              db.SupplierLineEvent.status.in_(("refused", "returned"))))
            if after >= 0:
                events = events.filter(db.SupplierLineEvent.id > after)
            else:
                events = events.filter(db.SupplierLineEvent.at >= db.utcnow() - datetime.timedelta(days=3))
            events = events.order_by(db.SupplierLineEvent.id.desc()).limit(50).all()
            last = session.query(db.SupplierLineEvent.id).join(db.SupplierLine).filter(
                db.SupplierLine.organization_id == user["organization_id"]).order_by(db.SupplierLineEvent.id.desc()).first()
            labels = {k: v[0] for k, v in lines_service.STATUSES.items()}
            return {"last_id": max(after, last[0] if last else 0), "items": [{
                "id": e.id, "at": e.at.isoformat(timespec="seconds"), "status": e.status,
                "label": labels.get(e.status, e.status), "text": e.text, "line_id": line.id,
                "order_id": line.order_id, "provider": line.provider, "brand": line.brand, "article": line.article,
                "name": line.name, "quantity": line.quantity, "client": line.client_name,
            } for e, line in reversed(events)]}

    # Фоновая проверка статусов: отказ виден, даже если никто не нажимал «Обновить статусы».
    refresh_minutes = float(os.environ.get("PRICER_STATUS_REFRESH_MINUTES", "0" if replay_mode else "30") or 0)
    stop_background = threading.Event()
    app.state.stop_background = stop_background
    app.state.background_rounds = 0

    def background_refresh():
        while not stop_background.wait(refresh_minutes * 60):
            app.state.background_rounds += 1
            with Session() as session:
                org_ids = [row[0] for row in session.query(db.SupplierLine.organization_id)
                           .filter(db.SupplierLine.closed.is_(False)).distinct()]
            for org_id in org_ids:
                if stop_background.is_set():
                    return
                try:
                    sync_all_lines(org_id)
                    report = refresh_statuses(build_engine(org_id), org_id)
                    changed = sum(r.get("changed", 0) for r in report)
                    if changed:
                        print(f"[pricer] статусы организации {org_id}: изменений {changed}")
                except Exception as exc:  # фоновая проверка не должна ронять сервис
                    print(f"[pricer] фоновая проверка статусов {org_id}: {type(exc).__name__}: {str(exc)[:200]}")

    if refresh_minutes > 0:
        threading.Thread(target=background_refresh, daemon=True, name="status-refresh").start()

    @app.get("/api/supplier-stats")
    def supplier_stats(days: int = 180, user=Depends(current_user)):
        sync_all_lines(user["organization_id"])
        with Session() as session:
            return lines_service.stats(session, user["organization_id"], days=max(1, min(days, 3650)))

    # ----- подбор по VIN / госномеру (Laximo, перенесено из приложения Strije/Abcp) -----

    def laximo_client(user):
        from laximo import LaximoClient

        with Session() as session:
            account = session.query(db.SupplierAccount).filter_by(
                organization_id=user["organization_id"], section="laximo").first()
            secrets = box.open(account.secrets_sealed) if account else {}
        if not (account and (account.config or {}).get("enabled", True) and secrets.get("login") and secrets.get("password")):
            raise HTTPException(status_code=400, detail="подключите каталог Laximo на вкладке «Поставщики»")
        return LaximoClient(secrets["login"], secrets["password"])

    def laximo_call(fn):
        from laximo import LaximoError

        try:
            return fn()
        except LaximoError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/vin/find")
    def vin_find(request: VinFindRequest, user=Depends(current_user)):
        from laximo import oem_brand_for, vehicle_year

        client = laximo_client(user)
        cache = state.setdefault("vin_cache", {})
        key = (user["organization_id"], request.query.strip().upper())
        cached = cache.get(key)
        if cached and time.time() - cached[0] < 3600:  # Laximo ограничивает число запросов
            plate, vehicles = cached[1]
        else:
            plate, vehicles = laximo_call(lambda: client.find_vehicle(request.query))
            cache[key] = (time.time(), (plate, vehicles))
        return {"plate": plate, "vehicles": [{**v, "year": vehicle_year(v), "oem_brand": oem_brand_for(v["brand"])}
                                             for v in vehicles]}

    @app.post("/api/vin/groups")
    def vin_groups(vehicle: VehicleRef, user=Depends(current_user)):
        client = laximo_client(user)
        return laximo_call(lambda: client.quick_groups(vehicle.model_dump())) or {}

    @app.post("/api/vin/details")
    def vin_details(request: VinDetailsRequest, user=Depends(current_user)):
        if request.quickGroupId is None and not request.query:
            raise HTTPException(status_code=400, detail="выберите группу или введите название детали")
        client = laximo_client(user)
        return laximo_call(lambda: client.quick_details(request.vehicle.model_dump(), request.quickGroupId, request.query))

    @app.get("/api/jobs/{job_id}/events")
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
