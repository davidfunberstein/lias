#!/usr/bin/env python3
"""
session_server.py — שרת מקומי לייצוא סשן.

שולחים לחבר/לקוח קובץ אחד בלבד.
הוא מריץ:   python session_server.py bdr
דפדפן נפתח לפורטל, ועמוד web ב-http://localhost:7777
עם כפתור "סיימתי" שמייצא את העוגיות ומאפשר הורדה / שליחה למייל.

הרץ:  python session_server.py [bdr|net|eca]  [--email כתובת]
"""
import sys, json, time, os, threading, subprocess, urllib.request, webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── auto-install ──────────────────────────────────────────────────────────────
def _ensure(pkg):
    try:
        __import__(pkg.replace("-","_").split("[")[0])
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
    "bdr": {"url": "https://sides.rbc.gov.il/Pages/FilesList.aspx",  "label": "בית הדין הרבני"},
    "net": {"url": "https://www.court.gov.il/ngcs.web.site/homepage.aspx", "label": "נט המשפט"},
    "eca": {"url": "https://publicsso.eca.gov.il/he/home/OpenCase",   "label": "הוצאה לפועל"},
}

portal_key = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
if portal_key == "auto":
    # Generic gov.il login — works for all portals
    PORTALS["auto"] = {
        "url":   "https://www.gov.il/he",
        "label": "gov.il (כל הפורטלים)",
    }
elif portal_key not in PORTALS:
    print(f"פורטל לא מוכר: {portal_key}  —  בחר: auto / bdr / net / eca"); sys.exit(2)

email_to = ""
for i, a in enumerate(sys.argv):
    if a in ("--email", "-e") and i+1 < len(sys.argv):
        email_to = sys.argv[i+1]

P       = PORTALS[portal_key]
OUT     = Path(f"session_{portal_key}.json")
_state  = {"ready": False, "payload": None, "error": ""}

# ── Playwright thread ─────────────────────────────────────────────────────────
_pw_ready = threading.Event()
_done_evt = threading.Event()

def _browser_thread():
    try:
        with sync_playwright() as pw:
            brow = pw.chromium.launch(headless=False,
                args=["--disable-blink-features=AutomationControlled","--start-maximized"])
            ctx  = brow.new_context(no_viewport=True)
            page = ctx.new_page()
            page.goto(P["url"], wait_until="domcontentloaded", timeout=60_000)
            _pw_ready.set()
            _done_evt.wait()          # block until user clicks "Done" in the web UI
            state = ctx.storage_state()
            _state["payload"] = {
                "portal": portal_key,
                "url": P["url"],
                "exported_at":  time.time(),
                "exported_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "storage_state": state,
            }
            OUT.write_text(json.dumps(_state["payload"], ensure_ascii=False, indent=2), encoding="utf-8")
            _state["ready"] = True
            brow.close()
    except Exception as e:
        _state["error"] = str(e)
        _state["ready"] = True
        _done_evt.set()

