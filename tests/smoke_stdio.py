"""End-to-end smoke test of the real stdio transport.

Spawns `server.py` exactly the way AnythingLLM does, speaks JSON-RPC over the pipe, and
walks a full plan -> approval -> execute -> complete lifecycle.

    python tests/smoke_stdio.py

Exits non-zero on the first mismatch. Prints a readable transcript so you can eyeball what
the model would actually receive.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' - ' + detail) if detail and not condition else ''}")
    if not condition:
        _failures.append(label)


class Client:
    def __init__(self, state_dir: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(SERVER), "--state-dir", str(state_dir),
             "--log-level", "ERROR"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            cwd=str(tempfile.gettempdir()),  # prove the state dir does not depend on CWD
            # This script drives the two-phase approval protocol; blocking approval
            # waits for a human and is covered by the unit suite instead.
            # The model-asks flow of 3.0 (still supported). The flow where the server asks
            # by itself is tests/smoke_gate_and_run.py.
            env=dict(os.environ, PLANNING_MCP_BLOCKING_APPROVAL="false", PYTHONUTF8="1",
                     PLANNING_MCP_AUTO_ASK="false"),
        )
        self._id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed the stream")
        return json.loads(line)

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call(self, name: str, arguments: dict) -> dict:
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        return json.loads(res["result"]["content"][0]["text"])

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        c = Client(state_dir)
        try:
            print("\n== handshake ==")
            init = c.request("initialize", {"protocolVersion": "2024-11-05",
                                            "clientInfo": {"name": "smoke", "version": "0"}})
            check("initialize returns serverInfo", "serverInfo" in init["result"])
            c.notify("notifications/initialized")

            tools = c.request("tools/list")["result"]["tools"]
            check("exactly 4 tools advertised", len(tools) == 4, str(len(tools)))

            print("\n== phase 1: planning ==")
            r = c.call("plan_and_think", {
                "goal": "Q3 리포트를 요약한다.",
                "thought": "먼저 파일을 찾아야 한다.",
                "step_number": 1, "total_steps": 2, "need_more_thinking": True})
            check("step 1 recorded", r["next_action"] == "CALL_PLAN_AND_THINK", r["next_action"])

            print("\n== guard: execution before approval ==")
            r = c.call("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
            check("blocked with PLAN_NOT_APPROVED",
                  r.get("error_code") == "PLAN_NOT_APPROVED", str(r.get("error_code")))

            print("\n== phase 1: finalize (with sloppy input on purpose) ==")
            r = c.call("plan_and_think", {
                "goal": "Q3 리포트를 요약한다.",
                "thought": "분해 완료.",
                "step_number": "7",              # wrong number, as a string
                "total_steps": 2,
                "need_more_thinking": "false",   # boolean as a string
                "task_list": "1. 파일 찾기\n2. 표 추출\n3. 요약 작성"})  # string, numbered
            check("plan awaits approval", r["plan_status"] == "AWAITING_APPROVAL", r["plan_status"])
            check("3 tasks parsed from a numbered string", len(r["tasks"]) == 3,
                  json.dumps(r["tasks"], ensure_ascii=False))
            check("step_number corrected to 2", r["recorded_step"] == 2, str(r["recorded_step"]))
            check("next_action is the approval gate",
                  r["next_action"] == "CALL_REQUEST_USER_APPROVAL", r["next_action"])

            print("\n== phase 2: HITL gate ==")
            r = c.call("request_user_approval", {
                "decision": "ASK_USER",
                "plan_summary": "파일을 찾고, 표를 추출하고, 요약을 작성합니다."})
            check("STOP_AND_WAIT_FOR_USER", r["next_action"] == "STOP_AND_WAIT_FOR_USER",
                  r["next_action"])
            check("display_to_user is pre-rendered", "파일 찾기" in r.get("display_to_user", ""))

            r = c.call("request_user_approval", {"decision": "네"})  # Korean alias
            check("Korean 'yes' understood as APPROVED", r["plan_status"] == "APPROVED",
                  r["plan_status"])
            check("points at task 1", r["next_task"]["task_id"] == 1)

            print("\n== phase 3: execution ==")
            c.call("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
            r = c.call("update_task_progress", {"task_id": 1, "status": "완료",
                                                "result_log": "q3.xlsx 를 찾음"})
            # The response no longer echoes the task it just changed; progress and the
            # handover to the next task are what say the DONE landed.
            check("Korean status alias works", r["ok"] and r["progress"] == "1/3 done",
                  str(r.get("error_code") or r["progress"]))
            check("hands over to task 2", r["next_task"]["task_id"] == 2,
                  str(r.get("next_task")))

            c.call("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
            r = c.call("update_task_progress", {"task_id": 2, "status": "FAILED",
                                                "result_log": "표를 읽을 수 없음"})
            check("failure blocks the plan", r["plan_status"] == "BLOCKED", r["plan_status"])
            check("must re-plan, not continue",
                  r["next_action"] == "CALL_PLAN_AND_THINK", r["next_action"])

            r = c.call("update_task_progress", {"task_id": 3, "status": "IN_PROGRESS"})
            check("task 3 refused while blocked", r.get("error_code") == "PLAN_BLOCKED",
                  str(r.get("error_code")))

            print("\n== recovery ==")
            r = c.call("get_current_plan", {"plan_id": "current"})
            check("recovery reports the same plan", r["plan_status"] == "BLOCKED", r["plan_status"])
            check("goal preserved", r["goal"] == "Q3 리포트를 요약한다.", r.get("goal", ""))
        finally:
            c.close()

        print("\n== persistence across a server restart ==")
        c2 = Client(state_dir)
        try:
            c2.request("initialize", {})
            r = c2.call("get_current_plan", {"plan_id": "current"})
            check("plan survived the restart", r["plan_status"] == "BLOCKED", r["plan_status"])
            check("task 1 still DONE", r["tasks"][0]["status"] == "DONE", r["tasks"][0]["status"])
        finally:
            c2.close()

        audit_lines = (state_dir / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
        events = [json.loads(l)["event"] for l in audit_lines]
        print("\n== audit trail ==")
        print("  " + " -> ".join(events))
        check("execution_blocked recorded as evidence", "execution_blocked" in events)
        check("approval recorded", "approved" in events)

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
