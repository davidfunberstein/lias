/* ─── floating viewer / חלון תצוגה צף ─── */
let viewerSources = {};
let viewerCtx = null;
let _savedScroll = 0, _scrollLocked = false;

let _docked = false;
function toggleDockViewer(){
  _docked = !_docked;
  $('fv').classList.toggle('docked', _docked);
  document.body.classList.toggle('fv-docked-mode', _docked);
  const b=$('fv-dock'); if(b) b.textContent = _docked ? '⇥ למרכז' : '⇤ לצד';
  _syncOverlays();
}
function _syncOverlays(){
  // Docked viewer floats at the side WITHOUT locking page scroll — the user
  // can scroll the case list and the document independently.
  if(_docked && $('fv').classList.contains('on')){
    if(_scrollLocked){
      _scrollLocked=false;
      document.body.style.position=''; document.body.style.top=''; document.body.style.width='';
      window.scrollTo(0,_savedScroll);
    }
    $('fv-bg').classList.remove('on');
    return;
  }
  const any = $('fv').classList.contains('on') || $('fvl').classList.contains('on');
  if(any && !_scrollLocked){
    _scrollLocked = true;
    _savedScroll = window.scrollY;
    document.body.style.position='fixed';
    document.body.style.top = `-${_savedScroll}px`;
    document.body.style.width='100%';
    $('fv-bg').classList.add('on');
  } else if(!any && _scrollLocked){
    _scrollLocked = false;
    document.body.style.position=''; document.body.style.top=''; document.body.style.width='';
    window.scrollTo(0, _savedScroll);
    $('fv-bg').classList.remove('on');
  }
}
function openDoc(id, name){
  viewerCtx = null;
  _showViewer(id, name);
}
function openDocAt(src, idx){
  const list = viewerSources[src]||[];
  const d = list[idx]; if(!d) return;
  viewerCtx = {src, idx};
  _showViewer(d.document_id, d.logical_name||d.physical_name||'');
}
let _listBehind = false;
async function _showViewer(id, name){
  if($('fvl').classList.contains('on')){ $('fvl').classList.remove('on'); _listBehind = true; }
  const url = '/api/doc/'+id;
  $('fv-title').textContent = name || 'מסמך';
  $('fv-frame').dataset.url = url;
  const list = viewerCtx ? (viewerSources[viewerCtx.src]||[]) : [];
  $('fv-prev').disabled = !viewerCtx || viewerCtx.idx<=0;
  $('fv-next').disabled = !viewerCtx || viewerCtx.idx>=list.length-1;
  $('fv').classList.add('on');
  _syncOverlays();
  let frame=$('fv-frame'); const docEl=$('fv-doc'), fb=$('fv-fallback');
  try{ frame.contentWindow.onbeforeunload=null; }catch(_){}
  frame.removeAttribute('src'); frame.parentNode.replaceChild(frame.cloneNode(), frame);
  frame = $('fv-frame');
  frame.addEventListener('load', ()=>{ try{ frame.contentWindow.onbeforeunload=null; }catch(_){} });
  frame.style.display='none'; docEl.style.display='none'; fb.style.display='none';
  let ct='';
  try{
    const h = await fetch(url,{method:'HEAD'});
    if(h.ok) ct = h.headers.get('Content-Type')||'';
    else if(h.status===404){
      fb.style.display='grid';
      fb.textContent = 'המסמך חסר בדיסק — ייתכן שהוסר או לא הורד';
      return;
    }
  }catch(e){}
  const isWord = ct.includes('wordprocessingml') || ct.includes('msword')
              || /\.(docx?)($|\?)/i.test(name||'');
  if(isWord){
    const idm = (url||'').match(/\/api\/doc\/(\d+)/);
    if(idm){
      try{
        const h2 = await fetch('/api/doc_view/'+idm[1], {method:'HEAD'});
        if(h2.ok){ frame.src='/api/doc_view/'+idm[1]; frame.style.display='block'; return; }
      }catch(e){}
    }
    if(window.mammoth){
      docEl.style.display='block';
      docEl.innerHTML = '<div class="page" style="min-height:120px"><span style="color:#999">טוען מסמך Word…</span></div>';
      try{
        const buf = await (await fetch(url)).arrayBuffer();
        const res = await mammoth.convertToHtml({arrayBuffer:buf});
        docEl.innerHTML = '<div class="page">'+(res.value || '<span style="color:#999">אין תוכן להצגה</span>')+'</div>';
      }catch(e){ docEl.innerHTML = '<div class="page"><span style="color:var(--danger)">שגיאה בטעינת Word: '+e+'</span></div>'; }
      return;
    }
    // No converter available — show a message instead of downloading the file
    // (a download would launch the Word application, which we never want).
    fb.style.display='grid';
    fb.innerHTML = 'לא ניתן להציג Word בדפדפן במחשב זה.<br>' +
      '<button class="fv-btn" style="margin-top:8px" onclick="window.open($(\'fv-frame\').dataset.url)">הורד קובץ ידנית</button>';
  } else {
    frame.src = url; frame.style.display='block';
  }
}
function viewerStep(dir){
  if(!viewerCtx) return;
  const next = viewerCtx.idx - dir;
  const list = viewerSources[viewerCtx.src]||[];
  if(next<0 || next>=list.length) return;
  openDocAt(viewerCtx.src, next);
}
function closeViewer(){
  if(!$('fv').classList.contains('on')) return;
  $('fv').classList.remove('on');
  if(_docked){ _docked=false; $('fv').classList.remove('docked');
    document.body.classList.remove('fv-docked-mode');
    const b=$('fv-dock'); if(b) b.textContent='⇤ לצד'; }
  const cf=$('fv-frame'); try{ cf.contentWindow.onbeforeunload=null; }catch(_){}
  cf.removeAttribute('src'); cf.parentNode.replaceChild(cf.cloneNode(), cf);
  $('fv-doc').innerHTML='';
  if(_listBehind){ $('fvl').classList.add('on'); _listBehind=false; }
  _syncOverlays();
}

