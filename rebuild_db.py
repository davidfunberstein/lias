#!/usr/bin/env python3
"""
Rebuild lias.db from the files on disk.
==========================================
Use this after receiving an existing state (a court_documents/ folder from
someone else) so the dashboard database matches the documents you have.

It re-imports every case manifest (summary CSVs) into a fresh SQLite database,
writes ECA manifests where missing, and folds fragmented case folders under
the right client — exactly what the app does automatically after a sync.

Run:  python3 rebuild_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from LIAS import config, db

    downloads = config.COURT_DOCS_DIR / "downloads"
    if not downloads.exists():
        print(f"✗ לא נמצאה תיקיית מסמכים: {downloads}")
        print("  ודא שתיקיית court_documents/downloads קיימת עם התיקים.")
        return 1

    # Refuse to run while the app is up — deleting the DB out from under live
    # connections causes "disk I/O error" and crashes the running engine.
    import socket
    for port in (8500, 8400):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                print(f"✗ האפליקציה פועלת (פורט {port}). סגור אותה קודם (Ctrl+C) והרץ שוב.")
                return 1

    print(f"→ בונה מחדש את lias.db מתוך {downloads}")

    # Start from a clean database so stale/duplicate rows never linger.
    db_file = config.PROJECT_ROOT / "lias.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_file) + suffix)
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    # 1) ECA cases: write a manifest so they get imported like any portal
    try:
        from eca_download import _write_case_manifest
        for case_dir in downloads.glob("*/הוצאה לפועל/*"):
            if case_dir.is_dir():
                _write_case_manifest(case_dir, case_dir.name)
    except Exception as exc:
        print(f"  ⚠ דילוג על מניפסט הוצל\"פ: {exc}")

    # 2) import every manifest CSV into a fresh DB
    from LIAS.migrate_csv import _find_manifest_dirs, _import_manifest
    db.init_db()
    n_docs = n_cases = 0
    for case_dir, csv_path in _find_manifest_dirs(downloads):
        docs, _ = _import_manifest(case_dir, csv_path, downloads)
        n_docs += docs
        n_cases += 1

    # 3) fold fragmented case-folder clients into the real client
    try:
        moved = db.merge_case_folder_clients()
        print(f"  ✓ אוחדו {moved} תיקים תחת הלקוחות הנכונים")
    except Exception as exc:
        print(f"  ⚠ איחוד לקוחות דילג: {exc}")

    print(f"✓ הושלם: {n_cases} תיקים, {n_docs} מסמכים ב-{config.PROJECT_ROOT / 'lias.db'}")
    print("  הפעל את האפליקציה:  python3 app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
