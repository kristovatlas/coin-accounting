"""Form 8949 boxes for tax years 2024 and earlier (ADR 0011).

Instructions for Form 8949 (2024), "Part I" and "Part II": box A/D for short/long-term transactions
reported on Form 1099-B with basis reported to the IRS, B/E for those reported without basis, C/F for
those not reported on a Form 1099-B. Digital assets without a 1099-B go in C/F.
https://www.irs.gov/pub/irs-prior/i8949--2024.pdf
"""

from __future__ import annotations

from typing import Final, Literal

LAST_YEAR: Final = 2024


def box(long_term: bool, reported: bool, basis_reported: bool) -> Literal["A", "B", "C", "D", "E", "F"]:
    if not reported:
        return "F" if long_term else "C"
    if basis_reported:
        return "D" if long_term else "A"
    return "E" if long_term else "B"
