"""3.1.0 - the gate on the transition, and the run in between.

Two things changed for a model that already thinks and remembers on its own, on a host
that has no approval step:

  A. The server asks the human itself at the two moments a plan changes hands - when the
     final task list arrives, and when the last task is DONE. The model no longer has a
     "now ask for approval" step to forget; request_user_approval is left with one job,
     waiting on a decision that has not come yet.
  B. Between those two gates the approval page used to say "no pending requests". It now
     shows the running plan, and the human can stop it before its next task or change
     what is left. Either takes effect when the agent next reports a task.

The flow before 3.1 (PLANNING_MCP_AUTO_ASK=false) is still supported and is what the
pinned classes of the older suites drive; everything here runs on the defaults.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planning.approval import (  # noqa: E402
    _PAGE,
    ApprovalServer,
    ApprovalStore,
    RunBoard,
)
from planning.config import Config  # noqa: E402
from planning.handlers import PlanningHandlers, _CallCtx  # noqa: E402
from planning.models import ErrorCode, Plan, PlanStatus, Task  # noqa: E402
from planning.protocol import INSTRUCTIONS  # noqa: E402
from planning.schemas import build_tool_definitions  # noqa: E402
from planning.state_machine import execution_guard, resolve_next_action  # noqa: E402
from planning.store import Store  # noqa: E402
from test_server import FakeApprovalUI  # noqa: E402

GOAL = "Q3 매출 보고서를 요약해 팀장에게 보낸다"
TASKS = ["보고서 파일 찾기", "분기별 매출 표 추출", "5줄 요약 작성"]
WHY = "보고서를 찾아야 표를 뽑고 요약할 수 있다"
AGENTS_MD = (ROOT / "agents.md").read_text(encoding="utf-8")


class GateCase(unittest.TestCase):
    """A fresh handler over a throwaway state dir, on the default configuration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def handler(self, ui=None, **cfg) -> PlanningHandlers:
        cfg.setdefault("blocking_approval", ui is not None)
        cfg.setdefault("approval_timeout", 1)
        return PlanningHandlers(
            Store(self.state_dir), Config(state_dir=self.state_dir, **cfg), approval_ui=ui
        )

    @staticmethod
    def submit(h, tasks=TASKS, **kw):
        """The model's final plan_and_think call."""
        args = {"goal": GOAL, "thought": WHY, "step_number": 1, "total_steps": 1,
                "need_more_thinking": False, "task_list": list(tasks)}
        args.update(kw)
        return h.dispatch("plan_and_think", args)

    @staticmethod
    def wait(h):
        """All that is left of request_user_approval: wait on a decision."""
        return h.dispatch("request_user_approval", {"decision": "ASK_USER"})

    @staticmethod
    def progress(h, task_id, status, log=None, **kw):
        args = {"task_id": task_id, "status": status, **kw}
        if log is not None:
            args["result_log"] = log
        return h.dispatch("update_task_progress", args)

    def done(self, h, task_id, **kw):
        return self.progress(
            h, task_id, "DONE", f"태스크 {task_id} 결과를 out/step{task_id}.txt 에 저장함", **kw
        )

    def start(self, h):
        return self.progress(h, 1, "IN_PROGRESS")

    def plan(self, h) -> Plan:
        state = h.store.load()
        return state.active_plan or max(state.plans.values(), key=lambda p: p.updated_at)

    def audit(self, event) -> list[dict]:
        path = self.state_dir / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if json.loads(l).get("event") == event]

    def running(self, ui=None, **cfg):
        """A plan the human approved at the gate, with its first task in progress."""
        ui = ui if ui is not None else FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, **cfg)
        res = self.submit(h)
        self.assertEqual(res["plan_status"], "APPROVED", res)
        self.start(h)
        ui.decision = None  # whatever is asked next, the human has not answered yet
        return h, ui, res["plan_id"]


# ===========================================================================
# A. The server asks at the transition
# ===========================================================================


class TestTheFinalTaskListIsTheRequest(GateCase):
    def test_the_request_opens_in_the_call_that_recorded_the_plan(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_mode="return")
        res = self.submit(h)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual([r["phase"] for r in ui.opened], ["PLAN"])
        self.assertEqual([t["title"] for t in ui.opened[0]["tasks"]], TASKS)
        self.assertTrue(self.plan(h).approval.requested_at)
        self.assertTrue(self.audit("approval_requested")[-1]["by_server"])

    def test_an_approval_is_the_answer_to_plan_and_think(self):
        """No request_user_approval call anywhere between the plan and the first task."""
        h = self.handler(FakeApprovalUI(decision="APPROVED"))
        res = self.submit(h)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        self.assertEqual(res["next_task"]["task_id"], 1)
        self.assertIn("Execution is now unlocked", res["message"])

    def test_a_request_for_changes_is_the_answer_too(self):
        h = self.handler(FakeApprovalUI(decision="REVISE", comment="메일은 보내지 마세요"))
        res = self.submit(h)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["user_comment"], "메일은 보내지 마세요")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")
        # Nothing in the reply sends the model to ask for approval: the redraft asks.
        self.assertNotIn("ask for approval", res["message"])

    def test_a_rejection_is_the_answer_too(self):
        h = self.handler(FakeApprovalUI(decision="REJECTED", comment="필요 없어졌습니다"))
        res = self.submit(h)
        self.assertEqual(res["plan_status"], "CANCELLED")
        self.assertEqual(res["next_action"], "ANSWER_USER")

    def test_the_models_own_sentence_is_the_overview_on_the_page(self):
        """It was never asked to write a plan_summary - there is no call to put one in."""
        ui = FakeApprovalUI(decision="APPROVED")
        self.submit(self.handler(ui))
        self.assertEqual(ui.opened[0]["summary"], WHY)

    def test_no_sentence_means_no_overview_not_a_refusal(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, model_profile="reasoning")
        res = h.dispatch("plan_and_think", {"goal": GOAL, "need_more_thinking": False,
                                            "task_list": TASKS})
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(ui.opened[0]["summary"], "")

    def test_choices_and_criteria_are_applied_at_that_gate(self):
        ui = FakeApprovalUI(decision="APPROVED", choices={"2": 1},
                            criteria={"3": "요약이 정확히 5줄이다"})
        h = self.handler(ui)
        res = self.submit(h, alternatives=[
            {"task_id": 2, "title": "CSV로 내보내 스크립트로 집계", "reason": "빠름"}])
        tasks = self.plan(h).tasks
        self.assertEqual((tasks[1].title, tasks[1].chosen), ("CSV로 내보내 스크립트로 집계", 1))
        self.assertEqual((tasks[2].done_when, tasks[2].done_when_by),
                         ("요약이 정확히 5줄이다", "user"))
        self.assertIn("The user set what finished means for task(s) 3", res["message"])

    def test_a_thinking_step_asks_nobody(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui)
        res = self.submit(h, need_more_thinking=True)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(ui.opened, [])

    def test_a_rewrite_of_flagged_tasks_goes_back_in_the_call_that_made_it(self):
        ui = FakeApprovalUI(decision="REVISE", task_comments={"2": "표는 원본 그대로"},
                            scope="TASKS")
        h = self.handler(ui)
        self.submit(h)
        ui.decision = "APPROVED"
        res = h.dispatch("plan_and_think", {
            "goal": GOAL, "thought": "표를 그대로 옮긴다", "need_more_thinking": False,
            "task_updates": [{"task_id": 2, "title": "매출 표를 원본 그대로 복사"}]})
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual([r["phase"] for r in ui.opened], ["PLAN", "PLAN"])
        self.assertEqual(ui.opened[1]["tasks"][1]["revision_note"], "표는 원본 그대로")
        self.assertEqual(ui.opened[1]["summary"], "표를 그대로 옮긴다")

    def test_a_page_that_cannot_be_reached_is_said_out_loud(self):
        h = self.handler(FakeApprovalUI(available=False))
        res = self.submit(h)
        self.assertTrue(any("NOT hard-paused" in n for n in res.get("input_notes", [])), res)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")

    def test_the_test_bypass_asks_nobody(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, autoapprove=True)
        res = self.submit(h)
        self.assertEqual(ui.opened, [])
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_switched_off_it_is_the_flow_of_3_0(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, auto_ask=False)
        res = self.submit(h)
        self.assertEqual(ui.opened, [])
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        self.assertEqual(self.wait(h)["error_code"], "MISSING_PLAN_SUMMARY")


