/* ─── sync view ─── */
function renderSync(){
  $('crumbs').innerHTML='';
  $('view').innerHTML = `
  <div class="hello"><div class="small">חיבור לפורטלים והורדת תיקים</div><h1>סנכרון</h1></div>
  <div class="grid">
    <div class="dark c12" id="sync-card"></div>
  </div>`;
  syncCard($('sync-card'));
}

/* ─── lawyer home ─── */
function renderLawyer(){
  $('crumbs').innerHTML='';
  const u=curUser(), k=D?.kpis;
  $('view').innerHTML = `
  <div class="hello"><div class="small">מוכן לנהל את התיקים שלך?</div>
    <h1>ברוך שובך, ${u.name.split(' ')[0]}</h1>
    ${D?.empty?`<div style="margin-top:10px;padding:12px 16px;border-radius:12px;background:var(--accent-soft);border:1px solid var(--accent);color:var(--accent-strong);font-weight:600">
      אין נתונים להצגה — ${D.empty_reason||'בצע סנכרון ראשון בלשונית "סנכרון"'}</div>`:''}</div>
  <div class="grid">
    <div class="card c4 clicky" onclick="openDrawer('clients')">
      <div class="kpi-top"><h3>לקוחות <span class="qtip" data-tip="כל הצדדים שהמערכת זיהתה כלקוחות שלך. לחיצה פותחת את הרשימה.">?</span></h3><div class="kpi-arrow">↗</div></div>
      <div class="kpi-num">${k?nf.format(k.clients):'…'}</div>
      <div class="kpi-sub">לחץ לרשימת הלקוחות</div>
      <div class="kpi-viz" id="viz-clients"></div>
    </div>
    <div class="card c4 clicky" onclick="openDrawer('cases')">
      <div class="kpi-top"><h3>תיקים פעילים <span class="qtip" data-tip="כל התיקים שהורדו מהפורטלים, מכל הערכאות.">?</span></h3><div class="kpi-arrow">↗</div></div>
      <div class="kpi-num">${k?nf.format(k.sub_cases):'…'}</div>
      <div class="kpi-sub">${k?`${D.arkaa.length} ערכאות · NET + BDR`:''}</div>
      <div class="kpi-viz" id="viz-cases"></div>
    </div>
    <div class="card c4">
      <div class="kpi-top"><h3>סה״כ מסמכים</h3><div class="kpi-arrow">↗</div></div>
      <div class="kpi-num">${k?nf.format(k.total_docs):'…'}</div>
      <div class="kpi-sub">${k?`${nf.format(k.pages)} עמודים`:''}</div>
      <div class="meter" id="meter"></div>
      <div class="pillrow" id="pills"></div>
    </div>
    <div class="card c8"><div class="kpi-top"><div><h2>מסמכים לפי חודש</h2>
      <div class="sub">לפי תאריך הגשה בפורטל</div></div></div>
      <div class="chartbox"><canvas id="ch-monthly"></canvas></div></div>
    <div class="card c4"><h2>פילוח לפי ערכאה</h2>
      <div class="sub">לחץ על ערכאה לרשימת התיקים</div>
      <div class="hbar" id="arkaa-bars"></div></div>
    <div class="card c12"><div class="kpi-top"><div><h2>מפת עומס — בקשות והחלטות לפי יום</h2>
      <div class="sub">כל נקודה = יום בתיק; גודל = כמות; ירוק = בקשות, כהה = החלטות. לחץ על נקודה לרשימת המסמכים</div></div></div>
      <div class="map-ctl" id="ch-load-ctl"></div>
      <div class="chartbox" id="ch-load-box" style="height:${Math.max(170, (D?.activity?.cases?.length||3)*46+70)}px"><canvas id="ch-load"></canvas></div></div>
    <div class="card c6"><div class="kpi-top"><div><h2>פילוח תיקים — בחר חיתוך</h2>
      <div class="sub">לחיצה על שורה פותחת את הרשימה</div></div>
      <select id="bd-dim" onchange="fillBreakdown()" style="border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12.5px;background:var(--surface);color:inherit">
        <option value="arkaa">לפי ערכאה</option>
        <option value="case">לפי תיק</option>
        <option value="submitter">לפי מגיש</option>
        <option value="doctype">לפי סוג מסמך</option>
        <option value="portal">לפי פורטל</option>
      </select></div>
      <div class="hbar" id="bd-bars"></div></div>
    <div class="card c8"><div class="kpi-top"><div><h2>מסמכים אחרונים</h2>
      <div class="sub">מכל הלקוחות — העמודה "לקוח" מפרידה ביניהם</div></div></div>
      <table><thead><tr><th>מסמך</th><th>לקוח</th><th>תיק</th><th>תאריך</th><th>סטטוס</th></tr></thead>
      <tbody id="docs-body"></tbody></table></div>
    <div class="card c4 ai"><h2>AI LIAS</h2>
      <div class="hello-ai"><div class="orb"></div>
        <p><b>שלום ${u.name.split(' ')[0]},</b><br>איך אפשר <span style="color:var(--accent-strong);font-weight:700">לעזור היום?</span></p></div>
      <div class="ai-input"><input id="ai-q" placeholder="שאל אותי כל דבר על התיקים…">
        <button onclick="aiAsk()">↖</button></div>
      <div class="chips">
        <button onclick="aiAsk('מה חדש?')">מה חדש</button>
        <button onclick="aiAsk('החלטות אחרונות')">סקירת החלטות</button>
        <button onclick="aiAsk('מועדים')">מועדים</button></div>
      <div style="margin-top:16px"><h2 style="font-size:13px">לקוחות מובילים</h2><div id="clients-list"></div></div>
    </div>
  </div>`;
  if(D) fillLawyer();
}

