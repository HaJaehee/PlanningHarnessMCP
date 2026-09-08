# 09 · Defects and Lessons

The 24 real defects found while hardening the server, each with root cause, symptom, and fix.
**This is the highest-value page for predicting where the next bug is.** Every one lived in a
place that had no test, and several were *silent* — the server reported success while losing
data or disarming the safety gate. D16 and D17 were found in *use* rather than by testing, and
both were the same shape: a state the protocol could reach but had no instruction for. D18 and
D19 came from *looking at the running system* — one by reading the page a human decides on,
one by scripting a model that ignores every hint. D20–D22 came from *reading the other side's
source and issue tracker*: an SDK default this code had documented backwards, and two guards
whose names asserted an invariant the code never checked.

The meta-lesson, stated once: **a green test suite over untested seams means nothing.** The way
these were found was to write a test asserting a *guarantee* (not the current output) for each
untested seam — threading, transports, locking, failure paths, hostile input — and watch it
fail.

---

<a id="d1"></a>
## D1 — Stale approval authorized later execution (1.4.0)
**Symptom:** approval popup never appeared, yet the agent executed. A `plan_state.json` from the
morning held a plan still marked `APPROVED`.
**Root cause:** `plan_and_think` redirected a *new, different* goal onto the old approved plan;
`request_user_approval(ASK_USER)` short-circuited with "already approved" so nothing blocked;
`update_task_progress` sailed through. A 5-hour-old approval licensed today's execution.
**Fix:** approval **expiry** (TTL, default 1800 s, unreadable timestamp = expired) checked at all
three entry points; a different goal no longer inherits an approval.

<a id="d2"></a>
## D2 — Approval window closed too early (1.5.0)
**Symptom:** approve/reject buttons vanished ~55 s after appearing, before the human answered.
**Root cause:** on tool-call timeout the server *cleared* the approval request.
**Fix:** leave the request published on timeout; collect a late click on the next tool call and
apply it (fingerprint-checked). The wait has a ceiling; the human does not.

<a id="d3"></a>
## D3 — Concurrent plan loss across processes (1.6.0)
**Symptom:** two near-simultaneous `plan_and_think` calls from different processes both got
`plan_20260724_0001`; one plan vanished from disk entirely (atomic write, so no corruption —
just a silent lost update).
**Root cause:** `Store.lock` is a `threading.Lock` — no effect across processes, and ghost
processes on one state dir are normal.
**Fix:** OS-level file lock (`state/.txnlock`) around the load-mutate-save cycle.

<a id="d4"></a>
## D4 — Per-process approval pages (1.7.0)
**Symptom:** approval requests raised by one process appeared on a port the human wasn't
watching, then timed out — the gate silently degraded to "asked, nothing stopped it".
**Root cause:** approval state lived in process memory; each process bound its own port.
**Fix:** shared `state/approval.json`; a singleton page elected by port binding with `/api/health`
peer detection and automatic takeover.

<a id="d5"></a>
## D5 — Transaction not mutually exclusive across threads (1.8.1)
**Symptom:** a test asserting mutual exclusion caught two threads inside the critical section at
once. Combined with D6's threaded transport, the D3 fix was *silently off* for same-process
concurrency.
**Root cause:** the nesting-depth counter was a shared instance attribute; a second thread saw
`depth != 0`, skipped the lock, and walked in.
**Fix:** per-thread depth and lock handle via `threading.local()`.
**Lesson:** re-entrant lock state must be thread-local, always.

<a id="d6"></a>
## D6 — Blocking approval stalled other sessions 52 s (1.6.0)
**Symptom:** an unrelated `get_current_plan` waited 52 s while another session's approval was
pending.
**Root cause (two):** (a) the store lock was held across the human wait; (b) the stdio read loop
handled requests inline, so nothing else was even *read*.
**Fix:** `store.paused()` releases the transaction during the wait (caller re-reads + re-verifies
after); the stdio loop handles each message on its own daemon thread (responses matched by id).

<a id="d7"></a>
## D7 — Single plan slot evicted concurrent sessions (1.8.0)
**Symptom:** two conversations working at once; the second's plan replaced the first's.
**Root cause:** a single `active_plan_id`.
**Fix:** plans coexist; route by explicit id / goal / only-active; `PLAN_AMBIGUOUS` + directory
when unclear; hints qualified with the plan_id. **Follow-on bug caught by the smoke test:** after
a blocking wait the handler resolved "the active plan" and picked up a *sibling*; fixed to re-read
by its own id.

