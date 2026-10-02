"""Каталог Laximo: подбор по VIN, номеру кузова и госномеру (перенесено из приложения Strije/Abcp).

Разбор ответов повторяет LaximoRepository.kt приложения: машина (catalog, vehicleId, ssd, бренд,
название, атрибуты) -> дерево быстрых групп -> детали с оригинальными номерами (OEM). Дальше OEM
ищется по поставщикам обычным поиском, бренд оригинала — по марке машины (oem_brand_for).
"""
import re

import requests

BASE = "https://ws.laximo.ru/restApi/v1/"

_LATIN_TO_CYRILLIC = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")
_RU_PLATE = re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$")

ERRORS = {
    "E_INVALIDREQUEST": "Неверно сформирован запрос к Laximo",
    "E_INVALIDPARAMETER": "Неверное значение параметра",
    "E_CATALOGNOTEXISTS": "Каталог не зарегистрирован",
    "E_UNKNOWNCOMMAND": "Неизвестная операция",
    "E_ACCESSDENIED": "Доступ к Laximo запрещён — проверьте логин и пароль",
    "E_NOTSUPPORTED": "Операция не поддерживается каталогом",
    "E_TOO_MANY_REQUESTS": "Превышен лимит запросов Laximo, попробуйте позже",
    "E_TIMEOUT": "Laximo не ответил вовремя",
    "E_UNEXPECTED_PROBLEM": "Сбой сервиса Laximo",
}


class LaximoError(Exception):
    pass


def normalize_ru_plate(text):
    """«н207вн 154» -> «Н207ВН154», если это госномер РФ (латиница-двойник заменяется на кириллицу)."""
    value = re.sub(r"[\s-]", "", str(text or "").upper()).translate(_LATIN_TO_CYRILLIC)
    return value if _RU_PLATE.match(value) else None


def oem_brand_for(car_brand):
    """Под каким брендом поставщики продают оригинал этой марки (как oemBrandFor в приложении)."""
    key = re.sub(r"[^A-Z]", "", str(car_brand or "").upper())
    if key in ("VOLKSWAGEN", "VW", "AUDI", "SKODA", "SEAT", "CUPRA"):
        return "VAG"
    return {
        "LEXUS": "TOYOTA", "INFINITI": "NISSAN", "MINI": "BMW", "DACIA": "RENAULT",
        "CHEVROLET": "GENERAL MOTORS", "OPEL": "GENERAL MOTORS", "CADILLAC": "GENERAL MOTORS",
        "DAEWOO": "GENERAL MOTORS", "MERCEDESBENZ": "MERCEDES", "MERCEDES": "MERCEDES",
    }.get(key, str(car_brand or "").strip())


def pretty_error(text):
    code, _, info = str(text or "").partition(":")
    code = code.strip()
    base = ERRORS.get(code)
    if not base:
        return str(text or "ошибка Laximo")[:200]
    return f"{base} ({info.strip()})" if info.strip() else base


def _s(obj, *names):
    for name in names:
        value = (obj or {}).get(name)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value)
    return None


def parse_vehicles(data):
    if isinstance(data, dict):
        rows = next((data[k] for k in ("rows", "vehicles", "data") if isinstance(data.get(k), list)), [data])
    elif isinstance(data, list):
        rows = data
    else:
        return []
    vehicles = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        catalog, ssd = _s(row, "catalog", "Catalog"), _s(row, "ssd", "SSD")
        if not catalog or not ssd:
            continue
        attributes = [{"key": _s(a, "key"), "name": _s(a, "name"), "value": _s(a, "value")}
                      for a in row.get("attributes") or [] if isinstance(a, dict) and _s(a, "key")]
        vehicles.append({
            "catalog": catalog, "vehicleId": _s(row, "vehicleId", "vehicleid", "VehicleId") or "0", "ssd": ssd,
            "brand": _s(row, "brand", "Brand", "manufacturer") or "",
            "name": _s(row, "name", "Name", "model", "vehicle") or "",
            "attributes": attributes,
        })
    return vehicles