/* ─── minimize to bottom pills ─── */
const MINI_LABEL = {fv:'📄 ', fvl:'🗂 ', drawer:'📚 '};
function minimizeOverlay(which){
  const el = $(which);
  if(!el || !el.classList.contains('on')) return;
  el.classList.remove('on');
  if(which==='drawer'){ $('drawer-bg').classList.remove('on'); }
  _syncOverlays();
  const title = which==='fv' ? $('fv-title').textContent
              : which==='fvl' ? $('fvl-title').textContent : 'תיקים';
  const pill = document.createElement('button');
  pill.className='mini-pill';
  pill.textContent = (MINI_LABEL[which]||'')+ (title||'חלון');
  pill.onclick = ()=>{
    pill.remove();
    if(which==='drawer'){ $('drawer').classList.add('on'); $('drawer-bg').classList.add('on'); }
    else{ el.classList.add('on'); _syncOverlays(); }
  };
  $('mini-tray').appendChild(pill);
}

/* ─── drag floating windows by their title bar ─── */
function _makeDraggable(winId, barId){
  const win=$(winId), bar=$(barId);
  if(!win||!bar) return;
  bar.addEventListener('mousedown', e=>{
    if(e.target.closest('button,select,input')) return;
    const r = win.getBoundingClientRect();
    win.style.transform='none'; win.style.right='auto';
    win.style.left=r.left+'px'; win.style.top=r.top+'px';
    const sx=e.clientX-r.left, sy=e.clientY-r.top;
    const mv = ev=>{ win.style.left=(ev.clientX-sx)+'px'; win.style.top=Math.max(0,ev.clientY-sy)+'px'; };
    const up = ()=>{ document.removeEventListener('mousemove',mv); document.removeEventListener('mouseup',up); };
    document.addEventListener('mousemove',mv); document.addEventListener('mouseup',up);
    e.preventDefault();
  });
}
_makeDraggable('fv','fv-top');
_makeDraggable('fvl','fvl-top');

