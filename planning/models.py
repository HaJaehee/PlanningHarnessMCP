"""Domain model: enums and dataclasses for the planning MCP server.

These enums are the single source of truth. `schemas.py` builds the advertised
tool schemas from them, and `state_machine.py` validates against them, so the
schema the LLM sees can never drift from what the server actually accepts.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def now_iso() -> str:
    """Local-time ISO8601 with offset, second precision (human-readable in the state file)."""
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def seconds_since(stamp: str | None) -> float:
    """Age of an ISO timestamp in seconds.

    Returns infinity when the value is missing or unparseable: an approval whose age
    cannot be established must be treated as expired, never as fresh.
    """
    if not stamp:
        return float("inf")
    try:
        then = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.astimezone()
    return (datetime.datetime.now().astimezone() - then).total_seconds()


class PlanStatus(str, Enum):
    NONE = "NONE"
    DRAFTING = "DRAFTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    IN_EXECUTION = "IN_EXECUTION"
    # Every task is marked DONE, but the human has not yet verified the evidence.
    # A weak model marking tasks complete without doing the work is the failure this
    # state exists to catch, so COMPLETED is no longer reachable without a human.
    AWAITING_COMPLETION = "AWAITING_COMPLETION"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"


class Decision(str, Enum):
    ASK_USER = "ASK_USER"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISE = "REVISE"


class NextAction(str, Enum):
    CALL_PLAN_AND_THINK = "CALL_PLAN_AND_THINK"
    CALL_REQUEST_USER_APPROVAL = "CALL_REQUEST_USER_APPROVAL"
    CALL_UPDATE_TASK_PROGRESS = "CALL_UPDATE_TASK_PROGRESS"
    CALL_GET_CURRENT_PLAN = "CALL_GET_CURRENT_PLAN"
    STOP_AND_WAIT_FOR_USER = "STOP_AND_WAIT_FOR_USER"
    ANSWER_USER = "ANSWER_USER"


class ErrorCode(str, Enum):
    PLAN_NOT_APPROVED = "PLAN_NOT_APPROVED"
    PLAN_NOT_READY = "PLAN_NOT_READY"
    PLAN_BLOCKED = "PLAN_BLOCKED"
    PLAN_CANCELLED = "PLAN_CANCELLED"
    NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    MISSING_TASK_LIST = "MISSING_TASK_LIST"
    MISSING_PLAN_SUMMARY = "MISSING_PLAN_SUMMARY"
    APPROVAL_NOT_REQUESTED = "APPROVAL_NOT_REQUESTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    PLAN_AMBIGUOUS = "PLAN_AMBIGUOUS"
    GOAL_NOT_MATCHED = "GOAL_NOT_MATCHED"
    TASK_NOT_STARTED = "TASK_NOT_STARTED"
    TASK_OUT_OF_ORDER = "TASK_OUT_OF_ORDER"
    MISSING_RESULT_LOG = "MISSING_RESULT_LOG"
    REWORK_NOT_DONE = "REWORK_NOT_DONE"
    COMPLETION_PENDING = "COMPLETION_PENDING"
    REVISION_NOT_REQUESTED = "REVISION_NOT_REQUESTED"
    REVISION_INCOMPLETE = "REVISION_INCOMPLETE"
    INVALID_STATUS = "INVALID_STATUS"
    INVALID_DECISION = "INVALID_DECISION"
    INVALID_STEP = "INVALID_STEP"
    # The circuit breaker paused this plan: the same step kept repeating with no human
    # in between. Only the human can lift it (see handlers._trip / D25).
    LOOP_HALTED = "LOOP_HALTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def _safe_int(value: Any, default: int) -> int:
    """An int from a state file that may have been hand-edited. Never raises."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


TERMINAL_PLAN_STATUSES = (PlanStatus.COMPLETED, PlanStatus.CANCELLED)
EXECUTABLE_PLAN_STATUSES = (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION)


