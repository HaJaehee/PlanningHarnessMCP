let busy=false,flash=null,pendingCount=0,lastError='';
// The version of the server that is serving this page (filled in when it is served).
const VERSION='__PLANNING_MCP_VERSION__';
// A request can be asked by another planning-mcp process on the same state directory,
// and after an upgrade that process may still be running older code. Nothing is said
// when the versions agree - which is nearly always.
function verNote(d){
  if(d.version===VERSION)return '';
  return '<p class="vernote">'+(d.version
    ?'이 요청은 planning-mcp '+esc(d.version)+' 서버가 보냈습니다. '
    :'이 요청을 보낸 서버는 버전을 남기지 않는 이전 버전입니다. ')+
    '이 페이지는 '+esc(VERSION)+'입니다.</p>';
}
const IDLE_TITLE='planning-mcp 승인';
// A popup can be blocked, land on another monitor, or open behind other windows.
// So the page makes itself noticeable instead: the tab title flashes and a short tone
// plays. Leaving this tab open is the reliable way to catch approval requests.
function alertOn(){
  if(flash)return;
  let on=false;
  flash=setInterval(()=>{on=!on;document.title=on?
    '\u26A0 승인 대기 '+pendingCount+'건':IDLE_TITLE;},700);
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
// ---- drafts --------------------------------------------------------------
// What the human has typed but not yet submitted lives in localStorage, not in the DOM.
// The page rebuilds itself whenever the queue changes - a second session asking for
// approval is enough - and rebuilding used to throw away a half-written comment with no
// trace. Keeping drafts outside the DOM means they survive that, plus a reload, plus
// closing the tab. localStorage is shared per origin, so the `storage` event below also
// keeps two open tabs showing the same text: whichever one the human finally submits
// from, it carries what they wrote.
const DRAFT='planning-mcp:draft:';
function dkey(req,tid){return DRAFT+req+':'+tid;}
function dget(req,tid){
  try{const v=localStorage.getItem(dkey(req,tid));return v===null?'':v;}catch(e){return '';}
}
function dset(req,tid,value){
  // Empty strings are stored, never removed: "I deleted what I wrote" is a state that
  // has to survive a re-render too, or the old text comes back.
  try{localStorage.setItem(dkey(req,tid),value);}catch(e){}
}
function dkeys(){
  const out=[];
  try{
    for(let i=0;i<localStorage.length;i++){
      const k=localStorage.key(i);
      if(k&&k.indexOf(DRAFT)===0)out.push(k);
    }
  }catch(e){}
  return out;
}
function dclear(req){
  try{dkeys().forEach(k=>{if(k.indexOf(DRAFT+req+':')===0)localStorage.removeItem(k);});}catch(e){}
}
// Drafts for requests that have left the queue would otherwise sit there forever and
// reappear if an id were ever reused.
function dprune(ids){
  try{
    dkeys().forEach(k=>{
      const rest=k.slice(DRAFT.length);
      const cut=rest.indexOf(':');
      if(cut>0&&ids.indexOf(rest.slice(0,cut))<0)localStorage.removeItem(k);
    });
  }catch(e){}
}
function taskBoxes(req){
  return document.querySelectorAll('textarea.tc[data-req="'+req+'"]');
}
// Puts what the human had typed back into ONE card, right after that card is drawn. A
// card that was not redrawn still holds it in the DOM and is left alone.
function restore(d){
  if(d.decided)return;
  const all=document.getElementById('c-'+d.id);
  if(all)all.value=dget(d.id,'_all');
  const whole=document.getElementById('all-'+d.id);
  if(whole)whole.checked=dget(d.id,'_whole')==='1';
  taskBoxes(d.id).forEach(b=>{
    b.value=dget(d.id,b.getAttribute('data-tid'));
    // A restored comment has to be visible, or the human sends a targeted revision
    // they cannot see. relabel() marks the row; opening it lets them edit it.
    if(b.value.trim()){
      const task=b.closest('.task');
      if(task){
        task.classList.add('open');
        const btn=task.querySelector('.tcbtn:not(.dwadd)');
        if(btn)btn.setAttribute('aria-expanded','true');
      }
    }
  });
  // The human's picks survive a rebuild like their comments do (D22).
  document.querySelectorAll('input[type=radio][data-req="'+d.id+'"]').forEach(r=>{
    const v=dget(d.id,'ch'+r.getAttribute('data-ctid'));
    if(v!==''&&r.value===v){
      r.checked=true;
      if(v==='other')openComment(d.id,r.getAttribute('data-ctid'),false);
    }
  });
  // So does a criterion they wrote. "No draft" and "erased it" are different states
  // here - erasing removes the criterion - so the raw item is read, not dget's ''.
  dwInputs(d.id).forEach(i=>{
    let v=null;
    try{v=localStorage.getItem(dkey(d.id,'dw'+i.getAttribute('data-dtid')));}catch(e){}
    if(v!==null&&v!==(i.getAttribute('data-orig')||'')){i.value=v;showCriterion(i);}
  });
  relabel(d.id);
}
// One delegated listener on a container that outlives every re-render, so no inline
// handler has to carry an escaped id.
function onInput(ev){
  const el=ev.target;
  if(!el)return;
  if(el.type==='radio'&&el.hasAttribute('data-ctid')){
    const req=el.getAttribute('data-req'),tid=el.getAttribute('data-ctid');
    dset(req,'ch'+tid,el.value);
    // Something other than the options on offer is a change to the task itself, which
    // is what the per-task comment and the REVISE button already handle.
    if(el.value==='other')openComment(req,tid,true);
    relabel(req);
    return;
  }
  if(el.type==='checkbox'&&el.id.indexOf('all-')===0){
    const req=el.id.slice(4);
    dset(req,'_whole',el.checked?'1':'');
    relabel(req);
    return;
  }
  if(el.classList&&el.classList.contains('dwi')){
    const req=el.getAttribute('data-req');
    dset(req,'dw'+el.getAttribute('data-dtid'),el.value);
    relabel(req);
    return;
  }
  if(el.tagName!=='TEXTAREA')return;
  const req=el.getAttribute('data-req');
  if(req){dset(req,el.getAttribute('data-tid'),el.value);relabel(req);return;}
  if(el.id.indexOf('c-')===0){
    const id=el.id.slice(2);
    dset(id,'_all',el.value);
    // A halt card's continue button says whether it carries a direction.
    relabel(id);
  }
}
// Another tab wrote a draft. Mirror it here so both windows show the same thing - the
// property the old full-rebuild had by accident, kept on purpose.
function onStorage(ev){
  if(!ev.key||ev.key.indexOf(DRAFT)!==0)return;
  const rest=ev.key.slice(DRAFT.length);
  const cut=rest.indexOf(':');
  if(cut<0)return;
  const req=rest.slice(0,cut),tid=rest.slice(cut+1),value=ev.newValue||'';
  if(tid==='_whole'){
    const box=document.getElementById('all-'+req);
    if(box)box.checked=value==='1';
    relabel(req);
    return;
  }
  if(tid.indexOf('ch')===0){
    const r=document.querySelector('input[type=radio][data-req="'+req+'"][data-ctid="'+
      tid.slice(2)+'"][value="'+value+'"]');
    if(r){r.checked=true;if(value==='other')openComment(req,tid.slice(2),false);}
    relabel(req);
    return;
  }
  if(tid.indexOf('dw')===0){
    const i=document.querySelector('input.dwi[data-req="'+req+'"][data-dtid="'+
      tid.slice(2)+'"]');
    if(i&&i!==document.activeElement){i.value=value;showCriterion(i);}
    relabel(req);
    return;
  }
  const el=tid==='_all'
    ? document.getElementById('c-'+req)
    : document.querySelector('textarea[data-req="'+req+'"][data-tid="'+tid+'"]');
  if(!el||el===document.activeElement)return;
  el.value=value;
  if(tid!=='_all'&&value.trim()){
    const task=el.closest('.task');
    if(task)task.classList.add('open');
  }
  relabel(req);
}

async function poll(){
  if(busy)return;
  try{
    const r=await fetch('/api/pending');const d=await r.json();
    const list=d.requests||[];
    RUNS=d.runs||[];
    dprune(list.map(x=>x.id).concat(RUNS.map(runId)));
    const undecided=list.filter(x=>!x.decided);
    pendingCount=undecided.length;
    // Nothing on the page changed: nothing is touched, and the tone is not played again.
    if(!draw(list))return;
    // A running plan is not a question, so it never raises the alarm.
    if(undecided.length)alertOn();else alertOff();
  }catch(e){}
}
// A run card redraws each time the agent reports a task, which can be while the human is
// typing into it. The text survives - it is a draft - and this puts the caret back.
function focusKey(){
  const el=document.activeElement;
  if(!el||!el.closest||!el.closest('#root'))return null;
  const key={id:el.id||'',req:'',tid:'',dtid:'',start:null,end:null};
  if(!key.id&&el.getAttribute){
    key.req=el.getAttribute('data-req')||'';
    key.tid=el.getAttribute('data-tid')||'';
    key.dtid=el.getAttribute('data-dtid')||'';
    if(!key.req)return null;
  }
  try{key.start=el.selectionStart;key.end=el.selectionEnd;}catch(e){}
  return key;
}
function refocus(key){
  if(!key)return;
  let el=null;
  if(key.id)el=document.getElementById(key.id);
  else if(key.tid)el=document.querySelector('textarea[data-req="'+key.req+'"][data-tid="'+key.tid+'"]');
  else if(key.dtid)el=document.querySelector('input.dwi[data-req="'+key.req+'"][data-dtid="'+key.dtid+'"]');
  if(!el||el.disabled)return;
  el.focus();
  try{if(typeof key.start==='number')el.setSelectionRange(key.start,key.end);}catch(e){}
}
// Quotes are escaped too so the same helper is safe inside an attribute value.
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
  '"':'&quot;',"'":'&#39;'}[c]));}

