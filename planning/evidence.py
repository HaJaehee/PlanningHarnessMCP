"""The verification contract: what "done" means, and what the server can check (3.0.0).

Until 3.0 the server judged a DONE by the shape of its `result_log` alone - not empty,
not "done", not the task title - and everything else was left to the human at the very
end, who had nothing to read the evidence against but the title. This module holds the
three pure pieces that move that boundary:

- `done_when`: one sentence per task saying what exists or is true when it is finished.
  The model proposes it, the human approves it with the plan (and may write it
  themselves), and the completion report is read against it.
- the repeat check: a result_log that only says the criterion back is not evidence.
- file checks: when a task says it produced a file, the server looks - inside the
  folders it was told it may look in, with `os.stat`, and nothing else.

Two rules shape all of it. Refuse only a claim that is structurally impossible - a file
that is not there, a sentence that adds nothing - and show everything else to the human:
a wrong refusal costs a weak model a retry loop. And every field added to help the model
do the work is equally a way to look as if it did (D19), so each one closes its laziest
use first.

Everything here is pure and never raises. Plan: docs/plan-3.0-verification-contract.md.
"""

from __future__ import annotations

import datetime
import os
import re
import threading
from pathlib import Path
from typing import Any

# What the server found for one file.
FOUND = "found"      # there, not empty, and changed since the task started
OLD = "old"          # there and not empty, but untouched since before the task started
FOLDER = "folder"    # a directory
EMPTY = "empty"      # there, with nothing in it
MISSING = "missing"  # inside an allowed folder, and not there
OUTSIDE = "outside"  # not inside any allowed folder - never looked at
UNKNOWN = "unknown"  # the check did not come back in time

# Where the path came from: the `files` argument, or the text of result_log.
DECLARED = "declared"
MENTIONED = "mentioned"

# A file the model declared and the server could not confirm refuses the DONE. A path
# merely mentioned in result_log never does: "deleted the temp file" is a true report
# about a file that is not there.
REFUSED_STATES = (MISSING, EMPTY)
CONFIRMED_STATES = (FOUND, OLD, FOLDER)

# Below this share of new text, the completion page marks the evidence as a near repeat
# of the criterion. Looser than the refusal threshold on purpose: the server refuses
# only what is plainly a repeat, and points the human at the rest.
ECHO_FLAG = 0.5

# How many characters a criterion needs, punctuation and spaces aside, to say anything.
_MIN_CRITERION = 4
# How many paths one result_log may contribute to the checks.
_MAX_MENTIONS = 6
# A file's mtime may trail the recorded start by this much and still count as changed:
# `started_at` is kept to the second, and some filesystems round mtimes.
_MTIME_SLACK_SEC = 2.0


def normalize_evidence(text: str) -> str:
    """Lowercase and strip whitespace/punctuation so claims can be compared by content."""
    return "".join(
        ch for ch in (text or "").lower() if not ch.isspace() and ch not in ".,!?;:-_…。、"
    )


# Criteria that only say the task gets done. Exact matches after normalization - a
# content filter, like handlers._EMPTY_CLAIMS, because length cannot tell "완료된다"
# from a real six-character Korean sentence.
_EMPTY_CRITERIA = {
    "done", "itisdone", "taskisdone", "thetaskisdone", "taskdone", "taskcompleted",
    "taskiscomplete", "thetaskiscomplete", "taskiscompleted", "thetaskiscompleted",
    "complete", "completed", "finished", "itisfinished", "taskisfinished",
    "thetaskisfinished", "success", "itworks", "workisdone", "theworkisdone",
    "everythingisdone", "allisdone", "ok",
    "완료", "완료된다", "완료됨", "완료한다", "작업완료", "작업이완료된다", "작업이완료됨",
    "작업을완료한다", "태스크가완료된다", "태스크완료", "끝", "끝난다", "작업이끝난다",
    "성공", "성공한다", "잘된다", "처리된다", "처리완료", "문제없다",
}


