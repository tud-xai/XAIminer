"""
Island tests: run the JS that is actually shipped in the templates.

The backend tests render markup but never execute it, so runtime bugs in the inline
"islands" slip through (a real bug once did). Here the rendered markup is handed to
a node check that evaluates the script against a stubbed DOM. No npm/jsdom/browser involved
- see tests/islands/README.md; skipped when node is not installed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from app import app as flask_app

ISLANDS_DIR = Path(__file__).parent / "islands"
NODE = shutil.which("node")
TEST_DATASET = "mock"


@unittest.skipUnless(NODE, "node not installed - island checks skipped")
class TestIslands(unittest.TestCase):

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = "testuser"
            sess["role"] = "validierer"
            sess["project"] = {"name": "Testprojekt", "type": "test",
                               "dataset": TEST_DATASET, "created_at": date.today().isoformat()}

    def _check(self, script_name, html):
        """Feeds rendered markup to a node check; its stdout is the failure report."""
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text(html, encoding="utf-8")
            proc = subprocess.run([NODE, str(ISLANDS_DIR / script_name), str(page)],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, f"\n{proc.stdout}{proc.stderr}")

    def _workspace(self):
        """Rendered workspace – carries the base.html islands (they live on every page)."""
        return self.client.get("/").data.decode()

    def test_j01__config_dialog_islands_behave(self):
        """Re-evaluation safety (HTMX re-inserts the script on every open), mode switching,
        and the XAI availability island - against the dialog we actually ship."""
        self.client.get("/")
        self._check("check_config_dialog.js", self.client.get("/panel/1/config").data.decode())

    def test_j02__scroll_sync_island_behaves(self):
        """Highest-risk island: echo guard, proportional mapping, swap restore."""
        self._check("check_scroll_sync.js", self._workspace())

    def test_j03__keynav_island_behaves(self):
        """Arrow keys step linked panels - but must not hijack typing in text inputs."""
        self._check("check_keynav.js", self._workspace())

    def test_j04__link_toggle_island_behaves(self):
        """Writes data-linked on the gallery – the seam the scroll coupling reads."""
        self._check("check_link_toggle.js", self._workspace())

    def test_j05__tracking_island_behaves(self):
        """Opt-in usage tracking: the shortcut must not fire by accident and a password must
        never enter the queue. The island only ships when tracking is configured, so the test
        configures it (the module global is read per request)."""
        previous = app_module.TRACKING_ENABLED
        app_module.TRACKING_ENABLED = True
        try:
            self._check("check_tracking.js", self._workspace())
        finally:
            app_module.TRACKING_ENABLED = previous


if __name__ == "__main__":
    unittest.main()
