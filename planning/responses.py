"""The single response builder.

Every handler returns through `build()`. That guarantees the four fields the system
prompt teaches the model to rely on - ok, plan_status, next_action, next_action_hint -
are present on every response from every tool, including error paths.
"""

from __future__ import annotations

from typing import Any

from .models import ErrorCode, NextAction, Plan, PlanStatus
from .state_machine import resolve_next_action


def build(
    plan: Plan | None,
    *,
    ok: bool = True,
    error_code: ErrorCode | None = None,
    message: str | None = None,
    notes: list[str] | None = None,
    qualify: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    action, hint = resolve_next_action(plan, error_code, qualify=qualify)
    payload: dict[str, Any] = {
        "ok": ok,
        "plan_id": plan.plan_id if plan else None,
        "plan_status": plan.plan_status if plan else PlanStatus.NONE.value,
        "next_action": action,
        "next_action_hint": hint,
    }
    if error_code is not None:
        payload["error_code"] = error_code.value
    if message:
        payload["message"] = message
    payload.update({k: v for k, v in extra.items() if v is not None})
    # Every hint that used to say "call with task_id=3" now says "use the task_id in
    # next_task". That is only safe if the field is guaranteed to be there whenever the
    # instruction points at it - a dangling reference is worse than the literal was. So
    # the referent is attached here, in the one place every response passes through,
    # rather than trusted to each of the fifteen call sites.
    if (
        action == NextAction.CALL_UPDATE_TASK_PROGRESS.value
        and plan is not None
        and "next_task" not in payload
    ):
        nxt = plan.next_task_brief()
        if nxt is not None:
            payload["next_task"] = nxt
    if notes:
        # Surfaced so the model can learn the correct shape, but never as an error.
        payload["input_notes"] = notes
    return payload


def error(
    plan: Plan | None,
    code: ErrorCode,
    message: str,
    *,
    notes: list[str] | None = None,
    qualify: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    return build(
        plan, ok=False, error_code=code, message=message, notes=notes, qualify=qualify, **extra
    )


def render_completion_report(plan: Plan, plan_summary: str | None = None) -> str:
    """Per-task evidence for the human to verify before a plan may be called complete.

    The server cannot see whether real work happened - it only sees tool calls. Showing
    the human what the agent claims it did, task by task, is the only thing that catches
    a model that marked everything DONE with invented result_log text.
    """
    lines = ["완료 보고 - 이 계획을 종료하기 전에 확인해 주시기 바랍니다."]
    if plan.goal:
        lines.append(f"목표: {plan.goal}")
    if plan_summary:
        lines.append("")
        lines.append(plan_summary.strip())
    lines.append("")
    for task in plan.tasks:
        lines.append(f"{task.task_id}. {task.title}")
        # A task the human sent back last round. Showing what they asked for, and what
        # it used to say, is what lets them check the request in one line instead of
        # re-reading the whole report.
        if task.revision_note:
            lines.append(f"   ↻ 요청하신 내용: {task.revision_note}")
        previous = (task.previous_result_log or "").strip()
        if previous:
            lines.append(f"   이전 결과: {previous}")
        evidence = (task.result_log or "").strip()
        lines.append(f"   -> {evidence}" if evidence else "   -> (증거 기록 없음)")
    lines.append("")
    lines.append(
        f"이 에이전트는 {len(plan.tasks)}개 태스크의 완료를 보고했습니다. "
        "실제로 완료되었습니까? (승인 / 수정 요청 / 거절)"
    )
    return "\n".join(lines)


def render_halt_for_user(plan: Plan, reason: str, draft: list[str], on_page: bool) -> str:
    """What the human reads when the circuit breaker has stopped an agent.

    Says why it stopped, what the agent was last weighing, and what is on the table -
    so the person can decide in one read whether the draft is good enough, whether the
    agent needs a direction, or whether to stop.
    """
    lines = ["에이전트 반복 감지 - 이 계획을 일시 정지했습니다."]
    if plan.goal:
        lines.append(f"목표: {plan.goal}")
    lines.append(f"멈춘 이유: {reason}")
    thought = plan.last_thought().strip()
    if thought:
        lines.append(f"에이전트의 마지막 생각: {thought}")
    if draft:
        lines.append("")
        lines.append("현재 초안:")
        lines.extend(f"{i}. {title}" for i, title in enumerate(draft, start=1))
    lines.append("")
    if on_page:
        choices = "[이 초안으로 승인] / [계속 진행] / [취소]" if draft else "[계속 진행] / [취소]"
        lines.append(f"승인 페이지에서 {choices} 중 하나를 선택해 주십시오.")
    else:
        choices = "승인(이 초안 사용) / 계속 / 취소" if draft else "계속 / 취소"
        lines.append(f"어떻게 진행할까요? ({choices}) 계속하실 경우 방향을 함께 알려 주셔도 됩니다.")
    return "\n".join(lines)


def render_plan_for_user(plan: Plan, plan_summary: str | None = None) -> str:
    """Pre-rendered approval block. The model only has to echo this string, which is the
    single most reliable operation a weak model can perform.

    A task the human flagged and the model then rewrote is marked, with its old wording
    underneath. Re-approving a revision should be a matter of reading the lines that
    changed, not the whole plan again.
    """
    lines = ["계획 승인 요청"]
    if plan.goal:
        # A corrected goal is shown as corrected. The human who said "that is not what I
        # meant" needs to see that the system took it, not just a goal line that silently
        # differs from the one they read last time.
        if plan.goal_history:
            lines.append(f"목표(수정됨): {plan.goal}")
            lines.append(f"   최초 목표: {plan.original_goal}")
        else:
            lines.append(f"목표: {plan.goal}")
    if plan_summary:
        lines.append("")
        lines.append(plan_summary.strip())
    lines.append("")
    revised = 0
    for task in plan.tasks:
        mark = "↻ " if task.revision_note else ""
        lines.append(f"{mark}{task.task_id}. {task.title}")
        if task.previous_title:
            lines.append(f"   이전: {task.previous_title}")
        if task.revision_note:
            revised += 1
            lines.append(f"   요청하신 내용: {task.revision_note}")
    lines.append("")
    if revised:
        lines.append(f"↻ 표시된 {revised}개 태스크만 수정했습니다. 나머지는 그대로입니다.")
    lines.append("이 계획을 승인합니까? (승인 / 수정 요청 / 거절)")
    return "\n".join(lines)
