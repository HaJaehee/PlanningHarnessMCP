# 04 · State Machine

Authoritative logic: `planning/state_machine.py` + the transition checks in
`planning/handlers.py`. Machine-readable form: [data/state-machine.xml](data/state-machine.xml).

## Plan statuses

`NONE → DRAFTING → AWAITING_APPROVAL → APPROVED → IN_EXECUTION → AWAITING_COMPLETION →
COMPLETED`, with `BLOCKED` (task failed) and `CANCELLED` (rejected) as branches. `COMPLETED`
and `CANCELLED` are terminal. `AWAITING_COMPLETION` (1.9.0) is the human's verification of the
finished work — see below.

```
                    plan_and_think (need_more_thinking=true)
                              ┌───────┐
                              ▼       │
   NONE ──plan_and_think──► DRAFTING ─┘
                              │ plan_and_think(final + task_list)
                              ▼
                     AWAITING_APPROVAL ──approval(REJECTED)──► CANCELLED
                       │  ▲                                       │
          approval     │  │ approval(REVISE) → DRAFTING           │ (new goal → new plan)
          (APPROVED)   ▼  │
                     APPROVED
                       │ update_task_progress(IN_PROGRESS) on first task
                       ▼
                  IN_EXECUTION ──update_task_progress(FAILED)──► BLOCKED
                       │                                           │ plan_and_think
                       │ all tasks DONE                            ▼
                       ▼                                        DRAFTING
                   COMPLETED
```

## Transition table (server-enforced)

