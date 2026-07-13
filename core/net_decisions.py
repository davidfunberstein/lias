"""NET HaMishpat — החלטות בתיק (Case Decisions) scraper.

Handles the decisions section of the NET portal:
- Navigating to the החלטות בתיק folder via the left-side tree
- Extracting all decisions from the ag-grid
- Fetching viewer history per decision (with pagination)
- Maintaining a viewers registry (viewers_registry.csv)
- Writing decision data as extra columns into the main manifest CSV
"""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from core.logger import Logger
    from core.manifest import ManifestManager

_TIK_NIYAR_PEYRUT_JS = """
() => {
    // Collect doc_ids from the פירוט column (col-id="2") in the current תיק נייר grid.
    // Each row has a link: __doPostBack("_ctl0:btnShowDocumentViewingHistory","<doc_id>")
    const rows = document.querySelectorAll('.ag-row[role="row"]');
    return Array.from(rows).map(row => {
        const col = row.querySelector('[col-id="2"]');
        if (!col) return null;
        const link = col.querySelector('a');
        if (!link) return null;
        const href = link.getAttribute('href') || '';
        const m = href.match(/"(\d+)"/);
        return m ? m[1] : null;
    }).filter(Boolean);
}
"""

_DECISIONS_JS = """
() => {
    const rows = document.querySelectorAll('.ag-row[role="row"]');
    return Array.from(rows).map(row => {
        const get = colId => {
            const c = row.querySelector('[col-id="' + colId + '"]');
            return c ? c.innerText.trim() : '';
        };
        const getHref = colId => {
            const c = row.querySelector('[col-id="' + colId + '"] a');
            return c ? (c.getAttribute('href') || '') : '';
        };
        const viewersHref = getHref('3');
        const docHref = getHref('1');
        const detailsHref = getHref('0');
        const viewersId = (viewersHref.match(/"(\\d+)"/) || [])[1] || '';
        const docId = (docHref.match(/"(\\d+)&/) || [])[1] || (docHref.match(/"(\\d+)"/) || [])[1] || '';
        const detailsId = (detailsHref.match(/"(\\d+)"/) || [])[1] || '';
        return {
            name: get('DecisionDisplayName'),
            date: get('DecisionSignatureDate'),
            judge: get('DecisionSignatureUserName'),
            viewers_id: viewersId,
            doc_id: docId,
            details_id: detailsId,
        };
    }).filter(r => r.name || r.date);
}
"""

_REGISTRY_CSV_NAME = "viewers_registry.csv"
_REGISTRY_COLS = ["מזהה", "שם", "מעמד", "משרד מייצג", "אופן צפיה", "כינוי ייחודי"]


