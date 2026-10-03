# 03 · Tool Contract

Four tools. Machine-readable schemas: [data/tool-schemas.json](data/tool-schemas.json). Shared
enums: [data/enums.json](data/enums.json). The authoritative source is `planning/schemas.py`
(built from `planning/models.py`); this page is the human-readable summary.

Design rules behind the schemas (all because the model is weak):
`snake_case`, all-lowercase; **no nested input objects** (`task_list` is `array<string>`, the
server assigns ids); UPPERCASE enums; every tool has ≥1 required param; every response carries
`next_action` + `next_action_hint`.

**The advertised text depends on the running configuration (1.16.0).** `build_tool_definitions`
takes `auto_advance`, `model_profile`, `approval_mode` and `blocking`, and every description says
only what is true of the server that is running. One text for all modes was a contradiction a
thinking model reasoned about on every call ([D25](09-defects-and-lessons.md#d25)):

| varies with | tool | what changes |
|---|---|---|
| `model_profile` | `plan_and_think` | `standard`: one thinking step per call. `reasoning`: record the plan in **one** call; `step_number`, `total_steps`, `revises_step` are not advertised (still accepted), `thought` is optional, a `task_list` with no `need_more_thinking` means final. |
| `approval_mode` / `blocking` | `request_user_approval` | chunked / heartbeat: "this call waits; APPROVAL_PENDING = call again". `return`: "show display_to_user, end your turn, call ASK_USER again when the user writes". No page: the two-phase ask / report text - the only mode that tells the model to send APPROVED. |
| `auto_advance` | `update_task_progress` | as since 1.11.0 |
| `done_when` (3.0.0) | `plan_and_think`, `update_task_progress` | on: the `done_when` field, and "if next_task has done_when, result_log must show it was met". Reasoning profile also: "to double-check a task, write the check in done_when instead of re-checking now". Off: none of it. |
| `file_checks` (3.0.0) | `update_task_progress` | the `files` field and "the server refuses DONE for a file it cannot find" - **only** when `PLANNING_MCP_ARTIFACT_ROOTS` is set. A field the model fills in and nothing reads would promise a check that is not made. |
| `local_repair` (3.0.0) | `plan_and_think` | on: "if a task failed, send task_updates for it". Off: `task_updates` is described as answering the user's comments only. |

With `done_when` and `local_repair` off none of the 3.0 text is advertised (`TestSchema`).

**The text was trimmed in 3.0.0.** It had grown one field failure at a time until "one task per
call" was said three times, the `FAILED` rule twice, and "send your own plan_id, `current` is
a guess" in full in both the description and the parameter of `get_current_plan`. Now each
rule is said once, and where it is said follows one layout (the docstring of
`planning/schemas.py`): a rule about *what to do* lives in the tool description, which is the
part that always arrives - some clients drop or shorten parameter descriptions when they relay
a schema (matrix E16); a parameter description gives the shape of the value and one example;
for an array of objects the example sits on the array, showing the whole shape, and the nested
fields carry a few words rather than a second example. No example, enum value or rule was
removed - `TestToolTextIsLean` pins the rules and a ceiling on the size.

| estimated tokens (`bytes // 3`, the method of `docs/context-budget-analysis.md`) | standard | reasoning |
|---|---:|---:|
| 2.0.0 | 3,903 | 3,703 |
| 3.0.0 new fields, before the trim | 4,239 | 4,066 |
| **3.0.0 as shipped** | **3,486** | **3,325** |
| ... with file checks (`files` advertised) | 3,599 | 3,438 |
| ... with `done_when` and `local_repair` off | 3,181 | 2,993 |

The trim is **not measured on the corporate model**. The long text was long because weak models
needed telling; what was cut is repetition, and repetition can itself be what a weak model
needs. If tool-call errors rise after the upgrade, that is the first place to look.

None of them says "before answering ANY request" any more: that rule, against `ANSWER_USER`, is
what sent a thinking model back into planning after the last task. They say "for each NEW
request; ANSWER_USER means write the answer". `TestPromptHygiene` checks every combination for
the removed phrases.

---

## 1. `plan_and_think` — the mandatory entry point

One thinking step per call. `need_more_thinking=true` to continue; on the final step set it
`false` **and** provide `task_list`.

Required: `goal`, `thought`, `step_number`, `total_steps`, `need_more_thinking`.
Optional: `task_list` (required when finalizing), `task_updates`, `revised_goal`, `revises_step`,
`plan_id`, `alternatives`, `recommended_reasons` (2.0.0), `done_when` (3.0.0).

- **Routing by goal.** The model repeats the same `goal` on every step (the system prompt tells
  it to), so a matching active plan *is* this session's plan. A different goal starts its own
  new plan and never touches another conversation's. Optional `plan_id` overrides goal routing.
- **Goal-drift protection (1.8.2).** If the model is *continuing* (`step_number > 1`) but its
  goal matches no active plan — i.e. the goal drifted — the server does **not** fork a new plan.
  It returns `GOAL_NOT_MATCHED` with an `active_plans` directory (id + goal), telling the model
  to call again with the exact goal shown, or the `plan_id`, or `step_number=1` for a genuinely
  new plan. `step_number == 1` with a new goal always starts a new plan (so new conversations
  work). Goal matching ignores trailing punctuation/whitespace. See
  [05](05-concurrency-and-sessions.md#goal-drift).
- **Goal revision (1.12.0).** Drift protection must not become an inability to be corrected.
  When the *user* says the goal itself was wrong ("I meant Q4, not Q3"), the model sends
  `revised_goal` alongside the `goal` it has been using: the plan is found by the old text and
  its `goal` is **updated in place** — same `plan_id`, same tasks, no fork. The anchor survives
  as `original_goal` plus a `goal_history` of `{at, from, to, source}` hops (audit event
  `goal_revised`), which is what immutability was really protecting. Routing then accepts the
  new text; the *old* text keeps resolving to the plan for as long as it stays active, so a
  model one turn behind the correction is not sent back to re-select. A `revised_goal` equal to
  the current goal after normalization is not a correction and is not recorded. If the model
  puts the corrected text in both fields it matches nothing by name, so with exactly one active
  plan the server applies it there; with several it returns `GOAL_NOT_MATCHED` to pick from.
  Revising the goal of an `APPROVED`/`IN_EXECUTION` plan is accepted and recorded but does not
  silently widen the approval: the response says the human approved the *previous* goal and
  tells the model to re-plan and re-approve if the tasks no longer serve the corrected one.
- On finalize → `plan_status: AWAITING_APPROVAL`, `next_action: CALL_REQUEST_USER_APPROVAL`.
- **Alternatives (2.0.0).** `task_list` is the model's *recommendation*. For a task whose right
  way depends on the user's preference it may add `alternatives:
  [{"task_id", "title", "reason", "topic"}]` (other ways to do that task; `topic` names what
  is being chosen in a few words, heads the choice, and is optional - the first one per task
  wins) and `recommended_reasons:
  [{"task_id", "reason"}]` (why it prefers its own) - the `{task_id, ...}` shape `task_updates`
  already taught it. `task_id` numbers into the same call's `task_list`. The server turns them
  into each task's `options` (index 0 = the recommendation) after validating: unknown numbers,
  a repeat of the recommendation, duplicates and DONE tasks (carried through a redraft) are
  dropped with a note; at most `PLANNING_MCP_MAX_ALTERNATIVES` (3) per task and
  `PLANNING_MCP_MAX_CHOICE_POINTS` (3) tasks per plan. The finalize response names the tasks
  that offer a choice (`choice_points`). Alternatives sent while still thinking are kept with
  the draft (`draft_alternatives`) and go with it if the server submits the draft; a new draft
  `task_list` replaces them. `task_updates` cannot carry alternatives, and rewriting a task
  drops its choice. Advertised in both profiles; only the reasoning profile is also told "if
  you are torn between two ways, put the other in alternatives - the user picks". Turned off
  with `PLANNING_MCP_ALTERNATIVES=off` (not advertised, ignored with a note).
- **Completion criteria (3.0.0).** `done_when: [{"task_id", "check"}]` says, for a task whose
  result can be checked, what will exist or be true when it is finished - the result, not the
  method, so it stays when the human picks another *way* to do the task. One per task (the
  first wins), cut to `PLANNING_MCP_MAX_DONE_WHEN_CHARS` (200) with a note. A criterion that only
  says the task gets done ("작업이 완료된다", the title repeated) is dropped with a note - the
  page then shows none, which is true and invites the human to write one. Kept with a draft
  (`draft_done_when`) and submitted with it. May accompany `task_updates`, for the tasks being
  rewritten: a rewritten task drops the criterion the model wrote for the old wording unless
  the same call sends a new one, and **never** replaces one the human wrote
  (`done_when_by == "user"`). There is no way for the model to set a criterion after the plan is
  on the page - `request_user_approval` has no such field in any mode. Turned off with
  `PLANNING_MCP_DONE_WHEN=off` (the model's field only; the human can still write one).
- **Repairing a failed task (3.0.0).** After a `FAILED`, the server names the task and the
  hint carries the argument: `task_updates=[{"task_id": 3, "title": "<another way to do this
  task>"}]`. The failed task must be among the updates; unfinished tasks after it may be;
  `DONE` tasks cannot be touched and keep their results. See
  [04](04-state-machine.md#local-repair-300).
- **Convergence (1.16.0, [D25](09-defects-and-lessons.md#d25)).**
  - *Thinking budget.* A drafting round may take `PLANNING_MCP_MAX_THINKING_STEPS` steps (0 =
    profile default: standard 8, reasoning 2; negative = unlimited). It starts on the round's first
    step and restarts whenever the plan re-enters DRAFTING (a human's 수정 요청, a failure, a lifted
    halt). Responses carry `thinking_steps_left`; the hint names the exit first.
  - *Draft.* A `task_list` sent with `need_more_thinking=true` is kept as `draft_tasks`
    (`draft_saved` in the response). A final call with no list uses it. When the budget runs out
    with a draft, the call is treated as final (`auto_finalized` audit) and - with a page - goes
    straight to the human; with no draft, the circuit breaker halts the plan.
  - *A finalized plan is not reopened by the model.* On `AWAITING_APPROVAL` (without
    `revised_goal`) or `AWAITING_COMPLETION` the call changes nothing and says so
    (`replan_redirected`). Before 1.16 it reset the plan to DRAFTING - after the last task that
    deleted every `result_log` ([D26](09-defects-and-lessons.md#d26)).
  - *An open request makes the call wait.* While a human has a request open for the plan, the
    call waits on them like the approval call does and returns their decision; the `thought` is
    attached to the card as an agent note. Undecided, it returns `APPROVAL_PENDING` /
    `PLAN_NOT_APPROVED` / `COMPLETION_PENDING`, never `ok:true`.
  - *No second lap.* A goal completed less than `PLANNING_MCP_REPLAN_COOLDOWN` (600 s) ago is
    answered from its results (`ANSWER_USER` + tasks with evidence), not re-planned. Reword the goal
    ("Redo: ...") to genuinely run it again.
- **Targeted revision (1.9.x).** When the human commented on individual tasks on the approval
  page, the plan carries `pending_revision` and the model finalizes with `task_updates`
  (`[{"task_id": 3, "title": "..."}]`) instead of `task_list`. Only the flagged tasks are
  rewritten — everything else keeps its id, position, status and `result_log`. An edit to an
  unflagged task is dropped with a note; an unknown `task_id` is `TASK_NOT_FOUND` and **nothing
  is written** (validation completes before any mutation). `task_updates` with no pending request
  → `REVISION_NOT_REQUESTED`. See [06](06-human-in-the-loop.md#per-task-review-19x).
- Guard rails: missing/empty `task_list` on finalize → `MISSING_TASK_LIST`; `step_number`
  jumps/repeats are auto-normalized (never an error); `revises_step` marks the old step
  superseded and reverts to `DRAFTING`; oversized `task_list` truncated to `max_tasks`.

## 2. `request_user_approval` — the HITL gate

Required: `decision` ∈ {`ASK_USER`,`APPROVED`,`REJECTED`,`REVISE`}.
Optional: `plan_summary` (the 3.0 flow only - see below), `user_comment`, `plan_id`.

**Since 3.1.0 the model does not open the request** (`PLANNING_MCP_AUTO_ASK`, default on):
the server does, in the final `plan_and_think` call and in the last `DONE`, and that call
waits. With a page this tool is then advertised as *WAITING FOR THE USER* - 
`decision: "ASK_USER"` and nothing else, called when a response says `APPROVAL_PENDING` -
and `plan_summary` is not in its schema. Without a page it is *REPORT THE USER'S REPLY*.
What follows describes what each decision does whenever it is called, in either flow. In
the 3.0 flow (`PLANNING_MCP_AUTO_ASK=false`) the model calls `ASK_USER` itself, after
planning and after the last task, and `plan_summary` is required the first time a plan is
asked about.

- **`ASK_USER`**: publishes the plan to the approval page and, in blocking mode, **holds the
  tool call open until a human decides** (see [06](06-human-in-the-loop.md)). Returns
  `next_action: STOP_AND_WAIT_FOR_USER` with a pre-rendered `display_to_user` and `approval_url`
  if the wait times out.
- **On a halted plan (1.16.0)** `ASK_USER` puts the halt card in front of the human and waits
  like an approval (no `plan_summary` needed). A decision is accepted only when no page holds the
  halt - in chat mode `APPROVED` = approve the draft as it stands, `REVISE` = continue (with
  `user_comment` as a direction), `REJECTED` = cancel. With the card on the page a model-sent
  decision is `APPROVAL_PENDING`, as in D20.
- **Choices (2.0.0).** With a page, the human picks on the page and the page is the only channel:
  `choices` is not even advertised, and one the model sends is ignored with a note (refused
  outright while the request is open - D20). In chat mode (no page) `APPROVED` may carry
  `choices: {"2": "B"}` - letters, A = the recommendation; a bare number is refused by
  leniency because it could mean A or B. Anything missing or invalid keeps the
  recommendation.
- **Criteria (3.0.0).** On a plan request the human may write or rewrite a task's `done_when`
  and approve in the same click; the page posts `criteria: {"3": "..."}` (`""` removes one) and
  the store refuses the whole decision if it names a task the request did not show or one that
  is already `DONE`. Applied in the same transaction as the approval, audited
  `criteria_applied` (before / after). The response tells the model which tasks changed; each
  criterion is in `next_task` when its task is reached, with `done_when_by: "user"`. Criteria
  typed before pressing REVISE are kept too: applied to the tasks on a per-task revision,
  carried in `user_comment` on a whole-plan one. Not accepted on a completion report or a halt
  card, and never from the model.
- **`APPROVED`/`REJECTED`/`REVISE`**: report what the human actually said. A `REVISE` the model
  reports itself is always a whole-plan revision; only the approval page can express a per-task
  one, because only there can the human point at a specific task.
- **What a per-task `REVISE` means depends on when it arrives (1.13.0).** Before execution it
  rewrites those tasks' wording (`pending_revision` + `task_updates`). On the **completion
  report** it means *do them again*: the named tasks reopen as `PENDING` carrying the human's
  sentence, every other task keeps its `DONE` and evidence, and the plan goes back to
  `IN_EXECUTION` **without re-approval** — the task list never changed. `_mutate_no` is the
  single place that reading is chosen. See [06](06-human-in-the-loop.md#rework-1130) and
  [D17](09-defects-and-lessons.md#d17).
- **Asked about a plan that is no longer waiting (3.1.0, D30).** A decision the human made
  between two calls is applied at the top of this one. `ASK_USER` then answers with it
  instead of asking again: `COMPLETED` → the confirmation; `DRAFTING` with tasks → the
  message that changes were asked for, with `user_comment`, `revision_scope` and
  `revision_targets`, exactly as when the decision is collected by the waiting call.
- **Approval binds to the exact version shown** (goal + task-title fingerprint). Approving a
  plan version the human never saw → `APPROVAL_NOT_REQUESTED`. An approval left idle past
  `approval_ttl` → `APPROVAL_EXPIRED`. A decision sent while the request is still open on the
  page did not come from the user at all → `APPROVAL_PENDING` (see
  [06](06-human-in-the-loop.md)); the same code is returned when a chunked wait slice ends
  undecided, carrying `waited_seconds` and `remaining_seconds`.

## 3. `update_task_progress` — execution tracking + the enforced gate

Required: `task_id`, `status` ∈ {`PENDING`,`IN_PROGRESS`,`DONE`,`FAILED`}.
Optional: `result_log`, `plan_id`, `files` (3.0.0, only where the server checks files).

- **The enforcement half of the HITL gate.** Until `plan_status` is `APPROVED`/`IN_EXECUTION`,
  every call returns `ok:false` / `PLAN_NOT_APPROVED`. The model cannot execute early even if it
  ignores the instruction. Since 1.16.0, if a human has the approval request open, the call
  waits on them first (and returns their decision if they make one) rather than bouncing back as
  fast as the model can retry. A halted plan refuses every call with `LOOP_HALTED`.
- **What was picked is what runs (2.0.0).** At approval the chosen option becomes the task's
  `title`. `next_task` carries `chosen_by_user` (`recommended` / `alternative`) and the choice's
  `choice_reason`, and when the task picked was an alternative, every hint that hands the task
  over says so (`choice_note`, like `_rework_suffix`). **No response to the model ever contains
  an option that was not picked** - `Task.brief()` has no options, only `page_brief()` (the
  page's view) does. An alternative the model can still see is one it may still do: the D19
  lesson, applied before the fact. `TestTheUnchosenStayUnseen` scans a whole lifecycle for it.
- `IN_PROGRESS` before the work, `DONE`/`FAILED` after. With `auto_advance` on (default) the
  server puts the next task into `IN_PROGRESS` as part of accepting a `DONE`, so only the first
  task needs an explicit start: a 5-task plan costs 1 + 5 calls instead of 5 + 5. The `DONE`
  guard is untouched — a task still has to be the one in progress, in order, with evidence —
  because the boundary that guard relies on is the `DONE` call itself, not the `IN_PROGRESS` one.
  Set `PLANNING_MCP_AUTO_ADVANCE=false` to require both calls; the tool description follows.
- Out-of-order starts are **redirected**, not rejected. Duplicate `DONE` is idempotent, and a
  redundant `IN_PROGRESS` on a running task is accepted without resetting `started_at`.
- **The contract at DONE (3.0.0).** `next_task` carries the task's `done_when` (and
  `done_when_by: "user"` when the human wrote it), and the hint restates it. A `result_log` that
  only says the criterion back is refused (`MISSING_RESULT_LOG`); a file listed in `files` that
  is not there, or is empty, refuses the DONE (`FILE_NOT_FOUND`, naming the folders the server
  looks in). After that refusal the model may save the file and resend, correct the path, or
  send DONE without the file - the last is accepted and shown to the human as a dropped claim.
  `files` sent to a server with no allowed folders is ignored with a note. What the server
  found is never in a response to the model. See
  [04](04-state-machine.md#the-verification-contract-300).
- `FAILED` → `plan_status: BLOCKED`; `next_action: CALL_PLAN_AND_THINK`. Since 3.0.0 the
  response names the finished tasks (`tasks_unchanged`) and the hint asks for a repair of the
  failed task alone, not a re-plan. Attempting another task while `BLOCKED` → `PLAN_BLOCKED`,
  with the same hint.
- All tasks `DONE` → `COMPLETED`, `next_action: ANSWER_USER`.

**The last `DONE` is the completion report (3.1.0).** With `PLANNING_MCP_AUTO_ASK` on, the
call that finishes the last task opens the completion request and waits. Its response is
the human's answer - `COMPLETED` (`ANSWER_USER`), a rework (`reopened_tasks`, `next_task`)
- or `APPROVAL_PENDING` with "That task is DONE and every task in this plan is finished".
It carries no `tasks`, like every execution response.

**Requests from the run card (3.1.0).** A stop or a note the human left on the approval
page is applied by this tool, where the agent reports a task: after a `DONE` is recorded
and before the next task starts, or before an `IN_PROGRESS` takes effect.
- A stop answers `ok:false` with `PLAN_PAUSED` (or `APPROVAL_PENDING` while a slice of the
  wait remains) and the message "That task is DONE. The user paused this plan before the
  next task." Until the human continues or cancels, every tool but `get_current_plan`
  answers `PLAN_PAUSED`.
- A note answers `ok:true`, `plan_status: DRAFTING`, `user_comment`, `tasks_unchanged`,
  and a hint with the exact `task_updates` argument for the first unfinished task.
- After the last task, or together with a `FAILED`, there is nothing to apply: see
  [06](06-human-in-the-loop.md#the-run-card-310).

## 4. `get_current_plan` — always-safe recovery

Required: `plan_id`. Send the id the session has been receiving in every response and the read
is exact — a session always gets **its own** plan back, including a plan already `COMPLETED` or
`CANCELLED`. The constant `"current"` is the fallback for a session that does not know its id
yet, and it is a *guess*: it resolves only while exactly one plan is live. Never mutates, never
errors. Returns goal, recent thinking steps (superseded ones summarized), tasks with their
**full** `result_log`, progress, approval record, `next_action_hint` naming the exact next call -
and since 1.16.0 `halted` (why the plan is paused), `draft_tasks`, and `thinking_steps_left`, so a
model re-reading its plan after a cut-off generation resumes from the draft instead of
re-deriving it.

**Not-my-plan answers must not read as "no plan" (1.15.1).** Both `"current"` under concurrency
and an unknown/stale `plan_id` return an `active_plans` directory rather than guessing. They stay
`ok: true`, but they carry `PLAN_AMBIGUOUS` so `next_action` resolves through the state machine to
`CALL_GET_CURRENT_PLAN`. Without the code they fell through to the `plan is None` default,
`CALL_PLAN_AND_THINK` — *"There is no active plan. Start one."* — which contradicted the message
beside it and turned "let me re-read my plan" into a forked duplicate plan. The parameter existed
and worked the whole time; the tool description told the model to send `"current"`, so it never
used it.

**Evidence is never truncated (1.13.1).** `Task.brief` used to cut `result_log` at 200
characters "to protect context". The approval page is built from that same dict — handlers pass
`tasks_brief()` straight to `open_request` — so a completion report asked a human to certify
work whose evidence ended in `...`, while the whole text sat in `plan_state.json`. If evidence
ever does threaten context, cap it where it is **written** (a maximum beside `min_result_log`),
never where it is read: a limit at the read point silently disagrees with what the store holds.

---

## Error codes → corrective next_action

Every error maps to a `next_action` that tells the model how to recover. Full list in
[data/enums.json](data/enums.json). The important ones:

| error_code | Meaning | Recovery next_action |
|---|---|---|
| `PLAN_NOT_APPROVED` | executing before approval | `CALL_REQUEST_USER_APPROVAL` |
| `PLAN_NOT_READY` | approving a plan with no task list yet | `CALL_PLAN_AND_THINK` |
| `PLAN_BLOCKED` | a task failed; must re-plan | `CALL_PLAN_AND_THINK` |
| `MISSING_TASK_LIST` | finalized without a task list | `CALL_PLAN_AND_THINK` |
| `APPROVAL_NOT_REQUESTED` | approving a version never shown | `CALL_REQUEST_USER_APPROVAL` |
| `APPROVAL_EXPIRED` | approval idle past TTL | `CALL_REQUEST_USER_APPROVAL` |
| `APPROVAL_PENDING` | the user has not answered yet — either this wait slice ended, or the model tried to decide on their behalf | `CALL_REQUEST_USER_APPROVAL` (with `ASK_USER`, immediately) |
| `PLAN_AMBIGUOUS` | several plans active, no `plan_id` given | `CALL_GET_CURRENT_PLAN` |
| `PLAN_EVICTED` | the `plan_id` sent names a plan the server closed to make room - it was the least recently used unfinished plan when the limit was reached (3.0.0) | `ANSWER_USER` (tell the user; start again only if they ask; never use another plan in its place) |
| `GOAL_NOT_MATCHED` | continuing (step>1) but goal matches no plan (drift) | `CALL_PLAN_AND_THINK` (with the exact goal from `active_plans`) |
| `TASK_NOT_FOUND` | bad `task_id` | `CALL_UPDATE_TASK_PROGRESS` (with valid ids listed) |
| `MISSING_RESULT_LOG` | `DONE` with evidence that is empty, a bare claim, or the task title | `CALL_UPDATE_TASK_PROGRESS` |
| `REWORK_NOT_DONE` | a reopened task reported `DONE` with the very outcome the user rejected | `CALL_UPDATE_TASK_PROGRESS` (quoting what they asked for) |
| `FILE_NOT_FOUND` | `DONE` listed a file the server looked for and did not find (3.0.0) | `CALL_UPDATE_TASK_PROGRESS` (save it, fix the path, or drop the claim) |
| `REVISION_INCOMPLETE` | no flagged task was rewritten - or, in a repair, the failed task was not | `CALL_PLAN_AND_THINK` (with the exact `task_updates` argument) |
| `LOOP_HALTED` | the circuit breaker paused this plan (or, with no plan, stopped a repeating call) | `CALL_REQUEST_USER_APPROVAL` (`ASK_USER`) before the human has been shown the halt; `STOP_AND_WAIT_FOR_USER` after |
| `PLAN_PAUSED` | the user stopped this plan from its run card (3.1.0); held exactly like a breaker halt | `CALL_REQUEST_USER_APPROVAL` (`ASK_USER`) until the pause card has been shown; `STOP_AND_WAIT_FOR_USER` after |
| `INTERNAL_ERROR` | something unexpected | `CALL_GET_CURRENT_PLAN` (resync) |

## Input leniency (invisible to the model)

Applied by `leniency.normalize()` before validation, so near-miss calls succeed:
case/alias normalization (`done|완료`→`DONE`, `네|yes`→`APPROVED`, …); `"true"/1/"3"` coercion;
`task_list` as a newline/comma string → array; array-of-objects → titles; numbering prefixes
stripped; unknown keys dropped; non-string keys dropped; NaN/inf rejected for integer fields.
3.0.0: `done_when` as `{"2": "..."}`, `"2: ..."` lines or a misnamed field (`criteria`,
`acceptance_criteria`); `files` as a JSON string, objects with `path`, or a newline / semicolon
separated string - **not** comma-separated, since a comma can be part of a file name.

> **Client caveat:** some MCP clients validate enums *before* the server sees them, so enum
> aliases like `done`/`진행중` may be rejected with `-32602` at the client. Non-enum leniency
> (task_list shapes, number coercion) works everywhere. Reinforce exact UPPERCASE enum values
> in the system prompt.
