/* ─── settings tabs ───────────────────────────────────────────────────────
   The panel used to be one long scroll of every field. It is now grouped into
   logical steps; each tab shows ✓ once that step is actually configured, so the
   setup state is visible at a glance and any step can be revisited later. */
function setTab(name){
  document.querySelectorAll('#set-tabs .set-tab')
    .forEach(b=>b.classList.toggle('on', b.dataset.tab===name));
  document.querySelectorAll('.set-pane')
    .forEach(p=>{ p.hidden = p.dataset.pane!==name; });
  const box=$('settings'); if(box) box.scrollTop=0;
  try{ localStorage.setItem('lias_set_tab', name); }catch(_){}
}
/* Mark each tab ✓/• from the status the panel already loaded. */
function refreshSetupMarks(){
  const done = t => {
    const el=$('set-ok-'+t.k); if(!el) return;
    el.textContent = t.ok ? '✓' : '';
    el.title = t.ok ? 'הוגדר' : 'עדיין לא הוגדר';
  };
  const has = (id, needle) => {
    const el=$(id); return !!(el && (el.textContent||'').includes(needle));
  };
  done({k:'account', ok: has('totp-status','✓') || has('google-status','✓')});
  done({k:'govil',   ok: has('gov-status','✓')});
  done({k:'share',   ok: !!(($('g-share')||{}).value||'').trim()});
  done({k:'downloads', ok: true});
  done({k:'general', ok: true});
}

