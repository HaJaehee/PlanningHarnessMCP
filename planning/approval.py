"""Human approval: shared file-backed state plus a single localhost page over it.

Why the state is in a file
--------------------------
Approval used to live in process memory. Restarts leave old MCP server processes alive
on the same state directory, so each one bound its own port and served its own page. The
human had one tab open - usually on the first port - and any approval request raised by
another process appeared on a port nobody was looking at, then timed out. The gate
silently degraded to "the model asked and nothing stopped it", which is the exact failure
this whole subsystem exists to prevent.

So the request and the decision live in `state/approval.json`, and the page is just a
view over that file. Any process can publish a request; any process can read the
decision. Reads take no lock (writes are atomic rename, so a reader never sees a torn
file); only writers serialize.

Why exactly one page
--------------------
The URL has to be stable, because a human keeps that tab open. Only the base port is
ever bound. If it is already taken, we check whether the occupant is another
planning-mcp on the *same* state directory - if so it is already serving our requests
and we need no page of our own. A background thread keeps retrying the bind, so if the
owner exits another process takes the same port over and the open tab keeps working.
"""

from __future__ import annotations

import html
import json
import logging
import os
import socket
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from .choices import validate_page_choices
from .config import SERVER_AUTHOR, SERVER_AUTHOR_EMAIL, SERVER_VERSION
from .evidence import validate_page_criteria
from .filelock import exclusive

log = logging.getLogger("planning-mcp.approval")

DECISIONS = ("APPROVED", "REJECTED", "REVISE")
APPROVAL_FILENAME = "approval.json"
APPROVAL_LOCK_FILENAME = ".approvallock"
PAGE_SEEN_FILENAME = "page_seen"
SERVER_SIGNATURE = "planning-mcp-approval"

# Which question the human is being asked. A PLAN request is a task list they may
# comment on line by line; a COMPLETION request is a report on work already done, where
# per-task rewriting is meaningless - the plan is right, the execution is in dispute.
PHASE_PLAN = "PLAN"
PHASE_COMPLETION = "COMPLETION"
# The circuit breaker stopped an agent that kept repeating itself (1.16.0, D25). Not a
# question about the plan's content: the human decides whether to approve the draft as
# it stands, let the agent continue (optionally with a direction), or cancel. Decisions
# reuse the three existing values - APPROVED / REVISE / REJECTED - so a page served by an
# older process, which renders this entry with its fallback form, still produces a
# decision the new handler can read.
PHASE_HALT = "HALT"
PHASES = (PHASE_PLAN, PHASE_COMPLETION, PHASE_HALT)

# No poll for this long means no tab is open.
PAGE_IDLE_SEC = 10.0
# How often the process serving the page records that a tab polled it. The record is
# shared through the state directory because the process that opens browsers is often not
# the process that owns the page - see `page_is_being_watched`.
PAGE_SEEN_WRITE_SEC = 2.0
# How long a browser we just launched is given to load the page and poll once before we
# are willing to launch another. Covers the window where no tab is polling yet but one is
# on its way, which is the only reason to suppress an open that `PAGE_IDLE_SEC` misses.
OPEN_GRACE_SEC = 10.0
# If a launch never results in a tab polling us, the next wait doubles, up to this. A
# browser that cannot open (corporate policy, no default handler) otherwise gets launched
# again on every slice of the chunked wait, forever, with nothing to show for it.
OPEN_GRACE_MAX_SEC = 600.0

# How stale `agent_last_seen` may get before the page stops claiming an agent is still
# waiting. Comfortably above the waiter's own 10s touch interval, so an ordinary pause
# between touches never flickers the chip.
AGENT_IDLE_SEC = 25.0

# How much of the plan one REVISE is allowed to change.
SCOPE_PLAN = "PLAN"    # rewrite the whole breakdown (the original behaviour)
SCOPE_TASKS = "TASKS"  # rewrite only the tasks the human commented on
SCOPES = (SCOPE_PLAN, SCOPE_TASKS)


@dataclass
class Verdict:
    """What the human decided, and how far it reaches.

    A plain tuple grew a third and fourth member as the page learned to carry per-task
    feedback, and every silent arity change is a chance to drop the human's words on the
    floor. Named fields make an omission a visible error instead.
    """

    decision: str
    comment: str = ""
    task_comments: dict[str, str] = field(default_factory=dict)
    scope: str = SCOPE_PLAN
    # 2.0.0 - {task_id: option index} for the tasks that offered a choice. Validated
    # against the options that were on screen before it is ever recorded.
    choices: dict[str, int] = field(default_factory=dict)
    # 3.0.0 - {task_id: done_when} for the tasks whose criterion the human wrote or
    # rewrote on the page. "" means they removed it. Validated the same way.
    criteria: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------


