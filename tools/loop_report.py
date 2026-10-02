"""Read state/audit.jsonl and say whether, where and how an agent looped.

    python tools/loop_report.py                       # state/audit.jsonl
    python tools/loop_report.py path/to/audit.jsonl
    python tools/loop_report.py --json                # machine-readable

Offline and standard-library only: it reads one file and writes to stdout. Run it with
the same Python that runs the server, on the machine where the log is - nothing leaves
that machine.

Why it exists (D25): the planning loop was observed with Zed / Goose and a mid-sized
thinking model, and cannot be reproduced from the development machine. Since 1.16.0 the
server writes what a loop looks like into the audit log - which client, how long the
model was away between calls (a proxy for a thinking block nobody can see), how often
its thoughts say "wait" / "reconsider", and every pause the circuit breaker made. This
turns those lines into a per-plan answer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "state" / "audit.jsonl"

# A gap this long between the server's answer and the model's next planning call means
# the model spent it generating - most likely inside its own reasoning block.
LONG_GAP_SEC = 60.0


def load(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  ! line {n} is not JSON, skipped", file=sys.stderr)
    return events


def summarize(events: list[dict]) -> dict:
    plans: dict[str, dict] = defaultdict(lambda: {
        "client": None, "goal": None, "thinking_steps": 0, "revisions": 0,
        "reconsider": 0, "max_gap_sec": 0.0, "long_gaps": 0, "auto_finalized": 0,
        "halts": [], "halt_resolutions": [], "redirects": Counter(), "held_calls": 0,
        "finalized": 0, "approved": 0, "completed": False,
    })
    clients = Counter()
    stopped_without_plan = []
    for e in events:
        kind = e.get("event")
        if kind == "client_connected":
            clients[f"{e.get('client_name')} ({e.get('model_profile')})"] += 1
            continue
        if kind == "loop_stopped_without_plan":
            stopped_without_plan.append(e)
            continue
        pid = e.get("plan_id")
        if not pid:
            continue
        p = plans[pid]
        p["client"] = p["client"] or e.get("client")
        if kind == "plan_created":
            p["goal"] = e.get("goal")
        elif kind == "thinking_step":
            p["thinking_steps"] += 1
            if e.get("revises_step"):
                p["revisions"] += 1
            p["reconsider"] += int(e.get("reconsider") or 0)
            gap = e.get("gap_sec")
            if isinstance(gap, (int, float)):
                p["max_gap_sec"] = max(p["max_gap_sec"], float(gap))
                if gap >= LONG_GAP_SEC:
                    p["long_gaps"] += 1
        elif kind == "auto_finalized":
            p["auto_finalized"] += 1
        elif kind == "loop_halted":
            p["halts"].append(f"{e.get('reason')}({e.get('count')})")
        elif kind == "halt_resolved":
            p["halt_resolutions"].append(e.get("action"))
        elif kind == "replan_redirected":
            p["redirects"][e.get("plan_status")] += 1
        elif kind == "replan_after_completion_suppressed":
            p["redirects"]["COMPLETED"] += 1
        elif kind == "call_held_for_human":
            p["held_calls"] += 1
        elif kind == "plan_finalized":
            p["finalized"] += 1
        elif kind == "approved":
            p["approved"] += 1
        elif kind == "completion_verified":
            p["completed"] = True

    for p in plans.values():
        p["redirects"] = dict(p["redirects"])
        p["looped"] = bool(
            p["halts"] or p["auto_finalized"] or p["redirects"] or p["held_calls"]
            or p["long_gaps"] or p["reconsider"] >= 3
        )
    return {
        "clients": dict(clients),
        "plans": dict(plans),
        "stopped_without_plan": len(stopped_without_plan),
    }


def render(report: dict) -> str:
    out = []
    plans = report["plans"]
    looped = {k: v for k, v in plans.items() if v["looped"]}
    out.append(f"clients            : {report['clients'] or '(none recorded - pre-1.16 log?)'}")
    out.append(f"plans              : {len(plans)}  (with loop signs: {len(looped)})")
    out.append(f"plan-less stops    : {report['stopped_without_plan']}")
    halts = Counter(h.split("(")[0] for p in plans.values() for h in p["halts"])
    out.append(f"halts by reason    : {dict(halts) or '-'}")
    out.append("")
    if not looped:
        out.append("No plan shows signs of a loop.")
        return "\n".join(out)
    out.append("Plans with loop signs")
    out.append("-" * 72)
    for pid, p in sorted(looped.items()):
        out.append(f"{pid}  [{p['client'] or '?'}]  {(p['goal'] or '')[:50]}")
        out.append(
            f"   thinking steps {p['thinking_steps']} (revisions {p['revisions']}), "
            f"reconsider words {p['reconsider']}, max gap {p['max_gap_sec']:.0f}s "
            f"({p['long_gaps']} >= {LONG_GAP_SEC:.0f}s)"
        )
        extra = []
        if p["auto_finalized"]:
            extra.append(f"auto-finalized x{p['auto_finalized']}")
        if p["halts"]:
            extra.append(f"halted {', '.join(p['halts'])} -> {p['halt_resolutions'] or 'open'}")
        if p["redirects"]:
            extra.append(f"re-plans redirected {p['redirects']}")
        if p["held_calls"]:
            extra.append(f"calls held for the human x{p['held_calls']}")
        if extra:
            out.append("   " + "; ".join(extra))
    out.append("")
    out.append(
        "Reading it: long gaps with few calls = the loop is inside the thinking block "
        "(see docs/thinking-model-hosts.md); many steps/redirects/halts = across calls "
        "(the server now bounds those)."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=str(DEFAULT_LOG))
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    path = Path(args.path)
    if not path.is_file():
        print(f"No audit log at {path}", file=sys.stderr)
        return 1
    report = summarize(load(path))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
