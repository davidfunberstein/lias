"""BDR Batch Mode — automatically iterates through all selected cases.

Two-phase architecture:
  PHASE 1 — Discovery (once, before any downloads):
    • Select "הכל" + "אתר"
    • Expand every selected DXGroupRow via aspxGVExpandRow() JS call
    • Extract ALL sub-case rows via JavaScript (text, onclick, open_date,
      court, close_date, future_hearing, last_activity)
    • Write ALL sub-cases to batch_progress.csv with status "ממתין"
    • Print full list to log

  PHASE 2 — Processing (one sub-case at a time):
    • goto(FilesList) → select "הכל" → expand parent group → call openFileDetails
    • Click Documents tab → sync_and_download_bdr
    • Update batch_progress.csv: download_start / download_end / status / hash
    • Repeat for next sub-case

Folder structure:
  downloads/{client}/{couple}/{case_id} {procedure} - {court}/
    summary.csv          ← document manifest
    sync_history.csv     ← hash history per case
    *.pdf / *.docx
  downloads/{client}/{couple}/
    batch_progress.csv   ← one row per sub-case, updated live
"""

from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.sync_api import Page

from core.download import (
    ROOT_OUTPUT_DIR,
    append_case_to_centralized_log,
    print_terminal_final_dashboard,
)
from core.sync_history import (
    SyncHistory,
    compute_bdr_hash,
    dates_from_bdr_snapshot,
)

if TYPE_CHECKING:
    from core.logger import Logger

BDR_FILES_URL = "https://sides.rbc.gov.il/Pages/FilesList.aspx"
GRID_ID = (
    "ctl00_ctl00_ContentPlaceHolder_PH_GridData_"
    "ASPxRoundPanel2_grdFilesView"
)

SEL_DROPDOWN_BTN = (
    "#ctl00_ctl00_ContentPlaceHolder_PH_GridData_"
    "ASPxRoundPanel1_cmbFileState_B-1"
)
SEL_OPTION_HAKOL = (
    "#ctl00_ctl00_ContentPlaceHolder_PH_GridData_"
    "ASPxRoundPanel1_cmbFileState_DDD_L_LBI0T0"
)
SEL_SEARCH_BTN = (
    "#ctl00_ctl00_ContentPlaceHolder_PH_GridData_"
    "ASPxRoundPanel1_btnFindUserFiles_B"
)

# ── JavaScript helpers ────────────────────────────────────────────────────────

_JS_EXTRACT_GROUP_ROWS = """
() => {
    const rows = document.querySelectorAll('tr[id*="DXGroupRow"]');
    return Array.from(rows).map(row => {
        const m = row.id.match(/DXGroupRow(?:Exp)?(\\d+)$/);
        const td = Array.from(row.querySelectorAll('td.dxgv')).pop();
        return {
            row_index: m ? parseInt(m[1]) : -1,
            text: td ? td.innerText.trim() : '',
            is_expanded: row.id.includes('Exp')
        };
    }).filter(r => r.row_index >= 0);
}
"""

_JS_EXTRACT_DATA_ROWS = """
() => {
    // Locate columns by HEADER TEXT, not by a fixed position. Open/closed is
    // decided purely by whether a close date exists, and that was read from a
    // hard-coded cells[5]. One shifted column in the grid and a CLOSED case
    // reported as open — which is exactly what users saw. Positions stay as a
    // fallback for when the headers cannot be read.
    const headerCells = Array.from(
        document.querySelectorAll('td.dxgHEC, th.dxgHEC, td[id*="_col"]'));
    const headers = headerCells.map(h => (h.innerText || '').replace(/\\u00a0/g,'').trim());
    const findCol = (names, fallback) => {
        for (const n of names) {
            const i = headers.findIndex(h => h && h.indexOf(n) !== -1);
            if (i !== -1) return i;
        }
        return fallback;
    };
    const iOpen  = findCol(['תאריך פתיחה'], 2);
    const iCourt = findCol(['בית דין', 'ערכאה'], 3);
    const iNext  = findCol(['דיון'], 4);
    const iClose = findCol(['תאריך סגירה', 'סגירה'], 5);
    const iLast  = findCol(['פעילות'], 7);

    // A close date must actually look like a date. A stray "-" or a label in
    // that cell used to count as "closed"; anything unparseable now reads as
    // empty, and the row stays open.
    const asDate = v => /\\d{1,2}[\\/.\\-]\\d{1,2}[\\/.\\-]\\d{2,4}/.test(v || '') ? v : '';

    const rows = document.querySelectorAll('tr[id*="DXDataRow"]');
    return Array.from(rows).map(row => {
        const linkEl = row.querySelector('td[id$="_0"] a');
        if (!linkEl) return null;
        const cells = Array.from(row.querySelectorAll('td.dxgv'));
        const cell = i => (cells[i] ? cells[i].innerText.split('\\n')[0].replace(/\\u00a0/g,'').trim() : '');
        return {
            text: linkEl.textContent.trim(),
            onclick: linkEl.getAttribute('onclick') || '',
            open_date: cell(iOpen),
            court: cell(iCourt),
            future_hearing: cell(iNext),
            close_date: asDate(cell(iClose)),
            last_activity: cell(iLast),
            _cols: {open: iOpen, court: iCourt, close: iClose}  // for the log
        };
    }).filter(Boolean).filter(r => r.text);
}
"""

# ── CSV columns ───────────────────────────────────────────────────────────────

BATCH_CSV_COLS = [
    "מזהה תיק",           # "1355021/2"
    "שם תיק",             # "1355021/2, החזקת ילדים – הסדרי שהות, ..."
    "הליך",
    "בית דין",
    "תאריך פתיחה",
    "תאריך סגירה",
    "דיון עתידי",
    "פעילות אחרונה",
    "זמן סשן",            # session start (same for all in this run)
    "שעת התחלת הורדה",
    "שעת סיום הורדה",
    "סטטוס",              # ממתין / בעיבוד / הצלחה / נכשל / דולג
    "קבצים סהכ",
    "הורדו",
    "נכשלו",
    "מסמך ראשון",
    "מסמך אחרון",
    "חתימת פורטל",
    "חתימה קודמת",
    "שינוי חתימה",
    "הערה",
    "תיקייה",
]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BdrCase:
    """One DXGroupRow (top-level case group) from the list page."""
    raw_text: str
    row_index: int          # actual DOM index (e.g. 0, 10, 22, 23)
    case_number: str = ""
    procedure: str = ""
    parties: list[str] = field(default_factory=list)
    couple_key: str = ""
    is_expanded: bool = False


