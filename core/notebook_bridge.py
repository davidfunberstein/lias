"""
notebook_bridge.py — Gemini Notebook (NotebookLM) integration.

Uses the notebooklm-py library (pip install notebooklm-py) to:
 1. Create or reuse a notebook per case
 2. Upload PDFs / Markdown as sources
 3. Ask structured questions and save the analysis to vector_store

Authentication: requires a Google account cookie exported from the browser.
The cookies are read from LIAS settings (google_cookies_path) or a default path.

This module is OPTIONAL — the pipeline works without it.
If notebooklm-py is not installed or auth fails, it logs a warning and returns {}.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

_NOTEBOOK_QUESTIONS = [
    ("summary",
     "סכם את כל התיק בפסקה אחת: הנושא המרכזי, מי עומד מול מי, ומה מצב ההליך."),
    ("timeline",
     "צור ציר זמן של האירועים המרכזיים בתיק: תאריכים + אירוע. פורמט: YYYY-MM-DD: תיאור."),
    ("parties",
     "מי הצדדים בתיק? תן שם, תפקיד ומייצג לכל צד. JSON: [{name, role, represented_by}]"),
    ("main_claims",
     "מה הטענות המרכזיות של כל צד? רשימה ממוספרת לכל צד."),
    ("citations",
     "רשום את כל האסמכתאות המשפטיות שמוזכרות: חקיקה, פסיקה, תקנות. JSON: [citation_string]"),
    ("open_questions",
     "מה השאלות המשפטיות הפתוחות שלא הוכרעו עדיין בתיק?"),
    ("inconsistencies",
     "האם יש סתירות פנימיות בין מסמכים — תאריכים שסותרים, טענות שונות על אותו עובדה?"),
]


def _try_import():
    try:
        import notebooklm
        return notebooklm
    except ImportError:
        return None


def _get_auth(settings: dict) -> dict | None:
    """Load Google auth cookies from settings or default path."""
    # Option 1: path in settings
    p = settings.get("google_notebook_cookies") or ""
    if p and Path(p).exists():
        try:
            return json.loads(Path(p).read_text())
        except Exception:
            pass
    # Option 2: default beside lias.db
    import config
    default = Path(config.PROJECT_ROOT) / "google_notebook_cookies.json"
    if default.exists():
        try:
            return json.loads(default.read_text())
        except Exception:
            pass
    return None


def create_or_get_notebook(sub_case_id: int, case_number: str,
                            settings: dict,
                            logger: "Logger | None" = None) -> str | None:
    """Return notebook_id for this case, creating one if needed.
    Notebook IDs are stored in doc_analysis.notebook_id (any doc in the case)."""
    nlm = _try_import()
    if not nlm:
        if logger: logger.warning("[notebook] notebooklm-py not installed — skipping")
        return None
    auth = _get_auth(settings)
    if not auth:
        if logger: logger.warning("[notebook] no Google auth cookies — skipping")
        return None

    # Check if we already have a notebook for this case
    from ui_modules.db import get_conn
    row = get_conn().execute(
        "SELECT notebook_id FROM doc_analysis WHERE sub_case_id=? AND notebook_id IS NOT NULL LIMIT 1",
        (sub_case_id,)
    ).fetchone()
    if row and row[0]:
        return row[0]

    try:
        nb_title = f"LIAS — תיק {case_number}"
        # notebooklm-py API
        nb = nlm.create(title=nb_title, credentials=auth)
        return nb.id
    except Exception as e:
        if logger: logger.warning(f"[notebook] create failed: {e}")
        return None


def upload_case_sources(notebook_id: str, sub_case_id: int,
                         settings: dict,
                         logger: "Logger | None" = None,
                         max_docs: int = 30) -> int:
    """Upload PDFs / .txt files for this case to the notebook.
    Returns number of sources added."""
    nlm = _try_import()
    if not nlm:
        return 0
    auth = _get_auth(settings)
    if not auth:
        return 0

    from ui_modules.db import get_conn
    rows = get_conn().execute(
        "SELECT document_id, physical_name, logical_name FROM documents "
        "WHERE sub_case_id=? AND physical_name IS NOT NULL ORDER BY submission_date LIMIT ?",
        (sub_case_id, max_docs)
    ).fetchall()

    added = 0
    try:
        nb = nlm.get(notebook_id, credentials=auth)
        for row in rows:
            path = Path(row["physical_name"])
            # prefer .txt if available (cheaper to upload)
            txt = path.with_suffix(".txt")
            src_path = txt if txt.exists() else path
            if not src_path.exists():
                continue
            try:
                nb.source.add_file(str(src_path), title=row["logical_name"] or src_path.name)
                added += 1
                time.sleep(0.5)
            except Exception as e:
                if logger: logger.warning(f"[notebook] source add failed: {e}")
    except Exception as e:
        if logger: logger.warning(f"[notebook] get notebook failed: {e}")
    return added


def extract_case_analysis(notebook_id: str, sub_case_id: int,
                           settings: dict,
                           logger: "Logger | None" = None) -> dict:
    """Ask structured questions in the notebook and return the analysis dict."""
    nlm = _try_import()
    if not nlm:
        return {}
    auth = _get_auth(settings)
    if not auth:
        return {}

    results: dict = {"notebook_id": notebook_id, "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        nb = nlm.get(notebook_id, credentials=auth)
        for key, question in _NOTEBOOK_QUESTIONS:
            try:
                answer = nb.ask(question)
                results[key] = answer
                time.sleep(1.0)   # rate limit
            except Exception as e:
                if logger: logger.warning(f"[notebook] question '{key}' failed: {e}")
                results[key] = None
    except Exception as e:
        if logger: logger.warning(f"[notebook] analysis failed: {e}")
        return {}
    return results


def run_notebook_pipeline(sub_case_id: int, case_number: str,
                           settings: dict,
                           logger: "Logger | None" = None) -> dict:
    """Full pipeline: create notebook → upload → analyze → store.
    Returns analysis dict. Safe to call in a background thread."""
    from core.vector_store import ensure_schema
    from ui_modules.db import get_conn

    ensure_schema()

    nb_id = create_or_get_notebook(sub_case_id, case_number, settings, logger)
    if not nb_id:
        return {}

    n = upload_case_sources(nb_id, sub_case_id, settings, logger)
    if logger: logger.info(f"[notebook] {n} sources uploaded for case {case_number}")

    # Wait for notebook to process sources (heuristic)
    time.sleep(min(n * 2, 30))

    analysis = extract_case_analysis(nb_id, sub_case_id, settings, logger)
    if not analysis:
        return {}

    # Store notebook_id on all doc_analysis rows for this case
    get_conn().execute(
        "UPDATE doc_analysis SET notebook_id=? WHERE sub_case_id=?",
        (nb_id, sub_case_id)
    )
    get_conn().commit()

    # Persist the full case analysis as a JSON file
    import config
    out = Path(config.PROJECT_ROOT) / "case_analysis" / f"{sub_case_id}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    if logger: logger.info(f"[notebook] analysis saved → {out}")

    return analysis
