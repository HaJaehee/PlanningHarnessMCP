"""Tool schemas exactly as advertised to the LLM (Phase 1 blueprint).

Enum values are pulled from `models.py` so the advertised schema and the runtime
validator cannot drift apart. Descriptions are written for a weak model: every
parameter carries a concrete Example, and every tool description states the
protocol position ("STEP 1", "STEP 2", ...) explicitly.
"""

from __future__ import annotations

from typing import Any

from .models import Decision, TaskStatus

# The first line every profile shares. "Plan before answering ANY request" used to sit
# here, and it is the rule a thinking model obeyed into a loop (D25): after the last
# task the server says ANSWER_USER, the rule says plan first, and the model resolves the
# conflict by planning the same goal again. A new request starts a plan; an answer the
# server asks for is not a new request.
_WHEN_TO_PLAN = (
    "Call this first for each NEW user request. When a response says next_action = "
    "ANSWER_USER, write the answer instead - that is not a new request."
)

PLAN_AND_THINK_DESCRIPTION = f"""STEP 1 - PLAN BEFORE YOU ACT.
{_WHEN_TO_PLAN}

HOW TO USE:
- One call per thinking step, starting at step_number = 1. Send the same goal each time.
- When your task breakdown is ready, send need_more_thinking = false with task_list.
  It does not need to be perfect: the user reviews it before anything runs.
- Each response says how many thinking steps are left. When none are left, the server
  sends your latest task_list to the user as it stands.

IF THE USER COMMENTED ON PARTICULAR TASKS:
The server names them. Send task_updates instead of task_list, rewriting only those
tasks. Every other task was already accepted - leave it alone.

Do not execute anything or answer the user while planning."""

# For a model that already reasons inside its own thinking block. Asking it to think a
# second time, out loud, one step per call - and inviting it to revise steps and raise
# its step count - is what turned its habit of checking once more into an endless loop.
# It records the result of the thinking it has already done, once, and the human's
# review takes the place of the verification it would otherwise keep repeating.
PLAN_AND_THINK_DESCRIPTION_REASONING = f"""STEP 1 - RECORD YOUR PLAN.
{_WHEN_TO_PLAN}

You have already reasoned about the request. Record the result in ONE call:
goal + task_list, with need_more_thinking = false.

Do not re-check the plan here. The user reviews it on the approval page before
anything runs - that review is the verification step.

If the user commented on particular tasks, the server names them: send task_updates
instead of task_list, rewriting only those tasks."""

# Appended to the reasoning description when alternatives are on (2.0.0). The
# self-verification loop of D25 is mostly a model oscillating between two ways of doing
# one task; this hands that indecision to the person whose preference decides it.
_TORN_REASONING = """
If you are torn between two ways of doing a task, do not reconsider. Put the one you
prefer in task_list and the other in alternatives. The user picks."""

# request_user_approval reads differently in each approval mode, and a description that
# describes the wrong one is a contradiction the model has to reason its way out of.
# Before 1.16 there was one text for all of them: "ASK_USER, then STOP" and "after the
# user replies in chat, report APPROVED", while the default chunked mode's responses
# said "call again at once, write nothing" and refused any APPROVED the model sent. A
# thinking model weighs the two against each other on every call.
_APPROVAL_COMMON = (
    "Use the same call after the last task is DONE, so the user can check the results."
)

REQUEST_USER_APPROVAL_DESCRIPTION = f"""STEP 2 - USER APPROVAL.
Call with decision = "ASK_USER" and a short plan_summary. The plan appears on the
user's approval page and this call waits while they decide.
- error_code APPROVAL_PENDING means the user is still deciding: call again at once,
  the same way, and write nothing in between.
- When the user decides, plan_status and next_action in the response tell you the
  result. Follow them.
- If the response contains display_to_user, show it to the user and end your turn.
{_APPROVAL_COMMON}
Only the user decides. Never send APPROVED, REJECTED or REVISE yourself."""

