"""TOTP (Google Authenticator) code generation — stdlib only, no pyotp.

EN: Generates the current 6-digit RFC-6238 TOTP code from a base32 secret, so
    login can use an authenticator-app code instead of waiting on an email OTP
    (faster and not subject to email delivery lag). The secret is stored in the
    OS keychain, never on disk.
HE: מחשב את קוד ה-TOTP הנוכחי (6 ספרות) מסוד base32 — כדי להתחבר עם קוד
    מאפליקציית אימות (Google Authenticator) במקום להמתין ל-OTP במייל.
    הסוד נשמר ב-Keychain, לעולם לא בקובץ.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

_KEYCHAIN_SERVICE = "gov-il-connect-totp"
_KEYCHAIN_ACCOUNT = "default"


def _normalize(secret: str) -> bytes:
    """Base32-decode an authenticator secret, tolerating spaces/lowercase and
    missing '=' padding (Google shows the secret in 4-char groups)."""
    s = (secret or "").replace(" ", "").replace("-", "").upper()
    if not s:
        raise ValueError("empty TOTP secret")
    s += "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s, casefold=True)


def totp_now(secret: str, digits: int = 6, period: int = 30,
             algorithm: str = "SHA1", at: float | None = None) -> str:
    """Return the current TOTP code for a base32 secret."""
    key = _normalize(secret)
    counter = int((at if at is not None else time.time()) // period)
    msg = struct.pack(">Q", counter)
    digestmod = getattr(hashlib, algorithm.lower(), hashlib.sha1)
    h = hmac.new(key, msg, digestmod).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(code: str, secret: str = "", window: int = 1) -> bool:
    """Check a 6-digit code against the stored secret, allowing ±`window`
    time-steps (30s each) for clock drift — the standard TOTP practice."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    secret = secret or get_totp_secret()
    if not secret:
        return False
    now = time.time()
    for step in range(-window, window + 1):
        try:
            if totp_now(secret, at=now + step * 30) == code:
                return True
        except Exception:
            return False
    return False


def provisioning_uri(account: str = "LIAS", issuer: str = "LIAS") -> str:
    """otpauth:// URI for enrolling this secret in Google Authenticator."""
    from urllib.parse import quote
    secret = get_totp_secret()
    if not secret:
        return ""
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}"
            f"?secret={secret}&issuer={quote(issuer)}&digits=6&period=30")


def get_totp_secret() -> str:
    """Load the stored TOTP secret from the OS keychain (empty if none)."""
    try:
        import keyring
        return (keyring.get_password(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT) or "").strip()
    except Exception:
        return ""


def set_totp_secret(secret: str) -> bool:
    """Store (or clear) the TOTP secret in the OS keychain. Validates it first."""
    try:
        import keyring
        secret = (secret or "").strip()
        if secret:
            _normalize(secret)          # raises if not valid base32
            totp_now(secret)            # raises if it can't generate
            keyring.set_password(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT, secret)
        else:
            try:
                keyring.delete_password(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT)
            except Exception:
                pass
        return True
    except Exception:
        return False


def totp_configured() -> bool:
    return bool(get_totp_secret())
