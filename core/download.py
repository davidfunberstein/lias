"""Download router and session orchestrator for BDR & NET portals.

Global session logger is created in configure_session_settings() and made
available to all navigators/scrapers via module-level _logger.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page

from core.bdr_navigation import BdrNavigator
from core.i18n import t
from core.logger import Logger
from core.manifest import ManifestManager, get_summary_csv_path
from core.net_navigation import NetNavigator, NoTikNiyarTab
from core.net_scraper import NetScraper

ROOT_OUTPUT_DIR = Path("court_documents")
SHARED_PROFILE_DIR = "./browser_profile"

SESSION_SETTINGS: dict = {
    "mode": "1",
    "date_filter": "n",
    "start_date": None,
    "end_date": None,
    "run_timestamp": None,
    "target_portal": "UNKNOWN",
    # Email OTP auto-login
    "email_enabled": False,
    "email_config_path": Path("email_config.json"),
    # Viewers tracking
    "check_viewers": True,
}

_logger: Logger | None = None


def get_logger() -> Logger | None:
    return _logger


# ---------------------------------------------------------------------------
# Session configuration & log rotation
# ---------------------------------------------------------------------------

def init_session_logger() -> None:
    """Initialise the session: set run_timestamp, rotate logs, create Logger.

    No prompts — called once at program start before any user interaction.
    """
    global _logger

    SESSION_SETTINGS["run_timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logs_dir = ROOT_OUTPUT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    latest_log = logs_dir / "latest.log"
    if latest_log.exists() and latest_log.stat().st_size > 0:
        for idx in range(19, 0, -1):
            cur = logs_dir / f"log_{idx}.log"
            nxt = logs_dir / f"log_{idx + 1}.log"
            if cur.exists():
                if idx == 19 and nxt.exists():
                    try:
                        nxt.unlink()
                    except Exception:
                        pass
                try:
                    cur.rename(nxt)
                except Exception:
                    pass
        try:
            latest_log.rename(logs_dir / "log_1.log")
        except Exception:
            pass

    _logger = Logger(latest_log)
    _logger.info(f"Session started. Timestamp: {SESSION_SETTINGS['run_timestamp']}")


def configure_download_settings() -> None:
    """Prompt for download mode and date filter. Called from the settings submenu."""
    if not _logger:
        init_session_logger()

    print("\n" + "=" * 50)
    print("DOWNLOAD SETTINGS")
    print("=" * 50)

    print("Select Download Mode:")
    print("  1. Updates Only (skip existing files) [Default]")
    print("  2. Full Re-download (everything from scratch)")
    mode = input("Enter choice (1/2): ").strip()
    SESSION_SETTINGS["mode"] = "2" if mode == "2" else "1"
    _logger.info(f"Mode: {'2 (Full Re-download)' if SESSION_SETTINGS['mode'] == '2' else '1 (Updates Only)'}")

    date_opt = input("\nApply a global date range filter? (y/n) [Default: n]: ").strip().lower()
    if date_opt == "y":
        try:
            print("Enter dates in DD/MM/YYYY format:")
            start_str = input("START date: ").strip()
            end_str = input("END date: ").strip()
            SESSION_SETTINGS["start_date"] = datetime.strptime(start_str, "%d/%m/%Y")
            SESSION_SETTINGS["end_date"] = datetime.strptime(end_str, "%d/%m/%Y")
            SESSION_SETTINGS["date_filter"] = "y"
            _logger.info(f"Date filter: {start_str} — {end_str}")
        except ValueError:
            print("Invalid date format — proceeding without filter.")
            _logger.warn("Date filter input invalid — disabled.")
            SESSION_SETTINGS["date_filter"] = "n"
    else:
        SESSION_SETTINGS["date_filter"] = "n"

    print("=" * 50 + "\n")


def configure_session_settings() -> None:
    """Backwards-compatible alias: init logger then prompt for download settings."""
    init_session_logger()
    configure_download_settings()


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def append_case_to_centralized_log(
    case_name: str,
    total_found: int,
    downloaded_list: list[dict],
    failed_list: list[dict],
    table_updated_list: list[dict],
    snapshot_lines: list[str],
) -> None:
    if not _logger:
        return
    _logger.section(f"Case: {case_name}")
    for line in snapshot_lines:
        _logger.info(line)
    _logger.info(f"Total documents detected:  {total_found}")
    _logger.info(f"New downloads:             {len(downloaded_list)}")
    _logger.info(f"Metadata-only updates:     {len(table_updated_list)}")
    _logger.info(f"Failed downloads:          {len(failed_list)}")
    if failed_list:
        _logger.warn("Failed downloads (manual action required):")
        for idx, item in enumerate(failed_list, 1):
            _logger.warn(
                f"  {idx}. [{item.get('תאריך מסמך', '')}] "
                f"{item.get('שם מסמך (מהטבלה)', '')} | "
                f"Submitter: {item.get('מגיש', '')} | "
                f"Expected: {item.get('שם קובץ פיזי בדיסק', '')}"
            )


def print_terminal_final_dashboard(
    total_found: int,
    downloaded_list: list[dict],
    failed_list: list[dict],
    table_updated_list: list[dict],
) -> None:
    lines = [
        "=" * 60,
        "CASE SYNC SUMMARY REPORT",
        "=" * 60,
        f"  Total portal items:           {total_found}",
        f"  Successfully downloaded:      {len(downloaded_list)}",
        f"  Metadata matched (no-dl):     {len(table_updated_list)}",
        f"  Failed / timed out:           {len(failed_list)}",
        "=" * 60,
    ]
    for line in lines:
        print(line)
    if _logger:
        for line in lines:
            _logger.info(line)

    if failed_list:
        msg = "MANUAL COMPLETION REQUIRED:"
        print(f"\n{msg}")
        if _logger:
            _logger.warn(msg)
        for idx, item in enumerate(failed_list, 1):
            row = (
                f"  {idx}. [{item.get('תאריך מסמך', '')}] "
                f"{item.get('שם מסמך (מהטבלה)', '')} -> "
                f"{item.get('שם קובץ פיזי בדיסק', '')}"
            )
            print(row)
            if _logger:
                _logger.warn(row)
    else:
        msg = "All documents for this case are fully synced."
        print(f"\n{msg}")
        if _logger:
            _logger.ok(msg)
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def extract_case_folder_name(full_case_name: str) -> str:
    """Extract court prefix + case number, discarding party names.

    Two portal numbering styles are supported:
      NET / court prefix:   'תאד״מ 41352-12-24 פונברשטיין נ׳ הבנק' → 'תאד״מ 41352-12-24'
      Rabbinical (BDR):     '1386836-7 הסדרי שהות - גדול'          → '1386836-7 הסדרי שהות - גדול'
                            '996030-2 סידורי גיטין - ירושלים'      → '996030-2 סידורי גיטין - ירושלים'
    The BDR style has NO letter prefix (starts with digits) and the topic
    text IS part of the folder name — so we keep the whole cleaned string.
    """
    s = full_case_name.strip()
    # Rabbinical: 'NNNNNNN-N ...topic...' — starts with 6-7 digits + dash.
    if re.match(r"^\d{6,7}-\d+(\D|$)", s):
        return re.sub(r'[\\/*?:"<>|]', "", s).strip()[:80]
    # NET / court prefix: letters + space + number.
    match = re.match(r"^(\S+\s+\d[\d\-]+)", s)
    if match:
        return match.group(1).strip()
    return re.sub(r'[\\/*?:"<>|]', "", s)[:40].strip()


def _normalize_party(name: str) -> str:
    """Strip legal suffixes like ואח', בע"מ, etc. from a party name."""
    s = name.strip()
    s = re.sub(r"\s+ואח['׳’]?\s*$", "", s)
    s = re.sub(r"\s+בע[\"״]מ\s*$", "", s)
    return s.strip()


