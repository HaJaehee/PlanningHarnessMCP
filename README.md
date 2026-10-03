# planning-mcp (PlanningHarnessMCP)

**버전: 3.0.0** · MCP 서버 이름: `planning-mcp` · 버전 단일 출처: `planning/config.py`의 `SERVER_VERSION` (MCP `initialize` 응답의 `serverInfo.version`으로 보고됩니다)

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

마지막으로 [agents.md](agents.md)의 내용을 워크스페이스 에이전트 설정의 시스템 프롬프트에 입력하고, **temperature를 0.3 이하**로 설정합니다. 단, CoT(thinking) 모델은 예외입니다. 낮은 temperature는 thinking 모델의 끝없는 반복을 유발할 수 있으므로 모델 카드의 권장값을 따르고, `PLANNING_MCP_MODEL_PROFILE=reasoning`을 설정해 주십시오([docs/thinking-model-hosts.md](docs/thinking-model-hosts.md) 참조). 초기 안정화 기간에는 다른 기본 에이전트 스킬을 비활성화하는 것을 권장합니다. 노출되는 도구 수가 적을수록 모델의 잘못된 도구 호출 확률이 대폭 감소합니다.

---

## 시스템 프롬프트

에이전트 시스템 프롬프트는 [agents.md](agents.md) 파일입니다. 이 문서에는 사본을 두지 않습니다.

- `agents.md`의 내용 전체를 AnythingLLM 워크스페이스의 에이전트 시스템 프롬프트에 그대로 붙여 넣으십시오.
- 서버 버전이 바뀌면 `agents.md`도 함께 바뀔 수 있습니다. 서버를 업그레이드한 뒤에는 프롬프트를 다시 붙여 넣어야 합니다.
- 한국어 번역본과 AnythingLLM 적용 시 주의사항은 [docs/phase3-anythingllm-agent-prompt.md](docs/phase3-anythingllm-agent-prompt.md)에 있습니다.

---

## 제공 도구 (4종)

| 도구 | 역할 |
|---|---|
| `plan_and_think` | 새 요청의 진입점입니다. 1회 호출당 1단계의 추론을 수행하며, 마지막 호출에서 `task_list`를 제출합니다(`reasoning` 프로필에서는 한 번의 호출로 기록). 특정 작업 수정 요청 시에는 `task_updates`를 전달하고, 사용자가 목표 자체를 수정한 경우에는 `revised_goal`을 사용합니다. 사고 단계에는 상한이 있으며, 상한에 도달하면 서버가 마지막 초안을 사용자에게 제출합니다. 확정된 계획은 사용자의 수정 요청 없이는 다시 열리지 않습니다. 사용자의 선호에 따라 방법이 갈리는 태스크는 `alternatives`로 대안을 함께 제시할 수 있으며, `task_list` 쪽이 권장안입니다(2.0.0). 결과를 확인할 수 있는 태스크에는 `done_when`으로 완료 기준(끝났을 때 무엇이 있거나 참이 되는지)을 붙일 수 있고, 사용자는 이를 계획과 함께 승인합니다. 태스크가 실패하면 서버가 그 태스크를 지목하며, 모델은 `task_updates`로 그 태스크만 다른 방법으로 다시 씁니다. 이미 끝난 태스크의 결과는 유지됩니다(3.0.0). |
| `request_user_approval` | HITL(Human-In-The-Loop) 승인 게이트입니다. 승인 페이지 위쪽에는 서버 버전이 작게 표시되고, 옆의 `i` 아이콘을 누르면 작성자·연락처·버전이 나옵니다. `ASK_USER` 요청 후 대기하며, 이후 `APPROVED` / `REJECTED` / `REVISE` 결과를 반영합니다. 대안이 있는 태스크는 사용자가 승인 페이지에서 고른 안이 그대로 태스크가 됩니다(채팅 모드에서는 모델이 `choices`로 전달). 사용자는 승인 페이지에서 태스크의 완료 기준을 직접 추가하거나 고친 뒤 그대로 승인할 수 있습니다. 수정 요청을 거치지 않습니다(3.0.0). |
| `update_task_progress` | 작업 진행 상태를 갱신합니다. 최초 작업만 `IN_PROGRESS`로 설정하고, 이후 작업은 작업당 `DONE` 또는 `FAILED`를 1회씩 보고합니다 (다음 작업은 서버가 자동으로 시작합니다). 사용자 미승인 상태에서는 호출이 거부됩니다. 완료 기준이 있는 태스크는 `result_log`가 기준 문장을 그대로 반복하면 반려됩니다. `PLANNING_MCP_ARTIFACT_ROOTS`가 설정된 서버에서는 태스크가 만들거나 바꾼 파일을 `files`로 보고하며, 서버가 허용된 폴더 안에서 파일이 실제로 있는지 확인합니다. 없으면 `DONE`이 반려됩니다(3.0.0). |
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
  loopguard.py            서킷 브레이커 카운터 (1.16.0)
  choices.py              태스크별 대안 검증 (2.0.0)
  evidence.py             완료 기준 검증, 증거 반복 검사, 허용 폴더 안 파일 확인 (3.0.0)
