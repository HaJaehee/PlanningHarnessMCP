"""1.16.0 - planning loops converge (D25, D26).

Field report: with Zed / Goose driving a mid-sized "thinking" model, the agent got stuck
in its own self-verification ("wait, let me reconsider"), both inside one thinking block
and as an endless run of plan_and_think calls. The corporate model cannot be run from
here, so these tests pin the server's side of the guarantee with scripted models:

    from any state, whatever a model sends, the number of planning calls it can make
    without a human in between is bounded, nothing it already produced is lost, and
    every path ends in front of a human or in an answer.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planning.approval import _PAGE, PHASE_HALT, ApprovalStore  # noqa: E402
from planning.config import Config  # noqa: E402
from planning.handlers import PlanningHandlers, reconsider_markers  # noqa: E402
from planning.loopguard import LoopGuard, call_signature  # noqa: E402
from planning.models import Plan, PlanStatus  # noqa: E402
from planning.protocol import INSTRUCTIONS, McpProtocol  # noqa: E402
from planning.schemas import build_tool_definitions  # noqa: E402
from planning.state_machine import resolve_next_action  # noqa: E402
from planning.store import Store  # noqa: E402
from test_server import FakeApprovalUI  # noqa: E402

GOAL = "Q3 매출 보고서를 요약한다"
TASKS = ["보고서 찾기", "표 추출", "요약 작성"]
EVIDENCE = "결과를 out/summary.txt 에 저장함"


class LoopCase(unittest.TestCase):
    """A fresh handler over a throwaway state dir; two-phase approval unless asked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def handler(self, ui=None, **cfg) -> PlanningHandlers:
        cfg.setdefault("blocking_approval", ui is not None)
        cfg.setdefault("approval_timeout", 1)
        config = Config(state_dir=self.state_dir, **cfg)
        return PlanningHandlers(Store(self.state_dir), config, approval_ui=ui)

    @staticmethod
    def think(h, step=1, more=True, goal=GOAL, **kw):
        args = {"goal": goal, "thought": kw.pop("thought", "생각"), "step_number": step,
                "total_steps": step + 1, "need_more_thinking": more}
        args.update(kw)
        return h.dispatch("plan_and_think", args)

    def finalize(self, h, tasks=None):
        return self.think(h, more=False, task_list=list(tasks or TASKS))

    def approve(self, h):
        self.finalize(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "요약"})
        return h.dispatch("request_user_approval", {"decision": "APPROVED"})

    def run_all(self, h, n=len(TASKS)):
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = None
        for tid in range(1, n + 1):
            res = h.dispatch("update_task_progress", {
                "task_id": tid, "status": "DONE", "result_log": f"{EVIDENCE} ({tid})"})
        return res

    def plan(self, h) -> Plan:
        state = h.store.load()
        return state.active_plan or max(state.plans.values(), key=lambda p: p.updated_at)

    def audit(self) -> list[dict]:
        path = self.state_dir / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def events(self, name: str) -> list[dict]:
        return [e for e in self.audit() if e.get("event") == name]


# ---------------------------------------------------------------------------
# The four doors found while planning 1.16 - pinned as regressions.
# ---------------------------------------------------------------------------


