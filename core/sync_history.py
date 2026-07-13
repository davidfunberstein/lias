"""Case-level sync history — sync_history.csv written to every case directory.

Works for both BDR and NET portals.

Columns
-------
תאריך ריצה       — YYYY-MM-DD HH:MM:SS
פורטל            — BDR / NET
מסמכים בפורטל    — total rows/items seen this run
הורדו חדשים      — newly downloaded
הורד מחדש        — re-downloaded (was missing)
נכשלו            — failed downloads
מסמך ראשון       — earliest document date seen
מסמך אחרון       — latest document date seen
חתימת פורטל      — first 16 chars of SHA-256 over sorted portal entries
חתימה קודמת      — hash from the previous run (empty on first run)
שינוי חתימה      — "כן" / "לא" / "ראשון"
הערה             — freeform warning (e.g. possible removal detected)

Hash construction
-----------------
For each portal document we form the string:
    NET:  "{doc_id}|{DocumentType}|{PresentationDate}|{CasePartyDisplayName}"
    BDR:  "{doc_type}|{doc_date}"

The list is sorted, joined with newline, hashed with SHA-256.
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

HISTORY_COLS = [
    "תאריך ריצה",
    "פורטל",
    "מסמכים בפורטל",
    "הורדו חדשים",
    "הורד מחדש",
    "נכשלו",
    "הועלו לDrive",
    "מסמך ראשון",
    "מסמך אחרון",
    "חתימת פורטל",
    "חתימה קודמת",
    "שינוי חתימה",
    "הערה",
]


# ---------------------------------------------------------------------------
# Hash builders
# ---------------------------------------------------------------------------

def compute_net_hash(metadata_lookup: dict) -> str:
    """
    Fingerprint a NET case from its metadata store.
    Each entry: "{doc_id}|{DocumentType}|{PresentationDate}|{CasePartyDisplayName}"
    Returns the first 16 hex chars of SHA-256.
    """
    entries = sorted(
        f"{doc_id}"
        f"|{meta.get('DocumentType', '')}"
        f"|{meta.get('PresentationDate', '')}"
        f"|{meta.get('CasePartyDisplayName', '')}"
        for doc_id, meta in metadata_lookup.items()
    )
    return _sha16(entries)


def compute_bdr_hash(snapshot_lines: list[str]) -> str:
    """
    Fingerprint a BDR case from BdrNavigator snapshot_lines.
    Each entry: "{doc_type}|{doc_date}"
    Returns the first 16 hex chars of SHA-256.
    """
    import re
    entries = []
    for line in snapshot_lines:
        m = re.search(r"Date=(.+?) \| Type=(.+?) \| Submitter", line)
        if m:
            entries.append(f"{m.group(2).strip()}|{m.group(1).strip()}")
    entries.sort()
    return _sha16(entries)


def _sha16(entries: list[str]) -> str:
    content = "\n".join(entries)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _earliest_latest(dates: list[str]) -> tuple[str, str]:
    """Given a list of 'DD/MM/YYYY' strings, return (earliest, latest)."""
    def _key(d: str) -> str:
        p = d.split("/")
        return f"{p[2]}{p[1]}{p[0]}" if len(p) == 3 else d
    if not dates:
        return "", ""
    s = sorted(dates, key=_key)
    return s[0], s[-1]


def dates_from_net_metadata(metadata_lookup: dict) -> tuple[str, str]:
    """Extract and sort all PresentationDate values from NET metadata."""
    import re
    raw_dates = []
    for meta in metadata_lookup.values():
        raw = (meta.get("PresentationDate") or "").strip()
        m = re.match(r"(\d{2}/\d{2}/\d{4})", raw)
        if m:
            raw_dates.append(m.group(1))
    return _earliest_latest(raw_dates)


def dates_from_bdr_snapshot(snapshot_lines: list[str]) -> tuple[str, str]:
    """Extract and sort all Date= values from BDR snapshot_lines."""
    import re
    raw_dates = []
    for line in snapshot_lines:
        m = re.search(r"Date=(\d{2}/\d{2}/\d{4})", line)
        if m:
            raw_dates.append(m.group(1))
    return _earliest_latest(raw_dates)


# ---------------------------------------------------------------------------
# History manager
# ---------------------------------------------------------------------------

class SyncHistory:
    """Append-only history of every sync run for one case directory."""

    def __init__(
        self,
        case_dir: Path,
        logger: "Logger | None" = None,
        label: str = "",
    ) -> None:
        if label:
            history_filename = f"sync_history — {label[:50]}.csv"
        else:
            history_filename = "sync_history.csv"
        self.path = case_dir / history_filename
        self.logger = logger
        self._rows: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                self._rows = [dict(r) for r in reader]
        except Exception as e:
            if self.logger:
                self.logger.warn(f"[SyncHistory] Could not load existing history: {e}")

    def last_hash(self) -> str:
        """Return the portal hash from the most recent completed run."""
        for row in reversed(self._rows):
            h = row.get("חתימת פורטל", "")
            if h:
                return h
        return ""

    def append(
        self,
        portal: str,
        total: int,
        new_downloads: int,
        re_downloads: int,
        failed: int,
        first_date: str,
        last_date: str,
        portal_hash: str,
        note: str = "",
        drive_uploads: int = 0,
    ) -> str:
        """
        Append one row.  Returns a change indicator:
          "ראשון"  — no previous hash
          "כן"     — hash changed
          "לא"     — hash unchanged
        """
        prev_hash = self.last_hash()

        if not prev_hash:
            changed = "ראשון"
        elif prev_hash != portal_hash:
            changed = "כן"
        else:
            changed = "לא"

        row = {
            "תאריך ריצה": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "פורטל": portal,
            "מסמכים בפורטל": str(total),
            "הורדו חדשים": str(new_downloads),
            "הורד מחדש": str(re_downloads),
            "נכשלו": str(failed),
            "הועלו לDrive": str(drive_uploads),
            "מסמך ראשון": first_date,
            "מסמך אחרון": last_date,
            "חתימת פורטל": portal_hash,
            "חתימה קודמת": prev_hash,
            "שינוי חתימה": changed,
            "הערה": note,
        }
        self._rows.append(row)
        self._save()
        return changed

    def _save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=HISTORY_COLS, extrasaction="ignore"
                )
                writer.writeheader()
                writer.writerows(self._rows)
        except Exception as e:
            if self.logger:
                self.logger.warn(f"[SyncHistory] Save failed: {e}")