/* ─── floating doc list (opened from charts) ─── */
let _fvlSortDir = -1;   // -1 = newest first
function fvlToggleSort(){ _fvlSortDir = -_fvlSortDir; _renderFvlList(); }
function _renderFvlList(){
  const dk = d=>{
    const p=String(d.submission_date||'').split('/');
    if(p.length===3) return p[2].padStart(4,'20')+p[1].padStart(2,'0')+p[0].padStart(2,'0');
    const m=(d.logical_name||d.physical_name||'').match(/(\d{4})[_.-](\d{2})[_.-](\d{2})/);
    return m? m[1]+m[2]+m[3] : '00000000';
  };
  const docs = [...(viewerSources.list||[])].sort((a,b)=> -_fvlSortDir * dk(a).localeCompare(dk(b)));
  viewerSources.list = docs;
  const btn = `<button class="fv-btn" style="font-size:11.5px;margin-bottom:8px" onclick="fvlToggleSort()">
    מיון לפי תאריך ${_fvlSortDir===-1?'⬇ מהחדש לישן':'⬆ מהישן לחדש'}</button>`;
  $('fvl-body').innerHTML = btn + (docs.map((d,i)=>`
    <div class="dl-item" onclick="openDocAt('list',${i})">
      <b>${d.logical_name||d.physical_name||'—'}</b>
      <span>${d.doc_type?d.doc_type.split(' - ')[0]:'—'} · ${d.sub_number||''} · ${d.submission_date||''}
        · ${(d.submitter_est||'').trim()||'לא צוין'}</span></div>`).join('')
    || '<div class="empty">אין מסמכים תואמים</div>');
}
async function openDocList(title, params){
  $('fvl-title').textContent = title;
  $('fvl-body').innerHTML = '<div class="empty">טוען…</div>';
  $('fvl-count').textContent = '';
  $('fvl').classList.add('on'); _syncOverlays();
  try{
    const r = await (await fetch('/api/docs?'+new URLSearchParams({limit:200, ...params}))).json();
    viewerSources.list = r.docs||[];
    $('fvl-count').textContent = `${nf.format(r.total||0)} מסמכים`;
    _renderFvlList();
  }catch(e){ $('fvl-body').innerHTML = '<div class="empty">שגיאה בטעינה</div>'; }
}
function closeDocList(){
  if(!$('fvl').classList.contains('on')) return;
  $('fvl').classList.remove('on');
  _syncOverlays();
}
function closeTopOverlay(){
  if($('fv').classList.contains('on')) closeViewer();
  else closeDocList();
}
document.addEventListener('keydown', e=>{
  if(e.key==='Escape') closeTopOverlay();
  if($('fv').classList.contains('on') && viewerCtx){
    if(e.key==='ArrowLeft') viewerStep(-1);
    if(e.key==='ArrowRight') viewerStep(1);
  }
});

/* ─── top search: cases / clients / parties / court ─── */
let _searchTimer=null;
function topSearch(q){
  q=(q||'').trim();
  const box=$('search-results'); if(!box) return;
  clearTimeout(_searchTimer);
  if(q.length<2){ box.style.display='none'; return; }
  _searchTimer=setTimeout(async ()=>{
    let r;
    try{ r = await (await fetch('/api/search?q='+encodeURIComponent(q))).json(); }
    catch(e){ box.style.display='none'; return; }
    const arkIcon = a => (a||'').includes('רבני')?'🕍':(a||'').includes('הוצאה')?'⚖️':'🏛';
    let html='';
    if(r.clients?.length){
      html += `<div class="sr-head">לקוחות</div>` + r.clients.map(c=>
        `<div class="sr-item" onclick="_srGo('client',${c.client_id})">👤 <b>${c.display_name}</b></div>`).join('');
    }
    if(r.cases?.length){
      html += `<div class="sr-head">תיקים</div>` + r.cases.map(c=>
        `<div class="sr-item" onclick="_srGo('case',${c.sub_case_id})">
          ${arkIcon(c.arkaa)} <b>${c.sub_number}</b>
          <span style="opacity:.6;font-size:11px"> · ${c.arkaa||c.portal}${c.client?' · '+c.client:''}</span></div>`).join('');
    }
    if(r.docs?.length){
      html += `<div class="sr-head">מסמכים</div>` + r.docs.map(d=>
        `<div class="sr-item" onclick="openDoc(${d.document_id},'${(d.logical_name||'').replace(/'/g,'')}');_srClose()">
          📄 ${d.logical_name||'מסמך'} <span style="opacity:.6;font-size:11px"> · ${d.sub_number||''}</span></div>`).join('');
    }
    html += await _portalCasesSection(q);
    box.innerHTML = html || '<div class="sr-item" style="opacity:.6">לא נמצאו תוצאות</div>';
    box.style.display='block';
  }, 220);
}

