"""Shared read-queries for both servers / שאילתות קריאה משותפות לשני השרתים.

EN: One place for the SQL that feeds the UI, used by api.py (FastAPI) and
    httpd.py (zero-dependency stdlib server). Filters run in SQL, not JS.
HE: מקום אחד ל-SQL שמזין את ה-UI, בשימוש api.py (FastAPI) ו-httpd.py
    (שרת ספריית-תקן בלי תלויות). הסינון רץ ב-SQL, לא ב-JS.
"""
from __future__ import annotations

from . import config, db, snapshot


def clients() -> list[dict]:
    rows = db.get_conn().execute(
        """SELECT c.client_id, c.display_name,
                  COUNT(DISTINCT s.sub_case_id) AS sub_cases,
                  COUNT(d.document_id) AS documents
           FROM clients c
           LEFT JOIN cases k ON k.client_id = c.client_id
           LEFT JOIN sub_cases s ON s.case_id = k.case_id
           LEFT JOIN documents d ON d.sub_case_id = s.sub_case_id
           GROUP BY c.client_id ORDER BY c.display_name"""
    ).fetchall()
    return [dict(r) for r in rows]


def client_tree(client_id: int) -> list[dict]:
    conn = db.get_conn()
    cases = [dict(r) for r in conn.execute(
        "SELECT * FROM cases WHERE client_id=? ORDER BY case_number", (client_id,)).fetchall()]
    for c in cases:
        c["sub_cases"] = [dict(r) for r in conn.execute(
            """SELECT s.*, COUNT(d.document_id) AS documents,
                      SUM(CASE WHEN d.download_status='ERROR' THEN 1 ELSE 0 END) AS errors
               FROM sub_cases s LEFT JOIN documents d ON d.sub_case_id=s.sub_case_id
               WHERE s.case_id=? GROUP BY s.sub_case_id ORDER BY s.sub_number""",
            (c["case_id"],)).fetchall()]
    return cases


def documents(sub_case_id: int, status: str = "", doc_type: str = "", q: str = "",
              sort: str = "submission_date", order: str = "desc") -> list[dict]:
    _SORT_COLS = {"submission_date", "logical_name", "doc_type", "pages",
                  "download_status", "submitter_est", "document_id"}
    col = sort if sort in _SORT_COLS else "submission_date"
    dir_ = "DESC" if order.lower() != "asc" else "ASC"
    sql = "SELECT * FROM documents WHERE sub_case_id=?"
    args: list = [sub_case_id]
    if status:
        sql += " AND download_status=?"; args.append(status)
    if doc_type:
        sql += " AND doc_type LIKE ?"; args.append(f"%{doc_type}%")
    if q:
        sql += " AND (logical_name LIKE ? OR physical_name LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += f" ORDER BY {col} {dir_}, document_id DESC LIMIT 500"
    return [dict(r) for r in db.get_conn().execute(sql, args).fetchall()]


def whats_new(sub_case_id: int) -> dict:
    d = snapshot.latest_diff(sub_case_id)
    return d or {"counts": {"added": 0, "removed": 0, "changed": 0}, "taken_at": None}


def ai_context(sub_case_id: int, max_txt_chars: int = 12000) -> dict:
    """Build context dict for AI: case metadata + doc list + available .txt content."""
    conn = db.get_conn()

    sub = conn.execute(
        """SELECT s.sub_number, s.sub_type, s.status,
                  c.display_name as client_name, k.case_number, k.portal, k.court, k.title
           FROM sub_cases s
           JOIN cases k ON k.case_id = s.case_id
           JOIN clients c ON c.client_id = k.client_id
           WHERE s.sub_case_id = ?""", (sub_case_id,)).fetchone()

    if not sub:
        return {}

    docs = conn.execute(
        """SELECT physical_name, logical_name, doc_type, submission_date,
                  download_status, pages, local_path
           FROM documents WHERE sub_case_id = ?
           ORDER BY submission_date DESC""", (sub_case_id,)).fetchall()

    doc_list = [dict(d) for d in docs]

    # Collect text from .txt files alongside PDFs
    txt_parts: list[str] = []
    chars_used = 0
    for doc in doc_list:
        lp = doc.get("local_path", "")
        if not lp:
            continue
        txt_path = (config.COURT_DOCS_DIR / lp).with_suffix(".txt")
        if txt_path.exists() and chars_used < max_txt_chars:
            try:
                content = txt_path.read_text(encoding="utf-8", errors="replace")
                snippet = content[: max_txt_chars - chars_used]
                txt_parts.append(f"=== {doc['physical_name']} ===\n{snippet}")
                chars_used += len(snippet)
            except Exception:
                pass

    return {
        "sub": dict(sub),
        "doc_count": len(doc_list),
        "doc_list": doc_list[:200],  # cap for prompt size
        "txt_content": "\n\n".join(txt_parts),
    }


def job_list(limit: int = 30) -> list[dict]:
    rows = db.get_conn().execute(
        "SELECT * FROM jobs ORDER BY job_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def sync_runs(limit: int = 30) -> list[dict]:
    rows = db.get_conn().execute(
        "SELECT * FROM sync_runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
