"""3.0.0 - the verification contract: what "done" means, and what the server can check.

Per task, the model may say what exists or is true when it is finished (`done_when`).
The human approves it with the plan and may write it themselves on the approval page.
At DONE the server refuses a result_log that only says the criterion back, and - inside
the folders it was told it may look in - a file that is not there. The completion page
shows the criterion beside the evidence and what the server itself found.
Plan: docs/plan-3.0-verification-contract.md.

The guarantees pinned here:
- a plan under no contract behaves - and fingerprints - exactly as in 2.0;
- the server refuses only what is structurally impossible, and never looks outside the
  allowed folders;
- only the human changes what "finished" means once a plan is on the page, and the
  wording they replaced never reaches the model again.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planning import evidence  # noqa: E402
from planning.approval import (  # noqa: E402
    _PAGE,
    PHASE_COMPLETION,
    PHASE_HALT,
    PHASE_PLAN,
    ApprovalServer,
    ApprovalStore,
    page_html,
)
from planning.config import (  # noqa: E402
    SERVER_AUTHOR,
    SERVER_AUTHOR_EMAIL,
    SERVER_VERSION,
    Config,
)
from planning.evidence import (  # noqa: E402
    check_files,
    check_mentions,
    clean_done_when,
    extract_paths,
    novelty,
    repeats_criterion,
    validate_page_criteria,
)
from planning.handlers import PlanningHandlers  # noqa: E402
from planning.leniency import normalize  # noqa: E402
from planning.models import Plan, Task, now_iso  # noqa: E402
from planning.responses import render_completion_report, render_plan_for_user  # noqa: E402
from planning.schemas import TOOL_DEFINITIONS, build_tool_definitions  # noqa: E402
from planning.store import Store  # noqa: E402
from test_server import FakeApprovalUI  # noqa: E402

GOAL = "Q3 매출 보고서를 요약해 팀장에게 보낸다"
TASKS = ["보고서 파일 찾기", "분기별 매출 집계", "5줄 요약 작성"]
CHECK = "분기별 매출 합계 4행이 있는 표가 만들어진다"
CHECKS = [{"task_id": 2, "check": CHECK}]
REAL = "분기별 합계 표를 만들었습니다: 1분기 120, 2분기 135, 3분기 128, 4분기 150"
PARROT = "분기별 매출 합계 4행이 있는 표가 만들어졌습니다"
EVIDENCE = "결과를 정리해 팀 공유 문서의 3번째 절에 붙여 넣음"


class ContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.state_dir = base / "state"
        self.root = base / "work"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def handler(self, ui=None, files=False, **cfg) -> PlanningHandlers:
        cfg.setdefault("blocking_approval", ui is not None)
        cfg.setdefault("approval_timeout", 1)
        if files:
            cfg.setdefault("artifact_roots", (self.root,))
        return PlanningHandlers(
            Store(self.state_dir), Config(state_dir=self.state_dir, **cfg), approval_ui=ui
        )

    @staticmethod
    def think(h, step=1, more=False, tasks=None, checks=CHECKS, **kw):
        args = {"goal": GOAL, "thought": "t", "step_number": step, "total_steps": step + 1,
                "need_more_thinking": more}
        if tasks is not False:
            args["task_list"] = list(tasks or TASKS)
        if checks is not None:
            args["done_when"] = checks
        args.update(kw)
        return h.dispatch("plan_and_think", args)

    def plan(self, h) -> Plan:
        state = h.store.load()
        return state.active_plan or max(state.plans.values(), key=lambda p: p.updated_at)

    def ask(self, h):
        return h.dispatch("request_user_approval",
                          {"decision": "ASK_USER", "plan_summary": "보고서를 요약합니다."})

    def approved(self, h, **kw):
        """Chat mode: plan, show it, and report the user's yes."""
        self.think(h, **kw)
        self.ask(h)
        return h.dispatch("request_user_approval", {"decision": "APPROVED"})

    @staticmethod
    def start(h, task_id=1):
        return h.dispatch("update_task_progress", {"task_id": task_id, "status": "IN_PROGRESS"})

    @staticmethod
    def done(h, task_id, log=EVIDENCE, **kw):
        return h.dispatch("update_task_progress",
                          {"task_id": task_id, "status": "DONE", "result_log": log, **kw})

    def audit(self, event) -> list[dict]:
        path = self.state_dir / "audit.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if json.loads(l).get("event") == event]


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


class TestLeniency(unittest.TestCase):
    def checks(self, value):
        return normalize("plan_and_think", {"goal": "g", "done_when": value})[0].get(
            "done_when")

    def test_every_shape_a_model_sends(self):
        for raw in (CHECKS, {"2": CHECK}, f"2: {CHECK}", {"task_id": "2", "criterion": CHECK},
                    json.dumps(CHECKS, ensure_ascii=False)):
            self.assertEqual(self.checks(raw), {2: CHECK}, raw)

    def test_the_first_criterion_for_a_task_wins(self):
        self.assertEqual(
            self.checks([{"task_id": 2, "check": "첫 번째"}, {"task_id": 2, "check": "두 번째"}]),
            {2: "첫 번째"})

    def test_digits_in_the_text_never_become_the_task_id(self):
        self.assertIsNone(self.checks([{"task": "Q3 report", "check": "a table exists"}]))

    def test_unreadable_is_dropped_with_a_note(self):
        clean, notes = normalize("plan_and_think", {"goal": "g", "done_when": 42})
        self.assertNotIn("done_when", clean)
        self.assertTrue(any("done_when" in n for n in notes))

    def test_a_misnamed_field_is_read(self):
        clean, _ = normalize("plan_and_think", {"goal": "g", "acceptance_criteria": {"1": "x"}})
        self.assertEqual(clean["done_when"], {1: "x"})

    def test_files_in_every_shape(self):
        def files(value):
            return normalize("update_task_progress",
                             {"task_id": 1, "status": "DONE", "files": value})[0].get("files")
        self.assertEqual(files(["a.txt", "b c.txt"]), ["a.txt", "b c.txt"])
        self.assertEqual(files('["x.csv"]'), ["x.csv"])
        self.assertEqual(files([{"path": "p.md"}]), ["p.md"])
        self.assertIsNone(files(5))

    def test_a_comma_in_a_file_name_is_not_a_separator(self):
        """Cutting a path in two would report a file that was never claimed."""
        clean, _ = normalize("update_task_progress", {
            "task_id": 1, "status": "DONE", "files": "매출, 3분기.xlsx; out/b.csv"})
        self.assertEqual(clean["files"], ["매출, 3분기.xlsx", "out/b.csv"])


class TestCleanDoneWhen(unittest.TestCase):
    def test_a_real_criterion_is_kept(self):
        out, notes = clean_done_when(TASKS, {2: CHECK}, 200)
        self.assertEqual((out, notes), ({2: CHECK}, []))

    def test_one_that_only_says_done_is_dropped(self):
        """The laziest use of the field: a criterion that adds nothing to the title."""
        for empty in ("작업이 완료된다", "The task is done.", "완료", TASKS[1], "ok"):
            out, notes = clean_done_when(TASKS, {2: empty}, 200)
            self.assertEqual(out, {}, empty)
            self.assertTrue(any("only says the task gets done" in n for n in notes))

    def test_a_task_number_outside_the_list(self):
        out, notes = clean_done_when(TASKS, {0: CHECK, 9: CHECK}, 200)
        self.assertEqual(out, {})
        self.assertTrue(any("1 to 3" in n for n in notes))

    def test_too_long_is_cut_and_said(self):
        out, notes = clean_done_when(TASKS, {1: "가" * 300}, 200)
        self.assertEqual(len(out[1]), 200)
        self.assertTrue(any("cut to 200" in n for n in notes))

    def test_a_finished_task_takes_none(self):
        out, notes = clean_done_when(TASKS, {1: CHECK}, 200, locked={1})
        self.assertEqual(out, {})
        self.assertTrue(any("already done" in n for n in notes))