REQUEST_USER_APPROVAL_DESCRIPTION_RETURN = f"""STEP 2 - USER APPROVAL.
Call with decision = "ASK_USER" and a short plan_summary. The plan appears on the
user's approval page. Show display_to_user to the user and end your turn.
When the user writes to you again, call this tool again with decision = "ASK_USER":
that collects their decision. Then follow next_action.
{_APPROVAL_COMMON}
Only the user decides. Never send APPROVED, REJECTED or REVISE yourself."""

# No approval page at all (PLANNING_MCP_BLOCKING_APPROVAL=false): the user answers in
# chat, so here - and only here - the model reports what they said.
REQUEST_USER_APPROVAL_DESCRIPTION_CHAT = f"""STEP 2 - USER APPROVAL.
Ask: call with decision = "ASK_USER" and a short plan_summary. Show display_to_user to
the user and end your turn.
Report: when the user replies, call this tool again with
  decision = "APPROVED" (they said yes / 승인),
  decision = "REJECTED" (they said no / 취소), or
  decision = "REVISE"   (they asked for changes - put their words in user_comment).
{_APPROVAL_COMMON}
Report only what the user actually said."""

UPDATE_TASK_PROGRESS_DESCRIPTION = """STEP 3 - EXECUTION TRACKING.
Handle exactly ONE task per call.

ALWAYS take task_id from the next_task field of the most recent server response.
next_task is the ONLY place the server publishes a task_id. Never reuse a task_id
from an earlier response and never count tasks yourself - earlier responses named
tasks that are already finished.

  1. Start the FIRST task: status = "IN_PROGRESS". Then do the work.
  2. Report it: status = "DONE" + a result_log saying what you actually produced.
  3. The server then starts the NEXT task for you and names it in next_task.
     Do that work, then report it "DONE" the same way.
     You do NOT send "IN_PROGRESS" again - just keep reporting DONE, one call per
     task, until the server tells you no tasks remain.
Use status = "FAILED" instead of "DONE" if the task did not work.
The response carries progress ("2/5 done") but not the task list. If you have lost
track of the plan, call get_current_plan - that is what it is for.

DONE IS ENFORCED. The server REFUSES a DONE for a task that:
  - is not the task currently in progress,
  - skips ahead while an earlier task is unfinished,
  - has no result_log describing the real outcome.
Never mark a task DONE before you actually did it. You must work through EVERY task in
order, one at a time. You are not finished until the server tells you so - keep going
while it says tasks remain.
If a task fails, set status = "FAILED" and explain in result_log - then follow the
next_action the server gives you back."""

# Used when PLANNING_MCP_AUTO_ADVANCE=false. The server then starts nothing on its own,
# so the description must ask for both calls or every DONE is refused.
UPDATE_TASK_PROGRESS_DESCRIPTION_MANUAL = """STEP 3 - EXECUTION TRACKING.
ALWAYS take task_id from the next_task field of the most recent server response.
next_task is the ONLY place the server publishes a task_id. Never reuse one from an
earlier response.

Call this tool TWICE for every task:
  1. BEFORE you start the task  -> status = "IN_PROGRESS"
  2. AFTER you finish the task  -> status = "DONE"  (or "FAILED" if it did not work)
Handle exactly ONE task per call. Never mark a task DONE before you actually did it.

DONE IS ENFORCED. The server REFUSES a DONE that:
  - was never marked IN_PROGRESS first,
  - skips ahead while an earlier task is unfinished,
  - has no result_log describing the real outcome.
You must therefore work through EVERY task in order, one at a time. You are not
finished until the server tells you so - keep going while it says tasks remain.
If a task fails, set status = "FAILED" and explain in result_log - then follow the
next_action the server gives you back."""

GET_CURRENT_PLAN_DESCRIPTION = """RECOVERY TOOL.
Call this when you are unsure what the plan is, which task you were on, or after a long
conversation. It returns YOUR plan and tells you exactly what to do next.
It changes nothing - it is always safe to call.

SEND YOUR OWN plan_id. Every response you have received carries a "plan_id" field -
send that exact value here and you will always get back your own plan, even if other
conversations are running plans at the same time.
Only send "current" if you genuinely do not know your plan_id (for example this is your
first call). "current" is a guess: when several plans are in flight the server cannot
tell which one is yours, so it answers with the list of plans and you have to call
again with the right plan_id."""