/* ─── settings: gov.il credentials ─── */
async function openSettings(){
  $('settings').style.display='block'; $('set-bg').classList.add('on');
  document.body.style.overflow='hidden';   // lock background scroll — scroll the modal, not the page
  $('settings').scrollTop=0;
  let _tab='account';
  try{ _tab = localStorage.getItem('lias_set_tab') || 'account'; }catch(_){}
  if(!document.querySelector(`#set-tabs .set-tab[data-tab="${_tab}"]`)) _tab='account';
  setTab(_tab);
  try{
    const s = await (await fetch('/api/govil/status')).json();
    $('gov-status').innerHTML = s.configured
      ? '<span style="color:var(--accent-strong)">אישורי gov.il מוגדרים ✓</span>'
      : '<span style="color:var(--warn)">אישורי gov.il עדיין לא הוגדרו</span>';
  }catch(e){ $('gov-status').textContent=''; }
  try{
    const st = await (await fetch('/api/settings')).json();
    if(st.login_method){ $('g-method').value = st.login_method;
      const mn=$('manual-auth-note'); if(mn) mn.style.display = st.login_method==='manual'?'block':'none'; }
    if(st.otp_method) $('g-otp').value = st.otp_method;
    if(st.share_email!==undefined) $('g-share').value = st.share_email||'';
    if(st.user_mode && $('g-usermode')) $('g-usermode').value = st.user_mode;
    if($('g-net-related')) $('g-net-related').checked = !!st.net_related;
    if($('g-browser-visible')) $('g-browser-visible').checked = st.browser_visible !== false;
    if(st.otp_method && $('g-otp')) $('g-otp').value = st.otp_method;
    if($('g-otp-source')) $('g-otp-source').value = st.otp_source || 'email';
    const ts=$('totp-status');
    if(ts) ts.innerHTML = st.totp_configured
      ? '<span style="color:var(--accent-strong)">✓ סוד TOTP מוגדר</span>'
      : '<span class="sub">לא הוגדר — הזן סוד מ-Google Authenticator כדי להשתמש</span>';
  }catch(e){}
  // email account status
  try{
    const em = await (await fetch('/api/email/status')).json();
    const es=$('email-status');
    if(es) es.innerHTML = em.configured
      ? `<span style="color:var(--accent-strong)">✓ מוגדר: ${em.address}</span>`
      : '<span style="color:var(--warn,#e6a800)">עדיין לא הוגדר — קוד המייל לא ייקרא אוטומטית עד שתגדיר</span>';
    if(em.address && $('g-email')) $('g-email').value = em.address;
  }catch(e){}
  toggleEmailBox();
  if(typeof loadLoginAudit==='function') loadLoginAudit();
  if(typeof loadGoogleStatus==='function') loadGoogleStatus();
  if($('g-lang')) $('g-lang').value = (typeof curLang==='function'?curLang():'he');
  if($('g-feedback')) $('g-feedback').checked = localStorage.getItem('lias_feedback')!=='0';
  refreshSetupMarks();
  // the Google/audit fetches above resolve after this point — re-mark once they land
  setTimeout(refreshSetupMarks, 1200);
}
function _syncNetRelated(){ /* scope now lives on the sync screen */ }
function toggleEmailBox(){
  const box=$('email-box'); if(!box) return;
  box.style.display = ($('g-otp')?.value==='sms') ? 'none' : 'block';
}
async function saveEmail(){
  const address=($('g-email')?.value||'').trim();
  const app_password=($('g-email-pw')?.value||'').trim();
  if(!address){ toast('נא להזין כתובת מייל', true); return; }
  try{
    const r = await (await fetch('/api/email/save',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({address, app_password})})).json();
    if(r.ok){
      toast('חשבון המייל נשמר ✓');
      $('g-email-pw').value='';
      const es=$('email-status'); if(es) es.innerHTML=`<span style="color:var(--accent-strong)">✓ מוגדר: ${address}</span>`;
    } else toast('שגיאה: '+(r.error||''), true);
  }catch(e){ toast('שגיאה בשמירה', true); }
}
async function saveSyncSettings(){
  // Scope/open-filter moved to the sync screen (single owner) — only the
  // settings that are NOT a per-run decision are saved from here.
  const body = {
    net_related: !!$('g-net-related')?.checked,
    browser_visible: !!$('g-browser-visible')?.checked,
  };
  const ok=$('g-sync-ok');
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if(r.ok){
      if(ok){ ok.style.color='var(--accent-strong)'; ok.textContent='✓ נשמר. שינוי "הצג דפדפן" יחול לאחר אתחול המנוע.'; }
      toast('הגדרות הסנכרון נשמרו ✓');
    } else { if(ok){ ok.style.color='var(--danger)'; ok.textContent='✗ המנוע כבוי — הפעל אותו כדי לשמור'; } }
  }catch(e){ if(ok){ ok.style.color='var(--danger)'; ok.textContent='✗ שגיאה בשמירה'; } }
}
async function saveOtpMethod(v){
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({otp_method:v})});
    toast(r.ok? (v==='sms'?'קוד אימות יגיע לטלפון — הזנה ידנית ✓':'קוד אימות ייקרא מהמייל אוטומטית ✓')
              : 'המנוע כבוי — הפעל אותו כדי לשמור', !r.ok);
  }catch(e){ toast('המנוע כבוי — הפעל אותו כדי לשמור', true); }
}
async function saveOtpSource(v){
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({otp_source:v})});
    toast(r.ok? (v==='totp'?'מקור הקוד: Google Authenticator ✓':'מקור הקוד: מייל ✓')
              : 'שגיאה בשמירה', !r.ok);
  }catch(e){ toast('שגיאה בשמירה', true); }
}
async function saveTotp(){
  const secret=($('g-totp-secret')?.value||'').trim();
  try{
    const r=await fetch('/api/totp/save',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({secret})});
    const d=await r.json();
    const st=$('totp-status');
    if(d.ok && d.configured){
      if(st){ st.style.color='var(--accent-strong)'; st.textContent='✓ סוד TOTP נשמר — אפשר לבחור "Google Authenticator" כמקור הקוד'; }
      if($('g-totp-secret')) $('g-totp-secret').value='';
      toast('סוד TOTP נשמר ✓');
    } else if(d.ok && !secret){
      if(st){ st.style.color='var(--ink-soft)'; st.textContent='הסוד נמחק'; }
      toast('סוד TOTP נמחק');
    } else {
      if(st){ st.style.color='var(--danger)'; st.textContent='✗ סוד לא תקין (base32) — בדוק והדבק שוב'; }
      toast('סוד TOTP לא תקין', true);
    }
  }catch(e){ toast('שגיאה בשמירת TOTP', true); }
}
async function saveGoogleLogin(){
  const client_id=($('g-google-client')?.value||'').trim();
  const allowed=($('g-google-allowed')?.value||'').trim();
  try{
    const r=await fetch('/api/google/save',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({client_id, allowed_emails:allowed})});
    const d=await r.json();
    const st=$('google-status');
    const _ci=$('g-google-client'); if(_ci) delete _ci.dataset.editing;
    if(d.ok){
      if(st){ st.style.color='var(--accent-strong)';
        st.textContent = d.configured
          ? `✓ מוגדר${(d.allowed_emails||[]).length?` · ${d.allowed_emails.length} מיילים מורשים`:' · כל חשבון Google'}`
          : 'נוקה'; }
      toast('הגדרות Google נשמרו ✓ — רענן את הדף כדי לראות את כפתור הכניסה');
    } else { toast('שגיאה בשמירה', true); }
  }catch(e){ toast('שגיאה בשמירה', true); }
}
/* The Client ID is NOT a secret: Google requires the page itself to pass it to
   google.accounts.id.initialize(), so it is always readable in the browser and
   cannot live in the Keychain like a password. What we can do is stop showing
   the raw string by default — collapse it to "✓ מוגדר" with a masked preview
   and a שנה button, the same shape as the real secrets. */
