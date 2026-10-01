#!/usr/bin/env python3
"""Lockfile policy check (ENGINEERING §2.5). Needs Python 3.11+ (`tomllib`): `make check` uses the
pinned interpreter when it verifies, otherwise the host's (CI runners have 3.12+).

The cooldown only applies when versions are resolved, so a hand-edited or bot-written
lockfile could still bring in a fresh or off-registry package. This check reads every entry:

- `uv.lock`: each package except our own virtual project comes from the PyPI registry, and
  every file (wheel or sdist) is an https URL on files.pythonhosted.org whose file name matches
  the entry's name and version, carries a sha256 hash, and was uploaded at least 7 days ago.
  Declared Python dependencies without a `uv.lock` fail.
- pnpm lockfiles: not supported yet, so any `pnpm-lock*.yaml` (any case) in the tree fails the
  check, as do the settings that create per-package or per-branch lockfiles (`sharedWorkspaceLockfile`,
  `gitBranchLockfile`) or that ignore or relocate the lockfile (`lockfile`, `lockfileDir`).
- Our `package.json` files have no lifecycle scripts and no `packageManager` field; there is no
  `.pnpmfile.*` (any case), no `configDependencies` in any spelling, no `pnpmfile` or
  `globalPnpmfile` setting, and `ignorePnpmfile: true` is present exactly once. YAML syntax that
  could spell a key indirectly (explicit keys, tags, anchors, aliases, merge keys, document markers)
  fails closed (#71, #72, PR #81 review).

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
    sys.exit("check_lockfiles.py needs Python 3.11 or newer: run `make toolchain`, then `make check`")

import tomllib  # noqa: E402

COOLDOWN = timedelta(days=7)
PYPI_INDEX = "https://pypi.org/simple"
# The exact shape of a PyPI file URL. Matching the whole URL rules out anything a URL parser could
# read differently from this check: queries, fragments, %-escapes, backslashes (a WHATWG parser,
# as uv uses, treats `\` as `/` and then collapses `..`), dot segments and whitespace (PR #81 review).
FILES_URL = re.compile(
    r"https://files\.pythonhosted\.org/packages/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{60}/([A-Za-z0-9_][A-Za-z0-9._+-]*)")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
LIFECYCLE = {"preinstall", "install", "postinstall", "prepublish", "preprepare", "prepare", "postprepare",
             "prepack", "postpack", "dependencies"}
# pnpm's own hooks (e.g. `pnpm:devPreinstall`, run before a local install) are rejected by prefix,
# so a hook added in a later pnpm release fails closed too (PR #63 review).
PNPM_HOOK_PREFIX = "pnpm:"
# Settings that make pnpm load hook code, write lockfiles this check doesn't see, or ignore or
# relocate the lockfile (#72, PR #81 review).
FORBIDDEN_WORKSPACE_KEYS = re.compile(
    r"(?:^|[{,])\s*[\"']?(pnpmfile|globalPnpmfile|sharedWorkspaceLockfile|gitBranchLockfile|lockfile|lockfileDir)"
    r"[\"']?\s*:", re.M)
# YAML syntax that can spell a mapping key the regex above wouldn't see, or start another
# document: explicit keys (`?`), tags, anchors, aliases, merge keys, document markers, and a
# flow collection at the start of a line. pnpm-workspace.yaml needs none of them, so any of them
# fails closed (PR #81 review).
WORKSPACE_INDIRECT_SYNTAX = re.compile(r"^\s*(?:[?!&*{\[]|<<|---|\.\.\.)|:\s*[!&*]", re.M)
IGNORE_PNPMFILE = re.compile(r"^ignorePnpmfile:\s*true\s*(?:#.*)?$", re.M)
SKIP_DIRS = {".git", ".venv", ".toolchain", "node_modules", ".pnpm-store", ".uv-cache"}


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def file_name_and_version(filename: str) -> tuple[str, str] | None:
    """Name and version from a wheel (PEP 427) or sdist (PEP 625) file name."""
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        return (parts[0], parts[1]) if len(parts) >= 5 else None
    for ext in (".tar.gz", ".zip"):
        if filename.endswith(ext):
            stem = filename[: -len(ext)]
            return tuple(stem.rsplit("-", 1)) if "-" in stem else None  # type: ignore[return-value]
    return None


def declares_python_dependencies(pyproject: dict) -> bool:
    project = pyproject.get("project", {})
    groups = pyproject.get("dependency-groups", {})
    return bool(project.get("dependencies") or any(project.get("optional-dependencies", {}).values())
                or any(groups.values()))


def walk(repo: Path):
    """Every file under the repository, skipping tool and dependency directories."""
    stack = [repo]
    while stack:
        here = stack.pop()
        for entry in sorted(here.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            else:
                yield entry


def has_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return any(k == key or has_key(v, key) for k, v in value.items())
    if isinstance(value, list):
        return any(has_key(v, key) for v in value)
    return False


def check_uv_lock(repo: Path, now: datetime) -> list[str]:
    path = repo / "uv.lock"
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text())
    if not path.exists():
        if declares_python_dependencies(pyproject):
            return ["pyproject.toml declares Python dependencies but there is no uv.lock (ENGINEERING §2.2)"]
        return []
    errors: list[str] = []
    project = pyproject["project"]["name"]
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
            if not isinstance(f, dict):
                errors.append(f"{where}: malformed file entry")
                continue
            url = f.get("url", "")
            match = FILES_URL.fullmatch(url) if isinstance(url, str) else None
            if match is None:
                errors.append(f"{where}: file URL is not a plain https URL on files.pythonhosted.org: {url!r}")
            filename = match.group(1) if match else str(url).rsplit("/", 1)[-1]
            parsed = file_name_and_version(filename)
            if parsed is None or normalize(parsed[0]) != normalize(name) or parsed[1] != version:
                errors.append(f"{where}: file {filename!r} doesn't match the entry's name and version (#71)")
            if not SHA256.match(f.get("hash", "")):
                errors.append(f"{where}: missing or malformed sha256 hash for {filename!r}")
            uploaded = f.get("upload-time")
            if not uploaded:
                errors.append(f"{where}: no upload-time for {filename!r}, so its age can't be checked")
                continue
            when = datetime.fromisoformat(str(uploaded).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if now - when < COOLDOWN:
                errors.append(f"{where}: {filename!r} was uploaded {when:%Y-%m-%d}, "
                              "less than 7 days ago (cooldown, ENGINEERING §2.1-2.2)")
    return errors


def check_js_side(repo: Path) -> list[str]:
    errors: list[str] = []
    files = list(walk(repo))
    for lockfile in files:  # case-insensitive, like the .pnpmfile check (PR #81 review)
        if lockfile.name.lower().startswith("pnpm-lock") and lockfile.name.lower().endswith(".yaml"):
            errors.append(f"{lockfile.relative_to(repo)}: the pnpm lockfile check is not implemented yet (lands "
                          "with the first JavaScript dependency); if this file is untracked, delete it")
    for pkg_json in [repo / "package.json", *sorted(repo.glob("*/package.json"))]:
        if not pkg_json.exists():
            continue
        rel = pkg_json.relative_to(repo)
        text = pkg_json.read_text()
        manifest = json.loads(text)
        if not isinstance(manifest, dict):
            errors.append(f"{rel}: not a JSON object")
            continue
        scripts = manifest.get("scripts", {}) or {}
        if not isinstance(scripts, dict):
            errors.append(f"{rel}: \"scripts\" must be an object")
            scripts = {}
        bad = sorted(name for name in scripts if name in LIFECYCLE or name.startswith(PNPM_HOOK_PREFIX))
        if bad:
            errors.append(f"{rel}: lifecycle scripts are not allowed: {', '.join(bad)}")
        # The decoded keys catch escaped spellings; the raw text catches everything else (#72).
        if "configDependencies" in text or has_key(manifest, "configDependencies"):
            errors.append(f"{rel}: configDependencies are not allowed (T-602)")
        if "packageManager" in text or has_key(manifest, "packageManager"):
            # pnpm 12 resolves a `packageManager` (or `devEngines.packageManager`) pin against the
            # registry on every command, outside sfw, and records it in pnpm-lock.yaml (#51).
            # The pin lives in scripts/toolchain.lock; `engines.pnpm` checks the version.
            errors.append(f"{pkg_json.relative_to(repo)}: a packageManager field is not allowed (#51); "
                          "the pin lives in scripts/toolchain.lock")
    for pnpmfile in files:  # case-insensitive: macOS's default file system is (#72)
        if pnpmfile.name.lower().startswith(".pnpmfile."):
            errors.append(f"{pnpmfile.relative_to(repo)}: a .pnpmfile is not allowed (T-602)")
    workspace = repo / "pnpm-workspace.yaml"
    if workspace.exists():
        try:
            text = workspace.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return errors + ["pnpm-workspace.yaml: not valid UTF-8"]
        # The text checks below see only ASCII. A leading byte-order mark (which YAML loaders
        # drop) or any other non-ASCII character could hide a key from them, and is invisible
        # in a diff, so only full-line comments may contain non-ASCII text, and a byte-order
        # mark is rejected anywhere (PR #81 review, round 3).
        if "\ufeff" in text or not all(line.isascii() or line.lstrip().startswith("#")
                                        for line in text.splitlines()):
            errors.append("pnpm-workspace.yaml: non-ASCII characters (including a byte-order mark) are only "
                          "allowed in full-line comments")
        # Without a YAML parser, fail closed: any mention, and any backslash (a double-quoted
        # key can spell a name with escapes), counts (PR #63 review, #72).
        if "configDependencies" in text:
            errors.append("pnpm-workspace.yaml: configDependencies are not allowed (T-602); remove every mention")
        if "\\" in text:
            errors.append("pnpm-workspace.yaml: backslashes are not allowed (an escaped key could hide a setting)")
        for match in FORBIDDEN_WORKSPACE_KEYS.finditer(text):
            errors.append(f"pnpm-workspace.yaml: the {match.group(1)} setting is not allowed (#72)")
        content = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for match in WORKSPACE_INDIRECT_SYNTAX.finditer(content):
            line = content[content.rfind("\n", 0, match.start()) + 1:].split("\n", 1)[0]
            errors.append(f"pnpm-workspace.yaml: YAML explicit keys, tags, anchors, aliases, merge keys, document "
                          f"markers and line-leading flow collections are not allowed: {line.strip()!r}")
        if not IGNORE_PNPMFILE.search(text) or content.count("ignorePnpmfile") != 1:
            errors.append("pnpm-workspace.yaml: `ignorePnpmfile: true` must be set, exactly once (T-602)")
    return errors


def check(repo: Path, now: datetime) -> list[str]:
    return check_uv_lock(repo, now) + check_js_side(repo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", nargs="?", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--now", help="ISO-8601 time to measure ages against (tests)")
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        errors = check(Path(args.repo), now)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError) as e:
        print(f"check_lockfiles: could not read the lockfiles: {e}", file=sys.stderr)
        return 1
    for e in errors:
        print(f"check_lockfiles: {e}")
    if not errors:
        print("check_lockfiles: ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
