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
    pool = jobs._pool if hasattr(jobs, "_pool") else None
    ctx  = getattr(pool, "_ctx", None)

    def _binfo(bm):
        if not bm:
            return {"available": False, "headless": True, "alive": False, "busy": False}
        return {"available": True, "headless": bm.headless,
                "alive": bm.is_alive() or bm.busy, "busy": bm.busy,
                "url": getattr(bm, "last_url", "")}

    main = getattr(ctx, "browser",     None) if ctx else None
    bdr  = getattr(ctx, "bdr_browser", None) if ctx else None
    eca  = getattr(ctx, "eca_browser", None) if ctx else None
    return {
        "main": _binfo(main),
        "bdr":  _binfo(bdr),
        "eca":  _binfo(eca),
        # backwards compat
        **_binfo(main),
    }


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


@app.post("/api/actions/pause_download")
def act_pause_download(job_id: int = 0):
    from .collector_bridge import pause_download
    pause_download(job_id)
    return {"ok": True}


@app.post("/api/actions/resume_download")
def act_resume_download(job_id: int = 0):
    from .collector_bridge import resume_download
    resume_download(job_id)
    return {"ok": True}


@app.post("/api/actions/toggle_browser_visible")
async def act_toggle_browser_visible(request: Request):
    """Show or hide the automation browser window mid-download."""
    from .collector_bridge import get_browser_for_portal
    body = await request.json()
    portal = (body.get("portal") or "NET").upper()
    visible = bool(body.get("visible", True))
    bm = get_browser_for_portal(portal)
    if bm is None:
        raise HTTPException(404, "no browser for portal")
    try:
        if visible:
            bm.show()
        else:
            bm.hide()
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "visible": visible}


@app.post("/api/actions/send_log_email")
async def act_send_log_email(request: Request):
    """Send the last 300 lines of latest.log to the given email address."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    to_addr = (body.get("to") or "").strip()
    if not to_addr:
        raise HTTPException(400, "missing 'to' address")
    try:
        from core.scheduler import send_log_email
        send_log_email(config.PROJECT_ROOT, to_addr)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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


@app.post("/api/actions/download_all")
async def act_download_all(request: Request):
    """Start sync on all enabled portals at once.
    Body (optional): {"open_only": true}  — pass open-cases-only filter to each portal job.
    """
    import json as _json
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    open_only: bool = bool(body.get("open_only", False))

    defaults_path = config.PROJECT_ROOT / "session_defaults.json"
    d: dict = {}
    if defaults_path.exists():
        try:
            d = _json.loads(defaults_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    job_ids = []
    if d.get("portal_net_enabled", True):
        net_filter = "open" if open_only else "all"
        job_ids.append(jobs.submit("net_auto_update", {"open_filter": net_filter}))
    if d.get("portal_bdr_enabled", True):
        bdr_params: dict = {"open_only": open_only} if open_only else {}
        job_ids.append(jobs.submit("bdr_batch", bdr_params))
    if d.get("portal_eca_enabled", True):
        try:
            from .collector_bridge import _eca_handler_exists
            if _eca_handler_exists():
                eca_params: dict = {"open_only": open_only} if open_only else {}
                job_ids.append(jobs.submit("eca_batch", eca_params))
        except Exception:
            pass
    return {"job_ids": job_ids, "count": len(job_ids)}


@app.post("/api/actions/bdr_batch")
async def act_bdr_batch(request: Request, force_rerun: bool = False,
                        client_filter: str = ""):
    """הורדת תיקי בד"ר — הכל, או רק התיקים שנבחרו ({"cases": [...]})."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    payload = {"force_rerun": body.get("force_rerun", force_rerun),
               "client_filter": body.get("client_filter", client_filter),
               "cases": body.get("cases") or [],
               "sub_cases": body.get("sub_cases") or []}
    if body.get("user_mode"):
        payload["user_mode"] = body["user_mode"]
    return {"job_id": jobs.submit("bdr_batch", payload)}


@app.post("/api/actions/bdr_list")
def act_bdr_list():
    """התחברות והצגת תיקי בד"ר לבחירה — כמו נט והוצל"פ."""
    return {"job_id": jobs.submit("bdr_list", {})}


@app.get("/api/net/cases")
def get_net_cases():
    """Latest NET case list (cumulative cache — same contract as ECA/BDR)."""
    try:
        from LIAS.collector_bridge import get_last_net_cases
        return {"cases": get_last_net_cases()}
    except Exception:
        return {"cases": []}


