"""Banned install and fetch-and-run commands (ENGINEERING §2.3, AGENTS.md).

Shared by check_install_commands.py (CI) and agent_guard.py (Claude Code hook).

Scope (ADR 0022): this catches **accidental or habitual** install commands, the
ones an agent types out of habit. It is not a boundary against deliberate evasion
(an agent writing commands into a script, `eval`, exported variables, …); that is
accepted risk R-8, covered by human review and the supply-chain controls.

Approach: tokenize each command, strip wrappers (with their operands), shell
keywords and leading VAR=value assignments, then judge the command. Package
managers get **allowlists** of safe subcommands, and unknown options before the
subcommand block, so a mis-parse fails closed.
"""

from __future__ import annotations

import os
import re
import shlex

# Tools that are banned whatever their arguments.
BANNED_TOOLS = {
    "npx": "npx", "pnpx": "npx", "bunx": "bunx", "uvx": "uvx", "yarn": "yarn", "bun": "bun",
    "corepack": "corepack", "pipx": "pipx", "pre-commit": "pre-commit", "easy_install": "easy_install",
    "poetry": "poetry", "pdm": "pdm", "pipenv": "pipenv", "hatch": "hatch", "rye": "rye", "pixi": "pixi",
    "conda": "conda", "mamba": "mamba", "micromamba": "micromamba", "deno": "deno",
}
# Other package managers: these subcommands install.
BANNED_SUBCOMMANDS = {
    "gem": {"install", "update"}, "cargo": {"install", "add", "binstall"}, "go": {"install", "get"},
    "brew": {"install", "reinstall", "upgrade", "bundle", "tap"}, "port": {"install", "upgrade"},
    "apt": {"install", "upgrade"}, "apt-get": {"install", "upgrade"}, "dnf": {"install", "upgrade"},
    "yum": {"install", "upgrade"},
}
# Safe subcommands per repository package manager; anything else is a violation.
ALLOWED_SUBCOMMANDS = {
    "pnpm": {"run", "test", "t", "list", "ls", "why", "audit", "outdated", "licenses", "help", "config"},
    "npm": {"view", "help", "ls", "list", "audit", "explain"},
    "uv": {"run", "version", "help", "tree"},
    "pip": {"list", "show", "freeze", "check", "help"},
}
# Options allowed before the subcommand. Options that take a separate value are listed
# with True. Any other option before the subcommand blocks (fails closed).
GLOBAL_OPTIONS = {
    "pnpm": {"-F": True, "--filter": True, "-C": True, "--dir": True, "-r": False, "--recursive": False,
             "-w": False, "--workspace-root": False, "-s": False, "--silent": False, "--reporter": True,
             "-v": False, "--version": False},
    "npm": {"--json": False, "-s": False, "--silent": False, "-v": False, "--version": False},
    "uv": {"-q": False, "--quiet": False, "-v": False, "--verbose": False, "--offline": False, "--frozen": False,
           "--locked": False, "--no-sync": False, "--no-progress": False, "--no-cache": False,
           "--directory": True, "--project": True, "-V": False, "--version": False},
    "pip": {"-q": False, "--quiet": False, "-v": False, "--verbose": False, "--isolated": False,
            "--no-cache-dir": False, "--disable-pip-version-check": False, "--no-input": False,
            "-V": False, "--version": False},
}
# Wrapper commands and which of their options take a value; `timeout` also has a
# required positional (the duration).
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"},
    "doas": {"-u", "-C"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
    "ionice": {"-c", "-n", "-p"},
    "xargs": {"-n", "-I", "-L", "-P", "-d", "-s", "-E", "-a"},
    "command": set(), "exec": set(), "nohup": set(), "time": set(), "builtin": set(),
}
POSITIONAL_WRAPPER_ARGS = {"timeout": 1}
SHELL_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "time"}
INTERPRETERS = re.compile(r"^((ba|z|da|k|c)?sh|python[0-9.]*t?|node|perl|ruby|php)$")
PYTHON = re.compile(r"^python[0-9.]*t?$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FETCHERS = {"curl", "wget", "aria2c", "http", "https", "fetch"}
# Python modules that install packages when run with `python -m`.
PYTHON_INSTALLER_MODULES = {"pip", "pip3", "ensurepip", "uv", "poetry", "pdm", "pipx", "pipenv", "hatch", "conda"}

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
    """Remove leading VAR=value assignments, shell keywords and wrappers with their operands.

    Returns (assignments, remaining tokens)."""
    assigns: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        name = os.path.basename(t)
        if ASSIGNMENT.match(t):
            assigns.append(t)
            i += 1
        elif t in SHELL_KEYWORDS:
            i += 1
        elif name in WRAPPERS:
            value_opts = WRAPPERS[name]
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or ASSIGNMENT.match(toks[i])):
                opt = toks[i]
                if ASSIGNMENT.match(opt):
                    assigns.append(opt)
                elif opt in value_opts:
                    i += 1  # skip the option's value
                i += 1
            i += POSITIONAL_WRAPPER_ARGS.get(name, 0)
        else:
            break
    return assigns, toks[i:]


def subcommand(tool: str, args: list[str]) -> tuple[str | None, int]:
    """(first non-option argument, its index). An unknown option before it is returned as the
    'subcommand', so it isn't allowlisted and blocks (fails closed)."""
    allowed = GLOBAL_OPTIONS.get(tool, {})
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return (args[i + 1], i + 1) if i + 1 < len(args) else (None, i)
        if a.startswith("-"):
            key = a.split("=", 1)[0]
            if key not in allowed:
                return (f"option {a}", i)
            if allowed[key] and "=" not in a:
                i += 1
            i += 1
            continue
        return (a, i)
    return (None, i)