/* Unified portal view: every case known from NET + BDR + הוצל"פ, whether or not
   its documents were downloaded. Served from the on-disk cache, so it answers
   without any portal login — and marks what is still missing. */
async function _portalCasesSection(q){
  let d;
  try{ d = await (await fetch('/api/proxy/cases/all?q='+encodeURIComponent(q))).json(); }
  catch(_){ return ''; }
  const cases = (d && d.cases) || [];
  if(!cases.length) return '';
  const ICON = {NET:'🏛', BDR:'🕍', ECA:'⚖️'};
  const chip = c => {
    const s=(c.status||'').trim();
    if(!s) return '';
    const closed=/סגור|closed/i.test(s);
    return `<span style="font-size:10px;padding:1px 6px;border-radius:6px;
      background:${closed?'rgba(150,150,150,.22)':'rgba(46,160,90,.20)'};
      color:${closed?'var(--ink-soft)':'#2ea05a'}">${closed?'●':'●'} ${s}
      ${closed&&c.close_date?' · '+c.close_date:''}</span>`;
  };
  const partiesLine = c => {
    const ps=(c.parties||[]).filter(p=>p&&p.name);
    if(ps.length) return ps.map(p=>`<span style="opacity:.65">${p.role||'צד'}:</span> ${p.name}`)
                           .join(' • ');
    return c.client ? `<span style="opacity:.65">לקוח:</span> ${c.client}` : '';
  };
  const missing = cases.filter(c=>!c.downloaded).length;
  return `<div class="sr-head">תיקים בפורטלים · ${cases.length}`
    + (missing?` · <span style="color:var(--warn,#e6a800)">${missing} טרם הורדו</span>`:'')
    + `</div>` + cases.slice(0,25).map(c=>`
    <div class="sr-item" onclick="_srPortalCase('${(c.number||'').replace(/'/g,'')}')">
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span>${ICON[c.portal]||'📁'}</span><b>${c.number}</b>
        <span style="opacity:.6;font-size:11px">${c.portal_label||''}${c.court?' · '+c.court:''}</span>
        ${chip(c)}
        ${c.downloaded
          ? `<span style="font-size:10px;color:var(--accent-strong)">✓ הורד · ${c.doc_count} מסמכים</span>`
          : `<span style="font-size:10px;color:var(--warn,#e6a800);font-weight:700">✗ טרם הורד</span>`}
      </div>
      <div style="font-size:11.5px;opacity:.85;margin-top:2px">${partiesLine(c)}</div>
    </div>`).join('');
}
/* Jump to the case in the dashboard if it was imported; otherwise explain. */
function _srPortalCase(num){
  const card = (window.D&&D.case_cards||[]).find(c=>(c.sub_number||'').includes(num));
  if(card){ _srClose(); $('topsearch').value=''; go('case', card.sub_case_id); return; }
  _srClose();
  toast(`תיק ${num} מוכר מהפורטל אך טרם יובא לדשבורד — הרץ סנכרון לתיק זה`, true);
}
function _srGo(v,id){ _srClose(); $('topsearch').value=''; go(v,id); }
function _srClose(){ const b=$('search-results'); if(b) b.style.display='none'; }
document.addEventListener('click', e=>{
  if(!e.target.closest('.search')) _srClose();
});

