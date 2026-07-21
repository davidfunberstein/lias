"""Bridge to the existing collector — reuse, don't rewrite.
גשר לקוד האיסוף הקיים — שימוש חוזר, לא שכתוב.

EN: The existing core/ entry points already take a live Playwright `page`
    (run_net_download(page, dir), run_bdr_download(page, dir)) — a perfect
    fit for BrowserManager.run(fn) where fn(page) is executed on the browser
    thread. Integration strategy:

      1. Job handlers below wrap the legacy functions as browser commands.
      2. The legacy code keeps writing its CSVs (untouched, zero risk).
      3. After each sync we RE-IMPORT the affected folder into SQLite
         (reusing migrate_csv), so the DB and the UI update automatically.
      4. A lightweight `scan` job reads the NET grid store JSON only
         (no downloads) → snapshot diff → "what's new" in the UI.

HE: נקודות הכניסה הקיימות ב-core/ כבר מקבלות `page` חי של Playwright
    (run_net_download(page, dir), run_bdr_download(page, dir)) — התאמה
    מושלמת ל-BrowserManager.run(fn) שבו fn(page) רץ על Thread הדפדפן.
    אסטרטגיית האינטגרציה:

      1. המטפלים כאן עוטפים את הפונקציות הישנות כפקודות דפדפן.
      2. הקוד הישן ממשיך לכתוב את ה-CSV שלו (לא נגענו, אפס סיכון).
      3. אחרי כל סנכרון מייבאים מחדש את התיקייה שהושפעה ל-SQLite
         (שימוש חוזר ב-migrate_csv), כך שה-DB וה-UI מתעדכנים אוטומטית.
      4. משימת `scan` קלה קוראת רק את ה-JSON של גריד נט המשפט
         (בלי הורדות) ← diff על Snapshot ← "מה חדש" ב-UI.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, db, jobs, snapshot
from .jobs import handler, JobContext

# EN: legacy imports are lazy + guarded so LIAS runs (UI, DB, jobs) even when
#     playwright isn't installed; browser jobs will then fail with a clear
#     message instead of killing the process.
# HE: הייבוא של הקוד הישן עצל ומוגן, כך ש-LIAS רצה (UI, DB, משימות) גם בלי
#     playwright מותקן; משימות דפדפן ייכשלו עם הודעה ברורה במקום להפיל הכל.


def _reimport_folder(case_dir: Path) -> int:
    """Refresh SQLite from a folder's CSVs after a legacy sync run.
    רענון SQLite מקבצי ה-CSV של תיקייה אחרי ריצת סנכרון ישנה."""
    from .migrate_csv import _find_manifest_dirs, _import_manifest
    downloads_root = config.COURT_DOCS_DIR / "downloads"
    n = 0
    for d, csv_path in _find_manifest_dirs(case_dir):
        docs, _ = _import_manifest(d, csv_path, downloads_root)
        n += docs
    return n


def _drive_sync(ctx: JobContext, why: str = "") -> None:
    """EN: push new/changed files to Google Drive if storage_mode is both/cloud.
        Logs every step so the user can see it in the live log. Called after
        every sync so Drive stays current without a separate click.
    HE: העלאה לדרייב אם המצב both/cloud. מתועד ביומן. נקרא אחרי כל סנכרון."""
    try:
        from core.download import SESSION_SETTINGS as _ss
        mode = _ss.get("storage_mode", "local")
    except Exception:
        mode = "local"
    if mode not in ("both", "cloud"):
        ctx and jobs.broadcast({"type": "job", "job_id": getattr(ctx, "job_id", 0),
                                "message": "דרייב כבוי (אחסון=מקומי) — דלג על העלאה"})
        return
    jobs.broadcast({"type": "job", "job_id": getattr(ctx, "job_id", 0),
                    "message": f"מעלה לדרייב… {why}"})
    try:
        from core.gdrive import run_smart_gdrive_upload
        creds = config.PROJECT_ROOT / "credentials.json"
        token = config.PROJECT_ROOT / "token.json"
        if not creds.exists():
            jobs.broadcast({"type": "job", "job_id": getattr(ctx, "job_id", 0),
                            "message": "⚠ אין credentials.json — הגדר Google Drive API תחילה"})
            return
        stats = run_smart_gdrive_upload(
            root_dir=config.COURT_DOCS_DIR / "downloads",
            credentials_path=creds, token_path=token, logger=None)
        jobs.broadcast({"type": "job", "job_id": getattr(ctx, "job_id", 0),
                        "message": f"דרייב: הועלו {stats.get('uploaded',0)}, "
                                   f"דילוג {stats.get('skipped',0)}, כשל {stats.get('failed',0)}"})
    except Exception as exc:
        jobs.broadcast({"type": "job", "job_id": getattr(ctx, "job_id", 0),
                        "message": f"⚠ העלאת דרייב נכשלה: {str(exc)[:80]}"})


# ---------------------------------------------------------------------------
# NET: scan-only (no downloads) → snapshot diff / סריקה בלבד ← diff
# ---------------------------------------------------------------------------

_JS_READ_GRID = """
() => {
  // EN: same hidden store the legacy scraper reads / HE: אותו מחסן נסתר שהקוד הישן קורא
  const el = document.querySelector('#PresentDocumentGridArrayStore');
  return el ? el.textContent : null;
}
"""


@handler("net_scan")
def net_scan(payload: dict, ctx: JobContext) -> str:
    """EN: read the currently-open NET case grid, register rows in the DB,
        store a snapshot and return the diff. Assumes (per David: assume,
        don't ask) that the case page is open in the browser — either the
        lawyer navigated there, or a previous job did.
    HE: קריאת הגריד של תיק נט המשפט הפתוח כרגע, רישום השורות ב-DB,
        שמירת Snapshot והחזרת ה-diff. מניח (לפי דוד: להניח, לא לשאול)
        שדף התיק פתוח בדפדפן — או שעורך הדין ניווט לשם, או משימה קודמת.
    """
    if ctx.browser is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    sub_case_id = int(payload["sub_case_id"])

    ctx.progress(0.1, "reading grid / קורא את הגריד")

    def _read(page):
        # the store may live inside a frame / המחסן יכול לשבת בתוך frame
        raw = page.evaluate(_JS_READ_GRID)
        if raw:
            return raw
        for frame in page.frames:
            try:
                raw = frame.evaluate(_JS_READ_GRID)
                if raw:
                    return raw
            except Exception:
                continue
        raise RuntimeError("grid store not found — is a case open? / המחסן לא נמצא — תיק פתוח?")

    raw = _run_portal(ctx, "net_scan", _read, timeout=120)
    rows = json.loads(raw)
    if isinstance(rows, dict):                      # some pages wrap the array / לפעמים עטוף
        rows = rows.get("rows", rows.get("data", []))

    ctx.progress(0.5, f"{len(rows)} rows / שורות")

    # register/refresh each row in documents / רישום כל שורה בטבלת המסמכים
    for r in rows:
        db.upsert_document(
            sub_case_id,
            str(r.get("FileName") or r.get("DocumentName") or r.get("doc_id") or "")[:255],
            logical_name=str(r.get("DocumentName") or "")[:255],
            doc_type=str(r.get("DocumentType") or ""),
            submitter_est=str(r.get("CasePartyDisplayName") or ""),
            submission_date=str(r.get("PresentationDate") or ""),
        )

    diff = snapshot.take_snapshot(sub_case_id, rows)
    ctx.progress(1.0, f"diff: {diff['counts']}")
    jobs.broadcast({"type": "diff", "sub_case_id": sub_case_id, "counts": diff["counts"]})
    return f"scan ok, diff={diff['counts']}"


# ---------------------------------------------------------------------------
# Full sync via legacy engine / סנכרון מלא דרך המנוע הקיים
# ---------------------------------------------------------------------------

@handler("net_sync_current")
def net_sync_current(payload: dict, ctx: JobContext) -> str:
    """EN: run the proven legacy NET download flow on the case that is open
        in the browser right now (mirrors menu option 2), then re-import its
        CSVs into SQLite. Long command timeout — big cases are slow.
    HE: הרצת זרימת ההורדה הבדוקה של נט המשפט על התיק שפתוח בדפדפן עכשיו
        (מקביל לאפשרות 2 בתפריט), ואז ייבוא ה-CSV שלו מחדש ל-SQLite.
        Timeout ארוך לפקודה — תיקים גדולים איטיים.
    """
    if ctx.browser is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    ctx.progress(0.05, "starting legacy NET sync / מפעיל סנכרון נט המשפט")

    def _run(page):
        from core.download import run_net_download
        run_net_download(page, output_dir=config.COURT_DOCS_DIR)
        return "legacy sync done"

    _run_portal(ctx, "net_sync", _run, timeout=3600)   # up to an hour / עד שעה
    ctx.progress(0.85, "re-importing CSVs / מייבא CSV מחדש")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    _drive_sync(ctx, "אחרי סנכרון נט")
    ctx.file_event(0, "SYNC_DONE", f"{n} docs refreshed / רועננו")
    return f"net sync ok, {n} docs re-imported"


@handler("bdr_sync_current")
def bdr_sync_current(payload: dict, ctx: JobContext) -> str:
    """Same as above for BDR (menu option 1) / כנ"ל עבור בתי הדין הרבניים."""
    bdr = ctx.bdr_browser or ctx.browser
    if bdr is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    ctx.progress(0.05, "starting legacy BDR sync / מפעיל סנכרון בתי הדין")

    def _run(page):
        from core.download import run_bdr_download
        run_bdr_download(page, output_dir=config.COURT_DOCS_DIR)
        return "legacy sync done"

    _saved = ctx.browser
    ctx.browser = bdr
    _run_portal(ctx, "bdr_sync", _run, timeout=3600)
    ctx.browser = _saved
    ctx.progress(0.85, "re-importing CSVs / מייבא CSV מחדש")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    _drive_sync(ctx, "אחרי סנכרון בד\"ר")
    ctx.file_event(0, "SYNC_DONE", f"{n} docs refreshed / רועננו")
    return f"bdr sync ok, {n} docs re-imported"


@handler("organize_clients")
def organize_clients(payload: dict, ctx: JobContext) -> str:
    """EN: per-case client inference + folder reorganization + reimport.
    HE: שיוך כל תיק ללקוח שלו (הצד שחוזר בין התיקים) וסידור התיקיות."""
    from core.client_inference import reorganize_cases_by_client
    from core.download import SESSION_SETTINGS as _ss
    lawyer = payload.get("lawyer_name") or _ss.get("lawyer_name") or ""
    if not lawyer:
        return "אין שם עורך דין מוגדר — קבע lawyer_name בהגדרות"
    ctx.progress(0.2, "משייך תיקים ללקוחות…")
    moved = reorganize_cases_by_client(config.COURT_DOCS_DIR / "downloads", lawyer)
    ctx.progress(0.7, "מייבא מחדש…")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    return f"{moved} תיקים שויכו, {n} מסמכים יובאו מחדש"


@handler("eca_list")
def eca_list(payload: dict, ctx: JobContext) -> str:
    """EN: connect to ECA and broadcast the list of open cases (no download)
        so the UI can offer a checkbox picker before syncing.
    HE: התחברות להוצל"פ ושידור רשימת התיקים ל-UI לבחירה — בלי להוריד."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    ctx.progress(0.1, "מתחבר להוצאה לפועל…")

    def _run(page):
        import sys
        sys.path.insert(0, str(config.PROJECT_ROOT))
        from eca_download import _login_eca, _extract_cases, OPEN_CASES_URL
        import time as _t
        if not _login_eca(page):
            raise RuntimeError("ההתחברות להוצאה לפועל נכשלה")
        if "/home/OpenCase" not in (page.url or ""):
            page.goto(OPEN_CASES_URL, wait_until="domcontentloaded", timeout=30000)
            _t.sleep(3)
        return _extract_cases(page)

    cases = _run_portal(ctx, "eca_list", _run, timeout=600)
    jobs.broadcast({"type": "eca_cases", "cases": cases})
    return f"נמצאו {len(cases)} תיקי הוצל\"פ"


@handler("eca_sync")
def eca_sync(payload: dict, ctx: JobContext) -> str:
    """EN: download all ECA (הוצאה לפועל) cases — motions + decisions per
        process — into downloads/{client}/הוצאה לפועל/{case}/{process}/.
    HE: הורדת כל תיקי ההוצאה לפועל, בקשות והחלטות לפי הליך, לתיקיית הלקוח."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    ctx.progress(0.05, "מתחבר להוצאה לפועל…")

    def _run(page):
        import sys
        sys.path.insert(0, str(config.PROJECT_ROOT))
        from eca_download import run_eca_download
        cases = payload.get("cases") or None
        return run_eca_download(page, config.COURT_DOCS_DIR, cases_filter=cases)

    result = _run_portal(ctx, "eca_sync", _run, timeout=3600)
    ctx.progress(0.9, "מייבא לדשבורד…")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    _drive_sync(ctx, "אחרי סנכרון הוצל\"פ")
    return f"{result} · {n} מסמכים בדשבורד"


@handler("open_portal")
def open_portal(payload: dict, ctx: JobContext) -> str:
    """Open the court portal in a visible Playwright browser with auto-login."""
    portal = payload.get("portal", "NET")
    target_browser = (ctx.bdr_browser or ctx.browser) if portal == "BDR" else ctx.browser
    if target_browser is None:
        raise RuntimeError("no browser attached")
    url = {"NET": config.NET_HOME_URL, "BDR": config.BDR_FILES_URL,
           "ECA": config.ECA_URL}.get(portal, config.NET_HOME_URL)

    if payload.get("visible") and target_browser._headless:
        target_browser.show()

    def _open_and_login(page):
        from core.connection import ensure_logged_in
        ok = ensure_logged_in(page, portal)
        if not ok:
            print(f"[open_portal] login incomplete for {portal} — manual step may be needed")
        return "opened"

    # 300s — email-OTP login can take a couple of minutes / התחברות עם OTP איטית
    _saved = ctx.browser
    ctx.browser = target_browser
    _run_portal(ctx, "open_portal", _open_and_login, timeout=300)
    ctx.browser = _saved
    return f"opened {portal} with auto-login"


def _run_portal(ctx: JobContext, name: str, fn, timeout: int):
    """EN: run a portal-bound browser command with STUBBORN retries — the
        court WAF sometimes resets connections (blocked fingerprint or rate
        limiting). Escalation ladder: retry → wait → relaunch visible →
        wait longer → final try. The user asked: keep trying until it works.
    HE: הרצת פקודת פורטל עם נסיונות עקשניים — ה-WAF לפעמים חותך חיבורים.
        סולם: ניסיון חוזר ← המתנה ← חלון גלוי ← המתנה ארוכה ← ניסיון אחרון."""
    import time as _t
    from .browser_manager import BrowserDead

    _BLOCK_MARKS = ("ERR_CONNECTION_RESET", "ERR_HTTP2", "ERR_CONNECTION_CLOSED",
                    "ERR_EMPTY_RESPONSE", "ERR_TIMED_OUT", "timed out", "נתקעה")
    # (wait-before, go-visible-first) / (המתנה לפני, לעבור לגלוי קודם)
    ladder = [(0, False), (15, False), (5, True), (60, True), (120, True)]
    last_exc = None
    for attempt, (wait_s, want_visible) in enumerate(ladder, 1):
        if wait_s:
            ctx.progress(0.08, f"הפורטל חוסם — ממתין {wait_s} שניות ומנסה שוב ({attempt}/{len(ladder)})")
            _t.sleep(wait_s)
        if want_visible and ctx.browser._headless:
            ctx.progress(0.09, "עובר לדפדפן גלוי — הפורטל דורש חלון אמיתי")
            jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                            "message": "נפתח חלון דפדפן — הפורטל חסם את המצב הסמוי"})
            try:
                ctx.browser.show()
            except Exception:
                pass
        try:
            return ctx.browser.run(name, fn, timeout=timeout)
        except BrowserDead as exc:
            last_exc = exc
            if not any(k in str(exc) for k in _BLOCK_MARKS):
                raise                      # real error — not a block / שגיאה אמיתית
            ctx.progress(0.07, f"ניסיון {attempt} נחסם ({str(exc)[:80]}…)")
    # Diagnose: is the whole IP blocked, or only the browser?
    # אבחון: האם כל הכתובת חסומה, או רק הדפדפן?
    diag = _probe_net()
    raise BrowserDead(
        f"{name}: הפורטל חסם את כל הנסיונות ({len(ladder)}). {diag} "
        f"שגיאה אחרונה: {last_exc}")


