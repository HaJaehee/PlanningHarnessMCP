"""The approval page: the files it is made of, and the sample data it is checked against.

The page lives in planning/web/ as plain HTML, CSS and JavaScript, and is served as one
response with all of it inline - no static routes. tests/web/fixtures.json holds one
entry for every kind of card the page draws, in the shape GET /api/pending answers;
tools/page_preview.py serves it for a look in a browser. Both are only worth anything
while that shape is the server's, so it is checked here against the real endpoint.
tests/web/golden.txt is what the page draws from it (see TestEveryCardDrawsAsRecorded);
what each card means to show is in test_page_cards.py.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pagejs  # noqa: E402
from planning import approval  # noqa: E402
from planning.approval import ApprovalServer, ApprovalStore, page_html  # noqa: E402

_spec = importlib.util.spec_from_file_location("page_preview", ROOT / "tools" / "page_preview.py")
page_preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(page_preview)


class TestThePageFiles(unittest.TestCase):
    def test_every_script_in_the_folder_is_loaded_and_only_those(self):
        """A script added to planning/web but not to PAGE_SCRIPTS would never run."""
        on_disk = sorted(p.name for p in approval.WEB_DIR.glob("*.js"))
        self.assertEqual(sorted(approval.PAGE_SCRIPTS), on_disk)
        self.assertEqual(sorted(p.name for p in approval.WEB_DIR.glob("*.css")), ["page.css"])

    def test_the_page_is_one_response_with_everything_inline(self):
        html = page_html()
        self.assertEqual(html.count("<style>"), 1)
        # Nothing for the browser to fetch: the server has no route that would answer.
        for external in ("<link", " src=", "@import", "url("):
            self.assertNotIn(external, html)
        self.assertNotIn("planning-mcp:style", html)
        self.assertNotIn("planning-mcp:scripts", html)

    def test_each_file_has_its_own_tag_and_name_in_order(self):
        """So the browser's developer tools list each file under its name, at its own line
        numbers: the file starts right after its tag, not one line below it."""
        names = re.findall(r"//# sourceURL=planning-mcp/([\w.-]+)\n</script>", page_html())
        self.assertEqual(names, list(approval.PAGE_SCRIPTS))
        for name in approval.PAGE_SCRIPTS:
            code = (approval.WEB_DIR / name).read_text(encoding="utf-8")
            self.assertIn("<script>" + code + "//# sourceURL=", approval._PAGE)
        css = (approval.WEB_DIR / "page.css").read_text(encoding="utf-8")
        self.assertIn("<style>" + css + "/*# sourceURL=planning-mcp/page.css */", approval._PAGE)

    def test_a_file_that_would_end_its_tag_early_is_refused(self):
        real = approval._web_file
        for name, bad in (("page.js", "const s='</SCRIPT>';\n"), ("page.css", "/* </style> */\n")):
            def fake(asked, name=name, bad=bad):
                return bad if asked == name else real(asked)
            with mock.patch.object(approval, "_web_file", fake):
                with self.assertRaises(ValueError):
                    approval._assemble_page()

    def test_the_server_serves_no_file_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = ApprovalServer(ApprovalStore(Path(tmp)), port=0, open_browser=False)
            self.assertTrue(ui.start())
            try:
                port = ui._httpd.server_address[1]
                for path in ("/page.js", "/web/page.js", "/planning/web/page.css",
                             "/../approval.py"):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
                    self.assertEqual(caught.exception.code, 404, path)
            finally:
                ui.shutdown()


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


@unittest.skipUnless(pagejs.NODE, "node is not installed")
class TestEveryCardDrawsAsRecorded(unittest.TestCase):
    """tests/web/golden.txt is the HTML every card of the fixtures draws to, a tag a line.

    A change to how the page is built must leave it exactly as it is: that is what makes
    rewriting the page's code safe. A change meant to alter what the page shows records
    it again - PLANNING_MCP_RECORD_GOLDEN=1 - and the diff of golden.txt is then exactly
    what the human will see differently."""

    def test_every_card(self):
        text = pagejs.golden_text(pagejs.drawn())
        if os.environ.get("PLANNING_MCP_RECORD_GOLDEN"):
            with open(pagejs.GOLDEN, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        recorded = pagejs.golden_blocks(pagejs.GOLDEN.read_text(encoding="utf-8"))
        drawn = pagejs.golden_blocks(text)
        self.assertEqual(list(recorded), list(drawn))
        for name, body in drawn.items():
            with self.subTest(card=name):
                self.assertEqual(recorded[name], body)


if __name__ == "__main__":
    unittest.main()
