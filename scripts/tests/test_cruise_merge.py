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
            "not a cruise branch": lambda f: f["pr"]["head"].update(ref="feat/something"),
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
            "tax engine": lambda f: f["changed_files"].append("backend/coinacct/tax/engine.py"),
            "tax tests": lambda f: f["changed_files"].append("backend/tests/unit/tax/test_lots.py"),
            "tax golden file": lambda f: f["changed_files"].append("backend/tests/integration/tax/golden/8949.csv"),
            "tax in upper case": lambda f: f["changed_files"].append("backend/coinacct/Tax/engine.py"),
            "tax with a non-ASCII name": lambda f: f["changed_files"].append("backend/coinacct/tax/règles.py"),
            "unscanned file type": lambda f: f["changed_files"].append("backend/coinacct/payload.txt"),
            "no suffix": lambda f: f["changed_files"].append("backend/coinacct/runme"),
            "dynamic code": lambda f: f["dynamic_code"].append(("backend/coinacct/x.py", 3)),
            "doxx engine": lambda f: f["changed_files"].append("backend/coinacct/doxx/rules.py"),
            "chain module": lambda f: f["changed_files"].append("backend/coinacct/chain/scan.py"),
            "mode switch": lambda f: f["changed_files"].append("PROCESS_MODE"),
            "mode switch, lower case": lambda f: f["changed_files"].append("process_mode"),
            "root pytest.toml": lambda f: f["changed_files"].append("pytest.toml"),
            "nested ruff.toml": lambda f: f["changed_files"].append("backend/ruff.toml"),
            "a conftest": lambda f: f["changed_files"].append("backend/tests/unit/conftest.py"),
            "an eslint config": lambda f: f["changed_files"].append("frontend/eslint.config.js"),
            "pyproject": lambda f: f["changed_files"].append("pyproject.toml"),
            "cruise guide": lambda f: f["changed_files"].append("docs/cruise-mode.md"),
            "tripwire didn't run": lambda f: f.update(tripwire=None),
            "no token": lambda f: f.update(token_problem="no readable cruise merge token"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                facts = copy.deepcopy(good_facts())
                change(facts)
                self.assertTrue(cruise_merge.decide(facts), "expected the gate to refuse")

    def test_a_similar_path_outside_the_blocked_ones_is_allowed(self):
        facts = good_facts()
        facts["changed_files"] += ["backend/coinacct/taxonomy.py", "docs/cruise-mode-notes.md",
                                   "frontend/src/views/tax/Report.tsx", "backend/coinacct/services/tax.py"]
        self.assertEqual(cruise_merge.decide(facts), [])

    def test_every_flag_blocks_except_the_two_noisy_content_labels(self):
        blocked = [
            {"file": "AGENTS.md", "kind": "path", "detail": "agent instructions or tooling"},
            {"file": "backend/coinacct/rpc.py", "kind": "path", "detail": "security-critical module"},
            {"file": "x.py", "kind": "removed", "detail": "removed test or assertion (near line 4)"},
            {"file": "x.py", "kind": "deleted", "detail": "file deleted"},
            {"file": "x", "kind": "symlink", "detail": "symbolic link added, changed or removed"},
            {"file": "x", "kind": "submodule", "detail": "git submodule (gitlink)"},
            {"file": "x", "kind": "executable", "detail": "file made executable"},
            {"file": "x", "kind": "unparsed", "detail": "could not parse this change"},
            {"file": "x.py", "kind": "content", "detail": "URL host 127.0.0.1 (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "URL host example.com (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "long encoded blob (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "network access (line 2)"},  # a renamed label
            {"file": "x.py", "kind": "novel", "detail": "a kind added to the tripwire later"},
        ]
        for flag in blocked:
            with self.subTest(flag["detail"]):
                self.assertEqual(len(cruise_merge.blocking([flag])), 1)
        noisy = [
            {"file": "x.py", "kind": "content", "detail": "dynamic code or deserialisation (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "environment-dependent behaviour (line 2)"},
        ]
        self.assertEqual(cruise_merge.blocking(noisy), [])

    def test_every_real_tripwire_content_label_is_classified_on_purpose(self):
        # A label the tripwire adds or renames blocks by default; this lists today's on purpose.
        labels = {label for label, _ in tripwire.ADDED_RULES}
        self.assertTrue(set(cruise_merge.NON_BLOCKING_CONTENT) <= labels)
        for label in labels - set(cruise_merge.NON_BLOCKING_CONTENT):
            with self.subTest(label):
                flag = {"file": "x.py", "kind": "content", "detail": f"{label} (line 1)"}
                self.assertEqual(len(cruise_merge.blocking([flag])), 1)

    def test_reasons_never_quote_source_text(self):
        facts = good_facts()
        facts["tripwire"] = [{"file": "x.py", "kind": "content", "detail": "network use (line 2)", "text": "SECRET"}]
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
        head = self.commit({"backend/coinacct/tax/règles.py": "y = 2\n"})
        changed = cruise_merge.git_z("diff", *cruise_merge.DIFF_OPTS, "--name-only", "-z", self.base, head)
        self.assertEqual(changed, ["backend/coinacct/tax/règles.py"])
        self.assertTrue(cruise_merge.blocked_path(changed[0]))

    def test_dangerous_dynamic_calls_are_found_and_re_compile_is_not(self):
        head = self.commit({
            "b.py": "import re\nPAT = re.compile(r'x')\nQ = re.compile('abc')\nm = pattern.exec('s')\n",
            "c.py": "z = 1\nexec(open('p.txt').read())\nm = __import__('o' + 's')\n",
            "d.py": "f = getattr(os, 'sy' + 'stem')\ncode = compile(src, 'f', 'exec')\nb = __builtins__\n",
            "e.js": "const cp = require('child_' + 'process')\n++n; eval(s)\nconst m = await import(name)\n",
            "f.html": "<p>hi</p>\n<script>eval(location.hash)</script>\n",
            "g.ts": "let x: Function\nconst y = new Function('return 1')\nwindow['fe' + 'tch'](u)\n",
        })
        names = ["b.py", "c.py", "d.py", "e.js", "f.html", "g.ts"]
        hits = cruise_merge.added_dynamic_code(self.base, head, names)
        self.assertEqual(hits, [("c.py", 2), ("c.py", 3), ("d.py", 1), ("d.py", 2), ("d.py", 3),
                                ("e.js", 1), ("e.js", 2), ("e.js", 3), ("f.html", 2), ("g.ts", 2), ("g.ts", 3)])


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