@app.get("/api/bdr/cases")
def get_bdr_cases():
    """Latest BDR case list (same contract as /api/eca/cases)."""
    try:
        from LIAS.collector_bridge import get_last_bdr_cases
        return {"cases": get_last_bdr_cases()}
    except Exception:
        return {"cases": []}


@app.get("/api/cases/all")
def get_all_cases(q: str = ""):
    """Every case this installation knows about, from all three portals, merged
    into one list and marked with whether its documents were actually downloaded.

    Served from the on-disk case cache, so it answers with NO portal login —
    the user can search and see what exists (and what is still missing) offline.
    Optional `q` filters on case number, parties, court or client."""
    from LIAS.collector_bridge import (get_last_net_cases, get_last_bdr_cases,
                                       get_last_eca_cases, downloaded_case_index)
    PORTALS = (("NET", 'נט המשפט', get_last_net_cases),
               ("BDR", 'בית הדין הרבני', get_last_bdr_cases),
               ("ECA", 'הוצאה לפועל', get_last_eca_cases))
    have = downloaded_case_index()
    out = []
    for code, label, getter in PORTALS:
        try:
            cases = getter() or []
        except Exception:
            cases = []
        for c in cases:
            num = str(c.get("number") or c.get("display_id")
                      or c.get("CaseDisplayIdentifier") or "").strip()
            if not num:
                continue
            info = have.get(num) or {}
            parties = c.get("parties") or []
            if not parties and c.get("party"):
                parties = [{"role": c.get("role") or "", "name": c["party"]}]
            out.append({
                "number": num,
                "portal": code,
                "portal_label": label,
                "type": c.get("type") or c.get("CaseTypeShortName") or "",
                "court": c.get("court") or c.get("CourtName") or "",
                "status": c.get("status") or c.get("CaseStatusName") or "",
                "open_date": c.get("open_date") or c.get("OpenDate") or "",
                "close_date": c.get("close_date") or "",
                "client": c.get("client") or c.get("party") or "",
                "parties": parties,
                "sub_cases": c.get("sub_cases") or [],
                "downloaded": bool(info),
                "doc_count": info.get("docs", 0),
                "folder": info.get("folder", ""),
            })
    # Cases sitting on disk that no portal listing covers (a cache was cleared,
    # or the documents arrived before the cache existed). Without this the view
    # would claim "no cases" while 1000+ documents are downloaded.
    seen = {r["number"] for r in out}
    for num, info in have.items():
        if num in seen:
            continue
        out.append({
            "number": num,
            "portal": info.get("portal", ""),
            "portal_label": info.get("portal_label", 'לא ידוע'),
            "type": "", "court": "", "status": "",
            "open_date": "", "close_date": "",
            "client": info.get("client", ""),
            "parties": info.get("parties", []),
            "sub_cases": [],
            "downloaded": True,
            "doc_count": info.get("docs", 0),
            "folder": info.get("folder", ""),
            "from_disk": True,
        })

    if q:
        needle = q.strip().lower()

        def _hit(r: dict) -> bool:
            hay = " ".join([r["number"], r["type"], r["court"], r["client"],
                            r["portal_label"],
                            " ".join(p.get("name", "") for p in r["parties"])])
            return needle in hay.lower()

        out = [r for r in out if _hit(r)]
    out.sort(key=lambda r: (r["downloaded"], r["number"]))
    return {"cases": out,
            "total": len(out),
            "missing": sum(1 for r in out if not r["downloaded"])}


