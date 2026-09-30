from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import secret_scan  # noqa: E402

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class SecretScanTests(unittest.TestCase):
    """Nothing credential-shaped may be posted publicly by the review panel (ADR 0020)."""

    def test_the_likely_local_credentials_are_caught(self):
        samples = {
            "GitHub token": "ghp_" + "a" * 36,
            "Anthropic or OpenAI key": "sk-ant-api03-" + "b" * 40,
            "JWT or bearer token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
            "private key": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "Bitcoin Core RPC credential": "rpcauth=user:salt$hash",
            "extended private key": "xprv" + (B58 * 2)[:107],
            "extended public key (privacy)": "zpub" + (B58 * 2)[:107],
        }
        for name, text in samples.items():
            with self.subTest(pattern=name):
                self.assertIn(name, [n for n, _ in secret_scan.findings(f"found: {text}")])
        self.assertIn("Anthropic or OpenAI key", [n for n, _ in secret_scan.findings("sk-proj-" + "c" * 40)])

    def test_ordinary_review_text_passes(self):
        text = ("- `scripts/tripwire.py:92` (High): paths are quoted; fix with core.quotePath=false.\n"
                "Commit 8f18578ebe2c469edc64ceac553e77e980d53979, see https://github.com/x/y/pull/8\n")
        self.assertEqual(secret_scan.findings(text), [])

    def test_cli_blocks_and_never_prints_the_secret(self):
        secret = "ghp_" + "z" * 36
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "review.md"
            f.write_text(f"leaked {secret}\n")
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(secret_scan.main([str(f)]), 1)
            self.assertNotIn(secret, buf.getvalue())
            self.assertIn("GitHub token", buf.getvalue())

    def test_a_clean_scan_prints_the_ok_line(self):
        # PR #60 review: an empty copy of the script also exits 0, so callers need this line.
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "review.md"
            f.write_text("nothing secret here\n")
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(secret_scan.main([str(f)]), 0)
            self.assertEqual(buf.getvalue().strip().splitlines()[-1], "secret_scan: ok")

    def test_home_directory_is_redacted(self):
        home = str(Path.home())
        self.assertEqual(secret_scan.redact_home(f"{home}/coin/x.py"), "~/coin/x.py")


if __name__ == "__main__":
    unittest.main()
