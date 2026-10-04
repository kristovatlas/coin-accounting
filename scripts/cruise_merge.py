#!/usr/bin/env python3
"""Cruise mode's merge gate (ADR 0030). Standard library only.

Merges one PR only when every mechanical condition below holds. Otherwise it prints each
reason and exits 1, and the PR goes to the human as in standard mode. Always run `main`'s
copy (`git show origin/main:scripts/cruise_merge.py`), never a PR's, as with the tripwire.

    cruise_merge.py N SHA [--dry-run]

The caller updates `origin/main` and the PR head first, in a separate step; the gate itself
never downloads anything.

Conditions, all required:
  - the local `origin/main` is exactly `main` on GitHub (so the copies below aren't stale)
  - `PROCESS_MODE` on that `main` is `cruise`, and the local stop file `.git/cruise-stop` is absent
  - the gate is byte-for-byte `main`'s copy of this script
  - `main`'s branch protection requires a pull request (with no required approvals), every
    REQUIRED_CHECKS name and branches up to date (`strict`), applies to administrators too (so the
    owner's token can't bypass it), and forbids deleting `main`
  - the PR is open, not a draft, on a `cruise/` branch of this repository, based on `main`, authored by the owner,
    every commit authored or committed by the owner, mergeable, and its head is exactly SHA
  - SHA contains the current `main`, so CI tested the result of the merge
  - on SHA, every REQUIRED_CHECKS check-run from github-actions succeeded, the check-run list is
    complete, and every other check-run completed without failing
  - no changed file is binary, has a suffix outside TEXT_SUFFIXES, or is blocked (`blocked_path`: the
    tax/doxx/chain areas, the cruise-mode files, and tool configuration such as pytest.toml or ruff.toml)
  - no added line in a file that can run code (CODE_SUFFIXES, HTML included) uses a dynamic-code
    name (DYNAMIC_CODE_ANY, and DYNAMIC_CODE_JS outside Python): a best-effort tripwire, not a complete
    control (R-11) (the tripwire's "dynamic code" label also fires on
    harmless `re.compile`, so the gate checks the dangerous calls itself)
  - `main`'s tripwire on merge-base(origin/main, SHA)..SHA raised no blocking flag (see `blocking`)
  Paths come from git with a fixed configuration and NUL separators, so quoting can't hide them.
  - the cruise merge token file is a regular file owned by this user, with mode 600

Just before merging, it re-reads GitHub's `main` and the stop file, and refuses if either changed.
The merge goes through the REST API with the cruise merge token: a merge commit, with `sha`
pinned so GitHub refuses if the head moved. The token is never printed.

Exit status: 0 merged (or would merge, with --dry-run); 1 refused; 2 could not check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import unicodedata
import sys
import tempfile
from pathlib import Path

# The CI jobs that must pass. `tests (…)` runs `make test` and `make lint`; until CI has those jobs
# (#44), the gate refuses every PR (ADR 0030).
REQUIRED_CHECKS = ("checks (ubuntu-latest)", "checks (macos-latest)", "tests (ubuntu-latest)", "tests (macos-latest)")
STOP_FILE = "cruise-stop"  # in the git common dir: `touch .git/cruise-stop` stops every merge
TOKEN_FILE_ENV = "CRUISE_MERGE_TOKEN_FILE"
DEFAULT_TOKEN_FILE = "~/.config/coin-accounting/cruise-merge-token"
# Paths the gate refuses on top of the tripwire's risky paths: anything under backend/ with a tax,
# doxx or chain component (the engines, their tests and golden files), and the files that control
# cruise mode itself. Compared in lower case: macOS checkouts are case-insensitive.
BLOCKED_COMPONENTS = ("tax", "doxx", "chain")
BLOCKED_FILES = ("process_mode", "docs/cruise-mode.md", "backend/coinacct/domain/secret.py",
                 "backend/tests/socket_guard.py", "backend/tests/stub_http.py")
# Security-critical modules that are single files today: a package of the same name would take over.
SHADOWABLE_MODULES = ("launcher", "config", "rpc")
# Tool configuration that, in any directory, can override or weaken the project's test, lint, type or
# build settings (pytest reads `pytest.toml` before `pyproject.toml`; ruff reads `ruff.toml` first).
BLOCKED_NAMES = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "ruff.toml",
                 ".ruff.toml", "mypy.ini", ".mypy.ini", ".coveragerc", "conftest.py", "pyproject.toml",
                 "package.json", "tsconfig.json")
BLOCKED_NAME_PREFIXES = ("eslint.config.", ".eslintrc", "vite.config.", "vitest.config.", "vitest.workspace.",
                         "playwright.config.", "postcss.config.", "babel.config.", ".babelrc", "tsconfig.")
# Every changed file must have one of these suffixes: no binaries, no unscanned file types.
TEXT_SUFFIXES = (".py", ".pyi", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".json", ".toml", ".yml", ".yaml",
                 ".css", ".html", ".htm", ".md", ".csv")
# Files that can run code: scanned for DYNAMIC_CODE line by line.
CODE_SUFFIXES = (".py", ".pyi", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".html", ".htm")
# Names that build, look up or run code at run time, and so could hide process, network or file access.
# A BEST-EFFORT TRIPWIRE, not a complete control (owner decision, 2026-10-04; R-11): it catches the
# common direct forms, and deliberate evasion is an accepted risk. Bare names match too, so a simple
# alias (`g = getattr`) is caught; dotted forms are excluded where the name is also a harmless method
# (`re.compile`, `regex.exec`). Lines are NFKC-normalised first, as Python does for identifiers.
DYNAMIC_CODE_ANY = re.compile(
    r"(?<![.\w])(eval|exec|getattr|setattr|delattr|globals)\b"
    r"|(?<![.\w])compile\s*\("
    r"|\b(__import__|__builtins__|builtins|importlib|runpy|pickle|marshal|ctypes|__globals__|__subclasses__"
    r"|attrgetter|methodcaller)\b"
    r"|\bsys\.modules\b"
)
# JavaScript, TypeScript and HTML only: in Python these forms are ordinary (`from x import (`, `self[k]`).
DYNAMIC_CODE_JS = re.compile(
    r"(?<![.\w])(require|import)\s*\("
    r"|\bReflect\.|\b(globalThis|window|self|this|top|parent|frames)\s*\[|\bnew\s+Function\b|(?<![.\w])Function\s*\("
    r"|\.constructor\s*\.\s*constructor\b|\b(setTimeout|setInterval)\s*\(\s*['\"`]"
)
PY_SUFFIXES = (".py", ".pyi")
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_") and k != "GIT_CONFIG_PARAMETERS"}
GIT_ENV.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C"})
GIT_OPTS = ("-c", "core.quotePath=false", "-c", "diff.external=", "-c", "core.attributesFile=" + os.devnull,
            "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false")
DIFF_OPTS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")


def blocked_path(path: str) -> bool:
    low = path.lower()
    parts = low.split("/")
    name = parts[-1]
    stem_words = re.split(r"[._-]", name.split(".", 1)[0]) if "." in name else re.split(r"[._-]", name)
    in_backend = parts[0] == "backend"
    return (
        low in BLOCKED_FILES
        or name in BLOCKED_NAMES
        or name.startswith(BLOCKED_NAME_PREFIXES)
        # tax, doxx and chain: as a directory, or as a word in the file name (`services/tax.py`, `test_tax_lots.py`)
        or (in_backend and any(c in parts[1:-1] or c in stem_words for c in BLOCKED_COMPONENTS))
        # the security-critical single-file modules, also as a package that would shadow them (`rpc/__init__.py`)
        or (len(parts) > 3 and parts[:2] == ["backend", "coinacct"] and parts[2] in SHADOWABLE_MODULES)
    )

# Every tripwire flag blocks except these content labels, which fire on every `re.compile` and
# `os.environ` and are left to the Opus tripwire check. Any new or renamed label blocks.
NON_BLOCKING_CONTENT = ("dynamic code or deserialisation", "environment-dependent behaviour")


def blocking(flags: list[dict]) -> list[str]:
    """The tripwire flags that stop an automatic merge, rendered without source text."""
    out = []
    for f in flags:
        kind, detail = f.get("kind", ""), f.get("detail", "")
        label = detail.split(" (line ", 1)[0]
        if not (kind == "content" and label in NON_BLOCKING_CONTENT):
            out.append(f"{f.get('file')}: {detail}")
    return out


def decide(facts: dict) -> list[str]:
    """Every reason not to merge; empty means merge. Pure, so it is tested directly."""
    reasons = []
    if not facts["main_fresh"]:
        reasons.append("the local origin/main isn't GitHub's main: update it first")
    if facts["mode"] != "cruise":
        reasons.append(f"PROCESS_MODE on main is {facts['mode']!r}, not 'cruise'")
    if facts["stop_file"]:
        reasons.append("the stop file .git/cruise-stop exists")
    if not facts["gate_is_mains"]:
        reasons.append("this gate isn't main's copy of scripts/cruise_merge.py")
    prot = facts["protection"] or {}
    checks = prot.get("required_status_checks") or {}
    missing = [c for c in REQUIRED_CHECKS if c not in (checks.get("contexts") or [])]
    if (
        missing
        or checks.get("strict") is not True
        or (prot.get("enforce_admins") or {}).get("enabled") is not True
        or not prot.get("required_pull_request_reviews")
        or (prot.get("required_pull_request_reviews") or {}).get("required_approving_review_count") != 0
        or (prot.get("required_pull_request_reviews") or {}).get("require_code_owner_reviews") is True
        or (prot.get("required_pull_request_reviews") or {}).get("require_last_push_approval") is True
        or (prot.get("allow_deletions") or {}).get("enabled") is not False
        or (prot.get("allow_force_pushes") or {}).get("enabled") is not False
    ):
        reasons.append(
            "main's protection must require a PR (with no approvals), every gate check and up-to-date branches, "
            "for admins too, and forbid deleting or force-pushing main (see docs/cruise-mode.md)"
        )
    pr, sha, owner = facts["pr"], facts["sha"], facts["owner"]
    if pr.get("state") != "open":
        reasons.append("the PR isn't open")
    if pr.get("draft") is not False:
        reasons.append("the PR is a draft (drafts are for the human)")
    if (pr.get("head") or {}).get("sha") != sha:
        reasons.append("the PR's head isn't the reviewed commit")
    if not str((pr.get("head") or {}).get("ref", "")).startswith("cruise/"):
        reasons.append("the PR isn't a cruise slice (its branch doesn't start with cruise/)")
    if (pr.get("base") or {}).get("ref") != "main":
        reasons.append("the PR isn't based on main")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    if head_repo is None or head_repo != ((pr.get("base") or {}).get("repo") or {}).get("full_name"):
        reasons.append("the PR comes from another repository")
    if (pr.get("user") or {}).get("login") != owner:
        reasons.append("the PR isn't the owner's")
    if pr.get("mergeable") is not True:
        reasons.append(f"GitHub doesn't report the PR mergeable (mergeable={pr.get('mergeable')})")
    if not facts["head_has_main"]:
        reasons.append("the PR head doesn't contain the current main: merge main into it, then let CI run")
    commits = facts["commits"]
    if not commits or len(commits) != pr.get("commits") or len(commits) > 250:
        reasons.append("the PR's commit list is incomplete or too long")
    for c in commits:
        logins = {(c.get("author") or {}).get("login"), (c.get("committer") or {}).get("login")}
        if owner not in logins:
            reasons.append(f"commit {str(c.get('sha', '?'))[:12]} isn't the owner's")
    runs = facts["check_runs"]
    if len(runs) != facts["check_runs_total"]:
        reasons.append("the check-run list is incomplete")
    for name in REQUIRED_CHECKS:
        ok = [
            r
            for r in runs
            if r.get("name") == name
            and (r.get("app") or {}).get("slug") == "github-actions"
            and r.get("conclusion") == "success"
        ]
        if not ok:
            reasons.append(f"{name} hasn't succeeded on the commit")
    for r in runs:
        if r.get("status") != "completed":
            reasons.append(f"check-run {r.get('name')} is still {r.get('status')}")
        elif r.get("conclusion") not in ("success", "neutral", "skipped"):
            reasons.append(f"check-run {r.get('name')} concluded {r.get('conclusion')}")
    reasons.extend(f"binary file changed: {p}" for p in facts["binary_files"])
    for p in facts["changed_files"]:
        if blocked_path(p):
            reasons.append(f"blocked path changed: {p}")
        elif not p.lower().endswith(TEXT_SUFFIXES):
            reasons.append(f"file type the gate doesn't scan: {p}")
    reasons.extend(f"dynamic code call added: {p} (line {n})" for p, n in facts["dynamic_code"])
    if facts["tripwire"] is None:
        reasons.append("the tripwire did not run")
    else:
        reasons.extend(f"tripwire: {b}" for b in blocking(facts["tripwire"]))
    if facts["token_problem"]:
        reasons.append(facts["token_problem"])
    return reasons


def run(*cmd: str, env: dict | None = None) -> str:
    if cmd[0] == "git":
        cmd, env = ("git", *GIT_OPTS, *cmd[1:]), GIT_ENV
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env=env).stdout


def gh_api(path: str) -> object:
    return json.loads(run("gh", "api", "-H", "Accept: application/vnd.github+json", path))


def token_file() -> Path:
    return Path(os.path.expanduser(os.environ.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_FILE))


def read_token(path: Path) -> tuple[str | None, str | None]:
    """(token, None), or (None, problem). Opened without following a link, and checked on the same fd."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, f"no readable cruise merge token at {path} (see docs/cruise-mode.md)"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return None, f"the cruise merge token file {path} must be the user's own regular file with mode 600"
        token = os.read(fd, 4096).decode("ascii", "replace").strip()
    finally:
        os.close(fd)
    return (token, None) if token else (None, f"the cruise merge token file {path} is empty")