state/                    런타임 데이터: plan_state.json, audit.jsonl (.gitignore 대상)
tests/                    단위 테스트 스위트 및 stdio 종단 간 스모크 테스트
tools/                    패키징·설치 검증 도구, loop_report.py (감사 로그 루프 분석)
package_source.ps1        tools/make_package.py 실행 래퍼 (Windows)
agents.md                 에이전트 시스템 프롬프트 원본 (AnythingLLM에 붙여 넣는 파일)
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
| `PLANNING_MCP_MAX_ACTIVE_PLANS` | `20` | 동시에 활성화(진행)할 수 있는 계획의 최대 허용 수입니다 (1.16.0부터 20, 이전 5). |
| `PLANNING_MCP_EVICT_LRU` | `true` | 3.0.0. 활성 계획 수가 한도에 도달했을 때 새 계획을 거절하지 않고, 가장 오래 쓰이지 않은(LRU) 미완료 계획을 자동으로 삭제해 자리를 만듭니다. 삭제된 계획의 태스크와 증거는 `audit.jsonl`의 `plan_evicted` 항목에 남고, 해당 대화가 다시 돌아오면 계획이 정리되었다는 안내(`PLAN_EVICTED`)를 받습니다. `false`이면 이전처럼 새 계획을 거절합니다. |
| `PLANNING_MCP_EVICT_MIN_IDLE` | `300` | 마지막으로 갱신된 지 이 시간(초)이 지나지 않은 계획은 사용 중으로 보고 삭제하지 않습니다. 모든 활성 계획이 사용 중이면 새 계획은 이전처럼 거절됩니다. |
| `PLANNING_MCP_COMPLETION_APPROVAL` | `true` | 마지막 작업이 완료(DONE)되어도 즉시 종료되지 않고, 사용자가 각 작업별 수행 결과를 최종 확인해야 전체 계획을 완료(COMPLETED) 처리합니다. |
| `PLANNING_MCP_MIN_RESULT_LOG` | `8` | 작업 완료(DONE) 보고 시 요구되는 최소 증빙 내용의 길이(공백 및 문장부호 제외)입니다. "완료", "done" 등 단순 상투어구는 길이와 관계없이 반려됩니다. |
| `PLANNING_MCP_AUTO_ADVANCE` | `true` | 작업 완료(DONE) 보고 시 서버가 다음 작업을 `IN_PROGRESS` 상태로 자동 시작합니다. 이를 통해 5개 작업 기준 실행 단계의 호출 횟수를 10회에서 6회로 단축합니다. `false`로 설정하면 매 작업마다 모델이 `IN_PROGRESS`를 직접 호출해야 합니다. |
| `PLANNING_MCP_MODEL_PROFILE` | `standard` | `reasoning`으로 설정하면 CoT(thinking) 모델용 도구 설명을 사용합니다. `plan_and_think`를 한 번의 호출로 계획을 기록하는 도구로 안내하고, 단계 번호 관련 필드를 노출하지 않습니다. [docs/thinking-model-hosts.md](docs/thinking-model-hosts.md) 참조. |
| `PLANNING_MCP_MAX_THINKING_STEPS` | `0` | 한 계획 라운드의 사고 단계 상한입니다. `0`이면 프로필 기본값(standard 8, reasoning 2), 음수면 무제한입니다. 상한에 도달하면 서버가 마지막 초안을 사용자에게 제출합니다. |
| `PLANNING_MCP_LOOP_BREAKER` | `true` | 서킷 브레이커입니다. 같은 단계가 반복되면 계획을 일시 정지하고 승인 페이지에 반복 감지 카드를 띄웁니다. |
| `PLANNING_MCP_BREAKER_CALLS` | `12` | 진척(확정, 사람의 결정, 태스크 DONE/FAILED) 없이 허용되는 호출 수입니다. |
| `PLANNING_MCP_BREAKER_REPEAT` | `3` | 같은 인자의 같은 호출이 연속으로 허용되는 횟수입니다. |
| `PLANNING_MCP_BREAKER_ERROR_STREAK` | `4` | 같은 오류 코드(또는 거절된 재계획)가 연속으로 허용되는 횟수입니다. |
| `PLANNING_MCP_BREAKER_RESPAWN` | `3` | 목표 문장만 바꿔 다시 시작한 계획이 이 개수에 이르면 정지합니다. |
| `PLANNING_MCP_REPLAN_COOLDOWN` | `600` | 방금 완료된 목표를 이 시간(초) 안에 다시 계획하려 하면, 새 계획을 만들지 않고 완료 결과로 답하도록 안내합니다. `0`이면 끕니다. |
| `PLANNING_MCP_ALTERNATIVES` | `true` | 2.0.0. 모델이 태스크별 대안(`alternatives`)과 권장 이유(`recommended_reasons`)를 제시하고, 사용자가 승인 페이지에서 고를 수 있게 합니다. `false`(또는 `off`)이면 필드를 광고하지 않고, 보내 와도 무시합니다. |
| `PLANNING_MCP_MAX_ALTERNATIVES` | `3` | 태스크당 허용되는 대안 수입니다(권장안 포함 선택지 2~4개). |
| `PLANNING_MCP_MAX_CHOICE_POINTS` | `3` | 계획당 선택지를 둘 수 있는 태스크 수입니다. |
| `PLANNING_MCP_DONE_WHEN` | `true` | 3.0.0. 모델이 태스크별 완료 기준(`done_when`)을 제시할 수 있게 합니다. `false`(또는 `off`)이면 필드를 광고하지 않고, 보내 와도 무시합니다. 이때에도 사용자는 승인 페이지에서 기준을 직접 적을 수 있습니다. |
| `PLANNING_MCP_MAX_DONE_WHEN_CHARS` | `200` | 완료 기준 한 건의 최대 길이입니다. 넘으면 잘라냅니다. |
| `PLANNING_MCP_EVIDENCE_NOVELTY` | `0.3` | 완료 기준이 있는 태스크에서, `result_log`가 기준 문장에 더한 새 내용의 비율이 이 값보다 낮으면 기준을 반복했을 뿐인 것으로 보고 `DONE`을 반려합니다. `0`이면 이 검사를 끕니다. |
| `PLANNING_MCP_ARTIFACT_ROOTS` | (비어 있음) | 3.0.0. 서버가 파일 존재를 확인해도 되는 폴더 목록입니다(`;`로 구분). 비어 있으면 파일을 확인하지 않고 `files` 필드도 광고하지 않습니다. 이 폴더 밖의 경로는 확인하지도, 반려하지도 않습니다. 에이전트의 도구가 결과물을 저장하는 폴더를 지정하십시오. |
| `PLANNING_MCP_LOCAL_REPAIR` | `true` | 3.0.0. 태스크가 실패하면 그 태스크만 다시 계획합니다. `false`이면 2.0처럼 계획 전체를 다시 세웁니다. |
| `PLANNING_MCP_AUTOAPPROVE` | `false` | **테스트 전용 옵션**입니다. HITL 승인 게이트를 건너뜁니다. 호출 시마다 경고 로그가 기록됩니다. |

