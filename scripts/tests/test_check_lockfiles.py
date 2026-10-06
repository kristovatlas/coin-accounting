from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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
RES = "    resolution: {integrity: sha512-" + "A" * 86 + "==}\n"
PNPM_BODY = ("importers:\n\n  frontend:\n    dependencies:\n      react:\n        specifier: ^19.3.0\n"
             "        version: 19.3.0\n\npackages:\n\n  '@scope/pkg@1.0.0':\n" + RES
             + "    engines: {node: '>=20'}\n\n  react@19.3.0:\n" + RES)
PNPM_SNAPSHOTS = "\nsnapshots:\n\n  '@scope/pkg@1.0.0': {}\n\n  react@19.3.0: {}\n"
PNPM_TIMES = {"@scope/pkg@1.0.0": OLD, "react@19.3.0": OLD}


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

    # --- pnpm-lock.yaml (ENGINEERING §2.5) ---------------------------------------------------------

    def pnpm(self, packages: str = "", times: dict | None = None, head: str = "lockfileVersion: '9.0'\n") -> None:
        default = PNPM_BODY + packages + PNPM_SNAPSHOTS
        (self.repo / "pnpm-lock.yaml").write_text(head + "\nsettings:\n  autoInstallPeers: true\n"
                                                 "  excludeLinksFromLockfile: false\n\n" + default)
        if times is not None:
            (self.repo / "pnpm-lock.times.json").write_text(json.dumps(times))

    def test_a_registry_pnpm_lockfile_with_old_packages_passes(self):
        self.pnpm(times=PNPM_TIMES)
        self.assertEqual(check_lockfiles.check(self.repo, NOW), [])

    def test_a_package_younger_than_the_cooldown_fails_on_the_npm_side(self):
        self.pnpm(times={**PNPM_TIMES, "react@19.3.0": "2026-09-28T00:00:00Z"})
        self.assertIn("react@19.3.0 was published 2026-09-28, less than 7 days ago", self.errors())

    def test_a_missing_publish_time_or_times_file_fails(self):
        self.pnpm(times={"@scope/pkg@1.0.0": OLD})
        self.assertIn("no publish time for react@19.3.0", self.errors())
        (self.repo / "pnpm-lock.times.json").unlink()
        self.assertIn("pnpm-lock.times.json is missing", self.errors())

    def test_non_registry_sources_fail(self):
        for bad in ("    resolution: {tarball: https://evil.example/x.tgz}\n",
                    "    resolution: {type: git, repo: https://github.com/a/b, commit: abc}\n",
                    "    resolution: {directory: ../x, type: directory}\n"):
            with self.subTest(bad=bad):
                self.pnpm("\n  evil@1.0.0:\n" + bad, times={**PNPM_TIMES, "evil@1.0.0": OLD})
                errors = self.errors()
                self.assertIn("a source other than the npm registry", errors)
                self.assertIn("evil@1.0.0 must have exactly one registry resolution", errors)

    def test_link_and_file_versions_in_importers_fail(self):
        for spec in ("link:../x", "file:../x.tgz", "workspace:*", "npm:other@1", "github:a/b"):
            with self.subTest(spec=spec):
                (self.repo / "pnpm-lock.yaml").write_text(
                    "lockfileVersion: '9.0'\n\nimporters:\n\n  frontend:\n    dependencies:\n"
                    f"      x:\n        specifier: {spec}\n        version: {spec}\n")
                self.assertIn("a source other than the npm registry", self.errors())

    def test_a_weak_or_extra_integrity_fails(self):
        for res in ("{integrity: sha1-" + "A" * 27 + "=}", "{integrity: sha512-short==}",
                    "{integrity: sha512-" + "A" * 86 + "==, extra: 1}"):
            with self.subTest(res=res):
                self.pnpm("\n  x@1.0.0:\n    resolution: " + res + "\n", times={**PNPM_TIMES, "x@1.0.0": OLD})
                self.assertIn("a resolution must be exactly {integrity: sha512-...}", self.errors())

    def test_a_package_without_a_resolution_or_with_two_fails(self):
        self.pnpm("\n  x@1.0.0:\n    engines: {node: '>=20'}\n", times={**PNPM_TIMES, "x@1.0.0": OLD})
        self.assertIn("x@1.0.0 must have exactly one registry resolution", self.errors())
        self.pnpm("\n  x@1.0.0:\n" + RES + RES, times={**PNPM_TIMES, "x@1.0.0": OLD})
        self.assertIn("x@1.0.0 must have exactly one registry resolution", self.errors())

    def test_unknown_top_level_keys_settings_and_versions_fail(self):
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n\noverrides:\n  x: 1.0.0\n")
        self.assertIn("top-level key 'overrides' is not allowed", self.errors())
        self.pnpm(times=PNPM_TIMES, head="lockfileVersion: '6.0'\n")
        self.assertIn("lockfileVersion: '9.0'", self.errors())
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n\nsettings:\n  autoInstallPeers: false\n")
        self.assertIn("unexpected setting", self.errors())

    def test_a_second_document_anchors_tags_and_non_ascii_fail(self):
        for extra in ("---\nlockfileVersion: '9.0'\n", "\n  x@1.0.0: &a\n", "\n  y@1.0.0: !!str\n"):
            with self.subTest(extra=extra):
                self.pnpm(extra, times=PNPM_TIMES)
                self.assertIn("only one plain YAML document is allowed", self.errors())
        (self.repo / "pnpm-lock.yaml").write_bytes("\ufefflockfileVersion: '9.0'\n".encode())
        self.assertIn("non-ASCII content", self.errors())

    def test_a_resolution_outside_packages_fails(self):
        (self.repo / "pnpm-lock.yaml").write_text(
            "lockfileVersion: '9.0'\n\nsnapshots:\n\n  react@19.3.0:\n" + RES)
        self.assertIn("a resolution outside the packages section", self.errors())

    def test_snapshots_and_importers_are_read_strictly_and_cross_checked(self):
        # PR #136 round 2: pnpm merges a snapshot's fields into its package, so `version:` there
        # would redirect the fetch; every reference must name a checked packages entry.
        lock = (self.repo / "pnpm-lock.yaml")
        self.pnpm(times=PNPM_TIMES)
        good = lock.read_text()
        for old, new, message in (
            ("  react@19.3.0: {}\n", "  react@19.3.0:\n    version: 19.3.1\n", "unexpected line in a snapshot"),
            ("  react@19.3.0: {}\n", "  react@19.3.0: {version: 6.6.6}\n", "unexpected snapshot entry"),
            ("  react@19.3.0: {}\n", "  react@19.3.0:\n    name: evil\n", "unexpected line in a snapshot"),
            ("  react@19.3.0: {}\n", "  react@19.3.0: {}\n\n  fresh@9.9.9: {}\n", "the snapshot fresh@9.9.9 has no entry in packages"),
            ("  react@19.3.0: {}\n", "  react@19.3.0:\n    dependencies:\n      fresh: 9.9.9\n", "fresh@9.9.9 is referenced but has no entry"),
            ("        version: 19.3.0\n", "        version: 19.3.1\n", "react@19.3.1 is referenced but has no entry"),
            ("        version: 19.3.0\n", "        version: 19.3.0\n        resolved: x\n", "unexpected line in an importer"),
            ("  '@scope/pkg@1.0.0': {}\n\n", "", "@scope/pkg@1.0.0 has no snapshot"),
        ):
            with self.subTest(new=new):
                self.assertIn(old, good)
                lock.write_text(good.replace(old, new, 1))
                self.assertIn(message, self.errors())

    def test_merge_keys_and_empty_packages_cannot_bypass_the_checks(self):
        # PR #136 round 2: a merge key under importers/snapshots, or snapshots and importers with no
        # packages at all, must not leave entries unchecked.
        lock = self.repo / "pnpm-lock.yaml"
        self.pnpm(times=PNPM_TIMES)
        good = lock.read_text()
        lock.write_text(good.replace("  react@19.3.0: {}\n", "  react@19.3.0:\n    <<: {dependencies: {x: 1.0.0}}\n", 1))
        self.assertIn("unexpected line in a snapshot", self.errors())
        lock.write_text(good.replace("  frontend:\n", "  frontend:\n    <<: {dependencies: {x: 1.0.0}}\n", 1))
        self.assertIn("unexpected line in an importer", self.errors())
        no_packages = good.split("\npackages:\n")[0] + "\nsnapshots:\n\n  react@19.3.0: {}\n"
        lock.write_text(no_packages)
        errors = self.errors()
        self.assertIn("the snapshot react@19.3.0 has no entry in packages", errors)
        self.assertIn("react@19.3.0 is referenced but has no entry in packages", errors)

    def test_an_alias_to_another_package_fails(self):
        # PR #136 round 3: `version: evil@1.0.0` would install evil as react.
        lock = self.repo / "pnpm-lock.yaml"
        extra = "\n  evil@1.0.0:\n" + RES
        self.pnpm(extra, times={**PNPM_TIMES, "evil@1.0.0": OLD})
        good = lock.read_text().replace("  react@19.3.0: {}\n", "  react@19.3.0: {}\n\n  evil@1.0.0: {}\n", 1)
        lock.write_text(good.replace("        version: 19.3.0\n", "        version: evil@1.0.0\n", 1))
        self.assertIn("unexpected line in an importer", self.errors())
        lock.write_text(good.replace("  react@19.3.0: {}\n", "  react@19.3.0:\n    dependencies:\n      react: evil@1.0.0\n", 1))
        self.assertIn("unexpected line in a snapshot", self.errors())

    def test_open_flow_values_quotes_and_repeated_sections_fail(self):
        # PR #136 round 3: a value left open could swallow the next line, so YAML and the check would
        # disagree about which lines belong to which entry.
        for bad, message in (("    engines: {node: [x]\n", "engines must be a one-line value"),
                             ("    os: [linux, {a: 1}]\n", "os must be a one-line value"),
                             ("    deprecated: 'open\n", "deprecated must be a plain one-line value")):
            with self.subTest(bad=bad):
                self.pnpm("\n  x@1.0.0:\n" + RES + bad, times={**PNPM_TIMES, "x@1.0.0": OLD})
                self.assertIn(message, self.errors())
        self.pnpm(times=PNPM_TIMES)
        lock = self.repo / "pnpm-lock.yaml"
        lock.write_text(lock.read_text() + "\nsettings:\n  autoInstallPeers: true\n")
        self.assertIn("the settings section appears twice", self.errors())

    def test_a_malformed_publish_time_makes_the_check_exit_1(self):
        self.pnpm(times={**PNPM_TIMES, "react@19.3.0": "last tuesday"})
        with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(check_lockfiles.main([str(self.repo), "--now", "2026-10-01T00:00:00+00:00"]), 1)

    def test_escaped_or_double_quoted_scalars_fail(self):
        # PR #136 review: "li\\u006ek:../x" decodes to link:../x, which the raw-text source scan can't see.
        for spec in ('"li\\u006ek:../x"', '"file:../x.tgz"', "'a\\b'"):
            with self.subTest(spec=spec):
                (self.repo / "pnpm-lock.yaml").write_text(
                    "lockfileVersion: '9.0'\n\nimporters:\n\n  frontend:\n    dependencies:\n"
                    f"      x:\n        specifier: ^1.0.0\n        version: {spec}\n")
                self.assertIn("double quotes and backslashes are not allowed", self.errors())

    def test_an_empty_lock_with_declared_js_dependencies_fails(self):
        (self.repo / "frontend").mkdir()
        (self.repo / "frontend" / "package.json").write_text('{"name": "f", "dependencies": {"react": "19.3.0"}}')
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n\nimporters:\n\npackages:\n\nsnapshots:\n")
        self.assertIn("pnpm-lock.yaml has no packages, but frontend/package.json declare dependencies", self.errors())

    def test_declared_js_dependencies_without_a_lockfile_fail(self):
        (self.repo / "frontend").mkdir()
        (self.repo / "frontend" / "package.json").write_text('{"name": "f", "dependencies": {"react": "19.3.0"}}')
        self.assertIn("pnpm-lock.yaml is missing, but frontend/package.json declare dependencies", self.errors())
        (self.repo / "frontend" / "package.json").write_text('{"name": "f", "dependencies": {}}')
        self.assertEqual(check_lockfiles.check(self.repo, NOW), [])

    def test_a_flow_style_section_cannot_hide_packages_or_settings(self):
        # PR #136 review: `packages: {...}` left the package list empty, so no check ran at all.
        evil = "{'evil@1.0.0': {resolution: {integrity: sha1-" + "A" * 27 + "=}}}"
        for key, value in (("packages", evil), ("settings", "{autoInstallPeers: false}"),
                           ("importers", "{}"), ("snapshots", "{}")):
            with self.subTest(key=key):
                (self.repo / "pnpm-lock.yaml").write_text(f"lockfileVersion: '9.0'\n\n{key}: {value}\n")
                self.assertIn(f"the {key} section must be written as a block", self.errors())

    def test_a_re_indented_packages_section_cannot_hide_entries(self):
        # PR #136 review: package keys at 4 spaces (fields at 6) were never recorded.
        body = "\n    evil@1.0.0:\n      resolution: {integrity: sha1-" + "A" * 27 + "=}\n"
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n\npackages:\n" + body)
        self.assertIn("unexpected line in a package entry", self.errors())

    def test_only_known_package_fields_are_allowed(self):
        # `name:`/`version:` could make pnpm fetch something other than the key the times file checks.
        for bad in ("    name: evil\n", "    version: 0.0.1\n", "    tarball: x\n", "    id: x\n",
                    "    hasBin: {a: 1}\n", "    deprecated: |\n", "    peerDependencies: {a: 1}\n",
                    "     engines: {node: '>=20'}\n", "      stray: 1\n"):
            with self.subTest(bad=bad):
                self.pnpm("\n  x@1.0.0:\n" + RES + bad, times={**PNPM_TIMES, "x@1.0.0": OLD})
                self.assertIn("unexpected line in a package entry" if "stray" in bad or "     engines" in bad
                              or "name" in bad or "version" in bad or "tarball" in bad or "id:" in bad
                              else "must be", self.errors())

    def test_peer_dependency_blocks_and_plain_fields_pass(self):
        extra = ("\n  x@1.0.0:\n" + RES + "    hasBin: true\n    deprecated: use y instead\n"
                 "    os: [linux]\n    cpu: [x64]\n    libc: [glibc]\n"
                 "    peerDependencies:\n      react: ^19.0.0\n      '@types/node': '*'\n"
                 "    peerDependenciesMeta:\n      '@types/node':\n        optional: true\n")
        self.pnpm(extra, times={**PNPM_TIMES, "x@1.0.0": OLD})
        with open(self.repo / "pnpm-lock.yaml", "a") as lock:
            lock.write("\n  x@1.0.0:\n    dependencies:\n      react: 19.3.0\n    transitivePeerDependencies:\n"
                       "      - '@types/node'\n")
        self.assertEqual(check_lockfiles.check(self.repo, NOW), [])

    def test_the_committed_lockfile_passes(self):
        # The real pnpm 12 output must keep passing the stricter read (fails if the check is too strict).
        repo = HERE.parent
        if not (repo / "pnpm-lock.yaml").exists():
            self.skipTest("no pnpm-lock.yaml in this checkout")
        self.assertEqual([e for e in check_lockfiles.check_pnpm_lock(repo, NOW) if "less than 7 days" not in e], [])

    def test_a_duplicate_package_entry_fails(self):
        self.pnpm("\n  react@19.3.0:\n" + RES, times=PNPM_TIMES)
        self.assertIn("react@19.3.0 appears twice", self.errors())

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
        for needle in (".PnpmFile.cjs: a .pnpmfile is not allowed", "frontend/pnpm-lock.yaml: only pnpm-lock.yaml",
                       "pnpm-lock.main.yaml:", "e2e/PNPM-LOCK.YAML:"):
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
