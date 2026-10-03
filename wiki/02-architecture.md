# 02 · Architecture

## Process model

```
AnythingLLM (Agent Mode)
      │  spawns child process, JSON-RPC over stdin/stdout (stdio transport)
      ▼
python server.py                         MCP server "planning-mcp"
      │
      ├── state/plan_state.json           active + archived plans (source of truth)
      ├── state/approval.json             the human-approval queue (shared across processes)
      ├── state/audit.jsonl               append-only event log
      ├── state/.txnlock                  cross-process lock for plan writes
      └── state/.approvallock             cross-process lock for approval writes
```

Primary transport is **stdio** (no port, no firewall exception). An optional **SSE** transport
on `127.0.0.1:8931` exists for when the server must outlive AnythingLLM restarts or be shared.
Both use the same handlers.

Separately, the human-approval **web page** listens on `127.0.0.1:8765` (see
[06](06-human-in-the-loop.md)). That is a second, loopback-only HTTP surface distinct from the
SSE transport.

## Module map (`planning/`)

| Module | Responsibility |
|---|---|
| `models.py` | `Plan` / `Task` / `ThinkingStep` dataclasses + **all enums** (`PlanStatus`, `TaskStatus`, `Decision`, `NextAction`, `ErrorCode`). Single source of truth. |
| `schemas.py` | The 4 tool `inputSchema` dicts, built from the enums in `models.py`. Advertised schema and runtime validator cannot drift. |
| `config.py` | `Config` dataclass + `from_env`. Timeout constants. See [data/config.json](data/config.json). |
| `leniency.py` | Input repair *before* validation: case/alias normalization, `"3"→3`, string→list splitting, numbering stripping, drop unknown keys. Must never raise. |
| `state_machine.py` | Legal transitions + `resolve_next_action` — the **only** producer of `(next_action, next_action_hint)`. |
| `responses.py` | The **single response builder**. Every handler returns through `build()`/`error()`. |
| `handlers.py` | The 4 tool implementations + plan routing + blocking-approval wait + late-decision collection. |
| `store.py` | `plan_state.json` persistence: atomic writes, corruption quarantine, retention pruning, audit log, **transactions** (thread + cross-process). |
| `approval.py` | `ApprovalStore` (shared file-backed queue) + `ApprovalServer` (the single localhost page). |
| `filelock.py` | OS-level advisory locking (`msvcrt` / `fcntl`), non-blocking with a retry loop. Shared by both stores. |
| `protocol.py` | Minimal MCP / JSON-RPC 2.0. `initialize`, `tools/list`, `tools/call`, `ping`, batch. |
| `transport.py` | `serve_stdio` (threaded) and `serve_sse`, plus notifiers for progress heartbeats. |
| `choices.py` | (2.0.0) Per-task alternatives: validate the model's alternatives against its task list (`build_options`), the page's picks against what it showed (`validate_page_choices`, strict), the model's relayed picks (`validate_model_choices`, lenient). Pure, never raises. |
| `evidence.py` | (3.0.0) The verification contract's pure pieces: validate the model's `done_when` (`clean_done_when`), score how much a `result_log` adds to it (`novelty`, `repeats_criterion`), pull paths out of prose (`extract_paths`), look for files **only inside the allowed folders** with `os.stat` on a deadline (`check_files`, `check_mentions`), validate the criteria the page posts (`validate_page_criteria`, strict). Imports nothing from the package; never raises. |
| `loopguard.py` | (1.16.0) The circuit breaker's counters: calls since the last milestone, repeated calls, error streaks. In process memory by design - see [04](04-state-machine.md#loop-convergence-1160). |
| `server.py` (root) | Entry point: arg parsing, config, builds protocol, claims the approval page, serves. |

## The request pipeline

Every tool call goes through the same seven stages. Stages 2–7 run **inside** one guarded,
transactional block in `handlers.dispatch`:

```
1. RECEIVE    raw arguments from MCP (protocol.py)
2. LENIENCY   leniency.normalize() — repair near-miss input, never raise
3. LATE-DECISION  apply any human decision recorded since the last call (blocking approval)
4. ROUTE      resolve which plan this call means (explicit id / by goal / the only active one)
5. MUTATE     handler applies the change, store.save() atomically, audit.jsonl append
6. RESPOND    responses.build() — the ONLY place a response is constructed
7. LOOP CHECK _after_call() — count the call against its plan; trip the circuit breaker (1.16)
```

Key invariants enforced by this shape:

- **`normalize()` is inside the exception guard.** If it ever raised, the failure becomes a
  graceful `ok:false / INTERNAL_ERROR`, not a raw JSON-RPC error. (This was a real fix — see
  [09](09-defects-and-lessons.md#d12).)
- **One transaction per call**, held for the whole handler, released only during a blocking
  human wait via `store.paused()`. Serialized against other threads *and* other processes.
- **`next_action` has exactly one producer** (`state_machine.resolve_next_action`). A weak
  model can never receive two different instructions for the same state.
- **Every wait goes through `_wait_on` and every end of a wait through `_settle`** (1.16.0) -
  the approval call, the halt card, and calls held because a human has a request open. A
  decision means the same thing whichever call happened to be waiting when it arrived.

## The response contract

Every response from every tool always contains:

```json
{ "ok": true, "plan_id": "...", "plan_status": "...",
  "next_action": "...", "next_action_hint": "..." }
```

plus tool-specific fields (`tasks`, `next_task`, `progress`, `display_to_user`, `approval_url`,
`active_plans`, `input_notes`, `error_code`, `message`). This uniformity is what lets a weak
model behave like a state machine. Enum values in [data/enums.json](data/enums.json).

## Failure philosophy

- **Never crash on startup.** A missing state file → start empty. An unparseable *or*
  structurally-wrong file → quarantine to `plan_state.corrupt.<ts>.json`, start empty, log to
  stderr. A server that fails to launch gives AnythingLLM no tools and the model reverts to
  answering from memory — the exact failure we exist to prevent.
- **Never report a lost write as success.** `store.save()` raises `StoreWriteError` →
  `INTERNAL_ERROR` + resync hint. The approval store propagates the same way.
- **stdout belongs to the protocol.** Under stdio, `sys.stdout` is redirected to stderr at
  startup so a stray `print()` anywhere cannot corrupt the JSON-RPC stream.
