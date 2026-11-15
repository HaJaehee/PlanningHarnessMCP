# planning-mcp (PlanningHarnessMCP)

**버전: 3.2.0** · MCP 서버 이름: `planning-mcp` · 버전 단일 출처: `planning/config.py`의 `SERVER_VERSION` (MCP `initialize` 응답의 `serverInfo.version`으로 보고됩니다)

AnythingLLM Agent Mode를 위한 경량 **계획·작업 관리 MCP 서버입니다**. 폐쇄망 환경에서 성능이 제한적인 사내 LLM이 기억에만 의존하여 즉각 답변하는 대신, `계획 → 사람 승인 → 실행 → 보고`의 생애주기를 준수하도록 하네스(Harness)를 제공합니다.

**외부 의존성이 없습니다 (0개).** Python 3.9 이상 표준 라이브러리만 사용하므로 `pip install`이 필요하지 않습니다. 외부 패키지 저장소에 접근할 수 없는 폐쇄망 환경에서 매우 유용합니다.

> **예외 한 가지 (3.3, 미배포):** `ADVANCED_WORKFLOW_PLANNING=1`로 워크플로우 다이어그램 기능을 활성화하면 승인 페이지가 브라우저에서 mermaid.js(MIT)를 사용합니다. 이 에셋은 `vendor/` 디렉터리에 버전과 SHA-256 해시를 고정하여 함께 패키징하며, 추가로 설치해야 하는 Python 패키지는 여전히 없습니다(`requirements.txt` 없음). 기능을 비활성화하면(기본값) 해당 파일을 읽거나 브라우저로 전송하지 않습니다.

---

## 개발 배경

사내 모델은 주로 OpenAI 호환 API를 통해서만 접근할 수 있으며, 다단계 추론이나 정교한 도구 호출 능력에는 한계가 있습니다. 이를 별도 제어 없이 사용할 경우, 계획 수립과 사용자 승인이 선행되어야 할 작업 요청에도 즉각 자의적으로 답변해 버리는 문제가 발생합니다. 본 서버는 이러한 문제를 다음과 같이 구조적으로 방지합니다.

