#!/usr/bin/env python3
"""Cruise mode's merge gate (ADR 0030, as narrowed by ADR 0031, autopilot). Standard library only.

Merges one PR only when every mechanical condition below holds. Otherwise it prints each
reason and exits 1, and the PR goes to the human as in standard mode. Always run `main`'s
copy (`git show origin/main:scripts/cruise_merge.py`), never a PR's, as with the tripwire.

    cruise_merge.py N SHA [--dry-run]
    cruise_merge.py --list-blocked MERGE_BASE SHA   (the changed paths the gate would refuse)

The caller updates `origin/main` and the PR head first, in a separate step; the gate itself
never downloads anything.

Conditions, all required:
  - the local `origin/main` is exactly `main` on GitHub (so the copies below aren't stale)
  - `PROCESS_MODE` on that `main` is `cruise`, and the local stop file `.git/cruise-stop` is absent
  - the gate is byte-for-byte `main`'s copy of this script
  - `main`'s branch protection requires a pull request (with no required approvals), every
    REQUIRED_CHECKS name and branches up to date (`strict`), applies to administrators too (so the
    owner's token can't bypass it), and forbids deleting and force-pushing `main`
  - the PR is open, not a draft, on a branch of this repository, based on `main`, authored by the owner,
    every commit authored or committed by the owner, mergeable, and its head is exactly SHA
  - SHA contains the current `main`, so CI tested the result of the merge
  - on SHA, every REQUIRED_CHECKS check-run from github-actions succeeded, the check-run list is
    complete, and every other check-run completed without failing
  - the PR doesn't carry the label `autopilot-blocked` (the panel's permanent block, ADR 0031 §5)
  - no file under the floored engines (`tax/`, `doxx/`, `chain/`) is deleted or renamed
  - the review panel cleared SHA: its commit status `review-panel`, set by the owner, is `success` (set only after a
    clean round and an Opus tripwire with no flag of Medium or above; advisory, since the owner's
    token can set it, R-9 and R-12, but it stops a merge no panel reviewed)
  - no changed file is binary, has a suffix outside TEXT_SUFFIXES, or is blocked (`blocked_path`):
    under autopilot (ADR 0031) only what needs a human decision or controls the agents themselves:
    dependency, lock and install-config files, ADRs and the architecture baseline, CI, `scripts/`,
    the Makefile, agent instructions, skills and tool configuration at any depth, the test socket
    guard and the files that switch it on, and the mode files. Application code, tests and the
    other binding documents merge automatically.
  - `main`'s tripwire ran on merge-base(origin/main, SHA)..SHA and raised no blocking flag (the
    tripwire's path, content, removed-line and deleted-file flags are judged by the review panel's
    Opus tripwire, whose Medium-or-above flags go to the human; every other kind, including kinds
    added later, blocks)
  Paths come from git with a fixed configuration and NUL separators, so quoting can't hide them.
  - the cruise merge token file is a regular file owned by this user, with mode 600

Just before merging, it re-reads GitHub's `main` and the stop file, and refuses if either changed.
The merge goes through the REST API with the cruise merge token: a merge commit, with `sha`
pinned so GitHub refuses if the head moved. The token is never printed.

Exit status: 0 merged (or would merge, with --dry-run); 1 refused; 2 could not check.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# The CI jobs that must pass. `tests (…)` runs `make test` and `make lint`.
REQUIRED_CHECKS = ("checks (ubuntu-latest)", "checks (macos-latest)", "tests (ubuntu-latest)", "tests (macos-latest)")
STOP_FILE = "cruise-stop"  # in the git common dir: `touch .git/cruise-stop` stops every merge
TOKEN_FILE_ENV = "CRUISE_MERGE_TOKEN_FILE"
DEFAULT_TOKEN_FILE = "~/.config/coin-accounting/cruise-merge-token"
# Autopilot (ADR 0031): the gate refuses only what needs a human decision, and what controls the
# agents themselves, so an agent can never loosen its own checks. Compared in lower case: macOS
# checkouts are case-insensitive.
BLOCKED_PREFIXES = (".github/", "scripts/", "docs/adr/")
BLOCKED_FILES = ("process_mode", "docs/cruise-mode.md", "makefile", "docs/architecture.md",
                 # the test socket guard (ENGINEERING §3.2), and the package that would shadow it
                 "backend/tests/socket_guard.py", "backend/tests/__init__.py")
# Anywhere. Dependency manifests, lockfiles and install configuration: new dependencies, and what
# installs them, are the owner's decision (ENGINEERING §2.4; the Makefile's DEP_FILES and
# INSTALL_CONFIG, kept in sync by a test). Plus the files that configure CI's checks and could take
# precedence over pyproject.toml for a subtree: the pytest files that switch the socket guard on or
# could skip it, and the lint, type-check and coverage settings (a ruff.toml beats pyproject.toml).
BLOCKED_NAMES = ("pyproject.toml", "uv.lock", "uv.toml", "package.json", "pnpm-lock.yaml", "pnpm-lock.times.json",
                 "pnpm-workspace.yaml", ".npmrc", ".python-version", ".node-version",
                 "conftest.py", "pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg",
                 "ruff.toml", ".ruff.toml", "mypy.ini", ".mypy.ini", ".coveragerc",
                 ".mcp.json", ".cursorrules", ".windsurfrules", ".clinerules", ".devcontainer.json",
                 "opencode.json", "opencode.jsonc",
                 # Socket's repository config: it can ignore paths or turn off the PR alerts the owner
                 # approves dependencies by (ENGINEERING §2.4)
                 "socket.yml", "socket.yaml", ".socket.yml", ".socket.yaml",
                 # the mutation-testing exclusion list (ENGINEERING §3.5), like a coverage setting
                 "mutation-exclusions.md")
BLOCKED_NAME_PATTERNS = ("requirements*.txt", "constraints*.txt", ".pnpmfile.*",
                         # frontend lint and coverage settings
                         "eslint.config.*", ".eslintrc*", "vitest.config.*", "vitest.workspace.*",
                         # Vitest reads its test and coverage settings from vite.config.* when there is
                         # no vitest.config.*
                         "vite.config.*",
                         # copied-in third-party code skips the dependency decision (ENGINEERING §2.4)
                         "*.min.*",
                         # agent instructions and skills, at any depth (AGENTS.override.md, backend/CLAUDE.md)
                         "*agents*.md", "*claude*.md", "*gemini*.md", "skill.md", ".aider*")
# Agent and editor tool configuration directories, at any depth (.vscode can hold MCP servers and tasks).
BLOCKED_DIRS = (".claude", ".codex", ".agents", ".cursor", ".gemini", ".vscode", ".idea", ".windsurf",
                ".clinerules", ".continue", ".roo", ".kiro", ".amazonq", ".devcontainer", ".zed", ".opencode",
                ".junie", ".kilocode", ".trae", ".fleet",
                # vendored third-party code (ENGINEERING §2.4)
                "vendor", "vendored", "third_party", "third-party", "node_modules", "bower_components",
                "site-packages", "dist-packages")
# Deleting or renaming a file in these engines would quietly drop the 95 % coverage floor and the
# mutation runs, which the Makefile keys on these directory names (ENGINEERING §3.3).
FLOORED_DIRS = ("backend/coinacct/tax/", "backend/coinacct/doxx/", "backend/coinacct/chain/")
# The PR label the review panel adds when a round finds a Critical issue, a committed secret or real
# data. It is never removed by an agent, and the gate refuses a PR that carries it (ADR 0031 §5).
BLOCK_LABEL = "autopilot-blocked"
# Every changed file must have one of these suffixes: no binaries, no unscanned file types.
TEXT_SUFFIXES = (".py", ".pyi", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".json", ".toml", ".yml", ".yaml",
                 ".css", ".html", ".htm", ".md", ".csv", ".svg", ".txt")
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_") and k != "GIT_CONFIG_PARAMETERS"}
GIT_ENV.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C"})
GIT_OPTS = ("-c", "core.quotePath=false", "-c", "diff.external=", "-c", "core.attributesFile=" + os.devnull,
            "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false")
DIFF_OPTS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")


# Only these may change at the top of the repository: anything else there (a root `coverage.py`, a
# `pytest/` package) would be imported in place of a real tool by the `python -m` commands CI runs from
# the root, and could turn the tests off. The blocked top-level entries stay with the owner anyway.
ALLOWED_TOP = ("backend", "frontend", "e2e", "docs", "plan.md", "readme.md")
# Python packages that may sit directly under backend/ and e2e/ (both are on the tests' sys.path):
# any other module there could shadow pytest, its plugins or the standard library.
ALLOWED_PACKAGES = {"backend": ("coinacct", "tests"), "e2e": ("harness",)}


def shadows_a_tool(parts: list[str]) -> bool:
    if parts[0] not in ALLOWED_TOP:
        return True
    allowed = ALLOWED_PACKAGES.get(parts[0])
    if allowed is None or len(parts) < 2:
        return False
    if len(parts) == 2:  # a file directly under backend/ or e2e/
        return parts[1].endswith((".py", ".pyi", ".pth"))
    return parts[1] not in allowed and parts[-1].endswith((".py", ".pyi"))


def blocked_path(path: str) -> bool:
    low = path.lower()
    parts = low.split("/")
    name = parts[-1]
    return (
        shadows_a_tool(parts)
        or low in BLOCKED_FILES
        or low.startswith(BLOCKED_PREFIXES)
        or name in BLOCKED_NAMES
        or any(fnmatch.fnmatchcase(name, pat) for pat in BLOCKED_NAME_PATTERNS)
        or any(part in BLOCKED_DIRS for part in parts[:-1])
        # a package next to the socket guard would shadow it (`socket_guard/__init__.py`)
        or low.startswith("backend/tests/socket_guard/")
    )

# Autopilot (ADR 0031): these tripwire kinds don't block; the review panel's Opus tripwire judges the
# change, and its Medium-or-above flags go to the human. Every other kind blocks: symlinks and
# submodules (CI rejects them too), executable bits, changes the tripwire can't parse, and any kind
# added to the tripwire later, until it is classified here on purpose.
REPORTED_KINDS = ("path", "content", "removed", "deleted")
REVIEW_STATUS = "review-panel"  # the commit status the review panel sets when it clears a commit


def blocking(flags: list[dict]) -> list[str]:
    """The tripwire flags that stop an automatic merge, rendered without source text."""
    return [f"{f.get('file')}: {f.get('detail', '')}" for f in flags if f.get("kind", "") not in REPORTED_KINDS]


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
    labels = {(lb or {}).get("name") for lb in (pr.get("labels") or [])}
    if BLOCK_LABEL in labels:
        reasons.append(f"the review panel blocked this PR for good (label {BLOCK_LABEL})")
    if facts["review_status"] != "success":
        reasons.append(f"the review panel hasn't cleared this commit (status {REVIEW_STATUS}: {facts['review_status']})")
    reasons.extend(f"binary file changed: {p}" for p in facts["binary_files"])
    reasons.extend(
        f"file deleted or renamed in a floored engine: {p}"
        for p in facts["deleted_files"]
        if p.lower().startswith(FLOORED_DIRS)
    )
    for p in facts["changed_files"]:
        if blocked_path(p):
            reasons.append(f"blocked path changed: {p}")
        elif not p.lower().endswith(TEXT_SUFFIXES):
            reasons.append(f"file type the gate doesn't scan: {p}")
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
    statuses = gh_api(f"repos/{name}/commits/{sha}/statuses?per_page=100")  # newest first
    review_status = owner_review_status(statuses, owner)
    deleted = git_z("diff", *DIFF_OPTS, "--name-only", "--diff-filter=D", "-z", merge_base, sha)
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
        "deleted_files": deleted,
        "changed_files": changed,
        "review_status": review_status,
        "tripwire": tripwire,
        "token_problem": token_problem,
    }


def recheck(facts: dict) -> list[str]:
    """The conditions that can change in the seconds between `gather` and the merge: `main`, the stop
    file, and the panel's two signals (the block label and the owner's status), which branch protection
    doesn't enforce."""
    reasons = []
    name = facts["name"]
    if gh_api(f"repos/{name}/commits/main")["sha"] != facts["github_main"]:
        reasons.append("main moved while the gate was checking")
    if (git_dir() / STOP_FILE).exists():
        reasons.append("the stop file .git/cruise-stop appeared")
    pr = gh_api(f"repos/{name}/pulls/{facts['pr']['number']}")
    if BLOCK_LABEL in {(lb or {}).get("name") for lb in (pr.get("labels") or [])}:
        reasons.append(f"the PR was labelled {BLOCK_LABEL} while the gate was checking")
    statuses = gh_api(f"repos/{name}/commits/{facts['sha']}/statuses?per_page=100")
    if owner_review_status(statuses, facts["owner"]) != "success":
        reasons.append("the review panel's status changed while the gate was checking")
    return reasons


