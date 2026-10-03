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

A card is redrawn whenever what it shows changes - and until 3.1.0 *every* card was, on
any change; a second session asking for approval was enough (see
[one card at a time](#one-card-at-a-time-310)). Two fixes keep a half-written comment safe:

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

> **3.0.0 moved this boundary; it did not remove it.** Two claims in a `result_log` are now
> things the server checks rather than takes on trust - a file the task says it produced, and
> evidence that says more than the criterion did - and the human reads what is left against a
> criterion they approved, with the checked and the unchecked told apart. A model that writes a
> plausible *new* sentence about work it did not do still passes. See
> [The contract on the page](#the-contract-on-the-page-300).

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

## Choosing between alternatives (2.0.0)

```
2.  집계 방식 · 3가지 중 선택
    ◉ A. 엑셀 피벗으로 매출 집계  [권장] — 보고서 서식이 그대로 유지됨
    ○ B. CSV로 내보낸 뒤 스크립트로 집계 — 빠르지만 서식이 사라짐
    ○ C. 수작업으로 합계 계산 — 느리지만 도구가 필요 없음
    ○    기타 (직접 입력)
```

- The heading says what is being chosen - the model's `topic` ("집계 방식") and the number of
  options. Without a topic it reads "진행 방법 · N가지 중 선택". It used to say "다음 중 하나를
  고르십시오", which told the human nothing about what they were choosing. Display only: the
  topic is not in the fingerprint and changes nothing about what runs.
- The recommendation (A) is pre-selected and marked 권장 with its reason; every alternative
  shows its trade-off, so the human can pick without asking.
- The approve button states its consequence before it is clicked, like the REVISE button:
  `승인` / `승인 · 2번 B안` / `승인 · 선택 2건 반영`. **기타** opens that task's comment box and
  disables approval (`승인 · 기타는 수정 요청으로`): something other than the options on offer
  is a change to the task, which the per-task REVISE path already handles - so "what the human
  approved is what runs" stays true.
- Picks live in `localStorage` with the drafts (`…:ch<task_id>`): they survive a rebuild, a
  reload, and are mirrored across tabs (D22).
- `record_decision` refuses a pick the request did not show (`validate_page_choices`) - the
  page only ever posts an index it rendered, so anything else is a stale tab or a forged
  request - and the page then keeps its drafts and says the decision was not recorded,
  instead of clearing them as if it had been.
- The **halt card** shows the draft's choices the same way, without 기타 (a direction goes in
  the halt card's own box): `이 초안으로 승인 · 3번 B안`.
- The **completion report** marks each such task `B안 선택` / `권장안`, so the human can check
  it was done the way they picked.
- In chat mode the plan text lists the options (`A. … [권장] — 이유`) and asks for "2번 B안"
  in the reply, which the model relays as `choices`.

Verified in a real browser against a real `ApprovalServer`: defaults and badges, the label
changing to `승인 · 2번 B안`, 기타 opening the comment box and disabling approval, picks
surviving a reload, the click reaching the waiting agent (task 2 handed over as the
alternative with its reason), the completion badges, and a halt-card draft approved with a
pick. The same check found the radios named "0" / "1" in the accessibility tree; they now
carry an `aria-label`.

<a id="the-contract-on-the-page-300"></a>
## The contract on the page (3.0.0)

Two things changed for the human: they say what "finished" means before the work, and they are
shown what the server itself found after it. Server side:
[04](04-state-machine.md#the-verification-contract-300).

**Before the work - writing the criterion.**

```
2.  엑셀 피벗으로 매출 집계                                        [의견]
    태스크 완료 기준  분기별 매출 합계 4행이 있는 표가 만들어진다     [수정]
3.  5줄 요약 작성                               [완료 기준 추가]   [의견]
```

- A task the model gave a criterion shows it with [수정]; a task without one shows only a small
  [완료 기준 추가] button (the row label and the button were 완료 기준 / 기준 추가 until
  3.1.0, and the field carried a sample sentence as its placeholder - removed, because the
  label beside it already says what goes in). Rendered as an open field on every row it would be the twelve textareas of
  1.11 again - the plan is what the human came to read.
- The human writes or rewrites it **and approves in the same click**. Nothing about the task
  changed, so there is nothing for the agent to redraft and no round trip through 수정 요청 -
  the reason this is on the approve path at all. The button states it first, like every other:
  `승인 · 완료 기준 1건 반영`, or with a choice `승인 · 변경 3건 반영`.
- What is typed is a draft like a comment: in `localStorage` (`…:dw<task_id>`), surviving a
  rebuild and a reload, mirrored across tabs (D22). Erasing a criterion is a real edit - it
  removes it - so "no draft" and "erased" are stored differently.
- Only tasks whose text differs from what was shown are posted (`criteria`). The store refuses
  the whole decision if one names a task the request did not show, or a finished one
  (`validate_page_criteria`) - the page then keeps the drafts and says the decision was not
  recorded, as since 2.0.
- **The model cannot set one.** With choices there is a chat-mode relay; here there is no field
  in any mode. In chat mode the user says what they want and the ordinary revision path runs.
- **What the human replaced is gone from the model's view.** The task carries only the current
  criterion; the model's own wording survives in the audit log (`criteria_applied`) and nowhere
  else. `TestTheHumanWritesTheCriterion` scans a whole lifecycle for it.
- **Typed, then 수정 요청 instead of 승인:** nothing typed is dropped. A per-task revision keeps
  the tasks, so the criteria are applied to them - and a criterion the human wrote stays on its
  task even when the model rewrites that task's wording. A whole-plan revision replaces every
  task, so they travel in the comment the model reads while redrafting.
- A **finished** task has no editor: what "done" meant for work already done is not changed
  afterwards. The **halt card** shows the draft's criteria read-only.
- `PLANNING_MCP_DONE_WHEN=off` removes the *model's* field. The buttons stay: a criterion the
  human writes is still handed to the model and still checked.

**After the work - the completion report.**

```
완료 확인 · plan_20261003_0001
3개 중 1개는 태스크 중에 만들거나 바꾼 파일을 서버가 확인했습니다. 나머지 2개는 에이전트의 보고가 근거입니다.

1.  집계                                                소요 2분 14초
    완료 기준  합계 4행 표가 생긴다
    → 표를 만들어 pivot.xlsx 로 저장
    ✔ pivot.xlsx · 14.2 KB · 이 태스크 중 생성/변경됨
2.  원본 확인                                           소요 3초
    → q3.xlsx 확인
    · q3.xlsx · 900 B · 작업 전부터 있던 파일 (이 태스크에서 바뀌지 않음)
3.  요약                                                소요 1시간 6분
    완료 기준  요약이 5줄이다
    → 요약이 5줄이다
    ⚠ 처음에 summary.md 을(를) 결과 파일로 적었다가 뺐습니다 (서버가 찾지 못함)
    ⚠ 증거가 완료 기준 문장을 거의 그대로 반복합니다
```

- The criterion sits directly above the evidence it is read against.
- The `✔ / · / ⚠` lines are the only lines on the card that are not the agent's word. The
  header counts the tasks for which the server confirmed a file **the task itself created or
  changed** - a file that was already there gets its line but does not move its task out of
  "the agent's report". The header appears only when at least one file was checked for the plan;
  without allowed folders it would say "0 of N" on every report and mean nothing.
- `소요` is `finished_at − started_at`, already recorded. Shown, not judged: no threshold was
  set without field data.
- A file that was there when the task reported `DONE` and is gone when the report is asked for
  reads `보고 당시에는 있었으나 지금은 없음`.
- Tasks of a repaired plan keep their `✕ 이전 시도 실패` line here too.

**Re-approving a repaired plan** ([local repair](04-state-machine.md#local-repair-300)) is an
ordinary PLAN request. The rewritten task shows its old wording struck through and
`✕ 이전 시도 실패: <reason>`; finished tasks show `DONE` and are not editable; the human approves,
comments per task, or rejects exactly as on a first approval.

Verified in a real browser against a real server through all three requests of one plan - see
[07](07-testing.md#browser-verification-manual-for-the-approval-page). That check found
[D27](09-defects-and-lessons.md#d27).

## Version label and information dialog (3.0.0)

Above the card, right-aligned and small: `planning-mcp 3.0.0` and an `i` button.

- **One element for every state.** The label sits outside `#root`, which is the only part
  `render()` replaces - so the idle screen, a plan request, a completion report and a halt
  card all show the same label, it is in the first paint (the version is filled in when the
  page is served, `page_html()`, not fetched by script), and no re-render can drop it.
- **Why it is there.** After an upgrade, "is the new version actually running?" had no answer
  on screen: a process keeps the modules it imported until it is restarted, so the files on
  disk say nothing about the page in the browser ([11](11-status-and-next-steps.md)).
- **It is the page's version, and says when a request is not.** The page is served by one
  process and asked by any of them. Each request records `server_version` when it is
  published; the card adds a line when that differs from the page's (`이 요청은 planning-mcp
  2.0.0 서버가 보냈습니다`) or is absent (a process older than 3.0.0). Silent when they agree.
- **The `i` opens a dialog**: `Author`, `Email`, `Version` (`SERVER_AUTHOR`,
  `SERVER_AUTHOR_EMAIL`, `SERVER_VERSION` in `config.py`). A native `<dialog>` - Esc closes
  it and focus stays inside - with a click on the backdrop closing it too, and a plain
  `alert` where `showModal` does not exist.
- **`decide()` disables `#root button` only.** It used to disable every button on the page
  while a decision was posted, relying on the next render to rebuild them. The icon and the
  dialog's button are never rebuilt; disabled once, they would have stayed disabled.
  (Since 3.1.0 only the buttons of the card being decided are switched off - `lock(key)` -
  because only that card is redrawn.)

Verified in a real browser: the label on the idle screen and above a plan and a completion
request; the two mismatch lines; the dialog's three lines; closing by button and by Esc; the
icon still working after a decision; no console error.

## The gate on the transition (3.1.0)

Until 3.1 the gate was a tool the model had to remember: finalize the plan, *then* call
`request_user_approval`; finish the last task, *then* call it again. The enforcement half
never depended on that - execution stays locked either way - but the lifecycle did: a model
that answered the user instead of asking left a plan nobody was ever shown.

Both moments are state transitions the server already sees, so it asks at them itself
(`_asks_itself`; `PLANNING_MCP_AUTO_ASK`, default on):

| the model's call | what the server does in that call |
|---|---|
| `plan_and_think` with the final `task_list` (or `task_updates`) | records the plan, opens the PLAN request, waits |
| `update_task_progress` `DONE` on the last task | records it, opens the COMPLETION request, waits |

The wait is the one `request_user_approval` always did - same slices, same request reuse,
same fingerprint check (`_ask_user(own=True)` → `_wait_on` → `_settle`). A decision made
during it is the answer to that call. If the slice ends first the response is
`APPROVAL_PENDING` and says what the call did record ("Your plan is recorded (3 tasks) ...",
"That task is DONE and every task in this plan is finished ...") so the model does not send
it again; `request_user_approval(ASK_USER)` then keeps waiting. With a page that is the only
thing the tool is still for; without one it reports the user's reply.

Consequences:

- **No `plan_summary`.** The model never opens the request, so it has no overview to write.
  The page's 개요 is the `thought` of the call that recorded the plan (nothing, if it sent
  none). In the 3.0 flow `plan_summary` is still required the first time a plan is asked
  about, and nowhere else: a completion report shows each task's evidence, and a request
  already on the page keeps the overview it was opened with.
- **One call, one slice.** `_CallCtx.wait_spent` is subtracted from every later wait in the
  same call. A loop trip decided earlier in the call (a respawn) suppresses the plan request;
  the halt card, which shows the same task list, asks instead.
- **Modes.** `chunked`: as above. `return`: the call returns `display_to_user` at once and
  the model ends its turn. No page: the call returns `display_to_user`, and the model reports
  the reply - `APPROVED` is accepted with no `ASK_USER` in between, because the plan *was*
  shown. `PLANNING_MCP_AUTOAPPROVE`: nothing is asked, and the tool texts are the 3.0 ones.
- **An agent still on the 3.0 prompt keeps working**: its `request_user_approval(ASK_USER)`
  joins the request that is already open.
- **A decision made between two calls** is collected at the top of the next one. When that
  call is the waiting call itself the plan is no longer waiting to be asked about, and it is
  not asked about again ([D30](09-defects-and-lessons.md#d30)).

## The run card (3.1.0)

Between the plan approval and the completion report the page used to be empty, and the host
this server is written for asks nothing before it runs a tool. The human could neither see
the work nor stop it.

`state/runs.json` (`approval.RunBoard`) lists the plans that are executing: goal, tasks with
status, the evidence and file checks of the finished ones, and when the entry last changed
("에이전트의 마지막 보고"). It is derived, never edited by a handler: `_sync_runs` rebuilds it
from the plan state at the end of every call and before every wait, so a plan is on it
exactly while it is `APPROVED` / `IN_EXECUTION`, not halted, and its approval has not gone
cold (`expires_at`). Its own file rather than a list in `approval.json`: an older process on
the same state directory rewrites that file whole, and would drop a stop without anyone
noticing.

A run card is not a question - no alarm, no title flash. It carries one text box and two
buttons. Both are recorded as a *request* on the board (`POST /api/control`) and applied by
the server when the agent next reports a task:

- **멈춤 (`PAUSE`).** The `DONE` is recorded first; the next task is not started; the plan is
  held with `halt.reason = "user_pause"`. The halt machinery of 1.16 is reused whole - card,
  wait, three answers - under other words: 실행 멈춤, 멈춘 이유, [계속 진행] / [의견 전달 후
  계속] / [계획 취소], error code `PLAN_PAUSED`, and a hint that says the user paused the plan
  and nothing about repeating. Continuing starts the task the stop held back
  (`_auto_advance`), and what the human typed leads the next hint (`plan.guidance`).
- **의견 전달 (`NOTE`).** The unfinished tasks are opened through local repair with a third
  origin: `pending_revision = {targets: {first unfinished: note}, origin: "run", open: [the
  rest]}`. The model answers with `task_updates` for the tasks the note changes (at least
  one; a finished task is ignored), each rewritten task carries the note and its previous
  wording, and the change goes back to the human in that call. Their words end up in the task
  text - re-read, re-approved, restated at execution - not in a hint the model is trusted to
  remember.

What the page says about timing is exactly what is true: the request is shown as *asked*
("멈춤을 요청하셨습니다 ...") until the agent reports, it can be taken back with [요청 취소]
(the words return to the box), and "이미 실행 중인 도구는 중단되지 않습니다".

Edges, each decided so that nothing typed is lost:

- applied before a task is started (`IN_PROGRESS`) as well as after a `DONE`;
- a `DONE` that is refused does not spend the request - it applies when the `DONE` is accepted;
- after the **last** task there is nothing left to stop or change: the completion report is
  asked and shows the words ("실행 중 남기신 의견 ..."; `plan.run_note`, cleared when the
  report is answered);
- with a **`FAILED`** the failure stops the plan anyway: the words go to the model in that
  response ("the user also wrote ...") where they can shape the repair;
- a memo typed beside 멈춤 is shown on the pause card and becomes the direction if the human
  resumes without typing another;
- consumed only after the plan is saved (`_consume_run_control`), so a write that fails
  leaves the request on the page instead of dropping it.

The card redraws each time the agent reports a task. What is typed is a draft like any other
and survives the redraw; the caret is put back (`focusKey` / `refocus`).

### One card at a time (3.1.0)

A task reported by one plan must not disturb the human reading another plan's request. The
page used to rebuild every card whenever anything changed (`root.innerHTML = ...`); with run
cards that is every task of every running plan, and each rebuild closed a comment box that
had been opened but not yet typed into, dropped a text selection, and moved the focus out
and back.

`draw(list)` keeps one element per request (`q-<request id>`) and per run (`run-<plan id>`)
and compares a signature of what each shows - for a request `decided`, `agent_waiting` and
the length of the agent note (its content is fixed by its id: a changed plan is a new
request); for a run its `rev` and the id of a pending control. A card whose signature is
unchanged is not touched; one already in its place is not moved (moving a node blurs it as
surely as redrawing does). Drafts are restored, and the caret put back, only in the card
that was drawn. The line between two cards is a CSS rule on the second (`.item+.item`), and
the error line and the hint paragraph are their own elements, so none of them redraws a
card. A decision switches off and redraws its own card (`lock` / `stale`).

Verified in a real browser with two plans on one page: while plan B reported two tasks, the
card of plan A's request stayed the same node with the same children, an opened empty
comment box stayed open, and the focus and the caret in its text box did not move; rejecting
A removed A's card and left B's untouched, with its buttons enabled.

`PLANNING_MCP_RUN_CONTROL=false` turns the card and both controls off.

Verified in a real browser against a real server: the request opened by the plan call (with
the model's sentence as 개요), the run card appearing on approval and following each task, a
stop asked mid-task (shown as asked, applied on the next report, task kept), continue, a note
(text and caret surviving a redraw; the re-approval card marking the rewritten task), and the
completion report opened by the last DONE.

## Config knobs

`PLANNING_MCP_BLOCKING_APPROVAL` (default true), `_APPROVAL_PORT` (8765), `_APPROVAL_TIMEOUT`
(900), `_APPROVAL_OPEN_BROWSER` (true), `_APPROVAL_TTL` (1800), `_AUTO_ASK` (true, 3.1.0),
`_RUN_CONTROL` (true, 3.1.0). Full list:
[data/config.json](data/config.json).