class NetDecisionsScraper:
    """Scrape and persist decisions + viewer history for a NET case."""

    def __init__(self, page: "Page", case_dir: Path, logger: "Logger | None" = None) -> None:
        self.page = page
        self.case_dir = Path(case_dir)
        self.logger = logger

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str, level: str = "info") -> None:
        print(f"[Decisions] {msg}")
        if self.logger:
            getattr(self.logger, level, self.logger.info)(f"[Decisions] {msg}")

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def navigate_to_decisions(self) -> bool:
        """Navigate to החלטות בתיק folder. Return True if grid appeared."""
        try:
            self.page.evaluate(
                "__doPostBack('_ctl0$ElectronicCaseFolderTreeView1$NavigateToFolder','18')"
            )
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            time.sleep(1.0)
            self.page.wait_for_selector(".ag-row", state="attached", timeout=8000)
            time.sleep(0.5)
            self._log("Navigated to החלטות בתיק.")
            return True
        except Exception as e:
            self._log(f"Navigation to החלטות בתיק failed: {e}", "error")
            return False

    # ------------------------------------------------------------------
    # Grid extraction
    # ------------------------------------------------------------------

    def extract_decisions(self) -> list[dict]:
        """Extract all decisions from the current grid."""
        try:
            rows = self.page.evaluate(_DECISIONS_JS)
            self._log(f"Extracted {len(rows)} decisions from grid.")
            return rows or []
        except Exception as e:
            self._log(f"Failed to extract decisions grid: {e}", "error")
            return []

    # ------------------------------------------------------------------
    # Viewers popup (with pagination)
    # ------------------------------------------------------------------

    _VIEWERS_ROWS_JS = """
    () => {
        const rows = document.querySelectorAll('.ag-row[role="row"]');
        return Array.from(rows).map(row => {
            const get = colId => {
                const c = row.querySelector('[col-id="' + colId + '"]');
                return c ? c.innerText.trim() : '';
            };
            const allCols = Array.from(row.querySelectorAll('[col-id]'))
                .map(c => c.getAttribute('col-id') + '=' + c.innerText.trim());
            return {
                party:  get('PartyAliasName'),
                name:   get('ViewerName'),
                office: get('OfficeName'),
                date:   get('ViewDate'),
                method: get('DocumentViewMethodDescription'),
                _debug: allCols.join(' | '),
            };
        }).filter(r => r.name || r.party);
    }
    """

    _NEXT_PAGE_JS = """
    () => {
        // Click "לדף הבא" in the ag-grid pagination bar if available and enabled
        const btns = Array.from(document.querySelectorAll(
            '.ag-paging-button.ngcs-buttonAsLink, button.ngcs-buttonAsLink'
        ));
        const nxt = btns.find(b => b.textContent.trim().includes('לדף הבא'));
        if (!nxt) return false;
        const parent = nxt.closest('[ref="btNext"]') || nxt.parentElement;
        if (parent && (parent.classList.contains('ag-disabled') ||
                       parent.getAttribute('aria-disabled') === 'true')) return false;
        nxt.click();
        return true;
    }
    """

    def extract_viewers_for_decision(self, doc_id: str) -> list[dict]:
        """Click viewers button, extract all viewer rows across all pages, return rows."""
        try:
            self.page.evaluate(
                f"__doPostBack('_ctl0:btnShowDocumentViewingHistory', '{doc_id}')"
            )
            try:
                self.page.wait_for_selector('[col-id="ViewerName"]', state="attached", timeout=6000)
                time.sleep(0.5)
            except Exception:
                self._log(f"Viewers grid did not appear for doc_id={doc_id}", "warn")
                self._go_back()
                return []

            all_viewers: list[dict] = []
            page_num = 1
            while True:
                rows = self.page.evaluate(self._VIEWERS_ROWS_JS) or []
                if rows and page_num == 1:
                    self._log(f"  Viewers sample (doc_id={doc_id}): {rows[0].get('_debug', '')}")
                all_viewers.extend(rows)

                try:
                    has_next = self.page.evaluate(self._NEXT_PAGE_JS)
                except Exception:
                    has_next = False
                if not has_next:
                    break
                time.sleep(0.8)
                page_num += 1

            if not all_viewers:
                self._log(f"  No viewer rows found for doc_id={doc_id}", "warn")
            else:
                self._log(f"  {len(all_viewers)} viewer row(s) across {page_num} page(s).")

            self._go_back()
            return all_viewers

        except Exception as e:
            self._log(f"Error extracting viewers for doc_id={doc_id}: {e}", "warn")
            try:
                self._go_back()
            except Exception:
                pass
            return []

    def _go_back(self) -> None:
        """Click חזרה button to return from viewers popup."""
        for sel in ("a#_ctl0_ButtonsGroup1_btnBack", "a:has-text('חזרה')"):
            try:
                btn = self.page.query_selector(sel)
                if btn:
                    btn.click()
                    time.sleep(0.5)
                    return
            except Exception:
                continue
        self._log("Could not find חזרה button.", "warn")

    # ------------------------------------------------------------------
    # Viewers registry
    # ------------------------------------------------------------------

    def load_viewers_registry(self) -> dict:
        """Load viewers_registry.csv → dict keyed by (name, method)."""
        path = self.case_dir / _REGISTRY_CSV_NAME
        registry: dict[tuple[str, str], dict] = {}
        if not path.exists():
            return registry
        try:
            with path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    key = (row.get("שם", ""), row.get("אופן צפיה", ""))
                    registry[key] = dict(row)
        except Exception as e:
            self._log(f"Could not load viewers registry: {e}", "warn")
        return registry

    def save_viewers_registry(self, registry: dict) -> None:
        """Save registry to viewers_registry.csv (re-number sequentially)."""
        path = self.case_dir / _REGISTRY_CSV_NAME
        rows = list(registry.values())
        for i, row in enumerate(rows, 1):
            row["מזהה"] = str(i)
        try:
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_REGISTRY_COLS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        except Exception as e:
            self._log(f"Could not save viewers registry: {e}", "error")

    def _ensure_registry_entry(
        self,
        registry: dict,
        name: str,
        party: str,
        office: str,
        method: str,
    ) -> None:
        """Add (name, method) to registry if absent."""
        key = (name, method)
        if key not in registry:
            next_id = str(len(registry) + 1)
            registry[key] = {
                "מזהה":        next_id,
                "שם":          name,
                "מעמד":        party,
                "משרד מייצג":  office,
                "אופן צפיה":   method,
                "כינוי ייחודי": f"{name} ({method})",
            }

    # ------------------------------------------------------------------
    # תיק נייר — viewers per document (פירוט column)
    # ------------------------------------------------------------------

    def _collect_all_tik_niyar_doc_ids(self) -> "list[str]":
        """Phase 1: paginate through ALL תיק נייר pages and collect doc_ids WITHOUT
        opening any viewer popups.  Opening a popup resets the grid to page 1, so
        we must separate discovery from extraction.
        """
        import json as _json
        all_ids: list[str] = []
        seen: set[str] = set()
        page_num = 0
        while True:
            page_num += 1
            try:
                ids_on_page: list[str] = self.page.evaluate(_TIK_NIYAR_PEYRUT_JS) or []
            except Exception as e:
                self._log(f"[TikNiyar] Failed to read page {page_num}: {e}", "warn")
                break
            if not ids_on_page:
                if page_num == 1:
                    self._log("[TikNiyar] No פירוט links found — grid may be empty.", "warn")
                break
            new_ids = [d for d in ids_on_page if d not in seen]
            if not new_ids:
                # All doc_ids already seen → grid looped back to a previous page
                self._log(f"[TikNiyar] Discovery stopped at page {page_num} (loop detected).")
                break
            all_ids.extend(new_ids)
            seen.update(new_ids)
            self._log(f"[TikNiyar] Discovery page {page_num}: +{len(new_ids)} docs (total {len(all_ids)})")
            try:
                has_next = self.page.evaluate(self._NEXT_PAGE_JS)
            except Exception:
                has_next = False
            if not has_next:
                break
            time.sleep(0.5)
        return all_ids

    def collect_tik_niyar_viewers(self) -> "dict[str, list[dict]]":
        """Collect viewing history for every document in the current תיק נייר grid.

        Two-phase approach:
          Phase 1 — paginate through ALL pages and collect every doc_id WITHOUT
                    opening any viewer popup (opening a popup resets the grid to p.1).
          Phase 2 — for each doc_id, extract viewers and write immediately to
                    tik_niyar_viewers.csv (crash-safe).  A progress file records which
                    doc_ids are done so a restart skips already-processed docs.

        Assumes the page is currently showing the תיק נייר tab.
        """
        import json as _json

        progress_file = self.case_dir / "tik_niyar_progress.json"
        viewers_csv   = self.case_dir / "tik_niyar_viewers.csv"
        _CSV_COLS = ["doc_id", "שם", "מעמד", "משרד מייצג", "תאריך צפיה", "אופן צפיה"]

        # Load already-processed doc_ids from previous run
        done: set[str] = set()
        if progress_file.exists():
            try:
                done = set(_json.loads(progress_file.read_text(encoding="utf-8")))
                self._log(f"[TikNiyar] Resuming — {len(done)} doc_id(s) already done.")
            except Exception:
                pass

        # Phase 1 — discover all doc_ids
        all_doc_ids = self._collect_all_tik_niyar_doc_ids()
        remaining = [d for d in all_doc_ids if d not in done]
        self._log(f"[TikNiyar] {len(all_doc_ids)} total doc_ids, {len(remaining)} to process.")

        # Load already-saved rows so we can return the full result
        result: dict[str, list[dict]] = {}
        if viewers_csv.exists():
            try:
                with viewers_csv.open(encoding="utf-8-sig", newline="") as f:
                    for row in csv.DictReader(f):
                        did = row.get("doc_id", "")
                        if did:
                            result.setdefault(did, []).append(row)
            except Exception:
                pass

        # Phase 2 — extract viewers per doc_id and write immediately
        write_header = not viewers_csv.exists()
        with viewers_csv.open("a", encoding="utf-8-sig", newline="") as csvf:
            writer = csv.DictWriter(csvf, fieldnames=_CSV_COLS, extrasaction="ignore")
            if write_header:
                writer.writeheader()

            for i, doc_id in enumerate(remaining, 1):
                self._log(f"[TikNiyar] {i}/{len(remaining)} doc_id={doc_id}")
                rows = self.extract_viewers_for_decision(doc_id)
                if rows:
                    result.setdefault(doc_id, []).extend(rows)
                    for r in rows:
                        writer.writerow({
                            "doc_id":      doc_id,
                            "שם":          r.get("שם", ""),
                            "מעמד":        r.get("מעמד", ""),
                            "משרד מייצג":  r.get("משרד מייצג", ""),
                            "תאריך צפיה": r.get("תאריך צפיה", ""),
                            "אופן צפיה":   r.get("אופן צפיה", ""),
                        })
                    csvf.flush()
                done.add(doc_id)
                try:
                    progress_file.write_text(
                        _json.dumps(list(done), ensure_ascii=False), encoding="utf-8"
                    )
                except Exception:
                    pass
                try:
                    self.page.wait_for_selector(".ag-row", state="attached", timeout=5000)
                except Exception:
                    pass
                time.sleep(0.3)

        # Clean up progress file — all done
        if progress_file.exists():
            try:
                progress_file.unlink()
            except Exception:
                pass

        self._log(f"[TikNiyar] Viewers collected for {len(result)} document(s).")
        return result

    # ------------------------------------------------------------------
    # Full flow
    # ------------------------------------------------------------------

    def update_all_decisions(
        self,
        manifest: "ManifestManager | None" = None,
        extra_viewers: "dict[str, list[dict]] | None" = None,
    ) -> None:
        """
        Full flow:
        1. Navigate to החלטות בתיק
        2. Extract all decisions from ag-grid
        3. For each decision: fetch viewers from החלטות popup + merge with extra_viewers
           (extra_viewers = viewers already collected from תיק נייר פירוט links)
        4. For doc_ids in extra_viewers that have NO decision entry: update viewers-only
        5. Write decision columns into the main manifest via update_decision_data()
        6. Save viewers registry

        extra_viewers: optional dict {doc_id: [viewer_rows]} pre-collected from תיק נייר.
        """
        if not self.navigate_to_decisions():
            self._log("Skipping decisions update — navigation failed.", "error")
            return

        decisions = self.extract_decisions()
        if not decisions:
            self._log("No decisions found in grid.")
            # Still process extra_viewers (viewers-only updates for plain docs)
            if extra_viewers and manifest is not None:
                self._write_extra_viewers_only(extra_viewers, set(), manifest)
            return

        registry = self.load_viewers_registry()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        written = 0
        handled_doc_ids: set[str] = set()

        for decision in decisions:
            doc_id = (
                decision.get("doc_id")
                or decision.get("viewers_id")
                or decision.get("details_id", "")
            )
            if not doc_id:
                continue

            handled_doc_ids.add(doc_id)

            # ── Viewers from החלטות popup ──
            viewers_id = decision.get("viewers_id", "")
            viewers_from_decisions: list[dict] = []
            if viewers_id:
                try:
                    viewers_from_decisions = self.extract_viewers_for_decision(viewers_id)
                    try:
                        self.page.wait_for_selector(".ag-row", state="attached", timeout=5000)
                    except Exception:
                        pass
                except Exception as ve:
                    self._log(f"Viewers fetch failed for doc_id={doc_id}: {ve}", "warn")

            # ── Merge with viewers from תיק נייר פירוט ──
            extra = (extra_viewers or {}).get(doc_id, [])
            all_viewer_rows = viewers_from_decisions + [
                v for v in extra
                if not any(
                    v.get("name") == existing.get("name") and v.get("date") == existing.get("date")
                    for existing in viewers_from_decisions
                )
            ]

            seen_ids = self._build_viewer_ids(all_viewer_rows, registry)

            decision_data = {
                "שם החלטה":         decision.get("name", ""),
                "תאריך החלטה":      decision.get("date", ""),
                "שופט":             decision.get("judge", ""),
                "צופים":            ", ".join(seen_ids),
                "מועד עדכון צופים": now_str,
            }

            if manifest is not None:
                manifest.update_decision_data(doc_id, decision_data)
                written += 1
            else:
                self._log(f"  No manifest provided — decision {doc_id} not persisted.", "warn")

        # ── Docs in תיק נייר that are not listed as decisions ──
        if extra_viewers and manifest is not None:
            self._write_extra_viewers_only(extra_viewers, handled_doc_ids, manifest, registry, now_str)

        self.save_viewers_registry(registry)
        self._log(
            f"Done — {len(decisions)} decisions processed, {written} written to manifest. "
            f"Registry: {len(registry)} viewers."
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_viewer_ids(
        self,
        viewer_rows: "list[dict]",
        registry: dict,
    ) -> "list[str]":
        """Convert viewer rows to sorted, deduplicated registry-ID strings."""
        entries: list[tuple["datetime | None", str]] = []
        for v in viewer_rows:
            v_name   = (v.get("name")   or "").strip()
            v_party  = (v.get("party")  or "").strip()
            v_office = (v.get("office") or "").strip()
            v_method = (v.get("method") or "").strip()
            v_date_str = (v.get("date") or "").strip()
            if not v_name:
                continue
            self._ensure_registry_entry(registry, v_name, v_party, v_office, v_method)
            reg_id = registry[(v_name, v_method)]["מזהה"]
            v_dt: "datetime | None" = None
            for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
                try:
                    v_dt = datetime.strptime(v_date_str, fmt)
                    break
                except ValueError:
                    continue
            entries.append((v_dt, reg_id))

        entries.sort(key=lambda x: (x[0] is None, x[0]))
        seen: list[str] = []
        for _, rid in entries:
            if rid not in seen:
                seen.append(rid)
        return seen

    def _write_extra_viewers_only(
        self,
        extra_viewers: "dict[str, list[dict]]",
        already_handled: "set[str]",
        manifest: "ManifestManager",
        registry: "dict | None" = None,
        now_str: str = "",
    ) -> None:
        """Write viewers-only updates for תיק נייר docs that have no decision entry."""
        if registry is None:
            registry = self.load_viewers_registry()
        if not now_str:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        extras_written = 0
        for doc_id, rows in extra_viewers.items():
            if doc_id in already_handled or not rows:
                continue
            seen_ids = self._build_viewer_ids(rows, registry)
            if not seen_ids:
                continue
            # Use update_decision_data with viewers-only keys — it leaves decision
            # columns empty for docs that have no corresponding decision entry.
            manifest.update_decision_data(doc_id, {
                "צופים":            ", ".join(seen_ids),
                "מועד עדכון צופים": now_str,
            })
            extras_written += 1

        if extras_written:
            self._log(f"[TikNiyar] Wrote viewers-only update for {extras_written} doc(s) without decision entry.")
            self.save_viewers_registry(registry)
