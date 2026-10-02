import pytest

from pricing import DEFAULT_MARKUP_RULES, calculate_sale_price, find_markup_rule, round_markup_price


def test_first_matching_range_wins():
    rule = find_markup_rule(150, DEFAULT_MARKUP_RULES)
    assert rule["percent"] == 60  # граница "до" исключается, "от" включается


def test_open_ended_range_when_to_is_zero():
    rules = [{"from": 0, "to": 100, "percent": 10, "fixed": 0}, {"from": 100, "to": 0, "percent": 5, "fixed": 0}]
    assert find_markup_rule(10**7, rules)["percent"] == 5


def test_no_rule_uses_default_markup():
    price, percent, fixed = calculate_sale_price(100, [], default_markup=20)
    assert (price, percent, fixed) == (120.0, 20.0, 0.0)


def test_markup_percent_and_fixed():
    rules = [{"from": 0, "to": 1000, "percent": 50, "fixed": 10}]
    assert calculate_sale_price(100, rules)[0] == 160.0


def test_rounding_goes_up_to_step():
    assert round_markup_price(101, "rubles_10") == 110
    assert round_markup_price(100, "rubles_10") == 100  # эпсилон: ровное значение не прыгает вверх
    assert round_markup_price(100.01, "rubles") == 101
    # Известная особенность float: 100.10000000000001. В веб-версии цены считаются в Decimal.
    assert round_markup_price(100.01, "kopecks_10") == pytest.approx(100.1)


def test_rounding_only_inside_price_range():
    assert round_markup_price(101, "rubles_10", rounding_from=500, rounding_to=1000) == 101


def test_unknown_rounding_mode_keeps_price():
    assert round_markup_price(101.5, "none") == 101.5


def test_default_rules_sale_price_example():
    # 1000 ₽ -> диапазон 500-1100 (49%) -> 1490 -> до 10 ₽ = 1490
    price, percent, _ = calculate_sale_price(1000, DEFAULT_MARKUP_RULES, "rubles_10")
    assert percent == 49
    assert price == 1490