// ---- per-task review -----------------------------------------------------
// Both phases can be reviewed task by task, but the request means different things:
// on a plan it rewrites the task's wording, on a completion report it orders that one
// task to be done again. The phase of each queued request is kept here so the buttons
// can say which of the two the human is about to ask for.
const PHASE={};
function comments(id){
  const out={};
  document.querySelectorAll('textarea[data-req="'+id+'"]').forEach(b=>{
    const v=b.value.trim();
    if(v)out[b.getAttribute('data-tid')]=v;
  });
  return out;
}
// Reveal one task's comment box. The textarea stays in the DOM either way, so a comment
// written and then collapsed is still collected by comments() - the row keeps its 'filled'
// marker precisely so that cannot happen silently.
function toggleComment(btn){
  const task=btn.closest('.task');
  if(!task)return;
  const open=task.classList.toggle('open');
  btn.setAttribute('aria-expanded',open?'true':'false');
  if(open){const ta=task.querySelector('textarea.tc');if(ta)ta.focus();}
}
function wholePlan(id){
  const box=document.getElementById('all-'+id);
  return !!(box&&box.checked);
}
// Whether the request is about the plan as a whole: the comment box for the whole plan
// has text, or the "rewrite the whole plan" box is ticked. A comment on the whole plan
// used to ride along with a per-task request without widening it; now what is written
// there is what is asked for, and the button says so.
function wholeOf(id){
  const all=document.getElementById('c-'+id);
  return wholePlan(id)||!!(all&&all.value.trim());
}
function scopeOf(id){
  if(wholeOf(id))return 'PLAN';
  return Object.keys(comments(id)).length?'TASKS':'PLAN';
}
// The button says what it will do BEFORE it is clicked, so choosing the scope is an
// explicit act by the human rather than something the server infers afterwards. One
// builder for both the first render and every relabel, so the two can never drift.
//
// One statement per line: what the button is, then each thing it carries. Joined by
// dots on one line they ran together, and it was not clear which words belonged to
// which. "계획 전체 재작성" is said only when the request really is about the whole plan
// (wholeOf) - it used to be the button's resting label, with nothing typed at all.
function revLabel(phase,ids,whole,crit){
  const done=phase==='COMPLETION';
  const lines=[done&&!whole?'다시 작업 요청':'수정 요청'];
  if(whole)lines.push(done?'계획 전체 다시 세우기':'계획 전체 재작성');
  else if(ids.length)lines.push(ids.length===1?ids[0]+'번만':ids.length+'개 태스크만');
  // A criterion the human wrote goes with this button too, and the button has to say
  // which way: onto the tasks when only some are rewritten (반영), or to the agent as
  // part of the comment when the whole plan is, because no task is left to carry it (전달).
  if(crit&&!done&&(whole||ids.length))
    lines.push('태스크 완료 기준 '+crit+'건 '+(whole?'전달':'반영'));
  return lines.join('\n');
}
// Writes such a label into a button: the first line as its title, the rest under it.
function setLabel(b,label){
  b.textContent='';
  String(label).split('\n').forEach((part,i)=>{
    const s=document.createElement('span');
    s.className=i?'bd':'bt';
    s.textContent=part;
    b.appendChild(s);
  });
}
// The continue button on a halt card states whether it sends the agent a direction.
function haltLabel(hasText){return hasText?'의견 전달 후 계속':'계속 진행';}
// ---- choices (2.0.0) -------------------------------------------------------
// A task that offers a choice shows its options as radios, the model's recommendation
// (index 0) pre-selected and marked 권장. What is picked travels with the approval;
// the server refuses any index that was not on screen.
const LETTERS='ABCDEFGH';
function hasChoice(t){return !!(t.options&&t.options.length>=2);}
// '집계 방식 · 3가지 중 선택' - what is being chosen, from the model; a neutral noun phrase
// when it gave none. Mirrors choices.choice_heading, which writes the chat text.
function choiceHeading(t){
  return '<span class="pick">'+esc((t.topic||'').trim()||'진행 방법')+
    '</span><span class="cnt"> · '+t.options.length+'가지 중 선택</span>';
}
function optionsHtml(d,t,allowOther){
  if(!hasChoice(t))return '';
  const name='ch-'+d.id+'-'+t.task_id;
  const attrs=' name="'+esc(name)+'" data-req="'+esc(d.id)+'" data-ctid="'+
    esc(String(t.task_id))+'"';
  let h='<div class="opts">';
  t.options.forEach((o,i)=>{
    // aria-label: a screen reader (and the accessibility tree) otherwise names the
    // radio by its value, "0" / "1", rather than by the option it stands for.
    h+='<label class="opt"><input type="radio"'+attrs+' value="'+i+'"'+(i===0?' checked':'')+
      ' aria-label="'+LETTERS[i]+'. '+esc(o.title)+(i===0?' (권장)':'')+
      '"><span class="optl">'+LETTERS[i]+'.</span><span>'+esc(o.title)+
      (i===0?'<span class="rec">권장</span>':'')+
      (o.reason?' <span class="why">— '+esc(o.reason)+'</span>':'')+'</span></label>';
  });
  if(allowOther)h+='<label class="opt"><input type="radio"'+attrs+' value="other" aria-label="기타">'+
    '<span class="optl"></span><span>기타 (직접 입력)</span></label>';
  return h+'</div>';
}
function checkedOf(id){
  return Array.from(document.querySelectorAll('input[type=radio][data-req="'+id+'"]:checked'));
}
function choicesOf(id){
  const out={};
  checkedOf(id).forEach(r=>{if(r.value!=='other')out[r.getAttribute('data-ctid')]=Number(r.value);});
  return out;
}
function anyOther(id){return checkedOf(id).some(r=>r.value==='other');}
// The approve button, like the REVISE button, says what it will do before it is
// clicked: which options it carries, or that 기타 needs a revision request instead.
function okLabel(phase,id){
  const base=phase==='HALT'?'이 초안으로 승인':'승인';
  if(anyOther(id))return base+'\n기타는 수정 요청으로';
  const changed=Object.entries(choicesOf(id)).filter(([k,v])=>v!==0);
  const crit=Object.keys(criteriaOf(id)).length;
  if(crit&&changed.length)return base+'\n변경 '+(changed.length+crit)+'건 반영';
  if(crit)return base+'\n태스크 완료 기준 '+crit+'건 반영';
  if(!changed.length)return base;
  if(changed.length===1)return base+'\n'+changed[0][0]+'번 '+LETTERS[changed[0][1]]+'안';
  return base+'\n선택 '+changed.length+'건 반영';
}
// ---- done_when (3.0.0) -----------------------------------------------------
// What "finished" means for a task. On a plan request the human may write or rewrite
// it and still approve: the task itself does not change, so there is nothing for the
// agent to redraft and no round trip is spent. The edit is a draft like any other -
// kept in localStorage, mirrored across tabs - and only the tasks whose text differs
// from what was shown are sent; the server refuses any task it did not show.
function dwInputs(id){
  return Array.from(document.querySelectorAll('input.dwi[data-req="'+id+'"]'));
}
function dwValue(i){return i.value.trim().replace(/\s+/g,' ');}
function criteriaOf(id){
  const out={};
  dwInputs(id).forEach(i=>{
    const v=dwValue(i);
    if(v!==(i.getAttribute('data-orig')||''))out[i.getAttribute('data-dtid')]=v;
  });
  return out;
}
function showCriterion(i){
  const row=i.closest('.dw');
  if(row){row.classList.remove('hid');row.classList.add('edit');}
  // The button that opens the field shows that it is open, as 의견 does.
  const task=i.closest('.task'),add=task&&task.querySelector('.dwadd');
  if(add)add.setAttribute('aria-expanded','true');
}
// 완료 기준 추가 is a toggle, like 의견 - while the field is empty. Once something is
// typed the field stays: it travels with the approval, and hiding it would hide what the
// approval carries. Erase the text and the button closes the field again.
function toggleCriterion(btn,req,tid){
  const i=document.querySelector('input.dwi[data-req="'+req+'"][data-dtid="'+tid+'"]');
  if(!i)return;
  const row=i.closest('.dw');
  if(row&&row.classList.contains('edit')&&!dwValue(i)){
    row.classList.remove('edit');row.classList.add('hid');
    btn.setAttribute('aria-expanded','false');
    return;
  }
  showCriterion(i);i.focus();
}
function editCriterion(btn,req,tid){
  const i=document.querySelector('input.dwi[data-req="'+req+'"][data-dtid="'+tid+'"]');
  if(!i)return;
  showCriterion(i);i.focus();
}
function dwRow(d,t,editable){
  const has=!!t.done_when,rid=esc(d.id),tid=esc(String(t.task_id));
  if(!has&&!editable)return '';
  let h='<div class="dw'+(has?'':' hid')+'"><span class="dwl">태스크 완료 기준</span>'+
    '<span class="dwt">'+esc(t.done_when||'')+
    (t.done_when_by==='user'?'<span class="dwby">사용자 지정</span>':'')+'</span>';
  if(editable)h+='<input class="dwi" type="text" maxlength="200" data-req="'+rid+
    '" data-dtid="'+tid+'" data-orig="'+esc(t.done_when||'')+'" value="'+esc(t.done_when||'')+
    '" aria-label="'+tid+'번 태스크 완료 기준">'+
    '<button class="dwbtn" type="button" onclick="editCriterion(this,\''+rid+'\',\''+
    tid+'\')">수정</button>';
  return h+'</div>';
}
// ---- what the server found (3.0.0) -------------------------------------------
// On a completion report: the files a task said it produced, as the server found them.
// Everything else on the row is the agent's word; these lines are not.
function sizeText(n){
  if(typeof n!=='number')return '';
  if(n<1024)return n+' B';
  if(n<1048576)return (n/1024).toFixed(1)+' KB';
  return (n/1048576).toFixed(1)+' MB';
}
function baseName(p){
  const s=String(p||'').replace(/\\/g,'/');
  const i=s.lastIndexOf('/');
  return i>=0?s.slice(i+1):s;
}
function tookText(s){
  if(typeof s!=='number')return '';
  if(s<60)return s+'초';
  const m=Math.floor(s/60),r=s%60;
  if(m<60)return m+'분'+(r?' '+r+'초':'');
  return Math.floor(m/60)+'시간 '+(m%60)+'분';
}
// A file the task itself created or changed. One that was already there before the
// task proves the file exists, not that the task did anything - it gets its own line,
// but it does not move the task out of "the agent's word only".
function producedCheck(c){
  return !c.gone&&(c.state==='found'||c.state==='folder');
}
function checkLine(c){
  const name=esc(baseName(c.path)),size=sizeText(c.size),det=size?' · '+size:'';
  const open='<div title="'+esc(c.path)+'" class="ck ';
  if(c.gone)return open+'warn">⚠ '+name+' · 보고 당시에는 있었으나 지금은 없음</div>';
  if(c.state==='found')return open+'ok">✔ '+name+det+' · 이 태스크 중 생성/변경됨</div>';
  if(c.state==='old')return open+'dim">· '+name+det+
    ' · 작업 전부터 있던 파일 (이 태스크에서 바뀌지 않음)</div>';
  if(c.state==='folder')return open+'ok">✔ '+name+' · 폴더가 있음</div>';
  if(c.state==='empty')return open+'warn">⚠ '+name+' · 빈 파일</div>';
  if(c.state==='missing')return open+'warn">⚠ '+name+' · 찾을 수 없음</div>';
  if(c.state==='unknown')return open+'dim">· '+name+' · 확인하지 못함 (응답 지연)</div>';
  return open+'dim">· '+name+' · 서버의 확인 범위 밖</div>';
}
function checksHtml(t){
  let h=(t.checks||[]).map(checkLine).join('');
  if((t.claims_withdrawn||[]).length)h+='<div class="ck warn">⚠ 처음에 '+
    esc(t.claims_withdrawn.map(baseName).join(', '))+
    ' 을(를) 결과 파일로 적었다가 뺐습니다 (서버가 찾지 못함)</div>';
  if(t.echo)h+='<div class="ck warn">⚠ 증거가 태스크 완료 기준 문장을 거의 그대로 반복합니다</div>';
  return h;
}
// Where to look first. Shown only when the server checked at least one file for this
// plan - otherwise every task would be "unconfirmed" and the line would say nothing.
function triage(d){
  const tasks=d.tasks||[];
  if(!tasks.some(t=>(t.checks||[]).length||(t.claims_withdrawn||[]).length))return '';
  const seen=tasks.filter(t=>(t.checks||[]).some(producedCheck)).length;
  const rest=tasks.length-seen;
  return '<div class="triage">'+tasks.length+'개 중 '+seen+
    '개는 태스크 중에 만들거나 바꾼 파일을 서버가 확인했습니다.'+
    (rest?' 나머지 '+rest+'개는 에이전트의 보고가 근거입니다.':'')+'</div>';
}
// Whether anything is written in a comment box of this request: the one for the whole
// plan, or any task's.
function hasOpinion(id){
  const all=document.getElementById('c-'+id);
  return !!(all&&all.value.trim())||Object.keys(comments(id)).length>0;
}
function relabelOk(id){
  const b=document.getElementById('ok-'+id);
  if(!b)return;
  setLabel(b,okLabel(PHASE[id],id));
  b.disabled=anyOther(id);
  // An approval carries no comment - whatever was typed would be dropped unread. So
  // while a comment is written there is no approve button to press by mistake: it is
  // gone, and back as soon as every box is empty again. (Not on a halt card, whose
  // box is the direction for "continue".)
  b.hidden=PHASE[id]!=='HALT'&&hasOpinion(id);
}
function openComment(req,tid,focus){
  const ta=document.querySelector('textarea.tc[data-req="'+req+'"][data-tid="'+tid+'"]');
  const task=ta&&ta.closest('.task');
  if(!task)return;
  task.classList.add('open');
  const btn=task.querySelector('.tcbtn:not(.dwadd)');
  if(btn)btn.setAttribute('aria-expanded','true');
  if(focus)ta.focus();
}
function relabel(id){
  // A run card has no button whose label depends on what is typed.
  if(PHASE[id]==='RUN')return;
  relabelOk(id);
  if(PHASE[id]==='HALT'){
    const b=document.getElementById('rev-'+id),c=document.getElementById('c-'+id);
    if(b)b.textContent=haltLabel(!!(c&&c.value.trim()));
    return;
  }
  // Mark the rows that carry a comment, so a collapsed one is still visible as such.
  document.querySelectorAll('textarea[data-req="'+id+'"]').forEach(b=>{
    const task=b.closest('.task');
    if(task)task.classList.toggle('filled',!!b.value.trim());
  });
  // And the criteria the human has changed: they travel with the approval.
  dwInputs(id).forEach(i=>{
    const row=i.closest('.dw');
    if(row)row.classList.toggle('changed',dwValue(i)!==(i.getAttribute('data-orig')||''));
  });
  const btn=document.getElementById('rev-'+id);
  if(!btn)return;
  const ids=Object.keys(comments(id)),whole=wholeOf(id);
  setLabel(btn,revLabel(PHASE[id],ids,whole,Object.keys(criteriaOf(id)).length));
  // No request without something to ask for. With nothing written and nothing ticked
  // the choice is to approve or to reject; the button appears with the first comment -
  // at the moment the approve button leaves (relabelOk).
  btn.hidden=!(whole||ids.length);
}
// The header a human needs before they can judge a task list at all: what is being
// asked, what the goal is, and the model's own overview of how it intends to get there.
// All three used to arrive inside the pre-rendered `display` blob; rendering tasks as
// rows dropped that blob, so they are composed here from their own fields instead.
function header(d){
  const title=d.phase==='COMPLETION'?'완료 확인':'계획 승인 요청';
  let h='<h1>'+title+' · '+esc(d.plan_id)+'</h1>'+verNote(d)+
    '<p class="goal"><span class="lbl">목표</span>'+esc(d.goal)+'</p>';
  if(d.summary)h+='<div class="summary"><span class="lbl">개요</span>'+esc(d.summary)+'</div>';
  return h;
}
const RUN_STATE={DONE:'완료',IN_PROGRESS:'진행중',FAILED:'실패',PENDING:'대기'};
function runBadge(t){
  return '<span class="badge'+(t.status==='IN_PROGRESS'?' now':'')+'">'+
    (RUN_STATE[t.status]||esc(t.status))+'</span>';
}
function taskRows(d){
  // On a completion report every task is DONE, so a DONE badge on every row is noise -
  // the evidence line below it already says so, and that is what needs the attention.
  const done=d.phase==='COMPLETION';
  return '<p class="tasklabel">태스크 '+(d.tasks||[]).length+'개</p>'+
    '<div class="tasks">'+(d.tasks||[]).map(t=>{
    const badge=t.status&&t.status!=='PENDING'&&!(done&&t.status==='DONE');
    const choose=!done&&hasChoice(t)&&t.chosen==null;
    // On a completion report a task that offered a choice says which one was carried
    // out, so the human can check it was done the way they picked.
    const picked=done&&hasChoice(t)&&t.chosen!=null
      ?'<span class="badge">'+(t.chosen===0?'권장안':LETTERS[t.chosen]+'안 선택')+'</span>':'';
    // A criterion can be written for any task still to be done. Finished work keeps the
    // one it was done under.
    const editable=!done&&t.status!=='DONE';
    const took=done?tookText(t.duration_sec):'';
    let row='<div class="task"><div class="tt"><span class="tn">'+esc(String(t.task_id))+
      '.</span>'+(choose?'<span>'+choiceHeading(t)+'</span>'
                        :'<span>'+esc(t.title)+'</span>')+picked+
      (badge?runBadge(t):'')+
      (took?'<span class="took">소요 '+took+'</span>':'')+
      (editable&&!t.done_when?'<button class="tcbtn dwadd" type="button" aria-expanded="false" '+
        'onclick="toggleCriterion(this,\''+esc(d.id)+'\',\''+esc(String(t.task_id))+
        '\')">완료 기준 추가</button>':'')+
      '<button class="tcbtn" type="button" aria-expanded="false" '+
      'onclick="toggleComment(this)">'+(done?'다시 작업':'의견')+'</button></div>';
    if(choose)row+=optionsHtml(d,t,true);
    if(t.previous_title)row+='<div class="was">'+esc(t.previous_title)+'</div>';
    if(t.revision_note)row+='<div class="note">\u21BB 요청하신 내용: '+
      esc(t.revision_note)+'</div>';
    // A task rewritten after it failed: why the first way did not work.
    if(t.failure_note)row+='<div class="note">✕ 이전 시도 실패: '+
      esc(t.failure_note)+'</div>';
    row+=dwRow(d,t,editable);
    // What the task used to produce, for a row the human already sent back once. The
    // request alone shows what was asked; only this shows whether it was answered.
    if(done&&t.previous_result_log)
      row+='<div class="ev old">이전 결과: '+esc(t.previous_result_log)+'</div>';
    // The claim the human is judging. Without it a completion report is a task list
    // that says nothing about whether the work happened.
    if(done){
      const ev=(t.result_log||'').trim();
      row+=ev?'<div class="ev">\u2192 '+esc(ev)+'</div>'
             :'<div class="ev none">(증거 기록 없음)</div>';
      row+=checksHtml(t);
    }
    // Labelled with the name of the button that opens it, so the box needs no sample
    // sentence inside - a grey one read as if something had already been written.
    const what=done?'다시 작업':'의견';
    row+='<div class="tcrow"><span class="dwl">'+what+'</span><textarea class="tc" data-req="'+
      esc(d.id)+'" data-tid="'+esc(String(t.task_id))+'" aria-label="'+
      esc(String(t.task_id))+'번 '+what+'"></textarea></div>';
    return row+'</div>';
  }).join('')+'</div>';
}
function chip(d){
  return d.agent_waiting
    ? '<div class="chip live">에이전트가 대기 중입니다 · 결정하시면 작업이 바로 이어집니다</div>'
    : '<div class="chip idle">에이전트가 대기를 멈췄습니다 · 지금 결정하셔도 반영되며, '+
      '채팅창에 메시지를 입력하시면 진행이 재개됩니다</div>';
}
// What the agent still wanted to reconsider while this request was open. It may not
// change the plan the human is reading, so it is shown here instead of being lost.
function agentNote(d){
  return d.agent_note?'<div class="summary agent"><span class="lbl">에이전트 추가 의견</span>'+
    esc(d.agent_note)+'</div>':'';
}
// The circuit breaker stopped an agent that kept repeating a step. The human is not
// judging the plan's wording here but deciding how the agent continues - so no per-task
// review, and the button set depends on whether there is a draft to approve as it stands.
function haltCard(d){
  // origin 'user': the human stopped a running plan from its run card (3.1.0). Same
  // decisions as a breaker halt - continue, with or without a direction, or cancel -
  // under words that do not suggest the agent did something wrong.
  const tasks=d.tasks||[],user=d.origin==='user';
  let h='<h1>'+(user?'실행 멈춤':'반복 감지')+' · '+esc(d.plan_id)+'</h1>'+verNote(d)+
    '<p class="goal"><span class="lbl">목표</span>'+esc(d.goal)+'</p>'+
    '<div class="summary warn"><span class="lbl">'+(user?'멈춘 이유':'에이전트가 멈춘 이유')+
    '</span>'+esc(d.summary)+'</div>';
  if(tasks.length){
    h+='<p class="tasklabel">'+(d.draft?'현재 초안 · 태스크 ':'태스크 ')+tasks.length+'개</p>'+
      '<div class="tasks">'+tasks.map(t=>'<div class="task"><div class="tt"><span class="tn">'+
      esc(String(t.task_id))+'.</span>'+
      (hasChoice(t)&&d.draft?'<span>'+choiceHeading(t)+'</span>'
                   :'<span>'+esc(t.title)+'</span>')+
      (t.status&&t.status!=='PENDING'?runBadge(t):'')+
      '</div>'+(d.draft?optionsHtml(d,t,false):'')+dwRow(d,t,false)+
      // What is already done, for a plan the human stopped: it is what they weigh
      // when deciding whether the rest should run.
      (user&&t.status==='DONE'&&(t.result_log||'').trim()
        ?'<div class="ev">\u2192 '+esc(t.result_log.trim())+'</div>':'')+
      '</div>').join('')+'</div>';
  }
  h+=chip(d);
  h+='<textarea id="c-'+esc(d.id)+
    '" placeholder="에이전트에게 전할 방향을 입력해 주십시오 (선택 사항)"></textarea>';
  h+='<div class="row">';
  if(d.draft)h+='<button class="ok" id="ok-'+esc(d.id)+'" onclick="decide(\''+esc(d.id)+
    '\',\'APPROVED\')">이 초안으로 승인</button>';
  h+='<button class="rev" id="rev-'+esc(d.id)+'" onclick="decide(\''+esc(d.id)+
    '\',\'REVISE\')">'+haltLabel(false)+'</button>';
  return h+'<button class="no" onclick="decide(\''+esc(d.id)+
    '\',\'REJECTED\')">'+(user?'계획 취소':'취소')+'</button></div>';
}
// ---- a plan that is running (3.1.0) ---------------------------------------------
// Between the plan approval and the completion report there used to be nothing here.
// This card is not a question, so it raises no alarm: it shows what is finished and what
// is in progress, and lets the human stop the plan before its next task or change what
// is left. Either takes effect when the agent next reports a task - the server cannot
// interrupt a tool that is already running, and the page says so.
let RUNS=[];
function runId(r){return 'run-'+r.plan_id;}
const RUN_HINT='실행 중인 계획은 에이전트가 태스크를 보고할 때마다 갱신됩니다. [멈춤]과 '+
  '[의견 전달]은 에이전트가 진행 중인 태스크를 보고하는 시점에 적용되며, 이미 실행 중인 '+
  '도구는 중단되지 않습니다.<br>'+
  '에이전트가 오류로 멈춰 보고하지 않으면 [계획 강제 취소]로 계획을 닫으실 수 있습니다. '+
  '카드는 바로 사라지고, 계획은 다음 도구 호출 때 취소됩니다.<br>';