class ApprovalStore:
    """The approval request and its decision, shared by every server process."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def path(self) -> Path:
        return self.state_dir / APPROVAL_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.state_dir / APPROVAL_LOCK_FILENAME

    @property
    def page_seen_path(self) -> Path:
        return self.state_dir / PAGE_SEEN_FILENAME

    # ---- page liveness -------------------------------------------------
    # Whether a tab is open is a property of the *machine*, not of one process, so it
    # lives in the state directory with everything else that is shared. No lock and no
    # atomic replace: a torn or lost write costs one stale liveness reading, and the
    # cost of getting it wrong is bounded in both directions (one extra tab, or one
    # slice's delay before reopening).
    def mark_page_seen(self, now: float | None = None) -> None:
        try:
            self.page_seen_path.write_text(repr(now or time.time()), encoding="ascii")
        except OSError:
            pass

    def page_last_seen(self) -> float:
        try:
            return float(self.page_seen_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return 0.0

    # ---- io -----------------------------------------------------------
    def read(self) -> dict[str, Any]:
        """Current record, or {}. Lock-free: writes are atomic."""
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _write(self, record: dict[str, Any]) -> bool:
        """Returns whether the record actually reached disk.

        Callers must propagate a False: a request that was never persisted cannot be
        answered by anyone, so reporting success would leave the gate silently disarmed.
        """
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.error("Could not persist approval state: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    # ---- operations ---------------------------------------------------
    def _requests(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return list(record.get("requests") or [])

    def publish(
        self,
        plan_id: str,
        goal: str,
        display: str,
        tasks: list[dict[str, Any]],
        fingerprint: str,
        phase: str = PHASE_PLAN,
        summary: str = "",
        draft: bool = False,
        origin: str = "",
    ) -> str | None:
        """Queue a request for the human. Returns its id, or None if it could not be saved.

        Concurrent sessions each get their own entry: one queue slot per plan would make
        two sessions asking at once hide each other. A new request for the SAME plan
        replaces that plan's old entry (the plan was revised), never another plan's.

        Re-asking about an UNCHANGED plan is not a new question, so it reuses the entry
        that is already on screen. A chunked wait re-publishes every 45 seconds, and
        minting a fresh id each time would be destructive in three separate ways: the
        page's signature would change and wipe the card - taking any half-written
        comment with it - the alarm would re-fire, and `created_at` would reset, so the
        total wait budget could never actually run out.
        """
        request_id = uuid.uuid4().hex
        entry = {
            "id": request_id,
            "plan_id": plan_id,
            "goal": goal,
            # The model's plain-language overview. It used to reach the human only inside
            # `display`; once the page started rendering tasks as rows that blob was no
            # longer shown, and the overview vanished with it. It is its own field now.
            "summary": summary,
            "display": display,
            "tasks": tasks,
            "fingerprint": fingerprint,
            "phase": phase if phase in PHASES else PHASE_PLAN,
            # HALT only: whether the card carries a task list the human can approve as
            # it stands. Without one, lifting the halt can only mean "continue".
            "draft": bool(draft),
            # HALT only: "user" when the human stopped a running plan themselves (3.1.0)
            # rather than the circuit breaker stopping a loop. Changes the card's words.
            "origin": str(origin or ""),
            "created_at": time.time(),
            "created_by_pid": os.getpid(),
            "decision": None,
            "comment": "",
            "task_comments": {},
            "scope": SCOPE_PLAN,
            "choices": {},
            "criteria": {},
            "decided_at": None,
            # Which version of the server asked. The page is served by one process and
            # asked by any of them, and after an upgrade the two are not always the same
            # code (a process keeps the modules it imported until it is restarted). The
            # page shows its own version, and says so when a request came from another.
            "server_version": SERVER_VERSION,
        }
        with exclusive(self.lock_path) as got:
            record = self.read()
            queue = self._requests(record)
            # Checked under the same lock as the write, so two threads asking at once
            # cannot both decide the entry is missing and mint one each.
            for candidate in queue:
                if (
                    candidate.get("plan_id") == plan_id
                    and candidate.get("fingerprint") == fingerprint
                    and candidate.get("decision") is None
                ):
                    if not got:
                        log.warning("Read the approval queue without the write lock")
                    return str(candidate.get("id"))
            queue = [r for r in queue if r.get("plan_id") != plan_id]
            queue.append(entry)
            written = self._write({"requests": queue})
        if not got:
            log.warning("Published an approval request without the write lock")
        return request_id if written else None

    @staticmethod
    def _clean_task_comments(raw: Any) -> dict[str, str]:
        """{task_id: comment} with blanks dropped. Ids are kept as text (JSON keys)."""
        if not isinstance(raw, dict):
            return {}
        cleaned: dict[str, str] = {}
        for key, value in raw.items():
            try:
                task_id = str(int(str(key).strip()))
            except (TypeError, ValueError):
                continue
            text = str(value or "").strip()
            if text:
                cleaned[task_id] = text
        return cleaned

    def record_decision(
        self,
        request_id: str,
        decision: str,
        comment: str,
        task_comments: Any = None,
        scope: Any = None,
        choices: Any = None,
        criteria: Any = None,
    ) -> bool:
        """Called by the page, for one specific queued request.

        `scope` comes from the page, which labels its own button with the consequence
        before the human clicks it. The server never re-derives it from whether comments
        happen to be present - that would silently overrule what the human was shown.

        Both phases may carry a task scope, but it means different things and only the
        handler knows which: on a PLAN request "these tasks only" is a rewrite of their
        wording, on a COMPLETION request it is an order to redo the work. What is still
        enforced here is that an entry with NO phase at all - written by an older process
        during a rolling restart, which had no per-task review of finished work - falls
        back to a whole-plan revision rather than being guessed at.

        `choices` (2.0.0) must name options this request actually showed, or nothing is
        recorded: the page only ever posts an index it rendered, so anything else is a
        stale tab or a forged request, and recording it would run something nobody
        picked. They count only on an approval, and never on a completion report.

        `criteria` (3.0.0) - the done_when sentences the human wrote - follow the same
        rule: a task this request did not show an editor for refuses the whole decision.
        They count on a plan request only, with an approval or a request for changes:
        what the human typed about a task is theirs whichever button they then press.
        """
        if decision not in DECISIONS:
            return False
        cleaned = self._clean_task_comments(task_comments)
        with exclusive(self.lock_path):
            record = self.read()
            queue = self._requests(record)
            for entry in queue:
                if entry.get("id") == request_id and entry.get("decision") is None:
                    wanted = scope if scope in SCOPES else SCOPE_PLAN
                    # Membership, not truthiness: an entry with no phase at all (written
                    # by an older process) still falls back to a whole-plan rewrite. A
                    # HALT card has no per-task review either.
                    if entry.get("phase") not in (PHASE_PLAN, PHASE_COMPLETION):
                        wanted = SCOPE_PLAN
                    if entry.get("phase") == PHASE_HALT:
                        cleaned = {}
                    picked: dict[str, int] = {}
                    if decision == "APPROVED" and entry.get("phase") in (PHASE_PLAN, PHASE_HALT):
                        checked = validate_page_choices(entry.get("tasks") or [], choices)
                        if checked is None:
                            log.warning("Refused choices that were not on screen: %r", choices)
                            return False
                        picked = checked
                    written: dict[str, str] = {}
                    if decision in ("APPROVED", "REVISE") and entry.get("phase") == PHASE_PLAN:
                        valid = validate_page_criteria(entry.get("tasks") or [], criteria)
                        if valid is None:
                            log.warning("Refused criteria for tasks not on screen: %r", criteria)
                            return False
                        written = valid
                    entry["decision"] = decision
                    entry["comment"] = comment or ""
                    entry["task_comments"] = cleaned
                    entry["scope"] = wanted
                    entry["choices"] = picked
                    entry["criteria"] = written
                    entry["decided_at"] = time.time()
                    # If this cannot be saved the click did nothing; say so rather than
                    # letting the page claim the decision was recorded.
                    return self._write({"requests": queue})
            return False

    def peek(self) -> list[dict[str, Any]]:
        """Everything currently queued, oldest first."""
        return sorted(self._requests(self.read()), key=lambda r: r.get("created_at", 0))

    def pending_entry(self, plan_id: str, fingerprint: str) -> dict[str, Any] | None:
        """The live, still-undecided request for exactly this plan version, if any.

        The fingerprint is part of the match on purpose: an entry for a plan that has
        since been rewritten is not a question anyone is still being asked, so it must
        not hold up the version that replaced it.
        """
        for entry in self._requests(self.read()):
            if (
                entry.get("plan_id") == plan_id
                and entry.get("fingerprint") == fingerprint
                and entry.get("decision") is None
            ):
                return entry
        return None

    def has_pending(self, plan_id: str, fingerprint: str) -> bool:
        """Is a human still looking at this exact plan version?

        While this is true the only decision that counts is one that came off the page.
        A decision arriving through the tool call instead is the model answering its own
        question - see the guard in _request_user_approval.
        """
        return self.pending_entry(plan_id, fingerprint) is not None

    def entry(self, request_id: str) -> dict[str, Any] | None:
        for candidate in self._requests(self.read()):
            if candidate.get("id") == request_id:
                return candidate
        return None

    def request_age(self, request_id: str) -> float:
        """Seconds since this request first went on screen.

        The total wait budget is measured from here rather than from the start of the
        current tool call, because a chunked wait spans many calls - and, after a
        restart, many processes. `publish` reuses the entry precisely so this number
        keeps counting across both.
        """
        entry = self.entry(request_id)
        if entry is None:
            return 0.0
        try:
            return max(0.0, time.time() - float(entry.get("created_at") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def set_agent_note(self, request_id: str, note: str) -> None:
        """Attach what the agent is still thinking while the human decides.

        A thinking model that calls plan_and_think again during approval is not allowed
        to change the plan the human is reading (D25) - but what it wanted to reconsider
        is information, so it is shown on the card instead of being thrown away. Only the
        latest note is kept.
        """
        with exclusive(self.lock_path):
            queue = self._requests(self.read())
            for candidate in queue:
                if candidate.get("id") == request_id and candidate.get("decision") is None:
                    candidate["agent_note"] = note
                    self._write({"requests": queue})
                    return

    def touch_agent(self, request_id: str) -> None:
        """Mark the agent as still waiting on this request.

        The page shows this as a liveness chip. It is deliberately not a countdown: the
        request outlives any single tool call, so a deadline would be a lie, and the one
        thing the human actually needs to know is whether deciding right now resumes the
        conversation by itself or needs a nudge in chat.
        """
        with exclusive(self.lock_path):
            queue = self._requests(self.read())
            for candidate in queue:
                if candidate.get("id") == request_id:
                    candidate["agent_last_seen"] = time.time()
                    self._write({"requests": queue})
                    return

    @staticmethod
    def _verdict(entry: dict[str, Any]) -> Verdict:
        """Read a decided entry. Old entries have no scope/task_comments: they predate
        per-task review and mean exactly what they always meant - rewrite the plan."""
        scope = entry.get("scope")
        return Verdict(
            decision=entry["decision"],
            comment=entry.get("comment", "") or "",
            task_comments=ApprovalStore._clean_task_comments(entry.get("task_comments")),
            scope=scope if scope in SCOPES else SCOPE_PLAN,
            choices={
                str(k): v for k, v in (entry.get("choices") or {}).items()
                if isinstance(v, int) and not isinstance(v, bool)
            },
            # An entry decided by an older process has none: it reads as "the human
            # changed no criterion", which is what that page could express.
            criteria={
                str(k): v for k, v in (entry.get("criteria") or {}).items()
                if isinstance(v, str)
            } if isinstance(entry.get("criteria"), dict) else {},
        )

    def _take(self, match) -> Verdict | None:
        with exclusive(self.lock_path):
            queue = self._requests(self.read())
            for entry in queue:
                verdict = match(entry)
                if verdict is None:
                    continue
                queue = [r for r in queue if r.get("id") != entry.get("id")]
                self._write({"requests": queue})
                return verdict if isinstance(verdict, Verdict) else None
            return None

    def claim(self, request_id: str) -> Verdict | None:
        """Consume the decision for a specific request. Used by the blocking waiter."""

        def match(entry):
            if entry.get("id") != request_id or not entry.get("decision"):
                return None
            return self._verdict(entry)

        return self._take(match)

    def claim_for_plan(self, plan_id: str, fingerprint: str) -> Verdict | None:
        """Consume a decision made after the tool call already returned.

        Only honoured for the exact plan version that was on screen - the human agreed
        to what they saw, not to whatever the plan became afterwards.
        """

        def match(entry):
            if entry.get("plan_id") != plan_id or not entry.get("decision"):
                return None
            if entry.get("fingerprint") != fingerprint:
                log.warning("Discarding a decision for a plan that has since changed (%s)", plan_id)
                return "drop"
            return self._verdict(entry)

        return self._take(match)

    def drop_for_plan(self, plan_id: str) -> None:
        """Withdraw a plan's request once the plan is settled.

        A cancelled or finished plan that keeps asking for approval is worse than
        useless: the human sees a live-looking request whose buttons do nothing, because
        the decision would be discarded on the fingerprint check anyway.
        """
        with exclusive(self.lock_path):
            queue = self._requests(self.read())
            remaining = [r for r in queue if r.get("plan_id") != plan_id]
            if len(remaining) != len(queue):
                self._write({"requests": remaining})

    def clear(self) -> None:
        with exclusive(self.lock_path):
            self._write({"requests": []})


# ---------------------------------------------------------------------------
# The run board (3.1.0)
# ---------------------------------------------------------------------------

RUNS_FILENAME = "runs.json"
RUNS_LOCK_FILENAME = ".runslock"

# What the human may ask of a running plan from the page.
CONTROL_PAUSE = "PAUSE"   # stop before the next task; they then decide on a pause card
CONTROL_NOTE = "NOTE"     # change what is left: the agent rewrites the unfinished tasks
CONTROL_CLEAR = "CLEAR"   # withdraw a request that has not been applied yet
CONTROLS = (CONTROL_PAUSE, CONTROL_NOTE)
MAX_CONTROL_CHARS = 1000


class RunBoard:
    """The plans that are executing right now, and what the human has asked of them.

    Between the plan approval and the completion report the page used to say "no
    pending requests": the human could neither see the work nor stop it, and the host
    this server is written for has no approval step of its own. The board is that view,
    and the one channel back.

    Its own file rather than a second list in approval.json: a planning-mcp process from
    before 3.1 on the same state directory rewrites that file whole, and would drop a
    stop the human had just asked for without anyone noticing.

    A control is a request, not a mutation. The page records it here; the server applies
    it when the agent next reports a task, inside the same transaction as every other
    change to the plan, and consumes it there. Until then the page shows it as asked
    for, not as done - which is exactly what is true.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)

    @property
    def path(self) -> Path:
        return self.state_dir / RUNS_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.state_dir / RUNS_LOCK_FILENAME

    def read(self) -> dict[str, dict[str, Any]]:
        """{plan_id: entry}. Lock-free: writes are atomic."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        runs = raw.get("runs") if isinstance(raw, dict) else None
        if not isinstance(runs, dict):
            return {}
        return {str(k): v for k, v in runs.items() if isinstance(v, dict)}

    def _write(self, runs: dict[str, dict[str, Any]]) -> bool:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"runs": runs}, ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.error("Could not persist the run board: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def sync(self, wanted: dict[str, dict[str, Any]]) -> bool:
        """Make the board show exactly `wanted`. Returns whether that is on disk.

        A control the human asked for survives a refresh of its plan's entry. One whose
        plan has left the board goes with it: that plan is no longer running, so there
        is nothing left to stop - the caller has already taken any words it carried.
        """
        if not wanted and not self.read():
            return True  # nothing running, nothing shown: never touch the disk for that
        with exclusive(self.lock_path):
            current = self.read()
            now = time.time()
            merged: dict[str, dict[str, Any]] = {}
            for plan_id, entry in wanted.items():
                old = current.get(plan_id) or {}
                new = dict(entry)
                if old.get("control"):
                    new["control"] = old["control"]
                # When the agent last reported: the moment this entry last changed.
                same = old.get("rev") == new.get("rev") and old.get("updated_at")
                new["updated_at"] = old["updated_at"] if same else now
                merged[plan_id] = new
            if merged == current:
                return True
            return self._write(merged)

    def request(self, plan_id: str, action: str, comment: Any = "") -> bool:
        """Called by the page. False = nothing was recorded, and the page says so."""
        if action not in CONTROLS and action != CONTROL_CLEAR:
            return False
        text = str(comment or "").strip()[:MAX_CONTROL_CHARS]
        if action == CONTROL_NOTE and not text:
            return False
        with exclusive(self.lock_path):
            runs = self.read()
            entry = runs.get(plan_id)
            if entry is None:
                # The plan is no longer running - it finished, failed or was stopped
                # while the page still showed the card.
                return False
            if action == CONTROL_CLEAR:
                if entry.pop("control", None) is None:
                    return True
            else:
                entry["control"] = {
                    "id": uuid.uuid4().hex[:12],
                    "action": action,
                    "comment": text,
                    "at": time.time(),
                }
            return self._write(runs)

    def pending(self, plan_id: str) -> dict[str, Any] | None:
        """The control waiting for this plan, without consuming it."""
        control = (self.read().get(plan_id) or {}).get("control")
        if isinstance(control, dict) and control.get("action") in CONTROLS:
            return control
        return None

    def take(self, plan_id: str) -> dict[str, Any] | None:
        """Consume the control for a plan. Called by the handler that applies it."""
        if self.pending(plan_id) is None:
            return None  # the common case, answered without the lock
        with exclusive(self.lock_path):
            runs = self.read()
            control = (runs.get(plan_id) or {}).pop("control", None)
            if not isinstance(control, dict) or control.get("action") not in CONTROLS:
                return None
            if not self._write(runs):
                # Not consumed, so not applied: it stays on the page as asked for and
                # the next call tries again, rather than being applied twice.
                return None
            return control

    def peek(self) -> list[dict[str, Any]]:
        """Every run that still has a live approval, in plan order."""
        now = time.time()
        out = []
        for _, entry in sorted(self.read().items()):
            try:
                expired = now > float(entry.get("expires_at") or now + 1)
            except (TypeError, ValueError):
                expired = False
            if not expired:
                out.append(entry)
        return out


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>planning-mcp 승인</title>
<style>
/* --sub: where everything under a task title starts - the title's own left edge, which
   is the number column (.tn, 1.4em at .95rem) plus the gap of .tt (.55rem). In rem on
   purpose. These rows used to be indented 1.95em each, and since each has its own
   font size, each got its own indent: the 태스크 완료 기준 label, the 의견 label, the
   options and the evidence lines all started a few pixels apart. */
:root{color-scheme:light dark;--sub:1.88rem}
body{font-family:system-ui,"Segoe UI","Malgun Gothic",sans-serif;margin:0;padding:2rem 1rem;
     background:#f6f7f9;color:#16181d}
@media(prefers-color-scheme:dark){body{background:#15171c;color:#e8eaed}}
.card{max-width:720px;margin:0 auto;background:#fff;border-radius:12px;padding:1.75rem;
      box-shadow:0 1px 3px rgba(0,0,0,.12)}
@media(prefers-color-scheme:dark){.card{background:#1e2126;box-shadow:none;border:1px solid #2c3038}}
h1{font-size:1.05rem;margin:0 0 .35rem;letter-spacing:.02em;text-transform:uppercase;opacity:.6}
.goal{font-size:1.15rem;font-weight:600;margin:0 0 1rem;line-height:1.4}
.goal .lbl{font-size:.78rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;
           opacity:.5;margin-right:.5rem;vertical-align:.08em}
.summary{background:#f2f3f5;border-radius:8px;padding:.85rem 1rem;margin:0 0 1.25rem;
         font-size:.94rem;line-height:1.65;white-space:pre-wrap;word-break:break-word}
@media(prefers-color-scheme:dark){.summary{background:#15171c}}
/* Why the circuit breaker stopped the agent, and what the agent was still weighing.
   Amber, not red: nothing is broken, a person is simply needed. */
.summary.warn{background:#fdf0e3}
.summary.agent{border-left:3px solid #8a5a00}
@media(prefers-color-scheme:dark){.summary.warn{background:#2a2115}}
.summary .lbl{display:block;font-size:.72rem;font-weight:600;text-transform:uppercase;
              letter-spacing:.04em;opacity:.5;margin-bottom:.35rem}
.tasklabel{font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;
           opacity:.5;margin:0 0 .2rem}
pre{white-space:pre-wrap;word-break:break-word;background:#f2f3f5;border-radius:8px;
    padding:1rem;font-size:.92rem;line-height:1.6;margin:0 0 1.25rem;
    font-family:ui-monospace,Consolas,monospace}
@media(prefers-color-scheme:dark){pre{background:#15171c}}
textarea{width:100%;box-sizing:border-box;min-height:64px;border-radius:8px;padding:.6rem;
         border:1px solid #ccd0d5;font:inherit;font-size:.92rem;margin-bottom:1rem;
         background:transparent;color:inherit}
@media(prefers-color-scheme:dark){textarea{border-color:#3a3f47}}
.row{display:flex;gap:.6rem;flex-wrap:wrap}
button{flex:1 1 auto;min-width:140px;padding:.85rem 1rem;border:0;border-radius:8px;
       font:inherit;font-weight:600;cursor:pointer;font-size:.95rem}
.ok{background:#1a7f37;color:#fff}.no{background:#b42318;color:#fff}.rev{background:#8a5a00;color:#fff}
button:disabled{opacity:.5;cursor:default}
/* Which planning-mcp is serving this page. Small, and outside #root so no re-render can
   remove it: after an upgrade "is the new version actually running?" is answered here. */
.ver{max-width:720px;margin:0 auto .4rem;padding:0 .25rem;box-sizing:border-box;
     display:flex;justify-content:flex-end;align-items:center;gap:.4rem;
     font-size:.72rem;letter-spacing:.02em}
.vt{opacity:.45}
/* The information icon beside it, and the dialog it opens. Every rule here undoes the
   page-wide `button` style, which is sized for the three decision buttons. */
.info{flex:0 0 auto;min-width:0;width:1.1rem;height:1.1rem;padding:0;border-radius:50%;
      border:1px solid currentColor;background:transparent;color:inherit;opacity:.5;
      font:italic 700 .68rem/1 Georgia,"Times New Roman",serif;cursor:pointer}
.info:hover,.info:focus-visible{opacity:1}
dialog.about{border:0;border-radius:12px;padding:0;min-width:270px;max-width:90vw;
             background:#fff;color:#16181d;box-shadow:0 8px 30px rgba(0,0,0,.25);
             font-size:.92rem}
@media(prefers-color-scheme:dark){
  dialog.about{background:#1e2126;color:#e8eaed;border:1px solid #2c3038}
}
dialog.about::backdrop{background:rgba(0,0,0,.35)}
.aboutin{padding:1.25rem 1.5rem}
.aboutin h2{font-size:1rem;margin:0 0 .8rem}
.aboutin p{margin:.3rem 0;line-height:1.5;word-break:break-word}
.aboutin .k{display:inline-block;min-width:4.4em;opacity:.6}
.aboutin button{display:block;flex:none;min-width:0;margin:1.1rem 0 0 auto;padding:.4rem 1rem;
                font-size:.85rem;background:#e6e8eb;color:inherit}
@media(prefers-color-scheme:dark){.aboutin button{background:#2c3038}}
.vernote{font-size:.78rem;color:#8a5a00;margin:-.1rem 0 .7rem;line-height:1.5}
@media(prefers-color-scheme:dark){.vernote{color:#d9a441}}
.idle{text-align:center;opacity:.6;padding:2.5rem 0;font-size:.95rem}
.done{text-align:center;padding:2rem 0;font-size:1.05rem;font-weight:600}
.hint{margin-top:1rem;font-size:.82rem;opacity:.55;line-height:1.5}
.tasks{margin:0 0 1.25rem}
.task{padding:.7rem 0;border-top:1px solid #e6e8eb}
@media(prefers-color-scheme:dark){.task{border-top-color:#2c3038}}
.task:first-child{border-top:0}
.tt{display:flex;gap:.55rem;align-items:baseline;line-height:1.45;font-size:.95rem}
.tn{opacity:.5;font-variant-numeric:tabular-nums;min-width:1.4em}
.badge{font-size:.72rem;padding:.1rem .4rem;border-radius:4px;background:#e6e8eb;opacity:.8}
@media(prefers-color-scheme:dark){.badge{background:#2c3038}}
.was{font-size:.82rem;opacity:.55;margin:.25rem 0 0 var(--sub);text-decoration:line-through}
.note{font-size:.82rem;margin:.25rem 0 0 var(--sub);color:#8a5a00}
@media(prefers-color-scheme:dark){.note{color:#d9a441}}
/* The agent's evidence for a finished task. Shown only on a completion report - it is
   the thing the human is actually being asked to judge. */
.ev{font-size:.86rem;opacity:.75;margin:.25rem 0 0 var(--sub);line-height:1.55;
    white-space:pre-wrap;word-break:break-word}
.ev.none{opacity:.45;font-style:italic}
/* Superseded output on a row the human sent back. Muted rather than struck through -
   a struck-out sentence is unreadable, and this one has to be compared, not dismissed. */
.ev.old{opacity:.45}
/* Per-task comment boxes are collapsed by default. Nine open textareas turn a task list
   into a form; the plan itself is what the human came here to read. */
.tcrow{display:none;gap:.5rem;align-items:baseline;margin:.4rem 0 0 var(--sub)}
.task.open .tcrow{display:flex}
/* The box sits beside its label, the way a criterion does - which is why it needs no
   sample sentence inside it. */
textarea.tc{flex:1 1 auto;width:auto;min-width:0;min-height:2.4rem;margin:0;font-size:.86rem;
            padding:.4rem .55rem}
/* The small buttons on a task row - 의견 / 다시 작업, 완료 기준 추가, 수정. They used to be
   flat grey at 65% opacity and read as labels rather than as something to press. Bold
   now, and slightly raised: a light edge on top, a shadow underneath, pressed in while
   held - and while the comment box the button opens is open. */
.tcbtn,.dwbtn{flex:0 0 auto;min-width:0;font-weight:700;border-radius:6px;color:inherit;
              border:1px solid #c2c7ce;background:linear-gradient(#fff,#e1e4e8);
              box-shadow:0 1px 2px rgba(0,0,0,.2),inset 0 1px 0 #fff}
.tcbtn:hover,.dwbtn:hover{background:linear-gradient(#fff,#d5d9df)}
.tcbtn:active,.dwbtn:active,.tcbtn[aria-expanded="true"]{
  background:#dfe2e6;box-shadow:inset 0 1px 2px rgba(0,0,0,.28)}
.tcbtn:active,.dwbtn:active{transform:translateY(1px)}
.tcbtn{margin-left:auto;align-self:center;padding:.15rem .5rem;font-size:.76rem}
/* A comment written and then collapsed is still submitted, so the row has to keep saying
   so - otherwise the human sends a targeted revision they can no longer see. */
.task.filled .tcbtn:not(.dwadd){background:#8a5a00;border-color:#6d4700;color:#fff;
  box-shadow:0 1px 2px rgba(0,0,0,.25),inset 0 1px 0 rgba(255,255,255,.25)}
@media(prefers-color-scheme:dark){
  .tcbtn,.dwbtn{border-color:#4a505b;background:linear-gradient(#3b414b,#2a2e36);
                box-shadow:0 1px 2px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.1)}
  .tcbtn:hover,.dwbtn:hover{background:linear-gradient(#454b56,#30353e)}
  .tcbtn:active,.dwbtn:active,.tcbtn[aria-expanded="true"]{
    background:#23272e;box-shadow:inset 0 1px 2px rgba(0,0,0,.6)}
  .task.filled .tcbtn:not(.dwadd){background:#8a5a00;border-color:#b07a14}
}
.scope{display:flex;align-items:center;gap:.45rem;margin:-.5rem 0 1rem;font-size:.86rem;
       opacity:.75}
.scope input{margin:0}
/* 2.0.0 - a task that offers a choice. The recommendation is pre-selected and marked;
   each option carries its trade-off, so the human can pick without asking. */
.opts{display:flex;flex-direction:column;gap:.3rem;margin:.4rem 0 0 var(--sub);font-size:.9rem}
.opt{display:flex;gap:.45rem;align-items:baseline;cursor:pointer;line-height:1.45}
.opt input{margin:0;flex:0 0 auto}
.optl{opacity:.55;min-width:1.1em;font-variant-numeric:tabular-nums}
.rec{font-size:.72rem;padding:.05rem .4rem;border-radius:4px;background:#e6f4ea;color:#1a7f37;
     font-weight:600;margin-left:.3rem}
@media(prefers-color-scheme:dark){.rec{background:#16281c;color:#5dbb77}}
.why{opacity:.65;font-size:.84rem}
/* The heading of a choice: what is being chosen, then how many options. */
.pick{font-weight:600}
.cnt{opacity:.55;font-size:.84rem;font-weight:400}
.err{background:#fdecea;color:#b42318;border-radius:8px;padding:.6rem .9rem;margin:0 0 1rem;
     font-size:.9rem}
/* 3.0.0 - the verification contract. What "finished" means for a task, as the human
   approved it or wrote it, and what the server itself found on disk. A task with no
   criterion shows only a small button, for the reason the comment boxes are collapsed:
   the plan is what the human came to read. */
.dw{display:flex;gap:.5rem;align-items:baseline;margin:.3rem 0 0 var(--sub);font-size:.86rem;
    line-height:1.5}
.dw.hid{display:none}
.dwl{flex:0 0 auto;font-size:.72rem;font-weight:600;letter-spacing:.04em;opacity:.5}
.dwt{flex:1 1 auto;word-break:break-word}
.dwby{font-size:.72rem;opacity:.6;margin-left:.35rem}
.dwbtn{padding:.1rem .45rem;font-size:.74rem}
input.dwi{display:none;flex:1 1 auto;min-width:0;box-sizing:border-box;border-radius:6px;
          padding:.3rem .5rem;border:1px solid #ccd0d5;font:inherit;font-size:.86rem;
          background:transparent;color:inherit}
@media(prefers-color-scheme:dark){input.dwi{border-color:#3a3f47}}
.dw.edit input.dwi{display:block}
.dw.edit .dwt,.dw.edit .dwbtn{display:none}
/* An edited criterion travels with the approval, so the row has to keep saying so. */
.dw.changed .dwl{color:#8a5a00;opacity:1}
.tcbtn.dwadd{margin-left:auto}
.tcbtn.dwadd+.tcbtn{margin-left:0}
.ck{font-size:.84rem;margin:.2rem 0 0 var(--sub);line-height:1.5;word-break:break-word}
.ck.ok{color:#1a7f37}.ck.warn{color:#8a5a00}.ck.dim{opacity:.6}
@media(prefers-color-scheme:dark){.ck.ok{color:#5dbb77}.ck.warn{color:#d9a441}}
.took{font-size:.76rem;opacity:.5;white-space:nowrap}
.triage{background:#f2f3f5;border-radius:8px;padding:.5rem .8rem;margin:0 0 .9rem;
        font-size:.86rem;line-height:1.5}
@media(prefers-color-scheme:dark){.triage{background:#15171c}}
/* Whether an agent is still holding a call open for this request. Not a countdown:
   the request outlives any one tool call, so the honest thing to show is not how long
   is left but whether deciding right now resumes the conversation by itself. */
.chip{display:inline-block;font-size:.78rem;padding:.2rem .55rem;border-radius:999px;
      margin:0 0 .9rem;line-height:1.5}
.chip.live{background:#e6f4ea;color:#1a7f37}
.chip.idle{background:#fdf0e3;color:#8a5a00}
@media(prefers-color-scheme:dark){
  .chip.live{background:#16281c;color:#5dbb77}
  .chip.idle{background:#2a2115;color:#d9a441}
}
/* 3.1.0 - a plan that is running. Not a question: the human may watch, stop it before
   its next task, or change what is left. */
.badge.now{background:#e6f4ea;color:#1a7f37;opacity:1}
@media(prefers-color-scheme:dark){.badge.now{background:#16281c;color:#5dbb77}}
.seen{font-size:.76rem;opacity:.5;margin:-.6rem 0 .9rem}
.pendingctl{background:#fdf0e3;border-radius:8px;padding:.75rem .95rem;margin:0 0 1rem;
            font-size:.9rem;line-height:1.6}
@media(prefers-color-scheme:dark){.pendingctl{background:#2a2115}}
.pendingctl .q{display:block;margin-top:.3rem;opacity:.8;white-space:pre-wrap;
               word-break:break-word}
/* One card per request and per running plan, each drawn on its own. The rule between two
   cards belongs to the second of them, so a card that comes or goes takes its rule along. */
.item+.item{border-top:1px solid #ccd0d5;margin-top:1.75rem;padding-top:1.75rem}
.linkbtn{display:block;flex:none;min-width:0;margin-top:.55rem;padding:.25rem .7rem;
         font-size:.8rem;font-weight:500;border-radius:6px;background:#fff;color:inherit}
@media(prefers-color-scheme:dark){.linkbtn{background:#2c3038}}
</style></head><body><div class="ver"><span class="vt">planning-mcp __PLANNING_MCP_VERSION__</span>
<button class="info" id="about-open" type="button" aria-label="프로그램 정보" title="정보">i</button></div>
<div class="card" id="root">
<div class="idle">현재 대기 중인 승인 요청이 없습니다.<br>
<span style="font-size:.85rem">에이전트가 계획을 제출하면 이곳에 표시됩니다.</span></div></div>
<dialog class="about" id="about" aria-labelledby="about-title"><div class="aboutin">
<h2 id="about-title">planning-mcp</h2>
<p><span class="k">Author:</span> __PLANNING_MCP_AUTHOR__</p>
<p><span class="k">Email:</span> __PLANNING_MCP_EMAIL__</p>
<p><span class="k">Version:</span> __PLANNING_MCP_VERSION__</p>
<button type="button" id="about-close">닫기</button>
</div></dialog>
<script>
let busy=false,flash=null,pendingCount=0,lastError='';
// The version of the server that is serving this page (filled in when it is served).
const VERSION='__PLANNING_MCP_VERSION__';
// A request can be asked by another planning-mcp process on the same state directory,
// and after an upgrade that process may still be running older code. Nothing is said
// when the versions agree - which is nearly always.
function verNote(d){
  if(d.version===VERSION)return '';
  return '<p class="vernote">'+(d.version
    ?'이 요청은 planning-mcp '+esc(d.version)+' 서버가 보냈습니다. '
    :'이 요청을 보낸 서버는 버전을 남기지 않는 이전 버전입니다. ')+
    '이 페이지는 '+esc(VERSION)+'입니다.</p>';
}
const IDLE_TITLE='planning-mcp 승인';
// A popup can be blocked, land on another monitor, or open behind other windows.
// So the page makes itself noticeable instead: the tab title flashes and a short tone
// plays. Leaving this tab open is the reliable way to catch approval requests.
function alertOn(){
  if(flash)return;
  let on=false;
  flash=setInterval(()=>{on=!on;document.title=on?
    '\\u26A0 승인 대기 '+pendingCount+'건':IDLE_TITLE;},700);
  try{
    const C=window.AudioContext||window.webkitAudioContext;if(!C)return;
    const ctx=new C();const o=ctx.createOscillator();const g=ctx.createGain();
    o.connect(g);g.connect(ctx.destination);o.frequency.value=880;g.gain.value=0.08;
    o.start();o.stop(ctx.currentTime+0.18);
    setTimeout(()=>{try{ctx.close();}catch(e){}},400);
  }catch(e){}
}
function alertOff(){
  if(flash){clearInterval(flash);flash=null;}
  document.title=IDLE_TITLE;
}
// ---- drafts --------------------------------------------------------------
// What the human has typed but not yet submitted lives in localStorage, not in the DOM.
// The page rebuilds itself whenever the queue changes - a second session asking for
// approval is enough - and rebuilding used to throw away a half-written comment with no
// trace. Keeping drafts outside the DOM means they survive that, plus a reload, plus
// closing the tab. localStorage is shared per origin, so the `storage` event below also
// keeps two open tabs showing the same text: whichever one the human finally submits
// from, it carries what they wrote.
const DRAFT='planning-mcp:draft:';
function dkey(req,tid){return DRAFT+req+':'+tid;}
function dget(req,tid){
  try{const v=localStorage.getItem(dkey(req,tid));return v===null?'':v;}catch(e){return '';}
}
function dset(req,tid,value){
  // Empty strings are stored, never removed: "I deleted what I wrote" is a state that
  // has to survive a re-render too, or the old text comes back.
  try{localStorage.setItem(dkey(req,tid),value);}catch(e){}
}
function dkeys(){
  const out=[];
  try{
    for(let i=0;i<localStorage.length;i++){
      const k=localStorage.key(i);
      if(k&&k.indexOf(DRAFT)===0)out.push(k);
    }
  }catch(e){}
  return out;
}
function dclear(req){
  try{dkeys().forEach(k=>{if(k.indexOf(DRAFT+req+':')===0)localStorage.removeItem(k);});}catch(e){}
}
// Drafts for requests that have left the queue would otherwise sit there forever and
// reappear if an id were ever reused.
function dprune(ids){
  try{
    dkeys().forEach(k=>{
      const rest=k.slice(DRAFT.length);
      const cut=rest.indexOf(':');
      if(cut>0&&ids.indexOf(rest.slice(0,cut))<0)localStorage.removeItem(k);
    });
  }catch(e){}
}
function taskBoxes(req){
  return document.querySelectorAll('textarea.tc[data-req="'+req+'"]');
}
// Puts what the human had typed back into ONE card, right after that card is drawn. A
// card that was not redrawn still holds it in the DOM and is left alone.
function restore(d){
  if(d.decided)return;
  const all=document.getElementById('c-'+d.id);
  if(all)all.value=dget(d.id,'_all');
  const whole=document.getElementById('all-'+d.id);
  if(whole)whole.checked=dget(d.id,'_whole')==='1';
  taskBoxes(d.id).forEach(b=>{
    b.value=dget(d.id,b.getAttribute('data-tid'));
    // A restored comment has to be visible, or the human sends a targeted revision
    // they cannot see. relabel() marks the row; opening it lets them edit it.
    if(b.value.trim()){
      const task=b.closest('.task');
      if(task){
        task.classList.add('open');
        const btn=task.querySelector('.tcbtn:not(.dwadd)');
        if(btn)btn.setAttribute('aria-expanded','true');
      }
    }
  });
  // The human's picks survive a rebuild like their comments do (D22).
  document.querySelectorAll('input[type=radio][data-req="'+d.id+'"]').forEach(r=>{
    const v=dget(d.id,'ch'+r.getAttribute('data-ctid'));
    if(v!==''&&r.value===v){
      r.checked=true;
      if(v==='other')openComment(d.id,r.getAttribute('data-ctid'),false);
    }
  });
  // So does a criterion they wrote. "No draft" and "erased it" are different states
  // here - erasing removes the criterion - so the raw item is read, not dget's ''.
  dwInputs(d.id).forEach(i=>{
    let v=null;
    try{v=localStorage.getItem(dkey(d.id,'dw'+i.getAttribute('data-dtid')));}catch(e){}
    if(v!==null&&v!==(i.getAttribute('data-orig')||'')){i.value=v;showCriterion(i);}
  });
  relabel(d.id);
}
// One delegated listener on a container that outlives every re-render, so no inline
// handler has to carry an escaped id.
function onInput(ev){
  const el=ev.target;
  if(!el)return;
  if(el.type==='radio'&&el.hasAttribute('data-ctid')){
    const req=el.getAttribute('data-req'),tid=el.getAttribute('data-ctid');
    dset(req,'ch'+tid,el.value);
    // Something other than the options on offer is a change to the task itself, which
    // is what the per-task comment and the REVISE button already handle.
    if(el.value==='other')openComment(req,tid,true);
    relabel(req);
    return;
  }
  if(el.type==='checkbox'&&el.id.indexOf('all-')===0){
    const req=el.id.slice(4);
    dset(req,'_whole',el.checked?'1':'');
    relabel(req);
    return;
  }
  if(el.classList&&el.classList.contains('dwi')){
    const req=el.getAttribute('data-req');
    dset(req,'dw'+el.getAttribute('data-dtid'),el.value);
    relabel(req);
    return;
  }
  if(el.tagName!=='TEXTAREA')return;
  const req=el.getAttribute('data-req');
  if(req){dset(req,el.getAttribute('data-tid'),el.value);relabel(req);return;}
  if(el.id.indexOf('c-')===0){
    const id=el.id.slice(2);
    dset(id,'_all',el.value);
    // A halt card's continue button says whether it carries a direction.
    relabel(id);
  }
}
// Another tab wrote a draft. Mirror it here so both windows show the same thing - the
// property the old full-rebuild had by accident, kept on purpose.
function onStorage(ev){
  if(!ev.key||ev.key.indexOf(DRAFT)!==0)return;
  const rest=ev.key.slice(DRAFT.length);
  const cut=rest.indexOf(':');
  if(cut<0)return;
  const req=rest.slice(0,cut),tid=rest.slice(cut+1),value=ev.newValue||'';
  if(tid==='_whole'){
    const box=document.getElementById('all-'+req);
    if(box)box.checked=value==='1';
    relabel(req);
    return;
  }
  if(tid.indexOf('ch')===0){
    const r=document.querySelector('input[type=radio][data-req="'+req+'"][data-ctid="'+
      tid.slice(2)+'"][value="'+value+'"]');
    if(r){r.checked=true;if(value==='other')openComment(req,tid.slice(2),false);}
    relabel(req);
    return;
  }
  if(tid.indexOf('dw')===0){
    const i=document.querySelector('input.dwi[data-req="'+req+'"][data-dtid="'+
      tid.slice(2)+'"]');
    if(i&&i!==document.activeElement){i.value=value;showCriterion(i);}
    relabel(req);
    return;
  }
  const el=tid==='_all'
    ? document.getElementById('c-'+req)
    : document.querySelector('textarea[data-req="'+req+'"][data-tid="'+tid+'"]');
  if(!el||el===document.activeElement)return;
  el.value=value;
  if(tid!=='_all'&&value.trim()){
    const task=el.closest('.task');
    if(task)task.classList.add('open');
  }
  relabel(req);
}

async function poll(){
  if(busy)return;
  try{
    const r=await fetch('/api/pending');const d=await r.json();
    const list=d.requests||[];
    RUNS=d.runs||[];
    dprune(list.map(x=>x.id).concat(RUNS.map(runId)));
    const undecided=list.filter(x=>!x.decided);
    pendingCount=undecided.length;
    // Nothing on the page changed: nothing is touched, and the tone is not played again.
    if(!draw(list))return;
    // A running plan is not a question, so it never raises the alarm.
    if(undecided.length)alertOn();else alertOff();
  }catch(e){}
}
// A run card redraws each time the agent reports a task, which can be while the human is
// typing into it. The text survives - it is a draft - and this puts the caret back.
function focusKey(){
  const el=document.activeElement;
  if(!el||!el.closest||!el.closest('#root'))return null;
  const key={id:el.id||'',req:'',tid:'',dtid:'',start:null,end:null};
  if(!key.id&&el.getAttribute){
    key.req=el.getAttribute('data-req')||'';
    key.tid=el.getAttribute('data-tid')||'';
    key.dtid=el.getAttribute('data-dtid')||'';
    if(!key.req)return null;
  }
  try{key.start=el.selectionStart;key.end=el.selectionEnd;}catch(e){}
  return key;
}
function refocus(key){
  if(!key)return;
  let el=null;
  if(key.id)el=document.getElementById(key.id);
  else if(key.tid)el=document.querySelector('textarea[data-req="'+key.req+'"][data-tid="'+key.tid+'"]');
  else if(key.dtid)el=document.querySelector('input.dwi[data-req="'+key.req+'"][data-dtid="'+key.dtid+'"]');
  if(!el||el.disabled)return;
  el.focus();
  try{if(typeof key.start==='number')el.setSelectionRange(key.start,key.end);}catch(e){}
}
// Quotes are escaped too so the same helper is safe inside an attribute value.
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
  '"':'&quot;',"'":'&#39;'}[c]));}

// ---- per-task review -----------------------------------------------------
// Both phases can be reviewed task by task, but the request means different things:
// on a plan it rewrites the task's wording, on a completion report it orders that one
// task to be done again. The phase of each queued request is kept here so the buttons
// can say which of the two the human is about to ask for.
const PHASE={};
function comments(id){
  const out={};
  document.querySelectorAll('textarea[data-req="'+id+'"]').forEach(b=>{
    const v=b.value.trim();
    if(v)out[b.getAttribute('data-tid')]=v;
  });
  return out;
}
// Reveal one task's comment box. The textarea stays in the DOM either way, so a comment
// written and then collapsed is still collected by comments() - the row keeps its 'filled'
// marker precisely so that cannot happen silently.
function toggleComment(btn){
  const task=btn.closest('.task');
  if(!task)return;
  const open=task.classList.toggle('open');
  btn.setAttribute('aria-expanded',open?'true':'false');
  if(open){const ta=task.querySelector('textarea.tc');if(ta)ta.focus();}
}
function wholePlan(id){
  const box=document.getElementById('all-'+id);
  return !!(box&&box.checked);
}
function scopeOf(id){
  if(wholePlan(id))return 'PLAN';
  return Object.keys(comments(id)).length?'TASKS':'PLAN';
}
// The button says what it will do BEFORE it is clicked, so choosing the scope is an
// explicit act by the human rather than something the server infers afterwards. One
// builder for both the first render and every relabel, so the two can never drift.
function revLabel(phase,ids,whole){
  const done=phase==='COMPLETION';
  if(whole||!ids.length)
    return done?'수정 요청 · 계획 전체 다시 세우기':'수정 요청 · 계획 전체 재작성';
  const verb=done?'다시 작업 요청':'수정 요청';
  return ids.length===1?verb+' · '+ids[0]+'번만':verb+' · '+ids.length+'개 태스크만';
}
// The continue button on a halt card states whether it sends the agent a direction.
function haltLabel(hasText){return hasText?'의견 전달 후 계속':'계속 진행';}
// ---- choices (2.0.0) -------------------------------------------------------
// A task that offers a choice shows its options as radios, the model's recommendation
// (index 0) pre-selected and marked 권장. What is picked travels with the approval;
// the server refuses any index that was not on screen.
const LETTERS='ABCDEFGH';
function hasChoice(t){return !!(t.options&&t.options.length>=2);}
// '집계 방식 · 3가지 중 선택' - what is being chosen, from the model; a neutral noun phrase
// when it gave none. Mirrors choices.choice_heading, which writes the chat text.
function choiceHeading(t){
  return '<span class="pick">'+esc((t.topic||'').trim()||'진행 방법')+
    '</span><span class="cnt"> · '+t.options.length+'가지 중 선택</span>';
}
function optionsHtml(d,t,allowOther){
  if(!hasChoice(t))return '';
  const name='ch-'+d.id+'-'+t.task_id;
  const attrs=' name="'+esc(name)+'" data-req="'+esc(d.id)+'" data-ctid="'+
    esc(String(t.task_id))+'"';
  let h='<div class="opts">';
  t.options.forEach((o,i)=>{
    // aria-label: a screen reader (and the accessibility tree) otherwise names the
    // radio by its value, "0" / "1", rather than by the option it stands for.
    h+='<label class="opt"><input type="radio"'+attrs+' value="'+i+'"'+(i===0?' checked':'')+
      ' aria-label="'+LETTERS[i]+'. '+esc(o.title)+(i===0?' (권장)':'')+
      '"><span class="optl">'+LETTERS[i]+'.</span><span>'+esc(o.title)+
      (i===0?'<span class="rec">권장</span>':'')+
      (o.reason?' <span class="why">— '+esc(o.reason)+'</span>':'')+'</span></label>';
  });
  if(allowOther)h+='<label class="opt"><input type="radio"'+attrs+' value="other" aria-label="기타">'+
    '<span class="optl"></span><span>기타 (직접 입력)</span></label>';
  return h+'</div>';
}
function checkedOf(id){
  return Array.from(document.querySelectorAll('input[type=radio][data-req="'+id+'"]:checked'));
}
function choicesOf(id){
  const out={};
  checkedOf(id).forEach(r=>{if(r.value!=='other')out[r.getAttribute('data-ctid')]=Number(r.value);});
  return out;
}
function anyOther(id){return checkedOf(id).some(r=>r.value==='other');}
// The approve button, like the REVISE button, says what it will do before it is
// clicked: which options it carries, or that 기타 needs a revision request instead.
function okLabel(phase,id){
  const base=phase==='HALT'?'이 초안으로 승인':'승인';
  if(anyOther(id))return base+' · 기타는 수정 요청으로';
  const changed=Object.entries(choicesOf(id)).filter(([k,v])=>v!==0);
  const crit=Object.keys(criteriaOf(id)).length;
  if(crit&&changed.length)return base+' · 변경 '+(changed.length+crit)+'건 반영';
  if(crit)return base+' · 태스크 완료 기준 '+crit+'건 반영';
  if(!changed.length)return base;
  if(changed.length===1)return base+' · '+changed[0][0]+'번 '+LETTERS[changed[0][1]]+'안';
  return base+' · 선택 '+changed.length+'건 반영';
}
// ---- done_when (3.0.0) -----------------------------------------------------
// What "finished" means for a task. On a plan request the human may write or rewrite
// it and still approve: the task itself does not change, so there is nothing for the
// agent to redraft and no round trip is spent. The edit is a draft like any other -
// kept in localStorage, mirrored across tabs - and only the tasks whose text differs
// from what was shown are sent; the server refuses any task it did not show.
function dwInputs(id){
  return Array.from(document.querySelectorAll('input.dwi[data-req="'+id+'"]'));
}
function dwValue(i){return i.value.trim().replace(/\\s+/g,' ');}
function criteriaOf(id){
  const out={};
  dwInputs(id).forEach(i=>{
    const v=dwValue(i);
    if(v!==(i.getAttribute('data-orig')||''))out[i.getAttribute('data-dtid')]=v;
  });
  return out;
}
function showCriterion(i){
  const row=i.closest('.dw');
  if(row){row.classList.remove('hid');row.classList.add('edit');}
  // The button that opens the field shows that it is open, as 의견 does.
  const task=i.closest('.task'),add=task&&task.querySelector('.dwadd');
  if(add)add.setAttribute('aria-expanded','true');
}
// 완료 기준 추가 is a toggle, like 의견 - while the field is empty. Once something is
// typed the field stays: it travels with the approval, and hiding it would hide what the
// approval carries. Erase the text and the button closes the field again.
function toggleCriterion(btn,req,tid){
  const i=document.querySelector('input.dwi[data-req="'+req+'"][data-dtid="'+tid+'"]');
  if(!i)return;
  const row=i.closest('.dw');
  if(row&&row.classList.contains('edit')&&!dwValue(i)){
    row.classList.remove('edit');row.classList.add('hid');
    btn.setAttribute('aria-expanded','false');
    return;
  }
  showCriterion(i);i.focus();
}
function editCriterion(btn,req,tid){
  const i=document.querySelector('input.dwi[data-req="'+req+'"][data-dtid="'+tid+'"]');
  if(!i)return;
  showCriterion(i);i.focus();
}
function dwRow(d,t,editable){
  const has=!!t.done_when,rid=esc(d.id),tid=esc(String(t.task_id));
  if(!has&&!editable)return '';
  let h='<div class="dw'+(has?'':' hid')+'"><span class="dwl">태스크 완료 기준</span>'+
    '<span class="dwt">'+esc(t.done_when||'')+
    (t.done_when_by==='user'?'<span class="dwby">사용자 지정</span>':'')+'</span>';
  if(editable)h+='<input class="dwi" type="text" maxlength="200" data-req="'+rid+
    '" data-dtid="'+tid+'" data-orig="'+esc(t.done_when||'')+'" value="'+esc(t.done_when||'')+
    '" aria-label="'+tid+'번 태스크 완료 기준">'+
    '<button class="dwbtn" type="button" onclick="editCriterion(this,\\''+rid+'\\',\\''+
    tid+'\\')">수정</button>';
  return h+'</div>';
}
// ---- what the server found (3.0.0) -------------------------------------------
// On a completion report: the files a task said it produced, as the server found them.
// Everything else on the row is the agent's word; these lines are not.
function sizeText(n){
  if(typeof n!=='number')return '';
  if(n<1024)return n+' B';
  if(n<1048576)return (n/1024).toFixed(1)+' KB';
  return (n/1048576).toFixed(1)+' MB';
}
function baseName(p){
  const s=String(p||'').replace(/\\\\/g,'/');
  const i=s.lastIndexOf('/');
  return i>=0?s.slice(i+1):s;
}
function tookText(s){
  if(typeof s!=='number')return '';
  if(s<60)return s+'초';
  const m=Math.floor(s/60),r=s%60;
  if(m<60)return m+'분'+(r?' '+r+'초':'');
  return Math.floor(m/60)+'시간 '+(m%60)+'분';
}
// A file the task itself created or changed. One that was already there before the
// task proves the file exists, not that the task did anything - it gets its own line,
// but it does not move the task out of "the agent's word only".
function producedCheck(c){
  return !c.gone&&(c.state==='found'||c.state==='folder');
}
function checkLine(c){
  const name=esc(baseName(c.path)),size=sizeText(c.size),det=size?' · '+size:'';
  const open='<div title="'+esc(c.path)+'" class="ck ';
  if(c.gone)return open+'warn">⚠ '+name+' · 보고 당시에는 있었으나 지금은 없음</div>';
  if(c.state==='found')return open+'ok">✔ '+name+det+' · 이 태스크 중 생성/변경됨</div>';
  if(c.state==='old')return open+'dim">· '+name+det+
    ' · 작업 전부터 있던 파일 (이 태스크에서 바뀌지 않음)</div>';
  if(c.state==='folder')return open+'ok">✔ '+name+' · 폴더가 있음</div>';
  if(c.state==='empty')return open+'warn">⚠ '+name+' · 빈 파일</div>';
  if(c.state==='missing')return open+'warn">⚠ '+name+' · 찾을 수 없음</div>';
  if(c.state==='unknown')return open+'dim">· '+name+' · 확인하지 못함 (응답 지연)</div>';
  return open+'dim">· '+name+' · 서버의 확인 범위 밖</div>';
}
function checksHtml(t){
  let h=(t.checks||[]).map(checkLine).join('');
  if((t.claims_withdrawn||[]).length)h+='<div class="ck warn">⚠ 처음에 '+
    esc(t.claims_withdrawn.map(baseName).join(', '))+
    ' 을(를) 결과 파일로 적었다가 뺐습니다 (서버가 찾지 못함)</div>';
  if(t.echo)h+='<div class="ck warn">⚠ 증거가 태스크 완료 기준 문장을 거의 그대로 반복합니다</div>';
  return h;
}
// Where to look first. Shown only when the server checked at least one file for this
// plan - otherwise every task would be "unconfirmed" and the line would say nothing.
function triage(d){
  const tasks=d.tasks||[];
  if(!tasks.some(t=>(t.checks||[]).length||(t.claims_withdrawn||[]).length))return '';
  const seen=tasks.filter(t=>(t.checks||[]).some(producedCheck)).length;
  const rest=tasks.length-seen;
  return '<div class="triage">'+tasks.length+'개 중 '+seen+
    '개는 태스크 중에 만들거나 바꾼 파일을 서버가 확인했습니다.'+
    (rest?' 나머지 '+rest+'개는 에이전트의 보고가 근거입니다.':'')+'</div>';
}
function relabelOk(id){
  const b=document.getElementById('ok-'+id);
  if(!b)return;
  b.textContent=okLabel(PHASE[id],id);
  b.disabled=anyOther(id);
}
function openComment(req,tid,focus){
  const ta=document.querySelector('textarea.tc[data-req="'+req+'"][data-tid="'+tid+'"]');
  const task=ta&&ta.closest('.task');
  if(!task)return;
  task.classList.add('open');
  const btn=task.querySelector('.tcbtn:not(.dwadd)');
  if(btn)btn.setAttribute('aria-expanded','true');
  if(focus)ta.focus();
}
function relabel(id){
  // A run card has no button whose label depends on what is typed.
  if(PHASE[id]==='RUN')return;
  relabelOk(id);
  if(PHASE[id]==='HALT'){
    const b=document.getElementById('rev-'+id),c=document.getElementById('c-'+id);
    if(b)b.textContent=haltLabel(!!(c&&c.value.trim()));
    return;
  }
  // Mark the rows that carry a comment, so a collapsed one is still visible as such.
  document.querySelectorAll('textarea[data-req="'+id+'"]').forEach(b=>{
    const task=b.closest('.task');
    if(task)task.classList.toggle('filled',!!b.value.trim());
  });
  // And the criteria the human has changed: they travel with the approval.
  dwInputs(id).forEach(i=>{
    const row=i.closest('.dw');
    if(row)row.classList.toggle('changed',dwValue(i)!==(i.getAttribute('data-orig')||''));
  });
  const btn=document.getElementById('rev-'+id);
  if(!btn)return;
  btn.textContent=revLabel(PHASE[id],Object.keys(comments(id)),wholePlan(id));
}
// The header a human needs before they can judge a task list at all: what is being
// asked, what the goal is, and the model's own overview of how it intends to get there.
// All three used to arrive inside the pre-rendered `display` blob; rendering tasks as
// rows dropped that blob, so they are composed here from their own fields instead.
function header(d){
  const title=d.phase==='COMPLETION'?'완료 확인':'계획 승인 요청';
  let h='<h1>'+title+' · '+esc(d.plan_id)+'</h1>'+verNote(d)+
    '<p class="goal"><span class="lbl">목표</span>'+esc(d.goal)+'</p>';
  if(d.summary)h+='<div class="summary"><span class="lbl">개요</span>'+esc(d.summary)+'</div>';
  return h;
}
function taskRows(d){
  // On a completion report every task is DONE, so a DONE badge on every row is noise -
  // the evidence line below it already says so, and that is what needs the attention.
  const done=d.phase==='COMPLETION';
  return '<p class="tasklabel">태스크 '+(d.tasks||[]).length+'개</p>'+
    '<div class="tasks">'+(d.tasks||[]).map(t=>{
    const badge=t.status&&t.status!=='PENDING'&&!(done&&t.status==='DONE');
    const choose=!done&&hasChoice(t)&&t.chosen==null;
    // On a completion report a task that offered a choice says which one was carried
    // out, so the human can check it was done the way they picked.
    const picked=done&&hasChoice(t)&&t.chosen!=null
      ?'<span class="badge">'+(t.chosen===0?'권장안':LETTERS[t.chosen]+'안 선택')+'</span>':'';
    // A criterion can be written for any task still to be done. Finished work keeps the
    // one it was done under.
    const editable=!done&&t.status!=='DONE';
    const took=done?tookText(t.duration_sec):'';
    let row='<div class="task"><div class="tt"><span class="tn">'+esc(String(t.task_id))+
      '.</span>'+(choose?'<span>'+choiceHeading(t)+'</span>'
                        :'<span>'+esc(t.title)+'</span>')+picked+
      (badge?'<span class="badge">'+esc(t.status)+'</span>':'')+
      (took?'<span class="took">소요 '+took+'</span>':'')+
      (editable&&!t.done_when?'<button class="tcbtn dwadd" type="button" aria-expanded="false" '+
        'onclick="toggleCriterion(this,\\''+esc(d.id)+'\\',\\''+esc(String(t.task_id))+
        '\\')">완료 기준 추가</button>':'')+
      '<button class="tcbtn" type="button" aria-expanded="false" '+
      'onclick="toggleComment(this)">'+(done?'다시 작업':'의견')+'</button></div>';
    if(choose)row+=optionsHtml(d,t,true);
    if(t.previous_title)row+='<div class="was">'+esc(t.previous_title)+'</div>';
    if(t.revision_note)row+='<div class="note">\\u21BB 요청하신 내용: '+
      esc(t.revision_note)+'</div>';
    // A task rewritten after it failed: why the first way did not work.
    if(t.failure_note)row+='<div class="note">✕ 이전 시도 실패: '+
      esc(t.failure_note)+'</div>';
    row+=dwRow(d,t,editable);
    // What the task used to produce, for a row the human already sent back once. The
    // request alone shows what was asked; only this shows whether it was answered.
    if(done&&t.previous_result_log)
      row+='<div class="ev old">이전 결과: '+esc(t.previous_result_log)+'</div>';
    // The claim the human is judging. Without it a completion report is a task list
    // that says nothing about whether the work happened.
    if(done){
      const ev=(t.result_log||'').trim();
      row+=ev?'<div class="ev">\\u2192 '+esc(ev)+'</div>'
             :'<div class="ev none">(증거 기록 없음)</div>';
      row+=checksHtml(t);
    }
    // Labelled with the name of the button that opens it, so the box needs no sample
    // sentence inside - a grey one read as if something had already been written.
    const what=done?'다시 작업':'의견';
    row+='<div class="tcrow"><span class="dwl">'+what+'</span><textarea class="tc" data-req="'+
      esc(d.id)+'" data-tid="'+esc(String(t.task_id))+'" aria-label="'+
      esc(String(t.task_id))+'번 '+what+'"></textarea></div>';
    return row+'</div>';
  }).join('')+'</div>';
}
function chip(d){
  return d.agent_waiting
    ? '<div class="chip live">에이전트가 대기 중입니다 · 결정하시면 작업이 바로 이어집니다</div>'
    : '<div class="chip idle">에이전트가 대기를 멈췄습니다 · 지금 결정하셔도 반영되며, '+
      '채팅창에 메시지를 입력하시면 진행이 재개됩니다</div>';
}
// What the agent still wanted to reconsider while this request was open. It may not
// change the plan the human is reading, so it is shown here instead of being lost.
function agentNote(d){
  return d.agent_note?'<div class="summary agent"><span class="lbl">에이전트 추가 의견</span>'+
    esc(d.agent_note)+'</div>':'';
}
// The circuit breaker stopped an agent that kept repeating a step. The human is not
// judging the plan's wording here but deciding how the agent continues - so no per-task
// review, and the button set depends on whether there is a draft to approve as it stands.
function haltCard(d){
  // origin 'user': the human stopped a running plan from its run card (3.1.0). Same
  // decisions as a breaker halt - continue, with or without a direction, or cancel -
  // under words that do not suggest the agent did something wrong.
  const tasks=d.tasks||[],user=d.origin==='user';
  let h='<h1>'+(user?'실행 멈춤':'반복 감지')+' · '+esc(d.plan_id)+'</h1>'+verNote(d)+
    '<p class="goal"><span class="lbl">목표</span>'+esc(d.goal)+'</p>'+
    '<div class="summary warn"><span class="lbl">'+(user?'멈춘 이유':'에이전트가 멈춘 이유')+
    '</span>'+esc(d.summary)+'</div>';
  if(tasks.length){
    h+='<p class="tasklabel">'+(d.draft?'현재 초안 · 태스크 ':'태스크 ')+tasks.length+'개</p>'+
      '<div class="tasks">'+tasks.map(t=>'<div class="task"><div class="tt"><span class="tn">'+
      esc(String(t.task_id))+'.</span>'+
      (hasChoice(t)&&d.draft?'<span>'+choiceHeading(t)+'</span>'
                   :'<span>'+esc(t.title)+'</span>')+
      (t.status&&t.status!=='PENDING'?'<span class="badge">'+esc(t.status)+'</span>':'')+
      '</div>'+(d.draft?optionsHtml(d,t,false):'')+dwRow(d,t,false)+
      // What is already done, for a plan the human stopped: it is what they weigh
      // when deciding whether the rest should run.
      (user&&t.status==='DONE'&&(t.result_log||'').trim()
        ?'<div class="ev">\\u2192 '+esc(t.result_log.trim())+'</div>':'')+
      '</div>').join('')+'</div>';
  }
  h+=chip(d);
  h+='<textarea id="c-'+esc(d.id)+
    '" placeholder="에이전트에게 전할 방향을 입력해 주십시오 (선택 사항)"></textarea>';
  h+='<div class="row">';
  if(d.draft)h+='<button class="ok" id="ok-'+esc(d.id)+'" onclick="decide(\\''+esc(d.id)+
    '\\',\\'APPROVED\\')">이 초안으로 승인</button>';
  h+='<button class="rev" id="rev-'+esc(d.id)+'" onclick="decide(\\''+esc(d.id)+
    '\\',\\'REVISE\\')">'+haltLabel(false)+'</button>';
  return h+'<button class="no" onclick="decide(\\''+esc(d.id)+
    '\\',\\'REJECTED\\')">'+(user?'계획 취소':'취소')+'</button></div>';
}
// ---- a plan that is running (3.1.0) ---------------------------------------------
// Between the plan approval and the completion report there used to be nothing here.
// This card is not a question, so it raises no alarm: it shows what is finished and what
// is in progress, and lets the human stop the plan before its next task or change what
// is left. Either takes effect when the agent next reports a task - the server cannot
// interrupt a tool that is already running, and the page says so.
let RUNS=[];
function runId(r){return 'run-'+r.plan_id;}
const RUN_STATE={DONE:'완료',IN_PROGRESS:'진행 중',FAILED:'실패',PENDING:'대기'};
const RUN_HINT='실행 중인 계획은 에이전트가 태스크를 보고할 때마다 갱신됩니다. [멈춤]과 '+
  '[의견 전달]은 에이전트가 진행 중인 태스크를 보고하는 시점에 적용되며, 이미 실행 중인 '+
  '도구는 중단되지 않습니다.<br>';
function clock(ts){
  if(typeof ts!=='number')return '';
  const d=new Date(ts*1000);
  return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
}
function runRows(r){
  const tasks=r.tasks||[],fin=tasks.filter(t=>t.status==='DONE').length;
  return '<p class="tasklabel">태스크 '+tasks.length+'개 · 완료 '+fin+'개</p>'+
    '<div class="tasks">'+tasks.map(t=>{
    const took=t.status==='DONE'?tookText(t.duration_sec):'';
    let row='<div class="task"><div class="tt"><span class="tn">'+esc(String(t.task_id))+
      '.</span><span>'+esc(t.title)+'</span><span class="badge'+
      (t.status==='IN_PROGRESS'?' now':'')+'">'+(RUN_STATE[t.status]||esc(t.status))+
      '</span>'+(took?'<span class="took">소요 '+took+'</span>':'')+'</div>';
    row+=dwRow({id:runId(r)},t,false);
    if(t.status==='DONE'){
      const ev=(t.result_log||'').trim();
      row+=ev?'<div class="ev">\\u2192 '+esc(ev)+'</div>'
             :'<div class="ev none">(증거 기록 없음)</div>';
      row+=checksHtml(t);
    }
    return row+'</div>';
  }).join('')+'</div>';
}
function runCard(r){
  const id=runId(r),pid=esc(r.plan_id),c=r.control;
  PHASE[id]='RUN';
  const seenAt=clock(r.updated_at);
  const h='<h1>실행 중 · '+pid+'</h1>'+verNote(r)+
    '<p class="goal"><span class="lbl">목표</span>'+esc(r.goal)+'</p>'+runRows(r)+
    (seenAt?'<p class="seen">에이전트의 마지막 보고 '+seenAt+'</p>':'');
  // Asked for, not yet applied: say exactly that, and let it be taken back.
  if(c)return h+'<div class="pendingctl">'+(c.action==='PAUSE'
    ?'멈춤을 요청하셨습니다. 에이전트가 진행 중인 태스크를 보고하면 다음 태스크를 시작하지 '+
     '않고 멈춥니다.'
    :'의견을 전달하셨습니다. 에이전트가 진행 중인 태스크를 보고하면, 남은 태스크를 의견에 맞게 '+
     '고쳐 다시 승인을 요청합니다.')+
    (c.comment?'<span class="q">'+esc(c.comment)+'</span>':'')+
    '<button class="linkbtn" type="button" onclick="control(\\''+pid+
    '\\',\\'CLEAR\\')">요청 취소</button></div>';
  return h+'<textarea id="c-'+esc(id)+'" placeholder="남은 태스크를 바꾸고 싶으실 때 의견을 '+
    '입력해 주십시오 (예: 요약은 표로 정리해 주세요)"></textarea>'+
    '<div class="row"><button class="rev" type="button" onclick="control(\\''+pid+
    '\\',\\'NOTE\\')">의견 전달</button><button class="no" type="button" onclick="control(\\''+
    pid+'\\',\\'PAUSE\\')">멈춤</button></div>';
}
function restoreRun(r){
  const box=document.getElementById('c-'+runId(r));
  if(box)box.value=dget(runId(r),'_all');
}
async function control(pid,action){
  if(busy)return;
  const id='run-'+pid,box=document.getElementById('c-'+id);
  const text=box?box.value.trim():'';
  if(action==='NOTE'&&!text){
    if(box){box.placeholder='전달하실 의견을 먼저 입력해 주십시오';box.focus();}
    return;
  }
  busy=true;
  lock(id);
  // Taking a request back puts its words back in the box: nothing typed is dropped.
  const run=RUNS.find(r=>r.plan_id===pid);
  const back=action==='CLEAR'&&run&&run.control?(run.control.comment||''):'';
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({plan_id:pid,action:action,comment:text})});
    const j=await r.json();
    if(j&&j.ok){
      if(action==='CLEAR'){if(back)dset(id,'_all',back);}else dclear(id);
      lastError='';
    }else lastError='요청을 기록하지 못했습니다. 계획이 이미 다음 단계로 넘어갔을 수 있으니 '+
      '화면을 확인해 주십시오.';
  }catch(e){lastError='요청을 전송하지 못했습니다. 다시 시도해 주십시오.';}
  busy=false;stale(id);poll();
}
const DONE_LABEL={APPROVED:'승인되었습니다',REJECTED:'거절되었습니다',REVISE:'수정 요청되었습니다'};
const HALT_DONE_LABEL={APPROVED:'초안이 승인되었습니다',REJECTED:'취소되었습니다',
  REVISE:'계속 진행하도록 했습니다'};
// ---- drawing: one card at a time ---------------------------------------------------
// Each request and each running plan is its own card, and a card is redrawn only when
// what it shows has changed. Before, any change rebuilt every card: with run cards on
// the page that meant a task reported by one plan redrew the approval request of another
// that the human was in the middle of reading - an opened comment box closed, a
// selection was lost, the caret had to be put back. What is typed was always safe (it
// is a draft); now the card it was typed into is not touched at all.
const NODES={};   // key -> the card's element
const SHOWN={};   // key -> what that card shows now; a different value means "redraw"
// Matches the static placeholder above, so the page does not flicker between two
// different wordings when the first poll lands.
const IDLE_HTML='<div class="idle">현재 대기 중인 승인 요청이 없습니다.<br>'+
  '<span style="font-size:.85rem">에이전트가 계획을 제출하면 이곳에 표시됩니다.</span></div>';
function reqKey(d){return 'q-'+d.id;}
// The request's own content is fixed by its id - a changed plan is a new request - so
// only what can change under one id is in the signature.
function reqSig(d){
  return (d.decided||'')+':'+(d.agent_waiting?1:0)+':'+(d.agent_note||'').length;
}
function runSig(r){return (r.rev||'')+':'+(r.control?r.control.id:'');}
// The buttons of one card, switched off while its decision is on its way. Only that
// card's: no other card is redrawn by this decision, so nothing would switch theirs
// back on - and the information icon lives outside #root altogether.
function lock(key){
  const node=NODES[key];
  if(node)node.querySelectorAll('button').forEach(b=>b.disabled=true);
}
// Redraw this card at the next poll whatever its signature says: its buttons were
// switched off, and a decision that could not be recorded changes nothing else.
function stale(key){delete SHOWN[key];}
function hintHtml(list){
  const anyChoice=list.some(d=>!d.decided&&(d.tasks||[]).some(hasChoice));
  const anyPlan=list.some(d=>!d.decided&&d.phase==='PLAN'&&(d.tasks||[]).length);
  return (RUNS.length?RUN_HINT:'')+(!list.length?'':(anyChoice?'선택지가 있는 태스크는 [권장]안이 기본으로 선택되어 있습니다. '+
    '다른 안을 고르면 승인 버튼에 반영 내용이 표시되고, 기타를 고르면 수정 요청으로 바뀝니다.<br>':'')+
    '태스크의 [의견] 또는 [다시 작업] 버튼을 누르면 해당 태스크에만 요청을 남기실 수 '+
    '있습니다. 완료 보고 단계에서는 지정하신 태스크만 다시 실행되며, 나머지 태스크의 결과는 '+
    '그대로 유지됩니다.<br>'+
    (anyPlan?'[완료 기준 추가] 또는 [수정]으로 태스크 완료 기준을 직접 적으실 수 있습니다. '+
    '적은 기준은 승인과 함께 반영되며, 수정 요청을 거치지 않습니다.<br>':'')+
    '결정하시기 전까지 해당 에이전트는 후속 작업을 진행하지 못합니다. '+
    '요청은 응답하실 때까지 사라지지 않으니 천천히 검토해 주시기 바랍니다.');
}
// Returns whether any card was added, removed or redrawn.
function draw(list){
  const root=document.getElementById('root');
  // 여러 세션이 동시에 승인을 기다릴 수 있으므로 큐 전체를 보여준다.
  const items=list.map(d=>({key:reqKey(d),sig:reqSig(d),html:()=>requestCard(d),
                            after:()=>restore(d)}))
    .concat(RUNS.map(r=>({key:runId(r),sig:runSig(r),html:()=>runCard(r),
                          after:()=>restoreRun(r)})));
  if(!items.length){
    if(root.querySelector('.idle'))return false;
    root.innerHTML=IDLE_HTML;
    Object.keys(NODES).forEach(k=>{delete NODES[k];delete SHOWN[k];});
    return true;
  }
  let changed=false;
  const idle=root.querySelector('.idle');
  if(idle)idle.remove();
  // The line that says a click could not be recorded. Its own element, so showing or
  // clearing it redraws no card.
  let err=root.querySelector('.err');
  if(lastError){
    if(!err){err=document.createElement('div');err.className='err';root.insertBefore(err,root.firstChild);}
    if(err.textContent!==lastError)err.textContent=lastError;
  }else if(err)err.remove();
  let hint=root.querySelector('p.hint');
  if(!hint){hint=document.createElement('p');hint.className='hint';root.appendChild(hint);}
  // Cards whose request was collected, or whose plan stopped running.
  const wanted={};
  items.forEach(it=>{wanted[it.key]=true;});
  Object.keys(NODES).forEach(k=>{
    if(wanted[k])return;
    NODES[k].remove();delete NODES[k];delete SHOWN[k];changed=true;
  });
  // In order: requests first, then runs. A card already in its place is not moved -
  // moving a node takes the focus out of it just as redrawing would.
  let cursor=root.querySelector('.item');
  items.forEach(it=>{
    let node=NODES[it.key];
    if(!node){
      node=document.createElement('div');
      node.className='item';
      node.setAttribute('data-key',it.key);
      NODES[it.key]=node;
    }
    if(node!==cursor)root.insertBefore(node,cursor||hint);
    cursor=node.nextElementSibling;
    if(cursor&&!cursor.classList.contains('item'))cursor=null;
    if(SHOWN[it.key]===it.sig)return;
    const focus=node.contains(document.activeElement)?focusKey():null;
    node.innerHTML=it.html();
    SHOWN[it.key]=it.sig;
    it.after();
    refocus(focus);
    changed=true;
  });
  const text=hintHtml(list);
  if(hint.getAttribute('data-src')!==text){hint.innerHTML=text;hint.setAttribute('data-src',text);}
  return changed;
}
function requestCard(d){
  if(d.decided){
    const label=(d.phase==='HALT'?HALT_DONE_LABEL:DONE_LABEL)[d.decided]||d.decided;
    return '<div class="done">'+esc(d.plan_id)+' — '+label+
      '<br><span style="font-weight:400;opacity:.6;font-size:.9rem">'+
      '에이전트가 이 결정을 반영합니다.</span></div>';
  }
  // An entry with no phase was published by an older server process on this same
  // state directory. It has no per-task review, so fall back to the original form.
  PHASE[d.id]=d.phase;
  if(d.phase==='HALT')return haltCard(d);
  const perTask=(d.phase==='PLAN'||d.phase==='COMPLETION')&&d.tasks&&d.tasks.length;
  // The fallback path keeps the original layout on purpose: `display` already opens
  // with its own title, 목표 and summary, so composing a header above it would repeat
  // all three.
  let html=perTask?header(d)+(d.phase==='COMPLETION'?triage(d):'')+taskRows(d)
    :'<h1>'+(d.phase==='COMPLETION'?'완료 확인':'승인 요청')+' · '+esc(d.plan_id)+
     '</h1>'+verNote(d)+'<p class="goal">'+esc(d.goal)+'</p><pre>'+esc(d.display)+'</pre>';
  html+=agentNote(d);
  html+=chip(d);
  html+='<textarea id="c-'+esc(d.id)+
    '" placeholder="전체 의견을 입력해 주십시오 (거절 사유도 여기에 입력하실 수 있습니다)"></textarea>';
  if(perTask)html+='<label class="scope"><input type="checkbox" id="all-'+esc(d.id)+
    '"> '+
    (d.phase==='COMPLETION'?'계획 자체를 다시 세우기 (태스크 추가·삭제·순서 변경 시 선택)'
                           :'계획 전체를 다시 세우기 (태스크 추가·삭제·순서 변경 시 선택)')+
    '</label>';
  return html+'<div class="row">'+
    '<button class="ok" id="ok-'+esc(d.id)+'" onclick="decide(\\''+esc(d.id)+
    '\\',\\'APPROVED\\')">승인</button>'+
    '<button class="rev" id="rev-'+esc(d.id)+'" onclick="decide(\\''+esc(d.id)+
    '\\',\\'REVISE\\')">'+(perTask?revLabel(d.phase,[],false):'수정 요청')+'</button>'+
    '<button class="no" onclick="decide(\\''+esc(d.id)+
    '\\',\\'REJECTED\\')">거절</button></div>';
}
async function decide(id,dec){
  if(busy)return;busy=true;alertOff();
  // Only this card's buttons, which its next redraw replaces - see lock().
  lock('q-'+id);
  const box=document.getElementById('c-'+id);
  const c=box?box.value:'';
  // Task comments travel with every decision, not just a targeted one: even when the
  // whole plan is being rewritten, what the human said about task 3 is useful context.
  try{
    const r=await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id,decision:dec,comment:c,
        task_comments:comments(id),scope:scopeOf(id),choices:choicesOf(id),
        criteria:criteriaOf(id)})});
    const j=await r.json();
    // Only once the decision is recorded. Clearing first would lose the text if it was
    // not, and this is the one copy of it.
    if(j&&j.ok){dclear(id);lastError='';}
    else lastError='결정을 기록하지 못했습니다. 화면이 최신이 아닐 수 있으니 잠시 후 다시 시도해 주십시오.';
  }catch(e){lastError='결정을 전송하지 못했습니다. 다시 시도해 주십시오.';}
  busy=false;stale('q-'+id);poll();
}
// Bound to #root rather than to each textarea: #root outlives every redraw, so the
// listeners survive whichever cards come, go or are drawn again.
document.getElementById('root').addEventListener('input',onInput);
document.getElementById('root').addEventListener('change',onInput);
window.addEventListener('storage',onStorage);
// ---- information dialog -------------------------------------------------------
// The small "i" beside the version: author, contact and version. A native <dialog>, so
// Esc closes it and focus stays inside it; a click on the backdrop closes it too (the
// dialog itself has no padding, so only the backdrop has it as the click target).
function aboutOpen(){
  const d=document.getElementById('about');
  if(d.showModal){if(!d.open)d.showModal();}
  else alert(d.textContent.trim().replace(/\\s*\\n\\s*/g,'\\n').replace(/\\n닫기$/,''));
}
function aboutClose(){const d=document.getElementById('about');if(d.open)d.close();}
document.getElementById('about-open').addEventListener('click',aboutOpen);
document.getElementById('about-close').addEventListener('click',aboutClose);
document.getElementById('about').addEventListener('click',ev=>{
  if(ev.target===ev.currentTarget)aboutClose();});
poll();setInterval(poll,1500);
</script></body></html>"""

