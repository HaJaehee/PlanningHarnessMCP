"""Serve the approval page over fixed sample data, to look at it in a browser.

    python tools/page_preview.py                # http://127.0.0.1:8810/
    python tools/page_preview.py --port 8811

Every kind of card the page draws - a plan with a choice and criteria, a revised plan, a
completion report with what the server found, both kinds of halt, a request from an older
server, decided requests, and running plans with and without a pending stop - taken from
tests/web/fixtures.json, the same data the page tests render. It is a real ApprovalServer
over a throwaway state directory, so the buttons work: a decision is recorded and its card
changes. Restart to start over.

Not on 8765 by default: that is where a live planning-mcp serves its page. Nor in
8781-8799, which the test suite binds.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planning.approval import (  # noqa: E402
    APPROVAL_FILENAME,
    RUNS_FILENAME,
    ApprovalServer,
    ApprovalStore,
)
from planning.config import SERVER_VERSION  # noqa: E402

FIXTURES = ROOT / "tests" / "web" / "fixtures.json"
PAGE_VERSION = "@page"
DEFAULT_PORT = 8810
# The page counts an agent as waiting while it touched its request in the last 25s.
TOUCH_SEC = 10.0


def load_fixtures(version: str = SERVER_VERSION, path: Path = FIXTURES) -> dict[str, Any]:
    """The fixtures as /api/pending would answer them, served by `version`."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["requests"] + data["runs"]:
        if item.get("version") == PAGE_VERSION:
            item["version"] = version
    return {"requests": data["requests"], "runs": data["runs"]}


def _stored_request(item: dict[str, Any], created_at: float) -> dict[str, Any]:
    """One /api/pending request back in the form ApprovalStore keeps it."""
    entry: dict[str, Any] = {
        "id": item["id"],
        "plan_id": item["plan_id"],
        "goal": item["goal"],
        "summary": item.get("summary") or "",
        "display": item.get("display") or "",
        "tasks": item.get("tasks") or [],
        "fingerprint": item["id"],
        "draft": bool(item.get("draft")),
        "origin": item.get("origin") or "",
        "created_at": created_at,
        "decision": item.get("decided"),
        "comment": "",
        "task_comments": {},
        "scope": "PLAN",
        "choices": {},
        "criteria": {},
        "decided_at": created_at if item.get("decided") else None,
        "agent_note": item.get("agent_note") or "",
        "agent_last_seen": created_at if item.get("agent_waiting") else 0.0,
    }
    # Absent, not null: that is how an entry from an older server looks on disk.
    if item.get("phase") is not None:
        entry["phase"] = item["phase"]
    if item.get("version") is not None:
        entry["server_version"] = item["version"]
    return entry


def _stored_run(item: dict[str, Any], now: float) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "plan_id": item["plan_id"],
        "goal": item["goal"],
        "tasks": item.get("tasks") or [],
        "rev": item.get("rev") or "",
        "updated_at": item.get("updated_at"),
    }
    if item.get("version") is not None:
        entry["server_version"] = item["version"]
    if item.get("control"):
        entry["control"] = dict(item["control"], at=now)
    return entry


def seed(state_dir: Path, fixtures: dict[str, Any]) -> None:
    """Write the fixtures into `state_dir` as the approval queue and the run board."""
    now = time.time()
    state_dir.mkdir(parents=True, exist_ok=True)
    # created_at keeps the queue in fixture order: the page lists it oldest first.
    requests = [_stored_request(item, now + i / 1000)
                for i, item in enumerate(fixtures["requests"])]
    runs = {item["plan_id"]: _stored_run(item, now) for item in fixtures["runs"]}
    (state_dir / APPROVAL_FILENAME).write_text(
        json.dumps({"requests": requests}, ensure_ascii=False, indent=2), encoding="utf-8")
    (state_dir / RUNS_FILENAME).write_text(
        json.dumps({"runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open the page in a browser")
    args = parser.parse_args(argv)

    fixtures = load_fixtures()
    waiting = [r["id"] for r in fixtures["requests"] if r.get("agent_waiting")]
    with tempfile.TemporaryDirectory(prefix="page-preview-") as tmp:
        store = ApprovalStore(Path(tmp))
        seed(store.state_dir, fixtures)
        server = ApprovalServer(store, port=args.port, open_browser=args.open)
        if not server.start() or not server.owns_page:
            print(f"Port {args.port} is taken. Try --port with another number.", file=sys.stderr)
            return 1
        stop = threading.Event()

        def touch() -> None:
            # Keeps the "agent is waiting" chip on the cards that show it.
            while not stop.wait(TOUCH_SEC):
                for request_id in waiting:
                    store.touch_agent(request_id)

        threading.Thread(target=touch, name="preview-touch", daemon=True).start()
        print(f"Approval page preview: {server.url}  (Ctrl+C to stop)", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