class TestLoopRegressions(LoopCase):
    def test_a_thinking_steps_are_bounded(self):
        """A: forty need_more_thinking=true calls used to be accepted, each answered with
        'call again with step_number=N+1'. Now the round ends at the budget."""
        h = self.handler()
        statuses = []
        for step in range(1, 41):
            res = self.think(h, step=step, task_list=TASKS)
            statuses.append(res["plan_status"])
            if res["plan_status"] != "DRAFTING":
                break
        self.assertLessEqual(len(statuses), 8)
        self.assertEqual(statuses[-1], "AWAITING_APPROVAL")
        self.assertEqual([t.title for t in self.plan(h).tasks], TASKS)

    def test_b_rethinking_during_approval_does_not_pull_the_plan_back(self):
        """B: plan_and_think while the human was looking reset the plan to DRAFTING and
        withdrew the request. Now it waits on the human and changes nothing."""
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui)
        self.finalize(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        before = self.plan(h)
        res = self.think(h, step=2, thought="wait, let me reconsider step 2",
                         more=False, task_list=["전혀 다른 계획"])
        after = self.plan(h)
        self.assertEqual(after.plan_status, "AWAITING_APPROVAL")
        self.assertEqual([t.title for t in after.tasks], [t.title for t in before.tasks])
        self.assertTrue(after.approval.requested_at)
        self.assertFalse(res["ok"])
        self.assertIn(res["error_code"], ("APPROVAL_PENDING", "PLAN_NOT_APPROVED"))
        # What it wanted to reconsider is shown to the human instead of acted on.
        self.assertEqual(ui.live["agent_note"], "wait, let me reconsider step 2")
        self.assertTrue(self.events("call_held_for_human"))

    def test_b_a_held_rethink_returns_the_humans_decision(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui)
        self.finalize(h)
        # The request is published undecided first, then the human clicks during the
        # model's stray call.
        ui.decision = None
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        ui.resolve("APPROVED")
        res = self.think(h, step=2, thought="hmm, maybe more steps")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["next_task"]["task_id"], 1)

    def test_c_rethinking_after_the_last_task_keeps_every_result(self):
        """C (D26): plan_and_think during AWAITING_COMPLETION went to DRAFTING with every
        task DONE, and the next task_list deleted all their evidence."""
        h = self.handler()
        self.approve(h)
        self.run_all(h)
        self.assertEqual(self.plan(h).plan_status, "AWAITING_COMPLETION")
        r1 = self.think(h, step=1, thought="let me plan the final answer")
        r2 = self.think(h, step=2, more=False, task_list=["Write the final answer"])
        plan = self.plan(h)
        self.assertEqual(plan.plan_status, "AWAITING_COMPLETION")
        self.assertEqual([t.title for t in plan.tasks], TASKS)
        self.assertTrue(all(t.result_log for t in plan.tasks))
        for res in (r1, r2):
            self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_d_the_same_goal_is_not_planned_again_right_after_completion(self):
        """D: 'plan before answering ANY request' + ANSWER_USER = a second lap."""
        h = self.handler()
        self.approve(h)
        self.run_all(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "보고"})
        done = h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertEqual(done["plan_status"], "COMPLETED")
        res = self.think(h, step=1, thought="answer the user")
        self.assertEqual(res["plan_id"], done["plan_id"])
        self.assertEqual(res["next_action"], "ANSWER_USER")
        self.assertTrue(all(t.get("result_log") for t in res["tasks"]))
        self.assertEqual(len(h.store.load().plans), 1)
        self.assertTrue(self.events("replan_after_completion_suppressed"))

    def test_d_a_different_goal_still_starts_a_new_plan(self):
        h = self.handler()
        self.approve(h)
        self.run_all(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "보고"})
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        res = self.think(h, goal="Q4 매출 보고서를 요약한다")
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(len(h.store.load().plans), 2)

    def test_d_cooldown_zero_turns_it_off(self):
        h = self.handler(replan_cooldown=0)
        self.approve(h)
        self.run_all(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "보고"})
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        res = self.think(h)
        self.assertEqual(res["plan_status"], "DRAFTING")


# ---------------------------------------------------------------------------
# Thinking budget and the draft
# ---------------------------------------------------------------------------


