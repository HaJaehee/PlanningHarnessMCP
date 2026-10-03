# 08 · Changelog

Version-by-version evolution with the *reason* for each. Machine-readable:
[data/versions.json](data/versions.json). Root-cause detail for the bug fixes:
[09-defects-and-lessons.md](09-defects-and-lessons.md).

The arc: v1.0–1.2 built the design; v1.3 added the physical pause; v1.4–1.5 hardened the
approval semantics; v1.6–1.8.1 made it correct under concurrency and hostile input. The later
versions are almost entirely *bug fixes found by writing tests for untested seams*.

---

### 1.0–1.2 — foundation
Four-tool MCP server, stdlib-only, atomic JSON persistence, audit log, leniency layer, the
single response builder, the enforced approval gate (`PLAN_NOT_APPROVED`). 1.2.0 added
`APPROVAL_NOT_REQUESTED` (approval binds to the shown version) and the air-gapped packaging
tooling (`make_package` / `setup_runtime` / `verify_install`, optional bundled Python).

### 1.3.0 — blocking approval (the physical pause)
`request_user_approval(ASK_USER)` holds the tool call open until a human decides on a localhost
page, so the agent loop cannot advance. Progress-heartbeat timeout arithmetic. This is the
project's central idea — see [06](06-human-in-the-loop.md).

