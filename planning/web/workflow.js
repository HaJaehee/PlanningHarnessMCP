// Approval page - the workflow diagram (3.3.0, ADVANCED_WORKFLOW_PLANNING=1).
//
// Loaded only when the flag is on, after app.js, together with vendor/mermaid.js. It adds
// what the diagram needs by wrapping functions of the other scripts (taskRows, restore,
// hasOpinion, scopeOf, relabel, reqSig, hintHtml, decide) rather than by editing them, so
// the page a server with the flag off serves is byte for byte the page of 3.2.
//
// What the human sees: the plan as a flowchart above its task list. Pressing a node opens
// a comment box for it - for a task's node, that task's own box below, so one task never
// has two. A comment on the drawing turns the approve button into 수정 요청(워크플로우).
//
// Mermaid never sees the model's text: the server sends canonical Mermaid that its guard
// (planning/workflow.py) wrote. If mermaid.js is missing or cannot draw it anyway, the
// source is shown instead and every node can still be commented on from a list.

// ---- drawing -----------------------------------------------------------------------
const WF={svg:{},failed:{},pending:{}};  // request id -> drawn svg / why not / in progress
let wfInited=false;
function wfAvailable(){return typeof mermaid!=='undefined'&&!!mermaid&&!!mermaid.render;}
function wfDark(){
  try{return window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;}
  catch(e){return false;}
}
function wfInit(){
  if(wfInited||!wfAvailable())return wfInited;
  try{
    // strict: labels are text, no click handlers, no links - on top of the server
    // having already removed every click, style and directive the model wrote.
    mermaid.initialize({startOnLoad:false,securityLevel:'strict',
      theme:wfDark()?'dark':'default',flowchart:{htmlLabels:false,useMaxWidth:true}});
    wfInited=true;
  }catch(e){WF.initError=String(e&&e.message||e);}
  return wfInited;
}
function wfNodes(d){return (d.workflow&&d.workflow.nodes)||[];}
function wfNode(d,id){return wfNodes(d).find(n=>n.id===id)||null;}
function wfTask(n){return n&&typeof n.task_id==='number'?n.task_id:null;}
// The id Mermaid gave a node's group: "flowchart-D1-3" -> "D1".
function wfNodeIdOf(el){
  const m=/^flowchart-(.+)-\d+$/.exec(el.id||'');
  return m?m[1]:null;
}

// ---- the box above the task list -------------------------------------------------------
function wfByNote(d){
  const w=d.workflow;
  if(w.by!=='server')return '';
  return html`<p class="wfby">${w.raw?'에이전트가 보낸 워크플로우를 그릴 수 없어 서버가 태스크 순서대로 그렸습니다.'
      :'에이전트가 워크플로우를 보내지 않아 서버가 태스크 순서대로 그렸습니다.'}</p>
    ${w.raw&&html`<details class="wfraw"><summary>에이전트가 보낸 원문</summary><pre>${w.raw}</pre></details>`}`;
}
// What the human asked of each node last time, so they can check it was done.
function wfAsked(d){
  const asked=d.workflow.asked||{};
  const ids=Object.keys(asked);
  if(!ids.length)return '';
  return html`${ids.map(id=>{
    const n=wfNode(d,id);
    const where=id==='*'?'워크플로우 전체':`${id}${n?' · '+n.label:''}`;
    return html`<div class="note">↻ 요청하신 내용 (${where}): ${asked[id]}</div>`;
  })}`;
}
// One comment row per node that is not a task (a task's node opens the task's own box).
function wfCommentRow(d,n){
  return html`<div class="wfc hid" data-wrow="${n.id}">
      <span class="dwl">${n.id} · ${n.label}</span>
      <textarea class="wfta" data-wreq="${d.id}" data-node="${n.id}"
        aria-label="${n.id} ${n.label} 의견"></textarea>
    </div>`;
}
function workflowBox(d){
  const others=wfNodes(d).filter(n=>wfTask(n)===null);
  return html`<div class="wf" id="wfbox-${d.id}">
      <p class="tasklabel">워크플로우</p>
      ${wfByNote(d)}
      <div class="wfsvg" id="wf-${d.id}" aria-label="워크플로우 다이어그램">그리는 중…</div>
      ${wfAsked(d)}
      <p class="wfhint">노드를 누르면 그 단계에 의견을 남기실 수 있습니다. 태스크 노드는 아래 해당 태스크의
        의견 칸이 열립니다.</p>
      <div class="wfcs">${others.map(n=>wfCommentRow(d,n))}
        <div class="wfc" data-wrow="*"><span class="dwl">워크플로우 전체 의견</span>
          <textarea class="wfta" data-wreq="${d.id}" data-node="*"
            aria-label="워크플로우 전체 의견"></textarea></div>
      </div>
    </div>`;
}
// Without a drawing: the source, and the nodes as buttons so each can still be commented on.
function wfFallback(d,why){
  return html`<p class="wfby">다이어그램을 그리지 못했습니다 (${why}). 아래는 워크플로우 원문입니다.</p>
    <pre>${d.workflow.text}</pre>
    <div class="wfchips">${wfNodes(d).map(n=>html`<button class="tcbtn" type="button"
      data-act="wfOpen" data-id="${d.id}" data-node="${n.id}">${n.id} · ${n.label}</button>`)}</div>`;
}

