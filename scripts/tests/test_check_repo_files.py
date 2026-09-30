from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

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
        os.symlink("/etc/passwd", self.repo / "link")
        self.git("add", "link")
        self.assertIn("symbolic link", " ".join(check_repo_files.check(self.repo)))
        self.assertEqual(self.run_check(), 1)

    def test_a_submodule_gitlink_fails(self):
        sha = "0" * 39 + "1"
        self.git("update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
        self.assertIn("git submodule", " ".join(check_repo_files.check(self.repo)))

    def test_a_gitmodules_file_fails(self):
        (self.repo / ".gitmodules").write_text("[submodule \"x\"]\n")
        self.assertIn(".gitmodules", " ".join(check_repo_files.check(self.repo)))

    def test_this_repository_passes(self):
        self.assertEqual(check_repo_files.check(HERE.parent), [])


if __name__ == "__main__":
    unittest.main()
