"""Session cache for Gov.il Playwright storage state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext

_LEGACY_PROFILE_PATH = Path.home() / ".net_ai_user_data"
_LEGACY_HINT_SHOWN_FILE = Path(__file__).resolve().parent.parent / ".legacy_hint_shown"


def check_legacy_profile(shared_profile_dir: Path) -> None:
    """One-time hint: if the shared profile has no session data but the legacy
    ~/.net_ai_user_data profile exists, suggest copying it."""
    if _LEGACY_HINT_SHOWN_FILE.exists():
        return
    if not _LEGACY_PROFILE_PATH.exists():
        return
    # The profile has real session data once Chromium creates Default/Cookies.
    if (shared_profile_dir / "Default" / "Cookies").exists():
        return
    _LEGACY_HINT_SHOWN_FILE.touch()
    print(
        f"\n[Hint] A legacy NET profile was found at: {_LEGACY_PROFILE_PATH}\n"
        f"To skip re-login, copy it into the shared profile:\n"
        f'  cp -r "{_LEGACY_PROFILE_PATH}/" "{shared_profile_dir}/"\n'
        "Then restart. (This message will not repeat.)\n"
    )

PACKAGE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = PACKAGE_DIR / ".session_cache.json"
DEFAULT_SESSION_TTL_DAYS = 7
# Shared across BDR and NET — Gov.il SSO is one login for all court portals.
SHARED_CACHE_KEY = "govil"


@dataclass
class CachedSession:
    connection_type: str
    storage_state: dict[str, Any]
    expires_at: str
    saved_at: str

    def is_expired(self) -> bool:
        expiry = datetime.fromisoformat(self.expires_at)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expiry


class SessionCache:
    """Read/write Playwright storage state shared across BDR and NET."""

    def __init__(self, cache_path: Path | None = None, ttl_days: int = DEFAULT_SESSION_TTL_DAYS) -> None:
        self.cache_path = cache_path or DEFAULT_CACHE_PATH
        self.ttl_days = ttl_days

    def _read_data(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_data(self, data: dict[str, Any]) -> None:
        if data:
            self.cache_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            self.cache_path.unlink(missing_ok=True)

    def _load_key(self, data: dict[str, Any], key: str) -> CachedSession | None:
        entry = data.get(key)
        if not entry:
            return None
        session = CachedSession(
            connection_type=key,
            storage_state=entry["storage_state"],
            expires_at=entry["expires_at"],
            saved_at=entry.get("saved_at", ""),
        )
        if session.is_expired():
            return None
        return session

    def _purge_expired(self, data: dict[str, Any]) -> dict[str, Any]:
        """Self-cleaning: drop expired entries from disk so stale cache never
        requires manual deletion. / ניקוי עצמי — קאש פג-תוקף נמחק אוטומטית."""
        fresh: dict[str, Any] = {}
        for key, entry in data.items():
            try:
                expiry = datetime.fromisoformat(entry["expires_at"])
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < expiry:
                    fresh[key] = entry
            except (KeyError, TypeError, ValueError):
                continue  # malformed entry — drop it
        if fresh != data:
            self._write_data(fresh)
        return fresh

    def load(self, connection_type: str) -> CachedSession | None:
        """Load shared Gov.il session first, then portal-specific fallback."""
        data = self._purge_expired(self._read_data())
        for key in (SHARED_CACHE_KEY, connection_type, "bdr", "net"):
            session = self._load_key(data, key)
            if session:
                return session
        return None

    def save(self, connection_type: str, context: BrowserContext) -> CachedSession:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=self.ttl_days)
        session = CachedSession(
            connection_type=connection_type,
            storage_state=context.storage_state(),
            expires_at=expires.isoformat(),
            saved_at=now.isoformat(),
        )

        entry = {
            "storage_state": session.storage_state,
            "expires_at": session.expires_at,
            "saved_at": session.saved_at,
        }
        data = self._read_data()
        data[SHARED_CACHE_KEY] = entry
        data[connection_type] = entry
        self._write_data(data)
        return session

    def clear(self, connection_type: str | None = None) -> None:
        if not self.cache_path.exists():
            return

        if connection_type is None:
            self.cache_path.unlink(missing_ok=True)
            return

        data = self._read_data()
        data.pop(connection_type, None)
        data.pop(SHARED_CACHE_KEY, None)
        self._write_data(data)