function clock(ts){
  if(typeof ts!=='number')return '';
  const d=new Date(ts*1000);
  return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
}
function runRows(r){
  const tasks=r.tasks||[],fin=tasks.filter(t=>t.status==='DONE').length;
  return '<p class="tasklabel">태스크 '+tasks.length+'개 · 완료 '+fin+'개</p>'+
    '<div class="tasks">'+tasks.map(t=>{
    const took=t.status==='DONE'?tookText(t.duration_sec):'';
    // The time taken goes before the state, so every row ends on its state label.
    let row='<div class="task"><div class="tt"><span class="tn">'+esc(String(t.task_id))+
      '.</span><span>'+esc(t.title)+'</span>'+
      (took?'<span class="took">소요 '+took+'</span>':'')+runBadge(t)+'</div>';
    row+=dwRow({id:runId(r)},t,false);
    if(t.status==='DONE'){
      const ev=(t.result_log||'').trim();
      row+=ev?'<div class="ev">\u2192 '+esc(ev)+'</div>'
             :'<div class="ev none">(증거 기록 없음)</div>';
      row+=checksHtml(t);
    }
    return row+'</div>';
  }).join('')+'</div>';
}
function runCard(r){
  const id=runId(r),pid=esc(r.plan_id),c=r.control;
  PHASE[id]='RUN';
  const seenAt=clock(r.updated_at);
  const h='<h1>실행 중 · '+pid+'</h1>'+verNote(r)+
    '<p class="goal"><span class="lbl">목표</span>'+esc(r.goal)+'</p>'+runRows(r)+
    (seenAt?'<p class="seen">에이전트의 마지막 보고 '+seenAt+'</p>':'');
  // Asked for, not yet applied: say exactly that, and let it be taken back - or, when
  // the agent is not coming back to apply it, close the plan without it.
  if(c)return h+'<div class="pendingctl">'+(c.action==='PAUSE'
    ?'멈춤을 요청하셨습니다. 에이전트가 진행 중인 태스크를 보고하면 다음 태스크를 시작하지 '+
     '않고 멈춥니다.'
    :'의견을 전달하셨습니다. 에이전트가 진행 중인 태스크를 보고하면, 남은 태스크를 의견에 맞게 '+
     '고쳐 다시 승인을 요청합니다.')+
    ' 에이전트가 오류로 멈춰 보고하지 않으면 계획을 강제로 취소하실 수 있습니다.'+
    (c.comment?'<span class="q">'+esc(c.comment)+'</span>':'')+
    '<div class="ctlrow"><button class="linkbtn" type="button" onclick="control(\''+pid+
    '\',\'CLEAR\')">요청 취소</button>'+killBtn(pid)+'</div></div>';
  return h+'<textarea id="c-'+esc(id)+'" placeholder="남은 태스크를 바꾸고 싶으실 때 의견을 '+
    '입력해 주십시오 (예: 요약은 표로 정리해 주세요)"></textarea>'+
    '<div class="row"><button class="rev" type="button" onclick="control(\''+pid+
    '\',\'NOTE\')">의견 전달</button><button class="no" type="button" onclick="control(\''+
    pid+'\',\'PAUSE\')">멈춤</button></div><div class="ctlrow end">'+killBtn(pid)+'</div>';
}
// 3.2.0 - a stop and a note wait for the agent's next report. An agent that failed in
// the middle of a plan never makes one, and its card stayed here for good. This closes
// the plan without it: the card goes at once, the plan on the next tool call.
function killBtn(pid){
  return '<button class="linkbtn kill" type="button" onclick="control(\''+pid+
    '\',\'CANCEL\')">계획 강제 취소</button>';
}
const KILL_ASK='실행 중인 계획을 강제로 취소하시겠습니까?\n\n에이전트의 보고를 기다리지 않고 '+
  '계획을 닫습니다. 되돌릴 수 없으며, 에이전트가 다시 보고하면 계획이 취소되었다는 '+
  '답을 받습니다.';
