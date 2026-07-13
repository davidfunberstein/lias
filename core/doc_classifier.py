"""Classify a document by name/type into a category: בקשה, תגובה, החלטה, etc."""
from __future__ import annotations
import re

CLASSIFICATIONS = [
    (["פסק דין", "פסק-דין"], "פסק דין"),
    (["החלטה"], "החלטה"),
    (["חוות דעת"], "חוות דעת"),
    (["תגובה"], "תגובה"),
    (["בקשה"], "בקשה"),
    (["כתב תביעה", "כתב-תביעה"], "תביעה"),
    (["כתב הגנה", "כתב-הגנה"], "הגנה"),
    (["תצהיר"], "תצהיר"),
    (["נספח"], "נספח"),
    (["הסכם"], "הסכם"),
    (["פרוטוקול"], "פרוטוקול"),
    (["זימון"], "זימון"),
    (["הודעה"], "הודעה"),
]


def classify_doc(doc_name: str, doc_type: str = "") -> str:
    """Return document category string extracted from doc_name and doc_type.
    Returns empty string if unrecognized."""
    combined = f"{doc_name} {doc_type}".strip()
    for keywords, label in CLASSIFICATIONS:
        if any(kw in combined for kw in keywords):
            return label
    return ""
