"""NET HaMishpat — תיקים קשורים (Related Cases) downloader.

After downloading the main case's תיק נייר, this module:
1. Navigates to תיקים קשורים (folder 24) in the left tree.
2. Extracts all rows from the ag-grid (case_id, display_id, case_name, …).
3. For each related case: navigates to it, runs the full תיק נייר download,
   then returns to the original case for the next one.

Entry point: process_related_cases()
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from core.logger import Logger

# Folder 24 = תיקים קשורים
_RELATED_FOLDER_JS = (
    "__doPostBack('_ctl0$ElectronicCaseFolderTreeView1$NavigateToFolder','24')"
)

_EXTRACT_RELATED_JS = """
() => {
    const rows = document.querySelectorAll('.ag-row[role="row"]');
    return Array.from(rows).map(row => {
        const get = colId => {
            const c = row.querySelector('[col-id="' + colId + '"]');
            return c ? c.innerText.trim() : '';
        };
        const link = row.querySelector('[col-id="CaseDisplayIdentifier"] a');
        const href = link ? (link.getAttribute('href') || '') : '';
        // href contains __doPostBack("_ctl0:SearchResultsWebGrid1:btnCase","71925892")
        const caseId = (href.match(/"(\\d+)"/) || [])[1] || '';
        return {
            display_id: get('CaseDisplayIdentifier'),
            case_id:    caseId,
            case_name:  get('CaseName'),
            case_type:  get('CaseTypeShortName'),
            court:      get('CourtName'),
            status:     get('CaseStatusName'),
            link_type:  get('CaseLinkTypeName'),
        };
    }).filter(r => r.case_id);
}
"""


def navigate_to_related_cases(page: "Page", logger: "Logger | None" = None) -> bool:
    """Navigate to תיקים קשורים folder. Returns True when grid appears."""
    def _log(msg: str) -> None:
        print(f"[RelatedCases] {msg}")
        if logger:
            logger.info(f"[RelatedCases] {msg}")

    try:
        page.evaluate(_RELATED_FOLDER_JS)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        time.sleep(1.0)
        page.wait_for_selector(".ag-row", state="attached", timeout=8000)
        time.sleep(0.5)
        _log("Navigated to תיקים קשורים.")
        return True
    except Exception as e:
        _log(f"Navigation to תיקים קשורים failed: {e}")
        return False


def extract_related_cases(page: "Page", logger: "Logger | None" = None) -> list[dict]:
    """Extract all related case rows from the current ag-grid."""
    try:
        rows = page.evaluate(_EXTRACT_RELATED_JS) or []
        print(f"[RelatedCases] Found {len(rows)} related case(s).")
        for r in rows:
            print(f"  • {r['display_id']}  {r['case_name']}  [{r['case_type']}]  {r['status']}")
        return rows
    except Exception as e:
        print(f"[RelatedCases] Failed to extract rows: {e}")
        if logger:
            logger.warn(f"[RelatedCases] extract failed: {e}")
        return []


def _navigate_to_related_case(page: "Page", case_id: str, display_id: str,
                               logger: "Logger | None" = None) -> bool:
    """Click on a related case link and wait for the case page to load."""
    def _log(msg: str) -> None:
        print(f"[RelatedCases] {msg}")
        if logger:
            logger.info(f"[RelatedCases] {msg}")

    try:
        _log(f"Navigating to related case {display_id} (id={case_id})...")
        page.evaluate(
            f"__doPostBack('_ctl0:SearchResultsWebGrid1:btnCase', '{case_id}')"
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        time.sleep(2.0)
        _log(f"Loaded related case {display_id}.")
        return True
    except Exception as e:
        _log(f"Failed to navigate to case {display_id}: {e}")
        return False


def process_related_cases(
    page: "Page",
    original_case_num: str,
    original_mmyy: str,
    root_output_dir: Path,
    session_settings: dict,
    resolve_paths_fn,
    logger: "Logger | None" = None,
    already_processed: set[str] | None = None,
) -> list[str]:
    """
    Full flow: navigate to תיקים קשורים, extract all cases, download each.

    Key design: saves the original case URL BEFORE clicking any related case,
    then restores it via page.goto() — avoiding the unreliable form-fill navigation.

    already_processed: set of display_ids that were already handled (avoids loops).
    Returns list of display_ids that were processed in this call.
    """
    import re as _re

    def _log(msg: str, level: str = "info") -> None:
        print(f"[RelatedCases] {msg}")
        if logger:
            getattr(logger, level, logger.info)(f"[RelatedCases] {msg}")

    from core.net_navigation import NetNavigator, NoTikNiyarTab
    from core.net_scraper import NetScraper
    from core.manifest import ManifestManager, get_summary_csv_path
    from core.sync_history import SyncHistory, compute_net_hash, dates_from_net_metadata
    from core.download import extract_case_folder_name, _append_net_to_global_summary

    if already_processed is None:
        already_processed = set()

    # Save the URL of the original case's secured portal page NOW, before any navigation.
    # We'll use page.goto(original_url) to return — far more reliable than form-fill.
    original_url = page.url

    # Step 1: Navigate to תיקים קשורים
    if not navigate_to_related_cases(page, logger):
        _log("Could not open תיקים קשורים — skipping.", "warn")
        return []

    # Step 2: Extract all related cases
    related = extract_related_cases(page, logger)
    if not related:
        _log("No related cases found.")
        return []

    run_timestamp = session_settings.get(
        "run_timestamp", __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    downloads_dir = root_output_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    # ── Build canonical folder names from grid data (before navigating anywhere) ──
    # We always use display_id + case_name as folder name for related cases.
    # The portal toolbar often fails to load for related cases (shows "Unknown_Case"),
    # so grid data is the reliable primary source.
    def _grid_folder_name(display_id: str, case_name: str) -> str:
        raw = f"{display_id} — {case_name}"
        return _re.sub(r'[\\/*?:"<>|]', "", raw).strip()

    # ── Show user what's coming ──
    print(f"\n[RelatedCases] תיקים קשורים — {len(related)} תיקים:")
    for r in related:
        _fn = _grid_folder_name(r["display_id"], r["case_name"])
        status_tag = "(חדש)" if not (downloads_dir / _fn).exists() else "(קיים)"
        print(f"  {status_tag}  {r['display_id']}  {r['case_name']}  [{r['case_type']}]  {r['status']}")
    print()

    # ── Pre-write all related cases to global summary as "ממתין" ──
    # (so the summary reflects what's queued even before downloading starts)
    for r in related:
        if r["display_id"] in already_processed:
            continue
        _pre_dir = downloads_dir / _grid_folder_name(r["display_id"], r["case_name"])
        try:
            _append_net_to_global_summary(
                case_dir=_pre_dir,
                raw_case_name=r["case_name"],
                parties_str="", representatives_str="",
                total=0, downloaded=0, failed=0, pending=1,
            )
        except Exception:
            pass

    processed_this_run: list[str] = []

    for idx, case_info in enumerate(related, 1):
        display_id = case_info["display_id"]
        case_id    = case_info["case_id"]
        case_name  = case_info["case_name"]

        if display_id in already_processed:
            _log(f"[{idx}/{len(related)}] {display_id} already processed — skip.")
            continue

        _log(f"[{idx}/{len(related)}] Processing related case: {display_id} — {case_name}")

        # Always use display_id + case_name as folder (stable, no portal dependency)
        case_dir_name = _grid_folder_name(display_id, case_name)
        case_dir = downloads_dir / case_dir_name
        case_dir.mkdir(parents=True, exist_ok=True)
        _log(f"  Output folder: {case_dir.name}")

        # Navigate to the related case
        ok = _navigate_to_related_case(page, case_id, display_id, logger)
        if not ok:
            _log(f"Skip {display_id} — navigation failed.", "warn")
            already_processed.add(display_id)
            _return_to_original_url(page, original_url, logger)
            if idx < len(related):
                navigate_to_related_cases(page, logger)
            continue

        scraper = NetScraper(page, logger=logger)
        # Try to get a better name from the portal toolbar (optional — for metadata only)
        raw_case_name = scraper.get_case_name_from_ui() or ""
        if raw_case_name == "Unknown_Case":
            raw_case_name = ""
        if raw_case_name:
            _log(f"  Portal name: '{raw_case_name}'")
        else:
            _log(f"  Portal name: (not available — using grid name)")

        nav = NetNavigator(page, logger=logger)
        try:
            nav.navigate_to_tik_niyar()
        except NoTikNiyarTab as e:
            _log(f"  No תיק נייר tab for {display_id}: {e} — skip.", "warn")
            already_processed.add(display_id)
            _return_to_original_url(page, original_url, logger)
            if idx < len(related):
                navigate_to_related_cases(page, logger)
            continue
        except Exception as e:
            _log(f"  Error navigating to תיק נייר for {display_id}: {e} — skip.", "warn")
            already_processed.add(display_id)
            _return_to_original_url(page, original_url, logger)
            if idx < len(related):
                navigate_to_related_cases(page, logger)
            continue

        manifest = ManifestManager(
            get_summary_csv_path(case_dir),
            run_timestamp=run_timestamp,
            logger=logger,
        )
        manifest.sync_with_disk(case_dir)

        successful_ids = manifest.get_successful_ids()
        re_dl_ids = manifest.get_missing_ids()

        metadata_lookup = scraper.extract_metadata()
        _log(f"  {len(metadata_lookup)} metadata entries.")
        scraper.pre_populate_manifest_from_metadata(case_dir, manifest, metadata_lookup)

        # Smart-skip
        metadata_ids = set(metadata_lookup.keys())
        if metadata_ids and metadata_ids.issubset(successful_ids) and not re_dl_ids:
            _log(f"  Smart skip — all {len(metadata_ids)} docs already synced.")
            portal_hash = compute_net_hash(metadata_lookup)
            first_date, last_date = dates_from_net_metadata(metadata_lookup)
            SyncHistory(case_dir, logger, label=case_dir.name).append(
                portal="NET", total=len(metadata_ids),
                new_downloads=0, re_downloads=0, failed=0,
                first_date=first_date, last_date=last_date, portal_hash=portal_hash,
            )
            # Update global summary even on smart-skip (so row shows real totals, not "ממתין")
            _update_global_summary(
                case_dir=case_dir,
                raw_case_name=raw_case_name or case_name,
                manifest=manifest,
                metadata_lookup=metadata_lookup,
                total_ok=0,
                total_fail=0,
                log_fn=_log,
                compute_net_hash=compute_net_hash,
                dates_from_net_metadata=dates_from_net_metadata,
                _append_fn=_append_net_to_global_summary,
            )
            already_processed.add(display_id)
            processed_this_run.append(display_id)
            _return_to_original_url(page, original_url, logger)
            if idx < len(related):
                navigate_to_related_cases(page, logger)
            continue

        # Download loop
        page_number = 1
        total_ok = 0
        total_fail = 0

        while True:
            _log(f"  Page {page_number}...")
            links_found, page_dl, page_fail = scraper.scrape_and_download_current_page(
                case_dir=case_dir,
                manifest=manifest,
                metadata_lookup=metadata_lookup,
                global_idx_start=len(successful_ids) + total_ok,
                re_download_ids=re_dl_ids,
            )
            if links_found == 0:
                break
            total_ok += len(page_dl)
            total_fail += len(page_fail)
            if not scraper.go_to_next_page():
                break
            page_number += 1
            time.sleep(5)

        # Post-download: history
        if metadata_lookup:
            portal_hash = compute_net_hash(metadata_lookup)
            first_date, last_date = dates_from_net_metadata(metadata_lookup)
            SyncHistory(case_dir, logger, label=case_dir.name).append(
                portal="NET", total=len(metadata_ids),
                new_downloads=total_ok, re_downloads=len(re_dl_ids), failed=total_fail,
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

        # Global summary — real-time update immediately after this case finishes
        _update_global_summary(
            case_dir=case_dir,
            raw_case_name=raw_case_name or case_name,
            manifest=manifest,
            metadata_lookup=metadata_lookup,
            total_ok=total_ok,
            total_fail=total_fail,
            log_fn=_log,
            compute_net_hash=compute_net_hash,
            dates_from_net_metadata=dates_from_net_metadata,
            _append_fn=_append_net_to_global_summary,
        )

        _log(f"  Done: {total_ok} downloaded, {total_fail} failed.")
        already_processed.add(display_id)
        processed_this_run.append(display_id)

        # Return to original case for next iteration
        _return_to_original_url(page, original_url, logger)

        # Re-navigate to תיקים קשורים for next case
        if idx < len(related):
            if not navigate_to_related_cases(page, logger):
                _log("Could not re-open תיקים קשורים — stopping.", "warn")
                break
            time.sleep(0.5)

    _log(
        f"Related cases complete — {len(processed_this_run)} processed, "
        f"{len(already_processed)} total seen."
    )
    return processed_this_run


def _update_global_summary(
    case_dir: "Path",
    raw_case_name: str,
    manifest,
    metadata_lookup: dict,
    total_ok: int,
    total_fail: int,
    log_fn,
    compute_net_hash,
    dates_from_net_metadata,
    _append_fn,
) -> None:
    """Write / update this case's row in all_cases_summary.csv immediately."""
    try:
        _pending = sum(
            1 for r in manifest.records
            if r.get("סטטוס הורדה") in ("Pending", "Failed")
        )
        _ph = compute_net_hash(metadata_lookup) if metadata_lookup else ""
        _fd, _ld = dates_from_net_metadata(metadata_lookup) if metadata_lookup else ("", "")
        _append_fn(
            case_dir=case_dir,
            raw_case_name=raw_case_name,
            parties_str=manifest.parties or "",
            representatives_str=manifest.representative or "",
            total=len(manifest.records),
            downloaded=total_ok,
            failed=total_fail,
            portal_hash=_ph,
            first_date=_fd,
            last_date=_ld,
            pending=_pending,
        )
    except Exception as _e:
        log_fn(f"  Global summary update failed: {_e}", "warn")


def _case_dir_exists(display_id: str, root_output_dir: Path) -> bool:
    """Check if any download folder name contains this display_id."""
    downloads_dir = root_output_dir / "downloads"
    if not downloads_dir.exists():
        return False
    import re as _re
    safe = _re.escape(display_id)
    return any(_re.search(safe, d.name) for d in downloads_dir.iterdir() if d.is_dir())


def _return_to_original_url(
    page: "Page",
    original_url: str,
    logger: "Logger | None" = None,
) -> None:
    """Navigate back to the original case via its saved URL — no form-fill needed."""
    if not original_url or original_url in ("about:blank", ""):
        return
    try:
        page.goto(original_url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(1.5)
        print(f"[RelatedCases] Returned to original case URL.")
    except Exception as e:
        print(f"[RelatedCases] Could not return to original URL: {e}")
        if logger:
            logger.warn(f"[RelatedCases] URL-based return failed: {e}")