// ---- putting the drawing in -------------------------------------------------------------
function wfCard(id){return (state.wfCards||{})[id];}
function wfPlace(d){
  const box=document.getElementById('wf-'+d.id);
  if(!box)return;
  if(WF.svg[d.id]){box.innerHTML=WF.svg[d.id];wfDecorate(d);return;}
  if(WF.failed[d.id]){box.innerHTML=String(wfFallback(d,WF.failed[d.id]));return;}
  if(!wfInit()){
    // Not the guard's fault: the bundled file is missing or was refused (see the log).
    WF.failed[d.id]=WF.initError?'mermaid 초기화 실패':'mermaid.js를 불러오지 못함';
    box.innerHTML=String(wfFallback(d,WF.failed[d.id]));
    return;
  }
  if(WF.pending[d.id])return;
  WF.pending[d.id]=true;
  // mermaid.render draws off-screen, so a redraw of the card meanwhile loses nothing:
  // the result is put into whichever box carries this id when it is done.
  Promise.resolve().then(()=>mermaid.render('wfg-'+d.id,d.workflow.text)).then(out=>{
    // The one piece of markup on the page that is not an html`` template: SVG from
    // mermaid in strict mode, drawn from text this server's guard wrote.
    WF.svg[d.id]=out.svg;
  },err=>{
    WF.failed[d.id]='문법 오류';
    wfReport(d,String(err&&err.message||err).slice(0,300));
  }).then(()=>{WF.pending[d.id]=false;const c=wfCard(d.id);if(c)wfPlace(c);});
}
// A drawing that failed is a bug in the guard, not the model's slip: the server records it.
function wfReport(d,why){
  try{
    fetch('/api/workflow-failed',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:d.id,plan_id:d.plan_id,error:why})});
  }catch(e){}
}
// Marks on the drawn nodes: which can be pressed, which carry a comment, which changed.
function wfDecorate(d){
  const box=document.getElementById('wf-'+d.id);
  if(!box)return;
  const changed=(d.workflow.changed||[]);
  box.querySelectorAll('g.node').forEach(g=>{
    const nid=wfNodeIdOf(g),n=nid&&wfNode(d,nid);
    if(!n)return;
    g.setAttribute('data-node',nid);
    g.setAttribute('data-wreq',d.id);
    g.setAttribute('tabindex','0');
    g.setAttribute('role','button');
    g.setAttribute('aria-label',`${nid} ${n.label} 의견`);
    g.classList.toggle('wfchanged',changed.indexOf(nid)>=0);
  });
  wfMark(d.id);
}
function wfMark(id){
  const box=document.getElementById('wf-'+id);
  if(!box)return;
  const said=wfComments(id),tasks=comments(id),d=wfCard(id);
  box.querySelectorAll('g.node[data-node]').forEach(g=>{
    const nid=g.getAttribute('data-node'),t=d&&wfTask(wfNode(d,nid));
    g.classList.toggle('wfmark',!!said[nid]||(t!==null&&!!tasks[String(t)]));
  });
}

