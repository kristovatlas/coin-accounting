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
# "__init__" is the package root's __init__.py: it may import nothing and has no capabilities.
TOP_MODULES = {"launcher", "config", "domain", "api", "services", "doxx", "tax", "chain", "rpc", "prices",
               "storage", "__init__"}

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
    "__init__": set(),
}

# Module (or module.member) -> capability. Matched against the full dotted name of every
# import, including each member of a `from X import Y`.
CAPABILITY_OF_MODULE = {
    "network": {"socket", "ssl", "http.client", "http.server", "urllib.request", "httpx", "requests", "aiohttp",
                "websockets", "ftplib", "smtplib", "xmlrpc.client", "xmlrpc.server", "socketserver",
                "asyncio.open_connection", "asyncio.start_server", "asyncio.open_unix_connection",
                "asyncio.start_unix_server", "asyncio.streams"},
    "webbrowser": {"webbrowser"},
    "subprocess": {"subprocess", "pty", "multiprocessing", "concurrent.futures.process"},
    # Foreign function calls can do anything a C program can, so they get their own capability.
    # Architecture §1: the launcher needs prctl(PR_SET_DUMPABLE, 0) on Linux, which Python's
    # standard library only reaches through ctypes.
    "ctypes": {"ctypes", "_ctypes"},
    "filesystem": {"tempfile", "shutil", "sqlite3", "glob", "fileinput", "pathlib.Path", "pathlib.PosixPath",
                   "pathlib.WindowsPath", "io.open", "io.FileIO", "codecs.open", "os.fdopen", "os.open"},
}
# Importing the bare module `pathlib`/`io`/`codecs`/`os` is fine; their risky members are listed above
# and caught as attribute calls below. `pathlib.PurePath` stays allowed everywhere.
NETWORK_CALL_ATTRS = {"open_connection", "start_server", "open_unix_connection", "start_unix_server",
                      "create_connection", "create_server", "create_datagram_endpoint", "sock_connect"}
OS_FS_FUNCS = {"remove", "unlink", "rename", "replace", "mkdir", "makedirs", "rmdir", "removedirs", "listdir",
               "scandir", "walk", "chmod", "chown", "open", "fdopen", "truncate", "symlink", "link", "mkfifo"}
OS_EXEC_PREFIXES = ("exec", "spawn", "posix_spawn")
OS_EXEC_FUNCS = {"system", "popen", "fork", "forkpty"}
PATH_FS_METHODS = {"write_text", "write_bytes", "read_text", "read_bytes", "open", "mkdir", "unlink", "rename",
                   "replace", "touch", "rmdir", "chmod", "symlink_to", "iterdir", "glob", "rglob", "exists",
                   "is_file", "is_dir", "stat"}
CLOCK_CALLS = {("time", "time"), ("time", "time_ns"), ("datetime", "now"), ("datetime", "utcnow"),
               ("datetime", "today"), ("date", "today")}
PURE = {"domain", "tax", "doxx", "__init__"}

# Capability -> modules (top-level name, or "top/sub.py" path) where it is allowed.
CAPABILITY_ALLOWED = {
    "network": {"rpc", "prices", "launcher"},
    "webbrowser": {"launcher"},
    "subprocess": {"storage/volume.py"},
    "ctypes": {"launcher"},
    "filesystem": {"storage", "launcher"},
}


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


