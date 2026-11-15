# 3.3 계획 — 워크플로우 다이어그램 (`ADVANCED_WORKFLOW_PLANNING`)

> **상태: 구현 완료 (미배포, 2026-11-16), develop 브랜치에 커밋, 미패키징·미푸시, 버전 미변경.** §9의 결정은 모두 권장안대로 확정했습니다. 구현·검증 결과는 §11에 있습니다.
> 영문 상세: [wiki 06](../wiki/06-human-in-the-loop.md#the-workflow-diagram-33-unreleased), [wiki 03](../wiki/03-tool-contract.md), [D34](../wiki/09-defects-and-lessons.md#d34).
> 작성: 2026-11-16, `d19077f` 기준 (3.2.0 + 미배포 페이지 재구성).

---

## 0. 한 줄 요약

`ADVANCED_WORKFLOW_PLANNING=1`이면 에이전트가 최종 계획과 함께 **Mermaid flowchart**를 보내고, 서버는 이를
**순수 파이썬 lint 가드**로 고쳐 쓰거나 거절한 뒤 **정규화된 Mermaid 텍스트**만 승인 페이지로 보냅니다. 페이지는
**함께 반입한 mermaid.js**로 그리고, 사람은 노드를 눌러 의견을 달며, 의견이 생기면 `승인` 버튼이
`수정 요청(워크플로우)`로 바뀝니다. `0`(기본값)이면 도구 텍스트·페이지·지문·응답이 **3.2와 바이트 단위로 같습니다.**

---

## 1. 의존성 원칙의 예외 — 무엇을 허용하고 무엇은 그대로인가

현재 원칙 (wiki README "Ground rules" 1): *Zero third-party dependencies. Standard library only.*

### 1.1 예외 문안 (wiki README, 01-overview, 배포 매뉴얼에 들어갈 내용)

> 1. **서버 런타임(파이썬)은 모든 모드에서 표준 라이브러리만 씁니다.** `pip install`은 어떤 설정에서도 필요하지 않습니다.
> 1a. **예외: `ADVANCED_WORKFLOW_PLANNING=1`일 때 승인 페이지는 브라우저에서 mermaid.js(MIT)를 씁니다.**
>     - 파일은 `vendor/mermaid/`에 **버전 고정·SHA-256 고정**으로 함께 반입하며, CDN이나 외부 네트워크는 쓰지 않습니다.
>     - 플래그가 `0`이면 서버는 이 파일을 읽지도, 보내지도 않습니다. 파일이 없어도 서버는 정상 기동합니다.
>     - 플래그가 `1`인데 파일이 없거나 해시가 다르면 **크게 알리며 저하**됩니다: 페이지는 다이어그램 대신 정규화된
>       Mermaid 원문을 보여 주고, 의견·수정 요청은 그대로 동작합니다. 승인 게이트는 절대 풀리지 않습니다.
>     - 다른 서드파티 산출물은 이 예외로 들어올 수 없습니다. 새 예외는 이 목록에 따로 적어야 합니다.

### 1.2 `requirements.txt`에 대하여 — 권장: 만들지 않고 `vendor/manifest.json`으로 대신함

Mermaid는 **자바스크립트 라이브러리**이고, 파이썬 패키지로는 오프라인에서 Mermaid를 파싱·렌더링할 수 없습니다.
PyPI의 `mermaid-py` 등은 mermaid.ink(외부 웹 서비스)를 부르거나 Node/Chromium(`mermaid-cli`)을 요구합니다 —
폐쇄망에서 둘 다 불가합니다. 그래서 이 기능을 써도 **파이썬 쪽에 설치할 것이 없습니다.**

`requirements.txt`를 만들면 내용은 비어 있는데 "무언가 pip로 설치해야 한다"는 신호를 보안 검토자와 운영자에게 줍니다.
대신 실제 의존성을 정확히 적는 파일을 둡니다.

```
vendor/
  manifest.json          # 반입 서드파티 목록: 이름, 버전, 원본 URL, SHA-256, 라이선스, 쓰는 설정
  mermaid/
    mermaid.min.js       # 버전 고정 (구현 시 11.x 안정판 하나로 고정)
    LICENSE              # MIT (번들에 포함된 DOMPurify·d3 등의 고지 포함)
tools/fetch_vendor.py    # 빌드 PC 전용: manifest의 URL에서 받아 SHA-256 대조 후 저장
```

```json
{ "mermaid": { "version": "11.x.y", "file": "vendor/mermaid/mermaid.min.js",
               "source": "https://cdn.jsdelivr.net/npm/mermaid@11.x.y/dist/mermaid.min.js",
               "sha256": "…", "license": "MIT", "used_when": "ADVANCED_WORKFLOW_PLANNING=1" } }
```

관례상 `requirements.txt`가 꼭 있어야 한다면(사내 스캐너 등) **주석만 있는 파일**로 둡니다:
`# Runtime: none (Python standard library only). Browser asset: see vendor/manifest.json.`
→ **§9 결정 1.**

### 1.3 반입·검증 도구의 변경

| 도구 | 변경 |
|---|---|
| `tools/fetch_vendor.py` (신규) | 빌드 PC에서만 실행. 해시 불일치면 저장하지 않고 실패 |
| `tools/make_package.py` | 허용 목록에 `vendor/**` 추가, `MANIFEST.txt`에 SHA-256 기록. `--no-vendor`로 3.2 크기(~110 KB) 패키지도 만들 수 있게 함 (mermaid 포함 시 ~3 MB) |
| `tools/verify_install.py` | stdlib-only import 검사는 그대로. `vendor/manifest.json`이 있으면 각 파일 해시 검사 → 불일치 NO-GO, 파일 없음은 "플래그 1에서 저하됨" 경고 |
| 보안 검토 사실 (10-deployment) | "HTML 1페이지 + JSON 3개" → "+ 고정 경로 1개(`/vendor/mermaid.js`, 플래그 1에서만, 메모리에서 서빙, 파일시스템 조회 없음)" |

---

## 2. 설정

| 변수 | 값 | 기본 | 의미 |
|---|---|---|---|
| `ADVANCED_WORKFLOW_PLANNING` | `1` / `0` (`true`/`false`도 허용, `_env_bool`) | `0` | 기능 전체 on/off |
| `PLANNING_MCP_WORKFLOW_MAX_NODES` | 정수 | `30` | 이보다 크면 lint 거절 (페이지 가독성·모델 토큰) |
| `PLANNING_MCP_WORKFLOW_RETRIES` | 정수 | `2` | lint 거절 후 다시 보낼 기회. 다 쓰면 서버가 선형 다이어그램으로 대신함 (§4.4) |

변수 이름은 요청대로 `ADVANCED_WORKFLOW_PLANNING`을 그대로 씁니다. 다른 설정은 모두 `PLANNING_MCP_` 접두사이므로
`PLANNING_MCP_ADVANCED_WORKFLOW`를 별칭으로 함께 읽을지 → **§9 결정 2.** `Config.advanced_workflow`,
`wiki/data/config.json`에 추가합니다.

---

## 3. 도구 계약 (모델이 보는 것)

### 3.1 `plan_and_think`에 필드 하나: `workflow` (string)

플래그 `1`에서만 광고됩니다 (D25: 실행 중인 서버에 사실인 것만 말한다). `0`에서 보내면 무시하고 note를 답니다
(`PLANNING_MCP_ALTERNATIVES=off`와 같은 처리).

도구 설명에 추가되는 규칙 (한 번만, 짧게 — 메모리 "concise text"):

> With the final task_list, send `workflow`: a Mermaid flowchart of the plan. One node per task with id T1..Tn in
> task order; a decision is D1{...}; start/end are S and E.

파라미터 설명 (형태 + 예시 하나):

```
flowchart TD
  S([시작]) --> T1["매출 파일 읽기"]
  T1 --> D1{"Q3 데이터가 있나?"}
  D1 -->|예| T2["피벗으로 집계"]
  D1 -->|아니오| E([종료])
  T2 --> T3["5줄 요약"] --> E
```

- `TestToolTextIsLean`에 플래그 `1` 상한을 따로 둡니다 (예상 +120~150 토큰). 플래그 `0` 상한은 그대로.
- **`agents.md`는 바꾸지 않습니다.** 붙여넣는 프롬프트는 하나뿐인데 플래그 `0`인 서버에서 `workflow`를 말하면
  광고되지 않은 필드를 말하는 모순이 됩니다 (Ground rule 7). 규칙은 항상 도착하는 도구 설명에만 둡니다.
- 사고 중(`need_more_thinking=true`)에 보낸 `workflow`는 `draft_workflow`로 초안과 함께 보관·제출됩니다 (`draft_tasks`와 같음).

### 3.2 다이어그램과 태스크 목록의 관계 — 핵심 결정

**태스크 목록이 권위, 다이어그램은 그 설명입니다.** 실행 순서·게이트·상태 기계는 지금과 똑같이 `task_list` 순서를 따릅니다.

- 노드 `T<n>`은 `n`번 태스크입니다. lint가 **모든 태스크에 노드가 하나씩 있는지, 없는 번호의 `T`가 없는지** 확인합니다.
- `T<n>` 노드의 라벨은 서버가 **태스크 제목으로 덮어씁니다** (모델이 다르게 적어도). 그래야 사람이 다이어그램에서 본
  것과 태스크 목록이 갈라지지 않습니다. 모델은 `T3`만 써도 됩니다.
- 분기(`D`)는 "조건에 따라 건너뛸 수 있다"를 사람에게 보여 주는 용도입니다. 서버에는 SKIPPED 상태가 없으므로
  건너뛴 태스크는 지금처럼 `DONE` + `result_log`("조건 불충족으로 건너뜀")로 보고합니다. → **§9 결정 3** (SKIPPED 도입은 범위 밖으로 둘 것을 권장).
- 역방향 간선(`T4 --> T2`, 재시도 루프)은 허용하되 페이지에 점선으로 표시하고 note를 답니다. 실행은 여전히 순차입니다.

### 3.3 지문(fingerprint)

Ground rule 5: 승인은 사람이 본 정확한 버전에 묶입니다. 플래그 `1`에서는 `goal + 태스크 제목 + 정규화된 workflow`의
해시가 지문입니다. 플래그 `0`에서는 지문 계산이 3.2와 같아야 하며(테스트로 고정), 그래서 기존 승인 대기열의 항목이
업그레이드로 무효화되지 않습니다.

### 3.4 응답

- 최종 호출 응답에 `workflow_notes`(lint가 고친 것) 를 `input_notes`로 싣습니다. 모델이 다음에 같은 실수를 덜 하게 하는 용도.
- 실행 중 응답(`next_task` 등)에는 다이어그램을 싣지 않습니다 — 컨텍스트 낭비이고 실행은 태스크 기준입니다.
- 채팅 모드(페이지 없음): `display_to_user`에 ` ```mermaid ` 블록으로 넣습니다 (AnythingLLM 채팅이 렌더링하면 그려지고,
  아니면 코드로 보임). 채팅 모드의 의견은 기존대로 전체 수정(REVISE) 경로입니다.

---

## 4. Lint 가드 — `planning/workflow.py` (순수 파이썬, 새 모듈)

`evidence.py`·`choices.py`처럼 **패키지의 다른 모듈을 import하지 않고, 절대 예외를 던지지 않는** 순수 함수 모음입니다.

### 4.1 구조: 관대하게 읽고, 엄격하게 다시 쓴다

```
모델 텍스트 ─► repair(텍스트 수준) ─► parse(부분 문법) ─► check(하네스 규칙) ─► emit(정규형)
                 고칠 수 있으면 고치고 note          고칠 수 없으면 오류 목록(줄 번호 + 원문 + 고칠 방법)
```

**브라우저에 가는 것은 모델의 원문이 아니라 서버가 AST에서 다시 쓴 정규형뿐입니다.** 정규형은 모든 라벨을 따옴표로
감싸고 특수문자를 엔티티로 바꾸며, 지원하는 문법만 씁니다. 그래서 mermaid.js 파싱 실패는 "모델이 틀렸다"가 아니라
"우리 emit에 버그가 있다"는 뜻이 되고, 이는 테스트로 잡을 수 있습니다.

지원 부분 문법 (그 밖은 오류):
- 헤더 `flowchart`/`graph` + `TD|TB|LR|RL|BT`
- 노드 모양: `[ ]` `( )` `([ ])` `(( ))` `[[ ]]` `{ }`
- 간선: `-->` `---` `-.->` `==>`, 라벨 `-->|x|` / `-- x -->`, 체인 `A --> B --> C`, `A & B --> C`
- `subgraph id ["제목"]` … `end`
- `%%` 주석 (버림)

### 4.2 자동 수리 (note만 남기고 통과)

LLM이 Mermaid에서 자주 틀리는 것들입니다. 각 항목이 테스트 코퍼스의 한 묶음이 됩니다.

| # | 흔한 실수 | 예 | 수리 |
|---|---|---|---|
| R1 | 코드 펜스째 보냄 | ` ```mermaid … ``` ` | 펜스 제거 |
| R2 | JSON 이스케이프된 줄바꿈 | 한 줄에 `\n`이 글자로 들어 있음 | 줄바꿈으로 복원 |
| R3 | 헤더 없음 / 잘못된 방향 | `A --> B`로 시작, `flowchart TOP` | `flowchart TD` 보충 |
| R4 | 따옴표 없는 라벨의 특수문자 | `A[파일 읽기 (csv)]`, `B{완료?: 예}`, `C[a/b & c]` | 라벨을 `"…"`로 감쌈 |
| R5 | 라벨 안의 큰따옴표 | `A["그가 "OK" 함"]` | `#quot;`로 치환 |
| R6 | 예약어 `end`·`graph`·`subgraph`를 노드 id로 | `end([종료])`, `T3 --> end` | id를 `E`로 바꾸고 참조도 함께 |
| R7 | 화살표 뒤 `o`/`x`로 시작하는 id가 붙음 | `A-->ok` (원형 간선으로 해석됨), `A---xB` | 화살표 양옆에 공백 |
| R8 | 잘못된 화살표 | `A -> B`, `A => B`, `A —> B`(em dash), `A → B` | `-->` |
| R9 | 간선 라벨 형식 오류 | `-->|예|>`, `--|예|-->`, `-->|"예" |` | `-->|"예"|` |
| R10 | 스마트 따옴표·전각 괄호가 문법 자리에 | `A[“시작”]`, `A（시작）` | ASCII로 |
| R11 | `subgraph`에 `end` 누락 | 마지막 subgraph가 안 닫힘 | 끝에 `end` 보충 |
| R12 | 줄 끝 `;`, 중복 공백, 탭 | | 정리 |
| R13 | 같은 id를 다른 라벨로 두 번 정의 | `T1[읽기]` … `T1[불러오기]` | 처음 것 유지, note |
| R14 | `T` 노드 라벨이 태스크 제목과 다름 | | 태스크 제목으로 덮어씀, note (§3.2) |
| R15 | 라벨의 HTML / `<br>` | `A[1단계<br>읽기]`, `<b>` | `<br>`은 줄바꿈 하나로, 나머지 태그 제거 |

### 4.3 보안상 제거 (note)

`click …`(콜백·링크), `%%{init: …}%%` 지시문, `style`/`classDef`/`class`/`linkStyle`(색은 서버가 상태로 입힘),
`:::클래스` 접미사. 페이지는 추가로 `securityLevel: 'strict'`로 그리므로 이중 방어입니다.

### 4.4 거절 (고칠 수 없음)

| 코드 (`error_code` 아님, `workflow_errors[]` 항목) | 예 |
|---|---|
| `NOT_FLOWCHART` | `sequenceDiagram`, `gantt` |
| `TASK_NODE_MISSING` / `TASK_NODE_UNKNOWN` | T2가 없음 / 태스크가 3개인데 T5가 있음 |
| `BAD_NODE_ID` | `데이터 읽기 --> B` (공백 있는 id) |
| `UNBALANCED` | `A[시작)` 괄호 짝이 안 맞음 |
| `UNREACHABLE` | 시작에서 닿지 않는 태스크 노드 |
| `TOO_LARGE` | 노드 > `WORKFLOW_MAX_NODES` |

거절의 처리 — 모델을 묶어 두지 않는다는 원칙(D25, "never strand the model"):

1. 최종 호출을 **`ok:false`, `error_code: WORKFLOW_INVALID`** 로 돌려보냅니다. 계획은 `DRAFTING`에 남고, 태스크 목록은
   `draft_tasks`로 보관되어 다시 보낼 필요가 없습니다. 힌트에는 오류 줄(번호·원문·고칠 방법)과 **현재 태스크로 서버가
   만든 선형 예시**를 그대로 넣습니다: "Send workflow again (task_list is kept). Line 4 `데이터 읽기 --> B`: a node id
   cannot contain spaces - use T1["데이터 읽기"]."
2. `PLANNING_MCP_WORKFLOW_RETRIES`(2)회를 넘기면 서버가 **태스크 순서대로 선형 다이어그램을 만들어** 계획을 사람에게
   보냅니다. 페이지에는 "에이전트의 워크플로우를 읽지 못해 태스크 순서로 그렸습니다" 한 줄과 모델 원문(접힘)을 보여 줍니다.
3. `workflow`를 아예 안 보낸 경우도 2와 같이 선형 다이어그램 + note. 재시도를 쓰지 않습니다.
4. 사고 예산·서킷 브레이커 카운터는 그대로 적용됩니다 (`WORKFLOW_INVALID`가 반복 호출 감지에서 예외가 되지 않음).

→ **§9 결정 4**: 1의 "거절 후 재요청" 대신 처음부터 2(서버 대체)로 갈지. 거절이 품질은 높지만 모델 호출이 늘어납니다.

### 4.5 마지막 그물: 브라우저의 `mermaid.parse`

페이지는 그리기 전에 `mermaid.parse(text)`를 부릅니다. 실패하면 원문을 `<pre>`로 보여 주고 "다이어그램을 그리지
못했습니다" 한 줄을 답니다. 태스크 목록·의견·승인은 그대로 동작합니다. 이 경로가 실제로 쓰였다면 emit 버그이므로
페이지가 `/api/control`이 아닌 기존 폴링에 `render_failed: <request id>`를 실어 보내 `audit.jsonl`에
`workflow_render_failed`로 남깁니다 (현장에서 잴 수 있게).

---

## 5. 승인 페이지

### 5.1 그리기

- `planning/web/workflow.js`(신규, `PAGE_SCRIPTS`에서 `views.js` 다음)가 다이어그램을 그리고 노드 클릭을 다룹니다.
  플래그 `0`이면 이 스크립트와 mermaid 모두 페이지에 들어가지 않습니다 (`_assemble_page`가 설정을 받음).
- mermaid.min.js는 **인라인하지 않고** 고정 경로 `GET /vendor/mermaid.js`로, 서버가 기동 시 메모리에 읽어 둔 바이트를
  `Cache-Control: max-age`와 함께 보냅니다. 이유: (1) `_assemble_page`는 `</script`가 들어 있는 스크립트를 거부하는데
  번들 라이브러리는 이를 포함할 수 있음, (2) 3 MB를 매 페이지 로드에 넣지 않음. 경로는 하나뿐이고 URL로 디스크를
  찾는 코드는 여전히 없습니다.
- 설정: `mermaid.initialize({startOnLoad:false, securityLevel:'strict', flowchart:{htmlLabels:false}})`, 테마는
  `prefers-color-scheme`에 따라 `default`/`dark` (지난 커밋의 다크 스킴과 맞춤).
- `mermaid.render()`가 돌려준 SVG는 html 템플릿을 거치지 않는 유일한 마크업입니다. strict 모드의 DOMPurify +
  서버 정규화(§4.1)가 근거이며, `html.js`에 `trustedSvg()` 하나로 예외를 모아 둡니다.
- 위치: PLAN 카드의 개요 아래, 태스크 목록 위. 접기/펼치기 가능(기본 펼침). 노드가 많으면 가로 스크롤.
- 카드 서명(`draw`)은 요청 id로 고정되므로 SVG는 요청당 한 번만 그리고 캐시합니다. 폴링이 다이어그램을 다시 그리지 않습니다 (3.1 "One card at a time").

### 5.2 노드에 의견 달기

```
┌ 워크플로우 ─────────────────────────────────────┐
│  (시작) → [T1 매출 파일 읽기] → ◇D1 Q3 데이터? ─예→ [T2 집계] → [T3 요약] → (종료) │
│                                    └아니오→ (종료)                                │
└──────────────────────────────────────────────┘
  노드를 누르면 의견을 남길 수 있습니다.
  ◇ D1 · Q3 데이터가 있나?     [ 데이터가 없으면 Q2로 대신해 주세요            ]  [지우기]
  워크플로우 전체 의견          [                                              ]
```

- 노드(`g.node`)를 누르면 다이어그램 아래에 그 노드의 의견 칸이 열립니다. 키보드로도: 노드마다 `tabindex=0`,
  `role=button`, `aria-label="D1 Q3 데이터가 있나? 의견"`.
- 의견이 있는 노드는 테두리를 강조합니다 (기존 `filled` 표시와 같은 색).
- **간선에도 의견을 달 수 있게 할지** → **§9 결정 5.** 권장: v1은 노드 + "워크플로우 전체 의견" 칸만. 간선 의견은
  "D1에서 '아니오'일 때 …"처럼 노드 의견으로 충분히 말할 수 있습니다.
- `T` 노드에 단 의견은 **그 태스크의 의견 칸과 같은 것**으로 다룹니다 (같은 draft 키 `…:<request>:<task_id>`).
  다이어그램에서 써도, 태스크 행에서 써도 양쪽에 보입니다. 한 태스크에 의견 칸이 둘이 되면 어느 것이 보내지는지
  모호해지기 때문입니다.
- 초안은 `localStorage` `planning-mcp:draft:<request_id>:wf:<node_id>` (D22: 재그리기·새로고침·탭 간 동기).

### 5.3 버튼이 결과를 먼저 말한다

기존 규칙(3.1)을 그대로 확장합니다. 의견이 생기는 순간 `승인`은 사라지고 수정 요청 버튼이 나타납니다.

| 쓴 것 | 버튼 (제목 / 줄) | `scope` |
|---|---|---|
| 없음 | `승인` | — |
| 태스크 3에만 (행 또는 T3 노드) | `수정 요청` / `3번만` | `TASKS` (3.2와 같음) |
| 비태스크 노드(D1, S, E) 또는 워크플로우 전체 칸 | `수정 요청(워크플로우)` / `노드 1개` | `WORKFLOW` |
| 위 + 태스크 3 | `수정 요청(워크플로우)` / `노드 1개` / `3번 태스크` | `WORKFLOW` |
| 계획 전체 칸 또는 ☐ 계획 전체를 다시 세우기 | `수정 요청` / `계획 전체 재작성` | `PLAN` |
| … + 완료 기준을 고침 | 한 줄 추가: `태스크 완료 기준 1건 반영` / `… 전달` | 위와 같음 |

우선순위 `PLAN > WORKFLOW > TASKS`. `scopeOf()`가 이 순서로 판정하고 라벨도 같은 함수에서 나오므로 라벨과 효과가
다를 수 없습니다 (3.1과 같은 보장). 서버는 scope를 추론하지 않습니다.

### 5.4 다시 승인할 때

새 PLAN 요청의 다이어그램에서 **바뀐 노드**(새 노드, 라벨이 바뀐 노드)를 강조하고, 의견을 달았던 노드에는
"요청하신 내용: …"을 툴팁/아래 줄로 보여 줍니다 — 사람이 자기 요청이 반영됐는지 한눈에 확인 (1.13 rework의 `이전 결과`와 같은 생각).

### 5.5 다른 카드 (선택: 3.3의 두 번째 단계)

- **실행 카드**: 같은 다이어그램에 태스크 상태 색(대기/진행중/완료/실패, `classDef`는 서버가 주입). 사람이 "지금 어디쯤"을 봄.
- **완료 보고**: 완료 색 + 재작업 요청 노드 강조.
- **정지 카드**: 초안 다이어그램 읽기 전용.

1단계(PLAN 카드)만으로 요청된 기능은 완성됩니다. 2단계는 같은 렌더러에 색만 입히면 되므로 작지만, 실행 카드는
태스크마다 다시 그려야 해서(서명이 `rev`로 바뀜) 비용이 있습니다 → **§9 결정 6.**

---

## 6. 서버 쪽 수정 요청 흐름

### 6.1 기록

`ApprovalStore.record_decision`: `SCOPES`에 `SCOPE_WORKFLOW = "WORKFLOW"` 추가. 페이지가
`workflow_comments: {"D1": "...", "*": "전체 의견"}`을 보냅니다. **보여 준 다이어그램에 없는 노드 id가 하나라도 있으면
결정 전체를 거부**합니다 (`validate_page_choices`·`validate_page_criteria`와 같은 엄격함 — 오래된 탭이거나 위조).
`T` 노드 의견은 페이지가 이미 `comments`(태스크 의견)로 옮겨 보내므로 `workflow_comments`에 `T*` 키는 없습니다.

`_mutate_no`(scope를 읽는 유일한 곳):

```python
plan.pending_revision = {
    "origin": "workflow",
    "workflow_targets": {"D1": "데이터가 없으면 Q2로", "*": "..."},
    "targets": {3: "..."},          # 태스크 의견이 있을 때만
}
```

완료 기준을 고친 채 눌렀다면 TASKS와 같이 태스크에 바로 적용합니다 (태스크가 유지되므로). 고른 대안도 TASKS와 같이 다룹니다.

### 6.2 모델이 받는 힌트

```
The user commented on the workflow.
  D1 "Q3 데이터가 있나?": "데이터가 없으면 Q2로 대신해 주세요"
Call plan_and_think with workflow=<the whole revised flowchart>. Keep T1..T3.
If a change needs a new or removed task, send the full task_list too.
```

태스크 의견도 있으면 기존 `task_updates=[{"task_id": 3, "title": "..."}]` 한 줄이 뒤에 붙습니다.

### 6.3 모델의 답

| 모델이 보낸 것 | 처리 |
|---|---|
| `workflow` (+ 의견 달린 태스크의 `task_updates`) | 정상. 태스크 id·순서·결과 유지. lint 통과 후 새 PLAN 요청 |
| `workflow` + 전체 `task_list` | 수락 + note, audit `workflow_replanned` (구조 변경이 필요했던 경우. 낭비일 수 있지만 안전 — 사람이 다시 승인) |
| `task_updates`만, `workflow` 없음 | `REVISION_INCOMPLETE`, 힌트에 정확한 인자 |
| `workflow`가 이전과 정규형이 같음 | `REVISION_INCOMPLETE` ("the workflow is unchanged") — 사람의 요청을 무시한 것 |
| 의견 없는 태스크를 고친 `task_updates` | 기존처럼 버리고 note |

`workflow`만으로 기존 3.0 경로(`pending_revision`, `REVISION_NOT_REQUESTED` 등)와 같은 모양이므로 새 오류 코드는
`WORKFLOW_INVALID` 하나뿐입니다.

---

## 7. 플래그 0의 동일성 보장

"0이면 현재 버전과 동일"을 테스트로 고정합니다.

- `build_tool_definitions(..., advanced_workflow=False)`의 출력이 3.2의 스냅샷과 같음 (모든 모드 조합).
- `page_html()`이 3.2와 바이트 단위로 같음 (workflow.js·mermaid 경로 없음).
- `/vendor/mermaid.js`는 404.
- 같은 plan의 지문이 3.2 계산과 같음.
- `workflow`를 보내면 무시 + note, 상태 변화 없음.
- `vendor/` 폴더가 없어도 기동·전 테스트 통과.

---

## 8. 테스트와 검증

| 무엇 | 어떻게 |
|---|---|
| lint 수리 R1–R15, 제거, 거절 | `tests/test_workflow_lint.py`: 실수 코퍼스(`tests/data/workflow_corpus/*.mmd` + 기대 정규형) — 실제로 모델이 틀린 출력을 모으면 여기에 추가 |
| lint가 절대 예외를 던지지 않음 | 무작위 바이트/잘린 텍스트 퍼징 (leniency 테스트와 같은 방식) |
| **정규형이 진짜 mermaid로 파싱됨** | 개발 PC 전용: 코퍼스 전체의 정규형을 브라우저(Claude 내장 브라우저 또는 `tools/page_preview.py`)에서 `mermaid.parse`로 확인. node만으로는 mermaid가 DOM을 요구해 `tests/web/runner.js`에 넣기 어려움 → 수동 검증 목록에 추가하고, 자동화는 jsdom 없이 가능한지 구현 때 확인 |
| 수정 요청 흐름 | `tests/test_workflow_revision.py`: WORKFLOW scope 기록, 모르는 노드 거부, 힌트, 모델 답 5가지(§6.3), 재승인 지문 |
| 페이지 | `tests/pagejs.py`로 `scopeOf`/`revLabel` 표(§5.3) 고정, 브라우저로 노드 클릭·키보드·다크 모드·초안 유지·버튼 라벨 전환·저하 모드(파일 없음) 확인 |
| 플래그 0 동일성 | §7 전부 |
| 반입 | `test_packaging.py`: `vendor/` 포함/제외 패키지, 해시 불일치 시 `verify_install` NO-GO |
| 스모크 | `smoke_workflow.py`: 실제 서버로 계획 → 페이지에서 노드 의견 → 수정 요청(워크플로우) → 수정된 계획 승인 → 실행 |

---

## 9. 결정이 필요한 것

| # | 질문 | 권장 |
|---|---|---|
| 1 | `requirements.txt` | **만들지 않고** `vendor/manifest.json`으로 대신. 꼭 필요하면 주석만 있는 파일 (§1.2) |
| 2 | 변수 이름 | `ADVANCED_WORKFLOW_PLANNING` 그대로 + `PLANNING_MCP_ADVANCED_WORKFLOW` 별칭 |
| 3 | 분기(건너뛰기)의 실행 의미 | 표시용으로만. SKIPPED 상태는 도입하지 않음 (§3.2) |
| 4 | lint로 못 고치는 다이어그램 | 거절 + 재요청 2회, 이후 서버가 선형 다이어그램으로 대신 (§4.4) |
| 5 | 간선 의견 | v1은 노드 + 전체 의견만 |
| 6 | 실행 카드·완료 보고의 다이어그램 | 3.3은 PLAN 카드(+정지 카드 읽기 전용)까지, 상태 색은 다음 단계 |
| 7 | mermaid 버전 | 11.x 안정판 하나로 고정, 해시를 manifest에 기록 |

---

## 10. 구현 순서 (확정 후)

1. `planning/workflow.py` + lint 테스트 코퍼스 (서버 나머지와 무관, 먼저 완성)
2. 설정·스키마·지문·`plan_and_think` 처리·`WORKFLOW_INVALID`·선형 대체 + 플래그 0 동일성 테스트
3. `vendor/` + `fetch_vendor.py` + `/vendor/mermaid.js` 경로 + 패키징·`verify_install`
4. 페이지: 그리기, 노드 의견, 버튼 라벨, 초안, 저하 모드
5. `WORKFLOW` scope 기록·`_mutate_no`·힌트·모델 답 처리
6. 스모크 + 브라우저 검증, wiki(README 원칙 1a, 03, 06, 10, data/config.json, 08, 11) 갱신, 버전 3.3.0

---

## 11. 구현 결과 (2026-11-16)

§9의 권장안 7건을 그대로 적용했습니다. 설계와 다른 점과 구현하며 정한 것은 다음과 같습니다.

| 항목 | 내용 |
|---|---|
| 공백 있는 노드 id | 설계(§4.4)에서는 거절이었으나, 뜻이 분명하므로 **수리**로 바꿨습니다. `데이터 확인 --> T1`은 라벨이 "데이터 확인"인 노드 `N1`이 됩니다. |
| 비ASCII·예약어 id | 모두 안전한 id(`N1`…, `end`는 `E`)로 바꾸고 라벨은 그대로 둡니다. |
| `T` 노드를 다른 id로 그린 경우 | 라벨이 태스크 제목과 같으면 그 태스크로 읽습니다 (`A["매출 파일 읽기"]` → `T1`). |
| 플래그 0의 페이지 | 3.2와 **바이트 단위로 동일**(SHA-256 비교로 확인). `workflow.js`는 다른 스크립트를 고치지 않고 함수를 감싸는 방식으로 확장하며, `decide`의 사본은 테스트로 원본과 대조합니다. |
| mermaid 버전 | 11.4.1 (`vendor/manifest.json`에 SHA-256 고정). |
| 바뀐 노드 표시 | id가 아니라 라벨로 판단합니다. 모델이 id만 바꾼 노드(`start` → `S`)는 표시하지 않습니다. |
| 렌더 실패 기록 | `/api/control` 대신 전용 경로 `POST /api/workflow-failed` → `state/workflow_render_failed.jsonl`. |
| `requirements.txt` | 만들지 않았습니다 (결정 1). |

**검증**

- 단위: `test_workflow_lint.py`(40), `test_workflow_revision.py`(28), `test_workflow_page.py`(17), 스모크 `smoke_workflow.py`. 기존 테스트는 변경 없이 모두 통과.
- 브라우저(실제 서버): 수리된 워크플로우 렌더링, 노드 클릭 → 의견, 버튼 `수정 요청(워크플로우) / 노드 1개 / 3번 태스크`, 새로고침 후 초안 유지, 대기 중인 에이전트에 WORKFLOW 수정 요청 전달, 다시 그린 카드의 바뀐 노드·요청 내용 표시, 다크 모드, mermaid 없을 때의 텍스트 대체.
- 실제 파서: lint 코퍼스와 퍼징 결과의 정규형 611건을 브라우저에서 mermaid 11.4.1로 파싱 → 처음 12건 실패(백틱으로 시작하는 라벨, [D34](../wiki/09-defects-and-lessons.md#d34)) → 수정 후 611건 모두 통과, 표본 62건 렌더링 통과.

**남은 것 (현장 확인)**: 사내 모델이 프롬프트 변경 없이 `workflow`를 보내는지, 거절 빈도와 사유, 워크플로우 의견에 다시 그리기로 답하는지 전체 재계획으로 답하는지 — [wiki 11](../wiki/11-status-and-next-steps.md)의 감사 이벤트로 측정합니다. 실행 카드·완료 보고의 상태 색 다이어그램(결정 6)은 다음 단계입니다.

