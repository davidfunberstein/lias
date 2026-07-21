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
import json
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

# live download counters (broadcast to the UI tasks balloon)
_STATS = {"done": 0, "errors": 0, "total": 0, "case_idx": 0, "cases_total": 0,
          "t0": 0.0}


def _stats_tick(status: str) -> None:
    if status == "ok":
        _STATS["done"] += 1
    elif status == "fail":
        _STATS["errors"] += 1
    try:
        from LIAS import jobs as _jobs
        mins = max((time.time() - _STATS["t0"]) / 60.0, 0.05)
        _jobs.broadcast({"type": "download_stats", "portal": "ECA",
                         "done": _STATS["done"], "errors": _STATS["errors"],
                         # ECA total isn't known upfront (discovered per case) —
                         # report case progress as the primary metric.
                         "case_idx": _STATS["case_idx"],
                         "cases_total": _STATS["cases_total"],
                         "rate": round(_STATS["done"] / mins, 1)})
    except Exception:
        pass
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
    """Collect ALL rows from the motions table. The table may virtual-scroll,
    so we scroll through it repeatedly and merge rows by process number until
    no new rows appear. Only plain data is kept — row handles go stale after
    scrolling, so downloads re-locate each row fresh by its process number."""
    collected: dict = {}

    def _grab() -> int:
        new = 0
        rows = page.query_selector_all("tr[mat-row], tr.mat-mdc-row, tr[role='row']")
        for row in rows:
            proc = _cell_text(row, "mat-column-processNumber")
            if not proc:
                continue
            key = (proc, _cell_text(row, "mat-column-motionOpenDate"),
                   _cell_text(row, "mat-column-motionDecisionName"))
            if key in collected:
                continue
            collected[key] = {
                "process":      proc,
                "name":         _cell_text(row, "mat-column-motionDecisionName"),
                "applicant":    _cell_text(row, "mat-column-motionApplicant"),
                "date":         _cell_text(row, "mat-column-motionOpenDate"),
                "dec_date":     _cell_text(row, "mat-column-decisionDate"),
                "dec_result":   _cell_text(row, "mat-column-decisionResultName"),
                "has_motion":   _has_button(row, "#motionDocumentID"),
                "has_decision": _has_button(row, "#decisionDocumentID"),
            }
            new += 1
        return new

    _grab()
    stale_rounds = 0
    for _ in range(60):
        try:
            rows = page.query_selector_all("tr[mat-row], tr.mat-mdc-row")
            if rows:
                rows[-1].scroll_into_view_if_needed(timeout=2000)
            page.mouse.wheel(0, 800)
        except Exception:
            pass
        time.sleep(0.6)
        if _grab() == 0:
            stale_rounds += 1
            if stale_rounds >= 3:
                break
        else:
            stale_rounds = 0
    return list(collected.values())


def _row_locator(page, proc: str, date: str):
    """Fresh locator for the row whose processNumber cell is exactly `proc`
    (and, when given, whose open-date matches — disambiguates duplicates)."""
    import re as _re
    base = page.locator("tr[mat-row], tr.mat-mdc-row").filter(
        has=page.locator("td.mat-column-processNumber .general-text",
                         has_text=_re.compile(rf"^\s*{_re.escape(proc)}\s*$")))
    if date:
        with_date = base.filter(
            has=page.locator("td.mat-column-motionOpenDate .general-text",
                             has_text=_re.compile(rf"^\s*{_re.escape(date)}\s*$")))
        if with_date.count() > 0:
            return with_date.first
    return base.first


# ---------------------------------------------------------------------------
# Viewer dialog handling
# ---------------------------------------------------------------------------

