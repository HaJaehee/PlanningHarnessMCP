"""3.0.0 - local repair: a FAILED task is rewritten where it stands.

Before 3.0 a failure sent the model back to draft the whole plan again, and the redraft
dropped the evidence of every task that had already finished - a break at step 3 cost
steps 1 and 2. Now the failure flags that one task exactly as a human's comment would
(the per-task review of 1.10), so the model rewrites it with `task_updates`, the
finished tasks keep their results, and the human re-approves only what changed.
Plan: docs/plan-3.0-verification-contract.md §5.

The guarantees pinned here:
- finished work is never touched by a repair, whatever the model sends;
- a repair that leaves the failed task as it is is not accepted;
- the human approves the change before anything runs again;
- re-planning the whole list is still possible, and still costs what it did in 2.0.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planning.approval import PHASE_PLAN  # noqa: E402
from planning.config import Config  # noqa: E402
from planning.handlers import PlanningHandlers  # noqa: E402
from planning.models import Plan  # noqa: E402
from planning.responses import render_completion_report, render_plan_for_user  # noqa: E402
from planning.schemas import build_tool_definitions  # noqa: E402
from planning.state_machine import resolve_next_action  # noqa: E402
from planning.store import Store  # noqa: E402
from test_server import FakeApprovalUI  # noqa: E402

GOAL = "분기 보고서를 정리한다"
TASKS = ["보고서 찾기", "PDF에서 표 추출", "요약 작성", "팀장에게 전송"]
WHY = "PDF가 스캔본이라 표를 읽을 수 없음"
NEW = "OCR로 텍스트를 뽑은 뒤 표 추출"
TOOLS = ("plan_and_think", "request_user_approval", "update_task_progress", "get_current_plan")


class RepairCase(unittest.TestCase):
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

    def plan(self, h) -> Plan:
        state = h.store.load()
        return state.active_plan or max(state.plans.values(), key=lambda p: p.updated_at)

    @staticmethod
    def think(h, **kw):
        args = {"goal": GOAL, "thought": "t", "step_number": 1, "total_steps": 1,
                "need_more_thinking": False}
        args.update(kw)
        return h.dispatch("plan_and_think", args)

    @staticmethod
    def ask(h):
        return h.dispatch("request_user_approval",
                          {"decision": "ASK_USER", "plan_summary": "보고서를 정리합니다."})

    @staticmethod
    def progress(h, task_id, status, log=None, **kw):
        args = {"task_id": task_id, "status": status, **kw}
        if log is not None:
            args["result_log"] = log
        return h.dispatch("update_task_progress", args)

    def failed_at_two(self, h=None, why=WHY, **think):
        """Approve the plan in chat, finish task 1, and fail task 2."""
        h = h or self.handler()
        self.think(h, task_list=list(TASKS), **think)
        self.ask(h)
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.progress(h, 1, "IN_PROGRESS")
        self.progress(h, 1, "DONE", "reports/q3.pdf 를 찾음 (12쪽)")
        return h, self.progress(h, 2, "FAILED", why)

    def repair(self, h, updates, **kw):
        return self.think(h, thought="다른 방법으로", task_updates=updates, **kw)

    def audit(self, event) -> list[dict]:
        path = self.state_dir / "audit.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if json.loads(l).get("event") == event]


class TestAFailureOpensOneTask(RepairCase):
    def test_the_failed_task_is_flagged_and_the_rest_named(self):
        h, res = self.failed_at_two()
        plan = self.plan(h)
        self.assertEqual(res["plan_status"], "BLOCKED")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")
        self.assertEqual(res["failed_task"], {"title": TASKS[1], "result_log": WHY})
        self.assertEqual(res["tasks_unchanged"], [1])
        self.assertEqual(plan.pending_revision,
                         {"targets": {"2": WHY}, "origin": "failure", "open": [3, 4]})
        self.assertTrue(plan.repairing())
        self.assertEqual((plan.revision_targets(), plan.revision_open()), ({2: WHY}, {3, 4}))

    def test_the_hint_hands_over_the_exact_argument(self):
        """A weak model copies a hint far more reliably than it composes one."""
        _, res = self.failed_at_two()
        hint = res["next_action_hint"]
        self.assertIn(f"Task 2 ('{TASKS[1]}') failed: \"{WHY}\"", hint)
        self.assertIn('task_updates=[{"task_id": 2, "title": "<another way to do this task>"}]',
                      hint)
        self.assertIn("do NOT send task_list unless the whole plan has to change", hint)
        self.assertIn("Task(s) 1 are finished and keep their results", hint)
        self.assertIn("The user approves the change before you continue", hint)
        self.assertEqual({t for t in TOOLS if t in hint}, {"plan_and_think"})

    def test_every_way_of_asking_gets_the_same_instruction(self):
        h, first = self.failed_at_two()
        hint = first["next_action_hint"]
        for res in (self.progress(h, 3, "IN_PROGRESS"), self.ask(h)):
            self.assertEqual(res["error_code"], "PLAN_BLOCKED")
            self.assertEqual(res["next_action_hint"], hint)
        back = h.dispatch("get_current_plan", {"plan_id": first["plan_id"]})
        self.assertEqual(back["next_action_hint"], hint)

    def test_a_thinking_step_keeps_the_flag_and_names_one_tool(self):
        h, _ = self.failed_at_two()
        res = self.think(h, step_number=5, need_more_thinking=True)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertTrue(self.plan(h).repairing())
        self.assertIn('task_updates=[{"task_id": 2', res["next_action_hint"])
        self.assertEqual({t for t in TOOLS if t in res["next_action_hint"]}, {"plan_and_think"})

    def test_a_long_reason_is_quoted_short_but_kept_whole(self):
        why = "원인: " + "표 구조가 깨져 있음. " * 40
        h, res = self.failed_at_two(why=why)
        self.assertLess(len(res["next_action_hint"]), 900)
        self.assertIn("…", res["next_action_hint"])
        self.repair(h, [{"task_id": 2, "title": NEW}])
        self.assertEqual(self.plan(h).tasks[1].failure_note, why.strip())

    def test_a_failure_with_no_reason_still_opens_the_task(self):
        h, res = self.failed_at_two(why=None)
        self.assertEqual(self.plan(h).revision_targets(), {2: "no reason was given"})
        self.assertIn("no reason was given", res["next_action_hint"])

    def test_the_first_task_failing_has_nothing_to_keep(self):
        h = self.handler()
        self.think(h, task_list=list(TASKS))
        self.ask(h)
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.progress(h, 1, "IN_PROGRESS")
        res = self.progress(h, 1, "FAILED", "폴더가 없음")
        self.assertNotIn("tasks_unchanged", res)
        self.assertNotIn("keep their results", res["next_action_hint"])
        self.assertEqual(self.plan(h).revision_open(), {2, 3, 4})


class TestRepairing(RepairCase):
    def test_only_the_failed_task_changes(self):
        h, _ = self.failed_at_two(self.handler(auto_ask=False))
        res = self.repair(h, [{"task_id": 2, "title": NEW}])
        tasks = self.plan(h).tasks
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["revised_tasks"], [2])
        self.assertIn("Finished tasks keep their results", res["message"])
        self.assertEqual((tasks[0].status, tasks[0].result_log),
                         ("DONE", "reports/q3.pdf 를 찾음 (12쪽)"))
        self.assertEqual(
            (tasks[1].title, tasks[1].status, tasks[1].result_log, tasks[1].previous_title,
             tasks[1].failure_note, tasks[1].revision_note),
            (NEW, "PENDING", None, TASKS[1], WHY, None))
        self.assertEqual([t.title for t in tasks[2:]], TASKS[2:])
        self.assertIsNone(self.plan(h).pending_revision)
        self.assertEqual(self.audit("task_repaired")[-1]["kept"], [1])

    def test_a_later_task_may_change_with_it(self):
        h, _ = self.failed_at_two()
        self.repair(h, [{"task_id": 2, "title": NEW},
                        {"task_id": 3, "title": "OCR 결과를 검토한 뒤 요약 작성"}])
        tasks = self.plan(h).tasks
        self.assertEqual(tasks[2].title, "OCR 결과를 검토한 뒤 요약 작성")
        self.assertEqual((tasks[2].previous_title, tasks[2].failure_note), (TASKS[2], None))

    def test_a_finished_task_is_out_of_reach(self):
        h, _ = self.failed_at_two()
        res = self.repair(h, [{"task_id": 1, "title": "보고서를 다시 찾기"},
                              {"task_id": 2, "title": NEW}])
        task = self.plan(h).tasks[0]
        self.assertEqual((task.title, task.status), (TASKS[0], "DONE"))
        self.assertTrue(any("finished tasks keep their results" in n
                            for n in res["input_notes"]))

    def test_leaving_the_failed_task_alone_is_not_a_repair(self):
        """Approved like that, the plan would hold a FAILED task nothing can finish."""
        h, _ = self.failed_at_two()
        before = self.plan(h).to_dict()["tasks"]
        res = self.repair(h, [{"task_id": 3, "title": "요약만 먼저 작성"}])
        self.assertEqual(res["error_code"], "REVISION_INCOMPLETE")
        self.assertIn("The failed task itself has to be rewritten", res["next_action_hint"])
        self.assertIn('task_updates=[{"task_id": 2', res["next_action_hint"])
        self.assertEqual(self.plan(h).to_dict()["tasks"], before)
        self.assertTrue(self.plan(h).repairing())

    def test_an_unknown_task_writes_nothing(self):
        h, _ = self.failed_at_two()
        res = self.repair(h, [{"task_id": 2, "title": NEW}, {"task_id": 9, "title": "x"}])
        self.assertEqual(res["error_code"], "TASK_NOT_FOUND")
        self.assertEqual(self.plan(h).tasks[1].status, "FAILED")

    def test_trying_the_same_way_again_is_a_repair(self):
        """The failure may have been passing; only a changed wording has a "before"."""
        h, _ = self.failed_at_two()
        res = self.repair(h, [{"task_id": 2, "title": TASKS[1] + "."}])
        task = self.plan(h).tasks[1]
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual((task.status, task.previous_title, task.failure_note),
                         ("PENDING", None, WHY))

    def test_the_bare_new_wording_is_read_as_the_failed_task(self):
        h, _ = self.failed_at_two()
        res = self.repair(h, [NEW])
        self.assertEqual(self.plan(h).tasks[1].title, NEW)
        self.assertTrue(any("Task 2 is the one that failed" in n for n in res["input_notes"]))

    def test_execution_stays_locked_until_the_human_approves(self):
        h, _ = self.failed_at_two()
        self.repair(h, [{"task_id": 2, "title": NEW}])
        res = self.progress(h, 2, "IN_PROGRESS")
        self.assertEqual(res["error_code"], "PLAN_NOT_APPROVED")


class TestAfterTheHumanApproves(RepairCase):
    def repaired(self):
        h, _ = self.failed_at_two()
        self.repair(h, [{"task_id": 2, "title": NEW}])
        self.ask(h)
        return h, h.dispatch("request_user_approval", {"decision": "APPROVED"})

    def test_work_resumes_at_the_repaired_task(self):
        h, res = self.repaired()
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["progress"], "1/4 done")
        self.assertEqual((res["next_task"]["task_id"], res["next_task"]["title"]), (2, NEW))
        self.assertEqual(res["next_task"]["failure_note"], WHY)
        self.assertIn(f'The earlier attempt at this step failed ("{WHY}")',
                      res["next_action_hint"])

    def test_the_plan_runs_to_the_completion_report_without_redoing_task_one(self):
        h, _ = self.repaired()
        self.assertTrue(self.progress(h, 1, "DONE", "다시 했다고 주장")["ok"])  # idempotent
        self.assertEqual(self.plan(h).tasks[0].result_log, "reports/q3.pdf 를 찾음 (12쪽)")
        self.progress(h, 2, "IN_PROGRESS")
        for tid in (2, 3, 4):
            res = self.progress(h, tid, "DONE", f"태스크 {tid} 결과를 out{tid}.txt 에 저장")
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        report = self.ask(h)["display_to_user"]
        self.assertIn(f"   ✕ 이전 시도 실패: {WHY}", report)
        self.assertIn("   -> reports/q3.pdf 를 찾음 (12쪽)", report)

    def test_a_second_failure_of_the_same_step_is_repaired_again(self):
        h, _ = self.repaired()
        self.progress(h, 2, "IN_PROGRESS")
        res = self.progress(h, 2, "FAILED", "OCR 도구가 없음")
        self.assertEqual(res["tasks_unchanged"], [1])
        self.repair(h, [{"task_id": 2, "title": "표를 직접 옮겨 적기"}])
        task = self.plan(h).tasks[1]
        self.assertEqual((task.title, task.failure_note, task.previous_title),
                         ("표를 직접 옮겨 적기", "OCR 도구가 없음", NEW))


class TestAPickSurvivesReApproval(RepairCase):
    """Found in the browser check of 3.0, and older than it: approving a plan a second
    time sent no pick for a choice already made (the page only offers an undecided
    one), and "no pick" was read as "the recommendation" - so the human's B became A.
    A repair makes a second approval routine, so it could no longer stay hidden."""

    ALT = "표를 손으로 옮겨 적기"

    def picked_b(self, **cfg):
        h = self.handler(**cfg)
        self.think(h, task_list=list(TASKS),
                   alternatives=[{"task_id": 2, "title": self.ALT, "reason": "느리지만 확실"}])
        self.ask(h)
        h.dispatch("request_user_approval", {"decision": "APPROVED", "choices": {"2": "B"}})
        self.assertEqual(self.plan(h).tasks[1].title, self.ALT)
        return h

    def test_after_a_repair(self):
        h = self.picked_b()
        self.progress(h, 1, "IN_PROGRESS")
        self.progress(h, 1, "DONE", "reports/q3.pdf 를 찾음 (12쪽)")
        self.progress(h, 2, "DONE", "표 3개를 tables.txt 에 옮겨 적음 (42행)")
        self.progress(h, 3, "FAILED", "요약 템플릿을 열 수 없음")
        self.repair(h, [{"task_id": 3, "title": "템플릿 없이 요약 작성"}])
        self.ask(h)
        res = h.dispatch("request_user_approval", {"decision": "APPROVED"})
        task = self.plan(h).tasks[1]
        self.assertEqual((task.title, task.chosen, task.status), (self.ALT, 1, "DONE"))
        self.assertNotIn(TASKS[1], json.dumps(res, ensure_ascii=False))

    def test_finished_work_is_not_re_decided_even_if_a_pick_arrives(self):
        h = self.picked_b()
        self.progress(h, 1, "IN_PROGRESS")
        self.progress(h, 1, "DONE", "reports/q3.pdf 를 찾음 (12쪽)")
        self.progress(h, 2, "DONE", "표 3개를 tables.txt 에 옮겨 적음 (42행)")
        self.progress(h, 3, "FAILED", "요약 템플릿을 열 수 없음")
        self.repair(h, [{"task_id": 3, "title": "템플릿 없이 요약 작성"}])
        self.ask(h)
        h.dispatch("request_user_approval", {"decision": "APPROVED", "choices": {"2": "A"}})
        self.assertEqual(self.plan(h).tasks[1].title, self.ALT)

    def test_after_an_approval_that_expired(self):
        h = self.picked_b(approval_ttl=60)
        state = h.store.load()
        state.active_plan.updated_at = "2020-01-01T00:00:00+09:00"
        h.store.save(state)
        res = self.ask(h)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        task = self.plan(h).tasks[1]
        self.assertEqual((task.title, task.chosen), (self.ALT, 1))


class TestWhatTheHumanSees(RepairCase):
    def test_the_page_shows_what_failed_and_what_replaces_it(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui)
        self.think(h, task_list=list(TASKS))
        self.ask(h)
        self.progress(h, 1, "IN_PROGRESS")
        self.progress(h, 1, "DONE", "reports/q3.pdf 를 찾음 (12쪽)")
        self.progress(h, 2, "FAILED", WHY)
        self.repair(h, [{"task_id": 2, "title": NEW}])
        ui.decision = None
        self.ask(h)
        entry = ui.opened[-1]
        self.assertEqual(entry["phase"], PHASE_PLAN)
        done, fixed = entry["tasks"][0], entry["tasks"][1]
        self.assertEqual((done["status"], done["result_log"]),
                         ("DONE", "reports/q3.pdf 를 찾음 (12쪽)"))
        self.assertEqual((fixed["title"], fixed["previous_title"], fixed["failure_note"],
                          fixed["status"]), (NEW, TASKS[1], WHY, "PENDING"))

    def test_the_chat_text_says_only_the_failed_task_was_re_planned(self):
        h, _ = self.failed_at_two()
        self.repair(h, [{"task_id": 2, "title": NEW}])
        text = self.ask(h)["display_to_user"]
        self.assertIn(f"2. {NEW}", text)
        self.assertIn(f"   이전: {TASKS[1]}", text)
        self.assertIn(f"   ✕ 이전 시도 실패: {WHY}", text)
        self.assertIn("실패한 태스크만 다시 계획했습니다. 이미 끝난 1개 태스크의 결과는 그대로 둡니다.",
                      text)

    def test_the_human_may_still_send_the_repair_back(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, auto_ask=False)
        self.think(h, task_list=list(TASKS))
        self.ask(h)
        self.progress(h, 1, "IN_PROGRESS")
        self.progress(h, 1, "DONE", "reports/q3.pdf 를 찾음 (12쪽)")
        self.progress(h, 2, "FAILED", WHY)
        self.repair(h, [{"task_id": 2, "title": NEW}])
        ui.decision, ui.task_comments, ui.scope = "REVISE", {"2": "OCR은 쓰지 마세요"}, "TASKS"
        res = self.ask(h)
        plan = self.plan(h)
        self.assertEqual(res["revision_scope"], "TASKS")
        self.assertFalse(plan.repairing())
        self.assertEqual(plan.revision_targets(), {2: "OCR은 쓰지 마세요"})
        self.assertEqual(plan.tasks[0].status, "DONE")


class TestThePre30PathStillWorks(RepairCase):
    def test_a_whole_new_list_is_accepted_at_its_old_cost(self):
        """Sometimes the whole approach was wrong. Still allowed - and still measurable."""
        h, _ = self.failed_at_two()
        res = self.think(h, task_list=["원본 엑셀을 요청", "엑셀에서 표 추출", "요약 작성"])
        plan = self.plan(h)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual([t.status for t in plan.tasks], ["PENDING"] * 3)
        self.assertIsNone(plan.pending_revision)
        self.assertTrue(any("Only task 2 failed" in n for n in res["input_notes"]))
        self.assertEqual(self.audit("repair_ignored")[-1]["targets"], [2])

    def test_turned_off_a_failure_asks_for_a_re_plan(self):
        h, res = self.failed_at_two(self.handler(local_repair=False))
        self.assertIsNone(self.plan(h).pending_revision)
        self.assertNotIn("tasks_unchanged", res)
        self.assertIn("re-plan around this failure", res["next_action_hint"])
        refused = self.repair(h, [{"task_id": 2, "title": NEW}])
        self.assertEqual(refused["error_code"], "REVISION_NOT_REQUESTED")

    def test_turned_off_the_tool_text_does_not_offer_it(self):
        def plan_tool(**kw):
            return {t["name"]: t for t in build_tool_definitions(**kw)}["plan_and_think"]
        for profile in ("standard", "reasoning"):
            on = plan_tool(model_profile=profile)
            off = plan_tool(model_profile=profile, local_repair=False)
            self.assertIn("failed", on["description"].lower())
            self.assertNotIn("failed", off["description"].lower())
            self.assertIn("Finished tasks keep their results", on["description"])
            self.assertIn("or a task failed",
                          on["inputSchema"]["properties"]["task_updates"]["description"])
            self.assertNotIn("failed",
                             off["inputSchema"]["properties"]["task_updates"]["description"])


class TestRepairAndTheContract(RepairCase):
    CHECK = "보고서의 매출 표가 tables.csv 로 저장된다"

    def test_the_models_criterion_goes_with_the_wording_it_described(self):
        h, _ = self.failed_at_two(done_when=[{"task_id": 2, "check": self.CHECK}])
        self.repair(h, [{"task_id": 2, "title": NEW}])
        self.assertIsNone(self.plan(h).tasks[1].done_when)

    def test_unless_it_is_sent_again_with_the_repair(self):
        h, _ = self.failed_at_two(done_when=[{"task_id": 2, "check": self.CHECK}])
        self.repair(h, [{"task_id": 2, "title": NEW}],
                    done_when=[{"task_id": 2, "check": self.CHECK}])
        self.assertEqual(self.plan(h).tasks[1].done_when, self.CHECK)

    def test_a_criterion_the_human_wrote_survives_the_repair(self):
        ui = FakeApprovalUI(decision="APPROVED", criteria={"2": self.CHECK})
        h = self.handler(ui)
        self.think(h, task_list=list(TASKS))
        self.ask(h)
        self.progress(h, 1, "IN_PROGRESS")
        self.progress(h, 1, "DONE", "reports/q3.pdf 를 찾음 (12쪽)")
        self.progress(h, 2, "FAILED", WHY)
        self.repair(h, [{"task_id": 2, "title": NEW}])
        task = self.plan(h).tasks[1]
        self.assertEqual((task.done_when, task.done_when_by), (self.CHECK, "user"))


class TestPersistenceAndRendering(RepairCase):
    def test_the_open_repair_survives_a_restart(self):
        h, res = self.failed_at_two()
        again = self.handler()
        back = again.dispatch("get_current_plan", {"plan_id": res["plan_id"]})
        self.assertEqual(back["next_action_hint"], res["next_action_hint"])
        self.assertEqual(self.repair(again, [{"task_id": 2, "title": NEW}])["plan_status"],
                         "AWAITING_APPROVAL")

    def test_a_hand_edited_marker_cannot_wedge_the_plan(self):
        plan = Plan.from_dict({"plan_id": "p", "goal": "g", "plan_status": "BLOCKED",
                               "pending_revision": {"targets": {"x": 1}, "origin": "failure",
                                                    "open": ["a", None, "3"]}})
        self.assertEqual((plan.revision_targets(), plan.revision_open()), ({}, {3}))
        action, _ = resolve_next_action(plan, None)
        self.assertEqual(action, "CALL_PLAN_AND_THINK")

    def test_a_2_0_blocked_plan_gets_the_2_0_instruction(self):
        """A plan that failed before the upgrade has no marker: it re-plans as before."""
        plan = Plan.from_dict({"plan_id": "p", "goal": "g", "plan_status": "BLOCKED",
                               "tasks": [{"task_id": 1, "title": "a", "status": "FAILED"}]})
        _, hint = resolve_next_action(plan, None)
        self.assertIn("re-plan around this failure", hint)

    def test_a_plan_that_never_failed_renders_as_before(self):
        h = self.handler()
        self.think(h, task_list=list(TASKS))
        text = render_plan_for_user(self.plan(h))
        self.assertNotIn("실패", text)
        self.assertNotIn("실패", render_completion_report(self.plan(h)))


if __name__ == "__main__":
    unittest.main()
