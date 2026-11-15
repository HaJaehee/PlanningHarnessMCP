"""The plan as a Mermaid flowchart, and the guard that keeps the model's Mermaid drawable (3.3.0).

With ADVANCED_WORKFLOW_PLANNING on, the model sends its final task list together with a
`workflow`: a Mermaid flowchart in which task n is the node T<n>. The approval page
draws it and the human comments on its nodes.

A mid-sized model writes Mermaid the way it writes everything else - mostly right, and
wrong in the same few places every time: a label with brackets in it, `end` as a node
id, `A-->ok` (which Mermaid reads as a circle edge to `k`), a code fence around the
whole thing, `\n` sent as two characters. Mermaid then draws nothing and says only
"Parse error on line 4". So the browser never sees the model's text:

    lint(raw)  =  repair (text)  ->  parse (a subset)  ->  check (harness rules)  ->  emit

`emit` writes canonical Mermaid from what was parsed: every label quoted, every special
character an entity, task labels taken from the task list, only syntax the subset
knows. Whatever the model sent, what reaches Mermaid is text this module wrote - so a
parse error in the browser means a bug here, which a test can catch, not a slip by the
model, which nobody could.

What can be repaired is repaired and said in a note (the model reads the notes and
makes the slip less often). What cannot - a sequence diagram, a task with no node, a
bracket that never closes - is an error with its line and how to fix it.

Pure, imports nothing from the package, never raises. Plan:
docs/plan-3.3-advanced-workflow.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_SOURCE_CHARS = 8000
DEFAULT_MAX_NODES = 30

# Error codes (entries of LintResult.errors, not the tool's error_code).
NOT_FLOWCHART = "NOT_FLOWCHART"
TASK_NODE_MISSING = "TASK_NODE_MISSING"
TASK_NODE_UNKNOWN = "TASK_NODE_UNKNOWN"
UNBALANCED = "UNBALANCED"
UNREACHABLE = "UNREACHABLE"
TOO_LARGE = "TOO_LARGE"
EMPTY = "EMPTY"
SYNTAX = "SYNTAX"

DIRECTIONS = ("TD", "TB", "LR", "RL", "BT")
OTHER_DIAGRAMS = (
    "sequencediagram", "classdiagram", "statediagram", "statediagram-v2", "erdiagram",
    "gantt", "pie", "journey", "mindmap", "timeline", "gitgraph", "quadrantchart",
    "requirementdiagram", "c4context", "sankey-beta", "xychart-beta", "block-beta",
    "packet-beta", "architecture-beta", "kanban",
)
# Words Mermaid reads as syntax wherever they appear as a node id.
RESERVED_IDS = {
    "end", "graph", "flowchart", "subgraph", "style", "class", "classdef", "click",
    "direction", "default", "linkstyle", "call", "href",
}
# Statements this module drops: they style or script the diagram, and the page does both.
_DROPPED = re.compile(r"^(click|style|classDef|class|linkStyle)\b", re.IGNORECASE)

# shape name -> (opener, closer). Longer openers first: "([" before "(" and "[".
SHAPES = (
    ("stadium", "([", "])"),
    ("circle", "((", "))"),
    ("subroutine", "[[", "]]"),
    ("cylinder", "[(", ")]"),
    ("hexagon", "{{", "}}"),
    ("rect", "[", "]"),
    ("round", "(", ")"),
    ("diamond", "{", "}"),
    ("asym", ">", "]"),
)
_SHAPE_BY_NAME = {name: (o, c) for name, o, c in SHAPES}

# Arrows, longest first. The odd ones are what models write instead of `-->`.
_ARROW = re.compile(
    r"<?(?:-{2,}>+|-\.+->+|-\.+-|={2,}>+|-{3,}|={3,}|->+|=>+|—+>|–+>|→|⟶|⇒|⟹)"
)
# `-- text -->`, `== text ==>`, `-. text .->`
_TEXT_EDGE_START = re.compile(r"(--|==|-\.)(?![->=.])")
_TEXT_EDGE_END = re.compile(r"\s(\.-+>+|\.-+|={2,}>+|-{2,}>+|-{3,}|={3,}|->+|=>+)")
_QUOTES_OPEN = "\"“”„"
_ID_CHARS = re.compile(r"[^\s\[\](){}<>|&;:\"“”'=\-→⟶⇒⟹—–]")


@dataclass
class Node:
    id: str
    label: str = ""
    shape: str = "rect"
    task_id: int | None = None
    subgraph: str | None = None


@dataclass
class Edge:
    src: str
    dst: str
    kind: str = "arrow"   # arrow | open | dotted | thick
    label: str = ""


@dataclass
class LintResult:
    text: str | None                         # canonical Mermaid, or None when errors
    notes: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.text is not None


def _key(text: str) -> str:
    """Compare titles loosely: case, spacing and punctuation do not make a different task."""
    return re.sub(r"[\W_]+", "", str(text or "").lower())


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def clean_label(raw: str) -> str:
    """The words of a label, with Mermaid's markup and the model's escapes taken out."""
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] in _QUOTES_OPEN and text[-1] in _QUOTES_OPEN:
        text = text[1:-1]
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":  # markdown string
        text = text[1:-1]
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = text.replace("\\n", " ")
    # Entities the model wrote itself come back as the characters they stand for, so
    # escaping below does not escape them twice.
    text = re.sub(r"#quot;", '"', text)
    text = re.sub(r"#(\d{2,5});", lambda m: _chr(m.group(1)), text)
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def _chr(code: str) -> str:
    try:
        return chr(int(code))
    except (ValueError, OverflowError):
        return ""


def escape_label(text: str) -> str:
    """A label as it goes between double quotes in canonical Mermaid."""
    out = []
    for ch in text:
        if ch == "#":
            out.append("#35;")
        elif ch == '"':
            out.append("#34;")
        elif ch == "<":
            out.append("#60;")
        elif ch == ">":
            out.append("#62;")
        elif ch == "`":
            # "` opens a Markdown string in Mermaid, and one never closed is a parse error
            # - found by parsing the fuzz corpus with the real mermaid.js.
            out.append("#96;")
        elif ch in "\r\n\t":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# text-level repair
# ---------------------------------------------------------------------------


def _repair_text(raw: str, notes: list[str]) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    fence = re.match(r"^```[ \t]*(mermaid)?[ \t]*\n?(.*?)\n?```\s*$", text, re.S | re.I)
    if fence:
        text = fence.group(2).strip()
        notes.append("workflow: removed the ``` fence - send the flowchart text alone.")
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
        notes.append(
            "workflow: it arrived on one line with \\n written as text; read \\n as line breaks."
        )
    text = text.replace("\t", "  ")
    return text


def _split_statements(text: str) -> list[tuple[int, str]]:
    """(line number, statement). `;` ends a statement, except inside a label."""
    out: list[tuple[int, str]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        buf: list[str] = []
        depth = 0
        quoted = False
        for ch in line:
            if ch in _QUOTES_OPEN:
                quoted = not quoted
            elif not quoted and ch in "[({":
                depth += 1
            elif not quoted and ch in "])}" and depth:
                depth -= 1
            if ch == ";" and depth == 0 and not quoted:
                out.append((number, "".join(buf).strip()))
                buf = []
                continue
            buf.append(ch)
        out.append((number, "".join(buf).strip()))
    return [(n, s) for n, s in out if s]


# ---------------------------------------------------------------------------
# parsing one statement
# ---------------------------------------------------------------------------


class _Fail(Exception):
    def __init__(self, code: str, message: str, fix: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


def _skip_ws(s: str, p: int) -> int:
    while p < len(s) and s[p] in " \t":
        p += 1
    return p


def _find_closer(s: str, p: int, opener: str, closer: str) -> int:
    """Index of `closer` matching an opener whose content starts at p, or -1.

    Brackets of the opener's kind nest (`[배열[0] 읽기]`), and anything between double
    quotes is skipped whole, so a quoted label may hold any bracket at all.
    """
    open_ch, close_ch = opener[-1], closer[0]
    depth = 0
    i = p
    while i < len(s):
        ch = s[i]
        if ch in _QUOTES_OPEN:
            j = i + 1
            while j < len(s) and s[j] not in _QUOTES_OPEN:
                j += 1
            if j < len(s):
                i = j + 1
                continue
        if s.startswith(closer, i) and depth == 0:
            return i
        if ch == open_ch and open_ch != close_ch:
            depth += 1
        elif ch == close_ch and depth > 0:
            depth -= 1
        i += 1
    return -1


def _read_node(s: str, p: int) -> tuple[str, str | None, str | None, int, bool]:
    """(raw id, shape, raw label, new p, id had spaces) at p.

    A bare run of words with no shape (`데이터 읽기 --> B`) is one node whose id is
    those words: Mermaid refuses it, but what the model meant is not in doubt.
    """
    start = p
    words: list[str] = []
    while True:
        q = p
        while q < len(s) and _ID_CHARS.match(s[q]):
            q += 1
        # `.` and `-` may sit inside an id (step-1) but not start an arrow there.
        while q < len(s) and s[q] in "-." and not _ARROW.match(s, q) \
                and not _TEXT_EDGE_START.match(s, q) and q + 1 < len(s) \
                and _ID_CHARS.match(s[q + 1]):
            q += 1
            while q < len(s) and _ID_CHARS.match(s[q]):
                q += 1
        if q == p:
            break
        words.append(s[p:q])
        p = q
        r = _skip_ws(s, p)
        # Another bare word follows: the id has a space in it.
        if r < len(s) and r > p and _ID_CHARS.match(s[r]) and not s.startswith("&", r):
            p = r
            continue
        break
    if not words:
        raise _Fail(SYNTAX, f"expected a node at '{s[start:start + 20]}'",
                    'write a node as ID["label"], e.g. T1["읽기"]')
    raw_id = " ".join(words)
    spaced = len(words) > 1
    r = _skip_ws(s, p)
    for name, opener, closer in SHAPES:
        if s.startswith(opener, r):
            if name == "asym" and r > p:
                break  # ` >` after a space is not a shape
            end = _find_closer(s, r + len(opener), opener, closer)
            if end < 0:
                raise _Fail(
                    UNBALANCED,
                    f"'{opener}' after {raw_id} is never closed with '{closer}'",
                    f'close it: {raw_id}{opener}"..."{closer}',
                )
            label = s[r + len(opener):end]
            p = end + len(closer)
            # `:::class` after a node styles it; the page does the styling.
            m = re.match(r":::[\w-]+", s[p:])
            if m:
                p += m.end()
            return raw_id, name, label, p, spaced
    m = re.match(r":::[\w-]+", s[p:])
    if m:
        p += m.end()
    return raw_id, None, None, p, spaced


def _read_edge(s: str, p: int, notes: list[str]) -> tuple[str, str, int] | None:
    """(kind, raw label, new p) for an edge at p, or None if there is none."""
    label = ""
    m = _TEXT_EDGE_START.match(s, p)
    if m and not _ARROW.match(s, p):
        rest = s[m.end():]
        bar = re.match(r"\s*\|(.*?)\|\s*", rest)
        if bar:  # `--|예|-->`
            after = _ARROW.match(rest, bar.end())
            if after:
                notes.append("workflow: an edge label sat inside the arrow; wrote it as -->|label|.")
                return _kind(m.group(1) + after.group(0)), bar.group(1), \
                    m.end() + after.end()
        arrow = _TEXT_EDGE_END.search(rest)
        if arrow and arrow.start() > 0:
            label = rest[: arrow.start()].strip()
            if label and not re.search(r"[\[\]{}()]", label):
                return _kind(m.group(1) + arrow.group(1)), label, m.end() + arrow.end()
    m = _ARROW.match(s, p)
    if not m:
        return None
    arrow = m.group(0)
    p = m.end()
    canonical = ("-->", "---", "-.->", "==>", "-.-", "===")
    core = arrow.lstrip("<")
    if core not in canonical and not re.fullmatch(r"-{3,}>|-{4,}|={3,}>|={4,}|-\.{2,}->", core):
        notes.append(f"workflow: '{arrow}' is not a Mermaid arrow; wrote it as -->.")
    elif arrow.startswith("<"):
        notes.append(f"workflow: '{arrow}' points both ways; drew it one way (-->).")
    q = _skip_ws(s, p)
    if q < len(s) and s[q] == "|":
        end = s.find("|", q + 1)
        if end < 0:
            raise _Fail(UNBALANCED, "an edge label opened with '|' is never closed",
                        'write the label as -->|"label"|')
        label = s[q + 1:end]
        p = end + 1
        if p < len(s) and s[p] == ">":
            p += 1
            notes.append("workflow: removed a stray '>' after an edge label.")
    return _kind(arrow), label, p


def _kind(arrow: str) -> str:
    core = arrow.lstrip("<")
    if "." in core:
        return "dotted"
    if core.startswith("==") and ">" in core:
        return "thick"
    if ">" in core or core in ("→", "⟶", "⇒", "⟹") or core.endswith(("→",)):
        return "arrow"
    if core.startswith("—") or core.startswith("–"):
        return "arrow"
    return "open"


# ---------------------------------------------------------------------------
# the whole diagram
# ---------------------------------------------------------------------------


class _Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}       # raw id -> node
        self.order: list[str] = []
        self.edges: list[Edge] = []
        self.subgraphs: list[dict[str, Any]] = []  # {raw, title, parent, direction}
        self.direction = "TD"

    def node(self, raw_id: str, shape: str | None, label: str | None,
             sub: str | None, notes: list[str]) -> str:
        found = self.nodes.get(raw_id)
        if found is None:
            found = Node(id=raw_id, subgraph=sub)
            self.nodes[raw_id] = found
            self.order.append(raw_id)
        if shape is not None:
            text = clean_label(label or "")
            if found.label and text and found.label != text:
                notes.append(
                    f"workflow: node {raw_id} was given two labels; kept the first, "
                    f"'{found.label}'."
                )
            elif text or not found.label:
                found.label = found.label or text
                found.shape = shape
        return raw_id


def _parse(text: str, notes: list[str]) -> _Graph:
    graph = _Graph()
    statements = _split_statements(text)
    # A diagram directive can switch Mermaid's own settings; the page sets them.
    kept: list[tuple[int, str]] = []
    for number, st in statements:
        if st.startswith("%%"):
            if st.startswith("%%{"):
                notes.append("workflow: removed a %%{init}%% directive.")
            continue
        kept.append((number, st))
    if not kept:
        raise _Fail(EMPTY, "the workflow is empty", "send a flowchart TD with one node per task")
    first_no, first = kept[0]
    head = re.match(r"^(flowchart|graph)\b\s*([A-Za-z]*)\s*(.*)$", first, re.I)
    word = first.split()[0].lower() if first.split() else ""
    if word in OTHER_DIAGRAMS:
        raise _Fail(NOT_FLOWCHART, f"'{first.split()[0]}' is not a flowchart",
                    "start with: flowchart TD")
    if head:
        direction = head.group(2).upper()
        if direction not in DIRECTIONS:
            if direction:
                notes.append(f"workflow: '{head.group(2)}' is not a direction; used TD.")
            direction = "TD"
        graph.direction = direction
        rest = head.group(3).strip()
        kept = kept[1:]
        if rest:
            kept.insert(0, (first_no, rest))
    else:
        notes.append("workflow: it had no 'flowchart TD' line; added one.")

    stack: list[str] = []
    sub_count = 0
    for number, st in kept:
        low = st.lower()
        dropped = _DROPPED.match(st)
        if dropped and st[dropped.end():dropped.end() + 1] in (" ", "\t"):
            if low.startswith("click"):
                notes.append("workflow: removed a click statement; the page handles clicks.")
            continue
        if low == "end" or low == "end;":
            if stack:
                stack.pop()
                continue
            raise _Fail(SYNTAX, f"line {number}: 'end' closes no subgraph",
                        "remove the extra 'end'")
        if low.startswith("subgraph"):
            body = st[len("subgraph"):].strip()
            sub_count += 1
            m = re.match(r'^([A-Za-z_][\w-]*)\s*\[(.*)\]\s*$', body)
            if m:
                raw, title = m.group(1), clean_label(m.group(2))
            elif body and body[0] in _QUOTES_OPEN:
                raw, title = f"__sg{sub_count}", clean_label(body)
            elif re.fullmatch(r"[A-Za-z_][\w-]*", body or ""):
                raw, title = body, body
            else:
                raw, title = f"__sg{sub_count}", clean_label(body)
            graph.subgraphs.append(
                {"raw": raw, "title": title, "parent": stack[-1] if stack else None,
                 "direction": None}
            )
            stack.append(raw)
            continue
        if low.startswith("direction "):
            d = st.split()[1].upper() if len(st.split()) > 1 else ""
            if stack and d in DIRECTIONS:
                for sg in graph.subgraphs:
                    if sg["raw"] == stack[-1]:
                        sg["direction"] = d
            continue
        try:
            _parse_chain(graph, st, stack[-1] if stack else None, notes)
        except _Fail as fail:
            raise _Fail(fail.code, f"line {number} `{st[:80]}`: {fail.message}", fail.fix)
    if stack:
        notes.append("workflow: a subgraph had no 'end'; closed it.")
    return graph


def _parse_chain(graph: _Graph, st: str, sub: str | None, notes: list[str]) -> None:
    """`A & B --> C -->|x| D` - groups of nodes joined by edges."""
    p = 0
    previous: list[str] = []
    pending: tuple[str, str] | None = None
    spaced_seen = False
    while True:
        p = _skip_ws(st, p)
        group: list[str] = []
        while True:
            raw_id, shape, label, p, spaced = _read_node(st, p)
            if spaced and not spaced_seen:
                spaced_seen = True
                notes.append(
                    f"workflow: '{raw_id}' is not a node id (ids have no spaces); "
                    f'read it as a node labelled "{raw_id}". Write N1["{raw_id}"].'
                )
            if spaced and shape is None:
                shape, label = "rect", raw_id
            group.append(graph.node(raw_id, shape, label, sub, notes))
            p = _skip_ws(st, p)
            if p < len(st) and st[p] == "&":
                p = _skip_ws(st, p + 1)
                continue
            break
        if pending is not None:
            kind, label_text = pending
            for a in previous:
                for b in group:
                    graph.edges.append(Edge(a, b, kind, clean_label(label_text)))
        previous = group
        p = _skip_ws(st, p)
        if p >= len(st):
            if pending is None and not group:
                raise _Fail(SYNTAX, "nothing to read", "")
            return
        edge = _read_edge(st, p, notes)
        if edge is None:
            raise _Fail(
                SYNTAX, f"unexpected '{st[p:p + 15]}'",
                'join nodes with --> and quote labels: A["..."] --> B["..."]',
            )
        kind, label_text, p = edge
        pending = (kind, label_text)
        p = _skip_ws(st, p)
        if p >= len(st):
            raise _Fail(SYNTAX, "an arrow points at nothing", "end the line with a node")


# ---------------------------------------------------------------------------
# harness rules and canonical ids
# ---------------------------------------------------------------------------

_TASK_ID = re.compile(r"^(?:t|task|task_|step|s)[\s_-]?(\d{1,3})$", re.I)


def _assign_ids(graph: _Graph, titles: list[str], notes: list[str],
                errors: list[dict[str, Any]]) -> dict[str, str]:
    """raw id -> canonical id. Tasks become T<n>; every other id is made safe."""
    mapping: dict[str, str] = {}
    task_of: dict[int, str] = {}
    count = len(titles)
    for raw in graph.order:
        m = _TASK_ID.match(raw)
        if m and raw[0] in "Tt":
            n = int(m.group(1))
            if 1 <= n <= count:
                if n in task_of:
                    continue
                task_of[n] = raw
                if raw != f"T{n}":
                    notes.append(f"workflow: node {raw} is task {n}; named it T{n}.")
            else:
                errors.append({
                    "code": TASK_NODE_UNKNOWN,
                    "message": f"node {raw} names task {n}, but the plan has {count} tasks",
                    "fix": f"use T1..T{count}, one per task",
                })
    # A task drawn under another id but with its exact title: that is the task.
    keys = {_key(t): i for i, t in enumerate(titles, start=1)}
    for raw in graph.order:
        if raw in task_of.values():
            continue
        node = graph.nodes[raw]
        n = keys.get(_key(node.label)) or keys.get(_key(raw))
        if n and n not in task_of:
            task_of[n] = raw
            notes.append(f"workflow: node {raw} has task {n}'s title; named it T{n}.")
    for n, raw in task_of.items():
        mapping[raw] = f"T{n}"
        graph.nodes[raw].task_id = n
    missing = [n for n in range(1, count + 1) if n not in task_of]
    if missing:
        errors.append({
            "code": TASK_NODE_MISSING,
            "message": "no node for task(s) " + ", ".join(str(n) for n in missing),
            "fix": "add " + ", ".join(f'T{n}["{titles[n - 1]}"]' for n in missing[:3])
                   + " and connect them",
        })
    used = set(mapping.values())
    counter = 0
    for raw in graph.order:
        if raw in mapping:
            continue
        safe = re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,30}", raw) is not None
        reserved = raw.lower() in RESERVED_IDS
        looks_task = re.fullmatch(r"T\d+", raw) is not None
        if safe and not reserved and not looks_task and raw not in used:
            mapping[raw] = raw
        else:
            if reserved and raw.lower() == "end" and "E" not in used \
                    and "E" not in graph.nodes:
                new = "E"
            else:
                counter += 1
                new = f"N{counter}"
                while new in used or new in graph.nodes:
                    counter += 1
                    new = f"N{counter}"
            if reserved:
                notes.append(f"workflow: '{raw}' is a Mermaid keyword; renamed the node {new}.")
            mapping[raw] = new
        used.add(mapping[raw])
    counter = 0
    for sg in graph.subgraphs:
        raw = sg["raw"]
        if raw in mapping and graph.nodes.get(raw) is None:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,30}", raw) and raw.lower() not in RESERVED_IDS \
                and raw not in used:
            sg["id"] = raw
        else:
            counter += 1
            sg["id"] = f"SG{counter}"
            while sg["id"] in used:
                counter += 1
                sg["id"] = f"SG{counter}"
        used.add(sg["id"])
    return mapping


def _check_reach(graph: _Graph, mapping: dict[str, str], notes: list[str],
                 errors: list[dict[str, Any]]) -> None:
    ids = [mapping[r] for r in graph.order]
    incoming: dict[str, int] = {i: 0 for i in ids}
    out: dict[str, list[str]] = {i: [] for i in ids}
    for e in graph.edges:
        a, b = mapping.get(e.src), mapping.get(e.dst)
        if a is None or b is None:
            continue
        out[a].append(b)
        incoming[b] += 1
    if not graph.edges:
        return
    roots = [i for i in ids if incoming[i] == 0]
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        todo.extend(out[cur])
    unreached = [i for i in ids if i not in seen and re.fullmatch(r"T\d+", i)]
    if unreached:
        errors.append({
            "code": UNREACHABLE,
            "message": "task node(s) " + ", ".join(unreached)
                       + " can only be reached from a loop with no way in",
            "fix": "add an edge from the start, e.g. S --> " + unreached[0],
        })
    lone = [i for i in ids if re.fullmatch(r"T\d+", i) and not out[i] and not incoming[i]]
    if lone and len(ids) > 1:
        notes.append(
            "workflow: " + ", ".join(lone) + " is not connected to anything."
        )
    for e in graph.edges:
        a, b = mapping.get(e.src, ""), mapping.get(e.dst, "")
        ma, mb = re.fullmatch(r"T(\d+)", a), re.fullmatch(r"T(\d+)", b)
        if ma and mb and int(mb.group(1)) < int(ma.group(1)):
            notes.append(
                f"workflow: {a} --> {b} points back to an earlier task. That is fine to "
                "draw, but tasks still run in order 1..n - report a repeat in result_log."
            )
            break


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------

_EDGE_TEXT = {"arrow": "-->", "open": "---", "dotted": "-.->", "thick": "==>"}


def _emit(graph: _Graph, mapping: dict[str, str], titles: list[str],
          notes: list[str]) -> tuple[str, list[dict[str, Any]]]:
    nodes_out: list[dict[str, Any]] = []
    relabelled: list[str] = []
    defs: dict[str | None, list[str]] = {}
    for raw in graph.order:
        node = graph.nodes[raw]
        cid = mapping[raw]
        label = node.label
        if node.task_id is not None:
            title = " ".join(str(titles[node.task_id - 1]).split())
            if label and _key(label) != _key(title):
                relabelled.append(cid)
            label = title
        if not label:
            label = raw  # a bare id is its own label - the model's words, not ours
        opener, closer = _SHAPE_BY_NAME.get(node.shape, ("[", "]"))
        defs.setdefault(node.subgraph, []).append(
            f'{cid}{opener}"{escape_label(label)}"{closer}'
        )
        nodes_out.append({
            "id": cid, "label": label, "shape": node.shape,
            **({"task_id": node.task_id} if node.task_id is not None else {}),
        })
    if relabelled:
        notes.append(
            "workflow: the label of " + ", ".join(relabelled)
            + " differed from its task title; the task title is shown."
        )
    lines = [f"flowchart {graph.direction}"]

    def block(sub: str | None, indent: str) -> None:
        for d in defs.get(sub, []):
            lines.append(indent + d)
        for sg in graph.subgraphs:
            if sg["parent"] == sub:
                lines.append(f'{indent}subgraph {sg["id"]} ["{escape_label(sg["title"] or sg["id"])}"]')
                if sg.get("direction"):
                    lines.append(f'{indent}  direction {sg["direction"]}')
                block(sg["raw"], indent + "  ")
                lines.append(f"{indent}end")

    block(None, "  ")
    sub_ids = {sg["raw"]: sg["id"] for sg in graph.subgraphs}
    seen_edges: set[tuple[str, str, str, str]] = set()
    for e in graph.edges:
        a = mapping.get(e.src) or sub_ids.get(e.src)
        b = mapping.get(e.dst) or sub_ids.get(e.dst)
        if not a or not b:
            continue
        key = (a, b, e.kind, e.label)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        arrow = _EDGE_TEXT.get(e.kind, "-->")
        if e.label:
            lines.append(f'  {a} {arrow}|"{escape_label(e.label)}"| {b}')
        else:
            lines.append(f"  {a} {arrow} {b}")
    return "\n".join(lines), nodes_out


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------


def lint(raw: Any, titles: list[str], max_nodes: int = DEFAULT_MAX_NODES) -> LintResult:
    """Repair, parse, check and rewrite the model's flowchart. Never raises."""
    try:
        return _lint(raw, list(titles or []), max_nodes)
    except Exception as exc:  # noqa: BLE001 - a guard that throws guards nothing
        return LintResult(None, [], [{
            "code": SYNTAX, "message": f"the workflow could not be read ({type(exc).__name__})",
            "fix": "send a simple flowchart TD with one line per edge",
        }])


