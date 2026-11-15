"""The Mermaid guard (planning/workflow.py, 3.3.0).

Each test in TestRepairs is one of the slips a model makes in Mermaid (the table in
docs/plan-3.3-advanced-workflow.md §4.2). Every result must also be canonical: linting
what lint wrote gives the same text back, and nothing in it is a construct the subset
does not know. A real Mermaid parse of the same corpus is the browser check in
wiki/07-testing.md.
"""

from __future__ import annotations

import random
import re
import unittest

from planning.workflow import (
    NOT_FLOWCHART,
    TASK_NODE_MISSING,
    TASK_NODE_UNKNOWN,
    TOO_LARGE,
    UNBALANCED,
    UNREACHABLE,
    escape_label,
    lint,
    linear,
    node_ids,
)

TITLES = ["매출 파일 읽기", "피벗으로 집계", "5줄 요약"]
GOOD = (
    "flowchart TD\n"
    "  S([시작]) --> T1[\"매출 파일 읽기\"]\n"
    "  T1 --> D1{\"Q3 데이터가 있나?\"}\n"
    "  D1 -->|예| T2[\"피벗으로 집계\"]\n"
    "  D1 -->|아니오| E([종료])\n"
    "  T2 --> T3[\"5줄 요약\"] --> E\n"
)

# Everything canonical text may contain, line by line.
_CANON_LINE = re.compile(
    r'^(flowchart (TD|TB|LR|RL|BT)'
    r'|  +[A-Za-z][A-Za-z0-9_]*(\(\["|\(\("|\[\["|\[\("|\{\{"|\["|\("|\{"|>")[^"]*("\]\)|"\)\)|"\]\]|"\)\]|"\}\}|"\]|"\)|"\}|"\])'
    r'|  +[A-Za-z][A-Za-z0-9_]* (-->|---|-\.->|==>)(\|"[^"]*"\|)? [A-Za-z][A-Za-z0-9_]*'
    r'|  +subgraph [A-Za-z][A-Za-z0-9_]* \["[^"]*"\]'
    r'|  +direction (TD|TB|LR|RL|BT)'
    r'|  +end)$'
)


def canonical_ok(text: str) -> bool:
    return all(_CANON_LINE.match(line) for line in text.split("\n"))


class TestGood(unittest.TestCase):
    def test_a_clean_flowchart_passes_without_notes(self):
        r = lint(GOOD, TITLES)
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.notes, [])
        self.assertTrue(canonical_ok(r.text), r.text)
        self.assertIn('D1 -->|"예"| T2', r.text)
        self.assertEqual({n["id"] for n in r.nodes}, {"S", "T1", "D1", "T2", "E", "T3"})
        self.assertEqual([n.get("task_id") for n in r.nodes if n.get("task_id")], [1, 2, 3])

    def test_canonical_text_lints_to_itself(self):
        first = lint(GOOD, TITLES)
        again = lint(first.text, TITLES)
        self.assertEqual(again.text, first.text)
        self.assertEqual(again.notes, [])

    def test_task_labels_come_from_the_task_list(self):
        r = lint(GOOD.replace("피벗으로 집계", "집계하기"), TITLES)
        self.assertIn('T2["피벗으로 집계"]', r.text)
        self.assertTrue(any("task title is shown" in n for n in r.notes))

    def test_a_bare_task_id_is_enough(self):
        r = lint("flowchart LR\n  T1 --> T2 --> T3", TITLES)
        self.assertTrue(r.ok, r.errors)
        self.assertIn('T1["매출 파일 읽기"]', r.text)
        self.assertTrue(r.text.startswith("flowchart LR"))

    def test_node_ids(self):
        self.assertEqual(node_ids(lint(GOOD, TITLES).text), {"S", "T1", "D1", "T2", "E", "T3"})

    def test_subgraph_and_ampersand(self):
        src = (
            "flowchart TD\n"
            "  subgraph prep [준비]\n"
            "    direction LR\n"
            "    T1\n"
            "  end\n"
            "  T1 --> T2 & T3\n"
        )
        r = lint(src, TITLES)
        self.assertTrue(r.ok, r.errors)
        self.assertIn('subgraph prep ["준비"]', r.text)
        self.assertIn("T1 --> T2", r.text)
        self.assertIn("T1 --> T3", r.text)
        self.assertTrue(canonical_ok(r.text), r.text)

    def test_edge_kinds(self):
        r = lint("flowchart TD\n T1 -.-> T2\n T2 ==> T3\n T3 --- X[끝]", TITLES)
        self.assertTrue(r.ok, r.errors)
        for edge in ("T1 -.-> T2", "T2 ==> T3", "T3 --- X"):
            self.assertIn(edge, r.text)

    def test_linear(self):
        r = linear(TITLES)
        self.assertTrue(canonical_ok(r.text), r.text)
        self.assertEqual(lint(r.text, TITLES).text, r.text)
        self.assertIn("T3 --> E", r.text)


