// Approval page - the cards. Each function here takes what the server sent and returns
// the HTML that shows it, and does nothing else: no reading the page, no changing it.
// That is what lets the tests draw every card outside a browser (tests/pagejs.py).
// What the human types into a card is form.js; putting cards on the page is board.js.
//
// Markup is written as html`` templates (html.js), so every ${value} is escaped. A
// button says what it does in data-act, with its arguments in data-* attributes, and
// app.js runs it: no handler is written into the markup.

// The version of the server that is serving this page (filled in when it is served).
const VERSION='__PLANNING_MCP_VERSION__';
// A request can be asked by another planning-mcp process on the same state directory,
// and after an upgrade that process may still be running older code. Nothing is said
// when the versions agree - which is nearly always.
function verNote(d){
  if(d.version===VERSION)return '';
  const asked=d.version?`이 요청은 planning-mcp ${d.version} 서버가 보냈습니다. `
                       :'이 요청을 보낸 서버는 버전을 남기지 않는 이전 버전입니다. ';
  return html`<p class="vernote">${asked}이 페이지는 ${VERSION}입니다.</p>`;
}

// ---- buttons -------------------------------------------------------------------
// A decision. Its id (ok-/rev-<request>) is how form.js relabels it as the human
// types; the reject button is never relabelled and has none.
function decideButton(d,kind,decision,label,hidden){
  return html`<button class="${kind}"${hidden&&html` hidden`}${kind!=='no'&&html` id="${kind}-${d.id}"`}
    data-act="decide" data-id="${d.id}" data-decision="${decision}">${label}</button>`;
}
// A stop, a note, taking either back, or closing the plan - for a running plan.
function controlButton(r,cls,action,label){
  return html`<button class="${cls}" type="button" data-act="control" data-plan="${r.plan_id}"
    data-control="${action}">${label}</button>`;
}

// ---- choices (2.0.0) -------------------------------------------------------
// A task that offers a choice shows its options as radios, the model's recommendation
// (index 0) pre-selected and marked 권장. What is picked travels with the approval;
// the server refuses any index that was not on screen.
function hasChoice(t){return !!(t.options&&t.options.length>=2);}
// '집계 방식 · 3가지 중 선택' - what is being chosen, from the model; a neutral noun phrase
// when it gave none. Mirrors choices.choice_heading, which writes the chat text.
function choiceHeading(t){
  return html`<span class="pick">${(t.topic||'').trim()||'진행 방법'}</span>
    <span class="cnt"> · ${t.options.length}가지 중 선택</span>`;
}
function optionsHtml(d,t,allowOther){
  if(!hasChoice(t))return '';
  // aria-label: a screen reader (and the accessibility tree) otherwise names the
  // radio by its value, "0" / "1", rather than by the option it stands for.
  const radio=(value,label,checked)=>html`<input type="radio" name="ch-${d.id}-${t.task_id}"
      data-req="${d.id}" data-ctid="${t.task_id}" value="${value}"${checked&&html` checked`}
      aria-label="${label}">`;
  const option=(o,i)=>html`<label class="opt">
      ${radio(i,`${LETTERS[i]}. ${o.title}${i===0?' (권장)':''}`,i===0)}
      <span class="optl">${LETTERS[i]}.</span>
      <span>${o.title}${i===0&&html`<span class="rec">권장</span>`}${o.reason&&html` <span class="why">— ${o.reason}</span>`}</span>
    </label>`;
  const other=html`<label class="opt">
      ${radio('other','기타',false)}
      <span class="optl"></span><span>기타 (직접 입력)</span>
    </label>`;
  return html`<div class="opts">${t.options.map(option)}${allowOther&&other}</div>`;
}

// ---- done_when (3.0.0) -----------------------------------------------------
// What "finished" means for a task, and on a plan the field to write or rewrite it.
function dwRow(d,t,editable){
  const has=!!t.done_when,text=t.done_when||'';
  if(!has&&!editable)return '';
  return html`<div class="dw${has?'':' hid'}">
      <span class="dwl">태스크 완료 기준</span>
      <span class="dwt">${text}${t.done_when_by==='user'&&html`<span class="dwby">사용자 지정</span>`}</span>
      ${editable&&html`<input class="dwi" type="text" maxlength="200" data-req="${d.id}"
          data-dtid="${t.task_id}" data-orig="${text}" value="${text}"
          aria-label="${t.task_id}번 태스크 완료 기준">
        <button class="dwbtn" type="button" data-act="editCriterion" data-id="${d.id}"
          data-task="${t.task_id}">수정</button>`}
    </div>`;
}

