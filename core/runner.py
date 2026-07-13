"""Main CLI execution runner — Legal-AI Scraper with persistent browser session."""

from __future__ import annotations

import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

from core.connection import GovIlConnection, ConnectionType, SHARED_PROFILE_DIR, UserGoBack
from core.download import (
    init_session_logger,
    configure_download_settings,
    run_download,
    get_logger,
    SESSION_SETTINGS,
    ROOT_OUTPUT_DIR,
    resolve_smart_paths,
)
from core.email_otp import setup_email_config, load_email_config
from core.user_mode import configure_user_mode, is_lawyer_mode
from core.i18n import t

_SETTINGS_FILE = Path("session_defaults.json")
_PERSISTENT_KEYS = ["lang", "mode", "date_filter", "storage_mode", "login_method",
                    "bdr_entity_type", "user_mode", "lawyer_name", "email_enabled",
                    "download_related_cases", "gemini_enabled", "gemini_api_key",
                    "check_viewers", "otp_method", "share_email", "groq_api_key"]

def _load_persistent_settings() -> None:
    """Load saved settings from session_defaults.json into SESSION_SETTINGS."""
    import json
    try:
        if _SETTINGS_FILE.exists():
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            for k in _PERSISTENT_KEYS:
                if k in data:
                    SESSION_SETTINGS[k] = data[k]
    except Exception:
        pass

def _save_persistent_settings() -> None:
    """Save relevant SESSION_SETTINGS keys to session_defaults.json."""
    import json
    try:
        data = {k: SESSION_SETTINGS[k] for k in _PERSISTENT_KEYS if k in SESSION_SETTINGS}
        _SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# Google Drive — optional; imported lazily to avoid hard dependency
_CREDENTIALS_PATH = ROOT_OUTPUT_DIR.parent / "credentials.json"
_TOKEN_PATH = ROOT_OUTPUT_DIR.parent / "token.json"

# Singleton background upload manager (created when storage_mode is "both" or "cloud")
_drive_manager = None  # type: "DriveUploadManager | None"


def _get_drive_manager():
    return _drive_manager


def _init_drive_manager(logger) -> bool:
    """Create and start the background DriveUploadManager. Returns True on success."""
    global _drive_manager
    mode = SESSION_SETTINGS.get("storage_mode", "local")
    if mode not in ("both", "cloud"):
        return False
    if not _CREDENTIALS_PATH.exists():
        from core.gdrive import print_drive_setup_instructions
        print_drive_setup_instructions()
        return False
    try:
        from core.drive_upload_manager import init_manager as _init_mgr
        _drive_manager = _init_mgr(ROOT_OUTPUT_DIR, _CREDENTIALS_PATH, _TOKEN_PATH, logger)
        print(f"{_ts()} [Drive] מנהל העלאות ברקע הופעל.")
        return True
    except Exception as e:
        print(f"{_ts()} [Drive] לא ניתן לאתחל מנהל העלאות: {e}")
        if logger:
            logger.error(f"[Drive] init_manager failed: {e}")
        return False

SESSION_SETTINGS.setdefault("storage_mode", "local")  # "local" | "both" | "cloud"
SESSION_SETTINGS.setdefault("bdr_entity_type", 'עו"ד מייצג')


# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------

def _maybe_gdrive_upload(root_dir: Path, logger) -> None:
    """After a download, upload new/unuploaded files to Drive in a background daemon thread."""
    mode = SESSION_SETTINGS.get("storage_mode", "local")
    if mode not in ("both", "cloud"):
        return

    def _upload() -> None:
        try:
            from core.gdrive import run_smart_gdrive_upload
            run_smart_gdrive_upload(
                root_dir=root_dir,
                credentials_path=_CREDENTIALS_PATH,
                token_path=_TOKEN_PATH,
                logger=logger,
            )
        except ImportError as exc:
            print(f"[GDrive] {exc}")
        except Exception as exc:
            print(f"[GDrive] Upload error: {exc}")
            if logger:
                logger.error(f"[GDrive] Background upload error: {exc}")

    t = threading.Thread(target=_upload, daemon=True, name="gdrive-upload")
    t.start()
    print(f"\n[GDrive] העלאה לDrive החלה ברקע...")


def _make_per_file_gdrive_callback(logger):
    """Return a callback that enqueues each downloaded file into the background DriveUploadManager.

    Returns None when storage_mode is 'local' (no Drive configured) or manager unavailable.
    Callback signature: (file_path, case_dir, doc_id, manifest) -> None
    """
    mode = SESSION_SETTINGS.get("storage_mode", "local")
    if mode not in ("both", "cloud"):
        return None

    def _callback(file_path: Path, case_dir: Path, doc_id: str, manifest) -> None:
        mgr = _get_drive_manager()
        if mgr is None or not mgr.is_running():
            return
        # Pass manifest metadata so upload log has rich info
        doc_meta: dict = {}
        try:
            for row in manifest.records:
                if row.get("מזהה ייחודי") == doc_id:
                    doc_meta = dict(row)
                    break
        except Exception:
            pass
        mgr.enqueue(file_path, case_dir, doc_meta)

    return _callback


def _set_per_file_gdrive_callback(logger) -> None:
    """Install the per-file Drive upload callback into net_scraper."""
    import core.net_scraper as _ns
    _ns._on_file_downloaded = _make_per_file_gdrive_callback(logger)


def _clear_per_file_gdrive_callback() -> None:
    """Remove the per-file Drive upload callback from net_scraper."""
    import core.net_scraper as _ns
    _ns._on_file_downloaded = None