class TestRepairs(unittest.TestCase):
    def fixed(self, src, titles=TITLES, note=None):
        r = lint(src, titles)
        self.assertTrue(r.ok, f"{src!r}: {r.errors}")
        self.assertTrue(canonical_ok(r.text), r.text)
        self.assertEqual(lint(r.text, titles).text, r.text, "not canonical")
        if note:
            self.assertTrue(any(note in n for n in r.notes), r.notes)
        return r

    def test_r1_code_fence(self):
        self.fixed("```mermaid\n" + GOOD + "```", note="fence")

    def test_r2_escaped_newlines(self):
        self.fixed(GOOD.replace("\n", "\\n"), note="\\n")

    def test_r3_missing_or_bad_header(self):
        self.fixed("T1 --> T2 --> T3", note="no 'flowchart TD'")
        self.fixed("flowchart TOP\nT1 --> T2 --> T3", note="not a direction")
        r = self.fixed("graph LR\nT1 --> T2 --> T3")
        self.assertTrue(r.text.startswith("flowchart LR"))

    def test_r4_special_characters_in_unquoted_labels(self):
        r = self.fixed(
            "flowchart TD\n T1 --> A[파일 읽기 (csv)]\n A --> B{완료?: 예}\n"
            " B --> C[a/b & c] --> T2 --> T3"
        )
        self.assertIn('A["파일 읽기 (csv)"]', r.text)
        self.assertIn('B{"완료?: 예"}', r.text)
        self.assertIn('C["a/b & c"]', r.text)

    def test_r4_nested_brackets(self):
        r = self.fixed('flowchart TD\n T1 --> A[배열[0] 읽기] --> T2 --> T3')
        self.assertIn('A["배열[0] 읽기"]', r.text)

    def test_r5_quotes_inside_a_label(self):
        r = self.fixed('flowchart TD\n T1 --> A["그가 "OK" 함"] --> T2 --> T3')
        self.assertIn('A["그가 #34;OK#34; 함"]', r.text)

    def test_r6_reserved_word_as_id(self):
        r = self.fixed("flowchart TD\n T1 --> T2 --> T3 --> end([끝])", note="keyword")
        self.assertIn('E(["끝"])', r.text)
        self.assertIn("T3 --> E", r.text)
        r = self.fixed("flowchart TD\n T1 --> T2 --> T3 --> style", note="keyword")
        self.assertIn('N1["style"]', r.text)  # the word stays, as a label only
        self.assertIn("T3 --> N1", r.text)

    def test_r7_o_or_x_after_an_arrow(self):
        r = self.fixed("flowchart TD\n T1-->ok[확인]\n ok-->T2\n T2---xray[x]\n xray-->T3")
        self.assertIn("T1 --> ok", r.text)
        self.assertIn("T2 --- xray", r.text)

    def test_r8_wrong_arrows(self):
        for arrow in ("->", "=>", "—>", "–>", "→", "⟶", "-->>"):
            with self.subTest(arrow=arrow):
                r = self.fixed(f"flowchart TD\n T1 {arrow} T2\n T2 --> T3")
                self.assertIn("T1 --> T2", r.text)

    def test_r9_edge_label_forms(self):
        cases = {
            "T1 -->|예|> T2": "예",
            "T1 --|예|--> T2": "예",
            'T1 -->|"예" | T2': "예",
            "T1 -- 예 --> T2": "예",
            "T1 -. 나중에 .-> T2": "나중에",
            "T1 == 꼭 ==> T2": "꼭",
        }
        for src, label in cases.items():
            with self.subTest(src=src):
                r = self.fixed(f"flowchart TD\n {src}\n T2 --> T3")
                self.assertIn(f'|"{label}"| T2', r.text)

    def test_r10_smart_quotes(self):
        r = self.fixed("flowchart TD\n T1 --> A[“시작”] --> T2 --> T3")
        self.assertIn('A["시작"]', r.text)

    def test_r11_unclosed_subgraph(self):
        self.fixed("flowchart TD\n subgraph g [묶음]\n T1 --> T2\n T2 --> T3", note="no 'end'")

    def test_r12_semicolons_and_spacing(self):
        r = self.fixed("flowchart TD;\n  T1-->T2;  T2   -->   T3;")
        self.assertIn("T1 --> T2", r.text)
        self.assertIn("T2 --> T3", r.text)

    def test_r13_two_labels_for_one_id(self):
        self.fixed(
            "flowchart TD\n T1 --> A[확인] --> T2\n A[검사] --> T3", note="two labels"
        )

    def test_r14_task_drawn_under_another_id(self):
        r = self.fixed(
            'flowchart TD\n A["매출 파일 읽기"] --> B["피벗으로 집계"] --> C[5줄 요약]',
            note="task 1's title",
        )
        self.assertIn("T1 --> T2", r.text)

    def test_r15_html_in_labels(self):
        r = self.fixed("flowchart TD\n T1 --> A[1단계<br>읽기 <b>중요</b>] --> T2 --> T3")
        self.assertIn('A["1단계 읽기 중요"]', r.text)

    def test_ids_with_spaces_become_labels(self):
        r = self.fixed("flowchart TD\n 데이터 확인 --> T1 --> T2 --> T3", note="no spaces")
        self.assertRegex(r.text, r'N1\["데이터 확인"\]')

    def test_non_ascii_ids_are_renamed(self):
        r = self.fixed("flowchart TD\n 시작 --> T1 --> T2 --> T3")
        self.assertIn('N1["시작"]', r.text)

    def test_task_ids_in_other_spellings(self):
        r = self.fixed("flowchart TD\n t1 --> Task2 --> T_3")
        self.assertIn("T1 --> T2", r.text)
        self.assertIn("T2 --> T3", r.text)

    def test_back_edge_is_kept_with_a_note(self):
        r = self.fixed("flowchart TD\n T1 --> T2 --> T3\n T3 -->|다시| T2", note="points back")
        self.assertIn('T3 -->|"다시"| T2', r.text)


