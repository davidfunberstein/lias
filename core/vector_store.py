"""
vector_store.py — lightweight document index built on SQLite FTS5.

Design goals:
- Zero new heavy dependencies (SQLite FTS5 is always available)
- Store structured analysis + raw chunks per document
- Full-text search across all docs, filterable by case/portal/doc_type
- Slot-in for real embeddings later (the schema has an `embedding` BLOB column;
  FTS5 is the fallback until a real embed provider is wired)

Schema lives in lias.db alongside existing tables.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import sqlite3 as _sqlite3
from pathlib import Path as _Path

_DB_PATH: str = ""


def _get_db_path() -> str:
    global _DB_PATH
    if _DB_PATH:
        return _DB_PATH
    # Try config module (available when running inside LIAS)
    for mod in ("config", "LIAS.config"):
        try:
            import importlib
            cfg = importlib.import_module(mod)
            _DB_PATH = str(cfg.DB_PATH)
            return _DB_PATH
        except Exception:
            pass
    # Fallback: find lias.db relative to this file
    here = _Path(__file__).resolve().parent.parent
    _DB_PATH = str(here / "lias.db")
    return _DB_PATH


def get_conn() -> _sqlite3.Connection:
    conn = _sqlite3.connect(_get_db_path(), timeout=30)
    conn.row_factory = _sqlite3.Row
    return conn

# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_analysis (
    document_id   INTEGER PRIMARY KEY REFERENCES documents(document_id) ON DELETE CASCADE,
    sub_case_id   INTEGER,
    analyzed_at   TEXT,
    analysis_src  TEXT,          -- groq / gemini / notebooklm / manual
    doc_category  TEXT,          -- בקשה / החלטה / כתב תביעה / נספח / …
    subject       TEXT,
    summary       TEXT,
    topics        TEXT,          -- JSON array
    submitter     TEXT,
    respondent    TEXT,
    dates_mentioned TEXT,        -- JSON array YYYY-MM-DD
    next_hearing  TEXT,
    legal_citations TEXT,        -- JSON array
    relief_requested TEXT,
    decision_outcome TEXT,
    attachments   TEXT,          -- JSON array
    keywords      TEXT,          -- JSON array  (for FTS boosting)
    notebook_id   TEXT,          -- Gemini Notebook id if uploaded there
    raw_text_path TEXT           -- path to .txt extracted by OCR
);

CREATE TABLE IF NOT EXISTS doc_chunks (
    chunk_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER REFERENCES documents(document_id) ON DELETE CASCADE,
    sub_case_id  INTEGER,
    chunk_idx    INTEGER,
    text         TEXT NOT NULL,
    meta         TEXT,           -- JSON: {doc_type, subject, portal, case_number, …}
    embedding    BLOB            -- reserved for future float32[] vector
);

CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(
    text, meta,
    content=doc_chunks,
    content_rowid=chunk_id,
    tokenize="unicode61 remove_diacritics 1"
);

CREATE TRIGGER IF NOT EXISTS doc_chunks_ai AFTER INSERT ON doc_chunks BEGIN
    INSERT INTO doc_chunks_fts(rowid, text, meta)
    VALUES (new.chunk_id, new.text, new.meta);
END;

CREATE TRIGGER IF NOT EXISTS doc_chunks_ad AFTER DELETE ON doc_chunks BEGIN
    INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, text, meta)
    VALUES ('delete', old.chunk_id, old.text, old.meta);
END;
"""

CHUNK_SIZE   = 600   # characters per chunk
CHUNK_OVERLAP = 80


def ensure_schema():
    """Create tables/triggers/FTS index if they don't exist yet."""
    conn = get_conn()
    for stmt in _SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                conn.execute(stmt)
            except Exception:
                pass
    conn.commit()


# ── Chunking ────────────────────────────────────────────────────────────────