def _pre_sync_gdrive(root_dir: Path, logger) -> None:
    """Upload any local files not yet marked as uploaded to Drive (manifest-based)."""
    mode = SESSION_SETTINGS.get("storage_mode", "local")
    if mode not in ("both", "cloud"):
        return
    print(f"{_ts()} [GDrive] סורק קבצים שטרם הועלו לDrive...")
    try:
        from core.gdrive import run_smart_gdrive_upload
        run_smart_gdrive_upload(
            root_dir=root_dir,
            credentials_path=_CREDENTIALS_PATH,
            token_path=_TOKEN_PATH,
            logger=logger,
        )
        print(f"{_ts()} [GDrive] סנכרון הושלם.")
    except ImportError as exc:
        print(f"[GDrive] {exc}")
    except Exception as exc:
        print(f"{_ts()} [GDrive] Pre-sync error: {exc}")
        if logger:
            logger.error(f"[GDrive] Pre-sync error: {exc}")


def _upload_logs_gdrive(logger) -> None:
    """Upload only latest.log to Drive (Legal-Ai/logs/latest.log)."""
    mode = SESSION_SETTINGS.get("storage_mode", "local")
    if mode not in ("both", "cloud"):
        return
    latest_log = ROOT_OUTPUT_DIR / "logs" / "latest.log"
    if not latest_log.exists():
        return
    try:
        from core.gdrive import GDriveUploader, DRIVE_ROOT_FOLDER
        uploader = GDriveUploader(_CREDENTIALS_PATH, _TOKEN_PATH, logger=logger)
        if not uploader.authenticate():
            return
        if uploader._service is None:
            return
        # Get/create Legal-Ai/logs/ folder
        root_id = uploader.get_or_create_folder(DRIVE_ROOT_FOLDER, parent_id=None)
        logs_id = uploader.get_or_create_folder("logs", parent_id=root_id)
        # Always overwrite — delete existing latest.log first
        try:
            existing = uploader._service.files().list(
                q=f"name='latest.log' and '{logs_id}' in parents and trashed=false",
                fields="files(id)",
            ).execute().get("files", [])
            for f in existing:
                uploader._service.files().delete(fileId=f["id"]).execute()
        except Exception:
            pass
        uploader.upload_file(latest_log, logs_id)
        if logger:
            logger.info("[GDrive] Uploaded latest.log to Drive.")
    except Exception as exc:
        if logger:
            logger.error(f"[GDrive] Log upload error: {exc}")


def _handle_gdrive_menu(logger) -> None:
    """Interactive Google Drive management sub-menu."""
    print("\n" + "=" * 54)
    print("[GDrive] GOOGLE DRIVE SYNC SETTINGS")
    print("-" * 54)
    current = SESSION_SETTINGS.get("storage_mode", "local")
    print(f"  Current storage mode: {current}")
    print()
    print("  1  Set storage mode")
    print("  2  Upload an existing local directory to Drive now")
    print("  3  Test Drive connection")
    print("  b  Back to main menu")
    print("=" * 54)

    sub = input("Enter choice (1 / 2 / 3 / b): ").strip().lower()

    if sub == "1":
        print("\n  Storage mode:")
        print("    1 — Local only (default)")
        print("    2 — Local + Google Drive")
        print("    3 — Google Drive only")
        mode_choice = input("  Choose (1 / 2 / 3): ").strip()
        mode_map = {"1": "local", "2": "both", "3": "cloud"}
        new_mode = mode_map.get(mode_choice)
        if new_mode:
            SESSION_SETTINGS["storage_mode"] = new_mode
            print(f"[GDrive] Storage mode set to '{new_mode}'.")
            if logger:
                logger.info(f"[GDrive] Storage mode changed to '{new_mode}'.")
            _save_persistent_settings()   # persist so next session remembers
            if new_mode in ("both", "cloud"):
                upload_existing = input(
                    f"  {_ts()} העלה קבצים מקומיים קיימים לDrive עכשיו? (y/n): "
                ).strip().lower()
                if upload_existing == "y":
                    _pre_sync_gdrive(ROOT_OUTPUT_DIR, logger)
        else:
            print("[GDrive] Invalid choice — mode unchanged.")

    elif sub == "2":
        path_str = input("[GDrive] Local directory path to upload (Enter to cancel): ").strip()
        if not path_str:
            print("[GDrive] Cancelled.")
            return
        local_dir = Path(path_str)
        if not local_dir.exists() or not local_dir.is_dir():
            print(f"[GDrive] Directory not found: {local_dir}")
            return
        try:
            from core.gdrive import run_gdrive_upload
            run_gdrive_upload(
                local_dir=local_dir,
                credentials_path=_CREDENTIALS_PATH,
                token_path=_TOKEN_PATH,
                logger=logger,
            )
        except ImportError as exc:
            print(f"[GDrive] {exc}")
        except Exception as exc:
            print(f"[GDrive] Upload error: {exc}")
            if logger:
                logger.error(f"[GDrive] Manual upload error: {exc}")

    elif sub == "3":
        try:
            from core.gdrive import GDriveUploader
            uploader = GDriveUploader(_CREDENTIALS_PATH, _TOKEN_PATH, logger=logger)
            print("[GDrive] Testing connection ...")
            if uploader.authenticate():
                print("[GDrive] Connection successful.")
            else:
                print("[GDrive] Connection failed.")
        except ImportError as exc:
            print(f"[GDrive] {exc}")
        except Exception as exc:
            print(f"[GDrive] Connection test error: {exc}")

    elif sub in ("b", "back", ""):
        return
    else:
        print("[GDrive] Invalid choice.")


_NET_HOME_URL = "https://www.court.gov.il/ngcs.web.site/homepage.aspx"


