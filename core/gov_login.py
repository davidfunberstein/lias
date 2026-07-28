"""Automated login for login.gov.il (Israeli National Authentication)."""
from __future__ import annotations

import time
import threading
from datetime import datetime
from typing import TYPE_CHECKING
from core.i18n import t

_govil_login_lock = threading.Lock()


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from core.logger import Logger


# ---------------------------------------------------------------------------
# Selector sets
# ---------------------------------------------------------------------------

_ID_SELECTORS = [
    'input#userId',                      # login.gov.il exact ID
    'input[type="tel"]',                 # fallback — tel input on the same page
    'input[placeholder*="מספר זהות"]',
    'input[placeholder*="---"]',
    'input[name*="username"]',
    'input[name="Ecom_User_ID"]',
]

_PASSWORD_SELECTORS = [
    'input#userPass',                    # login.gov.il exact ID
    'input[type="password"]',
]

_SUBMIT_SELECTORS = [
    'button#loginSubmit',                # login.gov.il exact ID
    'button:has-text("כניסה")',
    'input[type="submit"][value="כניסה"]',
]

_EMAIL_LINK_SELECTORS = [
    'a:has-text("דואר אלקטרוני")',
    'a:has-text("כתובת דוא")',
    'span:has-text("דואר אלקטרוני")',
]

_OTP_INPUT_SELECTORS = [
    'input#smsOtp',                      # login.gov.il SMS/phone OTP exact ID
    'input#mailOtp',                     # login.gov.il email OTP exact ID
    'input[id*="Otp"]',
    'input[id*="otp"]',
    'input[id*="OTP"]',
    'input[name*="otp" i]',
    'input[placeholder*="_ _"]',
    'input[maxlength="6"]',
    'input[type="tel"][placeholder]',
    'input[type="tel"]',
    'input[inputmode="numeric"]',
]

_OTP_SUBMIT_SELECTORS = [
    'button#loginSMSOtpSubmit',          # phone OTP submit exact ID (try first)
    'button#loginMailOtpSubmit',         # email OTP submit exact ID
    'button#loginSubmit',
    'button[type="submit"]:has-text("כניסה")',
    'button:has-text("כניסה")',
    'input[type="submit"][value*="כניסה"]',
]


# ---------------------------------------------------------------------------
# NET HaMishpat pre-login helpers
# ---------------------------------------------------------------------------

def handle_net_portal_entry(page: "Page", logger: "Logger | None" = None) -> None:
    """
    Handle the NET HaMishpat portal entry sequence:
      1. Dismiss the terms/cookies popup ("אישור") if present.
      2. Click the "הזדהות לאומית" button to start the national-auth flow.

    Call this after navigating to court.gov.il and before waiting for login.gov.il.
    Safe to call even if the popup is already dismissed or the page is already past this step.
    """
    # Step 1 — Dismiss popup (terms / cookie notice). It can appear with a
    # delay, so poll a few times before giving up.
    for _try in range(4):
        try:
            popup_btn = page.locator(
                'button:has-text("אישור"), '
                'input[type="button"][value="אישור"], '
                'input[type="submit"][value="אישור"], '
                'a:has-text("אישור")'
            ).first
            if popup_btn.count() > 0 and popup_btn.is_visible(timeout=2000):
                popup_btn.click()
                print(f"{_ts()} [Auth] Dismissed NET portal popup (אישור).")
                if logger:
                    logger.info("[GovLogin] Dismissed NET portal terms popup.")
                time.sleep(0.8)
                break
        except Exception:
            pass
        time.sleep(0.7)

    # Step 2 — Click "הזדהות לאומית" to enter the national auth flow
    _NAT_AUTH_SELECTORS = [
        '#btnSSOEnterIn',                    # exact ID on NET homepage
        'a:has-text("הזדהות לאומית")',
        'button:has-text("הזדהות לאומית")',
        'input[value="הזדהות לאומית"]',
        'a[href*="login.gov.il"]',
        # The button is sometimes inside a specific widget on the NET homepage
        '#btnNationalAuth',
        'a.national-auth',
    ]
    clicked = False
    for sel in _NAT_AUTH_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=3000):
                el.click()
                clicked = True
                print(f"{_ts()} [Auth] Clicked 'הזדהות לאומית' — redirecting to login.gov.il...")
                if logger:
                    logger.info(f"[GovLogin] Clicked national auth button: {sel}")
                break
        except Exception:
            continue

    if not clicked:
        # Try clicking the blue button in the top-right "פתיחת תיק" section
        try:
            btn = page.locator('text="הזדהות לאומית"').first
            if btn.is_visible(timeout=3000):
                btn.click()
                clicked = True
                print(f"{_ts()} [Auth] Clicked 'הזדהות לאומית' (text locator).")
        except Exception:
            pass

    if not clicked and logger:
        logger.warn("[GovLogin] 'הזדהות לאומית' button not found — user may need to click manually.")


