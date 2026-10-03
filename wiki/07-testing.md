# 07 · Testing

**740 unit tests + 6 end-to-end smoke tests, all passing** (as of 3.0.0; one unit test skips
where the OS will not let it create a symlink). Standard-library
`unittest` only. This project's defect history proves the rule: *test the seam before you
trust it.* See [09-defects-and-lessons.md](09-defects-and-lessons.md).

## Running

```bash
python -m unittest discover -s tests     # unit suite (~70 s)
python tests/smoke_stdio.py              # stdio end-to-end
python tests/smoke_blocking_approval.py  # blocking approval, real subprocess
python tests/smoke_shared_approval.py    # one page across 2 processes
python tests/smoke_multi_plan.py         # concurrent plans, 2 processes
python tests/smoke_sse.py                # SSE transport end-to-end
python tests/smoke_chunked_approval.py   # chunked approval wait (default mode)
```

Set `PYTHONUTF8=1` on Windows.

`python tools/verify_install.py` runs the unit suite **and five of the six smoke tests** as part
of the GO/NO-GO acceptance check on the target machine. `smoke_chunked_approval` is *not* in that
list, even though chunked waiting has been the **default** approval mode since 1.14.0 — so the
acceptance check does not exercise the path the target machine will actually take.

## Unit suite layout (`tests/test_server.py`)

One file, `HandlerTestCase` base (fresh temp state dir per test, blocking approval disabled so
tests drive the two-phase path). Test classes, roughly:

| Class | Covers |
|---|---|
| `TestLeniency` | input repair + hostile-input fuzzing (never raises) |
| `TestGuardRails` | schema guards (missing task_list, step normalization, revises_step, …) |
| `TestStateMachine` | transitions, gate enforcement, redirects, idempotency |
| `TestStaleApproval` | approval expiry, binding, new-goal-doesn't-inherit |
| `TestBlockingApproval` | blocking wait, timeout, late decision, fingerprint, UI degradation |
| `TestConcurrencyEdges` | transaction mutual-exclusion, reentrancy, `paused()`, intra-process no-loss |
| `TestMultiPlanEdges` | routing, sibling isolation, ambiguity, max-active, pruning |
| `TestApprovalQueueEdges` | queue semantics, one-decision-wins, concurrent publish, migration |
| `TestStoreFailureEdges` | lost-write reported as failure, corruption quarantine |
| `TestApprovalStoreFailureEdges` | publish/decide failure reporting, stale-entry withdrawal |
| `TestApprovalPageSurface` | the page HTML/JS/endpoints (browser-verified separately) |
| `TestLeniencyDispatchEdges` | dispatch survives every hostile input with the contract |
| `TestPlanStateEdges`, `TestFileLockEdges`, `TestProtocol` | misc edges, locking, JSON-RPC |

## Loop convergence suite (`tests/test_loop_convergence.py`, 1.16.0)

