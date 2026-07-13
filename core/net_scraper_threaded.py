"""NET HaMishpat threaded mode — EXPERIMENTAL.

ALL Playwright operations run in the main (calling) thread.
A lightweight daemon thread handles keyboard input only, so the user can
type 'stop' or 'status' while a download is in progress.

Root cause of the old architecture's crash:
  Playwright sync_api binds each Page to the greenlet that created it.
  Calling page operations from a background thread raises
  "Cannot switch to a different thread".

Fix:
  Main thread  → all Playwright / NetNavigator / NetScraper calls
  Daemon thread → input() reads only; puts lines into a queue
"""

from __future__ import annotations

import queue
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from core.doc_classifier import classify_doc
from core.manifest import ManifestManager
from core.net_navigation import NetNavigator, NoTikNiyarTab
from core.net_scraper import NetScraper

if TYPE_CHECKING:
    from core.logger import Logger

NET_URL = "https://www.court.gov.il/ngcs.web.site/homepage.aspx"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_net_threaded(
    logger: "Logger | None",
    session_settings: dict,
    root_output_dir: Path,
    resolve_paths_fn,
    existing_page=None,
    drive_manager=None,
) -> None:
    """
    NET HaMishpat threaded download session.

    All Playwright operations stay in the calling (main) thread.
    A background daemon thread reads keyboard input so the user can
    send 'stop' or 'status' commands while the download runs.
    Ctrl+C also stops cleanly.
    """
    if existing_page is None:
        print("[ERROR] NET threaded mode requires an existing browser page.")
        return

    page = existing_page

    def _log(msg: str, level: str = "info") -> None:
        pfx = "[NET Threaded]"
        if logger:
            getattr(logger, level)(f"{pfx} {msg}")
        else:
            print(f"{pfx} {msg}")

    # ── Navigate to NET portal — main thread ──────────────────────────────
    try:
        page.goto(NET_URL, wait_until="domcontentloaded")
        page.bring_to_front()
    except Exception as e:
        _log(f"Navigation to NET failed: {e}", "error")
        return

    # ── Handle NET portal entry: dismiss popup + click הזדהות לאומית ──────
    try:
        from core.gov_login import handle_net_portal_entry, is_gov_login_page, auto_login_flow
        handle_net_portal_entry(page, logger=logger)

        # Wait up to 8s for redirect to login.gov.il
        import time as _time
        deadline = _time.time() + 8
        while _time.time() < deadline:
            if is_gov_login_page(page):
                break
            _time.sleep(0.5)

        if is_gov_login_page(page):
            _log("Redirected to login.gov.il — starting auto-login...")
            # Build email reader if configured
            _email_reader = None
            try:
                from core.email_otp import EmailOTPReader, load_email_config
                from pathlib import Path as _Path
                _cfg = load_email_config(_Path("email_config.json"))
                if _cfg:
                    _email_reader = EmailOTPReader(_cfg, logger)
            except Exception:
                pass
            auto_login_flow(page, email_reader=_email_reader, logger=logger)
            # Wait for redirect back to NET after successful login
            _time.sleep(2)
    except Exception as e:
        _log(f"Auto-login error (continuing manually): {e}", "warn")

    print("\n" + "*" * 60)
    print(">>> STATUS: Browser is on NET HaMishpat portal.")
    print(">>> 1. Navigate to the desired case.")
    print(">>> 2. Ensure the document grid (תיק נייר) is visible.")
    print(">>> 3. Press ENTER to start download, or type 'b' to go back.")
    print("*" * 60)

    # ── Wait for Enter — MAIN THREAD (Ctrl+C works natively here) ─────────
    try:
        ans = input(">>> [Enter / b]: ").strip().lower()
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted — returning to main menu.")
        return
    if ans in ("b", "back", "q", "stop"):
        _log("User returned to main menu.")
        print("[INFO] Returning to main menu.")
        return

    # ── Start background input reader ONLY NOW (during download) ─────────
    stop_event = threading.Event()
    user_input_q: queue.Queue = queue.Queue()

    def _read_input() -> None:
        while not stop_event.is_set():
            try:
                line = input()
                user_input_q.put(line.strip().lower())
            except EOFError:
                break

    input_thread = threading.Thread(target=_read_input, daemon=True, name="NetInput")
    input_thread.start()

    def _check_input() -> str | None:
        """Non-blocking peek at next user command."""
        try:
            return user_input_q.get_nowait()
        except queue.Empty:
            return None

    def _handle_cmd(cmd: str) -> bool:
        """Handle a user command. Returns True if download should stop."""
        if cmd in ("stop", "b", "q"):
            _log("Download stopped by user.", "warn")
            stop_event.set()
            return True
        if cmd == "status":
            print(f"\n[STATUS] downloaded: {total_ok}, failed: {total_fail}\n")
        elif cmd == "d":
            if drive_manager is not None:
                drive_manager.live_view()
            else:
                print("\n  [Drive] Not active in this session.\n")
        elif cmd in ("stop-up", "x"):
            if drive_manager is not None:
                print("[Drive] Stopping uploads...")
                drive_manager.stop(wait=False)
            else:
                print("  [Drive] Not active.")
        elif cmd in ("stop-all", "qq"):
            stop_event.set()
            if drive_manager is not None:
                drive_manager.stop(wait=False)
            return True
        return False

    # ── Download — all Playwright ops in main thread ──────────────────────
    print("\n[NET Threaded] Download starting...")
    _drive_hint = "  d=drive-log | stop-up=stop-uploads | " if drive_manager else ""
    print(f"Commands: status | stop | {_drive_hint}stop-all\n")

    total_ok = 0
    total_fail = 0
    case_dir: Path | None = None
    raw_case_name = ""
    manifest: ManifestManager | None = None

    try:
        nav = NetNavigator(page, logger=logger)
        scraper = NetScraper(page)

        # Extract parties + case name
        parties = nav.extract_parties()
        raw_case_name = scraper.get_case_name_from_ui()
        _log(f"Case: '{raw_case_name}' | Parties: {parties}")

        # Navigate to תיק נייר tab
        try:
            nav.navigate_to_tik_niyar()
        except NoTikNiyarTab as e:
            _log(f"תיק נייר not found: {e}", "error")
            print("[INFO] Returning to main menu.")
            stop_event.set()
            return

        # Resolve output directory
        from core.download import extract_case_folder_name
        case_folder = extract_case_folder_name(raw_case_name)
        safe_folder = re.sub(r'[\\/*?:"<>|]', "", case_folder).strip()
        try:
            case_dir = resolve_paths_fn(parties, safe_folder)
        except Exception:
            case_dir = root_output_dir / "downloads" / safe_folder
            case_dir.mkdir(parents=True, exist_ok=True)
        _log(f"Directory: {case_dir}")

        # Load manifest
        manifest = ManifestManager(
            case_dir / "summary.csv",
            run_timestamp=session_settings.get("run_timestamp", ""),
            logger=logger,
        )
        manifest.sync_with_disk(case_dir)
        successful_ids: set = manifest.get_successful_ids()
        re_dl_ids: set = manifest.get_missing_ids()

        # Extract document metadata
        metadata_lookup: dict = scraper.extract_metadata()
        _log(f"Metadata: {len(metadata_lookup)} entries.")

        # Smart-skip check
        from core.sync_history import (
            SyncHistory, compute_net_hash, dates_from_net_metadata,
        )
        if metadata_lookup and set(metadata_lookup.keys()).issubset(successful_ids):
            _log("All documents already in manifest — smart skip.")
            portal_hash = compute_net_hash(metadata_lookup)
            first_date, last_date = dates_from_net_metadata(metadata_lookup)
            sh = SyncHistory(case_dir, logger)
            prev_hash = sh.last_hash()
            note = ""
            if prev_hash and prev_hash != portal_hash:
                note = "⚠️ חתימת פורטל השתנתה ללא הורדות חדשות — ייתכן שמסמך הוסר"
                print(f"\n[WARN] {note}")
            sh.append(
                portal="NET",
                total=len(metadata_lookup),
                new_downloads=0,
                re_downloads=0,
                failed=0,
                first_date=first_date,
                last_date=last_date,
                portal_hash=portal_hash,
                note=note,
            )
            print("[NET Threaded] All documents already synced — nothing to download.")
            stop_event.set()
            return

        # ── Page-by-page download loop ─────────────────────────────────
        page_number = 1

        while not stop_event.is_set():
            _log(f"Processing page {page_number}...")
            page_ok = 0
            page_fail = 0
            links_found = 0

            for frame in page.frames:
                try:
                    if frame.is_detached():
                        continue
                    locator = frame.locator("a[href*='btnDownloadDocument']")
                    count = locator.count()
                    if count == 0:
                        continue
                    links_found = count

                    for idx in range(count):
                        # Check for stop/status/drive commands between files
                        if stop_event.is_set():
                            break
                        cmd = _check_input()
                        if cmd and _handle_cmd(cmd):
                            break

                        link = locator.nth(idx)
                        href = (link.get_attribute("href") or "").replace("&amp;", "&")
                        id_match = (
                            re.search(r"[\d]+&([\d]+)", href)
                            or re.search(r"(\d{8,11})", href)
                        )
                        if not id_match:
                            continue

                        doc_id = str(id_match.group(1))
                        is_redownload = doc_id in re_dl_ids

                        if doc_id in successful_ids and not is_redownload:
                            continue

                        doc_meta = metadata_lookup.get(doc_id, {})
                        doc_type = doc_meta.get("DocumentType", "מסמך")
                        party_name = (doc_meta.get("CasePartyDisplayName") or "").strip()
                        raw_date = (doc_meta.get("PresentationDate") or "").strip()
                        doc_desc = (doc_meta.get("DocumentDesc") or "").strip()

                        def _clean(s: str) -> str:
                            return re.sub(r'[\\*?"<>|]', "", s.replace("/", " ")).strip()

                        clean_type = _clean(doc_type)
                        clean_party = _clean(party_name)
                        date_prefix = "0000_00_00"
                        dm = re.search(r"(\d{2})/(\d{2})/(\d{4})", raw_date)
                        if dm:
                            date_prefix = f"{dm.group(3)}_{dm.group(2)}_{dm.group(1)}"

                        if "החלטה" in clean_type:
                            base = f"{date_prefix} - החלטה"
                        elif clean_party:
                            base = f"{date_prefix} - {clean_type} - {clean_party}"
                        else:
                            base = f"{date_prefix} - {clean_type}"

                        base_filename = re.sub(r"\s+", " ", base).strip()
                        target_filename = f"{base_filename}.pdf"
                        target_path = case_dir / target_filename
                        counter = 2
                        while target_path.exists() and not is_redownload:
                            target_filename = f"{base_filename}_{counter}.pdf"
                            target_path = case_dir / target_filename
                            counter += 1

                        date_part = raw_date.split()[0] if raw_date else ""
                        time_part = raw_date.split()[1] if len(raw_date.split()) > 1 else ""

                        base_record = {
                            "שם מסמך (מהטבלה)": doc_desc or doc_type,
                            "שם קובץ מקורי (מהשרת)": "",
                            "תאריך מסמך": date_part,
                            "שעת מסמך": time_part,
                            "סוג קובץ": doc_type,
                            "מגיש": party_name,
                            "מזהה ייחודי": doc_id,
                            "שם קובץ פיזי בדיסק": target_filename,
                            "גודל (KB)": "0",
                            "סטטוס הורדה": "Pending",
                            "סיווג מסמך": classify_doc(doc_desc, doc_type),
                        }

                        _log(f"Downloading: {target_filename} (ID {doc_id})")

                        try:
                            link.scroll_into_view_if_needed(timeout=2000)
                            with page.expect_download(timeout=45000) as dl_info:
                                link.click(force=True)
                                scraper.handle_error_modal()

                            dl = dl_info.value
                            original_name = dl.suggested_filename
                            dl.save_as(str(target_path))
                            size_kb = str(round(target_path.stat().st_size / 1024, 2))

                            # Count pages from the downloaded PDF
                            from core.net_scraper import count_pdf_pages
                            page_count_str = count_pdf_pages(target_path)

                            viewers = scraper.get_document_viewers(doc_id)
                            record = {
                                **base_record,
                                "שם קובץ מקורי (מהשרת)": original_name,
                                "גודל (KB)": size_kb,
                                "סטטוס הורדה": "Success",
                                "צפיות": viewers,
                                "מספר עמודים": page_count_str,
                            }
                            manifest.upsert(record)
                            successful_ids.add(doc_id)
                            total_ok += 1
                            _log(f"OK: {target_filename} ({size_kb} KB)", "ok")

                        except Exception as e:
                            scraper.handle_error_modal()
                            record = {
                                **base_record,
                                "סטטוס הורדה": f"Failed ({str(e)[:60]})",
                            }
                            manifest.upsert(record)
                            total_fail += 1
                            _log(f"FAILED: {target_filename} — {e}", "error")

                        time.sleep(1.5)

                    break  # found the right frame

                except Exception:
                    continue

            _log(f"Page {page_number} done — links: {links_found}, ok: {page_ok}, fail: {page_fail}")

            if links_found == 0 or stop_event.is_set():
                break

            try:
                if not scraper.go_to_next_page():
                    break
                page_number += 1
                time.sleep(5)
            except Exception as e:
                _log(f"Pagination error: {e}", "error")
                break

        # ── Post-download: hash + sync_history ────────────────────────
        if manifest:
            manifest.print_summary(logger)
        print(f"\n[NET Threaded] Complete: {total_ok} downloaded, {total_fail} failed.")

        if case_dir and metadata_lookup:
            portal_hash = compute_net_hash(metadata_lookup)
            first_date, last_date = dates_from_net_metadata(metadata_lookup)
            sh = SyncHistory(case_dir, logger)
            prev_hash = sh.last_hash()
            note = ""
            if prev_hash and prev_hash != portal_hash and total_ok == 0 and total_fail == 0:
                note = "⚠️ חתימת פורטל השתנתה ללא הורדות — ייתכן שמסמך הוסר"
                print(f"\n[WARN] {note}")
            sh.append(
                portal="NET",
                total=len(metadata_lookup),
                new_downloads=total_ok,
                re_downloads=0,
                failed=total_fail,
                first_date=first_date,
                last_date=last_date,
                portal_hash=portal_hash,
                note=note,
            )

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C — download stopped, browser stays open.")
        if logger:
            logger.info("NET threaded mode interrupted by Ctrl+C.")

    except Exception as e:
        _log(f"Unexpected error: {e}", "error")

    finally:
        stop_event.set()

    print("[INFO] Returning to main menu.")