function fillLawyer(){
  const k=D.kpis;
  miniBars($('viz-clients'), D.clients.map(c=>c.docs), false);
  miniBars($('viz-cases'), D.case_cards.map(c=>c.docs), false);
  const pct = k.total_docs? Math.round(k.completed/k.total_docs*100):0;
  $('meter').innerHTML = `<i style="width:${pct}%;background:var(--accent)"></i>
    <i style="width:${k.total_docs?k.errors/k.total_docs*100:0}%;background:var(--danger)"></i>`;
  $('pills').innerHTML =
    (k.errors?`<span class="pill err" style="cursor:pointer" title="לחץ לרשימת התיקים עם שגיאות" onclick="openDrawer('error_cases')">${k.errors} שגיאות ↗</span>`:'')+
    (k.pending?`<span class="pill pend">${k.pending} ממתינים</span>`:'')+
    (!k.errors&&!k.pending?`<span class="pill ok">הכול תקין ✓</span>`:'');
  if($('sync-card')) syncCard($('sync-card'));
  const maxd = Math.max(...D.arkaa.map(a=>a.docs),1);
  $('arkaa-bars').innerHTML = D.arkaa.map(a=>`
    <div class="row" onclick="openDrawer('cases','${a.label}')">
      <div class="t"><span>${a.label} <span style="color:var(--ink-soft)">· ${a.cases} תיקים</span></span>
        <b>${nf.format(a.docs)}</b></div>
      <div class="bar"><i style="width:${a.docs/maxd*100}%"></i></div></div>`).join('');
  viewerSources.recent = D.recent_docs;
  const _cn = d => (D.clients||[]).find(c=>c.client_id===d.client_id)?.display_name
                || d.client_name || '—';
  $('docs-body').innerHTML = D.recent_docs.map((d,i)=>`
    <tr class="rowlink" onclick="openDocAt('recent',${i})" title="לחץ לפתיחת המסמך"><td><span class="docname" title="${d.logical_name||d.physical_name||''}">${d.logical_name||d.physical_name||'—'}</span></td>
    <td>${_cn(d)}</td>
    <td><span class="case">${d.sub_number||'—'}</span></td><td>${d.submission_date||'—'}</td>
    <td>${pill(d.download_status)}</td></tr>`).join('');
  $('clients-list').innerHTML = D.clients.slice(0,4).map((c,i)=>`
    <div class="client ${i===0?'hot':''}" onclick="go('client',${c.client_id})">
      <div class="num">${i+1}</div><div class="n">${c.display_name}</div>
      <div class="d">${c.cases} תיקים · ${nf.format(c.docs)}</div></div>`).join('');
  monthlyChart('ch-monthly', D.monthly);
  loadMap('ch-load', D.activity);
  fillBreakdown();
}

function fillBreakdown(){
  const el=$('bd-bars'); if(!el||!D) return;
  const dim=$('bd-dim')?.value||'arkaa';
  let rows=[];
  if(dim==='arkaa')
    rows = D.arkaa.map(a=>({label:a.label, n:a.docs, sub:`${a.cases} תיקים`,
      on:()=>openDrawer('cases', a.label)}));
  else if(dim==='case')
    rows = [...D.case_cards].sort((a,b)=>b.docs-a.docs).slice(0,12)
      .map(c=>({label:c.sub_number, n:c.docs, sub:c.arkaa, on:()=>go('case',c.sub_case_id)}));
  else if(dim==='submitter')
    rows = (D.submitters||[]).map(s=>({label:s.label, n:s.count, sub:'',
      on:()=>openDocList('מסמכים — '+s.label, {submitter:s.label})}));
  else if(dim==='doctype')
    rows = (D.doc_types||[]).map(t=>({label:t.label, n:t.count, sub:'',
      on:()=>openDocList('מסמכים — '+t.label, {group:t.label==='אחר'?'':t.label})}));
  else if(dim==='portal'){
    const p={};
    D.case_cards.forEach(c=>{const k=c.arkaa==='בית דין רבני'?'בתי דין רבניים':'נט המשפט';
      (p[k]=p[k]||{c:0,d:0}).c++; p[k].d+=c.docs;});
    rows = Object.entries(p).map(([label,v])=>({label, n:v.d, sub:`${v.c} תיקים`, on:null}));
  }
  const max=Math.max(...rows.map(r=>r.n),1);
  window._bdRows = rows;
  el.innerHTML = rows.map((r,i)=>`
    <div class="row" ${r.on?`onclick="window._bdRows[${i}].on()" style="cursor:pointer"`:''}>
      <div class="t"><span>${r.label}${r.sub?` <span style="color:var(--ink-soft)">· ${r.sub}</span>`:''}</span>
        <b>${nf.format(r.n)}</b></div>
      <div class="bar"><i style="width:${r.n/max*100}%"></i></div></div>`).join('')
    || '<div class="empty">אין נתונים</div>';
}

/* ─── favorites + view helpers ─── */
function _favs(){ try{return JSON.parse(localStorage.getItem('lias_favs'))||[]}catch(e){return []} }
function isFav(id){ return _favs().includes(id); }
function toggleFav(id){
  const f=_favs(); const i=f.indexOf(id);
  if(i>=0) f.splice(i,1); else f.push(id);
  localStorage.setItem('lias_favs', JSON.stringify(f));
  render();
}
let _caseView = localStorage.getItem('lias_case_view')||'tiles';
function setCaseView(v){ _caseView=v; localStorage.setItem('lias_case_view',v); render(); }

