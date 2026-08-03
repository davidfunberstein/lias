"""
notebook_bridge.py — Gemini Notebook (NotebookLM) integration via CLI.

Uses the `notebooklm` CLI (notebooklm-py 0.8.0, requires Python 3.10+,
installed via: uv tool install "notebooklm-py[browser]").

Auth setup (one-time):
    notebooklm login --browser-cookies chrome

Per-case flow:
    1. Create or reuse a notebook for the case
    2. Upload all documents (PDF / .txt) as sources
    3. Ask 7 structured questions
    4. Save results to case_analysis/{sub_case_id}.json + doc_analysis.notebook_id
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

_NLM_BIN = os.environ.get(
    "NOTEBOOKLM_BIN",
    str(Path.home() / ".local" / "bin" / "notebooklm"),
)

_NOTEBOOK_QUESTIONS = [
    ("summary",
     "סכם את כל התיק בפסקה אחת: הנושא המרכזי, מי עומד מול מי, ומה מצב ההליך."),
    ("timeline",
     "צור ציר זמן של האירועים המרכזיים בתיק. פורמט: YYYY-MM-DD: תיאור קצר."),
    ("parties",
     "מי הצדדים בתיק? לכל צד: שם, תפקיד, ומייצג. החזר JSON: [{\"name\":\"\",\"role\":\"\",\"represented_by\":\"\"}]"),
    ("main_claims",
     "מה הטענות המרכזיות של כל צד? רשימה ממוספרת לכל צד בנפרד."),
    ("citations",
     "רשום את כל האסמכתאות המשפטיות: חקיקה, פסיקה, תקנות. החזר JSON: [\"ציטוט\"]"),
    ("open_questions",
     "מה השאלות המשפטיות שלא הוכרעו עדיין בתיק?"),
    ("inconsistencies",
     "האם יש סתירות פנימיות בין מסמכים — תאריכים שסותרים, טענות שונות על אותה עובדה?"),
]


def _run(args: list[str], timeout: int = 60, logger: "Logger | None" = None) -> str:
    """Run a notebooklm CLI command and return stdout."""
    cmd = [_NLM_BIN] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip()
            if logger:
                logger.warning(f"[notebook] CLI error: {err[:200]}")
            return ""
        return r.stdout.strip()
    except FileNotFoundError:
        if logger:
            logger.warning(f"[notebook] notebooklm not found at {_NLM_BIN}. "
                           "Run: uv tool install 'notebooklm-py[browser]'")
        return ""
    except subprocess.TimeoutExpired:
        if logger:
            logger.warning(f"[notebook] CLI timeout ({timeout}s): {' '.join(args[:3])}")
        return ""
    except Exception as e:
        if logger:
            logger.warning(f"[notebook] CLI exception: {e}")
        return ""


def is_auth_ok(logger: "Logger | None" = None) -> bool:
    """Return True if notebooklm auth is configured."""
    out = _run(["auth", "check", "--json"], timeout=20, logger=logger)
    if not out:
        return False
    try:
        d = json.loads(out)
        return d.get("status") == "ok"
    except Exception:
        return False


def create_or_get_notebook(sub_case_id: int, case_number: str,
                            logger: "Logger | None" = None) -> str | None:
    """Return notebook_id for this case, creating one if needed."""
    from core.vector_store import get_conn

    # Check if we already stored a notebook_id for this case
    row = get_conn().execute(
        "SELECT notebook_id FROM doc_analysis "
        "WHERE sub_case_id=? AND notebook_id IS NOT NULL LIMIT 1",
        (sub_case_id,)
    ).fetchone()
    if row and row[0]:
        return row[0]

    title = f"LIAS — תיק {case_number}"
    out = _run(["create", title, "--json"], timeout=30, logger=logger)
    if not out:
        return None
    try:
        nb = json.loads(out)
        nb_id = nb.get("id") or nb.get("notebook_id")
        return nb_id
    except Exception:
        # CLI may return just the ID on one line
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        return lines[-1] if lines else None


def upload_case_sources(notebook_id: str, sub_case_id: int,
                         logger: "Logger | None" = None,
                         max_docs: int = 30) -> int:
    """Upload documents for this case to the notebook. Returns count added."""
    from ui_modules.db import get_conn

    rows = get_conn().execute(
        "SELECT document_id, physical_name, logical_name FROM documents "
        "WHERE sub_case_id=? AND physical_name IS NOT NULL AND physical_name != '' "
        "ORDER BY submission_date LIMIT ?",
        (sub_case_id, max_docs)
    ).fetchall()

    added = 0
    for row in rows:
        path = Path(row["physical_name"])
        # prefer .txt if available (lighter upload)
        txt = path.with_suffix(".txt")
        src = txt if txt.exists() else path
        if not src.exists():
            continue
        out = _run(["source", "add", notebook_id, str(src), "--wait", "--json"],
                   timeout=120, logger=logger)
        if out:
            added += 1
        else:
            if logger:
                logger.warning(f"[notebook] failed to add source: {src.name}")
        time.sleep(0.5)

    return added


def ask_question(notebook_id: str, question: str,
                 logger: "Logger | None" = None) -> str:
    """Ask a single question in the notebook. Returns answer text."""
    out = _run(["ask", notebook_id, question], timeout=90, logger=logger)
    return out


def extract_case_analysis(notebook_id: str,
                           logger: "Logger | None" = None) -> dict:
    """Ask all 7 structured questions and return results dict."""
    results: dict = {
        "notebook_id": notebook_id,
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    for key, question in _NOTEBOOK_QUESTIONS:
        answer = ask_question(notebook_id, question, logger=logger)
        results[key] = answer or None
        time.sleep(1.5)   # rate limit
    return results


def run_notebook_pipeline(sub_case_id: int, case_number: str,
                           logger: "Logger | None" = None) -> dict:
    """Full pipeline: create/reuse notebook → upload → ask 7 questions → store.

    Safe to call from a background thread. Returns analysis dict (empty on failure).
    """
    from core.vector_store import ensure_schema, get_conn

    ensure_schema()

    if not is_auth_ok(logger=logger):
        if logger:
            logger.warning(
                "[notebook] auth not configured. Run: "
                "notebooklm login --browser-cookies chrome"
            )
        return {}

    nb_id = create_or_get_notebook(sub_case_id, case_number, logger=logger)
    if not nb_id:
        if logger:
            logger.warning(f"[notebook] could not create notebook for case {case_number}")
        return {}

    n = upload_case_sources(nb_id, sub_case_id, logger=logger)
    if logger:
        logger.info(f"[notebook] {n} sources uploaded for case {case_number} → {nb_id}")

    # Wait for notebook to process before asking
    time.sleep(min(n * 2, 20))

    analysis = extract_case_analysis(nb_id, logger=logger)
    if not analysis:
        return {}

    # Persist notebook_id on all doc_analysis rows for this case
    conn = get_conn()
    conn.execute(
        "UPDATE doc_analysis SET notebook_id=? WHERE sub_case_id=?",
        (nb_id, sub_case_id)
    )
    conn.commit()

    # Save full analysis as JSON beside the DB
    try:
        import config as _cfg
        out_dir = Path(_cfg.PROJECT_ROOT) / "case_analysis"
    except Exception:
        out_dir = Path(__file__).resolve().parent.parent / "case_analysis"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{sub_case_id}.json"
    out_file.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    if logger:
        logger.info(f"[notebook] analysis saved → {out_file}")

    return analysis
