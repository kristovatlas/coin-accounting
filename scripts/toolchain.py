#!/usr/bin/env python3
"""Install the pinned toolchain into .toolchain/ (ENGINEERING §2.3).

Standard library only. Every artifact is checked against the SHA-256 pinned in
scripts/toolchain.lock before it is unpacked or made executable, and any mismatch
is a hard error. Nothing here goes through Socket Firewall, because these are
binaries, not registry packages. That is why every one of them is pinned by hash.

Usage:
    toolchain.py install [--only NAME ...] [--include bitcoind]
    toolchain.py path            # print the bin directory to put on PATH
    toolchain.py platform        # print the detected platform key
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "scripts" / "toolchain.lock"
TOOLCHAIN = ROOT / ".toolchain"
BIN = TOOLCHAIN / "bin"

# bitcoind is only needed for regtest tests; it is installed on request.
DEFAULT_TOOLS = ("sfw", "pnpm", "uv", "node", "python")
MAX_DOWNLOAD = 512 * 1024 * 1024


class ToolchainError(Exception):
    pass


def platform_key() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "darwin-arm64"
    if system == "Darwin" and machine in ("x86_64", "amd64"):
        return "darwin-x86_64"
    raise ToolchainError(f"unsupported platform: {system} {machine} (ADR 0003: Linux x86_64, macOS)")


def load_lock(path: Path = LOCK) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    if not url.startswith("https://"):
        raise ToolchainError(f"refusing non-HTTPS URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "coinacct-toolchain"})
    # Default SSL context: certificate and hostname verification are on.
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as out:
        total = 0
        while chunk := resp.read(1 << 20):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise ToolchainError(f"download too large: {url}")
            out.write(chunk)


def safe_extract(archive: Path, target: Path) -> None:
    """Extract a tarball, refusing absolute paths, `..` and links that escape."""
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")


def install_tool(name: str, spec: dict, key: str, force: bool = False) -> Path:
    entry = spec.get(key)
    if entry is None:
        raise ToolchainError(f"{name}: no artifact pinned for {key}")
    tool_dir = TOOLCHAIN / name / spec["version"]
    marker = tool_dir / ".sha256"
    if marker.exists() and marker.read_text().strip() == entry["sha256"] and not force:
        return tool_dir / entry["bin"]

    with tempfile.TemporaryDirectory(dir=TOOLCHAIN) as tmp:
        tmpdir = Path(tmp)
        artifact = tmpdir / "artifact"
        print(f"downloading {name} {spec['version']} ({key})", file=sys.stderr)
        download(entry["url"], artifact)
        actual = sha256_file(artifact)
        if actual != entry["sha256"]:
            raise ToolchainError(
                f"{name}: SHA-256 mismatch\n  expected {entry['sha256']}\n  got      {actual}\n  url      {entry['url']}"
            )
        staging = tmpdir / "staging"
        staging.mkdir()
        if entry["kind"] == "binary":
            shutil.copyfile(artifact, staging / entry["bin"])
        elif entry["kind"] == "tar":
            safe_extract(artifact, staging)
        else:
            raise ToolchainError(f"{name}: unknown kind {entry['kind']!r}")
        binary = staging / entry["bin"]
        if not binary.is_file():
            # Many tarballs wrap their content in one top-level directory.
            matches = [p for p in staging.rglob(Path(entry["bin"]).name) if p.is_file()]
            if len(matches) != 1:
                raise ToolchainError(f"{name}: expected binary {entry['bin']!r} not found in artifact")
            binary = matches[0]
        binary.chmod(0o755)
        if tool_dir.exists():
            shutil.rmtree(tool_dir)
        tool_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(tool_dir))
        (tool_dir / ".sha256").write_text(entry["sha256"] + "\n")
        return tool_dir / binary.relative_to(staging)


def link(bin_path: Path, name: str) -> None:
    BIN.mkdir(parents=True, exist_ok=True)
    link_path = BIN / name
    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(bin_path)


def cmd_install(args: argparse.Namespace) -> int:
    key = platform_key()
    lock = load_lock()
    tools = list(args.only or DEFAULT_TOOLS) + list(args.include or [])
    TOOLCHAIN.mkdir(exist_ok=True)
    for name in tools:
        if name not in lock:
            raise ToolchainError(f"unknown tool {name!r}")
        path = install_tool(name, lock[name], key, force=args.force)
        # Only the tool itself is linked. npm/npx ship inside the Node tarball
        # and are deliberately not put on PATH (ENGINEERING §2.3).
        link(path, {"python": "python3"}.get(name, name))
        print(f"ok  {name} {lock[name]['version']}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser("install")
    p_install.add_argument("--only", nargs="+")
    p_install.add_argument("--include", nargs="+")
    p_install.add_argument("--force", action="store_true")
    sub.add_parser("path")
    sub.add_parser("platform")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "install":
            return cmd_install(args)
        if args.cmd == "path":
            print(BIN)
            return 0
        if args.cmd == "platform":
            print(platform_key())
            return 0
    except ToolchainError as e:
        print(f"toolchain: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
