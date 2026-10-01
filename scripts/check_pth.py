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

import sys
from pathlib import Path

# uv writes `_virtualenv.pth` into every environment it creates (it patches distutils for
# virtualenv compatibility); it comes from the pinned uv, not from a wheel.
ALLOWED = {"_virtualenv.pth"}


def site_dirs(venv: Path) -> list[Path]:
    # Linux and macOS layout: <venv>/lib/pythonX.Y/site-packages (lib64 can be a link to lib).
    return sorted({p.resolve() for p in venv.glob("lib*/python*/site-packages") if p.is_dir()})


def unexpected(venv: Path) -> list[Path]:
    return sorted(pth for site in site_dirs(venv) for pth in site.glob("*.pth") if pth.name not in ALLOWED)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    venv = Path(args[0]) if args else Path(__file__).resolve().parent.parent / ".venv"
    if not site_dirs(venv):
        print(f"check_pth: no site-packages under {venv}", file=sys.stderr)
        return 1
    bad = unexpected(venv)
    for pth in bad:
        print(f"check_pth: unexpected .pth file (runs code on every interpreter start): {pth}")
    if not bad:
        print("check_pth: ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
