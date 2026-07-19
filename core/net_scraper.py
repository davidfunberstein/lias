"""NET HaMishpat scraper — page-by-page link downloads with unified ManifestManager.

Key behaviours from Net-AI 1.0:
- Reads metadata from hidden #PresentDocumentGridArrayStore JSON store
- Clicks individual download links (btnDownloadDocument)
- Clean Hebrew filenames: YYYY_MM_DD - Type - Party (no DocumentID, / replaced with space)
- Hot-saves manifest CSV after every file
- Supports pagination via 'לדף הבא' button

Additions:
- Disk sync before download: untracked files added, missing files re-queued
- Unified ManifestManager (same columns as BDR)
- Full timestamped logging
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.sync_api import Page

from core.manifest import ManifestManager

if TYPE_CHECKING:
    from core.logger import Logger

# ---------------------------------------------------------------------------
# Per-file Drive upload callback
# Set from runner.py before a download session starts.
# Signature: (file_path: Path, case_dir: Path, doc_id: str, manifest: ManifestManager) -> None
# ---------------------------------------------------------------------------
_on_file_downloaded = None


class _DocumentNotAvailable(Exception):
    """Raised when the portal shows 'מסמך אינו זמין כעת' instead of serving a download."""


def count_pdf_pages(path: Path) -> str:
    """Return the number of pages in a PDF file as a string, or '' on failure."""
    if not path.exists() or path.suffix.lower() != ".pdf":
        return ""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        return str(len(reader.pages))
    except Exception:
        pass
    # Fallback: count /Page entries in raw bytes (fast, works on most PDFs)
    try:
        data = path.read_bytes()
        count = data.count(b"/Type /Page\n") + data.count(b"/Type/Page\n") + data.count(b"/Type /Page\r")
        return str(count) if count > 0 else ""
    except Exception:
        return ""


class NetScraper:
    def __init__(self, page: Page, logger: "Logger | None" = None) -> None:
        self.page = page
        self.logger = logger
        # Native JS alert/confirm dialogs block Playwright clicks entirely —
        # auto-accept them so an unavailable-document alert never hangs the run.
        try:
            page.on("dialog", lambda d: d.accept())
        except Exception:
            pass

    def _log(self, msg: str, level: str = "info") -> None:
        prefixed = f"[NET Scraper] {msg}"
        if self.logger:
            getattr(self.logger, level)(prefixed)
        else:
            print(prefixed)

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    def extract_metadata(self) -> dict:
        """Pull the hidden JSON store from any frame and return {doc_id: meta}."""
        value = None
        for frame in self.page.frames:
            try:
                if frame.is_detached():
                    continue
                el = frame.locator("#PresentDocumentGridArrayStore")
                if el.count() > 0:
                    value = el.get_attribute("value")
                    if value:
                        break
            except Exception:
                continue

        if not value:
            try:
                value = self.page.locator(
                    "#PresentDocumentGridArrayStore"
                ).get_attribute("value")
            except Exception:
                pass

        if not value:
            self._log("Metadata store not found — filenames will be generic.", "warn")
            return {}

        raw_list = json.loads(value.replace("&quot;", '"'))
        self._log(f"Metadata store: {len(raw_list)} document entries.")
        return {str(d.get("DocumentID", "")): d for d in raw_list if d.get("DocumentID")}

    def get_case_name_from_ui(self) -> str:
        """Read the case name from the always-visible top toolbar."""
        selector = "#_ctl0_UpperToolBar_caseNameTD"
        for frame in self.page.frames:
            try:
                if frame.is_detached():
                    continue
                el = frame.locator(selector).first
                if el.count() > 0:
                    text = el.inner_text().strip()
                    if text:
                        self._log(f"Case name from toolbar: '{text}'")
                        return text
            except Exception:
                continue
        self._log("Could not read case name from toolbar — using fallback.", "warn")
        return "Unknown_Case"

    # Exact selector from portal HTML: <a class="modal_close2 modal_ReturnMessageClose" id="returnFocus">
    _MODAL_CLOSE_SEL = (
        "a#returnFocus, "
        "a.modal_ReturnMessageClose, "
        "a.modal_close2, "
        "button:has-text('אישור'), "
        "input[type='button'][value='אישור']"
    )

    def handle_error_modal(self) -> bool:
        """Dismiss system popups that block automation."""
        # Check in page directly first (most portals render dialogs in main frame)
        for ctx in [self.page] + list(self.page.frames):
            try:
                if hasattr(ctx, "is_detached") and ctx.is_detached():
                    continue
                loc = ctx.locator(self._MODAL_CLOSE_SEL).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    loc.click()
                    return True
            except Exception:
                continue
        return False

    def _check_unavailable_popup(self) -> bool:
        """Check all frames for 'מסמך אינו זמין כעת' popup and dismiss if found.

        Dismiss order: click אישור → if the modal is STILL visible, reload the
        page (the portal sometimes leaves a stuck overlay that blocks all
        further clicks — the user-reported "צריך אישור או לרענן" case).
        """
        for ctx in [self.page] + list(self.page.frames):
            try:
                if hasattr(ctx, "is_detached") and ctx.is_detached():
                    continue
                marker = ctx.locator("#MessageLS_DocumentNotAvailable")
                found = marker.count() > 0 and marker.first.is_visible(timeout=300)
                if not found:
                    txt = ctx.locator("div:has-text('אינו זמין'), span:has-text('אינו זמין')")
                    found = txt.count() > 0 and txt.first.is_visible(timeout=300)
                if not found:
                    continue
                # Step 1: try to close via אישור / X
                dismissed = False
                close = ctx.locator(self._MODAL_CLOSE_SEL).first
                if close.count() > 0:
                    try:
                        close.click()
                        time.sleep(0.5)
                        dismissed = not (marker.count() > 0 and marker.first.is_visible(timeout=300))
                    except Exception:
                        pass
                # Step 2: modal still stuck → reload the page to clear the overlay
                if not dismissed:
                    self._log("Popup stuck after אישור — reloading page to clear overlay.", "warn")
                    try:
                        self.page.reload(wait_until="domcontentloaded", timeout=20000)
                        time.sleep(2)
                    except Exception:
                        pass
                return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Filename construction
    # ------------------------------------------------------------------

    def _build_filename(
        self, doc_type: str, party_name: str, raw_date: str
    ) -> tuple[str, str]:
        """Return (date_prefix, base_filename) — no DocumentID, / replaced with space."""
        date_prefix = "0000_00_00"
        date_match = re.search(r"(\d{2})/(\d{2})/(\d{4})", raw_date)
        if date_match:
            date_prefix = (
                f"{date_match.group(3)}_{date_match.group(2)}_{date_match.group(1)}"
            )

        def _clean(s: str) -> str:
            return re.sub(r'[\\*?"<>|]', "", s.replace("/", " ")).strip()

        clean_type = _clean(doc_type)
        clean_party = _clean(party_name)

        if "החלטה" in clean_type:
            base = f"{date_prefix} - החלטה"
        elif clean_party:
            base = f"{date_prefix} - {clean_type} - {clean_party}"
        else:
            base = f"{date_prefix} - {clean_type}"

        return date_prefix, re.sub(r"\s+", " ", base).strip()

    def _unique_path(self, case_dir: Path, base_filename: str, doc_id: str,
                     is_redownload: bool) -> tuple[Path, str]:
        """Return a collision-free (path, filename) pair."""
        target_filename = f"{base_filename}.pdf"
        target_path = case_dir / target_filename

        counter = 2
        while target_path.exists() and not is_redownload:
            target_filename = f"{base_filename}_{counter}.pdf"
            target_path = case_dir / target_filename
            counter += 1

        return target_path, target_filename

    # ------------------------------------------------------------------
    # Pre-populate manifest from metadata (before any downloads)
    # ------------------------------------------------------------------

    def pre_populate_manifest_from_metadata(
        self,
        case_dir: Path,
        manifest: ManifestManager,
        metadata_lookup: dict,
    ) -> int:
        """Write all portal documents to manifest as 'Pending' before downloading.

        Skips documents already in manifest (any status). Returns count of new entries written.
        """
        existing_ids = manifest.get_all_ids()
        added = 0
        for doc_id, doc_meta in metadata_lookup.items():
            if doc_id in existing_ids:
                continue
            doc_type = doc_meta.get("DocumentType", "מסמך")
            party_name = (doc_meta.get("CasePartyDisplayName") or "").strip()
            raw_date = (doc_meta.get("PresentationDate") or "").strip()
            doc_desc = (doc_meta.get("DocumentDesc") or "").strip()
            has_attch = "+" if doc_meta.get("Attch") else ""
            date_prefix, base_filename = self._build_filename(doc_type, party_name, raw_date)
            _, target_filename = self._unique_path(case_dir, base_filename, doc_id, False)
            date_part = raw_date.split()[0] if raw_date else ""
            time_part = raw_date.split()[1] if len(raw_date.split()) > 1 else ""
            manifest.upsert({
                "שם מסמך (מהטבלה)": doc_desc or doc_type,
                "שם קובץ מקורי (מהשרת)": "",
                "תאריך מסמך": date_part,
                "שעת מסמך": time_part,
                "סוג קובץ": doc_type,
                "מגיש": party_name,
                "מזהה ייחודי": doc_id,
                "שם קובץ פיזי בדיסק": target_filename,
                "גודל (KB)": "",
                "סטטוס הורדה": "Pending",
                "מספר עמודים": "",
                "יש נספחים": has_attch,
            })
            added += 1
        if added:
            self._log(f"Pre-populated {added} new document(s) from metadata → status Pending.")
        return added

    # ------------------------------------------------------------------
    # Download loop
    # ------------------------------------------------------------------

    def scrape_and_download_current_page(
        self,
        case_dir: Path,
        manifest: ManifestManager,
        metadata_lookup: dict,
        global_idx_start: int,
        re_download_ids: set[str],
        total_in_portal: int = 0,
    ) -> tuple[int, list[dict], list[dict]]:
        """
        Process all download links on the current page.

        Returns (links_found, newly_downloaded_records, failed_records).
        Returns (0, [], []) when no download links exist — signals end of pagination.
        """
        successful_ids = manifest.get_successful_ids()
        downloaded: list[dict] = []
        failed: list[dict] = []
        processed = 0

        for frame in self.page.frames:
            try:
                if frame.is_detached():
                    continue

                locator = frame.locator("a[href*='btnDownloadDocument']")
                count = locator.count()
                if count == 0:
                    continue

                self._log(f"Found {count} download links on current page.")

                for idx in range(count):
                    link = locator.nth(idx)
                    href = (link.get_attribute("href") or "").replace("&amp;", "&").replace("&quot;", '"')
                    id_match = re.search(r"[\d]+&([\d]+)", href) or re.search(r"(\d{8,11})", href)
                    if not id_match:
                        continue

                    doc_id = str(id_match.group(1))
                    is_redownload = doc_id in re_download_ids

                    if doc_id in successful_ids and not is_redownload:
                        self._log(f"ID {doc_id}: already in manifest (Success), skipping.")
                        processed += 1
                        continue

                    doc_meta = metadata_lookup.get(doc_id, {})
                    doc_type = doc_meta.get("DocumentType", "מסמך")
                    party_name = (doc_meta.get("CasePartyDisplayName") or "").strip()
                    raw_date = (doc_meta.get("PresentationDate") or "").strip()
                    doc_desc = (doc_meta.get("DocumentDesc") or "").strip()
                    has_attch = "+" if doc_meta.get("Attch") else ""

                    date_prefix, base_filename = self._build_filename(doc_type, party_name, raw_date)

                    # Check if a file with this base name already exists on disk —
                    # it may have been downloaded before the manifest existed (Local Sync).
                    # If found, adopt the existing file instead of re-downloading.
                    existing_on_disk = case_dir / f"{base_filename}.pdf"
                    if existing_on_disk.exists() and not is_redownload:
                        existing_name = existing_on_disk.name
                        size_kb = str(round(existing_on_disk.stat().st_size / 1024, 2))
                        page_count_str = count_pdf_pages(existing_on_disk)
                        adopt_record = {
                            "שם מסמך (מהטבלה)": doc_meta.get("DocumentDesc") or doc_type,
                            "שם קובץ מקורי (מהשרת)": existing_name,
                            "תאריך מסמך": raw_date.split()[0] if raw_date else "",
                            "שעת מסמך": raw_date.split()[1] if len(raw_date.split()) > 1 else "",
                            "סוג קובץ": doc_type,
                            "מגיש": party_name,
                            "מזהה ייחודי": doc_id,
                            "שם קובץ פיזי בדיסק": existing_name,
                            "גודל (KB)": size_kb,
                            "סטטוס הורדה": "Success",
                            "מספר עמודים": page_count_str,
                            "יש נספחים": has_attch,
                        }
                        manifest.upsert(adopt_record)
                        downloaded.append(adopt_record)
                        processed += 1
                        self._log(
                            f"ID {doc_id}: file already on disk as '{existing_name}' — adopted, no re-download."
                        )
                        continue

                    target_path, target_filename = self._unique_path(
                        case_dir, base_filename, doc_id, is_redownload
                    )

                    date_part = raw_date.split()[0] if raw_date else ""
                    time_part = raw_date.split()[1] if len(raw_date.split()) > 1 else ""
                    global_num = global_idx_start + processed + 1
                    label = "[RE-DL] " if is_redownload else ""
                    total_suffix = f"/{total_in_portal}" if total_in_portal else ""
                    self._log(f"[{global_num}{total_suffix}] {label} Downloading: {target_filename}")

                    base_record: dict = {
                        "שם מסמך (מהטבלה)": doc_desc or doc_type,
                        "שם קובץ מקורי (מהשרת)": "",
                        "תאריך מסמך": date_part,
                        "שעת מסמך": time_part,
                        "סוג קובץ": doc_type,
                        "מגיש": party_name,
                        "מזהה ייחודי": doc_id,
                        "שם קובץ פיזי בדיסק": target_filename,
                        "גודל (KB)": "0",
                        "סטטוס הורדה": "Pending",
                        "מספר עמודים": "",
                        "יש נספחים": has_attch,
                    }

                    try:
                        link.scroll_into_view_if_needed(timeout=2000)
                        with self.page.expect_download(timeout=30000) as dl_info:
                            link.click(force=True)
                            # Check for "מסמך אינו זמין" popup quickly (1 s) instead of
                            # waiting up to 30 s for a download that will never come.
                            time.sleep(1.0)
                            if self._check_unavailable_popup():
                                raise _DocumentNotAvailable(target_filename)

                        dl = dl_info.value
                        original_name = dl.suggested_filename
                        dl.save_as(str(target_path))
                        size_kb = str(round(target_path.stat().st_size / 1024, 2))
                        page_count_str = count_pdf_pages(target_path)

                        record = {**base_record,
                                  "שם קובץ מקורי (מהשרת)": original_name,
                                  "גודל (KB)": size_kb,
                                  "סטטוס הורדה": "Success",
                                  "מספר עמודים": page_count_str}
                        manifest.upsert(record)
                        downloaded.append(record)
                        self._log(f"[{global_num}] Success -> {target_filename} ({size_kb} KB)", "ok")

                        # OCR: convert scanned PDFs to text (Gemini) if enabled
                        try:
                            from core.pdf_to_text import ocr_if_needed
                            from core.download import SESSION_SETTINGS
                            ocr_if_needed(target_path, SESSION_SETTINGS, logger=self.logger)
                        except Exception:
                            pass  # OCR failures never interrupt downloads

                        # Trigger per-file Drive upload in background thread if configured
                        if _on_file_downloaded is not None:
                            try:
                                _on_file_downloaded(target_path, case_dir, doc_id, manifest)
                            except Exception as _cbe:
                                self._log(f"Drive callback error: {_cbe}", "warn")

                        # Attachments: expand the "+" row and download נספחים
                        if has_attch:
                            try:
                                self._download_attachments(
                                    frame, doc_id, doc_desc or doc_type, base_filename,
                                    case_dir, manifest, raw_date, party_name,
                                )
                            except Exception as _ae:
                                self._log(f"Attachment download error for {doc_id}: {_ae}", "warn")

                    except _DocumentNotAvailable:
                        # Popup was already dismissed by _check_unavailable_popup above
                        status = "Missing"
                        record = {**base_record, "שם קובץ מקורי (מהשרת)": target_filename, "סטטוס הורדה": status}
                        manifest.upsert(record)
                        failed.append(record)
                        self._log(f"[{global_num}] MISSING (מסמך אינו זמין) → {target_filename}", "warn")
                        print(f"  ⚠ מסמך אינו זמין: {target_filename}")

                    except Exception as e:
                        # Check for popup that may have appeared after the download timeout
                        self.handle_error_modal()
                        if self._check_unavailable_popup():
                            status = "Missing"
                        elif "Timeout" in type(e).__name__ or "timeout" in str(e).lower():
                            # No response from server — refresh and mark failed
                            self._log(f"[{global_num}] TIMEOUT — refreshing page and continuing...", "warn")
                            try:
                                self.page.reload(wait_until="domcontentloaded", timeout=15000)
                                time.sleep(2)
                            except Exception:
                                pass
                            status = "Failed (timeout)"
                        else:
                            status = f"Failed ({str(e)[:60]})"
                        record = {**base_record, "שם קובץ מקורי (מהשרת)": target_filename, "סטטוס הורדה": status}
                        manifest.upsert(record)
                        failed.append(record)
                        if status == "Missing":
                            self._log(f"[{global_num}] MISSING → {target_filename}", "warn")
                            print(f"  ⚠ מסמך אינו זמין: {target_filename}")
                        else:
                            self._log(f"[{global_num}] FAILED → {target_filename}: {e}", "error")
                        # Return 0 links after page reload so caller re-navigates
                        if "timeout" in status:
                            return processed + 1, downloaded, failed

                    processed += 1
                    time.sleep(1.5)

                return processed, downloaded, failed

            except Exception as e:
                self._log(f"Frame-level error: {e}", "error")
                continue

        return 0, [], []

    # ------------------------------------------------------------------
    # Attachments (נספחים) — expand "+" row and download the detail grid
    # ------------------------------------------------------------------

    def _download_attachments(
        self,
        frame,
        parent_doc_id: str,
        parent_desc: str,
        parent_base_filename: str,
        case_dir: Path,
        manifest: ManifestManager,
        raw_date: str,
        party_name: str,
    ) -> int:
        """Expand the master row of parent_doc_id, download every attachment in
        the detail grid as '<parent> - נספח N.pdf', then collapse the row.
        Returns the number of attachments downloaded."""
        successful_ids = manifest.get_successful_ids()

        # Hrefs present BEFORE expansion — anything new afterwards is an attachment
        def _hrefs() -> set:
            try:
                return set(frame.evaluate(
                    "[...document.querySelectorAll(\"a[href*='btnDownloadDocument']\")].map(a=>a.href||a.getAttribute('href'))"
                ))
            except Exception:
                return set()

        before = _hrefs()

        # Find the master row that contains this doc's download link and expand it
        row = frame.locator(
            f"div[role='row']:has(a[href*='{parent_doc_id}'])"
        ).first
        expander = row.locator("span.ag-group-contracted:visible").first
        try:
            if expander.count() == 0:
                self._log(f"ID {parent_doc_id}: no expander found — skipping attachments.")
                return 0
            expander.click()
        except Exception as e:
            self._log(f"ID {parent_doc_id}: expander click failed: {e}", "warn")
            return 0
        time.sleep(2.0)

        new_hrefs = [h for h in _hrefs() - before if h]
        if not new_hrefs:
            self._log(f"ID {parent_doc_id}: expanded but no attachment links appeared.")
        n_done = 0
        for i, href in enumerate(sorted(new_hrefs), 1):
            id_match = re.search(r"[\d]+&([\d]+)", href.replace("&amp;", "&")) or re.search(r"(\d{8,11})", href)
            att_id = str(id_match.group(1)) if id_match else f"{parent_doc_id}-att{i}"
            if att_id in successful_ids:
                continue
            att_filename = f"{parent_base_filename} - נספח {i}.pdf"
            att_path = case_dir / att_filename
            date_part = raw_date.split()[0] if raw_date else ""
            record = {
                "שם מסמך (מהטבלה)": f"{parent_desc} — נספח {i}",
                "שם קובץ מקורי (מהשרת)": "",
                "תאריך מסמך": date_part,
                "שעת מסמך": "",
                "סוג קובץ": "נספח",
                "מגיש": party_name,
                "מזהה ייחודי": att_id,
                "שם קובץ פיזי בדיסק": att_filename,
                "גודל (KB)": "0",
                "סטטוס הורדה": "Pending",
                "מספר עמודים": "",
                "יש נספחים": "",
            }
            try:
                link = frame.locator(f"a[href*='{att_id}'][href*='btnDownloadDocument']").first
                link.scroll_into_view_if_needed(timeout=2000)
                with self.page.expect_download(timeout=30000) as dl_info:
                    link.click(force=True)
                    time.sleep(1.0)
                    if self._check_unavailable_popup():
                        raise _DocumentNotAvailable(att_filename)
                dl = dl_info.value
                dl.save_as(str(att_path))
                size_kb = str(round(att_path.stat().st_size / 1024, 2))
                record.update({
                    "שם קובץ מקורי (מהשרת)": dl.suggested_filename,
                    "גודל (KB)": size_kb,
                    "סטטוס הורדה": "Success",
                    "מספר עמודים": count_pdf_pages(att_path),
                })
                manifest.upsert(record)
                n_done += 1
                self._log(f"  נספח {i}/{len(new_hrefs)} → {att_filename} ({size_kb} KB)", "ok")
            except _DocumentNotAvailable:
                record["סטטוס הורדה"] = "Missing"
                manifest.upsert(record)
                self._log(f"  נספח {i}: מסמך אינו זמין", "warn")
            except Exception as e:
                record["סטטוס הורדה"] = f"Failed ({str(e)[:50]})"
                manifest.upsert(record)
                self._log(f"  נספח {i} נכשל: {e}", "warn")
            time.sleep(1.2)

        # Collapse the row back so the main loop's link indexing stays stable
        try:
            row.locator("span.ag-group-expanded:visible").first.click(timeout=2000)
            time.sleep(0.8)
        except Exception:
            pass
        return n_done

    # ------------------------------------------------------------------
    # Document viewing history
    # ------------------------------------------------------------------

    def get_document_viewers(self, doc_id: str) -> str:
        """
        Click the viewing history icon for a document row and extract viewer data.

        Triggers __doPostBack('_ctl0:btnShowDocumentViewingHistory', doc_id),
        waits for the ag-grid popup, extracts all viewer rows, and returns a
        semicolon-separated string like:
          "תובע: דוד פונברשטיין (09/06/2026); בא כוח נתבעים: פנינה יחזקאל (09/06/2026)"

        Returns empty string on any failure.
        """
        try:
            self.page.evaluate(
                f"__doPostBack('_ctl0:btnShowDocumentViewingHistory', '{doc_id}')"
            )
            time.sleep(1.5)

            # Wait for the popup grid to appear
            self.page.wait_for_selector('[col-id="ViewerName"]', timeout=5000)

            rows = self.page.evaluate("""() => {
                const rows = document.querySelectorAll('.ag-row[role="row"]');
                return Array.from(rows).map(row => {
                    const get = colId => {
                        const cell = row.querySelector('[col-id="' + colId + '"]');
                        return cell ? cell.innerText.trim() : '';
                    };
                    return {
                        role: get('PartyAliasName'),
                        name: get('ViewerName'),
                        office: get('OfficeName'),
                        date: get('ViewDate'),
                        method: get('DocumentViewMethodDescription'),
                    };
                }).filter(r => r.name);
            }""")

            if not rows:
                return ""

            # Try to close the modal
            try:
                close = self.page.locator(
                    'button:has-text("סגור"), button:has-text("×"), .modal-close'
                ).first
                if close.count() > 0:
                    close.click()
            except Exception:
                pass

            parts = []
            for r in rows:
                date_str = (r.get("date") or "")[:10]
                parts.append(f"{r['role']}: {r['name']} ({date_str})")
            return "; ".join(parts)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def go_to_next_page(self) -> bool:
        """Click the 'next page' button if present."""
        selector = (
            "button.ngcs-buttonAsLink:has-text('לדף הבא'), "
            "a:has-text('לדף הבא'), "
            "[title='לדף הבא']"
        )
        for frame in self.page.frames:
            try:
                if frame.is_detached():
                    continue
                btn = frame.locator(selector).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    return True
            except Exception:
                continue
        return False
