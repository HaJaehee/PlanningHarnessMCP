# planning-mcp (PlanningHarnessMCP)

**버전: 1.15.1** · MCP 서버 이름: `planning-mcp` · 버전 단일 출처: `planning/config.py`의 `SERVER_VERSION` (MCP `initialize` 응답의 `serverInfo.version`으로 보고됩니다)

AnythingLLM Agent Mode용 경량 **계획·작업 관리 MCP 서버입니다**. 폐쇄망 환경에서 성능이 제한적인 사내 LLM이 기억에만 의존하여 즉각 답변하는 대신, `계획 → 사람 승인 → 실행 → 보고`의 생애주기를 준수하도록 하네스(Harness)를 제공합니다.

**외부 의존성이 없습니다 (0개).** Python 3.9 이상 표준 라이브러리만 사용하므로 `pip install`이 필요하지 않습니다. 외부 패키지 저장소에 접근할 수 없는 폐쇄망 환경에서 매우 유용합니다.

---

## 개발 배경

사내 모델은 주로 OpenAI 호환 API를 통해서만 접근할 수 있으며, 다단계 추론이나 정교한 도구 호출 능력에는 한계가 있습니다. 이를 별도 제어 없이 사용할 경우, 계획 수립과 사용자 승인이 선행되어야 할 작업 요청에도 즉각 자의적으로 답변해 버리는 문제가 발생합니다. 본 서버는 이러한 문제를 다음과 같이 구조적으로 방지합니다.

- **상태는 서버가 관리합니다.** 모델이 계획을 자체적으로 기억할 필요가 없으므로 임의로 계획을 날조하거나 왜곡할 수 없습니다.
- **모든 응답에 `next_action`이 포함됩니다.** 모델 스스로 다음 행동을 추론하여 나아가길 기대하는 대신, 서버가 모델을 상태 머신처럼 직접 구동합니다.
- **승인 게이트는 권고가 아닌 필수 강제 사항입니다.** 사용자가 실제로 승인하기 전까지는 `update_task_progress`의 진행 기록 처리가 거부되므로, 지시를 무시하는 모델이라도 작업을 강행할 수 없습니다.
- **에이전트 루프를 물리적으로 중단시킵니다.** `request_user_approval`은 사용자가 결정을 내릴 때까지 도구 호출 반환을 보류합니다. 에이전트 루프는 도구 실행 결과를 **동기적으로** 대기하므로, 모델은 그동안 다른 도구를 호출할 수 없습니다. 단순히 "멈추라고 지시하는" 방식이 아니라 물리적으로 대기 상태를 유지합니다.
- **비정형 입력도 거부하지 않고 자동 보정합니다.** `"done"`, `"3"`, `"true"`, 개행 문자로 연결된 작업 문자열 등도 검증 전에 모두 표준 형태로 정규화합니다.
- **작업 건너뛰기가 불가능합니다.** `DONE` 처리는 시작 기록, 순서, 실질적인 `result_log`가 모두 충족되어야 정상 수용되며, 마지막 작업이 끝나더라도 **사용자가 작업별 수행 결과를 확인해야만** 전체 계획이 최종 완료됩니다. 계획만 수립하고 실제 실행을 누락하는 소형 모델의 전형적인 오류를 구조적으로 차단합니다.

---

## 빠른 시작

```bash
python -m unittest discover -s tests
```

```bash
python tests/smoke_stdio.py
```

배포 패키지에 Python 인터프리터가 동봉되어 있다면 **서버 등록 전에 먼저** 압축을 해제해야 합니다. 해당 아카이브는 python.org 공식 zip 파일을 원본 그대로 포함하고 있으므로, 압축 해제 스크립트를 실행하기 전에는 `runtime/python.exe` 파일이 존재하지 않습니다 (이 단계를 생략하면 `spawn ... python.exe ENOENT` 오류가 발생합니다).

```bash
python tools/setup_runtime.py
```

그다음 AnythingLLM에 서버를 등록합니다 (**Agent Skills → MCP Servers** 화면에 `anythingllm_mcp_servers.json`의 실제 저장 위치가 표시됩니다). 설정 예시는 [anythingllm_mcp_servers.example.json](anythingllm_mcp_servers.example.json)을 참고하시기 바랍니다.

