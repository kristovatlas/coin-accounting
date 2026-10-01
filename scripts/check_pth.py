#!/usr/bin/env python3
"""Fail on unexpected `.pth` files in the project's virtual environment (ENGINEERING §2.2, T-602).

Python runs `import` lines in every `.pth` file in a site-packages directory on every
interpreter start, so a wheel that ships one runs code without being imported. `make
bootstrap` runs this check right after installing; the allowlist holds only files the
pinned tools create themselves.

Usage:
    check_pth.py [VENV]    # default: .venv next to the repository root; exit 0 = ok, 1 = unexpected .pth
"""

from __future__ import annotations

import csv
import hashlib
import stat
import sys
from pathlib import Path

# uv writes `_virtualenv.pth` and the `_virtualenv.py` it imports into every environment it
# creates (virtualenv compatibility). They're allowed only as uv 0.12.17 writes them: the
# exact content and hash below, as regular files, listed in no installed package's RECORD.
# Trusting the name alone would let a wheel ship its own `_virtualenv.pth` (PR #74 review).
# A uv pin bump that changes them makes this check fail closed until these are updated.
UV_HOOK = {
    "_virtualenv.pth": b"import _virtualenv",
}
UV_HOOK_SHA256 = {
    "_virtualenv.py": "cfb3db86aaa53bb62b5ff764970bec2d71c9228590a0ebec57f6ec926cc0bf1a",
}


def site_dirs(venv: Path) -> list[Path]:
    # Linux and macOS layout: <venv>/lib/pythonX.Y/site-packages (lib64 can be a link to lib).
    return sorted({p.resolve() for p in venv.glob("lib*/python*/site-packages") if p.is_dir()})


def recorded_hooks(site: Path) -> list[str]:
    """Top-level `.pth` files and `_virtualenv.*` that an installed distribution claims (its RECORD)."""
    found = []
    for record in sorted(site.glob("*.dist-info/RECORD")):
        with record.open(newline="") as f:
            for row in csv.reader(f):
                path = row[0] if row else ""
                if "/" not in path and (path.endswith(".pth") or path.startswith("_virtualenv.")):
                    found.append(f"{record.parent.name}: {path}")
    return found


def problems(venv: Path) -> list[str]:
    found = []
    for site in site_dirs(venv):
        for pth in sorted(site.glob("*.pth")):
            if pth.name not in UV_HOOK:
                found.append(f"unexpected .pth file (runs code on every interpreter start): {pth}")
        for name, content in UV_HOOK.items():
            path = site / name
            if path.exists() or path.is_symlink():
                if not stat.S_ISREG(path.lstat().st_mode) or path.read_bytes() != content:
                    found.append(f"{path} is not uv's own file (content, or not a regular file)")
        for name, digest in UV_HOOK_SHA256.items():
            path = site / name
            if path.exists() or path.is_symlink():
                if not stat.S_ISREG(path.lstat().st_mode) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    found.append(f"{path} is not uv's own file (hash, or not a regular file)")
        for claim in recorded_hooks(site):
            found.append(f"an installed package ships a start-up hook: {claim}")
    return found


def unexpected(venv: Path) -> list[Path]:
    """The unexpected `.pth` files (kept for callers and tests that list them)."""
    return sorted(pth for site in site_dirs(venv) for pth in site.glob("*.pth") if pth.name not in UV_HOOK)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    venv = Path(args[0]) if args else Path(__file__).resolve().parent.parent / ".venv"
    if not site_dirs(venv):
        print(f"check_pth: no site-packages under {venv}", file=sys.stderr)
        return 1
    bad = problems(venv)
    for problem in bad:
        print(f"check_pth: {problem}")
    if not bad:
        print("check_pth: ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
