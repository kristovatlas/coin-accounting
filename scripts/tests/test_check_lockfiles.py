from __future__ import annotations

import io
import os
import subprocess
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
FILES = "https://files.pythonhosted.org/packages/ab/cd/" + "e" * 60 + "/"  # the real URL shape
OLD = "2026-09-01T00:00:00Z"


def wheel(name: str, uploaded: str = OLD, url: str | None = None, hash_: str = HASH) -> str:
    url = url or f"{FILES}{name}-1.0-py3-none-any.whl"
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
        self.assertIn("not a plain https URL on files.pythonhosted.org", self.errors())

    def test_a_query_or_fragment_cannot_supply_the_file_name(self):
        # PR #81 review: the fetched file is evil-1.0; the name check must not read the suffix.
        base = FILES + "evil-1.0-py3-none-any.whl"
        for url in (base + "#/pytest-1.0-py3-none-any.whl", base + "?/pytest-1.0-py3-none-any.whl",
                    FILES.replace("https:", "http:") + "pytest-1.0-py3-none-any.whl",
                    FILES.replace(".org/", ".org.example.invalid/", 1) + "pytest-1.0-py3-none-any.whl",
                    # A WHATWG parser (uv's) reads `\` as `/` and collapses `..`: this fetches evil-1.0.
                    FILES + "pytest-1.0-py3-none-any.whl\\\\..\\\\evil-1.0-py3-none-any.whl",  # TOML-escaped `\`
                    base.replace("/evil-", "/./evil-"), FILES + "pytest%2D1.0-py3-none-any.whl",
                    FILES + "pytest-1.0-py3-none-any.whl ", "https://files.pythonhosted.org/pytest-1.0-py3-none-any.whl"):
            with self.subTest(url=url):
                self.lock(self.pkg("pytest", wheels=f"[{wheel('pytest', url=url)}]"))
                self.assertIn("not a plain https URL on files.pythonhosted.org", self.errors())

    def test_a_missing_hash_fails(self):
        self.lock(self.pkg("evil", wheels=f"[{wheel('evil', hash_='')}]"))
        self.assertIn("sha256 hash", self.errors())

    def test_a_package_younger_than_the_cooldown_fails(self):
        self.lock(self.pkg("fresh", wheels=f"[{wheel('fresh', uploaded='2026-09-29T00:00:00Z')}]"))
        self.assertIn("less than 7 days ago", self.errors())

    def test_a_missing_upload_time_fails(self):
        w = '{ url = "' + FILES + 'a.whl", hash = "' + HASH + '", size = 1 }'
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
        # #72: "config\u0044ependencies" decodes to the same JSON key. The raw text never contains
        # the plain key, so only the decoded-key check can catch these (PR #81 review).
        cases = {'{"pnpm": {"config\\u0044ependencies": {}}}': "configDependencies are not allowed",
                 '{"package\\u004danager": "pnpm@1"}': "a packageManager field is not allowed",
                 '{"devEngines": {"\\u0070ackageManager": {"name": "pnpm"}}}': "a packageManager field is not allowed"}
        for text, needle in cases.items():
            with self.subTest(package_json=text):
                self.assertNotIn("configDependencies", text)
                self.assertNotIn("packageManager", text)
                (self.repo / "package.json").write_text(text)
                self.assertIn(needle, self.errors())

    def test_workspace_backslashes_and_hook_settings_fail(self):
        cases = {'"config\\x44ependencies": {}\nignorePnpmfile: true\n': "backslashes are not allowed",
                 "pnpmfile: tools/hooks.cjs\nignorePnpmfile: true\n": "the pnpmfile setting",
                 "ignorePnpmfile: true\n'globalPnpmfile': x\n": "the globalPnpmfile setting",
                 "ignorePnpmfile: true\nsharedWorkspaceLockfile: false\n": "the sharedWorkspaceLockfile setting",
                 "{gitBranchLockfile: true}\nignorePnpmfile: true\n": "the gitBranchLockfile setting",
                 "ignorePnpmfile: true\nlockfile: false\n": "the lockfile setting",
                 "ignorePnpmfile: true\nlockfileDir: ../elsewhere\n": "the lockfileDir setting",
                 # PR #81 review: YAML spellings of a key the regex wouldn't see.
                 "? pnpmfile\n: tools/hooks.cjs\nignorePnpmfile: true\n": "explicit keys, tags, anchors",
                 "!!str pnpmfile: tools/hooks.cjs\nignorePnpmfile: true\n": "explicit keys, tags, anchors",
                 "&a sharedWorkspaceLockfile: false\nignorePnpmfile: true\n": "explicit keys, tags, anchors",
                 "k: &a pnpmfile\n*a : tools/hooks.cjs\nignorePnpmfile: true\n": "explicit keys, tags, anchors",
                 "ignorePnpmfile: true\n---\npnpmfile: x\n": "explicit keys, tags, anchors",
                 "ignorePnpmfile: true\nignorePnpmfile: false\n": "exactly once",
                 # PR #81 round 3: a byte-order mark hides the first key from the text checks.
                 "\ufefflockfileDir: ../elsewhere\nignorePnpmfile: true\n": "non-ASCII characters",
                 "ignorePnpmfile: true\nlock\u200bfile: false\n": "non-ASCII characters",
                 "\ufeff# a comment\nlockfileDir: x\nignorePnpmfile: true\n": "non-ASCII characters",
                 "packages:\n  - frontend\n": "`ignorePnpmfile: true` must be set",
                 "ignorePnpmfile: false\n": "`ignorePnpmfile: true` must be set"}
        for text, needle in cases.items():
            with self.subTest(workspace=text):
                (self.repo / "pnpm-workspace.yaml").write_text(text)
                self.assertIn(needle, self.errors())
        (self.repo / "pnpm-workspace.yaml").write_text("# Never load a .pnpmfile (T-602, §2.1).\nignorePnpmfile: true\n")
        self.assertEqual(check_lockfiles.check(self.repo, NOW), [])

    def test_pnpmfiles_and_pnpm_lockfiles_anywhere_fail(self):
        (self.repo / "frontend" / "deep").mkdir(parents=True)
        (self.repo / "frontend" / "deep" / ".PnpmFile.cjs").write_text("")
        (self.repo / "frontend" / "pnpm-lock.yaml").write_text("")
        (self.repo / "pnpm-lock.main.yaml").write_text("")
        (self.repo / "e2e").mkdir(exist_ok=True)
        (self.repo / "e2e" / "PNPM-LOCK.YAML").write_text("")  # PR #81 review: macOS is case-insensitive
        (self.repo / "node_modules" / "x").mkdir(parents=True)
        (self.repo / "node_modules" / "x" / "pnpm-lock.yaml").write_text("")  # skipped: not ours
        found = self.errors()
        for needle in (".PnpmFile.cjs: a .pnpmfile is not allowed", "frontend/pnpm-lock.yaml:", "pnpm-lock.main.yaml:",
                       "e2e/PNPM-LOCK.YAML:"):
            with self.subTest(needle=needle):
                self.assertIn(needle, found)
        self.assertNotIn("node_modules", found)

    def test_a_file_that_does_not_match_its_entry_fails(self):
        # #71: an entry named "pytest" must not point at another project's wheel.
        other = wheel("evil", url=FILES + "evil-1.0-py3-none-any.whl")
        self.lock(self.pkg("pytest", wheels=f"[{other}]"))
        self.assertIn("doesn't match the entry's name and version", self.errors())
        newer = wheel("pytest", url=FILES + "pytest-9.9-py3-none-any.whl")
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