<a id="d8"></a>
## D8 — File-lock timeout did nothing (1.8.1)
**Symptom:** a 0.3 s lock holder stalled the next caller ~10 s; the `timeout` argument was inert.
**Root cause:** Windows `msvcrt.locking(LK_LOCK)` blocks internally ~10 s before failing.
**Fix:** non-blocking attempts (`LK_NBLCK` / `LOCK_NB`) in a 50 ms retry loop honouring our own
timeout.

<a id="d9"></a>
## D9 — Permanent lock failure burned the whole timeout (1.8.1)
**Symptom:** the test suite took 33 s; a lock on a missing directory retried for 20 s.
**Root cause:** transient failure (peer holds file) and permanent failure (no such dir) were
treated the same.
**Fix:** a missing parent directory fails immediately; only genuine contention retries. Suite
33 s → 13 s.

<a id="d10"></a>
## D10 — SSE POST hung; no SSE heartbeat (1.8.1)
**Symptom:** under SSE, a blocking approval left the POST hanging until the client gave up;
approvals silently fell back to the 55 s no-token ceiling.
**Root cause:** `do_POST` ran `handle_message` *before* sending 202; and no notifier was wired
for SSE, so progress heartbeats were impossible.
**Fix:** send 202 first, run the work on a thread, deliver the result over the SSE stream; add
`_SseNotifier` that fans heartbeats to open streams.
**Lesson:** stdio was tested; SSE was not — so SSE was where the bug was.

<a id="d11"></a>
## D11 — Four "reported success on a failed write" defects (1.8.1)
Writing failure-path tests for `store`/`approval` failed **7 times immediately**; the common
shape was *"failed but answered success"*:
- **D11a** `Store.save` logged an `OSError` and returned → `ok:true` for a mutation never
  written; the model executes a plan the server has no record of. **Fix:** raise
  `StoreWriteError` → `INTERNAL_ERROR` + resync hint.
- **D11b** valid JSON of the wrong shape (e.g. `plans` a list) raised in `State.from_dict` on
  *every* call, wedging the server permanently on `INTERNAL_ERROR`. **Fix:** quarantine like any
  corrupt file, start empty.
- **D11c** `ApprovalStore.publish` / `record_decision` returned success when the write failed →
  a request nobody can answer; the gate silently disarmed. **Fix:** `_write` returns bool;
  `publish` returns `None` on failure → handler emits the loud `NOT hard-paused` warning.
- **D11d** a cancelled/completed/revised plan left a live-looking approval request on the page
  whose buttons did nothing (discarded on fingerprint). **Fix:** withdraw the plan's entry on
  every settling transition.
**Test lesson:** patch the *specific* store's `_write`, never `os.replace` globally — `os` is
shared, so a global patch breaks the other store and tests the wrong thing.

<a id="d12"></a>
## D12 — `normalize()` outside the guard; crashed on non-string keys (1.8.1)
**Symptom:** a crafted non-string key made `normalize` call `.lower()` on a non-str and raise —
and because `normalize` ran *before* dispatch's try block, it escaped as a raw JSON-RPC error
instead of the graceful `ok:false` a weak model needs.
**Fix:** move `normalize()` inside the exception guard (any future leniency bug now degrades
gracefully); drop non-string keys; reject NaN/inf for integer fields. Fuzzed against nested
objects, mixed types, nulls, 100k-char strings across all four tools — it must never raise.

---

<a id="d13"></a>
## D13 — Goal drift forked plans silently (1.8.2)
**Symptom:** during multi-step drafting, the model rephrasing its `goal` between steps created a
second (third, …) plan; at approval time the model hit `PLAN_AMBIGUOUS` with plans it didn't
know it had. A period-only difference ("find file" vs "find file.") also forked.
**Root cause:** routing was by exact goal string, and the model repeating the goal verbatim
every step is exactly the kind of discipline a weak model lacks.
**Fix:** use `step_number` as the start-vs-continue signal. `step==1` starts a new plan;
`step>1` with no goal match returns `GOAL_NOT_MATCHED` + the active-goal list so the model picks
the exact one (or sends `plan_id`), never forking. Goal matching ignores trailing punctuation.
**Lesson:** any routing key the *model* must reproduce is a key the model can forget — give it a
way to recover (a list to pick from) instead of guessing on its behalf.