def handle_access_policy_interstitial(page: "Page", logger: "Logger | None" = None) -> bool:
    """
    Handle the gov.il error page:
      "Access policy evaluation is already in progress for your current session…
       click here to create a new session."
    Clicks the 'here' link (javascript:document.location=redirectURI) to start
    a fresh session. Returns True if the interstitial was found and clicked.
    """
    try:
        body = page.content()
        if "Access policy evaluation" not in body:
            return False
    except Exception:
        return False
    print(f"{_ts()} [Auth] Access-policy interstitial detected — clicking 'here'.")
    if logger:
        logger.info("[GovLogin] Access-policy interstitial detected — clicking 'here'.")
    _HERE_SELECTORS = [
        'a:has-text("here")',
        'a[href*="redirectURI"]',
        'a[href^="javascript:document.location"]',
    ]
    for sel in _HERE_SELECTORS:
        try:
            link = page.locator(sel).first
            if link.count() > 0 and link.is_visible(timeout=2000):
                link.click()
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                time.sleep(1.5)
                return True
        except Exception:
            continue
    # Last resort: execute the redirect ourselves
    try:
        page.evaluate("if (typeof redirectURI !== 'undefined') document.location = redirectURI;")
        time.sleep(2)
        return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _is_already_logged_in_bdr(page: "Page") -> bool:
    """Return True if BDR (sides.rbc.gov.il) shows an authenticated session."""
    try:
        url = page.url or ""
        if "login.gov.il" in url or "Login.aspx" in url:
            return False
        if "rbc.gov.il" not in url:
            return False
        content = page.content()
        indicators = ["תיקים שלי", "יציאה", "שלום", "התנתקות"]
        return any(ind in content for ind in indicators)
    except Exception:
        return False


def is_bdr_login_page(page: "Page") -> bool:
    """Return True if current page is the BDR internal login page (sides.rbc.gov.il/Login.aspx)."""
    try:
        url = page.url or ""
        return "rbc.gov.il" in url and "Login.aspx" in url
    except Exception:
        return False


_BDR_ENTITY_TYPES = [
    ('גורם פרטי', 'גורם פרטי'),
    ('עו"ד מייצג', 'עו"ד מייצג'),
    ('טו"ר מייצג', 'טו"ר מייצג'),
]

_BDR_COMBO_ID = "DetailsContainerPanel_ASPxRoundPanel2_cmbIsLayer7"


def _set_bdr_entity_combo(page: "Page", entity_value: str, logger: "Logger | None" = None) -> None:
    """Set the DevExpress entity-type combo to entity_value using JavaScript."""
    try:
        page.evaluate(
            """(val) => {
                var combo = aspxGetControlCollection().Get('DetailsContainerPanel_ASPxRoundPanel2_cmbIsLayer7');
                if (combo) combo.SetValue(val);
            }""",
            entity_value,
        )
        if logger:
            logger.info(f"[BDR] Entity combo set to: {entity_value!r}")
    except Exception as exc:
        print(f"{_ts()} [BDR] Warning: could not set entity combo via JS: {exc}")
        if logger:
            logger.warn(f"[BDR] Entity combo JS failed: {exc}")


