/* ─── engine control / הפעלת מנוע הסנכרון מהאתר ─── */
let _netDownloadJobId=null, _pendingNetCases=[], _dlStats=null;
const SCOPE_LABELS = {all:'כל התיקים', selected:'תיקים מסוימים', related:'תיקים + קשורים'};

let engineStarting=false;
async function startEngine(){
  if(engineStarting) return;
  engineStarting=true; toast('מפעיל את מנוע הסנכרון…');
  try{ await fetch('/api/system/start',{method:'POST'}); }catch(e){}
  for(let i=0;i<40;i++){
    await new Promise(r=>setTimeout(r,2000));
    try{
      const h = await (await fetch('/api/health')).json();
      if(h.full_ui_alive){ engineStarting=false; toast('מנוע הסנכרון פעיל ✓'); refresh(true); return; }
    }catch(e){}
  }
  engineStarting=false; toast('המנוע לא עלה — בדוק את lias_engine.log', true);
}
async function act(path, label){
  if(!D?.live){
    toast('מתחיל רקע…');
    await startEngine();
  }
  toast((label||'פעולה')+' — נשלח…');
  logEvent('→ '+(label||path));
  try{
    const r = await fetch('/api/proxy/actions/'+path, {method:'POST'});
    if(r.ok){
      toast((label||'פעולה')+' הופעל ✓');
      showJobBar(label||'פעולה', 0, 'ממתין לתחילת עבודה…');
    } else toast('שגיאה בהפעלה', true);
  }catch(e){ toast('שגיאה: '+e.message, true); }
}

async function restartEngine(){
  if(!confirm('לאתחל את המנוע? הדשבורד נשאר — רק המנוע מתחלף (כ-10 שניות).')) return;
  toast('מאתחל את המנוע…');
  try{ await fetch('/api/system/restart-engine',{method:'POST'}); }catch(e){}
  for(let i=0;i<30;i++){
    await new Promise(r=>setTimeout(r,2000));
    try{
      const h = await (await fetch('/api/health')).json();
      if(h.full_ui_alive){ toast('המנוע חזר ✓'); refresh(true); connectEngineSSE(); return; }
    }catch(e){}
  }
  toast('המנוע לא עלה — בדוק את היומן', true);
}

/* ─── live progress bar ─── */
let _jobBarHide=null;
function showJobBar(title, frac, msg){
  let b=$('jobbar');
  if(!b){
    b=document.createElement('div'); b.id='jobbar';
    b.style.cssText='position:fixed;bottom:16px;right:50%;transform:translateX(50%);z-index:125;'
      +'background:var(--surface,#fff);border:1px solid var(--line,#e5e5e5);border-radius:14px;'
      +'padding:10px 16px;width:min(440px,92vw);box-shadow:0 12px 40px rgba(0,0,0,.3);direction:rtl';
    b.innerHTML='<div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:6px">'
      +'<b id="jb-title"></b><span id="jb-pct"></span></div>'
      +'<div style="height:8px;background:var(--line,#eee);border-radius:6px;overflow:hidden">'
      +'<i id="jb-fill" style="display:block;height:100%;width:0;background:var(--accent,#2F7DF6);transition:width .4s"></i></div>'
      +'<div id="jb-msg" style="font-size:11.5px;color:var(--ink-soft,#777);margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>';
    document.body.appendChild(b);
  }
  clearTimeout(_jobBarHide);
  $('jb-title').textContent = title;
  $('jb-pct').textContent = Math.round((frac||0)*100)+'%';
  $('jb-fill').style.width = ((frac||0)*100)+'%';
  if(msg) $('jb-msg').textContent = msg;
}
function finishJobBar(ok, msg){
  if(!$('jobbar')) return;
  $('jb-fill').style.width='100%';
  $('jb-fill').style.background = ok? 'var(--accent,#2F7DF6)':'var(--danger,#c62828)';
  $('jb-pct').textContent = ok? '✓':'✗';
  if(msg) $('jb-msg').textContent = msg;
  clearTimeout(_jobBarHide);
  _jobBarHide = setTimeout(()=>$('jobbar')?.remove(), 6000);
}

