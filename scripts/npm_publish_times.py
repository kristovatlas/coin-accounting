#!/usr/bin/env python3
"""Record the npm publish time of every package in pnpm-lock.yaml (ENGINEERING §2.5).

`make propose-js` runs this after resolving, so the lockfile policy check can enforce the 7-day
cooldown offline. Each missing `name@version` is looked up with the pinned pnpm's `view` command,
run under Socket Firewall (the registry's own publish times, `time --json`); entries already
recorded are kept, and entries no longer in the lockfile are dropped. Nothing is installed or run
from a package.

Usage:
    npm_publish_times.py LOCKFILE TIMESFILE -- SFW PNPM
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_lockfiles import PNPM_PACKAGE  # noqa: E402 - the same entry syntax the check accepts


def packages(lockfile: Path) -> list[tuple[str, str]]:
    found, in_packages = [], False
    for line in lockfile.read_text().split("\n"):
        if line and not line.startswith(" "):
            in_packages = line.startswith("packages:")
            continue
        match = PNPM_PACKAGE.match(line) if in_packages else None
        if match:
            found.append((match["name"], match["ver"]))
    return found


def publish_time(command: list[str], name: str, version: str) -> str:
    # pnpm's output goes to files, not pipes: under sfw a pipe can be non-blocking, and pnpm
    # aborts when a large write to it returns EAGAIN.
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        done = subprocess.run(  # noqa: S603 - the pinned sfw and pnpm, a fixed subcommand
            [*command, "view", f"{name}@{version}", "time", "--json"],
            stdout=out_file, stderr=err_file, check=False, timeout=120,
        )
        if done.returncode != 0:
            err_file.seek(0)
            err = err_file.read().decode(errors="replace")
            raise SystemExit(f"npm_publish_times: looking up {name}@{version} failed "
                             f"(exit {done.returncode}):\n{err.strip()[-2000:]}")
        out_file.seek(0)
        out = out_file.read().decode()
    try:
        times = json.loads(out)
    except json.JSONDecodeError:
        raise SystemExit(f"npm_publish_times: the lookup of {name}@{version} returned no JSON") from None
    published = times.get(version) if isinstance(times, dict) else None
    if not isinstance(published, str):
        raise SystemExit(f"npm_publish_times: the registry reported no publish time for {name}@{version}")
    try:
        datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(f"npm_publish_times: {name}@{version} has a malformed publish time") from None
    return published


def main(argv: list[str]) -> int:
    if len(argv) < 5 or argv[2] != "--":
        print(__doc__, file=sys.stderr)
        return 2
    lockfile, times_file, command = Path(argv[0]), Path(argv[1]), argv[3:]
    try:
        old = json.loads(times_file.read_text()) if times_file.exists() else {}
    except json.JSONDecodeError:
        raise SystemExit(f"npm_publish_times: {times_file} is not valid JSON") from None
    if not isinstance(old, dict):
        raise SystemExit(f"npm_publish_times: {times_file} is not a JSON object")
    new = {}
    for name, version in packages(lockfile):
        key = f"{name}@{version}"
        new[key] = old.get(key) or publish_time(command, name, version)
    times_file.write_text(json.dumps(dict(sorted(new.items())), indent=1) + "\n")
    print(f"npm_publish_times: {len(new)} packages recorded in {times_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
