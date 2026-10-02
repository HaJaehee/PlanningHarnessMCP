"""The circuit breaker's counters: is this agent going round in circles?

Every guard before 1.16 answered "is this call legal?". None asked "is this the twelfth
call in a row that changed nothing?" - and a thinking model caught in its own
self-verification loop makes calls that are each perfectly legal (D25). Telling it to
stop is one more observation for it to reconsider. So the server counts, and when the
count says "loop", it stops the plan and hands the decision to a human.

Why the counters live in process memory
---------------------------------------
D24's lesson was that a fact kept in a local attribute, in a multi-process server, is
where the next bug hides. The fact measured here is different in kind: it is *one
agent's consecutive calls*, and one agent talks to one server process (a stdio child,
or one SSE session). Two processes cannot see the same agent's sequence, so there is
nothing to share. A restart resets the counts - and a restart has already broken
whatever loop was running. What IS shared is the halt itself: it is written to the plan,
in the state directory, where the page and every peer can see it.

What does not count
-------------------
A call that actually waited on a human (a chunked approval slice), or that arrived
while a human has a request open for that plan, is never counted: the human is already
in the loop, and tripping would only replace what they are looking at.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

REASON_NO_PROGRESS = "no_progress"
REASON_REPEATED_CALL = "repeated_call"
REASON_ERROR_STREAK = "error_streak"
REASON_RESPAWN = "respawn"
REASON_THINKING_BUDGET = "thinking_budget"

# The key used for calls that resolve to no plan at all (GOAL_NOT_MATCHED ping-pong,
# for instance). Those cannot halt a plan, so they end in a plain STOP instead.
NO_PLAN = ""


def call_signature(tool: str, args: dict[str, Any]) -> str:
    """Two calls with equal signatures are the same call, byte for byte.

    Built from the arguments AFTER leniency, so "DONE" and "done" are one call - a
    model that alternates spellings is still repeating itself.
    """
    try:
        body = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        body = repr(sorted(args.items(), key=lambda kv: str(kv[0])))
    return f"{tool}:{body}"


@dataclass
class _Track:
    calls: int = 0
    last_signature: str | None = None
    repeats: int = 0
    last_error: str | None = None
    error_streak: int = 0


class LoopGuard:
    def __init__(
        self,
        calls: int = 12,
        repeat: int = 3,
        error_streak: int = 4,
        respawn: int = 3,
        enabled: bool = True,
    ):
        self.limit_calls = calls
        self.limit_repeat = repeat
        self.limit_errors = error_streak
        self.limit_respawn = respawn
        self.enabled = enabled
        self._tracks: dict[str, _Track] = {}

    # ---- observations ---------------------------------------------------
    def observe(
        self, key: str, signature: str, error_code: str | None, counted: bool = True
    ) -> tuple[str, int] | None:
        """Record one call. Returns (reason, count) when it trips, else None.

        `counted=False` records nothing at all: a waited-out slice or a call made while
        the human is looking must neither add to a streak nor break one.
        """
        if not self.enabled or not counted:
            return None
        track = self._tracks.setdefault(key, _Track())

        if signature == track.last_signature:
            track.repeats += 1
        else:
            track.last_signature = signature
            track.repeats = 1

        if error_code:
            if error_code == track.last_error:
                track.error_streak += 1
            else:
                track.last_error = error_code
                track.error_streak = 1
        else:
            track.last_error = None
            track.error_streak = 0

        track.calls += 1

        if self.limit_repeat > 0 and track.repeats >= self.limit_repeat:
            return REASON_REPEATED_CALL, track.repeats
        if self.limit_errors > 0 and error_code and track.error_streak >= self.limit_errors:
            return REASON_ERROR_STREAK, track.error_streak
        if self.limit_calls > 0 and track.calls >= self.limit_calls:
            return REASON_NO_PROGRESS, track.calls
        return None

    def milestone(self, key: str) -> None:
        """The plan moved: a list was finalized, a human decided, a task finished."""
        self._tracks.pop(key, None)

    def respawn_tripped(self, family_size: int) -> bool:
        """Has the same plan been restarted under reworded goals this many times?

        The count comes from the shared state (Store.drafting_family), not from here:
        several conversations can share one server process, and their unrelated new
        plans must never add up to a "loop".
        """
        return self.enabled and self.limit_respawn > 0 and family_size >= self.limit_respawn

    def forget(self, key: str) -> None:
        self._tracks.pop(key, None)


__all__ = [
    "LoopGuard",
    "NO_PLAN",
    "REASON_ERROR_STREAK",
    "REASON_NO_PROGRESS",
    "REASON_REPEATED_CALL",
    "REASON_RESPAWN",
    "REASON_THINKING_BUDGET",
    "call_signature",
]
