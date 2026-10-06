#!/usr/bin/env python3
"""Lockfile policy check (ENGINEERING §2.5). Needs Python 3.11+ (`tomllib`): `make check` uses the
pinned interpreter when it verifies, otherwise the host's (CI runners have 3.12+).

The cooldown only applies when versions are resolved, so a hand-edited or bot-written
lockfile could still bring in a fresh or off-registry package. This check reads every entry:

- `uv.lock`: each package except our own virtual project comes from the PyPI registry, and
  every file (wheel or sdist) is an https URL on files.pythonhosted.org whose file name matches
  the entry's name and version, carries a sha256 hash, and was uploaded at least 7 days ago.
  Declared Python dependencies without a `uv.lock` fail.
- `pnpm-lock.yaml` (the only pnpm lockfile allowed: any other `pnpm-lock*.yaml`, in any case and
  anywhere, fails, as do the settings that create per-package or per-branch lockfiles,
  `sharedWorkspaceLockfile` and `gitBranchLockfile`, or that ignore or relocate it, `lockfile` and
  `lockfileDir`). It is read in full, strictly: lockfile version 9.0, one YAML document, ASCII, only
  the top-level keys pnpm writes for registry packages, no URL, git, tarball, link, file or
  directory source anywhere, no `@jsr/` scope (by default it resolves to JSR's registry), and for every package
  exactly one resolution, `{integrity: sha512-…}`, which pnpm writes when the tarball comes from the
  registry configured for that scope (pinning that to registry.npmjs.org is #36 and #93). Each package's publish time comes from
  `pnpm-lock.times.json`, which `make propose-js` records from the registry: it must be at least 7
  days old. Like `uv.lock`'s `upload-time`, a hand-edited times file could back-date a package; the
  owner accepted that limit for npm too (2026-10-05), since every change needs the owner's approval.
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
PNPM_LOCK = "pnpm-lock.yaml"
PNPM_TIMES = "pnpm-lock.times.json"
# The top-level sections, in the order pnpm writes them; a section out of order fails.
PNPM_TOP_ORDER = ("lockfileVersion", "settings", "importers", "packages", "snapshots")
PNPM_TOP_KEYS = set(PNPM_TOP_ORDER)
PNPM_SETTINGS = {"  autoInstallPeers: true", "  excludeLinksFromLockfile: false"}
# A package name as a key or item: quoted all-or-nothing (a quote left open would make YAML read the
# following lines differently from this check), a scoped name always quoted (YAML can't start a plain
# scalar with `@`), and never the `@jsr/` scope, which by default resolves to JSR's registry.
_SCOPE = r"@(?!jsr/)[a-z0-9][a-z0-9._~-]*/"
_BARE = r"[a-z0-9][a-z0-9._~-]*"
_OPEN = rf"(?P<q>')?(?P<name>(?(q)(?:{_SCOPE})?){_BARE})"  # the closing quote is _CLOSE
_CLOSE = r"(?(q)')"
_VER = r"[0-9][0-9A-Za-z.+-]*"
PNPM_PACKAGE = re.compile(rf"^  {_OPEN}@(?P<ver>{_VER}){_CLOSE}:$")
PNPM_RESOLUTION = re.compile(r"^    resolution: \{integrity: sha512-[A-Za-z0-9+/]{86}==\}$")
# Inside `packages`, every line has one of these shapes, exactly as pnpm 12 writes registry packages.
# Anything else (other indentation, other fields such as `name:`/`version:`/`tarball:`, flow-style
# collections where pnpm writes block style) fails closed, so no entry can be hidden from the check.
PNPM_FIELD = re.compile(r"^    (resolution|engines|os|cpu|libc|hasBin|deprecated|peerDependencies|peerDependenciesMeta):"
                        r"(?: (.*))?$")
PNPM_FLOW_FIELDS = {"resolution", "engines", "os", "cpu", "libc"}  # pnpm writes these as one-line flow values
# One closed, non-nested flow collection per line, read item by item, so no value can stay open and
# swallow the next line. A quote inside a plain flow item (`[a'b, 'c]`) is plain text to YAML, so
# counting quotes isn't enough: each item is either plain with no quote, or one whole quoted scalar.
_FLOW_QUOTED = r"'(?:[^']|'')*'"
_FLOW_ITEM = rf"(?:[A-Za-z0-9][A-Za-z0-9._-]*|{_FLOW_QUOTED})"
_FLOW_PLAIN = r"[^\s,'{}\[\]#&*!|>%@`?:-][^,'{}\[\]#:]*"  # no `:` either: `a: b` is a nested key
# An engine name: any plain YAML token with no quote, space, `:`, `#` or flow indicator, or one whole
# quoted scalar.
_ENGINE_NAME = rf"(?:[^\s,'{{}}\[\]#&*!|>%@`?:\"-][^\s,'{{}}\[\]#:\"]*|{_FLOW_QUOTED})"
_FLOW_PAIR = rf"{_ENGINE_NAME}: (?:{_FLOW_PLAIN}|{_FLOW_QUOTED})"
_FLOW_LIST = re.compile(rf"^\[(?:{_FLOW_ITEM}(?:, {_FLOW_ITEM})*)?\]$")
PNPM_FLOW_VALUE = {"engines": re.compile(rf"^\{{(?:{_FLOW_PAIR}(?:, {_FLOW_PAIR})*)?\}}$"),
                   "os": _FLOW_LIST, "cpu": _FLOW_LIST, "libc": _FLOW_LIST}
# A plain one-line value (`deprecated`, `hasBin`): either one whole quoted scalar, or a block plain
# scalar. In block context a quote after the first character is literal text and the scalar ends
# at the end of the line (later, deeper lines are rejected as unexpected), so `Don't use it` is
# safe. A `#` after a space would start a comment and `: ` a nested mapping, so neither may appear;
# a `#` right after another character is text (`https://x.example/y#z`).
_BLOCK_PLAIN = r"[^\s'{\[|>#&*!%@`?:,-](?:[^#:\s]|\s(?!#)|:(?!\s|$)|(?<=\S)#)*"
PNPM_PLAIN_VALUE = re.compile(rf"^(?:{_FLOW_QUOTED}|{_BLOCK_PLAIN})$")
PNPM_BLOCK_PLAIN_LINE = re.compile(rf"^    (?:deprecated|hasBin): {_BLOCK_PLAIN}$")
PNPM_PEER = re.compile(rf"^      {_OPEN}{_CLOSE}:(?: [^{{\[|>'][^']*|(?: '[^']*'))?$")
PNPM_PEER_META = re.compile(r"^        optional: (?:true|false)$")
# `snapshots` and `importers` are read just as strictly: a snapshot's fields are merged into its
# package by pnpm, so a `name:`/`version:` there would redirect the fetch, and every reference must
# name a checked `packages` entry.
_PEERS = r"(?:\([^\s'\"\\:]+\))*"
PNPM_SNAPSHOT = re.compile(rf"^  {_OPEN}@(?P<ver>{_VER}){_PEERS}{_CLOSE}:(?: \{{\}})?$")
PNPM_SNAPSHOT_FIELD = re.compile(r"^    (?:(dependencies|optionalDependencies|transitivePeerDependencies):|optional: true)$")
# No `name@version` values: that is an npm alias, which would install another package under this
# dependency's name. The committed lockfile has none; one needs an explicit decision.
PNPM_SNAPSHOT_DEP = re.compile(rf"^      {_OPEN}{_CLOSE}: (?P<ver>{_VER}){_PEERS}$")
PNPM_SNAPSHOT_PEER = re.compile(rf"^      - {_OPEN}{_CLOSE}$")
PNPM_IMPORTER = re.compile(r"^  (?:\.|[a-z0-9][a-z0-9._-]*):(?: \{\})?$")
PNPM_IMPORTER_GROUP = re.compile(r"^    (?:dependencies|devDependencies|optionalDependencies):$")
PNPM_IMPORTER_DEP = re.compile(rf"^      {_OPEN}{_CLOSE}:$")
PNPM_IMPORTER_SPEC = re.compile(r"^        specifier: (?:[^\s{\[|>'][^']*|'[^']*')$")
PNPM_IMPORTER_VERSION = re.compile(rf"^        version: (?P<ver>{_VER}){_PEERS}$")
# Anything that names a source other than the default registry. pnpm writes no URL at all for it.
PNPM_FOREIGN = re.compile(r"://|\b(?:link|file|git|github|gitlab|bitbucket|workspace|catalog|npm|jsr|portal|patch):"
                          r"|\btarball\b|\bdirectory\b|\btype: |\brepo: |\bcommit: ")
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


def _snapshot_line(n: int, line: str, state: dict, references: list, snapshots: set) -> list[str]:
    if not line.startswith("    "):
        match = PNPM_SNAPSHOT.match(line)
        if match is None:
            state["key"] = None
            return [f"{PNPM_LOCK}:{n}: unexpected snapshot entry"]
        state["key"], state["field"] = f"{match['name']}@{match['ver']}", None
        snapshots.add(state["key"])
        return []
    if state["key"] is None:
        return [f"{PNPM_LOCK}:{n}: unexpected line in a snapshot"]
    if not line.startswith("      "):
        match = PNPM_SNAPSHOT_FIELD.match(line)
        state["field"] = match.group(1) if match else None
        return [] if match else [f"{PNPM_LOCK}:{n}: unexpected line in a snapshot (only dependencies, "
                                 "optionalDependencies, transitivePeerDependencies and optional: true)"]
    if state["field"] in ("dependencies", "optionalDependencies"):
        match = PNPM_SNAPSHOT_DEP.match(line)
        if match:
            references.append((n, f"{match['name']}@{match['ver']}"))
            return []
    elif state["field"] == "transitivePeerDependencies" and PNPM_SNAPSHOT_PEER.match(line):
        return []
    return [f"{PNPM_LOCK}:{n}: unexpected line in a snapshot"]


def _importer_line(n: int, line: str, state: dict, references: list) -> list[str]:
    if not line.startswith("    "):
        ok = PNPM_IMPORTER.match(line) is not None
        state.update(importer=ok, group=False, dep=None)
        return [] if ok else [f"{PNPM_LOCK}:{n}: unexpected importer"]
    if not line.startswith("      "):
        ok = state["importer"] and PNPM_IMPORTER_GROUP.match(line) is not None
        state.update(group=ok, dep=None)
        return [] if ok else [f"{PNPM_LOCK}:{n}: unexpected line in an importer"]
    if not line.startswith("        "):
        match = PNPM_IMPORTER_DEP.match(line) if state["group"] else None
        state["dep"] = match["name"] if match else None
        return [] if match else [f"{PNPM_LOCK}:{n}: unexpected line in an importer"]
    if state["dep"] is not None:
        if PNPM_IMPORTER_SPEC.match(line):
            return []
        match = PNPM_IMPORTER_VERSION.match(line)
        if match:
            references.append((n, f"{state['dep']}@{match['ver']}"))
            return []
    return [f"{PNPM_LOCK}:{n}: unexpected line in an importer"]


JS_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def js_manifests_with_dependencies(repo: Path) -> list[str]:
    """The manifests (the root and every top-level directory, as check_js_side reads them) that declare
    any dependency."""
    found = []
    for manifest in [repo / "package.json", *sorted(repo.glob("*/package.json"))]:
        rel = manifest.relative_to(repo).as_posix()
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text())
        except json.JSONDecodeError:
            found.append(rel)  # unreadable: treat as declaring dependencies, so it fails closed
            continue
        if isinstance(data, dict) and any(data.get(field) for field in JS_DEPENDENCY_FIELDS):
            found.append(rel)
    return found


def check_pnpm_lock(repo: Path, now: datetime) -> list[str]:
    """ENGINEERING §2.5 for the npm side. A strict line-by-line read of the format pnpm 12 writes for
    registry packages: anything else fails closed, so nothing is skipped by a lenient parser."""
    path = repo / PNPM_LOCK
    if not path.exists():
        declared = [m for m in js_manifests_with_dependencies(repo)]
        if declared:
            return [f"{PNPM_LOCK} is missing, but {', '.join(declared)} declare dependencies: run make propose-js"]
        return []
    raw = path.read_bytes()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return [f"{PNPM_LOCK}: non-ASCII content (including a byte-order mark)"]
    lines = text.split("\n")
    if lines[0] != "lockfileVersion: '9.0'":
        return [f"{PNPM_LOCK}: the first line must be lockfileVersion: '9.0'"]
    errors: list[str] = []
    for n, line in enumerate(lines, 1):
        if "\t" in line or "\r" in line:
            errors.append(f"{PNPM_LOCK}:{n}: tabs and carriage returns are not allowed")
        if line.startswith(("---", "...", "%")) or re.search(r"(?:^|[\s:,\[{])[&*!?]", line):
            errors.append(f"{PNPM_LOCK}:{n}: only one plain YAML document is allowed (no markers, anchors, "
                          "aliases, tags or explicit keys)")
        if PNPM_FOREIGN.search(line):
            errors.append(f"{PNPM_LOCK}:{n}: a source other than the npm registry")
        if '"' in line or "\\" in line:
            # pnpm writes plain and single-quoted scalars only; a double-quoted one can hide a source
            # behind escapes (`"li\\u006ek:../x"` decodes to `link:../x`) that the scan above can't see.
            errors.append(f"{PNPM_LOCK}:{n}: double quotes and backslashes are not allowed")
        if line.count("'") % 2 and not PNPM_BLOCK_PLAIN_LINE.match(line):
            # pnpm escapes a quote inside a quoted scalar as `''`, so its lines have an even count, except
            # a block plain value, where a quote after the first character is literal text
            errors.append(f"{PNPM_LOCK}:{n}: a single quote is left open")
    section = None
    seen_sections: set[str] = set()
    packages: dict[str, int] = {}  # "name@version" -> resolution lines seen
    current = None
    field = None  # the open four-space field of the current package entry
    snapshots: set[str] = set()  # "name@version" of every snapshot (peer suffix removed)
    references: list[tuple[int, str]] = []  # (line, "name@version") from snapshot deps and importers
    snap_state: dict[str, object] = {"key": None, "field": None}
    imp_state: dict[str, object] = {"importer": False, "group": False, "dep": None}
    for n, line in enumerate(lines, 1):
        if not line:
            continue
        if not line.startswith(" "):
            key = line.split(":", 1)[0]
            if key not in PNPM_TOP_KEYS:
                errors.append(f"{PNPM_LOCK}:{n}: top-level key {key!r} is not allowed")
            elif key in seen_sections:
                errors.append(f"{PNPM_LOCK}:{n}: the {key} section appears twice")
            elif any(PNPM_TOP_ORDER.index(seen) > PNPM_TOP_ORDER.index(key) for seen in seen_sections & PNPM_TOP_KEYS):
                errors.append(f"{PNPM_LOCK}:{n}: the {key} section is out of order ({', '.join(PNPM_TOP_ORDER)})")
            elif n != 1 and line != f"{key}:":
                # e.g. `packages: {...}`: a flow-style section would hide every entry from the check
                errors.append(f"{PNPM_LOCK}:{n}: the {key} section must be written as a block (`{key}:` alone)")
            seen_sections.add(key)
            section, current, field = key, None, None
            continue
        if section == "settings" and line not in PNPM_SETTINGS:
            errors.append(f"{PNPM_LOCK}:{n}: unexpected setting")
        if section not in ("settings", "importers", "packages", "snapshots"):
            errors.append(f"{PNPM_LOCK}:{n}: unexpected indented line under {section}")
            continue
        if section != "packages":
            if line.lstrip().startswith("resolution:"):
                errors.append(f"{PNPM_LOCK}:{n}: a resolution outside the packages section")
            elif section == "snapshots":
                errors += _snapshot_line(n, line, snap_state, references, snapshots)
            elif section == "importers":
                errors += _importer_line(n, line, imp_state, references)
            continue
        if not line.startswith("    "):
            match = PNPM_PACKAGE.match(line)
            if match is None:
                errors.append(f"{PNPM_LOCK}:{n}: unexpected package entry")
                current, field = None, None
                continue
            current, field = f"{match['name']}@{match['ver']}", None
            if current in packages:
                errors.append(f"{PNPM_LOCK}:{n}: {current} appears twice")
            packages[current] = 0
        elif not line.startswith("      "):
            match = PNPM_FIELD.match(line)
            if current is None or match is None:
                errors.append(f"{PNPM_LOCK}:{n}: unexpected line in a package entry (only "
                              "resolution, engines, os, cpu, libc, hasBin, deprecated and peer dependencies)")
                field = None
                continue
            field, value = match.group(1), match.group(2) or ""
            if field == "resolution":
                if PNPM_RESOLUTION.match(line):
                    packages[current] += 1
                else:
                    errors.append(f"{PNPM_LOCK}:{n}: a resolution must be exactly {{integrity: sha512-...}}")
            elif field in PNPM_FLOW_FIELDS:
                if not PNPM_FLOW_VALUE[field].match(value):
                    errors.append(f"{PNPM_LOCK}:{n}: {field} must be a one-line value")
            elif field.startswith("peerDependencies"):
                if value:
                    errors.append(f"{PNPM_LOCK}:{n}: {field} must be written as a block")
            elif not PNPM_PLAIN_VALUE.match(value):
                errors.append(f"{PNPM_LOCK}:{n}: {field} must be a plain one-line value")
        elif field in ("peerDependencies", "peerDependenciesMeta") and PNPM_PEER.match(line):
            pass
        elif field == "peerDependenciesMeta" and PNPM_PEER_META.match(line):
            pass
        else:
            errors.append(f"{PNPM_LOCK}:{n}: unexpected line in a package entry")
    if not packages and js_manifests_with_dependencies(repo):
        errors.append(f"{PNPM_LOCK} has no packages, but {', '.join(js_manifests_with_dependencies(repo))} "
                      "declare dependencies: run make propose-js")
    for pkg, count in packages.items():
        if count != 1:
            errors.append(f"{PNPM_LOCK}: {pkg} must have exactly one registry resolution")
    for pkg in sorted(snapshots - packages.keys()):
        errors.append(f"{PNPM_LOCK}: the snapshot {pkg} has no entry in packages")
    for pkg in sorted(packages.keys() - snapshots):
        errors.append(f"{PNPM_LOCK}: {pkg} has no snapshot")
    for n, ref in references:
        if ref not in packages:
            errors.append(f"{PNPM_LOCK}:{n}: {ref} is referenced but has no entry in packages")
    errors += check_pnpm_times(repo, packages, now)
    return errors


def check_pnpm_times(repo: Path, packages: dict[str, int], now: datetime) -> list[str]:
    times_path = repo / PNPM_TIMES
    if not packages:
        return []
    if not times_path.exists():
        return [f"{PNPM_TIMES} is missing: run make propose-js to record publish times"]
    times = json.loads(times_path.read_text())
    if not isinstance(times, dict):
        return [f"{PNPM_TIMES}: not a JSON object"]
    errors = []
    for pkg in packages:
        published = times.get(pkg)
        if not isinstance(published, str):
            errors.append(f"{PNPM_TIMES}: no publish time for {pkg}, so its age can't be checked")
            continue
        when = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if now - when < COOLDOWN:
            errors.append(f"{PNPM_LOCK}: {pkg} was published {when:%Y-%m-%d}, less than 7 days ago "
                          "(cooldown, ENGINEERING §2.1)")
    return errors


def check_js_side(repo: Path) -> list[str]:
    errors: list[str] = []
    files = list(walk(repo))
    for lockfile in files:  # case-insensitive, like the .pnpmfile check (PR #81 review)
        name = lockfile.name.lower()
        if name.startswith("pnpm-lock") and name.endswith(".yaml") and lockfile != repo / PNPM_LOCK:
            errors.append(f"{lockfile.relative_to(repo)}: only {PNPM_LOCK} at the repository root is allowed")
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
    return check_uv_lock(repo, now) + check_js_side(repo) + check_pnpm_lock(repo, now)


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