@dataclass
class SubCase:
    """One DXDataRow (sub-case) inside an expanded DXGroupRow."""
    text: str               # "1355021/2, החזקת ילדים – הסדרי שהות, ..."
    open_call: str          # raw onclick attr, HTML entities still encoded
    parent_case_number: str
    sub_id: str             # "1355021/2"
    procedure: str          # "החזקת ילדים – הסדרי שהות"
    court: str              # "פתח תקוה" / "גדול"
    open_date: str
    close_date: str
    future_hearing: str
    last_activity: str
    group_row_index: int    # DOM index of the parent DXGroupRow


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_group_row(info: dict) -> BdrCase:
    text = info.get("text", "")
    row_index = info.get("row_index", -1)
    is_expanded = info.get("is_expanded", False)

    case = BdrCase(raw_text=text, row_index=row_index, is_expanded=is_expanded)
    clean = re.split(r",?\s*תאריך פתיחה", text)[0]
    clean = re.sub(r"^\s*:\s*", "", clean).strip()
    parts = [p.strip() for p in clean.split(",") if p.strip()]
    if not parts:
        return case
    if parts[0].isdigit():
        case.case_number = parts[0]
        parts = parts[1:]
    if parts:
        case.procedure = parts[0]
        parts = parts[1:]
    case.parties = parts
    case.couple_key = " × ".join(sorted(p.strip() for p in case.parties if p.strip()))
    return case


def _parse_sub_case(info: dict, parent: BdrCase) -> SubCase:
    text = info.get("text", "")
    parts = [p.strip() for p in text.split(",")]
    sub_id = parts[0] if parts else text[:20]  # "1355021/2"
    procedure = parts[1] if len(parts) > 1 else ""
    # Decode HTML entities in onclick
    open_call = info.get("onclick", "").replace("&quot;", '"').replace("&amp;", "&")
    return SubCase(
        text=text,
        open_call=open_call,
        parent_case_number=parent.case_number,
        sub_id=sub_id,
        procedure=procedure,
        court=info.get("court", ""),
        open_date=info.get("open_date", ""),
        close_date=info.get("close_date", ""),
        future_hearing=info.get("future_hearing", ""),
        last_activity=info.get("last_activity", ""),
        group_row_index=parent.row_index,
    )


def _group_cases(cases: list[BdrCase]) -> dict[str, list[BdrCase]]:
    groups: dict[str, list[BdrCase]] = {}
    for c in cases:
        key = c.couple_key or c.procedure or "אחר"
        groups.setdefault(key, []).append(c)
    return groups


# ── BatchProgress ─────────────────────────────────────────────────────────────

class BatchProgress:
    """Manages batch_progress.csv — one file per session or per couple dir."""

    def __init__(self, dir_: Path, session_start: str,
                 logger: "Logger | None" = None,
                 label: str = "",
                 force_rerun: bool = False) -> None:
        self._force_rerun = force_rerun
        if label:
            safe_label = label[:60].replace("/", "-").replace("\\", "-")
            self.path = dir_ / f"batch_progress — {safe_label}.csv"
        else:
            folder_label = dir_.name[:40].replace("/", "-").replace("\\", "-")
            self.path = dir_ / f"batch_progress — {folder_label}.csv"
        self.session_start = session_start
        self.logger = logger
        self._rows: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = row.get("מזהה תיק", "")
                    if key:
                        self._rows[key] = dict(row)
        except Exception as e:
            if self.logger:
                self.logger.warn(f"[BatchProgress] Could not load existing CSV: {e}")
        # Reset stale "בעיבוד" rows — they were interrupted mid-run
        # If force_rerun, also reset completed rows so everything re-downloads
        reset_statuses = {"בעיבוד", "הצלחה", "הצלחה חלקית"} if self._force_rerun else {"בעיבוד"}
        for key, row in self._rows.items():
            if row.get("סטטוס") in reset_statuses:
                row["סטטוס"] = "ממתין"
                row["שעת התחלת הורדה"] = ""
                row["שעת סיום הורדה"] = ""

    def get_previous_hash(self, sub_id: str) -> str:
        return self._rows.get(sub_id, {}).get("חתימת פורטל", "")

    def register_pending(self, sc: SubCase) -> None:
        """Write this sub-case to CSV with status 'ממתין' (if not already done)."""
        existing = self._rows.get(sc.sub_id, {})
        # Don't overwrite a completed row from a previous session
        if existing.get("סטטוס") in ("הצלחה", "הצלחה חלקית"):
            return
        self._rows[sc.sub_id] = {
            "מזהה תיק": sc.sub_id,
            "שם תיק": sc.text,
            "הליך": sc.procedure,
            "בית דין": sc.court,
            "תאריך פתיחה": sc.open_date,
            "תאריך סגירה": sc.close_date,
            "דיון עתידי": sc.future_hearing,
            "פעילות אחרונה": sc.last_activity,
            "זמן סשן": self.session_start,
            "שעת התחלת הורדה": "",
            "שעת סיום הורדה": "",
            "סטטוס": existing.get("סטטוס", "ממתין"),
            "קבצים סהכ": existing.get("קבצים סהכ", ""),
            "הורדו": existing.get("הורדו", ""),
            "נכשלו": existing.get("נכשלו", ""),
            "מסמך ראשון": existing.get("מסמך ראשון", ""),
            "מסמך אחרון": existing.get("מסמך אחרון", ""),
            "חתימת פורטל": existing.get("חתימת פורטל", ""),
            "חתימה קודמת": existing.get("חתימה קודמת", ""),
            "שינוי חתימה": existing.get("שינוי חתימה", ""),
            "הערה": existing.get("הערה", ""),
            "תיקייה": existing.get("תיקייה", ""),
        }
        self._save()

    def mark_started(self, sub_id: str) -> None:
        row = self._rows.get(sub_id, {})
        row["סטטוס"] = "בעיבוד"
        row["שעת התחלת הורדה"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row["זמן סשן"] = self.session_start
        self._rows[sub_id] = row
        self._save()

    def mark_done(
        self,
        sub_id: str,
        total: int,
        downloaded: int,
        failed: int,
        first_date: str,
        last_date: str,
        portal_hash: str,
        note: str = "",
        case_dir: "Path | None" = None,
    ) -> None:
        row = self._rows.get(sub_id, {})
        prev_hash = row.get("חתימת פורטל", "")
        if not prev_hash:
            hash_changed = "ראשון"
        elif prev_hash != portal_hash:
            hash_changed = "כן"
        else:
            hash_changed = "לא"
        pre_existing = max(0, total - downloaded - failed)
        note_parts = [note] if note else []
        if pre_existing > 0:
            note_parts.append(f"{pre_existing} קבצים כבר היו קיימים")
        combined_note = " | ".join(note_parts)
        update_dict = {
            "שעת סיום הורדה": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "סטטוס": "הצלחה" if failed == 0 else f"חלקי ({failed} נכשלו)",
            "קבצים סהכ": str(total),
            "הורדו": str(downloaded),
            "נכשלו": str(failed),
            "מסמך ראשון": first_date,
            "מסמך אחרון": last_date,
            "חתימה קודמת": prev_hash,
            "חתימת פורטל": portal_hash,
            "שינוי חתימה": hash_changed,
            "הערה": combined_note,
        }
        if case_dir is not None:
            update_dict["תיקייה"] = str(case_dir)
        row.update(update_dict)
        self._rows[sub_id] = row
        self._save()

    def mark_skipped(self, sub_id: str, reason: str) -> None:
        row = self._rows.get(sub_id, {})
        row.update({
            "שעת סיום הורדה": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "סטטוס": "דולג",
            "הערה": reason,
        })
        self._rows[sub_id] = row
        self._save()

    def _save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=BATCH_CSV_COLS, extrasaction="ignore"
                )
                writer.writeheader()
                writer.writerows(self._rows.values())
        except Exception as e:
            if self.logger:
                self.logger.warn(f"[BatchProgress] Save failed: {e}")