class TestWaitingIsAllThatIsLeftOfTheApprovalTool(GateCase):
    def test_a_slice_that_ends_undecided_says_the_plan_was_taken(self):
        """Or the model, unsure, sends the plan again."""
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_timeout=30, call_budget=1)
        res = self.submit(h)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertTrue(res["message"].startswith("Your plan is recorded (3 tasks)"), res)
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        # It never wrote a plan_summary, so nothing may ask it to repeat one.
        self.assertNotIn("plan_summary", res["next_action_hint"])
        self.assertNotIn("tasks", res)

    def test_the_waiting_call_needs_nothing_but_the_decision_field(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_mode="return")
        self.submit(h)
        ui.resolve("APPROVED")
        res = self.wait(h)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["next_task"]["task_id"], 1)

    def test_it_keeps_waiting_on_the_same_request(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_mode="return")
        self.submit(h)
        self.wait(h)
        self.wait(h)
        self.assertEqual(len(ui.opened), 1)

    def test_the_plan_sent_again_while_the_human_reads_changes_nothing(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_mode="return")
        self.submit(h)
        res = self.submit(h, tasks=["전혀 다른 계획"], thought="wait, let me reconsider")
        self.assertFalse(res["ok"])
        self.assertEqual([t.title for t in self.plan(h).tasks], TASKS)
        self.assertEqual(ui.live["agent_note"], "wait, let me reconsider")

    def test_it_still_refuses_a_decision_the_model_makes_up(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_mode="return")
        self.submit(h)
        res = h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertEqual(self.plan(h).plan_status, "AWAITING_APPROVAL")

    def test_one_call_waits_one_slice_however_many_requests_it_meets(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_timeout=30, call_budget=1)
        plan = Plan(plan_id="p", goal="g")
        rid = ui.open_request("p", "g", "d", [], "fp")
        h._tls.ctx = _CallCtx()
        h._ctx().wait_spent = 5.0  # this call has already used its slice
        started = time.monotonic()
        outcome = h._wait_on(rid, plan, [])
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNone(outcome.verdict)

    def test_a_loop_stopped_in_the_same_call_is_asked_about_once(self):
        """A respawn trip waits on the human itself; asking about the plan as well
        would hold one call for two slices."""
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_timeout=30, call_budget=1)
        for goal in ("Q3 매출 보고서를 요약해서 팀장에게 보낸다",
                     "Q3 매출 보고서를 요약하여 팀장에게 보낸다"):
            self.submit(h, goal=goal, need_more_thinking=True)
        started = time.monotonic()
        res = self.submit(h, goal="Q3 매출 보고서를 요약하고 팀장에게 보낸다")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual([r["phase"] for r in ui.opened], ["HALT"])
        self.assertEqual(self.plan(h).halt["reason"], "respawn")
        self.assertIn(res["error_code"], ("APPROVAL_PENDING", "LOOP_HALTED"))


class TestADecisionMadeBetweenTwoCalls(GateCase):
    """D30. The human's click is collected at the top of the next call, whatever that
    call is. When it is the waiting call itself, the plan is no longer waiting to be
    asked about - and before 3.1 it was asked about again anyway."""

    def test_a_confirmed_plan_is_not_reopened(self):
        h, ui, _ = self.running(approval_mode="return")
        self.done(h, 1)
        self.done(h, 2)
        self.done(h, 3)
        ui.resolve("APPROVED")
        res = self.wait(h)
        self.assertEqual(res["plan_status"], "COMPLETED")
        self.assertEqual(res["next_action"], "ANSWER_USER")
        self.assertEqual([r["phase"] for r in ui.opened], ["PLAN", "COMPLETION"])
        self.assertEqual(self.plan(h).plan_status, "COMPLETED")

    def test_a_plan_sent_back_is_not_shown_again_and_the_words_arrive(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_mode="return")
        self.submit(h)
        ui.resolve("REVISE", "메일은 보내지 마세요")
        res = self.wait(h)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["user_comment"], "메일은 보내지 마세요")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")
        self.assertEqual(len(ui.opened), 1)

    def test_tasks_flagged_on_the_page_arrive_with_their_comments(self):
        ui = FakeApprovalUI(decision=None, task_comments={"2": "표는 원본 그대로"},
                            scope="TASKS")
        h = self.handler(ui, approval_mode="return")
        self.submit(h)
        ui.resolve("REVISE", "")
        res = self.wait(h)
        self.assertEqual(res["revision_targets"],
                         [{"task_id": 2, "title": TASKS[1], "user_comment": "표는 원본 그대로"}])
        self.assertIn("task_updates", res["next_action_hint"])

    def test_the_flow_of_3_0_had_the_same_hole(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_mode="return", auto_ask=False)
        self.submit(h)
        ask = {"decision": "ASK_USER", "plan_summary": "요약합니다"}
        h.dispatch("request_user_approval", ask)
        ui.resolve("REVISE", "메일은 보내지 마세요")
        res = h.dispatch("request_user_approval", ask)
        self.assertEqual((res["plan_status"], res["user_comment"]),
                         ("DRAFTING", "메일은 보내지 마세요"))
        self.assertEqual(len(ui.opened), 1)

    def test_a_plan_never_finished_is_not_sent_for_approval_either(self):
        ui = FakeApprovalUI(decision="REVISE", comment="다시")
        h = self.handler(ui)
        self.submit(h)
        self.submit(h, need_more_thinking=True, step_number=2)
        res = self.wait(h)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(len(ui.opened), 1)


