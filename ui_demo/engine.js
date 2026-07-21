/* ─── engine control / הפעלת מנוע הסנכרון מהאתר ─── */
let _netDownloadJobId=null, _pendingNetCases=[], _dlStats=null;
try{ const _saved=sessionStorage.getItem('dlStats'); if(_saved) _dlStats=JSON.parse(_saved); }catch(_){}
try{ const _savedJid=sessionStorage.getItem('dlJobId'); if(_savedJid) _netDownloadJobId=+_savedJid; }catch(_){}
const SCOPE_LABELS = {all:'כל התיקים', selected:'תיקים מסוימים', related:'תיקים + קשורים'};

let engineStarting=false;
async function startEngine(){
  if(engineStarting) return false;
  engineStarting=true; toast('מפעיל את מנוע הסנכרון…');
  try{ await fetch('/api/system/start',{method:'POST'}); }catch(e){}
  for(let i=0;i<40;i++){
    await new Promise(r=>setTimeout(r,2000));
    try{
      const h = await (await fetch('/api/health')).json();
      if(h.full_ui_alive){
        engineStarting=false; toast('מנוע הסנכרון פעיל ✓');
        await refresh(true);
        return true;
      }
    }catch(e){}
  }
  engineStarting=false; toast('המנוע לא עלה — בדוק את lias_engine.log', true);
  return false;
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
let _jobBarHide=null, _jobBarMinimized=false;
function showJobBar(title, frac, msg){
  let b=$('jobbar');
  if(!b){
    _jobBarMinimized=false;
    b=document.createElement('div'); b.id='jobbar';
    b.style.cssText='position:fixed;bottom:16px;right:50%;transform:translateX(50%);z-index:125;'
      +'background:var(--surface,#fff);border:1px solid var(--line,#e5e5e5);border-radius:14px;'
      +'padding:10px 16px;width:min(440px,92vw);box-shadow:0 12px 40px rgba(0,0,0,.3);direction:rtl;'
      +'transition:all .3s';
    b.innerHTML='<div style="display:flex;justify-content:space-between;align-items:center;font-size:12.5px;margin-bottom:6px">'
      +'<b id="jb-title"></b>'
      +'<div style="display:flex;gap:6px;align-items:center">'
      +'<span id="jb-pct"></span>'
      +'<button id="jb-min" onclick="toggleJobBarMin()" style="background:none;border:none;cursor:pointer;font-size:14px;padding:0 2px;color:var(--ink-soft,#777)" title="מזער">▾</button>'
      +'<button onclick="$(\'jobbar\')?.remove()" style="background:none;border:none;cursor:pointer;font-size:13px;padding:0 2px;color:var(--ink-soft,#777)" title="סגור">✕</button>'
      +'</div></div>'
      +'<div id="jb-detail">'
      +'<div style="height:8px;background:var(--line,#eee);border-radius:6px;overflow:hidden">'
      +'<i id="jb-fill" style="display:block;height:100%;width:0;background:var(--accent,#2F7DF6);transition:width .4s"></i></div>'
      +'<div id="jb-msg" style="font-size:11.5px;color:var(--ink-soft,#777);margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>'
      +'</div>';
    document.body.appendChild(b);
  }
  clearTimeout(_jobBarHide);
  $('jb-title').textContent = title;
  $('jb-pct').textContent = Math.round((frac||0)*100)+'%';
  $('jb-fill').style.width = ((frac||0)*100)+'%';
  if(msg) $('jb-msg').textContent = msg;
}
function toggleJobBarMin(){
  _jobBarMinimized=!_jobBarMinimized;
  const d=$('jb-detail'); if(d) d.style.display=_jobBarMinimized?'none':'';
  const btn=$('jb-min'); if(btn) btn.textContent=_jobBarMinimized?'▴':'▾';
  const b=$('jobbar');
  if(b) b.style.width=_jobBarMinimized?'auto':'min(440px,92vw)';
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
      if(/מחובר|✓|הצליח|נכנס|logged|success|connected|dashboard/.test(e.message||'')){
        $('otp-box')?.remove(); _otpSubmitted=false;
      }
    }
    if(e.type==='drive'){ logEvent('☁ '+(e.message||'')); }
    if(e.type==='drive_error'){ logEvent('⚠ '+(e.message||'')); toast(e.message||'שגיאת Drive', true); }
    if(e.type==='net_cases'){ showNetCases(e.cases||[]); }
    if(e.type==='eca_cases'){ showEcaCases(e.cases||[]); }
    if(e.type==='download_stats'){
      _dlStats=e; try{sessionStorage.setItem('dlStats',JSON.stringify(e));}catch(_){}
      if(e.job_id && !_netDownloadJobId){ _netDownloadJobId=e.job_id; try{sessionStorage.setItem('dlJobId',e.job_id);}catch(_){} }
      _renderDlStats();
      if(e.done>0 && e.done%3===0 && typeof refresh==='function') refresh(true);
    }
    if(e.type==='job'){
      $('otp-box')?.remove();
      if(e.message) logEvent(`#${e.job_id||''} ${e.message}`);
      if(typeof e.progress==='number')
        showJobBar(JOB_LABELS[e.kind]||`משימה #${e.job_id||''}`, e.progress, e.message||'');
      if(e.state==='COMPLETED'||e.state==='ERROR'){
        $('otp-box')?.remove();
        if(e.kind==='net_smart_download'||e.kind==='net_download_all'){
          _dlStats=null; _netDownloadJobId=null;
          try{sessionStorage.removeItem('dlStats');sessionStorage.removeItem('dlJobId');}catch(_){}
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
      <div class="fv-title" style="color:#e8f5e9">יומן חי</div>
      <button class="fv-btn log-tab-btn" id="lg-t-engine" style="border-color:#2a352c;color:#c8e6c9" onclick="setLogTab('engine')">כללי</button>
      <button class="fv-btn log-tab-btn" id="lg-t-drive" style="border-color:#2a352c;color:#c8e6c9" onclick="setLogTab('drive')">Drive</button>
      <button class="fv-btn log-tab-btn" id="lg-t-transcription" style="border-color:#2a352c;color:#c8e6c9" onclick="setLogTab('transcription')">תמלול</button>
      <button class="fv-btn log-tab-btn" id="lg-t-events" style="border-color:#2a352c;color:#c8e6c9" onclick="setLogTab('events')">אירועים</button>
      <button class="fv-btn" style="border-color:#2a352c;color:#c8e6c9" onclick="copyLog()" title="העתק את הלוג ללוח">📋</button>
      <button class="fv-btn" style="border-color:#2a352c;color:#c8e6c9" onclick="downloadLog()" title="הורד כקובץ טקסט">⬇</button>
      <button class="fv-btn" style="border-color:#2a352c;color:#c8e6c9" onclick="maximizeLogWin()" title="הגדל/הקטן">⛶</button>
    </div>
    <div id="logwin-body" style="flex:1;overflow-y:auto;padding:8px 12px;font-size:11.5px;
      font-family:ui-monospace,monospace;direction:ltr;text-align:left;line-height:1.65;white-space:pre-wrap"></div>`;
  document.body.appendChild(w);
  _makeDraggable('logwin','logwin-top');
  setLogTab('engine');
  _logTimer = setInterval(_refreshLog, 3000);
}
function setLogTab(t){
  _logTab=t; _refreshLog();
  document.querySelectorAll('.log-tab-btn').forEach(b=>b.style.background='transparent');
  const active=$('lg-t-'+t); if(active) active.style.background='rgba(255,255,255,.15)';
}
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
      let filtered = (r.lines||[]).filter(l=>!/^INFO:\s+127\.0\.0\.1.*"(GET|POST|PUT|DELETE|OPTIONS) \/api\/(log|health|settings|events)/.test(l));
      if(_logTab==='drive'){
        filtered = filtered.filter(l=>/drive|google|upload|gdrive|cloud/i.test(l));
      } else if(_logTab==='transcription'){
        filtered = filtered.filter(l=>/transcri|whisper|תמלול|הקלטה|audio|speech/i.test(l));
      }
      const esc = t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;');
      const paint = l=>{
        const e=esc(l);
        if(/✗|שגיא|ERROR|Error|Traceback|failed|FAILED|Exception/.test(l))
          return `<span style="color:#ff8a80">${e}</span>`;
        if(/✓|Success|הושלם|הצליח|COMPLETED/.test(l))
          return `<span style="color:#69f0ae">${e}</span>`;
        if(/⚠|warn|WARN/.test(l)) return `<span style="color:#ffd54f">${e}</span>`;
        return e;
      };
      el.innerHTML = filtered.map(paint).join('\n')
        || (_logTab==='drive'?'אין לוגים של Drive':_logTab==='transcription'?'אין לוגים של תמלול':'הלוג ריק');
      window._logText = filtered.join('\n');
    }catch(e){ el.innerHTML = '<div style="color:#7a8a7d">המנוע כבוי — אין לוג. הפעל את המנוע.</div>'; }
  }
  if(atBottom) el.scrollTop = el.scrollHeight;
}

/* ─── tasks balloon: what runs now, what waits, rate, per-job stop ─── */
let _tasksTimer=null;
function toggleTasksWin(forceOpen){
  let w=$('taskswin');
  if(w && !forceOpen){ clearInterval(_tasksTimer); _tasksTimer=null; w.remove();
    try{sessionStorage.setItem('tasksWinOpen','0');}catch(_){}
    return; }
  if(w) return;
  w=document.createElement('div'); w.id='taskswin';
  w.style.cssText='position:fixed;bottom:16px;left:16px;width:min(440px,92vw);max-height:66vh;'
    +'background:var(--surface,#fff);border:1px solid var(--line,#e5e5e5);border-radius:14px;'
    +'z-index:119;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.35);'
    +'overflow:hidden;direction:rtl';
  w.innerHTML=`<div class="fv-top" id="taskswin-top">
      <button class="fv-btn" onclick="toggleTasksWin()">✕</button>
      <div class="fv-title">⏱ משימות פעילות</div>
      <button class="fv-btn" style="color:var(--danger)" onclick="stopAllDownloads()" title="עצור את כל ההורדות הפעילות">⏹ עצור הכל</button>
    </div>
    <div id="taskswin-body" style="flex:1;overflow-y:auto;padding:10px 14px;font-size:12.5px"></div>`;
  document.body.appendChild(w);
  _makeDraggable('taskswin','taskswin-top');
  try{sessionStorage.setItem('tasksWinOpen','1');}catch(_){}
  _refreshTasks();
  _tasksTimer=setInterval(_refreshTasks, 2500);
}
function stopAllDownloads(){
  document.querySelectorAll('[data-stopjob]').forEach(b=>{});
  // cancel every active download job
  (window._activeJobs||[]).forEach(j=>{
    if(['net_smart_download','net_download_all','bdr_batch','eca_sync'].includes(j.kind))
      fetch('/api/proxy/actions/cancel_download?job_id='+j.job_id,{method:'POST'});
  });
  toast('נשלחה הוראת עצירה לכל ההורדות — ייעצרו אחרי התיק הנוכחי');
}
function stopJob(jobId){
  fetch('/api/proxy/actions/cancel_download?job_id='+jobId,{method:'POST'});
  toast('נשלחה הוראת עצירה — התיק הנוכחי יסתיים ואז ייעצר');
}
async function _refreshTasks(){
  const el=$('taskswin-body'); if(!el) return;
  try{
    const jobs = await (await fetch('/api/jobs?limit=25')).json();
    const active = (jobs||[]).filter(j=>['RUNNING','PENDING'].includes(j.state));
    const recent = (jobs||[]).filter(j=>!['RUNNING','PENDING'].includes(j.state)).slice(0,5);
    window._activeJobs = active;
    const st = _dlStats;
    const CANCELLABLE = ['net_smart_download','net_download_all','bdr_batch','eca_sync'];
    const row = j=>`<div style="padding:8px 0;border-bottom:1px solid var(--line)">
      <div style="display:flex;align-items:center;gap:6px">
        <b style="flex:1">${JOB_ICONS[j.kind]||'⚙'} ${JOB_LABELS[j.kind]||j.kind}</b>
        ${pill(j.state)}
        ${j.state==='RUNNING'&&CANCELLABLE.includes(j.kind)?`<button class="fv-btn" style="font-size:10.5px;padding:3px 8px;color:var(--danger)" onclick="stopJob(${j.job_id})">⏹ עצור</button>`:''}
      </div>
      ${j.state==='RUNNING'?`<div style="height:6px;background:var(--line);border-radius:4px;margin:5px 0"><i style="display:block;height:100%;width:${Math.round((j.progress||0)*100)}%;background:var(--accent);border-radius:4px;transition:width .4s"></i></div>`:''}
      <div style="font-size:11.5px;color:var(--ink-soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${j.message||j.error||''}</div>
    </div>`;
    el.innerHTML =
      (st?`<div style="padding:8px 12px;margin-bottom:8px;border-radius:10px;background:var(--accent-soft,#eef4ff);font-weight:600">
        ⬇ הורדה פעילה: <b>${st.done||0}</b>${st.total?'/'+st.total:''} מסמכים${st.cases_total?` · תיק ${st.case_idx||0}/${st.cases_total}`:''}
        ${st.rate?` · ${st.rate}/דקה`:''} ${st.errors?` · <span style="color:var(--danger)">${st.errors} כשלו</span>`:''}
      </div>`:'')
      + (active.length? '<div style="font-weight:800;margin:4px 0;color:var(--accent-strong,#1d64d8)">רץ עכשיו / בהמתנה</div>'+active.map(row).join('')
                      : '<div class="empty" style="padding:16px 0">אין משימות פעילות כרגע</div>')
      + (recent.length? '<div style="font-weight:800;margin:12px 0 4px;opacity:.7">הסתיימו לאחרונה</div>'+recent.map(row).join('') : '');
  }catch(e){ el.innerHTML='<div class="empty">המנוע כבוי — הפעל סנכרון כדי לראות משימות</div>'; }
}

function copyLog(){
  const t = _logTab==='events' ? _logBuf.join('\n') : (window._logText||'');
  navigator.clipboard.writeText(t).then(()=>toast('הלוג הועתק ✓')).catch(()=>toast('שגיאה בהעתקה', true));
}
function downloadLog(){
  const t = _logTab==='events' ? _logBuf.join('\n') : (window._logText||'');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([t],{type:'text/plain'}));
  a.download=`lias_log_${_logTab}_${new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')}.txt`;
  a.click();
}
let _logMax=false;
function maximizeLogWin(){
  const w=$('logwin'); if(!w) return;
  _logMax=!_logMax;
  if(_logMax){ w.style.width='94vw'; w.style.height='86vh'; w.style.left='3vw'; w.style.bottom='4vh'; }
  else{ w.style.width='min(560px,94vw)'; w.style.height='380px'; w.style.left='16px'; w.style.bottom='16px'; }
}