@app.get("/api/cases/db")
def get_cases_db(portal: str = "", client_id: int = 0, open_only: bool = False):
    """Return case_cards directly from the local DB — includes last_synced,
    doc counts, status, and parties.  No portal login needed.
    Query params: portal (NET/BDR/ECA), client_id, open_only=true."""
    from ui_modules.db import _case_cards, _doc_rows
    import sqlite3, os
    db_path = str(config.PROJECT_ROOT / "lias.db")
    if not os.path.exists(db_path):
        return {"cases": [], "total": 0}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    rows = _doc_rows(con)
    cards = _case_cards(rows)
    if portal:
        cards = [c for c in cards if c.get("portal", "").upper() == portal.upper()]
    if client_id:
        cards = [c for c in cards if c.get("client_id") == client_id]
    if open_only:
        cards = [c for c in cards if (c.get("portal_status") or "").lower() not in ("סגור", "closed", "")]
    return {"cases": cards, "total": len(cards)}


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
async def act_eca_sync(request: Request):
    """הורדת תיקי ההוצאה לפועל — הכל, או רק התיקים שנבחרו ({"cases": [...]})."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return {"job_id": jobs.submit("eca_sync", body or {})}


@app.get("/api/tools/export_session")
def export_session(portal: str = "NET"):
    """Export current cookies from the running Playwright browser context for a portal.
    Returns the same storage_state JSON that import_session / export_session.py produce.
    Use this when the lawyer is already logged in and wants to share their own session."""
    import time as _t
    from .collector_bridge import get_browser_for_portal
    bm = get_browser_for_portal(portal.upper())
    if bm is None:
        raise HTTPException(503, "browser not available — start the engine first")

    def _get(page):
        return page.context.storage_state()

    try:
        state = bm.run(lambda page: page.context.storage_state(), timeout=15)
    except Exception as e:
        raise HTTPException(500, f"could not export cookies: {e}")

    payload = {
        "portal":        portal.upper(),
        "exported_at":   _t.time(),
        "exported_iso":  _t.strftime("%Y-%m-%dT%H:%M:%S"),
        "storage_state": state,
    }
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="session_{portal.lower()}.json"'}
    )


@app.post("/api/actions/import_session")
async def act_import_session(request: Request):
    """Inject a client-exported session (cookies) into the portal browser context.
    Payload: the full JSON produced by tools/export_session.py
    {"portal": "bdr"|"net"|"eca", "url": "...", "storage_state": {...}, "exported_at": N}
    The portal browser is shown so the user can verify the session is active."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    portal = (body.get("portal") or "").upper()
    if portal not in ("BDR", "NET", "ECA"):
        raise HTTPException(400, f"Unknown portal: {portal}")
    storage_state = body.get("storage_state")
    if not storage_state or not isinstance(storage_state.get("cookies"), list):
        raise HTTPException(400, "storage_state.cookies missing")
    return {"job_id": jobs.submit("import_session", {
        "portal": portal,
        "url": body.get("url", ""),
        "storage_state": storage_state,
    })}