# ── HTML ──────────────────────────────────────────────────────────────────────
_PAGE_WAITING = """<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ייצוא סשן — LIAS</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#0d1117;color:#c9d1d9;display:flex;flex-direction:column;
    align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;
    text-align:center}
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
  .spinner{width:40px;height:40px;border:3px solid #21262d;
    border-top-color:#58a6ff;border-radius:50%;animation:spin 1s linear infinite;margin:20px auto}
  @keyframes spin{to{transform:rotate(360deg)}}
  #status{font-size:13px;color:#6e7681;margin-top:12px;min-height:20px}
</style></head>
<body>
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
      '<span style="color:#3fb950">✓ יוצא בהצלחה — ' + j.cookies + ' עוגיות</span>';
    setTimeout(()=>location.href='/result',800);
  } else {
    document.getElementById('status').innerHTML=
      '<span style="color:#f85149">שגיאה: '+j.error+'</span>';
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
  a.dl{background:#1f6feb;color:#fff}
  a.dl:hover{background:#388bfd}
  button.mail{background:#21262d;color:#c9d1d9;border:1px solid #30363d}
  button.mail:hover{background:#30363d}
  #mail-form{display:none;margin-top:12px}
  #mail-form input{padding:8px 12px;border-radius:6px;border:1px solid #30363d;
    background:#0d1117;color:#c9d1d9;font-size:14px;width:240px;direction:ltr}
  #mail-form button{background:#238636;color:#fff}
  #mail-status{font-size:13px;margin-top:8px;min-height:18px}
  .warn{font-size:12px;color:#d29922;margin-top:16px}
</style></head>
<body>
<h1>✅ הסשן יוצא בהצלחה!</h1>
<p class="sub">__LABEL__ — __COOKIES__ עוגיות</p>
<div class="box">
  <b>שלח את הקובץ לעורך הדין:</b><br>
  <span class="info">__FILENAME__</span>
  <br><br>
  <a class="btn dl" href="/download">⬇ הורד קובץ</a>
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
    def log_message(self, *a): pass   # silence access log

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
        if self.path in ("/", "/index.html"):
            html = (_PAGE_WAITING
                    .replace("__LABEL__", P["label"]))
            self._send(200, html)
        elif self.path == "/result":
            if not _state["ready"]:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            n = len((_state["payload"] or {}).get("storage_state",{}).get("cookies",[]))
            html = (_PAGE_RESULT
                    .replace("__LABEL__", P["label"])
                    .replace("__COOKIES__", str(n))
                    .replace("__FILENAME__", str(OUT))
                    .replace("__EMAIL__", email_to))
            self._send(200, html)
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

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if self.path == "/done":
            _done_evt.set()
            # Wait up to 15 s for playwright to finish
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
                to   = data.get("email","").strip()
                if not to: raise ValueError("no email")
                _send_email(to, OUT)
                self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok":False,"error":str(e)}), "application/json")
        else:
            self._send(404, "לא נמצא")


def _send_email(to: str, file: Path):
    """Send the session JSON via smtplib using env-var credentials OR Gmail API."""
    import smtplib, ssl
    from email.message import EmailMessage
    smtp_user = os.environ.get("SMTP_USER","")
    smtp_pass = os.environ.get("SMTP_PASS","")
    smtp_host = os.environ.get("SMTP_HOST","smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT","587"))
    if not smtp_user or not smtp_pass:
        raise RuntimeError("הגדר SMTP_USER ו-SMTP_PASS בסביבה (env vars) לשליחת מייל")
    msg = EmailMessage()
    msg["Subject"] = f"סשן {portal_key.upper()} — LIAS"
    msg["From"]    = smtp_user
    msg["To"]      = to
    msg.set_content(
        f"מצורף קובץ הסשן לפורטל {PORTALS[portal_key]['label']}.\n"
        f"ייבא אותו ב-LIAS → הגדרות → הורדות → ייבוא סשן.\n"
        f"הקובץ תקף לשעה-שעתיים בלבד.")
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
    print("═" * 58)

    t = threading.Thread(target=_browser_thread, daemon=True)
    t.start()

    print("  פותח דפדפן ו-UI מקומי…")
    _pw_ready.wait(timeout=30)

    url = f"http://localhost:{PORT}"
    print(f"\n  פתח: {url}")
    print("  (נפתח אוטומטית בדפדפן שלך)\n")
    webbrowser.open(url)

    srv = HTTPServer(("0.0.0.0", PORT), H)
    try:
        while not (_state["ready"]):
            srv.handle_request()
        # serve a few more requests (result page, download)
        for _ in range(20):
            srv.handle_request()
    except KeyboardInterrupt:
        pass

    if _state.get("error"):
        print(f"\n  ✗ שגיאה: {_state['error']}")
        return 1
    n = len((_state["payload"] or {}).get("storage_state",{}).get("cookies",[]))
    print(f"\n  ✓ {OUT}  ({n} עוגיות)")
    print(f"  שלח לעורך הדין ← הוא מייבא ב-LIAS → הגדרות → ייבוא סשן")
    return 0

if __name__ == "__main__":
    sys.exit(main())