class TestTheLastDoneIsTheCompletionReport(GateCase):
    def finish_all(self, h, last_kw=None):
        self.done(h, 1)
        self.done(h, 2)
        return self.done(h, 3, **(last_kw or {}))

    def test_it_opens_the_report_in_the_call_that_finished_the_work(self):
        h, ui, _ = self.running(approval_mode="return")
        res = self.finish_all(h)
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertEqual(ui.opened[-1]["phase"], "COMPLETION")
        self.assertTrue(res["message"].startswith(
            "That task is DONE and every task in this plan is finished"), res)
        self.assertTrue(self.audit("completion_verification_requested")[-1]["by_server"])

    def test_the_users_confirmation_is_the_answer_to_the_done(self):
        h, ui, _ = self.running()
        self.done(h, 1)
        self.done(h, 2)
        ui.decision = "APPROVED"
        res = self.done(h, 3)
        self.assertEqual(res["plan_status"], "COMPLETED")
        self.assertEqual(res["next_action"], "ANSWER_USER")

    def test_a_task_sent_back_is_the_answer_to_the_done(self):
        h, ui, _ = self.running()
        self.done(h, 1)
        self.done(h, 2)
        ui.decision, ui.task_comments, ui.scope = "REVISE", {"2": "3분기가 빠졌습니다"}, "TASKS"
        res = self.done(h, 3)
        self.assertEqual(res["reopened_tasks"], [2])
        self.assertEqual(res["next_task"]["task_id"], 2)
        self.assertEqual(self.plan(h).plan_status, "IN_EXECUTION")

    def test_an_undecided_slice_says_the_done_was_taken(self):
        h, ui, _ = self.running(approval_timeout=30, call_budget=1)
        res = self.finish_all(h)
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertIn("That task is DONE", res["message"])
        self.assertEqual(self.plan(h).tasks[2].status, "DONE")
        ui.resolve("APPROVED")
        self.assertEqual(self.wait(h)["plan_status"], "COMPLETED")

    def test_the_done_sent_again_is_held_not_recorded_twice(self):
        h, ui, _ = self.running(approval_mode="return")
        self.finish_all(h)
        res = self.done(h, 3)
        self.assertEqual(res["error_code"], "COMPLETION_PENDING")
        self.assertEqual(len(self.audit("task_done")), 3)

    def test_the_whole_lifecycle_never_calls_the_approval_tool(self):
        """One call for the plan, one per task and one to start the first: N + 2."""
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui)
        calls = [self.submit(h), self.start(h), self.done(h, 1), self.done(h, 2),
                 self.done(h, 3)]
        self.assertEqual(calls[-1]["plan_status"], "COMPLETED")
        self.assertEqual([r["phase"] for r in ui.opened], ["PLAN", "COMPLETION"])
        self.assertEqual(len(calls), len(TASKS) + 2)

    def test_without_the_completion_check_nothing_is_asked(self):
        h, ui, _ = self.running(completion_approval=False)
        res = self.finish_all(h)
        self.assertEqual(res["plan_status"], "COMPLETED")
        self.assertEqual([r["phase"] for r in ui.opened], ["PLAN"])


class TestWithoutAPage(GateCase):
    """Chat mode: the server still asks by itself - by handing the model the text."""

    def test_the_final_task_list_returns_the_plan_to_show(self):
        h = self.handler()
        res = self.submit(h)
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("계획 승인 요청", res["display_to_user"])
        self.assertIn("Your plan is recorded", res["message"])

    def test_the_users_reply_is_reported_with_no_ask_in_between(self):
        h = self.handler()
        self.submit(h)
        res = h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertEqual(res["plan_status"], "APPROVED")

    def test_the_last_done_returns_the_report_and_no_task_list(self):
        h = self.handler()
        self.submit(h)
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.start(h)
        self.done(h, 1)
        self.done(h, 2)
        res = self.done(h, 3)
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("완료 보고", res["display_to_user"])
        self.assertNotIn("tasks", res)
        self.assertEqual(
            h.dispatch("request_user_approval", {"decision": "APPROVED"})["plan_status"],
            "COMPLETED")

    def test_asking_again_shows_it_again_without_a_summary(self):
        h = self.handler()
        self.submit(h)
        res = self.wait(h)
        self.assertTrue(res["ok"], res)
        self.assertIn("계획 승인 요청", res["display_to_user"])


# ===========================================================================
# What the model is told
# ===========================================================================


def tools(**kw) -> dict:
    return {t["name"]: t for t in build_tool_definitions(**kw)}


def size(**kw) -> int:
    return len(json.dumps(build_tool_definitions(**kw), ensure_ascii=False))


class TestToolTextSaysWhatIsTrue(unittest.TestCase):
    MODES = [dict(model_profile=p, approval_mode=m, blocking=b)
             for p in ("standard", "reasoning")
             for m, b in (("chunked", True), ("return", True), ("chunked", False))]

    def test_approval_is_no_longer_a_step(self):
        t = tools()
        self.assertTrue(t["request_user_approval"]["description"].startswith(
            "WAITING FOR THE USER."))
        self.assertTrue(t["plan_and_think"]["description"].startswith("STEP 1"))
        self.assertTrue(t["update_task_progress"]["description"].startswith("STEP 2"))
        for name in t:
            self.assertNotIn("STEP 3", t[name]["description"])

    def test_the_model_is_not_asked_for_a_summary_nobody_reads(self):
        for mode in self.MODES:
            props = tools(**mode)["request_user_approval"]["inputSchema"]["properties"]
            self.assertNotIn("plan_summary", props, mode)
            text = json.dumps(build_tool_definitions(**mode), ensure_ascii=False)
            self.assertNotIn("plan_summary", text, mode)

    def test_the_plan_tool_says_which_call_shows_the_plan(self):
        for profile in ("standard", "reasoning"):
            text = tools(model_profile=profile)["plan_and_think"]["description"]
            self.assertIn("call shows it to the user", text, profile)

    def test_each_mode_says_what_that_mode_does(self):
        chunked = tools()["request_user_approval"]["description"]
        self.assertIn("that call waits while they decide", chunked)
        self.assertIn("Never send APPROVED", chunked)
        returned = tools(approval_mode="return")["request_user_approval"]["description"]
        self.assertIn("end your turn", returned)
        self.assertNotIn("waits while they decide", returned)
        chat = tools(blocking=False)["request_user_approval"]["description"]
        self.assertTrue(chat.startswith("REPORT THE USER'S REPLY."))
        self.assertIn('decision = "APPROVED"', chat)

    def test_switched_off_the_text_is_the_one_of_3_0(self):
        t = tools(auto_ask=False)
        self.assertTrue(t["request_user_approval"]["description"].startswith(
            "STEP 2 - USER APPROVAL."))
        self.assertIn("plan_summary", t["request_user_approval"]["inputSchema"]["properties"])
        self.assertTrue(t["update_task_progress"]["description"].startswith("STEP 3"))
        self.assertIn("the user reviews it before anything runs",
                      t["plan_and_think"]["description"])

    def test_asking_less_of_the_model_takes_fewer_words(self):
        for mode in self.MODES:
            self.assertLess(size(**mode), size(auto_ask=False, **mode), mode)

    def test_the_bypass_does_not_promise_that_the_server_asks(self):
        sys.path.insert(0, str(ROOT))
        import server
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(state_dir=Path(tmp), blocking_approval=False, autoapprove=True)
            listed = {t["name"]: t for t in server.build_protocol(cfg).tools}
        self.assertTrue(listed["request_user_approval"]["description"].startswith("STEP 2"))

    def test_no_hint_asks_for_a_summary_except_the_one_that_refuses_its_absence(self):
        plans = [None]
        for status in PlanStatus:
            for requested in (None, "2026-10-03T10:00:00+09:00"):
                plan = Plan(plan_id="p", goal="g", plan_status=status.value,
                            tasks=[Task(1, "a", status="DONE", result_log="r"), Task(2, "b")])
                plan.approval.requested_at = requested
                plans.append(plan)
        for plan in plans:
            for code in [None] + list(ErrorCode):
                _, hint = resolve_next_action(plan, code)
                if code is ErrorCode.MISSING_PLAN_SUMMARY:
                    continue
                self.assertNotIn("plan_summary", hint, (plan and plan.plan_status, code))

    def test_the_server_instructions_name_no_approval_call(self):
        self.assertNotIn("request_user_approval", INSTRUCTIONS)
        self.assertIn("the user approves", INSTRUCTIONS)


