from __future__ import annotations

import contextlib
import copy
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import cruise_merge  # noqa: E402

SHA = "a" * 40
OWNER = "owner"
REPO = {"full_name": "owner/repo"}


def good_facts() -> dict:
    """A PR that every condition accepts."""
    runs = [
        {"name": n, "app": {"slug": "github-actions"}, "status": "completed", "conclusion": "success"}
        for n in cruise_merge.REQUIRED_CHECKS
    ] + [{"name": "Socket Security", "app": {"slug": "socket"}, "status": "completed", "conclusion": "success"}]
    return {
        "github_main": "c" * 40,
        "main_fresh": True,
        "mode": "cruise",
        "stop_file": False,
        "gate_is_mains": True,
        "protection": {
            "required_status_checks": {"strict": True, "contexts": list(cruise_merge.REQUIRED_CHECKS)},
            "enforce_admins": {"enabled": True},
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "allow_deletions": {"enabled": False},
            "allow_force_pushes": {"enabled": False},
        },
        "sha": SHA,
        "owner": OWNER,
        "pr": {
            "state": "open",
            "draft": False,
            "head": {"sha": SHA, "ref": "cruise/m0.3-1-discovery", "repo": REPO},
            "base": {"ref": "main", "repo": REPO},
            "user": {"login": OWNER},
            "mergeable": True,
            "commits": 1,
        },
        "head_has_main": True,
        "commits": [{"sha": SHA, "author": {"login": OWNER}, "committer": {"login": "web-flow"}}],
        "check_runs": runs,
        "check_runs_total": len(runs),
        "binary_files": [],
        "changed_files": ["backend/coinacct/services/discovery.py", "backend/tests/unit/test_discovery.py"],
        "review_status": "success",
        "deleted_files": [],
        "tripwire": [
            {"file": "backend/coinacct/services/discovery.py", "kind": "content",
             "detail": "dynamic code or deserialisation (line 3)"},
            {"file": "backend/coinacct/services/discovery.py", "kind": "content",
             "detail": "environment-dependent behaviour (line 9)"},
        ],
        "token_problem": None,
    }