def _lint(raw: Any, titles: list[str], max_nodes: int) -> LintResult:
    notes: list[str] = []
    errors: list[dict[str, Any]] = []
    if not isinstance(raw, str):
        raw = "" if raw is None else str(raw)
    if len(raw) > MAX_SOURCE_CHARS:
        return LintResult(None, notes, [{
            "code": TOO_LARGE, "message": f"the workflow is {len(raw)} characters long",
            "fix": f"keep it under {MAX_SOURCE_CHARS} characters - one node per task",
        }])
    text = _repair_text(raw, notes)
    try:
        graph = _parse(text, notes)
    except _Fail as fail:
        return LintResult(None, notes, [{"code": fail.code, "message": fail.message,
                                         "fix": fail.fix}])
    if not graph.order:
        return LintResult(None, notes, [{"code": EMPTY, "message": "the workflow has no nodes",
                                         "fix": "add one node per task, T1..Tn"}])
    if len(graph.order) > max(1, max_nodes):
        return LintResult(None, notes, [{
            "code": TOO_LARGE, "message": f"the workflow has {len(graph.order)} nodes",
            "fix": f"keep it to {max_nodes} nodes or fewer",
        }])
    mapping = _assign_ids(graph, titles, notes, errors)
    if not errors:
        _check_reach(graph, mapping, notes, errors)
    if errors:
        return LintResult(None, notes, errors)
    canonical, nodes = _emit(graph, mapping, titles, notes)
    return LintResult(canonical, notes, [], nodes)