/* ─── client view ─── */
function renderClient(){
  const u=curUser();
  const isLawyer = isPro(u);
  $('crumbs').innerHTML = isLawyer
    ? `<a onclick="go('home')">כל הלקוחות</a> ← <b>${C?C.display_name:'…'}</b>` : '';
  const k=C?.kpis;
  $('view').innerHTML = `
  <div class="hello"><div class="small">${isLawyer?'דשבורד לקוח':'התיקים שלך במקום אחד'}</div>
    <h1>${C? C.display_name : (u.role==='CLIENT'? 'שלום, '+u.name.split(' ')[0] : '…')}
    ${D?.demo_mode?'<span class="badge-demo">נתוני דמה</span>':''}</h1></div>
  <div class="grid">
    <div class="card c3 clicky" onclick="openDrawer('cases')" title="לרשימת התיקים">
      <div class="kpi-top"><h3>תיקים</h3><div class="kpi-arrow">↗</div></div>
      <div class="kpi-num">${(k&&k.cases!=null&&!isNaN(k.cases))?nf.format(k.cases):'—'}</div>
      <div class="kpi-sub">לחץ לרשימת התיקים</div></div>
    <div class="card c3 clicky" onclick="openDocList('כל המסמכים — '+(C?.display_name||''), {client_id:C?.client_id})" title="לרשימת כל המסמכים">
      <div class="kpi-top"><h3>מסמכים</h3><div class="kpi-arrow">↗</div></div>
      <div class="kpi-num">${(k&&k.docs!=null&&!isNaN(k.docs))?nf.format(k.docs):'—'}</div><div class="kpi-sub">לחץ לרשימה</div></div>
    <div class="card c3 clicky" onclick="openDocList('בקשות — '+(C?.display_name||''), {client_id:C?.client_id, group:'בקשה'})" title="לרשימת הבקשות">
      <div class="kpi-top"><h3>בקשות</h3><div class="kpi-arrow">↗</div></div>
      <div class="kpi-num">${(k&&k.requests!=null&&!isNaN(k.requests))?nf.format(k.requests):'—'}</div><div class="kpi-sub">לחץ לרשימה</div></div>
    <div class="card c3 clicky" onclick="openDocList('החלטות ופס״ד — '+(C?.display_name||''), {client_id:C?.client_id, group:'החלטה'})" title="לרשימת ההחלטות">
      <div class="kpi-top"><h3>החלטות ופס״ד</h3><div class="kpi-arrow">↗</div></div>
      <div class="kpi-num">${(k&&k.decisions!=null&&!isNaN(k.decisions))?nf.format(k.decisions):'—'}</div><div class="kpi-sub">לחץ לרשימה</div></div>
    <div class="card c12"><div class="kpi-top"><div><h2>התיקים</h2><div class="sub">לחץ על תיק לפירוט מלא · ★ מסמן מועדף שמוצג תמיד למעלה</div></div>
      <div style="display:flex;gap:6px">
        <button class="fv-btn" id="cv-tiles" onclick="setCaseView('tiles')" title="תצוגת משבצות">▦</button>
        <button class="fv-btn" id="cv-rows" onclick="setCaseView('rows')" title="תצוגת שורות">☰</button>
      </div></div>
      <div id="fav-cases"></div>
      <div class="grid" style="margin-top:14px" id="case-cards"></div></div>
    <div class="card c12"><div class="kpi-top"><div><h2>מפת עומס — בקשות והחלטות לפי יום</h2>
      <div class="sub">כל נקודה = יום בתיק; גודל = כמות; ירוק = בקשות, כהה = החלטות. לחץ על נקודה לרשימת המסמכים</div></div></div>
      <div class="map-ctl" id="ch-cload-ctl"></div>
      <div class="chartbox" id="ch-cload-box" style="height:${Math.max(170, (C?.activity?.cases?.length||3)*46+70)}px"><canvas id="ch-cload"></canvas></div></div>
    <div class="card c6"><h2>מועדי דיון והגשות</h2>
      <div class="sub">ייאסף אוטומטית מהפורטלים בשלב 1 · יכלול ייצוא CSV</div>
      <div class="empty">📅 אין עדיין מועדים במערכת.<br>
        טבלאות hearings + deadlines מוגדרות באפיון §5 —<br>ימולאו מהסנכרון הבא אחרי מימוש שלב 1.</div></div>
    <div class="card c3"><h2>פילוח לפי צד מגיש</h2>
      <div class="chartbox" style="height:150px"><canvas id="ch-sub"></canvas></div>
      <div class="legend" id="sub-legend"></div></div>
    <div class="card c3"><h2>פעילות לפי חודש</h2>
      <div class="chartbox" style="height:210px"><canvas id="ch-cmonthly"></canvas></div></div>
  </div>`;
  if(C) fillClient();
}