def python_module(args: list[str]) -> tuple[str | None, int]:
    """(module run with -m, index of the first argument after it), looking past interpreter options."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-c" or not a.startswith("-") or a == "-":
            return None, i  # inline code or a script: not module mode
        if a.startswith("--"):
            i += 1
            continue
        cluster = a[1:]
        if "m" in cluster:
            rest = cluster[cluster.index("m") + 1:]
            if rest:
                return rest, i + 1
            return (args[i + 1], i + 2) if i + 1 < len(args) else (None, i + 1)
        if cluster[-1:] in ("W", "X"):  # options that take a value
            i += 1
        i += 1
    return None, i


def judge(toks: list[str], assigns: list[str], depth: int = 0) -> list[str]:
    """Violations for one command (after wrappers are stripped)."""
    if not toks or depth > 3:
        return []
    tool = os.path.basename(toks[0])
    args = toks[1:]
    if tool in BANNED_TOOLS:
        return [BANNED_TOOLS[tool]]
    if tool in BANNED_SUBCOMMANDS:
        sub = next((a for a in args if not a.startswith("-")), None)
        return [f"{tool} {sub}"] if sub in BANNED_SUBCOMMANDS[tool] else []
    if re.fullmatch(r"pip[0-9.]*", tool):
        tool = "pip"
    if PYTHON.match(tool):
        mod, after = python_module(args)
        if mod is None:
            return []
        if mod in ("pip", "pip3"):
            tool, args = "pip", args[after:]
        elif mod == "uv":
            tool, args = "uv", args[after:]
        elif mod in PYTHON_INSTALLER_MODULES:
            return [f"python -m {mod}"]
        else:
            return []
    if tool not in ALLOWED_SUBCOMMANDS:
        return []
    sub, idx = subcommand(tool, args)
    if sub is None:
        return []  # e.g. `pnpm --version` (allowed global options only)
    rest = args[idx + 1:]
    if tool == "uv" and sub == "pip":
        inner, _ = subcommand("pip", rest)
        return [] if inner in ALLOWED_SUBCOMMANDS["pip"] else [f"uv pip {inner}"]
    if sub not in ALLOWED_SUBCOMMANDS[tool]:
        return [f"{tool} {sub}"]
    # Allowlisted subcommands whose particular forms install or reconfigure (round 3).
    if tool == "npm" and sub == "audit" and "fix" in rest:
        return ["npm audit fix"]
    if tool == "pnpm" and sub == "audit" and any(a.split("=")[0] == "--fix" for a in rest):
        return ["pnpm audit --fix"]
    if tool == "pnpm" and sub == "config" and (not rest or rest[0] not in ("get", "list")):
        return ["pnpm config (only get/list)"]
    if tool == "uv" and sub in ("tree", "version"):
        if not any(a in ("--frozen", "--locked") for a in args):
            return [f"uv {sub} without --frozen/--locked"]
        if sub == "version" and (any(not a.startswith("-") for a in rest) or any(a.startswith("--bump") for a in rest)):
            return ["uv version (changes the project)"]
    if tool == "uv" and sub == "run":
        return judge_uv_run(args[:idx], rest, assigns, depth)
    return []


UV_RUN_VALUE_OPTIONS = {"-p", "--python", "--directory", "--project", "--package", "--env-file", "--extra", "--group",
                        "--index", "--default-index"}


def judge_uv_run(before: list[str], run_args: list[str], assigns: list[str], depth: int) -> list[str]:
    no_sync = "--no-sync" in before or any(
        a.split("=", 1)[0] == "UV_NO_SYNC" and a.split("=", 1)[1] not in ("", "0", "false") for a in assigns)
    i = 0
    while i < len(run_args):
        a = run_args[i]
        key = a.split("=", 1)[0]
        if key.startswith("--with") or key == "-w":
            return ["uv run --with"]
        if key in ("-m", "--module"):
            mod = a.partition("=")[2] if "=" in a else (run_args[i + 1] if i + 1 < len(run_args) else "")
            if mod in PYTHON_INSTALLER_MODULES:
                return [f"uv run -m {mod}"]
            return [] if no_sync else ["uv run without --no-sync or UV_NO_SYNC=1"]
        if not a.startswith("-"):
            break
        if key == "--no-sync":
            no_sync = True
        if key in UV_RUN_VALUE_OPTIONS and "=" not in a:
            i += 1
        i += 1
    inner = run_args[i:]
    if inner and re.match(r"^https?://", inner[0]):
        return ["uv run <URL>"]
    found = [] if no_sync else ["uv run without --no-sync or UV_NO_SYNC=1"]
    # The command uv runs is judged too (`uv run pip install x`).
    inner_assigns, inner_toks = strip_prefix(inner)
    return found + judge(inner_toks, inner_assigns, depth + 1)


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
            if os.path.basename(first) in FETCHERS:
                has_fetch = True
            found += judge(toks, assigns)
    if re.search(r"[<$]\(\s*(curl|wget)\b", line):
        found.append("fetch and run")
    # Fetch-and-run: a download on the same line as a shell/interpreter running a script
    # (piped, saved to a file and executed, or sourced). `python3 -c …` reading data is fine.
    if has_fetch:
        for seg in segments(line):
            _, toks = strip_prefix(tokens(seg))
            if not toks:
                continue
            tool = os.path.basename(toks[0])
            args = toks[1:]
            runs_script = INTERPRETERS.match(tool) and (not args or args[0] not in ("-c", "-m", "-e", "-V", "--version"))
            if (runs_script or tool in ("source", ".") or toks[0].startswith("./")
                    or (tool == "chmod" and any("x" in a for a in args))):
                found.append("fetch and run")
    return sorted(set(found))
