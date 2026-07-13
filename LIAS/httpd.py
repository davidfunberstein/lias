"""Zero-dependency HTTP server — the UI works with nothing installed."""
from __future__ import annotations

import json
import mimetypes
import queue as _queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, jobs, queries

_ROUTES: list[tuple[str, re.Pattern, callable]] = []


def route(method: str, pattern: str):
    def deco(fn):
        _ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


# --- API routes ---------------------------------------------------------------

@route("GET", r"/api/clients")
def _clients(m, q):
    return queries.clients()


@route("GET", r"/api/clients/(\d+)/tree")
def _tree(m, q):
    return queries.client_tree(int(m.group(1)))


@route("GET", r"/api/sub_cases/(\d+)/documents")
def _docs(m, q):
    return queries.documents(
        int(m.group(1)),
        status=q.get("status", [""])[0],
        doc_type=q.get("doc_type", [""])[0],
        q=q.get("q", [""])[0],
        sort=q.get("sort", ["submission_date"])[0],
        order=q.get("order", ["desc"])[0],
    )


@route("GET", r"/api/sub_cases/(\d+)/whats_new")
def _new(m, q):
    return queries.whats_new(int(m.group(1)))


@route("GET", r"/api/jobs")
def _jobs(m, q):
    return queries.job_list(int(q.get("limit", ["30"])[0]))


@route("GET", r"/api/sync_runs")
def _runs(m, q):
    return queries.sync_runs(int(q.get("limit", ["30"])[0]))


@route("GET", r"/api/log")
def _log(m, q):
    lines = int(q.get("lines", ["120"])[0])
    log_path = config.COURT_DOCS_DIR / "logs" / "latest.log"
    if not log_path.exists():
        return {"lines": [], "path": str(log_path)}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return {"lines": text.splitlines()[-lines:], "path": str(log_path)}


@route("POST", r"/api/actions/open_portal/(NET|BDR)")
def _open(m, q):
    return {"job_id": jobs.submit("open_portal", {"portal": m.group(1)})}


@route("POST", r"/api/actions/net_scan/(\d+)")
def _scan(m, q):
    return {"job_id": jobs.submit("net_scan", {"sub_case_id": int(m.group(1))})}


@route("POST", r"/api/actions/sync_current/(NET|BDR)")
def _sync(m, q):
    kind = "net_sync_current" if m.group(1) == "NET" else "bdr_sync_current"
    return {"job_id": jobs.submit(kind)}


@route("POST", r"/api/actions/reimport")
def _reimport(m, q):
    return {"job_id": jobs.submit("reimport_csv")}


@route("POST", r"/api/actions/net_auto_update")
def _net_auto_update(m, q):
    return {"job_id": jobs.submit("net_auto_update")}


@route("POST", r"/api/actions/bdr_batch")
def _bdr_batch(m, q):
    return {"job_id": jobs.submit("bdr_batch")}


@route("POST", r"/api/actions/net_date_search")
def _net_date_search(m, q):
    years_back = int(q.get("years_back", ["10"])[0])
    return {"job_id": jobs.submit("net_date_search", {"years_back": years_back})}


@route("POST", r"/api/actions/convert_md/(\d+)")
def _convert_md(m, q):
    return {"job_id": jobs.submit("convert_md", {"document_id": int(m.group(1))})}



@route("GET", r"/api/settings")
def _get_settings(m, q):
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
    }

# --- AI endpoint --------------------------------------------------------------