def resolve_smart_paths(
    choices: list[str],
    formatted_case_name: str,
    lawyer_mode: bool = False,
) -> Path:
    """Resolve or create downloads/{party}/{case} folder structure.

    Matching priority:
    1. Existing case dir that already contains this exact case number (anywhere under downloads)
    2. Existing dir matching ALL parties (couple/group folder)
    3. Existing dir matching a single party, with no Hebrew names that are foreign to this case
    4. Ask user / create new
    """
    if not formatted_case_name:
        raise ValueError("Could not extract a valid case name from the portal.")

    downloads_base = ROOT_OUTPUT_DIR / "downloads"
    downloads_base.mkdir(parents=True, exist_ok=True)

    choices = [_normalize_party(c) for c in choices]

    cleaned_choices = [c.strip() for c in choices if c and c.strip()]
    final_party_dir: Path | None = None

    safe_case = re.sub(r'[\\*?:"<>|]', "-", formatted_case_name).strip()

    # ── Phase 0: Check if the exact case folder already exists anywhere ──────
    case_num_match = re.search(r"(\d+-\d+-\d+|\d+-\d+|\d+/\d+)", formatted_case_name)
    if case_num_match:
        search_num = case_num_match.group(1).replace("/", "-")
        for parent_dir in downloads_base.iterdir():
            if not parent_dir.is_dir():
                continue
            for sub in parent_dir.iterdir():
                if sub.is_dir() and search_num in sub.name:
                    if _logger:
                        _logger.info(
                            f"[Smart Path] Reusing existing case dir: '{sub}'"
                        )
                    return sub
            # Also check at the parent level itself
            if search_num in parent_dir.name:
                safe_full = parent_dir / safe_case
                if safe_full.exists():
                    return safe_full

    _STOP_HE = {
        "של", "כן", "את", "עם", "לא", "הם", "אל", "על", "כי",
        "בית", "דין", "נייר", "משפט", "תיק",
        "אישות", "גירושין", "מזונות", "ילדים", "רכוש", "שהות",
        "הסדרי", "כריכה", "אפוטרופוס", "מינוי", "גיטין", "סידורי",
        "פתח", "תקוה", "ירושלים", "תלאביב", "גדול", "קטן", "רחובות",
        "נתניה", "חיפה", "אשדוד", "בת", "ים", "רמת", "גן",
        "ואח", "בע",
    }
    all_our_he_words: set[str] = set()
    for side in cleaned_choices:
        all_our_he_words.update(
            w for w in re.findall(r"[א-ת]{2,}", side) if len(w) > 1
        )

    # ── Phase 1: Folder matching ALL parties (couple folder) ─────────────────
    if len(cleaned_choices) >= 2:
        all_words = list(all_our_he_words)
        if all_words:
            for existing_dir in downloads_base.iterdir():
                if existing_dir.is_dir():
                    clean_dir = existing_dir.name.replace("_", " ")
                    if not all(w in clean_dir for w in all_words):
                        continue
                    folder_he = {
                        w for w in re.findall(r"[א-ת]{2,}", clean_dir)
                        if len(w) > 1
                    }
                    foreign = folder_he - all_our_he_words - _STOP_HE
                    if foreign:
                        continue
                    final_party_dir = existing_dir
                    if _logger:
                        _logger.info(
                            f"[Smart Path] All-parties match: '{final_party_dir.name}'"
                        )
                    break

    # ── Phase 2: Single-party match — exclude folders with foreign names ─────
    # "Foreign" = Hebrew words in folder that belong to NONE of the current parties
    if not final_party_dir:
        for side in cleaned_choices:
            party_he_words = [
                w for w in re.findall(r"[א-ת]{2,}", side) if len(w) > 1
            ]
            if not party_he_words:
                continue
            for existing_dir in downloads_base.iterdir():
                if not existing_dir.is_dir():
                    continue
                clean_dir = existing_dir.name.replace("_", " ")
                # Must contain all words of this party
                if not all(w in clean_dir for w in party_he_words):
                    continue
                # Check for "foreign" Hebrew words in folder name
                folder_he = {
                    w for w in re.findall(r"[א-ת]{2,}", clean_dir)
                    if len(w) > 1
                }
                foreign = folder_he - all_our_he_words - _STOP_HE
                if foreign:
                    # Folder contains Hebrew names not in our party list → skip
                    if _logger:
                        _logger.info(
                            f"[Smart Path] Skipping '{existing_dir.name}' "
                            f"— foreign words: {foreign}"
                        )
                    continue
                final_party_dir = existing_dir
                if _logger:
                    _logger.info(
                        f"[Smart Path] Matched existing party dir: '{final_party_dir.name}'"
                    )
                break
            if final_party_dir:
                break

    # ── Phase 3: No match — auto-select (lawyer) or ask (private) ───────────
    if not final_party_dir:
        if lawyer_mode:
            # Lawyer mode: never ask — build a folder from all parties combined
            # e.g. "דוד פונברשטיין - קייטלין (חדוה) בר"
            if len(cleaned_choices) >= 2:
                combined = " - ".join(cleaned_choices[:2])
            elif cleaned_choices:
                combined = cleaned_choices[0]
            else:
                combined = "General_Cases"
            selected_party = combined
            if _logger:
                _logger.info(
                    f"[Smart Path] Lawyer mode — auto-creating folder: '{selected_party}'"
                )
        elif len(cleaned_choices) > 1:
            # In LIAS mode (no terminal): auto-combine like lawyer mode.
            if SESSION_SETTINGS.get("lias_mode"):
                combined = " - ".join(cleaned_choices[:2])
                selected_party = combined
                if _logger:
                    _logger.info(f"[Smart Path] LIAS mode — auto-folder: '{selected_party}'")
            else:
                print("\n" + "?" * 50)
                print(t("smart_path_title"))
                for idx, side in enumerate(cleaned_choices, 1):
                    print(f"  {idx}. {side}")
                print("?" * 50)
                while True:
                    user_sel = input(t("smart_path_prompt", n=len(cleaned_choices))).strip()
                    if user_sel.isdigit() and 1 <= int(user_sel) <= len(cleaned_choices):
                        selected_party = cleaned_choices[int(user_sel) - 1]
                        break
                    print(t("smart_path_invalid"))
        else:
            selected_party = cleaned_choices[0] if cleaned_choices else "General_Cases"

        final_party_dir = downloads_base / re.sub(r'[\\/*?:"<>|]', "-", selected_party).strip()
        final_party_dir.mkdir(parents=True, exist_ok=True)
        if _logger:
            _logger.info(f"[Smart Path] {t('smart_path_created')}'{final_party_dir.name}'")

    # ── Build case dir ────────────────────────────────────────────────────────
    final_case_dir = final_party_dir / safe_case

    if case_num_match:
        case_number = case_num_match.group(1).replace("/", "-")
        for sub in final_party_dir.iterdir():
            if sub.is_dir() and case_number in sub.name.replace("/", "-"):
                final_case_dir = sub
                if _logger:
                    _logger.info(f"[Smart Path] Matched existing case dir: '{sub.name}'")
                break

    final_case_dir.mkdir(parents=True, exist_ok=True)
    if _logger:
        _logger.info(f"Case directory resolved: {final_case_dir.absolute()}")
    return final_case_dir