// ---- comments on the drawing ------------------------------------------------------------
function wfBoxes(id){return document.querySelectorAll('textarea.wfta[data-wreq="'+id+'"]');}
function wfComments(id){
  const out={};
  wfBoxes(id).forEach(b=>{const v=b.value.trim();if(v)out[b.getAttribute('data-node')]=v;});
  return out;
}
function wfAny(id){return Object.keys(wfComments(id)).length>0;}
function wfShowRow(id,nid,focus){
  const ta=document.querySelector('textarea.wfta[data-wreq="'+id+'"][data-node="'+nid+'"]');
  if(!ta)return;
  const row=ta.closest('.wfc');
  if(row)row.classList.remove('hid');
  if(focus)ta.focus();
}
// A node was pressed: a task's opens that task's box; any other its own row.
function wfOpen(id,nid){
  const d=wfCard(id),n=d&&wfNode(d,nid);
  if(!n)return;
  const t=wfTask(n);
  if(t!==null){
    openComment(id,String(t),true);
    const ta=document.querySelector('textarea.tc[data-req="'+id+'"][data-tid="'+t+'"]');
    if(ta&&ta.scrollIntoView)ta.scrollIntoView({block:'center'});
    return;
  }
  wfShowRow(id,nid,true);
}
function wfOnClick(ev){
  const g=ev.target.closest&&ev.target.closest('g.node[data-node]');
  if(g){wfOpen(g.getAttribute('data-wreq'),g.getAttribute('data-node'));return;}
  const b=ev.target.closest&&ev.target.closest('button[data-act="wfOpen"]');
  if(b&&!b.disabled)wfOpen(b.dataset.id,b.dataset.node);
}
function wfOnKey(ev){
  if(ev.key!=='Enter'&&ev.key!==' ')return;
  const g=ev.target.closest&&ev.target.closest('g.node[data-node]');
  if(!g)return;
  ev.preventDefault();
  wfOpen(g.getAttribute('data-wreq'),g.getAttribute('data-node'));
}
function wfOnInput(ev){
  const el=ev.target;
  if(!el||!el.classList||!el.classList.contains('wfta'))return;
  const id=el.getAttribute('data-wreq');
  dset(id,'wf:'+el.getAttribute('data-node'),el.value);
  relabel(id);
}
function wfOnStorage(ev){
  if(!ev.key||ev.key.indexOf(DRAFT)!==0)return;
  const rest=ev.key.slice(DRAFT.length),cut=rest.indexOf(':');
  if(cut<0)return;
  const id=rest.slice(0,cut),tid=rest.slice(cut+1);
  if(tid.indexOf('wf:')!==0)return;
  const ta=document.querySelector('textarea.wfta[data-wreq="'+id+'"][data-node="'+tid.slice(3)+'"]');
  if(!ta||ta===document.activeElement)return;
  ta.value=ev.newValue||'';
  if(ta.value.trim())wfShowRow(id,tid.slice(3),false);
  relabel(id);
}

// ---- what the button says ------------------------------------------------------------------
// 수정 요청(워크플로우), then one line per thing it carries - the rule of labels.js.
function wfLabel(nodes,tasks,crit){
  const lines=['수정 요청(워크플로우)'];
  const n=nodes.filter(k=>k!=='*').length;
  if(n)lines.push('노드 '+n+'개');
  if(nodes.indexOf('*')>=0)lines.push('워크플로우 전체 의견');
  if(tasks.length)lines.push(tasks.length===1?tasks[0]+'번 태스크':tasks.length+'개 태스크');
  if(crit)lines.push('태스크 완료 기준 '+crit+'건 반영');
  return lines.join('\n');
}

