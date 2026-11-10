"""tools/migrate/request_finance_export.js: its own Node tests, run from the Python suite.

Skipped where Node.js 20 or newer is not installed. On its own: node --test tools/migrate/
"""
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).parent
NODE = shutil.which("node")


def node_major():
    try:
        out = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=30).stdout
        return int(out.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


@unittest.skipUnless(NODE and node_major() >= 20, "Node.js 20 or newer is not installed")
class RequestFinanceExportNodeTests(unittest.TestCase):
    def test_the_script_parses(self):
        done = subprocess.run([NODE, "--check", str(HERE / "request_finance_export.js")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_node_suite_passes(self):
        done = subprocess.run([NODE, "--test", str(HERE / "request_finance_export.test.js")],
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr[-2000:])