class TestThinkingBudget(LoopCase):
    def test_the_hint_names_the_exit_first(self):
        h = self.handler()
        res = self.think(h)
        hint = res["next_action_hint"]
        self.assertLess(hint.index("need_more_thinking=false"), hint.index("step_number=2"))
        self.assertIn("does not need to be perfect", hint)
        self.assertEqual(res["thinking_steps_left"], 7)

    def test_the_last_step_is_announced(self):
        h = self.handler(max_thinking_steps=3)
        self.think(h, step=1)
        res = self.think(h, step=2)
        self.assertIn("one thinking step left", res["next_action_hint"])

    def test_a_draft_is_kept_while_thinking(self):
        h = self.handler()
        res = self.think(h, task_list=TASKS)
        self.assertEqual(res["draft_saved"], 3)
        self.assertEqual(self.plan(h).draft_tasks, TASKS)

    def test_budget_spent_with_a_draft_submits_it(self):
        h = self.handler(max_thinking_steps=3)
        self.think(h, step=1, task_list=["초안 1"])
        self.think(h, step=2, task_list=TASKS)
        res = self.think(h, step=3, thought="wait, let me reconsider once more")
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual([t["title"] for t in res["tasks"]], TASKS)
        self.assertTrue(any("thinking steps" in n for n in res["input_notes"]))
        self.assertTrue(self.events("auto_finalized"))
        self.assertEqual(self.plan(h).draft_tasks, [])

    def test_auto_submit_goes_straight_to_the_page(self):
        """The model that would not call its plan final is not trusted to ask for
        approval either: the server publishes, and the call waits on the human."""
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, max_thinking_steps=2)
        self.think(h, step=1, task_list=TASKS)
        res = self.think(h, step=2)
        self.assertEqual(len(ui.opened), 1)
        self.assertIn("마지막 초안", ui.opened[0]["summary"])
        self.assertEqual(res["plan_status"], "APPROVED")

    def test_budget_spent_without_a_draft_halts(self):
        h = self.handler(max_thinking_steps=2)
        self.think(h, step=1)
        res = self.think(h, step=2)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "LOOP_HALTED")
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("display_to_user", res)
        self.assertEqual(self.plan(h).halt["reason"], "thinking_budget")

    def test_final_call_without_a_list_uses_the_draft(self):
        h = self.handler()
        self.think(h, step=1, task_list=TASKS)
        res = self.think(h, step=2, more=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual([t["title"] for t in res["tasks"]], TASKS)

    def test_a_human_revision_opens_a_fresh_round(self):
        h = self.handler(max_thinking_steps=2)
        self.finalize(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        h.dispatch("request_user_approval", {"decision": "REVISE", "user_comment": "더 짧게"})
        res = self.think(h, step=2)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["thinking_steps_left"], 1)

    def test_negative_budget_is_unlimited(self):
        h = self.handler(max_thinking_steps=-1, loop_breaker=False)
        for step in range(1, 30):
            res = self.think(h, step=step)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertNotIn("thinking_steps_left", res)


class TestReasoningProfile(LoopCase):
    def test_one_call_records_the_plan(self):
        h = self.handler(model_profile="reasoning")
        res = h.dispatch("plan_and_think", {"goal": GOAL, "task_list": TASKS})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertNotIn("input_notes", res)  # no nagging about thought / need_more

    def test_two_steps_then_the_draft_goes_to_the_user(self):
        h = self.handler(model_profile="reasoning")
        r1 = h.dispatch("plan_and_think", {
            "goal": GOAL, "task_list": TASKS, "need_more_thinking": True})
        self.assertIn("one thinking step left", r1["next_action_hint"])
        r2 = h.dispatch("plan_and_think", {
            "goal": GOAL, "need_more_thinking": True, "thought": "wait, reconsider"})
        self.assertEqual(r2["plan_status"], "AWAITING_APPROVAL")

    def test_the_advertised_schema_is_one_call(self):
        tools = {t["name"]: t for t in build_tool_definitions(model_profile="reasoning")}
        props = tools["plan_and_think"]["inputSchema"]["properties"]
        for hidden in ("step_number", "total_steps", "revises_step"):
            self.assertNotIn(hidden, props)
        self.assertIn("ONE call", tools["plan_and_think"]["description"])

    def test_the_server_still_accepts_step_fields(self):
        h = self.handler(model_profile="reasoning")
        res = self.think(h, more=False, task_list=TASKS, revises_step=None)
        self.assertTrue(res["ok"], res)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestLoopGuardUnit(unittest.TestCase):
    def test_repeat_trips(self):
        g = LoopGuard(calls=99, repeat=3, error_streak=99)
        sig = call_signature("t", {"a": 1})
        self.assertIsNone(g.observe("p", sig, None))
        self.assertIsNone(g.observe("p", sig, None))
        self.assertEqual(g.observe("p", sig, None), ("repeated_call", 3))

    def test_uncounted_calls_neither_add_nor_break_a_streak(self):
        g = LoopGuard(calls=99, repeat=3, error_streak=99)
        sig = call_signature("t", {"a": 1})
        g.observe("p", sig, None)
        g.observe("p", call_signature("t", {"b": 2}), None, counted=False)
        g.observe("p", sig, None)
        self.assertEqual(g.observe("p", sig, None), ("repeated_call", 3))

    def test_milestone_resets(self):
        g = LoopGuard(calls=3, repeat=99, error_streak=99)
        g.observe("p", "a", None)
        g.observe("p", "b", None)
        g.milestone("p")
        self.assertIsNone(g.observe("p", "c", None))

    def test_error_streak_needs_the_same_code(self):
        g = LoopGuard(calls=99, repeat=99, error_streak=3)
        g.observe("p", "a", "X")
        g.observe("p", "b", "Y")
        g.observe("p", "c", "X")
        self.assertIsNone(g.observe("p", "d", "Y"))

    def test_disabled_never_trips(self):
        g = LoopGuard(calls=1, repeat=1, error_streak=1, enabled=False)
        self.assertIsNone(g.observe("p", "a", "X"))


class TestLoopBreaker(LoopCase):
    def test_the_same_call_three_times_halts(self):
        h = self.handler()
        self.approve(h)
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        call = {"plan_id": "current"}
        h.dispatch("get_current_plan", call)
        h.dispatch("get_current_plan", call)
        res = h.dispatch("get_current_plan", call)
        self.assertEqual(res["error_code"], "LOOP_HALTED")
        self.assertEqual(self.plan(h).halt["reason"], "repeated_call")
        self.assertTrue(self.events("loop_halted"))

    def test_no_progress_halts_at_the_limit(self):
        h = self.handler(breaker_calls=5, breaker_repeat=99)
        self.approve(h)
        # Starting a task is a call, not progress: progress is a task finished.
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        halted_at = None
        for i in range(1, 6):
            res = h.dispatch("get_current_plan", {"plan_id": self.plan(h).plan_id})
            if res.get("error_code") == "LOOP_HALTED":
                halted_at = i
                break
        self.assertEqual(halted_at, 4)  # 1 IN_PROGRESS + 4 reads = 5 calls
        self.assertEqual(self.plan(h).halt["reason"], "no_progress")

    def test_progress_keeps_the_breaker_quiet(self):
        h = self.handler(breaker_calls=3)
        self.approve(h)
        res = self.run_all(h)
        self.assertTrue(res["ok"], res)
        self.assertIsNone(self.plan(h).halt)

    def test_a_halted_plan_refuses_work(self):
        h = self.handler()
        self.approve(h)
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        for _ in range(3):
            h.dispatch("get_current_plan", {"plan_id": "current"})
        res = h.dispatch("update_task_progress", {
            "task_id": 1, "status": "DONE", "result_log": EVIDENCE})
        self.assertEqual(res["error_code"], "LOOP_HALTED")
        self.assertEqual(self.plan(h).tasks[0].status, "IN_PROGRESS")
        res = self.think(h, step=9)
        self.assertEqual(res["error_code"], "LOOP_HALTED")

    def test_recovery_explains_the_pause(self):
        h = self.handler()
        self.approve(h)
        for _ in range(3):
            h.dispatch("get_current_plan", {"plan_id": "current"})
        res = h.dispatch("get_current_plan", {"plan_id": self.plan(h).plan_id})
        self.assertIn("halted", res)
        self.assertIn("반복", res["halted"]["reason"])

    def test_chat_resolution_continue_with_guidance(self):
        """Without a page the human answers in chat and the model reports it."""
        h = self.handler(max_thinking_steps=2)
        self.think(h, step=1)
        self.think(h, step=2)  # budget spent, no draft -> halted
        res = h.dispatch("request_user_approval", {
            "decision": "REVISE", "user_comment": "CSV 말고 엑셀로 하세요"})
        self.assertTrue(res["ok"], res)
        plan = self.plan(h)
        self.assertIsNone(plan.halt)
        self.assertEqual(plan.guidance, "CSV 말고 엑셀로 하세요")
        self.assertIn('The user said: "CSV 말고 엑셀로 하세요"', res["next_action_hint"])
        # A fresh thinking budget.
        nxt = self.think(h, step=3, task_list=TASKS)
        self.assertEqual(nxt["plan_status"], "DRAFTING")
        # The guidance is retired once the plan moves on.
        done = self.think(h, step=4, more=False)
        self.assertIsNone(self.plan(h).guidance)
        self.assertNotIn("The user said", done["next_action_hint"])

    def test_chat_resolution_cancel(self):
        h = self.handler(max_thinking_steps=1)
        self.think(h, step=1)
        res = h.dispatch("request_user_approval", {"decision": "REJECTED"})
        self.assertEqual(res["plan_status"], "CANCELLED")

    def test_chat_resolution_approve_the_draft(self):
        h = self.handler(breaker_repeat=2)
        self.think(h, step=1, task_list=TASKS)
        # The same call twice is a loop at this limit.
        self.think(h, step=2, thought="same", task_list=TASKS)
        self.think(h, step=2, thought="same", task_list=TASKS)
        self.assertTrue(self.plan(h).halt)
        res = h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual([t.title for t in self.plan(h).tasks], TASKS)
        self.assertEqual(res["next_task"]["task_id"], 1)

    def test_ask_user_on_a_halted_plan_shows_the_halt(self):
        h = self.handler(max_thinking_steps=1)
        self.think(h, step=1)
        res = h.dispatch("request_user_approval", {"decision": "ASK_USER"})
        self.assertIn("반복 감지", res["display_to_user"])
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")

    def test_page_halt_is_published_and_waited_on(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, max_thinking_steps=2)
        self.think(h, step=1, task_list=None)
        res = self.think(h, step=2)
        self.assertEqual(ui.live["phase"], PHASE_HALT)
        self.assertFalse(ui.live["draft"])
        self.assertIn("생각 단계 2단계", ui.live["summary"])
        self.assertFalse(res["ok"])

    def test_waiting_on_a_halt_does_not_mention_a_plan_summary(self):
        """A halt has no plan_summary; telling a thinking model to resend 'the same
        plan_summary as before' is one more thing for it to puzzle over."""
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, max_thinking_steps=2, approval_timeout=30, call_budget=1)
        self.think(h, step=1)
        res = self.think(h, step=2)
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertNotIn("plan_summary", res["next_action_hint"])
        self.assertIn("ASK_USER", res["next_action_hint"])

    def test_the_model_cannot_lift_a_halt_on_the_page(self):
        """D20 for halts: while the human has the card, a model APPROVED is refused."""
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, max_thinking_steps=2)
        self.think(h, step=1)
        self.think(h, step=2)
        res = h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertTrue(self.plan(h).halt)

    def test_a_late_human_decision_lifts_the_halt(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, max_thinking_steps=2)
        self.think(h, step=1)
        self.think(h, step=2)
        ui.resolve("REVISE", "표는 생략하세요")
        res = h.dispatch("get_current_plan", {"plan_id": "current"})
        plan = self.plan(h)
        self.assertIsNone(plan.halt)
        self.assertEqual(plan.guidance, "표는 생략하세요")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

    def test_page_decision_on_the_halt_card_approves_the_draft(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, breaker_repeat=2)
        self.think(h, step=1, task_list=TASKS)
        self.think(h, step=2, thought="same", task_list=TASKS)
        self.think(h, step=2, thought="same", task_list=TASKS)
        self.assertTrue(ui.live["draft"])
        ui.resolve("APPROVED")
        res = h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "APPROVED")

    def test_waiting_on_a_human_is_never_a_loop(self):
        """A chunked wait repeats the very same ASK_USER on purpose."""
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, breaker_repeat=2, approval_timeout=30, call_budget=1)
        self.finalize(h)
        for _ in range(4):
            res = h.dispatch("request_user_approval",
                             {"decision": "ASK_USER", "plan_summary": "s"})
            self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertIsNone(self.plan(h).halt)

    def test_rewording_the_goal_and_restarting_is_a_loop(self):
        """A model that reconsiders its goal and restarts at step 1 forks plans."""
        h = self.handler()
        self.think(h, goal="Q3 매출 보고서를 요약해서 팀장에게 보낸다")
        self.think(h, goal="Q3 매출 보고서를 요약하여 팀장에게 보낸다")
        res = self.think(h, goal="Q3 매출 보고서를 요약하고 팀장에게 보낸다")
        self.assertEqual(res["error_code"], "LOOP_HALTED")
        self.assertEqual(self.plan(h).halt["reason"], "respawn")

    def test_unrelated_new_plans_never_add_up(self):
        h = self.handler()
        for goal in ("주간 보고서 작성", "회의실 예약 확인", "서버 로그 분석", "신규 입사자 교육 자료"):
            res = self.think(h, goal=goal)
            self.assertEqual(res["plan_status"], "DRAFTING", goal)

    def test_a_plan_less_loop_ends_in_a_stop(self):
        """GOAL_NOT_MATCHED over and over has no plan to pause."""
        h = self.handler()
        self.think(h, goal="원래 목표")
        res = None
        for i in range(4):
            res = self.think(h, step=5 + i, goal=f"전혀 무관한 목표 {i}")
        self.assertEqual(res["error_code"], "LOOP_HALTED")
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("display_to_user", res)