def _probe_net() -> str:
    """Quick reachability probe to court.gov.il from this machine (urllib)."""
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://www.court.gov.il/ngcs.web.site/homepage.aspx",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/126.0.0.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status == 200
        if ok:
            return ("אבחון: השרת עונה למחשב הזה — החסימה ממוקדת בדפדפן האוטומציה. "
                    "המתן ~30 דקות בלי לנסות (ה-WAF משחרר חסימות זמניות) ונסה שוב.")
    except Exception as exc:
        return (f"אבחון: גם בקשה רגילה מהמחשב נחסמת ({type(exc).__name__}) — "
                "חסימת רשת/IP זמנית של הפורטל. המתן 30–60 דקות או החלף רשת "
                "(נקודה חמה מהטלפון) ונסה שוב.")
    return "אבחון: לא חד-משמעי — המתן מספר דקות ונסה שוב."


@handler("net_date_list")
def net_date_list(payload: dict, ctx: JobContext) -> str:
    """EN: list all NET cases in a date range WITHOUT downloading — the UI
        shows them with checkboxes and the user picks which to sync.
    HE: רשימת כל תיקי נט בטווח תאריכים בלי להוריד — ה-UI מציג עם צ'קבוקסים
        והמשתמש מסמן מה לסנכרן."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    years_back = int(payload.get("years_back", 12))
    ctx.progress(0.1, f"מאתר תיקים — {years_back} שנים אחורה")

    def _run(page):
        import time as _t
        from datetime import datetime, timedelta
        from core.connection import ensure_logged_in
        from core.net_search_cases import (
            navigate_to_date_search, fill_date_range_and_search,
            extract_cases_from_search_grid,
        )
        ensure_logged_in(page, "NET")
        if not navigate_to_date_search(page):
            raise RuntimeError("מסך חיפוש לפי תאריך לא נפתח")
        today = datetime.now()
        frm = (today - timedelta(days=years_back * 365)).strftime("%d/%m/%Y")
        if not fill_date_range_and_search(page, frm, today.strftime("%d/%m/%Y")):
            raise RuntimeError("החיפוש נכשל")
        return extract_cases_from_search_grid(page)

    cases = _run_portal(ctx, "net_date_list", _run, timeout=600)
    # Push straight to the UI as a live event / דחיפה ישירה ל-UI כאירוע חי
    jobs.broadcast({"type": "net_cases", "cases": cases})
    return f"נמצאו {len(cases)} תיקים בטווח"


@handler("drive_sync_now")
def drive_sync_now(payload: dict, ctx: JobContext) -> str:
    """Manual 'upload to Drive now' / העלאה לדרייב עכשיו — לפי בקשה."""
    ctx.progress(0.1, "מעלה קבצים חדשים לדרייב…")
    _drive_sync(ctx, "העלאה ידנית")
    ctx.progress(1.0, "הסתיים")
    return "drive sync done"


@handler("drive_share")
def drive_share(payload: dict, ctx: JobContext) -> str:
    """EN: share Drive access read-only. scope='all' shares the whole root;
        scope='case' shares only one case's folder. emails = list/CSV.
    HE: שיתוף צפייה בדרייב. 'all' = הכל; 'case' = תיקיית תיק אחד בלבד."""
    emails = payload.get("emails", "")
    scope = payload.get("scope", "all")
    case_folder = payload.get("case_folder", "")  # e.g. "downloads/פונברשטיין/1386836-7 …"
    if not emails:
        return "לא צוינו מיילים"
    try:
        from core.gdrive import GDriveUploader, DRIVE_ROOT_FOLDER
        import re as _re
        creds = config.PROJECT_ROOT / "credentials.json"
        token = config.PROJECT_ROOT / "token.json"
        up = GDriveUploader(creds, token, logger=None)
        if not up.authenticate():
            return "התחברות לדרייב נכשלה"
        root_id = up.get_or_create_folder(DRIVE_ROOT_FOLDER, parent_id=None)
        target_id = root_id
        label = "כל הדרייב"
        if scope in ("case", "client") and case_folder:
            # walk the mirrored path to the case/client folder
            parent = root_id
            for part in Path(case_folder).parts:
                parent = up.get_or_create_folder(part, parent_id=parent)
            target_id = parent
            label = ("הלקוח " if scope == "client" else "התיק ") + Path(case_folder).name
        shared = []
        for em in _re.split(r"[,;\s]+", emails):
            if em.strip() and up.share_readonly(target_id, em.strip()):
                shared.append(em.strip())
        return f"שותף ({label}) עם: {', '.join(shared) or 'אף אחד'}"
    except Exception as exc:
        return f"שיתוף נכשל: {exc}"


@handler("net_download_all")
def net_download_all(payload: dict, ctx: JobContext) -> str:
    """EN: download EVERY case visible to the user via date-range search —
        independent of the 'related cases' logic. Wraps net_date_search.
    HE: הורדת כל התיקים דרך חיפוש טווח תאריכים — ללא תלות בתיקים קשורים."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    years_back = min(int(payload.get("years_back", 10)), 10)
    ctx.progress(0.05, f"הורדת כל התיקים — {years_back} שנים אחורה (ללא תלות בקשורים)")

    def _run(page):
        from core.connection import ensure_logged_in
        from core.download import SESSION_SETTINGS
        from core.net_search_cases import run_bulk_download_from_date_search
        ensure_logged_in(page, "NET")
        run_bulk_download_from_date_search(
            page=page, root_output_dir=config.COURT_DOCS_DIR,
            session_settings={**SESSION_SETTINGS, "download_related_cases": False},
            logger=None, years_back=years_back)
        return "download-all done"

    _run_portal(ctx, "net_download_all", _run, timeout=14400)
    ctx.progress(0.9, "מייבא CSV מחדש")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    _drive_sync(ctx, "אחרי הורדת הכל")
    ctx.file_event(0, "SYNC_DONE", f"{n} docs")
    return f"הורדת הכל: {n} מסמכים עודכנו"


