#!/usr/bin/env python3
"""Enforce the module import and capability rules of docs/architecture.md §2.

Standard library only (an AST walk; no import-linter). Hygiene, not a security
boundary: dynamic tricks are banned by rule 'dynamic-import' but can't all be seen.

Usage: check_architecture.py [--src backend/coinacct]
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PKG = "coinacct"
TOP_MODULES = {"launcher", "config", "domain", "api", "services", "doxx", "tax", "chain", "rpc", "prices", "storage"}

# Allowed imports between project modules (architecture §2). Imports inside the same
# top-level module are always allowed, and every module may import `domain`.
ALLOWED: dict[str, set[str]] = {
    "launcher": {"config", "storage", "api"},
    "config": set(),
    "domain": set(),
    "api": {"services"},
    "services": {"doxx", "tax", "chain", "prices", "storage"},
    "doxx": set(),
    "tax": set(),
    "chain": {"rpc", "storage"},
    "rpc": set(),
    "prices": {"storage"},
    "storage": set(),
}

NETWORK_MODULES = {"socket", "ssl", "http.client", "urllib.request", "httpx", "requests", "aiohttp",
                   "websockets", "ftplib", "smtplib", "xmlrpc.client", "socketserver", "http.server"}
FS_MODULES = {"tempfile", "shutil", "sqlite3", "glob", "fileinput"}
PURE = {"domain", "tax", "doxx"}

# Capability -> modules (top-level name, or "top/sub.py" path) where it is allowed.
CAPABILITY_ALLOWED = {
    "network": {"rpc", "prices", "launcher"},
    "webbrowser": {"launcher"},
    "subprocess": {"storage/volume.py"},
    "filesystem": {"storage", "launcher"},
}
FS_METHODS = {"write_text", "write_bytes", "read_text", "read_bytes", "open", "mkdir", "unlink", "rename",
              "replace", "touch", "rmdir", "chmod", "symlink_to", "iterdir", "glob", "rglob"}
OS_FS_FUNCS = {"remove", "unlink", "rename", "replace", "mkdir", "makedirs", "rmdir", "removedirs", "listdir",
               "scandir", "walk", "chmod", "open", "fdopen", "truncate", "symlink", "link"}
OS_EXEC_FUNCS = {"system", "popen", "execv", "execve", "execvp", "execl", "execlp", "spawnv", "spawnl",
                 "posix_spawn", "fork", "forkpty"}
CLOCK_CALLS = {("time", "time"), ("time", "time_ns"), ("datetime", "now"), ("datetime", "utcnow"),
               ("datetime", "today"), ("date", "today")}


@dataclass
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


def module_of(rel: Path) -> tuple[str, str]:
    """Return (top-level module, 'top/rest' path key) for a file relative to the package dir."""
    parts = rel.parts
    top = parts[0][:-3] if len(parts) == 1 and parts[0].endswith(".py") else parts[0]
    return top, "/".join(parts)


def allowed(capability: str, top: str, key: str) -> bool:
    places = CAPABILITY_ALLOWED[capability]
    return top in places or key in places


def resolve_relative(node: ast.ImportFrom, key: str) -> str | None:
    """Turn a relative import into an absolute coinacct.* name."""
    pkg_parts = [PKG] + key.split("/")[:-1]
    if node.level > len(pkg_parts):
        return None
    base = pkg_parts[: len(pkg_parts) - node.level + 1]
    return ".".join(base + ([node.module] if node.module else []))


class Checker(ast.NodeVisitor):
    def __init__(self, top: str, key: str, path: str) -> None:
        self.top, self.key, self.path = top, key, path
        self.out: list[Violation] = []
        self.aliases: dict[str, str] = {}

    def v(self, node: ast.AST, rule: str, detail: str) -> None:
        self.out.append(Violation(self.path, getattr(node, "lineno", 0), rule, detail))

    def check_import(self, node: ast.AST, name: str) -> None:
        if name == PKG or name.startswith(PKG + "."):
            target = name.split(".")[1] if "." in name else None
            if target is None:
                return
            if target not in TOP_MODULES:
                self.v(node, "import-edge", f"unknown project module {target!r}")
            elif target != self.top and target != "domain" and target not in ALLOWED.get(self.top, set()):
                self.v(node, "import-edge", f"{self.top} may not import {target} (architecture §2)")
            return
        root = name.split(".")[0]
        if name in NETWORK_MODULES or root in {"httpx", "requests", "aiohttp", "websockets"}:
            if not allowed("network", self.top, self.key):
                self.v(node, "capability-network", f"import {name}")
        if root == "webbrowser" and not allowed("webbrowser", self.top, self.key):
            self.v(node, "capability-webbrowser", f"import {name}")
        if root == "subprocess" and not allowed("subprocess", self.top, self.key):
            self.v(node, "capability-subprocess", f"import {name}")
        if root in FS_MODULES and not allowed("filesystem", self.top, self.key):
            self.v(node, "capability-filesystem", f"import {name}")
        if root in {"importlib"}:
            self.v(node, "dynamic-import", f"import {name}")
        if self.top == "tax" and root == "math":
            self.v(node, "float-in-tax", "import math (floating point)")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.check_import(node, a.name)
            self.aliases[a.asname or a.name.split(".")[0]] = a.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        name = resolve_relative(node, self.key) if node.level else (node.module or "")
        if name is None:
            self.v(node, "import-edge", "relative import escapes the package")
        else:
            self.check_import(node, name)
            for a in node.names:
                self.aliases[a.asname or a.name] = f"{name}.{a.name}"
                if name in ("os",) and a.name in OS_EXEC_FUNCS and not allowed("subprocess", self.top, self.key):
                    self.v(node, "capability-subprocess", f"from os import {a.name}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name):
            if f.id == "__import__":
                self.v(node, "dynamic-import", "__import__()")
            if f.id == "open" and not allowed("filesystem", self.top, self.key):
                self.v(node, "capability-filesystem", "open()")
            if f.id == "float" and self.top == "tax":
                self.v(node, "float-in-tax", "float()")
            if f.id in ("eval", "exec"):
                self.v(node, "code-execution", f"{f.id}()")
        if isinstance(f, ast.Attribute):
            owner = f.value.id if isinstance(f.value, ast.Name) else (
                f.value.attr if isinstance(f.value, ast.Attribute) else None)
            owner_full = self.aliases.get(owner or "", owner or "")
            if owner_full == "os" and f.attr in OS_EXEC_FUNCS and not allowed("subprocess", self.top, self.key):
                self.v(node, "capability-subprocess", f"os.{f.attr}()")
            if owner_full == "os" and f.attr in OS_FS_FUNCS and not allowed("filesystem", self.top, self.key):
                self.v(node, "capability-filesystem", f"os.{f.attr}()")
            if f.attr in FS_METHODS and owner_full not in ("os",) and not allowed("filesystem", self.top, self.key):
                if isinstance(f.value, ast.Call) or owner in ("Path", "path") or "path" in (owner or "").lower():
                    self.v(node, "capability-filesystem", f".{f.attr}()")
            if self.top in PURE and owner is not None and (owner.split(".")[-1], f.attr) in CLOCK_CALLS:
                self.v(node, "capability-clock", f"{owner}.{f.attr}() in a pure module")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if self.top == "tax" and isinstance(node.value, float):
            self.v(node, "float-in-tax", f"float literal {node.value!r}")
        self.generic_visit(node)


def check_tree(src: Path) -> list[Violation]:
    out: list[Violation] = []
    if not src.exists():
        return out
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src)
        top, key = module_of(rel)
        if top == "__init__.py" or top == "__init__":
            continue
        if top not in TOP_MODULES:
            out.append(Violation(str(path), 0, "unknown-module", f"{top!r} is not in architecture §2"))
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as e:
            out.append(Violation(str(path), e.lineno or 0, "syntax", str(e)))
            continue
        c = Checker(top, key, str(path))
        c.visit(tree)
        out += c.out
    return out


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=root / "backend" / PKG)
    args = ap.parse_args(argv)
    violations = check_tree(args.src)
    for v in violations:
        print(f"check_architecture: {v}", file=sys.stderr)
    if not violations:
        print(f"check_architecture: ok ({args.src})", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