class DecideTests(unittest.TestCase):
    """The gate merges only when every condition holds (ADR 0030); each one alone refuses."""

    def test_a_pr_meeting_every_condition_is_merged(self):
        self.assertEqual(cruise_merge.decide(good_facts()), [])

    def test_each_condition_alone_refuses(self):
        cases = {
            "stale main": lambda f: f.update(main_fresh=False),
            "standard mode": lambda f: f.update(mode="standard"),
            "missing mode": lambda f: f.update(mode="(missing)"),
            "stop file": lambda f: f.update(stop_file=True),
            "not main's gate": lambda f: f.update(gate_is_mains=False),
            "main unprotected": lambda f: f.update(protection=None),
            "tests not required": lambda f: f["protection"]["required_status_checks"].update(
                contexts=["checks (ubuntu-latest)"]),
            "not strict": lambda f: f["protection"]["required_status_checks"].update(strict=False),
            "admins bypass": lambda f: f["protection"].update(enforce_admins={"enabled": False}),
            "no PR required": lambda f: f["protection"].pop("required_pull_request_reviews"),
            "an approval required": lambda f: f["protection"]["required_pull_request_reviews"].update(
                required_approving_review_count=1),
            "code owners required": lambda f: f["protection"]["required_pull_request_reviews"].update(
                require_code_owner_reviews=True),
            "main deletable": lambda f: f["protection"].update(allow_deletions={"enabled": True}),
            "main force-pushable": lambda f: f["protection"].update(allow_force_pushes={"enabled": True}),
            "closed": lambda f: f["pr"].update(state="closed"),
            "draft": lambda f: f["pr"].update(draft=True),
            "draft unknown": lambda f: f["pr"].pop("draft"),
            "head moved": lambda f: f["pr"]["head"].update(sha="b" * 40),
            "stacked": lambda f: f["pr"]["base"].update(ref="m0.4/x"),
            "fork": lambda f: f["pr"]["head"].update(repo={"full_name": "other/repo"}),
            "deleted fork": lambda f: f["pr"]["head"].update(repo=None),
            "not the owner's": lambda f: f["pr"].update(user={"login": "someone"}),
            "conflicting": lambda f: f["pr"].update(mergeable=False),
            "mergeable unknown": lambda f: f["pr"].update(mergeable=None),
            "head lacks main": lambda f: f.update(head_has_main=False),
            "commit list short": lambda f: f["pr"].update(commits=2),
            "too many commits": lambda f: f.update(commits=f["commits"] * 251) or f["pr"].update(commits=251),
            "foreign commit": lambda f: f["commits"][0].update(author={"login": "x"}, committer={"login": "y"}),
            "tests job missing": lambda f: f.update(check_runs=f["check_runs"][:2] + f["check_runs"][4:],
                                                    check_runs_total=3),
            "CI from another app": lambda f: f["check_runs"][0]["app"].update(slug="impostor"),
            "CI failed": lambda f: f["check_runs"][1].update(conclusion="failure"),
            "check running": lambda f: f["check_runs"][4].update(status="in_progress", conclusion=None),
            "Socket failed": lambda f: f["check_runs"][4].update(conclusion="failure"),
            "check-runs incomplete": lambda f: f.update(check_runs_total=101),
            "binary file": lambda f: f["binary_files"].append("backend/coinacct/fast.cpython-313-x86_64-linux-gnu.so"),
            "unscanned file type": lambda f: f["changed_files"].append("backend/coinacct/payload.bin"),
            "no suffix": lambda f: f["changed_files"].append("backend/coinacct/runme"),
            "mode switch": lambda f: f["changed_files"].append("PROCESS_MODE"),
            "mode switch, lower case": lambda f: f["changed_files"].append("process_mode"),
            "cruise guide": lambda f: f["changed_files"].append("docs/cruise-mode.md"),
            "the gate itself": lambda f: f["changed_files"].append("scripts/cruise_merge.py"),
            "the agent guard": lambda f: f["changed_files"].append("scripts/agent_guard.py"),
            "the toolchain lock": lambda f: f["changed_files"].append("scripts/toolchain.lock"),
            "a skill": lambda f: f["changed_files"].append(".claude/skills/cruise/SKILL.md"),
            "agent settings": lambda f: f["changed_files"].append(".claude/settings.json"),
            "agent instructions": lambda f: f["changed_files"].append("AGENTS.md"),
            "claude instructions": lambda f: f["changed_files"].append("CLAUDE.md"),
            "a CI workflow": lambda f: f["changed_files"].append(".github/workflows/ci.yml"),
            "the Makefile": lambda f: f["changed_files"].append("Makefile"),
            "a new ADR": lambda f: f["changed_files"].append("docs/adr/0032-x.md"),
            "the architecture baseline": lambda f: f["changed_files"].append("docs/architecture.md"),
            "pyproject": lambda f: f["changed_files"].append("pyproject.toml"),
            "uv lockfile": lambda f: f["changed_files"].append("uv.lock"),
            "a package.json": lambda f: f["changed_files"].append("frontend/package.json"),
            "the pnpm lockfile": lambda f: f["changed_files"].append("pnpm-lock.yaml"),
            "npm publish times": lambda f: f["changed_files"].append("pnpm-lock.times.json"),
            "pnpm workspace": lambda f: f["changed_files"].append("pnpm-workspace.yaml"),
            "an npmrc": lambda f: f["changed_files"].append("frontend/.npmrc"),
            "a pnpmfile": lambda f: f["changed_files"].append(".pnpmfile.cjs"),
            "an MCP config": lambda f: f["changed_files"].append(".mcp.json"),
            "a nested MCP config": lambda f: f["changed_files"].append("frontend/.mcp.json"),
            "agent instruction overrides": lambda f: f["changed_files"].append("AGENTS.override.md"),
            "scoped agent instructions": lambda f: f["changed_files"].append("backend/AGENTS.md"),
            "scoped claude instructions": lambda f: f["changed_files"].append("frontend/CLAUDE.md"),
            "local claude instructions": lambda f: f["changed_files"].append("CLAUDE.local.md"),
            "a nested skill": lambda f: f["changed_files"].append("backend/.claude/skills/x/SKILL.md"),
            "codex config": lambda f: f["changed_files"].append(".codex/config.toml"),
            "nested codex config": lambda f: f["changed_files"].append("e2e/.codex/config.toml"),
            "root install config": lambda f: f["changed_files"].append("uv" + ".toml"),
            "nested install config": lambda f: f["changed_files"].append("backend/uv" + ".toml"),
            "python version pin": lambda f: f["changed_files"].append(".python-version"),
            "node version pin": lambda f: f["changed_files"].append(".node-version"),
            "a requirements variant": lambda f: f["changed_files"].append("requirements-dev.txt"),
            "a constraints file": lambda f: f["changed_files"].append("backend/constraints.txt"),
            "a pnpmfile variant": lambda f: f["changed_files"].append("e2e/.pnpmfile.js"),
            "the conftest that installs the socket guard": lambda f: f["changed_files"].append(
                "backend/tests/conftest.py"),
            "a nested conftest": lambda f: f["changed_files"].append("backend/tests/unit/conftest.py"),
            "a pytest.toml": lambda f: f["changed_files"].append("pytest.toml"),
            "a hidden pytest.toml": lambda f: f["changed_files"].append(".pytest.toml"),
            "the tests package init": lambda f: f["changed_files"].append("backend/tests/__init__.py"),
            "a ruff config": lambda f: f["changed_files"].append("ruff.toml"),
            "a nested hidden ruff config": lambda f: f["changed_files"].append("backend/coinacct/.ruff.toml"),
            "a pytest.ini": lambda f: f["changed_files"].append("backend/tests/pytest.ini"),
            "a tox.ini": lambda f: f["changed_files"].append("tox.ini"),
            "a setup.cfg": lambda f: f["changed_files"].append("setup.cfg"),
            "a coveragerc": lambda f: f["changed_files"].append(".coveragerc"),
            "a mypy.ini": lambda f: f["changed_files"].append("mypy.ini"),
            "an eslint config": lambda f: f["changed_files"].append("frontend/eslint.config.js"),
            "an old eslintrc": lambda f: f["changed_files"].append("frontend/.eslintrc.json"),
            "a vitest config": lambda f: f["changed_files"].append("frontend/vitest.config.ts"),
            "gemini instructions": lambda f: f["changed_files"].append("GEMINI.md"),
            "vscode MCP servers": lambda f: f["changed_files"].append(".vscode/mcp.json"),
            "vscode tasks": lambda f: f["changed_files"].append(".vscode/tasks.json"),
            "cursor rules": lambda f: f["changed_files"].append(".cursorrules"),
            "aider config": lambda f: f["changed_files"].append(".aider.conf.yml"),
            "windsurf rules": lambda f: f["changed_files"].append(".windsurf/rules/rule.md"),
            "a dev container": lambda f: f["changed_files"].append(".devcontainer/devcontainer.json"),
            "a root dev container": lambda f: f["changed_files"].append(".devcontainer.json"),
            "cline rules": lambda f: f["changed_files"].append(".clinerules/rules.md"),
            "a vite config": lambda f: f["changed_files"].append("frontend/vite.config.ts"),
            "the mutation exclusions": lambda f: f["changed_files"].append("backend/tests/mutation-exclusions.md"),
            "vendored code": lambda f: f["changed_files"].append("backend/coinacct/vendor/lib.py"),
            "third-party code": lambda f: f["changed_files"].append("frontend/src/third_party/x.ts"),
            "a minified bundle": lambda f: f["changed_files"].append("frontend/src/lib.min.js"),
            "a tax module deleted": lambda f: f["deleted_files"].append("backend/coinacct/tax/lots.py"),
            "a chain module renamed away": lambda f: f["deleted_files"].append("backend/coinacct/chain/scan.py"),
            "the permanent block label": lambda f: f["pr"].update(labels=[{"name": "autopilot-blocked"}]),
            "socket config": lambda f: f["changed_files"].append("socket.yml"),
            "copied node_modules": lambda f: f["changed_files"].append("frontend/node_modules/pkg/index.js"),
            "copied site-packages": lambda f: f["changed_files"].append("backend/site-packages/pkg/__init__.py"),
            "a minified tsx": lambda f: f["changed_files"].append("frontend/src/bundle.min.tsx"),
            "a minified svg": lambda f: f["changed_files"].append("frontend/src/asset.min.svg"),
            "nested socket config": lambda f: f["changed_files"].append("frontend/.socket.yaml"),
            "zed settings": lambda f: f["changed_files"].append(".zed/settings.json"),
            "opencode config": lambda f: f["changed_files"].append("opencode.json"),
            "a root module shadowing coverage": lambda f: f["changed_files"].append("coverage.py"),
            "a root package shadowing pytest": lambda f: f["changed_files"].append("pytest/__init__.py"),
            "a new top-level directory": lambda f: f["changed_files"].append("tools/notes.md"),
            "a module directly under backend": lambda f: f["changed_files"].append("backend/hypothesis.py"),
            "a .pth file under backend": lambda f: f["changed_files"].append("backend/x.pth"),
            "a new package under backend": lambda f: f["changed_files"].append("backend/_pytest/__init__.py"),
            "a new package under e2e": lambda f: f["changed_files"].append("e2e/pytest/__init__.py"),
            "not cleared by the panel": lambda f: f.update(review_status=None),
            "the panel's status failed": lambda f: f.update(review_status="failure"),
            "the panel's status pending": lambda f: f.update(review_status="pending"),
            "the socket guard": lambda f: f["changed_files"].append("backend/tests/socket_guard.py"),
            "a package shadowing the socket guard": lambda f: f["changed_files"].append(
                "backend/tests/socket_guard/__init__.py"),
            "a symlink": lambda f: f.update(tripwire=[{"file": "x", "kind": "symlink", "detail": "symbolic link"}]),
            "an executable bit": lambda f: f.update(tripwire=[{"file": "x", "kind": "executable",
                                                                  "detail": "file made executable"}]),
            "tripwire didn't run": lambda f: f.update(tripwire=None),
            "no token": lambda f: f.update(token_problem="no readable cruise merge token"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                facts = copy.deepcopy(good_facts())
                change(facts)
                self.assertTrue(cruise_merge.decide(facts), "expected the gate to refuse")

    def test_application_code_tests_and_living_docs_merge_under_autopilot(self):
        # ADR 0031: the security-critical modules, the tax/doxx/chain engines, their tests and the
        # living binding documents are no longer the owner's to merge.
        facts = good_facts()
        facts["changed_files"] += [
            "backend/coinacct/tax/engine.py", "backend/tests/unit/tax/test_lots.py",
            "backend/coinacct/doxx/rules.py", "backend/coinacct/chain/scan.py",
            "backend/coinacct/api/security.py", "backend/coinacct/launcher.py", "backend/coinacct/rpc.py",
            "backend/coinacct/storage/watchdog.py", "backend/coinacct/domain/secret.py",
            "frontend/src/views/tax/Report.tsx",
            "e2e/specs/tax/export.spec.ts", "e2e/playwright.config.ts", "frontend/tsconfig.json",
            "docs/THREAT_MODEL.md", "docs/ENGINEERING.md", "PLAN.md", "docs/DEPENDENCIES.md",
            "docs/cruise-mode-notes.md", "frontend/src/logo.svg",
        ]
        self.assertEqual(cruise_merge.decide(facts), [])

    def test_other_labels_and_deletions_outside_the_engines_merge(self):
        facts = good_facts()
        facts["pr"]["labels"] = [{"name": "cruise"}]
        facts["deleted_files"] = ["backend/coinacct/services/old.py"]
        self.assertEqual(cruise_merge.decide(facts), [])

    def test_only_the_owners_review_status_counts(self):
        statuses = [
            {"context": "review-panel", "state": "success", "creator": {"login": "github-actions[bot]"}},
            {"context": "review-panel", "state": "failure", "creator": {"login": OWNER}},
            {"context": "review-panel", "state": "success", "creator": {"login": OWNER}},
        ]
        self.assertEqual(cruise_merge.owner_review_status(statuses, OWNER), "failure")  # newest of the owner's
        self.assertIsNone(cruise_merge.owner_review_status(statuses[:1], OWNER))

    def test_refused_paths_covers_every_path_rule(self):
        self.assertEqual(
            cruise_merge.refused_paths(
                ["backend/coinacct/x.py", "backend/run.sh", "backend/tests/conftest.py", "a.bin"],
                ["a.bin"],
                ["backend/coinacct/tax/lots.py", "backend/coinacct/services/old.py"],
            ),
            ["backend/run.sh", "backend/tests/conftest.py", "a.bin", "backend/coinacct/tax/lots.py"],
        )

    def test_any_branch_of_the_repository_can_merge(self):
        facts = good_facts()
        facts["pr"]["head"].update(ref="m0.3/launcher-serve")
        self.assertEqual(cruise_merge.decide(facts), [])

    def test_only_structural_tripwire_flags_block(self):
        blocking = [
            {"file": "x", "kind": "symlink", "detail": "symbolic link added, changed or removed"},
            {"file": "x", "kind": "submodule", "detail": "git submodule (gitlink)"},
            {"file": "x", "kind": "executable", "detail": "file made executable"},
            {"file": "x", "kind": "unparsed", "detail": "could not parse this change"},
            {"file": "x.py", "kind": "novel", "detail": "a kind added to the tripwire later"},
        ]
        for flag in blocking:
            with self.subTest(flag["kind"]):
                self.assertEqual(len(cruise_merge.blocking([flag])), 1)
        reported = [
            {"file": "AGENTS.md", "kind": "path", "detail": "agent instructions or tooling"},
            {"file": "backend/coinacct/rpc.py", "kind": "path", "detail": "security-critical module"},
            {"file": "x.py", "kind": "removed", "detail": "removed test or assertion (near line 4)"},
            {"file": "x.py", "kind": "deleted", "detail": "file deleted"},
            {"file": "x.py", "kind": "content", "detail": "network use (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "test weakening (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "dynamic code or deserialisation (line 2)"},
        ]
        self.assertEqual(cruise_merge.blocking(reported), [])

    def test_every_tripwire_kind_is_classified_on_purpose(self):
        # A kind the tripwire emits must be either reported (judged by the Opus tripwire) or one the
        # gate deliberately blocks; a new kind is caught here, and blocks until it is classified.
        source = (HERE / "tripwire.py").read_text()
        kinds = set(re.findall(r'"kind": "([a-z]+)"', source))
        self.assertTrue(kinds)
        deliberately_blocking = {"symlink", "submodule", "executable", "unparsed"}
        self.assertEqual(kinds - set(cruise_merge.REPORTED_KINDS), deliberately_blocking)

    def test_every_makefile_dependency_or_install_file_is_blocked(self):
        # The Makefile's approval gate (ENGINEERING 2.4) treats "on main" as approved, so every file it
        # guards must be one the gate never merges.
        makefile = (HERE.parent / "Makefile").read_text()
        globs = re.findall(r"':\(glob\)([^']+)'", makefile)
        self.assertGreater(len(globs), 10)
        for glob in globs:
            path = glob.replace("*/", "backend/").replace("*", "x")
            with self.subTest(glob):
                self.assertTrue(cruise_merge.blocked_path(path), path)

    def test_reasons_never_quote_source_text(self):
        facts = good_facts()
        facts["tripwire"] = [{"file": "x.py", "kind": "symlink", "detail": "symbolic link", "text": "SECRET"}]
        self.assertNotIn("SECRET", "\n".join(cruise_merge.decide(facts)))


class GitScanTests(unittest.TestCase):
    """The gate's own git reads: unquoted NUL-separated paths."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self._cwd = os.getcwd()
        os.chdir(self.repo)
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.py").write_text("x = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        return subprocess.run(["git", *args], cwd=self.repo, env=env, check=True, capture_output=True,
                              text=True).stdout

    def commit(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD").strip()

    def test_list_blocked_names_only_the_refused_paths(self):
        head = self.commit({"backend/coinacct/x.py": "y = 1\n", "backend/tests/conftest.py": "z = 1\n"})
        self.assertEqual(cruise_merge.list_blocked(self.base, head), ["backend/tests/conftest.py"])

    def test_list_blocked_cli_prints_the_refused_paths_and_fails_on_an_unknown_commit(self):
        head = self.commit({"backend/coinacct/x.py": "y = 1\n", "backend/tests/conftest.py": "z = 1\n"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cruise_merge.main(["--list-blocked", self.base, head]), 0)
        self.assertEqual(out.getvalue(), "backend/tests/conftest.py\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cruise_merge.main(["--list-blocked", self.base, "d" * 40]), 2)

    def test_a_non_ascii_path_comes_back_unquoted_and_is_blocked(self):
        head = self.commit({"docs/adr/0099-règles.md": "y = 2\n"})
        changed = cruise_merge.git_z("diff", *cruise_merge.DIFF_OPTS, "--name-only", "-z", self.base, head)
        self.assertEqual(changed, ["docs/adr/0099-règles.md"])
        self.assertTrue(cruise_merge.blocked_path(changed[0]))


class RecheckTests(unittest.TestCase):
    def setUp(self):
        self._saved = (cruise_merge.gh_api, cruise_merge.git_dir)
        self.tmp = tempfile.TemporaryDirectory()
        cruise_merge.git_dir = lambda: Path(self.tmp.name)
        self.labels: list = []
        self.statuses = [{"context": "review-panel", "state": "success", "creator": {"login": OWNER}}]

        def api(path):
            if path.endswith("/commits/main"):
                return {"sha": "c" * 40}
            if "/statuses" in path:
                return self.statuses
            return {"labels": self.labels}

        cruise_merge.gh_api = api
        self.facts = {**good_facts(), "name": "owner/repo"}
        self.facts["pr"]["number"] = 5

    def tearDown(self):
        cruise_merge.gh_api, cruise_merge.git_dir = self._saved
        self.tmp.cleanup()

    def test_unchanged_passes(self):
        self.assertEqual(cruise_merge.recheck(self.facts), [])

    def test_a_block_label_added_just_before_merging_refuses(self):
        self.labels.append({"name": "autopilot-blocked"})
        self.assertTrue(cruise_merge.recheck(self.facts))

    def test_a_newer_failing_owner_status_refuses(self):
        self.statuses.insert(0, {"context": "review-panel", "state": "failure", "creator": {"login": OWNER}})
        self.assertTrue(cruise_merge.recheck(self.facts))


class ListBlockedCliTests(unittest.TestCase):
    def test_bad_arguments_exit_2(self):
        self.assertEqual(cruise_merge.main(["--list-blocked", "abc"]), 2)
        self.assertEqual(cruise_merge.main(["--list-blocked", "--output=x", SHA]), 2)
        self.assertEqual(cruise_merge.main(["--list-blocked", SHA, "b" * 39]), 2)


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "token"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_private_token_file_is_read(self):
        self.path.write_text("t0ken\n")
        self.path.chmod(0o600)
        self.assertEqual(cruise_merge.read_token(self.path), ("t0ken", None))

    def test_a_missing_readable_empty_or_linked_token_file_is_refused(self):
        self.assertIsNone(cruise_merge.read_token(self.path)[0])
        self.path.write_text("t0ken\n")
        self.path.chmod(0o644)
        self.assertIsNone(cruise_merge.read_token(self.path)[0])
        self.path.chmod(0o600)
        link = Path(self._tmp.name) / "link"
        os.symlink(self.path, link)
        self.assertIsNone(cruise_merge.read_token(link)[0])
        self.path.write_text("")
        self.assertIsNone(cruise_merge.read_token(self.path)[0])

    def test_problems_never_include_the_token(self):
        self.path.write_text("t0ken\n")
        self.path.chmod(0o644)
        self.assertNotIn("t0ken", cruise_merge.read_token(self.path)[1])


class MainTests(unittest.TestCase):
    def setUp(self):
        self._saved = (cruise_merge.gather, cruise_merge.recheck, cruise_merge.merge)
        self.merged = []
        cruise_merge.merge = lambda name, n, sha: self.merged.append((n, sha))
        cruise_merge.recheck = lambda facts: []

    def tearDown(self):
        cruise_merge.gather, cruise_merge.recheck, cruise_merge.merge = self._saved

    def test_a_short_sha_is_refused_before_anything_runs(self):
        self.assertEqual(cruise_merge.main(["5", "abc123"]), 2)

    def test_a_good_pr_is_merged_and_a_dry_run_merges_nothing(self):
        cruise_merge.gather = lambda n, sha: {**good_facts(), "name": "owner/repo"}
        self.assertEqual(cruise_merge.main(["5", SHA, "--dry-run"]), 0)
        self.assertEqual(self.merged, [])
        self.assertEqual(cruise_merge.main(["5", SHA]), 0)
        self.assertEqual(self.merged, [(5, SHA)])

    def test_a_crash_while_checking_exits_2_and_merges_nothing(self):
        def boom(n, sha):
            raise ValueError("unexpected")
        cruise_merge.gather = boom
        self.assertEqual(cruise_merge.main(["5", SHA]), 2)
        self.assertEqual(self.merged, [])

    def test_a_refused_pr_exits_1_and_merges_nothing(self):
        cruise_merge.gather = lambda n, sha: {**good_facts(), "name": "owner/repo", "mode": "standard"}
        self.assertEqual(cruise_merge.main(["5", SHA]), 1)
        self.assertEqual(self.merged, [])

    def test_a_change_just_before_merging_refuses(self):
        cruise_merge.gather = lambda n, sha: {**good_facts(), "name": "owner/repo"}
        cruise_merge.recheck = lambda facts: ["main moved while the gate was checking"]
        self.assertEqual(cruise_merge.main(["5", SHA]), 1)
        self.assertEqual(self.merged, [])


if __name__ == "__main__":
    unittest.main()
