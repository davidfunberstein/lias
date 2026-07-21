"""Dashboard payload builders."""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

from .db import (
    _connect, _full_ui_alive, _norm_doc_type, _parse_ddmmyyyy,
    _arkaa, _doc_rows, _case_cards, _activity, _submitters, GROUPS,
)

HEB_MONTHS = ["ינו", "פבר", "מרץ", "אפר", "מאי", "יונ",
              "יול", "אוג", "ספט", "אוק", "נוב", "דצמ"]


def dashboard_from_db(con: sqlite3.Connection, full_ui_port: int) -> dict:
    q = con.execute

    status = dict(q(
        "SELECT download_status, COUNT(*) FROM documents GROUP BY download_status"
    ).fetchall())
    total_docs = sum(status.values())
    n_clients = q("SELECT COUNT(*) FROM clients").fetchone()[0]
    n_cases = q("SELECT COUNT(*) FROM cases").fetchone()[0]
    n_sub = q("SELECT COUNT(*) FROM sub_cases").fetchone()[0]
    total_pages = q("SELECT COALESCE(SUM(pages),0) FROM documents").fetchone()[0]

    counts: Counter = Counter()
    for (d,) in q("SELECT submission_date FROM documents WHERE submission_date != ''"):
        dt = _parse_ddmmyyyy(d)
        if dt:
            counts[(dt.year, dt.month)] += 1
    monthly = []
    if counts:
        last = max(counts)
        y, m = last
        keys = []
        for _ in range(12):
            keys.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        for (yy, mm) in reversed(keys):
            monthly.append({
                "label": f"{HEB_MONTHS[mm - 1]} {str(yy)[2:]}",
                "ym": f"{yy}-{mm:02d}",
                "count": counts.get((yy, mm), 0),
            })

    types: Counter = Counter()
    for (t,) in q("SELECT doc_type FROM documents"):
        types[_norm_doc_type(t)] += 1
    other = types.pop("אחר", 0)
    doc_types = [{"label": k, "count": v} for k, v in types.most_common(6)]
    rest = other + sum(types.values()) - sum(d["count"] for d in doc_types)
    if rest > 0:
        doc_types.append({"label": "אחר", "count": rest})

    recent_docs = [dict(r) for r in q("""
        SELECT d.document_id, d.logical_name, d.physical_name, d.doc_type,
               d.submission_date, d.download_status, d.pages, d.file_size_kb,
               s.sub_number, ca.client_id, cl.display_name AS client_name
        FROM documents d
        LEFT JOIN sub_cases s ON s.sub_case_id = d.sub_case_id
        LEFT JOIN cases ca ON ca.case_id = s.case_id
        LEFT JOIN clients cl ON cl.client_id = ca.client_id
        ORDER BY d.downloaded_at DESC, d.document_id DESC
        LIMIT 8""")]

    jobs = [dict(r) for r in q("""
        SELECT job_id, kind, state, progress, message, created_at
        FROM jobs ORDER BY job_id DESC LIMIT 40""")]

    row = q("""SELECT portal, started_at, finished_at, total_in_portal,
                      downloaded_new, failed, hash_changed
               FROM sync_runs ORDER BY run_id DESC LIMIT 1""").fetchone()
    last_sync = dict(row) if row else None

    clients = [dict(r) for r in q("""
        SELECT c.client_id, c.display_name,
               COUNT(DISTINCT ca.case_id) AS cases,
               COUNT(d.document_id)       AS docs
        FROM clients c
        LEFT JOIN cases ca     ON ca.client_id = c.client_id
        LEFT JOIN sub_cases s  ON s.case_id    = ca.case_id
        LEFT JOIN documents d  ON d.sub_case_id = s.sub_case_id
        GROUP BY c.client_id ORDER BY docs DESC""")]

    all_rows = _doc_rows(con)
    cards = _case_cards(all_rows)
    arkaa: dict[str, dict] = {}
    for c in cards:
        a = arkaa.setdefault(c["arkaa"], {"label": c["arkaa"], "cases": 0, "docs": 0})
        a["cases"] += 1
        a["docs"] += c["docs"]

    return {
        "demo_mode": False,
        "case_cards": cards,
        "arkaa": sorted(arkaa.values(), key=lambda a: a["docs"], reverse=True),
        "submitters": _submitters(all_rows),
        "activity": _activity(all_rows, by="client"),
        "live": _full_ui_alive(full_ui_port),
        "full_ui_url": f"http://localhost:{full_ui_port}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "kpis": {
            "total_docs": total_docs,
            "completed": status.get("COMPLETED", 0),
            "errors": status.get("ERROR", 0),
            "pending": status.get("PENDING", 0),
            "clients": n_clients,
            "cases": n_cases,
            "sub_cases": n_sub,
            "pages": total_pages,
        },
        "monthly": monthly,
        "doc_types": doc_types,
        "recent_docs": recent_docs,
        "jobs": jobs,
        "last_sync": last_sync,
        "clients": clients,
    }


