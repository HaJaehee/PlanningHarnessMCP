"""2.0.0 - per-task alternatives: the model proposes, the human picks.

The model's task_list is its recommendation; `alternatives` offers other ways to do a
task and `recommended_reasons` says why it prefers its own. The approval page shows
them as a choice with the recommendation pre-selected, and whatever the human picks
becomes the task. Plan: docs/plan-2.0-task-alternatives.md.

The guarantees pinned here:
- what was picked is what runs, and only the human picks while a page is open;
- after approval the model never sees an option that was not picked (D19);
- a plan with no choices behaves - and fingerprints - exactly as in 1.16.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planning.approval import _PAGE, PHASE_COMPLETION, PHASE_HALT, PHASE_PLAN, ApprovalStore  # noqa: E402
from planning.choices import (  # noqa: E402
    build_options,
    letter,
    validate_model_choices,
    validate_page_choices,
)
from planning.config import Config  # noqa: E402
from planning.handlers import PlanningHandlers  # noqa: E402
from planning.leniency import normalize  # noqa: E402
from planning.models import Plan, Task  # noqa: E402
from planning.responses import render_completion_report, render_plan_for_user  # noqa: E402
from planning.schemas import build_tool_definitions  # noqa: E402
from planning.store import Store  # noqa: E402
from test_server import FakeApprovalUI  # noqa: E402

GOAL = "Q3 매출 보고서를 요약해 팀장에게 보낸다"
TASKS = ["보고서 파일 찾기", "엑셀 피벗으로 집계", "5줄 요약 작성"]
ALT = "CSV로 내보낸 뒤 스크립트로 집계"
ALT2 = "수작업으로 합계 계산"
ALTS = [{"task_id": 2, "title": ALT, "reason": "빠르지만 서식이 사라짐"}]
REASONS = [{"task_id": 2, "reason": "보고서 서식이 그대로 유지됨"}]
EVIDENCE = "결과를 out/summary.txt 에 저장함"


class AltCase(unittest.TestCase):
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
    def think(h, step=1, more=False, tasks=None, alts=ALTS, reasons=REASONS, **kw):
        args = {"goal": GOAL, "thought": "t", "step_number": step, "total_steps": step + 1,
                "need_more_thinking": more}
        if tasks is not False:
            args["task_list"] = list(tasks or TASKS)
        if alts is not None:
            args["alternatives"] = alts
        if reasons is not None:
            args["recommended_reasons"] = reasons
        args.update(kw)
        return h.dispatch("plan_and_think", args)

    def plan(self, h) -> Plan:
        state = h.store.load()
        return state.active_plan or max(state.plans.values(), key=lambda p: p.updated_at)

    def ask(self, h):
        return h.dispatch("request_user_approval",
                          {"decision": "ASK_USER", "plan_summary": "보고서를 요약합니다."})

    def audit(self, event) -> list[dict]:
        path = self.state_dir / "audit.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if json.loads(l).get("event") == event]


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


class TestLeniency(unittest.TestCase):
    def alts(self, value):
        return normalize("plan_and_think", {"goal": "g", "alternatives": value})[0].get(
            "alternatives")

    def test_the_canonical_shape(self):
        self.assertEqual(self.alts(ALTS), ALTS)

    def test_lines_with_a_task_number(self):
        self.assertEqual(
            self.alts("2: CSV로 내보내기\ntask 3 - 생략"),
            [{"task_id": 2, "title": "CSV로 내보내기", "reason": ""},
             {"task_id": 3, "title": "생략", "reason": ""}],
        )

    def test_a_map_of_task_to_options(self):
        out = self.alts({"2": ["CSV", "JSON"], "3": {"option": "x", "why": "y"}})
        self.assertEqual([(a["task_id"], a["title"], a["reason"]) for a in out],
                         [(2, "CSV", ""), (2, "JSON", ""), (3, "x", "y")])

    def test_digits_in_the_text_never_become_the_task_id(self):
        """'Q3 report' must not attach itself to task 3."""
        self.assertIsNone(self.alts([{"task": "Q3 report export"}]))
        self.assertEqual(self.alts([{"task": "Q3 report export", "id": 2}])[0]["task_id"], 2)

    def test_unreadable_alternatives_are_dropped_with_a_note(self):
        clean, notes = normalize("plan_and_think", {"goal": "g", "alternatives": 42})
        self.assertNotIn("alternatives", clean)
        self.assertTrue(any("alternatives" in n for n in notes))

    def test_reasons_in_every_shape(self):
        for raw in ({"2": "서식 유지"}, [{"task_id": "2", "reason": "서식 유지"}], "2: 서식 유지"):
            clean, _ = normalize("plan_and_think", {"goal": "g", "recommended_reasons": raw})
            self.assertEqual(clean["recommended_reasons"], {2: "서식 유지"}, raw)

    def test_choices_are_letters(self):
        for raw, want in (({"2": "B"}, {"2": 1}), ({"2": "b안"}, {"2": 1}),
                          ({"2": "권장"}, {"2": 0}), ({"2": "option C"}, {"2": 2}),
                          ([{"task_id": 2, "choice": "A"}], {"2": 0})):
            clean, _ = normalize("request_user_approval", {"decision": "APPROVED",
                                                           "choices": raw})
            self.assertEqual(clean["choices"], want, raw)

    def test_a_bare_number_is_not_guessed(self):
        """'1' could mean option A or index 1 (B); the server does not guess a pick."""
        clean, notes = normalize("request_user_approval",
                                 {"decision": "APPROVED", "choices": {"2": 1}})
        self.assertNotIn("choices", clean)
        self.assertTrue(any("letters" in n for n in notes))


class TestBuildOptions(unittest.TestCase):
    def test_the_recommendation_is_option_a(self):
        opts, notes = build_options(TASKS, ALTS, {2: "서식 유지"}, 3, 3)
        self.assertEqual(opts[2][0], {"title": TASKS[1], "reason": "서식 유지"})
        self.assertEqual(opts[2][1]["title"], ALT)
        self.assertEqual(notes, [])

    def test_bad_entries_are_dropped(self):
        opts, notes = build_options(TASKS, [
            {"task_id": 9, "title": "범위 밖"},
            {"task_id": 2, "title": TASKS[1] + "."},   # the recommendation again
            {"task_id": 2, "title": ALT}, {"task_id": 2, "title": ALT},  # duplicate
        ], {}, 3, 3)
        self.assertEqual([o["title"] for o in opts[2]], [TASKS[1], ALT])
        self.assertTrue(any("task_id must be" in n for n in notes))

    def test_limits(self):
        alts = [{"task_id": 1, "title": f"a{i}"} for i in range(5)]
        alts += [{"task_id": t, "title": "x"} for t in (2, 3)]
        opts, notes = build_options(TASKS, alts, {}, 3, 2)
        self.assertEqual(len(opts[1]), 4)          # recommendation + 3
        self.assertEqual(sorted(opts), [1, 2])     # only 2 choice points
        self.assertEqual(len(notes), 2)

    def test_done_tasks_offer_no_choice(self):
        opts, notes = build_options(TASKS, ALTS, {}, 3, 3, locked={2})
        self.assertEqual(opts, {})
        self.assertTrue(any("already done" in n for n in notes))

    def test_a_reason_with_nothing_to_choose_is_dropped(self):
        opts, notes = build_options(TASKS, [], {1: "이유"}, 3, 3)
        self.assertEqual(opts, {})
        self.assertTrue(any("nothing to recommend" in n for n in notes))

    def test_letters(self):
        self.assertEqual([letter(i) for i in range(4)], ["A", "B", "C", "D"])


class TestChoiceValidation(unittest.TestCase):
    TASKS_ON_PAGE = [
        {"task_id": 1, "title": "a"},
        {"task_id": 2, "title": "b", "options": [{"title": "b"}, {"title": "c"}]},
    ]

    def test_the_page_may_only_pick_what_it_showed(self):
        ok = validate_page_choices(self.TASKS_ON_PAGE, {"2": 1})
        self.assertEqual(ok, {"2": 1})
        self.assertEqual(validate_page_choices(self.TASKS_ON_PAGE, None), {})
        for bad in ({"1": 0}, {"2": 2}, {"2": -1}, {"2": True}, {"2": "1"}, {"x": 0}, [1]):
            self.assertIsNone(validate_page_choices(self.TASKS_ON_PAGE, bad), bad)

    def test_the_model_relay_falls_back_to_the_recommendation(self):
        opts = {2: [{"title": "b"}, {"title": "c"}]}
        out, notes = validate_model_choices(opts, {"2": 1, "1": 0})
        self.assertEqual(out, {2: 1})
        out, notes = validate_model_choices(opts, {"2": 3})
        self.assertEqual(out, {})
        self.assertTrue(any("(A) was kept" in n for n in notes))


# ---------------------------------------------------------------------------
# The plan carries options; the human picks; the pick runs
# ---------------------------------------------------------------------------


class TestProposing(AltCase):
    def test_finalize_attaches_options(self):
        h = self.handler()
        res = self.think(h)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["choice_points"], [2])
        self.assertIn("offer a choice", res["message"])
        task = self.plan(h).tasks[1]
        self.assertEqual([o["title"] for o in task.options], [TASKS[1], ALT])
        self.assertEqual(task.options[0]["reason"], "보고서 서식이 그대로 유지됨")
        self.assertIsNone(task.chosen)

    def test_the_model_view_never_lists_options(self):
        h = self.handler()
        res = self.think(h)
        self.assertNotIn(ALT, json.dumps(res, ensure_ascii=False))

    def test_no_alternatives_is_exactly_1_16(self):
        h = self.handler()
        res = self.think(h, alts=None, reasons=None)
        self.assertNotIn("choice_points", res)
        self.assertTrue(all(t.options is None for t in self.plan(h).tasks))

    def test_the_fingerprint_of_a_plan_without_choices_is_unchanged(self):
        """A request left on the page across a rolling upgrade must still match."""
        h = self.handler()
        self.think(h, alts=None, reasons=None)
        plan = self.plan(h)
        payload = "\x00".join(
            [plan.goal]
            + [f"{t.task_id}|{t.title}|{t.status}|{(t.result_log or '')}" for t in plan.tasks]
        )
        old = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(h._fingerprint(plan), old)

    def test_the_fingerprint_binds_the_options(self):
        h = self.handler()
        self.think(h)
        plan = self.plan(h)
        before = h._fingerprint(plan)
        plan.tasks[1].options[1]["title"] = "다른 대안"
        self.assertNotEqual(h._fingerprint(plan), before)

    def test_turned_off(self):
        h = self.handler(alternatives=False)
        res = self.think(h)
        self.assertTrue(any("turned off" in n for n in res["input_notes"]))
        self.assertTrue(all(t.options is None for t in self.plan(h).tasks))

    def test_rewriting_a_task_drops_its_choice(self):
        h = self.handler()
        self.think(h)
        plan_id = self.plan(h).plan_id
        self.ask(h)
        h.dispatch("request_user_approval", {"decision": "REVISE", "user_comment": "전체 수정"})
        # A targeted revision of task 2 (as the page would set it up).
        state = h.store.load()
        state.plans[plan_id].pending_revision = {"targets": {"2": "더 간단히"}}
        h.store.save(state)
        res = h.dispatch("plan_and_think", {
            "goal": GOAL, "thought": "t", "step_number": 2, "total_steps": 2,
            "need_more_thinking": False, "task_updates": [{"task_id": 2, "title": "간단 집계"}],
            "alternatives": ALTS})
        self.assertTrue(res["ok"], res)
        self.assertTrue(any("full task_list" in n for n in res["input_notes"]))
        self.assertIsNone(self.plan(h).tasks[1].options)

    def test_done_work_carried_through_a_redraft_offers_no_choice(self):
        h = self.handler()
        self.think(h, alts=None, reasons=None)
        self.ask(h)
        h.dispatch("request_user_approval", {"decision": "APPROVED"})
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        for tid in (1, 2, 3):
            h.dispatch("update_task_progress", {"task_id": tid, "status": "DONE",
                                                "result_log": f"{EVIDENCE} {tid}"})
        self.ask(h)
        h.dispatch("request_user_approval", {"decision": "REVISE", "user_comment": "단계 추가"})
        res = self.think(h, step=2, tasks=TASKS + ["메일 발송"],
                         alts=[{"task_id": 2, "title": ALT}, {"task_id": 4, "title": "메신저"}],
                         reasons=None)
        plan = self.plan(h)
        self.assertIsNone(plan.tasks[1].options)       # done: nothing left to choose
        self.assertTrue(plan.tasks[3].has_choice)
        self.assertTrue(any("already done" in n for n in res["input_notes"]))


class TestChatModePicking(AltCase):
    """No approval page: the user answers in chat and the model relays the pick."""

    def approve(self, h, **extra):
        self.think(h)
        self.ask(h)
        return h.dispatch("request_user_approval", {"decision": "APPROVED", **extra})

    def test_the_plan_shown_in_chat_lists_the_options(self):
        h = self.handler()
        self.think(h)
        display = self.ask(h)["display_to_user"]
        self.assertIn(f"A. {TASKS[1]} [권장] — 보고서 서식이 그대로 유지됨", display)
        self.assertIn(f"B. {ALT} — 빠르지만 서식이 사라짐", display)
        self.assertIn("'2번 B안'처럼", display)

    def test_a_relayed_pick_runs(self):
        h = self.handler()
        res = self.approve(h, choices={"2": "B"})
        self.assertEqual(res["plan_status"], "APPROVED")
        task = self.plan(h).tasks[1]
        self.assertEqual((task.title, task.chosen), (ALT, 1))
        self.assertIn(f"task 2: '{ALT}'", res["message"])
        self.assertEqual(self.audit("choices_applied")[0]["chosen"], {"2": "B"})

    def test_no_pick_means_the_recommendation(self):
        h = self.handler()
        res = self.approve(h)
        task = self.plan(h).tasks[1]
        self.assertEqual((task.title, task.chosen), (TASKS[1], 0))
        self.assertIn("task 2: your recommendation", res["message"])

    def test_a_pick_that_does_not_exist_keeps_the_recommendation(self):
        h = self.handler()
        res = self.approve(h, choices={"2": "D"})
        self.assertEqual(self.plan(h).tasks[1].chosen, 0)
        self.assertTrue(any("(A) was kept" in n for n in res["input_notes"]))

    def test_the_hint_restates_the_pick_when_the_task_comes_up(self):
        h = self.handler()
        self.approve(h, choices={"2": "B"})
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE",
                                                  "result_log": EVIDENCE})
        self.assertIn("over your recommendation (빠르지만 서식이 사라짐)", res["message"])
        self.assertEqual(res["next_task"]["chosen_by_user"], "alternative")
        self.assertEqual(res["next_task"]["choice_reason"], "빠르지만 서식이 사라짐")
        cur = h.dispatch("get_current_plan", {"plan_id": res["plan_id"]})
        self.assertIn("over your recommendation", cur["next_action_hint"])

    def test_the_recommendation_needs_no_restating(self):
        h = self.handler()
        self.approve(h)
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE",
                                                  "result_log": EVIDENCE})
        self.assertNotIn("over your recommendation", res["message"])
        self.assertEqual(res["next_task"]["chosen_by_user"], "recommended")

    def test_the_completion_report_says_which_way_was_done(self):
        h = self.handler()
        self.approve(h, choices={"2": "B"})
        h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        for tid in (1, 2, 3):
            h.dispatch("update_task_progress", {"task_id": tid, "status": "DONE",
                                                "result_log": f"{EVIDENCE} {tid}"})
        display = self.ask(h)["display_to_user"]
        self.assertIn(f"2. (B안 선택) {ALT}", display)
        self.assertIn(f"1. {TASKS[0]}", display)


class TestTheUnchosenStayUnseen(AltCase):
    """D19 applied before the fact: after approval no response to the model contains an
    option that was not picked - an alternative it can still see is one it may still do."""

    def scan(self, h, picks):
        self.think(h, alts=[{"task_id": 2, "title": ALT, "reason": "r1"},
                            {"task_id": 2, "title": ALT2, "reason": "r2"}])
        self.ask(h)
        seen = [h.dispatch("request_user_approval", {"decision": "APPROVED", "choices": picks})]
        seen.append(h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"}))
        for tid in (1, 2, 3):
            seen.append(h.dispatch("update_task_progress", {
                "task_id": tid, "status": "DONE", "result_log": f"{EVIDENCE} {tid}"}))
            seen.append(h.dispatch("get_current_plan", {"plan_id": seen[0]["plan_id"]}))
        seen.append(self.ask(h))
        seen.append(h.dispatch("request_user_approval", {"decision": "APPROVED"}))
        seen.append(self.think(h, tasks=False, alts=None, reasons=None))
        return json.dumps(seen, ensure_ascii=False)

    def test_picking_an_alternative(self):
        text = self.scan(self.handler(), {"2": "B"})
        self.assertIn(ALT, text)
        self.assertNotIn(ALT2, text)
        self.assertNotIn(TASKS[1], text)

    def test_keeping_the_recommendation(self):
        text = self.scan(self.handler(), {})
        self.assertNotIn(ALT, text)
        self.assertNotIn(ALT2, text)


class TestPagePicking(AltCase):
    def test_the_page_gets_the_options(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        row = ui.opened[-1]["tasks"][1]
        self.assertEqual([o["title"] for o in row["options"]], [TASKS[1], ALT])
        self.assertNotIn("options", ui.opened[-1]["tasks"][0])

    def test_the_humans_pick_runs(self):
        ui = FakeApprovalUI(decision="APPROVED", choices={"2": 1})
        h = self.handler(ui)
        self.think(h)
        res = self.ask(h)
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(self.plan(h).tasks[1].title, ALT)

    def test_a_late_pick_runs(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        ui.resolve("APPROVED", choices={"2": 1})
        res = h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(self.plan(h).tasks[1].chosen, 1)

    def test_the_model_cannot_pick_while_the_page_is_open(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        res = h.dispatch("request_user_approval", {"decision": "APPROVED",
                                                   "choices": {"2": "B"}})
        self.assertEqual(res["error_code"], "APPROVAL_PENDING")
        self.assertIsNone(self.plan(h).tasks[1].chosen)

    def test_with_a_page_a_relayed_pick_is_ignored(self):
        h = self.handler(FakeApprovalUI(decision=None))
        notes: list[str] = []
        self.assertIsNone(h._model_choices({"choices": {"2": 1}}, notes))
        self.assertTrue(any("approval page" in n for n in notes))

    def test_the_chat_display_points_to_the_page(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui)
        self.think(h)
        self.assertIn("승인 페이지에서 고르실 수", self.ask(h)["display_to_user"])

    def test_choices_schema_only_in_chat_mode(self):
        def approval(**kw):
            return {t["name"]: t for t in build_tool_definitions(**kw)}[
                "request_user_approval"]["inputSchema"]["properties"]
        self.assertIn("choices", approval(blocking=False))
        self.assertNotIn("choices", approval())
        self.assertNotIn("choices", approval(approval_mode="return"))
        self.assertNotIn("choices", approval(blocking=False, alternatives=False))


class TestStoreRecordsOnlyWhatWasShown(AltCase):
    ROWS = [{"task_id": 1, "title": "a"},
            {"task_id": 2, "title": "b", "options": [{"title": "b"}, {"title": "c"}]}]

    def store(self, phase=PHASE_PLAN):
        s = ApprovalStore(self.state_dir)
        return s, s.publish("p", "g", "d", self.ROWS, "fp", phase)

    def test_a_valid_pick_is_recorded(self):
        s, rid = self.store()
        self.assertTrue(s.record_decision(rid, "APPROVED", "", choices={"2": 1}))
        self.assertEqual(s.claim(rid).choices, {"2": 1})

    def test_a_pick_that_was_not_on_screen_records_nothing(self):
        for bad in ({"2": 5}, {"1": 0}, "x"):
            s, rid = self.store()
            self.assertFalse(s.record_decision(rid, "APPROVED", "", choices=bad), bad)
            self.assertIsNone(s.entry(rid)["decision"])

    def test_picks_count_only_on_an_approval(self):
        s, rid = self.store()
        self.assertTrue(s.record_decision(rid, "REVISE", "고쳐 주세요", choices={"2": 9}))
        self.assertEqual(s.claim(rid).choices, {})

    def test_a_completion_report_takes_no_picks(self):
        s, rid = self.store(PHASE_COMPLETION)
        self.assertTrue(s.record_decision(rid, "APPROVED", "", choices={"2": 1}))
        self.assertEqual(s.claim(rid).choices, {})

    def test_a_halt_card_takes_picks(self):
        s, rid = self.store(PHASE_HALT)
        self.assertTrue(s.record_decision(rid, "APPROVED", "", choices={"2": 1}))
        self.assertEqual(s.claim(rid).choices, {"2": 1})


# ---------------------------------------------------------------------------
# Drafts and the halt card (decisions 2.0 §8-2, §8-3)
# ---------------------------------------------------------------------------


class TestDraftAlternatives(AltCase):
    def test_kept_with_the_draft(self):
        h = self.handler()
        self.think(h, more=True)
        plan = self.plan(h)
        self.assertEqual(plan.draft_alternatives, ALTS)
        self.assertEqual(plan.draft_reasons, {"2": "보고서 서식이 그대로 유지됨"})

    def test_a_new_draft_list_replaces_them(self):
        h = self.handler()
        self.think(h, more=True)
        self.think(h, step=2, more=True, alts=None, reasons=None)
        self.assertEqual(self.plan(h).draft_alternatives, [])

    def test_alternatives_alone_attach_to_the_draft(self):
        h = self.handler()
        self.think(h, more=True, alts=None, reasons=None)
        self.think(h, step=2, more=True, tasks=False)
        self.assertEqual(self.plan(h).draft_alternatives, ALTS)

    def test_an_auto_submitted_draft_offers_the_choices(self):
        h = self.handler(max_thinking_steps=2)
        self.think(h, more=True)
        res = self.think(h, step=2, more=True, tasks=False, alts=None, reasons=None)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["choice_points"], [2])

    def test_a_final_call_without_a_list_uses_the_draft_choices(self):
        h = self.handler()
        self.think(h, more=True)
        self.think(h, step=2, tasks=False, alts=None, reasons=None)
        self.assertTrue(self.plan(h).tasks[1].has_choice)

    def test_finalizing_clears_the_draft(self):
        h = self.handler()
        self.think(h, more=True)
        self.think(h, step=2)
        plan = self.plan(h)
        self.assertEqual((plan.draft_tasks, plan.draft_alternatives, plan.draft_reasons),
                         ([], [], {}))


class TestHaltCardChoices(AltCase):
    def loop(self, h):
        """The same call twice: a halt at breaker_repeat=2, with a draft carrying a choice."""
        self.think(h, more=True)
        self.think(h, step=2, more=True, thought="same")
        return self.think(h, step=2, more=True, thought="same")

    def test_the_card_shows_the_draft_choices(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, breaker_repeat=2)
        self.loop(h)
        card = ui.live
        self.assertEqual(card["phase"], PHASE_HALT)
        self.assertTrue(card["draft"])
        self.assertEqual([o["title"] for o in card["tasks"][1]["options"]], [TASKS[1], ALT])

    def test_approving_the_draft_applies_the_pick(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, breaker_repeat=2)
        self.loop(h)
        ui.resolve("APPROVED", choices={"2": 1})
        res = h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "APPROVED")
        task = self.plan(h).tasks[1]
        self.assertEqual((task.title, task.chosen), (ALT, 1))

    def test_in_chat_the_relayed_pick_applies(self):
        h = self.handler(breaker_repeat=2)
        self.loop(h)
        res = h.dispatch("request_user_approval", {"decision": "APPROVED",
                                                   "choices": {"2": "B"}})
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(self.plan(h).tasks[1].title, ALT)
        self.assertIn(f"task 2: '{ALT}'", res["message"])

    def test_the_halt_fingerprint_binds_the_draft_choices(self):
        h = self.handler(FakeApprovalUI(decision=None), breaker_repeat=2)
        self.loop(h)
        plan = self.plan(h)
        before = h._request_fingerprint(plan)
        plan.draft_alternatives = [{"task_id": 2, "title": "다른 것"}]
        self.assertNotEqual(h._request_fingerprint(plan), before)


# ---------------------------------------------------------------------------
# Schema, page, persistence
# ---------------------------------------------------------------------------


class TestSchema(unittest.TestCase):
    def plan_tool(self, **kw):
        return {t["name"]: t for t in build_tool_definitions(**kw)}["plan_and_think"]

    def test_both_profiles_advertise_the_fields(self):
        for profile in ("standard", "reasoning"):
            props = self.plan_tool(model_profile=profile)["inputSchema"]["properties"]
            self.assertIn("alternatives", props, profile)
            self.assertIn("recommended_reasons", props, profile)

    def test_only_the_reasoning_profile_is_told_to_turn_doubt_into_a_choice(self):
        self.assertIn("torn between", self.plan_tool(model_profile="reasoning")["description"])
        self.assertNotIn("torn between", self.plan_tool()["description"])

    def test_off_hides_everything(self):
        for profile in ("standard", "reasoning"):
            tool = self.plan_tool(model_profile=profile, alternatives=False)
            self.assertNotIn("alternatives", tool["inputSchema"]["properties"])
            self.assertNotIn("torn between", tool["description"])

    def test_the_limits_are_in_the_description(self):
        text = self.plan_tool(max_alternatives=2, max_choice_points=1)[
            "inputSchema"]["properties"]["alternatives"]["description"]
        self.assertIn("At most 2 per task, 1 tasks per plan", text)
        self.assertIn("not on facts you can check", text)

    def test_the_server_uses_the_config(self):
        sys.path.insert(0, str(ROOT))
        import server  # noqa: F401 - builds the protocol from a Config
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(state_dir=Path(tmp), blocking_approval=False, alternatives=False)
            proto = server.build_protocol(cfg)
            tool = {t["name"]: t for t in proto.tools}["plan_and_think"]
            self.assertNotIn("alternatives", tool["inputSchema"]["properties"])


class TestPageTemplate(unittest.TestCase):
    """The choice UI, asserted against the template (no socket); clicked through in a
    real browser separately."""

    def test_options_render_with_the_recommendation_marked(self):
        self.assertIn("function optionsHtml(d,t,allowOther)", _PAGE)
        self.assertIn("<span class=\"rec\">권장</span>", _PAGE)
        self.assertIn("(i===0?' checked':'')", _PAGE)

    def test_other_opens_the_comment_and_blocks_approval(self):
        self.assertIn("기타 (직접 입력)", _PAGE)
        self.assertIn("if(el.value==='other')openComment(req,tid,true);", _PAGE)
        self.assertIn("b.disabled=anyOther(id);", _PAGE)

    def test_the_approve_button_states_its_consequence(self):
        self.assertIn("function okLabel(phase,id)", _PAGE)
        self.assertIn("'번 '+LETTERS[changed[0][1]]+'안'", _PAGE)
        self.assertIn("id=\"ok-'+esc(d.id)+'\"", _PAGE)

    def test_the_halt_card_offers_the_choice_without_other(self):
        self.assertIn("(d.draft?optionsHtml(d,t,false):'')", _PAGE)

    def test_picks_travel_with_the_decision(self):
        self.assertIn("choices:choicesOf(id)", _PAGE)

    def test_picks_survive_a_rebuild_and_mirror_across_tabs(self):
        self.assertIn("dset(req,'ch'+tid,el.value);", _PAGE)
        self.assertIn("if(tid.indexOf('ch')===0){", _PAGE)
        self.assertIn("dget(d.id,'ch'+r.getAttribute('data-ctid'))", _PAGE)

    def test_a_refused_decision_keeps_the_drafts_and_says_so(self):
        self.assertIn("if(j&&j.ok){dclear(id);lastError='';}", _PAGE)
        self.assertIn("결정을 기록하지 못했습니다", _PAGE)

    def test_the_completion_report_shows_the_pick(self):
        self.assertIn("'안 선택'", _PAGE)

    def test_radios_are_named_by_their_option(self):
        """Found in the browser check: without it the accessibility tree read "0" / "1"."""
        self.assertIn("' aria-label=\"'+LETTERS[i]+'. '+esc(o.title)", _PAGE)


class TestPersistence(unittest.TestCase):
    def test_options_round_trip(self):
        task = Task(task_id=2, title="b", options=[{"title": "b", "reason": ""},
                                                     {"title": "c", "reason": "r"}], chosen=1)
        again = Task.from_dict(json.loads(json.dumps(task.to_dict())))
        self.assertEqual((again.options, again.chosen), (task.options, 1))

    def test_malformed_options_read_as_no_choice(self):
        for raw in ("x", [{"title": "only one"}], [{"no": "title"}, 3]):
            self.assertIsNone(Task.from_dict({"task_id": 1, "title": "t", "options": raw}).options)

    def test_a_1_16_state_file_loads(self):
        plan = Plan.from_dict({"plan_id": "p", "goal": "g", "tasks": [{"task_id": 1,
                                                                         "title": "t"}]})
        self.assertIsNone(plan.tasks[0].options)
        self.assertEqual((plan.draft_alternatives, plan.draft_reasons), ([], {}))

    def test_page_brief_carries_options_and_brief_does_not(self):
        task = Task(task_id=2, title="b", options=[{"title": "b", "reason": ""},
                                                     {"title": "c", "reason": ""}])
        self.assertNotIn("options", task.brief())
        self.assertEqual(len(task.page_brief()["options"]), 2)

    def test_render_helpers(self):
        plan = Plan(plan_id="p", goal="g", tasks=[
            Task(task_id=1, title="b", options=[{"title": "b", "reason": "x"},
                                                {"title": "c", "reason": "y"}])])
        text = render_plan_for_user(plan, on_page=False)
        self.assertIn("A. b [권장] — x", text)
        plan.tasks[0].chosen = 1
        plan.tasks[0].title = "c"
        self.assertIn("1. (B안 선택) c", render_completion_report(plan))


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = Config(state_dir=Path("."))
        self.assertEqual((cfg.alternatives, cfg.max_alternatives, cfg.max_choice_points),
                         (True, 3, 3))

    def test_env(self):
        env = {"PLANNING_MCP_ALTERNATIVES": "off", "PLANNING_MCP_MAX_ALTERNATIVES": "2",
               "PLANNING_MCP_MAX_CHOICE_POINTS": "5"}
        with mock.patch.dict(os.environ, env):
            cfg = Config.from_env()
        self.assertEqual((cfg.alternatives, cfg.max_alternatives, cfg.max_choice_points),
                         (False, 2, 5))


if __name__ == "__main__":
    unittest.main()