// ---- what the server found (3.0.0) -------------------------------------------
// On a completion report: the files a task said it produced, as the server found them.
// Everything else on the row is the agent's word; these lines are not.
//
// A file the task itself created or changed. One that was already there before the
// task proves the file exists, not that the task did anything - it gets its own line,
// but it does not move the task out of "the agent's word only".
function producedCheck(c){
  return !c.gone&&(c.state==='found'||c.state==='folder');
}
// What a line says for each state the server can report: its tone, its mark, its
// words, and whether the file's size goes with them. Worded as the chat text words it
// (evidence.describe_check).
const CHECK_SAYS={
  found:['ok','✔','이 태스크 중 생성/변경됨',true],
  old:['dim','·','작업 전부터 있던 파일 (이 태스크에서 바뀌지 않음)',true],
  folder:['ok','✔','폴더가 있음',false],
  empty:['warn','⚠','빈 파일',false],
  missing:['warn','⚠','찾을 수 없음',false],
  unknown:['dim','·','확인하지 못함 (응답 지연)',false],
};
const CHECK_GONE=['warn','⚠','보고 당시에는 있었으나 지금은 없음',false];
const CHECK_OUTSIDE=['dim','·','서버의 확인 범위 밖',false];
function checkLine(c){
  const known=Object.prototype.hasOwnProperty.call(CHECK_SAYS,c.state);
  const [tone,mark,words,sized]=c.gone?CHECK_GONE:known?CHECK_SAYS[c.state]:CHECK_OUTSIDE;
  const size=sized?sizeText(c.size):'';
  return html`<div title="${c.path}" class="ck ${tone}">${mark} ${baseName(c.path)}${size&&` · ${size}`} · ${words}</div>`;
}
function checksHtml(t){
  const withdrawn=t.claims_withdrawn||[];
  return html`${(t.checks||[]).map(checkLine)}
    ${withdrawn.length>0&&html`<div class="ck warn">⚠ 처음에 ${withdrawn.map(baseName).join(', ')} 을(를)
      결과 파일로 적었다가 뺐습니다 (서버가 찾지 못함)</div>`}
    ${t.echo&&html`<div class="ck warn">⚠ 증거가 태스크 완료 기준 문장을 거의 그대로 반복합니다</div>`}`;
}
// The claim the human is judging, then what the server found. Without it a completion
// report is a task list that says nothing about whether the work happened.
function evidence(t){
  const ev=(t.result_log||'').trim();
  return html`${ev?html`<div class="ev">→ ${ev}</div>`:html`<div class="ev none">(증거 기록 없음)</div>`}
    ${checksHtml(t)}`;
}
// Where to look first. Shown only when the server checked at least one file for this
// plan - otherwise every task would be "unconfirmed" and the line would say nothing.
function triage(d){
  const tasks=d.tasks||[];
  if(!tasks.some(t=>(t.checks||[]).length||(t.claims_withdrawn||[]).length))return '';
  const seen=tasks.filter(t=>(t.checks||[]).some(producedCheck)).length;
  const rest=tasks.length-seen;
  return html`<div class="triage">${tasks.length}개 중 ${seen}개는 태스크 중에 만들거나 바꾼 파일을
    서버가 확인했습니다.${rest>0&&html` 나머지 ${rest}개는 에이전트의 보고가 근거입니다.`}</div>`;
}