@handler("delete_case")
def delete_case(payload: dict, ctx: JobContext) -> str:
    """EN: remove a whole sub-case — DB rows gone, files moved to .trash
        (never hard-deleted), and the deletion mirrored to Drive trash.
    HE: מחיקת תת-תיק שלם — רשומות DB, קבצים ל-.trash (לא מחיקה קשה),
        והמחיקה משתקפת לפח של דרייב."""
    sub_case_id = int(payload.get("sub_case_id", 0))
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT document_id, local_path FROM documents WHERE sub_case_id=?",
        (sub_case_id,)).fetchall()
    trash = config.COURT_DOCS_DIR / ".trash"
    trash.mkdir(exist_ok=True)
    moved = 0
    case_dir = None
    for r in rows:
        if r["local_path"]:
            src = config.COURT_DOCS_DIR / r["local_path"]
            case_dir = src.parent
            if src.exists():
                dst = trash / src.name
                i = 1
                while dst.exists():
                    dst = trash / f"{src.stem} ({i}){src.suffix}"; i += 1
                src.rename(dst); moved += 1
    conn.execute("DELETE FROM documents WHERE sub_case_id=?", (sub_case_id,))
    conn.execute("DELETE FROM sub_cases WHERE sub_case_id=?", (sub_case_id,))
    conn.execute("DELETE FROM cases WHERE case_id NOT IN (SELECT DISTINCT case_id FROM sub_cases)")
    conn.execute("DELETE FROM clients WHERE client_id NOT IN (SELECT DISTINCT client_id FROM cases)")
    conn.commit()
    # mirror to Drive trash / השתקפות לפח דרייב
    try:
        from core.download import SESSION_SETTINGS as _ss
        if _ss.get("storage_mode") in ("both", "cloud") and case_dir is not None:
            from core.gdrive import GDriveUploader, DRIVE_ROOT_FOLDER
            up = GDriveUploader(config.PROJECT_ROOT / "credentials.json",
                                config.PROJECT_ROOT / "token.json", logger=None)
            if up.authenticate():
                root_id = up.get_or_create_folder(DRIVE_ROOT_FOLDER, parent_id=None)
                rel = case_dir.relative_to(config.COURT_DOCS_DIR)
                parent = root_id
                for part in rel.parts:
                    parent = up.get_or_create_folder(part, parent_id=parent)
                up._service.files().update(fileId=parent, body={"trashed": True}).execute()
                ctx and jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                        "message": "התיק הועבר גם לפח בדרייב"})
    except Exception as exc:
        ctx and jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                "message": f"⚠ מחיקת דרייב נכשלה: {str(exc)[:60]}"})
    jobs.broadcast({"type": "file", "status": "CASE_DELETED", "name": str(sub_case_id)})
    return f"תיק {sub_case_id} נמחק — {moved} קבצים ל-.trash"