/* ─── pins and notes ─── */
const TOPIC_COLORS = ['#2F7DF6','#F5A623','#3B82F6','#E85D75','#9B59B6','#1ABC9C','#95A5A6'];
let _notesCache = null;
async function loadNotes(force){
  if(_notesCache && !force) return _notesCache;
  try{ _notesCache = await (await fetch('/api/notes')).json(); }
  catch(e){ _notesCache = {items:[]}; }
  return _notesCache;
}
function topicColor(topic, topics){
  return TOPIC_COLORS[Math.max(0,topics.indexOf(topic)) % TOPIC_COLORS.length];
}
async function pinDoc(docId, name, subNumber){
  const notes = await loadNotes();
  const topics = [...new Set(notes.items.map(i=>i.topic))];
  const old = $('pin-box'); if(old) old.remove();
  const d = document.createElement('div');
  d.id='pin-box';
  d.style.cssText='position:fixed;top:50%;right:50%;transform:translate(50%,-50%);z-index:135;'
    +'background:var(--surface,#fff);border:1px solid var(--line,#e5e5e5);border-radius:14px;'
    +'padding:18px;box-shadow:0 16px 60px rgba(0,0,0,.35);width:min(360px,92vw);direction:rtl';
  d.innerHTML = `<b style="font-size:14px">📌 תיוק מסמך</b>
    <div class="sub" style="margin:4px 0 10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${name}</div>
    <input id="pin-topic" list="pin-topics" placeholder="נושא (למשל: מזונות, לערעור…)"
      style="width:100%;border:1px solid var(--line);border-radius:8px;padding:9px;font-size:13.5px;margin-bottom:8px">
    <datalist id="pin-topics">${topics.map(t=>`<option value="${t}">`).join('')}</datalist>
    ${topics.length?`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${topics.map(t=>
      `<button class="map-chip" onclick="$('pin-topic').value='${t.replace(/'/g,"\\'")}'">${t}</button>`).join('')}</div>`:''}
    <textarea id="pin-note" placeholder="הערה חופשית…" rows="3"
      style="width:100%;border:1px solid var(--line);border-radius:8px;padding:9px;font-size:13.5px;resize:vertical"></textarea>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn-accent" style="flex:1" onclick="savePin(${docId},'${(name||'').replace(/'/g,'')}','${(subNumber||'').replace(/'/g,'')}')">שמור 📌</button>
      <button class="fv-btn" onclick="$('pin-box').remove()">בטל</button></div>`;
  document.body.appendChild(d);
  $('pin-topic').focus();
}
async function savePin(docId, name, subNumber){
  const topic = ($('pin-topic')?.value||'').trim() || 'כללי';
  const note = ($('pin-note')?.value||'').trim();
  const r = await (await fetch('/api/notes/save',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({document_id:docId, doc_name:name, sub_number:subNumber, topic, note})})).json();
  $('pin-box')?.remove();
  if(r.ok){ toast(`נשמר בנושא "${topic}" 📌`); loadNotes(true); }
  else toast('שגיאה בשמירה', true);
}
async function openPinboard(){
  const notes = await loadNotes(true);
  const topics = [...new Set(notes.items.map(i=>i.topic))];
  $('fvl-title').textContent = '📌 המרכזייה שלי — לפי נושאים';
  $('fvl-count').textContent = `${notes.items.length} פריטים`;
  $('fvl').classList.add('on'); _syncOverlays();
  $('fvl-body').innerHTML = topics.length? topics.map(t=>{
    const items = notes.items.filter(i=>i.topic===t);
    return `<details open style="margin-top:10px">
      <summary style="font-weight:800;cursor:pointer;padding:5px 0">
        <span class="dot" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${topicColor(t,topics)};margin-left:6px"></span>
        ${t} · ${items.length}</summary>
      ${items.map(i=>`<div class="dl-item" style="cursor:pointer" onclick="openDoc(${i.document_id},'${(i.doc_name||'').replace(/'/g,'')}')">
        <b>${i.doc_name||'מסמך'}</b>
        <span>${i.sub_number||''} · ${(i.created||'').replace('T',' ')}
          ${i.note?`<br>💬 ${i.note}`:''}</span>
        <button class="fv-btn" style="font-size:10.5px;float:left" onclick="event.stopPropagation();delPin(${i.id})">הסר</button>
      </div>`).join('')}</details>`;
  }).join('') : '<div class="empty">אין עדיין פריטים מתויקים.<br>לחץ 📌 ליד כל מסמך כדי לשמור אותו עם הערה לנושא.</div>';
}
async function delPin(id){
  await fetch('/api/notes/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id})});
  openPinboard();
}