def merge(name: str, n: int, sha: str) -> None:
    token, problem = read_token(token_file())
    if token is None:
        raise RuntimeError(problem)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""), "GH_TOKEN": token}
    run(
        "gh", "api", "--hostname", "github.com", "-X", "PUT", f"repos/{name}/pulls/{n}/merge",
        "-f", f"sha={sha}", "-f", "merge_method=merge",
        "-f", f"commit_title=Merge pull request #{n} (autopilot, ADR 0031)",
        env=env,
    )  # fmt: skip


def owner_review_status(statuses: list, owner: str) -> str | None:
    """The newest `review-panel` status the owner set; one set by anyone else (an app, a workflow) is ignored."""
    for st in statuses:
        if st.get("context") == REVIEW_STATUS and (st.get("creator") or {}).get("login") == owner:
            return st.get("state")
    return None


def refused_paths(changed: list[str], binary: list[str], deleted: list[str]) -> list[str]:
    """Every changed path the gate refuses on its own: blocked, binary, an unscanned type, or a deletion
    in a floored engine."""
    out = [p for p in changed if blocked_path(p) or not p.lower().endswith(TEXT_SUFFIXES)]
    out += [p for p in binary if p not in out]
    out += [p for p in deleted if p.lower().startswith(FLOORED_DIRS) and p not in out]
    return out


