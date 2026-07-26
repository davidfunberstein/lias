"""Shared gov.il SSO session across the three portal browsers.

EN: NET, BDR and ECA each run in their OWN persistent browser profile, so a
    successful login in one portal left the other two logged out — each asked
    for its own OTP even though a live gov.il session already existed. That is
    the "no memory that the system already logged in" problem.

    This module keeps ONE copy of the gov.il SSO cookies on disk. After any
    successful portal login we save them; before a login we inject them into
    the context that is about to authenticate, so an existing session is
    reused instead of triggering a second OTP.

HE: לכל פורטל יש פרופיל דפדפן נפרד, ולכן התחברות באחד לא "נזכרה" באחרים —
    כל אחד ביקש OTP משלו למרות שכבר קיים סשן חי. המודול שומר עותק אחד של
    עוגיות ה-SSO של gov.il: אחרי התחברות מוצלחת שומרים, ולפני התחברות
    מזריקים — כך סשן קיים מנוצל מחדש במקום לשלוח קוד נוסף.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_STORE = Path(__file__).resolve().parent.parent / ".gov_session.json"
_lock = threading.Lock()

# Cookies worth sharing — the SSO identity provider and the portals themselves.
_SHARED_DOMAINS = (
    "login.gov.il", ".gov.il", "gov.il",
    "securesso.court.gov.il", "court.gov.il",
    "sides.rbc.gov.il", "rbc.gov.il",
    "publicsso.eca.gov.il", "eca.gov.il",
    "amazoncognito.com", "il-central-1.amazoncognito.com",
)

# A gov.il SSO session is short-lived; don't reuse a stale one.
MAX_AGE_SEC = 30 * 60


def _is_shared(cookie: dict) -> bool:
    dom = (cookie.get("domain") or "").lstrip(".").lower()
    return any(dom == d.lstrip(".").lower() or dom.endswith("." + d.lstrip(".").lower())
               for d in _SHARED_DOMAINS)


def save_from_context(context, portal: str = "") -> int:
    """Persist the gov.il-related cookies of a context that just authenticated.
    Returns how many cookies were stored."""
    try:
        cookies = [c for c in context.cookies() if _is_shared(c)]
    except Exception:
        return 0
    if not cookies:
        return 0
    payload = {"ts": time.time(), "portal": portal, "cookies": cookies}
    try:
        with _lock:
            _STORE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"[GovSession] נשמר סשן gov.il משותף ({len(cookies)} עוגיות, מקור: {portal})")
        return len(cookies)
    except Exception:
        return 0


def load() -> list:
    """Return the stored cookies if they are still fresh, else []."""
    try:
        with _lock:
            data = json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if time.time() - float(data.get("ts") or 0) > MAX_AGE_SEC:
        return []
    return data.get("cookies") or []


def apply_to_context(context, portal: str = "") -> bool:
    """Inject a stored, still-fresh gov.il session into this context so an
    already-authenticated session is reused (no second OTP). True if applied."""
    cookies = load()
    if not cookies:
        return False
    try:
        context.add_cookies(cookies)
        print(f"[GovSession] הוזרק סשן gov.il קיים ל-{portal or 'פורטל'} "
              f"({len(cookies)} עוגיות) — אין צורך בקוד נוסף")
        return True
    except Exception as exc:
        print(f"[GovSession] הזרקת סשן נכשלה: {str(exc)[:80]}")
        return False


def clear() -> None:
    try:
        with _lock:
            _STORE.unlink(missing_ok=True)
    except Exception:
        pass
