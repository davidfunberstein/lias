"""
doc_pipeline.py — post-download text extraction + FTS indexing pipeline.

Flow per document:
  1. Extract text  (txt cache beside PDF, or PyMuPDF text layer)
  2. Store chunks  (doc_chunks FTS in lias.db for full-text search)

Analysis (structured fields) runs at the CASE level via NotebookLM:
  → notebook_bridge.run_notebook_pipeline(sub_case_id, case_number)

Runs in a background thread — never blocks downloads.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

# ── text extraction ────────────────────────────────────────────────────────────

def _get_text(file_path: str | Path, settings: dict | None = None,
              logger: "Logger | None" = None) -> str:
    """Return extracted text for a PDF/DOCX. Uses .txt cache if available,
    then falls back to PyMuPDF text layer (no external API call)."""
    p = Path(file_path)
    if not p.exists():
        return ""

    txt_path = p.with_suffix(".txt")
    if txt_path.exists() and txt_path.stat().st_size > 20:
        return txt_path.read_text(encoding="utf-8", errors="replace")

    suffix = p.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(p))
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            if text.strip():
                txt_path.write_text(text, encoding="utf-8")
            return text
        except ImportError:
            pass
        except Exception as e:
            if logger:
                logger.warning(f"[pipeline] PDF text extract failed: {e}")
        return ""
    elif suffix in (".docx", ".doc"):
        try:
            import mammoth
            with open(p, "rb") as f:
                result = mammoth.extract_raw_text(f)
            text = result.value
            if text.strip():
                txt_path.write_text(text, encoding="utf-8")
            return text
        except Exception:
            return ""
    return ""


# ── pipeline entry points ─────────────────────────────────────────────────────

def process_document(document_id: int, settings: dict | None = None,
                     logger: "Logger | None" = None) -> dict:
    """Extract text and index document chunks in FTS.

    Structured analysis (subject, citations, outcome …) happens at the CASE
    level via notebook_bridge.run_notebook_pipeline().
    """
    from ui_modules.db import get_conn
    from core.vector_store import store_analysis, ensure_schema

    ensure_schema()

    row = get_conn().execute(
        "SELECT d.physical_name, d.sub_case_id, d.doc_type "
        "FROM documents d WHERE d.document_id=?", (document_id,)
    ).fetchone()
    if not row:
        return {}

    file_path  = row["physical_name"] or ""
    sub_case_id = row["sub_case_id"] or 0
    doc_type_db = row["doc_type"] or ""

    text = _get_text(file_path, settings, logger)
    if not text:
        return {}

    txt_path = Path(file_path).with_suffix(".txt") if file_path else None
    analysis = {
        "doc_category": doc_type_db,
        "subject": "",
        "keywords": [],
        "raw_text_path": str(txt_path) if txt_path and txt_path.exists() else "",
    }

    store_analysis(document_id, sub_case_id, analysis, text=text, src="fts_only")
    return analysis


def process_case(sub_case_id: int, settings: dict | None = None,
                 logger: "Logger | None" = None,
                 skip_analyzed: bool = True,
                 run_notebook: bool = True) -> int:
    """Extract text + index all documents in a case. Returns count processed.

    If run_notebook=True (default), also triggers NotebookLM analysis for the
    full case after all documents are indexed.
    """
    from ui_modules.db import get_conn
    from core.vector_store import ensure_schema

    ensure_schema()
    conn = get_conn()

    if skip_analyzed:
        rows = conn.execute("""
            SELECT d.document_id FROM documents d
            LEFT JOIN doc_analysis da ON da.document_id=d.document_id
            WHERE d.sub_case_id=? AND d.physical_name IS NOT NULL
              AND d.physical_name != '' AND da.document_id IS NULL
        """, (sub_case_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT document_id FROM documents "
            "WHERE sub_case_id=? AND physical_name IS NOT NULL AND physical_name != ''",
            (sub_case_id,)
        ).fetchall()

    count = 0
    for row in rows:
        try:
            result = process_document(row[0], settings, logger)
            if result:
                count += 1
                time.sleep(0.1)
        except Exception as e:
            if logger:
                logger.warning(f"[pipeline] doc {row[0]}: {e}")

    if run_notebook and count > 0:
        try:
            case_row = conn.execute(
                "SELECT case_number FROM sub_cases WHERE sub_case_id=?",
                (sub_case_id,)
            ).fetchone()
            case_number = case_row["case_number"] if case_row else str(sub_case_id)
            from core.notebook_bridge import run_notebook_pipeline
            run_notebook_pipeline(sub_case_id, case_number, logger=logger)
        except Exception as e:
            if logger:
                logger.warning(f"[pipeline] notebook pipeline failed: {e}")

    return count


# ── background queue ───────────────────────────────────────────────────────────

_queue: list[tuple[int, dict]] = []
_lock  = threading.Lock()
_worker_running = False


def _worker():
    global _worker_running
    from core.download import SESSION_SETTINGS
    while True:
        with _lock:
            if not _queue:
                _worker_running = False
                return
            doc_id, settings = _queue.pop(0)
        try:
            process_document(doc_id, settings)
        except Exception:
            pass
        time.sleep(0.1)


def queue_document(document_id: int, settings: dict | None = None) -> None:
    """Enqueue a document for background analysis. Fire-and-forget."""
    global _worker_running
    from core.download import SESSION_SETTINGS
    s = settings or dict(SESSION_SETTINGS)
    with _lock:
        _queue.append((document_id, s))
        if not _worker_running:
            _worker_running = True
            t = threading.Thread(target=_worker, daemon=True, name="lias-analysis")
            t.start()