function restoreRun(r){
  const box=document.getElementById('c-'+runId(r));
  if(box)box.value=dget(runId(r),'_all');
}
async function control(pid,action){
  if(busy)return;
  const id='run-'+pid,box=document.getElementById('c-'+id);
  const text=box?box.value.trim():'';
  if(action==='NOTE'&&!text){
    if(box){box.placeholder='전달하실 의견을 먼저 입력해 주십시오';box.focus();}
    return;
  }
  if(action==='CANCEL'&&!confirm(KILL_ASK))return;
  busy=true;
  lock(id);
  // Taking a request back puts its words back in the box: nothing typed is dropped.
  const run=RUNS.find(r=>r.plan_id===pid);
  const back=action==='CLEAR'&&run&&run.control?(run.control.comment||''):'';
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({plan_id:pid,action:action,comment:text})});
    const j=await r.json();
    if(j&&j.ok){
      if(action==='CLEAR'){if(back)dset(id,'_all',back);}else dclear(id);
      lastError='';
    }else lastError='요청을 기록하지 못했습니다. 계획이 이미 다음 단계로 넘어갔을 수 있으니 '+
      '화면을 확인해 주십시오.';
  }catch(e){lastError='요청을 전송하지 못했습니다. 다시 시도해 주십시오.';}
  busy=false;stale(id);poll();
}
const DONE_LABEL={APPROVED:'승인되었습니다',REJECTED:'거절되었습니다',REVISE:'수정 요청되었습니다'};
const HALT_DONE_LABEL={APPROVED:'초안이 승인되었습니다',REJECTED:'취소되었습니다',
  REVISE:'계속 진행하도록 했습니다'};
