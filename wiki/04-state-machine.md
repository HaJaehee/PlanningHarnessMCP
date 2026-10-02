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
| `IN_EXECUTION` | `update_task_progress` FAILED | `BLOCKED` | — |
| `BLOCKED` | `plan_and_think` | `DRAFTING` (same plan_id) | — |
| `BLOCKED` | `update_task_progress` on another task | *(no change)* | `PLAN_BLOCKED` |
| `DRAFTING` | `plan_and_think` (more, **budget spent, draft kept**) | `AWAITING_APPROVAL` - draft submitted, straight to the page (1.16) | — |
| `DRAFTING` | `plan_and_think` (more, **budget spent, no draft**) | `DRAFTING` + `halt` | — |
| `AWAITING_APPROVAL` (no request open) | `plan_and_think` without `revised_goal` | *(no change)* - redirected to approval (1.16) | — |
| `AWAITING_COMPLETION` (no request open) | `plan_and_think` | *(no change)* - redirected to the completion report (1.16, D26) | — |
| any, **request open on the page** | `plan_and_think` / `update_task_progress` on that plan | *(waits on the human; returns their decision)* (1.16) | undecided → `APPROVAL_PENDING` / `PLAN_NOT_APPROVED` / `COMPLETION_PENDING` / `LOOP_HALTED` |
| `COMPLETED` < cooldown | `plan_and_think` with the same goal | *(no change)*, `ANSWER_USER` + results (1.16) | — |
| any non-terminal | circuit breaker trips | same status + **`halt` overlay** | — |
| halted | any tool | *(no change)* | `LOOP_HALTED` (or waits, if the halt card is open) |
| halted | human: 이 초안으로 승인 | `APPROVED` - the draft becomes the task list | — |
| halted | human: 계속 진행 (+ direction) | same status, `guidance` set; DRAFTING gets a fresh budget | — |
| halted | human: 취소 | `CANCELLED` | — |
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
