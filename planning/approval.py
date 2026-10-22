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
# Close the plan without waiting for its agent (3.2.0). A stop and a note are applied
# when the agent next reports a task, and an agent that failed in the middle of a plan
# never does: its card stayed on the page with the stop "asked for" for good. A cancel
# takes the card off at once and is applied on the next tool call of any kind.
CONTROL_CANCEL = "CANCEL"
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
        if action not in CONTROLS + (CONTROL_CLEAR, CONTROL_CANCEL):
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
            if self._action(entry) == CONTROL_CANCEL:
                # Final: the card has left the page. A second tab that still shows it
                # can cancel again, but not stop, note or take the cancel back.
                return action == CONTROL_CANCEL
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

    @staticmethod
    def _action(entry: dict[str, Any]) -> str | None:
        control = entry.get("control")
        return control.get("action") if isinstance(control, dict) else None

    def pending(self, plan_id: str) -> dict[str, Any] | None:
        """The stop or note waiting for this plan, without consuming it."""
        entry = self.read().get(plan_id) or {}
        return entry["control"] if self._action(entry) in CONTROLS else None

    def cancels(self) -> list[str]:
        """The plans cancelled from their card that have not been closed yet."""
        return [pid for pid, entry in sorted(self.read().items())
                if self._action(entry) == CONTROL_CANCEL]

    def take(
        self, plan_id: str, actions: tuple[str, ...] = CONTROLS
    ) -> dict[str, Any] | None:
        """Consume the control for a plan. Called by the handler that applies it."""
        if self._action(self.read().get(plan_id) or {}) not in actions:
            return None  # the common case, answered without the lock
        with exclusive(self.lock_path):
            runs = self.read()
            entry = runs.get(plan_id) or {}
            if self._action(entry) not in actions:
                return None
            control = entry.pop("control")
            if not self._write(runs):
                # Not consumed, so not applied: it stays on the page as asked for and
                # the next call tries again, rather than being applied twice.
                return None
            return control

    def peek(self) -> list[dict[str, Any]]:
        """Every run that still has a live approval and was not cancelled, in plan order."""
        now = time.time()
        out = []
        for _, entry in sorted(self.read().items()):
            if self._action(entry) == CONTROL_CANCEL:
                continue
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

# The page lives in plain files beside this module - planning/web/ - so it can be read,
# edited and debugged as the HTML, CSS and JavaScript it is. It is still served as ONE
# response, as when it was a string constant here: the stylesheet goes inline in a
# <style>, each script into a <script> of its own. So the server has no static routes,
# and nothing on disk is ever looked up from a URL.
#
# Read once, when this module is imported. A process serves the page it was started
# with: after an upgrade an older process may still own the port, and new scripts
# talking to its old endpoints would be a page that half works. Held in memory, the page
# and the endpoints always come from the same code.
WEB_DIR = Path(__file__).with_name("web")
# In load order: a script may use what an earlier one defined.
PAGE_SCRIPTS = ("page.js",)
_STYLE_SLOT = "<!-- planning-mcp:style -->"
_SCRIPTS_SLOT = "<!-- planning-mcp:scripts -->"


def _web_file(name: str) -> str:
    text = (WEB_DIR / name).read_text(encoding="utf-8")
    return text if text.endswith("\n") else text + "\n"


def _assemble_page() -> str:
    """page.html with its stylesheet and scripts put inline.

    Each file ends with a sourceURL comment, so the browser's developer tools list it
    under its own name, and a breakpoint can be set in it. Its first line starts right
    after the opening tag, so the line numbers they show are the file's own.
    """
    shell = _web_file("page.html")
    if shell.count(_STYLE_SLOT) != 1 or shell.count(_SCRIPTS_SLOT) != 1:
        raise ValueError("page.html must hold the style slot and the scripts slot once each")
    style = _web_file("page.css")
    # Inline, the first "</style" or "</script" ends the tag - even inside a comment
    # or a string - and the rest of the file would be shown as text.
    if "</style" in style.lower():
        raise ValueError("page.css contains '</style', which cannot be inlined")
    scripts = []
    for name in PAGE_SCRIPTS:
        code = _web_file(name)
        if "</script" in code.lower():
            raise ValueError(f"{name} contains '</script', which cannot be inlined")
        scripts.append(f"<script>{code}//# sourceURL=planning-mcp/{name}\n</script>")
    style = f"<style>{style}/*# sourceURL=planning-mcp/page.css */\n</style>"
    return shell.replace(_STYLE_SLOT, style).replace(_SCRIPTS_SLOT, "\n".join(scripts))


_PAGE = _assemble_page()

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

    def run_cancels(self) -> list[str]:
        return self.runs.cancels()

    def take_run_cancel(self, plan_id: str) -> dict[str, Any] | None:
        return self.runs.take(plan_id, (CONTROL_CANCEL,))

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
                    # A running plan: stop it, change what is left, take that back, or
                    # cancel it without waiting for its agent.
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
