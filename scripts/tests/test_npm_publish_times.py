"""npm_publish_times.py records the registry's publish times for the npm cooldown (ENGINEERING §2.5).

The pinned sfw and pnpm are replaced by a small script that prints canned `time --json` output, so
nothing is fetched.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

try:
    import npm_publish_times
except SystemExit:  # host Python < 3.11: it imports check_lockfiles, which needs the pinned Python
    npm_publish_times = None

RES = "    resolution: {integrity: sha512-" + "A" * 86 + "==}\n"
LOCK = ("lockfileVersion: '9.0'\n\nimporters:\n\n  frontend:\n    dependencies:\n      react:\n"
        "        specifier: ^19.3.0\n        version: 19.3.0\n\npackages:\n\n  '@scope/pkg@1.0.0':\n" + RES
        + "\n  react@19.3.0:\n" + RES + "\nsnapshots:\n\n  react@19.3.0: {}\n")


@unittest.skipIf(npm_publish_times is None, "needs Python 3.11+ (runs on the pinned Python in CI)")
class PublishTimesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.lock = self.dir / "pnpm-lock.yaml"
        self.times = self.dir / "pnpm-lock.times.json"
        self.lock.write_text(LOCK)
        self.calls = self.dir / "calls"

    def tearDown(self):
        self._tmp.cleanup()

    def fake(self, responses: dict[str, str], fail: bool = False) -> list[str]:
        """A stand-in for `sfw pnpm`: `view NAME@VERSION time --json` prints the canned response."""
        script = self.dir / "fake.py"
        script.write_text(
            "import json, sys\n"
            f"responses = {json.dumps(responses)}\n"
            f"open({str(self.calls)!r}, 'a').write(sys.argv[2] + '\\n')\n"
            f"sys.exit(3) if {fail!r} else print(responses.get(sys.argv[2], ''))\n"
        )
        return [sys.executable, str(script)]

    def run_main(self, command: list[str]) -> int:
        with redirect_stdout(io.StringIO()):
            return npm_publish_times.main([str(self.lock), str(self.times), "--", *command])

    def test_only_entries_in_the_packages_section_are_read(self):
        self.assertEqual(npm_publish_times.packages(self.lock), [("@scope/pkg", "1.0.0"), ("react", "19.3.0")])

    def test_missing_times_are_looked_up_and_kept_ones_reused_and_stale_ones_dropped(self):
        self.times.write_text(json.dumps({"react@19.3.0": "2026-09-01T00:00:00.000Z", "gone@1.0.0": "2026-01-01T00:00:00Z"}))
        cmd = self.fake({"@scope/pkg@1.0.0": json.dumps({"1.0.0": "2026-08-01T00:00:00.000Z"})})
        self.assertEqual(self.run_main(cmd), 0)
        self.assertEqual(json.loads(self.times.read_text()),
                         {"@scope/pkg@1.0.0": "2026-08-01T00:00:00.000Z", "react@19.3.0": "2026-09-01T00:00:00.000Z"})
        self.assertEqual(self.calls.read_text().split(), ["@scope/pkg@1.0.0"])  # react was already recorded

    def test_a_failed_lookup_stops_without_writing(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_main(self.fake({}, fail=True))
        self.assertIn("looking up @scope/pkg@1.0.0 failed", str(raised.exception))
        self.assertFalse(self.times.exists())

    def test_a_missing_or_malformed_time_stops_with_a_message(self):
        for response in ("", json.dumps({"2.0.0": "2026-08-01T00:00:00Z"}), json.dumps({"1.0.0": 5}),
                         json.dumps({"1.0.0": "last tuesday"}), json.dumps(["1.0.0"])):
            with self.subTest(response=response):
                with self.assertRaises(SystemExit) as raised:
                    self.run_main(self.fake({"@scope/pkg@1.0.0": response, "react@19.3.0": json.dumps({"19.3.0": "2026-08-01T00:00:00Z"})}))
                self.assertIn("npm_publish_times:", str(raised.exception))
                self.assertFalse(self.times.exists())

    def test_a_malformed_times_file_stops_with_a_message(self):
        for content in ("{not json", "[1, 2]"):
            with self.subTest(content=content):
                self.times.write_text(content)
                with self.assertRaises(SystemExit) as raised:
                    self.run_main(self.fake({}))
                self.assertIn("npm_publish_times:", str(raised.exception))

    def test_bad_arguments_print_the_usage(self):
        with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(npm_publish_times.main([str(self.lock), str(self.times), "sfw", "pnpm"]), 2)


if __name__ == "__main__":
    unittest.main()
