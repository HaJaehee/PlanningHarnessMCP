"""SSE 전송 종단 검증.

stdio 만 검증돼 있어 SSE 경로는 사각지대였다. 특히 블로킹 승인과 결합했을 때
POST 가 매달리지 않는지 확인한다.

  1) /sse 로 세션을 열고 endpoint 이벤트를 받는가
  2) initialize / tools/list 왕복
  3) 블로킹 승인 중에도 POST 가 즉시 202 로 응답하는가  <- 핵심
  4) 승인 페이지에서 승인하면 결과가 SSE 스트림으로 오는가
  5) 대기 중 다른 요청도 처리되는가
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
SSE_PORT = 8934
APPROVAL_PORT = 8935
_failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


class SseClient:
    def __init__(self) -> None:
        self.endpoint: str | None = None
        self.messages: list[dict] = []
        self.lock = threading.Lock()
        self._ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            stream = urllib.request.urlopen(f"http://127.0.0.1:{SSE_PORT}/sse", timeout=120)
        except Exception:
            return
        event = None
        try:
            for raw in stream:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
                    if event == "endpoint":
                        self.endpoint = data
                        self._ready.set()
                    else:
                        try:
                            with self.lock:
                                self.messages.append(json.loads(data))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass  # the server going away at the end of the run is expected

    def wait_ready(self, timeout: float = 15) -> bool:
        return self._ready.wait(timeout)

    def post(self, payload: dict) -> tuple[int, float]:
        started = time.monotonic()
        req = urllib.request.Request(
            f"http://127.0.0.1:{SSE_PORT}{self.endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, time.monotonic() - started

    def reply_for(self, msg_id: int, timeout: float = 30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                for m in self.messages:
                    if m.get("id") == msg_id:
                        return m
            time.sleep(0.05)
        return None

    def progress_notes(self) -> list[dict]:
        with self.lock:
            return [m for m in self.messages if m.get("method") == "notifications/progress"]


def tool_payload(mid: int, name: str, args: dict, token=None) -> dict:
    params: dict = {"name": name, "arguments": args}
    if token is not None:
        params["_meta"] = {"progressToken": token}
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": params}


def approval_page(path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{APPROVAL_PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, PYTHONUTF8="1", PLANNING_MCP_BLOCKING_APPROVAL="true",
                   PLANNING_MCP_APPROVAL_PORT=str(APPROVAL_PORT),
                   PLANNING_MCP_APPROVAL_OPEN_BROWSER="false",
                   PLANNING_MCP_APPROVAL_TIMEOUT="900",
                   # The model-asks flow of 3.0 (still supported). The flow where the server asks
                   # by itself is tests/smoke_gate_and_run.py.
                   PLANNING_MCP_AUTO_ASK="false")
        proc = subprocess.Popen(
            [sys.executable, "-u", str(SERVER), "--transport", "sse", "--port", str(SSE_PORT),
             "--state-dir", tmp, "--log-level", "ERROR"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            time.sleep(2.5)
            print("== 1) SSE 세션 수립 ==")
            c = SseClient()
            check("endpoint 이벤트 수신", c.wait_ready(), "타임아웃")
            if not c.endpoint:
                return 1
            check("session_id 포함", "session_id=" in c.endpoint, c.endpoint)

            print("\n== 2) 기본 왕복 ==")
            c.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"}})
            init = c.reply_for(1)
            check("initialize 응답", init is not None and "serverInfo" in init["result"])
            c.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = c.reply_for(2)
            check("도구 4개", tools is not None and len(tools["result"]["tools"]) == 4)

            print("\n== 3) 블로킹 승인 중 POST 가 매달리지 않는가 ==")
            c.post(tool_payload(3, "plan_and_think", {
                "goal": "SSE 검증", "thought": "t", "step_number": 1, "total_steps": 1,
                "need_more_thinking": False, "task_list": ["SSE작업1"]}))
            check("계획 생성", c.reply_for(3) is not None)

            status, elapsed = c.post(tool_payload(
                4, "request_user_approval",
                {"decision": "ASK_USER", "plan_summary": "SSE 요약"}, token="sse-tok"))
            check("POST 가 즉시 202 반환", status == 202 and elapsed < 5,
                  f"status={status} elapsed={elapsed:.1f}s")
            time.sleep(2)
            check("아직 결과는 오지 않음 (블로킹 중)", c.reply_for(4, timeout=0.5) is None)

            print("\n== 4) 대기 중에도 다른 요청이 처리되는가 ==")
            c.post(tool_payload(5, "get_current_plan", {"plan_id": "current"}))
            check("다른 호출 응답", c.reply_for(5, timeout=10) is not None)

            print("\n== 5) 하트비트 (20초 간격이라 한 번은 지나야 함) ==")
            time.sleep(21)
            notes = c.progress_notes()
            check("progress 알림이 SSE 로 전달됨", len(notes) >= 1, f"{len(notes)}건")
            if notes:
                check("토큰이 되돌아옴", notes[0]["params"].get("progressToken") == "sse-tok")

            print("\n== 6) 승인하면 결과가 SSE 로 오는가 ==")
            queue = approval_page("/api/pending")["requests"]
            check("승인 페이지에 요청 노출", len(queue) == 1, f"{len(queue)}건")
            if queue:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{APPROVAL_PORT}/api/decide",
                    data=json.dumps({"id": queue[0]["id"], "decision": "APPROVED",
                                     "comment": ""}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
                done = c.reply_for(4, timeout=20)
                check("승인 결과가 SSE 스트림으로 도착", done is not None)
                if done:
                    payload = json.loads(done["result"]["content"][0]["text"])
                    check("APPROVED 로 반환", payload["plan_status"] == "APPROVED",
                          payload["plan_status"])

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} - {', '.join(_failures)}")
        return 1
    print("SSE 전송 종단 검증 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
