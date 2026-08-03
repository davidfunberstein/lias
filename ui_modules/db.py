"""DB helpers for the LIAS dashboard."""
from __future__ import annotations

import os
import re
import socket
import sqlite3
from collections import Counter
from datetime import datetime


def _connect(db_path: str) -> sqlite3.Connection | None:
    if not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _court_docs_dir(here: str) -> str:
    """Same resolution as LIAS/config.py."""
    try:
        import json
        with open(os.path.join(here, "session_defaults.json"), encoding="utf-8") as fh:
            override = json.load(fh).get("court_docs_dir", "")
        if override:
            return os.path.realpath(os.path.expanduser(override))
    except (OSError, ValueError):
        pass
    return os.path.join(here, "court_documents")


def _full_ui_alive() -> bool:
    """Is the engine running? (in-process)"""
    try:
        from ui_modules import engine_inproc
        return engine_inproc.alive()
    except Exception:
        return False


def _norm_doc_type(raw: str) -> str:
    """Collapse 40+ raw doc types into top-level groups."""
    t = (raw or "").strip()
    if not t:
        return "אחר"
    head = re.split(r"\s*[-–]\s*", t)[0].strip()
    for key in ("בקשה", "תגובה", "החלטה", "פסק דין", "פרוטוקול",
                "הודעה", "כתב", "אישור"):
        if head.startswith(key) or t.startswith(key):
            return key
    return "אחר"


def _parse_ddmmyyyy(s: str):
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y")
    except (ValueError, AttributeError):
        return None


def _arkaa(portal: str, sub_number: str, court: str = "") -> str:
    """Derive court instance. Prefers an explicit court name from the portal
    (cases.court) when available; otherwise falls back to number/keyword rules."""
    court = (court or "").strip()
    if court:
        if "רבני" in court:
            if "גדול" in court:
                return "בית דין רבני גדול"
            return "בית דין רבני אזורי"
        if "הוצאה" in court or "הוצל" in court:
            return "הוצאה לפועל"
        if "עבודה" in court:
            return "בית הדין לעבודה"
        if "משפחה" in court:
            return "ענייני משפחה"
        if "שלום" in court:
            return "שלום — אזרחי"
        if "מחוזי" in court:
            return "מחוזי"
        if "עליון" in court:
            return "בית משפט עליון"
        return court
    s = (sub_number or "").strip()
    if portal == "ECA":
        return "הוצאה לפועל"
    if portal == "BDR" or re.match(r"^\d{6,7}-\d+(\D|$)", s):
        return "בית דין רבני אזורי"
    if s.startswith("בל "):
        return "בית הדין לעבודה"
    if s.startswith(("תאדמ", "תא ", 'ת"א')):
        return "שלום — אזרחי"
    if any(k in s for k in ("גירושין", "החזקת ילדים", "משמורת", "מזונות", "תלהמ")):
        return "ענייני משפחה"
    return "בתי משפט (NET)"


GROUPS = ("בקשה", "תגובה", "החלטה", "פסק דין", "פרוטוקול", "אישור")


def _doc_rows(con, sub_case_id: int | None = None, client_id: int | None = None):
    """Raw doc rows with case context."""
    sql = """SELECT d.document_id, d.logical_name, d.physical_name, d.doc_type,
                    d.submitter_est, d.submission_date, d.download_status,
                    d.pages, d.local_path, s.sub_case_id, s.sub_number,
                    ca.portal, ca.client_id, ca.court, ca.title AS case_title,
                    cl.display_name AS client_name,
                    (SELECT MAX(started_at) FROM sync_runs r
                     WHERE r.sub_case_id = s.sub_case_id) AS last_synced
             FROM documents d
             JOIN sub_cases s  ON s.sub_case_id = d.sub_case_id
             JOIN cases ca     ON ca.case_id    = s.case_id
             LEFT JOIN clients cl ON cl.client_id = ca.client_id"""
    args: list = []
    if sub_case_id is not None:
        sql += " WHERE s.sub_case_id = ?"
        args.append(sub_case_id)
    elif client_id is not None:
        sql += " WHERE ca.client_id = ?"
        args.append(client_id)
    return [dict(r) for r in con.execute(sql, args)]


_HEB_NAME = re.compile(r"[\u05d0-\u05ea]{2,}\s+[\u05d0-\u05ea]{2,}")