| From | Call | To | If illegal → |
|---|---|---|---|
| `NONE`/`COMPLETED`/`CANCELLED` | `plan_and_think` step 1 | `DRAFTING` (new plan_id) | — |
| `DRAFTING` | `plan_and_think` (more) | `DRAFTING` | — |
| `DRAFTING` | `plan_and_think` (final+task_list) | `AWAITING_APPROVAL` | no task_list → `MISSING_TASK_LIST` |
| `AWAITING_APPROVAL` | `approval` ASK_USER | `AWAITING_APPROVAL` (+ blocking wait) | — |
| `AWAITING_APPROVAL` | `approval` APPROVED | `APPROVED` | version not shown → `APPROVAL_NOT_REQUESTED` |
| `AWAITING_APPROVAL` | `approval` REVISE (scope `PLAN`) | `DRAFTING` | — |
| `AWAITING_APPROVAL` | `approval` REVISE (scope `TASKS`) | `DRAFTING` + `pending_revision` | — |
| `DRAFTING` + `pending_revision` | `plan_and_think` (final+`task_updates`) | `AWAITING_APPROVAL` (only flagged tasks rewritten) | no target addressed → `REVISION_INCOMPLETE` |
| `DRAFTING` *without* `pending_revision` | `plan_and_think` (final+`task_updates`) | *(no change)* | `REVISION_NOT_REQUESTED` |
| `AWAITING_APPROVAL` | `approval` REJECTED | `CANCELLED` | — |
| `AWAITING_APPROVAL`/`DRAFTING` | `update_task_progress` | *(no change)* | `PLAN_NOT_APPROVED` ← **critical guard** |
| `APPROVED` | `update_task_progress` IN_PROGRESS | `IN_EXECUTION` | — |
| `IN_EXECUTION` | `update_task_progress` DONE (not last) | `IN_EXECUTION`, next task auto-started `IN_PROGRESS` | — |
| `IN_EXECUTION` | `update_task_progress` DONE (last) | `AWAITING_COMPLETION` | — |
| `AWAITING_COMPLETION` | `approval` APPROVED | `COMPLETED` | — |
| `AWAITING_COMPLETION` | `approval` REVISE (scope `TASKS`) | **`IN_EXECUTION`**, only the named tasks reopened — *no re-approval* (1.13.0) | — |
| `AWAITING_COMPLETION` | `approval` REVISE (scope `PLAN`) | `DRAFTING` + `rework_from_completion` | — |
| `DRAFTING` + `rework_from_completion` | `plan_and_think` (final+`task_list`) | `AWAITING_APPROVAL`, surviving tasks keep `DONE` + `result_log` | — |
| `AWAITING_APPROVAL` | `approval` APPROVED **when every task is already DONE** | `AWAITING_COMPLETION`, not `APPROVED` (1.13.0) | — |
| `AWAITING_COMPLETION` | `update_task_progress` | *(no change)* | `COMPLETION_PENDING` |
| `IN_EXECUTION` | `update_task_progress` FAILED | `BLOCKED` + `pending_revision` (`origin: failure`) naming the failed task (3.0) | — |
| `BLOCKED` | `plan_and_think` | `DRAFTING` (same plan_id) | — |
| `BLOCKED`/`DRAFTING` + failure marker | `plan_and_think` (final+`task_updates`) | `AWAITING_APPROVAL` - the failed task (and any later unfinished task sent) rewritten, `DONE` tasks untouched (3.0) | failed task not rewritten → `REVISION_INCOMPLETE` |
| `BLOCKED`/`DRAFTING` + failure marker | `plan_and_think` (final+`task_list`) | `AWAITING_APPROVAL` - whole re-plan, finished work not carried, audited `repair_ignored` | — |
| `IN_EXECUTION` | `update_task_progress` DONE naming a file that is not there (3.0) | *(no change)* | `FILE_NOT_FOUND` |
| `IN_EXECUTION` | `update_task_progress` DONE whose `result_log` only repeats `done_when` (3.0) | *(no change)* | `MISSING_RESULT_LOG` |
| `BLOCKED` | `update_task_progress` on another task | *(no change)* | `PLAN_BLOCKED` |
| `DRAFTING` | `plan_and_think` (more, **budget spent, draft kept**) | `AWAITING_APPROVAL` - draft submitted, straight to the page (1.16) | — |
| `DRAFTING` | `plan_and_think` (more, **budget spent, no draft**) | `DRAFTING` + `halt` | — |
| `AWAITING_APPROVAL` (no request open) | `plan_and_think` without `revised_goal` | *(no change)* - redirected to approval (1.16) | — |
| `AWAITING_COMPLETION` (no request open) | `plan_and_think` | *(no change)* - redirected to the completion report (1.16, D26) | — |
| any, **request open on the page** | `plan_and_think` / `update_task_progress` on that plan | *(waits on the human; returns their decision)* (1.16) | undecided → `APPROVAL_PENDING` / `PLAN_NOT_APPROVED` / `COMPLETION_PENDING` / `LOOP_HALTED` |
| `COMPLETED` < cooldown | `plan_and_think` with the same goal | *(no change)*, `ANSWER_USER` + results (1.16) | — |
| any non-terminal | circuit breaker trips | same status + **`halt` overlay** | — |
| halted | any tool | *(no change)* | `LOOP_HALTED` (or waits, if the halt card is open) |
| halted | human: 이 초안으로 승인 | `APPROVED` - the draft becomes the task list, with the draft's choices applied (2.0) | — |
| halted | human: 계속 진행 (+ direction) | same status, `guidance` set; DRAFTING gets a fresh budget | — |
| halted | human: 취소 | `CANCELLED` | — |
| `AWAITING_APPROVAL` | human approves with **choices** (2.0) | `APPROVED`; each task with options gets `chosen`, and the picked option becomes its `title`. A choice already made on an earlier approval is kept (3.0, D27) | a pick not on screen → nothing recorded (page) / recommendation kept (chat) |
| `AWAITING_APPROVAL` | human approves with **criteria** (3.0) | `APPROVED`; each named task's `done_when` becomes what they wrote, `done_when_by = user` | a task not on screen, or already `DONE` → nothing recorded |
| any unfinished, **idle ≥ `evict_min_idle`, least recently used** | another conversation starts a plan while `max_active_plans` are active (3.0) | *(removed)* - evicted; remembered by id, evidence in the audit log | every active plan in use → the new plan is refused, `PLAN_AMBIGUOUS` |
| *(evicted)* | any tool with that `plan_id` | *(no change)* | `PLAN_EVICTED` → `ANSWER_USER` |
| any | `get_current_plan` | *(no change)* | never fails |