function fillClient(){
  const freshChip = c=>{
    const closed = caseStatusMap()[c.sub_case_id]==='closed';
    if(closed) return '<span class="fresh gray">סגור</span>';
    if(!c.last) return '';
    const days = Math.floor((Date.now()-new Date(c.last))/864e5);
    const dstr = c.last.split('-').reverse().join('/');
    if(days<=7)  return `<span class="fresh green" title="עודכן לאחרונה: ${dstr}">✓ לפני ${days} ימים</span>`;
    if(days>30) return `<span class="fresh orange" title="עודכן לאחרונה: ${dstr}">⚠ ${dstr} · לפני ${days} ימים</span>`;
    return `<span class="fresh gray" title="עודכן לאחרונה">${dstr}</span>`;
  };
  const card = c=>{
    const dec=(c.groups['החלטה']||0)+(c.groups['פסק דין']||0);
    const who = (c.parties&&c.parties.length>=2)? c.parties.join(' נ׳ ') : '';
    const where = [c.arkaa, c.location].filter(Boolean).join(' · ');
    const fav = isFav(c.sub_case_id);
    return `<div class="card ${_caseView==='rows'?'c12 crow':'c4'} clicky ccard" onclick="go('case',${c.sub_case_id})">
      <div class="head"><b>${c.sub_number}</b>${freshChip(c)}
        <span class="favstar ${fav?'on':''}" onclick="event.stopPropagation();toggleFav(${c.sub_case_id})" title="הוסף/הסר ממועדפים">${fav?'★':'☆'}</span>
        ${arkaaTag(c.arkaa,c.portal)}</div>
      ${who?`<div class="whoWhere">⚖ <b>${who}</b></div>`:''}
      <div class="whoWhere" style="opacity:.75">${where}</div>
      <div class="range">${c.first?c.first.split('-').reverse().join('/'):''} — ${c.last?c.last.split('-').reverse().join('/'):''}</div>
      <div class="nums">
        <div><div class="n">${nf.format(c.docs)}</div><div class="t">מסמכים</div></div>
        <div><div class="n">${c.groups['בקשה']||0}</div><div class="t">בקשות</div></div>
        <div><div class="n">${dec}</div><div class="t">החלטות</div></div></div>
      <div class="strip">${strip(c.groups,c.other,c.docs)}</div></div>`;
  };
  // favorites first — always shown on top
  const favs = C.case_cards.filter(c=>isFav(c.sub_case_id));
  const favEl = $('fav-cases');
  if(favEl) favEl.innerHTML = favs.length
    ? `<div style="margin:10px 0 -4px;font-weight:800;font-size:13px">★ תיקים במעקב</div>
       <div class="grid" style="margin-top:8px">${favs.map(card).join('')}</div>
       <div style="border-bottom:1px solid var(--line);margin:14px 0 4px"></div>`
    : '';
  const byArkaa = {};
  C.case_cards.forEach(c=>{ (byArkaa[c.arkaa]=byArkaa[c.arkaa]||[]).push(c); });
  const _arkaaOrder = {'בית דין רבני הגדול':1, 'בית דין רבני':2, 'בית דין רבני אזורי':3,
    'בית משפט עליון':4, 'בית משפט מחוזי':5, 'בית משפט לענייני משפחה':6,
    'בית משפט שלום':7, 'בית משפט':8, 'הוצאה לפועל':9};
  const _arkaaIcon = a => a.includes('רבני')?'🕍': a.includes('הוצאה')?'⚖️':'🏛️';
  // Hierarchical, collapsible layout: ערכאה → תיק ראשי → תתי-תיקים.
  // Everything starts CLOSED; whatever the user opens is remembered
  // (localStorage via _det) so returning to the dashboard keeps the context.
  const mainNum = c => ((c.sub_number||'').match(/^(\d+)/)||[])[1] || 'אחר';
  $('case-cards').innerHTML = Object.entries(byArkaa)
    .sort((a,b)=>(_arkaaOrder[a[0]]||99)-(_arkaaOrder[b[0]]||99))
    .map(([ark,arr])=>{
      const docs = arr.reduce((s,c)=>s+(c.docs||0),0);
      const byMain = {};
      arr.forEach(c=>{ (byMain[mainNum(c)]=byMain[mainNum(c)]||[]).push(c); });
      const mains = Object.entries(byMain)
        .sort((a,b)=>b[1].reduce((s,c)=>s+c.docs,0)-a[1].reduce((s,c)=>s+c.docs,0))
        .map(([mn,list])=>{
          const md = list.reduce((s,c)=>s+(c.docs||0),0);
          const gWho=(list.find(x=>x.parties&&x.parties.length>=2)||{}).parties;
          const gLoc=(list.find(x=>x.location)||{}).location||'';
          return `<details ${_det('dash:'+ark+':'+mn)} style="margin:8px 0;border:1px solid var(--line);border-radius:10px;padding:6px 12px">
            <summary style="cursor:pointer;font-weight:700;font-size:13.5px;padding:5px 0">
              📁 תיק ${mn}
              ${gWho?`<span style="font-weight:600;font-size:12.5px"> · ${gWho.join(' נ׳ ')}</span>`:''}
              ${gLoc?`<span style="font-weight:400;opacity:.7;font-size:12px"> · 📍 ${gLoc}</span>`:''}
              <span style="font-weight:400;opacity:.6;font-size:12px">· ${list.length} תתי-תיקים · ${nf.format(md)} מסמכים</span>
            </summary>
            <div class="grid" style="margin-top:8px">${list.sort((a,b)=>(b.docs||0)-(a.docs||0)).map(card).join('')}</div>
          </details>`;
        }).join('');
      return `<details class="c12" ${_det('dash:'+ark)} style="margin:6px 0">
        <summary style="display:flex;align-items:center;gap:10px;cursor:pointer;
            padding:10px 14px;border-radius:10px;list-style-position:inside;
            background:rgba(47,125,246,.10);border:1px solid rgba(47,125,246,.25)">
          <span style="font-size:16px">${_arkaaIcon(ark)}</span>
          <b style="font-size:14px">${ark}</b>
          <span style="font-size:12px;opacity:.65">${arr.length} תיקים · ${nf.format(docs)} מסמכים</span>
        </summary>
        ${mains}
      </details>`;
    }).join('')
    || '<div class="empty c12">אין תיקים ללקוח זה</div>';
  donut('ch-sub','sub-legend', C.submitters, {client_id:C.client_id});
  monthlyChart('ch-cmonthly', C.monthly, {client_id:C.client_id});
  loadMap('ch-cload', C.activity);
}

/* ─── case view ─── */
let caseFilters = {hide_approvals:'1', group:'', submitter:'', q:''};
let caseSort = {col:'date', dir:-1};
let caseDates = {from:'', to:''};

