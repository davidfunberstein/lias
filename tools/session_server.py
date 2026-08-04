#!/usr/bin/env python3
"""
session_server.py — Remote login proxy for session export.

Two modes:
  Local mode (no --callback):
    python session_server.py [net|bdr|eca]
    Opens Playwright browser locally + web UI. User clicks "Done" to export.

  Remote mode (--callback URL):
    python session_server.py net --callback https://xxx.ngrok-free.app/api/profiles/receive_cookies
    Serves a credential form. Remote user enters ID+password+OTP via their
    phone/browser. Playwright on THIS machine logs in on their behalf.
    Cookies are POSTed automatically to the callback URL when done.
"""
import sys, json, time, os, threading, subprocess, urllib.request, webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── auto-install ──────────────────────────────────────────────────────────────
def _ensure(pkg):
    try: __import__(pkg.replace("-","_").split("[")[0])
    except ImportError:
        print(f"  מתקין {pkg}…")
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=True)

_ensure("playwright")
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
    from playwright.sync_api import sync_playwright

# ── config ────────────────────────────────────────────────────────────────────
PORT = 7777
PORTALS = {
    "bdr": {"url": "https://sides.rbc.gov.il/Pages/FilesList.aspx",              "label": "בית הדין הרבני"},
    "net": {"url": "https://www.court.gov.il/ngcs.web.site/homepage.aspx",        "label": "נט המשפט"},
    "eca": {"url": "https://publicsso.eca.gov.il/he/home/OpenCase",               "label": "הוצאה לפועל"},
}

portal_key   = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
if portal_key == "auto":
    PORTALS["auto"] = {"url": "https://iam.gov.il/", "label": "gov.il (כל הפורטלים)"}
elif portal_key not in PORTALS:
    print(f"פורטל לא מוכר: {portal_key}  —  בחר: auto / bdr / net / eca"); sys.exit(2)

email_to     = ""
callback_url = ""
for i, a in enumerate(sys.argv):
    if a in ("--email",    "-e") and i+1 < len(sys.argv): email_to     = sys.argv[i+1]
    if a in ("--callback", "-c") and i+1 < len(sys.argv): callback_url = sys.argv[i+1]

P   = PORTALS[portal_key]
OUT = Path(f"session_{portal_key}.json")

# ── shared state ──────────────────────────────────────────────────────────────
_state = {
    "ready": False, "payload": None, "error": "",
    "sent_ok": False,
    # remote-login flow
    "step": "idle",   # idle | waiting_creds | logging_in | waiting_otp | done | failed
    "otp": None,
}
_creds_evt = threading.Event()   # fired when user submits credentials
_otp_evt   = threading.Event()   # fired when user submits OTP
_done_evt  = threading.Event()   # fired when local user clicks "Done" (local mode)
_pw_ready  = threading.Event()

