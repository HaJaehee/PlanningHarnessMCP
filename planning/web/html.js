// Approval page - escaping. Everything the server sends is text: goals, task titles,
// evidence, file paths. esc() keeps it text wherever a view puts it into the page.
// Quotes are escaped too so the same helper is safe inside an attribute value.
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
  '"':'&quot;',"'":'&#39;'}[c]));}
