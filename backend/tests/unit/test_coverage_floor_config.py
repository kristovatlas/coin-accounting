"""The coverage floors use two decimal places (ENGINEERING §3.3)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from coverage.results import should_fail_under

PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def test_a_total_just_under_a_floor_fails_it() -> None:
    # coverage.py rounds the total to `precision` digits before comparing it with --fail-under.
    # At the default precision of 0, 94.6 % rounds to 95 and passes the 95 % floor; at 2, only
    # totals from 94.995 % up do. The values below are exact enough in binary not to depend on
    # float representation.
    precision = tomllib.loads(PYPROJECT.read_text())["tool"]["coverage"]["report"]["precision"]
    assert precision >= 2
    assert should_fail_under(94.6, 95, precision)
    assert should_fail_under(94.99, 95, precision)
    assert should_fail_under(84.99, 85, precision)
    assert not should_fail_under(95.0, 95, precision)