def parties_location_from_path(local_path: str) -> tuple:
    """Parties + city out of a doc path: the client/couple folder names the
    people; the sub-case folder's trailing segment is often the city."""
    if not local_path:
        return [], ""
    parts = list(__import__("pathlib").Path(local_path).parts)
    body = parts[1:-1] if len(parts) > 2 else []
    parties: list = []
    for seg in body:
        if " - " in seg or " — " in seg:
            cand = [x.strip() for x in re.split(r"\s+-\s+|\s+—\s+", seg)]
            ppl = [c for c in cand if _HEB_NAME.search(c)]
            if len(ppl) >= 2:
                parties = ppl; break
        m = re.split(r"\s+נ['\u05f3\u2019]\s+", seg)
        if len(m) > 1:
            # strip a leading case-number prefix ("15083-09-24 — ")
            parties = [re.sub(r"^[\d\-]+\s*[—-]\s*", "", x).strip()
                       for x in m if x.strip()]; break
    location = ""
    if body:
        tail = body[-1].rsplit(" - ", 1)[-1].strip() if " - " in body[-1] else ""
        if tail and not re.search(r"\d", tail) and len(tail) <= 20:
            location = tail
    if not parties:
        try:
            import json as _json
            from LIAS.config import COURT_DOCS_DIR as _DOCS
            _case_dir = _DOCS / __import__("pathlib").Path(local_path).parent
            for _anc in (_case_dir, _case_dir.parent, _case_dir.parent.parent):
                _ci = _anc / "case_info.json"
                if _ci.exists():
                    _info = _json.loads(_ci.read_text(encoding="utf-8"))
                    for _pr in _info.get("parties", []):
                        _nm = (_pr.get("name") or "").strip()
                        if _nm:
                            parties.append(_nm)
                    location = location or _info.get("location") or _info.get("court") or ""
                    break
        except Exception:
            pass
    return parties, location


def _find_case_info(sub_number: str, portal: str) -> dict | None:
    """Find case_info.json on disk by matching sub_number against folder names.
    For BDR cases without a JSON, synthesize parties from the folder structure
    and read close_date from batch_progress CSVs."""
    try:
        import json as _json
        from LIAS.config import COURT_DOCS_DIR as _DOCS
        downloads = _DOCS / "downloads"
        if not downloads.exists():
            return None
        sn = (sub_number or "").strip()
        m = re.match(r"[\w\-]+-\d+", sn)
        sn_key = m.group(0) if m else re.sub(r"[^\d\-]", "", sn).strip("-")
        for ci in downloads.rglob("case_info.json"):
            folder = ci.parent.name
            if sn in folder or (sn_key and sn_key in folder):
                return _json.loads(ci.read_text(encoding="utf-8"))
        result: dict | None = None
        if sn_key:
            for d in downloads.rglob(f"*{sn_key}*"):
                if not d.is_dir():
                    continue
                parent = d.parent.name
                if " - " in parent:
                    names = [p.strip() for p in parent.split(" - ") if p.strip()]
                    if len(names) >= 2:
                        result = {"parties": [{"name": n} for n in names]}
                        break
        bdr_id = sn_key.replace("-", "/") if sn_key and re.match(r"^\d+-\d+$", sn_key) else ""
        if bdr_id:
            import csv as _csv
            for bp in downloads.glob("batch_progress*"):
                if not bp.is_file():
                    continue
                try:
                    with open(bp, encoding="utf-8-sig") as f:
                        for row in _csv.DictReader(f):
                            if row.get("מזהה תיק", "") == bdr_id:
                                cd = (row.get("תאריך סגירה") or "").strip()
                                if result is None:
                                    result = {}
                                # Prefer explicit status field; fall back to close_date
                                # only when the date is in the past (conservative: open if unsure)
                                explicit = (row.get("סטטוס") or "").strip()
                                if explicit:
                                    result["status"] = explicit
                                    result["status_source"] = "csv"
                                elif cd:
                                    import datetime as _dt
                                    try:
                                        parts = cd.replace("/", ".").split(".")
                                        cd_date = _dt.date(int(parts[2]), int(parts[1]), int(parts[0]))
                                        # Only infer closed if date is past — never for future or today
                                        result["status"] = "סגור" if cd_date < _dt.date.today() else "פתוח"
                                    except Exception:
                                        result["status"] = "פתוח"  # unknown → assume open
                                    result["status_source"] = f"close_date:{cd}"
                                else:
                                    result["status"] = "פתוח"
                                    result["status_source"] = "no_close_date"
                                break
                except Exception:
                    pass
        return result
    except Exception:
        pass
    return None