function _maskClientId(id){
  if(!id) return '';
  const head = id.slice(0, 8), tail = id.slice(-28);   // ….apps.googleusercontent.com
  return `${head}…${tail}`;
}
function editGoogleClientId(){
  const inp=$('g-google-client'); if(!inp) return;
  inp.dataset.editing='1';
  inp.type='text'; inp.style.display='';
  inp.value=''; inp.placeholder='הדבק Client ID חדש…'; inp.focus();
  const v=$('g-google-client-view'); if(v) v.style.display='none';
}
async function loadGoogleStatus(){
  try{
    const d=await (await fetch('/api/google/status')).json();
    const inp=$('g-google-client'), view=$('g-google-client-view');
    if(inp && view){
      if(d.configured && d.client_id && inp.dataset.editing!=='1'){
        inp.style.display='none';
        view.style.display='flex';
        view.innerHTML =
          `<code style="flex:1;font-size:11.5px;opacity:.75;direction:ltr;text-align:left;
                        overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
           >${_maskClientId(d.client_id)}</code>
           <button type="button" class="btn" style="font-size:11px;padding:4px 12px"
                   onclick="editGoogleClientId()">שנה</button>`;
      } else {
        inp.style.display=''; view.style.display='none';
        if(inp.dataset.editing!=='1') inp.value = d.client_id||'';
      }
    }
    if($('g-google-allowed')) $('g-google-allowed').value = (d.allowed_emails||[]).join(', ');
    const st=$('google-status');
    if(st) st.innerHTML = d.configured
      ? `<span style="color:var(--accent-strong)">✓ מוגדר${(d.allowed_emails||[]).length?` · ${d.allowed_emails.length} מיילים מורשים`:' · כל חשבון Google'}</span>`
      : '<span class="sub">לא הוגדר — הזן Client ID כדי לאפשר כניסה עם Google</span>';
  }catch(_){}
}
async function loadLoginAudit(){
  const el=$('login-audit-list'); if(!el) return;
  el.innerHTML='<div class="sub">טוען…</div>';
  try{
    const d=await (await fetch('/api/login_audit')).json();
    const rows=d.entries||[];
    if(!rows.length){ el.innerHTML='<div class="sub">אין רשומות עדיין</div>'; return; }
    const ICON={success:'✓',failed:'✗',blocked:'⛔',start:'→',otp_sent:'✉'};
    const COLOR={success:'var(--accent-strong)',failed:'var(--danger)',blocked:'#e6a800'};
    el.innerHTML=rows.map(r=>`<div style="display:flex;gap:8px;padding:4px 0;border-bottom:1px solid var(--line)">
      <span style="color:${COLOR[r.status]||'inherit'};font-weight:700">${ICON[r.status]||'·'}</span>
      <span style="width:120px;opacity:.7">${r.ts||''}</span>
      <b style="width:44px">${r.portal||''}</b>
      <span style="width:74px;opacity:.8">${r.method||''}</span>
      <span style="flex:1;opacity:.7">${r.status||''}${r.detail?' · '+r.detail:''}</span>
    </div>`).join('');
  }catch(e){ el.innerHTML='<div class="sub" style="color:var(--danger)">שגיאה בטעינה</div>'; }
}
async function saveShareEmail(v){
  v=(v||'').trim();
  const emails = v.split(/[,;\s]+/).filter(Boolean);
  const bad = emails.filter(e=>!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e));
  const ok = $('g-share-ok');
  if(bad.length){ if(ok){ok.style.color='var(--danger)';ok.textContent='מייל לא תקין: '+bad.join(', ');} return; }
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({share_email:emails.join(', ')})});
    const good = r.ok;
    if(ok){ ok.style.color = good?'var(--accent-strong)':'var(--danger)';
      ok.textContent = good? (emails.length? '✓ יישותף עם: '+emails.join(', ') : 'השיתוף בוטל')
                           : '✗ המנוע כבוי — הפעל אותו כדי לשמור'; }
    toast(good? 'הגדרת שיתוף הדרייב נשמרה ✓' : 'המנוע כבוי', !good);
  }catch(e){ if(ok){ok.style.color='var(--danger)';ok.textContent='✗ המנוע כבוי';} }
}
async function saveUserMode(v){
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({user_mode:v})});
    toast(r.ok? 'סוג המשתמש נשמר ✓' : 'המנוע כבוי — הפעל אותו כדי לשמור', !r.ok);
  }catch(e){ toast('המנוע כבוי — הפעל אותו כדי לשמור', true); }
}
async function saveCaseScope(v){
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({case_scope:v, download_related_cases:v==='related'})});
    toast(r.ok? 'היקף ההורדה נשמר ✓' : 'המנוע כבוי — הפעל אותו כדי לשמור', !r.ok);
  }catch(e){ toast('המנוע כבוי — הפעל אותו כדי לשמור', true); }
}
async function saveLoginMethod(v){
  const mn=$('manual-auth-note'); if(mn) mn.style.display = v==='manual'?'block':'none';
  try{
    const r = await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({login_method:v})});
    toast(r.ok? 'שיטת ההתחברות נשמרה ✓' : 'המנוע כבוי — הפעל אותו כדי לשמור', !r.ok);
  }catch(e){ toast('המנוע כבוי — הפעל אותו כדי לשמור', true); }
}
function togglePw(){
  const p=$('g-pw'); p.type = p.type==='password' ? 'text' : 'password';
  $('pw-eye').style.opacity = p.type==='text' ? 1 : .55;
}
function closeSettings(){ $('settings').style.display='none'; $('set-bg').classList.remove('on'); document.body.style.overflow=''; }
async function saveGovil(){
  const id=$('g-id').value.trim(), pw=$('g-pw').value;
  if(!id||!pw){ toast('נא למלא ת.ז. וסיסמה', true); return; }
  const r = await (await fetch('/api/govil/save',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({id, password:pw})})).json();
  if(r.ok){ toast('נשמר ב-Keychain ✓'); $('g-id').value=''; $('g-pw').value=''; closeSettings(); }
  else toast('שגיאה: '+(r.error||''), true);
}