# ---------------------------------------------------------------------------
# Scripted "thinking models": whatever they send, the server converges.
# ---------------------------------------------------------------------------


class TestAdversarialThinkingModels(LoopCase):
    """Each policy imitates a self-verification habit seen in the field. The bound is
    deliberately generous; what matters is that one exists for every policy."""

    BOUND = 25

    def drive(self, h, policy, limit=60):
        """Run a policy until the server hands control to a human or an answer."""
        res = None
        for i in range(limit):
            tool, args = policy(i, res)
            res = h.dispatch(tool, args)
            if res["next_action"] in ("STOP_AND_WAIT_FOR_USER", "ANSWER_USER"):
                return i + 1, res
            if res.get("error_code") == "LOOP_HALTED":
                return i + 1, res
        self.fail(f"no convergence within {limit} calls: {res}")

    def test_always_needs_one_more_step(self):
        h = self.handler()
        n, res = self.drive(h, lambda i, r: ("plan_and_think", {
            "goal": GOAL, "thought": f"wait, let me reconsider ({i})",
            "step_number": i + 1, "total_steps": i + 2, "need_more_thinking": True,
            "task_list": TASKS}))
        self.assertLessEqual(n, self.BOUND)

    def test_always_revises_its_last_step(self):
        h = self.handler()
        n, _ = self.drive(h, lambda i, r: ("plan_and_think", {
            "goal": GOAL, "thought": "actually, re-check", "step_number": i + 1,
            "total_steps": i + 2, "need_more_thinking": True,
            "revises_step": i if i else None}))
        self.assertLessEqual(n, self.BOUND)

    def test_replans_instead_of_asking(self):
        h = self.handler()
        n, _ = self.drive(h, lambda i, r: ("plan_and_think", {
            "goal": GOAL, "thought": "let me double check the plan", "step_number": i + 1,
            "total_steps": i + 1, "need_more_thinking": False,
            "task_list": TASKS if i % 2 else TASKS[:2]}))
        # One finalize, then turned-away re-plans count like an error streak.
        self.assertLessEqual(n, 5)
        self.assertEqual(len(self.plan(h).tasks), 2)  # the first list survived
        self.assertIn("다시 세우려는", self.plan(h).halt["text"])

    def test_keeps_replanning_a_completed_goal(self):
        """ANSWER_USER ignored over and over: the finished plan cannot be paused, so the
        sequence ends in a plain STOP instead."""
        h = self.handler()
        self.approve(h)
        self.run_all(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "보고"})
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        res = None
        for i in range(1, 8):
            res = self.think(h, thought=f"I must plan before answering ({i})")
            if res.get("error_code") == "LOOP_HALTED":
                break
        self.assertEqual(res["error_code"], "LOOP_HALTED")
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertLessEqual(i, 4)
        self.assertEqual(len(h.store.load().plans), 1)

    def test_the_loop_signal_never_reaches_the_model(self):
        h = self.handler()
        self.finalize(h)
        res = self.finalize(h)
        self.assertNotIn("_loop_signal", res)

    def test_replans_instead_of_reporting_completion(self):
        h = self.handler()
        self.approve(h)
        self.run_all(h)
        n, res = self.drive(h, lambda i, r: ("plan_and_think", {
            "goal": GOAL, "thought": "hmm, plan the summary", "step_number": i + 1,
            "total_steps": i + 2, "need_more_thinking": i % 2 == 0,
            "task_list": ["최종 답변 작성"]}))
        self.assertLessEqual(n, self.BOUND)
        plan = self.plan(h)
        self.assertEqual([t.title for t in plan.tasks], TASKS)
        self.assertTrue(all(t.result_log for t in plan.tasks))

    def test_replans_after_completion(self):
        h = self.handler()
        self.approve(h)
        self.run_all(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "보고"})
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        n, res = self.drive(h, lambda i, r: ("plan_and_think", {
            "goal": GOAL, "thought": "before answering I must plan", "step_number": 1,
            "total_steps": 1, "need_more_thinking": True}))
        self.assertEqual(n, 1)
        self.assertEqual(res["next_action"], "ANSWER_USER")

    def test_spams_execution_before_approval(self):
        h = self.handler()
        self.finalize(h)
        n, res = self.drive(h, lambda i, r: ("update_task_progress", {
            "task_id": 1, "status": "IN_PROGRESS"}))
        self.assertLessEqual(n, self.BOUND)
        self.assertEqual(self.plan(h).tasks[0].status, "PENDING")

    def test_bounded_in_blocking_mode_too(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, call_budget=1, approval_timeout=1)
        n, _ = self.drive(h, lambda i, r: ("plan_and_think", {
            "goal": GOAL, "thought": "wait", "step_number": i + 1, "total_steps": i + 2,
            "need_more_thinking": True, "task_list": TASKS}))
        self.assertLessEqual(n, self.BOUND)