def git_dir() -> Path:
    return Path(run("git", "rev-parse", "--path-format=absolute", "--git-common-dir").strip())


def git_z(*args: str) -> list[str]:
    """NUL-separated git output, with a fixed configuration, so paths are never quoted or rewritten."""
    out = subprocess.run(["git", *GIT_OPTS, *args], check=True, capture_output=True, env=GIT_ENV).stdout
    return [f.decode("utf-8", "surrogateescape") for f in out.split(b"\0") if f]


def added_dynamic_code(merge_base: str, sha: str, paths: list[str]) -> list[tuple[str, int]]:
    hits = []
    for path in paths:
        if not path.lower().endswith(CODE_SUFFIXES):
            continue
        is_py = path.lower().endswith(PY_SUFFIXES)
        patch = subprocess.run(
            ["git", *GIT_OPTS, "diff", *DIFF_OPTS, "--text", "-U0", merge_base, sha, "--", f":(literal){path}"],
            check=True, capture_output=True, env=GIT_ENV,
        ).stdout.decode("utf-8", "replace")
        in_hunk, line_no = False, 0
        for line in patch.split("\n"):
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:  # everything before the first hunk header is metadata, including `+++ b/path`
                in_hunk, line_no = True, int(m.group(1))
                continue
            if in_hunk and line.startswith("+"):
                text = unicodedata.normalize("NFKC", line[1:])
                if DYNAMIC_CODE_ANY.search(text) or (not is_py and DYNAMIC_CODE_JS.search(text)):
                    hits.append((path, line_no))
                line_no += 1
    return hits