/* ─── NET case locator ─── */
function openNetCase(sync){
  const num = ($('nc-num')?.value||'').trim();
  const my = ($('nc-my')?.value||'').trim();
  if(!num){ toast('נא להזין מספר תיק', true); return; }
  if(!my){ toast('נא לבחור חודש ושנה', true); return; }
  const mmyy = my.slice(5,7) + my.slice(2,4);
  act(`net_open_case?case_number=${encodeURIComponent(num)}&month_year=${mmyy}&sync=${sync?1:0}`,
      sync? `איתור וסנכרון תיק ${num}` : `איתור תיק ${num}`);
}

/* ─── live engine events (SSE) ─── */
let _es=null, _esTimer=null, _logBuf=[];
function logEvent(txt){
  _logBuf.unshift(new Date().toLocaleTimeString('he-IL')+'  '+txt);
  if(_logBuf.length>200) _logBuf.pop();
  const el=$('logwin-body');
  if(el) el.innerHTML = _logBuf.map(l=>`<div>${l}</div>`).join('');
}
function connectEngineSSE(){
  if(_es) return;
  try{ _es = new EventSource('/api/events'); }catch(e){ return; }
  _es.onopen = ()=> logEvent('✓ חיבור חי למנוע');
  _es.onmessage = ev=>{
    let e={}; try{ e=JSON.parse(ev.data); }catch(_){ return; }
    if(e.type==='otp_required'){ showOtp(); logEvent('🔐 נדרש קוד אימות'); }
    if(e.type==='auth_progress'){
      logEvent('🔑 '+(e.message||''));
      showJobBar('התחברות', 0.5, e.message||'');
      if(/מחובר|ממלא|מזין|קוד|✓|הצליח|נכנס|פורטל/.test(e.message||'')) $('otp-box')?.remove();
    }
    if(e.type==='drive'){ logEvent('☁ '+(e.message||'')); }
    if(e.type==='drive_error'){ logEvent('⚠ '+(e.message||'')); toast(e.message||'שגיאת Drive', true); }
    if(e.type==='net_cases'){ showNetCases(e.cases||[]); }
    if(e.type==='download_stats'){ _dlStats=e; _renderDlStats(); }
    if(e.type==='job'){
      if(e.message) logEvent(`#${e.job_id||''} ${e.message}`);
      if(typeof e.progress==='number')
        showJobBar(JOB_LABELS[e.kind]||`משימה #${e.job_id||''}`, e.progress, e.message||'');
      if(e.state==='COMPLETED'||e.state==='ERROR'){
        $('otp-box')?.remove();
        if(e.kind==='net_smart_download'||e.kind==='net_download_all'){
          _dlStats=null; _netDownloadJobId=null;
          const p=$('dl-stats-panel'); if(p) p.style.display='none';
        }
        logEvent(`${JOB_LABELS[e.kind]||e.kind||'משימה'} — ${e.state}`);
        finishJobBar(e.state==='COMPLETED', e.error||'');
        toast(`${JOB_LABELS[e.kind]||e.kind||'משימה'} — ${e.state==='COMPLETED'?'הושלמה ✓':'שגיאה'}`, e.state==='ERROR');
        refresh(true);
      }
    }
    if(e.type==='file'){
      if(e.name) logEvent(`📄 ${e.name} → ${e.status||''}`);
      if(e.status==='SYNC_DONE'||e.status==='UPLOADED'||e.status==='TRASHED') refresh(true);
    }
  };
  _es.onerror = ()=>{
    _es?.close(); _es=null;
    clearTimeout(_esTimer);
    _esTimer = setTimeout(connectEngineSSE, 15000);
  };
}

