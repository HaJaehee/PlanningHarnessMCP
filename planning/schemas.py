"""Tool schemas exactly as advertised to the LLM (Phase 1 blueprint).

Enum values are pulled from `models.py` so the advertised schema and the runtime
validator cannot drift apart. Descriptions are written for a weak model: every
parameter carries a concrete Example, and every tool description states the
protocol position ("STEP 1", "STEP 2", ...) explicitly.

How the text is laid out (3.0.0 trim). All of it is sent with every request, so each
rule is said once, and where it is said is not arbitrary:
- a rule about *what to do* lives in the tool description. Some clients drop or
  shorten parameter descriptions when they relay a schema (matrix E16); the tool
  description is the part that always arrives.
- a parameter description says only the shape of the value and gives one example. For
  an array of objects the example is on the array - it shows the whole shape, which is
  what a weak model copies - and the nested fields carry a few words, not a second
  example.
What the trim took out was repetition, never an example, an enum value or a rule.
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

# Appended when local repair is on (3.0.0). A failed task is repaired where it stands,
# through the same task_updates the per-task review already taught the model.
_REPAIR_STANDARD = """

IF A TASK FAILED:
The server names it. Send task_updates rewriting that task with another way to do it
(and any later task that has to change). Finished tasks keep their results."""

_REPAIR_REASONING = """
If a task failed, the server names it: send task_updates for that task with another way
to do it. Finished tasks keep their results."""

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

# Appended to the reasoning description when done_when is on (3.0.0). 2.0 gave the
# model's indecision between two ways somewhere to go; this does the same for its urge
# to check once more - the check is written down and happens when the task is done.
_CHECK_LATER_REASONING = """
If you want to double-check a task, do not re-check it now. Write the check in
done_when; it is checked when the task is done."""

# Appended to update_task_progress when the matching feature is on, so the description
# never promises a check the running server does not make.
_DONE_WHEN_EXEC = """

If next_task has done_when, result_log must show it was met, with the actual values,
names or path. Repeating the done_when sentence is refused."""

_FILES_EXEC = """

List the files the task created or changed in files: the server looks for each one and
refuses DONE for a file it cannot find."""

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

# --- the server asks at the transition (3.1.0, PLANNING_MCP_AUTO_ASK) -------------
# With it on - the default - the final plan_and_think call and the last DONE are the
# approval requests, so three things in these texts would be false: that the plan is
# "reviewed" some time later, that request_user_approval is a step, and that the model
# writes a plan_summary. Each is replaced rather than explained: the text gets shorter,
# and the rule it carried - "now ask" - is one the model no longer has to keep.
_REVIEWED_LATER = (
    "  It does not need to be perfect: the user reviews it before anything runs."
)
_REVIEWED_IN_THIS_CALL = (
    "  It does not need to be perfect: that call shows it to the user, who reviews it\n"
    "  before anything runs."
)
_REVIEWED_LATER_REASONING = (
    "Do not re-check the plan here. The user reviews it on the approval page before\n"
    "anything runs - that review is the verification step."
)
_REVIEWED_IN_THIS_CALL_REASONING = (
    "Do not re-check the plan here. That call shows it to the user, who reviews it before\n"
    "anything runs - that review is the verification step."
)

_ASKS_ITSELF = (
    "The server asks the user itself - when you send your task list, and when the last "
    "task\nis DONE"
)

REQUEST_USER_APPROVAL_DESCRIPTION_AUTO = f"""WAITING FOR THE USER.
{_ASKS_ITSELF} - and that call waits while they decide. Call this tool only when a
response tells you to, with decision = "ASK_USER":
- error_code APPROVAL_PENDING means the user is still deciding: call at once, and again
  each time you get it. Write nothing in between.
- When the user decides, plan_status and next_action in the response tell you the
  result. Follow them.
- If the response contains display_to_user, show it to the user and end your turn.
Only the user decides. Never send APPROVED, REJECTED or REVISE yourself."""

REQUEST_USER_APPROVAL_DESCRIPTION_AUTO_RETURN = f"""WAITING FOR THE USER.
{_ASKS_ITSELF}. That response carries display_to_user: show it to the user
and end your turn.
When the user writes to you again, call this tool with decision = "ASK_USER": that
collects their decision. Then follow next_action.
Only the user decides. Never send APPROVED, REJECTED or REVISE yourself."""

REQUEST_USER_APPROVAL_DESCRIPTION_AUTO_CHAT = f"""REPORT THE USER'S REPLY.
{_ASKS_ITSELF}. That response carries display_to_user: show it to the user
and end your turn.
When the user replies, call this tool with
  decision = "APPROVED" (they said yes / 승인),
  decision = "REJECTED" (they said no / 취소), or
  decision = "REVISE"   (they asked for changes - put their words in user_comment).
