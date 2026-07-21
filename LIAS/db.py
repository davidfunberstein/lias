"""SQLite source of truth / מקור האמת ב-SQLite.

EN: WAL mode so the browser thread, job workers and the API can read/write
    concurrently. One connection per thread (SQLite requirement) via
    thread-local storage. Schema is versioned — bump SCHEMA_VERSION and add
    a migration step to evolve safely.
HE: מצב WAL כדי ש-Thread הדפדפן, ה-Workers וה-API יקראו ויכתבו במקביל.
    חיבור אחד לכל Thread (דרישת SQLite) דרך thread-local. הסכמה מנוהלת
    גרסאות — מעלים את SCHEMA_VERSION ומוסיפים צעד מיגרציה כדי להתפתח בבטחה.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config

SCHEMA_VERSION = 2

_local = threading.local()

# ---------------------------------------------------------------------------
# DDL — mirrors models.py 1:1 / משקף את models.py אחד לאחד
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS clients (
    client_id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL UNIQUE,
    aliases_json TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS cases (
    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(client_id),
    portal TEXT NOT NULL,
    court TEXT DEFAULT '',
    case_number TEXT NOT NULL,
    title TEXT DEFAULT '',
    UNIQUE(portal, case_number)
);
CREATE TABLE IF NOT EXISTS sub_cases (
    sub_case_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(case_id),
    sub_number TEXT NOT NULL,
    sub_type TEXT DEFAULT '',
    status TEXT DEFAULT '',
    UNIQUE(case_id, sub_number)
);
CREATE TABLE IF NOT EXISTS documents (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_case_id INTEGER NOT NULL REFERENCES sub_cases(sub_case_id),
    physical_name TEXT NOT NULL,
    logical_name TEXT DEFAULT '',
    doc_type TEXT DEFAULT '',
    submitter_est TEXT DEFAULT '',
    side_label TEXT DEFAULT 'UNKNOWN',
    submission_date TEXT DEFAULT '',
    submission_time TEXT DEFAULT '',
    filing_ts_resolved TEXT,
    pages INTEGER DEFAULT 0,
    file_size_kb INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    is_sticky INTEGER DEFAULT 0,
    download_status TEXT DEFAULT 'PENDING',
    retry_count INTEGER DEFAULT 0,
    local_path TEXT DEFAULT '',
    downloaded_at TEXT,
    drive_file_id TEXT DEFAULT '',
    has_attachments INTEGER DEFAULT 0,
    UNIQUE(sub_case_id, physical_name)
);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(download_status);
CREATE INDEX IF NOT EXISTS idx_documents_sub ON documents(sub_case_id);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_case_id INTEGER NOT NULL REFERENCES sub_cases(sub_case_id),
    taken_at TEXT NOT NULL,
    grid_json TEXT NOT NULL,
    diff_from_prev_json TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT DEFAULT '{}',
    state TEXT DEFAULT 'PENDING',
    progress REAL DEFAULT 0,
    message TEXT DEFAULT '',
    error TEXT DEFAULT '',
    retry_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal TEXT NOT NULL,
    sub_case_id INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total_in_portal INTEGER DEFAULT 0,
    downloaded_new INTEGER DEFAULT 0,
    re_downloaded INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    portal_hash TEXT DEFAULT '',
    prev_hash TEXT DEFAULT '',
    hash_changed TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
"""