def linear(titles: list[str]) -> LintResult:
    """The plan in task order, drawn by the server when the model's drawing failed."""
    lines = ["flowchart TD", '  S(["시작"])']
    for i, title in enumerate(titles, start=1):
        lines.append(f'  T{i}["{escape_label(" ".join(str(title).split()))}"]')
    lines.append('  E(["끝"])')
    chain = ["S"] + [f"T{i}" for i in range(1, len(titles) + 1)] + ["E"]
    for a, b in zip(chain, chain[1:]):
        lines.append(f"  {a} --> {b}")
    nodes = [{"id": "S", "label": "시작", "shape": "stadium"}]
    nodes += [{"id": f"T{i}", "label": " ".join(str(t).split()), "shape": "rect", "task_id": i}
              for i, t in enumerate(titles, start=1)]
    nodes.append({"id": "E", "label": "끝", "shape": "stadium"})
    return LintResult("\n".join(lines), [], [], nodes)


def errors_text(errors: list[dict[str, Any]]) -> str:
    """The errors as one sentence each, for the hint."""
    parts = []
    for e in errors[:4]:
        fix = f" - {e['fix']}" if e.get("fix") else ""
        parts.append(f"{e.get('message', '')}{fix}")
    return " ".join(f"({i}) {p}." for i, p in enumerate(parts, start=1))


def node_ids(canonical: str) -> set[str]:
    """The ids a canonical workflow defines - what a page comment may name."""
    return {m.group(1) for m in re.finditer(r'^\s+([A-Za-z][A-Za-z0-9_]*)[\[({>]',
                                            canonical or "", re.M)}
