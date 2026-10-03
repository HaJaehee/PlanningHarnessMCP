"""다중 계획 종단 검증: 두 세션이 각자의 계획으로 동시에 일할 수 있는가?

  1) 서버 A, B 를 같은 state_dir 로 띄운다 (승인 페이지는 하나)
  2) A 와 B 가 서로 다른 목표로 각자 계획을 세운다  -> 서로를 밀어내지 않아야 한다
  3) 둘 다 승인을 요청한다                          -> 페이지에 2건이 동시에 떠야 한다
  4) 페이지에서 각각 승인한다                        -> 각자의 블로킹이 각각 풀려야 한다
  5) 각 세션이 자기 작업만 진행한다                   -> 상대 계획은 그대로여야 한다
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
PORT = 8771
_failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


class Session:
    def __init__(self, state_dir: str):
        env = dict(os.environ, PYTHONUTF8="1", PLANNING_MCP_BLOCKING_APPROVAL="true",
                   PLANNING_MCP_APPROVAL_PORT=str(PORT),
                   PLANNING_MCP_APPROVAL_OPEN_BROWSER="false",
                   PLANNING_MCP_APPROVAL_TIMEOUT="900",
                   # The model-asks flow of 3.0 (still supported). The flow where the server asks
                   # by itself is tests/smoke_gate_and_run.py.
                   PLANNING_MCP_AUTO_ASK="false")
        self.p = subprocess.Popen(
            [sys.executable, "-u", str(SERVER), "--state-dir", state_dir, "--log-level", "ERROR"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env)
        self.n = 0
        self.lock = threading.Lock()
        self.replies: dict[int, dict] = {}
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2024-11-05"})

    def _read(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                with self.lock:
                    self.replies[msg["id"]] = msg

    def _send(self, method, params, mid) -> None:
        m = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params:
            m["params"] = params
        self.p.stdin.write(json.dumps(m) + "\n")
        self.p.stdin.flush()

    def request(self, method, params=None, wait=60):
        self.n += 1
        mid = self.n
        self._send(method, params, mid)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            with self.lock:
                if mid in self.replies:
                    return self.replies.pop(mid)
            time.sleep(0.02)
        return None

    def call(self, name, args, wait=60):
        r = self.request("tools/call", {"name": name, "arguments": args}, wait)
        return json.loads(r["result"]["content"][0]["text"]) if r else None

    def call_async(self, name, args) -> int:
        self.n += 1
        mid = self.n
        self._send("tools/call", {"name": name, "arguments": args}, mid)
        return mid

    def reply_for(self, mid):
        with self.lock:
            msg = self.replies.pop(mid, None)
        return json.loads(msg["result"]["content"][0]["text"]) if msg else None

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def page(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def decide(request_id, decision):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/decide",
        data=json.dumps({"id": request_id, "decision": decision, "comment": ""}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        print("== 1) 같은 state_dir 로 서버 2개 ==")
        A = Session(tmp); time.sleep(1.2)
        B = Session(tmp); time.sleep(1.2)

        print("\n== 2) 서로 다른 목표로 각자 계획 ==")
        pa = A.call("plan_and_think", {"goal": "A: 회의실 예약", "thought": "t",
                                       "step_number": 1, "total_steps": 1,
                                       "need_more_thinking": False,
                                       "task_list": ["A작업1", "A작업2"]})
        pb = B.call("plan_and_think", {"goal": "B: 보고서 요약", "thought": "t",
                                       "step_number": 1, "total_steps": 1,
                                       "need_more_thinking": False,
                                       "task_list": ["B작업1"]})
        check("서로 다른 plan_id", pa["plan_id"] != pb["plan_id"],
              f"{pa['plan_id']} vs {pb['plan_id']}")
        check("A의 작업이 그대로", [t["title"] for t in pa["tasks"]] == ["A작업1", "A작업2"])
        check("B의 작업이 그대로", [t["title"] for t in pb["tasks"]] == ["B작업1"])
        check("여러 계획이므로 힌트에 plan_id 포함", pb["plan_id"] in pb["next_action_hint"])

        print("\n== 3) 둘 다 승인 요청 -> 페이지에 2건 ==")
        ma = A.call_async("request_user_approval",
                          {"decision": "ASK_USER", "plan_summary": "A 요약",
                           "plan_id": pa["plan_id"]})
        mb = B.call_async("request_user_approval",
                          {"decision": "ASK_USER", "plan_summary": "B 요약",
                           "plan_id": pb["plan_id"]})
        time.sleep(3)
        queue = page("/api/pending")["requests"]
        check("승인 대기 2건이 동시에 노출", len(queue) == 2, f"{len(queue)}건")
        plans_shown = {q["plan_id"] for q in queue}
        check("두 계획 모두 표시", plans_shown == {pa["plan_id"], pb["plan_id"]}, str(plans_shown))
        check("두 호출 모두 블로킹 중",
              A.reply_for(ma) is None and B.reply_for(mb) is None)

        print("\n== 4) 각각 승인 -> 각자의 블로킹 해제 ==")
        for q in queue:
            decide(q["id"], "APPROVED")
        ra = rb = None
        for _ in range(80):
            ra = ra or A.reply_for(ma)
            rb = rb or B.reply_for(mb)
            if ra and rb:
                break
            time.sleep(0.2)
        check("A의 호출 반환", ra is not None)
        check("B의 호출 반환", rb is not None)
        if ra and rb:
            check("A가 APPROVED", ra["plan_status"] == "APPROVED", ra["plan_status"])
            check("B가 APPROVED", rb["plan_status"] == "APPROVED", rb["plan_status"])
            check("각자 자기 계획", ra["plan_id"] == pa["plan_id"] and rb["plan_id"] == pb["plan_id"])

        print("\n== 5) 각 세션이 자기 작업만 진행 ==")
        A.call("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS",
                                        "plan_id": pa["plan_id"]})
        done = A.call("update_task_progress", {"task_id": 1, "status": "DONE",
                                               "result_log": "A1 결과를 /tmp/a1.txt 에 저장함",
                                               "plan_id": pa["plan_id"]})
        check("A의 진행이 기록됨", done["progress"] == "1/2 done", done.get("progress"))
        bnow = B.call("get_current_plan", {"plan_id": pb["plan_id"]})
        check("B의 계획은 손대지 않음", bnow["tasks"][0]["status"] == "PENDING",
              bnow["tasks"][0]["status"])
        check("B의 목표 보존", bnow["goal"] == "B: 보고서 요약", bnow["goal"])

        A.close(); B.close()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} - {', '.join(_failures)}")
        return 1
    print("다중 계획 종단 검증 통과 — 세션들이 서로를 밀어내지 않는다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