def handle_bdr_login_page(
    page: "Page",
    lawyer_mode: bool = False,
    logger: "Logger | None" = None,
    session_settings: "dict | None" = None,
) -> bool:
    """
    Handle the BDR Login.aspx page.
    Always prompts the user to select entity type (גורם פרטי / עו"ד מייצג / טו"ר מייצג)
    before clicking 'כניסה למערכת'. Hebrew values are always used (portal requirement).
    Returns True if the button was clicked.
    """
    _ENTER_SYSTEM_SELECTORS = [
        "#DetailsContainerPanel_ASPxRoundPanel2_ASPxButton1_B",
        "td.dxbButton_Office2010Blue",
        'span:has-text("כניסה למערכת")',
        'input[value="כניסה למערכת"]',
        '[id*="ASPxButton1"]',
    ]

    print(f"{_ts()} [BDR] {t('bdr_login_detected')}")
    # Determine entity type from session_settings (user_mode) — no terminal prompt.
    _mode = (session_settings or {}).get("user_mode", "private")
    if _mode == "lawyer":
        choice_idx = 1   # עו"ד מייצג
    elif _mode == "pleader":
        choice_idx = 2   # טו"ר מייצג
    else:
        choice_idx = 0   # גורם פרטי (default)
    entity_value = _BDR_ENTITY_TYPES[choice_idx][1]
    print(f"{_ts()} [BDR] {t('entity_set')}{entity_value}")
    if logger:
        logger.info(f"[BDR] Entity type selected: '{entity_value}'.")
    # Store in session so callers can reference it later
    if session_settings is not None:
        session_settings["bdr_entity_type"] = entity_value
    _set_bdr_entity_combo(page, entity_value, logger=logger)

    # Brief pause to allow the DevExpress combo state to settle
    time.sleep(0.5)

    print(f"{_ts()} [BDR] {t('bdr_clicking_enter')}")

    for sel in _ENTER_SYSTEM_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=3000)
            page.click(sel)
            if logger:
                logger.info(f"[BDR] Clicked 'כניסה למערכת' with selector: {sel}")
            print(f"{_ts()} [BDR] {t('bdr_clicked_enter')}")
            return True
        except Exception:
            continue

    print(f"{_ts()} [BDR] {t('bdr_not_found')}")
    if logger:
        logger.warn("[BDR] 'כניסה למערכת' button not found.")
    return False


def _is_already_logged_in_net(page: "Page", wait_ms: int = 8000) -> bool:
    """Return True if the browser has an active authenticated session on the NET portal.

    Reliable indicators (in priority order):
    1. URL is on securesso.court.gov.il  AND  'יציאה' / 'אזור אישי' is visible
       → definitively authenticated on the secured portal.
    2. URL is on court.gov.il (any sub-domain) and page shows ת"ז / ID number
       → logged-in indicator present even on public homepage.
    3. URL is login.gov.il → definitely NOT logged in.
    4. URL not on court.gov.il at all → unknown / not logged in.

    The public homepage (ngcs.web.site/homepage.aspx) is accessible WITHOUT auth,
    so its mere presence does NOT mean the session is valid.
    """
    import time as _t
    try:
        url = page.url or ""

        if "login.gov.il" in url:
            return False
        if "court.gov.il" not in url:
            return False

        # On secured portal — look for authenticated header (fast path)
        if "securesso.court.gov.il" in url:
            try:
                page.wait_for_selector(
                    'a:has-text("יציאה"), a:has-text("אזור אישי")',
                    timeout=wait_ms,
                )
                return True
            except Exception:
                pass
            # Fallback: content scan
            _t.sleep(0.5)
            content = page.content()
            return any(ind in content for ind in ["אזור אישי", "יציאה"])

        # On public homepage or other court.gov.il page
        # "יציאה" does not appear here, but a ת"ז number does when authenticated
        _t.sleep(0.5)
        content = page.content()
        import re as _re
        # Look for displayed ID number pattern (7-9 digits rendered as user identifier)
        if _re.search(r'ת"ז\s*\d{7,9}', content):
            return True
        if "אזור אישי" in content or "יציאה" in content:
            return True
        return False
    except Exception:
        return False


def is_gov_login_page(page: "Page") -> bool:
    """Return True if current page is any login.gov.il auth page.

    Matches both:
    - Standard password page: ?id=usernamePasswordSMSOtp
    - WebAuthn/passkey variant: ?id=webauthn  (BDR sometimes lands here first)
    """
    try:
        url = page.url or ""
        return "login.gov.il" in url and (
            "usernamePasswordSMSOtp" in url or "id=webauthn" in url
        )
    except Exception:
        return False


