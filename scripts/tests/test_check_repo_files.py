from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import check_repo_files  # noqa: E402

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class CheckRepoFilesTests(unittest.TestCase):
    """Symlinks and submodules are banned from the tree (ADR 0023)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.git("init", "-q")
        (self.repo / "a.py").write_text("x = 1\n")
        self.git("add", "a.py")

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, env=ENV, check=True, capture_output=True)

    def run_check(self) -> int:
        with redirect_stdout(io.StringIO()):
            return check_repo_files.main([str(self.repo)])

    def test_ordinary_files_pass(self):
        self.assertEqual(check_repo_files.check(self.repo), [])
        self.assertEqual(self.run_check(), 0)

    def test_a_tracked_symlink_fails(self):
        os.symlink("a.py", self.repo / "link")
        self.git("add", "link")
        self.assertIn("symbolic link", " ".join(check_repo_files.check(self.repo)))
        self.assertEqual(self.run_check(), 1)

    def test_a_submodule_gitlink_fails(self):
        sha = "0" * 39 + "1"
        self.git("update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
        self.assertIn("git submodule", " ".join(check_repo_files.check(self.repo)))
        self.assertEqual(self.run_check(), 1)

    def test_a_tracked_gitmodules_file_fails(self):
        (self.repo / ".gitmodules").write_text("[submodule \"x\"]\n")
        self.git("add", ".gitmodules")
        self.assertIn(".gitmodules", " ".join(check_repo_files.check(self.repo)))
        self.assertEqual(self.run_check(), 1)

    def test_workflow_linter_config_files_fail(self):
        # ADR 0026: config in the checked repository could switch an audit off (e.g. impostor-commit).
        for path in ("zizmor.yml", ".github/zizmor.yaml", ".github/actionlint.yaml", ".github/ActionLint.yml"):
            with self.subTest(path=path):
                (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
                (self.repo / path).write_text("rules: {}\n")
                self.git("add", path)
                self.assertIn("workflow-linter config", " ".join(check_repo_files.check(self.repo)))
                self.git("rm", "-q", "--cached", path)
                (self.repo / path).unlink()
        self.assertEqual(check_repo_files.check(self.repo), [])

    def test_zizmor_ignore_comments_under_github_fail(self):
        wf = self.repo / ".github" / "workflows" / "ci.yml"
        wf.parent.mkdir(parents=True)
        for text in ("- uses: a/b@0123 # zizmor: ignore[impostor-commit]\n", "x: 1 # Zizmor:Ignore[foo]\n"):
            with self.subTest(text=text):
                wf.write_text(text)
                self.git("add", ".github/workflows/ci.yml")
                self.assertIn("zizmor ignore comment", " ".join(check_repo_files.check(self.repo)))
        wf.write_text("- uses: a/b@0123 # v1\n")
        self.git("add", ".github/workflows/ci.yml")
        self.assertEqual(check_repo_files.check(self.repo), [])

    def test_an_untracked_gitmodules_file_is_ignored(self):
        (self.repo / ".gitmodules").write_text("[submodule \"x\"]\n")
        self.assertEqual(check_repo_files.check(self.repo), [])

    def test_a_subdirectory_checks_the_whole_repository(self):
        os.symlink("a.py", self.repo / "link")
        self.git("add", "link")
        (self.repo / "sub").mkdir()
        self.assertIn("symbolic link", " ".join(check_repo_files.check(self.repo / "sub")))

    def test_not_a_git_repository_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(check_repo_files.main([d]), 1)

    def test_an_inherited_index_override_is_ignored(self):
        os.symlink("a.py", self.repo / "link")
        self.git("add", "link")
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": os.devnull}):
            self.assertIn("symbolic link", " ".join(check_repo_files.check(self.repo)))

    def test_this_repository_passes(self):
        self.assertEqual(check_repo_files.check(HERE.parent), [])


if __name__ == "__main__":
    unittest.main()