# Shared by the tools that act on an existing plan. Optional on purpose: with one plan
# in flight the server resolves it, so a model that never learns about plan_id behaves
# exactly as before. It is only needed when several sessions are planning at once, and
# then the server puts the value straight into next_action_hint.
_PLAN_ID_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "OPTIONAL. Leave this out unless the server asks for it. If several plans are "
        "active at the same time, set it to the plan_id this conversation has been "
        'receiving in every response. Example: "plan_20260724_0002"'
    ),
}

PLAN_AND_THINK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": (
                "One sentence restating what the user ultimately wants. Repeat the SAME goal "
                "text on every step. Example: 'Summarize the Q3 sales report and email it to "
                "the team lead.' If the user CORRECTS the goal, keep sending the old text here "
                "and put the corrected one in revised_goal."
            ),
        },
        "revised_goal": {
            "type": "string",
            "description": (
                "OPTIONAL. Use ONLY when the user says the goal itself was wrong or has "
                "changed - for example 'no, I meant the Q4 report, not Q3'. Put the corrected "
                "goal here and leave 'goal' as the text you have been sending, so the server "
                "can find the plan and record the change. Do NOT use it to reword or "
                "paraphrase the same goal. Example: 'Summarize the Q4 sales report and email "
                "it to the team lead.'"
            ),
        },
        "thought": {
            "type": "string",
            "description": (
                "Your reasoning for this step, in one or two sentences. Example: 'I must "
                "first locate the Q3 report file before I can summarize it.'"
            ),
        },
        "step_number": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Which thinking step this is. Starts at 1 and increases by exactly 1 each call. "
                "Example: 2"
            ),
        },
        "total_steps": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "How many thinking steps you expect to need in total. Example: 3"
            ),
        },
        "need_more_thinking": {
            "type": "boolean",
            "description": (
                "false = task_list is ready (it does not need to be perfect - the user "
                "reviews it). true = you need another thinking step. Example: false"
            ),
        },
        "task_list": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "REQUIRED when need_more_thinking is false. A flat list of plain-text action "
                "items, in execution order. Plain strings only - do NOT send objects, do NOT add "
                "numbering, do NOT add status. The server assigns task_id automatically. "
                'Example: ["Locate the Q3 sales report file", "Extract the revenue table", '
                '"Write a 5-line summary", "Send the summary by email"]'
            ),
        },
        "task_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "The task the user commented on. Example: 3",
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "The rewritten task, answering the user's comment. Example: "
                            "'Copy the revenue table into the summary unchanged'"
                        ),
                    },
                },
                "required": ["task_id", "title"],
            },
            "description": (
                "ONLY use this when the server told you the user commented on specific tasks. "
                "Rewrite JUST those tasks; every task you do not list stays exactly as it is. "
                "Do NOT send task_list at the same time, and do NOT use this to add, delete or "
                "reorder tasks - that requires a full task_list. "
                'Example: [{"task_id": 3, "title": "Copy the revenue table in unchanged"}]'
            ),
        },
        "revises_step": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "OPTIONAL - normally left out. The step_number of an earlier step that this "
                "step replaces. Example: 2"
            ),
        },
        "plan_id": _PLAN_ID_PARAM,
    },
    "required": ["goal", "thought", "step_number", "total_steps", "need_more_thinking"],
}

# The reasoning profile advertises only what a one-call plan needs. step_number,
# total_steps and revises_step are left out on purpose - each is an invitation to plan
# about planning - but the server still accepts them, so a model that sends them anyway
# is not refused.
PLAN_AND_THINK_SCHEMA_REASONING: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": PLAN_AND_THINK_SCHEMA["properties"]["goal"],
        "revised_goal": PLAN_AND_THINK_SCHEMA["properties"]["revised_goal"],
        "task_list": PLAN_AND_THINK_SCHEMA["properties"]["task_list"],
        "need_more_thinking": {
            "type": "boolean",
            "description": (
                "Send false. Use true only if you genuinely cannot list the tasks yet: you "
                "get one more call, and after it your latest task_list goes to the user as "
                "it stands. Example: false"
            ),
        },
        "thought": {
            "type": "string",
            "description": (
                "OPTIONAL. One sentence on why this plan. Example: 'The report has to be "
                "found before it can be summarized.'"
            ),
        },
        "task_updates": PLAN_AND_THINK_SCHEMA["properties"]["task_updates"],
        "plan_id": _PLAN_ID_PARAM,
    },
    "required": ["goal", "need_more_thinking"],
}


