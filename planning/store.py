"""Persistence: one JSON file, atomic writes, an append-only audit log.

Failure philosophy: never crash. A server that fails to start gives AnythingLLM no
tools at all, and the model silently reverts to answering from memory - the exact
failure this project exists to prevent. A corrupt state file is therefore quarantined
and replaced with an empty one, not raised.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from .filelock import exclusive

from .models import (  # noqa: F401
    Plan,
    PlanStatus,
    TERMINAL_PLAN_STATUSES,
    now_iso,
    seconds_since,
)

log = logging.getLogger("planning-mcp.store")

SCHEMA_VERSION = 1

# Trailing sentence punctuation and whitespace that must not fork a plan.
_GOAL_TRIM = " \t\r\n.!?,;:。！？．…"


def goal_key(goal: str) -> str:
    """Normalized key for goal-based routing. Conservative: only trims edges."""
    return (goal or "").strip().strip(_GOAL_TRIM).strip()


_TITLE_COLLAPSE = re.compile(r"\s+")


def title_key(title: str) -> str:
    """Normalized key for carrying evidence across a re-plan.

    Same philosophy as `goal_key`: trim the edges (plus collapse internal whitespace,
    because a task title gets retyped where a goal gets echoed) and leave wording and
    case alone. Deliberately NOT fuzzy, because the two failure modes are not
    symmetric: failing to match only costs a redo, while matching two different tasks
    would let a task that must be redone keep a stale result_log and be skipped - the
    exact failure the evidence guards exist to prevent. List numbering ("1. ", "- ")
    is already stripped upstream by `leniency._clean_title`, so it never reaches here.
    """
    return _TITLE_COLLAPSE.sub(" ", (title or "").strip().strip(_GOAL_TRIM)).strip()


# Character-bigram Jaccard at or above which two goals count as rewordings of one
# another. Bigrams rather than words because Korean attaches particles to words
# ("보고서를" / "보고서"), so whole-word overlap understates how alike two goals are.
GOAL_FAMILY_SIMILARITY = 0.5


def _goal_shingles(goal: str) -> set[str]:
    text = re.sub(r"[\s" + re.escape(_GOAL_TRIM) + r"]+", "", (goal or "").lower())
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else set()


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


STATE_FILENAME = "plan_state.json"
AUDIT_FILENAME = "audit.jsonl"
CLIENT_CAPS_FILENAME = "client_caps.json"
LOCK_FILENAME = ".lock"
TXN_LOCK_FILENAME = ".txnlock"
TXN_LOCK_TIMEOUT = 20.0


class StoreWriteError(OSError):
    """Raised when a mutation could not be persisted, so callers never report success."""


class State:
    """In-memory view of the whole state file."""

    def __init__(self, active_plan_id: str | None = None, plans: dict[str, Plan] | None = None):
        self.active_plan_id = active_plan_id
        self.plans: dict[str, Plan] = plans or {}

    @property
    def active_plan(self) -> Plan | None:
        """The single plan in flight, or the most recently touched one.

        Kept for the common case of one conversation. When several sessions each have
        their own plan, callers must resolve explicitly - see `active_plans`.
        """
        actives = self.active_plans()
        if len(actives) == 1:
            return actives[0]
        if self.active_plan_id and self.active_plan_id in self.plans:
            candidate = self.plans[self.active_plan_id]
            if PlanStatus(candidate.plan_status) not in TERMINAL_PLAN_STATUSES:
                return candidate
        return actives[0] if actives else None

    def active_plans(self) -> list[Plan]:
        """Every plan still in play, most recently touched first.

        Concurrent sessions each hold their own plan; a single active-plan slot made
        them evict one another.
        """
        live = [
            p for p in self.plans.values()
            if PlanStatus(p.plan_status) not in TERMINAL_PLAN_STATUSES
        ]
        live.sort(key=lambda p: p.updated_at, reverse=True)
        return live

    def plan_for_goal(self, goal: str) -> Plan | None:
        """Route by goal: within one conversation the model repeats it every step.

        Matching is normalized so a trailing period or stray whitespace does not fork a
        second plan (a demonstrated failure). It stays conservative — case and wording
        must still match — so two genuinely different conversations are not merged.
        """
        key = goal_key(goal)
        if not key:
            return None
        for plan in self.active_plans():
            if goal_key(plan.goal) == key:
                return plan
        return None

    def recently_completed(self, goal: str, within_seconds: int) -> Plan | None:
        """A plan with this goal that a human closed as COMPLETED moments ago.

        The model that has just been told "write the final answer" and instead calls
        plan_and_think with the same goal is not starting new work - it is following a
        "plan before answering anything" rule into a second lap of the same plan (D25).
        """
        key = goal_key(goal)
        if not key or within_seconds <= 0:
            return None
        recent = [
            p for p in self.plans.values()
            if p.status is PlanStatus.COMPLETED
            and goal_key(p.goal) == key
            and p.idle_seconds() < within_seconds
        ]
        return max(recent, key=lambda p: p.updated_at) if recent else None

    def drafting_family(self, plan: Plan, within_seconds: int = 600) -> int:
        """How many recent, still-drafting plans (this one included) share this goal.

        "Share" means a rewording, not an exact match - an exact match would have routed
        to the existing plan instead of creating one. A thinking model that reconsiders
        its goal and restarts at step 1 produces exactly these: several DRAFTING plans
        minutes apart, none ever finalized, whose goals differ by a few words.
        """
        mine = _goal_shingles(plan.goal)
        if not mine:
            return 1
        family = 1
        for other in self.plans.values():
            if other.plan_id == plan.plan_id or other.status is not PlanStatus.DRAFTING:
                continue
            if seconds_since(other.created_at) >= within_seconds:
                continue
            if _similarity(mine, _goal_shingles(other.goal)) >= GOAL_FAMILY_SIMILARITY:
                family += 1
        return family

    def plan_for_former_goal(self, goal: str) -> Plan | None:
        """Route by a goal this plan has since revised away from.

        Only consulted after `plan_for_goal` misses. Right after a correction the model
        is still repeating the old wording it has been echoing all conversation; without
        this it would be told its own plan does not exist and burn a turn re-selecting
        it. Current goals always win, so this can never steal a match from another plan.
        """
        key = goal_key(goal)
        if not key:
            return None
        for plan in self.active_plans():
            if any(goal_key(former) == key for former in plan.former_goals()):
                return plan
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "active_plan_id": self.active_plan_id,
            "plans": {pid: p.to_dict() for pid, p in self.plans.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "State":
        plans = {}
        for pid, praw in (raw.get("plans") or {}).items():
            try:
                plans[pid] = Plan.from_dict(praw)
            except Exception:  # one bad plan must not take down the whole file
                log.warning("Dropping unreadable plan %s", pid)
                continue
        return cls(active_plan_id=raw.get("active_plan_id"), plans=plans)


class Store:
    def __init__(self, state_dir: Path, max_plans: int = 20):
        self.state_dir = Path(state_dir)
        self.max_plans = max_plans
        self.lock = threading.Lock()
        # Nesting depth is PER THREAD. A shared counter meant a second thread arriving
        # while the first held the transaction saw depth != 0, skipped the lock, and
        # walked straight into the critical section - which silently disabled the
        # serialization that exists to stop concurrent writers losing a plan.
        self._local = threading.local()
        # Merged into every audit record - today just the connected client (zed, goose,
        # anythingllm, ...). Reproduction on the corporate model is impossible from
        # here, so the audit log is the only place a field loop can be attributed.
        self.audit_defaults: dict[str, Any] = {}
        self._ensure_dir()
        self._write_lock_file()

    # ---- transactions --------------------------------------------------
    @contextlib.contextmanager
    def transaction(self):
        """Serialize a load-mutate-save cycle against every other writer.

        `self.lock` only covers threads inside one process. AnythingLLM (and Claude
        Code) can leave several server processes alive on the same state directory, and
        two of them doing load-mutate-save concurrently silently lose a whole plan:
        both read the same file, both allocate the same plan_id, and the second write
        overwrites the first. Measured, not theoretical. So the cycle also takes an
        OS-level lock on a sidecar file.
        """
        self._enter()
        try:
            yield
        finally:
            self._exit()

    @contextlib.contextmanager
    def paused(self):
        """Give the transaction up while waiting on a human, then take it back.

        Holding the lock across a blocking approval froze every other session for the
        whole wait (measured at 52s, and up to the full timeout with heartbeats). The
        caller MUST reload state afterwards - anything may have changed meanwhile.
        """
        depth = self._depth
        for _ in range(depth):
            self._exit()
        try:
            yield
        finally:
            for _ in range(depth):
                self._enter()

    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    def _enter(self) -> None:
        if self._depth == 0:
            self.lock.acquire()
            handle = exclusive(self.state_dir / TXN_LOCK_FILENAME, timeout=TXN_LOCK_TIMEOUT)
            handle.__enter__()
            self._local.handle = handle
        self._local.depth = self._depth + 1

    def _exit(self) -> None:
        self._local.depth = self._depth - 1
        if self._local.depth == 0:
            handle = getattr(self._local, "handle", None)
            self._local.handle = None
            if handle is not None:
                handle.__exit__(None, None, None)
            self.lock.release()

    # ---- paths ---------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.state_dir / STATE_FILENAME

    @property
    def audit_path(self) -> Path:
        return self.state_dir / AUDIT_FILENAME

    def _ensure_dir(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Cannot create state dir %s: %s", self.state_dir, exc)

    def _write_lock_file(self) -> None:
        """Advisory only. A stale or conflicting lock warns but never blocks startup."""
        path = self.state_dir / LOCK_FILENAME
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8").strip()
                log.warning(
                    "Lock file already present (pid %s). Continuing anyway - "
                    "make sure only one AnythingLLM workspace uses this state dir.",
                    existing,
                )
            path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass  # a read-only state dir must not prevent serving tools

    # ---- load / save ---------------------------------------------------
    def load(self) -> State:
        path = self.state_path
        if not path.exists():
            return State()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            self._quarantine(path, exc)
            return State()
        if not isinstance(raw, dict):
            self._quarantine(path, ValueError("state file root is not an object"))
            return State()
        try:
            return State.from_dict(raw)
        except Exception as exc:  # noqa: BLE001
            # Valid JSON of the wrong shape used to raise on every single call, leaving
            # the server permanently stuck on INTERNAL_ERROR. Quarantine it like any
            # other unusable file and carry on.
            self._quarantine(path, exc)
            return State()

    def _quarantine(self, path: Path, exc: Exception) -> None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        target = path.with_name(f"plan_state.corrupt.{stamp}.json")
        log.error("State file unreadable (%s). Quarantining to %s and starting empty.", exc, target)
        try:
            path.replace(target)
        except OSError:
            pass

    def save(self, state: State) -> None:
        self._prune(state)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        tmp = self.state_path.with_suffix(".json.tmp")
        try:
            self._ensure_dir()
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)  # atomic on NTFS
        except OSError as exc:
            # Swallowing this used to return ok:true for a mutation that never reached
            # disk - the model would go on executing a plan the server has no record of.
            # Raise instead: dispatch turns it into INTERNAL_ERROR with a resync hint.
            log.error("Failed to persist state: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StoreWriteError(f"could not persist plan state: {exc}") from exc

    def _prune(self, state: State) -> None:
        """Keep the newest `max_plans`. The active plan is never pruned."""
        if len(state.plans) <= self.max_plans:
            return
        finished = [
            p
            for p in state.plans.values()
            if p.plan_id != state.active_plan_id
            and PlanStatus(p.plan_status) in TERMINAL_PLAN_STATUSES
        ]
        finished.sort(key=lambda p: p.updated_at)
        for plan in finished[: len(state.plans) - self.max_plans]:
            state.plans.pop(plan.plan_id, None)
            log.info("Pruned old plan %s", plan.plan_id)

    # ---- audit ---------------------------------------------------------
    def audit(self, event: str, **fields: Any) -> None:
        """Append-only evidence log. Written after the state file: a duplicate line is
        harmless, a lost state write is not."""
        record = {"ts": now_iso(), "event": event, **self.audit_defaults, **fields}
        try:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Could not append to audit log: %s", exc)

    # ---- observed client limits -----------------------------------------
    @property
    def client_caps_path(self) -> Path:
        return self.state_dir / CLIENT_CAPS_FILENAME

    def observed_call_cap(self) -> float | None:
        """The shortest tool call this client has ever cancelled, in seconds.

        A client's real per-request timeout is not discoverable by asking - it is not in
        `initialize`, and the documented defaults are wrong often enough to be useless
        (see the note in config.py). What IS observable is the moment it gives up. That
        number, remembered across restarts, is the only honest input to how long the
        next wait may run.
        """
        try:
            raw = json.loads(self.client_caps_path.read_text(encoding="utf-8"))
            value = float(raw["cancelled_after_sec"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        return value if value > 0 else None

    def record_call_cap(self, seconds: float) -> None:
        """Remember a cancellation, keeping the tightest limit seen.

        Best-effort by design: losing this only costs us the adaptive shrink, and the
        configured budget is already meant to be safe on its own.
        """
        if seconds <= 0:
            return
        previous = self.observed_call_cap()
        if previous is not None and previous <= seconds:
            return
        payload = json.dumps(
            {"cancelled_after_sec": round(seconds, 2), "observed_at": now_iso()},
            ensure_ascii=False,
            indent=2,
        )
        tmp = self.client_caps_path.with_suffix(".json.tmp")
        try:
            self._ensure_dir()
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.client_caps_path)
        except OSError as exc:
            log.warning("Could not record the observed client timeout: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- ids -----------------------------------------------------------
    def next_plan_id(self, state: State) -> str:
        day = datetime.datetime.now().strftime("%Y%m%d")
        prefix = f"plan_{day}_"
        used = [pid for pid in state.plans if pid.startswith(prefix)]
        seq = len(used) + 1
        while f"{prefix}{seq:04d}" in state.plans:
            seq += 1
        return f"{prefix}{seq:04d}"