### 1.4.0 — stale approval fix
A plan left `APPROVED` in `plan_state.json` from a previous session authorized execution hours
later in an unrelated conversation. Added approval **expiry** (TTL) and stopped a new goal from
inheriting an old approval. Defect [D1](09-defects-and-lessons.md#d1).

### 1.5.0 — approval window persists
The approval request was cleared when the tool call timed out (55 s), so buttons vanished before
the human answered. Now the request outlives the tool call and a late click is applied on the
next call. Defect [D2](09-defects-and-lessons.md#d2).

### 1.6.0 — concurrency, round 1
Two processes on one state dir lost a whole plan (shared `threading.Lock` is useless across
processes) → cross-process file lock. A blocking approval froze other sessions for 52 s → release
the lock during the wait (`store.paused()`) **and** thread the stdio read loop. Defects
[D3](09-defects-and-lessons.md#d3), [D6](09-defects-and-lessons.md#d6).

### 1.7.0 — one approval surface per state dir
Approval state was per-process, so ghost processes served their own unwatched pages. Moved it to
a shared `state/approval.json` with a singleton page elected by port binding + automatic
takeover. Defect [D4](09-defects-and-lessons.md#d4).

### 1.8.0 — multi-plan
Single `active_plan_id` meant concurrent sessions evicted each other. Plans now coexist, routed
by explicit id / by goal / the-only-active-one; approval became a queue. Defect
[D7](09-defects-and-lessons.md#d7).

### 1.8.1 — the edge-case hunt (three commits)
Writing tests for untested seams surfaced **8 more defects**:
- Thread-unsafe transaction depth counter (shared, not thread-local) → serialization silently
  off. [D5](09-defects-and-lessons.md#d5)
- File-lock timeout ignored (Windows `LK_LOCK` blocks 10 s internally). [D8](09-defects-and-lessons.md#d8)
- File lock burned full timeout on permanent failures (missing dir). [D9](09-defects-and-lessons.md#d9)
- SSE POST hung during blocking approval; SSE had no heartbeat notifier. [D10](09-defects-and-lessons.md#d10)
- `Store.save` reported lost writes as success. [D11a](09-defects-and-lessons.md#d11)
- Structurally-wrong (but valid-JSON) state file wedged every call on `INTERNAL_ERROR`. [D11b](09-defects-and-lessons.md#d11)
- Approval publish/decide reported unpersisted writes as success. [D11c](09-defects-and-lessons.md#d11)
- Settled plans left ghost approval requests on the page. [D11d](09-defects-and-lessons.md#d11)
- `normalize()` ran outside the exception guard; crashed on non-string keys. [D12](09-defects-and-lessons.md#d12)

Plus real-browser verification of the approval page and a fuzz suite for leniency.

### 1.10.0 — per-task plan review
`REVISE` used to mean one thing: throw the breakdown away and redraft it. A mid-sized model then
rewrites five tasks because the human objected to one, drifting on the four nobody questioned.

The approval page now renders a **PLAN** request as task rows, each with its own comment box, and
the REVISE button **states its consequence before it is clicked** (`수정 요청 · 3번만` vs
`수정 요청 · 계획 전체 재작성`, with a `☐ 계획 전체를 다시 세우기` override). The page sends that
`scope`; the server never infers it. `scope=TASKS` records `plan.pending_revision`, and
`plan_and_think` finalizes with the new `task_updates` parameter, rewriting **only** the flagged
tasks — ids, positions, and the `result_log` of untouched tasks all survive.

Deliberately out of scope, both for the same reason (each would answer a question the human did
not ask): the **completion phase** stays whole-plan — a completion report disputes whether work
happened, which rewriting a plan line cannot address — and **add/delete/reorder** stay whole-plan
because they renumber `task_id` and break the ordering invariants. A full `task_list` sent in
answer to a targeted request is accepted with a note and audited `targeted_revision_ignored`:
wasteful, not unsafe, and now measurable. See
[06](06-human-in-the-loop.md#per-task-review-19x).

> **Superseded in 1.13.0.** Excluding the completion phase was the right diagnosis and the
> wrong remedy: rewriting a plan line indeed does not answer a completion report, but *redoing
> the task* does, and that option did not exist. See [D17](09-defects-and-lessons.md#d17).


### 1.11.0 — fewer calls, same enforcement
An external review of the 1.10.0 tool surface raised two objections on behalf of small models:
14+ tool calls for a 5-task plan, and the `task_list` (strings) vs `task_updates` (objects) type
split. Its proposed fixes — a `batch_update` that marks several tasks `DONE` at once, and merging
the two parameters into one shape — were both rejected, and both for the same reason: the thing
being called overhead is the enforcement.

`DONE` in a batch is precisely the 1.9.0 field failure ([09](09-defects-and-lessons.md)), and
the two task parameters differ in *authority*, not format — `task_list` costs a full
re-approval, `task_updates` may only answer a request the human made. What did survive review:

- **Auto-advance** (`PLANNING_MCP_AUTO_ADVANCE`, default on). Accepting a `DONE` also puts the
  next task into `IN_PROGRESS`. The separate start call was a round trip, not a safeguard: the
  model still gets one "do the work" instruction per task and still cannot claim `DONE` without
  its own evidence, so 5 tasks cost 6 execution calls instead of 10 with nothing given up. A
  redundant `IN_PROGRESS` is accepted without resetting `started_at`, and
  `build_tool_definitions(auto_advance=...)` swaps the tool description so the advertised
  contract cannot contradict the running server.
- **Bare `task_updates` are read, not guessed.** A model asked to rewrite one task often sends
  just the new wording (`["the rewritten task"]`, or the bare sentence). Leniency now sets
  id-less titles aside instead of dropping them, and `handlers` pairs one with the single flagged
  task — unambiguous by construction. With several flagged tasks it stays a guess, so it is
  refused with a note naming the shape to send.

The review's own call count was also out of date: blocking approval has folded the second
`request_user_approval` into the first since 1.3.0. Real cost for 5 tasks: 14 → 10 calls.

Also in 1.11.0: the **per-task comment boxes are collapsed** behind an [의견] button. Rendered
open, a twelve-task plan was twelve textareas and the plan itself became unreadable — the page
exists to be read before it is answered. The boxes stay in the DOM, so a comment survives being
collapsed; the row keeps an amber marker so it cannot be submitted invisibly.

---

### 1.12.0 — the goal became mutable

Every other part of the plan could be corrected by the human; the one thing that could not was
the sentence saying what the plan was *for*. `goal` was frozen at creation and doubled as the
routing key, so a user saying "that is not what I meant — Q4, not Q3" left the server in the one
state nobody can act on: correct tasks under a goal the user had already disowned, shown back to
them on every response and on the approval page. Drift protection (1.8.2) had quietly hardened
into an inability to be corrected.

The fix separates the two things immutability was conflating. **Identity** stays fixed — the
plan keeps its `plan_id`, tasks, evidence and history. **Wording** follows the user: the new
optional `revised_goal` parameter updates `goal` in place, and the audit anchor moves into
`original_goal` + a `goal_history` of `{at, from, to, source}` hops with a `goal_revised` audit
event. Auditability was never a reason to freeze the field; it is a reason to record the change.

Routing absorbs the correction rather than punishing it: a plan answers to its previous goal
text for as long as it is active, so a model still echoing the old wording continues its plan
instead of being told it does not exist. A "revision" that only adds punctuation is not
recorded. And a goal revised on an already-approved plan is taken, but the response says
outright that the human approved the *previous* goal — the correction updates the metadata, it
does not extend the mandate.

---

### 1.12.1 — the last DONE stopped being silent ([D16](09-defects-and-lessons.md#d16))

Observed in use: the model completed every task, marked them all `DONE` with real evidence — and
then wrote a final answer to the user instead of requesting the completion report. The server
had moved the plan to `AWAITING_COMPLETION` and answered `next_action =
CALL_REQUEST_USER_APPROVAL`; the model went past it. Nothing was corrupted (the plan simply sits
in `AWAITING_COMPLETION` and the guard refuses further task writes), but the HITL gate that puts
a human in front of the evidence quietly never ran.

The cause was not that hints are weak. Auto-advance (1.11.0) made `message` the field that
carries the next order on every DONE — "task N has been started for you — do that work NOW" —
so over a plan the model learns that `message` is where its instruction lives, while
`next_action_hint` repeats similar wording every turn and becomes background. On the **last**
DONE there is nothing to advance into, so `message` was `None`: the one response in the whole
loop that said nothing, arriving at the one moment when declaring victory is the cheapest
continuation available. The model was not overriding an instruction — it was filling a silence.

`_finish_task` now always says what happens next. With every task DONE it asks for the
completion report explicitly — `request_user_approval` with `decision='ASK_USER'` and a per-task
`plan_summary` under `completion_approval`, or the final answer to the user when that gate is
off — and adds "do not declare success yourself".

The branch is on `advanced is None`, but that condition is not the same as "the plan is
finished": with `auto_advance` off, nothing is auto-started even when tasks remain. Collapsing
the two would have made a manual-mode server tell the model to report completion halfway through
the plan — the exact failure 1.9.0 was built to prevent. So the empty-`advanced` case splits
three ways: work left (name the next task), all done + `AWAITING_COMPLETION` (ask for the
report), all done + `COMPLETED` (write the final answer). `next_action` is untouched;
`resolve_next_action` remains its single producer.

The agent prompt was the other half of the gap: R4 and R7 named `next_action`,
`next_action_hint` and `display_to_user`, and never mentioned `message` — so the field the model
had actually been steering by was one no rule acknowledged. Both variants now name it, and
Phase 3b spells out that the last DONE is not an ending. This half only takes effect when the
prompt is repasted into AnythingLLM; the server-side message alone does not deliver it.

### 1.13.0 — rework: a completion report can be sent back task by task
The loop worked until the human pressed **수정 요청** on the *completion report*; then a small
model came apart. It was not the model. A completion revision was always a whole-plan redraft,
which sent the plan back to `DRAFTING` with every task still `DONE`, gave the generic "re-plan"
hint with the human's actual sentence in no instruction field at all, and then — because
finalizing replaces `plan.tasks` — **deleted every `result_log` the model had written**. The only
remaining move was to redo the entire plan under the ordering and evidence guards.

Now the completion page offers per-task comments, and `AWAITING_COMPLETION` + task comments means
**redo those tasks**, not rewrite their titles: only the named tasks reopen (keeping
`previous_result_log` so the redo is not blind), every other task keeps its `DONE` and its
evidence, and the plan returns to `IN_EXECUTION` **without re-approval** — the task list never
changed. The hint leads with the human's own words and repeats them wherever the task is handed
over. The whole-plan escape hatch stays for add/delete/reorder and now carries evidence across
the redraft, matched with `title_key` (the `goal_key` relaxation, applied to titles). Defect
[D17](09-defects-and-lessons.md#d17).

### 1.13.1 — evidence is never truncated
Found by driving the whole lifecycle through a real stdio MCP server with a browser on the
approval page. `Task.brief` cut `result_log` at 200 characters "to protect context" — but the
approval page is built from that same dict, so the completion gate was asking a human to certify
work whose evidence ended in `...` while the full text sat in `plan_state.json`. The cap is gone
from both surfaces. `previous_result_log` had the same split (text report only), so the page rows
now show the request, the old output and the new one together. Defect
[D18](09-defects-and-lessons.md#d18).

### 1.13.2 — the rework state, probed adversarially
"Will a small model really only touch the task that was sent back?" is not answered by a scripted
run where the agent behaves. So the rework state was driven by a model that ignores every hint.
Most routes were already closed: an accepted `DONE` task cannot be restarted or overwritten
(idempotent, redirects to the reopened one), and `plan_and_think` short-circuits on an
approved/running plan — *"already approved and running"* — so neither `task_list` nor
`task_updates` can reach the task list or its evidence. One route was open: reporting the
reopened task `DONE` with the very `result_log` the user had just rejected, which is the cheapest
continuation available and now returns `REWORK_NOT_DONE`. The enforcement/instruction split is
written down in [06](06-human-in-the-loop.md#rework-1130), including the row that says a model
which does no work but writes a plausible new outcome is **not** detectable — that is what the
completion report is for. Defect [D19](09-defects-and-lessons.md#d19).

### 1.14.0 — the wait stops betting on the client
The blocking wait extended past the client's 60 s request timeout by sending progress
heartbeats, on the strength of a code comment saying `resetTimeoutOnProgress` defaults to true.
It defaulted to **false** and was flipped later, and it is the *client's* option to pass — so a
server can neither set it nor read it back. Where it is off, the call is killed at 60 s, the
client throws the result away, and the conversation breaks mid-approval.

The wait is now **chunked**: one tool call lasts at most `call_budget` (45 s) whatever the
client does, ends in an `APPROVAL_PENDING` response telling the model to call straight back, and
the human's 900 s budget accumulates across slices from the request's `created_at`. Same shape
the protocol is converging on ([SEP-1391], [SEP-1539]). `notifications/cancelled` is no longer
swallowed: it unblocks the waiting call and records what the client actually allowed, so later
slices shrink under it. Two guards that had been asserting invariants they never checked were
closed at the same time — the model could approve its own plan, and `_open_browser_once` opened
a window every time. Drafts moved out of the DOM into `localStorage` so the page's full rebuild
(now far more frequent) cannot eat a half-written comment, and mirror across tabs. No countdown
was added: it would measure the call, not the request. Defects
[D20](09-defects-and-lessons.md#d20-—-the-model-could-approve-its-own-plan-1140),
[D21](09-defects-and-lessons.md#d21-—-the-heartbeat-bet-and-the-swallowed-cancellation-1140),
[D22](09-defects-and-lessons.md#d22-—-a-new-browser-tab-per-approval-and-a-rebuild-that-ate-what-you-typed-1140).

### 1.14.1 — a closed approval window comes back
Found in live testing of 1.14.0, not by the suite. The tab-spam guard added in 1.14.0 was a
permanent latch, so closing the approval window meant no request could ever open one again: the
agent went on slicing its 45 s waits against a page that was not on screen, and the human waited
for a window that was never coming. Suppression is now time-based (`OPEN_GRACE_SEC`, 10 s) on
top of the liveness check that was already correct — both expire, so the state is always
recoverable. Defect [D23](09-defects-and-lessons.md#d23-—-close-the-approval-window-and-it-never-comes-back-1141).

### 1.14.2 — page liveness is shared, like everything else
The 1.14.1 fix leaned entirely on "is a tab watching?", and that was answered from a
process-local counter only the page's *owner* ever updates. The instance that opens browsers is
whichever one holds the request — usually a peer, which therefore always concluded nobody was
watching. Result: a new window every 45 s, one per slice, at a human already looking at the
page. Liveness now goes through `page_seen` in the state directory. The launch grace also backs
off when a launch never produces a poll. Defect
[D24](09-defects-and-lessons.md#d24-—-a-new-window-every-45-seconds-at-a-human-already-looking-at-the-page-1142).

[SEP-1391]: https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1391
[SEP-1539]: https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1539

### 1.15.0 — one task id, in one field
`update_task_progress` stopped re-sending the whole task list on every response, and every
literal `task_id=N` left the hints: the only place an id is published is `next_task`. Five tasks
used to leave six copies of the list in the conversation, five of them wrong - and each carried
an instruction ("call update_task_progress with task_id=3"), not just data.

### 1.15.1 — get_current_plan returns your own plan
The tool description told the model to send `"current"`, which only resolves while exactly one
plan is live; with two, the answer fell through to "there is no active plan, start one" and the
model forked a duplicate. The description now asks for the session's own `plan_id`, and the
not-my-plan answers carry `PLAN_AMBIGUOUS` so `next_action` says `CALL_GET_CURRENT_PLAN`.

### 1.16.0 — planning loops converge ([D25](09-defects-and-lessons.md#d25), [D26](09-defects-and-lessons.md#d26))
Field report: Zed / Goose with a mid-sized thinking model looped in its own self-verification
("wait, let me reconsider"), both inside one thinking block and across `plan_and_think` calls.
Not reproducible here, so the server side was located by scripting the calls a looping model
makes - four were simply accepted - and closed with enforcement rather than wording:

- **Thinking budget + draft.** A drafting round has `PLANNING_MCP_MAX_THINKING_STEPS` steps
  (standard 8, reasoning 2). Every hint names the exit first ("it does not need to be perfect -
  the user reviews it") and counts down. The latest `task_list` sent while thinking is kept as
  `draft_tasks`; when the budget runs out the server **submits the draft** (straight to the
  approval page when there is one) instead of refusing. A final call with no list uses the draft.
- **A finalized plan is not reopened by the model.** `plan_and_think` on `AWAITING_APPROVAL` or
  `AWAITING_COMPLETION` is redirected (D26 closed: re-thinking after the last task used to delete
  every `result_log`). Only the human's 수정 요청 reopens a plan; `revised_goal` stays the one
  exception before approval.
- **Calls wait on an open request.** While a human has a request open for a plan, `plan_and_think`
  and `update_task_progress` on it wait on the human exactly like the approval call - paced at one
  call per slice instead of spinning - and the model's thought is shown on the card as
  **에이전트 추가 의견**. Undecided, they return a refusal, never `ok:true`.
- **No second lap after completion.** The same goal, completed under `PLANNING_MCP_REPLAN_COOLDOWN`
  (600 s) ago, is answered from its results (`ANSWER_USER`) instead of re-planned.
- **Circuit breaker** (`planning/loopguard.py`). Per plan, in process memory: no progress for 12
  calls, the same call 3 times, the same error - or a turned-away re-plan - 4 times; from shared
  state: a goal reworded and restarted 3 times; plus a budget spent with no draft. The plan gets a
  `halt` overlay, every tool refuses it (`LOOP_HALTED`), and a **halt card** goes on the approval
  page: 이 초안으로 승인 / 계속 진행 (with an optional direction, which then leads every hint) /
  취소. The tripping call waits on it. Waited-out slices and calls made while a human is looking
  never count. Without a page, the human answers in chat.
- **`reasoning` profile** (`PLANNING_MCP_MODEL_PROFILE`): `plan_and_think` records the plan in one
  call; `step_number` / `total_steps` / `revises_step` are not advertised (still accepted).
- **Prompt hygiene.** `request_user_approval`'s description is generated per approval mode;
  "before answering ANY request" became "for each NEW request; ANSWER_USER means answer"
  everywhere (descriptions, MCP `instructions`, prompt). `agents.md` was rewritten from ~110 dense
  lines to ~45 without a single contradiction, the old Variant A (~270 lines) replaced, and the
  three copies pinned equal by `TestPromptHygiene`. The README's "temperature ≤ 0.3" now excludes
  thinking models, whose model cards warn that greedy decoding causes endless repetition.
- **Field telemetry.** `client_connected` (from `clientInfo`), `gap_sec`, `reconsider` and
  `thought_chars` on every thinking step, `loop_halted` / `halt_resolved` / `auto_finalized` /
  `replan_redirected` / `call_held_for_human`; `tools/loop_report.py` summarizes them offline.
- `PLANNING_MCP_MAX_ACTIVE_PLANS` default 5 → **20** (requested).

Seven existing tests changed expectation; each kept its safety assertion and changed only the
route (e.g. a re-plan after finalize is now redirected, so the list it would have replaced
survives). 78 new tests in `tests/test_loop_convergence.py`, including scripted "thinking models"
that must converge in a bounded number of calls.

### 2.0.0 — the model proposes, the human picks
The approval page used to offer approve / revise / reject for the plan as written. Now a task
whose right way depends on the user's preference can offer a choice: the model's `task_list` is
its recommendation, `alternatives` the other ways, `recommended_reasons` why it prefers its
own; the page shows them as radios with the recommendation pre-selected and marked 권장, and
whatever the human picks becomes the task. Plan and decisions:
`docs/plan-2.0-task-alternatives.md`.

- **Also a 1.16 follow-up.** The self-verification loop of D25 is mostly a model oscillating
  between two ways of doing one task. The reasoning profile is now told: if torn, do not
  reconsider - put the other way in `alternatives`, the user picks.
- **Only the human picks while a page is open.** `choices` is advertised only in chat mode;
  the store refuses an index the request did not show; the model's relay is honoured only with
  no page.
- **The unchosen never reach the model again** (D19 before the fact): `Task.brief()` carries the
  pick (`chosen_by_user`, `choice_reason`) but never the options; only `page_brief()` does.
- **Nothing changes for a plan without choices** - not its responses, not its fingerprint (so
  requests on the page survive a rolling upgrade).
- Kept with the draft and submitted with it (decision §8-3); the halt card offers the choice
  too, without 기타 (§8-2); a reason for the recommendation (§8-4); both profiles, with
  `PLANNING_MCP_ALTERNATIVES=off` (§8-1); limits `PLANNING_MCP_MAX_ALTERNATIVES` /
  `PLANNING_MCP_MAX_CHOICE_POINTS` (3 / 3).
- Each choice is headed by what is being chosen: the model's optional `topic` ("집계 방식 · 3가지
  중 선택"), or "진행 방법 · N가지 중 선택" without one - not "choose one of the following".
- The page keeps its drafts and says so when a decision is not recorded (it used to clear
  them after any POST).
- Major version: the plan model (`options`, `chosen`, `draft_alternatives`) and the approval
  protocol (`choices`) changed. State files from 1.16 load unchanged.

85 new tests in `tests/test_alternatives.py`; no existing test changed expectation.

### 3.0.0 — what "done" means, and what the server can check
Through 2.0 the harness governed *when* the model may act. The remaining hole was the one this
wiki called its honest boundary: `DONE` is a claim, checked only for its shape, and judged at the
very end by a human with nothing to read it against but the task title. 2026's harness work
converges on the same answer - agree what "done" is before the work, verify what can be observed,
keep the judge separate from the worker - and 3.0 builds the part of it that fits a stdlib-only
server with no model of its own. Plan, research and decisions:
`docs/plan-3.0-verification-contract.md`.

**The verification contract** ([04](04-state-machine.md#the-verification-contract-300)).
- `plan_and_think.done_when`: per task, what exists or is true when it is finished. The human
  approves it with the plan - and may **write or rewrite it on the approval page and approve in
  the same click**, with no revision round trip, because the task itself did not change.
- The criterion is handed over with the task (`next_task.done_when`, the hint), not left five
  turns back. A `result_log` that only says it back is refused.
- `update_task_progress.files`: the files a task created or changed. Inside the folders named in
  `PLANNING_MCP_ARTIFACT_ROOTS` the server looks (`os.stat`, nothing else, nothing outside), and
  a file that is not there refuses the `DONE` (`FILE_NOT_FOUND`). Off by default: no folders,
  no checks, and the field is not advertised.
- The completion page shows criterion, evidence and what the server found side by side, says how
  many tasks rest on the agent's report alone, and marks a dropped claim, a file gone since, and
  evidence that nearly repeats the criterion.
- No LLM judge. Reported false-success detectors built on a second model do little better than
  chance; the checks here are deterministic and the judge is the human.

**Local repair** ([04](04-state-machine.md#local-repair-300),
[D28](09-defects-and-lessons.md#d28)). A `FAILED` task used to force a re-plan of the whole
list, which dropped the evidence of every finished task. Now the failure flags that one task -
the 1.10 per-task machinery, with the failure as the comment - and the model rewrites it (and,
if it must, the unfinished tasks after it) with `task_updates`. Finished tasks cannot be touched;
the failed one must be; the human re-approves the change. A whole `task_list` still works, at
its old cost, audited.

**Found on the way** ([D27](09-defects-and-lessons.md#d27)). Approving a plan a second time put
the recommendation back over the human's pick - a 2.0 bug on the expired-approval path, exposed
when a repair made second approvals routine. Found by the browser check, not by the suite.

- **Nothing changes for a plan under no contract**: responses, chat texts and fingerprint are
  the 2.0 ones, and with `PLANNING_MCP_DONE_WHEN=off` + `PLANNING_MCP_LOCAL_REPAIR=false` none
  of the 3.0 text is advertised. State files from 2.0 load unchanged.
- **Cost, and the trim that paid for it**: the new fields took the tool definitions from
  3,903 to 4,239 estimated tokens (the `bytes // 3` method of
  `docs/context-budget-analysis.md`) - more than the +250-300 first estimated. So the whole
  tool text was trimmed in the same release: each rule said once, in the tool description
  (the part every client relays); one example per parameter; no second example on nested
  fields. Result: **3,486** (3,599 with file checks) - 3.0 with everything on now costs less
  than 2.0 did. No rule, example or enum value was removed (`TestToolTextIsLean`), but the
  trim is unmeasured on the corporate model. Table in
  [03](03-tool-contract.md).
- **The repeat threshold is 0.3, not the 0.5 first proposed**: measured on more pairs, 0.5 would
  have refused "Saved the summary to out/summary.md (5 lines, 412 bytes)". Evidence under 0.5 is
  marked on the page instead. Every score is audited (`task_done.novelty`) for field tuning.
- **The agent prompt was trimmed too**, at the user's direction: the target is a mid-sized
  model, and a long prompt lowers how much of it such a model follows. `agents.md` went from
  46 lines / 3,862 characters to 31 / 2,275. A rule the tool descriptions state, or that the
  hint gives at the moment it applies, is no longer repeated in the prompt (`LOOP_HALTED`,
  the rework rule, `task_updates`, `task_id` from `next_task`, the refused phrases); the
  table of what moved where is in `docs/phase3-anythingllm-agent-prompt.md`. The Korean
  Variant B, which had been left at its 2.0 wording, was rewritten to match. The README no
  longer embeds the prompt: it names `agents.md` as the file to paste.
- **The approval page shows its version.** A small `planning-mcp 3.0.0` above the card, the
  same element on the idle screen, a plan request, a completion report and a halt card - so
  "is the new version actually running?" has an answer on screen (a process keeps the code
  it imported until restarted). Each request also records which version asked, and the card
  says so when that differs from the page's, or when the asking process is too old to say.
  A small `i` beside it opens an information dialog (author, email, version).
- **At the plan limit, the least recently used unfinished plan is evicted** instead of the new
  one being refused (`PLANNING_MCP_EVICT_LRU`, default on). With 20 slots the table fills with
  plans abandoned conversations left behind, and only a human rejecting them on the page ever
  removed one. A plan touched within `PLANNING_MCP_EVICT_MIN_IDLE` (300 s) is in use and is
  never evicted - if all are, the new plan is refused as before, which is also what stops a
  model that keeps opening plans from emptying the table. The evicted plan's evidence goes to
  the audit log, its request leaves the approval page, and a conversation that returns to it
  gets `PLAN_EVICTED` → `ANSWER_USER`, never another conversation's plans. See
  [05](05-concurrency-and-sessions.md).
- **Plan ids are never reused** ([D29](09-defects-and-lessons.md#d29)): found by the eviction
  tests, where the plan that displaced an evicted one was handed its id.
- **Packaging**: `agents.md` is shipped (the README names it as the file to paste, and the
  package did not contain it); the repository's `MANIFEST.txt` no longer names the bundled
  runtime, so `--with-python` can be built in any order (the open item of
  [11](11-status-and-next-steps.md)). `package_source.ps1` runs it on Windows.
- New module `planning/evidence.py` (pure). One new error code (`FILE_NOT_FOUND`); no new plan
  status, task status or `next_action`.
- Major version: the plan model (`done_when`, `files`, `checks`, `failure_note`,
  `pending_revision.origin`) and the approval protocol (`criteria`) changed.

212 new tests (`tests/test_verification.py` 135, `tests/test_local_repair.py` 34,
`tests/test_plan_eviction.py` 32, `tests/test_packaging.py` 8, three in `TestPromptHygiene`). No existing test changed expectation - including the ones that pin
tool-description wording - except two that follow a decision: `agents.md` must now stay
under 2,600 characters (was 4,200), and the README is required to point at `agents.md`
rather than to contain it.

### 3.1.0 — the server asks, and the human can step in while it runs

The target is a mid-sized model that already thinks (CoT) and remembers on its own, on a
host with no approval step of its own. What such a model lacks is the gate - so the gate is
where 3.1 spends its effort, in two directions: ask less of the model, give the human more.

- **The gate is on the transition, not on a call.** The final `plan_and_think` call and the
  last `DONE` *are* the approval requests: the server opens the request in the call that
  recorded the plan (or finished the work), and that call waits on the human exactly as
  `request_user_approval` did. A decision made during the wait is the answer to that call. A
  targeted revision, a repair and a note from the run card go back to the human the same
  way, in the call that rewrote the tasks. `request_user_approval` is left with one job: keep
  waiting when a slice ended undecided (`APPROVAL_PENDING`) - or, with no page, report the
  user's reply. `plan_summary` is gone from that flow; the model's own sentence (`thought`)
  is the overview on the page. `PLANNING_MCP_AUTO_ASK=false` restores the flow of 3.0 with
  its texts; nothing is asked under `PLANNING_MCP_AUTOAPPROVE`.
- **Less text, not more.** Two rules left the prompt (8 → 6; 2,275 → 2,126 characters), and
  the tool text shrank: 3,486 → 3,406 estimated tokens (standard), 3,325 → 3,237
  (reasoning). The approval tool is advertised as *WAITING FOR THE USER*, execution became
  STEP 2, hints no longer ask for a `plan_summary`, and the server instructions no longer
  name `request_user_approval`. Not measured on the corporate model.
- **One call waits one slice.** A call that has already waited gets only what is left of the
  client's budget (`_CallCtx.wait_spent`), and a loop trip decided earlier in the same call
  suppresses the plan request - the halt card asks instead.
- **The run card.** Between the two gates the page said "no pending requests". It now shows
  each executing plan - tasks, statuses, the evidence of the finished ones, when the agent
  last reported - from a new shared file, `state/runs.json` (`RunBoard`), rebuilt by one
  `_sync_runs` at the end of every call and before every wait. A run card raises no alarm.
- **Stop (멈춤).** Asked for on the card, applied when the agent next reports a task: the
  `DONE` is recorded, the next task does not start, and the plan is held by the breaker's
  halt machinery under another reason (`halt.reason = "user_pause"`, error code
  `PLAN_PAUSED`, a card titled 실행 멈춤). The human continues - with or without a direction,
  which leads the next hint - or cancels. Continuing starts the task the stop held back.
- **Change what is left (의견 전달).** The note opens the unfinished tasks through the
  local-repair machinery with a third origin (`pending_revision.origin = "run"`): the model
  rewrites the tasks the note affects with `task_updates`, each carries the note
  (`revision_note`), finished tasks are out of reach, and the human approves the change
  before anything continues. A whole `task_list` is accepted too and keeps the finished work
  of tasks whose wording survived.
- **One card at a time.** The page redraws only the card whose content changed. A task
  reported by one plan no longer rebuilds another plan's approval request - an opened comment
  box stays open, a selection and the caret stay where they were. A decision switches off
  and redraws its own card only
  ([06](06-human-in-the-loop.md#one-card-at-a-time-310)).
- **The criterion has one name.** It is 태스크 완료 기준 (was 완료 기준) wherever the user
  reads it: the row label on the page, the approve button (`승인 · 태스크 완료 기준 1건
  반영`), the warning about evidence that repeats it, and the plan and completion text
  shown in chat. The button that adds one is 완료 기준 추가 (was 기준 추가), and the field
  no longer shows a sample sentence as its placeholder.
- **Nothing typed is dropped.** A request not yet applied can be taken back and its words
  return to the box; a stop or a note that arrives after the last task is shown on the
  completion report; one that meets a failure goes to the model with the failure; a memo
  typed beside 멈춤 becomes the direction if the human resumes without typing another.
- **[D30](09-defects-and-lessons.md#d30)** fixed: a decision made between two calls was
  undone by the `request_user_approval(ASK_USER)` that collected it - a confirmed plan went
  back to `AWAITING_APPROVAL`, and a plan sent back for changes was shown again unchanged
  while the model never received the comment. The ordinary path in `return` mode since 1.14.
- New: settings `PLANNING_MCP_AUTO_ASK` and `PLANNING_MCP_RUN_CONTROL` (both on); error code
  `PLAN_PAUSED`; audit events `run_paused`, `run_note_applied`, `tasks_steered`,
  `steer_replanned`, `run_control_moot`, and `by_server` on `approval_requested` /
  `completion_verification_requested`; `POST /api/control`, and `runs` in
  `GET /api/pending`; `Plan.run_note`.
- Minor version: the default flow changed, but every 3.0 call sequence is still accepted - an
  agent on the 3.0 prompt calls `request_user_approval(ASK_USER)` and joins the request the
  server already opened. The state file gained one optional field.

119 new tests (`tests/test_gate_and_run.py`) and a seventh smoke test
(`tests/smoke_gate_and_run.py`, added to `verify_install.py`). The older suites run on the
new default except where they assert the model-asks call sequence step by step; those
classes and tests are pinned to it, so the 3.0 flow keeps its coverage
([07](07-testing.md#gate-and-run-suite-teststest_gate_and_runpy-310)). Two tests changed
expectation: the version, and the page-template string of the halt card's task row; two
more follow the page drawing one card at a time (which buttons a decision switches off,
and where the agent note enters a card's signature).
Verified in a real browser - see [06](06-human-in-the-loop.md#the-run-card-310).

---

## Git commit ↔ version map

| Commit | Version / theme |
|---|---|
| `4ac3562` | v1.2.0 foundation + packaging |
| `8e277da` | v1.3.0 blocking approval |
| `98abb86` | approval UI surfacing hardening |
| `920dd7d` | v1.4.0 stale approval |
| `225c0fb` | v1.5.0 persistent approval window |
| `dab9c97` | v1.6.0 concurrency round 1 |
| `a3456bd` | v1.7.0 shared approval surface |
| `4980396` | v1.8.0 multi-plan |
| `046a75e` | v1.8.1 thread-safe txn + 27 edge tests |
| `49129d3` | protocol/transport/filelock fixes |
| `7ded398` | store/approval failure paths |
| `6d3748b` | approval page browser check + leniency safety |

`main` branch, remote `github.com/HaJaehee/PlanningHarnessMCP`. **Not pushed** as of
this writing — see [11](11-status-and-next-steps.md).
