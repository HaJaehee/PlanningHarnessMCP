# 05 · Concurrency and Sessions

This is the hardest-won part of the project. Read it before touching `store.py`,
`approval.py`, `filelock.py`, or the routing logic in `handlers.py`. Most defects in
[09](09-defects-and-lessons.md) live here.

## The setup that makes concurrency real

MCP has **no session identifier** — the server cannot tell which call came from which
conversation. Worse, AnythingLLM/Claude Code leave **old server processes alive** on the same
state directory after a restart (observed: 2–3 ghosts). And a blocking approval occupies one
handler for as long as the human takes. So concurrency is not theoretical; it is the normal
operating condition.

## Multi-plan routing (1.8.0)

There used to be a single `active_plan_id`, so two conversations evicted each other's plans.
Now plans coexist, and a call is routed three ways, in priority order:

1. **Explicit `plan_id`** on `request_user_approval` / `update_task_progress` / `get_current_plan`.
2. **By goal**, for `plan_and_think` — the model repeats the same goal each step, so a matching
   active plan is this session's. A different goal gets a fresh plan; it never touches another's.
3. **The only active plan**, when there is exactly one — the ordinary case, so a model that
   never learns `plan_id` behaves exactly as before.

If several plans are active and no `plan_id` is given → `PLAN_AMBIGUOUS` with an `active_plans`
directory. A weak model recovers because (a) it only ever saw *its own* plan_id in prior
responses, and (b) while multiple plans are live, every `next_action_hint` names the plan_id to
include (`qualify=True`). `PLANNING_MCP_MAX_ACTIVE_PLANS` (default 20 since 1.16.0, was 5) caps how many can coexist.

**At the limit, the least recently used unfinished plan is evicted (3.0.0).** Until then a full
table refused every new plan (`PLAN_AMBIGUOUS`) until a human rejected an old one on the approval
page - and a conversation that was abandoned never comes back to do that. With the limit at 20,
the slots fill with exactly such plans. Now `_evict_for_room` removes the plan written to longest
ago (`updated_at`) and the new plan takes the slot. `PLANNING_MCP_EVICT_LRU=false` restores the
refusal.

- **"Used" is the last write, and a plan in use is never evicted.** An agent waiting on approval
  touches its plan every wait slice (45 s); an executing one at every task. A plan idle for less
  than `PLANNING_MCP_EVICT_MIN_IDLE` (300 s) therefore has somebody on it and is skipped; if
  every active plan is that fresh, the new plan is refused as before. A read
  (`get_current_plan`) is deliberately not a use - touching on read would also un-expire an
  approval.
- **The floor is also the brake on a runaway.** A model that keeps opening plans evicts idle ones
  until its own fresh plans hold every slot, and then it is refused: at most
  `max_active_plans` evictions, never an endless churn (`TestAPlanInUseIsNeverEvicted`).
- **Any unfinished status can go**, a halted plan and one waiting on a completion report
  included; a finished plan never counts and is never the victim (retention handles those).
- **Nothing is lost silently.** The `plan_evicted` audit line carries the plan's goal, status,
  idle time and every task with its `result_log`; its request is withdrawn from the approval
  page; and the state file remembers the id (`evicted`, the newest 50).
- **A conversation that comes back is told the truth.** Any tool called with an evicted
  `plan_id` answers `PLAN_EVICTED` → `ANSWER_USER`: the plan was closed for inactivity, tell
  the user and ask whether to do it again. Without the remembered id it would be an *unknown*
  id, and the answer for those - "do not start a new plan, use one of these", followed by other
  conversations' plans - would invite it to adopt somebody else's. `PLAN_EVICTED` never carries
  an `active_plans` list.
