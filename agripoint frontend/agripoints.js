(function(){
 // Uses the SAME sign-in token the main AgriWaste app stores after login. No separate auth.
 var tok=localStorage.getItem('tok'),page=document.body.dataset.page;
 if(!tok){location.href='/';return}
 function api(p){return fetch('/api/agripoints'+p,{headers:{Authorization:'Bearer '+tok}}).then(function(r){
  return r.json().catch(function(){return{}}).then(function(d){
   if(r.status===401){localStorage.removeItem('tok');location.href='/';throw 0}
   if(!r.ok)throw new Error(r.status===403?'AgriPoints are available to farmer accounts only.':(d.error||'Could not load AgriPoints.'));return d})})}
 function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
 var n=function(v){return Number(v||0).toLocaleString()};
 function fail(m){if(m===0)return;var d=el('div','ap-err',m.message||'Something went wrong.');var root=document.getElementById('root');root.textContent='';root.appendChild(d)}
 var out=document.getElementById('signout');if(out)out.onclick=function(e){e.preventDefault();localStorage.removeItem('tok');localStorage.removeItem('me');location.href='/'};

 if(page==='home')api('/summary').then(function(s){
  document.getElementById('bal').textContent=n(s.balance);document.getElementById('earned').textContent=n(s.total_earned);document.getElementById('month').textContent=n(s.month_earned);
  var box=document.getElementById('rules');box.textContent='';
  s.rules.forEach(function(r){var row=el('div','ap-rule');row.appendChild(el('span','',r.description));row.appendChild(el('b','','+'+r.points+' pts'));box.appendChild(row)});
  document.getElementById('content').hidden=false}).catch(fail);

 if(page==='history')api('/transactions').then(function(rows){
  var body=document.getElementById('rows'),empty=document.getElementById('empty'),tbl=document.getElementById('tbl');
  if(!rows.length){empty.hidden=false;tbl.hidden=true}else{empty.hidden=true;tbl.hidden=false}
  rows.forEach(function(t){var tr=el('tr'),pos=t.points>0;
   function cell(l,node){var td=el('td');td.dataset.label=l;td.appendChild(typeof node==='string'?document.createTextNode(node):node);tr.appendChild(td)}
   cell('Date',new Date(t.created_at.replace(' ','T')+'Z').toLocaleString());cell('Description',t.description);
   cell('Reference',t.request_id?'Request #'+t.request_id:t.reference);
   cell('Points',el('span',pos?'earn':'spend',(pos?'+':'−')+n(Math.abs(t.points))));
   cell('Status',el('span','tag '+t.status,t.status[0].toUpperCase()+t.status.slice(1)));cell('Balance',n(t.balance_after));body.appendChild(tr)});
  document.getElementById('content').hidden=false}).catch(fail);
})();