_VERSION_TOKEN = "__PLANNING_MCP_VERSION__"
_AUTHOR_TOKEN = "__PLANNING_MCP_AUTHOR__"
_EMAIL_TOKEN = "__PLANNING_MCP_EMAIL__"


def page_html(
    version: str = SERVER_VERSION,
    author: str = SERVER_AUTHOR,
    email: str = SERVER_AUTHOR_EMAIL,
) -> str:
    """The page as it is served: the template with this server's version filled in.

    Filled in when the page is served rather than fetched by script, so the label is
    there in the very first paint - including the idle state, before any poll - and
    cannot be lost to a re-render. Only characters a version string can have go in: the
    value lands inside HTML text and a JavaScript string literal. Author and email land
    in HTML text only (the information dialog) and are escaped for it.
    """
    safe = "".join(ch for ch in str(version) if ch.isalnum() or ch in ".-+") or "?"
    return (
        _PAGE.replace(_VERSION_TOKEN, safe)
        .replace(_AUTHOR_TOKEN, html.escape(str(author)))
        .replace(_EMAIL_TOKEN, html.escape(str(email)))
    )


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """Refuses to share its port.

    http.server sets allow_reuse_address, and on Windows SO_REUSEADDR lets a second
    process bind a port another process is already listening on. That would break the
    single-page guarantee: two instances would both think they own the URL.
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        exclusive_opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_opt is not None:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive_opt, 1)
            except OSError:
                pass
        super().server_bind()


class ApprovalServer:
    """Serves the shared approval state on one stable localhost URL."""

    def __init__(
        self,
        store: ApprovalStore,
        port: int = 8765,
        open_browser: bool = True,
        takeover_interval: float = 5.0,
    ):
        self.store = store
        # Plans that are running, and what the human asked of them (3.1.0). Beside the
        # approval state, in the same directory, for the same reason: any process may
        # write it and the one that owns the page reads it.
        self.runs = RunBoard(store.state_dir)
        self.base_port = port
        self.port = port
        self.open_browser = open_browser
        self.takeover_interval = takeover_interval
        self._httpd: _ExclusiveHTTPServer | None = None
        self._lock = threading.Lock()
        # When we last launched a browser - NOT whether we ever did. A permanent latch
        # here meant closing the tab closed the door: the human never saw another
        # request. See `_open_browser`.
        self._last_open_at = 0.0
        # Consecutive launches after which no tab ever polled. Backs the grace off so a
        # browser that cannot open is not relaunched on every slice forever.
        self._opens_without_contact = 0
        self._stop = threading.Event()
        # When the page last asked for work. A live tab makes opening another one pure
        # harm: the human ends up with several windows holding different half-written
        # comments, and whichever one they submit from silently discards the rest.
        self._last_poll_at = 0.0
        self._last_poll_written_at = 0.0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def owns_page(self) -> bool:
        return self._httpd is not None

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> bool:
        """Take the page if it is free, otherwise confirm a peer already serves it."""
        if self._try_bind():
            # Same liveness question as `_surface`: a tab left open across our restart is
            # still watching, and a second window would only split the human's attention.
            if self.open_browser and not self.page_is_being_watched():
                self._open_browser()
            self._start_takeover_watch()
            return True

        peer = self._probe_peer(self.base_port)
        if peer == "ours":
            log.info(
                "Approval page already served by another planning-mcp instance on %s; "
                "publishing to the shared state it reads",
                self.url,
            )
            self._start_takeover_watch()
            return True

        log.error(
            "Port %s is held by something that is not a planning-mcp approval page for "
            "this state directory. The approval page is unavailable.",
            self.base_port,
        )
        return False

    def _try_bind(self) -> bool:
        with self._lock:
            if self._httpd is not None:
                return True
            try:
                httpd = _ExclusiveHTTPServer(("127.0.0.1", self.base_port), self._make_handler())
            except OSError:
                return False
            self._httpd = httpd
            self.port = self.base_port
            threading.Thread(target=httpd.serve_forever, name="approval-ui", daemon=True).start()
            log.info("Approval UI listening on %s", self.url)
            return True

    def _probe_peer(self, port: int) -> str:
        """Is the occupant of `port` a planning-mcp page for our state directory?"""
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                info = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - any failure means "not ours"
            return "unknown"
        if info.get("server") != SERVER_SIGNATURE:
            return "foreign"
        try:
            same = Path(info.get("state_dir", "")).resolve() == self.store.state_dir.resolve()
        except OSError:
            same = False
        return "ours" if same else "other-state-dir"

    def _start_takeover_watch(self) -> None:
        """Keep trying to own the page so a dead owner is replaced automatically."""
        if self._httpd is not None:
            return  # we already own it
        def watch() -> None:
            while not self._stop.wait(self.takeover_interval):
                if self._try_bind():
                    log.warning("Took over the approval page on %s", self.url)
                    return
        threading.Thread(target=watch, name="approval-takeover", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    # ---- request flow --------------------------------------------------
    def open_request(
        self, plan_id: str, goal: str, display: str, tasks: list[dict[str, Any]],
        fingerprint: str = "", phase: str = PHASE_PLAN, summary: str = "",
        draft: bool = False, origin: str = "",
    ) -> str | None:
        request_id = self.store.publish(
            plan_id, goal, display, tasks, fingerprint, phase, summary, draft, origin
        )
        self._surface()
        return request_id

    # ---- the run board (3.1.0) -------------------------------------------
    def sync_runs(self, wanted: dict[str, dict[str, Any]]) -> bool:
        return self.runs.sync(wanted)

    def take_run_control(self, plan_id: str) -> dict[str, Any] | None:
        return self.runs.take(plan_id)

    def run_control_pending(self, plan_id: str) -> dict[str, Any] | None:
        return self.runs.pending(plan_id)

    def claim(self, request_id: str) -> Verdict | None:
        return self.store.claim(request_id)

    def has_pending(self, plan_id: str, fingerprint: str) -> bool:
        return self.store.has_pending(plan_id, fingerprint)

    def pending_request(self, plan_id: str, fingerprint: str) -> dict[str, Any] | None:
        return self.store.pending_entry(plan_id, fingerprint)

    def set_agent_note(self, request_id: str, note: str) -> None:
        self.store.set_agent_note(request_id, note)

    def request_age(self, request_id: str) -> float:
        return self.store.request_age(request_id)

    def touch_agent(self, request_id: str) -> None:
        self.store.touch_agent(request_id)

    def take_decision(self, plan_id: str, fingerprint: str) -> Verdict | None:
        return self.store.claim_for_plan(plan_id, fingerprint)

    def drop_for_plan(self, plan_id: str) -> None:
        self.store.drop_for_plan(plan_id)

    def note_poll(self) -> None:
        """A tab just asked us for work. Publish that where peers can see it.

        Throttled: the page polls every 1.5s and only `PAGE_IDLE_SEC` resolution matters,
        so there is no reason to touch the disk on every request.
        """
        now = time.time()
        self._last_poll_at = now
        if (now - self._last_poll_written_at) >= PAGE_SEEN_WRITE_SEC:
            self._last_poll_written_at = now
            self.store.mark_page_seen(now)

    def page_is_being_watched(self) -> bool:
        """Has a tab polled *anyone* recently enough to count as open?

        The page polls every 1.5s, so a gap of PAGE_IDLE_SEC means every tab is gone (or
        was never opened).

        The answer has to come from the state directory, not from this process. Only one
        process owns the page; every other planning-mcp instance on the same state dir is
        a peer whose own `_last_poll_at` stays at zero forever, because the polls go to
        the owner. The process that decides to open a browser is whichever one is holding
        an approval request - routinely a peer. Reading only our own counter, a peer
        concludes "no tab is open" every single time, which is how a 45s chunked wait
        turned into a new window every 45 seconds (D24). Our in-process value is still
        consulted so a fresh poll counts immediately, before it has been written out.
        """
        seen = max(self._last_poll_at, self.store.page_last_seen())
        return (time.time() - seen) < PAGE_IDLE_SEC

    def _surface(self) -> None:
        log.warning("HUMAN APPROVAL NEEDED -> %s", self.url)
        if not self.open_browser:
            return
        if self.page_is_being_watched():
            log.info("A tab is already watching %s; not opening another", self.url)
            return
        self._open_browser()

    def _open_browser(self) -> None:
        """Best-effort only. Corporate policy, a missing default browser, or a second
        monitor can all defeat this, which is why the page also polls.

        Opening is suppressed by *liveness*, never permanently. The predecessor of this
        function latched a `_opened_once` flag the first time it succeeded and refused
        forever after, so closing the approval tab meant no request ever surfaced a
        window again - the human waited on a page that was never going to appear. The
        duplicate-tab problem that latch was fighting is already handled by
        `page_is_being_watched`; all this needs to add is a grace period so the chunked
        wait's repeat calls do not launch a second browser while the first is still
        starting up.

        That grace doubles each time a launch fails to produce a tab, because "opened
        successfully" is a lie a browser can tell: policy can swallow the window, or the
        handler can be missing. Without the backoff, every slice of the wait relaunches
        it and nothing ever appears. A single poll from anywhere clears the count.
        """
        now = time.time()
        contacted = self.store.page_last_seen() >= self._last_open_at
        if self._last_open_at and not contacted:
            grace = min(
                OPEN_GRACE_SEC * (2 ** self._opens_without_contact), OPEN_GRACE_MAX_SEC
            )
        else:
            self._opens_without_contact = 0
            grace = OPEN_GRACE_SEC
        if (now - self._last_open_at) < grace:
            return
        self._opens_without_contact += 1
        self._last_open_at = now
        try:
            if webbrowser.open(self.url):
                return
        except Exception as exc:  # noqa: BLE001
            log.debug("webbrowser.open failed: %s", exc)
        try:
            os.startfile(self.url)  # type: ignore[attr-defined]
            return
        except Exception:  # noqa: BLE001
            pass
        log.warning("Could not open a browser automatically. Open %s manually.", self.url)

    # ---- http ----------------------------------------------------------
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                log.debug("approval-ui %s", fmt % args)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, payload: Any, code: int = 200) -> None:
                self._send(
                    code,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/":
                    self._send(200, page_html().encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/health":
                    self._json(
                        {"server": SERVER_SIGNATURE, "state_dir": str(server.store.state_dir)}
                    )
                elif path == "/api/pending":
                    server.note_poll()
                    now = time.time()
                    self._json({"requests": [
                        {
                            "id": e["id"],
                            "plan_id": e.get("plan_id"),
                            "goal": e.get("goal"),
                            "summary": e.get("summary") or "",
                            "display": e.get("display"),
                            # The task list was already being persisted for the
                            # fingerprint; the page needs it to offer per-task review.
                            "tasks": e.get("tasks") or [],
                            # Deliberately NOT defaulted. An entry written by an older
                            # process has no phase, and guessing PLAN would offer
                            # per-task review on what may be a completion report. The
                            # page treats a missing phase as "use the original form".
                            "phase": e.get("phase"),
                            "draft": bool(e.get("draft")),
                            "origin": e.get("origin") or "",
                            "agent_note": e.get("agent_note") or "",
                            # The asking process's version; absent on an entry written
                            # by a process older than 3.0.0, and the page says so.
                            "version": e.get("server_version"),
                            "decided": e.get("decision"),
                            # Whether an agent is still holding a call open for this
                            # request. Deliberately not a countdown: the request
                            # outlives any single tool call, so a deadline would be a
                            # lie. What the human cannot otherwise tell is whether
                            # deciding right now resumes the conversation on its own.
                            "agent_waiting": (
                                now - float(e.get("agent_last_seen") or 0.0)
                            ) < AGENT_IDLE_SEC,
                        }
                        for e in server.store.peek()
                    ], "runs": [
                        {
                            "plan_id": r.get("plan_id"),
                            "goal": r.get("goal"),
                            "tasks": r.get("tasks") or [],
                            # Changes exactly when what the card shows changes.
                            "rev": r.get("rev") or "",
                            "version": r.get("server_version"),
                            "updated_at": r.get("updated_at"),
                            "control": (
                                {
                                    "id": r["control"].get("id"),
                                    "action": r["control"].get("action"),
                                    "comment": r["control"].get("comment") or "",
                                }
                                if isinstance(r.get("control"), dict) else None
                            ),
                        }
                        for r in server.runs.peek()
                    ]})
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                route = urlparse(self.path).path
                if route not in ("/api/decide", "/api/control"):
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.send_error(400)
                    return
                if not isinstance(body, dict):
                    self.send_error(400)
                    return
                if route == "/api/control":
                    # A running plan: stop it, change what is left, or take that back.
                    self._json({"ok": server.runs.request(
                        str(body.get("plan_id", "")),
                        str(body.get("action", "")).upper(),
                        body.get("comment", ""),
                    )})
                    return
                ok = server.store.record_decision(
                    str(body.get("id", "")),
                    str(body.get("decision", "")).upper(),
                    body.get("comment", ""),
                    body.get("task_comments"),
                    body.get("scope"),
                    body.get("choices"),
                    body.get("criteria"),
                )
                self._json({"ok": ok})

        return Handler