def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """One connection per thread / חיבור אחד לכל Thread."""
    path = str(db_path or config.DB_PATH)
    conn = getattr(_local, "conn_" + path, None)
    if conn is None:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        # EN: WAL is best for concurrency, but fails on some mounted/network
        #     filesystems (shared-memory files unsupported) → fall back to
        #     TRUNCATE, which is safe everywhere. busy_timeout still protects
        #     concurrent threads.
        # HE: WAL הכי טוב למקביליות, אבל נכשל בחלק ממערכות קבצים ממופות/רשת
        #     (אין תמיכה בקבצי זיכרון משותף) ← נסיגה ל-TRUNCATE שבטוח בכל מקום.
        #     busy_timeout עדיין מגן על Threads מקבילים.
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.execute("PRAGMA journal_mode=TRUNCATE")
        except sqlite3.OperationalError:
            conn.execute("PRAGMA journal_mode=TRUNCATE")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        setattr(_local, "conn_" + path, conn)
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Create schema if needed / יצירת הסכמה אם צריך."""
    conn = get_conn(db_path)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    _run_migrations(conn)
    conn.commit()


def _run_migrations(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row["value"]) if row else 1
    if current < 2:
        try:
            conn.execute("ALTER TABLE documents ADD COLUMN has_attachments INTEGER DEFAULT 0")
        except Exception:
            pass
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")


# ---------------------------------------------------------------------------
# Small helpers / עזרים קטנים
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert_client(name: str) -> int:
    """Return client_id, creating if new / מחזיר client_id, יוצר אם חדש."""
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO clients(display_name) VALUES(?)", (name,))
    conn.commit()
    row = conn.execute("SELECT client_id FROM clients WHERE display_name=?", (name,)).fetchone()
    return row["client_id"]


def upsert_case(client_id: int, portal: str, case_number: str, court: str = "", title: str = "") -> int:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO cases(client_id, portal, case_number, court, title) VALUES(?,?,?,?,?)",
        (client_id, portal, case_number, court, title),
    )
    conn.commit()
    row = conn.execute(
        "SELECT case_id FROM cases WHERE portal=? AND case_number=?", (portal, case_number)
    ).fetchone()
    return row["case_id"]


def upsert_sub_case(case_id: int, sub_number: str, sub_type: str = "", status: str = "") -> int:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO sub_cases(case_id, sub_number, sub_type, status) VALUES(?,?,?,?)",
        (case_id, sub_number, sub_type, status),
    )
    conn.commit()
    row = conn.execute(
        "SELECT sub_case_id FROM sub_cases WHERE case_id=? AND sub_number=?", (case_id, sub_number)
    ).fetchone()
    return row["sub_case_id"]


def upsert_document(sub_case_id: int, physical_name: str, **fields: Any) -> int:
    """Insert or update by (sub_case_id, physical_name) / הוספה או עדכון לפי המפתח."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO documents(sub_case_id, physical_name) VALUES(?,?)",
        (sub_case_id, physical_name),
    )
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE documents SET {cols} WHERE sub_case_id=? AND physical_name=?",
            (*fields.values(), sub_case_id, physical_name),
        )
    conn.commit()
    row = conn.execute(
        "SELECT document_id FROM documents WHERE sub_case_id=? AND physical_name=?",
        (sub_case_id, physical_name),
    ).fetchone()
    return row["document_id"]


def set_document_status(document_id: int, status: str, error_note: str = "") -> None:
    """The retry state machine's single write point / נקודת הכתיבה של מכונת המצבים (שלב 10)."""
    conn = get_conn()
    if status == "ERROR":
        conn.execute(
            "UPDATE documents SET download_status=?, retry_count=retry_count+1 WHERE document_id=?",
            (status, document_id),
        )
    elif status == "COMPLETED":
        conn.execute(
            "UPDATE documents SET download_status=?, downloaded_at=? WHERE document_id=?",
            (status, _now(), document_id),
        )
    else:
        conn.execute(
            "UPDATE documents SET download_status=? WHERE document_id=?", (status, document_id)
        )
    conn.commit()