// ---- the other scripts, extended ------------------------------------------------------------
// Each wrapper does what the original does for a card without a workflow.
const wfBase={taskRows,restore,hasOpinion,scopeOf,relabel,reqSig,hintHtml};
taskRows=function(d){
  const inner=wfBase.taskRows(d);
  if(d.phase!=='PLAN'||!d.workflow||d.decided)return inner;
  return html`${workflowBox(d)}${inner}`;
};
restore=function(d){
  if(d.workflow&&d.phase==='PLAN'&&!d.decided){
    state.wfCards=state.wfCards||{};
    state.wfCards[d.id]=d;
    wfBoxes(d.id).forEach(b=>{
      b.value=dget(d.id,'wf:'+b.getAttribute('data-node'));
      if(b.value.trim())wfShowRow(d.id,b.getAttribute('data-node'),false);
    });
  }
  wfBase.restore(d);
  if(d.workflow&&d.phase==='PLAN'&&!d.decided)wfPlace(d);
};
hasOpinion=function(id){return wfBase.hasOpinion(id)||wfAny(id);};
scopeOf=function(id){
  if(wholeOf(id))return 'PLAN';
  return wfAny(id)?'WORKFLOW':wfBase.scopeOf(id);
};
relabel=function(id){
  wfBase.relabel(id);
  if(state.phase[id]!=='PLAN')return;
  wfMark(id);
  const btn=document.getElementById('rev-'+id);
  if(!btn||wholeOf(id)||!wfAny(id))return;
  setLabel(btn,wfLabel(Object.keys(wfComments(id)),Object.keys(comments(id)),
    Object.keys(criteriaOf(id)).length));
  btn.hidden=false;
};
// A card drawn before this script was read had no diagram: the suffix redraws it.
reqSig=function(d){return wfBase.reqSig(d)+(d.workflow?':wf':'');};
const WF_HINT=html`워크플로우의 노드를 누르면 그 단계에 의견을 남기실 수 있습니다. 의견을 적으시면 [승인]
  대신 [수정 요청(워크플로우)] 버튼이 나타납니다.<br>`;
hintHtml=function(list,runs){
  const any=list.some(d=>!d.decided&&d.phase==='PLAN'&&!!d.workflow);
  return html`${any&&WF_HINT}${wfBase.hintHtml(list,runs)}`;
};
// app.js's decide, with the workflow comments in the body. Kept the same line for line
// otherwise - tests/test_workflow_page.py compares the two.
decide=async function(id,dec){
  if(state.busy)return;state.busy=true;alertOff();
  lock('q-'+id);
  const box=document.getElementById('c-'+id);
  const c=box?box.value:'';
  try{
    const r=await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id,decision:dec,comment:c,
        task_comments:comments(id),scope:scopeOf(id),choices:choicesOf(id),
        criteria:criteriaOf(id),workflow_comments:wfComments(id)})});
    const j=await r.json();
    if(j&&j.ok){dclear(id);state.lastError='';}
    else state.lastError='결정을 기록하지 못했습니다. 화면이 최신이 아닐 수 있으니 잠시 후 다시 시도해 주십시오.';
  }catch(e){state.lastError='결정을 전송하지 못했습니다. 다시 시도해 주십시오.';}
  state.busy=false;stale('q-'+id);poll();
};

// ---- start --------------------------------------------------------------------------------
document.getElementById('root').addEventListener('click',wfOnClick);
document.getElementById('root').addEventListener('keydown',wfOnKey);
document.getElementById('root').addEventListener('input',wfOnInput);
window.addEventListener('storage',wfOnStorage);
