/* ─── engine control / הפעלת מנוע הסנכרון מהאתר ─── */
let _netDownloadJobId=null, _pendingNetCases=[];
/* Per-portal download stats — each portal (NET/BDR/ECA) keeps its own live
   stats so parallel downloads never overwrite each other in the panel
   (previously a single _dlStats caused "shows NET while I'm on ECA"). */
let _dlByPortal={}, _dlStatsTimer=null, _importBatchTimer=null;
const PORTAL_LABELS={NET:'נט המשפט', BDR:'בית הדין הרבני', ECA:'הוצאה לפועל'};
try{ const _saved=sessionStorage.getItem('dlByPortal'); if(_saved) _dlByPortal=JSON.parse(_saved)||{}; }catch(_){}
try{ const _savedJid=sessionStorage.getItem('dlJobId'); if(_savedJid) _netDownloadJobId=+_savedJid; }catch(_){}
function _saveDl(){ try{sessionStorage.setItem('dlByPortal',JSON.stringify(_dlByPortal));}catch(_){}}

/* ── case-list helpers (sort + cumulative merge), field-tolerant across NET/ECA ── */
function _caseId(c){ return c.CaseDisplayIdentifier || c.display_id || c.number || c.id || ''; }
function _caseStatus(c){ return c.CaseStatusName || c.status || ''; }
function _caseIsOpen(c){ const s=_caseStatus(c); return !s || /פתוח|פעיל|open/i.test(s); }
/* derive a sortable date key from an explicit date field or the NNNN-MM-YY id */
function _caseDateKey(c){
  const d = c.date || c.OpenDate || c.open_date || '';
  const m1 = String(d).match(/(\d{2,4})[-/.](\d{1,2})[-/.](\d{1,2})/);
  if(m1){ let y=+m1[1]; if(y<100)y+=2000; return y*10000 + (+m1[2])*100 + (+m1[3]); }
  const id = String(_caseId(c));
  const m2 = id.match(/(\d+)-(\d{2})-(\d{2})/);   // seq-MM-YY
  if(m2){ return (2000+ +m2[3])*10000 + (+m2[2])*100; }
  return 0;
}
/* open cases first, then newest date first */
function _sortCasesForPicker(cases){
  return cases.slice().sort((a,b)=>{
    const oa=_caseIsOpen(a)?0:1, ob=_caseIsOpen(b)?0:1;
    if(oa!==ob) return oa-ob;
    return _caseDateKey(b)-_caseDateKey(a);
  });
}
/* ── open/closed filter (Settings ⚙ → הורדות → סינון לפי מצב תיק) ──────────
   all         — everything
   open        — only cases that are open
   open_sub    — cases open OR holding at least one open sub-case
   open_client — every case of any client who has at least one open case
                 (a lawyer usually wants the closed history of an active client) */
function _openFilterMode(){
  // the sync screen's own choice wins for this run; Settings is the default
  if(window._syncOpenOverride) return window._syncOpenOverride;
  const s = window._settings || {};
  if(s.open_filter) return s.open_filter;
  try{ return localStorage.getItem('lias_open_filter') || 'all'; }catch(_){ return 'all'; }
}
function _caseClientKey(c){
  return (c.client || c.party || c.CaseName || c.name || '').trim();
}
function _hasOpenSub(c){
  return (c.sub_cases||[]).some(s=>{
    const st = s.status||''; return !st || /פתוח|פעיל|open/i.test(st);
  });
}
function _applyOpenFilter(cases, mode){
  mode = mode || _openFilterMode();
  const list = cases||[];
  if(mode==='all' || !list.length) return list;
  if(mode==='open')     return list.filter(_caseIsOpen);
  if(mode==='open_sub') return list.filter(c=>_caseIsOpen(c) || _hasOpenSub(c));
  if(mode==='open_client'){
    const activeClients = new Set(
      list.filter(c=>_caseIsOpen(c)||_hasOpenSub(c)).map(_caseClientKey).filter(Boolean));
    // keep cases of an active client; if a case has no client name, keep it only
    // when it is itself open, so unattributed closed cases don't sneak back in.
    return list.filter(c=>{
      const k=_caseClientKey(c);
      return k ? activeClients.has(k) : (_caseIsOpen(c)||_hasOpenSub(c));
    });
  }
  return list;
}
/* short label for the picker header so the active filter is never invisible */
const OPEN_FILTER_LABELS = {all:'', open:'רק פתוחים',
  open_sub:'פתוחים או עם תת-תיק פתוח', open_client:'לקוחות עם תיק פתוח'};
function _openFilterNote(shown, total){
  const m=_openFilterMode(); if(m==='all' || shown===total) return '';
  return ` · <span style="color:var(--accent-strong,#7fb3ff)">מסונן: ${OPEN_FILTER_LABELS[m]}
           (${shown} מתוך ${total})</span>`;
}
/* merge new cases into an existing list, deduped by case id (cumulative) */
function _mergeCasesList(existing, incoming){
  const byId={}; (existing||[]).forEach(c=>{ byId[_caseId(c)]=c; });
  (incoming||[]).forEach(c=>{ byId[_caseId(c)]=Object.assign(byId[_caseId(c)]||{}, c); });
  return Object.values(byId);
}
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
/* One portal at a time. NET/BDR/ECA share one gov.il identity and one OTP
   mailbox, so two portals running together made each login swallow the other's
   code. The engine enforces this with a lock; this is the friendly front door
   so the user is told *before* a job is queued and fails. */
const PORTAL_JOB_KINDS = ['net_smart_download','net_download_all','net_sync_selected',
  'bdr_batch','bdr_sync_current','bdr_list','eca_sync','eca_list','net_list',
  'verdict_scrape','verdict_download','refresh_judges'];
const PORTAL_JOB_LABEL = {net:'נט המשפט', bdr:'בית הדין הרבני', eca:'הוצאה לפועל'};
// verdict jobs and judge refresh use NET browser
function _kindToPortal(kind){
  if(['verdict_scrape','verdict_download','refresh_judges'].includes(kind)) return 'NET';
  return (kind||'').split('_')[0].toUpperCase();
}
async function portalBusy(forPortal){
  try{
    const jobs = await (await fetch('/api/jobs?limit=25')).json();
    const running = (jobs||[]).filter(j=>['RUNNING','PENDING'].includes(j.state)
                                       && PORTAL_JOB_KINDS.includes(j.kind));
    if(forPortal){
      const mine = running.find(j=>_kindToPortal(j.kind)===forPortal);
      return mine ? (PORTAL_JOB_LABEL[_kindToPortal(mine.kind).toLowerCase()]||mine.kind) : '';
    }
    const first = running[0];
    return first ? (PORTAL_JOB_LABEL[_kindToPortal(first.kind).toLowerCase()]||first.kind) : '';
  }catch(_){ return ''; }
}
async function ensureNoPortalRunning(portal){
  const busy = await portalBusy();
  if(busy){
    toast(`כרגע רצה פעולה ב${busy} — המתן לסיום ואז נסה שוב.`, true);
    return false;
  }
  return true;
}

/* ── visual lock ───────────────────────────────────────────────────────────
   Refusing the click was correct but invisible: the other portals still LOOKED
   clickable. While one portal runs, the other two are greyed out and disabled,
   and a banner names what is running. Polls the same /api/jobs the guard uses,
   so the button state can never disagree with the engine's lock. */
let _portalLockBusy='';
function _applyPortalLock(jobs){
  const bar=$('sync-platforms'); if(!bar) return;
  let runningPortals=new Set();
  (jobs||[]).filter(x=>['RUNNING','PENDING'].includes(x.state) && PORTAL_JOB_KINDS.includes(x.kind))
            .forEach(j=>runningPortals.add(_kindToPortal(j.kind)));
  _portalLockBusy = [...runningPortals].join(',');
  const note=$('portal-lock-note');
  const anyBusy = runningPortals.size > 0;
  ['NET','BDR','ECA'].forEach(p=>{
    const b=$('plat-'+p); if(!b) return;
    const blocked = anyBusy && !runningPortals.has(p);
    const running = runningPortals.has(p);
    b.disabled = anyBusy;
    b.style.opacity       = blocked ? '.45' : '';
    b.style.filter        = blocked ? 'grayscale(1)' : '';
    b.style.pointerEvents = blocked ? 'none' : '';
    b.style.cursor        = anyBusy && !running ? 'not-allowed' : '';
    b.title = blocked ? `פעולה רצה כעת — המתן לסיום` : '';
  });
  _renderSyncQueue(jobs);
  if(note){
    if(anyBusy){
      const labels = [...runningPortals].map(p=>PORTAL_LABELS[p]||p).join(' + ');
      note.style.display='block';
      note.innerHTML = `🔒 <b>${labels}</b> פועל כעת.`
        + `&ensp;<button onclick="toggleRealBrowser()" `
        + `style="font-size:12px;padding:3px 10px;border-radius:8px;cursor:pointer;`
        + `background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.35);color:inherit">`
        + (_realBrowserVisible?'🙈 הסתר דפדפן':'🖥 הצג דפדפן')+'</button>';
    } else note.style.display='none';
  }
}
// Kept for legacy call-sites; now a no-op since the unified poller drives the lock
function startPortalLockWatch(){ _pollState(); }

