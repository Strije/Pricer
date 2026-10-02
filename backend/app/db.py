"""База данных: организации, пользователи, сессии, учётные записи поставщиков.

По умолчанию SQLite в backend/var/pricer.db; для сервера — PostgreSQL через PRICER_DATABASE_URL.
"""
import datetime
import json

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
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
    role: Mapped[str] = mapped_column(String(20), default="admin")  # admin | manager | customer
    # личные настройки: избранные бренды и поставщики выдачи и т.п.
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)
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
    # последняя проверка подключения: {ok, at, seconds, message}
    status: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    organization: Mapped[Organization] = relationship(back_populates="accounts")


class Order(Base):
    """Заказ: документ в формате десктопа (OrderStore) + поля для поиска и блокировки отправки."""
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("organization_id", "order_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(40))  # ORD-ГГГГММДД-NNNN
    status: Mapped[str] = mapped_column(String(30), default="draft")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Блокировка отправки в базе: повторный запуск отправки того же заказа невозможен даже
    # из другого процесса. submit_targets — какие позиции уходят (для восстановления после сбоя).
    submitting: Mapped[bool] = mapped_column(Boolean, default=False)
    submit_key: Mapped[str] = mapped_column(String(200), default="")
    submit_targets: Mapped[list] = mapped_column(JSON, default=list)
    submit_started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SubmissionLog(Base):
    """Журнал отправок поставщикам: каждая позиция и ответ поставщика (замена order_history.json)."""
    __tablename__ = "submission_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(100), default="")
    brand: Mapped[str] = mapped_column(String(100), default="")
    article: Mapped[str] = mapped_column(String(100), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class Client(Base):
    """Клиент организации (в десктопе — строка в заказе)."""
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(30), default="", index=True)
    email: Mapped[str] = mapped_column(String(254), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    vehicles: Mapped[list["Vehicle"]] = relationship(back_populates="client", cascade="all, delete-orphan")


class Vehicle(Base):
    """Машина клиента: VIN или номер кузова, госномер, марка, модель, год."""
    __tablename__ = "vehicles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    vin: Mapped[str] = mapped_column(String(30), default="", index=True)
    plate: Mapped[str] = mapped_column(String(20), default="")
    make: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(60), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    client: Mapped[Client] = relationship(back_populates="vehicles")


class SupplierLine(Base):
    """Позиция, отправленная поставщику: текущий статус («в пути», «на складе», «отказ»…)."""
    __tablename__ = "supplier_lines"
    __table_args__ = (UniqueConstraint("organization_id", "order_id", "item_index"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    item_index: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(100), default="", index=True)
    brand: Mapped[str] = mapped_column(String(100), default="", index=True)
    article: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(300), default="")
    warehouse: Mapped[str] = mapped_column(String(120), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    purchase_price: Mapped[float] = mapped_column(default=0.0)
    client_name: Mapped[str] = mapped_column(String(200), default="")
    supplier_ref: Mapped[str] = mapped_column(String(100), default="")  # номер позиции/заказа у поставщика
    # Ключи десктопа для сопоставления со статусами ABCP: positionId из ответа на заказ и supplierCode
    # предложения (вместе с брендом и номером — _position_key в abcp_supplier.py).
    position_id: Mapped[str] = mapped_column(String(100), default="")
    supplier_code: Mapped[str] = mapped_column(String(100), default="")
    promised_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)  # срок, обещанный при подборе
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="", index=True)
    status_text: Mapped[str] = mapped_column(String(500), default="")
    status_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    arrived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    events: Mapped[list["SupplierLineEvent"]] = relationship(back_populates="line", cascade="all, delete-orphan",
                                                             order_by="SupplierLineEvent.id")


class SupplierLineEvent(Base):
    """Движение позиции: смена статуса, кто/что изменил (отправка, поставщик, оператор)."""
    __tablename__ = "supplier_line_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("supplier_lines.id"), index=True)
    at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(20), default="manual")  # submit | supplier | manual
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    line: Mapped[SupplierLine] = relationship(back_populates="events")


class Quote(Base):
    """Подбор для клиента: позиции по запросу клиента, в каждой — варианты из выдачи; клиент выбирает
    по ссылке без входа (token), менеджер превращает выбор в заказ."""
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    client_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|sent|viewed|chosen|ordered|cancelled
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # {lines: [...], client_comment, contact}
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    viewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    chosen_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    order_id: Mapped[str] = mapped_column(String(40), default="")


class Notification(Base):
    """Уведомление организации (клиент выбрал варианты в подборе и т.п.) для всплывающих сообщений."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class Cart(Base):
    """Корзина пользователя (в десктопе — DraftCart в памяти окна): строки в формате DraftCart."""
    __tablename__ = "carts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    entries: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def make_sessionmaker(url):
    kwargs = {"json_serializer": lambda obj: json.dumps(obj, ensure_ascii=False)}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    return sessionmaker(engine, expire_on_commit=False)


def add_missing_columns(engine):
    """Простая миграция: новые столбцы моделей добавляются в уже существующие таблицы.

    create_all создаёт только новые таблицы; без этого база прошлой версии не открылась бы.
    Удаление и переименование столбцов так не делаем — для них понадобится Alembic.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    added = []
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                if isinstance(default, bool):
                    default_sql = " DEFAULT " + ("1" if default else "0")
                elif isinstance(default, (int, float)):
                    default_sql = f" DEFAULT {default}"
                elif isinstance(default, str):
                    default_sql = " DEFAULT '" + default.replace("'", "''") + "'"
                else:
                    default_sql = ""
                connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{default_sql}'))
                added.append(f"{table.name}.{column.name}")
    return added
