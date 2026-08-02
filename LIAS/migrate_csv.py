"""One-time CSV → SQLite import / ייבוא חד-פעמי מ-CSV ל-SQLite.

EN: Walks court_documents/downloads/, finds every folder that has a
    "summary — *.csv" manifest, and imports: client (top folder), case,
    sub-case, every document row, and the sync_history rows. Safe to re-run —
    everything is upsert by natural keys. The CSVs stay untouched (dual-source
    period, per the plan: delete them only two releases later).
HE: עובר על court_documents/downloads/, מוצא כל תיקייה עם מניפסט
    "summary — *.csv", ומייבא: לקוח (התיקייה העליונה), תיק, תת-תיק,
    כל שורת מסמך, ואת שורות ה-sync_history. בטוח להריץ שוב — הכל upsert
    לפי מפתחות טבעיים. קבצי ה-CSV לא נגעים (תקופת מקור-כפול, לפי התוכנית:
    מוחקים אותם רק שתי גרסאות קדימה).

Run / הרצה (from gov-il-connect-v2 root / מתיקיית השורש):
    python3 -m LIAS.migrate_csv
Or directly / או ישירות:  python3 LIAS/migrate_csv.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

# direct-execution shim / תאימות הרצה ישירה (see run.py)
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import runpy
    runpy.run_module("LIAS.migrate_csv", run_name="__main__")
    sys.exit(0)

from . import config, db

# Status mapping: legacy CSV → state machine / מיפוי סטטוסים: CSV ישן → מכונת מצבים
_STATUS_MAP = {
    "Success": "COMPLETED",
    "Local Sync": "COMPLETED",
    "Missing": "MISSING",
}


def _map_status(raw: str) -> str:
    raw = (raw or "").strip()
    if raw in _STATUS_MAP:
        return _STATUS_MAP[raw]
    if raw.startswith("Failed"):
        return "ERROR"
    return "PENDING"


def _find_manifest_dirs(root: Path):
    """Yield every dir containing a summary CSV / כל תיקייה עם CSV מניפסט."""
    seen = set()
    for csv_path in root.rglob("summary*—*.csv"):
        if csv_path.parent not in seen:
            seen.add(csv_path.parent)
            yield csv_path.parent, csv_path
    for csv_path in root.rglob("summary.csv"):  # legacy name / שם ישן
        if csv_path.parent not in seen:
            seen.add(csv_path.parent)
            yield csv_path.parent, csv_path


def _detect_portal(case_dir: Path) -> str:
    """EN: read portal from sync_history if present; otherwise infer from the
        folder name — rabbinical (BDR) cases are numbered 'NNNNNNN-N topic',
        NET cases carry a letter prefix like 'תמש 330-04-22'. Default NET.
    HE: קריאת הפורטל מ-sync_history אם קיים; אחרת זיהוי לפי שם התיקייה —
        תיק רבני ממוספר 'NNNNNNN-N נושא', תיק נט עם קידומת אותיות."""
    for sh in case_dir.glob("sync_history*.csv"):
        try:
            with open(sh, encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            if rows and rows[0].get("פורטל"):
                return rows[0]["פורטל"].strip()
        except Exception:
            pass
    import re as _re
    if _re.match(r"^\d{6,7}-\d+(\D|$)", case_dir.name.strip()):
        return "BDR"
    return "NET"


def _import_manifest(case_dir: Path, csv_path: Path, downloads_root: Path) -> tuple[int, int]:
    """Import one folder / ייבוא תיקייה אחת. Returns (docs, runs)."""
    rel = case_dir.relative_to(downloads_root)
    parts = list(rel.parts)
    # EN: hierarchy = client / [case] / [sub_case]; flat folders are their own case.
    # HE: היררכיה = לקוח / [תיק] / [תת-תיק]; תיקייה שטוחה היא תיק בפני עצמו.
    client_name = parts[0]

    import json as _json, re as _re
    from core.download import _normalize_party

    _ln = ""
    try:
        _sd = config.PROJECT_ROOT / "session_defaults.json"
        if _sd.exists():
            _ln = _json.loads(_sd.read_text("utf-8")).get("lawyer_name", "")
    except Exception:
        pass
    _ln_words = set(_re.findall(r"[א-ת]{2,}", _ln)) if _ln else set()
    _ln_last = _ln.split()[-1] if _ln else ""

    def _is_me(name):
        nw = set(_re.findall(r"[א-ת]{2,}", name))
        return bool(nw) and nw.issubset(_ln_words)

    def _pick_client(normed_names):
        if _ln_words and len(normed_names) >= 2:
            others = [n for n in normed_names if not _is_me(n)]
            if others:
                return " נ' ".join(sorted(set(others)))
        if len(normed_names) >= 2:
            return " נ' ".join(sorted(set(normed_names)))
        if normed_names:
            return normed_names[0]
        return None

    try:
        _ci = case_dir / "case_info.json"
        if not _ci.exists():
            for _p in (case_dir.parent, case_dir.parent.parent):
                if (_p / "case_info.json").exists():
                    _ci = _p / "case_info.json"; break
        if _ci.exists():
            _info = _json.loads(_ci.read_text(encoding="utf-8"))
            _parties = _info.get("parties", [])
            if _parties:
                _normed = [_normalize_party(p.get("name", "")) for p in _parties if p.get("name")]
                result = _pick_client(_normed)
                if result:
                    client_name = result
    except Exception:
        pass

    if _is_me(client_name) or client_name == parts[0]:
        for part in parts:
            if " - " in part:
                folder_parties = [_normalize_party(p) for p in part.split(" - ") if p.strip()]
                result = _pick_client(folder_parties)
                if result and not _is_me(result):
                    client_name = result
                    break
    # ECA folders are client/"הוצאה לפועל"/case-number/... — drop the portal
    # segment so the case number is the REAL number, not the literal
    # "הוצאה לפועל" (which would collide across clients and merge them).
    if len(parts) > 1 and parts[1] in ("הוצאה לפועל", "הוצל\"פ"):
        del parts[1]
    case_number = parts[1] if len(parts) > 1 else parts[0]
    sub_number = parts[-1]

    if _ln_last and _ln_last in set(_re.findall(r"[א-ת]{2,}", client_name)):
        client_name = _ln

    portal = _detect_portal(case_dir)
    client_id = db.upsert_client(client_name)
    case_id = db.upsert_case(client_id, portal, case_number)
    sub_case_id = db.upsert_sub_case(case_id, sub_number)

    n_docs = 0
    try:
        f_check = open(csv_path, encoding="utf-8-sig")
        f_check.read(256)
        f_check.close()
    except (UnicodeDecodeError, Exception):
        print(f"  ⚠ skipping unreadable CSV (possibly XLSX): {csv_path.name}")
        return 0, 0
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            physical = (row.get("שם קובץ פיזי בדיסק") or row.get("שם קובץ מקורי (מהשרת)") or "").strip()
            if not physical:
                continue
            local = case_dir / physical
            # Meaningful display name: table label → original server name →
            # physical filename (stem). Never leave it blank in the DB.
            # שם תצוגה: תווית מהטבלה ← שם מקורי מהשרת ← שם הקובץ הפיזי.
            _orig = (row.get("שם קובץ מקורי (מהשרת)") or "").strip()
            if _orig.lower().startswith("unknown"):
                _orig = ""
            logical = ((row.get("שם מסמך (מהטבלה)") or "").strip()
                       or _orig
                       or physical.rsplit(".", 1)[0])
            db.upsert_document(
                sub_case_id,
                physical,
                logical_name=logical,
                doc_type=(row.get("סוג קובץ") or row.get("סיווג מסמך") or "").strip(),
                submitter_est=(row.get("מגיש") or "").strip(),
                submission_date=(row.get("תאריך מסמך") or "").strip(),
                submission_time=(row.get("שעת מסמך") or "").strip(),
                pages=int(float(row.get("מספר עמודים") or 0)),
                file_size_kb=int(float(row.get("גודל (KB)") or 0)),
                download_status="COMPLETED" if local.exists() else _map_status(row.get("סטטוס הורדה", "")),
                local_path=str(local.relative_to(config.COURT_DOCS_DIR)) if local.exists() else "",
                has_attachments=1 if (row.get("יש נספחים") or "").strip() == "+" else 0,
            )
            n_docs += 1

    # sync_history → sync_runs
    n_runs = 0
    conn = db.get_conn()
    for sh in case_dir.glob("sync_history*.csv"):
        with open(sh, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                started = (row.get("תאריך ריצה") or "").strip()
                if not started:
                    continue
                # idempotent: skip if identical run exists / דילוג אם ריצה זהה קיימת
                dup = conn.execute(
                    "SELECT 1 FROM sync_runs WHERE started_at=? AND sub_case_id=?",
                    (started, sub_case_id),
                ).fetchone()
                if dup:
                    continue
                conn.execute(
                    """INSERT INTO sync_runs(portal, sub_case_id, started_at,
                       total_in_portal, downloaded_new, re_downloaded, failed,
                       portal_hash, prev_hash, hash_changed, note)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        (row.get("פורטל") or portal).strip(), sub_case_id, started,
                        int(row.get("מסמכים בפורטל") or 0), int(row.get("הורדו חדשים") or 0),
                        int(row.get("הורד מחדש") or 0), int(row.get("נכשלו") or 0),
                        (row.get("חתימת פורטל") or "").strip(), (row.get("חתימה קודמת") or "").strip(),
                        (row.get("שינוי חתימה") or "").strip(), (row.get("הערה") or "").strip(),
                    ),
                )
                n_runs += 1
    conn.commit()
    return n_docs, n_runs


