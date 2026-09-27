"""Human approval: shared file-backed state plus a single localhost page over it.

Why the state is in a file
--------------------------
Approval used to live in process memory. Restarts leave old MCP server processes alive
on the same state directory, so each one bound its own port and served its own page. The
human had one tab open - usually on the first port - and any approval request raised by
another process appeared on a port nobody was looking at, then timed out. The gate
silently degraded to "the model asked and nothing stopped it", which is the exact failure
this whole subsystem exists to prevent.

So the request and the decision live in `state/approval.json`, and the page is just a
view over that file. Any process can publish a request; any process can read the
decision. Reads take no lock (writes are atomic rename, so a reader never sees a torn
file); only writers serialize.

Why exactly one page
--------------------
The URL has to be stable, because a human keeps that tab open. Only the base port is
ever bound. If it is already taken, we check whether the occupant is another
planning-mcp on the *same* state directory - if so it is already serving our requests
and we need no page of our own. A background thread keeps retrying the bind, so if the
owner exits another process takes the same port over and the open tab keeps working.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from .filelock import exclusive

log = logging.getLogger("planning-mcp.approval")

DECISIONS = ("APPROVED", "REJECTED", "REVISE")
APPROVAL_FILENAME = "approval.json"
APPROVAL_LOCK_FILENAME = ".approvallock"
PAGE_SEEN_FILENAME = "page_seen"
SERVER_SIGNATURE = "planning-mcp-approval"

# Which question the human is being asked. A PLAN request is a task list they may
# comment on line by line; a COMPLETION request is a report on work already done, where
# per-task rewriting is meaningless - the plan is right, the execution is in dispute.
PHASE_PLAN = "PLAN"
PHASE_COMPLETION = "COMPLETION"

# No poll for this long means no tab is open.
PAGE_IDLE_SEC = 10.0
# How often the process serving the page records that a tab polled it. The record is
# shared through the state directory because the process that opens browsers is often not
# the process that owns the page - see `page_is_being_watched`.
PAGE_SEEN_WRITE_SEC = 2.0
# How long a browser we just launched is given to load the page and poll once before we
# are willing to launch another. Covers the window where no tab is polling yet but one is
# on its way, which is the only reason to suppress an open that `PAGE_IDLE_SEC` misses.
OPEN_GRACE_SEC = 10.0
# If a launch never results in a tab polling us, the next wait doubles, up to this. A
# browser that cannot open (corporate policy, no default handler) otherwise gets launched
# again on every slice of the chunked wait, forever, with nothing to show for it.
OPEN_GRACE_MAX_SEC = 600.0

# How stale `agent_last_seen` may get before the page stops claiming an agent is still
# waiting. Comfortably above the waiter's own 10s touch interval, so an ordinary pause
# between touches never flickers the chip.
AGENT_IDLE_SEC = 25.0

# How much of the plan one REVISE is allowed to change.
SCOPE_PLAN = "PLAN"    # rewrite the whole breakdown (the original behaviour)
SCOPE_TASKS = "TASKS"  # rewrite only the tasks the human commented on
SCOPES = (SCOPE_PLAN, SCOPE_TASKS)


@dataclass
class Verdict:
    """What the human decided, and how far it reaches.

    A plain tuple grew a third and fourth member as the page learned to carry per-task
    feedback, and every silent arity change is a chance to drop the human's words on the
    floor. Named fields make an omission a visible error instead.
    """

    decision: str
    comment: str = ""
    task_comments: dict[str, str] = field(default_factory=dict)
    scope: str = SCOPE_PLAN


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------


class ApprovalStore:
    """The approval request and its decision, shared by every server process."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def path(self) -> Path:
        return self.state_dir / APPROVAL_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.state_dir / APPROVAL_LOCK_FILENAME

    @property
    def page_seen_path(self) -> Path:
        return self.state_dir / PAGE_SEEN_FILENAME

    # ---- page liveness -------------------------------------------------
    # Whether a tab is open is a property of the *machine*, not of one process, so it
    # lives in the state directory with everything else that is shared. No lock and no
    # atomic replace: a torn or lost write costs one stale liveness reading, and the
    # cost of getting it wrong is bounded in both directions (one extra tab, or one
    # slice's delay before reopening).
    def mark_page_seen(self, now: float | None = None) -> None:
        try:
            self.page_seen_path.write_text(repr(now or time.time()), encoding="ascii")
        except OSError:
            pass

    def page_last_seen(self) -> float:
        try:
            return float(self.page_seen_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return 0.0

    # ---- io -----------------------------------------------------------
    def read(self) -> dict[str, Any]:
        """Current record, or {}. Lock-free: writes are atomic."""
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _write(self, record: dict[str, Any]) -> bool:
        """Returns whether the record actually reached disk.

        Callers must propagate a False: a request that was never persisted cannot be
        answered by anyone, so reporting success would leave the gate silently disarmed.
        """
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.error("Could not persist approval state: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    # ---- operations ---------------------------------------------------
    def _requests(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return list(record.get("requests") or [])

    def publish(
        self,
        plan_id: str,
        goal: str,
        display: str,
        tasks: list[dict[str, Any]],
        fingerprint: str,
        phase: str = PHASE_PLAN,
        summary: str = "",
    ) -> str | None:
        """Queue a request for the human. Returns its id, or None if it could not be saved.

        Concurrent sessions each get their own entry: one queue slot per plan would make
        two sessions asking at once hide each other. A new request for the SAME plan
        replaces that plan's old entry (the plan was revised), never another plan's.

        Re-asking about an UNCHANGED plan is not a new question, so it reuses the entry
        that is already on screen. A chunked wait re-publishes every 45 seconds, and
        minting a fresh id each time would be destructive in three separate ways: the
        page's signature would change and wipe the card - taking any half-written
        comment with it - the alarm would re-fire, and `created_at` would reset, so the
        total wait budget could never actually run out.
        """
        request_id = uuid.uuid4().hex
        entry = {
            "id": request_id,
            "plan_id": plan_id,
            "goal": goal,
            # The model's plain-language overview. It used to reach the human only inside
            # `display`; once the page started rendering tasks as rows that blob was no
            # longer shown, and the overview vanished with it. It is its own field now.
            "summary": summary,
            "display": display,
            "tasks": tasks,
            "fingerprint": fingerprint,
            "phase": phase if phase in (PHASE_PLAN, PHASE_COMPLETION) else PHASE_PLAN,
            "created_at": time.time(),
            "created_by_pid": os.getpid(),
            "decision": None,
            "comment": "",
            "task_comments": {},
            "scope": SCOPE_PLAN,
            "decided_at": None,
        }
        with exclusive(self.lock_path) as got:
            record = self.read()
            queue = self._requests(record)
            # Checked under the same lock as the write, so two threads asking at once
            # cannot both decide the entry is missing and mint one each.
            for candidate in queue:
                if (
                    candidate.get("plan_id") == plan_id
                    and candidate.get("fingerprint") == fingerprint
                    and candidate.get("decision") is None
                ):
                    if not got:
                        log.warning("Read the approval queue without the write lock")
                    return str(candidate.get("id"))
            queue = [r for r in queue if r.get("plan_id") != plan_id]
            queue.append(entry)
            written = self._write({"requests": queue})
        if not got:
            log.warning("Published an approval request without the write lock")
        return request_id if written else None

    @staticmethod
    def _clean_task_comments(raw: Any) -> dict[str, str]:
        """{task_id: comment} with blanks dropped. Ids are kept as text (JSON keys)."""
        if not isinstance(raw, dict):
            return {}
        cleaned: dict[str, str] = {}
        for key, value in raw.items():
            try:
                task_id = str(int(str(key).strip()))
            except (TypeError, ValueError):
                continue
            text = str(value or "").strip()
            if text:
                cleaned[task_id] = text
        return cleaned

    def record_decision(
        self,
        request_id: str,
        decision: str,
        comment: str,
        task_comments: Any = None,
        scope: Any = None,
    ) -> bool:
        """Called by the page, for one specific queued request.

        `scope` comes from the page, which labels its own button with the consequence
        before the human clicks it. The server never re-derives it from whether comments
        happen to be present - that would silently overrule what the human was shown.

        Both phases may carry a task scope, but it means different things and only the
        handler knows which: on a PLAN request "these tasks only" is a rewrite of their
        wording, on a COMPLETION request it is an order to redo the work. What is still
        enforced here is that an entry with NO phase at all - written by an older process
        during a rolling restart, which had no per-task review of finished work - falls
        back to a whole-plan revision rather than being guessed at.
        """
        if decision not in DECISIONS:
            return False
        cleaned = self._clean_task_comments(task_comments)
        with exclusive(self.lock_path):
            record = self.read()
            queue = self._requests(record)
            for entry in queue:
                if entry.get("id") == request_id and entry.get("decision") is None:
                    wanted = scope if scope in SCOPES else SCOPE_PLAN
                    # Membership, not truthiness: an entry with no phase at all (written
                    # by an older process) still falls back to a whole-plan rewrite.
                    if entry.get("phase") not in (PHASE_PLAN, PHASE_COMPLETION):
                        wanted = SCOPE_PLAN
                    entry["decision"] = decision
                    entry["comment"] = comment or ""
                    entry["task_comments"] = cleaned
                    entry["scope"] = wanted
                    entry["decided_at"] = time.time()
                    # If this cannot be saved the click did nothing; say so rather than
                    # letting the page claim the decision was recorded.
                    return self._write({"requests": queue})
            return False

    def peek(self) -> list[dict[str, Any]]:
        """Everything currently queued, oldest first."""
        return sorted(self._requests(self.read()), key=lambda r: r.get("created_at", 0))

    def pending_entry(self, plan_id: str, fingerprint: str) -> dict[str, Any] | None:
        """The live, still-undecided request for exactly this plan version, if any.

        The fingerprint is part of the match on purpose: an entry for a plan that has
        since been rewritten is not a question anyone is still being asked, so it must
        not hold up the version that replaced it.
        """
        for entry in self._requests(self.read()):
            if (
                entry.get("plan_id") == plan_id
                and entry.get("fingerprint") == fingerprint
                and entry.get("decision") is None
            ):
                return entry
        return None

    def has_pending(self, plan_id: str, fingerprint: str) -> bool:
        """Is a human still looking at this exact plan version?

        While this is true the only decision that counts is one that came off the page.
        A decision arriving through the tool call instead is the model answering its own
        question - see the guard in _request_user_approval.
        """
        return self.pending_entry(plan_id, fingerprint) is not None

    def entry(self, request_id: str) -> dict[str, Any] | None:
        for candidate in self._requests(self.read()):
            if candidate.get("id") == request_id:
                return candidate
        return None

    def request_age(self, request_id: str) -> float:
        """Seconds since this request first went on screen.

        The total wait budget is measured from here rather than from the start of the
        current tool call, because a chunked wait spans many calls - and, after a
        restart, many processes. `publish` reuses the entry precisely so this number
        keeps counting across both.
        """
        entry = self.entry(request_id)
        if entry is None:
            return 0.0
        try:
            return max(0.0, time.time() - float(entry.get("created_at") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def touch_agent(self, request_id: str) -> None:
        """Mark the agent as still waiting on this request.

        The page shows this as a liveness chip. It is deliberately not a countdown: the
        request outlives any single tool call, so a deadline would be a lie, and the one
        thing the human actually needs to know is whether deciding right now resumes the
        conversation by itself or needs a nudge in chat.
        """
        with exclusive(self.lock_path):
            queue = self._requests(self.read())
            for candidate in queue:
                if candidate.get("id") == request_id:
                    candidate["agent_last_seen"] = time.time()
                    self._write({"requests": queue})
                    return

    @staticmethod
    def _verdict(entry: dict[str, Any]) -> Verdict:
        """Read a decided entry. Old entries have no scope/task_comments: they predate
        per-task review and mean exactly what they always meant - rewrite the plan."""
        scope = entry.get("scope")
        return Verdict(
            decision=entry["decision"],
            comment=entry.get("comment", "") or "",
            task_comments=ApprovalStore._clean_task_comments(entry.get("task_comments")),
            scope=scope if scope in SCOPES else SCOPE_PLAN,
        )

    def _take(self, match) -> Verdict | None:
        with exclusive(self.lock_path):
            queue = self._requests(self.read())
            for entry in queue:
                verdict = match(entry)
                if verdict is None:
                    continue
                queue = [r for r in queue if r.get("id") != entry.get("id")]
                self._write({"requests": queue})
                return verdict if isinstance(verdict, Verdict) else None
            return None

    def claim(self, request_id: str) -> Verdict | None:
        """Consume the decision for a specific request. Used by the blocking waiter."""

        def match(entry):
            if entry.get("id") != request_id or not entry.get("decision"):
                return None
            return self._verdict(entry)

        return self._take(match)

    def claim_for_plan(self, plan_id: str, fingerprint: str) -> Verdict | None:
        """Consume a decision made after the tool call already returned.

        Only honoured for the exact plan version that was on screen - the human agreed
        to what they saw, not to whatever the plan became afterwards.
        """

        def match(entry):
            if entry.get("plan_id") != plan_id or not entry.get("decision"):
                return None
            if entry.get("fingerprint") != fingerprint:
                log.warning("Discarding a decision for a plan that has since changed (%s)", plan_id)
                return "drop"
            return self._verdict(entry)

        return self._take(match)

    def drop_for_plan(self, plan_id: str) -> None:
        """Withdraw a plan's request once the plan is settled.

        A cancelled or finished plan that keeps asking for approval is worse than
        useless: the human sees a live-looking request whose buttons do nothing, because
        the decision would be discarded on the fingerprint check anyway.
        """
        with exclusive(self.lock_path):
            queue = self._requests(self.read())
            remaining = [r for r in queue if r.get("plan_id") != plan_id]
            if len(remaining) != len(queue):
                self._write({"requests": remaining})

    def clear(self) -> None:
        with exclusive(self.lock_path):
            self._write({"requests": []})


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>planning-mcp 승인</title>
<style>
:root{color-scheme:light dark}
body{font-family:system-ui,"Segoe UI","Malgun Gothic",sans-serif;margin:0;padding:2rem 1rem;
     background:#f6f7f9;color:#16181d}
@media(prefers-color-scheme:dark){body{background:#15171c;color:#e8eaed}}
.card{max-width:720px;margin:0 auto;background:#fff;border-radius:12px;padding:1.75rem;
      box-shadow:0 1px 3px rgba(0,0,0,.12)}
@media(prefers-color-scheme:dark){.card{background:#1e2126;box-shadow:none;border:1px solid #2c3038}}
h1{font-size:1.05rem;margin:0 0 .35rem;letter-spacing:.02em;text-transform:uppercase;opacity:.6}
.goal{font-size:1.15rem;font-weight:600;margin:0 0 1rem;line-height:1.4}
.goal .lbl{font-size:.78rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;
           opacity:.5;margin-right:.5rem;vertical-align:.08em}
.summary{background:#f2f3f5;border-radius:8px;padding:.85rem 1rem;margin:0 0 1.25rem;
         font-size:.94rem;line-height:1.65;white-space:pre-wrap;word-break:break-word}
@media(prefers-color-scheme:dark){.summary{background:#15171c}}
.summary .lbl{display:block;font-size:.72rem;font-weight:600;text-transform:uppercase;
              letter-spacing:.04em;opacity:.5;margin-bottom:.35rem}
.tasklabel{font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;
           opacity:.5;margin:0 0 .2rem}
pre{white-space:pre-wrap;word-break:break-word;background:#f2f3f5;border-radius:8px;
    padding:1rem;font-size:.92rem;line-height:1.6;margin:0 0 1.25rem;
    font-family:ui-monospace,Consolas,monospace}
@media(prefers-color-scheme:dark){pre{background:#15171c}}
textarea{width:100%;box-sizing:border-box;min-height:64px;border-radius:8px;padding:.6rem;
         border:1px solid #ccd0d5;font:inherit;font-size:.92rem;margin-bottom:1rem;
         background:transparent;color:inherit}
@media(prefers-color-scheme:dark){textarea{border-color:#3a3f47}}
.row{display:flex;gap:.6rem;flex-wrap:wrap}
button{flex:1 1 auto;min-width:140px;padding:.85rem 1rem;border:0;border-radius:8px;
       font:inherit;font-weight:600;cursor:pointer;font-size:.95rem}
.ok{background:#1a7f37;color:#fff}.no{background:#b42318;color:#fff}.rev{background:#8a5a00;color:#fff}
button:disabled{opacity:.5;cursor:default}
.idle{text-align:center;opacity:.6;padding:2.5rem 0;font-size:.95rem}
.done{text-align:center;padding:2rem 0;font-size:1.05rem;font-weight:600}
.hint{margin-top:1rem;font-size:.82rem;opacity:.55;line-height:1.5}
.tasks{margin:0 0 1.25rem}
.task{padding:.7rem 0;border-top:1px solid #e6e8eb}
@media(prefers-color-scheme:dark){.task{border-top-color:#2c3038}}
.task:first-child{border-top:0}
.tt{display:flex;gap:.55rem;align-items:baseline;line-height:1.45;font-size:.95rem}
.tn{opacity:.5;font-variant-numeric:tabular-nums;min-width:1.4em}
.badge{font-size:.72rem;padding:.1rem .4rem;border-radius:4px;background:#e6e8eb;opacity:.8}
@media(prefers-color-scheme:dark){.badge{background:#2c3038}}
.was{font-size:.82rem;opacity:.55;margin:.25rem 0 0 1.95em;text-decoration:line-through}
.note{font-size:.82rem;margin:.25rem 0 0 1.95em;color:#8a5a00}
@media(prefers-color-scheme:dark){.note{color:#d9a441}}
/* The agent's evidence for a finished task. Shown only on a completion report - it is
   the thing the human is actually being asked to judge. */
.ev{font-size:.86rem;opacity:.75;margin:.25rem 0 0 1.95em;line-height:1.55;
    white-space:pre-wrap;word-break:break-word}
.ev.none{opacity:.45;font-style:italic}
/* Superseded output on a row the human sent back. Muted rather than struck through -
   a struck-out sentence is unreadable, and this one has to be compared, not dismissed. */
.ev.old{opacity:.45}
/* Per-task comment boxes are collapsed by default. Nine open textareas turn a task list
   into a form; the plan itself is what the human came here to read. */
textarea.tc{display:none;min-height:2.4rem;margin:.4rem 0 0 1.95em;width:calc(100% - 1.95em);
            font-size:.86rem;padding:.4rem .55rem}
.task.open textarea.tc{display:block}
.tcbtn{flex:0 0 auto;min-width:0;margin-left:auto;align-self:center;padding:.15rem .5rem;
       font-size:.76rem;font-weight:500;border-radius:6px;background:#e6e8eb;color:inherit;
       opacity:.65}
.tcbtn:hover{opacity:1}
.task.open .tcbtn{opacity:1}
/* A comment written and then collapsed is still submitted, so the row has to keep saying
   so - otherwise the human sends a targeted revision they can no longer see. */
.task.filled .tcbtn{background:#8a5a00;color:#fff;opacity:1}
@media(prefers-color-scheme:dark){.tcbtn{background:#2c3038}}
.scope{display:flex;align-items:center;gap:.45rem;margin:-.5rem 0 1rem;font-size:.86rem;
       opacity:.75}
.scope input{margin:0}
/* Whether an agent is still holding a call open for this request. Not a countdown:
   the request outlives any one tool call, so the honest thing to show is not how long
   is left but whether deciding right now resumes the conversation by itself. */
.chip{display:inline-block;font-size:.78rem;padding:.2rem .55rem;border-radius:999px;
      margin:0 0 .9rem;line-height:1.5}
.chip.live{background:#e6f4ea;color:#1a7f37}
.chip.idle{background:#fdf0e3;color:#8a5a00}
@media(prefers-color-scheme:dark){
  .chip.live{background:#16281c;color:#5dbb77}
  .chip.idle{background:#2a2115;color:#d9a441}
}
</style></head><body><div class="card" id="root">
<div class="idle">현재 대기 중인 승인 요청이 없습니다.<br>
<span style="font-size:.85rem">에이전트가 계획을 제출하면 이곳에 표시됩니다.</span></div></div>
<script>
let seen='',busy=false,flash=null,pendingCount=0;
const IDLE_TITLE='planning-mcp 승인';
// A popup can be blocked, land on another monitor, or open behind other windows.
// So the page makes itself noticeable instead: the tab title flashes and a short tone
// plays. Leaving this tab open is the reliable way to catch approval requests.
function alertOn(){
  if(flash)return;
  let on=false;
  flash=setInterval(()=>{on=!on;document.title=on?
    '\\u26A0 승인 대기 '+pendingCount+'건':IDLE_TITLE;},700);
  try{
    const C=window.AudioContext||window.webkitAudioContext;if(!C)return;
    const ctx=new C();const o=ctx.createOscillator();const g=ctx.createGain();
    o.connect(g);g.connect(ctx.destination);o.frequency.value=880;g.gain.value=0.08;
    o.start();o.stop(ctx.currentTime+0.18);
    setTimeout(()=>{try{ctx.close();}catch(e){}},400);
  }catch(e){}
}
function alertOff(){
  if(flash){clearInterval(flash);flash=null;}
  document.title=IDLE_TITLE;
}
// ---- drafts --------------------------------------------------------------
// What the human has typed but not yet submitted lives in localStorage, not in the DOM.
// The page rebuilds itself whenever the queue changes - a second session asking for
// approval is enough - and rebuilding used to throw away a half-written comment with no
// trace. Keeping drafts outside the DOM means they survive that, plus a reload, plus
// closing the tab. localStorage is shared per origin, so the `storage` event below also
// keeps two open tabs showing the same text: whichever one the human finally submits
// from, it carries what they wrote.
const DRAFT='planning-mcp:draft:';
function dkey(req,tid){return DRAFT+req+':'+tid;}
function dget(req,tid){
  try{const v=localStorage.getItem(dkey(req,tid));return v===null?'':v;}catch(e){return '';}
}
function dset(req,tid,value){
  // Empty strings are stored, never removed: "I deleted what I wrote" is a state that
  // has to survive a re-render too, or the old text comes back.
  try{localStorage.setItem(dkey(req,tid),value);}catch(e){}
}
function dkeys(){
  const out=[];
  try{
    for(let i=0;i<localStorage.length;i++){
      const k=localStorage.key(i);
      if(k&&k.indexOf(DRAFT)===0)out.push(k);
    }
  }catch(e){}
  return out;
}
function dclear(req){
  try{dkeys().forEach(k=>{if(k.indexOf(DRAFT+req+':')===0)localStorage.removeItem(k);});}catch(e){}
}
// Drafts for requests that have left the queue would otherwise sit there forever and
// reappear if an id were ever reused.
function dprune(ids){
  try{
    dkeys().forEach(k=>{
      const rest=k.slice(DRAFT.length);
      const cut=rest.indexOf(':');
      if(cut>0&&ids.indexOf(rest.slice(0,cut))<0)localStorage.removeItem(k);
    });
  }catch(e){}
}
function taskBoxes(req){
  return document.querySelectorAll('textarea.tc[data-req="'+req+'"]');
}
function restore(list){
  list.forEach(d=>{
    if(d.decided)return;
    const all=document.getElementById('c-'+d.id);
    if(all)all.value=dget(d.id,'_all');
    const whole=document.getElementById('all-'+d.id);
    if(whole)whole.checked=dget(d.id,'_whole')==='1';
    taskBoxes(d.id).forEach(b=>{
      b.value=dget(d.id,b.getAttribute('data-tid'));
      // A restored comment has to be visible, or the human sends a targeted revision
      // they cannot see. relabel() marks the row; opening it lets them edit it.
      if(b.value.trim()){
        const task=b.closest('.task');
        if(task){
          task.classList.add('open');
          const btn=task.querySelector('.tcbtn');
          if(btn)btn.setAttribute('aria-expanded','true');
        }
      }
    });
    relabel(d.id);
  });
}
// One delegated listener on a container that outlives every re-render, so no inline
// handler has to carry an escaped id.
function onInput(ev){
  const el=ev.target;
  if(!el)return;
  if(el.type==='checkbox'&&el.id.indexOf('all-')===0){
    const req=el.id.slice(4);
    dset(req,'_whole',el.checked?'1':'');
    relabel(req);
    return;
  }
  if(el.tagName!=='TEXTAREA')return;
  const req=el.getAttribute('data-req');
  if(req){dset(req,el.getAttribute('data-tid'),el.value);relabel(req);return;}
  if(el.id.indexOf('c-')===0)dset(el.id.slice(2),'_all',el.value);
}
// Another tab wrote a draft. Mirror it here so both windows show the same thing - the
// property the old full-rebuild had by accident, kept on purpose.
function onStorage(ev){
  if(!ev.key||ev.key.indexOf(DRAFT)!==0)return;
  const rest=ev.key.slice(DRAFT.length);
  const cut=rest.indexOf(':');
  if(cut<0)return;
  const req=rest.slice(0,cut),tid=rest.slice(cut+1),value=ev.newValue||'';
  if(tid==='_whole'){
    const box=document.getElementById('all-'+req);
    if(box)box.checked=value==='1';
    relabel(req);
    return;
  }
  const el=tid==='_all'
    ? document.getElementById('c-'+req)
    : document.querySelector('textarea[data-req="'+req+'"][data-tid="'+tid+'"]');
  if(!el||el===document.activeElement)return;
  el.value=value;
  if(tid!=='_all'&&value.trim()){
    const task=el.closest('.task');
    if(task)task.classList.add('open');
  }
  relabel(req);
}

async function poll(){
  if(busy)return;
  try{
    const r=await fetch('/api/pending');const d=await r.json();
    const list=d.requests||[];
    dprune(list.map(x=>x.id));
    const sig=list.map(x=>x.id+':'+(x.decided||'')+':'+(x.agent_waiting?1:0)).join('|');
    if(sig===seen)return;
    seen=sig;
    const undecided=list.filter(x=>!x.decided);
    pendingCount=undecided.length;
    render(list);
    restore(list);
    if(undecided.length)alertOn();else alertOff();
  }catch(e){}
}
// Quotes are escaped too so the same helper is safe inside an attribute value.
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
  '"':'&quot;',"'":'&#39;'}[c]));}

// ---- per-task review -----------------------------------------------------
// Both phases can be reviewed task by task, but the request means different things:
// on a plan it rewrites the task's wording, on a completion report it orders that one
// task to be done again. The phase of each queued request is kept here so the buttons
// can say which of the two the human is about to ask for.
const PHASE={};
function comments(id){
  const out={};
  document.querySelectorAll('textarea[data-req="'+id+'"]').forEach(b=>{
    const v=b.value.trim();
    if(v)out[b.getAttribute('data-tid')]=v;
  });
  return out;
}
// Reveal one task's comment box. The textarea stays in the DOM either way, so a comment
// written and then collapsed is still collected by comments() - the row keeps its 'filled'
// marker precisely so that cannot happen silently.
function toggleComment(btn){
  const task=btn.closest('.task');
  if(!task)return;
  const open=task.classList.toggle('open');
  btn.setAttribute('aria-expanded',open?'true':'false');
  if(open){const ta=task.querySelector('textarea.tc');if(ta)ta.focus();}
}
function wholePlan(id){
  const box=document.getElementById('all-'+id);
  return !!(box&&box.checked);
}
function scopeOf(id){
  if(wholePlan(id))return 'PLAN';
  return Object.keys(comments(id)).length?'TASKS':'PLAN';
}
// The button says what it will do BEFORE it is clicked, so choosing the scope is an
// explicit act by the human rather than something the server infers afterwards. One
// builder for both the first render and every relabel, so the two can never drift.
function revLabel(phase,ids,whole){
  const done=phase==='COMPLETION';
  if(whole||!ids.length)
    return done?'수정 요청 · 계획 전체 다시 세우기':'수정 요청 · 계획 전체 재작성';
  const verb=done?'다시 작업 요청':'수정 요청';
  return ids.length===1?verb+' · '+ids[0]+'번만':verb+' · '+ids.length+'개 태스크만';
}
function relabel(id){
  // Mark the rows that carry a comment, so a collapsed one is still visible as such.
  document.querySelectorAll('textarea[data-req="'+id+'"]').forEach(b=>{
    const task=b.closest('.task');
    if(task)task.classList.toggle('filled',!!b.value.trim());
  });
  const btn=document.getElementById('rev-'+id);
  if(!btn)return;
  btn.textContent=revLabel(PHASE[id],Object.keys(comments(id)),wholePlan(id));
}
// The header a human needs before they can judge a task list at all: what is being
// asked, what the goal is, and the model's own overview of how it intends to get there.
// All three used to arrive inside the pre-rendered `display` blob; rendering tasks as
// rows dropped that blob, so they are composed here from their own fields instead.
function header(d){
  const title=d.phase==='COMPLETION'?'완료 확인':'계획 승인 요청';
  let h='<h1>'+title+' · '+esc(d.plan_id)+'</h1>'+
    '<p class="goal"><span class="lbl">목표</span>'+esc(d.goal)+'</p>';
  if(d.summary)h+='<div class="summary"><span class="lbl">개요</span>'+esc(d.summary)+'</div>';
  return h;
}
function taskRows(d){
  // On a completion report every task is DONE, so a DONE badge on every row is noise -
  // the evidence line below it already says so, and that is what needs the attention.
  const done=d.phase==='COMPLETION';
  return '<p class="tasklabel">태스크 '+(d.tasks||[]).length+'개</p>'+
    '<div class="tasks">'+(d.tasks||[]).map(t=>{
    const badge=t.status&&t.status!=='PENDING'&&!(done&&t.status==='DONE');
    let row='<div class="task"><div class="tt"><span class="tn">'+esc(String(t.task_id))+
      '.</span><span>'+esc(t.title)+'</span>'+
      (badge?'<span class="badge">'+esc(t.status)+'</span>':'')+
      '<button class="tcbtn" type="button" aria-expanded="false" '+
      'onclick="toggleComment(this)">'+(done?'다시 작업':'의견')+'</button></div>';
    if(t.previous_title)row+='<div class="was">'+esc(t.previous_title)+'</div>';
    if(t.revision_note)row+='<div class="note">\\u21BB 요청하신 내용: '+
      esc(t.revision_note)+'</div>';
    // What the task used to produce, for a row the human already sent back once. The
    // request alone shows what was asked; only this shows whether it was answered.
    if(done&&t.previous_result_log)
      row+='<div class="ev old">이전 결과: '+esc(t.previous_result_log)+'</div>';
    // The claim the human is judging. Without it a completion report is a task list
    // that says nothing about whether the work happened.
    if(done){
      const ev=(t.result_log||'').trim();
      row+=ev?'<div class="ev">\\u2192 '+esc(ev)+'</div>'
             :'<div class="ev none">(증거 기록 없음)</div>';
    }
    row+='<textarea class="tc" data-req="'+esc(d.id)+'" data-tid="'+esc(String(t.task_id))+
      '" placeholder="'+(done?'해당 태스크의 재작업 요청 사항을 입력해 주십시오'
                             :'해당 태스크에 대한 의견을 입력해 주십시오 (선택 사항)')+'"></textarea>';
    return row+'</div>';
  }).join('')+'</div>';
}
function render(list){
  const root=document.getElementById('root');
  // Matches the static placeholder above, so the page does not flicker between two
  // different wordings when the first poll lands.
  if(!list.length){root.innerHTML='<div class="idle">현재 대기 중인 승인 요청이 없습니다.<br>'+
    '<span style="font-size:.85rem">에이전트가 계획을 제출하면 이곳에 표시됩니다.</span></div>';return;}
  // 여러 세션이 동시에 승인을 기다릴 수 있으므로 큐 전체를 보여준다.
  root.innerHTML=list.map(d=>{
    if(d.decided){
      const label={APPROVED:'승인되었습니다',REJECTED:'거절되었습니다',REVISE:'수정 요청되었습니다'}[d.decided]||d.decided;
      return '<div class="done">'+esc(d.plan_id)+' — '+label+
        '<br><span style="font-weight:400;opacity:.6;font-size:.9rem">'+
        '에이전트가 이 결정을 반영합니다.</span></div>';
    }
    // An entry with no phase was published by an older server process on this same
    // state directory. It has no per-task review, so fall back to the original form.
    PHASE[d.id]=d.phase;
    const perTask=(d.phase==='PLAN'||d.phase==='COMPLETION')&&d.tasks&&d.tasks.length;
    // The fallback path keeps the original layout on purpose: `display` already opens
    // with its own title, 목표 and summary, so composing a header above it would repeat
    // all three.
    let html=perTask?header(d)+taskRows(d)
      :'<h1>'+(d.phase==='COMPLETION'?'완료 확인':'승인 요청')+' · '+esc(d.plan_id)+
       '</h1><p class="goal">'+esc(d.goal)+'</p><pre>'+esc(d.display)+'</pre>';
    html+=d.agent_waiting
      ? '<div class="chip live">에이전트가 대기 중입니다 · 결정하시면 작업이 바로 이어집니다</div>'
      : '<div class="chip idle">에이전트가 대기를 멈췄습니다 · 지금 결정하셔도 반영되며, '+
        '채팅창에 메시지를 입력하시면 진행이 재개됩니다</div>';
    html+='<textarea id="c-'+esc(d.id)+
      '" placeholder="전체 의견을 입력해 주십시오 (거절 사유도 여기에 입력하실 수 있습니다)"></textarea>';
    if(perTask)html+='<label class="scope"><input type="checkbox" id="all-'+esc(d.id)+
      '"> '+
      (d.phase==='COMPLETION'?'계획 자체를 다시 세우기 (태스크 추가·삭제·순서 변경 시 선택)'
                             :'계획 전체를 다시 세우기 (태스크 추가·삭제·순서 변경 시 선택)')+
      '</label>';
    return html+'<div class="row">'+
      '<button class="ok" onclick="decide(\\''+esc(d.id)+'\\',\\'APPROVED\\')">승인</button>'+
      '<button class="rev" id="rev-'+esc(d.id)+'" onclick="decide(\\''+esc(d.id)+
      '\\',\\'REVISE\\')">'+(perTask?revLabel(d.phase,[],false):'수정 요청')+'</button>'+
      '<button class="no" onclick="decide(\\''+esc(d.id)+
      '\\',\\'REJECTED\\')">거절</button></div>';
  }).join('<hr style="border:0;border-top:1px solid #ccd0d5;margin:1.75rem 0">')+
    '<p class="hint">태스크의 [의견] 또는 [다시 작업] 버튼을 누르면 해당 태스크에만 요청을 남기실 수 '+
    '있습니다. 완료 보고 단계에서는 지정하신 태스크만 다시 실행되며, 나머지 태스크의 결과는 '+
    '그대로 유지됩니다.<br>'+
    '결정하시기 전까지 해당 에이전트는 후속 작업을 진행하지 못합니다. '+
    '요청은 응답하실 때까지 사라지지 않으니 천천히 검토해 주시기 바랍니다.</p>';
}
async function decide(id,dec){
  if(busy)return;busy=true;alertOff();
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  const box=document.getElementById('c-'+id);
  const c=box?box.value:'';
  // Task comments travel with every decision, not just a targeted one: even when the
  // whole plan is being rewritten, what the human said about task 3 is useful context.
  try{
    await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id,decision:dec,comment:c,
        task_comments:comments(id),scope:scopeOf(id)})});
    // Only once the decision is away. Clearing first would lose the text if the POST
    // failed, and this is the one copy of it.
    dclear(id);
  }catch(e){}
  busy=false;seen='';poll();
}
// Bound to #root rather than to each textarea: #root outlives every re-render, so the
// listeners survive a rebuild that replaces all of its children.
document.getElementById('root').addEventListener('input',onInput);
document.getElementById('root').addEventListener('change',onInput);
window.addEventListener('storage',onStorage);
poll();setInterval(poll,1500);
</script></body></html>"""


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """Refuses to share its port.

    http.server sets allow_reuse_address, and on Windows SO_REUSEADDR lets a second
    process bind a port another process is already listening on. That would break the
    single-page guarantee: two instances would both think they own the URL.
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        exclusive_opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_opt is not None:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive_opt, 1)
            except OSError:
                pass
        super().server_bind()


class ApprovalServer:
    """Serves the shared approval state on one stable localhost URL."""

    def __init__(
        self,
        store: ApprovalStore,
        port: int = 8765,
        open_browser: bool = True,
        takeover_interval: float = 5.0,
    ):
        self.store = store
        self.base_port = port
        self.port = port
        self.open_browser = open_browser
        self.takeover_interval = takeover_interval
        self._httpd: _ExclusiveHTTPServer | None = None
        self._lock = threading.Lock()
        # When we last launched a browser - NOT whether we ever did. A permanent latch
        # here meant closing the tab closed the door: the human never saw another
        # request. See `_open_browser`.
        self._last_open_at = 0.0
        # Consecutive launches after which no tab ever polled. Backs the grace off so a
        # browser that cannot open is not relaunched on every slice forever.
        self._opens_without_contact = 0
        self._stop = threading.Event()
        # When the page last asked for work. A live tab makes opening another one pure
        # harm: the human ends up with several windows holding different half-written
        # comments, and whichever one they submit from silently discards the rest.
        self._last_poll_at = 0.0
        self._last_poll_written_at = 0.0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def owns_page(self) -> bool:
        return self._httpd is not None

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> bool:
        """Take the page if it is free, otherwise confirm a peer already serves it."""
        if self._try_bind():
            # Same liveness question as `_surface`: a tab left open across our restart is
            # still watching, and a second window would only split the human's attention.
            if self.open_browser and not self.page_is_being_watched():
                self._open_browser()
            self._start_takeover_watch()
            return True

        peer = self._probe_peer(self.base_port)
        if peer == "ours":
            log.info(
                "Approval page already served by another planning-mcp instance on %s; "
                "publishing to the shared state it reads",
                self.url,
            )
            self._start_takeover_watch()
            return True

        log.error(
            "Port %s is held by something that is not a planning-mcp approval page for "
            "this state directory. The approval page is unavailable.",
            self.base_port,
        )
        return False

    def _try_bind(self) -> bool:
        with self._lock:
            if self._httpd is not None:
                return True
            try:
                httpd = _ExclusiveHTTPServer(("127.0.0.1", self.base_port), self._make_handler())
            except OSError:
                return False
            self._httpd = httpd
            self.port = self.base_port
            threading.Thread(target=httpd.serve_forever, name="approval-ui", daemon=True).start()
            log.info("Approval UI listening on %s", self.url)
            return True

    def _probe_peer(self, port: int) -> str:
        """Is the occupant of `port` a planning-mcp page for our state directory?"""
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                info = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - any failure means "not ours"
            return "unknown"
        if info.get("server") != SERVER_SIGNATURE:
            return "foreign"
        try:
            same = Path(info.get("state_dir", "")).resolve() == self.store.state_dir.resolve()
        except OSError:
            same = False
        return "ours" if same else "other-state-dir"

    def _start_takeover_watch(self) -> None:
        """Keep trying to own the page so a dead owner is replaced automatically."""
        if self._httpd is not None:
            return  # we already own it
        def watch() -> None:
            while not self._stop.wait(self.takeover_interval):
                if self._try_bind():
                    log.warning("Took over the approval page on %s", self.url)
                    return
        threading.Thread(target=watch, name="approval-takeover", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    # ---- request flow --------------------------------------------------
    def open_request(
        self, plan_id: str, goal: str, display: str, tasks: list[dict[str, Any]],
        fingerprint: str = "", phase: str = PHASE_PLAN, summary: str = "",
    ) -> str | None:
        request_id = self.store.publish(
            plan_id, goal, display, tasks, fingerprint, phase, summary
        )
        self._surface()
        return request_id

    def claim(self, request_id: str) -> Verdict | None:
        return self.store.claim(request_id)

    def has_pending(self, plan_id: str, fingerprint: str) -> bool:
        return self.store.has_pending(plan_id, fingerprint)

    def request_age(self, request_id: str) -> float:
        return self.store.request_age(request_id)

    def touch_agent(self, request_id: str) -> None:
        self.store.touch_agent(request_id)

    def take_decision(self, plan_id: str, fingerprint: str) -> Verdict | None:
        return self.store.claim_for_plan(plan_id, fingerprint)

    def drop_for_plan(self, plan_id: str) -> None:
        self.store.drop_for_plan(plan_id)

    def note_poll(self) -> None:
        """A tab just asked us for work. Publish that where peers can see it.

        Throttled: the page polls every 1.5s and only `PAGE_IDLE_SEC` resolution matters,
        so there is no reason to touch the disk on every request.
        """
        now = time.time()
        self._last_poll_at = now
        if (now - self._last_poll_written_at) >= PAGE_SEEN_WRITE_SEC:
            self._last_poll_written_at = now
            self.store.mark_page_seen(now)

    def page_is_being_watched(self) -> bool:
        """Has a tab polled *anyone* recently enough to count as open?

        The page polls every 1.5s, so a gap of PAGE_IDLE_SEC means every tab is gone (or
        was never opened).

        The answer has to come from the state directory, not from this process. Only one
        process owns the page; every other planning-mcp instance on the same state dir is
        a peer whose own `_last_poll_at` stays at zero forever, because the polls go to
        the owner. The process that decides to open a browser is whichever one is holding
        an approval request - routinely a peer. Reading only our own counter, a peer
        concludes "no tab is open" every single time, which is how a 45s chunked wait
        turned into a new window every 45 seconds (D24). Our in-process value is still
        consulted so a fresh poll counts immediately, before it has been written out.
        """
        seen = max(self._last_poll_at, self.store.page_last_seen())
        return (time.time() - seen) < PAGE_IDLE_SEC

    def _surface(self) -> None:
        log.warning("HUMAN APPROVAL NEEDED -> %s", self.url)
        if not self.open_browser:
            return
        if self.page_is_being_watched():
            log.info("A tab is already watching %s; not opening another", self.url)
            return
        self._open_browser()

    def _open_browser(self) -> None:
        """Best-effort only. Corporate policy, a missing default browser, or a second
        monitor can all defeat this, which is why the page also polls.

        Opening is suppressed by *liveness*, never permanently. The predecessor of this
        function latched a `_opened_once` flag the first time it succeeded and refused
        forever after, so closing the approval tab meant no request ever surfaced a
        window again - the human waited on a page that was never going to appear. The
        duplicate-tab problem that latch was fighting is already handled by
        `page_is_being_watched`; all this needs to add is a grace period so the chunked
        wait's repeat calls do not launch a second browser while the first is still
        starting up.

        That grace doubles each time a launch fails to produce a tab, because "opened
        successfully" is a lie a browser can tell: policy can swallow the window, or the
        handler can be missing. Without the backoff, every slice of the wait relaunches
        it and nothing ever appears. A single poll from anywhere clears the count.
        """
        now = time.time()
        contacted = self.store.page_last_seen() >= self._last_open_at
        if self._last_open_at and not contacted:
            grace = min(
                OPEN_GRACE_SEC * (2 ** self._opens_without_contact), OPEN_GRACE_MAX_SEC
            )
        else:
            self._opens_without_contact = 0
            grace = OPEN_GRACE_SEC
        if (now - self._last_open_at) < grace:
            return
        self._opens_without_contact += 1
        self._last_open_at = now
        try:
            if webbrowser.open(self.url):
                return
        except Exception as exc:  # noqa: BLE001
            log.debug("webbrowser.open failed: %s", exc)
        try:
            os.startfile(self.url)  # type: ignore[attr-defined]
            return
        except Exception:  # noqa: BLE001
            pass
        log.warning("Could not open a browser automatically. Open %s manually.", self.url)

    # ---- http ----------------------------------------------------------
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                log.debug("approval-ui %s", fmt % args)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, payload: Any, code: int = 200) -> None:
                self._send(
                    code,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/":
                    self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/health":
                    self._json(
                        {"server": SERVER_SIGNATURE, "state_dir": str(server.store.state_dir)}
                    )
                elif path == "/api/pending":
                    server.note_poll()
                    now = time.time()
                    self._json({"requests": [
                        {
                            "id": e["id"],
                            "plan_id": e.get("plan_id"),
                            "goal": e.get("goal"),
                            "summary": e.get("summary") or "",
                            "display": e.get("display"),
                            # The task list was already being persisted for the
                            # fingerprint; the page needs it to offer per-task review.
                            "tasks": e.get("tasks") or [],
                            # Deliberately NOT defaulted. An entry written by an older
                            # process has no phase, and guessing PLAN would offer
                            # per-task review on what may be a completion report. The
                            # page treats a missing phase as "use the original form".
                            "phase": e.get("phase"),
                            "decided": e.get("decision"),
                            # Whether an agent is still holding a call open for this
                            # request. Deliberately not a countdown: the request
                            # outlives any single tool call, so a deadline would be a
                            # lie. What the human cannot otherwise tell is whether
                            # deciding right now resumes the conversation on its own.
                            "agent_waiting": (
                                now - float(e.get("agent_last_seen") or 0.0)
                            ) < AGENT_IDLE_SEC,
                        }
                        for e in server.store.peek()
                    ]})
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/api/decide":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.send_error(400)
                    return
                ok = server.store.record_decision(
                    str(body.get("id", "")),
                    str(body.get("decision", "")).upper(),
                    body.get("comment", ""),
                    body.get("task_comments"),
                    body.get("scope"),
                )
                self._json({"ok": ok})

        return Handler