def _chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks by character count (paragraph-aware)."""
    text = text.strip()
    if not text:
        return []
    # prefer splitting on paragraph breaks
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf = [], ""
    for para in paras:
        if len(buf) + len(para) + 2 > size and buf:
            chunks.append(buf)
            buf = buf[-overlap:] + "\n\n" + para
        else:
            buf = (buf + "\n\n" + para).strip() if buf else para
    if buf:
        chunks.append(buf)
    return chunks or [text[:size]]


# ── Store ───────────────────────────────────────────────────────────────────

def store_analysis(document_id: int, sub_case_id: int, analysis: dict,
                   text: str = "", src: str = "groq") -> None:
    """Persist structured analysis and chunk the raw text into FTS index."""
    ensure_schema()
    conn = get_conn()

    def _j(v):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else (v or "")

    conn.execute("""
        INSERT INTO doc_analysis
          (document_id, sub_case_id, analyzed_at, analysis_src,
           doc_category, subject, summary, topics, submitter, respondent,
           dates_mentioned, next_hearing, legal_citations,
           relief_requested, decision_outcome, attachments, keywords, raw_text_path)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(document_id) DO UPDATE SET
          analyzed_at=excluded.analyzed_at, analysis_src=excluded.analysis_src,
          doc_category=excluded.doc_category, subject=excluded.subject,
          summary=excluded.summary, topics=excluded.topics,
          submitter=excluded.submitter, respondent=excluded.respondent,
          dates_mentioned=excluded.dates_mentioned, next_hearing=excluded.next_hearing,
          legal_citations=excluded.legal_citations,
          relief_requested=excluded.relief_requested,
          decision_outcome=excluded.decision_outcome,
          attachments=excluded.attachments, keywords=excluded.keywords,
          raw_text_path=excluded.raw_text_path
    """, (
        document_id, sub_case_id, time.strftime("%Y-%m-%dT%H:%M:%S"), src,
        analysis.get("doc_category",""),
        analysis.get("subject",""),
        analysis.get("summary",""),
        _j(analysis.get("topics",[])),
        analysis.get("submitter",""),
        analysis.get("respondent",""),
        _j(analysis.get("dates_mentioned",[])),
        analysis.get("next_hearing",""),
        _j(analysis.get("legal_citations",[])),
        analysis.get("relief_requested",""),
        analysis.get("decision_outcome",""),
        _j(analysis.get("attachments",[])),
        _j(analysis.get("keywords",[])),
        analysis.get("raw_text_path",""),
    ))

    # chunk raw text into FTS
    if text.strip():
        conn.execute("DELETE FROM doc_chunks WHERE document_id=?", (document_id,))
        meta_base = json.dumps({
            "doc_category": analysis.get("doc_category",""),
            "subject":      analysis.get("subject",""),
            "submitter":    analysis.get("submitter",""),
            "keywords":     analysis.get("keywords",[]),
        }, ensure_ascii=False)
        chunks = _chunk(text)
        conn.executemany(
            "INSERT INTO doc_chunks (document_id, sub_case_id, chunk_idx, text, meta) VALUES (?,?,?,?,?)",
            [(document_id, sub_case_id, i, c, meta_base) for i, c in enumerate(chunks)]
        )

    conn.commit()


def get_analysis(document_id: int) -> dict | None:
    """Return stored analysis for one document, or None."""
    ensure_schema()
    row = get_conn().execute(
        "SELECT * FROM doc_analysis WHERE document_id=?", (document_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("topics","dates_mentioned","legal_citations","attachments","keywords"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else []
        except Exception:
            d[k] = []
    return d


def get_case_analysis(sub_case_id: int) -> list[dict]:
    """Return all stored analyses for a case."""
    ensure_schema()
    rows = get_conn().execute(
        "SELECT da.*, d.logical_name, d.doc_type, d.submission_date "
        "FROM doc_analysis da "
        "LEFT JOIN documents d ON d.document_id=da.document_id "
        "WHERE da.sub_case_id=? ORDER BY d.submission_date",
        (sub_case_id,)
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        for k in ("topics","dates_mentioned","legal_citations","attachments","keywords"):
            try: d[k] = json.loads(d[k]) if d.get(k) else []
            except Exception: d[k] = []
        result.append(d)
    return result


def search(query: str, limit: int = 10,
           sub_case_id: int | None = None,
           doc_category: str | None = None) -> list[dict]:
    """Full-text search across all indexed chunks.
    Returns list of {chunk_id, document_id, sub_case_id, text, score, meta}."""
    ensure_schema()
    if not query.strip():
        return []

    # FTS5 match syntax: sanitize query
    safe_q = query.replace('"', '""')
    params: list[Any] = [safe_q]
    extra = ""
    if sub_case_id:
        extra += " AND dc.sub_case_id=?"
        params.append(sub_case_id)
    if doc_category:
        extra += " AND da.doc_category=?"
        params.append(doc_category)
    params.append(limit)

    try:
        rows = get_conn().execute(f"""
            SELECT dc.chunk_id, dc.document_id, dc.sub_case_id,
                   dc.text, dc.meta,
                   fts.rank AS score,
                   da.doc_category, da.subject, da.submitter
            FROM doc_chunks_fts fts
            JOIN doc_chunks dc ON dc.chunk_id = fts.rowid
            LEFT JOIN doc_analysis da ON da.document_id = dc.document_id
            WHERE doc_chunks_fts MATCH ?
            {extra}
            ORDER BY fts.rank
            LIMIT ?
        """, params).fetchall()
    except Exception:
        return []

    out = []
    for r in rows:
        d = dict(r)
        try: d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
        except Exception: d["meta"] = {}
        out.append(d)
    return out


def stats() -> dict:
    """Quick summary: how many docs analyzed, chunks indexed."""
    ensure_schema()
    conn = get_conn()
    try:
        n_analyzed = conn.execute("SELECT COUNT(*) FROM doc_analysis").fetchone()[0]
        n_chunks   = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
        n_cases    = conn.execute("SELECT COUNT(DISTINCT sub_case_id) FROM doc_analysis").fetchone()[0]
        by_cat     = {r[0]: r[1] for r in conn.execute(
            "SELECT doc_category, COUNT(*) FROM doc_analysis GROUP BY doc_category"
        ).fetchall()}
        return {"analyzed": n_analyzed, "chunks": n_chunks,
                "cases": n_cases, "by_category": by_cat}
    except Exception:
        return {"analyzed": 0, "chunks": 0, "cases": 0, "by_category": {}}
