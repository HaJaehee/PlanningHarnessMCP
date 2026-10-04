"""Phase 4 Part D - server unit tests. No LLM required.

    python -m unittest discover -s tests -v

Run these before spending corporate-LLM turns on the behavioral matrix.
"""

from __future__ import annotations

import datetime
import dataclasses
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from unittest import mock
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The server logs expected warnings (lock files, quarantined state, autoapprove) that would
# otherwise drown the test output. The tests assert on responses and the audit log instead.
logging.disable(logging.CRITICAL)

from planning import approval  # noqa: E402
from planning.approval import _PAGE, ApprovalServer, ApprovalStore, Verdict  # noqa: E402
from planning.config import CALL_BUDGET_SEC, SDK_REQUEST_TIMEOUT_SEC, Config  # noqa: E402
from planning.filelock import exclusive  # noqa: E402
from planning.handlers import PlanningHandlers  # noqa: E402
from planning.leniency import UNMATCHED_TITLES_KEY, normalize  # noqa: E402
from planning.protocol import McpProtocol  # noqa: E402
from planning.responses import render_plan_for_user  # noqa: E402
from planning.schemas import TOOL_DEFINITIONS, build_tool_definitions  # noqa: E402
from planning.store import Store, title_key  # noqa: E402

REQUIRED_FIELDS = ("ok", "plan_status", "next_action", "next_action_hint")


class HandlerTestCase(unittest.TestCase):
    """Base: a fresh handler over a throwaway state dir."""

    # False pins a class to the flow before 3.1, in which the model asks for approval
    # with its own request_user_approval call (PLANNING_MCP_AUTO_ASK=false - still
    # supported). The classes that set it assert that call sequence step by step; the
    # default, where the server asks at the transition, is tests/test_gate_and_run.py.
    AUTO_ASK = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        # Blocking approval is the production default; the unit suite drives the
        # two-phase path, so it is disabled here and exercised in TestBlockingApproval.
        self.config = Config(
            state_dir=self.state_dir, blocking_approval=False, auto_ask=self.AUTO_ASK
        )
        self.store = Store(self.state_dir, max_plans=self.config.max_plans)
        self.h = PlanningHandlers(self.store, self.config)

    def legacy(self) -> None:
        """Rebuild the handler for the model-asks flow, for a single test (see AUTO_ASK)."""
        self.config = dataclasses.replace(self.config, auto_ask=False)
        self.h = PlanningHandlers(self.store, self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- helpers -------------------------------------------------------
    def think(self, **kwargs):
        args = {
            "goal": "Summarize the Q3 sales report.",
            "thought": "thinking",
            "step_number": 1,
            "total_steps": 2,
            "need_more_thinking": True,
        }
        args.update(kwargs)
        return self.h.dispatch("plan_and_think", args)

    def approve_flow(self, tasks=None):
        """Drive a plan all the way to APPROVED."""
        tasks = tasks or ["Find the file", "Read it", "Write the summary"]
        self.think(step_number=1, need_more_thinking=True)
        self.think(step_number=2, need_more_thinking=False, task_list=tasks)
        self.h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "I will do X."}
        )
        return self.h.dispatch("request_user_approval", {"decision": "APPROVED"})

    def do_task(self, task_id, log=None, **kw):
        """Run one task the way the server now demands: start, then finish with evidence."""
        self.h.dispatch(
            "update_task_progress", {"task_id": task_id, "status": "IN_PROGRESS", **kw}
        )
        return self.h.dispatch("update_task_progress", {
            "task_id": task_id, "status": "DONE",
            "result_log": log or f"saved output of task {task_id} to /tmp/out{task_id}.txt",
            **kw,
        })

    def assertContract(self, res):  # noqa: N802
        for field in REQUIRED_FIELDS:
            self.assertIn(field, res, f"missing {field} in {res}")


# ===========================================================================
# D1 - leniency layer
# ===========================================================================


class TestLeniency(unittest.TestCase):
    def test_status_aliases(self):
        for raw in ("done", "Done", "completed", "finished", "완료"):
            clean, _ = normalize("update_task_progress", {"task_id": 1, "status": raw})
            self.assertEqual(clean["status"], "DONE", raw)
        for raw in ("in progress", "started", "doing", "running", "IN_PROGRESS"):
            clean, _ = normalize("update_task_progress", {"task_id": 1, "status": raw})
            self.assertEqual(clean["status"], "IN_PROGRESS", raw)
        for raw in ("fail", "failed", "error", "실패"):
            clean, _ = normalize("update_task_progress", {"task_id": 1, "status": raw})
            self.assertEqual(clean["status"], "FAILED", raw)

    def test_decision_aliases(self):
        for raw in ("yes", "y", "ok", "approve", "승인", "네", "진행"):
            clean, _ = normalize("request_user_approval", {"decision": raw})
            self.assertEqual(clean["decision"], "APPROVED", raw)
        for raw in ("no", "cancel", "reject", "취소", "아니오", "하지마"):
            clean, _ = normalize("request_user_approval", {"decision": raw})
            self.assertEqual(clean["decision"], "REJECTED", raw)
        for raw in ("revise", "change", "수정"):
            clean, _ = normalize("request_user_approval", {"decision": raw})
            self.assertEqual(clean["decision"], "REVISE", raw)

    def test_bool_coercion(self):
        for raw in ("false", "False", 0, "0", "no"):
            clean, _ = normalize("plan_and_think", {"need_more_thinking": raw})
            self.assertIs(clean["need_more_thinking"], False, raw)
        for raw in ("true", "True", 1, "yes"):
            clean, _ = normalize("plan_and_think", {"need_more_thinking": raw})
            self.assertIs(clean["need_more_thinking"], True, raw)

    def test_int_coercion(self):
        clean, _ = normalize("plan_and_think", {"step_number": "3", "total_steps": 4.0})
        self.assertEqual(clean["step_number"], 3)
        self.assertEqual(clean["total_steps"], 4)
        clean, _ = normalize("update_task_progress", {"task_id": "task 2"})
        self.assertEqual(clean["task_id"], 2)

    def test_task_list_from_string(self):
        clean, _ = normalize("plan_and_think", {"task_list": "a\nb\nc"})
        self.assertEqual(clean["task_list"], ["a", "b", "c"])
        clean, _ = normalize("plan_and_think", {"task_list": "a, b, c"})
        self.assertEqual(clean["task_list"], ["a", "b", "c"])

    def test_task_list_from_objects(self):
        clean, _ = normalize(
            "plan_and_think", {"task_list": [{"title": "a"}, {"task": "b"}, {"name": "c"}]}
        )
        self.assertEqual(clean["task_list"], ["a", "b", "c"])

    def test_task_list_numbering_stripped(self):
        clean, _ = normalize("plan_and_think", {"task_list": ["1. a", "2) b", "- c", "• d"]})
        self.assertEqual(clean["task_list"], ["a", "b", "c", "d"])

    def test_task_updates_shapes(self):
        """Every unambiguous way a model might express 'change task 3 to X'."""
        want = [{"task_id": 3, "title": "표를 그대로 삽입"}]
        for raw in (
            [{"task_id": 3, "title": "표를 그대로 삽입"}],
            {"task_id": 3, "title": "표를 그대로 삽입"},           # not wrapped in a list
            {"3": "표를 그대로 삽입"},                             # object map
            '[{"task_id": 3, "title": "표를 그대로 삽입"}]',        # JSON in a string
            [{"task_id": "3", "title": "3. 표를 그대로 삽입"}],     # string id, numbered title
            [{"id": 3, "name": "표를 그대로 삽입"}],                # alternate key names
        ):
            clean, _ = normalize("plan_and_think", {"task_updates": raw})
            self.assertEqual(clean["task_updates"], want, f"failed on {raw!r}")

    def test_task_updates_drops_unusable_entries(self):
        """An entry with no id or no title is dropped - never guessed at."""
        clean, _ = normalize("plan_and_think", {"task_updates": [
            {"task_id": 2, "title": "제대로 된 항목"},
            {"title": "아이디 없음"},
            {"task_id": 5},
            "그냥 문자열",
            {"task_id": 0, "title": "0번은 없음"},
        ]})
        self.assertEqual(clean["task_updates"], [{"task_id": 2, "title": "제대로 된 항목"}])

    def test_task_updates_duplicates_collapse(self):
        clean, _ = normalize("plan_and_think", {"task_updates": [
            {"task_id": 2, "title": "첫 번째"},
            {"task_id": 2, "title": "두 번째"},
        ]})
        self.assertEqual(clean["task_updates"], [{"task_id": 2, "title": "두 번째"}])

    def test_task_updates_unreadable_is_ignored_not_fatal(self):
        clean, notes = normalize("plan_and_think", {"task_updates": "완전히 엉뚱한 문장"})
        self.assertNotIn("task_updates", clean)
        self.assertTrue(any("task_updates" in n for n in notes))

    def test_unknown_keys_dropped_and_remapped(self):
        clean, notes = normalize(
            "update_task_progress", {"taskId": 1, "status": "DONE", "bogus": 9}
        )
        self.assertEqual(clean["task_id"], 1)
        self.assertNotIn("bogus", clean)
        self.assertTrue(any("bogus" in n for n in notes))

    def test_non_dict_arguments(self):
        clean, notes = normalize("plan_and_think", "not a dict")
        self.assertEqual(clean, {})
        self.assertTrue(notes)

    # ---- normalize must never raise, whatever the model sends -----------
    HOSTILE_ARGS = [
        {"task_list": [{"title": {"nested": "dict"}}]},        # title value is a dict
        {"task_list": [[1, 2], None, {"x": 1}]},               # list of junk
        {"task_list": {"2": ["a"], "1": None}},                # dict of junk values
        {"task_list": [{"title": ["a", "b"]}]},                # title value is a list
        {"step_number": [1, 2, 3]},                             # int field is a list
        {"step_number": {"n": 1}},                             # int field is a dict
        {"need_more_thinking": {"maybe": True}},               # bool field is a dict
        {"status": ["DONE"]},                                  # enum field is a list
        {"decision": {"x": 1}},                                # enum field is a dict
        {"task_list": "a" * 100_000},                          # very long string
        {"goal": None, "thought": None},                       # explicit nulls
        {"task_id": "1" * 5000},                               # absurd number string
        {"result_log": {"nested": {"deep": {"deeper": 1}}}},   # nested object scalar
        {"": "empty key", "\n": "newline key"},                # odd keys
        {"revises_step": float("nan")},                        # NaN
        {"total_steps": float("inf")},                         # infinity
        {"task_list": []},                                     # empty list
        {"task_list": [""]},                                   # list of empty string
    ]

    def test_normalize_never_raises_on_hostile_input(self):
        for tool in ("plan_and_think", "update_task_progress",
                     "request_user_approval", "get_current_plan"):
            for args in self.HOSTILE_ARGS:
                try:
                    clean, notes = normalize(tool, args)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"normalize({tool}, {args!r}) raised {type(exc).__name__}: {exc}")
                self.assertIsInstance(clean, dict)
                self.assertIsInstance(notes, list)

    def test_normalize_non_string_keys(self):
        # JSON cannot produce these, but a crafted client could.
        clean, notes = normalize("update_task_progress", {5: "x", None: "y", "status": "DONE"})
        self.assertIsInstance(clean, dict)
        self.assertEqual(clean.get("status"), "DONE")

    def test_nan_and_inf_ints_are_rejected(self):
        clean, _ = normalize("plan_and_think", {"step_number": float("nan"),
                                                "total_steps": float("inf")})
        self.assertNotIn("step_number", clean)
        self.assertNotIn("total_steps", clean)


# ===========================================================================
# D2 - schema guard rails
# ===========================================================================


