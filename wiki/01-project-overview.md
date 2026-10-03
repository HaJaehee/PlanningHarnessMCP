# 01 · Project Overview

> **Prior page:** [00-why-a-harness.md](00-why-a-harness.md) frames the same problem *without*
> the air-gap constraint — an agent with a scratchpad and a memory file drifts for the same
> structural reasons. Read it first if the question is "why is any harness necessary?".

## The problem

A corporate LLM runs in an **air-gapped datacenter**. We reach it only through an
OpenAI-compatible API wired into a local **AnythingLLM** desktop app with Agent Mode. The
model is weak at multi-step reasoning and tool calling: left alone it answers immediately and
confidently, including for requests that should have been planned and approved first. There is
no SSH/OS access to the datacenter; the only lever we have is the local MCP layer.

## The goal

Give that model a **planning harness** that makes it behave like a state machine:

```
PLAN  →  HUMAN APPROVAL (HITL)  →  EXECUTE  →  REPORT
```

and a **human-in-the-loop gate** the model cannot skip. The harness is a single MCP server
exposing four tools. The design principle is that **the server owns the state and drives the
model** — the model never has to remember a plan (so it cannot hallucinate one), and every
response tells it exactly what to do next.

## Why the constraints shape everything

| Constraint | Consequence in the design |
|---|---|
| Air-gapped, no package index | **Zero third-party dependencies.** Web server = `http.server`; JSON-RPC, file locking, packaging all stdlib. |
| Weak model, unreliable tool calls | Only 4 tools; `snake_case`; no nested input objects; UPPERCASE enums; a lenient input layer that repairs near-miss calls; every response carries `next_action` + `next_action_hint`. |
| Model won't reliably self-stop | The approval gate is **enforced** server-side and, in blocking mode, physically holds the tool call open. |
| Long AnythingLLM conversations get truncated | `get_current_plan` is an always-safe recovery tool; state is durable on disk. |
| Runs on the user's own Windows PC | Windows-first (atomic `os.replace`, `msvcrt` locking, `SO_EXCLUSIVEADDRUSE`); can bundle an embeddable Python so the target needs no interpreter. |

## What the four tools do (one line each)

- **`plan_and_think`** — mandatory entry point; one thinking step per call, final call carries `task_list`.
- **`request_user_approval`** — the HITL gate: `ASK_USER` → (blocking wait) → `APPROVED`/`REJECTED`/`REVISE`.
  Since 3.1.0 the server opens the request itself (in the final `plan_and_think` call and in
  the last `DONE`), so the model calls this tool only to keep waiting on a decision that has
  not come yet.
- **`update_task_progress`** — `IN_PROGRESS` before each task, `DONE`/`FAILED` after; refuses to run unapproved.
- **`get_current_plan`** — always-safe recovery after context truncation.

Full contract: [03-tool-contract.md](03-tool-contract.md).

## What this server deliberately does NOT do

- It does **not execute** anything. It plans and tracks; AnythingLLM's other skills execute.
  (This is why the enforced gate matters — see the limitation note below.)
- No sub-tasks / dependency graphs. A flat 2–7 item list is the largest structure a weak model
  tracks reliably.
- No multi-user tenancy. One local PC, one user. (Multiple concurrent *sessions* are supported
  since 1.8.0, but they share one state directory — see [05](05-concurrency-and-sessions.md).)

## The one structural limitation to keep in mind

The gate governs **this server's own tools**. The model does real work with *other*
AnythingLLM skills, which this server cannot intercept. Two things contain that:

1. **Blocking approval** physically pauses the agent loop, so the model cannot reach any other
   tool until a human decides (see [06](06-human-in-the-loop.md)).
2. **Operational guidance**: during bring-up, disable the other agent skills so a premature
   execution has nothing to call.

If a future task needs execution itself to be gated, the design note is: execution must become
one of *our* tools so the gatekeeper controls it.

## Origin

Built to the brief in `/CLAUDE.md` (Korean). The four-phase design is recorded in `docs/`
(phase1 schema, phase2 architecture, phase3 agent system prompt, phase4 test matrix) plus a
Korean air-gapped deployment manual. This wiki supersedes those for day-to-day work.
