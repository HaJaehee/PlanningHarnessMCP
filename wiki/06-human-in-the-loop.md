# 06 · Human-in-the-Loop (Blocking Approval)

The core insight of this project. Logic: `handlers._wait_for_human` + `approval.py`.

## Why blocking is necessary

AnythingLLM's agent loop feeds a tool result back to the model and lets *the model* decide
whether to keep calling tools. Returning `STOP_AND_WAIT_FOR_USER` as text does **not** stop a
weak model — it reads the instruction as one more observation and calls the next tool. As of
AnythingLLM 1.15.0 there is no host-side lever for this: `directOutput` is Agent-Flow-only, MCP
config is server-level only, and the custom-skill `"exit"` return is unofficial and prompt-
dependent (i.e. the thing that already fails).

## The mechanism: don't break the loop, make the loop wait on us

The agent loop waits **synchronously** for a tool result before the model can generate anything
else. So if `request_user_approval(ASK_USER)` does not return, the loop physically cannot
advance. The pause needs nothing from AnythingLLM — no loop patch, no directOutput.

```
model ── request_user_approval(ASK_USER) ──►  server
                                               │ persist AWAITING_APPROVAL
                                               │ publish plan to http://127.0.0.1:8765/
                                               │ store.paused()  ← release lock, let others run
                                               │ ┌ heartbeat: notifications/progress q20s
   agent loop BLOCKED here, cannot execute ◄────┤ └ (resets the client's 60s request timer)
                                               │
        human clicks 승인 / 거절 / 수정요청 ──────┤
        ◄── APPROVED + next_task ───────────────┘  (same call returns)
```

## Timeout arithmetic (this determines everything)