<a id="d14"></a>
## D14 — Tasks planned but never executed; plan reported finished (1.9.0)
**Symptom (field report):** the small model drove one full plan-approve-update-recover cycle
correctly, then stopped doing the actual work: it marked tasks `DONE` in a row without
performing them and reported the plan complete.
**Root cause:** `DONE` was accepted unconditionally. Missing `IN_PROGRESS`, missing
`result_log`, and out-of-order completion were all *notes*, not refusals — and `all_done()`
awarded `COMPLETED` on the model's word alone. The server treated a **claim** as a **fact**.
**Fix, three layers:**
1. `DONE` is refused unless the task was started, all earlier tasks are finished, and
   `result_log` is real evidence (`TASK_NOT_STARTED` / `TASK_OUT_OF_ORDER` /
   `MISSING_RESULT_LOG`). Since `IN_PROGRESS` already enforces order, batch-marking becomes
   structurally impossible.
2. Evidence is judged by **content, not length** — an exact-match filter for bare success
   phrases ("완료", "done", "ok", repeating the task title) plus a low floor of 8 normalized
   characters. A first attempt used a 15-character minimum and wrongly rejected
   `"매출 표 12행을 추출함"`, a perfectly concrete Korean outcome; length is a bad proxy in a
   dense script.
3. `COMPLETED` is no longer reachable by the model: the last `DONE` moves the plan to
   `AWAITING_COMPLETION`, and a human must approve a per-task **completion report**. The
   fingerprint covers each task's status and `result_log`, so the evidence cannot be edited
   after the human saw it.
**Lesson:** the server cannot observe work, so anything the model *asserts* about work must
either carry evidence or be verified by a human. Accepting an unverifiable claim with a
warning note is the same as accepting it.

<a id="d15"></a>
## D15 — The goal could not be corrected (1.12.0)
**Symptom:** the user says "that is not what I meant — Q4, not Q3". The model re-plans and the
tasks change, but the plan's `goal` cannot: it is fixed at creation. The server then shows the
disowned goal on every response, in `get_current_plan`, and at the top of the approval page,
while executing tasks written for the corrected one.
**Root cause:** `goal` was doing two jobs — the routing key (D13) and the human-facing statement
of intent. Freezing it protected the first job and broke the second. The stated justification,
auditability, does not actually require immutability; it requires a *record*.
**Fix:** `revised_goal` on `plan_and_think` updates `goal` in place while `original_goal` and
`goal_history` keep the anchor and every hop (`goal_revised` audit event). Identity (`plan_id`,
tasks, evidence) is what stays fixed. Former goal texts keep routing to the plan so a model one
turn behind the correction does not lose it; punctuation-only changes are not recorded; and a
revision on an `APPROVED` plan is recorded with an explicit note that the human approved the
*previous* goal.
**Lesson:** "immutable for audit" is usually a record-keeping requirement wearing a constraint's
clothes. In a conversational system the user's intent is discovered, not declared — any field
holding that intent must be able to follow a correction, or the metadata starts describing a
plan that no longer exists.

<a id="d16"></a>
## D16 — The model stopped without reporting completion (1.12.1)
**Symptom:** observed in use. Every task gets done and marked `DONE` with real evidence, the
server moves the plan to `AWAITING_COMPLETION` and answers
`next_action = CALL_REQUEST_USER_APPROVAL` — and the model ignores it, writing a final answer to
the user instead. The completion report, the one gate that puts a human in front of the
evidence, is never requested. The plan stays `AWAITING_COMPLETION` forever; nothing is corrupted,
but the HITL check silently does not happen.
**Root cause:** not "weak models ignore hints" — the channel went silent at exactly the wrong
moment. Since auto-advance (1.11.0), every mid-loop `DONE` response carried its order in
`message` ("task N has been started for you — do that work NOW"). Across a plan the model learns
that `message` is where its next instruction lives, while `next_action_hint` repeats similar
wording every turn and fades into background. On the **last** `DONE` there is nothing to
advance into, so `message` was `None` — the one response in the loop that said nothing, arriving
at the one moment when declaring victory is the cheapest continuation available to the model. It
was not overriding an instruction; it was filling a silence.
**Fix:** `_finish_task` always states the next step. All tasks done + `AWAITING_COMPLETION` asks
for the completion report explicitly (`request_user_approval`, `decision='ASK_USER'`, per-task
`plan_summary`, plus "do not declare success yourself"); `COMPLETED` asks for the final answer.
`advanced is None` is deliberately **not** treated as "the plan is finished" — with
`auto_advance` off, tasks can remain — so that case names the next task instead. The agent
prompt (Phase 3) now also names `message` as an instruction channel in R4/R7, since the fix
otherwise depends on the model reading a field the prompt never mentioned.
**Lesson (D17 below):** the same reading applies to a *state*, not just a channel — DRAFTING
with every task DONE was a sentence the protocol had no grammar for.

