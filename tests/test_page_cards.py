"""What each card of the approval page shows, rendered from tests/web/fixtures.json.

The page's own JavaScript draws every card here, through node (tests/pagejs.py), and the
tests read the HTML it returns. So they check what the human is shown - the words, the
buttons, what is escaped, what is left out - and not how the source that draws it is
spelled: rewriting how a card is built leaves them alone, and changing what it shows
does not. Skipped where node is missing, as on the target machine.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pagejs  # noqa: E402
from pagejs import action_of, needs_node  # noqa: E402


@needs_node
class CardCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.items = pagejs.by_id(pagejs.fixtures())

    def card(self, key: str, **changes):
        return pagejs.card_of(key, **changes)

    def rows(self, key: str, **changes):
        return pagejs.rows_of(key, **changes)

    def call(self, fn: str, *args):
        return pagejs.page().call(fn, *args)


class TestEveryButtonDoesWhatItSays(CardCase):
    """What a button says and what it does are written in two places of a card; this ties
    them together, whichever way the card wires its clicks."""

    def actions(self, node):
        return [(b.text(), action_of(b)) for b in node.find_all("button")]

    def test_no_button_on_any_card_is_inert(self):
        for key in self.items:
            for button in self.card(key).find_all("button"):
                action_of(button)

    def test_a_request_decides_itself(self):
        for key in ("r1plan", "r2plan", "r3done", "r6legacy"):
            self.assertEqual(self.actions(self.card(key).one("div", "row")),
                             [("승인", ("decide", key, "APPROVED")),
                              ("수정 요청", ("decide", key, "REVISE")),
                              ("거절", ("decide", key, "REJECTED"))])

    def test_a_halt_card_continues_or_cancels(self):
        self.assertEqual(self.actions(self.card("r4halt").one("div", "row")),
                         [("이 초안으로 승인", ("decide", "r4halt", "APPROVED")),
                          ("계속 진행", ("decide", "r4halt", "REVISE")),
                          ("취소", ("decide", "r4halt", "REJECTED"))])
        self.assertEqual(self.actions(self.card("r5user").one("div", "row")),
                         [("계속 진행", ("decide", "r5user", "REVISE")),
                          ("계획 취소", ("decide", "r5user", "REJECTED"))])

    def test_a_task_row_opens_its_own_fields(self):
        rows = self.rows("r1plan")
        self.assertEqual(self.actions(rows[0]),
                         [("완료 기준 추가", ("toggleCriterion", "r1plan", "1")),
                          ("의견", ("toggleComment",)),
                          ("수정", ("editCriterion", "r1plan", "1"))])
        self.assertEqual(self.actions(rows[2]),
                         [("의견", ("toggleComment",)), ("수정", ("editCriterion", "r1plan", "3"))])
        self.assertEqual(self.actions(self.rows("r3done")[0]), [("다시 작업", ("toggleComment",))])

    def test_a_running_plan_is_controlled_by_its_id(self):
        pid = "plan_20261022_0011"
        self.assertEqual(self.actions(self.card("run-" + pid)),
                         [("의견 전달", ("control", pid, "NOTE")),
                          ("멈춤", ("control", pid, "PAUSE")),
                          ("계획 강제 취소", ("control", pid, "CANCEL"))])
        pid = "plan_20261022_0012"
        self.assertEqual(self.actions(self.card("run-" + pid)),
                         [("요청 취소", ("control", pid, "CLEAR")),
                          ("계획 강제 취소", ("control", pid, "CANCEL"))])


class TestAPlanRequest(CardCase):
    def test_the_header_says_what_is_asked_and_why(self):
        c = self.card("r1plan")
        self.assertEqual(c.one("h1").text(), "계획 승인 요청 · plan_20261022_0001")
        goal = c.one("p", "goal")
        self.assertEqual(goal.one("span", "lbl").text(), "목표")
        self.assertEqual(goal.text(), "목표3분기 매출 보고서를 요약해 팀장에게 메일로 보낸다")
        summary = c.one("div", "summary")
        self.assertEqual(summary.one("span", "lbl").text(), "개요")
        self.assertIn("다섯 줄로 요약한 뒤", summary.text())
        self.assertEqual(c.one("p", "tasklabel").text(), "태스크 4개")

    def test_every_task_has_a_comment_box_of_its_own_closed_until_asked_for(self):
        for n, row in enumerate(self.rows("r1plan"), 1):
            button = [b for b in row.find_all("button", "tcbtn") if "dwadd" not in b.classes]
            self.assertEqual([b.text() for b in button], ["의견"])
            self.assertEqual(button[0].attrs["aria-expanded"], "false")
            box = row.one("div", "tcrow")
            self.assertEqual(box.one("span", "dwl").text(), "의견")
            area = box.one("textarea", "tc")
            self.assertEqual((area.attrs["data-req"], area.attrs["data-tid"]), ("r1plan", str(n)))
            self.assertEqual(area.attrs["aria-label"], f"{n}번 의견")
            self.assertEqual(area.text(), "")

    def test_a_task_still_to_do_carries_no_state_label(self):
        for row in self.rows("r1plan"):
            self.assertFalse(row.has(cls="badge"))
        done = self.rows("r2plan")[0]
        self.assertEqual(done.one(cls="badge").text(), "완료")

    def test_a_choice_shows_every_option_with_the_recommendation_picked(self):
        row = self.rows("r1plan")[1]
        title = row.one("div", "tt")
        self.assertEqual(title.one("span", "pick").text(), "집계 방식")
        self.assertEqual(title.one("span", "cnt").text(), " · 3가지 중 선택")
        radios = row.find_all("input", type="radio")
        self.assertEqual([r.attrs["value"] for r in radios], ["0", "1", "2", "other"])
        self.assertEqual(["checked" in r.attrs for r in radios], [True, False, False, False])
        self.assertTrue(all(r.attrs["name"] == "ch-r1plan-2" for r in radios))
        self.assertEqual([r.attrs["aria-label"] for r in radios],
                         ["A. 매출 표를 엑셀 서식 그대로 옮기기 (권장)",
                          "B. CSV로 내보내 스크립트로 합산", "C. 표를 직접 다시 입력", "기타"])
        options = row.find_all("label", "opt")
        self.assertEqual([o.one("span", "optl").text() for o in options], ["A.", "B.", "C.", ""])
        self.assertEqual(len(row.find_all("span", "rec")), 1)
        self.assertEqual(options[0].one("span", "rec").text(), "권장")
        self.assertEqual(options[1].one("span", "why").text(), "— 빠르지만 서식이 사라짐")
        self.assertIn("기타 (직접 입력)", options[3].text())

    def test_a_choice_without_a_topic_is_still_named(self):
        row = self.rows("r2plan")[3]
        self.assertEqual(row.one("span", "pick").text(), "진행 방법")
        self.assertEqual(row.one("span", "cnt").text(), " · 2가지 중 선택")
        first = row.find_all("label", "opt")[0]
        self.assertFalse(first.has(cls="why"))

    def test_a_criterion_can_be_written_for_any_task_still_to_do(self):
        rows = self.rows("r1plan")
        for row in rows[:2]:
            self.assertEqual(row.one("button", "dwadd").text(), "완료 기준 추가")
            line = row.one("div", "dw")
            self.assertIn("hid", line.classes)
            self.assertEqual(line.one("input", "dwi").attrs["data-orig"], "")
        for row, text in ((rows[2], "요약이 정확히 5줄이다"), (rows[3], "보낸편지함에 메일이 있다")):
            self.assertFalse(row.has(cls="dwadd"))
            line = row.one("div", "dw")
            self.assertNotIn("hid", line.classes)
            self.assertEqual(line.one("span", "dwl").text(), "태스크 완료 기준")
            field = line.one("input", "dwi")
            self.assertEqual((field.attrs["data-orig"], field.attrs["value"]), (text, text))
            self.assertEqual(field.attrs["maxlength"], "200")
            self.assertEqual(line.one("button", "dwbtn").text(), "수정")
        self.assertEqual(rows[2].one("span", "dwby").text(), "사용자 지정")
        self.assertFalse(rows[3].has(cls="dwby"))

    def test_a_finished_task_keeps_the_criterion_it_was_done_under(self):
        row = self.rows("r2plan")[0]
        self.assertEqual(row.one("span", "dwt").text(), "data.csv 가 있다")
        self.assertFalse(row.has("input", "dwi"))
        self.assertFalse(row.has(cls="dwadd"))

    def test_a_rewritten_task_shows_what_it_was_and_why(self):
        rows = self.rows("r2plan")
        self.assertEqual(rows[1].one("div", "was").text(), "차트를 화면에 띄우기")
        self.assertEqual(rows[1].one("div", "note").text(),
                         "\u21bb 요청하신 내용: 화면 말고 파일로 저장해 주세요")
        self.assertEqual(rows[2].one("div", "note").text(),
                         '✕ 이전 시도 실패: 권한이 없어 "공유 폴더"에 쓸 수 없었습니다')

    def test_approve_and_reject_are_offered_and_revise_waits_for_a_comment(self):
        c = self.card("r1plan")
        row = c.one("div", "row")
        self.assertEqual([(b.attrs.get("id"), b.text(), "hidden" in b.attrs)
                          for b in row.find_all("button")],
                         [("ok-r1plan", "승인", False), ("rev-r1plan", "수정 요청", True),
                          (None, "거절", False)])
        self.assertEqual([b.classes for b in row.find_all("button")], [["ok"], ["rev"], ["no"]])
        self.assertIn("거절 사유", c.one("textarea", id="c-r1plan").attrs["placeholder"])
        scope = c.one("label", "scope")
        self.assertTrue(scope.has("input", type="checkbox", id="all-r1plan"))
        self.assertIn("계획 전체를 다시 세우기", scope.text())

    def test_it_says_whether_the_agent_is_still_waiting(self):
        """Not a countdown: the request outlives the call, so a deadline would be a lie."""
        live = self.card("r1plan").one("div", "chip")
        self.assertEqual(live.classes, ["chip", "live"])
        self.assertEqual(live.text(), "에이전트가 대기 중입니다 · 결정하시면 작업이 바로 이어집니다")
        idle = self.card("r2plan").one("div", "chip")
        self.assertEqual(idle.classes, ["chip", "idle"])
        self.assertIn("에이전트가 대기를 멈췄습니다", idle.text())
        self.assertIn("채팅창에 메시지를 입력하시면 진행이 재개됩니다", idle.text())
        for key in self.items:
            html = pagejs.card(self.items[key])
            for banned in ("남은 시간", "초 남음", "초 후 만료", "deadline"):
                self.assertNotIn(banned, html)

    def test_what_the_agent_still_wanted_to_say_is_shown(self):
        self.assertFalse(self.card("r1plan").has(cls="agent"))
        note = self.card("r2plan").one("div", "summary agent")
        self.assertEqual(note.one("span", "lbl").text(), "에이전트 추가 의견")
        self.assertIn("공유 폴더 권한부터", note.text())


class TestACompletionReport(CardCase):
    def test_the_header_and_where_to_look_first(self):
        c = self.card("r3done")
        self.assertEqual(c.one("h1").text(), "완료 확인 · plan_20261022_0003")
        self.assertEqual(c.one("div", "triage").text(),
                         "4개 중 1개는 태스크 중에 만들거나 바꾼 파일을 서버가 확인했습니다. "
                         "나머지 3개는 에이전트의 보고가 근거입니다.")

    def test_no_triage_line_when_the_server_checked_nothing(self):
        tasks = [{k: v for k, v in t.items() if k not in ("checks", "claims_withdrawn")}
                 for t in self.items["r3done"]["tasks"]]
        self.assertFalse(self.card("r3done", tasks=tasks).has(cls="triage"))

    def test_each_row_shows_the_evidence_and_not_a_done_label(self):
        rows = self.rows("r3done")
        # The only labels are the option that was carried out: every task is DONE.
        self.assertEqual([[b.text() for b in r.find_all(cls="badge")] for r in rows],
                         [[], ["C안 선택"], [], ["권장안"]])
        self.assertEqual([r.one("span", "took").text() for r in rows],
                         ["소요 12초", "소요 2분 5초", "소요 1시간 2분", "소요 3초"])
        evidence = [[e.text() for e in r.find_all("div", "ev")] for r in rows]
        self.assertEqual(evidence, [["→ D:/reports/q3_sales.xlsx 를 찾았습니다."],
                                    ["이전 결과: 표의 절반만 옮겼습니다.",
                                     "→ CSV로 내보내 4분기 열까지 합산했습니다."],
                                    ["→ 요약이 정확히 5줄이다"],
                                    ["(증거 기록 없음)"]])
        self.assertEqual(rows[1].find_all("div", "ev")[0].classes, ["ev", "old"])
        self.assertEqual(rows[3].one("div", "ev").classes, ["ev", "none"])
        self.assertEqual(rows[1].one("div", "note").text(), "\u21bb 요청하신 내용: 4분기 열도 포함해 주세요")

    def test_what_the_server_found_is_told_apart_from_the_agents_word(self):
        rows = self.rows("r3done")
        lines = [[(c.classes[1], c.text(), c.attrs.get("title")) for c in r.find_all("div", "ck")]
                 for r in rows]
        self.assertEqual(lines[0], [("dim", "· q3_sales.xlsx · 47.1 KB · 작업 전부터 있던 파일 "
                                     "(이 태스크에서 바뀌지 않음)", "D:/reports/q3_sales.xlsx")])
        self.assertEqual(lines[1], [("ok", "✔ q3_table.csv · 2.0 KB · 이 태스크 중 생성/변경됨",
                                     "D:/work/q3_table.csv"),
                                    ("ok", "✔ tmp · 폴더가 있음", "D:/work/tmp")])
        self.assertEqual(lines[2], [
            ("warn", "⚠ summary.txt · 빈 파일", "D:/work/summary.txt"),
            ("warn", "⚠ summary_old.txt · 찾을 수 없음", "D:/work/summary_old.txt"),
            ("warn", "⚠ 처음에 draft.docx 을(를) 결과 파일로 적었다가 뺐습니다 (서버가 찾지 못함)", None),
            ("warn", "⚠ 증거가 태스크 완료 기준 문장을 거의 그대로 반복합니다", None)])
        self.assertEqual(lines[3], [
            ("dim", "· system.ini · 서버의 확인 범위 밖", "C:/Windows/system.ini"),
            ("dim", "· mail.eml · 확인하지 못함 (응답 지연)", "D:/work/mail.eml"),
            ("warn", "⚠ sent.txt · 보고 당시에는 있었으나 지금은 없음", "D:/work/sent.txt")])

    def test_a_task_can_be_sent_back_but_its_criterion_is_not_rewritten(self):
        for n, row in enumerate(self.rows("r3done"), 1):
            self.assertEqual(row.one("button", "tcbtn").text(), "다시 작업")
            self.assertEqual(row.one("div", "tcrow").one("span", "dwl").text(), "다시 작업")
            self.assertEqual(row.one("textarea", "tc").attrs["aria-label"], f"{n}번 다시 작업")
            self.assertFalse(row.has("input", "dwi"))
            self.assertFalse(row.has(cls="dwadd"))
        c = self.card("r3done")
        self.assertIn("계획 자체를 다시 세우기", c.one("label", "scope").text())
        self.assertFalse(c.has("input", type="radio"))


class TestAHaltCard(CardCase):
    def test_the_breaker_asks_how_to_go_on_with_its_draft(self):
        c = self.card("r4halt")
        self.assertEqual(c.one("h1").text(), "반복 감지 · plan_20261022_0004")
        reason = c.one("div", "summary warn")
        self.assertEqual(reason.one("span", "lbl").text(), "에이전트가 멈춘 이유")
        self.assertEqual(c.one("p", "tasklabel").text(), "현재 초안 · 태스크 2개")
        rows = self.rows("r4halt")
        self.assertEqual([r.attrs["value"] for r in rows[1].find_all("input", type="radio")],
                         ["0", "1"])
        self.assertEqual(rows[1].one("span", "dwt").text(), "원인별 개수 표가 있다")
        # Not a review of the wording: no per-task comments, no criterion editing.
        self.assertFalse(c.has("textarea", "tc"))
        self.assertFalse(c.has("input", "dwi"))
        self.assertFalse(c.has("button", "tcbtn"))
        self.assertIn("방향", c.one("textarea", id="c-r4halt").attrs["placeholder"])
        self.assertEqual([(b.attrs.get("id"), b.text(), "hidden" in b.attrs)
                          for b in c.one("div", "row").find_all("button")],
                         [("ok-r4halt", "이 초안으로 승인", False),
                          ("rev-r4halt", "계속 진행", False), (None, "취소", False)])

    def test_a_plan_the_human_stopped_is_not_called_a_loop(self):
        c = self.card("r5user")
        self.assertEqual(c.one("h1").text(), "실행 멈춤 · plan_20261022_0005")
        self.assertEqual(c.one("div", "summary warn").one("span", "lbl").text(), "멈춘 이유")
        self.assertEqual(c.one("p", "tasklabel").text(), "태스크 4개")
        rows = self.rows("r5user")
        self.assertEqual([[b.text() for b in r.find_all(cls="badge")] for r in rows],
                         [["완료"], ["완료"], ["진행중"], []])
        self.assertEqual(rows[2].one(cls="badge").classes, ["badge", "now"])
        self.assertEqual([[e.text() for e in r.find_all("div", "ev")] for r in rows],
                         [["→ build-tools.zip 을 받았습니다."], [], [], []])
        self.assertEqual([(b.attrs.get("id"), b.text()) for b in c.one("div", "row").find_all("button")],
                         [("rev-r5user", "계속 진행"), (None, "계획 취소")])


class TestOlderAndDecidedRequests(CardCase):
    def test_a_request_from_a_server_with_no_phase_gets_the_original_form(self):
        c = self.card("r6legacy")
        self.assertEqual(c.one("h1").text(), "승인 요청 · plan_20261022_0006")
        self.assertEqual(c.one("p", "goal").text(), "이전 버전 서버가 보낸 요청")
        self.assertEqual(c.one("pre").text(), self.items["r6legacy"]["display"])
        self.assertFalse(c.has(cls="tasks"))
        self.assertFalse(c.has(cls="scope"))
        self.assertEqual([b.attrs.get("id") for b in c.one("div", "row").find_all("button")],
                         ["ok-r6legacy", "rev-r6legacy", None])

    def test_a_version_other_than_the_pages_own_is_named(self):
        self.assertFalse(self.card("r1plan").has(cls="vernote"))
        self.assertEqual(self.card("r2plan").one("p", "vernote").text(),
                         "이 요청은 planning-mcp 3.0.0 서버가 보냈습니다. 이 페이지는 9.9.9입니다.")
        self.assertEqual(self.card("r6legacy").one("p", "vernote").text(),
                         "이 요청을 보낸 서버는 버전을 남기지 않는 이전 버전입니다. 이 페이지는 9.9.9입니다.")
        self.assertIn("3.1.0", self.card("run-plan_20261022_0013").one("p", "vernote").text())
        self.assertIn("3.0.0", self.card("r4halt", version="3.0.0").one("p", "vernote").text())

    def test_a_decided_request_says_what_was_decided_and_nothing_more(self):
        said = {}
        for key in ("r7decided", "r8halted"):
            for decision in ("APPROVED", "REJECTED", "REVISE"):
                c = self.card(key, decided=decision)
                self.assertFalse(c.has("button") or c.has("textarea"))
                said[key, decision] = c.one("div", "done").text()
        self.assertEqual(said["r7decided", "APPROVED"],
                         "plan_20261022_0007 — 승인되었습니다에이전트가 이 결정을 반영합니다.")
        self.assertIn("거절되었습니다", said["r7decided", "REJECTED"])
        self.assertIn("수정 요청되었습니다", said["r7decided", "REVISE"])
        self.assertIn("초안이 승인되었습니다", said["r8halted", "APPROVED"])
        self.assertIn("— 취소되었습니다", said["r8halted", "REJECTED"])
        self.assertIn("계속 진행하도록 했습니다", said["r8halted", "REVISE"])


class TestARunningPlan(CardCase):
    def test_it_shows_progress_and_every_row_ends_on_its_state(self):
        c = self.card("run-plan_20261022_0011")
        self.assertEqual(c.one("h1").text(), "실행 중 · plan_20261022_0011")
        self.assertEqual(c.one("p", "tasklabel").text(), "태스크 5개 · 완료 2개")
        rows = self.rows("run-plan_20261022_0011")
        last = [r.one("div", "tt").elements()[-1] for r in rows]
        self.assertEqual([(n.classes, n.text()) for n in last],
                         [(["badge"], "완료"), (["badge"], "완료"), (["badge", "now"], "진행중"),
                          (["badge"], "대기"), (["badge"], "실패")])
        # The time taken goes before the state.
        self.assertEqual([r.one("div", "tt").elements()[-2].text() for r in rows[:2]],
                         ["소요 40초", "소요 1분 5초"])
        self.assertEqual([[e.text() for e in r.find_all("div", "ev")] for r in rows],
                         [["→ a.md, b.md, c.md 를 받았습니다."], ["(증거 기록 없음)"], [], [], []])
        self.assertEqual(rows[0].one("div", "ck").text(), "✔ a.md · 900 B · 이 태스크 중 생성/변경됨")
        self.assertEqual(rows[2].one("span", "dwby").text(), "사용자 지정")
        self.assertFalse(c.has("button", "tcbtn"))
        self.assertFalse(c.has("input", "dwi"))
        self.assertEqual(c.one("p", "seen").text(), "에이전트의 마지막 보고 01:00")

    def test_it_can_be_stopped_or_changed_and_closed_without_its_agent(self):
        c = self.card("run-plan_20261022_0011")
        self.assertIn("요약은 표로", c.one("textarea", id="c-run-plan_20261022_0011")
                      .attrs["placeholder"])
        self.assertEqual([(b.classes, b.text()) for b in c.one("div", "row").find_all("button")],
                         [(["rev"], "의견 전달"), (["no"], "멈춤")])
        end = c.one("div", "ctlrow end")
        self.assertEqual([(b.classes, b.text()) for b in end.find_all("button")],
                         [(["linkbtn", "kill"], "계획 강제 취소")])
        self.assertFalse(c.has(cls="pendingctl"))

    def test_a_stop_asked_for_is_shown_as_asked_not_as_done(self):
        c = self.card("run-plan_20261022_0012")
        asked = c.one("div", "pendingctl")
        self.assertTrue(asked.text().startswith(
            "멈춤을 요청하셨습니다. 에이전트가 진행 중인 태스크를 보고하면 다음 태스크를 시작하지 "
            "않고 멈춥니다."))
        self.assertIn("계획을 강제로 취소하실 수 있습니다.", asked.text())
        self.assertFalse(asked.has(cls="q"))
        self.assertEqual([b.text() for b in asked.find_all("button")], ["요청 취소", "계획 강제 취소"])
        self.assertFalse(c.has("textarea"))
        self.assertFalse(c.has("div", "row"))

    def test_a_note_asked_for_is_quoted_back(self):
        asked = self.card("run-plan_20261022_0013").one("div", "pendingctl")
        self.assertTrue(asked.text().startswith("의견을 전달하셨습니다."))
        self.assertEqual(asked.one("span", "q").text(),
                         "요약은 표로 정리해 주세요\n<b>태그</b>는 글자로 보여야 합니다")


class TestNothingFromTheServerIsMarkup(CardCase):
    def test_no_element_comes_from_plan_text(self):
        for key in self.items:
            c = self.card(key)
            for tag in ("script", "b", "i"):
                self.assertFalse(c.has(tag), f"{key}: <{tag}>")

    def test_the_text_reads_as_it_was_written(self):
        c = self.card("r2plan")
        self.assertEqual(c.one("p", "goal").text(),
                         "목표태그가 든 목표 <script>alert(\"goal\")</script> & 'quote'")
        self.assertEqual(self.rows("r2plan")[2].one("div", "tt").find_all("span")[1].text(),
                         "<b>굵게</b> 보이면 안 되는 제목 <script>alert(1)</script>")
        field = self.rows("r2plan")[2].one("input", "dwi")
        self.assertEqual(field.attrs["data-orig"], "<i>기울임</i> 기준 & 'quote'")
        self.assertEqual(field.attrs["value"], "<i>기울임</i> 기준 & 'quote'")
        self.assertIn("<b>태그</b>", self.card("r6legacy").one("pre").text())
        # The overview keeps its line breaks (shown as written, white-space: pre-wrap).
        self.assertIn("\n", c.find_all("div", "summary")[0].text())


class TestTheNotesUnderTheCards(CardCase):
    def hint(self, requests, runs):
        return pagejs.hint(requests, runs)

    def test_each_note_is_there_only_when_it_applies(self):
        data = pagejs.fixtures()
        requests, runs = data["requests"], data["runs"]
        completion = [d for d in requests if d["phase"] == "COMPLETION"]
        run_note = "실행 중인 계획은 에이전트가 태스크를 보고할 때마다 갱신됩니다."
        choice_note = "선택지가 있는 태스크는 [권장]안이 기본으로 선택되어 있습니다."
        criterion_note = "[완료 기준 추가] 또는 [수정]으로 태스크 완료 기준을 직접 적으실 수 있습니다."
        waits = "요청은 응답하실 때까지 사라지지 않으니 천천히 검토해 주시기 바랍니다."
        self.assertEqual(self.hint([], []), "")
        only_runs = self.hint([], runs)
        self.assertIn(run_note, only_runs)
        self.assertIn("[계획 강제 취소]", only_runs)
        self.assertNotIn(waits, only_runs)
        both = self.hint(requests, runs)
        self.assertTrue(both.startswith(run_note))
        for note in (choice_note, criterion_note, waits, "의견을 적으시면 [승인] 대신 [수정 요청] 버튼이 나타납니다."):
            self.assertIn(note, both)
        self.assertNotIn(run_note, self.hint(requests, []))
        self.assertNotIn(criterion_note, self.hint(completion, []))
        decided = [dict(d, decided="APPROVED") for d in requests]
        self.assertNotIn(choice_note, self.hint(decided, []))
        self.assertNotIn(criterion_note, self.hint(decided, []))

    def test_the_idle_text_is_the_one_the_page_starts_with(self):
        """So the page does not flicker between two wordings when the first poll lands."""
        shell = (pagejs.approval.WEB_DIR / "page.html").read_text(encoding="utf-8")
        start = re.search(r'<div class="idle">.*?</div>', shell, re.S).group(0)
        self.assertEqual(pagejs.idle(), start.replace("\n", ""))


class TestTheLittleWords(CardCase):
    def test_how_long_a_task_took(self):
        cases = {0: "0초", 59: "59초", 60: "1분", 125: "2분 5초", 3600: "1시간 0분",
                 3725: "1시간 2분"}
        for seconds, text in cases.items():
            self.assertEqual(self.call("tookText", seconds), text)
        self.assertEqual(self.call("tookText", None), "")

    def test_how_big_a_file_is(self):
        cases = {0: "0 B", 1023: "1023 B", 1024: "1.0 KB", 48213: "47.1 KB",
                 1048576: "1.0 MB", 1536000: "1.5 MB"}
        for size, text in cases.items():
            self.assertEqual(self.call("sizeText", size), text)
        self.assertEqual(self.call("sizeText", None), "")

    def test_a_file_is_named_by_its_last_part(self):
        self.assertEqual(self.call("baseName", "D:\\work\\draft.docx"), "draft.docx")
        self.assertEqual(self.call("baseName", "a/b/c.txt"), "c.txt")
        self.assertEqual(self.call("baseName", "plain"), "plain")
        self.assertEqual(self.call("baseName", None), "")

    def test_the_time_of_the_last_report(self):
        self.assertEqual(self.call("clock", 1792630800), "01:00")
        self.assertEqual(self.call("clock", None), "")

    def test_the_escaper_covers_attribute_values(self):
        self.assertEqual(self.call("esc", "<a href=\"x\">'&'</a>"),
                         "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;")
        self.assertEqual(self.call("esc", None), "")


class TestTheRevisionButtonSaysWhatItWillDo(CardCase):
    """One builder labels the button on every redraw: the scope is shown before the click,
    never inferred by the server afterwards."""

    def label(self, phase, ids, whole, crit):
        return self.call("revLabel", phase, ids, whole, crit)

    def test_a_plan(self):
        self.assertEqual(self.label("PLAN", [], False, 0), "수정 요청")
        self.assertEqual(self.label("PLAN", ["2"], False, 0), "수정 요청\n2번만")
        self.assertEqual(self.label("PLAN", ["2", "3"], False, 0), "수정 요청\n2개 태스크만")
        self.assertEqual(self.label("PLAN", ["2"], True, 0), "수정 요청\n계획 전체 재작성")

    def test_a_completion_report_asks_for_rework_not_a_rewrite(self):
        self.assertEqual(self.label("COMPLETION", ["2"], False, 0), "다시 작업 요청\n2번만")
        self.assertEqual(self.label("COMPLETION", [], True, 0), "수정 요청\n계획 전체 다시 세우기")
        self.assertEqual(self.label("COMPLETION", ["2"], False, 3), "다시 작업 요청\n2번만")

    def test_criteria_written_go_onto_the_tasks_or_to_the_agent(self):
        self.assertEqual(self.label("PLAN", ["2"], False, 2),
                         "수정 요청\n2번만\n태스크 완료 기준 2건 반영")
        self.assertEqual(self.label("PLAN", [], True, 1),
                         "수정 요청\n계획 전체 재작성\n태스크 완료 기준 1건 전달")
        self.assertEqual(self.label("PLAN", [], False, 2), "수정 요청")

    def test_a_halt_cards_continue_button(self):
        self.assertEqual(self.call("haltLabel", False), "계속 진행")
        self.assertEqual(self.call("haltLabel", True), "의견 전달 후 계속")


if __name__ == "__main__":
    unittest.main()
