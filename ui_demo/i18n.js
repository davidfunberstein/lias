/* ─── i18n — bilingual UI (he default / en) ───────────────────────────────
   HOW TO KEEP THIS UP FOREVER (the practice):
   1. Every user-visible string you ADD to the UI gets a key here in BOTH
      languages (he + en). Hebrew is the source of truth.
   2. Static HTML: add data-i18n="key" to the element; applyLang() swaps it.
   3. Dynamic JS strings: use T('key') instead of a literal.
   4. Before a release, run in the console: i18nMissing() — lists keys that
      exist in he but not en, so nothing ships untranslated.
   5. The language choice persists in localStorage ('lias_lang') and flips
      the document direction automatically (rtl/ltr).                      */
const I18N = {
  he: {
    nav_dash:'דשבורד', nav_cases:'תיקים', nav_sync:'סנכרון', nav_transcribe:'תמלול',
    nav_docs:'מסמכים', nav_ai:'עוזר AI', soon:'בקרוב',
    search_ph:'חפש לקוח, תיק, צד או ערכאה…',
    settings:'הגדרות', close:'סגור', save:'שמור', cancel:'בטל',
    sync_title:'סנכרון — הורדת תיקים',
    plat_net:'נט המשפט', plat_bdr:'בית הדין הרבני', plat_eca:'הוצאה לפועל',
    fav_title:'★ תיקים במעקב',
    no_data:'אין נתונים עדיין — בצע סנכרון ראשון',
    open_case:'תיק פתוח', closed_case:'תיק סגור',
    docs:'מסמכים', requests:'בקשות', decisions:'החלטות',
  },
  en: {
    nav_dash:'Dashboard', nav_cases:'Cases', nav_sync:'Sync', nav_transcribe:'Transcribe',
    nav_docs:'Documents', nav_ai:'AI Assistant', soon:'soon',
    search_ph:'Search client, case, party or court…',
    settings:'Settings', close:'Close', save:'Save', cancel:'Cancel',
    sync_title:'Sync — download cases',
    plat_net:'Net-HaMishpat', plat_bdr:'Rabbinical Court', plat_eca:'Enforcement (ECA)',
    fav_title:'★ Watched cases',
    no_data:'No data yet — run a first sync',
    open_case:'Open', closed_case:'Closed',
    docs:'Documents', requests:'Motions', decisions:'Decisions',
  },
};
function curLang(){ return localStorage.getItem('lias_lang')||'he'; }
function T(key){ return (I18N[curLang()]||{})[key] ?? I18N.he[key] ?? key; }
function setLang(l){
  localStorage.setItem('lias_lang', l);
  applyLang();
  if(typeof render==='function') render();
}
function applyLang(){
  const l = curLang();
  document.documentElement.lang = l;
  document.documentElement.dir = l==='he' ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    el.textContent = T(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{
    el.placeholder = T(el.dataset.i18nPh);
  });
}
function i18nMissing(){
  const he=Object.keys(I18N.he), en=new Set(Object.keys(I18N.en));
  const miss=he.filter(k=>!en.has(k));
  console.log(miss.length? 'Missing EN keys: '+miss.join(', ') : '✓ all keys translated');
  return miss;
}
/* ── auto-translation of dynamically-rendered Hebrew ─────────────────────
   The UI renders most strings from JS literals (Hebrew). Instead of touching
   every render call, when lang=en we walk the DOM after each render and swap
   known Hebrew literals via HE2EN. New strings: add them to HE2EN too.     */
const HE2EN = {
  'דשבורד':'Dashboard','תיקים':'Cases','סנכרון':'Sync','תמלול':'Transcription',
  'מסמכים':'Documents','עוזר AI':'AI Assistant','בקרוב':'soon',
  'לקוחות':'Clients','תיקים פעילים':'Active cases','סה"כ מסמכים':'Total documents',
  'מסמכים לפי חודש':'Documents by month','פילוח לפי ערכאה':'By court',
  'לפי תאריך הגשה בפורטל':'By portal filing date',
  'לחץ על ערכאה לרשימת התיקים':'Click a court to list its cases',
  'לחץ לרשימת הלקוחות':'Click to list clients',
  'ברוך שובך, דוד':'Welcome back, David','מוכן לנהל את התיקים שלך?':'Ready to manage your cases?',
  'סנכרון — הורדת תיקים':'Sync — download cases',
  'חיבור לפורטלים והורדת תיקים':'Connect to portals & download cases',
  'בחר תחילה מאיזה פורטל להוריד, ואז מה להוריד.':'First pick a portal, then what to download.',
  'נט המשפט':'Net-HaMishpat','בית הדין הרבני':'Rabbinical Court','הוצאה לפועל':'Enforcement (ECA)',
  'הורד מנט המשפט':'Download from Net-HaMishpat','הורד מבית הדין הרבני':'Download from Rabbinical Court',
  'הורד מהוצאה לפועל — הצג רשימה':'Download from ECA — show list',
  'התחברות':'Connecting','פותח פורטל ECA':'Opening ECA portal',
  'תיק פתוח':'Open case','תיק סגור':'Closed case','בקשות':'Motions','החלטות':'Decisions',
  'הגדרות':'Settings','שמור':'Save','סגור':'Close','בטל':'Cancel','מחק':'Delete',
  'הורדה':'Download','העתק':'Copy','הגדל':'Maximize','רענן':'Refresh',
  'משימות':'Tasks','עצור':'Stop','עצור הכל':'Stop all','אין משימות פעילות':'No active tasks',
  'אין נתונים':'No data','אין נתונים עדיין — בצע סנכרון ראשון':'No data yet — run a first sync',
  'עודכן':'Updated','לפני':'ago','ימים':'days','היום':'today','אתמול':'yesterday',
  'הקלטות שמורות':'Saved recordings','התחל תמלול':'Start transcription','המשך':'Resume','השמעה':'Play',
  'תמלולים':'Transcriptions','הושלם':'Completed','נעצר':'Stopped',
  'ערכאה':'Court','צדדים':'Parties','עיר':'City','סטטוס':'Status',
  'תיקים במעקב':'Watched cases','חיפוש':'Search','תוצאות':'Results',
  'יומן פעולות':'Activity log','משוב':'Feedback','שלח':'Send',
};
function autoTranslate(root){
  if(curLang()!=='en') return;
  const w=document.createTreeWalker(root||document.body, NodeFilter.SHOW_TEXT);
  let n;
  while(n=w.nextNode()){
    const t=n.textContent.trim();
    if(t && HE2EN[t]) n.textContent = n.textContent.replace(t, HE2EN[t]);
  }
}
/* re-translate after every render — hook into DOM mutations, throttled */
let _i18nTick=null;
new MutationObserver(()=>{
  if(curLang()!=='en' || _i18nTick) return;
  _i18nTick=setTimeout(()=>{ _i18nTick=null; autoTranslate(); }, 150);
}).observe(document.documentElement, {childList:true, subtree:true});
const _applyLangOrig = applyLang;
applyLang = function(){ _applyLangOrig(); autoTranslate(); };
document.addEventListener('DOMContentLoaded', applyLang);