# ---------------------------------------------------------------------------
# The words the model reads: no contradictions, no invitations to loop.
# ---------------------------------------------------------------------------


AGENTS_MD = (ROOT / "agents.md").read_text(encoding="utf-8")
_EMPHATIC = re.compile(r"\b(MUST|NEVER|ALWAYS|STOP|ONLY|MANDATORY|IMMEDIATELY|ABSOLUTE)\b")
# Phrases that were removed in 1.16 because each one either contradicts another
# instruction the model receives, or invites one more round of thinking (D25).
_BANNED = (
    "ANY user request", "answering anything", "Think step-by-step", "step-by-step",
    "One idea per call", "raise this number", "No exceptions", "OBEY IT LITERALLY",
)


def _all_tool_text(**kw) -> str:
    parts = []
    for tool in build_tool_definitions(**kw):
        parts.append(tool["description"])
        for prop in tool["inputSchema"]["properties"].values():
            parts.append(prop.get("description", ""))
    return "\n".join(parts)


class TestPromptHygiene(unittest.TestCase):
    MODES = [
        dict(model_profile=p, approval_mode=m, blocking=b, auto_advance=a)
        for p in ("standard", "reasoning")
        for m, b in (("chunked", True), ("return", True), ("trust_heartbeat", True),
                     ("chunked", False))
        for a in (True, False)
    ]

    def test_no_banned_phrase_in_any_configuration(self):
        for mode in self.MODES:
            text = _all_tool_text(**mode)
            for phrase in _BANNED:
                self.assertNotIn(phrase, text, f"{phrase!r} in {mode}")
        for phrase in _BANNED:
            self.assertNotIn(phrase, AGENTS_MD, phrase)
            self.assertNotIn(phrase, INSTRUCTIONS, phrase)

    def test_blocking_modes_never_tell_the_model_to_report_a_decision(self):
        for mode in self.MODES:
            if not mode["blocking"]:
                continue
            tools = {t["name"]: t for t in build_tool_definitions(**mode)}
            text = tools["request_user_approval"]["description"]
            self.assertNotIn("Then STOP", text)
            self.assertIn("Never send APPROVED", text)

    def test_only_the_chat_mode_asks_the_model_to_report(self):
        tools = {t["name"]: t for t in build_tool_definitions(blocking=False)}
        self.assertIn('decision = "APPROVED"', tools["request_user_approval"]["description"])

    def test_return_mode_says_end_your_turn_not_call_again(self):
        tools = {t["name"]: t for t in build_tool_definitions(approval_mode="return")}
        text = tools["request_user_approval"]["description"]
        self.assertIn("end your turn", text)
        self.assertNotIn("call again at once", text)

    def test_reasoning_tool_text_is_calm(self):
        text = _all_tool_text(model_profile="reasoning")
        self.assertLessEqual(len(_EMPHATIC.findall(text)), 18)

    def test_agents_md_is_short_and_calm(self):
        self.assertLessEqual(len(_EMPHATIC.findall(AGENTS_MD)), 6)
        self.assertLess(len(AGENTS_MD), 4200)

    def test_every_drafting_hint_names_one_tool(self):
        tools = ("plan_and_think", "request_user_approval", "update_task_progress",
                 "get_current_plan")
        for budget_end, steps in ((0, 1), (8, 3), (8, 7), (2, 1)):
            plan = Plan(plan_id="p", goal="g", plan_status="DRAFTING",
                        step_budget_end=budget_end)
            from planning.models import ThinkingStep
            plan.thinking_steps = [ThinkingStep(step_number=i, thought="t")
                                   for i in range(1, steps + 1)]
            _, hint = resolve_next_action(plan)
            named = {t for t in tools if t in hint}
            self.assertEqual(named, {"plan_and_think"}, hint)

    def test_answer_user_is_named_as_an_exception_everywhere(self):
        for mode in self.MODES:
            tools = {t["name"]: t for t in build_tool_definitions(**mode)}
            self.assertIn("ANSWER_USER", tools["plan_and_think"]["description"])
        self.assertIn("ANSWER_USER", AGENTS_MD)
        self.assertIn("ANSWER_USER", INSTRUCTIONS)

    def test_the_three_copies_of_the_prompt_agree(self):
        """agents.md is canonical; README and the Phase 3 manual embed it verbatim."""
        body = AGENTS_MD.strip()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        phase3 = (ROOT / "docs" / "phase3-anythingllm-agent-prompt.md").read_text(
            encoding="utf-8")
        self.assertIn(body, readme)
        self.assertIn(body, phase3)


