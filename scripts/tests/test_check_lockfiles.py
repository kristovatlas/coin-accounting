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

    def test_every_lifecycle_and_pnpm_hook_name_fails(self):
        # PR #63 review: pnpm's own `pnpm:devPreinstall` hook runs before a local install.
        for name in sorted(check_lockfiles.LIFECYCLE) + ["pnpm:devPreinstall", "pnpm:someFutureHook"]:
            with self.subTest(script=name):
                (self.repo / "package.json").write_text('{"scripts": {"%s": "x", "build": "vite"}}' % name)
                self.assertIn(f"lifecycle scripts are not allowed: {name}", self.errors())

    def test_ordinary_scripts_pass(self):
        (self.repo / "package.json").write_text('{"scripts": {"build": "vite", "test": "vitest", "lint": "eslint"}}')
        self.assertEqual(check_lockfiles.check(self.repo, NOW), [])

    def test_config_dependencies_fail_in_any_spelling(self):
        # PR #63 review: quoted and flow-style YAML keys are the same pnpm setting.
        for text in ('"configDependencies":\n  x: 1\n', "'configDependencies': {x: 1}\n",
                     "{configDependencies: {x: 1}}\n", "packages:\n  - frontend\n  configDependencies : {}\n"):
            with self.subTest(workspace=text):
                (self.repo / "pnpm-workspace.yaml").write_text(text)
                self.assertIn("pnpm-workspace.yaml: configDependencies are not allowed", self.errors())
        (self.repo / "pnpm-workspace.yaml").unlink()
        (self.repo / "package.json").write_text('{"pnpm": {"configDependencies": {"x": "1"}}}')
        self.assertIn("package.json: configDependencies are not allowed", self.errors())

    def test_a_package_manager_field_fails(self):
        # #51: pnpm 12 resolves a packageManager pin against the registry on every command.
        for text in ('{"packageManager": "pnpm@12.5.1"}', '{"devEngines": {"packageManager": {"name": "pnpm"}}}'):
            with self.subTest(package_json=text):
                (self.repo / "package.json").write_text(text)
                self.assertIn("a packageManager field is not allowed", self.errors())

    def test_escaped_json_keys_are_decoded(self):
        # #72: "configDependencies" is the same JSON key.
        for key in (r"configDependencies", r"packageManager"):
            with self.subTest(key=key):
                (self.repo / "package.json").write_text('{"pnpm": {"%s": {}}}' % key)
                self.assertRegex(self.errors(), "configDependencies are not allowed|packageManager field is not allowed")

    def test_workspace_backslashes_and_hook_settings_fail(self):
        cases = {'"config\\x44ependencies": {}\nignorePnpmfile: true\n': "backslashes are not allowed",
                 "pnpmfile: tools/hooks.cjs\nignorePnpmfile: true\n": "the pnpmfile setting",
                 "ignorePnpmfile: true\n'globalPnpmfile': x\n": "the globalPnpmfile setting",
                 "ignorePnpmfile: true\nsharedWorkspaceLockfile: false\n": "the sharedWorkspaceLockfile setting",
                 "{gitBranchLockfile: true}\nignorePnpmfile: true\n": "the gitBranchLockfile setting",
                 "packages:\n  - frontend\n": "`ignorePnpmfile: true` must be set",
                 "ignorePnpmfile: false\n": "`ignorePnpmfile: true` must be set"}
        for text, needle in cases.items():
            with self.subTest(workspace=text):
                (self.repo / "pnpm-workspace.yaml").write_text(text)
                self.assertIn(needle, self.errors())
        (self.repo / "pnpm-workspace.yaml").write_text("# Never load a .pnpmfile (T-602).\nignorePnpmfile: true\n")
        self.assertEqual(check_lockfiles.check(self.repo, NOW), [])

    def test_pnpmfiles_and_pnpm_lockfiles_anywhere_fail(self):
        (self.repo / "frontend" / "deep").mkdir(parents=True)
        (self.repo / "frontend" / "deep" / ".PnpmFile.cjs").write_text("")
        (self.repo / "frontend" / "pnpm-lock.yaml").write_text("")
        (self.repo / "pnpm-lock.main.yaml").write_text("")
        (self.repo / "node_modules" / "x").mkdir(parents=True)
        (self.repo / "node_modules" / "x" / "pnpm-lock.yaml").write_text("")  # skipped: not ours
        found = self.errors()
        for needle in (".PnpmFile.cjs: a .pnpmfile is not allowed", "frontend/pnpm-lock.yaml:", "pnpm-lock.main.yaml:"):
            with self.subTest(needle=needle):
                self.assertIn(needle, found)
        self.assertNotIn("node_modules", found)

    def test_a_file_that_does_not_match_its_entry_fails(self):
        # #71: an entry named "pytest" must not point at another project's wheel.
        other = wheel("evil", url="https://files.pythonhosted.org/packages/xx/evil-1.0-py3-none-any.whl")
        self.lock(self.pkg("pytest", wheels=f"[{other}]"))
        self.assertIn("doesn't match the entry's name and version", self.errors())
        newer = wheel("pytest", url="https://files.pythonhosted.org/packages/xx/pytest-9.9-py3-none-any.whl")
        self.lock(self.pkg("pytest", wheels=f"[{newer}]"))
        self.assertIn("doesn't match the entry's name and version", self.errors())

    def test_declared_dependencies_without_a_lockfile_fail(self):
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "coinacct"\nversion = "0.0.0"\n[dependency-groups]\ndev = ["pytest==9.1.1"]\n')
        self.assertIn("there is no uv.lock", self.errors())

    def test_malformed_input_fails_with_a_message_not_a_traceback(self):
        with redirect_stdout(io.StringIO()):
            (self.repo / "package.json").write_text('{"scripts": "postinstall"}')
            self.assertIn('"scripts" must be an object', self.errors())
            (self.repo / "package.json").write_text("[]")
            self.assertIn("not a JSON object", self.errors())
            (self.repo / "package.json").unlink()
            self.lock(self.pkg("odd", wheels='["not-a-table"]'))
            self.assertIn("malformed file entry", self.errors())

    def test_a_naive_now_is_read_as_utc(self):
        with redirect_stdout(io.StringIO()):
            self.lock(self.pkg("pytest"))
            self.assertEqual(check_lockfiles.main([str(self.repo), "--now", "2026-10-01T00:00:00"]), 0)

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