## Deliberate leniencies (rejecting these would strand a weak model)

- **Out-of-order task start** → redirected to the correct `task_id`, `ok:true`.
- **Duplicate `DONE`** → idempotent, points at the next pending task.
- **Redundant `IN_PROGRESS` on a task already running** → accepted, `started_at` preserved. This
  is the normal shape once auto-advance is on: the server started the task and the model sends
  the call anyway, out of habit or because its prompt predates the change.
- **`plan_and_think` while `APPROVED`/`IN_EXECUTION` with the *same* goal** → not a new plan;
  redirect to the in-flight task.

## Task completion is enforced, not asserted (1.9.0)

The field failure: a small model marked every task `DONE` in a row without doing the work,
and reported the plan finished. `DONE` is a *claim*, so it is now checked:

| Refusal | error_code |
|---|---|
| the task was never `IN_PROGRESS` | `TASK_NOT_STARTED` |
| an earlier task is unfinished | `TASK_OUT_OF_ORDER` |
| `result_log` is empty, a bare success claim ("완료", "done", "ok"), or just the task title | `MISSING_RESULT_LOG` |

Length alone cannot judge evidence — Korean fits a real outcome into ~8 characters — so the
check is content-based (an exact-match phrase filter plus a low length floor,
`PLANNING_MCP_MIN_RESULT_LOG`, default 8 normalized characters). Starting the wrong task is
still forgiven with a redirect; *claiming* the wrong task is finished is not.

Because each `DONE` now requires the task to be the one in progress, and starting a task already
enforces order, batch-marking is structurally impossible.

## Auto-advance (1.11.0)

Accepting a `DONE` also puts the next task into `IN_PROGRESS` (`PLANNING_MCP_AUTO_ADVANCE`,
default on). This is a round-trip cut, not a relaxation. The `IN_PROGRESS` call was never the
thing stopping a model from claiming work it had not done — the evidence check is — and the turn
boundary it provided survives: the model still receives one "task N is in progress, do the work"
instruction per task and still cannot claim `DONE` for it until a later call, with its own
`result_log`. What disappears is one empty round trip per task: 5 tasks cost 6 execution calls
instead of 10, which is context and error surface a small model does not have to spend.

Rejected alternative: a `batch_update` that marks several tasks `DONE` in one call. It would
delete the enforcement outright — the failure mode in the section above is exactly a model
firing `DONE` at every task at once — and the models it was proposed to help are the ones most
likely to do it.

**And `COMPLETED` is no longer self-awarded.** When the last task is `DONE` the plan enters
`AWAITING_COMPLETION`; the model must call `request_user_approval(ASK_USER)`, which shows the
human a **completion report** of every task with its `result_log`. Only the human's `APPROVED`
closes the plan (`REJECTED` → `CANCELLED`). This is
the only check that catches invented `result_log` text, since the server cannot observe work.
The approval fingerprint covers each task's status and evidence, so a report cannot be
rewritten after the human saw it. Set `PLANNING_MCP_COMPLETION_APPROVAL=false` to go straight
to `COMPLETED` as before.

Anti-abandonment: while any task remains, every `next_action_hint` names how many are left and
says not to tell the user the work is finished.

**`REVISE` on a completion report is rework, not re-planning (1.13.0).** The human is not
disputing the task list there — they approved it and it has not changed — they are saying that
what came out of specific tasks is not good enough. So the named tasks go back to `PENDING` with
the human's sentence on them (`revision_note`) and their old output kept (`previous_result_log`),
every other task keeps its `DONE` and its evidence, and the plan returns to `IN_EXECUTION`
**without a second approval**. Nothing about ordering changes, so `can_start_task`,
`unfinished_before`, `current_task`, `_auto_advance` and `all_done` all work unmodified: a
reopened task is simply the next `PENDING` one, and finishing it walks back into
`AWAITING_COMPLETION` for a fresh report.