class TestGuardRails(HandlerTestCase):
    def test_missing_task_list_on_final_step(self):
        self.think(step_number=1)
        res = self.think(step_number=2, need_more_thinking=False)
        self.assertContract(res)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "MISSING_TASK_LIST")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")
        self.assertIn("task_list", res["next_action_hint"])

    def test_empty_task_list_on_final_step(self):
        res = self.think(need_more_thinking=False, task_list=[])
        self.assertEqual(res["error_code"], "MISSING_TASK_LIST")

    def test_step_number_jump_is_normalized(self):
        self.think(step_number=1)
        res = self.think(step_number=5)
        self.assertTrue(res["ok"])
        self.assertEqual(res["recorded_step"], 2)
        self.assertTrue(any("corrected" in n for n in res.get("input_notes", [])))

    def test_step_number_repeat_is_normalized(self):
        self.think(step_number=1)
        self.think(step_number=2)
        res = self.think(step_number=2)
        self.assertEqual(res["recorded_step"], 3)

    def test_revises_step_supersedes_while_drafting(self):
        self.think(step_number=1)
        self.think(step_number=2)
        res = self.think(step_number=3, revises_step=2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "DRAFTING")
        current = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertIn("superseded_steps", current)

    def test_revises_step_does_not_reopen_a_finished_plan(self):
        """1.16 (D25): a finalized task list is not reopened by the model's second
        thoughts - before, this put the plan back to DRAFTING, the lap a thinking model
        could run forever. Only the human's 'request changes' reopens it."""
        self.legacy()
        self.think(step_number=1)
        self.think(step_number=2, need_more_thinking=False, task_list=["a", "b"])
        res = self.think(step_number=3, revises_step=2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        self.assertEqual([t["title"] for t in res["tasks"]], ["a", "b"])

    def test_revises_step_out_of_range(self):
        self.think(step_number=1)
        res = self.think(step_number=2, revises_step=99)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "INVALID_STEP")

    def test_oversized_task_list_is_truncated(self):
        res = self.think(
            need_more_thinking=False, task_list=[f"task {i}" for i in range(40)]
        )
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["tasks"]), self.config.max_tasks)
        self.assertTrue(any("kept the first" in n for n in res["input_notes"]))

    def test_task_not_found(self):
        self.approve_flow()
        res = self.h.dispatch("update_task_progress", {"task_id": 99, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "TASK_NOT_FOUND")
        # The hint no longer spells an id out; it points at the one field that carries
        # one, so the recovery route has to actually be there.
        self.assertIn("next_task", res["next_action_hint"])
        self.assertEqual(res["next_task"]["task_id"], 1)

    def test_get_current_plan_with_no_plan(self):
        res = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "NONE")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

    def test_missing_plan_summary(self):
        self.legacy()
        self.think(need_more_thinking=False, task_list=["a", "b"])
        res = self.h.dispatch("request_user_approval", {"decision": "ASK_USER"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "MISSING_PLAN_SUMMARY")

    def test_invalid_decision(self):
        self.think()
        res = self.h.dispatch("request_user_approval", {"decision": "maybe later"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "INVALID_DECISION")

    def test_missing_scalars_are_tolerated(self):
        res = self.h.dispatch("plan_and_think", {"thought": "just thinking"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["recorded_step"], 1)


# ===========================================================================
# D3 - state machine invariants
# ===========================================================================


class TestStateMachine(HandlerTestCase):
    AUTO_ASK = False

    def test_execution_blocked_while_drafting(self):
        self.think()
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_NOT_APPROVED")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_execution_blocked_while_awaiting_approval(self):
        self.think(need_more_thinking=False, task_list=["a", "b"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "x"})
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_NOT_APPROVED")

    def test_ask_user_returns_stop_and_display(self):
        self.think(need_more_thinking=False, task_list=["Find the file", "Read it"])
        res = self.h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "I will do X."}
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("display_to_user", res)
        self.assertIn("Find the file", res["display_to_user"])

    def test_full_happy_path(self):
        res = self.approve_flow(["a", "b"])
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["next_task"]["task_id"], 1)

        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(res["plan_status"], "IN_EXECUTION")
        res = self.do_task(1, log="wrote the summary to /tmp/a.txt")
        self.assertEqual(res["progress"], "1/2 done")
        self.assertEqual(res["next_task"]["task_id"], 2)

        # The last DONE no longer completes the plan on the model's say-so (1.9.0).
        res = self.do_task(2, log="emailed the summary to the team lead")
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

        self.h.dispatch("request_user_approval",
                        {"decision": "ASK_USER", "plan_summary": "both tasks finished"})
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertEqual(res["plan_status"], "COMPLETED")
        self.assertEqual(res["next_action"], "ANSWER_USER")

    def test_rejection_cancels_plan(self):
        self.think(need_more_thinking=False, task_list=["a"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "x"})
        res = self.h.dispatch(
            "request_user_approval", {"decision": "REJECTED", "user_comment": "아니, 하지마"}
        )
        self.assertEqual(res["plan_status"], "CANCELLED")
        self.assertEqual(res["next_action"], "ANSWER_USER")
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(res["error_code"], "PLAN_CANCELLED")

    def test_revise_requires_second_approval(self):
        self.think(need_more_thinking=False, task_list=["a", "email it"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "x"})
        res = self.h.dispatch(
            "request_user_approval",
            {"decision": "REVISE", "user_comment": "이메일은 보내지 마"},
        )
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["revision_count"], 1)
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

        # Execution must still be locked after the revision.
        blocked = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(blocked["error_code"], "PLAN_NOT_APPROVED")

        # Re-planning lands back at the gate, not at execution.
        res = self.think(need_more_thinking=False, task_list=["a"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_approve_while_drafting_is_refused(self):
        self.think()
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_NOT_READY")

    def test_failed_task_blocks_forward_progress(self):
        self.approve_flow(["a", "b", "c"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE"})
        self.h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        res = self.h.dispatch(
            "update_task_progress",
            {"task_id": 2, "status": "FAILED", "result_log": "file not found"},
        )
        self.assertEqual(res["plan_status"], "BLOCKED")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")
        self.assertEqual(res["failed_task"]["title"], "b")
        self.assertEqual(res["failed_task"]["result_log"], "file not found")

        res = self.h.dispatch("update_task_progress", {"task_id": 3, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_BLOCKED")

    def test_replan_after_block_requires_new_approval(self):
        self.approve_flow(["a", "b"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "FAILED"})
        res = self.think(need_more_thinking=False, task_list=["a2", "b2"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(res["error_code"], "PLAN_NOT_APPROVED")

    def test_out_of_order_start_is_redirected_not_rejected(self):
        self.approve_flow(["a", "b", "c"])
        res = self.h.dispatch("update_task_progress", {"task_id": 3, "status": "IN_PROGRESS"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["next_task"]["task_id"], 1)

    def test_done_is_idempotent(self):
        self.approve_flow(["a", "b"])
        self.do_task(1)
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["progress"], "1/2 done")

    def test_done_without_in_progress_is_refused(self):
        """Claiming a task is finished without ever starting it is now rejected (1.9.0)."""
        self.approve_flow(["a", "b"])
        res = self.h.dispatch("update_task_progress", {
            "task_id": 1, "status": "DONE", "result_log": "pretended to do the work"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "TASK_NOT_STARTED")
        self.assertEqual(res["progress"], "0/2 done")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("done_without_start", audit)

    def test_approved_without_ask_is_refused(self):
        """APPROVED must be preceded by ASK_USER on the SAME plan version."""
        self.think(need_more_thinking=False, task_list=["a", "b"])
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_NOT_REQUESTED")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("stale_approval_refused", audit)

    def test_stale_approval_after_replacement_is_refused(self):
        """Session 1 asks about plan A; session 2 replaces it; session 1's belated
        approval must NOT unlock plan B - the human never saw B."""
        self.think(goal="A: 보고서 요약", need_more_thinking=False, task_list=["찾기", "요약"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        b = self.think(goal="B: 전 직원 메일 발송", step_number=1,
                       need_more_thinking=False, task_list=["초안", "발송"])
        pid = b["plan_id"]
        res = self.h.dispatch(
            "request_user_approval",
            {"decision": "APPROVED", "user_comment": "승인", "plan_id": pid},
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_NOT_REQUESTED")
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")  # still locked
        blocked = self.h.dispatch(
            "update_task_progress", {"task_id": 1, "status": "IN_PROGRESS", "plan_id": pid}
        )
        self.assertEqual(blocked["error_code"], "PLAN_NOT_APPROVED")
        # Re-asking (showing the CURRENT plan) and then approving works.
        self.h.dispatch("request_user_approval",
                        {"decision": "ASK_USER", "plan_summary": "s2", "plan_id": pid})
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED", "plan_id": pid})
        self.assertEqual(res["plan_status"], "APPROVED")

    def test_revise_then_approve_without_reask_is_refused(self):
        """The B2 shortcut (revise -> approve without re-showing) is now server-blocked."""
        self.think(need_more_thinking=False, task_list=["a", "email"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        self.h.dispatch("request_user_approval", {"decision": "REVISE", "user_comment": "no email"})
        self.think(need_more_thinking=False, task_list=["a"])
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_NOT_REQUESTED")

    def test_concurrent_sessions_keep_separate_plans(self):
        """Two goals in flight must coexist, not evict one another (1.8.0)."""
        a = self.think(goal="A세션: 회의실 예약", need_more_thinking=False, task_list=["a1", "a2"])
        b = self.think(goal="B세션: 보고서 요약", need_more_thinking=False, task_list=["b1"])
        self.assertNotEqual(a["plan_id"], b["plan_id"])
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["plans"]), 2)
        goals = sorted(p["goal"] for p in raw["plans"].values())
        self.assertEqual(goals, ["A세션: 회의실 예약", "B세션: 보고서 요약"])
        # Each session's own tasks survive intact.
        by_goal = {p["goal"]: [t["title"] for t in p["tasks"]] for p in raw["plans"].values()}
        self.assertEqual(by_goal["A세션: 회의실 예약"], ["a1", "a2"])
        self.assertEqual(by_goal["B세션: 보고서 요약"], ["b1"])

    def test_same_goal_continues_the_same_plan(self):
        """Repeating the goal - which the prompt tells the model to do - routes back."""
        first = self.think(goal="같은 목표", step_number=1, need_more_thinking=True)
        second = self.think(goal="같은 목표", step_number=2, need_more_thinking=False,
                            task_list=["x"])
        self.assertEqual(first["plan_id"], second["plan_id"])

    def test_ambiguous_calls_are_refused_with_a_directory(self):
        self.think(goal="A", need_more_thinking=False, task_list=["a"])
        self.think(goal="B", need_more_thinking=False, task_list=["b"])
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_AMBIGUOUS")
        self.assertEqual(len(res["active_plans"]), 2)

    def test_plan_id_routes_to_the_right_plan(self):
        a = self.think(goal="A", need_more_thinking=False, task_list=["a작업"])
        b = self.think(goal="B", need_more_thinking=False, task_list=["b작업"])
        for pid in (a["plan_id"], b["plan_id"]):
            self.h.dispatch("request_user_approval",
                            {"decision": "ASK_USER", "plan_summary": "s", "plan_id": pid})
            self.h.dispatch("request_user_approval", {"decision": "APPROVED", "plan_id": pid})
        res = self.h.dispatch("update_task_progress",
                              {"task_id": 1, "status": "IN_PROGRESS", "plan_id": b["plan_id"]})
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_id"], b["plan_id"])
        self.assertEqual(res["next_task"]["title"], "b작업")
        # A is untouched.
        a_now = self.h.dispatch("get_current_plan", {"plan_id": a["plan_id"]})
        self.assertEqual(a_now["tasks"][0]["status"], "PENDING")

    def test_hints_name_the_plan_id_when_several_are_active(self):
        """A model that copies the hint verbatim must still route correctly."""
        self.think(goal="A", need_more_thinking=False, task_list=["a"])
        b = self.think(goal="B", need_more_thinking=False, task_list=["b"])
        self.assertIn(b["plan_id"], b["next_action_hint"])


class TestTaskCompletionEnforcement(HandlerTestCase):
    """The reported field failure: a small model marks every task DONE without doing
    the work, then reports the plan finished (1.9.0)."""

    def approved(self, n=4):
        return self.approve_flow([f"작업{i}" for i in range(1, n + 1)])

    def test_batch_marking_is_refused(self):
        """DONE fired at every task in a row must not complete anything.

        Since 1.16 the fourth refusal in a row is also a loop, so the circuit breaker
        pauses the plan on it - either way nothing is marked done.
        """
        self.approved()
        for tid in (1, 2, 3, 4):
            res = self.h.dispatch("update_task_progress", {"task_id": tid, "status": "DONE"})
            self.assertFalse(res["ok"], f"task {tid} accepted a bare DONE")
            expected = "LOOP_HALTED" if tid == 4 else "TASK_NOT_STARTED"
            self.assertEqual(res["error_code"], expected)
        cur = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(cur["progress"], "0/4 done")
        self.assertNotEqual(cur["plan_status"], "COMPLETED")

    def test_done_out_of_order_is_refused(self):
        self.approved()
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        # Jump the queue: start+finish 3 while 1 is unfinished.
        res = self.h.dispatch("update_task_progress", {"task_id": 3, "status": "IN_PROGRESS"})
        self.assertEqual(res.get("next_task", {}).get("task_id"), 1)  # redirected, lenient
        res = self.h.dispatch("update_task_progress", {
            "task_id": 3, "status": "DONE", "result_log": "이것은 충분히 긴 결과 기록입니다"})
        self.assertFalse(res["ok"])
        self.assertIn(res["error_code"], ("TASK_NOT_STARTED", "TASK_OUT_OF_ORDER"))

    def test_evidence_free_logs_are_refused(self):
        # The evidence filter on its own: seven refusals in a row would otherwise trip
        # the circuit breaker, which has its own tests (TestLoopBreaker).
        self.h = PlanningHandlers(
            self.store, Config(state_dir=self.state_dir, blocking_approval=False,
                               loop_breaker=False)
        )
        self.approved(1)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        for bad in ("", "완료", "done", "OK", "성공적으로 완료했습니다", "작업1", "  완료  "):
            res = self.h.dispatch("update_task_progress", {
                "task_id": 1, "status": "DONE", "result_log": bad})
            self.assertFalse(res["ok"], f"{bad!r} was accepted as evidence")
            self.assertEqual(res["error_code"], "MISSING_RESULT_LOG")

    def test_terse_but_concrete_evidence_is_accepted(self):
        """Korean packs a real outcome into few characters - do not punish that."""
        self.approved(1)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.h.dispatch("update_task_progress", {
            "task_id": 1, "status": "DONE", "result_log": "매출 표 12행 추출"})
        self.assertTrue(res["ok"], res.get("message"))

    def test_result_log_repeating_the_title_is_refused(self):
        self.approve_flow(["q3 리포트 파일을 찾는다"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.h.dispatch("update_task_progress", {
            "task_id": 1, "status": "DONE", "result_log": "q3 리포트 파일을 찾는다"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "MISSING_RESULT_LOG")

    def test_last_done_awaits_human_verification(self):
        self.legacy()
        self.approved(2)
        self.do_task(1)
        res = self.do_task(2)
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("completion_pending", audit)

    def test_completion_report_shows_per_task_evidence(self):
        self.approved(2)
        self.do_task(1, log="q3.xlsx 를 /reports 에서 찾음")
        self.do_task(2, log="요약을 summary.md 로 저장함")
        res = self.h.dispatch("request_user_approval",
                              {"decision": "ASK_USER", "plan_summary": "둘 다 끝냈습니다"})
        report = res["display_to_user"]
        self.assertIn("완료 보고", report)
        self.assertIn("2개 태스크의 완료를 보고했습니다", report)
        self.assertIn("q3.xlsx 를 /reports 에서 찾음", report)
        self.assertIn("요약을 summary.md 로 저장함", report)
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")

    def test_human_confirmation_completes_the_plan(self):
        self.approved(1)
        self.do_task(1)
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertEqual(res["plan_status"], "COMPLETED")
        self.assertEqual(res["next_action"], "ANSWER_USER")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("completion_verified", audit)

    def test_human_rejecting_the_report_does_not_complete(self):
        self.approved(1)
        self.do_task(1)
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        res = self.h.dispatch("request_user_approval",
                              {"decision": "REJECTED", "user_comment": "실제로 안 됐음"})
        self.assertEqual(res["plan_status"], "CANCELLED")

    def test_human_asking_for_more_work_reopens_planning(self):
        self.approved(1)
        self.do_task(1)
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        res = self.h.dispatch("request_user_approval",
                              {"decision": "REVISE", "user_comment": "3번이 빠졌어요"})
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

    def test_tasks_cannot_be_touched_while_awaiting_verification(self):
        self.approved(1)
        self.do_task(1)
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "COMPLETION_PENDING")

    def test_hint_names_the_outstanding_tasks(self):
        """Anti-abandonment: the model must never lose sight of what is left."""
        self.approved(3)
        res = self.do_task(1)
        self.assertIn("2 of 3 tasks are still unfinished", res["next_action_hint"])
        self.assertIn("Do NOT tell the user", res["next_action_hint"])

    def test_completion_approval_can_be_disabled(self):
        cfg = Config(state_dir=self.state_dir, blocking_approval=False,
                     completion_approval=False)
        h = PlanningHandlers(Store(self.state_dir), cfg)
        h.dispatch("plan_and_think", {"goal": "g", "thought": "t", "step_number": 1,
                                      "total_steps": 1, "need_more_thinking": False,
                                      "task_list": ["작업"]})
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = h.dispatch("update_task_progress", {
            "task_id": 1, "status": "DONE", "result_log": "결과물을 /tmp/out 에 저장함"})
        self.assertEqual(res["plan_status"], "COMPLETED")

    def test_evidence_cannot_be_rewritten_after_the_human_saw_it(self):
        """The fingerprint covers result_log, so a late edit invalidates the approval."""
        self.approved(1)
        self.do_task(1, log="원래 증거: /tmp/a.txt 생성")
        before = self.h._fingerprint(self.h.store.load().active_plan)
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        pid = raw["active_plan_id"]
        raw["plans"][pid]["tasks"][0]["result_log"] = "조작된 증거"
        (self.state_dir / "plan_state.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        after = self.h._fingerprint(self.h.store.load().active_plan)
        self.assertNotEqual(before, after)


class TestPortConfiguration(unittest.TestCase):
    """Ports must be settable from the environment: AnythingLLM's MCP config sets `env`
    far more naturally than it sets `args`."""

    def env(self, **overrides):
        with mock.patch.dict(os.environ, overrides, clear=False):
            return Config.from_env()

    def test_approval_port_defaults_to_8765(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Config.from_env().approval_port, 8765)

    def test_approval_port_from_env(self):
        self.assertEqual(self.env(PLANNING_MCP_APPROVAL_PORT="8899").approval_port, 8899)

    def test_sse_port_and_host_from_env(self):
        cfg = self.env(PLANNING_MCP_SSE_PORT="9001", PLANNING_MCP_SSE_HOST="localhost")
        self.assertEqual(cfg.sse_port, 9001)
        self.assertEqual(cfg.sse_host, "localhost")

    def test_sse_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = Config.from_env()
            self.assertEqual(cfg.sse_port, 8931)
            self.assertEqual(cfg.sse_host, "127.0.0.1")

    def test_garbage_port_falls_back_to_the_default(self):
        self.assertEqual(self.env(PLANNING_MCP_APPROVAL_PORT="not-a-port").approval_port, 8765)

    def _run_sse(self, argv, **env):
        """Run server.main for the SSE transport without actually serving."""
        import server as server_module
        env = dict(env, PLANNING_MCP_BLOCKING_APPROVAL="false",
                   PLANNING_MCP_STATE_DIR=tempfile.mkdtemp())
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(server_module, "serve_sse") as served:
            server_module.main(["--transport", "sse", *argv])
        return served.call_args.kwargs

    def test_env_port_is_used_when_no_cli_flag(self):
        """The bug this guards: an argparse default would silently beat the env var."""
        kwargs = self._run_sse([], PLANNING_MCP_SSE_PORT="9100")
        self.assertEqual(kwargs["port"], 9100)

    def test_cli_flag_overrides_the_env(self):
        kwargs = self._run_sse(["--port", "9200"], PLANNING_MCP_SSE_PORT="9100")
        self.assertEqual(kwargs["port"], 9200)


class TestGoalDriftRouting(HandlerTestCase):
    """Goal drift mid-drafting must not fork a plan; the model is asked to pick (1.8.2)."""

    def step(self, goal, n, more=True, tasks=None, plan_id=None):
        a = {"goal": goal, "thought": "t", "step_number": n, "total_steps": 3,
             "need_more_thinking": more}
        if tasks is not None:
            a["task_list"] = tasks
        if plan_id is not None:
            a["plan_id"] = plan_id
        return self.h.dispatch("plan_and_think", a)

    def test_drifted_goal_while_continuing_is_refused_with_the_list(self):
        r1 = self.step("Q3 리포트를 요약한다", 1)
        r2 = self.step("Q3 리포트를 요약하고 정리한다", 2)   # drift
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["error_code"], "GOAL_NOT_MATCHED")
        self.assertEqual(r2["next_action"], "CALL_PLAN_AND_THINK")
        self.assertEqual([p["plan_id"] for p in r2["active_plans"]], [r1["plan_id"]])
        self.assertEqual(r2["active_plans"][0]["goal"], "Q3 리포트를 요약한다")
        # No second plan was created.
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["plans"]), 1)

    def test_picking_the_exact_goal_continues_the_plan(self):
        r1 = self.step("원래 목표", 1)
        r2 = self.step("드리프트된 목표", 2)
        exact = r2["active_plans"][0]["goal"]
        r3 = self.step(exact, 2, more=False, tasks=["작업"])
        self.assertTrue(r3["ok"])
        self.assertEqual(r3["plan_id"], r1["plan_id"])
        self.assertEqual(r3["plan_status"], "AWAITING_APPROVAL")

    def test_plan_id_continues_across_a_goal_change(self):
        r1 = self.step("목표A", 1)
        r2 = self.step("완전히 다른 표현", 2, more=False, tasks=["작업"], plan_id=r1["plan_id"])
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["plan_id"], r1["plan_id"])

    def test_step1_new_goal_creates_a_new_plan_even_with_actives(self):
        r1 = self.step("첫 대화", 1, more=False, tasks=["a"])
        r2 = self.step("둘째 대화", 1, more=False, tasks=["b"])   # step 1 = starting fresh
        self.assertTrue(r2["ok"])
        self.assertNotEqual(r2["plan_id"], r1["plan_id"])

    def test_trailing_punctuation_does_not_fork(self):
        r1 = self.step("파일을 찾는다", 1)
        r2 = self.step("파일을 찾는다.", 2, more=False, tasks=["찾기"])   # period only
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["plan_id"], r1["plan_id"])

    def test_drift_with_no_active_plans_just_starts_one(self):
        """step>1 but nothing active: recover by starting, don't error out."""
        r = self.step("갑자기 2단계", 2, more=False, tasks=["작업"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["plan_status"], "AWAITING_APPROVAL")

    def test_concurrent_drafts_still_route_by_goal(self):
        """Two conversations drafting at once: goal match must still isolate them."""
        a1 = self.step("대화A 목표", 1)
        b1 = self.step("대화B 목표", 1)
        self.assertNotEqual(a1["plan_id"], b1["plan_id"])
        # A continues with its exact goal — must hit A, not B, not a fork.
        a2 = self.step("대화A 목표", 2, more=False, tasks=["A작업"])
        self.assertEqual(a2["plan_id"], a1["plan_id"])
        # B drifts while continuing — ambiguous list, no fork.
        b2 = self.step("대화B 목표 살짝 다름", 2)
        self.assertEqual(b2["error_code"], "GOAL_NOT_MATCHED")
        self.assertEqual({p["plan_id"] for p in b2["active_plans"]},
                         {a1["plan_id"], b1["plan_id"]})

    def test_new_goal_never_inherits_an_approval(self):
        """A different goal must not ride on an approved plan's execution licence.

        1.4.0 enforced this by superseding the approved plan; since 1.8.0 the two plans
        simply coexist and the new one starts locked, so a concurrent session no longer
        loses its work either way.
        """
        self.approve_flow(["a", "b"])
        res = self.think(goal="다른 세션의 새 목표", step_number=1,
                         need_more_thinking=False, task_list=["hijack"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        blocked = self.h.dispatch(
            "update_task_progress",
            {"task_id": 1, "status": "IN_PROGRESS", "plan_id": res["plan_id"]},
        )
        self.assertEqual(blocked["error_code"], "PLAN_NOT_APPROVED")

    def test_revised_goal_updates_the_plan_and_keeps_the_history(self):
        """The user corrected their intent; the metadata must follow it (1.12.0)."""
        r1 = self.step("Q3 리포트를 요약한다", 1)
        r2 = self.h.dispatch("plan_and_think", {
            "goal": "Q3 리포트를 요약한다",
            "revised_goal": "Q4 리포트를 요약한다",
            "thought": "사용자가 Q4라고 정정했다", "step_number": 2, "total_steps": 3,
            "need_more_thinking": True,
        })
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["plan_id"], r1["plan_id"])   # corrected, not forked
        self.assertEqual(r2["goal"], "Q4 리포트를 요약한다")

        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["plans"]), 1)
        plan = raw["plans"][r1["plan_id"]]
        self.assertEqual(plan["goal"], "Q4 리포트를 요약한다")
        self.assertEqual(plan["original_goal"], "Q3 리포트를 요약한다")
        self.assertEqual(
            [(h["from"], h["to"]) for h in plan["goal_history"]],
            [("Q3 리포트를 요약한다", "Q4 리포트를 요약한다")],
        )
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("goal_revised", audit)

    def test_corrected_goal_routes_the_next_step(self):
        r1 = self.step("원래 목표", 1)
        self.h.dispatch("plan_and_think", {
            "goal": "원래 목표", "revised_goal": "정정된 목표", "thought": "t",
            "step_number": 2, "total_steps": 3, "need_more_thinking": True,
        })
        after = self.step("정정된 목표", 3, more=False, tasks=["작업"])
        self.assertTrue(after["ok"])
        self.assertEqual(after["plan_id"], r1["plan_id"])

    def test_the_old_goal_still_finds_the_plan_for_one_more_turn(self):
        """A model lagging one turn behind a correction must not be told to re-select."""
        r1 = self.step("원래 목표", 1)
        self.h.dispatch("plan_and_think", {
            "goal": "원래 목표", "revised_goal": "정정된 목표", "thought": "t",
            "step_number": 2, "total_steps": 3, "need_more_thinking": True,
        })
        lagging = self.step("원래 목표", 3, more=False, tasks=["작업"])
        self.assertTrue(lagging["ok"])
        self.assertEqual(lagging["plan_id"], r1["plan_id"])
        self.assertEqual(lagging["plan_status"], "AWAITING_APPROVAL")

    def test_revised_goal_in_both_fields_still_finds_the_plan(self):
        r1 = self.step("원래 목표", 1)
        r2 = self.h.dispatch("plan_and_think", {
            "goal": "정정된 목표", "revised_goal": "정정된 목표", "thought": "t",
            "step_number": 2, "total_steps": 3, "need_more_thinking": True,
        })
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["plan_id"], r1["plan_id"])
        self.assertEqual(r2["goal"], "정정된 목표")

    def test_revised_goal_with_no_plan_yet_just_starts_one(self):
        r = self.h.dispatch("plan_and_think", {
            "goal": "", "revised_goal": "정정된 목표", "thought": "t",
            "step_number": 1, "total_steps": 1, "need_more_thinking": False,
            "task_list": ["작업"],
        })
        self.assertTrue(r["ok"])
        res = self.h.dispatch("get_current_plan", {"plan_id": r["plan_id"]})
        self.assertEqual(res["goal"], "정정된 목표")
        self.assertNotIn("original_goal", res)   # nothing was revised

    def test_paraphrase_only_revision_is_not_recorded(self):
        """Trailing punctuation is not a correction; history must stay meaningful."""
        r1 = self.step("파일을 찾는다", 1)
        self.h.dispatch("plan_and_think", {
            "goal": "파일을 찾는다", "revised_goal": "파일을 찾는다.", "thought": "t",
            "step_number": 2, "total_steps": 2, "need_more_thinking": True,
        })
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["plans"][r1["plan_id"]]["goal_history"], [])

    def test_get_current_plan_reports_the_original_goal(self):
        r1 = self.step("원래 목표", 1)
        self.h.dispatch("plan_and_think", {
            "goal": "원래 목표", "revised_goal": "정정된 목표", "thought": "t",
            "step_number": 2, "total_steps": 3, "need_more_thinking": True,
        })
        res = self.h.dispatch("get_current_plan", {"plan_id": r1["plan_id"]})
        self.assertEqual(res["goal"], "정정된 목표")
        self.assertEqual(res["original_goal"], "원래 목표")
        self.assertEqual(res["goal_revisions"], 1)

    def test_revising_the_goal_mid_execution_warns_about_the_approval(self):
        """The human approved the OLD goal; the correction is taken but flagged."""
        self.approve_flow(["a", "b"])
        res = self.h.dispatch("plan_and_think", {
            "goal": "Summarize the Q3 sales report.",
            "revised_goal": "Summarize the Q4 sales report.",
            "thought": "사용자가 Q4라고 정정했다", "step_number": 3, "total_steps": 3,
            "need_more_thinking": True,
        })
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["goal"], "Summarize the Q4 sales report.")
        self.assertTrue(any("approved this plan under the previous goal" in n
                            for n in res["input_notes"]))
        # The correction really was persisted, not just described.
        stored = self.h.dispatch("get_current_plan", {"plan_id": res["plan_id"]})
        self.assertEqual(stored["goal"], "Summarize the Q4 sales report.")

    def test_separate_state_dirs_are_fully_isolated(self):
        other_dir = self.state_dir / "other"
        other = PlanningHandlers(
            Store(other_dir), Config(state_dir=other_dir, blocking_approval=False)
        )
        self.think(goal="세션1 목표", need_more_thinking=False, task_list=["x"])
        res = other.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "NONE")

    def test_plan_and_think_during_execution_redirects(self):
        self.approve_flow(["a", "b"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.think(step_number=1, need_more_thinking=False, task_list=["totally new"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "IN_EXECUTION")
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        self.assertEqual(res["tasks"][0]["title"], "a")  # original plan untouched

    def test_every_response_has_the_contract_fields(self):
        calls = [
            ("plan_and_think", {"goal": "g", "thought": "t", "step_number": 1,
                                "total_steps": 1, "need_more_thinking": False,
                                "task_list": ["a"]}),
            ("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"}),
            ("request_user_approval", {"decision": "APPROVED"}),
            ("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"}),
            ("update_task_progress", {"task_id": 1, "status": "DONE"}),
            ("get_current_plan", {"plan_id": "current"}),
            ("update_task_progress", {"task_id": 42, "status": "DONE"}),
            ("request_user_approval", {"decision": "nonsense"}),
            ("bogus_tool", {}),
        ]
        for name, args in calls:
            self.assertContract(self.h.dispatch(name, args))

    def test_unknown_tool_does_not_raise(self):
        res = self.h.dispatch("definitely_not_a_tool", {"x": 1})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "INTERNAL_ERROR")


# ===========================================================================
# D4 - persistence & robustness
# ===========================================================================


class TestPersistence(HandlerTestCase):
    def test_state_survives_a_new_handler_instance(self):
        self.approve_flow(["a", "b"])
        self.do_task(1)

        fresh = PlanningHandlers(Store(self.state_dir), self.config)
        res = fresh.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["progress"], "1/2 done")
        self.assertEqual(res["next_task"]["task_id"], 2)

    def test_corrupt_state_file_is_quarantined(self):
        self.think()
        (self.state_dir / "plan_state.json").write_text("{{{garbage", encoding="utf-8")
        fresh = PlanningHandlers(Store(self.state_dir), self.config)
        res = fresh.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "NONE")
        self.assertTrue(list(self.state_dir.glob("plan_state.corrupt.*.json")))

    def test_korean_text_round_trips(self):
        self.think(goal="한글 목표", thought="한글 생각", need_more_thinking=False,
                   task_list=["첫 번째 작업", "두 번째 작업"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "요약"})
        self.h.dispatch(
            "request_user_approval", {"decision": "REVISE", "user_comment": "이메일은 빼줘"}
        )
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        plan = next(iter(raw["plans"].values()))
        self.assertEqual(plan["approval"]["user_comment"], "이메일은 빼줘")
        self.assertEqual(plan["tasks"][0]["title"], "첫 번째 작업")

    def test_result_log_is_never_truncated_in_responses(self):
        """It used to be cut at 200 chars. The approval page is built from this same
        dict, so that asked a human to certify evidence ending in '...'."""
        evidence = "민원 접수 화면을 만들었습니다. " * 40  # ~800 chars
        self.approve_flow(["a"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.h.dispatch(
            "update_task_progress",
            {"task_id": 1, "status": "DONE", "result_log": evidence},
        )
        # update_task_progress stopped echoing the task list, so the surface that still
        # publishes evidence to the model is the recovery tool. The rule is unchanged:
        # wherever evidence is published it is published whole.
        res = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["tasks"][0]["result_log"], evidence.strip())
        self.assertNotIn("...", res["tasks"][0]["result_log"])

    def test_the_page_receives_the_same_untruncated_evidence(self):
        """What reaches the human must be what the store holds - the whole point."""
        evidence = "스키마를 정의했습니다. " * 40
        ui = FakeApprovalUI(decision=None)
        h = PlanningHandlers(
            Store(self.state_dir),
            Config(state_dir=self.state_dir, blocking_approval=True, approval_timeout=1),
            approval_ui=ui,
        )
        h.dispatch("plan_and_think", {"goal": "g", "thought": "t", "step_number": 1,
                                      "total_steps": 1, "need_more_thinking": False,
                                      "task_list": ["작업"]})
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        ui.resolve("APPROVED")  # the human clicks; re-asking would reuse the same entry
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE",
                                            "result_log": evidence})
        ui.decision = None  # capture the completion report instead of answering it
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "끝"})
        published = ui.opened[-1]
        self.assertEqual(published["phase"], "COMPLETION")
        self.assertEqual(published["tasks"][0]["result_log"], evidence.strip())

    def test_plan_pruning_keeps_the_active_plan(self):
        config = Config(state_dir=self.state_dir, max_plans=3, blocking_approval=False)
        h = PlanningHandlers(Store(self.state_dir, max_plans=3), config)
        for i in range(6):
            h.dispatch(
                "plan_and_think",
                {"goal": f"goal {i}", "thought": "t", "step_number": 1, "total_steps": 1,
                 "need_more_thinking": False, "task_list": ["a"]},
            )
            h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
            h.dispatch("request_user_approval", {"decision": "REJECTED"})
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(raw["plans"]), 3)
        self.assertIn(raw["active_plan_id"], raw["plans"])

    def test_autoapprove_bypasses_the_gate(self):
        config = Config(state_dir=self.state_dir, autoapprove=True, blocking_approval=False)
        h = PlanningHandlers(Store(self.state_dir), config)
        h.dispatch(
            "plan_and_think",
            {"goal": "g", "thought": "t", "step_number": 1, "total_steps": 1,
             "need_more_thinking": False, "task_list": ["a"]},
        )
        res = h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertTrue(res["ok"])