@handler("net_sync_selected")
def net_sync_selected(payload: dict, ctx: JobContext) -> str:
    """EN: sync a batch of user-picked NET cases in ONE browser session —
        avoids many colliding open_portal jobs. Each case is navigated to by
        number+MMYY and downloaded in turn.
    HE: סנכרון אצווה של תיקים שהמשתמש סימן, בסשן דפדפן אחד — בלי התנגשות
        של הרבה משימות. כל תיק: ניווט לפי מספר+MMYY והורדה בתורו."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    cases = payload.get("cases") or []
    if isinstance(cases, str):
        cases = json.loads(cases)
    if not cases:
        return "אין תיקים לסנכרון"
    total = len(cases)
    done = failed = 0

    def _run(page):
        nonlocal done, failed
        import time as _t
        from core.connection import ensure_logged_in
        from core.net_case_navigator import navigate_to_case_by_number
        from core.download import run_net_download

        ensure_logged_in(page, "NET")

        for i, c in enumerate(cases, 1):
            cn, my = str(c.get("case_number", "")), str(c.get("month_year", ""))
            ident = c.get("id", f"{cn}-{my}")
            ctx.progress(i / (total + 1), f"({i}/{total}) מסנכרן תיק {ident}")
            try:
                if not navigate_to_case_by_number(page, cn, my):
                    failed += 1
                    jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                    "message": f"תיק {ident} לא אותר — מדלג"})
                    continue
                run_net_download(page, output_dir=config.COURT_DOCS_DIR)
                done += 1
                jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                "message": f"✓ תיק {ident} סונכרן ({i}/{total})"})
            except Exception as exc:
                failed += 1
                jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                "message": f"✗ תיק {ident} נכשל: {str(exc)[:60]}"})
        return "batch done"

    _run_portal(ctx, "net_sync_selected", _run, timeout=7200)
    ctx.progress(0.95, "מייבא CSV מחדש")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    _drive_sync(ctx, "אחרי אצווה")
    ctx.file_event(0, "SYNC_DONE", f"{n} docs")
    return f"אצווה: {done} סונכרנו, {failed} נכשלו, {n} מסמכים עודכנו"


@handler("net_open_case")
def net_open_case(payload: dict, ctx: JobContext) -> str:
    """EN: reach a specific NET case directly from the UI — case number +
        month/year (MMYY) go into the portal's header case-locator, no manual
        navigation needed. Optionally runs a full sync afterwards.
    HE: הגעה לתיק נט ספציפי ישירות מה-UI — מספר תיק + חודש/שנה נכנסים
        לאיתור התיקים של הפורטל, בלי ניווט ידני. אופציונלית מסנכרן מיד."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached / אין דפדפן מחובר")
    case_number = str(payload.get("case_number", "")).strip()
    month_year = str(payload.get("month_year", "")).strip()   # MMYY
    do_sync = bool(payload.get("sync"))
    if not case_number or len(month_year) != 4 or not month_year.isdigit():
        raise ValueError("case_number + month_year (MMYY) required")

    # Viewing happens in the embedded browser window of the new UI — no popup.
    # הצפייה בחלון המוטמע של ה-UI החדש, בלי חלון קופץ.

    ctx.progress(0.05, f"מאתר תיק {case_number} ({month_year[:2]}/{month_year[2:]})")

    def _open(page):
        import time as _t
        from core.connection import ensure_logged_in
        from core.net_case_navigator import navigate_to_case_by_number

        ensure_logged_in(page, "NET")

        if not navigate_to_case_by_number(page, case_number, month_year):
            raise RuntimeError(f"התיק {case_number} לא אותר בפורטל")
        return "case open"

    _run_portal(ctx, "net_open_case", _open, timeout=300)

    if not do_sync:
        return f"תיק {case_number} פתוח בפורטל"

    ctx.progress(0.3, "התיק אותר — מסנכרן מסמכים")

    def _sync(page):
        from core.download import run_net_download
        run_net_download(page, output_dir=config.COURT_DOCS_DIR)
        return "sync done"

    _run_portal(ctx, "net_open_case_sync", _sync, timeout=3600)
    ctx.progress(0.85, "מייבא CSV מחדש")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    ctx.file_event(0, "SYNC_DONE", f"{n} docs refreshed")
    return f"תיק {case_number}: סונכרן, {n} מסמכים עודכנו"