- **상태는 서버가 관리합니다.** 모델이 계획을 자체적으로 기억할 필요가 없으므로 임의로 계획을 날조하거나 왜곡할 수 없습니다.
- **모든 응답에 `next_action`이 포함됩니다.** 모델 스스로 다음 행동을 추론하여 나아가길 기대하는 대신, 서버가 모델을 상태 머신처럼 직접 구동합니다.
- **승인 게이트는 권고가 아닌 필수 강제 사항입니다.** 사용자가 실제로 승인하기 전까지는 `update_task_progress`의 진행 기록 처리가 거부되므로, 지시를 무시하는 모델이라도 작업을 강행할 수 없습니다.
- **에이전트 루프를 물리적으로 중단시킵니다.** 승인 요청은 서버가 직접 게시합니다(3.1.0). 모델이 최종 태스크 목록을 보내는 호출과 마지막 태스크의 `DONE` 호출이 곧 승인 요청이며, 그 호출은 사용자가 결정을 내릴 때까지 반환이 보류됩니다. 모델이 승인 요청을 누락할 수 있는 단계 자체가 구조적으로 존재하지 않습니다. 에이전트 루프는 도구 실행 결과를 **동기적으로** 대기하므로, 모델은 그동안 다른 도구를 호출할 수 없습니다. 단순히 "멈추라고 지시하는" 방식이 아니라 물리적으로 대기 상태를 유지합니다.
- **실행 중에도 사용자가 개입할 수 있습니다(3.1.0).** 승인 페이지는 실행 중인 계획을 태스크별로 보여줍니다. 사용자는 다음 태스크로 넘어가기 전에 실행을 멈추거나, 남은 태스크에 대한 의견을 전달할 수 있습니다. 호스트에 도구 실행 승인 기능이 없더라도, 계획 승인과 완료 확인 사이에 사용자가 직접 개입할 수 있습니다.
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
| `plan_and_think` | 새 요청의 진입점입니다. 1회 호출당 1단계의 추론을 수행하며, 마지막 호출에서 `task_list`를 제출합니다(`reasoning` 프로필에서는 한 번의 호출로 기록). 특정 작업 수정 요청 시에는 `task_updates`를 전달하고, 사용자가 목표 자체를 수정한 경우에는 `revised_goal`을 사용합니다. 사고 단계에는 상한이 있으며, 상한에 도달하면 서버가 마지막 초안을 사용자에게 제출합니다. 확정된 계획은 사용자의 수정 요청 없이는 다시 열리지 않습니다. 사용자의 선호에 따라 방법이 갈리는 태스크는 `alternatives`로 대안을 함께 제시할 수 있으며, `task_list` 쪽이 권장안입니다(2.0.0). 결과를 확인할 수 있는 태스크에는 `done_when`으로 완료 기준(작업 완료 시 충족되어야 할 조건이나 산출물)을 지정할 수 있고, 사용자는 이를 계획과 함께 승인합니다. 태스크가 실패하면 서버가 해당 태스크를 지정하며, 모델은 `task_updates`로 해당 태스크만 다른 방식으로 다시 작성합니다. 이미 끝난 태스크의 결과는 유지됩니다(3.0.0). 최종 `task_list`를 전송하는 호출이 곧 승인 요청이 됩니다. 서버가 계획을 승인 페이지에 게시하고, 사용자의 승인 결정이 해당 호출의 응답으로 반환됩니다(3.1.0). |
| `request_user_approval` | HITL(Human-In-The-Loop) 승인 게이트입니다. 3.1.0부터 승인 요청은 서버가 직접 게시하므로, 이 도구는 사용자가 아직 결정하지 않았다는 응답(`APPROVAL_PENDING`)을 받았을 때 `decision="ASK_USER"`로 대기를 이어가는 용도로만 호출됩니다. `plan_summary`는 전달할 필요가 없습니다. 승인 페이지가 없는 채팅 모드에서는 사용자의 답변을 전달하는 데 사용합니다. `PLANNING_MCP_AUTO_ASK=false`로 설정하면 3.0 방식처럼 모델이 계획 확정 후 및 마지막 태스크 완료 후에 직접 호출합니다. 승인 페이지 상단에는 서버 버전이 작게 표시되며, 옆의 `i` 아이콘을 클릭하면 작성자·연락처·버전 정보가 표시됩니다. `ASK_USER` 요청 후 대기하며, 이후 사용자의 `APPROVED` / `REJECTED` / `REVISE` 결정을 반영합니다. 대안이 제시된 태스크는 사용자가 승인 페이지에서 선택한 안이 그대로 태스크 내용으로 확정됩니다(채팅 모드에서는 모델이 `choices`로 전달). 사용자는 승인 페이지에서 태스크의 완료 기준을 직접 추가하거나 수정한 뒤, 별도 수정 요청 단계를 거치지 않고 그대로 승인할 수 있습니다(3.0.0). |
| `update_task_progress` | 작업 진행 상태를 갱신합니다. 최초 작업만 `IN_PROGRESS`로 설정하고, 이후 작업은 작업당 `DONE` 또는 `FAILED`를 1회씩 보고합니다 (다음 작업은 서버가 자동으로 시작합니다). 사용자 미승인 상태에서는 호출이 거부됩니다. 완료 기준이 있는 태스크는 `result_log`가 기준 문장을 그대로 반복할 경우 반려됩니다. `PLANNING_MCP_ARTIFACT_ROOTS`가 설정된 서버에서는 태스크가 생성하거나 수정한 파일을 `files`로 보고하며, 서버가 허용된 폴더 내에 파일이 실제로 존재하는지 확인합니다. 파일이 존재하지 않으면 `DONE` 보고가 반려됩니다(3.0.0). 마지막 태스크의 `DONE`은 곧 완료 확인 요청이 되며, 사용자의 최종 확인 결과가 해당 호출의 응답으로 반환됩니다. 사용자가 승인 페이지에서 멈춤이나 의견 전달을 미리 요청해 두었다면, 이 호출이 결과를 기록한 뒤 다음 태스크를 시작하지 않고 해당 요청을 즉시 적용합니다(3.1.0). |
| `get_current_plan` | 컨텍스트가 단절되었을 때 계획 상태를 복구합니다. 언제든 안전하게 호출할 수 있으며, 현재 세션의 `plan_id`를 전달하면 해당 계획 정보를 안정적으로 조회할 수 있습니다. |

상세 스키마 및 응답 명세는 [docs/phase1-tool-schema-blueprint.md](docs/phase1-tool-schema-blueprint.md)에서 확인하실 수 있습니다.

모든 도구의 응답에는 `ok`, `plan_status`, `next_action`, `next_action_hint` 필드가 항상 포함됩니다.

---