function caseStatusMap(){ try{return JSON.parse(localStorage.getItem('lias_case_status'))||{}}catch(e){return {}} }
function toggleCaseStatus(id){
  const m = caseStatusMap();
  m[id] = m[id]==='closed' ? 'open' : 'closed';
  localStorage.setItem('lias_case_status', JSON.stringify(m));
  render();
}
function setCaseSort(col){
  caseSort = {col, dir: caseSort.col===col ? -caseSort.dir : -1};
  fillCase();
}
function setDateFilter(k, v){ caseDates[k]=v; fillCase(); }
function sortArrow(col){ return caseSort.col===col ? (caseSort.dir===1?'↑':'↓') : ''; }
function toggleDatePop(){
  const p=$('date-pop'); if(p) p.style.display = p.style.display==='none'?'block':'none';
}
function _dateFilter(docs){
  if(!caseDates.from && !caseDates.to) return docs;
  return docs.filter(d=>{
    const p=(d.submission_date||'').split('/');
    if(p.length!==3) return false;
    const iso=p[2]+'-'+p[1]+'-'+p[0];
    if(caseDates.from && iso<caseDates.from) return false;
    if(caseDates.to && iso>caseDates.to) return false;
    return true;
  });
}
function _sortDocs(docs){
  const key = {
    name: d=>(d.logical_name||d.physical_name||''),
    type: d=>(d.doc_type||''),
    submitter: d=>((d.submitter_est||'').trim()||'~'),
    date: d=>{
      const p=(d.submission_date||'').split('/');
      if(p.length===3) return p[2].padStart(4,'20')+p[1].padStart(2,'0')+p[0].padStart(2,'0');
      // fallback: filenames start with a sortable 2026_03_29 prefix
      const m=(d.logical_name||d.physical_name||'').match(/(\d{4})[_.-](\d{2})[_.-](\d{2})/);
      return m? m[1]+m[2]+m[3] : '00000000';
    },
    pages: d=>+(d.pages||0),
    status: d=>(d.download_status||''),
  }[caseSort.col];
  return [...docs].sort((a,b)=>{
    const x=key(a), y=key(b);
    return (x<y?-1:x>y?1:0)*caseSort.dir;
  });
}
async function uploadToCase(){
  // Add one or more EXTERNAL documents to this case — saved in the case
  // folder and marked "לא מהתיק" (doc_type: צירוף ידני).
  if(!K?.sub_case_id) return;
  const inp=document.createElement('input');
  inp.type='file'; inp.multiple=true;
  inp.onchange=async ()=>{
    for(const f of inp.files){
      toast(`מעלה "${f.name}"…`);
      try{
        const r=await fetch(`/api/upload_doc?sub_case_id=${K.sub_case_id}&name=${encodeURIComponent(f.name)}`,
          {method:'POST', body:f});
        const j=await r.json();
        if(j.ok) toast(`✓ ${j.file}`);
        else toast('שגיאה: '+(j.error||j.detail||''), true);
      }catch(e){ toast('שגיאה בהעלאה: '+e.message, true); }
    }
    refresh(true);
  };
  inp.click();
}
function uploadForFailed(docIdx){
  const d = viewerSources.case?.[docIdx];
  if(!d || !K?.sub_case_id){ toast('פתח תיק קודם', true); return; }
  const expected = d.physical_name || d.logical_name;
  if(!expected){ toast('אין שם צפוי למסמך', true); return; }
  const inp=document.createElement('input'); inp.type='file';
  inp.onchange=async ()=>{
    const f=inp.files[0]; if(!f) return;
    const ext = (f.name.match(/\.[^.]+$/)||[''])[0];
    const stem = expected.replace(/\.[^.]+$/, '');
    const name = stem + (ext || '.pdf');
    toast(`מעלה כ-"${name}"…`);
    try{
      const r=await fetch(`/api/upload_doc?sub_case_id=${K.sub_case_id}&name=${encodeURIComponent(name)}&document_id=${d.document_id}`,
        {method:'POST', body:f});
      const j=await r.json();
      if(j.ok){ toast(`צורף ✓ — ${j.file}`); refresh(true); }
      else toast('שגיאה: '+(j.error||j.detail||''), true);
    }catch(e){ toast('שגיאה בהעלאה: '+e.message, true); }
  };
  inp.click();
}
async function trashDoc(id, name){
  if(!confirm(`להסיר את "${name}"? הקובץ יועבר לתיקיית .trash (לא נמחק לצמיתות).`)) return;
  await act('trash_doc?document_id='+id, 'הסרת מסמך');
  setTimeout(()=>refresh(true), 800);
}
async function deleteCase(){
  if(!K?.sub_case_id) return;
  if(!confirm(`למחוק את כל התיק "${K.sub_number}"?\nכל הקבצים יעברו ל-.trash (לא מחיקה קשה) וגם לפח בדרייב אם מחובר.`)) return;
  await act('delete_case?sub_case_id='+K.sub_case_id, 'מחיקת תיק');
  setTimeout(()=>go('home'), 1200);
}
async function openCaseInPortal(){
  if(!K) return;
  const portal = K.portal || (K.arkaa==='הוצאה לפועל'?'ECA':K.arkaa==='בית דין רבני'?'BDR':'NET');
  toast('פותח את התיק בפורטל…');
  await act(`open_case_view?portal=${portal}&case_number=${encodeURIComponent(K.sub_number||'')}`,
            `פתיחת תיק ${K.sub_number} בפורטל`);
}
async function shareCase(){
  if(!K?.sub_case_id) return;
  const emails = prompt('מיילים לשיתוף התיק הזה בצפייה בלבד (מופרדים בפסיק):');
  if(!emails) return;
  const client = (D?.clients||[]).find(c=>c.client_id===K.client_id)?.display_name||'';
  const folder = `downloads/${client}/${K.sub_number}`;
  await act(`drive_share?scope=case&case_folder=${encodeURIComponent(folder)}&emails=${encodeURIComponent(emails)}`,
            'שיתוף התיק');
}

