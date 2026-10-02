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
from app import orders as order_service  # noqa: E402
from app import supplier_catalog as catalog  # noqa: E402
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
    client: str = Field(min_length=1, max_length=200)
    manager: str = Field(default="", max_length=200)
    ship_date: str = Field(default="", max_length=20)
    comment: str = Field(default="", max_length=1000)
    vin: str = Field(default="", max_length=30)


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
        armtek.datetime.datetime = replay.FrozenDateTime
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
        state["engines"][organization_id] = engine
        return engine

    for org_id, order_id in order_service.recover_interrupted_submits(Session):
        print(f"[pricer] {order_id}: отправка была прервана перезапуском, позиции помечены как unknown")

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
        if suffix not in (".csv", ".txt", ".xlsx", ".xlsm"):
            raise HTTPException(status_code=400, detail="поддерживаются CSV, TXT и XLSX")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "order" + suffix)
            with open(path, "wb") as handle:
                handle.write(raw)
            try:
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
            engine.order_store.user_id = user["id"]
            try:
                order = order_service.create_order_from_file(
                    engine, engine.order_store, entries, manager=request.manager or user["name"] or user["email"],
                    client=request.client, ship_date=request.ship_date, comment=request.comment, vin=request.vin)
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