**Lesson:** in a redundant protocol, a channel that carries orders is also making a promise, and
its **absence is read as content**. `None` was not neutral — it meant "no further orders" to a
model that had been trained by the preceding turns to expect one. Whenever an instruction field
can go empty, ask what the emptiness says at that exact state, especially at the last step of a
loop where the model's own priors already point at stopping.

<a id="d17"></a>
## D17 — Revising a completion report made the model useless (1.13.0)
**Symptom:** reported from the field. `계획 → 승인 → 실행 → 완료 보고 → 완료 승인` runs cleanly on
a small model. The moment the human presses **수정 요청** on the *completion report*, the same
model falls apart: it re-runs tasks it already finished, or fires evidence-free `DONE` at each
one and loops on `MISSING_RESULT_LOG`, or tries to answer and is blocked by `execution_guard`.
The one thing it never does is the fix the human asked for.
**Root cause — not the model. Six steps, each individually defensible:**
1. The page offered no per-task comment on a completion report (`perTask` required
   `phase==='PLAN'`), and `record_decision` clamped any non-PLAN phase to `SCOPE_PLAN`. There
   was no way for the human to say *"only task 3"* about finished work.
2. `_mutate_revise` therefore always ran, sending the plan back to `DRAFTING` with every task
   still `DONE` — `plan_status=DRAFTING, progress=7/7 done`, a state the protocol has no grammar
   for.
3. With no targets, `_status_action`'s DRAFTING branch gave the *generic* hint. **The human's
   sentence appeared in no instruction field at all** — only once, in `user_comment`, which is
   three turns back in a small model's context by the time it acts.
4. `task_updates` was refused (`REVISION_NOT_REQUESTED`), so the only legal move was a full
   `task_list`.
5. Finalizing replaces `plan.tasks` wholesale — **every `result_log` was deleted**. It survives
   in `superseded_tasks`, which no response ever carries, so the model can never see it again.
6. Re-approval of a byte-identical plan, then all N tasks to be redone under the ordering and
   evidence guards.
**Fix:** completion revision became **rework**, not re-planning. Per-task comments are enabled
for `PHASE_COMPLETION` (the clamp now only catches an entry with *no* phase — the rolling-restart
case). `_mutate_no` routes on `plan.status`: `AWAITING_COMPLETION` + targets → `_mutate_rework`,
which reopens only the named tasks (`PENDING`, `revision_note` set, `previous_result_log` kept so
the redo is not blind), leaves every other task `DONE` with its evidence, and returns the plan to
`IN_EXECUTION` **without re-approval** — the task list never changed, so re-approving it is
ceremony that costs a weak model two turns and a chance to derail. `_status_action` leads with
the human's own sentence and forbids re-planning; `_rework_suffix` repeats it wherever a task is
handed over, including auto-advance. The whole-plan escape hatch survives for add/delete/reorder,
but now carries evidence across the redraft (`_carry_evidence`, matched with `title_key`).
**Why nothing else had to change:** reopening task 3 leaves 1–2 `DONE`, so `can_start_task`,
`unfinished_before`, `current_task`, `_auto_advance` and `all_done` all keep working untouched —
a reopened task is simply the next `PENDING` one. The machinery was already right; only the
transition into it was wrong.
**Second-order hole closed by the fix:** if a redraft carries *every* task, approving it would
leave an `APPROVED` plan with nothing to run and `next_action = ANSWER_USER`, skipping the
completion gate entirely. `_mutate_approved` now routes an all-done approval to
`AWAITING_COMPLETION` instead.
**Lesson:** one button meant three different things (*this plan is wrong* / *you missed a step* /
*this output is wrong*), and the state machine only implemented the first. When a control is
reachable from two states, ask what it means in **each** — a shortcut that is merely wasteful
before execution becomes destructive after it, because by then there is work to destroy.