Every MCP client caps a single `tools/call`. The TypeScript SDK's
`DEFAULT_REQUEST_TIMEOUT_MSEC` is **60 s**, and that number is the de facto industry
default: AnythingLLM inherits it, Claude Desktop hardcodes it with no way to configure it
([claude-code#22542], [claude-code#43791]), Cursor matches it. Overrunning it does not merely
fail the call — the client **discards the result and the conversation breaks mid-approval**.

### The heartbeat bet, and why it was wrong (fixed in 1.14.0)

Until 1.13 this server bet everything on progress notifications: send
`notifications/progress` every 20 s, the client resets its timer, and one call could block for
the full 900 s. That bet failed twice over.

1. `resetTimeoutOnProgress` **defaulted to `false`** in the TypeScript SDK and was only later
   flipped to `true` ([typescript-sdk#849]). Any client on an older bundled SDK ignores the
   heartbeat entirely.
2. It is a **per-request option the client passes**. A server cannot set it, cannot read it,
   and cannot detect which way it went. The only symptom is the call dying at 60 s with a
   heartbeat thread still ticking happily.

Betting a safety gate on a flag the other side owns is the bug. The heartbeat is still sent
when a `progressToken` is present — it costs nothing and helps the clients where it works —
but nothing depends on it.

### Chunked waiting (the default)

`approval_mode=chunked` splits the wait into slices of `call_budget` (default **45 s**). Each
slice ends in a normal response that says *not decided yet, call me straight back*, and the
model does. The total wait is still `approval_timeout` (default 900 s), measured from when the
request first appeared — **not** from the start of the current call, so it survives both the
slicing and a server restart.

This is also where the protocol itself is heading: splitting one long call into several
request/response pairs ([SEP-1391] Long-Running Operations, [SEP-1539] Timeout Coordination,
[mcp#982]).

| mode | one call lasts | resumes by itself after a click? | when to use |
|---|---|---|---|
| `chunked` *(default)* | ≤ `call_budget` (45 s) | yes, within one slice | always, unless measured otherwise |
| `return` | ~0 s | no — the user must send a chat message | clients that punish repeat tool calls |
| `trust_heartbeat` | up to 900 s | yes | only where progress resets are *measured* to work |

The slice response is `ok:false` + `error_code: APPROVAL_PENDING`, and it deliberately carries
**no** `tasks`, `display_to_user`, `message` or `next_task`. A weak model reading an `ok:true`
payload full of plan detail as "approved, proceed" is precisely the failure this gate exists to
prevent, so there is nothing in the payload that could be mistaken for a verdict.

### Learning the real limit

`notifications/cancelled` used to be swallowed. Now it unblocks the waiting call and records
how long the client actually allowed, in `state/client_caps.json` (audited as
`client_cancelled_call`). Later slices shrink to stay under the tightest value ever observed.
This is the only reliable way to discover a client's cap, since it is not in `initialize` and
the documented defaults cannot be trusted.

[claude-code#22542]: https://github.com/anthropics/claude-code/issues/22542
[claude-code#43791]: https://github.com/anthropics/claude-code/issues/43791
[typescript-sdk#849]: https://github.com/modelcontextprotocol/typescript-sdk/pull/849
[SEP-1391]: https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1391
[SEP-1539]: https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1539
[mcp#982]: https://github.com/modelcontextprotocol/modelcontextprotocol/issues/982

## Only a human may decide (1.14.0)

`_approve` checked only `plan.approval.requested_at` — a field that is set *because* we are
waiting, so it was at its most permissive exactly when the model was most likely to guess. A
model could call `request_user_approval(decision='APPROVED')` and unlock a plan nobody had
approved. Chunked waiting would have made that far worse, since the model is now asked to call
this tool repeatedly and `APPROVED` is one token away from `ASK_USER`.

While an undecided request for this plan version is live on the page, a model-sourced
`APPROVED` / `REJECTED` / `REVISE` is refused with `APPROVAL_PENDING` and audited as
`self_approval_refused`. Real decisions are never blocked: `_apply_late_decision` runs first in
`dispatch`, and every `_mutate_*` withdraws the request, so a genuine click has already
emptied the queue before the guard looks.

## The page never loses what you typed (1.14.0)

The page rebuilds its whole card list whenever the queue changes, and a second session asking
for approval is enough to trigger it. Two fixes keep a half-written comment safe:

- **`publish` reuses the entry** when plan id *and* fingerprint match an undecided request.
  A chunked wait re-publishes every 45 s; minting a fresh id each time would change the page
  signature, wipe the card, re-fire the alarm, and reset `created_at` so the total budget could
  never expire.
- **Drafts live in `localStorage`**, keyed `planning-mcp:draft:<request_id>:<task_id>`, not in
  the DOM. They survive a rebuild, a reload, and closing the tab. A `storage` listener mirrors
  them into every other open tab, so two windows show the same text — whichever one the human
  submits from carries what they wrote. Empty strings are stored rather than deleted, so
  "I erased that" also survives.

There is deliberately **no countdown**. A countdown measures the tool call, but the request
outlives the call, so the number would be a lie *and* would manufacture urgency the page
explicitly promises is unnecessary. Instead each card carries a liveness chip driven by
`agent_last_seen`: *에이전트가 대기 중* (deciding now resumes the conversation by itself) or
*에이전트가 대기를 멈췄습니다* (the decision still counts, but the user must send one chat
message to continue).

`_surface` also stopped opening a browser window per request. Two rules decide it, and
**both expire** (1.14.1) — an earlier attempt latched "opened once" permanently, which meant
closing the tab closed the door for the rest of the session:

- a tab has polled `/api/pending` within `PAGE_IDLE_SEC` (10 s) — someone is already looking;
- we launched a browser within `OPEN_GRACE_SEC` (10 s) — one is still starting up, which is
  what keeps the chunked wait's repeat calls from stacking windows. This one **doubles** on
  each launch that no tab ever answers, up to `OPEN_GRACE_MAX_SEC` (10 min), because
  `webbrowser.open` returning `True` is not proof a window appeared.

Otherwise it opens. So closing the window and leaving the agent waiting gets you a fresh one
on the next 45 s chunk, not silence.

**Liveness is shared, not per-process** (1.14.2). Only one instance owns the page; the rest are
peers, and the human's tab polls the owner alone. But the instance that decides to open a
browser is whichever one is *holding an approval request* — routinely a peer. So a poll is
written to `page_seen` in the state directory (throttled to `PAGE_SEEN_WRITE_SEC`, 2 s) and
`page_is_being_watched` reads the max of that and its own counter. Reading only the local
counter, a peer concludes "nobody is watching" every time, and a 45 s chunked wait becomes a
new window every 45 seconds at a human who already has the page open.

## The request outlives the tool call (1.5.0)

The wait has a ceiling but the human does not. On timeout the request is **deliberately left on
the page** (clearing it is what made buttons vanish after 55 s before anyone could answer).
Whatever the human clicks afterwards is collected on the **next tool call of any kind**
(`_apply_late_decision`, audited `late_decision_applied`) and applied. A late decision is only
honoured for the exact plan version that was on screen (fingerprint), else discarded.

## Two enforcement layers

1. **Instructional gate:** `STOP_AND_WAIT_FOR_USER` + pre-rendered `display_to_user`. The model
   only has to echo a string — the single most reliable thing a weak model does.
2. **Enforcement gate:** `update_task_progress` checks `plan_status` server-side. Until
   `APPROVED`/`IN_EXECUTION`, every call is `PLAN_NOT_APPROVED`. Even a model ignoring the
   instruction cannot make execution *real*.

Blocking approval adds a third, physical layer on top of these.

## The approval web page (`approval.py`)

- Loopback-only HTTP on `127.0.0.1:8765` (configurable). Serves one self-contained HTML page
  (CSS/JS inlined as string constants — **zero static files, zero dependencies**) plus three
  JSON endpoints: `GET /api/health`, `GET /api/pending`, `POST /api/decide`.
- Renders the **whole queue** (multiple concurrent sessions), each with 승인/수정요청/거절
  buttons and a comment box. Polls every 1.5 s. Tab title flashes `⚠ 승인 대기 N건` and a short
  tone plays on a new request.
- A **PLAN** request renders a header — `계획 승인 요청 · <plan_id>`, the labelled 목표, and the
  model's 개요 (`plan_summary`) — then the task list as rows, each with a collapsed comment box
  behind an [의견] button (see [Per-task review](#per-task-review-19x)). A **COMPLETION** request
  (1.13.0) uses the same rows under a `완료 확인` header, each showing that task's `result_log`
  (or `(증거 기록 없음)`) — the claim the human is actually judging — behind a [다시 작업] button.
  The `DONE` badge is suppressed there, since every task carries it and the evidence line already
  says so. Only an entry with **no phase at all** — written by an older process during a rolling
  restart — still falls back to the original single `<pre>` + one comment box.
- `plan_summary` travels as **its own field** on the request, not only inside the pre-rendered
  `display`. It briefly did not: when the page stopped rendering `display` for PLAN requests, the
  overview went with it and the human was left judging a bare task list. A field the page can
  compose with is the fix; `display` remains what the model echoes into chat.
- **HTML/XSS escaping** is client-side (`esc()`), verified in a real browser: a `<script>` in
  plan text renders as inert text, no console error. The `onclick` handlers pass the request's
  hex id (safe), so escaped plan content cannot break routing.
- **Surfacing is best-effort.** The browser is auto-opened once at startup and on each request,
  but a blocked popup / second monitor / missing default browser can defeat it — so the
  approval URL is also printed to stderr (`APPROVE PLANS AT -> ...`) and included in the chat
  `display_to_user`. **Operational advice: open the tab once and leave it; it polls.**
- If the page cannot start at all, the server **degrades loudly**: the response carries a
  `NOT hard-paused` warning in `input_notes` and audits `approval_ui_unavailable`. It never
  silently disarms.

## Per-task review (1.9.x)

REVISE used to mean one thing: throw the breakdown away and redraft it. For a mid-sized model
that is a waste — the human objected to one line, and the model rewrites five, drifting on the
four nobody questioned. Per-task review makes the narrow case narrow.

**The human's side.** On a PLAN request each task is a row with its own comment box, plus the
global box. The per-task boxes are **collapsed behind an [의견] button** (1.11.0): rendered open,
five to twelve textareas turned the plan into a form and buried the thing the human came to
read. The textarea stays in the DOM whether or not it is revealed, so `comments()` collects it
either way — which means a comment written and then collapsed would otherwise vanish from view
while still being submitted. The row therefore keeps a `filled` marker (amber button) once it
has text, and the REVISE label below counts it regardless. The REVISE button **states its own
consequence before it is clicked**:

| what is typed | button label | `scope` sent |
|---|---|---|
| nothing per-task | `수정 요청 · 계획 전체 재작성` | `PLAN` |
| task 3 only | `수정 요청 · 3번만` | `TASKS` |
| tasks 2 and 3 | `수정 요청 · 2개 태스크만` | `TASKS` |
| any, with `☐ 계획 전체를 다시 세우기` ticked | `수정 요청 · 계획 전체 재작성` | `PLAN` |

That is why the server **never infers the scope**: the page already showed the human what would
happen, and re-deriving it from "are there comments?" would overrule what they were shown.

**The model's side.** `TASKS` scope records `plan.pending_revision = {"targets": {...}}`. While
that is set, `next_action_hint` hands the model the literal argument to send —
`task_updates=[{"task_id": 3, "title": "<the rewritten task>"}]` — and names the tasks it must
not touch. `plan_and_think` then rewrites only the flagged tasks: **task ids, positions, and the
`result_log` of untouched tasks all survive.** Unflagged edits are dropped with a note.

**Boundaries, and why.**
- **The completion phase means something different** — see [Rework](#rework-1130) below. Until
  1.13.0 it was excluded outright, and that exclusion was defect
  [D17](09-defects-and-lessons.md#d17).
- **Add / delete / reorder are excluded.** They renumber `task_id`, which breaks the ordering
  invariants in `can_start_task` / `unfinished_before` and any id the model is holding. Those go
  through 계획 전체 재작성; the checkbox label says so.
- **A full `task_list` in answer to a targeted request is accepted**, with a note and an audited
  `targeted_revision_ignored`. It is wasteful, not unsafe — the human re-approves every task
  either way — and refusing would strand a model that cannot follow the hint. Auditing it is
  what makes the waste measurable in the field.
- **`task_updates` with no pending request is refused** (`REVISION_NOT_REQUESTED`). Otherwise the
  model could edit an approved plan on its own authority.

**Mixed versions.** An entry published by an older process has no `phase`; `/api/pending` returns
it as `null` rather than guessing `PLAN`, and the page falls back to the original single-comment
form. A decided entry with no `scope` reads as `PLAN`. Both directions degrade to 1.8 behaviour.

## Rework (1.13.0)

The same per-task machinery, pointed at finished work. On a **COMPLETION** request "these tasks
only" does not mean *rewrite their wording*, it means **do them again** — so the scope value is
identical and the *handler* reads it against `plan.status`. `_mutate_no` is the one place that
decision is made.

| what the human does | result |
|---|---|
| comments on task 2, clicks `다시 작업 요청 · 2번만` | task 2 → `PENDING` + `revision_note`, its old output kept as `previous_result_log`; tasks 1, 3 keep `DONE` + `result_log`; plan → `IN_EXECUTION`, **no re-approval** |
| ticks `☐ 계획 자체를 다시 세우기` | plan → `DRAFTING` + `rework_from_completion`; the redraft carries evidence for tasks whose title survives, and *does* need a new approval |
| clicks 거절 | `CANCELLED`, unchanged |

**Why no second approval.** The task list did not change — re-approving it is ceremony that
costs a weak model two turns and a chance to derail, and the rework is verified anyway by the
next completion report. What the human said is not a withdrawal of their approval; it is an
order about output.

**Why the execution machinery needed no changes.** Reopening task 3 leaves 1–2 `DONE`, so
`can_start_task(3)` passes, `unfinished_before(3)` is empty, `current_task()` returns 3, and
finishing it walks into `all_done()` → `AWAITING_COMPLETION` → a fresh report. The report marks
the reworked rows with `↻ 요청하신 내용` and `이전 결과`, so the human checks their own request in
one line.

This holds when the reopened tasks are **not adjacent**, which is the interesting case. Send back
2 and 5 out of five tasks and 3, 4 stay `DONE` between them: `current_task()` returns the first
`PENDING`, so finishing 2 skips straight to 5, `can_start_task(5)` passes because 1–4 are all
`DONE`, and auto-advance starts it. Ordering still binds in the other direction — reaching for 5
while 2 is outstanding is redirected back to 2, and a bare `DONE` on 5 is `TASK_NOT_STARTED`.

**What the model cannot do, whatever the hint says.** The hint is instruction; these are
enforcement, and they are what actually answers "will a small model really touch only the task
that was sent back?":

| the model tries | what happens |
|---|---|
| start or re-finish an accepted `DONE` task | idempotent no-op; the reply redirects to the reopened task and its `result_log` is not overwritten |
| `plan_and_think` with a new `task_list` | short-circuits — *"This plan is already approved and running"* — the task list and every `result_log` are untouched (`EXECUTABLE_PLAN_STATUSES` guard) |
| `task_updates` to retitle something | same short-circuit; no `pending_revision` exists during a rework |
| ask for approval again, or self-report `APPROVED` | the plan is `IN_EXECUTION`, not awaiting anything; `_approve` refuses and the status does not move |
| report the reopened task `DONE` with the outcome the user just rejected | `REWORK_NOT_DONE` (1.13.2) — the human sent it back *because* that outcome was wrong, so it cannot also be the answer |
| do no real work but write a plausible new `result_log` | **not detectable.** The server never sees the work. This is what the completion report is for, and why the page shows `이전 결과` beside the new one |

The last row is the honest boundary of the whole design: the server enforces structure, the human
checks substance. Everything above only exists to make sure the human is shown the right thing.

**Keeping the request in front of the model.** `_status_action` leads the hint with the human's
literal sentence, forbids re-planning, and names the tasks that must not be touched;
`_rework_suffix` repeats it in `message` wherever a task is handed over — including the
auto-advance into a *second* reworked task, which is the only thing the model reads at that
moment. A request that appears once, three turns back, is a request a small model has already
lost.

## Held calls, agent notes, and the halt card (1.16.0)

[D25](09-defects-and-lessons.md#d25) extended the physical pause from one tool to every tool.

**Held calls.** While a human has a request open for a plan (approval, completion report, or
halt), `plan_and_think` and `update_task_progress` on that plan go through `_hold_for_human`:
they wait on the same request, with the same slicing and budget (`_wait_on`), and return the
human's decision if one arrives (`_settle` is shared by all three entry points). A model that
"reconsiders" mid-approval used to put the plan back to DRAFTING and withdraw the request out
from under the person reading it; now it is paced at one call per slice and changes nothing. If
the slice ends undecided the held call returns a refusal (`APPROVAL_PENDING`,
`PLAN_NOT_APPROVED`, `COMPLETION_PENDING` or `LOOP_HALTED`) - never `ok:true`, which a weak model
could read as "started".

**Agent notes.** What a held `plan_and_think` wanted to reconsider is not thrown away: its
`thought` is attached to the card (`set_agent_note`, latest only) and shown as
**에이전트 추가 의견**. If the human agrees, they press 수정 요청; the model never gets to act on
its own second thoughts unasked. The page signature includes the note's length so a new note
re-renders the card - drafts survive, as since D22.

**The halt card** (`PHASE_HALT`). When the circuit breaker trips, the plan is published as:

```
반복 감지 · plan_20261002_0001
목표 ...
에이전트가 멈춘 이유: 같은 호출이 2회 연속 반복되었습니다.
                     에이전트의 마지막 생각: Wait, let me reconsider ...
현재 초안 · 태스크 4개   (only when there is a draft to approve)
[이 초안으로 승인]  [계속 진행 | 의견 전달 후 계속]  [취소]
```

The three buttons reuse the three existing decision values - APPROVED / REVISE / REJECTED - so a
page served by an older process (rolling restart), which renders the entry with its fallback
form, still produces a decision the new handler can read. The continue button relabels itself
as soon as a direction is typed, the same consequence-first rule as the REVISE button. A halt
decision carries no per-task scope (`record_decision` strips it). The request fingerprint
includes the halt id and the draft, so a decision on one halt can never answer the next.

Verified in a real browser against a real `ApprovalServer`: both cards rendered (halt card with
draft and three buttons; an approval card with the agent note), typing a direction relabelled
the button, and the click released the agent's held call within a second with
`The user said: "..."` leading its next hint.

## Config knobs

`PLANNING_MCP_BLOCKING_APPROVAL` (default true), `_APPROVAL_PORT` (8765), `_APPROVAL_TIMEOUT`
(900), `_APPROVAL_OPEN_BROWSER` (true), `_APPROVAL_TTL` (1800). Full list:
[data/config.json](data/config.json).