# ===========================================================================
# Stale approval (observed in the field: a morning approval unlocked afternoon work)
# ===========================================================================


class TestStaleApproval(HandlerTestCase):
    def approved_plan_aged(self, hours: float, goal="오전 작업: 로그 정리"):
        """Drive a plan to APPROVED, then backdate it as if it were left overnight."""
        self.think(goal=goal, need_more_thinking=False, task_list=["오전 A", "오전 B"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        pid = raw["active_plan_id"]
        old = (
            datetime.datetime.now().astimezone() - datetime.timedelta(hours=hours)
        ).replace(microsecond=0).isoformat()
        raw["plans"][pid]["updated_at"] = old
        raw["plans"][pid]["approval"]["decided_at"] = old
        (self.state_dir / "plan_state.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )
        return pid

    def test_stale_approval_does_not_unlock_execution(self):
        self.approved_plan_aged(hours=5)
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_EXPIRED")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_expired", audit)

    def test_stale_approval_does_not_short_circuit_ask_user(self):
        """The field symptom: ASK_USER returned 'already approved' and never paused."""
        self.approved_plan_aged(hours=5)
        res = self.h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"}
        )
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertNotIn("already approved", (res.get("message") or "").lower())

    def test_revising_the_goal_cannot_un_expire_an_approval(self):
        """Revising touches the plan; the touch must not reset the staleness clock."""
        self.approved_plan_aged(hours=5)
        res = self.h.dispatch("plan_and_think", {
            "goal": "오전 작업: 로그 정리", "revised_goal": "오전 작업: 로그 아카이빙",
            "thought": "사용자가 정정했다", "step_number": 2, "total_steps": 2,
            "need_more_thinking": True,
        })
        self.assertEqual(res["goal"], "오전 작업: 로그 아카이빙")   # correction taken
        blocked = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertFalse(blocked["ok"])                          # approval still gone
        self.assertIn(blocked["error_code"], ("APPROVAL_EXPIRED", "PLAN_NOT_APPROVED"))

    def test_fresh_approval_still_works(self):
        """The TTL must not break a plan being executed right now."""
        self.approve_flow(["a", "b"])
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "IN_EXECUTION")

    def test_new_goal_does_not_inherit_an_approved_plan(self):
        """A different goal must never be redirected onto an approved plan."""
        self.approve_flow(["오전 A", "오전 B"])
        res = self.think(
            goal="오후 작업: 배포 스크립트 실행", step_number=1,
            need_more_thinking=False, task_list=["배포 스크립트 실행"],
        )
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual([t["title"] for t in res["tasks"]], ["배포 스크립트 실행"])
        blocked = self.h.dispatch(
            "update_task_progress",
            {"task_id": 1, "status": "IN_PROGRESS", "plan_id": res["plan_id"]},
        )
        self.assertEqual(blocked["error_code"], "PLAN_NOT_APPROVED")

    def test_same_goal_mid_execution_still_redirects(self):
        """The original leniency must survive: same goal, plan in flight -> redirect."""
        self.approve_flow(["a", "b"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.think(step_number=1, need_more_thinking=False, task_list=["완전히 새 목록"])
        self.assertEqual(res["plan_status"], "IN_EXECUTION")
        self.assertEqual(res["tasks"][0]["title"], "a")

    def test_unparseable_timestamp_is_treated_as_expired(self):
        self.approve_flow(["a"])
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        raw["plans"][raw["active_plan_id"]]["updated_at"] = "not-a-date"
        (self.state_dir / "plan_state.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(res["error_code"], "APPROVAL_EXPIRED")


# ===========================================================================
# Blocking approval (the real HITL pause)
# ===========================================================================


class FakeApprovalUI:
    """Stands in for the shared approval state + page. Decides immediately, or never."""

    url = "http://127.0.0.1:0/"
    owns_page = True

    def __init__(self, decision=None, comment="", available=True,
                 task_comments=None, scope="PLAN", choices=None, criteria=None):
        self.decision = decision
        self.comment = comment
        self.choices = choices or {}
        self.criteria = criteria or {}
        self.available = available
        self.task_comments = task_comments or {}
        self.scope = scope
        self.opened: list[dict] = []
        self.cleared = 0
        self.agent_touches = 0
        self.live: dict | None = None
        # The run board (3.1.0): {plan_id: entry}, an entry's "control" being what the
        # human asked of that run on the page.
        self.runs: dict[str, dict] = {}

    def open_request(self, plan_id, goal, display, tasks, fingerprint="", phase="PLAN",
                     summary="", draft=False, origin=""):
        if not self.available:
            return None
        # Mirrors the real store's reuse rule: re-asking about an unchanged plan is the
        # same question, so it keeps the entry (and the id) already on screen.
        live = self.live
        if (
            live is not None
            and live["plan_id"] == plan_id
            and live["fingerprint"] == fingerprint
            and not live.get("decision")
        ):
            return live["id"]
        record = {
            "id": f"req{len(self.opened)}", "plan_id": plan_id, "fingerprint": fingerprint,
            "decision": self.decision, "comment": self.comment, "phase": phase,
            "tasks": tasks, "display": display, "goal": goal, "summary": summary,
            "draft": draft, "origin": origin, "created_at": time.time(),
        }
        self.opened.append(record)
        self.live = record
        return record["id"]

    def has_pending(self, plan_id, fingerprint):
        return self.pending_request(plan_id, fingerprint) is not None

    def pending_request(self, plan_id, fingerprint):
        r = self.live
        if (
            r is not None
            and r["plan_id"] == plan_id
            and r["fingerprint"] == fingerprint
            and not r.get("decision")
        ):
            return r
        return None

    def set_agent_note(self, request_id, note):
        if self.live is not None and self.live["id"] == request_id:
            self.live["agent_note"] = note

    def request_age(self, request_id):
        r = self.live
        if r is None or r["id"] != request_id:
            return 0.0
        return max(0.0, time.time() - r.get("created_at", time.time()))

    def touch_agent(self, request_id):
        self.agent_touches += 1

    def resolve(self, decision, comment="", choices=None, criteria=None):
        """Simulates the human clicking after the fact."""
        self.live["decision"] = decision
        self.live["comment"] = comment
        if choices is not None:
            self.choices = choices
        if criteria is not None:
            self.criteria = criteria

    def _verdict(self, record):
        # Both real phases may carry a per-task scope; an entry with no phase at all
        # cannot. The real store enforces exactly that in record_decision, and the
        # double must be neither more nor less permissive.
        scope = (
            self.scope
            if record.get("phase") in ("PLAN", "COMPLETION")
            else "PLAN"
        )
        return Verdict(
            decision=record["decision"],
            comment=record.get("comment", "") or "",
            task_comments=dict(self.task_comments) if scope == "TASKS" else {},
            scope=scope,
            # Like the real store: choices only count on an approval of a plan or halt.
            choices=dict(self.choices)
            if record["decision"] == "APPROVED" and record.get("phase") in ("PLAN", "HALT")
            else {},
            # Like the real store: what the human wrote counts on a plan request, with
            # an approval or a request for changes.
            criteria=dict(self.criteria)
            if record["decision"] in ("APPROVED", "REVISE") and record.get("phase") == "PLAN"
            else {},
        )

    def claim(self, request_id):
        r = self.live
        if r is None or r["id"] != request_id or not r.get("decision"):
            return None
        self.live = None
        self.cleared += 1
        return self._verdict(r)

    def take_decision(self, plan_id, fingerprint):
        r = self.live
        if r is None or not r.get("decision"):
            return None
        if r["plan_id"] != plan_id or r["fingerprint"] != fingerprint:
            self.live = None
            return None
        self.live = None
        return self._verdict(r)

    def drop_for_plan(self, plan_id):
        if self.live is not None and self.live["plan_id"] == plan_id:
            self.live = None

    # ---- the run board (3.1.0), with the real board's rules -----------------
    def sync_runs(self, wanted):
        """A control survives a refresh of its plan's entry and leaves with the plan."""
        merged = {}
        for plan_id, entry in wanted.items():
            new = dict(entry)
            control = (self.runs.get(plan_id) or {}).get("control")
            if control:
                new["control"] = control
            merged[plan_id] = new
        self.runs = merged
        return True

    def run_control_pending(self, plan_id):
        return (self.runs.get(plan_id) or {}).get("control")

    def take_run_control(self, plan_id):
        return (self.runs.get(plan_id) or {}).pop("control", None)

    def on_run_card(self, plan_id, action, comment=""):
        """Simulates the human pressing a button on a running plan's card."""
        if plan_id not in self.runs or (action == "NOTE" and not comment.strip()):
            return False
        self.runs[plan_id]["control"] = {
            "id": f"ctl{len(self.opened)}", "action": action, "comment": comment.strip(),
        }
        return True


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def progress(self, token, progress, message=None):
        self.sent.append((token, progress, message))


class TestBlockingApproval(HandlerTestCase):
    def blocking(self, ui, timeout=1):
        cfg = Config(
            state_dir=self.state_dir, blocking_approval=True, approval_timeout=timeout
        )
        return PlanningHandlers(Store(self.state_dir), cfg, approval_ui=ui)

    def draft(self, h):
        h.dispatch("plan_and_think", {
            "goal": "블로킹 승인 검증", "thought": "t", "step_number": 1, "total_steps": 1,
            "need_more_thinking": False, "task_list": ["작업 1", "작업 2"]})

    def ask(self, h, **kw):
        return h.dispatch(
            "request_user_approval",
            {"decision": "ASK_USER", "plan_summary": "요약"},
            **kw,
        )

    def test_human_approves_unlocks_in_one_call(self):
        """ASK_USER blocks, the human clicks approve, and the SAME call returns APPROVED."""
        ui = FakeApprovalUI("APPROVED")
        h = self.blocking(ui)
        self.draft(h)
        res = self.ask(h)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        self.assertEqual(res["next_task"]["task_id"], 1)
        self.assertEqual(len(ui.opened), 1)
        self.assertEqual(ui.cleared, 1)

    def test_human_rejects(self):
        h = self.blocking(FakeApprovalUI("REJECTED", "하지마"))
        self.draft(h)
        res = self.ask(h)
        self.assertEqual(res["plan_status"], "CANCELLED")
        blocked = h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(blocked["error_code"], "PLAN_CANCELLED")

    def test_human_requests_revision(self):
        h = self.blocking(FakeApprovalUI("REVISE", "2번 빼줘"))
        self.draft(h)
        res = self.ask(h)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["user_comment"], "2번 빼줘")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

    def test_timeout_leaves_plan_locked(self):
        h = self.blocking(FakeApprovalUI(decision=None), timeout=1)
        self.draft(h)
        res = self.ask(h)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("display_to_user", res)
        blocked = h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(blocked["error_code"], "PLAN_NOT_APPROVED")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_wait_timeout", audit)

    def test_ui_unavailable_degrades_loudly(self):
        """If the UI cannot start we must degrade - but never silently."""
        h = self.blocking(FakeApprovalUI(available=False))
        self.draft(h)
        res = self.ask(h)
        self.assertTrue(res["ok"])
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertTrue(
            any("NOT hard-paused" in n for n in res.get("input_notes", [])),
            f"degradation must be visible, got {res.get('input_notes')}",
        )
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_ui_unavailable", audit)

    def test_two_instances_share_one_page_and_one_state(self):
        """The whole point: a request from EITHER process shows on the same page."""
        store_dir = self.state_dir / "shared"
        owner = ApprovalServer(ApprovalStore(store_dir), port=8795, open_browser=False)
        peer = ApprovalServer(ApprovalStore(store_dir), port=8795, open_browser=False)
        try:
            self.assertTrue(owner.start())
            self.assertTrue(owner.owns_page)
            # The second instance must NOT grab a different port; it detects the peer.
            self.assertTrue(peer.start())
            self.assertFalse(peer.owns_page)
            self.assertEqual(peer.url, owner.url, "both must point at one URL")

            # A request published by the peer is served by the owner's page.
            req = peer.open_request("plan_p", "목표", "PLAN...", [{"task_id": 1}], "fp")
            with urllib.request.urlopen(owner.url + "api/pending", timeout=5) as r:
                shown = json.loads(r.read().decode("utf-8"))["requests"][0]
            self.assertEqual(shown["id"], req)
            self.assertEqual(shown["plan_id"], "plan_p")

            # And a decision taken on that page is visible to the peer that asked.
            body = json.dumps({"id": req, "decision": "APPROVED", "comment": "ok"}).encode()
            post = urllib.request.Request(
                owner.url + "api/decide", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(post, timeout=5) as r:
                self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])
            self.assertEqual(peer.claim(req), Verdict("APPROVED", "ok"))
        finally:
            owner.shutdown()
            peer.shutdown()

    def test_a_real_poll_to_the_owner_is_visible_to_the_peer(self):
        """D24 end to end: the HTTP handler must publish liveness, not just remember it.

        This is the shape that broke in production - the human's tab talks to the owner,
        the approval request is held by the peer, and it is the peer that decides whether
        to launch a browser.
        """
        store_dir = self.state_dir / "shared-liveness"
        owner = ApprovalServer(ApprovalStore(store_dir), port=8796, open_browser=False)
        peer = ApprovalServer(ApprovalStore(store_dir), port=8796, open_browser=False)
        try:
            self.assertTrue(owner.start())
            self.assertTrue(peer.start())
            self.assertFalse(peer.page_is_being_watched(), "no tab has polled yet")

            with urllib.request.urlopen(owner.url + "api/pending", timeout=5) as r:
                r.read()

            self.assertEqual(peer._last_poll_at, 0.0, "the peer was never polled itself")
            self.assertTrue(
                peer.page_is_being_watched(),
                "a peer that cannot see the owner's tab opens a window every slice",
            )
        finally:
            owner.shutdown()
            peer.shutdown()

    def test_foreign_occupant_is_detected(self):
        """A non-planning-mcp listener on the port must be reported, not adopted."""
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 8797))
        blocker.listen(1)
        srv = ApprovalServer(ApprovalStore(self.state_dir / "x"), port=8797, open_browser=False)
        try:
            self.assertFalse(srv.start())
        finally:
            srv.shutdown()
            blocker.close()

    def test_store_survives_a_new_process_view(self):
        """A second ApprovalStore object sees what the first published."""
        d = self.state_dir / "sharedstore"
        a, b = ApprovalStore(d), ApprovalStore(d)
        rid = a.publish("plan_1", "g", "display", [], "fp1")
        self.assertEqual(b.peek()[0]["id"], rid)
        self.assertTrue(b.record_decision(rid, "APPROVED", "예"))
        self.assertEqual(a.claim_for_plan("plan_1", "fp1"), Verdict("APPROVED", "예"))
        self.assertEqual(a.peek(), [])

    def test_heartbeat_only_with_progress_token(self):
        notifier = RecordingNotifier()
        h = self.blocking(FakeApprovalUI(decision=None), timeout=1)
        self.draft(h)
        self.ask(h, progress_token=None, notifier=notifier)
        self.assertEqual(notifier.sent, [])  # no token -> must not send progress

    def test_one_call_stays_under_the_client_timeout_with_or_without_a_token(self):
        """A progressToken must not buy a longer single call.

        This is the 1.14 correction. `resetTimeoutOnProgress` is the client's option to
        set, it defaulted to false in older TypeScript SDKs, and a server cannot observe
        which way it went - so betting a 900s call on it is what killed conversations at
        60s. Chunking makes the bet unnecessary.
        """
        h = self.blocking(FakeApprovalUI(decision=None), timeout=3600)
        for can_heartbeat in (False, True):
            self.assertLessEqual(
                h.effective_timeout(can_heartbeat=can_heartbeat),
                SDK_REQUEST_TIMEOUT_SEC - 1,
                "one call must finish inside the client's request timeout",
            )

    def test_chunk_shrinks_as_the_total_budget_runs_down(self):
        h = self.blocking(FakeApprovalUI(decision=None), timeout=3600)
        self.assertEqual(h.effective_timeout(can_heartbeat=False), h.config.call_budget)
        # 10s left of the human's budget: the slice may not overrun it.
        self.assertEqual(h.effective_timeout(can_heartbeat=False, already_waited=3590), 10)
        self.assertEqual(h.effective_timeout(can_heartbeat=False, already_waited=3600), 0)

    def test_short_configured_timeout_is_respected_either_way(self):
        h = self.blocking(FakeApprovalUI(decision=None), timeout=5)
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 5)
        self.assertEqual(h.effective_timeout(can_heartbeat=True), 5)

    def test_observed_client_cancellation_shrinks_the_next_chunk(self):
        h = self.blocking(FakeApprovalUI(decision=None), timeout=3600)
        h.store.record_call_cap(30)  # this client actually gave up at 30s
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 25)

    def test_trust_heartbeat_mode_keeps_the_old_single_long_call(self):
        h = self.blocking(FakeApprovalUI(decision=None), timeout=3600)
        h.config.approval_mode = "trust_heartbeat"
        self.assertEqual(h.effective_timeout(can_heartbeat=True), 3600)
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 55)

    def test_request_stays_open_after_timeout(self):
        """The page must keep the buttons after the tool call gives up."""
        ui = FakeApprovalUI(decision=None)
        h = self.blocking(ui, timeout=1)
        self.draft(h)
        self.ask(h)
        self.assertEqual(ui.cleared, 0, "a timed-out request must NOT be cleared")
        self.assertIsNotNone(ui.live, "the human must still be able to decide")

    def test_late_approval_is_applied_on_the_next_call(self):
        """Click approve after the timeout -> the next tool call honours it."""
        ui = FakeApprovalUI(decision=None)
        h = self.blocking(ui, timeout=1)
        self.draft(h)
        res = self.ask(h)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")

        ui.resolve("APPROVED", "승인")               # 사람이 뒤늦게 클릭
        res = h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["approval"]["decision"], "APPROVED")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("late_decision_applied", audit)

        run = h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertTrue(run["ok"])

    def test_late_rejection_is_applied(self):
        ui = FakeApprovalUI(decision=None)
        h = self.blocking(ui, timeout=1)
        self.draft(h)
        self.ask(h)
        ui.resolve("REJECTED", "하지마")
        res = h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "CANCELLED")

    def test_late_decision_is_discarded_if_the_plan_changed(self):
        """A decision must not apply to a plan the human never saw."""
        ui = FakeApprovalUI(decision=None)
        h = self.blocking(ui, timeout=1)
        self.draft(h)
        self.ask(h)
        # Changed underneath the page by another session. (Since 1.16 the model's own
        # plan_and_think can no longer do this while the request is open - it waits on
        # the human instead; see TestLoopRegressions.)
        state = h.store.load()
        state.active_plan.tasks[0].title = "전혀 다른 작업"
        h.store.save(state)
        ui.resolve("APPROVED", "")     # 사람이 본 적 없는 계획에 대한 승인
        res = h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")

    def test_approval_url_reaches_the_user_in_chat(self):
        """A blocked popup must not hide the page: the URL rides in display_to_user."""
        ui = FakeApprovalUI(decision=None)
        ui.url = "http://127.0.0.1:8899/"
        h = self.blocking(ui, timeout=1)
        self.draft(h)
        res = self.ask(h)
        self.assertEqual(res["approval_url"], "http://127.0.0.1:8899/")
        # The chat line drops the trailing slash for readability; the field keeps it.
        self.assertIn("현재 페이지: http://127.0.0.1:8899", res["display_to_user"])

    def test_out_of_band_decision_is_audited(self):
        h = self.blocking(FakeApprovalUI("APPROVED", "승인"))
        self.draft(h)
        self.ask(h)
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_decided_out_of_band", audit)

    def test_approval_server_binds_and_serves(self):
        """The real server (not the fake) must start and answer /api/pending."""
        srv = ApprovalServer(
            ApprovalStore(self.state_dir / "real"), port=8791, open_browser=False
        )
        try:
            self.assertTrue(srv.start())
            req = srv.open_request("plan_x", "목표", "PLAN...", [{"task_id": 1}], "fp")
            self.assertIsNotNone(req)
            with urllib.request.urlopen(srv.url + "api/pending", timeout=5) as r:
                payload = json.loads(r.read().decode("utf-8"))["requests"][0]
            self.assertEqual(payload["plan_id"], "plan_x")
            body = json.dumps(
                {"id": payload["id"], "decision": "APPROVED", "comment": "ok"}
            ).encode("utf-8")
            post = urllib.request.Request(
                srv.url + "api/decide", data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(post, timeout=5) as r:
                self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])
            self.assertEqual(srv.claim(req), Verdict("APPROVED", "ok"))
        finally:
            srv.shutdown()


