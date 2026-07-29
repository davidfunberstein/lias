"""Keychain access that cannot hang the app.

A macOS Keychain item remembers WHICH binary created it. Read it from a
different binary and securityd asks the user to authorize — and if that dialog
cannot be shown (no GUI session, a sandboxed parent, or the user simply never
answers), the underlying SecItemCopyMatching call blocks *forever*.

That is not hypothetical. On a user's machine the credentials had been saved by
one Python, the project was re-cloned, and the new virtualenv resolved to a
uv-managed interpreter — a different binary, ad-hoc signed. Every read then
blocked in securityd. Because the HTTP server serves requests on a small pool,
one stuck read starved the rest: the settings page hung, the status endpoint
never answered, and syncing was impossible. The app looked dead, and nothing in
the log said why.

Every keyring call in the app goes through here. A blocked call is abandoned
after a few seconds and reported as "unknown" instead of freezing the process,
and the user is told exactly how to fix it.
"""
from __future__ import annotations

import threading

# Reading is on the hot path (status checks), so keep it short — a healthy
# Keychain answers in milliseconds. Writing may legitimately show a prompt the
# user has to click, so allow much longer before giving up on it.
READ_TIMEOUT_SEC = 5.0
WRITE_TIMEOUT_SEC = 30.0

_blocked = threading.Event()      # set once we know securityd is not answering


def is_blocked() -> bool:
    """True when a Keychain call has timed out during this run."""
    return _blocked.is_set()


REMEDY = (
    "הגישה ל-Keychain חסומה — macOS מבקש אישור שלא נענה.\n"
    "        הפריטים נשמרו בעבר על ידי גרסת פייתון אחרת, ולכן המערכת\n"
    "        מבקשת אישור מחדש. שתי דרכים לפתור:\n"
    "        1. אם קופץ חלון 'allow access' — לחץ Always Allow (לא Allow).\n"
    "        2. או אפס את הפריטים והזן מחדש בהגדרות ⚙:\n"
    "             security delete-generic-password -s gov-il-connect\n"
    "             security delete-generic-password -s gov-il-connect-email"
)


def _run(fn, timeout: float, what: str):
    """Run a keyring call on a throwaway thread; abandon it if securityd stalls.

    The thread is a daemon and is deliberately left behind when it hangs — it is
    stuck inside a C call that cannot be interrupted, and waiting on it is
    exactly the freeze we are avoiding. It costs one idle thread per incident.
    """
    box: dict = {}

    def work():
        try:
            box["v"] = fn()
        except Exception as exc:                     # keyring/backend failure
            box["e"] = exc

    t = threading.Thread(target=work, daemon=True, name=f"keychain-{what}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        if not _blocked.is_set():
            _blocked.set()
            print(f"[Keychain] ⛔ {what}: אין תשובה תוך {timeout:.0f} שניות.\n        {REMEDY}",
                  flush=True)
        raise TimeoutError(f"keychain {what} timed out")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def get_password(service: str, account: str, default=None):
    """Read a secret. Returns `default` when blocked or missing — never hangs."""
    try:
        import keyring
        return _run(lambda: keyring.get_password(service, account),
                    READ_TIMEOUT_SEC, f"קריאת {service}/{account}")
    except TimeoutError:
        return default
    except Exception as exc:
        print(f"[Keychain] קריאת {service}/{account} נכשלה: {exc}", flush=True)
        return default


def set_password(service: str, account: str, value: str) -> bool:
    """Write a secret. Returns False (with a logged reason) instead of hanging."""
    try:
        import keyring
        _run(lambda: keyring.set_password(service, account, value),
             WRITE_TIMEOUT_SEC, f"כתיבת {service}/{account}")
        _blocked.clear()          # a successful write means access is fine again
        return True
    except TimeoutError:
        return False
    except Exception as exc:
        print(f"[Keychain] כתיבת {service}/{account} נכשלה: {exc}", flush=True)
        return False


def delete_password(service: str, account: str) -> bool:
    try:
        import keyring
        _run(lambda: keyring.delete_password(service, account),
             READ_TIMEOUT_SEC, f"מחיקת {service}/{account}")
        return True
    except Exception:
        return False
