"""Transition rules and the single next_action resolver.

`resolve_next_action` is a pure function of (plan_status, task states, error_code) and
is the ONLY producer of `next_action`. If two code paths could emit different
instructions for the same state, a weak model receives contradictory orders and loops -
so every response goes through here.
"""

from __future__ import annotations

from .models import ErrorCode, NextAction, Plan, PlanStatus, TaskStatus

# ---------------------------------------------------------------------------
# Error-driven resolution: what the model must do to get unstuck.
# ---------------------------------------------------------------------------


def choice_note(task) -> str:
    """The human's pick, restated wherever the task is handed to the model (2.0.0).

    Said at the moment of execution, not only in the approval reply, for the reason
    _rework_suffix exists: a sentence the model saw once, turns ago, is one it has lost.
    Only the chosen option is ever named - the others are not repeated anywhere.
    """
    picked = task.chosen_option() if task is not None else None
    if picked is None or not task.chosen:
        return ""
    reason = f" ({picked['reason']})" if picked.get("reason") else ""
    return (
        f" The user chose this way of doing it over your recommendation{reason} - do it "
        "exactly this way."
    )


def _halt_action(plan: Plan) -> tuple[str, str]:
    """What a model does while the circuit breaker holds its plan.

    One instruction, no options, nothing to reconsider - the whole point of the halt is
    that the next decision is not the model's. Which instruction depends only on whether
    the human has been shown the halt yet, and on which channel they will answer in.
    """
    halt = plan.halt or {}
    if not halt.get("asked"):
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "The server paused this plan because the same step kept repeating. Do not "
            "retry the call you just made. Call request_user_approval with "
            "decision='ASK_USER' - the user decides how to continue. Write nothing else.",
        )
    if halt.get("channel") == "chat":
        return (
            NextAction.STOP_AND_WAIT_FOR_USER.value,
            "This plan is paused because a step kept repeating. Show display_to_user to "
            "the user and end your turn. When they reply, report their choice with "
            "request_user_approval: APPROVED (use the draft as it is), REVISE (continue - "
            "put their words in user_comment) or REJECTED (cancel).",
        )
    return (
        NextAction.STOP_AND_WAIT_FOR_USER.value,
        "This plan is paused because a step kept repeating, and the user decides on the "
        "approval page how it continues. Show display_to_user to the user and end your "
        "turn. When the user writes to you again, call request_user_approval with "
        "decision='ASK_USER' to pick up their decision.",
    )