class TestChunkedApproval(HandlerTestCase):
    """The 1.14 answer to the client-side 60s cap on a single tools/call.

    A call that outlives the client's patience does not merely fail - the client drops
    the result and the conversation breaks mid-approval. So the wait gives itself up
    while the client is still listening and tells the model to come straight back.
    """

    def handlers(self, ui, timeout=900, budget=1, mode="chunked"):
        cfg = Config(
            state_dir=self.state_dir,
            blocking_approval=True,
            approval_timeout=timeout,
            call_budget=budget,
            approval_mode=mode,
        )
        return PlanningHandlers(Store(self.state_dir), cfg, approval_ui=ui)

    def draft(self, h):
        h.dispatch("plan_and_think", {
            "goal": "분할 대기 검증", "thought": "t", "step_number": 1, "total_steps": 1,
            "need_more_thinking": False, "task_list": ["작업 1", "작업 2"]})

    def ask(self, h, **kw):
        return h.dispatch(
            "request_user_approval",
            {"decision": "ASK_USER", "plan_summary": "요약"},
            **kw,
        )

    # ---- the slice ends, the question does not -----------------------------
    def test_chunk_expiry_says_pending_not_approved(self):
        h = self.handlers(FakeApprovalUI(decision=None))
        self.draft(h)
        res = self.ask(h)
        self.assertFalse(res["ok"], "a pending wait must never read as success")
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        self.assertIn("NOT decided", res["next_action_hint"])

    def test_chunk_expiry_withholds_anything_that_reads_as_a_verdict(self):
        """The payload is the one place a weak model could find a false go-ahead."""
        h = self.handlers(FakeApprovalUI(decision=None))
        self.draft(h)
        res = self.ask(h)
        self.assertNotIn("tasks", res)
        self.assertNotIn("display_to_user", res)
        self.assertNotIn("next_task", res)

    def test_chunk_expiry_reports_the_budget(self):
        h = self.handlers(FakeApprovalUI(decision=None), timeout=900, budget=1)
        self.draft(h)
        res = self.ask(h)
        self.assertGreaterEqual(res["waited_seconds"], 0)
        self.assertGreater(res["remaining_seconds"], 0)

    def test_re_asking_reuses_the_same_request(self):
        """Otherwise the page rebuilds every slice and eats the human's half-typed comment."""
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui)
        self.draft(h)
        self.ask(h)
        self.ask(h)
        self.ask(h)
        self.assertEqual(len(ui.opened), 1, "each slice minted a new request")

    def test_the_agent_is_marked_alive_while_it_waits(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui)
        self.draft(h)
        self.ask(h)
        self.assertGreater(ui.agent_touches, 0)

    def test_a_click_during_a_later_slice_is_honoured(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui)
        self.draft(h)
        self.assertEqual(self.ask(h)["error_code"], "APPROVAL_PENDING")
        ui.resolve("APPROVED")
        res = self.ask(h)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "APPROVED")

    def test_exhausting_the_whole_budget_stops_instead_of_looping(self):
        """A model told to retry forever would spin until the context ran out."""
        h = self.handlers(FakeApprovalUI(decision=None), timeout=1, budget=1)
        self.draft(h)
        res = self.ask(h)
        self.assertTrue(res["ok"])
        self.assertNotEqual(res.get("error_code"), "APPROVAL_PENDING")
        self.assertIn("display_to_user", res)
        self.assertIn("wait expired", " ".join(res["input_notes"]))

    def test_return_mode_does_not_hold_the_call(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui, timeout=900, mode="return")
        self.draft(h)
        started = time.monotonic()
        res = self.ask(h)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("display_to_user", res)
        self.assertIsNotNone(ui.live, "the request must still reach the page")

    # ---- the client giving up ----------------------------------------------
    def test_cancellation_unblocks_the_wait_and_is_remembered(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui, timeout=900, budget=30)
        self.draft(h)
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        started = time.monotonic()
        self.ask(h, cancel_event=cancel)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5, "a cancelled call must not keep waiting")
        self.assertIsNotNone(h.store.observed_call_cap())
        self.assertIsNotNone(ui.live, "the human must still be able to decide")
        events = [json.loads(l) for l in
                  (self.state_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e["event"] == "client_cancelled_call" for e in events))

    def test_protocol_routes_a_cancellation_to_the_right_call(self):
        seen = {}

        class Spy:
            def dispatch(self, name, args, progress_token=None, notifier=None,
                         cancel_event=None):
                seen["event"] = cancel_event
                # The client gives up while this call is still running.
                p._cancel(7)
                seen["set_during_call"] = cancel_event.is_set()
                return {"ok": True, "plan_status": "NONE", "next_action": "ANSWER_USER",
                        "next_action_hint": "-"}

        p = McpProtocol(Spy(), TOOL_DEFINITIONS, "x", "1")
        p.handle_message({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                          "params": {"name": "get_current_plan", "arguments": {}}})
        self.assertTrue(seen["set_during_call"])
        # And the registry is emptied, so a late duplicate cancel is a no-op.
        p._cancel(7)

    def test_a_cancellation_for_an_unknown_id_is_ignored(self):
        p = McpProtocol(object(), TOOL_DEFINITIONS, "x", "1")
        self.assertEqual(
            p.handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled",
                              "params": {"requestId": 404}}),
            None,
        )

    # ---- the model may not answer its own question --------------------------
    def test_model_cannot_approve_while_the_request_is_open(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui)
        self.draft(h)
        self.ask(h)
        res = h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        plan_id = next(iter(h.store.load().plans.keys()))
        self.assertEqual(
            h.store.load().plans[plan_id].plan_status, "AWAITING_APPROVAL"
        )

    def test_model_cannot_reject_or_revise_either(self):
        """Refused as many times as it tries: the guard is state, not a one-shot latch."""
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui)
        self.draft(h)
        self.ask(h)
        for decision in ("REJECTED", "REVISE", "APPROVED"):
            with self.subTest(decision=decision):
                res = h.dispatch(
                    "request_user_approval",
                    {"decision": decision, "user_comment": "내 마음대로"},
                )
                self.assertEqual(res["error_code"], "APPROVAL_PENDING")
                self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")

    def test_the_refusal_is_audited(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui)
        self.draft(h)
        self.ask(h)
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        events = [json.loads(l) for l in
                  (self.state_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e["event"] == "self_approval_refused" for e in events))

    def test_a_real_click_is_never_blocked_by_the_guard(self):
        """The guard must refuse only the model's own word, never the human's."""
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui)
        self.draft(h)
        self.ask(h)
        ui.resolve("APPROVED")
        # Any tool call at all drains the decision first, which withdraws the request.
        res = h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "APPROVED")

    def test_the_guard_lifts_once_the_plan_has_moved_on(self):
        """An entry for a superseded plan version must not freeze the new one."""
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui)
        self.draft(h)
        self.ask(h)
        plan_id = next(iter(h.store.load().plans.keys()))
        self.assertTrue(ui.has_pending(plan_id, ui.live["fingerprint"]))
        self.assertFalse(ui.has_pending(plan_id, "a-different-version"))


class TestBrowserTabSpam(HandlerTestCase):
    """One request must not equal one new browser window - and closing the window must
    not mean no window ever again.

    `_surface` called `_open_browser_once` on every request, and that function never
    checked the `_opened_once` flag it is named for - the guard lived only in `start()`.
    Every approval opened another tab, so the human ended up with several windows each
    holding a different half-written comment, and whichever one they submitted from
    silently discarded the rest.

    Fixing that by honouring `_opened_once` overshot (D23): the latch was permanent, so
    once the human closed the approval tab no later request could ever open one. They
    were left waiting on a window that would never come back. Suppression now comes from
    liveness plus a short launch grace, both of which expire.

    Which put the whole weight on liveness being *right*, and it was process-local (D24).
    See TestPageLivenessIsShared.
    """

    def server(self):
        srv = ApprovalServer(ApprovalStore(self.state_dir), port=0, open_browser=True)
        srv._opened = 0

        def fake_open(url):
            srv._opened += 1
            return True

        self.addCleanup(mock.patch.stopall)
        mock.patch("planning.approval.webbrowser.open", fake_open).start()
        return srv

    def test_only_the_first_request_opens_a_window(self):
        srv = self.server()
        for _ in range(4):
            srv._surface()
        self.assertEqual(srv._opened, 1)

    def test_a_live_tab_stops_even_the_first_open(self):
        srv = self.server()
        srv._last_poll_at = time.time()  # a tab polled just now
        srv._surface()
        self.assertEqual(srv._opened, 0)
        self.assertTrue(srv.page_is_being_watched())

    def test_a_long_gone_tab_does_not_count_as_watching(self):
        srv = self.server()
        srv._last_poll_at = time.time() - 3600
        self.assertFalse(srv.page_is_being_watched())
        srv._surface()
        self.assertEqual(srv._opened, 1)

    def test_closing_the_tab_lets_the_next_request_open_one(self):
        """D23. The human closes the approval window, then a later request arrives."""
        srv = self.server()
        srv._surface()  # first request opens a tab
        srv.note_poll()  # it loads and starts polling; they are looking at it
        srv._surface()
        self.assertEqual(srv._opened, 1, "no second tab while one is being watched")

        # They close the tab: the last poll recedes past the idle window.
        gone = time.time() - (approval.PAGE_IDLE_SEC + 1)
        srv._last_poll_at = gone
        srv.store.mark_page_seen(gone)
        srv._last_open_at = gone
        srv._surface()
        self.assertEqual(srv._opened, 2, "a closed tab must be reopened, not shrugged off")

    def test_the_launch_grace_survives_a_chunked_wait_repeat(self):
        """The tab has been asked for but has not polled yet - do not launch another."""
        srv = self.server()
        srv._surface()
        for _ in range(5):
            srv._surface()  # repeat calls land before the browser has finished starting
        self.assertEqual(srv._opened, 1)

    def test_suppression_is_never_permanent(self):
        srv = self.server()
        for _ in range(3):
            srv._surface()
            # Each window really did appear and poll, then the human closed it again.
            gone = time.time() - (approval.PAGE_IDLE_SEC + 1)
            srv._last_open_at = gone
            srv._last_poll_at = gone
            srv.store.mark_page_seen(gone)
        self.assertEqual(srv._opened, 3)


class TestPageLivenessIsShared(HandlerTestCase):
    """D24. Whether a tab is open is a fact about the machine, not about one process.

    Only one planning-mcp process owns the approval page; every other instance on the
    same state directory is a peer, and the polls go to the owner. The process that
    decides to open a browser is whichever one is holding an approval request - routinely
    a peer, whose own `_last_poll_at` is zero forever. Reading only that counter, a peer
    concludes "nobody is watching" every time, and the 45s chunked wait becomes a new
    window every 45 seconds in front of a human who already has the page open.
    """

    def pair(self):
        """An owner and a peer, sharing one state directory - the real deployment."""
        owner = ApprovalServer(ApprovalStore(self.state_dir), port=0, open_browser=True)
        peer = ApprovalServer(ApprovalStore(self.state_dir), port=0, open_browser=True)
        peer._opened = 0

        def fake_open(url):
            peer._opened += 1
            return True

        self.addCleanup(mock.patch.stopall)
        mock.patch("planning.approval.webbrowser.open", fake_open).start()
        return owner, peer

    def test_a_peer_sees_the_tab_the_owner_is_serving(self):
        owner, peer = self.pair()
        owner.note_poll()  # the human's tab polls the owner, not the peer
        self.assertEqual(peer._last_poll_at, 0.0, "the peer is not the one being polled")
        self.assertTrue(peer.page_is_being_watched())
        peer._surface()
        self.assertEqual(peer._opened, 0)

    def test_the_chunked_wait_does_not_open_a_window_per_slice(self):
        owner, peer = self.pair()
        for _ in range(6):  # six 45s slices of one wait
            owner.note_poll()  # the tab is open the whole time
            peer._last_open_at -= approval.OPEN_GRACE_SEC + 1  # slices are far apart
            peer._surface()
        self.assertEqual(peer._opened, 0, "one window per slice is the D24 bug")

    def test_a_peer_still_opens_one_when_no_tab_answers(self):
        owner, peer = self.pair()
        owner.note_poll()
        peer.store.mark_page_seen(time.time() - (approval.PAGE_IDLE_SEC + 1))
        peer._last_poll_at = 0.0
        self.assertFalse(peer.page_is_being_watched())
        peer._surface()
        self.assertEqual(peer._opened, 1)

    def test_a_poll_is_written_where_another_process_can_read_it(self):
        owner, peer = self.pair()
        owner.note_poll()
        self.assertGreater(peer.store.page_last_seen(), 0.0)

    def test_writes_are_throttled_but_never_stale_enough_to_matter(self):
        owner, _ = self.pair()
        owner.note_poll()
        first = owner.store.page_last_seen()
        owner.note_poll()  # immediately again - throttled, not written
        self.assertEqual(owner.store.page_last_seen(), first)
        # The throttle window is far shorter than the idle window it feeds.
        self.assertLess(approval.PAGE_SEEN_WRITE_SEC, approval.PAGE_IDLE_SEC)

    def test_a_missing_or_corrupt_record_reads_as_no_tab(self):
        _, peer = self.pair()
        self.assertEqual(peer.store.page_last_seen(), 0.0)
        peer.store.page_seen_path.write_text("not a number", encoding="ascii")
        self.assertEqual(peer.store.page_last_seen(), 0.0)
        self.assertFalse(peer.page_is_being_watched())


class TestBrowserThatCannotOpen(HandlerTestCase):
    """`webbrowser.open` returning True is not proof a window appeared.

    Policy can swallow it and the handler can be missing. With a fixed grace, every slice
    of the wait relaunches a browser that never shows up.
    """

    def server(self):
        srv = ApprovalServer(ApprovalStore(self.state_dir), port=0, open_browser=True)
        srv._opened = 0

        def fake_open(url):
            srv._opened += 1
            return True  # claims success, no tab ever polls

        self.addCleanup(mock.patch.stopall)
        mock.patch("planning.approval.webbrowser.open", fake_open).start()
        return srv

    def test_launches_back_off_instead_of_repeating_every_slice(self):
        srv = self.server()
        for _ in range(8):
            srv._last_open_at -= approval.OPEN_GRACE_SEC + 1  # one slice later
            srv._surface()
        self.assertLess(srv._opened, 8, "a browser that never opens must not be spammed")
        self.assertGreater(srv._opened, 0)

    def test_the_backoff_is_capped(self):
        srv = self.server()
        srv._opens_without_contact = 99
        srv._last_open_at = time.time() - (approval.OPEN_GRACE_MAX_SEC + 1)
        srv._surface()
        self.assertEqual(srv._opened, 1)

    def test_one_real_poll_makes_it_responsive_again(self):
        srv = self.server()
        for _ in range(8):
            srv._last_open_at -= approval.OPEN_GRACE_SEC + 1
            srv._surface()
        backed_off = srv._opened

        # The human opens the page by hand: contact re-established, then closes it.
        srv.store.mark_page_seen(time.time())
        srv._last_poll_at = 0.0
        srv._surface()  # a tab is watching - nothing to do
        self.assertEqual(srv._opened, backed_off)

        # ...then closes it. The poll answered the launch, so it is the newer of the two.
        seen = time.time() - (approval.PAGE_IDLE_SEC + 1)
        srv.store.mark_page_seen(seen)
        srv._last_poll_at = seen
        srv._last_open_at = seen - 1
        srv._surface()
        self.assertEqual(srv._opened, backed_off + 1, "grace must be back to its floor")


class TestPublishReuse(HandlerTestCase):
    """The store-level rule the chunked wait depends on."""

    def approvals(self):
        return ApprovalStore(self.state_dir)

    def test_same_plan_and_fingerprint_keeps_the_entry(self):
        s = self.approvals()
        first = s.publish("p1", "g", "d", [{"task_id": 1}], "fp1", "PLAN", "s")
        again = s.publish("p1", "g", "d", [{"task_id": 1}], "fp1", "PLAN", "s")
        self.assertEqual(first, again)
        self.assertEqual(len(s.peek()), 1)

    def test_created_at_is_not_reset_so_the_budget_can_run_out(self):
        s = self.approvals()
        rid = s.publish("p1", "g", "d", [{"task_id": 1}], "fp1", "PLAN", "s")
        born = s.entry(rid)["created_at"]
        time.sleep(0.05)
        s.publish("p1", "g", "d", [{"task_id": 1}], "fp1", "PLAN", "s")
        self.assertEqual(s.entry(rid)["created_at"], born)

    def test_a_changed_plan_gets_a_new_entry(self):
        s = self.approvals()
        first = s.publish("p1", "g", "d", [{"task_id": 1}], "fp1", "PLAN", "s")
        second = s.publish("p1", "g", "d2", [{"task_id": 1}], "fp2", "PLAN", "s")
        self.assertNotEqual(first, second)
        self.assertEqual(len(s.peek()), 1, "the superseded version must not linger")

    def test_a_decided_entry_is_not_reused(self):
        s = self.approvals()
        first = s.publish("p1", "g", "d", [{"task_id": 1}], "fp1", "PLAN", "s")
        s.record_decision(first, "APPROVED", "")
        second = s.publish("p1", "g", "d", [{"task_id": 1}], "fp1", "PLAN", "s")
        self.assertNotEqual(first, second)

    def test_another_plan_is_untouched(self):
        s = self.approvals()
        a = s.publish("p1", "g", "d", [{"task_id": 1}], "fpA", "PLAN", "s")
        b = s.publish("p2", "g", "d", [{"task_id": 1}], "fpB", "PLAN", "s")
        self.assertEqual({a, b}, {e["id"] for e in s.peek()})

    def test_request_age_grows_and_touch_marks_the_agent(self):
        s = self.approvals()
        rid = s.publish("p1", "g", "d", [{"task_id": 1}], "fp1", "PLAN", "s")
        time.sleep(0.05)
        self.assertGreater(s.request_age(rid), 0)
        self.assertEqual(s.request_age("nope"), 0.0)
        s.touch_agent(rid)
        self.assertIn("agent_last_seen", s.entry(rid))


class TestObservedClientCap(HandlerTestCase):
    def test_the_tightest_observation_wins_and_survives_a_reload(self):
        s = Store(self.state_dir)
        self.assertIsNone(s.observed_call_cap())
        s.record_call_cap(50)
        s.record_call_cap(30)
        s.record_call_cap(55)  # looser; must not overwrite the tighter one
        self.assertEqual(Store(self.state_dir).observed_call_cap(), 30)

    def test_a_nonsense_value_is_ignored(self):
        s = Store(self.state_dir)
        s.record_call_cap(0)
        s.record_call_cap(-5)
        self.assertIsNone(s.observed_call_cap())

    def test_a_corrupt_file_reads_as_no_observation(self):
        s = Store(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        s.client_caps_path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(s.observed_call_cap())


# ===========================================================================
# Edge cases — deliberately probing the seams between recent refactors
# ===========================================================================


class TestConcurrencyEdges(HandlerTestCase):
    def test_transaction_is_exclusive_across_threads(self):
        """Only one thread may be inside a transaction at a time.

        The stdio transport now handles each request on its own thread, so this is a
        live path, not a theoretical one.
        """
        store = Store(self.state_dir / "txn")
        inside: list[int] = []
        violations: list[str] = []

        def worker() -> None:
            for _ in range(5):
                with store.transaction():
                    inside.append(1)
                    if len(inside) > 1:
                        violations.append(f"{len(inside)} threads inside at once")
                    time.sleep(0.01)
                    inside.pop()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(violations, [], "transaction is not mutually exclusive")

    def test_concurrent_requests_in_one_process_lose_nothing(self):
        """The stdio transport threads every request; none may clobber another's plan."""
        h = PlanningHandlers(
            Store(self.state_dir),
            Config(state_dir=self.state_dir, blocking_approval=False, max_active_plans=50),
        )
        results: list[dict] = []
        barrier = threading.Barrier(6)

        def worker(i: int) -> None:
            barrier.wait()  # maximise the overlap
            results.append(h.dispatch("plan_and_think", {
                "goal": f"목표 {i}", "thought": "t", "step_number": 1, "total_steps": 1,
                "need_more_thinking": False, "task_list": [f"작업 {i}"]}))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        ids = [r["plan_id"] for r in results]
        self.assertEqual(len(set(ids)), 6, f"plan_id collision: {ids}")
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["plans"]), 6, "a concurrent plan was lost")
        self.assertEqual(
            {p["goal"] for p in raw["plans"].values()},
            {f"목표 {i}" for i in range(6)},
        )

    def test_transaction_is_reentrant_within_one_thread(self):
        store = Store(self.state_dir / "reentrant")
        with store.transaction():
            with store.transaction():
                store.save(store.load())
        # A second, independent acquisition must still work afterwards.
        with store.transaction():
            pass

    def test_paused_outside_a_transaction_is_harmless(self):
        store = Store(self.state_dir / "paused")
        with store.paused():
            pass
        with store.transaction():
            pass

    def test_paused_lets_another_thread_in(self):
        store = Store(self.state_dir / "handoff")
        got_in = threading.Event()

        def other() -> None:
            with store.transaction():
                got_in.set()

        with store.transaction():
            t = threading.Thread(target=other, daemon=True)
            t.start()
            self.assertFalse(got_in.wait(0.3), "should be blocked while we hold it")
            with store.paused():
                self.assertTrue(got_in.wait(5), "paused() must release the transaction")
            t.join(timeout=5)