class TestThePrompt(unittest.TestCase):
    def test_two_rules_left_it(self):
        self.assertNotIn("Then call request_user_approval", AGENTS_MD)
        self.assertNotIn("After the last task, call request_user_approval", AGENTS_MD)
        self.assertNotIn("plan_summary", AGENTS_MD)
        rules = AGENTS_MD.split("<rules>")[1].split("</rules>")[0].strip().splitlines()
        self.assertEqual([r.split(".")[0] for r in rules], ["1", "2", "3", "4", "5", "6"])

    def test_it_says_who_shows_the_plan(self):
        self.assertIn("the server shows it to the user", AGENTS_MD)

    def test_the_one_thing_left_to_say_about_the_approval_tool(self):
        """It is called on APPROVAL_PENDING - possibly for the first time, so not 'again'."""
        self.assertIn('Call request_user_approval at once with decision="ASK_USER", and '
                      "again each time you get it", AGENTS_MD)

    def test_it_got_shorter(self):
        self.assertLess(len(AGENTS_MD), 2200)  # 2,275 characters in 3.0.0


# ===========================================================================
# B. The run board
# ===========================================================================


class TestRunBoard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.board = RunBoard(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def entry(pid="p1", rev="r1", **kw):
        return {"plan_id": pid, "goal": "g", "tasks": [], "rev": rev, **kw}

    def test_nothing_running_never_touches_the_disk(self):
        self.assertTrue(self.board.sync({}))
        self.assertFalse(self.board.path.exists())

    def test_it_shows_exactly_what_is_running(self):
        self.board.sync({"p1": self.entry(), "p2": self.entry("p2")})
        self.board.sync({"p2": self.entry("p2")})
        self.assertEqual([e["plan_id"] for e in self.board.peek()], ["p2"])

    def test_a_request_survives_a_refresh_of_its_plan(self):
        self.board.sync({"p1": self.entry()})
        self.assertTrue(self.board.request("p1", "PAUSE", "잠깐만요"))
        self.board.sync({"p1": self.entry(rev="r2")})
        self.assertEqual(self.board.pending("p1")["comment"], "잠깐만요")

    def test_it_leaves_with_a_plan_that_stopped_running(self):
        self.board.sync({"p1": self.entry()})
        self.board.request("p1", "PAUSE")
        self.board.sync({})
        self.board.sync({"p1": self.entry()})
        self.assertIsNone(self.board.pending("p1"))

    def test_only_a_running_plan_can_be_asked_anything(self):
        self.assertFalse(self.board.request("nope", "PAUSE"))

    def test_a_note_needs_words_and_an_action_needs_a_name(self):
        self.board.sync({"p1": self.entry()})
        self.assertFalse(self.board.request("p1", "NOTE", "   "))
        self.assertFalse(self.board.request("p1", "DELETE_EVERYTHING", "x"))
        self.assertIsNone(self.board.pending("p1"))

    def test_it_can_be_taken_back_before_it_is_applied(self):
        self.board.sync({"p1": self.entry()})
        self.board.request("p1", "NOTE", "표로 정리")
        self.assertTrue(self.board.request("p1", "CLEAR"))
        self.assertIsNone(self.board.pending("p1"))
        self.assertTrue(self.board.request("p1", "CLEAR"))  # nothing to clear is not an error

    def test_it_is_consumed_once(self):
        self.board.sync({"p1": self.entry()})
        self.board.request("p1", "PAUSE")
        self.assertEqual(self.board.take("p1")["action"], "PAUSE")
        self.assertIsNone(self.board.take("p1"))

    def test_a_take_that_cannot_be_saved_is_not_a_take(self):
        """Or it would be applied now and again on the next call."""
        self.board.sync({"p1": self.entry()})
        self.board.request("p1", "PAUSE")
        with mock.patch.object(RunBoard, "_write", return_value=False):
            self.assertIsNone(self.board.take("p1"))
        self.assertIsNotNone(self.board.pending("p1"))

    def test_a_request_that_cannot_be_saved_is_reported_as_not_recorded(self):
        self.board.sync({"p1": self.entry()})
        with mock.patch.object(RunBoard, "_write", return_value=False):
            self.assertFalse(self.board.request("p1", "PAUSE"))

    def test_words_are_capped_not_refused(self):
        self.board.sync({"p1": self.entry()})
        self.board.request("p1", "NOTE", "가" * 5000)
        self.assertEqual(len(self.board.pending("p1")["comment"]), 1000)

    def test_a_card_whose_approval_went_cold_is_not_shown(self):
        self.board.sync({"p1": self.entry(expires_at=time.time() - 5),
                         "p2": self.entry("p2", expires_at=time.time() + 600)})
        self.assertEqual([e["plan_id"] for e in self.board.peek()], ["p2"])

    def test_the_time_of_the_last_report_moves_only_when_the_card_changes(self):
        self.board.sync({"p1": self.entry()})
        first = self.board.read()["p1"]["updated_at"]
        self.board.sync({"p1": self.entry(expires_at=1.0)})  # same rev: a touch, no news
        self.assertEqual(self.board.read()["p1"]["updated_at"], first)
        time.sleep(0.01)
        self.board.sync({"p1": self.entry(rev="r2")})
        self.assertGreater(self.board.read()["p1"]["updated_at"], first)

    def test_a_file_that_is_not_a_board_reads_as_empty(self):
        for junk in ("", "[]", '{"runs": 3}', "{not json"):
            self.board.path.write_text(junk, encoding="utf-8")
            self.assertEqual(self.board.read(), {}, junk)
            self.assertEqual(self.board.peek(), [])


class TestWhatThePageShowsOfARun(GateCase):
    def test_an_approved_plan_is_on_the_board_before_its_first_task(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui)
        pid = self.submit(h)["plan_id"]
        self.assertEqual(list(ui.runs), [pid])
        self.assertEqual([t["status"] for t in ui.runs[pid]["tasks"]],
                         ["PENDING", "PENDING", "PENDING"])

    def test_it_follows_the_run_task_by_task(self):
        h, ui, pid = self.running()
        before = ui.runs[pid]["rev"]
        self.done(h, 1)
        row = ui.runs[pid]["tasks"]
        self.assertEqual([t["status"] for t in row], ["DONE", "IN_PROGRESS", "PENDING"])
        self.assertIn("out/step1.txt", row[0]["result_log"])
        self.assertNotEqual(ui.runs[pid]["rev"], before)

    def test_a_call_that_changes_nothing_does_not_redraw_the_card(self):
        h, ui, pid = self.running()
        before = ui.runs[pid]["rev"]
        h.dispatch("get_current_plan", {"plan_id": pid})
        self.assertEqual(ui.runs[pid]["rev"], before)

    def test_a_plan_being_drafted_or_awaiting_approval_is_not_a_run(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, approval_mode="return")
        self.submit(h, need_more_thinking=True)
        self.assertEqual(ui.runs, {})
        self.submit(h)
        self.assertEqual(ui.runs, {})

    def test_it_leaves_the_board_before_the_report_is_waited_on(self):
        """Never both a running card and a completion request for one plan."""
        seen = []

        class Watching(FakeApprovalUI):
            def claim(self, request_id):
                seen.append(sorted(self.runs))
                return super().claim(request_id)

        h, ui, pid = self.running(Watching(decision="APPROVED"), approval_mode="return")
        self.done(h, 1)
        self.done(h, 2)
        seen.clear()
        self.done(h, 3)
        self.assertEqual(seen, [[]])
        self.assertEqual(ui.runs, {})

    def test_it_leaves_when_the_plan_fails_is_cancelled_or_is_held(self):
        h, ui, pid = self.running()
        self.progress(h, 1, "FAILED", "보고서 폴더에 접근할 수 없습니다")
        self.assertEqual(ui.runs, {})

    def test_an_approval_gone_cold_takes_its_card_with_it(self):
        h, ui, pid = self.running(approval_ttl=600)
        self.assertAlmostEqual(ui.runs[pid]["expires_at"], time.time() + 600, delta=5)

    def test_switched_off_there_is_no_board_and_no_control(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, run_control=False)
        pid = self.submit(h)["plan_id"]
        self.assertEqual(ui.runs, {})
        self.start(h)
        ui.runs[pid] = {"control": {"id": "c", "action": "PAUSE", "comment": ""}}
        res = self.done(h, 1)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["next_task"]["task_id"], 2)

    def test_a_board_that_cannot_be_written_never_fails_a_call(self):
        class Broken(FakeApprovalUI):
            def sync_runs(self, wanted):
                raise OSError("disk full")

        h = self.handler(Broken(decision="APPROVED"))
        res = self.submit(h)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["plan_status"], "APPROVED")