/* ── sync queue panel ─────────────────────────────────────────────────────── */
function _renderSyncQueue(jobs){
  const el = $('sync-queue-panel'); if(!el) return;
  const active = (jobs||[]).filter(j=>['RUNNING','PENDING'].includes(j.state)
                                    && PORTAL_JOB_KINDS.includes(j.kind));
  if(!active.length){ el.innerHTML=''; return; }
  el.innerHTML = active.map(j=>{
    const pct = Math.round((j.progress||0)*100);
    const portal = _kindToPortal(j.kind);
    const label = JOB_ICONS[j.kind]||'⚙' + ' ' + (JOB_LABELS[j.kind]||j.kind);
    const canStop = ['net_smart_download','net_download_all','bdr_batch','eca_sync',
                     'verdict_scrape','verdict_download'].includes(j.kind);
    return `<div style="background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.3);
      border-radius:10px;padding:8px 12px;margin-bottom:6px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span style="font-size:12px;font-weight:700;flex:1">${PORTAL_LABELS[portal]||portal} — ${JOB_LABELS[j.kind]||j.kind}</span>
        <span style="font-size:11px;opacity:.7">${j.state==='PENDING'?'ממתין':pct+'%'}</span>
        ${canStop?`<button onclick="stopJob(${j.job_id})"
          style="padding:2px 8px;border-radius:5px;border:1px solid rgba(255,80,80,.5);
          background:transparent;color:#ff5050;font-size:11px;cursor:pointer">⏹ עצור</button>`:''}
      </div>
      <div style="height:5px;background:rgba(255,255,255,.15);border-radius:3px">
        <div style="height:100%;width:${pct}%;background:var(--accent);border-radius:3px;transition:width .4s"></div>
      </div>
      ${j.message?`<div style="font-size:11px;opacity:.7;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${j.message}</div>`:''}
    </div>`;
  }).join('');
}

/* ─── unified state poller ─────────────────────────────────────────────────
   Replaces three separate setIntervals (log/3s, tasks/2.5s, portal-lock/3s)
   with a single 2-second poll that pauses when the tab is hidden. */
let _stateTimer=null, _engineState={tasks:[], log_tail:[]};
document.addEventListener('visibilitychange', ()=>{
  if(document.visibilityState==='visible') _pollState();
});
function _startStatePoller(){
  clearInterval(_stateTimer);
  _pollState();
  _stateTimer = setInterval(_pollState, 2000);
}
async function _pollState(){
  if(document.hidden) return;
  try{
    const r = await fetch('/api/engine/state');
    if(!r.ok) return;
    _engineState = await r.json();
  }catch(_){ return; }
  if($('taskswin-body')) _renderTasksUI(_engineState.tasks);
  if($('logwin-body') && _logTab!=='events') _renderLogUI(_engineState.log_tail);
  _applyPortalLock(_engineState.tasks);
}
function _renderLogUI(lines){
  const el=$('logwin-body'); if(!el) return;
  const atBottom = el.scrollTop+el.clientHeight >= el.scrollHeight-40;
  el.style.direction='ltr'; el.style.textAlign='left';
  const levelSel = $('lg-level');
  const minLevel = levelSel?.value || 'INFO';
  const LEVELS = ['DEBUG','INFO','WARN','ERROR'];
  const minIdx = LEVELS.indexOf(minLevel);
  let filtered = (lines||[]).filter(l=>{
    // Hide HTTP access log noise
    if(/^\[\d{2}:\d{2}:\d{2}\].*\[INFO\].*"(GET|POST) \/api\/(log|health|settings|events|engine\/state)/.test(l)) return false;
    if(/^INFO:\s+127\.0\.0\.1.*"(GET|POST|PUT|DELETE|OPTIONS) \/api\/(log|health|settings|events)/.test(l)) return false;
    if(minLevel==='ALL') return true;
    // Filter by level
    const m = l.match(/\[(DEBUG|INFO|WARN|ERROR)\]/);
    if(m) return LEVELS.indexOf(m[1]) >= minIdx;
    return minIdx <= 1; // lines without level tag — show at INFO+
  });
  if(_logTab==='drive'){
    filtered = filtered.filter(l=>/drive|google|upload|gdrive|cloud/i.test(l));
  } else if(_logTab==='transcription'){
    filtered = filtered.filter(l=>/transcri|whisper|תמלול|הקלטה|audio|speech/i.test(l));
  }
  const esc = t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const paint = l=>{
    const e=esc(l);
    if(/\[ERROR\]|✗|שגיא|Traceback|failed|FAILED|Exception/.test(l))
      return `<span style="color:#ff8a80;display:block">${e}</span>`;
    if(/\[WARN\]|⚠|warn/.test(l)) return `<span style="color:#ffd54f">${e}</span>`;
    if(/✓|הושלם|הצליח|COMPLETED|\[OK\]/.test(l))
      return `<span style="color:#69f0ae">${e}</span>`;
    if(/\[DEBUG\]/.test(l)) return `<span style="opacity:.5">${e}</span>`;
    return e;
  };
  const html = filtered.slice(-500).map(paint).join('\n')
    || (_logTab==='drive'?'אין לוגים של Drive':_logTab==='transcription'?'אין לוגים של תמלול':'הלוג ריק');
  el.innerHTML = html;
  window._logText = filtered.join('\n');
  if(atBottom) el.scrollTop = el.scrollHeight;
}
function _renderTasksUI(jobs){
  const el=$('taskswin-body'); if(!el) return;
  const active = (jobs||[]).filter(j=>['RUNNING','PENDING'].includes(j.state));
  const recent = (jobs||[]).filter(j=>!['RUNNING','PENDING'].includes(j.state)).slice(0,5);
  window._activeJobs = active;
  const activePortals = Object.keys(_dlByPortal);
  const CANCELLABLE = ['net_smart_download','net_download_all','bdr_batch','eca_sync','verdict_scrape','verdict_download'];
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
    activePortals.map(p=>{const st=_dlByPortal[p];const docs=st.docs_downloaded||0;
      const cases = st.cases_detail || [];
      const casesHtml = cases.length ? `
        <div style="margin-top:6px;max-height:180px;overflow-y:auto;font-weight:400">
        ${cases.map(c=>{
          const s=c.status||'pending';
          const icon={done:'✓',downloading:'⏳',failed:'✗',skipped:'⏭'}[s]||'·';
          const canStop = (s==='pending'||s==='downloading');
          return `<div style="display:flex;align-items:center;gap:6px;padding:3px 0;
                   font-size:12px;border-top:1px solid rgba(127,127,127,.15);
                   opacity:${s==='done'?'.6':'1'}">
            <span style="width:14px">${icon}</span>
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
                  white-space:nowrap"><b>${c.id}</b> ${c.name||''}</span>
            ${canStop ? `<button class="fv-btn" title="עצור רק את התיק הזה"
                 style="font-size:10.5px;padding:2px 8px;color:var(--danger)"
                 onclick="stopCase('${p}','${c.id}')">⏹ תיק</button>` : ''}
          </div>`;}).join('')}
        </div>` : '';
      return `<div style="padding:8px 12px;margin-bottom:8px;border-radius:10px;background:var(--accent-soft,#eef4ff);font-weight:600">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="flex:1">⬇ <b>${PORTAL_LABELS[p]||p}</b>: תיק ${st.done||0}/${st.total||0} · ${docs} מסמכים${st.speed_per_min?` · ${st.speed_per_min}/דקה`:''} ${st.failed?` · <span style="color:var(--danger)">${st.failed} כשלו</span>`:''}</span>
        <button class="fv-btn" style="font-size:10.5px;padding:3px 9px;color:var(--danger)"
                onclick="stopPortalDownload('${p}')" title="עצור את כל ההורדה של פורטל זה">⏹ פורטל</button>
      </div>
      ${casesHtml}
    </div>`;}).join('')
    + (active.length? '<div style="font-weight:800;margin:4px 0;color:var(--accent-strong,#1d64d8)">רץ עכשיו / בהמתנה</div>'+active.map(row).join('')
                    : '<div class="empty" style="padding:16px 0">אין משימות פעילות כרגע</div>')
    + (recent.length? '<div style="font-weight:800;margin:12px 0 4px;opacity:.7">הסתיימו לאחרונה</div>'+recent.map(row).join('') : '');
}

async function act(path, label){
  if(!D?.live){
    await startEngine();
  }
  logEvent('→ '+(label||path));
  try{
    const r = await fetch('/api/proxy/actions/'+path, {method:'POST'});
    if(r.ok){
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
function _showDoneBanner(msg){
  // Prominent "session finished" banner — auto-dismisses after 8s
  let b=document.getElementById('done-banner');
  if(b) b.remove();
  b=document.createElement('div'); b.id='done-banner';
  b.style.cssText='position:fixed;top:16px;left:50%;transform:translateX(-50%);'
    +'z-index:9999;background:#1a7a4a;color:#fff;padding:14px 28px;border-radius:14px;'
    +'font-size:16px;font-weight:700;box-shadow:0 4px 24px rgba(0,0,0,.4);'
    +'display:flex;align-items:center;gap:12px;max-width:90vw;cursor:pointer';
  b.innerHTML=`<span style="font-size:22px">✅</span><span>${msg}</span>`
    +`<button onclick="document.getElementById('done-banner')?.remove()" `
    +`style="background:rgba(255,255,255,.25);border:none;color:#fff;border-radius:8px;`
    +`padding:2px 10px;cursor:pointer;margin-right:4px;font-size:13px">✕</button>`;
  b.onclick=e=>{ if(e.target.tagName!=='BUTTON') b.remove(); };
  document.body.appendChild(b);
  setTimeout(()=>b.isConnected&&b.remove(), 8000);
  try{ new Audio('data:audio/wav;base64,UklGRl9vT19XQVZFZm10IBAAAA==').play(); }catch(_){}
}
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
    if(e.type==='bdr_cases'){ showBdrCases(e.cases||[]); }
    if(e.type==='download_stats'){
      const portal=e.portal||'NET';
      _dlByPortal[portal]=e; _saveDl();
      if(e.job_id && !_netDownloadJobId){ _netDownloadJobId=e.job_id; try{sessionStorage.setItem('dlJobId',e.job_id);}catch(_){} }
      if(!_dlStatsTimer) _dlStatsTimer=setTimeout(()=>{ _dlStatsTimer=null; _renderDlStats(); }, 500);
    }
    if(e.type==='case_imported'){
      logEvent(`✓ ${e.case}: ${e.docs} מסמכים נוספו לדשבורד`);
      _serverDlAt = 0;
      if(!_importBatchTimer) _importBatchTimer=setTimeout(()=>{
        _importBatchTimer=null;
        refreshDownloadedSet(true).then(()=>{ _renderDlStats(); });
        if(typeof refresh==='function') refresh(true);
      }, 2000);
    }
    if(e.type==='portal_done'){
      const msg = e.message||`הורדת ${e.label||''} הסתיימה ✓`;
      logEvent(`✅ ${msg}`);
      _showDoneBanner(msg);
    }
    if(e.type==='job'){
      $('otp-box')?.remove();
      if(e.message) logEvent(`#${e.job_id||''} ${e.message}`);
      if(typeof e.progress==='number')
        showJobBar(JOB_LABELS[e.kind]||`משימה #${e.job_id||''}`, e.progress, e.message||'');
      if(e.state==='COMPLETED'||e.state==='ERROR'){
        $('otp-box')?.remove();
        // Clear the finished portal's live stats (per-portal, so a parallel
        // portal's panel stays intact).
        const KIND_PORTAL={net_smart_download:'NET',net_download_all:'NET',net_date_search:'NET',
                           bdr_batch:'BDR',bdr_sync_current:'BDR',eca_sync:'ECA'};
        const donePortal=KIND_PORTAL[e.kind];
        if(donePortal){
          delete _dlByPortal[donePortal]; _saveDl();
          if(!Object.keys(_dlByPortal).length){ _netDownloadJobId=null; try{sessionStorage.removeItem('dlJobId');}catch(_){} }
          _renderDlStats();
        }
        logEvent(`${JOB_LABELS[e.kind]||e.kind||'משימה'} — ${e.state}`);
        finishJobBar(e.state==='COMPLETED', e.error||'');
        toast(`${JOB_LABELS[e.kind]||e.kind||'משימה'} — ${e.state==='COMPLETED'?'הושלמה ✓':'שגיאה'}`, e.state==='ERROR');
        // ECA list done — fetch the cases directly (don't rely on the one-shot
        // 'eca_cases' broadcast, which can be missed during the long login).
        if(e.kind==='eca_list' && e.state==='COMPLETED'){
          fetch('/api/proxy/eca/cases').then(r=>r.json())
            .then(d=>{ if(d.cases) showEcaCases(d.cases); })
            .catch(()=>{});
        }
        if(e.kind==='bdr_list' && e.state==='COMPLETED'){
          fetch('/api/proxy/bdr/cases').then(r=>r.json())
            .then(d=>{ if(d.cases) showBdrCases(d.cases); })
            .catch(()=>{});
        }
        // Login failure → offer a one-click retry (refresh + try again)
        if(e.state==='ERROR' && /התחברות|login|gov\.il|נכשל|OTP|אימות/i.test((e.error||'')+(e.message||''))
           && ['eca_sync','eca_list','bdr_batch','bdr_list','net_smart_download','net_download_all','net_list_cases','open_portal'].includes(e.kind)){
          showLoginRetry(e.kind, e.error||e.message||'ההתחברות נכשלה');
        }
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
let _logTab='engine';
function toggleLogWin(){
  let w=$('logwin');
  if(w){ w.remove(); return; }
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
      <select id="lg-level" onchange="_pollState()" title="סינון לפי רמה"
        style="font-size:10.5px;border:1px solid #2a352c;background:#0E1B29;color:#c8e6c9;
               border-radius:6px;padding:2px 4px;cursor:pointer">
        <option value="INFO">INFO+</option>
        <option value="WARN">WARN+</option>
        <option value="ERROR">ERROR</option>
        <option value="ALL">הכל</option>
      </select>
      <button class="fv-btn" style="border-color:#2a352c;color:#c8e6c9" onclick="copyLog()" title="העתק את הלוג ללוח">📋</button>
      <button class="fv-btn" style="border-color:#2a352c;color:#c8e6c9" onclick="downloadLog()" title="הורד כקובץ טקסט">⬇</button>
      <button class="fv-btn" style="border-color:#2a352c;color:#c8e6c9" onclick="maximizeLogWin()" title="הגדל/הקטן">⛶</button>
    </div>
    <div id="logwin-body" style="flex:1;overflow-y:auto;padding:8px 12px;font-size:11.5px;
      font-family:ui-monospace,monospace;direction:ltr;text-align:left;line-height:1.65;white-space:pre-wrap"></div>`;
  document.body.appendChild(w);
  _makeDraggable('logwin','logwin-top');
  setLogTab('engine');
  _pollState();
}
function setLogTab(t){
  _logTab=t;
  document.querySelectorAll('.log-tab-btn').forEach(b=>b.style.background='transparent');
  const active=$('lg-t-'+t); if(active) active.style.background='rgba(255,255,255,.15)';
  if(t==='events'){
    const el=$('logwin-body'); if(!el) return;
    el.style.direction='rtl'; el.style.textAlign='right';
    el.innerHTML = _logBuf.map(l=>`<div>${l}</div>`).join('') || '<div style="color:#7a8a7d">אין אירועים עדיין</div>';
  } else {
    _renderLogUI(_engineState.log_tail);
  }
}

/* ─── tasks balloon: what runs now, what waits, rate, per-job stop ─── */
function toggleTasksWin(forceOpen){
  let w=$('taskswin');
  if(w && !forceOpen){ w.remove();
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
  _pollState();
}
function stopAllDownloads(){
  // Immediate visual feedback
  const btn = document.querySelector('[onclick="stopAllDownloads()"]');
  if(btn){ btn.textContent='⏳ עוצר…'; btn.disabled=true; }
  (window._activeJobs||[]).forEach(j=>{
    if(['net_smart_download','net_download_all','bdr_batch','eca_sync'].includes(j.kind))
      fetch('/api/proxy/actions/cancel_download?job_id='+j.job_id,{method:'POST'});
  });
  toast('הוראת עצירה נשלחה — עוצר תוך שנייה-שתיים');
}
function stopJob(jobId){
  fetch('/api/proxy/actions/cancel_download?job_id='+jobId,{method:'POST'});
  toast('נשלחה הוראת עצירה — התיק הנוכחי יסתיים ואז ייעצר');
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

/* ─── debug-phase feedback widget (💬) — writes to user_feedback.log ─── */
function feedbackEnabled(){ return localStorage.getItem('lias_feedback')!=='0'; }
function ensureFeedback(){
  $('fb-note-btn')?.remove();
  if(!feedbackEnabled()) return;
  const b=document.createElement('button');
  b.id='fb-note-btn'; b.textContent='💬';
  b.title='הערה למפתח — נרשמת ליומן ייעודי לשיפור המערכת';
  b.style.cssText='position:fixed;bottom:16px;right:16px;z-index:117;width:40px;height:40px;'
    +'border-radius:50%;background:var(--warn,#F5A623);color:#fff;font-size:18px;'
    +'box-shadow:0 6px 20px rgba(0,0,0,.25);border:none;cursor:pointer';
  b.onclick=()=>{
    $('fb-box')?.remove();
    const d=document.createElement('div'); d.id='fb-box';
    d.style.cssText='position:fixed;bottom:66px;right:16px;z-index:130;width:min(340px,90vw);'
      +'background:var(--surface,#fff);border:1px solid var(--line);border-radius:12px;'
      +'padding:14px;box-shadow:0 12px 40px rgba(0,0,0,.3);direction:rtl';
    d.innerHTML=`<b style="font-size:13px">💬 הערה על המסך הנוכחי</b>
      <textarea id="fb-text" rows="3" placeholder="מה לא עובד / מה כדאי לשפר…"
        style="width:100%;margin-top:8px;border:1px solid var(--line);border-radius:8px;padding:8px;font-size:13px;resize:vertical"></textarea>
      <div style="display:flex;gap:6px;margin-top:8px">
        <button class="btn-accent" style="flex:1;padding:8px" onclick="sendFeedback()">שלח</button>
        <button class="fv-btn" onclick="$('fb-box').remove()">בטל</button></div>`;
    document.body.appendChild(d); $('fb-text').focus();
  };
  document.body.appendChild(b);
}
async function sendFeedback(){
  const note=($('fb-text')?.value||'').trim();
  if(!note){ toast('ההערה ריקה', true); return; }
  try{
    await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({page:(location.hash||'#home'), note})});
    toast('ההערה נרשמה — תודה! 💬'); $('fb-box').remove();
  }catch(e){ toast('שגיאה בשליחה', true); }
}
document.addEventListener('DOMContentLoaded', ensureFeedback);

/* ─── real automation browser — show/hide ─── */
let _realBrowserVisible=false;
/* BDR: connect → list cases (with both parties) → pick → download.
   IDENTICAL flow to NET and ECA — no client-name prompt, nothing configurable
   on this screen (scope + user-mode come from Settings ⚙). */
async function bdrConnectAndList(){
  if(!await ensureNoPortalRunning('BDR')) return;
  toast('מתחבר לבית הדין הרבני ושולף תיקים…');
  logEvent('→ התחברות והצגת תיקי בד"ר');
  const picker=$('sync-case-picker');
  if(picker){ picker.style.display='block';
    picker.innerHTML='<div style="padding:10px;color:rgba(255,255,255,.7)">מתחבר לבית הדין הרבני… (ייתכן שיידרש קוד אימות)</div>'; }
  act('bdr_list','חיבור והצגת תיקי בד"ר');
}
async function runBdrBatch(client_filter, cases, sub_cases){
  if(!await ensureNoPortalRunning('BDR')) return;
  client_filter = client_filter || '';
  const n = (cases||[]).length, m = (sub_cases||[]).length;
  toast(n||m ? `מוריד ${n?n+' תיקים':''}${n&&m?' · ':''}${m?m+' תת-תיקים':''}…`
          : (client_filter ? `מוריד תיקי בד"ר של "${client_filter}"…`
                           : 'מתחבר לבית הדין הרבני ומוריד את כל התיקים…'));
  logEvent('→ הורדת תיקי BDR' + (n||m? ` (${n} תיקים, ${m} תת-תיקים)` : (client_filter? ' — '+client_filter : ' (הכל)')));
  const _bdrOpenFilter = _currentScope==='open'||_currentScope==='open_client' ? _currentScope : 'all';
  fetch('/api/proxy/actions/bdr_batch', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({client_filter, cases: cases||[], sub_cases: sub_cases||[], open_filter: _bdrOpenFilter})
  }).then(r=>{
    if(r.ok) toast('הורדת BDR הופעלה ✓');
    else toast('שגיאה בהפעלת BDR', true);
  }).catch(e=>toast('שגיאה: '+e.message, true));
}
/* ECA: connect → list cases (with parties) → pick with checkboxes → download */
let _ecaCases = [];
try{ const _s=localStorage.getItem('ecaCasesAll'); if(_s) _ecaCases=JSON.parse(_s)||[]; }catch(_){}
/* cases already sent to download — shown as ✓ הורד with the box cleared */
/* "Already downloaded" is decided by the SERVER, not by the browser.
   These sets used to be persisted in localStorage, so after reset_all.sh wiped
   every document the picker still showed the first case as "✓ הורד" — stale
   client state outliving the data it described. They are now session-only
   optimistic marks ("just sent to download"); the authoritative answer comes
   from /api/cases/all, which reads the documents actually on disk. */
let _ecaHandled = new Set();          // sent during THIS session only
function _saveEcaHandled(){ /* intentionally not persisted — see above */ }
let _serverDownloaded = new Set(), _serverDlAt = 0;
async function refreshDownloadedSet(force){
  if(!force && Date.now()-_serverDlAt < 8000) return _serverDownloaded;
  try{
    const d = await (await fetch('/api/proxy/cases/all')).json();
    _serverDownloaded = new Set((d.cases||[])
      .filter(c=>c.downloaded).map(c=>String(c.number)));
    _serverDlAt = Date.now();
    for(const s of [_ecaHandled, _bdrHandled]){
      for(const n of [...s]) if(!_serverDownloaded.has(String(n))) s.delete(n);
    }
  }catch(_){}
  return _serverDownloaded;
}
function _isDownloaded(num, sessionSet){
  const n=String(num);
  return _serverDownloaded.has(n) || (sessionSet && sessionSet.has(num));
}
async function ecaConnectAndList(){
  if(!await ensureNoPortalRunning('ECA')) return;
  toast('מתחבר להוצאה לפועל ושולף תיקים…');
  logEvent('→ התחברות והצגת תיקי הוצל"פ');
  const picker=$('sync-case-picker');
  if(picker){ picker.style.display='block';
    picker.innerHTML='<div style="padding:10px;color:rgba(255,255,255,.7)">מתחבר לפורטל ההוצאה לפועל… (ייתכן שיידרש קוד אימות)</div>'; }
  act('eca_list','חיבור והצגת תיקי הוצל"פ');
}
function _ecaPartiesLine(c){
  // Always show WHO the parties are and WHICH side is זוכה / חייב.
  // Prefer the full parties list (both sides) from גורמים בתיק; otherwise use
  // the card's role + name, labelling both sides explicitly.
  if(Array.isArray(c.parties) && c.parties.length){
    return c.parties.map(p=>`<span style="opacity:.7">${p.role||'צד'}:</span> <b>${p.name||''}</b>`)
                    .join(' &nbsp;•&nbsp; ');
  }
  const role=(c.role||'').trim(), name=(c.party||'').trim();
  if(!role && !name) return '<span style="opacity:.6">אין פרטי צדדים — הרץ סנכרון לעדכון</span>';
  // On the ECA card, `role` is YOUR side in this case and `name` is the party
  // shown on it; the opposite side is derived so both are always visible.
  const OPP={'זוכה':'חייב','חייב':'זוכה'};
  const other=OPP[role]||'הצד שכנגד';
  return `<span style="opacity:.7">מעמדך:</span> <b>${role||'—'}</b>`
       + ` &nbsp;•&nbsp; <span style="opacity:.7">${other}:</span> <b>${name||'—'}</b>`;
}
function showEcaCases(cases){
  if(_syncPlatform !== 'ECA') return;  // don't overwrite another portal's picker
  // ask the server what is really downloaded, then repaint with the truth
  if(!showEcaCases._busy){ showEcaCases._busy=1;
    refreshDownloadedSet().then(()=>{ showEcaCases._busy=0; showEcaCases([]); }); }
  // Cumulative + sorted (open on top, newest first), like NET.
  _ecaCases = _mergeCasesList(_ecaCases, cases||[]);
  try{ localStorage.setItem('ecaCasesAll', JSON.stringify(_ecaCases)); }catch(_){}
  const picker=$('sync-case-picker'); if(!picker) return;
  if(!_ecaCases.length){ picker.style.display='none'; toast('לא נמצאו תיקי הוצל"פ', true); return; }
  // In "download all" mode — hide picker entirely; the download button is the UI
  if(_syncScopeChoice==='all'){ picker.style.display='none'; return; }
  // Remember ticks before re-render so a refreshDownloadedSet() callback
  // doesn't wipe the user's selection.
  const _wasChecked = new Set(
    [...document.querySelectorAll('.eca-cb:checked')].map(cb=>cb.dataset.i));
  const _hadPicker = picker.innerHTML.includes('eca-cb');
  // NOTE: the checkboxes carry data-i indexes into _ecaCases, so _ecaCases must
  // BE the rendered (filtered) list — otherwise a filtered picker downloads the
  // wrong cases. The unfiltered set stays in localStorage for the next open.
  const _all=_sortCasesForPicker(_ecaCases);
  const list=_applyOpenFilter(_all); _ecaCases=list;
  if(!list.length){ picker.style.display='block';
    picker.innerHTML='<div style="padding:10px;color:rgba(255,255,255,.7)">כל תיקי ההוצל"פ '
      +'סוננו לפי מצב התיק. שנה את הסינון בהגדרות ⚙ → הורדות.</div>'; return; }
  picker.style.display='block';
  picker.innerHTML = `<div style="font-size:13px;color:rgba(255,255,255,.7);margin-bottom:6px">
      נמצאו ${list.length} תיקי הוצאה לפועל — סמן מה להוריד${_openFilterNote(list.length,_all.length)}</div>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllEca(true)">סמן הכל</button>
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllEca(false)">נקה הכל</button>
    </div>
    ${_pickerSearchBox('eca-q','🔎 סינון: מספר תיק / צד / סוג…')}
    <div style="max-height:320px;overflow-y:auto;display:flex;flex-direction:column;gap:4px">
      ${list.map((c,i)=>{
        const done=_isDownloaded(c.number,_ecaHandled);
        return `<label data-searchrow="eca-q" data-hay="${_rowHay(c)}"
          style="display:flex;gap:8px;align-items:center;padding:7px 10px;border:1px solid rgba(255,255,255,.12);border-radius:8px;cursor:pointer${done?';opacity:.72':''}">
        <input type="checkbox" class="eca-cb" data-i="${i}" ${done?'':'checked'}>
        <div style="flex:1;min-width:0">
          <div style="font-weight:700;font-size:13px">${c.number}
            <span style="font-weight:400;opacity:.7">· ${c.type||''}</span>
            ${_statusChip(c.status||c.CaseStatusName, c.close_date, c.open_date)}
            ${done?' <span style="font-size:10px;color:var(--accent-strong,#7fb3ff)">· ✓ הורד</span>':''}</div>
          <div style="font-size:11.5px;opacity:.85;line-height:1.5">${_ecaPartiesLine(c)}</div>
        </div>
        ${done?`<button title="פתח את התיק במערכת" onclick="event.preventDefault();event.stopPropagation();_goToCaseByNumber('${c.number}')" style="background:none;border:none;cursor:pointer;color:var(--accent-strong,#7fb3ff);font-size:13px;padding:0 4px">↗</button>`:''}
      </label>`;}).join('')}
    </div>
    <button class="btn-accent" style="width:100%;margin-top:10px;padding:12px" onclick="runEcaSelected()">⬇ הורד את המסומנים</button>`;
  // restore ticks after re-render (only when picker already existed — first
  // render keeps its default of "everything not yet downloaded")
  if(_hadPicker){
    document.querySelectorAll('.eca-cb').forEach(cb=>{ cb.checked=_wasChecked.has(cb.dataset.i); });
  }
}
function _selectAllEca(v){ document.querySelectorAll('.eca-cb').forEach(cb=>cb.checked=v); }
async function runEcaDryRun(){
  const picked=[...(document.querySelectorAll('.eca-cb:checked'))].map(cb=>_ecaCases[+cb.dataset.i]?.number).filter(Boolean);
  const caseNum = picked[0] || (_ecaCases[0]?.number) || '';
  if(!caseNum){ toast('לא נמצא תיק להרצה יבשה', true); return; }
  toast(`🧪 הרצה יבשה לתיק ${caseNum}…`);
  logEvent(`→ הרצה יבשה: ${caseNum}`);
  fetch('/api/proxy/actions/eca_dry_run',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({case: caseNum, limit:5})
  }).then(r=>r.ok ? toast(`הרצה יבשה הופעלה ✓ — בדוק את לוג הפעילות`) : toast('שגיאה', true))
    .catch(e=>toast('שגיאה: '+e.message, true));
}
async function runEcaSelected(){
  const picked = [...document.querySelectorAll('.eca-cb:checked')].map(cb=>_ecaCases[+cb.dataset.i].number);
  if(!picked.length){ toast('לא סומן אף תיק', true); return; }
  if(!await ensureNoPortalRunning('ECA')) return;
  const all = picked.length===_ecaCases.length;
  toast(all?'מוריד את כל תיקי ההוצל"פ…':`מוריד ${picked.length} תיקי הוצל"פ…`);
  logEvent('→ הורדת הוצל"פ ('+(all?'הכל':picked.join(', '))+')');
  // Remember what was sent to download so a later re-render shows them as
  // handled (✓ הורד) with the checkbox CLEARED — instead of re-appearing
  // "already selected" as if a new selection was made.
  picked.forEach(n=>_ecaHandled.add(n));
  _saveEcaHandled();
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
  try{
    // Sync with actual server state before toggling to avoid double-open
    const status = await fetch('/api/browser/status').then(r=>r.json()).catch(()=>({}));
    // headless=true means browser is hidden; headless=false or window visible means shown
    const currentlyVisible = status.available && !status.headless;
    _realBrowserVisible = currentlyVisible;
    const path = _realBrowserVisible ? 'browser/hide' : 'browser/show';
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
let _shotTimer=null, _shotBlobUrl=null;
function toggleBrowserWin(){
  let w=$('bwin');
  if(w){ clearInterval(_shotTimer); _shotTimer=null;
    if(_shotBlobUrl){ URL.revokeObjectURL(_shotBlobUrl); _shotBlobUrl=null; }
    w.remove(); return; }
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
    if(document.hidden) return;
    try{
      const r = await fetch('/api/browser/screenshot?t='+Date.now());
      if(!r.ok) throw 0;
      const blob = await r.blob();
      if(_shotBlobUrl) URL.revokeObjectURL(_shotBlobUrl);
      _shotBlobUrl = URL.createObjectURL(blob);
      $('bwin-body').innerHTML = `<img style="width:100%;display:block" src="${_shotBlobUrl}">`;
      hasFrame=true;
      setState('🟢 תצוגה חיה');
    }catch(e){
      setState('⏳ הדפדפן עסוק — התצוגה תתעדכן בעוד רגע');
    }
  };
  tick(); _shotTimer = setInterval(tick, 5000);
}

/* ─── sync card ─── */
let _currentScope = 'all';
/* Platform-first sync: pick a portal, THEN see only its relevant options.
   The three portals are independent — NET/BDR/ECA never share a flow. */
let _syncPlatform = null;
try{ _syncPlatform = localStorage.getItem('lias_last_portal') || null; }catch(_){}
function syncCard(el){
  const isLawyer = (curUser()?.role||'') === 'LAWYER';
  el.innerHTML = `
    <!-- Task queue balloon -->
    <div id="sync-queue-panel" style="margin-bottom:14px"></div>

    <div style="display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap">
      <!-- Portal tabs -->
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="sync-platforms">
        <button class="sync-plat" id="plat-NET" onclick="pickPlatform('NET')">🏛<div>נט המשפט</div></button>
        <button class="sync-plat" id="plat-BDR" onclick="pickPlatform('BDR')">🕍<div>בית הדין הרבני</div></button>
        <button class="sync-plat" id="plat-ECA" onclick="pickPlatform('ECA')">⚖️<div>הוצאה לפועל</div></button>
      </div>
      <!-- Global options -->
      <div style="display:flex;flex-direction:column;gap:7px;font-size:12.5px;padding-top:4px">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="opt-open-only" style="width:auto"
            onchange="_syncOptChanged()"> רק תיקים פתוחים
        </label>
        ${isLawyer ? `<label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="opt-open-client" style="width:auto"
            onchange="_syncOptChanged()"> לקוחות עם תיק פתוח
        </label>` : ''}
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;opacity:.8">
          <input type="checkbox" id="sync-browser-visible" style="width:auto"
            onchange="_saveBrowserVisible(this.checked)"> הצג דפדפן
        </label>
      </div>
    </div>

    <!-- Download all portals serially -->
    <button onclick="downloadAll()"
      style="margin-top:10px;width:100%;padding:8px 14px;border-radius:10px;
      border:1px solid rgba(255,255,255,.22);background:transparent;color:inherit;
      font-size:12.5px;font-weight:600;cursor:pointer;text-align:right">
      ⬇ הורד כל הפורטלים (נט → בד"ר → הוצל"פ)
    </button>
    <div id="download-all-status" style="font-size:11.5px;margin-top:4px;color:var(--ink-soft)"></div>

    <div id="portal-lock-note" style="display:none;margin-top:10px;padding:8px 12px;
         border-radius:10px;background:rgba(230,168,0,.14);border:1px solid rgba(230,168,0,.5);
         font-size:12.5px;font-weight:600"></div>

    <div id="dl-stats-panel" style="display:none;margin-top:8px"></div>
    <div id="sync-options" style="margin-top:12px"></div>
    <div id="sync-case-picker" style="display:none;margin-top:12px"></div>
    <div id="download-all-status" style="font-size:12px;margin-top:5px;color:var(--ink-soft)"></div>
`;
  fetch('/api/settings').then(r=>r.json()).then(st=>{
    window._settings = st;
    _currentScope = st.case_scope || 'all';
    const bvCb = $('sync-browser-visible');
    if(bvCb) bvCb.checked = st.browser_visible !== false;
    // Init checkboxes from saved state
    const openOnlyCb = $('opt-open-only');
    if(openOnlyCb) openOnlyCb.checked = (_syncAllMode==='open' || _syncAllMode==='open_client');
    const openClientCb = $('opt-open-client');
    if(openClientCb) openClientCb.checked = _syncAllMode==='open_client';
    if(_syncPlatform) pickPlatform(_syncPlatform);
  }).catch(()=>{});
  if(_syncPlatform) pickPlatform(_syncPlatform);
  if(Object.keys(_dlByPortal).length) _renderDlStats();
  startPortalLockWatch();
}

function _saveBrowserVisible(checked){
  // Keep both checkboxes (sync panel + settings panel) in sync
  const gs = $('g-browser-visible');
  if(gs) gs.checked = checked;
  // Persist via the settings API (same path as saveSyncSettings)
  fetch('/api/settings').then(r=>r.json()).then(st=>{
    return fetch('/api/settings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({...st, browser_visible: checked})
    });
  }).catch(()=>{});
}

let _sessionImportData = null;
function _onSessionFileChosen(input){
  _sessionImportData = null;
  const st = $('session-import-status');
  if(!input.files || !input.files[0]){ if(st) st.textContent=''; return; }
  const reader = new FileReader();
  reader.onload = e=>{
    try{
      const d = JSON.parse(e.target.result);
      if(!d.portal || !d.storage_state?.cookies?.length)
        throw new Error('קובץ לא תקין — חסר portal או cookies');
      _sessionImportData = d;
      if(st) st.textContent = `✓ פורטל: ${d.portal} · ${d.storage_state.cookies.length} עוגיות`;
    }catch(err){
      if(st) st.textContent = '✗ '+err.message;
    }
  };
  reader.readAsText(input.files[0]);
}

async function _doImportSession(){
  const st = $('session-import-status');
  if(!_sessionImportData){ if(st) st.textContent='בחר קובץ JSON קודם'; return; }
  if(st) st.textContent = 'מייבא…';
  try{
    const r = await fetch('/api/actions/import_session', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(_sessionImportData)
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(d.detail || d.error || r.status);
    if(st) st.textContent = '✓ הסשן יובא — הדפדפן ייפתח לאימות';
  }catch(err){
    if(st) st.textContent = '✗ '+err.message;
  }
}

async function _downloadStaleCases(onlyPortal){
  const st = $('stale-status');
  if(st) st.textContent = 'מחפש תיקים לא-מעודכנים…';
  try{
    const r = await fetch('/api/cases/all');
    const cards = await r.json().catch(()=>[]);
    const _sm = caseStatusMap ? caseStatusMap() : {};
    // Sort: no last_synced first, then oldest first (highest priority)
    const stale = cards
      .filter(c=> _sm[c.sub_case_id]!=='closed')
      .sort((a,b)=>{
        const ta = a.last_synced ? new Date(a.last_synced.replace(' ','T')).getTime() : 0;
        const tb = b.last_synced ? new Date(b.last_synced.replace(' ','T')).getTime() : 0;
        return ta - tb; // oldest first
      });
    if(!stale.length){ if(st) st.textContent = '✓ אין תיקים פתוחים'; return; }
    const byPortal = {};
    stale.forEach(c=>{ (byPortal[c.portal]=byPortal[c.portal]||[]).push(c); });
    // If called from a specific portal button, filter to that portal only
    const portals = onlyPortal ? [onlyPortal] : Object.keys(byPortal);
    if(st) st.textContent = `מריץ עדכון ל-${stale.length} תיקים לפי תאריך עדכון (${portals.join(', ')})…`;
    if(byPortal.BDR && portals.includes('BDR')){
      const caseNums = [...new Set(byPortal.BDR.map(c=>(c.sub_number||'').split(' ')[0]).filter(Boolean))];
      runBdrBatch('', caseNums, []);
    }
    if(byPortal.NET && portals.includes('NET')){
      const ids = byPortal.NET.map(c=>c.sub_case_id);
      fetch('/api/proxy/actions/net_smart_download',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({scope:'selected', sub_case_ids:ids, open_filter:'all'})
      }).catch(()=>{});
    }
    if(byPortal.ECA && portals.includes('ECA')){
      const caseNums = byPortal.ECA.map(c=>c.sub_number||c.case_number||'').filter(Boolean);
      fetch('/api/proxy/actions/eca_sync',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({cases:caseNums})
      }).catch(()=>{});
    }
  }catch(err){
    if(st) st.textContent = '✗ '+err.message;
  }
}

function pickPlatform(p){
  _syncPlatform = p;
  try{ localStorage.setItem('lias_last_portal', p); }catch(_){}
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
  const relNote = p==='NET' && s.net_related ? ' · כולל תיקים קשורים' : '';
  if(!_syncScopeChoice) _syncScopeChoice = scope==='all' ? 'all' : 'selected';
  box.innerHTML = _renderScopeChooser(p, label, relNote);
  _applyScopeChoice();
  // Restore cached case list when switching tabs — throttled to once per 30s
  // to avoid duplicate fetches when pickPlatform is called multiple times on load.
  const _now = Date.now();
  if(p==='ECA' && scope==='selected' && _now-(pickPlatform._t?.ECA||0) > 30000){
    pickPlatform._t = {...(pickPlatform._t||{}), ECA:_now};
    fetch('/api/proxy/eca/cases').then(r=>r.json())
      .then(d=>{ if(d.cases && d.cases.length) showEcaCases(d.cases); })
      .catch(()=>{});
  }
  if(p==='BDR' && scope==='selected' && _now-(pickPlatform._t?.BDR||0) > 30000){
    pickPlatform._t = {...(pickPlatform._t||{}), BDR:_now};
    fetch('/api/proxy/bdr/cases').then(r=>r.json())
      .then(d=>{ if(d.cases && d.cases.length) showBdrCases(d.cases); })
      .catch(()=>{});
  }
  if(p==='NET' && scope==='selected' && _now-(pickPlatform._t?.NET||0) > 30000){
    pickPlatform._t = {...(pickPlatform._t||{}), NET:_now};
    fetch('/api/proxy/net/cases').then(r=>r.json())
      .then(d=>{ if(d.cases && d.cases.length) showNetCases(d.cases); })
      .catch(()=>{ if(_allNetCases.length) showNetCases([]); });
  }
}
/* ── scope chooser ─────────────────────────────────────────────────────────
   The scope used to live only in Settings, which meant the most important
   decision of the screen was invisible at the moment of deciding. It is now a
   two-step choice on the sync screen itself, and the second step depends on the
   first — because "which cases" means something different in each branch:

     כל התיקים      → how wide?  clients-with-an-open-case (the usual first run
                       for a lawyer) / only open cases / everything incl. closed
     תיקים מסוימים  → what to LIST for picking?  only open / everything

   Settings still holds the default; this only overrides it for this run. */
let _syncScopeChoice = '';                 // 'all' | 'selected'
let _syncAllMode     = 'open_client';      // all-branch breadth
let _syncPickMode    = 'open';             // selected-branch list filter
try{
  const _sc = JSON.parse(localStorage.getItem('lias_sync_scope')||'{}');
  if(_sc.scope)    _syncScopeChoice = _sc.scope;
  if(_sc.allMode)  _syncAllMode     = _sc.allMode;
  if(_sc.pickMode) _syncPickMode    = _sc.pickMode;
}catch(_){}
function _saveSyncScope(){
  try{ localStorage.setItem('lias_sync_scope', JSON.stringify(
    {scope:_syncScopeChoice, allMode:_syncAllMode, pickMode:_syncPickMode})); }catch(_){}
}

function _renderScopeChooser(p, label, relNote){
  const runFn = {NET:'runNet()', BDR:'runBdr()', ECA:'runEca()'}[p];
  const listFn = {
    NET:`setSyncScope('selected');startNetDownload()`,
    BDR:`setSyncScope('selected');bdrConnectAndList()`,
    ECA:`setSyncScope('selected');ecaConnectAndList()`
  }[p];
  const subNote = p==='BDR'
    ? `<div style="font-size:11px;opacity:.65;margin-top:4px">בבד״ר: תיק עם תת-תיק פתוח ייכלל על כל ההליכים. לבחירה ידנית — לחץ "בחר מהרשימה".</div>` : '';
  return `
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <button class="btn-accent sync-opt" id="scope-go" onclick="${runFn}" style="flex:2;min-width:160px"></button>
      <button onclick="setSyncScope('selected');${listFn.split(';')[1]}"
        style="flex:1;min-width:130px;padding:7px 12px;border-radius:8px;border:1px solid rgba(255,255,255,.22);
        background:transparent;color:inherit;font-size:12px;cursor:pointer;text-align:right">
        📋 בחר מהרשימה</button>
    </div>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;opacity:.8;margin-bottom:6px">
      <input type="checkbox" id="opt-stale-first" style="width:auto"
        onchange="_syncOptChanged()"> 📅 עדכן ישנים קודם (לפי תאריך עדכון)
    </label>
    <div id="scope-hint" style="font-size:11px;opacity:.65;margin-bottom:4px"></div>
    ${subNote}
    <div id="stale-status" style="font-size:11.5px;color:var(--ink-soft);margin-top:4px"></div>`;
}

function setSyncScope(v){ _syncScopeChoice=v; _saveSyncScope(); _applyScopeChoice(); }
function setSyncAllMode(v){ _syncAllMode=v; _saveSyncScope(); _applyScopeChoice(); }
function setSyncPickMode(v){ _syncPickMode=v; _saveSyncScope(); _applyScopeChoice(); }

function _syncOptChanged(){
  const openOnly   = $('opt-open-only')?.checked   || false;
  const openClient = $('opt-open-client')?.checked || false;
  if(openClient && openOnly){
    _syncAllMode = 'open_client';
  } else if(openOnly){
    _syncAllMode = 'open';
  } else {
    _syncAllMode = 'all';
  }
  _syncPickMode = openOnly ? 'open' : 'all';
  window._syncOpenOverride = _syncAllMode;
  _saveSyncScope();
  _applyScopeChoice();
  // Re-render the case picker with the new filter if data is already loaded
  if(_syncPlatform === 'NET' && _allNetCases.length) _renderNetCasesPicker(_allNetCases);
  if(_syncPlatform === 'BDR' && _bdrCases.length) showBdrCases([]);
  if(_syncPlatform === 'ECA' && _ecaCases.length) showEcaCases([]);
}

function _applyScopeChoice(){
  if(_syncPlatform){
    const s=window._settings||{};
    const label={NET:'נט המשפט',BDR:'בית הדין הרבני',ECA:'הוצאה לפועל'}[_syncPlatform];
    const box=$('sync-options');
    if(box && !box.querySelector('#scope-go')) box.innerHTML=_renderScopeChooser(_syncPlatform,label,'');
  }
  const go=$('scope-go'), hint=$('scope-hint');
  if(!go) return;
  // derive label from checkboxes state
  const openFilter = _syncAllMode==='open_client' ? 'לקוחות עם תיק פתוח'
                   : _syncAllMode==='open'        ? 'תיקים פתוחים'
                   :                                'הכל';
  go.textContent = `⬇ הורד — ${openFilter}`;
  if(hint) hint.textContent = _syncAllMode==='open_client'
    ? 'כל התיקים של לקוח שיש לו לפחות תיק פתוח אחד'
    : _syncAllMode==='open' ? 'מדלג על תיקים סגורים' : 'כולל תיקים סגורים';
  window._syncOpenOverride = _syncAllMode;
}

/* ── BDR case picker — same contract/behaviour as the ECA one ── */
let _bdrCases = [];
try{ const _s=localStorage.getItem('bdrCasesAll'); if(_s) _bdrCases=JSON.parse(_s)||[]; }catch(_){}
let _bdrHandled = new Set();   // session-only, same rationale as _ecaHandled
/* status chip — open (green) / closed + date (grey) */
/* ── picker search ─────────────────────────────────────────────────────────
   With dozens of cases the list was unusable without scrolling. One search box
   per picker, filtering on case number, parties, court, type and sub-case ids.
   Rows are hidden with `display:none` rather than re-rendered, so checkboxes
   the user already ticked keep both their state and their data-i index. */
function _pickerSearchBox(id, placeholder){
  return `<input id="${id}" type="search" autocomplete="off"
    placeholder="${placeholder||'🔎 סינון: מספר תיק / צד / ערכאה…'}"
    oninput="_filterPickerRows('${id}')"
    style="width:100%;margin-bottom:8px;padding:8px 10px;border-radius:8px;
           border:1px solid rgba(255,255,255,.22);background:rgba(255,255,255,.08);
           color:inherit;font-size:13px">
    <div id="${id}-count" style="font-size:11.5px;opacity:.6;margin-bottom:6px"></div>`;
}
function _filterPickerRows(inputId){
  const inp=$(inputId); if(!inp) return;
  const q=(inp.value||'').trim().toLowerCase();
  const rows=document.querySelectorAll(`[data-searchrow="${inputId}"]`);
  let shown=0;
  rows.forEach(r=>{
    const hit = !q || (r.dataset.hay||'').includes(q);
    r.style.display = hit ? '' : 'none';
    if(hit) shown++;
  });
  const c=$(inputId+'-count');
  if(c) c.textContent = q ? `${shown} מתוך ${rows.length} תיקים תואמים` : '';
}
/* everything a row should be findable by, lowercased once at render time */
function _rowHay(c){
  return [c.number, c.display_id, c.CaseDisplayIdentifier, c.type, c.CaseTypeShortName,
          c.court, c.CourtName, c.party, c.client, c.name, c.CaseName, c.status,
          ...(c.parties||[]).map(p=>p&&p.name),
          ...(c.sub_cases||[]).flatMap(s=>[s.sub_id, s.procedure, s.court])]
    .filter(Boolean).join(' ').toLowerCase().replace(/"/g,'');
}

/* Open = green, closed = red. Grey read as "no data" and the two states were
   hard to tell apart at a glance, which is the whole point of the chip. */
function _statusChip(status, closeDate, openDate){
  const closed = status==='סגור' || !!(closeDate||'').trim();
  if(closed){
    const d=(closeDate||'').trim();
    return `<span style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;
      background:rgba(235,64,52,.18);color:#ff7b70;border:1px solid rgba(235,64,52,.45);
      white-space:nowrap">✖ סגור${d?' · '+d:''}</span>`;
  }
  const d=(openDate||'').trim();
  return `<span style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;
    background:rgba(52,199,89,.20);color:#5ede83;border:1px solid rgba(52,199,89,.45);
    white-space:nowrap">● פתוח${d?' · מ-'+d:''}</span>`;
}
function showBdrCases(cases){
  if(_syncPlatform !== 'BDR') return;  // don't overwrite another portal's picker
  if(!showBdrCases._busy){ showBdrCases._busy=1;
    refreshDownloadedSet().then(()=>{ showBdrCases._busy=0; showBdrCases([]); }); }
  _bdrCases = _mergeCasesList(_bdrCases, cases||[]);
  try{ localStorage.setItem('bdrCasesAll', JSON.stringify(_bdrCases)); }catch(_){}
  const picker=$('sync-case-picker'); if(!picker) return;
  if(!_bdrCases.length){ picker.style.display='none'; toast('לא נמצאו תיקי בד"ר', true); return; }
  // In "download all" mode — hide picker; download button is the UI
  if(_syncScopeChoice==='all'){ picker.style.display='none'; return; }
  // Remember what the user already ticked. A blanket "don't re-render" guard
  // used to protect the selection, but it also froze the list so newly-listed
  // cases never appeared. Re-render always; restore the ticks afterwards.
  const _wasChecked = new Set(
    [...document.querySelectorAll('.bdr-cb:checked,.bdr-sub-cb:checked')]
      .map(cb=>cb.className+':'+(cb.dataset.i||'')+':'+(cb.dataset.j||'')));
  const _hadPicker = picker.innerHTML.includes('bdr-cb');
  // data-i indexes point into _bdrCases, so it must be the rendered (filtered) list
  const _all=_sortCasesForPicker(_bdrCases);
  const list=_applyOpenFilter(_all); _bdrCases=list;
  if(!list.length){ picker.style.display='block';
    picker.innerHTML='<div style="padding:10px;color:rgba(255,255,255,.7)">כל תיקי בד"ר סוננו '
      +'לפי מצב התיק. שנה את הסינון בהגדרות ⚙ → הורדות.</div>'; return; }
  const totalSubs=list.reduce((n,c)=>n+((c.sub_cases||[]).length||0),0);
  picker.style.display='block';
  picker.innerHTML = `<div style="font-size:13px;color:rgba(255,255,255,.7);margin-bottom:6px">
      נמצאו ${list.length} תיקים${totalSubs?` · ${totalSubs} תת-תיקים`:''} — סמן תיק שלם או תת-תיק מסוים${_openFilterNote(list.length,_all.length)}</div>
    <div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_bdrSelectAll(true)">סמן הכל</button>
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_bdrSelectAll(false)">נקה הכל</button>
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_bdrSelectOpen()">רק תיקים פתוחים</button>
    </div>
    ${_pickerSearchBox('bdr-q','🔎 סינון: מספר תיק / צד / ערכאה / תת-תיק…')}
    <div style="max-height:360px;overflow-y:auto;display:flex;flex-direction:column;gap:6px">
      ${list.map((c,i)=>{
        const done=_isDownloaded(c.number,_bdrHandled);
        const subs=c.sub_cases||[];
        return `<div data-searchrow="bdr-q" data-hay="${_rowHay(c)}"
          style="border:1px solid rgba(255,255,255,.14);border-radius:9px;padding:7px 10px${done?';opacity:.75':''}">
        <label style="display:flex;gap:8px;align-items:flex-start;cursor:pointer">
          <input type="checkbox" class="bdr-cb" data-i="${i}" data-whole="1" ${done?'':'checked'}
                 onchange="_bdrToggleWhole(${i}, this.checked)">
          <div style="flex:1;min-width:0">
            <div style="font-weight:700;font-size:15px;display:flex;gap:6px;align-items:center;flex-wrap:wrap">
              <span>תיק ${c.number}</span>
              <span style="font-weight:400;opacity:.7">· ${c.type||''}</span>
              ${_statusChip(c.status, c.close_date, c.open_date)}
              ${subs.length?`<span style="font-size:11px;opacity:.6">(${subs.length} תת-תיקים — תיק שלם)</span>`:''}
              ${done?'<span style="font-size:11px;color:var(--accent-strong,#7fb3ff)">✓ הורד</span>':''}
            </div>
            <div style="font-size:13px;opacity:.85;line-height:1.5">
              <span style="opacity:.7">בין:</span> <b>${c.party||'—'}</b>
              ${c.court?` &nbsp;•&nbsp; <span style="opacity:.7">ערכאה:</span> <b>${c.court}</b>`:''}
            </div>
          </div>
        </label>
        ${subs.length?`<div style="margin-inline-start:24px;margin-top:5px;display:flex;flex-direction:column;gap:3px">
          ${subs.map((s,j)=>`<label style="display:flex;gap:7px;align-items:center;cursor:pointer;font-size:13px">
            <input type="checkbox" class="bdr-sub-cb" data-i="${i}" data-j="${j}" ${done?'':'checked'}>
            <span style="flex:1;min-width:0">
              <b>${s.sub_id}</b> <span style="opacity:.75">${s.procedure||''}</span>
              ${s.court?`<span style="opacity:.6"> · ${s.court}</span>`:''}
            </span>
            ${_statusChip(s.status, s.close_date, s.open_date)}
          </label>`).join('')}
        </div>`:''}
      </div>`;}).join('')}
    </div>
    <button class="btn-accent" style="width:100%;margin-top:10px;padding:12px" onclick="runBdrSelected()">⬇ הורד את המסומנים</button>`;
  // put the user's ticks back after the re-render (only when a picker existed —
  // a first render keeps its default of "everything not yet downloaded")
  if(_hadPicker){
    document.querySelectorAll('.bdr-cb,.bdr-sub-cb').forEach(cb=>{
      cb.checked = _wasChecked.has(cb.className+':'+(cb.dataset.i||'')+':'+(cb.dataset.j||''));
    });
  }
}
function _bdrSelectAll(v){
  document.querySelectorAll('.bdr-cb,.bdr-sub-cb').forEach(cb=>cb.checked=v);
}
function _bdrSelectOpen(){
  document.querySelectorAll('.bdr-cb').forEach(cb=>{
    const c=_bdrCases[+cb.dataset.i];
    cb.checked = !!(c && !_isCaseClosed(c));
  });
  document.querySelectorAll('.bdr-sub-cb').forEach(cb=>{
    const c=_bdrCases[+cb.dataset.i]; const s=(c&&c.sub_cases||[])[+cb.dataset.j];
    cb.checked = !!(s && !_isCaseClosed(s));
  });
}
function _isCaseClosed(c){
  if(!c) return false;
  if(c.status==='סגור') return true;
  // close_date present = closed (consistent with _statusChip)
  return !!(c.close_date||'').trim();
}
/* checking a whole case checks all of its sub-cases */
function _bdrToggleWhole(i, checked){
  document.querySelectorAll(`.bdr-sub-cb[data-i="${i}"]`).forEach(cb=>cb.checked=checked);
}
function runBdrSelected(){
  // A case is requested either whole (parent ticked) or per sub-case.
  const picked=new Set();
  document.querySelectorAll('.bdr-cb:checked').forEach(cb=>{
    const c=_bdrCases[+cb.dataset.i]; if(c) picked.add(c.number);
  });
  const subs=[];
  document.querySelectorAll('.bdr-sub-cb:checked').forEach(cb=>{
    const c=_bdrCases[+cb.dataset.i]; const s=(c&&c.sub_cases||[])[+cb.dataset.j];
    if(s) subs.push(s.sub_id);
  });
  if(!picked.size && !subs.length){ toast('לא סומן אף תיק', true); return; }
  picked.forEach(n=>_bdrHandled.add(n));
  runBdrBatch('', [...picked], subs);
  const picker=$('sync-case-picker'); if(picker) picker.style.display='none';
}
async function runNet(){
  if($('opt-stale-first')?.checked){ _downloadStaleCases('NET'); return; }
  if(!await ensureNoPortalRunning('NET')) return;
  const s = window._settings || {};
  const scope = _syncScopeChoice || s.net_scope || 'selected';
  if(scope==='all'){
    const filter = _syncAllMode || 'all';
    fetch('/api/proxy/actions/net_download_all', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({open_filter: filter})
    }).then(r=>{
      if(r.ok) showJobBar('הורדת כל תיקי נט', 0, 'ממתין לתחילת עבודה…');
      else toast('שגיאה בהפעלה', true);
    }).catch(e=>toast('שגיאה: '+e.message, true));
    logEvent('→ הורדת כל תיקי נט (סינון: '+filter+')');
  } else {
    startNetDownload();
  }
}
function runBdr(){
  if($('opt-stale-first')?.checked){ _downloadStaleCases('BDR'); return; }
  const s = window._settings || {};
  const scope = _syncScopeChoice || s.bdr_scope || 'all';
  if(scope==='all') runBdrBatch('');
  else bdrConnectAndList();
}
async function runEca(){
  if($('opt-stale-first')?.checked){ _downloadStaleCases('ECA'); return; }
  if(!await ensureNoPortalRunning('ECA')) return;
  const s = window._settings || {};
  const scope = _syncScopeChoice || s.eca_scope || 'all';
  if(scope==='all'){
    act('eca_sync','סנכרון כל תיקי הוצל"פ');
  } else {
    ecaConnectAndList();
  }
}
async function checkCaseByNumber(portal){
  if(portal==='NET'){ openNetCase(true); return; }
  const num=($('nc-num')?.value||'').trim();
  if(!num){ toast('נא להזין מספר תיק', true); return; }
  if(!await ensureNoPortalRunning(portal)) return;
  if(portal==='ECA'){
    // ONE request. This used to fire act('eca_sync') (no body → download every
    // case) and immediately a second eca_sync for this case; the first grabbed
    // the portal lock and the second died with "כרגע רצה פעולה בהוצאה לפועל".
    logEvent('→ סנכרון תיק הוצל"פ '+num);
    toast('מסנכרן תיק הוצל"פ '+num+'…');
    fetch('/api/proxy/actions/eca_sync',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cases:[num]})})
      .then(r=>{ if(!r.ok) toast('שגיאה בהפעלת סנכרון', true); })
      .catch(e=>toast('שגיאה: '+e.message, true));
  } else if(portal==='BDR'){
    runBdrBatch(num);   // client_filter also matches a case number substring
  }
}
function syncOpenNetCase(){ act('sync_current/NET','סנכרון התיק הפתוח בנט'); }
/* Login failure → clear message + one-click retry */
function showLoginRetry(kind, err){
  $('login-retry')?.remove();
  const map={eca_sync:'eca_sync',eca_list:'eca_list',bdr_batch:'bdr_batch',bdr_list:'bdr_list',
    net_smart_download:'net_list_cases',net_download_all:'net_download_all',
    net_list_cases:'net_list_cases',open_portal:'open_portal'};
  const retryKind=map[kind]||kind;
  const d=document.createElement('div'); d.id='login-retry';
  d.style.cssText='position:fixed;top:50%;right:50%;transform:translate(50%,-50%);z-index:140;'
    +'background:var(--surface,#fff);border:1px solid var(--danger,#c62828);border-radius:14px;'
    +'padding:20px;width:min(420px,92vw);box-shadow:0 16px 60px rgba(0,0,0,.4);direction:rtl';
  d.innerHTML=`<b style="font-size:15px;color:var(--danger)">⚠️ ההתחברות נכשלה</b>
    <div class="sub" style="margin:8px 0;line-height:1.6">${(err||'').slice(0,160)}</div>
    <div class="note" style="margin-bottom:12px">ייתכן שהקוד לא נקרא מהמייל, או שהפורטל דרש רענון. אפשר לנסות שוב:</div>
    <div style="display:flex;gap:8px">
      <button class="btn-accent" style="flex:1" onclick="$('login-retry').remove(); act('${retryKind}','ניסיון חוזר')">🔄 נסה שוב</button>
      <button class="fv-btn" onclick="openSettings();$('login-retry').remove()">בדוק הגדרות מייל</button>
      <button class="fv-btn" onclick="$('login-retry').remove()">בטל</button>
    </div>`;
  document.body.appendChild(d);
}
async function startNetDownload(){
  if(!await ensureNoPortalRunning('NET')) return;
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
  if(!await ensureNoPortalRunning('NET')) return;
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
function cancelNetDownload(){ stopPortalDownload('NET'); }
function stopPortalDownload(portal){
  const s=_dlByPortal[portal]; const jid=(s&&s.job_id)||_netDownloadJobId;
  if(!jid){ toast('אין הורדה פעילה לבטל', true); return; }
  act('cancel_download?job_id='+jid, 'ביטול הורדת '+(PORTAL_LABELS[portal]||portal));
  toast('שולח הוראת עצירה — ההורדה תיעצר אחרי התיק הנוכחי');
}
function stopCase(portal, caseId){
  const s=_dlByPortal[portal]; const jid=s&&s.job_id;
  if(!jid){ toast('אין הורדה פעילה', true); return; }
  // Mark it locally at once. The engine only reacts between documents, so
  // without this the row kept looking active for seconds and the click felt
  // ignored — people pressed it again and again.
  const row=(s.cases_detail||[]).find(c=>String(c.id)===String(caseId));
  if(row && (row.status==='pending'||row.status==='downloading')){
    row.status='skipped'; _saveDl();
    if(typeof _renderDlStats==='function') _renderDlStats();
    _pollState();
  }
  logEvent(`⏹ בקשת עצירה לתיק ${caseId} (${PORTAL_LABELS[portal]||portal})`);
  fetch('/api/proxy/actions/cancel_case',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({job_id:jid,case:caseId})})
    .then(r=>{ if(r.ok) toast(`תיק ${caseId} יידלג — ההורדה ממשיכה לשאר התיקים`);
               else { toast('שגיאה בעצירת התיק', true); if(row) row.status='downloading'; } })
    .catch(e=>{ toast('שגיאה: '+e.message,true); if(row) row.status='downloading'; });
}
/* ═══════════════════════════════════════════════════════════════════
   Floating Download Bubble
   ─ Fixed bottom-left, draggable, minimizable, survives refresh
   ═══════════════════════════════════════════════════════════════════ */
