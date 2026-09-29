#!/usr/bin/env python3
"""CI hygiene check: no unwrapped install or fetch-and-run commands (ENGINEERING §2.3).

Scans the Makefile, scripts, workflows, and tool and agent config files. Inside the
Makefile and scripts/, install commands must be prefixed with `$(SFW)`/`sfw`.
Documentation is not scanned: it quotes the banned commands on purpose.
This is hygiene, not a security boundary.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from banned_commands import violations  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SELF_ALLOW = {"scripts/banned_commands.py", "scripts/check_install_commands.py", "scripts/agent_guard.py",
              # Data, not commands: URLs, hashes and descriptive prose.
              "scripts/toolchain.lock",
              # Detection patterns for these very commands (ADR 0020 tripwire).
              "scripts/tripwire.py"}
SFW_ALLOWED_DIRS = ("Makefile", "scripts/")


def targets(root: Path) -> list[Path]:
    paths = []
    for pattern in ("Makefile", "scripts/**/*", ".github/workflows/*", ".claude/*.json", "package.json",
                    "**/package.json", ".mcp.json", "pnpm-workspace.yaml", "pyproject.toml"):
        paths += [p for p in root.glob(pattern) if p.is_file()]
    skip = ("node_modules", ".toolchain", ".venv", "scripts/tests/")
    return sorted({p for p in paths if not any(s in p.as_posix() for s in skip)})


def check(root: Path) -> list[str]:
    errors = []
    for path in targets(root):
        rel = path.relative_to(root).as_posix()
        if rel in SELF_ALLOW:
            continue
        allow_sfw = rel.startswith(SFW_ALLOWED_DIRS)
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        code_lines = []
        for n, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            code_lines.append(line)
            for rule in violations(line, allow_sfw=allow_sfw):
                errors.append(f"{rel}:{n}: banned command ({rule}): {stripped}")
        # A download on one line and a run on another (e.g. a multi-line workflow step).
        if not any("fetch and run" in e for e in errors if e.startswith(rel + ":")):
            if "fetch and run" in violations("\n".join(code_lines), allow_sfw=allow_sfw):
                errors.append(f"{rel}: banned command (fetch and run across lines)")
    return errors


def main() -> int:
    errors = check(ROOT)
    for e in errors:
        print(f"check_install_commands: {e}", file=sys.stderr)
    if not errors:
        print("check_install_commands: ok", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
