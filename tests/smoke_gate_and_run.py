"""End-to-end proof of 3.1.0, over a real stdio server and its real approval page.

What the unit suite cannot show is that the pieces meet across processes: the server is
spawned exactly as AnythingLLM spawns it, the "human" is plain HTTP against the approval
page, and the "model" never calls request_user_approval unless a response tells it to.

Verified here:

  1. the final plan_and_think call opens the approval request by itself, and a click
     made while that call is waiting comes back as its answer - APPROVED, no
     request_user_approval call in between;
  2. the approved plan is shown on the page as running, task by task;
  3. a stop asked for on the page is applied when the agent next reports a task: that
     DONE is kept, the next task does not start, and the human gets a pause card;
  4. "continue" from the pause card starts the task the stop held back;
  5. a note asked for on the page opens the unfinished tasks; the rewrite goes back to
     the human in the call that made it, and the finished task keeps its result;
  6. the last DONE opens the completion report by itself, and the human's confirmation
     is the answer to that DONE.

    python tests/smoke_gate_and_run.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smoke_chunked_approval as base  # noqa: E402
from smoke_chunked_approval import Server, _failures, check, http_json  # noqa: E402

base.PORT = 8798
GOAL = "분기 보고서를 요약해 팀장에게 보낸다"
TASKS = ["보고서 파일 찾기", "매출 표 추출", "5줄 요약 작성"]


def pending() -> dict:
    return http_json("/api/pending")


def click_when_asked(decision: str, phase: str, comment: str = "", after: float = 0.6):
    """A human who answers the next request of this phase a moment after it appears."""
    def run():
        end = time.monotonic() + 30
        while time.monotonic() < end:
            for entry in pending().get("requests", []):
                if entry.get("phase") == phase and not entry.get("decided"):
                    time.sleep(after)
                    http_json("/api/decide", {"id": entry["id"], "decision": decision,
                                              "comment": comment})
                    return
            time.sleep(0.1)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def waiting_call(s: Server, res: dict) -> dict:
    """What a model does with APPROVAL_PENDING: the one job left for the approval tool."""
    for _ in range(20):
        if res is None or res.get("error_code") != "APPROVAL_PENDING":
            return res
        res = s.call("request_user_approval", {"decision": "ASK_USER"}, wait=30)
    return res


def done(s: Server, task_id: int) -> dict:
    return s.call("update_task_progress", {
        "task_id": task_id, "status": "DONE",
        "result_log": f"태스크 {task_id} 결과를 out/step{task_id}.txt 에 저장했습니다"}, wait=30)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        s = Server(tmp, budget=5)
        try:
            check("승인 페이지 기동", s.page_is_up())

            print("\n== 1. 계획 제출이 곧 승인 요청 ==")
            human = click_when_asked("APPROVED", "PLAN")
            res = s.call("plan_and_think", {
                "goal": GOAL, "thought": "보고서를 찾아 표를 뽑고 요약합니다.",
                "step_number": 1, "total_steps": 1, "need_more_thinking": False,
                "task_list": TASKS}, wait=30)
            human.join(5)
            check("plan_and_think의 응답이 곧 승인 결과", res is not None and
                  res.get("plan_status") == "APPROVED", str(res)[:200])
            check("다음 행동은 첫 태스크", res.get("next_action") == "CALL_UPDATE_TASK_PROGRESS"
                  and res.get("next_task", {}).get("task_id") == 1)
            asked = [e for e in s.audit() if e["event"] == "approval_requested"]
            check("요청은 서버가 직접 열었음", len(asked) == 1 and asked[0].get("by_server") is True)
            pid = res["plan_id"]

            print("\n== 2. 실행 중인 계획이 페이지에 보임 ==")
            s.call("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
            page = pending()
            check("승인 요청은 없음", page["requests"] == [])
            check("실행 중 카드 1개", [r["plan_id"] for r in page.get("runs", [])] == [pid])
            check("첫 태스크 진행 중",
                  [t["status"] for t in page["runs"][0]["tasks"]] ==
                  ["IN_PROGRESS", "PENDING", "PENDING"])

            print("\n== 3. 멈춤: 끝난 태스크는 남고 다음 태스크는 시작하지 않음 ==")
            check("멈춤 요청 기록", http_json("/api/control", {
                "plan_id": pid, "action": "PAUSE", "comment": ""}).get("ok") is True)
            check("페이지에 '요청됨'으로 표시",
                  (pending()["runs"][0].get("control") or {}).get("action") == "PAUSE")
            res = done(s, 1)
            check("DONE은 기록되고 멈춤으로 응답", res is not None and res.get("error_code") in
                  ("APPROVAL_PENDING", "PLAN_PAUSED") and "That task is DONE" in
                  res.get("message", ""), str(res)[:240])
            page = pending()
            card = page["requests"][0] if page["requests"] else {}
            check("실행 중 카드 대신 멈춤 카드", page.get("runs") == [] and
                  (card.get("phase"), card.get("origin")) == ("HALT", "user"), str(card)[:160])
            check("1번 완료, 2번 미시작",
                  [t["status"] for t in card.get("tasks", [])] == ["DONE", "PENDING", "PENDING"])
            blocked = s.call("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"},
                             wait=30)
            check("멈춘 동안에는 진행 불가", blocked.get("error_code") in
                  ("PLAN_PAUSED", "APPROVAL_PENDING"), str(blocked)[:160])

            print("\n== 4. 계속 진행: 멈춤이 막았던 태스크가 시작됨 ==")
            human = click_when_asked("REVISE", "HALT", after=0.1)
            res = waiting_call(s, s.call("request_user_approval", {"decision": "ASK_USER"},
                                         wait=30))
            human.join(5)
            check("2번 태스크가 진행 중으로 넘어옴", res.get("next_task", {}).get("task_id") == 2
                  and res["next_task"]["status"] == "IN_PROGRESS", str(res)[:240])
            check("다시 실행 중 카드", [r["plan_id"] for r in pending().get("runs", [])] == [pid])

            print("\n== 5. 의견 전달: 남은 태스크만 다시 쓰고 재승인 ==")
            note = "요약은 표로 정리해 주세요"
            check("의견 기록", http_json("/api/control", {
                "plan_id": pid, "action": "NOTE", "comment": note}).get("ok") is True)
            res = done(s, 2)
            check("DONE 기록 후 계획이 수정 대기로", res.get("plan_status") == "DRAFTING"
                  and res.get("user_comment") == note, str(res)[:240])
            check("힌트가 task_updates와 의견을 안내", "task_updates" in res["next_action_hint"]
                  and note in res["next_action_hint"])
            human = click_when_asked("APPROVED", "PLAN")
            res = waiting_call(s, s.call("plan_and_think", {
                "goal": GOAL, "thought": "요약 형식을 표로 바꿉니다.",
                "need_more_thinking": False,
                "task_updates": [{"task_id": 3, "title": "5줄 요약을 표로 작성"}]}, wait=30))
            human.join(5)
            check("다시 쓴 계획이 승인됨", res.get("plan_status") == "APPROVED", str(res)[:240])
            plan = s.call("get_current_plan", {"plan_id": pid})
            check("끝난 태스크의 결과 유지", plan["tasks"][0]["status"] == "DONE" and
                  "out/step1.txt" in plan["tasks"][0].get("result_log", ""))
            check("3번만 바뀜", [t["title"] for t in plan["tasks"]] ==
                  [TASKS[0], TASKS[1], "5줄 요약을 표로 작성"])

            print("\n== 6. 마지막 DONE이 곧 완료 확인 ==")
            s.call("update_task_progress", {"task_id": 3, "status": "IN_PROGRESS"})
            human = click_when_asked("APPROVED", "COMPLETION")
            res = waiting_call(s, done(s, 3))
            human.join(5)
            check("마지막 DONE의 응답이 곧 완료 확정", res.get("plan_status") == "COMPLETED"
                  and res.get("next_action") == "ANSWER_USER", str(res)[:240])
            page = pending()
            check("페이지가 비워짐", page["requests"] == [] and page.get("runs") == [])
            events = [e["event"] for e in s.audit()]
            check("감사 로그에 멈춤과 의견이 남음",
                  events.count("run_paused") == 1 and events.count("run_note_applied") == 1
                  and events.count("tasks_steered") == 1)
        finally:
            s.close()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} - " + ", ".join(_failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
