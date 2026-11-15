"""The approval page with the workflow diagram (3.3.0).

What the page draws is checked under node (tests/pagejs.py); what only a browser can do -
mermaid drawing the SVG, a click on a node - was checked in a real browser (wiki 07).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pagejs import needs_node, source, workflow_page  # noqa: E402
from planning import approval  # noqa: E402
from planning.approval import ApprovalServer, ApprovalStore  # noqa: E402
from planning.workflow import lint  # noqa: E402

TASKS = ["보고서 파일 찾기", "분기별 매출 표 추출", "5줄 요약 작성"]
FLOW = lint("flowchart TD\n S([시작]) --> T1 --> D1{\"Q3 <있나>?\"}\n D1 -->|예| T2 --> T3 --> E([끝])",
            TASKS)


def request(**changes):
    d = {
        "id": "r1", "plan_id": "plan_1", "goal": "목표", "summary": "", "phase": "PLAN",
        "version": "9.9.9", "decided": None, "agent_waiting": True, "agent_note": "",
        "tasks": [{"task_id": i, "title": t, "status": "PENDING"} for i, t in
                  enumerate(TASKS, start=1)],
        "workflow": {"text": FLOW.text, "nodes": FLOW.nodes, "by": "agent", "raw": "",
                     "changed": [], "asked": {}},
    }
    d.update(changes)
    return d


class TestThePageWithoutTheFlag(unittest.TestCase):
    def test_none_of_it_is_in_the_page(self):
        page = approval.page_html()
        for word in ("workflow.js", "workflow.css", "mermaid", "wfta", "/api/workflow-failed"):
            self.assertNotIn(word, page)

    def test_the_page_is_assembled_from_3_2s_files_only(self):
        self.assertEqual(approval._PAGE, approval._assemble_page())
        self.assertNotIn("workflow.js", approval.PAGE_SCRIPTS)

    def test_with_the_flag(self):
        page = approval.page_html(workflow=True)
        self.assertIn("planning-mcp/workflow.js", page)
        # mermaid first, workflow.js after app.js
        self.assertLess(page.index('src="vendor/mermaid.js"'), page.index("planning-mcp/state.js"))
        self.assertLess(page.index("planning-mcp/app.js"), page.index("planning-mcp/workflow.js"))
        self.assertIn(".wfsvg", page)


class TestServing(unittest.TestCase):
    def serve(self, workflow):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ui = ApprovalServer(ApprovalStore(Path(tmp.name)), port=0, open_browser=False,
                            workflow=workflow)
        self.assertTrue(ui.start())
        self.addCleanup(ui.shutdown)
        return ui

    def get(self, ui, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{ui._httpd.server_address[1]}{path}", timeout=5) as r:
                return r.status, r.read(), r.headers.get("Cache-Control")
        except urllib.error.HTTPError as exc:
            return exc.code, b"", None

    def test_off_no_route(self):
        ui = self.serve(False)
        self.assertEqual(self.get(ui, approval.MERMAID_ROUTE)[0], 404)
        body = self.get(ui, "/")[1].decode("utf-8")
        self.assertEqual(body, approval.page_html())

    @unittest.skipUnless(approval.MERMAID_FILE.exists(), "vendor/mermaid not fetched")
    def test_on_the_bundle_is_served(self):
        ui = self.serve(True)
        status, body, cache = self.get(ui, approval.MERMAID_ROUTE)
        self.assertEqual(status, 200)
        self.assertEqual(body, approval.MERMAID_FILE.read_bytes())
        self.assertIn("max-age", cache)
        self.assertIn("workflow.js", self.get(ui, "/")[1].decode("utf-8"))

    def test_a_file_that_is_not_the_pinned_one_is_not_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "mermaid.min.js"
            fake.write_bytes(b"alert(1)")
            with mock.patch.object(approval, "MERMAID_FILE", fake):
                data, why = approval.load_mermaid()
                self.assertIsNone(data)
                self.assertIn("sha256", why)
                ui = self.serve(True)
                self.assertIsNone(ui.mermaid)
                self.assertEqual(self.get(ui, approval.MERMAID_ROUTE)[0], 404)
            with mock.patch.object(approval, "MERMAID_FILE", Path(tmp) / "none.js"):
                self.assertIn("missing", approval.load_mermaid()[1])

    def test_the_manifest_pins_what_is_on_disk(self):
        manifest = json.loads((approval.VENDOR_DIR / "manifest.json").read_text(encoding="utf-8"))
        for item in manifest["mermaid"]["files"]:
            path = approval.VENDOR_DIR.parent / item["path"]
            if path.exists():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])

    def test_a_render_failure_is_recorded(self):
        ui = self.serve(True)
        req = urllib.request.Request(
            f"http://127.0.0.1:{ui._httpd.server_address[1]}/api/workflow-failed",
            data=json.dumps({"id": "r1", "plan_id": "p", "error": "Parse error"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        line = (ui.store.state_dir / "workflow_render_failed.jsonl").read_text(encoding="utf-8")
        self.assertIn("Parse error", line)


@needs_node
class TestTheCard(unittest.TestCase):
    def test_the_box_sits_above_the_tasks_with_one_row_per_other_node(self):
        out = workflow_page().call("requestCard", request())
        self.assertLess(out.index('id="wfbox-r1"'), out.index("태스크 3개"))
        rows = re.findall(r'data-wrow="([^"]+)"', out)
        self.assertEqual(sorted(rows), ["*", "D1", "E", "S"])  # no row for a task node
        # Escaped like every other value on the page.
        self.assertIn("D1 · Q3 &lt;있나&gt;?", out)

    def test_not_on_a_completion_report_or_a_decided_card(self):
        for changes in ({"phase": "COMPLETION"}, {"decided": "APPROVED"}, {"workflow": None}):
            with self.subTest(**{k: str(v) for k, v in changes.items()}):
                self.assertNotIn("wfbox", workflow_page().call("requestCard", request(**changes)))

    def test_server_drawn_says_so_and_shows_the_models_text(self):
        wf = dict(request()["workflow"], by="server", raw="graph ??? <b>")
        out = workflow_page().call("requestCard", request(workflow=wf))
        self.assertIn("서버가 태스크 순서대로 그렸습니다", out)
        self.assertIn("graph ??? &lt;b&gt;", out)

    def test_what_was_asked_is_shown(self):
        wf = dict(request()["workflow"], asked={"D1": "Q2로 대신", "*": "검증 추가"})
        out = workflow_page().call("requestCard", request(workflow=wf))
        self.assertIn("요청하신 내용 (D1 · Q3 &lt;있나&gt;?): Q2로 대신", out)
        self.assertIn("요청하신 내용 (워크플로우 전체): 검증 추가", out)

    def test_the_signature_redraws_a_card_drawn_before_the_script(self):
        page = workflow_page()
        self.assertTrue(page.call("reqSig", request()).endswith(":wf"))
        self.assertFalse(page.call("reqSig", request(workflow=None)).endswith(":wf"))

    def test_the_label(self):
        page = workflow_page()
        self.assertEqual(page.call("wfLabel", ["D1"], [], 0), "수정 요청(워크플로우)\n노드 1개")
        self.assertEqual(page.call("wfLabel", ["D1", "E", "*"], ["3"], 1),
                         "수정 요청(워크플로우)\n노드 2개\n워크플로우 전체 의견\n3번 태스크\n"
                         "태스크 완료 기준 1건 반영")
        self.assertEqual(page.call("wfLabel", ["*"], ["2", "3"], 0),
                         "수정 요청(워크플로우)\n워크플로우 전체 의견\n2개 태스크")

    def test_the_hint(self):
        out = workflow_page().call("hintHtml", [request()], [])
        self.assertIn("수정 요청(워크플로우)", out)
        self.assertNotIn("워크플로우", workflow_page().call("hintHtml", [request(workflow=None)], []))

    def test_the_fallback_lists_every_node(self):
        out = workflow_page().call("wfFallback", request(), "test")
        self.assertEqual(len(re.findall(r'data-act="wfOpen"', out)), len(FLOW.nodes))
        self.assertIn("flowchart TD", out)


class TestDecide(unittest.TestCase):
    """workflow.js carries its own copy of app.js's decide; only the body may differ."""

    def body(self, src):
        fn = src[src.index("decide"):]
        fn = fn[:fn.index("stale('q-'+id);poll();")]
        return re.sub(r"\s+", "", re.sub(r"//[^\n]*", "", fn[fn.index("{"):]))

    def test_the_same_but_for_the_workflow_comments(self):
        mine = self.body(source("workflow.js")[source("workflow.js").index("decide=async"):])
        theirs = self.body(source("app.js")[source("app.js").index("async function decide"):])
        self.assertEqual(mine.replace(",workflow_comments:wfComments(id)", ""), theirs)
        self.assertIn("workflow_comments:wfComments(id)", mine)


if __name__ == "__main__":
    unittest.main()