# ── Playwright helpers ────────────────────────────────────────────────────────
def _export_cookies(ctx):
    state = ctx.storage_state()
    payload = {
        "portal": portal_key, "url": P["url"],
        "exported_at": time.time(),
        "exported_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "storage_state": state,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _state["payload"] = payload
    # Auto-send to callback
    if callback_url:
        try:
            body = json.dumps(payload).encode()
            req  = urllib.request.Request(callback_url, data=body,
                       headers={"Content-Type":"application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
            _state["sent_ok"] = True
            print(f"  ✓ עוגיות נשלחו אוטומטית → {callback_url}")
        except Exception as ce:
            _state["sent_ok"] = False
            print(f"  ✗ שליחה אוטומטית נכשלה: {ce}")
    return payload

# ── Browser thread — LOCAL mode (no callback) ─────────────────────────────────
def _browser_local():
    try:
        with sync_playwright() as pw:
            brow = pw.chromium.launch(headless=False,
                args=["--disable-blink-features=AutomationControlled","--start-maximized"])
            ctx  = brow.new_context(no_viewport=True)
            page = ctx.new_page()
            page.goto(P["url"], wait_until="domcontentloaded", timeout=60_000)
            _pw_ready.set()
            _done_evt.wait()
            _export_cookies(ctx)
            _state["ready"] = True
            brow.close()
    except Exception as e:
        _state["error"] = str(e); _state["ready"] = True; _done_evt.set()

# ── Browser thread — REMOTE mode (with callback) ──────────────────────────────
# NET portal login selectors (gov.il unified login)
_NET_ID_SEL    = 'input[name="IDNumber"], input[id*="id"], input[placeholder*="ז"]'
_NET_PASS_SEL  = 'input[type="password"]'
_NET_SUBMIT    = 'button[type="submit"], input[type="submit"]'
_NET_OTP_SEL   = 'input[name*="otp"], input[name*="code"], input[placeholder*="קוד"], input[placeholder*="OTP"]'

def _browser_remote():
    _state["step"] = "waiting_creds"
    try:
        # Use a persistent profile so the portal doesn't flag the browser as bot
        profile_dir = str(Path(os.path.expanduser("~")) / ".lias_invite_profile")
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                args=["--disable-blink-features=AutomationControlled",
                      "--start-maximized", "--no-first-run"],
                ignore_https_errors=True,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Navigate to portal immediately so it loads naturally
            try:
                page.goto(P["url"], wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                page.goto(P["url"], wait_until="commit", timeout=30_000)
            _pw_ready.set()

            # Wait for credentials from the web form
            _creds_evt.wait(timeout=300)
            creds = _state.get("creds", {})
            id_num   = creds.get("id","").strip()
            password = creds.get("password","").strip()
            if not id_num or not password:
                raise ValueError("פרטים חסרים")

            _state["step"] = "logging_in"
            page.wait_for_timeout(1000)

            # Fill credentials
            try:
                page.locator(_NET_ID_SEL).first.fill(id_num)
                page.locator(_NET_PASS_SEL).first.fill(password)
                page.locator(_NET_SUBMIT).first.click()
                page.wait_for_timeout(3000)
            except Exception:
                # Fallback: just navigate and let user see result
                pass

            # Check if OTP is needed
            try:
                otp_el = page.locator(_NET_OTP_SEL).first
                otp_el.wait_for(timeout=5000)
                _state["step"] = "waiting_otp"
                _otp_evt.wait(timeout=180)
                otp_code = (_state.get("otp") or "").strip()
                if otp_code:
                    otp_el.fill(otp_code)
                    page.locator(_NET_SUBMIT).first.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass  # no OTP needed

            # Export
            _export_cookies(ctx)
            _state["step"] = "done"
            _state["ready"] = True
            try: ctx.close()
            except Exception: pass
    except Exception as e:
        _state["error"] = str(e)
        _state["step"]  = "failed"
        _state["ready"] = True


# ── HTML pages ────────────────────────────────────────────────────────────────
_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#0d1117;color:#c9d1d9;display:flex;flex-direction:column;
  align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;text-align:center}
h1{font-size:22px;margin-bottom:6px}
.sub{color:#6e7681;margin-bottom:24px;font-size:14px}
.card{background:#161b22;border:1px solid #21262d;border-radius:14px;
  padding:22px 26px;max-width:400px;width:100%;text-align:right}
label{display:block;font-size:12px;color:#8b949e;margin-bottom:4px;margin-top:14px}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;
  padding:10px 12px;border-radius:8px;border:1px solid #30363d;
  background:#0d1117;color:#c9d1d9;font-size:15px;direction:ltr}
input:focus{outline:none;border-color:#58a6ff}
.btn{display:block;width:100%;margin-top:18px;padding:12px;border-radius:8px;
  border:none;background:#238636;color:#fff;font-size:16px;font-weight:700;
  cursor:pointer;font-family:inherit;transition:background .15s}
.btn:hover{background:#2ea043}
.btn:disabled{background:#21262d;color:#555;cursor:default}
.warn{font-size:11px;color:#6e7681;margin-top:12px;line-height:1.6}
#msg{font-size:13px;margin-top:12px;min-height:18px;text-align:center}
.ok{color:#3fb950}.err{color:#f85149}.spin{color:#58a6ff}
"""

def _page_creds():
    return f"""<!DOCTYPE html><html dir="rtl" lang="he">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>כניסה — {P['label']}</title><style>{_CSS}</style></head><body>
<h1>🔐 כניסה ל{P['label']}</h1>
<p class="sub">הזן את פרטי הכניסה שלך</p>
<div class="card">
  <label>מספר זהות</label>
  <input type="text" id="id" placeholder="012345678" inputmode="numeric" autocomplete="off">
  <label>סיסמה</label>
  <input type="password" id="pw" placeholder="••••••••" autocomplete="current-password">
  <button class="btn" id="btn" onclick="submit()">כנס →</button>
  <div id="msg"></div>
  <p class="warn">🔒 הפרטים נשלחים בצורה מוצפנת ישירות לעורך הדין שלך ולא נשמרים.</p>
</div>
<script>
async function submit(){{
  const id=document.getElementById('id').value.trim();
  const pw=document.getElementById('pw').value.trim();
  if(!id||!pw){{document.getElementById('msg').innerHTML='<span class="err">מלא את כל השדות</span>';return;}}
  document.getElementById('btn').disabled=true;
  document.getElementById('msg').innerHTML='<span class="spin">מתחבר…</span>';
  const r=await fetch('/creds',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{id,password:pw}})}});
  const j=await r.json();
  if(j.otp_needed){{location.href='/otp';}}
  else if(j.ok){{location.href='/done_remote';}}
  else{{document.getElementById('msg').innerHTML='<span class="err">'+j.error+'</span>';
    document.getElementById('btn').disabled=false;}}
}}
</script></body></html>"""

def _page_otp():
    return f"""<!DOCTYPE html><html dir="rtl" lang="he">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>קוד OTP — {P['label']}</title><style>{_CSS}</style></head><body>
<h1>📱 קוד אימות</h1>
<p class="sub">בדוק SMS / אימייל</p>
<div class="card">
  <label>קוד OTP שקיבלת</label>
  <input type="text" id="otp" placeholder="123456" inputmode="numeric" autocomplete="one-time-code">
  <button class="btn" id="btn" onclick="submit()">אמת →</button>
  <div id="msg"></div>
</div>
<script>
document.getElementById('otp').focus();
async function submit(){{
  const code=document.getElementById('otp').value.trim();
  if(!code){{document.getElementById('msg').innerHTML='<span class="err">הזן קוד</span>';return;}}
  document.getElementById('btn').disabled=true;
  document.getElementById('msg').innerHTML='<span class="spin">מאמת…</span>';
  const r=await fetch('/otp_submit',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{code}})}});
  const j=await r.json();
  if(j.ok){{location.href='/done_remote';}}
  else{{document.getElementById('msg').innerHTML='<span class="err">'+j.error+'</span>';
    document.getElementById('btn').disabled=false;}}
}}
</script></body></html>"""

def _page_done_remote(sent_ok):
    msg = ("✅ הכניסה הצליחה! העוגיות נשלחו אוטומטית לעורך הדין. אפשר לסגור את הדף."
           if sent_ok else
           "✅ הכניסה הצליחה! אך השליחה האוטומטית נכשלה — דווח לעורך הדין.")
    color = "#3fb950" if sent_ok else "#d29922"
    return f"""<!DOCTYPE html><html dir="rtl" lang="he">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>סיום — {P['label']}</title><style>{_CSS}</style></head><body>
<h1 style="color:{color}">{msg}</h1>
</body></html>"""

# ── Local mode pages (unchanged) ──────────────────────────────────────────────
_PAGE_WAITING = """<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ייצוא סשן — LIAS</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#0d1117;color:#c9d1d9;display:flex;flex-direction:column;
    align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;text-align:center}
  h1{font-size:24px;margin-bottom:8px}
  .sub{color:#6e7681;margin-bottom:32px;font-size:15px}
  .step{background:#161b22;border:1px solid #21262d;border-radius:12px;
    padding:20px 28px;max-width:420px;margin-bottom:20px;line-height:1.8}
  .step b{color:#58a6ff}
  button{background:#238636;border:none;border-radius:8px;color:#fff;
    font-size:17px;padding:14px 40px;cursor:pointer;margin-top:8px;
    font-family:inherit;transition:background .15s}
  button:hover{background:#2ea043}
  button:disabled{background:#21262d;color:#444;cursor:default}
  #status{font-size:13px;color:#6e7681;margin-top:12px;min-height:20px}
</style></head><body>
<h1>🔐 ייצוא סשן — __LABEL__</h1>
<p class="sub">התחבר לפורטל בדפדפן שנפתח, ולחץ "סיימתי"</p>
<div class="step">
  <b>שלב 1</b> — בדפדפן שנפתח, התחבר עם שם המשתמש, סיסמה וקוד OTP אם נדרש.<br>
  <b>שלב 2</b> — וודא שאתה רואה את <b>רשימת התיקים שלך</b>.<br>
  <b>שלב 3</b> — חזור לכאן ולחץ:
</div>
<button id="btn" onclick="done()">✅ סיימתי — ייצא עוגיות</button>
<div id="status"></div>
<script>
async function done(){
  document.getElementById('btn').disabled=true;
  document.getElementById('status').textContent='מייצא…';
  const r=await fetch('/done',{method:'POST'});
  const j=await r.json();
  if(j.ok){
    document.getElementById('status').innerHTML=
      '<span style="color:#3fb950">✓ יוצא — '+j.cookies+' עוגיות</span>';
    setTimeout(()=>location.href='/result',800);
  }else{
    document.getElementById('status').innerHTML='<span style="color:#f85149">'+j.error+'</span>';
    document.getElementById('btn').disabled=false;
  }
}
</script></body></html>"""

_PAGE_RESULT = """<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>סשן יוצא — LIAS</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#0d1117;color:#c9d1d9;display:flex;flex-direction:column;
    align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;text-align:center}
  h1{font-size:24px;margin-bottom:8px;color:#3fb950}
  .sub{color:#6e7681;margin-bottom:32px}
  .box{background:#161b22;border:1px solid #21262d;border-radius:12px;
    padding:20px 28px;max-width:440px;line-height:1.8;margin-bottom:16px}
  .info{font-family:monospace;font-size:13px;color:#58a6ff;margin-top:4px}
  a.btn,button{display:inline-block;border:none;border-radius:8px;
    font-size:15px;padding:12px 28px;cursor:pointer;font-family:inherit;
    text-decoration:none;margin:6px;transition:background .15s}
  a.dl{background:#1f6feb;color:#fff} a.dl:hover{background:#388bfd}
  button.mail{background:#21262d;color:#c9d1d9;border:1px solid #30363d}
  #mail-form{display:none;margin-top:12px}
  #mail-form input{padding:8px 12px;border-radius:6px;border:1px solid #30363d;
    background:#0d1117;color:#c9d1d9;font-size:14px;width:240px;direction:ltr}
  #mail-form button{background:#238636;color:#fff}
  #mail-status{font-size:13px;margin-top:8px;min-height:18px}
  .warn{font-size:12px;color:#d29922;margin-top:16px}
</style></head><body>
<h1>✅ הסשן יוצא בהצלחה!</h1>
<p class="sub">__LABEL__ — __COOKIES__ עוגיות</p>
<div class="box">
  __AUTO_SENT_MSG__
  <a class="btn dl" href="/download">⬇ הורד קובץ (גיבוי)</a>
  <button class="mail" onclick="document.getElementById('mail-form').style.display='block'">📧 שלח למייל</button>
  <div id="mail-form">
    <input id="email-inp" type="email" placeholder="david@example.com" value="__EMAIL__">
    <button onclick="sendMail()">שלח</button>
    <div id="mail-status"></div>
  </div>
</div>
<p class="warn">⚠ הקובץ תקף לשעה-שעתיים בלבד — שלח בהקדם.</p>
<script>
async function sendMail(){
  const e=document.getElementById('email-inp').value.trim();
  if(!e){document.getElementById('mail-status').textContent='הזן כתובת מייל';return}
  document.getElementById('mail-status').textContent='שולח…';
  const r=await fetch('/send_email',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({email:e})});
  const j=await r.json();
  document.getElementById('mail-status').innerHTML=
    j.ok?'<span style="color:#3fb950">✓ נשלח!</span>':'<span style="color:#f85149">'+j.error+'</span>';
}
</script></body></html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ct="text/html; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("ngrok-skip-browser-warning", "1")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        step = _state["step"]
        if callback_url:
            # REMOTE mode routing
            if self.path in ("/", "/index.html"):
                self._send(200, _page_creds())
            elif self.path == "/otp":
                self._send(200, _page_otp())
            elif self.path == "/done_remote":
                self._send(200, _page_done_remote(_state.get("sent_ok", False)))
            elif self.path == "/download":
                if not OUT.exists():
                    self._send(404, "קובץ לא נמצא"); return
                data = OUT.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", f'attachment; filename="{OUT.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
            else:
                self._send(404, "לא נמצא")
        else:
            # LOCAL mode routing
            if self.path in ("/", "/index.html"):
                self._send(200, _PAGE_WAITING.replace("__LABEL__", P["label"]))
            elif self.path == "/result":
                if not _state["ready"]:
                    self.send_response(302); self.send_header("Location", "/"); self.end_headers(); return
                n = len((_state["payload"] or {}).get("storage_state",{}).get("cookies",[]))
                if _state.get("sent_ok"):
                    auto_msg = '<b style="color:#3fb950">✅ העוגיות נשלחו אוטומטית לעורך הדין!</b>'
                elif callback_url:
                    auto_msg = '<b style="color:#f85149">⚠ השליחה נכשלה — הורד ידנית.</b>'
                else:
                    auto_msg = '<b>שלח את הקובץ לעורך הדין:</b>'
                html = (_PAGE_RESULT
                        .replace("__LABEL__", P["label"])
                        .replace("__COOKIES__", str(n))
                        .replace("__EMAIL__", email_to)
                        .replace("__AUTO_SENT_MSG__", auto_msg))
                self._send(200, html)
            elif self.path == "/download":
                if not OUT.exists(): self._send(404, "קובץ לא נמצא"); return
                data = OUT.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", f'attachment; filename="{OUT.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
            else:
                self._send(404, "לא נמצא")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if self.path == "/creds":
            # Remote mode: receive credentials, start Playwright login
            try:
                data = json.loads(body)
                _state["creds"] = data
                _creds_evt.set()
                # Wait for Playwright to process (up to 20s)
                for _ in range(40):
                    if _state["step"] in ("waiting_otp","done","failed"): break
                    time.sleep(0.5)
                step = _state["step"]
                if step == "waiting_otp":
                    self._send(200, json.dumps({"ok": False, "otp_needed": True}), "application/json")
                elif step == "done":
                    self._send(200, json.dumps({"ok": True}), "application/json")
                elif step == "failed":
                    self._send(200, json.dumps({"ok": False, "error": _state["error"]}), "application/json")
                else:
                    self._send(200, json.dumps({"ok": False, "error": "תהליך הכניסה עדיין רץ — נסה שוב"}), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")

        elif self.path == "/otp_submit":
            try:
                data = json.loads(body)
                _state["otp"] = data.get("code","")
                _otp_evt.set()
                for _ in range(30):
                    if _state["step"] in ("done","failed"): break
                    time.sleep(0.5)
                if _state["step"] == "done":
                    self._send(200, json.dumps({"ok": True}), "application/json")
                else:
                    self._send(200, json.dumps({"ok": False, "error": _state.get("error","נכשל")}), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")

        elif self.path == "/done":
            # Local mode
            _done_evt.set()
            for _ in range(30):
                if _state["ready"]: break
                time.sleep(0.5)
            if _state.get("error"):
                self._send(200, json.dumps({"ok": False, "error": _state["error"]}), "application/json")
            else:
                n = len((_state["payload"] or {}).get("storage_state",{}).get("cookies",[]))
                self._send(200, json.dumps({"ok": True, "cookies": n}), "application/json")

        elif self.path == "/send_email":
            if not OUT.exists():
                self._send(200, json.dumps({"ok":False,"error":"קובץ לא קיים"}), "application/json"); return
            try:
                data = json.loads(body)
                _send_email(data.get("email","").strip(), OUT)
                self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok":False,"error":str(e)}), "application/json")
        else:
            self._send(404, "לא נמצא")


def _send_email(to: str, file: Path):
    import smtplib, ssl
    from email.message import EmailMessage
    smtp_user = os.environ.get("SMTP_USER","")
    smtp_pass = os.environ.get("SMTP_PASS","")
    smtp_host = os.environ.get("SMTP_HOST","smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT","587"))
    if not smtp_user or not smtp_pass:
        raise RuntimeError("הגדר SMTP_USER ו-SMTP_PASS לשליחת מייל")
    msg = EmailMessage()
    msg["Subject"] = f"סשן {portal_key.upper()} — LIAS"
    msg["From"]    = smtp_user
    msg["To"]      = to
    msg.set_content(f"מצורף קובץ הסשן לפורטל {P['label']}.\nהקובץ תקף לשעה-שעתיים.")
    msg.add_attachment(file.read_bytes(), maintype="application",
                       subtype="json", filename=file.name)
    ctx = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.ehlo(); s.starttls(context=ctx); s.login(smtp_user, smtp_pass)
        s.send_message(msg)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print("═" * 58)
    print(f"  ייצוא סשן — {P['label']}")
    if callback_url:
        print(f"  מצב: Remote (callback → {callback_url})")
    print("═" * 58)

    if callback_url:
        # Remote mode: start headless Playwright thread, serve credential form
        t = threading.Thread(target=_browser_remote, daemon=True)
        t.start()
        _pw_ready.wait(timeout=15)
        print(f"\n  שרת מוכן: http://localhost:{PORT}")
        print(f"  (ממתין לכניסה של הלקוח…)\n")
    else:
        # Local mode: open visible browser, open web UI
        t = threading.Thread(target=_browser_local, daemon=True)
        t.start()
        _pw_ready.wait(timeout=30)
        url = f"http://localhost:{PORT}"
        print(f"\n  פתח: {url}\n")
        webbrowser.open(url)

    srv = HTTPServer(("0.0.0.0", PORT), H)
    try:
        while not _state["ready"]:
            srv.handle_request()
        for _ in range(10):
            srv.handle_request()
    except KeyboardInterrupt:
        pass

    if _state.get("error"):
        print(f"\n  ✗ שגיאה: {_state['error']}")
        return 1
    n = len((_state["payload"] or {}).get("storage_state",{}).get("cookies",[]))
    print(f"\n  ✓ {OUT}  ({n} עוגיות)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
