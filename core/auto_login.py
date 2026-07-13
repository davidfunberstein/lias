"""Auto-login helper — detects OTP fields and fills them automatically."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from core.logger import Logger


# Hebrew / English keywords that suggest an OTP input page
_OTP_KEYWORDS = ["קוד", "otp", "אימות", "one-time", "one time", "verification code", "קוד אימות"]


def _page_has_otp_field(page: "Page") -> bool:
    """Return True if the current page appears to contain an OTP input."""
    # Numeric input with maxlength between 4 and 8 — classic OTP field
    for maxlen in range(4, 9):
        if page.query_selector(f'input[type="number"][maxlength="{maxlen}"]'):
            return True
        if page.query_selector(f'input[type="text"][maxlength="{maxlen}"]'):
            return True

    # Keyword scan in visible text
    try:
        body_text = page.inner_text("body").lower()
        for kw in _OTP_KEYWORDS:
            if kw in body_text:
                return True
    except Exception:
        pass

    return False


def _page_has_password_field(page: "Page") -> bool:
    """Return True if the current page has a password input."""
    return page.query_selector('input[type="password"]') is not None


def _fill_and_submit_otp(page: "Page", otp_code: str, logger: "Logger | None") -> bool:
    """Fill the OTP into the first matching input and submit the form."""
    selectors = [
        'input[type="number"]',
        'input[type="text"][maxlength]',
        'input[autocomplete="one-time-code"]',
        'input[name*="otp"]',
        'input[name*="code"]',
        'input[id*="otp"]',
        'input[id*="code"]',
    ]
    for sel in selectors:
        field = page.query_selector(sel)
        if field:
            try:
                field.fill(otp_code)
                if logger:
                    logger.info(f"[AutoLogin] Filled OTP into selector: {sel}")
                # Try to find a submit button
                submit = page.query_selector('button[type="submit"]') or page.query_selector('input[type="submit"]')
                if submit:
                    submit.click()
                    if logger:
                        logger.info("[AutoLogin] Clicked submit button.")
                else:
                    field.press("Enter")
                    if logger:
                        logger.info("[AutoLogin] Pressed Enter to submit OTP.")
                return True
            except Exception as exc:
                if logger:
                    logger.warn(f"[AutoLogin] Failed to fill OTP with selector {sel}: {exc}")
    return False


def attempt_auto_login(
    page: "Page",
    email_config: dict,
    logger: "Logger | None" = None,
) -> bool:
    """Check if the current page is a login/OTP page and handle it automatically.

    Workflow:
      1. If a password field is found: currently logs a warning and returns False
         (credential filling is intentionally left to the user for security reasons).
      2. If an OTP field is found: waits for the OTP email and fills it automatically.

    Returns True if an OTP was successfully submitted, False otherwise.
    """
    if _page_has_password_field(page):
        msg = "[AutoLogin] Password login page detected — manual login required (credentials not auto-filled)."
        print(msg)
        if logger:
            logger.info(msg)
        return False

    if not _page_has_otp_field(page):
        # Not a login page at all
        return False

    print("[AutoLogin] OTP page detected — fetching code from email...")
    if logger:
        logger.info("[AutoLogin] OTP input page detected. Starting email polling.")

    try:
        from core.email_otp import EmailOTPReader
        reader = EmailOTPReader(email_config, logger=logger)
        otp = reader.wait_for_otp(timeout_seconds=120, poll_interval=5)
    except Exception as exc:
        msg = f"[AutoLogin] EmailOTPReader error: {exc}"
        print(msg)
        if logger:
            logger.error(msg)
        return False

    if not otp:
        print("[AutoLogin] No OTP received within timeout — login not completed.")
        if logger:
            logger.warn("[AutoLogin] OTP not received — login incomplete.")
        return False

    success = _fill_and_submit_otp(page, otp, logger)
    if success:
        print(f"[AutoLogin] OTP submitted successfully.")
    else:
        print("[AutoLogin] Could not find a suitable OTP input field to fill.")
        if logger:
            logger.warn("[AutoLogin] OTP field not fillable.")
    return success
