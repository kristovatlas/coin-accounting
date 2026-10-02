"""The coverage floors compare unrounded figures (ENGINEERING §3.3)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from coverage.results import should_fail_under

PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def test_a_total_just_under_a_floor_fails_it() -> None:
    # coverage.py rounds the total to `precision` digits before comparing; 94.6 % rounds to 95 at
    # the default precision of 0 and would pass the 95 % floor.
    precision = tomllib.loads(PYPROJECT.read_text())["tool"]["coverage"]["report"]["precision"]
    assert should_fail_under(94.99, 95, precision)
    assert should_fail_under(84.995, 85, precision) or precision >= 3
    assert not should_fail_under(95.0, 95, precision)