# ---------------------------------------------------------------------------
# BDR download
# ---------------------------------------------------------------------------

def run_bdr_download(page: Page, output_dir: Path | None = None) -> None:
    SESSION_SETTINGS["target_portal"] = "BDR (Rabbinical Courts)"
    if _logger:
        _logger.section("BDR Download Session")

    bdr_nav = BdrNavigator(page, logger=_logger)

    bdr_nav.click_documents_tab()
    if _logger:
        _logger.info("Waiting for BDR documents table to render...")
    try:
        page.wait_for_selector("tr[id*='DXDataRow']", timeout=20000)
        time.sleep(2)
    except Exception as e:
        msg = f"BDR documents table did not load within timeout: {e}"
        if _logger:
            _logger.error(msg)
        print(f"[ERROR] {msg}")
        return

    choices, formatted_case_name = bdr_nav.extract_case_details_and_route_raw()
    base_root_dir = output_dir or ROOT_OUTPUT_DIR
    case_dir = resolve_smart_paths(choices, formatted_case_name)

    total_found, downloaded_list, re_download_list, failed_list, table_updated_list, snapshot_lines = (
        bdr_nav.sync_and_download_bdr(case_dir, SESSION_SETTINGS, SESSION_SETTINGS["run_timestamp"])
    )

    append_case_to_centralized_log(
        formatted_case_name, total_found,
        downloaded_list, failed_list, table_updated_list, snapshot_lines,
    )
    print_terminal_final_dashboard(total_found, downloaded_list, failed_list, table_updated_list)

    # Hash & sync history
    from core.sync_history import (
        SyncHistory, compute_bdr_hash, dates_from_bdr_snapshot
    )
    portal_hash = compute_bdr_hash(snapshot_lines)
    first_date, last_date = dates_from_bdr_snapshot(snapshot_lines)
    history = SyncHistory(case_dir, logger=_logger, label=case_dir.name)
    prev_hash = history.last_hash()
    note = ""
    if (
        prev_hash
        and prev_hash != portal_hash
        and len(downloaded_list) == 0
        and len(re_download_list) == 0
        and len(failed_list) == 0
    ):
        note = "⚠️ חתימת פורטל השתנתה ללא הורדות חדשות — ייתכן שמסמך הוסר מהתיק"
        print(f"\n[WARN] {note}")
        if _logger:
            _logger.warn(note)
    changed = history.append(
        portal="BDR",
        total=total_found,
        new_downloads=len(downloaded_list),
        re_downloads=len(re_download_list),
        failed=len(failed_list),
        first_date=first_date,
        last_date=last_date,
        portal_hash=portal_hash,
        note=note,
    )
    if _logger:
        _logger.info(
            f"[SyncHistory] hash={portal_hash} | prev={prev_hash} | changed={changed}"
        )


