"""Handles the initial connection and browser navigation to government portals."""

import time
from datetime import datetime
from pathlib import Path
from playwright.sync_api import Page
from enum import Enum
from core.i18n import t


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def _progress(message: str) -> None:
    """Print a login-progress step; when running under LIAS, also broadcast SSE
    so the web UI can show where we are in the flow."""
    print(f"{_ts()} [Auth] {message}")
    try:
        from core.download import SESSION_SETTINGS as _ss
        if _ss.get("lias_mode"):
            from LIAS import jobs as _jobs
            _jobs.broadcast({"type": "auth_progress", "message": message})
    except Exception:
        pass


class ConnectionType(Enum):
    BDR = "bdr"
    NET = "net"


SHARED_PROFILE_DIR = "./browser_profile"

NET_URL = "https://www.court.gov.il/ngcs.web.site/homepage.aspx"
BDR_URL = "https://sides.rbc.gov.il/Pages/FilesList.aspx"


class UserGoBack(Exception):
    """Raised when the user types 'b' / 'back' at the wait-for-Enter prompt."""


def is_page_alive(page: Page) -> bool:
    """Return True if the page/browser connection is still usable."""
    try:
        _ = page.url
        return True
    except Exception:
        return False


def recover_browser_session(
    page: Page,
    portal: str,
    session_settings=None,
    logger=None,
    max_retries: int = 3,
) -> Page:
    """Attempt to reconnect/relaunch the browser and re-authenticate.

    Call this when a navigation step raises a Playwright disconnection error.
    Returns a Page object (may be the same page if recovery succeeded, or a new page).

    The caller must update its own ``page`` reference to the returned value.
    """
    _log = lambda msg, lvl="warn": (
        logger and getattr(logger, lvl, logger.info)(f"[BrowserRecovery] {msg}")
        or print(f"{_ts()} [BrowserRecovery] {msg}")
    )

    for attempt in range(1, max_retries + 1):
        _log(f"ניסיון שחזור {attempt}/{max_retries}...")
        try:
            if is_page_alive(page):
                url = NET_URL if portal == "NET" else BDR_URL
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                _log("הדפדפן עדיין חי — ניווט מחדש לפורטל.")
                return page
        except Exception:
            pass

        # Browser context crashed or closed — try to relaunch
        _log("הדפדפן לא מגיב — מנסה לאתחל מחדש...", "warn")
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch_persistent_context(
                SHARED_PROFILE_DIR,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            new_page = browser.new_page()
            url = NET_URL if portal == "NET" else BDR_URL
            new_page.goto(url, wait_until="domcontentloaded", timeout=25000)
            _log("דפדפן חדש הופעל בהצלחה.")

            # Re-authenticate
            from core.gov_login import (
                _is_already_logged_in_bdr, _is_already_logged_in_net,
                is_bdr_login_page, handle_bdr_login_page,
            )
            if portal == "BDR":
                if not _is_already_logged_in_bdr(new_page):
                    if is_bdr_login_page(new_page):
                        handle_bdr_login_page(new_page, session_settings=session_settings)
                    _run_gov_autologin(new_page, "BDR")
                    time.sleep(1.5)
                    for _ in range(3):
                        if is_bdr_login_page(new_page):
                            handle_bdr_login_page(new_page, session_settings=session_settings)
                            time.sleep(2)
                            break
                        if _is_already_logged_in_bdr(new_page):
                            break
                        time.sleep(1)
            else:
                if not _is_already_logged_in_net(new_page):
                    from core.gov_login import handle_net_portal_entry
                    handle_net_portal_entry(new_page)
                    _run_gov_autologin(new_page, "NET")

            _log("שחזור הושלם.", "info")
            return new_page

        except Exception as exc:
            _log(f"ניסיון {attempt} נכשל: {exc}", "error")
            time.sleep(3 * attempt)

    _log("כל ניסיונות השחזור נכשלו — ממשיך בלי שחזור.", "error")
    return page


# ---------------------------------------------------------------------------
# Shared login helper
# ---------------------------------------------------------------------------

def ensure_logged_in(page: Page, portal: str) -> bool:
    """
    Unified entry point: navigate to the portal (if needed) and complete the
    full login chain. Idempotent — returns immediately if already logged in.
    Shared by all LIAS job handlers (open_portal / net_scan / bdr_batch / …).
    Returns True when an authenticated portal session is available.
    """
    from core.gov_login import (
        handle_net_portal_entry, handle_access_policy_interstitial,
        _is_already_logged_in_net, _is_already_logged_in_bdr,
        is_bdr_login_page, handle_bdr_login_page,
    )
    from core.download import SESSION_SETTINGS

    is_net = portal.upper() == "NET"
    url = NET_URL if is_net else BDR_URL
    already = _is_already_logged_in_net if is_net else _is_already_logged_in_bdr

    if already(page):
        return True

    _progress(f"פותח פורטל {portal}")
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            break
        except Exception as e:
            if attempt:
                raise RuntimeError(f"לא הצלחתי לפתוח {url}: {e}")
            time.sleep(2)
    time.sleep(1)

    handle_access_policy_interstitial(page)
    if already(page):
        _progress("מחובר ✓ (session קיים)")
        return True

    if is_net:
        _progress("לוחץ 'הזדהות לאומית'")
        handle_net_portal_entry(page)
        _run_gov_autologin(page, "NET")
        # Sometimes gov.il drops us back on the PUBLIC homepage even though the
        # SSO session is now valid — one more 'הזדהות לאומית' click enters
        # instantly without another OTP.
        for _ in range(2):
            time.sleep(2)
            if already(page):
                break
            if "court.gov.il" in (page.url or "") and "login.gov.il" not in (page.url or ""):
                _progress("חוזר לפורטל — לוחץ 'הזדהות לאומית' שוב")
                handle_access_policy_interstitial(page)
                handle_net_portal_entry(page)
                time.sleep(3)
    else:
        if is_bdr_login_page(page):
            handle_bdr_login_page(page, session_settings=SESSION_SETTINGS)
        _run_gov_autologin(page, "BDR")
        # After gov.il auth we may land back on Login.aspx — re-select entity
        time.sleep(1.5)
        for _ in range(3):
            if is_bdr_login_page(page):
                handle_bdr_login_page(page, session_settings=SESSION_SETTINGS)
                time.sleep(2)
                break
            if already(page):
                break
            time.sleep(1)

    return already(page)


def _run_gov_autologin(page: Page, portal_name: str) -> None:
    """
    Detect login.gov.il in the current tab or any open tab and run the full
    auto-login flow (credentials + OTP). Shared by NET and BDR.

    For NET: caller should already have clicked 'הזדהות לאומית' before calling this.
    For BDR: the portal redirects straight to login.gov.il — just call this after goto().
    """
    from core.gov_login import auto_login_flow, handle_access_policy_interstitial
    from core.credentials import credentials_exist

    _progress(f"מתחבר ל-{portal_name} — ממתין ל-login.gov.il")

    # Handle the "Access policy evaluation… click here" interstitial if present
    try:
        handle_access_policy_interstitial(page)
    except Exception:
        pass

    # Locate the login.gov.il page
    login_page: Page | None = None
    try:
        page.wait_for_url("*login.gov.il*", timeout=12000)
        login_page = page
        print(f"{_ts()} [Auth] Reached login.gov.il: {page.url}")
    except Exception:
        for p in page.context.pages:
            if "login.gov.il" in (p.url or ""):
                login_page = p
                print(f"{_ts()} [Auth] Found login.gov.il in tab: {p.url}")
                break

    if not (login_page and "login.gov.il" in (login_page.url or "")):
        print(f"{_ts()} [Auth] login.gov.il not detected — skipping auto-login.")
        return

    print(f"{_ts()} [Auth] {t('auth_starting', portal=portal_name)}")

    # ── Offer passkey / quick-login if available ──────────────────────────────
    from core.gov_login import has_passkey_button, try_passkey_login
    from core.download import SESSION_SETTINGS as _sess
    saved_method = _sess.get("login_method", "standard")

    def _bring_portal_to_front() -> None:
        portal_domain = "court.gov.il" if portal_name == "NET" else "rbc.gov.il"
        try:
            for p in page.context.pages:
                if not p.is_closed() and portal_domain in (p.url or ""):
                    p.bring_to_front()
                    break
        except Exception:
            pass

    # ── Handle WebAuthn-first redirect ───────────────────────────────────────
    # BDR sometimes lands on ?id=webauthn instead of the standard password page.
    # We must NOT do a bare goto() — that breaks the SAML session.
    # Instead, click the "login with username/password" link that is on that page.
    current_login_url = login_page.url or ""
    if "id=webauthn" in current_login_url and saved_method != "passkey":
        print(f"{_ts()} [Auth] WebAuthn URL detected — looking for password login link...")
        _switched = False
        _pw_link_selectors = [
            'a[href*="usernamePasswordSMSOtp"]',
            'a:has-text("שם משתמש")',
            'a:has-text("סיסמה")',
            'a:has-text("כניסה עם שם משתמש")',
            'button:has-text("כניסה עם שם משתמש")',
            # Generic "other login method" links
            'a[class*="login-option"]',
            'a[class*="method"]',
        ]
        for _sel in _pw_link_selectors:
            try:
                _btn = login_page.locator(_sel).first
                if _btn.count() > 0 and _btn.is_visible(timeout=2000):
                    _btn.click()
                    print(f"{_ts()} [Auth] Clicked password login link ({_sel}) — waiting for form...")
                    try:
                        login_page.wait_for_selector('input#userId', timeout=8000)
                    except Exception:
                        time.sleep(2)
                    _switched = True
                    break
            except Exception:
                continue
        if not _switched:
            # Last resort: dump links on page for debugging
            try:
                _links = login_page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href]')).map(a => a.href + ' | ' + a.innerText.trim()).slice(0,15);
                }""")
                print(f"{_ts()} [Auth] WebAuthn page links (debug): {_links}")
            except Exception:
                pass
            print(f"{_ts()} [Auth] Could not find password login link on WebAuthn page — proceeding anyway.")

    if has_passkey_button(login_page):
        if saved_method == "passkey":
            # Saved preference: auto-use passkey, no prompt
            _progress("כניסה מהירה (Passkey) — אשר ב-Touch ID / בחר תעודת זהות בחלון שנפתח")
            success = try_passkey_login(login_page, wait_seconds=120)
            if success:
                _bring_portal_to_front()
                time.sleep(1)
                return
            print(f"{_ts()} [Auth] {t('passkey_fallback')}")
            # Fall through to standard login

        elif saved_method == "standard":
            # Saved preference: use standard login, skip passkey prompt entirely
            pass  # fall through directly to credentials + OTP

        elif _sess.get("lias_mode"):
            # LIAS mode with no saved preference — no terminal to ask on;
            # default to standard login (user can pick passkey in settings).
            pass

        else:
            # No saved preference — ask once, then remember
            print(f"\n{'─' * 54}")
            print(f"  {t('passkey_detected')}")
            print(f"{'─' * 54}")
            print(f"  {t('passkey_option_p')}")
            print(f"  {t('passkey_option_enter')}")
            print(f"  (הבחירה תישמר להפעלות הבאות — ניתן לשנות בהגדרות)")
            print(f"{'─' * 54}")
            raw = input(f"  {t('passkey_prompt')} ").strip().lower()
            if raw == "p":
                # Save choice
                from core.download import SESSION_SETTINGS as _s2
                _s2["login_method"] = "passkey"
                try:
                    from core.runner import _save_persistent_settings
                    _save_persistent_settings()
                except Exception:
                    pass
                success = try_passkey_login(login_page, wait_seconds=90)
                if success:
                    _bring_portal_to_front()
                    time.sleep(1)
                    return
                print(f"{_ts()} [Auth] {t('passkey_fallback')}")
            else:
                # Save standard as preference
                from core.download import SESSION_SETTINGS as _s2
                _s2["login_method"] = "standard"
                try:
                    from core.runner import _save_persistent_settings
                    _save_persistent_settings()
                except Exception:
                    pass

    # ── Standard login: credentials + OTP ────────────────────────────────────
    # Build email OTP reader if configured
    _email_reader = None
    try:
        from core.email_otp import EmailOTPReader, load_email_config
        # Absolute path — CWD differs when the engine is launched from ui_demo
        _cfg_path = Path(__file__).resolve().parent.parent / "email_config.json"
        _cfg = load_email_config(_cfg_path)
        if _cfg:
            _email_reader = EmailOTPReader(_cfg)
    except Exception:
        pass

    if credentials_exist():
        _progress("ממלא תעודת זהות וסיסמה")
        auto_login_flow(login_page, email_reader=_email_reader)
    else:
        _progress("אין אישורי כניסה שמורים — הזן ת\"ז וסיסמה בהגדרות")
        print(f"{_ts()} [Auth] {t('auth_tip')}")
        return

    # Wait for the login page to navigate away from login.gov.il
    _progress("ממתין לאישור התחברות")
    print(f"{_ts()} [Auth] {t('auth_waiting_redirect')}")
    logged_in = False
    try:
        login_page.wait_for_url(
            lambda u: "login.gov.il" not in u,
            timeout=45000,
        )
        _progress("מחובר ✓")
        print(f"{_ts()} [Auth] {t('auth_complete', url=login_page.url)}")
        logged_in = True
    except Exception:
        # Fallback: scan all open tabs
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                all_urls = [p.url for p in page.context.pages if not p.is_closed()]
                non_login = [u for u in all_urls if u and u != "about:blank" and "login.gov.il" not in u]
                if non_login:
                    print(f"{_ts()} [Auth] Login complete (tab scan): {non_login[0]}")
                    logged_in = True
                    break
            except Exception:
                break
            time.sleep(1)

    if not logged_in:
        print(f"{_ts()} [Auth] {t('auth_timeout')}")

    # Bring the portal tab to front
    portal_domain = "court.gov.il" if portal_name == "NET" else "rbc.gov.il"
    try:
        for p in page.context.pages:
            if not p.is_closed() and portal_domain in (p.url or ""):
                p.bring_to_front()
                break
    except Exception:
        pass
    time.sleep(1)


# ---------------------------------------------------------------------------
# Connection class
# ---------------------------------------------------------------------------

class GovIlConnection:
    def __init__(self, connection_type: ConnectionType) -> None:
        self.connection_type = connection_type

    def connect(self, page: Page) -> Page:
        is_net = self.connection_type == ConnectionType.NET
        url = NET_URL if is_net else BDR_URL
        portal = "NET" if is_net else "BDR"

        # Navigate — domcontentloaded is fast; JS header renders shortly after
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass

        # ── NET portal ──────────────────────────────────────────────────────
        if is_net:
            try:
                from core.gov_login import (
                    handle_net_portal_entry,
                    _is_already_logged_in_net,
                )

                if _is_already_logged_in_net(page):
                    print(f"{_ts()} [Auth] {t('auth_already_net')}")
                    return self._prompt_and_return(page, portal)

                # Click הזדהות לאומית — then handle login.gov.il
                handle_net_portal_entry(page)
                _run_gov_autologin(page, "NET")

            except UserGoBack:
                raise
            except Exception as e:
                print(f"{_ts()} [Auth] NET auto-login skipped: {e}")

        # ── BDR portal ──────────────────────────────────────────────────────
        else:
            try:
                from core.gov_login import (
                    _is_already_logged_in_bdr,
                    is_bdr_login_page,
                    handle_bdr_login_page,
                )
                from core.download import SESSION_SETTINGS

                if _is_already_logged_in_bdr(page):
                    print(f"{_ts()} [Auth] {t('auth_already_bdr')}")
                    return self._prompt_and_return(page, portal)

                # BDR login flow:
                # FilesList → Login.aspx → gov.il OTP → Login.aspx (again) → FilesList
                if is_bdr_login_page(page):
                    handle_bdr_login_page(page, session_settings=SESSION_SETTINGS)

                # gov.il OTP auth
                _run_gov_autologin(page, "BDR")

                # After gov.il auth we land back on Login.aspx — handle entity type again
                time.sleep(1.5)
                for _ in range(3):
                    if is_bdr_login_page(page):
                        print(f"{_ts()} [Auth] Back on BDR Login.aspx after gov.il auth — selecting entity type.")
                        handle_bdr_login_page(page, session_settings=SESSION_SETTINGS)
                        time.sleep(2)
                        break
                    if _is_already_logged_in_bdr(page):
                        break
                    time.sleep(1)

            except UserGoBack:
                raise
            except Exception as e:
                print(f"{_ts()} [Auth] BDR auto-login skipped: {e}")

        return self._prompt_and_return(page, portal)

    def _prompt_and_return(self, page: Page, portal: str) -> Page:
        """Show the 'press Enter to start' prompt and return the page."""
        from core.download import SESSION_SETTINGS as _ss
        if _ss.get("lias_mode"):
            # Running inside LIAS web UI — no terminal, skip input()
            print(f"[Auth] {portal} portal ready (LIAS mode)")
            return page
        print("\n" + "*" * 60)
        print(t("conn_connected", portal=portal))
        print(t("conn_navigate"))
        print(t("conn_ensure_grid"))
        print(t("conn_press_enter"))
        print("*" * 60)
        ans = input(t("conn_prompt")).strip().lower()
        if ans in ("b", "back", "q"):
            raise UserGoBack()
        return page
