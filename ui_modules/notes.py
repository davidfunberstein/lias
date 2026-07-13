"""Pins & notes -- document annotations."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


def _read_notes(notes_path: str) -> dict:
    try:
        with open(notes_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"items": []}


def _write_notes(data: dict, notes_path: str) -> None:
    tmp = notes_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, notes_path)


def _notes_save(payload: dict, notes_path: str) -> dict:
    data = _read_notes(notes_path)
    item = {
        "id": max([i.get("id", 0) for i in data["items"]] + [0]) + 1,
        "document_id": payload.get("document_id"),
        "doc_name": (payload.get("doc_name") or "")[:200],
        "sub_number": (payload.get("sub_number") or "")[:100],
        "topic": (payload.get("topic") or "כללי").strip()[:60],
        "note": (payload.get("note") or "")[:2000],
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    data["items"].insert(0, item)
    _write_notes(data, notes_path)
    return {"ok": True, "item": item}


def _notes_export_pdf(payload: dict, notes_path: str,
                      serve_document_fn) -> dict:
    """Bake notes into a new PDF saved beside the original."""
    doc_id = payload.get("document_id")
    if not doc_id:
        return {"ok": False, "error": "document_id required"}
    src_path, fname = serve_document_fn(int(doc_id))
    if not src_path:
        return {"ok": False, "error": "הקובץ לא נמצא בדיסק"}
    src = Path(src_path)
    if src.suffix.lower() != ".pdf":
        return {"ok": False, "error": "ייצוא מסומן נתמך רק ל-PDF"}
    notes = [i for i in _read_notes(notes_path)["items"] if i.get("document_id") == doc_id]
    if not notes:
        return {"ok": False, "error": "אין הערות למסמך זה"}
    try:
        import io
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        font_name = "Helvetica"
        for cand in ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
                     "/System/Library/Fonts/Supplemental/Arial.ttf",
                     "/Library/Fonts/Arial.ttf"):
            if Path(cand).exists():
                pdfmetrics.registerFont(TTFont("HebFont", cand))
                font_name = "HebFont"
                break

        reader = PdfReader(str(src))
        writer = PdfWriter()
        for p in reader.pages:
            writer.add_page(p)

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        c.setFont(font_name, 16)
        c.drawRightString(w - 40, h - 50, "הערות למסמך")
        c.setFont(font_name, 11)
        y = h - 85
        for n in notes:
            line = f"[{n.get('topic','')}] {n.get('note','')}  ({n.get('created','')[:10]})"
            for chunk in [line[i:i+90] for i in range(0, len(line), 90)] or [""]:
                c.drawRightString(w - 40, y, chunk)
                y -= 16
                if y < 60:
                    c.showPage(); c.setFont(font_name, 11); y = h - 60
            y -= 6
        c.save()
        buf.seek(0)
        for p in PdfReader(buf).pages:
            writer.add_page(p)

        out = src.with_name(src.stem + " — מסומן.pdf")
        with out.open("wb") as f:
            writer.write(f)
        return {"ok": True, "path": str(out), "name": out.name, "notes": len(notes)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _notes_delete(payload: dict, notes_path: str) -> dict:
    data = _read_notes(notes_path)
    n = len(data["items"])
    data["items"] = [i for i in data["items"] if i.get("id") != payload.get("id")]
    _write_notes(data, notes_path)
    return {"ok": True, "deleted": n - len(data["items"])}
