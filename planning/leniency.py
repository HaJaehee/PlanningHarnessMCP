"""Input leniency layer - invisible to the LLM, applied before validation.

Weak models produce near-miss arguments: "done" instead of "DONE", "3" instead of 3,
a newline-joined string instead of an array. Rejecting those wastes a turn and often
makes the model abandon the protocol. This module repairs what it can and records
what it repaired; it never raises and never rejects.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import Decision, TaskStatus

_ALLOWED_KEYS: dict[str, set[str]] = {
    "plan_and_think": {
        "goal",
        "revised_goal",
        "thought",
        "step_number",
        "total_steps",
        "need_more_thinking",
        "task_list",
        "task_updates",
        "revises_step",
        "plan_id",
        "alternatives",
        "recommended_reasons",
    },
    "request_user_approval": {"decision", "plan_summary", "user_comment", "plan_id", "choices"},
    "update_task_progress": {"task_id", "status", "result_log", "plan_id"},
    "get_current_plan": {"plan_id"},
}

_STATUS_ALIASES = {
    "done": TaskStatus.DONE,
    "complete": TaskStatus.DONE,
    "completed": TaskStatus.DONE,
    "finished": TaskStatus.DONE,
    "finish": TaskStatus.DONE,
    "success": TaskStatus.DONE,
    "완료": TaskStatus.DONE,
    "in progress": TaskStatus.IN_PROGRESS,
    "inprogress": TaskStatus.IN_PROGRESS,
    "in_progress": TaskStatus.IN_PROGRESS,
    "started": TaskStatus.IN_PROGRESS,
    "start": TaskStatus.IN_PROGRESS,
    "starting": TaskStatus.IN_PROGRESS,
    "doing": TaskStatus.IN_PROGRESS,
    "running": TaskStatus.IN_PROGRESS,
    "진행중": TaskStatus.IN_PROGRESS,
    "fail": TaskStatus.FAILED,
    "failed": TaskStatus.FAILED,
    "failure": TaskStatus.FAILED,
    "error": TaskStatus.FAILED,
    "실패": TaskStatus.FAILED,
    "pending": TaskStatus.PENDING,
    "todo": TaskStatus.PENDING,
    "not started": TaskStatus.PENDING,
    "대기": TaskStatus.PENDING,
}

_DECISION_ALIASES = {
    "ask": Decision.ASK_USER,
    "ask user": Decision.ASK_USER,
    "ask_user": Decision.ASK_USER,
    "request": Decision.ASK_USER,
    "확인": Decision.ASK_USER,
    "yes": Decision.APPROVED,
    "y": Decision.APPROVED,
    "ok": Decision.APPROVED,
    "okay": Decision.APPROVED,
    "approve": Decision.APPROVED,
    "approved": Decision.APPROVED,
    "accept": Decision.APPROVED,
    "proceed": Decision.APPROVED,
    "go": Decision.APPROVED,
    "승인": Decision.APPROVED,
    "네": Decision.APPROVED,
    "예": Decision.APPROVED,
    "진행": Decision.APPROVED,
    "좋아요": Decision.APPROVED,
    "no": Decision.REJECTED,
    "n": Decision.REJECTED,
    "cancel": Decision.REJECTED,
    "cancelled": Decision.REJECTED,
    "reject": Decision.REJECTED,
    "rejected": Decision.REJECTED,
    "stop": Decision.REJECTED,
    "deny": Decision.REJECTED,
    "취소": Decision.REJECTED,
    "아니오": Decision.REJECTED,
    "아니요": Decision.REJECTED,
    "하지마": Decision.REJECTED,
    "거부": Decision.REJECTED,
    "revise": Decision.REVISE,
    "revised": Decision.REVISE,
    "change": Decision.REVISE,
    "modify": Decision.REVISE,
    "edit": Decision.REVISE,
    "update": Decision.REVISE,
    "수정": Decision.REVISE,
    "변경": Decision.REVISE,
}

_TRUE_WORDS = {"true", "1", "yes", "y", "on", "continue", "네", "예"}
_FALSE_WORDS = {"false", "0", "no", "n", "off", "done", "아니오", "아니요"}

# Strips "1. ", "2) ", "- ", "* ", "• " but leaves ordinary prose untouched.
_NUMBERING = re.compile(r"^\s*(?:[-*•·]|\(?\d{1,2}[.)\]])\s+")
_TITLE_KEYS = ("title", "name", "task", "text", "description", "step", "action")


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_WORDS:
            return True
        if v in _FALSE_WORDS:
            return False
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        m = re.search(r"-?\d+", value)
        if m:
            try:
                return int(m.group())
            except ValueError:
                return None
    return None


def _clean_title(raw: Any) -> str | None:
    if isinstance(raw, dict):
        for key in _TITLE_KEYS:
            if isinstance(raw.get(key), str) and raw[key].strip():
                raw = raw[key]
                break
        else:
            return None
    if not isinstance(raw, str):
        return None
    text = _NUMBERING.sub("", raw).strip()
    return text or None


def _coerce_task_list(value: Any) -> list[str] | None:
    items: list[Any]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "\n" in text:
            items = text.split("\n")
        elif ";" in text:
            items = text.split(";")
        elif "," in text:
            items = text.split(",")
        else:
            items = [text]
    elif isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        # {"1": "do a", "2": "do b"} - seen from models that mimic JSON objects
        items = [value[k] for k in sorted(value, key=lambda k: str(k))]
    else:
        return None
    cleaned = [t for t in (_clean_title(i) for i in items) if t]
    return cleaned


_TASK_ID_KEYS = ("task_id", "taskId", "taskid", "id", "task_number", "number", "task")

# Titles that arrived with no task_id. This layer is stateless and cannot know which
# task the human flagged, so it never guesses - it hands them to handlers.py, which
# pairs them only when the plan makes the pairing unambiguous.
UNMATCHED_TITLES_KEY = "_unmatched_titles"


def _coerce_task_updates(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Into ([{"task_id": int, "title": str}], [titles with no id]).

    Weak models express "change task 3 to X" every way imaginable: a bare object, a
    JSON string, or {"3": "X"}. Each of those is unambiguous, so each is accepted.
    An entry with a title but no id is NOT guessed at here - inventing a task_id would
    rewrite a task the human never flagged - so it goes to the second list instead.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return [], []
        try:
            return _coerce_task_updates(json.loads(text))
        except ValueError:
            # Not JSON, so it is prose: the rewritten task itself, sent bare. Useless on
            # its own, usable when exactly one task was flagged.
            title = _clean_title(text)
            return [], ([title] if title else [])

    items: list[Any]
    if isinstance(value, dict):
        if any(k in value for k in _TASK_ID_KEYS[:4]):
            items = [value]  # one bare update, not wrapped in a list
        else:
            items = []
            for key, entry in value.items():  # {"3": "new title"} / {"3": {...}}
                if isinstance(entry, dict):
                    merged = dict(entry)
                    merged.setdefault("task_id", key)
                    items.append(merged)
                else:
                    items.append({"task_id": key, "title": entry})
    elif isinstance(value, list):
        items = value
    else:
        return [], []

    # Keyed by task_id so a model that repeats itself does not queue the same edit twice.
    out: dict[int, dict[str, Any]] = {}
    unmatched: list[str] = []
    for item in items:
        if isinstance(item, dict):
            raw_id = next((item[k] for k in _TASK_ID_KEYS if item.get(k) is not None), None)
            task_id = _coerce_int(raw_id)
        else:
            task_id = None  # e.g. task_updates=["the rewritten task"]
        title = _clean_title(item)
        if not title:
            continue
        if task_id is None or task_id < 1:
            unmatched.append(title)
            continue
        out[task_id] = {"task_id": task_id, "title": title}
    return list(out.values()), unmatched


# ---- alternatives (2.0.0) ---------------------------------------------------
# Narrower than _TASK_ID_KEYS on purpose: in an alternative, "task" is far more often the
# alternative's text than its id, and reading digits out of "Q3 report" would attach it
# to task 3.
_ALT_ID_KEYS = ("task_id", "taskId", "taskid", "id", "task_number", "number")
_ALT_TITLE_KEYS = ("title", "option", "alternative", "text", "name", "task", "description")
_REASON_KEYS = ("reason", "why", "tradeoff", "trade_off", "because", "note", "pros_cons")
# What is being chosen ("집계 방식"), shown as the heading of the choice. "label" is left
# out on purpose: a model is as likely to put the alternative itself there.
_TOPIC_KEYS = ("topic", "header", "heading", "subject", "question", "choice_topic")
# "2: export to CSV", "task 2 - export to CSV", "2) export to CSV"
_ID_PREFIX = re.compile(r"^\s*(?:task\s*)?(\d{1,2})\s*[:.)\]\-–—]\s*(.+?)\s*$", re.IGNORECASE)


def _as_items(value: Any) -> list[Any]:
    """Lists, JSON strings, line-separated strings and {"2": ...} maps, as one list."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return _as_items(json.loads(text))
        except ValueError:
            return [line for line in re.split(r"[\n;]+", text) if line.strip()]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if any(k in value for k in _ALT_ID_KEYS):
            return [value]
        items: list[Any] = []
        for key, entry in value.items():  # {"2": "...", "3": [...], "4": {...}}
            for one in entry if isinstance(entry, list) else [entry]:
                if isinstance(one, dict):
                    merged = dict(one)
                    merged.setdefault("task_id", key)
                    items.append(merged)
                else:
                    items.append({"task_id": key, "title": one, "reason": one})
        return items
    return []