@app.post("/api/actions/cancel_case")
async def act_cancel_case(request: Request):
    """Stop the download of ONE specific case without aborting the batch."""
    from .collector_bridge import cancel_case
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    job_id = int(body.get("job_id") or 0)
    case = str(body.get("case") or "")
    if job_id and case:
        cancel_case(job_id, case)
    return {"ok": bool(job_id and case)}


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
async def act_net_download_all(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    years_back = int(body.get("years_back", 20))
    open_filter = body.get("open_filter", "all")
    return {"job_id": jobs.submit("net_download_all", {"years_back": years_back, "open_filter": open_filter})}


@app.post("/api/actions/reorganize_folders")
def act_reorganize_folders():
    """Move flat case folders into party-grouped parent folders, then re-import."""
    from core.download import reorganize_downloads
    result = reorganize_downloads()
    if result.get("moved"):
        from LIAS.migrate_csv import migrate
        migrate()
    return result


@app.post("/api/actions/reorganize_by_client")
def act_reorganize_by_client():
    """Re-assign all cases to inferred clients (by recurring party name) and re-import."""
    from core.client_inference import reorganize_cases_by_client
    from core.download import SESSION_SETTINGS
    from ui_modules import db as _db
    import config
    lawyer = SESSION_SETTINGS.get("lawyer_name") or SESSION_SETTINGS.get("share_name") or ""
    if not lawyer:
        raise HTTPException(400, "lawyer_name not set in session — run a sync first")
    moved = reorganize_cases_by_client(config.COURT_DOCS_DIR / "downloads", lawyer)
    # re-import so dashboard reflects new client assignments
    from LIAS.collector_bridge import _reimport_folder
    n = _reimport_folder(config.COURT_DOCS_DIR / "downloads")
    return {"moved": moved, "reimported": n}


@app.get("/api/cases/{sub_case_id}/viewers")
def get_case_viewers(sub_case_id: int):
    """Return viewers_registry.csv data for a NET case."""
    import csv as _csv
    import config
    from ui_modules.db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.case_number, c.portal FROM sub_cases s "
            "JOIN cases c ON c.case_id=s.case_id "
            "WHERE s.sub_case_id=?", (sub_case_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    if row["portal"] != "NET":
        return {"viewers": [], "note": "only NET portal tracks viewers"}
    # Find the case folder(s) that match case_number
    base = config.COURT_DOCS_DIR / "downloads"
    case_num = row["case_number"] or ""
    matches = list(base.rglob(f"*{case_num}*"))
    viewers: list[dict] = []
    seen: set[tuple] = set()
    for m in matches:
        reg = m if m.is_file() and m.name == "viewers_registry.csv" \
              else m / "viewers_registry.csv" if m.is_dir() else None
        if not reg or not reg.exists():
            continue
        try:
            with reg.open(encoding="utf-8-sig", newline="") as f:
                for r in _csv.DictReader(f):
                    key = (r.get("שם",""), r.get("אופן צפיה",""))
                    if key not in seen:
                        seen.add(key)
                        viewers.append(r)
        except Exception:
            pass
    return {"viewers": viewers, "case_number": case_num}


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
        "otp_source":              d.get("otp_source", "email"),   # email | totp
        "totp_configured":         _totp_exists(),
        "user_mode":               d.get("user_mode", "private"),
        "lawyer_name":             d.get("lawyer_name", ""),
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
        # Portal enable/disable
        "portal_net_enabled":      d.get("portal_net_enabled", True),
        "portal_bdr_enabled":      d.get("portal_bdr_enabled", True),
        "portal_eca_enabled":      d.get("portal_eca_enabled", True),
        # Auto-sync scheduling
        "auto_sync_enabled":       d.get("auto_sync_enabled", False),
        "auto_sync_interval_hours": d.get("auto_sync_interval_hours", 4),
        # Developer log email
        "log_email_enabled":       d.get("log_email_enabled", False),
        "log_email_to":            d.get("log_email_to", ""),
    }


_creds_exist_cache: list = []          # [(timestamp, value)] — one entry


def _invalidate_creds_cache() -> None:
    _creds_exist_cache.clear()


def _govil_creds_exist() -> bool:
    """Reports only WHETHER credentials exist — never the values.

    Cached, because this sits on GET /api/settings, which the sync screen calls
    on every render. The dashboard auto-refreshes every 10s and its data changes
    constantly during a download, so the screen re-rendered continuously and
    each render did two keychain reads. Every read can raise the macOS "allow
    access?" prompt when the running Python is not on the item's ACL — which is
    why prompts appeared to pop up nonstop. The answer only changes when we
    write, so writes clear the cache."""
    import time as _t
    if _creds_exist_cache:
        ts, val = _creds_exist_cache[0]
        if _t.time() - ts < 300.0:
            return val
    from core import keychain
    val = bool(keychain.get_password("gov-il-connect", "id_number")
               and keychain.get_password("gov-il-connect", "password"))
    _creds_exist_cache[:] = [(_t.time(), val)]
    return val


def _totp_exists() -> bool:
    try:
        from core.totp import totp_configured
        return totp_configured()
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
    otp_source: Optional[str] = None    # "email" | "totp" (Google Authenticator)
    user_mode: Optional[str] = None
    lawyer_name: Optional[str] = None
    share_email: Optional[str] = None   # Drive read-only share / שיתוף צפייה בדרייב
    case_scope: Optional[str] = None    # all | related | single
    years_back: Optional[str] = None    # search window for "התיקים שלי"
    net_scope: Optional[str] = None
    net_related: Optional[bool] = None
    bdr_scope: Optional[str] = None
    eca_scope: Optional[str] = None
    browser_visible: Optional[bool] = None
    # Portal enable/disable
    portal_net_enabled: Optional[bool] = None
    portal_bdr_enabled: Optional[bool] = None
    portal_eca_enabled: Optional[bool] = None
    # Auto-sync scheduling
    auto_sync_enabled: Optional[bool] = None
    auto_sync_interval_hours: Optional[int] = None
    # Developer log email
    log_email_enabled: Optional[bool] = None
    log_email_to: Optional[str] = None
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
                    "browser_visible", "portal_net_enabled", "portal_bdr_enabled",
                    "portal_eca_enabled", "auto_sync_enabled", "log_email_enabled"}
    _STR_FIELDS  = {"mode", "storage_mode", "login_method", "otp_method", "otp_source",
                    "user_mode", "lawyer_name", "share_email", "case_scope", "years_back",
                    "net_scope", "bdr_scope", "eca_scope", "log_email_to"}
    _INT_FIELDS  = {"auto_sync_interval_hours"}
    for f in _BOOL_FIELDS:
        v = getattr(req, f, None)
        if v is not None:
            d[f] = v
    for f in _STR_FIELDS:
        v = getattr(req, f, None)
        if v is not None:
            d[f] = v
    for f in _INT_FIELDS:
        v = getattr(req, f, None)
        if v is not None:
            d[f] = int(v)
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
            _invalidate_creds_cache()      # next read reflects the new state
        except Exception as exc:
            return {"ok": False, "error": f"keychain: {exc}",
                    "court_docs_dir_effective": str(config.COURT_DOCS_DIR)}
    return {"ok": True, "court_docs_dir_effective": str(config.COURT_DOCS_DIR),
            "govil_configured": _govil_creds_exist()}

