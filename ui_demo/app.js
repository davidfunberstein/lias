const $ = id => document.getElementById(id);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const nf = new Intl.NumberFormat('he-IL');
let D = null, C = null, K = null, charts = {}, route = {v:'home'};

/* ─── auth (demo, local only) ─── */
function user(){ try{return JSON.parse(localStorage.getItem('lias_user'))}catch(e){return null} }
function doLogin(e){
  e.preventDefault();
  const role = $('l-role').value;
  const u = {role, name:$('l-name').value.trim(), email:$('l-email').value.trim(),
             username:$('l-user').value.trim(), remember:$('l-rem').checked,
             bdr_entity_type: role==='LAWYER'?'lawyer':role==='PLEADER'?'pleader':'private'};
  (u.remember?localStorage:sessionStorage).setItem('lias_user', JSON.stringify(u));
  $('login').classList.add('hide');
  boot();
}
function logout(){ localStorage.removeItem('lias_user'); sessionStorage.removeItem('lias_user'); location.reload(); }
function curUser(){ return user() || (()=>{try{return JSON.parse(sessionStorage.getItem('lias_user'))}catch(e){return null}})(); }
function isPro(u){ return u && u.role!=='CLIENT'; }

/* ─── helpers ─── */
const JOB_LABELS = {net_sync_current:'סנכרון תיק NET', net_auto_update:'עדכון כל תיקי NET',
  bdr_batch:'הורדת אצווה BDR', open_portal:'פתיחת פורטל', reimport_csv:'התאמת DB',
  convert_md:'המרת מסמך לטקסט', purge_stale:'ניקוי רשומות', net_date_search:'חיפוש תאריכים',
  net_list_cases:'חיפוש תיקים בנט', net_smart_download:'הורדת תיקים מנט', net_download_all:'הורדת כל התיקים',
  eca_sync:'סנכרון הוצאה לפועל'};
const JOB_ICONS = {net_sync_current:'🔄', net_auto_update:'🔄', bdr_batch:'📥', eca_sync:'⚖️',
  open_portal:'🌐', reimport_csv:'🗂', convert_md:'📝', purge_stale:'🧹', net_date_search:'🔎'};
const GROUP_COLORS = {'בקשה':'#2F7DF6','תגובה':'#7EB1FA','החלטה':'#0E1B29',
  'פסק דין':'#F5A623','פרוטוקול':'#3B82F6','אישור':'#C9D6CE','אחר':'#6B7570'};

function pill(state){
  const m = {COMPLETED:['ok','הושלם'], ERROR:['err','שגיאה'], PENDING:['pend','ממתין'],
             RUNNING:['run','רץ עכשיו'], IN_PROGRESS:['run','בתהליך'], CANCELLED:['pend','בוטל']};
  const [c,t] = m[state] || ['gray', state||'—'];
  return `<span class="pill ${c}">${t}</span>`;
}
const arkaaTag = (a,portal)=>`<span class="tag ${portal==='BDR'?'bdr':''}">${a}</span>`;
function miniBars(el, values, hlLast=true){
  el.innerHTML=''; const max=Math.max(...values,1);
  values.forEach((v,i)=>{const b=document.createElement('i');
    b.style.height=Math.max(12,v/max*100)+'%';
    if(hlLast? i===values.length-1 : v===max) b.className='on';
    el.appendChild(b);});
}
function strip(groups, other, total){
  let html='';
  for(const [g,n] of Object.entries(groups)) if(n)
    html+=`<i title="${g}: ${n}" style="width:${n/total*100}%;background:${GROUP_COLORS[g]}"></i>`;
  if(other) html+=`<i title="אחר: ${other}" style="width:${other/total*100}%;background:${GROUP_COLORS['אחר']}"></i>`;
  return html;
}
function destroyCharts(){ Object.values(charts).forEach(c=>c?.destroy()); charts={}; }

