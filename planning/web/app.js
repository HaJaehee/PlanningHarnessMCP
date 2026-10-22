// Approval page - talking to the server, and starting up. Loaded last: the wiring at
// the bottom of this file runs as soon as it is read, and uses every other script.

// ---- noticing a request ---------------------------------------------------------
const IDLE_TITLE='planning-mcp 승인';
let flash=null;  // the title's blink timer while a request waits
// A popup can be blocked, land on another monitor, or open behind other windows.
// So the page makes itself noticeable instead: the tab title flashes and a short tone
// plays. Leaving this tab open is the reliable way to catch approval requests.
function alertOn(){
  if(flash)return;
  let on=false;
  flash=setInterval(()=>{on=!on;document.title=on?
    '\u26A0 승인 대기 '+state.pendingCount+'건':IDLE_TITLE;},700);
  try{
    const C=window.AudioContext||window.webkitAudioContext;if(!C)return;
    const ctx=new C();const o=ctx.createOscillator();const g=ctx.createGain();
    o.connect(g);g.connect(ctx.destination);o.frequency.value=880;g.gain.value=0.08;
    o.start();o.stop(ctx.currentTime+0.18);
    setTimeout(()=>{try{ctx.close();}catch(e){}},400);
  }catch(e){}
}
function alertOff(){
  if(flash){clearInterval(flash);flash=null;}
  document.title=IDLE_TITLE;
}

// ---- the server -----------------------------------------------------------------
async function poll(){
  if(state.busy)return;
  try{
    const r=await fetch('/api/pending');const d=await r.json();
    const list=d.requests||[];
    state.runs=d.runs||[];
    dprune(list.map(x=>x.id).concat(state.runs.map(runId)));
    const undecided=list.filter(x=>!x.decided);
    state.pendingCount=undecided.length;
    // Nothing on the page changed: nothing is touched, and the tone is not played again.
    if(!draw(list))return;
    // A running plan is not a question, so it never raises the alarm.
    if(undecided.length)alertOn();else alertOff();
  }catch(e){}
}
const KILL_ASK='실행 중인 계획을 강제로 취소하시겠습니까?\n\n에이전트의 보고를 기다리지 않고 '+
  '계획을 닫습니다. 되돌릴 수 없으며, 에이전트가 다시 보고하면 계획이 취소되었다는 '+
  '답을 받습니다.';
// A stop, a note, taking either back, or closing the plan, for a running plan's card.
async function control(pid,action){
  if(state.busy)return;
  const id='run-'+pid,box=document.getElementById('c-'+id);
  const text=box?box.value.trim():'';
  if(action==='NOTE'&&!text){
    if(box){box.placeholder='전달하실 의견을 먼저 입력해 주십시오';box.focus();}
    return;
  }
  if(action==='CANCEL'&&!confirm(KILL_ASK))return;
  state.busy=true;
  lock(id);
  // Taking a request back puts its words back in the box: nothing typed is dropped.
  const run=state.runs.find(r=>r.plan_id===pid);
  const back=action==='CLEAR'&&run&&run.control?(run.control.comment||''):'';
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({plan_id:pid,action:action,comment:text})});
    const j=await r.json();
    if(j&&j.ok){
      if(action==='CLEAR'){if(back)dset(id,'_all',back);}else dclear(id);
      state.lastError='';
    }else state.lastError='요청을 기록하지 못했습니다. 계획이 이미 다음 단계로 넘어갔을 수 있으니 '+
      '화면을 확인해 주십시오.';
  }catch(e){state.lastError='요청을 전송하지 못했습니다. 다시 시도해 주십시오.';}
  state.busy=false;stale(id);poll();
}
async function decide(id,dec){
  if(state.busy)return;state.busy=true;alertOff();
  // Only this card's buttons, which its next redraw replaces - see lock().
  lock('q-'+id);
  const box=document.getElementById('c-'+id);
  const c=box?box.value:'';
  // Task comments travel with every decision, not just a targeted one: even when the
  // whole plan is being rewritten, what the human said about task 3 is useful context.
  try{
    const r=await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id,decision:dec,comment:c,
        task_comments:comments(id),scope:scopeOf(id),choices:choicesOf(id),
        criteria:criteriaOf(id)})});
    const j=await r.json();
    // Only once the decision is recorded. Clearing first would lose the text if it was
    // not, and this is the one copy of it.
    if(j&&j.ok){dclear(id);state.lastError='';}
    else state.lastError='결정을 기록하지 못했습니다. 화면이 최신이 아닐 수 있으니 잠시 후 다시 시도해 주십시오.';
  }catch(e){state.lastError='결정을 전송하지 못했습니다. 다시 시도해 주십시오.';}
  state.busy=false;stale('q-'+id);poll();
}

// ---- information dialog -------------------------------------------------------
// The small "i" beside the version: author, contact and version. A native <dialog>, so
// Esc closes it and focus stays inside it; a click on the backdrop closes it too (the
// dialog itself has no padding, so only the backdrop has it as the click target).
function aboutOpen(){
  const d=document.getElementById('about');
  if(d.showModal){if(!d.open)d.showModal();}
  else alert(d.textContent.trim().replace(/\s*\n\s*/g,'\n').replace(/\n닫기$/,''));
}
function aboutClose(){const d=document.getElementById('about');if(d.open)d.close();}

// ---- clicks -------------------------------------------------------------------
// A button on a card says what it does in data-act and carries its arguments in data-*
// (views.js). One listener runs it, so no handler is written into the markup - an id
// there once had to be escaped three times over: for HTML, for JavaScript, for Python.
const ACTIONS={
  decide:b=>decide(b.dataset.id,b.dataset.decision),
  control:b=>control(b.dataset.plan,b.dataset.control),
  toggleComment:b=>toggleComment(b),
  toggleCriterion:b=>toggleCriterion(b,b.dataset.id,b.dataset.task),
  editCriterion:b=>editCriterion(b,b.dataset.id,b.dataset.task),
};
function onClick(ev){
  const b=ev.target.closest&&ev.target.closest('button[data-act]');
  if(!b||b.disabled||!Object.prototype.hasOwnProperty.call(ACTIONS,b.dataset.act))return;
  ACTIONS[b.dataset.act](b);
}

// ---- start --------------------------------------------------------------------
// Bound to #root rather than to each card: #root outlives every redraw, so the
// listeners survive whichever cards come, go or are drawn again.
document.getElementById('root').addEventListener('click',onClick);
document.getElementById('root').addEventListener('input',onInput);
document.getElementById('root').addEventListener('change',onInput);
window.addEventListener('storage',onStorage);
document.getElementById('about-open').addEventListener('click',aboutOpen);
document.getElementById('about-close').addEventListener('click',aboutClose);
document.getElementById('about').addEventListener('click',ev=>{
  if(ev.target===ev.currentTarget)aboutClose();});
poll();setInterval(poll,1500);