def _error_action(plan: Plan | None, code: ErrorCode) -> tuple[str, str]:
    if code is ErrorCode.LOOP_HALTED:
        if plan is not None and plan.halt:
            return _halt_action(plan)
        # A loop that never resolved to a plan (the same routing error over and over)
        # has no plan to pause. The only safe instruction left is to hand back to the
        # user rather than make the same call a fifth time.
        return (
            NextAction.STOP_AND_WAIT_FOR_USER.value,
            "The server stopped this sequence because the same call kept failing. Do not "
            "repeat it. Show display_to_user to the user and end your turn - the user "
            "will tell you how to continue.",
        )
    if code is ErrorCode.PLAN_NOT_APPROVED:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "You cannot start tasks yet - the user has not approved the plan. "
            "Call request_user_approval with decision='ASK_USER'.",
        )
    if code is ErrorCode.APPROVAL_PENDING:
        if plan is not None and plan.halt:
            # Waiting on a halt card. There is no plan_summary to repeat here, and
            # telling a thinking model to send "the same plan_summary as before" when it
            # never sent one is one more thing for it to puzzle over.
            return (
                NextAction.CALL_REQUEST_USER_APPROVAL.value,
                "The user has not decided yet how this paused plan continues. Call "
                "request_user_approval again at once with decision='ASK_USER'. Do nothing "
                "else and write nothing meanwhile.",
            )
        # Two callers share this code, and the instruction is the same for both: a wait
        # chunk that ended with the request still on screen, and a model that tried to
        # decide on the user's behalf while it was. Saying "no approval has been given"
        # first matters more than saying what to call - a weak model that reads this as
        # a go-ahead is the exact failure the whole gate exists to prevent.
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "The user has NOT decided yet. No approval has been given and nothing is "
            "unlocked. Do not execute anything, do not answer the user, do not ask them "
            "anything and do not print the plan again. Call request_user_approval "
            "immediately with decision='ASK_USER' and the same plan_summary as before. "
            "Repeat that until this response changes. Output no text meanwhile.",
        )
    if code is ErrorCode.PLAN_NOT_READY:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "The plan has no task list yet, so there is nothing to approve. Call "
            "plan_and_think with need_more_thinking=false and a non-empty task_list first.",
        )
    if code is ErrorCode.PLAN_BLOCKED:
        failed = plan.first_failed_task() if plan else None
        which = f"The task '{failed.title}' failed. " if failed else ""
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            f"{which}You may NOT continue to another task. Call plan_and_think to re-plan "
            "around this failure, then request approval again.",
        )
    if code is ErrorCode.PLAN_CANCELLED:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "This plan was cancelled by the user. Do not execute any of its tasks. If the user "
            "wants something else, start a new plan with plan_and_think at step_number=1.",
        )
    if code is ErrorCode.NO_ACTIVE_PLAN:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "There is no active plan. Start one by calling plan_and_think with step_number=1.",
        )
    if code is ErrorCode.TASK_NOT_FOUND:
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            "That task_id does not exist in this plan. Do not guess task numbers. The "
            "next_task field of this response holds the only task_id you may send now - "
            "call update_task_progress with exactly that value.",
        )
    if code is ErrorCode.MISSING_TASK_LIST:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "Send the same step again with a non-empty task_list. Example: "
            'task_list=["Locate the file", "Extract the table", "Write the summary"].',
        )
    if code is ErrorCode.MISSING_PLAN_SUMMARY:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "Call request_user_approval again with decision='ASK_USER' AND a plan_summary "
            "written in plain language for the user.",
        )
    if code is ErrorCode.APPROVAL_NOT_REQUESTED:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "The CURRENT version of this plan was never shown to the user - it may have "
            "been revised or replaced since you last asked. Call request_user_approval "
            "with decision='ASK_USER' and a plan_summary of the current plan, show it to "
            "the user, and wait for their answer. Do not reuse an old approval.",
        )
    if code is ErrorCode.APPROVAL_EXPIRED:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "This plan's approval has expired - it was approved a while ago and left "
            "idle, so it no longer authorizes execution. Call request_user_approval "
            "with decision='ASK_USER' and get the user to approve it again now.",
        )
    if code is ErrorCode.PLAN_AMBIGUOUS:
        return (
            NextAction.CALL_GET_CURRENT_PLAN.value,
            "Several plans are active at once, so the server cannot tell which one you "
            "mean. Repeat your call with plan_id set to the plan you are working on - "
            "it is the plan_id this conversation has been receiving in every response. "
            "Call get_current_plan with plan_id='current' if you need the list.",
        )
    if code is ErrorCode.GOAL_NOT_MATCHED:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "You said you are continuing (step_number > 1), but no active plan has this "
            "goal. Do not start a new plan by drifting the goal. Look at active_plans "
            "below: if one of them is what you are working on, call plan_and_think again "
            "with its EXACT goal text (or its plan_id). If the USER actually corrected "
            "the goal, call again with that plan's EXACT goal text plus "
            "revised_goal='<the corrected goal>' - the server will update it and keep "
            "the old one on record. If this really is a brand-new plan, call again with "
            "step_number=1.",
        )
    if code is ErrorCode.TASK_NOT_STARTED:
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            "You cannot mark a task DONE that you never started. Call "
            "update_task_progress with the task_id from next_task below and "
            "status='IN_PROGRESS' first, then actually do the work, and only then "
            "report DONE.",
        )
    if code is ErrorCode.TASK_OUT_OF_ORDER:
        task = plan.current_task() if plan else None
        which = f" It is '{task.title}'." if task else ""
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            "An earlier task is still unfinished, so this one cannot be complete. Work "
            "through the tasks in order: next_task below names the one to start now."
            f"{which} Call update_task_progress with its task_id and "
            "status='IN_PROGRESS'.",
        )
    if code is ErrorCode.MISSING_RESULT_LOG:
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            "DONE was refused because result_log did not describe what you actually did. "
            "Send the same call again with a result_log of at least one full sentence "
            "stating the concrete outcome - for example what you found, where you saved "
            "it, or what you produced. 'done' or 'ok' is not acceptable evidence.",
        )
    if code is ErrorCode.REWORK_NOT_DONE:
        task = plan.current_task() if plan else None
        asked = f' They said: "{task.revision_note}".' if task and task.revision_note else ""
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            "You reported the same result_log this task already had - the one the user "
            f"rejected.{asked} Redoing the work means the outcome CHANGES. Do the work "
            "again so it answers what they said, then call update_task_progress with the "
            "task_id from next_task below, status='DONE' and a result_log describing the "
            "NEW outcome. If you believe the original was already correct, say so to the "
            "user instead of reporting it as redone.",
        )
    if code is ErrorCode.COMPLETION_PENDING:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "Every task is already marked DONE and the plan is waiting for the user to "
            "verify the completion report. Do not change tasks now. Call "
            "request_user_approval with decision='ASK_USER'.",
        )
    if code is ErrorCode.REVISION_NOT_REQUESTED:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "You sent task_updates, but the user has not asked for changes to specific "
            "tasks. task_updates may ONLY be used to answer a per-task revision request. "
            "If you are re-planning, send a full task_list instead.",
        )
    if code is ErrorCode.REVISION_INCOMPLETE:
        targets = plan.revision_targets() if plan else {}
        which = ", ".join(str(t) for t in sorted(targets)) or "the flagged tasks"
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            f"None of the tasks the user commented on were rewritten. Send task_updates "
            f"containing task_id {which} - those are the only tasks you may change.",
        )
    if code is ErrorCode.INVALID_STATUS:
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            "status must be exactly one of: PENDING, IN_PROGRESS, DONE, FAILED. Call the tool "
            "again with a valid status.",
        )
    if code is ErrorCode.INVALID_DECISION:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "decision must be exactly one of: ASK_USER, APPROVED, REJECTED, REVISE. Use "
            "ASK_USER to ask the human, and the others only to report what the human "
            "actually replied.",
        )
    if code is ErrorCode.INVALID_STEP:
        last = plan.last_step_number() if plan else 0
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            f"revises_step must point at an existing step between 1 and {last}. Call "
            "plan_and_think again with a valid revises_step, or omit it.",
        )
    return (
        NextAction.CALL_GET_CURRENT_PLAN.value,
        "Something went wrong on the server. Call get_current_plan with plan_id='current' to "
        "resync, then continue from the task it reports.",
    )