def demo_payload(full_ui_port: int) -> dict:
    """Fallback when no DB exists."""
    today = datetime.now()
    monthly = []
    seq = [12, 19, 8, 24, 31, 17, 22, 40, 28, 35, 21, 44]
    for i in range(11, -1, -1):
        dt = today - timedelta(days=30 * i)
        monthly.append({"label": f"{HEB_MONTHS[dt.month - 1]} {str(dt.year)[2:]}",
                        "ym": dt.strftime("%Y-%m"),
                        "count": seq[11 - i]})
    return {
        "demo_mode": True,
        "case_cards": [
            {"sub_case_id": 439, "sub_number": "1355021-2 החזקת ילדים", "portal": "NET",
             "client_id": 1, "arkaa": "ענייני משפחה", "docs": 352,
             "groups": {"בקשה": 150, "תגובה": 40, "החלטה": 120, "פסק דין": 2,
                        "פרוטוקול": 10, "אישור": 20}, "other": 10,
             "first": "2022-01-31", "last": "2026-07-02"},
            {"sub_case_id": 502, "sub_number": "בל 12355-06-24", "portal": "NET",
             "client_id": 1, "arkaa": "בית הדין לעבודה", "docs": 54,
             "groups": {"בקשה": 20, "תגובה": 6, "החלטה": 18, "פסק דין": 0,
                        "פרוטוקול": 3, "אישור": 5}, "other": 2,
             "first": "2024-12-01", "last": "2026-05-30"}],
        "arkaa": [{"label": "ענייני משפחה", "cases": 2, "docs": 374},
                  {"label": "בית הדין לעבודה", "cases": 1, "docs": 54},
                  {"label": "בית דין רבני", "cases": 1, "docs": 22}],
        "activity": {"by": "case", "ids": [439, 502],
            "cases": ["1355021-2 החזקת ילדים", "בל 12355-06-24"],
            "points": [{"x": f"2026-06-0{d}", "ci": d % 2, "g": g, "n": (d % 3) + 1}
                       for d in range(1, 9) for g in ("בקשה", "החלטה")]},
        "submitters": [{"label": "שטרן ג'רמי", "count": 70},
                       {"label": "ברזיק תומר", "count": 56},
                       {"label": "לא צוין", "count": 227}],
        "live": _full_ui_alive(full_ui_port),
        "full_ui_url": f"http://localhost:{full_ui_port}",
        "generated_at": today.isoformat(timespec="seconds"),
        "kpis": {"total_docs": 445, "completed": 442, "errors": 2, "pending": 1,
                 "clients": 3, "cases": 5, "sub_cases": 9, "pages": 3120},
        "monthly": monthly,
        "doc_types": [
            {"label": "בקשה", "count": 182}, {"label": "החלטה", "count": 121},
            {"label": "תגובה", "count": 54}, {"label": "פרוטוקול", "count": 25},
            {"label": "פסק דין", "count": 9}, {"label": "אחר", "count": 54}],
        "recent_docs": [
            {"document_id": i, "logical_name": n, "doc_type": t,
             "submission_date": d, "download_status": s, "pages": p,
             "sub_number": c}
            for i, (n, t, d, s, p, c) in enumerate([
                ("החלטה בבקשה למתן הוראות", "החלטה", "02/07/2026", "COMPLETED", 3, "1355021-1"),
                ("תגובה לבקשת הצד השני", "תגובה", "30/06/2026", "COMPLETED", 7, "1355021-2"),
                ("בקשה לעיון במסמכים", "בקשה", "28/06/2026", "PENDING", 2, "1355021-1"),
                ("פרוטוקול דיון", "פרוטוקול", "25/06/2026", "COMPLETED", 14, "12355-06-24"),
                ("החלטה — הבהרה", "החלטה", "21/06/2026", "ERROR", 1, "1355021-2"),
            ], 1)],
        "jobs": [
            {"job_id": 140, "kind": "net_sync_current", "state": "COMPLETED",
             "progress": 100, "message": "הורדו 3 מסמכים חדשים", "created_at": "2026-07-05 21:14"},
            {"job_id": 139, "kind": "open_portal", "state": "COMPLETED",
             "progress": 100, "message": "התחברות ל-NET הצליחה", "created_at": "2026-07-05 21:02"},
            {"job_id": 138, "kind": "bdr_batch", "state": "ERROR",
             "progress": 40, "message": "פג תוקף session", "created_at": "2026-07-04 18:40"},
        ],
        "last_sync": {"portal": "NET", "started_at": "2026-07-05 21:14",
                      "finished_at": "2026-07-05 21:19", "total_in_portal": 445,
                      "downloaded_new": 3, "failed": 0, "hash_changed": 1},
        "clients": [
            {"client_id": 1, "display_name": "פונברשטיין", "cases": 2, "docs": 391},
            {"client_id": 2, "display_name": "לקוח ב'", "cases": 2, "docs": 41},
            {"client_id": 3, "display_name": "לקוח ג'", "cases": 1, "docs": 13}],
    }


