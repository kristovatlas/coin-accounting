#!/usr/bin/env python3
"""Mechanical tripwire: flag PR changes that deserve the human's careful look (ADR 0020).

Standard library only. Given a base and a head commit, it lists every changed path, mode
and added or removed line that matches a fixed set of risky patterns:
- agent instructions, CI, scripts, dependency and test configuration, the security-critical
  modules and the binding documents
- process, network or dynamic-code use; new URL hosts; long encoded blobs
- deleted files, removed test or guard code, symlinks, submodules, new executables

It is a heuristic that raises flags. A deliberate author can evade it. It never quotes
source text in its output (only a category, the file and the line number), so it can't leak
a secret. The review panel runs `main`'s copy of it on the exact commit it hands to the human.

Git runs with a fixed configuration: no user or system config, no attributes-driven
textconv or external diff, unquoted paths, NUL-separated names. So the result doesn't
depend on the machine it runs on.

Usage:
    tripwire.py BASE HEAD [--json]
Exit status: 0 = scanned (flags or not); 2 = could not scan (treat as a failed tripwire).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath

if sys.version_info < (3, 9):  # noqa: UP036 - runs on the host Python
    sys.exit("tripwire.py needs Python 3.9 or newer")

PATH_RULES = [
    ("agent instructions or tooling", ["*agents*.md", "*claude*.md", "*skill.md", ".claude/*", "*/.claude/*",
                                       ".codex/*", "*/.codex/*", ".mcp.json", "*/.mcp.json"]),
    ("CI or repository automation", [".github/*", "*/.github/*", ".gitattributes", "*/.gitattributes",
                                     ".gitmodules", ".pre-commit-config.yaml", ".envrc", "*/.envrc"]),
    ("scripts, Makefile or build files", ["scripts/*", "makefile", "*/makefile", "gnumakefile", "*/gnumakefile",
                                          "*.mk", "dockerfile*", "*/dockerfile*", "justfile", "*/justfile"]),
    ("dependency or install config", ["package.json", "*/package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
                                      "pyproject.toml", "*/pyproject.toml", "uv.lock", "uv.toml", "*/uv.toml",
                                      ".npmrc", "*/.npmrc", ".pnpmfile.*", "*/.pnpmfile.*", ".python-version",
                                      ".node-version", "requirements*.txt", "*/requirements*.txt"]),
    ("test, tool or start-up configuration", ["conftest.py", "*/conftest.py", "*.ini", "*.cfg", "*.pth",
                                              "*sitecustomize.py", "*usercustomize.py", "*/mutation-exclusions.md",
                                              "*playwright.config.*", "*vitest.config.*", "*vite.config.*",
                                              "*tsconfig*.json", "*.setup.*"]),
    ("test harness", ["e2e/harness/*"]),
    ("security-critical module", ["backend/coinacct/launcher.py", "backend/coinacct/config.py",
                                  "backend/coinacct/rpc.py", "backend/coinacct/api/*", "backend/coinacct/prices/*",
                                  "backend/coinacct/storage/*", "backend/coinacct/chain/node_checks.py",
                                  "frontend/src/api/*"]),
    ("binding document or ADR", ["docs/adr/*", "docs/architecture.md", "docs/engineering.md",
                                 "docs/threat_model.md", "plan.md", "docs/dependencies.md"]),
]

CODE_SUFFIXES = {".py", ".pyi", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".sh",
                 ".bash", ".zsh", ".yml", ".yaml", ".toml", ".json", ".cfg", ".ini", ".html", ".htm", ".svg",
                 ".css", ".vue", ".svelte", ".mk", ".pth"}
CODE_NAMES = {"makefile", "gnumakefile", "dockerfile", "justfile", ".envrc"}

ADDED_RULES = [
    ("process execution", r"\b(subprocess|child_process|os\.(system|popen|exec\w*|spawn\w*|fork)|pty\b|"
                          r"multiprocessing|ProcessPoolExecutor|create_subprocess_\w+|execa|spawnSync|execSync)\b"),
    ("network use", r"\b(socket|ssl|http\.client|urllib\.request|urlopen|httpx|requests|aiohttp|websockets?|"
                    r"XMLHttpRequest|WebSocket|EventSource|sendBeacon|dgram|tls)\b|\bfetch\s*\(|"
                    r"['\"](node:)?(net|http|https)['\"]"),
    ("dynamic code or deserialisation", r"\b(eval|exec|compile|__import__|importlib|pickle|marshal|ctypes|"
                                        r"getattr|globalThis|Function)\b"),
    ("download or install command", r"\b(pip3?|npm|pnpm|yarn|uv|npx|uvx|curl|wget)\b"),
    ("environment-dependent behaviour", r"\b(os\.environ|process\.env|getenv)\b"),
    ("files outside the repository", r"(~/|\$HOME|expanduser|Path\.home|/etc/|/home/|/Users/|\.ssh|\.config/|"
                                     r"\.codex|\.aws|/dev/mapper|/Volumes/)"),
    ("test weakening", r"(pytest\.mark\.(skip|xfail)|@skip|\.skip\(|\.only\(|pragma: no cover|noqa|"
                       r"type: ignore|--no-verify|fail-under|mutation|mypy:\s*(ignore-errors|disable-error-code)|"
                       r"@ts-nocheck|@ts-ignore|eslint-disable|socket_guard)"),
]
REMOVED_RULES = [
    ("removed test or assertion", r"\b(assert|def test_|expect\(|it\(|test\(|pytest)"),
    ("removed guard or check", r"\b(guard|socket|block|refuse|deny|veracrypt|verify|check)\w*"),
]
URL_RE = re.compile(r"\b(?:https?|wss?|ftp)://([A-Za-z0-9.-]+)")
BLOB_RE = re.compile(r"[A-Za-z0-9+/_-]{120,}={0,2}|\b[0-9a-fA-F]{120,}\b")

GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
           "LC_ALL": "C"}
for _k in [k for k in GIT_ENV if k.startswith("GIT_CONFIG_") and k not in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL")]:
    del GIT_ENV[_k]
GIT_OPTS = ["-c", "core.quotePath=false", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
            "-c", "diff.external=", "-c", "core.attributesFile=" + os.devnull]
DIFF_OPTS = ["--no-color", "--no-ext-diff", "--no-textconv", "--no-renames"]


def git(*args: str) -> bytes:
    return subprocess.run(["git", *GIT_OPTS, *args], check=True, capture_output=True, env=GIT_ENV).stdout


def path_flags(path: str) -> list[str]:
    low = path.lower()
    flags = [label for label, pats in PATH_RULES if any(fnmatch.fnmatch(low, p) for p in pats)]
    if not flags and any(part.startswith(".") for part in PurePosixPath(low).parts):
        flags.append("hidden path (dot component)")
    return flags


def is_code(path: str, head: str, status: str) -> bool:
    p = PurePosixPath(path.lower())
    if p.suffix in CODE_SUFFIXES or p.name in CODE_NAMES or p.name.startswith("dockerfile"):
        return True
    if status == "D":
        return True
    try:  # extensionless scripts: a shebang makes it code
        return git("cat-file", "-p", f"{head}:{path}")[:2] == b"#!"
    except subprocess.CalledProcessError:
        return False


def scan(base: str, head: str) -> list[dict]:
    """Return {"file", "kind", "detail"} flags for merge-base(base, head)..head. Never quotes source."""
    flags: list[dict] = []
    merge_base = git("merge-base", base, head).decode().strip()
    raw = git("diff", *DIFF_OPTS, "--raw", "-z", "--no-abbrev", merge_base, head).split(b"\0")
    entries = []
    i = 0
    while i + 1 < len(raw):
        meta, path = raw[i].decode("utf-8", "replace"), raw[i + 1].decode("utf-8", "replace")
        i += 2
        fields = meta.lstrip(":").split()
        if len(fields) < 5:
            flags.append({"file": path, "kind": "unparsed", "detail": "could not parse this change"})
            continue
        old_mode, new_mode, status = fields[0], fields[1], fields[4]
        entries.append((path, status))
        for mode in sorted({old_mode, new_mode}):
            if mode == "120000":
                flags.append({"file": path, "kind": "symlink", "detail": "symbolic link added, changed or removed"})
            elif mode == "160000":
                flags.append({"file": path, "kind": "submodule", "detail": "git submodule (gitlink)"})
        if new_mode == "100755" and old_mode != "100755":
            flags.append({"file": path, "kind": "executable", "detail": "file made executable"})
        if status == "D":
            flags.append({"file": path, "kind": "deleted", "detail": "file deleted"})
        for label in path_flags(path):
            flags.append({"file": path, "kind": "path", "detail": label})

    for path, status in entries:
        if not is_code(path, head, status):
            patch = git("diff", *DIFF_OPTS, "--text", "-U0", merge_base, head, "--", f":(literal){path}")
            if BLOB_RE.search(patch.decode("utf-8", "replace")):
                flags.append({"file": path, "kind": "content", "detail": "long encoded blob (base64/hex)"})
            continue
        patch = git("diff", *DIFF_OPTS, "--text", "-U0", merge_base, head, "--", f":(literal){path}")
        in_hunk, new_line = False, 0
        for line in patch.decode("utf-8", "replace").split("\n"):
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:  # everything before the first hunk header is diff metadata, never content
                in_hunk, new_line = True, int(m.group(1))
                continue
            if not in_hunk or not line:
                continue
            sign, text = line[0], line[1:]
            if sign == "+":
                for label, pattern in ADDED_RULES:
                    if re.search(pattern, text):
                        flags.append({"file": path, "kind": "content", "detail": f"{label} (line {new_line})"})
                for host in URL_RE.findall(text):
                    flags.append({"file": path, "kind": "content", "detail": f"URL host {host} (line {new_line})"})
                if BLOB_RE.search(text):
                    flags.append({"file": path, "kind": "content",
                                  "detail": f"long encoded blob (line {new_line})"})
                new_line += 1
            elif sign == "-":
                for label, pattern in REMOVED_RULES:
                    if re.search(pattern, text, re.IGNORECASE):
                        flags.append({"file": path, "kind": "removed", "detail": f"{label} (near line {new_line})"})
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
    except (subprocess.CalledProcessError, OSError) as e:
        print("tripwire: FAILED (could not scan)", file=sys.stdout)
        print(f"tripwire: {e}", file=sys.stderr)
        return 2
    print(json.dumps(flags, indent=2) if args.json else render(flags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
