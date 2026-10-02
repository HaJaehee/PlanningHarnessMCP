# CoT(thinking) 모델 + Zed / Goose 운용 가이드

> 대상: planning-mcp **1.16.0 이상**. 1.16.0은 "에이전트가 자기 검증(`wait, let me reconsider`)에 빠져
> 계획 단계에서 빠져나오지 못하는" 현장 문제(wiki D25)를 해결하기 위한 버전입니다.
> 서버 쪽에서 할 수 있는 일은 서버가 하고, **서버가 볼 수 없는 부분은 이 문서의 호스트 설정으로** 막습니다.

---

## 1. 폭주는 두 군데에서 일어납니다

| 위치 | 증상 | 서버가 볼 수 있나 | 1.16의 대응 |
|---|---|---|---|
| **호출과 호출 사이** | `plan_and_think`가 끝없이 반복됨 (`need_more_thinking=true`, `revises_step`, 같은 호출 재전송, 승인 대기 중 재계획, 완료 후 재계획) | 예 | 사고 예산 → 마지막 초안 자동 제출, 상태 가드, **서킷 브레이커**(승인 페이지 카드로 정지) |
| **한 번의 thinking 블록 안** | 도구 호출 없이 추론이 끝없이 이어짐 | 아니요 | 폭주를 부르던 지시문 충돌 제거 + **이 문서의 호스트 설정**(샘플링, 출력 길이 상한) |

블록 안 폭주는 서버가 끊을 수 없습니다. 그래서 두 가지로 대응합니다.
1. **유한하게 만들기**: 출력 길이 상한을 두면 끝없는 생성이 언젠가 잘립니다.
2. **잘린 뒤 이어받기**: 서버가 초안(`draft_tasks`)과 진행 상태를 보관하므로, 사용자가 "계속"이라고 한 마디만 하면 이어집니다.

---

## 2. 서버 설정 (MCP 서버 `env`)

| 환경 변수 | 기본값 | thinking 모델 권장 | 설명 |
|---|---|---|---|
| `PLANNING_MCP_MODEL_PROFILE` | `standard` | **`reasoning`** | 도구 설명을 "한 번의 호출로 계획을 기록하라"로 바꿉니다. `step_number`·`total_steps`·`revises_step`을 노출하지 않습니다. |
| `PLANNING_MCP_MAX_THINKING_STEPS` | `0` (프로필 기본값: standard 8, reasoning 2) | 그대로 | 한 계획 라운드의 사고 단계 상한. 다 쓰면 서버가 마지막 초안을 사용자에게 제출합니다. 음수는 무제한. |
| `PLANNING_MCP_LOOP_BREAKER` | `true` | 그대로 | 서킷 브레이커 켜기/끄기. |
| `PLANNING_MCP_BREAKER_CALLS` | `12` | 그대로 | 진척(확정, 사람의 결정, 태스크 DONE/FAILED) 없이 이어질 수 있는 호출 수. |
| `PLANNING_MCP_BREAKER_REPEAT` | `3` | 그대로 | 같은 인자의 같은 호출이 연속으로 허용되는 횟수. |
| `PLANNING_MCP_BREAKER_ERROR_STREAK` | `4` | 그대로 | 같은 오류 코드가 연속으로 허용되는 횟수. |
| `PLANNING_MCP_BREAKER_RESPAWN` | `3` | 그대로 | 목표를 조금씩 바꿔 다시 시작한 DRAFTING 계획이 이 개수에 이르면 정지. |
| `PLANNING_MCP_REPLAN_COOLDOWN` | `600` | 그대로 | 방금(초) 완료된 목표를 다시 계획하지 않고 결과로 답하게 합니다. `0`이면 끔. |

사람이 승인 대기 중이거나 실제로 승인을 기다린 호출은 브레이커가 **세지 않습니다**. 정상적인 대기를 루프로 오인하지 않기 위해서입니다.

---

## 3. 샘플링 — 낮은 temperature가 오히려 폭주를 부릅니다

README의 "temperature 0.3 이하" 권고는 **비추론(standard) 모델용**입니다. thinking 모델에는 적용하지 마십시오.

- **Qwen3 계열**: 모델 카드는 thinking 모드에서 `Temperature=0.6, TopP=0.95, TopK=20, MinP=0`을 권장하고,
  "DO NOT use greedy decoding"이라고 명시합니다. 그리디 디코딩은 성능 저하와 **끝없는 반복**을 일으킵니다.
  반복이 남으면 `presence_penalty`를 0~2 사이에서 올립니다(양자화 모델은 1.5 권장).
- **DeepSeek-R1 계열**: 모델 카드는 temperature 0.5~0.7(0.6 권장)을 권하며, 그 이유를 끝없는 반복과
  비일관적 출력 방지라고 밝힙니다.
- 그 밖의 모델은 **모델 카드의 권장값**을 따르십시오. 사내 엔드포인트가 temperature를 고정하고 있다면
  운영 측에 thinking 모델에 맞는 값인지 확인을 요청하십시오.

설정 위치:
- **Goose**: 환경 변수 `GOOSE_TEMPERATURE`
- **Zed / AnythingLLM**: 모델(제공자) 설정. 위치는 버전마다 다르므로 사용 중인 버전에서 확인하십시오.