class TestMultiPlanEdges(HandlerTestCase):
    def two_plans(self):
        a = self.think(goal="A", need_more_thinking=False, task_list=["a1", "a2"])
        b = self.think(goal="B", need_more_thinking=False, task_list=["b1"])
        return a["plan_id"], b["plan_id"]

    def approve(self, pid):
        self.h.dispatch("request_user_approval",
                        {"decision": "ASK_USER", "plan_summary": "s", "plan_id": pid})
        return self.h.dispatch("request_user_approval", {"decision": "APPROVED", "plan_id": pid})

    def test_identical_goals_route_to_the_same_plan(self):
        self.legacy()
        first = self.think(goal="같은 목표", need_more_thinking=False, task_list=["x"])
        second = self.think(goal="같은 목표", need_more_thinking=False, task_list=["y"])
        self.assertEqual(first["plan_id"], second["plan_id"])
        # 1.16 (D25): the second call is a re-think of a finalized plan, so it is
        # redirected to approval rather than allowed to replace the list the human is
        # about to be shown.
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        plan = raw["plans"][first["plan_id"]]
        self.assertEqual([t["title"] for t in plan["tasks"]], ["x"])
        self.assertEqual(second["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_blocked_plan_does_not_block_its_sibling(self):
        a_id, b_id = self.two_plans()
        self.approve(a_id)
        self.approve(b_id)
        self.h.dispatch("update_task_progress",
                        {"task_id": 1, "status": "FAILED", "plan_id": a_id})
        res = self.h.dispatch("update_task_progress",
                              {"task_id": 1, "status": "IN_PROGRESS", "plan_id": b_id})
        self.assertTrue(res["ok"], res.get("error_code"))
        self.assertEqual(res["plan_id"], b_id)

    def test_expiry_touches_only_the_resolved_plan(self):
        a_id, b_id = self.two_plans()
        self.approve(a_id)
        self.approve(b_id)
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        old = (datetime.datetime.now().astimezone()
               - datetime.timedelta(hours=5)).replace(microsecond=0).isoformat()
        raw["plans"][a_id]["updated_at"] = old
        (self.state_dir / "plan_state.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        fresh = self.h.dispatch("update_task_progress",
                                {"task_id": 1, "status": "IN_PROGRESS", "plan_id": b_id})
        self.assertTrue(fresh["ok"], "a sibling must not be expired by proxy")
        stale = self.h.dispatch("update_task_progress",
                                {"task_id": 1, "status": "IN_PROGRESS", "plan_id": a_id})
        self.assertEqual(stale["error_code"], "APPROVAL_EXPIRED")

    def test_finishing_one_plan_removes_the_ambiguity(self):
        a_id, b_id = self.two_plans()
        self.approve(a_id)
        self.h.dispatch("request_user_approval",
                        {"decision": "REJECTED", "plan_id": b_id})
        # Only A is live now, so plan_id becomes optional again.
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertTrue(res["ok"], res.get("error_code"))
        self.assertEqual(res["plan_id"], a_id)

    def test_terminal_plan_is_still_readable_by_id(self):
        a_id, _ = self.two_plans()
        self.h.dispatch("request_user_approval", {"decision": "REJECTED", "plan_id": a_id})
        res = self.h.dispatch("get_current_plan", {"plan_id": a_id})
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "CANCELLED")

    def test_each_session_gets_its_own_plan_by_id(self):
        a_id, b_id = self.two_plans()
        for pid in (a_id, b_id, a_id):  # interleaved, as concurrent sessions arrive
            res = self.h.dispatch("get_current_plan", {"plan_id": pid})
            self.assertTrue(res["ok"], res.get("error_code"))
            self.assertEqual(res["plan_id"], pid, "a session was handed another's plan")
            self.assertIn("tasks", res)

    def test_current_never_guesses_between_concurrent_plans(self):
        a_id, b_id = self.two_plans()
        res = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        # It must hand back the directory, not silently pick one of them.
        self.assertIsNone(res["plan_id"])
        self.assertEqual({p["plan_id"] for p in res["active_plans"]}, {a_id, b_id})

    def test_ambiguous_read_does_not_tell_the_model_to_start_a_new_plan(self):
        self.two_plans()
        res = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["next_action"], "CALL_GET_CURRENT_PLAN")
        self.assertIn("plan_id", res["next_action_hint"])

    def test_unknown_plan_id_offers_the_directory_instead_of_a_fresh_start(self):
        a_id, b_id = self.two_plans()
        res = self.h.dispatch("get_current_plan", {"plan_id": "plan_does_not_exist"})
        # A stale or mistyped id must not read as "no plan exists" - that is what made
        # the model abandon its plan and fork a new one.
        self.assertEqual(res["next_action"], "CALL_GET_CURRENT_PLAN")
        self.assertEqual({p["plan_id"] for p in res["active_plans"]}, {a_id, b_id})

    def test_unknown_plan_id_is_reported(self):
        self.two_plans()
        res = self.h.dispatch("get_current_plan", {"plan_id": "plan_does_not_exist"})
        self.assertTrue(res["ok"])
        self.assertTrue(
            any("plan_does_not_exist" in n for n in res.get("input_notes", [])),
            res.get("input_notes"),
        )

    def test_max_active_plans_is_enforced(self):
        cfg = Config(state_dir=self.state_dir, blocking_approval=False, max_active_plans=2)
        h = PlanningHandlers(Store(self.state_dir), cfg)
        for name in ("G1", "G2"):
            h.dispatch("plan_and_think", {"goal": name, "thought": "t", "step_number": 1,
                                          "total_steps": 1, "need_more_thinking": False,
                                          "task_list": ["x"]})
        res = h.dispatch("plan_and_think", {"goal": "G3", "thought": "t", "step_number": 1,
                                            "total_steps": 1, "need_more_thinking": False,
                                            "task_list": ["x"]})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_AMBIGUOUS")
        self.assertEqual(len(res["active_plans"]), 2)

    def test_pruning_never_drops_an_active_plan(self):
        cfg = Config(state_dir=self.state_dir, blocking_approval=False, max_plans=3,
                     max_active_plans=10)
        h = PlanningHandlers(Store(self.state_dir, max_plans=3), cfg)
        live = []
        for i in range(4):
            r = h.dispatch("plan_and_think", {"goal": f"살아있는 {i}", "thought": "t",
                                              "step_number": 1, "total_steps": 1,
                                              "need_more_thinking": False, "task_list": ["x"]})
            live.append(r["plan_id"])
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        for pid in live:
            self.assertIn(pid, raw["plans"], "an active plan was pruned away")

    def test_plan_id_accepts_whitespace_and_case(self):
        a_id, _ = self.two_plans()
        res = self.h.dispatch("get_current_plan", {"plan_id": f"  {a_id}  "})
        self.assertEqual(res["plan_id"], a_id)
        res = self.h.dispatch("get_current_plan", {"plan_id": "CURRENT"})
        self.assertIn("active_plans", res)  # ambiguous, reported as a directory

    def test_plan_id_sent_as_a_number_does_not_crash(self):
        self.two_plans()
        res = self.h.dispatch("update_task_progress",
                              {"task_id": 1, "status": "IN_PROGRESS", "plan_id": 12345})
        self.assertContract(res)
        self.assertFalse(res["ok"])


class TestApprovalQueueEdges(HandlerTestCase):
    def store_for(self, name):
        return ApprovalStore(self.state_dir / name)

    def test_one_entry_per_plan(self):
        s = self.store_for("q1")
        s.publish("plan_a", "g", "d1", [], "fp1")
        second = s.publish("plan_a", "g", "d2", [], "fp2")
        queue = s.peek()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["id"], second)

    def test_sibling_entries_coexist_and_claim_independently(self):
        s = self.store_for("q2")
        ra = s.publish("plan_a", "ga", "da", [], "fpa")
        rb = s.publish("plan_b", "gb", "db", [], "fpb")
        self.assertEqual(len(s.peek()), 2)
        self.assertTrue(s.record_decision(rb, "APPROVED", "ok"))
        self.assertIsNone(s.claim(ra), "claiming A must not pick up B's decision")
        self.assertEqual(s.claim(rb), Verdict("APPROVED", "ok"))
        self.assertEqual([e["id"] for e in s.peek()], [ra])

    def test_fingerprint_mismatch_drops_only_that_entry(self):
        s = self.store_for("q3")
        ra = s.publish("plan_a", "ga", "da", [], "fpa")
        rb = s.publish("plan_b", "gb", "db", [], "fpb")
        s.record_decision(ra, "APPROVED", "")
        s.record_decision(rb, "APPROVED", "")
        self.assertIsNone(s.claim_for_plan("plan_a", "CHANGED"))
        self.assertEqual([e["id"] for e in s.peek()], [rb], "B's entry must survive")
        self.assertEqual(s.claim_for_plan("plan_b", "fpb"), Verdict("APPROVED", ""))

    def test_decision_cannot_be_recorded_twice(self):
        s = self.store_for("q4")
        r = s.publish("p", "g", "d", [], "fp")
        self.assertTrue(s.record_decision(r, "APPROVED", ""))
        self.assertFalse(s.record_decision(r, "REJECTED", ""))

    def test_unknown_request_id_is_rejected(self):
        s = self.store_for("q5")
        s.publish("p", "g", "d", [], "fp")
        self.assertFalse(s.record_decision("nope", "APPROVED", ""))

    def test_legacy_single_record_file_does_not_crash(self):
        """1.7.0 wrote one record; 1.8.0 writes a queue. Upgrading must not explode."""
        d = self.state_dir / "legacy"
        d.mkdir(parents=True, exist_ok=True)
        (d / "approval.json").write_text(
            json.dumps({"id": "old", "plan_id": "p", "decision": "APPROVED"}),
            encoding="utf-8")
        s = ApprovalStore(d)
        self.assertEqual(s.peek(), [])
        self.assertIsNone(s.claim("old"))
        self.assertEqual(s.publish("p2", "g", "d", [], "fp") and len(s.peek()), 1)

    def test_corrupt_approval_file_is_ignored(self):
        d = self.state_dir / "corrupt"
        d.mkdir(parents=True, exist_ok=True)
        (d / "approval.json").write_text("{{{ not json", encoding="utf-8")
        s = ApprovalStore(d)
        self.assertEqual(s.peek(), [])
        s.publish("p", "g", "d", [], "fp")
        self.assertEqual(len(s.peek()), 1)


class TestLeniencyDispatchEdges(HandlerTestCase):
    def test_dispatch_survives_every_hostile_input(self):
        """The real guarantee: hostile args yield the contract, never a raw error."""
        for tool in ("plan_and_think", "update_task_progress",
                     "request_user_approval", "get_current_plan"):
            for args in TestLeniency.HOSTILE_ARGS:
                res = self.h.dispatch(tool, args)
                self.assertContract(res)

    def test_dispatch_with_non_dict_args_gives_the_contract(self):
        for bad in ("string", 42, [1, 2], None, True):
            res = self.h.dispatch("plan_and_think", bad)
            self.assertContract(res)


class TestStoreFailureEdges(HandlerTestCase):
    def test_failed_save_is_not_reported_as_success(self):
        """A write that never landed must not come back as ok:true."""
        self.think(need_more_thinking=False, task_list=["a"])
        with mock.patch("planning.store.os.replace", side_effect=OSError("disk full")):
            res = self.think(goal="새 목표", need_more_thinking=False, task_list=["b"])
        self.assertFalse(res["ok"], "a lost write was reported as success")
        self.assertEqual(res["error_code"], "INTERNAL_ERROR")
        self.assertEqual(res["next_action"], "CALL_GET_CURRENT_PLAN")

    def test_state_survives_a_failed_save(self):
        first = self.think(need_more_thinking=False, task_list=["a"])
        with mock.patch("planning.store.os.replace", side_effect=OSError("disk full")):
            self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_id"], first["plan_id"])
        self.assertTrue(res["ok"])

    def test_structurally_wrong_state_file_is_quarantined(self):
        """Valid JSON of the wrong shape must not wedge every future call."""
        self.think(need_more_thinking=False, task_list=["a"])
        (self.state_dir / "plan_state.json").write_text(
            json.dumps({"schema_version": 1, "plans": [{"plan_id": "x"}]}), encoding="utf-8")
        fresh = PlanningHandlers(Store(self.state_dir), self.config)
        res = fresh.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertTrue(res["ok"], res.get("error_code"))
        self.assertEqual(res["plan_status"], "NONE")
        self.assertTrue(list(self.state_dir.glob("plan_state.corrupt.*.json")))

    def test_plans_entry_that_is_not_a_mapping(self):
        self.think(need_more_thinking=False, task_list=["a"])
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        raw["plans"]["broken"] = "not a plan"
        (self.state_dir / "plan_state.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        fresh = PlanningHandlers(Store(self.state_dir), self.config)
        res = fresh.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertTrue(res["ok"], res.get("error_code"))

    def test_audit_failure_does_not_break_the_call(self):
        with mock.patch("builtins.open", side_effect=OSError("nope")):
            self.h.store.audit("something", plan_id="x")  # must not raise


class TestApprovalStoreFailureEdges(HandlerTestCase):
    def test_publish_that_cannot_persist_reports_failure(self):
        s = ApprovalStore(self.state_dir / "ap1")
        # Patch the approval store's own write, not os.replace: os is a shared module,
        # so patching it there would also break the plan store and test the wrong thing.
        with mock.patch.object(ApprovalStore, "_write", return_value=False):
            request_id = s.publish("p", "g", "d", [], "fp")
        self.assertIsNone(request_id, "publish must not claim success it did not achieve")

    def test_blocking_degrades_loudly_when_the_queue_cannot_be_written(self):
        ui = ApprovalServer(ApprovalStore(self.state_dir / "ap2"), port=8799,
                            open_browser=False)
        h = PlanningHandlers(
            Store(self.state_dir),
            Config(state_dir=self.state_dir, blocking_approval=True, approval_timeout=1,
                   auto_ask=False),
            approval_ui=ui,
        )
        try:
            h.dispatch("plan_and_think", {"goal": "g", "thought": "t", "step_number": 1,
                                          "total_steps": 1, "need_more_thinking": False,
                                          "task_list": ["a"]})
            with mock.patch.object(ApprovalStore, "_write", return_value=False):
                res = h.dispatch("request_user_approval",
                                 {"decision": "ASK_USER", "plan_summary": "s"})
            self.assertTrue(
                any("NOT hard-paused" in n for n in res.get("input_notes", [])),
                f"degradation must be visible, got {res.get('input_notes')}",
            )
        finally:
            ui.shutdown()

    def test_decision_that_cannot_persist_reports_failure(self):
        s = ApprovalStore(self.state_dir / "ap3")
        rid = s.publish("p", "g", "d", [], "fp")
        with mock.patch.object(ApprovalStore, "_write", return_value=False):
            self.assertFalse(s.record_decision(rid, "APPROVED", ""))

    def test_only_one_claimer_wins_a_decision(self):
        s = ApprovalStore(self.state_dir / "ap4")
        rid = s.publish("p", "g", "d", [], "fp")
        s.record_decision(rid, "APPROVED", "ok")
        winners: list[tuple] = []

        def claim():
            got = s.claim(rid)
            if got:
                winners.append(got)

        threads = [threading.Thread(target=claim) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(winners), 1, f"decision consumed {len(winners)} times")

    def test_concurrent_publishes_all_survive(self):
        s = ApprovalStore(self.state_dir / "ap5")
        def pub(i):
            s.publish(f"plan_{i}", f"g{i}", "d", [], f"fp{i}")
        threads = [threading.Thread(target=pub, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(s.peek()), 6, "a concurrent publish was lost")

    def test_entry_is_dropped_when_its_plan_is_cancelled(self):
        """A dead plan must not keep asking the human for approval."""
        ui = FakeApprovalUI(decision=None)
        h = self.blocking_handlers(ui)
        h.dispatch("plan_and_think", {"goal": "g", "thought": "t", "step_number": 1,
                                      "total_steps": 1, "need_more_thinking": False,
                                      "task_list": ["a"]})
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        self.assertIsNotNone(ui.live)
        # Through the page, which is the only way a rejection can arrive now: while a
        # request is open, a REJECTED sent by the model is refused as its own invention.
        ui.resolve("REJECTED", "no")
        h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertIsNone(ui.live, "a cancelled plan left a stale approval on the page")

    def test_entry_is_dropped_when_its_plan_completes(self):
        ui = FakeApprovalUI(decision=None)
        h = self.blocking_handlers(ui)
        h.dispatch("plan_and_think", {"goal": "g", "thought": "t", "step_number": 1,
                                      "total_steps": 1, "need_more_thinking": False,
                                      "task_list": ["a"]})
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        ui.resolve("APPROVED")
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE"})
        self.assertIsNone(ui.live, "a finished plan left a stale approval on the page")

    def blocking_handlers(self, ui):
        return PlanningHandlers(
            Store(self.state_dir),
            Config(state_dir=self.state_dir, blocking_approval=True, approval_timeout=1),
            approval_ui=ui,
        )


class TestApprovalPageSurface(HandlerTestCase):
    """The page HTML/JS itself. The full click-through was verified in a real browser;
    these guard the template against regression without needing one."""

    def serve(self):
        srv = ApprovalServer(ApprovalStore(self.state_dir / "page"), port=8788,
                             open_browser=False)
        self.assertTrue(srv.start())
        return srv

    def get(self, srv, path):
        with urllib.request.urlopen(srv.url + path, timeout=5) as r:
            return r.read().decode("utf-8")

    def test_root_serves_html_with_the_escaping_function(self):
        srv = self.serve()
        try:
            html = self.get(srv, "")
            self.assertIn("<!doctype html>", html.lower())
            # The client-side escaper is what neutralises HTML in plan text.
            self.assertIn("function esc(", html)
            self.assertIn("&amp;", html)  # esc maps & -> &amp;
            self.assertIn("function decide(", html)
        finally:
            srv.shutdown()

    def test_per_task_comment_boxes_are_collapsed_by_default(self):
        """Nine open textareas bury the plan the human came here to read."""
        srv = self.serve()
        try:
            html = self.get(srv, "")
            self.assertIn(".tcrow{display:none", html)
            self.assertIn(".task.open .tcrow{display:flex}", html)
            self.assertIn("function toggleComment(", html)
            self.assertIn('onclick="toggleComment(this)"', html)
            # Hidden boxes are still submitted, so a filled row must stay marked.
            self.assertIn(".task.filled .tcbtn", html)
            self.assertIn("classList.toggle('filled'", html)
            # And a hidden feature nobody is told about is a removed feature.
            self.assertIn("[의견]", html)
        finally:
            srv.shutdown()

    def test_api_returns_content_raw_for_the_client_to_escape(self):
        """Escaping is the page's job; the API must not double-escape or drop content."""
        srv = self.serve()
        try:
            srv.store.publish("p", "<script>x</script>", "d & <b>", [], "fp")
            payload = json.loads(self.get(srv, "api/pending"))
            entry = payload["requests"][0]
            self.assertEqual(entry["goal"], "<script>x</script>")
            self.assertEqual(entry["display"], "d & <b>")
        finally:
            srv.shutdown()

    def test_health_endpoint_identifies_the_server(self):
        srv = self.serve()
        try:
            info = json.loads(self.get(srv, "api/health"))
            self.assertEqual(info["server"], "planning-mcp-approval")
            self.assertIn("state_dir", info)
        finally:
            srv.shutdown()

    def test_decide_endpoint_round_trip(self):
        srv = self.serve()
        try:
            rid = srv.store.publish("p", "g", "d", [], "fp")
            body = json.dumps({"id": rid, "decision": "APPROVED", "comment": "좋아요"}).encode()
            req = urllib.request.Request(srv.url + "api/decide", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertTrue(json.loads(r.read().decode())["ok"])
            self.assertEqual(srv.claim(rid), Verdict("APPROVED", "좋아요"))
        finally:
            srv.shutdown()

    def test_decide_with_bad_body_is_rejected_not_crashed(self):
        srv = self.serve()
        try:
            for body in (b"not json", b'{"id":"x"}', b'{"decision":"MAYBE"}', b"{}"):
                req = urllib.request.Request(srv.url + "api/decide", data=body,
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        payload = json.loads(r.read().decode())
                        self.assertFalse(payload.get("ok"))
                except urllib.error.HTTPError as e:
                    self.assertIn(e.code, (400, 404))
        finally:
            srv.shutdown()

    def test_unknown_path_is_404_not_500(self):
        srv = self.serve()
        try:
            for path in ("nope", "api/nope", "../etc/passwd", "api/pending/x"):
                try:
                    with urllib.request.urlopen(srv.url + path, timeout=5) as r:
                        self.assertNotEqual(r.status, 500)
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 404)
        finally:
            srv.shutdown()


class TargetedRevisionFixture(HandlerTestCase):
    """Helpers for driving a plan to a per-task revision request. Holds no tests."""

    AUTO_ASK = False  # its helpers ask, and read the decision off that call

    TASKS = ["보고서 찾기", "표 추출", "요약 작성"]

    def blocking(self, ui, timeout=1):
        cfg = Config(
            state_dir=self.state_dir, blocking_approval=True, approval_timeout=timeout,
            auto_ask=self.AUTO_ASK,
        )
        return PlanningHandlers(Store(self.state_dir), cfg, approval_ui=ui)

    def draft(self, h, tasks=None):
        return h.dispatch("plan_and_think", {
            "goal": "분기 보고서 정리", "thought": "t", "step_number": 1, "total_steps": 1,
            "need_more_thinking": False, "task_list": tasks or self.TASKS})

    def flagged(self, comments, tasks=None, scope="TASKS"):
        """Drive a plan to AWAITING_APPROVAL, then have the human flag some tasks."""
        ui = FakeApprovalUI("REVISE", "", task_comments=comments, scope=scope)
        h = self.blocking(ui)
        self.draft(h, tasks)
        res = h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "요약"}
        )
        return h, res

    def update(self, h, updates):
        return h.dispatch("plan_and_think", {
            "goal": "분기 보고서 정리", "thought": "고쳤습니다", "step_number": 2,
            "total_steps": 2, "need_more_thinking": False, "task_updates": updates})

    def run_all(self, h, count=3):
        """Execute every task the way the server demands: start, then finish with evidence."""
        for task_id in range(1, count + 1):
            h.dispatch("update_task_progress", {"task_id": task_id, "status": "IN_PROGRESS"})
            h.dispatch("update_task_progress", {
                "task_id": task_id, "status": "DONE",
                "result_log": f"태스크 {task_id} 결과를 /tmp/out{task_id}.txt 에 저장"})

    def reported(self, ui=None, tasks=None):
        """Drive a plan to APPROVED, run every task, and stop at the completion report."""
        ui = ui if ui is not None else FakeApprovalUI("APPROVED")
        h = self.blocking(ui)
        self.draft(h, tasks)
        res = h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "요약"}
        )
        self.run_all(h, len(tasks or self.TASKS))
        return h, res

    def plan(self, h):
        return h.store.load().active_plan

    def audit(self):
        return (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")


class TestTargetedRevision(TargetedRevisionFixture):
    """Per-task review.

    The human comments on individual tasks on the approval page; only those tasks are
    rewritten. The tasks they read and accepted keep their wording, their number, and -
    if the plan had already been running - their evidence.
    """

    # ---- the request ---------------------------------------------------
    def test_the_summary_reaches_the_approval_page(self):
        """The human cannot judge a bare task list; the overview must travel with it."""
        ui = FakeApprovalUI(decision=None)  # no decision: just capture what was published
        h = self.blocking(ui)
        self.draft(h)
        h.dispatch("request_user_approval", {
            "decision": "ASK_USER",
            "plan_summary": "보고서를 찾아 표를 뽑고 5줄로 요약합니다."})
        published = ui.opened[-1]
        self.assertEqual(published["phase"], "PLAN")
        self.assertEqual(published["goal"], "분기 보고서 정리")
        self.assertEqual(published["summary"], "보고서를 찾아 표를 뽑고 5줄로 요약합니다.")

    def test_flagged_tasks_are_recorded_as_targets(self):
        h, res = self.flagged({"2": "표를 그대로 넣어주세요"})
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["revision_scope"], "TASKS")
        self.assertEqual(
            res["revision_targets"],
            [{"task_id": 2, "title": "표 추출", "user_comment": "표를 그대로 넣어주세요"}],
        )
        self.assertEqual(res["tasks_unchanged"], [1, 3])
        self.assertEqual(self.plan(h).revision_targets(), {2: "표를 그대로 넣어주세요"})

    def test_hint_hands_the_model_the_exact_call(self):
        """A weak model copies the hint; it must therefore contain the literal argument."""
        _, res = self.flagged({"2": "표를 그대로"})
        hint = res["next_action_hint"]
        self.assertIn("task_updates", hint)
        self.assertIn('"task_id": 2', hint)
        self.assertIn("do NOT send task_list", hint)
        self.assertIn("1, 3", hint)  # the tasks that must not move

    def test_whole_plan_scope_leaves_no_targets(self):
        """The page said 'rewrite everything', so nothing is pinned down."""
        h, res = self.flagged({"2": "표를 그대로"}, scope="PLAN")
        self.assertEqual(res["revision_scope"], "PLAN")
        self.assertIsNone(self.plan(h).pending_revision)
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

    # ---- applying the revision -----------------------------------------
    def test_only_the_flagged_task_is_rewritten(self):
        h, _ = self.flagged({"2": "표를 그대로 넣어주세요"})
        res = self.update(h, [{"task_id": 2, "title": "표를 원본 그대로 삽입"}])
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["revised_tasks"], [2])
        titles = [t.title for t in self.plan(h).tasks]
        self.assertEqual(titles, ["보고서 찾기", "표를 원본 그대로 삽입", "요약 작성"])
        task = self.plan(h).get_task(2)
        self.assertEqual(task.previous_title, "표 추출")
        self.assertEqual(task.revision_note, "표를 그대로 넣어주세요")
        self.assertIsNone(self.plan(h).pending_revision)

    def test_task_ids_are_not_renumbered(self):
        """The whole point: task 3 is still task 3, so nothing the human read moved."""
        h, _ = self.flagged({"1": "다른 폴더를 보세요"})
        self.update(h, [{"task_id": 1, "title": "아카이브 폴더에서 보고서 찾기"}])
        self.assertEqual([t.task_id for t in self.plan(h).tasks], [1, 2, 3])
        self.assertEqual(self.plan(h).get_task(3).title, "요약 작성")

    def test_unflagged_edits_are_ignored_with_a_note(self):
        h, _ = self.flagged({"2": "표를 그대로"})
        res = self.update(h, [
            {"task_id": 2, "title": "표를 원본 그대로 삽입"},
            {"task_id": 3, "title": "몰래 바꾼 요약"},
        ])
        self.assertTrue(res["ok"])
        self.assertEqual(self.plan(h).get_task(3).title, "요약 작성")
        self.assertTrue(
            any("did not ask" in n for n in res.get("input_notes", [])),
            f"the silent edit must be reported, got {res.get('input_notes')}",
        )

    def test_a_second_target_left_unaddressed_is_reported(self):
        h, _ = self.flagged({"1": "다른 폴더", "3": "더 길게"})
        res = self.update(h, [{"task_id": 1, "title": "아카이브에서 찾기"}])
        self.assertTrue(res["ok"])
        self.assertTrue(
            any("did not rewrite" in n for n in res.get("input_notes", [])),
            f"got {res.get('input_notes')}",
        )

    def test_unknown_task_id_changes_nothing(self):
        h, _ = self.flagged({"2": "표를 그대로"})
        res = self.update(h, [
            {"task_id": 2, "title": "표를 원본 그대로 삽입"},
            {"task_id": 99, "title": "존재하지 않는 태스크"},
        ])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "TASK_NOT_FOUND")
        # Validation runs before any write, so the partial edit must not have landed.
        self.assertEqual(self.plan(h).get_task(2).title, "표 추출")
        self.assertEqual(self.plan(h).plan_status, "DRAFTING")
        self.assertEqual(self.plan(h).revision_targets(), {2: "표를 그대로"})

    def test_addressing_none_of_the_flagged_tasks_is_refused(self):
        h, _ = self.flagged({"2": "표를 그대로"})
        res = self.update(h, [{"task_id": 1, "title": "엉뚱한 태스크를 고침"}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "REVISION_INCOMPLETE")
        self.assertIn("2", res["next_action_hint"])
        self.assertEqual(self.plan(h).get_task(1).title, "보고서 찾기")

    def test_finalizing_a_revision_with_nothing_at_all_is_refused(self):
        h, _ = self.flagged({"2": "표를 그대로"})
        res = h.dispatch("plan_and_think", {
            "goal": "분기 보고서 정리", "thought": "끝", "step_number": 2, "total_steps": 2,
            "need_more_thinking": False})
        self.assertEqual(res["error_code"], "REVISION_INCOMPLETE")

    def test_task_updates_without_a_request_are_refused(self):
        """Otherwise the model could edit an approved plan on its own authority."""
        self.h.dispatch("plan_and_think", {
            "goal": "g", "thought": "t", "step_number": 1, "total_steps": 1,
            "need_more_thinking": False, "task_list": ["하나", "둘"]})
        res = self.h.dispatch("plan_and_think", {
            "goal": "g", "thought": "t2", "step_number": 2, "total_steps": 2,
            "need_more_thinking": False,
            "task_updates": [{"task_id": 1, "title": "몰래 바꾼 태스크"}]})
        # Since 1.16 a finalized plan is not reopened by plan_and_think at all, so the
        # edit never reaches the task list.
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        self.assertEqual(self.h.store.load().active_plan.get_task(1).title, "하나")
        self.assertIn("replan_redirected", self.audit())

    def test_task_updates_while_drafting_without_a_request_are_refused(self):
        """The same edit during a drafting round (after a failure, say) is refused."""
        self.h.dispatch("plan_and_think", {
            "goal": "g", "thought": "t", "step_number": 1, "total_steps": 2,
            "need_more_thinking": True})
        res = self.h.dispatch("plan_and_think", {
            "goal": "g", "thought": "t2", "step_number": 2, "total_steps": 2,
            "need_more_thinking": False,
            "task_updates": [{"task_id": 1, "title": "몰래 바꾼 태스크"}]})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "REVISION_NOT_REQUESTED")
        self.assertIn("unrequested_task_updates", self.audit())

    def test_whole_plan_rewrite_is_accepted_but_recorded(self):
        """Wasteful, not unsafe - the human re-approves everything either way."""
        h, _ = self.flagged({"2": "표를 그대로"})
        res = h.dispatch("plan_and_think", {
            "goal": "분기 보고서 정리", "thought": "다시", "step_number": 2, "total_steps": 2,
            "need_more_thinking": False, "task_list": ["새 하나", "새 둘"]})
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual([t.title for t in self.plan(h).tasks], ["새 하나", "새 둘"])
        self.assertIsNone(self.plan(h).pending_revision)
        self.assertIn("targeted_revision_ignored", self.audit())

    def test_task_list_alongside_task_updates_is_ignored(self):
        h, _ = self.flagged({"2": "표를 그대로"})
        res = h.dispatch("plan_and_think", {
            "goal": "분기 보고서 정리", "thought": "고침", "step_number": 2, "total_steps": 2,
            "need_more_thinking": False,
            "task_updates": [{"task_id": 2, "title": "표를 원본 그대로 삽입"}],
            "task_list": ["완전히 다른 계획"]})
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.plan(h).tasks), 3)
        self.assertEqual(self.plan(h).get_task(2).title, "표를 원본 그대로 삽입")

    # ---- what makes it worth doing --------------------------------------
    def test_evidence_of_untouched_tasks_survives(self):
        """A plan that has already run keeps the work nobody objected to.

        This is the case a whole-plan rewrite destroys: every DONE task is replaced by a
        fresh PENDING one and the agent redoes work the human never questioned.
        """
        h, _ = self.flagged({"2": "표를 그대로"})
        # Shape of a plan whose approval expired mid-execution: task 1 already finished.
        path = self.state_dir / "plan_state.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        pid = raw["active_plan_id"]
        raw["plans"][pid]["tasks"][0].update(
            {"status": "DONE", "result_log": "/reports/q3.xlsx 에서 찾음"}
        )
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        self.update(h, [{"task_id": 2, "title": "표를 원본 그대로 삽입"}])
        first, second = self.plan(h).get_task(1), self.plan(h).get_task(2)
        self.assertEqual(first.status, "DONE")
        self.assertEqual(first.result_log, "/reports/q3.xlsx 에서 찾음")
        # The rewritten one is a different task now, so its old outcome cannot stand.
        self.assertEqual(second.status, "PENDING")
        self.assertIsNone(second.result_log)

    def test_previous_task_list_is_kept_as_evidence(self):
        h, _ = self.flagged({"2": "표를 그대로"})
        self.update(h, [{"task_id": 2, "title": "표를 원본 그대로 삽입"}])
        superseded = self.plan(h).superseded_tasks
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0][1]["title"], "표 추출")

    # ---- what the human sees next ---------------------------------------
    def test_display_marks_the_revised_task_only(self):
        h, _ = self.flagged({"2": "표를 그대로 넣어주세요"})
        self.update(h, [{"task_id": 2, "title": "표를 원본 그대로 삽입"}])
        text = render_plan_for_user(self.plan(h), "수정했습니다")
        self.assertIn("↻ 2. 표를 원본 그대로 삽입", text)
        self.assertIn("이전: 표 추출", text)
        self.assertIn("요청하신 내용: 표를 그대로 넣어주세요", text)
        self.assertIn("1. 보고서 찾기", text)
        self.assertNotIn("↻ 1.", text)

    def test_revision_marks_are_cleared_once_approved(self):
        h, _ = self.flagged({"2": "표를 그대로"})
        self.update(h, [{"task_id": 2, "title": "표를 원본 그대로 삽입"}])
        h.approval_ui.decision = "APPROVED"
        h.approval_ui.scope = "PLAN"
        res = h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "다시 요약"}
        )
        self.assertEqual(res["plan_status"], "APPROVED")
        task = self.plan(h).get_task(2)
        self.assertIsNone(task.revision_note)
        self.assertIsNone(task.previous_title)

    def test_completion_phase_is_published_as_such(self):
        h, _ = self.reported()
        h.approval_ui.decision = None  # capture the report instead of answering it
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "완료 보고"})
        self.assertEqual(h.approval_ui.opened[-1]["phase"], "COMPLETION")