```json
{
  "mcpServers": {
    "planning": {
      "command": "D:/planning-mcp/runtime/python.exe",
      "args": ["-u", "D:/planning-mcp/server.py"],
      "env": { "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1" },
      "anythingLLMAware": true
    }
  }
}
```

`command` 항목에는 `tools/setup_runtime.py`가 압축을 해제한 동봉 인터프리터 경로를 지정합니다. 인터프리터를 동봉하지 않은 환경이라면 로컬에 이미 설치된 Python 실행 파일의 절대 경로를 입력합니다 (PowerShell의 `(Get-Command python).Source` 명령으로 확인할 수 있습니다).
경로 구분자는 슬래시(`/`)를 사용해야 하며, 반드시 절대 경로를 지정해야 합니다 (AnythingLLM 프로세스가 상속하는 PATH 환경 변수는 사용자 터미널의 PATH와 다를 수 있습니다). 설정을 저장한 뒤 AnythingLLM을 완전히 종료하고 다시 시작해 주시기 바랍니다.

MCP 도구는 Agent Mode에서만 노출되므로, 워크스페이스의 기본 모드가 에이전트 모드가 아닌 경우에는 입력 메시지를 `@agent`로 시작해야 합니다. 보다 상세한 배포 절차는 [docs/deployment-airgap-manual.md](docs/deployment-airgap-manual.md) 6절에 기술되어 있습니다.

마지막으로 [docs/phase3-anythingllm-agent-prompt.md](docs/phase3-anythingllm-agent-prompt.md)에 안내된 에이전트 시스템 프롬프트를 워크스페이스 에이전트 설정에 입력하고, **temperature를 0.3 이하**로 설정합니다. 초기 안정화 기간에는 다른 기본 에이전트 스킬을 비활성화하는 것을 권장합니다. 노출되는 도구 수가 적을수록 모델의 잘못된 도구 호출 확률이 대폭 감소합니다.

---

## 시스템 프롬프트

