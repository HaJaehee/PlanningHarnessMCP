# Phase 1 — Tool Interface & Schema Blueprint

Server name: `planning-mcp` (single unified server, 4 tools only)
Transport: stdio or SSE on `localhost` (AnythingLLM custom MCP)

---

## 0. Design Rules (why the schema looks like this)

These rules exist because the target model is a weak, air-gapped corporate LLM with
unreliable tool-calling. Every deviation costs us a malformed call.

| Rule | Reason |
|---|---|
| Only 4 tools, no overlapping names | Tool-selection confusion is the #1 failure mode |
| `snake_case`, all-lowercase, no abbreviations | Models mis-capitalize `taskId` / `TaskID` |
| **No nested objects, no array-of-objects** in any input | Weak models emit broken JSON for nested structures. `task_list` is `array<string>`; the server assigns IDs |
| Enums are UPPERCASE fixed strings | Prevents `done` / `Done` / `completed` drift; server also accepts case-insensitive + aliases |
| Every tool has ≥1 required param | Zero-arg tools cause `{}` / `null` argument bugs |
| Every response contains `next_action` + `next_action_hint` | The server *drives* the model instead of hoping it reasons |
| Server never throws; errors return `ok: false` + a corrective `next_action` | A raw MCP error usually makes a weak model give up and hallucinate an answer |

---

## 1. `plan_and_think`

**Purpose:** Sequential Thinking + task breakdown in one call. This is the mandatory entry point.

### Description string (shipped to the LLM verbatim)

```
STEP 1 — MANDATORY FIRST TOOL.
You MUST call this tool before answering ANY user request, even simple-looking ones.
Use it to think one step at a time and to write down the task breakdown.

HOW TO USE:
- Call it once per thinking step. Start at step_number = 1.
- Keep calling with need_more_thinking = true until your plan is complete.
- On your FINAL thinking step, set need_more_thinking = false AND provide task_list.
- To correct an earlier step, set revises_step to that step number.

IF THE USER COMMENTED ON PARTICULAR TASKS:
The server will tell you so and name them. Then send task_updates instead of task_list,
rewriting ONLY those tasks. Every other task was already accepted - leave it alone.

DO NOT execute anything, DO NOT answer the user while using this tool.
```

### JSON Schema

```json
{
  "name": "plan_and_think",
  "description": "<description string above>",
  "inputSchema": {
    "type": "object",
    "properties": {
      "goal": {
        "type": "string",
        "description": "One sentence restating what the user ultimately wants. Repeat the SAME goal text on every step. Example: 'Summarize the Q3 sales report and email it to the team lead.' If the user CORRECTS the goal, keep sending the old text here and put the corrected one in revised_goal."
      },
      "revised_goal": {
        "type": "string",
        "description": "OPTIONAL. Use ONLY when the user says the goal itself was wrong or has changed - for example 'no, I meant the Q4 report, not Q3'. Put the corrected goal here and leave 'goal' as the text you have been sending, so the server can find the plan and record the change. Do NOT use it to reword or paraphrase the same goal. Example: 'Summarize the Q4 sales report and email it to the team lead.'"
      },
      "thought": {
        "type": "string",
        "description": "Your reasoning for THIS step only. One idea per call. Example: 'Step 2: I must first locate the Q3 report file before I can summarize it.'"
      },
      "step_number": {
        "type": "integer",
        "minimum": 1,
        "description": "Which thinking step this is. Starts at 1 and increases by exactly 1 each call. Example: 2"
      },
      "total_steps": {
        "type": "integer",
        "minimum": 1,
        "description": "Your current estimate of how many thinking steps are needed. You may raise this number later. Example: 4"
      },
      "need_more_thinking": {
        "type": "boolean",
        "description": "true = you will call plan_and_think again. false = this is your LAST thinking step and task_list is now final. Example: true"
      },
      "task_list": {
        "type": "array",
        "items": { "type": "string" },
        "description": "REQUIRED when need_more_thinking is false. A flat list of plain-text action items, in execution order. Plain strings only - do NOT send objects, do NOT add numbering, do NOT add status. The server assigns task_id automatically. Example: [\"Locate the Q3 sales report file\", \"Extract the revenue table\", \"Write a 5-line summary\", \"Send the summary by email\"]"
      },
      "task_updates": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "task_id": { "type": "integer", "minimum": 1, "description": "The task the user commented on. Example: 3" },
            "title": { "type": "string", "description": "The rewritten task, answering the user's comment. Example: 'Copy the revenue table into the summary unchanged'" }
          },
          "required": ["task_id", "title"]
        },
        "description": "ONLY use this when the server told you the user commented on specific tasks. Rewrite JUST those tasks; every task you do not list stays exactly as it is. Do NOT send task_list at the same time, and do NOT use this to add, delete or reorder tasks - that requires a full task_list. Example: [{\"task_id\": 3, \"title\": \"Copy the revenue table in unchanged\"}]"
      },
      "revises_step": {
        "type": "integer",
        "minimum": 1,
        "description": "OPTIONAL. Only set this when you are CORRECTING an earlier thinking step. Set it to the step_number you are replacing. Example: 2"
      }
    },
    "required": ["goal", "thought", "step_number", "total_steps", "need_more_thinking"]
  }
}
```