// ---- a request -----------------------------------------------------------------
// The header a human needs before they can judge a task list at all: what is being
// asked, what the goal is, and the model's own overview of how it intends to get there.
// All three used to arrive inside the pre-rendered `display` blob; rendering tasks as
// rows dropped that blob, so they are composed here from their own fields instead.
function header(d){
  const title=d.phase==='COMPLETION'?'완료 확인':'계획 승인 요청';
  return html`<h1>${title} · ${d.plan_id}</h1>${verNote(d)}
    <p class="goal"><span class="lbl">목표</span>${d.goal}</p>
    ${d.summary&&html`<div class="summary"><span class="lbl">개요</span>${d.summary}</div>`}`;
}
const RUN_STATE={DONE:'완료',IN_PROGRESS:'진행중',FAILED:'실패',PENDING:'대기'};
function runBadge(t){
  return html`<span class="badge${t.status==='IN_PROGRESS'?' now':''}">${RUN_STATE[t.status]||t.status}</span>`;
}
function taskRows(d){
  const tasks=d.tasks||[];
  return html`<p class="tasklabel">태스크 ${tasks.length}개</p>
    <div class="tasks">${tasks.map(t=>taskRow(d,t))}</div>`;
}
function taskRow(d,t){
  // On a completion report every task is DONE, so a DONE badge on every row is noise -
  // the evidence line below it already says so, and that is what needs the attention.
  const done=d.phase==='COMPLETION';
  const badge=t.status&&t.status!=='PENDING'&&!(done&&t.status==='DONE');
  const choose=!done&&hasChoice(t)&&t.chosen==null;
  // On a completion report a task that offered a choice says which one was carried
  // out, so the human can check it was done the way they picked.
  const picked=done&&hasChoice(t)&&t.chosen!=null;
  // A criterion can be written for any task still to be done. Finished work keeps the
  // one it was done under.
  const editable=!done&&t.status!=='DONE';
  const took=done?tookText(t.duration_sec):'';
  // The comment box is labelled with the name of the button that opens it, so it needs
  // no sample sentence inside - a grey one read as if something had already been written.
  const what=done?'다시 작업':'의견';
  // Under the title: the options; how the task was rewritten - on request, or after it
  // failed; its criterion; on a completion report, what it produced before the human
  // sent it back (the request alone shows what was asked, only this shows whether it
  // was answered) and the evidence; and the task's comment box.
  return html`<div class="task">
      <div class="tt">
        <span class="tn">${t.task_id}.</span>
        <span>${choose?choiceHeading(t):t.title}</span>
        ${picked&&html`<span class="badge">${t.chosen===0?'권장안':LETTERS[t.chosen]+'안 선택'}</span>`}
        ${badge&&runBadge(t)}
        ${took&&html`<span class="took">소요 ${took}</span>`}
        ${editable&&!t.done_when&&html`<button class="tcbtn dwadd" type="button" aria-expanded="false"
          data-act="toggleCriterion" data-id="${d.id}" data-task="${t.task_id}">완료 기준 추가</button>`}
        <button class="tcbtn" type="button" aria-expanded="false" data-act="toggleComment">${what}</button>
      </div>
      ${choose&&optionsHtml(d,t,true)}
      ${t.previous_title&&html`<div class="was">${t.previous_title}</div>`}
      ${t.revision_note&&html`<div class="note">↻ 요청하신 내용: ${t.revision_note}</div>`}
      ${t.failure_note&&html`<div class="note">✕ 이전 시도 실패: ${t.failure_note}</div>`}
      ${dwRow(d,t,editable)}
      ${done&&t.previous_result_log&&html`<div class="ev old">이전 결과: ${t.previous_result_log}</div>`}
      ${done&&evidence(t)}
      <div class="tcrow"><span class="dwl">${what}</span><textarea class="tc" data-req="${d.id}"
        data-tid="${t.task_id}" aria-label="${t.task_id}번 ${what}"></textarea></div>
    </div>`;
}
function chip(d){
  return d.agent_waiting
    ?html`<div class="chip live">에이전트가 대기 중입니다 · 결정하시면 작업이 바로 이어집니다</div>`
    :html`<div class="chip idle">에이전트가 대기를 멈췄습니다 · 지금 결정하셔도 반영되며,
      채팅창에 메시지를 입력하시면 진행이 재개됩니다</div>`;
}
// What the agent still wanted to reconsider while this request was open. It may not
// change the plan the human is reading, so it is shown here instead of being lost.
function agentNote(d){
  return d.agent_note&&html`<div class="summary agent"><span class="lbl">에이전트 추가 의견</span>${d.agent_note}</div>`;
}
// The circuit breaker stopped an agent that kept repeating a step. The human is not
// judging the plan's wording here but deciding how the agent continues - so no per-task
// review, and the button set depends on whether there is a draft to approve as it stands.
function haltCard(d){
  // origin 'user': the human stopped a running plan from its run card (3.1.0). Same
  // decisions as a breaker halt - continue, with or without a direction, or cancel -
  // under words that do not suggest the agent did something wrong.
  const tasks=d.tasks||[],user=d.origin==='user';
  // What is already done, for a plan the human stopped: it is what they weigh when
  // deciding whether the rest should run.
  const row=t=>html`<div class="task">
      <div class="tt">
        <span class="tn">${t.task_id}.</span>
        <span>${hasChoice(t)&&d.draft?choiceHeading(t):t.title}</span>
        ${t.status&&t.status!=='PENDING'&&runBadge(t)}
      </div>
      ${d.draft&&optionsHtml(d,t,false)}
      ${dwRow(d,t,false)}
      ${user&&t.status==='DONE'&&(t.result_log||'').trim()&&html`<div class="ev">→ ${t.result_log.trim()}</div>`}
    </div>`;
  return html`<h1>${user?'실행 멈춤':'반복 감지'} · ${d.plan_id}</h1>${verNote(d)}
    <p class="goal"><span class="lbl">목표</span>${d.goal}</p>
    <div class="summary warn"><span class="lbl">${user?'멈춘 이유':'에이전트가 멈춘 이유'}</span>${d.summary}</div>
    ${tasks.length>0&&html`<p class="tasklabel">${d.draft?'현재 초안 · 태스크 ':'태스크 '}${tasks.length}개</p>
      <div class="tasks">${tasks.map(row)}</div>`}
    ${chip(d)}
    <textarea id="c-${d.id}" placeholder="에이전트에게 전할 방향을 입력해 주십시오 (선택 사항)"></textarea>
    <div class="row">
      ${d.draft&&decideButton(d,'ok','APPROVED','이 초안으로 승인')}
      ${decideButton(d,'rev','REVISE',haltLabel(false))}
      ${decideButton(d,'no','REJECTED',user?'계획 취소':'취소')}
    </div>`;
}

