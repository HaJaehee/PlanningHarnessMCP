// Approval page - what the human types and picks on a card: the comment boxes, the
// choices, the criteria, and the buttons whose labels follow them. Everything typed is
// a draft (drafts.js) the moment it is typed, so a card can be redrawn at any time.

// ---- a card drawn again ----------------------------------------------------------
function taskBoxes(req){
  return document.querySelectorAll('textarea.tc[data-req="'+req+'"]');
}
// Puts what the human had typed back into ONE card, right after that card is drawn. A
// card that was not redrawn still holds it in the DOM and is left alone.
function restore(d){
  if(d.decided)return;
  state.phase[d.id]=d.phase;
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
function restoreRun(r){
  state.phase[runId(r)]='RUN';
  const box=document.getElementById('c-'+runId(r));
  if(box)box.value=dget(runId(r),'_all');
}

// ---- typing ----------------------------------------------------------------------
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

// ---- per-task review -----------------------------------------------------
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
function openComment(req,tid,focus){
  const ta=document.querySelector('textarea.tc[data-req="'+req+'"][data-tid="'+tid+'"]');
  const task=ta&&ta.closest('.task');
  if(!task)return;
  task.classList.add('open');
  const btn=task.querySelector('.tcbtn:not(.dwadd)');
  if(btn)btn.setAttribute('aria-expanded','true');
  if(focus)ta.focus();
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
// Whether anything is written in a comment box of this request: the one for the whole
// plan, or any task's.
function hasOpinion(id){
  const all=document.getElementById('c-'+id);
  return !!(all&&all.value.trim())||Object.keys(comments(id)).length>0;
}

// ---- choices (2.0.0) -------------------------------------------------------
function checkedOf(id){
  return Array.from(document.querySelectorAll('input[type=radio][data-req="'+id+'"]:checked'));
}
function choicesOf(id){
  const out={};
  checkedOf(id).forEach(r=>{if(r.value!=='other')out[r.getAttribute('data-ctid')]=Number(r.value);});
  return out;
}
function anyOther(id){return checkedOf(id).some(r=>r.value==='other');}

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

// ---- the buttons follow what is typed ----------------------------------------------
// Writes a label from labels.js into a button: the first line as its title, the rest
// under it.
function setLabel(b,label){
  b.textContent='';
  String(label).split('\n').forEach((part,i)=>{
    const s=document.createElement('span');
    s.className=i?'bd':'bt';
    s.textContent=part;
    b.appendChild(s);
  });
}
// What an approval of this request would carry, for okLabel.
function approvalOf(id){
  return {other:anyOther(id),
          changed:Object.entries(choicesOf(id)).filter(([k,v])=>v!==0),
          crit:Object.keys(criteriaOf(id)).length};
}
function relabelOk(id){
  const b=document.getElementById('ok-'+id);
  if(!b)return;
  setLabel(b,okLabel(state.phase[id],approvalOf(id)));
  b.disabled=anyOther(id);
  // An approval carries no comment - whatever was typed would be dropped unread. So
  // while a comment is written there is no approve button to press by mistake: it is
  // gone, and back as soon as every box is empty again. (Not on a halt card, whose
  // box is the direction for "continue".)
  b.hidden=state.phase[id]!=='HALT'&&hasOpinion(id);
}
function relabel(id){
  // A run card has no button whose label depends on what is typed.
  if(state.phase[id]==='RUN')return;
  relabelOk(id);
  if(state.phase[id]==='HALT'){
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
  setLabel(btn,revLabel(state.phase[id],ids,whole,Object.keys(criteriaOf(id)).length));
  // No request without something to ask for. With nothing written and nothing ticked
  // the choice is to approve or to reject; the button appears with the first comment -
  // at the moment the approve button leaves (relabelOk).
  btn.hidden=!(whole||ids.length);
}
