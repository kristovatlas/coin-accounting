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

    def assertFlags(self, files: dict[str, str], expected: str) -> None:
        out = self.run_tree(files)
        self.assertTrue(any(expected in o for o in out), f"{expected!r} not in {out}")

    # --- import edges -------------------------------------------------------

    def test_allowed_edges_pass(self):
        out = self.run_tree({
            "api/routes.py": "from coinacct.services import reports\nfrom coinacct.domain import Sats\n",
            "services/reports.py": "from coinacct import tax, chain, storage, doxx, prices\n",
            "chain/scans.py": "from coinacct.rpc import call\nfrom ..storage import db\nfrom .. import rpc\n",
        })
        self.assertEqual(out, [])

    def test_api_may_not_import_tax_directly(self):
        out = self.run_tree({"api/routes.py": "from coinacct.tax import engine\n"})
        self.assertEqual(out, ["import-edge: api may not import tax (architecture §2)"])

    def test_forbidden_edge_via_package_member_import(self):
        # `from coinacct import tax` used to pass because only `coinacct` was checked.
        self.assertFlags({"api/r.py": "from coinacct import tax\n"}, "api may not import tax")

    def test_forbidden_edge_via_relative_member_import(self):
        self.assertFlags({"api/r.py": "from .. import tax\n"}, "api may not import tax")

    def test_domain_may_import_nothing_from_the_project(self):
        self.assertFlags({"domain/types.py": "from coinacct.storage import db\n"}, "domain may not import storage")

    def test_relative_import_is_resolved_before_checking(self):
        out = self.run_tree({"chain/scans.py": "from ..services import jobs\n"})
        self.assertEqual(out, ["import-edge: chain may not import services (architecture §2)"])

    def test_package_root_init_is_checked(self):
        self.assertFlags({"__init__.py": "import socket\n"}, "capability-network: import socket")

    # --- capabilities -------------------------------------------------------

    def test_network_only_in_allowed_modules(self):
        self.assertEqual(self.run_tree({"rpc.py": "import http.client\n"}), [])
        self.assertEqual(self.run_tree({"services/x.py": "import socket\n"}), ["capability-network: import socket"])

    def test_network_via_from_import_member(self):
        self.assertFlags({"tax/a.py": "from urllib import request\n"}, "capability-network: import urllib.request")
        self.assertFlags({"tax/b.py": "from http import client\n"}, "capability-network: import http.client")

    def test_asyncio_streams_are_network(self):
        self.assertFlags({"services/x.py": "import asyncio\nasyncio.open_connection('h', 1)\n"},
                         "capability-network: asyncio.open_connection()")
        self.assertFlags({"services/y.py": "from asyncio import open_connection\n"},
                         "capability-network: import asyncio.open_connection")

    def test_subprocess_only_in_storage_volume(self):
        self.assertEqual(self.run_tree({"storage/volume.py": "import subprocess\n"}), [])
        out = self.run_tree({"storage/db.py": "import subprocess\n", "launcher.py": "import os\nos.system('x')\n"})
        self.assertIn("capability-subprocess: import subprocess", out)
        self.assertIn("capability-subprocess: os.system()", out)

    def test_every_os_exec_and_spawn_form_is_subprocess(self):
        self.assertFlags({"services/x.py": "import os\nos.posix_spawnp('a', 'a', [], {})\n"},
                         "capability-subprocess: os.posix_spawnp()")
        self.assertFlags({"services/y.py": "import os\nos.execvpe('a', [], {})\n"}, "capability-subprocess")
        self.assertFlags({"services/z.py": "import pty\n"}, "capability-subprocess: import pty")
        self.assertFlags({"services/w.py": "from os import posix_spawn\n"}, "capability-subprocess")

    def test_filesystem_only_in_storage_and_launcher(self):
        self.assertEqual(self.run_tree({"storage/db.py": "import sqlite3\nopen('x')\n"}), [])
        out = self.run_tree({"chain/cache.py": "import sqlite3\n", "tax/engine.py": "open('x')\n"})
        self.assertIn("capability-filesystem: import sqlite3", out)
        self.assertIn("capability-filesystem: open()", out)

    def test_filesystem_via_pathlib_value_and_other_openers(self):
        self.assertFlags({"services/x.py": "from pathlib import Path\n"}, "capability-filesystem: import pathlib.Path")
        self.assertFlags({"services/y.py": "import pathlib\np = pathlib.Path('x')\np.write_text('s')\n"},
                         "capability-filesystem: Path.write_text()")
        self.assertFlags({"services/z.py": "import io\nio.open('x')\n"}, "capability-filesystem: io.open()")
        self.assertFlags({"services/v.py": "from os import unlink\n"}, "capability-filesystem: import os.unlink")
        self.assertEqual(self.run_tree({"services/u.py": "from pathlib import PurePath\n"}), [])

    # --- tax purity -----------------------------------------------------------

    def test_floats_banned_in_tax(self):
        out = self.run_tree({"tax/engine.py": "x = 0.1\ny = float('1')\nimport math\n"})
        self.assertIn("float-in-tax: float literal 0.1", out)
        self.assertIn("float-in-tax: float()", out)
        self.assertIn("float-in-tax: import math (floating point)", out)
        self.assertEqual(self.run_tree({"services/x.py": "x = 0.5\n"}), [])

    def test_true_division_banned_in_tax(self):
        # #10: `sats / n` yields a float and silently loses precision.
        self.assertFlags({"tax/a.py": "def f(sats, n):\n    return sats / n\n"}, "float-in-tax: true division")
        self.assertFlags({"tax/b.py": "def f(x):\n    x /= 2\n    return x\n"}, "float-in-tax: true division `/=`")
        self.assertEqual(self.run_tree({"tax/c.py": "def f(sats, n):\n    return sats // n\n"}), [])
        self.assertEqual(self.run_tree({"services/d.py": "x = 1 / 2\n"}), [])

    def test_clock_banned_in_pure_modules_including_aliases(self):
        self.assertEqual(self.run_tree({"doxx/rules.py": "import time\nt = time.time()\n"}),
                         ["capability-clock: time.time() in a pure module"])
        self.assertFlags({"tax/a.py": "import time as t\nx = t.time()\n"}, "capability-clock: time.time()")
        self.assertFlags({"tax/b.py": "from datetime import datetime as dt\nx = dt.now()\n"},
                         "capability-clock: datetime.datetime.now()")
        self.assertEqual(self.run_tree({"services/x.py": "import time\nt = time.time()\n"}), [])

    def test_dynamic_import_and_eval_banned_everywhere(self):
        out = self.run_tree({"launcher.py": "import importlib\n__import__('x')\neval('1')\n"})
        self.assertIn("dynamic-import: import importlib", out)
        self.assertIn("dynamic-import: __import__()", out)
        self.assertIn("code-execution: eval()", out)

    def test_unknown_top_level_module_is_reported(self):
        self.assertEqual(self.run_tree({"utils/helpers.py": "x = 1\n"}),
                         ["unknown-module: 'utils' is not in architecture §2"])

    def test_missing_source_tree_passes(self):
        self.assertEqual(ca.check_tree(Path("/nonexistent/coinacct")), [])


if __name__ == "__main__":
    unittest.main()