def pending_documents(sub_case_id: int, max_retries: int) -> list[sqlite3.Row]:
    """Resume point after crash — guide step 10 / נקודת ההמשך אחרי קריסה — שלב 10.

    EN: everything not COMPLETED and not permanently failed.
    HE: כל מה שלא COMPLETED ולא נכשל סופית.
    """
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM documents
           WHERE sub_case_id=? AND download_status NOT IN ('COMPLETED','SKIPPED_STICKY')
             AND retry_count < ?
           ORDER BY document_id""",
        (sub_case_id, max_retries),
    ).fetchall()


# --- Jobs / משימות ----------------------------------------------------------

def create_job(kind: str, payload: dict | None = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO jobs(kind, payload_json, created_at) VALUES(?,?,?)",
        (kind, json.dumps(payload or {}, ensure_ascii=False), _now()),
    )
    conn.commit()
    return cur.lastrowid


def claim_next_job(kinds: Iterable[str]) -> Optional[sqlite3.Row]:
    """Atomically claim a PENDING job / תפיסה אטומית של משימה ממתינה."""
    conn = get_conn()
    placeholders = ",".join("?" * len(list(kinds)))
    kinds = list(kinds)
    with conn:  # transaction / טרנזקציה
        row = conn.execute(
            f"SELECT * FROM jobs WHERE state='PENDING' AND kind IN ({placeholders}) "
            "ORDER BY job_id LIMIT 1",
            kinds,
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=?", (row["job_id"],))
    return row


def update_job(job_id: int, **fields: Any) -> None:
    conn = get_conn()
    if fields.get("state") in ("COMPLETED", "ERROR", "CANCELLED"):
        fields.setdefault("finished_at", _now())
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", (*fields.values(), job_id))
    conn.commit()


def merge_case_folder_clients() -> int:
    """Fold "case-folder" clients into the real person/entity clients.

    After a flat BDR/NET import, folders like "15083-09-24 — פונברשטיין נ' בר"
    each became their own client_id. But the recurring party (the lawyer's
    client) already exists as a plain client (e.g. "דוד פונברשטיין"). This
    reassigns every case of a case-folder-client to the real client whose name
    shares a token with one of the folder's parties, then deletes the emptied
    shell. Conservative: only merges on a clear token match, never guesses.
    Returns the number of cases reassigned. Idempotent.
    """
    import re
    conn = get_conn()
    rows = conn.execute("SELECT client_id, display_name FROM clients").fetchall()

    def _is_case_folder(name: str) -> bool:
        return bool(re.search(r"\bנ['׳’]\b", name) or re.match(r"^\d+-\d+-\d+", name.strip()))

    def _tokens(name: str) -> set:
        return {t for t in re.split(r"[\s'׳’\-–.]+", name) if len(t) >= 2}

    def _parties(folder: str) -> list[str]:
        tail = folder
        for sep in (" — ", " - "):
            if sep in tail:
                tail = tail.split(sep, 1)[1]; break
        return [p.strip() for p in re.split(r"\s+נ['׳’]\s+|\s+-\s+", tail) if p.strip()]

    real = [(r["client_id"], r["display_name"], _tokens(r["display_name"]))
            for r in rows if not _is_case_folder(r["display_name"])]
    reassigned = 0
    for r in rows:
        if not _is_case_folder(r["display_name"]):
            continue
        party_tokens = set()
        for p in _parties(r["display_name"]):
            party_tokens |= _tokens(p)
        # best real client = most shared tokens (min 1), skip generic stop-tokens
        party_tokens -= {"ואח"}
        best = None; best_overlap = 0
        for cid, _nm, toks in real:
            ov = len(toks & party_tokens)
            if ov > best_overlap:
                best_overlap, best = ov, cid
        if best is None or best == r["client_id"]:
            continue
        conn.execute("UPDATE cases SET client_id=? WHERE client_id=?", (best, r["client_id"]))
        n = conn.execute("SELECT changes()").fetchone()[0]
        reassigned += n or 0
        # delete the emptied shell client
        left = conn.execute("SELECT COUNT(*) FROM cases WHERE client_id=?", (r["client_id"],)).fetchone()[0]
        if left == 0:
            conn.execute("DELETE FROM clients WHERE client_id=?", (r["client_id"],))
    conn.commit()

    # Second pass: fold multi-party "couple/procedure" folders (e.g.
    # "אישות - דוד פונברשטיין - חנה פונברשטיין") into the plain person client
    # ("דוד פונברשטיין") whose full name is contained in the folder.
    rows = conn.execute("SELECT client_id, display_name FROM clients").fetchall()
    def _is_person(name: str) -> bool:
        return " - " not in name and " — " not in name and not re.search(r"\bנ['׳’]\b", name)
    persons = [(r["client_id"], _tokens(r["display_name"]))
               for r in rows if _is_person(r["display_name"])]
    for r in rows:
        if _is_person(r["display_name"]):
            continue
        folder_toks = _tokens(r["display_name"])
        for cid, ptoks in persons:
            if cid != r["client_id"] and len(ptoks) >= 2 and ptoks <= folder_toks:
                conn.execute("UPDATE cases SET client_id=? WHERE client_id=?", (cid, r["client_id"]))
                reassigned += conn.execute("SELECT changes()").fetchone()[0] or 0
                if conn.execute("SELECT COUNT(*) FROM cases WHERE client_id=?", (r["client_id"],)).fetchone()[0] == 0:
                    conn.execute("DELETE FROM clients WHERE client_id=?", (r["client_id"],))
                break
    conn.commit()
    return reassigned
