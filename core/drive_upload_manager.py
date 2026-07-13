"""Background Google Drive upload manager.

Runs a daemon thread that continuously uploads queued files to Drive, mirrors
the local folder hierarchy under the Drive root folder, and maintains:

  court_documents/
    מעקב אחר העלאות/
      sessions_summary.csv               ← session-level overview
      <case_dir_name> — העלאות לדרייב/   ← per-case upload logs (20 rotations)
        latest.log
        log_1.log … log_19.log

After each successful upload:
  • Marks 'עלה לDrive' and 'נתיב בדרייב' in the case manifest CSV.
  • Appends a row to the case's upload log.
  • Updates sessions_summary.csv.

Thread safety: manifest updates use a per-case threading.Lock.
"""

from __future__ import annotations

import csv
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from core.logger import Logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# NOTE: overwritten at runtime by gdrive.DRIVE_ROOT_FOLDER — keep in sync
DRIVE_ROOT_FOLDER = "Legal-AI Court Downloads"
UPLOAD_TRACKING_DIR = "drive-uploads"
_OLD_UPLOAD_TRACKING_DIR = "מעקב אחר העלאות"  # legacy Hebrew name — migrated on first run
SESSION_SUMMARY_NAME = "sessions_summary.csv"
LOG_MAX_ROTATIONS = 19  # keep latest.log + log_1 … log_19

_SESSION_SUMMARY_COLS = [
    "תחילת הרצה", "סיום הרצה", "תיקים שטופלו",
    "קבצים שהועלו", "קבצים שנדלגו", "קבצים שנכשלו",
    "סטטוס", "הערות",
]

_UPLOAD_LOG_COLS = [
    "שם מסמך",
    "שם קובץ",
    "תאריך הגשה לתיק",
    "תאריך הורדה למחשב",
    "תחילת העלאה",
    "סיום העלאה",
    "גודל (KB)",
    "נתיב בדרייב",
    "סטטוס",
    "הערה",
]

# ---------------------------------------------------------------------------
# UploadJob
# ---------------------------------------------------------------------------

class UploadJob(NamedTuple):
    file_path: Path
    case_dir: Path
    root_dir: Path
    doc_meta: dict  # optional metadata from manifest row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rotate_log(log_path: Path) -> None:
    """Rotate latest.log → log_1.log → … → log_19.log, drop log_20+."""
    if not log_path.exists() or log_path.stat().st_size == 0:
        return
    parent = log_path.parent
    for i in range(LOG_MAX_ROTATIONS, 0, -1):
        cur = parent / f"log_{i}.log"
        nxt = parent / f"log_{i + 1}.log"
        if cur.exists():
            if i == LOG_MAX_ROTATIONS and nxt.exists():
                try:
                    nxt.unlink()
                except Exception:
                    pass
            try:
                cur.rename(nxt)
            except Exception:
                pass
    try:
        log_path.rename(parent / "log_1.log")
    except Exception:
        pass