# ===========================================================================
# B. Stop
# ===========================================================================


class TestPause(GateCase):
    def paused_after_one(self, memo="", **cfg):
        cfg.setdefault("approval_mode", "return")
        h, ui, pid = self.running(**cfg)
        self.assertTrue(ui.on_run_card(pid, "PAUSE", memo))
        return h, ui, pid, self.done(h, 1)

    def test_the_work_that_was_done_is_kept_and_the_next_task_does_not_start(self):
        h, ui, pid, res = self.paused_after_one()
        tasks = self.plan(h).tasks
        self.assertEqual([t.status for t in tasks], ["DONE", "PENDING", "PENDING"])
        self.assertIn("out/step1.txt", tasks[0].result_log)
        self.assertEqual(self.plan(h).halt["reason"], "user_pause")

    def test_the_model_is_told_its_done_was_taken_and_why_it_stops(self):
        h, ui, pid, res = self.paused_after_one()
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_PAUSED")
        self.assertEqual(res["message"],
                         "That task is DONE. The user paused this plan before the next task.")
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("The user paused this plan", res["next_action_hint"])
        # Nothing went wrong, so nothing may say the model repeated itself.
        self.assertNotIn("repeating", res["next_action_hint"])
        self.assertIn("실행 멈춤", res["display_to_user"])

    def test_a_slice_that_ends_undecided_sends_it_to_wait(self):
        h, ui, pid, res = self.paused_after_one(approval_mode="chunked",
                                                approval_timeout=30, call_budget=1)
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertIn("That task is DONE", res["message"])
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_the_human_gets_a_card_that_says_they_stopped_it(self):
        h, ui, pid, res = self.paused_after_one(memo="표 형식부터 확인하고 싶습니다")
        card = ui.opened[-1]
        self.assertEqual((card["phase"], card["origin"], card["draft"]), ("HALT", "user", False))
        self.assertIn("사용자가 실행을 멈췄습니다. 태스크 3개 중 1개가 끝났습니다.", card["summary"])
        self.assertIn("2번 태스크는 시작하지 않았습니다", card["summary"])
        self.assertIn("남기신 메모: 표 형식부터 확인하고 싶습니다", card["summary"])
        self.assertEqual([t["status"] for t in card["tasks"]], ["DONE", "PENDING", "PENDING"])

    def test_it_is_applied_once_and_leaves_the_board(self):
        h, ui, pid, res = self.paused_after_one()
        self.assertEqual(ui.runs, {})
        self.assertEqual(len(self.audit("run_paused")), 1)
        self.assertEqual(self.audit("run_paused")[0]["progress"], "1/3 done")

    def test_nothing_moves_while_it_is_held(self):
        h, ui, pid, res = self.paused_after_one()
        self.assertEqual(execution_guard(self.plan(h)), ErrorCode.PLAN_PAUSED)
        for call in (lambda: self.progress(h, 2, "IN_PROGRESS"), lambda: self.done(h, 2),
                     lambda: self.submit(h, tasks=["다른 계획"])):
            self.assertEqual(call()["error_code"], "PLAN_PAUSED")
        self.assertEqual([t.status for t in self.plan(h).tasks],
                         ["DONE", "PENDING", "PENDING"])
        self.assertEqual(h.dispatch("request_user_approval",
                                    {"decision": "APPROVED"})["error_code"], "APPROVAL_PENDING")

    def test_continue_starts_the_task_the_stop_held_back(self):
        h, ui, pid, _ = self.paused_after_one()
        ui.resolve("REVISE", "")
        res = self.wait(h)
        plan = self.plan(h)
        self.assertIsNone(plan.halt)
        self.assertEqual([t.status for t in plan.tasks], ["DONE", "IN_PROGRESS", "PENDING"])
        self.assertEqual(res["next_task"]["task_id"], 2)
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        self.assertEqual(self.audit("late_decision_applied")[-1]["halt_action"], "resumed")
        self.assertEqual(list(ui.runs), [pid])  # it is running again, and shown as such

    def test_a_direction_given_on_resuming_leads_the_next_hint(self):
        h, ui, pid, _ = self.paused_after_one()
        ui.resolve("REVISE", "표는 한국어로 써 주세요")
        res = self.wait(h)
        self.assertTrue(res["next_action_hint"].startswith(
            'The user said: "표는 한국어로 써 주세요".'), res["next_action_hint"])

    def test_resuming_without_words_falls_back_to_what_they_wrote_when_stopping(self):
        h, ui, pid, _ = self.paused_after_one(memo="3분기 수치를 꼭 넣어 주세요")
        ui.resolve("REVISE", "")
        res = self.wait(h)
        self.assertIn("3분기 수치를 꼭 넣어 주세요", res["next_action_hint"])

    def test_cancel_ends_the_plan_with_the_finished_work_on_record(self):
        h, ui, pid, _ = self.paused_after_one()
        ui.resolve("REJECTED", "방향이 바뀌었습니다")
        res = self.wait(h)
        self.assertEqual(res["plan_status"], "CANCELLED")
        self.assertEqual(self.plan(h).tasks[0].status, "DONE")

    def test_it_can_stop_a_plan_before_its_first_task(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, approval_mode="return")
        pid = self.submit(h)["plan_id"]
        ui.decision = None
        ui.on_run_card(pid, "PAUSE")
        res = self.start(h)
        self.assertEqual(res["error_code"], "PLAN_PAUSED")
        self.assertEqual(res["message"], "The user paused this plan before the next task.")
        self.assertEqual(self.plan(h).tasks[0].status, "PENDING")
        self.assertIn("1번 태스크는 시작하지 않았습니다", ui.opened[-1]["summary"])

    def test_a_done_that_is_refused_does_not_spend_the_stop(self):
        h, ui, pid = self.running(approval_mode="return")
        ui.on_run_card(pid, "PAUSE")
        res = self.progress(h, 1, "DONE", "완료")
        self.assertEqual(res["error_code"], "MISSING_RESULT_LOG")
        self.assertIsNotNone(ui.run_control_pending(pid))
        self.assertEqual(self.done(h, 1)["error_code"], "PLAN_PAUSED")

    def test_after_the_last_task_there_is_nothing_left_to_stop(self):
        """The completion report is asked instead - with what they wrote on it."""
        h, ui, pid = self.running(approval_mode="return")
        self.done(h, 1)
        self.done(h, 2)
        ui.on_run_card(pid, "PAUSE", "요약 길이를 확인하고 싶었습니다")
        res = self.done(h, 3)
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertIsNone(self.plan(h).halt)
        report = ui.opened[-1]
        self.assertEqual(report["phase"], "COMPLETION")
        self.assertIn("실행 중 남기신 의견", report["summary"])
        self.assertIn("요약 길이를 확인하고 싶었습니다", report["summary"])
        self.assertEqual(self.audit("run_control_moot")[-1]["reason"], "all_tasks_done")
        ui.resolve("APPROVED")
        self.wait(h)
        self.assertIsNone(self.plan(h).run_note)

    def test_a_failure_already_stops_the_plan_and_their_words_go_with_it(self):
        h, ui, pid = self.running(approval_mode="return")
        ui.on_run_card(pid, "PAUSE", "PDF 말고 엑셀 원본을 쓰세요")
        res = self.progress(h, 1, "FAILED", "보고서 폴더에 접근할 수 없습니다")
        self.assertEqual(res["plan_status"], "BLOCKED")
        self.assertIsNone(self.plan(h).halt)
        self.assertIn('the user also wrote: "PDF 말고 엑셀 원본을 쓰세요"', res["message"])
        self.assertEqual(res["user_comment"], "PDF 말고 엑셀 원본을 쓰세요")
        self.assertEqual(self.audit("run_control_moot")[-1]["reason"], "task_failed")

    def test_a_loop_stopped_by_the_server_keeps_its_own_words(self):
        h, ui, pid = self.running(approval_mode="return", breaker_repeat=2)
        for _ in range(3):
            res = self.progress(h, 1, "DONE", "완료")
        self.assertEqual(res["error_code"], "LOOP_HALTED")
        self.assertEqual(ui.opened[-1]["origin"], "")
        self.assertIn("repeating", res["next_action_hint"])