### Response contract

```json
{
  "ok": true,
  "plan_id": "plan_20260724_0001",
  "plan_status": "DRAFTING",
  "recorded_step": 2,
  "total_steps": 4,
  "tasks": [],
  "next_action": "CALL_PLAN_AND_THINK",
  "next_action_hint": "Call plan_and_think again with step_number=3 and the same goal.",
  "message": "Thinking step 2 recorded."
}
```

On the final step (`need_more_thinking: false` + valid `task_list`):

```json
{
  "ok": true,
  "plan_id": "plan_20260724_0001",
  "plan_status": "AWAITING_APPROVAL",
  "recorded_step": 4,
  "total_steps": 4,
  "tasks": [
    { "task_id": 1, "title": "Locate the Q3 sales report file", "status": "PENDING" },
    { "task_id": 2, "title": "Extract the revenue table",       "status": "PENDING" },
    { "task_id": 3, "title": "Write a 5-line summary",           "status": "PENDING" },
    { "task_id": 4, "title": "Send the summary by email",        "status": "PENDING" }
  ],
  "next_action": "CALL_REQUEST_USER_APPROVAL",
  "next_action_hint": "Plan is drafted but LOCKED. You must now call request_user_approval with decision='ASK_USER'. You are NOT allowed to start any task yet.",
  "message": "Plan created with 4 tasks. Execution is locked until the user approves."
}
```

### Guard rails implemented server-side
- `need_more_thinking: false` with empty/missing `task_list` → `ok: false`, `next_action: "CALL_PLAN_AND_THINK"`, hint: *"Send the same step again with a non-empty task_list."*
- `step_number` skipped or repeated → auto-normalized to `last_step + 1`, warning included, never an error.
- `revises_step` present → old step marked `superseded`, history preserved, plan reverts to `DRAFTING`.
- `task_updates` present while the plan carries a pending per-task revision → only the flagged tasks are rewritten; task ids, positions, and the `result_log` of untouched tasks survive. An edit to an unflagged task is dropped with a note; an unknown `task_id` → `TASK_NOT_FOUND` with nothing written.
- `task_updates` present with **no** pending per-task revision → `REVISION_NOT_REQUESTED`. The model may not edit an approved plan on its own authority.
- A full `task_list` sent in answer to a per-task request is accepted (the user re-approves every task anyway), noted, and audited `targeted_revision_ignored`.

---

## 2. `request_user_approval` (HITL breakpoint)

**Purpose:** The mandatory human gate between planning and execution. One tool, two modes,
selected by the required `decision` enum — no hidden state for the model to track.

### Description string

```
STEP 2 — MANDATORY HUMAN APPROVAL GATE.
The plan is LOCKED until the user approves it. You cannot skip this tool.

USE IN TWO PHASES:
Phase A - ASK: call with decision = "ASK_USER" and a plan_summary.
          Then STOP. Print the plan to the user and wait. Say nothing else.
Phase B - REPORT: after the user replies in chat, call this tool AGAIN with
          decision = "APPROVED"  (user said yes / ok / proceed / 승인)
          decision = "REJECTED"  (user said no / cancel / stop / 취소)
          decision = "REVISE"    (user asked for changes) + put the requested
                                 change into user_comment.

NEVER guess the user's answer. NEVER call APPROVED unless the user actually said so.
```

### JSON Schema