## 승인 페이지와 실행 중 개입 (3.1.0)

승인 요청은 서버가 직접 게시합니다. 모델이 최종 태스크 목록을 보낸 호출과 마지막 태스크의 `DONE` 호출이 곧 승인 요청이 되며, 사용자의 결정이 해당 호출의 응답으로 반환됩니다. 모델이 `request_user_approval`을 호출하는 것은 사용자가 아직 결정하지 않았다는 응답(`APPROVAL_PENDING`)을 받았을 때뿐입니다.

승인 페이지(`http://127.0.0.1:8765/`)에는 네 가지 카드가 표시됩니다.

| 카드 | 표시되는 때 | 사용자가 할 수 있는 일 |
|---|---|---|
| 계획 승인 요청 | 모델이 최종 태스크 목록을 보냈을 때 | 승인, 수정 요청(전체 또는 태스크별), 거절, 대안 선택, 완료 기준 작성 |
| 실행 중 | 계획이 승인된 뒤부터 마지막 태스크가 끝날 때까지 | 끝난 태스크의 결과 확인, **멈춤**, **의견 전달**, **계획 강제 취소**(3.2.0) |
| 실행 멈춤 | 멈춤을 요청한 뒤 에이전트가 진행 중인 태스크를 보고했을 때 | 계속 진행(진행 방향 지시 사항 기재 가능), 계획 취소 |
| 완료 확인 | 마지막 태스크가 끝났을 때 | 승인, 태스크별 다시 작업, 계획 다시 세우기, 거절 |

멈춤과 의견 전달은 에이전트가 진행 중인 태스크를 보고하는 시점에 적용됩니다.

- **멈춤**: 보고된 태스크의 결과는 기록되고, 다음 태스크는 시작되지 않습니다. 사용자가 계속 진행을 선택하면 일시 중단했던 다음 태스크부터 진행이 재개됩니다.
- **의견 전달**: 에이전트가 남은 태스크를 전달받은 의견에 맞게 다시 작성하고, 사용자가 해당 변경 사항을 승인한 뒤에 실행이 재개됩니다. 이미 완료된 태스크의 결과는 유지됩니다.
- 이미 실행 중인 도구는 서버가 중단할 수 없습니다.
- 아직 적용되지 않은 요청은 [요청 취소]로 되돌릴 수 있으며, 입력했던 내용은 입력란에 그대로 복원됩니다.
- 마지막 태스크가 완료된 뒤에 도착한 요청은 적용 대상이 없으므로, 입력한 내용이 완료 확인 카드에 표시됩니다.
- **계획 강제 취소**(3.2.0): 에이전트가 오류로 중단되어 태스크를 보고하지 못하면, 멈춤이나 의견 전달 요청이 적용되는 시점이 영구히 오지 않을 수 있습니다. 이때 실행 중 카드의 [계획 강제 취소]를 누르면, 확인 대화상자를 거쳐 해당 카드가 즉시 화면에서 사라집니다. 해당 계획은 다음 도구 호출 시점(어느 세션에서 발생하든)에 `CANCELLED` 상태로 최종 종료됩니다. 이미 완료된 태스크의 결과는 기록에 보존되며, 에이전트가 뒤늦게 다시 보고하더라도 `PLAN_CANCELLED` 오류 응답을 받게 됩니다. 이 작업은 취소하거나 되돌릴 수 없습니다.

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
  approval.py             승인 대기열, 실행 중 계획 보드, 승인 페이지 서버
  web/                    승인 페이지 원본: page.html, page.css, 스크립트 9개 (서버가 단일 응답으로 인라인 조립)
