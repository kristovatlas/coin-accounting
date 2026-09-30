from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

try:
    import check_lockfiles
except SystemExit:  # host Python < 3.11: the check itself runs on the pinned Python
    check_lockfiles = None

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
HASH = "sha256:" + "a" * 64
OLD = "2026-09-01T00:00:00Z"


def wheel(name: str, uploaded: str = OLD, url: str | None = None, hash_: str = HASH) -> str:
    url = url or f"https://files.pythonhosted.org/packages/xx/{name}-1.0-py3-none-any.whl"
    return f'{{ url = "{url}", hash = "{hash_}", size = 1, upload-time = "{uploaded}" }}'


@unittest.skipIf(check_lockfiles is None, "needs Python 3.11+ (runs on the pinned Python in CI)")
class CheckLockfilesTests(unittest.TestCase):
    """ENGINEERING §2.5: every lockfile entry comes from the registry, is hashed and is old enough."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "pyproject.toml").write_text('[project]\nname = "coinacct"\nversion = "0.0.0"\n')

    def tearDown(self):
        self._tmp.cleanup()

    def lock(self, *packages: str) -> None:
        body = '\n'.join(packages)
        (self.repo / "uv.lock").write_text(
            'version = 1\nrequires-python = "==3.13.*"\n\n'
            '[[package]]\nname = "coinacct"\nversion = "0.0.0"\nsource = { virtual = "." }\n\n' + body)

    def pkg(self, name: str, source: str = '{ registry = "https://pypi.org/simple" }', wheels: str | None = None) -> str:
        wheels = wheels if wheels is not None else f"[{wheel(name)}]"
        return f'[[package]]\nname = "{name}"\nversion = "1.0"\nsource = {source}\nwheels = {wheels}\n'

    def errors(self) -> str:
        return " | ".join(check_lockfiles.check(self.repo, NOW))

    def test_no_lockfiles_pass(self):
        self.assertEqual(check_lockfiles.check(self.repo, NOW), [])

    def test_a_well_formed_registry_package_passes(self):
        self.lock(self.pkg("pytest"))
        self.assertEqual(check_lockfiles.check(self.repo, NOW), [])

    def test_a_git_or_url_source_fails(self):
        self.lock(self.pkg("evil", source='{ git = "https://example.invalid/evil.git" }'))
        self.assertIn("not the PyPI registry", self.errors())

    def test_a_file_off_the_registry_host_fails(self):
        self.lock(self.pkg("evil", wheels=f"[{wheel('evil', url='https://example.invalid/evil.whl')}]"))
        self.assertIn("not on files.pythonhosted.org", self.errors())

    def test_a_missing_hash_fails(self):
        self.lock(self.pkg("evil", wheels=f"[{wheel('evil', hash_='')}]"))
        self.assertIn("sha256 hash", self.errors())

    def test_a_package_younger_than_the_cooldown_fails(self):
        self.lock(self.pkg("fresh", wheels=f"[{wheel('fresh', uploaded='2026-09-29T00:00:00Z')}]"))
        self.assertIn("less than 7 days ago", self.errors())

    def test_a_missing_upload_time_fails(self):
        w = '{ url = "https://files.pythonhosted.org/packages/xx/a.whl", hash = "' + HASH + '", size = 1 }'
        self.lock(self.pkg("undated", wheels=f"[{w}]"))
        self.assertIn("no upload-time", self.errors())

    def test_an_sdist_only_package_fails(self):
        self.lock(self.pkg("sdistonly", wheels="[]"))
        self.assertIn("no wheels", self.errors())

    def test_a_pnpm_lockfile_fails_closed_for_now(self):
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        self.assertIn("pnpm lockfile check is not implemented", self.errors())

    def test_lifecycle_scripts_pnpmfile_and_config_dependencies_fail(self):
        (self.repo / "frontend").mkdir()
        (self.repo / "frontend" / "package.json").write_text('{"scripts": {"postinstall": "x", "build": "vite"}}')
        (self.repo / ".pnpmfile.cjs").write_text("")
        (self.repo / "pnpm-workspace.yaml").write_text("configDependencies:\n  x: 1\n")
        found = self.errors()
        for needle in ("lifecycle scripts are not allowed: postinstall", ".pnpmfile", "configDependencies"):
            with self.subTest(needle=needle):
                self.assertIn(needle, found)

    def test_cli_exit_codes(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check_lockfiles.main([str(self.repo), "--now", NOW.isoformat()]), 0)
            self.lock(self.pkg("evil", source='{ path = "../evil" }'))
            self.assertEqual(check_lockfiles.main([str(self.repo), "--now", NOW.isoformat()]), 1)

    def test_this_repository_passes(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check_lockfiles.main([str(HERE.parent)]), 0)


if __name__ == "__main__":
    unittest.main()
