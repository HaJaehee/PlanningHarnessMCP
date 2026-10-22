// Approval page - building HTML.
//
// A view writes its markup as an html`...` template. Every ${value} in it is escaped,
// so text from the server - goals, task titles, evidence, file paths - stays text; an
// html`` inside (or a list of them) goes in as markup. null, undefined, false and true
// put in nothing, so `${cond&&html`...`}` shows its markup only when cond holds. (A
// number is a value: write `${n>0&&...}`, never `${n&&...}`, or a 0 is printed.)
//
// The source's layout is not the page's: a line break with the indent around it is
// dropped next to a tag or a ${value}, is one space inside a tag (between attributes)
// and is one space between two words. So a template can be indented like the markup it
// builds and still draw exactly what one long line would - with one rule: keep a space
// that must show beside a ${value} on the same line as the value.

// Quotes are escaped too so the same helper is safe inside an attribute value.
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
  '"':'&quot;',"'":'&#39;'}[c]));}
class Html{
  constructor(text){this.text=text;}
  toString(){return this.text;}
}
function html(strings,...values){
  const parts=squeeze(strings);
  let out=parts[0];
  values.forEach((value,i)=>{out+=piece(value)+parts[i+1];});
  return new Html(out);
}
function piece(value){
  if(value==null||value===false||value===true)return '';
  if(value instanceof Html)return value.text;
  if(Array.isArray(value))return value.map(piece).join('');
  return esc(String(value));
}
// The static parts of a template, with the source's line breaks taken out as above.
// Worked out once per template: a template's `strings` is the same array on every call.
const SQUEEZED=new WeakMap();
function squeeze(strings){
  let parts=SQUEEZED.get(strings);
  if(parts)return parts;
  let inTag=false;
  parts=strings.map(text=>{
    const bits=text.split(/(\s*\n\s*)/);
    return bits.map((bit,i)=>{
      if(i%2===0){
        const open=bit.lastIndexOf('<'),close=bit.lastIndexOf('>');
        if(open>close)inTag=true;else if(close>open)inTag=false;
        return bit;
      }
      if(inTag)return ' ';
      const before=bits[i-1],after=bits[i+1];
      return !before||!after||before.endsWith('>')||after.startsWith('<')?'':' ';
    }).join('');
  });
  SQUEEZED.set(strings,parts);
  return parts;
}