/* ─── real automation browser — show/hide ─── */
let _realBrowserVisible=false;
function runBdrByClient(){
  const flt = prompt('שם הלקוח להורדת תיקיו מבית הדין הרבני:');
  if(flt === null) return;
  if(!flt.trim()){ toast('לא הוזן שם', true); return; }
  runBdrBatch(flt.trim());
}
function runBdrBatch(client_filter){
  client_filter = client_filter || '';
  toast(client_filter ? `מוריד תיקי בד"ר של "${client_filter}"…` : 'מתחבר לבית הדין הרבני ומוריד את כל התיקים…');
  logEvent('→ הורדת תיקי BDR' + (client_filter? ' — '+client_filter : ' (הכל)'));
  fetch('/api/proxy/actions/bdr_batch', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_mode:'lawyer', client_filter})
  }).then(r=>{
    if(r.ok) toast('הורדת BDR הופעלה ✓');
    else toast('שגיאה בהפעלת BDR', true);
  }).catch(e=>toast('שגיאה: '+e.message, true));
}
/* ECA: connect → list cases (with parties) → pick with checkboxes → download */
let _ecaCases = [];
function ecaConnectAndList(){
  toast('מתחבר להוצאה לפועל ושולף תיקים…');
  logEvent('→ התחברות והצגת תיקי הוצל"פ');
  const picker=$('sync-case-picker');
  if(picker){ picker.style.display='block';
    picker.innerHTML='<div style="padding:10px;color:rgba(255,255,255,.7)">מתחבר לפורטל ההוצאה לפועל… (ייתכן שיידרש קוד אימות)</div>'; }
  act('eca_list','חיבור והצגת תיקי הוצל"פ');
}
function showEcaCases(cases){
  _ecaCases = cases||[];
  const picker=$('sync-case-picker'); if(!picker) return;
  if(!_ecaCases.length){ picker.style.display='none'; toast('לא נמצאו תיקי הוצל"פ', true); return; }
  picker.style.display='block';
  picker.innerHTML = `<div style="font-size:12px;color:rgba(255,255,255,.7);margin-bottom:6px">
      נמצאו ${_ecaCases.length} תיקי הוצאה לפועל — סמן מה להוריד</div>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllEca(true)">סמן הכל</button>
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllEca(false)">נקה הכל</button>
    </div>
    <div style="max-height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:4px">
      ${_ecaCases.map((c,i)=>`<label style="display:flex;gap:8px;align-items:center;padding:7px 10px;border:1px solid rgba(255,255,255,.12);border-radius:8px;cursor:pointer">
        <input type="checkbox" class="eca-cb" data-i="${i}" checked>
        <div style="flex:1;min-width:0">
          <div style="font-weight:700;font-size:13px">${c.number} <span style="font-weight:400;opacity:.7">· ${c.type||''}</span></div>
          <div style="font-size:11.5px;opacity:.7;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            ${c.role||''}${c.party?` · הצד שכנגד: ${c.party}`:''}</div>
        </div>
      </label>`).join('')}
    </div>
    <button class="btn-accent" style="width:100%;margin-top:10px;padding:12px" onclick="runEcaSelected()">⬇ הורד את המסומנים</button>`;
}
function _selectAllEca(v){ document.querySelectorAll('.eca-cb').forEach(cb=>cb.checked=v); }
function runEcaSelected(){
  const picked = [...document.querySelectorAll('.eca-cb:checked')].map(cb=>_ecaCases[+cb.dataset.i].number);
  if(!picked.length){ toast('לא סומן אף תיק', true); return; }
  const all = picked.length===_ecaCases.length;
  toast(all?'מוריד את כל תיקי ההוצל"פ…':`מוריד ${picked.length} תיקי הוצל"פ…`);
  logEvent('→ הורדת הוצל"פ ('+(all?'הכל':picked.join(', '))+')');
  fetch('/api/proxy/actions/eca_sync', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(all ? {} : {cases:picked})
  }).then(r=>{ if(r.ok) toast('הורדת הוצל"פ הופעלה ✓'); else toast('שגיאה', true); })
    .catch(e=>toast('שגיאה: '+e.message, true));
  const picker=$('sync-case-picker'); if(picker){ picker.style.display='none'; }
}
async function toggleRealBrowser(){
  if(!D?.live){
    const ok = await startEngine();
    if(!ok){ toast('המנוע לא עלה', true); return; }
  }
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
/* Platform-first sync: pick a portal, THEN see only its relevant options.
   The three portals are independent — NET/BDR/ECA never share a flow. */
let _syncPlatform = null;
function syncCard(el){
  el.innerHTML = `<h2>סנכרון — הורדת תיקים</h2>
    <div class="meta">בחר תחילה מאיזה פורטל להוריד, ואז מה להוריד.
      ☁ אם Drive מוגדר, הקבצים עולים אוטומטית במקביל.</div>
    <div id="dl-stats-panel" style="display:none"></div>
    <div style="display:flex;gap:10px;margin-top:12px" id="sync-platforms">
      <button class="sync-plat" id="plat-NET" onclick="pickPlatform('NET')">🏛<div>נט המשפט</div></button>
      <button class="sync-plat" id="plat-BDR" onclick="pickPlatform('BDR')">🕍<div>בית הדין הרבני</div></button>
      <button class="sync-plat" id="plat-ECA" onclick="pickPlatform('ECA')">⚖️<div>הוצאה לפועל</div></button>
    </div>
    <div id="sync-options" style="margin-top:14px"></div>
    <div id="sync-case-picker" style="display:none;margin-top:12px"></div>
`;
  fetch('/api/settings').then(r=>r.json()).then(st=>{
    window._settings = st;
    _currentScope = st.case_scope || 'all';
    if(_syncPlatform) pickPlatform(_syncPlatform);   // re-render with fresh settings
  }).catch(()=>{});
  if(_syncPlatform) pickPlatform(_syncPlatform);
  if(_dlStats) _renderDlStats();
}

function pickPlatform(p){
  _syncPlatform = p;
  ['NET','BDR','ECA'].forEach(x=>{
    const b=$('plat-'+x); if(b) b.classList.toggle('on', x===p);
  });
  const box=$('sync-options'); if(!box) return;
  const picker=$('sync-case-picker'); if(picker){ picker.style.display='none'; picker.innerHTML=''; }
  // Behavior follows the per-platform settings (Settings ⚙). One download
  // button per platform; a shared "check a specific case by number" tool.
  const s = window._settings || {};
  const scope = {NET:s.net_scope||'selected', BDR:s.bdr_scope||'all', ECA:s.eca_scope||'selected'}[p];
  const label = {NET:'נט המשפט', BDR:'בית הדין הרבני', ECA:'הוצאה לפועל'}[p];
  const scopeText = scope==='all' ? 'כל התיקים (לפי ההגדרות)' : 'הצג רשימה ובחר תיקים (לפי ההגדרות)';
  const relNote = p==='NET' && s.net_related ? ' · כולל תיקים קשורים' : '';
  const runFn = {NET:'runNet()', BDR:'runBdr()', ECA:'ecaConnectAndList()'}[p];
  box.innerHTML = `
    <div class="sync-opt-row">
      <button class="btn-accent sync-opt" onclick="${runFn}">⬇ הורד מ${label} — ${scopeText}</button>
      <div class="sync-opt-hint">ההתנהגות נקבעת ב<a onclick="openSettings()" style="text-decoration:underline;cursor:pointer">הגדרות ⚙</a>:
        היקף = <b>${scope==='all'?'כל התיקים':'תיקים מסוימים'}</b>${relNote}.</div>
    </div>
    ${p==='NET'?`<div class="sync-opt-row">
      <button class="btn-accent sync-opt" onclick="syncOpenNetCase()">🔄 סנכרן את התיק הפתוח בדפדפן</button>
      <div class="sync-opt-hint">מוריד את המסמכים החדשים של התיק שפתוח כרגע בדפדפן האוטומציה.</div>
    </div>`:''}
    <div class="sync-opt-row" style="border:1px solid var(--line,rgba(255,255,255,.15));border-radius:10px;padding:10px 12px">
      <div class="sync-opt-hint" style="margin-bottom:6px">🔎 בדיקת עדכונים בתיק ספציפי לפי מספר (גם אם עוד לא במערכת):</div>
      <div style="display:flex;gap:6px;align-items:center">
        <input id="nc-num" placeholder="מספר תיק" style="flex:1;border:1px solid var(--line,rgba(255,255,255,.2));border-radius:8px;padding:7px 10px;font-size:13px;background:rgba(127,127,127,.08);color:inherit">
        ${p==='NET'?`<input id="nc-my" type="month" style="width:130px;border:1px solid var(--line,rgba(255,255,255,.2));border-radius:8px;padding:7px 8px;font-size:12px;background:rgba(127,127,127,.08);color:inherit">`:''}
        <button class="btn-accent" style="padding:7px 14px;font-size:12px;white-space:nowrap" onclick="checkCaseByNumber('${p}')">אתר וסנכרן</button>
      </div>
    </div>`;
}
function runNet(){
  const s = window._settings || {};
  if((s.net_scope||'selected')==='all'){
    if(!confirm('להוריד את כל תיקי נט המשפט? (לפי ההגדרות)')) return;
    act('net_download_all','הורדת כל תיקי נט');
  } else {
    startNetDownload();   // show list → pick
  }
}
function runBdr(){
  const s = window._settings || {};
  if((s.bdr_scope||'all')==='all') runBdrBatch('');
  else runBdrByClient();
}
function checkCaseByNumber(portal){
  if(portal==='NET'){ openNetCase(true); return; }
  const num=($('nc-num')?.value||'').trim();
  if(!num){ toast('נא להזין מספר תיק', true); return; }
  if(portal==='ECA'){
    act(`eca_sync`,'סנכרון תיק הוצל"פ '+num);
    fetch('/api/proxy/actions/eca_sync',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cases:[num]})});
  } else if(portal==='BDR'){
    runBdrBatch(num);   // client_filter also matches a case number substring
  }
}
function syncOpenNetCase(){ act('sync_current/NET','סנכרון התיק הפתוח בנט'); }
function startNetDownload(){
  toast('מתחבר לנט המשפט ומחפש תיקים…');
  act('net_list_cases','חיפוש תיקים בנט');
}
/* first showNetCases — always show picker so user selects which cases to download */
function showNetCases(cases){
  _pendingNetCases = cases;
  const picker=$('sync-case-picker'); if(!picker) return;
  if(!cases.length){
    picker.style.display='none';
    toast('לא נמצאו תיקים בפורטל', true);
    return;
  }
  picker.style.display='block';
  const scope = _currentScope;
  picker.innerHTML=`<div style="font-size:12px;color:rgba(255,255,255,.7);margin-bottom:6px">
      נמצאו ${cases.length} תיקים בנט — סמן מה להוריד${scope==='related'?' (+ תיקים קשורים)':''}</div>
    <div style="display:flex;gap:6px;margin-bottom:6px">
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllNetCases(true)">סמן הכל</button>
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllNetCases(false)">נקה הכל</button>
    </div>
    <div id="net-cases-list" style="max-height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:3px">
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
  const checks = [...document.querySelectorAll('#net-cases-list input:checked')];
  if(!checks.length){ toast('סמן תיק אחד לפחות', true); return; }
  const selected = checks.map(cb=>_pendingNetCases[+cb.value]).filter(Boolean);
  const mode = _currentScope==='related' ? 'related' : 'selected';
  _startSmartDownload(mode, selected);
}
async function _startSmartDownload(mode, cases){
  if(!D?.live){
    const ok = await startEngine();
    if(!ok){ toast('המנוע לא עלה — לא ניתן להוריד', true); return; }
  }
  const body = JSON.stringify({mode, cases, years_back:10});
  try{
    const r = await fetch('/api/proxy/actions/net_smart_download', {
      method:'POST', headers:{'Content-Type':'application/json'}, body});
    const j = await r.json();
    if(j.job_id){ _netDownloadJobId = j.job_id; try{sessionStorage.setItem('dlJobId',j.job_id);}catch(_){} }
    toast('ההורדה התחילה');
    // No auto browser/show — popping the automation Chrome mid-work looked
    // like "a random unrelated browser opened". Use the 🖥 toggle to peek.
    const picker=$('sync-case-picker');
    if(picker){ picker.style.display='none'; picker.innerHTML=''; }
  }catch(e){ toast('שגיאה: '+e.message, true); }
}
function cancelNetDownload(){
  const jid = _netDownloadJobId || sessionStorage.getItem('dlJobId');
  if(!jid){ toast('אין הורדה פעילה לבטל', true); return; }
  act('cancel_download?job_id='+jid, 'ביטול הורדה');
  toast('שולח הוראת עצירה — ההורדה תיעצר אחרי התיק הנוכחי');
}
function _renderDlStats(){
  const s=_dlStats; if(!s) return;
  let panel=$('dl-stats-panel');
  if(!panel){
    panel=document.createElement('div'); panel.id='dl-stats-panel';
    panel.style.cssText='position:fixed;top:80px;left:16px;z-index:120;width:min(380px,90vw);'
      +'max-height:70vh;overflow-y:auto;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.35);direction:rtl';
    document.body.appendChild(panel);
  }
  panel.style.display='block';
  const pct = s.total? Math.round(s.done/s.total*100) : 0;
  const elapsed = s.elapsed_sec||0;
  const mm = Math.floor(elapsed/60), ss = String(elapsed%60).padStart(2,'0');
  const etaM = s.eta_sec? Math.floor(s.eta_sec/60) : 0;
  const etaS = s.eta_sec? String(s.eta_sec%60).padStart(2,'0') : '00';
  const currentLabel = s.current_name
    ? `${s.current_case} — ${s.current_name}`
    : s.current_case||'';

  let casesHtml = '';
  const details = s.cases_detail||[];
  if(details.length){
    const completed = new Set((s.completed_cases||[]).map(c=>c.id));
    casesHtml = `<div style="margin-top:8px;max-height:180px;overflow-y:auto;font-size:11px;border-top:1px solid rgba(255,255,255,.1);padding-top:6px">
      <b style="font-size:11.5px">תיקים (${s.done}/${s.total}):</b>
      ${details.map(c=>{
        const isCurrent = c.id===s.current_case;
        const isDone = completed.has(c.id);
        const icon = isDone?'✓':isCurrent?'⏳':'·';
        const opacity = isDone?'.5':isCurrent?'1':'.65';
        const weight = isCurrent?'bold':'normal';
        const clickable = isDone ? `cursor:pointer` : '';
        const onclick = isDone ? `onclick="closeDlPanel();_goToCaseByNumber('${c.id}')"` : '';
        return `<div style="padding:3px 0;opacity:${opacity};font-weight:${weight};border-bottom:1px solid rgba(255,255,255,.05);${clickable}" ${onclick}>
          ${icon} <b>${c.id}</b> ${c.name||''}<br>
          <span style="color:rgba(255,255,255,.4);font-size:10px">${c.court||''} · ${c.type||''} · ${c.interest||''} · ${c.status||''}</span>
        </div>`;
      }).join('')}
    </div>`;
  }

  panel.innerHTML=`<div style="background:var(--card-bg,#1a2332);border:1px solid rgba(255,255,255,.2);border-radius:12px;padding:12px 16px">
    <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;margin-bottom:6px">
      <b>הורדה פעילה</b>
      <div style="display:flex;gap:8px;align-items:center">
        <span>${pct}% · ${s.done}/${s.total} תיקים</span>
        <button onclick="closeDlPanel()" style="background:none;border:none;cursor:pointer;color:rgba(255,255,255,.5);font-size:14px">✕</button>
      </div></div>
    <div style="height:7px;background:rgba(255,255,255,.12);border-radius:4px;overflow:hidden;margin-bottom:8px">
      <div style="height:100%;width:${pct}%;background:var(--accent);transition:width .4s"></div></div>
    ${currentLabel?`<div style="font-size:12px;margin-bottom:6px">📂 <b>${currentLabel}</b></div>`:''}
    <div style="display:flex;flex-wrap:wrap;gap:10px 16px;font-size:11.5px;color:rgba(255,255,255,.55)">
      <span>נותרו: ${s.remaining||0}</span>
      <span>נכשלו: <span style="color:${s.failed?'#ef5350':'inherit'}">${s.failed||0}</span></span>
      <span>מסמכים: ${s.docs_downloaded||0}</span>
      <span>קצב: ${s.speed_per_min||0}/דק׳</span>
      <span>זמן: ${mm}:${ss}</span>
      ${s.eta_sec?`<span>צפי סיום: ~${etaM}:${etaS}</span>`:''}
    </div>
    ${s.output_dir?`<div style="font-size:10px;color:rgba(255,255,255,.35);margin-top:6px;word-break:break-all">📁 ${s.output_dir}</div>`:''}
    ${casesHtml}
    <button class="btn-accent" style="margin-top:10px;font-size:12px;padding:6px 14px;background:rgba(198,40,40,.8)"
      onclick="cancelNetDownload()">⏹ עצור הורדה</button>
  </div>`;
}

function closeDlPanel(){ const p=$('dl-stats-panel'); if(p) p.style.display='none'; }
function _goToCaseByNumber(displayId){
  if(!D?.case_cards) return;
  const card = D.case_cards.find(c=>(c.sub_number||'').includes(displayId));
  if(card){ go('case', card.sub_case_id); }
  else { toast('התיק טרם נוסף למערכת — רענן את הדשבורד'); refresh(true); }
}

/* ─── NET cases checkbox picker — inline in sync card ─── */
/* This second definition overrides the first showNetCases above. */
function showNetCases(cases){
  _pendingNetCases = cases;
  if(!cases.length){
    toast('לא נמצאו תיקים בפורטל', true);
    return;
  }
  // Navigate to sync tab so the picker is visible
  if(route.v !== 'sync'){ go('sync'); }
  // Wait for DOM to render sync card
  setTimeout(()=>_renderNetCasesPicker(cases), 200);
}
function _renderNetCasesPicker(cases){
  const picker = $('sync-case-picker');
  if(!picker){ setTimeout(()=>_renderNetCasesPicker(cases), 300); return; }
  const _esc = s => (s||'').replace(/"/g, '״').replace(/'/g, '׳');
  picker.style.display='block';
  picker.innerHTML=`<div style="font-size:13px;color:rgba(255,255,255,.85);margin-bottom:8px;font-weight:700">
      נמצאו ${cases.length} תיקים — סמן מה להוריד</div>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllNetCases(true)">סמן הכל</button>
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllNetCases(false)">נקה הכל</button>
    </div>
    <div id="net-cases-list" style="max-height:400px;overflow-y:auto;display:flex;flex-direction:column;gap:4px">
    ${cases.map((c,i)=>{
      const did = _esc(c.CaseDisplayIdentifier || c.display_id || '');
      const name = _esc(c.CaseName || c.name || '');
      const type = _esc(c.CaseTypeShortName || c.type || '');
      const court = _esc(c.CourtName || c.court || '');
      const status = _esc(c.CaseStatusName || c.status || '');
      const interest = _esc(c.CaseInterestName || c.interest || '');
      const dm = did.match(/(\d+)-(\d{2})-(\d{2})/);
      const mmyy = dm ? dm[2]+'/'+dm[3] : '';
      return `<label style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;
        background:rgba(255,255,255,.08);cursor:pointer;font-size:12.5px;color:rgba(255,255,255,.9)">
        <input type="checkbox" name="net-case" value="${i}" style="accent-color:var(--accent)">
        <span style="flex:1">
          <span style="display:flex;justify-content:space-between;align-items:baseline">
            <b>${did}</b>
            <span style="font-size:11px;color:rgba(255,255,255,.45)">${mmyy}</span>
          </span>
          <span style="display:block;font-size:12px;margin-top:2px">${name}</span>
          <span style="display:block;font-size:11px;color:rgba(255,255,255,.5);margin-top:1px">${type} · ${court}${status?' · '+status:''}${interest?' · '+interest:''}</span>
        </span>
      </label>`;}).join('')}
    </div>
    <button class="btn-accent" style="margin-top:10px;padding:12px;font-size:14px;width:100%"
      onclick="_confirmNetCases()">⬇ הורד תיקים מסומנים</button>`;
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
    const did = (c.CaseDisplayIdentifier||c.display_id||'').trim();
    if(c.case_number && c.mmyy){
      items.push({case_number:c.case_number, month_year:c.mmyy, id:did, name:c.name||c.CaseName||''});
      continue;
    }
    const m = did.match(/^(\d+)-(\d{1,2})-(\d{2})$/);
    if(!m){ logEvent('⚠ מזהה לא מפוענח: '+did); continue; }
    items.push({case_number:m[1], month_year:m[2].padStart(2,'0')+m[3], id:did, name:c.name||c.CaseName||''});
  }
  if(!items.length){ toast('אף מזהה לא פוענח', true); return; }
  act('net_sync_selected?cases='+encodeURIComponent(JSON.stringify(items)),
      `סנכרון ${items.length} תיקים מסומנים`);
  toast(`${items.length} תיקים נשלחו לסנכרון (אצווה אחת)`);
  closeDocList();
}

/* ─── OTP dialog ─── */
let _otpSubmitted=false;
function showOtp(){
  if($('otp-box') || _otpSubmitted) return;
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
  _otpSubmitted=true;
  $('otp-box')?.remove();
  await act('submit_otp?otp='+encodeURIComponent(code), 'קוד אימות');
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
          <button class="fv-btn" style="font-size:11px" onclick="toggleLogWin()">📜 יומן</button>
          <button class="fv-btn" style="font-size:11px" onclick="$('fab-panel').classList.remove('on')">✕</button>
        </div></div>
      <div id="fab-status" style="padding:6px 12px;font-size:11px;border-bottom:1px solid var(--line)"></div>
      <div class="fp-body" id="fab-jobs"></div>
    </div>
    <div style="display:flex;gap:6px;align-items:center">
      <button class="fab-btn" onclick="toggleTasksWin()" title="משימות פעילות — מה רץ, מה ממתין, קצב ועצירה" style="font-size:16px;width:36px;height:36px">⏱</button>
      <button class="fab-btn" onclick="toggleLogWin()" title="יומן חי" style="font-size:16px;width:36px;height:36px">📜</button>
      <button class="fab-btn" onclick="toggleFab()" title="פעילות אחרונה ויומן">
        📋<span class="fab-badge hide" id="fab-badge">0</span>
      </button>
    </div>`;
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
  const st=$('fab-status');
  if(st){
    const eng = D.live ? '<span style="color:#4ade80">● מנוע פעיל</span>' : '<span style="color:#f87171">● מנוע כבוי</span>';
    const trJobs = Object.values(typeof _transcription_jobs_local!=='undefined'?_transcription_jobs_local:{});
    const trActive = trJobs.filter(j=>j.state==='transcribing'||j.state==='queued').length;
    const trDone = trJobs.filter(j=>j.state==='done').length;
    let trLine = '';
    if(trActive) trLine = ` · <span style="color:#60a5fa">🎙 ${trActive} תמלולים פעילים</span>`;
    else if(trDone) trLine = ` · 🎙 ${trDone} תמלולים הושלמו`;
    st.innerHTML = eng + (running ? ` · ⚙ ${running} משימות` : '') + trLine;
  }
  const groups = {};
  for(const j of D.jobs.slice(0,30)){
    const k = j.kind||'other';
    (groups[k] = groups[k]||[]).push(j);
  }
  let html = '';
  for(const [kind, jobs] of Object.entries(groups)){
    const label = JOB_LABELS[kind]||kind;
    const icon = JOB_ICONS[kind]||'⚙️';
    const runCount = jobs.filter(j=>j.state==='RUNNING').length;
    const doneCount = jobs.filter(j=>j.state==='COMPLETED').length;
    const errCount = jobs.filter(j=>j.state==='ERROR').length;
    html += `<div style="margin:8px 0 4px;font-size:12px;font-weight:700;color:var(--ink-soft);display:flex;align-items:center;gap:6px">
      ${icon} ${label} <span style="font-weight:400;font-size:11px">(${jobs.length}${runCount?' • '+runCount+' פעיל':''}${errCount?' • '+errCount+' שגיאות':''})</span></div>`;
    for(const j of jobs.slice(0,5)){
      html += `<div class="job"><div class="ic">${icon}</div>
        <div class="tx"><b>${j.message||label}</b><span>${j.started_at||''}</span></div>
        ${pill(j.state)}</div>`;
    }
    if(jobs.length>5) html += `<div style="font-size:11px;color:var(--ink-soft);padding:2px 8px">ועוד ${jobs.length-5}…</div>`;
  }
  el.innerHTML = html || '<div class="empty">אין פעילות עדיין</div>';
}