def _ai_ask_handler(body: dict) -> dict:
    """Call Gemini with case context and return answer."""
    question = body.get("question", "").strip()
    sub_case_id = body.get("sub_case_id")
    api_key = body.get("api_key", "").strip()

    if not question:
        return {"error": "No question provided"}
    if not api_key:
        import sys
        project_root = config.PROJECT_ROOT
        sys.path.insert(0, str(project_root))
        try:
            _defaults_path = project_root / "court_documents" / "session_defaults.json"
            if _defaults_path.exists():
                _defaults = json.loads(_defaults_path.read_text(encoding="utf-8"))
                api_key = _defaults.get("gemini_api_key", "")
        except Exception:
            pass
    if not api_key:
        return {"error": "Gemini API key not set — configure in Settings > 10 in the main app"}

    try:
        import google.generativeai as genai
    except ImportError:
        return {"error": "google-generativeai not installed: pip install google-generativeai"}

    ctx = queries.ai_context(sub_case_id) if sub_case_id else {}
    sub = ctx.get("sub", {})
    doc_list = ctx.get("doc_list", [])
    txt_content = ctx.get("txt_content", "")

    if sub:
        case_summary = (
            f"Client: {sub.get('client_name')}\n"
            f"Case: {sub.get('case_number')} — {sub.get('title', '')}\n"
            f"Sub-case: {sub.get('sub_number')} | Portal: {sub.get('portal')} | Court: {sub.get('court')}\n"
            f"Total documents: {ctx.get('doc_count', 0)}\n\n"
            "Document list (newest first):\n"
        )
        for d in doc_list[:80]:
            case_summary += (
                f"  • {d.get('submission_date', '')}  [{d.get('doc_type', '')}]  "
                f"{d.get('logical_name') or d.get('physical_name', '')}  "
                f"({d.get('download_status', '')})\n"
            )
    else:
        case_summary = "(No case selected — answering from general knowledge)"

    prompt = (
        "You are a legal assistant for an Israeli lawyer. "
        "Answer the question based on the case documents below. "
        "Be concise, precise, and cite specific documents by date and type when relevant. "
        "Answer in the same language as the question (Hebrew or English).\n\n"
        f"=== CASE CONTEXT ===\n{case_summary}\n"
        + (f"\n=== DOCUMENT TEXT ===\n{txt_content[:8000]}\n" if txt_content else "")
        + f"\n=== QUESTION ===\n{question}"
    )

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash-lite")
        resp = model.generate_content(prompt)
        return {"answer": resp.text.strip(), "docs_used": len(doc_list), "txt_chars": len(txt_content)}
    except Exception as e:
        return {"error": f"Gemini error: {e}"}


# --- Request handler ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path, q = parsed.path, parse_qs(parsed.query)

        if method == "GET" and path == "/":
            html = (config.UI_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if method == "GET" and path == "/events":
            self._sse()
            return

        # Serve document file (PDF etc.)
        doc_m = re.match(r"^/api/doc/(\d+)$", path)
        if method == "GET" and doc_m:
            self._serve_doc(int(doc_m.group(1)))
            return

        # Settings save
        if method == "POST" and path == "/api/settings":
            try:
                import json as _j
                length = int(self.headers.get("Content-Length", 0))
                body = _j.loads(self.rfile.read(length) if length else b"{}")
                court_docs_dir = body.get("court_docs_dir", "")
                defaults_path = config.PROJECT_ROOT / "session_defaults.json"
                d = {}
                if defaults_path.exists():
                    try:
                        d = _j.loads(defaults_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                if court_docs_dir:
                    d["court_docs_dir"] = court_docs_dir
                elif "court_docs_dir" in d:
                    del d["court_docs_dir"]
                defaults_path.write_text(_j.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                from pathlib import Path as _P
                config.COURT_DOCS_DIR = _P(court_docs_dir).expanduser().resolve() if court_docs_dir else (config.PROJECT_ROOT / "court_documents")
                self._json({"ok": True, "court_docs_dir_effective": str(config.COURT_DOCS_DIR)})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        # AI endpoint needs the JSON body
        if method == "POST" and path == "/api/ai/ask":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) if length else b"{}")
                self._json(_ai_ask_handler(body))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        for m_, rx, fn in _ROUTES:
            match = rx.match(path)
            if m_ == method and match:
                try:
                    self._json(fn(match, q))
                except Exception as e:
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
        self._json({"error": "not found"}, 404)

    def _serve_doc(self, document_id: int) -> None:
        from . import db
        row = db.get_conn().execute(
            "SELECT local_path, logical_name, physical_name FROM documents WHERE document_id=?",
            (document_id,)
        ).fetchone()
        if not row or not row["local_path"]:
            self._json({"error": "no local file recorded"}, 404)
            return
        file_path = config.COURT_DOCS_DIR / row["local_path"]
        if not file_path.exists():
            self._json({"error": f"file not on disk: {file_path}"}, 404)
            return
        data = file_path.read_bytes()
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        fname = row["logical_name"] or row["physical_name"]
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{fname}"')
        self.end_headers()
        self.wfile.write(data)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        sub = jobs.subscribe()
        try:
            while True:
                try:
                    ev = sub.get(timeout=15)
                    data = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except _queue.Empty:
                    data = ": keepalive\n\n"
                self.wfile.write(data.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            jobs.unsubscribe(sub)

    def do_GET(self):   # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")


def serve(host: str = config.API_HOST, port: int = config.API_PORT) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"UI (stdlib server): http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