// ---- a plan that is running (3.1.0) ---------------------------------------------
// Between the plan approval and the completion report there used to be nothing here.
// This card is not a question, so it raises no alarm: it shows what is finished and what
// is in progress, and lets the human stop the plan before its next task or change what
// is left. Either takes effect when the agent next reports a task - the server cannot
// interrupt a tool that is already running, and the page says so.
function runId(r){return 'run-'+r.plan_id;}
function runRows(r){
  const tasks=r.tasks||[],fin=tasks.filter(t=>t.status==='DONE').length;
  const row=t=>{
    const took=t.status==='DONE'?tookText(t.duration_sec):'';
    // The time taken goes before the state, so every row ends on its state label.
    return html`<div class="task">
        <div class="tt">
          <span class="tn">${t.task_id}.</span><span>${t.title}</span>
          ${took&&html`<span class="took">소요 ${took}</span>`}${runBadge(t)}
        </div>
        ${dwRow({id:runId(r)},t,false)}
        ${t.status==='DONE'&&evidence(t)}
      </div>`;
  };
  return html`<p class="tasklabel">태스크 ${tasks.length}개 · 완료 ${fin}개</p>
    <div class="tasks">${tasks.map(row)}</div>`;
}
function runCard(r){
  const c=r.control,seenAt=clock(r.updated_at);
  const head=html`<h1>실행 중 · ${r.plan_id}</h1>${verNote(r)}
    <p class="goal"><span class="lbl">목표</span>${r.goal}</p>${runRows(r)}
    ${seenAt&&html`<p class="seen">에이전트의 마지막 보고 ${seenAt}</p>`}`;
  // Asked for, not yet applied: say exactly that, and let it be taken back - or, when
  // the agent is not coming back to apply it, close the plan without it.
  if(c){
    const asked=c.action==='PAUSE'
      ?'멈춤을 요청하셨습니다. 에이전트가 진행 중인 태스크를 보고하면 다음 태스크를 시작하지 않고 멈춥니다.'
      :'의견을 전달하셨습니다. 에이전트가 진행 중인 태스크를 보고하면, 남은 태스크를 의견에 맞게 고쳐 '+
       '다시 승인을 요청합니다.';
    return html`${head}<div class="pendingctl">${asked} 에이전트가 오류로 멈춰 보고하지 않으면
        계획을 강제로 취소하실 수 있습니다.${c.comment&&html`<span class="q">${c.comment}</span>`}
      <div class="ctlrow">${controlButton(r,'linkbtn','CLEAR','요청 취소')}${killBtn(r)}</div></div>`;
  }
  return html`${head}<textarea id="c-${runId(r)}" placeholder="남은 태스크를 바꾸고 싶으실 때 의견을
      입력해 주십시오 (예: 요약은 표로 정리해 주세요)"></textarea>
    <div class="row">${controlButton(r,'rev','NOTE','의견 전달')}${controlButton(r,'no','PAUSE','멈춤')}</div>
    <div class="ctlrow end">${killBtn(r)}</div>`;
}
// 3.2.0 - a stop and a note wait for the agent's next report. An agent that failed in
// the middle of a plan never makes one, and its card stayed here for good. This closes
// the plan without it: the card goes at once, the plan on the next tool call.
function killBtn(r){return controlButton(r,'linkbtn kill','CANCEL','계획 강제 취소');}

