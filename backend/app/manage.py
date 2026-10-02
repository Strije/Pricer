"""Управление сервером из консоли (на сервере — от имени сервиса, с его настройками из /etc/pricer.env):

    python -m app.manage create-org "Автодруг" admin@example.com   # организация и её администратор
    python -m app.manage reset-password admin@example.com          # новый пароль (печатается один раз)
    python -m app.manage list                                      # организации и пользователи

Пароль генерируется случайный и показывается один раз — регистрацию на публичном сервере можно
выключить (PRICER_ALLOW_SIGNUP=0), а организации заводить отсюда.
"""
import os
import secrets
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (HERE, os.path.join(HERE, "core"), os.path.join(HERE, "integrations")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app import db  # noqa: E402
from app.security import hash_password  # noqa: E402


def sessionmaker():
    var_dir = os.environ.get("PRICER_VAR_DIR") or os.path.join(HERE, "var")
    url = os.environ.get("PRICER_DATABASE_URL") or f"sqlite:///{os.path.join(var_dir, 'pricer.db')}"
    return db.make_sessionmaker(url)


def new_password():
    return secrets.token_urlsafe(12)


def create_org(name, email):
    email = email.strip().lower()
    with sessionmaker()() as session:
        if session.query(db.User).filter_by(email=email).first():
            raise SystemExit(f"пользователь {email} уже есть — используйте reset-password")
        org = db.Organization(name=name.strip(), settings={})
        session.add(org)
        session.flush()
        password = new_password()
        session.add(db.User(email=email, name="", password_hash=hash_password(password), role="admin",
                            organization_id=org.id))
        session.commit()
    print(f"Организация «{name}» создана. Вход: {email}  пароль: {password}")
    print("Пароль показан один раз — смените его после входа или сбросьте командой reset-password.")


def reset_password(email):
    email = email.strip().lower()
    with sessionmaker()() as session:
        user = session.query(db.User).filter_by(email=email).first()
        if user is None:
            raise SystemExit(f"пользователя {email} нет")
        password = new_password()
        user.password_hash = hash_password(password)
        session.query(db.Session).filter_by(user_id=user.id).delete()  # старые входы закрываем
        session.commit()
    print(f"Новый пароль для {email}: {password}")


def list_orgs():
    with sessionmaker()() as session:
        for org in session.query(db.Organization).order_by(db.Organization.id):
            users = ", ".join(f"{u.email} ({u.role})" for u in org.users)
            print(f"{org.id:>4}  {org.name}  —  {users or 'нет пользователей'}  · поставщиков: {len(org.accounts)}")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["create-org"] and len(args) == 3:
        create_org(args[1], args[2])
    elif args[:1] == ["reset-password"] and len(args) == 2:
        reset_password(args[1])
    elif args[:1] == ["list"]:
        list_orgs()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