@dataclass
class Task:
    task_id: int
    title: str
    status: str = TaskStatus.PENDING.value
    result_log: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # Set when the human commented on THIS task on the approval page and the model
    # rewrote it in response. Both fields exist only for the re-approval window: they
    # let the page show "this is the one you flagged, here is what changed" so the
    # human re-reads one line instead of the whole plan. Cleared once approved.
    revision_note: str | None = None
    previous_title: str | None = None
    # What this task produced before the human sent it back for rework. Reopening a task
    # clears result_log (the old outcome is no longer the outcome), but a model told to
    # redo work with no record of what it did the first time is working blind, and a
    # small one will simply produce something unrelated. Kept until the plan completes.
    previous_result_log: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "result_log": self.result_log,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "revision_note": self.revision_note,
            "previous_title": self.previous_title,
            "previous_result_log": self.previous_result_log,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Task":
        return cls(
            task_id=int(raw.get("task_id", 0)),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", TaskStatus.PENDING.value)),
            result_log=raw.get("result_log"),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            revision_note=raw.get("revision_note"),
            previous_title=raw.get("previous_title"),
            previous_result_log=raw.get("previous_result_log"),
        )

    def clear_revision_marks(self) -> None:
        self.revision_note = None
        self.previous_title = None
        self.previous_result_log = None

    def brief(self) -> dict[str, Any]:
        """The task as everything outside the store sees it. Evidence is NEVER truncated.

        `result_log` used to be cut at 200 characters "to protect context". That cap
        reached the human: the approval page is built from this same dict (handlers
        passes `tasks_brief()` straight to `open_request`), so a completion report asked
        someone to certify work whose evidence ended in `...`. Judging a claim you can
        only see the first sentence of is not a check, and the full text was sitting in
        `plan_state.json` the whole time.

        The model needs it whole for the same reason: on a rework it is handed
        `previous_result_log` precisely so it can tell what it already produced and what
        was missing, and a truncated one hides the ending - usually where the gap is.

        If evidence ever does threaten context, cap it where it is *written* (a maximum
        next to `min_result_log`), not where it is read: a limit at the read point
        silently disagrees with what the store holds.
        """
        out: dict[str, Any] = {"task_id": self.task_id, "title": self.title, "status": self.status}
        if self.result_log:
            out["result_log"] = self.result_log
        if self.revision_note:
            out["revision_note"] = self.revision_note
        if self.previous_title:
            out["previous_title"] = self.previous_title
        if self.previous_result_log:
            out["previous_result_log"] = self.previous_result_log
        return out


@dataclass
class ThinkingStep:
    step_number: int
    thought: str
    superseded: bool = False
    revises_step: int | None = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_number": self.step_number,
            "thought": self.thought,
            "superseded": self.superseded,
            "revises_step": self.revises_step,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ThinkingStep":
        return cls(
            step_number=int(raw.get("step_number", 1)),
            thought=str(raw.get("thought", "")),
            superseded=bool(raw.get("superseded", False)),
            revises_step=raw.get("revises_step"),
            created_at=raw.get("created_at") or now_iso(),
        )


@dataclass
class Approval:
    requested_at: str | None = None
    decided_at: str | None = None
    decision: str | None = None
    user_comment: str | None = None
    revision_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_at": self.requested_at,
            "decided_at": self.decided_at,
            "decision": self.decision,
            "user_comment": self.user_comment,
            "revision_count": self.revision_count,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Approval":
        raw = raw or {}
        return cls(
            requested_at=raw.get("requested_at"),
            decided_at=raw.get("decided_at"),
            decision=raw.get("decision"),
            user_comment=raw.get("user_comment"),
            revision_count=int(raw.get("revision_count", 0)),
        )

    def reset_request(self) -> None:
        """Called whenever the plan returns to DRAFTING: a changed plan needs a fresh approval."""
        self.requested_at = None
        self.decided_at = None
        self.decision = None


MAX_GOAL_HISTORY = 20