def migrate() -> dict:
    """Full import / ייבוא מלא. Clears existing data first to avoid duplicates."""
    db.init_db()
    conn = db.get_conn()
    for tbl in ("documents", "sync_runs", "snapshots", "sub_cases", "cases", "clients"):
        try:
            conn.execute(f"DELETE FROM {tbl}")
        except Exception:
            pass
    conn.commit()
    downloads_root = config.COURT_DOCS_DIR / "downloads"
    if not downloads_root.exists():
        print(f"!! downloads folder not found / תיקיית ההורדות לא נמצאה: {downloads_root}")
        return {"folders": 0, "documents": 0, "runs": 0}

    totals = {"folders": 0, "documents": 0, "runs": 0}
    for case_dir, csv_path in _find_manifest_dirs(downloads_root):
        docs, runs = _import_manifest(case_dir, csv_path, downloads_root)
        totals["folders"] += 1
        totals["documents"] += docs
        totals["runs"] += runs
        print(f"  ✓ {case_dir.name}: {docs} docs / מסמכים, {runs} runs / ריצות")

    print(f"\nDone / הסתיים: {totals['folders']} folders, "
          f"{totals['documents']} documents, {totals['runs']} sync runs")
    return totals


if __name__ == "__main__":
    sys.exit(0 if migrate()["folders"] >= 0 else 1)