class TestCompletionRework(TargetedRevisionFixture):
    """Sending finished work back, task by task.

    The failure this replaces: a completion-report REVISE was always a whole-plan
    redraft, so the model was handed a DRAFTING plan whose tasks were all DONE, told
    only "re-plan", and then - because finalizing replaces the task list - lost every
    result_log it had written. It answered by redoing the entire plan, or by inventing
    evidence to get past the DONE guards. Neither is the fix the human asked for.
    """

    def sent_back(self, comments, comment="", scope="TASKS", tasks=None):
        """...then have the human send specific tasks back from that report."""
        ui = FakeApprovalUI("APPROVED")
        h, _ = self.reported(ui, tasks)
        self.assertEqual(self.plan(h).plan_status, "AWAITING_COMPLETION")
        ui.decision, ui.comment = "REVISE", comment
        ui.scope, ui.task_comments = scope, comments
        res = h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "완료 보고"}
        )
        return h, res

    # ---- the mutation ---------------------------------------------------
    def test_only_the_named_task_reopens(self):
        h, res = self.sent_back({"2": "표가 3개 빠졌어요"})
        self.assertEqual(res["plan_status"], "IN_EXECUTION")
        self.assertEqual(res["reopened_tasks"], [2])
        plan = self.plan(h)
        reopened = plan.get_task(2)
        self.assertEqual(reopened.status, "PENDING")
        self.assertEqual(reopened.revision_note, "표가 3개 빠졌어요")
        self.assertIsNone(reopened.result_log)
        self.assertIsNone(reopened.started_at)
        for task_id in (1, 3):
            kept = plan.get_task(task_id)
            self.assertEqual(kept.status, "DONE", f"task {task_id} lost its status")
            self.assertIn(f"/tmp/out{task_id}.txt", kept.result_log or "")

    def test_the_previous_output_is_kept_so_the_redo_is_not_blind(self):
        h, res = self.sent_back({"2": "표가 3개 빠졌어요"})
        self.assertEqual(self.plan(h).get_task(2).previous_result_log,
                         "태스크 2 결과를 /tmp/out2.txt 에 저장")
        brief = next(t for t in res["tasks"] if t["task_id"] == 2)
        self.assertIn("previous_result_log", brief)

    def test_the_plan_is_not_sent_back_for_approval(self):
        """The task list did not change, so re-approving it is pure ceremony - and a
        weak model spends two turns and a chance to derail on every round of it."""
        h, res = self.sent_back({"2": "표가 3개 빠졌어요"})
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        self.assertNotEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertNotEqual(res["plan_status"], "DRAFTING")
        # And execution really is open, not merely described as open.
        started = h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        self.assertTrue(started["ok"], started.get("error_code"))

    def test_the_task_list_itself_is_untouched(self):
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        plan = self.plan(h)
        self.assertEqual([t.title for t in plan.tasks], self.TASKS)
        self.assertIsNone(plan.pending_revision)

    def test_it_is_audited_as_rework_not_as_a_revision(self):
        self.sent_back({"2": "표가 3개 빠졌어요"}, comment="표를 다시 봐주세요")
        audit = self.audit()
        self.assertIn("rework_requested", audit)
        self.assertIn("표가 3개 빠졌어요", audit)

    # ---- what the model is told -----------------------------------------
    def test_the_hint_carries_the_users_own_words(self):
        """A weak model loses a string that appeared once, three turns ago. The request
        has to live in the hint until it is answered."""
        _, res = self.sent_back({"2": "표가 3개 빠졌어요"})
        hint = res["next_action_hint"]
        self.assertIn("표가 3개 빠졌어요", hint)
        self.assertIn("Redo ONLY that task", hint)
        # Which task it is now travels in the field, not in the sentence - so the
        # sentence must not name one and the field must.
        self.assertNotIn("task_id=2", hint)
        self.assertEqual(res["next_task"]["task_id"], 2)
        self.assertEqual(res["next_task"]["revision_note"], "표가 3개 빠졌어요")

    def test_the_hint_forbids_re_planning_and_protects_accepted_work(self):
        _, res = self.sent_back({"2": "표가 3개 빠졌어요"})
        hint = res["next_action_hint"]
        self.assertIn("Do not re-plan", hint)
        self.assertIn("do NOT redo", hint)
        self.assertIn("The other tasks were accepted", hint)

    def test_the_request_survives_into_the_in_progress_hint(self):
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        res = h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        self.assertIn("표가 3개 빠졌어요", res["next_action_hint"])

    def test_a_second_reopened_task_keeps_its_request_when_reached(self):
        """Task 3 is reached by finishing task 2; the message announcing that is the
        only thing the model reads at that moment."""
        h, _ = self.sent_back({"2": "표를 다시", "3": "요약이 너무 짧아요"})
        h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        res = h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": "표 3개를 모두 넣어 다시 저장함"})
        self.assertIn("요약이 너무 짧아요", res["message"])
        self.assertIn("요약이 너무 짧아요", res["next_action_hint"])

    # ---- reopened tasks with finished work between them -------------------
    FIVE = [f"작업{i}" for i in range(1, 6)]

    def gapped(self):
        """Tasks 2 and 5 sent back out of a 5-task plan; 3 and 4 stay DONE between them."""
        return self.sent_back(
            {"2": "표가 빠졌어요", "5": "요약이 너무 짧아요"}, tasks=self.FIVE
        )

    def statuses(self, h):
        return {t.task_id: t.status for t in self.plan(h).tasks}

    def finish(self, h, task_id, log, start=True):
        if start:
            h.dispatch("update_task_progress", {"task_id": task_id, "status": "IN_PROGRESS"})
        return h.dispatch("update_task_progress", {
            "task_id": task_id, "status": "DONE", "result_log": log})

    def test_finishing_one_reopened_task_hands_over_the_next_one_across_the_gap(self):
        """The reopened tasks are not adjacent. Task 2 is followed by two tasks that are
        still DONE, so the auto-advance has to skip them and land on 5."""
        h, res = self.gapped()
        self.assertEqual(res["reopened_tasks"], [2, 5])
        self.assertEqual(res["tasks_unchanged"], [1, 3, 4])
        self.assertEqual(res["next_task"]["task_id"], 2)

        res = self.finish(h, 2, "표 3개를 모두 넣어 out2.txt 재저장")
        self.assertEqual(res["next_task"]["task_id"], 5)
        self.assertEqual(
            self.statuses(h),
            {1: "DONE", 2: "DONE", 3: "DONE", 4: "DONE", 5: "IN_PROGRESS"},
        )

    def test_the_tasks_in_the_gap_are_never_touched(self):
        h, _ = self.gapped()
        res = self.finish(h, 2, "표 3개를 모두 넣어 out2.txt 재저장")
        for task_id in (1, 3, 4):
            task = self.plan(h).get_task(task_id)
            self.assertEqual(task.status, "DONE")
            self.assertIn(f"/tmp/out{task_id}.txt", task.result_log or "")
            self.assertIsNone(task.revision_note)

    def test_the_request_survives_the_gap(self):
        """Crossing two accepted tasks must not drop what the user said about task 5."""
        h, _ = self.gapped()
        res = self.finish(h, 2, "표 3개를 모두 넣어 out2.txt 재저장")
        self.assertIn("요약이 너무 짧아요", res["message"])
        self.assertIn("요약이 너무 짧아요", res["next_action_hint"])
        # Auto-advance already started it, so the hint must not ask for IN_PROGRESS again.
        self.assertIn("It is already IN_PROGRESS", res["next_action_hint"])

    def test_a_later_reopened_task_cannot_jump_the_earlier_one(self):
        """Ordering still binds across a rework: 5 may not run while 2 is outstanding."""
        h, _ = self.gapped()
        res = h.dispatch("update_task_progress", {"task_id": 5, "status": "IN_PROGRESS"})
        self.assertEqual(res["next_task"]["task_id"], 2)  # redirected, not rejected
        self.assertEqual(self.plan(h).get_task(5).status, "PENDING")
        res = h.dispatch("update_task_progress", {
            "task_id": 5, "status": "DONE", "result_log": "요약을 12줄로 늘려 저장함"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "TASK_NOT_STARTED")

    def test_the_gapped_plan_returns_to_verification_only_after_both(self):
        h, _ = self.gapped()
        res = self.finish(h, 2, "표 3개를 모두 넣어 out2.txt 재저장")
        self.assertEqual(res["plan_status"], "IN_EXECUTION")
        self.assertEqual(res["progress"], "4/5 done")
        # Auto-advance already started task 5, so it is finished without IN_PROGRESS.
        res = self.finish(h, 5, "요약을 12줄로 늘려 out5.txt 재저장", start=False)
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertEqual(res["progress"], "5/5 done")

    # ---- what a disobedient model cannot do -------------------------------
    def test_resubmitting_the_rejected_outcome_is_refused(self):
        """The cheapest way to 'finish' a rework is to re-report what was already there.
        The user rejected that outcome, so it cannot also be the answer to their request."""
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        old = self.plan(h).get_task(2).previous_result_log
        h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        res = h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": old})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "REWORK_NOT_DONE")
        self.assertEqual(self.plan(h).get_task(2).status, "IN_PROGRESS")
        self.assertIn("표가 3개 빠졌어요", res["next_action_hint"])

    def test_cosmetic_whitespace_does_not_launder_the_old_outcome(self):
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        old = self.plan(h).get_task(2).previous_result_log
        h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        res = h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": f"  {old.upper()}  "})
        self.assertEqual(res["error_code"], "REWORK_NOT_DONE")

    def test_a_genuinely_new_outcome_is_accepted(self):
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        res = h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": "표 3개를 모두 넣어 다시 저장함"})
        self.assertTrue(res["ok"], res.get("error_code"))
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")

    def test_an_ordinary_task_may_repeat_an_earlier_wording(self):
        """The guard is scoped to reopened tasks. A plain plan has no rejected outcome,
        so two tasks that legitimately produce the same sentence must not be refused."""
        h, _ = self.reported()
        plan = self.plan(h)
        self.assertTrue(all(t.previous_result_log is None for t in plan.tasks))

    def test_accepted_tasks_cannot_be_restarted_or_overwritten(self):
        """Redoing work nobody complained about is refused by the state machine, not
        only discouraged by the hint."""
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        res = h.dispatch("update_task_progress", {"task_id": 3, "status": "IN_PROGRESS"})
        self.assertEqual(self.plan(h).get_task(3).status, "DONE")
        self.assertEqual(res["next_task"]["task_id"], 2)  # redirected at the open one
        before = self.plan(h).get_task(3).result_log
        h.dispatch("update_task_progress", {
            "task_id": 3, "status": "DONE", "result_log": "3번을 멋대로 다시 만들었습니다"})
        self.assertEqual(self.plan(h).get_task(3).result_log, before)

    def test_re_planning_during_a_rework_cannot_touch_the_plan(self):
        """A model that reaches for plan_and_think out of habit - which is what the old
        behaviour taught it - must not be able to wipe the task list or its evidence."""
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        titles = [t.title for t in self.plan(h).tasks]
        res = h.dispatch("plan_and_think", {
            "goal": "분기 보고서 정리", "thought": "다시 짜자", "step_number": 2,
            "total_steps": 2, "need_more_thinking": False,
            "task_list": ["완전히", "다른", "계획"]})
        self.assertEqual(res["plan_status"], "IN_EXECUTION")
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        plan = self.plan(h)
        self.assertEqual([t.title for t in plan.tasks], titles)
        self.assertEqual([t.task_id for t in plan.tasks if t.status == "DONE"], [1, 3])

    # ---- the loop closes ------------------------------------------------
    def test_redoing_the_task_returns_to_the_completion_report(self):
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        res = h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": "표 3개를 모두 넣어 다시 저장함"})
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertEqual(res["progress"], "3/3 done")

    def test_the_new_report_shows_what_was_asked_and_what_changed(self):
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        h.approval_ui.decision = None  # capture the report instead of answering it
        h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": "표 3개를 모두 넣어 다시 저장함"})
        res = h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "다시 했습니다"}
        )
        report = res["display_to_user"]
        self.assertIn("요청하신 내용: 표가 3개 빠졌어요", report)
        self.assertIn("이전 결과: 태스크 2 결과를 /tmp/out2.txt 에 저장", report)
        self.assertIn("표 3개를 모두 넣어 다시 저장함", report)

    def test_approving_the_second_report_completes_the_plan(self):
        h, _ = self.sent_back({"2": "표가 3개 빠졌어요"})
        h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": "표 3개를 모두 넣어 다시 저장함"})
        h.approval_ui.decision = "APPROVED"
        res = h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "다시 했습니다"}
        )
        self.assertEqual(res["plan_status"], "COMPLETED")
        # A completed plan is no longer active, so it is read back by id.
        finished = h.store.load().plans[res["plan_id"]]
        self.assertIsNone(finished.get_task(2).revision_note)
        self.assertIsNone(finished.get_task(2).previous_result_log)

    # ---- the whole-plan escape hatch still exists ------------------------
    def test_the_whole_plan_checkbox_still_reopens_planning(self):
        """'계획 자체를 다시 세우기' is for adding or deleting a task, and that really
        does need a new plan and a new approval."""
        _, res = self.sent_back({}, comment="4번 단계가 통째로 빠졌어요", scope="PLAN")
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")