def has_passkey_button(page: "Page") -> bool:
    """Return True if the login.gov.il page shows the 'כניסה מהירה' passkey button.

    The button has id='passkeyContainer' and appears on the standard login page
    alongside the ID+password form.  Clicking it opens a native WebAuthn dialog
    (phone / tablet / USB security key) that the user must complete manually.
    """
    try:
        if "login.gov.il" not in (page.url or ""):
            return False
        el = page.locator("#passkeyContainer").first
        return el.count() > 0 and el.is_visible(timeout=2000)
    except Exception:
        return False


def is_otp_page(page: "Page") -> bool:
    """Return True if current page is asking for OTP code."""
    try:
        url = page.url or ""
        if "login.gov.il" not in url:
            return False
        content = page.content()
        return "קוד אימות" in content
    except Exception:
        return False


def _is_sms_otp_page(page: "Page") -> bool:
    """Return True if on the SMS OTP page (not yet switched to email)."""
    try:
        content = page.content()
        return "שלחנו קוד אימות לטלפון הנייד" in content
    except Exception:
        return False


def _is_email_otp_page(page: "Page") -> bool:
    """Return True if on the email OTP page."""
    try:
        content = page.content()
        return "שלחנו קוד אימות לדוא" in content or "שלחנו קוד אימות לכתובת" in content
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Login steps
# ---------------------------------------------------------------------------

def perform_login(
    page: "Page",
    id_number: str,
    password: str,
    logger: "Logger | None" = None,
) -> bool:
    """
    Fill ID + password and submit. Returns True if submitted successfully.
    Does NOT handle the OTP step — caller must handle that separately.
    """
    def _react_type(selector: str, value: str) -> None:
        """Type like a human — React inputs ignore plain .fill()."""
        el = page.locator(selector).first
        el.click()
        try:
            el.press_sequentially(value, delay=45)
        except AttributeError:
            el.type(value, delay=45)
        try:
            if (el.input_value() or "").strip() != value:
                page.fill(selector, value)
                page.dispatch_event(selector, "input")
                page.dispatch_event(selector, "change")
        except Exception:
            pass

    # Fill ID field
    id_filled = False
    for selector in _ID_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=5000)
            _react_type(selector, id_number)
            id_filled = True
            if logger:
                logger.info(f"[GovLogin] Filled ID with selector: {selector}")
            break
        except Exception:
            continue

    if not id_filled:
        msg = f"{_ts()} [GovLogin] Could not find ID input field."
        print(msg)
        if logger:
            logger.warn(msg)
        return False

    # Fill password field
    pass_filled = False
    for selector in _PASSWORD_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=3000)
            _react_type(selector, password)
            pass_filled = True
            break
        except Exception:
            continue
    if not pass_filled:
        msg = f"{_ts()} [GovLogin] Could not find password field."
        print(msg)
        if logger:
            logger.warn(msg)
        return False

    # Click submit
    submitted = False
    for selector in _SUBMIT_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=5000)
            page.click(selector)
            submitted = True
            if logger:
                logger.info(f"[GovLogin] Clicked submit with selector: {selector}")
            break
        except Exception:
            continue

    if not submitted:
        msg = f"{_ts()} [GovLogin] Could not find submit button."
        print(msg)
        if logger:
            logger.warn(msg)
        return False

    return True


def switch_otp_to_email(page: "Page", logger: "Logger | None" = None) -> bool:
    """
    If on SMS OTP page, click the 'send to email' link to switch to email OTP.
    Returns True if clicked successfully or already on email OTP page.
    """
    if _is_email_otp_page(page):
        if logger:
            logger.info("[GovLogin] Already on email OTP page.")
        return True

    for selector in _EMAIL_LINK_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=5000)
            page.click(selector)
            if logger:
                logger.info(f"[GovLogin] Clicked email OTP link: {selector}")
            return True
        except Exception:
            continue

    msg = "[GovLogin] Could not find 'send to email' link on OTP page."
    print(msg)
    if logger:
        logger.warn(msg)
    return False


