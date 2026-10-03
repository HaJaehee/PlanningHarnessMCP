"""3.0.0 - when the active-plan limit is reached, the least recently used unfinished plan
is closed to make room (PLANNING_MCP_EVICT_LRU, on by default).

Before, a full table refused every new plan until a human rejected an old one on the
approval page - and an abandoned conversation never comes back to do that. With the
limit raised to 20 (1.16.0) the table fills with exactly such plans.

The guarantees pinned here:
- the victim is the plan written to longest ago, and only an unfinished one;
- a plan that is in use (touched within PLANNING_MCP_EVICT_MIN_IDLE) is never evicted -
  if every plan is in use the new one is refused, as before;
- nothing is lost silently: the audit line keeps the evicted plan's evidence, and a
  conversation that comes back to it is told what happened, not handed another plan.

    python -m unittest discover -s tests
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planning.config import Config  # noqa: E402
from planning.handlers import PlanningHandlers  # noqa: E402
from planning.models import Plan  # noqa: E402
from planning.state_machine import resolve_next_action  # noqa: E402
from planning.models import ErrorCode  # noqa: E402
from planning.store import MAX_EVICTED_REMEMBERED, State, Store  # noqa: E402
from test_server import FakeApprovalUI  # noqa: E402

LONG_AGO = "2020-01-01T00:00:00+09:00"


class EvictionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def handler(self, ui=None, limit=3, **cfg) -> PlanningHandlers:
        cfg.setdefault("blocking_approval", ui is not None)
        cfg.setdefault("approval_timeout", 1)
        return PlanningHandlers(
            Store(self.state_dir),
            Config(state_dir=self.state_dir, max_active_plans=limit, **cfg),
            approval_ui=ui,
        )

    @staticmethod
    def new_plan(h, goal, **kw):
        args = {"goal": goal, "thought": "t", "step_number": 1, "total_steps": 1,
                "need_more_thinking": False, "task_list": ["첫 태스크", "둘째 태스크"]}
        args.update(kw)
        return h.dispatch("plan_and_think", args)

    def fill(self, h, count):
        return [self.new_plan(h, f"목표 {i}")["plan_id"] for i in range(1, count + 1)]

    def age(self, h, plan_id, stamp=LONG_AGO):
        """Make a plan look untouched since `stamp`."""
        state = h.store.load()
        state.plans[plan_id].updated_at = stamp
        h.store.save(state)

    def ids(self, h) -> list[str]:
        return sorted(h.store.load().plans)

    def audit(self, event) -> list[dict]:
        path = self.state_dir / "audit.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if json.loads(l).get("event") == event]


class TestMakingRoom(EvictionCase):
    def test_the_least_recently_used_plan_goes(self):
        h = self.handler()
        a, b, c = self.fill(h, 3)
        self.age(h, b, "2020-01-01T00:00:00+09:00")
        self.age(h, a, "2021-01-01T00:00:00+09:00")
        res = self.new_plan(h, "새 목표")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(self.ids(h), sorted([a, c, res["plan_id"]]))
        event = self.audit("plan_evicted")[-1]
        self.assertEqual((event["plan_id"], event["limit"], event["made_room_for"]),
                         (b, 3, "새 목표"))

    def test_the_new_plan_is_not_told_about_it(self):
        """Somebody else's plan being closed is not this conversation's business."""
        h = self.handler()
        a, _, _ = self.fill(h, 3)
        self.age(h, a)
        res = self.new_plan(h, "새 목표")
        text = json.dumps(res, ensure_ascii=False)
        self.assertNotIn(a, text)
        self.assertNotIn("evict", text.lower())

    def test_order_is_by_last_write_not_by_creation(self):
        h = self.handler()
        a, b, c = self.fill(h, 3)
        for pid in (a, b, c):
            self.age(h, pid, "2022-01-01T00:00:00+09:00")
        # The oldest plan is used again, so it is no longer the least recently used.
        h.dispatch("request_user_approval",
                   {"decision": "ASK_USER", "plan_summary": "요약", "plan_id": a})
        self.new_plan(h, "새 목표")
        self.assertIn(a, self.ids(h))
        self.assertEqual(len(self.audit("plan_evicted")), 1)
        self.assertIn(self.audit("plan_evicted")[0]["plan_id"], (b, c))

    def test_only_as_many_as_needed(self):
        h = self.handler()
        a, b, c = self.fill(h, 3)
        for pid in (a, b, c):
            self.age(h, pid)
        self.new_plan(h, "새 목표")
        self.assertEqual(len(self.ids(h)), 3)
        self.assertEqual(len(self.audit("plan_evicted")), 1)

    def test_a_lowered_limit_evicts_down_to_it(self):
        h = self.handler(limit=5)
        plans = self.fill(h, 5)
        for index, pid in enumerate(plans):
            self.age(h, pid, f"202{index}-01-01T00:00:00+09:00")
        lowered = self.handler(limit=2)
        res = self.new_plan(lowered, "새 목표")
        self.assertTrue(res["ok"])
        self.assertEqual(self.ids(lowered), sorted([plans[4], res["plan_id"]]))

    def test_an_unreadable_timestamp_counts_as_the_most_idle(self):
        h = self.handler()
        a, b, c = self.fill(h, 3)
        self.age(h, a)
        self.age(h, b, "not a date")
        self.new_plan(h, "새 목표")
        event = self.audit("plan_evicted")[-1]
        self.assertEqual((event["plan_id"], event["idle_seconds"]), (b, None))

    def test_a_finished_plan_never_counts_and_is_never_the_victim(self):
        h = self.handler()
        a, b, c = self.fill(h, 3)
        state = h.store.load()
        state.plans[a].plan_status = "COMPLETED"
        state.plans[a].updated_at = LONG_AGO
        h.store.save(state)
        res = self.new_plan(h, "새 목표")
        self.assertTrue(res["ok"])
        self.assertEqual(self.audit("plan_evicted"), [])
        self.assertIn(a, self.ids(h))

    def test_any_unfinished_status_can_go(self):
        for status in ("DRAFTING", "AWAITING_APPROVAL", "APPROVED", "IN_EXECUTION",
                       "AWAITING_COMPLETION", "BLOCKED"):
            with tempfile.TemporaryDirectory() as tmp:
                self.state_dir = Path(tmp)
                h = self.handler()
                a, _, _ = self.fill(h, 3)
                state = h.store.load()
                state.plans[a].plan_status = status
                state.plans[a].updated_at = LONG_AGO
                h.store.save(state)
                self.assertTrue(self.new_plan(h, "새 목표")["ok"], status)
                self.assertEqual(self.audit("plan_evicted")[-1]["plan_status"], status)


class TestAPlanInUseIsNeverEvicted(EvictionCase):
    def test_fresh_plans_are_not_evicted_and_the_new_one_is_refused(self):
        """Every slot held by a plan somebody is using: the old answer still applies."""
        h = self.handler()
        self.fill(h, 3)
        res = self.new_plan(h, "새 목표")
        self.assertEqual(res["error_code"], "PLAN_AMBIGUOUS")
        self.assertEqual(len(res["active_plans"]), 3)
        self.assertEqual(self.audit("plan_evicted"), [])

    def test_the_floor_is_configurable(self):
        h = self.handler(evict_min_idle=0)
        self.fill(h, 3)
        self.assertTrue(self.new_plan(h, "새 목표")["ok"])
        self.assertEqual(len(self.audit("plan_evicted")), 1)

    def test_idle_just_under_the_floor_is_kept_and_just_over_goes(self):
        h = self.handler(evict_min_idle=300)
        a, b, c = self.fill(h, 3)
        with mock.patch.object(Plan, "idle_seconds", lambda self: 299.0):
            self.assertEqual(self.new_plan(h, "새 목표")["error_code"], "PLAN_AMBIGUOUS")
        with mock.patch.object(Plan, "idle_seconds", lambda self: 300.0):
            self.assertTrue(self.new_plan(h, "새 목표")["ok"])

    def test_a_model_that_keeps_opening_plans_cannot_empty_the_table(self):
        """Its own fresh plans fill the slots, and fresh plans cannot be evicted."""
        h = self.handler()
        old = self.fill(h, 3)
        for pid in old:
            self.age(h, pid)
        results = [self.new_plan(h, f"폭주 {i}") for i in range(10)]
        self.assertEqual([r["ok"] for r in results], [True] * 3 + [False] * 7)
        self.assertEqual(len(self.audit("plan_evicted")), 3)

    def test_turned_off_the_new_plan_is_refused_as_before(self):
        h = self.handler(evict_lru=False)
        plans = self.fill(h, 3)
        for pid in plans:
            self.age(h, pid)
        res = self.new_plan(h, "새 목표")
        self.assertEqual(res["error_code"], "PLAN_AMBIGUOUS")
        self.assertEqual(self.ids(h), sorted(plans))

    def test_continuing_an_existing_plan_never_evicts(self):
        h = self.handler()
        a, b, c = self.fill(h, 3)
        for pid in (a, b, c):
            self.age(h, pid)
        res = h.dispatch("get_current_plan", {"plan_id": a})
        self.assertEqual(res["plan_id"], a)
        self.assertEqual(self.audit("plan_evicted"), [])


class TestNothingIsLostSilently(EvictionCase):
    def worked(self, h):
        """A plan with one finished task and evidence, then left idle."""
        res = self.new_plan(h, "증거가 있는 계획")
        pid = res["plan_id"]
        h.dispatch("request_user_approval",
                   {"decision": "ASK_USER", "plan_summary": "요약", "plan_id": pid})
        h.dispatch("request_user_approval", {"decision": "APPROVED", "plan_id": pid})
        h.dispatch("update_task_progress",
                   {"task_id": 1, "status": "IN_PROGRESS", "plan_id": pid})
        h.dispatch("update_task_progress", {
            "task_id": 1, "status": "DONE", "plan_id": pid,
            "result_log": "reports/q3.xlsx 를 찾았습니다 (시트 3개)"})
        return pid

    def test_the_audit_line_keeps_the_evidence(self):
        h = self.handler()
        pid = self.worked(h)
        self.fill(h, 2)
        self.age(h, pid)
        self.new_plan(h, "새 목표")
        event = self.audit("plan_evicted")[-1]
        self.assertEqual((event["plan_id"], event["goal"], event["plan_status"],
                          event["progress"]),
                         (pid, "증거가 있는 계획", "IN_EXECUTION", "1/2 done"))
        self.assertEqual(event["tasks"][0]["result_log"], "reports/q3.xlsx 를 찾았습니다 (시트 3개)")
        self.assertGreater(event["idle_seconds"], 300)

    def test_its_request_leaves_the_approval_page(self):
        """An evicted plan that kept asking would show buttons that do nothing."""
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui)
        first = self.new_plan(h, "승인을 기다리다 버려진 계획")["plan_id"]
        h.dispatch("request_user_approval",
                   {"decision": "ASK_USER", "plan_summary": "요약", "plan_id": first})
        self.assertEqual(ui.live["plan_id"], first)
        ui.live = dict(ui.live)  # keep the request; add two more plans without asking
        self.new_plan(h, "목표 2")
        self.new_plan(h, "목표 3")
        self.age(h, first)
        self.new_plan(h, "새 목표")
        self.assertIsNone(ui.live)

    def test_the_state_file_remembers_what_it_closed(self):
        h = self.handler()
        pid = self.worked(h)
        self.fill(h, 2)
        self.age(h, pid)
        self.new_plan(h, "새 목표")
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        self.assertNotIn(pid, raw["plans"])
        record = raw["evicted"][pid]
        self.assertEqual((record["goal"], record["plan_status"], record["progress"]),
                         ("증거가 있는 계획", "IN_EXECUTION", "1/2 done"))
        self.assertIn("evicted_at", record)

    def test_only_the_newest_are_remembered(self):
        state = State()
        for i in range(MAX_EVICTED_REMEMBERED + 5):
            state.evict(Plan(plan_id=f"p{i}", goal="g"), 400, "t")
        self.assertEqual(len(state.evicted), MAX_EVICTED_REMEMBERED)
        self.assertNotIn("p0", state.evicted)
        self.assertIn(f"p{MAX_EVICTED_REMEMBERED + 4}", state.evicted)

    def test_a_hand_edited_record_cannot_wedge_the_file(self):
        self.assertEqual(State.from_dict({"evicted": ["x"]}).evicted, {})
        self.assertEqual(State.from_dict({"evicted": {"p1": 3, "p2": {"goal": "g"}}}).evicted,
                         {"p2": {"goal": "g"}})
        # A file from before 3.0.0 has no such key at all.
        self.assertEqual(State.from_dict({"plans": {}}).evicted, {})


class TestComingBackToAnEvictedPlan(EvictionCase):
    def evicted(self, **cfg):
        h = self.handler(**cfg)
        a, b, c = self.fill(h, 3)
        self.age(h, a)
        new = self.new_plan(h, "새 목표")["plan_id"]
        return h, a, new

    def check(self, res, pid):
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_EVICTED")
        self.assertEqual(res["next_action"], "ANSWER_USER")
        self.assertEqual(res["evicted_plan"]["plan_id"], pid)
        self.assertEqual(res["evicted_plan"]["goal"], "목표 1")
        self.assertIn("closed it to make room", res["message"])
        # Never the other conversations' plans: that is how one would be adopted.
        self.assertNotIn("active_plans", res)
        self.assertIsNone(res["plan_id"])

    def test_every_tool_says_what_happened(self):
        _, pid, _ = self.evicted()
        # A fresh handler per call: four refusals in a row on one would be stopped by
        # the breaker (see test_asking_again_and_again_ends), which is not the point here.
        self.check(self.handler().dispatch("get_current_plan", {"plan_id": pid}), pid)
        self.check(self.handler().dispatch(
            "update_task_progress",
            {"task_id": 1, "status": "IN_PROGRESS", "plan_id": pid}), pid)
        self.check(self.handler().dispatch(
            "request_user_approval",
            {"decision": "ASK_USER", "plan_summary": "요약", "plan_id": pid}), pid)
        self.check(self.new_plan(self.handler(), "목표 1", step_number=2, plan_id=pid), pid)

    def test_the_hint_tells_the_user_not_another_plan(self):
        _, hint = resolve_next_action(None, ErrorCode.PLAN_EVICTED)
        self.assertIn("Tell the user", hint)
        self.assertIn("Do not use another plan in its place", hint)
        self.assertIn("plan_and_think", hint)

    def test_starting_the_same_goal_again_is_a_new_plan(self):
        h = self.handler()
        a, b, c = self.fill(h, 3)
        self.age(h, a)
        self.new_plan(h, "새 목표")  # a is evicted
        self.age(h, b)              # ... and there is an idle plan to make room again
        res = self.new_plan(h, "목표 1", plan_id=a)
        self.assertTrue(res["ok"], res)
        self.assertNotIn(res["plan_id"], (a, b, c))
        self.assertTrue(any("closed by the server" in n for n in res["input_notes"]))

    def test_an_id_that_never_existed_is_still_just_unknown(self):
        h, _, _ = self.evicted()
        res = h.dispatch("get_current_plan", {"plan_id": "plan_19990101_0001"})
        self.assertNotEqual(res.get("error_code"), "PLAN_EVICTED")
        self.assertIn("active_plans", res)

    def test_it_survives_a_restart(self):
        h, pid, _ = self.evicted()
        again = self.handler()
        self.check(again.dispatch("get_current_plan", {"plan_id": pid}), pid)

    def test_asking_again_and_again_ends(self):
        """The 1.16 breaker: the same refused call, repeated, is stopped."""
        h, pid, _ = self.evicted()
        codes = [h.dispatch("get_current_plan", {"plan_id": pid}).get("error_code")
                 for _ in range(6)]
        self.assertEqual(codes[0], "PLAN_EVICTED")
        self.assertIn("LOOP_HALTED", codes)


class TestAnIdIsNeverReused(EvictionCase):
    """Found by these tests: the plan that displaced an evicted one was given its id.

    Ids were "how many of today's plans exist, plus one", so removing a plan handed
    its id out again - to the very plan that took its place. The conversation coming
    back for the old plan would have been put on somebody else's new one.
    """

    def test_the_plan_that_takes_the_slot_does_not_take_the_id(self):
        h = self.handler()
        a, b, c = self.fill(h, 3)
        self.age(h, a)
        new = self.new_plan(h, "새 목표")["plan_id"]
        self.assertNotIn(new, (a, b, c))
        self.assertEqual(h.dispatch("get_current_plan", {"plan_id": a})["error_code"],
                         "PLAN_EVICTED")

    def test_ids_keep_rising_through_many_evictions(self):
        h = self.handler(evict_min_idle=0)
        seen = [self.new_plan(h, f"목표 {i}")["plan_id"] for i in range(12)]
        self.assertEqual(len(set(seen)), 12)
        self.assertEqual(seen, sorted(seen))

    def test_not_even_after_the_record_of_an_eviction_is_gone(self):
        """The remembered evictions are capped; the last issued id is not forgotten."""
        h = self.handler(evict_min_idle=0)
        first = self.new_plan(h, "목표 0")["plan_id"]
        state = h.store.load()
        state.plans.clear()
        state.evicted.clear()
        h.store.save(state)
        self.assertGreater(self.new_plan(h, "목표 1")["plan_id"], first)

    def test_nor_after_retention_prunes_a_finished_plan(self):
        h = PlanningHandlers(
            Store(self.state_dir, max_plans=1),
            Config(state_dir=self.state_dir, blocking_approval=False, max_plans=1))
        first = self.new_plan(h, "끝난 계획")["plan_id"]
        state = h.store.load()
        state.plans[first].plan_status = "COMPLETED"
        h.store.save(state)
        second = self.new_plan(h, "다음 계획")["plan_id"]
        third = self.new_plan(h, "그다음 계획")["plan_id"]
        self.assertNotIn(first, h.store.load().plans)  # pruned
        self.assertEqual(len({first, second, third}), 3)

    def test_a_state_file_from_before_3_0_keeps_counting_from_its_plans(self):
        state = State.from_dict({"plans": {}})
        self.assertIsNone(state.last_plan_id)
        store = Store(self.state_dir)
        self.assertTrue(store.next_plan_id(state).endswith("_0001"))
        self.assertTrue(store.next_plan_id(state).endswith("_0002"))


class TestConfig(unittest.TestCase):
    def test_eviction_is_the_default(self):
        cfg = Config(state_dir=Path("."))
        self.assertEqual((cfg.evict_lru, cfg.evict_min_idle, cfg.max_active_plans),
                         (True, 300, 20))

    def test_from_env(self):
        with mock.patch.dict(os.environ, {"PLANNING_MCP_EVICT_LRU": "false",
                                          "PLANNING_MCP_EVICT_MIN_IDLE": "1800"}):
            cfg = Config.from_env()
        self.assertEqual((cfg.evict_lru, cfg.evict_min_idle), (False, 1800))
        with mock.patch.dict(os.environ, {"PLANNING_MCP_EVICT_MIN_IDLE": "soon"}):
            self.assertEqual(Config.from_env().evict_min_idle, 300)


if __name__ == "__main__":
    unittest.main()