/* ─── routing ─── */
function go(v, id){
  route = {v, id};
  location.hash = v==='home' ? '' : id!=null ? `${v}/${id}` : v;
  render();
  refresh(true);
}
window.addEventListener('hashchange', ()=>{
  const [v,id] = location.hash.replace('#','').split('/');
  route = v ? {v, id:+id} : {v:'home'};
  render(); refresh(true);
});

/* ─── drawer ─── */
/* Collapsible-state memory: groups start CLOSED; whatever the user opens
   stays open on the next render/visit (localStorage). */
function _openState(){ try{return JSON.parse(localStorage.getItem('lias_open_groups'))||{}}catch(e){return {}} }
function _groupToggled(key, el){
  const m=_openState(); m[key]=el.open; localStorage.setItem('lias_open_groups', JSON.stringify(m));
}
function _isOpen(key){ return _openState()[key]===true; }
function _det(key){ return `${_isOpen(key)?'open ':''}ontoggle="_groupToggled('${key.replace(/'/g,"\\'")}',this)"`; }
function openDrawer(kind, filter){
  const b = $('drawer-body'); let html='';
  if(!D){ return; }
  if(kind==='clients'){
    html = `<h2>לקוחות</h2><div class="sub">${D.clients.length} לקוחות במערכת</div>`+
      D.clients.map(c=>`<div class="dl-item" onclick="closeDrawer();go('client',${c.client_id})">
        <b>${c.display_name}</b><span>${c.cases} תיקים · ${nf.format(c.docs)} מסמכים</span></div>`).join('');
  } else if(kind==='error_cases'){
    const errCards = (D.case_cards||[]).filter(c=>c.errors>0);
    html = `<h2>תיקים עם שגיאות הורדה</h2><div class="sub">${errCards.length} תיקים — לחץ כדי להיכנס לתיק</div>`+
      (errCards.length? errCards.map(c=>`<div class="dl-item" onclick="closeDrawer();go('case',${c.sub_case_id})">
        <b>${c.sub_number||c.sub_case_id}</b><span style="color:var(--danger)">${c.errors} מסמכים נכשלו · ${c.docs} סה״כ</span></div>`).join('')
      : '<div class="sub">אין תיקים עם שגיאות 🎉</div>');
  } else if(kind==='cases'){
    const allCards = filter? D.case_cards.filter(c=>c.arkaa===filter) : D.case_cards;
    const st = caseStatusMap();
    const chip = c=> st[c.sub_case_id]==='closed'
      ? '<span class="pill gray" style="margin-right:6px">סגור</span>'
      : '<span class="pill ok" style="margin-right:6px">פתוח</span>';
    const parties = c => {
      const n = c.sub_number||'';
      const m = n.match(/נ['’]\s*(.+)/);
      return m ? m[1].trim() : '';
    };
    const item = c=>{
      const vs = parties(c);
      return `<div class="dl-item" onclick="closeDrawer();go('case',${c.sub_case_id})">
        <b>${c.sub_number}</b>${chip(c)}
        ${vs?`<span style="font-size:11px;color:var(--ink-soft)">מול: ${vs}</span>`:''}
        <span>${c.arkaa} · ${nf.format(c.docs)} מסמכים · ${c.groups['בקשה']||0} בקשות · ${(c.groups['החלטה']||0)+(c.groups['פסק דין']||0)} החלטות</span>
      </div>`;
    };
    const srt = arr => [...arr].sort((a,b)=>
      ((st[a.sub_case_id]==='closed')-(st[b.sub_case_id]==='closed')) ||
      (b.last||'').localeCompare(a.last||''));
    const byClient = {};
    for(const c of allCards){
      const cname = (D.clients||[]).find(cl=>cl.client_id===c.client_id)?.display_name || 'ללא לקוח';
      (byClient[cname] = byClient[cname]||[]).push(c);
    }
    const courtLabel = c => {
      if(c.arkaa!=='בית דין רבני') return '🏛 נט המשפט';
      return /[-\s]גדול\s*$/.test(c.sub_number||'') ? '⚖ ביה״ד הרבני הגדול' : '⚖ ביה״ד רבני אזורי';
    };
    let body = '';
    for(const [client, cases] of Object.entries(byClient)){
      const withDocs = cases.filter(c=>c.docs>0);
      const noDocs = cases.filter(c=>!c.docs);
      const byCourt = {};
      for(const c of srt(withDocs)){
        const cl = courtLabel(c);
        (byCourt[cl] = byCourt[cl]||[]).push(c);
      }
      body += `<details ${_det('cl:'+client)} style="margin-top:14px;border:1px solid var(--line);border-radius:10px;padding:8px 12px">
        <summary style="font-weight:800;cursor:pointer;padding:6px 0;font-size:15px">👤 ${client} · ${cases.length} תיקים</summary>`;
      for(const [court, clist] of Object.entries(byCourt)){
        body += `<details ${_det('cl:'+client+':'+court)} style="margin:6px 0 4px 12px">
          <summary style="font-weight:600;cursor:pointer;padding:4px 0;font-size:13px">${court} · ${clist.length}</summary>
          ${clist.map(item).join('')}</details>`;
      }
      if(noDocs.length){
        body += `<details style="margin:4px 0 4px 12px;opacity:.6">
          <summary style="cursor:pointer;padding:4px 0;font-size:12px;color:var(--ink-soft)">טרם הורדו מסמכים · ${noDocs.length}</summary>
          ${noDocs.map(c=>`<div class="dl-item" style="opacity:.6" onclick="closeDrawer();go('case',${c.sub_case_id})">
            <b>${c.sub_number}</b><span>${c.arkaa}</span></div>`).join('')}</details>`;
      }
      body += '</details>';
    }
    html = `<h2>תיקים${filter? ' — '+filter : ''}</h2>
      <div class="sub">${allCards.filter(c=>c.docs>0).length} תיקים עם מסמכים · לפי לקוח וערכאה</div>`
      + (body || '<div class="empty">אין תיקים</div>');
  }
  b.innerHTML = html;
  $('drawer').classList.add('on');
  $('drawer-bg').classList.add('on');
}
function closeDrawer(){
  $('drawer').classList.remove('on');
  $('drawer-bg').classList.remove('on');
}
function drawerOpen(){ return $('drawer').classList.contains('on'); }

/* ─── toast ─── */
function toast(msg, err){
  let t=$('toast'); if(!t){t=document.createElement('div');t.id='toast';document.body.appendChild(t);}
  t.textContent=msg; t.className='show'+(err?' err':'');
  clearTimeout(t._h); t._h=setTimeout(()=>t.className='',4000);
}

/* ═══════════════ RENDER ═══════════════ */
function render(){
  destroyCharts();
  const u = curUser();
  if(!u) return;
  if(route.v==='case') renderCase();
  else if(route.v==='sync') renderSync();
  else if(route.v==='transcribe') renderTranscribe();
  else if(route.v==='client' || !isPro(u)) renderClient();
  else renderLawyer();
  document.querySelectorAll('.nav button').forEach(b=>{
    b.classList.toggle('active', b.dataset.nav===route.v ||
      (!['sync','transcribe'].includes(route.v) && b.dataset.nav==='home'));
  });
}

/* ─── shared chart widgets ─── */
function monthlyChart(id, data, ctx){
  if(!data?.length || !window.Chart) return;
  const accent=css('--accent'), ink=css('--ink-soft'), line=css('--line');
  Chart.defaults.font.family="'Heebo',sans-serif";
  const vals=data.map(m=>m.count), maxIdx=vals.lastIndexOf(Math.max(...vals));
  charts[id]=new Chart($(id),{type:'bar',
    data:{labels:data.map(m=>m.label),datasets:[{data:vals,borderRadius:8,borderSkipped:false,
      maxBarThickness:34,backgroundColor:vals.map((_,i)=>i===maxIdx?accent:'#E9EDEB'),
      hoverBackgroundColor:accent}]},
    options:{maintainAspectRatio:false,
      onClick:(e,els)=>{
        if(!els?.length) return;
        const m = data[els[0].index];
        if(m?.ym) openDocList(`מסמכים — ${m.label}`, {month:m.ym, ...(ctx||{})});
      },
      onHover:(e,els)=>{ e.native.target.style.cursor = els?.length?'pointer':'default'; },
      plugins:{legend:{display:false},tooltip:{rtl:true,callbacks:{label:c=>` ${c.parsed.y} מסמכים — לחץ לרשימה`}}},
      scales:{x:{reverse:true,grid:{display:false},ticks:{color:ink,font:{size:11}}},
        y:{position:'right',border:{display:false},grid:{color:line},
           ticks:{color:ink,font:{size:11},precision:0}}}}});
}
function donut(id, legendId, items, ctx){
  if(!items?.length || !window.Chart) return;
  const palette=[css('--accent'),'#0E1B29','#7EB1FA','#C9D6CE','#F5A623','#6B7570','#DDE6E0'];
  const open = i => { const t=items[i];
    if(t) openDocList(`מסמכים — ${t.label}`, {submitter:t.label, ...(ctx||{})}); };
  charts[id]=new Chart($(id),{type:'doughnut',
    data:{labels:items.map(t=>t.label),
      datasets:[{data:items.map(t=>t.count),backgroundColor:palette,
        borderWidth:3,borderColor:'#fff',cutout:'68%'}]},
    options:{maintainAspectRatio:false,
      onClick:(e,els)=>{ if(els?.length && ctx) open(els[0].index); },
      onHover:(e,els)=>{ e.native.target.style.cursor = (els?.length&&ctx)?'pointer':'default'; },
      plugins:{legend:{display:false},tooltip:{rtl:true}}}});
  $(legendId).innerHTML = items.map((t,i)=>`
    <div class="li" ${ctx?`style="cursor:pointer" data-i="${i}"`:''}><span class="dot" style="background:${palette[i%palette.length]}"></span>
    <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.label}</span><b>${nf.format(t.count)}</b></div>`).join('');
  if(ctx) $(legendId).querySelectorAll('.li').forEach(el=>
    el.onclick = ()=> open(+el.dataset.i));
}

/* load map: requests/decisions per day per case */
const MAP_PERIODS = [['all','הכול'],['12m','שנה אחרונה'],['3m','3 חודשים'],['1m','חודש אחרון']];
let mapData = {};
let mapPrefs = (()=>{try{return JSON.parse(localStorage.getItem('lias_map_prefs'))||{}}catch(e){return {}}})();
function _saveMapPrefs(){ try{localStorage.setItem('lias_map_prefs', JSON.stringify(mapPrefs));}catch(e){} }
function setMapPeriod(id, period){
  (mapPrefs[id] = mapPrefs[id]||{}).period = period; _saveMapPrefs();
  charts[id]?.destroy(); loadMap(id, mapData[id]);
}
function toggleMapCase(id, label){
  const p = (mapPrefs[id] = mapPrefs[id]||{});
  const set = new Set(p.cases||[]);
  set.has(label) ? set.delete(label) : set.add(label);
  p.cases = [...set]; _saveMapPrefs();
  charts[id]?.destroy(); loadMap(id, mapData[id]);
}
function clearMapCases(id){
  (mapPrefs[id] = mapPrefs[id]||{}).cases = []; _saveMapPrefs();
  charts[id]?.destroy(); loadMap(id, mapData[id]);
}
function _mapControls(id, act, pref){
  const ctl = $(id+'-ctl'); if(!ctl) return;
  const sel = new Set(pref.cases||[]);
  ctl.innerHTML =
    `<select onchange="setMapPeriod('${id}',this.value)">`+
      MAP_PERIODS.map(([v,l])=>`<option value="${v}" ${pref.period===v||(!pref.period&&v==='all')?'selected':''}>${l}</option>`).join('')+
    `</select>`+
    act.cases.map((c,i)=>`<button class="map-chip ${sel.has(c)?'on':''}"
        onclick="toggleMapCase('${id}','${c.replace(/'/g,"\\'")}')" title="הצג/הסתר תיק">${c}</button>`).join('')+
    (sel.size?`<button class="map-chip clear" onclick="clearMapCases('${id}')">✕ נקה סינון</button>`:'');
}
function loadMap(id, act){
  if(!act?.points?.length || !window.Chart) return;
  mapData[id] = act;
  const pref = mapPrefs[id]||{};
  _mapControls(id, act, pref);
  const days = {'1m':31,'3m':92,'12m':366}[pref.period];
  const minTs = days ? Date.now()-days*86400e3 : 0;
  const selSet = new Set(pref.cases||[]);
  const keep = act.cases.map(c=>!selSet.size || selSet.has(c));
  const idxMap = {}; const fCases=[], fIds=[];
  act.cases.forEach((c,i)=>{ if(keep[i]){ idxMap[i]=fCases.length; fCases.push(c); fIds.push(act.ids?.[i]); }});
  const fPoints = act.points
    .filter(p=>keep[p.ci] && (+new Date(p.x))>=minTs)
    .map(p=>({...p, ci:idxMap[p.ci]}));
  const box = $(id+'-box');
  if(box){
    box.style.height = Math.max(150, fCases.length*46+70)+'px';
    box.innerHTML = fPoints.length
      ? `<canvas id="${id}"></canvas>`
      : `<div class="empty" style="height:100%;display:grid;place-items:center">אין בקשות/החלטות בתקופה שנבחרה</div>`;
  }
  if(!fPoints.length) return;
  act = {...act, cases:fCases, ids:fIds, points:fPoints};
  const day = 24*3600*1000;
  const mk = g => act.points.filter(p=>p.g===g).map(p=>({
    x:+new Date(p.x), y:p.ci + (g==='החלטה'?0.18:-0.18), r:Math.min(4+p.n*2.2,14), n:p.n, d:p.x, g}));
  charts[id] = new Chart($(id), {type:'bubble',
    data:{datasets:[
      {label:'בקשות', data:mk('בקשה'), backgroundColor:'rgba(111,224,111,.75)', borderColor:css('--accent-strong'), borderWidth:1},
      {label:'החלטות', data:mk('החלטה'), backgroundColor:'rgba(16,22,19,.78)', borderColor:'#000', borderWidth:1}]},
    options:{maintainAspectRatio:false,
      onClick:(e,els)=>{
        if(!els?.length) return;
        const el=els[0], p=charts[id].data.datasets[el.datasetIndex].data[el.index];
        const ci=Math.round(p.y), rowId=act.ids?.[ci];
        const params={date:p.d, group:p.g};
        if(rowId!=null) params[act.by==='client'?'client_id':'sub_case_id']=rowId;
        openDocList(`${p.g==='בקשה'?'בקשות':'החלטות'} — ${act.cases[ci]||''} · ${p.d.split('-').reverse().join('/')}`, params);
      },
      onHover:(e,els)=>{ e.native.target.style.cursor = els?.length?'pointer':'default'; },
      plugins:{legend:{position:'bottom', rtl:true, labels:{font:{size:11}, boxWidth:12}},
        tooltip:{rtl:true, callbacks:{label:c=>{
          const p=c.raw; return ` ${p.d.split('-').reverse().join('/')} · ${act.cases[Math.round(p.y)]} · ${p.n} ${p.g==='בקשה'?'בקשות':'החלטות'} — לחץ לרשימה`;}}}},
      scales:{
        x:{type:'linear', reverse:true, grid:{color:css('--line')},
           ticks:{color:css('--ink-soft'), font:{size:10}, maxTicksLimit:9,
             callback:v=>{const t=new Date(v); return (t.getMonth()+1)+'/'+String(t.getFullYear()).slice(2);}}},
        y:{min:-0.7, max:act.cases.length-0.3, grid:{color:css('--line')},
           ticks:{color:css('--ink-soft'), font:{size:10}, stepSize:1,
             callback:v=>Number.isInteger(v)? (act.cases[v]||'') : ''}}}}});
}

function aiAsk(q){
  alert('עוזר ה-AI ירוץ דרך המערכת המלאה (אפיון §4, שלב 5).');
}

/* ─── data ─── */
let lastSig='';
function userBusy(){
  const a=document.activeElement;
  return drawerOpen() || $('settings').style.display==='block' ||
    (a && (a.tagName==='INPUT'||a.tagName==='SELECT'||a.tagName==='TEXTAREA') && a.id!=='');
}
async function refresh(force){
  try{
    if(route.v==='case' && route.id){
      const p=new URLSearchParams(caseFilters);
      K = await (await fetch(`/api/case/${route.id}?`+p)).json();
    } else if(route.v==='client' || !isPro(curUser())){
      const id = route.id || D?.clients?.[0]?.client_id || 1;
      C = await (await fetch(`/api/client/${id}`)).json();
    }
    if(!D || route.v==='home' || route.v==='sync'){
      D = await (await fetch('/api/dashboard')).json();
    }
    if(!isPro(curUser()) && !route.id && D?.clients?.length && !C){
      C = await (await fetch(`/api/client/${D.clients[0].client_id}`)).json();
    }
    const sig = JSON.stringify([{...D, generated_at:''}, C, K, route]);
    if(force || (sig!==lastSig && !userBusy())){ lastSig=sig; render(); }
    refreshFab();
    $('foot').textContent = `LIAS · עודכן ${D.generated_at.replace('T',' ')} · קריאה בלבד מ-lias.db · מנוע הסנכרון ${D.live?'פעיל ✓':'כבוי'} · מתרענן אוטומטית`;
  }catch(e){ $('foot').textContent='שגיאה בטעינת נתונים: '+e.message; }
}

/* ─── safe shutdown ─── */
async function safeShutdown(){
  if(!confirm('לסגור את המערכת? המנוע ייעצר וכל חלונות הדפדפן של ההורדות ייסגרו.')) return;
  try{ await fetch('/api/system/shutdown',{method:'POST'}); }catch(e){}
  document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;font-size:20px;direction:rtl">'
    + '✅ המערכת נסגרה בבטחה. אפשר לסגור את הטאב.</div>';
  setTimeout(()=>{ try{ window.close(); }catch(e){} }, 800);
}

/* heartbeat */
window.addEventListener('beforeunload', e=>{
  e.stopImmediatePropagation();
  e.preventDefault = ()=>{};
  delete e.returnValue;
  Object.defineProperty(e, 'returnValue', { get(){return undefined;}, set(){}, configurable:true });
}, true);
try{
  Object.defineProperty(window, 'onbeforeunload',
    { get(){ return null; }, set(){}, configurable:false });
  const origAEL = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function(type, fn, opts){
    if(type==='beforeunload' && this===window) return;
    return origAEL.call(this, type, fn, opts);
  };
}catch(_){}
setInterval(()=>{ fetch('/api/heartbeat',{method:'POST'}).catch(()=>{}); }, 5000);
fetch('/api/heartbeat',{method:'POST'}).catch(()=>{});

function boot(){
  const u = curUser();
  $('av-name').textContent = u.name;
  $('av-letter').textContent = (u.name||'?')[0];
  $('av-role').textContent = u.role==='LAWYER'?'עורך דין':u.role==='PLEADER'?'טוען רבני':'לקוח פרטי';
  const [v,id] = location.hash.replace('#','').split('/');
  route = v? {v, id:+id} : {v:'home'};
  render();
  refresh(true);
  setInterval(()=>refresh(false), 10000);
  ensureFab();
  connectEngineSSE();
  if(isPro(u) && !sessionStorage.getItem('govil_checked')){
    sessionStorage.setItem('govil_checked','1');
    fetch('/api/govil/status').then(r=>r.json()).then(s=>{
      if(s.ok && !s.configured){
        openSettings();
        toast('הגדרה חד-פעמית: חיבור ל-gov.il');
      }
    }).catch(()=>{});
  }
}