decision = "ASK_USER" shows the plan or the results to the user again.
Report only what the user actually said."""

# Each rule once (3.0.0 trim). The longer text this replaces said "one task per call"
# three times, the FAILED rule twice, and the result_log requirement in three places -
# a habit that grew one field failure at a time (D14, D16). The rules themselves are all
# still here: where the task_id comes from, the three steps, FAILED, the three refusals.
UPDATE_TASK_PROGRESS_DESCRIPTION = """STEP 3 - EXECUTION TRACKING. One task per call.

Take task_id from next_task in the most recent server response. That is the ONLY place
the server publishes one: never reuse an earlier task_id and never count tasks yourself.

  1. Start the FIRST task: status = "IN_PROGRESS". Then do the work.
  2. Report it: status = "DONE" + a result_log saying what you actually produced.
  3. The server then starts the NEXT task for you and names it in next_task. Do that
     work and report it "DONE" the same way. You do NOT send "IN_PROGRESS" again - keep
     going, one call per task, until the server tells you no tasks remain.
If a task did not work, send status = "FAILED" with the reason in result_log instead of
"DONE", then follow next_action.

The server REFUSES a DONE for a task that is not the one in progress, that skips an
unfinished earlier task, or that has no result_log describing the real outcome. Never
mark a task DONE before you actually did it.
The response carries progress ("2/5 done"), not the task list. If you lose track of the
plan, call get_current_plan."""

# Used when PLANNING_MCP_AUTO_ADVANCE=false. The server then starts nothing on its own,
# so the description must ask for both calls or every DONE is refused.
UPDATE_TASK_PROGRESS_DESCRIPTION_MANUAL = """STEP 3 - EXECUTION TRACKING. One task per call.

Take task_id from next_task in the most recent server response. That is the ONLY place
the server publishes one: never reuse an earlier task_id.

Call this tool TWICE for every task:
  1. BEFORE you start the task  -> status = "IN_PROGRESS"
  2. AFTER you finish the task  -> status = "DONE" + a result_log saying what you
     actually produced
If a task did not work, send status = "FAILED" with the reason in result_log instead of
"DONE", then follow next_action. Keep going, in order, until the server tells you no
tasks remain.

The server REFUSES a DONE that was never marked IN_PROGRESS first, that skips an
unfinished earlier task, or that has no result_log describing the real outcome. Never
mark a task DONE before you actually did it."""

# The description and the plan_id parameter used to explain "send your own plan_id,
# 'current' is a guess" twice over. Said once here; the parameter keeps the example.
# The order still matters (1.15.1): the plan_id first, "current" as the fallback.
GET_CURRENT_PLAN_DESCRIPTION = """RECOVERY TOOL. Call it when you are unsure what the plan is or which task you were on.
It returns YOUR plan and says what to do next. It changes nothing - always safe to call.