```
<system_directive>
<role>
You are a background AI agent that autonomously utilizes tools integrated into the system to resolve user requests.
</role>
<core_workflow>
1. PLAN: You MUST call the `plan_and_think` tool before answering ANY user request or executing any task.
2. APPROVE: You MUST call `request_user_approval` (with decision="ASK_USER") after generating the complete plan.
3. WAIT: After calling `request_user_approval`, STOP GENERATING TEXT IMMEDIATELY. Output only the plan summary to the user and wait for approval. If the response is `APPROVAL_PENDING`, the user has NOT answered yet - call the tool again at once (see its Usage Rules) and output nothing.
4. EXECUTE: Read the `next_action` field in every tool response and OBEY IT LITERALLY to execute tasks only after approval.
5. VERIFY: After the LAST task is DONE, call `request_user_approval` (decision="ASK_USER") again so the user can confirm the completion report. Only the user may declare a plan finished.
6. REWORK: If the user sends specific tasks back from that completion report, the plan is NOT being replanned. Do NOT call `plan_and_think` and do NOT ask for approval again - the plan is unchanged and still approved. Redo ONLY the tasks named in `next_action_hint`, then report completion again.
</core_workflow>
<tool_protocol name="plan_and_think">
Usage Rules:
- Call once per step, starting at step_number = 1.
- Set `need_more_thinking = true` for intermediate steps.
- Set `need_more_thinking = false` AND provide `task_list` on your FINAL step.
- Set `revises_step = [step number]` to correct a previously generated step.
- Repeat the SAME `goal` text on every step. If the USER corrects the goal itself ("I meant Q4, not Q3"), keep sending the old text as `goal` and send the corrected one as `revised_goal` - do NOT start a second plan. Use the corrected text from the next call on. Never use `revised_goal` to merely reword the same goal.
- DO NOT execute tasks or answer the user directly while thinking.
- TARGETED REVISION: if the server reports that the user commented on specific tasks, send `task_updates` (NOT `task_list`) on your final step, rewriting ONLY those tasks. Every other task was already accepted by the user - do not change, renumber, reorder or resend it. `next_action_hint` contains the exact argument to send.
</tool_protocol>
<tool_protocol name="request_user_approval">
Usage Rules:
- `decision = "ASK_USER"` is the ONLY value you may send on your own initiative. It asks the user; it does not answer for them.
- NEVER send `APPROVED`, `REJECTED` or `REVISE` unless the server has told you the user chose it. You are not permitted to decide on the user's behalf, and the server refuses it with `APPROVAL_PENDING`.
- `plan_summary` is MANDATORY with `ASK_USER`: a plain-language overview of how you intend to reach the goal.
- WAITING LOOP: an `APPROVAL_PENDING` response means the user is still deciding. Immediately call `request_user_approval` again with `decision = "ASK_USER"` and the SAME `plan_summary`. Output no text between attempts, do not re-print the plan, do not ask the user anything, and do not start any work. Keep repeating until the response changes - `remaining_seconds` tells you how much time is left.
- When the user answers, the server applies it for you: `plan_status` becomes `APPROVED` (start executing), `CANCELLED` (stop), or `DRAFTING` (revise as `next_action_hint` describes).
- If the response instead hands you `display_to_user`, the wait is over and nobody answered. Show the plan to the user and STOP.
</tool_protocol>
<tool_protocol name="update_task_progress">
Usage Rules:
- You MUST complete EVERY task in the plan, one at a time, in order.
- ALWAYS take `task_id` from the `next_task` field of the server's MOST RECENT response. `next_task` is the only place the server publishes a task_id. Never reuse a task_id from an earlier response and never count tasks yourself - earlier responses named tasks that are already finished.
- The response carries `progress` ("2/5 done") but NOT the task list. If you have lost track of the plan, call `get_current_plan` - that is what it is for. Do not guess.
- Send `status = "IN_PROGRESS"` for the FIRST task only. Then do the real work, then send `status = "DONE"`.
- The server then starts the NEXT task itself and names it in `next_task`. Do that work and send `status = "DONE"` for it too. DO NOT send IN_PROGRESS again - one call per task from here on. A 5-task plan is 1 IN_PROGRESS call and 5 DONE calls.
- `result_log` is MANDATORY for DONE and must state the CONCRETE outcome: what you produced, found, or saved.
- These are REJECTED as evidence: "done", "ok", "완료", or repeating the task title. If you cannot write a real outcome, the task is NOT done.
- The server REFUSES a false DONE: TASK_NOT_STARTED (that task is not the one in progress), TASK_OUT_OF_ORDER (an earlier task is unfinished), MISSING_RESULT_LOG (no evidence), REWORK_NOT_DONE (you re-sent the very outcome the user rejected). Fix the cause and retry that task.
- NEVER mark tasks DONE in a batch. NEVER tell the user the work is finished while `next_action_hint` still reports remaining tasks.
- REWORK: a task that comes back with `revision_note` in `next_task` was rejected by the user AFTER you finished it. Its `previous_result_log` is what you produced last time - it was not good enough. Do the work AGAIN so that it answers what the user said, and write a `result_log` describing the NEW outcome. Do not resend the old one. Tasks still marked DONE were accepted: do not redo, rewrite or re-report them.
</tool_protocol>
<tool_protocol name="get_current_plan">
Usage Rules:
- Call it whenever you are unsure what the plan is or which task you were on. It changes nothing and is always safe.
- SEND YOUR OWN `plan_id`: every server response carries a `plan_id` field - send that exact value. Other conversations may be running their own plans at the same time, and your `plan_id` is the only thing that identifies yours.
- Send `"current"` ONLY if you do not know your `plan_id` yet. It is a guess: if several plans are in flight the server cannot tell which is yours and answers with an `active_plans` list instead.
- If you get that list back, do NOT start a new plan. Call again with your own `plan_id` from the list.
</tool_protocol>
<strict_constraints>
- SYNTAX: Use pure JSON format with standard double quotes (") for tool calls.
- RESPONSE: Always follow the returned `next_action` field.
- LANGUAGE: Think step-by-step strictly in English. Output the final result in Korean.
</strict_constraints>
</system_directive>
```