```json
{
  "name": "request_user_approval",
  "description": "<description string above>",
  "inputSchema": {
    "type": "object",
    "properties": {
      "decision": {
        "type": "string",
        "enum": ["ASK_USER", "APPROVED", "REJECTED", "REVISE"],
        "description": "ASK_USER = first call, ask the human. APPROVED / REJECTED / REVISE = report what the human actually replied. Example: \"ASK_USER\""
      },
      "plan_summary": {
        "type": "string",
        "description": "REQUIRED when decision is ASK_USER. A short human-readable summary of the plan you want approval for, written for a non-technical reader. Example: 'I will (1) find the Q3 report, (2) extract the revenue table, (3) write a 5-line summary, (4) email it to the team lead.'"
      },
      "user_comment": {
        "type": "string",
        "description": "OPTIONAL. Copy the user's exact words here when decision is REVISE or REJECTED. Example: 'Do not send the email, just show me the summary.'"
      }
    },
    "required": ["decision"]
  }
}
```

### Response contract

`decision = "ASK_USER"`:
```json
{
  "ok": true,
  "plan_id": "plan_20260724_0001",
  "plan_status": "AWAITING_APPROVAL",
  "tasks": [ "...same shape as above..." ],
  "next_action": "STOP_AND_WAIT_FOR_USER",
  "next_action_hint": "STOP NOW. Do not call any other tool. Show the plan below to the user and ask: 'Approve this plan? (yes / no / tell me what to change)'. Wait for their reply in the next message.",
  "display_to_user": "PLAN FOR APPROVAL\n1. Locate the Q3 sales report file\n2. Extract the revenue table\n3. Write a 5-line summary\n4. Send the summary by email\n\nApprove this plan? (yes / no / change ...)"
}
```

`decision = "APPROVED"`:
```json
{
  "ok": true,
  "plan_status": "APPROVED",
  "next_action": "CALL_UPDATE_TASK_PROGRESS",
  "next_action_hint": "Execution is now UNLOCKED. Start task_id=1 by calling update_task_progress with status='IN_PROGRESS'.",
  "next_task": { "task_id": 1, "title": "Locate the Q3 sales report file" }
}
```

`decision = "REVISE"`:
```json
{
  "ok": true,
  "plan_status": "DRAFTING",
  "next_action": "CALL_PLAN_AND_THINK",
  "next_action_hint": "The user requested changes. Call plan_and_think with revises_step set, incorporate the user_comment, then request approval again.",
  "user_comment": "Do not send the email, just show me the summary.",
  "revision_scope": "PLAN"
}
```

`decision = "REVISE"` where the human commented on individual tasks on the approval page
(`revision_scope: "TASKS"` — only those tasks may change):
```json
{
  "ok": true,
  "plan_status": "DRAFTING",
  "next_action": "CALL_PLAN_AND_THINK",
  "next_action_hint": "The user commented on specific tasks (task 3: \"Just paste the table, do not summarize it\"). Call plan_and_think with need_more_thinking=false and task_updates=[{\"task_id\": 3, \"title\": \"<the rewritten task>\"}] - do NOT send task_list. Task(s) 1, 2, 4 were accepted by the user as they stand - do NOT change, renumber, reorder or resend them.",
  "revision_scope": "TASKS",
  "revision_targets": [
    { "task_id": 3, "title": "Write a 5-line summary", "user_comment": "Just paste the table, do not summarize it" }
  ],
  "tasks_unchanged": [1, 2, 4]
}
```

`decision = "REJECTED"`:
```json
{
  "ok": true,
  "plan_status": "CANCELLED",
  "next_action": "ANSWER_USER",
  "next_action_hint": "The plan is cancelled. Tell the user it was cancelled and ask what they want to do instead. Do not execute anything."
}
```

---

## 3. `update_task_progress`

**Purpose:** State machine for tasks + execution log. Also the server-side enforcer of the
approval lock.

### Description string

```
STEP 3 — EXECUTION TRACKING.
Handle exactly ONE task per call.

  1. Start the FIRST task: status = "IN_PROGRESS". Then do the work.
  2. Report it: status = "DONE" + a result_log saying what you actually produced.
  3. The server then starts the NEXT task for you and names it in next_task.
     Do that work, then report it "DONE" the same way.
     You do NOT send "IN_PROGRESS" again - just keep reporting DONE, one call per
     task, until the server tells you no tasks remain.
Use status = "FAILED" instead of "DONE" if the task did not work.
Never mark a task DONE before you actually did it.
If a task fails, set status = "FAILED" and explain in result_log - then follow the
next_action the server gives you back.
```

