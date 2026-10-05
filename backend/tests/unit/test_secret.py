"""The Secret wrapper (THREAT_MODEL T-201)."""

from __future__ import annotations

import copy
import pickle

import pytest

from coinacct.domain.secret import Secret


def test_formatting_never_shows_the_value_t201() -> None:
    s = Secret("hunter2-value")
    rendered = (str(s), repr(s), f"{s}", f"{s!r}", f"{s:>40}", "%s" % s, str([s]), str({"k": s}))  # noqa: UP031
    for text in rendered:
        assert "hunter2-value" not in text
    assert s.reveal() == "hunter2-value"


def test_a_secret_cant_be_pickled_or_copied_out_t201() -> None:
    s = Secret("hunter2-value")
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with pytest.raises(TypeError):
        copy.copy(s)


def test_an_empty_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        Secret("")


def test_secrets_compare_by_value() -> None:
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
    assert Secret("a") != "a"
    assert len({Secret("a"), Secret("a")}) == 1