def _close_viewer(page, wait_s: float = 6.0) -> None:
    """Close any open document-viewer / error dialog and WAIT until it is gone
    — an open overlay blocks every next click (the 'decision never downloads'
    bug). Handles the pdf.js viewer and the "המסמך אינו זמין" error popup."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            if page.locator("mat-dialog-container, .cdk-overlay-pane .mdc-dialog").count() == 0:
                return
        except Exception:
            return
        for sel in ('mat-dialog-container button[aria-label*="סגור"]',
                    'mat-dialog-container button:has-text("סגירה")',
                    'mat-dialog-container button:has-text("אישור")',
                    'mat-dialog-container mat-icon:has-text("close")',
                    '.cdk-overlay-pane button[aria-label*="Close"]'):
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=300):
                    el.click()
                    time.sleep(0.5)
                    break
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.5)


def _is_unavailable_dialog(page) -> bool:
    """True if the current dialog says the document is unavailable."""
    try:
        dlg = page.locator("mat-dialog-container, .cdk-overlay-pane")
        if dlg.count() == 0:
            return False
        txt = dlg.first.inner_text(timeout=1000) or ""
        return any(k in txt for k in ("אינו זמין", "לא זמין", "לא ניתן להציג"))
    except Exception:
        return False


def _extract_file_from_json(raw: bytes) -> bytes:
    """Find a base64-encoded PDF/Office file anywhere inside a JSON payload
    (the ECA API returns the document like this via GetPermittedDocument)."""
    import base64
    import json as _json
    try:
        obj = _json.loads(raw)
    except Exception:
        return b""
    found: list[bytes] = []

    def _walk(v):
        if isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, list):
            for x in v:
                _walk(x)
        elif isinstance(v, str) and len(v) > 1000:
            try:
                d = base64.b64decode(v, validate=False)
                if d[:4] == b"%PDF" or d[:2] == b"PK":
                    found.append(d)
            except Exception:
                pass

    _walk(obj)
    return found[0] if found else b""


# ---------------------------------------------------------------------------
# Document download
# ---------------------------------------------------------------------------

def _download_doc(page, row_loc, btn_id: str, save_path: Path) -> str:
    """Click a document button on a freshly-located row and capture the file.
    Returns 'ok' / 'missing' (unavailable — dismissed, safe to continue) /
    'fail'."""
    if save_path.exists():
        _log(f"      ↩ כבר קיים: {save_path.name}")
        return "ok"
    save_path.parent.mkdir(parents=True, exist_ok=True)

    btn = row_loc.locator(f"#{btn_id}").first
    try:
        if btn.count() == 0 or not btn.is_visible(timeout=1500):
            return "fail"
    except Exception:
        return "fail"

    pdf_responses: list = []

    def _on_response(resp):
        try:
            ct = (resp.headers or {}).get("content-type", "")
            if "pdf" in ct or "octet-stream" in ct or \
               "GetPermittedDocument" in (resp.url or ""):
                pdf_responses.append(resp)
        except Exception:
            pass

    page.on("response", _on_response)
    try:
        _close_viewer(page, wait_s=3)          # make sure nothing blocks us
        try:
            row_loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            with page.expect_download(timeout=6000) as dl_info:
                btn.click()
            dl_info.value.save_as(str(save_path))
            _log(f"      ✓ {save_path.name}")
            return "ok"
        except Exception:
            pass

        # Wait for the API response that carries the document
        deadline = time.time() + 12
        while time.time() < deadline and not pdf_responses:
            if _is_unavailable_dialog(page):
                _log(f"      ⚠ המסמך אינו זמין — מדלג: {save_path.name}")
                return "missing"
            time.sleep(0.5)
        if pdf_responses:
            try:
                data = pdf_responses[-1].body()
                if data[:1] in (b"{", b"["):
                    data = _extract_file_from_json(data)
                if data and len(data) > 500:
                    save_path.write_bytes(data)
                    _log(f"      ✓ {save_path.name}")
                    return "ok"
            except Exception as e:
                _log(f"      ⚠ network body: {e}")
        if _is_unavailable_dialog(page):
            _log(f"      ⚠ המסמך אינו זמין — מדלג: {save_path.name}")
            return "missing"
        _log(f"      ✗ לא הורד: {save_path.name}")
        return "fail"
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        _close_viewer(page)                     # never leave a blocking overlay


# ---------------------------------------------------------------------------
# Process a single case
# ---------------------------------------------------------------------------

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


def _collect_case_parties(page, case_dir: Path) -> dict:
    """First visit to a case: open the 'גורמים בתיק' tab and harvest each
    party's role, name and ID into case_info.json (cached — parties rarely
    change, so we only do this when the file is missing)."""
    info_path = case_dir / "case_info.json"
    if info_path.exists():
        try:
            return json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    import json as _json
    parties = []
    try:
        tab = page.locator("#CaseParties").first
        if tab.count() == 0:
            return {}
        tab.click()
        time.sleep(2)
        # expand every role group (זוכה / חייב / בא כוח…)
        headers = page.locator("#table-groups-expansion mat-expansion-panel-header").all()
        for h in headers:
            try:
                role = h.locator("span.bold").first.inner_text(timeout=1000).strip()
                if h.get_attribute("aria-expanded") != "true":
                    h.click()
                    time.sleep(1.2)
            except Exception:
                continue
            # inside: app-case-party-general rows of מעמד/זיהוי/שם
            for grp in page.locator("app-case-party-general").all():
                try:
                    labels = [x.strip() for x in grp.inner_text(timeout=1500).split("\n") if x.strip()]
                    rec = {"role": role}
                    for i in range(0, len(labels) - 1):
                        if labels[i] == "שם":
                            rec["name"] = labels[i + 1]
                        elif labels[i] == "זיהוי":
                            rec["id"] = labels[i + 1]
                    if rec.get("name") and rec not in parties:
                        parties.append(rec)
                except Exception:
                    continue
    except Exception as e:
        _log(f"  ⚠ גורמים בתיק: {e}")
    info = {"parties": parties}
    if parties:
        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            info_path.write_text(_json.dumps(info, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
            _log("  ✓ צדדים: " + ", ".join(f"{p['role']}: {p.get('name','')}" for p in parties))
        except Exception:
            pass
    return info


def _process_case(page, case: dict, output_dir: Path, by_client: bool = False) -> None:
    case_num = case["number"]
    if by_client:
        client = _sanitize(case.get("party") or "ללא שם")
        case_dir = output_dir / client / "הוצאה לפועל" / _sanitize(case_num)
    else:
        case_dir = output_dir / _sanitize(case_num)

    _log(f"\n{'='*60}")
    _log(f"תיק: {case_num}  ({case.get('type', '')})")

    if not _open_motions_tab(page, case_num):
        _log(f"  ✗ דילוג על תיק {case_num}")
        return

    # First visit: harvest parties (role/name/ID) from גורמים בתיק, then
    # return to the motions tab.
    if not (case_dir / "case_info.json").exists():
        _collect_case_parties(page, case_dir)
        try:
            page.locator("#MotionDecisionTable").first.click()
            time.sleep(2)
        except Exception:
            pass

    _maximize_paginator(page)
    time.sleep(1)
    _expand_all_rows(page)

    rows_data = _extract_rows(page)
    _log(f"  {len(rows_data)} שורות נמצאו")

    for rd in rows_data:
        proc = rd["process"]
        applicant = _sanitize(rd["applicant"] or "")
        date = _sanitize(rd["date"] or "")
        dec_date = _sanitize(rd["dec_date"] or "")
        proc_dir = case_dir / _sanitize(proc.split(".")[0])

        if rd["has_motion"]:
            filename = _make_filename(proc, date, "בקשה", applicant)
            _log(f"    [{proc}] בקשה ({date}) — {filename}")
            row = _row_locator(page, proc, rd["date"])
            _stats_tick(_download_doc(page, row, "motionDocumentID", proc_dir / filename))

        if rd["has_decision"]:
            filename = _make_filename(proc, dec_date, "החלטה", applicant)
            _log(f"    [{proc}] החלטה ({dec_date}) — {filename}")
            row = _row_locator(page, proc, rd["date"])
            _stats_tick(_download_doc(page, row, "decisionDocumentID", proc_dir / filename))

        time.sleep(0.3)

    _write_case_manifest(case_dir, case_num)


def _write_case_manifest(case_dir: Path, case_num: str) -> None:
    """Write summary.csv + sync_history.csv in the LIAS manifest format so the
    dashboard importer indexes ECA documents like any other portal."""
    import csv as _csv
    if not case_dir.exists():
        return
    rows = []
    for pdf in sorted(case_dir.rglob("*.pdf")):
        rel = pdf.relative_to(case_dir)
        stem = pdf.stem
        parts = [x.strip() for x in stem.split(" - ")]
        # "{proc} - {date} - {סוג} - {מגיש}"
        date = parts[1] if len(parts) > 1 else ""
        if re.match(r"\d{2}\.\d{2}\.\d{2}$", date):
            d, m, y = date.split(".")
            date = f"{d}/{m}/20{y}"
        rows.append({
            "שם מסמך (מהטבלה)": stem,
            "שם קובץ פיזי בדיסק": str(rel),
            "סוג קובץ": parts[2] if len(parts) > 2 else "",
            "מגיש": parts[3] if len(parts) > 3 else "",
            "תאריך מסמך": date,
            "סטטוס הורדה": "Success",
            "גודל (KB)": str(round(pdf.stat().st_size / 1024, 1)),
        })
    if not rows:
        return
    with open(case_dir / "summary.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(case_dir / "sync_history.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["תאריך ריצה", "פורטל", "תיק"])
        w.writeheader()
        w.writerow({"תאריך ריצה": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "פורטל": "ECA", "תיק": case_num})


# ---------------------------------------------------------------------------
# Sync entry point (called from LIAS jobs on an already-open page)
# ---------------------------------------------------------------------------

def run_eca_download(page, root_output_dir: Path, cases_filter: list[str] | None = None,
                     progress=None, should_cancel=None) -> str:
    """Download all ECA cases into root_output_dir/downloads/{client}/הוצאה לפועל/{case}.

    Assumes the caller provides a Playwright page; handles ECA login itself
    (reuses gov.il auto-login). `progress(fraction, message)` — optional
    callback so the job bar / tasks balloon reflect per-case progress.
    Returns a short summary string.
    """
    downloads_dir = Path(root_output_dir) / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(0.1, "מתחבר להוצאה לפועל…")
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

    _STATS.update(done=0, errors=0, total=0, case_idx=0,
                  cases_total=len(cases), t0=time.time())
    ok = 0
    for case in cases:
        if should_cancel and should_cancel():
            _log("⏹ ההורדה נעצרה על ידי המשתמש")
            return f"נעצר: {ok}/{len(cases)} תיקים ({_STATS['done']} מסמכים)"
        _STATS["case_idx"] += 1
        if progress:
            # spread cases across 0.15..0.85 of the bar
            frac = 0.15 + 0.70 * (_STATS["case_idx"] - 1) / max(len(cases), 1)
            progress(frac, f"תיק {_STATS['case_idx']}/{len(cases)}: {case['number']}")
        try:
            _process_case(page, case, downloads_dir, by_client=True)
            ok += 1
        except Exception as e:
            _log(f"✗ שגיאה בתיק {case['number']}: {e}")
        time.sleep(1)
    return f"הוצל\"פ: {ok}/{len(cases)} תיקים ({_STATS['done']} מסמכים, {_STATS['errors']} כשלו)"


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
