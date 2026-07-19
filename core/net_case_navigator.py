"""NET HaMishpat — navigate to a specific case by number."""
from __future__ import annotations
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from core.logger import Logger

def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

def parse_net_case_number(name: str) -> tuple[str, str] | None:
    """Extract (case_number, MMYY) from a NET case name or folder name.

    Examples:
      "תמ\"ש 50123-06-24 פלוני נ. פלמוני" -> ("50123", "0624")
      "50123-06-24" -> ("50123", "0624")
    Returns None if no match.
    """
    m = re.search(r'(\d{3,6})-(\d{2})-(\d{2})', name)
    if m:
        result = (m.group(1), m.group(2) + m.group(3))
        print(f"{_ts()} [CaseNav] parse_net_case_number('{name}')")
        print(f"{_ts()} [CaseNav]   regex match: groups={m.groups()}")
        print(f"{_ts()} [CaseNav]   → case_number='{result[0]}', mmyy='{result[1]}'")
        return result
    print(f"{_ts()} [CaseNav] parse_net_case_number('{name}') → no match")
    return None

def navigate_to_case_by_number(
    page: "Page",
    case_number: str,
    month_year: str,
    logger: "Logger | None" = None,
) -> bool:
    """Fill the NET portal header search fields and click אתר.

    Selectors (from portal HTML):
      Month-year: #Header1_CaseLocatorHeaderUC2_BamaMonthYearTextBoxHT  (MMYY, e.g. "0624")
      Case number: #Header1_CaseLocatorHeaderUC2_BamaCaseNumberTextBoxHT
      Submit: #Header1_CaseLocatorHeaderUC2_SearchHeaderCaseButton

    Returns True if the click was issued successfully.
    """
    import time

    def _log(msg: str, level: str = "info") -> None:
        print(f"{_ts()} [CaseNav] {msg}")
        if logger:
            getattr(logger, level, logger.info)(f"[CaseNav] {msg}")

    def _dismiss_popup() -> None:
        """Dismiss אישור / error / cookie popup if present."""
        for sel in [
            'a.modal_ReturnMessageClose',
            'a.modal_close2',
            '#MessageLS_CaseNotFoundORNotAllowed a',
            'button:has-text("אישור")',
            'input[type="button"][value="אישור"]',
            'a:has-text("אישור")',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1500):
                    btn.click()
                    time.sleep(0.5)
                    return
            except Exception:
                continue

    # The secured portal uses 'header_' prefix; the public homepage may use 'Header1_'.
    # Probe at runtime so navigation works regardless of which portal variant is active.
    def _resolve_sel(base_id: str) -> str:
        for prefix in ("header_", "Header1_"):
            try:
                sel = f"#{prefix}{base_id}"
                if page.locator(sel).count() > 0:
                    return sel
            except Exception:
                pass
        return f"#header_{base_id}"  # best-guess default

    _BASE_MY  = "CaseLocatorHeaderUC2_BamaMonthYearTextBoxHT"
    _BASE_NUM = "CaseLocatorHeaderUC2_BamaCaseNumberTextBoxHT"
    _BASE_BTN = "CaseLocatorHeaderUC2_SearchHeaderCaseButton"

    my_sel  = _resolve_sel(_BASE_MY)
    num_sel = _resolve_sel(_BASE_NUM)
    btn_sel = _resolve_sel(_BASE_BTN)

    def _fill_via_js(css_selector: str, value: str) -> None:
        """Set a field value via JS using the resolved CSS selector (e.g. '#header_...')."""
        # Strip leading '#' to get just the id string
        elem_id = css_selector.lstrip("#")
        js = f"""
            (function() {{
                var el = document.getElementById('{elem_id}');
                if (!el) {{ console.warn('fill_via_js: element not found: {elem_id}'); return; }}
                var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeInputValueSetter.call(el, '{value}');
                el.dispatchEvent(new Event('input',  {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }})();
        """
        try:
            page.evaluate(js)
        except Exception:
            pass

    def _fill_field(selector: str, value: str, timeout: int = 8000) -> None:
        """Click into a field, clear it completely, then type the value."""
        page.wait_for_selector(selector, state="visible", timeout=timeout)
        page.click(selector)
        time.sleep(0.15)
        # Select all + delete (no triple_click — only click/dblclick exist on Locator)
        page.keyboard.press("Control+a")
        time.sleep(0.1)
        page.keyboard.press("Delete")
        time.sleep(0.1)
        page.keyboard.type(value, delay=70)

    # Keep MMYY format (no dash) — the portal auto-formats as MM-YY
    # Strip any dash that may have been added by earlier normalisation
    month_year_raw = month_year.replace("-", "")  # e.g. "0422"

    _UNREAD = ""  # sentinel — means we could not confirm the value

    def _read_field_value(selector: str) -> str:
        """Read current value of a field. Returns '' on any failure (not a sentinel string)."""
        try:
            return page.locator(selector).input_value(timeout=2000) or ""
        except Exception:
            return _UNREAD

    try:
        print(f"\n{'='*60}")
        print(f"{_ts()} [CaseNav] ── CASE NAVIGATION DEBUG ──")
        print(f"{_ts()} [CaseNav]   Input  case_number : '{case_number}'")
        print(f"{_ts()} [CaseNav]   Input  month_year  : '{month_year}' (raw param)")
        print(f"{_ts()} [CaseNav]   Processed MMYY     : '{month_year_raw}' (after strip dash)")
        print(f"{'='*60}\n")

        # Navigate to the secured portal's "my cases" page where the
        # case locator header fields are always visible.
        # PersonalAreaPage hides them in a dropdown; case detail pages
        # have them in DOM but not interactive. The my-cases page works.
        _SECURED_HOME = "https://securesso.court.gov.il/Ngcs.Web.Secured/PersonalAreaPage.aspx"
        _log(f"Navigating to secured portal before entering case {case_number}/{month_year_raw}...")
        try:
            page.goto(_SECURED_HOME, wait_until="networkidle", timeout=20000)
        except Exception:
            try:
                page.goto(_SECURED_HOME, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                time.sleep(3)

        # Open "my cases" page so the case locator header becomes visible
        try:
            from core.net_search_cases import navigate_to_my_cases
            navigate_to_my_cases(page)
        except Exception as nav_exc:
            _log(f"  Could not navigate to my-cases: {nav_exc}")

        # Re-resolve selectors (secured portal uses 'header_' prefix)
        my_sel  = _resolve_sel(_BASE_MY)
        num_sel = _resolve_sel(_BASE_NUM)
        btn_sel = _resolve_sel(_BASE_BTN)

        # Force-show the case locator if still hidden
        try:
            page.evaluate("""
                ['header_CaseLocatorHeaderUC2_BamaMonthYearTextBoxHT',
                 'Header1_CaseLocatorHeaderUC2_BamaMonthYearTextBoxHT'].forEach(id => {
                    var el = document.getElementById(id);
                    if (!el) return;
                    var node = el;
                    while (node && node !== document.body) {
                        var s = window.getComputedStyle(node);
                        if (s.display === 'none') node.style.setProperty('display', '', 'important');
                        if (s.visibility === 'hidden') node.style.setProperty('visibility', 'visible', 'important');
                        node = node.parentElement;
                    }
                });
            """)
            time.sleep(0.5)
        except Exception:
            pass

        # Wait for the search fields to become visible
        try:
            page.wait_for_selector(my_sel, state="visible", timeout=10000)
        except Exception:
            _log("  Case locator fields not visible — will try keyboard fill")
            time.sleep(2)
        time.sleep(0.8)

        # Dismiss terms/validation popup if present
        _dismiss_popup()
        time.sleep(0.4)

        # Click month-year field to ensure it's focused/active
        try:
            page.click(my_sel, timeout=3000)
            time.sleep(0.3)
        except Exception:
            pass

        _log(f"  Selectors resolved: my={my_sel!r}  num={num_sel!r}  btn={btn_sel!r}")

        # ── Fill month-year ──
        # JS fill with retry; keyboard fill as final fallback.
        val_after_js = ""
        for attempt in range(3):
            _log(f"  [1] month-year: sending '{month_year_raw}' via JS fill (attempt {attempt+1})...")
            _fill_via_js(my_sel, month_year_raw)
            time.sleep(0.4)
            val_after_js = _read_field_value(my_sel)
            _log(f"      → field value after JS fill: '{val_after_js}'")
            if val_after_js.replace("-", ""):
                break
            # Field not ready — click it and wait before retry
            try:
                page.click(my_sel, timeout=2000)
            except Exception:
                pass
            time.sleep(1.0)
        if not val_after_js.replace("-", ""):
            _log(f"  [2] month-year: JS fill gave empty after retries — falling back to keyboard fill...")
            _fill_field(my_sel, month_year_raw)
            time.sleep(0.3)
            val_after_kb = _read_field_value(my_sel)
            _log(f"      → field value after keyboard fill: '{val_after_kb}'")
        else:
            _log(f"  [2] month-year: JS fill OK ('{val_after_js}') — skipping keyboard fill.")

        # ── Fill case number ──
        val_num_after_js = ""
        for attempt in range(3):
            _log(f"  [3] case-number: sending '{case_number}' via JS fill (attempt {attempt+1})...")
            _fill_via_js(num_sel, case_number)
            time.sleep(0.4)
            val_num_after_js = _read_field_value(num_sel)
            _log(f"      → field value after JS fill: '{val_num_after_js}'")
            if val_num_after_js:
                break
            try:
                page.click(num_sel, timeout=2000)
            except Exception:
                pass
            time.sleep(1.0)
        if not val_num_after_js:
            _log(f"  [4] case-number: JS fill gave empty after retries — falling back to keyboard fill...")
            _fill_field(num_sel, case_number)
            time.sleep(0.3)
            val_num_after_kb = _read_field_value(num_sel)
            _log(f"      → field value after keyboard fill: '{val_num_after_kb}'")
        else:
            _log(f"  [4] case-number: JS fill OK ('{val_num_after_js}') — skipping keyboard fill.")

        # Final state of both fields before clicking
        final_my  = _read_field_value(my_sel)
        final_num = _read_field_value(num_sel)
        print(f"\n{_ts()} [CaseNav] ── FINAL FIELD VALUES BEFORE SUBMIT ──")
        print(f"{_ts()} [CaseNav]   month-year  field: '{final_my}'")
        print(f"{_ts()} [CaseNav]   case-number field: '{final_num}'")
        print(f"{_ts()} [CaseNav]   expected: month-year='{month_year_raw}', case-number='{case_number}'\n")

        # Dismiss any validation popup that appeared during fill
        _dismiss_popup()
        time.sleep(0.2)

        # ── Click אתר (search) ──
        page.wait_for_selector(btn_sel, state="visible", timeout=5000)
        page.click(btn_sel)
        _log("  → clicked search button")

        # Wait for page to load the case
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            time.sleep(2)
        except Exception:
            time.sleep(3)

        # Check for "שגיאה במספר תיק" error popup
        try:
            err_el = page.locator('#MessageLS_CaseNotFoundORNotAllowed, div:has-text("שגיאה במספר תיק")').first
            if err_el.count() > 0 and err_el.is_visible(timeout=1500):
                _log(f"Portal showed 'שגיאה במספר תיק' for {case_number}/{month_year_raw} — dismissing", "warn")
                _dismiss_popup()
                return False
        except Exception:
            pass

        # Verify we are now on the SECURED portal (not stayed on public homepage)
        landed_url = page.url or ""
        if "securesso.court.gov.il" not in landed_url and "court.gov.il" in landed_url:
            # May be on public portal — check if case page loaded anyway
            if "CaseView" not in landed_url and "PersonalAreaPage" not in landed_url:
                _log(
                    f"After search, still on portal ({landed_url[:60]}) — "
                    "case might not exist or portal redirected.",
                    "warn",
                )
                _dismiss_popup()
                return False

        _log(f"Navigated to case {case_number}/{month_year_raw}.")
        return True
    except Exception as e:
        _log(f"Failed to navigate to case {case_number}/{month_year}: {e}", "error")
        return False

def prompt_case_number_input() -> tuple[str, str] | None:
    """Ask user to enter a NET case number.
    Expected format: NUMBER-MMYY (e.g. 50123-0624) or MM/YY-NUMBER.
    Returns (case_number, MMYY) or None if skipped.
    """
    print(f"\n{_ts()} [CaseNav] Enter case number to navigate directly.")
    print("  Format: NUMBER-MMYY  (e.g. 50123-0624 for case 50123, June 2024)")
    print("  Or press Enter to navigate manually in the browser.")

    raw = input("Case number [Enter to skip]: ").strip()
    if not raw:
        return None

    # Try to parse
    result = parse_net_case_number(raw)
    if result:
        return result

    # Try alternate format NUMBER-MMYY directly
    m = re.match(r'^(\d{3,6})-(\d{4})$', raw)
    if m:
        return m.group(1), m.group(2)

    print(f"{_ts()} [CaseNav] Could not parse case number from '{raw}' — navigating manually.")
    return None