<a id="d18"></a>
## D18 — The human was asked to certify evidence ending in `...` (1.13.1)
**Symptom:** on the approval page, every task's `result_log` was cut mid-sentence with a
trailing `...`. The full text was in `plan_state.json` the whole time.
**Root cause:** `Task.brief` capped `result_log` at 200 characters, documented as "compact form
sent to the LLM — capped to protect context". That reasoning was sound for the audience it named
and wrong about who the audience actually is: `handlers._wait_for_human` passes
`plan.tasks_brief()` straight into `open_request`, so the **approval page is built from the same
dict**. The completion gate exists so a human can check whether claimed work is real, and it was
showing them the first sentence of the claim. The two surfaces had also drifted:
`render_completion_report` (the text the model echoes into chat) used `result_log` directly and
was never truncated, so the chat transcript was complete while the page a person actually decides
on was not.
**Fix:** the cap is gone. Both surfaces render the stored text. `previous_result_log` — added in
1.13.0 for rework — had the same split and was likewise only in the text report, so the page rows
now show `↻ 요청하신 내용` / `이전 결과` / the new outcome together, which is what makes a
reworked task checkable in one glance.
**If context ever really is the problem:** cap where evidence is **written** (a maximum beside
`min_result_log`, refused at `update_task_progress` with a corrective hint), never where it is
read. A read-side limit silently disagrees with what the store holds, and the disagreement lands
on whoever is furthest from the code.
**How it was found:** not by a test — by driving the whole lifecycle through a real stdio MCP
server with a real browser on the approval page and *looking at it*. Every assertion in the suite
was green; none of them said "a human can read this".
**Lesson:** a truncation is a lie about your own data, and it is told to whichever audience you
were not picturing. When one serializer feeds both a model and a person, the person's constraints
win — they cannot ask a follow-up question.

