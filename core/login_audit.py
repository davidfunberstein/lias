"""Login audit log + per-portal concurrency guard (semaphore).

EN: Every login attempt to a government portal is recorded here — when, which
    portal, which method (email-OTP / TOTP / passkey / session), and the
    outcome. This gives the operator full visibility and control over "who is
    connecting" (David's request). A per-portal lock (semaphore of size 1)
    prevents two logins to the SAME portal running at once, which is what
    triggered duplicate OTP emails and gov.il rate-limits.
HE: כל ניסיון התחברות לפורטל ממשלתי נרשם כאן — מתי, לאיזה פורטל, באיזו שיטה,
    ומה התוצאה. נותן שליטה מלאה ב"מי מתחבר". מנעול לכל פורטל (סמפור בגודל 1)
    מונע שתי התחברויות במקביל לאותו פורטל — מה שגרם ל-OTP כפולים וחסימות.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

_AUDIT_PATH = Path(__file__).resolve().parent.parent / "login_audit.log"
_lock = threading.Lock()

# One re-entrant-safe lock per portal so only one login runs per portal at a
# time. A plain Lock (not RLock) — the login flow never re-enters itself.
_portal_locks: dict[str, threading.Lock] = {}
_portal_locks_guard = threading.Lock()


def _portal_lock(portal: str) -> threading.Lock:
    with _portal_locks_guard:
        return _portal_locks.setdefault(portal.upper(), threading.Lock())


def record(portal: str, method: str, status: str, detail: str = "",
           user: str = "") -> None:
    """Append one audit entry (JSONL) and broadcast it to the UI live log.

    status: 'start' | 'success' | 'failed' | 'blocked' | 'otp_sent' | …
    """
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "portal": (portal or "").upper(),
        "method": method,
        "status": status,
        "detail": (detail or "")[:300],
        "user": user,
    }
    try:
        with _lock:
            with _AUDIT_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # Mirror to the console + the UI SSE stream so it shows live.
    icon = {"success": "✓", "failed": "✗", "blocked": "⛔",
            "start": "→", "otp_sent": "✉"}.get(status, "·")
    print(f"[{entry['ts']}] [LoginAudit] {icon} {entry['portal']} "
          f"{method} — {status} {detail}".rstrip())
    try:
        from LIAS import jobs as _jobs
        _jobs.broadcast({"type": "login_audit", **entry})
    except Exception:
        pass


def read_log(limit: int = 200) -> list[dict]:
    """Return the most recent audit entries, newest first (for a UI panel)."""
    try:
        lines = _AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    out.reverse()
    return out


class portal_login:
    """Context manager: hold the per-portal login lock, and audit start/result.

        with portal_login("ECA", method="totp", user="…") as sess:
            ...run the login...
            sess.success("logged in")   # or sess.fail("…")

    If another login for the same portal is already running, this blocks up to
    ``wait`` seconds; if still busy it records a 'blocked' entry and raises.
    """

    def __init__(self, portal: str, method: str = "standard", user: str = "",
                 wait: float = 300.0):
        self.portal = (portal or "").upper()
        self.method = method
        self.user = user
        self.wait = wait
        self._lk = _portal_lock(self.portal)
        self._held = False
        self._resolved = False

    def __enter__(self) -> "portal_login":
        if not self._lk.acquire(timeout=self.wait):
            record(self.portal, self.method, "blocked",
                   "התחברות אחרת לאותו פורטל כבר רצה — לא הופעלה כפילות",
                   self.user)
            raise RuntimeError(
                f"התחברות ל-{self.portal} כבר מתבצעת — נסה שוב בעוד רגע")
        self._held = True
        record(self.portal, self.method, "start", "", self.user)
        return self

    def success(self, detail: str = "") -> None:
        if not self._resolved:
            record(self.portal, self.method, "success", detail, self.user)
            self._resolved = True

    def fail(self, detail: str = "") -> None:
        if not self._resolved:
            record(self.portal, self.method, "failed", detail, self.user)
            self._resolved = True

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._resolved:
            if exc is not None:
                record(self.portal, self.method, "failed",
                       f"{type(exc).__name__}: {exc}", self.user)
            else:
                record(self.portal, self.method, "success", "", self.user)
            self._resolved = True
        if self._held:
            try:
                self._lk.release()
            except Exception:
                pass
