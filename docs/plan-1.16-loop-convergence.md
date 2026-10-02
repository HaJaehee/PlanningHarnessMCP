# 1.16 계획 — 계획 단계 폭주 차단 (구현 완료)

> **상태: 구현 완료 (2026-10-02), develop 브랜치에 커밋, 미패키징·미푸시.** 상세 근거와 설계는 영문 wiki가 원본입니다:
> [D25/D26](../wiki/09-defects-and-lessons.md#d25), [상태 기계 §Loop convergence](../wiki/04-state-machine.md#loop-convergence-1160),
> [HITL §Held calls](../wiki/06-human-in-the-loop.md), [변경 이력 1.16.0](../wiki/08-changelog.md).
> 호스트 설정은 [thinking-model-hosts.md](thinking-model-hosts.md), 다음 단계는 [plan-2.0-task-alternatives.md](plan-2.0-task-alternatives.md).

## 1. 사용자가 확인한 사실

- Zed / Goose + 중형 CoT(thinking) 모델 조합에서, 자기 검증("wait, let me reconsider")이 폭주해 계획 단계를
  빠져나오지 못합니다.
- 폭주는 **두 곳 모두**에서 관찰되었습니다: 한 번의 thinking 블록 안, 그리고 `plan_and_think` 호출의 반복.
- 사용자 PC에서 사내 모델을 직접 돌릴 수 없어 **재현이 불가능**합니다.
- 서킷 브레이커가 필요합니다.
- 추가 요청: `agents.md` 지시문 충돌을 교정하고 장황한 설명을 요약할 것, `PLANNING_MCP_MAX_ACTIVE_PLANS` 기본값을 20으로 올릴 것.

## 2. 재현 없이 진행한 방법

1. 모델 없이 코드만으로 4개 경로(A–D)를 재현했습니다. 모두 서버가 그대로 받아주고 있었습니다.
   - A: 사고 단계에 상한이 없음
   - B: 승인 대기 중에 재계획하면 DRAFTING으로 되돌아감
   - C: 완료 보고 대기 중에 재계획하면 증거가 소실됨 (D26)
   - D: 완료 직후 같은 목표를 다시 계획함
2. 재검토를 반복하도록 일부러 짠 가짜 모델로 "사람 개입 없는 계획 호출은 유한하다"는 보장을 테스트로 고정했습니다.
3. 배포 후 판단할 수 있도록 현장 기록을 감사 로그에 남기고, 오프라인 분석 도구(`tools/loop_report.py`)를 만들었습니다.

## 3. 구현 내용

| 항목 | 내용 |
|---|---|
| 사고 예산 | 계획 라운드마다 상한을 둡니다(standard 8, reasoning 2). 힌트는 항상 "확정해도 된다(완벽할 필요 없음, 사용자가 검토함)"를 먼저 말하고 남은 단계를 셉니다. |
| 초안 보관·자동 제출 | 생각 중에 보낸 `task_list`를 초안으로 보관합니다. 예산이 소진되면 거절하지 않고 초안을 사람에게 제출합니다(승인 페이지가 있으면 바로 게시). 초안이 없으면 정지합니다. |
| 상태 가드 | 확정된 계획은 모델의 재계획으로 다시 열리지 않습니다(D26 차단). 사람이 승인 요청을 보고 있는 동안에는 그 계획에 대한 호출이 사람을 기다리고, 모델의 생각은 카드의 "에이전트 추가 의견"으로 표시됩니다. 방금 완료한 목표는 다시 계획하지 않고 결과로 답하게 합니다. |
| 서킷 브레이커 | 다음 중 하나에 해당하면 계획을 정지하고 승인 페이지에 반복 감지 카드를 띄웁니다. 버튼은 이 초안으로 승인 / 계속 진행(방향 전달) / 취소입니다.<br>· 진척 없는 호출 12회<br>· 같은 호출 3회 연속<br>· 같은 오류 또는 거절된 재계획 4회 연속<br>· 목표만 바꾼 재시작 3회<br>· 초안 없이 예산 소진 |
| reasoning 프로필 | `PLANNING_MCP_MODEL_PROFILE=reasoning`을 설정하면 `plan_and_think`가 한 번의 호출로 계획을 기록합니다. 단계 번호 관련 필드는 노출하지 않습니다. |
| 지시문 정리 | `request_user_approval` 설명을 승인 모드별로 생성합니다. "ANY request" 규칙을 "새 요청마다, ANSWER_USER는 답하기"로 바꿨습니다. `agents.md`를 약 45줄로 재작성했고, README·Phase 3 문서와 똑같도록 테스트로 고정했습니다. |
| 현장 기록 | `clientInfo`, `gap_sec`, `reconsider`, `loop_halted`/`halt_resolved`, `auto_finalized`, `replan_redirected`, `call_held_for_human`을 기록합니다. `tools/loop_report.py`로 계획별 반복 징후를 요약합니다. |
| 설정 | `PLANNING_MCP_MAX_ACTIVE_PLANS` 기본값을 5에서 20으로 올렸습니다. |

## 4. agents.md에서 교정한 충돌

| 이전 지시 | 충돌 대상 |
|---|---|
| "MUST call plan_and_think before answering ANY user request" (Phase 3 Variant A: "No exceptions ... STOP and call plan_and_think") | 마지막 태스크 후 `next_action: ANSWER_USER` |
| "After request_user_approval, STOP GENERATING TEXT IMMEDIATELY" | `APPROVAL_PENDING`: "즉시 다시 호출" |
| "Think step-by-step strictly in English" | thinking 모델은 이미 자체 블록에서 추론함 |
| "Set revises_step to correct a previous step", 약 50개의 MUST/NEVER/STOP | 매 결정마다 준수 여부를 재검증하게 만드는 유발 요인 |
| 오류 코드 목록, 증거 규칙, 재작업 규칙의 장문 설명 | 해당 시점의 `next_action_hint`가 이미 전달함 → 요약 |

## 5. 검증 결과

- 단위 테스트 443개(신규 78개)와 smoke 테스트 6개가 모두 통과했습니다.
- 기존 테스트 7개는 기대값을 갱신했습니다. 각 테스트의 안전성 단언은 유지했고, 1.16에서 의도적으로 바뀐 경로만 반영했습니다.
- 실제 `ApprovalServer`와 브라우저로 직접 확인했습니다.
  - 반복 감지 카드와 에이전트 추가 의견 카드가 정상 표시됨
  - 방향을 입력하면 버튼 라벨이 바뀜
  - 클릭하면 대기 중이던 에이전트 호출이 1초 안에 사람의 문장을 앞세워 반환됨
- 가짜 모델 기준으로, 사람 앞에 도달하기까지의 호출 수는 다음과 같습니다.

| 가짜 모델의 행동 | 호출 수 |
|---|---|
| 항상 한 단계 더 (standard) | 12회 이내 |
| 항상 한 단계 더 (reasoning) | 6회 이내 |
| 확정 후 재계획 | 5회 |
| 완료 후 재계획 | 4번째 호출에서 정지 |

## 6. 남은 일 (현장)

1. thinking 모델 배포 시 다음을 적용합니다.
   - `PLANNING_MCP_MODEL_PROFILE=reasoning` 설정
   - 새 프롬프트 붙여넣기
   - [호스트 설정](thinking-model-hosts.md): 샘플링은 모델 카드 권장값, 출력 길이 상한 지정
2. 몇 세션 사용한 뒤 `python tools/loop_report.py`를 실행합니다.
   - 호출은 적은데 간격이 긴 경우 → 블록 안 폭주이므로 호스트 설정을 확인합니다.
   - 정지·자동 제출이 많은 경우 → 호출 사이 폭주이며, 서버가 막고 있는 상태입니다.
3. 브레이커 기본값은 실측 없이 정한 보수적인 값입니다. 2번의 결과를 보고 조정합니다.
4. 패키징(`package_source.ps1`, `MANIFEST.txt` 갱신)과 푸시는 사용자 확인 후 진행합니다.