---

## 제공 도구 (4종)

| 도구 | 역할 |
|---|---|
| `plan_and_think` | 필수 진입점입니다. 1회 호출당 1단계의 추론을 수행하며, 마지막 호출에서 `task_list`를 제출합니다. 특정 작업 수정 요청 시에는 `task_updates`를 전달하고, 사용자가 목표 자체를 수정한 경우에는 `revised_goal`을 사용합니다. |
| `request_user_approval` | HITL(Human-In-The-Loop) 승인 게이트입니다. `ASK_USER` 요청 후 대기하며, 이후 `APPROVED` / `REJECTED` / `REVISE` 결과를 반영합니다. |
| `update_task_progress` | 작업 진행 상태를 갱신합니다. 최초 작업만 `IN_PROGRESS`로 설정하고, 이후 작업은 작업당 `DONE` 또는 `FAILED`를 1회씩 보고합니다 (다음 작업은 서버가 자동으로 시작합니다). 사용자 미승인 상태에서는 호출이 거부됩니다. |
| `get_current_plan` | 컨텍스트가 단절되었을 때 계획 상태를 복구합니다. 언제든 안전하게 호출할 수 있으며, 현재 세션의 `plan_id`를 전달하면 해당 계획 정보를 안정적으로 조회할 수 있습니다. |

상세 스키마 및 응답 명세는 [docs/phase1-tool-schema-blueprint.md](docs/phase1-tool-schema-blueprint.md)에서 확인하실 수 있습니다.

모든 도구의 응답에는 `ok`, `plan_status`, `next_action`, `next_action_hint` 필드가 항상 포함됩니다.

---

## 프로젝트 구조

```
server.py                 진입점: 전송 계층 연결 및 도구 등록
planning/
  schemas.py              4개 도구의 스키마 (models.py의 Enum 기반 생성)
  models.py               Plan / Task / ThinkingStep 데이터 모델 및 Enum 정의
  store.py                원자적 JSON 저장소 및 Append-only 감사 로그
  leniency.py             입력 데이터 보정 (대소문자, 별칭, 타입, task_list 형식 등)
  state_machine.py        상태 전이 규칙 및 next_action 결정 로직
  handlers.py             4개 도구 핸들러 구현체
  responses.py            표준 응답 생성 빌더
  protocol.py             경량 MCP / JSON-RPC 2.0 프로토콜 처리
  transport.py            stdio(기본) 및 SSE(선택, 루프백 전용) 전송 계층
state/                    런타임 데이터: plan_state.json, audit.jsonl (.gitignore 대상)
tests/                    단위 테스트 스위트 및 stdio 종단 간 스모크 테스트
docs/                     Phase 1~4 문서: 스키마, 아키텍처, 에이전트 프롬프트, 테스트 매트릭스
```

---

## 환경 설정