# ---------------------------------------------------------------------------
# Status-driven resolution: the normal path.
# ---------------------------------------------------------------------------


def _status_action(plan: Plan | None) -> tuple[str, str]:
    action, hint = _status_action_inner(plan)
    # What the human said when they lifted a halt leads every hint until the plan next
    # moves on. Only for the states where the model is doing the work - in a waiting
    # state the human is the one acting.
    if (
        plan is not None
        and plan.guidance
        and not plan.halt
        and plan.status in (PlanStatus.DRAFTING, PlanStatus.APPROVED, PlanStatus.IN_EXECUTION)
    ):
        hint = f'The user said: "{plan.guidance}". {hint}'
    return action, hint


def _drafting_hint(plan: Plan) -> str:
    """Continue-or-finalize, with finalizing put first.

    The old hint was a single instruction - "call plan_and_think again with
    step_number=N" - on every thinking step, with no word that stopping was allowed. For
    a model whose habit is to verify once more (D25), that made one more step the
    cheapest continuation forever. The exit is now named first, the bar for using it is
    stated ("it does not need to be perfect - the user reviews it"), and the remaining
    budget is counted down.
    """
    nxt = plan.last_step_number() + 1
    left = plan.thinking_steps_left()
    if left is not None and left <= 1:
        return (
            "You have one thinking step left. Make it your final one: call plan_and_think "
            "with need_more_thinking=false and your task_list. The plan does not need to "
            "be perfect - the user reviews it before anything runs."
        )
    budget = f" ({left} thinking steps left)" if left is not None else ""
    return (
        "If your task breakdown is ready, call plan_and_think now with "
        "need_more_thinking=false and task_list - it does not need to be perfect, the "
        "user reviews it before anything runs. Otherwise call plan_and_think with "
        f"step_number={nxt} and the same goal{budget}."
    )