def docs_list(params: dict, db_path: str) -> dict:
    """Paged, filtered document list."""
    con = _connect(db_path)
    if con is None:
        return {"total": 0, "docs": []}
    try:
        sub = params.get("sub_case_id")
        cli = params.get("client_id")
        rows = _doc_rows(con,
                         sub_case_id=int(sub) if sub else None,
                         client_id=int(cli) if (cli and not sub) else None)
    finally:
        con.close()
    group = params.get("group", "")
    q = (params.get("q") or "").strip()
    month = (params.get("month") or "").strip()
    day = (params.get("date") or "").strip()
    submitter = (params.get("submitter") or "").strip()

    def ok(r):
        g = _norm_doc_type(r["doc_type"])
        if group == "החלטה":
            if g not in ("החלטה", "פסק דין"):
                return False
        elif group == "בקשה":
            if g not in ("בקשה", "תגובה"):
                return False
        elif group and g != group:
            return False
        if month or day:
            dt = _parse_ddmmyyyy(r["submission_date"])
            if not dt:
                return False
            if month and dt.strftime("%Y-%m") != month:
                return False
            if day and dt.strftime("%Y-%m-%d") != day:
                return False
        if submitter and ((r["submitter_est"] or "").strip() or "לא צוין") != submitter:
            return False
        if q and all(q not in (r[f] or "") for f in
                     ("logical_name", "physical_name", "doc_type", "submitter_est", "sub_number")):
            return False
        return True

    rows = [r for r in rows if ok(r)]
    rows.sort(key=lambda r: (_parse_ddmmyyyy(r["submission_date"]) or datetime.min),
              reverse=True)
    off = int(params.get("offset", 0) or 0)
    lim = min(int(params.get("limit", 30) or 30), 200)
    return {"total": len(rows), "docs": rows[off:off + lim]}


def search_all(q: str, db_path: str) -> dict:
    """Top-bar autocomplete."""
    q = (q or "").strip()
    out = {"clients": [], "cases": [], "docs": []}
    if len(q) < 2:
        return out
    con = _connect(db_path)
    if con is None:
        return out
    like = f"%{q}%"
    try:
        out["clients"] = [dict(r) for r in con.execute(
            "SELECT client_id, display_name FROM clients WHERE display_name LIKE ? LIMIT 6",
            (like,))]
        # Cases matched by number, by the parties (client name / folder path),
        # or by court/arkaa keyword — with portal so the UI can label them.
        rows = con.execute(
            """SELECT s.sub_case_id, s.sub_number, ca.portal, cl.display_name AS client,
                      (SELECT local_path FROM documents WHERE sub_case_id=s.sub_case_id
                       AND local_path!='' LIMIT 1) AS lp
               FROM sub_cases s
               JOIN cases ca ON ca.case_id=s.case_id
               LEFT JOIN clients cl ON cl.client_id=ca.client_id
               WHERE s.sub_number LIKE ? OR cl.display_name LIKE ?
               ORDER BY s.sub_number LIMIT 40""", (like, like)).fetchall()
        seen = set()
        cases = []
        for r in rows:
            d = dict(r)
            d["arkaa"] = _arkaa(d.get("portal"), d.get("sub_number"))
            # also allow matching the folder path (parties) and arkaa text
            if d["sub_case_id"] in seen:
                continue
            seen.add(d["sub_case_id"])
            cases.append({"sub_case_id": d["sub_case_id"], "sub_number": d["sub_number"],
                          "portal": d["portal"], "arkaa": d["arkaa"], "client": d.get("client")})
        # court/arkaa keyword search over all cases (small dataset)
        if len(q) >= 2:
            for r in con.execute(
                """SELECT s.sub_case_id, s.sub_number, ca.portal, cl.display_name AS client,
                          (SELECT local_path FROM documents WHERE sub_case_id=s.sub_case_id AND local_path!='' LIMIT 1) AS lp
                   FROM sub_cases s JOIN cases ca ON ca.case_id=s.case_id
                   LEFT JOIN clients cl ON cl.client_id=ca.client_id""").fetchall():
                d = dict(r)
                if d["sub_case_id"] in seen:
                    continue
                ark = _arkaa(d.get("portal"), d.get("sub_number"))
                if q in ark or (d.get("lp") and q in d["lp"]):
                    seen.add(d["sub_case_id"])
                    cases.append({"sub_case_id": d["sub_case_id"], "sub_number": d["sub_number"],
                                  "portal": d["portal"], "arkaa": ark, "client": d.get("client")})
        out["cases"] = cases[:10]
        out["docs"] = [dict(r) for r in con.execute(
            """SELECT d.document_id, d.logical_name, d.local_path, s.sub_number
               FROM documents d JOIN sub_cases s ON s.sub_case_id=d.sub_case_id
               WHERE d.logical_name LIKE ? OR d.doc_type LIKE ?
               ORDER BY d.document_id DESC LIMIT 8""", (like, like))]
    finally:
        con.close()
    return out