state/                    런타임 데이터: plan_state.json, audit.jsonl, approval.json, runs.json (.gitignore 대상)
tests/                    단위 테스트 스위트 및 stdio 종단 간 스모크 테스트
tools/                    패키징·설치 검증 도구, loop_report.py (감사 로그 루프 분석), page_preview.py (승인 페이지 미리보기)
package_source.ps1        tools/make_package.py 실행 래퍼 (Windows)
agents.md                 에이전트 시스템 프롬프트 원본 (AnythingLLM에 붙여 넣는 파일)
LICENSE.md                라이선스 (MIT)
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
| `PLANNING_MCP_EVICT_LRU` | `true` | 3.0.0. 활성 계획 수가 한도에 도달했을 때 새 계획을 거절하는 대신, 가장 오랫동안 사용되지 않은(LRU) 미완료 계획을 자동으로 정리하여 공간을 확보합니다. 삭제된 계획의 태스크와 증거는 `audit.jsonl`의 `plan_evicted` 항목에 보존되며, 해당 세션의 대화가 다시 인입되면 계획이 정리되었다는 안내(`PLAN_EVICTED`)를 받습니다. `false`이면 이전처럼 새 계획을 거절합니다. |
| `PLANNING_MCP_EVICT_MIN_IDLE` | `300` | 마지막으로 갱신된 지 이 시간(초)이 지나지 않은 계획은 사용 중인 것으로 간주하여 정리 대상에서 제외합니다. 모든 활성 계획이 사용 중이면 새 계획은 이전처럼 거절됩니다. |
| `PLANNING_MCP_COMPLETION_APPROVAL` | `true` | 마지막 작업이 완료(DONE)되어도 즉시 종료되지 않고, 사용자가 각 작업별 수행 결과를 최종 확인해야 전체 계획을 완료(COMPLETED) 처리합니다. |
| `PLANNING_MCP_MIN_RESULT_LOG` | `8` | 작업 완료(DONE) 보고 시 요구되는 최소 증빙 내용의 길이(공백 및 문장부호 제외)입니다. "완료", "done" 등 단순 상투어구는 길이와 관계없이 반려됩니다. |
| `PLANNING_MCP_AUTO_ADVANCE` | `true` | 작업 완료(DONE) 보고 시 서버가 다음 작업을 `IN_PROGRESS` 상태로 자동 시작합니다. 이를 통해 5개 작업 기준 실행 단계의 호출 횟수를 10회에서 6회로 단축합니다. `false`로 설정하면 매 작업마다 모델이 `IN_PROGRESS`를 직접 호출해야 합니다. |
| `PLANNING_MCP_MODEL_PROFILE` | `standard` | `reasoning`으로 설정하면 CoT(thinking) 모델용 도구 설명을 사용합니다. `plan_and_think`를 한 번의 호출로 계획을 기록하는 도구로 안내하고, 단계 번호 관련 필드를 노출하지 않습니다. [docs/thinking-model-hosts.md](docs/thinking-model-hosts.md) 참조. |
| `PLANNING_MCP_MAX_THINKING_STEPS` | `0` | 한 계획 라운드의 사고 단계 상한입니다. `0`이면 프로필 기본값(standard 8, reasoning 2), 음수면 무제한입니다. 상한에 도달하면 서버가 마지막 초안을 사용자에게 제출합니다. |
| `PLANNING_MCP_LOOP_BREAKER` | `true` | 서킷 브레이커입니다. 같은 단계가 반복되면 계획을 일시 정지하고 승인 페이지에 반복 감지 카드를 표시합니다. |
| `PLANNING_MCP_BREAKER_CALLS` | `12` | 진척(확정, 사람의 결정, 태스크 DONE/FAILED) 없이 허용되는 호출 수입니다. |
| `PLANNING_MCP_BREAKER_REPEAT` | `3` | 같은 인자의 같은 호출이 연속으로 허용되는 횟수입니다. |
| `PLANNING_MCP_BREAKER_ERROR_STREAK` | `4` | 같은 오류 코드(또는 거절된 재계획)가 연속으로 허용되는 횟수입니다. |
| `PLANNING_MCP_BREAKER_RESPAWN` | `3` | 목표 문장만 변경하여 다시 시작한 계획이 이 횟수에 도달하면 정지합니다. |
| `PLANNING_MCP_REPLAN_COOLDOWN` | `600` | 방금 완료된 목표를 이 시간(초) 이내에 다시 계획하려고 시도하면, 새 계획을 생성하지 않고 기존 완료 결과로 답변하도록 안내합니다. `0`으로 설정하면 이 기능을 비활성화합니다. |
| `PLANNING_MCP_ALTERNATIVES` | `true` | 2.0.0. 모델이 태스크별 대안(`alternatives`)과 권장 이유(`recommended_reasons`)를 제시하고, 사용자가 승인 페이지에서 고를 수 있게 합니다. `false`(또는 `off`)이면 도구 스키마에 필드를 노출하지 않으며, 모델이 전달하더라도 무시합니다. |
| `PLANNING_MCP_MAX_ALTERNATIVES` | `3` | 태스크당 허용되는 대안 수입니다(권장안 포함 선택지 2~4개). |
| `PLANNING_MCP_MAX_CHOICE_POINTS` | `3` | 계획당 선택지를 둘 수 있는 태스크 수입니다. |
| `PLANNING_MCP_DONE_WHEN` | `true` | 3.0.0. 모델이 태스크별 완료 기준(`done_when`)을 제시할 수 있게 합니다. `false`(또는 `off`)이면 도구 스키마에 필드를 노출하지 않으며, 모델이 전달하더라도 무시합니다. 이 경우에도 사용자는 승인 페이지에서 기준을 직접 작성할 수 있습니다. |
| `PLANNING_MCP_MAX_DONE_WHEN_CHARS` | `200` | 완료 기준 1건의 최대 글자 수입니다. 이를 초과하면 자동으로 잘라냅니다. |
| `PLANNING_MCP_EVIDENCE_NOVELTY` | `0.3` | 완료 기준이 있는 태스크에서, `result_log`에 추가된 고유 내용의 비율이 이 값보다 낮으면 완료 기준 문장을 단순 반복한 것으로 간주하여 `DONE` 보고를 반려합니다. `0`으로 설정하면 이 검사를 비활성화합니다. |
| `PLANNING_MCP_ARTIFACT_ROOTS` | (비어 있음) | 3.0.0. 서버가 파일의 실재 여부를 검증할 수 있는 허용 폴더 목록입니다(`;`로 구분). 비어 있으면 파일 존재를 확인하지 않고 도구 스키마에 `files` 필드도 노출하지 않습니다. 지정된 폴더 외부의 경로는 검증하지 않으며 반려하지도 않습니다. 에이전트 도구가 산출물을 저장하는 폴더를 지정하십시오. |
| `PLANNING_MCP_LOCAL_REPAIR` | `true` | 3.0.0. 태스크가 실패하면 해당 태스크만 국소적으로 다시 계획합니다. `false`이면 2.0 방식처럼 계획 전체를 처음부터 다시 수립합니다. |
| `PLANNING_MCP_AUTO_ASK` | `true` | 3.1.0. 승인 요청을 서버가 직접 게시합니다. 모델이 최종 `task_list`를 전송하는 호출과 마지막 태스크의 `DONE` 호출이 곧 승인 요청이 되므로, 모델이 `request_user_approval`을 따로 호출해야 하는 단계가 없어집니다. `false`이면 3.0 방식처럼 모델이 `request_user_approval(ASK_USER)`를 `plan_summary`와 함께 호출합니다. |
| `ADVANCED_WORKFLOW_PLANNING` | `0` | 3.3(미배포). `1`로 설정하면 에이전트가 계획과 함께 Mermaid 순서도(`workflow`)를 전송하고, 승인 페이지에서 이를 다이어그램으로 시각화합니다. 노드를 클릭하여 검토 의견을 남기면 [승인] 버튼이 [수정 요청(워크플로우)]로 전환됩니다. 에이전트가 작성한 Mermaid 코드는 서버의 문법 가드(`planning/workflow.py`)를 통해 검증 및 정규화된 후에만 페이지로 전달됩니다. `0`(기본값)이면 기존 3.2와 동일하게 동작합니다. 별칭 `PLANNING_MCP_ADVANCED_WORKFLOW`. |
| `PLANNING_MCP_RUN_CONTROL` | `true` | 3.1.0. 실행 중인 계획을 승인 페이지에 표시하고, 사용자가 멈춤과 의견 전달을 요청할 수 있게 합니다. 요청은 에이전트가 진행 중인 태스크를 보고하는 시점에 적용됩니다. 계획 강제 취소(3.2.0)는 에이전트의 보고를 기다리지 않고 다음 도구 호출 시 즉시 적용됩니다. `false`이면 실행 중에는 페이지에 아무것도 표시하지 않습니다. |
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

3.1.0부터는 다음 이벤트도 기록됩니다. `approval_requested`와 `completion_verification_requested`의 `by_server`(서버가 직접 게시한 요청), `run_paused`(사용자의 멈춤 요청), `run_note_applied`와 `tasks_steered`(실행 중 의견 전달 및 그에 따른 태스크 수정), `run_control_moot`(적용 대상이 소멸된 요청). 3.2.0부터는 `run_cancelled`(사용자의 계획 강제 취소)도 기록됩니다.

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

---

## 라이선스

[MIT License](LICENSE.md). `--with-python`으로 동봉하는 Python 런타임은 이 라이선스의 대상이 아니며, 런타임 zip 안의 `LICENSE.txt`를 따릅니다.
