// Approval page - what the decision buttons say. Pure: a label is worked out from what
// the human has typed and picked, which form.js reads off the page and passes in.

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
// The continue button on a halt card states whether it sends the agent a direction.
function haltLabel(hasText){return hasText?'의견 전달 후 계속':'계속 진행';}
// The approve button, like the REVISE button, says what it will do before it is
// clicked: which options it carries, or that 기타 needs a revision request instead.
// `picked` is what the approval would carry (form.js approvalOf): other - 기타 is
// picked somewhere; changed - [task id, option index] for each choice moved off the
// recommendation; crit - how many criteria the human wrote or rewrote.
function okLabel(phase,picked){
  const base=phase==='HALT'?'이 초안으로 승인':'승인';
  if(picked.other)return base+'\n기타는 수정 요청으로';
  const changed=picked.changed,crit=picked.crit;
  if(crit&&changed.length)return base+'\n변경 '+(changed.length+crit)+'건 반영';
  if(crit)return base+'\n태스크 완료 기준 '+crit+'건 반영';
  if(!changed.length)return base;
  if(changed.length===1)return base+'\n'+changed[0][0]+'번 '+LETTERS[changed[0][1]]+'안';
  return base+'\n선택 '+changed.length+'건 반영';
}
// What a decided card says in place of its buttons.
const DONE_LABEL={APPROVED:'승인되었습니다',REJECTED:'거절되었습니다',REVISE:'수정 요청되었습니다'};
const HALT_DONE_LABEL={APPROVED:'초안이 승인되었습니다',REJECTED:'취소되었습니다',
  REVISE:'계속 진행하도록 했습니다'};
