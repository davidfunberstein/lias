"""FastAPI backend + SSE — the UI's window into LIAS."""
from __future__ import annotations

import json
import mimetypes
import os
import queue as _queue
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel

from . import config, db, jobs, queries, snapshot

app = FastAPI(title="LIAS", version="0.1.0")


# --- Static UI ---------------------------------------------------------------

@app.get("/")
def index():
    # no-store — the UI must never be served stale from browser cache
    # בלי קאש — שה-UI לעולם לא יוגש ישן מהדפדפן
    return FileResponse(config.UI_DIR / "index.html",
                        headers={"Cache-Control": "no-store"})


# --- State endpoints ---------------------------------------------------------

@app.get("/api/clients")
def clients():
    rows = db.get_conn().execute(
        """SELECT c.client_id, c.display_name,
                  COUNT(DISTINCT s.sub_case_id) AS sub_cases,
                  COUNT(d.document_id) AS documents
           FROM clients c
           LEFT JOIN cases k ON k.client_id = c.client_id
           LEFT JOIN sub_cases s ON s.case_id = k.case_id
           LEFT JOIN documents d ON d.sub_case_id = s.sub_case_id
           GROUP BY c.client_id ORDER BY c.display_name"""
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/clients/{client_id}/tree")
def client_tree(client_id: int):
    conn = db.get_conn()
    cases = [dict(r) for r in conn.execute(
        "SELECT * FROM cases WHERE client_id=? ORDER BY case_number", (client_id,)).fetchall()]
    for c in cases:
        c["sub_cases"] = [dict(r) for r in conn.execute(
            """SELECT s.*, COUNT(d.document_id) AS documents,
                      SUM(CASE WHEN d.download_status='ERROR' THEN 1 ELSE 0 END) AS errors
               FROM sub_cases s LEFT JOIN documents d ON d.sub_case_id=s.sub_case_id
               WHERE s.case_id=? GROUP BY s.sub_case_id ORDER BY s.sub_number""",
            (c["case_id"],)).fetchall()]
    return cases


@app.get("/api/sub_cases/{sub_case_id}/documents")
def documents(sub_case_id: int, status: str = "", doc_type: str = "", q: str = "",
              sort: str = "submission_date", order: str = "desc"):
    _SORT_COLS = {"submission_date", "logical_name", "physical_name", "doc_type",
                  "pages", "download_status", "submitter_est", "document_id"}
    col = sort if sort in _SORT_COLS else "physical_name"
    dir_ = "DESC" if order.lower() != "asc" else "ASC"
    sql = "SELECT * FROM documents WHERE sub_case_id=?"
    args: list = [sub_case_id]
    if status:
        sql += " AND download_status=?"; args.append(status)
    if doc_type:
        sql += " AND doc_type LIKE ?"; args.append(f"%{doc_type}%")
    if q:
        sql += " AND (logical_name LIKE ? OR physical_name LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    # submission_date stored as DD/MM/YYYY — reformat for correct chronological sort
    if col == "submission_date":
        sort_expr = (
            f"(CASE WHEN submission_date GLOB '??/??/????' "
            f"THEN substr(submission_date,7,4)||substr(submission_date,4,2)||substr(submission_date,1,2) "
            f"ELSE submission_date END) {dir_}"
        )
    else:
        sort_expr = f"{col} {dir_}"
    sql += f" ORDER BY {sort_expr}, document_id DESC LIMIT 500"
    return [dict(r) for r in db.get_conn().execute(sql, args).fetchall()]


@app.get("/api/sub_cases/{sub_case_id}/whats_new")
def whats_new(sub_case_id: int):
    d = snapshot.latest_diff(sub_case_id)
    return d or {"counts": {"added": 0, "removed": 0, "changed": 0}, "taken_at": None}


@app.get("/api/jobs")
def job_list(limit: int = 30):
    rows = db.get_conn().execute(
        "SELECT * FROM jobs ORDER BY job_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/sync_runs")
def sync_runs(limit: int = 30):
    rows = db.get_conn().execute(
        "SELECT * FROM sync_runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/log")
def get_log(lines: int = 200, source: str = "main"):
    """source: main | drive"""
    if source == "drive":
        # find the latest drive-upload log
        drive_base = config.COURT_DOCS_DIR / "drive-uploads"
        candidates = list(drive_base.rglob("latest.log")) if drive_base.exists() else []
        # pick most recently modified log (active upload session)
        log_path = max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
    else:
        log_path = config.COURT_DOCS_DIR / "logs" / "latest.log"
    if not log_path or not log_path.exists():
        return {"lines": [], "path": str(log_path or "")}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if source == "drive":
        # CSV log — format each row as a readable timestamped line.
        import csv as _csv, io as _io
        try:
            reader = _csv.DictReader(_io.StringIO(text))
            formatted = []
            for row in reader:
                ts   = row.get("תחילת העלאה") or row.get("סיום העלאה") or ""
                name = row.get("שם קובץ") or row.get("שם מסמך") or ""
                stat = row.get("סטטוס") or ""
                kb   = row.get("גודל (KB)") or ""
                note = row.get("הערה") or ""
                parts = [f"[{ts}]" if ts else "", name, stat,
                         f"{kb} KB" if kb else "", note]
                formatted.append("  ".join(p for p in parts if p))
            return {"lines": formatted[-lines:], "path": str(log_path)}
        except Exception:
            pass  # fall through to raw text
    return {"lines": text.splitlines()[-lines:], "path": str(log_path)}


@app.api_route("/api/doc/{document_id}", methods=["GET", "HEAD"])
def serve_doc(document_id: int):
    row = db.get_conn().execute(
        "SELECT local_path, logical_name, physical_name FROM documents WHERE document_id=?",
        (document_id,)
    ).fetchone()
    if not row or not row["local_path"]:
        raise HTTPException(404, "no local file recorded")
    path = config.COURT_DOCS_DIR / row["local_path"]
    if not path.exists():
        raise HTTPException(404, f"file not on disk: {path}")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    from urllib.parse import quote as _q
    fname = row["physical_name"] or row["logical_name"]
    encoded = _q(fname, safe="")
    if mime == "application/pdf":
        # inline display but with filename so browser uses it on download
        return FileResponse(str(path), media_type=mime,
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{encoded}"})
    # non-PDF: offer as download with RFC 5987 encoded filename
    fname = row["logical_name"] or row["physical_name"]
    encoded = _q(fname, safe="")
    cd = f"attachment; filename*=UTF-8''{encoded}"
    return FileResponse(str(path), media_type=mime,
                        headers={"Content-Disposition": cd})


@app.post("/api/doc/{document_id}/reveal")
def reveal_doc(document_id: int):
    """Open the file's parent folder in Finder and select the file."""
    import subprocess
    row = db.get_conn().execute(
        "SELECT local_path FROM documents WHERE document_id=?", (document_id,)
    ).fetchone()
    if not row or not row["local_path"]:
        raise HTTPException(404, "no local file recorded")
    path = config.COURT_DOCS_DIR / row["local_path"]
    if not path.exists():
        raise HTTPException(404, f"file not on disk: {path}")
    subprocess.Popen(["open", "-R", str(path)])
    return {"ok": True}

@app.post("/api/doc/{document_id}/open")
def open_doc(document_id: int):
    """Open the file directly with the OS default application."""
    import subprocess
    row = db.get_conn().execute(
        "SELECT local_path FROM documents WHERE document_id=?", (document_id,)
    ).fetchone()
    if not row or not row["local_path"]:
        raise HTTPException(404, "no local file recorded")
    path = config.COURT_DOCS_DIR / row["local_path"]
    if not path.exists():
        raise HTTPException(404, f"file not on disk: {path}")
    subprocess.Popen(["open", str(path)])
    return {"ok": True}


# --- Browser visibility ------------------------------------------------------

@app.get("/api/browser/status")
def browser_status():
    b = jobs._pool._browser if hasattr(jobs, "_pool") and jobs._pool else None
    if not b:
        return {"available": False, "headless": True, "alive": False, "busy": False}
    # Cached values only — never blocks behind a running job.
    # ערכים מהמטמון בלבד — לא נתקע מאחורי משימה רצה.
    return {"available": True, "headless": b.headless,
            "alive": b.is_alive() or b.busy, "busy": b.busy, "url": b.last_url}


@app.post("/api/actions/browser/show")
def act_browser_show():
    b = jobs._pool._browser if hasattr(jobs, "_pool") and jobs._pool else None
    if not b:
        raise HTTPException(503, "browser not available")
    b.show()
    return {"ok": True, "headless": b.headless}


@app.get("/api/browser/screenshot")
def browser_screenshot():
    """Live screenshot of the automation browser — the 'browser window inside
    the site'. / צילום חי של דפדפן האוטומציה — החלון המוטמע באתר."""
    b = jobs._pool._browser if hasattr(jobs, "_pool") and jobs._pool else None
    if not b or not (b.is_alive() or b.busy):
        raise HTTPException(503, "browser not available")
    if b.busy:
        # A job is running — a screenshot would queue behind it. The UI shows
        # "busy" and keeps the last frame. / משימה רצה — ה-UI מציג "עסוק".
        raise HTTPException(423, "browser busy")
    try:
        data = b.run("screenshot",
                     lambda page: page.screenshot(type="jpeg", quality=55),
                     timeout=15)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise HTTPException(503, f"screenshot failed: {exc}")


@app.post("/api/actions/browser/hide")
def act_browser_hide():
    b = jobs._pool._browser if hasattr(jobs, "_pool") and jobs._pool else None
    if not b:
        raise HTTPException(503, "browser not available")
    b.hide()
    return {"ok": True}


# --- Actions -----------------------------------------------------------------

@app.get("/api/doc_pdf/{document_id}")
def serve_doc_as_pdf(document_id: int):
    """Serve a DOCX document converted to PDF (LibreOffice, cached beside the
    source). Gives a faithful Word rendering instead of mammoth's approximation."""
    import subprocess, shutil
    row = db.get_conn().execute(
        "SELECT local_path, physical_name FROM documents WHERE document_id=?",
        (document_id,)).fetchone()
    if not row or not row["local_path"]:
        raise HTTPException(404, "no local file recorded")
    src = config.COURT_DOCS_DIR / row["local_path"]
    if not src.exists():
        raise HTTPException(404, f"file not on disk: {src}")
    if src.suffix.lower() == ".pdf":
        return FileResponse(str(src), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    pdf_path = src.with_suffix(".preview.pdf")
    if not pdf_path.exists() or pdf_path.stat().st_mtime < src.stat().st_mtime:
        soffice = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        if not Path(soffice).exists():
            raise HTTPException(501, "LibreOffice not installed")
        try:
            import tempfile as _tf
            prof = Path(_tf.gettempdir()) / "lias_soffice_profile"
            env = {**os.environ, "DISPLAY": "", "LSUIElement": "1"}
            subprocess.run(
                [soffice, "--headless", "--invisible", "--nodefault",
                 "--norestore", "--nologo",
                 f"-env:UserInstallation=file://{prof}",
                 "--convert-to", "pdf",
                 "--outdir", str(src.parent), str(src)],
                timeout=90, capture_output=True, check=True, env=env)
            produced = src.with_suffix(".pdf")
            if produced != pdf_path and produced.exists():
                produced.rename(pdf_path)
        except Exception as e:
            raise HTTPException(500, f"conversion failed: {e}")
    if not pdf_path.exists():
        raise HTTPException(500, "conversion produced no file")
    return FileResponse(str(pdf_path), media_type="application/pdf",
                        headers={"Content-Disposition": "inline"})


@app.get("/api/ocr/test")
def ocr_test():
    """Ping the configured OCR provider so the user can verify the API key works."""
    from core.pdf_to_text import resolve_ocr_provider, groq_text_completion
    from core.download import SESSION_SETTINGS
    provider, key = resolve_ocr_provider(SESSION_SETTINGS)
    if not provider:
        return {"ok": False, "provider": "", "error": "לא מוגדר מפתח Groq/Gemini"}
    if provider == "groq":
        try:
            reply = groq_text_completion("Reply with the single word: ok", key, timeout=20)
            return {"ok": bool(reply), "provider": "groq", "reply": reply[:40]}
        except Exception as e:
            return {"ok": False, "provider": "groq", "error": str(e)[:200]}
    return {"ok": True, "provider": provider, "note": "מפתח קיים (לא נבדק בפועל)"}


@app.post("/api/actions/submit_otp")
def act_submit_otp(otp: str = ""):
    jobs.submit_otp(otp)
    return {"ok": True}


@app.post("/api/actions/open_portal/{portal}")
def act_open_portal(portal: str):
    if portal not in ("NET", "BDR", "ECA"):
        raise HTTPException(400, "portal must be NET, BDR or ECA")
    return {"job_id": jobs.submit("open_portal", {"portal": portal})}


@app.post("/api/actions/cancel_download")
def act_cancel_download(job_id: int = 0):
    from .collector_bridge import cancel_download
    cancel_download(job_id)
    return {"ok": True}


@app.post("/api/actions/net_list_cases")
def act_net_list_cases(years_back: int = 20):
    return {"job_id": jobs.submit("net_list_cases", {"years_back": years_back})}


@app.post("/api/actions/net_smart_download")
async def act_net_smart_download(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return {"job_id": jobs.submit("net_smart_download", body)}


@app.post("/api/actions/net_scan/{sub_case_id}")
def act_net_scan(sub_case_id: int):
    return {"job_id": jobs.submit("net_scan", {"sub_case_id": sub_case_id})}


@app.post("/api/actions/sync_current/{portal}")
def act_sync_current(portal: str):
    kind = "net_sync_current" if portal == "NET" else "bdr_sync_current"
    return {"job_id": jobs.submit(kind)}


@app.post("/api/actions/reimport")
def act_reimport():
    return {"job_id": jobs.submit("reimport_csv")}


@app.post("/api/actions/purge_stale")
def act_purge_stale(mode: str = "missing"):
    return {"job_id": jobs.submit("purge_stale", {"mode": mode})}


@app.post("/api/actions/net_auto_update")
def act_net_auto_update():
    return {"job_id": jobs.submit("net_auto_update")}


@app.post("/api/actions/bdr_batch")
def act_bdr_batch(force_rerun: bool = False, client_filter: str = ""):
    return {"job_id": jobs.submit("bdr_batch", {"force_rerun": force_rerun,
                                                "client_filter": client_filter})}


@app.post("/api/actions/open_case_view")
def act_open_case_view(portal: str = "", case_number: str = ""):
    """פתיחת התיק ויזואלית בדפדפן האוטומציה."""
    return {"job_id": jobs.submit("open_case_view",
                                  {"portal": portal, "case_number": case_number})}


@app.post("/api/actions/eca_list")
def act_eca_list():
    """התחברות והצגת תיקי הוצל"פ לבחירה."""
    return {"job_id": jobs.submit("eca_list", {})}


@app.get("/api/eca/cases")
def get_eca_cases():
    """Latest ECA case list — the UI polls this when the eca_list job completes
    so the picker appears even if the live SSE broadcast was missed."""
    try:
        from LIAS.collector_bridge import get_last_eca_cases
        return {"cases": get_last_eca_cases()}
    except Exception:
        return {"cases": []}


@app.post("/api/actions/eca_sync")
def act_eca_sync():
    """הורדת כל תיקי ההוצאה לפועל לפי לקוח."""
    return {"job_id": jobs.submit("eca_sync", {})}


@app.post("/api/actions/net_date_search")
def act_net_date_search(years_back: int = 12):
    return {"job_id": jobs.submit("net_date_search", {"years_back": years_back})}


@app.post("/api/upload_doc")
async def upload_doc(request: Request, sub_case_id: int, name: str = "",
                     document_id: int = 0):
    """EN: attach a missing file to a case manually, with a proper name.
        Saved INTO the case folder as a new file — originals are never touched.
    HE: צירוף ידני של קובץ חסר לתיק, עם שם תקני. נשמר כקובץ חדש בתיקיית
        התיק — לעולם לא דורס קובץ קיים."""
    from . import config as _cfg
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty file")
    name = (name or "").strip() or "קובץ שצורף ידנית"
    conn = db.get_conn()
    row = conn.execute(
        "SELECT local_path FROM documents WHERE sub_case_id=? AND local_path!='' LIMIT 1",
        (sub_case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "sub_case has no folder yet")
    import os as _os
    folder = (_cfg.COURT_DOCS_DIR / row["local_path"]).parent
    folder.mkdir(parents=True, exist_ok=True)
    # keep original extension if the name lacks one
    ext = _os.path.splitext(name)[1]
    fname = name if ext else name + ".pdf"
    target = folder / fname
    i = 1
    while target.exists():                      # never overwrite / בלי דריסה
        stem, e = _os.path.splitext(fname)
        target = folder / f"{stem} ({i}){e}"
        i += 1
    target.write_bytes(body)
    rel = str(target.relative_to(_cfg.COURT_DOCS_DIR))
    if document_id:
        # Fixing a FAILED download: attach the file to the existing record so
        # the error clears and the doc keeps its metadata (type, date, pages).
        conn.execute(
            "UPDATE documents SET local_path=?, physical_name=?, "
            "download_status='COMPLETED' WHERE document_id=?",
            (rel, target.name, document_id))
        conn.commit()
        doc_id = document_id
    else:
        doc_id = db.upsert_document(
            sub_case_id, target.name,
            logical_name=_os.path.splitext(target.name)[0],
            doc_type="צירוף ידני",
            download_status="COMPLETED",
            local_path=rel,
        )
    jobs.broadcast({"type": "file", "document_id": doc_id,
                    "status": "UPLOADED", "name": target.name})
    return {"ok": True, "document_id": doc_id, "file": target.name}


@app.post("/api/actions/trash_doc")
def act_trash_doc(document_id: int):
    """EN: remove a document — the file moves to court_documents/.trash
        (never hard-deleted) and the DB row is removed.
    HE: הסרת מסמך — הקובץ עובר ל-.trash (לא נמחק לצמיתות) והרשומה מוסרת."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT local_path, physical_name FROM documents WHERE document_id=?",
        (document_id,)).fetchone()
    if not row:
        raise HTTPException(404, "document not found")
    if row["local_path"]:
        src = config.COURT_DOCS_DIR / row["local_path"]
        if src.exists():
            trash = config.COURT_DOCS_DIR / ".trash"
            trash.mkdir(exist_ok=True)
            dst = trash / src.name
            i = 1
            while dst.exists():
                dst = trash / f"{src.stem} ({i}){src.suffix}"
                i += 1
            src.rename(dst)
    conn.execute("DELETE FROM documents WHERE document_id=?", (document_id,))
    conn.commit()
    jobs.broadcast({"type": "file", "document_id": document_id,
                    "status": "TRASHED", "name": row["physical_name"] or ""})
    return {"ok": True}


@app.post("/api/actions/net_date_list")
def act_net_date_list(years_back: int = 2):
    """List NET cases in range for checkbox picking / רשימת תיקים לסימון."""
    return {"job_id": jobs.submit("net_date_list", {"years_back": years_back})}


@app.post("/api/actions/drive_sync_now")
def act_drive_sync_now():
    return {"job_id": jobs.submit("drive_sync_now")}


@app.post("/api/actions/drive_share")
def act_drive_share(emails: str = "", scope: str = "all", case_folder: str = ""):
    return {"job_id": jobs.submit("drive_share",
            {"emails": emails, "scope": scope, "case_folder": case_folder})}


@app.post("/api/actions/net_download_all")
def act_net_download_all(years_back: int = 20):
    return {"job_id": jobs.submit("net_download_all", {"years_back": years_back})}


@app.post("/api/actions/delete_case")
def act_delete_case(sub_case_id: int):
    return {"job_id": jobs.submit("delete_case", {"sub_case_id": sub_case_id})}


@app.post("/api/actions/net_sync_selected")
def act_net_sync_selected(cases: str = ""):
    """Batch-sync user-picked NET cases (JSON list) / סנכרון אצווה מסומנת."""
    import json as _j
    try:
        parsed = _j.loads(cases) if cases else []
    except Exception:
        parsed = []
    return {"job_id": jobs.submit("net_sync_selected", {"cases": parsed})}


@app.post("/api/actions/net_open_case")
def act_net_open_case(case_number: str = "", month_year: str = "", sync: bool = False):
    """Reach a NET case by number + MMYY straight from the UI / איתור תיק מה-UI."""
    return {"job_id": jobs.submit("net_open_case", {
        "case_number": case_number, "month_year": month_year, "sync": sync})}


@app.get("/api/doc_md/{document_id}")
def serve_doc_md(document_id: int, view: str = "download"):
    """view=download → file attachment; view=text → plain text for inline rendering"""
    row = db.get_conn().execute(
        "SELECT local_path, logical_name, physical_name FROM documents WHERE document_id=?",
        (document_id,)
    ).fetchone()
    if not row or not row["local_path"]:
        raise HTTPException(404, "no local file")
    pdf_path = config.COURT_DOCS_DIR / row["local_path"]
    md_path = pdf_path.with_suffix(".md")
    if not md_path.exists():
        raise HTTPException(404, "md not found")
    if view == "text":
        text = md_path.read_text(encoding="utf-8", errors="replace")
        return Response(content=text, media_type="text/plain; charset=utf-8")
    from urllib.parse import quote as _q
    fname = (row["logical_name"] or row["physical_name"] or md_path.stem) + ".md"
    encoded = _q(fname, safe="")
    return FileResponse(str(md_path), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"})


@app.post("/api/actions/convert_md/{document_id}")
def act_convert_md(document_id: int):
    return {"job_id": jobs.submit("convert_md", {"document_id": document_id})}




# --- Settings ----------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    import json
    defaults_path = config.PROJECT_ROOT / "session_defaults.json"
    d = {}
    if defaults_path.exists():
        try:
            d = json.loads(defaults_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "court_docs_dir": d.get("court_docs_dir", ""),
        "court_docs_dir_effective": str(config.COURT_DOCS_DIR),
        "downloads_exists": (config.COURT_DOCS_DIR / "downloads").exists(),
        "check_viewers":           d.get("check_viewers", True),
        "download_related_cases":  d.get("download_related_cases", False),
        "mode":                    d.get("mode", "1"),
        "storage_mode":            d.get("storage_mode", "local"),
        "login_method":            d.get("login_method", "standard"),
        "otp_method":              d.get("otp_method", "email"),
        "user_mode":               d.get("user_mode", "private"),
        "share_email":             d.get("share_email", ""),
        # all = every case | related = case + related cases | single = case only
        "case_scope":              d.get("case_scope", "all"),
        "years_back":              d.get("years_back", "12"),
        # Per-platform sync config (each portal independent):
        "net_scope":               d.get("net_scope", "selected"),   # all | selected
        "net_related":             d.get("net_related", False),      # NET only: include related cases
        "bdr_scope":               d.get("bdr_scope", "all"),        # all | selected
        "eca_scope":               d.get("eca_scope", "selected"),   # all | selected
        "browser_visible":         d.get("browser_visible", True),   # show automation browser
        "govil_configured":        _govil_creds_exist(),
    }


def _govil_creds_exist() -> bool:
    """Reports only WHETHER credentials exist — never the values."""
    try:
        import keyring
        return bool(keyring.get_password("gov-il-connect", "id_number")
                    and keyring.get_password("gov-il-connect", "password"))
    except Exception:
        return False


class SettingsUpdate(BaseModel):
    court_docs_dir: str = ""
    check_viewers: Optional[bool] = None
    download_related_cases: Optional[bool] = None
    mode: Optional[str] = None
    storage_mode: Optional[str] = None
    login_method: Optional[str] = None
    otp_method: Optional[str] = None    # "email" auto / "sms" manual to phone
    user_mode: Optional[str] = None
    share_email: Optional[str] = None   # Drive read-only share / שיתוף צפייה בדרייב
    case_scope: Optional[str] = None    # all | related | single
    years_back: Optional[str] = None    # search window for "התיקים שלי"
    net_scope: Optional[str] = None
    net_related: Optional[bool] = None
    bdr_scope: Optional[str] = None
    eca_scope: Optional[str] = None
    browser_visible: Optional[bool] = None
    # EN: change gov.il credentials from the UI — written straight to the OS
    #     keychain, never stored in files and never echoed back.
    # HE: החלפת ת"ז/סיסמה מה-UI — נכתב ישירות ל-Keychain, לא לקבצים.
    govil_id: Optional[str] = None
    govil_password: Optional[str] = None


@app.post("/api/settings")
def save_settings(req: SettingsUpdate):
    import json
    defaults_path = config.PROJECT_ROOT / "session_defaults.json"
    d = {}
    if defaults_path.exists():
        try:
            d = json.loads(defaults_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if req.court_docs_dir:
        d["court_docs_dir"] = req.court_docs_dir
    elif "court_docs_dir" in d:
        del d["court_docs_dir"]
    _BOOL_FIELDS = {"check_viewers", "download_related_cases", "net_related",
                    "browser_visible"}
    _STR_FIELDS  = {"mode", "storage_mode", "login_method", "otp_method", "user_mode",
                    "share_email", "case_scope", "years_back",
                    "net_scope", "bdr_scope", "eca_scope"}
    for f in _BOOL_FIELDS:
        v = getattr(req, f)
        if v is not None:
            d[f] = v
    for f in _STR_FIELDS:
        v = getattr(req, f)
        if v is not None:
            d[f] = v
    defaults_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    from pathlib import Path as _P
    config.COURT_DOCS_DIR = _P(req.court_docs_dir).expanduser().resolve() if req.court_docs_dir else (config.PROJECT_ROOT / "court_documents")
    # sync into SESSION_SETTINGS immediately
    try:
        from core.download import SESSION_SETTINGS as _SS
        for f in _BOOL_FIELDS:
            v = getattr(req, f)
            if v is not None:
                _SS[f] = v
        for f in _STR_FIELDS:
            v = getattr(req, f)
            if v is not None:
                _SS[f] = v
    except Exception:
        pass
    # gov.il credentials → OS keychain only / ת"ז וסיסמה — ל-Keychain בלבד
    if req.govil_id or req.govil_password:
        try:
            import keyring
            if req.govil_id and req.govil_id.strip():
                keyring.set_password("gov-il-connect", "id_number", req.govil_id.strip())
            if req.govil_password:
                keyring.set_password("gov-il-connect", "password", req.govil_password)
        except Exception as exc:
            return {"ok": False, "error": f"keychain: {exc}",
                    "court_docs_dir_effective": str(config.COURT_DOCS_DIR)}
    return {"ok": True, "court_docs_dir_effective": str(config.COURT_DOCS_DIR),
            "govil_configured": _govil_creds_exist()}

# --- AI chat -----------------------------------------------------------------

class AiAskRequest(BaseModel):
    question: str
    sub_case_id: Optional[int] = None
    api_key: str = ""


@app.post("/api/ai/ask")
def ai_ask(req: AiAskRequest):
    from .httpd import _ai_ask_handler
    return _ai_ask_handler({"question": req.question,
                            "sub_case_id": req.sub_case_id,
                            "api_key": req.api_key})


# --- SSE ---------------------------------------------------------------------

@app.get("/events")
def events():
    q = jobs.subscribe()

    def gen():
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except _queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            jobs.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
