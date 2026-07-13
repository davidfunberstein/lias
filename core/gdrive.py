"""Google Drive upload — mirrors local court_documents hierarchy to Drive."""

from __future__ import annotations

import os
from pathlib import Path

# SCOPE: drive.file — this app can ONLY see and modify files that IT created.
# It cannot read, list, or modify any other files in the user's Google Drive.
# This is the most restrictive useful scope available.

# ---------------------------------------------------------------------------
# Optional dependency guard — graceful ImportError message
# ---------------------------------------------------------------------------
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    _GDRIVE_AVAILABLE = True
except ImportError:
    _GDRIVE_AVAILABLE = False

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Top-level folder in Google Drive — always this name, never prompted.
DRIVE_ROOT_FOLDER = "Legal-AI Court Downloads"


# ---------------------------------------------------------------------------
# Manifest helper — mark a file as uploaded to Drive
# ---------------------------------------------------------------------------

def _mark_uploaded_in_manifest(file_path: Path) -> None:
    """Set 'עלה לDrive' = 'כן' for *file_path* in the parent directory's summary CSV.

    Silently does nothing if no summary CSV is found or the column is absent.
    """
    import csv as _csv
    from datetime import datetime as _dt

    parent = file_path.parent
    # Find summary CSV in the same directory
    summary_csv: Path | None = None
    for candidate in parent.iterdir():
        if candidate.is_file() and candidate.suffix == ".csv" and candidate.stem.startswith("summary"):
            summary_csv = candidate
            break
    if summary_csv is None:
        return

    try:
        rows: list[dict] = []
        fieldnames: list[str] = []
        with summary_csv.open(encoding="utf-8-sig", newline="") as f:
            reader = _csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)

        if not fieldnames or "עלה לDrive" not in fieldnames:
            return

        filename = file_path.name
        now_str = _dt.now().strftime("%Y-%m-%d %H:%M")
        changed = False
        for row in rows:
            disk_name = row.get("שם קובץ פיזי בדיסק", "")
            if disk_name == filename and row.get("עלה לDrive", "") != "כן":
                row["עלה לDrive"] = now_str
                changed = True

        if not changed:
            return

        with summary_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except Exception:
        pass

_MIME_MAP = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
}