class WrapperTests(unittest.TestCase):
    """PR #81 review: scripts/check-lockfiles runs the pinned python3 only once it verifies."""

    def run_wrapper(self, verify_exit: int, pinned_first: bool = False) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / ".toolchain" / "bin").mkdir(parents=True)
            wrapper = root / "scripts" / "check-lockfiles"
            wrapper.write_text((HERE / "check-lockfiles").read_text())
            (root / "scripts" / "toolchain.py").write_text(f"import sys\nsys.exit({verify_exit})\n")
            (root / "scripts" / "check_lockfiles.py").write_text("import sys\nprint(sys.executable)\n")
            pinned = root / ".toolchain" / "bin" / "python3"
            pinned.write_text("#!/bin/sh\necho PINNED\n")
            for f in (wrapper, pinned):
                f.chmod(0o755)
            path = os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin"
            if pinned_first:  # PR #81 round 2: the pinned python3 must not verify itself
                path = str(pinned.parent) + os.pathsep + path
            out = subprocess.run(["sh", str(wrapper)], capture_output=True, text=True, check=True,
                                 env={**os.environ, "PATH": path})
            return out.stdout + out.stderr

    def test_a_verified_pinned_python_is_used(self):
        self.assertIn("PINNED", self.run_wrapper(0))

    def test_an_unverified_pinned_python_is_not_run(self):
        out = self.run_wrapper(1)
        self.assertNotIn("PINNED", out)
        self.assertIn("did not verify", out)

    def test_the_pinned_python_never_verifies_itself(self):
        # With .toolchain/bin first on PATH, a bare `python3` would be the pinned binary: it could
        # "verify" itself and then run the check. The wrapper skips that directory.
        out = self.run_wrapper(1, pinned_first=True)
        self.assertNotIn("PINNED", out)
        self.assertIn("did not verify", out)
        self.assertIn("PINNED", self.run_wrapper(0, pinned_first=True))


class MakefileInterpreterTests(unittest.TestCase):
    """PR #81 round 3: the Makefile's host python3 (SYS_PYTHON), which runs the toolchain verifier,
    is never the pinned python3, even with .toolchain/bin first on the caller's PATH."""

    def test_sys_python_skips_the_pinned_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text((HERE.parent / "Makefile").read_text())
            (root / "show.mk").write_text("show-sys-python:\n\t@echo $(SYS_PYTHON)\n")
            pinned_dir = root / ".toolchain" / "bin"
            pinned_dir.mkdir(parents=True)
            pinned = pinned_dir / "python3"
            pinned.write_text("#!/bin/sh\necho PINNED\n")
            pinned.chmod(0o755)
            host_dir = os.path.dirname(sys.executable)
            for path in (f"{pinned_dir}{os.pathsep}{host_dir}{os.pathsep}/usr/bin:/bin",
                         f"{host_dir}{os.pathsep}/usr/bin:/bin"):
                with self.subTest(PATH=path):
                    out = subprocess.run(["make", "-s", "-f", "Makefile", "-f", "show.mk", "show-sys-python"],
                                         cwd=root, capture_output=True, text=True, check=True,
                                         env={**os.environ, "PATH": path}).stdout.strip()
                    self.assertTrue(out.endswith("/python3"), out)
                    self.assertFalse(out.startswith(str(pinned_dir)), out)
                    self.assertFalse(os.path.samefile(out, pinned), out)

if __name__ == "__main__":
    unittest.main()
