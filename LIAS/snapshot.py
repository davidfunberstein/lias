"""Snapshot diff engine — guide step 6 / מנוע השוואת מצבים — שלב 6 במדריך.

EN: Upgrades "the hash changed: yes/no" into "here is exactly what changed".
    Each sync stores the live grid as JSON; the diff against the previous
    snapshot yields added / removed / changed rows. Only added+changed rows
    go to the download queue, and the diff feeds the UI's "what's new" feed.
HE: משדרג את "החתימה השתנתה: כן/לא" ל"הנה בדיוק מה שהשתנה".
    כל סנכרון שומר את הגריד החי כ-JSON; ה-diff מול ה-Snapshot הקודם נותן
    שורות שנוספו / נעלמו / השתנו. רק נוסף+השתנה נכנסים לתור ההורדה,
    וה-diff מזין את פיד "מה חדש" ב-UI.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from . import db


def _row_key(row: dict) -> str:
    """Stable identity of a grid row / זהות יציבה של שורת גריד.

    EN: prefer the portal's own document id; fall back to date+type+submitter.
    HE: עדיף מזהה המסמך של הפורטל; אחרת תאריך+סוג+מגיש.
    """
    doc_id = str(row.get("doc_id") or row.get("מזהה ייחודי") or "").strip()
    if doc_id:
        return f"id:{doc_id}"
    return "k:" + "|".join(
        str(row.get(k, "")).strip()
        for k in ("date", "type", "submitter", "תאריך מסמך", "סוג קובץ", "מגיש")
    )


def _row_fingerprint(row: dict) -> str:
    """Content fingerprint to detect metadata changes / טביעת תוכן לזיהוי שינויי מטא-דאטה."""
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def diff_grids(prev_rows: list[dict], curr_rows: list[dict]) -> dict[str, Any]:
    """Pure diff — no I/O / diff טהור — בלי קלט/פלט. Testable / ניתן לבדיקה."""
    prev = { _row_key(r): r for r in prev_rows }
    curr = { _row_key(r): r for r in curr_rows }

    added   = [curr[k] for k in curr.keys() - prev.keys()]
    removed = [prev[k] for k in prev.keys() - curr.keys()]
    changed = [
        {"before": prev[k], "after": curr[k]}
        for k in curr.keys() & prev.keys()
        if _row_fingerprint(prev[k]) != _row_fingerprint(curr[k])
    ]
    return {
        "added": added,        # new documents / מסמכים חדשים
        "removed": removed,    # sealed/hidden docs / מסמכים שנחסו או הוסתרו
        "changed": changed,    # metadata edits / שינויי מטא-דאטה
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


def take_snapshot(sub_case_id: int, grid_rows: list[dict]) -> dict[str, Any]:
    """Store snapshot + return diff vs previous / שמירת Snapshot והחזרת diff מול הקודם."""
    conn = db.get_conn()
    prev = conn.execute(
        "SELECT grid_json FROM snapshots WHERE sub_case_id=? ORDER BY snapshot_id DESC LIMIT 1",
        (sub_case_id,),
    ).fetchone()
    prev_rows = json.loads(prev["grid_json"]) if prev else []

    diff = diff_grids(prev_rows, grid_rows)

    conn.execute(
        "INSERT INTO snapshots(sub_case_id, taken_at, grid_json, diff_from_prev_json) VALUES(?,?,?,?)",
        (
            sub_case_id,
            datetime.now().isoformat(timespec="seconds"),
            json.dumps(grid_rows, ensure_ascii=False),
            json.dumps(diff, ensure_ascii=False),
        ),
    )
    conn.commit()
    return diff


def latest_diff(sub_case_id: int) -> Optional[dict]:
    """For the UI's "what's new" / עבור "מה חדש" ב-UI."""
    row = db.get_conn().execute(
        "SELECT diff_from_prev_json, taken_at FROM snapshots WHERE sub_case_id=? "
        "ORDER BY snapshot_id DESC LIMIT 1",
        (sub_case_id,),
    ).fetchone()
    if row is None:
        return None
    out = json.loads(row["diff_from_prev_json"])
    out["taken_at"] = row["taken_at"]
    return out
