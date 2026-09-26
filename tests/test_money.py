from decimal import Decimal

import pytest

from app.money import format_euro, parse_german_amount, to_cents


def test_to_cents_basic():
    assert to_cents("1234.56") == 123456
    assert to_cents(Decimal("0.005")) == 1  # kaufmännisch aufgerundet
    assert to_cents(12) == 1200


def test_to_cents_rejects_float():
    with pytest.raises(TypeError):
        to_cents(0.1)


def test_parse_german_amount():
    assert parse_german_amount("1.234,56 €") == 123456
    assert parse_german_amount("-0,99") == -99
    assert parse_german_amount("1.234") == 123400  # Punkt = Tausendertrenner
    with pytest.raises(ValueError):
        parse_german_amount("  ")


def test_format_euro():
    assert format_euro(-123456) == "-1.234,56 €"
    assert format_euro(5) == "0,05 €"


def test_parse_flexible_amount():
    from app.money import parse_flexible_amount

    assert parse_flexible_amount("1631.60") == 163160
    assert parse_flexible_amount("12.5 €") == 1250
    assert parse_flexible_amount("1.234") == 123400
    assert parse_flexible_amount("1.631,60 €") == 163160
    assert parse_flexible_amount("1.234.567") == 123456700
    assert parse_flexible_amount("800") == 80000