/* ─── log window ─── */
let _logTimer=null, _logTab='engine';
function toggleLogWin(){
  let w=$('logwin');
  if(w){ clearInterval(_logTimer); _logTimer=null; w.remove(); return; }
  w=document.createElement('div'); w.id='logwin';
  w.style.cssText='position:fixed;bottom:16px;left:16px;width:min(560px,94vw);height:380px;'
    +'background:#0E1B29;color:#c8e6c9;border:1px solid #2a352c;border-radius:14px;'
    +'z-index:118;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.35);'
    +'overflow:hidden;resize:both;min-width:340px;min-height:200px';
  w.innerHTML=`<div class="fv-top" id="logwin-top" style="border-color:#2a352c">
      <button class="fv-btn" style="border-color:#2a352c;color:#c8e6c9" onclick="toggleLogWin()">✕</button>
      <div class="fv-title" style="color:#e8f5e9">יומן חי — המנוע</div>
      <button class="fv-btn" id="lg-t-engine" style="border-color:#2a352c;color:#c8e6c9" onclick="setLogTab('engine')">לוג מלא</button>
      <button class="fv-btn" id="lg-t-events" style="border-color:#2a352c;color:#c8e6c9" onclick="setLogTab('events')">אירועים</button>
    </div>
    <div id="logwin-body" style="flex:1;overflow-y:auto;padding:8px 12px;font-size:11.5px;
      font-family:ui-monospace,monospace;direction:ltr;text-align:left;line-height:1.65;white-space:pre-wrap"></div>`;
  document.body.appendChild(w);
  _makeDraggable('logwin','logwin-top');
  setLogTab('engine');
  _logTimer = setInterval(_refreshLog, 3000);
}
function setLogTab(t){ _logTab=t; _refreshLog(); }
async function _refreshLog(){
  const el=$('logwin-body'); if(!el) return;
  const atBottom = el.scrollTop+el.clientHeight >= el.scrollHeight-40;
  if(_logTab==='events'){
    el.style.direction='rtl'; el.style.textAlign='right';
    el.innerHTML = _logBuf.map(l=>`<div>${l}</div>`).join('') || '<div style="color:#7a8a7d">אין אירועים עדיין</div>';
  } else {
    el.style.direction='ltr'; el.style.textAlign='left';
    try{
      const r = await (await fetch('/api/log?lines=300')).json();
      el.textContent = (r.lines||[]).join('\n') || 'הלוג ריק';
    }catch(e){ el.innerHTML = '<div style="color:#7a8a7d">המנוע כבוי — אין לוג. הפעל את המנוע.</div>'; }
  }
  if(atBottom) el.scrollTop = el.scrollHeight;
}

/* ─── real automation browser — show/hide ─── */
let _realBrowserVisible=false;
function runBdrBatch(){
  act('bdr_batch', 'הורדת תיקי BDR');
}
async function toggleRealBrowser(){
  const path = _realBrowserVisible ? 'browser/hide' : 'browser/show';
  try{
    const r = await fetch('/api/proxy/actions/'+path, {method:'POST'});
    if(!r.ok) throw 0;
    _realBrowserVisible = !_realBrowserVisible;
    const btn = $('browser-toggle');
    if(btn) btn.textContent = _realBrowserVisible ? '🙈 הסתר דפדפן' : '🖥 הצג דפדפן';
    toast(_realBrowserVisible ? 'הדפדפן נפתח — ההורדות ממשיכות גם כשהוא מוסתר'
                              : 'הדפדפן הוסתר — ההורדות ממשיכות ברקע');
  }catch(e){ toast('פעולת דפדפן נכשלה — ודא שהמנוע פעיל', true); }
}

