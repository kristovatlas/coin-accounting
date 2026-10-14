"""Tax rules by tax year (ADR 0011: rule sets are versioned by tax year, citing the IRS instructions).

`box` chooses a Form 8949 box for a disposal of digital assets. Each year's rules are in their own
module; a year without one uses the latest earlier rule set, and `RULESET` names the version a report
prints (PLAN §8).
"""

from __future__ import annotations

from typing import Final, Literal

from coinacct.tax.engine import EngineError
from coinacct.tax.rules import y2024, y2025

type Box = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]

RULESET: Final = "2026-10-14"  # bumped whenever a year's rules change
BOXES: Final[frozenset[str]] = frozenset("ABCDEFGHIJKL")


def box(tax_year: int, long_term: bool, reported: bool, basis_reported: bool) -> Box:
    """The box for a digital-asset disposal in `tax_year`: `reported` if a Form 1099-B (2024 and
    earlier) or 1099-DA (2025 and later) reports it, `basis_reported` if that form reports its basis."""
    if type(tax_year) is not int or tax_year < 2009:
        raise EngineError("a tax year is a whole number from 2009 on")
    if basis_reported and not reported:
        raise EngineError("basis is reported only on a form that reports the sale")
    if tax_year <= y2024.LAST_YEAR:
        return y2024.box(long_term, reported, basis_reported)
    return y2025.box(long_term, reported, basis_reported)
