"""Banned install and fetch-and-run commands (ENGINEERING §2.3, AGENTS.md).

Shared by check_install_commands.py (CI) and agent_guard.py (Claude Code hook).
This is hygiene, not a security boundary: obfuscated commands are not detected.
"""

from __future__ import annotations

import re

# A command word starts at the beginning, after a shell separator, or after a path
# separator (so /usr/bin/npx and .venv/bin/pip are caught too).
_W = r"(?:^|[\s;&|(`$/=])"
_INTERPRETERS = r"(?:ba|z|da|k)?sh|python[0-9.]*|node|perl|ruby|php"

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("npx/pnpx/bunx", re.compile(_W + r"(npx|pnpx|bunx)\b")),
    ("pnpm dlx/create/exec", re.compile(_W + r"pnpm\s+(dlx|create|exec)\b")),
    ("pnpm install/add/update", re.compile(_W + r"pnpm\s+(i|install|add|update|up|upgrade)\b")),
    ("npm install/init/create/exec", re.compile(_W + r"npm\s+(i|install|add|ci|exec|x|init|create|update|up)\b")),
    ("yarn/bun", re.compile(_W + r"(yarn|bun)(\s|$)")),
    ("corepack", re.compile(_W + r"corepack\b")),
    ("uvx", re.compile(_W + r"uvx\b")),
    ("uv tool", re.compile(_W + r"uv\s+tool\s+(run|install)\b")),
    ("uv add/sync/lock", re.compile(_W + r"uv\s+(add|sync|lock)\b")),
    ("uv pip install/sync", re.compile(_W + r"uv\s+pip\s+(install|sync)\b")),
    ("uv run --with", re.compile(_W + r"uv\s+run\b.*\s--with\b")),
    ("pip install", re.compile(_W + r"(pip[0-9.]*|python[0-9.]*\s+-m\s+pip)\s+install\b")),
    ("ensurepip", re.compile(r"\bensurepip\b")),
    ("pipx", re.compile(_W + r"pipx\b")),
    ("fetch piped to an interpreter", re.compile(
        r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(env\s+)?(" + _INTERPRETERS + r")\b")),
    ("fetch in process substitution", re.compile(r"[<$]\(\s*(curl|wget)\b")),
    ("pre-commit", re.compile(_W + r"pre-commit\b")),
]

# `uv run` syncs the environment by default, i.e. an unwrapped install (ENGINEERING §2.2).
_UV_RUN = re.compile(_W + r"uv\s+run\b")
_UV_RUN_SAFE = re.compile(r"(^|\s)UV_NO_SYNC=1(\s|$)|\s--no-sync\b")

# Inside the repo's Makefile and scripts, a command may run through Socket Firewall.
_SFW_CMD = re.compile(r"^\s*@?\s*((then|else|do)\s+)*(\"?\$\(SFW\)\"?|\"?\$\{?SFW\}?\"?|\S*/sfw|sfw)\s+")

# Shell separators between commands.
_SPLIT = re.compile(r"&&|\|\||;|&|\||\n|`|\$\(")


def segments(command: str) -> list[str]:
    """Split a shell command line into individual command segments (approximate)."""
    return [s.strip() for s in _SPLIT.split(command) if s.strip()]


def violations(line: str, allow_sfw: bool = False) -> list[str]:
    """Return the names of rules that `line` breaks.

    With allow_sfw, a segment is exempt only if that segment itself runs through sfw;
    one sfw on a line does not exempt other commands on it.
    """
    found: list[str] = []
    if allow_sfw:
        # Normalize the Makefile/shell variable forms of the pinned binary, so that
        # splitting on `$(` below doesn't cut the token itself.
        line = re.sub(r"\"?\$[({]SFW[)}]\"?|\"?\$SFW\b\"?", "sfw", line)
    # Pipe-aware rules look at the whole line, before splitting.
    for name, rx in RULES:
        if name.startswith("fetch") and rx.search(line):
            found.append(name)
    for seg in segments(line):
        if allow_sfw and _SFW_CMD.match(seg):
            continue
        found += [name for name, rx in RULES if not name.startswith("fetch") and rx.search(seg)]
        if _UV_RUN.search(seg) and not _UV_RUN_SAFE.search(seg):
            found.append("uv run without --no-sync or UV_NO_SYNC=1")
    return sorted(set(found))