# --- Query API ---------------------------------------------------------------

@app.get("/api/query")
def query(q: str = "", portal: str = "", court: str = "",
          client: str = "", limit: int = 100):
    """General search across clients, cases and documents.

    GET /api/query?q=פונברשטיין  — free-text search
    GET /api/query?portal=NET    — filter by portal
    GET /api/query?court=שלום    — filter by court
    GET /api/query?client=דוד    — filter by client name
    """
    conn = db.get_conn()
    sql = """SELECT d.document_id, d.logical_name, d.doc_type,
                    d.submission_date, d.download_status, d.pages,
                    s.sub_case_id, s.sub_number,
                    ca.case_number, ca.portal, ca.court, ca.title,
                    cl.display_name AS client_name, cl.client_id
             FROM documents d
             JOIN sub_cases s  ON s.sub_case_id = d.sub_case_id
             JOIN cases ca     ON ca.case_id    = s.case_id
             LEFT JOIN clients cl ON cl.client_id = ca.client_id
             WHERE 1=1"""
    args: list = []
    if q:
        sql += """ AND (d.logical_name LIKE ? OR d.doc_type LIKE ?
                   OR s.sub_number LIKE ? OR ca.title LIKE ?
                   OR cl.display_name LIKE ?)"""
        p = f"%{q}%"
        args += [p, p, p, p, p]
    if portal:
        sql += " AND ca.portal = ?"
        args.append(portal.upper())
    if court:
        sql += " AND ca.court LIKE ?"
        args.append(f"%{court}%")
    if client:
        sql += " AND cl.display_name LIKE ?"
        args.append(f"%{client}%")
    sql += f" ORDER BY d.submission_date DESC LIMIT ?"
    args.append(min(limit, 500))
    rows = [dict(r) for r in conn.execute(sql, args)]
    cases_sql = """SELECT DISTINCT ca.case_number, ca.portal, ca.court, ca.title,
                          cl.display_name AS client_name
                   FROM cases ca
                   LEFT JOIN clients cl ON cl.client_id = ca.client_id
                   WHERE 1=1"""
    c_args: list = []
    if q:
        c_args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        cases_sql += " AND (ca.case_number LIKE ? OR ca.title LIKE ? OR cl.display_name LIKE ?)"
    if portal:
        cases_sql += " AND ca.portal = ?"
        c_args.append(portal.upper())
    if court:
        cases_sql += " AND ca.court LIKE ?"
        c_args.append(f"%{court}%")
    if client:
        cases_sql += " AND cl.display_name LIKE ?"
        c_args.append(f"%{client}%")
    case_rows = [dict(r) for r in conn.execute(cases_sql, c_args)]
    return {"documents": rows, "cases": case_rows,
            "total_docs": len(rows), "total_cases": len(case_rows)}


@app.get("/api/stats")
def stats():
    """System statistics — overview of all data in the DB."""
    conn = db.get_conn()
    r = conn.execute("""
        SELECT COUNT(DISTINCT cl.client_id) AS clients,
               COUNT(DISTINCT ca.case_id) AS cases,
               COUNT(DISTINCT s.sub_case_id) AS sub_cases,
               COUNT(d.document_id) AS documents,
               COALESCE(SUM(d.pages), 0) AS pages,
               SUM(CASE WHEN d.download_status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN d.download_status IN ('ERROR','MISSING') THEN 1 ELSE 0 END) AS errors
        FROM clients cl
        LEFT JOIN cases ca ON ca.client_id = cl.client_id
        LEFT JOIN sub_cases s ON s.case_id = ca.case_id
        LEFT JOIN documents d ON d.sub_case_id = s.sub_case_id
    """).fetchone()
    portals = [dict(p) for p in conn.execute("""
        SELECT ca.portal, COUNT(DISTINCT ca.case_id) AS cases,
               COUNT(d.document_id) AS documents
        FROM cases ca
        LEFT JOIN sub_cases s ON s.case_id = ca.case_id
        LEFT JOIN documents d ON d.sub_case_id = s.sub_case_id
        GROUP BY ca.portal
    """)]
    return {"summary": dict(r), "by_portal": portals}


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