# ── User-selection helpers ────────────────────────────────────────────────────

def _ask_which_groups(groups: dict[str, list[BdrCase]]) -> list[str]:
    keys = list(groups.keys())
    print("\n" + "=" * 60)
    print("תיקים שנמצאו — קבוצות לפי צדדים:")
    print("=" * 60)
    for idx, key in enumerate(keys, 1):
        group = groups[key]
        nums = ", ".join(c.case_number for c in group)
        procs = ", ".join(sorted({c.procedure for c in group}))
        print(f"  {idx}. {key}")
        print(f"     תיקים: {nums}  |  סוג: {procs}")
    print("=" * 60)
    # LIAS mode: auto-select all without prompting
    try:
        from core.download import SESSION_SETTINGS as _ss
        if _ss.get("lias_mode"):
            print("[LIAS] בחירה אוטומטית: כל הקבוצות")
            return keys
    except Exception:
        pass
    print("הזן מספרי קבוצות (לדוגמה: 1,3), או a / הכל לכולם:")
    raw = input(">>> ").strip()
    if raw.lower() in ("הכל", "all", "a", "*", ""):
        return keys
    selected = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            i = int(tok) - 1
            if 0 <= i < len(keys):
                selected.append(keys[i])
    return selected


def _ask_client_dir(parties: list[str], downloads_base: Path,
                    logger: "Logger | None" = None) -> Path:
    for party in parties:
        words = [w for w in re.findall(r"\w+", party) if len(w) > 1]
        for existing in downloads_base.iterdir():
            if existing.is_dir():
                clean = existing.name.replace("_", " ")
                if all(w in clean for w in words):
                    print(f"[Smart Path] תיקיית לקוח קיימת: '{existing.name}'")
                    if logger:
                        logger.info(f"[BDR Batch] Client dir: {existing}")
                    return existing
    # LIAS mode: auto-pick first party
    try:
        from core.download import SESSION_SETTINGS as _ss
        if _ss.get("lias_mode"):
            chosen = parties[0]
            d = downloads_base / chosen
            d.mkdir(parents=True, exist_ok=True)
            print(f"[LIAS] תיקיית לקוח אוטומטית: '{chosen}'")
            return d
    except Exception:
        pass
    print("\nאיזה צד הוא הלקוח שלך?")
    for idx, p in enumerate(parties, 1):
        print(f"  {idx}. {p}")
    while True:
        sel = input(f">>> (1-{len(parties)}): ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(parties):
            chosen = parties[int(sel) - 1]
            d = downloads_base / chosen
            d.mkdir(parents=True, exist_ok=True)
            return d
        print("בחירה לא תקינה.")


# ── Main runner ───────────────────────────────────────────────────────────────

class BdrBatchRunner:

    def __init__(self, page: Page, logger: "Logger | None" = None,
                 on_case_done: "Callable[[Path, str], None] | None" = None,
                 progress_cb: "Callable[[dict], None] | None" = None,
                 should_cancel: "Callable[[str | None], bool] | None" = None) -> None:
        self.page = page
        self.logger = logger
        self._on_case_done = on_case_done
        self._progress_cb = progress_cb
        self._should_cancel = should_cancel
        self._case_data: dict[str, "BdrCase"] = {}
        self._sub_cases: dict[str, "SubCase"] = {}
        self._case_dirs: dict[str, Path] = {}
        self._stats = {"done": 0, "total": 0, "failed": 0, "skipped": 0,
                       "docs_downloaded": 0, "current_case": "", "current_name": ""}

    def _log(self, msg: str, level: str = "info") -> None:
        prefixed = f"[BDR Batch] {msg}"
        if self.logger:
            getattr(self.logger, level)(prefixed)
        else:
            print(prefixed)

    def _fire_progress(self) -> None:
        if self._progress_cb:
            try:
                self._progress_cb(dict(self._stats))
            except Exception:
                pass

    def _set_case_status(self, sub_id: str, status: str) -> None:
        for c in self._stats.get("cases_detail", []):
            if c["id"] == sub_id:
                c["status"] = status
                break

    # ── Public entry ──────────────────────────────────────────────────────────

    def run(self, session_settings: dict, root_output_dir: Path) -> None:
        session_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Apply client folder if inferred
        client_name = session_settings.get("client_name", "")
        if client_name:
            effective_root = root_output_dir / client_name
            effective_root.mkdir(parents=True, exist_ok=True)
            self._log(f"[ClientInference] הורדות יכנסו תחת: {effective_root}")
        else:
            effective_root = root_output_dir

        downloads_base = effective_root / "downloads"
        downloads_base.mkdir(parents=True, exist_ok=True)

        # ── Step 1: select "הכל" + "אתר" ─────────────────────────────
        self._select_all_cases()
        try:
            self.page.wait_for_selector("tr[id*='DXGroupRow']", timeout=20000)
        except Exception:
            self._log("לא נמצאו שורות תיקים — האם המשתמש מחובר?", "error")
            return
        time.sleep(1)

        # ── Step 2: extract group rows via JS ─────────────────────────
        raw_groups = self.page.evaluate(_JS_EXTRACT_GROUP_ROWS)
        group_cases = [_parse_group_row(r) for r in raw_groups if r["row_index"] >= 0]
        if not group_cases:
            self._log("לא הצלחתי לחלץ תיקים.", "error")
            return
        self._log(f"נמצאו {len(group_cases)} קבוצות-תיקים.")

        # ── Step 3: ask user (or auto-select all in lawyer mode) ─────────
        groups = _group_cases(group_cases)
        lawyer_mode = session_settings.get("user_mode") in ("lawyer", "pleader")

        # Optional client-name filter (from UI search) — keep only groups whose
        # party names contain the filter string.
        # Explicit selection from the UI picker: whole cases and/or specific
        # sub-cases. Whole-case numbers filter the groups; sub-case ids are
        # kept for a second filter after discovery.
        want_cases = {str(c).strip() for c in (session_settings.get("cases") or []) if str(c).strip()}
        self._want_sub_ids = {str(s).strip() for s in (session_settings.get("sub_cases") or []) if str(s).strip()}
        if want_cases:
            filtered = {k: g for k, g in groups.items()
                        if any(c.case_number in want_cases for c in g)}
            self._log(f"בחירת תיקים מה-UI: {len(filtered)}/{len(groups)} קבוצות "
                      f"({len(want_cases)} תיקים נבחרו).")
            groups = filtered
        elif self._want_sub_ids:
            # only sub-cases were picked — keep groups that own them
            filtered = {k: g for k, g in groups.items()
                        if any(any(sid.startswith(c.case_number) for sid in self._want_sub_ids)
                               for c in g)}
            self._log(f"בחירת תת-תיקים מה-UI: {len(filtered)}/{len(groups)} קבוצות.")
            groups = filtered

        client_filter = (session_settings.get("client_filter") or "").strip()
        if client_filter:
            filtered = {
                k: g for k, g in groups.items()
                if any(client_filter in p for c in g for p in c.parties)
            }
            self._log(f"סינון לפי לקוח '{client_filter}': {len(filtered)}/{len(groups)} קבוצות.")
            groups = filtered
            if not groups:
                self._log("אף תיק לא תואם לשם הלקוח — יוצא.", "warn")
                return
        if not groups:
            self._log("אין תיקים תואמים לבחירה — יוצא.", "warn")
            return

        if lawyer_mode:
            selected_keys = list(groups.keys())
            self._log(f"מצב עורך דין — נבחרו כל {len(selected_keys)} הקבוצות אוטומטית.")
        else:
            selected_keys = _ask_which_groups(groups)
            if not selected_keys:
                self._log("לא נבחרו תיקים — יוצא.")
                return

        # ── Step 4: resolve directories ───────────────────────────────
        couple_dir_map: dict[str, Path] = {}

        if lawyer_mode:
            # Lawyer mode — new folder structure rules:
            #   With client_name:
            #     downloads/{client_name}/{case_number} - {court}/{sub_id} {proc} - {court}/
            #   Without client_name, 1 parent case in group:
            #     downloads/{case_number} - {court}/{sub_id} {proc} - {court}/
            #   Without client_name, 2+ parent cases in group:
            #     downloads/{procedure} - {parties}/{case_number} - {court}/{sub_id} {proc} - {court}/
            # A client filter implies the client's folder name too
            client_name = session_settings.get("client_name", "") or client_filter

            for key in selected_keys:
                group = groups[key]
                first = group[0]

                if client_name:
                    # All parent cases directly under the client folder
                    client_dir = downloads_base / re.sub(r'[\\/*?:"<>|]', "-", client_name)
                    client_dir.mkdir(parents=True, exist_ok=True)
                    for c in group:
                        couple_dir_map[c.case_number] = client_dir
                        self._case_data[c.case_number] = c
                else:
                    procedure = first.procedure or "תיק"
                    parties_str = " - ".join(p.strip() for p in first.parties if p.strip())
                    group_name = f"{procedure} - {parties_str}"
                    group_name_safe = re.sub(r'[\\/*?:"<>|]', "-", group_name)

                    if len(group) > 1:
                        # Multiple parent cases → group folder
                        group_dir = downloads_base / group_name_safe
                    else:
                        # Single parent case → no group folder
                        group_dir = downloads_base

                    group_dir.mkdir(parents=True, exist_ok=True)
                    for c in group:
                        couple_dir_map[c.case_number] = group_dir
                        self._case_data[c.case_number] = c

            self._log(f"[BDR Batch] תיקיות קבוצה: {[str(d) for d in set(couple_dir_map.values())]}")
        else:
            first_parties = groups[selected_keys[0]][0].parties
            client_dir = _ask_client_dir(first_parties, downloads_base, self.logger)
            self._log(f"תיקיית לקוח: {client_dir}")

            for key in selected_keys:
                group = groups[key]
                parties = group[0].parties
                couple_name = (
                    f"{parties[0]} - {parties[1]}" if len(parties) >= 2
                    else parties[0] if parties
                    else "לא_ידוע"
                )
                couple_dir = client_dir / re.sub(r'[\\/*?:"<>|]', "-", couple_name).strip()
                couple_dir.mkdir(parents=True, exist_ok=True)
                for c in group:
                    couple_dir_map[c.case_number] = couple_dir

        # ── Step 5: DISCOVERY — expand and collect all sub-cases ──────
        selected_cases = [c for k in selected_keys for c in groups[k]]
        all_sub_cases = self._discover_all_sub_cases(selected_cases)

        # Specific sub-cases picked in the UI → download only those. Whole
        # cases picked (want_cases) already brought ALL their sub-cases here,
        # so only narrow when a sub-case selection exists WITHOUT its parent.
        want_sub = getattr(self, "_want_sub_ids", set())
        if want_sub:
            keep = [sc for sc in all_sub_cases
                    if sc.sub_id in want_sub or sc.parent_case_number in want_cases]
            if keep:
                self._log(f"בחירת תת-תיקים: {len(keep)}/{len(all_sub_cases)} יורדו.")
                all_sub_cases = keep

        if not all_sub_cases:
            self._log("לא נמצאו תת-תיקים אחרי פתיחת הקבוצות.", "error")
            return

        # ── Step 6: print full list + write "ממתין" to CSV ────────────
        # Single session-level BatchProgress at downloads_base
        # Label: first group's parties or "אצווה"
        first_key = selected_keys[0]
        first_group = groups[first_key]
        first_parties = first_group[0].parties
        if first_parties:
            session_label = " - ".join(p.strip() for p in first_parties[:2] if p.strip())
        else:
            session_label = first_group[0].couple_key or "אצווה"
        force_rerun = session_settings.get("force_rerun", False)
        session_progress = BatchProgress(
            downloads_base, session_start, self.logger, label=session_label,
            force_rerun=force_rerun,
        )

        # Determine couple dir for each sub-case (for navigation only — not for CSV splitting)
        sc_couple: dict[str, Path] = {}
        for sc in all_sub_cases:
            cd = couple_dir_map.get(sc.parent_case_number)
            if cd:
                sc_couple[sc.sub_id] = cd

        # Register all sub-cases as pending in the single session CSV
        for sc in all_sub_cases:
            session_progress.register_pending(sc)

        print("\n" + "=" * 70)
        print(f"[BDR Batch] {len(all_sub_cases)} תת-תיקים לעיבוד:")
        _hdr_bd = 'ב"ד'
        print(f"{'מזהה':<18} {'הליך':<30} {_hdr_bd:<12} {'פתיחה':<12} {'פעילות אחרונה'}")
        print("-" * 70)
        for sc in all_sub_cases:
            print(
                f"  {sc.sub_id:<16} {sc.procedure[:28]:<30} {sc.court:<12} "
                f"{sc.open_date:<12} {sc.last_activity}"
            )
        print("=" * 70 + "\n")
        if self.logger:
            self.logger.info(
                f"[BDR Batch] Sub-cases to process: "
                f"{[s.sub_id for s in all_sub_cases]}"
            )

        # ── Step 7: process each sub-case ─────────────────────────────
        self._goto_files_list()

        # Register atexit handler so we flush status even if terminal closes
        import atexit, signal

        def _emergency_flush():
            """Mark any still-in-progress cases as interrupted."""
            for _sc in all_sub_cases:
                _row = session_progress._rows.get(_sc.sub_id, {})
                if _row.get("סטטוס") == "בעיבוד":
                    _row["סטטוס"] = "נקטע"
                    _row["הערה"] = (_row.get("הערה", "") + " | הופסק על ידי סגירת מסוף").strip(" | ")
                    session_progress._rows[_sc.sub_id] = _row
            try:
                session_progress._save()
            except Exception:
                pass

        atexit.register(_emergency_flush)
        def _sigterm_handler(*_):
            _emergency_flush()

        try:
            signal.signal(signal.SIGTERM, _sigterm_handler)
        except (OSError, ValueError):
            pass  # SIGTERM may not be settable on all platforms

        self._stats["total"] = len(all_sub_cases)
        self._stats["cases_detail"] = [
            {"id": sc.sub_id, "name": sc.procedure, "court": sc.court,
             "status": "pending"} for sc in all_sub_cases]
        self._fire_progress()

        try:
            for idx, sc in enumerate(all_sub_cases):
                if self._should_cancel and self._should_cancel(None):
                    self._log("הופסק על ידי המשתמש")
                    break
                if self._should_cancel and self._should_cancel(sc.sub_id):
                    self._log(f"תיק {sc.sub_id} נדלג לבקשת המשתמש")
                    self._stats["skipped"] += 1
                    self._set_case_status(sc.sub_id, "skipped")
                    continue
                prev_status = session_progress._rows.get(sc.sub_id, {}).get("סטטוס", "")
                if prev_status in ("הצלחה", "הצלחה חלקית"):
                    self._log(f"דולג (כבר הושלם): {sc.sub_id}")
                    self._stats["skipped"] += 1
                    self._set_case_status(sc.sub_id, "done")
                    continue
                cd = sc_couple.get(sc.sub_id)
                if not cd:
                    self._log(f"אין תיקיית זוג עבור {sc.sub_id} — מדלג.", "warn")
                    self._stats["skipped"] += 1
                    self._set_case_status(sc.sub_id, "skipped")
                    continue
                self._stats["current_case"] = sc.sub_id
                self._stats["current_name"] = sc.procedure
                self._set_case_status(sc.sub_id, "downloading")
                self._fire_progress()
                self._process_sub_case(
                    sc, cd, session_settings, session_progress,
                    root_output_dir=root_output_dir,
                )
        except KeyboardInterrupt:
            _emergency_flush()
            self._log("הופסק על ידי המשתמש (Ctrl+C).", "warn")
        finally:
            atexit.unregister(_emergency_flush)

        self._log("מצב אצווה הסתיים.")
        print("\n[BDR Batch] כל התיקים שנבחרו סונכרנו.")
        self._write_global_summary(root_output_dir, session_settings)

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _find_current_row_index(self, case_number: str) -> int | None:
        """
        Re-query the live DOM and return the CURRENT row_index for the group
        row whose text contains *case_number*.  Must be called right before
        expanding so the index reflects any DOM shifts caused by prior expansions.
        """
        live = self.page.evaluate(_JS_EXTRACT_GROUP_ROWS)
        for g in live:
            if case_number in g.get("text", ""):
                return g["row_index"]
        return None

    def _discover_all_sub_cases(self, selected_cases: list[BdrCase]) -> list[SubCase]:
        """Expand each selected group row and collect sub-cases via JS.

        IMPORTANT: after expanding group N its data-rows shift all subsequent
        group-row DOM indices.  We re-query the live DOM index each time
        so we always call aspxGVExpandRow with the *current* index.
        """
        all_sub: list[SubCase] = []

        for bdr_case in selected_cases:
            # Re-read live index — may have changed after previous group expanded
            live_index = self._find_current_row_index(bdr_case.case_number)
            if live_index is None:
                self._log(
                    f"  לא נמצאה שורה עבור {bdr_case.case_number} בDOM — מדלג.",
                    "warn",
                )
                continue
            # Update the stored index so _process_sub_case can use it too
            bdr_case.row_index = live_index
            self._log(
                f"מרחיב תיק {bdr_case.case_number} (row_index={live_index})..."
            )
            if not self._expand_group_js(live_index):
                self._log("  הרחבה נכשלה — מדלג.", "warn")
                continue

            # Extract only data rows that belong to this group (by case number prefix)
            rows_info: list[dict] = self.page.evaluate(_JS_EXTRACT_DATA_ROWS)
            group_rows = [r for r in rows_info if r["text"].startswith(bdr_case.case_number)]

            self._log(
                f"  {bdr_case.case_number}: נמצאו {len(group_rows)} תת-תיקים."
            )
            for r in group_rows:
                sc = _parse_sub_case(r, bdr_case)
                self._log(
                    f"    • {sc.sub_id} | {sc.procedure} | {sc.court} | "
                    f"פתיחה: {sc.open_date} | פעילות: {sc.last_activity}"
                )
                all_sub.append(sc)

        return all_sub

    # ── Navigation ────────────────────────────────────────────────────────────

    def _select_status_and_search(self, option_index: int, label: str) -> bool:
        """Pick status option #option_index from the dropdown, click 'אתר',
        and return True only if group rows are actually visible afterwards."""
        for _attempt in (1, 2):
            try:
                self._log(f"בוחר '{label}' מהתפריט...")
                btn = self.page.locator(SEL_DROPDOWN_BTN)
                btn.wait_for(timeout=10000)
                btn.click(force=True)
                time.sleep(1)
                opt_sel = SEL_OPTION_HAKOL.replace("LBI0T0", f"LBI{option_index}T0")
                self.page.locator(opt_sel).click(force=True)
                time.sleep(0.5)
                break
            except Exception as e:
                self._log(f"שגיאה בתפריט ({label}): {e}", "warn")
                if _attempt == 1:
                    self._log("קוטע ניווט תקוע וחוזר לרשימת התיקים...", "warn")
                    try:
                        self.page.goto(BDR_FILES_URL,
                                       wait_until="domcontentloaded", timeout=20000)
                        time.sleep(2)
                    except Exception as e2:
                        self._log(f"ניווט חזרה נכשל: {e2}", "warn")
        try:
            self._log("לוחץ 'אתר'...")
            self.page.locator(SEL_SEARCH_BTN).click(force=True)
            time.sleep(4)
        except Exception as e:
            self._log(f"שגיאה ב'אתר': {e}", "error")
        try:
            self.page.wait_for_selector("tr[id*='DXGroupRow']", timeout=15000)
            return True
        except Exception:
            return False

    def _select_all_cases(self) -> None:
        """Level 1: 'הכל' + 'אתר'. Level 2 fallback: the portal occasionally
        returns an empty grid for 'הכל' — walk the individual status options
        (פתוח/סגור וכו') until one of them yields rows."""
        if self._select_status_and_search(0, "הכל"):
            return
        self._log("'הכל' לא החזיר תיקים — דרגה 2: מנסה סטטוסים בודדים.", "warn")
        for idx in range(1, 5):
            if self._select_status_and_search(idx, f"סטטוס #{idx}"):
                return
        self._log("אף סטטוס לא החזיר תיקים.", "error")

    def _expand_group_js(self, row_index: int) -> bool:
        """Expand a DXGroupRow by calling aspxGVExpandRow() via JS."""
        try:
            # Check if already expanded
            raw_groups = self.page.evaluate(_JS_EXTRACT_GROUP_ROWS)
            for g in raw_groups:
                if g["row_index"] == row_index and g["is_expanded"]:
                    self._log(f"  שורה {row_index} כבר מורחבת.")
                    return True

            self.page.evaluate(f"aspxGVExpandRow('{GRID_ID}', {row_index})")
            # Wait for DXDataRow to appear
            self.page.wait_for_selector(
                f"tr[id*='DXDataRow'][id*='{GRID_ID}'], tr[id*='DXDataRow']",
                timeout=10000
            )
            time.sleep(1.5)
            return True
        except Exception as e:
            self._log(f"  שגיאה בהרחבה של שורה {row_index}: {e}", "warn")
            return False

    def _goto_files_list(self) -> None:
        self._log("מנווט לרשימת התיקים...")
        self.page.goto(BDR_FILES_URL, wait_until="domcontentloaded")
        time.sleep(2)

    def _open_sub_case(self, sc: SubCase) -> bool:
        """Navigate into a sub-case using its openFileDetails JS call."""
        try:
            call_match = re.search(r"(openFileDetails\([^)]+\))", sc.open_call)
            if call_match:
                self._log(f"  JS navigate: {sc.sub_id}...")
                self.page.evaluate(call_match.group(1))
                self.page.wait_for_load_state("domcontentloaded")
                time.sleep(3)
                return True
            # Fallback: click the link by text
            link = self.page.locator(f"a:text-is('{sc.sub_id},')").first
            if link.count() == 0:
                link = self.page.get_by_text(sc.sub_id, exact=False).first
            link.click(force=True)
            self.page.wait_for_load_state("domcontentloaded")
            time.sleep(3)
            return True
        except Exception as e:
            self._log(f"  ניווט נכשל ({sc.sub_id}): {e}", "error")
            return False

    # ── Core: process one sub-case ────────────────────────────────────────────

    def _process_sub_case(
        self,
        sc: SubCase,
        couple_dir: Path,
        session_settings: dict,
        progress: BatchProgress,
        root_output_dir: Path | None = None,
    ) -> None:
        from core.bdr_navigation import BdrNavigator

        print(f"\n{'=' * 65}")
        print(f"[BDR Batch] עיבוד: {sc.sub_id} — {sc.procedure} ({sc.court})")
        print(f"{'=' * 65}")
        self._log(f"מתחיל עיבוד: {sc.text}")

        # Build folder name using data already from discovery (no extra navigation)
        court_safe = re.sub(r'[\\/*?:"<>|]', "-", sc.court).strip()
        proc_safe = re.sub(r'[\\/*?:"<>|/]', "-", sc.procedure).strip()
        case_num_safe = sc.sub_id.replace("/", "-")
        if court_safe:
            sub_folder = f"{case_num_safe} {proc_safe} - {court_safe}"
        else:
            sub_folder = f"{case_num_safe} {proc_safe}"
        sub_folder = re.sub(r"\s+", " ", sub_folder).strip()

        if session_settings.get("user_mode") == "lawyer":
            # Lawyer mode — 3-level:
            # couple_dir = group_dir (or client_dir or downloads_base)
            # parent_dir = group_dir/{case_number} - {court}/
            # case_dir   = parent_dir/{sub_id} {proc} - {court}/
            group_dir = couple_dir  # passed in as the group-level dir
            # Parent folder: {case_number} - {court}  (no parties — already in group/client folder)
            parent_name = sc.parent_case_number.replace("/", "-")
            if court_safe:
                parent_name = f"{parent_name} - {court_safe}"
            parent_name = re.sub(r'[\\/*?:"<>|]', "-", parent_name).strip()
            parent_dir = group_dir / parent_name
            parent_dir.mkdir(parents=True, exist_ok=True)
            case_dir = parent_dir / sub_folder
        else:
            # Private mode: existing behavior — nest under couple_dir
            case_dir = couple_dir / sub_folder
        case_dir.mkdir(parents=True, exist_ok=True)
        self._log(f"  תיקייה: {case_dir}")
        self._case_dirs[sc.sub_id] = case_dir
        self._sub_cases[sc.sub_id] = sc

        progress.mark_started(sc.sub_id)

        # ── Browser recovery: check and restore connection if needed ──────────
        from core.connection import is_page_alive, recover_browser_session
        if not is_page_alive(self.page):
            self._log("חיבור לדפדפן אבד — מנסה לשחזר...", "warn")
            session_settings_ref = session_settings if session_settings else {}
            self.page = recover_browser_session(
                self.page, "BDR",
                session_settings=session_settings_ref,
                logger=self.logger,
            )

        # Navigate: FilesList → select "הכל" → expand parent → open sub-case
        try:
            self._goto_files_list()
        except Exception as nav_exc:
            self._log(f"ניווט לרשימה נכשל ({nav_exc}) — מנסה שחזור...", "warn")
            self.page = recover_browser_session(
                self.page, "BDR", session_settings=session_settings, logger=self.logger
            )
            try:
                self._goto_files_list()
            except Exception:
                progress.mark_skipped(sc.sub_id, "ניווט לרשימה נכשל גם אחרי שחזור")
                return

        self._select_all_cases()
        try:
            self.page.wait_for_selector("tr[id*='DXGroupRow']", timeout=15000)
        except Exception:
            pass
        time.sleep(1)

        # Re-query the live DOM index — the page reloaded so groups collapsed
        # and indices reset; sc.group_row_index may be stale from discovery.
        live_index = self._find_current_row_index(sc.parent_case_number)
        if live_index is None:
            live_index = sc.group_row_index  # fallback
            self._log(
                f"  אזהרה: לא נמצא row_index חי ל-{sc.parent_case_number}, "
                f"משתמש בישן ({live_index})",
                "warn",
            )
        if not self._expand_group_js(live_index):
            progress.mark_skipped(sc.sub_id, "הרחבת הקבוצה נכשלה")
            return

        if not self._open_sub_case(sc):
            progress.mark_skipped(sc.sub_id, "ניווט לתת-תיק נכשל")
            return

        # Documents tab
        nav = BdrNavigator(self.page, logger=self.logger)
        nav.click_documents_tab()
        try:
            self.page.wait_for_selector("tr[id*='DXDataRow']", timeout=20000)
            time.sleep(1)
        except Exception:
            progress.mark_skipped(sc.sub_id, "טבלת מסמכים לא נטענה")
            self._log("  טבלת מסמכים לא נטענה — מדלג.", "warn")
            return

        # Build parties string from parent BdrCase
        parent_bdr = self._case_data.get(sc.parent_case_number)
        parties_str = (
            " | ".join(p.strip() for p in parent_bdr.parties if p.strip())
            if parent_bdr else ""
        )

        # Sync
        total, downloaded, re_downloaded, failed, table_updated, snapshot_lines = (
            nav.sync_and_download_bdr(
                case_dir,
                session_settings,
                session_settings.get("run_timestamp", ""),
                parties=parties_str,
            )
        )

        # Hash
        portal_hash = compute_bdr_hash(snapshot_lines)
        first_date, last_date = dates_from_bdr_snapshot(snapshot_lines)
        prev_hash = progress.get_previous_hash(sc.sub_id)
        note = ""
        if prev_hash and prev_hash != portal_hash and len(downloaded) == 0 and len(re_downloaded) == 0:
            note = "⚠️ חתימת פורטל השתנתה ללא הורדות חדשות — ייתכן שמסמך הוסר"
            print(f"\n[WARN] {note}")
            if self.logger:
                self.logger.warn(f"[BDR Batch] {note} — {sc.sub_id}")

        # Write sync_history.csv in case_dir (filename uses sub_folder for clarity)
        case_history = SyncHistory(case_dir, self.logger, label=sub_folder)
        case_history.append(
            portal="BDR",
            total=total,
            new_downloads=len(downloaded),
            re_downloads=len(re_downloaded),
            failed=len(failed),
            first_date=first_date,
            last_date=last_date,
            portal_hash=portal_hash,
            note=note,
        )

        # Update batch_progress.csv
        progress.mark_done(
            sub_id=sc.sub_id,
            total=total,
            downloaded=len(downloaded),
            failed=len(failed),
            first_date=first_date,
            last_date=last_date,
            portal_hash=portal_hash,
            note=note,
            case_dir=case_dir,
        )

        append_case_to_centralized_log(
            str(case_dir), total, downloaded, failed, table_updated, snapshot_lines
        )
        print_terminal_final_dashboard(total, downloaded, failed, table_updated)
        self._log(
            f"  {sc.sub_id} — {len(downloaded)} חדשים, {len(re_downloaded)} מחדש, "
            f"{len(failed)} נכשלו | hash={portal_hash}"
        )

        self._stats["done"] += 1
        self._stats["docs_downloaded"] += len(downloaded) + len(re_downloaded)
        if failed:
            self._stats["failed"] += 1
        self._set_case_status(sc.sub_id, "failed" if failed and not downloaded else "done")
        self._fire_progress()

        if self._on_case_done:
            try:
                self._on_case_done(case_dir, sc.sub_id)
            except Exception as _cb_err:
                self._log(f"  on_case_done callback error: {_cb_err}", "warn")

        # ── Write parent-level sub-case summary ───────────────────────────────
        # e.g. 1355021 - פתח תקוה / parent_summary — 1355021 - פתח תקוה.csv
        parent_dir = case_dir.parent
        self._write_parent_summary(parent_dir, sc, total, len(downloaded), len(failed),
                                   first_date, last_date, portal_hash)

        # ── Update global summary after every completed sub-case ──────────────
        if root_output_dir:
            self._write_global_summary(root_output_dir, session_settings or {})

    def _write_parent_summary(
        self,
        parent_dir: Path,
        sc: "SubCase",
        total: int,
        downloaded: int,
        failed: int,
        first_date: str,
        last_date: str,
        portal_hash: str,
    ) -> None:
        """Write/update a summary CSV at the parent case directory level.

        File: parent_dir/parent_summary — {parent_dir.name}.csv
        One row per sub-case that has been processed under this parent.
        """
        import csv as _csv
        from datetime import datetime as _dt

        label = parent_dir.name[:60].replace("/", "-").replace("\\", "-")
        path = parent_dir / f"parent_summary — {label}.csv"

        COLS = [
            "מזהה תת-תיק",
            "הליך",
            "בית דין",
            "תאריך פתיחה",
            "תאריך סגירה",
            "פעילות אחרונה",
            "קבצים סהכ",
            "הורדו",
            "נכשלו",
            "מסמך ראשון",
            "מסמך אחרון",
            "חתימת פורטל",
            "תיקייה",
            "זמן עדכון",
        ]

        # Load existing rows
        rows: dict[str, dict] = {}
        if path.exists():
            try:
                with path.open(encoding="utf-8-sig") as f:
                    for row in _csv.DictReader(f):
                        sid = row.get("מזהה תת-תיק", "")
                        if sid:
                            rows[sid] = dict(row)
            except Exception:
                pass

        case_dir = self._case_dirs.get(sc.sub_id, "")
        rows[sc.sub_id] = {
            "מזהה תת-תיק": sc.sub_id,
            "הליך": sc.procedure,
            "בית דין": sc.court,
            "תאריך פתיחה": sc.open_date,
            "תאריך סגירה": sc.close_date,
            "פעילות אחרונה": sc.last_activity,
            "קבצים סהכ": str(total),
            "הורדו": str(downloaded),
            "נכשלו": str(failed),
            "מסמך ראשון": first_date,
            "מסמך אחרון": last_date,
            "חתימת פורטל": portal_hash,
            "תיקייה": str(case_dir),
            "זמן עדכון": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = _csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows.values())
        except Exception as exc:
            self._log(f"[ParentSummary] שגיאה בכתיבה ל-{path}: {exc}", "warn")

    def _write_global_summary(self, root_output_dir: Path, session_settings: dict) -> None:
        """Write/update a global all_cases_summary.csv one level above case folders."""
        from datetime import datetime as _dt
        import csv as _csv

        summary_dir = root_output_dir / "downloads"
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_path = summary_dir / "all_cases_summary.csv"

        COLS = [
            "מזהה תיק",         # sub_id
            "שם תיק",           # sc.text
            "הליך",             # sc.procedure
            "בית משפט / בית דין",  # sc.court  — "בית דין" for BDR
            "פורטל",            # "BDR"
            "צדדים",            # parties from BdrCase
            "תיקייה",           # absolute case_dir path
            "תיקיית תיק על",    # parent_case_number
            "גורם שיפוטי",      # judge / panel — empty for now, filled later
            "תאריך פתיחה",
            "תאריך סגירה",
            "פעילות אחרונה",
            "קבצים סהכ",
            "הורדו",
            "נכשלו",
            "מסמך ראשון",
            "מסמך אחרון",
            "חתימת פורטל",
            "זמן סיכום",
        ]

        # Load existing rows keyed by sub_id
        existing: dict[str, dict] = {}
        if summary_path.exists():
            try:
                with summary_path.open(encoding="utf-8-sig") as f:
                    for row in _csv.DictReader(f):
                        sid = row.get("מזהה תיק", "")
                        if sid:
                            existing[sid] = row
            except Exception:
                pass

        now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

        # Merge data from _sub_cases (SubCase objects have per-sub-case data)
        for sub_id, sc in self._sub_cases.items():
            parent_bdr = self._case_data.get(sc.parent_case_number)
            parties_str = (
                " | ".join(p.strip() for p in parent_bdr.parties if p.strip())
                if parent_bdr else ""
            )
            row = existing.get(sub_id, {})
            row.update({
                "מזהה תיק": sub_id,
                "שם תיק": sc.text or row.get("שם תיק", ""),
                "הליך": sc.procedure or row.get("הליך", ""),
                "בית משפט / בית דין": sc.court or row.get("בית משפט / בית דין", ""),
                "פורטל": "BDR",
                "צדדים": parties_str,
                "תיקייה": row.get("תיקייה", ""),
                "תיקיית תיק על": sc.parent_case_number or sub_id,
                "גורם שיפוטי": row.get("גורם שיפוטי", ""),
                "תאריך פתיחה": sc.open_date or row.get("תאריך פתיחה", ""),
                "תאריך סגירה": sc.close_date or row.get("תאריך סגירה", ""),
                "פעילות אחרונה": sc.last_activity or row.get("פעילות אחרונה", ""),
                "זמן סיכום": now_str,
            })
            existing[sub_id] = row

        # Merge stats from all progress CSVs
        # Scan all batch_progress CSVs under root_output_dir
        for prog_csv in summary_dir.rglob("batch_progress*.csv"):
            try:
                with prog_csv.open(encoding="utf-8-sig") as f:
                    for prow in _csv.DictReader(f):
                        sid = prow.get("מזהה תיק", "")
                        if not sid or sid not in existing:
                            continue
                        for col in ["קבצים סהכ", "הורדו", "נכשלו", "מסמך ראשון", "מסמך אחרון", "חתימת פורטל"]:
                            if prow.get(col):
                                existing[sid][col] = prow[col]
                        if prow.get("תיקייה"):
                            existing[sid]["תיקייה"] = prow["תיקייה"]
            except Exception:
                pass

        # Fill תיקייה from self._case_dirs where not yet set
        for sub_id, case_dir_path in self._case_dirs.items():
            if sub_id in existing:
                if not existing[sub_id].get("תיקייה"):
                    existing[sub_id]["תיקייה"] = str(case_dir_path)

        # Write
        try:
            with summary_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = _csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
                writer.writeheader()
                for row in existing.values():
                    writer.writerow(row)
            self._log(f"[Summary] כתב {len(existing)} שורות ל-{summary_path}")
        except Exception as exc:
            self._log(f"[Summary] שגיאה בכתיבת הסיכום: {exc}", "warn")
