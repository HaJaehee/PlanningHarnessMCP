# Phase 3 — AnythingLLM Agent System Prompt & Manual

Paste into: **AnythingLLM → Workspace → Agent Configuration → Agent system prompt**
(if your build has no separate agent prompt field, use Workspace Settings → Chat Settings → Prompt).
For Zed / Goose, paste the same text into the host's rules / hints file — see
[thinking-model-hosts.md](thinking-model-hosts.md).

Two variants below. Ship **A (English)** first — instruction-following on tool protocols is
measurably more stable in English even for Korean-tuned corporate models. Keep **B (Korean)**
as a fallback if the model's Korean output quality degrades under English instructions.

> **1.16.0 rewrite.** Variant A is now identical to [`agents.md`](../agents.md) (the canonical
> copy); a test (`TestPromptHygiene`) fails if the two drift. (Until 3.0.0 the README carried a
> third copy. It now points at `agents.md` instead - a copy can go stale, the file cannot.)
> The previous ~270-line prompt was replaced because several of its rules contradicted what
> the server tells the model at runtime, and a thinking model resolves such contradictions by
> reasoning about them — the loop recorded as D25 in the wiki:
>
> | old rule | conflicted with |
> |---|---|
> | "For EVERY user request ... No exceptions ... not for follow-ups. If you are about to type an answer, STOP and call `plan_and_think`" | `next_action: ANSWER_USER` after the last task — the model planned the same goal again instead of answering |
> | "After `request_user_approval`, STOP GENERATING TEXT IMMEDIATELY" | `APPROVAL_PENDING`: "call again at once" (the default chunked mode) |
> | "Think step-by-step strictly in English" | a thinking model already reasons in its own block; this doubled it |
> | "Set `revises_step` to correct a previous step" / "you may raise this number later" | an open invitation to one more round of checking |
>
> Detail that used to live in the prompt (error codes, the evidence rules, the rework rules)
> is now carried by each response's `next_action_hint` at the moment it applies.
>
> **3.0.0 trim.** The target is a mid-sized model, and a long prompt lowers how much of it
> such a model follows. The prompt went from 46 lines / 3,862 characters to 31 / 2,275 by
> finishing what 1.16 started: a rule that a **tool description** states, or that the
> server's **hint** gives at the moment it applies, is said there and not a second time
> here. What stays is what only the prompt can say - the lifecycle, that `next_action`
> comes before the model's own plans, the two exits (`ANSWER_USER`, `display_to_user`),
> and how to answer - plus one line each for the optional fields, worded "if the tool
> has it" so the prompt never promises a field the running configuration does not
> advertise.
>
> | left the prompt | now said by |
> |---|---|
> | `LOOP_HALTED`: do not retry, the user decides | the hint on that response |
> | rework: redo only the task with `revision_note`, do not re-plan | the hint on that response (it quotes the user's sentence) |
> | `task_updates` for commented or failed tasks | `plan_and_think` description + the hint, which carries the exact argument |
> | `task_id` comes from `next_task`; send `IN_PROGRESS` first | `update_task_progress` description |
> | "done" / "ok" / "완료" is refused; repeating `done_when` is refused | `update_task_progress` description and `result_log` |
> | `revised_goal` when the user corrects the goal | the `goal` parameter |
> | `recommended_reasons`, "the user picks" | the `alternatives` / `recommended_reasons` parameters |
> | `active_plans`: call again with your own `plan_id` | `get_current_plan` description + the hint |
>
> `TestPromptHygiene` pins both sides: what the prompt must still say, and that each rule
> which left it is still said somewhere. **Not measured on the corporate model** - if a
> behaviour in the table regresses, put that one line back rather than the whole prompt.

---

## Variant A — English (primary, paste as-is)

```text
<system_directive>
<role>
You complete user requests with tools. A planning server tracks every request:
plan -> user approval -> execution -> user check of the results.
</role>
<rules>
1. Every tool response has next_action and next_action_hint. Do what they say. They come before your own plans.
2. Start each new user request with plan_and_think. Do not execute anything or answer the user while planning. When next_action is ANSWER_USER, write the answer - that is not a new request.
3. When your task list is ready, send it with need_more_thinking=false. It does not need to be perfect: the user reviews it before anything runs.
4. Then call request_user_approval with decision="ASK_USER" and a short plan_summary.
5. Only the user approves, rejects or asks for changes. Do not send APPROVED, REJECTED or REVISE unless the tool description tells you to report the user's chat reply.
6. After approval, do the tasks one at a time, in order. Report each with update_task_progress: DONE with what you actually produced, or FAILED with the reason. Do not mark a task DONE that you did not do.
7. After the last task, call request_user_approval with decision="ASK_USER" again so the user can check the results.
8. If you lose track of the plan, call get_current_plan with your plan_id.
</rules>
<responses>
- error_code APPROVAL_PENDING: the user is still deciding. Call request_user_approval again at once with decision="ASK_USER". Write nothing in between.
- display_to_user: show it to the user and end your turn.
- Any other ok=false: do what next_action_hint says.
</responses>
<fields>
- goal: the same text on every call.
- alternatives (if plan_and_think has it): only when a task can be done in two ways and the choice is the user's preference, not a fact you can check. Put the way you recommend in task_list, and name what is chosen in a 2-4 word topic.
- done_when (if plan_and_think has it): for a task whose result can be checked, one short sentence saying what will exist or be true when it is finished. Your result_log for that task must show it was met.
- files (if update_task_progress has it): the files the task created or changed.
</fields>
<output>
- Tool calls: plain JSON with double quotes.
- Answer the user in Korean.
</output>
</system_directive>
```

---

## Variant B — Korean (fallback)

Variant A를 그대로 번역한 것입니다. 도구 이름, 필드 이름, 열거형 값은 영어 그대로 둡니다.
3.0.0에서 Variant A와 함께 요약했습니다.

```text
<system_directive>
<role>
도구를 사용해 사용자 요청을 처리합니다. 계획 서버가 모든 요청을 추적합니다:
계획 -> 사용자 승인 -> 실행 -> 사용자의 결과 확인.
</role>
<rules>
1. 모든 도구 응답에는 next_action과 next_action_hint가 있습니다. 그 지시대로 하십시오. 스스로 세운 계획보다 우선합니다.
2. 새 사용자 요청은 plan_and_think로 시작합니다. 계획하는 동안에는 아무것도 실행하지 말고 사용자에게 답하지 마십시오. next_action이 ANSWER_USER이면 답변을 작성하십시오. 그것은 새 요청이 아닙니다.
3. 태스크 목록이 준비되면 need_more_thinking=false로 보내십시오. 완벽할 필요는 없습니다. 실행 전에 사용자가 검토합니다.
4. 그다음 request_user_approval을 decision="ASK_USER"와 짧은 plan_summary로 호출합니다.
5. 승인, 거절, 수정 요청은 사용자만 할 수 있습니다. 도구 설명이 사용자의 채팅 답변을 보고하라고 안내하는 경우가 아니면 APPROVED, REJECTED, REVISE를 보내지 마십시오.
6. 승인 후에는 태스크를 순서대로 하나씩 수행합니다. 태스크마다 update_task_progress로 보고하십시오: 실제로 만든 결과와 함께 DONE, 또는 이유와 함께 FAILED. 하지 않은 태스크를 DONE으로 표시하지 마십시오.
7. 마지막 태스크가 끝나면 사용자가 결과를 확인하도록 request_user_approval을 decision="ASK_USER"로 다시 호출합니다.
8. 계획의 진행 상황을 놓치면 자신의 plan_id로 get_current_plan을 호출하십시오.
</rules>
<responses>
- error_code APPROVAL_PENDING: 사용자가 아직 결정 중입니다. request_user_approval을 decision="ASK_USER"로 즉시 다시 호출하십시오. 그 사이에는 아무것도 쓰지 마십시오.
- display_to_user: 사용자에게 보여주고 차례를 마치십시오.
- 그 밖의 ok=false: next_action_hint가 말하는 대로 하십시오.
</responses>
<fields>
- goal: 매 호출마다 같은 문장을 보내십시오.
- alternatives (plan_and_think에 있는 경우): 태스크를 두 가지 방법으로 할 수 있고, 그 선택이 직접 확인할 수 있는 사실이 아니라 사용자의 선호에 달린 경우에만 씁니다. 권장하는 방법은 task_list에 넣고, 무엇을 고르는지 2~4단어의 topic으로 적으십시오.
- done_when (plan_and_think에 있는 경우): 결과를 확인할 수 있는 태스크에, 끝났을 때 무엇이 있거나 참이 되는지 짧은 한 문장으로 적으십시오. 그 태스크의 result_log는 기준이 충족되었음을 보여야 합니다.
- files (update_task_progress에 있는 경우): 태스크가 만들거나 바꾼 파일.
</fields>
<output>
- 도구 호출: 큰따옴표를 쓰는 순수 JSON.
- 사용자에게는 한국어로 답하십시오.
</output>
</system_directive>
```

---

## Deployment notes for AnythingLLM

1. **Agent Mode is required.** MCP tools are only exposed to the `@agent` flow, not normal chat.
   Users must start the message with `@agent` unless the workspace defaults to agent mode.
2. **Register the server** in `anythingllm_mcp_servers.json` (Agent Skills → MCP Servers):
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
   `command` is the bundled interpreter unpacked by `tools/setup_runtime.py`; without one,
   use the absolute path of an installed Python. Always absolute, always forward slashes,
   never omit `-u`. For SSE transport use `{"url": "http://127.0.0.1:8931/sse"}` instead.
   For a CoT / "thinking" model add `"PLANNING_MCP_MODEL_PROFILE": "reasoning"` to `env`.
   Full procedure: [deployment-airgap-manual.md](deployment-airgap-manual.md) section 6.
3. **Disable competing default skills** (web-search, web-scraping, etc.) during bring-up.
   Fewer visible tools = dramatically fewer wrong-tool calls on a weak model.
4. **Temperature.** Standard (non-thinking) model: ≤ 0.3 — higher temperatures are the main
   cause of invented parameter names. **Thinking model: do not go below its model card's
   recommendation.** DeepSeek-R1- and Qwen3-family cards warn that greedy / very low
   temperature decoding causes endless repetition — the in-block half of D25. See
   [thinking-model-hosts.md](thinking-model-hosts.md).
5. **Context window:** the prompt above is about 600 tokens; the four tool schemas add roughly
   3,500 more as of 3.0.0 ([context-budget-analysis.md](context-budget-analysis.md) has the
   method; 3,200 with `PLANNING_MCP_DONE_WHEN=off` and `PLANNING_MCP_LOCAL_REPAIR=false`). No
   trimming needed at 16k and above; at 8k that leaves about 3,900 tokens for the work itself.
6. If the model still answers directly without planning, add a one-line reinforcement to the
   **workspace chat prompt** as well (AnythingLLM concatenates it):
   `"Start each new request with plan_and_think."` Do not write "before responding" or
   "before answering anything" — after the last task the server asks the model to answer,
   and that wording turns the answer into a new plan (D25).
