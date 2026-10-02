"""Справочник клиентов и их машин (в десктопе клиент и VIN были просто строками в заказе).

Клиент запоминается сам: при оформлении заказа находим его по выбранному id, по телефону или
по имени, иначе создаём; VIN, которого ещё нет у клиента, добавляем как новую машину.
"""
import re

from app import db

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def normalize_vin(value):
    return "".join(ch for ch in str(value or "").strip().upper() if ch.isalnum())


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    return digits


def vin_warning(vin):
    """Подсказка, если VIN не похож на стандартный (японские номера кузова при этом допустимы)."""
    if not vin:
        return ""
    if len(vin) == 17 and not VIN_RE.match(vin):
        return "в VIN не бывает букв I, O, Q — проверьте"
    return ""


def client_view(client, with_details=False):
    data = {"id": client.id, "name": client.name, "phone": client.phone, "email": client.email,
            "comment": client.comment,
            "vehicles": [vehicle_view(v) for v in sorted(client.vehicles, key=lambda v: v.id)]}
    return data


def vehicle_view(vehicle):
    return {"id": vehicle.id, "vin": vehicle.vin, "plate": vehicle.plate, "make": vehicle.make,
            "model": vehicle.model, "year": vehicle.year, "comment": vehicle.comment}


def vehicle_title(vehicle):
    """Марка, модель, год и госномер — VIN показывается отдельно, здесь его не повторяем."""
    parts = [vehicle.make, vehicle.model, str(vehicle.year or ""), vehicle.plate]
    return " ".join(p for p in parts if p).strip()


def search(session, organization_id, query, limit=20):
    rows = session.query(db.Client).filter_by(organization_id=organization_id).order_by(db.Client.name).all()
    q = str(query or "").strip().lower()
    if not q:
        return rows[:limit]
    q_phone, q_vin = normalize_phone(q), normalize_vin(q)
    found = []
    for client in rows:
        haystack = [client.name.lower(), client.email.lower()]
        if (any(q in h for h in haystack)
                or (len(q_phone) >= 4 and q_phone in client.phone)
                or (len(q_vin) >= 4 and any(q_vin in v.vin or q_vin in normalize_vin(v.plate) for v in client.vehicles))):
            found.append(client)
        if len(found) >= limit:
            break
    return found


def resolve(session, organization_id, *, client_id=None, vehicle_id=None, name="", phone="", vin=""):
    """Клиент и машина для заказа. Возвращает (клиент, машина или None)."""
    name, phone, vin = str(name or "").strip(), normalize_phone(phone), normalize_vin(vin)
    client = None
    if client_id:
        client = session.get(db.Client, int(client_id))
        if client is None or client.organization_id != organization_id:
            raise ValueError("клиент не найден")
    if client is None and phone:
        client = session.query(db.Client).filter_by(organization_id=organization_id, phone=phone).first()
    if client is None and name:
        client = next((c for c in session.query(db.Client).filter_by(organization_id=organization_id)
                       if c.name.strip().lower() == name.lower()), None)
    if client is None:
        if not name:
            raise ValueError("укажите клиента")
        client = db.Client(organization_id=organization_id, name=name, phone=phone)
        session.add(client)
        session.flush()
    elif phone and not client.phone:
        client.phone = phone

    vehicle = None
    if vehicle_id:
        vehicle = session.get(db.Vehicle, int(vehicle_id))
        if vehicle is None or vehicle.client_id != client.id:
            raise ValueError("машина не найдена у этого клиента")
    elif vin:
        vehicle = next((v for v in client.vehicles if v.vin == vin), None)
        if vehicle is None:
            vehicle = db.Vehicle(organization_id=organization_id, client=client, vin=vin)
            session.add(vehicle)
            session.flush()
    return client, vehicle


def order_client_fields(client, vehicle):
    """Поля клиента в документе заказа (формат десктопа + ссылки на справочник)."""
    return {
        "id": client.id,
        "name": client.name,
        "phone": client.phone,
        "vin": vehicle.vin if vehicle else "",
        "vehicle_id": vehicle.id if vehicle else None,
        "vehicle": vehicle_title(vehicle) if vehicle else "",
    }
