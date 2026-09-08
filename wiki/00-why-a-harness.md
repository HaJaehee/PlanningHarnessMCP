# 00 · Why a Harness At All

[01-project-overview.md](01-project-overview.md) explains this server from its deployment
constraint: an air-gapped corporate model too weak to plan on its own. That framing invites the
wrong conclusion — that a stronger model with a scratchpad and a memory file would not need any
of this.

It would. The failure this server exists to stop is not a **reasoning** failure, so a better
reasoner does not remove it.

## The claim

**Thinking and memory are both the model's own output. Neither can refuse an action.**

A chain of thought is a token stream the same model produced and the same model may contradict
one turn later. A memory file is one block of context competing with everything else in the
window, and it loses to recency. Both are *advisory*. The model reads its own plan, judges its
own compliance, and decides whether to continue — referee and player in one.

`06-human-in-the-loop.md` states the operational form of this:

> Returning `STOP_AND_WAIT_FOR_USER` as text does **not** stop a weak model — it reads the
> instruction as one more observation and calls the next tool.

An instruction becomes an observation. An observation can be skipped. Drift is a **control-flow**
failure, and control flow can only be enforced from outside the model.

## Four ways it actually happens

Each of these was observed in this project, not theorised. Root causes are in
[09-defects-and-lessons.md](09-defects-and-lessons.md).

**Silence is read as content** — [D16](09-defects-and-lessons.md#d16) (1.12.1). Every mid-loop
response carried the next order in `message`. On the *last* task there was nothing to advance
into, so `message` was `None` — the one response in the loop that said nothing, arriving at the
one moment when declaring victory is the cheapest continuation available. The model was not
overriding an instruction; it was filling a silence. A model does not disobey, it takes the
cheapest continuation, and an unspecified gap is always filled by its priors.

**A rule stated once is three turns gone** — [D17](09-defects-and-lessons.md#d17) (1.13.0). The
human's correction lived in exactly one field, once. By the time the model acted it was three
turns back, and the model re-ran finished work, looped on `MISSING_RESULT_LOG`, and never did the
one thing that was asked. A system prompt and a memory entry have the same shape: injected once,
then pushed down the window.

**"Done" is a claim, not a fact** — [D14](09-defects-and-lessons.md#d14) (1.9.0). The model drove
one full cycle correctly, then marked every remaining task `DONE` without doing the work and
reported the plan finished. Quality of reasoning is orthogonal: a model can think well and then
assert falsely in the same turn.

**The goal itself drifts silently** — [D13](09-defects-and-lessons.md#d13) (1.8.2). Rephrasing
its own `goal` between steps forked the plan; `"find file"` vs `"find file."` was enough.
Reproducing a string verbatim every step is exactly the discipline a weak model lacks — and with
no fixed anchor, the drift is not even observable.

[D20](09-defects-and-lessons.md#d20) (1.14.0) is the limit case: the model could call
`request_user_approval(decision='APPROVED')` and unlock execution with nobody having clicked
anything. "Ask before you act" is not a rule while the model decides whether to ask.

## Category, not degree

| | what it improves | what it cannot do |
|---|---|---|
| **Thinking** | reasoning *within* one step | bind the step that follows |
| **Memory** | recall *across* sessions | outrank the recent context it sits in |
| **Enforcement** | — | *this is the missing one* |

## What the harness supplies

The design principle from [01](01-project-overview.md): **the server owns the state and drives
the model.** The plan is on disk, not in the context, so truncation cannot lose it and the model
cannot hallucinate it. Three layers, each answering one row above:

1. **Instruction** — every response carries `next_action` and a `next_action_hint` that contains
   the literal arguments of the next call. This makes the *correct* move the cheapest
   continuation (D16), and `_rework_suffix` repeats the human's own sentence wherever a task is
   handed over, including auto-advance, so it is never three turns back (D17). See
   [04-state-machine.md](04-state-machine.md#next_action-decoder-what-the-model-does-with-each).
2. **Server enforcement** — illegal transitions are refused, not noted: `PLAN_NOT_APPROVED`,
   `TASK_NOT_STARTED`, `MISSING_RESULT_LOG`, `REWORK_NOT_DONE`. A model ignoring every hint still
   cannot make execution *real* (D14). Table: [04-state-machine.md](04-state-machine.md).
3. **Physical pause** — don't break the agent loop, make the loop wait on us. The loop blocks
   synchronously on a tool result, so a `request_user_approval(ASK_USER)` that does not return
   cannot be stepped past. No host patch required. See
   [06-human-in-the-loop.md](06-human-in-the-loop.md).

Identity is pinned the same way: `original_goal` + `goal_history` keep the anchor while `goal`
follows a correction ([D15](09-defects-and-lessons.md#d15)), and `get_current_plan` is an
always-safe recovery after truncation (D13).

## The honest boundary

From [06](06-human-in-the-loop.md#rework-1130), on a model that does no work but writes a
plausible `result_log`:

> **not detectable.** The server never sees the work.

and on what that leaves:

> The last row is the honest boundary of the whole design: the server enforces structure, the
> human checks substance. Everything above only exists to make sure the human is shown the
> right thing.

This harness does not remove hallucination. It makes drift *blockable and visible*, and puts a
person physically in front of the evidence before a plan can close. And per
[D19](09-defects-and-lessons.md#d19): every field added to help the model do the work is equally
a way to *look* like it did the work — so when you add one, close its laziest use first.
