# 11 · Status and Next Steps

## Current state (as of version 2.0.0, 2026-10-02)

- **2.0.0 - per-task alternatives**: the model proposes (`task_list` = recommendation,
  `alternatives`, `recommended_reasons`), the human picks on the approval page (radios, 권장,
  기타 → revision), the pick becomes the task, and the model never sees the unchosen options
  again. Also on the halt card and in the draft; chat-mode relay; off switch and limits.
  Plan and decisions: [`docs/plan-2.0-task-alternatives.md`](../docs/plan-2.0-task-alternatives.md).
- **Tests:** 519 unit + 6 smoke, all passing (`tests/test_alternatives.py` is new).
- **Not packaged or pushed.** `MANIFEST.txt` is still the 1.15.1 one, so `verify_install.py`
  reports NO-GO on integrity until `make_package.py` is rerun.

### 1.16.0 (previous)

- **1.16.0 - planning loops converge** ([D25](09-defects-and-lessons.md#d25),
  [D26](09-defects-and-lessons.md#d26)): thinking budget + draft submission, a finalized plan is
  not reopened by the model, calls wait on an open request, no second lap after completion, the
  circuit breaker with a halt card on the approval page, a `reasoning` model profile, per-mode
  tool descriptions, a rewritten conflict-free prompt (agents.md == README == Phase 3 Variant A),
  field telemetry + `tools/loop_report.py`, `max_active_plans` default 20. Host-side guide for
  thinking models: `docs/thinking-model-hosts.md`. Committed on `develop` in three commits
  (prompt, code, docs); not packaged or pushed yet - `MANIFEST.txt` is still the 1.15.1 one, so
  `verify_install.py` reports NO-GO on integrity until `make_package.py` is rerun.
- **Tests (then):** 443 unit + 6 smoke (`tests/test_loop_convergence.py`).

### 2.0.0 follow-up (field)

1. Watch how often models offer choices, and on which tasks: `plan_finalized` audit lines carry
   `choice_points`, `choices_applied` records what the human picked. If a weak model offers
   choices for facts it could check, lower `PLANNING_MCP_MAX_CHOICE_POINTS` or set
   `PLANNING_MCP_ALTERNATIVES=off` for that deployment.
2. If humans mostly keep the recommendation, the choice UI is costing attention for little; if
   they often pick 기타, the alternatives are not the right ones - both are visible in
   `choices_applied` and `revision_requested`.

### 1.16.0 follow-up (field)

The loop could not be reproduced here, so whether 1.16 fixes it is a field question:
1. Deploy with `PLANNING_MCP_MODEL_PROFILE=reasoning` for the thinking model, paste the new
   prompt, and apply the host settings in `docs/thinking-model-hosts.md` (sampling at the model
   card's values - **not** the README's ≤ 0.3, which is for non-thinking models - and a max-token
   cap).
2. After a few sessions run `python tools/loop_report.py`. Few calls with long `gap_sec` = the
   loop is still inside the thinking block (host settings); halts / auto-submits = across calls,
   now bounded by the server. Tune the `PLANNING_MCP_BREAKER_*` defaults from what it shows.
3. Breaker defaults (12 / 3 / 4 / 3) were set without field data and are deliberately
   conservative; a false trip costs one click on the halt card.

## Earlier state notes (1.13.2, kept for the packaging history)

- **Code:** feature-complete for the design. All four tools, blocking approval, multi-plan,
  shared approval surface, cross-process locking, failure-path hardening, leniency fuzzing,
  per-task plan review (1.10.0), auto-advance (1.11.0), goal revision (1.12.0), an explicit
  next-step message on every DONE including the last (1.12.1), and rework — a completion
  report can be sent back task by task without re-planning or losing evidence (1.13.0).
- **Tests (then):** 282 unit + 5 smoke, all passing. `verify_install.py` runs everything.
  Note: `TestApprovalPageSurface` / `TestPerTaskPageSurface` / `TestBlockingApproval` bind
  hardcoded ports 8788-8799. On a machine where something else already holds them, those
  tests fail on `srv.start()` returning False — a test-isolation weakness, not a server
  bug. Worth switching to ephemeral ports; the 1.13.0 page changes are covered
  socket-free by `TestCompletionPageTemplate` for exactly this reason.
- **Git:** on `develop`. Remote is `github.com/HaJaehee/PlanningHarnessMCP`.
  **NOT pushed.** Pushing is a user decision — the repo may be public and `docs/` contains
  deployment/security material; confirm before pushing.
- **Build artifacts (1.12.0, built 2026-07-28) — 1.12.1 and 1.13.0 have not been packaged yet:**

  | archive | size | sha256 |
  |---|---|---|
  | `dist/planning-mcp-1.12.0-20260728.zip` | 184,383 B | `8f976bc0618327bde7fa24e27ace231a62fcbd2bf92bbf617bb2213e4692f120` |
  | `dist/planning-mcp-1.12.0-20260728-with-python.zip` | 13,160,512 B | `db3f87fe3469673cfab1636ef2d0166178b23a44ccd8e62a9c6533df58942845` |

  Both were built from commit `74572a3`. The source-only archive was extracted to a scratch
  directory and `verify_install.py` returned **GO** there: 31 manifest files match, all modules
  import, 237 unit + 5 smoke tests pass. The `--with-python` variant is current again (1.11.0
  never had one).

  Every archive embeds its own `MANIFEST.txt`, which carries a build timestamp — so **the zip
  hash changes on every rebuild even when no source changed.** Re-record after building, and
  hand the hash to the corporate side out-of-band.
- **Live install:** `D:\planning-mcp` is **still on 1.11.0** — 1.12.0 has not been synced there.
  The 1.11.0 sync (2026-07-28) verified **GO** with its bundled runtime: 31 manifest files match,
  228 unit + 5 smoke tests pass. `state/` and `runtime/` were not touched by that sync; the
  previous code is at `D:\planning-mcp.backup-1.10.0-20260728-110855`.

  **The running processes stay on the old code until AnythingLLM restarts them** — Python reads a
  module once at import, so overwriting the files changes nothing already in memory. After
  syncing to 1.12.0, restart AnythingLLM and **repaste the agent prompt**: an agent still running
  the 1.11.0 prompt never sends `revised_goal`, so a user correcting the goal still leaves the
  old goal in the metadata — the server-side fix alone does not produce the behaviour.

  The same applies to 1.13.0, and more sharply: the server will reopen a single task and say
  so, but an agent running a pre-1.13.0 prompt has no rule saying "a rework is not a
  re-plan", and Phase 3c is what supplies it. **Repaste the prompt or the fix is half
  delivered.**

## Known open items

1. **`make_package --with-python` clobbers the repo's `MANIFEST.txt`.** It appends a
   `runtime/python-3.x-embed-amd64.zip` line, which is correct *inside that archive* but names a
   path the repo working tree does not have — so a later `verify_install.py` in the repo reports
   `[FAIL] missing: runtime/...`. Workaround: build the `--with-python` variant **first** and the
   source-only variant **last**, so the repo is left with a manifest that matches it. A real fix
   would write the archive manifest without touching the repo copy.
2. **Push decision** — see above.
3. **~~AnythingLLM progressToken behaviour is unconfirmed.~~ Resolved in 1.14.0 — by removing
   the dependency rather than by answering the question.** The old note said the mitigation was
   "a client that supplies a progressToken, not a server change". That was wrong on both halves:
   `resetTimeoutOnProgress` defaulted to *false* in older TypeScript SDKs and is the client's
   option to pass regardless, so a token guarantees nothing and no server can check. The wait is
   now chunked into 45 s slices that no client kills. Whether AnythingLLM sends a token is now
   merely an optimisation, and `audit.jsonl` answers it directly: a `client_cancelled_call`
   entry records the client's real limit the first time it gives up.
4. **The chunked loop's cost on a weak model is unmeasured in the field.** Each 45 s slice is
   one extra tool call, so a 5-minute deliberation costs ~7. The refusal path is safe — a model
   that goes off-script gets `ok:false` and cannot change any state (see D20) — but if the
   corporate LLM will not reliably re-call, set `PLANNING_MCP_APPROVAL_MODE=return` and the
   human sends one chat message after deciding instead.

## Where to look for the next bug

From [09](09-defects-and-lessons.md#where-the-next-bug-probably-is): thinner-covered seams are
`models.py` serialization boundaries, `config.py` env parsing, SSE session cleanup on abrupt
disconnect, and retention pruning under many active plans. The reliable method: write the test
that asserts the guarantee, watch it fail.

## Possible future work (not started, design notes only)

- **Structural edits under per-task review.** 1.10.0 deliberately excludes add/delete/reorder
  because they renumber `task_id` and break the ordering invariants in `can_start_task` /
  `unfinished_before`. Doing it properly means a stable task identity separate from the ordering
  key, which is a real change to `models.py` and every id the model holds. Only worth it if the
  field shows people reaching for 계획 전체 재작성 mainly to add one step.
- **Measure whether models actually use `task_updates`.** The audit log records
  `targeted_revision_ignored` every time a model answers a targeted request with a whole
  `task_list`. That count against `tasks_revised` is the metric; if it stays high for the
  corporate model, the fix is prompt/hint wording, not more server logic.

- **Gate execution itself.** The gate governs our own tools; the model executes with other
  AnythingLLM skills we can't intercept. To close that, execution would have to become one of
  *our* tools (e.g. `execute_step` checking `plan_status == APPROVED`). Significant redesign;
  only worth it if disabling other skills during bring-up proves insufficient.
- **Retention/pruning polish** for the multi-plan era (ensure pruning never touches an active
  plan under `max_active_plans` pressure; there is a base test to extend).

## Hard rules for whoever continues (repeat of README, because they matter)

1. Zero third-party dependencies. Standard library only.
2. Never crash a call — always the `ok/plan_status/next_action/next_action_hint` contract.
3. Never report a lost write as success.
4. The approval gate is enforced, not requested; blocking approval physically pauses the loop.
5. Approval binds to the exact plan version the human saw (fingerprint).
6. Tool schemas are generated from the enums in `models.py` — keep them in sync there.
7. **Before trusting a seam, write the test that fuzzes it.** This is how every real bug here
   was found.

## Fast orientation for a fresh AI session

1. Read [README.md](README.md) and this page.
2. Skim [09-defects-and-lessons.md](09-defects-and-lessons.md) — it's the fastest way to
   understand what's fragile and why the code looks the way it does.
3. `python -m unittest discover -s tests` to confirm a green baseline (~13 s).
4. For any change touching concurrency/approval, run the relevant smoke test too.
