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
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from banned_commands import violations  # noqa: E402

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


def check_command(command: str) -> list[str]:
    problems = []
    for part in command.replace("&&", "\n").replace("||", "\n").replace(";", "\n").splitlines():
        part = part.strip()
        if not part or part.startswith("make ") or part == "make":
            continue
        problems += violations(part)
    return problems


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