- **Ids are never reused** ([D29](09-defects-and-lessons.md#d29)). The new plan gets an id one
  past the highest ever issued that day (`last_plan_id` in the state file), not the evicted
  plan's.
- The new plan's own response says nothing about the eviction: another conversation's plan being
  closed is not its business.
Raising it removed the implicit bound a respawning model used to hit; the 1.16 circuit
breaker bounds that loop directly instead (a goal reworded and restarted 3 times halts -
judged from the shared state, so unrelated sessions never add up; see
[04](04-state-machine.md#loop-convergence-1160)).

**Watch-outs baked in as fixes:** after a blocking wait, re-read *your own* plan by id (not
"the active plan", which could be a sibling); the late-decision collector iterates *all* active
plans, not just one.

<a id="goal-drift"></a>
### Goal drift — the routing key can be forgotten (1.8.2)

Because routing is by goal string, the model *forgetting or rephrasing* the goal is a real
hazard. Before 1.8.2, a drifted goal on step 2 of drafting silently created a **second plan** —
the model thought it was continuing one plan while the server accumulated several, then hit
`PLAN_AMBIGUOUS` at approval time. Demonstrated with a fuzz script.

The fix distinguishes *starting* from *continuing* using `step_number`, which the model already
sends:

- **`step_number == 1`** → starting fresh → a new plan (this is how a new concurrent
  conversation legitimately begins, so it must stay).
- **`step_number > 1`, exact goal match** → continue that plan (normal).
- **`step_number > 1`, no match** → the model is continuing but the goal drifted. Do **not**
  fork. Return `GOAL_NOT_MATCHED` with the `active_plans` list (id + goal) and ask the model to
  repeat the exact goal, send the `plan_id`, or use `step_number=1` if it's genuinely new.
- **`step_number > 1`, no active plans** → recover by starting one (don't error out).

Goal matching normalizes trailing punctuation/whitespace (a period-only difference no longer
forks) but stays conservative on case and wording, so two genuinely different conversations are
never merged. `plan_id` is now also accepted by `plan_and_think` to continue explicitly.

**Drift is not the same as correction (1.12.0).** Everything above is about the *model* losing
the goal, and the answer is to refuse the fork. When the *user* changes the goal, refusing is
wrong: the model sends `revised_goal` and the plan's goal is updated in place (see
[03](03-tool-contract.md#1-plan_and_think--the-mandatory-entry-point)). Routing then accepts
either text — the current goal for as long as the plan lives, and each former goal for as long
as the plan stays active — so the correction never costs the model its plan. Current goals are
matched first, so a former goal can never steal a match from another conversation's live one.

**Safety net regardless:** `get_current_plan` always echoes the exact goal, and after
finalization the approval/execution tools route by `plan_id` or the-only-active-plan — so a
forgotten goal during *execution* of a single plan is harmless.

## Locking (`filelock.py` + `store.transaction()`)

Two layers, because `threading.Lock` only covers one process:

- **Thread layer:** `store.lock` (a `threading.Lock`), with per-thread nesting depth via
  `threading.local()`. (A shared depth counter was a real bug — see [09](09-defects-and-lessons.md#d5).)
- **Process layer:** an OS advisory lock on `state/.txnlock` (plan writes) and
  `state/.approvallock` (approval writes), via `msvcrt.locking` / `fcntl.flock`.

Critical detail: locking uses **non-blocking attempts + a 50 ms retry loop**, *not* a blocking
acquire. Windows `msvcrt.locking(LK_LOCK)` blocks internally for ~10 s before giving up, which
made the timeout argument meaningless and stalled contended callers for ten seconds
([09](09-defects-and-lessons.md#d8)). If the lock can't be taken within `timeout` (default
20 s) the call proceeds **unserialized but logs an error** — blocking the user is worse than a
rare race, but the race really can lose a plan, so it must be loud. A missing parent directory
fails immediately rather than burning the whole timeout.

Reads take **no lock**: every write is a temp file + atomic `os.replace`, so a reader sees the
whole old version or the whole new one, never a torn file. Only writers serialize.

## `store.paused()` — releasing the lock during a human wait

Holding the transaction across a blocking approval froze every other session (measured 52 s).
`store.paused()` releases the transaction for the duration of the wait and re-acquires it after.
The caller **must re-read state** afterwards — anything may have changed — and re-verify the
plan fingerprint before honouring a decision.

## Threaded stdio transport

The stdio read loop handles each request on its **own daemon thread**. Handling inline meant a
blocking approval stalled every later request behind it (again 52 s —
[09](09-defects-and-lessons.md#d6)). JSON-RPC matches responses by id, so out-of-order
completion is fine; writes are serialized through one output lock (`StdioNotifier`).

## Shared approval surface (1.7.0)

Approval state used to be per-process, so each ghost bound its own port and served its own
page; a request raised by a process the human wasn't watching timed out silently. Now:

- **State is shared** in `state/approval.json` (a queue, one entry per plan).
- **The page is a singleton elected by port binding.** Only the base port (8765) is bound. If
  it's taken, the instance probes `/api/health`; if the occupant is another planning-mcp on the
  *same* state directory, it publishes to the shared state that page already serves — no second
  page. The URL stays stable (a human keeps that tab open). A background thread keeps retrying
  the bind, so if the owner exits, another process takes the port over and the open tab keeps
  working. A foreign occupant is reported as an error, never adopted.

Because a decision can be recorded by a *different* process, the blocking waiter **polls** the
shared file every 200 ms rather than waiting on a `threading.Event`.

## Approval queue semantics

Two sessions waiting at once both appear on the page, each with its own buttons. Publishing for
a plan replaces that plan's earlier entry (revision), never another plan's. A plan's entry is
**withdrawn as soon as the plan settles** (approved/rejected/revised/completed) — a request
whose buttons would be ignored on the fingerprint check is worse than no request.

## Still true after all this

The **plan slot is multiplexed, but the state directory is shared.** For genuinely isolated
workspaces, run one server registration per workspace with distinct
`PLANNING_MCP_STATE_DIR` — separate state dirs are fully isolated. See
[10-deployment.md](10-deployment.md).