let _dlBubbleMinimized = false;
let _dlBubblePaused    = false;
let _dlBubblePos       = null;   // {x,y} saved position
try {
  const _s = sessionStorage.getItem('dlBubble');
  if(_s){ const p=JSON.parse(_s); _dlBubbleMinimized=p.min||false; _dlBubblePos=p.pos||null; }
} catch(_){}

function _saveBubbleState(){
  try{ sessionStorage.setItem('dlBubble', JSON.stringify({min:_dlBubbleMinimized, pos:_dlBubblePos})); }catch(_){}
}

function _renderDlStats(){
  const portals = Object.keys(_dlByPortal);
  let bubble = document.getElementById('dl-bubble');

  if(!portals.length){
    if(bubble) bubble.style.display='none';
    return;
  }

  if(!bubble){
    bubble = document.createElement('div');
    bubble.id = 'dl-bubble';
    bubble.dir = 'rtl';
    bubble.style.cssText = [
      'position:fixed','z-index:9999','direction:rtl',
      'font-family:inherit','user-select:none',
      'transition:box-shadow .15s',
    ].join(';');
    document.body.appendChild(bubble);
    _applyBubblePos(bubble);
    _makeDraggable(bubble);
  }
  bubble.style.display = 'block';

  if(_dlBubbleMinimized){
    // ── Minimized badge ──────────────────────────────────────────
    const totalDone   = Object.values(_dlByPortal).reduce((s,p)=>s+(p.done||0),0);
    const totalCases  = Object.values(_dlByPortal).reduce((s,p)=>s+(p.total||0),0);
    const totalDocs   = Object.values(_dlByPortal).reduce((s,p)=>s+(p.docs_downloaded||0),0);
    const portLabel   = portals.map(p=>PORTAL_LABELS[p]||p).join(' · ');
    const paused      = _dlBubblePaused;
    bubble.innerHTML = `
      <div onclick="_dlBubbleMinimized=false;_saveBubbleState();_renderDlStats()"
        style="background:#1a2a40;color:#e8edf4;border-radius:999px;padding:7px 14px 7px 10px;
               box-shadow:0 4px 18px rgba(0,0,0,.5);cursor:pointer;display:flex;align-items:center;gap:8px;
               border:1px solid rgba(255,255,255,.18);font-size:13px;white-space:nowrap">
        <span style="font-size:16px">${paused?'⏸':'⬇'}</span>
        <span><b>${totalDone}/${totalCases}</b> תיקים · ${totalDocs} מסמכים</span>
        <span style="opacity:.55;font-size:11px">${portLabel}</span>
        <span style="opacity:.4;font-size:11px">▲</span>
      </div>`;
    return;
  }

  // ── Expanded bubble ──────────────────────────────────────────
  bubble.style.cssText += ';width:min(400px,96vw);border-radius:16px;'
    +'background:#111c2d;border:1px solid rgba(255,255,255,.15);'
    +'box-shadow:0 16px 48px rgba(0,0,0,.6);overflow:hidden';

  const paused = _dlBubblePaused;
  const activePortal = portals.find(p=>_dlByPortal[p].job_id) || portals[0];
  const jobId = _dlByPortal[activePortal]?.job_id || 0;

  bubble.innerHTML = `
    <div id="dl-bubble-drag" style="background:#0d1726;padding:8px 12px;cursor:grab;
         display:flex;align-items:center;gap:8px;border-bottom:1px solid rgba(255,255,255,.1)">
      <span style="font-size:14px;flex:1;font-weight:700;color:#c9d8f0">
        ${paused?'⏸ מושהה':'⬇ מוריד'}
      </span>
      <button title="הסתר / הצג דפדפן" onclick="_toggleBubbleBrowser('${activePortal}')"
        style="${_btnStyle('#1e3a5f')}">👁</button>
      <button title="${paused?'המשך':'השהה'}" onclick="_togglePause(${jobId})"
        style="${_btnStyle(paused?'#1a4020':'#2a2a15')}">
        ${paused?'▶':'⏸'}
      </button>
      <button title="עצור הכל" onclick="stopAllDownloads()"
        style="${_btnStyle('#4a0f0f')}">⏹</button>
      <button title="מזער" onclick="_dlBubbleMinimized=true;_saveBubbleState();_renderDlStats()"
        style="${_btnStyle('#1a2333')}">▼</button>
    </div>
    <div style="padding:10px 14px;max-height:60vh;overflow-y:auto">
      ${portals.map(p=>_renderPortalCard(p, _dlByPortal[p])).join('')}
    </div>`;

  // Re-attach drag to new header
  _makeDraggable(bubble, document.getElementById('dl-bubble-drag'));
}