def _alternatives_params(max_per_task: int, max_points: int) -> dict[str, Any]:
    """The two optional plan_and_think fields of 2.0.0, in the {task_id, ...} shape
    task_updates already taught the model."""
    return {
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "The task's number in task_list (first = 1). Example: 2",
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Another way to do that task. Example: 'Export the table to CSV "
                            "and total it with a script'"
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Its trade-off, short. Example: 'faster, loses formatting'",
                    },
                    "topic": {
                        "type": "string",
                        "description": (
                            "OPTIONAL. What is being chosen, in two to four words, in the "
                            "language you use with the user - shown as the heading of the "
                            "choice. Once per task is enough. Example: '집계 방식'"
                        ),
                    },
                },
                "required": ["task_id", "title"],
            },
            "description": (
                "OPTIONAL. Other ways to do a task, ONLY when the choice depends on the user's "
                "preference - not on facts you can check yourself. The task in task_list is "
                "your recommendation; the user picks one option on the approval page and you "
                f"are told which. At most {max_per_task} per task, {max_points} tasks per "
                'plan. Example: [{"task_id": 2, "title": "Export to CSV and total it with a '
                'script", "reason": "faster, loses formatting", "topic": "집계 방식"}]'
            ),
        },
        "recommended_reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "minimum": 1, "description": "Example: 2"},
                    "reason": {
                        "type": "string",
                        "description": "Example: 'keeps the report formatting'",
                    },
                },
                "required": ["task_id", "reason"],
            },
            "description": (
                "OPTIONAL. Why you recommend the task_list version of a task that has "
                'alternatives. Example: [{"task_id": 2, "reason": "keeps the report '
                'formatting"}]'
            ),
        },
    }


_CHOICES_PARAM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": (
        "OPTIONAL, with decision='APPROVED' only. The option the user picked in chat for each "
        "task that offered a choice, as a letter: A = your recommendation, B / C / D = the "
        'alternatives in order. Leave a task out to keep A. Example: {"2": "B"}'
    ),
}


def _decision_param(chat: bool) -> dict[str, Any]:
    if chat:
        text = (
            "ASK_USER = ask the user. APPROVED / REJECTED / REVISE = report what the user "
            'replied in chat - never what you expect. Example: "ASK_USER"'
        )
    else:
        text = (
            "Send ASK_USER. The user answers on the approval page, and the server refuses "
            "APPROVED / REJECTED / REVISE from you while their question is open. "
            'Example: "ASK_USER"'
        )
    return {"type": "string", "enum": [d.value for d in Decision], "description": text}


REQUEST_USER_APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": _decision_param(chat=False),
        "plan_summary": {
            "type": "string",
            "description": (
                "REQUIRED when decision is ASK_USER. A short human-readable summary of the plan "
                "you want approval for, written for a non-technical reader. Example: 'I will (1) "
                "find the Q3 report, (2) extract the revenue table, (3) write a 5-line summary, "
                "(4) email it to the team lead.'"
            ),
        },
        "user_comment": {
            "type": "string",
            "description": (
                "OPTIONAL. Copy the user's exact words here when decision is REVISE or REJECTED. "
                "Example: 'Do not send the email, just show me the summary.'"
            ),
        },
        "plan_id": _PLAN_ID_PARAM,
    },
    "required": ["decision"],
}

UPDATE_TASK_PROGRESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Copy this from next_task.task_id in the server's most recent response. That "
                "is the only task you may act on. One task per call. Example: 1"
            ),
        },
        "status": {
            "type": "string",
            "enum": [s.value for s in TaskStatus],
            "description": (
                "IN_PROGRESS = starting now. DONE = finished successfully. FAILED = could not "
                'finish. PENDING = reset back to not-started. Example: "IN_PROGRESS"'
            ),
        },
        "result_log": {
            "type": "string",
            "description": (
                "REQUIRED when status is DONE - the server rejects DONE without it. Write one "
                "or two sentences stating the CONCRETE outcome of the work you just did: what "
                "you found, where you saved it, or what you produced. 'done' / 'ok' / 'completed' "
                "is not acceptable. Example: 'Found the file at /reports/q3_sales.xlsx.'  or  "
                "'FAILED: no file matching q3 was found in /reports.'"
            ),
        },
        "plan_id": _PLAN_ID_PARAM,
    },
    "required": ["task_id", "status"],
}

