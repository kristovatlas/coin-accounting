#!/usr/bin/env python3
"""Repository file-type check: no symbolic links and no git submodules (ADR 0023).

Standard library only. A symlink in the tree can point anywhere on the machine that checks it
out: tests, tools and AI reviewers would then read or write outside the repository. A
submodule pulls in code from another repository that no review of this one covers. The
project needs neither, so both are banned; lifting the ban needs a new ADR.

Usage:
    check_repo_files.py [REPO]    # exit 0 = ok, 1 = banned entries found
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 9):  # noqa: UP036 - runs on the host Python
    sys.exit("check_repo_files.py needs Python 3.9 or newer")

BANNED_MODES = {"120000": "symbolic link", "160000": "git submodule"}


def check(repo: Path) -> list[str]:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "--stage", "-z"], check=True,
                         capture_output=True, env=env).stdout
    errors = []
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode = meta.split(b" ", 1)[0].decode()
        if mode in BANNED_MODES:
            name = path.decode("utf-8", "backslashreplace")
            errors.append(f"{name!r}: {BANNED_MODES[mode]} (banned, ADR 0023)")
    if (repo / ".gitmodules").exists():
        errors.append("'.gitmodules': submodule configuration (banned, ADR 0023)")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    try:
        errors = check(repo)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"check_repo_files: could not list files: {e}", file=sys.stderr)
        return 1
    for e in errors:
        print(f"check_repo_files: {e}")
    if not errors:
        print("check_repo_files: ok (no symlinks or submodules)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