// ---- drawing: one card at a time ---------------------------------------------------
// Each request and each running plan is its own card, and a card is redrawn only when
// what it shows has changed. Before, any change rebuilt every card: with run cards on
// the page that meant a task reported by one plan redrew the approval request of another
// that the human was in the middle of reading - an opened comment box closed, a
// selection was lost, the caret had to be put back. What is typed was always safe (it
// is a draft); now the card it was typed into is not touched at all.
const NODES={};   // key -> the card's element
const SHOWN={};   // key -> what that card shows now; a different value means "redraw"
// Matches the static placeholder above, so the page does not flicker between two
// different wordings when the first poll lands.
const IDLE_HTML='<div class="idle">현재 대기 중인 승인 요청이 없습니다.<br>'+
  '<span style="font-size:.85rem">에이전트가 계획을 제출하면 이곳에 표시됩니다.</span></div>';
function reqKey(d){return 'q-'+d.id;}
// The request's own content is fixed by its id - a changed plan is a new request - so
// only what can change under one id is in the signature.
function reqSig(d){
  return (d.decided||'')+':'+(d.agent_waiting?1:0)+':'+(d.agent_note||'').length;
}
function runSig(r){return (r.rev||'')+':'+(r.control?r.control.id:'');}
// The buttons of one card, switched off while its decision is on its way. Only that
// card's: no other card is redrawn by this decision, so nothing would switch theirs
// back on - and the information icon lives outside #root altogether.
function lock(key){
  const node=NODES[key];
  if(node)node.querySelectorAll('button').forEach(b=>b.disabled=true);
}
// Redraw this card at the next poll whatever its signature says: its buttons were
// switched off, and a decision that could not be recorded changes nothing else.
function stale(key){delete SHOWN[key];}
function hintHtml(list){
  const anyChoice=list.some(d=>!d.decided&&(d.tasks||[]).some(hasChoice));
  const anyPlan=list.some(d=>!d.decided&&d.phase==='PLAN'&&(d.tasks||[]).length);
  return (RUNS.length?RUN_HINT:'')+(!list.length?'':(anyChoice?'선택지가 있는 태스크는 [권장]안이 기본으로 선택되어 있습니다. '+
    '다른 안을 고르면 승인 버튼에 반영 내용이 표시되고, 기타를 고르면 수정 요청으로 바뀝니다.<br>':'')+
    '태스크의 [의견] 또는 [다시 작업] 버튼을 누르면 해당 태스크에만 요청을 남기실 수 '+
    '있습니다. 완료 보고 단계에서는 지정하신 태스크만 다시 실행되며, 나머지 태스크의 결과는 '+
    '그대로 유지됩니다.<br>'+
    '의견을 적으시면 [승인] 대신 [수정 요청] 버튼이 나타납니다.<br>'+
    (anyPlan?'[완료 기준 추가] 또는 [수정]으로 태스크 완료 기준을 직접 적으실 수 있습니다. '+
    '적은 기준은 승인과 함께 반영되며, 수정 요청을 거치지 않습니다.<br>':'')+
    '결정하시기 전까지 해당 에이전트는 후속 작업을 진행하지 못합니다. '+
    '요청은 응답하실 때까지 사라지지 않으니 천천히 검토해 주시기 바랍니다.');
}
// Returns whether any card was added, removed or redrawn.
function draw(list){
  const root=document.getElementById('root');
  // 여러 세션이 동시에 승인을 기다릴 수 있으므로 큐 전체를 보여준다.
  const items=list.map(d=>({key:reqKey(d),sig:reqSig(d),html:()=>requestCard(d),
                            after:()=>restore(d)}))
    .concat(RUNS.map(r=>({key:runId(r),sig:runSig(r),html:()=>runCard(r),
                          after:()=>restoreRun(r)})));
  // The idle text is a child of #root itself. Only that one: an agent that stopped
  // waiting shows a chip with the class "chip idle" inside its card, and a search of the
  // whole tree removed one such chip on every poll until none was left.
  if(!items.length){
    if(root.querySelector(':scope>.idle'))return false;
    root.innerHTML=IDLE_HTML;
    Object.keys(NODES).forEach(k=>{delete NODES[k];delete SHOWN[k];});
    return true;
  }
  let changed=false;
  const idle=root.querySelector(':scope>.idle');
  if(idle)idle.remove();
  // The line that says a click could not be recorded. Its own element, so showing or
  // clearing it redraws no card.
  let err=root.querySelector('.err');
  if(lastError){
    if(!err){err=document.createElement('div');err.className='err';root.insertBefore(err,root.firstChild);}
    if(err.textContent!==lastError)err.textContent=lastError;
  }else if(err)err.remove();
  let hint=root.querySelector('p.hint');
  if(!hint){hint=document.createElement('p');hint.className='hint';root.appendChild(hint);}
  // Cards whose request was collected, or whose plan stopped running.
  const wanted={};
  items.forEach(it=>{wanted[it.key]=true;});
  Object.keys(NODES).forEach(k=>{
    if(wanted[k])return;
    NODES[k].remove();delete NODES[k];delete SHOWN[k];changed=true;
  });
  // In order: requests first, then runs. A card already in its place is not moved -
  // moving a node takes the focus out of it just as redrawing would.
  let cursor=root.querySelector('.item');
  items.forEach(it=>{
    let node=NODES[it.key];
    if(!node){
      node=document.createElement('div');
      node.className='item';
      node.setAttribute('data-key',it.key);
      NODES[it.key]=node;
    }
    if(node!==cursor)root.insertBefore(node,cursor||hint);
    cursor=node.nextElementSibling;
    if(cursor&&!cursor.classList.contains('item'))cursor=null;
    if(SHOWN[it.key]===it.sig)return;
    const focus=node.contains(document.activeElement)?focusKey():null;
    node.innerHTML=it.html();
    SHOWN[it.key]=it.sig;
    it.after();
    refocus(focus);
    changed=true;
  });
  const text=hintHtml(list);
  if(hint.getAttribute('data-src')!==text){hint.innerHTML=text;hint.setAttribute('data-src',text);}
  return changed;
}
function requestCard(d){
  if(d.decided){
    const label=(d.phase==='HALT'?HALT_DONE_LABEL:DONE_LABEL)[d.decided]||d.decided;
    return '<div class="done">'+esc(d.plan_id)+' — '+label+
      '<br><span style="font-weight:400;opacity:.6;font-size:.9rem">'+
      '에이전트가 이 결정을 반영합니다.</span></div>';
  }
  // An entry with no phase was published by an older server process on this same
  // state directory. It has no per-task review, so fall back to the original form.
  PHASE[d.id]=d.phase;
  if(d.phase==='HALT')return haltCard(d);
  const perTask=(d.phase==='PLAN'||d.phase==='COMPLETION')&&d.tasks&&d.tasks.length;
  // The fallback path keeps the original layout on purpose: `display` already opens
  // with its own title, 목표 and summary, so composing a header above it would repeat
  // all three.
  let html=perTask?header(d)+(d.phase==='COMPLETION'?triage(d):'')+taskRows(d)
    :'<h1>'+(d.phase==='COMPLETION'?'완료 확인':'승인 요청')+' · '+esc(d.plan_id)+
     '</h1>'+verNote(d)+'<p class="goal">'+esc(d.goal)+'</p><pre>'+esc(d.display)+'</pre>';
  html+=agentNote(d);
  html+=chip(d);
  html+='<textarea id="c-'+esc(d.id)+
    '" placeholder="전체 의견을 입력해 주십시오 (거절 사유도 여기에 입력하실 수 있습니다)"></textarea>';
  if(perTask)html+='<label class="scope"><input type="checkbox" id="all-'+esc(d.id)+
    '"> '+
    (d.phase==='COMPLETION'?'계획 자체를 다시 세우기 (태스크 추가·삭제·순서 변경 시 선택)'
                           :'계획 전체를 다시 세우기 (태스크 추가·삭제·순서 변경 시 선택)')+
    '</label>';
  return html+'<div class="row">'+
    '<button class="ok" id="ok-'+esc(d.id)+'" onclick="decide(\''+esc(d.id)+
    '\',\'APPROVED\')">승인</button>'+
    '<button class="rev" hidden id="rev-'+esc(d.id)+'" onclick="decide(\''+esc(d.id)+
    '\',\'REVISE\')">수정 요청</button>'+
    '<button class="no" onclick="decide(\''+esc(d.id)+
    '\',\'REJECTED\')">거절</button></div>';
}
async function decide(id,dec){
  if(busy)return;busy=true;alertOff();
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
    if(j&&j.ok){dclear(id);lastError='';}
    else lastError='결정을 기록하지 못했습니다. 화면이 최신이 아닐 수 있으니 잠시 후 다시 시도해 주십시오.';
  }catch(e){lastError='결정을 전송하지 못했습니다. 다시 시도해 주십시오.';}
  busy=false;stale('q-'+id);poll();
}
// Bound to #root rather than to each textarea: #root outlives every redraw, so the
// listeners survive whichever cards come, go or are drawn again.
document.getElementById('root').addEventListener('input',onInput);
document.getElementById('root').addEventListener('change',onInput);
window.addEventListener('storage',onStorage);
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
document.getElementById('about-open').addEventListener('click',aboutOpen);
document.getElementById('about-close').addEventListener('click',aboutClose);
document.getElementById('about').addEventListener('click',ev=>{
  if(ev.target===ev.currentTarget)aboutClose();});
poll();setInterval(poll,1500);