CLI 옵션 지원: `--transport stdio|sse`, `--host`, `--port`, `--state-dir`, `--log-level`

---

## 에이전트 수행 내역 확인

`state/plan_state.json` 파일은 사람이 직접 읽을 수 있는 JSON 형식으로 저장됩니다. 파일을 열어 보면 에이전트가 현재 인식하고 있는 진행 상태와 계획을 그대로 확인할 수 있습니다. `state/audit.jsonl`은 줄마다 하나의 JSON 객체가 기록되는 추가 전용(Append-only) 감사 로그입니다.

```
plan_created → thinking_step → execution_blocked → plan_finalized →
approval_requested → approved → task_started → task_done → task_failed
```

1.16.0부터는 `client_connected`(접속한 호스트), `thinking_step`의 `gap_sec`·`reconsider`, `loop_halted`·`halt_resolved`, `auto_finalized`, `replan_redirected` 이벤트도 기록됩니다. `python tools/loop_report.py`를 실행하면 계획별로 반복 징후를 요약해 줍니다.

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
| 가이드 | [CoT(thinking) 모델 + Zed / Goose 운용 가이드](docs/thinking-model-hosts.md) |
| 계획 | [2.0 계획: 태스크별 대안 선택](docs/plan-2.0-task-alternatives.md) (구현 완료) |

---

## 폐쇄망 반입용 패키징

```bash
python tools/make_package.py
```

Windows에서는 저장소 루트의 `package_source.ps1`로 같은 작업을 실행할 수 있습니다. Python 실행 파일을 찾아 `tools/make_package.py`를 실행하고, 인자를 그대로 전달합니다.

```powershell
.\package_source.ps1
.\package_source.ps1 --with-python C:\dl\python-3.12.10-embed-amd64.zip
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