def _case_cards(rows: list[dict]) -> list[dict]:
    """Aggregate docs into one card per sub-case."""
    by: dict[int, dict] = {}
    for r in rows:
        c = by.setdefault(r["sub_case_id"], {
            "sub_case_id": r["sub_case_id"], "sub_number": r["sub_number"],
            "portal": r["portal"], "client_id": r["client_id"],
            "arkaa": _arkaa(r["portal"], r["sub_number"], r.get("court") or ""),
            "court": r.get("court") or "",
            "case_title": r.get("case_title") or "",
            "docs": 0, "errors": 0, "groups": {g: 0 for g in GROUPS}, "other": 0,
            "first": None, "last": None, "parties": [], "location": "",
            "last_synced": r.get("last_synced") or None,
        })
        if not c["parties"] and r.get("local_path"):
            c["parties"], c["location"] = parties_location_from_path(r["local_path"])
        if "portal_status" not in c and r.get("local_path"):
            try:
                import json as _json2
                from LIAS.config import COURT_DOCS_DIR as _DOCS2
                _cd = _DOCS2 / __import__("pathlib").Path(r["local_path"]).parent
                for _a in (_cd, _cd.parent, _cd.parent.parent):
                    _cp = _a / "case_info.json"
                    if _cp.exists():
                        c["portal_status"] = _json2.loads(
                            _cp.read_text(encoding="utf-8")).get("status", "")
                        break
            except Exception:
                pass
        c["docs"] += 1
        if (r.get("download_status") or "").upper() in ("ERROR", "FAILED") \
                or "Failed" in (r.get("download_status") or ""):
            c["errors"] += 1
        g = _norm_doc_type(r["doc_type"])
        if g in c["groups"]:
            c["groups"][g] += 1
        else:
            c["other"] += 1
        dt = _parse_ddmmyyyy(r["submission_date"])
        if dt:
            iso = dt.strftime("%Y-%m-%d")
            c["first"] = min(c["first"] or iso, iso)
            c["last"] = max(c["last"] or iso, iso)
    for c in by.values():
        if not c["parties"] or "portal_status" not in c:
            ci = _find_case_info(c["sub_number"], c["portal"])
            if ci:
                if not c["parties"]:
                    c["parties"] = [p.get("name", "") for p in ci.get("parties", [])
                                    if p.get("name")]
                if "portal_status" not in c:
                    c["portal_status"] = ci.get("status", "")
    merged: dict[str, dict] = {}
    for c in by.values():
        sn = c["sub_number"]
        m = re.match(r"(\d{6,7}-\d+)\s", sn)
        key = (c["client_id"], m.group(1)) if m else (c["client_id"], sn)
        if key in merged:
            dst = merged[key]
            dst["docs"] += c["docs"]
            dst["errors"] += c["errors"]
            for g in GROUPS:
                dst["groups"][g] = dst["groups"].get(g, 0) + c["groups"].get(g, 0)
            dst["other"] += c["other"]
            if c["first"]:
                dst["first"] = min(dst["first"] or c["first"], c["first"])
            if c["last"]:
                dst["last"] = max(dst["last"] or c["last"], c["last"])
            if not dst["parties"] and c["parties"]:
                dst["parties"] = c["parties"]
            if not dst.get("portal_status") and c.get("portal_status"):
                dst["portal_status"] = c["portal_status"]
                if c.get("status_source"):
                    dst["status_source"] = c["status_source"]
            if not dst["location"] and c["location"]:
                dst["location"] = c["location"]
            # Keep the most recent sync timestamp across merged sub-cases
            ls_dst = dst.get("last_synced") or ""
            ls_src = c.get("last_synced") or ""
            dst["last_synced"] = max(ls_dst, ls_src) or None
        else:
            if m:
                topic = re.sub(r"^\d{6,7}-\d+\s*", "", sn).rsplit(" - ", 1)[0].strip()
                c["sub_number"] = m.group(1) + (" " + topic if topic else "")
            merged[key] = c
    return sorted(merged.values(), key=lambda c: c["last"] or "", reverse=True)


def _activity(rows: list[dict], by: str = "case") -> dict:
    """Requests/decisions per day -- load map."""
    label_of: dict = {}
    pts: Counter = Counter()
    tot: Counter = Counter()
    for r in rows:
        g = _norm_doc_type(r["doc_type"])
        if g in ("בקשה", "תגובה"):
            g = "בקשה"
        elif g in ("החלטה", "פסק דין"):
            g = "החלטה"
        else:
            continue
        dt = _parse_ddmmyyyy(r["submission_date"])
        if not dt:
            continue
        key = r["client_id"] if by == "client" else r["sub_case_id"]
        label_of[key] = ((r.get("client_name") or f"לקוח {key}") if by == "client"
                         else (r["sub_number"] or str(key)))[:28]
        pts[(key, dt.strftime("%Y-%m-%d"), g)] += 1
        tot[key] += 1
    top = {k for k, _ in tot.most_common(12)}
    keys = sorted(top, key=lambda k: label_of[k])
    idx = {k: i for i, k in enumerate(keys)}
    labels = [label_of[k] for k in keys]
    ids = list(keys)
    other_i = None
    if len(tot) > len(top):
        other_i = len(keys)
        labels.append("אחר")
        ids.append(None)
    points: Counter = Counter()
    for (k, d, g), n in pts.items():
        points[(idx.get(k, other_i), d, g)] += n
    return {
        "by": by, "cases": labels, "ids": ids,
        "points": [{"x": d, "ci": ci, "g": g, "n": n}
                   for (ci, d, g), n in sorted(points.items(), key=lambda kv: kv[0][1])
                   if ci is not None],
    }


def _submitters(rows: list[dict], limit: int = 8) -> list[dict]:
    cnt: Counter = Counter()
    for r in rows:
        cnt[(r["submitter_est"] or "").strip() or "לא צוין"] += 1
    return [{"label": k, "count": v} for k, v in cnt.most_common(limit)]