def fill_otp_and_submit(
    page: "Page",
    otp_code: str,
    logger: "Logger | None" = None,
) -> bool:
    """Fill OTP code into the input field and click כניסה. Returns True on success.
    Searches the main page AND every iframe — gov.il sometimes renders the OTP
    widget inside a frame, in which case page.locator() on the top frame misses
    it entirely (the real cause of 'as if I clicked nothing')."""
    otp_code = (otp_code or "").strip()
    print(f"{_ts()} [GovLogin] fill_otp: filling code of length {len(otp_code)}")

    # Build the list of frames to search (main first, then children)
    frames = [page]
    try:
        frames += [f for f in page.frames if f != page.main_frame]
    except Exception:
        pass

    filled_frame = None
    for frame in frames:
        for selector in _OTP_INPUT_SELECTORS:
            try:
                loc = frame.locator(selector)
                if loc.count() == 0:
                    continue
                el = loc.first
                if not el.is_visible(timeout=1500):
                    continue
                el.click(click_count=3)          # select any leftover value
                try:
                    frame.page.keyboard.press("Backspace")
                except Exception:
                    pass
                el.type(otp_code, delay=90)
                time.sleep(0.4)
                cur = ""
                try:
                    cur = (el.input_value() or "").strip()
                except Exception:
                    pass
                if cur != otp_code:
                    # React native-setter trick inside this frame
                    frame.evaluate(
                        """([sel, code]) => {
                            const el = document.querySelector(sel);
                            if (!el) return;
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            setter.call(el, code);
                            el.dispatchEvent(new Event('input', {bubbles:true}));
                            el.dispatchEvent(new Event('change', {bubbles:true}));
                        }""",
                        [selector, otp_code],
                    )
                    time.sleep(0.4)
                    try:
                        cur = (el.input_value() or "").strip()
                    except Exception:
                        pass
                print(f"{_ts()} [GovLogin] fill_otp: selector {selector!r} → field now '{cur}'")
                if cur == otp_code:
                    filled_frame = frame
                    if logger:
                        logger.info(f"[GovLogin] OTP filled via {selector} (frame={frame!=page})")
                    break
            except Exception as e:
                print(f"{_ts()} [GovLogin] fill_otp: {selector!r} failed: {str(e)[:80]}")
                continue
        if filled_frame:
            break

    if not filled_frame:
        msg = f"{_ts()} [GovLogin] Could not fill OTP in any frame."
        print(msg)
        if logger:
            logger.warn(msg)
        return False

    time.sleep(0.8)   # button enables only after the state updates

    submitted = False
    for selector in _OTP_SUBMIT_SELECTORS:
        try:
            btn = filled_frame.locator(selector).first
            if btn.count() == 0:
                continue
            btn.click(timeout=4000)
            submitted = True
            print(f"{_ts()} [GovLogin] fill_otp: clicked submit {selector!r}")
            break
        except Exception:
            try:
                filled_frame.locator(selector).first.click(force=True)
                submitted = True
                break
            except Exception:
                continue
    if not submitted:
        # Enter key as a last resort — many OTP forms submit on Enter
        try:
            filled_frame.page.keyboard.press("Enter")
            submitted = True
            print(f"{_ts()} [GovLogin] fill_otp: submitted with Enter")
        except Exception:
            pass

    if not submitted:
        msg = f"{_ts()} [GovLogin] Could not find OTP submit button."
        print(msg)
        if logger:
            logger.warn(msg)
        return False

    return True


