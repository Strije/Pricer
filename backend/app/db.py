"""База данных: организации, пользователи, сессии, учётные записи поставщиков.

По умолчанию SQLite в backend/var/pricer.db; для сервера — PostgreSQL через PRICER_DATABASE_URL.
"""
import datetime
import json

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)  # наценки, округление, фильтры
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    users: Mapped[list["User"]] = relationship(back_populates="organization")
    accounts: Mapped[list["SupplierAccount"]] = relationship(back_populates="organization",
                                                             cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="admin")  # admin | manager
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    organization: Mapped[Organization] = relationship(back_populates="users")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class Session(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    user: Mapped[User] = relationship()


class SupplierAccount(Base):
    __tablename__ = "supplier_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    section: Mapped[str] = mapped_column(String(50))  # раздел settings.json: armtek, abcp_suppliers, …
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # открытые поля
    secrets_sealed: Mapped[str] = mapped_column(Text, default="")  # зашифрованные секретные поля
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    organization: Mapped[Organization] = relationship(back_populates="accounts")


def make_sessionmaker(url):
    kwargs = {"json_serializer": lambda obj: json.dumps(obj, ensure_ascii=False)}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
