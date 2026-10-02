"""Веб-сервис Pricer: create_app собирает общее окружение (база, ключи, движки организаций, вход)
и подключает разделы API из app/routes — у каждого setup(app, ctx).

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
    PRICER_PUBLIC_URL     — внешний адрес (https://…) для ссылок подборов и возврата из ЮKassa.
    PRICER_DOCS           — 1, чтобы открыть документацию API на /docs (по умолчанию закрыта).
    PRICER_STATUS_REFRESH_MINUTES — как часто сервер сам спрашивает статусы заказов у поставщиков
                            (по умолчанию 30 минут, 0 — только по кнопке; в демо-режиме выключено).
"""
import datetime
import json
import os
import shutil
import sys
import threading
import time
from types import SimpleNamespace

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(BACKEND, "core"), os.path.join(BACKEND, "integrations")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

from app import db  # noqa: E402
from app import orders as order_service  # noqa: E402
from app import prices as price_service  # noqa: E402
from app import supplier_catalog as catalog  # noqa: E402
from app import supplier_lines as lines_service  # noqa: E402
from app.redact import Redactor  # noqa: E402
from app.routes.common import COOKIE, CSRF_HEADER, _Patch, SESSION_DAYS, STATIC  # noqa: E402
from app.security import load_master_key, new_token, SecretBox, token_hash  # noqa: E402
from app.routes import auth, cart, clients, lines, orders, prices, quotes, search, suppliers, vin  # noqa: E402


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

        if replay_value in ("1", "true"):
            # строки файла заказа (search-…) и поиск «Проценки» по артикулу (find-…: 162622, W71295, OC90)
            records = replay.load("find-2026-10-02.jsonl.gz") + replay.load("search-2026-10-02.jsonl.gz")
        else:
            records = replay.load(replay_value)
        state["replayer"] = replay.install(_Patch(), records, reuse=True)
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
        engine.site_links = site_links(organization_id)
        # Прайс-листы — из базы (загружает сервер по расписанию), а не файлами при поиске, как в десктопе
        from url_csv_provider import UrlCsvProvider

        engine.providers = [p for p in engine.providers if not isinstance(p, UrlCsvProvider)]
        with Session() as session:
            has_prices = session.query(db.PriceSource.id).filter_by(organization_id=organization_id, enabled=True).first()
        if has_prices:
            engine.providers.append(price_service.PriceDbProvider(Session, organization_id))
            PROVIDER_DISPLAY_NAMES["PriceDbProvider"] = "Прайс-листы"
        engine.provider_names = [PROVIDER_DISPLAY_NAMES.get(type(p).__name__, type(p).__name__) for p in engine.providers]
        return engine

    def site_links(organization_id):
        """Поставщик (имя в выдаче) -> ссылка поиска на его сайте с {article}: из настроек поставщика."""
        links = {}
        with Session() as session:
            org = session.get(db.Organization, organization_id)
            for account in org.accounts:
                template = str((account.config or {}).get("site_search_url") or "").strip()
                if not template or catalog.CATALOG.get(account.section, {}).get("service"):
                    continue
                try:
                    from engine import PROVIDER_DISPLAY_NAMES

                    for provider in account_engine(org, account).providers:
                        cls = type(provider).__name__
                        links[PROVIDER_DISPLAY_NAMES.get(cls, getattr(provider, "DISPLAY_NAME", cls))] = template
                except Exception:
                    continue
        return links

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

    def staff_user(user=Depends(current_user)):
        if user["role"] == "customer":
            raise HTTPException(status_code=403, detail="недоступно покупателю")
        return user

    # Документация API (/docs) — только по явному разрешению: на публичном сервере она не нужна.
    docs = os.environ.get("PRICER_DOCS", "0") == "1"
    app = FastAPI(title="Pricer", version="0.9.0", docs_url="/docs" if docs else None,
                  redoc_url=None, openapi_url="/openapi.json" if docs else None)
    from starlette.middleware.gzip import GZipMiddleware

    app.add_middleware(GZipMiddleware, minimum_size=2000)  # поток событий (SSE) не сжимается
    app.state.pricer = {"state": state, "engine_for": engine_for, "Session": Session, "box": box}  # для тестов

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # Изменяющие запросы принимаем только со своим заголовком: чужой сайт не может его
        # добавить без разрешения CORS, поэтому подделать запрос из браузера пользователя нельзя.
        # Уведомления ЮKassa приходят без заголовка — они лишь просят перепроверить платёж у ЮKassa.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/") \
                and request.url.path != "/api/yookassa/webhook":
            if request.headers.get(CSRF_HEADER, "").lower() != "pricer":
                return JSONResponse({"detail": "запрос отклонён (нет заголовка X-Requested-With)"}, status_code=403)
        return await call_next(request)

    # ----- страницы -----

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def account_engine(org, account):
        """Движок только с этим подключением: построился ли из него поставщик (включён и хватает данных)."""
        from engine import ProcurementEngine

        accounts = [(account.section, account.config, box.open(account.secrets_sealed))]
        settings = catalog.compose_settings(org.settings or {}, accounts)
        for section, spec in catalog.CATALOG.items():  # остальные выключены — иначе встанут умолчания движка
            if section != account.section and not spec.get("multiple"):
                settings.setdefault(section, {})["enabled"] = False
        return ProcurementEngine(settings, brand_aliases=brand_resolver())

    # фоновые потоки разделов (статусы, оплаты, прайсы) останавливаются этим сигналом
    stop_background = threading.Event()
    app.state.stop_background = stop_background
    app.state.background_rounds = 0

    # ----- разделы API (app/routes): каждому — общее окружение ctx -----

    ctx = SimpleNamespace(**{name: value for name, value in locals().items() if not name.startswith("_")})
    for module in (search, clients, cart, orders, vin, lines, quotes, suppliers, prices, auth):
        module.setup(app, ctx)

    return app


def __getattr__(name):
    # uvicorn app.server:app — приложение создаётся при первом обращении, а не при импорте модуля
    # (тесты создают своё через create_app с временной папкой).
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
