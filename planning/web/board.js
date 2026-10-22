// Approval page - putting the cards on the page, one at a time.
//
// Each request and each running plan is its own card, and a card is redrawn only when
// what it shows has changed. Before, any change rebuilt every card: with run cards on
// the page that meant a task reported by one plan redrew the approval request of another
// that the human was in the middle of reading - an opened comment box closed, a
// selection was lost, the caret had to be put back. What is typed was always safe (it
// is a draft); now the card it was typed into is not touched at all.
const NODES={};   // key -> the card's element
const SHOWN={};   // key -> what that card shows now; a different value means "redraw"
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
// Returns whether any card was added, removed or redrawn.
function draw(list){
  const root=document.getElementById('root');
  // 여러 세션이 동시에 승인을 기다릴 수 있으므로 큐 전체를 보여준다.
  const items=list.map(d=>({key:reqKey(d),sig:reqSig(d),html:()=>requestCard(d),
                            after:()=>restore(d)}))
    .concat(state.runs.map(r=>({key:runId(r),sig:runSig(r),html:()=>runCard(r),
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
  if(state.lastError){
    if(!err){err=document.createElement('div');err.className='err';root.insertBefore(err,root.firstChild);}
    if(err.textContent!==state.lastError)err.textContent=state.lastError;
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
  const text=hintHtml(list,state.runs);
  if(hint.getAttribute('data-src')!==text){hint.innerHTML=text;hint.setAttribute('data-src',text);}
  return changed;
}
