"""Схемы запросов API (pydantic)."""
from pydantic import BaseModel, Field

from app.routes.common import ORDER_FILE_ROWS


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


class PasswordChangeRequest(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=8, max_length=200)


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


class FindRequest(BaseModel):
    """Поиск «Проценки»: только артикул; бренд — подпись варианта из шага выбора (если уточняли),
    brand_hint — бренд, известный заранее (оригинал из каталога Laximo: VAG, TOYOTA…)."""
    article: str = Field(min_length=1, max_length=80)
    brand: str = Field(default="", max_length=120)
    brand_hint: str = Field(default="", max_length=80)


class QuoteRequest(BaseModel):
    title: str = Field(default="", max_length=200)
    client: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=30)
    vin: str = Field(default="", max_length=30)
    client_id: int | None = None
    vehicle_id: int | None = None


class QuoteLineRequest(BaseModel):
    request: str | None = Field(default=None, max_length=200)
    qty: int | None = Field(default=None, ge=1, le=10000)


class QuoteVariantRequest(BaseModel):
    job_id: str
    internal_offer_id: str = Field(min_length=1, max_length=64)
    line_id: str = Field(default="", max_length=20)  # пусто — позиция по запросу поиска (создаётся сама)
    request: str = Field(default="", max_length=200)
    qty: int = Field(default=1, ge=1, le=10000)


class QuoteSendRequest(BaseModel):
    hours: int = Field(default=24, ge=1, le=336)


class QuoteChoiceRequest(BaseModel):
    choices: dict[str, str] = Field(default_factory=dict)
    comment: str = Field(default="", max_length=1000)
    contact: str = Field(default="", max_length=200)


class PriceSourceRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    kind: str | None = Field(default=None, pattern="^(url|ftp|email)$")
    enabled: bool | None = None
    location: str | None = Field(default=None, max_length=2000)  # пусто — не менять
    schedule_hours: int | None = Field(default=None, ge=1, le=720)
    settings: dict | None = None


class FavoritesRequest(BaseModel):
    brands: list[str] = Field(default_factory=list, max_length=500)
    providers: list[str] = Field(default_factory=list, max_length=500)


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


class ServiceQuoteRequest(BaseModel):
    """«ТО по машине»: машина из каталога Laximo; quote_id — дополнить существующий подбор."""
    vehicle: VehicleRef
    oem_brand: str = Field(default="", max_length=60)
    title: str = Field(default="", max_length=200)
    quote_id: int | None = None
    items: list[str] | None = Field(default=None, max_length=15)


class VinDetailsRequest(BaseModel):
    vehicle: VehicleRef
    quickGroupId: int | None = None
    query: str | None = Field(default=None, max_length=100)


class ItemsRequest(BaseModel):
    items: list[int] | None = None  # None — все позиции заказа
    confirm: bool = False
