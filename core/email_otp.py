"""Email OTP reader — retrieves one-time login codes sent to the user's email.

Supports two backends:
  - gmail  : Gmail API (OAuth2, easiest for Gmail users)
  - imap   : Standard IMAP (works with Outlook, Walla, etc.)
"""

from __future__ import annotations

import imaplib
import email as email_lib
import json
import re
import time
from datetime import datetime
from email.header import decode_header


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

DEFAULT_OTP_REGEX = r"\b(\d{4,8})\b"

# Gmail API scopes needed (read-only is sufficient)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class EmailOTPReader:
    def __init__(self, config: dict, logger: "Logger | None" = None):
        """
        config keys (loaded from email_config.json):
          backend       : "gmail" or "imap"

          # Gmail:
          gmail_credentials : path to credentials.json
          gmail_token       : path to token.json

          # IMAP:
          imap_host     : e.g. "imap.gmail.com" or "imap.mail.yahoo.com"
          imap_port     : 993
          imap_user     : email address
          imap_password : app password (NOT regular password — uses app-specific password)
          imap_folder   : "INBOX"

          # OTP detection:
          sender_filter : e.g. "no-reply@court.gov.il" (optional, helps narrow search)
          otp_regex     : e.g. r"\\b(\\d{6})\\b"  (default: any 4-8 digit sequence)
          subject_filter: e.g. "קוד אימות" (optional)
        """
        self.config = config
        self.logger = logger
        self.backend = config.get("backend", "imap").lower()
        self.otp_regex = config.get("otp_regex", DEFAULT_OTP_REGEX)
        self.sender_filter = config.get("sender_filter", "").strip()
        self.subject_filter = config.get("subject_filter", "").strip()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture_baseline(self) -> int | set:
        """Snapshot the current inbox state. Call this BEFORE triggering the login
        so that any OTP email that arrives immediately after submit is not missed."""
        try:
            if self.backend == "gmail":
                return self.get_current_message_ids_gmail()
            else:
                return self.get_latest_uid_imap()
        except Exception as exc:
            if self.logger:
                self.logger.warn(f"[EmailOTP] Could not capture baseline: {exc}")
            return 0 if self.backend != "gmail" else set()

    def wait_for_otp(
        self,
        timeout_seconds: int = 60,
        poll_interval: int = 5,
        baseline: "int | set | None" = None,
    ) -> str | None:
        """Poll inbox for a new email containing an OTP code.

        Pass a pre-recorded ``baseline`` (from ``capture_baseline()``) to avoid
        missing an OTP that arrives immediately after the login form is submitted.
        If ``baseline`` is None the current inbox state is recorded now (old behaviour).
        """
        if self.logger:
            self.logger.info(f"[EmailOTP] Waiting for OTP (backend={self.backend}, timeout={timeout_seconds}s)...")
        print(f"{_ts()} [EmailOTP] Waiting for OTP email (up to {timeout_seconds}s)...")

        # Record baseline state (unless caller already captured it)
        if baseline is None:
            if self.backend == "gmail":
                seen_ids = self.get_current_message_ids_gmail()
                baseline: int | set = seen_ids
            else:
                baseline = self.get_latest_uid_imap()

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                if self.backend == "gmail":
                    code = self._fetch_otp_gmail(baseline)  # type: ignore[arg-type]
                else:
                    code = self._fetch_otp_imap(baseline)  # type: ignore[arg-type]

                if code:
                    if self.logger:
                        self.logger.ok(f"[EmailOTP] OTP retrieved: {code}")
                    print(f"{_ts()} [EmailOTP] OTP found: {code}")
                    return code
            except Exception as exc:
                msg = f"{_ts()} [EmailOTP] Poll error: {exc}"
                print(msg)
                if self.logger:
                    self.logger.warn(msg)

        print(f"{_ts()} [EmailOTP] Timed out waiting for OTP.")
        if self.logger:
            self.logger.warn("[EmailOTP] Timed out waiting for OTP.")
        return None

    # ------------------------------------------------------------------
    # Gmail backend
    # ------------------------------------------------------------------

    def _build_gmail_service(self):
        """Build and return an authenticated Gmail API service object."""
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise ImportError(
                "Gmail backend requires google-api-python-client and google-auth-oauthlib. "
                "Install with: pip install google-api-python-client google-auth-oauthlib"
            ) from exc

        _root = Path(__file__).resolve().parent.parent
        creds_path = Path(self.config.get("gmail_credentials", "credentials.json"))
        token_path = Path(self.config.get("gmail_token", "gmail_token.json"))
        # relative paths resolve against the project root, not the CWD
        if not creds_path.is_absolute():
            creds_path = _root / creds_path
        if not token_path.is_absolute():
            token_path = _root / token_path

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_current_message_ids_gmail(self) -> set:
        """Return the set of current message IDs in the inbox (baseline)."""
        service = self._build_gmail_service()
        query = self._build_gmail_query()
        response = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
        messages = response.get("messages", [])
        return {m["id"] for m in messages}

    def _fetch_otp_gmail(self, seen_ids: set) -> str | None:
        """Check for new Gmail messages not in seen_ids; return OTP if found."""
        service = self._build_gmail_service()
        query = self._build_gmail_query()
        response = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
        messages = response.get("messages", [])

        for msg_meta in messages:
            msg_id = msg_meta["id"]
            if msg_id in seen_ids:
                continue
            # New message — fetch full content
            msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            body = self._extract_gmail_body(msg)
            subject = self._extract_gmail_header(msg, "Subject")

            if self.subject_filter and self.subject_filter not in subject:
                continue

            code = self._extract_otp_from_text(body + " " + subject)
            if code:
                return code
        return None

    def _build_gmail_query(self) -> str:
        parts = ["in:inbox"]
        if self.sender_filter:
            parts.append(f"from:{self.sender_filter}")
        if self.subject_filter:
            parts.append(f"subject:{self.subject_filter}")
        return " ".join(parts)

    @staticmethod
    def _extract_gmail_header(msg: dict, header_name: str) -> str:
        headers = msg.get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == header_name.lower():
                return h.get("value", "")
        return ""

    @staticmethod
    def _extract_gmail_body(msg: dict) -> str:
        """Recursively extract text/plain or text/html body from a Gmail message."""
        import base64

        def _parts(payload: dict) -> str:
            mime = payload.get("mimeType", "")
            if mime in ("text/plain", "text/html"):
                data = payload.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
            for part in payload.get("parts", []):
                result = _parts(part)
                if result:
                    return result
            return ""

        return _parts(msg.get("payload", {}))

    # ------------------------------------------------------------------
    # IMAP backend
    # ------------------------------------------------------------------

    def _imap_connect(self) -> imaplib.IMAP4_SSL:
        host = self.config.get("imap_host", "imap.gmail.com")
        port = int(self.config.get("imap_port", 993))
        user = self.config.get("imap_user", "")
        password = self.config.get("imap_password", "")
        folder = self.config.get("imap_folder", "INBOX")

        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, password)
        conn.select(folder, readonly=True)
        return conn

    def get_latest_uid_imap(self) -> int:
        """Return the highest UID currently in the configured IMAP folder."""
        conn = self._imap_connect()
        try:
            _, data = conn.uid("search", None, "ALL")
            uids = data[0].split() if data and data[0] else []
            if not uids:
                return 0
            return int(uids[-1])
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _fetch_otp_imap(self, seen_before_uid: int) -> str | None:
        """Fetch messages with UID > seen_before_uid and extract OTP."""
        conn = self._imap_connect()
        try:
            search_criteria = f"UID {seen_before_uid + 1}:*"
            _, data = conn.uid("search", None, search_criteria)
            uids = data[0].split() if data and data[0] else []

            for uid_bytes in reversed(uids):
                uid = int(uid_bytes)
                if uid <= seen_before_uid:
                    continue

                _, msg_data = conn.uid("fetch", uid_bytes, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                # Filter by sender
                sender = msg.get("From", "")
                if self.sender_filter and self.sender_filter not in sender:
                    continue

                # Filter by subject
                subject = self._decode_imap_header(msg.get("Subject", ""))
                if self.subject_filter and self.subject_filter not in subject:
                    continue

                body = self._extract_imap_body(msg)
                code = self._extract_otp_from_text(body + " " + subject)
                if code:
                    return code
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return None

    @staticmethod
    def _decode_imap_header(value: str) -> str:
        parts = decode_header(value)
        decoded = ""
        for raw, charset in parts:
            if isinstance(raw, bytes):
                decoded += raw.decode(charset or "utf-8", errors="ignore")
            else:
                decoded += raw
        return decoded

    @staticmethod
    def _extract_imap_body(msg) -> str:
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct in ("text/plain", "text/html"):
                    try:
                        charset = part.get_content_charset() or "utf-8"
                        body += part.get_payload(decode=True).decode(charset, errors="ignore")
                    except Exception:
                        pass
        else:
            try:
                charset = msg.get_content_charset() or "utf-8"
                body = msg.get_payload(decode=True).decode(charset, errors="ignore")
            except Exception:
                pass
        return body

    # ------------------------------------------------------------------
    # OTP extraction
    # ------------------------------------------------------------------

    def _extract_otp_from_text(self, text: str) -> str | None:
        """Apply otp_regex to text and return first match group, or None."""
        if not text:
            return None
        match = re.search(self.otp_regex, text)
        if match:
            # Return first capturing group if present, else whole match
            return match.group(1) if match.lastindex and match.lastindex >= 1 else match.group(0)
        return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def setup_email_config(config_path: Path) -> dict:
    """Interactive setup wizard — asks user for backend choice and credentials.

    Saves configuration to config_path (email_config.json).
    Returns the resulting config dict.
    """
    print("\n" + "=" * 54)
    print("EMAIL OTP SETUP")
    print("=" * 54)
    print("Choose email backend:")
    print("  1. IMAP  (Outlook, Walla, or Gmail with app-password)")
    print("  2. Gmail API  (OAuth2 — recommended for Gmail users)")
    backend_choice = input("Choice (1/2) [Default: 1]: ").strip()
    backend = "gmail" if backend_choice == "2" else "imap"

    config: dict = {"backend": backend}

    if backend == "imap":
        config["imap_host"] = input("IMAP host [imap.gmail.com]: ").strip() or "imap.gmail.com"
        port_raw = input("IMAP port [993]: ").strip()
        config["imap_port"] = int(port_raw) if port_raw.isdigit() else 993
        config["imap_user"] = input("Email address: ").strip()
        config["imap_password"] = input("App-specific password (NOT your regular password): ").strip()
        config["imap_folder"] = input("Folder [INBOX]: ").strip() or "INBOX"
    else:
        config["gmail_credentials"] = input("Path to credentials.json [credentials.json]: ").strip() or "credentials.json"
        config["gmail_token"] = input("Path to token.json [gmail_token.json]: ").strip() or "gmail_token.json"

    config["sender_filter"] = input("Sender filter (e.g. no-reply@court.gov.il) [leave blank]: ").strip()
    config["subject_filter"] = input("Subject filter (e.g. קוד אימות) [leave blank]: ").strip()
    config["otp_regex"] = input(r"OTP regex [\b(\d{6})\b]: ").strip() or r"\b(\d{6})\b"

    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    print(f"\n[EmailOTP] Config saved to: {config_path}")
    return config


def load_email_config(config_path: Path) -> dict | None:
    """Load existing config from config_path. Returns None if not found.

    Security: the IMAP app-password lives in the OS keychain, not in the
    JSON file. If an old config still contains "imap_password" in plaintext,
    it is migrated to the keychain once and scrubbed from the file.
    """
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text())
    except Exception:
        return None

    try:
        import keyring
        service = "gov-il-connect-email"
        account = config.get("imap_user") or "default"
        plaintext = (config.get("imap_password") or "").strip()
        if plaintext:
            # Migrate: keychain becomes the source of truth, file is scrubbed.
            keyring.set_password(service, account, plaintext)
            scrubbed = dict(config)
            scrubbed["imap_password"] = ""
            config_path.write_text(
                json.dumps(scrubbed, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[EmailOTP] Moved IMAP app-password from email_config.json to the OS keychain.")
        else:
            stored = keyring.get_password(service, account)
            if stored:
                config["imap_password"] = stored
    except Exception:
        # keyring unavailable — fall back to whatever the file holds.
        pass
    return config