# ===========================================================================
# B. Change what is left
# ===========================================================================


NOTE = "요약은 표로 정리해 주세요"
REWRITTEN = "5줄 요약을 표로 작성"


class TestNote(GateCase):
    def noted_after_one(self, **cfg):
        h, ui, pid = self.running(**cfg)
        self.assertTrue(ui.on_run_card(pid, "NOTE", NOTE))
        return h, ui, pid, self.done(h, 1)

    def rewrite(self, h, updates, **kw):
        return h.dispatch("plan_and_think", {
            "goal": GOAL, "thought": "요약 형식을 표로 바꾼다", "need_more_thinking": False,
            "task_updates": updates, **kw})

    def test_the_done_is_kept_and_the_unfinished_tasks_are_opened(self):
        h, ui, pid, res = self.noted_after_one()
        plan = self.plan(h)
        self.assertEqual(plan.plan_status, "DRAFTING")
        self.assertEqual([t.status for t in plan.tasks], ["DONE", "PENDING", "PENDING"])
        self.assertTrue(plan.steering())
        self.assertEqual(plan.revision_targets(), {2: NOTE})
        self.assertEqual(plan.revision_open(), {3})

    def test_the_model_is_told_what_was_asked_and_what_to_send(self):
        h, ui, pid, res = self.noted_after_one()
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["message"].startswith("That task is DONE. Before you continue"))
        self.assertEqual(res["user_comment"], NOTE)
        self.assertEqual(res["tasks_unchanged"], [1])
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")
        hint = res["next_action_hint"]
        self.assertIn(f'While you were working the user wrote: "{NOTE}"', hint)
        self.assertIn('task_updates=[{"task_id": 2', hint)
        self.assertIn("You may rewrite task(s) 2, 3", hint)
        self.assertIn("Task(s) 1 are finished and keep their results", hint)

    def test_execution_is_locked_until_the_change_is_approved(self):
        h, ui, pid, _ = self.noted_after_one()
        self.assertEqual(self.progress(h, 2, "IN_PROGRESS")["error_code"], "PLAN_NOT_APPROVED")
        self.assertEqual(ui.runs, {})

    def test_the_rewrite_goes_to_the_human_and_execution_carries_on(self):
        h, ui, pid, _ = self.noted_after_one()
        ui.decision = "APPROVED"
        res = self.rewrite(h, [{"task_id": 3, "title": REWRITTEN}])
        self.assertEqual(res["plan_status"], "APPROVED")
        card = ui.opened[-1]
        self.assertEqual(card["phase"], "PLAN")
        self.assertEqual((card["tasks"][2]["title"], card["tasks"][2]["revision_note"],
                          card["tasks"][2]["previous_title"]),
                         (REWRITTEN, NOTE, TASKS[2]))
        self.assertNotIn("revision_note", card["tasks"][1])
        plan = self.plan(h)
        self.assertEqual([t.status for t in plan.tasks], ["DONE", "PENDING", "PENDING"])
        self.assertIn("out/step1.txt", plan.tasks[0].result_log)
        self.assertEqual(res["next_task"]["task_id"], 2)
        self.assertEqual(self.audit("tasks_steered")[-1]["applied"], [3])
        self.assertEqual(self.audit("tasks_steered")[-1]["kept"], [1])

    def test_no_particular_task_has_to_change_but_one_must(self):
        h, ui, pid, _ = self.noted_after_one()
        res = self.rewrite(h, [{"task_id": 1, "title": "보고서를 다시 찾기"}])
        self.assertEqual(res["error_code"], "REVISION_INCOMPLETE")
        self.assertIn("No unfinished task was rewritten", res["next_action_hint"])
        self.assertEqual(self.plan(h).tasks[0].title, TASKS[0])

    def test_a_finished_task_is_out_of_reach(self):
        h, ui, pid, _ = self.noted_after_one()
        ui.decision = "APPROVED"
        res = self.rewrite(h, [{"task_id": 1, "title": "보고서를 다시 찾기"},
                               {"task_id": 2, "title": "매출 표를 표 형식 그대로 추출"}])
        plan = self.plan(h)
        self.assertEqual((plan.tasks[0].title, plan.tasks[0].status), (TASKS[0], "DONE"))
        self.assertTrue(any("finished tasks keep their results" in n
                            for n in res.get("input_notes", [])), res)

    def test_a_whole_new_list_is_accepted_and_keeps_the_finished_work(self):
        h, ui, pid, _ = self.noted_after_one()
        ui.decision = "APPROVED"
        res = self.submit(h, tasks=[TASKS[0], TASKS[1], REWRITTEN, "팀장에게 메일 보내기"])
        plan = self.plan(h)
        self.assertEqual([t.status for t in plan.tasks],
                         ["DONE", "PENDING", "PENDING", "PENDING"])
        self.assertIn("out/step1.txt", plan.tasks[0].result_log)
        self.assertEqual(len(self.audit("steer_replanned")), 1)
        self.assertEqual(len(self.audit("evidence_carried")), 1)

    def test_the_human_may_send_the_change_back_without_losing_the_work(self):
        h, ui, pid, _ = self.noted_after_one()
        ui.decision, ui.comment = "REVISE", "메일 보내는 단계도 넣어 주세요"
        self.rewrite(h, [{"task_id": 3, "title": REWRITTEN}])
        self.assertEqual(self.plan(h).plan_status, "DRAFTING")
        ui.decision = "APPROVED"
        self.submit(h, tasks=[TASKS[0], TASKS[1], REWRITTEN, "팀장에게 메일 보내기"])
        plan = self.plan(h)
        self.assertEqual(plan.tasks[0].status, "DONE")
        self.assertIn("out/step1.txt", plan.tasks[0].result_log)

    def test_it_reaches_a_plan_that_has_not_started(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui)
        pid = self.submit(h)["plan_id"]
        ui.decision = None
        ui.on_run_card(pid, "NOTE", NOTE)
        res = self.start(h)
        self.assertEqual(self.plan(h).plan_status, "DRAFTING")
        self.assertEqual(res["message"],
                         "Before you continue, the user asked for a change to the tasks that "
                         "are left. Finished tasks keep their results.")
        self.assertEqual(self.plan(h).revision_targets(), {1: NOTE})

    def test_after_the_last_task_it_is_shown_on_the_report_instead(self):
        h, ui, pid = self.running(approval_mode="return")
        self.done(h, 1)
        self.done(h, 2)
        ui.on_run_card(pid, "NOTE", NOTE)
        self.done(h, 3)
        self.assertEqual(self.plan(h).plan_status, "AWAITING_COMPLETION")
        self.assertIn(NOTE, ui.opened[-1]["summary"])
        self.assertEqual(self.plan(h).run_note, NOTE)

    def test_it_is_applied_once(self):
        h, ui, pid, _ = self.noted_after_one()
        self.assertEqual(len(self.audit("run_note_applied")), 1)
        ui.decision = "APPROVED"
        self.rewrite(h, [{"task_id": 3, "title": REWRITTEN}])
        self.start_next = self.progress(h, 2, "IN_PROGRESS")
        self.assertTrue(self.done(h, 2)["ok"])
        self.assertEqual(len(self.audit("run_note_applied")), 1)

    def test_the_hint_for_a_note_is_not_the_hint_for_a_failure(self):
        plan = Plan(plan_id="p", goal="g", plan_status="DRAFTING",
                    tasks=[Task(1, "a", status="DONE", result_log="r"), Task(2, "b")])
        plan.pending_revision = {"targets": {"2": NOTE}, "origin": "run", "open": []}
        hint = resolve_next_action(plan)[1]
        self.assertNotIn("failed", hint)
        self.assertIn(NOTE, hint)