def _first_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _coerce_alternatives(value: Any) -> list[dict[str, Any]]:
    """Into [{"task_id": int, "title": str, "reason": str}]. Entries with no task id or no
    text are dropped: which task an alternative belongs to is never guessed."""
    out: list[dict[str, Any]] = []
    for item in _as_items(value):
        if isinstance(item, str):
            m = _ID_PREFIX.match(item)
            if not m:
                continue
            item = {"task_id": m.group(1), "title": m.group(2)}
        if not isinstance(item, dict):
            continue
        raw_id = next((item[k] for k in _ALT_ID_KEYS if item.get(k) is not None), None)
        tid = _coerce_int(raw_id) if not isinstance(raw_id, str) or raw_id.strip().isdigit() \
            else None
        title = _first_text(item, _ALT_TITLE_KEYS)
        if tid is None or tid < 1 or not title:
            continue
        reason = _first_text(item, _REASON_KEYS)
        entry = {"task_id": tid, "title": title, "reason": "" if reason == title else reason}
        topic = _first_text(item, _TOPIC_KEYS)
        if topic and topic != title:
            entry["topic"] = topic
        out.append(entry)
    return out


def _coerce_reasons(value: Any) -> dict[int, str]:
    """Into {task_id: reason}."""
    out: dict[int, str] = {}
    for item in _as_items(value):
        if isinstance(item, str):
            m = _ID_PREFIX.match(item)
            if not m:
                continue
            item = {"task_id": m.group(1), "reason": m.group(2)}
        if not isinstance(item, dict):
            continue
        raw_id = next((item[k] for k in _ALT_ID_KEYS if item.get(k) is not None), None)
        tid = _coerce_int(raw_id) if not isinstance(raw_id, str) or raw_id.strip().isdigit() \
            else None
        reason = _first_text(item, _REASON_KEYS + ("title", "text"))
        if tid and tid > 0 and reason:
            out[tid] = reason
    return out


