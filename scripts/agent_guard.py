#!/usr/bin/env python3
"""Claude Code hooks that enforce AGENTS.md hard rules (ENGINEERING §6).

  agent_guard.py pretooluse     PreToolUse hook: reads the tool call as JSON on stdin.
                                Exit code 2 blocks the call, and stderr is shown to the agent.
  agent_guard.py sessionstart   SessionStart hook: warns if a VeraCrypt volume is mounted.

Rules:
  - If a VeraCrypt volume is mounted, block every tool call (AGENTS.md "No real data").
  - Block banned install and fetch-and-run commands in Bash, unless run via `make`.
Standard library only. Hygiene, not a security boundary.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from banned_commands import segments, strip_prefix, tokens, violations  # noqa: E402

BLOCK = 2


def veracrypt_mounted() -> bool:
    try:
        if platform.system() == "Linux":
            if any(Path("/dev/mapper").glob("veracrypt*")):
                return True
            return "veracrypt" in Path("/proc/mounts").read_text().lower()
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
        return "veracrypt" in out.lower()
    except Exception:
        # If the check itself fails, don't guess: report it as mounted, so work stops for a human.
        return True


# Only these repository targets, with only these variables, count as "via make".
# No -f/-C/--eval, no other options, no SFW=/TOOLBIN=/PATH= overrides (PR #7 review).
MAKE_TARGETS = {"help", "toolchain", "test-tools", "propose-js", "propose-py", "bootstrap", "audit", "check"}
MAKE_VARS = {"PKG", "DEV", "BASE", "WORKSPACE"}
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9@._/+=:-]*$")
# Variables that change what make runs or which interpreter verifies the toolchain.
DANGEROUS_VARS = {"MAKEFILES", "MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "PATH", "SYS_PYTHON", "SFW", "TOOLBIN",
                  "PNPM", "UV", "SHELL", "BASH_ENV", "ENV"}


def _project_dir() -> str:
    return os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def check_make(command: str) -> list[str]:
    """`make` counts as "via make" only for an exact repository target, run from the project dir,
    with no dangerous variables anywhere in the command (PR #7 review, round 2)."""
    problems: list[str] = []
    has_make = False
    for seg in segments(command):
        assigns, toks = strip_prefix(tokens(seg))
        for a in assigns:
            if a.split("=", 1)[0] in DANGEROUS_VARS:
                problems.append(f"sets {a.split('=', 1)[0]}")
        if not toks:
            continue
        tool = os.path.basename(toks[0])
        if tool in ("make", "gmake"):
            has_make = True
            for arg in toks[1:]:
                if arg in MAKE_TARGETS:
                    continue
                key, eq, val = arg.partition("=")
                if eq and key in MAKE_VARS and _SAFE_VALUE.match(val):
                    continue
                problems.append("make with arguments outside the repository targets")
                break
    if has_make:
        for seg in segments(command):
            _, toks = strip_prefix(tokens(seg))
            if toks and toks[0] in ("cd", "pushd"):
                target = toks[1] if len(toks) > 1 else os.path.expanduser("~")
                if os.path.realpath(os.path.expanduser(target)) != _project_dir():
                    problems.append("make run from outside the project directory")
    return problems


def check_command(command: str) -> list[str]:
    return sorted(set(violations(command) + check_make(command)))


def pretooluse(payload: dict) -> int:
    if veracrypt_mounted():
        print("BLOCKED: a VeraCrypt volume appears to be mounted. Agents must not run while real data "
              "is accessible (AGENTS.md, THREAT_MODEL T-607). Ask the human to dismount it.", file=sys.stderr)
        return BLOCK
    if payload.get("tool_name") == "Bash":
        command = (payload.get("tool_input") or {}).get("command", "")
        problems = check_command(command)
        if problems:
            print("BLOCKED: banned install/fetch command (" + ", ".join(sorted(set(problems))) + "). "
                  "Use the repo's make targets (ENGINEERING §2.3, AGENTS.md).", file=sys.stderr)
            return BLOCK
    return 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "pretooluse":
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError:
            payload = {}
        return pretooluse(payload)
    if mode == "sessionstart":
        if veracrypt_mounted():
            print("WARNING: a VeraCrypt volume appears to be mounted. All tool calls will be blocked until "
                  "it is dismounted (AGENTS.md).")
        return 0
    print("usage: agent_guard.py pretooluse|sessionstart", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