@dataclass
class Plan:
    plan_id: str
    goal: str
    plan_status: str = PlanStatus.DRAFTING.value
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    total_steps: int = 1
    thinking_steps: list[ThinkingStep] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    approval: Approval = field(default_factory=Approval)
    superseded_tasks: list[list[dict[str, Any]]] = field(default_factory=list)
    # The goal as first stated, kept as the audit anchor. `goal` itself is mutable: an
    # agent's first reading of an ambiguous request is often wrong, and a system that
    # cannot absorb "no, I meant B" ends up carrying the wrong goal in its metadata
    # while executing the right tasks. Correction is recorded, not forbidden.
    original_goal: str = ""
    goal_history: list[dict[str, Any]] = field(default_factory=list)
    # Set only when the human asked for a TARGETED revision: {"targets": {"3": "comment"}}.
    # Its presence is what makes the server demand task_updates instead of a whole new
    # task_list, and what tells it which tasks the model is allowed to touch. Cleared as
    # soon as the revision is applied, so it can never authorize a later edit.
    pending_revision: dict[str, Any] | None = None
    # Set when a WHOLE-PLAN revision was asked for from the completion report rather than
    # from the plan-approval screen. The work has already been done at that point, so the
    # redraft must not throw its evidence away: tasks that survive the rewrite keep what
    # they produced. Consumed (and cleared) by the next finalization in plan_and_think.
    # A revision asked for before execution has no evidence to keep, and an ordinary
    # re-plan after a failure SHOULD drop it - hence a flag rather than always-on.
    rework_from_completion: bool = False
    # The latest task_list the model sent while still thinking. Kept so that a model
    # which never calls its own plan final - the self-verification loop of D25 - still
    # has something the server can put in front of the human when its thinking budget
    # runs out. Cleared whenever a task list is finalized.
    draft_tasks: list[str] = field(default_factory=list)
    # The step number at which this drafting round's thinking budget is spent. 0 means
    # "not started": the next thinking step opens a fresh round. Reset to 0 whenever the
    # plan re-enters DRAFTING (a human asked for changes, a task failed, a halt was lifted).
    step_budget_end: int = 0
    # Set by the circuit breaker. While present, no tool may move this plan; the human
    # decides on the approval page (or, without a page, in chat). Keys: id, reason,
    # detail, at, asked, channel ("page" | "chat").
    halt: dict[str, Any] | None = None
    # What the human said when they lifted a halt. Leads every hint until the next
    # milestone, for the same reason `_rework_suffix` exists: a sentence the model saw
    # once, three turns back, is a sentence it no longer has.
    guidance: str | None = None

    # ---- serialization -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "plan_status": self.plan_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_steps": self.total_steps,
            "thinking_steps": [s.to_dict() for s in self.thinking_steps],
            "tasks": [t.to_dict() for t in self.tasks],
            "approval": self.approval.to_dict(),
            "superseded_tasks": self.superseded_tasks,
            "pending_revision": self.pending_revision,
            "rework_from_completion": self.rework_from_completion,
            "original_goal": self.original_goal or self.goal,
            "goal_history": self.goal_history,
            "draft_tasks": self.draft_tasks,
            "step_budget_end": self.step_budget_end,
            "halt": self.halt,
            "guidance": self.guidance,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Plan":
        return cls(
            plan_id=str(raw.get("plan_id", "")),
            goal=str(raw.get("goal", "")),
            plan_status=str(raw.get("plan_status", PlanStatus.DRAFTING.value)),
            created_at=raw.get("created_at") or now_iso(),
            updated_at=raw.get("updated_at") or now_iso(),
            total_steps=int(raw.get("total_steps", 1)),
            thinking_steps=[ThinkingStep.from_dict(s) for s in raw.get("thinking_steps", [])],
            tasks=[Task.from_dict(t) for t in raw.get("tasks", [])],
            approval=Approval.from_dict(raw.get("approval")),
            superseded_tasks=raw.get("superseded_tasks", []),
            pending_revision=raw.get("pending_revision") or None,
            rework_from_completion=bool(raw.get("rework_from_completion")),
            # A state file written before goal revision existed has neither field; its
            # goal has never changed, so it *is* the original.
            original_goal=str(raw.get("original_goal") or raw.get("goal", "")),
            goal_history=[h for h in (raw.get("goal_history") or []) if isinstance(h, dict)],
            # Fields added in 1.16.0. A file written earlier has none of them, which reads
            # as "no draft, no budget round started, not halted" - exactly right.
            draft_tasks=[str(t) for t in (raw.get("draft_tasks") or []) if isinstance(t, str)],
            step_budget_end=_safe_int(raw.get("step_budget_end"), 0),
            halt=raw.get("halt") if isinstance(raw.get("halt"), dict) else None,
            guidance=str(raw["guidance"]) if raw.get("guidance") else None,
        )

    # ---- queries -------------------------------------------------------
    @property
    def status(self) -> PlanStatus:
        try:
            return PlanStatus(self.plan_status)
        except ValueError:
            return PlanStatus.DRAFTING

    def touch(self) -> None:
        self.updated_at = now_iso()

    def idle_seconds(self) -> float:
        """How long since anything happened to this plan."""
        return seconds_since(self.updated_at)

    def approval_is_stale(self, ttl_seconds: int) -> bool:
        """An approval only authorizes work that follows it promptly.

        A plan approved this morning must not silently authorize execution in an
        unrelated conversation hours later - the human who approved it was agreeing to
        that plan, then, not to whatever the model decides to do next.
        """
        if self.status not in EXECUTABLE_PLAN_STATUSES:
            return False
        return self.idle_seconds() > ttl_seconds

    def revise_goal(self, new_goal: str, source: str = "model") -> bool:
        """Adopt a corrected goal, keeping the change on the record. False = no change.

        Auditability is what immutability was really protecting, and it survives here:
        `original_goal` is the untouched anchor and every hop is in `goal_history`. The
        history is capped, the anchor is not - trimming can lose intermediate wording but
        never the question "what was this plan originally started for?".
        """
        new = (new_goal or "").strip()
        if not new or new == self.goal:
            return False
        if not self.original_goal:
            self.original_goal = self.goal
        self.goal_history.append(
            {"at": now_iso(), "from": self.goal, "to": new, "source": source}
        )
        if len(self.goal_history) > MAX_GOAL_HISTORY:
            del self.goal_history[: -MAX_GOAL_HISTORY]
        self.goal = new
        self.touch()
        return True

    def former_goals(self) -> list[str]:
        """Every goal text this plan has previously answered to, newest first."""
        seen: list[str] = []
        for entry in reversed(self.goal_history):
            previous = str(entry.get("from") or "")
            if previous and previous not in seen:
                seen.append(previous)
        return seen

    def set_status(self, status: PlanStatus) -> None:
        self.plan_status = status.value
        self.touch()

    def get_task(self, task_id: int) -> Task | None:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def last_step_number(self) -> int:
        return max((s.step_number for s in self.thinking_steps), default=0)

    def last_thought(self) -> str:
        """The most recent thinking step's text - what the model was last weighing."""
        return self.thinking_steps[-1].thought if self.thinking_steps else ""

    def thinking_steps_left(self) -> int | None:
        """Steps left in this drafting round, or None when no budget applies."""
        if self.step_budget_end <= 0:
            return None
        return max(0, self.step_budget_end - self.last_step_number())

    def halt_draft(self) -> list[str]:
        """The task list a human could approve straight from a halt card, if any.

        While drafting that is the model's latest draft; once a list has been finalized
        but not yet shown, it is that list. Anywhere else there is nothing to approve -
        lifting the halt simply lets the plan continue.
        """
        if self.status is PlanStatus.DRAFTING:
            return list(self.draft_tasks)
        if self.status is PlanStatus.AWAITING_APPROVAL:
            return [t.title for t in self.tasks]
        return []

    def current_task(self) -> Task | None:
        """The task the model should be working on: an in-flight one, else the first pending one."""
        for t in self.tasks:
            if t.status == TaskStatus.IN_PROGRESS.value:
                return t
        for t in self.tasks:
            if t.status == TaskStatus.PENDING.value:
                return t
        return None

    def first_failed_task(self) -> Task | None:
        for t in self.tasks:
            if t.status == TaskStatus.FAILED.value:
                return t
        return None

    def done_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == TaskStatus.DONE.value)

    def progress(self) -> str:
        return f"{self.done_count()}/{len(self.tasks)} done"

    def all_done(self) -> bool:
        return bool(self.tasks) and all(t.status == TaskStatus.DONE.value for t in self.tasks)

    def remaining_tasks(self) -> list[Task]:
        """Tasks not yet finished. Used to keep 'work left' in front of the model."""
        return [t for t in self.tasks if t.status != TaskStatus.DONE.value]

    def unfinished_before(self, task_id: int) -> list[Task]:
        """Earlier tasks that are not DONE - claiming a later one is finished is a lie."""
        return [
            t for t in self.tasks
            if t.task_id < task_id and t.status != TaskStatus.DONE.value
        ]

    def tasks_without_evidence(self) -> list[Task]:
        return [
            t for t in self.tasks
            if t.status == TaskStatus.DONE.value and not (t.result_log or "").strip()
        ]

    def tasks_brief(self) -> list[dict[str, Any]]:
        return [t.brief() for t in self.tasks]

    def next_task_brief(self) -> dict[str, Any] | None:
        """The one task the model may act on now - the ONLY place an id is published.

        Execution responses used to carry the whole task list, so every turn left a
        fresh copy of it in the conversation and every hint spelled out a literal
        `task_id=N`. After five tasks the history held five such sentences, four of
        them naming the wrong task, and they read as instructions rather than as data.
        Publishing exactly one id, in one field, means a stale copy can only ever be
        stale data - there is nothing in it to execute.

        The rework fields ride along because the guard that refuses a resubmitted
        outcome (D19) is only fair if the model can see what it produced last time.
        They are the one part of the old echo that carries its weight here.
        """
        task = self.current_task()
        if task is None:
            return None
        out: dict[str, Any] = {
            "task_id": task.task_id,
            "title": task.title,
            "status": task.status,
        }
        if task.revision_note:
            out["revision_note"] = task.revision_note
        if task.previous_result_log:
            out["previous_result_log"] = task.previous_result_log
        return out

    # ---- targeted revision ---------------------------------------------
    def revision_targets(self) -> dict[int, str]:
        """{task_id: the human's comment} for a pending targeted revision, else {}.

        JSON object keys are strings, so the ids arrive as text and are coerced back
        here. An unreadable key is dropped rather than raised: a malformed marker must
        not make the whole plan unloadable.
        """
        raw = (self.pending_revision or {}).get("targets") or {}
        targets: dict[int, str] = {}
        if isinstance(raw, dict):
            for key, comment in raw.items():
                try:
                    targets[int(key)] = str(comment or "")
                except (TypeError, ValueError):
                    continue
        return targets

    def clear_revision_marks(self) -> None:
        for task in self.tasks:
            task.clear_revision_marks()