_RECOMMENDED_WORDS = {"recommended", "default", "권장", "권장안", "기본"}


def _choice_index(value: Any) -> int | None:
    """"A" / "b" / "B안" / "option C" / "recommended" -> 0-based index.

    A bare number is refused on purpose: "1" could mean the first option (A) or index 1
    (B), and the server does not guess which option a user chose.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in _RECOMMENDED_WORDS:
        return 0
    m = re.fullmatch(r"(?:option\s*)?([a-h])\s*(?:안|案)?", text)
    return "abcdefgh".index(m.group(1)) if m else None


def _coerce_choices(value: Any) -> tuple[dict[str, int], list[str]]:
    out: dict[str, int] = {}
    bad: list[str] = []
    for item in _as_items(value):
        if isinstance(item, dict):
            raw_id = next((item[k] for k in _ALT_ID_KEYS if item.get(k) is not None), None)
            pick = next((item[k] for k in ("choice", "option", "pick", "title", "reason")
                         if item.get(k) is not None), None)
        else:
            continue
        tid = _coerce_int(raw_id)
        index = _choice_index(pick)
        if tid is None or tid < 1:
            continue
        if index is None:
            bad.append(str(tid))
            continue
        out[str(tid)] = index
    notes = (
        [f"Could not read the choice for task(s) {', '.join(bad)}. Use letters: A is the "
         "recommendation, B / C / D the alternatives."]
        if bad else []
    )
    return out, notes


def _normalize_enum(value: Any, aliases: dict[str, Any]) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if not key:
        return None
    direct = key.upper().replace(" ", "_").replace("-", "_")
    known = {m.value for m in TaskStatus} | {m.value for m in Decision}
    if direct in known:
        return direct
    hit = aliases.get(key) or aliases.get(key.replace("_", " "))
    return hit.value if hit else None


def normalize(tool_name: str, args: Any) -> tuple[dict[str, Any], list[str]]:
    """Repair a raw arguments dict. Returns (clean_args, notes)."""
    notes: list[str] = []
    if not isinstance(args, dict):
        return {}, ["Arguments were not a JSON object; treated as empty."]

    allowed = _ALLOWED_KEYS.get(tool_name, set())
    clean: dict[str, Any] = {}
    for key, value in args.items():
        if key in allowed:
            clean[key] = value
            continue
        # JSON keys are strings, but a crafted client could send otherwise; a
        # non-string key must be dropped, not crash the whole call.
        if not isinstance(key, str):
            notes.append(f"Ignored non-text parameter key {key!r}.")
            continue
        # Common misspellings weak models produce for the id fields.
        lowered = key.lower().replace("-", "_")
        remap = {
            "taskid": "task_id",
            "task_number": "task_id",
            "id": "task_id",
            "stepnumber": "step_number",
            "step": "step_number",
            "totalsteps": "total_steps",
            "needmorethinking": "need_more_thinking",
            "tasklist": "task_list",
            "tasks": "task_list",
            "planid": "plan_id",
            "newgoal": "revised_goal",
            "correctedgoal": "revised_goal",
            "updatedgoal": "revised_goal",
            "changedgoal": "revised_goal",
            "goalrevision": "revised_goal",
            "revisegoal": "revised_goal",
            "summary": "plan_summary",
            "comment": "user_comment",
            "log": "result_log",
            "result": "result_log",
        }.get(lowered.replace("_", ""), remap_direct(lowered, allowed))
        if remap and remap in allowed and remap not in clean:
            clean[remap] = value
            notes.append(f"Renamed unknown parameter '{key}' to '{remap}'.")
        else:
            notes.append(f"Ignored unknown parameter '{key}'.")

    for int_key in ("step_number", "total_steps", "task_id", "revises_step"):
        if int_key in clean:
            coerced = _coerce_int(clean[int_key])
            if coerced is None:
                clean.pop(int_key)
                notes.append(f"Could not read '{int_key}' as a number; ignored it.")
            elif coerced != clean[int_key]:
                notes.append(f"Read '{int_key}' as the number {coerced}.")
                clean[int_key] = coerced

    if "need_more_thinking" in clean:
        coerced_bool = _coerce_bool(clean["need_more_thinking"])
        if coerced_bool is None:
            clean.pop("need_more_thinking")
            notes.append("Could not read 'need_more_thinking' as true/false; ignored it.")
        elif coerced_bool is not clean["need_more_thinking"]:
            notes.append(f"Read 'need_more_thinking' as {str(coerced_bool).lower()}.")
            clean["need_more_thinking"] = coerced_bool

    if "task_list" in clean:
        coerced_list = _coerce_task_list(clean["task_list"])
        if coerced_list is None:
            clean.pop("task_list")
            notes.append("Could not read 'task_list' as a list of strings; ignored it.")
        else:
            if coerced_list != clean["task_list"]:
                notes.append(f"Normalized 'task_list' into {len(coerced_list)} plain strings.")
            clean["task_list"] = coerced_list

    if "task_updates" in clean:
        raw_updates = clean["task_updates"]
        coerced_updates, unmatched_titles = _coerce_task_updates(raw_updates)
        if coerced_updates:
            if coerced_updates != raw_updates:
                notes.append(
                    f"Normalized 'task_updates' into {len(coerced_updates)} "
                    "{task_id, title} entries."
                )
            clean["task_updates"] = coerced_updates
        else:
            clean.pop("task_updates")
            if raw_updates:
                notes.append(
                    "Could not read 'task_updates'; ignored it. Expected "
                    '[{"task_id": 3, "title": "the rewritten task"}].'
                )
        if unmatched_titles:
            clean[UNMATCHED_TITLES_KEY] = unmatched_titles

    if "alternatives" in clean:
        raw = clean["alternatives"]
        coerced_alts = _coerce_alternatives(raw)
        if coerced_alts:
            clean["alternatives"] = coerced_alts
        else:
            clean.pop("alternatives")
            if raw:
                notes.append(
                    "Could not read 'alternatives'; ignored it. Expected "
                    '[{"task_id": 2, "title": "the other way", "reason": "why"}].'
                )

    if "recommended_reasons" in clean:
        raw = clean["recommended_reasons"]
        coerced_reasons = _coerce_reasons(raw)
        if coerced_reasons:
            clean["recommended_reasons"] = coerced_reasons
        else:
            clean.pop("recommended_reasons")
            if raw:
                notes.append(
                    "Could not read 'recommended_reasons'; ignored it. Expected "
                    '[{"task_id": 2, "reason": "why you recommend it"}].'
                )

    if "choices" in clean:
        coerced_choices, choice_notes = _coerce_choices(clean["choices"])
        notes.extend(choice_notes)
        if coerced_choices:
            clean["choices"] = coerced_choices
        else:
            clean.pop("choices")

    if "status" in clean:
        normalized = _normalize_enum(clean["status"], _STATUS_ALIASES)
        if normalized and normalized in {s.value for s in TaskStatus}:
            if normalized != clean["status"]:
                notes.append(f"Read status '{clean['status']}' as '{normalized}'.")
            clean["status"] = normalized

    if "decision" in clean:
        normalized = _normalize_enum(clean["decision"], _DECISION_ALIASES)
        if normalized and normalized in {d.value for d in Decision}:
            if normalized != clean["decision"]:
                notes.append(f"Read decision '{clean['decision']}' as '{normalized}'.")
            clean["decision"] = normalized

    for str_key in (
        "goal", "revised_goal", "thought", "plan_summary", "user_comment", "result_log", "plan_id",
    ):
        if str_key in clean and clean[str_key] is not None and not isinstance(clean[str_key], str):
            clean[str_key] = str(clean[str_key])
            notes.append(f"Converted '{str_key}' to text.")

    return clean, notes


def remap_direct(lowered: str, allowed: set[str]) -> str | None:
    """Last-chance match: a key that already equals an allowed name apart from case."""
    return lowered if lowered in allowed else None