def try_passkey_login(
    page: "Page",
    logger: "Logger | None" = None,
    wait_seconds: int = 60,
) -> bool:
    """
    Click the 'כניסה מהירה' (passkey / WebAuthn) button and wait for the user
    to approve on their device (phone, tablet, USB security key).

    This is a MANUAL step — the browser opens a native OS dialog that the user
    must interact with.  This function clicks the button and then waits up to
    ``wait_seconds`` for the page to navigate away from login.gov.il.

    Returns True if login succeeded (page left login.gov.il), False otherwise.

    Usage:
        from core.gov_login import has_passkey_button, try_passkey_login
        if has_passkey_button(page):
            success = try_passkey_login(page, logger=logger)
    """
    _PASSKEY_SELECTORS = [
        "#passkeyContainer",
        "div[id='passkeyContainer']",
        "div:has-text('כניסה מהירה')",
    ]

    clicked = False
    for sel in _PASSKEY_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                el.click()
                clicked = True
                from core.i18n import t as _t
                print(f"{_ts()} [Auth] {_t('passkey_waiting')}")
                if logger:
                    logger.info("[GovLogin] Clicked passkey button — waiting for device approval.")
                break
        except Exception:
            continue

    if not clicked:
        if logger:
            logger.warn("[GovLogin] Passkey button not found or not clickable.")
        return False

    # Wait for the page to navigate away from login.gov.il
    try:
        page.wait_for_url(
            lambda u: "login.gov.il" not in u,
            timeout=wait_seconds * 1000,
        )
        from core.i18n import t as _t
        print(f"{_ts()} [Auth] {_t('passkey_success')}")
        if logger:
            logger.info("[GovLogin] Passkey login succeeded.")
        return True
    except Exception:
        from core.i18n import t as _t
        print(f"{_ts()} [Auth] {_t('passkey_timeout', n=wait_seconds)}")
        if logger:
            logger.warn(f"[GovLogin] Passkey login timed out after {wait_seconds}s.")
        return False


def auto_login_flow(
    page: "Page",
    email_reader=None,
    logger: "Logger | None" = None,
) -> bool:
    """
    Full automated login flow — serialized with a global lock so two portals
    never race through the gov.il OTP phase simultaneously.
    """
    if not is_gov_login_page(page):
        if logger:
            logger.info("[GovLogin] Not on login page — skipping auto_login_flow.")
        return False

    if not _govil_login_lock.acquire(timeout=300):
        msg = "[GovLogin] התחברות אחרת ל-gov.il כבר מתבצעת — לא ניתן להמשיך"
        print(f"{_ts()} {msg}")
        if logger:
            logger.warn(msg)
        return False
    try:
        return _auto_login_flow_inner(page, email_reader, logger)
    finally:
        _govil_login_lock.release()