def _ensure_logged_in(page, portal: str, logger) -> bool:
    """Ensure the browser is logged in to the given portal ("NET" or "BDR").

    Returns True if logged in (or best-effort attempt made), False only on hard failure.
    """
    import time as _time
    from core.connection import _run_gov_autologin

    if portal == "NET":
        try:
            from core.gov_login import (
                _is_already_logged_in_net,
                handle_net_portal_entry,
                is_gov_login_page,
            )

            # Fast path: already on securesso with valid session
            current_url = page.url or ""
            if "securesso.court.gov.il" in current_url and _is_already_logged_in_net(page):
                if logger:
                    logger.info("[EnsureLogin] Already on secured NET portal.")
                return True

            # Navigate to public homepage to start the auth flow
            if "court.gov.il" not in current_url:
                try:
                    page.goto(_NET_HOME_URL, wait_until="domcontentloaded", timeout=25000)
                    _time.sleep(1.5)
                except Exception as _ge:
                    if logger:
                        logger.warn(f"[EnsureLogin] NET goto failed: {_ge}")

            page.bring_to_front()

            # Check on public homepage: ת"ז visible means authenticated session
            if _is_already_logged_in_net(page):
                if logger:
                    logger.info("[EnsureLogin] Authenticated session detected on NET homepage.")
                # Still need to enter secured portal for case work
                handle_net_portal_entry(page)   # click הזדהות לאומית — instant with valid session
                _run_gov_autologin(page, "NET")  # handles passkey / any prompts
                _time.sleep(1)
                return True

            # No session — full login flow
            if logger:
                logger.info("[EnsureLogin] No NET session — starting full authentication...")
            handle_net_portal_entry(page)
            _run_gov_autologin(page, "NET")
            _time.sleep(1.5)

            ok = _is_already_logged_in_net(page)
            if not ok and logger:
                logger.warn("[EnsureLogin] Could not confirm NET login after auth attempt.")
            return True  # best-effort

        except UserGoBack:
            raise
        except Exception as e:
            if logger:
                logger.warn(f"[EnsureLogin] NET login error: {e} — proceeding anyway.")
            return True  # best-effort

    elif portal == "BDR":
        try:
            from core.gov_login import _is_already_logged_in_bdr, is_bdr_login_page, handle_bdr_login_page
            import time as _time2

            if _is_already_logged_in_bdr(page):
                if logger:
                    logger.info("[EnsureLogin] Already logged in to BDR.")
                return True

            if is_bdr_login_page(page):
                handle_bdr_login_page(page, session_settings=SESSION_SETTINGS)

            _run_gov_autologin(page, "BDR")
            _time2.sleep(1.5)

            for _ in range(3):
                if is_bdr_login_page(page):
                    handle_bdr_login_page(page, session_settings=SESSION_SETTINGS)
                    _time2.sleep(2)
                    break
                if _is_already_logged_in_bdr(page):
                    break
                _time2.sleep(1)

            return True

        except UserGoBack:
            raise
        except Exception as e:
            if logger:
                logger.warn(f"[EnsureLogin] BDR login error: {e} — proceeding anyway.")
            return True

    return True


def _attempt_auto_login_if_needed(page, email_cfg: dict, logger) -> None:
    """Call attempt_auto_login if the page looks like a login or OTP page."""
    try:
        from core.auto_login import attempt_auto_login
        attempt_auto_login(page, email_cfg, logger=logger)
    except Exception as exc:
        msg = f"[AutoLogin] Error during auto-login attempt: {exc}"
        print(msg)
        if logger:
            logger.warn(msg)


def _handle_credentials_menu(logger) -> None:
    """Interactive credentials setup sub-menu."""
    from core.credentials import credentials_exist, get_credentials, clear_credentials, reset_credentials

    print("\n" + "=" * 54)
    print("[Auth] CREDENTIALS SETUP (ID + Password)")
    print("-" * 54)
    status = "Saved in keychain" if credentials_exist() else "Not saved"
    print(f"  Current status: {status}")
    print()
    print("  1  Setup / update credentials")
    print("  2  Clear saved credentials")
    print("  b  Back to main menu")
    print("=" * 54)

    sub = input("Enter choice (1 / 2 / b): ").strip().lower()

    if sub == "1":
        reset_credentials()
    elif sub == "2":
        clear_credentials()
        print("[Auth] Credentials cleared from keychain.")
        if logger:
            logger.info("[Auth] Credentials cleared by user.")
    elif sub in ("b", "back", ""):
        return
    else:
        print("[Auth] Invalid choice.")


def _handle_email_menu(logger) -> None:
    """Interactive email OTP setup sub-menu."""
    print("\n" + "=" * 54)
    print("[EmailOTP] EMAIL AUTO-LOGIN SETTINGS")
    print("-" * 54)
    enabled = SESSION_SETTINGS.get("email_enabled", False)
    config_path = SESSION_SETTINGS.get("email_config_path")
    print(f"  Current status: {'Enabled' if enabled else 'Disabled'}")
    if config_path:
        existing = load_email_config(config_path)
        if existing:
            print(f"  Config file: {config_path} (backend={existing.get('backend', '?')})")
        else:
            print(f"  Config file: {config_path} (not configured yet)")
    print()
    print("  1  Run setup wizard (create/update email_config.json)")
    print("  2  Enable email OTP auto-login")
    print("  3  Disable email OTP auto-login")
    print("  b  Back to main menu")
    print("=" * 54)

    sub = input("Enter choice (1 / 2 / 3 / b): ").strip().lower()

    if sub == "1":
        cfg = setup_email_config(config_path)
        SESSION_SETTINGS["email_enabled"] = True
        print("[EmailOTP] Setup complete. Email OTP auto-login is now enabled.")
        if logger:
            logger.info(f"[EmailOTP] Config saved. backend={cfg.get('backend')}")
    elif sub == "2":
        existing = load_email_config(config_path)
        if not existing:
            print("[EmailOTP] No config found — please run setup wizard first (option 1).")
        else:
            SESSION_SETTINGS["email_enabled"] = True
            print("[EmailOTP] Email OTP auto-login enabled.")
            if logger:
                logger.info("[EmailOTP] Auto-login enabled by user.")
    elif sub == "3":
        SESSION_SETTINGS["email_enabled"] = False
        print("[EmailOTP] Email OTP auto-login disabled.")
        if logger:
            logger.info("[EmailOTP] Auto-login disabled by user.")
    elif sub in ("b", "back", ""):
        return
    else:
        print("[EmailOTP] Invalid choice.")