## 4. 출력 길이 상한

호스트나 엔드포인트의 최대 출력 토큰(`max_tokens`)을 무제한으로 두지 마십시오. 상한이 있으면 블록 안
폭주도 결국 잘립니다. 잘린 다음에는 다음 순서로 이어집니다.

1. 사용자가 채팅에 "계속"이라고 입력합니다.
2. 모델이 `plan_and_think`나 `get_current_plan`을 호출합니다.
3. 서버가 보관해 둔 초안과 남은 사고 단계를 알려 줍니다.
4. 남은 단계가 없으면 서버가 초안을 그대로 사용자에게 제출합니다.

## 5. 플래너는 하나만

호스트에 자체 계획/할 일(todo)/thinking 도구가 있으면 끄십시오. 계획 도구가 둘이면 모델이 어느 쪽에
계획을 적을지부터 다시 따지게 됩니다.

- **Zed**: 에이전트 **프로필**에서 필요한 도구만 켭니다(`agent.profiles.<이름>.tools`, `enable_all_context_servers`).
- **Goose**: `GOOSE_MAX_TURNS`(사용자 입력 없이 허용되는 턴 수, 기본 1000)를 수십 단위로 낮추면 호스트
  쪽에서도 상한이 생깁니다.

---

## 6. 호스트별 등록 예시

경로는 예시입니다. 실제 설치 위치에 맞게 바꾸고, 반드시 절대 경로와 슬래시(`/`)를 쓰십시오.

### Zed — `settings.json`

```json
{
  "context_servers": {
    "planning": {
      "command": "D:/planning-mcp/runtime/python.exe",
      "args": ["-u", "D:/planning-mcp/server.py"],
      "env": {
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "PLANNING_MCP_MODEL_PROFILE": "reasoning"
      }
    }
  }
}
```

지시문은 [`agents.md`](../agents.md)의 내용을 프로젝트 지시 파일에 넣습니다. Zed는 `.rules`, `AGENTS.md`
등 지원 목록에서 처음 발견한 파일 하나를 프로젝트 지시로 사용하고, 전역 지시는
`%APPDATA%\Zed\AGENTS.md`(Windows)에서 읽습니다.

### Goose — `%APPDATA%\Block\goose\config\config.yaml`

```yaml
extensions:
  planning:
    name: planning
    type: stdio
    cmd: D:/planning-mcp/runtime/python.exe
    args: ["-u", "D:/planning-mcp/server.py"]
    enabled: true
    timeout: 120          # 승인 대기 1회분(45초)이 들어가도록 60 이상
    envs:
      PYTHONUTF8: "1"
      PYTHONUNBUFFERED: "1"
      PLANNING_MCP_MODEL_PROFILE: reasoning
```

지시문은 [`agents.md`](../agents.md)의 내용을 프로젝트의 `.goosehints` 또는 `AGENTS.md`에 넣습니다.

### AnythingLLM

[phase3-anythingllm-agent-prompt.md](phase3-anythingllm-agent-prompt.md)의 등록 예시 `env`에
`"PLANNING_MCP_MODEL_PROFILE": "reasoning"`을 추가합니다.

---

## 7. 사람이 보게 되는 것

| 상황 | 승인 페이지 |
|---|---|
| 사고 예산을 다 쓴 경우 | 일반 승인 카드. 개요에 "서버가 마지막 초안을 그대로 제출했습니다"와 에이전트의 마지막 생각이 표시됩니다. |
| 승인 대기 중에 에이전트가 다시 생각한 경우 | 같은 카드에 **에이전트 추가 의견**이 붙습니다. 계획은 바뀌지 않으며, 동의하면 수정 요청을 누르면 됩니다. |
| 브레이커가 발동한 경우 | **반복 감지** 카드: 멈춘 이유, 마지막 생각, 현재 초안, 버튼 `[이 초안으로 승인]` `[계속 진행 / 의견 전달 후 계속]` `[취소]`. 방향을 적고 계속하면 그 문장이 에이전트의 다음 지시 맨 앞에 붙습니다. |

## 8. 현장 기록 확인

재현 환경이 없으므로 현장 기록이 유일한 근거입니다. 서버가 `state/audit.jsonl`에 다음을 남깁니다.

- 접속한 클라이언트(`client`)
- 호출 사이 간격(`gap_sec`)
- thought 속 재검토 표현 수(`reconsider`)
- 정지(`loop_halted`)와 해제(`halt_resolved`)
- 자동 제출(`auto_finalized`)
- 재계획 차단(`replan_redirected`)

```bash
python tools/loop_report.py
```

- 호출 수는 적은데 간격이 긴 계획이 많다면 → 블록 안 폭주입니다. 3·4절의 호스트 설정을 확인하십시오.
- 정지·자동 제출이 많다면 → 호출 사이 폭주이며, 서버가 막고 있다는 뜻입니다.

---

참고:
[Zed MCP](https://zed.dev/docs/ai/mcp) ·
[Zed Instructions](https://zed.dev/docs/ai/instructions) ·
[Goose 설정 파일](https://block.github.io/goose/docs/guides/config-file/) ·
[Goose 환경 변수](https://goose-docs.ai/docs/guides/environment-variables/) ·
[Qwen3 모델 카드](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