class TestRemoved(unittest.TestCase):
    def test_click_style_init_and_classes(self):
        src = (
            "%%{init: {'securityLevel': 'loose'}}%%\n"
            "flowchart TD\n"
            "  T1:::hot --> T2 --> T3\n"
            "  click T1 callback \"tip\"\n"
            "  click T2 href \"http://example.com\"\n"
            "  style T1 fill:#f00\n"
            "  classDef hot fill:#f00\n"
            "  class T2 hot\n"
            "  linkStyle 0 stroke:#f00\n"
            "  %% a comment\n"
        )
        r = lint(src, TITLES)
        self.assertTrue(r.ok, r.errors)
        for word in ("click", "style", "classDef", "class ", "linkStyle", "init", ":::", "%%",
                     "http"):
            self.assertNotIn(word, r.text)
        self.assertTrue(any("init" in n for n in r.notes))
        self.assertTrue(any("click" in n for n in r.notes))

    def test_markup_in_labels_cannot_reach_the_page(self):
        r = lint('flowchart TD\n T1 --> A["<script>alert(1)</script>"] --> T2 --> T3', TITLES)
        self.assertTrue(r.ok, r.errors)
        self.assertNotIn("<", r.text)
        self.assertNotIn(">\"", r.text)

    def test_escape_label(self):
        self.assertEqual(escape_label('a "b" <c> #1'), "a #34;b#34; #60;c#62; #35;1")
        # A label that starts with a backtick opened a Markdown string Mermaid never saw
        # closed: 12 of 610 fuzzed texts failed in the real parser before this.
        self.assertEqual(escape_label("`x"), "#96;x")
        r = lint("flowchart TD\n T1 --> A[`x] --> T2 --> T3\n T3 -->|`y| B", TITLES)
        self.assertNotIn("`", r.text)
        self.assertEqual(lint(r.text, TITLES).text, r.text)
        # The model's own entities are read back, not escaped twice.
        r = lint('flowchart TD\n T1 --> A["a #quot;b#quot;"] --> T2 --> T3', TITLES)
        self.assertIn('A["a #34;b#34;"]', r.text)


