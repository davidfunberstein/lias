"""BDR (Rabbinical Courts) threaded mode — EXPERIMENTAL.

ALL Playwright operations run in the main (calling) thread.
A lightweight daemon thread handles keyboard input only, so the user can
type 'stop' or 'status' while a download is in progress.

Root cause of the old architecture's crash:
  Playwright sync_api binds each Page to the greenlet that created it.
  Calling page.goto() from a background thread raises
  "Cannot switch to a different thread".

Fix:
  Main thread  → all Playwright / BdrNavigator calls
  Daemon thread → input() reads only; puts lines into a queue
"""

from __future__ import annotations

import queue
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

BDR_URL = "https://sides.rbc.gov.il/Pages/FilesList.aspx"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_bdr_threaded(
    logger: "Logger | None",
    session_settings: dict,
    root_output_dir: Path,
    resolve_paths_fn,
    existing_page=None,
    drive_manager=None,
) -> None:
    """
    BDR threaded download session.

    All Playwright operations stay in the calling (main) thread.
    A background daemon thread reads keyboard input so the user can
    send 'stop' or 'status' commands while the download runs.
    Ctrl+C also stops cleanly.
    """
    if existing_page is None:
        print("[ERROR] BDR threaded mode requires an existing browser page.")
        return

    page = existing_page

    def _log(msg: str, level: str = "info") -> None:
        pfx = "[BDR Threaded]"
        if logger:
            getattr(logger, level)(f"{pfx} {msg}")
        else:
            print(f"{pfx} {msg}")

    # ── Navigate to BDR portal — main thread ─────────────────────────────
    try:
        page.goto(BDR_URL, wait_until="domcontentloaded")
        page.bring_to_front()
    except Exception as e:
        _log(f"Navigation to BDR failed: {e}", "error")
        return

    print("\n" + "*" * 60)
    print(">>> STATUS: Browser is on BDR portal.")
    print(">>> Starting download automatically (LIAS mode).")
    print("*" * 60)

    # LIAS mode: skip the Enter prompt — proceed automatically
    try:
        from core.download import SESSION_SETTINGS as _ss
        if not _ss.get("lias_mode"):
            try:
                ans = input(">>> [Enter / b]: ").strip().lower()
            except KeyboardInterrupt:
                print("\n[INFO] Interrupted — returning to main menu.")
                return
            if ans in ("b", "back", "q", "stop"):
                _log("User returned to main menu.")
                print("[INFO] Returning to main menu.")
                return
    except Exception:
        pass

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

    input_thread = threading.Thread(target=_read_input, daemon=True, name="BdrInput")
    input_thread.start()

    def _check_input() -> str | None:
        """Non-blocking peek at the next user command (returns None if empty)."""
        try:
            return user_input_q.get_nowait()
        except queue.Empty:
            return None

    _dl_ok = [0]   # mutable counter accessible inside _handle_cmd closure
    _dl_fail = [0]

    def _handle_cmd(cmd: str) -> bool:
        """Handle a user command. Returns True if download should stop."""
        if cmd in ("stop", "b", "q"):
            stop_event.set()
            return True
        if cmd == "status":
            print(f"\n[STATUS] downloaded: {_dl_ok[0]}, failed: {_dl_fail[0]}\n")
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
    print("\n[BDR Threaded] Download starting...")
    _drive_hint = "  d=drive-log | stop-up=stop-uploads | " if drive_manager else ""
    print(f"Commands: status | stop | {_drive_hint}stop-all\n")

    try:
        from core.bdr_navigation import BdrNavigator

        nav = BdrNavigator(page, logger=logger)
        nav.click_documents_tab()

        try:
            page.wait_for_selector("tr[id*='DXDataRow']", timeout=20000)
            time.sleep(1)
        except Exception as e:
            _log(f"Documents table did not load: {e}", "error")
            stop_event.set()
            return

        choices, case_name = nav.extract_case_details_and_route_raw()
        _log(f"Case: '{case_name}'")

        safe_name = re.sub(r'[\\/*?:"<>|]', "-", case_name).strip()
        try:
            case_dir = resolve_paths_fn(choices, safe_name)
        except Exception:
            case_dir = root_output_dir / "downloads" / safe_name
            case_dir.mkdir(parents=True, exist_ok=True)
        _log(f"Directory: {case_dir}")

        def _input_dispatch() -> "str | None":
            """Peek at next command; dispatch drive/stop-all locally, pass through stop/status."""
            cmd = _check_input()
            if cmd is None:
                return None
            if cmd in ("stop", "b", "q", "status"):
                return cmd  # let bdr_navigation handle these
            _handle_cmd(cmd)
            return None

        total, downloaded, re_downloaded, failed, table_updated, snapshot_lines = (
            nav.sync_and_download_bdr(
                case_dir,
                session_settings,
                session_settings.get("run_timestamp", ""),
                stop_event=stop_event,
                input_check_fn=_input_dispatch,
            )
        )

        from core.download import (
            append_case_to_centralized_log,
            print_terminal_final_dashboard,
        )
        append_case_to_centralized_log(
            case_name, total, downloaded, failed, table_updated, snapshot_lines
        )
        print_terminal_final_dashboard(total, downloaded, failed, table_updated)

        from core.sync_history import SyncHistory, compute_bdr_hash, dates_from_bdr_snapshot
        portal_hash = compute_bdr_hash(snapshot_lines)
        first_date, last_date = dates_from_bdr_snapshot(snapshot_lines)
        sh = SyncHistory(case_dir, logger)
        prev_hash = sh.last_hash()
        note = ""
        if (
            prev_hash
            and prev_hash != portal_hash
            and not downloaded
            and not re_downloaded
            and not failed
        ):
            note = "⚠️ חתימת פורטל השתנתה ללא הורדות — ייתכן שמסמך הוסר"
            print(f"\n[WARN] {note}")
        sh.append(
            portal="BDR",
            total=total,
            new_downloads=len(downloaded),
            re_downloads=len(re_downloaded),
            failed=len(failed),
            first_date=first_date,
            last_date=last_date,
            portal_hash=portal_hash,
            note=note,
        )
        _log(
            f"Sync complete: {len(downloaded)} new, "
            f"{len(re_downloaded)} re-downloaded, {len(failed)} failed."
        )

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C — download stopped, browser stays open.")
        if logger:
            logger.info("BDR threaded mode interrupted by Ctrl+C.")

    except Exception as e:
        _log(f"Unexpected error: {e}", "error")

    finally:
        stop_event.set()

    print("[INFO] Returning to main menu.")