function renderCase(){
  const u=curUser();
  $('crumbs').innerHTML =
    (isPro(u)?`<a onclick="go('home')">כל הלקוחות</a> ← `:'')+
    `<a onclick="go('client',${K?K.client_id:''})">${isPro(u)?'הלקוח':'התיקים שלי'}</a> ← <b>${K?K.sub_number:'…'}</b>`;
  const s=K?.stats||{};
  $('view').innerHTML = `
  <div class="hello"><div class="small">${K?K.arkaa:''}</div>
    <h1 style="font-size:26px">${K?K.sub_number:'טוען…'} ${K?arkaaTag(K.arkaa,K.portal):''}
      ${K?(( ()=>{const ls=caseStatusMap()[K.sub_case_id]; const st=ls||(K.portal_status&&/סגור|closed/i.test(K.portal_status)?'closed':'open'); return st==='closed'
        ? `<span class="pill gray">תיק סגור</span> <button class="fv-btn" style="font-size:11px" onclick="toggleCaseStatus(${K.sub_case_id})">סמן כפתוח</button>`
        : `<span class="pill ok">תיק פתוח</span> <button class="fv-btn" style="font-size:11px" onclick="toggleCaseStatus(${K.sub_case_id})">סמן כסגור</button>`;})() ):''}
      ${K?`<button class="fv-btn" style="font-size:11px" onclick="openCaseInPortal()" title="פתח את התיק בפורטל בדפדפן לצפייה ויזואלית">🌐 פתח בפורטל</button>
        <button class="fv-btn" style="font-size:11px" onclick="uploadToCase()" title="העלה מסמך אחד או כמה לתיק — יסומנו 'לא מהתיק'">➕ הוסף מסמכים</button>
        <button class="fv-btn" style="font-size:11px" onclick="shareCase()" title="שתף רק את התיק הזה בצפייה">🔗 שתף תיק</button>
        <button class="fv-btn" style="font-size:11px;color:var(--danger)" onclick="deleteCase()" title="מחק את כל התיק (ל-trash + דרייב)">🗑 מחק תיק</button>`:''}</h1>
    ${K&&(K.parties?.length||K.location)?`<div class="sub" style="margin-top:8px;padding:8px 12px;border-radius:8px;background:var(--accent-soft,rgba(47,125,246,.08))">
      ${K.parties?.length?`⚖ <b>הצדדים:</b> ${K.parties.join(' &nbsp;נ׳&nbsp; ')}`:''}
      ${K.location?` &nbsp;·&nbsp; 📍 <b>${K.location}</b>`:''}</div>`:''}
    <div class="sub" style="margin-top:6px">${K?`לקוח: <b>${(D?.clients||[]).find(c=>c.client_id===K.client_id)?.display_name||'—'}</b>
      · ערכאה: <b>${K.arkaa}</b> · פורטל: <b>${K.portal}</b>
      · מסמך ראשון: <b>${K.first_date||'—'}</b> · מסמך אחרון: <b>${K.last_date||'—'}</b>
      ${K.last_sync?` · 🔄 סונכרן מול הפורטל: <b>${(K.last_sync.at||'').replace('T',' ').slice(0,16)}</b>${K.last_sync.new?` (+${K.last_sync.new} חדשים)`:''}`:' · <span style="opacity:.6">טרם סונכרן ישירות</span>'}
      · סה״כ: <b>${nf.format(K.total)}</b> מסמכים
      (${['בקשה','תגובה','החלטה','פסק דין','פרוטוקול'].map(g=>`${g}: ${K.stats?.[g]??0}`).join(' · ')})`:''}</div></div>
  <div class="grid">
    <div class="card c3"><div class="kpi-top"><h3>מסמכים בתיק</h3></div>
      <div class="kpi-num">${K?nf.format(K.total):'…'}</div>
      <div class="kpi-sub" id="shown-sub"></div></div>
    <div class="card c3 clicky" onclick="setFilter('group','בקשה')" title="לחץ לסינון הטבלה לבקשות">
      <div class="kpi-top"><h3>בקשות</h3><div class="kpi-arrow">↓</div></div>
      <div class="kpi-num">${s['בקשה']??'…'}</div><div class="kpi-sub">תגובות: ${s['תגובה']??0} · לחץ לסינון</div></div>
    <div class="card c3 clicky" onclick="setFilter('group','החלטה')" title="לחץ לסינון הטבלה להחלטות">
      <div class="kpi-top"><h3>החלטות ופס״ד</h3><div class="kpi-arrow">↓</div></div>
      <div class="kpi-num">${(s['החלטה']??0)+(s['פסק דין']??0)}</div>
      <div class="kpi-sub">פרוטוקולים: ${s['פרוטוקול']??0} · לחץ לסינון</div></div>
    <div class="card c3"><div class="kpi-top"><h3>אישורים (רעש)</h3></div>
      <div class="kpi-num" style="color:var(--ink-soft)">${s['אישור']??'…'}</div>
      <div class="kpi-sub">מוסתרים כברירת מחדל</div></div>
    <div class="card c9">
      <div class="kpi-top"><h2>מסמכים</h2></div>
      <div class="filters">
        <label class="chk"><input type="checkbox" id="f-approvals" ${caseFilters.hide_approvals==='1'?'checked':''}
          onchange="setFilter('hide_approvals', this.checked?'1':'0')"> הסתר אישורים</label>
        <select id="f-group" onchange="setFilter('group',this.value)">
          <option value="">כל הסוגים</option>
          ${['בקשה','תגובה','החלטה','פסק דין','פרוטוקול','אישור','אחר'].map(g=>
            `<option ${caseFilters.group===g?'selected':''}>${g}</option>`).join('')}
        </select>
        <select id="f-sub" onchange="setFilter('submitter',this.value)">
          <option value="">כל המגישים</option>
          ${(K?.submitters||[]).map(s=>
            `<option ${caseFilters.submitter===s.label?'selected':''} value="${s.label}">${s.label} (${s.count})</option>`).join('')}
        </select>
        <input type="text" placeholder="חיפוש חופשי…" value="${caseFilters.q}"
          onchange="setFilter('q',this.value)">
        <span class="hint" id="f-hint"></span>
      </div>
      <table><thead><tr>
        <th class="sortable" onclick="setCaseSort('name')">מסמך ${sortArrow('name')}</th>
        <th class="sortable" onclick="setCaseSort('type')">סוג ${sortArrow('type')}</th>
        <th class="sortable" onclick="setCaseSort('submitter')">מגיש ${sortArrow('submitter')}</th>
        <th style="position:relative;white-space:nowrap">
          <span class="sortable" onclick="setCaseSort('date')">תאריך ${sortArrow('date')}</span>
          <span class="sortable" onclick="event.stopPropagation();toggleDatePop()" title="סינון מ־עד">📅${caseDates.from||caseDates.to?'●':''}</span>
          <span id="date-pop" style="display:none;position:absolute;top:26px;right:0;z-index:20;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px;box-shadow:0 8px 30px rgba(0,0,0,.2);white-space:nowrap">
            מ־<input type="date" value="${caseDates.from}" onchange="setDateFilter('from',this.value)">
            עד <input type="date" value="${caseDates.to}" onchange="setDateFilter('to',this.value)">
            <button class="fv-btn" style="font-size:11px" onclick="caseDates={from:'',to:''};fillCase()">נקה</button>
          </span></th>
        <th class="sortable" onclick="setCaseSort('pages')">עמ׳ ${sortArrow('pages')}</th>
        <th class="sortable" onclick="setCaseSort('status')">סטטוס ${sortArrow('status')}</th>
        <th></th>
      </tr></thead>
      <tbody id="case-docs"></tbody></table></div>
    <div class="card c3"><h2>פילוח לפי צד מגיש</h2>
      <div class="chartbox" style="height:150px"><canvas id="ch-ksub"></canvas></div>
      <div class="legend" id="ksub-legend"></div>
      <div class="sub" style="margin-top:10px;line-height:1.6">שם המגיש הגולמי יוחלף בתג צד
        (הצד שלנו / הצד השני / ביהמ״ש) — זיהוי מי-מייצג-את-מי ב-AI, הצלבת תיקים
        או שאלה ללקוח (אפיון §4.4, שלב 3).</div>
      <div style="margin-top:14px"><h2 style="font-size:13px">תיקים קשורים</h2>
        <div id="related-cases"></div></div>
    </div>
  </div>`;
  if(K) fillCase();
}

