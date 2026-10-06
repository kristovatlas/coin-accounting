from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import cruise_merge  # noqa: E402
import tripwire  # noqa: E402

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
        "dynamic_code": [],
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
            "backend/tests/unit/conftest.py", "frontend/src/views/tax/Report.tsx", "frontend/vite.config.ts",
            "e2e/specs/tax/export.spec.ts", "e2e/playwright.config.ts", "frontend/tsconfig.json",
            "docs/THREAT_MODEL.md", "docs/ENGINEERING.md", "PLAN.md", "docs/DEPENDENCIES.md",
            "docs/cruise-mode-notes.md", "frontend/src/logo.svg",
        ]
        self.assertEqual(cruise_merge.decide(facts), [])

    def test_any_branch_of_the_repository_can_merge(self):
        facts = good_facts()
        facts["pr"]["head"].update(ref="m0.3/launcher-serve")
        self.assertEqual(cruise_merge.decide(facts), [])

    def test_dynamic_code_is_reported_not_blocking(self):
        # ADR 0031: the Opus tripwire judges it; a Medium-or-above flag sends the PR to the human.
        facts = good_facts()
        facts["dynamic_code"].append(("backend/coinacct/x.py", 3))
        self.assertEqual(cruise_merge.decide(facts), [])

    def test_only_structural_tripwire_flags_block(self):
        blocking = [
            {"file": "x", "kind": "symlink", "detail": "symbolic link added, changed or removed"},
            {"file": "x", "kind": "submodule", "detail": "git submodule (gitlink)"},
            {"file": "x", "kind": "executable", "detail": "file made executable"},
            {"file": "x", "kind": "unparsed", "detail": "could not parse this change"},
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
            {"file": "x.py", "kind": "novel", "detail": "a kind added to the tripwire later"},
        ]
        self.assertEqual(cruise_merge.blocking(reported), [])

    def test_reasons_never_quote_source_text(self):
        facts = good_facts()
        facts["tripwire"] = [{"file": "x.py", "kind": "symlink", "detail": "symbolic link", "text": "SECRET"}]
        self.assertNotIn("SECRET", "\n".join(cruise_merge.decide(facts)))


class GitScanTests(unittest.TestCase):
    """The gate's own git reads: unquoted NUL-separated paths, and the dynamic-call scan."""

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

    def test_a_non_ascii_path_comes_back_unquoted_and_is_blocked(self):
        head = self.commit({"docs/adr/0099-règles.md": "y = 2\n"})
        changed = cruise_merge.git_z("diff", *cruise_merge.DIFF_OPTS, "--name-only", "-z", self.base, head)
        self.assertEqual(changed, ["docs/adr/0099-règles.md"])
        self.assertTrue(cruise_merge.blocked_path(changed[0]))

    def test_dangerous_dynamic_calls_are_found_and_re_compile_is_not(self):
        head = self.commit({
            "b.py": "import re\nPAT = re.compile(r'x')\nQ = re.compile('abc')\nm = pattern.exec('s')\n",
            "c.py": "z = 1\nexec(open('p.txt').read())\nm = __import__('o' + 's')\n",
            "d.py": "f = getattr(os, 'sy' + 'stem')\ncode = compile(src, 'f', 'exec')\nb = __builtins__\n",
            "e.js": "const cp = require('child_' + 'process')\n++n; eval(s)\nconst m = await import(name)\n",
            "f.html": "<p>hi</p>\n<script>eval(location.hash)</script>\n",
            "g.ts": "let x: Function\nconst y = new Function('return 1')\nwindow['fe' + 'tch'](u)\n",
            "h.py": "lookup = getattr\nrun = ｅｘｅｃ\nf = op.attrgetter('system')\n",
        })
        names = ["b.py", "c.py", "d.py", "e.js", "f.html", "g.ts", "h.py"]
        hits = cruise_merge.added_dynamic_code(self.base, head, names)
        self.assertEqual(hits, [("c.py", 2), ("c.py", 3), ("d.py", 1), ("d.py", 2), ("d.py", 3),
                                ("e.js", 1), ("e.js", 2), ("e.js", 3), ("f.html", 2), ("g.ts", 2), ("g.ts", 3),
                                ("h.py", 1), ("h.py", 2), ("h.py", 3)])

    def test_ordinary_python_is_not_flagged(self):
        # Ruff-formatted multi-line imports, mapping access and vars(): not dynamic code in Python.
        head = self.commit({
            "i.py": "from a.b import (\n    c,\n)\nv = self[key]\nopts = vars(args)\nm = obj.exec_module\n",
        })
        self.assertEqual(cruise_merge.added_dynamic_code(self.base, head, ["i.py"]), [])


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
