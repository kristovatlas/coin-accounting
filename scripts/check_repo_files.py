#!/usr/bin/env python3
"""Repository file check: no symbolic links and no git submodules (ADR 0023), and nothing that
switches the workflow linters' checks off (ADR 0026).

Standard library only. A symlink in the tree can point anywhere on the machine that checks it
out: tests, tools and AI reviewers would then read or write outside the repository. A
submodule pulls in code from another repository that no review of this one covers. The
project needs neither, so both are banned; lifting the ban needs a new ADR.

zizmor and actionlint read their own config files from the repository they check, and zizmor
honours inline `zizmor: ignore[...]` comments, so a single PR could add an impostor-commit pin
and quietly switch that audit off. Linter config files and ignore comments under `.github/` are
therefore rejected; adding a reviewed config means changing this check (ADR 0026).

Usage:
    check_repo_files.py [REPO]    # exit 0 = ok, 1 = banned entries found
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 9):  # noqa: UP036 - runs on the host Python
    sys.exit("check_repo_files.py needs Python 3.9 or newer")

BANNED_MODES = {"120000": "symbolic link", "160000": "git submodule"}
# zizmor reads zizmor.yml / .github/zizmor.yml; actionlint reads .github/actionlint.yml (ADR 0026).
LINTER_CONFIG_NAMES = {"zizmor.yml", "zizmor.yaml", "actionlint.yml", "actionlint.yaml"}
ZIZMOR_IGNORE = re.compile(rb"zizmor\s*:\s*ignore", re.IGNORECASE)


def git_env() -> dict:
    """A fixed git environment: no user or system config, no injected config, and no
    inherited repository or index overrides, so the result doesn't depend on the machine."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_CONFIG") and k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return env


def check(repo: Path) -> list[str]:
    env = git_env()
    top = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], check=True,
                         capture_output=True, env=env).stdout.decode().strip()
    out = subprocess.run(["git", "-C", top, "ls-files", "--stage", "-z"], check=True,
                         capture_output=True, env=env).stdout
    errors = []
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode = meta.split(b" ", 1)[0].decode()
        name = path.decode("utf-8", "backslashreplace")
        if mode in BANNED_MODES:
            errors.append(f"{name!r}: {BANNED_MODES[mode]} (banned, ADR 0023)")
        elif name == ".gitmodules":
            errors.append("'.gitmodules': submodule configuration (banned, ADR 0023)")
        elif name.rsplit("/", 1)[-1].lower() in LINTER_CONFIG_NAMES:
            errors.append(f"{name!r}: workflow-linter config could switch checks off (ADR 0026)")
        elif name.lower().startswith(".github/"):
            try:
                data = (Path(top) / name).read_bytes()
            except OSError:
                data = b""
            if ZIZMOR_IGNORE.search(data):
                errors.append(f"{name!r}: a zizmor ignore comment could hide a finding (ADR 0026)")
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
        print("check_repo_files: ok (no symlinks, submodules or workflow-linter overrides)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
