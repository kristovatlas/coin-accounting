"""USD amounts and the engine's one division: exact cents, splits that add up (PLAN §7; T-502)."""

from __future__ import annotations

from decimal import Context, Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st

from coinacct.domain.money import add, share, subtract, usd


@pytest.mark.parametrize(
    ("text", "value"),
    [("0", "0.00"), ("12.5", "12.50"), (" 1000.00 ", "1000.00"), ("1.230", "1.23"), ("1E+2", "100.00")],
)
def test_usd_reads_exact_cents(text: str, value: str) -> None:
    assert usd(text) == Decimal(value)
    assert usd(text).as_tuple().exponent == -2


# 61 significant digits: a 60-digit context would round it to 1.00 while parsing and accept it.
LONG = "1." + "0" * 59 + "1"


@pytest.mark.parametrize(
    "text",
    ["0.001", "1.234", "-1", "NaN", "Infinity", "abc", "", "1e100", "1000000000000000", LONG, "1" + "0" * 70],
)
def test_usd_refuses_what_isnt_exact_cents_below_a_quadrillion(text: str) -> None:
    with pytest.raises(ValueError):  # never decimal.InvalidOperation or Overflow
        usd(text)


def test_usd_refuses_a_huge_decimal_and_keeps_the_bound() -> None:
    with pytest.raises(ValueError, match="quadrillion"):
        usd(Decimal("1E+60"))
    assert usd("999999999999999.99") == Decimal("999999999999999.99")


def test_minus_zero_is_zero() -> None:
    assert str(usd("-0.00")) == "0.00"


def test_usd_refuses_a_float_t502() -> None:
    with pytest.raises(ValueError):
        usd(1.5)  # type: ignore[arg-type]


def test_a_share_rounds_half_to_even_once() -> None:
    assert share(Decimal("0.10"), 1, 3) == Decimal("0.03")  # 0.0333…
    assert share(Decimal("0.07"), 1, 2) == Decimal("0.04")  # 0.035: half to even
    assert share(Decimal("0.05"), 1, 2) == Decimal("0.02")  # 0.025: half to even
    assert share(Decimal("60000.00"), 100_000_000, 120_000_000) == Decimal("50000.00")


def test_money_arithmetic_ignores_the_callers_context_t502() -> None:
    with localcontext(Context(prec=3)):
        assert share(Decimal("1000.00"), 1, 3) == Decimal("333.33")
        assert subtract(Decimal("1000.00"), Decimal("333.33")) == Decimal("666.67")
        assert add(Decimal("999999.99"), Decimal("0.01")) == Decimal("1000000.00")
        assert subtract(Decimal("1.00"), Decimal("2.50")) == Decimal("-1.50")


def test_the_whole_share_is_the_amount_and_none_is_zero() -> None:
    assert share(Decimal("0.01"), 7, 7) == Decimal("0.01")
    assert share(Decimal("123.45"), 0, 9) == Decimal("0.00")


@pytest.mark.parametrize(("part", "whole"), [(1, 0), (-1, 3), (4, 3)])
def test_a_share_needs_a_part_of_a_positive_whole(part: int, whole: int) -> None:
    with pytest.raises(ValueError):
        share(Decimal("1.00"), part, whole)


@pytest.mark.parametrize(("part", "whole"), [(True, 3), (1, 3.0), (Decimal(1), 3)])
def test_a_share_takes_integer_sats_only(part: object, whole: object) -> None:
    with pytest.raises(TypeError):
        share(Decimal("1.00"), part, whole)  # type: ignore[arg-type]


@given(
    cents=st.integers(min_value=0, max_value=10**15),
    parts=st.lists(st.integers(min_value=1, max_value=10**12), min_size=1, max_size=12),
)
def test_shares_of_what_remains_add_up_to_the_whole(cents: int, parts: list[int]) -> None:
    amount = Decimal(cents).scaleb(-2)
    left_sats, left = sum(parts), amount
    total = Decimal("0.00")
    for p in parts:
        piece = share(left, p, left_sats)
        assert Decimal("0.00") <= piece <= left
        total += piece
        left -= piece
        left_sats -= p
    assert total == amount
    assert left == 0