def _status_action_inner(plan: Plan | None) -> tuple[str, str]:
    if plan is None or plan.status is PlanStatus.NONE:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "There is no active plan. Start one by calling plan_and_think with step_number=1.",
        )

    if plan.halt:
        return _halt_action(plan)

    status = plan.status

    if status is PlanStatus.DRAFTING:
        # A targeted revision is still DRAFTING - locked, nothing executable - but the
        # instruction is completely different: rewrite the flagged tasks, leave the rest
        # alone. Handing a weak model the literal argument to send is what makes this
        # usable at all; it copies the hint far more reliably than it composes one.
        targets = plan.revision_targets()
        if targets:
            listed = "; ".join(
                f"task {tid}: \"{comment}\"" for tid, comment in sorted(targets.items())
            )
            example = ", ".join(
                '{"task_id": %d, "title": "<the rewritten task>"}' % tid
                for tid in sorted(targets)
            )
            keep = [t.task_id for t in plan.tasks if t.task_id not in targets]
            untouched = (
                " Task(s) " + ", ".join(str(k) for k in keep) + " were accepted by the "
                "user as they stand - do NOT change, renumber, reorder or resend them."
                if keep
                else ""
            )
            return (
                NextAction.CALL_PLAN_AND_THINK.value,
                f"The user commented on specific tasks ({listed}). Call plan_and_think "
                "with need_more_thinking=false and "
                f"task_updates=[{example}] - do NOT send task_list.{untouched}",
            )
        return NextAction.CALL_PLAN_AND_THINK.value, _drafting_hint(plan)

    if status is PlanStatus.AWAITING_APPROVAL:
        if plan.approval.requested_at and not plan.approval.decision:
            return (
                NextAction.STOP_AND_WAIT_FOR_USER.value,
                "STOP NOW. Do not call any other tool. Show the display_to_user text below to "
                "the user and ask: '이 계획을 승인합니까? (승인 / 수정 요청 / 거절)'. "
                "Wait for their reply in the next message.",
            )
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "Plan is drafted but LOCKED. You must now call request_user_approval with "
            "decision='ASK_USER'. You are NOT allowed to start any task yet.",
        )

    if status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
        task = plan.current_task()
        if task is None:
            return (
                NextAction.ANSWER_USER.value,
                "All tasks are complete. Now write the final answer to the user, summarizing "
                "the result_log of each task.",
            )
        # Spelling out the outstanding work in every single response is the counter to a
        # model that quietly stops after one or two tasks and declares the plan finished.
        left = plan.remaining_tasks()
        # The count stays; the id list does not. Naming the outstanding ids here put a
        # second set of task numbers in the same sentence as the instruction, which is
        # the ambiguity next_task exists to remove. The pressure this line applies -
        # "you are not finished" - is carried entirely by the count.
        outstanding = (
            f" {len(left)} of {len(plan.tasks)} tasks are still unfinished. Do NOT tell "
            "the user the work is finished until every task is DONE."
        )
        # A task the human reopened from the completion report. The plan is not what is
        # wrong, so the generic "next task" hint would send a weak model off to re-plan
        # or to redo everything; what it needs is the sentence the human actually wrote,
        # and an explicit instruction to leave the accepted work alone.
        if task.revision_note:
            keep = (
                " The other tasks were accepted by the user and keep the results you "
                "already produced - do NOT redo, rewrite or re-report them."
                if any(t.status == TaskStatus.DONE.value for t in plan.tasks)
                else ""
            )
            step = (
                "It is already IN_PROGRESS: do the work now and call "
                "update_task_progress with the task_id from next_task below, "
                "status='DONE' and a result_log describing the NEW outcome."
                if task.status == TaskStatus.IN_PROGRESS.value
                else "Call update_task_progress with the task_id from next_task below "
                "and status='IN_PROGRESS'."
            )
            return (
                NextAction.CALL_UPDATE_TASK_PROGRESS.value,
                f"The user reviewed the finished work and sent one task back to be done "
                f"AGAIN: \"{task.revision_note}\". That task is '{task.title}', named in "
                f"next_task below. Do not re-plan and do not ask for approval - the plan "
                f"is unchanged and still approved. Redo ONLY that task so that it answers "
                f"what they said. {step}{keep}{outstanding}",
            )
        if task.status == TaskStatus.IN_PROGRESS.value:
            return (
                NextAction.CALL_UPDATE_TASK_PROGRESS.value,
                f"The task in next_task below ('{task.title}') is in progress."
                + choice_note(task)
                + " When it is finished, call update_task_progress with its task_id, "
                "status='DONE' and a result_log describing what you actually did."
                + outstanding,
            )
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            f"Next task is '{task.title}' - next_task below holds its task_id."
            + choice_note(task)
            + " Call update_task_progress with that task_id and status='IN_PROGRESS'."
            + outstanding,
        )

    if status is PlanStatus.AWAITING_COMPLETION:
        if plan.approval.requested_at and not plan.approval.decision:
            return (
                NextAction.STOP_AND_WAIT_FOR_USER.value,
                "STOP NOW. Show the completion report below to the user and ask them to "
                "confirm the work is really finished. Do not call any other tool and do "
                "not claim success yourself. Wait for their reply.",
            )
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "Every task is marked DONE, but the plan is NOT complete until the user has "
            "verified it. Call request_user_approval with decision='ASK_USER' and a "
            "plan_summary of what you actually produced, task by task.",
        )

    if status is PlanStatus.BLOCKED:
        return _error_action(plan, ErrorCode.PLAN_BLOCKED)

    if status is PlanStatus.COMPLETED:
        return (
            NextAction.ANSWER_USER.value,
            "All tasks are complete. Now write the final answer to the user, summarizing the "
            "result_log of each task.",
        )

    if status is PlanStatus.CANCELLED:
        return (
            NextAction.ANSWER_USER.value,
            "The plan is cancelled. Tell the user it was cancelled and ask what they want to do "
            "instead. Do not execute anything.",
        )

    return (
        NextAction.CALL_GET_CURRENT_PLAN.value,
        "Call get_current_plan with plan_id='current' to find out what to do next.",
    )