# ---------------------------------------------------------------------------
# Field telemetry and the page
# ---------------------------------------------------------------------------


class TestTelemetry(LoopCase):
    def test_initialize_records_the_client(self):
        h = self.handler()
        proto = McpProtocol(h, build_tool_definitions(), "planning-mcp", "2.0.0")
        proto.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"clientInfo": {"name": "zed", "version": "0.2"}}})
        self.think(h, thought="Wait, let me reconsider. Actually, 다시 생각해 보면")
        connected = self.events("client_connected")
        self.assertEqual(connected[0]["client_name"], "zed")
        step = self.events("thinking_step")[0]
        self.assertEqual(step["client"], "zed 0.2")
        # "Wait", "let me reconsider", "Actually", "다시 생각"
        self.assertEqual(step["reconsider"], 4)
        self.assertIn("gap_sec", step)

    def test_the_instructions_no_longer_say_answering_anything(self):
        h = self.handler()
        proto = McpProtocol(h, build_tool_definitions(), "planning-mcp", "2.0.0")
        out = proto.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {}})
        self.assertIn("ANSWER_USER means write the answer", out["result"]["instructions"])

    def test_reconsider_markers(self):
        self.assertEqual(reconsider_markers("Wait. Hmm, actually let me re-check."), 4)
        self.assertEqual(reconsider_markers("보고서를 찾는다"), 0)
        self.assertEqual(reconsider_markers("잠깐, 재검토가 필요하다"), 2)


