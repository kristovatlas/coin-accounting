from __future__ import annotations

import hashlib
import io
import os
import subprocess
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import check_adrs  # noqa: E402
import toolchain  # noqa: E402

ADR = """---
status: accepted
date: 2026-09-28
deciders: human
{extra}---

# {num}: {title}

Body.
"""


def make_repo(root: Path, arch: str | None, adrs: list[tuple[str, str, str]], index: bool = True) -> None:
    adr_dir = root / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    rows = []
    for num, title, extra in adrs:
        (adr_dir / f"{num}-x.md").write_text(ADR.format(num=num, title=title, extra=extra))
        rows.append(f"| [{num}]({num}-x.md) | {title} |")
    if index:
        (adr_dir / "README.md").write_text("| ADR | Title |\n|---|---|\n" + "\n".join(rows) + "\n")
    if arch is not None:
        (root / "docs" / "architecture.md").write_text(arch)


class CheckAdrsTests(unittest.TestCase):
    def check(self, root: Path) -> list[str]:
        errors, metas, titles = check_adrs.check_files(root / "docs" / "adr")
        return errors + check_adrs.check_index(root / "docs" / "adr", titles) + check_adrs.check_hash(root, metas)

    def test_valid_repo_passes(self):
        with tempfile.TemporaryDirectory() as d:
            h = hashlib.sha256(b"arch").hexdigest()
            make_repo(Path(d), "arch", [("0001", "One", ""), ("0002", "Two", f"architecture_sha256: {h}\n")])
            self.assertEqual(self.check(Path(d)), [])

    def test_hash_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(Path(d), "changed", [("0001", "One", "architecture_sha256: " + "0" * 64 + "\n")])
            errors = self.check(Path(d))
            self.assertEqual(len(errors), 1)
            self.assertIn("does not match ADR 0001", errors[0])

    def test_highest_numbered_hash_is_the_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            new = hashlib.sha256(b"v2").hexdigest()
            make_repo(Path(d), "v2", [("0001", "A", "architecture_sha256: " + "0" * 64 + "\n"),
                                      ("0005", "B", f"architecture_sha256: {new}\n")])
            self.assertEqual(self.check(Path(d)), [])

    def test_index_must_match_files(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(Path(d), None, [("0001", "One", "")], index=False)
            (Path(d) / "docs/adr/README.md").write_text("| [0001](0001-x.md) | Wrong |\n| [0002](0002-x.md) | Ghost |\n")
            errors = self.check(Path(d))
            self.assertTrue(any("title of ADR 0001" in e for e in errors))
            self.assertTrue(any("lists ADR 0002" in e for e in errors))

    def test_invalid_status_fails(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(Path(d), None, [("0001", "One", "")])
            p = Path(d) / "docs/adr/0001-x.md"
            p.write_text(p.read_text().replace("status: accepted", "status: maybe"))
            self.assertTrue(any("invalid status" in e for e in self.check(Path(d))))


class AdrBaseComparisonTests(unittest.TestCase):
    """ADR 0001 immutability against the base branch (check_against_base)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.h = hashlib.sha256(b"arch").hexdigest()
        make_repo(self.root, "arch", [("0001", "One", ""), ("0002", "Two", f"architecture_sha256: {self.h}\n")])
        self.git("init", "-q")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        # Isolated from the developer's git config (commit signing, hooks paths, …).
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True,
                              env=env).stdout

    def check(self):
        errors, metas, _ = check_adrs.check_files(self.root / "docs" / "adr")
        errors += check_adrs.duplicate_numbers(self.root / "docs" / "adr")
        return errors + check_adrs.check_against_base(self.root, self.base, metas)

    def adr(self, num):
        return self.root / "docs" / "adr" / f"{num}-x.md"

    def test_unchanged_passes(self):
        self.assertEqual(self.check(), [])

    def test_body_change_fails(self):
        self.adr("0001").write_text(self.adr("0001").read_text().replace("Body.", "Rewritten."))
        self.assertTrue(any("may only change its status line" in e for e in self.check()))

    def test_deleted_adr_fails(self):
        self.adr("0001").unlink()
        self.assertTrue(any("was removed" in e for e in self.check()))

    def test_renamed_adr_fails(self):
        self.adr("0001").rename(self.root / "docs" / "adr" / "0001-renamed.md")
        self.assertTrue(any("was renamed" in e for e in self.check()))

    def test_duplicate_number_fails(self):
        (self.root / "docs" / "adr" / "0001-copy.md").write_text(self.adr("0001").read_text())
        self.assertTrue(any("used by several files" in e for e in self.check()))

    def test_status_back_to_proposed_fails(self):
        self.adr("0001").write_text(self.adr("0001").read_text().replace("status: accepted", "status: proposed"))
        self.assertTrue(any("may not change from 'accepted' to 'proposed'" in e for e in self.check()))

    def test_deprecated_is_allowed(self):
        self.adr("0001").write_text(self.adr("0001").read_text().replace("status: accepted", "status: deprecated"))
        self.assertEqual(self.check(), [])

    def test_superseded_needs_a_successor_pointing_back(self):
        self.adr("0001").write_text(self.adr("0001").read_text().replace("status: accepted", "status: superseded by 0003"))
        self.assertTrue(any("may not change" in e for e in self.check()))
        (self.root / "docs" / "adr" / "0003-x.md").write_text(ADR.format(num="0003", title="Three", extra="supersedes: 0001\n"))
        readme = self.root / "docs" / "adr" / "README.md"
        readme.write_text(readme.read_text() + "| [0003](0003-x.md) | Three |\n")
        self.assertEqual(self.check(), [])

    def test_architecture_change_needs_one_new_hashed_adr(self):
        (self.root / "docs" / "architecture.md").write_text("arch v2")
        self.assertTrue(any("add exactly one new ADR" in e for e in self.check()))
        h2 = hashlib.sha256(b"arch v2").hexdigest()
        (self.root / "docs" / "adr" / "0003-x.md").write_text(
            ADR.format(num="0003", title="Three", extra=f"architecture_sha256: {h2}\n"))
        self.assertEqual([e for e in self.check() if "exactly one new ADR" in e], [])


class ToolchainTests(unittest.TestCase):
    def test_lock_has_every_tool_for_every_platform(self):
        lock = toolchain.load_lock()
        for name in ("sfw", "pnpm", "uv", "node", "python", "bitcoind"):
            for key in ("linux-x86_64", "linux-arm64", "darwin-arm64", "darwin-x86_64"):
                with self.subTest(tool=name, platform=key):
                    entry = lock[name][key]
                    self.assertTrue(entry["url"].startswith("https://"))
                    if "sha256" in entry:
                        self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                    else:
                        self.assertRegex(entry["integrity"], r"^sha512-[A-Za-z0-9+/]{86}==$")

    def test_pnpm_comes_from_the_npm_registry_pinned_by_integrity(self):
        # User decision in PR #7: publisher-provided integrity instead of a GitHub digest.
        for key, entry in toolchain.load_lock()["pnpm"].items():
            if key in ("version", "source"):
                continue
            self.assertTrue(entry["url"].startswith("https://registry.npmjs.org/pnpm/-/"))
            self.assertEqual(entry["kind"], "npm-tgz")

    def test_sha256_mismatch_is_a_hard_error(self):
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x", "sha256": "0" * 64,
                                                  "kind": "binary", "bin": "x"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(b"evil")):
            with self.assertRaisesRegex(toolchain.ToolchainError, "hash mismatch"):
                toolchain.install_tool("x", spec, "linux-x86_64")
            self.assertFalse((Path(d) / "x").exists())

    def test_matching_binary_is_installed_executable(self):
        data = b"#!/bin/sh\necho ok\n"
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                  "sha256": hashlib.sha256(data).hexdigest(),
                                                  "kind": "binary", "bin": "tool"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(data)):
            path = toolchain.install_tool("x", spec, "linux-x86_64")
            self.assertEqual(path.read_bytes(), data)
            self.assertTrue(path.stat().st_mode & 0o100)

    def test_tar_with_path_traversal_is_refused(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../../escape")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "a.tar.gz"
            archive.write_bytes(buf.getvalue())
            target = Path(d) / "out"
            target.mkdir()
            with self.assertRaises(tarfile.TarError):
                toolchain.safe_extract(archive, target)

    def wrapped_tar(self, inner: str, data: bytes) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(inner)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def test_second_install_of_wrapped_tarball_returns_the_real_binary(self):
        # PR #7 review: the cache hit used to return tool_dir/entry["bin"], which doesn't exist for
        # tarballs that wrap their content in a top-level directory (uv, Node).
        blob = self.wrapped_tar("uv-x86_64-unknown-linux-gnu/uv", b"#!/bin/sh\n")
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                  "sha256": hashlib.sha256(blob).hexdigest(), "kind": "tar", "bin": "uv"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            first = toolchain.install_tool("uv", spec, "linux-x86_64")
            second = toolchain.install_tool("uv", spec, "linux-x86_64")
            self.assertEqual(first, second)
            self.assertTrue(second.is_file())

    def test_modified_binary_is_reinstalled_on_the_next_run(self):
        data = b"#!/bin/sh\n"
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                  "sha256": hashlib.sha256(data).hexdigest(), "kind": "binary", "bin": "sfw"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(data)):
            path = toolchain.install_tool("sfw", spec, "linux-x86_64")
            path.write_bytes(b"tampered")
            again = toolchain.install_tool("sfw", spec, "linux-x86_64")
            self.assertEqual(again.read_bytes(), data)

    def test_verify_accepts_the_pinned_binary_and_rejects_substitutes(self):
        data = b"#!/bin/sh\n"
        lock = {"sfw": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/x",
                                                         "sha256": hashlib.sha256(data).hexdigest(),
                                                         "kind": "binary", "bin": "sfw"}}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(data)):
            with self.assertRaisesRegex(toolchain.ToolchainError, "not installed"):
                toolchain.verify_tool("sfw", lock, "linux-x86_64")
            path = toolchain.install_tool("sfw", lock["sfw"], "linux-x86_64")
            toolchain.link(path, "sfw")
            self.assertEqual(toolchain.verify_tool("sfw", lock, "linux-x86_64"), path.resolve())
            # A symlink pointing somewhere else (e.g. /usr/bin/env) is refused.
            (Path(d) / "bin" / "sfw").unlink()
            (Path(d) / "bin" / "sfw").symlink_to("/usr/bin/env")
            with self.assertRaisesRegex(toolchain.ToolchainError, "does not point"):
                toolchain.verify_tool("sfw", lock, "linux-x86_64")
            toolchain.link(path, "sfw")
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(toolchain.ToolchainError, "modified"):
                toolchain.verify_tool("sfw", lock, "linux-x86_64")

    def test_extraction_without_tarfile_filters_still_refuses_unsafe_members(self):
        # Older host Pythons (e.g. macOS's 3.9) have no tarfile.data_filter (PR #7 review, round 2).
        def archive_with(name, linkname=None):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo(name)
                if linkname:
                    info.type, info.linkname = tarfile.SYMTYPE, linkname
                    tar.addfile(info)
                else:
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"x"))
            return buf.getvalue()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(toolchain, "HAS_TAR_FILTERS", False):
            for name, link in (("../escape", None), ("/abs", None), ("ok/link", "/etc/passwd"), ("ok/l2", "../../x")):
                with self.subTest(name=name):
                    archive = Path(d) / "a.tar.gz"
                    archive.write_bytes(archive_with(name, link))
                    target = Path(d) / f"out-{abs(hash(name))}"
                    target.mkdir()
                    with self.assertRaises(tarfile.TarError):
                        toolchain.safe_extract(archive, target)
            archive = Path(d) / "good.tar.gz"
            archive.write_bytes(archive_with("pkg/bin/tool"))
            target = Path(d) / "good"
            target.mkdir()
            toolchain.safe_extract(archive, target)
            self.assertTrue((target / "pkg" / "bin" / "tool").is_file())

    def test_linux_arm64_is_a_supported_platform(self):
        with mock.patch.object(toolchain.platform, "system", return_value="Linux"), \
                mock.patch.object(toolchain.platform, "machine", return_value="aarch64"):
            self.assertEqual(toolchain.platform_key(), "linux-arm64")

    def npm_tarball(self, data: bytes) -> tuple[bytes, str]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("package/pnpm")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        blob = buf.getvalue()
        import base64
        return blob, "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()

    def test_npm_tarball_is_checked_by_integrity_and_wrapped_with_pinned_node(self):
        blob, integrity = self.npm_tarball(b"console.log('pnpm')\n")
        spec = {"version": "1", "linux-x86_64": {"url": "https://example.invalid/p.tgz", "integrity": integrity,
                                                  "kind": "npm-tgz", "bin": "package/pnpm"}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            path = toolchain.install_tool("pnpm", spec, "linux-x86_64")
            text = path.read_text()
            self.assertIn(str(Path(d) / "bin" / "node"), text)
            self.assertIn("package/pnpm", text)
            self.assertEqual(toolchain.install_tool("pnpm", spec, "linux-x86_64"), path)
        bad = dict(spec["linux-x86_64"], integrity="sha512-" + "A" * 86 + "==")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            with self.assertRaisesRegex(toolchain.ToolchainError, "hash mismatch"):
                toolchain.install_tool("pnpm", {"version": "1", "linux-x86_64": bad}, "linux-x86_64")

    def npm_tarball_with(self, files: dict) -> tuple[bytes, str]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        blob = buf.getvalue()
        import base64
        return blob, "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()

    def pinned_pnpm_and_node(self):
        # The layout of the real pnpm 12 tarball: package.json's "bin" maps pnpm to "pnpm"
        # (registry metadata for pnpm 12.5.1), and the code lives in dist/.
        blob, integrity = self.npm_tarball_with({"package/pnpm": b"#!/usr/bin/env node\nrequire('./dist/pnpm.cjs')\n",
                                                 "package/dist/pnpm.cjs": b"console.log('pnpm')\n"})
        node = self.wrapped_tar("node-v1-linux-x64/bin/node", b"#!/bin/sh\n")
        lock = {"pnpm": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/p.tgz", "integrity": integrity,
                                                          "kind": "npm-tgz", "bin": "package/pnpm"}},
                "node": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/n.tgz",
                                                          "sha256": hashlib.sha256(node).hexdigest(),
                                                          "kind": "tar", "bin": "bin/node"}}}
        blobs = {"https://example.invalid/p.tgz": blob, "https://example.invalid/n.tgz": node}
        return lock, lambda url, dest: dest.write_bytes(blobs[url])

    def test_modified_pnpm_code_is_detected_not_just_the_wrapper(self):
        # PR #7 review, round 4: only the generated wrapper used to be hashed.
        lock, fake_download = self.pinned_pnpm_and_node()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", fake_download):
            for name in ("node", "pnpm"):
                toolchain.link(toolchain.install_tool(name, lock[name], "linux-x86_64"), name)
            toolchain.verify_tool("pnpm", lock, "linux-x86_64")
            code = Path(d) / "pnpm" / "1" / "package" / "dist" / "pnpm.cjs"
            code.write_bytes(b"steal()\n")
            with self.assertRaisesRegex(toolchain.ToolchainError, "pnpm was modified"):
                toolchain.verify_tool("pnpm", lock, "linux-x86_64")
            toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")  # reinstalls from the artifact
            self.assertEqual(code.read_bytes(), b"console.log('pnpm')\n")
            toolchain.verify_tool("pnpm", lock, "linux-x86_64")
            # The Node that runs pnpm is part of pnpm's verification.
            (Path(d) / "node" / "1" / "node-v1-linux-x64" / "lib").mkdir()
            with self.assertRaisesRegex(toolchain.ToolchainError, "node was modified"):
                toolchain.verify_tool("pnpm", lock, "linux-x86_64")

    def test_moved_checkout_is_reinstalled_not_reused(self):
        # PR #7 review, round 4: the wrapper embeds absolute paths of the original tree.
        lock, fake_download = self.pinned_pnpm_and_node()
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d) / "a" / ".toolchain", Path(d) / "b" / ".toolchain"
            with mock.patch.object(toolchain, "TOOLCHAIN", old), mock.patch.object(toolchain, "BIN", old / "bin"), \
                    mock.patch.object(toolchain, "download", fake_download):
                old.mkdir(parents=True)
                toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")
            shutil.copytree(old, new, symlinks=True)
            with mock.patch.object(toolchain, "TOOLCHAIN", new), mock.patch.object(toolchain, "BIN", new / "bin"), \
                    mock.patch.object(toolchain, "download", fake_download):
                with self.assertRaisesRegex(toolchain.ToolchainError, "not installed"):
                    toolchain.verify_tool("pnpm", lock, "linux-x86_64")
                path = toolchain.install_tool("pnpm", lock["pnpm"], "linux-x86_64")
                self.assertNotIn(str(old), path.read_text())
                self.assertIn(str(new / "bin" / "node"), path.read_text())

    def test_planted_bytecode_is_detected(self):
        # Round 5: Python runs a matching .pyc instead of its source, so __pycache__ is covered.
        blob = self.wrapped_tar("python/bin/python3", b"#!/bin/sh\n")
        lock = {"python": {"version": "1", "linux-x86_64": {"url": "https://example.invalid/py.tgz",
                                                            "sha256": hashlib.sha256(blob).hexdigest(),
                                                            "kind": "tar", "bin": "bin/python3"}}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "TOOLCHAIN", Path(d)), \
                mock.patch.object(toolchain, "BIN", Path(d) / "bin"), \
                mock.patch.object(toolchain, "download", lambda url, dest: dest.write_bytes(blob)):
            toolchain.link(toolchain.install_tool("python", lock["python"], "linux-x86_64"), "python3")
            toolchain.verify_tool("python", lock, "linux-x86_64")
            cache = Path(d) / "python" / "1" / "python" / "lib" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "os.cpython-313.pyc").write_bytes(b"planted")
            with self.assertRaisesRegex(toolchain.ToolchainError, "python was modified"):
                toolchain.verify_tool("python", lock, "linux-x86_64")

    def test_install_refuses_pins_that_are_not_on_origin_main(self):
        # Round 6: running `toolchain.py install` directly must not skip the approval gate.
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            lock = root / "scripts" / "toolchain.lock"
            lock.write_text("{}\n")
            git = lambda *a: subprocess.run(["git", "-C", d, *a], env=env, check=True, capture_output=True)
            with mock.patch.object(toolchain, "ROOT", root), mock.patch.object(toolchain, "LOCK", lock), \
                    mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}):
                git("init", "-q")
                with self.assertRaisesRegex(toolchain.ToolchainError, "unknown"):
                    toolchain.require_approved_lock(root)
                git("add", "-A")
                git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
                git("update-ref", "refs/remotes/origin/main", "HEAD")
                toolchain.require_approved_lock(root)
                lock.write_text('{"sfw": {}}\n')
                with self.assertRaisesRegex(toolchain.ToolchainError, "differs from origin/main"):
                    toolchain.require_approved_lock(root)
                with mock.patch.object(toolchain, "install_tool") as install:
                    self.assertEqual(toolchain.main(["install"]), 1)
                    install.assert_not_called()

    def test_verify_without_an_artifact_for_this_platform_is_a_clean_error(self):
        lock = {"sfw": {"version": "1", "darwin-arm64": {}}}
        with self.assertRaisesRegex(toolchain.ToolchainError, "no artifact pinned"):
            toolchain.verify_tool("sfw", lock, "linux-x86_64")

    def test_non_https_url_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(toolchain.ToolchainError, "non-HTTPS"):
                toolchain.download("http://example.invalid/x", Path(d) / "x")


class HostPythonCompatibilityTests(unittest.TestCase):
    def test_union_annotations_need_the_future_import(self):
        # Round 5: `str | None` in an evaluated annotation raises TypeError on the host minimum, 3.9.
        import ast
        for path in sorted(Path(__file__).resolve().parent.parent.rglob("*.py")):
            tree = ast.parse(path.read_text(), feature_version=(3, 9))
            future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                         and any(a.name == "annotations" for a in n.names) for n in tree.body)
            annotations = [n.annotation for n in ast.walk(tree) if isinstance(n, (ast.arg, ast.AnnAssign)) and n.annotation]
            annotations += [n.returns for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns]
            uses_union = any(isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)
                             for a in annotations for sub in ast.walk(a))
            with self.subTest(file=path.name):
                self.assertTrue(future or not uses_union, "add `from __future__ import annotations`")


if __name__ == "__main__":
    unittest.main()
