"""The four tool implementations.

Pipeline per call: RECEIVE -> LENIENCY -> VALIDATE -> GUARD -> MUTATE -> RESPOND.
No handler formats its own response; everything goes through `responses.build`.
No handler raises; `dispatch` converts any escaping exception into a resync instruction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import time

from .approval import (
    PHASE_COMPLETION,
    PHASE_HALT,
    PHASE_PLAN,
    CONTROL_NOTE,
    SCOPE_PLAN,
    SCOPE_TASKS,
    ApprovalServer,
    ApprovalStore,
    Verdict,
)
from .config import (
    APPROVAL_MODE_CHUNKED,
    APPROVAL_MODE_RETURN,
    APPROVAL_MODE_TRUST_HEARTBEAT,
    FILE_CHECK_TIMEOUT_SEC,
    NO_PROGRESS_WAIT_CEILING_SEC,
    SERVER_VERSION,
    Config,
)
from .choices import build_options, collect_topics, letter, validate_model_choices
from .evidence import (
    CONFIRMED_STATES,
    DECLARED,
    REFUSED_STATES,
    check_files,
    check_mentions,
    clean_done_when,
    confirmed,
    novelty,
    produced,
    refused,
    repeats_criterion,
)
from .evidence import normalize_evidence as _normalize_evidence
from .leniency import UNMATCHED_TITLES_KEY, normalize
from .loopguard import (
    NO_PLAN,
    REASON_ERROR_STREAK,
    REASON_NO_PROGRESS,
    REASON_REPEATED_CALL,
    REASON_RESPAWN,
    REASON_THINKING_BUDGET,
    LoopGuard,
    call_signature,
)
from .models import (
    Approval,
    Decision,
    ErrorCode,
    EXECUTABLE_PLAN_STATUSES,
    HALT_USER_PAUSE,
    ORIGIN_FAILURE,
    ORIGIN_RUN,
    Plan,
    PlanStatus,
    TERMINAL_PLAN_STATUSES,
    Task,
    TaskStatus,
    ThinkingStep,
    now_iso,
)
from .responses import (
    build,
    error,
    render_completion_report,
    render_halt_for_user,
    render_plan_for_user,
)
from .state_machine import (
    can_start_task,
    choice_note,
    done_when_note,
    execution_guard,
    repair_note,
)
from .store import State, Store, goal_key, title_key

log = logging.getLogger("planning-mcp.handlers")

# How the wait ended. The three non-decided outcomes need different things said to the
# model, and a bare `None` cannot tell them apart: "ask me again in a moment" and "stop,
# the user never answered" are opposite instructions, and sending the wrong one either
# strands a live request or spins the model forever.
WAIT_DECIDED = "DECIDED"          # a human answered; the verdict is attached
WAIT_PENDING = "PENDING"          # this call's slice is up, budget remains - call back
WAIT_GAVE_UP = "GAVE_UP"          # the whole budget is spent - show the plan and stop
WAIT_UNAVAILABLE = "UNAVAILABLE"  # the request could never be published

# How often the waiter tells the page it is still on the other end. Cheap enough to be
# invisible next to the 0.2s claim poll, frequent enough that the page's liveness chip
# flips within a few seconds of a call actually ending.
AGENT_HEARTBEAT_SEC = 10.0


@dataclass
class WaitOutcome:
    """Why `_wait_on` returned, and how much of the human's budget is left.

    A dataclass rather than a tuple for the same reason `Verdict` is one: this grew from
    a bare `Verdict | None`, and the next field added to a tuple would be silently
    dropped by every existing call site.
    """

    verdict: Verdict | None = None
    reason: str = WAIT_GAVE_UP
    waited: float = 0.0
    remaining: float = 0.0


@dataclass
class _CallCtx:
    """Per-call facts the circuit breaker needs, kept off every handler's signature.

    Thread-local because the stdio transport runs calls on worker threads: two calls in
    flight must not see each other's "this call waited on a human".
    """

    progress_token: Any = None
    notifier: Any = None
    cancel_event: Any = None
    # The call spent real time waiting on a human (or collected their decision). Such a
    # call is the harness working as designed, never a loop.
    waited: bool = False
    # Seconds this call has already spent waiting on a human. One call gets one
    # slice of the client's patience, however many requests it ends up waiting on.
    wait_spent: float = 0.0
    # Plans that moved during this call - a finalize, a decision, a finished task.
    milestones: set = field(default_factory=set)
    # A trip decided inside a handler (a respawn, a thinking budget spent with no draft)
    # that _after_call carries out once the handler has returned: (plan_id, reason, n).
    trip: tuple | None = None


# Words a model uses when it goes back over what it already decided. Counted into the
# audit log only - never acted on, because "actually" is also how people write. Their
# trend per client is the only field evidence there is for a loop inside one thinking
# block, which the server cannot see directly (D25).
_RECONSIDER = re.compile(
    r"\b(?:wait|reconsider\w*|re-?check\w*|double[- ]check\w*|actually|hmm+|let me re\w*|"
    r"on second thought)\b|다시 생각|재검토|잠깐|다시 확인|다시 보",
    re.IGNORECASE,
)


def reconsider_markers(text: str) -> int:
    return len(_RECONSIDER.findall(text or ""))


# A private key on a response, read and removed by _after_call: "this re-plan was turned
# away". Never reaches the model.
_LOOP_SIGNAL = "_loop_signal"
REPLAN_REDIRECTED = "REPLAN_REDIRECTED"
# Internal keys on the args handed to _approve: choices from the page (already validated
# against what was on screen) and choices the model relayed in chat mode (validated
# leniently against the plan). Never accepted from the model directly.
_PAGE_CHOICES = "_page_choices"
_MODEL_CHOICES = "_model_choices"
# The done_when sentences the human wrote on the approval page (3.0.0). There is no
# model-side counterpart: what "finished" means is the human's to change, and a relay
# field would be one more way for the model to decide it for them (D20).
_PAGE_CRITERIA = "_page_criteria"


# Why the breaker stopped an agent, worded for the human who has to decide what next.
def _halt_reason_text(reason: str, count: int, detail: str = "") -> str:
    if reason == REASON_REPEATED_CALL:
        return f"같은 호출이 {count}회 연속 반복되었습니다."
    if reason == REASON_ERROR_STREAK and detail == REPLAN_REDIRECTED:
        return f"이미 정해진 계획을 다시 세우려는 호출이 {count}회 연속 이어졌습니다."
    if reason == REASON_ERROR_STREAK:
        return f"같은 오류({detail})가 {count}회 연속 발생했습니다."
    if reason == REASON_NO_PROGRESS:
        return f"진척 없이 도구 호출이 {count}회 이어졌습니다."
    if reason == REASON_RESPAWN:
        return f"같은 목표를 표현만 바꾼 새 계획이 {count}개째 만들어졌습니다."
    if reason == REASON_THINKING_BUDGET:
        return f"생각 단계 {count}단계를 모두 썼지만 확정할 태스크 목록을 보내지 않았습니다."
    return "같은 단계가 반복되었습니다."


# Phrases that assert completion without evidencing it. Length alone cannot separate
# these from a genuinely terse but informative log - Korean packs a real sentence into
# ~12 characters - so the filter is content-based and only fires on an exact match.
_EMPTY_CLAIMS = {
    "done", "ok", "okay", "complete", "completed", "finished", "success", "successful",
    "task done", "task complete", "task completed", "work done", "all done", "yes",
    "완료", "성공", "작업완료", "완료함", "완료했습니다", "완료되었습니다", "성공적으로완료",
    "성공적으로완료했습니다", "작업을완료했습니다", "처리완료", "끝", "됐습니다", "했습니다",
}


def missing_evidence_reason(result_log: str, task_title: str, min_len: int) -> str | None:
    """Why this result_log fails as proof of work, or None if it is acceptable.

    The server cannot see whether work happened; the closest available check is whether
    the model wrote something that *could only* have been written after doing it.
    """
    text = (result_log or "").strip()
    if not text:
        return "result_log was empty"
    normalized = _normalize_evidence(text)
    if normalized in _EMPTY_CLAIMS:
        return "result_log only claims success without saying what was produced"
    if normalized == _normalize_evidence(task_title):
        return "result_log just repeats the task title instead of reporting an outcome"
    if len(normalized) < min_len:
        return f"result_log is too short to be a real outcome (min {min_len} characters)"
    return None


class PlanningHandlers:
    def __init__(self, store: Store, config: Config, approval_ui: ApprovalServer | None = None):
        self.store = store
        self.config = config
        if approval_ui is not None:
            self.approval_ui = approval_ui
        elif config.blocking_approval:
            self.approval_ui = ApprovalServer(
                ApprovalStore(config.state_dir),
                port=config.approval_port,
                open_browser=config.approval_open_browser,
            )
        else:
            self.approval_ui = None
        self.loop = LoopGuard(
            calls=config.breaker_calls,
            repeat=config.breaker_repeat,
            error_streak=config.breaker_error_streak,
            respawn=config.breaker_respawn,
            enabled=config.loop_breaker,
        )
        self._tls = threading.local()
        # When each plan last answered a call, for the gap_sec telemetry: the time the
        # model spent between our response and its next call is the closest the server
        # can get to measuring a thinking block it cannot see.
        self._last_answered: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def _ctx(self) -> _CallCtx:
        ctx = getattr(self._tls, "ctx", None)
        if ctx is None:
            ctx = _CallCtx()
            self._tls.ctx = ctx
        return ctx

    def note_client(self, client_info: Any, profile_note: bool = True) -> None:
        """Record which host connected (zed, goose, anythingllm, ...).

        Reproducing a field loop on the corporate model is impossible from here, so the
        audit log is the only evidence there will ever be - and it is useless unless each
        line says which agent host produced it.
        """
        info = client_info if isinstance(client_info, dict) else {}
        name = str(info.get("name") or "unknown")[:80]
        version = str(info.get("version") or "")[:40]
        self.store.audit_defaults["client"] = f"{name} {version}".strip()
        if profile_note:
            self.store.audit(
                "client_connected",
                client_name=name,
                client_version=version,
                model_profile=self.config.model_profile,
                thinking_budget=self.config.thinking_budget,
                loop_breaker=self.config.loop_breaker,
                approval_mode=self.config.approval_mode,
            )

    def dispatch(
        self,
        tool_name: str,
        raw_args: Any,
        progress_token: Any = None,
        notifier: Any = None,
        cancel_event: Any = None,
    ) -> dict[str, Any]:
        notes: list[str] = []
        self._tls.ctx = _CallCtx(progress_token, notifier, cancel_event)
        try:
            # normalize() lives inside the guard on purpose: were it to raise on some
            # unforeseen input it would otherwise escape as a raw JSON-RPC error, when a
            # weak model needs the graceful ok:false + next_action instead.
            clean, notes = normalize(tool_name, raw_args)
            # Serialized against other threads AND other server processes on the same
            # state directory. The blocking approval wait explicitly gives this up
            # (see _wait_on) so one pending approval cannot freeze every other
            # session for the length of the wait.
            with self.store.transaction():
                # A human may have clicked approve after the previous call timed out.
                # Collect that first so every handler below sees the true state.
                self._apply_late_decision()
                response = self._route(tool_name, clean, notes)
                if response is not None:
                    response = self._after_call(tool_name, clean, response, notes)
                    self._sync_runs()
                    return response
        except Exception as exc:  # noqa: BLE001 - nothing may escape to the model
            log.exception("Handler %s failed", tool_name)
            self.store.audit("internal_error", tool=tool_name, error=type(exc).__name__)
            plan = self._safe_active_plan()
            return error(
                plan,
                ErrorCode.INTERNAL_ERROR,
                type(exc).__name__,  # class name only - never a stack trace
                notes=notes,
            )
        return error(
            None,
            ErrorCode.INTERNAL_ERROR,
            f"Unknown tool '{tool_name}'.",
            notes=notes,
        )

    def _route(self, tool_name: str, clean: dict[str, Any], notes: list[str]) -> Any:
        if tool_name == "plan_and_think":
            return self._plan_and_think(clean, notes)
        if tool_name == "request_user_approval":
            ctx = self._ctx()
            return self._request_user_approval(
                clean,
                notes,
                progress_token=ctx.progress_token,
                notifier=ctx.notifier,
                cancel_event=ctx.cancel_event,
            )
        if tool_name == "update_task_progress":
            return self._update_task_progress(clean, notes)
        if tool_name == "get_current_plan":
            return self._get_current_plan(clean, notes)
        return None

    # ------------------------------------------------------------------
    # Circuit breaker (1.16.0, D25)
    # ------------------------------------------------------------------
    def _milestone(self, plan: Plan | None) -> None:
        """This plan just moved. Its loop counters start over.

        The human's guidance from a lifted halt has done its job once the plan moves on,
        so it is retired here too - the next hint should not keep quoting it.
        """
        if plan is None:
            return
        self._ctx().milestones.add(plan.plan_id)
        self.loop.milestone(plan.plan_id)
        plan.guidance = None

    def _request_fingerprint(self, plan: Plan) -> str:
        """The fingerprint of whatever request this plan has on the page right now.

        For a halt that is not just the plan: two halts of an unchanged plan are two
        different questions, and a decision on the first must not answer the second.
        """
        base = self._fingerprint(plan)
        if not plan.halt:
            return base
        payload = "\x00".join(
            [base, str(plan.halt.get("id", ""))]
            + plan.halt_draft()
            + [json.dumps([plan.draft_alternatives, plan.draft_reasons],
                          ensure_ascii=False, sort_keys=True)]
            # Only when the draft carries criteria, so a draft without them keeps the
            # fingerprint it had in 2.0.
            + ([json.dumps(plan.draft_done_when, ensure_ascii=False, sort_keys=True)]
               if plan.draft_done_when else [])
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _live_request(self, plan: Plan | None) -> dict[str, Any] | None:
        """The undecided request a human is looking at for this exact plan version."""
        if plan is None or self.approval_ui is None or self.config.autoapprove:
            return None
        try:
            return self.approval_ui.pending_request(
                plan.plan_id, self._request_fingerprint(plan)
            )
        except Exception:  # noqa: BLE001 - an unreadable queue must not fail a call
            log.exception("Could not read the approval queue")
            return None

    def _model_choices(
        self, args: dict[str, Any], notes: list[str]
    ) -> dict[str, int] | None:
        """Choices the model relays from a chat reply - honoured only with no page.

        With a page, the human picks there and the page is the only channel a choice may
        arrive by; one the model sends is a guess at what they want (D20's lesson,
        applied to choices).
        """
        relayed = args.get("choices")
        if not relayed:
            return None
        if self.approval_ui is not None:
            notes.append(
                "choices are made by the user on the approval page; the ones you sent were "
                "ignored."
            )
            return None
        return relayed

    @staticmethod
    def _choices_sentence(plan: Plan) -> str:
        """What the human picked, worded for the model - chosen options only."""
        parts = []
        for task in plan.tasks:
            picked = task.chosen_option()
            if picked is None:
                continue
            if task.chosen == 0:
                parts.append(f"task {task.task_id}: your recommendation")
            else:
                parts.append(f"task {task.task_id}: '{task.title}'")
        if not parts:
            return ""
        return (
            " The user chose how to do the tasks that offered a choice - "
            + "; ".join(parts)
            + ". Do each one exactly that way."
        )

    @staticmethod
    def _redirected(response: dict[str, Any]) -> dict[str, Any]:
        """Mark a re-plan the server turned away ("your plan is recorded", "already
        running", "already completed").

        Such a reply is ok:true - it is not the model's fault the first time - but each
        one after the server has said "do not plan again" is a loop in plain sight. The
        breaker counts the signal like an error code, so a run of them trips at the
        error-streak limit rather than at the much higher no-progress limit. Removed in
        _after_call before the model sees the response.
        """
        response[_LOOP_SIGNAL] = REPLAN_REDIRECTED
        return response

    def _after_call(
        self, tool: str, args: dict[str, Any], response: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        """Count this call against its plan and stop the plan if it is going in circles."""
        ctx = self._ctx()
        signal = response.pop(_LOOP_SIGNAL, None)
        pid = response.get("plan_id")
        if pid:
            self._last_answered[pid] = time.monotonic()
        if ctx.trip is not None:
            trip_pid, reason, count = ctx.trip
            ctx.trip = None
            halted = self._trip(trip_pid, reason, count, "", notes)
            if halted is not None:
                return halted
        if not self.config.loop_breaker:
            return response

        signature = call_signature(tool, args)
        code = response.get("error_code") or signal
        if not pid:
            # A loop that never reaches a plan - the same routing error over and over -
            # has nothing to pause. It still has to end.
            tripped = self.loop.observe(NO_PLAN, signature, code, counted=not ctx.waited)
            return response if tripped is None else self._stop_without_halt(
                NO_PLAN, tool, code, tripped, notes
            )

        state = self.store.load()
        plan = state.plans.get(pid)
        if plan is None or plan.halt:
            return response
        if plan.status in TERMINAL_PLAN_STATUSES:
            # A finished plan cannot be paused, but a model re-planning it over and over
            # (after ANSWER_USER) is still a loop and still has to end.
            tripped = self.loop.observe(pid, signature, code, counted=not ctx.waited)
            return response if tripped is None else self._stop_without_halt(
                pid, tool, code, tripped, notes
            )
        # Never counted: a call that waited on a human, one that moved the plan, and one
        # made while a human has a request for this plan open in front of them.
        counted = not (
            ctx.waited or pid in ctx.milestones or self._live_request(plan) is not None
        )
        tripped = self.loop.observe(pid, signature, code, counted=counted)
        if tripped is None:
            return response
        reason, count = tripped
        halted = self._trip(pid, reason, count, code or "", notes)
        return halted if halted is not None else response

    def _stop_without_halt(
        self, key: str, tool: str, code: str | None, tripped: tuple[str, int],
        notes: list[str],
    ) -> dict[str, Any]:
        """End a loop that has no live plan to pause: hand back to the user in chat."""
        self.loop.forget(key)
        reason, count = tripped
        self.store.audit(
            "loop_stopped_without_plan", plan_id=key or None, reason=reason,
            count=count, error_code=code, tool=tool,
        )
        return build(
            None,
            ok=False,
            error_code=ErrorCode.LOOP_HALTED,
            message="The same call kept repeating, so the server stopped this sequence.",
            notes=notes,
            display_to_user=(
                "에이전트가 같은 호출을 반복해 진행을 멈췄습니다. "
                f"({_halt_reason_text(reason, count, code or '')}) "
                "어떻게 진행할지 알려 주십시오."
            ),
        )

    def _trip(
        self, plan_id: str, reason: str, count: int, detail: str, notes: list[str]
    ) -> dict[str, Any] | None:
        """Stop a plan and hand the decision to a human. None = nothing to stop."""
        state = self.store.load()
        plan = state.plans.get(plan_id)
        if plan is None or plan.status in TERMINAL_PLAN_STATUSES:
            return None
        self.loop.forget(plan_id)
        plan.halt = {
            "id": uuid.uuid4().hex[:12],
            "reason": reason,
            "count": count,
            "detail": detail,
            "text": _halt_reason_text(reason, count, detail),
            "at": now_iso(),
            "asked": False,
            "channel": "page" if self.approval_ui is not None else "chat",
        }
        plan.touch()
        self.store.save(state)
        self.store.audit(
            "loop_halted",
            plan_id=plan.plan_id,
            reason=reason,
            count=count,
            detail=detail or None,
            plan_status=plan.plan_status,
            last_thought=plan.last_thought() or None,
        )
        log.warning("Loop breaker paused %s: %s (%s)", plan.plan_id, reason, count)
        return self._ask_halt(state, plan, notes)

    def _ask_halt(
        self, state: State, plan: Plan, notes: list[str], recorded: str = ""
    ) -> dict[str, Any]:
        """Put a halted plan in front of the human, and wait like an approval does."""
        halt = plan.halt or {}
        by_user = plan.paused_by_user()
        code = self._halt_code(plan)
        draft = plan.halt_draft()
        on_page = self.approval_ui is not None
        display = render_halt_for_user(
            plan, halt.get("text", ""), draft, on_page, by_user=by_user
        )
        approval_url = self.approval_ui.url if on_page else None
        if approval_url:
            display = f"{display}\n\n현재 페이지: {approval_url.rstrip('/')}"
        if not halt.get("asked"):
            halt["asked"] = True
            plan.halt = halt
            plan.touch()
            self.store.save(state)
        if not on_page:
            return build(
                plan,
                ok=False,
                error_code=code,
                message="This plan is paused. The user decides how it continues.",
                notes=notes,
                display_to_user=display,
            )
        summary = halt.get("text", "")
        if by_user:
            # Their own memo, not the agent's last thought: they stopped it, and what
            # they wrote while doing so is the context of the decision they now make.
            memo = str(halt.get("note") or "").strip()
            if memo:
                summary = f"{summary}\n남기신 메모: {memo}"
        else:
            thought = plan.last_thought().strip()
            if thought:
                summary = f"{summary}\n에이전트의 마지막 생각: {thought}"
        tasks = (
            self._draft_page_tasks(plan)
            if plan.status is PlanStatus.DRAFTING
            else plan.tasks_for_page()
        )
        fingerprint = self._request_fingerprint(plan)
        request_id = self.approval_ui.open_request(
            plan.plan_id, plan.goal, display, tasks, fingerprint, PHASE_HALT, summary,
            draft=bool(draft), **({"origin": "user"} if by_user else {}),
        )
        if request_id is None:
            return self._wait_unavailable(plan, notes, display)
        outcome = self._wait_on(request_id, plan, notes)
        return self._settle(
            plan.plan_id, fingerprint, outcome, notes, approval_url, display,
            refusal=code, recorded=recorded,
        )

    def _draft_options(self, plan: Plan) -> dict[int, list[dict[str, str]]]:
        options, _ = build_options(
            list(plan.draft_tasks),
            list(plan.draft_alternatives),
            {int(k): v for k, v in plan.draft_reasons.items() if str(k).isdigit()},
            self.config.max_alternatives,
            self.config.max_choice_points,
        )
        return options

    def _draft_criteria(self, plan: Plan) -> dict[int, str]:
        """The draft's done_when sentences, validated against the draft they number into."""
        if not self.config.done_when or not plan.draft_done_when:
            return {}
        items = {int(k): v for k, v in plan.draft_done_when.items() if str(k).isdigit()}
        criteria, _ = clean_done_when(
            list(plan.draft_tasks), items, self.config.max_done_when_chars
        )
        return criteria

    def _draft_page_tasks(self, plan: Plan) -> list[dict[str, Any]]:
        """The draft as approval-page rows, with the choices and criteria it carries."""
        options = self._draft_options(plan)
        topics = collect_topics(list(plan.draft_alternatives), options)
        criteria = self._draft_criteria(plan)
        rows = []
        for i, title in enumerate(plan.draft_tasks, start=1):
            row: dict[str, Any] = {
                "task_id": i, "title": title, "status": TaskStatus.PENDING.value
            }
            if i in options:
                row["options"] = options[i]
                if i in topics:
                    row["topic"] = topics[i]
            if i in criteria:
                row["done_when"] = criteria[i]
            rows.append(row)
        return rows

    def _attach_done_when(
        self, plan: Plan, done_when: dict[int, str], notes: list[str],
        only: set[int] | None = None,
    ) -> None:
        """Put the model's criteria on the tasks they describe (3.0.0).

        `only` limits it to the tasks a task_updates call rewrote. A criterion the human
        wrote is never replaced by the model's: what "finished" means for that task is
        theirs to change, on the page.
        """
        if not done_when:
            return
        locked = {t.task_id for t in plan.tasks if t.status == TaskStatus.DONE.value}
        criteria, check_notes = clean_done_when(
            [t.title for t in plan.tasks], done_when, self.config.max_done_when_chars, locked
        )
        notes.extend(check_notes)
        kept: list[int] = []
        for task in plan.tasks:
            if task.task_id not in criteria or (only is not None and task.task_id not in only):
                continue
            if task.done_when_by:
                kept.append(task.task_id)
                continue
            task.set_done_when(criteria[task.task_id])
        if kept:
            notes.append(
                "The user wrote the done_when for task(s) "
                + ", ".join(str(t) for t in kept)
                + " themselves; yours was not applied there."
            )

    def _apply_criteria(self, plan: Plan, criteria: dict[Any, str] | None) -> list[dict]:
        """Make what the human wrote on the approval page the task's criterion (3.0.0).

        An empty string removes the criterion. Finished tasks are left alone. Returns
        what changed, for the audit - which is the only place the model's own wording
        survives once the human has replaced it.
        """
        changed: list[dict] = []
        limit = self.config.max_done_when_chars
        for raw_id, raw in (criteria or {}).items():
            try:
                task = plan.get_task(int(raw_id))
            except (TypeError, ValueError):
                continue
            if task is None or task.status == TaskStatus.DONE.value:
                continue
            text = " ".join(str(raw or "").split())
            if limit > 0:
                text = text[:limit].rstrip()
            if text == (task.done_when or ""):
                continue
            changed.append(
                {"task_id": task.task_id, "from": task.done_when, "to": text or None}
            )
            task.set_done_when(text, by_user=True)
        if changed:
            self.store.audit("criteria_applied", plan_id=plan.plan_id, changed=changed)
        return changed

    @staticmethod
    def _criteria_sentence(changed: list[dict]) -> str:
        """Told to the model once, at approval; next_task repeats each at its turn."""
        ids = [str(c["task_id"]) for c in changed]
        if not ids:
            return ""
        return (
            f" The user set what finished means for task(s) {', '.join(ids)} - each "
            "task's done_when is in next_task when you reach it."
        )

    def _apply_choices(self, plan: Plan, choices: dict[Any, int] | None) -> list[dict]:
        """Make the human's pick the task (2.0.0). Returns what changed, for the audit.

        Every task that offered a choice gets a `chosen`, so the record says the human
        decided it - picking the recommendation is a decision too. Anything missing or
        out of range keeps the recommendation (index 0).
        """
        picks = {str(k): v for k, v in (choices or {}).items()}
        changed: list[dict] = []
        for task in plan.tasks:
            if not task.has_choice:
                continue
            decided = task.chosen is not None
            if decided and (
                str(task.task_id) not in picks or task.status == TaskStatus.DONE.value
            ):
                # Picked on an earlier approval of this same plan. The page offers a
                # choice only while it is undecided, so a re-approval - after a repair
                # (3.0.0) or an expired approval - arrives with no pick for it, and
                # "no pick" must not put the recommendation back over what the human
                # chose. Work already done the chosen way is never re-decided at all.
                continue
            index = picks.get(str(task.task_id), 0)
            if not isinstance(index, int) or not 0 <= index < len(task.options or []):
                index = 0
            task.chosen = index
            picked = (task.options or [])[index]["title"]
            if picked != task.title:
                changed.append({"task_id": task.task_id, "from": task.title, "to": picked})
                task.title = picked
        if plan.choice_points():
            self.store.audit(
                "choices_applied",
                plan_id=plan.plan_id,
                chosen={str(t.task_id): letter(t.chosen or 0) for t in plan.tasks
                        if t.has_choice},
                changed=changed or None,
            )
        return changed

    def _mutate_halt(
        self, plan: Plan, decision: str, comment: str | None,
        choices: dict[Any, int] | None = None,
    ) -> str:
        """Apply the human's answer to a halt. Returns what happened, for the audit."""
        draft = plan.halt_draft()
        by_user = plan.paused_by_user()
        memo = str((plan.halt or {}).get("note") or "").strip() if by_user else ""
        plan.halt = None
        self.loop.forget(plan.plan_id)
        self._ctx().milestones.add(plan.plan_id)
        if decision == Decision.REJECTED.value:
            self._mutate_rejected(plan, comment)
            return "cancelled"
        if decision == Decision.APPROVED.value and draft:
            # The human read this draft on the halt card and approved it as it stands.
            if plan.status is PlanStatus.DRAFTING:
                options = self._draft_options(plan)
                topics = collect_topics(list(plan.draft_alternatives), options)
                criteria = self._draft_criteria(plan)
                if plan.tasks:
                    plan.superseded_tasks.append([t.to_dict() for t in plan.tasks])
                plan.tasks = [
                    Task(task_id=i, title=title, options=options.get(i),
                         choice_topic=topics.get(i), done_when=criteria.get(i))
                    for i, title in enumerate(draft, start=1)
                ]
                plan.clear_draft()
                plan.pending_revision = None
                plan.set_status(PlanStatus.AWAITING_APPROVAL)
            plan.approval.requested_at = now_iso()
            self._mutate_approved(plan, comment, choices)
            return "draft_approved"
        # Continue - with a direction, if the human gave one. A plan they paused
        # themselves falls back to what they wrote when they stopped it.
        plan.guidance = (comment or "").strip() or memo or None
        if plan.status is PlanStatus.DRAFTING:
            plan.step_budget_end = 0  # a fresh thinking budget
        if by_user and plan.status in EXECUTABLE_PLAN_STATUSES:
            # The pause was applied in place of starting the next task; resuming is
            # that start, so the model is not sent to make a call the stop swallowed.
            self._auto_advance(plan)
        plan.touch()
        self._withdraw_approval_request(plan)
        return "resumed"

    def _resolve_halt(
        self, state: State, plan: Plan, decision: str, comment: str | None,
        notes: list[str], choices: dict[Any, int] | None = None,
    ) -> dict[str, Any]:
        action = self._mutate_halt(plan, decision, comment, choices)
        guidance = plan.guidance
        self.store.save(state)
        self.store.audit(
            "halt_resolved", plan_id=plan.plan_id, action=action, comment=comment or None
        )
        if action == "cancelled":
            message = "The user cancelled this plan. Nothing more will run."
        elif action == "draft_approved":
            message = (
                "The user approved your draft plan. Execution is unlocked."
                + self._choices_sentence(plan)
            )
        else:
            message = "The user let you continue." + (
                f' They said: "{guidance}". Follow that.' if guidance else ""
            )
        return build(
            plan,
            message=message,
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            progress=plan.progress() if plan.tasks else None,
        )

    # ------------------------------------------------------------------
    # The gate on the transition, and the run in between (3.1.0)
    # ------------------------------------------------------------------
    def _asks_itself(self) -> bool:
        """Does the server put a plan in front of the human without being asked to?

        Before 3.1 the model had to remember a separate request_user_approval call at
        the two moments a plan changes hands - after its final task list, and after the
        last task. Forgetting it, or answering the user instead, was the commonest way
        a weak model left the lifecycle. Both moments are state transitions the server
        already sees, so it asks at them itself.
        """
        return self.config.auto_ask and not self.config.autoapprove

    @staticmethod
    def _spoken(thought: str) -> str:
        """The model's own sentence about its plan: the overview on the approval page."""
        text = (thought or "").strip()
        return "" if text == "(no thought text provided)" else text

    @staticmethod
    def _halt_code(plan: Plan | None) -> ErrorCode:
        """Why a held plan refuses a call: the breaker stopped it, or the human did."""
        if plan is not None and plan.paused_by_user():
            return ErrorCode.PLAN_PAUSED
        return ErrorCode.LOOP_HALTED

    def _run_entry(self, plan: Plan) -> dict[str, Any]:
        """A running plan as the page shows it."""
        entry: dict[str, Any] = {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "tasks": plan.tasks_for_page(),
            # Changes exactly when what the card shows changes, so the page redraws
            # then and at no other time.
            "rev": self._fingerprint(plan),
            "server_version": SERVER_VERSION,
        }
        idle = plan.idle_seconds()
        if idle != float("inf"):
            # An approval that has gone cold no longer authorizes anything (see
            # _expire_stale_approval), so its card stops being shown at that moment -
            # without waiting for a call that may never come.
            entry["expires_at"] = int(time.time() - idle) + self.config.approval_ttl
        return entry

    def _sync_runs(self) -> None:
        """Make the run board show exactly the plans that are executing.

        One place, called at the end of every call and before every wait, rather than a
        publish at each of the dozen transitions that start, advance or end a run: a
        transition added later cannot forget the board, because none of them know it
        exists. Derived from the shared state file, so every process computes the same
        board.
        """
        if self.approval_ui is None:
            return
        try:
            wanted: dict[str, dict[str, Any]] = {}
            if self.config.run_control:
                for plan in self.store.load().plans.values():
                    if (
                        plan.status in EXECUTABLE_PLAN_STATUSES
                        and not plan.halt
                        and not plan.approval_is_stale(self.config.approval_ttl)
                    ):
                        wanted[plan.plan_id] = self._run_entry(plan)
            if not self.approval_ui.sync_runs(wanted):
                log.warning("The run board could not be saved; the page may lag behind")
        except Exception:  # noqa: BLE001 - a view must never fail a call
            log.debug("Could not refresh the run board", exc_info=True)

    def _run_control(self, plan: Plan) -> dict[str, Any] | None:
        """What the human has asked of this running plan on the page, if anything."""
        if self.approval_ui is None or not self.config.run_control:
            return None
        try:
            return self.approval_ui.run_control_pending(plan.plan_id)
        except Exception:  # noqa: BLE001 - an unreadable board must not fail a call
            log.debug("Could not read the run board", exc_info=True)
            return None

    def _consume_run_control(self, plan: Plan) -> None:
        """Take an applied control off the board. Only ever after the plan is saved: a
        control consumed before a write that then fails would be a stop the human asked
        for and nobody carried out."""
        try:
            self.approval_ui.take_run_control(plan.plan_id)
        except Exception:  # noqa: BLE001 - housekeeping; the sync drops it as well
            log.debug("Could not clear the run control for %s", plan.plan_id)

    @staticmethod
    def _control_words(control: dict[str, Any]) -> str:
        return str(control.get("comment") or "").strip()

    def _apply_run_control(
        self, state: State, plan: Plan, control: dict[str, Any], notes: list[str],
        reported: str = "",
    ) -> dict[str, Any]:
        """Carry out what the human asked of a running plan (3.1.0).

        Called where the agent reports a task, after that report is recorded - work that
        was done is never thrown away to honour a stop - and before the next task
        starts. `reported` says what this call did record, so a model answered with
        "paused" or "the user wants a change" is not left wondering about its DONE.
        """
        words = self._control_words(control)
        if control.get("action") == CONTROL_NOTE and words:
            return self._steer(state, plan, words, notes, reported)
        return self._pause(state, plan, words, notes, reported)

    def _pause(
        self, state: State, plan: Plan, words: str, notes: list[str], reported: str
    ) -> dict[str, Any]:
        """Hold a running plan because the human asked, and hand them the decision.

        The breaker's halt is reused whole - the card, the wait, the three answers - so
        a plan stopped by a person is held exactly as firmly as one stopped by the
        server. Only the reason differs, and with it every sentence shown to anyone.
        """
        current = plan.current_task()
        text = (
            f"사용자가 실행을 멈췄습니다. 태스크 {len(plan.tasks)}개 중 "
            f"{plan.done_count()}개가 끝났습니다."
        )
        if current is not None:
            text += (
                f" {current.task_id}번 태스크는 진행 중인 상태로 멈췄습니다."
                if current.status == TaskStatus.IN_PROGRESS.value
                else f" {current.task_id}번 태스크는 시작하지 않았습니다."
            )
        plan.halt = {
            "id": uuid.uuid4().hex[:12],
            "reason": HALT_USER_PAUSE,
            "count": 0,
            "detail": "",
            "text": text,
            # What they typed beside the button. Shown back on the pause card, and it
            # becomes the direction if they resume without typing another (D22).
            "note": words or None,
            "at": now_iso(),
            "asked": False,
            "channel": "page",
        }
        self.loop.forget(plan.plan_id)
        self._ctx().milestones.add(plan.plan_id)
        plan.touch()
        self.store.save(state)
        self._consume_run_control(plan)
        self.store.audit(
            "run_paused", plan_id=plan.plan_id, comment=words or None,
            progress=plan.progress(),
        )
        log.warning("The user paused %s from the approval page", plan.plan_id)
        return self._ask_halt(
            state, plan, notes,
            recorded=reported + "The user paused this plan before the next task.",
        )

    def _steer(
        self, state: State, plan: Plan, words: str, notes: list[str], reported: str
    ) -> dict[str, Any]:
        """Open the unfinished tasks for the change the human wrote on the run card.

        The local-repair machinery of 3.0, with the human rather than a failure as its
        origin: finished tasks are out of reach, the unfinished ones may be rewritten
        with task_updates, and the human approves the result before anything continues.
        Their words end up in the wording of the tasks - re-read, re-approved and
        restated at execution - instead of in a hint the model is trusted to remember.
        """
        unfinished = [t for t in plan.tasks if t.status != TaskStatus.DONE.value]
        kept = [t.task_id for t in plan.tasks if t.status == TaskStatus.DONE.value]
        plan.pending_revision = {
            "targets": {str(unfinished[0].task_id): words},
            "origin": ORIGIN_RUN,
            "open": [t.task_id for t in unfinished[1:]],
        }
        # The work already done survives whatever the model sends back - a whole
        # task_list as much as task_updates (see _carry_evidence).
        plan.rework_from_completion = True
        plan.approval.revision_count += 1
        plan.approval.user_comment = words
        plan.approval.reset_request()
        plan.step_budget_end = 0
        plan.clear_draft()
        plan.set_status(PlanStatus.DRAFTING)
        self._milestone(plan)
        self.store.save(state)
        self._consume_run_control(plan)
        self.store.audit(
            "run_note_applied", plan_id=plan.plan_id, user_comment=words,
            open=[t.task_id for t in unfinished], progress=plan.progress(),
        )
        return build(
            plan,
            message=(
                reported + "Before you continue, the user asked for a change to the "
                "tasks that are left. Finished tasks keep their results."
            ),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            user_comment=words,
            revision_count=plan.approval.revision_count,
            revision_scope=SCOPE_TASKS,
            tasks_unchanged=kept or None,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
        )

    # ---- decision application (shared by the blocking and late paths) -------
    @staticmethod
    def _fingerprint(plan: Plan) -> str:
        """Identifies the exact plan version shown to the human.

        Includes each task's status and evidence, so a completion report the human
        approved cannot be quietly rewritten afterwards - the decision would no longer
        match and is discarded.
        """
        payload = "\x00".join(
            [plan.goal]
            + [
                f"{t.task_id}|{t.title}|{t.status}|{(t.result_log or '')}"
                # Options join the payload only where a task has them (2.0.0), so a plan
                # with no choices keeps exactly its 1.16 fingerprint - a request left on
                # the page across a rolling upgrade still matches.
                + (f"|{json.dumps(t.options, ensure_ascii=False, sort_keys=True)}|{t.chosen}"
                   if t.has_choice else "")
                # The same rule for the contract (3.0.0): the criterion the human read,
                # and what the server said it found, are part of what they approved.
                # Path and state only - a file the human opens and saves while reading
                # the report changes its size and mtime, and must not void the request.
                + (f"|dw:{t.done_when}|{t.done_when_by or ''}" if t.done_when else "")
                + (
                    "|ck:" + json.dumps(
                        [[c.get("path"), c.get("state")] for c in t.checks]
                        + [["-", w] for w in t.claims_withdrawn],
                        ensure_ascii=False,
                    )
                    if t.checks or t.claims_withdrawn else ""
                )
                for t in plan.tasks
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _decision_is_the_models_own(self, plan: Plan | None) -> bool:
        """Would accepting a decision right now mean trusting the model's word for it?

        True exactly while the human is still being asked about this plan version. The
        approval store is the authority, not the plan record: the request lives there,
        it is shared across server processes, and it is withdrawn the instant a real
        decision lands.
        """
        if plan is None or self.approval_ui is None:
            return False
        if self.config.autoapprove:
            return False  # testing escape hatch only; server.py logs a loud warning
        try:
            return self.approval_ui.has_pending(
                plan.plan_id, self._request_fingerprint(plan)
            )
        except Exception:  # noqa: BLE001 - an unreadable queue must not block a decision
            log.exception("Could not read the approval queue; allowing the decision")
            return False

    def _withdraw_approval_request(self, plan: Plan) -> None:
        """Take a plan's request off the page once it has been settled."""
        if self.approval_ui is not None:
            try:
                self.approval_ui.drop_for_plan(plan.plan_id)
            except Exception:  # noqa: BLE001 - never fail a call over UI housekeeping
                log.debug("Could not withdraw the approval request for %s", plan.plan_id)

    def _mutate_approved(
        self, plan: Plan, comment: str | None, choices: dict[Any, int] | None = None,
        criteria: dict[Any, str] | None = None,
    ) -> list[dict]:
        """Returns the criteria the human changed while approving (3.0.0), if any."""
        plan.approval.decision = Decision.APPROVED.value
        plan.approval.decided_at = now_iso()
        if comment:
            plan.approval.user_comment = comment
        changed: list[dict] = []
        if plan.status is not PlanStatus.AWAITING_COMPLETION:
            self._apply_choices(plan, choices)
            # In the same transaction as the approval, like the choices: what the human
            # approved - including what they said "finished" means - is what runs.
            changed = self._apply_criteria(plan, criteria)
        # The "you flagged this one" markers exist to guide a re-read of a revised plan.
        # Once it is approved they are answered, and leaving them would decorate the
        # completion report with stale complaints.
        plan.clear_revision_marks()
        plan.pending_revision = None
        plan.rework_from_completion = False
        plan.clear_draft()
        if plan.status is PlanStatus.AWAITING_COMPLETION:
            plan.run_note = None  # shown with the report this approval answers
        self._milestone(plan)
        # Approving a completion report closes the plan; approving a draft unlocks it -
        # unless the draft is already finished work carried through a redraft, in which
        # case unlocking it would let the model answer without the completion check ever
        # being asked. Route that back to the verification gate instead.
        if plan.status is PlanStatus.AWAITING_COMPLETION:
            plan.set_status(PlanStatus.COMPLETED)
        elif plan.all_done() and self.config.completion_approval:
            plan.set_status(PlanStatus.AWAITING_COMPLETION)
        else:
            plan.set_status(PlanStatus.APPROVED)
        self._withdraw_approval_request(plan)
        return changed

    def _mutate_rejected(self, plan: Plan, comment: str | None) -> None:
        plan.approval.decision = Decision.REJECTED.value
        plan.approval.decided_at = now_iso()
        plan.approval.user_comment = comment or ""
        plan.halt = None
        self._milestone(plan)
        plan.set_status(PlanStatus.CANCELLED)
        self._withdraw_approval_request(plan)

    @staticmethod
    def _revision_targets(plan: Plan, task_comments: dict[str, str] | None) -> dict[str, str]:
        """Keep only comments that name a task this plan actually has.

        A comment on a task that no longer exists cannot be answered by rewriting it, and
        letting it through would leave a target the model can never satisfy.
        """
        targets: dict[str, str] = {}
        for raw_id, text in (task_comments or {}).items():
            try:
                task_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            body = str(text or "").strip()
            if body and plan.get_task(task_id) is not None:
                targets[str(task_id)] = body
        return targets

    def _mutate_no(
        self,
        plan: Plan,
        comment: str | None,
        task_comments: dict[str, str] | None = None,
        scope: str = SCOPE_PLAN,
        criteria: dict[Any, str] | None = None,
    ) -> tuple[str, Any]:
        """Route the human's "no" to the mutation that actually answers it.

        The same button carries two different objections depending on when it is pressed.
        Before execution it means "this plan is wrong". After the completion report it
        means "the plan was fine, what came out of these tasks is not" - and treating
        that as a re-plan is what used to throw away every task's evidence and order the
        model to redo work nobody complained about.

        Returns ("REWORK", [task_id, ...]) or ("REVISE", {task_id: comment}).
        """
        targets = (
            self._revision_targets(plan, task_comments) if scope == SCOPE_TASKS else {}
        )
        if plan.status is PlanStatus.AWAITING_COMPLETION and targets:
            reopened = self._mutate_rework(
                plan, comment, {int(k): v for k, v in targets.items()}
            )
            return "REWORK", reopened
        return "REVISE", self._mutate_revise(plan, comment, task_comments, scope, criteria)

    def _mutate_rework(
        self, plan: Plan, comment: str | None, targets: dict[int, str]
    ) -> list[int]:
        """Reopen specific finished tasks without disturbing the plan around them.

        The human is not disputing the task list here - they approved it and it has not
        changed - so it is not re-approved. Only the named tasks go back to PENDING;
        every other task keeps its DONE status and its evidence, which is the whole
        point. Ordering is untouched, so `can_start_task` and `unfinished_before` keep
        working unchanged: a reopened task is simply the next PENDING one.
        """
        plan.approval.revision_count += 1
        plan.approval.user_comment = comment or ""
        self._milestone(plan)
        # The completion report is withdrawn rather than answered: once the rework lands,
        # a fresh one has to be asked for and verified.
        plan.approval.reset_request()
        reopened: list[int] = []
        for task_id in sorted(targets):
            task = plan.get_task(task_id)
            if task is None:  # _revision_targets already filtered; belt and braces
                continue
            task.revision_note = targets[task_id]
            # What it produced last time, kept so the redo is not done blind. Marks on
            # other tasks are deliberately left alone: a note from an earlier round is
            # still the reason that task reads the way it does on the next report.
            task.previous_result_log = task.result_log or task.previous_result_log
            task.result_log = None
            task.status = TaskStatus.PENDING.value
            task.started_at = None
            task.finished_at = None
            # What the server found belonged to the outcome the human just sent back.
            # The criterion stays: it is what the redo is still measured against.
            task.clear_file_evidence()
            reopened.append(task_id)
        plan.pending_revision = None
        plan.rework_from_completion = False
        plan.run_note = None
        plan.set_status(PlanStatus.IN_EXECUTION)
        self._withdraw_approval_request(plan)
        return reopened

    def _mutate_revise(
        self,
        plan: Plan,
        comment: str | None,
        task_comments: dict[str, str] | None = None,
        scope: str = SCOPE_PLAN,
        criteria: dict[Any, str] | None = None,
    ) -> dict[str, str]:
        """Send a plan back for changes. Returns the per-task targets, if any.

        Two shapes of "no" share this path. A whole-plan revision is the original
        behaviour: everything is redrafted. A targeted one records exactly which tasks
        the human objected to, which is the only thing that later stops the model from
        quietly rewriting the tasks they already accepted.
        """
        # Read before the status changes. A redraft asked for from the completion report
        # must not delete the work that has already been done - see `_carry_evidence`.
        # Nor must one asked for while re-approving a change made mid-run (3.1.0): the
        # flag was raised when the human's note opened the plan, and it stays up for as
        # long as there is finished work to keep.
        plan.rework_from_completion = plan.status is PlanStatus.AWAITING_COMPLETION or (
            plan.rework_from_completion
            and any(t.status == TaskStatus.DONE.value for t in plan.tasks)
        )
        plan.run_note = None
        # Criteria the human typed before pressing REVISE are theirs whatever button
        # they then chose (3.0.0). A targeted revision keeps the tasks, so the criteria
        # are applied to them. A whole-plan one replaces every task, so there is
        # nothing to attach them to - they travel in the comment instead, where the
        # model reads them while redrafting. Either way nothing typed is dropped (D22).
        if criteria and not plan.rework_from_completion:
            if scope == SCOPE_TASKS and self._revision_targets(plan, task_comments):
                self._apply_criteria(plan, criteria)
            else:
                comment = self._criteria_as_comment(plan, comment, criteria)
        plan.approval.revision_count += 1
        plan.approval.user_comment = comment or ""
        plan.approval.reset_request()
        self._milestone(plan)
        # A new drafting round: a fresh thinking budget, and no stale draft from the round
        # the human just sent back.
        plan.step_budget_end = 0
        plan.clear_draft()
        # Markers from a previous round would otherwise read as complaints about this one.
        plan.clear_revision_marks()
        targets = (
            self._revision_targets(plan, task_comments) if scope == SCOPE_TASKS else {}
        )
        plan.pending_revision = {"targets": targets} if targets else None
        plan.set_status(PlanStatus.DRAFTING)
        self._withdraw_approval_request(plan)
        return targets

    @staticmethod
    def _criteria_as_comment(
        plan: Plan, comment: str | None, criteria: dict[Any, str]
    ) -> str:
        parts = [comment.strip()] if (comment or "").strip() else []
        for raw_id, raw in criteria.items():
            try:
                task = plan.get_task(int(raw_id))
            except (TypeError, ValueError):
                continue
            text = " ".join(str(raw or "").split())
            if task is not None and text:
                parts.append(f"[완료 기준] {task.task_id}번 '{task.title}': {text}")
        return " / ".join(parts)

    def _apply_late_decision(self) -> None:
        """Honour a decision the human made after the tool call had already returned.

        The approval page stays actionable past the request timeout so people can take
        their time. Whatever they clicked is collected here, on the next tool call of
        any kind, and applied to the plan - otherwise the button would appear to work
        while nothing actually happened.
        """
        if self.approval_ui is None:
            return
        state = self.store.load()
        # Any active plan may have a decision waiting - concurrent sessions each have
        # their own, so checking only "the active plan" would strand the others.
        for candidate in state.active_plans():
            taken = self.approval_ui.take_decision(
                candidate.plan_id, self._request_fingerprint(candidate)
            )
            if taken is not None:
                plan = candidate
                break
        else:
            return
        verdict: Verdict = taken
        if plan.halt:
            # The fingerprint matched the halt card, so this answers the halt.
            action = self._mutate_halt(
                plan, verdict.decision, verdict.comment, verdict.choices
            )
            self.store.save(state)
            self.store.audit(
                "late_decision_applied", plan_id=plan.plan_id, decision=verdict.decision,
                comment=verdict.comment, halt_action=action,
            )
            return
        if verdict.decision == Decision.APPROVED.value:
            self._mutate_approved(
                plan, verdict.comment, verdict.choices, verdict.criteria
            )
        elif verdict.decision == Decision.REJECTED.value:
            self._mutate_rejected(plan, verdict.comment)
        else:
            self._mutate_no(
                plan, verdict.comment, verdict.task_comments, verdict.scope,
                verdict.criteria,
            )
        self.store.save(state)
        self.store.audit(
            "late_decision_applied",
            plan_id=plan.plan_id,
            decision=verdict.decision,
            comment=verdict.comment,
            scope=verdict.scope,
            task_comments=verdict.task_comments or None,
        )
        log.warning("Applied a late human decision for %s: %s", plan.plan_id, verdict.decision)

    # ---- plan routing ---------------------------------------------------
    _AMBIGUOUS = object()

    @staticmethod
    def _resolve_plan(state: State, plan_id: Any) -> Any:
        """Which plan does this call mean?

        Explicit plan_id wins. Otherwise, if exactly one plan is in flight it is
        unambiguous and the model never has to know plan_id exists. Only genuinely
        concurrent sessions hit the ambiguous case, and the error tells the model
        exactly what to send.
        """
        if isinstance(plan_id, str) and plan_id.strip().lower() not in (
            "", "current", "active", "latest"
        ):
            return state.plans.get(plan_id.strip())
        actives = state.active_plans()
        if len(actives) == 1:
            return actives[0]
        if not actives:
            # Nothing live: fall back to the most recent plan so the model is told
            # "that plan was cancelled/completed" instead of "no plan exists", which
            # would invite it to quietly start over.
            if state.plans:
                return max(state.plans.values(), key=lambda p: p.updated_at)
            return None
        return PlanningHandlers._AMBIGUOUS

    @staticmethod
    def _plan_directory(state: State) -> list[dict[str, Any]]:
        return [
            {"plan_id": p.plan_id, "goal": p.goal, "plan_status": p.plan_status,
             "progress": p.progress()}
            for p in state.active_plans()
        ]

    def _ambiguous(self, state: State, notes: list[str]) -> dict[str, Any]:
        return error(
            None,
            ErrorCode.PLAN_AMBIGUOUS,
            f"{len(state.active_plans())} plans are active; say which one with plan_id.",
            notes=notes,
            active_plans=self._plan_directory(state),
        )

    # ---- the active-plan limit (3.0.0) ------------------------------------
    def _evict_for_room(self, state: State, incoming_goal: str) -> list[str]:
        """Close least-recently-used unfinished plans until a new one fits.

        Before 3.0 a full table refused every new plan until a human rejected an old
        one on the approval page - and an abandoned conversation never comes back to do
        that. With the limit at 20 the table fills with exactly such plans, so the
        default is now to drop the one touched longest ago.

        "Used" is the plan's last write (`updated_at`): an agent waiting on approval
        touches its plan every wait slice and an executing one at every task, so a plan
        idle past `evict_min_idle` has nobody on it. Anything fresher is never evicted -
        if every active plan is in use, the new one is refused as before. That floor is
        also what stops a model that keeps opening plans from emptying the table: its
        own fresh plans fill the slots and cannot be evicted.

        Nothing is lost silently. The audit line carries each evicted plan's tasks and
        evidence, and the state file remembers the id (`State.evicted`), so a
        conversation that comes back is told what happened instead of being pointed at
        somebody else's plan.
        """
        if not self.config.evict_lru:
            return []
        limit = max(1, self.config.max_active_plans)
        closed: list[tuple[Plan, int | None]] = []
        # Most idle first; an unreadable timestamp counts as the most idle of all.
        for plan in sorted(state.active_plans(), key=lambda p: (-p.idle_seconds(), p.plan_id)):
            if len(state.active_plans()) < limit:
                break
            idle = plan.idle_seconds()
            if idle < self.config.evict_min_idle:
                break  # every plan after this one is fresher still
            idle_field = None if idle == float("inf") else int(idle)
            state.evict(plan, idle_field, now_iso())
            closed.append((plan, idle_field))
        if not closed:
            return []
        # State first: a duplicate audit line is harmless, a lost state write is not.
        self.store.save(state)
        for plan, idle_field in closed:
            self._withdraw_approval_request(plan)
            self.loop.forget(plan.plan_id)
            self._last_answered.pop(plan.plan_id, None)
            self.store.audit(
                "plan_evicted",
                plan_id=plan.plan_id,
                goal=plan.goal,
                plan_status=plan.plan_status,
                idle_seconds=idle_field,
                progress=plan.progress() if plan.tasks else None,
                # The evidence the plan held, so removing it from the state file does
                # not remove the only record of work that was done.
                tasks=plan.tasks_brief() or None,
                halted=bool(plan.halt) or None,
                limit=limit,
                made_room_for=incoming_goal or None,
            )
            log.warning(
                "Evicted idle plan %s (%s, idle %s) to make room: %s active plans allowed",
                plan.plan_id, plan.plan_status,
                f"{idle_field}s" if idle_field is not None else "unreadable timestamp", limit,
            )
        return [plan.plan_id for plan, _ in closed]

    @staticmethod
    def _explicit_plan_id(plan_id: Any) -> str | None:
        if isinstance(plan_id, str) and plan_id.strip().lower() not in (
            "", "current", "active", "latest"
        ):
            return plan_id.strip()
        return None

    def _evicted(self, state: State, plan_id: Any, notes: list[str]) -> dict[str, Any] | None:
        """The answer for a call naming a plan the server closed to make room, else None.

        Without this an evicted id is just an unknown id, and the answers for those were
        written for a mistyped one: "there is no active plan, start one", or - from
        get_current_plan - "do not start a new plan, use one of these", followed by other
        conversations' plans. Neither is true of a plan that existed and was closed.
        """
        pid = self._explicit_plan_id(plan_id)
        if pid is None or pid in state.plans:
            return None
        record = state.evicted.get(pid)
        if record is None:
            return None
        self.store.audit("evicted_plan_requested", plan_id=pid)
        return build(
            None,
            ok=False,
            error_code=ErrorCode.PLAN_EVICTED,
            message=(
                f"The plan '{record.get('goal')}' no longer exists. It had been idle, and "
                f"the limit of {self.config.max_active_plans} active plans was reached, so "
                "the server closed it to make room."
            ),
            notes=notes,
            evicted_plan={
                "plan_id": pid, **{k: v for k, v in record.items() if v is not None}
            },
        )

    def _expire_stale_approval(self, state: State, plan: Plan | None) -> bool:
        """Revoke an approval that has gone cold, before anything acts on it.

        Without this, a plan approved hours ago in a different conversation keeps
        authorizing execution: plan_and_think redirects to it, request_user_approval
        short-circuits with "already approved" (so nothing ever blocks and no approval
        page appears), and update_task_progress sails through. Observed in the field.
        """
        if plan is None or not plan.approval_is_stale(self.config.approval_ttl):
            return False
        idle = plan.idle_seconds()
        # An unreadable timestamp yields infinity, which int() cannot represent.
        idle_field = None if idle == float("inf") else int(idle)
        plan.set_status(PlanStatus.AWAITING_APPROVAL)
        plan.approval.reset_request()  # forces a fresh ASK_USER, not a replayed decision
        self.store.save(state)
        self.store.audit(
            "approval_expired", plan_id=plan.plan_id, idle_seconds=idle_field,
            ttl=self.config.approval_ttl,
        )
        log.warning(
            "Approval for %s expired (%s idle) - re-approval required",
            plan.plan_id,
            f"{idle_field}s" if idle_field is not None else "unreadable timestamp",
        )
        return True

    def _safe_active_plan(self) -> Plan | None:
        try:
            return self.store.load().active_plan
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 1. plan_and_think
    # ------------------------------------------------------------------
    def _plan_and_think(self, args: dict[str, Any], notes: list[str]) -> dict[str, Any]:
        state = self.store.load()
        goal = (args.get("goal") or "").strip()
        revised_goal = (args.get("revised_goal") or "").strip()
        thought = (args.get("thought") or "").strip()
        step_number = args.get("step_number")
        continuing = isinstance(step_number, int) and step_number > 1

        # Routing, in priority order:
        #   1. explicit plan_id wins;
        #   2. exact (normalized) goal match - the model repeats its goal each step;
        #   3. the corrected goal, for a model that put it in both fields;
        #   4. a goal this plan has since revised away from;
        #   5. no match while CONTINUING (step > 1) - do not fork a plan on drifted goal;
        #      hand the model the list of active goals so it picks the exact one;
        #   6. no match while STARTING (step 1) - a genuinely new plan.
        plan = None
        pid_arg = args.get("plan_id")
        if isinstance(pid_arg, str) and pid_arg.strip().lower() not in (
            "", "current", "active", "latest"
        ):
            plan = state.plans.get(pid_arg.strip())
        if plan is None:
            plan = state.plan_for_goal(goal)
        if plan is None and revised_goal:
            plan = state.plan_for_goal(revised_goal)
        if plan is None and goal:
            # One turn of grace after a correction: the model is still echoing the
            # wording it has used all conversation. Continue the plan, do not fork it.
            plan = state.plan_for_former_goal(goal)
            if plan is not None:
                notes.append(
                    f"'{goal}' is this plan's previous goal; it was corrected to "
                    f"'{plan.goal}'. Send the corrected text from now on."
                )
        if plan is None and revised_goal and len(state.active_plans()) == 1:
            # A correction with the corrected text in BOTH fields matches nothing by
            # name. But asking to correct a goal presupposes a plan that has one, and
            # with exactly one in flight there is no other plan it could mean. With
            # several active this stays unresolved and the model is asked to pick.
            plan = state.active_plans()[0]
            notes.append(
                "'goal' matched no plan, so 'revised_goal' was applied to the only "
                "active plan. Send the goal already on record next time."
            )
        if plan is None and not goal:
            resolved = self._resolve_plan(state, pid_arg)
            plan = None if resolved is self._AMBIGUOUS else resolved
        if plan is not None and plan.status in TERMINAL_PLAN_STATUSES:
            plan = None  # a finished plan is never resurrected; this starts a new one

        # The same goal a human closed as COMPLETED moments ago. A model told "write the
        # final answer" that plans instead is not starting new work: it is obeying a
        # "plan before answering anything" rule into a second lap of the same plan, and
        # each lap ends in exactly the same place (D25). Hand it the finished results.
        if plan is None and goal and not revised_goal:
            finished = state.recently_completed(goal, self.config.replan_cooldown)
            if finished is not None:
                self.store.audit(
                    "replan_after_completion_suppressed",
                    plan_id=finished.plan_id, goal=goal,
                    seconds_since=int(finished.idle_seconds()),
                )
                return self._redirected(build(
                    finished,
                    message=(
                        "This exact goal was completed a moment ago and the user "
                        "confirmed it. Do not plan it again: answer the user now with the "
                        "results below. If the user really asked for it to be done again, "
                        "start a new plan whose goal says so."
                    ),
                    notes=notes,
                    tasks=finished.tasks_brief(),
                    progress=finished.progress(),
                ))

        # Continuing a plan the server closed to make room (3.0.0). Not drift: the plan
        # is gone, and the model must be told that rather than shown the other plans.
        if plan is None and continuing:
            gone = self._evicted(state, pid_arg, notes)
            if gone is not None:
                return gone

        # The requested feature: no exact goal match, but the model believes it is
        # continuing. Rather than silently create a second plan (goal drift = fork),
        # return the active goals and let the model pick the exact one.
        if plan is None and goal and continuing and state.active_plans():
            self.store.audit("goal_not_matched", goal=goal, step_number=step_number)
            return error(
                None,
                ErrorCode.GOAL_NOT_MATCHED,
                f"No active plan matches the goal '{goal}'.",
                notes=notes,
                active_plans=self._plan_directory(state),
            )

        if plan is not None and plan.halt:
            return self._halted(state, plan, notes, agent_note=thought)

        # A human is looking at this plan right now - approving it, checking its
        # completion report, or deciding a halt. Before 1.16 a call here quietly put the
        # plan back to DRAFTING and withdrew the request (D25): a thinking model that
        # "reconsidered" while waiting pulled the plan out from under the human, then
        # re-submitted it, round after round. Now the call simply waits on the human
        # like the approval call does, and what the model wanted to reconsider is shown
        # on the card instead of acted on.
        if plan is not None:
            held = self._hold_for_human(state, plan, notes, agent_note=thought)
            if held is not None:
                return held

        if self._expire_stale_approval(state, plan):
            notes.append(
                "This plan's approval had expired and was revoked. It no longer "
                "authorizes any execution."
            )

        # --- goal revision --------------------------------------------------
        # The user said the goal itself was wrong. Refusing the edit would leave the
        # server describing the plan by a goal the user has already disowned while it
        # executes tasks written for the corrected one - a state nobody can act on. So
        # the goal moves; `original_goal` and `goal_history` carry the audit trail that
        # freezing it used to provide.
        #
        # Deliberately AFTER the staleness check: revising touches the plan, and a touch
        # before the check would let a wording fix silently un-expire an approval that
        # had already sat idle past its TTL.
        goal_was_revised = False
        if plan is not None and revised_goal and goal_key(revised_goal) != goal_key(plan.goal):
            previous_goal = plan.goal
            goal_was_revised = plan.revise_goal(revised_goal)
            if goal_was_revised:
                self.store.save(state)
                self.store.audit(
                    "goal_revised",
                    plan_id=plan.plan_id,
                    previous_goal=previous_goal,
                    goal=plan.goal,
                    original_goal=plan.original_goal,
                    plan_status=plan.plan_status,
                )
                notes.append(
                    f"The goal was corrected to '{plan.goal}'. The previous wording is "
                    "kept in this plan's history. Send the corrected text as 'goal' from "
                    "now on."
                )

        if plan is not None and plan.status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
            self.store.audit("plan_and_think_redirected", plan_id=plan.plan_id)
            if goal_was_revised:
                # The approval on file was given for the OLD goal. Recording the
                # correction must not quietly widen what the human agreed to.
                notes.append(
                    "The user approved this plan under the previous goal. Check whether "
                    "the remaining tasks still serve the corrected goal - if they do "
                    "not, tell the user before you continue."
                )
            return self._redirected(build(
                plan,
                message=(
                    "This plan is already approved and running. Continue it instead of "
                    "planning again. Use get_current_plan if you need the details."
                ),
                notes=notes,
                goal=plan.goal if goal_was_revised else None,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
                next_task=plan.next_task_brief(),
            ))

        # A finished task list is not reopened by the model's own second thoughts. Only
        # the human's "request changes" sends a plan back to DRAFTING; a user correcting
        # the goal itself (revised_goal) is the one exception, since that changes what
        # the tasks are for. Before 1.16 these calls reset the plan to DRAFTING - the
        # finalize -> reconsider -> finalize lap a thinking model could run forever, and,
        # after the last task, the lap that deleted every result_log (D25/D26).
        if (
            plan is not None
            and plan.status is PlanStatus.AWAITING_APPROVAL
            and not goal_was_revised
        ):
            asked = bool(plan.approval.requested_at and not plan.approval.decision)
            self.store.audit(
                "replan_redirected", plan_id=plan.plan_id, plan_status=plan.plan_status,
                asked=asked, reconsider=reconsider_markers(thought) or None,
            )
            return self._redirected(build(
                plan,
                message=(
                    "The user has already been shown this plan and has not answered yet. "
                    "Do not plan again - wait for their reply."
                    if asked
                    else "Your plan is recorded and complete. It does not need more "
                    "checking: the user reviews it before anything runs."
                ),
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
            ))
        if plan is not None and plan.status is PlanStatus.AWAITING_COMPLETION:
            asked = bool(plan.approval.requested_at and not plan.approval.decision)
            self.store.audit(
                "replan_redirected", plan_id=plan.plan_id, plan_status=plan.plan_status,
                asked=asked, reconsider=reconsider_markers(thought) or None,
            )
            return self._redirected(build(
                plan,
                message=(
                    "Every task in this plan is already DONE and the user has been shown "
                    "the completion report. Do not plan again - wait for their reply."
                    if asked
                    else "Every task in this plan is already DONE. Do not plan again: "
                    "report completion so the user can check the results."
                ),
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                progress=plan.progress(),
            ))

        if plan is None and len(state.active_plans()) >= self.config.max_active_plans:
            # 3.0.0: make room rather than refuse. The slots are nearly always held by
            # plans a conversation walked away from - nothing else ever removes those.
            self._evict_for_room(state, goal or revised_goal)
        if plan is None and len(state.active_plans()) >= self.config.max_active_plans:
            # Still full: eviction is off, or every active plan is in use right now.
            return error(
                None,
                ErrorCode.PLAN_AMBIGUOUS,
                f"{len(state.active_plans())} plans are already active "
                f"(limit {self.config.max_active_plans}).",
                notes=notes,
                active_plans=self._plan_directory(state),
                message_hint=(
                    "Finish or cancel one of the active plans before starting another."
                ),
            )

        # Lenient defaults: erroring on a missing scalar costs a turn and teaches nothing.
        if plan is not None:
            if not goal:
                goal = plan.goal
                notes.append("No goal was sent; reused the goal already on record.")
        elif revised_goal:
            # A correction with nothing to correct: there is no plan yet, so the
            # corrected text is simply the goal of the one about to be created.
            goal = revised_goal
            notes.append(
                "There was no existing plan to correct, so 'revised_goal' was used as "
                "the goal of this new plan."
            )
        if not goal:
            goal = thought[:120] or "(goal not stated)"
            notes.append("No goal was sent; derived one from your thought. Send 'goal' next time.")
        if not thought:
            thought = "(no thought text provided)"
            # The reasoning profile makes `thought` optional on purpose: that model has
            # already reasoned, and nagging it to write its reasoning out again is the
            # double thinking the profile exists to remove.
            if not self.config.reasoning_profile:
                notes.append(
                    "No thought text was sent. Send one short reasoning sentence per step."
                )

        # Start or reuse the plan.
        if plan is None:
            if self._explicit_plan_id(pid_arg) in state.evicted:
                notes.append(
                    "The plan_id you sent was closed by the server after sitting idle "
                    "(the limit on active plans was reached). This call started a new "
                    "plan - use the plan_id in this response from now on."
                )
            plan_id = self.store.next_plan_id(state)
            plan = Plan(plan_id=plan_id, goal=goal, plan_status=PlanStatus.DRAFTING.value)
            state.plans[plan_id] = plan
            state.active_plan_id = plan_id
            self.store.audit("plan_created", plan_id=plan_id, goal=goal)
            # A model that reconsiders its goal and starts over at step 1 with new
            # wording forks a fresh plan each time - a loop spread across plan_ids, where
            # no per-plan counter can see it. Judged from the shared state, not from this
            # process, so separate conversations with unrelated goals never add up.
            family = state.drafting_family(plan, within_seconds=600)
            if self.loop.respawn_tripped(family):
                self._ctx().trip = (plan_id, REASON_RESPAWN, family)
        else:
            if plan.status is PlanStatus.BLOCKED:
                self.store.audit("replan_after_failure", plan_id=plan.plan_id)
            if plan.status is not PlanStatus.DRAFTING:
                # Re-entering DRAFTING opens a new round with a fresh thinking budget.
                plan.step_budget_end = 0
                plan.clear_draft()
            plan.set_status(PlanStatus.DRAFTING)
            plan.approval.reset_request()
            state.active_plan_id = plan.plan_id

        # --- revises_step -------------------------------------------------
        revises_step = args.get("revises_step")
        if revises_step is not None:
            target = next(
                (s for s in plan.thinking_steps if s.step_number == revises_step and not s.superseded),
                None,
            )
            if target is None:
                return error(
                    plan,
                    ErrorCode.INVALID_STEP,
                    f"There is no active thinking step numbered {revises_step}.",
                    notes=notes,
                )
            target.superseded = True
            notes.append(f"Step {revises_step} was marked superseded by this revision.")

        # --- step numbering ------------------------------------------------
        step_number = args.get("step_number")
        expected = plan.last_step_number() + 1
        if step_number is None or step_number != expected:
            if step_number is not None:
                notes.append(f"step_number {step_number} was corrected to {expected}.")
            step_number = expected

        total_steps = args.get("total_steps") or 0
        total_steps = max(int(total_steps), step_number)
        plan.total_steps = total_steps

        plan.thinking_steps.append(
            ThinkingStep(step_number=step_number, thought=thought, revises_step=revises_step)
        )
        last = self._last_answered.get(plan.plan_id)
        self.store.audit(
            "thinking_step",
            plan_id=plan.plan_id,
            step_number=step_number,
            revises_step=revises_step,
            thought=thought,
            # Field evidence for D25, which cannot be reproduced from here: how long the
            # model was away (a proxy for a thinking block the server cannot see) and how
            # often it talks itself back into reconsidering.
            gap_sec=round(time.monotonic() - last, 1) if last is not None else None,
            reconsider=reconsider_markers(thought),
            thought_chars=len(thought),
            profile=self.config.model_profile,
        )

        need_more = args.get("need_more_thinking")
        if need_more is None:
            if self.config.reasoning_profile and (
                args.get("task_list") or args.get("task_updates")
            ):
                # The reasoning profile records a plan in one call. A task list with no
                # verdict on need_more_thinking is that one call.
                need_more = False
            else:
                need_more = True
                notes.append("need_more_thinking was missing; assumed true (still planning).")

        # --- alternatives (2.0.0) ---------------------------------------------
        alternatives: list[dict[str, Any]] = list(args.get("alternatives") or [])
        reasons: dict[int, str] = dict(args.get("recommended_reasons") or {})
        if (alternatives or reasons) and not self.config.alternatives:
            notes.append(
                "Alternatives are turned off on this server; 'alternatives' and "
                "'recommended_reasons' were ignored. Send only task_list."
            )
            alternatives, reasons = [], {}

        # --- done_when (3.0.0) ------------------------------------------------
        done_when: dict[int, str] = dict(args.get("done_when") or {})
        if done_when and not self.config.done_when:
            notes.append(
                "done_when is turned off on this server; it was ignored. Send only "
                "task_list."
            )
            done_when = {}

        # --- thinking budget --------------------------------------------------
        # The round's budget is fixed on its first step. A model whose habit is one
        # more check (D25) is not refused when it runs out - a refusal is one more thing
        # for it to reconsider. The server instead does what the model would not: it
        # takes the latest task list as final and hands it to the human, whose review
        # is the verification the model kept trying to do on its own.
        budget = self.config.thinking_budget
        if budget > 0 and plan.step_budget_end <= 0:
            plan.step_budget_end = (step_number - 1) + budget
        if need_more and args.get("task_list"):
            # The alternatives travel with the draft they number into: a new draft
            # replaces them, so a choice can never point at a task that has moved.
            plan.draft_tasks = list(args["task_list"])[: self.config.max_tasks]
            plan.draft_alternatives = alternatives
            plan.draft_reasons = {str(k): v for k, v in reasons.items()}
            plan.draft_done_when = {str(k): v for k, v in done_when.items()}
        elif need_more and (alternatives or reasons) and plan.draft_tasks:
            plan.draft_alternatives = alternatives
            plan.draft_reasons = {str(k): v for k, v in reasons.items()}
        if need_more and done_when and plan.draft_tasks and not args.get("task_list"):
            # Criteria sent on their own join the draft they number into, like the
            # alternatives above.
            plan.draft_done_when = {str(k): v for k, v in done_when.items()}
        auto_submitted = False
        if need_more and budget > 0 and step_number >= plan.step_budget_end:
            if plan.draft_tasks:
                need_more = False
                auto_submitted = True
                notes.append(
                    f"You have used all {budget} thinking steps, so your latest task_list "
                    "is now the plan. It does not need more checking - the user reviews "
                    "it before anything runs."
                )
                self.store.audit(
                    "auto_finalized", plan_id=plan.plan_id, steps=step_number,
                    budget=budget, tasks=len(plan.draft_tasks),
                )
            else:
                # Nothing to submit. Record the step, and let the breaker hand this to
                # a human (see _after_call).
                self._ctx().trip = (plan.plan_id, REASON_THINKING_BUDGET, budget)

        # --- still thinking --------------------------------------------------
        if need_more:
            plan.touch()
            self.store.save(state)
            return build(
                plan,
                recorded_step=step_number,
                total_steps=plan.total_steps,
                thinking_steps_left=plan.thinking_steps_left(),
                draft_saved=len(plan.draft_tasks) or None,
                tasks=plan.tasks_brief() or None,
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                goal=plan.goal if goal_was_revised else None,
                message=f"Thinking step {step_number} recorded.",
            )

        # --- finalizing -------------------------------------------------------
        task_list = args.get("task_list") or []
        task_updates = args.get("task_updates") or []
        targets = plan.revision_targets()
        if not task_list and not task_updates and plan.draft_tasks:
            # Called final without the list but with one already on record: that list
            # is the answer. Refusing (MISSING_TASK_LIST) would send a model that has
            # finally stopped back into the loop it just left.
            task_list = list(plan.draft_tasks)
            if not alternatives and not reasons:
                alternatives = list(plan.draft_alternatives)
                reasons = {int(k): v for k, v in plan.draft_reasons.items()}
            if not done_when and self.config.done_when:
                done_when = {
                    int(k): v for k, v in plan.draft_done_when.items() if str(k).isdigit()
                }
            if not auto_submitted:
                notes.append(
                    "No task_list was sent, so your latest draft task_list was used."
                )
        if task_updates and (alternatives or reasons) and not task_list:
            notes.append(
                "alternatives can only be offered with a full task_list; they were ignored "
                "for this task_updates call."
            )
            alternatives, reasons = [], {}

        # A model told to rewrite one flagged task frequently sends only the new wording -
        # task_updates=["the rewritten task"] - because that is what it was asked for.
        # With exactly one flagged task there is exactly one task it can mean, so pairing
        # them is a reading, not a guess. With more than one it WOULD be a guess, and the
        # model is sent back to try again rather than have the wrong task rewritten.
        unmatched = args.get(UNMATCHED_TITLES_KEY) or []
        if not task_updates and len(targets) == 1 and len(unmatched) == 1:
            only = next(iter(targets))
            task_updates = [{"task_id": only, "title": unmatched[0]}]
            why = (
                f"Task {only} is the one that failed"
                if plan.repairing()
                else f"The user commented on task {only} only"
            )
            notes.append(
                f"task_updates arrived with no task_id. {why}, so it was applied there. "
                f'Next time send [{{"task_id": {only}, "title": "..."}}].'
            )
        elif unmatched and targets:
            notes.append(
                f"{len(unmatched)} entry/entries in task_updates had no task_id and were "
                f"ignored: the user commented on {len(targets)} tasks, so there is no way "
                'to tell which one you meant. Send [{"task_id": <number>, "title": "..."}].'
            )

        if task_updates and not targets:
            # Without a pending per-task request this would be the model editing an
            # arbitrary task on its own authority - the plan the human saw would change
            # underneath them. Refuse; a genuine re-plan goes through task_list.
            plan.touch()
            self.store.save(state)
            self.store.audit("unrequested_task_updates", plan_id=plan.plan_id)
            return error(
                plan,
                ErrorCode.REVISION_NOT_REQUESTED,
                "task_updates was sent but the user has not asked for changes to any "
                "specific task.",
                notes=notes,
                recorded_step=step_number,
                tasks=plan.tasks_brief(),
            )

        if targets and task_updates:
            if task_list:
                notes.append(
                    "Both task_updates and task_list were sent. task_list was ignored: "
                    "the user only asked for changes to specific tasks."
                )
            return self._apply_task_updates(
                state, plan, task_updates, targets, notes, step_number, done_when,
                thought=thought,
            )

        if not task_list:
            plan.touch()
            self.store.save(state)  # the thought is kept; only the finalization is refused
            if targets:
                return error(
                    plan,
                    ErrorCode.REVISION_INCOMPLETE,
                    "The user asked for changes to specific tasks, but neither "
                    "task_updates nor task_list was sent.",
                    notes=notes,
                    recorded_step=step_number,
                    tasks=plan.tasks_brief(),
                )
            return error(
                plan,
                ErrorCode.MISSING_TASK_LIST,
                "need_more_thinking was false but task_list was empty or missing.",
                notes=notes,
                recorded_step=step_number,
            )

        if targets and plan.repairing():
            # A task failed and the model re-planned everything instead of repairing
            # that one task. Still allowed - sometimes the whole approach was wrong -
            # but it is the pre-3.0 behaviour with its cost: finished work is not
            # carried over. Said out loud and recorded, so the cost is visible.
            notes.append(
                f"Only task {', '.join(str(t) for t in sorted(targets))} failed, but you "
                "replaced the whole task list. It was accepted; tasks that were already "
                "finished must be done again and the user must re-approve every task. "
                "Next time send task_updates for the failed task."
            )
            self.store.audit(
                "repair_ignored", plan_id=plan.plan_id, targets=sorted(targets)
            )
            plan.pending_revision = None
        elif targets and plan.steering():
            # The human wrote to a running plan and the model answered with a whole new
            # task list. Accepted; what was already done is carried onto the tasks that
            # survive unchanged (rework_from_completion, raised by _steer).
            notes.append(
                "You replaced the whole task list. It was accepted: a finished task whose "
                "wording did not change keeps its result, and the user re-approves the "
                "plan. Next time send task_updates for the unfinished tasks only."
            )
            self.store.audit("steer_replanned", plan_id=plan.plan_id)
            plan.pending_revision = None
        elif targets:
            # The model was asked to change two lines and rewrote the whole plan. That is
            # wasteful rather than unsafe - the human still re-approves every task - so it
            # is accepted, told, and recorded, which is what makes the waste measurable.
            notes.append(
                "The user only asked for changes to task(s) "
                f"{', '.join(str(t) for t in sorted(targets))}, but you replaced the whole "
                "task list. It was accepted; the user must now re-approve every task. "
                "Next time send task_updates."
            )
            self.store.audit(
                "targeted_revision_ignored",
                plan_id=plan.plan_id,
                targets=sorted(targets),
            )
            plan.pending_revision = None

        if len(task_list) > self.config.max_tasks:
            notes.append(
                f"task_list had {len(task_list)} items; kept the first {self.config.max_tasks}. "
                "Aim for 2-7 concrete actions."
            )
            task_list = task_list[: self.config.max_tasks]

        if plan.tasks:  # re-plan: keep the old breakdown as evidence
            plan.superseded_tasks.append([t.to_dict() for t in plan.tasks])

        previous = plan.tasks
        plan.tasks = [Task(task_id=i, title=title) for i, title in enumerate(task_list, start=1)]
        if plan.rework_from_completion:
            plan.rework_from_completion = False
            carried = self._carry_evidence(previous, plan.tasks)
            if carried:
                notes.append(
                    "Task(s) "
                    + ", ".join(str(t) for t in carried)
                    + " survived this rewrite unchanged, so they keep the work you "
                    "already did and their result_log. Do NOT do them again - only the "
                    "tasks that are still PENDING need work."
                )
                self.store.audit(
                    "evidence_carried", plan_id=plan.plan_id, tasks=carried
                )
        self._attach_options(plan, alternatives, reasons, notes)
        self._attach_done_when(plan, done_when, notes)
        plan.set_status(PlanStatus.AWAITING_APPROVAL)
        plan.approval.reset_request()
        plan.clear_draft()
        self._milestone(plan)
        self.store.save(state)
        self.store.audit(
            "plan_finalized", plan_id=plan.plan_id, tasks=[t.title for t in plan.tasks],
            auto=auto_submitted or None,
            choice_points=plan.choice_points() or None,
            criteria=[t.task_id for t in plan.tasks if t.done_when] or None,
        )

        # A trip decided earlier in this call (a respawn) is carried out when the handler
        # returns, and it waits on the human itself: asking here as well would hold one
        # call for two slices, past what the client allows.
        untripped = self._ctx().trip is None
        if auto_submitted and self.approval_ui is not None and untripped:
            # Straight to the human. The model was not going to call this plan final on
            # its own, so it cannot be trusted to ask for approval either - and the page
            # is where the verification it kept attempting actually happens.
            budget_note = (
                f"에이전트가 생각 단계 {budget}단계를 모두 써서, 서버가 마지막 초안을 그대로 "
                "제출했습니다."
            )
            last_thought = plan.last_thought().strip()
            if last_thought:
                budget_note += f"\n에이전트의 마지막 생각: {last_thought}"
            return self._ask_user(
                state, plan, {"plan_summary": budget_note}, notes, own=True,
                recorded=self._plan_recorded(plan),
            )
        if untripped and self._asks_itself():
            # 3.1.0: every final task list goes to the human in the call that recorded
            # it. There is no separate "now ask" step left for the model to forget.
            return self._ask_user(
                state, plan, {"plan_summary": self._spoken(thought)}, notes, own=True,
                recorded=self._plan_recorded(plan),
            )

        points = plan.choice_points()
        offered = (
            f" Task(s) {', '.join(str(t) for t in points)} offer a choice; the user picks "
            "on the approval page, and you will be told which way to do them."
            if points
            else ""
        )
        return build(
            plan,
            recorded_step=step_number,
            total_steps=plan.total_steps,
            tasks=plan.tasks_brief(),
            choice_points=points or None,
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            goal=plan.goal if goal_was_revised else None,
            message=(
                f"Plan created with {len(plan.tasks)} tasks. It does not need more "
                "checking - the user reviews it next. Execution is locked until they "
                "approve." + offered
            ),
        )

    @staticmethod
    def _plan_recorded(plan: Plan) -> str:
        """Said when the call that recorded a plan ends before the human has decided."""
        return (
            f"Your plan is recorded ({len(plan.tasks)} tasks) and the user is "
            "reviewing it."
        )

    def _attach_options(
        self,
        plan: Plan,
        alternatives: list[dict[str, Any]],
        reasons: dict[int, str],
        notes: list[str],
    ) -> None:
        """Turn the model's alternatives into each task's options (2.0.0).

        Every task's choice is rebuilt from what came with this task list - a redraft
        that drops an alternative drops the choice. A task already DONE (carried through
        a redraft from the completion report) offers none: there is nothing left to
        choose about work that has happened.
        """
        for task in plan.tasks:
            task.clear_choice()
        if not (alternatives or reasons):
            return
        locked = {t.task_id for t in plan.tasks if t.status == TaskStatus.DONE.value}
        options, alt_notes = build_options(
            [t.title for t in plan.tasks],
            alternatives,
            reasons,
            self.config.max_alternatives,
            self.config.max_choice_points,
            locked,
        )
        notes.extend(alt_notes)
        topics = collect_topics(alternatives, options)
        for task in plan.tasks:
            task.options = options.get(task.task_id)
            task.choice_topic = topics.get(task.task_id)

    @staticmethod
    def _carry_evidence(previous: list[Task], current: list[Task]) -> list[int]:
        """Move finished work onto the tasks that survived a redraft. Returns their ids.

        Only ever used when the redraft answers a revision asked for from the COMPLETION
        report: the work has already happened, and wiping it is what turned "you missed a
        step" into an order to rerun the entire plan. An ordinary re-plan - after a
        failure, say - still drops the old evidence, which is correct there.

        Matched with `title_key`, so a trailing period or a doubled space does not lose a
        task's work, and by key rather than by position, so reordering or inserting a step
        keeps it too. Deliberately no fuzzy matching: a miss only costs a redo, while a
        wrong match would let a task that must be redone keep a stale result_log and be
        skipped. Duplicate titles are paired in order; whatever is left over carries
        nothing. Only DONE work is carried - a FAILED task has nothing worth keeping.
        """
        pool: dict[str, list[Task]] = {}
        for task in previous:
            if task.status == TaskStatus.DONE.value:
                pool.setdefault(title_key(task.title), []).append(task)
        carried: list[int] = []
        for task in current:
            bucket = pool.get(title_key(task.title))
            if not bucket:
                continue
            source = bucket.pop(0)
            task.status = source.status
            task.result_log = source.result_log
            task.started_at = source.started_at
            task.finished_at = source.finished_at
            task.previous_result_log = source.previous_result_log
            # The contract the work was done under, and what the server found for it,
            # belong to the work and travel with it (3.0.0).
            task.done_when = source.done_when
            task.done_when_by = source.done_when_by
            task.files = list(source.files)
            task.checks = [dict(c) for c in source.checks]
            task.claims_withdrawn = list(source.claims_withdrawn)
            carried.append(task.task_id)
        return carried

    def _apply_task_updates(
        self,
        state: State,
        plan: Plan,
        updates: list[dict[str, Any]],
        targets: dict[int, str],
        notes: list[str],
        step_number: int,
        done_when: dict[int, str] | None = None,
        thought: str = "",
    ) -> dict[str, Any]:
        """Rewrite only the tasks the human flagged, leaving the rest untouched.

        This is the whole point of per-task review: the tasks the human already read and
        accepted keep their identity, their position, and - if the plan had already been
        running - their evidence. Only a flagged task is reset, because only a flagged
        task is being asked to change.

        The same machinery repairs a FAILED task (3.0.0). There the flag was raised by
        the failure, not by a comment: the failed task must be rewritten, the unfinished
        tasks after it may be, and everything already DONE is out of reach - which is
        what lets a plan that broke at step 3 keep the work of steps 1 and 2.

        And it applies a note the human wrote on a running plan (3.1.0). No task is
        singled out there: any unfinished task may be rewritten, at least one must be,
        and each rewritten one is marked with what they wrote.
        """
        repairing = plan.repairing()
        steering = plan.steering()
        said = next(iter(targets.values()), "") if steering else ""
        allowed = set(targets) | (plan.revision_open() if repairing or steering else set())
        # Validated in full before anything is written. A bad task_id halfway through a
        # single-pass loop would persist half a revision and leave the plan in a state
        # nobody approved.
        planned: list[tuple[int, str]] = []
        ignored: list[int] = []
        for item in updates:
            task_id = item.get("task_id")
            title = str(item.get("title") or "").strip()
            if not isinstance(task_id, int) or plan.get_task(task_id) is None:
                plan.touch()
                self.store.save(state)
                valid = ", ".join(str(t.task_id) for t in plan.tasks) or "none"
                return error(
                    plan,
                    ErrorCode.TASK_NOT_FOUND,
                    f"No task with task_id={task_id} in this plan. Valid task_id "
                    f"values are: {valid}.",
                    notes=notes,
                    recorded_step=step_number,
                    tasks=plan.tasks_brief(),
                )
            if task_id not in allowed or not title:
                ignored.append(task_id)
                continue
            planned.append((task_id, title))

        # A repair that leaves the failed task as it is repairs nothing: the plan would
        # be approved with a FAILED task in it, which nothing downstream can finish.
        missed = (
            [t for t in sorted(targets) if t not in {tid for tid, _ in planned}]
            if repairing else []
        )
        if not planned or missed:
            plan.touch()
            self.store.save(state)
            return error(
                plan,
                ErrorCode.REVISION_INCOMPLETE,
                "The failed task was not rewritten."
                if repairing
                else "No unfinished task was rewritten."
                if steering
                else "None of the tasks the user commented on were rewritten.",
                notes=notes,
                recorded_step=step_number,
                tasks=plan.tasks_brief(),
            )

        plan.superseded_tasks.append([t.to_dict() for t in plan.tasks])
        for task_id, title in planned:
            task = plan.get_task(task_id)
            if repairing:
                # Trying the same thing again is a legitimate repair (the failure may
                # have been passing); only a changed wording has a "before" to show.
                task.previous_title = (
                    task.title if title_key(title) != title_key(task.title) else None
                )
                if task_id in targets:
                    task.failure_note = targets[task_id]
            elif steering:
                task.previous_title = (
                    task.title if title_key(title) != title_key(task.title) else None
                )
                task.revision_note = said
            else:
                task.previous_title = task.title
                task.revision_note = targets[task_id]
            task.title = title
            task.clear_choice()  # the old options described the old wording
            # So did a criterion the model wrote. One the human wrote stays: it is their
            # statement of what the result must be, whatever the wording of the task.
            if not task.done_when_by:
                task.set_done_when(None)
            # A rewritten task is a different task, so whatever was done for the old one
            # no longer counts. Untouched tasks keep their status and their result_log.
            task.status = TaskStatus.PENDING.value
            task.result_log = None
            task.started_at = None
            task.finished_at = None
            task.clear_file_evidence()

        applied = [task_id for task_id, _ in planned]
        self._attach_done_when(plan, done_when or {}, notes, only=set(applied))
        if ignored:
            notes.append(
                f"Ignored the change to task(s) {', '.join(str(t) for t in ignored)}: "
                + ("finished tasks keep their results and cannot be rewritten here."
                   if repairing or steering
                   else "the user did not ask for those to change.")
            )
        # A note on a running plan names no task, so none can be left unaddressed.
        unaddressed = [] if steering else [t for t in sorted(targets) if t not in applied]
        if unaddressed:
            notes.append(
                "The user also commented on task(s) "
                f"{', '.join(str(t) for t in unaddressed)}, which you did not rewrite. "
                "They are unchanged and the user will see that when they review."
            )

        plan.pending_revision = None
        plan.clear_draft()
        plan.set_status(PlanStatus.AWAITING_APPROVAL)
        plan.approval.reset_request()
        self._milestone(plan)
        self.store.save(state)
        self.store.audit(
            "task_repaired" if repairing else "tasks_steered" if steering else "tasks_revised",
            plan_id=plan.plan_id,
            applied=applied,
            ignored=ignored or None,
            unaddressed=unaddressed or None,
            **(
                {"kept": [t.task_id for t in plan.tasks
                          if t.status == TaskStatus.DONE.value]}
                if repairing or steering else {}
            ),
        )
        if self._ctx().trip is None and self._asks_itself():
            # The change goes to the human in the call that made it (3.1.0).
            return self._ask_user(
                state, plan, {"plan_summary": self._spoken(thought)}, notes, own=True,
                recorded=(
                    f"Rewrote task(s) {', '.join(str(t) for t in applied)}. The user is "
                    "reviewing the change."
                ),
            )
        return build(
            plan,
            recorded_step=step_number,
            total_steps=plan.total_steps,
            tasks=plan.tasks_brief(),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            revised_tasks=applied,
            message=(
                f"Rewrote task(s) {', '.join(str(t) for t in applied)} after the failure. "
                "Finished tasks keep their results. Execution stays locked until the "
                "user approves this change."
                if repairing
                else f"Rewrote task(s) {', '.join(str(t) for t in applied)} as the user "
                "asked while you were working. Finished tasks keep their results. "
                "Execution stays locked until the user approves this change."
                if steering
                else f"Rewrote task(s) {', '.join(str(t) for t in applied)} as the user "
                "asked. Every other task is unchanged. Execution stays locked until the "
                "user approves this version."
            ),
        )

    # ------------------------------------------------------------------
    # 2. request_user_approval  (HITL gate)
    # ------------------------------------------------------------------
    def _request_user_approval(
        self,
        args: dict[str, Any],
        notes: list[str],
        progress_token: Any = None,
        notifier: Any = None,
        cancel_event: Any = None,
    ) -> dict[str, Any]:
        state = self.store.load()
        plan = self._resolve_plan(state, args.get("plan_id"))
        if plan is self._AMBIGUOUS:
            return self._ambiguous(state, notes)
        if plan is None:
            gone = self._evicted(state, args.get("plan_id"), notes)
            if gone is not None:
                return gone
        if self._expire_stale_approval(state, plan):
            notes.append(
                "This plan's earlier approval had expired and was revoked. Ask the user "
                "to approve the current plan again."
            )

        raw_decision = args.get("decision")
        try:
            decision = Decision(raw_decision)
        except ValueError:
            return error(
                plan,
                ErrorCode.INVALID_DECISION,
                f"'{raw_decision}' is not a valid decision.",
                notes=notes,
            )

        if plan is None:
            return error(plan, ErrorCode.NO_ACTIVE_PLAN, "No plan exists yet.", notes=notes)

        if plan.halt:
            # The circuit breaker holds this plan. Asking puts the halt in front of the
            # human (and waits, like any approval); a decision is only accepted when no
            # page holds the question - otherwise it did not come from the human (D20).
            if decision is Decision.ASK_USER:
                return self._ask_halt(state, plan, notes)
            if self._decision_is_the_models_own(plan):
                self.store.audit(
                    "self_approval_refused", plan_id=plan.plan_id, decision=decision.value,
                    halted=True,
                )
                return error(
                    plan,
                    ErrorCode.APPROVAL_PENDING,
                    f"'{decision.value}' did not come from the user - the pause is still "
                    "open on the approval page.",
                    notes=notes,
                    approval_url=self.approval_ui.url if self.approval_ui else None,
                )
            return self._resolve_halt(
                state, plan, decision.value, args.get("user_comment"), notes,
                choices=self._model_choices(args, notes),
            )

        if decision is Decision.ASK_USER:
            return self._ask_user(
                state,
                plan,
                args,
                notes,
                progress_token=progress_token,
                notifier=notifier,
                cancel_event=cancel_event,
            )

        # Everything below applies a decision. Only a human may make one, and while a
        # request for this exact plan version is still on the page, no human has.
        #
        # `requested_at` alone (checked in _approve) cannot tell the difference: it is
        # set precisely BECAUSE we are waiting, so it is at its most permissive at the
        # moment the model is most likely to guess. Under a chunked wait the model is
        # asked to call this tool over and over, which makes picking APPROVED instead of
        # ASK_USER a one-token slip away from unlocking execution nobody authorized.
        #
        # A decision the human really did make is not blocked here: _apply_late_decision
        # runs first in dispatch() and every _mutate_* withdraws the request, so by the
        # time this check sees the queue the entry is already gone.
        if self._decision_is_the_models_own(plan):
            self.store.audit(
                "self_approval_refused", plan_id=plan.plan_id, decision=decision.value
            )
            return error(
                plan,
                ErrorCode.APPROVAL_PENDING,
                f"'{decision.value}' did not come from the user - they have not "
                "answered the request that is still open on the approval page.",
                notes=notes,
                approval_url=self.approval_ui.url if self.approval_ui else None,
            )

        if decision is Decision.APPROVED:
            relayed = self._model_choices(args, notes)
            args = {k: v for k, v in args.items() if k != "choices"}
            if relayed:
                args[_MODEL_CHOICES] = relayed
            return self._approve(state, plan, args, notes)
        if decision is Decision.REVISE:
            return self._revise(state, plan, args, notes)
        return self._reject(state, plan, args, notes)

    def _ask_user(
        self,
        state: State,
        plan: Plan,
        args: dict[str, Any],
        notes: list[str],
        progress_token: Any = None,
        notifier: Any = None,
        cancel_event: Any = None,
        own: bool = False,
        recorded: str = "",
    ) -> dict[str, Any]:
        """Put the plan, or the finished work, in front of the human and wait.

        `own` (3.1.0): the server is asking by itself, at the transition - the call that
        recorded the final task list, or the last DONE. `recorded` is what that call
        did, said to the model if the human has not decided when the call must return.
        """
        if plan.status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
            return build(
                plan,
                message="This plan was already approved by the user. Continue executing it.",
                notes=notes,
                tasks=plan.tasks_brief(),
                next_task=plan.next_task_brief(),
            )
        if plan.status is PlanStatus.CANCELLED:
            return error(plan, ErrorCode.PLAN_CANCELLED, "This plan was cancelled.", notes=notes)
        if plan.status is PlanStatus.BLOCKED:
            return error(plan, ErrorCode.PLAN_BLOCKED, "A task failed.", notes=notes)
        # The human may have decided between two calls - their click is collected at the
        # top of this one (_apply_late_decision). In `return` mode that is the ordinary
        # way a decision arrives, and with the server asking by itself (3.1.0) this call
        # is the one that collects it. The plan is then no longer waiting to be asked
        # about, and asking again would undo what they decided (D30): a plan they had
        # just confirmed as finished went back to AWAITING_APPROVAL, and one they had
        # sent back for changes was put in front of them again, unchanged, while the
        # model never heard what they wrote.
        if plan.status is PlanStatus.COMPLETED:
            return build(
                plan,
                message="The user confirmed the work is finished. The plan is complete.",
                notes=notes,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
            )
        if plan.status is PlanStatus.DRAFTING and plan.tasks:
            sent_back = plan.approval.revision_count > 0
            flagged = plan.revision_targets()
            return build(
                plan,
                message=(
                    "The user asked for changes to this plan. Re-plan with plan_and_think; "
                    "the plan stays locked until they approve the new version."
                    if sent_back
                    else "This plan is still being drafted, so there is nothing to approve "
                    "yet. Finish it with plan_and_think."
                ),
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                # The same fields the reply carries when the decision is collected by
                # the call that was waiting for it (_revise).
                user_comment=(plan.approval.user_comment or None) if sent_back else None,
                revision_count=plan.approval.revision_count if sent_back else None,
                revision_scope=(SCOPE_TASKS if flagged else SCOPE_PLAN) if sent_back else None,
                revision_targets=[
                    {"task_id": task_id, "title": plan.get_task(task_id).title,
                     "user_comment": body}
                    for task_id, body in sorted(flagged.items())
                    if plan.get_task(task_id) is not None
                ] or None,
                tasks=plan.tasks_brief(),
            )
        if not plan.tasks:
            return error(
                plan,
                ErrorCode.PLAN_NOT_READY,
                "There is no task list to approve yet.",
                notes=notes,
            )

        # Two different questions share this tool: "may I run this plan?" before work,
        # and "is this actually finished?" after it. The plan's status decides which,
        # so the human always sees the right thing.
        completion_phase = plan.status is PlanStatus.AWAITING_COMPLETION
        plan_summary = (args.get("plan_summary") or "").strip()
        # The overview is the model's to give only where the model is the one asking,
        # and only the first time: a completion report already shows each task's
        # evidence, a request that is still on the page keeps the overview it was
        # opened with, and when the server asks at the transition (3.1.0) the model
        # was never told to write one.
        if not plan_summary and not (
            own
            or completion_phase
            or self._asks_itself()
            or self._live_request(plan) is not None
        ):
            return error(
                plan,
                ErrorCode.MISSING_PLAN_SUMMARY,
                "plan_summary is required when decision is ASK_USER.",
                notes=notes,
            )

        if completion_phase:
            # Before the human is asked to certify the work, look once more: a file
            # that was there when the task reported DONE may have been removed since.
            self._refresh_checks(plan)
            if plan.run_note:
                # What they wrote on the run card arrived after the last task had
                # finished. This report is where a change to finished work is asked
                # for, so their words are put in front of them here (D22).
                late = (
                    "실행 중 남기신 의견 (전달되기 전에 모든 태스크가 끝났습니다): "
                    + plan.run_note
                )
                plan_summary = f"{plan_summary}\n\n{late}" if plan_summary else late
        plan.approval.requested_at = now_iso()
        plan.approval.decision = None
        plan.approval.decided_at = None
        if not completion_phase:
            plan.set_status(PlanStatus.AWAITING_APPROVAL)
        else:
            plan.touch()
        self.store.save(state)
        self.store.audit(
            "completion_verification_requested" if completion_phase else "approval_requested",
            plan_id=plan.plan_id,
            plan_summary=plan_summary,
            by_server=own or None,
        )
        display = (
            render_completion_report(plan, plan_summary)
            if completion_phase
            else render_plan_for_user(
                plan, plan_summary, on_page=self.approval_ui is not None
            )
        )

        # The URL only reaches the human through stderr otherwise, which nobody reads in
        # a desktop app. Putting it in display_to_user means the model prints it in chat,
        # so a blocked popup or a second monitor no longer hides the approval page.
        approval_url = self.approval_ui.url if self.approval_ui is not None else None
        if approval_url:
            display = f"{display}\n\n현재 페이지: {approval_url.rstrip('/')}"

        # Asking a human is progress, whatever they then answer: the plan has left the
        # model's hands.
        self._milestone(plan)

        if self.approval_ui is not None:
            fingerprint = self._fingerprint(plan)
            phase = PHASE_COMPLETION if completion_phase else PHASE_PLAN
            request_id = self.approval_ui.open_request(
                plan.plan_id, plan.goal, display, plan.tasks_for_page(),
                fingerprint, phase, plan_summary,
            )
            if request_id is None:
                return self._wait_unavailable(plan, notes, display)
            outcome = self._wait_on(request_id, plan, notes)
            return self._settle(
                plan.plan_id, fingerprint, outcome, notes, approval_url, display,
                recorded=recorded,
            )

        return build(
            plan,
            message=recorded or None,
            # Not on the last DONE: that is an execution response, which carries
            # progress and never the task list - display_to_user has the report.
            tasks=None if own and completion_phase else plan.tasks_brief(),
            notes=notes,
            display_to_user=display,
            approval_url=approval_url,
        )

    def _refresh_checks(self, plan: Plan) -> None:
        """Re-look at the files already confirmed, and record the ones now gone.

        Only ever downgrades. Whether a file was changed during its task was decided
        when the task reported DONE and is not re-judged here: the human may open and
        save a file while reading the report, and that must neither improve the file's
        standing nor - through the fingerprint - void the request they are answering.
        """
        roots = self.config.artifact_roots
        if not roots:
            return
        deadline = time.monotonic() + FILE_CHECK_TIMEOUT_SEC
        for task in plan.tasks:
            held = [c for c in task.checks if c.get("state") in CONFIRMED_STATES]
            if not held:
                continue
            left = max(0.2, deadline - time.monotonic())
            current = check_files(
                [str(c.get("path")) for c in held], roots, task.started_at, timeout=left
            )
            for fact, now in zip(held, current):
                if now.get("state") in REFUSED_STATES:
                    fact["state"] = now["state"]
                    fact["gone"] = True
                    self.store.audit(
                        "file_gone_before_completion", plan_id=plan.plan_id,
                        task_id=task.task_id, path=fact.get("path"),
                    )

    def _wait_unavailable(
        self, plan: Plan, notes: list[str], display: str
    ) -> dict[str, Any]:
        """The request could not be published, so nothing is holding the agent."""
        # Degrading quietly would remove the hard pause without anyone noticing - the
        # worst possible failure for a safety gate. Make it audible instead.
        log.error(
            "APPROVAL UI UNAVAILABLE - the hard pause is OFF for this call. "
            "The model is only *asked* to stop."
        )
        self.store.audit("approval_ui_unavailable", plan_id=plan.plan_id)
        notes.append(
            "WARNING: the approval UI could not start, so this plan was NOT hard-paused. "
            "Do not execute anything. Show the plan to the user and stop."
        )
        return build(
            plan,
            tasks=plan.tasks_brief(),
            notes=notes,
            display_to_user=display,
        )

    def _hold_for_human(
        self, state: State, plan: Plan, notes: list[str], agent_note: str = ""
    ) -> dict[str, Any] | None:
        """If a human is deciding about this plan right now, wait on them. Else None.

        One rule for every tool: while the human has a request open for a plan, any call
        about that plan waits on the human, exactly as the approval call itself does. It
        is the physical pause applied to the stray call - the model that "reconsiders"
        mid-approval, or reaches for update_task_progress before anyone said yes - and it
        paces such a model at one call per wait slice instead of letting it spin.
        """
        entry = self._live_request(plan)
        if entry is None:
            return None
        note = (agent_note or "").strip()
        if note and note != "(no thought text provided)":
            try:
                self.approval_ui.set_agent_note(str(entry.get("id")), note)
            except Exception:  # noqa: BLE001 - a lost note must never fail the call
                log.debug("Could not attach the agent note to %s", plan.plan_id)
        self.store.audit(
            "call_held_for_human",
            plan_id=plan.plan_id,
            plan_status=plan.plan_status,
            phase=entry.get("phase"),
            agent_note=bool(note) or None,
            reconsider=reconsider_markers(note) or None,
        )
        fingerprint = self._request_fingerprint(plan)
        # If the wait ends with nobody having decided, the stray call still did nothing
        # - and must say so with a refusal, not an ok:true a weak model could read as
        # "started".
        if plan.halt:
            refusal = self._halt_code(plan)
        elif plan.status is PlanStatus.AWAITING_COMPLETION:
            refusal = ErrorCode.COMPLETION_PENDING
        else:
            refusal = ErrorCode.PLAN_NOT_APPROVED
        outcome = self._wait_on(str(entry.get("id")), plan, notes)
        return self._settle(
            plan.plan_id,
            fingerprint,
            outcome,
            notes,
            self.approval_ui.url,
            str(entry.get("display") or ""),
            refusal=refusal,
        )

    def _halted(
        self, state: State, plan: Plan, notes: list[str], agent_note: str = ""
    ) -> dict[str, Any]:
        """A call on a plan the circuit breaker holds. It waits if the halt is on the
        page; otherwise it is refused - only the human can lift a halt."""
        held = self._hold_for_human(state, plan, notes, agent_note=agent_note)
        if held is not None:
            return held
        return error(
            plan,
            self._halt_code(plan),
            "The user paused this plan. Only the user can resume it."
            if plan.paused_by_user()
            else "This plan is paused because a step kept repeating. Only the user can "
            "resume it.",
            notes=notes,
        )

    def _settle(
        self,
        plan_id: str,
        fingerprint: str,
        outcome: WaitOutcome,
        notes: list[str],
        approval_url: str | None,
        display: str,
        refusal: ErrorCode | None = None,
        recorded: str = "",
    ) -> dict[str, Any]:
        """Turn the end of a wait into the response, for every kind of request.

        `refusal` is set for a call that was held rather than asked (see
        _hold_for_human): when nobody decided, it reports that code instead of ok:true.

        `recorded` (3.1.0) is what the call did before it started waiting - a plan
        recorded, a task marked DONE. It is said only when nobody decided: a call that
        comes back "still waiting" must not leave the model unsure whether the thing it
        sent was taken, or it sends it again.
        """
        state = self.store.load()
        plan = state.plans.get(plan_id)
        decided = outcome.verdict
        if decided is not None:
            # The transaction was released while waiting, so another session may have
            # moved things on. Re-read THIS plan by id - resolving "the active plan"
            # would pick up a concurrent session's plan instead - and verify it is still
            # what the human saw.
            if plan is None or self._request_fingerprint(plan) != fingerprint:
                self.store.audit(
                    "approval_discarded_plan_changed",
                    plan_id=plan.plan_id if plan else None,
                    decision=decided.decision,
                )
                notes.append(
                    "The plan changed while the user was deciding, so that decision "
                    "was discarded. Show the current plan and ask again."
                )
                return build(
                    plan,
                    notes=notes,
                    tasks=plan.tasks_brief() if plan else None,
                    approval_url=approval_url,
                )
            if plan.halt:
                return self._resolve_halt(
                    state, plan, decided.decision, decided.comment, notes,
                    choices=decided.choices,
                )
            # Reuse the already-tested transitions so the blocking path and the
            # two-phase path can never diverge.
            forwarded: dict[str, Any] = (
                {"user_comment": decided.comment} if decided.comment else {}
            )
            if decided.choices:
                forwarded[_PAGE_CHOICES] = decided.choices
            if decided.criteria:
                forwarded[_PAGE_CRITERIA] = decided.criteria
            if decided.decision == Decision.APPROVED.value:
                return self._approve(state, plan, forwarded, notes)
            if decided.decision == Decision.REJECTED.value:
                return self._reject(state, plan, forwarded, notes)
            if decided.decision == Decision.REVISE.value:
                return self._revise(state, plan, forwarded, notes, verdict=decided)

        if outcome.reason == WAIT_PENDING:
            # The slice ended, not the question. Everything the model might mistake for
            # a verdict is deliberately withheld: no tasks, no display_to_user, no
            # message. A weak model that reads an ok:true payload full of plan detail as
            # "approved, proceed" is the failure this whole gate exists to prevent, so
            # the only thing on offer here is an instruction to wait.
            return error(
                plan,
                ErrorCode.APPROVAL_PENDING,
                (recorded + " " if recorded else "")
                + "Still waiting for the user to decide on the approval page.",
                notes=notes,
                approval_url=approval_url,
                waited_seconds=int(outcome.waited),
                remaining_seconds=int(outcome.remaining),
            )

        notes.append(
            "No human decision arrived before the wait expired. The plan is still "
            "LOCKED. Show the plan to the user and stop; do not execute anything. "
            "The request is still open on the approval page - if the user decides "
            "later, it will be applied on the next tool call."
        )
        if refusal is not None:
            return error(
                plan,
                refusal,
                # A call that was only held did nothing. One that recorded something
                # first - a DONE, before the user's pause took hold - says what.
                recorded or "The user has not decided yet, so this call did nothing.",
                notes=notes,
                display_to_user=display or None,
                approval_url=approval_url,
            )
        return build(
            plan,
            message=recorded or None,
            tasks=plan.tasks_brief() if plan else None,
            notes=notes,
            display_to_user=display,
            approval_url=approval_url,
        )

    def _wait_on(self, request_id: str, plan: Plan, notes: list[str]) -> WaitOutcome:
        """Hold this tool call while a human decides.

        This is what actually stops the agent: the client's loop waits synchronously for
        the tool result, so while we do not return, the model cannot emit another tool
        call - no matter what the system prompt failed to make it do.

        What it must NOT do is outlive the client's patience. A call killed at 60s takes
        the conversation with it (the client discards the result), so in the default
        chunked mode the wait is deliberately given up while the client is still
        listening, and the model is told to come straight back. The request itself stays
        on the page throughout: it belongs to the human, not to any one tool call.
        """
        ctx = self._ctx()
        progress_token, notifier, cancel_event = (
            ctx.progress_token, ctx.notifier, ctx.cancel_event
        )
        can_heartbeat = progress_token is not None and notifier is not None

        # Measured from when the request first appeared, not from now: a chunked wait is
        # many calls (and, after a restart, many processes) against one budget.
        already_waited = self.approval_ui.request_age(request_id)
        timeout = self.effective_timeout(can_heartbeat, already_waited)
        if self.config.approval_mode == APPROVAL_MODE_CHUNKED:
            # One call, one slice. A call that has already waited once - on a request
            # it opened itself, say - gets only what is left of the slice, so no path
            # through a handler can hold a single call past the client's limit.
            timeout = max(0, timeout - math.ceil(ctx.wait_spent))
        budget_left = max(0.0, self.config.approval_timeout - already_waited)
        # The page must not show a plan as running while it shows that plan's request.
        self._sync_runs()

        stop = threading.Event()
        if can_heartbeat:
            # Still sent, because it costs nothing and genuinely helps the clients that
            # honour it. It is no longer what the wait's safety depends on.
            threading.Thread(
                target=self._heartbeat,
                args=(notifier, progress_token, stop),
                name="approval-heartbeat",
                daemon=True,
            ).start()

        log.warning(
            "Waiting for human approval of %s at %s (mode %s, this call %ss, "
            "%ss of budget left, heartbeat %s)",
            plan.plan_id,
            self.approval_ui.url,
            self.config.approval_mode,
            timeout,
            int(budget_left),
            "on" if can_heartbeat else "off - no progressToken from client",
        )
        decided: Verdict | None = None
        cancelled = False
        started = time.monotonic()
        try:
            # Let every other session through while this one waits on a person.
            with self.store.paused():
                deadline = started + timeout
                next_touch = 0.0
                while True:
                    # Polling, not an Event: the decision may be recorded by a DIFFERENT
                    # server process (whichever one owns the page), so it has to be read
                    # from shared state.
                    #
                    # Claimed before the deadline is examined, so a decision that is
                    # already waiting is always collected - even when this call's slice
                    # is zero seconds, as it is in `return` mode and on the last scrap
                    # of a spent budget.
                    decided = self.approval_ui.claim(request_id)
                    if decided is not None:
                        break
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    if now >= next_touch:
                        # Tells the page an agent is genuinely still on the other end.
                        self.approval_ui.touch_agent(request_id)
                        next_touch = now + AGENT_HEARTBEAT_SEC
                    time.sleep(0.2)
        finally:
            stop.set()

        waited = time.monotonic() - started
        ctx.wait_spent += waited
        total_waited = already_waited + waited
        remaining = max(0.0, self.config.approval_timeout - total_waited)
        # A call that genuinely waited on a person, or brought back their decision, is
        # the harness working as designed. The circuit breaker must never count it -
        # a chunked wait repeats the very same call twenty times on purpose.
        if decided is not None or waited >= 1.0:
            ctx.waited = True

        if decided is not None:
            self.store.audit(
                "approval_decided_out_of_band",
                plan_id=plan.plan_id,
                decision=decided.decision,
                comment=decided.comment,
                scope=decided.scope,
                task_comments=decided.task_comments or None,
            )
            return WaitOutcome(decided, WAIT_DECIDED, total_waited, remaining)

        # Deliberately leave the request published in every case below. Clearing it here
        # is what once made the buttons vanish after 55s, before the human had a chance
        # to answer; whatever they click later is collected by _apply_late_decision.
        if cancelled:
            # The client stopped listening, so this response is going nowhere - but the
            # number is worth keeping. It is the only direct measurement of what this
            # client actually allows, and the next call shrinks itself to fit.
            self.store.audit(
                "client_cancelled_call", plan_id=plan.plan_id, after_sec=round(waited, 2)
            )
            self.store.record_call_cap(waited)
            log.warning(
                "Client abandoned the approval call after %.1fs; future waits will stay "
                "under that. The request is still open at %s.",
                waited,
                self.approval_ui.url,
            )

        if remaining <= 0 or self.config.approval_mode != APPROVAL_MODE_CHUNKED:
            self.store.audit(
                "approval_wait_timeout",
                plan_id=plan.plan_id,
                timeout=timeout,
                total_waited=round(total_waited, 2),
            )
            return WaitOutcome(None, WAIT_GAVE_UP, total_waited, 0.0)

        self.store.audit(
            "approval_chunk_expired",
            plan_id=plan.plan_id,
            chunk=timeout,
            total_waited=round(total_waited, 2),
            remaining=round(remaining, 2),
        )
        return WaitOutcome(None, WAIT_PENDING, total_waited, remaining)

    def effective_timeout(self, can_heartbeat: bool, already_waited: float = 0.0) -> int:
        """How long we may hold THIS tool call open.

        The distinction that matters is between the total wait (approval_timeout, which
        belongs to the human) and one call's share of it (which belongs to whatever
        limit the client enforces). Only the second is capped here.

        `trust_heartbeat` keeps the pre-1.14 bet that progress notifications extend the
        client's timer. `chunked` makes no assumption about the client at all - which is
        the only safe default, since the option that controls it is the client's to set
        and impossible for a server to observe.
        """
        # Rounded up, not truncated. Truncating turns any budget with a fractional
        # remainder into zero, which skips the wait entirely - and a gate that silently
        # stops waiting is indistinguishable from no gate.
        remaining = max(0, math.ceil(self.config.approval_timeout - already_waited))
        mode = self.config.approval_mode
        if mode == APPROVAL_MODE_RETURN:
            return 0
        if mode == APPROVAL_MODE_TRUST_HEARTBEAT:
            if can_heartbeat:
                return remaining
            return min(remaining, NO_PROGRESS_WAIT_CEILING_SEC)
        budget = max(1, self.config.call_budget)
        observed = self.store.observed_call_cap()
        if observed is not None:
            # A client has already been seen giving up sooner than we assumed. Believe
            # the measurement over the default, and leave a margin under it.
            budget = min(budget, max(5, int(observed) - 5))
        return min(remaining, budget)

    @staticmethod
    def _heartbeat(notifier: Any, token: Any, stop: threading.Event) -> None:
        """Reset the client's request timer while a human thinks.

        The MCP TS SDK resets its 60s timeout on every progress notification and sets no
        maxTotalTimeout, so a steady heartbeat turns a bounded wait into an open one.
        """
        n = 0
        while not stop.wait(20):
            n += 1
            notifier.progress(token, n, "Waiting for human approval...")

    def _approve(
        self, state: State, plan: Plan, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        if plan.status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
            return build(
                plan,
                message="Already approved. Continue executing.",
                notes=notes,
                tasks=plan.tasks_brief(),
                next_task=plan.next_task_brief(),
            )
        completion_phase = plan.status is PlanStatus.AWAITING_COMPLETION
        if plan.status is not PlanStatus.AWAITING_APPROVAL and not completion_phase:
            return error(
                plan,
                ErrorCode.PLAN_NOT_READY,
                f"A plan in status {plan.plan_status} cannot be approved.",
                notes=notes,
            )
        if not plan.approval.requested_at:
            # Approval reported for a plan version the user was never shown. Every change
            # to the task list (finalize, revise, cross-session replacement) clears
            # requested_at, so this branch means either the model skipped ASK_USER or the
            # plan CHANGED after it was shown. Accepting would let an approval land on
            # tasks the human never saw - the exact cross-session misdirection this gate
            # exists to prevent. Hard refusal: force a re-display of the current plan.
            self.store.audit("stale_approval_refused", plan_id=plan.plan_id)
            return error(
                plan,
                ErrorCode.APPROVAL_NOT_REQUESTED,
                "The current version of this plan was never shown to the user.",
                notes=notes,
                tasks=plan.tasks_brief(),
            )

        choices = args.get(_PAGE_CHOICES)
        if choices is None and args.get(_MODEL_CHOICES):
            options = {t.task_id: t.options for t in plan.tasks if t.has_choice}
            choices, choice_notes = validate_model_choices(options, args[_MODEL_CHOICES])
            notes.extend(choice_notes)
        rewritten = self._mutate_approved(
            plan, args.get("user_comment"), choices, args.get(_PAGE_CRITERIA)
        )
        self.store.save(state)
        self.store.audit(
            "completion_verified" if completion_phase else "approved",
            plan_id=plan.plan_id,
            comment=args.get("user_comment"),
        )

        if completion_phase:
            return build(
                plan,
                message="The user confirmed the work is finished. The plan is now complete.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
            )

        return build(
            plan,
            message=(
                # Everything carried through the redraft, so there is nothing left to
                # run - saying "unlocked" here would read as "go and do it all again".
                "The user approved this plan. Every task in it is already finished, so "
                "report completion now instead of executing anything."
                if plan.status is PlanStatus.AWAITING_COMPLETION
                else "Execution is now unlocked."
                + self._choices_sentence(plan)
                + self._criteria_sentence(rewritten)
            ),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
            next_task=plan.next_task_brief(),
        )

    def _revise(
        self,
        state: State,
        plan: Plan,
        args: dict[str, Any],
        notes: list[str],
        verdict: Verdict | None = None,
    ) -> dict[str, Any]:
        """Record a request for changes.

        `verdict` is present only when the decision came from the approval page, which is
        the only place per-task comments can be written. When the model reports the
        decision itself (the two-phase path) there is no such detail, so the plan is
        redrafted whole - exactly as it always was.
        """
        comment = args.get("user_comment") or ""
        kind, detail = self._mutate_no(
            plan,
            comment,
            verdict.task_comments if verdict else None,
            verdict.scope if verdict else SCOPE_PLAN,
            verdict.criteria if verdict else None,
        )
        if kind == "REWORK":
            return self._reworked(state, plan, detail, comment, verdict, notes)
        targets = detail
        # A whole-plan revision carries any criteria the human typed inside the comment
        # (see _mutate_revise), so the model is told the comment as it was recorded.
        comment = plan.approval.user_comment or comment
        self.store.save(state)
        self.store.audit(
            "revision_requested",
            plan_id=plan.plan_id,
            revision_count=plan.approval.revision_count,
            user_comment=comment,
            scope=SCOPE_TASKS if targets else SCOPE_PLAN,
            task_comments=(verdict.task_comments or None) if verdict else None,
        )

        if targets:
            flagged = plan.revision_targets()
            return build(
                plan,
                message=(
                    "The user asked for changes to specific tasks. Rewrite ONLY those "
                    "tasks and send them back as task_updates. Every other task was "
                    "accepted and must stay exactly as it is."
                ),
                notes=notes,
                user_comment=comment or None,
                revision_count=plan.approval.revision_count,
                revision_scope=SCOPE_TASKS,
                revision_targets=[
                    {
                        "task_id": task_id,
                        "title": plan.get_task(task_id).title,
                        "user_comment": body,
                    }
                    for task_id, body in sorted(flagged.items())
                ],
                tasks_unchanged=[t.task_id for t in plan.tasks if t.task_id not in flagged],
                tasks=plan.tasks_brief(),
            )

        return build(
            plan,
            message=(
                "The user requested changes. Re-plan with plan_and_think. The plan stays "
                "locked until they approve the new version."
            ),
            notes=notes,
            user_comment=comment or None,
            revision_count=plan.approval.revision_count,
            revision_scope=SCOPE_PLAN,
            # Even a whole-plan rewrite benefits from knowing what the human said about
            # each task; it just does not constrain which tasks may change.
            task_comments=(verdict.task_comments or None) if verdict else None,
        )

    def _reworked(
        self,
        state: State,
        plan: Plan,
        reopened: list[int],
        comment: str,
        verdict: Verdict | None,
        notes: list[str],
    ) -> dict[str, Any]:
        """Answer a completion report that sent specific tasks back to be done again."""
        self.store.save(state)
        self.store.audit(
            "rework_requested",
            plan_id=plan.plan_id,
            revision_count=plan.approval.revision_count,
            user_comment=comment,
            reopened=reopened,
            task_comments=(verdict.task_comments or None) if verdict else None,
        )
        which = ", ".join(str(t) for t in reopened)
        kept = [t.task_id for t in plan.tasks if t.status == TaskStatus.DONE.value]
        return build(
            plan,
            message=(
                f"The user checked the finished work and sent task(s) {which} back to be "
                "done AGAIN. The plan itself is unchanged and still approved - do not "
                "re-plan and do not ask for approval. Redo only those tasks, then report "
                "completion again."
            ),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            user_comment=comment or None,
            revision_count=plan.approval.revision_count,
            reopened_tasks=reopened,
            tasks_unchanged=kept or None,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
            next_task=plan.next_task_brief(),
        )

    def _reject(
        self, state: State, plan: Plan, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        comment = args.get("user_comment") or ""
        self._mutate_rejected(plan, comment)
        self.store.save(state)
        self.store.audit("rejected", plan_id=plan.plan_id, user_comment=comment)
        return build(
            plan,
            message="The plan was cancelled by the user. Nothing was executed.",
            notes=notes,
            user_comment=comment or None,
        )

    # ------------------------------------------------------------------
    # 3. update_task_progress
    # ------------------------------------------------------------------
    def _update_task_progress(self, args: dict[str, Any], notes: list[str]) -> dict[str, Any]:
        state = self.store.load()
        plan = self._resolve_plan(state, args.get("plan_id"))
        if plan is self._AMBIGUOUS:
            return self._ambiguous(state, notes)
        if plan is None:
            gone = self._evicted(state, args.get("plan_id"), notes)
            if gone is not None:
                return gone
        if plan is not None and plan.halt:
            return self._halted(state, plan, notes)
        if plan is not None:
            # Reaching for execution while the human is still deciding: wait on them
            # instead of bouncing off PLAN_NOT_APPROVED as fast as the model can retry.
            held = self._hold_for_human(state, plan, notes)
            if held is not None:
                return held
        if self._expire_stale_approval(state, plan):
            self.store.audit("execution_blocked", plan_id=plan.plan_id, reason="APPROVAL_EXPIRED")
            return error(
                plan,
                ErrorCode.APPROVAL_EXPIRED,
                "The approval for this plan expired while it sat idle.",
                notes=notes,
            )

        guard = execution_guard(plan, autoapprove=self.config.autoapprove)
        if guard is not None:
            self.store.audit(
                "execution_blocked",
                plan_id=plan.plan_id if plan else None,
                reason=guard.value,
                attempted_task_id=args.get("task_id"),
            )
            return error(plan, guard, "Execution is not allowed in the current state.", notes=notes)

        assert plan is not None  # execution_guard returns NO_ACTIVE_PLAN otherwise
        if self.config.autoapprove and plan.status is PlanStatus.AWAITING_APPROVAL:
            log.warning("PLANNING_MCP_AUTOAPPROVE is ON - the HITL gate is bypassed (test mode).")
            plan.set_status(PlanStatus.APPROVED)

        raw_status = args.get("status")
        try:
            status = TaskStatus(raw_status)
        except ValueError:
            return error(
                plan,
                ErrorCode.INVALID_STATUS,
                f"'{raw_status}' is not a valid task status.",
                notes=notes,
            )

        task_id = args.get("task_id")
        task = plan.get_task(task_id) if task_id is not None else None
        if task is None:
            return error(
                plan,
                ErrorCode.TASK_NOT_FOUND,
                f"No task with task_id={task_id} in this plan.",
                notes=notes,
            )

        if status in (TaskStatus.IN_PROGRESS, TaskStatus.PENDING):
            # What the human asked of this run on the page (3.1.0) is applied before a
            # task is started. A DONE or a FAILED is recorded first - see there.
            control = self._run_control(plan)
            if control is not None:
                return self._apply_run_control(state, plan, control, notes)

        if status is TaskStatus.IN_PROGRESS:
            return self._start_task(state, plan, task, notes)
        if status is TaskStatus.DONE:
            return self._finish_task(state, plan, task, args, notes)
        if status is TaskStatus.FAILED:
            return self._fail_task(state, plan, task, args, notes)
        return self._reset_task(state, plan, task, notes)

    def _start_task(
        self, state: State, plan: Plan, task: Task, notes: list[str]
    ) -> dict[str, Any]:
        if task.status == TaskStatus.DONE.value:
            return build(
                plan,
                message="That task is already DONE. next_task names the one to work on now.",
                notes=notes,
                progress=plan.progress(),
            )
        if task.status == TaskStatus.IN_PROGRESS.value:
            # Already running - usually because the server started it (auto-advance) and
            # the model sent IN_PROGRESS out of habit. Rewriting started_at here would
            # backdate nothing useful and lose the real start time.
            return build(
                plan,
                progress=plan.progress(),
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                message=(
                    "That task was already IN_PROGRESS. Do the work now, then report it "
                    "DONE with a result_log."
                ),
            )
        if not can_start_task(plan, task.task_id):
            # Redirect rather than reject: rejecting strands the model.
            self.store.audit(
                "out_of_order_start", plan_id=plan.plan_id, attempted=task.task_id
            )
            return build(
                plan,
                message=(
                    "That task cannot start yet because earlier tasks are not finished. "
                    "Start the one in next_task instead."
                ),
                notes=notes,
                progress=plan.progress(),
            )

        task.status = TaskStatus.IN_PROGRESS.value
        task.started_at = now_iso()
        plan.set_status(PlanStatus.IN_EXECUTION)
        self.store.save(state)
        self.store.audit("task_started", plan_id=plan.plan_id, task_id=task.task_id)
        return build(
            plan,
            progress=plan.progress(),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            message="Marked IN_PROGRESS - it is the task in next_task. Do the work now.",
        )

    def _finish_task(
        self, state: State, plan: Plan, task: Task, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        # Reporting DONE is a claim that work happened, and a weak model will happily
        # fire DONE at every task in a row without doing any. The server cannot observe
        # the work, but it can refuse claims that are structurally impossible or
        # evidence-free. Starting the wrong task is still forgiven (a redirect); saying
        # the wrong task is finished is not.
        if task.status == TaskStatus.DONE.value:
            return build(
                plan,
                message="That task is already DONE. next_task names the one to work on now.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                progress=plan.progress(),
            )

        if task.status != TaskStatus.IN_PROGRESS.value:
            self.store.audit(
                "done_without_start", plan_id=plan.plan_id, task_id=task.task_id,
                previous_status=task.status,
            )
            return error(
                plan,
                ErrorCode.TASK_NOT_STARTED,
                "That task was never marked IN_PROGRESS.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                progress=plan.progress(),
            )

        earlier = plan.unfinished_before(task.task_id)
        if earlier:
            self.store.audit(
                "done_out_of_order", plan_id=plan.plan_id, task_id=task.task_id,
                unfinished=[t.task_id for t in earlier],
            )
            return error(
                plan,
                ErrorCode.TASK_OUT_OF_ORDER,
                f"{len(earlier)} earlier task(s) are not finished yet.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                progress=plan.progress(),
            )

        evidence = (args.get("result_log") or "").strip()
        # A reopened task whose new outcome is byte-for-byte the old one is a structurally
        # impossible claim, exactly like an evidence-free DONE: the human sent it back
        # *because* that outcome was not good enough, so it cannot also be the answer. The
        # server still cannot see whether real work happened - but it can see this.
        if task.revision_note and task.previous_result_log and evidence:
            if _normalize_evidence(evidence) == _normalize_evidence(task.previous_result_log):
                self.store.audit(
                    "rework_resubmitted_old_evidence",
                    plan_id=plan.plan_id, task_id=task.task_id, result_log=evidence,
                )
                return error(
                    plan,
                    ErrorCode.REWORK_NOT_DONE,
                    "DONE refused: this is the same result_log that task already had, "
                    "and the user rejected that outcome.",
                    notes=notes,
                    qualify=len(state.active_plans()) > 1,
                    progress=plan.progress(),
                )
        reason = missing_evidence_reason(evidence, task.title, self.config.min_result_log)
        if reason is not None:
            self.store.audit(
                "done_without_evidence", plan_id=plan.plan_id, task_id=task.task_id,
                result_log=evidence, reason=reason,
            )
            return error(
                plan,
                ErrorCode.MISSING_RESULT_LOG,
                f"DONE refused: {reason}.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                progress=plan.progress(),
            )

        # --- the verification contract (3.0.0) ---------------------------------
        # The two claims the server can test instead of taking on trust: a file the
        # task says it produced, and evidence that says more than the criterion did.
        declared = [str(p) for p in (args.get("files") or [])]
        roots = self.config.artifact_roots
        facts: list[dict[str, Any]] = []
        if declared and not roots:
            notes.append(
                "This server does not check files, so 'files' was ignored. Say in "
                "result_log where the output is."
            )
            declared = []
        if roots:
            facts = check_files(
                declared, roots, task.started_at, DECLARED, FILE_CHECK_TIMEOUT_SEC
            )
            absent = refused(facts)
            if absent:
                for path in absent:
                    if path not in task.file_refusals:
                        task.file_refusals.append(path)
                plan.touch()
                self.store.save(state)
                self.store.audit(
                    "file_not_found", plan_id=plan.plan_id, task_id=task.task_id,
                    files=absent, result_log=evidence,
                )
                looked = "; ".join(str(r) for r in roots)
                return error(
                    plan,
                    ErrorCode.FILE_NOT_FOUND,
                    "DONE refused: the server could not find "
                    + ", ".join(f"'{p}'" for p in absent)
                    + f" (or it is empty). It looks in: {looked}.",
                    notes=notes,
                    qualify=len(state.active_plans()) > 1,
                    progress=plan.progress(),
                )
            facts += check_mentions(
                evidence, declared, roots, task.started_at, FILE_CHECK_TIMEOUT_SEC
            )
        score = round(novelty(task.done_when, evidence), 2) if task.done_when else None
        # A criterion said back is not evidence - unless the server has itself found
        # what the task produced, in which case the sentence no longer has to carry it.
        # A file that was already there before the task does not count for that.
        if not produced(facts) and repeats_criterion(
            task.done_when, evidence, self.config.evidence_novelty
        ):
            self.store.audit(
                "done_repeats_criterion", plan_id=plan.plan_id, task_id=task.task_id,
                result_log=evidence, novelty=score,
            )
            return error(
                plan,
                ErrorCode.MISSING_RESULT_LOG,
                "DONE refused: result_log only repeats the done_when sentence. Say what "
                "was actually produced - the values, the names or the path.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                progress=plan.progress(),
            )

        task.status = TaskStatus.DONE.value
        task.finished_at = now_iso()
        task.result_log = evidence
        task.files = declared
        task.checks = facts
        if task.file_refusals and not confirmed(facts):
            # The model named a file, was told it is not there, and then reported DONE
            # with nothing the server could confirm. Allowed - the task may truly have
            # produced no file - but the human is shown that the claim was dropped.
            task.claims_withdrawn = list(task.file_refusals)
            self.store.audit(
                "file_claim_withdrawn", plan_id=plan.plan_id, task_id=task.task_id,
                files=task.claims_withdrawn,
            )
        task.file_refusals = []
        self._milestone(plan)
        # What the human asked of this run on the page (3.1.0). Read after the DONE is
        # on the task: the work is kept either way, and it is the NEXT task that does
        # not start.
        control = self._run_control(plan)

        if plan.all_done():
            if self.config.completion_approval:
                # COMPLETED is no longer something the model can award itself. The human
                # sees the per-task evidence first - the only check that catches invented
                # result_log text.
                plan.set_status(PlanStatus.AWAITING_COMPLETION)
                plan.approval.reset_request()
                self._withdraw_approval_request(plan)
                self.store.audit("completion_pending", plan_id=plan.plan_id)
            else:
                plan.set_status(PlanStatus.COMPLETED)
                self._withdraw_approval_request(plan)
            advanced = None
            if control is not None and plan.status is PlanStatus.AWAITING_COMPLETION:
                # Nothing is left to stop or to change. What they wrote still reaches
                # them, on the completion report (see _ask_user).
                plan.run_note = self._control_words(control) or None
        else:
            plan.set_status(PlanStatus.IN_EXECUTION)
            advanced = None if control is not None else self._auto_advance(plan)
        self.store.save(state)
        self.store.audit(
            "task_done",
            plan_id=plan.plan_id,
            task_id=task.task_id,
            result_log=task.result_log,
            # Field data for tuning the contract: how far the evidence went beyond the
            # criterion, and what the server found. Absent on a task under no contract.
            **({"novelty": score} if score is not None else {}),
            **(
                {"checks": [[c.get("source"), c.get("state")] for c in task.checks]}
                if task.checks else {}
            ),
        )

        if control is not None and plan.all_done():
            self._consume_run_control(plan)
            self.store.audit(
                "run_control_moot", plan_id=plan.plan_id, action=control.get("action"),
                comment=self._control_words(control) or None, reason="all_tasks_done",
            )
        elif control is not None:
            return self._apply_run_control(
                state, plan, control, notes, reported="That task is DONE. "
            )
        if plan.status is PlanStatus.AWAITING_COMPLETION and self._asks_itself():
            # 3.1.0: the last DONE is the completion report. The human checks the
            # results in the call that finished them.
            return self._ask_user(
                state, plan, {}, notes, own=True,
                recorded=(
                    "That task is DONE and every task in this plan is finished. The "
                    "user is checking the results."
                ),
            )

        nxt = plan.current_task()
        if advanced is not None:
            message = (
                f"That task is DONE. The next one ('{advanced.title}') has been started "
                "for you and is in next_task - do that work NOW, then report it DONE "
                "with its own result_log. Do not send IN_PROGRESS again."
                + self._rework_suffix(advanced)
                + choice_note(advanced)
            )
        elif nxt is not None:
            # Nothing was auto-started (auto_advance off, or the next task is not
            # startable yet) but work remains - never let this read as "finished".
            message = (
                f"That task is DONE. The plan is NOT finished: next is '{nxt.title}', "
                "in next_task. Call update_task_progress with its task_id and "
                "status='IN_PROGRESS'."
                + self._rework_suffix(nxt)
                + choice_note(nxt)
            )
        elif plan.status is PlanStatus.AWAITING_COMPLETION:
            # Every task is finished, so the only thing left is the completion report.
            # A silent message here left the model guessing at the last, most
            # error-prone step.
            message = (
                "That task is DONE and every task in this plan is finished. "
                "Report completion NOW: call request_user_approval with "
                "decision='ASK_USER' and a plan_summary that states, task by task, what "
                "you actually produced. Do not declare success yourself."
            )
        else:
            message = (
                "That task is DONE and every task in this plan is finished. "
                "Report completion NOW: write the final answer to the user, summarizing "
                "the result_log of each task."
            )
        return build(
            plan,
            progress=plan.progress(),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            message=message,
        )

    @staticmethod
    def _rework_suffix(task: Task | None) -> str:
        """Why this task is open again, restated wherever it is handed to the model.

        Two reworked tasks in one round means the second is reached by finishing the
        first, and the message announcing that is the only thing the model reads at
        that moment. Dropping the request there is how it gets redone as if nothing
        had been asked for.
        """
        if task is None or not task.revision_note:
            return ""
        return (
            f' The user sent that task back to be done again: "{task.revision_note}". '
            "Answer that, do not repeat what you did before."
        )

    def _auto_advance(self, plan: Plan) -> Task | None:
        """Start the next task as part of reporting the previous one DONE.

        The separate IN_PROGRESS call exists to force a turn boundary between "I am
        starting" and "I am finished" - but reporting DONE already *is* that boundary
        for the next task: the model receives "task N is in progress, do the work" and
        still cannot claim DONE until a later call, with its own evidence. So the
        boundary survives intact and one round trip per task disappears, which is what a
        small model's context and error rate actually pay for.

        Nothing here relaxes the DONE guard: _finish_task's checks are unchanged, and a
        task is only ever advanced into once every earlier task is finished.
        """
        if not self.config.auto_advance:
            return None
        nxt = plan.current_task()
        if nxt is None or nxt.status != TaskStatus.PENDING.value:
            return None
        if not can_start_task(plan, nxt.task_id):
            return None
        nxt.status = TaskStatus.IN_PROGRESS.value
        nxt.started_at = now_iso()
        self.store.audit("task_auto_started", plan_id=plan.plan_id, task_id=nxt.task_id)
        return nxt

    def _fail_task(
        self, state: State, plan: Plan, task: Task, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        task.status = TaskStatus.FAILED.value
        task.finished_at = now_iso()
        task.result_log = args.get("result_log") or task.result_log
        self._milestone(plan)
        plan.set_status(PlanStatus.BLOCKED)
        kept = [t.task_id for t in plan.tasks if t.status == TaskStatus.DONE.value]
        if self.config.local_repair:
            # Open this one task for repair (3.0.0). Before, a failure sent the model
            # back to draft the whole plan again, and the redraft dropped the evidence
            # of every task that had already finished - a break at step 3 cost steps 1
            # and 2. Now the failed task is the flagged one, exactly as if the human had
            # commented on it, and only it (and what follows it) may change.
            reason = (task.result_log or "").strip() or "no reason was given"
            plan.pending_revision = {
                "targets": {str(task.task_id): reason},
                "origin": ORIGIN_FAILURE,
                "open": [
                    t.task_id for t in plan.tasks
                    if t.task_id > task.task_id and t.status != TaskStatus.DONE.value
                ],
            }
        # What the human asked of this run on the page (3.1.0). The failure already
        # stops the plan and sends it back for their approval, so a stop has nothing
        # left to do; anything they wrote goes to the model with this response, where
        # it can shape the repair.
        control = self._run_control(plan)
        self.store.save(state)
        self.store.audit(
            "task_failed", plan_id=plan.plan_id, task_id=task.task_id, result_log=task.result_log
        )
        said = ""
        if control is not None:
            self._consume_run_control(plan)
            said = self._control_words(control)
            self.store.audit(
                "run_control_moot", plan_id=plan.plan_id, action=control.get("action"),
                comment=said or None, reason="task_failed",
            )
        return build(
            plan,
            progress=plan.progress(),
            notes=notes,
            failed_task={"title": task.title, "result_log": task.result_log},
            tasks_unchanged=(kept or None) if self.config.local_repair else None,
            user_comment=said or None,
            message=(
                f"The task '{task.title}' failed. Forward progress is halted."
                + (
                    " The finished tasks keep their results - only this task needs "
                    "another way."
                    if self.config.local_repair and kept
                    else ""
                )
                + (
                    f' While you were working the user also wrote: "{said}". Take it '
                    "into account."
                    if said
                    else ""
                )
            ),
        )

    def _reset_task(
        self, state: State, plan: Plan, task: Task, notes: list[str]
    ) -> dict[str, Any]:
        task.status = TaskStatus.PENDING.value
        task.started_at = None
        task.finished_at = None
        if plan.status is PlanStatus.BLOCKED and not plan.first_failed_task():
            plan.set_status(PlanStatus.IN_EXECUTION)
        self.store.save(state)
        self.store.audit("task_reset", plan_id=plan.plan_id, task_id=task.task_id)
        return build(
            plan,
            progress=plan.progress(),
            notes=notes,
            message=f"The task '{task.title}' was reset to PENDING.",
        )

    # ------------------------------------------------------------------
    # 4. get_current_plan
    # ------------------------------------------------------------------
    def _get_current_plan(self, args: dict[str, Any], notes: list[str]) -> dict[str, Any]:
        state = self.store.load()
        requested = (args.get("plan_id") or "current").strip()

        plan = self._resolve_plan(state, requested)
        # Both "which plan did you mean?" answers below stay ok=true - recovery must never
        # fail - but they carry PLAN_AMBIGUOUS so next_action comes out of the one state
        # machine and says CALL_GET_CURRENT_PLAN. Without the code they defaulted to
        # CALL_PLAN_AND_THINK ("there is no active plan, start one"), which contradicted
        # the message and is how a model asking for its own plan ended up forking a new one.
        if plan is self._AMBIGUOUS:
            return build(
                None,
                error_code=ErrorCode.PLAN_AMBIGUOUS,
                notes=notes,
                message=(
                    f"{len(state.active_plans())} plans are active, so 'current' cannot say "
                    "which one is yours. Call again with the plan_id your conversation has "
                    "been receiving in every response - it is one of the plans listed here."
                ),
                active_plans=self._plan_directory(state),
            )

        if plan is None:
            # A plan the server closed to make room (3.0.0) is neither "no plan exists"
            # nor a mistyped id, and must not be answered with other conversations' plans.
            gone = self._evicted(state, requested, notes)
            if gone is not None:
                return gone

        if plan is None and requested.lower() not in ("current", "active", "latest", ""):
            # An id that names nothing is NOT the same as "no plan exists". Saying the
            # latter invites the model to throw its plan away and start over, when the id
            # is far more likely stale or mistyped - so show it what it can actually ask for.
            notes.append(f"No plan with id '{requested}'.")
            directory = self._plan_directory(state)
            if directory:
                return build(
                    None,
                    error_code=ErrorCode.PLAN_AMBIGUOUS,
                    notes=notes,
                    message=(
                        f"There is no plan with id '{requested}'. Do NOT start a new plan - "
                        "call again with one of the plan_ids listed here."
                    ),
                    active_plans=directory,
                )
            return build(
                None,
                notes=notes,
                message=(
                    f"There is no plan with id '{requested}', and no plan is active."
                    if state.plans
                    else "No plan has been created yet."
                ),
            )

        if plan is None:
            return build(None, notes=notes, message="No plan has been created yet.")

        # Compact the history so recovery never blows an already-truncated context.
        active_steps = [s for s in plan.thinking_steps if not s.superseded]
        superseded_count = len(plan.thinking_steps) - len(active_steps)
        thinking = [
            {"step_number": s.step_number, "thought": s.thought, "superseded": False}
            for s in active_steps[-6:]
        ]

        return build(
            plan,
            goal=plan.goal,
            # Only when it actually moved: an unchanged goal repeated twice reads as two
            # different goals to a weak model.
            original_goal=plan.original_goal if plan.goal_history else None,
            goal_revisions=len(plan.goal_history) or None,
            thinking_steps=thinking,
            superseded_steps=f"{superseded_count} earlier steps superseded"
            if superseded_count
            else None,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
            approval={
                "decision": plan.approval.decision,
                "revision_count": plan.approval.revision_count,
                "user_comment": plan.approval.user_comment,
            },
            # Recovery must explain a pause, or a model re-reading its plan sees nothing
            # wrong and retries the very call that was stopped.
            halted=(
                {"reason": plan.halt.get("text"), "since": plan.halt.get("at")}
                if plan.halt
                else None
            ),
            draft_tasks=plan.draft_tasks or None,
            thinking_steps_left=plan.thinking_steps_left()
            if plan.status is PlanStatus.DRAFTING
            else None,
            notes=notes,
        )


__all__ = ["PlanningHandlers", "Approval"]