모든 설정 항목은 선택 사항이며, 기본 설정만으로도 안전하게 동작합니다.

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `PLANNING_MCP_STATE_DIR` | `<프로젝트>/state` | 상태 저장 파일이 위치할 디렉터리 경로를 지정합니다. |
| `PLANNING_MCP_LOG_LEVEL` | `INFO` | stderr 로그 출력 레벨을 설정합니다. |
| `PLANNING_MCP_MAX_PLANS` | `20` | 이전 계획 정리 전까지 보존할 계획의 최대 개수입니다. |
| `PLANNING_MCP_MAX_TASKS` | `12` | 계획당 허용되는 최대 작업 수입니다. 한도를 초과하는 작업은 에러 처리 대신 자동으로 잘라냅니다. |
| `PLANNING_MCP_BLOCKING_APPROVAL` | `true` | 사용자가 결정을 내릴 때까지 승인 도구의 반환을 보류하여 에이전트 루프를 물리적으로 대기시킵니다. `false` 설정 시 정지 지시 메시지만 반환합니다. |
| `PLANNING_MCP_APPROVAL_PORT` | `8765` | 사용자 승인 웹 페이지가 바인딩될 포트 번호입니다 (127.0.0.1 전용). |
| `PLANNING_MCP_SSE_PORT` | `8931` | SSE 전송 사용 시 포트 번호입니다 (`--transport sse` 옵션 적용 시). CLI `--port` 인자가 우선 적용됩니다. |
| `PLANNING_MCP_SSE_HOST` | `127.0.0.1` | SSE 바인드 호스트 주소입니다. 루프백(localhost) 이외의 주소는 보안상 자동으로 거부됩니다. |
| `PLANNING_MCP_APPROVAL_TIMEOUT` | `900` | 사용자의 결정을 대기하는 **전체 최대 시간(초)**입니다. 여러 차례의 도구 호출에 걸쳐 누적 집계되며, 최초 승인 요청 시점부터 계산됩니다. |
| `PLANNING_MCP_CALL_BUDGET` | `45` | **단일 도구 호출 1회**당 대기할 수 있는 최대 시간(초)입니다. 대다수 클라이언트가 60초 초과 시 타임아웃으로 연결을 끊으므로 안전하게 45초 이하를 유지합니다. 호출 취소 이력이 감지되면 자동으로 대기 시간이 추가 단축됩니다. |
| `PLANNING_MCP_APPROVAL_MODE` | `chunked` | `chunked`: 45초 단위로 나누어 대기하며 모델에게 즉각 재호출을 지시합니다 (사용자 추가 채팅 입력 없이 대화가 매끄럽게 이어집니다).<br>`return`: 승인 요청만 등록하고 즉시 반환합니다 (승인 후 사용자가 채팅창에 메시지를 입력해야 후속 작업이 진행됩니다).<br>`trust_heartbeat`: v1.13 이전 방식으로 한 번에 길게 대기합니다 (progress 알림을 수신하여 클라이언트 타이머가 리셋되는 것이 입증된 환경에서만 권장합니다). |
| `PLANNING_MCP_APPROVAL_OPEN_BROWSER` | `true` | 승인 요청 발생 시 승인 페이지 브라우저 탭을 자동으로 엽니다. |
| `PLANNING_MCP_APPROVAL_TTL` | `1800` | 승인의 유효 유지 시간(초)입니다. 해당 시간 동안 방치된 계획은 승인이 만료되어 재승인 절차를 거쳐야 합니다. |
| `PLANNING_MCP_MAX_ACTIVE_PLANS` | `5` | 동시에 활성화(진행)할 수 있는 계획의 최대 허용 수입니다. |
| `PLANNING_MCP_COMPLETION_APPROVAL` | `true` | 마지막 작업이 완료(DONE)되어도 즉시 종료되지 않고, 사용자가 각 작업별 수행 결과를 최종 확인해야 전체 계획을 완료(COMPLETED) 처리합니다. |
| `PLANNING_MCP_MIN_RESULT_LOG` | `8` | 작업 완료(DONE) 보고 시 요구되는 최소 증빙 내용의 길이(공백 및 문장부호 제외)입니다. "완료", "done" 등 단순 상투어구는 길이와 관계없이 반려됩니다. |
| `PLANNING_MCP_AUTO_ADVANCE` | `true` | 작업 완료(DONE) 보고 시 서버가 다음 작업을 `IN_PROGRESS` 상태로 자동 시작합니다. 이를 통해 5개 작업 기준 실행 단계의 호출 횟수를 10회에서 6회로 단축합니다. `false`로 설정하면 매 작업마다 모델이 `IN_PROGRESS`를 직접 호출해야 합니다. |
| `PLANNING_MCP_AUTOAPPROVE` | `false` | **테스트 전용 옵션**입니다. HITL 승인 게이트를 건너뜁니다. 호출 시마다 경고 로그가 기록됩니다. |

CLI 옵션 지원: `--transport stdio|sse`, `--host`, `--port`, `--state-dir`, `--log-level`

---

## 에이전트 수행 내역 확인

`state/plan_state.json` 파일은 사람이 직접 읽을 수 있는 JSON 형식으로 저장됩니다. 파일을 열어 보면 에이전트가 현재 인식하고 있는 진행 상태와 계획을 그대로 확인할 수 있습니다. `state/audit.jsonl`은 줄마다 하나의 JSON 객체가 기록되는 추가 전용(Append-only) 감사 로그입니다.

