"""BDR (Rabbinical Courts) navigation — tab transitions, case extraction, and document sync.

Uses ManifestManager for a unified CSV structure compatible with the NET portal.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.sync_api import Page

from core.manifest import ManifestManager, get_summary_csv_path

# ---------------------------------------------------------------------------
# PDF page count helper
# ---------------------------------------------------------------------------

def _count_pdf_pages(path: Path) -> str:
    """Return number of pages in a PDF as a string, or '' on failure."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path), strict=False)
        return str(len(reader.pages))
    except Exception:
        pass
    # Fallback: raw byte count of /Type /Page
    try:
        data = path.read_bytes()
        return str(data.count(b"/Type /Page") + data.count(b"/Type/Page"))
    except Exception:
        return ""


if TYPE_CHECKING:
    from core.logger import Logger


class BdrNavigator:
    def __init__(self, page: Page, logger: "Logger | None" = None) -> None:
        self.page = page
        self.logger = logger

    def _log(self, msg: str, level: str = "info") -> None:
        prefixed = f"[BDR Navigator] {msg}"
        if self.logger:
            getattr(self.logger, level)(prefixed)
        else:
            print(prefixed)

    def _dismiss_doc_unavailable(self) -> bool:
        """Dismiss 'מסמך אינו זמין כעת' / 'DocumentNotAvailable' popup. Returns True if dismissed."""
        import time as _t
        for sel in [
            # prefix match — the real id carries a suffix: MessageLS_DocumentNotAvailable-ct'
            '[id^="MessageLS_DocumentNotAvailable"] a.modal_ReturnMessageClose',
            '[id^="MessageLS_DocumentNotAvailable"] a.modal_close2',
            'a.modal_ReturnMessageClose',
            'a.modal_close2',
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click()
                    _t.sleep(0.5)
                    return True
            except Exception:
                continue
        try:
            if self.page.locator('div:has-text("מסמך אינו זמין")').first.is_visible(timeout=1000):
                for close_sel in ['a:has-text("אישור")', 'a.modal_ReturnMessageClose']:
                    try:
                        self.page.locator(close_sel).first.click()
                        _t.sleep(0.5)
                        return True
                    except Exception:
                        continue
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Tab navigation
    # ------------------------------------------------------------------

    def click_documents_tab(self) -> None:
        try:
            btn = self.page.locator(
                "#ctl00_ctl00_ContentPlaceHolder_ASPxNavBar1_I0i5_T a, "
                "a[href*='FileDocuments.aspx']"
            ).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                time.sleep(1)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Case details extraction
    # ------------------------------------------------------------------

    def extract_case_details_and_route_raw(self) -> tuple[list[str], str]:
        """
        1. Navigate to 'File Details' tab and extract party names + case number.
        2. Return to 'Documents' tab.
        Returns (party_names_list, formatted_case_name).
        """
        choices: list[str] = []
        formatted_case_name = ""

        try:
            self._log("Navigating to 'File Details' tab...")
            file_details_btn = self.page.locator(
                "#ctl00_ctl00_ContentPlaceHolder_ASPxNavBar1_I0i0_T a, "
                "a[href*='FileDetails.aspx']"
            ).first
            file_details_btn.click()

            target_label = (
                "#ctl00_ctl00_ContentPlaceHolder_PH_GridData"
                "_pnlFileDetails_lblFileName"
            )
            self.page.wait_for_selector(target_label, timeout=8000)
            time.sleep(0.8)

            filename_el = self.page.locator(target_label).first
            if filename_el.count() > 0:
                raw_text = filename_el.inner_text().strip()
                self._log(f"Raw label text: {raw_text}")

                num_match = re.search(r"(\d+[-\/]\d+)", raw_text)
                if num_match:
                    formatted_case_name = num_match.group(1).replace("/", "-").strip()

                for part in [p.strip() for p in raw_text.split(",") if p.strip()]:
                    if any(c.isdigit() for c in part):
                        continue
                    if any(w in part for w in ["החזקת ילדים", "הסדרי שהות", "גירושין", "מזונות", "אישות"]):
                        continue
                    clean = " ".join(part.split())
                    if len(clean) > 3 and clean not in choices:
                        choices.append(clean)

        except Exception as e:
            self._log(f"File Details extraction error: {e}", "warn")

        try:
            self._log("Returning to 'Documents' tab...")
            self.click_documents_tab()
            self.page.wait_for_selector("tr[id*='DXDataRow']", timeout=10000)
            time.sleep(0.5)
        except Exception as e:
            self._log(f"Error returning to Documents tab: {e}", "warn")

        if not formatted_case_name:
            url_match = re.search(r"FileId=(\d+)", self.page.url)
            formatted_case_name = url_match.group(1) if url_match else "BDR_Case"

        if "הסדרי שהות" not in formatted_case_name:
            formatted_case_name = f"{formatted_case_name} הסדרי שהות - גדול"
        elif "גדול" not in formatted_case_name:
            formatted_case_name = f"{formatted_case_name} - גדול"

        self._log(f"Parties: {choices}")
        self._log(f"Case folder name: {formatted_case_name}")
        return choices, formatted_case_name

    # ------------------------------------------------------------------
    # Document sync and download
    # ------------------------------------------------------------------

    def sync_and_download_bdr(
        self,
        case_dir: Path,
        settings: dict,
        run_timestamp: str,
        stop_event=None,
        input_check_fn=None,
        parties: str = "",
    ) -> tuple[int, list[dict], list[dict], list[dict], list[dict], list[str]]:
        """
        Full BDR sync cycle using ManifestManager.

        Steps:
        1. Load manifest and sync with disk (add untracked, mark missing).
        2. Snapshot portal table.
        3. Download new / re-download missing files.
        4. Return (total_rows, downloaded, failed, table_updated, snapshot_lines).
        """
        manifest = ManifestManager(
            get_summary_csv_path(case_dir),
            run_timestamp=run_timestamp,
            logger=self.logger,
            portal="BDR",
            parties=parties,
        )
        manifest.sync_with_disk(case_dir)

        rows = self.page.locator("tr[id*='DXDataRow']")
        total_rows = rows.count()
        snapshot_lines: list[str] = []

        self._log(f"Snapshot: {total_rows} rows in BDR table.")
        for i in range(total_rows):
            try:
                row = rows.nth(i)
                cells = row.locator("td.dxgv")
                if cells.count() >= 3:
                    d_type = cells.nth(0).inner_text().strip()
                    d_sub = cells.nth(1).inner_text().strip()
                    d_date = cells.nth(2).inner_text().split("\n")[0].strip()
                    line = f"  Row {i + 1}: Date={d_date} | Type={d_type} | Submitter={d_sub}"
                    snapshot_lines.append(line)
                    self._log(line)
            except Exception:
                pass

        downloaded_list: list[dict] = []      # brand-new downloads this run
        re_download_list: list[dict] = []     # re-downloads of previously failed/missing
        failed_list: list[dict] = []
        table_updated_list: list[dict] = []
        missing_ids = manifest.get_missing_ids()
        failed_ids = manifest.get_failed_ids()
        # Track base-name usage this run to handle duplicate metadata rows
        _base_count: dict[str, int] = {}

        for i in range(total_rows):
            # Check for stop signal or user command between every file
            if stop_event and stop_event.is_set():
                self._log("Download interrupted by stop signal.", "warn")
                break
            if input_check_fn:
                cmd = input_check_fn()
                if cmd in ("stop", "b", "q"):
                    self._log("Download stopped by user command.", "warn")
                    if stop_event:
                        stop_event.set()
                    break
                if cmd == "status":
                    print(
                        f"\n[STATUS] Row {i}/{total_rows} — "
                        f"downloaded: {len(downloaded_list)} new + {len(re_download_list)} re-dl, "
                        f"failed: {len(failed_list)}\n"
                    )

            try:
                row = rows.nth(i)
                cells = row.locator("td.dxgv")
                if cells.count() < 3:
                    continue

                doc_type = cells.nth(0).inner_text().strip()
                submitter = cells.nth(1).inner_text().strip()
                raw_date = cells.nth(2).inner_text().split("\n")[0].strip()

                if settings.get("date_filter") == "y":
                    try:
                        doc_date = datetime.strptime(raw_date, "%d/%m/%Y")
                        if not (settings.get("start_date") <= doc_date <= settings.get("end_date")):
                            continue
                    except ValueError:
                        continue

                base_uid = f"{raw_date}_{doc_type}_{submitter}".replace("/", "_")
                # Disambiguate duplicate-metadata rows (same date+type+submitter)
                _base_count[base_uid] = _base_count.get(base_uid, 0) + 1
                dup_suffix = f"_{_base_count[base_uid]}" if _base_count[base_uid] > 1 else ""
                unique_id = base_uid + dup_suffix

                date_parts = raw_date.split("/")
                f_date = (
                    f"{date_parts[2]}_{date_parts[1]}_{date_parts[0]}"
                    if len(date_parts) == 3
                    else raw_date.replace("/", "_")
                )

                safe_doc = doc_type.replace('"', "").replace("'", "").replace("/", " ").strip()
                expected_base = f"{f_date}_{safe_doc}"
                if submitter and submitter != "לא ידוע":
                    expected_base += f"_{submitter.replace('/', ' ')}"
                # Append duplicate suffix to filename too, so each gets its own file
                expected_base_unique = expected_base + dup_suffix
                expected_stem = re.sub(r'[\\*?:"<>|]', "-", expected_base_unique)[:120].strip()
                expected_filename = expected_stem + ".pdf"

                # EXACT match only. This used to be a prefix test
                # (f.name.startswith(expected_base_unique[:100])), which silently
                # skipped real documents: a shorter name is a prefix of a longer
                # one, so "01_01_2026_החלטה" counted as already-downloaded merely
                # because "01_01_2026_החלטה בבקשה.pdf" was on disk — and the run
                # reported everything up to date while documents were missing.
                # Duplicates on the same date are already disambiguated by
                # dup_suffix, so an exact comparison is the correct test.
                file_exists = any(
                    f.is_file() and (f.name == expected_filename or f.stem == expected_stem)
                    for f in case_dir.iterdir()
                )
                is_missing = unique_id in missing_ids

                if settings.get("mode", "1") == "1" and file_exists and not is_missing:
                    self._log(f"Row {i + 1}: '{expected_filename}' exists — skipping.")
                    continue

                download_btn = (
                    row.locator("a, img, input[type='image']")
                    .filter(has_not=self.page.locator("span"))
                    .first
                )
                if not (download_btn.count() > 0 and download_btn.is_visible()):
                    self._log(f"Row {i + 1}: no visible download button — recording as failed.", "warn")
                    no_btn_record: dict = {
                        "שם מסמך (מהטבלה)": doc_type,
                        "שם קובץ מקורי (מהשרת)": "Failed (No Download Button)",
                        "תאריך מסמך": raw_date,
                        "שעת מסמך": "",
                        "סוג קובץ": doc_type,
                        "מגיש": submitter,
                        "מזהה ייחודי": unique_id,
                        "שם קובץ פיזי בדיסק": expected_filename,
                        "גודל (KB)": "0",
                        "סטטוס הורדה": "Failed (No Button)",
                    }
                    manifest.upsert(no_btn_record)
                    failed_list.append(no_btn_record)
                    continue

                self._log(f"Row {i + 1}/{total_rows}: downloading '{expected_filename}'...")

                base_record: dict = {
                    "שם מסמך (מהטבלה)": doc_type,
                    "שם קובץ מקורי (מהשרת)": "",
                    "תאריך מסמך": raw_date,
                    "שעת מסמך": "",
                    "סוג קובץ": doc_type,
                    "מגיש": submitter,
                    "מזהה ייחודי": unique_id,
                    "שם קובץ פיזי בדיסק": expected_filename,
                    "גודל (KB)": "0",
                    "סטטוס הורדה": "Pending",
                }

                try:
                    with self.page.expect_download(timeout=30000) as dl_info:
                        download_btn.click()

                    dl = dl_info.value
                except Exception as dl_exc:
                    # Check for "מסמך אינו זמין כעת" popup and dismiss
                    if self._dismiss_doc_unavailable():
                        self._log(f"Row {i + 1}: מסמך אינו זמין — ממשיך.", "warn")
                        record = {**base_record,
                                  "שם קובץ מקורי (מהשרת)": "Failed (Document Unavailable)",
                                  "סטטוס הורדה": "Failed (Unavailable)"}
                        manifest.upsert(record)
                        failed_list.append(record)
                        continue
                    raise dl_exc

                try:
                    server_filename = dl.suggested_filename or "unknown.pdf"
                    ext = (
                        f".{server_filename.split('.')[-1].lower()}"
                        if "." in server_filename
                        else ".pdf"
                    )
                    clean_filename = (
                        re.sub(r'[\\*?:"<>|]', "-", expected_base_unique)[:120].strip() + ext
                    )
                    final_path = case_dir / clean_filename
                    dl.save_as(str(final_path))

                    size_kb = str(round(final_path.stat().st_size / 1024, 2))
                    page_count = _count_pdf_pages(final_path) if ext == ".pdf" else ""
                    record = {**base_record,
                              "שם קובץ מקורי (מהשרת)": server_filename,
                              "שם קובץ פיזי בדיסק": clean_filename,
                              "גודל (KB)": size_kb,
                              "מספר עמודים": page_count,
                              "סטטוס הורדה": "Success"}
                    manifest.upsert(record)
                    is_redl = unique_id in missing_ids or unique_id in failed_ids
                    if is_redl:
                        re_download_list.append(record)
                    else:
                        downloaded_list.append(record)
                    self._log(
                        f"Row {i + 1}: {'Re-download' if is_redl else 'New'} "
                        f"-> {clean_filename} ({size_kb} KB)", "ok"
                    )

                    # OCR: convert scanned PDFs to text (Gemini) if enabled
                    if ext == ".pdf":
                        try:
                            from core.pdf_to_text import ocr_if_needed
                            from core.download import SESSION_SETTINGS
                            ocr_if_needed(final_path, SESSION_SETTINGS, logger=self.logger)
                        except Exception:
                            pass  # OCR failures never interrupt downloads

                except Exception as e:
                    self._log(f"Row {i + 1}: download failed — {e}", "error")
                    record = {**base_record,
                              "שם קובץ מקורי (מהשרת)": "Failed (Download Timeout)",
                              "סטטוס הורדה": "Failed (Timeout/Blocked)"}
                    manifest.upsert(record)
                    failed_list.append(record)

            except Exception as e:
                self._log(f"Row {i + 1}: unexpected error — {e}", "error")

        self._log(f"הורדה הושלמה: {total_rows} מסמכים בסה״כ | הורדו: {len(downloaded_list)} | נכשלו: {len(failed_list)}")
        manifest.print_summary(self.logger)
        return total_rows, downloaded_list, re_download_list, failed_list, table_updated_list, snapshot_lines
