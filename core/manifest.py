"""Unified CSV manifest manager shared by both BDR and NET portals.

Columns that do not apply to a given portal are stored as empty strings.
Every write is atomic — the file is always in a valid state after each upsert.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

# Columns relevant to BDR (Rabbinical Courts) per-case manifest
BDR_COLUMNS = [
    "שם מסמך (מהטבלה)",
    "שם קובץ מקורי (מהשרת)",
    "תאריך מסמך",
    "סוג קובץ",
    "מגיש",
    "מזהה ייחודי",
    "שם קובץ פיזי בדיסק",
    "גודל (KB)",
    "מועד עדכון אחרון",
    "מועד הרצה",
    "סטטוס הורדה",
    "מספר עמודים",
    "עלה לDrive",
]

# Columns relevant to NET (Net HaMishpat) per-case manifest
NET_COLUMNS = [
    "שם מסמך (מהטבלה)",
    "שם קובץ מקורי (מהשרת)",
    "תאריך מסמך",
    "שעת מסמך",
    "סוג קובץ",
    "מגיש",
    "מזהה ייחודי",
    "שם קובץ פיזי בדיסק",
    "גודל (KB)",
    "מועד עדכון אחרון",
    "מועד הרצה",
    "סטטוס הורדה",
    "סיווג מסמך",
    "מספר עמודים",
    "יש נספחים",
    "עלה לDrive",
    "נתיב בדרייב",
    # Decision columns (filled only for documents that appear in החלטות בתיק)
    "שם החלטה",
    "תאריך החלטה",
    "שופט",
    "צופים",
    "מועד עדכון צופים",
]

# Keep COLUMNS as alias for NET_COLUMNS for backward compatibility
COLUMNS = NET_COLUMNS


def get_summary_csv_path(case_dir: Path) -> Path:
    """Return the meaningful summary CSV path for a case directory.

    Format: ``case_dir / 'summary — {case_dir.name[:60]}.csv'``
    Falls back to ``summary.csv`` if the name is blank.
    Also transparently migrates an old ``summary.csv`` to the new name.
    """
    label = case_dir.name[:60].replace("/", "-").replace("\\", "-").strip()
    if not label:
        return case_dir / "summary.csv"
    new_path = case_dir / f"summary — {label}.csv"
    # Migrate legacy summary.csv → new name (only if the new file doesn't exist yet)
    old_path = case_dir / "summary.csv"
    if old_path.exists() and not new_path.exists():
        try:
            old_path.rename(new_path)
        except Exception:
            pass  # Migration not critical — continue with new path
    return new_path


class ManifestManager:
    """Read/write a unified CSV manifest for one case directory."""

    def __init__(
        self,
        csv_path: Path,
        run_timestamp: str = "",
        logger: "Logger | None" = None,
        portal: str = "",
        case_number: str = "",
        case_period: str = "",
        representative: str = "",
        parties: str = "",
    ) -> None:
        self.csv_path = csv_path
        self.run_timestamp = run_timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logger = logger
        self.portal = portal
        self.case_number = case_number
        self.case_period = case_period
        self.representative = representative
        self.parties = parties
        self._records: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # Portal-specific columns
    # ------------------------------------------------------------------

    @property
    def columns(self) -> list:
        return BDR_COLUMNS if self.portal == "BDR" else NET_COLUMNS

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize(self, row: dict) -> dict:
        result = {col: "" for col in self.columns}
        result.update({k: str(v) for k, v in row.items() if k in self.columns})
        return result

    def _load(self) -> None:
        if not self.csv_path.exists():
            self._records = []
            return
        try:
            with open(self.csv_path, "r", encoding="utf-8-sig") as f:
                self._records = [self._normalize(r) for r in csv.DictReader(f)]
        except Exception as e:
            if self.logger:
                self.logger.warn(f"Could not read manifest {self.csv_path}: {e}")
            self._records = []

    def _save(self) -> None:
        try:
            with open(self.csv_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self._records)
        except Exception as e:
            if self.logger:
                self.logger.error(f"Could not save manifest: {e}")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_successful_ids(self) -> set[str]:
        return {r["מזהה ייחודי"] for r in self._records
                if r["מזהה ייחודי"] and r["סטטוס הורדה"] == "Success"}

    def get_missing_ids(self) -> set[str]:
        return {r["מזהה ייחודי"] for r in self._records
                if r["מזהה ייחודי"] and r["סטטוס הורדה"] == "Missing"}

    def get_failed_ids(self) -> set[str]:
        """UIDs whose previous download attempt failed — candidates for re-download."""
        return {r["מזהה ייחודי"] for r in self._records
                if r["מזהה ייחודי"] and "Failed" in r.get("סטטוס הורדה", "")}

    def get_all_ids(self) -> set[str]:
        return {r["מזהה ייחודי"] for r in self._records if r["מזהה ייחודי"]}

    @property
    def records(self) -> list[dict]:
        return list(self._records)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert(self, record: dict) -> None:
        """Insert or update a record, identified by מזהה ייחודי."""
        uid = record.get("מזהה ייחודי", "")
        normalized = self._normalize(record)
        normalized["מועד עדכון אחרון"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        normalized["מועד הרצה"] = self.run_timestamp

        # Fill portal metadata from instance defaults if not already set in the record
        if not normalized.get("פורטל") and self.portal:
            normalized["פורטל"] = self.portal
        if not normalized.get("מספר תיק - מספר") and self.case_number:
            normalized["מספר תיק - מספר"] = self.case_number
        if not normalized.get("מספר תיק - שנה-חודש") and self.case_period:
            normalized["מספר תיק - שנה-חודש"] = self.case_period
        if not normalized.get("מייצג") and self.representative:
            normalized["מייצג"] = self.representative

        for i, r in enumerate(self._records):
            if r["מזהה ייחודי"] == uid and uid:
                self._records[i] = normalized
                self._save()
                return

        self._records.append(normalized)
        self._save()

    def mark_missing(self, uid: str) -> None:
        for r in self._records:
            if r["מזהה ייחודי"] == uid:
                r["סטטוס הורדה"] = "Missing"
                r["מועד עדכון אחרון"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                break
        self._save()

    def mark_uploaded_to_drive(self, uid: str) -> None:
        """Mark a specific document as uploaded to Drive, recording the upload timestamp."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in self._records:
            if r.get("מזהה ייחודי") == uid:
                r["עלה לDrive"] = now
                r["מועד עדכון אחרון"] = now
                break
        self._save()

    def mark_all_uploaded_to_drive(self) -> None:
        """Mark all Success records as uploaded to Drive, recording the upload timestamp."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in self._records:
            if r.get("סטטוס הורדה") == "Success":
                r["עלה לDrive"] = now
        self._save()

    def get_not_uploaded_files(self) -> list[str]:
        """Return list of physical filenames that succeeded but were not uploaded to Drive."""
        return [
            r["שם קובץ פיזי בדיסק"]
            for r in self._records
            if r.get("סטטוס הורדה") == "Success"
            and not r.get("עלה לDrive")
            and r.get("שם קובץ פיזי בדיסק")
        ]

    # ------------------------------------------------------------------
    # Disk synchronisation (runs at the start of every case session)
    # ------------------------------------------------------------------

    def sync_with_disk(self, case_dir: Path) -> None:
        """
        1. Files on disk but not in manifest → add with status 'Local Sync'.
        2. Records in manifest whose file is missing from disk → mark 'Missing'.
        """
        logger = self.logger
        disk_files = {
            f.name for f in case_dir.iterdir()
            if f.is_file()
            and f.suffix.lower() in (".pdf", ".docx", ".xlsx")
            and f.name != self.csv_path.name
        }

        manifest_filenames = {r["שם קובץ פיזי בדיסק"] for r in self._records
                              if r["שם קובץ פיזי בדיסק"]}

        # Build a set of base names already covered (strip _N suffixes)
        import re as _re
        def _base(name: str) -> str:
            """Strip trailing _N suffix before extension: '2024_12_04 - החלטה_2.pdf' → '2024_12_04 - החלטה'"""
            stem = name.rsplit(".", 1)[0]
            return _re.sub(r"_\d+$", "", stem)

        covered_bases = {_base(n) for n in manifest_filenames}

        for fname in disk_files - manifest_filenames:
            # Skip if a file with the same base name (possibly with _N suffix) is already tracked
            if _base(fname) in covered_bases:
                if logger:
                    logger.info(f"Disk sync: '{fname}' base already covered — skipping Local Sync entry.")
                continue
            fpath = case_dir / fname
            label = fpath.stem.replace("_", " ")
            record = {
                "שם מסמך (מהטבלה)": label,
                "שם קובץ מקורי (מהשרת)": "Unknown (Local Sync)",
                "תאריך מסמך": datetime.fromtimestamp(fpath.stat().st_mtime).strftime("%d/%m/%Y"),
                "שם קובץ פיזי בדיסק": fname,
                "גודל (KB)": str(round(fpath.stat().st_size / 1024, 2)),
                "מזהה ייחודי": f"local_{fname}",
                "סטטוס הורדה": "Local Sync",
            }
            self._records.append(self._normalize(record))
            covered_bases.add(_base(fname))
            if logger:
                logger.info(f"Disk sync: added untracked file '{fname}' to manifest.")

        for r in self._records:
            fname = r["שם קובץ פיזי בדיסק"]
            if fname and fname not in disk_files and r["סטטוס הורדה"] == "Success":
                r["סטטוס הורדה"] = "Missing"
                r["מועד עדכון אחרון"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if logger:
                    logger.warn(f"File '{fname}' recorded in manifest but absent from disk — marked Missing.")

        # Remove Local Sync entries whose base name is now covered by a Success record
        import re as _re2
        def _base2(n: str) -> str:
            return _re2.sub(r"_\d+$", "", n.rsplit(".", 1)[0])

        success_bases = {_base2(r["שם קובץ פיזי בדיסק"])
                         for r in self._records
                         if r.get("סטטוס הורדה") == "Success" and r.get("שם קובץ פיזי בדיסק")}
        before = len(self._records)
        self._records = [
            r for r in self._records
            if not (r.get("סטטוס הורדה") == "Local Sync"
                    and _base2(r.get("שם קובץ פיזי בדיסק", "")) in success_bases)
        ]
        removed = before - len(self._records)
        if removed and logger:
            logger.info(f"Disk sync: removed {removed} Local Sync duplicate(s) now covered by Success records.")

        self._save()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def update_decision_data(self, uid: str, data: dict) -> None:
        """Update decision columns in the manifest row whose מזהה ייחודי == uid.

        If no matching row exists (decision not yet downloaded), a placeholder row
        is appended with status 'לא הורד' so viewers data is not lost.
        """
        _DECISION_COLS = ("שם החלטה", "תאריך החלטה", "שופט", "צופים", "מועד עדכון צופים")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in self._records:
            if r.get("מזהה ייחודי") == uid:
                for col in _DECISION_COLS:
                    if col in data:
                        r[col] = data[col]
                r["מועד עדכון אחרון"] = now
                self._save()
                return
        # Not found — add a stub row so decision metadata isn't lost
        row = {col: "" for col in NET_COLUMNS}
        row.update({
            "מזהה ייחודי": uid,
            "שם מסמך (מהטבלה)": data.get("שם החלטה", ""),
            "תאריך מסמך": data.get("תאריך החלטה", ""),
            "מגיש": data.get("שופט", ""),
            "סטטוס הורדה": "לא הורד",
            "מועד עדכון אחרון": now,
            "מועד הרצה": self.run_timestamp,
        })
        for col in _DECISION_COLS:
            row[col] = data.get(col, "")
        self._records.append(row)
        self._save()

    def print_summary(self, logger: "Logger | None" = None) -> dict:
        total = len(self._records)
        success = sum(1 for r in self._records if r["סטטוס הורדה"] == "Success")
        missing = sum(1 for r in self._records if r["סטטוס הורדה"] == "Missing")
        failed = sum(1 for r in self._records if "Failed" in r["סטטוס הורדה"])
        local = sum(1 for r in self._records if r["סטטוס הורדה"] == "Local Sync")

        lines = [
            "=" * 60,
            "MANIFEST SUMMARY",
            "=" * 60,
            f"  Total entries:        {total}",
            f"  Successfully saved:   {success}",
            f"  Missing from disk:    {missing}",
            f"  Failed downloads:     {failed}",
            f"  Local (untracked):    {local}",
            "=" * 60,
        ]
        for line in lines:
            if logger:
                logger.info(line)
            else:
                print(line)

        return {"total": total, "success": success, "missing": missing,
                "failed": failed, "local": local}