Two descriptions exist: the one above (server auto-starts the next task, the default) and a
"call this tool TWICE for every task" variant used when `PLANNING_MCP_AUTO_ADVANCE=false`.
`schemas.build_tool_definitions(auto_advance=...)` picks one, so the advertised contract can
never contradict the running server.

### JSON Schema

```json
{
  "name": "update_task_progress",
  "description": "<description string above>",
  "inputSchema": {
    "type": "object",
    "properties": {
      "task_id": {
        "type": "integer",
        "minimum": 1,
        "description": "The number of the task you are working on, taken from the tasks list the server gave you. One task per call. Example: 1"
      },
      "status": {
        "type": "string",
        "enum": ["PENDING", "IN_PROGRESS", "DONE", "FAILED"],
        "description": "IN_PROGRESS = starting now. DONE = finished successfully. FAILED = could not finish. PENDING = reset back to not-started. Example: \"IN_PROGRESS\""
      },
      "result_log": {
        "type": "string",
        "description": "OPTIONAL but STRONGLY recommended. What you actually did or what actually went wrong, in one or two sentences. Example: 'Found the file at /reports/q3_sales.xlsx.'  or  'FAILED: no file matching q3 was found in /reports.'"
      }
    },
    "required": ["task_id", "status"]
  }
}
```

### Response contract

```json
{
  "ok": true,
  "plan_id": "plan_20260724_0001",
  "task_id": 1,
  "task_status": "DONE",
  "progress": "1/4 done",
  "tasks": [ "...full refreshed list..." ],
  "next_action": "CALL_UPDATE_TASK_PROGRESS",
  "next_action_hint": "Next task is task_id=2 'Extract the revenue table'. Call update_task_progress with task_id=2 and status='IN_PROGRESS'.",
  "next_task": { "task_id": 2, "title": "Extract the revenue table" }
}
```

All tasks `DONE`:
```json
{
  "ok": true,
  "plan_status": "COMPLETED",
  "progress": "4/4 done",
  "next_action": "ANSWER_USER",
  "next_action_hint": "All tasks are complete. Now write the final answer to the user, summarizing the result_log of each task."
}
```

On `FAILED`:
```json
{
  "ok": true,
  "task_status": "FAILED",
  "next_action": "CALL_PLAN_AND_THINK",
  "next_action_hint": "Task 2 failed. Do NOT continue to task 3. Call plan_and_think to re-plan around this failure, then request_user_approval again.",
  "failed_task": { "task_id": 2, "title": "Extract the revenue table", "result_log": "..." }
}
```

Execution attempted while locked (the critical guard):
```json
{
  "ok": false,
  "error_code": "PLAN_NOT_APPROVED",
  "plan_status": "AWAITING_APPROVAL",
  "next_action": "CALL_REQUEST_USER_APPROVAL",
  "next_action_hint": "You cannot start tasks yet - the user has not approved the plan. Call request_user_approval with decision='ASK_USER'."
}
```

---

## 4. `get_current_plan`

**Purpose:** Context recovery. Long AnythingLLM conversations get truncated; this lets the
model re-read its own plan instead of hallucinating one.

### Description string

```
RECOVERY TOOL.
Call this when you are unsure what the plan is, which task you were on, or after a long
conversation. It returns the full current plan and tells you exactly what to do next.
It changes nothing - it is always safe to call.
```

### JSON Schema

```json
{
  "name": "get_current_plan",
  "description": "<description string above>",
  "inputSchema": {
    "type": "object",
    "properties": {
      "plan_id": {
        "type": "string",
        "description": "Use the exact text \"current\" to get the active plan. Only use a real plan_id if you want an older plan. Example: \"current\"",
        "default": "current"
      }
    },
    "required": ["plan_id"]
  }
}
```

> `plan_id` is required-with-a-constant rather than a zero-arg tool on purpose: weak models
> reliably emit `{"plan_id":"current"}` but frequently emit malformed/empty arguments for
> no-parameter tools.

### Response contract

```json
{
  "ok": true,
  "plan_id": "plan_20260724_0001",
  "goal": "Summarize the Q3 sales report and email it to the team lead.",
  "plan_status": "APPROVED",
  "thinking_steps": [
    { "step_number": 1, "thought": "...", "superseded": false }
  ],
  "tasks": [
    { "task_id": 1, "title": "...", "status": "DONE",        "result_log": "..." },
    { "task_id": 2, "title": "...", "status": "IN_PROGRESS", "result_log": null },
    { "task_id": 3, "title": "...", "status": "PENDING",     "result_log": null }
  ],
  "progress": "1/3 done",
  "next_action": "CALL_UPDATE_TASK_PROGRESS",
  "next_action_hint": "Resume task_id=2. When it is finished, call update_task_progress with status='DONE'."
}
```