class TestTheRepeatCheck(unittest.TestCase):
    """The threshold is set from these cases; they are the only data there is.

    A criterion said back - with its tense changed, or word for word - is refused. A
    report that adds a count, a size or a name is not, even when the criterion already
    named the file: refusing that would punish the model for a precise criterion.
    """

    REPEATS = [
        (CHECK, CHECK + "."),
        (CHECK, PARROT),
        ("The summary is saved to out/summary.md", "The summary was saved to out/summary.md"),
        ("요약 파일이 D:/out/summary.md 로 저장된다", "요약 파일을 D:/out/summary.md 로 저장했습니다"),
        ("A table with one revenue total per quarter (4 rows) exists",
         "A table with one revenue total per quarter (4 rows) now exists."),
    ]
    REPORTS = [
        (CHECK, REAL),
        (CHECK, "피벗 결과를 D:/reports/q4_pivot.xlsx 로 저장했습니다. 4행(1~4분기), 합계 533"),
        (CHECK, "표 완성. 합계 533, 4행."),
        ("The summary is saved to out/summary.md",
         "Saved the summary to out/summary.md (5 lines, 412 bytes)"),
        ("요약 파일이 D:/out/summary.md 로 저장된다",
         "요약 파일을 D:/out/summary.md 로 저장했습니다 (5줄)"),
        ("오류가 0건이다", "검사 결과 오류 0건, 경고 2건"),
        ("담당자 3명의 이름이 표에 있다", "표에 김철수, 이영희, 박민수 3명을 넣었습니다"),
    ]

    def test_a_criterion_said_back_is_a_repeat(self):
        threshold = Config(state_dir=Path(".")).evidence_novelty
        for check, log in self.REPEATS:
            self.assertTrue(repeats_criterion(check, log, threshold), (novelty(check, log), log))

    def test_a_report_that_adds_something_is_not(self):
        threshold = Config(state_dir=Path(".")).evidence_novelty
        for check, log in self.REPORTS:
            self.assertFalse(repeats_criterion(check, log, threshold), (novelty(check, log), log))

    def test_no_criterion_or_no_threshold_never_refuses(self):
        self.assertFalse(repeats_criterion(None, PARROT, 0.3))
        self.assertFalse(repeats_criterion(CHECK, CHECK, 0))

    def test_novelty_is_a_share(self):
        self.assertEqual(novelty(CHECK, CHECK), 0.0)
        self.assertEqual(novelty(CHECK, ""), 0.0)
        self.assertEqual(novelty("", REAL), 1.0)


class TestExtractPaths(unittest.TestCase):
    def test_paths_as_people_write_them(self):
        text = ("피벗 결과를 D:/reports/q4_pivot.xlsx 로 저장했습니다. out/summary.txt에도 썼고 "
                "/reports/q3.xlsx 는 그대로이며 매출_요약.xlsx 를 새로 만들었습니다.")
        self.assertEqual(extract_paths(text), [
            "D:/reports/q4_pivot.xlsx", "out/summary.txt", "/reports/q3.xlsx", "매출_요약.xlsx"])

    def test_numbers_and_urls_are_not_paths(self):
        self.assertEqual(extract_paths("버전 3.5, 합계 1,204.50 - http://host/page.html 참고"), [])

    def test_a_backslash_path(self):
        self.assertEqual(extract_paths(r"saved to D:\reports\q4.xlsx."), [r"D:\reports\q4.xlsx"])


class FilesCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "work"
        (self.root / "out").mkdir(parents=True)
        self.roots = (self.root,)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name, text="x", age=0.0) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        if age:
            past = time.time() - age
            os.utime(path, (past, past))
        return path

    def state(self, path, started=None):
        return check_files([path], self.roots, started or now_iso())[0]["state"]


class TestCheckFiles(FilesCase):
    def test_a_file_written_during_the_task(self):
        started = now_iso()
        self.write("out/a.txt")
        fact = check_files(["out/a.txt"], self.roots, started)[0]
        self.assertEqual((fact["state"], fact["size"], fact["source"]), ("found", 1, "declared"))
        self.assertIn("mtime", fact)

    def test_the_full_path_works_too(self):
        path = self.write("out/a.txt")
        self.assertEqual(self.state(str(path)), "found")

    def test_one_that_was_there_before_the_task(self):
        """"Find the file" is a real task - not refused, but not credited as produced."""
        self.write("out/a.txt", age=3600)
        self.assertEqual(self.state("out/a.txt"), "old")

    def test_missing_empty_and_folder(self):
        self.write("empty.txt", "")
        self.assertEqual(self.state("nope.txt"), "missing")
        self.assertEqual(self.state("empty.txt"), "empty")
        self.assertEqual(self.state("out"), "folder")

    @unittest.skipUnless(os.name == "nt", "a rooted path with no drive is a real path elsewhere")
    def test_a_sandbox_style_path_is_tried_under_the_allowed_folder(self):
        self.write("out/a.txt")
        self.assertEqual(self.state("/workspace/out/a.txt"), "found")
        self.assertEqual(self.state("/workspace/out/nope.txt"), "missing")

    def test_no_roots_no_checks(self):
        self.assertEqual(check_files(["out/a.txt"], (), now_iso()), [])


