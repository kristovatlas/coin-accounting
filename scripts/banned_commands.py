"""Banned install and fetch-and-run commands (ENGINEERING §2.3, AGENTS.md).

Shared by check_install_commands.py (CI) and agent_guard.py (Claude Code hook).
This is hygiene, not a security boundary: obfuscated commands are not detected.
"""

from __future__ import annotations

import re

# Each rule: (name, regex). Matched against one shell command or one line of a file.
_WORD = r"(?:^|[\s;&|(`$])"
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("npx", re.compile(_WORD + r"npx\b")),
    ("pnpm dlx", re.compile(_WORD + r"pnpm\s+dlx\b")),
    ("pnpx", re.compile(_WORD + r"pnpx\b")),
    ("uvx", re.compile(_WORD + r"uvx\b")),
    ("uv tool run/install", re.compile(_WORD + r"uv\s+tool\s+(run|install)\b")),
    ("pip install", re.compile(_WORD + r"(pip3?|python3?\s+-m\s+pip)\s+install\b")),
    ("uv pip install", re.compile(_WORD + r"uv\s+pip\s+install\b")),
    ("npm install/add/ci/exec", re.compile(_WORD + r"npm\s+(i|install|add|ci|exec|x)\b")),
    ("yarn", re.compile(_WORD + r"yarn(\s|$)")),
    ("pnpm install/add/update", re.compile(_WORD + r"pnpm\s+(i|install|add|update|up|upgrade)\b")),
    ("uv add/sync/lock", re.compile(_WORD + r"uv\s+(add|sync|lock)\b")),
    ("curl|wget piped to a shell", re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(ba|z|da)?sh\b")),
    ("pre-commit", re.compile(_WORD + r"pre-commit\b")),
]

# Commands that go through Socket Firewall inside the repo's own scripts are allowed there
# (the Makefile and scripts/), but not typed directly by a person or an agent.
SFW_PREFIX = re.compile(r"(^|[\s;&|(])(\$\(SFW\)|\$\{?SFW\}?|sfw)\s+")


def violations(line: str, allow_sfw: bool = False) -> list[str]:
    """Return the names of rules that `line` breaks."""
    text = line
    if allow_sfw and SFW_PREFIX.search(text):
        return []
    return [name for name, rx in RULES if rx.search(text)]
