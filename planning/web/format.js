// Approval page - the small words and numbers the cards show: how big a file is, how
// long a task took, when the agent last reported, and the letters options go by.
// Pure: nothing here reads or changes the page.
const LETTERS='ABCDEFGH';
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
function clock(ts){
  if(typeof ts!=='number')return '';
  const d=new Date(ts*1000);
  return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
}