No active plan:
```json
{
  "ok": true,
  "plan_status": "NONE",
  "next_action": "CALL_PLAN_AND_THINK",
  "next_action_hint": "There is no active plan. Start one by calling plan_and_think with step_number=1."
}
```

---

## 5. Shared enums (single source of truth)

| Enum | Values |
|---|---|
| `plan_status` | `NONE`, `DRAFTING`, `AWAITING_APPROVAL`, `APPROVED`, `IN_EXECUTION`, `BLOCKED`, `COMPLETED`, `CANCELLED` |
| `task.status` | `PENDING`, `IN_PROGRESS`, `DONE`, `FAILED` |
| `next_action` | `CALL_PLAN_AND_THINK`, `CALL_REQUEST_USER_APPROVAL`, `CALL_UPDATE_TASK_PROGRESS`, `CALL_GET_CURRENT_PLAN`, `STOP_AND_WAIT_FOR_USER`, `ANSWER_USER` |
| `error_code` | `PLAN_NOT_APPROVED`, `PLAN_NOT_READY`, `PLAN_BLOCKED`, `PLAN_CANCELLED`, `NO_ACTIVE_PLAN`, `TASK_NOT_FOUND`, `MISSING_TASK_LIST`, `MISSING_PLAN_SUMMARY`, `INVALID_STATUS`, `INVALID_DECISION`, `INVALID_STEP`, `INTERNAL_ERROR` |

> The error list grew during implementation: `PLAN_NOT_READY` (approve requested before a task
> list exists), `PLAN_BLOCKED` (a task failed — re-plan, do not continue), `MISSING_PLAN_SUMMARY`,
> `INVALID_DECISION`, `INVALID_STEP` (bad `revises_step`), `APPROVAL_NOT_REQUESTED` (APPROVED
> sent for a plan version the user was never shown — approval binds to the exact version last
> displayed via ASK_USER), and `INTERNAL_ERROR`. Each maps to a
> corrective `next_action` in `planning/state_machine.py`; the enums live in `planning/models.py`
> and the advertised schemas are generated from them, so this table cannot drift from the code.

**Every response from every tool always contains:** `ok`, `plan_status`, `next_action`,
`next_action_hint`. This uniformity is what lets a weak model behave like a state machine.

## 6. Input leniency layer (server-side, invisible to the LLM)

`normalize()` runs **inside** the handler's exception guard. It used to run before it, so an
unforeseen input that made it raise escaped as a raw JSON-RPC error instead of the graceful
`ok:false` a weak model can act on. It also drops non-string keys instead of calling `.lower()`
on them, and rejects NaN/inf where an integer is expected. Fuzzed against nested objects, mixed
types, nulls and 100k-char strings across all four tools — it must never raise.

Applied before validation so that near-miss calls succeed instead of erroring:

- Case/alias normalization: `done|complete|completed|finished|완료` → `DONE`; `in progress|started|doing|running|진행중` → `IN_PROGRESS`; `fail|error|failed|실패` → `FAILED`; `yes|y|ok|approve|승인|네|예|진행|좋아요` → `APPROVED`; `no|cancel|reject|취소|아니오|아니요|거부|거절` → `REJECTED`.

  > **Client-side validation caveat (observed in practice):** these enum aliases only help when
  > the MCP client forwards the raw value. Strict clients (observed: Claude Code) validate
  > `status`/`decision` against the advertised enum and reject non-members with `-32602` before
  > the server ever sees them — `done`/`진행중`/`네` cannot be repaired there. The non-enum
  > repairs below are unaffected (they operate on values that pass schema validation). Verify
  > AnythingLLM's behavior during bring-up: Phase 4, E16–E17.
- `"true"` / `"false"` / `1` / `0` strings coerced to boolean for `need_more_thinking`.
- `"3"` coerced to integer for `step_number` / `total_steps` / `task_id`.
- `task_list` given as a newline- or comma-separated single string → split into an array.
- `task_list` given as array-of-objects → `title`/`name`/`task`/`text` key extracted.
- Unknown extra properties are dropped, never rejected.
