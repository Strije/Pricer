"""Вход: регистрация организации, вход и выход, смена пароля, /api/me, публичные сведения, каталог разделов."""

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app import db
from app import supplier_catalog as catalog
from app.routes.common import COOKIE
from app.schemas import LoginRequest, PasswordChangeRequest, RegisterRequest
from app.security import hash_password, token_hash, verify_password


def setup(app, ctx):
    Session, allow_signup, current_user = ctx.Session, ctx.allow_signup, ctx.current_user
    engine_for, replay_mode, start_session = ctx.engine_for, ctx.replay_mode, ctx.start_session

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

    from app.security import LoginLimiter

    by_email, by_address = LoginLimiter(limit=5), LoginLimiter(limit=30)

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, response: Response, request: Request):
        email = payload.email.strip().lower()
        address = request.client.host if request.client else ""
        wait = max(by_email.wait(email), by_address.wait(address))
        if wait:
            raise HTTPException(status_code=429, detail=f"слишком много попыток входа — попробуйте через {max(1, wait // 60)} мин.")
        with Session() as session:
            user = session.query(db.User).filter_by(email=email).first()
            if user is None or not verify_password(payload.password, user.password_hash):
                by_email.fail(email)
                by_address.fail(address)
                raise HTTPException(status_code=401, detail="неверный email или пароль")
            user_id = user.id
        by_email.reset(email)
        start_session(response, user_id)
        return {"ok": True}

    @app.get("/health")
    def health():
        """Для сторожа сервера: сервис жив и база отвечает."""
        from sqlalchemy import text

        try:
            with Session() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse({"ok": False, "db": type(exc).__name__}, status_code=503)
        return {"ok": True}

    @app.post("/api/auth/password")
    def change_password(payload: PasswordChangeRequest, request: Request, user=Depends(current_user)):
        """Смена своего пароля: остальные входы (другие браузеры, телефоны) закрываются."""
        if by_email.wait(user["email"]):
            raise HTTPException(status_code=429, detail="слишком много попыток — попробуйте позже")
        with Session() as session:
            row = session.get(db.User, user["id"])
            if not verify_password(payload.current, row.password_hash):
                by_email.fail(user["email"])
                raise HTTPException(status_code=400, detail="текущий пароль неверный")
            row.password_hash = hash_password(payload.new)
            keep = token_hash(request.cookies.get(COOKIE) or "")
            session.query(db.Session).filter(db.Session.user_id == user["id"], db.Session.token_hash != keep).delete()
            session.commit()
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
