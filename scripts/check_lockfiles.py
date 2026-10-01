#!/usr/bin/env python3
"""Lockfile policy check (ENGINEERING §2.5). Runs on the pinned Python (it needs `tomllib`).

The cooldown only applies when versions are resolved, so a hand-edited or bot-written
lockfile could still bring in a fresh or off-registry package. This check reads every entry:

- `uv.lock`: each package except our own virtual project comes from the PyPI registry, and
  every file (wheel or sdist) is an https URL on files.pythonhosted.org, carries a sha256
  hash, and was uploaded at least 7 days ago.
- `pnpm-lock.yaml`: not supported yet, so its presence fails the check. pnpm is re-pinned in
  M0.2 (#62), and the pnpm part of this check lands with the first JavaScript dependency.
- Our `package.json` files have no lifecycle scripts; there is no `.pnpmfile.*` and no
  `configDependencies`.

There are no cooldown exceptions yet (§2.6): supporting one means extending this script in
the same PR that records the exception.

Usage:
    check_lockfiles.py [REPO] [--now ISO-8601]    # exit 0 = ok, 1 = policy violation
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 - tomllib arrived in 3.11
    sys.exit("check_lockfiles.py needs Python 3.11 or newer: run it with .toolchain/bin/python3 (make check-lockfiles)")

import tomllib  # noqa: E402

COOLDOWN = timedelta(days=7)
PYPI_INDEX = "https://pypi.org/simple"
FILES_HOST = "https://files.pythonhosted.org/"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
LIFECYCLE = {"preinstall", "install", "postinstall", "prepublish", "preprepare", "prepare", "postprepare",
             "prepack", "postpack", "dependencies"}
# pnpm's own hooks (e.g. `pnpm:devPreinstall`, run before a local install) are rejected by prefix,
# so a hook added in a later pnpm release fails closed too (PR #63 review).
PNPM_HOOK_PREFIX = "pnpm:"


def check_uv_lock(repo: Path, now: datetime) -> list[str]:
    path = repo / "uv.lock"
    if not path.exists():
        return []
    errors: list[str] = []
    project = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["name"]
    lock = tomllib.loads(path.read_text())
    for pkg in lock.get("package", []):
        name, version = pkg.get("name", "?"), pkg.get("version", "?")
        where = f"uv.lock: {name} {version}"
        source = pkg.get("source", {})
        if source == {"virtual": "."} and name == project:
            continue  # our own project, never built (package = false)
        if source != {"registry": PYPI_INDEX}:
            errors.append(f"{where}: source {source!r} is not the PyPI registry")
            continue
        files = list(pkg.get("wheels", []))
        if "sdist" in pkg:
            files.append(pkg["sdist"])
        if not pkg.get("wheels"):
            errors.append(f"{where}: no wheels (sdist builds are not allowed, ENGINEERING §2.2)")
        for f in files:
            url = f.get("url", "")
            if not url.startswith(FILES_HOST):
                errors.append(f"{where}: file URL is not on files.pythonhosted.org: {url!r}")
            if not SHA256.match(f.get("hash", "")):
                errors.append(f"{where}: missing or malformed sha256 hash for {url.rsplit('/', 1)[-1]!r}")
            uploaded = f.get("upload-time")
            if not uploaded:
                errors.append(f"{where}: no upload-time for {url.rsplit('/', 1)[-1]!r}, so its age can't be checked")
                continue
            when = datetime.fromisoformat(str(uploaded).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if now - when < COOLDOWN:
                errors.append(f"{where}: {url.rsplit('/', 1)[-1]!r} was uploaded {when:%Y-%m-%d}, "
                              "less than 7 days ago (cooldown, ENGINEERING §2.1-2.2)")
    return errors


def check_js_side(repo: Path) -> list[str]:
    errors: list[str] = []
    if (repo / "pnpm-lock.yaml").exists():
        errors.append("pnpm-lock.yaml: the pnpm lockfile check is not implemented yet (lands with the first "
                      "JavaScript dependency, after pnpm is re-pinned in #62)")
    for pkg_json in [repo / "package.json", *sorted(repo.glob("*/package.json"))]:
        if not pkg_json.exists():
            continue
        text = pkg_json.read_text()
        scripts = json.loads(text).get("scripts", {}) or {}
        bad = sorted(name for name in scripts if name in LIFECYCLE or name.startswith(PNPM_HOOK_PREFIX))
        if bad:
            errors.append(f"{pkg_json.relative_to(repo)}: lifecycle scripts are not allowed: {', '.join(bad)}")
        if "configDependencies" in text:
            errors.append(f"{pkg_json.relative_to(repo)}: configDependencies are not allowed (T-602)")
    for pnpmfile in sorted(repo.glob(".pnpmfile.*")) + sorted(repo.glob("*/.pnpmfile.*")):
        errors.append(f"{pnpmfile.relative_to(repo)}: a .pnpmfile is not allowed (T-602)")
    workspace = repo / "pnpm-workspace.yaml"
    # Any occurrence fails, comments included: a key can be quoted or written in flow style, and
    # without a YAML parser failing closed is the safe reading (PR #63 review).
    if workspace.exists() and "configDependencies" in workspace.read_text():
        errors.append("pnpm-workspace.yaml: configDependencies are not allowed (T-602); remove every mention")
    return errors


def check(repo: Path, now: datetime) -> list[str]:
    return check_uv_lock(repo, now) + check_js_side(repo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", nargs="?", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--now", help="ISO-8601 time to measure ages against (tests)")
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    try:
        errors = check(Path(args.repo), now)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as e:
        print(f"check_lockfiles: could not read the lockfiles: {e}", file=sys.stderr)
        return 1
    for e in errors:
        print(f"check_lockfiles: {e}")
    if not errors:
        print("check_lockfiles: ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