def clean_done_when(
    titles: list[str],
    items: dict[int, str],
    max_chars: int,
    locked: set[int] | None = None,
) -> tuple[dict[int, str], list[str]]:
    """Validate the model's criteria against the task list they number into.

    Returns ({task_id: criterion}, notes). A criterion that only says the task gets
    done - or repeats its title - is dropped: the page then shows "none" for that task,
    which is the truth and invites the human to write one.
    """
    locked = locked or set()
    notes: list[str] = []
    out: dict[int, str] = {}
    unknown: list[Any] = []
    vacuous: list[int] = []
    cut: list[int] = []
    for tid, raw in (items or {}).items():
        if not isinstance(tid, int) or isinstance(tid, bool) or not 1 <= tid <= len(titles):
            unknown.append(tid)
            continue
        if tid in locked:
            notes.append(f"Task {tid} is already done, so its done_when was ignored.")
            continue
        text = " ".join(str(raw or "").split())
        key = normalize_evidence(text)
        if (
            len(key) < _MIN_CRITERION
            or key in _EMPTY_CRITERIA
            or key == normalize_evidence(titles[tid - 1])
        ):
            vacuous.append(tid)
            continue
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars].rstrip()
            cut.append(tid)
        out[tid] = text
    if unknown:
        notes.append(
            "done_when for task "
            + ", ".join(str(t) for t in sorted(unknown, key=str))
            + f" was ignored: task_id must be a task's number in task_list (1 to {len(titles)})."
        )
    if vacuous:
        notes.append(
            "done_when for task(s) "
            + ", ".join(str(t) for t in sorted(vacuous))
            + " was ignored: it only says the task gets done. Say what will exist or be "
            "true - a file, a number, a table."
        )
    if cut:
        notes.append(
            "done_when for task(s) "
            + ", ".join(str(t) for t in sorted(cut))
            + f" was cut to {max_chars} characters. Keep it to one short sentence."
        )
    return out, notes


def _bigrams(text: str) -> set[str]:
    key = normalize_evidence(text)
    return {key[i:i + 2] for i in range(len(key) - 1)}


def novelty(done_when: str, result_log: str) -> float:
    """The share of the evidence that is not already in the criterion (0.0 - 1.0).

    Character bigrams, so it needs no tokenizer and works for Korean. Measured on
    hand-written cases (see tests): a criterion said back with only its tense changed
    scores 0.0-0.25; a report that adds a count, a size or a name scores 0.33 and up.
    """
    evidence = _bigrams(result_log)
    if not evidence:
        return 0.0
    return len(evidence - _bigrams(done_when)) / len(evidence)


def repeats_criterion(done_when: str | None, result_log: str, threshold: float) -> bool:
    """Is this result_log just the done_when sentence said back?"""
    if not done_when or threshold <= 0:
        return False
    return novelty(done_when, result_log) < threshold


def echoes_criterion(done_when: str | None, result_log: str | None) -> bool:
    """Close enough to the criterion that the human should look twice (page only)."""
    if not done_when or not (result_log or "").strip():
        return False
    return novelty(done_when, result_log or "") < ECHO_FLAG


# ---------------------------------------------------------------------------
# File checks
# ---------------------------------------------------------------------------

# A path as people write it in a sentence: an optional drive or leading slash, folders,
# and a name ending in an ASCII extension. Spaces are not allowed inside one - a path
# with spaces belongs in `files`, where it is not being pulled out of prose. Korean text
# directly after the extension ("summary.txt에") ends the match, which is what makes
# this usable on Korean result_logs at all.
_SEGMENT = r"[^\s\\/:*?\"<>|,;()\[\]{}'`]+"
_PATH = re.compile(
    r"(?<![\w/\\.])((?:[A-Za-z]:)?[\\/]?(?:" + _SEGMENT + r"[\\/])*"
    + _SEGMENT + r"\.[A-Za-z][A-Za-z0-9]{0,5})(?![A-Za-z0-9])"
)


def extract_paths(text: str) -> list[str]:
    """File paths mentioned in a result_log, in order, without repeats."""
    out: list[str] = []
    for match in _PATH.finditer(text or ""):
        path = match.group(1).rstrip(".")
        if path not in out:
            out.append(path)
        if len(out) >= _MAX_MENTIONS:
            break
    return out


