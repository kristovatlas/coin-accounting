"""Banned install and fetch-and-run commands (ENGINEERING §2.3, AGENTS.md).

Shared by check_install_commands.py (CI) and agent_guard.py (Claude Code hook).

Approach (PR #7 review, round 2): tokenize each command, strip wrappers and leading
VAR=value assignments, skip global options, then judge the subcommand. The package
managers get **allowlists** of safe subcommands, so an unknown or mis-parsed form is
blocked rather than let through. This is hygiene, not a security boundary.
"""

from __future__ import annotations

import os
import re
import shlex

# Tools that are banned whatever their arguments.
BANNED_TOOLS = {
    "npx": "npx", "pnpx": "npx", "bunx": "bunx", "uvx": "uvx", "yarn": "yarn", "bun": "bun",
    "corepack": "corepack", "pipx": "pipx", "pre-commit": "pre-commit", "easy_install": "easy_install",
}
# Safe subcommands per package manager; anything else is a violation.
ALLOWED_SUBCOMMANDS = {
    "pnpm": {"run", "test", "t", "list", "ls", "why", "audit", "outdated", "licenses", "help", "config"},
    "npm": {"view", "help", "ls", "list", "audit", "explain"},
    "uv": {"run", "version", "help", "tree"},
    "pip": {"list", "show", "freeze", "check", "help"},
}
# Options that take a separate value (so the value isn't mistaken for the subcommand).
VALUE_OPTIONS = {
    "pnpm": {"-F", "--filter", "-C", "--dir", "--reporter", "--loglevel", "--workspace-concurrency"},
    "npm": {"--prefix", "-w", "--workspace", "--loglevel", "--registry"},
    "uv": {"--directory", "--project", "--config-file", "--cache-dir", "--color", "--python", "-p"},
    "pip": {"--log", "--proxy", "--timeout", "--cache-dir", "--python"},
}
WRAPPERS = {"env", "command", "exec", "nice", "nohup", "time", "sudo", "doas", "xargs", "stdbuf", "timeout"}
INTERPRETERS = re.compile(r"^((ba|z|da|k|c)?sh|python[0-9.]*|node|perl|ruby|php)$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FETCHERS = {"curl", "wget"}

_SPLIT = re.compile(r"&&|\|\||;|&|\||\n|`|\$\(|<\(|\(|\)|\{|\}")
PINNED_SFW = "__PINNED_SFW__"


def segments(command: str) -> list[str]:
    """Split a shell command line into individual command segments (approximate)."""
    return [s.strip() for s in _SPLIT.split(command) if s.strip()]


def tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def strip_prefix(toks: list[str]) -> tuple[list[str], list[str]]:
    """Remove leading VAR=value assignments and wrapper commands (with their flags).

    Returns (assignments, remaining tokens)."""
    assigns: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if ASSIGNMENT.match(t):
            assigns.append(t)
            i += 1
        elif os.path.basename(t) in WRAPPERS:
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or ASSIGNMENT.match(toks[i])):
                if ASSIGNMENT.match(toks[i]):
                    assigns.append(toks[i])
                i += 1
        else:
            break
    return assigns, toks[i:]


def subcommand(tool: str, args: list[str]) -> str | None:
    """First non-option argument, skipping options (and the values of value-taking ones)."""
    values = VALUE_OPTIONS.get(tool, set())
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if a.startswith("-"):
            if a in values:
                i += 1
            i += 1
            continue
        return a
    return None


def judge(toks: list[str], assigns: list[str]) -> list[str]:
    """Violations for one command (after wrappers are stripped)."""
    if not toks:
        return []
    tool = os.path.basename(toks[0])
    args = toks[1:]
    if tool in BANNED_TOOLS:
        return [BANNED_TOOLS[tool]]
    if re.fullmatch(r"pip[0-9.]*", tool):
        tool = "pip"
    if INTERPRETERS.match(tool) and len(args) >= 2 and args[0] == "-m":
        mod = args[1]
        if mod in ("pip", "pip3"):
            tool, args = "pip", args[2:]
        elif mod == "ensurepip":
            return ["ensurepip"]
    if tool in ALLOWED_SUBCOMMANDS:
        sub = subcommand(tool, args)
        if sub is None:
            return []  # e.g. `pnpm --version`
        if tool == "uv" and sub == "pip":
            rest = args[args.index("pip") + 1:]
            inner = subcommand("pip", rest)
            return [] if inner in ALLOWED_SUBCOMMANDS["pip"] else [f"uv pip {inner}"]
        if sub not in ALLOWED_SUBCOMMANDS[tool]:
            return [f"{tool} {sub}"]
        if tool == "uv" and sub == "run":
            run_args = args[args.index("run") + 1:]
            if any(a in ("--with", "-w", "--with-requirements") or a.startswith("--with=") for a in run_args):
                return ["uv run --with"]
            before_cmd = []
            for a in run_args:
                if not a.startswith("-"):
                    break
                before_cmd.append(a)
            no_sync = "--no-sync" in before_cmd or "--no-sync" in args[: args.index("run")] or any(
                a.split("=", 1)[0] == "UV_NO_SYNC" and a.split("=", 1)[1] not in ("", "0", "false") for a in assigns)
            if not no_sync:
                return ["uv run without --no-sync or UV_NO_SYNC=1"]
    return []


def violations(line: str, allow_sfw: bool = False) -> list[str]:
    """Return the names of rules that `line` breaks.

    With allow_sfw (the repo's Makefile and scripts), a command is exempt only if it is
    run through the pinned Socket Firewall: `$(SFW)` / `"$(SFW)"` / `.toolchain/bin/sfw`.
    One sfw on a line does not exempt other commands on it.
    """
    found: list[str] = []
    if allow_sfw:
        line = re.sub(r"\"?\$[({]SFW[)}]\"?|\"?\$SFW\b\"?", PINNED_SFW, line)
    # Commands passed as strings to a shell (`bash -c "npx x"`) are judged too.
    texts = [line] + [m.group(2) for m in re.finditer(r"(['\"])(.*?)\1", line)]
    has_fetch = False
    for text in texts:
        for seg in segments(text):
            assigns, toks = strip_prefix(tokens(seg))
            if not toks:
                continue
            first = toks[0]
            if allow_sfw and (first == PINNED_SFW or first.endswith(".toolchain/bin/sfw")):
                continue
            if os.path.basename(first) == "sfw" or first == PINNED_SFW:
                # Any other sfw doesn't count: judge the command it wraps.
                toks = toks[1:]
                if not toks:
                    continue
                first = toks[0]
            tool = os.path.basename(first)
            if tool in FETCHERS:
                has_fetch = True
            found += judge(toks, assigns)
    if re.search(r"[<$]\(\s*(curl|wget)\b", line):
        found.append("fetch and run")
    # Fetch-and-run: a download on the same line as a shell/interpreter running a script
    # (piped, or saved to a file and executed). `python3 -c …` reading data is fine.
    if has_fetch:
        for seg in segments(line):
            _, toks = strip_prefix(tokens(seg))
            if not toks:
                continue
            tool = os.path.basename(toks[0])
            args = toks[1:]
            runs_script = INTERPRETERS.match(tool) and (not args or args[0] not in ("-c", "-m", "-e", "-V", "--version"))
            if runs_script or toks[0].startswith("./") or (tool == "chmod" and any("x" in a for a in args)):
                found.append("fetch and run")
    return sorted(set(found))
