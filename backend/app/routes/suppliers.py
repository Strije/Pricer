"""Поставщики и сервисы организации: список, проверка, добавление и правка, импорт settings.json, настройки организации."""
import json
import time

from fastapi import Depends, File, HTTPException, UploadFile

from app import db
from app import supplier_catalog as catalog
from app.redact import Redactor
from app.routes.common import _account_view, SETTINGS_UPLOAD_LIMIT
from app.schemas import AccountRequest, OrderOptionsRequest


def setup(app, ctx):
    Session, account_engine, admin_user = ctx.Session, ctx.account_engine, ctx.admin_user
    box, current_user, invalidate = ctx.box, ctx.current_user, ctx.invalidate
    replay_mode, replay_module_dummy = ctx.replay_mode, ctx.replay_module_dummy
    yookassa_client = ctx.yookassa_client

    @app.get("/api/suppliers")
    def list_suppliers(user=Depends(current_user)):
        with Session() as session:
            org = session.get(db.Organization, user["organization_id"])
            out = []
            for account in sorted(org.accounts, key=lambda a: a.id):
                view = _account_view(account, box)
                if catalog.CATALOG.get(account.section, {}).get("service"):
                    view["active"] = bool(view["secrets_set"])
                else:
                    try:
                        view["active"] = bool(account_engine(org, account).providers)
                    except Exception:
                        view["active"] = False
                out.append(view)
            return out

    @app.post("/api/services/{section}/check")
    def check_service(section: str, user=Depends(admin_user)):
        """Проверка подключения сервиса без побочных действий: ЮKassa — «информация о магазине»,
        Laximo — пробный поиск машины (неверный ключ Laximo отвечает «доступ запрещён»)."""
        with Session() as session:
            account = session.query(db.SupplierAccount).filter_by(organization_id=user["organization_id"], section=section).first()
            if account is None:
                raise HTTPException(status_code=404, detail="сервис не подключён")
            cfg = {**(account.config or {}), **box.open(account.secrets_sealed)}
            secrets_list = list(box.open(account.secrets_sealed).values())
        redactor = Redactor(secrets_list)
        started = time.monotonic()
        try:
            if section == "yookassa":
                info = yookassa_client(cfg).me()
                status = {"ok": True, "message": f"магазин {info['account_id']} · {'тестовый' if info['test'] else 'боевой'}"
                          f" · чеки {'включены' if info['fiscalization'] else 'выключены'}", **info}
                if cfg.get("receipts") and not info["fiscalization"]:
                    status = {**status, "ok": False, "message": status["message"] + " — в Pricer чеки включены, а в ЮKassa нет"}
            elif section == "laximo":
                from laximo import LaximoClient, LaximoError

                try:
                    _, vehicles = LaximoClient(cfg.get("login"), cfg.get("password")).find_vehicle("WAUBH54B11N111054")
                    status = {"ok": True, "message": f"Laximo отвечает (пробный VIN: машин {len(vehicles)})"}
                except LaximoError as exc:
                    status = {"ok": False, "message": str(exc)}
            else:
                raise HTTPException(status_code=400, detail="для этого раздела проверки нет")
        except HTTPException:
            raise
        except Exception as exc:
            status = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        status["message"] = redactor.text(status["message"])[:300]
        status["seconds"] = round(time.monotonic() - started, 1)
        status["at"] = db.utcnow().isoformat(timespec="seconds")
        with Session() as session:
            account = session.query(db.SupplierAccount).filter_by(organization_id=user["organization_id"], section=section).first()
            account.status = {k: status[k] for k in ("ok", "message", "seconds", "at")}
            session.commit()
        return status

    @app.post("/api/services/mailbox/{account_id}/check")
    def check_mailbox(account_id: int, user=Depends(admin_user)):
        """Почтовый ящик: вход по IMAP и список папок (письма не читаются и не меняются)."""
        from mail_prices import MailError, folders

        with Session() as session:
            account = session.get(db.SupplierAccount, account_id)
            if account is None or account.organization_id != user["organization_id"] or account.section != "mailbox":
                raise HTTPException(status_code=404, detail="ящик не найден")
            cfg = {**(account.config or {}), **box.open(account.secrets_sealed)}
        try:
            names = folders(cfg)
            status = {"ok": True, "message": f"вход выполнен · папок: {len(names)}", "folders": names[:50]}
        except MailError as exc:
            status = {"ok": False, "message": str(exc)}
        status["message"] = Redactor([cfg.get("password") or ""]).text(status["message"])[:300]
        status["at"] = db.utcnow().isoformat(timespec="seconds")
        with Session() as session:
            account = session.get(db.SupplierAccount, account_id)
            account.status = {k: status[k] for k in ("ok", "message", "at")}
            session.commit()
        return status

    CHECK_ARTICLE = "OC90"  # ходовой номер: его знают почти все поставщики

    @app.post("/api/suppliers/{account_id}/check")
    def check_supplier(account_id: int, user=Depends(current_user)):
        """Пробный запрос к поставщику (бренды для ходового номера): работает ли, сколько отвечает,
        что ответил при ошибке (неверный ключ, доступ только с разрешённых IP…)."""
        import concurrent.futures

        with Session() as session:
            account = session.get(db.SupplierAccount, account_id)
            if account is None or account.organization_id != user["organization_id"]:
                raise HTTPException(status_code=404, detail="поставщик не найден")
            org = session.get(db.Organization, user["organization_id"])
            engine = account_engine(org, account)
            secret_values = list(box.open(account.secrets_sealed).values())
        redactor = Redactor(secret_values + ([replay_module_dummy()] if replay_mode else []))
        if catalog.CATALOG.get(account.section, {}).get("service"):
            raise HTTPException(status_code=400, detail="это служебное подключение, а не поставщик")
        if not engine.providers:
            status = {"ok": False, "message": "выключен или не хватает данных для подключения"}
        else:
            provider = engine.providers[0]

            def probe():
                method = getattr(provider, "get_brand_candidates", None) or getattr(provider, "get_brands", None)
                found = method(CHECK_ARTICLE) if callable(method) else provider.get_prices(CHECK_ARTICLE)
                return len(found or []), str(getattr(provider, "last_message", "") or "")

            started = time.monotonic()
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                count, message = pool.submit(probe).result(timeout=30)
                ok = count > 0 or not message
                status = {"ok": ok, "count": count,
                          "message": message or (f"ответил: {count} вариантов для {CHECK_ARTICLE}" if count else "ответил, вариантов нет")}
            except concurrent.futures.TimeoutError:
                status = {"ok": False, "message": "не ответил за 30 секунд"}
            except Exception as exc:
                status = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            status["seconds"] = round(time.monotonic() - started, 1)
        status["message"] = redactor.text(status["message"])[:300]
        status["at"] = db.utcnow().isoformat(timespec="seconds")
        with Session() as session:
            account = session.get(db.SupplierAccount, account_id)
            account.status = status
            session.commit()
        return status

    @app.post("/api/suppliers/{account_id}/order-options")
    def supplier_order_options(account_id: int, payload: OrderOptionsRequest | None = None, user=Depends(admin_user)):
        """Варианты для профиля заказа (доставка, оплата, адреса, реквизиты) из справочников поставщика,
        как кнопка проверки в настройках десктопа. Только чтение, ничего не сохраняет."""
        import concurrent.futures

        from app.order_profile import LOADERS, load_options

        with Session() as session:
            account = session.get(db.SupplierAccount, account_id)
            if account is None or account.organization_id != user["organization_id"]:
                raise HTTPException(status_code=404, detail="поставщик не найден")
            section = account.section
            secrets = box.open(account.secrets_sealed)
            cfg = {**(account.config or {}), **secrets}
        if section not in LOADERS:
            raise HTTPException(status_code=400, detail="у этого поставщика нет справочников для заказа")
        secret_names = catalog.secret_fields(section)
        for key, value in ((payload.values if payload else None) or {}).items():
            if key not in secret_names and isinstance(value, (str, int, float, bool)):
                cfg[key] = value
        redactor = Redactor(list(secrets.values()) + ([replay_module_dummy()] if replay_mode else []))
        started = time.monotonic()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            result = pool.submit(load_options, section, cfg).result(timeout=45)
        except concurrent.futures.TimeoutError:
            result = {"ok": False, "message": "поставщик не ответил за 45 секунд", "options": {}, "defaults": {}}
        except Exception as exc:
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}", "options": {}, "defaults": {}}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        result["message"] = redactor.text(str(result.get("message") or ""))[:300]
        result["seconds"] = round(time.monotonic() - started, 1)
        return redactor.messages(result)

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