<a id="d19"></a>
## D19 — A rework could be "finished" by resubmitting the rejected outcome (1.13.2)
**Symptom:** none in the field — found by scripting a model that ignores every hint and seeing
what the server physically stops. Most routes were already closed (see the table in
[06](06-human-in-the-loop.md#rework-1130)): an accepted `DONE` task cannot be restarted or
overwritten, and `plan_and_think` short-circuits on an approved/running plan, so neither a fresh
`task_list` nor `task_updates` can reach the task list or its evidence.
**The one open route:** report the reopened task `DONE` with the *same* `result_log` it already
had. Accepted, plan moves to `AWAITING_COMPLETION`, and the human is shown a "reworked" task
whose outcome is identical to the one they rejected a minute earlier. For a weak model this is
the cheapest possible continuation — the text is right there in `previous_result_log`.
**Fix:** `REWORK_NOT_DONE`. A reopened task (`revision_note` set) reporting evidence that
normalizes equal to its `previous_result_log` is refused, with a hint that quotes what the user
actually asked for and adds the honest alternative — *"if you believe the original was already
correct, say so to the user instead of reporting it as redone."* Matching is on normalized text,
so whitespace or case cannot launder it. Scoped to reopened tasks: an ordinary plan has no
rejected outcome, and two tasks that legitimately produce the same sentence must still pass.
**Lesson:** adding a field for the model to read (`previous_result_log`, 1.13.0) also hands it
the perfect forgery. Every piece of context you give a weak model to help it do the work is
equally a way to *look* like it did the work — so when you add one, ask what the laziest
possible use of it would be, and close that first.

<a id="d20"></a>
## D20 — the model could approve its own plan (1.14.0)

**Root cause:** `_approve` guarded on `plan.approval.requested_at`, which answers "was this
version ever shown to a human?" — not "did a human answer?". That field is set *because* the
server is waiting, so the guard was at its most permissive at exactly the moment the model was
most likely to guess.
**Symptom:** none observed, but the route was fully open: `request_user_approval(decision=
'APPROVED')` from the model unlocked execution with nobody having clicked anything. Every other
layer holds (`execution_guard` refuses tasks while `AWAITING_APPROVAL`), which is precisely why
this was invisible — the model did not need to break the execution gate, it could just open it.
**Fix:** while an undecided request for this plan id *and* fingerprint is live in the approval
store, a model-sourced `APPROVED`/`REJECTED`/`REVISE` is refused with `APPROVAL_PENDING` and
audited as `self_approval_refused`. The store is the authority, not the plan record: the request
lives there, it is shared across processes, and it is withdrawn the instant a real decision
lands — so a genuine click is never blocked (`_apply_late_decision` runs first in `dispatch`).
**Lesson:** "was the question asked?" and "was it answered?" are different questions, and a
field that tracks the first will always look most permissive while you wait for the second. When
a guard protects against the model inventing an input, check the channel the *human* uses, not
a flag the server set on the model's behalf.

<a id="d21"></a>
## D21 — the heartbeat bet, and the swallowed cancellation (1.14.0)

**Root cause:** the blocking wait extended past the client's 60 s request timeout by sending
`notifications/progress`, on the documented assumption that `resetTimeoutOnProgress` defaults to
`true` and no `maxTotalTimeout` is set. That option **defaulted to `false`** in the TypeScript
SDK and was flipped later ([typescript-sdk#849]) — and it is a per-request option the *client*
passes, so a server can neither set it nor observe it. Separately, `notifications/cancelled` was
swallowed with the other notifications, so the server never learned when a client gave up.
**Symptom:** the tool call dies at 60 s with a heartbeat thread still ticking. The client
discards the result ("No result received from client-side tool execution"), the conversation
breaks mid-approval, and whatever the human clicks afterwards only lands if they type another
chat message. Meanwhile the server held the wait — and its thread — for up to the full 900 s
for a reply nobody would ever read.
**Fix:** chunked waiting. One call now lasts at most `call_budget` (45 s) regardless of
heartbeats, and ends in an `APPROVAL_PENDING` response telling the model to call straight back;
the human's 900 s budget is measured from the request's `created_at` so it spans slices and
restarts. `notifications/cancelled` is routed to the waiting call through an in-flight registry
keyed by `str(request_id)`, unblocks it, and records the observed limit in `client_caps.json`
so later slices shrink under it. The heartbeat is still sent, but nothing depends on it.
**Lesson:** never build a safety guarantee on a switch the other side owns — especially one you
cannot read back. And a notification you "safely ignore" is often a measurement you are
throwing away: cancellation was the only direct evidence of the one number that mattered.

<a id="d22"></a>
## D22 — a new browser tab per approval, and a rebuild that ate what you typed (1.14.0)

**Root cause:** two independent bugs that only became painful together. `_surface` called
`_open_browser_once` on every request, and that function never checked the `_opened_once` flag
it is named for — the guard lived only in `start()`. Meanwhile the page rebuilds its entire card
list (`root.innerHTML = ...`) whenever the queue signature changes, and per-task comments lived
only in the DOM.
**Symptom:** each approval request opened another window. A human typing a comment in tab 1 got
a fresh empty tab 2 for the next request; submitting from tab 2 silently discarded what they had
written. Even in a single tab, a *second session* asking for approval was enough to wipe a
half-finished comment with no trace. Chunked waiting would have made this fire every 45 s, since
each slice re-published the request under a new uuid.
**Fix:** three parts. `publish` reuses an undecided entry when plan id and fingerprint match, so
a slice never changes the page signature (and `created_at` is preserved, so the total budget can
actually expire). Drafts moved to `localStorage` keyed by request id, restored on every render,
mirrored across tabs by a `storage` listener, and cleared only after a decision posts
successfully. `_surface` skips opening a browser when a tab polled within the last 10 s, and
`_open_browser_once` honours its flag. (That last clause was itself the next bug — see D23.)
**Deliberately not fixed with partial rendering,** which was the obvious repair: keeping DOM
nodes per request would let two tabs drift apart, and the human would submit from whichever tab
happened to lack their text. Keeping the full rebuild and moving the *state* out of the DOM
preserves "every window shows the same thing" — and extends it to drafts, which the old code
never had.
**Lesson:** when re-rendering destroys user input, the instinct is to re-render less. The better
question is why the input was somewhere a re-render could reach. Also: a function named
`_open_browser_once` that opens the browser every time is exactly the kind of bug a reader skims
past, because the name asserts the invariant the code forgot.

<a id="d23"></a>
## D23 — close the approval window and it never comes back (1.14.1)

**Root cause:** the D22 fix overshot. `_opened_once` is a **permanent** latch: the first
successful `webbrowser.open` sets it, and `_open_browser_once` returns early forever after.
`_surface` already had the correct guard — `page_is_being_watched()`, which expires — so the
latch added nothing except an unrecoverable state.
**Symptom:** found in live testing, not by the suite. Suppressing a duplicate tab worked; the
inverse did not. Close the approval window and every later request surfaces nothing at all —
the log line still says `HUMAN APPROVAL NEEDED`, the agent keeps slicing its 45 s waits, and the
human sits in front of a browser with no page, waiting on a window that will never open. It
ends when the 900 s budget expires. Chunked waiting makes it worse than it looks: the retries
that would have reopened the tab are exactly the calls the latch silences.
**Fix:** replace the boolean with `_last_open_at` and suppress for `OPEN_GRACE_SEC` (10 s)
instead of forever. That grace covers the only real gap — a browser that has been launched but
has not polled yet — and then lets go. Both suppression rules now expire, so any state the
server ends up in is one it can leave. `start()` calls the same guarded function rather than
reproducing the check.
**Lesson:** the two tests written for D22 both asserted *suppression* (`_opened == 1`), and
both passed against a latch that could never release. A test that a thing does not happen is
only half a test; the missing half is that it happens again once the reason expires. When a fix
for "too often" lands, ask what now makes it happen *at all* — and prefer a timestamp to a
boolean whenever the condition being tracked is one the world can undo.

<a id="d24"></a>
## D24 — a new window every 45 seconds, at a human already looking at the page (1.14.2)

**Root cause:** the D23 fix put the whole weight on `page_is_being_watched()`, and that
function was process-local. `_last_poll_at` is set by the HTTP handler, so only the instance
that *owns* the page ever updates it; every other planning-mcp instance on the same state
directory is a peer whose counter stays at zero forever. And the instance that decides whether
to open a browser is whichever one is **holding the approval request** — routinely a peer. The
old code carried a comment admitting this and calling it safe: *"the worst case is one extra
tab, not a request nobody sees."* That was only true because the permanent latch capped it at
one. Remove the latch and the same blind spot fires on every slice.
**Symptom:** found in live testing, immediately. Two planning-mcp processes were running
(confirmed: PID 22412 held `127.0.0.1:8765` with the browser connected to it; the peer served
the tool calls). A new browser window every 45 seconds — one per slice of the chunked wait —
in front of a human who had the page open the whole time. Strictly worse than D23.
**Fix:** liveness moved to the state directory like everything else that is shared. The
handler writes `page_seen` (throttled to 2 s, unlocked and non-atomic — a lost write costs one
stale reading in a direction that is already bounded), and `page_is_being_watched` takes the
max of that and the local counter, so a fresh poll still counts before it is written out. The
launch grace also backs off exponentially when a launch never produces a poll, since
`webbrowser.open` returning `True` does not mean a window appeared.
**Lesson:** three fixes in a row to the same six lines, each correct about the bug in front of
it and blind to the state it left behind. The through-line is that *this server is
multi-process by design* — plans, decisions and locks were all shared through the state
directory, and page liveness was the one fact still kept in a local attribute. A comment
explaining why a known-wrong value is tolerable is a load-bearing assumption; when the code it
was written against changes, that comment is where the next bug is. Tests would not have caught
this either: every browser test used a single `ApprovalServer`, so the peer — the whole
deployment topology — was never in the picture.

[typescript-sdk#849]: https://github.com/modelcontextprotocol/typescript-sdk/pull/849

## Where the next bug probably is

Judging by the pattern (bugs cluster in untested seams and failure paths), the thinner-covered
areas still are:
- `models.py` serialization round-trips at the boundaries (unusual `from_dict` inputs, huge task
  counts, retention pruning interacting with multi-plan).
- `config.py` env-var parsing (already partly covered, but not every coercion path).
- The **SSE session lifecycle** under abrupt client disconnects (queues left in `_SseSessions`).
- Interaction of **retention pruning** (`max_plans`) with **many active plans** — pruning must
  never drop an active plan (there is a test; extend it for the multi-plan case).
