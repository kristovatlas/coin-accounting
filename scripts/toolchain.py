#!/usr/bin/env python3
"""Install the pinned toolchain into .toolchain/ (ENGINEERING §2.3).

Standard library only. Every artifact is checked against the SHA-256 pinned in
scripts/toolchain.lock (JSON) before it is unpacked or made executable, and any mismatch
is a hard error. Nothing here goes through Socket Firewall, because these are
binaries, not registry packages. That is why every one of them is pinned by hash.

Usage:
    toolchain.py install [--only NAME ...] [--include bitcoind]  # pins must match origin/main
    toolchain.py verify NAME...  # check .toolchain/bin/NAME is the pinned, unmodified binary
    toolchain.py path            # print the bin directory to put on PATH
    toolchain.py platform        # print the detected platform key
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

if sys.version_info < (3, 9):  # noqa: UP036 - this script runs on the host Python, before the pinned one exists
    sys.exit("toolchain.py needs Python 3.9 or newer on the host (it installs the pinned Python 3.13).")

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "scripts" / "toolchain.lock"
TOOLCHAIN = ROOT / ".toolchain"
BIN = TOOLCHAIN / "bin"
MARKER = ".installed.json"
# tarfile extraction filters exist from 3.12 (and some security backports); older hosts use our own checks.
HAS_TAR_FILTERS = hasattr(tarfile, "data_filter")

# bitcoind is only needed for regtest tests; it is installed on request.
DEFAULT_TOOLS = ("sfw", "pnpm", "uv", "node", "python")
MAX_DOWNLOAD = 512 * 1024 * 1024
# Pins are approved by the human's merge (ENGINEERING §2.4); the full ref avoids a tag or
# branch that happens to be named origin/main (PR #7 review, round 6).
APPROVED_REF = "refs/remotes/origin/main"


class ToolchainError(Exception):
    pass


def platform_key() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    if system == "Linux" and machine in ("aarch64", "arm64"):
        return "linux-arm64"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "darwin-arm64"
    if system == "Darwin" and machine in ("x86_64", "amd64"):
        return "darwin-x86_64"
    raise ToolchainError(f"unsupported platform: {system} {machine} (ADR 0003: Linux x86_64, macOS)")


def load_lock(path: Path = LOCK) -> dict:
    with path.open() as f:
        data = json.load(f)
    data.pop("_comment", None)
    return data


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
        if HAS_TAR_FILTERS:
            tar.extractall(target, filter="data")
            return
        # Older host Pythons (e.g. macOS's 3.9) lack extraction filters: check each member ourselves.
        root = target.resolve()
        for m in tar.getmembers():
            dest = (target / m.name).resolve()
            if m.name.startswith(("/", "\\")) or ".." in Path(m.name).parts or not (dest == root or root in dest.parents):
                raise tarfile.TarError(f"unsafe path in archive: {m.name!r}")
            if not (m.isfile() or m.isdir() or m.issym()):
                raise tarfile.TarError(f"unsupported member type in archive: {m.name!r}")
            if m.issym():
                link_dest = (dest.parent / m.linkname).resolve()
                if Path(m.linkname).is_absolute() or not (link_dest == root or root in link_dest.parents):
                    raise tarfile.TarError(f"symlink escapes the archive: {m.name!r} -> {m.linkname!r}")
            m.mode &= 0o755
        tar.extractall(target)


def artifact_id(entry: dict) -> str:
    """The pinned identity of an artifact: a SHA-256 hex digest, or an npm `sha512-…` integrity."""
    return entry.get("sha256") or entry["integrity"]


def artifact_matches(entry: dict, path: Path) -> tuple[bool, str]:
    if "sha256" in entry:
        actual = sha256_file(path)
        return actual == entry["sha256"], actual
    algo, _, expected = entry["integrity"].partition("-")
    if algo != "sha512":
        raise ToolchainError(f"unsupported integrity algorithm {algo!r}")
    h = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = base64.b64encode(h.digest()).decode()
    return actual == expected, f"sha512-{actual}"


def install_tool(name: str, spec: dict, key: str, force: bool = False) -> Path:
    entry = spec.get(key)
    if entry is None:
        raise ToolchainError(f"{name}: no artifact pinned for {key}")
    tool_dir = TOOLCHAIN / name / spec["version"]
    cached = read_marker(tool_dir)
    if cached and marker_current(cached, entry) and not force:
        binary = tool_dir / cached["bin"]
        if binary.is_file() and tree_digest(tool_dir) == cached["tree_sha256"]:
            # Installs made before trees were read-only (or whose modes changed since) are
            # hardened here; the digest doesn't record write bits (PR #64 review).
            make_read_only(tool_dir)
            return binary
        # Missing, modified or moved since install: reinstall from the pinned artifact.

    with tempfile.TemporaryDirectory(dir=TOOLCHAIN) as tmp:
        tmpdir = Path(tmp)
        artifact = tmpdir / "artifact"
        print(f"downloading {name} {spec['version']} ({key})", file=sys.stderr)
        download(entry["url"], artifact)
        ok, actual = artifact_matches(entry, artifact)
        if not ok:
            raise ToolchainError(
                f"{name}: hash mismatch\n  expected {artifact_id(entry)}\n  got      {actual}\n  url      {entry['url']}"
            )
        staging = tmpdir / "staging"
        staging.mkdir()
        if entry["kind"] == "binary":
            shutil.copyfile(artifact, staging / entry["bin"])
        elif entry["kind"] in ("tar", "npm-tgz"):
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
        rel_bin = binary.relative_to(staging).as_posix()
        if tool_dir.exists():
            remove_tree(tool_dir)
        tool_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(tool_dir))
        if entry["kind"] == "npm-tgz":
            # A JavaScript package: run its entry point with the pinned Node. Its npm
            # lifecycle scripts are never run (ENGINEERING §2.1).
            wrapper = tool_dir / "run"
            node, script = shlex.quote(str(BIN / "node")), shlex.quote(str(tool_dir / rel_bin))
            wrapper.write_text(f'#!/bin/sh\nexec {node} {script} "$@"\n')
            wrapper.chmod(0o755)
            rel_bin = "run"
        # The digest covers every installed file, not just the entry point: pnpm's JavaScript,
        # Node's libraries (PR #7 review, round 4). The root catches a moved checkout, whose
        # wrapper would still point at the old tree.
        (tool_dir / MARKER).write_text(json.dumps(
            {"artifact": artifact_id(entry), "bin": rel_bin, "root": str(TOOLCHAIN),
             "tree_sha256": tree_digest(tool_dir)}) + "\n")
        try:
            make_read_only(tool_dir)
        except ToolchainError:
            # Don't leave a valid marker on a half-hardened tree: the next run reinstalls (#69).
            tool_dir.chmod(tool_dir.stat().st_mode | 0o700)
            (tool_dir / MARKER).unlink(missing_ok=True)
            raise
        return tool_dir / rel_bin


def make_read_only(tool_dir: Path) -> None:
    """Remove every write bit from an installed tree.

    Running a tool must not change its install. The pinned Python, for one, writes `.pyc`
    files next to its standard library, which broke the tree digest (M0.2). With the tree
    read-only it skips them. Leaving `.pyc` files out of the digest instead would let a
    tampered one run unnoticed.
    """
    paths = [Path(dirpath) / name
             for dirpath, dirnames, filenames in os.walk(tool_dir, topdown=False, followlinks=False)
             for name in filenames + dirnames]
    for path in [*paths, tool_dir]:
        try:
            mode = path.lstat().st_mode
            # Already read-only: no chmod, which fails on files another user owns even when the
            # mode wouldn't change (#69). Symlinks are skipped; their modes mean nothing.
            if not stat.S_ISLNK(mode) and mode & 0o222:
                path.chmod(mode & ~0o222)
        except OSError as e:
            raise ToolchainError(f"can't make {path} read-only ({e.strerror}); was .toolchain created by another "
                                 "user, e.g. with sudo? Fix its ownership, then run 'make toolchain'") from e


def writable_paths(tool_dir: Path) -> list[Path]:
    """Files and directories in an installed tree that still have a write bit (symlinks excluded)."""
    found = [tool_dir] if tool_dir.lstat().st_mode & 0o222 else []
    for dirpath, dirnames, filenames in os.walk(tool_dir, followlinks=False):
        for name in dirnames + filenames:
            mode = (Path(dirpath) / name).lstat().st_mode
            if not stat.S_ISLNK(mode) and mode & 0o222:
                found.append(Path(dirpath) / name)
    return found


def remove_tree(tool_dir: Path) -> None:
    """Delete an installed tree, which `make_read_only` left without write bits."""
    # A symlinked or non-directory tool_dir is refused before any chmod: os.walk would follow
    # the top-level link and add write bits outside .toolchain (#69).
    if not stat.S_ISDIR(tool_dir.lstat().st_mode):
        raise ToolchainError(f"{tool_dir} is not a real directory (a symlink?); refusing to remove it")
    try:
        for dirpath, dirnames, _ in os.walk(tool_dir, followlinks=False):
            Path(dirpath).chmod(Path(dirpath).stat().st_mode | 0o700)
            for name in dirnames:
                path = Path(dirpath) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o700)
        shutil.rmtree(tool_dir)
    except OSError as e:
        raise ToolchainError(f"can't remove {tool_dir} ({e.strerror}); was .toolchain created by another user, "
                             "e.g. with sudo? Fix its ownership, then run 'make toolchain'") from e


def read_marker(tool_dir: Path) -> dict | None:
    try:
        data = json.loads((tool_dir / MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not {"artifact", "bin", "root", "tree_sha256"} <= data.keys():
        return None
    return data


def marker_current(cached: dict, entry: dict) -> bool:
    return cached["artifact"] == artifact_id(entry) and cached["root"] == str(TOOLCHAIN)


def tree_digest(tool_dir: Path) -> str:
    """SHA-256 over every file (content and executable bit), symlink (target) and directory
    under tool_dir, except the marker. Symlinks are recorded, never followed.

    Bytecode caches are included: Python runs a matching `.pyc` instead of its source
    (PR #7 review, round 5). Installed trees are read-only (`make_read_only`), so running a tool
    can't write into them; PYTHONDONTWRITEBYTECODE in the Makefile is a second layer. A stray
    write still fails verification, and `make toolchain` reinstalls. Write bits aren't part of
    the digest: `verify_tool` checks them separately.
    """
    h = hashlib.sha256()
    entries = []
    for dirpath, dirnames, filenames in os.walk(tool_dir, followlinks=False):
        for name in dirnames + filenames:
            entries.append(Path(dirpath) / name)
    for path in sorted(entries, key=lambda p: p.relative_to(tool_dir).as_posix()):
        rel = path.relative_to(tool_dir).as_posix()
        if rel == MARKER:
            continue
        if path.is_symlink():
            h.update(f"L {rel} -> {os.readlink(path)}\n".encode())
        elif path.is_dir():
            h.update(f"D {rel}\n".encode())
        else:
            executable = int(bool(path.stat().st_mode & 0o111))
            h.update(f"F {rel} {executable} {sha256_file(path)}\n".encode())
    return h.hexdigest()


def verify_tool(name: str, lock: dict | None = None, key: str | None = None) -> Path:
    """Check that .toolchain/bin/<name> is the pinned, unmodified install; return its binary.

    Used by `make require-toolchain` so a stale or replaced tool can never run an install
    (ENGINEERING §2.3, "no silent fallback"). Every file of the install is checked, and a
    JavaScript tool (pnpm) also needs the pinned Node that runs it.
    """
    lock = lock or load_lock()
    key = key or platform_key()
    if name not in lock:
        raise ToolchainError(f"unknown tool {name!r}")
    spec = lock[name]
    entry = spec.get(key)
    if entry is None:
        raise ToolchainError(f"{name}: no artifact pinned for {key}")
    tool_dir = TOOLCHAIN / name / spec["version"]
    cached = read_marker(tool_dir)
    if not cached or not marker_current(cached, entry):
        raise ToolchainError(f"{name} {spec['version']} is not installed from the pinned artifact; run 'make toolchain'")
    expected = (tool_dir / cached["bin"]).resolve()
    link_path = BIN / {"python": "python3"}.get(name, name)
    if not link_path.is_symlink() or link_path.resolve() != expected:
        raise ToolchainError(f"{link_path} does not point at the pinned {name} {spec['version']}; run 'make toolchain'")
    if tree_digest(tool_dir) != cached["tree_sha256"]:
        raise ToolchainError(f"{name} was modified after install; check why, then run 'make toolchain'")
    if writable_paths(tool_dir):
        # Read-only is what keeps running a tool from changing it (#69); `make toolchain`
        # re-applies it without reinstalling.
        raise ToolchainError(f"{name} {spec['version']} is writable (e.g. {writable_paths(tool_dir)[0]}); "
                             "run 'make toolchain' to make it read-only again")
    if entry.get("kind") == "binary" and sha256_file(expected) != entry.get("sha256"):
        raise ToolchainError(f"{name} binary hash differs from the pinned artifact")
    if entry.get("kind") == "npm-tgz":
        verify_tool("node", lock, key)
    return expected


def link(bin_path: Path, name: str) -> None:
    BIN.mkdir(parents=True, exist_ok=True)
    link_path = BIN / name
    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(bin_path)


def require_approved_lock(root: Path | None = None) -> None:
    """Refuse to install pins that differ from origin/main (i.e. aren't merged by the human).

    Checked here, not only in the Makefile, so a direct `toolchain.py install` is gated too
    (PR #7 review, round 6). `make toolchain DEPS_APPROVED=1` passes --approved.
    """
    # Resolved at call time (not as a default argument), so the checked repository is always
    # the one ROOT names when the check runs.
    root = root if root is not None else ROOT
    rel = LOCK.relative_to(ROOT).as_posix()

    def git(*args: str) -> int:
        return subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    if git("rev-parse", "-q", "--verify", APPROVED_REF) != 0:
        raise ToolchainError(f"{APPROVED_REF} is unknown, so the pins' approval can't be checked; run 'git fetch origin'")
    if git("diff", "--quiet", APPROVED_REF, "--", rel) != 0:
        raise ToolchainError(f"{rel} differs from origin/main; pins are installed only after the human approves them "
                             "(ENGINEERING §2.4). AI agents: stop and ask the human (AGENTS.md). The human, after approving, "
                             "may rerun with DEPS_APPROVED=1 on the make command line")


def cmd_install(args: argparse.Namespace) -> int:
    if not args.approved:
        require_approved_lock()
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
    p_install.add_argument("--approved", action="store_true", help=argparse.SUPPRESS)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("names", nargs="+")
    sub.add_parser("path")
    sub.add_parser("platform")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "install":
            return cmd_install(args)
        if args.cmd == "verify":
            for name in args.names:
                print(verify_tool(name))
            return 0
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
