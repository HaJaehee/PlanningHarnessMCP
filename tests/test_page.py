"""The approval page: the sample data it is checked against.

tests/web/fixtures.json holds one entry for every kind of card the page draws, in the
shape GET /api/pending answers. tools/page_preview.py serves it for a look in a browser.
Both are only worth anything while that shape is the server's, so it is checked here
against the real endpoint.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planning.approval import ApprovalServer, ApprovalStore  # noqa: E402

_spec = importlib.util.spec_from_file_location("page_preview", ROOT / "tools" / "page_preview.py")
page_preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(page_preview)


class TestTheFixtures(unittest.TestCase):
    def test_they_are_what_the_server_answers(self):
        """Seeded into a state directory and read back through the real endpoint, every
        request and run comes back exactly as written - so what the tests render is what
        a real server sends."""
        fixtures = page_preview.load_fixtures("9.9.9")
        with tempfile.TemporaryDirectory() as tmp:
            page_preview.seed(Path(tmp), fixtures)
            ui = ApprovalServer(ApprovalStore(Path(tmp)), port=0, open_browser=False)
            self.assertTrue(ui.start())
            try:
                port = ui._httpd.server_address[1]
                with urlopen(f"http://127.0.0.1:{port}/api/pending", timeout=5) as resp:
                    served = json.loads(resp.read().decode("utf-8"))
            finally:
                ui.shutdown()
        self.assertEqual(served, fixtures)

    def test_they_cover_every_card(self):
        fixtures = page_preview.load_fixtures("9.9.9")
        kinds = {(r["phase"], r["origin"], r["draft"], bool(r["decided"]))
                 for r in fixtures["requests"]}
        for kind in [("PLAN", "", False, False), ("COMPLETION", "", False, False),
                     ("HALT", "", True, False), ("HALT", "user", False, False),
                     (None, "", False, False), ("PLAN", "", False, True),
                     ("HALT", "", False, True)]:
            self.assertIn(kind, kinds)
        controls = [(r["control"] or {}).get("action") for r in fixtures["runs"]]
        self.assertEqual(controls, [None, "PAUSE", "NOTE"])
        states = {c["state"] for r in fixtures["requests"] for t in r["tasks"]
                  for c in t.get("checks", [])}
        self.assertEqual(states, {"found", "old", "folder", "empty", "missing", "outside",
                                  "unknown"})

    def test_page_version_is_filled_in(self):
        versions = {r["version"] for r in page_preview.load_fixtures("9.9.9")["requests"]}
        self.assertEqual(versions, {"9.9.9", "3.0.0", None})


if __name__ == "__main__":
    unittest.main()
