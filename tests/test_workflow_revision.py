"""The workflow diagram on the server side (3.3.0, ADVANCED_WORKFLOW_PLANNING).

Against the real approval store, in `return` mode: the server asks in the call that
records the plan, the human's click is recorded with `record_decision` exactly as the
page posts it, and the next call collects it. The guard itself is tested in
test_workflow_lint.py; this is what the server does with it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planning.approval import ApprovalServer, ApprovalStore  # noqa: E402
from planning.config import Config  # noqa: E402
from planning.handlers import PlanningHandlers  # noqa: E402
from planning.leniency import normalize  # noqa: E402
from planning.models import Plan  # noqa: E402
from planning.schemas import build_tool_definitions  # noqa: E402
from planning.store import Store  # noqa: E402

GOAL = "Q3 매출 보고서를 요약해 팀장에게 보낸다"
TASKS = ["보고서 파일 찾기", "분기별 매출 표 추출", "5줄 요약 작성"]
FLOW = (
    "flowchart TD\n"
    "  S([시작]) --> T1\n"
    "  T1 --> D1{\"Q3 데이터가 있나?\"}\n"
    "  D1 -->|예| T2\n"
    "  D1 -->|아니오| E([끝])\n"
    "  T2 --> T3 --> E\n"
)
FLOW2 = FLOW.replace("D1 -->|아니오| E([끝])", "D1 -->|아니오| Q2[\"Q2 데이터로 대신\"] --> T2")
BAD = "flowchart TD\n  T1 --> A[시작) --> T2 --> T3"


class WorkflowCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.ui = ApprovalServer(ApprovalStore(self.state_dir), open_browser=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def handler(self, **cfg) -> PlanningHandlers:
        cfg.setdefault("advanced_workflow", True)
        cfg.setdefault("approval_mode", "return")
        cfg.setdefault("approval_timeout", 1)
        return PlanningHandlers(
            Store(self.state_dir), Config(state_dir=self.state_dir, **cfg), approval_ui=self.ui
        )

    @staticmethod
    def submit(h, tasks=TASKS, **kw):
        args = {"goal": GOAL, "thought": "보고서를 찾아야 표를 뽑는다", "step_number": 1,
                "total_steps": 1, "need_more_thinking": False, "task_list": list(tasks)}
        args.update(kw)
        return h.dispatch("plan_and_think", args)

    @staticmethod
    def again(h, **kw):
        args = {"goal": GOAL, "thought": "고친다", "need_more_thinking": False}
        args.update(kw)
        return h.dispatch("plan_and_think", args)

    @staticmethod
    def wait(h):
        return h.dispatch("request_user_approval", {"decision": "ASK_USER"})

    def plan(self, h) -> Plan:
        state = h.store.load()
        return state.active_plan or max(state.plans.values(), key=lambda p: p.updated_at)

    def entry(self) -> dict:
        queue = self.ui.store.peek()
        self.assertEqual(len(queue), 1)
        return queue[0]

    def decide(self, decision="REVISE", comment="", tasks=None, scope="WORKFLOW",
               workflow=None, criteria=None) -> bool:
        return self.ui.store.record_decision(
            self.entry()["id"], decision, comment, tasks or {}, scope, None, criteria,
            workflow,
        )

    def audit(self, event) -> list[dict]:
        path = self.state_dir / "audit.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [r for r in map(json.loads, lines) if r.get("event") == event]


class TestOff(WorkflowCase):
    """ADVANCED_WORKFLOW_PLANNING=0 is 3.2: nothing advertised, kept, drawn or posted."""

    def test_the_tools_are_3_2s(self):
        self.assertEqual(build_tool_definitions(workflow=False), build_tool_definitions())
        text = json.dumps(build_tool_definitions(), ensure_ascii=False)
        self.assertNotIn("workflow", text)
        self.assertNotIn("Mermaid", text)

    def test_a_workflow_sent_anyway_is_ignored(self):
        h = self.handler(advanced_workflow=False)
        res = self.submit(h, workflow=FLOW)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertTrue(any("turned off" in n for n in res["input_notes"]))
        self.assertIsNone(self.plan(h).workflow)
        self.assertNotIn("workflow", self.entry())
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        plan = next(iter(raw["plans"].values()))
        self.assertFalse([k for k in plan if k.startswith(("workflow", "draft_workflow"))])

    def test_the_fingerprint_is_3_2s(self):
        on, off = self.handler(), self.handler(advanced_workflow=False)
        plan = Plan(plan_id="p", goal="g")
        self.assertEqual(on._fingerprint(plan), off._fingerprint(plan))
        plan.workflow = "flowchart TD\n  T1[\"a\"]"
        self.assertNotEqual(on._fingerprint(plan), off._fingerprint(Plan(plan_id="p", goal="g")))

    def test_the_setting(self):
        for env, want in (({}, False), ({"ADVANCED_WORKFLOW_PLANNING": "1"}, True),
                          ({"ADVANCED_WORKFLOW_PLANNING": "0"}, False),
                          ({"PLANNING_MCP_ADVANCED_WORKFLOW": "true"}, True),
                          ({"ADVANCED_WORKFLOW_PLANNING": "0",
                            "PLANNING_MCP_ADVANCED_WORKFLOW": "1"}, False)):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=False):
                for k in ("ADVANCED_WORKFLOW_PLANNING", "PLANNING_MCP_ADVANCED_WORKFLOW"):
                    if k not in env:
                        os.environ.pop(k, None)
                self.assertEqual(Config.from_env().advanced_workflow, want)


class TestAdvertised(unittest.TestCase):
    def test_one_rule_and_one_parameter(self):
        for profile in ("standard", "reasoning"):
            with self.subTest(profile=profile):
                tools = {t["name"]: t for t in build_tool_definitions(
                    model_profile=profile, workflow=True)}
                plan = tools["plan_and_think"]
                self.assertIn("workflow", plan["inputSchema"]["properties"])
                self.assertEqual(plan["description"].count("Mermaid flowchart"), 1)
                self.assertIn("flowchart TD", plan["inputSchema"]["properties"]["workflow"]
                              ["description"])
                # Only plan_and_think changes.
                off = {t["name"]: t for t in build_tool_definitions(model_profile=profile)}
                for name in ("request_user_approval", "update_task_progress",
                             "get_current_plan"):
                    self.assertEqual(tools[name], off[name])

    def test_the_closing_line_stays_last(self):
        plan = build_tool_definitions(workflow=True)[0]["description"]
        self.assertTrue(plan.endswith("Do not execute anything or answer the user while planning."))

    def test_leniency(self):
        clean, notes = normalize("plan_and_think", {"mermaid": ["flowchart TD", "T1 --> T2"]})
        self.assertEqual(clean["workflow"], "flowchart TD\nT1 --> T2")
        clean, _ = normalize("plan_and_think", {"workflow": {"code": "flowchart TD"}})
        self.assertEqual(clean["workflow"], "flowchart TD")


class TestDrawing(WorkflowCase):
    def test_the_models_drawing_reaches_the_page(self):
        h = self.handler()
        res = self.submit(h, workflow=FLOW)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        plan = self.plan(h)
        self.assertEqual(plan.workflow_by, "agent")
        self.assertIn('T2["분기별 매출 표 추출"]', plan.workflow)
        wf = self.entry()["workflow"]
        self.assertEqual(wf["text"], plan.workflow)
        self.assertEqual({n["id"] for n in wf["nodes"]}, {"S", "T1", "D1", "T2", "E", "T3"})
        self.assertEqual(wf["by"], "agent")
        self.assertEqual(wf["changed"], [])
        self.assertEqual(self.entry()["fingerprint"], h._fingerprint(plan))

    def test_approved_it_runs_like_any_plan(self):
        h = self.handler()
        self.submit(h, workflow=FLOW)
        self.assertTrue(self.decide("APPROVED", scope="PLAN"))
        res = self.wait(h)
        self.assertEqual(res["plan_status"], "APPROVED")
        res = h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertTrue(res["ok"], res)
        self.assertNotIn("workflow", json.dumps(res, ensure_ascii=False))

    def test_missing_is_drawn_in_order(self):
        h = self.handler()
        res = self.submit(h)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertTrue(any("drew the tasks in order" in n for n in res["input_notes"]))
        plan = self.plan(h)
        self.assertEqual(plan.workflow_by, "server")
        self.assertIn("T2 --> T3", plan.workflow)

    def test_undrawable_is_sent_back_with_the_list_kept(self):
        h = self.handler()
        res = self.submit(h, workflow=BAD, done_when=[{"task_id": 2, "check": "표 4행이 있다"}])
        self.assertEqual(res["error_code"], "WORKFLOW_INVALID")
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertIn("never closed", res["message"])
        self.assertIn("T3 --> E", res["workflow_example"])
        self.assertIn("task_list is kept", res["next_action_hint"])
        self.assertEqual(self.ui.store.peek(), [])  # nothing went to the human
        self.assertEqual(self.plan(h).draft_tasks, TASKS)
        res = self.again(h, workflow=FLOW)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        plan = self.plan(h)
        self.assertEqual([t.title for t in plan.tasks], TASKS)
        self.assertEqual(plan.tasks[1].done_when, "표 4행이 있다")
        self.assertEqual(plan.workflow_by, "agent")

    def test_after_the_retries_the_server_draws_it(self):
        h = self.handler(workflow_retries=2)
        self.assertEqual(self.submit(h, workflow=BAD)["error_code"], "WORKFLOW_INVALID")
        self.assertEqual(self.again(h, workflow=BAD)["error_code"], "WORKFLOW_INVALID")
        res = self.again(h, workflow=BAD)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        plan = self.plan(h)
        self.assertEqual((plan.workflow_by, plan.workflow_raw), ("server", BAD))
        wf = self.entry()["workflow"]
        self.assertEqual((wf["by"], wf["raw"]), ("server", BAD))
        self.assertEqual(len(self.audit("workflow_refused")), 2)
        self.assertEqual(self.audit("workflow_drawn_by_server")[-1]["reason"], "invalid")

    def test_no_retries_configured(self):
        h = self.handler(workflow_retries=0)
        res = self.submit(h, workflow=BAD)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(self.plan(h).workflow_by, "server")

    def test_a_draft_workflow_is_submitted_with_its_draft(self):
        h = self.handler()
        h.dispatch("plan_and_think", {"goal": GOAL, "thought": "초안", "step_number": 1,
                                      "total_steps": 2, "need_more_thinking": True,
                                      "task_list": TASKS, "workflow": FLOW})
        res = self.again(h, step_number=2)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        self.assertEqual(self.plan(h).workflow_by, "agent")

    def test_repairs_are_told_to_the_model(self):
        h = self.handler()
        res = self.submit(h, workflow="```mermaid\n" + FLOW.replace("-->", "->") + "```")
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        notes = " ".join(res["input_notes"])
        self.assertIn("fence", notes)
        self.assertIn("not a Mermaid arrow", notes)

    def test_chat_mode_shows_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = PlanningHandlers(Store(Path(tmp)), Config(
                state_dir=Path(tmp), advanced_workflow=True, blocking_approval=False))
            res = self.submit(h, workflow=FLOW)
            self.assertIn("```mermaid\nflowchart TD", res["display_to_user"])


class TestWorkflowRevision(WorkflowCase):
    def sent_back(self, h, workflow=None, tasks=None, comment=""):
        self.submit(h, workflow=FLOW)
        self.assertTrue(self.decide(workflow=workflow or {"D1": "데이터가 없으면 Q2로 대신"},
                                    tasks=tasks, comment=comment))
        return self.wait(h)

    def test_the_model_is_told_what_was_said_where(self):
        h = self.handler()
        res = self.sent_back(h)
        self.assertEqual((res["plan_status"], res["revision_scope"]), ("DRAFTING", "WORKFLOW"))
        self.assertEqual(res["workflow_comments"], [
            {"node": "D1", "label": "Q3 데이터가 있나?", "user_comment": "데이터가 없으면 Q2로 대신"}
        ])
        self.assertIn("workflow=<the whole revised flowchart>", res["next_action_hint"])
        self.assertIn("T1..T3", res["next_action_hint"])
        self.assertIn("flowchart TD", res["workflow"])
        applied = self.audit("late_decision_applied")[-1]
        self.assertEqual((applied["scope"], applied["workflow_comments"]),
                         ("WORKFLOW", {"D1": "데이터가 없으면 Q2로 대신"}))

    def test_a_redrawn_workflow_goes_back_with_its_changes_marked(self):
        h = self.handler()
        self.sent_back(h)
        res = self.again(h, workflow=FLOW2)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        plan = self.plan(h)
        self.assertIn("Q2", plan.workflow)
        self.assertEqual([t.title for t in plan.tasks], TASKS)
        self.assertIsNone(plan.pending_revision)
        wf = self.entry()["workflow"]
        self.assertIn("Q2", wf["changed"])
        self.assertNotIn("T1", wf["changed"])
        self.assertEqual(wf["asked"], {"D1": "데이터가 없으면 Q2로 대신"})
        # Approving it clears the marks.
        self.assertTrue(self.decide("APPROVED", scope="PLAN"))
        self.wait(h)
        self.assertIsNone(self.plan(h).workflow_prev)

    def test_no_workflow_or_the_same_one_does_not_answer_it(self):
        h = self.handler()
        self.sent_back(h)
        res = self.again(h)
        self.assertEqual(res["error_code"], "REVISION_INCOMPLETE")
        self.assertIn("workflow=", res["next_action_hint"])
        res = self.again(h, workflow=FLOW)
        self.assertEqual(res["error_code"], "REVISION_INCOMPLETE")
        self.assertIn("same", res["message"])

    def test_an_undrawable_answer_is_sent_back_and_the_request_kept(self):
        h = self.handler()
        self.sent_back(h)
        res = self.again(h, workflow=BAD)
        self.assertEqual(res["error_code"], "WORKFLOW_INVALID")
        self.assertTrue(self.plan(h).workflow_revision())
        self.assertEqual(self.again(h, workflow=FLOW2)["plan_status"], "AWAITING_APPROVAL")

    def test_with_a_task_comment_both_are_answered(self):
        h = self.handler()
        res = self.sent_back(h, tasks={"3": "요약은 3줄로"})
        self.assertEqual(res["revision_targets"],
                         [{"task_id": 3, "title": TASKS[2], "user_comment": "요약은 3줄로"}])
        self.assertIn('task_updates=[{"task_id": 3', res["next_action_hint"])
        res = self.again(h, workflow=FLOW2,
                         task_updates=[{"task_id": 3, "title": "3줄 요약 작성"}])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        plan = self.plan(h)
        self.assertEqual(plan.tasks[2].title, "3줄 요약 작성")
        self.assertIn('T3["3줄 요약 작성"]', plan.workflow)
        self.assertIn("Q2", plan.workflow)

    def test_a_comment_on_a_task_node_is_a_comment_on_the_task(self):
        h = self.handler()
        self.submit(h, workflow=FLOW)
        self.assertTrue(self.decide(workflow={"T2": "표는 원본 그대로"}))
        res = self.wait(h)
        self.assertEqual(res["revision_scope"], "TASKS")
        self.assertEqual(res["revision_targets"][0]["task_id"], 2)
        # And the task node follows the rewritten title.
        res = self.again(h, task_updates=[{"task_id": 2, "title": "원본 그대로 표 옮기기"}])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        self.assertIn('T2["원본 그대로 표 옮기기"]', self.plan(h).workflow)

    def test_a_node_that_was_not_shown_refuses_the_decision(self):
        h = self.handler()
        self.submit(h, workflow=FLOW)
        self.assertFalse(self.decide(workflow={"X9": "?"}))
        self.assertFalse(self.decide(workflow="D1"))
        self.assertIsNone(self.entry()["decision"])

    def test_the_whole_workflow_comment(self):
        h = self.handler()
        res = self.sent_back(h, workflow={"*": "검증 단계를 넣어 주세요"})
        self.assertEqual(res["workflow_comments"][0]["node"], "*")
        self.assertIn('the whole workflow: "검증 단계를 넣어 주세요"', res["next_action_hint"])

    def test_a_new_task_comes_with_a_full_task_list(self):
        h = self.handler()
        self.sent_back(h, workflow={"*": "검증 단계를 넣어 주세요"})
        flow4 = FLOW.replace("T2 --> T3", "T2 --> T3 --> T4")
        res = self.again(h, task_list=TASKS + ["숫자 검증"], workflow=flow4)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        self.assertEqual(len(self.plan(h).tasks), 4)
        self.assertTrue(self.audit("workflow_replanned"))

    def test_a_whole_plan_revision_carries_the_node_comments(self):
        h = self.handler()
        self.submit(h, workflow=FLOW)
        self.assertTrue(self.decide(scope="PLAN", workflow={"D1": "분기 조건은 빼 주세요"},
                                    comment="다시 짜 주세요"))
        res = self.wait(h)
        self.assertEqual(res["revision_scope"], "PLAN")
        self.assertIn("분기 조건은 빼 주세요", res["user_comment"])
        self.assertIn("다시 짜 주세요", res["user_comment"])

    def test_criteria_typed_with_it_are_applied(self):
        h = self.handler()
        self.submit(h, workflow=FLOW)
        self.assertTrue(self.decide(workflow={"D1": "Q2로 대신"}, criteria={"2": "표 4행"}))
        self.wait(h)
        self.assertEqual(self.plan(h).tasks[1].done_when, "표 4행")

    def test_a_late_click_is_collected_by_the_next_call(self):
        h = self.handler()
        self.submit(h, workflow=FLOW)
        self.assertTrue(self.decide(workflow={"D1": "Q2로 대신"}))
        res = self.again(h, workflow=FLOW2)  # collected at the top of this call
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL", res)
        self.assertIn("Q2", self.plan(h).workflow)


if __name__ == "__main__":
    unittest.main()