def resolve_next_action(
    plan: Plan | None,
    error_code: ErrorCode | None = None,
    qualify: bool = False,
) -> tuple[str, str]:
    """The single source of truth for (next_action, next_action_hint).

    `qualify` is set when more than one plan is active. The hint then names the
    plan_id in the call it tells the model to make, so a model that copies the hint
    verbatim - which weak ones do - routes to the right plan without understanding why.
    """
    if error_code is not None:
        action, hint = _error_action(plan, error_code)
    else:
        action, hint = _status_action(plan)
    if qualify and plan is not None and action.startswith("CALL_"):
        hint = (
            f"{hint} Several plans are active, so include plan_id='{plan.plan_id}' "
            "in this call."
        )
    return action, hint


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def execution_guard(plan: Plan | None, autoapprove: bool = False) -> ErrorCode | None:
    """May the model touch task state right now? Returns an error code if not.

    This is the ENFORCEMENT half of the HITL gate. The instructional half (telling the
    model to stop and wait) can be ignored by a weak model; this one cannot, because
    progress is only real once the server records it.
    """
    if plan is None:
        return ErrorCode.NO_ACTIVE_PLAN
    if plan.halt:
        return ErrorCode.LOOP_HALTED
    status = plan.status
    if status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
        return None
    if status is PlanStatus.AWAITING_COMPLETION:
        return ErrorCode.COMPLETION_PENDING
    if status is PlanStatus.BLOCKED:
        return ErrorCode.PLAN_BLOCKED
    if status is PlanStatus.CANCELLED:
        return ErrorCode.PLAN_CANCELLED
    if status is PlanStatus.COMPLETED:
        return ErrorCode.NO_ACTIVE_PLAN
    if autoapprove and status is PlanStatus.AWAITING_APPROVAL:
        return None  # testing escape hatch only; server.py logs a loud warning
    return ErrorCode.PLAN_NOT_APPROVED


def can_start_task(plan: Plan, task_id: int) -> bool:
    """A task may start only once every lower-numbered task has finished."""
    for task in plan.tasks:
        if task.task_id >= task_id:
            break
        if task.status in (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value):
            return False
    return True
