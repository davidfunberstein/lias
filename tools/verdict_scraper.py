"""Verdict scraper — downloads public court decisions from court.gov.il.

No gov.il login required. Uses the public "איתור החלטות" (verdict localization)
page, filters by court + judge + date range, downloads PDFs to a dedicated
verdicts/ folder, and logs a run manifest JSON.

Usage (standalone):
    python tools/verdict_scraper.py --court 30 --judge "כהן" --from 01/01/2025 --to 01/08/2026

Called via LIAS job system:
    jobs.submit("verdict_scrape", {court_id, judge_name, date_from, date_to, max_pages})
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COURT_URL    = "https://www.court.gov.il/NGCS.Web.Site/HomePage.aspx"
TIMEOUT_MS   = 30_000
NAV_TIMEOUT  = 60_000
DL_TIMEOUT   = 60_000  # per PDF


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s)


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

def scrape_verdicts(
    court_id: str,
    judge_name: str,
    date_from: str,
    date_to: str,
    output_dir: Path,
    max_pages: int = 5,
    progress_cb=None,
    logger=None,
) -> list[dict]:
    """Scrape verdicts and download PDFs.

    Returns list of verdict metadata dicts.
    progress_cb(fraction, message) called throughout.
    """
    def log(msg: str):
        if logger:
            logger.info(msg)
        else:
            print(f"[verdict_scraper] {msg}")

    def progress(frac: float, msg: str = ""):
        if progress_cb:
            progress_cb(frac, msg)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright לא מותקן — הרץ: pip install playwright && playwright install chromium")

    pdf_dir = output_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    verdicts: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            locale="he-IL",
            extra_http_headers={"Accept-Language": "he-IL,he;q=0.9"},
        )
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        try:
            log(f"navigating to {COURT_URL}")
            progress(0.02, "פותח את אתר בתי המשפט…")
            page.goto(COURT_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)

            # ── Click "איתור החלטות" via doPostBack ──────────────────────────
            log("clicking איתור החלטות")
            page.evaluate(
                "javascript:__doPostBack('Header1$UpperMenu1$btnVerdictLocalization','')"
            )
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            progress(0.05, "עובר למסך איתור החלטות…")

            # ── Switch to "איתור לפי פרמטרים" tab ───────────────────────────
            log("switching to parameters tab")
            try:
                page.click("a[href='#tabOrderDetails']", timeout=10_000)
            except Exception:
                page.evaluate("javascript:$('#tabOrderDetails').click()")
            page.wait_for_timeout(800)
            progress(0.08, "בחירת טאב איתור לפי פרמטרים…")

            # ── Select court ─────────────────────────────────────────────────
            log(f"selecting court {court_id}")
            page.select_option("#LocateByParameters1_ddlSelectCourt", court_id)
            page.wait_for_timeout(1200)  # judge list loads dynamically
            progress(0.12, "בחירת בית משפט…")

            # ── Select judge ─────────────────────────────────────────────────
            if judge_name:
                log(f"selecting judge: {judge_name}")
                try:
                    # Try to find option by text
                    page.select_option(
                        "#LocateByParameters1_ddlJudgeName",
                        label=judge_name,
                        timeout=8_000,
                    )
                    progress(0.16, f"בחירת שופט: {judge_name}…")
                except Exception:
                    log(f"judge '{judge_name}' not found in dropdown — searching by partial text")
                    # Try partial match via JS
                    found = page.evaluate(f"""
                        (() => {{
                            const sel = document.querySelector('#LocateByParameters1_ddlJudgeName');
                            if (!sel) return false;
                            for (let i = 0; i < sel.options.length; i++) {{
                                if (sel.options[i].text.includes('{judge_name}')) {{
                                    sel.selectedIndex = i;
                                    return sel.options[i].text;
                                }}
                            }}
                            return false;
                        }})()
                    """)
                    if not found:
                        log(f"judge '{judge_name}' not matched — proceeding without judge filter")
            else:
                log("no judge filter")
                progress(0.16, "ללא סינון שופט…")

            # ── Date range ───────────────────────────────────────────────────
            if date_from:
                log(f"setting date_from={date_from}")
                page.fill("#LocateByParameters1_dateFrom", date_from)
                page.evaluate(
                    "document.querySelector('#LocateByParameters1_dateFrom').dispatchEvent(new Event('change'))"
                )
            if date_to:
                log(f"setting date_to={date_to}")
                page.fill("#LocateByParameters1_DateTo", date_to)
                page.evaluate(
                    "document.querySelector('#LocateByParameters1_DateTo').dispatchEvent(new Event('change'))"
                )
            page.wait_for_timeout(500)
            progress(0.20, "הגדרת טווח תאריכים…")

            # ── Click search ─────────────────────────────────────────────────
            log("clicking איתור")
            page.evaluate("javascript:__doPostBack('ButtonsGroup1$btnLocate','')")
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(2000)
            progress(0.25, "מחפש החלטות…")

            # ── Paginate + collect rows ───────────────────────────────────────
            page_num = 0
            while page_num < max_pages:
                page_num += 1
                log(f"processing results page {page_num}")
                progress(
                    0.25 + 0.55 * (page_num / max_pages),
                    f"מעבד עמוד תוצאות {page_num}…",
                )

                # Extract rows from ag-grid
                rows_data = page.evaluate("""
                    (() => {
                        const rows = document.querySelectorAll('.ag-center-cols-container .ag-row');
                        const results = [];
                        rows.forEach(row => {
                            const cells = row.querySelectorAll('.ag-cell');
                            const docLink = row.querySelector('a[href*="btnDocument"]');
                            const docHref = docLink ? docLink.getAttribute('href') : '';
                            // extract parameters from doPostBack call
                            const m = docHref.match(/"btnDocument","([^"]+)"/);
                            const docParam = m ? m[1] : '';
                            results.push({
                                date:      cells[0] ? cells[0].textContent.trim() : '',
                                court:     cells[1] ? cells[1].textContent.trim() : '',
                                proceeding:cells[2] ? cells[2].textContent.trim() : '',
                                case_type: cells[3] ? cells[3].textContent.trim() : '',
                                interest:  cells[4] ? cells[4].textContent.trim() : '',
                                case_num:  cells[5] ? cells[5].textContent.trim() : '',
                                case_name: cells[6] ? cells[6].textContent.trim() : '',
                                dec_type:  cells[7] ? cells[7].textContent.trim() : '',
                                dec_attr:  cells[8] ? cells[8].textContent.trim() : '',
                                doc_param: docParam,
                            });
                        });
                        return results;
                    })()
                """)

                if not rows_data:
                    log(f"no rows found on page {page_num}, stopping")
                    break

                log(f"found {len(rows_data)} rows on page {page_num}")

                for i, row in enumerate(rows_data):
                    if not row.get("doc_param"):
                        continue
                    verdict = dict(row)
                    verdict["downloaded_at"] = _ts()
                    verdict["pdf_path"] = ""

                    # Download the document
                    try:
                        pdf_name = _safe_name(
                            f"{row['date']}_{row['court']}_{row['case_num']}_{row['dec_type']}"
                        ) + ".pdf"
                        pdf_path = pdf_dir / pdf_name

                        if not pdf_path.exists():
                            log(f"  downloading doc {i+1}/{len(rows_data)}: {row['case_num']}")
                            # Open document — this triggers a new page/tab
                            with context.expect_page() as new_page_info:
                                page.evaluate(
                                    f"javascript:__doPostBack('btnDocument','{row['doc_param']}')"
                                )
                            doc_page = new_page_info.value
                            doc_page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
                            doc_page.wait_for_timeout(1500)

                            # Click save button
                            try:
                                with doc_page.expect_download(timeout=DL_TIMEOUT) as dl_info:
                                    doc_page.click("#btnSave", timeout=10_000)
                                dl = dl_info.value
                                dl.save_as(str(pdf_path))
                                verdict["pdf_path"] = str(pdf_path.relative_to(output_dir))
                                log(f"    saved: {pdf_name}")
                            except Exception as e:
                                log(f"    download failed: {e}")
                            finally:
                                try:
                                    doc_page.close()
                                except Exception:
                                    pass
                        else:
                            verdict["pdf_path"] = str(pdf_path.relative_to(output_dir))
                            log(f"  already have: {pdf_name}")

                    except Exception as e:
                        log(f"  doc download error for {row['case_num']}: {e}")

                    verdicts.append(verdict)
                    time.sleep(0.5)  # be polite

                # Try to go to next page
                has_next = page.evaluate("""
                    (() => {
                        const btn = document.querySelector('button.ag-paging-button[aria-label]') ||
                                    Array.from(document.querySelectorAll('button.ag-paging-button'))
                                         .find(b => b.textContent.includes('לדף הבא'));
                        if (!btn) return false;
                        const parent = btn.closest('[aria-disabled]') || btn.closest('.ag-disabled');
                        if (parent && parent.getAttribute('aria-disabled') === 'true') return false;
                        if (btn.disabled) return false;
                        return true;
                    })()
                """)
                if not has_next:
                    log("no next page, done")
                    break

                log("clicking next page")
                page.evaluate("""
                    (() => {
                        const btn = Array.from(document.querySelectorAll('button.ag-paging-button'))
                                        .find(b => b.textContent.includes('לדף הבא'));
                        if (btn) btn.click();
                    })()
                """)
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
                page.wait_for_timeout(2000)

        finally:
            try:
                browser.close()
            except Exception:
                pass

    progress(0.95, f"הורדו {len(verdicts)} החלטות")
    return verdicts


# ---------------------------------------------------------------------------
# LIAS job handler (registered in collector_bridge)
# ---------------------------------------------------------------------------

def register_verdict_handler():
    """Register the verdict_scrape job handler with LIAS."""
    try:
        from LIAS.jobs import handler, JobContext
        from LIAS import config

        @handler("verdict_scrape")
        def _handle_verdict_scrape(payload: dict, ctx: JobContext):
            court_id   = payload.get("court_id", "-1")
            judge_name = payload.get("judge_name", "")
            date_from  = payload.get("date_from", "")
            date_to    = payload.get("date_to", "")
            max_pages  = int(payload.get("max_pages", 5))

            output_dir = config.COURT_DOCS_DIR / "verdicts"
            output_dir.mkdir(parents=True, exist_ok=True)

            verdicts = scrape_verdicts(
                court_id=court_id,
                judge_name=judge_name,
                date_from=date_from,
                date_to=date_to,
                output_dir=output_dir,
                max_pages=max_pages,
                progress_cb=ctx.progress,
            )

            # Save run manifest
            run_file = output_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            run_file.write_text(
                json.dumps({
                    "run_at":    _ts(),
                    "court_id":  court_id,
                    "judge":     judge_name,
                    "date_from": date_from,
                    "date_to":   date_to,
                    "count":     len(verdicts),
                    "verdicts":  verdicts,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            ctx.progress(1.0, f"הסתיים — {len(verdicts)} החלטות הורדו")
            return f"{len(verdicts)} verdicts downloaded"

    except ImportError:
        pass  # standalone use without LIAS


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Scrape public court verdicts")
    parser.add_argument("--court",   required=True, help="Court ID (e.g. 30 for שלום ירושלים)")
    parser.add_argument("--judge",   default="", help="Judge name (partial match)")
    parser.add_argument("--from",    dest="date_from", default="", help="From date DD/MM/YYYY")
    parser.add_argument("--to",      dest="date_to",   default="", help="To date DD/MM/YYYY")
    parser.add_argument("--pages",   type=int, default=5, help="Max result pages (default 5)")
    parser.add_argument("--out",     default="./verdicts_output", help="Output directory")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    results = scrape_verdicts(
        court_id=args.court,
        judge_name=args.judge,
        date_from=args.date_from,
        date_to=args.date_to,
        output_dir=out,
        max_pages=args.pages,
        progress_cb=lambda f, m: print(f"[{f:.0%}] {m}"),
    )
    print(f"\nDone: {len(results)} verdicts")
    summary = out / "summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved to {summary}")