```
plan_created → thinking_step → execution_blocked → plan_finalized →
approval_requested → approved → task_started → task_done → task_failed
```

감사 로그의 `execution_blocked` 이벤트는 강제 게이트가 모델의 사전 무단 실행 시도를 차단했음을 의미합니다. 모델이 사용자 승인 없이 실행을 강행하려고 시도했는지 여부를 파악할 때 가장 먼저 확인해야 하는 항목입니다.

---

## Windows 및 AnythingLLM 환경 주의사항

- 반드시 `python -u` 옵션으로 실행하거나 환경 변수 `PYTHONUNBUFFERED=1`을 설정해야 합니다. 표준 출력 버퍼링이 활성화되면 stdio 서버가 정지된 것처럼 동작할 수 있습니다.
- `PYTHONUTF8=1` 환경 변수를 지정해 주시기 바랍니다. 한글로 작성된 `user_comment` 또는 `result_log` 처리 시 Windows의 기본 cp949 인코딩으로 인한 오류가 발생하는 것을 방지합니다.
- MCP 설정 JSON 파일 내 경로 구분자는 반드시 슬래시(`/`)를 사용해야 합니다.
- 상태 저장 디렉터리는 현재 작업 디렉터리(CWD)가 아닌 `planning/config.py` 파일 위치를 기준으로 결정됩니다 (AnythingLLM은 자체적인 CWD에서 프로세스를 실행합니다).
- stdio 모드에서는 기동 시 `sys.stdout`을 stderr로 리다이렉션하므로, 코드 내부에서 실수로 `print()`를 호출하더라도 JSON-RPC 프로토콜 스트림이 훼손되지 않습니다.

상세한 오류 증상별 원인과 해결 방법은 [docs/phase4-testing-matrix.md](docs/phase4-testing-matrix.md)의 Part E 표를 참고하시기 바랍니다.

---

## 관련 문서

| 단계 | 문서명 |
|---|---|
| Phase 1 | [도구 인터페이스 및 스키마 명세](docs/phase1-tool-schema-blueprint.md) |
| Phase 2 | [로컬 서버 아키텍처](docs/phase2-server-architecture.md) |
| Phase 3 | [AnythingLLM 에이전트 시스템 프롬프트](docs/phase3-anythingllm-agent-prompt.md) |
| Phase 4 | [테스트 및 트러블슈팅 매트릭스](docs/phase4-testing-matrix.md) |
| 가이드 | [폐쇄망 반입 및 배포 매뉴얼](docs/deployment-airgap-manual.md) |

---

## 폐쇄망 반입용 패키징

```bash
python tools/make_package.py
```

위 명령을 실행하면 `dist/planning-mcp-<버전>-<날짜>.zip` 파일(약 90 KB, 순수 텍스트 파일 구성)과 파일별 SHA-256 해시 목록이 기록된 `MANIFEST.txt`가 생성됩니다.

대상 시스템에 Python이 설치되어 있지 않은 환경을 지원하려면, python.org의 공식 임베디드 배포판 zip 파일을 함께 패키징할 수 있습니다. 원본 파일이 변조 없이 그대로 포함되므로 python.org 공식 체크섬과 대조하여 보안 검증을 수행할 수 있습니다.

```bash
python tools/make_package.py --with-python C:\dl\python-3.12.10-embed-amd64.zip
```

반입 대상 시스템에서는 다음과 같이 설치 검증을 진행합니다 (임베디드 Python이 동봉된 경우 먼저 `python tools/setup_runtime.py`로 인터프리터 압축을 해제합니다).

```bash
python tools/verify_install.py
```

이 검증 스크립트는 Python 버전, 파일 무결성, 순수 표준 라이브러리 준수 여부, 단위 테스트 스위트, stdio 스모크 테스트를 순차적으로 검증한 후 최종 **GO / NO-GO** 결과를 판정합니다. 배포에 관한 전체 절차는 [docs/deployment-airgap-manual.md](docs/deployment-airgap-manual.md)에서 확인하실 수 있습니다.
