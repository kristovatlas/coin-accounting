#!/usr/bin/env python3
"""Mechanical tripwire: flag PR changes that deserve the human's careful look (ADR 0020).

Standard library only. Given a base and a head commit, lists every changed path, mode and
added line that matches a fixed set of risky patterns: agent instructions, CI, scripts,
dependency and test configuration, network or process use, dynamic code, obfuscated blobs,
symlinks. It never decides that a change is safe; it only raises flags. The review panel runs
it, next to a separate Opus tripwire review, on the exact commit it hands to the human.

Usage:
    tripwire.py BASE HEAD [--json]   # always exits 0 unless git fails; flags go to stdout
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import PurePosixPath

if sys.version_info < (3, 9):  # noqa: UP036 - runs on the host Python
    sys.exit("tripwire.py needs Python 3.9 or newer")

# Paths whose change always needs the human's eyes. Matched against every path segment
# pattern with fnmatch (so `*` crosses `/`), case-insensitively.
PATH_RULES = [
    ("agent instructions or tooling", ["*agents*.md", "*claude*.md", "*skill.md", ".claude/*", "*/.claude/*",
                                       ".codex/*", "*/.codex/*", ".mcp.json", "*/.mcp.json"]),
    ("CI or repository automation", [".github/*", "*/.github/*", ".gitattributes", "*/.gitattributes",
                                     ".gitmodules", ".pre-commit-config.yaml"]),
    ("scripts, Makefile or controls", ["scripts/*", "makefile", "*.mk"]),
    ("dependency or install config", ["package.json", "*/package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
                                      "pyproject.toml", "*/pyproject.toml", "uv.lock", "uv.toml", "*/uv.toml",
                                      ".npmrc", "*/.npmrc", ".pnpmfile.*", "*/.pnpmfile.*", ".python-version",
                                      ".node-version", "requirements*.txt"]),
    ("test or tool configuration", ["conftest.py", "*/conftest.py", "*.ini", "*.cfg", "*.pth",
                                    "*/mutation-exclusions.md", "playwright.config.*", "*/playwright.config.*",
                                    "vite.config.*", "*/vite.config.*", "tsconfig*.json", "*/tsconfig*.json"]),
    ("test harness", ["e2e/harness/*"]),
    ("security-critical module", ["backend/coinacct/launcher.py", "backend/coinacct/config.py",
                                  "backend/coinacct/rpc.py", "backend/coinacct/api/*", "backend/coinacct/prices/*",
                                  "backend/coinacct/storage/*", "backend/coinacct/chain/node_checks.py",
                                  "frontend/src/api/*"]),
    ("binding document or ADR", ["docs/adr/*", "docs/architecture.md", "docs/engineering.md",
                                 "docs/threat_model.md", "plan.md", "docs/dependencies.md"]),
]

CODE_SUFFIXES = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh",
                 ".yml", ".yaml", ".toml", ".json", ".cfg", ".ini", ".html", ".vue", ".svelte"}

# Content rules for added lines in code files: (label, regex).
CONTENT_RULES = [
    ("process execution", r"\b(subprocess|child_process|os\.(system|popen|exec\w*|spawn\w*|fork)|pty\.|"
                          r"multiprocessing|ProcessPoolExecutor|create_subprocess_\w+|execa|spawnSync|execSync)\b"),
    ("network use", r"\b(socket|ssl|http\.client|urllib\.request|urlopen|httpx|requests|aiohttp|websockets?|"
                    r"XMLHttpRequest|WebSocket|EventSource|navigator\.sendBeacon|require\(['\"](net|http|https|"
                    r"dgram|tls)['\"]\)|from ['\"](node:)?(net|http|https|dgram|tls)['\"])\b|\bfetch\s*\("),
    ("dynamic code or deserialisation", r"\b(eval|exec|compile|__import__|importlib|pickle\.loads?|marshal\.loads?|"
                                        r"ctypes|new Function|setattr\(\s*(builtins|sys|os))\b"),
    ("install or fetch-and-run", r"\b(pip3?|npm|pnpm|yarn|uv|npx|uvx|curl|wget)\s+(install|add|i|dlx|run|-[a-zA-Z]*O|"
                                 r"-fsSL|--output)\b"),
    ("environment-dependent behaviour", r"\b(os\.environ|process\.env)\b.*\b(CI|GITHUB_ACTIONS|USER|HOME)\b"),
    ("files outside the repository", r"(~/|\$HOME|expanduser|Path\.home\(\)|/etc/|/home/|/Users/|\.ssh|\.config/gh|"
                                     r"\.codex|\.aws|/dev/mapper)"),
    ("test weakening", r"(pytest\.mark\.(skip|xfail)|@skip|\.skip\(|it\.only|describe\.skip|pragma: no cover|"
                       r"# noqa|type: ignore|--no-verify|--cov-fail-under)"),
]
URL_RE = re.compile(r"\b(?:https?|wss?|ftp)://([A-Za-z0-9.-]+)")
BLOB_RE = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}|\b[0-9a-fA-F]{120,}\b")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def path_flags(path: str) -> list[str]:
    low = path.lower()
    flags = [label for label, pats in PATH_RULES if any(fnmatch.fnmatch(low, p) for p in pats)]
    if any(part.startswith(".") for part in PurePosixPath(low).parts) and not flags:
        flags.append("hidden path (dot component)")
    return flags


def scan(base: str, head: str) -> list[dict]:
    """Return a list of {"file", "kind", "detail"} flags for the change base..head."""
    flags: list[dict] = []
    merge_base = git("merge-base", base, head).strip()
    for line in git("diff", "--raw", "--no-renames", "--no-abbrev", merge_base, head).splitlines():
        meta, _, path = line.partition("\t")
        old_mode, new_mode = meta.lstrip(":").split()[:2]
        for mode in {old_mode, new_mode}:
            if mode == "120000":
                flags.append({"file": path, "kind": "symlink", "detail": "symbolic link added or changed"})
            elif mode == "160000":
                flags.append({"file": path, "kind": "submodule", "detail": "git submodule (gitlink)"})
        if new_mode == "100755" and old_mode != "100755":
            flags.append({"file": path, "kind": "executable", "detail": "file made executable"})
        for label in path_flags(path):
            flags.append({"file": path, "kind": "path", "detail": label})

    current = None
    for line in git("diff", "--no-renames", "-U0", "--no-color", merge_base, head).splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
            continue
        if line.startswith("Binary files") and " and b/" in line:
            flags.append({"file": line.rsplit(" and b/", 1)[1].rsplit(" differ", 1)[0], "kind": "binary",
                          "detail": "binary file added or changed"})
            continue
        if current is None or not line.startswith("+") or line.startswith("+++"):
            continue
        text = line[1:]
        suffix = PurePosixPath(current).suffix.lower()
        is_code = suffix in CODE_SUFFIXES or PurePosixPath(current).name.lower() == "makefile"
        if BLOB_RE.search(text):
            flags.append({"file": current, "kind": "content", "detail": "long encoded blob (base64/hex)"})
        if not is_code:
            continue
        for label, pattern in CONTENT_RULES:
            if re.search(pattern, text):
                flags.append({"file": current, "kind": "content", "detail": f"{label}: {text.strip()[:120]}"})
        for host in URL_RE.findall(text):
            flags.append({"file": current, "kind": "content", "detail": f"URL host {host}"})
    # One entry per (file, detail).
    seen, unique = set(), []
    for f in flags:
        key = (f["file"], f["detail"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def render(flags: list[dict]) -> str:
    if not flags:
        return "tripwire: no flags"
    lines = [f"tripwire: {len(flags)} flag(s)"]
    for f in flags:
        lines.append(f"- `{f['file']}` ({f['kind']}): {f['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        flags = scan(args.base, args.head)
    except subprocess.CalledProcessError as e:
        print(f"tripwire: git failed: {e.stderr.strip()}", file=sys.stderr)
        return 2
    print(json.dumps(flags, indent=2) if args.json else render(flags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