function fillCase(){
  $('shown-sub').textContent = `מוצגים ${nf.format(K.shown)} · מוסתרים ${nf.format(K.hidden)}`;
  $('f-hint').textContent = `${nf.format(K.shown)} תוצאות`;
  viewerSources.case = _sortDocs(_dateFilter(K.docs));
  $('case-docs').innerHTML = viewerSources.case.map((d,i)=>`
    <tr class="rowlink" onclick="openDocAt('case',${i})" title="לחץ לפתיחת המסמך"><td><span class="docname" title="${d.logical_name||d.physical_name||''}">${d.logical_name||d.physical_name||'—'}</span></td>
    <td><span class="pill gray">${d.doc_type? d.doc_type.split(' - ')[0] : '—'}</span>${(d.doc_type||'').includes('צירוף ידני')?' <span class="pill pend" title="הועלה ידנית — לא הגיע מהפורטל">לא מהתיק</span>':''}</td>
    <td>${(d.submitter_est||'').trim()||'—'}</td><td>${d.submission_date||'—'}</td>
    <td>${d.pages||''}</td><td>${pill(d.download_status)}</td>
    <td style="white-space:nowrap">
      ${/ERROR|FAILED|Failed/.test(d.download_status||'')?`<span onclick="event.stopPropagation();uploadForFailed(${i})"
        title="צרף ידנית את הקובץ החסר — יישמר בשם התקני" style="cursor:pointer;opacity:.8">📎</span> `:''}
      <span onclick="event.stopPropagation();pinDoc(${d.document_id},'${(d.logical_name||d.physical_name||'').replace(/'/g,'')}','${(K.sub_number||'').replace(/'/g,'')}')"
        title="תייק עם הערה לנושא" style="cursor:pointer;opacity:.55">📌</span>
      <span onclick="event.stopPropagation();trashDoc(${d.document_id},'${(d.logical_name||d.physical_name||'').replace(/'/g,'')}')"
        title="הסר מסמך (לתיקיית trash)" style="cursor:pointer;opacity:.45">🗑</span></td></tr>`).join('')
    || '<tr><td colspan="7" class="empty">אין מסמכים תואמים לפילטר</td></tr>';
  donut('ch-ksub','ksub-legend', K.submitters.filter(s=>s.label!=='לא צוין').slice(0,6),
        {sub_case_id:K.sub_case_id});
  const prefix = (K.sub_number||'').trim().split(/[\s-]/)[0];
  const rel = prefix && D?.case_cards
    ? D.case_cards.filter(c=>c.sub_case_id!==K.sub_case_id && (c.sub_number||'').startsWith(prefix))
    : [];
  if($('related-cases')) $('related-cases').innerHTML = rel.length
    ? rel.map(c=>`<div class="dl-item" onclick="go('case',${c.sub_case_id})" style="cursor:pointer">
        <b>${c.sub_number}</b><span>${nf.format(c.docs)} מסמכים</span></div>`).join('')
    : '<div class="empty" style="padding:12px">אין תתי-תיק נוספים לתיק זה במערכת</div>';
}
function setFilter(k,v){ caseFilters[k]=v; refresh(true); }

