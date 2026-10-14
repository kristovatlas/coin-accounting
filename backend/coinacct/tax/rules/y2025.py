"""Form 8949 boxes for digital assets from tax year 2025 (ADR 0011).

Instructions for Form 8949 (2025): digital-asset transactions go in boxes G, H and I (short-term) and
J, K and L (long-term), not in A-F. G/J: reported on Form 1099-DA with basis reported to the IRS; H/K:
reported on Form 1099-DA without basis (noncovered); I/L: not reported on a Form 1099-DA (an on-chain
disposal, say). https://www.irs.gov/instructions/i8949
"""

from __future__ import annotations

from typing import Literal


def box(long_term: bool, reported: bool, basis_reported: bool) -> Literal["G", "H", "I", "J", "K", "L"]:
    if not reported:
        return "L" if long_term else "I"
    if basis_reported:
        return "J" if long_term else "G"
    return "K" if long_term else "H"
