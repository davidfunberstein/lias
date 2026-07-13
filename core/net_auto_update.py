"""NET HaMishpat auto-update — scan all existing NET case directories and update them.

Scans all directories under root_output_dir/downloads/ recursively for summary.csv files.
For each found directory, parses the case number from the folder name, navigates to that
case on NET portal, and runs a full download update cycle.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from core.logger import Logger

NET_HOME_URL = "https://www.court.gov.il/ngcs.web.site/homepage.aspx"


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def _find_case_dirs_with_manifest(downloads_dir: Path) -> list[Path]:
    """Recursively find all directories that contain a summary CSV file.

    Matches both the new ``summary — *.csv`` pattern and the legacy ``summary.csv``.
    """
    seen: set[Path] = set()
    result: list[Path] = []
    if not downloads_dir.exists():
        return result
    # New meaningful names first
    for csv_file in downloads_dir.rglob("summary — *.csv"):
        p = csv_file.parent
        if p not in seen:
            seen.add(p)
            result.append(p)
    # Legacy fallback
    for csv_file in downloads_dir.rglob("summary.csv"):
        p = csv_file.parent
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def run_net_auto_update(
    page: "Page",
    logger: "Logger | None",
    root_output_dir: Path,
    session_settings: dict,
    resolve_paths_fn,
) -> None:
    """Scan all existing NET case directories and update each one automatically.

    For each directory under root_output_dir/downloads/ that:
    1. Contains a summary.csv file
    2. Has a folder name from which a NET case number can be parsed

    The function navigates to that case on NET portal and runs a download update cycle.
    """
    def _log(msg: str, level: str = "info") -> None:
        print(f"{_ts()} [AutoUpdate] {msg}")
        if logger:
            getattr(logger, level, logger.info)(f"[AutoUpdate] {msg}")

    from core.net_case_navigator import parse_net_case_number, navigate_to_case_by_number
    from core.net_navigation import NetNavigator, NoTikNiyarTab
    from core.net_scraper import NetScraper
    from core.manifest import ManifestManager, get_summary_csv_path
    from core.sync_history import SyncHistory, compute_net_hash, dates_from_net_metadata
    from core.download import extract_case_folder_name

    downloads_dir = root_output_dir / "downloads"

    _log(f"Scanning for case directories under: {downloads_dir}")
    case_dirs = _find_case_dirs_with_manifest(downloads_dir)

    if not case_dirs:
        print(f"{_ts()} [AutoUpdate] No case directories with summary.csv found under {downloads_dir}.")
        return

    # Filter to only those with parseable NET case numbers
    net_cases: list[tuple[Path, str, str]] = []  # (path, case_number, month_year)
    skipped = []
    for case_dir in case_dirs:
        parsed = parse_net_case_number(case_dir.name)
        if parsed:
            net_cases.append((case_dir, parsed[0], parsed[1]))
        else:
            skipped.append(case_dir.name)

    _log(f"Found {len(net_cases)} parseable NET case(s). Skipped {len(skipped)} (no parseable case number).")
    if skipped:
        _log(f"Skipped directories: {skipped}")

    if not net_cases:
        print(f"{_ts()} [AutoUpdate] No NET cases with parseable case numbers found. Exiting.")
        return

    # Show list and confirm
    print(f"\n{_ts()} [AutoUpdate] Cases to update:")
    for i, (case_dir, case_num, mmyy) in enumerate(net_cases, 1):
        print(f"  {i}. {case_dir.name}  (case #{case_num}, period {mmyy[:2]}/{mmyy[2:]})")
    print()

    from core.gov_login import (
        _is_already_logged_in_net,
        handle_net_portal_entry,
        is_gov_login_page,
    )
    from core.connection import _run_gov_autologin

    def _ensure_on_secured_portal() -> bool:
        """Ensure the browser is on securesso.court.gov.il (authenticated portal).

        Returns True when we are on the secured portal.
        Case search from the PUBLIC homepage does NOT open the case in the secured
        portal — it must be entered while already on securesso.
        """
        current_url = page.url or ""

        # Already on secured portal — nothing to do
        if "securesso.court.gov.il" in current_url:
            _log("Already on secured NET portal.")
            return True

        # Redirected to login — re-authenticate first
        if is_gov_login_page(page):
            _log("Session expired — re-authenticating...", "warn")
            _run_gov_autologin(page, "NET")
            time.sleep(2)

        # On public homepage — click "הזדהות לאומית" to enter secured portal
        _log("Entering secured NET portal via authentication button...")
        page.goto(NET_HOME_URL, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            time.sleep(1.5)

        handle_net_portal_entry(page)   # clicks "הזדהות לאומית"
        _run_gov_autologin(page, "NET") # handles passkey / login.gov.il flow

        # Wait until we land on securesso
        try:
            page.wait_for_url("*securesso.court.gov.il*", timeout=20000)
            _log("Now on secured NET portal.")
            time.sleep(1.5)
            return True
        except Exception:
            # May have landed there but URL check failed — verify by content
            if "securesso.court.gov.il" in (page.url or ""):
                return True
            _log("Could not confirm secured portal after auth.", "warn")
            return False

    # ── Initial portal entry ──
    _ensure_on_secured_portal()
    page.bring_to_front()

    # Process each case
    total_updated = 0
    total_skipped = 0
    total_failed = 0

    run_timestamp = session_settings.get("run_timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    for idx, (case_dir, case_num, mmyy) in enumerate(net_cases, 1):
        print(f"\n{_ts()} [AutoUpdate] [{idx}/{len(net_cases)}] Processing: {case_dir.name}")

        # Ensure we're on the secured portal before each case
        # (session may have expired, or we may have drifted to public site)
        secured = _ensure_on_secured_portal()
        if not secured:
            _log(f"Cannot reach secured portal for case {case_num} — skipping.", "error")
            total_failed += 1
            continue

        # Navigate to the specific case
        success = navigate_to_case_by_number(page, case_num, mmyy, logger=logger)
        if not success:
            _log(f"Could not navigate to case {case_num}/{mmyy} — skipping.", "warn")
            total_skipped += 1
            continue

        # Allow page to settle
        time.sleep(3)

        # Initialize navigator and scraper
        nav = NetNavigator(page, logger=logger)
        scraper = NetScraper(page, logger=logger)

        # Get full case name from portal toolbar (used for summary + history label)
        raw_case_name = scraper.get_case_name_from_ui() or case_dir.name
        _log(f"Case {case_num}: full name from portal: '{raw_case_name}'")

        # Navigate to תיק נייר
        try:
            nav.navigate_to_tik_niyar()
        except NoTikNiyarTab as e:
            _log(f"No 'תיק נייר' tab for case {case_num}: {e} — skipping.", "warn")
            total_skipped += 1
            continue
        except Exception as e:
            _log(f"Error navigating to תיק נייר for case {case_num}: {e} — skipping.", "error")
            total_skipped += 1
            continue

        # Load manifest and sync with disk
        manifest = ManifestManager(
            get_summary_csv_path(case_dir),
            run_timestamp=run_timestamp,
            logger=logger,
        )
        manifest.sync_with_disk(case_dir)

        successful_ids = manifest.get_successful_ids()
        re_dl_ids = manifest.get_missing_ids()

        # Extract metadata and pre-populate manifest (all docs as Pending before download)
        metadata_lookup: dict = scraper.extract_metadata()
        _log(f"Case {case_num}: {len(metadata_lookup)} metadata entries.")
        scraper.pre_populate_manifest_from_metadata(case_dir, manifest, metadata_lookup)

        # Smart-skip check
        metadata_ids = set(metadata_lookup.keys())
        if metadata_ids and metadata_ids.issubset(successful_ids) and not re_dl_ids:
            _log(f"Case {case_num}: all documents already synced — smart skip.")
            portal_hash = compute_net_hash(metadata_lookup)
            first_date, last_date = dates_from_net_metadata(metadata_lookup)
            sh = SyncHistory(case_dir, logger, label=case_dir.name)
            prev_hash = sh.last_hash()
            note = ""
            if prev_hash and prev_hash != portal_hash:
                note = "חתימת פורטל השתנתה ללא הורדות חדשות — ייתכן שמסמך הוסר"
                print(f"\n{_ts()} [WARN] {note}")
                if logger:
                    logger.warn(note)
            drive_uploaded = sum(1 for r in manifest.records if r.get("עלה לDrive"))
            sh.append(
                portal="NET",
                total=len(metadata_ids),
                new_downloads=0,
                re_downloads=0,
                failed=0,
                first_date=first_date,
                last_date=last_date,
                portal_hash=portal_hash,
                note=note,
                drive_uploads=drive_uploaded,
            )
            total_skipped += 1
            continue

        # Page-by-page download loop
        page_number = 1
        total_ok = 0
        total_fail = 0

        while True:
            _log(f"Case {case_num}: scanning page {page_number}...")
            links_found, page_dl, page_fail = scraper.scrape_and_download_current_page(
                case_dir=case_dir,
                manifest=manifest,
                metadata_lookup=metadata_lookup,
                global_idx_start=len(successful_ids) + total_ok,
                re_download_ids=re_dl_ids,
            )

            if links_found == 0:
                _log(f"Case {case_num}: no download links on page {page_number} — stopping pagination.")
                break

            total_ok += len(page_dl)
            total_fail += len(page_fail)

            if not scraper.go_to_next_page():
                _log(f"Case {case_num}: no further pages.")
                break

            page_number += 1
            time.sleep(5)

        # Retry failed downloads once
        retry_ids_au = manifest.get_failed_ids()
        if retry_ids_au:
            _log(f"Case {case_num}: retrying {len(retry_ids_au)} failed download(s)...")
            print(f"\n  🔄 מנסה שוב {len(retry_ids_au)} הורדות שנכשלו...")
            try:
                scraper.page.goto(scraper.page.url, wait_until="domcontentloaded", timeout=15000)
                time.sleep(2)
                nav.navigate_to_tik_niyar()
                time.sleep(1)
            except Exception as _re_nav_au:
                _log(f"Could not navigate back for retry: {_re_nav_au}", "warn")

            retry_meta_au = scraper.extract_metadata()
            retry_dl_au, retry_fail_au = [], []
            while True:
                found_r, rdl_r, rfail_r = scraper.scrape_and_download_current_page(
                    case_dir=case_dir,
                    manifest=manifest,
                    metadata_lookup=retry_meta_au or metadata_lookup,
                    global_idx_start=len(manifest.get_successful_ids()),
                    re_download_ids=retry_ids_au,
                )
                if found_r == 0:
                    break
                retry_dl_au.extend(rdl_r)
                retry_fail_au.extend(rfail_r)
                if not scraper.go_to_next_page():
                    break
                time.sleep(3)

            total_ok += len(retry_dl_au)
            total_fail -= min(total_fail, len(retry_dl_au))
            total_fail += len(retry_fail_au)
            if logger:
                logger.info(f"Retry: {len(retry_dl_au)} recovered, {len(retry_fail_au)} still failed.")

        # Post-download: sync history
        if metadata_lookup:
            portal_hash = compute_net_hash(metadata_lookup)
            first_date, last_date = dates_from_net_metadata(metadata_lookup)
            sh = SyncHistory(case_dir, logger, label=case_dir.name)
            prev_hash = sh.last_hash()
            note = ""
            if prev_hash and prev_hash != portal_hash and total_ok == 0 and total_fail == 0:
                note = "חתימת פורטל השתנתה ללא הורדות חדשות — ייתכן שמסמך הוסר"
                print(f"\n{_ts()} [WARN] {note}")
                if logger:
                    logger.warn(note)
            drive_uploaded = sum(1 for r in manifest.records if r.get("עלה לDrive"))
            sh.append(
                portal="NET",
                total=len(metadata_ids),
                new_downloads=total_ok,
                re_downloads=len(re_dl_ids),
                failed=total_fail,
                first_date=first_date,
                last_date=last_date,
                portal_hash=portal_hash,
                note=note,
                drive_uploads=drive_uploaded,
            )

        manifest.print_summary(logger)

        # Decisions + viewers update (merge תיק נייר פירוט + החלטות)
        try:
            from core.net_decisions import NetDecisionsScraper
            _decisions_scraper = NetDecisionsScraper(page, case_dir, logger=logger)
            _tn_viewers = _decisions_scraper.collect_tik_niyar_viewers()
            _decisions_scraper.update_all_decisions(manifest=manifest, extra_viewers=_tn_viewers)
        except Exception as _de:
            _log(f"Decisions/viewers update skipped: {_de}", "warn")

        _log(f"Case {case_num}: {total_ok} downloaded, {total_fail} failed.")

        # Update global summary
        try:
            from core.download import _append_net_to_global_summary
            from core.sync_history import compute_net_hash, dates_from_net_metadata
            _parties_str = manifest.parties or ""
            _reps_str = manifest.representative or ""
            _pending_au = sum(1 for r in manifest.records if r.get("סטטוס הורדה") in ("Pending", "Failed"))
            _ph = compute_net_hash(metadata_lookup) if metadata_lookup else ""
            _fd, _ld = dates_from_net_metadata(metadata_lookup) if metadata_lookup else ("", "")
            _append_net_to_global_summary(
                case_dir=case_dir,
                raw_case_name=raw_case_name,
                parties_str=_parties_str,
                representatives_str=_reps_str,
                total=len(manifest.records),
                downloaded=total_ok,
                failed=total_fail,
                portal_hash=_ph,
                first_date=_fd,
                last_date=_ld,
                pending=_pending_au,
            )
        except Exception as _exc:
            _log(f"Global summary update skipped: {_exc}", "warn")

        # Related cases — if enabled
        if session_settings.get("download_related_cases"):
            try:
                from core.net_related_cases import process_related_cases
                process_related_cases(
                    page=page,
                    original_case_num=case_num,
                    original_mmyy=mmyy,
                    root_output_dir=root_output_dir,
                    session_settings=session_settings,
                    resolve_paths_fn=resolve_paths_fn,
                    logger=logger,
                )
            except Exception as _re:
                _log(f"Related cases skipped: {_re}", "warn")

        if total_ok > 0 or total_fail == 0:
            total_updated += 1
        else:
            total_failed += 1

    # Final report
    print(f"\n{_ts()} [AutoUpdate] ===== AUTO-UPDATE COMPLETE =====")
    print(f"{_ts()} [AutoUpdate]   Cases processed:  {len(net_cases)}")
    print(f"{_ts()} [AutoUpdate]   Updated/synced:   {total_updated}")
    print(f"{_ts()} [AutoUpdate]   Already current:  {total_skipped}")
    print(f"{_ts()} [AutoUpdate]   Failed/skipped:   {total_failed}")
    print(f"{_ts()} [AutoUpdate] ====================================")
    if logger:
        logger.info(
            f"[AutoUpdate] Complete: {total_updated} updated, "
            f"{total_skipped} skipped, {total_failed} failed."
        )