class TestTheServerNeverLooksOutside(FilesCase):
    OUTSIDE = ["../secret.txt", "out/../../secret.txt", "D:rel.txt"] + (
        ["C:/Windows/win.ini", r"\\server\share\x.txt"] if os.name == "nt" else ["/etc/passwd"]
    )

    def test_outside_paths_are_reported_not_checked(self):
        for path in self.OUTSIDE:
            self.assertEqual(self.state(path), "outside", path)

    def test_nothing_on_disk_is_touched_for_them(self):
        with mock.patch.object(evidence.os, "stat") as stat, \
                mock.patch.object(evidence.os.path, "realpath") as real:
            check_files(self.OUTSIDE, self.roots, now_iso())
        self.assertEqual((stat.call_count, real.call_count), (0, 0))

    def test_a_link_out_of_the_folder_is_not_followed(self):
        outside = self.root.parent / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            os.symlink(outside, self.root / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.assertEqual(self.state("link.txt"), "missing")

    def test_a_check_that_does_not_come_back_is_unknown_not_missing(self):
        """A stalled network folder must not refuse a DONE - or hold the state lock."""
        def slow(*a, **k):
            time.sleep(0.5)
            return {"path": "x", "state": "found"}
        with mock.patch.object(evidence, "_check_one", side_effect=slow):
            began = time.monotonic()
            facts = check_files(["a.txt", "b.txt"], self.roots, now_iso(), timeout=0.05)
        self.assertLess(time.monotonic() - began, 0.4)
        self.assertEqual([f["state"] for f in facts], ["unknown", "unknown"])
        self.assertEqual(evidence.refused(facts), [])


class TestMentions(FilesCase):
    def test_only_a_file_that_is_there_is_kept(self):
        """"Removed out/tmp.csv" is a true sentence about a file that is not there."""
        self.write("out/a.txt")
        facts = check_mentions("out/a.txt 에 저장하고 out/tmp.csv 는 지웠습니다", [],
                               self.roots, None)
        self.assertEqual([(f["path"], f["state"], f["source"]) for f in facts],
                         [("out/a.txt", "found", "mentioned")])

    def test_a_declared_file_is_not_listed_twice(self):
        self.write("out/a.txt")
        self.assertEqual(check_mentions("out/a.txt 에 저장", ["out/a.txt"], self.roots, None), [])


# ---------------------------------------------------------------------------
# Planning: the criterion is proposed, approved, and handed over with the task
# ---------------------------------------------------------------------------


class TestProposing(ContractCase):
    def test_the_criterion_is_stored_and_returned(self):
        h = self.handler()
        res = self.think(h)
        self.assertEqual(self.plan(h).tasks[1].done_when, CHECK)
        self.assertEqual(res["tasks"][1]["done_when"], CHECK)
        self.assertNotIn("done_when", res["tasks"][0])
        self.assertEqual(self.audit("plan_finalized")[-1]["criteria"], [2])

    def test_an_empty_criterion_is_dropped_with_a_note(self):
        h = self.handler()
        res = self.think(h, checks=[{"task_id": 2, "check": "작업이 완료된다"}])
        self.assertIsNone(self.plan(h).tasks[1].done_when)
        self.assertTrue(any("only says the task gets done" in n for n in res["input_notes"]))

    def test_turned_off_it_is_ignored_and_said(self):
        h = self.handler(done_when=False)
        res = self.think(h)
        self.assertIsNone(self.plan(h).tasks[1].done_when)
        self.assertTrue(any("turned off" in n for n in res["input_notes"]))

    def test_the_chat_text_shows_it(self):
        h = self.handler()
        self.think(h)
        self.assertIn(f"   태스크 완료 기준: {CHECK}", self.ask(h)["display_to_user"])

    def test_the_page_gets_it(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        rows = ui.opened[-1]["tasks"]
        self.assertEqual(rows[1]["done_when"], CHECK)
        self.assertNotIn("done_when", rows[0])


class TestNothingChangesWithoutAContract(ContractCase):
    """A plan whose model sent no criterion, on a server with no folders to look in."""

    def test_the_fingerprint_is_the_2_0_one(self):
        h = self.handler()
        self.approved(h, checks=None)
        self.start(h)
        self.done(h, 1)
        plan = self.plan(h)
        payload = "\x00".join(
            [plan.goal]
            + [f"{t.task_id}|{t.title}|{t.status}|{(t.result_log or '')}" for t in plan.tasks]
        )
        self.assertEqual(PlanningHandlers._fingerprint(plan),
                         hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16])

    def test_no_response_mentions_the_contract(self):
        h = self.handler()
        seen = [self.approved(h, checks=None), self.start(h)]
        for tid in (1, 2, 3):
            seen.append(self.done(h, tid, f"{EVIDENCE} {tid}"))
        seen.append(self.ask(h))
        text = json.dumps(seen, ensure_ascii=False)
        for word in ("done_when", "완료 기준", "서버 확인", "failure_note", "checks"):
            self.assertNotIn(word, text)

    def test_the_default_tool_list_offers_no_files(self):
        update = {t["name"]: t for t in TOOL_DEFINITIONS}["update_task_progress"]
        self.assertNotIn("files", update["inputSchema"]["properties"])

    def test_the_chat_texts_are_unchanged(self):
        plan = Plan(plan_id="p", goal="g", tasks=[
            Task(1, "a", status="DONE", result_log="한 일", started_at=now_iso(),
                 finished_at=now_iso())])
        self.assertEqual(render_plan_for_user(plan).splitlines(),
                         ["계획 승인 요청", "목표: g", "", "1. a", "",
                          "이 계획을 승인합니까? (승인 / 수정 요청 / 거절)"])
        self.assertEqual(render_completion_report(plan).splitlines()[3:5],
                         ["1. a", "   -> 한 일"])


class TestHandover(ContractCase):
    def test_the_criterion_rides_with_the_task(self):
        """Approved several turns ago, it would be a sentence the model has lost."""
        h = self.handler()
        self.approved(h)
        self.start(h)
        res = self.done(h, 1)
        self.assertEqual(res["next_task"]["task_id"], 2)
        self.assertEqual(res["next_task"]["done_when"], CHECK)
        self.assertIn(f'It is finished when: "{CHECK}"', res["next_action_hint"])
        self.assertIn("not the sentence repeated", res["next_action_hint"])

    def test_a_task_with_none_says_nothing(self):
        h = self.handler()
        res = self.approved(h)
        self.assertNotIn("done_when", res["next_task"])
        self.assertNotIn("finished when", res["next_action_hint"])

    def test_only_the_current_tasks_criterion_is_sent(self):
        h = self.handler()
        self.approved(h, checks=[{"task_id": 2, "check": CHECK},
                                 {"task_id": 3, "check": "요약이 정확히 5줄이다"}])
        res = self.start(h)
        self.assertNotIn("요약이 정확히 5줄", json.dumps(res, ensure_ascii=False))


# ---------------------------------------------------------------------------
# DONE: what the server refuses
# ---------------------------------------------------------------------------


class TestEvidenceAgainstTheCriterion(ContractCase):
    # A criterion and the same sentence said back, for the cases where a file decides
    # whether that is acceptable. Asserted to be a repeat, so the tests cannot pass
    # merely because the pair stopped counting as one.
    SAVED = "5줄 요약이 out.md 파일로 저장된다"
    SAID_BACK = "5줄 요약이 out.md 파일로 저장되었습니다"

    def test_the_pair_used_below_is_a_repeat(self):
        self.assertTrue(repeats_criterion(self.SAVED, self.SAID_BACK, 0.3))

    def at_task_two(self, **cfg):
        h = self.handler(**cfg)
        self.approved(h)
        self.start(h)
        self.done(h, 1)
        return h

    def test_the_criterion_said_back_is_refused(self):
        h = self.at_task_two()
        res = self.done(h, 2, PARROT)
        self.assertEqual(res["error_code"], "MISSING_RESULT_LOG")
        self.assertIn("only repeats the done_when sentence", res["message"])
        self.assertEqual(self.plan(h).tasks[1].status, "IN_PROGRESS")
        self.assertEqual(self.audit("done_repeats_criterion")[-1]["task_id"], 2)

    def test_real_evidence_is_accepted_and_scored(self):
        h = self.at_task_two()
        res = self.done(h, 2, REAL)
        self.assertTrue(res["ok"])
        self.assertGreater(self.audit("task_done")[-1]["novelty"], 0.5)

    def test_the_threshold_can_be_turned_off(self):
        h = self.at_task_two(evidence_novelty=0)
        self.assertTrue(self.done(h, 2, PARROT)["ok"])

    def test_a_task_under_no_criterion_is_not_scored(self):
        h = self.handler()
        self.approved(h)
        self.start(h)
        self.assertTrue(self.done(h, 1)["ok"])
        self.assertNotIn("novelty", self.audit("task_done")[-1])

    def test_a_repeat_with_a_file_the_server_found_is_accepted(self):
        """Once the server has found the output itself, the sentence need not carry it."""
        h = self.handler(files=True)
        self.approved(h, checks=[{"task_id": 1, "check": self.SAVED}])
        self.start(h)
        (self.root / "out.md").write_text("요약", encoding="utf-8")
        res = self.done(h, 1, self.SAID_BACK, files=["out.md"])
        self.assertTrue(res["ok"], res)

    def test_a_file_that_was_already_there_does_not_stand_in(self):
        """It proves the file exists, not that this task wrote it."""
        h = self.handler(files=True)
        self.approved(h, checks=[{"task_id": 1, "check": self.SAVED}])
        path = self.root / "out.md"
        path.write_text("예전 요약", encoding="utf-8")
        past = time.time() - 3600
        os.utime(path, (past, past))
        self.start(h)
        res = self.done(h, 1, self.SAID_BACK, files=["out.md"])
        self.assertEqual(res["error_code"], "MISSING_RESULT_LOG")

    def test_a_near_repeat_is_shown_to_the_human_not_refused(self):
        h = self.at_task_two()
        near = "완료 기준 충족: 분기별 매출 합계 4행이 있는 표가 만들어졌음을 확인했습니다"
        self.assertTrue(self.done(h, 2, near)["ok"])
        self.assertTrue(self.plan(h).tasks[1].page_brief()["echo"])
        self.assertNotIn("echo", self.plan(h).tasks[1].brief())


class TestFilesAtDone(ContractCase):
    def started(self, **cfg):
        h = self.handler(files=True, **cfg)
        self.approved(h, checks=None)
        self.start(h)
        return h

    def test_a_file_that_is_not_there_refuses_done(self):
        h = self.started()
        res = self.done(h, 1, files=["nope.xlsx"])
        self.assertEqual(res["error_code"], "FILE_NOT_FOUND")
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        self.assertIn("'nope.xlsx'", res["message"])
        self.assertIn(str(self.root), res["message"])
        self.assertEqual(res["next_task"]["task_id"], 1)
        self.assertEqual(self.plan(h).tasks[0].status, "IN_PROGRESS")
        self.assertEqual(self.audit("file_not_found")[-1]["files"], ["nope.xlsx"])

    def test_an_empty_file_refuses_too(self):
        h = self.started()
        (self.root / "empty.csv").write_text("", encoding="utf-8")
        self.assertEqual(self.done(h, 1, files=["empty.csv"])["error_code"], "FILE_NOT_FOUND")

    def test_saving_it_and_sending_again_is_accepted(self):
        h = self.started()
        self.done(h, 1, files=["q4.xlsx"])
        (self.root / "q4.xlsx").write_text("data", encoding="utf-8")
        res = self.done(h, 1, files=["q4.xlsx"])
        self.assertTrue(res["ok"], res)
        task = self.plan(h).tasks[0]
        self.assertEqual([(c["path"], c["state"]) for c in task.checks], [("q4.xlsx", "found")])
        self.assertEqual((task.claims_withdrawn, task.file_refusals), ([], []))
        self.assertEqual(self.audit("task_done")[-1]["checks"], [["declared", "found"]])

    def test_dropping_the_claim_is_allowed_and_shown(self):
        """The task may truly have made no file - but the human sees the claim was made."""
        h = self.started()
        self.done(h, 1, files=["nope.xlsx"])
        self.assertTrue(self.done(h, 1)["ok"])
        task = self.plan(h).tasks[0]
        self.assertEqual(task.claims_withdrawn, ["nope.xlsx"])
        self.assertEqual(task.page_brief()["claims_withdrawn"], ["nope.xlsx"])
        self.assertNotIn("claims_withdrawn", task.brief())
        self.assertEqual(self.audit("file_claim_withdrawn")[-1]["files"], ["nope.xlsx"])

    def test_a_path_outside_the_folders_is_neither_checked_nor_refused(self):
        h = self.started()
        res = self.done(h, 1, files=["../elsewhere/out.csv"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.plan(h).tasks[0].checks[0]["state"], "outside")

    def test_a_file_named_only_in_the_result_log_is_confirmed(self):
        h = self.started()
        (self.root / "summary.md").write_text("요약", encoding="utf-8")
        self.assertTrue(self.done(h, 1, "요약을 summary.md 에 저장하고 tmp.csv 는 지웠습니다")["ok"])
        self.assertEqual(
            [(c["path"], c["state"], c["source"]) for c in self.plan(h).tasks[0].checks],
            [("summary.md", "found", "mentioned")])

    def test_an_old_file_is_accepted_and_marked(self):
        h = self.started()
        path = self.root / "source.xlsx"
        path.write_text("data", encoding="utf-8")
        past = time.time() - 3600
        os.utime(path, (past, past))
        self.assertTrue(self.done(h, 1, files=["source.xlsx"])["ok"])
        self.assertEqual(self.plan(h).tasks[0].checks[0]["state"], "old")

    def test_without_folders_files_is_ignored_and_said(self):
        h = self.handler()
        self.approved(h, checks=None)
        self.start(h)
        res = self.done(h, 1, files=["nope.xlsx"])
        self.assertTrue(res["ok"])
        self.assertTrue(any("does not check files" in n for n in res["input_notes"]))
        self.assertEqual(self.plan(h).tasks[0].checks, [])

    def test_the_model_never_sees_what_the_server_found(self):
        h = self.started()
        (self.root / "q4.xlsx").write_text("data", encoding="utf-8")
        res = self.done(h, 1, files=["q4.xlsx"])
        back = h.dispatch("get_current_plan", {"plan_id": res["plan_id"]})
        self.assertNotIn("checks", json.dumps([res, back]))

    def test_a_refusal_that_keeps_repeating_reaches_the_human(self):
        """Bounded by the 1.16 breaker: the same refused call, repeated, halts the plan."""
        h = self.started()
        codes = [self.done(h, 1, files=["nope.xlsx"]).get("error_code") for _ in range(5)]
        self.assertEqual(codes[:2], ["FILE_NOT_FOUND"] * 2)
        self.assertIn("LOOP_HALTED", codes)
        self.assertEqual(self.plan(h).tasks[0].status, "IN_PROGRESS")


class TestCompletionReport(ContractCase):
    def finished(self, ui=None, **cfg):
        h = self.handler(ui, files=True, **cfg)
        self.think(h)
        self.ask(h)
        if ui is None:
            h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.start(h)
        self.done(h, 1)
        (self.root / "pivot.xlsx").write_text("data", encoding="utf-8")
        self.done(h, 2, REAL, files=["pivot.xlsx"])
        self.done(h, 3, "5줄 요약을 채팅에 적었습니다: 매출은 전 분기 대비 4% 증가 ...")
        return h

    def test_the_page_row_carries_criterion_evidence_and_checks(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.finished(ui, auto_ask=False)
        ui.decision = None
        self.ask(h)
        entry = ui.opened[-1]
        self.assertEqual(entry["phase"], PHASE_COMPLETION)
        row = entry["tasks"][1]
        self.assertEqual(row["done_when"], CHECK)
        self.assertEqual(row["result_log"], REAL)
        self.assertEqual([(c["path"], c["state"]) for c in row["checks"]],
                         [("pivot.xlsx", "found")])
        self.assertIn("duration_sec", row)
        self.assertNotIn("checks", entry["tasks"][0])

    def test_the_chat_report_says_what_the_server_found(self):
        h = self.finished()
        text = self.ask(h)["display_to_user"]
        self.assertIn(f"   태스크 완료 기준: {CHECK}", text)
        self.assertIn("   서버 확인: ✔ pivot.xlsx · 4 B · 이 태스크 중 생성/변경됨", text)

    def test_a_file_removed_since_is_said_before_the_human_certifies(self):
        h = self.finished()
        (self.root / "pivot.xlsx").unlink()
        text = self.ask(h)["display_to_user"]
        self.assertIn("보고 당시에는 있었으나 지금은 없음", text)
        check = self.plan(h).tasks[1].checks[0]
        self.assertEqual((check["state"], check["gone"]), ("missing", True))
        self.assertEqual(self.audit("file_gone_before_completion")[-1]["task_id"], 2)

    def test_reading_the_report_does_not_void_the_request(self):
        """The human opening and saving the file changes its size and mtime only."""
        h = self.finished()
        self.ask(h)
        before = PlanningHandlers._fingerprint(self.plan(h))
        (self.root / "pivot.xlsx").write_text("data, opened and saved", encoding="utf-8")
        self.ask(h)
        self.assertEqual(PlanningHandlers._fingerprint(self.plan(h)), before)

    def test_what_the_server_found_is_part_of_what_was_approved(self):
        h = self.finished()
        self.ask(h)
        before = PlanningHandlers._fingerprint(self.plan(h))
        (self.root / "pivot.xlsx").unlink()
        self.ask(h)
        self.assertNotEqual(PlanningHandlers._fingerprint(self.plan(h)), before)

    def test_a_task_sent_back_starts_clean_but_keeps_its_criterion(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.finished(ui, auto_ask=False)
        ui.decision, ui.task_comments, ui.scope = "REVISE", {"2": "3분기가 빠졌습니다"}, "TASKS"
        self.ask(h)
        task = self.plan(h).tasks[1]
        self.assertEqual((task.status, task.checks, task.files), ("PENDING", [], []))
        self.assertEqual(task.done_when, CHECK)


# ---------------------------------------------------------------------------
# The human writes the criterion on the approval page
# ---------------------------------------------------------------------------

MINE = "3분기까지 포함한 합계 표가 pivot.xlsx 로 저장된다"


class TestTheHumanWritesTheCriterion(ContractCase):
    def test_written_and_approved_in_one_click(self):
        """No revision round trip: the task did not change, only what finished means."""
        ui = FakeApprovalUI(decision="APPROVED", criteria={"3": "요약이 정확히 5줄이다"})
        h = self.handler(ui, auto_ask=False)
        self.think(h)
        res = self.ask(h)
        self.assertEqual(res["plan_status"], "APPROVED")
        task = self.plan(h).tasks[2]
        self.assertEqual((task.done_when, task.done_when_by), ("요약이 정확히 5줄이다", "user"))
        self.assertIn("The user set what finished means for task(s) 3", res["message"])
        self.assertEqual(self.audit("criteria_applied")[-1]["changed"],
                         [{"task_id": 3, "from": None, "to": "요약이 정확히 5줄이다"}])

    def test_the_task_is_handed_over_as_the_users(self):
        ui = FakeApprovalUI(decision="APPROVED", criteria={"2": MINE})
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        self.start(h)
        res = self.done(h, 1)
        self.assertEqual((res["next_task"]["done_when"], res["next_task"]["done_when_by"]),
                         (MINE, "user"))
        self.assertIn(f'The user set what finished means: "{MINE}"', res["next_action_hint"])

    def test_the_wording_it_replaced_never_reaches_the_model_again(self):
        ui = FakeApprovalUI(decision="APPROVED", criteria={"2": MINE})
        h = self.handler(ui)
        self.think(h)
        seen = [self.ask(h), self.start(h)]
        for tid in (1, 2, 3):
            seen.append(self.done(h, tid, f"{REAL} ({tid})"))
            seen.append(h.dispatch("get_current_plan", {"plan_id": seen[0]["plan_id"]}))
        ui.decision = None
        seen.append(self.ask(h))
        text = json.dumps(seen, ensure_ascii=False)
        self.assertIn(MINE, text)
        self.assertNotIn(CHECK, text)

    def test_erasing_it_removes_the_criterion(self):
        ui = FakeApprovalUI(decision="APPROVED", criteria={"2": ""})
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        task = self.plan(h).tasks[1]
        self.assertEqual((task.done_when, task.done_when_by), (None, None))

    def test_unchanged_text_is_not_a_change(self):
        ui = FakeApprovalUI(decision="APPROVED", criteria={"2": CHECK})
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        self.assertIsNone(self.plan(h).tasks[1].done_when_by)
        self.assertEqual(self.audit("criteria_applied"), [])

    def test_a_late_click_carries_it_too(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        ui.resolve("APPROVED", criteria={"2": MINE})
        h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(self.plan(h).tasks[1].done_when, MINE)

    def test_it_is_cut_to_the_configured_length(self):
        ui = FakeApprovalUI(decision="APPROVED", criteria={"2": "가" * 300})
        h = self.handler(ui, max_done_when_chars=50)
        self.think(h)
        self.ask(h)
        self.assertEqual(len(self.plan(h).tasks[1].done_when), 50)

    def test_the_model_has_no_way_to_set_one_at_approval(self):
        """No relay field exists - unlike choices, even in chat mode."""
        h = self.handler()
        self.think(h)
        self.ask(h)
        res = h.dispatch("request_user_approval",
                         {"decision": "APPROVED", "criteria": {"2": "모델이 정한 기준"},
                          "_page_criteria": {"2": "모델이 정한 기준"}})
        self.assertEqual(res["plan_status"], "APPROVED")
        task = self.plan(h).tasks[1]
        self.assertEqual((task.done_when, task.done_when_by), (CHECK, None))
        for tool in ({}, {"blocking": False}, {"approval_mode": "return"}):
            props = {t["name"]: t for t in build_tool_definitions(**tool)}[
                "request_user_approval"]["inputSchema"]["properties"]
            self.assertNotIn("criteria", props)

    def test_even_with_the_models_field_off_the_human_may_write_one(self):
        ui = FakeApprovalUI(decision="APPROVED", criteria={"2": MINE})
        h = self.handler(ui, done_when=False)
        self.think(h)
        self.ask(h)
        self.assertEqual(self.plan(h).tasks[1].done_when, MINE)


class TestCriteriaTypedBeforeAskingForChanges(ContractCase):
    """What the human typed is theirs whichever button they then press (D22)."""

    def test_a_targeted_revision_keeps_them_on_the_tasks(self):
        ui = FakeApprovalUI(decision="REVISE", task_comments={"3": "7줄로"}, scope="TASKS",
                            criteria={"2": MINE, "3": "요약이 정확히 7줄이다"})
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        tasks = self.plan(h).tasks
        self.assertEqual((tasks[1].done_when, tasks[1].done_when_by), (MINE, "user"))
        res = h.dispatch("plan_and_think", {
            "goal": GOAL, "need_more_thinking": False, "thought": "고침",
            "task_updates": [{"task_id": 3, "title": "7줄 요약 작성"}],
            "done_when": [{"task_id": 3, "check": "모델이 다시 쓴 기준이다"}]})
        task = self.plan(h).tasks[2]
        self.assertEqual((task.title, task.done_when, task.done_when_by),
                         ("7줄 요약 작성", "요약이 정확히 7줄이다", "user"))
        self.assertTrue(any("wrote the done_when" in n for n in res["input_notes"]))

    def test_a_whole_plan_revision_carries_them_in_the_comment(self):
        ui = FakeApprovalUI(decision="REVISE", comment="순서를 바꿔 주세요",
                            criteria={"2": MINE})
        h = self.handler(ui)
        self.think(h)
        res = self.ask(h)
        self.assertEqual(res["revision_scope"], "PLAN")
        self.assertIn("순서를 바꿔 주세요", res["user_comment"])
        self.assertIn(f"[태스크 완료 기준] 2번 '{TASKS[1]}': {MINE}", res["user_comment"])

    def test_a_rewritten_task_drops_the_models_own_criterion(self):
        """It described the old wording - like the options in 2.0."""
        ui = FakeApprovalUI(decision="REVISE", task_comments={"2": "피벗 말고 스크립트로"},
                            scope="TASKS")
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        h.dispatch("plan_and_think", {
            "goal": GOAL, "need_more_thinking": False, "thought": "고침",
            "task_updates": [{"task_id": 2, "title": "스크립트로 분기별 매출 집계"}]})
        self.assertIsNone(self.plan(h).tasks[1].done_when)

    def test_unless_it_sends_a_new_one_with_the_rewrite(self):
        ui = FakeApprovalUI(decision="REVISE", task_comments={"2": "피벗 말고 스크립트로"},
                            scope="TASKS")
        h = self.handler(ui)
        self.think(h)
        self.ask(h)
        h.dispatch("plan_and_think", {
            "goal": GOAL, "need_more_thinking": False, "thought": "고침",
            "task_updates": [{"task_id": 2, "title": "스크립트로 분기별 매출 집계"}],
            "done_when": [{"task_id": 2, "check": "합계 4행이 sums.csv 로 저장된다"},
                          {"task_id": 1, "check": "건드리면 안 되는 태스크의 기준"}]})
        tasks = self.plan(h).tasks
        self.assertEqual(tasks[1].done_when, "합계 4행이 sums.csv 로 저장된다")
        self.assertIsNone(tasks[0].done_when)


class TestStoreRecordsOnlyWhatWasShown(ContractCase):
    ROWS = [{"task_id": 1, "title": "a", "status": "DONE"},
            {"task_id": 2, "title": "b", "status": "PENDING", "done_when": "x"}]

    def store(self, phase=PHASE_PLAN):
        s = ApprovalStore(self.state_dir)
        return s, s.publish("p", "g", "d", self.ROWS, "fp", phase)

    def test_a_criterion_for_a_task_on_screen_is_recorded(self):
        s, rid = self.store()
        self.assertTrue(s.record_decision(rid, "APPROVED", "", criteria={"2": "  새   기준 "}))
        self.assertEqual(s.claim(rid).criteria, {"2": "새 기준"})

    def test_one_for_anything_else_records_nothing(self):
        for bad in ({"9": "x"}, {"1": "끝난 태스크"}, {"2": 5}, {"x": "y"}, "text"):
            s, rid = self.store()
            self.assertFalse(s.record_decision(rid, "APPROVED", "", criteria=bad), bad)
            self.assertIsNone(s.entry(rid)["decision"])

    def test_it_travels_with_a_request_for_changes(self):
        s, rid = self.store()
        self.assertTrue(s.record_decision(rid, "REVISE", "고쳐 주세요", criteria={"2": "y"}))
        self.assertEqual(s.claim(rid).criteria, {"2": "y"})

    def test_not_with_a_rejection_a_completion_report_or_a_halt(self):
        for phase, decision in ((PHASE_PLAN, "REJECTED"), (PHASE_COMPLETION, "APPROVED"),
                                (PHASE_HALT, "APPROVED")):
            s, rid = self.store(phase)
            self.assertTrue(s.record_decision(rid, decision, "", criteria={"9": "무엇이든"}))
            self.assertEqual(s.claim(rid).criteria, {}, (phase, decision))

    def test_an_entry_decided_by_an_older_page_has_none(self):
        s, rid = self.store()
        self.assertTrue(s.record_decision(rid, "APPROVED", ""))
        record = s.read()
        del record["requests"][0]["criteria"]
        s._write(record)
        self.assertEqual(s.claim(rid).criteria, {})

    def test_the_pure_validator(self):
        self.assertEqual(validate_page_criteria(self.ROWS, None), {})
        self.assertEqual(validate_page_criteria(self.ROWS, {"2": ""}), {"2": ""})
        self.assertIsNone(validate_page_criteria(self.ROWS, {"1": "x"}))
        self.assertEqual(len(validate_page_criteria(self.ROWS, {"2": "가" * 900})["2"]), 500)


class TestFingerprint(ContractCase):
    def fp(self, **task):
        plan = Plan(plan_id="p", goal="g", tasks=[Task(1, "a", **task)])
        return PlanningHandlers._fingerprint(plan)

    def test_the_criterion_is_part_of_what_was_approved(self):
        self.assertNotEqual(self.fp(), self.fp(done_when=CHECK))
        self.assertNotEqual(self.fp(done_when=CHECK), self.fp(done_when=MINE))
        self.assertNotEqual(self.fp(done_when=CHECK), self.fp(done_when=CHECK, done_when_by="user"))

    def test_so_is_what_the_server_found_but_not_size_or_time(self):
        found = {"path": "a.txt", "state": "found", "size": 1, "mtime": "t1"}
        self.assertNotEqual(self.fp(), self.fp(checks=[found]))
        self.assertNotEqual(self.fp(checks=[found]),
                            self.fp(checks=[{**found, "state": "missing"}]))
        self.assertEqual(self.fp(checks=[found]),
                         self.fp(checks=[{**found, "size": 99, "mtime": "t2"}]))
        self.assertNotEqual(self.fp(), self.fp(claims_withdrawn=["a.txt"]))


# ---------------------------------------------------------------------------
# Drafts and the halt card
# ---------------------------------------------------------------------------


class TestDraftCriteria(ContractCase):
    def test_kept_with_the_draft(self):
        h = self.handler()
        res = self.think(h, more=True)
        self.assertEqual(self.plan(h).draft_done_when, {"2": CHECK})
        self.assertEqual(res["draft_saved"], 3)

    def test_a_final_call_with_no_list_uses_them(self):
        h = self.handler()
        self.think(h, more=True)
        self.think(h, step=2, tasks=False, checks=None)
        self.assertEqual(self.plan(h).tasks[1].done_when, CHECK)
        self.assertEqual(self.plan(h).draft_done_when, {})

    def test_a_new_draft_replaces_them(self):
        """They number into the list they came with; a new list makes them wrong."""
        h = self.handler()
        self.think(h, more=True)
        self.think(h, step=2, more=True, tasks=["다른 계획", "둘째"], checks=None)
        self.assertEqual(self.plan(h).draft_done_when, {})

    def test_sent_on_their_own_they_join_the_draft(self):
        h = self.handler()
        self.think(h, more=True, checks=None)
        self.think(h, step=2, more=True, tasks=False)
        self.assertEqual(self.plan(h).draft_done_when, {"2": CHECK})

    def test_a_draft_the_server_submits_carries_them(self):
        ui = FakeApprovalUI(decision="APPROVED")
        h = self.handler(ui, max_thinking_steps=2)
        self.think(h, more=True)
        self.think(h, step=2, more=True, tasks=False, checks=None)
        self.assertEqual(self.audit("auto_finalized")[-1]["tasks"], 3)
        self.assertEqual(ui.opened[-1]["tasks"][1]["done_when"], CHECK)
        self.assertEqual(self.plan(h).tasks[1].done_when, CHECK)

    def test_the_halt_card_shows_and_keeps_them(self):
        ui = FakeApprovalUI(decision=None)
        h = self.handler(ui, breaker_repeat=2)
        self.think(h, more=True)
        res = None
        for _ in range(4):
            res = self.think(h, step=2, more=True)
            if res.get("error_code") == "LOOP_HALTED":
                break
        self.assertEqual(res["error_code"], "LOOP_HALTED")
        entry = ui.opened[-1]
        self.assertEqual(entry["phase"], PHASE_HALT)
        self.assertEqual(entry["tasks"][1]["done_when"], CHECK)
        ui.resolve("APPROVED")
        h.dispatch("get_current_plan", {"plan_id": "current"})
        plan = self.plan(h)
        self.assertEqual(plan.plan_status, "APPROVED")
        self.assertEqual(plan.tasks[1].done_when, CHECK)

    def test_a_draft_without_criteria_keeps_its_2_0_halt_fingerprint(self):
        h = self.handler()
        self.think(h, more=True, checks=None)
        plan = self.plan(h)
        plan.halt = {"id": "h1"}
        payload = "\x00".join(
            [PlanningHandlers._fingerprint(plan), "h1"] + plan.halt_draft()
            + [json.dumps([plan.draft_alternatives, plan.draft_reasons],
                          ensure_ascii=False, sort_keys=True)])
        self.assertEqual(h._request_fingerprint(plan),
                         hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16])


# ---------------------------------------------------------------------------
# What is advertised
# ---------------------------------------------------------------------------


class TestSchema(unittest.TestCase):
    @staticmethod
    def tools(**kw):
        return {t["name"]: t for t in build_tool_definitions(**kw)}

    def test_done_when_is_offered_in_both_profiles(self):
        for profile in ("standard", "reasoning"):
            props = self.tools(model_profile=profile)["plan_and_think"]["inputSchema"][
                "properties"]
            self.assertIn("done_when", props, profile)
            self.assertEqual(props["done_when"]["items"]["required"], ["task_id", "check"])

    def test_turned_off_nothing_mentions_it(self):
        tools = self.tools(done_when=False)
        text = json.dumps(tools)
        self.assertNotIn("done_when", text)

    def test_files_is_offered_only_where_the_server_looks(self):
        self.assertNotIn("files", self.tools()["update_task_progress"]["inputSchema"][
            "properties"])
        with_checks = self.tools(file_checks=True)["update_task_progress"]
        self.assertIn("files", with_checks["inputSchema"]["properties"])
        self.assertIn("refuses DONE for a file it cannot find", with_checks["description"])
        self.assertNotIn("cannot find", self.tools()["update_task_progress"]["description"])

    def test_the_reasoning_model_is_told_to_write_the_check_down_not_re_check(self):
        text = self.tools(model_profile="reasoning")["plan_and_think"]["description"]
        self.assertIn("Write the check in\ndone_when", text)
        self.assertNotIn("double-check", self.tools()["plan_and_think"]["description"])
        self.assertNotIn(
            "double-check",
            self.tools(model_profile="reasoning", done_when=False)["plan_and_think"][
                "description"])

    def test_turned_off_none_of_the_3_0_text_is_advertised(self):
        """The off switches must really turn the text off, not just the behaviour."""
        text = json.dumps(self.tools(done_when=False, local_repair=False), ensure_ascii=False)
        for word in ("done_when", "files", "failed, the server names", "IF A TASK FAILED",
                     "or a task failed"):
            self.assertNotIn(word, text)

    def test_the_new_fields_cost_what_was_measured(self):
        """Every word here is sent with every request (docs/context-budget-analysis.md)."""
        base = tool_cost(done_when=False, local_repair=False)
        self.assertLess(tool_cost() - base, 360)
        self.assertLess(tool_cost(file_checks=True) - base, 480)


def tool_cost(**kw) -> int:
    """The estimate of docs/context-budget-analysis.md: compact JSON bytes // 3."""
    return sum(len(json.dumps(t, ensure_ascii=False).encode()) // 3
               for t in build_tool_definitions(**kw))


class TestToolTextIsLean(unittest.TestCase):
    """The 3.0.0 trim: each rule said once, and none of them lost.

    The tool text grew one field failure at a time until "one task per call" was said
    three times and the plan_id rule twice. Trimming it took 3.0 - with every feature
    on - below what 2.0 cost. These tests hold both halves of that: a ceiling, so the
    text cannot quietly grow back, and the rules themselves, so a later trim cannot
    quietly cut one. None of it has been measured on the corporate model.
    """

    MODES = [
        dict(model_profile=p, approval_mode=m, blocking=b, auto_advance=a, file_checks=f)
        for p in ("standard", "reasoning")
        for m, b in (("chunked", True), ("return", True), ("chunked", False))
        for a in (True, False)
        for f in (True, False)
    ]

    @staticmethod
    def tools(**kw):
        return {t["name"]: t for t in build_tool_definitions(**kw)}

    def test_3_0_with_everything_on_costs_less_than_2_0_did(self):
        """2.0.0 advertised 3,903 estimated tokens (standard) and 3,703 (reasoning)."""
        self.assertLess(tool_cost(), 3550)
        self.assertLess(tool_cost(file_checks=True), 3650)
        self.assertLess(tool_cost(model_profile="reasoning", file_checks=True), 3500)

    def test_every_parameter_still_shows_what_to_send(self):
        """Phase 1: every parameter carries a concrete example. The one exception names
        the single value it takes."""
        for mode in self.MODES:
            for name, tool in self.tools(**mode).items():
                for param, prop in tool["inputSchema"]["properties"].items():
                    text = prop["description"]
                    shown = "Example" in text or (param == "decision" and '"ASK_USER"' in text)
                    self.assertTrue(shown, (name, param, mode))

    def test_an_array_of_objects_shows_its_whole_shape_once(self):
        """The example sits on the array, where a weak model copies it from; the nested
        fields do not repeat it."""
        props = self.tools()["plan_and_think"]["inputSchema"]["properties"]
        for name, keys in (("task_updates", ("task_id", "title")),
                           ("alternatives", ("task_id", "title", "reason", "topic")),
                           ("recommended_reasons", ("task_id", "reason")),
                           ("done_when", ("task_id", "check"))):
            example = props[name]["description"].split("Example:", 1)[1]
            for key in keys:
                self.assertIn(f'"{key}"', example, (name, key))
            for nested in props[name]["items"]["properties"].values():
                self.assertNotIn("Example", nested.get("description", ""), name)

    def execution_text(self, **kw) -> str:
        """The description as one line - where a sentence wraps is not a rule."""
        return " ".join(self.tools(**kw)["update_task_progress"]["description"].split())

    def test_the_execution_rules_are_all_still_there(self):
        for auto in (True, False):
            text = self.execution_text(auto_advance=auto)
            for rule in ("One task per call", "next_task", "ONLY place", "never reuse",
                         '"IN_PROGRESS"', '"DONE"', '"FAILED"', "result_log", "next_action",
                         "REFUSES", "skips an unfinished earlier task", "no tasks remain",
                         "Never mark a task DONE before you actually did it"):
                self.assertIn(rule, text, (auto, rule))
        auto = self.execution_text()
        self.assertIn("not the one in progress", auto)
        self.assertIn("get_current_plan", auto)
        self.assertIn("never count tasks yourself", auto)
        self.assertIn("never marked IN_PROGRESS first", self.execution_text(auto_advance=False))

    def test_each_execution_rule_is_said_once(self):
        text = self.execution_text(file_checks=True)
        for phrase in ("FAILED", "One task per call", "get_current_plan", "ONLY place",
                       "REFUSES", "Never mark a task DONE"):
            self.assertEqual(text.count(phrase), 1, phrase)

    def test_recovery_asks_for_the_plan_id_before_it_offers_current(self):
        """1.15.1: a description that led with "current" made a model fork its plan."""
        tool = self.tools()["get_current_plan"]
        text = tool["description"]
        self.assertLess(text.index("plan_id"), text.index('"current"'))
        self.assertIn("changes nothing", text)
        self.assertIn("list of plans", text)
        param = tool["inputSchema"]["properties"]["plan_id"]["description"]
        self.assertLess(param.index("plan_id"), param.index('"current"'))

    def test_plan_id_is_explained_once_per_tool(self):
        for name in ("plan_and_think", "request_user_approval", "update_task_progress"):
            text = self.tools()[name]["inputSchema"]["properties"]["plan_id"]["description"]
            self.assertLess(len(text), 130, name)
            self.assertIn("unless the server asks for it", text)

    def test_the_planning_rules_are_all_still_there(self):
        props = self.tools()["plan_and_think"]["inputSchema"]["properties"]
        self.assertIn("SAME text on every", props["goal"]["description"])
        self.assertIn("revised_goal", props["goal"]["description"])
        self.assertIn("Do NOT use it to reword", props["revised_goal"]["description"])
        for rule in ("REQUIRED when need_more_thinking is false", "Strings only",
                     "the server assigns task_id"):
            self.assertIn(rule, props["task_list"]["description"].replace("\n", " "))
        for rule in ("ONLY use this when the server asks for it", "Rewrite JUST those tasks",
                     "Not together with task_list", "add, delete or reorder"):
            self.assertIn(rule, props["task_updates"]["description"])
        self.assertIn("REQUIRED with DONE", self.tools()["update_task_progress"][
            "inputSchema"]["properties"]["result_log"]["description"])
        self.assertIn("is refused", self.tools()["update_task_progress"][
            "inputSchema"]["properties"]["result_log"]["description"])


class TestPageTemplate(unittest.TestCase):
    def test_the_page_can_write_a_criterion_and_send_it(self):
        for piece in ("criteria:criteriaOf(id)", "function criteriaOf(id)", "function dwRow(",
                      "data-orig=", "'dw'+", "row+=dwRow(d,t,editable)"):
            self.assertIn(piece, _PAGE, piece)

    def test_the_criterion_row_is_named_for_what_it_is(self):
        """The label says whose criterion it is, the button says what it adds, and the
        field carries no sample sentence - the label beside it already says what goes
        in, and a grey example read as if something had been filled in."""
        self.assertIn('<span class="dwl">태스크 완료 기준</span>', _PAGE)
        self.assertIn(">완료 기준 추가</button>", _PAGE)
        self.assertIn("[완료 기준 추가] 또는 [수정]으로", _PAGE)
        self.assertIn("번 태스크 완료 기준\">", _PAGE)
        row = _PAGE[_PAGE.index("function dwRow("):_PAGE.index("// ---- what the server found")]
        self.assertNotIn("placeholder", row)
        self.assertNotIn("이 태스크가 끝났다고 볼 기준", _PAGE)

    def test_the_comment_box_is_labelled_and_carries_no_sample_sentence(self):
        """Like the criterion: the label beside the box is the name of the button that
        opens it (의견, or 다시 작업 on a completion report), so nothing is written inside."""
        self.assertIn("const what=done?'다시 작업':'의견';", _PAGE)
        self.assertIn("<div class=\"tcrow\"><span class=\"dwl\">'+what+'</span><textarea "
                      "class=\"tc\"", _PAGE)
        self.assertIn("번 '+what+'\"></textarea></div>'", _PAGE)
        rows = _PAGE[_PAGE.index("function taskRows(d)"):_PAGE.index("function chip(d)")]
        self.assertNotIn("placeholder", rows)
        for gone in ("해당 태스크에 대한 의견을 입력해 주십시오",
                     "해당 태스크의 재작업 요청 사항을 입력해 주십시오"):
            self.assertNotIn(gone, _PAGE)

    def test_the_add_button_toggles_an_empty_criterion_field(self):
        """Open and still empty, the button closes it again - like 의견. With something
        typed it stays open: that text travels with the approval."""
        self.assertIn("function toggleCriterion(btn,req,tid)", _PAGE)
        self.assertIn("if(row&&row.classList.contains('edit')&&!dwValue(i)){", _PAGE)
        self.assertIn("row.classList.remove('edit');row.classList.add('hid');", _PAGE)
        self.assertIn('<button class="tcbtn dwadd" type="button" aria-expanded="false" ', _PAGE)
        self.assertIn("'onclick=\"toggleCriterion(this,", _PAGE)
        # 수정, on a criterion that already exists, only ever opens.
        self.assertIn("'<button class=\"dwbtn\" type=\"button\" onclick=\"editCriterion(this,",
                      _PAGE)

    def test_each_button_shows_the_state_of_its_own_field(self):
        """Both buttons of a row are .tcbtn; the one that opens the comment box must not
        be found by 'the first .tcbtn', which is 완료 기준 추가 when there is one."""
        self.assertEqual(_PAGE.count("task.querySelector('.tcbtn:not(.dwadd)')"), 2)
        self.assertNotIn("task.querySelector('.tcbtn')", _PAGE)
        self.assertIn("if(add)add.setAttribute('aria-expanded','true');", _PAGE)

    def test_everything_under_a_task_title_starts_on_one_line(self):
        """Each row under a title has its own font size, so an indent in em gave each its
        own left edge - the criterion label and the comment label sat 4px apart. One
        length in rem, said once."""
        css = _PAGE[:_PAGE.index("</style>")]
        self.assertIn("--sub:1.88rem", css)
        for selector in (".was{", ".note{", ".ev{", ".tcrow{", ".opts{", ".dw{", ".ck{"):
            at = css.index(selector)
            self.assertIn("0 0 var(--sub)", css[at:css.index("}", at)], selector)
        self.assertNotIn("0 0 1.95em", css)  # no rule still uses the old em indent

    def test_no_approve_button_while_a_comment_is_written(self):
        """An approval carries no comment, so one typed and then approved was dropped
        unread - and, on a completion report, a request to redo a task was lost while
        the plan closed. The button is hidden while any comment box of the request has
        text, and comes back when all of them are empty."""
        self.assertIn("function hasOpinion(id){", _PAGE)
        self.assertIn("return !!(all&&all.value.trim())||Object.keys(comments(id)).length>0;",
                      _PAGE)
        self.assertIn("b.hidden=PHASE[id]!=='HALT'&&hasOpinion(id);", _PAGE)
        # Decided on every keystroke, and again whenever a card is drawn from its drafts.
        self.assertIn("dset(id,'_all',el.value);", _PAGE)
        self.assertIn("relabel(d.id);", _PAGE)

    def test_the_revise_button_says_what_becomes_of_a_criterion(self):
        """Applied to the tasks when only some are rewritten; handed to the agent in the
        comment when the whole plan is, since no task is left to carry it."""
        self.assertIn("function revLabel(phase,ids,whole,crit){", _PAGE)
        self.assertIn("if(crit&&!done)label+=' · 태스크 완료 기준 '+crit+'건 '+(all?'전달':'반영');",
                      _PAGE)
        self.assertIn("Object.keys(criteriaOf(id)).length);", _PAGE)

    def test_the_small_row_buttons_look_like_buttons(self):
        """의견, 완료 기준 추가 and 수정 were flat grey at 65% opacity and were missed.
        They are bold and slightly raised, in both colour schemes."""
        start = _PAGE.index(".tcbtn,.dwbtn{")
        rule = _PAGE[start:_PAGE.index("}", start)]
        for piece in ("font-weight:700", "border:1px solid", "linear-gradient(",
                      "box-shadow:0 1px 2px"):
            self.assertIn(piece, rule, piece)
        css = _PAGE[:_PAGE.index("</style>")]
        for selector in (".tcbtn{", ".dwbtn{", ".tcbtn,.dwbtn{"):
            at = css.index(selector)
            self.assertNotIn("opacity", css[at:css.index("}", at)], selector)
        self.assertIn('.tcbtn[aria-expanded="true"]', css)
        dark = css[css.index("@media(prefers-color-scheme:dark){\n  .tcbtn,.dwbtn{"):]
        self.assertIn("linear-gradient(#3b414b,#2a2e36)", dark[:400])
        # Only the button whose comment is filled turns amber - not the one beside it.
        self.assertIn(".task.filled .tcbtn:not(.dwadd){", css)

    def test_the_criterion_has_one_name_wherever_the_user_reads_it(self):
        """Page and chat text alike say 태스크 완료 기준. The one place the shorter form
        stays is the button that adds one, which the user named 완료 기준 추가."""
        import re
        from planning import handlers, responses
        bare = re.compile(r"(?<!태스크 )완료 기준(?! 추가)")
        self.assertEqual(bare.findall(_PAGE), [])
        for module in (responses, handlers):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertEqual(bare.findall(source), [], module.__name__)

    def test_the_approve_button_says_what_it_carries(self):
        self.assertIn("' · 태스크 완료 기준 '+crit+'건 반영'", _PAGE)
        self.assertIn("' · 변경 '+(changed.length+crit)+'건 반영'", _PAGE)

    def test_a_finished_task_gets_no_editor(self):
        self.assertIn("const editable=!done&&t.status!=='DONE';", _PAGE)

    def test_the_completion_row_shows_what_the_server_found(self):
        for piece in ("row+=checksHtml(t);", "function checkLine(c)", "function triage(d)",
                      "이 태스크 중 생성/변경됨", "작업 전부터 있던 파일", "찾을 수 없음",
                      "보고 당시에는 있었으나 지금은 없음", "서버의 확인 범위 밖",
                      "에이전트의 보고가 근거입니다", "소요 ", "거의 그대로 반복합니다",
                      "결과 파일로 적었다가 뺐습니다", "function producedCheck(c)"):
            self.assertIn(piece, _PAGE, piece)

    def test_the_page_and_the_chat_text_word_a_check_the_same_way(self):
        for state in ("found", "old", "folder", "empty", "missing", "unknown", "outside"):
            line = evidence.describe_check({"path": "a.txt", "state": state})
            self.assertIn(line.split(" · ", 1)[1].split(" (")[0], _PAGE, state)

    def test_the_halt_card_shows_the_criterion_read_only(self):
        self.assertIn("(d.draft?optionsHtml(d,t,false):'')+dwRow(d,t,false)+", _PAGE)

    def test_everything_typed_is_escaped(self):
        self.assertIn("esc(t.done_when||'')", _PAGE)
        self.assertIn("esc(t.failure_note)", _PAGE)
        self.assertIn("esc(c.path)", _PAGE)


class TestVersionOnThePage(ContractCase):
    """The approval page says which planning-mcp is serving it - on the idle screen, a
    plan request and a completion report alike.

    After an upgrade the question "is the new version actually running?" had no answer
    on screen: a process keeps the code it imported until it is restarted, so files on
    disk say nothing about the page in the browser.
    """

    def test_the_served_page_carries_the_version(self):
        html = page_html()
        self.assertIn(f'<span class="vt">planning-mcp {SERVER_VERSION}</span>', html)
        self.assertIn(f"const VERSION='{SERVER_VERSION}';", html)
        self.assertNotIn("__PLANNING_MCP_", html)
        # The label, the script constant, and the information dialog.
        self.assertEqual(_PAGE.count("__PLANNING_MCP_VERSION__"), 3)

    def test_it_is_outside_the_part_the_page_redraws(self):
        """Idle, plan, completion and halt are all drawn inside #root. The label is one
        element above it, so it is the same in every state and no redraw can drop it."""
        html = page_html()
        self.assertLess(html.index('<div class="ver">'),
                        html.index('<div class="card" id="root">'))
        script = html[html.index("<script>"):]
        self.assertNotIn('class="ver"', script)

    def test_it_is_small(self):
        rule = _PAGE[_PAGE.index(".ver{"):_PAGE.index("}", _PAGE.index(".ver{"))]
        self.assertIn("font-size:.72rem", rule)
        self.assertIn(".vt{opacity:.45}", _PAGE)

    def test_only_version_characters_are_filled_in(self):
        """The value lands in HTML text and in a JavaScript string."""
        html = page_html("9.9<script>alert(1)</script>'")
        self.assertIn('<span class="vt">planning-mcp 9.9scriptalert1script</span>', html)
        self.assertIn("const VERSION='9.9scriptalert1script';", html)
        self.assertIn("planning-mcp ?<", page_html("<>"))

    def test_a_request_records_which_version_asked(self):
        store = ApprovalStore(self.state_dir)
        rid = store.publish("p", "g", "d", [], "fp", PHASE_PLAN)
        self.assertEqual(store.entry(rid)["server_version"], SERVER_VERSION)

    def test_the_page_says_so_when_another_version_asked(self):
        """Silent when the versions agree; a line on the card when they do not, or when
        the asking process is too old to say."""
        self.assertIn("if(d.version===VERSION)return '';", _PAGE)
        self.assertIn("버전을 남기지 않는 이전 버전입니다", _PAGE)
        self.assertIn("esc(d.version)", _PAGE)
        # Its definition, and the three places a request is drawn: the plan / completion
        # header, the halt card, and the fallback form for an entry with no phase.
        self.assertEqual(_PAGE.count("verNote(d)"), 4)

    def test_over_http_on_the_idle_page_and_with_a_request(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = ApprovalServer(ApprovalStore(self.state_dir), port=port, open_browser=False)
        if not server.start():
            self.skipTest("could not bind a local port")
        try:
            idle = urlopen(server.url, timeout=5).read().decode("utf-8")
            self.assertIn(f'<span class="vt">planning-mcp {SERVER_VERSION}</span>', idle)
            self.assertIn("현재 대기 중인 승인 요청이 없습니다", idle)
            self.assertIn(f'<p><span class="k">Email:</span> {SERVER_AUTHOR_EMAIL}</p>', idle)
            server.open_request("p", "g", "d", [{"task_id": 1, "title": "a",
                                                  "status": "PENDING"}], "fp", PHASE_PLAN, "s")
            pending = json.loads(urlopen(server.url + "api/pending", timeout=5).read())
            self.assertEqual(pending["requests"][0]["version"], SERVER_VERSION)
        finally:
            server.shutdown()


class TestInformationDialog(unittest.TestCase):
    """A small "i" beside the version opens a dialog: author, email, version."""

    def test_the_dialog_says_who_and_which_version(self):
        html = page_html()
        dialog = html[html.index('<dialog class="about"'):html.index("</dialog>")]
        self.assertIn('<p><span class="k">Author:</span> Ha, Jaehee</p>', dialog)
        self.assertIn('<p><span class="k">Email:</span> lovesm135@naver.com</p>', dialog)
        self.assertIn(f'<p><span class="k">Version:</span> {SERVER_VERSION}</p>', dialog)
        self.assertEqual((SERVER_AUTHOR, SERVER_AUTHOR_EMAIL),
                         ("Ha, Jaehee", "lovesm135@naver.com"))

    def test_the_icon_is_small_and_named(self):
        html = page_html()
        self.assertIn('<button class="info" id="about-open" type="button" '
                      'aria-label="프로그램 정보" title="정보">i</button>', html)
        rule = _PAGE[_PAGE.index(".info{"):_PAGE.index("}", _PAGE.index(".info{"))]
        for piece in ("width:1.1rem", "height:1.1rem", "min-width:0", "flex:0 0 auto"):
            self.assertIn(piece, rule)

    def test_icon_and_dialog_are_outside_the_part_the_page_redraws(self):
        html = page_html()
        root = html.index('<div class="card" id="root">')
        script = html.index("<script>")
        self.assertLess(html.index('id="about-open"'), root)
        self.assertLess(root, html.index('<dialog class="about"'))
        self.assertLess(html.index("</dialog>"), script)

    def test_a_decision_does_not_disable_it(self):
        """decide() used to disable every button on the page. Only the buttons of the
        card being decided are switched off - that card is the one its redraw rebuilds
        (3.1.0); the icon and the dialog's button never are."""
        self.assertIn("if(node)node.querySelectorAll('button').forEach(b=>b.disabled=true);",
                      _PAGE)
        self.assertNotIn("document.querySelectorAll('button')", _PAGE)
        self.assertNotIn("document.querySelectorAll('#root button')", _PAGE)

    def test_it_opens_closes_and_has_a_fallback(self):
        for piece in ("d.showModal()", "d.close()", "else alert(",
                      "getElementById('about-open').addEventListener('click',aboutOpen)",
                      "getElementById('about-close').addEventListener('click',aboutClose)",
                      "if(ev.target===ev.currentTarget)aboutClose();"):
            self.assertIn(piece, _PAGE, piece)
        # The dialog itself has no padding, so only the backdrop is a click on it.
        rule = _PAGE[_PAGE.index("dialog.about{"):_PAGE.index("}", _PAGE.index("dialog.about{"))]
        self.assertIn("padding:0", rule)

    def test_author_and_email_are_escaped(self):
        html = page_html(author='<b>A & "B"</b>', email="x@y.z<script>")
        self.assertIn("&lt;b&gt;A &amp; &quot;B&quot;&lt;/b&gt;", html)
        self.assertIn("x@y.z&lt;script&gt;", html)
        self.assertNotIn("<b>A", html)


class TestPersistence(unittest.TestCase):
    def test_a_2_0_task_reads_as_under_no_contract(self):
        task = Task.from_dict({"task_id": 1, "title": "a", "status": "DONE", "result_log": "r"})
        self.assertEqual(
            (task.done_when, task.done_when_by, task.files, task.checks, task.file_refusals,
             task.claims_withdrawn, task.failure_note),
            (None, None, [], [], [], [], None))
        self.assertEqual(task.brief(), {"task_id": 1, "title": "a", "status": "DONE",
                                        "result_log": "r"})

    def test_round_trip(self):
        task = Task(2, "b", done_when=CHECK, done_when_by="user", files=["a.txt"],
                    checks=[{"path": "a.txt", "state": "found"}], claims_withdrawn=["x"],
                    file_refusals=["y"], failure_note="실패")
        self.assertEqual(Task.from_dict(task.to_dict()), task)
        plan = Plan(plan_id="p", goal="g", draft_done_when={"2": CHECK})
        self.assertEqual(Plan.from_dict(plan.to_dict()).draft_done_when, {"2": CHECK})

    def test_a_hand_edited_file_cannot_wedge_the_plan(self):
        task = Task.from_dict({"task_id": 1, "title": "a", "files": "oops", "checks": [1, "x"],
                               "claims_withdrawn": {"a": 1}, "done_when": ""})
        self.assertEqual((task.files, task.checks, task.claims_withdrawn, task.done_when),
                         ([], [], [], None))
        plan = Plan.from_dict({"plan_id": "p", "goal": "g", "draft_done_when": ["x"]})
        self.assertEqual(plan.draft_done_when, {})


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = Config(state_dir=Path("."))
        self.assertEqual((cfg.done_when, cfg.max_done_when_chars, cfg.evidence_novelty,
                          cfg.artifact_roots, cfg.file_checks, cfg.local_repair),
                         (True, 200, 0.3, (), False, True))
        self.assertEqual(SERVER_VERSION, "3.1.0")

    def test_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp).resolve() / "a", Path(tmp).resolve() / "b"
            env = {"PLANNING_MCP_DONE_WHEN": "off", "PLANNING_MCP_MAX_DONE_WHEN_CHARS": "80",
                   "PLANNING_MCP_EVIDENCE_NOVELTY": "0.45", "PLANNING_MCP_LOCAL_REPAIR": "false",
                   "PLANNING_MCP_ARTIFACT_ROOTS": f' {a} ; "{b}" ;; {a}'}
            with mock.patch.dict(os.environ, env):
                cfg = Config.from_env()
        self.assertEqual((cfg.done_when, cfg.max_done_when_chars, cfg.evidence_novelty,
                          cfg.local_repair, cfg.artifact_roots, cfg.file_checks),
                         (False, 80, 0.45, False, (a, b), True))

    def test_a_bad_number_keeps_the_default(self):
        for raw in ("abc", "nan", ""):
            with mock.patch.dict(os.environ, {"PLANNING_MCP_EVIDENCE_NOVELTY": raw}):
                self.assertEqual(Config.from_env().evidence_novelty, 0.3, raw)


if __name__ == "__main__":
    unittest.main()
