"""
doc_pipeline.py — post-download analysis pipeline.

Flow per document:
  1. Extract text  (txt cache beside PDF, or OCR via Gemini/Groq)
  2. Analyze       (LLM → structured JSON via expanded _ANALYSIS_PROMPT)
  3. Store         (doc_analysis + doc_chunks FTS in lias.db)

Runs async (background thread) — never blocks downloads.
NotebookLM integration: optional, per-case, see notebook_bridge.py.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

# ── Expanded analysis prompt ───────────────────────────────────────────────────
_ANALYSIS_PROMPT = """\
לפניך טקסט של מסמך משפטי ישראלי. החזר JSON בלבד, ללא הסברים, בפורמט:
{
  "doc_category": "בקשה"|"החלטה"|"כתב הגנה"|"כתב תביעה"|"ערעור"|"נספח"|"פרוטוקול"|"חוות דעת"|"הסכמה"|"אחר",
  "subject": "נושא המסמך במשפט אחד קצר",
  "summary": "סיכום 2-3 משפטים",
  "topics": ["נושא משפטי 1", "נושא משפטי 2"],
  "submitter": "שם המגיש/פונה (אם מוזכר, אחרת null)",
  "respondent": "שם הצד שכנגד (אם מוזכר, אחרת null)",
  "dates_mentioned": ["YYYY-MM-DD"],
  "next_hearing": "YYYY-MM-DD אם מוזכר דיון עתידי, אחרת null",
  "legal_citations": ["חוק X סעיף Y", "ע\"א 123/45 פלוני נ' אלמוני"],
  "relief_requested": "הסעד המבוקש (רלוונטי לבקשות/תביעות, אחרת null)",
  "decision_outcome": "תוצאת ההחלטה אם זו החלטה — אושר/נדחה/נדחה בחלקה/אחר, אחרת null",
  "attachments": ["נספח א — תצהיר", "נספח ב — חוזה"],
  "keywords": ["מילות מפתח לחיפוש"]
}

הטקסט:
"""

# ── text extraction ────────────────────────────────────────────────────────────

def _get_text(file_path: str | Path, settings: dict,
              logger: "Logger | None" = None) -> str:
    """Return extracted text for a PDF/DOCX. Uses .txt cache if exists."""
    p = Path(file_path)
    if not p.exists():
        return ""

    # txt cache beside the PDF
    txt_path = p.with_suffix(".txt")
    if txt_path.exists() and txt_path.stat().st_size > 20:
        return txt_path.read_text(encoding="utf-8", errors="replace")

    suffix = p.suffix.lower()
    if suffix == ".pdf":
        try:
            from core.pdf_to_text import extract_text_from_pdf, resolve_ocr_provider
            provider, key = resolve_ocr_provider(settings)
            if not key:
                # fall back to PyMuPDF text layer only (no API call)
                try:
                    import fitz
                    doc = fitz.open(str(p))
                    text = "\n".join(page.get_text() for page in doc)
                    doc.close()
                    if text.strip():
                        txt_path.write_text(text, encoding="utf-8")
                    return text
                except ImportError:
                    return ""
            text = extract_text_from_pdf(p, key, logger=logger, provider=provider)
            return text
        except Exception as e:
            if logger: logger.warning(f"[pipeline] text extract failed: {e}")
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


# ── LLM analysis ──────────────────────────────────────────────────────────────

def _analyze_text(text: str, settings: dict,
                  logger: "Logger | None" = None) -> dict:
    """Call Groq/Gemini with _ANALYSIS_PROMPT. Return parsed dict or {}."""
    if not text.strip():
        return {}
    try:
        from core.pdf_to_text import resolve_ocr_provider, groq_text_completion
        provider, key = resolve_ocr_provider(settings)
        if provider != "groq" or not key:
            return {}
        raw = groq_text_completion(_ANALYSIS_PROMPT + text[:14000], key, timeout=90)
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except Exception as e:
        if logger: logger.warning(f"[pipeline] analysis failed: {e}")
        return {}


# ── pipeline entry points ─────────────────────────────────────────────────────

def process_document(document_id: int, settings: dict,
                     logger: "Logger | None" = None) -> dict:
    """Extract, analyze, and store one document. Returns analysis dict."""
    from ui_modules.db import get_conn
    from core.vector_store import store_analysis, ensure_schema

    ensure_schema()

    # fetch file path + case info from DB
    row = get_conn().execute(
        "SELECT d.physical_name, d.sub_case_id, d.doc_type "
        "FROM documents d WHERE d.document_id=?", (document_id,)
    ).fetchone()
    if not row:
        return {}

    file_path = row["physical_name"] or ""
    sub_case_id = row["sub_case_id"] or 0
    doc_type_db = row["doc_type"] or ""

    text = _get_text(file_path, settings, logger)
    if not text:
        return {}

    analysis = _analyze_text(text, settings, logger)
    if not analysis:
        # store minimal entry so we don't retry repeatedly
        analysis = {"doc_category": doc_type_db, "subject": "", "keywords": []}

    txt_path = Path(file_path).with_suffix(".txt") if file_path else None
    analysis["raw_text_path"] = str(txt_path) if txt_path and txt_path.exists() else ""

    store_analysis(document_id, sub_case_id, analysis, text=text, src="groq")
    return analysis


def process_case(sub_case_id: int, settings: dict,
                 logger: "Logger | None" = None,
                 skip_analyzed: bool = True) -> int:
    """Process all unanalyzed documents in a case. Returns count processed."""
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
            "SELECT document_id FROM documents WHERE sub_case_id=? AND physical_name IS NOT NULL",
            (sub_case_id,)
        ).fetchall()

    count = 0
    for row in rows:
        try:
            result = process_document(row[0], settings, logger)
            if result:
                count += 1
                time.sleep(0.2)   # gentle rate-limiting
        except Exception as e:
            if logger: logger.warning(f"[pipeline] doc {row[0]}: {e}")
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
