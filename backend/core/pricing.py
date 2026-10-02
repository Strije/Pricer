"""Расчёт продажной цены: правила наценки по диапазонам и округление.

Вынесено из markup_editor.py без изменения логики; не зависит от Qt.
"""
import math


DEFAULT_MARKUP_RULES = [
    {"from": 0, "to": 150, "percent": 65, "fixed": 0},
    {"from": 150, "to": 300, "percent": 60, "fixed": 0},
    {"from": 300, "to": 500, "percent": 51, "fixed": 0},
    {"from": 500, "to": 1100, "percent": 49, "fixed": 0},
    {"from": 1100, "to": 1400, "percent": 47, "fixed": 0},
    {"from": 1400, "to": 1600, "percent": 45, "fixed": 0},
    {"from": 1600, "to": 1900, "percent": 40, "fixed": 0},
    {"from": 1900, "to": 2500, "percent": 38, "fixed": 0},
    {"from": 2500, "to": 3000, "percent": 37, "fixed": 0},
    {"from": 3000, "to": 5000, "percent": 35, "fixed": 0},
    {"from": 5000, "to": 7000, "percent": 25, "fixed": 0},
    {"from": 7000, "to": 10000, "percent": 24, "fixed": 0},
    {"from": 10000, "to": 15000, "percent": 23, "fixed": 0},
    {"from": 15000, "to": 9999999, "percent": 17, "fixed": 0},
]

ROUNDING_OPTIONS = [
    ("none", "Не округлять"),
    ("kopecks_10", "До 10 коп."),
    ("rubles", "До 1 ₽"),
    ("rubles_10", "До 10 ₽"),
    ("rubles_50", "До 50 ₽"),
    ("rubles_100", "До 100 ₽"),
]


def find_markup_rule(price, rules):
    for rule in rules or []:
        try:
            start = float(rule.get("from", 0) or 0)
            end = float(rule.get("to", 0) or 0)
        except (TypeError, ValueError):
            continue
        if price >= start and (end <= 0 or price < end):
            return rule
    return None


def round_markup_price(price, rounding_mode, rounding_from=0, rounding_to=10000000):
    try:
        price = float(price)
        start = float(rounding_from or 0)
        end = float(rounding_to or 0)
    except (TypeError, ValueError):
        return price
    if not (start <= price <= end):
        return price
    steps = {
        "kopecks_10": 0.1,
        "rubles": 1,
        "rubles_10": 10,
        "rubles_50": 50,
        "rubles_100": 100,
    }
    step = steps.get(rounding_mode)
    if not step:
        return price
    return math.ceil((price - 1e-9) / step) * step


def calculate_sale_price(
    purchase_price,
    rules,
    rounding_mode="none",
    rounding_from=0,
    rounding_to=10000000,
    default_markup=0,
):
    purchase_price = float(purchase_price or 0)
    rule = find_markup_rule(purchase_price, rules)
    if rule:
        percent = float(rule.get("percent", 0) or 0)
        fixed = float(rule.get("fixed", 0) or 0)
    else:
        percent = float(default_markup or 0)
        fixed = 0.0
    marked = purchase_price * (1 + percent / 100) + fixed
    return round_markup_price(marked, rounding_mode, rounding_from, rounding_to), percent, fixed