# ── Analysis / Vector store ───────────────────────────────────────────────────

@app.post("/api/analyze/case/{sub_case_id}")
def analyze_case(sub_case_id: int, force: bool = False):
    """Trigger background analysis pipeline for all documents in a case.
    Returns job-style dict with queued count."""
    from core.doc_pipeline import process_case, queue_document
    from core.download import SESSION_SETTINGS
    from ui_modules.db import get_conn
    if force:
        skip = False
    else:
        skip = True
    # queue each unanalyzed doc
    rows = get_conn().execute(
        """SELECT d.document_id FROM documents d
           LEFT JOIN doc_analysis da ON da.document_id=d.document_id
           WHERE d.sub_case_id=? AND d.physical_name IS NOT NULL
             AND d.physical_name!=''
             AND (? OR da.document_id IS NULL)""",
        (sub_case_id, 1 if not skip else 0)
    ).fetchall()
    for r in rows:
        queue_document(r[0], dict(SESSION_SETTINGS))
    return {"queued": len(rows), "sub_case_id": sub_case_id}


@app.get("/api/analyze/case/{sub_case_id}")
def get_case_analysis(sub_case_id: int):
    """Return stored analysis for all documents in a case."""
    from core.vector_store import get_case_analysis, ensure_schema
    ensure_schema()
    return {"analyses": get_case_analysis(sub_case_id)}


@app.get("/api/analyze/doc/{document_id}")
def get_doc_analysis(document_id: int):
    """Return stored analysis for a single document."""
    from core.vector_store import get_analysis, ensure_schema
    ensure_schema()
    a = get_analysis(document_id)
    if not a:
        raise HTTPException(404, "not analyzed yet")
    return a


@app.get("/api/vector/search")
def vector_search(q: str = "", sub_case_id: int = 0,
                  doc_category: str = "", limit: int = 10):
    """Full-text search across all indexed document chunks."""
    from core.vector_store import search, ensure_schema
    ensure_schema()
    if not q:
        raise HTTPException(400, "q is required")
    results = search(q, limit=min(limit, 50),
                     sub_case_id=sub_case_id or None,
                     doc_category=doc_category or None)
    return {"results": results, "count": len(results)}


@app.get("/api/vector/stats")
def vector_stats():
    """Summary of what's indexed in the vector store."""
    from core.vector_store import stats, ensure_schema
    ensure_schema()
    return stats()


@app.post("/api/analyze/notebook/{sub_case_id}")
def analyze_notebook(sub_case_id: int):
    """Trigger NotebookLM pipeline for a case (background job)."""
    return {"job_id": jobs.submit("notebook_analysis", {"sub_case_id": sub_case_id})}


@app.get("/api/notebooklm/status")
def notebooklm_status():
    """Check if notebooklm CLI is installed and authenticated."""
    import subprocess, shutil
    bin_path = shutil.which("notebooklm") or str(Path.home() / ".local" / "bin" / "notebooklm")
    if not Path(bin_path).exists():
        return {"installed": False, "authenticated": False, "account": None,
                "error": "notebooklm לא מותקן — הרץ: uv tool install 'notebooklm-py[browser]'"}
    try:
        r = subprocess.run([bin_path, "auth", "check", "--json"],
                           capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout.strip() or "{}")
        ok = data.get("status") == "ok"
        return {"installed": True, "authenticated": ok,
                "account": data.get("account"),
                "error": None if ok else "לא מחובר — לחץ 'חבר ל-Google'"}
    except Exception as e:
        return {"installed": True, "authenticated": False, "account": None, "error": str(e)}