def is_os_exec(name: str) -> bool:
    return name in OS_EXEC_FUNCS or name.startswith(OS_EXEC_PREFIXES)


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
        self.aliases: dict[str, str] = {}      # local name -> fully qualified name
        self.path_vars: set[str] = set()        # local names bound to pathlib.Path(...) values

    def v(self, node: ast.AST, rule: str, detail: str) -> None:
        self.out.append(Violation(self.path, getattr(node, "lineno", 0), rule, detail))

    def need(self, node: ast.AST, capability: str, detail: str) -> None:
        if not allowed(capability, self.top, self.key):
            self.v(node, f"capability-{capability}", detail)

    def check_name(self, node: ast.AST, name: str) -> None:
        """Check one fully qualified imported name (a module, or module.member)."""
        if name == PKG or name.startswith(PKG + "."):
            parts = name.split(".")
            if len(parts) < 2:
                return
            target = parts[1]
            if target not in TOP_MODULES or target == "__init__":
                self.v(node, "import-edge", f"unknown project module {target!r}")
            elif target != self.top and target != "domain" and target not in ALLOWED.get(self.top, set()):
                self.v(node, "import-edge", f"{self.top} may not import {target} (architecture §2)")
            return
        for capability, names in CAPABILITY_OF_MODULE.items():
            if any(name == n or name.startswith(n + ".") for n in names):
                self.need(node, capability, f"import {name}")
        root = name.split(".")[0]
        if root == "importlib":
            self.v(node, "dynamic-import", f"import {name}")
        if root == "os" and "." in name:
            member = name.split(".", 1)[1]
            if is_os_exec(member):
                self.need(node, "subprocess", f"import {name}")
            elif member in OS_FS_FUNCS:
                self.need(node, "filesystem", f"import {name}")
        if self.top == "tax" and root == "math":
            self.v(node, "float-in-tax", "import math (floating point)")
        if self.top in PURE and name in ("time.time", "time.time_ns"):
            self.v(node, "capability-clock", f"import {name} in a pure module")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.check_name(node, a.name)
            self.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        name = resolve_relative(node, self.key) if node.level else (node.module or "")
        if name is None:
            self.v(node, "import-edge", "relative import escapes the package")
            return
        self.check_name(node, name)
        for a in node.names:
            if a.name == "*":
                self.v(node, "import-edge", f"star import from {name}")
                continue
            # Check each member as a qualified name: `from coinacct import tax`,
            # `from urllib import request`, `from os import unlink` (PR #7 review).
            self.check_name(node, f"{name}.{a.name}")
            self.aliases[a.asname or a.name] = f"{name}.{a.name}"
        self.generic_visit(node)

    def qualified(self, expr: ast.expr) -> str | None:
        """Resolve a dotted expression like `t.time` or `dt.now` through the import aliases."""
        if isinstance(expr, ast.Name):
            return self.aliases.get(expr.id, expr.id)
        if isinstance(expr, ast.Attribute):
            base = self.qualified(expr.value)
            return f"{base}.{expr.attr}" if base else None
        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        # Track variables bound to pathlib.Path(...) so `p = Path(x); p.write_text()` is caught.
        if isinstance(node.value, ast.Call):
            q = self.qualified(node.value.func) or ""
            if q.endswith(("pathlib.Path", "pathlib.PosixPath")) or q in ("Path",):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.path_vars.add(t.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name):
            if f.id == "__import__":
                self.v(node, "dynamic-import", "__import__()")
            if f.id == "open":
                self.need(node, "filesystem", "open()")
            if f.id == "float" and self.top == "tax":
                self.v(node, "float-in-tax", "float()")
            if f.id in ("eval", "exec"):
                self.v(node, "code-execution", f"{f.id}()")
        q = self.qualified(f) or ""
        parts = q.split(".")
        if len(parts) >= 2:
            owner, attr = parts[-2], parts[-1]
            if parts[0] == "os" and is_os_exec(attr):
                self.need(node, "subprocess", f"{q}()")
            elif parts[0] == "os" and attr in OS_FS_FUNCS:
                self.need(node, "filesystem", f"{q}()")
            if q in ("io.open", "codecs.open"):
                self.need(node, "filesystem", f"{q}()")
            if attr in NETWORK_CALL_ATTRS:  # asyncio streams and event-loop sockets
                self.need(node, "network", f"{q}()")
            if self.top in PURE and (owner, attr) in CLOCK_CALLS:
                self.v(node, "capability-clock", f"{q}() in a pure module")
        if isinstance(f, ast.Attribute) and f.attr in PATH_FS_METHODS:
            receiver = f.value
            is_path = (isinstance(receiver, ast.Name) and receiver.id in self.path_vars) or (
                isinstance(receiver, ast.Call) and (self.qualified(receiver.func) or "").endswith("Path"))
            if is_path:
                self.need(node, "filesystem", f"Path.{f.attr}()")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        # `a / b` on integer sats yields a float and silently loses precision (#10, T-502).
        # tax/ must use `//` for exact integer division, or the Decimal helper in domain/.
        if self.top == "tax" and isinstance(node.op, ast.Div):
            self.v(node, "float-in-tax", "true division `/` (use `//` or the domain Decimal helper)")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self.top == "tax" and isinstance(node.op, ast.Div):
            self.v(node, "float-in-tax", "true division `/=` (use `//=` or the domain Decimal helper)")
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
        seen = set()
        for v in c.out:
            k = (v.line, v.rule, v.detail)
            if k not in seen:
                seen.add(k)
                out.append(v)
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
