"""The approval page's JavaScript, called from Python tests through node.

    from pagejs import NODE, page
    page().call("tookText", 125)          # '2분 5초'

The page is checked for what it draws, not for how its source is spelled: a render
function is called with a fixture and the HTML it returns is asserted on. That needs a
JavaScript engine, and the target machine has none, so every test that uses this is
skipped where node is missing (`skipUnless(NODE, ...)`) - the same rule the page's parse
check has always followed. One node process serves the whole test run.
"""

from __future__ import annotations

import atexit
import copy
import functools
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planning import approval  # noqa: E402

NODE = shutil.which("node")
# For a test that draws the page: skipped where there is no JavaScript engine to draw it.
needs_node = unittest.skipUnless(NODE, "node is not installed")
RUNNER = ROOT / "tests" / "web" / "runner.js"
GOLDEN = ROOT / "tests" / "web" / "golden.txt"
# What the page under test calls its own version. Fixtures marked "@page" get the same.
PAGE_VERSION = "9.9.9"

_spec = importlib.util.spec_from_file_location("page_preview", ROOT / "tools" / "page_preview.py")
page_preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(page_preview)


@functools.lru_cache(maxsize=1)
def _fixtures() -> dict[str, Any]:
    return page_preview.load_fixtures(PAGE_VERSION)


def fixtures() -> dict[str, Any]:
    """tests/web/fixtures.json, as a server of PAGE_VERSION would send it."""
    return copy.deepcopy(_fixtures())


def source(name: str) -> str:
    """One of the page's files as it is on disk. For what only a browser can run - what
    happens on a click, a keystroke, a redraw - the tests check the code that does it."""
    return (approval.WEB_DIR / name).read_text(encoding="utf-8")


def scripts() -> str:
    """All of the page's JavaScript, in load order."""
    return "".join(source(name) for name in approval.PAGE_SCRIPTS)


def by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every fixture by its card key: the request id, or run-<plan_id> for a run."""
    out = {d["id"]: d for d in data["requests"]}
    out.update({"run-" + r["plan_id"]: r for r in data["runs"]})
    return out


class PageJS:
    def __init__(self) -> None:
        scripts = [str(approval.WEB_DIR / name) for name in approval.PAGE_SCRIPTS]
        env = dict(os.environ, TZ="UTC", PAGE_VERSION=PAGE_VERSION)
        self.proc = subprocess.Popen(
            [NODE, str(RUNNER), *scripts], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", env=env, cwd=str(ROOT),
        )

    def _ask(self, request: dict[str, Any]) -> Any:
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the page runner exited")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["ok"]

    def call(self, fn: str, *args: Any) -> Any:
        return self._ask({"fn": fn, "args": list(args)})

    def get(self, name: str) -> Any:
        return self._ask({"get": name})

    def set(self, name: str, value: Any) -> None:
        self._ask({"set": name, "value": value})

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)


_page: PageJS | None = None


def page() -> PageJS:
    global _page
    if _page is None:
        _page = PageJS()
        atexit.register(_page.close)
    return _page


# ---- what the page draws -------------------------------------------------------
# One function per thing the page draws, so the tests do not depend on how the page's
# functions are called - only these do.


def card(item: dict[str, Any]) -> str:
    """The HTML of one card: a request, or a run (it has no "id")."""
    if "id" in item:
        return page().call("requestCard", item)
    return page().call("runCard", item)


def hint(requests: list[dict[str, Any]], runs: list[dict[str, Any]]) -> str:
    """The notes under the cards, for this queue and these runs."""
    return page().call("hintHtml", requests, runs)


def idle() -> str:
    return page().get("IDLE_HTML")


def card_of(key: str, **changes: Any) -> Node:
    """A fixture's card, drawn and parsed; `changes` replace fields of the fixture."""
    return parse(card(dict(by_id(fixtures())[key], **changes)))


def rows_of(key: str, **changes: Any) -> list[Node]:
    """The task rows of a fixture's card."""
    return card_of(key, **changes).one(cls="tasks").elements()


