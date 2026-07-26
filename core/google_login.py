"""Sign in to LIAS with a Google account ("Sign in with Google").

EN: The browser gets an ID token from Google Identity Services and posts it
    here. We verify it SERVER-SIDE against Google's tokeninfo endpoint (stdlib
    urllib — no extra packages), check it was issued for OUR client id, and
    check the email against an allow-list. Only then is the login accepted,
    and it is written to the login audit log like every other connection.

HE: הדפדפן מקבל מגוגל אסימון זהות ושולח אותו לכאן. אנחנו מאמתים אותו בצד
    השרת מול גוגל, בודקים שהוא הונפק עבור המזהה שלנו, ושכתובת המייל נמצאת
    ברשימת המורשים — ורק אז מאשרים כניסה. כל ניסיון נרשם ביומן ההתחברויות.

Setup (one time):
  1. https://console.cloud.google.com → create/choose a project
  2. APIs & Services → Credentials → Create credentials → OAuth client ID
  3. Application type: **Web application**, name: **LIAS**
  4. Authorized JavaScript origins:  http://localhost:8500
  5. Copy the Client ID and paste it in LIAS: Settings ⚙ → Google login.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

_CFG = Path(__file__).resolve().parent.parent / "google_login.json"
_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo?id_token="


def load_config() -> dict:
    try:
        return json.loads(_CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(client_id: str = "", allowed_emails=None) -> dict:
    cfg = load_config()
    if client_id is not None:
        cfg["client_id"] = (client_id or "").strip()
    if allowed_emails is not None:
        if isinstance(allowed_emails, str):
            allowed_emails = [e.strip() for e in allowed_emails.replace(";", ",").split(",")]
        cfg["allowed_emails"] = [e.strip().lower() for e in allowed_emails if e and e.strip()]
    try:
        _CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return cfg


def status() -> dict:
    cfg = load_config()
    return {"configured": bool(cfg.get("client_id")),
            "client_id": cfg.get("client_id", ""),
            "allowed_emails": cfg.get("allowed_emails", [])}


def verify_id_token(id_token: str) -> dict:
    """Verify a Google ID token. Returns {ok, email, name, error}."""
    cfg = load_config()
    client_id = (cfg.get("client_id") or "").strip()
    if not client_id:
        return {"ok": False, "error": "כניסה עם Google לא הוגדרה (חסר Client ID בהגדרות)"}
    if not id_token:
        return {"ok": False, "error": "לא התקבל אסימון מגוגל"}
    try:
        url = _TOKENINFO + urllib.parse.quote(id_token, safe="")
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        return {"ok": False, "error": f"אימות מול Google נכשל: {str(exc)[:80]}"}

    if data.get("aud") != client_id:
        return {"ok": False, "error": "האסימון לא הונפק עבור אפליקציה זו"}
    if data.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return {"ok": False, "error": "מנפיק אסימון לא מוכר"}
    if str(data.get("email_verified", "")).lower() not in ("true", "1"):
        return {"ok": False, "error": "כתובת המייל בגוגל אינה מאומתת"}

    email = (data.get("email") or "").lower()
    allowed = [e.lower() for e in (cfg.get("allowed_emails") or [])]
    if allowed and email not in allowed:
        return {"ok": False, "error": f"החשבון {email} אינו מורשה להתחבר למערכת"}
    return {"ok": True, "email": email, "name": data.get("name") or email}