def vehicle_year(vehicle):
    for attribute in vehicle.get("attributes") or []:
        if str(attribute.get("key") or "").lower() in ("date", "year", "manufactured", "modelyear", "prodrange"):
            match = re.search(r"\b(19[5-9]\d|20\d\d)\b", str(attribute.get("value") or ""))
            if match:
                return int(match.group(1))
    return None


def parse_quick_groups(data):
    def node(obj):
        if not isinstance(obj, dict):
            return None
        name = _s(obj, "name", "quickGroupName")
        if not name:
            return None
        group_id = _s(obj, "quickGroupId")
        children = [c for c in (node(x) for x in obj.get("children") or []) if c]
        return {"name": name, "quickGroupId": int(group_id) if group_id and group_id.isdigit() else None,
                "synonyms": _s(obj, "synonyms") or "", "children": children}
    return node(data) if isinstance(data, dict) else None


def parse_quick_details(data, default_ssd=""):
    if isinstance(data, dict):
        categories = data.get("categories") or data.get("data") or []
    elif isinstance(data, list):
        categories = data
    else:
        categories = []
    result = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        units = [{"unitId": _s(u, "unitId"), "name": _s(u, "name") or "Узел", "code": _s(u, "code"),
                  "imageUrl": _s(u, "imageUrl"), "ssd": _s(u, "ssd") or default_ssd,
                  "details": [_detail(d) for d in u.get("details") or [] if isinstance(d, dict)]}
                 for u in category.get("units") or [] if isinstance(u, dict) and _s(u, "unitId")]
        details = [_detail(d) for d in category.get("details") or [] if isinstance(d, dict)]
        result.append({"name": _s(category, "name") or "Категория", "units": units, "details": details})
    return result


def _detail(obj):
    return {"name": _s(obj, "name") or "", "oem": (_s(obj, "oem") or "").strip(), "codeOnImage": _s(obj, "codeOnImage")}


class LaximoClient:
    def __init__(self, user, password, session=None, timeout=25):
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.auth = (user, password)
        self.timeout = timeout

    def call(self, method, params):
        try:
            response = self.session.post(BASE + method, params=params, data=b"", auth=self.auth, timeout=self.timeout,
                                         headers={"accept-language": "ru_RU", "Accept": "application/json"})
        except requests.exceptions.RequestException as exc:
            raise LaximoError("нет связи с каталогом Laximo") from exc
        if response.status_code >= 400:
            raise LaximoError(pretty_error(response.text))
        try:
            return response.json()
        except ValueError:
            raise LaximoError(pretty_error(response.text))

    def find_vehicle(self, query):
        """Госномер РФ ищем по номеру, всё остальное — как VIN или номер кузова."""
        query = str(query or "").strip()
        if not query:
            raise LaximoError("введите VIN, номер кузова или госномер")
        plate = normalize_ru_plate(query)
        if plate:
            data = self.call("findVehicleByPlateNumber", {"countryCode": "ru", "plateNumber": plate})
        else:
            data = self.call("findVehicle", {"identString": query.upper()})
        return plate, parse_vehicles(data)

    def quick_groups(self, vehicle):
        return parse_quick_groups(self.call("listQuickGroup", {
            "catalog": vehicle["catalog"], "ssd": vehicle["ssd"], "vehicleId": vehicle["vehicleId"]}))

    def quick_details(self, vehicle, quick_group_id=None, query=None):
        params = {"catalog": vehicle["catalog"], "ssd": vehicle["ssd"], "vehicleId": vehicle["vehicleId"], "all": "true"}
        if query:
            params["query"] = query
        else:
            params["quickGroupId"] = str(quick_group_id)
        return parse_quick_details(self.call("listQuickDetail", params), vehicle["ssd"])
