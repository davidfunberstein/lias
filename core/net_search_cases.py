"""NET HaMishpat — case search by date range and bulk download.

Flow:
1. Click "איתור תיקים" → "תיקים לתאריך פתיחה"
2. Fill from/to date and click אישור
3. Read all results from the hidden CaseSearchResultsGridArrayStore JSON
4. For each case: parse CaseDisplayIdentifier → navigate → download תיק נייר
   (related-cases recursion is OFF for this flow to avoid infinite chains)

All cases found are downloaded in the same style as manual per-case downloads.
A reminder is printed that this covers the full picture of what's visible to the
logged-in user (typically a private client or their lawyer).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from core.logger import Logger

# Parse "NNN-MM-YY" → (case_num, mmyy)
_CASE_ID_RE = re.compile(r'^(\d+)-(\d{2})-(\d{2})$')


def _parse_display_id(display_id: str) -> tuple[str, str] | None:
    """Parse '330-04-22' → ('330', '0422'). Returns None if format unrecognised."""
    m = _CASE_ID_RE.match((display_id or "").strip())
    if m:
        return m.group(1), m.group(2) + m.group(3)
    return None


# JS run in the page: find a menu <a>/<button> by its visible Hebrew label
# (text or alt) and fire the REAL __doPostBack target parsed out of its
# href/onclick. This is resilient to the portal renaming its ASP.NET naming
# container (header$UpperMenu1$… → Header1$CaseLocatorHeaderUC2$…), which is
# what silently broke the date-range search and forced the "my cases" fallback.
_POSTBACK_BY_LABEL_JS = r"""
(label) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const els = Array.from(document.querySelectorAll(
    "a[href*='__doPostBack'], a[onclick*='__doPostBack'], a.dropdown-item, button, input[type='button']"));
  for (const el of els) {
    const lab = norm(el.getAttribute && el.getAttribute('alt')) ||
                norm(el.textContent) || norm(el.value);
    if (lab === label) {
      const src = (el.getAttribute('href') || '') + ' ' + (el.getAttribute('onclick') || '');
      const m = src.match(/__doPostBack\((['"])(.*?)\1/);
      if (m && typeof __doPostBack === 'function') { __doPostBack(m[2], ''); return m[2]; }
      try { el.click(); return 'click'; } catch (e) {}
    }
  }
  return null;
}
"""


def _fire_postback_by_label(page: "Page", label: str) -> bool:
    """Trigger the menu item whose visible label == `label`, container-agnostic.
    Returns True if a postback target was found and fired (or the item clicked)."""
    try:
        target = page.evaluate(_POSTBACK_BY_LABEL_JS, label)
        if target:
            print(f"[Search] Fired '{label}' via {target}")
            return True
    except Exception as e:
        print(f"[Search] postback-by-label '{label}' failed: {e}")
    return False


def _open_case_search_dropdown(page: "Page") -> None:
    """Open the Bootstrap 'איתור תיקים' dropdown so sub-items become visible.

    The menu uses data-bs-toggle="dropdown"; the container id has changed across
    portal versions (#header_UpperMenu1_tdSearchCase → CaseLocatorHeaderUC2), so
    we try several container-scoped selectors plus a plain text match.
    """
    container = page.locator("#header_UpperMenu1_tdSearchCase")
    try:
        menu = container.locator(".dropdown-menu").first
        if menu.count() > 0 and menu.is_visible(timeout=500):
            print("[Search] Dropdown already open.")
            return
    except Exception:
        pass

    selectors = [
        '#header_UpperMenu1_tdSearchCase > button.dropdown-toggle',
        '#header_UpperMenu1_tdSearchCase button:has-text("איתור תיקים")',
        'button.dropdown-toggle:has-text("איתור תיקים")',
        '[data-bs-toggle="dropdown"]:has-text("איתור תיקים")',
        'a:has-text("איתור תיקים")',
        'button:has-text("איתור תיקים")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0:
                btn.click(timeout=4000)
                time.sleep(0.5)
                print(f"[Search] Clicked dropdown toggle 'איתור תיקים' via {sel}")
                return
        except Exception:
            continue
    print("[Search] Could not find dropdown toggle 'איתור תיקים'.")


def navigate_to_my_cases(page: "Page") -> bool:
    """Click 'איתור תיקים' → 'התיקים שלי' and wait for the date form.

    The 'התיקים שלי' screen shows the same from/to date fields
    (#fromDateCalendar / #toDateCalendar) plus the user's case list.
    """
    def _form_visible() -> bool:
        # The date-search page ("תיקים לתאריך פתיחה") ALSO has #fromDateCalendar,
        # so require the MyCases URL — otherwise we silently stay on the wrong
        # screen and re-run the date search instead of "התיקים שלי".
        try:
            if page.locator("#fromDateCalendar").count() == 0:
                return False
            return "MyCasesListView" in (page.url or "")
        except Exception:
            return False

    if _form_visible():
        return True
    try:
        page.evaluate("__doPostBack('header$UpperMenu1$btnMyCases','')")
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        time.sleep(1)
        if _form_visible():
            print("[Search] Opened 'התיקים שלי'.")
            return True
    except Exception as e:
        print(f"[Search] btnMyCases postback failed: {e}")

    _open_case_search_dropdown(page)

    # Container-agnostic: fire 'התיקים שלי' by its live postback target.
    if _fire_postback_by_label(page, "התיקים שלי"):
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        time.sleep(1)
        if _form_visible():
            print("[Search] Opened 'התיקים שלי' (label postback).")
            return True

    for sel in ("#header_UpperMenu1_btnMyCases", 'a:has-text("התיקים שלי")'):
        try:
            link = page.locator(sel).first
            if link.count() > 0:
                link.click(timeout=4000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                time.sleep(1)
                if _form_visible():
                    print(f"[Search] Opened 'התיקים שלי' via {sel}.")
                    return True
        except Exception:
            continue
    return False


# The portal removed/changed the date-range search form, so all six strategies
# below fail and every listing paid ~30s of doomed attempts before falling back
# to "התיקים שלי" (which works and returns the same cases). Remember the failure
# for the life of the process and skip straight to the fallback next time.
# A restart re-tests it, so a portal-side fix is picked up on its own.
_DATE_SEARCH_UNAVAILABLE = False


def date_search_known_broken() -> bool:
    return _DATE_SEARCH_UNAVAILABLE


def navigate_to_date_search(page: "Page") -> bool:
    """Click 'תיקים לתאריך פתיחה' and wait for the date form to appear.

    The button fires an ASP.NET postback that partially updates the page via
    UpdatePanel — so a full domcontentloaded never fires.  We trigger the
    postback, wait for the network to quiet down, then look for the form.
    Falls back to clicking the visible link if JS evaluation doesn't work.
    """
    global _DATE_SEARCH_UNAVAILABLE      # declared before first use (read below)
    if _DATE_SEARCH_UNAVAILABLE:
        print("[Search] Skipping date-range form — known unavailable this session.")
        return False

    def _is_date_search_page() -> bool:
        """Check we're on the date search page specifically (not MyCases which also has fromDateCalendar)."""
        try:
            url = page.url or ""
            if "MyCasesListView" in url:
                return False
            return page.locator("#fromDateCalendar").count() > 0
        except Exception:
            return False

    def _wait_form(timeout_ms: int = 12000) -> bool:
        try:
            page.wait_for_selector("#fromDateCalendar", state="attached", timeout=timeout_ms)
            url = page.url or ""
            return "MyCasesListView" not in url
        except Exception:
            return False

    if _is_date_search_page():
        return True

    # Primary (container-agnostic): open the parent menu, then fire the item's
    # own postback target parsed from the live DOM — survives id/container
    # renames on the portal side.
    _open_case_search_dropdown(page)
    if _fire_postback_by_label(page, "תיקים לתאריך פתיחה"):
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        time.sleep(0.5)
        if _wait_form(10000):
            print("[Search] Opened date-range search form (label postback).")
            return True

    # Legacy: postback to the old hard-coded button ID (older portal versions)
    try:
        page.evaluate("__doPostBack('header$UpperMenu1$btnCaseDate','')")
        # UpdatePanel AJAX — wait for network to settle, not a full page load
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        time.sleep(0.5)
        if _wait_form(10000):
            print("[Search] Opened date-range search form.")
            return True
    except Exception as e:
        print(f"[Search] Postback eval failed: {e}")

    # Open parent dropdown menu before trying sub-items
    _open_case_search_dropdown(page)

    # Fallback: click the visible anchor directly
    try:
        link = page.locator("#header_UpperMenu1_btnCaseDate")
        if link.count() > 0:
            link.click(timeout=4000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            time.sleep(0.5)
            if _wait_form(10000):
                print("[Search] Opened date-range search form via anchor click.")
                return True
    except Exception as e:
        print(f"[Search] Anchor click failed: {e}")

    # Fallback 2: click by visible text
    try:
        link = page.locator('a:has-text("תיקים לתאריך פתיחה")').first
        if link.count() > 0:
            link.click(timeout=4000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            time.sleep(0.5)
            if _wait_form(10000):
                print("[Search] Opened date-range search form via text click.")
                return True
    except Exception as e:
        print(f"[Search] Text click failed: {e}")

    # Diagnosis: dump current URL and visible inputs
    try:
        print(f"[Search] Current URL: {page.url}")
        ids = page.evaluate(
            "Array.from(document.querySelectorAll('input[id]')).map(e=>e.id).slice(0,25)"
        )
        print(f"[Search] Input IDs on page: {ids}")
    except Exception:
        pass

    _DATE_SEARCH_UNAVAILABLE = True
    print("[Search] Could not open date-range search form — all strategies failed. "
          "Using 'התיקים שלי' from here on (no more retries this session).")
    return False


def fill_date_range_and_search(
    page: "Page",
    from_date: str,
    to_date: str,
) -> bool:
    """Fill date fields and click אישור. Dates in DD/MM/YYYY format."""
    try:
        # Fill from date
        page.evaluate(
            f"document.getElementById('fromDateCalendar').value = '{from_date}';"
            f"document.getElementById('fromDateCalendar').dispatchEvent(new Event('change'));"
        )
        # Fill to date
        page.evaluate(
            f"document.getElementById('toDateCalendar').value = '{to_date}';"
            f"document.getElementById('toDateCalendar').dispatchEvent(new Event('change'));"
        )
        time.sleep(0.3)
        # Click אישור
        page.evaluate(
            "WebForm_DoPostBackWithOptions(new WebForm_PostBackOptions("
            "'buttonsGroup:searchButton', '', true, '', '', false, true))"
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            pass
        time.sleep(1.5)
        print(f"[Search] Searched for cases from {from_date} to {to_date}.")
        return True
    except Exception as e:
        print(f"[Search] Search failed: {e}")
        return False


def extract_cases_from_search_grid(page: "Page") -> list[dict]:
    """Read all case records from the hidden CaseSearchResultsGridArrayStore JSON field.

    All results are embedded in this hidden input — no need to paginate the UI.
    Returns list of dicts with keys: CaseID, CaseDisplayIdentifier, CaseName,
    CaseTypeShortName, CourtName, CaseStatusName, CaseInterestName.
    """
    try:
        raw = page.evaluate(
            "document.getElementById('CaseSearchResultsGridArrayStore')?.value || ''"
        )
        if not raw:
            print("[Search] CaseSearchResultsGridArrayStore is empty.")
            return []
        cases = json.loads(raw)
        print(f"[Search] Found {len(cases)} case(s) in search results.")
        return cases
    except Exception as e:
        print(f"[Search] Could not read search results: {e}")
        return []


def fill_my_cases_dates_and_search(page: "Page", from_date: str, to_date: str) -> bool:
    """On the 'התיקים שלי' screen: fill from/to dates and click אישור."""
    try:
        page.evaluate(
            f"var f=document.getElementById('fromDateCalendar'); if(f){{f.value='{from_date}';f.dispatchEvent(new Event('change'));}}"
            f"var t=document.getElementById('toDateCalendar'); if(t){{t.value='{to_date}';t.dispatchEvent(new Event('change'));}}"
        )
        time.sleep(0.5)
        clicked = page.evaluate(
            "(() => { const a=[...document.querySelectorAll('a,input[type=button],button')]"
            ".find(e=>(e.innerText||e.value||'').trim()==='אישור'); if(a){a.click();return true;} return false; })()"
        )
        if not clicked:
            page.locator('a:has-text("אישור"), input[value="אישור"]').first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[Search] my-cases date search failed: {e}")
        return False


def extract_cases_from_my_cases_grid(page: "Page") -> list[dict]:
    """Extract all cases from the 'רשימת התיקים שלי' grid, paging through
    every page with 'לדף הבא' until exhausted.

    Returns a list of dicts: {"CaseDisplayIdentifier": "22936-02-15",
    "CaseName": ..., "CourtName": ..., "CaseStatusName": ...}.
    """
    _EXTRACT_JS = """
    (() => {
      const rows = [];
      const idRe = /\\d+-\\d{2}-\\d{2}/;
      // Try any hidden JSON store (CaseSearchResultsGridArrayStore or MyCasesGridArrayStore etc.)
      for (const el of document.querySelectorAll('input[type="hidden"][id*="GridArrayStore"]')) {
        if (el.value) {
          try {
            const parsed = JSON.parse(el.value);
            if (Array.isArray(parsed) && parsed.length > 0) {
              return parsed.map(c => ({id: c.CaseDisplayIdentifier, cells: [
                c.CaseTypeShortName||'', c.CaseDisplayIdentifier||'', c.CaseName||'',
                c.CaseInterestName||'', c.CourtName||'', c.CaseStatusName||''
              ], data: c}));
            }
          } catch(e) {}
        }
      }
      // Fallback: scrape ag-grid rendered rows
      document.querySelectorAll('.ag-row, div[role="row"]').forEach(r => {
        const cells = [...r.querySelectorAll('.ag-cell, div[role="gridcell"], td')]
          .map(c => (c.innerText || '').trim());
        const id = cells.find(c => idRe.test(c));
        if (id) {
          const m = id.match(idRe);
          rows.push({ id: m[0], cells });
        }
      });
      // Fallback 2: table rows
      if (rows.length === 0) {
        document.querySelectorAll('tr').forEach(r => {
          const cells = [...r.querySelectorAll('td')].map(c => (c.innerText || '').trim());
          const id = cells.find(c => idRe.test(c));
          if (id) {
            const m = id.match(idRe);
            rows.push({ id: m[0], cells });
          }
        });
      }
      return rows;
    })()
    """
    all_cases: list[dict] = []
    seen: set[str] = set()
    for page_num in range(1, 40):  # hard cap
        if page_num == 1:
            time.sleep(3)
        try:
            rows = page.evaluate(_EXTRACT_JS) or []
            if page_num == 1 and not rows:
                debug = page.evaluate("""(() => {
                  const stores = [...document.querySelectorAll('input[type="hidden"]')]
                    .filter(e=>e.value&&e.value.length>10).map(e=>e.id+'='+e.value.substring(0,80));
                  const agRows = document.querySelectorAll('.ag-row').length;
                  const trs = document.querySelectorAll('tr').length;
                  return {stores: stores.slice(0,5), agRows, trs, url: location.href};
                })()""")
                print(f"[Search] my-cases debug: {debug}")
        except Exception as e:
            print(f"[Search] my-cases extract failed: {e}")
            break
        new = 0
        for r in rows:
            cid = r["id"]
            if cid in seen:
                continue
            seen.add(cid)
            cells = r.get("cells", [])
            data = r.get("data", {})
            all_cases.append({
                "CaseDisplayIdentifier": cid,
                "CaseName": data.get("CaseName") or next((c for c in cells if c and c != cid and not c.isdigit()), ""),
                "CaseTypeShortName": data.get("CaseTypeShortName", ""),
                "CourtName": data.get("CourtName", ""),
                "CaseStatusName": data.get("CaseStatusName", ""),
                "CaseInterestName": data.get("CaseInterestName", ""),
                "cells": cells,
            })
            new += 1
        print(f"[Search] my-cases page {page_num}: +{new} (total {len(all_cases)})")
        # next page?
        try:
            has_next = page.evaluate(
                "(() => { const b=[...document.querySelectorAll('button,a')]"
                ".find(e=>(e.innerText||'').trim()==='לדף הבא' && !e.disabled"
                " && !(e.closest('[aria-disabled=\"true\"]'))); if(b){b.click();return true;} return false; })()"
            )
        except Exception:
            has_next = False
        if not has_next or new == 0:
            break
        time.sleep(1.5)
    return all_cases


def run_bulk_download_from_date_search(
    page: "Page",
    root_output_dir: Path,
    session_settings: dict,
    logger: "Logger | None" = None,
    years_back: int = 10,
) -> None:
    """Full flow: search by date → extract cases → download each one.

    years_back: how many years back from today the from_date will be set.
    """
    def _log(msg: str, level: str = "info") -> None:
        print(f"[BulkSearch] {msg}")
        if logger:
            getattr(logger, level, logger.info)(f"[BulkSearch] {msg}")

    from core.net_case_navigator import navigate_to_case_by_number
    from core.net_navigation import NetNavigator, NoTikNiyarTab
    from core.net_scraper import NetScraper
    from core.manifest import ManifestManager, get_summary_csv_path
    from core.sync_history import SyncHistory, compute_net_hash, dates_from_net_metadata
    from core.download import extract_case_folder_name, _append_net_to_global_summary

    today = datetime.now()
    from_dt = today - timedelta(days=years_back * 365)
    from_date_str = from_dt.strftime("%d/%m/%Y")
    to_date_str = today.strftime("%d/%m/%Y")

    print("\n" + "=" * 60)
    print("  הורדת כל תיקי לקוח — איתור לפי תאריך")
    print(f"  טווח: {from_date_str} עד {to_date_str}")
    print("  הערה: כיסוי מלא של כל מה שגלוי ללקוח הפרטי.")
    print("=" * 60 + "\n")

    # Step 1-3: Preferred path — "התיקים שלי" (user's own case list with dates)
    cases: list[dict] = []
    if navigate_to_my_cases(page):
        if fill_my_cases_dates_and_search(page, from_date_str, to_date_str):
            cases = extract_cases_from_my_cases_grid(page)
            if cases:
                _log(f"'התיקים שלי' — {len(cases)} תיקים נמצאו.")

    # Fallback path — "תיקים לתאריך פתיחה" (hidden JSON store)
    if not cases:
        # Navigate back to a page where the dropdown works — use postback directly
        # since we're already on the secured domain
        if not navigate_to_date_search(page):
            _log("Cannot open date search — aborting.", "error")
            return
        if not fill_date_range_and_search(page, from_date_str, to_date_str):
            _log("Search failed — aborting.", "error")
            return
        cases = extract_cases_from_search_grid(page)

    if not cases:
        _log("No cases found in search results.")
        return

    # Step 4: Filter to parseable cases and preview
    parseable: list[tuple[dict, str, str]] = []
    skipped: list[str] = []
    for c in cases:
        did = c.get("CaseDisplayIdentifier", "")
        parsed = _parse_display_id(did)
        if parsed:
            parseable.append((c, parsed[0], parsed[1]))
        else:
            skipped.append(did)

    _log(f"{len(parseable)} תיקים להורדה, {len(skipped)} דולגו (פורמט לא מוכר: {skipped}).")

    run_timestamp = session_settings.get(
        "run_timestamp", today.strftime("%Y-%m-%d %H:%M:%S")
    )
    downloads_dir = root_output_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    total_ok = 0
    total_skip = 0
    total_fail = 0

    for idx, (case_info, case_num, mmyy) in enumerate(parseable, 1):
        display_id = case_info.get("CaseDisplayIdentifier", "")
        case_name_hint = case_info.get("CaseName", "")
        case_type = case_info.get("CaseTypeShortName", "")
        _log(f"[{idx}/{len(parseable)}] {display_id} — {case_name_hint}")

        # Navigate to the case
        success = navigate_to_case_by_number(page, case_num, mmyy, logger=logger)
        if not success:
            _log(f"  Could not navigate to {display_id} — skip.", "warn")
            total_fail += 1
            continue

        time.sleep(3)

        scraper = NetScraper(page, logger=logger)
        raw_case_name = scraper.get_case_name_from_ui() or display_id

        nav = NetNavigator(page, logger=logger)
        try:
            nav.navigate_to_tik_niyar()
        except NoTikNiyarTab as e:
            _log(f"  No תיק נייר tab: {e} — skip.", "warn")
            total_skip += 1
            continue
        except Exception as e:
            _log(f"  Error on תיק נייר: {e} — skip.", "warn")
            total_skip += 1
            continue

        case_dir_name = extract_case_folder_name(raw_case_name)
        if not case_dir_name:
            case_dir_name = re.sub(r'[\\/*?:"<>|]', "", f"{display_id} — {case_name_hint}").strip()
        case_dir = downloads_dir / case_dir_name
        case_dir.mkdir(parents=True, exist_ok=True)

        manifest = ManifestManager(
            get_summary_csv_path(case_dir),
            run_timestamp=run_timestamp,
            logger=logger,
        )
        manifest.sync_with_disk(case_dir)

        successful_ids = manifest.get_successful_ids()
        re_dl_ids = manifest.get_missing_ids()

        metadata_lookup = scraper.extract_metadata()
        scraper.pre_populate_manifest_from_metadata(case_dir, manifest, metadata_lookup)

        metadata_ids = set(metadata_lookup.keys())
        if metadata_ids and metadata_ids.issubset(successful_ids) and not re_dl_ids:
            _log(f"  Smart skip — all {len(metadata_ids)} docs already synced.")
            portal_hash = compute_net_hash(metadata_lookup)
            first_date, last_date = dates_from_net_metadata(metadata_lookup)
            sh = SyncHistory(case_dir, logger, label=case_dir.name)
            sh.append(
                portal="NET", total=len(metadata_ids),
                new_downloads=0, re_downloads=0, failed=0,
                first_date=first_date, last_date=last_date, portal_hash=portal_hash,
            )
            total_skip += 1
            continue

        # Download loop
        page_number = 1
        dl_ok = 0
        dl_fail = 0

        while True:
            links_found, page_dl, page_fail = scraper.scrape_and_download_current_page(
                case_dir=case_dir,
                manifest=manifest,
                metadata_lookup=metadata_lookup,
                global_idx_start=len(successful_ids) + dl_ok,
                re_download_ids=re_dl_ids,
            )
            if links_found == 0:
                break
            dl_ok += len(page_dl)
            dl_fail += len(page_fail)
            if not scraper.go_to_next_page():
                break
            page_number += 1
            time.sleep(5)

        # History
        if metadata_lookup:
            portal_hash = compute_net_hash(metadata_lookup)
            first_date, last_date = dates_from_net_metadata(metadata_lookup)
            sh = SyncHistory(case_dir, logger, label=case_dir.name)
            sh.append(
                portal="NET", total=len(metadata_ids),
                new_downloads=dl_ok, re_downloads=len(re_dl_ids), failed=dl_fail,
                first_date=first_date, last_date=last_date, portal_hash=portal_hash,
            )

        manifest.print_summary(logger)

        # Decisions & viewers (merge תיק נייר פירוט + החלטות)
        try:
            from core.net_decisions import NetDecisionsScraper
            _ds = NetDecisionsScraper(page, case_dir, logger=logger)
            _tn_viewers = _ds.collect_tik_niyar_viewers()
            _ds.update_all_decisions(manifest=manifest, extra_viewers=_tn_viewers)
        except Exception as _de:
            _log(f"  Decisions update skipped: {_de}", "warn")

        # Global summary
        try:
            _pending = sum(1 for r in manifest.records if r.get("סטטוס הורדה") in ("Pending", "Failed"))
            _ph = compute_net_hash(metadata_lookup) if metadata_lookup else ""
            _fd, _ld = dates_from_net_metadata(metadata_lookup) if metadata_lookup else ("", "")
            _append_net_to_global_summary(
                case_dir=case_dir,
                raw_case_name=raw_case_name,
                parties_str=manifest.parties or "",
                representatives_str=manifest.representative or "",
                total=len(manifest.records),
                downloaded=dl_ok,
                failed=dl_fail,
                portal_hash=_ph,
                first_date=_fd,
                last_date=_ld,
                pending=_pending,
            )
        except Exception as _e:
            _log(f"  Global summary update failed: {_e}", "warn")

        if dl_ok > 0 or dl_fail == 0:
            total_ok += 1
        else:
            total_fail += 1

        _log(f"  Done: {dl_ok} downloaded, {dl_fail} failed.")

    print("\n" + "=" * 60)
    print(f"  [BulkSearch] הסתיים")
    print(f"  תיקים שעודכנו:    {total_ok}")
    print(f"  תיקים שדולגו:     {total_skip}")
    print(f"  תיקים שנכשלו:     {total_fail}")
    print("=" * 60)
    if logger:
        logger.info(
            f"[BulkSearch] Done: {total_ok} updated, {total_skip} skipped, {total_fail} failed."
        )
