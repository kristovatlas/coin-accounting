#!/usr/bin/env python3
"""Check ADR rules (ADR 0001, ADR 0014, ENGINEERING §4). Standard library only.

Checks:
  1. Every ADR file has valid front matter (status, date, deciders) and a matching title.
  2. docs/adr/README.md lists exactly the ADRs present, with their titles.
  3. Architecture hash anchor: the highest-numbered ADR with `architecture_sha256`
     must match sha256(docs/architecture.md).
  4. With --base REF: an ADR that exists at REF may change only its status line
     (to accepted/rejected/deprecated/superseded by NNNN). If docs/architecture.md
     changed since REF, exactly one ADR added in this change must carry the new hash.

Usage: check_adrs.py [--root DIR] [--base GIT_REF]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ADR_NAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
STATUS = re.compile(r"^(proposed|accepted|rejected|deprecated|superseded by \d{4})$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HASH = re.compile(r"^[0-9a-f]{64}$")


def front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("missing front matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("unterminated front matter")
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"bad front matter line: {line!r}")
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()
    return fields, text[end + 5 :]


def adr_files(adr_dir: Path) -> dict[str, Path]:
    out = {}
    for p in sorted(adr_dir.glob("*.md")):
        m = ADR_NAME.match(p.name)
        if m and m.group(1) != "0000":
            out.setdefault(m.group(1), p)
    return out


def duplicate_numbers(adr_dir: Path) -> list[str]:
    seen: dict[str, list[str]] = {}
    for p in sorted(adr_dir.glob("*.md")):
        m = ADR_NAME.match(p.name)
        if m and m.group(1) != "0000":
            seen.setdefault(m.group(1), []).append(p.name)
    return [f"ADR number {n} is used by several files: {', '.join(v)}" for n, v in seen.items() if len(v) > 1]


def check_files(adr_dir: Path) -> tuple[list[str], dict[str, dict[str, str]], dict[str, str]]:
    errors: list[str] = []
    metas: dict[str, dict[str, str]] = {}
    titles: dict[str, str] = {}
    for num, path in adr_files(adr_dir).items():
        try:
            meta, body = front_matter(path.read_text())
        except ValueError as e:
            errors.append(f"{path.name}: {e}")
            continue
        for key in ("status", "date", "deciders"):
            if key not in meta:
                errors.append(f"{path.name}: front matter lacks {key!r}")
        if "status" in meta and not STATUS.match(meta["status"]):
            errors.append(f"{path.name}: invalid status {meta['status']!r}")
        if "date" in meta and not DATE.match(meta["date"]):
            errors.append(f"{path.name}: invalid date {meta['date']!r}")
        if "architecture_sha256" in meta and not HASH.match(meta["architecture_sha256"]):
            errors.append(f"{path.name}: architecture_sha256 is not a SHA-256 hex digest")
        m = re.search(rf"^# {num}: (.+)$", body, re.M)
        if not m:
            errors.append(f"{path.name}: first heading must be '# {num}: <title>'")
        else:
            titles[num] = m.group(1).strip()
        metas[num] = meta
    return errors, metas, titles


def check_index(adr_dir: Path, titles: dict[str, str]) -> list[str]:
    readme = adr_dir / "README.md"
    if not readme.exists():
        return ["docs/adr/README.md is missing"]
    listed = dict(re.findall(r"^\| \[(\d{4})\]\([^)]+\) \| (.+?) \|$", readme.read_text(), re.M))
    errors = []
    for num in sorted(set(titles) | set(listed)):
        if num not in listed:
            errors.append(f"README.md: ADR {num} is missing from the index")
        elif num not in titles:
            errors.append(f"README.md: lists ADR {num}, which does not exist")
        elif listed[num] != titles[num]:
            errors.append(f"README.md: title of ADR {num} is {listed[num]!r}, file says {titles[num]!r}")
    return errors


def anchor(metas: dict[str, dict[str, str]]) -> tuple[str, str] | None:
    with_hash = [(n, m["architecture_sha256"]) for n, m in metas.items() if "architecture_sha256" in m]
    return max(with_hash) if with_hash else None


def check_hash(root: Path, metas: dict[str, dict[str, str]]) -> list[str]:
    arch = root / "docs" / "architecture.md"
    a = anchor(metas)
    if not arch.exists():
        return [] if a is None else ["an ADR records architecture_sha256 but docs/architecture.md is missing"]
    if a is None:
        return ["docs/architecture.md exists but no ADR records architecture_sha256 (ADR 0014)"]
    actual = hashlib.sha256(arch.read_bytes()).hexdigest()
    if actual != a[1]:
        return [
            f"docs/architecture.md sha256 {actual} does not match ADR {a[0]} ({a[1]}). "
            "Changing the architecture needs a new ADR with the new hash (ADR 0014)."
        ]
    return []


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def status_of(text: str) -> str:
    try:
        return front_matter(text)[0].get("status", "")
    except ValueError:
        return ""


def check_against_base(root: Path, base: str, metas: dict[str, dict[str, str]]) -> list[str]:
    """ADR 0001: an ADR on the base branch is decided. It can't be removed or renamed,
    and only its status line may change, to `deprecated` or `superseded by NNNN`
    (where NNNN exists and says `supersedes: <this number>`)."""
    errors = []
    adr_dir = root / "docs" / "adr"
    current = adr_files(adr_dir)
    base_paths = [p for p in git(root, "ls-tree", "-r", "--name-only", base, "docs/adr/").split()
                  if ADR_NAME.match(Path(p).name) and not Path(p).name.startswith("0000")]
    base_by_num = {ADR_NAME.match(Path(p).name).group(1): p for p in base_paths}
    for num, rel in sorted(base_by_num.items()):
        if num not in current:
            errors.append(f"{rel}: a decided ADR was removed (ADR 0001)")
            continue
        new_rel = current[num].relative_to(root).as_posix()
        if new_rel != rel:
            errors.append(f"{rel}: a decided ADR was renamed to {new_rel} (ADR 0001)")
            continue
        old = git(root, "show", f"{base}:{rel}")
        new = current[num].read_text()
        strip = lambda t: re.sub(r"^status:.*$", "status:", t, count=1, flags=re.M)  # noqa: E731
        if strip(old) != strip(new):
            errors.append(f"{rel}: a decided ADR may only change its status line (ADR 0001); write a new ADR instead")
        old_status, new_status = status_of(old), status_of(new)
        if new_status != old_status:
            ok = new_status == "deprecated"
            m = re.fullmatch(r"superseded by (\d{4})", new_status)
            if m:
                succ = metas.get(m.group(1), {})
                ok = succ.get("supersedes") == num
            if old_status == "proposed" and new_status in ("accepted", "rejected"):
                ok = True
            if not ok:
                errors.append(f"{rel}: status may not change from {old_status!r} to {new_status!r} (ADR 0001)")
    added_with_hash = [n for n, m in metas.items() if n not in base_by_num and "architecture_sha256" in m]
    arch_changed = git(root, "diff", "--name-only", base, "--", "docs/architecture.md").strip() != ""
    if arch_changed and len(added_with_hash) != 1:
        errors.append(
            "docs/architecture.md changed: add exactly one new ADR with architecture_sha256 "
            f"(found {len(added_with_hash)}) (ADR 0014)"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--base", help="git ref of the target branch, e.g. origin/main")
    args = ap.parse_args(argv)
    adr_dir = args.root / "docs" / "adr"
    errors, metas, titles = check_files(adr_dir)
    errors += duplicate_numbers(adr_dir)
    errors += check_index(adr_dir, titles)
    errors += check_hash(args.root, metas)
    if args.base:
        errors += check_against_base(args.root, args.base, metas)
    for e in errors:
        print(f"check_adrs: {e}", file=sys.stderr)
    if not errors:
        print(f"check_adrs: ok ({len(metas)} ADRs)", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
