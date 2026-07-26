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
FULL_UI_PORT = 8400
KEYRING_SERVICE = "gov-il-connect"
NOTES_PATH = os.path.join(HERE, "annotations.json")
TRANSCRIPTIONS_DIR = os.path.join(HERE, "transcriptions")
os.makedirs(TRANSCRIPTIONS_DIR, exist_ok=True)

# ── shared mutable state (passed to modules as holder dicts) ────────────────
_engine_proc_holder: dict = {"proc": None}
_server_ref: dict = {"server": None}
_shutting_down_flag: dict = {"value": False}
_heartbeat_holder: dict = {"value": 0.0}


# ── convenience wrappers that bind globals to module functions ──────────────
def _do_connect():
    return _connect(DB_PATH)

def _do_full_ui_alive():
    return engine_inproc.alive()

def _do_build_dashboard():
    return build_dashboard(DB_PATH, FULL_UI_PORT)

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
def _govil_status() -> dict:
    try:
        import keyring
        has_id = bool(keyring.get_password(KEYRING_SERVICE, "id_number")
                      or keyring.get_password(KEYRING_SERVICE, "id"))
        has_pw = bool(keyring.get_password(KEYRING_SERVICE, "password"))
        return {"ok": True, "configured": has_id and has_pw}
    except Exception as exc:
        return {"ok": False, "configured": False, "error": str(exc)}


def _govil_save(payload: dict) -> dict:
    try:
        import keyring
        gid = (payload.get("id") or "").strip()
        pw = payload.get("password") or ""
        if not gid or not pw:
            return {"ok": False, "error": "missing id/password"}
        keyring.set_password(KEYRING_SERVICE, "id_number", gid)
        keyring.set_password(KEYRING_SERVICE, "password", pw)
        try:
            keyring.delete_password(KEYRING_SERVICE, "id")
        except Exception:
            pass
        return {"ok": True, "configured": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── email OTP account (reads the gov.il one-time code from your inbox) ──────
def _email_status() -> dict:
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
            import keyring
            has_pw = bool(keyring.get_password("gov-il-connect-email", address))
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
            import keyring
            keyring.set_password("gov-il-connect-email", address, app_pw)
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
            self._json(case_view(int(m_case.group(1)), params, DB_PATH))
        elif m_client:
            self._json(client_view(int(m_client.group(1)), DB_PATH))
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
        elif path == "/api/proxy/bdr/cases":
            code, body, _ct = engine_inproc.request("GET", "/api/bdr/cases")
            self._send(code, body, "application/json; charset=utf-8")
        elif path.startswith("/api/doc_pdf/") or path in (
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
        elif path == "/api/docs":
            self._json(docs_list(params, DB_PATH))
        elif path == "/api/search":
            self._json(search_all(params.get("q", ""), DB_PATH))
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
        if path == "/api/govil/save":
            self._json(_govil_save(payload))
        elif path == "/api/email/save":
            self._json(_email_save(payload))
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


def main() -> int:
    import webbrowser
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
                     args=(FULL_UI_PORT, _shutting_down_flag,
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
