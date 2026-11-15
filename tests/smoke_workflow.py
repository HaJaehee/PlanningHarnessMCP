"""End-to-end proof of the workflow diagram (3.3.0), over a real stdio server and its page.

The server is spawned as AnythingLLM spawns it, with ADVANCED_WORKFLOW_PLANNING=1; the
"human" is plain HTTP against the approval page.

Verified here:

  1. tools/list advertises `workflow` on plan_and_think;
  2. the model's Mermaid - code fence, `->`, `end` as an id, a click - is repaired and
     reaches the page as canonical text with its nodes;
  3. the page carries the diagram script, and mermaid.js is served from the bundle (or,
     in a package built without it, the route is absent and the page still loads);
  4. a comment on a node, sent as the page sends it, comes back to the waiting call as
     a WORKFLOW revision with the node, its label and the comment;
  5. the redrawn workflow goes back to the human with the new node marked as changed;
  6. a node the request did not show refuses the whole decision.

    python tests/smoke_workflow.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smoke_chunked_approval as base  # noqa: E402
from smoke_chunked_approval import Server, _failures, check, http_json  # noqa: E402

base.PORT = 8787
GOAL = "Q3 매출 보고서를 요약해 팀장에게 보낸다"
TASKS = ["보고서 파일 찾기", "분기별 매출 표 추출", "5줄 요약 작성"]
FLOW = """```mermaid
flowchart TD
  start([시작]) -> T1
  T1 --> D1{Q3 데이터가 있나?}
  D1 -->|예| T2
  D1 -- 아니오 --> end([끝])
  T2 --> T3 --> end
  click T1 callback
```"""
FLOW2 = """flowchart TD
  S([시작]) --> T1
  T1 --> D1{"Q3 데이터가 있나?"}
  D1 -->|예| T2
  D1 -->|아니오| Q2["Q2 데이터로 대신 집계"] --> T2
  T2 --> T3 --> E([끝])"""


def get(path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{base.PORT}{path}", timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def plan_request() -> dict:
    for entry in http_json("/api/pending").get("requests", []):
        if entry.get("phase") == "PLAN" and not entry.get("decided"):
            return entry
    return {}


def comment_when_asked(workflow_comments: dict, after: float = 0.6):
    def run():
        end = time.monotonic() + 30
        while time.monotonic() < end:
            entry = plan_request()
            if entry:
                time.sleep(after)
                http_json("/api/decide", {
                    "id": entry["id"], "decision": "REVISE", "comment": "",
                    "task_comments": {}, "scope": "WORKFLOW", "choices": {}, "criteria": {},
                    "workflow_comments": workflow_comments})
                return
            time.sleep(0.1)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def main() -> int:
    os.environ["ADVANCED_WORKFLOW_PLANNING"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        s = Server(tmp, budget=5)
        try:
            check("승인 페이지 기동", s.page_is_up())

            print("\n== 1. 도구 목록 ==")
            tools = s.request("tools/list", {})["result"]["tools"]
            plan = next(t for t in tools if t["name"] == "plan_and_think")
            check("plan_and_think에 workflow 파라미터", "workflow" in plan["inputSchema"]["properties"])

            print("\n== 2. 수리된 워크플로우가 페이지로 ==")
            human = comment_when_asked({"D1": "데이터가 없으면 Q2로 대신 집계"})
            res = s.call("plan_and_think", {
                "goal": GOAL, "thought": "파일을 찾고 표를 뽑아 요약합니다.", "step_number": 1,
                "total_steps": 1, "need_more_thinking": False, "task_list": TASKS,
                "workflow": FLOW}, wait=30)
            human.join(5)
            notes = " ".join(res.get("input_notes") or [])
            check("수리 내용이 모델에게 전달", "fence" in notes and "keyword" in notes, notes[:200])

            print("\n== 3. 노드 의견이 대기 중인 호출의 답 ==")
            check("WORKFLOW 수정 요청", (res.get("plan_status"), res.get("revision_scope")) ==
                  ("DRAFTING", "WORKFLOW"), str(res)[:240])
            check("노드·라벨·의견", res.get("workflow_comments") == [
                {"node": "D1", "label": "Q3 데이터가 있나?",
                 "user_comment": "데이터가 없으면 Q2로 대신 집계"}], str(res.get("workflow_comments")))
            check("정확한 호출을 안내", "workflow=<the whole revised flowchart>" in
                  res.get("next_action_hint", ""))
            check("모델이 받은 워크플로우는 정규형", "click" not in (res.get("workflow") or "x")
                  and 'E(["끝"])' in (res.get("workflow") or ""))

            print("\n== 4. 다시 그린 워크플로우 ==")
            res = s.call("plan_and_think", {
                "goal": GOAL, "thought": "Q2 경로를 넣었습니다.", "need_more_thinking": False,
                "workflow": FLOW2}, wait=30)
            check("다시 승인 요청으로", res.get("error_code") == "APPROVAL_PENDING", str(res)[:200])
            entry = plan_request()
            wf = entry.get("workflow") or {}
            check("새 요청에 워크플로우", "Q2" in wf.get("text", ""), str(entry)[:200])
            check("바뀐 노드 표시", wf.get("changed") == ["Q2"], str(wf.get("changed")))
            check("요청하신 내용 표시", wf.get("asked") == {"D1": "데이터가 없으면 Q2로 대신 집계"})

            print("\n== 5. 페이지와 mermaid.js ==")
            status, page = get("/")
            text = page.decode("utf-8")
            check("페이지에 다이어그램 스크립트", "planning-mcp/workflow.js" in text
                  and 'src="vendor/mermaid.js"' in text)
            status, lib = get("/vendor/mermaid.js")
            bundled = (Path(__file__).resolve().parent.parent / "vendor" / "mermaid"
                       / "mermaid.min.js").exists()
            check("mermaid.js 제공 (번들이 있으면 200, 없으면 404)",
                  (status == 200 and len(lib) > 100_000) if bundled else status == 404,
                  f"{status} {len(lib)}")

            print("\n== 6. 보이지 않은 노드는 거부 ==")
            refused = http_json("/api/decide", {
                "id": entry["id"], "decision": "REVISE", "scope": "WORKFLOW",
                "workflow_comments": {"ZZ": "?"}})
            check("결정 전체 거부", refused.get("ok") is False and not plan_request().get("decided"))
        finally:
            s.close()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {_failures}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