function _btnStyle(bg){
  return `background:${bg};border:none;border-radius:7px;padding:4px 8px;cursor:pointer;`
        +'color:#c9d8f0;font-size:13px;min-width:28px;';
}

function _applyBubblePos(bubble){
  if(_dlBubblePos){
    bubble.style.left = _dlBubblePos.x+'px';
    bubble.style.top  = _dlBubblePos.y+'px';
    bubble.style.bottom = 'auto';
    bubble.style.right  = 'auto';
  } else {
    bubble.style.bottom = '20px';
    bubble.style.left   = '16px';
  }
}

function _makeDraggable(el, handle){
  const grip = handle || el;
  grip.style.cursor = 'grab';
  grip.onmousedown = e=>{
    if(e.target.tagName==='BUTTON') return;
    e.preventDefault();
    const r = el.getBoundingClientRect();
    const ox = e.clientX - r.left, oy = e.clientY - r.top;
    grip.style.cursor = 'grabbing';
    const onMove = e=>{
      const x = Math.max(0, Math.min(window.innerWidth-el.offsetWidth,  e.clientX-ox));
      const y = Math.max(0, Math.min(window.innerHeight-el.offsetHeight, e.clientY-oy));
      el.style.left='auto'; el.style.right='auto';
      el.style.top='auto';  el.style.bottom='auto';
      el.style.left=x+'px'; el.style.top=y+'px';
      _dlBubblePos={x,y}; _saveBubbleState();
    };
    const onUp = ()=>{
      grip.style.cursor='grab';
      document.removeEventListener('mousemove',onMove);
      document.removeEventListener('mouseup',onUp);
    };
    document.addEventListener('mousemove',onMove);
    document.addEventListener('mouseup',onUp);
  };
}