def _handle_settings_menu(logger) -> None:
    """Consolidated settings submenu — all settings in one place."""
    while True:
        mode_desc = (
            t("menu_mode_updates")
            if SESSION_SETTINGS["mode"] == "1"
            else t("menu_mode_full")
        )
        date_desc = (
            "Active Filter"
            if SESSION_SETTINGS["date_filter"] == "y"
            else "No Filter"
        )
        storage_desc = {
            "local": t("menu_storage_local"),
            "both": t("menu_storage_both"),
            "cloud": t("menu_storage_cloud"),
        }.get(SESSION_SETTINGS.get("storage_mode", "local"), t("menu_storage_local"))
        email_desc = "Enabled" if SESSION_SETTINGS.get("email_enabled") else "Disabled"
        user_mode_desc = "Lawyer" if is_lawyer_mode(SESSION_SETTINGS) else "Private"
        _ENTITY_EN = {
            'עו"ד מייצג': "Legal Rep.", 'עו"ד': "Attorney",
            'טו"ר מייצג': "Religious Rep.", 'גורם פרטי': "Private",
        }
        _raw_entity = SESSION_SETTINGS.get("bdr_entity_type", 'עו"ד מייצג')
        entity_desc = _ENTITY_EN.get(_raw_entity, _raw_entity)
        if is_lawyer_mode(SESSION_SETTINGS):
            user_summary = f"User={user_mode_desc} ({entity_desc})"
        else:
            user_summary = f"User={user_mode_desc}"
        login_method_desc = "Passkey (כניסה מהירה)" if SESSION_SETTINGS.get("login_method") == "passkey" else "Standard (Password + OTP)"

        related_desc = "Yes" if SESSION_SETTINGS.get("download_related_cases") else "No"
        print("\n" + "=" * 60)
        print(t("settings_title"))
        print("=" * 60)
        print(
            f"  Mode={mode_desc} | Date={date_desc} | "
            f"Email={email_desc} | Storage={storage_desc} | {user_summary}"
        )
        print("-" * 60)
        print(t("settings_sec_conn"))
        print(t("settings_1"))
        print(f"  2  Login method  [{login_method_desc}]")
        print(t("settings_3"))
        print(t("settings_sec_dl"))
        print(f"  4  Download mode  [{mode_desc}]")
        print(t("settings_5"))
        print(f"  6  NET related cases  [{related_desc}]")
        print(t("settings_sec_storage"))
        print(f"  7  Google Drive  [{storage_desc}]")
        print(t("settings_sec_user"))
        print(t("settings_8"))
        print(t("settings_9"))
        ocr_on = SESSION_SETTINGS.get("gemini_enabled", False)
        viewers_on = SESSION_SETTINGS.get("check_viewers", True)
        print(t("settings_sec_ocr"))
        print(f" 10  OCR — Gemini  [{'On' if ocr_on else 'Off'}]")
        print(f" 11  Viewers check (צפיות)  [{'On' if viewers_on else 'Off'}]")
        print(t("settings_back"))
        print("=" * 60)

        sub = input(t("settings_prompt2")).strip().lower()

        if sub == "1":
            _handle_credentials_menu(logger)
        elif sub == "2":
            print("\n  Login method:")
            print("  1 — Standard: Password + OTP (default)")
            print("  2 — Passkey: fast login (WebAuthn)")
            lm = input("  Choose (1/2): ").strip()
            if lm == "2":
                SESSION_SETTINGS["login_method"] = "passkey"
                print("[Settings] Login method: Passkey.")
            else:
                SESSION_SETTINGS["login_method"] = "standard"
                print("[Settings] Login method: Standard (Password + OTP).")
            _save_persistent_settings()
        elif sub == "3":
            _handle_email_menu(logger)
        elif sub == "4":
            configure_download_settings()
        elif sub == "5":
            date_opt = input("\nEnable date range filter? (y/n) [default: n]: ").strip().lower()
            if date_opt == "y":
                try:
                    from datetime import datetime as _dt
                    print("Enter dates in DD/MM/YYYY format:")
                    start_str = input("Start date: ").strip()
                    end_str = input("End date: ").strip()
                    SESSION_SETTINGS["start_date"] = _dt.strptime(start_str, "%d/%m/%Y")
                    SESSION_SETTINGS["end_date"] = _dt.strptime(end_str, "%d/%m/%Y")
                    SESSION_SETTINGS["date_filter"] = "y"
                    if logger:
                        logger.info(f"Date filter: {start_str} — {end_str}")
                    print(f"{_ts()} [Settings] Date filter: {start_str} — {end_str}")
                except ValueError:
                    print("Invalid date format — continuing without filter.")
                    SESSION_SETTINGS["date_filter"] = "n"
            else:
                SESSION_SETTINGS["date_filter"] = "n"
                print(f"{_ts()} [Settings] Date filter: Off.")
        elif sub == "6":
            current_rc = SESSION_SETTINGS.get("download_related_cases", False)
            print(f"\n  NET related cases — download all cases linked to the main case")
            print(f"  Current: {'On' if current_rc else 'Off'}")
            ans = input("  Enable? (y/n): ").strip().lower()
            SESSION_SETTINGS["download_related_cases"] = ans == "y"
            print(f"  [Settings] Related cases: {'Yes' if ans == 'y' else 'No'}")
            _save_persistent_settings()
        elif sub == "7":
            _handle_gdrive_menu(logger)
        elif sub == "8":
            configure_user_mode(SESSION_SETTINGS)
        elif sub == "9":
            current = SESSION_SETTINGS.get("lawyer_name", "")
            print(f"{_ts()} {t('lawyer_name_current')}{current or t('lawyer_name_not_set')}")
            new_name = input(t("lawyer_name_prompt")).strip()
            if new_name:
                SESSION_SETTINGS["lawyer_name"] = new_name
                print(f"{_ts()} {t('lawyer_name_set')}{new_name}")
            elif new_name == "" and current:
                confirm = input("  Clear existing name? (y/n): ").strip().lower()
                if confirm == "y":
                    SESSION_SETTINGS.pop("lawyer_name", None)
                    print(f"{_ts()} {t('lawyer_name_cleared')}")
            _save_persistent_settings()
        elif sub == "10":
            current_ocr = SESSION_SETTINGS.get("gemini_enabled", False)
            print(f"\n  OCR — convert scanned PDFs to Hebrew text via Gemini Flash")
            print(f"  Current: {'On' if current_ocr else 'Off'}")
            ans = input("  Enable? (y/n): ").strip().lower()
            if ans == "y":
                existing_key = SESSION_SETTINGS.get("gemini_api_key", "")
                if existing_key:
                    print(f"  Existing API Key: {existing_key[:8]}...")
                    change = input("  Change API Key? (y/n): ").strip().lower()
                else:
                    change = "y"
                if change == "y":
                    new_key = input("  Gemini API Key (from aistudio.google.com): ").strip()
                    if new_key:
                        SESSION_SETTINGS["gemini_api_key"] = new_key
                SESSION_SETTINGS["gemini_enabled"] = True
                print("  [Settings] OCR Gemini: On.")
            else:
                SESSION_SETTINGS["gemini_enabled"] = False
                print("  [Settings] OCR Gemini: Off.")
            _save_persistent_settings()
        elif sub == "11":
            current_v = SESSION_SETTINGS.get("check_viewers", True)
            print(f"\n  Viewers check — scan document viewers (צופים) after each case download")
            print(f"  Current: {'On' if current_v else 'Off'}")
            ans = input("  Enable? (y/n): ").strip().lower()
            SESSION_SETTINGS["check_viewers"] = ans != "n"
            print(f"  [Settings] Viewers check: {'On' if SESSION_SETTINGS['check_viewers'] else 'Off'}.")
            _save_persistent_settings()
        elif sub in ("b", "back", ""):
            _save_persistent_settings()
            return
        else:
            print("Invalid choice.")