class TestEvidenceSurvivesARedraft(TargetedRevisionFixture):
    """Part 2: a whole-plan revision asked for from the completion report must not
    delete work that has already been done. Only that path - an ordinary re-plan after
    a failure should still drop stale evidence."""

    def redrafted(self, new_tasks, comment="4번이 빠졌어요"):
        ui = FakeApprovalUI("APPROVED")
        h, _ = self.reported(ui)
        ui.decision, ui.comment, ui.scope = "REVISE", comment, "PLAN"
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "완료 보고"})
        res = h.dispatch("plan_and_think", {
            "goal": "분기 보고서 정리", "thought": "단계를 추가합니다", "step_number": 2,
            "total_steps": 2, "need_more_thinking": False, "task_list": new_tasks})
        return h, res

    def statuses(self, h):
        return {t.title: t.status for t in self.plan(h).tasks}

    def test_surviving_tasks_keep_their_work(self):
        h, res = self.redrafted(self.TASKS + ["검수 받기"])
        self.assertEqual(
            self.statuses(h),
            {"보고서 찾기": "DONE", "표 추출": "DONE", "요약 작성": "DONE", "검수 받기": "PENDING"},
        )
        self.assertIn("/tmp/out2.txt", self.plan(h).get_task(2).result_log or "")
        self.assertIn("survived this rewrite", " ".join(res["input_notes"]))

    def test_a_changed_title_is_a_different_task_and_starts_over(self):
        h, _ = self.redrafted(["보고서 찾기", "표 추출 후 검증", "요약 작성"])
        self.assertEqual(
            self.statuses(h),
            {"보고서 찾기": "DONE", "표 추출 후 검증": "PENDING", "요약 작성": "DONE"},
        )

    def test_matching_is_lenient_at_the_edges_like_goal_matching(self):
        """A retyped title differing only in punctuation or spacing is the same task -
        the same relaxation `goal_key` applies to routing."""
        h, _ = self.redrafted(["보고서 찾기.", "표  추출 ", " 요약 작성"])
        self.assertEqual(
            [t.status for t in self.plan(h).tasks], ["DONE", "DONE", "DONE"]
        )

    def test_reordering_keeps_the_work_because_matching_is_by_key(self):
        h, _ = self.redrafted(["요약 작성", "보고서 찾기", "표 추출"])
        self.assertEqual(self.statuses(h),
                         {"요약 작성": "DONE", "보고서 찾기": "DONE", "표 추출": "DONE"})

    def test_an_all_done_redraft_goes_back_to_verification_not_to_execution(self):
        """Everything carried, so approving it must not unlock a plan with nothing left
        to run and let the model answer without the completion check."""
        h, _ = self.redrafted(list(self.TASKS))
        h.approval_ui.decision = "APPROVED"
        res = h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "그대로입니다"}
        )
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_an_ordinary_replan_still_drops_stale_evidence(self):
        """Regression guard: the carry-over is scoped to the completion path only."""
        h = self.blocking(FakeApprovalUI("REVISE", "다시 세워주세요", scope="PLAN"))
        self.draft(h)
        h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "요약"})
        self.assertFalse(self.plan(h).rework_from_completion)
        h.dispatch("plan_and_think", {
            "goal": "분기 보고서 정리", "thought": "다시", "step_number": 2, "total_steps": 2,
            "need_more_thinking": False, "task_list": list(self.TASKS)})
        self.assertEqual([t.status for t in self.plan(h).tasks],
                         ["PENDING", "PENDING", "PENDING"])


class TestTitleKey(unittest.TestCase):
    """`title_key` decides whether finished work survives a redraft, so where its
    leniency stops is a guarantee, not an implementation detail."""

    def test_edges_and_inner_whitespace_are_normalized(self):
        for variant in ("표 추출", " 표 추출 ", "표 추출.", "표 추출!", "표  추출", "표\t추출"):
            self.assertEqual(title_key(variant), title_key("표 추출"), variant)

    def test_different_wording_is_a_different_task(self):
        for other in ("표 추출 후 검증", "표추출", "표 정리", "TABLE 추출"):
            self.assertNotEqual(title_key(other), title_key("표 추출"), other)

    def test_case_is_significant_just_as_it_is_for_goals(self):
        self.assertNotEqual(title_key("Extract the table"), title_key("extract the table"))

    def test_empty_input_is_survivable(self):
        self.assertEqual(title_key(""), "")
        self.assertEqual(title_key(None), "")


class TestRevisionScopeAtTheStore(HandlerTestCase):
    """The page decides the scope; the store only refuses what cannot be honoured."""

    def store_for(self, name):
        return ApprovalStore(self.state_dir / name)

    def test_task_comments_round_trip(self):
        s = self.store_for("r1")
        rid = s.publish("plan_a", "g", "d", [{"task_id": 1}], "fp", phase="PLAN")
        self.assertTrue(
            s.record_decision(rid, "REVISE", "", {"2": " 표를 그대로 "}, "TASKS")
        )
        self.assertEqual(
            s.claim(rid), Verdict("REVISE", "", {"2": "표를 그대로"}, "TASKS")
        )

    def test_blank_and_unreadable_comments_are_dropped(self):
        s = self.store_for("r2")
        rid = s.publish("plan_a", "g", "d", [], "fp", phase="PLAN")
        s.record_decision(
            rid, "REVISE", "", {"1": "   ", "x": "노이즈", "3": "실제 의견"}, "TASKS"
        )
        self.assertEqual(s.claim(rid).task_comments, {"3": "실제 의견"})

    def test_completion_request_can_carry_a_task_scope(self):
        """On a completion report "these tasks only" means redo them, not retitle them -
        but that reading belongs to the handler. The store just carries it faithfully."""
        s = self.store_for("r3")
        rid = s.publish("plan_a", "g", "d", [], "fp", phase="COMPLETION")
        s.record_decision(rid, "REVISE", "", {"2": "안 된 것 같아요"}, "TASKS")
        verdict = s.claim(rid)
        self.assertEqual(verdict.scope, "TASKS")
        self.assertEqual(verdict.task_comments, {"2": "안 된 것 같아요"})

    def test_entry_without_a_phase_cannot_carry_a_task_scope(self):
        """Written by an older process during a rolling restart; degrade, do not guess."""
        s = self.store_for("r4")
        rid = s.publish("plan_a", "g", "d", [], "fp")
        record = json.loads(s.path.read_text(encoding="utf-8"))
        for entry in record["requests"]:
            entry.pop("phase", None)
        s.path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        s.record_decision(rid, "REVISE", "", {"2": "고쳐주세요"}, "TASKS")
        self.assertEqual(s.claim(rid).scope, "PLAN")

    def test_unknown_scope_falls_back_to_whole_plan(self):
        s = self.store_for("r5")
        rid = s.publish("plan_a", "g", "d", [], "fp", phase="PLAN")
        s.record_decision(rid, "REVISE", "", {"2": "x"}, "EVERYTHING")
        self.assertEqual(s.claim(rid).scope, "PLAN")

    def test_old_decided_entry_reads_as_a_whole_plan_revision(self):
        s = self.store_for("r6")
        rid = s.publish("plan_a", "g", "d", [], "fp", phase="PLAN")
        s.record_decision(rid, "REVISE", "고쳐줘")
        record = json.loads(s.path.read_text(encoding="utf-8"))
        for entry in record["requests"]:  # as an older build would have written it
            entry.pop("scope", None)
            entry.pop("task_comments", None)
        s.path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(s.claim(rid), Verdict("REVISE", "고쳐줘", {}, "PLAN"))