async function _togglePause(jobId){
  const ep = _dlBubblePaused ? 'resume_download' : 'pause_download';
  _dlBubblePaused = !_dlBubblePaused;
  _renderDlStats();
  await fetch(`/api/proxy/actions/${ep}?job_id=${jobId}`,{method:'POST'}).catch(()=>{});
}

async function _toggleBubbleBrowser(portal){
  // Detect current visibility from browser status
  const r = await fetch('/api/browser/status').then(r=>r.json()).catch(()=>({}));
  const portalKey = portal==='BDR'?'bdr':portal==='ECA'?'eca':'main';
  const vis = !(r[portalKey]?.headless ?? true);
  await fetch('/api/proxy/actions/toggle_browser_visible',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({portal, visible:!vis})
  }).catch(()=>{});
  toast(vis ? 'דפדפן הוסתר' : 'דפדפן נפתח');
}

function _renderPortalCard(portal, s){
  const pct = s.total? Math.round((s.done||0)/s.total*100) : 0;
  const elapsed = s.elapsed_sec||0;
  const mm = Math.floor(elapsed/60), ss2 = String(elapsed%60).padStart(2,'0');
  const currentCase = s.current_name ? `${s.current_case} — ${s.current_name}` : (s.current_case||'');
  const currentDoc  = s.current_doc || '';

  const details = s.cases_detail||[];
  const casesHtml = details.length ? `
    <div style="margin-top:8px;max-height:180px;overflow-y:auto;font-size:11.5px;
         border-top:1px solid rgba(255,255,255,.08);padding-top:6px">
      ${details.map(c=>{
        const st=c.status||'pending';
        const icon = {done:'✓',downloading:'⏳',failed:'✗',skipped:'⏭'}[st]||'·';
        const dim  = st==='done'||st==='skipped';
        const stopBtn = (st==='pending'||st==='downloading')
          ? `<button onclick="event.stopPropagation();stopCase('${portal}','${c.id}')"
              title="דלג לתיק הבא" style="background:none;border:none;cursor:pointer;
              color:#ef9a9a;font-size:11px;padding:0 2px" >⏭</button>` : '';
        const clk = st==='done'
          ? `style="cursor:pointer" onclick="closeDlPanel();_goToCaseByNumber('${c.id}')"` : '';
        return `<div style="display:flex;align-items:center;gap:4px;padding:2px 0;
                  opacity:${dim?.5:1};font-weight:${st==='downloading'?700:400};
                  border-bottom:1px solid rgba(255,255,255,.04)">
          <span style="min-width:14px;text-align:center;font-size:11px">${icon}</span>
          <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" ${clk}>
            ${c.id}${c.name?' — '+c.name:''}
          </span>${stopBtn}
        </div>`;
      }).join('')}
    </div>` : '';

  return `<div style="background:rgba(255,255,255,.04);border-radius:10px;padding:10px 12px;margin-bottom:8px">
    <div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:5px">
      <b style="color:#a8c8f8">${PORTAL_LABELS[portal]||portal}</b>
      <span style="color:rgba(255,255,255,.5)">${mm}:${ss2} · ${s.speed_per_min||0} מסמכים/דק׳</span>
    </div>
    <div style="height:5px;background:rgba(255,255,255,.1);border-radius:3px;overflow:hidden;margin-bottom:6px">
      <div style="height:100%;width:${pct}%;background:#4a9eff;transition:width .4s;border-radius:3px"></div>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:#c9d8f0;margin-bottom:${currentCase?6:0}px">
      <span>תיקים: <b>${s.done||0}/${s.total||0}</b></span>
      <span>מסמכים: <b>${s.docs_downloaded||0}</b></span>
      ${s.failed?`<span style="color:#ef9a9a">נכשלו: <b>${s.failed}</b></span>`:''}
      <span style="color:rgba(255,255,255,.4)">נותרו: ${s.remaining||0}</span>
    </div>
    ${currentCase?`<div style="font-size:11.5px;color:rgba(255,255,255,.65);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
      📂 ${currentCase}</div>`:''}
    ${currentDoc?`<div style="font-size:11px;color:rgba(255,255,255,.4);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px">
      📄 ${currentDoc}</div>`:''}
    ${casesHtml}
  </div>`;
}

function closeDlPanel(){ const b=document.getElementById('dl-bubble'); if(b) b.style.display='none'; }
function _goToCaseByNumber(displayId){
  if(!D?.case_cards) return;
  const card = D.case_cards.find(c=>(c.sub_number||'').includes(displayId));
  if(card){ go('case', card.sub_case_id); }
  else { toast('התיק טרם נוסף למערכת — רענן את הדשבורד'); refresh(true); }
}

/* ─── NET cases checkbox picker — inline in sync card ─── */
/* This second definition overrides the first showNetCases above. */
let _allNetCases=[];
try{ const _s=localStorage.getItem('netCasesAll'); if(_s) _allNetCases=JSON.parse(_s)||[]; }catch(_){}
function showNetCases(cases){
  // Cumulative: remember every case ever shown + merge newly-arrived ones
  _allNetCases = _mergeCasesList(_allNetCases, cases||[]);
  try{ localStorage.setItem('netCasesAll', JSON.stringify(_allNetCases)); }catch(_){}
  _pendingNetCases = _allNetCases;
  if(!_allNetCases.length){ toast('לא נמצאו תיקים בפורטל', true); return; }
  if(route.v !== 'sync'){ go('sync'); }
  // In "download all" mode — hide picker; download button is the UI
  if(_syncScopeChoice==='all'){
    const picker=$('sync-case-picker'); if(picker) picker.style.display='none';
    return;
  }
  setTimeout(()=>_renderNetCasesPicker(_allNetCases), 200);
}
function _renderNetCasesPicker(cases){
  const picker = $('sync-case-picker');
  if(!picker){ setTimeout(()=>_renderNetCasesPicker(cases), 300); return; }
  const _allNet = _sortCasesForPicker(cases);   // open on top, newest first
  cases = _applyOpenFilter(_allNet);            // value="i" indexes this list
  _pendingNetCases = cases;
  const _esc = s => (s||'').replace(/"/g, '״').replace(/'/g, '׳');
  picker.style.display='block';
  if(!cases.length){
    picker.innerHTML='<div style="padding:10px;color:rgba(255,255,255,.7)">כל תיקי נט המשפט '
      +'סוננו לפי מצב התיק. שנה את הסינון בהגדרות ⚙ → הורדות.</div>'; return; }
  picker.innerHTML=`<div style="font-size:13px;color:rgba(255,255,255,.85);margin-bottom:8px;font-weight:700">
      נמצאו ${cases.length} תיקים — סמן מה להוריד${_openFilterNote(cases.length,_allNet.length)}</div>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllNetCases(true)">סמן הכל</button>
      <button class="btn-accent" style="font-size:11px;padding:5px 10px" onclick="_selectAllNetCases(false)">נקה הכל</button>
    </div>
    ${_pickerSearchBox('net-q','🔎 סינון: מספר תיק / צד / ערכאה…')}
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
      return `<label data-searchrow="net-q" data-hay="${_rowHay(c)}"
        style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;
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
async function syncPickedCases(){
  const picked = [...document.querySelectorAll('.nc-pick:checked')].map(el=>window._netCases[+el.dataset.i]);
  if(!picked.length){ toast('לא סומן אף תיק', true); return; }
  if(!await ensureNoPortalRunning('NET')) return;
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

// ── Download all portals serially ───────────────────────────────────────────
// Runs NET → BDR → ECA in order, waiting for each job to COMPLETE before next.
async function downloadAll(){
  const el = $('download-all-status');
  const filter = _syncAllMode || 'all';
  const staleFirst = !!$('opt-stale-first')?.checked;
  if(staleFirst){ _downloadStaleCases(); return; }

  const portals = ['NET','BDR','ECA'];
  if(el) el.textContent = `מפעיל הורדה סדרתית: ${portals.join(' → ')}…`;

  for(const p of portals){
    const busy = await portalBusy(p);
    if(busy){ if(el) el.textContent=`⏩ מדלג ${p} — כבר פעיל`; continue; }
    let jobId = null;
    try{
      if(p==='NET'){
        const r = await fetch('/api/proxy/actions/net_download_all',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({open_filter: filter})});
        const d = await r.json(); jobId = d.job_id;
      } else if(p==='BDR'){
        const r = await fetch('/api/proxy/actions/bdr_batch',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({open_filter: filter, cases:[], sub_cases:[]})});
        const d = await r.json(); jobId = d.job_id;
      } else if(p==='ECA'){
        const r = await fetch('/api/proxy/actions/eca_sync',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({open_filter: filter})});
        const d = await r.json(); jobId = d.job_id;
      }
    } catch(e){ if(el) el.textContent=`שגיאה ב-${p}: ${e.message}`; continue; }

    if(!jobId){ if(el) el.textContent=`⚠ ${p} — לא הוחזר job_id`; continue; }
    if(el) el.textContent = `⏳ ${p} רץ (job ${jobId})…`;

    // Wait for job to complete (poll every 3s, up to 90 min)
    await new Promise(resolve=>{
      let ticks=0;
      const t = setInterval(async()=>{
        ticks++;
        if(ticks > 1800){ clearInterval(t); resolve(); return; }
        try{
          const jobs = await (await fetch('/api/jobs')).json();
          const job = (jobs||[]).find(j=>j.job_id===jobId);
          if(!job || ['COMPLETED','FAILED','CANCELLED'].includes(job.state)){
            clearInterval(t);
            if(el) el.textContent = job?.state==='COMPLETED'
              ? `✓ ${p} הסתיים — ממשיך לפורטל הבא…`
              : `⚠ ${p} ${job?.state||'לא נמצא'}`;
            resolve();
          } else if(el){
            el.textContent = `⏳ ${p}: ${job.message||''} (${Math.round((job.progress||0)*100)}%)`;
          }
        }catch{ clearInterval(t); resolve(); }
      }, 3000);
    });
  }
  if(el) el.textContent = '✓ הורדה מכל הפורטלים הסתיימה';
}

// ── Client profile manager ──────────────────────────────────────────────────
let _activeProfile = null;  // mirrors server-side _active_profile

function _updateProfileBanner(profile){
  _activeProfile = profile || null;
  const banner = $('profile-banner');
  if(!banner) return;
  if(profile){
    banner.style.display = 'flex';
    const nameEl = $('profile-banner-name');
    const dirEl  = $('profile-banner-dir');
    if(nameEl) nameEl.textContent = profile.name;
    if(dirEl)  dirEl.textContent  = `הורדות: court_documents/profiles/${profile.slug}/`;
  } else {
    banner.style.display = 'none';
  }
  // tint the profile-tab-btn to indicate active state
  const btn = $('profile-tab-btn');
  if(btn) btn.style.background = profile
    ? 'rgba(99,102,241,.55)' : 'rgba(99,102,241,.18)';
}

async function openProfileManager(){
  if($('profile-mgr')) { $('profile-mgr').remove(); return; }

  // Load current profiles
  let data = {profiles:[], active:null};
  try{ data = await fetch('/api/profiles').then(r=>r.json()); }catch{}

  _updateProfileBanner(data.active);

  const box = document.createElement('div');
  box.id = 'profile-mgr';
  const _isDark = document.documentElement.dataset.theme === 'dark'
    || (!document.documentElement.dataset.theme && window.matchMedia('(prefers-color-scheme:dark)').matches);
  box.style.cssText = `position:fixed;top:60px;left:50%;transform:translateX(-50%);
    z-index:200;background:${_isDark?'#1e2130':'#ffffff'};
    border:1.5px solid rgba(99,102,241,.4);
    border-radius:16px;padding:20px 24px;width:420px;max-width:96vw;
    box-shadow:0 20px 60px rgba(0,0,0,.45);direction:rtl;font-size:13px;
    color:${_isDark?'#e2e8f0':'#1a1a2e'}`;

  const _render = (profiles, active) => {
    const activeId = active?.id;
    const rows = profiles.length
      ? profiles.map(p => `
        <div style="display:flex;align-items:center;gap:8px;padding:7px 10px;
          border-radius:10px;background:${p.id===activeId?'rgba(99,102,241,.18)':'rgba(255,255,255,.04)'};
          border:1px solid ${p.id===activeId?'rgba(99,102,241,.5)':'rgba(255,255,255,.1)'}">
          <span style="font-size:20px">👤</span>
          <div style="flex:1;min-width:0">
            <div style="font-weight:700">${p.name}</div>
            <div style="font-size:11px;opacity:.6">${p.slug}</div>
          </div>
          ${p.id===activeId
            ? `<span style="font-size:11px;color:rgba(99,102,241,1);font-weight:700">● פעיל</span>
               <button onclick="_deactivateAndRefreshMgr()" style="font-size:12px;padding:3px 10px;
                 border-radius:8px;cursor:pointer;background:rgba(99,102,241,.22);
                 border:1px solid rgba(99,102,241,.5);color:inherit">חזור</button>`
            : `<button onclick="_activateProfile('${p.id}')"
                 style="font-size:12px;padding:3px 10px;border-radius:8px;cursor:pointer;
                 background:rgba(99,102,241,.14);border:1px solid rgba(99,102,241,.35);color:inherit">
                 הפעל ▶</button>
               <button onclick="_deleteProfile('${p.id}','${p.name}')"
                 style="font-size:12px;padding:3px 8px;border-radius:8px;cursor:pointer;
                 background:rgba(220,38,38,.12);border:1px solid rgba(220,38,38,.3);color:inherit">✕</button>`}
        </div>
        ${p.id===activeId ? `
        <div style="margin-top:8px;padding:10px 12px;border-radius:10px;
          background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.2)">
          <div style="font-size:12px;font-weight:700;margin-bottom:10px">🍪 עוגיות פרופיל</div>

          <!-- Step 1: connect to NET -->
          <div style="font-size:11px;opacity:.7;margin-bottom:6px;line-height:1.5">
            <b>שלב 1</b> — כנס לנט המשפט עם פרטי הלקוח (עוגיות אחידות לכל הפורטלים)
          </div>
          <button onclick="_profileOpenNet()"
            style="width:100%;padding:7px;border-radius:8px;font-size:12.5px;font-weight:700;
            cursor:pointer;background:rgba(59,130,246,.2);border:1px solid rgba(59,130,246,.4);
            color:inherit;margin-bottom:10px">
            🌐 פתח נט המשפט לכניסה
          </button>

          <!-- Step 2: export -->
          <div style="font-size:11px;opacity:.7;margin-bottom:6px;line-height:1.5">
            <b>שלב 2</b> — לאחר כניסה מוצלחת, ייצא את העוגיות כ-JSON ושלח ללקוח
          </div>
          <button onclick="_exportProfileCookies('NET')"
            style="width:100%;padding:7px;border-radius:8px;font-size:12.5px;font-weight:700;
            cursor:pointer;background:rgba(99,102,241,.2);border:1px solid rgba(99,102,241,.4);
            color:inherit;margin-bottom:12px">
            ⬆ ייצא עוגיות → JSON להורדה
          </button>

          <div style="border-top:1px solid rgba(255,255,255,.1);padding-top:10px">
            <div style="font-size:11px;opacity:.7;margin-bottom:6px">
              <b>ייבוא</b> — קיבלת JSON מהלקוח (דרך session_server.py)?
            </div>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="file" id="profile-cookie-file" accept=".json"
                onchange="_onProfileCookieChosen(this)"
                style="font-size:11.5px;flex:1;min-width:0;cursor:pointer">
              <button onclick="_importProfileCookies()"
                style="padding:5px 12px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;
                white-space:nowrap;background:rgba(34,197,94,.2);border:1px solid rgba(34,197,94,.4);
                color:inherit">ייבא ⬇</button>
            </div>
          </div>
          <div id="profile-cookie-status" style="font-size:11px;margin-top:6px;
            color:var(--ink-soft);min-height:14px"></div>
        </div>` : ''}
        `).join('')
      : `<div style="text-align:center;opacity:.5;padding:12px">אין פרופילי לקוחות עדיין</div>`;

    box.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <b style="font-size:15px">👤 פרופילי לקוחות</b>
        <button onclick="$('profile-mgr').remove()" style="background:none;border:none;
          font-size:18px;cursor:pointer;color:var(--ink-soft)">✕</button>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px">${rows}</div>
      <div style="border-top:1px solid rgba(255,255,255,.1);padding-top:12px">
        <div style="font-weight:600;margin-bottom:8px;font-size:12px;opacity:.7">+ צור פרופיל חדש</div>
        <div style="display:flex;gap:8px">
          <input id="profile-new-name" placeholder="שם הלקוח / התיק" style="flex:1;
            padding:7px 10px;border-radius:8px;border:1px solid rgba(255,255,255,.2);
            background:rgba(255,255,255,.07);color:inherit;font-size:13px;direction:rtl">
          <button onclick="_createProfile()" style="padding:7px 14px;border-radius:8px;
            background:rgba(99,102,241,.3);border:1px solid rgba(99,102,241,.5);
            color:inherit;font-weight:700;cursor:pointer;white-space:nowrap">צור</button>
        </div>
        <div id="profile-create-status" style="font-size:11.5px;margin-top:6px;
          color:var(--ink-soft);min-height:16px"></div>
      </div>
      <div style="border-top:1px solid rgba(255,255,255,.1);padding-top:12px;margin-top:4px">
        <div style="font-weight:600;margin-bottom:6px;font-size:12px;opacity:.7">📨 הזמן עוגיות מלקוח</div>
        <div style="font-size:11px;opacity:.6;margin-bottom:8px;line-height:1.5">
          יוצר קישור — הלקוח פותח, מתחבר, ושולח. אתה מייבא לפרופיל.
        </div>
        <button onclick="_inviteCookies()" id="invite-cookies-btn"
          style="width:100%;padding:7px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;
          background:rgba(245,158,11,.2);border:1px solid rgba(245,158,11,.4);color:inherit">
          🔗 צור קישור לכניסה
        </button>
        <div id="invite-url-row" style="display:none;margin-top:8px">
          <div style="display:flex;gap:6px;align-items:center">
            <input id="invite-url-val" readonly style="flex:1;padding:5px 8px;border-radius:8px;
              border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.07);
              color:inherit;font-size:11.5px;direction:ltr">
            <button onclick="_copyInviteUrl()" style="padding:5px 10px;border-radius:8px;
              background:rgba(99,102,241,.3);border:1px solid rgba(99,102,241,.5);
              color:inherit;font-weight:700;cursor:pointer;white-space:nowrap;font-size:12px">העתק</button>
          </div>
          <button onclick="_stopInvite()" style="margin-top:6px;width:100%;padding:5px;border-radius:8px;
            font-size:11.5px;cursor:pointer;background:rgba(239,68,68,.15);
            border:1px solid rgba(239,68,68,.3);color:inherit;opacity:.8">⛔ סגור קישור</button>
        </div>
        <div id="invite-status" style="font-size:11px;margin-top:5px;min-height:14px;opacity:.8"></div>
      </div>
      <div style="margin-top:10px;font-size:11px;opacity:.5;border-top:1px solid rgba(255,255,255,.1);padding-top:8px">
        כל פרופיל: תיקיית הורדות נפרדת · פרופיל דפדפן נפרד (cookies) · DB נפרד<br>
        בזמן פרופיל פעיל — הורדות המשתמש הראשי מעוצרות
      </div>`;
  };

  _render(data.profiles, data.active);
  document.body.appendChild(box);

  // close on outside click
  setTimeout(()=>{
    const _close = e => { if(!box.contains(e.target) && e.target.id!=='profile-tab-btn'){
      box.remove(); document.removeEventListener('mousedown',_close); }};
    document.addEventListener('mousedown', _close);
  }, 100);
}