def gather(n: int, sha: str) -> dict:
    repo = json.loads(run("gh", "repo", "view", "--json", "nameWithOwner,owner"))
    name, owner = repo["nameWithOwner"], repo["owner"]["login"]
    github_main = gh_api(f"repos/{name}/commits/main")["sha"]
    local_main = run("git", "rev-parse", "refs/remotes/origin/main").strip()
    try:
        mode = run("git", "show", "refs/remotes/origin/main:PROCESS_MODE").strip()
    except subprocess.CalledProcessError:
        mode = "(missing)"
    try:
        protection = gh_api(f"repos/{name}/branches/main/protection")  # needs the owner's admin read
    except subprocess.CalledProcessError:
        protection = None  # 404 (unprotected) or no access: refused
    own = Path(__file__).read_bytes()
    mains = subprocess.run(["git", *GIT_OPTS, "show", "refs/remotes/origin/main:scripts/cruise_merge.py"],
                           capture_output=True, check=True, env=GIT_ENV).stdout
    pr = gh_api(f"repos/{name}/pulls/{n}")
    commits: list = []
    for page in (1, 2, 3):
        batch = gh_api(f"repos/{name}/pulls/{n}/commits?per_page=100&page={page}")
        commits += batch
        if len(batch) < 100:
            break
    runs: list = []
    total = None
    for page in range(1, 11):
        data = gh_api(f"repos/{name}/commits/{sha}/check-runs?per_page=100&page={page}")
        total = data.get("total_count")
        runs += data["check_runs"]
        if len(runs) >= (total or 0) or not data["check_runs"]:
            break
    head_has_main = subprocess.run(["git", *GIT_OPTS, "merge-base", "--is-ancestor", github_main, sha],
                                   env=GIT_ENV).returncode == 0
    merge_base = run("git", "merge-base", "refs/remotes/origin/main", sha).strip()
    numstat = git_z("diff", *DIFF_OPTS, "--numstat", "-z", merge_base, sha)
    binary = [f.split("\t", 2)[2] for f in numstat if f.startswith("-\t-\t")]
    changed = git_z("diff", *DIFF_OPTS, "--name-only", "-z", merge_base, sha)
    dynamic = added_dynamic_code(merge_base, sha, changed)
    tripwire = None
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "tripwire.py"
        script.write_text(run("git", "show", "refs/remotes/origin/main:scripts/tripwire.py"))
        try:
            tripwire = json.loads(run(sys.executable, str(script), "refs/remotes/origin/main", sha, "--json"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            tripwire = None
    _, token_problem = read_token(token_file())
    return {
        "name": name,
        "github_main": github_main,
        "main_fresh": local_main == github_main,
        "mode": mode,
        "stop_file": (git_dir() / STOP_FILE).exists(),
        "gate_is_mains": own == mains,
        "protection": protection,
        "pr": pr,
        "sha": sha,
        "owner": owner,
        "head_has_main": head_has_main,
        "commits": commits,
        "check_runs": runs,
        "check_runs_total": total,
        "binary_files": binary,
        "changed_files": changed,
        "dynamic_code": dynamic,
        "tripwire": tripwire,
        "token_problem": token_problem,
    }


def recheck(facts: dict) -> list[str]:
    """The conditions that can change in the seconds between `gather` and the merge."""
    reasons = []
    if gh_api(f"repos/{facts['name']}/commits/main")["sha"] != facts["github_main"]:
        reasons.append("main moved while the gate was checking")
    if (git_dir() / STOP_FILE).exists():
        reasons.append("the stop file .git/cruise-stop appeared")
    return reasons


def merge(name: str, n: int, sha: str) -> None:
    token, problem = read_token(token_file())
    if token is None:
        raise RuntimeError(problem)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""), "GH_TOKEN": token}
    run(
        "gh", "api", "--hostname", "github.com", "-X", "PUT", f"repos/{name}/pulls/{n}/merge",
        "-f", f"sha={sha}", "-f", "merge_method=merge",
        "-f", f"commit_title=Merge pull request #{n} (cruise mode, ADR 0030)",
        env=env,
    )  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pr", type=int)
    parser.add_argument("sha")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if len(args.sha) != 40 or any(c not in "0123456789abcdef" for c in args.sha):
        print("cruise_merge: SHA must be a full 40-character commit id", file=sys.stderr)
        return 2
    try:
        facts = gather(args.pr, args.sha)
        reasons = decide(facts)
        if not reasons and not args.dry_run:
            reasons = recheck(facts)
    except Exception as e:  # any failure to check is exit 2, never a refusal (exit 1) or a merge
        print(f"cruise_merge: could not check PR #{args.pr} ({type(e).__name__})", file=sys.stderr)
        return 2
    if reasons:
        print(f"cruise_merge: PR #{args.pr} goes to the human:")
        for r in reasons:
            print(f"- {r}")
        return 1
    if args.dry_run:
        print(f"cruise_merge: PR #{args.pr} at {args.sha[:12]} would be merged")
        return 0
    try:
        merge(facts["name"], args.pr, args.sha)
    except Exception:
        print(f"cruise_merge: GitHub refused to merge PR #{args.pr}", file=sys.stderr)
        return 2
    print(f"cruise_merge: merged PR #{args.pr} at {args.sha[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
