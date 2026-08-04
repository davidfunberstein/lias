#!/usr/bin/env python3
"""LIAS — legal case-management app server / שרת האפליקציה לניהול תיקים.

  * Engine/API :  python -m LIAS.run   -> http://localhost:8400
  * App (this) :  python3 app.py       -> http://localhost:8500
    (run_ui_demo.py נשאר כקיצור תאימות לשם הישן)

Reads lias.db (app root, fallback LIAS/lias.db) in READ-ONLY mode.
Falls back to built-in demo data when the DB is missing, so the
dashboard can be previewed on any machine.

Zero dependencies — Python stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── module imports ──────────────────────────────────────────────────────────
from ui_modules.db import _connect, _full_ui_alive
from ui_modules.dashboard import (
    dashboard_from_db, demo_payload, docs_list, search_all,
    case_view, client_view, _empty_payload, build_dashboard,
)
from ui_modules.notes import (
    _read_notes, _write_notes, _notes_save, _notes_export_pdf, _notes_delete,
)
from ui_modules.engine import _autoreload_watcher, _watchdog
from ui_modules import engine_inproc
from ui_modules.documents import serve_document, _find_soffice, docx_to_pdf
from ui_modules.transcription import (
    _get_whisper_model, _split_audio, _transcribe_chunk,
    _transcribe_worker, _fmt_ts, _list_transcriptions,
    _transcription_jobs, _transcription_lock,
)

# ── global config ───────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "lias.db")
if not os.path.exists(DB_PATH):
    DB_PATH = os.path.join(HERE, "LIAS", "lias.db")
UI_PATH = os.path.join(HERE, "ui_demo", "index.html")
PORT = int(os.environ.get("LIAS_DEMO_PORT", "8500"))
HOST = os.environ.get("LIAS_HOST", "127.0.0.1")
KEYRING_SERVICE = "gov-il-connect"
NOTES_PATH = os.path.join(HERE, "annotations.json")
TRANSCRIPTIONS_DIR = os.path.join(HERE, "transcriptions")
os.makedirs(TRANSCRIPTIONS_DIR, exist_ok=True)

# ── client profiles ─────────────────────────────────────────────────────────
PROFILES_PATH = os.path.join(HERE, "profiles.json")
_profile_state: dict = {"active": None}  # mutable — no global needed in handlers

def _active_db_path() -> str:
    """Return the DB path for the currently active profile, or the main DB."""
    active = _profile_state.get("active")
    if active:
        return os.path.join(HERE, "profiles_db", active["slug"], "lias.db")
    return DB_PATH

def _load_profiles() -> list:
    try:
        if os.path.exists(PROFILES_PATH):
            return json.loads(open(PROFILES_PATH, encoding="utf-8").read())
    except Exception:
        pass
    return []

def _save_profiles(profiles: list) -> None:
    with open(PROFILES_PATH, "w", encoding="utf-8") as fh:
        json.dump(profiles, fh, ensure_ascii=False, indent=2)

def _slugify(name: str) -> str:
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = re.sub(r"[^\w\s-]", "", nfkd, flags=re.ASCII).strip().lower()
    slug = re.sub(r"[\s-]+", "_", ascii_name)
    return slug or f"profile_{uuid.uuid4().hex[:8]}"

# ── shared mutable state (passed to modules as holder dicts) ────────────────
_engine_proc_holder: dict = {"proc": None}
_server_ref: dict = {"server": None}
_shutting_down_flag: dict = {"value": False}
_heartbeat_holder: dict = {"value": 0.0}


# ── convenience wrappers that bind globals to module functions ──────────────
def _do_connect():
    return _connect(_active_db_path())

def _do_full_ui_alive():
    return engine_inproc.alive()

def _do_build_dashboard():
    return build_dashboard(_active_db_path())

def _raw_tables():
    con = _connect(_active_db_path())
    if con is None:
        return {"clients": [], "cases": [], "documents": []}
    try:
        clients = [dict(r) for r in con.execute("""
            SELECT c.client_id, c.display_name,
                   COUNT(DISTINCT ca.case_id) AS cases,
                   COUNT(DISTINCT s.sub_case_id) AS sub_cases,
                   COUNT(d.document_id) AS docs,
                   COALESCE(SUM(d.pages), 0) AS pages,
                   SUM(CASE WHEN d.download_status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN d.download_status='PENDING' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN d.download_status IN ('ERROR','MISSING') THEN 1 ELSE 0 END) AS errors
            FROM clients c
            LEFT JOIN cases ca ON ca.client_id = c.client_id
            LEFT JOIN sub_cases s ON s.case_id = ca.case_id
            LEFT JOIN documents d ON d.sub_case_id = s.sub_case_id
            GROUP BY c.client_id ORDER BY c.display_name""")]
        cases = [dict(r) for r in con.execute("""
            SELECT s.sub_case_id, s.sub_number, ca.case_number, ca.portal,
                   cl.display_name AS client_name, cl.client_id,
                   COUNT(d.document_id) AS docs,
                   COALESCE(SUM(d.pages), 0) AS pages,
                   SUM(CASE WHEN d.download_status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN d.download_status='PENDING' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN d.download_status IN ('ERROR','MISSING') THEN 1 ELSE 0 END) AS errors,
                   MIN(d.submission_date) AS first_date,
                   MAX(d.submission_date) AS last_date
            FROM sub_cases s
            JOIN cases ca ON ca.case_id = s.case_id
            LEFT JOIN clients cl ON cl.client_id = ca.client_id
            LEFT JOIN documents d ON d.sub_case_id = s.sub_case_id
            GROUP BY s.sub_case_id ORDER BY cl.display_name, s.sub_number""")]
        syncs = [dict(r) for r in con.execute("""
            SELECT sr.run_id, sr.portal, s.sub_number, sr.started_at,
                   sr.total_in_portal, sr.downloaded_new, sr.re_downloaded,
                   sr.failed, sr.hash_changed
            FROM sync_runs sr
            JOIN sub_cases s ON s.sub_case_id = sr.sub_case_id
            ORDER BY sr.run_id DESC LIMIT 50""")]
    finally:
        con.close()
    return {"clients": clients, "cases": cases, "syncs": syncs}

def _do_start_engine():
    return engine_inproc.start()

def _do_stop_engine():
    return engine_inproc.stop()

def _do_shutdown_all(reason: str):
    if _shutting_down_flag.get("value"):
        return
    _shutting_down_flag["value"] = True
    print(f"\n[shutdown] {reason} — closing engine and server / סוגר מנוע ושרת…")
    engine_inproc.stop()
    server = _server_ref.get("server")
    if server is not None:
        threading.Thread(target=server.shutdown, daemon=True).start()

def _do_restart_engine():
    return engine_inproc.restart()

def _do_serve_document(document_id: int):
    return serve_document(document_id, DB_PATH, HERE)


# ── gov.il credentials (small, kept here) ──────────────────────────────────
# Cache for "is it configured?" answers. Every read of a keychain item can make
# macOS raise its "allow access?" prompt — most often after the app is launched
# by a different Python than the one that stored the item, because the binary is
# no longer on the item's ACL. The status endpoints are called on every settings
# open and on the startup check, so each of those turned into another prompt and
# the user got them "every second". A configured/not-configured answer changes
# only when we ourselves save, so it is cached and invalidated on write.
_status_cache: dict = {}
_STATUS_TTL_SEC = 300.0


def _cached_status(key: str, fn):
    import time as _t
    hit = _status_cache.get(key)
    if hit and (_t.time() - hit[0]) < _STATUS_TTL_SEC:
        return hit[1]
    val = fn()
    _status_cache[key] = (_t.time(), val)
    return val


def _govil_status() -> dict:
    def _read() -> dict:
        from core import keychain
        has_id = bool(keychain.get_password(KEYRING_SERVICE, "id_number")
                      or keychain.get_password(KEYRING_SERVICE, "id"))
        has_pw = bool(keychain.get_password(KEYRING_SERVICE, "password"))
        if keychain.is_blocked():
            return {"ok": False, "configured": False, "blocked": True,
                    "error": keychain.REMEDY}
        return {"ok": True, "configured": has_id and has_pw}
    return _cached_status("govil", _read)


def _govil_save(payload: dict) -> dict:
    """Save whichever of ID / password was supplied — each one independently.

    This used to demand BOTH on every save, while the form cleared both fields
    after saving and never showed the stored password again. Correcting only the
    ID therefore meant retyping a password you could not see; a typo there
    silently replaced a working password with a broken one. Now an empty field
    means "leave it as it is", so one value can never clobber the other."""
    try:
        from core import keychain
        gid = (payload.get("id") or "").strip()
        pw = payload.get("password") or ""
        if not gid and not pw:
            return {"ok": False, "error": "לא הוזן דבר — מלא ת.ז. או סיסמה"}
        if gid:
            if not keychain.set_password(KEYRING_SERVICE, "id_number", gid):
                return {"ok": False, "blocked": True, "error": keychain.REMEDY}
            keychain.delete_password(KEYRING_SERVICE, "id")      # legacy key
        if pw:
            if not keychain.set_password(KEYRING_SERVICE, "password", pw):
                return {"ok": False, "blocked": True, "error": keychain.REMEDY}
        _status_cache.pop("govil", None)          # reflect the change at once
        saved = [n for n, v in (("ת.ז.", gid), ("סיסמה", pw)) if v]
        return {"ok": True, "configured": True, "saved": saved,
                "message": "נשמר: " + " + ".join(saved)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── email OTP account (reads the gov.il one-time code from your inbox) ──────
def _email_status() -> dict:
    return _cached_status("email", _email_status_read)


def _email_status_read() -> dict:
    import json
    cfg_path = os.path.join(HERE, "email_config.json")
    address = ""
    has_pw = False
    try:
        if os.path.exists(cfg_path):
            cfg = json.loads(open(cfg_path, encoding="utf-8").read())
            address = cfg.get("imap_user", "")
            if (cfg.get("imap_password") or "").strip():
                has_pw = True
        if not has_pw and address:
            from core import keychain
            has_pw = bool(keychain.get_password("gov-il-connect-email", address))
    except Exception:
        pass
    return {"ok": True, "configured": bool(address and has_pw), "address": address}


def _email_save(payload: dict) -> dict:
    """Save the inbox that receives the gov.il OTP. Address → email_config.json,
    app-password → OS keychain (never stored in the file)."""
    import json
    cfg_path = os.path.join(HERE, "email_config.json")
    address = (payload.get("address") or "").strip()
    app_pw = (payload.get("app_password") or "").strip()
    if not address:
        return {"ok": False, "error": "missing address"}
    host = "imap.gmail.com"
    low = address.lower()
    if "outlook" in low or "hotmail" in low or "live." in low:
        host = "outlook.office365.com"
    elif "yahoo" in low:
        host = "imap.mail.yahoo.com"
    elif "walla" in low:
        host = "imap.walla.co.il"
    try:
        cfg = {}
        if os.path.exists(cfg_path):
            cfg = json.loads(open(cfg_path, encoding="utf-8").read())
        cfg.update({"backend": "imap", "imap_host": host, "imap_port": 993,
                    "imap_user": address, "imap_password": "",
                    "imap_folder": cfg.get("imap_folder", "INBOX"),
                    "sender_filter": cfg.get("sender_filter", "DoNotReply@digital.gov.il"),
                    "otp_regex": cfg.get("otp_regex", r"\b(\d{6})\b")})
        open(cfg_path, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False, indent=2))
        if app_pw:
            from core import keychain
            if not keychain.set_password("gov-il-connect-email", address, app_pw):
                return {"ok": False, "blocked": True, "error": keychain.REMEDY}
        _status_cache.pop("email", None)
        return {"ok": True, "configured": True, "host": host}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── HTTP handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qsl, urlparse
        u = urlparse(self.path)
        path = u.path
        params = dict(parse_qsl(u.query))
        m_case = re.match(r"^/api/case/(\d+)$", path)
        m_client = re.match(r"^/api/client/(\d+)$", path)
        m_doc = re.match(r"^/api/doc/(\d+)$", path)
        if m_case:
            self._json(case_view(int(m_case.group(1)), params, _active_db_path()))
        elif m_client:
            self._json(client_view(int(m_client.group(1)), _active_db_path()))
        elif m_doc:
            import mimetypes
            from urllib.parse import quote
            fpath, fname = _do_serve_document(int(m_doc.group(1)))
            if not fpath:
                self._json({"error": "file not found on disk"}, 404)
                return
            mime = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
            with open(fpath, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            disp = "inline" if mime == "application/pdf" else "attachment"
            self.send_header("Content-Disposition",
                             f"{disp}; filename*=UTF-8''{quote(fname, safe='')}")
            self.end_headers()
            self.wfile.write(body)
        elif re.match(r"^/api/doc_view/(\d+)$", path):
            from urllib.parse import quote
            did = int(re.match(r"^/api/doc_view/(\d+)$", path).group(1))
            fpath, fname = _do_serve_document(did)
            if not fpath:
                self._json({"error": "file not found on disk"}, 404)
                return
            view_path = fpath
            if Path(fpath).suffix.lower() in (".docx", ".doc"):
                conv = docx_to_pdf(fpath)
                if conv:
                    view_path = conv
                else:
                    self._json({"error": "LibreOffice not available for Word→PDF"}, 415)
                    return
            with open(view_path, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             f"inline; filename*=UTF-8''{quote(Path(view_path).name, safe='')}")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/govil/status":
            self._json(_govil_status())
        elif path == "/api/email/status":
            self._json(_email_status())
        elif path == "/api/totp/status":
            try:
                from core.totp import totp_configured, provisioning_uri
                self._json({"configured": totp_configured(),
                            "otpauth": provisioning_uri()})
            except Exception:
                self._json({"configured": False, "otpauth": ""})
        elif path == "/api/google/status":
            try:
                from core.google_login import status as _gstatus
                self._json(_gstatus())
            except Exception:
                self._json({"configured": False, "client_id": "", "allowed_emails": []})
        elif path == "/api/login_audit":
            try:
                from core.login_audit import read_log
                self._json({"entries": read_log(200)})
            except Exception:
                self._json({"entries": []})
        elif path == "/api/notes":
            self._json(_read_notes(NOTES_PATH))
        elif path == "/api/settings":
            code, body, _ct = engine_inproc.request("GET", "/api/settings")
            self._send(code, body, "application/json; charset=utf-8")
        elif path == "/api/proxy/ocr/test":
            code, body, _ct = engine_inproc.request("GET", "/api/ocr/test")
            self._send(code, body, "application/json; charset=utf-8")
        elif path == "/api/proxy/eca/cases":
            code, body, _ct = engine_inproc.request("GET", "/api/eca/cases")
            self._send(code, body, "application/json; charset=utf-8")
        elif path == "/api/proxy/net/cases":
            code, body, _ct = engine_inproc.request("GET", "/api/net/cases")
            self._send(code, body, "application/json; charset=utf-8")
        elif path == "/api/proxy/bdr/cases":
            code, body, _ct = engine_inproc.request("GET", "/api/bdr/cases")
            self._send(code, body, "application/json; charset=utf-8")
        elif path == "/api/engine/state":
            # Unified state endpoint — combines jobs + log tail in one round-trip
            import json as _j
            jobs_code, jobs_body, _ = engine_inproc.request("GET", "/api/jobs?limit=25")
            log_code, log_body, _ = engine_inproc.request("GET", "/api/log?lines=200")
            try:
                tasks = _j.loads(jobs_body) if jobs_code == 200 else []
            except Exception:
                tasks = []
            try:
                log_tail = _j.loads(log_body).get("lines", []) if log_code == 200 else []
            except Exception:
                log_tail = []
            self._json({"tasks": tasks, "log_tail": log_tail})
        elif path.startswith("/api/doc_pdf/") or path.startswith("/api/query") \
                or path.startswith("/api/stats") \
                or path.startswith("/api/verdicts/") \
                or path.startswith("/api/cases") \
                or path.startswith("/api/tools/") \
                or path in (
                "/api/browser/screenshot", "/api/browser/status", "/api/log",
                "/api/jobs"):
            code, body, ct = engine_inproc.request("GET", self.path)
            self._send(code, body, ct or "application/octet-stream")
        elif path == "/api/events":
            import queue as _q, json as _j
            jobs_mod = engine_inproc.events_queue()
            if jobs_mod is None:
                self._json({"error": "engine offline"}, 502)
                return
            q = jobs_mod.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(
                            f"data: {_j.dumps(ev, ensure_ascii=False)}\n\n".encode())
                    except _q.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                jobs_mod.unsubscribe(q)
            return
        elif path == "/api/profiles":
            profiles = _load_profiles()
            self._json({"profiles": profiles, "active": _profile_state["active"]})
        elif path == "/api/docs":
            self._json(docs_list(params, _active_db_path()))
        elif path == "/api/search":
            self._json(search_all(params.get("q", ""), _active_db_path()))
        elif path in ("/", "/index.html"):
            try:
                with open(UI_PATH, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "ui_demo/index.html not found".encode(), "text/plain")
        elif re.match(r"^/[\w.-]+\.(js|css)$", path):
            import mimetypes
            fpath = os.path.join(HERE, "ui_demo", path.lstrip("/"))
            if os.path.isfile(fpath):
                mime = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
                with open(fpath, "rb") as fh:
                    self._send(200, fh.read(), mime)
            else:
                self._send(404, b"not found", "text/plain")
        elif path == "/api/raw_tables":
            self._json(_raw_tables())
        elif path == "/api/dashboard":
            self._json(_do_build_dashboard())
        elif path == "/api/transcriptions":
            self._json({"items": _list_transcriptions(TRANSCRIPTIONS_DIR)})
        elif re.match(r"^/api/transcription/(.+)$", path):
            fname = re.match(r"^/api/transcription/(.+)$", path).group(1)
            fpath = os.path.join(TRANSCRIPTIONS_DIR, os.path.basename(fname))
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as fh:
                    self._send(200, fh.read().encode("utf-8"), "text/markdown; charset=utf-8")
            else:
                self._json({"error": "not found"}, 404)
        elif re.match(r"^/api/transcription_audio/(.+)$", path):
            import mimetypes
            from urllib.parse import quote
            job_id = re.match(r"^/api/transcription_audio/(.+)$", path).group(1)
            from urllib.parse import unquote as _unq
            job_id = _unq(job_id)
            found = None
            # 1) a kept recording next to the transcript (by filename/stem)
            cand = os.path.join(TRANSCRIPTIONS_DIR, os.path.basename(job_id))
            if os.path.isfile(cand):
                found = cand
            else:
                for ext in (".mp3", ".m4a", ".wav", ".ogg", ".webm"):
                    c2 = os.path.join(TRANSCRIPTIONS_DIR, os.path.basename(job_id) + ext)
                    if os.path.isfile(c2):
                        found = c2; break
            # 2) an active job's upload
            upload_dir = os.path.join(TRANSCRIPTIONS_DIR, ".uploads")
            if not found and os.path.isdir(upload_dir):
                for f in os.listdir(upload_dir):
                    if f.startswith(job_id):
                        found = os.path.join(upload_dir, f)
                        break
            if found and os.path.isfile(found):
                mime = mimetypes.guess_type(found)[0] or "audio/mpeg"
                with open(found, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition",
                    f"inline; filename*=UTF-8''{quote(os.path.basename(found), safe='')}")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "audio not found"}, 404)
        elif path == "/api/transcription_status":
            jid = params.get("id", "")
            with _transcription_lock:
                job = dict(_transcription_jobs.get(jid) or {})
                if "partial_lines" in job:
                    job["partial_lines"] = list(job["partial_lines"][-30:])
            self._json(job or {"error": "not found"}, 200 if job else 404)
        elif path == "/api/health":
            con = _do_connect()
            docs = 0
            if con:
                try:
                    docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                finally:
                    con.close()
            self._json({"ok": True, "db": con is not None, "docs": docs,
                        "full_ui_alive": _do_full_ui_alive()})
        elif path in ("/docs", "/redoc", "/openapi.json") or path.startswith("/docs/"):
            code, body, ct = engine_inproc.request("GET", self.path)
            self._send(code, body, ct or "text/html; charset=utf-8")
        else:
            self._json({"error": "not found"}, 404)

    def do_HEAD(self):  # noqa: N802
        clean = self.path.split("?", 1)[0]
        if clean.startswith("/api/doc_pdf/"):
            code, _body, _ct = engine_inproc.request("GET", clean)
            self.send_response(code)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            return
        m_view = re.match(r"^/api/doc_view/(\d+)$", clean)
        if m_view:
            fpath, _ = _do_serve_document(int(m_view.group(1)))
            if not fpath:
                self.send_response(404); self.end_headers(); return
            if Path(fpath).suffix.lower() in (".docx", ".doc") and not _find_soffice():
                self.send_response(415); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            return
        m_doc = re.match(r"^/api/doc/(\d+)$", clean)
        if not m_doc:
            self.send_response(404)
            self.end_headers()
            return
        import mimetypes
        fpath, _ = _do_serve_document(int(m_doc.group(1)))
        if not fpath:
            self.send_response(404)
            self.end_headers()
            return
        mime = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(os.path.getsize(fpath)))
        self.end_headers()

    def do_POST(self):  # noqa: N802
        full_path = self.path
        path = full_path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if path == "/api/transcribe":
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                self._json({"error": "multipart required"}, 400)
                return
            import cgi
            env = {"REQUEST_METHOD": "POST",
                   "CONTENT_TYPE": ctype,
                   "CONTENT_LENGTH": str(length)}
            form = cgi.FieldStorage(fp=self.rfile if not raw else __import__("io").BytesIO(raw),
                                     headers=self.headers, environ=env)
            audio = form["file"] if "file" in form else None
            lang = form.getvalue("language", "he")
            if audio is None or not getattr(audio, 'filename', None):
                self._json({"error": "no file"}, 400)
                return
            job_id = uuid.uuid4().hex[:12]
            ext = Path(audio.filename).suffix or ".wav"
            audio_dir = os.path.join(TRANSCRIPTIONS_DIR, ".uploads")
            os.makedirs(audio_dir, exist_ok=True)
            audio_path = os.path.join(audio_dir, f"{job_id}{ext}")
            with open(audio_path, "wb") as fh:
                fh.write(audio.file.read())
            with _transcription_lock:
                _transcription_jobs[job_id] = {
                    "id": job_id, "filename": audio.filename,
                    "language": lang, "state": "queued",
                    "progress": 0, "message": "בתור…",
                    "created": datetime.now().isoformat(timespec="seconds"),
                }
            threading.Thread(target=_transcribe_worker,
                             args=(job_id, audio_path, lang, audio.filename,
                                   TRANSCRIPTIONS_DIR),
                             daemon=True).start()
            self._json({"ok": True, "id": job_id})
            return

        if path == "/api/upload_doc":
            code, body, _ct = engine_inproc.request(
                "POST", self.path, raw,
                content_type=self.headers.get("Content-Type", ""))
            self._send(code, body, "application/json; charset=utf-8")
            return

        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if path == "/api/govil/logout":
            import json as _jgl
            code, body, _ = engine_inproc.request(
                "POST", "/api/actions/govil_logout", _jgl.dumps({}).encode())
            self._send(code, body, "application/json")
        elif path == "/api/govil/save":
            self._json(_govil_save(payload))
        elif path == "/api/email/save":
            self._json(_email_save(payload))
        elif path == "/api/google/save":
            try:
                from core.google_login import save_config, status as _gstatus
                save_config(payload.get("client_id", ""),
                            payload.get("allowed_emails"))
                self._json({"ok": True, **_gstatus()})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/google/login":
            # Sign in with Google — verified server-side, then audited.
            try:
                from core.google_login import verify_id_token
                from core.login_audit import record
                res = verify_id_token(payload.get("credential", ""))
                if not res.get("ok"):
                    record("APP", "google", "failed", res.get("error", ""),
                           payload.get("hint", ""))
                    self._json({"ok": False, "error": res.get("error")}, 401)
                    return
                record("APP", "google", "success", "כניסה עם Google",
                       f"{res.get('name','')} <{res.get('email','')}>")
                self._json({"ok": True, "email": res.get("email"),
                            "name": res.get("name")})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/app_login":
            # App-level login gate. When a TOTP secret is configured, a valid
            # Google Authenticator code is REQUIRED. Every attempt (success or
            # failure) is written to the login audit log — this is the
            # "control over everyone who connects" the user asked for.
            try:
                from core.totp import totp_configured, verify_totp
                from core.login_audit import record
                name = (payload.get("name") or "").strip()
                role = (payload.get("role") or "").strip()
                code = (payload.get("totp") or "").strip()
                who = f"{name or 'ללא שם'} ({role or '—'})"
                if totp_configured():
                    if not code:
                        record("APP", "totp", "failed", "לא הוזן קוד מאפליקציית האימות", who)
                        self._json({"ok": False, "need_totp": True,
                                    "error": "נדרש קוד מ-Google Authenticator"}, 401)
                        return
                    if not verify_totp(code):
                        record("APP", "totp", "failed", "קוד אימות שגוי", who)
                        self._json({"ok": False, "need_totp": True,
                                    "error": "קוד שגוי — נסה שוב"}, 401)
                        return
                    record("APP", "totp", "success", "כניסה למערכת", who)
                else:
                    record("APP", "local", "success", "כניסה למערכת (ללא TOTP)", who)
                self._json({"ok": True})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/totp/save":
            # payload: {secret: base32 Google Authenticator secret} — stored in
            # the OS keychain (never on disk); empty clears it.
            try:
                from core.totp import set_totp_secret, totp_configured
                ok = set_totp_secret(payload.get("secret", ""))
                self._json({"ok": ok, "configured": totp_configured()})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/transcription_delete":
            # payload: {name: file to delete (md or audio)} — moves to .trash
            try:
                name = os.path.basename(payload.get("name", ""))
                src = os.path.join(TRANSCRIPTIONS_DIR, name)
                if not name or not os.path.exists(src):
                    self._json({"ok": False, "error": "not found"}, 404)
                    return
                trash = os.path.join(TRANSCRIPTIONS_DIR, ".trash")
                os.makedirs(trash, exist_ok=True)
                import shutil as _sh
                _sh.move(src, os.path.join(trash, name))
                self._json({"ok": True})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/transcription_resume":
            # payload: {stem} — re-run transcription on the KEPT audio
            try:
                stem = os.path.basename(payload.get("stem", ""))
                audio = None
                for ext in (".mp3", ".m4a", ".wav", ".ogg", ".webm"):
                    c = os.path.join(TRANSCRIPTIONS_DIR, stem + ext)
                    if os.path.isfile(c):
                        audio = c; break
                if not audio:
                    self._json({"ok": False, "error": "ההקלטה לא נשמרה — העלה אותה מחדש"}, 404)
                    return
                import shutil as _sh
                job_id = uuid.uuid4().hex[:12]
                upload_dir = os.path.join(TRANSCRIPTIONS_DIR, ".uploads")
                os.makedirs(upload_dir, exist_ok=True)
                work = os.path.join(upload_dir, job_id + os.path.splitext(audio)[1])
                _sh.copy(audio, work)
                _transcription_jobs[job_id] = {"state": "queued", "progress": 0,
                                               "message": "ממשיך תמלול…",
                                               "original_name": os.path.basename(audio)}
                threading.Thread(target=_transcribe_worker,
                                 args=(job_id, work, "he", os.path.basename(audio),
                                       TRANSCRIPTIONS_DIR),
                                 daemon=True).start()
                self._json({"ok": True, "id": job_id})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/profiles/create":
            try:
                name = (payload.get("name") or "").strip()
                if not name:
                    self._json({"ok": False, "error": "שם חסר"}, 400); return
                profiles = _load_profiles()
                slug = _slugify(name)
                # ensure slug uniqueness
                existing_slugs = {p["slug"] for p in profiles}
                base_slug = slug
                i = 2
                while slug in existing_slugs:
                    slug = f"{base_slug}_{i}"; i += 1
                profile = {
                    "id": uuid.uuid4().hex[:12],
                    "slug": slug,
                    "name": name,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                # pre-create dirs so the UI can show them
                Path(HERE, "browser_profiles", slug).mkdir(parents=True, exist_ok=True)
                Path(HERE, "court_documents", "profiles", slug).mkdir(parents=True, exist_ok=True)
                Path(HERE, "profiles_db", slug).mkdir(parents=True, exist_ok=True)
                profiles.append(profile)
                _save_profiles(profiles)
                self._json({"ok": True, "profile": profile})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/profiles/activate":
            try:
                pid = (payload.get("id") or "").strip()
                profiles = _load_profiles()
                profile = next((p for p in profiles if p["id"] == pid), None)
                if not profile:
                    self._json({"ok": False, "error": "פרופיל לא נמצא"}, 404); return
                # pause running portal jobs
                paused = []
                try:
                    _jc, _jb, _ = engine_inproc.request("GET", "/api/jobs?limit=25")
                    jobs = json.loads(_jb) if _jc == 200 else []
                    for j in jobs:
                        if j.get("state") in ("RUNNING", "PENDING") and j.get("kind") in (
                            "net_smart_download","net_download_all","net_sync_selected",
                            "bdr_batch","bdr_sync_current","bdr_list","eca_sync","eca_list",
                            "net_list","verdict_scrape","verdict_download"):
                            engine_inproc.request("POST", f"/api/jobs/{j['job_id']}/stop", b"")
                            paused.append(j["job_id"])
                except Exception:
                    pass
                profile["_paused_jobs"] = paused
                _profile_state["active"] = profile
                try:
                    engine_inproc.switch_profile(profile, HERE)
                except Exception:
                    pass
                self._json({"ok": True, "profile": profile, "paused": paused})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/profiles/deactivate":
            try:
                _profile_state["active"] = None
                try:
                    engine_inproc.switch_profile(None, HERE)
                except Exception:
                    pass
                self._json({"ok": True})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/profiles/delete":
            try:
                pid = (payload.get("id") or "").strip()
                profiles = _load_profiles()
                profiles = [p for p in profiles if p["id"] != pid]
                _save_profiles(profiles)
                if _profile_state["active"] and _profile_state["active"].get("id") == pid:
                    _profile_state["active"] = None
                    try:
                        engine_inproc.switch_profile(None, HERE)
                    except Exception:
                        pass
                self._json({"ok": True})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        elif path == "/api/feedback":
            # Debug-phase user notes → dedicated log for the developer
            try:
                line = (f"[{__import__('datetime').datetime.now().isoformat(timespec='seconds')}] "
                        f"[{payload.get('page','?')}] {payload.get('note','').strip()}\n")
                with open(os.path.join(HERE, "user_feedback.log"), "a", encoding="utf-8") as fh:
                    fh.write(line)
                self._json({"ok": True})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
        elif path == "/api/settings":
            code, body, _ct = engine_inproc.request(
                "POST", "/api/settings", json.dumps(payload).encode())
            self._send(code, body, "application/json; charset=utf-8")
        elif path == "/api/ocr/save":
            try:
                import keyring
                key = (payload.get("groq_key") or "").strip()
                if key:
                    keyring.set_password(KEYRING_SERVICE, "groq_api_key", key)
                self._json({"ok": bool(key)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
        elif path == "/api/notes/save":
            self._json(_notes_save(payload, NOTES_PATH))
        elif path == "/api/notes/delete":
            self._json(_notes_delete(payload, NOTES_PATH))
        elif path == "/api/notes/export_pdf":
            self._json(_notes_export_pdf(payload, NOTES_PATH, _do_serve_document))
        elif path == "/api/system/start":
            self._json(_do_start_engine())
        elif path == "/api/system/restart-engine":
            self._json(_do_restart_engine())
        elif path == "/api/heartbeat":
            import time as _t
            _heartbeat_holder["value"] = _t.time()
            self._json({"ok": True})
        elif path == "/api/system/shutdown":
            self._json({"ok": True, "message": "shutting down"})
            threading.Thread(target=_do_shutdown_all,
                             args=("user requested safe shutdown",),
                             daemon=True).start()
        elif path.startswith("/api/actions/") or path.startswith("/api/verdicts/"):
            code, resp_body, ct = engine_inproc.request("POST", self.path, raw)
            self._send(code, resp_body, ct or "application/json")
        elif path.startswith("/api/proxy/"):
            proxy_path = full_path[len("/api/proxy/"):]
            import re as _re
            if not _re.match(r"^actions/[\w/]+", proxy_path.split("?")[0]):
                code, resp_body = 403, b'{"error":"forbidden"}'
            else:
                code, resp_body, _ct = engine_inproc.request(
                    "POST", "/api/" + proxy_path, raw)
            self._send(code, resp_body, "application/json; charset=utf-8")
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):  # quiet
        pass


def _raise_fd_limit() -> None:
    """Raise the open-file limit before anything opens a file descriptor.

    macOS ships a soft limit as low as 256. LIAS runs three Chrome profiles via
    Playwright plus SQLite, the log and the HTTP server, which blows straight
    through it — and the failure is brutal rather than graceful: Playwright
    cannot spawn its driver ("OSError: [Errno 24] Too many open files"), SQLite
    reports "unable to open database file", and all three browser threads enter
    a crash/relaunch loop that never recovers because every retry needs more
    descriptors. Raising the soft limit to the hard limit costs nothing and
    removes the whole failure mode."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 8192 if hard == resource.RLIM_INFINITY else min(hard, 65536)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            print(f"[startup] open-file limit {soft} → {want}")
    except Exception as exc:
        print(f"[startup] could not raise the open-file limit ({exc}); "
              f"if you see 'Too many open files', run:  ulimit -n 8192")