@app.post("/api/notebooklm/login")
def notebooklm_login():
    """Import Google cookies from Chrome into notebooklm auth storage."""
    import subprocess, shutil
    bin_path = shutil.which("notebooklm") or str(Path.home() / ".local" / "bin" / "notebooklm")
    if not Path(bin_path).exists():
        raise HTTPException(503, "notebooklm לא מותקן")
    try:
        r = subprocess.run(
            [bin_path, "login", "--browser-cookies", "chrome", "--json"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            # verify auth worked
            r2 = subprocess.run([bin_path, "auth", "check", "--json"],
                                 capture_output=True, text=True, timeout=15)
            data = json.loads(r2.stdout.strip() or "{}")
            if data.get("status") == "ok":
                return {"ok": True, "account": data.get("account")}
        err = r.stderr.strip() or r.stdout.strip()
        return {"ok": False, "error": err[:300] or "login נכשל"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout — Chrome לא נגיש או עוגיות לא נמצאו"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Verdicts scraper (public decisions / החלטות שהותרו לפרסום) ---------------

_VERDICT_COURTS = [
    {"id": "-1", "name": "בחר"},
    {"id": "11",  "name": "העליון"},
    {"id": "16",  "name": "המחוזי באר שבע"},
    {"id": "13",  "name": "המחוזי חיפה"},
    {"id": "14",  "name": "המחוזי ירושלים"},
    {"id": "896", "name": "המחוזי מרכז"},
    {"id": "12",  "name": "המחוזי נוף הגליל-נצרת"},
    {"id": "15",  "name": "המחוזי תל אביב - יפו"},
    {"id": "30",  "name": "שלום ירושלים"},
    {"id": "32",  "name": "שלום תל אביב - יפו"},
    {"id": "26",  "name": "שלום חיפה"},
    {"id": "41",  "name": "שלום באר שבע"},
    {"id": "17",  "name": "שלום נוף הגליל-נצרת"},
    {"id": "38",  "name": "שלום נתניה"},
    {"id": "39",  "name": "שלום כפר סבא"},
    {"id": "35",  "name": "שלום פתח תקווה"},
    {"id": "40",  "name": "שלום ראשון לציון"},
    {"id": "37",  "name": "שלום רחובות"},
    {"id": "33",  "name": "ענייני משפחה במחוז ת\"א"},
    {"id": "47",  "name": "הארצי לעבודה"},
    {"id": "49",  "name": "אזורי לעבודה תל אביב - יפו"},
    {"id": "48",  "name": "אזורי לעבודה ירושלים"},
    {"id": "50",  "name": "אזורי לעבודה חיפה"},
    {"id": "51",  "name": "אזורי לעבודה באר שבע"},
]


@app.get("/api/verdicts/courts")
def verdicts_courts():
    return {"courts": _VERDICT_COURTS}


_JUDGES_CACHE: dict = {}  # court_id → [{value, name}, ...]
_JUDGES_CACHE_PATH = config.PROJECT_ROOT / "verdicts_judges_cache.json"

def _load_judges_cache():
    global _JUDGES_CACHE
    try:
        if _JUDGES_CACHE_PATH.exists():
            import json as _j
            _JUDGES_CACHE = _j.loads(_JUDGES_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass

def _save_judges_cache_to_disk():
    try:
        import json as _j
        _JUDGES_CACHE_PATH.write_text(
            _j.dumps(_JUDGES_CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

_load_judges_cache()


@app.get("/api/verdicts/judges")
def verdicts_judges(court_id: str = ""):
    """Return cached judges for a court (populated by verdicts_refresh_judges job)."""
    if not court_id or court_id in ("-1", ""):
        return {"judges": [], "cached": False}
    judges = _JUDGES_CACHE.get(court_id, [])
    return {"judges": judges, "cached": bool(judges)}


@app.post("/api/verdicts/refresh_judges")
async def verdicts_refresh_judges(request: Request):
    """Start a job that scrapes judges for all courts.
    Body: {headless: bool}  — default False (visible browser)."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    headless = bool(body.get("headless", False))
    return {"job_id": jobs.submit("verdicts_refresh_judges", {"headless": headless})}


@app.post("/api/verdicts/search")
async def verdicts_search(request: Request):
    """Start a verdict scrape job.
    Body: {court_id, judge_name, date_from (DD/MM/YYYY), date_to, max_pages, headless}"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    court_id   = str(body.get("court_id", "-1"))
    judge_name = str(body.get("judge_name", ""))
    date_from  = str(body.get("date_from", ""))
    date_to    = str(body.get("date_to", ""))
    max_pages  = int(body.get("max_pages", 5))
    headless   = bool(body.get("headless", False))
    if court_id == "-1":
        raise HTTPException(400, "יש לבחור בית משפט")
    payload = {"court_id": court_id, "judge_name": judge_name,
               "date_from": date_from, "date_to": date_to,
               "max_pages": max_pages, "headless": headless}
    return {"job_id": jobs.submit("verdict_scrape", payload)}


@app.get("/api/verdicts/results")
def verdicts_results(limit: int = 200):
    """Return previously scraped verdicts. Returns most recent run only."""
    import json as _json
    results_dir = config.COURT_DOCS_DIR / "verdicts"
    if not results_dir.exists():
        return {"verdicts": [], "run_file": "", "search_params": {}}
    runs = sorted(results_dir.glob("run_*.json"), reverse=True)
    if not runs:
        return {"verdicts": [], "run_file": "", "search_params": {}}
    try:
        data = _json.loads(runs[0].read_text(encoding="utf-8"))
        # Verify pdf_path existence so front-end can mark downloaded
        pdf_base = results_dir / "pdfs"
        for v in data.get("verdicts", []):
            p = v.get("pdf_path", "")
            if p and not (pdf_base / Path(p).name).exists():
                v["pdf_path"] = ""  # stale path — file was deleted
        return {
            "verdicts": data.get("verdicts", [])[:limit],
            "run_file": data.get("run_file", str(runs[0])),
            "search_params": {
                "court_id":  data.get("court_id", ""),
                "judge":     data.get("judge", ""),
                "date_from": data.get("date_from", ""),
                "date_to":   data.get("date_to", ""),
            },
        }
    except Exception:
        return {"verdicts": [], "run_file": "", "search_params": {}}


@app.post("/api/verdicts/download_selected")
async def verdicts_download_selected(request: Request):
    """Start a job to download specific verdicts by doc_param.
    Body: {doc_params, court_id, judge_name, date_from, date_to, run_file, headless}"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    doc_params = body.get("doc_params", [])
    if not doc_params:
        raise HTTPException(400, "אין מסמכים לבחירה")
    payload = {
        "court_id":  str(body.get("court_id", "")),
        "judge_name": str(body.get("judge_name", "")),
        "date_from": str(body.get("date_from", "")),
        "date_to":   str(body.get("date_to", "")),
        "doc_params": doc_params,
        "run_file":   str(body.get("run_file", "")),
        "headless":   bool(body.get("headless", False)),
    }
    return {"job_id": jobs.submit("verdict_download", payload)}


@app.get("/api/verdicts/download/{filename}")
def verdicts_download(filename: str):
    """Serve a downloaded verdict PDF."""
    safe = Path(filename).name
    path = config.COURT_DOCS_DIR / "verdicts" / "pdfs" / safe
    if not path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(str(path), media_type="application/pdf",
                        headers={"Content-Disposition": "inline"})


@app.post("/api/verdicts/delete")
async def verdicts_delete(request: Request):
    """Delete a downloaded verdict PDF and clear its pdf_path in all run JSON files."""
    import json as _json
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    filename = Path(body.get("filename", "")).name
    if not filename:
        raise HTTPException(400, "filename required")
    path = config.COURT_DOCS_DIR / "verdicts" / "pdfs" / filename
    if not path.exists():
        raise HTTPException(404, "file not found")
    path.unlink()
    # Clear pdf_path in all run JSON files that reference this file
    results_dir = config.COURT_DOCS_DIR / "verdicts"
    for run_file in results_dir.glob("run_*.json"):
        try:
            data = _json.loads(run_file.read_text(encoding="utf-8"))
            changed = False
            for v in data.get("verdicts", []):
                if Path(v.get("pdf_path", "")).name == filename:
                    v["pdf_path"] = ""
                    changed = True
            if changed:
                run_file.write_text(_json.dumps(data, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        except Exception:
            pass
    return {"ok": True, "deleted": filename}


@app.get("/api/verdicts/library")
def verdicts_library():
    """Return all downloaded verdicts across all run files, deduplicated by case_num."""
    import json as _json
    results_dir = config.COURT_DOCS_DIR / "verdicts"
    if not results_dir.exists():
        return {"verdicts": []}
    pdf_base = results_dir / "pdfs"
    seen: dict[str, dict] = {}  # case_num → verdict row
    for run_file in sorted(results_dir.glob("run_*.json")):
        try:
            data = _json.loads(run_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for v in data.get("verdicts", []):
            p = v.get("pdf_path", "")
            if not p:
                continue
            # Validate PDF still on disk
            if not (pdf_base / Path(p).name).exists():
                continue
            key = v.get("case_num") or p
            if key not in seen or v.get("date", "") > seen[key].get("date", ""):
                seen[key] = v
    rows = sorted(seen.values(), key=lambda r: r.get("date", ""), reverse=True)
    return {"verdicts": rows}


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
