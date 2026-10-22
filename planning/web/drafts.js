// Approval page - drafts.
//
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
