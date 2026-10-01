#!/usr/bin/env python3
"""Fail on unexpected `.pth` files in the project's virtual environment (ENGINEERING §2.2, T-602).

Python runs `import` lines in every `.pth` file in a site-packages directory on every
interpreter start, so a wheel that ships one runs code without being imported. `make
bootstrap` runs this check right after installing; the allowlist holds only files the
pinned tools create themselves.

Usage:
    check_pth.py [VENV]    # default: .venv next to the repository root; exit 0 = ok, 1 = a start-up hook problem or no site-packages
"""

from __future__ import annotations

import base64
import csv
import hashlib
import re
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


# coverage.py's start-up hook (ADR 0025). coverage ships `a1_coverage.pth` in every wheel so that
# `[run] patch = ["subprocess"]` (ENGINEERING §3.3) can measure child processes; it does nothing
# unless COVERAGE_PROCESS_START or COVERAGE_PROCESS_CONFIG is set. It's allowed only with the exact
# content coverage 7.16.1 ships (identical in every locked wheel), as a regular file, claimed in
# RECORD by coverage's own dist-info with that same hash, and only when nothing but coverage
# provides the `coverage` module it imports. A coverage pin bump that changes the file makes this
# check fail closed until the hash is updated.
COVERAGE_HOOK = "a1_coverage.pth"
COVERAGE_HOOK_SHA256 = "ef2ed06d19867ec669c09a804060666a9cd5e383af0a9d11aa2de79b77d448e8"
COVERAGE_DIST = re.compile(r"coverage-[^-]+\.dist-info")
COVERAGE_MODULE = "coverage"


def record_hash(sha256_hex: str) -> str:
    """The `sha256=` value a wheel RECORD uses: urlsafe base64 without padding (PEP 376/427)."""
    return "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(sha256_hex)).decode().rstrip("=")


def site_dirs(venv: Path) -> list[Path]:
    # Linux and macOS layout: <venv>/lib/pythonX.Y/site-packages (lib64 can be a link to lib).
    return sorted({p.resolve() for p in venv.glob("lib*/python*/site-packages") if p.is_dir()})


HOOK_NAME = "_virtualenv"


def is_hook_name(name: str) -> bool:
    """`_virtualenv` itself or `_virtualenv.<anything>`: every name `import _virtualenv` could load.
    Case-folded: on a case-insensitive file system with PYTHONCASEOK set, `_VIRTUALENV/` would be
    found too (PR #74 review, round 3)."""
    name = name.casefold()
    return name == HOOK_NAME or name.startswith(HOOK_NAME + ".")


def is_coverage_module(name: str) -> bool:
    """`coverage` or `coverage.<anything>`: every top-level name `import coverage` could load."""
    name = name.casefold()
    return name == COVERAGE_MODULE or name.startswith(COVERAGE_MODULE + ".")


def recorded_hooks(site: Path) -> list[str]:
    """Start-up hooks an installed distribution claims in its RECORD: a top-level `.pth`, anything
    `import _virtualenv` could resolve to (a module, package or extension), or cached bytecode for it.
    The one exception is coverage's own `a1_coverage.pth` with its pinned hash (ADR 0025). Another
    distribution claiming the `coverage` module, which that hook imports, is a problem too."""
    found = []
    for record in sorted(site.glob("*.dist-info/RECORD")):
        is_coverage = COVERAGE_DIST.fullmatch(record.parent.name) is not None
        with record.open(newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                path = (row[0] if row else "").removeprefix("./")
                parts = path.split("/")
                if is_coverage and path == COVERAGE_HOOK and len(row) > 1 and row[1] == record_hash(COVERAGE_HOOK_SHA256):
                    continue
                if (len(parts) == 1 and path.casefold().endswith(".pth")) or is_hook_name(parts[0]) \
                        or (parts[0] == "__pycache__" and len(parts) > 1 and parts[1].casefold().startswith(HOOK_NAME)) \
                        or (not is_coverage and is_coverage_module(parts[0])):
                    found.append(f"{record.parent.name}: {path}")
    return found


def coverage_claims_hook(site: Path) -> bool:
    """True if an installed coverage dist-info's RECORD lists `a1_coverage.pth` with the pinned hash."""
    for record in sorted(site.glob("*.dist-info/RECORD")):
        if COVERAGE_DIST.fullmatch(record.parent.name):
            with record.open(newline="", encoding="utf-8") as f:
                if any(row[:2] == [COVERAGE_HOOK, record_hash(COVERAGE_HOOK_SHA256)] for row in csv.reader(f)):
                    return True
    return False


def problems(venv: Path) -> list[str]:
    found = []
    for site in site_dirs(venv):
        for pth in sorted(site.glob("*.pth")):
            if pth.name == COVERAGE_HOOK:
                if not stat.S_ISREG(pth.lstat().st_mode) \
                        or hashlib.sha256(pth.read_bytes()).hexdigest() != COVERAGE_HOOK_SHA256:
                    found.append(f"{pth} is not coverage's own file (hash, or not a regular file)")
                elif not coverage_claims_hook(site):
                    found.append(f"{pth} is present but no installed coverage distribution claims it")
            elif pth.name not in UV_HOOK:  # case-variant names like `_VIRTUALENV.pth` are unexpected too
                found.append(f"unexpected .pth file (runs code on every interpreter start): {pth}")
        # Everything `import _virtualenv` could load must be uv's own two regular files; a
        # package, extension module or any other variant could shadow them (PR #74 review).
        for entry in sorted(site.iterdir()):
            if is_hook_name(entry.name) and (entry.name not in (*UV_HOOK, *UV_HOOK_SHA256)
                                             or not stat.S_ISREG(entry.lstat().st_mode)):
                found.append(f"{entry} could shadow uv's _virtualenv module")
        pth, module = site / "_virtualenv.pth", site / "_virtualenv.py"
        if pth.exists() or pth.is_symlink():
            if not stat.S_ISREG(pth.lstat().st_mode) or pth.read_bytes() != UV_HOOK["_virtualenv.pth"]:
                found.append(f"{pth} is not uv's own file (content, or not a regular file)")
            if not (module.exists() or module.is_symlink()):
                found.append(f"{pth} is present without uv's {module.name}, so its import would load something else")
        if module.exists() or module.is_symlink():
            if not stat.S_ISREG(module.lstat().st_mode) \
                    or hashlib.sha256(module.read_bytes()).hexdigest() != UV_HOOK_SHA256["_virtualenv.py"]:
                found.append(f"{module} is not uv's own file (hash, or not a regular file)")
        # No cached bytecode for the hook at all: a .pyc whose header copies the source's mtime
        # and size runs whatever code it holds, and the host Python running this check can't
        # recompile it for the venv's version to compare (PR #74 review, round 3). `make
        # bootstrap` deletes these caches before the check; the venv's Python rebuilds them from
        # uv's verified source.
        cache = site / "__pycache__"
        if cache.is_dir():
            for pyc in sorted(cache.iterdir()):
                if pyc.name.casefold().startswith(HOOK_NAME):
                    found.append(f"{pyc}: cached bytecode for uv's hook isn't allowed (make bootstrap removes it)")
        for claim in recorded_hooks(site):
            found.append(f"an installed package ships a start-up hook: {claim}")
    return found


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