/* legacy screenshot mirror */
let _shotTimer=null;
function toggleBrowserWin(){
  let w=$('bwin');
  if(w){ clearInterval(_shotTimer); _shotTimer=null; w.remove(); return; }
  w=document.createElement('div'); w.id='bwin';
  w.style.cssText='position:fixed;bottom:16px;right:16px;width:min(560px,94vw);'
    +'background:var(--surface,#fff);border:1px solid var(--line,#e5e5e5);border-radius:14px;'
    +'z-index:118;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.3);overflow:hidden;resize:both;min-width:320px';
  w.innerHTML=`<div class="fv-top" id="bwin-top"><button class="fv-btn" onclick="toggleBrowserWin()">✕</button>
      <div class="fv-title">🖥 מסך הדפדפן</div>
      <span id="bwin-url" class="sub" style="flex:0 1 auto;direction:ltr;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:45%"></span></div>
    <div id="bwin-state" class="sub" style="padding:4px 12px;margin:0;border-bottom:1px solid var(--line)"></div>
    <div id="bwin-body" style="min-height:200px;display:grid;place-items:center;color:var(--ink-soft);font-size:13px">מתחבר…</div>`;
  document.body.appendChild(w);
  _makeDraggable('bwin','bwin-top');
  let hasFrame=false;
  const setState = t => { const el=$('bwin-state'); if(el) el.textContent=t; };
  const tick = async ()=>{
    let st={};
    try{ st = await (await fetch('/api/browser/status')).json(); }catch(e){ st={}; }
    const url = st.url||'';
    if($('bwin-url')) $('bwin-url').textContent = url.replace(/^https?:\/\//,'').slice(0,60);
    if(!st.available){
      setState('המנוע כבוי — הפעל אותו מכרטיס הסנכרון');
      if(!hasFrame) $('bwin-body').innerHTML = '<div style="padding:30px;text-align:center">הפעל את המנוע ואז פעולה כלשהי — המסך יופיע כאן</div>';
      return;
    }
    if(st.busy){
      setState('⏳ המנוע עובד עכשיו בפורטל — התצוגה תתעדכן בין פעולות');
      return;
    }
    if(!url || url==='about:blank'){
      setState('הדפדפן במנוחה — עמוד ריק');
      if(!hasFrame) $('bwin-body').innerHTML = '<div style="padding:30px;text-align:center">ברגע שתופעל פעולה בפורטל, המסך יופיע כאן</div>';
      return;
    }
    try{
      const r = await fetch('/api/browser/screenshot?t='+Date.now());
      if(!r.ok) throw 0;
      const blob = await r.blob();
      $('bwin-body').innerHTML = `<img style="width:100%;display:block" src="${URL.createObjectURL(blob)}">`;
      hasFrame=true;
      setState('🟢 תצוגה חיה');
    }catch(e){
      setState('⏳ הדפדפן עסוק — התצוגה תתעדכן בעוד רגע');
    }
  };
  tick(); _shotTimer = setInterval(tick, 2500);
}

/* ─── sync card ─── */
let _currentScope = 'all';
function syncCard(el){
  el.innerHTML = `<h2>הורדת תיקים</h2>
    <div class="meta">בחר מאיפה להוריד ומה להוריד.
      ☁ אם Drive מוגדר, הקבצים עולים אוטומטית במקביל.</div>
    <div id="sync-scope-label" style="font-size:12px;color:rgba(255,255,255,.55);margin:10px 0 6px"></div>
    <div id="sync-case-picker" style="display:none;margin-bottom:10px"></div>
    <div id="dl-stats-panel" style="display:none"></div>
    <div style="display:flex;flex-direction:column;gap:10px;margin-top:10px" id="sync-buttons">
      <button class="btn-accent" style="padding:14px;font-size:15px" id="btn-net-dl"
        onclick="startNetDownload()">⬇ הורד מנט המשפט</button>
      <button class="btn-accent" style="padding:14px;font-size:15px" id="btn-bdr-dl"
        onclick="runBdrBatch()">⬇ הורד מבית הדין הרבני</button>
    </div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn-accent" style="font-size:12px;padding:9px;flex:1" id="browser-toggle" onclick="toggleRealBrowser()">🖥 הצג דפדפן</button>
      <button class="btn-accent" style="font-size:12px;padding:9px;flex:1" onclick="toggleLogWin()">📜 יומן חי</button>
    </div>`;
  fetch('/api/settings').then(r=>r.json()).then(st=>{
    _currentScope = st.case_scope || 'all';
    const lbl = $('sync-scope-label');
    if(lbl) lbl.textContent = 'היקף NET: ' + (SCOPE_LABELS[_currentScope]||_currentScope) + ' (ניתן לשנות בהגדרות ⚙)';
  }).catch(()=>{});
  if(_dlStats) _renderDlStats();
}
function startNetDownload(){
  const scope = _currentScope;
  if(scope==='all'){
    _startSmartDownload('all', []);
  } else {
    toast('מתחבר לנט המשפט ומחפש תיקים…');
    act('net_list_cases','חיפוש תיקים בנט');
  }
}
/* first showNetCases — scope-based picker in sync card (overridden below) */
function showNetCases(cases){
  _pendingNetCases = cases;
  const picker=$('sync-case-picker'); if(!picker) return;
  const scope = _currentScope;
  if(scope==='all' || !cases.length){
    picker.style.display='none';
    if(!cases.length) toast('לא נמצאו תיקים בפורטל', true);
    return;
  }
  picker.style.display='block';
  const multi = true;
  picker.innerHTML=`<div style="font-size:12px;color:rgba(255,255,255,.7);margin-bottom:6px">
      נמצאו ${cases.length} תיקים בנט — ${scope==='related'?'סמן תיקים (+ קשורים לכל אחד):':'סמן מה להוריד:'}</div>
    <div style="display:flex;gap:6px;margin-bottom:6px">
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllNetCases(true)">סמן הכל</button>
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllNetCases(false)">נקה הכל</button>
    </div>
    <div id="net-cases-list" style="max-height:240px;overflow-y:auto;display:flex;flex-direction:column;gap:3px">
    ${cases.map((c,i)=>`<label style="display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:8px;
      background:rgba(255,255,255,.08);cursor:pointer;font-size:12px;color:rgba(255,255,255,.9)">
      <input type="checkbox" name="net-case" value="${i}" style="accent-color:var(--accent)">
      <span style="flex:1">${c.display_id} — ${c.name||c.type||''}</span>
      <span style="color:rgba(255,255,255,.45);font-size:11px">${c.court||''}</span>
    </label>`).join('')}
    </div>
    <button class="btn-accent" style="margin-top:10px;padding:12px;font-size:14px;width:100%"
      onclick="_confirmNetCases()">⬇ הורד תיקים מסומנים</button>`;
}
function _selectAllNetCases(on){
  document.querySelectorAll('#net-cases-list input').forEach(cb=>cb.checked=on);
}
function _confirmNetCases(){
  const scope = _currentScope;
  const checks = [...document.querySelectorAll('#net-cases-list input:checked')];
  if(!checks.length){ toast('סמן תיק אחד לפחות', true); return; }
  const selected = checks.map(cb=>_pendingNetCases[+cb.value]).filter(Boolean);
  _startSmartDownload(scope==='related'?'related':'selected', selected);
}
async function _startSmartDownload(mode, cases){
  const body = JSON.stringify({mode, cases, years_back:20});
  try{
    const r = await fetch('/api/proxy/actions/net_smart_download', {
      method:'POST', headers:{'Content-Type':'application/json'}, body});
    const j = await r.json();
    if(j.job_id) _netDownloadJobId = j.job_id;
    toast('ההורדה התחילה');
    const picker=$('sync-case-picker');
    if(picker){ picker.style.display='none'; picker.innerHTML=''; }
  }catch(e){ toast('שגיאה: '+e.message, true); }
}
function cancelNetDownload(){
  if(!_netDownloadJobId) return;
  act('cancel_download?job_id='+_netDownloadJobId, 'ביטול הורדה');
  toast('שולח הוראת עצירה — ההורדה תיעצר אחרי התיק הנוכחי');
}
function _renderDlStats(){
  const s=_dlStats; if(!s) return;
  const panel=$('dl-stats-panel'); if(!panel) return;
  panel.style.display='block';
  const pct = s.total? Math.round(s.done/s.total*100) : 0;
  const elapsed = s.elapsed_sec||0;
  const mm = Math.floor(elapsed/60), ss = elapsed%60;
  panel.innerHTML=`<div style="border:1px solid rgba(255,255,255,.2);border-radius:10px;padding:10px 14px;margin-bottom:10px">
    <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px">
      <b>הורדה פעילה${s.current_case?' — '+s.current_case:''}</b>
      <span>${pct}% · ${s.done}/${s.total} תיקים</span></div>
    <div style="height:6px;background:rgba(255,255,255,.15);border-radius:4px;overflow:hidden;margin-bottom:8px">
      <div style="height:100%;width:${pct}%;background:var(--accent);transition:width .4s"></div></div>
    <div style="display:flex;gap:16px;font-size:11px;color:rgba(255,255,255,.6)">
      <span>נותרו: ${s.remaining||0}</span>
      <span>נכשלו: ${s.failed||0}</span>
      <span>קצב: ${s.speed_per_min||0}/דק׳</span>
      <span>זמן: ${mm}:${String(ss).padStart(2,'0')}</span>
    </div>
    <button class="btn-accent" style="margin-top:8px;font-size:12px;padding:6px 14px;background:rgba(198,40,40,.8)"
      onclick="cancelNetDownload()">⏹ עצור הורדה</button>
  </div>`;
}

/* ─── NET cases checkbox picker (overrides showNetCases from SSE) ─── */
/* Note: this second definition of showNetCases overrides the first one above,
   matching the original file's behavior where both declarations existed. */
function showNetCases(cases){
  $('fvl-title').textContent = 'תיקים שנמצאו בפורטל — סמן מה לסנכרן';
  $('fvl-count').textContent = `${cases.length} תיקים`;
  $('fvl').classList.add('on'); _syncOverlays();
  window._netCases = cases;
  const head = cases.length? `<label class="dl-item" style="display:flex;gap:10px;align-items:center;cursor:pointer;position:sticky;top:0;background:var(--surface,#fff);z-index:2;font-weight:800;border-bottom:1px solid var(--line)">
      <input type="checkbox" id="nc-all" style="margin:0" onchange="document.querySelectorAll('.nc-pick').forEach(c=>c.checked=this.checked);ncCount()">
      <span>בחר הכל / נקה הכל</span></label>` : '';
  $('fvl-body').innerHTML = head + (cases.map((c,i)=>`
    <label class="dl-item" style="display:flex;gap:10px;align-items:flex-start;cursor:pointer">
      <input type="checkbox" class="nc-pick" data-i="${i}" style="margin-top:3px" onchange="ncCount()">
      <span style="flex:1"><b>${c.CaseDisplayIdentifier||''} ${c.CaseName||''}</b>
      <span style="display:block">${c.CaseTypeShortName||''} · ${c.CourtName||''} · ${c.CaseStatusName||''}</span></span>
    </label>`).join('') || '<div class="empty">לא נמצאו תיקים בטווח</div>')
    + (cases.length? `<div style="position:sticky;bottom:0;background:var(--surface,#fff);padding:10px 0">
        <button class="btn-accent" style="width:100%" onclick="syncPickedCases()"><span id="nc-btn-txt">סמן תיקים לסנכרון</span></button></div>` : '');
}
function ncCount(){
  const n = document.querySelectorAll('.nc-pick:checked').length;
  const t = $('nc-btn-txt'); if(t) t.textContent = n? `⬇ סנכרן ${n} תיקים מסומנים` : 'סמן תיקים לסנכרון';
  const all = $('nc-all'); if(all) all.checked = n===document.querySelectorAll('.nc-pick').length && n>0;
}
function syncPickedCases(){
  const picked = [...document.querySelectorAll('.nc-pick:checked')].map(el=>window._netCases[+el.dataset.i]);
  if(!picked.length){ toast('לא סומן אף תיק', true); return; }
  const items = [];
  for(const c of picked){
    const m = (c.CaseDisplayIdentifier||'').trim().match(/^(\d+)-(\d{1,2})-(\d{2})$/);
    if(!m){ logEvent('⚠ מזהה לא מפוענח: '+c.CaseDisplayIdentifier); continue; }
    items.push({case_number:m[1], month_year:m[2].padStart(2,'0')+m[3], id:c.CaseDisplayIdentifier});
  }
  if(!items.length){ toast('אף מזהה לא פוענח', true); return; }
  act('net_sync_selected?cases='+encodeURIComponent(JSON.stringify(items)),
      `סנכרון ${items.length} תיקים מסומנים`);
  toast(`${items.length} תיקים נשלחו לסנכרון (אצווה אחת)`);
  closeDocList();
}

/* ─── OTP dialog ─── */
function showOtp(){
  if($('otp-box')) return;
  const d = document.createElement('div');
  d.id='otp-box';
  d.style.cssText='position:fixed;bottom:24px;right:24px;z-index:130;background:var(--surface,#fff);'
    +'border:1px solid var(--line,#e5e5e5);border-radius:14px;padding:16px;box-shadow:0 12px 40px rgba(0,0,0,.25);width:260px';
  d.innerHTML = `<b style="font-size:14px">🔐 נדרש קוד אימות</b>
    <div class="sub" style="margin:4px 0 8px">הקוד נשלח אליך ב-SMS / אימייל</div>
    <input id="otp-in" inputmode="numeric" placeholder="קוד בן 6 ספרות" style="width:100%;border:1px solid var(--line,#e5e5e5);border-radius:8px;padding:8px;font-size:14px;letter-spacing:3px;text-align:center">
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn-accent" style="flex:1" onclick="sendOtp()">שלח</button>
      <button class="fv-btn" onclick="$('otp-box').remove()">בטל</button></div>`;
  document.body.appendChild(d);
  $('otp-in').focus();
  $('otp-in').addEventListener('keydown', e=>{ if(e.key==='Enter') sendOtp(); });
}
async function sendOtp(){
  const code = ($('otp-in')?.value||'').trim();
  if(!code) return;
  await act('submit_otp?otp='+encodeURIComponent(code), 'קוד אימות');
  $('otp-box')?.remove();
}

/* ─── activity FAB ─── */
function ensureFab(){
  if($('fab-activity')) return;
  const fab = document.createElement('div');
  fab.className='fab-activity'; fab.id='fab-activity';
  fab.innerHTML=`
    <div class="fab-panel" id="fab-panel">
      <div class="fp-head"><span>📋 פעילות אחרונה</span>
        <div style="display:flex;gap:6px">
          <button class="fv-btn" style="font-size:11px" onclick="toggleLogWin()">יומן מלא</button>
          <button class="fv-btn" style="font-size:11px" onclick="$('fab-panel').classList.remove('on')">✕</button>
        </div></div>
      <div class="fp-body" id="fab-jobs"></div>
    </div>
    <button class="fab-btn" onclick="toggleFab()" title="פעילות אחרונה ויומן">
      📋<span class="fab-badge hide" id="fab-badge">0</span>
    </button>`;
  document.body.appendChild(fab);
}
function toggleFab(){
  const p=$('fab-panel');
  p.classList.toggle('on');
  if(p.classList.contains('on')) refreshFab();
}
function refreshFab(){
  const el=$('fab-jobs'); if(!el||!D?.jobs) return;
  const running = D.jobs.filter(j=>j.state==='RUNNING').length;
  const badge=$('fab-badge');
  if(running){ badge.textContent=running; badge.classList.remove('hide'); }
  else badge.classList.add('hide');
  el.innerHTML = D.jobs.slice(0,20).map(j=>`
    <div class="job"><div class="ic">${JOB_ICONS[j.kind]||'⚙️'}</div>
      <div class="tx"><b>${JOB_LABELS[j.kind]||j.kind}</b><span>${j.message||''}</span></div>
      ${pill(j.state)}</div>`).join('') || '<div class="empty">אין פעילות עדיין</div>';
}
