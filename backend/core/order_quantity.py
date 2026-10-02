from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import re


@dataclass(frozen=True)
class QuantityInfo:
    requested_quantity: Decimal
    available_quantity: Decimal | None
    minimum_quantity: Decimal
    quantity_step: Decimal
    package_quantity: Decimal
    actual_order_quantity: Decimal
    can_order: bool
    availability_is_lower_bound: bool = False
    availability_text: str = ""
    reason: str = ""

    def actual_int(self):
        return int(self.actual_order_quantity)

    def available_int(self):
        if self.available_quantity is None:
            return 0
        return int(self.available_quantity)


def _decimal(value, default=None):
    if value in (None, ""):
        return default
    try:
        text = str(value).strip().replace(" ", "").replace(",", ".")
        if not text:
            return default
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return default


def _positive_decimal(value, default):
    number = _decimal(value, default)
    if number is None or number <= 0:
        return default
    return number


def parse_available_quantity(value):
    text = str(value or "").strip()
    lowered = text.lower()
    if "под заказ" in lowered:
        return None, False, True
    if not text:
        return None, False, False
    is_lower_bound = bool(re.search(r"(?:^|[^\d])(?:>|от)\s*\d", lowered))
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        return None, False, False
    quantity = _decimal(match.group(0), None)
    if quantity is None:
        return None, is_lower_bound, False
    return quantity, is_lower_bound, quantity > 0


def quantity_from_item(item, requested_quantity=1):
    item = item or {}
    requested = _positive_decimal(requested_quantity, Decimal("1"))
    available, lower_bound, available_orderable = parse_available_quantity(
        item.get("available_quantity", item.get("quantity", ""))
    )
    lower_bound = bool(item.get("availability_is_lower_bound", lower_bound))
    if item.get("can_order_quantity") is True and available is None:
        available_orderable = True

    minimum = _positive_decimal(
        item.get(
            "minimum_quantity",
            item.get("min_quantity", item.get("min_order_quantity", item.get("min_order", 1))),
        ),
        Decimal("1"),
    )
    package_quantity = _positive_decimal(item.get("package_quantity", 1), Decimal("1"))
    step = _positive_decimal(
        item.get("quantity_step", item.get("multiplicity", package_quantity)),
        Decimal("1"),
    )

    base = max(requested, minimum)
    actual = (base / step).to_integral_value(rounding=ROUND_CEILING) * step

    can_order = available_orderable
    reason = ""
    if available is not None and actual > available:
        if lower_bound:
            can_order = True
        else:
            can_order = False
            reason = f"нужно {format_quantity(actual)}, доступно {format_quantity(available)}"
    elif available is None and not available_orderable:
        can_order = False
        reason = "остаток неизвестен"

    return QuantityInfo(
        requested_quantity=requested,
        available_quantity=available,
        minimum_quantity=minimum,
        quantity_step=step,
        package_quantity=package_quantity,
        actual_order_quantity=actual,
        can_order=can_order,
        availability_is_lower_bound=lower_bound,
        availability_text=str(item.get("quantity", "") or ""),
        reason=reason,
    )


def format_quantity(value):
    if value is None:
        return ""
    number = _decimal(value, Decimal("0"))
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")