@handler("reimport_csv")
def reimport_csv(payload: dict, ctx: JobContext) -> str:
    """Manual full re-import (no browser) / ייבוא מלא ידני (בלי דפדפן)."""
    ctx.progress(0.2, "re-importing all folders / מייבא את כל התיקיות")
    from .migrate_csv import migrate
    totals = migrate()
    return f"reimported: {totals}"


@handler("purge_stale")
def purge_stale(payload: dict, ctx: JobContext) -> str:
    """Remove DB records whose files no longer exist on disk, then remove
    empty sub_cases/cases/clients. If mode='all', wipe everything regardless."""
    from . import db, config
    conn = db.get_conn()
    mode = payload.get("mode", "missing")

    if mode == "all":
        conn.execute("DELETE FROM documents")
        conn.execute("DELETE FROM sub_cases")
        conn.execute("DELETE FROM cases")
        conn.execute("DELETE FROM clients")
        conn.commit()
        return "cleared: all records deleted"

    # mode == 'missing': delete only records without a file on disk
    rows = conn.execute(
        "SELECT document_id, local_path FROM documents WHERE local_path IS NOT NULL"
    ).fetchall()
    deleted = 0
    for row in rows:
        p = config.COURT_DOCS_DIR / row["local_path"]
        if not p.exists():
            conn.execute("DELETE FROM documents WHERE document_id=?", (row["document_id"],))
            deleted += 1

    # Also delete sub_cases with no documents, cases with no sub_cases, etc.
    conn.execute(
        "DELETE FROM sub_cases WHERE sub_case_id NOT IN (SELECT DISTINCT sub_case_id FROM documents)"
    )
    conn.execute(
        "DELETE FROM cases WHERE case_id NOT IN (SELECT DISTINCT case_id FROM sub_cases)"
    )
    conn.execute(
        "DELETE FROM clients WHERE client_id NOT IN (SELECT DISTINCT client_id FROM cases)"
    )
    conn.commit()
    return f"purged: {deleted} missing documents removed"


# ---------------------------------------------------------------------------
# NET auto-update all existing cases / עדכון אוטומטי של כל תיקי נט המשפט
# ---------------------------------------------------------------------------