class TestPerTaskPageSurface(HandlerTestCase):
    """The page needs the task list and the phase to offer per-task review at all."""

    def serve(self, name="ptp", port=8789):
        srv = ApprovalServer(ApprovalStore(self.state_dir / name), port=port,
                             open_browser=False)
        self.assertTrue(srv.start())
        return srv

    def test_pending_exposes_tasks_and_phase(self):
        srv = self.serve()
        try:
            srv.store.publish(
                "p", "목표", "디스플레이",
                [{"task_id": 1, "title": "하나", "status": "PENDING"}], "fp", phase="PLAN",
            )
            with urllib.request.urlopen(srv.url + "api/pending", timeout=5) as r:
                entry = json.loads(r.read().decode("utf-8"))["requests"][0]
            self.assertEqual(entry["phase"], "PLAN")
            self.assertEqual(entry["tasks"][0]["title"], "하나")
        finally:
            srv.shutdown()

    def test_pending_does_not_invent_a_phase(self):
        """A missing phase must stay missing, so the page falls back to the old form."""
        srv = self.serve("ptp2", 8790)
        try:
            srv.store.publish("p", "g", "d", [{"task_id": 1}], "fp")
            record = json.loads(srv.store.path.read_text(encoding="utf-8"))
            for entry in record["requests"]:
                entry.pop("phase", None)
            srv.store.path.write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8")
            with urllib.request.urlopen(srv.url + "api/pending", timeout=5) as r:
                entry = json.loads(r.read().decode("utf-8"))["requests"][0]
            self.assertIsNone(entry["phase"])
        finally:
            srv.shutdown()

    def test_decide_carries_task_comments_and_scope(self):
        srv = self.serve("ptp3", 8791)
        try:
            rid = srv.store.publish("p", "g", "d", [{"task_id": 2}], "fp", phase="PLAN")
            body = json.dumps({
                "id": rid, "decision": "REVISE", "comment": "",
                "task_comments": {"2": "표를 그대로"}, "scope": "TASKS",
            }).encode()
            req = urllib.request.Request(srv.url + "api/decide", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertTrue(json.loads(r.read().decode())["ok"])
            self.assertEqual(
                srv.claim(rid), Verdict("REVISE", "", {"2": "표를 그대로"}, "TASKS")
            )
        finally:
            srv.shutdown()

    def test_pending_exposes_the_plan_summary(self):
        """It reaches the human through its own field now, not buried in `display`."""
        srv = self.serve("ptp5", 8793)
        try:
            srv.store.publish(
                "p", "목표", "디스플레이", [{"task_id": 1, "title": "하나"}], "fp",
                phase="PLAN", summary="보고서를 찾아 표를 뽑고 요약합니다.",
            )
            with urllib.request.urlopen(srv.url + "api/pending", timeout=5) as r:
                entry = json.loads(r.read().decode("utf-8"))["requests"][0]
            self.assertEqual(entry["summary"], "보고서를 찾아 표를 뽑고 요약합니다.")
        finally:
            srv.shutdown()

    def test_summary_is_empty_not_missing_for_old_entries(self):
        srv = self.serve("ptp6", 8794)
        try:
            srv.store.publish("p", "g", "d", [], "fp")
            record = json.loads(srv.store.path.read_text(encoding="utf-8"))
            for entry in record["requests"]:
                entry.pop("summary", None)
            srv.store.path.write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8")
            with urllib.request.urlopen(srv.url + "api/pending", timeout=5) as r:
                entry = json.loads(r.read().decode("utf-8"))["requests"][0]
            self.assertEqual(entry["summary"], "")
        finally:
            srv.shutdown()

    def test_page_ships_the_header_builder(self):
        srv = self.serve("ptp7", 8796)
        try:
            with urllib.request.urlopen(srv.url, timeout=5) as r:
                html = r.read().decode("utf-8")
            for fragment in ("function header(", "계획 승인 요청", "목표", "개요",
                             "class=\"summary\""):
                self.assertIn(fragment, html)
        finally:
            srv.shutdown()

    def test_page_ships_the_per_task_review_code(self):
        srv = self.serve("ptp4", 8792)
        try:
            with urllib.request.urlopen(srv.url, timeout=5) as r:
                html = r.read().decode("utf-8")
            for fragment in ("function taskRows(", "function relabel(", "function scopeOf(",
                             "task_comments:comments(id)", "계획 전체를 다시 세우기"):
                self.assertIn(fragment, html)
            # The escaper is used in attribute values now, so quotes must be covered.
            self.assertIn("&quot;", html)
        finally:
            srv.shutdown()


class TestCompletionPageTemplate(unittest.TestCase):
    """The completion report's per-task review, asserted against the template itself.

    No socket: the page is a constant, so these run wherever the suite runs, and they
    cover the branch that decides whether finished work can be sent back one task at a
    time at all. The click-through was verified in a real browser.
    """

    def test_a_completion_report_is_reviewed_task_by_task(self):
        self.assertIn("d.phase==='PLAN'||d.phase==='COMPLETION'", _PAGE)

    def test_the_phase_is_kept_so_the_two_can_be_labelled_differently(self):
        self.assertIn("const PHASE={}", _PAGE)
        self.assertIn("PHASE[d.id]=d.phase", _PAGE)

    def test_one_builder_labels_the_button_everywhere(self):
        """First render and every relabel must agree, or the button lies about scope."""
        self.assertIn("function revLabel(", _PAGE)
        self.assertIn("revLabel(PHASE[id],ids,whole,Object.keys(criteriaOf(id)).length)", _PAGE)

    def test_the_button_asks_for_rework_not_for_a_rewrite(self):
        self.assertIn("다시 작업 요청", _PAGE)

    def test_the_evidence_is_what_the_human_judges(self):
        self.assertIn("t.result_log", _PAGE)
        self.assertIn("증거 기록 없음", _PAGE)

    def test_a_row_sent_back_shows_the_request_and_the_old_output(self):
        """Both surfaces must agree. The request alone shows what was asked; only the
        previous output shows whether the new one answers it."""
        self.assertIn("요청하신 내용", _PAGE)
        self.assertIn("t.previous_result_log", _PAGE)
        self.assertIn("이전 결과", _PAGE)

    def test_the_whole_plan_escape_hatch_survives(self):
        self.assertIn("계획 자체를 다시 세우기", _PAGE)


class TestDraftSurvivalTemplate(unittest.TestCase):
    """Half-written comments must outlive a rebuild of the page.

    A card is redrawn whenever what it shows changes (until 3.1.0 every card was, on
    any change - a second session asking for approval was enough), and a reload or a
    second tab starts from nothing. Keeping drafts in the DOM meant losing them, so they
    live in localStorage instead. Asserted against the template; the click-through,
    including two tabs and a mid-typing rebuild, was verified in a real browser.
    """

    def test_drafts_are_stored_outside_the_dom(self):
        self.assertIn("planning-mcp:draft:", _PAGE)
        self.assertIn("localStorage.setItem", _PAGE)
        self.assertIn("function restore(", _PAGE)

    def test_an_emptied_box_stays_empty(self):
        """Removing the key instead of storing '' would resurrect the deleted text."""
        self.assertIn("localStorage.setItem(dkey(req,tid),value)", _PAGE)

    def test_every_tab_shows_the_same_draft(self):
        self.assertIn("window.addEventListener('storage',onStorage)", _PAGE)
        self.assertIn("function onStorage(", _PAGE)

    def test_listeners_are_bound_to_a_node_that_outlives_a_rebuild(self):
        self.assertIn("document.getElementById('root').addEventListener('input',onInput)", _PAGE)
        self.assertNotIn("oninput=", _PAGE)

    def test_drafts_are_cleared_once_submitted_and_pruned_when_stale(self):
        self.assertIn("dclear(id)", _PAGE)
        self.assertIn("function dprune(", _PAGE)

    def test_the_liveness_chip_replaces_a_countdown(self):
        """A countdown would measure the tool call, not the request - and the request
        outlives the call, so the number would be a lie that also creates false rush."""
        self.assertIn("d.agent_waiting", _PAGE)
        self.assertIn("에이전트가 대기 중입니다", _PAGE)
        self.assertIn("에이전트가 대기를 멈췄습니다", _PAGE)
        # No per-request timer, and no wording that implies a deadline.
        for banned in ("남은 시간", "초 남음", "초 후 만료", "deadline"):
            self.assertNotIn(banned, _PAGE)
        # The only recurring timers are the 1.5s poll and the 700ms title flash.
        self.assertEqual(_PAGE.count("setInterval("), 2)

    def test_the_page_still_promises_the_request_will_wait(self):
        self.assertIn("요청은 응답하실 때까지 사라지지 않으니", _PAGE)


class TestPlanStateEdges(HandlerTestCase):
    def test_task_list_of_blanks_is_refused(self):
        res = self.think(need_more_thinking=False, task_list=["", "   ", "\n"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "MISSING_TASK_LIST")

    def test_all_tasks_failed_keeps_the_plan_blocked(self):
        self.approve_flow(["a", "b"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "FAILED"})
        res = self.h.dispatch("update_task_progress", {"task_id": 2, "status": "FAILED"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_BLOCKED")

    def test_active_plan_id_pointing_at_a_deleted_plan(self):
        self.think(need_more_thinking=False, task_list=["a"])
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        raw["active_plan_id"] = "plan_ghost"
        (self.state_dir / "plan_state.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        res = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertContract(res)
        self.assertTrue(res["ok"])

    def test_task_id_zero_and_negative(self):
        self.approve_flow(["a"])
        for bad in (0, -1):
            res = self.h.dispatch("update_task_progress",
                                  {"task_id": bad, "status": "IN_PROGRESS"})
            self.assertFalse(res["ok"], f"task_id={bad} should not be accepted")
            self.assertEqual(res["error_code"], "TASK_NOT_FOUND")

    def test_every_multi_plan_path_keeps_the_contract(self):
        self.think(goal="A", need_more_thinking=False, task_list=["a"])
        self.think(goal="B", need_more_thinking=False, task_list=["b"])
        for name, args in [
            ("update_task_progress", {"task_id": 1, "status": "DONE"}),
            ("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"}),
            ("get_current_plan", {"plan_id": "current"}),
            ("get_current_plan", {"plan_id": "plan_nope"}),
        ]:
            self.assertContract(self.h.dispatch(name, args))


# ===========================================================================
# Protocol layer
# ===========================================================================


class TestProtocol(HandlerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.p = McpProtocol(self.h, TOOL_DEFINITIONS, "planning-mcp", "1.0.0")

    def test_initialize(self):
        res = self.p.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertIn("serverInfo", res["result"])
        self.assertIn("tools", res["result"]["capabilities"])

    def test_tools_list_exposes_exactly_four_tools(self):
        res = self.p.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = res["result"]["tools"]
        self.assertEqual(
            sorted(t["name"] for t in tools),
            ["get_current_plan", "plan_and_think", "request_user_approval", "update_task_progress"],
        )
        for tool in tools:
            self.assertTrue(tool["description"].strip())
            self.assertTrue(tool["inputSchema"]["required"])  # every tool needs >=1 required param

    def test_tools_call_returns_text_content(self):
        res = self.p.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_current_plan",
                    "arguments": {"plan_id": "current"},
                },
            }
        )
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(payload["next_action"], "CALL_PLAN_AND_THINK")
        self.assertFalse(res["result"]["isError"])

    def test_string_arguments_are_parsed(self):
        res = self.p.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_current_plan", "arguments": '{"plan_id": "current"}'},
            }
        )
        self.assertIn("content", res["result"])

    def test_failed_tool_call_is_not_a_protocol_error(self):
        res = self.p.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "update_task_progress",
                           "arguments": {"task_id": 1, "status": "DONE"}},
            }
        )
        self.assertNotIn("error", res)
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])

    def test_notification_gets_no_response(self):
        self.assertIsNone(
            self.p.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_unknown_method(self):
        res = self.p.handle_message({"jsonrpc": "2.0", "id": 6, "method": "nope/nope"})
        self.assertEqual(res["error"]["code"], -32601)

    # ---- malformed / hostile input -------------------------------------
    def test_tools_call_without_params(self):
        res = self.p.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertIn("next_action", payload)

    def test_arguments_of_the_wrong_shape(self):
        for bad in ([1, 2, 3], "plain text", 42, None, True):
            res = self.p.handle_message({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "get_current_plan", "arguments": bad}})
            self.assertIn("result", res, f"arguments={bad!r} produced no result")
            payload = json.loads(res["result"]["content"][0]["text"])
            for field in REQUIRED_FIELDS:
                self.assertIn(field, payload)

    def test_message_that_is_not_an_object(self):
        for bad in ("string", 5, None, True):
            res = self.p.handle_message(bad)
            self.assertEqual(res["error"]["code"], -32600)

    def test_batch_request_returns_a_batch(self):
        res = self.p.handle_message([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertIsInstance(res, list)
        self.assertEqual(sorted(r["id"] for r in res), [1, 2])  # the notification is silent

    def test_batch_of_only_notifications_is_silent(self):
        self.assertIsNone(self.p.handle_message([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]))

    def test_null_id_is_still_answered(self):
        res = self.p.handle_message({"jsonrpc": "2.0", "id": None, "method": "ping"})
        self.assertIsNotNone(res)
        self.assertIsNone(res["id"])

    def test_unknown_notification_is_ignored(self):
        self.assertIsNone(self.p.handle_message({"jsonrpc": "2.0", "method": "nope/nope"}))

    def test_unparseable_string_arguments(self):
        res = self.p.handle_message({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_current_plan", "arguments": "{not json"}})
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertIn("next_action", payload)

    def test_oversized_payload_is_survivable(self):
        huge = "가" * 200_000
        res = self.p.handle_message({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "plan_and_think", "arguments": {
                "goal": huge, "thought": huge, "step_number": 1, "total_steps": 1,
                "need_more_thinking": True}}})
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        # And the state file must still be readable afterwards.
        again = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertTrue(again["ok"])

    def test_progress_token_is_forwarded(self):
        seen = {}

        class Spy:
            def dispatch(self, name, args, progress_token=None, notifier=None, **kw):
                seen["token"] = progress_token
                seen["cancel_event"] = kw.get("cancel_event")
                return {"ok": True, "plan_status": "NONE", "next_action": "ANSWER_USER",
                        "next_action_hint": "-"}

        p = McpProtocol(Spy(), TOOL_DEFINITIONS, "x", "1")
        p.handle_message({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                          "params": {"name": "get_current_plan", "arguments": {},
                                     "_meta": {"progressToken": "tok-9"}}})
        self.assertEqual(seen["token"], "tok-9")
        # Handed down with the token so a blocking wait can notice the client giving up.
        self.assertIsNotNone(seen["cancel_event"])


class TestFileLockEdges(HandlerTestCase):
    def test_lock_is_exclusive_and_released(self):
        path = self.state_dir / "l1"
        with exclusive(path) as got:
            self.assertTrue(got)
        with exclusive(path) as got:  # must be reusable after release
            self.assertTrue(got)

    def test_timeout_yields_false_rather_than_raising(self):
        """A contended lock must degrade loudly, never wedge the user's call."""
        path = self.state_dir / "l2"
        holder_in = threading.Event()
        release = threading.Event()

        def holder():
            with exclusive(path):
                holder_in.set()
                release.wait(20)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            self.assertTrue(holder_in.wait(5))
            started = time.monotonic()
            with exclusive(path, timeout=0.5) as got:
                self.assertFalse(got, "should report that it gave up")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 3, "must honour its own timeout, not the OS default")
        finally:
            release.set()
            t.join(timeout=10)

    def test_contention_clears_quickly_once_released(self):
        """A brief holder must not stall the next caller for seconds."""
        path = self.state_dir / "l3"
        holder_in = threading.Event()

        def holder():
            with exclusive(path):
                holder_in.set()
                time.sleep(0.3)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            self.assertTrue(holder_in.wait(5))
            started = time.monotonic()
            with exclusive(path, timeout=10) as got:
                elapsed = time.monotonic() - started
                self.assertTrue(got)
            self.assertLess(elapsed, 2.0, f"waited {elapsed:.1f}s for a 0.3s holder")
        finally:
            t.join(timeout=10)

    def test_unwritable_lock_path_degrades(self):
        missing = self.state_dir / "no" / "such" / "dir" / "lock"
        with exclusive(missing) as got:
            self.assertFalse(got)


class TestAutoAdvance(HandlerTestCase):
    """Reporting a task DONE also starts the next one.

    The separate IN_PROGRESS call was a round trip, not a safeguard: the safeguard is
    that DONE is refused unless the task is the one in progress AND carries evidence.
    These tests exist to prove the round trip went away and the safeguard did not.
    """

    def approved(self, n=5):
        return self.approve_flow([f"작업{i}" for i in range(1, n + 1)])

    def statuses(self):
        return [t.status for t in self.h.store.load().active_plan.tasks]

    def finish(self, task_id):
        return self.h.dispatch("update_task_progress", {
            "task_id": task_id, "status": "DONE",
            "result_log": f"작업{task_id} 결과를 /tmp/out{task_id}.txt 에 저장함"})

    def test_done_starts_the_next_task(self):
        self.approved(3)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.finish(1)
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(self.statuses(), ["DONE", "IN_PROGRESS", "PENDING"])
        self.assertEqual(res["next_task"]["task_id"], 2)
        self.assertIn("task_auto_started", (self.state_dir / "audit.jsonl").read_text("utf-8"))

    def test_five_tasks_take_six_calls(self):
        """The whole point of the change: 10 execution round trips become 6."""
        self.approved(5)
        calls = [{"task_id": 1, "status": "IN_PROGRESS"}]
        calls += [
            {"task_id": i, "status": "DONE", "result_log": f"결과 {i} 를 파일로 저장함"}
            for i in range(1, 6)
        ]
        for args in calls:
            res = self.h.dispatch("update_task_progress", args)
            self.assertTrue(res["ok"], f"{args} -> {res.get('message')}")
        self.assertEqual(len(calls), 6)
        self.assertEqual(self.statuses(), ["DONE"] * 5)
        self.assertEqual(
            self.h.store.load().active_plan.plan_status, "AWAITING_COMPLETION"
        )

    def test_bare_done_at_every_task_is_still_refused(self):
        """Gemini's batch_update, attempted through the back door. Must not work."""
        self.approved(4)
        first = self.h.dispatch("update_task_progress", {
            "task_id": 1, "status": "DONE", "result_log": "1번 결과를 저장함"})
        self.assertFalse(first["ok"])
        self.assertEqual(first["error_code"], "TASK_NOT_STARTED")
        for tid in (2, 3, 4):
            res = self.h.dispatch("update_task_progress", {
                "task_id": tid, "status": "DONE", "result_log": f"{tid}번 결과를 저장함"})
            self.assertFalse(res["ok"], f"task {tid} accepted a DONE it never started")
        self.assertEqual(self.statuses(), ["PENDING"] * 4)

    def test_advanced_task_still_needs_its_own_evidence(self):
        """Auto-advance hands out IN_PROGRESS, never a free DONE."""
        self.approved(2)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.finish(1)
        res = self.h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": "완료"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "MISSING_RESULT_LOG")

    def test_cannot_jump_past_the_advanced_task(self):
        self.approved(3)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.finish(1)  # task 2 is now IN_PROGRESS, task 3 is PENDING
        res = self.finish(3)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "TASK_NOT_STARTED")

    def test_redundant_in_progress_is_idempotent(self):
        """A model that sends IN_PROGRESS out of habit must not rewind the clock."""
        self.approved(2)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.finish(1)
        started = self.h.store.load().active_plan.get_task(2).started_at
        res = self.h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        self.assertTrue(res["ok"])
        self.assertIn("already IN_PROGRESS", res["message"])
        self.assertEqual(self.h.store.load().active_plan.get_task(2).started_at, started)

    def test_last_task_advances_into_nothing(self):
        self.approved(2)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.finish(2 - 1)  # task 1
        self.assertEqual(res["next_task"]["task_id"], 2)
        res = self.finish(2)
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertIsNone(res.get("next_task"))

    def test_last_done_tells_the_model_to_report_completion(self):
        """Nothing is auto-started after the final task, so message said nothing at all
        - at the one step where a weak model is most likely to invent its own ending
        (1.12.1)."""
        self.legacy()
        self.approved(2)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.finish(1)
        res = self.finish(2)
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")
        self.assertIn("every task in this plan is finished", res["message"])
        self.assertIn("request_user_approval", res["message"])

    def test_last_done_without_completion_approval_asks_for_the_final_answer(self):
        cfg = Config(state_dir=self.state_dir, blocking_approval=False,
                     completion_approval=False)
        self.h = PlanningHandlers(Store(self.state_dir), cfg)
        self.approved(1)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.finish(1)
        self.assertEqual(res["plan_status"], "COMPLETED")
        self.assertIn("every task in this plan is finished", res["message"])
        self.assertIn("final answer", res["message"])

    def test_manual_mode_done_names_the_next_task_not_completion(self):
        """advanced is None with work left must never read as 'report completion'."""
        cfg = Config(state_dir=self.state_dir, blocking_approval=False, auto_advance=False)
        self.h = PlanningHandlers(Store(self.state_dir), cfg)
        self.approved(2)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.finish(1)
        self.assertIn("NOT finished", res["message"])
        self.assertIn("IN_PROGRESS", res["message"])
        self.assertNotIn("finished. Report completion", res["message"])

    def test_failure_does_not_advance(self):
        """A blocked plan must not have its next task quietly started."""
        self.approved(3)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.h.dispatch("update_task_progress", {
            "task_id": 1, "status": "FAILED", "result_log": "파일을 찾지 못함"})
        self.assertEqual(self.statuses(), ["FAILED", "PENDING", "PENDING"])

    def test_can_be_disabled(self):
        cfg = Config(state_dir=self.state_dir, blocking_approval=False, auto_advance=False)
        self.h = PlanningHandlers(Store(self.state_dir), cfg)
        self.approved(2)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.finish(1)
        self.assertEqual(self.statuses(), ["DONE", "PENDING"])

    def test_the_description_matches_the_mode(self):
        """A tool description that contradicts the server is worse than a long one."""
        auto = build_tool_definitions(auto_advance=True)[2]["description"]
        manual = build_tool_definitions(auto_advance=False)[2]["description"]
        self.assertIn("You do NOT send \"IN_PROGRESS\" again", auto)
        self.assertIn("Call this tool TWICE for every task", manual)


class TestUnmatchedTaskUpdates(TargetedRevisionFixture):
    """task_updates sent as bare text, with no task_id.

    The model was asked to rewrite one task, so it sends one rewritten task. Pairing that
    with the single flagged id is a reading; pairing it with one of several is a guess,
    and a wrong guess rewrites a task the human accepted.
    """

    def test_leniency_separates_titles_that_have_no_id(self):
        clean, _ = normalize("plan_and_think", {"task_updates": ["아이디 없는 제목"]})
        self.assertNotIn("task_updates", clean)
        self.assertEqual(clean[UNMATCHED_TITLES_KEY], ["아이디 없는 제목"])

    def test_leniency_keeps_good_entries_and_sets_the_rest_aside(self):
        clean, _ = normalize("plan_and_think", {"task_updates": [
            {"task_id": 2, "title": "제대로 된 항목"},
            "아이디 없음",
        ]})
        self.assertEqual(clean["task_updates"], [{"task_id": 2, "title": "제대로 된 항목"}])
        self.assertEqual(clean[UNMATCHED_TITLES_KEY], ["아이디 없음"])

    def test_single_target_pairs_a_bare_title(self):
        h, _ = self.flagged({"2": "표를 그대로 넣어주세요"})
        res = self.update(h, ["표를 원본 그대로 삽입"])
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(res["revised_tasks"], [2])
        self.assertEqual(
            [t.title for t in self.plan(h).tasks],
            ["보고서 찾기", "표를 원본 그대로 삽입", "요약 작성"],
        )
        self.assertTrue(any("no task_id" in n for n in res["input_notes"]))

    def test_single_target_pairs_a_bare_string(self):
        """Not even a list - just the sentence."""
        h, _ = self.flagged({"3": "더 짧게"})
        res = self.update(h, "요약을 3줄로 작성")
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(self.plan(h).get_task(3).title, "요약을 3줄로 작성")

    def test_several_targets_are_never_guessed(self):
        h, _ = self.flagged({"1": "다르게", "3": "더 짧게"})
        res = self.update(h, ["첫 번째 수정본", "세 번째 수정본"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "REVISION_INCOMPLETE")
        self.assertEqual([t.title for t in self.plan(h).tasks], self.TASKS)
        self.assertTrue(any("no task_id" in n for n in res["input_notes"]))

    def test_a_bare_title_with_no_pending_revision_changes_nothing(self):
        h = self.blocking(FakeApprovalUI(decision=None))
        self.draft(h)
        res = self.update(h, ["아무도 요청하지 않은 수정"])
        # Since 1.16 the finalized plan is not reopened by plan_and_think at all.
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        self.assertEqual([t.title for t in self.plan(h).tasks], self.TASKS)


class TestLeanExecutionResponse(HandlerTestCase):
    """update_task_progress publishes one task id, in one field, and no task list.

    The old shape re-sent the whole plan on every execution call, so a five-task run
    left six copies of the task list in the conversation - five of them describing a
    state that no longer existed. Worse, each carried a sentence of the form "call
    update_task_progress with task_id=N", which is an instruction, not data: a model
    reading back through its own history found four wrong ones and one right one, with
    nothing to distinguish them.
    """

    TASKS = ["Find the file", "Read it", "Write the summary"]

    def execution_responses(self):
        """Every response the model gets while executing a plan end to end."""
        self.approve_flow(self.TASKS)
        out = []
        for task_id in range(1, len(self.TASKS) + 1):
            out.append(self.h.dispatch(
                "update_task_progress", {"task_id": task_id, "status": "IN_PROGRESS"}))
            out.append(self.h.dispatch("update_task_progress", {
                "task_id": task_id, "status": "DONE",
                "result_log": f"saved output of task {task_id} to /tmp/out{task_id}.txt"}))
        return out

    def test_no_execution_response_carries_the_task_list(self):
        for res in self.execution_responses():
            self.assertNotIn("tasks", res)

    def test_progress_replaces_it(self):
        """The count is what the list was actually being read for."""
        self.approve_flow(self.TASKS)
        res = self.do_task(1)
        self.assertEqual(res["progress"], "1/3 done")
        self.assertIn("2 of 3 tasks are still unfinished", res["next_action_hint"])

    def test_next_task_is_the_only_id_anywhere_in_the_response(self):
        """The guarantee that makes a stale copy inert: nothing else names a task."""
        for res in self.execution_responses():
            published = dict(res)
            published.pop("next_task", None)
            blob = json.dumps(published, ensure_ascii=False)
            for task_id in range(1, len(self.TASKS) + 1):
                self.assertNotIn(f"task_id={task_id}", blob)

    def test_the_hint_never_dangles(self):
        """Every hint that says 'use next_task' must ship a next_task to use."""
        for res in self.execution_responses():
            if "next_task" in res["next_action_hint"]:
                self.assertIn("next_task", res, res["next_action_hint"])

    def test_next_task_is_present_whenever_the_next_action_is_an_update(self):
        for res in self.execution_responses():
            if res["next_action"] == "CALL_UPDATE_TASK_PROGRESS":
                self.assertIn("next_task", res)
                self.assertIn("task_id", res["next_task"])

    def test_next_task_names_the_task_the_server_will_actually_accept(self):
        """A model that copies next_task.task_id must never be refused for it."""
        self.approve_flow(self.TASKS)
        res = self.h.dispatch(
            "update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        for _ in range(len(self.TASKS)):
            task_id = res["next_task"]["task_id"]
            res = self.h.dispatch("update_task_progress", {
                "task_id": task_id, "status": "DONE",
                "result_log": f"produced output {task_id} and stored it at /tmp/o{task_id}"})
            self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(res["plan_status"], "AWAITING_COMPLETION")

    def test_the_recovery_tool_still_returns_the_whole_plan(self):
        """Dropping the echo is only safe because resync stayed one call away."""
        self.approve_flow(self.TASKS)
        self.do_task(1)
        res = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(len(res["tasks"]), 3)
        self.assertEqual(res["next_task"]["task_id"], 2)

    def test_a_refusal_also_stays_lean_and_still_points_somewhere(self):
        self.approve_flow(self.TASKS)
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.h.dispatch(
            "update_task_progress", {"task_id": 1, "status": "DONE", "result_log": "ok"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "MISSING_RESULT_LOG")
        self.assertNotIn("tasks", res)
        self.assertEqual(res["next_task"]["task_id"], 1)

    def test_it_is_materially_smaller(self):
        """The reason for the change, asserted rather than assumed."""
        self.approve_flow(self.TASKS)
        res = self.do_task(1, log="찾은 파일을 /tmp/out1.txt 에 저장했습니다. " * 8)
        # Measured on the wire: protocol.py serializes with indent=2 and sends that.
        lean = len(json.dumps(res, ensure_ascii=False, indent=2).encode())
        fat = len(json.dumps(
            {**res, "tasks": self.store.load().active_plan.tasks_brief()},
            ensure_ascii=False, indent=2).encode())
        # The echoed list was the majority of the payload, and it grew with every task
        # while this half stayed flat.
        self.assertLess(lean * 100, fat * 55)


class TestLeanResponseKeepsTheReworkGuard(TargetedRevisionFixture):
    """D19 depends on the model seeing what it produced last time.

    That evidence used to arrive inside the echoed task list. With the list gone it
    rides in next_task instead - the one part of the old echo that earns its place,
    because a model asked to redo work without it will resubmit the rejected outcome
    and be refused for a reason it cannot see.
    """

    def sent_back_one(self):
        """Run a plan to the completion report, then have the human reopen task 2."""
        ui = FakeApprovalUI("APPROVED")
        h, _ = self.reported(ui)
        ui.decision, ui.comment = "REVISE", ""
        ui.scope, ui.task_comments = "TASKS", {"2": "표가 3개 빠졌어요"}
        res = h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "완료 보고"}
        )
        return h, res

    def test_next_task_carries_the_users_request(self):
        _, res = self.sent_back_one()
        self.assertEqual(res["next_task"]["revision_note"], "표가 3개 빠졌어요")

    def test_next_task_carries_what_was_produced_before(self):
        _, res = self.sent_back_one()
        self.assertIn("/tmp/out2.txt", res["next_task"]["previous_result_log"])

    def test_it_survives_into_the_next_execution_response(self):
        """The model reads the freshest response, not the one three turns back."""
        h, _ = self.sent_back_one()
        res = h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        self.assertEqual(res["next_task"]["revision_note"], "표가 3개 빠졌어요")
        self.assertIn("/tmp/out2.txt", res["next_task"]["previous_result_log"])

    def test_resubmitting_the_old_outcome_is_still_refused(self):
        h, _ = self.sent_back_one()
        h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        old = self.plan(h).get_task(2).previous_result_log
        res = h.dispatch("update_task_progress", {
            "task_id": 2, "status": "DONE", "result_log": old})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "REWORK_NOT_DONE")
        self.assertEqual(res["next_task"]["task_id"], 2)


class TestCallBudgetEnv(HandlerTestCase):
    """`PLANNING_MCP_CALL_BUDGET` - the per-call ceiling, end to end.

    It is the one knob a deployer has to reach for when a client turns out to be less
    patient than the 60s default. If it silently fails to take effect, the symptom is
    not a config warning, it is a conversation dying mid-approval - so this covers
    parsing, the fallbacks, and that the parsed value actually reaches the wait.
    """

    def env_config(self, **overrides):
        """Config.from_env() with a scrubbed environment, so a real PLANNING_MCP_*
        setting on the developer's machine cannot decide the outcome."""
        with mock.patch.dict(os.environ, overrides, clear=True):
            return Config.from_env(state_dir_override=str(self.state_dir))

    def handlers(self, ui=None, **env):
        cfg = self.env_config(**env)
        return PlanningHandlers(
            Store(self.state_dir), cfg, approval_ui=ui or FakeApprovalUI(decision=None)
        )

    # ---- parsing ----------------------------------------------------------
    def test_unset_falls_back_to_the_default(self):
        self.assertEqual(self.env_config().call_budget, CALL_BUDGET_SEC)

    def test_value_from_env_is_used(self):
        self.assertEqual(self.env_config(PLANNING_MCP_CALL_BUDGET="20").call_budget, 20)

    def test_whitespace_padded_value_is_accepted(self):
        self.assertEqual(self.env_config(PLANNING_MCP_CALL_BUDGET=" 30 ").call_budget, 30)

    def test_garbage_falls_back_to_the_default(self):
        """A typo must not disarm the cap - falling back beats crashing at startup."""
        for junk in ("abc", "30s", "45.5", "", "  "):
            with self.subTest(raw=junk):
                self.assertEqual(
                    self.env_config(PLANNING_MCP_CALL_BUDGET=junk).call_budget,
                    CALL_BUDGET_SEC,
                )

    def test_default_stays_under_the_client_request_timeout(self):
        self.assertLess(CALL_BUDGET_SEC, SDK_REQUEST_TIMEOUT_SEC)

    # ---- it reaches the wait ----------------------------------------------
    def test_env_budget_becomes_the_slice_length(self):
        h = self.handlers(PLANNING_MCP_CALL_BUDGET="12", PLANNING_MCP_APPROVAL_TIMEOUT="900")
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 12)
        self.assertEqual(h.effective_timeout(can_heartbeat=True), 12,
                         "a progressToken must not buy a longer single call")

    def test_a_bigger_budget_still_cannot_outlive_the_total_wait(self):
        h = self.handlers(PLANNING_MCP_CALL_BUDGET="120", PLANNING_MCP_APPROVAL_TIMEOUT="30")
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 30)

    def test_the_slice_shrinks_with_the_remaining_total(self):
        h = self.handlers(PLANNING_MCP_CALL_BUDGET="20", PLANNING_MCP_APPROVAL_TIMEOUT="60")
        self.assertEqual(h.effective_timeout(can_heartbeat=False, already_waited=50), 10)
        self.assertEqual(h.effective_timeout(can_heartbeat=False, already_waited=60), 0)

    def test_nonsensical_budgets_still_wait_at_least_a_second(self):
        """0 or negative would otherwise skip the wait, which is indistinguishable
        from having no gate at all."""
        for raw in ("0", "-5"):
            with self.subTest(raw=raw):
                h = self.handlers(PLANNING_MCP_CALL_BUDGET=raw,
                                  PLANNING_MCP_APPROVAL_TIMEOUT="900")
                self.assertEqual(h.effective_timeout(can_heartbeat=False), 1)

    def test_a_measured_client_cap_beats_a_larger_env_budget(self):
        h = self.handlers(PLANNING_MCP_CALL_BUDGET="45", PLANNING_MCP_APPROVAL_TIMEOUT="900")
        h.store.record_call_cap(30)  # this client was seen giving up at 30s
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 25)

    def test_a_smaller_env_budget_beats_the_measured_cap(self):
        h = self.handlers(PLANNING_MCP_CALL_BUDGET="10", PLANNING_MCP_APPROVAL_TIMEOUT="900")
        h.store.record_call_cap(30)
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 10)

    def test_other_modes_ignore_the_budget(self):
        h = self.handlers(PLANNING_MCP_CALL_BUDGET="12",
                          PLANNING_MCP_APPROVAL_TIMEOUT="900",
                          PLANNING_MCP_APPROVAL_MODE="trust_heartbeat")
        self.assertEqual(h.effective_timeout(can_heartbeat=True), 900)
        h = self.handlers(PLANNING_MCP_CALL_BUDGET="12",
                          PLANNING_MCP_APPROVAL_MODE="return")
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 0)

    # ---- the real wait honours it -----------------------------------------
    def test_a_real_wait_ends_within_the_env_budget(self):
        """The end-to-end claim: set the var, and one tools/call actually returns
        inside it instead of holding the client open."""
        ui = FakeApprovalUI(decision=None)
        h = self.handlers(ui, PLANNING_MCP_CALL_BUDGET="1",
                          PLANNING_MCP_APPROVAL_TIMEOUT="900")
        h.dispatch("plan_and_think", {
            "goal": "예산 검증", "thought": "t", "step_number": 1, "total_steps": 1,
            "need_more_thinking": False, "task_list": ["작업 1"]})
        started = time.monotonic()
        res = h.dispatch("request_user_approval",
                         {"decision": "ASK_USER", "plan_summary": "요약"})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10, "the 1s budget did not bound the call")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertLessEqual(res["waited_seconds"], 5)
        self.assertGreater(res["remaining_seconds"], 0,
                           "the human's total budget must survive the slice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