Treating that as a redraft is what defect [D17](09-defects-and-lessons.md#d17) was: `DRAFTING`
with every task `DONE`, the generic re-plan hint, and — because finalizing replaces `plan.tasks`
— every `result_log` deleted. The whole-plan checkbox still exists for add/delete/reorder, which
genuinely needs a new plan and a new approval, but it now carries evidence across the redraft for
tasks whose title survives (`_carry_evidence`, matched with `title_key`).

## Loop convergence (1.16.0)

Every guard above answers "is this call legal?". A thinking model caught in its own
self-verification ([D25](09-defects-and-lessons.md#d25)) makes calls that are each perfectly
legal - one more thinking step, one more re-plan - so 1.16 adds the question "is this the twelfth
call in a row that changed nothing?", and answers it with enforcement rather than wording.

**The thinking budget** bounds a drafting round (`step_budget_end`, set on the round's first
step; 0 = not started). The hint counts down and names the exit first. When it runs out the
server does what the model would not: with a draft it submits the draft; without one it halts.
Refusing would only have been one more thing for the model to reconsider.

**The circuit breaker** (`planning/loopguard.py`) counts per plan, since the last *milestone* -
a finalize, a human decision, a task DONE or FAILED, an approval request asked:

| trips on | default | env |
|---|---|---|
| calls with no milestone | 12 | `PLANNING_MCP_BREAKER_CALLS` |
| the same call (same normalized arguments) in a row | 3 | `PLANNING_MCP_BREAKER_REPEAT` |
| the same `error_code` in a row - a turned-away re-plan counts as one | 4 | `PLANNING_MCP_BREAKER_ERROR_STREAK` |
| DRAFTING plans from the last 10 min whose goals are rewordings of one another (bigram Jaccard ≥ 0.5) | 3 | `PLANNING_MCP_BREAKER_RESPAWN` |
| thinking budget spent with no draft | - | `PLANNING_MCP_MAX_THINKING_STEPS` |

Never counted: a call that waited on a human (a chunked slice repeats the very same call on
purpose) and any call made while a human has a request open for that plan. The counters live in
process memory on purpose: they measure one agent's consecutive calls, and one agent talks to
one process; the respawn count, which spans plans, is read from the shared state so unrelated
conversations in one process never add up. The **halt** itself is written to the plan.

A halt is an **overlay**, not a status: it can land in DRAFTING, AWAITING_APPROVAL, IN_EXECUTION
or AWAITING_COMPLETION, and lifting it must return the plan to where it was. While it is set,
`_halt_action` is the only producer of `next_action`: `CALL_REQUEST_USER_APPROVAL` (ASK_USER)
until the human has been shown it, then `STOP_AND_WAIT_FOR_USER`. A loop that has no live plan
(a routing error over and over, or re-planning a COMPLETED goal) cannot be paused, so it ends in a
plain `LOOP_HALTED` + `STOP_AND_WAIT_FOR_USER` with a `display_to_user`.

What the breaker does not see: a loop inside one generation. See
`docs/thinking-model-hosts.md` for the host-side half.

## Choices are decided at approval (2.0.0)

A task's options are part of the plan the human approves. The fingerprint includes them -
only for tasks that have them, so a plan with no choices keeps exactly its 1.16 fingerprint
and a request left on the page across a rolling upgrade still matches. Approval applies the
pick in the same transaction (`_apply_choices`, audit `choices_applied`): every task that
offered a choice gets a `chosen` (keeping the recommendation is a decision too), and the
picked option becomes the task. From then on the task is an ordinary task - ordering,
evidence and rework guards are untouched. A completion report takes no choices; a rework
redoes the option that was chosen.

A choice is decided **once**. A later approval of the same plan - after a repair, or after an
approval expired - arrives with no pick for it (the page offers a choice only while it is
undecided), and that must not be read as "the recommendation": a decided task keeps its choice,
and a `DONE` task is never re-decided ([D27](09-defects-and-lessons.md#d27)).

## The verification contract (3.0.0)

Logic: `planning/evidence.py` (pure) + `handlers._finish_task`. Design and decisions:
[`docs/plan-3.0-verification-contract.md`](../docs/plan-3.0-verification-contract.md).

A task may carry `done_when` - one sentence saying what exists or is true when it is finished.
The model proposes it (`plan_and_think.done_when`, `{task_id, check}`), the human approves it
with the plan and may write it themselves on the approval page ([06](06-human-in-the-loop.md)),
and from then on it is part of the task: handed over in `next_task` and the hint at the moment
the task starts, shown beside the evidence on the completion report, and included in the
fingerprint (only where a task has one - a plan without criteria keeps its 2.0 fingerprint).

`DONE` gains two refusals, after the 1.9.0 ones:

| Refusal | error_code | Why it is safe to refuse |
|---|---|---|
| a file listed in `files` is not in an allowed folder's tree, or is empty | `FILE_NOT_FOUND` | the model said the task produced it; the server looked |
| `result_log` adds less than `PLANNING_MCP_EVIDENCE_NOVELTY` (0.3) of new text to `done_when` | `MISSING_RESULT_LOG` | the criterion said back is the criterion, not evidence that it was met |

Everything short of that is **shown to the human, not refused** - the 1.9.0 rule, kept: a wrong
refusal costs a weak model a retry loop, and both refusals are bounded by the 1.16 breaker (a
refused call repeated halts the plan for a human).

- **File checks** run only inside `PLANNING_MCP_ARTIFACT_ROOTS` (`;`-separated; empty = no
  checks, and `files` is not advertised). A path outside - `..`, another drive, UNC, a link out
  of the folder - is recorded `outside` and **nothing on disk is touched for it**. Inside, the
  server calls `os.stat` and nothing else. States: `found` (changed since the task started),
  `old` (there, but untouched since before it started), `folder`, `empty`, `missing`,
  `outside`, `unknown` (the check did not come back within 3 s - never a refusal). A relative
  path is tried under each root; on Windows a rooted path with no drive (`/workspace/out.csv`,
  a sandbox's view) is tried under each root with its leading folders dropped one at a time.
- **A path named only in `result_log`** is checked too, but only a positive finding is kept:
  "removed out/tmp.csv" is a true sentence about a file that is not there.
- **A file that was already there** (`old`) proves the file exists, not that the task did
  anything. It is shown, but it cannot stand in for evidence: only a `found` / `folder` check
  lets a `result_log` that repeats the criterion through.
- **A dropped claim is allowed and visible.** After `FILE_NOT_FOUND` the model may send `DONE`
  without the file (the task may truly have produced none); the task then carries
  `claims_withdrawn`, audited `file_claim_withdrawn`, and the page says so.
- **The repeat check** is the share of the evidence's character bigrams that are not in the
  criterion - no tokenizer, works for Korean. The threshold was set from hand-written pairs
  (`TestTheRepeatCheck`): said back with only the tense changed scores 0.0-0.25; adding a
  count, a size or a name scores 0.33 and up. 0.3 refuses the first group only. Evidence under
  0.5 is marked on the completion page (`echo`) instead of refused. Every `DONE` under a
  criterion records its score (`task_done.novelty`), so the threshold can be tuned from the
  field.
- **Before the completion report is shown**, confirmed files are looked at once more; one that
  has gone is marked (`gone`, audited `file_gone_before_completion`). Only path and state are
  in the fingerprint - a human opening and saving a file while reading the report changes its
  size and mtime, and must not void the request they are answering.
- **What the server found is for the human.** `checks`, `claims_withdrawn` and `echo` are in
  `page_brief()` only; no response to the model carries them.

What this does **not** do: judge whether the content is right. A file exists or it does not. The
boundary of [06](06-human-in-the-loop.md#rework-1130) has moved, not gone.

<a id="local-repair-300"></a>
## Local repair (3.0.0)

A `FAILED` task used to leave one move: re-plan the whole task list, which drops the evidence of
every task already finished ([D28](09-defects-and-lessons.md#d28)). Now `_fail_task` flags the
failed task the way a human's comment flags one:

```
pending_revision = {"targets": {"3": "<the failure's result_log>"},
                    "origin": "failure", "open": [4, 5]}
```

and the per-task machinery of 1.10 does the rest, under three rules that differ from a human's
revision:

| | human's per-task revision (1.10) | repair after a failure (3.0) |
|---|---|---|
| must be rewritten | none (an unaddressed target is noted) | the failed task - else `REVISION_INCOMPLETE`, nothing written |
| may also be rewritten | nothing | the unfinished tasks after it (`open`) |
| a `DONE` task | reset if the human flagged it | out of reach; an edit is dropped with a note |
| marker on the rewritten task | `revision_note` (their comment) | `failure_note` (why it failed), kept until the plan ends |

The hint hands over the literal argument (`task_updates=[{"task_id": 3, "title": "<another way
to do this task>"}]`) from every tool - the `FAILED` response, a stray `update_task_progress`
(`PLAN_BLOCKED`), `request_user_approval`, `get_current_plan` - and names only `plan_and_think`.
The failure text is clipped to 200 characters there; the task keeps it whole.

After the rewrite the plan is `AWAITING_APPROVAL` like any changed plan: the human sees the old
wording struck through, the reason, and the `DONE` rows that stay. On approval `current_task()`
is the repaired task - the tasks before it are `DONE`, so the ordering guards need no change -
and `next_task.failure_note` plus the hint say why it reads differently. Sending the same
wording again is a valid repair (the failure may have been passing).

A whole `task_list` is still accepted - sometimes the approach was wrong, not the step - with the
2.0 consequences (finished work is not carried), a note saying so, and an audited
`repair_ignored`; `task_repaired` against `repair_ignored` is the field measure of whether a
model follows the hint. `PLANNING_MCP_LOCAL_REPAIR=false` restores the 2.0 path and removes the
repair text from the tool descriptions.

Out of scope, as since 1.10: adding, deleting or reordering tasks in a repair. One step replaced
by two still needs the whole-plan path.

## Two time-based / version-based guards (added after real bugs)

- **Approval expiry** (1.4.0): an `APPROVED`/`IN_EXECUTION` plan left idle past `approval_ttl`
  (default 1800 s) has its approval revoked on the next touch → back to `AWAITING_APPROVAL`,
  audited `approval_expired`. Stops a morning approval from authorizing afternoon execution.
  An unreadable timestamp counts as expired (fail-safe).
- **Approval binding** (1.4.0): any change to the task list clears the pending approval, so
  `APPROVED` for a version the human never saw is refused with `APPROVAL_NOT_REQUESTED`.

## next_action decoder (what the model does with each)

| next_action | Model action |
|---|---|
| `CALL_PLAN_AND_THINK` | call `plan_and_think` |
| `CALL_REQUEST_USER_APPROVAL` | call `request_user_approval` |
| `CALL_UPDATE_TASK_PROGRESS` | call `update_task_progress` |
| `CALL_GET_CURRENT_PLAN` | call `get_current_plan` |
| `STOP_AND_WAIT_FOR_USER` | print `display_to_user`, end the turn |
| `ANSWER_USER` | write the final answer |

When more than one plan is active, hints are **qualified**: the `next_action_hint` names the
`plan_id` to include, so a model that copies the hint verbatim routes correctly.