# ===========================================================================
# The page and its two endpoints
# ===========================================================================


class TestThePage(unittest.TestCase):
    def test_a_running_plan_has_a_card_with_both_controls(self):
        for piece in ("실행 중 · ", "function runCard(r)", "의견 전달", ">멈춤</button>",
                      "요청 취소", "control(", "/api/control", "에이전트의 마지막 보고"):
            self.assertIn(piece, _PAGE, piece)

    def test_it_says_when_a_control_takes_effect_and_what_it_cannot_do(self):
        self.assertIn("이미 실행 중인 '+\n  '도구는 중단되지 않습니다", _PAGE)
        self.assertIn("다음 태스크를 시작하지 '+\n     '않고 멈춥니다", _PAGE)

    def test_a_run_never_raises_the_alarm(self):
        self.assertIn("if(undecided.length)alertOn();else alertOff();", _PAGE)
        # Runs are counted as something to show, never as something waiting.
        self.assertIn("const undecided=list.filter(x=>!x.decided);", _PAGE)
        self.assertIn(".concat(RUNS.map(r=>({key:runId(r)", _PAGE)

    def test_everything_shown_on_it_is_escaped(self):
        for piece in ("esc(r.goal)", "esc(c.comment)", "esc(t.title)", "esc(ev)",
                      "pid=esc(r.plan_id)"):
            self.assertIn(piece, _PAGE, piece)

    def test_what_is_typed_survives_a_redraw_and_a_withdrawal(self):
        self.assertIn("dprune(list.map(x=>x.id).concat(RUNS.map(runId)))", _PAGE)
        self.assertIn("box.value=dget(runId(r),'_all')", _PAGE)
        self.assertIn("if(action==='CLEAR'){if(back)dset(id,'_all',back);}else dclear(id);", _PAGE)
        self.assertIn("refocus(focus);", _PAGE)

    def test_a_plan_the_human_stopped_is_not_called_a_loop(self):
        self.assertIn("user=d.origin==='user'", _PAGE)
        self.assertIn("(user?'실행 멈춤':'반복 감지')", _PAGE)
        self.assertIn("(user?'계획 취소':'취소')", _PAGE)