def main() -> int:
    import webbrowser
    _raise_fd_limit()
    if os.environ.get("LIAS_KEEP_ENGINE") != "1":
        if _do_stop_engine():
            print("[startup] stopped a stale engine — a fresh one will load latest code")
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _server_ref["server"] = server
    url = f"http://localhost:{PORT}"
    if HOST == "0.0.0.0":
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            lan_ip = probe.getsockname()[0]
            probe.close()
            print(f"  Phone: http://{lan_ip}:{PORT}  (same Wi-Fi / אותה רשת)")
        except OSError:
            pass
    db_state = "lias.db ✓ (read-only)" if os.path.exists(DB_PATH) else "no DB — demo data"
    print("─" * 52)
    print("  LIAS — New Dashboard (demo) / דשבורד חדש")
    print(f"  UI:    {url}")
    print(f"  Data:  {db_state}")
    print("  Note:  engine runs IN-PROCESS — port 8400 is disabled")
    print("  Stop:  Ctrl+C")
    print("─" * 52)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_watchdog,
                     args=(_shutting_down_flag,
                           _heartbeat_holder, _do_shutdown_all),
                     daemon=True).start()
    if os.environ.get("LIAS_AUTORELOAD", "1") != "0":
        threading.Thread(target=_autoreload_watcher,
                         args=(_shutting_down_flag,),
                         daemon=True).start()
    import atexit, signal as _signal
    atexit.register(lambda: _do_stop_engine())

    def _sig_exit(signum, frame):
        _do_shutdown_all(f"signal {signum}")
        sys.exit(0)

    for _sig in (_signal.SIGTERM, _signal.SIGHUP):
        try:
            _signal.signal(_sig, _sig_exit)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _do_shutdown_all("Ctrl+C")
    print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