@handler("net_auto_update")
def net_auto_update(payload: dict, ctx: JobContext) -> str:
    """Run run_net_auto_update on all folders in downloads/ then re-import."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached")
    ctx.progress(0.05, "starting NET auto-update")

    def _run(page):
        from core.connection import ensure_logged_in
        from core.net_auto_update import run_net_auto_update
        from core.download import resolve_smart_paths, SESSION_SETTINGS
        ensure_logged_in(page, "NET")
        run_net_auto_update(
            page=page,
            logger=None,
            root_output_dir=config.COURT_DOCS_DIR,
            session_settings=SESSION_SETTINGS,
            resolve_paths_fn=resolve_smart_paths,
        )
        return "auto-update done"

    _run_portal(ctx, "net_auto_update", _run, timeout=7200)
    ctx.progress(0.9, "re-importing CSVs")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    jobs.broadcast({"type": "file", "name": "auto-update", "status": "SYNC_DONE"})
    return f"net auto-update ok, {n} docs re-imported"


# ---------------------------------------------------------------------------
# BDR batch — all cases / הורדת כל תיקי בתי הדין הרבניים
# ---------------------------------------------------------------------------

@handler("bdr_batch")
def bdr_batch(payload: dict, ctx: JobContext) -> str:
    """Run BdrBatchRunner then re-import all CSVs.
    Uses the dedicated BDR browser if available, otherwise falls back to the shared one."""
    bdr = ctx.bdr_browser or ctx.browser
    if bdr is None:
        raise RuntimeError("no browser attached")
    ctx.progress(0.05, "starting BDR batch")

    def _run(page):
        from core.bdr_batch import BdrBatchRunner
        from core.connection import ensure_logged_in
        try:
            ensure_logged_in(page, "BDR")
        except Exception as e:
            print(f"[bdr_batch] login step: {e}")
        from core.download import SESSION_SETTINGS
        run_settings = {**SESSION_SETTINGS,
                        "force_rerun": payload.get("force_rerun", False),
                        "client_filter": payload.get("client_filter", ""),
                        "user_mode": payload.get("user_mode", "lawyer")}
        batch = BdrBatchRunner(page, logger=None)
        batch.run(run_settings, config.COURT_DOCS_DIR)
        return "bdr batch done"

    # Use the BDR browser directly
    _saved_browser = ctx.browser
    ctx.browser = bdr
    _run_portal(ctx, "bdr_batch", _run, timeout=7200)
    ctx.browser = _saved_browser
    # Real-time client attribution: move each case under its inferred client
    # (the party that repeats across the lawyer's cases) before re-importing.
    try:
        from core.client_inference import reorganize_cases_by_client
        from core.download import SESSION_SETTINGS as _ss
        lawyer = _ss.get("lawyer_name") or _ss.get("share_name") or ""
        if lawyer:
            ctx.progress(0.88, "משייך תיקים ללקוחות…")
            moved = reorganize_cases_by_client(
                config.COURT_DOCS_DIR / "downloads", lawyer)
            if moved:
                jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                "message": f"{moved} תיקים שויכו ללקוחות"})
    except Exception as _ce:
        print(f"[bdr_batch] client reorg skipped: {_ce}")
    ctx.progress(0.9, "re-importing CSVs")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    jobs.broadcast({"type": "file", "name": "bdr-batch", "status": "SYNC_DONE"})
    return f"bdr batch ok, {n} docs re-imported"


# ---------------------------------------------------------------------------
# NET date-range search → download all matching cases / חיפוש לפי טווח תאריכים
# ---------------------------------------------------------------------------

@handler("net_date_search")
def net_date_search(payload: dict, ctx: JobContext) -> str:
    """Search NET by date range and download all matching cases."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached")
    years_back = int(payload.get("years_back", 12))
    ctx.progress(0.05, f"NET date search: {years_back} years back")

    def _run(page):
        from core.connection import ensure_logged_in
        from core.download import SESSION_SETTINGS
        from core.net_search_cases import run_bulk_download_from_date_search
        ensure_logged_in(page, "NET")
        run_bulk_download_from_date_search(
            page=page,
            root_output_dir=config.COURT_DOCS_DIR,
            session_settings=SESSION_SETTINGS,
            logger=None,
            years_back=years_back,
        )
        return "date search done"

    _run_portal(ctx, "net_date_search", _run, timeout=7200)
    ctx.progress(0.9, "re-importing CSVs")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    jobs.broadcast({"type": "file", "name": "date-search", "status": "SYNC_DONE"})
    return f"net date search ok, {n} docs re-imported"


# ---------------------------------------------------------------------------
# NET smart download — list → pick → download with live stats & cancel
# ---------------------------------------------------------------------------

# Cancel flag — checked between cases so the user can stop mid-batch.
_cancel_flags: dict[int, bool] = {}


def cancel_download(job_id: int) -> None:
    """Signal a running net_smart_download / bdr_batch to stop after the current case."""
    _cancel_flags[job_id] = True


@handler("net_list_cases")
def net_list_cases(payload: dict, ctx: JobContext) -> str:
    """Connect to NET, go to 'התיקים שלי', scrape case list, send to UI via SSE.
    Does NOT download — just discovers what's available."""
    if ctx.browser is None:
        raise RuntimeError("no browser attached")
    years_back = min(int(payload.get("years_back", 10)), 10)

    def _run(page):
        from core.connection import ensure_logged_in
        from core.net_search_cases import (
            navigate_to_my_cases, fill_my_cases_dates_and_search,
            extract_cases_from_my_cases_grid,
            navigate_to_date_search, fill_date_range_and_search,
            extract_cases_from_search_grid, _parse_display_id,
        )
        from datetime import datetime, timedelta

        ensure_logged_in(page, "NET")
        ctx.progress(0.2, "מחובר — מחפש תיקים…")

        today = datetime.now()
        from_dt = today - timedelta(days=years_back * 365)
        from_str = from_dt.strftime("%d/%m/%Y")
        to_str = today.strftime("%d/%m/%Y")

        cases = []
        # Prefer date-search ("תיקים לתאריך פתיחה") — filters by date range
        if navigate_to_date_search(page):
            if fill_date_range_and_search(page, from_str, to_str):
                cases = extract_cases_from_search_grid(page)

        if not cases:
            if navigate_to_my_cases(page):
                if fill_my_cases_dates_and_search(page, from_str, to_str):
                    cases = extract_cases_from_my_cases_grid(page)

        parseable = []
        for c in cases:
            did = c.get("CaseDisplayIdentifier", "")
            parsed = _parse_display_id(did)
            if parsed:
                parseable.append({
                    "display_id": did,
                    "case_number": parsed[0],
                    "mmyy": parsed[1],
                    "name": c.get("CaseName", ""),
                    "court": c.get("CourtName", ""),
                    "status": c.get("CaseStatusName", ""),
                    "type": c.get("CaseTypeShortName", ""),
                    "interest": c.get("CaseInterestName", ""),
                })

        jobs.broadcast({"type": "net_cases", "cases": parseable,
                        "total": len(parseable), "years_back": years_back})
        return f"found {len(parseable)} cases"

    _run_portal(ctx, "net_list_cases", _run, timeout=300)
    return "case list sent to UI"