Send the plan_id that every earlier response carried: that always returns your own plan.
Send "current" only if you do not know it yet. "current" is a guess - with several plans
in flight the server answers with the list of plans, and you call again with the right
plan_id."""


# Shared by the tools that act on an existing plan. Optional on purpose: with one plan
# in flight the server resolves it, so a model that never learns about plan_id behaves
# exactly as before. It is only needed when several sessions are planning at once, and
# then the server puts the value straight into next_action_hint.
_PLAN_ID_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "OPTIONAL. Leave it out unless the server asks for it; then send the plan_id "
        'from earlier responses. Example: "plan_20260724_0002"'
    ),
}

PLAN_AND_THINK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": (
                "What the user ultimately wants, in one sentence. Send the SAME text on every "
                "call. Example: 'Summarize the Q3 sales report and email it to the team lead.' "
                "If the user CORRECTS the goal, keep the old text here and put the new one in "
                "revised_goal."
            ),
        },
        "revised_goal": {
            "type": "string",
            "description": (
                "OPTIONAL. The corrected goal, ONLY when the user says the goal itself was "
                "wrong ('no, I meant the Q4 report, not Q3'). Do NOT use it to reword the same "
                "goal. Example: 'Summarize the Q4 sales report and email it to the team lead.'"
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
                "Which thinking step this is: 1, then one more each call. Example: 2"
            ),
        },
        "total_steps": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "How many thinking steps you expect in total. Example: 3"
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
                "REQUIRED when need_more_thinking is false. Plain-text action items in "
                "execution order. Strings only - no objects, numbering or status; the server "
                "assigns task_id. "
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
                        "description": "The task to rewrite.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Its new wording.",
                    },
                },
                "required": ["task_id", "title"],
            },
            "description": (
                "ONLY use this when the server asks for it: the user commented on specific "
                "tasks, or a task failed. "
                "Rewrite JUST those tasks; a task you do not list stays as it is. Not together "
                "with task_list, and not to add, delete or reorder tasks - that needs a full "
                "task_list. "
                'Example: [{"task_id": 3, "title": "Copy the revenue table in unchanged"}]'
            ),
        },
        "revises_step": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "OPTIONAL - normally left out. The step_number of an earlier step this one "
                "replaces. Example: 2"
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
                        "description": "The task's number in task_list (first = 1).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Another way to do that task.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Its trade-off, short.",
                    },
                    "topic": {
                        "type": "string",
                        "description": (
                            "OPTIONAL. What is being chosen, in two to four words, in the "
                            "language you use with the user. Once per task is enough."
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
                    "task_id": {"type": "integer", "minimum": 1},
                    "reason": {"type": "string"},
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


# The optional plan_and_think field of 3.0.0, in the {task_id, ...} shape task_updates
# already taught the model. Kept short on purpose: every word here is sent with every
# request (docs/context-budget-analysis.md), so the example carries the instruction.
_DONE_WHEN_PARAM: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "minimum": 1},
            "check": {
                "type": "string",
                "description": "The result, not the method. One short sentence.",
            },
        },
        "required": ["task_id", "check"],
    },
    "description": (
        "OPTIONAL. What will exist or be true when a task is finished, for tasks whose "
        "result can be checked. The user sees it with the plan, and your result_log for "
        'that task must show it was met. Example: [{"task_id": 2, "check": "A table with '
        'one revenue total per quarter (4 rows) exists"}]'
    ),
}


_FILES_PARAM: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "OPTIONAL, with DONE. The files this task created or changed. Leave it out if "
        'there are none. Example: ["D:/reports/q4_pivot.xlsx"]'
    ),
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
            'Send "ASK_USER". The user answers on the approval page, and the server refuses '
            "the other values from you while their question is open."
        )
    return {"type": "string", "enum": [d.value for d in Decision], "description": text}


REQUEST_USER_APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": _decision_param(chat=False),
        "plan_summary": {
            "type": "string",
            "description": (
                "REQUIRED with ASK_USER. A short summary of the plan for a non-technical "
                "reader. Example: 'I will (1) find the Q3 report, (2) extract the revenue "
                "table, (3) write a 5-line summary, (4) email it to the team lead.'"
            ),
        },
        "user_comment": {
            "type": "string",
            "description": (
                "OPTIONAL. The user's exact words, with REVISE or REJECTED. Example: 'Do not "
                "send the email, just show me the summary.'"
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
                "Copy next_task.task_id from the server's most recent response - the only "
                "task you may act on. Example: 1"
            ),
        },
        "status": {
            "type": "string",
            "enum": [s.value for s in TaskStatus],
            "description": (
                "IN_PROGRESS = starting now. DONE = finished. FAILED = could not finish. "
                'PENDING = reset to not-started. Example: "IN_PROGRESS"'
            ),
        },
        "result_log": {
            "type": "string",
            "description": (
                "REQUIRED with DONE. One or two sentences on the CONCRETE outcome: what you "
                "found, what you produced, or where you saved it. 'done' / 'ok' / 'completed' "
                "is refused. Example: 'Found the file at /reports/q3_sales.xlsx.'  or  "
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
                "The plan_id from an earlier response - a finished or cancelled plan can be "
                'read too. Example: "plan_20260724_0002". Send "current" only if you do not '
                "know it yet."
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
    done_when: bool = True,
    file_checks: bool = False,
    local_repair: bool = True,
    auto_ask: bool = True,
) -> list[dict[str, Any]]:
    """The advertised tool list for a given configuration.

    Every description says only what is true of the running server. One that describes a
    different mode - STOP here, call again there - is a contradiction the model has to
    reason its way out of, and for a thinking model that reasoning is the loop (D25).
    """
    reasoning = model_profile == "reasoning"
    if not blocking:
        chat = True
        approval_text = (
            REQUEST_USER_APPROVAL_DESCRIPTION_AUTO_CHAT if auto_ask
            else REQUEST_USER_APPROVAL_DESCRIPTION_CHAT
        )
    elif approval_mode == "return":
        chat = False
        approval_text = (
            REQUEST_USER_APPROVAL_DESCRIPTION_AUTO_RETURN if auto_ask
            else REQUEST_USER_APPROVAL_DESCRIPTION_RETURN
        )
    else:
        chat = False
        approval_text = (
            REQUEST_USER_APPROVAL_DESCRIPTION_AUTO if auto_ask
            else REQUEST_USER_APPROVAL_DESCRIPTION
        )
    approval_schema = dict(REQUEST_USER_APPROVAL_SCHEMA)
    approval_schema["properties"] = dict(REQUEST_USER_APPROVAL_SCHEMA["properties"])
    approval_schema["properties"]["decision"] = _decision_param(chat=chat)
    if auto_ask:
        # The model no longer opens the request, so it has no overview to write: the
        # field would be one more thing to fill in for nobody to read.
        del approval_schema["properties"]["plan_summary"]
    # Only in chat mode does the model relay what the user picked; with a page the user
    # picks there, so the field is not even offered (2.0.0).
    if chat and alternatives:
        approval_schema["properties"]["choices"] = _CHOICES_PARAM

    plan_text = PLAN_AND_THINK_DESCRIPTION_REASONING if reasoning else PLAN_AND_THINK_DESCRIPTION
    if auto_ask:
        plan_text = plan_text.replace(_REVIEWED_LATER, _REVIEWED_IN_THIS_CALL).replace(
            _REVIEWED_LATER_REASONING, _REVIEWED_IN_THIS_CALL_REASONING
        )
    plan_schema = dict(PLAN_AND_THINK_SCHEMA_REASONING if reasoning else PLAN_AND_THINK_SCHEMA)
    if alternatives:
        plan_schema["properties"] = {
            **plan_schema["properties"],
            **_alternatives_params(max_alternatives, max_choice_points),
        }
        if reasoning:
            plan_text += _TORN_REASONING
    if local_repair and reasoning:
        plan_text += _REPAIR_REASONING
    elif local_repair:
        # Beside the other "if the server names a task" case, ahead of the closing line.
        closing = "\n\nDo not execute anything or answer the user while planning."
        plan_text = plan_text.replace(closing, _REPAIR_STANDARD + closing)
    else:
        # Without repair a task_updates call can only answer the user's comments, and
        # the description must not offer a use the server would refuse.
        updates = dict(plan_schema["properties"]["task_updates"])
        updates["description"] = updates["description"].replace(
            "the server asks for it: the user commented on specific tasks, or a task "
            "failed. ",
            "the server told you the user commented on specific tasks. ",
        )
        plan_schema["properties"] = {**plan_schema["properties"], "task_updates": updates}
    update_text = (
        UPDATE_TASK_PROGRESS_DESCRIPTION if auto_advance
        else UPDATE_TASK_PROGRESS_DESCRIPTION_MANUAL
    )
    if auto_ask:
        # Approval is no longer a step of its own, so execution is the second one.
        update_text = update_text.replace("STEP 3 -", "STEP 2 -", 1)
    update_schema = dict(UPDATE_TASK_PROGRESS_SCHEMA)
    if done_when:
        plan_schema["properties"] = {
            **plan_schema["properties"], "done_when": _DONE_WHEN_PARAM
        }
        if reasoning:
            plan_text += _CHECK_LATER_REASONING
        update_text += _DONE_WHEN_EXEC
    if file_checks:
        # Offered only where the server will actually look. A field the model fills in
        # and nothing reads is a promise of verification that is not kept.
        update_schema["properties"] = {
            **UPDATE_TASK_PROGRESS_SCHEMA["properties"], "files": _FILES_PARAM
        }
        update_text += _FILES_EXEC
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
            "description": update_text,
            "inputSchema": update_schema,
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
