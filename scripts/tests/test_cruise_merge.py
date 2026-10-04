from __future__ import annotations

import copy
import os
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
    return {
        "main_fresh": True,
        "mode": "cruise",
        "stop_file": False,
        "sha": SHA,
        "owner": OWNER,
        "pr": {
            "state": "open",
            "draft": False,
            "head": {"sha": SHA, "repo": REPO},
            "base": {"ref": "main", "repo": REPO},
            "user": {"login": OWNER},
            "mergeable": True,
            "commits": 1,
        },
        "commits": [{"sha": SHA, "author": {"login": OWNER}, "committer": {"login": "web-flow"}}],
        "check_runs": [
            {"name": n, "app": {"slug": "github-actions"}, "status": "completed", "conclusion": "success"}
            for n in cruise_merge.REQUIRED_CHECKS
        ]
        + [{"name": "Socket Security", "app": {"slug": "socket"}, "status": "completed", "conclusion": "success"}],
        "tripwire": [
            {"file": "backend/coinacct/tax/engine.py", "kind": "content", "detail": "dynamic code (line 3)"},
            {"file": "backend/tests/unit/x.py", "kind": "content", "detail": "URL host 127.0.0.1 (line 9)"},
        ],
        "token_problem": None,
    }


class DecideTests(unittest.TestCase):
    """The gate merges only when every condition holds (ADR 0030); each one alone refuses."""

    def test_a_pr_meeting_every_condition_is_merged(self):
        self.assertEqual(cruise_merge.decide(good_facts()), [])

    def refused(self, change) -> list[str]:
        facts = copy.deepcopy(good_facts())
        change(facts)
        reasons = cruise_merge.decide(facts)
        self.assertTrue(reasons, "expected the gate to refuse")
        return reasons

    def test_each_condition_alone_refuses(self):
        cases = {
            "stale main": lambda f: f.update(main_fresh=False),
            "standard mode": lambda f: f.update(mode="standard"),
            "missing mode": lambda f: f.update(mode="(missing)"),
            "stop file": lambda f: f.update(stop_file=True),
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
            "commit list short": lambda f: f["pr"].update(commits=2),
            "foreign commit": lambda f: f["commits"][0].update(author={"login": "x"}, committer={"login": "y"}),
            "linux CI missing": lambda f: f.update(check_runs=f["check_runs"][1:]),
            "CI from another app": lambda f: f["check_runs"][0]["app"].update(slug="impostor"),
            "CI failed": lambda f: f["check_runs"][1].update(conclusion="failure"),
            "check running": lambda f: f["check_runs"][2].update(status="in_progress", conclusion=None),
            "Socket failed": lambda f: f["check_runs"][2].update(conclusion="failure"),
            "tripwire didn't run": lambda f: f.update(tripwire=None),
            "no token": lambda f: f.update(token_problem="no cruise merge token"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.refused(change)

    def test_blocking_tripwire_flags_refuse_and_noisy_ones_dont(self):
        blocked = [
            {"file": "AGENTS.md", "kind": "path", "detail": "agent instructions or tooling"},
            {"file": "backend/coinacct/rpc.py", "kind": "path", "detail": "security-critical module"},
            {"file": "docs/THREAT_MODEL.md", "kind": "path", "detail": "binding document or ADR"},
            {"file": "uv.lock", "kind": "path", "detail": "dependency or install config"},
            {"file": "x.py", "kind": "removed", "detail": "removed test or assertion (near line 4)"},
            {"file": "x.py", "kind": "deleted", "detail": "file deleted"},
            {"file": "x", "kind": "symlink", "detail": "symbolic link added, changed or removed"},
            {"file": "x.py", "kind": "content", "detail": "network use (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "process execution (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "test weakening (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "URL host example.com (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "long encoded blob (line 2)"},
            {"file": "x.py", "kind": "novel", "detail": "a kind added to the tripwire later"},
        ]
        for flag in blocked:
            with self.subTest(flag["detail"]):
                self.assertEqual(len(cruise_merge.blocking([flag])), 1)
        noisy = [
            {"file": "x.py", "kind": "content", "detail": "dynamic code or deserialisation (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "environment-dependent behaviour (line 2)"},
            {"file": "x.py", "kind": "content", "detail": "URL host localhost (line 2)"},
        ]
        self.assertEqual(cruise_merge.blocking(noisy), [])

    def test_reasons_never_quote_source_text(self):
        facts = good_facts()
        facts["tripwire"] = [{"file": "x.py", "kind": "content", "detail": "network use (line 2)", "text": "SECRET"}]
        self.assertNotIn("SECRET", "\n".join(cruise_merge.decide(facts)))


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "token"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_private_token_file_is_accepted(self):
        self.path.write_text("t\n")
        self.path.chmod(0o600)
        self.assertIsNone(cruise_merge.check_token_file(self.path))

    def test_a_missing_readable_or_linked_token_file_is_refused(self):
        self.assertIsNotNone(cruise_merge.check_token_file(self.path))
        self.path.write_text("t\n")
        self.path.chmod(0o644)
        self.assertIsNotNone(cruise_merge.check_token_file(self.path))
        self.path.chmod(0o600)
        link = Path(self._tmp.name) / "link"
        os.symlink(self.path, link)
        self.assertIsNotNone(cruise_merge.check_token_file(link))


class MainTests(unittest.TestCase):
    def test_a_short_sha_is_refused_before_anything_runs(self):
        self.assertEqual(cruise_merge.main(["5", "abc123"]), 2)


if __name__ == "__main__":
    unittest.main()
