# planning-mcp Wiki

> **You are an AI continuing work on this project.** This wiki is written for you. Read this
> page first, then jump to whatever section your task touches. Every page is self-contained.

`planning-mcp` is a lightweight **Planning & Task-Management MCP server** for AnythingLLM
Agent Mode. It exists to stop a weak, air-gapped corporate LLM from answering from memory:
it forces a `PLAN → HUMAN APPROVAL → EXECUTE → REPORT` lifecycle and physically pauses the
agent loop until a human approves. **Zero third-party dependencies** — Python 3.9+ standard
library only, because the deployment target has no package index.

Current version: **3.1.0** · 862 unit tests + 7 end-to-end smoke tests, all passing.

---

## How to use this wiki

| If your task is about… | Read |
|---|---|
| **Why a harness at all** — why memory + a scratchpad are not enough | [00-why-a-harness.md](00-why-a-harness.md) |
| Understanding what this is and why | [01-project-overview.md](01-project-overview.md) |
| How the server is built (modules, request pipeline) | [02-architecture.md](02-architecture.md) |
| The four tools and their response contract | [03-tool-contract.md](03-tool-contract.md) + [data/tool-schemas.json](data/tool-schemas.json) |
| Plan/task states and legal transitions | [04-state-machine.md](04-state-machine.md) + [data/state-machine.xml](data/state-machine.xml) |
| Concurrency: multiple sessions, locking, plan routing | [05-concurrency-and-sessions.md](05-concurrency-and-sessions.md) |
| The human-approval gate and its web page | [06-human-in-the-loop.md](06-human-in-the-loop.md) |
| How tests are organised and run | [07-testing.md](07-testing.md) |
| What changed in each version and why | [08-changelog.md](08-changelog.md) + [data/versions.json](data/versions.json) |
| **Known failure modes and their root causes** | [09-defects-and-lessons.md](09-defects-and-lessons.md) |
| **Who asks for approval, and what the human can do while a plan runs: the gate on the transition, the run card, stop and note (3.1)** | [06-human-in-the-loop.md#the-gate-on-the-transition-310](06-human-in-the-loop.md#the-gate-on-the-transition-310) + [06-human-in-the-loop.md#the-run-card-310](06-human-in-the-loop.md#the-run-card-310) |
| **What "done" means and what the server checks: `done_when`, file checks, the human writing the criterion (3.0)** | [04-state-machine.md#the-verification-contract-300](04-state-machine.md#the-verification-contract-300) + [06-human-in-the-loop.md#the-contract-on-the-page-300](06-human-in-the-loop.md#the-contract-on-the-page-300) + [../docs/plan-3.0-verification-contract.md](../docs/plan-3.0-verification-contract.md) |
| **A failed task repaired in place, finished work kept (3.0)** | [04-state-machine.md#local-repair-300](04-state-machine.md#local-repair-300) + [09-defects-and-lessons.md#d28](09-defects-and-lessons.md#d28) |
| **Per-task alternatives: the model proposes, the human picks (2.0)** | [03-tool-contract.md](03-tool-contract.md) + [06-human-in-the-loop.md#choosing-between-alternatives-200](06-human-in-the-loop.md#choosing-between-alternatives-200) + [../docs/plan-2.0-task-alternatives.md](../docs/plan-2.0-task-alternatives.md) |
| **Thinking (CoT) models looping in planning; the circuit breaker** | [04-state-machine.md#loop-convergence-1160](04-state-machine.md#loop-convergence-1160) + [../docs/thinking-model-hosts.md](../docs/thinking-model-hosts.md) |
| Packaging and air-gapped transfer | [10-deployment.md](10-deployment.md) |
| **Current status and what to do next** | [11-status-and-next-steps.md](11-status-and-next-steps.md) |

Machine-readable data lives in [`data/`](data/): tool schemas, enums, the state machine,
config, and version history as JSON/XML.

---

## The single most important thing to internalise

**Every hard problem in this project came from a place nobody was looking at.** Twenty-six
defects are catalogued so far. Through 1.8.1, thirteen of them surfaced the moment a test was
written for a previously-untested seam — threaded transport, SSE, file locking, store failure
paths, leniency edge cases, concurrent sessions — and several were "silent": the server reported
success while losing data or disarming the safety gate. The later ones needed the other three
lenses: D16 and D17 were found *in use*, D18 and D19 by **looking at the running system**,
D20–D22 by reading the other side's source and issue tracker, and D23–D24 in **live testing**,
against a green suite that asserted only half of each guarantee. D25 came from the field and
could not be reproduced at all; it was found by scripting the calls a looping model makes and
asking which ones the server simply accepted.

So the working rule for continuing this project is: **before changing behaviour, write the
test that pins the current behaviour; before trusting a path, write the test that fuzzes it.**
The bug catalog in [09-defects-and-lessons.md](09-defects-and-lessons.md) is the distilled
result and the best predictor of where the next bug is.

---

## Repository layout

```
CLAUDE.md                    original project brief (Korean)
README.md                    user-facing readme (Korean)
server.py                    entry point: transport + tool registration
planning/                    the server (see 02-architecture.md)
tests/                       unit suite + 5 smoke tests (see 07-testing.md)
tools/                       make_package / setup_runtime / verify_install
docs/                        phase1-4 design docs + air-gap deployment manual (Korean+English)
wiki/                        THIS wiki (English, for AI hand-off)
dist/                        built transfer archives (gitignored)
state/                       runtime state (gitignored)
```

Note: `docs/` predates this wiki and is partly Korean. Where `docs/` and `wiki/` overlap,
**the wiki is the current, English source of truth**; `docs/` is kept for the deployment
manual and the original phase design records.

---

## Ground rules that must not be broken

1. **Zero third-party dependencies.** Standard library only. The target machine cannot
   `pip install`. Every feature — the web server, JSON-RPC, file locking — uses stdlib.
2. **The server never crashes a call.** Every tool result carries `ok / plan_status /
   next_action / next_action_hint`. Errors return `ok:false` with a corrective `next_action`,
   never a raw exception.
3. **The approval gate is enforced, not requested.** `update_task_progress` refuses to record
   progress until a human has actually approved. Blocking approval physically holds the tool
   call open so the agent loop cannot advance.
4. **Never report success for a write that did not land.** A lost write must become
   `INTERNAL_ERROR`, not `ok:true`.
5. **Approval binds to the exact plan version the human saw** (goal + task-title fingerprint).
6. Keep the four advertised tool schemas generated from the enums in `planning/models.py`, so
   the schema and the runtime validator cannot drift.
7. **No instruction may contradict another** - in a tool description, a hint, the MCP
   `instructions`, or the agent prompt. A thinking model treats an unresolved contradiction as a
   task (D25). `agents.md` is the canonical prompt; the Phase 3 manual embeds it verbatim and
   `TestPromptHygiene` fails if they drift. The top-level README only points at the file
   (3.0.0) - it used to carry a third copy.
8. **The model never sees an option the human did not pick** (2.0). Options go to the page
   (`page_brief`), never into a model-facing response (`brief`).