def _case_upload_dir(case_dir: Path, tracking_root: Path) -> Path:
    """Return (and create) the upload-log folder for a given case."""
    folder_name = f"{case_dir.name} — העלאות לדרייב"
    d = tracking_root / folder_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_upload_log_row(upload_dir: Path, row: dict) -> None:
    """Append one row to latest.log in the case's upload-log folder."""
    log_path = upload_dir / "latest.log"
    write_header = not log_path.exists()
    try:
        with log_path.open("a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_UPLOAD_LOG_COLS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        pass


def _build_drive_path(root_dir: Path, file_path: Path) -> str:
    """Build a human-readable Drive path string."""
    try:
        rel = file_path.relative_to(root_dir)
        return f"{DRIVE_ROOT_FOLDER}/{root_dir.name}/{rel}"
    except ValueError:
        return f"{DRIVE_ROOT_FOLDER}/{file_path.name}"


# ---------------------------------------------------------------------------
# Manifest update (thread-safe per case_dir)
# ---------------------------------------------------------------------------

_manifest_locks: dict[str, threading.Lock] = {}
_manifest_locks_lock = threading.Lock()


def _get_manifest_lock(case_dir: Path) -> threading.Lock:
    key = str(case_dir)
    with _manifest_locks_lock:
        if key not in _manifest_locks:
            _manifest_locks[key] = threading.Lock()
        return _manifest_locks[key]


def _mark_uploaded_in_manifest(
    file_path: Path,
    case_dir: Path,
    drive_path: str,
    upload_time: str,
) -> bool:
    """Update 'עלה לDrive' and 'נתיב בדרייב' in the case manifest CSV.

    Uses a per-case lock to prevent concurrent writes from main + upload threads.
    Returns True if a row was updated.
    """
    # Find the summary CSV in case_dir
    summary_csv: Path | None = None
    try:
        for candidate in case_dir.iterdir():
            if candidate.is_file() and candidate.suffix == ".csv" and candidate.stem.startswith("summary"):
                summary_csv = candidate
                break
    except Exception:
        return False

    if summary_csv is None:
        return False

    lock = _get_manifest_lock(case_dir)
    with lock:
        try:
            rows: list[dict] = []
            fieldnames: list[str] = []
            with summary_csv.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                rows = list(reader)

            if not fieldnames:
                return False

            # Ensure the two Drive columns exist
            for col in ("עלה לDrive", "נתיב בדרייב"):
                if col not in fieldnames:
                    fieldnames.append(col)

            fname = file_path.name
            changed = False
            for row in rows:
                if row.get("שם קובץ פיזי בדיסק", "") == fname:
                    row["עלה לDrive"] = upload_time
                    row["נתיב בדרייב"] = drive_path
                    changed = True

            if not changed:
                return False

            with summary_csv.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Session summary
# ---------------------------------------------------------------------------

def _update_session_summary(
    tracking_root: Path,
    session_start: str,
    session_end: str,
    cases: set[str],
    uploaded: int,
    skipped: int,
    failed: int,
    status: str,
    notes: str = "",
) -> None:
    summary_path = tracking_root / SESSION_SUMMARY_NAME
    rows: list[dict] = []
    if summary_path.exists():
        try:
            with summary_path.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            pass

    rows.append({
        "תחילת הרצה": session_start,
        "סיום הרצה": session_end,
        "תיקים שטופלו": "; ".join(sorted(cases)) if cases else "",
        "קבצים שהועלו": str(uploaded),
        "קבצים שנדלגו": str(skipped),
        "קבצים שנכשלו": str(failed),
        "סטטוס": status,
        "הערות": notes,
    })

    try:
        with summary_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_SESSION_SUMMARY_COLS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# DriveUploadManager
# ---------------------------------------------------------------------------

class DriveUploadManager:
    """Background Drive upload manager.

    Usage:
        manager = DriveUploadManager(root_dir, credentials_path, token_path, logger)
        manager.start()

        # During download loop:
        manager.enqueue(file_path, case_dir, doc_meta)

        # From menu:
        manager.show_log()   # print last lines of upload log
        manager.pause()      # pause uploads
        manager.resume()     # resume uploads
        manager.stop()       # stop cleanly (finishes current upload)
        manager.status_line  # one-line string for display
    """

    def __init__(
        self,
        root_dir: Path,
        credentials_path: Path,
        token_path: Path,
        logger: "Logger | None" = None,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.logger = logger

        self.tracking_root = self.root_dir / UPLOAD_TRACKING_DIR
        # Migrate legacy Hebrew folder name on first run
        _old = self.root_dir / _OLD_UPLOAD_TRACKING_DIR
        if _old.exists() and not self.tracking_root.exists():
            try:
                _old.rename(self.tracking_root)
            except Exception:
                pass
        self.tracking_root.mkdir(parents=True, exist_ok=True)

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially

        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Stats (protected by _lock)
        self._uploaded = 0
        self._skipped = 0
        self._failed = 0
        self._queued = 0
        self._current_file: str = ""
        self._cases_touched: set[str] = set()
        self._session_start = _now()
        self._last_status = "Idle"
        self._last_error = ""
        self._is_paused = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background upload thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="DriveUploadThread")
        self._thread.start()
        self._log("Background upload thread started.")

    def enqueue(
        self,
        file_path: Path,
        case_dir: Path,
        doc_meta: dict | None = None,
    ) -> None:
        """Add a file to the upload queue."""
        job = UploadJob(
            file_path=Path(file_path),
            case_dir=Path(case_dir),
            root_dir=self.root_dir,
            doc_meta=doc_meta or {},
        )
        self._queue.put(job)
        with self._lock:
            self._queued += 1
        self._log(f"Queued: {file_path.name}  (queue size: {self._queue.qsize()})")

    def stop(self, wait: bool = True, timeout: float = 60.0) -> None:
        """Signal the upload thread to stop after finishing the current upload."""
        self._log("Stop requested.")
        self._stop_event.set()
        self._queue.put(None)  # unblock queue.get()
        if wait and self._thread:
            self._thread.join(timeout=timeout)
        self._finalize_session("Stopped")

    def pause(self) -> None:
        """Pause uploads (current upload will finish before pausing)."""
        self._pause_event.clear()
        with self._lock:
            self._is_paused = True
            self._last_status = "Paused"
        print("[Drive] ⏸  Uploads paused.")

    def resume(self) -> None:
        """Resume uploads."""
        self._pause_event.set()
        with self._lock:
            self._is_paused = False
            self._last_status = "Active"
        print("[Drive] ▶  Uploads resumed.")

    def wait_for_idle(self, timeout: float = 300.0) -> bool:
        """Block until queue is empty. Returns True if queue drained within timeout."""
        start = time.time()
        while not self._queue.empty():
            if time.time() - start > timeout:
                return False
            time.sleep(1)
        return True

    @property
    def status_line(self) -> str:
        with self._lock:
            q = self._queue.qsize()
            up = self._uploaded
            sk = self._skipped
            fa = self._failed
            cur = self._current_file
            st = self._last_status
        parts = [f"[Drive] {st}"]
        if cur:
            parts.append(f"uploading: {cur}")
        parts.append(f"✓{up}  ↷{sk}  ✗{fa}  queue:{q}")
        return "  ".join(parts)

    def show_log(self, case_dir: Path | None = None, lines: int = 40) -> None:
        """Print last N lines of the upload log to console."""
        print("\n" + "=" * 60)
        print("  [Drive] יומן העלאות אחרון")
        print("=" * 60)

        if case_dir is not None:
            # Show log for a specific case
            upload_dir = _case_upload_dir(case_dir, self.tracking_root)
            log_path = upload_dir / "latest.log"
            self._print_log_file(log_path, lines)
        else:
            # Show logs for all cases (most recent first)
            dirs = sorted(
                [d for d in self.tracking_root.iterdir() if d.is_dir()],
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )
            shown = 0
            for d in dirs[:5]:  # last 5 cases
                log_path = d / "latest.log"
                if log_path.exists():
                    print(f"\n  ── {d.name} ──")
                    self._print_log_file(log_path, max(lines // 5, 10))
                    shown += 1
            if not shown:
                print("  אין לוגים עדיין.")
        print("=" * 60 + "\n")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _latest_log_file(self) -> "Path | None":
        """Return the most recently modified latest.log under tracking_root."""
        best: "Path | None" = None
        best_mtime = 0.0
        try:
            for d in self.tracking_root.iterdir():
                lp = d / "latest.log"
                if lp.exists():
                    m = lp.stat().st_mtime
                    if m > best_mtime:
                        best_mtime = m
                        best = lp
        except Exception:
            pass
        return best

    def live_view(self) -> None:
        """Live tail of the upload log — blocks until the user presses Enter.

        Downloads can continue in the background while this is active.
        Drive uploads also continue — only the terminal view switches.
        """
        SEP = "─" * 62

        log_path = self._latest_log_file()

        print(f"\n{SEP}")
        print("  [Drive] Live upload log  —  Enter / b = back to downloads")
        print(f"  {self.status_line}")
        print(SEP)

        if log_path is None:
            print("  No upload log yet.")
            try:
                input("  Press Enter to return... ")
            except (EOFError, KeyboardInterrupt):
                pass
            print(f"{SEP}\n")
            return

        # Print last 20 uploaded rows as context
        try:
            with log_path.open(encoding="utf-8-sig", newline="") as f:
                all_rows = list(csv.DictReader(f))
            for row in all_rows[-20:]:
                status = row.get("סטטוס", "")
                icon = "✓" if "הועלה" in status else ("↷" if "נדלג" in status else "✗")
                fname = row.get("שם קובץ", "")[:42]
                size = row.get("גודל (KB)", "")
                ts = row.get("סיום העלאה", "")
                print(f"  {icon} {fname:<42}  {size:>8} KB  {ts}")
            prior_row_count = len(all_rows)
        except Exception:
            prior_row_count = 0

        print(SEP)
        print("  Streaming new lines... (Enter/b to return to downloads)")
        print(SEP)

        _stop_tail = threading.Event()

        def _tail() -> None:
            nonlocal prior_row_count
            while not _stop_tail.is_set():
                try:
                    with log_path.open(encoding="utf-8-sig", newline="") as f:
                        rows = list(csv.DictReader(f))
                    new_rows = rows[prior_row_count:]
                    for row in new_rows:
                        status = row.get("סטטוס", "")
                        icon = "✓" if "הועלה" in status else ("↷" if "נדלג" in status else "✗")
                        fname = row.get("שם קובץ", "")[:42]
                        size = row.get("גודל (KB)", "")
                        ts = row.get("סיום העלאה", "")
                        print(f"  {icon} {fname:<42}  {size:>8} KB  {ts}", flush=True)
                    prior_row_count = len(rows)
                except Exception:
                    pass
                _stop_tail.wait(timeout=1.0)

        tail_thread = threading.Thread(target=_tail, daemon=True, name="DriveTail")
        tail_thread.start()

        try:
            input("")
        except (EOFError, KeyboardInterrupt):
            pass

        _stop_tail.set()
        tail_thread.join(timeout=2)

        print(SEP)
        print(f"  [Drive] {self.status_line}")
        print(f"{SEP}\n")

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Main loop of the background upload thread."""
        from core.gdrive import GDriveUploader, DRIVE_ROOT_FOLDER as _ROOT
        global DRIVE_ROOT_FOLDER
        DRIVE_ROOT_FOLDER = _ROOT  # sync with gdrive.py constant

        with self._lock:
            self._last_status = "Connecting to Drive..."

        uploader = GDriveUploader(self.credentials_path, self.token_path, self.logger)
        if not uploader.authenticate():
            with self._lock:
                self._last_status = "Auth failed"
                self._last_error = "Drive authentication failed"
            self._log("Authentication failed — upload thread exiting.", level="error")
            self._report_daemon_error(
                "אימות Drive נכשל — ייתכן ש-token.json פג. "
                "לחץ 'חבר מחדש Drive' בהגדרות או מחק את token.json והפעל מחדש."
            )
            self._finalize_session("Auth failed")
            return

        # Ensure top-level Drive folder exists
        try:
            root_id = uploader.get_or_create_folder(DRIVE_ROOT_FOLDER, parent_id=None)
        except Exception as e:
            with self._lock:
                self._last_status = f"Error: {e}"
            self._log(f"Cannot create Drive root folder: {e}", level="error")
            self._report_daemon_error(f"יצירת תיקיית השורש בדרייב נכשלה: {e}")
            self._finalize_session("Error")
            return

        with self._lock:
            self._last_status = "Active"

        self._log(f"Ready. Drive root: {DRIVE_ROOT_FOLDER} (id={root_id})")

        # Cache: local_dir_path → drive_folder_id
        _folder_cache: dict[str, str] = {str(self.root_dir): root_id}

        def _get_drive_folder(local_dir: Path) -> str:
            key = str(local_dir)
            if key in _folder_cache:
                return _folder_cache[key]
            try:
                parts = local_dir.relative_to(self.root_dir).parts
            except ValueError:
                parts = (local_dir.name,)
            parent_id = root_id
            built = self.root_dir
            for part in parts:
                built = built / part
                bkey = str(built)
                if bkey in _folder_cache:
                    parent_id = _folder_cache[bkey]
                else:
                    parent_id = uploader.get_or_create_folder(part, parent_id=parent_id)
                    _folder_cache[bkey] = parent_id
            _folder_cache[key] = parent_id
            return parent_id

        while not self._stop_event.is_set():
            # Wait if paused
            self._pause_event.wait()

            try:
                job = self._queue.get(timeout=2)
            except queue.Empty:
                continue

            if job is None or self._stop_event.is_set():
                break

            self._process_job(job, uploader, _get_drive_folder)

        with self._lock:
            self._last_status = "Done"
        self._log("Upload thread finished.")
        self._finalize_session("Done")

    def _process_job(self, job: UploadJob, uploader, get_drive_folder_fn) -> None:
        file_path = job.file_path
        case_dir = job.case_dir
        doc_meta = job.doc_meta

        if not file_path.exists():
            self._log(f"File gone — skip: {file_path.name}", level="warn")
            with self._lock:
                self._skipped += 1
            return

        with self._lock:
            self._current_file = file_path.name
            self._last_status = f"Uploading: {file_path.name}"

        upload_start = _now()
        drive_path = _build_drive_path(self.root_dir, file_path)
        size_kb = round(file_path.stat().st_size / 1024, 2) if file_path.exists() else 0

        doc_name = doc_meta.get("שם מסמך (מהטבלה)", file_path.stem)
        doc_date = doc_meta.get("תאריך מסמך", "")
        download_date = doc_meta.get("מועד הרצה", "")

        upload_dir = _case_upload_dir(case_dir, self.tracking_root)

        # Real-time visibility: log the START of the upload immediately,
        # and mirror it to the UI over SSE.
        _write_upload_log_row(upload_dir, {
            "שם מסמך": doc_name, "שם קובץ": file_path.name,
            "תאריך הגשה לתיק": doc_date, "תאריך הורדה למחשב": download_date,
            "תחילת העלאה": upload_start, "סיום העלאה": "",
            "גודל (KB)": str(size_kb), "נתיב בדרייב": drive_path,
            "סטטוס": "מתחיל העלאה", "הערה": "",
        })
        self._broadcast_sse(f"⬆ מתחיל העלאה: {file_path.name}")

        try:
            drive_folder_id = get_drive_folder_fn(file_path.parent)

            # Check if already on Drive
            from googleapiclient.errors import HttpError
            existing = (
                uploader._service.files()
                .list(
                    q=f"name='{file_path.name}' and '{drive_folder_id}' in parents and trashed=false",
                    fields="files(id)",
                    spaces="drive",
                )
                .execute()
                .get("files", [])
            )

            upload_end = _now()

            if existing:
                self._log(f"Already on Drive (skip): {file_path.name}")
                with self._lock:
                    self._skipped += 1
                    self._current_file = ""
                    self._cases_touched.add(case_dir.name)
                _write_upload_log_row(upload_dir, {
                    "שם מסמך": doc_name, "שם קובץ": file_path.name,
                    "תאריך הגשה לתיק": doc_date, "תאריך הורדה למחשב": download_date,
                    "תחילת העלאה": upload_start, "סיום העלאה": upload_end,
                    "גודל (KB)": str(size_kb), "נתיב בדרייב": drive_path,
                    "סטטוס": "קיים (נדלג)", "הערה": "",
                })
                _mark_uploaded_in_manifest(file_path, case_dir, drive_path, upload_end)
                return

            file_id = uploader.upload_file(file_path, drive_folder_id)
            upload_end = _now()

            _mark_uploaded_in_manifest(file_path, case_dir, drive_path, upload_end)

            _write_upload_log_row(upload_dir, {
                "שם מסמך": doc_name, "שם קובץ": file_path.name,
                "תאריך הגשה לתיק": doc_date, "תאריך הורדה למחשב": download_date,
                "תחילת העלאה": upload_start, "סיום העלאה": upload_end,
                "גודל (KB)": str(size_kb), "נתיב בדרייב": drive_path,
                "סטטוס": "הועלה", "הערה": f"Drive ID: {file_id}",
            })

            with self._lock:
                self._uploaded += 1
                self._current_file = ""
                self._cases_touched.add(case_dir.name)
                self._last_status = "Active"

            # Print to main thread console (interleaved is acceptable)
            print(f"\r[Drive] ↑ {file_path.name}  ({size_kb} KB)")
            self._broadcast_sse(f"✓ הועלה: {file_path.name} ({size_kb} KB)")

        except Exception as e:
            upload_end = _now()
            err_str = str(e)[:120]
            self._log(f"FAILED: {file_path.name} — {err_str}", level="error")

            _write_upload_log_row(upload_dir, {
                "שם מסמך": doc_name, "שם קובץ": file_path.name,
                "תאריך הגשה לתיק": doc_date, "תאריך הורדה למחשב": download_date,
                "תחילת העלאה": upload_start, "סיום העלאה": upload_end,
                "גודל (KB)": str(size_kb), "נתיב בדרייב": drive_path,
                "סטטוס": "נכשל", "הערה": err_str,
            })

            with self._lock:
                self._failed += 1
                self._current_file = ""
                self._last_error = err_str
                self._cases_touched.add(case_dir.name)
            self._broadcast_sse(f"✗ העלאה נכשלה: {file_path.name} — {err_str}", event_type="drive_error")

            # Alert user if it looks like a network error
            if any(kw in err_str.lower() for kw in ("connection", "timeout", "network", "socket")):
                print(f"\n[Drive] ⚠  שגיאת רשת בהעלאת {file_path.name}: {err_str}")
                print("[Drive]    Use p=pause / x=stop from main menu.\n")

    # ------------------------------------------------------------------
    # Session finalization
    # ------------------------------------------------------------------

    def _finalize_session(self, status: str) -> None:
        with self._lock:
            up = self._uploaded
            sk = self._skipped
            fa = self._failed
            cases = set(self._cases_touched)

        _update_session_summary(
            tracking_root=self.tracking_root,
            session_start=self._session_start,
            session_end=_now(),
            cases=cases,
            uploaded=up,
            skipped=sk,
            failed=fa,
            status=status,
        )

        # Rotate upload logs for all touched cases
        for case_name in cases:
            upload_dir = self.tracking_root / f"{case_name} — העלאות לדרייב"
            if upload_dir.is_dir():
                _rotate_log(upload_dir / "latest.log")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str, level: str = "info") -> None:
        prefixed = f"[DriveManager] {msg}"
        if self.logger:
            getattr(self.logger, level, self.logger.info)(prefixed)
        # Don't print to stdout — would interleave badly with download progress

    def _broadcast_sse(self, message: str, event_type: str = "drive") -> None:
        """Mirror a drive event to the web UI (no-op outside LIAS)."""
        try:
            from LIAS import jobs as _jobs
            _jobs.broadcast({"type": event_type, "message": message})
        except Exception:
            pass

    def _report_daemon_error(self, message: str) -> None:
        """Persist a daemon-level failure where the user can find it, and
        notify the UI. Without this, auth failures died silently and no
        latest.log was ever created."""
        try:
            self.tracking_root.mkdir(parents=True, exist_ok=True)
            err_path = self.tracking_root / "daemon-error.log"
            with err_path.open("a", encoding="utf-8") as f:
                f.write(f"[{_now()}] {message}\n")
        except Exception:
            pass
        self._broadcast_sse(f"⚠ Drive: {message}", event_type="drive_error")

    def _print_log_file(self, log_path: Path, lines: int) -> None:
        if not log_path.exists():
            print("  (אין לוג עדיין)")
            return
        try:
            with log_path.open(encoding="utf-8-sig", newline="") as f:
                all_rows = list(csv.DictReader(f))
            for row in all_rows[-lines:]:
                status = row.get("סטטוס", "")
                icon = "✓" if "הועלה" in status else ("↷" if "נדלג" in status else "✗")
                print(
                    f"  {icon} {row.get('שם קובץ','')[:40]:<40}  "
                    f"{row.get('גודל (KB)',''):>8} KB  "
                    f"{row.get('סיום העלאה','')}"
                )
        except Exception as e:
            print(f"  (שגיאה בקריאת לוג: {e})")


# ---------------------------------------------------------------------------
# Module-level singleton (set by runner.py after init)
# ---------------------------------------------------------------------------

_manager: DriveUploadManager | None = None


def get_manager() -> DriveUploadManager | None:
    return _manager


def init_manager(
    root_dir: Path,
    credentials_path: Path,
    token_path: Path,
    logger=None,
) -> DriveUploadManager:
    """Create, start and register the global upload manager."""
    global _manager
    _manager = DriveUploadManager(root_dir, credentials_path, token_path, logger)
    _manager.start()
    return _manager