class TestRefused(unittest.TestCase):
    def refused(self, src, code, titles=TITLES):
        r = lint(src, titles)
        self.assertFalse(r.ok, r.text)
        self.assertIsNone(r.text)
        self.assertEqual(r.errors[0]["code"], code, r.errors)
        self.assertTrue(r.errors[0]["message"])
        return r

    def test_not_a_flowchart(self):
        self.refused("sequenceDiagram\n A->>B: hi", NOT_FLOWCHART)
        self.refused("gantt\n title x", NOT_FLOWCHART)

    def test_task_missing(self):
        r = self.refused("flowchart TD\n T1 --> T3", TASK_NODE_MISSING)
        self.assertIn('T2["피벗으로 집계"]', r.errors[0]["fix"])

    def test_task_unknown(self):
        self.refused("flowchart TD\n T1 --> T2 --> T3 --> T5", TASK_NODE_UNKNOWN)

    def test_unbalanced(self):
        r = self.refused("flowchart TD\n T1 --> A[시작) --> T2 --> T3", UNBALANCED)
        self.assertIn("line 2", r.errors[0]["message"])

    def test_unreachable_loop(self):
        self.refused("flowchart TD\n T1 --> X\n T2 --> T3\n T3 --> T2", UNREACHABLE)

    def test_too_large(self):
        src = "flowchart TD\n" + "\n".join(f" N{i} --> N{i + 1}" for i in range(40))
        self.refused(src + "\n T1 --> T2 --> T3", TOO_LARGE)
        self.refused("x" * 9000, TOO_LARGE)

    def test_empty(self):
        self.assertFalse(lint("", TITLES).ok)
        self.assertFalse(lint(None, TITLES).ok)
        self.assertFalse(lint("flowchart TD", TITLES).ok)


class TestNeverRaises(unittest.TestCase):
    def test_fuzz(self):
        rng = random.Random(33)
        alphabet = list("TDSE123 \n\t-=.>|[](){}\"'&;:%#<~`ox가나→") + ["-->", "end", "subgraph "]
        base = [GOOD, linear(TITLES).text]
        for i in range(1500):
            if i % 3 == 0:
                src = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
            else:
                text = rng.choice(base)
                cut = rng.randint(0, len(text))
                src = text[:cut] + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6))) \
                    + text[cut + rng.randint(0, 5):]
            r = lint(src, TITLES)
            if r.ok:
                self.assertTrue(canonical_ok(r.text), f"{src!r} ->\n{r.text}")
                self.assertEqual(lint(r.text, TITLES).text, r.text, repr(src))
            else:
                self.assertTrue(r.errors)

    def test_odd_types(self):
        for raw in (123, b"T1", ["T1"], {"a": 1}):
            lint(raw, TITLES)


if __name__ == "__main__":
    unittest.main()