# ---------------------------------------------------------------------------
# NET download (standard — synchronous)
# ---------------------------------------------------------------------------

def run_net_download(page: Page, output_dir: Path | None = None) -> None:
    SESSION_SETTINGS["target_portal"] = "NET (Net HaMishpat)"
    if _logger:
        _logger.section("NET Download Session")

    nav = NetNavigator(page, logger=_logger)
    scraper = NetScraper(page, logger=_logger)

    # 1. Case name from top toolbar (available on any tab)
    raw_case_name = scraper.get_case_name_from_ui()
    case_folder_name = extract_case_folder_name(raw_case_name)
    safe_case_folder = re.sub(r'[\\/*?:"<>|]', "", case_folder_name).strip()
    if _logger:
        _logger.info(f"Full case string: '{raw_case_name}' → folder: '{safe_case_folder}'")

    # 2. Extract case-level metadata (court, judge, procedure) — best-effort
    case_meta: dict = {}
    try:
        case_meta = nav.extract_case_metadata()
    except Exception:
        pass

    # 3. Extract parties for smart-path resolution (best-effort)
    parties: list[str] = []
    try:
        parties = nav.extract_parties()
    except Exception as e:
        if _logger:
            _logger.warn(f"Party extraction failed — will use case folder directly: {e}")

    # Get full party data (name + role + representative)
    parties_full: list[dict] = []
    try:
        parties_full = nav.extract_parties_full()
    except Exception:
        pass

    parties_str = " | ".join(p.get("name", "") for p in parties_full if p.get("name"))
    representatives_str = " | ".join(
        f"{p.get('name','')} ({p.get('representative','')})"
        for p in parties_full
        if p.get("representative")
    )

    if not parties and parties_full:
        parties = [p.get("name", "") for p in parties_full if p.get("name")]

    # Identify which side our lawyer represents (תובע / נתבע) and save to metadata
    our_side = ""
    _lawyer_name = SESSION_SETTINGS.get("lawyer_name", "")
    if _lawyer_name and parties_full:
        our_side = nav.identify_our_side(parties_full, _lawyer_name)
        if our_side and _logger:
            _logger.info(f"Our side identified: {our_side} (lawyer: {_lawyer_name})")
    if case_meta is not None:
        case_meta["our_side"] = our_side

    # 3. Navigate to תיק נייר
    try:
        nav.navigate_to_tik_niyar()
    except NoTikNiyarTab as exc:
        msg = f"No 'תיק נייר' tab — {exc}"
        print(f"\n[INFO] {msg}")
        print("[INFO] Returning to main menu.")
        if _logger:
            _logger.warn(msg)
        return

    # 4. Resolve case directory: downloads/{party}/{case_number}/
    _lawyer = SESSION_SETTINGS.get("user_mode") == "lawyer"
    try:
        case_dir = resolve_smart_paths(parties, safe_case_folder, lawyer_mode=_lawyer)
    except ValueError:
        fallback = (output_dir or ROOT_OUTPUT_DIR) / "downloads" / safe_case_folder
        fallback.mkdir(parents=True, exist_ok=True)
        case_dir = fallback
        if _logger:
            _logger.warn(f"Smart path failed — using fallback: {case_dir}")

    if _logger:
        _logger.info(f"Target directory: {case_dir.absolute()}")

    _ci_path = case_dir / "case_info.json"
    if not _ci_path.exists() and (parties_full or parties):
        _ci_parties = ([{"name": p.get("name", ""), "role": p.get("role", "")}
                        for p in parties_full if p.get("name")]
                       if parties_full else [{"name": n} for n in parties if n])
        _ci_data = {
            "portal": "NET", "case_id": safe_case_folder,
            "full_name": raw_case_name, "parties": _ci_parties,
            "location": (case_meta or {}).get("court", ""),
        }
        try:
            import json as _json
            _ci_path.write_text(_json.dumps(_ci_data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:
            pass

    # 5. Load or create manifest, then sync with disk
    manifest = ManifestManager(
        get_summary_csv_path(case_dir),
        run_timestamp=SESSION_SETTINGS["run_timestamp"],
        logger=_logger,
        parties=parties_str,
    )
    manifest.sync_with_disk(case_dir)

    # Write global summary early — so the case appears even if run is interrupted
    _append_net_to_global_summary(
        case_dir=case_dir,
        raw_case_name=raw_case_name,
        parties_str=parties_str,
        representatives_str=representatives_str,
        total=len(manifest.records),
        downloaded=0,
        failed=0,
        case_meta=case_meta,
    )

    # 6. Collect IDs that need re-downloading (status = Missing)
    re_download_ids = manifest.get_missing_ids()
    if re_download_ids and _logger:
        _logger.warn(f"{len(re_download_ids)} missing files will be re-downloaded.")

    # 7. Pull metadata store
    metadata_lookup = scraper.extract_metadata()
    # Pre-populate manifest with all portal docs as Pending before any downloads
    scraper.pre_populate_manifest_from_metadata(case_dir, manifest, metadata_lookup)

    # Smart-skip: if every portal document is already a success, skip page scan.
    from core.sync_history import (
        SyncHistory, compute_net_hash, dates_from_net_metadata
    )
    successful_ids = manifest.get_successful_ids()
    metadata_ids = set(metadata_lookup.keys())
    if metadata_ids and metadata_ids.issubset(successful_ids) and not re_download_ids:
        msg = (
            f"All {len(metadata_ids)} portal documents already in manifest as Success — "
            "skipping page scan."
        )
        print(f"\n[INFO] {msg}")
        if _logger:
            _logger.info(msg)
        manifest.print_summary(_logger)
        snapshot_lines = [
            f"  [NET] Case: {raw_case_name}",
            f"  [NET] Folder: {case_dir.name}",
            "  Pages scanned: 0 (smart-skip)",
            f"  Total manifest entries: {len(manifest.records)}",
        ]
        # Hash & history even on smart-skip (detects silent removals)
        portal_hash = compute_net_hash(metadata_lookup)
        first_date, last_date = dates_from_net_metadata(metadata_lookup)
        history = SyncHistory(case_dir, logger=_logger, label=case_dir.name)
        prev_hash = history.last_hash()
        note = ""
        if prev_hash and prev_hash != portal_hash:
            note = "⚠️ חתימת פורטל השתנתה ללא הורדות חדשות — ייתכן שמסמך הוסר מהתיק"
            print(f"\n[WARN] {note}")
            if _logger:
                _logger.warn(note)
        _drive_skip = sum(1 for r in manifest.records if r.get("עלה לDrive"))
        changed = history.append(
            portal="NET",
            total=len(metadata_ids),
            new_downloads=0,
            re_downloads=0,
            failed=0,
            first_date=first_date,
            last_date=last_date,
            portal_hash=portal_hash,
            note=note,
            drive_uploads=_drive_skip,
        )
        if _logger:
            _logger.info(
                f"[SyncHistory] hash={portal_hash} | prev={prev_hash} | changed={changed}"
            )
        append_case_to_centralized_log(raw_case_name, len(manifest.records), [], [], [], snapshot_lines)
        print_terminal_final_dashboard(len(manifest.records), [], [], [])
        _pending = sum(1 for r in manifest.records if r.get("סטטוס הורדה") in ("Pending", "Failed"))
        _append_net_to_global_summary(
            case_dir=case_dir,
            raw_case_name=raw_case_name,
            parties_str=parties_str,
            representatives_str=representatives_str,
            total=len(manifest.records),
            downloaded=0,
            failed=0,
            case_meta=case_meta,
            portal_hash=portal_hash,
            first_date=first_date,
            last_date=last_date,
            pending=_pending,
            signature_changed=changed,
        )
        return

    # 8. Page-by-page download loop
    page_number = 1
    total_in_portal = len(metadata_lookup)
    already_done = len(manifest.get_successful_ids())
    remaining_total = max(0, total_in_portal - already_done + len(re_download_ids))
    if _logger:
        _logger.info(
            f"סיכום: {total_in_portal} מסמכים בפורטל | "
            f"{already_done} כבר הורדו | "
            f"{remaining_total} נותרו להורדה"
        )
    print(
        f"\n  📋 {total_in_portal} מסמכים בפורטל — "
        f"{already_done} כבר הורדו — "
        f"נותרו: {remaining_total}"
    )
    global_idx = already_done
    all_downloaded: list[dict] = []
    all_failed: list[dict] = []
    snapshot_lines = [
        f"  [NET] Case: {raw_case_name}",
        f"  [NET] Folder: {case_dir.name}",
    ]

    while True:
        if _logger:
            _logger.info(f"Scanning page {page_number}...")

        links_found, page_dl, page_fail = scraper.scrape_and_download_current_page(
            case_dir=case_dir,
            manifest=manifest,
            metadata_lookup=metadata_lookup,
            global_idx_start=global_idx,
            re_download_ids=re_download_ids,
            total_in_portal=total_in_portal,
        )

        if links_found == 0:
            if _logger:
                _logger.info("No download links on this page — stopping pagination.")
            break

        all_downloaded.extend(page_dl)
        all_failed.extend(page_fail)
        global_idx += links_found

        if _logger:
            _logger.info(f"Page {page_number}: {len(page_dl)} downloaded, {len(page_fail)} failed.")

        # Update global summary after every page so progress is visible if interrupted
        try:
            _ph_mid = compute_net_hash(metadata_lookup) if metadata_lookup else ""
            _fd_mid, _ld_mid = dates_from_net_metadata(metadata_lookup) if metadata_lookup else ("", "")
            _pending_mid = sum(1 for r in manifest.records if r.get("סטטוס הורדה") in ("Pending", "Failed"))
            _append_net_to_global_summary(
                case_dir=case_dir,
                raw_case_name=raw_case_name,
                parties_str=parties_str,
                representatives_str=representatives_str,
                total=len(manifest.records),
                downloaded=len(all_downloaded),
                failed=len(all_failed),
                case_meta=case_meta,
                portal_hash=_ph_mid,
                first_date=_fd_mid,
                last_date=_ld_mid,
                pending=_pending_mid,
            )
        except Exception:
            pass

        if scraper.go_to_next_page():
            page_number += 1
            time.sleep(5)
        else:
            if _logger:
                _logger.info("No further pages.")
            break

    snapshot_lines.append(f"  Pages scanned: {page_number}")
    snapshot_lines.append(f"  Total manifest entries: {len(manifest.records)}")

    # Retry failed downloads once
    retry_ids = manifest.get_failed_ids()
    if retry_ids:
        if _logger:
            _logger.info(f"Retrying {len(retry_ids)} failed download(s)...")
        print(f"\n  🔄 מנסה שוב {len(retry_ids)} הורדות שנכשלו...")
        # Navigate back to first page
        try:
            scraper.page.goto(scraper.page.url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
            nav.navigate_to_tik_niyar()
            time.sleep(1)
        except Exception as _re_nav:
            if _logger:
                _logger.warn(f"Could not navigate back for retry: {_re_nav}")

        retry_meta = scraper.extract_metadata()
        retry_dl, retry_fail = [], []
        retry_page = 1
        while True:
            found, rdl, rfail = scraper.scrape_and_download_current_page(
                case_dir=case_dir,
                manifest=manifest,
                metadata_lookup=retry_meta or metadata_lookup,
                global_idx_start=len(manifest.get_successful_ids()),
                re_download_ids=retry_ids,
                total_in_portal=total_in_portal,
            )
            if found == 0:
                break
            retry_dl.extend(rdl)
            retry_fail.extend(rfail)
            if not scraper.go_to_next_page():
                break
            retry_page += 1
            time.sleep(3)

        all_downloaded.extend(retry_dl)
        # Remove retried entries from all_failed if they now succeeded
        retry_success_ids = {r.get("מזהה ייחודי") for r in retry_dl}
        all_failed = [r for r in all_failed if r.get("מזהה ייחודי") not in retry_success_ids]
        all_failed.extend(retry_fail)
        if _logger:
            _logger.info(f"Retry: {len(retry_dl)} recovered, {len(retry_fail)} still failed.")

    manifest.print_summary(_logger)

    # Decisions + viewers update
    # Collect from תיק נייר פירוט links first (page is still on תיק נייר),
    # then navigate to החלטות and merge both viewer sources.
    if SESSION_SETTINGS.get("check_viewers", True):
        try:
            from core.net_decisions import NetDecisionsScraper
            decisions_scraper = NetDecisionsScraper(page, case_dir, logger=_logger)
            tik_niyar_viewers = decisions_scraper.collect_tik_niyar_viewers()
            decisions_scraper.update_all_decisions(manifest=manifest, extra_viewers=tik_niyar_viewers)
        except Exception as _de:
            if _logger:
                _logger.warn(f"Decisions/viewers update skipped: {_de}")
    else:
        if _logger:
            _logger.info("Viewers check skipped (disabled in settings).")

    append_case_to_centralized_log(
        raw_case_name, len(manifest.records),
        all_downloaded, all_failed, [], snapshot_lines,
    )
    print_terminal_final_dashboard(len(manifest.records), all_downloaded, all_failed, [])

    # Hash & sync history
    portal_hash = compute_net_hash(metadata_lookup)
    first_date, last_date = dates_from_net_metadata(metadata_lookup)
    history = SyncHistory(case_dir, logger=_logger, label=case_dir.name)
    prev_hash = history.last_hash()
    note = ""
    if (
        prev_hash
        and prev_hash != portal_hash
        and len(all_downloaded) == 0
        and len(all_failed) == 0
    ):
        note = "⚠️ חתימת פורטל השתנתה ללא הורדות חדשות — ייתכן שמסמך הוסר מהתיק"
        print(f"\n[WARN] {note}")
        if _logger:
            _logger.warn(note)
    _drive_count = sum(1 for r in manifest.records if r.get("עלה לDrive"))
    changed = history.append(
        portal="NET",
        total=len(metadata_ids),
        new_downloads=len(all_downloaded),
        re_downloads=len(re_download_ids),
        failed=len(all_failed),
        first_date=first_date,
        last_date=last_date,
        portal_hash=portal_hash,
        note=note,
        drive_uploads=_drive_count,
    )
    if _logger:
        _logger.info(
            f"[SyncHistory] hash={portal_hash} | prev={prev_hash} | changed={changed}"
        )

    # Write / update global summary
    _pending_count = sum(1 for r in manifest.records if r.get("סטטוס הורדה") in ("Pending", "Failed"))
    _append_net_to_global_summary(
        case_dir=case_dir,
        raw_case_name=raw_case_name,
        parties_str=parties_str,
        representatives_str=representatives_str,
        total=len(manifest.records),
        downloaded=len(all_downloaded),
        failed=len(all_failed),
        case_meta=case_meta,
        portal_hash=portal_hash,
        first_date=first_date,
        last_date=last_date,
        pending=_pending_count,
        signature_changed=changed,
    )

    # Related cases — if enabled in settings, process all תיקים קשורים
    if SESSION_SETTINGS.get("download_related_cases"):
        try:
            from core.net_related_cases import process_related_cases
            from core.net_case_navigator import parse_net_case_number
            parsed = parse_net_case_number(safe_case_folder)
            if parsed:
                orig_num, orig_mmyy = parsed
                process_related_cases(
                    page=page,
                    original_case_num=orig_num,
                    original_mmyy=orig_mmyy,
                    root_output_dir=output_dir or ROOT_OUTPUT_DIR,
                    session_settings=SESSION_SETTINGS,
                    resolve_paths_fn=None,
                    logger=_logger,
                )
            else:
                if _logger:
                    _logger.warn("[RelatedCases] Could not parse case number from folder name — skipping.")
        except Exception as _re:
            if _logger:
                _logger.warn(f"[RelatedCases] Skipped: {_re}")
            print(f"[RelatedCases] Error: {_re}")


# ---------------------------------------------------------------------------
# Global summary helper (NET cases)
# ---------------------------------------------------------------------------

def _append_net_to_global_summary(
    case_dir: Path,
    raw_case_name: str,
    parties_str: str,
    representatives_str: str,
    total: int,
    downloaded: int,
    failed: int,
    case_meta: dict | None = None,
    portal_hash: str = "",
    first_date: str = "",
    last_date: str = "",
    pending: int = 0,
    signature_changed: str = "",
) -> None:
    """Append / update a row for a NET case in downloads/all_cases_summary.csv."""
    import csv as _csv
    from datetime import datetime as _dt

    # Locate the downloads/ folder (walk up from case_dir until we find it,
    # fall back to ROOT_OUTPUT_DIR / "downloads").
    summary_dir: Path | None = None
    cur = case_dir.parent
    for _ in range(6):
        if cur.name == "downloads" or (cur / "downloads").is_dir():
            summary_dir = cur if cur.name == "downloads" else cur / "downloads"
            break
        cur = cur.parent
    if summary_dir is None:
        summary_dir = ROOT_OUTPUT_DIR / "downloads"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "all_cases_summary.csv"

    COLS = [
        "מזהה תיק",
        "שם תיק",
        "הליך",
        "בית משפט / בית דין",
        "פורטל",
        "הצד שלנו",
        "צדדים",
        "מייצגים",
        "תיקייה",
        "תיקיית תיק על",
        "גורם שיפוטי",
        "תאריך פתיחה",
        "תאריך סגירה",
        "פעילות אחרונה",
        "קבצים סהכ",
        "הורדו",
        "נכשלו",
        "ממתין להורדה",
        "חדש מפעם קודמת",
        "מסמך ראשון",
        "מסמך אחרון",
        "חתימת פורטל",
        "זמן סיכום",
    ]

    # Load existing rows keyed by תיקייה path (NET cases have no sub_id)
    existing: dict[str, dict] = {}
    if summary_path.exists():
        try:
            with summary_path.open(encoding="utf-8-sig") as f:
                for row in _csv.DictReader(f):
                    key = row.get("תיקייה") or row.get("מזהה תיק", "")
                    if key:
                        existing[key] = dict(row)
        except Exception:
            pass

    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = case_meta or {}
    key = str(case_dir)
    row = existing.get(key, {})
    row.update({
        "מזהה תיק": row.get("מזהה תיק") or meta.get("case_number") or raw_case_name,
        "שם תיק": meta.get("case_title") or raw_case_name,
        "הליך": meta.get("procedure") or row.get("הליך", ""),
        "בית משפט / בית דין": meta.get("court") or row.get("בית משפט / בית דין", ""),
        "פורטל": "NET",
        "הצד שלנו": meta.get("our_side") or row.get("הצד שלנו", ""),
        "צדדים": parties_str or row.get("צדדים", ""),
        "מייצגים": representatives_str or row.get("מייצגים", ""),
        "תיקייה": str(case_dir),
        "תיקיית תיק על": str(case_dir.parent),
        "גורם שיפוטי": meta.get("judge") or row.get("גורם שיפוטי", ""),
        "תאריך פתיחה": meta.get("open_date") or row.get("תאריך פתיחה", ""),
        "תאריך סגירה": meta.get("close_date") or row.get("תאריך סגירה", ""),
        "פעילות אחרונה": now_str,
        "קבצים סהכ": str(total),
        "הורדו": str(downloaded),
        "נכשלו": str(failed),
        "ממתין להורדה": str(pending) if pending else row.get("ממתין להורדה", ""),
        "חדש מפעם קודמת": signature_changed or row.get("חדש מפעם קודמת", ""),
        "מסמך ראשון": first_date or row.get("מסמך ראשון", ""),
        "מסמך אחרון": last_date or row.get("מסמך אחרון", ""),
        "חתימת פורטל": portal_hash or row.get("חתימת פורטל", ""),
        "זמן סיכום": now_str,
    })
    existing[key] = row

    try:
        with summary_path.open("w", encoding="utf-8-sig", newline="") as f:
            # Merge column list: BDR rows might lack "מייצגים" — extrasaction="ignore" handles extra keys
            all_cols = COLS + [c for c in (list(existing.values())[0].keys() if existing else []) if c not in COLS]
            writer = _csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
            writer.writeheader()
            for r in existing.values():
                writer.writerow(r)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def run_download(page: Page, connection_type: any, output_dir: Path | None = None) -> None:
    if connection_type.name == "BDR":
        run_bdr_download(page, output_dir)
    elif connection_type.name == "NET":
        run_net_download(page, output_dir)


# ---------------------------------------------------------------------------
# Folder reorganization — group cases by party names
# ---------------------------------------------------------------------------

def _party_key(names: list[str]) -> str:
    """Canonical key for a set of parties, order-independent."""
    normed = sorted(set(_normalize_party(n) for n in names if n.strip()))
    return " | ".join(normed)


def reorganize_downloads(logger=None) -> dict:
    """Move flat case folders into party-grouped parent folders.

    Batch approach: reads ALL case_info.json first, groups by normalized
    party set, then moves each group under a single parent folder."""
    import json as _json, shutil

    downloads = ROOT_OUTPUT_DIR / "downloads"
    if not downloads.exists():
        return {"moved": 0, "skipped": 0, "errors": []}

    # Phase 1: collect case info from flat case dirs
    groups: dict[str, list[tuple[Path, list[str]]]] = {}
    skipped = 0
    for case_dir in sorted(downloads.iterdir()):
        if not case_dir.is_dir() or case_dir.parent != downloads:
            continue
        ci_path = case_dir / "case_info.json"
        if not ci_path.exists():
            skipped += 1
            continue
        try:
            info = _json.loads(ci_path.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        parties_raw = info.get("parties", [])
        names = [p.get("name", "").strip() for p in parties_raw if p.get("name", "").strip()]
        if not names:
            skipped += 1
            continue
        key = _party_key(names)
        groups.setdefault(key, []).append((case_dir, names))

    # Phase 2: for each group, choose a folder name and move
    moved = 0
    errors: list[str] = []
    for key, cases in groups.items():
        if len(cases) < 1:
            continue
        names = [_normalize_party(n) for n in cases[0][1]]
        if len(names) >= 2:
            folder_name = " - ".join(names[:2])
        else:
            folder_name = names[0] if names else "General"
        safe_folder = re.sub(r'[\\/*?:"<>|]', "-", folder_name).strip()
        parent = downloads / safe_folder
        parent.mkdir(parents=True, exist_ok=True)
        for case_dir, _ in cases:
            dest = parent / case_dir.name
            if dest.exists():
                for item in case_dir.iterdir():
                    d = dest / item.name
                    if not d.exists():
                        shutil.move(str(item), str(d))
                try:
                    case_dir.rmdir()
                except OSError:
                    pass
            else:
                try:
                    shutil.move(str(case_dir), str(dest))
                except Exception as e:
                    errors.append(f"{case_dir.name}: {e}")
                    continue
            if logger:
                logger.info(f"[Reorg] {case_dir.name} → {safe_folder}/{case_dir.name}")
            moved += 1

    return {"moved": moved, "skipped": skipped, "errors": errors}