// ---- the page around the cards ---------------------------------------------------
// Matches the placeholder in page.html, so the page does not flicker between two
// different wordings when the first poll lands.
const IDLE_HTML=html`<div class="idle">현재 대기 중인 승인 요청이 없습니다.<br>
  <span style="font-size:.85rem">에이전트가 계획을 제출하면 이곳에 표시됩니다.</span></div>`;
// The notes under the cards: each is there only while it applies.
const RUN_HINT=html`실행 중인 계획은 에이전트가 태스크를 보고할 때마다 갱신됩니다. [멈춤]과
  [의견 전달]은 에이전트가 진행 중인 태스크를 보고하는 시점에 적용되며, 이미 실행 중인 도구는
  중단되지 않습니다.<br>에이전트가 오류로 멈춰 보고하지 않으면 [계획 강제 취소]로 계획을 닫으실 수
  있습니다. 카드는 바로 사라지고, 계획은 다음 도구 호출 때 취소됩니다.<br>`;
const CHOICE_HINT=html`선택지가 있는 태스크는 [권장]안이 기본으로 선택되어 있습니다. 다른 안을 고르면
  승인 버튼에 반영 내용이 표시되고, 기타를 고르면 수정 요청으로 바뀝니다.<br>`;
const REVIEW_HINT=html`태스크의 [의견] 또는 [다시 작업] 버튼을 누르면 해당 태스크에만 요청을 남기실 수
  있습니다. 완료 보고 단계에서는 지정하신 태스크만 다시 실행되며, 나머지 태스크의 결과는 그대로
  유지됩니다.<br>의견을 적으시면 [승인] 대신 [수정 요청] 버튼이 나타납니다.<br>`;
const CRITERION_HINT=html`[완료 기준 추가] 또는 [수정]으로 태스크 완료 기준을 직접 적으실 수
  있습니다. 적은 기준은 승인과 함께 반영되며, 수정 요청을 거치지 않습니다.<br>`;
const WAIT_HINT=html`결정하시기 전까지 해당 에이전트는 후속 작업을 진행하지 못합니다. 요청은 응답하실
  때까지 사라지지 않으니 천천히 검토해 주시기 바랍니다.`;
function hintHtml(list,runs){
  const anyChoice=list.some(d=>!d.decided&&(d.tasks||[]).some(hasChoice));
  const anyPlan=list.some(d=>!d.decided&&d.phase==='PLAN'&&(d.tasks||[]).length>0);
  return html`${runs.length>0&&RUN_HINT}${list.length>0&&html`${anyChoice&&CHOICE_HINT}${REVIEW_HINT}
    ${anyPlan&&CRITERION_HINT}${WAIT_HINT}`}`;
}

// ---- one request, whatever it asks ------------------------------------------------
function requestCard(d){
  if(d.decided){
    const label=(d.phase==='HALT'?HALT_DONE_LABEL:DONE_LABEL)[d.decided]||d.decided;
    return html`<div class="done">${d.plan_id} — ${label}<br>
      <span style="font-weight:400;opacity:.6;font-size:.9rem">에이전트가 이 결정을 반영합니다.</span></div>`;
  }
  if(d.phase==='HALT')return haltCard(d);
  // An entry with no phase was published by an older server process on this same
  // state directory. It has no per-task review, so fall back to the original form.
  const perTask=(d.phase==='PLAN'||d.phase==='COMPLETION')&&!!d.tasks&&d.tasks.length>0;
  // The fallback keeps the original layout on purpose: `display` already opens with its
  // own title, 목표 and summary, so composing a header above it would repeat all three.
  const top=perTask
    ?html`${header(d)}${d.phase==='COMPLETION'&&triage(d)}${taskRows(d)}`
    :html`<h1>${d.phase==='COMPLETION'?'완료 확인':'승인 요청'} · ${d.plan_id}</h1>${verNote(d)}
      <p class="goal">${d.goal}</p><pre>${d.display}</pre>`;
  const scope=d.phase==='COMPLETION'?'계획 자체를 다시 세우기':'계획 전체를 다시 세우기';
  return html`${top}${agentNote(d)}${chip(d)}
    <textarea id="c-${d.id}" placeholder="전체 의견을 입력해 주십시오 (거절 사유도 여기에 입력하실 수 있습니다)"></textarea>
    ${perTask&&html`<label class="scope"><input type="checkbox" id="all-${d.id}"> ${scope} (태스크 추가·삭제·순서 변경 시 선택)</label>`}
    <div class="row">
      ${decideButton(d,'ok','APPROVED','승인')}
      ${decideButton(d,'rev','REVISE','수정 요청',true)}
      ${decideButton(d,'no','REJECTED','거절')}
    </div>`;
}