/* ─── transcription tab ─── */
let _trPollTimer=null;
function renderTranscribe(){
  $('crumbs').innerHTML='';
  $('view').innerHTML = `
  <div class="hello"><div class="small">תמלול הקלטות באמצעות Whisper — עברית ואנגלית</div><h1>תמלול</h1></div>
  <div class="grid">
    <div class="card c8">
      <h2>העלאת הקלטות</h2>
      <div class="sub" style="margin-bottom:12px">גרור קבצי שמע או לחץ לבחירה. הקלטות ארוכות מחולקות ל-10 דק׳ ומתומללות במקביל.</div>
      <div class="tr-drop" id="tr-drop" onclick="$('tr-file').click()"
        ondragover="event.preventDefault();this.classList.add('over')"
        ondragleave="this.classList.remove('over')"
        ondrop="event.preventDefault();this.classList.remove('over');handleAudioDrop(event.dataTransfer.files)">
        <div style="font-size:36px;margin-bottom:8px">🎙</div>
        <div>גרור הקלטות לכאן או <b>לחץ לבחירה</b></div>
        <div style="font-size:11px;margin-top:4px;color:var(--ink-soft)">mp3 · wav · m4a · ogg · webm — ניתן להעלות מספר קבצים</div>
        <input type="file" id="tr-file" accept="audio/*,.mp3,.wav,.m4a,.ogg,.webm" multiple onchange="handleAudioDrop(this.files)">
      </div>
      <div style="display:flex;gap:10px;align-items:center;margin:10px 0 14px">
        <label style="font-size:13px;font-weight:600">שפה:</label>
        <select id="tr-lang" style="border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px">
          <option value="he">עברית (ivrit.ai)</option>
          <option value="en">English (OpenAI)</option>
        </select>
      </div>
      <div id="tr-active-jobs"></div>
    </div>
    <div class="card c4">
      <h2>תמלולים שנשמרו</h2>
      <div class="sub" style="margin-bottom:10px">קבצי MD — לחץ לצפייה</div>
      <div id="tr-list" class="tr-list"></div>
    </div>
  </div>`;
  loadTranscriptions();
  _restoreActiveTranscriptions();
}
async function handleAudioDrop(files){
  if(!files?.length) return;
  const lang = $('tr-lang')?.value || 'he';
  for(const file of files){
    toast('מעלה '+file.name+'…');
    const form = new FormData();
    form.append('file', file);
    form.append('language', lang);
    try{
      const r = await fetch('/api/transcribe', {method:'POST', body:form});
      const j = await r.json();
      if(j.ok){
        toast(file.name+' — התמלול התחיל');
        pollTranscription(j.id, file.name);
      } else toast(j.error||'שגיאה', true);
    }catch(e){ toast('שגיאת העלאה: '+e.message, true); }
  }
  if($('tr-file')) $('tr-file').value='';
}
const _activeTranscriptions = {};
function pollTranscription(jobId, fname){
  _activeTranscriptions[jobId] = {id:jobId, fname, state:'queued', progress:0, message:'בתור…', partial_lines:[]};
  _renderTrJob(jobId);
  const poll = async()=>{
    try{
      const r = await (await fetch('/api/transcription_status?id='+jobId)).json();
      const t = _activeTranscriptions[jobId];
      if(t){ t.state=r.state; t.progress=r.progress||0; t.message=r.message||r.state||'';
             t.partial_lines=r.partial_lines||[]; t.md_name=r.md_name; }
      _renderTrJob(jobId);
      if(typeof refreshFab==='function') refreshFab();
      if(r.state==='done'){
        _trNotify(fname);
        if(route.v==='transcribe') loadTranscriptions();
        return;
      }
      if(r.state==='error') return;
      if(r.error){
        const t=_activeTranscriptions[jobId];
        if(t){ t.state='error'; t.message='השרת אותחל — יש להעלות שוב'; }
        _renderTrJob(jobId);
        return;
      }
      setTimeout(poll, 2000);
    }catch(e){ setTimeout(poll, 3000); }
  };
  poll();
}
function _renderTrJob(jobId){
  const t = _activeTranscriptions[jobId]; if(!t) return;
  let div = $('tr-job-'+jobId);
  if(!div){
    const el=$('tr-active-jobs'); if(!el) return;
    div = document.createElement('div');
    div.className='tr-progress'; div.id='tr-job-'+jobId;
    div.style.cssText='border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px';
    el.appendChild(div);
  }
  if(t.state==='done'){
    div.innerHTML=`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
      <b style="color:var(--accent-strong)">✅ ${t.fname}</b> — הושלם!
      <a href="#" onclick="event.preventDefault();viewTranscription('${t.md_name}')"
        style="color:var(--accent-strong);font-weight:700;text-decoration:underline">צפה בתמלול</a>
      <a href="/api/transcription_audio/${t.id}" download style="font-size:11px;color:var(--ink-soft);text-decoration:underline">⬇ הורד הקלטה</a></div>
      <audio controls preload="none" src="/api/transcription_audio/${t.id}" style="width:100%;height:32px"></audio>`;
    return;
  }
  if(t.state==='error'){
    div.innerHTML=`<b style="color:var(--danger)">❌ ${t.fname}</b> — ${t.message||'שגיאה'}`;
    return;
  }
  div.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <b>🎙 ${t.fname}</b>
    <span style="font-size:12px;color:var(--ink-soft)">${Math.round(t.progress*100)}%
      <a href="/api/transcription_audio/${t.id}" download style="margin-right:8px;color:var(--ink-soft)">⬇</a></span></div>
    <span style="font-size:12px;color:var(--ink-soft)">${t.message}</span>
    <div style="height:6px;background:var(--line);border-radius:4px;overflow:hidden;margin:8px 0">
      <div style="height:100%;width:${t.progress*100}%;background:var(--accent);transition:width .4s"></div></div>
    <audio controls preload="none" src="/api/transcription_audio/${t.id}" style="width:100%;height:32px;margin:6px 0"></audio>`
    + (t.partial_lines?.length ? `<div style="max-height:260px;overflow-y:auto;font-size:12px;line-height:1.8;
      background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:8px;
      direction:rtl;white-space:pre-wrap;font-family:Heebo,sans-serif;color:var(--ink-soft)">${t.partial_lines.join('\n')}</div>` : '');
}
function _restoreActiveTranscriptions(){
  for(const jobId of Object.keys(_activeTranscriptions)){
    _renderTrJob(jobId);
  }
}
function _trNotify(fname){
  toast('תמלול הושלם: '+fname+' ✓');
  if('Notification' in window && Notification.permission==='granted'){
    new Notification('LIAS — תמלול הושלם', {body:fname, icon:'🎙'});
  } else if('Notification' in window && Notification.permission!=='denied'){
    Notification.requestPermission();
  }
}
async function loadTranscriptions(){
  const el=$('tr-list'); if(!el) return;
  try{
    const r = await (await fetch('/api/transcriptions')).json();
    const items = r.items||[];
    const done = items.filter(t=>t.status!=='partial');
    const partial = items.filter(t=>t.status==='partial');
    const badge = t => t.status==='partial'
      ? '<span class="pill pend" style="font-size:10px">⏸ נעצר באמצע</span>'
      : '<span class="pill ok" style="font-size:10px">✓ הושלם</span>';
    const item = t=>`
      <div class="tr-item" style="flex-direction:column;align-items:stretch">
        <div style="display:flex;align-items:center;gap:10px;cursor:pointer" onclick="viewTranscription('${t.name}')">
          <div class="ic">${t.status==='partial'?'⏸':'📝'}</div>
          <div class="info" style="flex:1"><b>${(t.stem||t.name)}</b> ${badge(t)}
            <span>${t.size_kb} KB · ${t.modified?.replace('T',' ')}</span></div>
          <div style="display:flex;gap:6px" onclick="event.stopPropagation()">
            ${t.status==='partial'&&t.has_audio?`<button class="fv-btn" style="font-size:11px" onclick="resumeTr('${t.stem}')" title="ממשיך את התמלול מההקלטה השמורה">▶ המשך תמלול</button>`:''}
            ${t.has_audio?`<button class="fv-btn" style="font-size:11px;color:var(--danger)" onclick="delTr('${t.audio_name}','ההקלטה')" title="מחק את קובץ ההקלטה (התמלול נשאר)">🗑 הקלטה</button>`:''}
            <button class="fv-btn" style="font-size:11px;color:var(--danger)" onclick="delTr('${t.name}','התמלול')" title="מחק את קובץ התמלול">🗑 תמלול</button>
          </div>
        </div>
        ${t.has_audio?`<audio controls preload="none" src="/api/transcription_audio/${encodeURIComponent(t.audio_name)}" style="width:100%;height:32px;margin-top:6px"></audio>`:''}
      </div>`;
    el.innerHTML =
      (done.length? '<div class="sub" style="font-weight:700;margin:4px 0">תמלולים שהושלמו</div>'+done.map(item).join('') : '')
      + (partial.length? '<div class="sub" style="font-weight:700;margin:12px 0 4px">נעצרו באמצע (ניתן לצפות בחלק שתומלל)</div>'+partial.map(item).join('') : '')
      || '<div class="empty">אין תמלולים עדיין — העלה הקלטה</div>';
  }catch(e){ el.innerHTML='<div class="empty">שגיאה בטעינה</div>'; }
}
async function delTr(name, what){
  if(!confirm(`למחוק את ${what} "${name}"? (עובר ל-.trash — לא נמחק לצמיתות)`)) return;
  const r = await (await fetch('/api/transcription_delete',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})})).json();
  if(r.ok){ toast(what+' נמחק ✓'); loadTranscriptions(); }
  else toast('שגיאה: '+(r.error||''), true);
}
async function resumeTr(stem){
  const r = await (await fetch('/api/transcription_resume',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({stem})})).json();
  if(r.ok){ toast('ממשיך את התמלול ▶'); if(typeof pollTranscription==='function') pollTranscription(r.id, stem); }
  else toast('שגיאה: '+(r.error||''), true);
}
async function viewTranscription(name){
  try{
    const r = await fetch('/api/transcription/'+encodeURIComponent(name));
    const text = await r.text();
    const w = $('fv');
    $('fv-title').textContent = name;
    $('fv-frame').style.display='none';
    $('fv-doc').style.display='block';
    $('fv-doc').innerHTML = '<div class="page" style="direction:rtl;white-space:pre-wrap;font-family:Heebo,sans-serif;font-size:14px;line-height:2">'
      + text.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>';
    w.classList.add('on'); _syncOverlays();
  }catch(e){ toast('שגיאה בפתיחת תמלול', true); }
}