GET_CURRENT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan_id": {
            "type": "string",
            "default": "current",
            "description": (
                "The plan you want. Send the plan_id YOUR conversation has been receiving in "
                'every response - that is how you get your own plan back. Example: '
                '"plan_20260724_0002". It also reads a plan that is already finished or '
                'cancelled. Send the exact text "current" ONLY if you do not know your '
                "plan_id yet; the server then guesses, and with several plans in flight it "
                "cannot guess and returns the list of plans instead."
            ),
        },
    },
    # Required-with-a-constant on purpose: weak models reliably emit {"plan_id":"current"}
    # but frequently emit malformed/empty arguments for zero-parameter tools.
    "required": ["plan_id"],
}


def build_tool_definitions(
    auto_advance: bool = True,
    model_profile: str = "standard",
    approval_mode: str = "chunked",
    blocking: bool = True,
    alternatives: bool = True,
    max_alternatives: int = 3,
    max_choice_points: int = 3,
) -> list[dict[str, Any]]:
    """The advertised tool list for a given configuration.

    Every description says only what is true of the running server. One that describes a
    different mode - STOP here, call again there - is a contradiction the model has to
    reason its way out of, and for a thinking model that reasoning is the loop (D25).
    """
    reasoning = model_profile == "reasoning"
    if not blocking:
        approval_text, chat = REQUEST_USER_APPROVAL_DESCRIPTION_CHAT, True
    elif approval_mode == "return":
        approval_text, chat = REQUEST_USER_APPROVAL_DESCRIPTION_RETURN, False
    else:
        approval_text, chat = REQUEST_USER_APPROVAL_DESCRIPTION, False
    approval_schema = dict(REQUEST_USER_APPROVAL_SCHEMA)
    approval_schema["properties"] = dict(REQUEST_USER_APPROVAL_SCHEMA["properties"])
    approval_schema["properties"]["decision"] = _decision_param(chat=chat)
    # Only in chat mode does the model relay what the user picked; with a page the user
    # picks there, so the field is not even offered (2.0.0).
    if chat and alternatives:
        approval_schema["properties"]["choices"] = _CHOICES_PARAM

    plan_text = PLAN_AND_THINK_DESCRIPTION_REASONING if reasoning else PLAN_AND_THINK_DESCRIPTION
    plan_schema = dict(PLAN_AND_THINK_SCHEMA_REASONING if reasoning else PLAN_AND_THINK_SCHEMA)
    if alternatives:
        plan_schema["properties"] = {
            **plan_schema["properties"],
            **_alternatives_params(max_alternatives, max_choice_points),
        }
        if reasoning:
            plan_text += _TORN_REASONING
    return [
        {
            "name": "plan_and_think",
            "description": plan_text,
            "inputSchema": plan_schema,
        },
        {
            "name": "request_user_approval",
            "description": approval_text,
            "inputSchema": approval_schema,
        },
        {
            "name": "update_task_progress",
            "description": UPDATE_TASK_PROGRESS_DESCRIPTION
            if auto_advance
            else UPDATE_TASK_PROGRESS_DESCRIPTION_MANUAL,
            "inputSchema": UPDATE_TASK_PROGRESS_SCHEMA,
        },
        {
            "name": "get_current_plan",
            "description": GET_CURRENT_PLAN_DESCRIPTION,
            "inputSchema": GET_CURRENT_PLAN_SCHEMA,
        },
    ]


TOOL_DEFINITIONS: list[dict[str, Any]] = build_tool_definitions()

TOOL_NAMES = tuple(t["name"] for t in TOOL_DEFINITIONS)

__all__ = [
    "TOOL_DEFINITIONS",
    "TOOL_NAMES",
    "build_tool_definitions",
]