def _auto_login_flow_inner(
    page: "Page",
    email_reader=None,
    logger: "Logger | None" = None,
) -> bool:
    if not is_gov_login_page(page):
        return False

    from core.credentials import get_credentials

    try:
        id_number, password = get_credentials()
    except Exception as exc:
        msg = f"[GovLogin] Failed to load credentials: {exc}"
        print(msg)
        if logger:
            logger.warn(msg)
        return False


    # Capture email baseline BEFORE submitting so we don't miss a fast OTP
    email_baseline = None
    if email_reader is not None:
        try:
            email_baseline = email_reader.capture_baseline()
            if logger:
                logger.info(f"[GovLogin] Email baseline captured: {email_baseline!r}")
        except Exception as exc:
            if logger:
                logger.warn(f"[GovLogin] Could not capture email baseline: {exc}")

    if not perform_login(page, id_number, password, logger=logger):
        return False

    # Wait up to 10s for OTP page
    otp_appeared = False
    deadline = time.time() + 10
    while time.time() < deadline:
        if is_otp_page(page):
            otp_appeared = True
            break
        time.sleep(0.5)

    if not otp_appeared:
        if logger:
            logger.info("[GovLogin] No OTP page detected after submit — login may be complete.")
        return True

    # OTP delivery method / ערוץ קבלת הקוד:
    #   "email" (default) — auto-switch to email + auto-read from inbox
    #   "sms"             — stay on the PHONE page; the user types the code
    #                       in the LIAS UI or directly on the gov.il page.
    otp_method = "email"
    try:
        from core.download import SESSION_SETTINGS as _ss_m
        otp_method = (_ss_m.get("otp_method") or "email").lower()
    except Exception:
        pass

    if otp_method == "sms":
        # Stay on SMS page — gov.il already sent the code to the phone.
        print(f"{_ts()} [Auth] קוד אימות נשלח לטלפון — הזן אותו ב-UI, "
              f"או ישירות בדף gov.il בחלון הדפדפן.")
        if logger:
            logger.info("[Auth] SMS OTP — waiting for manual code (UI or page).")
        # Allow several attempts — wrong code / not-yet-arrived should not
        # abort the whole login. / כמה נסיונות — קוד שגוי לא מפיל הכול.
        try:
            from LIAS import jobs as _jobs
        except Exception:
            _jobs = None
        deadline = time.time() + 300
        while time.time() < deadline:
            otp_code = ""
            if _jobs is not None:
                _jobs._otp_value = ""
                _jobs._otp_event.clear()
                _jobs.broadcast({"type": "otp_required",
                                 "message": "הזן את הקוד שנשלח לטלפון (SMS)"})
                while time.time() < deadline:
                    if _jobs._otp_event.is_set():
                        otp_code = (_jobs._otp_value or "").strip()
                        break
                    # user may have typed the code straight on the gov.il page
                    if "login.gov.il" not in (page.url or ""):
                        if logger:
                            logger.info("[Auth] OTP entered manually on page — done.")
                        return True
                    time.sleep(1)
            else:
                otp_code = input(f"{_ts()} [Auth] הזן את הקוד שנשלח לטלפון: ").strip()
            if not otp_code:
                continue
            if _jobs is not None:
                _jobs.broadcast({"type": "auth_progress", "message": "מזין קוד אימות"})
            fill_otp_and_submit(page, otp_code, logger=logger)
            time.sleep(2)
            # success = we left login.gov.il / הצלחה = יצאנו מדף ההזדהות
            if "login.gov.il" not in (page.url or ""):
                if logger:
                    logger.ok("[Auth] SMS OTP accepted — logged in.")
                return True
            # still here → wrong/expired code, ask again
            if _jobs is not None:
                _jobs.broadcast({"type": "job",
                                 "message": "הקוד לא התקבל — שלח שוב את הקוד מהטלפון"})
            print(f"{_ts()} [Auth] הקוד לא התקבל — מנסים שוב.")
        print("[GovLogin] תם הזמן להזנת קוד — ההתחברות לא הושלמה.")
        return False

    # email flow (default): if on SMS page, switch to email
    if _is_sms_otp_page(page):
        if not switch_otp_to_email(page, logger=logger):
            return False
        # Wait briefly for email OTP page to load
        time.sleep(1)

    print(f"{_ts()} [Auth] OTP email sent — waiting for code...")
    if logger:
        logger.info("[Auth] OTP email sent — waiting for code.")

    def _ask_otp_terminal() -> str:
        return input(f"{_ts()} [Auth] Please enter OTP from your email: ").strip()

    def _ask_otp_lias() -> str:
        try:
            from LIAS import jobs as _jobs
            return _jobs.request_otp_from_ui(timeout=180)
        except Exception:
            return _ask_otp_terminal()

    def _ask_otp() -> str:
        try:
            from core.download import SESSION_SETTINGS as _ss
            if _ss.get("lias_mode"):
                return _ask_otp_lias()
        except Exception:
            pass
        return _ask_otp_terminal()

    # Google Authenticator (TOTP): if the user configured a TOTP secret and
    # chose it as the OTP source, generate the code locally — instant, no email
    # delivery lag. Falls through to email/manual if it isn't set up.
    otp_code = ""
    try:
        from core.download import SESSION_SETTINGS as _ss
        from core.totp import totp_configured, totp_now, get_totp_secret
        if _ss.get("otp_source") == "totp" and totp_configured():
            otp_code = totp_now(get_totp_secret())
            print(f"{_ts()} [Auth] קוד מאפליקציית האימות (Google Authenticator) הופק")
            if logger:
                logger.info("[GovLogin] Using Google Authenticator (TOTP) code")
    except Exception as _te:
        print(f"{_ts()} [Auth] TOTP לא זמין ({_te}) — עובר ל-OTP במייל")

    if not otp_code and email_reader is not None:
        try:
            # gov.il OTP email can take a while to arrive — wait up to 120s.
            otp_code = email_reader.wait_for_otp(timeout_seconds=120,
                                                 baseline=email_baseline)
        except Exception as exc:
            msg = f"[GovLogin] EmailOTPReader error: {exc}"
            print(msg)
            if logger:
                logger.warn(msg)
            otp_code = _ask_otp()
    elif not otp_code:
        otp_code = _ask_otp()

    if not otp_code:
        msg = "[GovLogin] No OTP code provided."
        print(msg)
        if logger:
            logger.warn(msg)
        return False

    return fill_otp_and_submit(page, otp_code, logger=logger)