class TestLoopReport(LoopCase):
    """tools/loop_report.py turns the audit log into a per-plan loop verdict."""

    def test_a_halted_plan_is_reported(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import loop_report
        h = self.handler(max_thinking_steps=2)
        self.think(h, step=1, thought="wait, reconsider")
        self.think(h, step=2)
        report = loop_report.summarize(loop_report.load(self.state_dir / "audit.jsonl"))
        plan = next(iter(report["plans"].values()))
        self.assertTrue(plan["looped"])
        self.assertEqual(plan["halts"], ["thinking_budget(2)"])
        self.assertGreaterEqual(plan["reconsider"], 2)
        self.assertIn("thinking_budget", loop_report.render(report))

    def test_a_clean_run_is_reported_clean(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import loop_report
        h = self.handler()
        self.approve(h)
        self.run_all(h)
        report = loop_report.summarize(loop_report.load(self.state_dir / "audit.jsonl"))
        self.assertFalse(any(p["looped"] for p in report["plans"].values()))
        self.assertIn("No plan shows signs of a loop", loop_report.render(report))


class TestHaltPageTemplate(unittest.TestCase):
    """The halt card, asserted against the template (no socket)."""

    def test_the_halt_card_exists_and_is_routed(self):
        self.assertIn("function haltCard(d)", _PAGE)
        self.assertIn("if(d.phase==='HALT')return haltCard(d);", _PAGE)

    def test_the_draft_decides_the_buttons(self):
        self.assertIn("if(d.draft)", _PAGE)
        self.assertIn("이 초안으로 승인", _PAGE)
        self.assertIn("haltLabel(false)", _PAGE)

    def test_the_continue_button_says_whether_it_carries_a_direction(self):
        self.assertIn("의견 전달 후 계속", _PAGE)
        self.assertIn("if(PHASE[id]==='HALT')", _PAGE)

    def test_the_agent_note_is_shown_and_rerenders(self):
        self.assertIn("에이전트 추가 의견", _PAGE)
        self.assertIn("(x.agent_note||'').length", _PAGE)


class TestHaltAtTheStore(LoopCase):
    def test_a_halt_entry_keeps_its_phase_and_draft_flag(self):
        store = ApprovalStore(self.state_dir)
        rid = store.publish("p", "g", "d", [], "fp", PHASE_HALT, "why", draft=True)
        entry = store.entry(rid)
        self.assertEqual(entry["phase"], PHASE_HALT)
        self.assertTrue(entry["draft"])

    def test_a_halt_decision_carries_no_task_scope(self):
        store = ApprovalStore(self.state_dir)
        rid = store.publish("p", "g", "d", [], "fp", PHASE_HALT, "why")
        self.assertTrue(store.record_decision(rid, "REVISE", "방향", {"1": "x"}, "TASKS"))
        verdict = store.claim(rid)
        self.assertEqual(verdict.scope, "PLAN")
        self.assertEqual(verdict.task_comments, {})
        self.assertEqual(verdict.comment, "방향")

    def test_agent_note_is_attached_to_an_open_request_only(self):
        store = ApprovalStore(self.state_dir)
        rid = store.publish("p", "g", "d", [], "fp")
        store.set_agent_note(rid, "다시 생각해 보니")
        self.assertEqual(store.entry(rid)["agent_note"], "다시 생각해 보니")


class TestConfigDefaults(unittest.TestCase):
    def test_max_active_plans_default_is_twenty(self):
        self.assertEqual(Config(state_dir=Path(".")).max_active_plans, 20)

    def test_profile_budgets(self):
        self.assertEqual(Config(state_dir=Path(".")).thinking_budget, 8)
        self.assertEqual(
            Config(state_dir=Path("."), model_profile="reasoning").thinking_budget, 2)
        self.assertEqual(
            Config(state_dir=Path("."), max_thinking_steps=5).thinking_budget, 5)
        self.assertEqual(
            Config(state_dir=Path("."), max_thinking_steps=-1).thinking_budget, 0)

    def test_env(self):
        import os
        from unittest import mock
        env = {
            "PLANNING_MCP_MODEL_PROFILE": "reasoning",
            "PLANNING_MCP_MAX_THINKING_STEPS": "4",
            "PLANNING_MCP_LOOP_BREAKER": "false",
            "PLANNING_MCP_BREAKER_CALLS": "20",
            "PLANNING_MCP_BREAKER_REPEAT": "5",
            "PLANNING_MCP_BREAKER_ERROR_STREAK": "6",
            "PLANNING_MCP_BREAKER_RESPAWN": "4",
            "PLANNING_MCP_REPLAN_COOLDOWN": "0",
        }
        with mock.patch.dict(os.environ, env):
            cfg = Config.from_env()
        self.assertEqual(cfg.model_profile, "reasoning")
        self.assertEqual(cfg.thinking_budget, 4)
        self.assertFalse(cfg.loop_breaker)
        self.assertEqual((cfg.breaker_calls, cfg.breaker_repeat, cfg.breaker_error_streak,
                          cfg.breaker_respawn, cfg.replan_cooldown), (20, 5, 6, 4, 0))

    def test_a_typo_in_the_profile_falls_back_to_standard(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"PLANNING_MCP_MODEL_PROFILE": "thinking"}):
            self.assertEqual(Config.from_env().model_profile, "standard")

    def test_old_state_files_load(self):
        raw = {"plan_id": "p", "goal": "g", "plan_status": "DRAFTING"}
        plan = Plan.from_dict(raw)
        self.assertEqual((plan.draft_tasks, plan.step_budget_end, plan.halt, plan.guidance),
                         ([], 0, None, None))
        self.assertIs(plan.status, PlanStatus.DRAFTING)


if __name__ == "__main__":
    unittest.main()