def _case_parties_and_location(rows: list, portal: str) -> tuple:
    """Best-effort parties + location for the case-detail header.
    - ECA: read case_info.json (role/name/id) saved on first visit.
    - BDR/NET: parse the client/couple folder ("אישות - דוד x - חנה x") for
      the two person parties, and the trailing city from the sub-case folder
      ("... - פתח תקוה"). Returns (parties_list, location_str)."""
    import json, re, os
    from pathlib import Path as _Path
    try:
        from LIAS.config import COURT_DOCS_DIR as _DOCS
    except Exception:
        _DOCS = _Path(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   "court_documents"))
    parties: list = []
    location = ""

    lp = ""
    for r in rows:
        if r.get("local_path"):
            lp = r["local_path"]; break
    if not lp:
        return parties, location

    parts = _Path(lp).parts                       # downloads/client/case/sub/file
    body = list(parts[1:-1]) if len(parts) > 2 else []

    # 1) ECA parties from case_info.json (saved on first visit to the case)
    case_dir = _DOCS / _Path(lp).parent
    for anc in [case_dir, case_dir.parent, case_dir.parent.parent]:
        try:
            info_p = anc / "case_info.json"
            if info_p.exists():
                info = json.loads(info_p.read_text(encoding="utf-8"))
                for pr in info.get("parties", []):
                    nm = (pr.get("name") or "").strip()
                    role = (pr.get("role") or "").strip()
                    if nm:
                        parties.append(f"{role}: {nm}" if role else nm)
                location = info.get("city") or info.get("location") or ""
                break
        except Exception:
            pass

    _NAME = re.compile(r"[\u05d0-\u05ea]{2,}\s+[\u05d0-\u05ea]{2,}")
    def _people(seg: str) -> list:
        if " - " in seg or " \u2014 " in seg:
            cand = [x.strip() for x in re.split(r"\s+-\s+|\s+\u2014\s+", seg)]
            return [c for c in cand if _NAME.search(c)]
        m = re.split(r"\s+\u05e0['\u05f3\u2019]\s+", seg)
        return [x.strip() for x in m if x.strip()] if len(m) > 1 else []

    # 2) parties from the first folder segment that names >=2 people
    if not parties:
        for seg in body:
            ppl = _people(seg)
            if len(ppl) >= 2:
                parties = ppl; break

    # 3) location: trailing city in the sub-case folder ("... - פתח תקוה")
    if not location and body:
        sub = body[-1]
        tail = sub.rsplit(" - ", 1)[-1].strip() if " - " in sub else ""
        if tail and not re.search(r"\d", tail) and len(tail) <= 20:
            location = tail
    return parties, location