78 tests for [D25](09-defects-and-lessons.md#d25)/[D26](09-defects-and-lessons.md#d26). The field
model cannot be run here, so the guarantee is pinned with scripted models instead: *from any
state, whatever a model sends, the planning calls it can make without a human in between are
bounded, nothing it produced is lost, and every path ends in front of a human or in an answer.*

| Class | Covers |
|---|---|
| `TestLoopRegressions` | the four doors found while planning 1.16 (A–D), pinned |
| `TestThinkingBudget`, `TestReasoningProfile` | budget, countdown hint, draft kept / submitted / used, fresh round after a revision, one-call profile |
| `TestLoopGuardUnit`, `TestLoopBreaker` | counters; each trip reason; halted plans refuse work; chat / page / late resolution; waiting is never a loop; unrelated plans never add up |
| `TestAdversarialThinkingModels` | scripted "thinking models" (always one more step, always revising, re-planning instead of asking / reporting / answering, spamming execution) must converge |
| `TestPromptHygiene` | every profile × approval mode × auto_advance: no removed phrase, no "report APPROVED" in blocking modes, a calm reasoning profile, one tool per drafting hint, and **agents.md == Phase 3 Variant A**. Since 3.0.0 also: the README points at `agents.md`
instead of embedding a copy, `agents.md` stays under 2,600 characters, still says what only the prompt can say, and every rule that left it is still said by a tool description or a hint |
| `TestTelemetry`, `TestLoopReport` | `clientInfo` → audit, `gap_sec` / `reconsider`, the offline report |
| `TestHaltPageTemplate`, `TestHaltAtTheStore` | the halt card and agent note in the page template (no socket); HALT entries at the store |
| `TestConfigDefaults` | new env vars, `max_active_plans` = 20, pre-1.16 state files load |

Measured with the scripted policies (no page, defaults): a standard-profile model that always
asks for one more step reaches a human in at most 12 calls, a reasoning-profile one in 6; one that
keeps re-planning a finalized plan in 5; one that keeps re-planning a completed goal is stopped
on the 4th call.

## Alternatives suite (`tests/test_alternatives.py`, 2.0.0)

85 tests. The guarantees: *what was picked is what runs, only the human picks while a page is
open, after approval the model never sees an option that was not picked, and a plan with no
choices behaves - and fingerprints - exactly as in 1.16.*

| Class | Covers |
|---|---|
| `TestLeniency`, `TestBuildOptions`, `TestChoiceValidation` | input shapes; validation against the task list; strict page / lenient chat validation; a bare number is never read as a pick |
| `TestProposing` | options attached at finalize, never in the model's view, 1.16 fingerprint unchanged, options bound into it, off switch, rewrite drops a choice, DONE work offers none |
| `TestChatModePicking`, `TestPagePicking` | the pick runs (chat relay, page, late decision), D20 for choices, the hint and `next_task` restate an alternative, the completion report shows it |
| `TestTheUnchosenStayUnseen` | scans every response of a full lifecycle for an option that was not picked. Verified to fail when `Task.brief()` leaks options |
| `TestStoreRecordsOnlyWhatWasShown` | the store refuses picks that were not on screen; picks count only on a plan / halt approval |
| `TestDraftAlternatives`, `TestHaltCardChoices` | alternatives kept with the draft, replaced with it, submitted with it; the halt card's choice UI and its fingerprint |
| `TestTopic` | the heading: the model's topic, its aliases (not `label`), the first one per task, the neutral fallback, chat text, page, halt card |
| `TestSchema`, `TestPageTemplate`, `TestPersistence`, `TestConfig` | advertised fields per profile / mode, the page template, round-trips, env |

## Verification contract suite (`tests/test_verification.py`, 3.0.0)

135 tests. The guarantees: *a plan under no contract behaves - and fingerprints - exactly as in
2.0; the server refuses only what is structurally impossible and never looks outside the allowed
folders; only the human changes what "finished" means once a plan is on the page, and the wording
they replaced never reaches the model again.*

| Class | Covers |
|---|---|
| `TestLeniency`, `TestCleanDoneWhen` | input shapes for `done_when` and `files` (a comma is not a separator); a criterion that only says "done" is dropped |
| `TestTheRepeatCheck` | the hand-written pairs the refusal threshold was set from: a criterion said back is a repeat, a report that adds a count / size / name is not. **Change the default threshold only together with this table** |
| `TestExtractPaths`, `TestCheckFiles`, `TestMentions` | paths in Korean and English prose; found / old / empty / missing / folder; a mention is kept only when the file is there |
| `TestTheServerNeverLooksOutside` | `..`, another drive, UNC, a link out of the folder: reported `outside`, and `os.stat` / `realpath` are **not called**; a check that does not come back is `unknown`, never `missing` |
| `TestProposing`, `TestHandover` | the criterion is stored, shown, and handed over with the task (only the current task's) |
| `TestNothingChangesWithoutAContract` | the 2.0 fingerprint formula, no new field in any response, the chat texts line for line |
| `TestEvidenceAgainstTheCriterion`, `TestFilesAtDone` | the repeat refusal and its off switch; `FILE_NOT_FOUND`; a dropped claim is accepted and shown; only a file the task itself produced can stand in for evidence; a repeating refusal reaches the human through the 1.16 breaker |
| `TestCompletionReport` | page rows and chat text; a file removed since is said before the human certifies; opening and saving a file does not void the request, removing it does |
| `TestTheHumanWritesTheCriterion`, `TestCriteriaTypedBeforeAskingForChanges` | written and approved in one click (also by a late click); the replaced wording appears in no later response; the model has no way to set one; what was typed survives a REVISE |
| `TestStoreRecordsOnlyWhatWasShown`, `TestFingerprint` | the store refuses a criterion for a task that was not on screen or is finished; what is in the fingerprint and what is deliberately not |
| `TestDraftCriteria` | kept with the draft, replaced with it, submitted with it, shown on the halt card; a draft without criteria keeps its 2.0 halt fingerprint |
| `TestSchema`, `TestPageTemplate`, `TestPersistence`, `TestConfig` | what is advertised per configuration (and its measured token cost), the page template, round-trips, env |
| `TestVersionOnThePage`, `TestInformationDialog` | the version label is in the served page, outside the part the page redraws, small, and only version characters are filled in; a request records which version asked and the page says so when it differs; the `i` dialog's three lines, its escaping, and that a decision no longer disables it. One test serves the page over real HTTP on a free port |
| `TestToolTextIsLean` | the trimmed tool text: a ceiling on its size (3.0 with everything on costs less than 2.0 did), every parameter still shows what to send, an array of objects shows its whole shape once, each execution rule is present and said once, recovery asks for the `plan_id` before it offers `"current"` (1.15.1) |

## Local repair suite (`tests/test_local_repair.py`, 3.0.0)

34 tests. The guarantees: *finished work is never touched by a repair, whatever the model sends;
a repair that leaves the failed task as it is is not accepted; the human approves the change
before anything runs again; re-planning the whole list still works and still costs what it did.*

| Class | Covers |
|---|---|
| `TestAFailureOpensOneTask` | the marker a failure leaves, the hint's literal argument, the same instruction from every tool, a long reason clipped in the hint and kept whole on the task |
| `TestRepairing` | only the failed task changes; a later task may; a finished one may not; leaving the failed task alone is `REVISION_INCOMPLETE` and writes nothing; a same-wording retry; the bare title |
| `TestAfterTheHumanApproves` | work resumes at the repaired task, runs to the completion report without redoing task 1; a second failure of the same step |
| `TestAPickSurvivesReApproval` | [D27](09-defects-and-lessons.md#d27): the human's pick is not reset by a second approval - after a repair, or after an approval expired |
| `TestWhatTheHumanSees` | page rows and chat text of a repaired plan; the human may still send the repair back |
| `TestThePre30PathStillWorks` | a whole new `task_list` is accepted at its old cost and audited; the off switch restores 2.0 behaviour and 2.0 tool text |
| `TestRepairAndTheContract`, `TestPersistenceAndRendering` | which criterion survives a repair; the open repair survives a restart; a 2.0 `BLOCKED` plan gets the 2.0 instruction |

## Plan eviction suite (`tests/test_plan_eviction.py`, 3.0.0)

32 tests. The guarantees: *the victim is the unfinished plan written to longest ago; a plan in
use is never evicted; nothing is lost silently; an id is never reused.*

| Class | Covers |
|---|---|
| `TestMakingRoom` | the least recently used plan goes (by last write, not creation); only as many as needed; a lowered limit; an unreadable timestamp counts as the most idle; a finished plan never counts; every unfinished status can go; the new plan's response does not mention it |
| `TestAPlanInUseIsNeverEvicted` | the floor at 299 s / 300 s; all plans fresh → refused as before; a model that keeps opening plans gets `max_active_plans` evictions and then refusals; the off switch; a read never evicts |
| `TestNothingIsLostSilently` | the audit line carries tasks and `result_log`; the request leaves the approval page; the state file remembers the id, capped at 50; a hand-edited record cannot wedge the file |
| `TestComingBackToAnEvictedPlan` | all four tools answer `PLAN_EVICTED` → `ANSWER_USER` with no `active_plans`; the same goal started again is a new plan; an id that never existed is still just unknown; survives a restart; a repeated request is stopped by the breaker |
| `TestAnIdIsNeverReused` | [D29](09-defects-and-lessons.md#d29): after an eviction, after many, after the remembered list is gone, after retention pruning, and from a pre-3.0 state file |

## Packaging suite (`tests/test_packaging.py`, 3.0.0)

8 tests. The package is the only thing that reaches the corporate PC, so: every file the
README links to is in it (`agents.md` was not, after the prompt moved out of the README),
every module and test is in it, nothing private is (`state/`, `dist/`, `runtime/`,
`CLAUDE.md`); and the repository's manifest never names the bundled runtime while the
archive's does.

## Smoke tests (real subprocesses, real HTTP)

These spawn `server.py` exactly as AnythingLLM does and speak JSON-RPC over the pipe. They
catch things unit tests cannot (threading, real ports, cross-process locks):

- **smoke_stdio** — full lifecycle + sloppy-input recovery + Korean round-trip + restart persistence.
- **smoke_blocking_approval** — 6 scenarios: tool blocks, heartbeat with token, click→APPROVED,
  every call returns inside the client's 60 s limit, the request survives a spent budget and a
  late click still lands, blocking doesn't stall other sessions.
- **smoke_chunked_approval** — the 1.14.0 wait, in three scenarios: a slice ends inside the
  client's limit as `ok:false` + `APPROVAL_PENDING` carrying nothing that reads as a verdict;
  re-asking reuses the same request so the page never redraws; a click during a later slice
  unlocks execution; a model-sent decision is refused while the request is open;
  `notifications/cancelled` unblocks the call and is recorded as the client's real cap.
- **smoke_shared_approval** — 2 processes, one page, request from the non-owner shows on the
  page, decision reaches the asker, owner death → automatic port takeover.
- **smoke_multi_plan** — 2 processes, separate plans, both approvals on one page, each unblocks
  independently, sibling untouched.
- **smoke_sse** — SSE session, POST returns 202 immediately during a blocking approval,
  heartbeat over the stream, decision arrives over the stream.

## How to add a test that finds a real bug (the method that worked)

1. Pick a seam with no coverage (a failure path, a concurrency boundary, a transport).
2. Write the test that asserts the **guarantee**, not the current output (e.g. "a lost write is
   never reported as success", "the transaction is mutually exclusive across threads").
3. Run it. In this project that step found 13 defects. If it passes first try, you've documented
   a guarantee for free.
4. For concurrency, use `threading.Barrier`/`Event` to force real overlap; for failure paths,
   `mock.patch.object` the *specific* store's `_write` (never `os.replace` globally — `os` is
   shared and you'll break the other store and test the wrong thing).

## Browser verification (manual, for the approval page)

The page's HTML/JS was verified in a real browser: multi-request rendering, XSS escaping (no
execution, no console error), tab-title alert, textarea comment, all three buttons → POST →
recorded decision, `\'` onclick escaping routing to the correct id, queue removal after
decision. `TestApprovalPageSurface` guards the template against regression without a browser.
Re-run the manual browser check if you change `_PAGE` in `approval.py`.

3.0.0 was checked the same way, against a real server driven through all three requests of one
plan: a criterion written with [기준 추가] and another rewritten with [수정] relabel the button
(`승인 · 완료 기준 2건 반영`, with a choice `승인 · 변경 3건 반영`), survive a reload, and reach
the waiting agent as `done_when_by: "user"`; the re-approval of a repaired plan shows the old
wording, the failure reason and the kept `DONE` rows; the completion report shows criterion,
evidence, what the server found, the duration and a withdrawn claim; `<script>` and `<b>` in a
task and in a criterion render as text; no console error. **That check is what found
[D27](09-defects-and-lessons.md#d27)** - the suite had no test that approved a plan twice.