def _require_gdrive() -> None:
    if not _GDRIVE_AVAILABLE:
        raise ImportError(
            "[GDrive] Required packages not installed. Run:\n"
            "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )


def print_drive_setup_instructions() -> None:
    """Print one-time setup instructions when credentials.json is missing."""
    print("""
[GDrive] Setup required — credentials.json not found.

To connect Google Drive (one-time setup):
  1. Go to: https://console.cloud.google.com
  2. Create project → Enable "Google Drive API"
  3. APIs & Services → Credentials → Create → OAuth 2.0 Client ID → Desktop App
  4. Download JSON → rename to credentials.json → place in project root
  5. Run again — browser will open for one-time authorization

After that, uploads happen automatically with no further setup.
""")


# ---------------------------------------------------------------------------
# GDriveUploader
# ---------------------------------------------------------------------------

class GDriveUploader:
    """Uploads local files to Google Drive, mirroring the folder hierarchy."""

    def __init__(self, credentials_path: Path, token_path: Path, logger=None) -> None:
        _require_gdrive()
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.logger = logger
        self._service = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        """OAuth2 flow. Returns True on success."""
        _require_gdrive()

        if not self.credentials_path.exists():
            print_drive_setup_instructions()
            return False

        creds = None

        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            except Exception as exc:
                if self.logger:
                    self.logger.error(f"[GDrive] Failed to load token: {exc}")
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    if self.logger:
                        self.logger.error(f"[GDrive] Token refresh failed: {exc}")
                    creds = None

            if not creds:
                try:
                    import io
                    import contextlib
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(self.credentials_path), SCOPES
                    )
                    print("\n[GDrive] Browser opening for Google authorization — approve in browser to continue uploads.")
                    _captured = io.StringIO()
                    with contextlib.redirect_stdout(_captured):
                        creds = flow.run_local_server(port=0, open_browser=True)
                except Exception as exc:
                    if self.logger:
                        self.logger.error(f"[GDrive] OAuth flow error: {exc}")
                    print(f"[GDrive] Authentication failed: {exc}")
                    return False

            # Persist the token for next run
            try:
                self.token_path.parent.mkdir(parents=True, exist_ok=True)
                self.token_path.write_text(creds.to_json())
            except Exception as exc:
                if self.logger:
                    self.logger.error(f"[GDrive] Could not save token: {exc}")

        try:
            self._service = build("drive", "v3", credentials=creds)
        except Exception as exc:
            if self.logger:
                self.logger.error(f"[GDrive] Could not build Drive service: {exc}")
            print(f"[GDrive] Failed to initialise Drive service: {exc}")
            return False

        if self.logger:
            self.logger.info("[GDrive] Authenticated successfully.")
        return True

    # ------------------------------------------------------------------
    # Sharing
    # ------------------------------------------------------------------

    def share_readonly(self, file_id: str, email: str) -> bool:
        """EN: share a Drive file/folder with an email as VIEWER only — the
            recipient can see and download but never edit or comment. Silent
            (no notification email). Idempotent — safe to call repeatedly.
        HE: שיתוף צפייה-בלבד לפי מייל — המקבל רואה ומוריד, לא עורך ולא
            מגיב. בלי מייל התראה. בטוח לקריאה חוזרת."""
        email = (email or "").strip()
        if not email or "@" not in email:
            return False
        try:
            perms = self.service.permissions().list(
                fileId=file_id, fields="permissions(emailAddress,role)").execute()
            for p in perms.get("permissions", []):
                if (p.get("emailAddress") or "").lower() == email.lower():
                    return True          # already shared / כבר משותף
            self.service.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "user", "emailAddress": email},
                sendNotificationEmail=False,
            ).execute()
            if self.logger:
                self.logger.info(f"[GDrive] Shared read-only with {email}")
            print(f"[GDrive] שיתוף צפייה-בלבד עם {email} ✓")
            return True
        except Exception as exc:
            print(f"[GDrive] share failed for {email}: {exc}")
            return False

    # ------------------------------------------------------------------
    # Folder helpers
    # ------------------------------------------------------------------

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Find or create a Drive folder. Returns its folder ID."""
        q_parts = [
            f"name='{name}'",
            "mimeType='application/vnd.google-apps.folder'",
            "trashed=false",
        ]
        if parent_id:
            q_parts.append(f"'{parent_id}' in parents")
        query = " and ".join(q_parts)

        try:
            results = (
                self._service.files()
                .list(q=query, fields="files(id, name)", spaces="drive")
                .execute()
            )
            files = results.get("files", [])
            if files:
                return files[0]["id"]
        except HttpError as exc:
            if self.logger:
                self.logger.error(f"[GDrive] Error searching for folder '{name}': {exc}")
            raise

        # Create the folder
        metadata: dict = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]

        try:
            folder = (
                self._service.files()
                .create(body=metadata, fields="id")
                .execute()
            )
            folder_id: str = folder["id"]
            if self.logger:
                self.logger.info(f"[GDrive] Created folder '{name}' (id={folder_id}).")
            return folder_id
        except HttpError as exc:
            if self.logger:
                self.logger.error(f"[GDrive] Error creating folder '{name}': {exc}")
            raise

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    def upload_file(self, local_path: Path, parent_folder_id: str) -> str:
        """Upload a file to Drive. Skips if same name already exists. Returns file ID."""
        name = local_path.name

        # Deduplication check
        query = (
            f"name='{name}' and '{parent_folder_id}' in parents and trashed=false"
        )
        try:
            results = (
                self._service.files()
                .list(q=query, fields="files(id, name)", spaces="drive")
                .execute()
            )
            existing = results.get("files", [])
            if existing:
                if self.logger:
                    self.logger.info(f"[GDrive] Skipping (already exists): {name}")
                return existing[0]["id"]
        except HttpError as exc:
            if self.logger:
                self.logger.error(f"[GDrive] Error checking existence of '{name}': {exc}")
            raise

        suffix = local_path.suffix.lower()
        mime_type = _MIME_MAP.get(suffix, "application/octet-stream")

        print(f"[GDrive] Uploading: {name} ...")
        if self.logger:
            self.logger.info(f"[GDrive] Uploading: {name}")

        metadata = {"name": name, "parents": [parent_folder_id]}
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

        try:
            uploaded = (
                self._service.files()
                .create(body=metadata, media_body=media, fields="id")
                .execute()
            )
            file_id: str = uploaded["id"]
            if self.logger:
                self.logger.info(f"[GDrive] Uploaded '{name}' (id={file_id}).")
            return file_id
        except HttpError as exc:
            if self.logger:
                self.logger.error(f"[GDrive] Upload failed for '{name}': {exc}")
            raise

    # ------------------------------------------------------------------
    # Directory mirroring
    # ------------------------------------------------------------------

    def mirror_directory(self, local_dir: Path) -> dict:
        """Mirror *local_dir* into Drive under Legal-Ai/<local_dir.name>/...

        Drive hierarchy created:
            My Drive/
              Legal-Ai/                  ← DRIVE_ROOT_FOLDER, always
                <local_dir.name>/        ← subfolder named after local dir
                  ... (recursive copy)

        Returns ``{"uploaded": int, "skipped": int, "failed": int}``.
        """
        stats: dict[str, int] = {"uploaded": 0, "skipped": 0, "failed": 0}

        # 1. Find or create the top-level "Legal-Ai" folder in Drive root
        legal_ai_id = self.get_or_create_folder(DRIVE_ROOT_FOLDER, parent_id=None)

        # 2. Create/find a subfolder named after the local directory
        subdir_id = self.get_or_create_folder(local_dir.name, parent_id=legal_ai_id)

        # 3. Recursively mirror contents into that subfolder
        self._mirror_recursive(local_dir, subdir_id, stats)
        return stats

    def _mirror_recursive(
        self, local_dir: Path, drive_folder_id: str, stats: dict
    ) -> None:
        for item in sorted(local_dir.iterdir()):
            if item.is_dir():
                try:
                    child_id = self.get_or_create_folder(item.name, drive_folder_id)
                    self._mirror_recursive(item, child_id, stats)
                except Exception as exc:
                    if self.logger:
                        self.logger.error(f"[GDrive] Failed to create folder '{item.name}': {exc}")
                    stats["failed"] += 1
            elif item.is_file():
                try:
                    # Detect skip before upload to track correctly
                    name = item.name
                    query = (
                        f"name='{name}' and '{drive_folder_id}' in parents and trashed=false"
                    )
                    results = (
                        self._service.files()
                        .list(q=query, fields="files(id)", spaces="drive")
                        .execute()
                    )
                    if results.get("files"):
                        if self.logger:
                            self.logger.info(f"[GDrive] Skipping (already exists): {name}")
                        stats["skipped"] += 1
                        # Still mark as uploaded in manifest (may have been missed before)
                        _mark_uploaded_in_manifest(item)
                    else:
                        self.upload_file(item, drive_folder_id)
                        stats["uploaded"] += 1
                        _mark_uploaded_in_manifest(item)
                except Exception as exc:
                    if self.logger:
                        self.logger.error(f"[GDrive] Failed for '{item.name}': {exc}")
                    print(f"[GDrive] ERROR uploading '{item.name}': {exc}")
                    stats["failed"] += 1


# ---------------------------------------------------------------------------
# Module-level entry point
# ---------------------------------------------------------------------------

def collect_unuploaded_files(root_dir: Path) -> list[Path]:
    """Scan all summary CSVs under *root_dir* and return files where 'עלה לDrive' is empty.

    Only considers rows with סטטוס הורדה == 'Success' and a non-empty שם קובץ פיזי בדיסק.
    Returns absolute Path objects for files that exist on disk and haven't been uploaded.
    """
    import csv as _csv

    result: list[Path] = []
    for csv_file in root_dir.rglob("*.csv"):
        if not csv_file.stem.startswith("summary"):
            continue
        case_dir = csv_file.parent
        try:
            with csv_file.open(encoding="utf-8-sig", newline="") as f:
                reader = _csv.DictReader(f)
                if not reader.fieldnames or "עלה לDrive" not in reader.fieldnames:
                    continue
                for row in reader:
                    if row.get("סטטוס הורדה", "") != "Success":
                        continue
                    fname = row.get("שם קובץ פיזי בדיסק", "").strip()
                    if not fname:
                        continue
                    if row.get("עלה לDrive", "").strip():
                        continue  # already uploaded
                    full_path = case_dir / fname
                    if full_path.exists():
                        result.append(full_path)
        except Exception:
            continue
    return result


def run_smart_gdrive_upload(
    root_dir: Path,
    credentials_path: Path,
    token_path: Path,
    logger=None,
) -> dict:
    """Upload only files not yet marked as 'עלה לDrive' in their manifest CSV.

    Uses collect_unuploaded_files() to find candidates, uploads each one,
    marks the manifest, and returns stats.
    """
    import csv as _csv

    def _log(msg: str) -> None:
        print(f"[GDrive] {msg}")
        if logger:
            logger.info(f"[GDrive] {msg}")

    stats: dict[str, int] = {"uploaded": 0, "skipped": 0, "failed": 0}

    if not _GDRIVE_AVAILABLE:
        print("[GDrive] Required packages not installed — skipping Drive upload.")
        return stats

    if not Path(credentials_path).exists():
        print_drive_setup_instructions()
        return stats

    pending = collect_unuploaded_files(root_dir)
    if not pending:
        _log("כל הקבצים כבר הועלו לDrive — אין מה לעלות.")
        return stats

    _log(f"נמצאו {len(pending)} קבצים להעלאה לDrive...")

    uploader = GDriveUploader(credentials_path, token_path, logger=logger)
    if not uploader.authenticate():
        print("[GDrive] Authentication failed. Aborting upload.")
        return stats

    # Get / create Legal-Ai root
    try:
        legal_ai_id = uploader.get_or_create_folder(DRIVE_ROOT_FOLDER, parent_id=None)
    except Exception as exc:
        print(f"[GDrive] Cannot access/create {DRIVE_ROOT_FOLDER} folder: {exc}")
        return stats

    # 4.3: auto-share the whole drive folder READ-ONLY with the registered
    # user email — access without ability to change what was uploaded.
    # שיתוף אוטומטי צפייה-בלבד עם המייל הרשום — גישה בלי יכולת שינוי.
    try:
        share_email = ""
        try:
            from core.download import SESSION_SETTINGS as _ss
            share_email = _ss.get("share_email", "")
        except Exception:
            pass
        if not share_email:
            import json as _json
            _defaults = Path(__file__).resolve().parent.parent / "session_defaults.json"
            if _defaults.exists():
                share_email = _json.loads(_defaults.read_text(encoding="utf-8")).get("share_email", "")
        if share_email:
            # comma / semicolon / whitespace separated — share with each
            # מספר מיילים מופרדים בפסיק/נקודה-פסיק/רווח — שיתוף עם כל אחד
            import re as _re
            for _em in _re.split(r"[,;\s]+", share_email):
                if _em.strip():
                    uploader.share_readonly(legal_ai_id, _em.strip())
    except Exception:
        pass

    # Cache of drive folder_ids: local_dir → drive_folder_id
    _folder_cache: dict[str, str] = {}

    def _get_drive_folder(local_dir: Path) -> str:
        """Ensure the mirrored folder path exists in Drive and return its ID."""
        key = str(local_dir)
        if key in _folder_cache:
            return _folder_cache[key]
        # Build relative path from root_dir to local_dir
        try:
            parts = local_dir.relative_to(root_dir).parts
        except ValueError:
            parts = (local_dir.name,)
        parent_id = legal_ai_id
        for part in parts:
            sub_key = str(Path(root_dir, *parts[:parts.index(part) + 1]))
            if sub_key in _folder_cache:
                parent_id = _folder_cache[sub_key]
            else:
                parent_id = uploader.get_or_create_folder(part, parent_id=parent_id)
                _folder_cache[sub_key] = parent_id
        _folder_cache[key] = parent_id
        return parent_id

    # Also need root_dir itself mirrored
    root_folder_id = uploader.get_or_create_folder(root_dir.name, parent_id=legal_ai_id)
    _folder_cache[str(root_dir)] = root_folder_id

    total_pending = len(pending)
    for idx, file_path in enumerate(pending, 1):
        try:
            _log(f"מעלה {idx}/{total_pending}: {file_path.name}...")
            drive_folder_id = _get_drive_folder(file_path.parent)
            uploader.upload_file(file_path, drive_folder_id)
            _mark_uploaded_in_manifest(file_path)
            stats["uploaded"] += 1
        except Exception as exc:
            _log(f"שגיאה בהעלאת '{file_path.name}': {exc}")
            stats["failed"] += 1

    _log(
        f"העלאה הושלמה — הועלו: {stats['uploaded']}, "
        f"נדלגו: {stats['skipped']}, נכשלו: {stats['failed']}"
    )
    return stats


def run_gdrive_upload(
    local_dir: Path,
    credentials_path: Path,
    token_path: Path,
    logger=None,
) -> None:
    """Mirror *local_dir* to Legal-Ai/<local_dir.name>/ in Google Drive.

    No prompts — credentials.json must exist or setup instructions are printed.
    Drive hierarchy:
        My Drive/Legal-Ai/<local_dir.name>/...
    """
    _require_gdrive()

    local_dir = Path(local_dir)

    # Check credentials exist before doing anything
    if not Path(credentials_path).exists():
        print_drive_setup_instructions()
        return

    uploader = GDriveUploader(credentials_path, token_path, logger=logger)

    print(f"[GDrive] Authenticating ...")
    if not uploader.authenticate():
        print("[GDrive] Authentication failed. Aborting upload.")
        return

    print(f"[GDrive] Uploading to {DRIVE_ROOT_FOLDER}/{local_dir.name}/ ...")
    if logger:
        logger.info(f"[GDrive] Mirroring '{local_dir}' → {DRIVE_ROOT_FOLDER}/{local_dir.name}/")

    stats = uploader.mirror_directory(local_dir=local_dir)

    print(
        f"[GDrive] Upload complete — "
        f"uploaded: {stats['uploaded']}, "
        f"skipped: {stats['skipped']}, "
        f"failed: {stats['failed']}"
    )
    if logger:
        logger.info(
            f"[GDrive] Mirror complete: uploaded={stats['uploaded']}, "
            f"skipped={stats['skipped']}, failed={stats['failed']}"
        )
