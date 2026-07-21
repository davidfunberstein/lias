/* ─── settings: gov.il credentials ─── */
async function openSettings(){
  $('settings').style.display='block'; $('set-bg').classList.add('on');
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
    // per-platform sync settings
    if($('g-net-scope')) $('g-net-scope').value = st.net_scope || 'selected';
    if($('g-net-related')) $('g-net-related').checked = !!st.net_related;
    if($('g-bdr-scope')) $('g-bdr-scope').value = st.bdr_scope || 'all';
    if($('g-eca-scope')) $('g-eca-scope').value = st.eca_scope || 'selected';
    if($('g-browser-visible')) $('g-browser-visible').checked = st.browser_visible !== false;
  }catch(e){}
}
async function saveSyncSettings(){
  const body = {
    net_scope: $('g-net-scope')?.value,
    net_related: !!$('g-net-related')?.checked,
    bdr_scope: $('g-bdr-scope')?.value,
    eca_scope: $('g-eca-scope')?.value,
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
async function saveGroq(){
  const key=$('g-groq').value.trim();
  if(!key){ toast('נא להדביק מפתח', true); return; }
  const r = await (await fetch('/api/ocr/save',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({groq_key:key})})).json();
  if(r.ok){ toast('מפתח Groq נשמר ב-Keychain ✓'); $('g-groq').value=''; }
  else toast('שגיאה: '+(r.error||''), true);
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
async function testGroq(){
  const el=$('g-groq-status');
  if(el){ el.style.color='var(--ink-soft)'; el.textContent='בודק…'; }
  try{
    const r = await (await fetch('/api/proxy/ocr/test')).json();
    if(el){
      el.style.color = r.ok ? 'var(--accent-strong)' : 'var(--danger)';
      el.textContent = r.ok ? `✓ ${r.provider} עובד (${r.reply||''})`
                            : `✗ ${r.error||'המפתח לא עובד'}`;
    }
  }catch(e){ if(el){ el.style.color='var(--danger)'; el.textContent='✗ המנוע כבוי'; } }
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
function closeSettings(){ $('settings').style.display='none'; $('set-bg').classList.remove('on'); }
async function saveGovil(){
  const id=$('g-id').value.trim(), pw=$('g-pw').value;
  if(!id||!pw){ toast('נא למלא ת.ז. וסיסמה', true); return; }
  const r = await (await fetch('/api/govil/save',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({id, password:pw})})).json();
  if(r.ok){ toast('נשמר ב-Keychain ✓'); $('g-id').value=''; $('g-pw').value=''; closeSettings(); }
  else toast('שגיאה: '+(r.error||''), true);
}