# Each data-act a card may carry, and the data-* attributes app.js passes to it.
ACTIONS = {
    "decide": ("data-id", "data-decision"),
    "control": ("data-plan", "data-control"),
    "toggleComment": (),
    "toggleCriterion": ("data-id", "data-task"),
    "editCriterion": ("data-id", "data-task"),
}


def action_of(button: Node) -> tuple[str, ...]:
    """What a button does when clicked, as (what, *its arguments): ("decide", "r1plan",
    "APPROVED"), ("control", plan_id, "CANCEL"), ("toggleComment",) and so on - read from
    its data-act and the data-* attributes app.js hands to the function of that name."""
    act = button.attrs.get("data-act")
    if act is None:
        raise AssertionError(f"<{button.tag} class={button.classes}> does nothing on a click")
    if act not in ACTIONS:
        raise AssertionError(f"no action is called {act!r}")
    return (act, *(button.attrs[name] for name in ACTIONS[act]))


def draw_all() -> dict[str, str]:
    """Everything the fixtures draw, by name, in a fixed order."""
    data = fixtures()
    out = {key: card(item) for key, item in by_id(data).items()}
    requests, runs = data["requests"], data["runs"]
    completion = [d for d in requests if d["phase"] == "COMPLETION"]
    out["hint:all"] = hint(requests, runs)
    out["hint:runs-only"] = hint([], runs)
    out["hint:requests-only"] = hint(requests, [])
    out["hint:completion-only"] = hint(completion, [])
    out["idle"] = idle()
    return out


def golden_text(cards: dict[str, str]) -> str:
    """The drawn HTML as tests/web/golden.txt keeps it: one tag per line, so a change to
    what the page draws shows in a diff as the lines it changed. Line breaks inside the
    HTML itself are written as \\n, so every line of the file is one line of markup."""
    blocks = []
    for name, html in cards.items():
        body = html.replace("\\", "\\\\").replace("\n", "\\n").replace("><", ">\n<")
        blocks.append(f"=== {name}\n{body}\n")
    return "".join(blocks)


def golden_blocks(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for block in text.split("=== ")[1:]:
        name, _, body = block.partition("\n")
        out[name] = body
    return out


# ---- reading what was drawn ------------------------------------------------------
# Drawn HTML parsed into a small tree, so a test can ask for "the button with id ok-x"
# or "the text of .note" without caring about attribute order or how the markup is
# spelled. Text and attribute values come back unescaped: a "<b>" in .text() is a "<b>"
# the human reads, while a real <b> element would be a node.

_VOID = {"br", "input", "meta", "img", "hr", "link"}


class Node:
    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def nodes(self) -> Iterator[Node]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.nodes()

    def elements(self) -> list[Node]:
        """Child elements, without the text between them."""
        return [c for c in self.children if isinstance(c, Node)]

    def find_all(self, tag: str | None = None, cls: str | None = None,
                 **attrs: str) -> list[Node]:
        """Descendants matching all of: tag, a class (space-separated = all of them), and
        attributes (underscores in names stand for dashes: data_req -> data-req)."""
        wanted = (cls or "").split()
        out = []
        for node in self.nodes():
            if tag and node.tag != tag:
                continue
            if any(c not in node.classes for c in wanted):
                continue
            if any(node.attrs.get(k.replace("_", "-")) != v for k, v in attrs.items()):
                continue
            out.append(node)
        return out

    def one(self, tag: str | None = None, cls: str | None = None, **attrs: str) -> Node:
        found = self.find_all(tag, cls, **attrs)
        if len(found) != 1:
            raise AssertionError(f"expected one {tag or ''}.{cls or ''} {attrs}, "
                                 f"found {len(found)}")
        return found[0]

    def has(self, tag: str | None = None, cls: str | None = None, **attrs: str) -> bool:
        return bool(self.find_all(tag, cls, **attrs))


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: "" if v is None else v for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        raise AssertionError(f"</{tag}> closes nothing")

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    if len(builder.stack) != 1:
        raise AssertionError(f"unclosed <{builder.stack[-1].tag}>")
    return builder.root
