import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import check_architecture as ca  # noqa: E402


class ArchitectureCheckTests(unittest.TestCase):
    def run_tree(self, files: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "coinacct"
            for rel, code in files.items():
                p = src / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(textwrap.dedent(code))
            return [f"{v.rule}: {v.detail}" for v in ca.check_tree(src)]

    def test_allowed_edges_pass(self):
        out = self.run_tree({
            "api/routes.py": "from coinacct.services import reports\nfrom coinacct.domain import Sats\n",
            "services/reports.py": "from coinacct import tax, chain, storage, doxx, prices\n",
            "chain/scans.py": "from coinacct.rpc import call\nfrom ..storage import db\n",
        })
        self.assertEqual(out, [])

    def test_api_may_not_import_tax_directly(self):
        out = self.run_tree({"api/routes.py": "from coinacct.tax import engine\n"})
        self.assertEqual(out, ["import-edge: api may not import tax (architecture §2)"])

    def test_domain_may_import_nothing_from_the_project(self):
        out = self.run_tree({"domain/types.py": "from coinacct.storage import db\n"})
        self.assertEqual(len(out), 1)
        self.assertIn("domain may not import storage", out[0])

    def test_relative_import_is_resolved_before_checking(self):
        out = self.run_tree({"chain/scans.py": "from ..services import jobs\n"})
        self.assertEqual(out, ["import-edge: chain may not import services (architecture §2)"])

    def test_network_only_in_allowed_modules(self):
        self.assertEqual(self.run_tree({"rpc.py": "import http.client\n"}), [])
        out = self.run_tree({"services/x.py": "import socket\n"})
        self.assertEqual(out, ["capability-network: import socket"])

    def test_subprocess_only_in_storage_volume(self):
        self.assertEqual(self.run_tree({"storage/volume.py": "import subprocess\n"}), [])
        out = self.run_tree({"storage/db.py": "import subprocess\n", "launcher.py": "import os\nos.system('x')\n"})
        self.assertIn("capability-subprocess: import subprocess", out)
        self.assertIn("capability-subprocess: os.system()", out)

    def test_filesystem_only_in_storage_and_launcher(self):
        self.assertEqual(self.run_tree({"storage/db.py": "import sqlite3\nopen('x')\n"}), [])
        out = self.run_tree({"chain/cache.py": "import sqlite3\n", "tax/engine.py": "open('x')\n"})
        self.assertIn("capability-filesystem: import sqlite3", out)
        self.assertIn("capability-filesystem: open()", out)

    def test_floats_banned_in_tax(self):
        out = self.run_tree({"tax/engine.py": "x = 0.1\ny = float('1')\nimport math\n"})
        self.assertIn("float-in-tax: float literal 0.1", out)
        self.assertIn("float-in-tax: float()", out)
        self.assertIn("float-in-tax: import math (floating point)", out)
        self.assertEqual(self.run_tree({"services/x.py": "x = 0.5\n"}), [])

    def test_clock_banned_in_pure_modules(self):
        out = self.run_tree({"doxx/rules.py": "import time\nt = time.time()\n"})
        self.assertEqual(out, ["capability-clock: time.time() in a pure module"])
        self.assertEqual(self.run_tree({"services/x.py": "import time\nt = time.time()\n"}), [])

    def test_dynamic_import_and_eval_banned_everywhere(self):
        out = self.run_tree({"launcher.py": "import importlib\n__import__('x')\neval('1')\n"})
        self.assertIn("dynamic-import: import importlib", out)
        self.assertIn("dynamic-import: __import__()", out)
        self.assertIn("code-execution: eval()", out)

    def test_unknown_top_level_module_is_reported(self):
        out = self.run_tree({"utils/helpers.py": "x = 1\n"})
        self.assertEqual(out, ["unknown-module: 'utils' is not in architecture §2"])

    def test_missing_source_tree_passes(self):
        self.assertEqual(ca.check_tree(Path("/nonexistent/coinacct")), [])


if __name__ == "__main__":
    unittest.main()
