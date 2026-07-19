#!/usr/bin/env python3
"""
ECA (הוצאה לפועל) downloader
=============================
מוריד את כל מסמכי הבקשות וההחלטות מהוצאה לפועל (publicsso.eca.gov.il).

מבנה תיקיות:
  הוצאה לפועל/
    {תיק}/
      {הליך}/
        {הליך} - {תאריך} - בקשה - {מגיש}.pdf
        {הליך} - {תאריך_החלטה} - החלטה - {מגיש}.pdf

הפעלה:
  python eca_download.py [--output ./הוצאה_לפועל] [--profile ./browser_profile]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_URL = "https://publicsso.eca.gov.il"
OPEN_CASES_URL = f"{BASE_URL}/he/home/OpenCase"


def _ts() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def _log(msg: str) -> None:
    print(f"{_ts()} {msg}", flush=True)


def _sanitize(name: str) -> str:
    """Remove filesystem-illegal characters."""
    return re.sub(r'[\\/:*?"<>|]', "_", name.strip())


def _make_filename(process: str, date: str, doc_type: str, applicant: str) -> str:
    """Build the document filename."""
    parts = [p for p in [process, date, doc_type, applicant] if p]
    return _sanitize(" - ".join(parts)) + ".pdf"


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------

def _launch_browser(profile_dir: Optional[Path]):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    if profile_dir:
        profile_dir.mkdir(parents=True, exist_ok=True)
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
            accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
    else:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = browser.new_context(accept_downloads=True, no_viewport=True)
        page = ctx.new_page()
    return pw, ctx, page


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _wait_for_eca_home(page, timeout: int = 120) -> bool:
    """Wait until we land on an authenticated ECA page (not login.gov.il)."""
    _log("ממתין להתחברות ל-ECA…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        _click_system_choice(page)
        url = page.url or ""
        if "publicsso.eca.gov.il" in url and "login.gov.il" not in url:
            # Check that Angular has rendered (carousel or main content present)
            try:
                page.wait_for_selector("#carousel-cases, app-root", timeout=5000)
                _log("✓ מחובר ל-ECA")
                return True
            except Exception:
                pass
        time.sleep(1.5)
    return False


def _click_system_choice(page) -> None:
    """If a system-selection page appears (בד"ר נט / הוצאה לפועל), pick הוצאה לפועל."""
    _ECA_CHOICE_SELECTORS = [
        'button:has-text("הוצאה לפועל")',
        'a:has-text("הוצאה לפועל")',
        'div:has-text("הוצאה לפועל") >> visible=true',
        'input[value*="הוצאה לפועל"]',
    ]
    for sel in _ECA_CHOICE_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                el.click()
                _log("✓ נבחרה מערכת: הוצאה לפועל")
                time.sleep(2)
                return
        except Exception:
            continue


def _login_eca(page) -> bool:
    """Navigate to ECA and authenticate via the unified login chain
    (core.connection.ensure_logged_in — same mechanism as NET/BDR)."""
    try:
        from core.connection import ensure_logged_in
        if ensure_logged_in(page, "ECA"):
            return True
    except Exception as e:
        _log(f"ensure_logged_in נכשל ({e}) — עובר למסלול עצמאי")

    # Standalone fallback (no LIAS engine available)
    page.goto(OPEN_CASES_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)

    url = page.url or ""

    # Already authenticated
    if "publicsso.eca.gov.il" in url and "login.gov.il" not in url:
        _log("✓ כבר מחובר")
        return True

    _click_system_choice(page)
    url = page.url or ""

    # On login.gov.il — try auto-login if credentials available
    if "login.gov.il" in url:
        try:
            from core.gov_login import auto_login_flow
            from core.email_otp import EmailOTPReader
            try:
                email_reader = EmailOTPReader()
            except Exception:
                email_reader = None
            auto_login_flow(page, email_reader=email_reader)
        except Exception as e:
            _log(f"auto_login_flow לא זמין: {e}")

    # Wait for user to complete login manually if needed
    return _wait_for_eca_home(page, timeout=180)


# ---------------------------------------------------------------------------
# Case list
# ---------------------------------------------------------------------------

def _extract_cases(page) -> list[dict]:
    """Extract all open cases from the carousel."""
    _log("מחלץ רשימת תיקים…")
    try:
        page.wait_for_selector("#carousel-cases mat-card", timeout=15000)
    except Exception:
        _log("⚠ לא נמצא קרוסל תיקים")
        return []

    cards = page.query_selector_all("#carousel-cases mat-card")
    cases = []
    for card in cards:
        card_id = card.get_attribute("id") or ""  # e.g. card-case-529310-10-24
        m = re.match(r"card-case-(.+)", card_id)
        if not m:
            continue
        case_num = m.group(1)

        # Extract subtitle (type), role, and the party name shown on the card
        try:
            subtitle = card.query_selector("mat-card-subtitle")
            case_type = subtitle.inner_text().strip() if subtitle else ""
        except Exception:
            case_type = ""

        try:
            role_el = card.query_selector(".col.text-start p.bold")
            role = role_el.inner_text().strip() if role_el else ""
        except Exception:
            role = ""

        # The other party's name (client grouping key) — the bold span inside
        # the card content, e.g. "הבנק הבינלאומי..." or "דוד פונברשטיין"
        try:
            party_el = card.query_selector("mat-card-content span.bold.mat-mdc-tooltip-trigger, mat-card-content span.mat-mdc-tooltip-trigger")
            party = party_el.inner_text().strip() if party_el else ""
        except Exception:
            party = ""

        cases.append({"number": case_num, "type": case_type, "role": role, "party": party})
        _log(f"  תיק: {case_num} | {case_type} | {role} | {party}")

    _log(f"נמצאו {len(cases)} תיקים")
    return cases


# ---------------------------------------------------------------------------
# Navigate to case and open בקשות והחלטות tab
# ---------------------------------------------------------------------------

def _open_motions_tab(page, case_num: str) -> bool:
    """Navigate to case info page and click the 'בקשות והחלטות' tab."""
    url = f"{BASE_URL}/he/caseinfo/{case_num}"
    _log(f"  ניווט לתיק {case_num}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)

    # Click the MotionDecisionTable tab
    try:
        tab = page.locator("#MotionDecisionTable").first
        tab.wait_for(state="visible", timeout=10000)
        tab.click()
        _log("  ✓ לחץ על 'בקשות והחלטות'")
        time.sleep(2)
        return True
    except Exception as e:
        _log(f"  ⚠ לא מצא טאב 'בקשות והחלטות': {e}")
        return False


# ---------------------------------------------------------------------------
# Paginator — set to show max items
# ---------------------------------------------------------------------------

def _maximize_paginator(page) -> None:
    """Change items-per-page selector to the maximum available option."""
    try:
        # Click the mat-select for page size
        page.locator("mat-paginator mat-select").first.click()
        time.sleep(0.8)
        # Pick the largest option (last in the list)
        options = page.locator("mat-option").all()
        if options:
            options[-1].click()
            time.sleep(1.5)
            _log("  ✓ הגדיל את הפגינטור למקסימום")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Row extraction
# ---------------------------------------------------------------------------

def _cell_text(row, col_class: str) -> str:
    try:
        cell = row.query_selector(f"td.{col_class}")
        if not cell:
            return ""
        div = cell.query_selector(".general-text")
        return (div.inner_text() if div else cell.inner_text()).strip()
    except Exception:
        return ""


def _has_button(row, selector: str) -> bool:
    try:
        btn = row.query_selector(selector)
        return btn is not None and btn.is_visible()
    except Exception:
        return False


def _extract_rows(page) -> list[dict]:
    """Extract all data rows from the current table."""
    rows = page.query_selector_all("table mat-mdc-row, table tr[mat-row], mat-row, tr[role='row']")
    if not rows:
        # Fallback — try Angular mat-row
        rows = page.query_selector_all("[role='row']:not([role='columnheader'])")
    result = []
    for row in rows:
        proc = _cell_text(row, "mat-column-processNumber")
        if not proc:
            continue
        result.append({
            "process":      proc,
            "name":         _cell_text(row, "mat-column-motionDecisionName"),
            "applicant":    _cell_text(row, "mat-column-motionApplicant"),
            "date":         _cell_text(row, "mat-column-motionOpenDate"),
            "dec_date":     _cell_text(row, "mat-column-decisionDate"),
            "dec_result":   _cell_text(row, "mat-column-decisionResultName"),
            "has_motion":   _has_button(row, "#motionDocumentID"),
            "has_decision": _has_button(row, "#decisionDocumentID"),
            "has_childs":   _has_button(row, "#hasChilds"),
            "_row":         row,
        })
    return result


# ---------------------------------------------------------------------------
# Process a single case
# ---------------------------------------------------------------------------

def _process_case(page, case: dict, output_dir: Path, by_client: bool = False) -> None:
    case_num = case["number"]
    if by_client:
        # downloads/{לקוח}/הוצאה לפועל/{תיק}/... — mirrors the NET/BDR layout
        client = _sanitize(case.get("party") or "ללא שם")
        case_dir = output_dir / client / "הוצאה לפועל" / _sanitize(case_num)
    else:
        case_dir = output_dir / _sanitize(case_num)

    _log(f"\n{'='*60}")
    _log(f"תיק: {case_num}  ({case.get('type', '')})")

    if not _open_motions_tab(page, case_num):
        _log(f"  ✗ דילוג על תיק {case_num}")
        return

    _maximize_paginator(page)
    time.sleep(1)

    # Expand all parent rows (hasChilds) first
    _expand_all_rows(page)

    rows_data = _extract_rows(page)
    _log(f"  {len(rows_data)} שורות נמצאו")

    for rd in rows_data:
        proc = rd["process"]
        applicant = _sanitize(rd["applicant"] or "")
        date = _sanitize(rd["date"] or "")
        dec_date = _sanitize(rd["dec_date"] or "")

        # Folder per top-level process (e.g. "20" not "20.1")
        top_proc = proc.split(".")[0]
        proc_dir = case_dir / _sanitize(top_proc)

        row_el = rd["_row"]

        # Download motion (בקשה)
        if rd["has_motion"]:
            filename = _make_filename(proc, date, "בקשה", applicant)
            save_path = proc_dir / filename
            _log(f"    [{proc}] בקשה ({date}) — {filename}")
            _download_doc_from_row(page, row_el, "motionDocumentID", save_path)

        # Download decision (החלטה)
        if rd["has_decision"]:
            filename = _make_filename(proc, dec_date, "החלטה", applicant)
            save_path = proc_dir / filename
            _log(f"    [{proc}] החלטה ({dec_date}) — {filename}")
            _download_doc_from_row(page, row_el, "decisionDocumentID", save_path)

        time.sleep(0.3)


def _expand_all_rows(page) -> None:
    """Click all 'hasChilds' (zoom_in) buttons to expand parent rows."""
    try:
        btns = page.locator("#hasChilds").all()
        if not btns:
            return
        _log(f"  פותח {len(btns)} שורות אב…")
        for btn in btns:
            try:
                if btn.is_visible(timeout=1000):
                    btn.click()
                    time.sleep(0.5)
            except Exception:
                pass
        time.sleep(1)
    except Exception:
        pass


def _download_doc_from_row(page, row_el, btn_id: str, save_path: Path) -> bool:
    """Click document button on a specific DOM row element."""
    if save_path.exists():
        _log(f"      ↩ כבר קיים: {save_path.name}")
        return True

    save_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        btn = row_el.query_selector(f"#{btn_id}")
        if not btn or not btn.is_visible():
            return False

        # Try download intercept first
        try:
            with page.expect_download(timeout=8000) as dl_info:
                btn.click()
            dl = dl_info.value
            dl.save_as(str(save_path))
            _log(f"      ✓ {save_path.name}")
            return True
        except Exception:
            pass

        # Try new tab
        try:
            with page.context.expect_page(timeout=8000) as pg_info:
                btn.click()
            new_tab = pg_info.value
            time.sleep(2)
            new_url = new_tab.url
            if new_url and new_url != "about:blank":
                import requests
                cookies = {c["name"]: c["value"] for c in page.context.cookies()}
                resp = requests.get(new_url, cookies=cookies, timeout=30,
                                    headers={"Referer": page.url})
                if resp.ok and len(resp.content) > 500:
                    save_path.write_bytes(resp.content)
                    _log(f"      ✓ {save_path.name}")
                    try:
                        new_tab.close()
                    except Exception:
                        pass
                    return True
            try:
                new_tab.close()
            except Exception:
                pass
        except Exception:
            pass

    except Exception as e:
        _log(f"      ✗ {btn_id}: {e}")

    _log(f"      ✗ לא הורד: {save_path.name}")
    return False


# ---------------------------------------------------------------------------
# Sync entry point (called from LIAS jobs on an already-open page)
# ---------------------------------------------------------------------------

def run_eca_download(page, root_output_dir: Path, cases_filter: list[str] | None = None) -> str:
    """Download all ECA cases into root_output_dir/downloads/{client}/הוצאה לפועל/{case}.

    Assumes the caller provides a Playwright page; handles ECA login itself
    (reuses gov.il auto-login). Returns a short summary string.
    """
    downloads_dir = Path(root_output_dir) / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    if not _login_eca(page):
        raise RuntimeError("ההתחברות להוצאה לפועל נכשלה")

    if "/home/OpenCase" not in (page.url or ""):
        page.goto(OPEN_CASES_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

    cases = _extract_cases(page)
    if cases_filter:
        wanted = set(cases_filter)
        cases = [c for c in cases if c["number"] in wanted]
    if not cases:
        return "לא נמצאו תיקי הוצאה לפועל"

    ok = 0
    for case in cases:
        try:
            _process_case(page, case, downloads_dir, by_client=True)
            ok += 1
        except Exception as e:
            _log(f"✗ שגיאה בתיק {case['number']}: {e}")
        time.sleep(1)
    return f"הוצל\"פ: {ok}/{len(cases)} תיקים סונכרנו"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="הורד מסמכים מהוצאה לפועל")
    parser.add_argument("--output", default="./הוצאה_לפועל",
                        help="תיקיית יעד (ברירת מחדל: ./הוצאה_לפועל)")
    parser.add_argument("--profile", default="./browser_profile",
                        help="נתיב לפרופיל דפדפן (ברירת מחדל: ./browser_profile)")
    parser.add_argument("--cases", nargs="*",
                        help="מספרי תיקים ספציפיים (אופציונלי — אחרת יורד הכול)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(args.profile) if args.profile else None

    _log(f"מתחיל הורדת מסמכי הוצאה לפועל → {output_dir.resolve()}")

    pw, ctx, page = _launch_browser(profile_dir)

    try:
        if not _login_eca(page):
            _log("✗ ההתחברות נכשלה — בדוק את הדפדפן ונסה שוב")
            sys.exit(1)

        # Make sure we're on the open cases page
        if "/home/OpenCase" not in (page.url or ""):
            page.goto(OPEN_CASES_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

        cases = _extract_cases(page)
        if not cases:
            _log("✗ לא נמצאו תיקים")
            sys.exit(1)

        # Filter by specific cases if requested
        if args.cases:
            requested = set(args.cases)
            cases = [c for c in cases if c["number"] in requested]
            _log(f"מסנן לתיקים: {[c['number'] for c in cases]}")

        _log(f"\nמעבד {len(cases)} תיקים…\n")

        for case in cases:
            try:
                _process_case(page, case, output_dir, by_client=True)
            except Exception as e:
                _log(f"✗ שגיאה בתיק {case['number']}: {e}")
            time.sleep(1)

        _log(f"\n{'='*60}")
        _log(f"✓ הסתיים! מסמכים נשמרו ב: {output_dir.resolve()}")

    finally:
        input("\nלחץ Enter לסגירת הדפדפן…")
        try:
            ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
