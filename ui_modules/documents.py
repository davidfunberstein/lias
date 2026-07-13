"""Document serving -- resolve and convert local files."""
from __future__ import annotations

import os
from pathlib import Path

from .db import _connect, _court_docs_dir


def serve_document(document_id: int, db_path: str, here: str):
    """Resolve local file for a document. Returns (path, name) or (None, None)."""
    con = _connect(db_path)
    if con is None:
        return None, None
    try:
        row = con.execute(
            "SELECT local_path, physical_name, logical_name FROM documents "
            "WHERE document_id=?", (document_id,)).fetchone()
    finally:
        con.close()
    if not row or not row["local_path"]:
        return None, None
    path = os.path.join(_court_docs_dir(here), row["local_path"])
    if not os.path.exists(path):
        return None, None
    return path, (row["physical_name"] or row["logical_name"] or "document")


_SOFFICE_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "soffice", "libreoffice",
]


def _find_soffice() -> str | None:
    import shutil
    for c in _SOFFICE_CANDIDATES:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        w = shutil.which(c)
        if w:
            return w
    return None


def docx_to_pdf(src_path: str) -> str | None:
    """Convert a .docx/.doc to PDF for on-screen display (cached)."""
    src = Path(src_path)
    if src.suffix.lower() not in (".docx", ".doc"):
        return None
    out_dir = src.parent / "קבצים שהומרו"
    out_pdf = out_dir / (src.stem + ".pdf")
    if out_pdf.exists() and out_pdf.stat().st_mtime >= src.stat().st_mtime:
        return str(out_pdf)
    soffice = _find_soffice()
    if not soffice:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    import subprocess, tempfile
    prof = Path(tempfile.gettempdir()) / "lias_soffice_profile"
    try:
        env = {**os.environ, "DISPLAY": "", "LSUIElement": "1"}
        subprocess.run(
            [soffice, "--headless", "--invisible", "--nodefault", "--norestore",
             "--nologo", f"-env:UserInstallation=file://{prof}",
             "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
            capture_output=True, timeout=90, env=env,
        )
    except Exception:
        return None
    return str(out_pdf) if out_pdf.exists() else None
