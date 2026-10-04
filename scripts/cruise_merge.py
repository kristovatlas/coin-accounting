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
  - `main` is a protected branch whose required status checks include every REQUIRED_CHECKS name
  - the PR is open, not a draft, from this repository, based on `main`, authored by the owner,
    every commit authored or committed by the owner, mergeable, and its head is exactly SHA
  - SHA contains the current `main`, so CI tested the result of the merge
  - on SHA, every REQUIRED_CHECKS check-run from github-actions succeeded, the check-run list is
    complete, and every other check-run completed without failing
  - no changed file is binary, or under BLOCKED_PATHS
  - `main`'s tripwire on merge-base(origin/main, SHA)..SHA raised no blocking flag (see `blocking`)
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
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# The CI jobs that must pass. `tests (…)` runs `make test` and `make lint`; until CI has those jobs
# (#44), the gate refuses every PR (ADR 0030).
REQUIRED_CHECKS = ("checks (ubuntu-latest)", "checks (macos-latest)", "tests (ubuntu-latest)", "tests (macos-latest)")
STOP_FILE = "cruise-stop"  # in the git common dir: `touch .git/cruise-stop` stops every merge
TOKEN_FILE_ENV = "CRUISE_MERGE_TOKEN_FILE"
DEFAULT_TOKEN_FILE = "~/.config/coin-accounting/cruise-merge-token"
# Paths the gate refuses on top of the tripwire's risky paths: the tax, doxx and chain engines, and
# the files that control cruise mode itself.
BLOCKED_PATHS = ("backend/coinacct/tax/", "backend/coinacct/doxx/", "backend/coinacct/chain/", "PROCESS_MODE",
                 "docs/cruise-mode.md")

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
    missing = [c for c in REQUIRED_CHECKS if c not in facts["protected_checks"]]
    if not facts["main_protected"] or missing:
        reasons.append("main isn't protected with every required check (see docs/cruise-mode.md)")
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
    reasons.extend(f"binary file changed: {p}" for p in facts["binary_files"])
    for p in facts["changed_files"]:
        if any(p == b or (b.endswith("/") and p.startswith(b)) for b in BLOCKED_PATHS):
            reasons.append(f"blocked path changed: {p}")
    if facts["tripwire"] is None:
        reasons.append("the tripwire did not run")
    else:
        reasons.extend(f"tripwire: {b}" for b in blocking(facts["tripwire"]))
    if facts["token_problem"]:
        reasons.append(facts["token_problem"])
    return reasons


def run(*cmd: str, env: dict | None = None) -> str:
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


def gather(n: int, sha: str) -> dict:
    repo = json.loads(run("gh", "repo", "view", "--json", "nameWithOwner,owner"))
    name, owner = repo["nameWithOwner"], repo["owner"]["login"]
    github_main = gh_api(f"repos/{name}/commits/main")["sha"]
    local_main = run("git", "rev-parse", "refs/remotes/origin/main").strip()
    try:
        mode = run("git", "show", "refs/remotes/origin/main:PROCESS_MODE").strip()
    except subprocess.CalledProcessError:
        mode = "(missing)"
    branch = gh_api(f"repos/{name}/branches/main")
    checks = ((branch.get("protection") or {}).get("required_status_checks") or {}).get("contexts") or []
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
    head_has_main = subprocess.run(["git", "merge-base", "--is-ancestor", github_main, sha]).returncode == 0
    merge_base = run("git", "merge-base", "refs/remotes/origin/main", sha).strip()
    numstat = run("git", "diff", "--numstat", "--no-renames", merge_base, sha).splitlines()
    binary = [line.split("\t", 2)[2] for line in numstat if line.startswith("-\t-\t")]
    changed = run("git", "diff", "--name-only", "--no-renames", merge_base, sha).splitlines()
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
        "main_protected": branch.get("protected") is True,
        "protected_checks": checks,
        "pr": pr,
        "sha": sha,
        "owner": owner,
        "head_has_main": head_has_main,
        "commits": commits,
        "check_runs": runs,
        "check_runs_total": total,
        "binary_files": binary,
        "changed_files": changed,
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
    except (subprocess.CalledProcessError, OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as e:
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
    except (subprocess.CalledProcessError, RuntimeError):
        print(f"cruise_merge: GitHub refused to merge PR #{args.pr}", file=sys.stderr)
        return 2
    print(f"cruise_merge: merged PR #{args.pr} at {args.sha[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