@handler("net_smart_download")
def net_smart_download(payload: dict, ctx: JobContext) -> str:
    """Download NET cases with live stats. Modes:
    - all: download every case (no user selection)
    - selected: download only the cases the user picked (ids in payload)
    - related: download picked cases + their related-cases tab

    Broadcasts per-case progress, speed stats, and supports cancellation.
    Resumes from where it left off if cases were partially downloaded before.
    """
    if ctx.browser is None:
        raise RuntimeError("no browser attached")

    mode = payload.get("mode", "all")  # all | selected | related
    selected_ids = payload.get("cases", [])  # list of {case_number, mmyy, display_id}
    if isinstance(selected_ids, str):
        selected_ids = json.loads(selected_ids)
    years_back = min(int(payload.get("years_back", 10)), 10)

    _cancel_flags[ctx.job_id] = False
    stats = {"done": 0, "total": 0, "failed": 0, "docs_downloaded": 0,
             "docs_in_current": 0, "current_case": "", "current_name": "",
             "start_time": 0.0, "cases_queue": [], "cases_detail": [],
             "output_dir": str(config.COURT_DOCS_DIR), "completed_cases": []}

    def _broadcast_stats():
        import time as _t
        elapsed = _t.time() - stats["start_time"] if stats["start_time"] else 0
        speed = stats["docs_downloaded"] / elapsed * 60 if elapsed > 10 else 0
        remaining = stats["total"] - stats["done"] - stats["failed"]
        eta_sec = round(remaining / speed * 60) if speed > 0 else 0
        jobs.broadcast({"type": "download_stats",
                        "done": stats["done"], "total": stats["total"],
                        "failed": stats["failed"],
                        "docs_downloaded": stats["docs_downloaded"],
                        "docs_in_current": stats["docs_in_current"],
                        "current_case": stats["current_case"],
                        "current_name": stats.get("current_name", ""),
                        "speed_per_min": round(speed, 1),
                        "elapsed_sec": round(elapsed),
                        "remaining": remaining,
                        "eta_sec": eta_sec,
                        "cases_detail": stats.get("cases_detail", []),
                        "completed_cases": stats.get("completed_cases", [])[-10:],
                        "output_dir": stats.get("output_dir", ""),
                        "job_id": ctx.job_id})

    def _run(page):
        import time as _t
        from core.connection import ensure_logged_in
        from core.net_case_navigator import navigate_to_case_by_number
        from core.download import run_net_download, SESSION_SETTINGS
        from core.net_search_cases import (
            navigate_to_my_cases, fill_my_cases_dates_and_search,
            extract_cases_from_my_cases_grid,
            navigate_to_date_search, fill_date_range_and_search,
            extract_cases_from_search_grid, _parse_display_id,
        )
        from datetime import datetime, timedelta

        ensure_logged_in(page, "NET")
        stats["start_time"] = _t.time()

        # --- discover cases if mode=all, or use selected_ids ---
        if mode == "all":
            ctx.progress(0.05, "מחפש את כל התיקים…")
            today = datetime.now()
            from_dt = today - timedelta(days=years_back * 365)
            from_str = from_dt.strftime("%d/%m/%Y")
            to_str = today.strftime("%d/%m/%Y")

            cases = []
            if navigate_to_my_cases(page):
                if fill_my_cases_dates_and_search(page, from_str, to_str):
                    cases = extract_cases_from_my_cases_grid(page)
            if not cases:
                if navigate_to_date_search(page):
                    if fill_date_range_and_search(page, from_str, to_str):
                        cases = extract_cases_from_search_grid(page)

            queue = []
            for c in cases:
                did = c.get("CaseDisplayIdentifier", "")
                parsed = _parse_display_id(did)
                if parsed:
                    queue.append({"display_id": did, "case_number": parsed[0],
                                  "mmyy": parsed[1], "name": c.get("CaseName", ""),
                                  "court": c.get("CourtName", ""),
                                  "status": c.get("CaseStatusName", ""),
                                  "type": c.get("CaseTypeShortName", ""),
                                  "interest": c.get("CaseInterestName", "")})
        else:
            queue = list(selected_ids)

        stats["total"] = len(queue)
        stats["cases_queue"] = [q.get("display_id", "") for q in queue]
        stats["cases_detail"] = [{"id": q.get("display_id",""), "name": q.get("name",""),
                                   "court": q.get("court",""), "type": q.get("type",""),
                                   "status": q.get("status",""), "interest": q.get("interest","")}
                                  for q in queue]
        ctx.progress(0.08, f"נמצאו {len(queue)} תיקים — מתחיל הורדה")
        _broadcast_stats()

        for idx, case_info in enumerate(queue, 1):
            if _cancel_flags.get(ctx.job_id):
                ctx.progress(idx / (len(queue) + 1),
                             f"הופסק על ידי המשתמש — {stats['done']} תיקים הורדו")
                _broadcast_stats()
                return "cancelled by user"

            cn = str(case_info.get("case_number", ""))
            my = str(case_info.get("mmyy", ""))
            did = case_info.get("display_id", f"{cn}-{my}")
            stats["current_case"] = did
            stats["current_name"] = case_info.get("name", "")
            ctx.progress(idx / (len(queue) + 1),
                         f"({idx}/{len(queue)}) מוריד תיק {did}")
            _broadcast_stats()

            try:
                if not navigate_to_case_by_number(page, cn, my):
                    stats["failed"] += 1
                    jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                    "message": f"תיק {did} לא אותר — מדלג"})
                    continue

                run_net_download(page, output_dir=config.COURT_DOCS_DIR)
                stats["done"] += 1
                stats["docs_downloaded"] += 1
                stats["completed_cases"].append({"id": did, "name": case_info.get("name",""),
                                                  "status": "ok"})

                # related cases mode — descend into related tab
                if mode == "related":
                    try:
                        _download_related(page, ctx, stats, queue, idx)
                    except Exception as re:
                        jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                        "message": f"תיקים קשורים של {did}: {str(re)[:60]}"})

                jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                "message": f"✓ תיק {did} הורד ({idx}/{len(queue)})"})
            except Exception as exc:
                stats["failed"] += 1
                jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                                "message": f"✗ תיק {did} נכשל: {str(exc)[:60]}"})
            _broadcast_stats()

        return "smart download done"

    _run_portal(ctx, "net_smart_download", _run, timeout=14400)
    ctx.progress(0.95, "מייבא CSV מחדש")
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    _drive_sync(ctx, "אחרי הורדה")
    jobs.broadcast({"type": "file", "status": "SYNC_DONE", "name": f"{n} docs"})
    _cancel_flags.pop(ctx.job_id, None)
    return (f"הורדה: {stats['done']} תיקים, {stats['failed']} נכשלו, "
            f"{n} מסמכים עודכנו")