def _ensure_browser(playwright_context, browser_context, logger=None):
    """Return (playwright_context, browser_context, page) — relaunching if dead."""
    def _alive(bc) -> bool:
        if bc is None:
            return False
        try:
            pages = bc.pages
            if pages:
                p = pages[0]
                if p.is_closed():
                    return False
                _ = p.url
                # Actual driver round-trip — catches dead Playwright process
                p.evaluate("1")
            return True
        except Exception:
            return False

    if not _alive(browser_context):
        # Clean up dead references
        if playwright_context is not None:
            try:
                playwright_context.stop()
            except Exception:
                pass
        print(f"\n{_ts()} [INFO] {t('info_launching')}")
        if logger:
            logger.info("Launching Playwright persistent browser context.")
        playwright_context = sync_playwright().start()
        _bc = None
        for _attempt in range(3):
            try:
                _bc = playwright_context.chromium.launch_persistent_context(
                    user_data_dir=SHARED_PROFILE_DIR,
                    headless=False,
                    args=[
                        "--start-maximized",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                break
            except Exception as _exc:
                if _attempt < 2:
                    import time as _t
                    print(f"{_ts()} [Browser] Retrying launch ({_attempt+2}/3)...")
                    _t.sleep(2)
                else:
                    raise
        browser_context = _bc
    else:
        print(f"\n{_ts()} [INFO] {t('info_reusing')}")
        if logger:
            logger.info("Re-using existing browser context.")

    if browser_context.pages:
        page = browser_context.pages[0]
    else:
        page = browser_context.new_page()
    try:
        page.bring_to_front()
    except Exception:
        pass
    return playwright_context, browser_context, page


def _print_ux_tree() -> None:
    """Print a full ASCII UX decision tree to the terminal."""
    print("\n" + "=" * 65)
    print("  LIAS — UX MAP  (? to show again)")
    print("=" * 65)
    print("""
LAUNCH  python3 main.py
  │
  ├─ Language: he / en
  │
  └─ Welcome screen
       │
       ├─ [1]  Private Client ──────────────────────────────┐
       ├─ [2]  Lawyer / Rabbinical Pleader ─────────────────┤
       │         └─ entity type: private / lawyer / pleader  │
       ├─ [s]  Settings  (see below)                        │
       └─ [q]  Quit                                         │
                                                            ▼
                                                       MAIN MENU
  ┌─────────────────────────────────────────────────────────────────┐
  │  [1]  BDR — Rabbinical Courts (single case)                     │
  │         login → select entity → navigate → download → manifest  │
  │                                                                  │
  │  [2]  NET — Net HaMishpat (single case)                         │
  │         login → navigate to case → תיק נייר tab                 │
  │         → download page-by-page → decisions + viewers           │
  │         → related cases (if enabled) → global summary           │
  │                                                                  │
  │  [5]  BDR — Download ALL cases (batch)                          │
  │         Phase 1: discover all sub-cases → write batch_progress  │
  │         Phase 2: download each case → update progress live      │
  │         keyboard: "status" | "stop"                              │
  │                                                                  │
  │  [6]  NET — Update all existing cases                           │
  │    ├─ [1]  Update all existing NET folders                      │
  │    │        scan downloads/ → navigate each → new docs only     │
  │    │        smart-skip if already synced                        │
  │    ├─ [2]  Add new NET case (single download)                   │
  │    └─ [3]  Bulk date-range search → download all matches        │
  │                                                                  │
  │  [s]  Settings  (see below)                                      │
  │  [?]  Show this map                                              │
  │  [q]  Quit  (waits up to 60s for Drive queue)                   │
  │                                                                  │
  │  Drive controls (shown only when Drive is active):               │
  │  [d] show upload log   [p] pause/resume   [x] stop              │
  └─────────────────────────────────────────────────────────────────┘

  SETTINGS  [s]
  ┌── Connection & Automation ──────────────────────────────────────┐
  │  [1]  Credentials (ID + password) → saved in Keychain           │
  │  [2]  Login method: Standard (password+OTP) / Passkey           │
  │  [3]  Email OTP auto-read: enable → Gmail API or IMAP           │
  ├── Downloads ───────────────────────────────────────────────────┤
  │  [4]  Download mode: Updates Only / Full Re-download            │
  │  [5]  Date range filter: start + end (DD/MM/YYYY)              │
  │  [6]  NET related cases: yes / no                               │
  ├── Storage ─────────────────────────────────────────────────────┤
  │  [7]  Google Drive: local / local+Drive / Drive-only            │
  ├── User ────────────────────────────────────────────────────────┤
  │  [8]  User mode: Lawyer / Private                               │
  │  [9]  Lawyer name (for client identification)                   │
  ├── OCR ─────────────────────────────────────────────────────────┤
  │  [10] OCR — scanned PDF to text via Gemini Flash                │
  │         enable → enter API key → auto-runs after every download │
  │         result saved as <document>.txt beside the PDF           │
  └── [b]  Back ──────────────────────────────────────────────────┘

  OUTPUT STRUCTURE
  court_documents/
    logs/            latest.log + 20 rotations
    downloads/
      <client>/
        <case>/
          summary — <name>.csv       manifest (18–20 cols)
          sync_history — <name>.csv  change tracking + hash
          *.pdf                      downloaded documents
          *.txt                      OCR text (if enabled)
      all_cases_summary.csv          global summary (23 cols)
    מעקב אחר העלאות/
      sessions_summary.csv
      <case> — העלאות לדרייב/  latest.log + 19 rotations
""")
    print("=" * 65)


def _print_main_menu() -> None:
    """Print the main menu appropriate for the current user mode."""
    print("\n" + "=" * 60)
    mode_desc = t("menu_mode_updates") if SESSION_SETTINGS["mode"] == "1" else t("menu_mode_full")
    storage_desc = {
        "local": t("menu_storage_local"),
        "both": t("menu_storage_both"),
        "cloud": t("menu_storage_cloud"),
    }.get(SESSION_SETTINGS.get("storage_mode", "local"), t("menu_storage_local"))

    # Show Drive upload status if manager is active
    mgr = _get_drive_manager()
    if mgr is not None:
        print(mgr.status_line)

    _ENTITY_EN = {
        'עו"ד מייצג': "Legal Rep.", 'עו"ד': "Attorney",
        'טו"ר מייצג': "Religious Rep.", 'גורם פרטי': "Private",
    }
    if is_lawyer_mode(SESSION_SETTINGS):
        _raw_entity = SESSION_SETTINGS.get("bdr_entity_type", 'עו"ד מייצג')
        entity_desc = _ENTITY_EN.get(_raw_entity, _raw_entity)
        print(f"{t('menu_active_lawyer')} | {entity_desc} | Mode: {mode_desc} | Storage: {storage_desc}")
    else:
        print(f"{t('menu_active_private')} | Mode: {mode_desc} | Storage: {storage_desc}")
    print("-" * 60)
    print(t("menu_1"))
    print(t("menu_2"))
    print("  ---")
    print(t("menu_settings"))
    print(t("menu_quit_he"))

    # Drive options (shown only when manager is active)
    if _get_drive_manager() is not None:
        print("  ---")
        print("  d  — Drive upload log (live)")
        print("  p  — Pause / resume Drive uploads")
        print("  x  — Stop Drive uploads")
    print("  ?  — show full UX map / מפת תפריטים")
    print("=" * 60)


def run_cli() -> None:
    _load_persistent_settings()
    saved_lang = SESSION_SETTINGS.get("lang", "he")
    print("Language / שפה:")
    print("  he  עברית  [ברירת מחדל / Default]")
    print("  en  English")
    print(f"  (saved: {saved_lang} — press Enter to keep)")
    lang_choice = input("  → ").strip().lower()
    if lang_choice in ("he", "en"):
        SESSION_SETTINGS["lang"] = lang_choice
    # else keep the loaded value
    _save_persistent_settings()

    print("Legal-AI Scraper Active.")

    # 1. Init logger (no prompts)
    init_session_logger()

    # Init background Drive upload manager (if storage_mode is "both"/"cloud")
    _init_drive_manager(get_logger())

    playwright_context = None
    browser_context = None
    page = None

    # 2. Welcome screen — choose mode or settings
    while True:
        result = configure_user_mode(SESSION_SETTINGS)
        if result == "quit":
            print(f"\n{_ts()} [INFO] {t('info_goodbye')}")
            return
        elif result == "settings":
            _handle_settings_menu(get_logger())
            continue  # show welcome again
        else:
            break  # mode chosen, proceed to main loop

    # 3. Main loop
    try:
        while True:
            logger = get_logger()

            _print_main_menu()
            _drive_active = _get_drive_manager() is not None
            _prompt_extras = " / d / p / x" if _drive_active else ""
            choice = input(f"Enter choice (1/2/s/q{_prompt_extras} / ?): ").strip().lower()

            if choice in ("?", "h", "help"):
                _print_ux_tree()
                continue

            if choice == "q":
                print(f"\n{_ts()} [INFO] {t('info_terminated')}")
                if logger:
                    logger.info("Session terminated by user.")
                mgr = _get_drive_manager()
                if mgr is not None and mgr.is_running():
                    print(f"{_ts()} [Drive] Waiting for background uploads to finish... (up to 60s)")
                    mgr.stop(wait=True, timeout=60)
                break

            elif choice == "d":
                mgr = _get_drive_manager()
                if mgr is None:
                    print("Drive not active in this session.")
                else:
                    mgr.show_log()
                continue

            elif choice == "p":
                mgr = _get_drive_manager()
                if mgr is None:
                    print("Drive not active in this session.")
                else:
                    if mgr._is_paused:
                        mgr.resume()
                    else:
                        mgr.pause()
                continue

            elif choice == "x":
                mgr = _get_drive_manager()
                if mgr is None:
                    print("Drive not active in this session.")
                else:
                    print("[Drive] Stopping background uploads...")
                    mgr.stop(wait=False)
                    print("[Drive] Stop signal sent.")
                continue

            elif choice == "s":
                _handle_settings_menu(logger)

            elif choice == "1":
                # ── BDR — Rabbinical Courts ──────────────────────────────────
                _related_on = SESSION_SETTINGS.get("download_related_cases", False)
                print(f"\n  {t('bdr_sub_title')}")
                print("  " + "-" * 36)
                print(t("bdr_sub_1"))
                print(t("bdr_sub_2"))
                print(t("bdr_sub_back"))
                sub_bdr = input(t("bdr_sub_prompt")).strip().lower()

                if sub_bdr in ("b", ""):
                    continue

                playwright_context, browser_context, page = _ensure_browser(
                    playwright_context, browser_context, logger
                )

                if sub_bdr == "1":
                    # BDR single case (original choice "1")
                    print(f"Navigating to Rabbinical Courts (BDR)...")
                    if logger:
                        logger.info("Navigating to BDR (single case).")
                    try:
                        connection = GovIlConnection(ConnectionType.BDR)
                        connection.connect(page)
                        from core.gov_login import auto_login_flow, is_gov_login_page
                        if is_gov_login_page(page):
                            email_reader = None
                            if SESSION_SETTINGS.get("email_enabled"):
                                from core.email_otp import EmailOTPReader, load_email_config as _load_cfg
                                cfg = _load_cfg(Path("email_config.json"))
                                if cfg:
                                    email_reader = EmailOTPReader(cfg, logger)
                            auto_login_flow(page, email_reader=email_reader, logger=logger)
                        elif SESSION_SETTINGS.get("email_enabled"):
                            email_cfg = load_email_config(SESSION_SETTINGS.get("email_config_path"))
                            if email_cfg:
                                _attempt_auto_login_if_needed(page, email_cfg, logger)
                        _set_per_file_gdrive_callback(logger)
                        try:
                            run_download(page, ConnectionType.BDR)
                        finally:
                            _clear_per_file_gdrive_callback()
                        print(f"\n{_ts()} [SUCCESS] Sync complete — BDR.")
                        if logger:
                            logger.ok("Sync complete — BDR.")
                        _upload_logs_gdrive(logger)
                    except UserGoBack:
                        print(f"\n{_ts()} [INFO] {t('info_returning')}")
                    except KeyboardInterrupt:
                        print(f"\n{_ts()} [INFO] {t('info_interrupted')}")
                    except Exception as e:
                        msg = f"BDR single-case error: {e}"
                        print(f"\n{_ts()} [ERROR] {msg}")
                        if logger:
                            logger.error(msg)
                        if any(kw in str(e).lower() for kw in ("connection closed", "target closed", "browser has been closed")):
                            print(f"{_ts()} [INFO] {t('info_conn_lost')}")
                            browser_context = None

                elif sub_bdr == "2":
                    # BDR batch (original choice "5")
                    from core.bdr_batch import BDR_FILES_URL, BdrBatchRunner
                    print(f"\n{_ts()} [INFO] Starting BDR batch mode...")
                    if logger:
                        logger.info("Starting BDR batch mode.")
                    print(f"{_ts()} [INFO] Navigating to BDR portal...")
                    try:
                        page.goto(BDR_FILES_URL, wait_until="networkidle", timeout=20000)
                    except Exception:
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            pass
                    try:
                        from core.gov_login import (
                            _is_already_logged_in_bdr,
                            is_bdr_login_page,
                            handle_bdr_login_page,
                        )
                        from core.connection import _run_gov_autologin
                        import time as _time
                        if _is_already_logged_in_bdr(page):
                            print(f"{_ts()} [Auth] Already logged in to BDR.")
                        else:
                            if is_bdr_login_page(page):
                                handle_bdr_login_page(page, session_settings=SESSION_SETTINGS)
                            _run_gov_autologin(page, "BDR")
                            _time.sleep(1.5)
                            for _ in range(3):
                                if is_bdr_login_page(page):
                                    handle_bdr_login_page(page, session_settings=SESSION_SETTINGS)
                                    _time.sleep(2)
                                    break
                                if _is_already_logged_in_bdr(page):
                                    break
                                _time.sleep(1)
                    except Exception as _e:
                        print(f"{_ts()} [Auth] BDR auto-login skipped: {_e}")

                    if is_lawyer_mode(SESSION_SETTINGS):
                        lawyer_name = SESSION_SETTINGS.get("lawyer_name", "")
                        if lawyer_name:
                            from core.client_inference import infer_client_name
                            _client_name, _mismatches = infer_client_name(ROOT_OUTPUT_DIR, lawyer_name)
                            if _mismatches:
                                print(f"{_ts()} {t('lawyer_mismatch', names=', '.join(_mismatches[:5]))}")
                            if _client_name:
                                print(f"{_ts()} {t('client_inferred', client=_client_name)}")
                                SESSION_SETTINGS["client_name"] = _client_name
                            else:
                                print(f"{_ts()} {t('client_not_inferred')}")
                                SESSION_SETTINGS.pop("client_name", None)

                    _stop_event_5 = threading.Event()
                    _input_q_5: queue.Queue = queue.Queue()

                    def _read_input_5() -> None:
                        while not _stop_event_5.is_set():
                            try:
                                line = input()
                                _input_q_5.put(line.strip().lower())
                            except EOFError:
                                break

                    threading.Thread(target=_read_input_5, daemon=True, name="BdrBatchInput").start()
                    print("Commands during download: 'status' — show progress | 'stop' — cancel\n")

                    if SESSION_SETTINGS.get("storage_mode") in ("both", "cloud"):
                        _pre_sync_gdrive(ROOT_OUTPUT_DIR, logger)

                    try:
                        batch = BdrBatchRunner(page, logger=logger)
                        batch.run(SESSION_SETTINGS, ROOT_OUTPUT_DIR)
                    except KeyboardInterrupt:
                        print(f"\n{_ts()} [INFO] {t('info_interrupted')}")
                        if logger:
                            logger.info("BDR batch interrupted.")
                        try:
                            if page is not None:
                                page.goto("about:blank", wait_until="commit", timeout=3000)
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"\n{_ts()} [ERROR] BDR batch error: {e}")
                        if logger:
                            logger.error(f"BDR batch error: {e}")
                    finally:
                        _stop_event_5.set()

                else:
                    print("Invalid choice — enter 1, 2, or b.")

            elif choice == "2":
                # ── NET — Net HaMishpat ──────────────────────────────────────
                _related_on = SESSION_SETTINGS.get("download_related_cases", False)
                _related_tag = "  [+ related cases on]" if _related_on else ""
                print(f"\n  {t('net_sub_title')}")
                print("  " + "-" * 36)
                print(t("net_sub_1") + _related_tag)
                print(t("net_sub_2"))
                print(t("net_sub_3"))
                print(t("net_sub_back"))
                sub_net = input(t("net_sub_prompt")).strip().lower()

                if sub_net in ("b", ""):
                    continue

                playwright_context, browser_context, page = _ensure_browser(
                    playwright_context, browser_context, logger
                )

                if sub_net == "1":
                    # NET single case (original choice "2")
                    print(f"Navigating to Net HaMishpat (NET)...")
                    if logger:
                        logger.info("Navigating to NET (single case).")
                    try:
                        connection = GovIlConnection(ConnectionType.NET)
                        connection.connect(page)
                        from core.gov_login import auto_login_flow, is_gov_login_page
                        if is_gov_login_page(page):
                            email_reader = None
                            if SESSION_SETTINGS.get("email_enabled"):
                                from core.email_otp import EmailOTPReader, load_email_config as _load_cfg
                                cfg = _load_cfg(Path("email_config.json"))
                                if cfg:
                                    email_reader = EmailOTPReader(cfg, logger)
                            auto_login_flow(page, email_reader=email_reader, logger=logger)
                        elif SESSION_SETTINGS.get("email_enabled"):
                            email_cfg = load_email_config(SESSION_SETTINGS.get("email_config_path"))
                            if email_cfg:
                                _attempt_auto_login_if_needed(page, email_cfg, logger)
                        _set_per_file_gdrive_callback(logger)
                        try:
                            run_download(page, ConnectionType.NET)
                        finally:
                            _clear_per_file_gdrive_callback()
                        print(f"\n{_ts()} [SUCCESS] Sync complete — NET.")
                        if logger:
                            logger.ok("Sync complete — NET.")
                        _upload_logs_gdrive(logger)
                    except UserGoBack:
                        print(f"\n{_ts()} [INFO] {t('info_returning')}")
                    except KeyboardInterrupt:
                        print(f"\n{_ts()} [INFO] {t('info_interrupted')}")
                    except Exception as e:
                        msg = f"NET single-case error: {e}"
                        print(f"\n{_ts()} [ERROR] {msg}")
                        if logger:
                            logger.error(msg)
                        if any(kw in str(e).lower() for kw in ("connection closed", "target closed", "browser has been closed")):
                            print(f"{_ts()} [INFO] {t('info_conn_lost')}")
                            browser_context = None

                elif sub_net == "2":
                    # NET auto-update all existing cases (original choice "6.1")
                    print(f"{_ts()} [INFO] Opening NET portal for auto-update...")
                    try:
                        _ensure_logged_in(page, "NET", logger)
                    except UserGoBack:
                        print(f"\n{_ts()} [INFO] {t('info_returning')}")
                        continue
                    try:
                        from core.net_auto_update import run_net_auto_update
                        _set_per_file_gdrive_callback(logger)
                        try:
                            run_net_auto_update(
                                page=page,
                                logger=logger,
                                root_output_dir=ROOT_OUTPUT_DIR,
                                session_settings=SESSION_SETTINGS,
                                resolve_paths_fn=resolve_smart_paths,
                            )
                        finally:
                            _clear_per_file_gdrive_callback()
                        _upload_logs_gdrive(logger)
                    except KeyboardInterrupt:
                        print(f"\n{_ts()} [INFO] {t('info_interrupted')}")
                        if logger:
                            logger.info("NET auto-update interrupted.")
                        try:
                            if page is not None:
                                page.goto("about:blank", wait_until="commit", timeout=3000)
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"\n{_ts()} [ERROR] Auto-update error: {e}")
                        if logger:
                            logger.error(f"Auto-update error: {e}")

                elif sub_net == "3":
                    # NET date-range search → bulk download (original choice "6.3")
                    try:
                        _ensure_logged_in(page, "NET", logger)
                    except UserGoBack:
                        print(f"\n{_ts()} [INFO] {t('info_returning')}")
                        continue
                    try:
                        years_input = input(
                            "  How many years back to search? (default: 10): "
                        ).strip()
                        years_back = int(years_input) if years_input.isdigit() else 10
                        from core.net_search_cases import run_bulk_download_from_date_search
                        _set_per_file_gdrive_callback(logger)
                        try:
                            run_bulk_download_from_date_search(
                                page=page,
                                root_output_dir=ROOT_OUTPUT_DIR,
                                session_settings=SESSION_SETTINGS,
                                logger=logger,
                                years_back=years_back,
                            )
                        finally:
                            _clear_per_file_gdrive_callback()
                        _upload_logs_gdrive(logger)
                    except KeyboardInterrupt:
                        print(f"\n{_ts()} [INFO] {t('info_interrupted')}")
                        if logger:
                            logger.info("Bulk search interrupted.")
                    except Exception as e:
                        print(f"\n{_ts()} [ERROR] Bulk search error: {e}")
                        if logger:
                            logger.error(f"Bulk search error: {e}")

                else:
                    print("Invalid choice — enter 1, 2, 3, or b.")

            else:
                print("Invalid entry. Please choose 1, 2, s, or q.")

    finally:
        if browser_context is not None:
            print(f"\n{_ts()} [INFO] Closing browser cleanly...")
            lg = get_logger()
            if lg:
                lg.info("Browser context closed.")
            try:
                browser_context.close()
            except Exception:
                pass
        if playwright_context is not None:
            try:
                playwright_context.stop()
            except Exception:
                pass
        _upload_logs_gdrive(get_logger())


if __name__ == "__main__":
    try:
        run_cli()
    except KeyboardInterrupt:
        print(f"\n\n{_ts()} [INFO] Interrupted by user. Exiting...")
        sys.exit(0)