class TestOnlyTheCardThatChangedIsRedrawn(unittest.TestCase):
    """A task reported by one plan must not redraw another plan's approval request.

    With run cards the page changes every time any agent reports a task. Rebuilding
    every card on each change closed a comment box the human had just opened on a
    different card, dropped their selection, and moved the focus out and back. Asserted
    against the template; the behaviour itself - two plans on one page, one reporting
    tasks while the other's request is being typed into - was verified in a real browser.
    """

    def test_each_request_and_each_run_is_its_own_card(self):
        for piece in ("function draw(list)", "const NODES={};", "const SHOWN={};",
                      "node.className='item';", "key:reqKey(d),sig:reqSig(d)",
                      "key:runId(r),sig:runSig(r)"):
            self.assertIn(piece, _PAGE, piece)

    def test_a_card_whose_signature_is_unchanged_is_left_alone(self):
        self.assertIn("if(SHOWN[it.key]===it.sig)return;", _PAGE)
        # One place redraws a card, and one resets the page - to the idle text.
        self.assertEqual(_PAGE.count("node.innerHTML=it.html();"), 1)
        self.assertEqual(_PAGE.count("root.innerHTML="), 1)
        self.assertIn("root.innerHTML=IDLE_HTML;", _PAGE)
        self.assertNotIn(".join('<hr", _PAGE)

    def test_the_signature_is_what_can_change_under_one_key(self):
        self.assertIn("return (d.decided||'')+':'+(d.agent_waiting?1:0)+':'+"
                      "(d.agent_note||'').length;", _PAGE)
        self.assertIn("function runSig(r){return (r.rev||'')+':'+(r.control?r.control.id:'');}",
                      _PAGE)

    def test_a_card_already_in_place_is_not_moved(self):
        """Moving a node takes the focus out of it just as redrawing would."""
        self.assertIn("if(node!==cursor)root.insertBefore(node,cursor||hint);", _PAGE)

    def test_what_was_typed_is_put_back_only_into_the_card_that_was_drawn(self):
        self.assertIn("after:()=>restore(d)", _PAGE)
        self.assertIn("after:()=>restoreRun(r)", _PAGE)
        self.assertIn("function restore(d){", _PAGE)
        self.assertIn("const focus=node.contains(document.activeElement)?focusKey():null;",
                      _PAGE)

    def test_a_decision_switches_off_and_redraws_its_own_card_only(self):
        self.assertIn("lock('q-'+id);", _PAGE)
        self.assertIn("busy=false;stale('q-'+id);poll();", _PAGE)
        self.assertIn("busy=false;stale(id);poll();", _PAGE)
        self.assertNotIn("#root button", _PAGE)

    def test_the_line_between_cards_and_the_notices_are_not_cards(self):
        """The rule belongs to the second of two cards; the error line and the hint are
        their own elements, so showing or changing them redraws nothing."""
        self.assertIn(".item+.item{border-top:1px solid #ccd0d5;", _PAGE)
        self.assertIn("if(err.textContent!==lastError)err.textContent=lastError;", _PAGE)
        self.assertIn("if(hint.getAttribute('data-src')!==text)", _PAGE)

    def test_a_poll_that_changes_nothing_touches_nothing(self):
        self.assertIn("if(!draw(list))return;", _PAGE)


class TestTheEndpoints(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.ui = ApprovalServer(ApprovalStore(self.state_dir), port=0, open_browser=False)
        self.assertTrue(self.ui.start())
        self.base = f"http://127.0.0.1:{self.ui._httpd.server_address[1]}"

    def tearDown(self) -> None:
        self.ui.shutdown()
        super().tearDown()

    def get(self):
        with urlopen(f"{self.base}/api/pending", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, **body):
        req = Request(f"{self.base}/api/control", data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))["ok"]

    def approved(self):
        """A plan approved in chat on a server whose page is real."""
        h = self.handler(self.ui, autoapprove=True)
        pid = self.submit(h)["plan_id"]
        self.start(h)
        return h, pid

    def test_the_page_is_told_what_is_running(self):
        h, pid = self.approved()
        data = self.get()
        self.assertEqual(data["requests"], [])
        (run,) = data["runs"]
        self.assertEqual((run["plan_id"], run["goal"], run["control"]), (pid, GOAL, None))
        self.assertEqual([t["status"] for t in run["tasks"]],
                         ["IN_PROGRESS", "PENDING", "PENDING"])
        self.assertTrue(run["rev"])
        self.assertEqual(run["version"], Config(state_dir=self.state_dir) and
                         __import__("planning.config", fromlist=["SERVER_VERSION"]).SERVER_VERSION)

    def test_a_stop_is_recorded_shown_as_asked_and_can_be_taken_back(self):
        h, pid = self.approved()
        self.assertTrue(self.post(plan_id=pid, action="pause", comment="잠깐만요"))
        control = self.get()["runs"][0]["control"]
        self.assertEqual((control["action"], control["comment"]), ("PAUSE", "잠깐만요"))
        self.assertTrue(self.post(plan_id=pid, action="CLEAR"))
        self.assertIsNone(self.get()["runs"][0]["control"])

    def test_what_cannot_be_recorded_is_answered_with_false(self):
        h, pid = self.approved()
        self.assertFalse(self.post(plan_id="plan_nope", action="PAUSE"))
        self.assertFalse(self.post(plan_id=pid, action="NOTE", comment=""))
        self.assertFalse(self.post(plan_id=pid, action="APPROVE"))

    def test_a_stop_from_the_real_page_reaches_the_plan(self):
        h, pid = self.approved()
        self.post(plan_id=pid, action="PAUSE")
        res = self.done(h, 1)
        self.assertIn(res["error_code"], ("APPROVAL_PENDING", "PLAN_PAUSED"))
        self.assertEqual(self.plan(h).halt["reason"], "user_pause")
        data = self.get()
        self.assertEqual(data["runs"], [])
        (card,) = data["requests"]
        self.assertEqual((card["phase"], card["origin"]), ("HALT", "user"))

    def test_the_old_endpoint_still_answers(self):
        req = Request(f"{self.base}/api/decide",
                      data=json.dumps({"id": "none", "decision": "APPROVED"}).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as resp:
            self.assertFalse(json.loads(resp.read().decode("utf-8"))["ok"])


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = Config(state_dir=Path("."))
        self.assertTrue(cfg.auto_ask)
        self.assertTrue(cfg.run_control)

    def test_environment(self):
        with mock.patch.dict("os.environ", {"PLANNING_MCP_AUTO_ASK": "false",
                                            "PLANNING_MCP_RUN_CONTROL": "0"}):
            cfg = Config.from_env()
        self.assertFalse(cfg.auto_ask)
        self.assertFalse(cfg.run_control)


if __name__ == "__main__":
    unittest.main()