def _download_related(page, ctx, stats, queue, parent_idx):
    """Enter the 'תיקים קשורים' tab for the current case and add found cases to the queue."""
    from core.net_search_cases import _parse_display_id
    try:
        # Navigate to related-cases tab in the portal
        rel_tab = page.locator('a:has-text("תיקים קשורים"), '
                               'span:has-text("תיקים קשורים")').first
        if rel_tab.count() == 0:
            return
        rel_tab.click()
        page.wait_for_load_state("networkidle", timeout=10000)
        import time; time.sleep(1.5)

        # Extract related case IDs from the grid
        rows = page.evaluate("""
            (() => {
                const idRe = /^\\d+-\\d{2}-\\d{2}$/;
                return [...document.querySelectorAll('div[role="row"], tr')]
                    .flatMap(r => [...r.querySelectorAll('div[role="gridcell"], td')]
                        .map(c => (c.innerText||'').trim()))
                    .filter(t => idRe.test(t));
            })()
        """) or []

        existing_ids = {q.get("display_id") for q in queue}
        added = 0
        for did in rows:
            if did in existing_ids:
                continue
            parsed = _parse_display_id(did)
            if parsed:
                new_case = {"display_id": did, "case_number": parsed[0],
                            "mmyy": parsed[1], "name": "תיק קשור"}
                queue.append(new_case)
                existing_ids.add(did)
                stats["total"] += 1
                added += 1

        if added:
            jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                            "message": f"נמצאו {added} תיקים קשורים — נוספו לתור"})
    except Exception as e:
        jobs.broadcast({"type": "job", "job_id": ctx.job_id,
                        "message": f"תיקים קשורים: {str(e)[:60]}"})


# ---------------------------------------------------------------------------
# PDF → Markdown conversion (pdfplumber text layer + bidi fix)
# ---------------------------------------------------------------------------

@handler("convert_md")
def convert_md(payload: dict, ctx: JobContext) -> str:
    """Extract text from a downloaded PDF or DOCX and save a .md file alongside it."""
    doc_id = int(payload["document_id"])
    row = db.get_conn().execute(
        "SELECT local_path, physical_name, logical_name FROM documents WHERE document_id=?",
        (doc_id,)
    ).fetchone()
    if not row or not row["local_path"]:
        raise RuntimeError("document not found or has no local_path")

    file_path = config.COURT_DOCS_DIR / row["local_path"]
    if not file_path.exists():
        raise RuntimeError(f"file not on disk: {file_path}")

    ext = file_path.suffix.lower()

    if ext in (".docx", ".doc"):
        try:
            import mammoth as _mammoth
        except ImportError:
            raise RuntimeError("mammoth not installed — run: pip install mammoth")
        ctx.progress(0.2, "reading DOCX")
        with open(str(file_path), "rb") as f:
            result = _mammoth.extract_raw_text(f)
        full_text = result.value or ""
        ctx.progress(0.9, "done")
    else:
        try:
            import pdfplumber
        except ImportError:
            raise RuntimeError("pdfplumber not installed — run: pip install pdfplumber")
        ctx.progress(0.1, "reading PDF")
        parts = []
        with pdfplumber.open(str(file_path)) as pdf:
            total = len(pdf.pages)
            # detect scanned PDF: first page has images but no chars
            first_page = pdf.pages[0]
            is_scanned = not first_page.chars and first_page.images
            if is_scanned:
                # OCR path — Groq/Gemini vision preferred, pytesseract fallback
                from core.pdf_to_text import resolve_ocr_provider, extract_text_from_pdf
                from core.download import SESSION_SETTINGS as _ss
                provider, api_key = resolve_ocr_provider(_ss)
                llm_text = ""
                if provider and api_key:
                    ctx.progress(0.15, f"OCR דרך {provider}")
                    try:
                        llm_text = extract_text_from_pdf(
                            file_path, api_key, logger=None,
                            cache_path=file_path.parent / "ocr_cache.json",
                            provider=provider,
                        ) or ""
                    except Exception as _oe:
                        print(f"[convert_md] {provider} OCR failed: {_oe}")
                if llm_text.strip():
                    parts.append(llm_text)
                else:
                    try:
                        import pytesseract as _tess
                    except ImportError:
                        raise RuntimeError("pytesseract not installed — run: pip install pytesseract")
                    for i, page in enumerate(pdf.pages, 1):
                        img = page.to_image(resolution=200).original
                        text = _tess.image_to_string(img, lang="heb+eng").strip()
                        parts.append(f"\n--- עמוד {i} ---\n\n{text}\n")
                        ctx.progress(0.1 + 0.8 * i / max(total, 1), f"OCR עמוד {i}/{total}")
            else:
                for i, page in enumerate(pdf.pages, 1):
                    text = (page.extract_text() or "").strip()
                    parts.append(f"\n--- עמוד {i} ---\n\n{text}\n")
                    ctx.progress(0.1 + 0.8 * i / max(total, 1), f"עמוד {i}/{total}")
        full_text = "".join(parts)
    # rename pdf_path → file_path for the rest of the function
    pdf_path = file_path

    # Fix reversed Hebrew (bidi) if library available
    try:
        import re as _re
        from bidi.algorithm import get_display
        lines = full_text.split("\n")
        full_text = "\n".join(
            get_display(ln) if _re.search(r'[א-ת]', ln) else ln
            for ln in lines
        )
    except ImportError:
        pass

    title = row["logical_name"] or row["physical_name"]
    md_path = pdf_path.with_suffix(".md")
    md_path.write_text(f"# {title}\n\n{full_text}", encoding="utf-8")

    # Structured metadata (subject, topics, attachments, decision type)
    try:
        from core.pdf_to_text import extract_doc_metadata
        from core.download import SESSION_SETTINGS as _ss2
        ctx.progress(0.95, "מחלץ מטא-דאטה")
        meta = extract_doc_metadata(full_text, _ss2)
        if meta:
            import json as _json
            md_path.with_suffix(".meta.json").write_text(
                _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    ctx.progress(1.0, f"נשמר: {md_path.name}")
    jobs.broadcast({"type": "file", "name": md_path.name, "status": "MD_DONE"})
    return f"saved {md_path.name} ({len(full_text)} chars)"
