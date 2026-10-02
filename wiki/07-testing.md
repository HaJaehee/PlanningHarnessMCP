# 07 · Testing

**528 unit tests + 6 end-to-end smoke tests, all passing** (as of 2.0.0). Standard-library
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
| `TestPromptHygiene` | every profile × approval mode × auto_advance: no removed phrase, no "report APPROVED" in blocking modes, a calm reasoning profile, one tool per drafting hint, and **agents.md == README block == Phase 3 Variant A** |
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