def _fold(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(path: str, roots: tuple[Path, ...] | list[Path]) -> bool:
    """Lexically inside one of the allowed folders. Touches nothing on disk."""
    folded = _fold(path)
    for root in roots:
        base = _fold(str(root))
        if folded == base or folded.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False


def _candidates(raw: str, roots: tuple[Path, ...] | list[Path]) -> list[str]:
    """Every absolute path this text could mean, without looking at the disk.

    A full path means itself. A relative path is tried under each allowed folder. On
    Windows a rooted path with no drive ("/workspace/out.csv") names no real location -
    it is usually a sandbox's own view of its workspace - so it is tried under each
    allowed folder with its leading folders dropped one at a time, longest first.
    """
    text = (raw or "").strip().strip("\"'")
    if not text:
        return []
    drive, tail = os.path.splitdrive(text)
    rooted = tail.startswith(("/", "\\"))
    if rooted and (drive or os.name != "nt"):
        return [os.path.normpath(text)]
    if drive:
        return []  # "D:file.txt" - relative to a drive's current folder; not guessed at
    parts = [p for p in re.split(r"[\\/]+", tail) if p and p != "."]
    if not parts:
        return []
    tails = [parts[i:] for i in range(len(parts))] if rooted else [parts]
    return [
        os.path.normpath(os.path.join(str(root), *suffix))
        for suffix in tails
        for root in roots
    ]


def _stamp(seconds: float) -> str:
    return (
        datetime.datetime.fromtimestamp(seconds)
        .astimezone()
        .replace(microsecond=0)
        .isoformat()
    )


def _started_ts(started_at: str | None) -> float | None:
    if not started_at:
        return None
    try:
        moment = datetime.datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


def _check_one(
    raw: str, roots: tuple[Path, ...] | list[Path], started: float | None, source: str
) -> dict[str, Any]:
    """Look for one file. Never raises; never looks outside the allowed folders."""
    fact: dict[str, Any] = {"path": raw, "state": OUTSIDE, "source": source}
    allowed = [c for c in _candidates(raw, roots) if _inside(c, roots)]
    if not allowed:
        return fact
    fact["state"] = MISSING
    for candidate in allowed:
        try:
            # Follows links, so a link inside an allowed folder that points out of it
            # is caught here and not followed.
            real = os.path.realpath(candidate)
            if not _inside(real, roots):
                continue
            info = os.stat(real)
        except (OSError, ValueError):
            continue
        fact["mtime"] = _stamp(info.st_mtime)
        if os.path.isdir(real):
            fact["state"] = FOLDER
            return fact
        fact["size"] = int(info.st_size)
        if info.st_size == 0:
            fact["state"] = EMPTY
            continue  # an empty match under one root must not hide a real one under another
        changed = started is None or info.st_mtime >= started - _MTIME_SLACK_SEC
        fact["state"] = FOUND if changed else OLD
        return fact
    return fact


def check_files(
    paths: list[str],
    roots: tuple[Path, ...] | list[Path],
    started_at: str | None,
    source: str = DECLARED,
    timeout: float = 3.0,
) -> list[dict[str, Any]]:
    """What the server finds for each path. One fact per path, in order.

    Runs on a helper thread with a deadline: a stat on a network folder that has gone
    away can block for tens of seconds, and this is called inside the state lock. A
    path whose check does not come back is reported UNKNOWN - never MISSING, which
    would refuse a DONE over a slow disk.
    """
    wanted = [p for p in (str(p or "").strip() for p in paths) if p]
    if not wanted or not roots:
        return []
    started = _started_ts(started_at)
    facts: list[dict[str, Any] | None] = [None] * len(wanted)

    def work() -> None:
        for index, raw in enumerate(wanted):
            try:
                facts[index] = _check_one(raw, roots, started, source)
            except Exception:  # noqa: BLE001 - a check must never take a call down
                facts[index] = {"path": raw, "state": UNKNOWN, "source": source}

    worker = threading.Thread(target=work, name="file-check", daemon=True)
    worker.start()
    worker.join(timeout)
    return [
        fact if fact is not None else {"path": raw, "state": UNKNOWN, "source": source}
        for raw, fact in zip(wanted, facts)
    ]


def check_mentions(
    result_log: str,
    declared: list[str],
    roots: tuple[Path, ...] | list[Path],
    started_at: str | None,
    timeout: float = 3.0,
) -> list[dict[str, Any]]:
    """Files the result_log names that the server can confirm - and only those.

    A mention is prose, so only a positive finding is kept: a path that is not there
    may be exactly what the sentence says ("removed out/tmp.csv").
    """
    known = {_fold(p.strip().strip("\"'")) for p in declared}
    mentioned = [p for p in extract_paths(result_log) if _fold(p) not in known]
    facts = check_files(mentioned, roots, started_at, MENTIONED, timeout)
    return [f for f in facts if f.get("state") in CONFIRMED_STATES]


def refused(facts: list[dict[str, Any]]) -> list[str]:
    """The declared files that are not there."""
    return [
        str(f.get("path"))
        for f in facts
        if f.get("source") == DECLARED and f.get("state") in REFUSED_STATES
    ]


def confirmed(facts: list[dict[str, Any]]) -> bool:
    """The server saw at least one of these files, whenever it was written."""
    return any(f.get("state") in CONFIRMED_STATES and not f.get("gone") for f in facts)


def produced(facts: list[dict[str, Any]]) -> bool:
    """The server saw a file that this task itself created or changed.

    The stronger of the two: a file that was already there before the task started
    proves the file exists, not that the task did anything. Only this one may stand in
    for evidence the result_log does not give.
    """
    return any(
        f.get("state") in (FOUND, FOLDER) and not f.get("gone") for f in facts
    )


def describe_size(size: Any) -> str:
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def describe_check(fact: dict[str, Any]) -> str:
    """One check as the human reads it in the chat-mode completion report.

    The approval page renders the same facts itself (`checkLine` in approval.py); the
    wording is kept in step by tests.
    """
    name = os.path.basename(str(fact.get("path") or "").replace("\\", "/")) or str(
        fact.get("path") or ""
    )
    state = fact.get("state")
    size = describe_size(fact.get("size"))
    detail = f" · {size}" if size else ""
    if state == FOUND:
        return f"✔ {name}{detail} · 이 태스크 중 생성/변경됨"
    if state == OLD:
        return f"· {name}{detail} · 작업 전부터 있던 파일 (이 태스크에서 바뀌지 않음)"
    if state == FOLDER:
        return f"✔ {name} · 폴더가 있음"
    if state == EMPTY:
        return f"⚠ {name} · 빈 파일"
    if state == MISSING:
        return f"⚠ {name} · 찾을 수 없음"
    if state == UNKNOWN:
        return f"· {name} · 확인하지 못함 (응답 지연)"
    return f"· {name} · 서버의 확인 범위 밖"


def validate_page_criteria(tasks: list[dict[str, Any]], raw: Any) -> dict[str, str] | None:
    """The criteria the human wrote on the page, against the tasks it was showing.

    None = refuse the whole decision. Strict for the reason `validate_page_choices` is:
    the page only posts a task it rendered an editor for, so anything else is a stale
    tab or a forged request. An empty string is a real value - the human removed the
    criterion. Finished tasks take none: what "done" meant for work already done is not
    something to change afterwards.
    """
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        return None
    editable = {
        str(t.get("task_id"))
        for t in tasks
        if isinstance(t, dict) and t.get("status") != "DONE"
    }
    out: dict[str, str] = {}
    for key, value in raw.items():
        try:
            tid = str(int(str(key).strip()))
        except (TypeError, ValueError):
            return None
        if tid not in editable or not isinstance(value, str):
            return None
        out[tid] = " ".join(value.split())[:PAGE_CRITERION_CAP]
    return out


# The store has no configuration, so it enforces only an outer bound; the handler cuts
# to PLANNING_MCP_MAX_DONE_WHEN_CHARS when it applies the criterion.
PAGE_CRITERION_CAP = 500


__all__ = [
    "CONFIRMED_STATES",
    "DECLARED",
    "ECHO_FLAG",
    "EMPTY",
    "FOLDER",
    "FOUND",
    "MENTIONED",
    "MISSING",
    "OLD",
    "OUTSIDE",
    "PAGE_CRITERION_CAP",
    "REFUSED_STATES",
    "UNKNOWN",
    "check_files",
    "check_mentions",
    "clean_done_when",
    "confirmed",
    "describe_check",
    "describe_size",
    "echoes_criterion",
    "extract_paths",
    "normalize_evidence",
    "novelty",
    "produced",
    "refused",
    "repeats_criterion",
    "validate_page_criteria",
]
