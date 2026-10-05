"""A wrapper that keeps a secret out of reprs, logs and tracebacks (THREAT_MODEL T-201)."""

from __future__ import annotations

from typing import Final, final

REDACTED: Final = "<redacted>"


@final
class Secret:
    """Holds a credential. `str()`, `repr()` and formatting never show the value; only `reveal()`
    does, at the one place that needs it (building the RPC auth header)."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("a secret can't be empty")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash((Secret, self._value))

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("a Secret can't be pickled or copied out of the process")