let _profileCookieData = null;
let _exportSessionPanel = null;
function exportMySession(){
  if(_exportSessionPanel){ _exportSessionPanel.remove(); _exportSessionPanel=null; return; }
  const _isDark = document.documentElement.dataset.theme==='dark'
    || (!document.documentElement.dataset.theme && window.matchMedia('(prefers-color-scheme:dark)').matches);
  const box = document.createElement('div');
  box.style.cssText = `position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:9999;
    width:340px;border-radius:16px;padding:20px;
    background:${_isDark?'#1e2130':'#ffffff'};color:${_isDark?'#e2e8f0':'#1a1a2e'};
    border:1px solid ${_isDark?'rgba(255,255,255,.12)':'rgba(0,0,0,.1)'};
    box-shadow:0 16px 48px rgba(0,0,0,.35);direction:rtl;font-family:inherit`;
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <span style="font-weight:700;font-size:14px">📤 שלח עוגיות לעמית</span>
      <button onclick="exportMySession()" style="background:none;border:none;cursor:pointer;font-size:16px;color:inherit;opacity:.6">✕</button>
    </div>
    <div style="font-size:12px;opacity:.7;line-height:1.6;margin-bottom:14px">
      <b>שלב 1</b> — פתח את דפדפן נט המשפט והתחבר עם הפרטים שלך<br>
      <b>שלב 2</b> — לחץ "הורד JSON" ושלח את הקובץ לעמית
    </div>
    <button id="exp-open-btn" onclick="_expOpenBrowser()"
      style="width:100%;padding:8px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;
      background:rgba(59,130,246,.2);border:1px solid rgba(59,130,246,.4);color:inherit;margin-bottom:8px">
      🌐 פתח נט המשפט לכניסה
    </button>
    <button onclick="_expDownload()"
      style="width:100%;padding:8px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;
      background:rgba(16,185,129,.2);border:1px solid rgba(16,185,129,.4);color:inherit">
      ⬇ הורד JSON (אחרי כניסה)
    </button>
    <div id="exp-status" style="font-size:11.5px;margin-top:8px;min-height:16px;opacity:.8"></div>`;
  document.body.appendChild(box);
  _exportSessionPanel = box;
}

async function _expOpenBrowser(){
  const st = $('exp-status'), btn = $('exp-open-btn');
  if(st) st.textContent = 'מפעיל מנוע…';
  if(!D?.live){
    const ok = await startEngine();
    if(!ok){ if(st) st.textContent='✗ המנוע לא עלה'; return; }
  }
  if(st) st.textContent = 'פותח דפדפן…';
  try{
    const r = await fetch('/api/proxy/actions/browser/show',{method:'POST'});
    if(r.ok || r.status < 500){
      if(st) st.textContent = '✓ דפדפן פתוח — התחבר ואז לחץ "הורד JSON"';
      if(btn) btn.style.background='rgba(16,185,129,.2)';
    } else throw new Error(r.status);
  }catch(e){ if(st) st.textContent='✗ '+e.message; }
}

async function _expDownload(){
  const st = $('exp-status');
  if(st) st.textContent = 'מייצא…';
  try{
    const r = await fetch('/api/tools/export_session?portal=NET');
    if(!r.ok){
      const d = await r.json().catch(()=>({}));
      if(r.status===503) throw new Error('לא מחובר — התחבר לנט המשפט בדפדפן שנפתח');
      throw new Error(d.detail || r.status);
    }
    const data = await r.json();
    const blob = new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `session_${Date.now()}.json`;
    a.click();
    if(st) st.textContent = '✓ קובץ JSON הורד — שלח לעמית';
    setTimeout(()=>{ if(_exportSessionPanel){_exportSessionPanel.remove();_exportSessionPanel=null;} }, 3000);
  }catch(err){ if(st) st.textContent='✗ '+err.message; }
}

async function _inviteCookies(){
  const st = $('invite-status'), btn = $('invite-cookies-btn'), row = $('invite-url-row');
  if(st) st.textContent = 'מפעיל session_server ו-ngrok…';
  if(btn) btn.disabled = true;
  try{
    const r = await fetch('/api/profiles/invite_cookies',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:'{}'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || r.status);
    const inp = $('invite-url-val');
    if(inp) inp.value = d.url;
    if(row) row.style.display = 'block';
    if(st) st.textContent = '✓ קישור פעיל — שלח ללקוח';
  }catch(err){
    if(st) st.textContent = '✗ '+err.message;
    if(btn) btn.disabled = false;
  }
}

function _copyInviteUrl(){
  const v = $('invite-url-val')?.value;
  if(!v) return;
  navigator.clipboard.writeText(v).then(()=>toast('הקישור הועתק ✓'));
}

async function _stopInvite(){
  await fetch('/api/profiles/invite_stop',{method:'POST'}).catch(()=>{});
  const row = $('invite-url-row'), st = $('invite-status'), btn = $('invite-cookies-btn');
  if(row) row.style.display='none';
  if(st) st.textContent='';
  if(btn) btn.disabled=false;
  toast('קישור ה-ngrok נסגר');
}

async function _profileOpenNet(){
  const st = $('profile-cookie-status');
  if(st) st.textContent = 'מפעיל מנוע…';
  // Start engine if not running (same as startEngine() but inline)
  try{ await fetch('/api/system/start',{method:'POST'}); }catch{}
  // Wait up to 20s for engine to be alive
  let alive = false;
  for(let i=0;i<10;i++){
    await new Promise(r=>setTimeout(r,1500));
    try{ const h=await(await fetch('/api/health')).json(); if(h.full_ui_alive){alive=true;break;} }catch{}
  }
  if(!alive){ if(st) st.textContent='✗ המנוע לא עלה — נסה להפעיל ידנית'; return; }
  // Show the NET browser window via the proxy
  if(st) st.textContent = 'פותח נט המשפט…';
  try{
    const r = await fetch('/api/proxy/actions/browser/show',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({portal:'NET'})
    });
    if(r.ok || r.status < 500)
      if(st) st.textContent = '✓ דפדפן נט המשפט נפתח — התחבר עם פרטי הלקוח, ואז לחץ "ייצא עוגיות"';
    else throw new Error(r.status);
  }catch(err){
    if(st) st.textContent = '✗ '+err.message;
  }
}

function _onProfileCookieChosen(input){
  _profileCookieData = null;
  const st = $('profile-cookie-status');
  if(!input.files?.[0]){ if(st) st.textContent=''; return; }
  const reader = new FileReader();
  reader.onload = e => {
    try{
      const d = JSON.parse(e.target.result);
      if(!d.portal || !d.storage_state?.cookies?.length)
        throw new Error('קובץ לא תקין — חסר portal או cookies');
      _profileCookieData = d;
      if(st) st.textContent = `✓ ${d.portal} · ${d.storage_state.cookies.length} עוגיות — לחץ "ייבא"`;
    }catch(err){ if(st) st.textContent = '✗ '+err.message; }
  };
  reader.readAsText(input.files[0]);
}

async function _exportProfileCookies(portal){
  const st = $('profile-cookie-status');
  if(st) st.textContent = `מייצא ${portal}…`;
  try{
    // Ensure engine is up before exporting
    await fetch('/api/system/start',{method:'POST'}).catch(()=>{});
    const r = await fetch(`/api/tools/export_session?portal=${portal}`);
    if(!r.ok){
      const err = (await r.json().catch(()=>({}))).detail || r.status;
      if(r.status === 503)
        throw new Error('דפדפן לא פתוח — לחץ "פתח נט המשפט", התחבר, ואז נסה שוב');
      throw new Error(err);
    }
    const data = await r.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `session_${portal.toLowerCase()}_${Date.now()}.json`;
    a.click();
    if(st) st.textContent = `✓ ${portal} יוצא — שלח ללקוח`;
  }catch(err){ if(st) st.textContent = '✗ '+err.message; }
}

async function _importProfileCookies(){
  const st = $('profile-cookie-status');
  if(!_profileCookieData){ if(st) st.textContent='בחר קובץ JSON קודם'; return; }
  if(st) st.textContent = 'מייבא…';
  try{
    const r = await fetch('/api/actions/import_session',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(_profileCookieData)
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(d.detail||d.error||r.status);
    _profileCookieData = null;
    if(st) st.textContent = '✓ עוגיות יובאו לפרופיל — ניתן להוריד תיקים';
  }catch(err){ if(st) st.textContent = '✗ '+err.message; }
}

async function _createProfile(){
  const nameEl = $('profile-new-name');
  const st = $('profile-create-status');
  const name = nameEl?.value?.trim();
  if(!name){ if(st) st.textContent='נדרש שם'; return; }
  if(st) st.textContent = 'יוצר…';
  try{
    const r = await fetch('/api/profiles/create',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})
    }).then(d=>d.json());
    if(r.ok){
      if(st) st.textContent = `✓ נוצר: ${r.profile.name}`;
      if(nameEl) nameEl.value = '';
      setTimeout(()=>{ $('profile-mgr')?.remove(); openProfileManager(); }, 500);
    } else {
      if(st) st.textContent = '✗ ' + (r.error||'שגיאה');
    }
  }catch(e){ if(st) st.textContent = '✗ שגיאת רשת'; }
}

async function _activateProfile(id){
  const st = $('profile-create-status');
  if(st) st.textContent = 'מפעיל פרופיל (עוצר הורדות פעילות)…';
  try{
    const r = await fetch('/api/profiles/activate',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id})
    }).then(d=>d.json());
    if(r.ok){
      _updateProfileBanner(r.profile);
      if(r.paused?.length) toast(`עצרתי ${r.paused.length} הורדות — ניתן לחדש לאחר מכן`, false);
      $('profile-mgr')?.remove();
      openProfileManager();
    } else {
      toast('שגיאה: ' + (r.error||''), true);
    }
  }catch(e){ toast('שגיאת רשת', true); }
}

async function _deactivateAndRefreshMgr(){
  await deactivateProfile();
  $('profile-mgr')?.remove();
  openProfileManager();
}

async function deactivateProfile(){
  try{
    await fetch('/api/profiles/deactivate',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:'{}'});
  }catch{}
  _updateProfileBanner(null);
  toast('חזרת למשתמש הראשי', false);
}

async function _deleteProfile(id, name){
  if(!confirm(`למחוק את הפרופיל "${name}"?\n(הקבצים שהורדו ישארו בתיקייה)`)) return;
  try{
    const r = await fetch('/api/profiles/delete',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id})
    }).then(d=>d.json());
    if(r.ok){ $('profile-mgr')?.remove(); openProfileManager(); }
    else toast('שגיאה: '+(r.error||''), true);
  }catch{ toast('שגיאת רשת', true); }
}

// Restore active profile banner on page load
(async ()=>{
  try{
    const d = await fetch('/api/profiles').then(r=>r.json());
    if(d.active) _updateProfileBanner(d.active);
  }catch{}
})();

// ── Browser window quick toggle ─────────────────────────────────────────────
async function _toggleBrowserWindow(){ toggleRealBrowser(); }

// Start the unified state poller once the page is ready
document.addEventListener('DOMContentLoaded', _startStatePoller);

// Esc closes any open floating panel or modal
document.addEventListener('keydown', e=>{
  if(e.key!=='Escape') return;
  if($('logwin'))   { toggleLogWin(); return; }
  if($('taskswin')) { toggleTasksWin(); return; }
  if($('bwin'))     { toggleBrowserWin(); return; }
  if($('otp-box'))  { $('otp-box').remove(); return; }
  if(typeof closeSettings==='function' && $('settings')?.style.display!=='none') closeSettings();
});