def case_view(sub_case_id: int, params: dict, db_path: str) -> dict:
    """Case screen: stats + filtered docs."""
    con = _connect(db_path)
    if con is None:
        return {"error": "no db"}
    try:
        rows = _doc_rows(con, sub_case_id=sub_case_id)
    finally:
        con.close()
    if not rows:
        return {"error": "case not found"}

    hide_approvals = params.get("hide_approvals", "1") != "0"
    group = params.get("group", "")
    submitter = params.get("submitter", "")
    q = params.get("q", "").strip()

    stats = {g: 0 for g in GROUPS}
    stats["אחר"] = 0
    for r in rows:
        g = _norm_doc_type(r["doc_type"])
        stats[g if g in stats else "אחר"] += 1

    def visible(r: dict) -> bool:
        g = _norm_doc_type(r["doc_type"])
        if hide_approvals and g == "אישור":
            return False
        if group and g != group:
            return False
        if submitter and ((r["submitter_est"] or "").strip() or "לא צוין") != submitter:
            return False
        if q and q not in (r["logical_name"] or "") and q not in (r["doc_type"] or "") \
                and q not in (r["physical_name"] or ""):
            return False
        return True

    docs = [r for r in rows if visible(r)]
    docs.sort(key=lambda r: (_parse_ddmmyyyy(r["submission_date"]) or datetime.min),
              reverse=True)
    dates = sorted(d for d in (_parse_ddmmyyyy(r["submission_date"]) for r in rows) if d)
    first = rows[0]
    parties, location = _case_parties_and_location(rows, first["portal"])
    return {
        "sub_case_id": sub_case_id,
        "sub_number": first["sub_number"],
        "portal": first["portal"],
        "arkaa": _arkaa(first["portal"], first["sub_number"]),
        "client_id": first["client_id"],
        "parties": parties,
        "location": location,
        "stats": stats,
        "total": len(rows),
        "shown": len(docs),
        "hidden": len(rows) - len(docs),
        "submitters": _submitters(rows, 12),
        "first_date": dates[0].strftime("%d/%m/%Y") if dates else "",
        "last_date": dates[-1].strftime("%d/%m/%Y") if dates else "",
        "docs": docs[:400],
    }


def client_view(client_id: int, db_path: str) -> dict:
    """Client dashboard."""
    con = _connect(db_path)
    if con is None:
        return {"error": "no db"}
    try:
        name = con.execute("SELECT display_name FROM clients WHERE client_id=?",
                           (client_id,)).fetchone()
        rows = _doc_rows(con, client_id=client_id)
    finally:
        con.close()
    cards = _case_cards(rows)
    counts: Counter = Counter()
    monthly_c: Counter = Counter()
    for r in rows:
        counts[_norm_doc_type(r["doc_type"])] += 1
        dt = _parse_ddmmyyyy(r["submission_date"])
        if dt:
            monthly_c[(dt.year, dt.month)] += 1
    mon = []
    for (y, m) in sorted(monthly_c)[-12:]:
        mon.append({"label": f"{HEB_MONTHS[m-1]} {str(y)[2:]}",
                    "ym": f"{y}-{m:02d}",
                    "count": monthly_c[(y, m)]})
    return {
        "client_id": client_id,
        "display_name": name[0] if name else f"לקוח {client_id}",
        "kpis": {"docs": len(rows), "cases": len(cards),
                 "requests": counts.get("בקשה", 0),
                 "decisions": counts.get("החלטה", 0) + counts.get("פסק דין", 0)},
        "case_cards": cards,
        "submitters": _submitters(rows),
        "activity": _activity(rows),
        "monthly": mon,
        "deadlines": [],
    }


def _empty_payload(reason: str) -> dict:
    """No fake data -- empty dashboard with explanation."""
    template = {
        "demo_mode": False, "case_cards": [], "arkaa": [], "submitters": [],
        "activity": {}, "live": False, "full_ui_url": "", "generated_at": "",
        "kpis": {}, "monthly": [], "doc_types": [], "recent_docs": [],
        "jobs": [], "last_sync": {}, "clients": [],
    }
    return {k: ([] if isinstance(v, list) else ({} if isinstance(v, dict) else 0))
            for k, v in template.items()} | {"empty": True, "empty_reason": reason,
                                              "generated": datetime.now().isoformat(timespec="seconds")}


def build_dashboard(db_path: str, full_ui_port: int) -> dict:
    con = _connect(db_path)
    if con is None:
        return _empty_payload("אין עדיין DB — הפעל את המנוע ובצע סנכרון ראשון")
    try:
        return dashboard_from_db(con, full_ui_port)
    except sqlite3.Error as exc:
        print(f"[warn] DB read failed ({exc})", file=sys.stderr)
        return _empty_payload(f"שגיאת קריאה מה-DB: {exc}")
    finally:
        con.close()
