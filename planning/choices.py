"""Per-task alternatives: the model proposes, the human picks (2.0.0).

The model's `task_list` is its recommendation. For a task where the right way depends on
the user's preference it may add `alternatives` - other ways to do that one task - and
`recommended_reasons` - why it prefers its own. The approval page shows them as a choice
with the recommendation pre-selected; whatever the human picks becomes the task.

Each task's choice is stored as `options` (index 0 = the recommendation, 1.. = the
alternatives) and `chosen` (the index the human picked). Everything here is pure: it
validates what the model sent against the tasks it describes, and what the page sent
against the options it showed. Nothing raises.

Two guarantees live elsewhere but depend on this module being strict:
- after approval the model is told only the option that was chosen - an alternative it
  can still see is one it may still do (the D19 lesson);
- a choice the page posts must name an option that was actually on screen, or nothing is
  recorded (`validate_page_choices`).
"""

from __future__ import annotations

import re
from typing import Any

LETTERS = "ABCDEFGH"


def letter(index: int) -> str:
    """Option index -> the letter the human sees (A = the recommendation)."""
    return LETTERS[index] if 0 <= index < len(LETTERS) else str(index + 1)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().strip(" .!?,;:。")).lower()


def build_options(
    titles: list[str],
    alternatives: list[dict[str, Any]],
    reasons: dict[int, str],
    max_per_task: int,
    max_points: int,
    locked: set[int] | None = None,
) -> tuple[dict[int, list[dict[str, str]]], list[str]]:
    """Validate the model's alternatives against the task list they refer to.

    Returns ({task_id: options}, notes). task_id is 1-based into `titles`. A task in
    `locked` (already DONE, carried through a redraft) takes no alternatives: there is
    nothing left to choose about work that has been done.
    """
    notes: list[str] = []
    locked = locked or set()
    per_task: dict[int, list[dict[str, str]]] = {}
    dropped_ids: set[int] = set()
    for item in alternatives:
        tid = item.get("task_id")
        title = str(item.get("title") or "").strip()
        if not isinstance(tid, int) or not 1 <= tid <= len(titles):
            dropped_ids.add(tid)
            continue
        if tid in locked:
            notes.append(f"Task {tid} is already done, so its alternatives were ignored.")
            continue
        if not title or _key(title) == _key(titles[tid - 1]):
            continue
        bucket = per_task.setdefault(tid, [])
        if any(_key(o["title"]) == _key(title) for o in bucket):
            continue
        bucket.append({"title": title, "reason": str(item.get("reason") or "").strip()})
    if dropped_ids:
        listed = ", ".join(str(t) for t in sorted(dropped_ids, key=str))
        notes.append(
            f"Alternatives for task {listed} were ignored: task_id must be a task's number "
            f"in task_list (1 to {len(titles)})."
        )

    options: dict[int, list[dict[str, str]]] = {}
    for tid in sorted(per_task):
        alts = per_task[tid]
        if len(options) >= max_points > 0:
            notes.append(
                f"Only {max_points} tasks per plan may offer a choice; the alternatives for "
                f"task {tid} were ignored. Offer a choice only where the user's preference "
                "decides it."
            )
            continue
        if max_per_task > 0 and len(alts) > max_per_task:
            notes.append(
                f"Task {tid} had {len(alts)} alternatives; kept the first {max_per_task}."
            )
            alts = alts[:max_per_task]
        options[tid] = [{"title": titles[tid - 1], "reason": reasons.get(tid, "")}] + alts

    stray = sorted(t for t in reasons if t not in options)
    if stray:
        notes.append(
            "recommended_reasons for task(s) "
            + ", ".join(str(t) for t in stray)
            + " were ignored: there is no choice there, so nothing to recommend."
        )
    return options, notes


def collect_topics(
    alternatives: list[dict[str, Any]], options: dict[int, list[dict[str, str]]]
) -> dict[int, str]:
    """{task_id: what is being chosen}, for tasks that kept a choice.

    The first non-empty topic among a task's alternatives wins - the model is asked to
    send it once, but sending it on every alternative must not be an error.
    """
    topics: dict[int, str] = {}
    for item in alternatives:
        tid = item.get("task_id")
        topic = str(item.get("topic") or "").strip()
        if topic and tid in options and tid not in topics:
            topics[tid] = topic
    return topics


def choice_heading(topic: str | None, count: int) -> str:
    """The heading a choice is shown under: '집계 방식 · 3가지 중 선택'.

    Without a topic from the model it falls back to a neutral noun phrase - an
    instruction like "choose one of the following" says nothing about what is chosen.
    """
    return f"{(topic or '').strip() or '진행 방법'} · {count}가지 중 선택"


def validate_page_choices(
    tasks: list[dict[str, Any]], raw: Any
) -> dict[str, int] | None:
    """The page's choices against the options it was showing. None = refuse.

    Strict on purpose. The page only ever posts an index it rendered, so anything else
    is a stale tab or a forged request - and recording a choice that was never on
    screen would execute something nobody picked.
    """
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        return None
    shown = {
        str(t.get("task_id")): len(t.get("options") or [])
        for t in tasks
        if isinstance(t, dict) and len(t.get("options") or []) >= 2
    }
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            tid = str(int(str(key).strip()))
        except (TypeError, ValueError):
            return None
        if tid not in shown:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if not 0 <= value < shown[tid]:
            return None
        out[tid] = value
    return out


def validate_model_choices(
    options: dict[int, list[dict[str, str]]], raw: dict[str, int] | None
) -> tuple[dict[int, int], list[str]]:
    """Choices the model reported from a chat reply (no approval page).

    Lenient where the page path is strict: the model is relaying what the user said,
    and a mistake about one task should not throw away the others. Anything it got wrong
    falls back to the recommendation, with a note.
    """
    notes: list[str] = []
    out: dict[int, int] = {}
    for key, value in (raw or {}).items():
        try:
            tid = int(str(key))
        except (TypeError, ValueError):
            continue
        opts = options.get(tid)
        if not opts:
            notes.append(f"Task {tid} has no choice; ignored.")
            continue
        if not isinstance(value, int) or not 0 <= value < len(opts):
            notes.append(
                f"Task {tid} has options A to {letter(len(opts) - 1)}; the recommendation "
                "(A) was kept."
            )
            continue
        out[tid] = value
    return out, notes


__all__ = [
    "LETTERS",
    "build_options",
    "choice_heading",
    "collect_topics",
    "letter",
    "validate_model_choices",
    "validate_page_choices",
]