def list_blocked(merge_base: str, sha: str) -> list[str]:
    """The changed paths the gate would refuse, so the review panel picks the profile from the gate's own
    rules rather than by eye (ADR 0031 §2)."""
    changed = git_z("diff", *DIFF_OPTS, "--name-only", "-z", merge_base, sha)
    numstat = git_z("diff", *DIFF_OPTS, "--numstat", "-z", merge_base, sha)
    binary = [f.split("\t", 2)[2] for f in numstat if f.startswith("-\t-\t")]
    deleted = git_z("diff", *DIFF_OPTS, "--name-only", "--diff-filter=D", "-z", merge_base, sha)
    return refused_paths(changed, binary, deleted)


def is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in[:1] == ["--list-blocked"]:
        if len(args_in) != 3 or not (is_sha(args_in[1]) and is_sha(args_in[2])):
            print("usage: cruise_merge.py --list-blocked MERGE_BASE SHA (full commit ids)", file=sys.stderr)
            return 2
        try:
            for p in list_blocked(args_in[1], args_in[2]):
                print(p)
        except Exception as e:
            print(f"cruise_merge: could not list ({type(e).__name__})", file=sys.stderr)
            return 2
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pr", type=int)
    parser.add_argument("sha")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(args_in)
    if not is_sha(args.sha):
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
