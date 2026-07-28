#!/usr/bin/env python3
"""
ECA (הוצאה לפועל) downloader
=============================
מוריד את כל מסמכי הבקשות וההחלטות מהוצאה לפועל (publicsso.eca.gov.il).

מבנה תיקיות:
  הוצאה לפועל/
    {תיק}/
      {הליך}/
        {הליך} - {תאריך} - בקשה - {מגיש}.pdf
        {הליך} - {תאריך_החלטה} - החלטה - {מגיש}.pdf

הפעלה:
  python eca_download.py [--output ./הוצאה_לפועל] [--profile ./browser_profile]
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_URL = "https://publicsso.eca.gov.il"

# live download counters (broadcast to the UI tasks balloon)
_STATS = {"done": 0, "errors": 0, "total": 0, "case_idx": 0, "cases_total": 0,
          "t0": 0.0, "job_id": None, "current_case": "", "current_name": "",
          "cases_detail": [], "output_dir": ""}


def _stats_tick(status: str) -> None:
    if status == "ok":
        _STATS["done"] += 1
    elif status == "fail":
        _STATS["errors"] += 1
    _broadcast_eca_stats()


def _broadcast_eca_stats() -> None:
    """Emit the SAME normalized, portal-tagged shape NET uses so the UI can key
    stats per portal (no more cross-portal mixups / 'undefined/N')."""
    try:
        from LIAS import jobs as _jobs
        mins = max((time.time() - _STATS["t0"]) / 60.0, 0.05)
        _jobs.broadcast({"type": "download_stats", "portal": "ECA",
                         "job_id": _STATS.get("job_id"),
                         "done": _STATS["case_idx"],          # cases finished
                         "total": _STATS["cases_total"],       # cases total
                         "failed": _STATS["errors"],
                         "docs_downloaded": _STATS["done"],
                         "current_case": _STATS.get("current_case", ""),
                         "current_name": _STATS.get("current_name", ""),
                         "remaining": max(_STATS["cases_total"] - _STATS["case_idx"], 0),
                         "speed_per_min": round(_STATS["done"] / mins, 1),
                         "elapsed_sec": round(time.time() - _STATS["t0"]) if _STATS["t0"] else 0,
                         "cases_detail": _STATS.get("cases_detail", []),
                         "output_dir": _STATS.get("output_dir", "")})
    except Exception:
        pass
OPEN_CASES_URL = f"{BASE_URL}/he/home/OpenCase"


def _ts() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def _log(msg: str) -> None:
    print(f"{_ts()} {msg}", flush=True)


def _sanitize(name: str) -> str:
    """Remove filesystem-illegal characters."""
    return re.sub(r'[\\/:*?"<>|]', "_", name.strip())


def _make_filename(process: str, date: str, doc_type: str, applicant: str) -> str:
    """Build the document filename."""
    parts = [p for p in [process, date, doc_type, applicant] if p]
    return _sanitize(" - ".join(parts)) + ".pdf"


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _wait_for_eca_home(page, timeout: int = 60) -> bool:
    """Wait until we land on an authenticated ECA page (not login.gov.il)."""
    _log("ממתין להתחברות ל-ECA…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        _click_system_choice(page)
        url = page.url or ""
        if "publicsso.eca.gov.il" in url and "login.gov.il" not in url:
            # Check that Angular has rendered (carousel or main content present)
            try:
                page.wait_for_selector("#carousel-cases, app-root", timeout=5000)
                _log("✓ מחובר ל-ECA")
                return True
            except Exception:
                pass
        time.sleep(1.5)
    return False


def _click_system_choice(page) -> None:
    """If a system-selection page appears (בד"ר נט / הוצאה לפועל), pick הוצאה לפועל."""
    _ECA_CHOICE_SELECTORS = [
        'button:has-text("הוצאה לפועל")',
        'a:has-text("הוצאה לפועל")',
        'div:has-text("הוצאה לפועל") >> visible=true',
        'input[value*="הוצאה לפועל"]',
    ]
    for sel in _ECA_CHOICE_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=600):
                el.click()
                _log("✓ נבחרה מערכת: הוצאה לפועל")
                time.sleep(1)
                return
        except Exception:
            continue


def install_eca_asset_fix(page) -> None:
    """The ECA CDN (CloudFront) sometimes serves its index.html instead of the
    site's own JS/CSS/JSON assets to automation clients — the Angular shell
    never boots and the page stays blank ("אנא המתן" forever / 0 cases).
    Workaround (verified live): intercept publicsso asset requests, re-fetch
    them via the API client with plain-Chrome headers (+ a cache-busting query
    on a poisoned response) and fulfill with the correct MIME type."""
    import random
    import re as _re
    import ssl as _ssl
    import urllib.request as _ur
    ctx = page.context
    if getattr(ctx, "_eca_asset_fix", False):
        return
    ctx._eca_asset_fix = True
    _EXT = _re.compile(r"\.(js|css|json|svg|png|jpg|woff2?|ico)(\?|$)")
    _MIME = {"js": "application/javascript", "css": "text/css",
             "json": "application/json", "svg": "image/svg+xml",
             "png": "image/png", "jpg": "image/jpeg", "woff": "font/woff",
             "woff2": "font/woff2", "ico": "image/x-icon"}
    _UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    _sslctx = _ssl.create_default_context()
    # Per-URL cache of served bodies. The live Angular app re-requests assets
    # (lazy chunks, re-navigation); without a cache every hit does a blocking
    # urllib fetch on Playwright's event-loop thread, which starves the browser
    # until it crashes and relaunches (blank window). Cache = one fetch each.
    _cache: dict = {}

    def _fetch(url: str) -> tuple[int, bytes]:
        """Plain urllib fetch — MUST NOT call any Playwright API here. The route
        handler runs on Playwright's own event loop; re-entering Playwright
        (ctx.request.get) from inside it deadlocks the BrowserManager thread
        and the page hangs forever on 'אנא המתן'. urllib is inert to that loop."""
        req = _ur.Request(url, headers={
            "User-Agent": _UA, "Accept": "*/*",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
            "Referer": "https://publicsso.eca.gov.il/he/login"})
        with _ur.urlopen(req, timeout=15, context=_sslctx) as resp:
            return resp.status, resp.read()

    def _prewarm() -> None:
        """Fetch the index + every asset it references INTO the cache before the
        first navigation. Runs on the caller's thread (not Playwright's event
        loop), so afterwards every route hit is an instant cache serve — the
        page boots fast and the event loop is never blocked (no crash)."""
        try:
            base = "https://publicsso.eca.gov.il/he/"
            status, idx = _fetch(base + "login")
            for _ in range(3):
                if idx[:9].lower() != b"<!doctype":
                    break  # index itself must be HTML; if not, edge is confused
                if b"<app-root" in idx or b"chunk-" in idx or b"main-" in idx:
                    break
                status, idx = _fetch(base + f"login?cb={random.randint(1, 10**9)}")
            html = idx.decode("utf-8", "ignore")
            assets = set(_re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', html))
            for a in assets:
                full = a if a.startswith("http") else base + a.lstrip("/")
                if "publicsso.eca.gov.il" not in full:
                    continue
                key = full.split("?")[0]
                if key in _cache:
                    continue
                try:
                    st, body = _fetch(full)
                    for _ in range(2):
                        if body[:9].lower() != b"<!doctype":
                            break
                        st, body = _fetch(full + ("&" if "?" in full else "?") +
                                          f"cb={random.randint(1, 10**9)}")
                    if body[:9].lower() != b"<!doctype":
                        _cache[key] = (st, body)
                except Exception:
                    continue
            _log(f"✓ טעינה מוקדמת של {len(_cache)} קבצי אתר הוצל\"פ (מונע קריסה)")
        except Exception as _e:
            _log(f"⚠ טעינה מוקדמת נכשלה ({str(_e)[:60]}) — ממשיך בכל זאת")

    def _route(route):
        url = route.request.url
        key = url.split("?")[0]
        try:
            cached = _cache.get(key)
            if cached is not None:
                status, body = cached
            else:
                status, body = _fetch(url)
                # at most 2 cache-busting retries if the edge served the shell
                for _ in range(2):
                    if body[:9].lower() != b"<!doctype":
                        break
                    status, body = _fetch(
                        url + ("&" if "?" in url else "?") + f"cb={random.randint(1, 10**9)}")
                if body[:9].lower() != b"<!doctype":
                    _cache[key] = (status, body)   # only cache good bodies
            if body[:9].lower() == b"<!doctype":
                route.fallback()
                return
            ext = _EXT.search(url).group(1)
            route.fulfill(status=status, body=body,
                          content_type=_MIME.get(ext, "application/octet-stream"))
        except Exception:
            try:
                route.fallback()
            except Exception:
                pass

    ctx.route(lambda u: "publicsso.eca.gov.il" in u and _EXT.search(u), _route)
    _log("✓ הותקן עוקף CDN לנכסי אתר ההוצאה לפועל")
    _prewarm()


def _login_eca(page) -> bool:
    """Authenticate to ECA through the app's ONE unified login chain
    (core.connection.ensure_logged_in — the same mechanism NET and BDR use,
    including the audit log and the per-portal lock).

    There is deliberately no second/standalone login path here: ECA is part of
    the main application, not a separate program.
    """
    install_eca_asset_fix(page)
    from core.connection import ensure_logged_in
    if ensure_logged_in(page, "ECA"):
        return True
    raise RuntimeError("ההתחברות להוצאה לפועל לא הושלמה — בדוק את אישורי gov.il "
                       "ואת מקור קוד האימות בהגדרות ⚙.")


# ---------------------------------------------------------------------------
# Case list
# ---------------------------------------------------------------------------

def _portal_down(page) -> bool:
    """The ECA site sometimes serves its index.html for its own JS assets
    (broken deploy/CDN) — the Angular shell never boots and every page is
    blank. Detect that so we say 'the portal is down' instead of '0 cases'."""
    try:
        # Down = Angular never booted AND nothing rendered at all (the legit
        # loading state shows "אנא המתן…" in the body, so body text > 10).
        return page.evaluate(
            "(document.querySelector('app-root')?.innerHTML||'').length < 50"
            " && (document.body?.innerText||'').trim().length < 10")
    except Exception:
        return False


def _extract_cases(page) -> list[dict]:
    """Extract all open cases from the carousel."""
    _log("מחלץ רשימת תיקים…")
    time.sleep(4)
    if _portal_down(page):
        _log("⛔ אתר ההוצאה לפועל אינו נטען כרגע (תקלה באתר הממשלתי — "
             "הקבצים של האתר עצמו לא מוגשים). נסה שוב מאוחר יותר.")
        raise RuntimeError("אתר ההוצאה לפועל אינו זמין כרגע (תקלה בצד הממשלתי) — נסה מאוחר יותר")
    # Angular can take a while; wait for cards, retrying and re-navigating once.
    cards = []
    for attempt in range(3):
        try:
            page.wait_for_selector(
                "#carousel-cases mat-card, app-mycases-cards mat-card, mat-card[id^='card-case']",
                timeout=20000)
        except Exception:
            pass
        cards = page.query_selector_all(
            "mat-card[id^='card-case'], #carousel-cases mat-card, app-mycases-cards mat-card")
        if cards:
            break
        _log(f"⚠ לא נמצאו כרטיסי תיקים (ניסיון {attempt+1}/3) — URL={page.url[:70]}")
        # maybe we're not on the OpenCase page — go there and retry
        try:
            if "/home/OpenCase" not in (page.url or ""):
                page.goto(OPEN_CASES_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
        except Exception:
            pass
    if not cards:
        _log("⚠ לא נמצא קרוסל תיקים — ייתכן שההתחברות לא הושלמה. בדוק את הדפדפן.")
        return []

    cases = []
    for card in cards:
        card_id = card.get_attribute("id") or ""  # e.g. card-case-529310-10-24
        m = re.match(r"card-case-(.+)", card_id)
        if not m:
            continue
        case_num = m.group(1)

        # Extract subtitle (type), role, and the party name shown on the card
        try:
            subtitle = card.query_selector("mat-card-subtitle")
            case_type = subtitle.inner_text().strip() if subtitle else ""
        except Exception:
            case_type = ""

        try:
            role_el = card.query_selector(".col.text-start p.bold")
            role = role_el.inner_text().strip() if role_el else ""
        except Exception:
            role = ""

        # The other party's name (client grouping key) — the bold span inside
        # the card content, e.g. "הבנק הבינלאומי..." or "דוד פונברשטיין"
        try:
            party_el = card.query_selector("mat-card-content span.bold.mat-mdc-tooltip-trigger, mat-card-content span.mat-mdc-tooltip-trigger")
            party = party_el.inner_text().strip() if party_el else ""
        except Exception:
            party = ""

        cases.append({"number": case_num, "type": case_type, "role": role, "party": party})
        _log(f"  תיק: {case_num} | {case_type} | {role} | {party}")

    _log(f"נמצאו {len(cases)} תיקים")
    return cases


# ---------------------------------------------------------------------------
# Navigate to case and open בקשות והחלטות tab
# ---------------------------------------------------------------------------

def _on_case_page(page, case_num: str) -> bool:
    """True only when the browser is really sitting on THIS case's page.
    The Angular router can silently bounce us back to OpenCase (or to the SSO)
    after a goto, which used to look like a successful navigation."""
    try:
        return f"/caseinfo/{case_num}" in (page.url or "")
    except Exception:
        return False


def _goto_case(page, case_num: str) -> bool:
    """Land on https://publicsso.eca.gov.il/he/caseinfo/{case_num} and stay there.

    Every case is opened by URL — that is the portal's own deep link. Because the
    SPA sometimes swallows the first navigation (router still booting, session
    re-check mid-flight), we verify the URL stuck and retry instead of silently
    downloading the *previous* case's documents."""
    url = f"{BASE_URL}/he/caseinfo/{case_num}"
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            _log(f"  ⚠ ניווט לתיק {case_num} נכשל (ניסיון {attempt}/3): {e}")
            time.sleep(2)
            continue
        # give the router a moment, then confirm the URL really stuck
        for _ in range(10):
            time.sleep(1)
            if _on_case_page(page, case_num):
                break
        if not _on_case_page(page, case_num):
            _log(f"  ⚠ הפורטל החזיר אותנו מ-{case_num} אל {(page.url or '')[:70]} "
                 f"(ניסיון {attempt}/3)")
            time.sleep(2)
            continue
        # URL is right — wait for the case shell to actually render
        try:
            page.wait_for_selector("#MotionDecisionTable, mat-tab-link, app-caseinfo",
                                   timeout=20000)
        except Exception:
            pass
        _log(f"  ✓ נכנס לתיק {case_num}")
        return True
    _log(f"  ✗ לא הצלחתי להיכנס לתיק {case_num} אחרי 3 ניסיונות")
    return False


def _open_motions_tab(page, case_num: str) -> bool:
    """Enter the case by URL, then open its 'בקשות והחלטות' tab."""
    if not _on_case_page(page, case_num):
        if not _goto_case(page, case_num):
            return False

    for attempt in range(1, 3):
        try:
            tab = page.locator("#MotionDecisionTable").first
            tab.wait_for(state="visible", timeout=10000)
            tab.click()
            _log("  ✓ לחץ על 'בקשות והחלטות'")
            time.sleep(2)
            return True
        except Exception as e:
            _log(f"  ⚠ לא מצא טאב 'בקשות והחלטות' (ניסיון {attempt}/2): {e}")
            if attempt == 1:
                _goto_case(page, case_num)
    return False


# ---------------------------------------------------------------------------
# Paginator — set to show max items
# ---------------------------------------------------------------------------

def _maximize_paginator(page) -> None:
    """Change items-per-page selector to the maximum available option."""
    try:
        # Click the mat-select for page size
        page.locator("mat-paginator mat-select").first.click()
        time.sleep(0.8)
        # Pick the largest option (last in the list)
        options = page.locator("mat-option").all()
        if options:
            options[-1].click()
            time.sleep(1.5)
            _log("  ✓ הגדיל את הפגינטור למקסימום")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Row extraction
# ---------------------------------------------------------------------------

def _cell_text(row, col_class: str) -> str:
    try:
        cell = row.query_selector(f"td.{col_class}")
        if not cell:
            return ""
        div = cell.query_selector(".general-text")
        return (div.inner_text() if div else cell.inner_text()).strip()
    except Exception:
        return ""


def _has_button(row, selector: str) -> bool:
    try:
        btn = row.query_selector(selector)
        return btn is not None and btn.is_visible()
    except Exception:
        return False


def _extract_rows(page) -> list[dict]:
    """Collect ALL rows from the motions table. The table may virtual-scroll,
    so we scroll through it repeatedly and merge rows by process number until
    no new rows appear. Only plain data is kept — row handles go stale after
    scrolling, so downloads re-locate each row fresh by its process number."""
    collected: dict = {}

    def _grab() -> int:
        new = 0
        rows = page.query_selector_all("tr[mat-row], tr.mat-mdc-row, tr[role='row']")
        for row in rows:
            proc = _cell_text(row, "mat-column-processNumber")
            if not proc:
                continue
            key = (proc, _cell_text(row, "mat-column-motionOpenDate"),
                   _cell_text(row, "mat-column-motionDecisionName"))
            if key in collected:
                continue
            collected[key] = {
                "process":      proc,
                "name":         _cell_text(row, "mat-column-motionDecisionName"),
                "applicant":    _cell_text(row, "mat-column-motionApplicant"),
                "date":         _cell_text(row, "mat-column-motionOpenDate"),
                "dec_date":     _cell_text(row, "mat-column-decisionDate"),
                "dec_result":   _cell_text(row, "mat-column-decisionResultName"),
                "has_motion":   _has_button(row, "#motionDocumentID"),
                "has_decision": _has_button(row, "#decisionDocumentID"),
            }
            new += 1
        return new

    _grab()
    stale_rounds = 0
    for _ in range(60):
        try:
            rows = page.query_selector_all("tr[mat-row], tr.mat-mdc-row")
            if rows:
                rows[-1].scroll_into_view_if_needed(timeout=2000)
            page.mouse.wheel(0, 800)
        except Exception:
            pass
        time.sleep(0.6)
        if _grab() == 0:
            stale_rounds += 1
            if stale_rounds >= 3:
                break
        else:
            stale_rounds = 0
    return list(collected.values())


def _row_locator(page, proc: str, date: str):
    """Fresh locator for the row whose processNumber cell is exactly `proc`
    (and, when given, whose open-date matches — disambiguates duplicates)."""
    import re as _re
    base = page.locator("tr[mat-row], tr.mat-mdc-row").filter(
        has=page.locator("td.mat-column-processNumber .general-text",
                         has_text=_re.compile(rf"^\s*{_re.escape(proc)}\s*$")))
    if date:
        with_date = base.filter(
            has=page.locator("td.mat-column-motionOpenDate .general-text",
                             has_text=_re.compile(rf"^\s*{_re.escape(date)}\s*$")))
        if with_date.count() > 0:
            return with_date.first
    return base.first


# ---------------------------------------------------------------------------
# Viewer dialog handling
# ---------------------------------------------------------------------------

def _close_viewer(page, wait_s: float = 6.0) -> None:
    """Close any open document-viewer / error dialog and WAIT until it is gone
    — an open overlay blocks every next click (the 'decision never downloads'
    bug). Handles the pdf.js viewer and the "המסמך אינו זמין" error popup."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            if page.locator("mat-dialog-container, .cdk-overlay-pane .mdc-dialog").count() == 0:
                return
        except Exception:
            return
        for sel in ('mat-dialog-container button[aria-label*="סגור"]',
                    'mat-dialog-container button:has-text("סגירה")',
                    'mat-dialog-container button:has-text("אישור")',
                    'mat-dialog-container mat-icon:has-text("close")',
                    '.cdk-overlay-pane button[aria-label*="Close"]'):
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=300):
                    el.click()
                    time.sleep(0.5)
                    break
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.5)


def _is_unavailable_dialog(page) -> bool:
    """True if the current dialog says the document is unavailable."""
    try:
        dlg = page.locator("mat-dialog-container, .cdk-overlay-pane")
        if dlg.count() == 0:
            return False
        txt = dlg.first.inner_text(timeout=1000) or ""
        return any(k in txt for k in ("אינו זמין", "לא זמין", "לא ניתן להציג"))
    except Exception:
        return False


def _extract_file_from_json(raw: bytes) -> bytes:
    """Find a base64-encoded PDF/Office file anywhere inside a JSON payload
    (the ECA API returns the document like this via GetPermittedDocument)."""
    import base64
    import json as _json
    try:
        obj = _json.loads(raw)
    except Exception:
        return b""
    found: list[bytes] = []

    def _walk(v):
        if isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, list):
            for x in v:
                _walk(x)
        elif isinstance(v, str) and len(v) > 1000:
            try:
                d = base64.b64decode(v, validate=False)
                if d[:4] == b"%PDF" or d[:2] == b"PK":
                    found.append(d)
            except Exception:
                pass

    _walk(obj)
    return found[0] if found else b""


# ---------------------------------------------------------------------------
# Document download
# ---------------------------------------------------------------------------

def _download_doc(page, row_loc, btn_id: str, save_path: Path) -> str:
    """Click a document button on a freshly-located row and capture the file.
    Returns 'ok' / 'missing' (unavailable — dismissed, safe to continue) /
    'fail'."""
    if save_path.exists():
        _log(f"      ↩ כבר קיים: {save_path.name}")
        return "ok"
    save_path.parent.mkdir(parents=True, exist_ok=True)

    btn = row_loc.locator(f"#{btn_id}").first
    try:
        if btn.count() == 0 or not btn.is_visible(timeout=1500):
            return "fail"
    except Exception:
        return "fail"

    pdf_responses: list = []

    def _on_response(resp):
        try:
            ct = (resp.headers or {}).get("content-type", "")
            if "pdf" in ct or "octet-stream" in ct or \
               "GetPermittedDocument" in (resp.url or ""):
                pdf_responses.append(resp)
        except Exception:
            pass

    page.on("response", _on_response)
    try:
        _close_viewer(page, wait_s=3)          # make sure nothing blocks us
        try:
            row_loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            with page.expect_download(timeout=6000) as dl_info:
                btn.click()
            dl = dl_info.value
            # save_as() blocks until the transfer completes and takes NO timeout,
            # so a stalled file used to hang the whole run with nothing in the log
            # to say which document was stuck. Name it first, then bound the wait
            # by checking the download actually finished before copying it.
            _log(f"      ↓ מוריד: {save_path.name}")
            _t0 = time.time()
            fail = dl.failure()            # returns None once the transfer ended
            if fail:
                _log(f"      ⚠ ההורדה נכשלה ({fail}) — מדלג: {save_path.name}")
                return "fail"
            dl.save_as(str(save_path))
            _log(f"      ✓ {save_path.name} ({time.time()-_t0:.1f}s)")
            return "ok"
        except Exception as _e:
            _log(f"      ⚠ נתיב ההורדה הישיר נכשל ({str(_e)[:60]}) — מנסה דרך ה-API")

        # Wait for the API response that carries the document
        deadline = time.time() + 12
        while time.time() < deadline and not pdf_responses:
            if _is_unavailable_dialog(page):
                _log(f"      ⚠ המסמך אינו זמין — מדלג: {save_path.name}")
                return "missing"
            time.sleep(0.5)
        if pdf_responses:
            try:
                data = pdf_responses[-1].body()
                if data[:1] in (b"{", b"["):
                    data = _extract_file_from_json(data)
                if data and len(data) > 500:
                    save_path.write_bytes(data)
                    _log(f"      ✓ {save_path.name}")
                    return "ok"
            except Exception as e:
                _log(f"      ⚠ network body: {e}")
        if _is_unavailable_dialog(page):
            _log(f"      ⚠ המסמך אינו זמין — מדלג: {save_path.name}")
            return "missing"
        _log(f"      ✗ לא הורד: {save_path.name}")
        return "fail"
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        _close_viewer(page)                     # never leave a blocking overlay


# ---------------------------------------------------------------------------
# Process a single case
# ---------------------------------------------------------------------------

def _expand_all_rows(page) -> None:
    """Click all 'hasChilds' (zoom_in) buttons to expand parent rows."""
    try:
        btns = page.locator("#hasChilds").all()
        if not btns:
            return
        _log(f"  פותח {len(btns)} שורות אב…")
        for btn in btns:
            try:
                if btn.is_visible(timeout=1000):
                    btn.click()
                    time.sleep(0.5)
            except Exception:
                pass
        time.sleep(1)
    except Exception:
        pass


def _harvest_parties(page) -> list[dict]:
    """Open the 'גורמים בתיק' tab and return EVERY party [{role,name,id,phone}] —
    both sides (זוכה + חייב + באי־כוח). Pure: navigates + reads, writes nothing.
    Tries several selectors so a markup tweak doesn't silently return nothing."""
    parties: list[dict] = []
    clicked = False
    for sel in ("a#CaseParties",
                "#CaseParties",
                'a[role="tab"]:has-text("גורמים")',
                'a:has-text("גורמים בתיק")',
                'span.bold:has-text("גורמים בתיק")'):
        try:
            tab = page.locator(sel).first
            if tab.count() > 0 and tab.is_visible(timeout=1500):
                tab.click()
                clicked = True
                time.sleep(2)
                break
        except Exception:
            continue
    if not clicked:
        _log("  ⚠ לא נמצא טאב 'גורמים בתיק' — מדלג על איסוף צדדים")
        return parties
    try:
        panels = page.locator("#table-groups-expansion mat-expansion-panel").all()
        if not panels:
            panels = page.locator("mat-expansion-panel").all()
        for panel in panels:
            header = panel.locator("mat-expansion-panel-header").first
            try:
                role = header.locator("span.bold").first.inner_text(timeout=1000).strip()
            except Exception:
                try:
                    role = header.inner_text(timeout=1000).strip().split("\n")[0]
                except Exception:
                    role = ""
            try:
                if header.get_attribute("aria-expanded") != "true":
                    header.click()
                    time.sleep(1.0)
            except Exception:
                pass
            # Strategy 1: read from table rows (cdk-column-* cells)
            rows = panel.locator("tr.mat-mdc-row, tr.mat-row").all()
            if not rows:
                rows = panel.locator("tr").all()
            for row in rows:
                rec = {"role": role}
                for col, key in [("name", "name"),
                                 ("authenticationNumber", "id"),
                                 ("phoneNumber", "phone")]:
                    try:
                        cell = row.locator(f"td.cdk-column-{col}")
                        if cell.count() == 0:
                            cell = row.locator(f"td.mat-column-{col}")
                        if cell.count() > 0:
                            inner = cell.locator("div.general-text")
                            if inner.count() > 0:
                                val = inner.first.inner_text(timeout=1000).strip()
                            else:
                                val = cell.inner_text(timeout=1000).strip()
                            if val:
                                rec[key] = val
                    except Exception:
                        pass
                if rec.get("name") and rec not in parties:
                    parties.append(rec)
            # Strategy 2 (fallback): app-case-party-general label pairs
            if not rows:
                for grp in panel.locator("app-case-party-general").all():
                    try:
                        labels = [x.strip() for x in grp.inner_text(timeout=1500).split("\n") if x.strip()]
                        rec = {"role": role}
                        for i in range(0, len(labels) - 1):
                            if labels[i] == "שם":
                                rec["name"] = labels[i + 1]
                            elif labels[i] == "זיהוי":
                                rec["id"] = labels[i + 1]
                        if rec.get("name") and rec not in parties:
                            parties.append(rec)
                    except Exception:
                        continue
    except Exception as e:
        _log(f"  ⚠ גורמים בתיק: {e}")
    return parties


# ---------------------------------------------------------------------------
# Which client does a case belong to?
# ---------------------------------------------------------------------------
# The card in the carousel shows the OTHER side (e.g. "הבנק הבינלאומי" — the
# זוכה). Filing the case under that name buried the lawyer's own client: case
# 529310-10-24 ended up under the bank instead of under דוד פונברשטיין, who is
# the חייב the office represents. So: prefer a party this installation already
# knows as a client, and only fall back to the card name when nothing matches.

_CASE_INDEX_NAME = ".eca_case_index.json"


def _norm_name(s: str) -> str:
    """Fold a party name for comparison. Quotes are DROPPED, not turned into
    spaces, so the portal's 'בע"מ' matches the folder's sanitized 'בעמ'."""
    s = re.sub(r"[\"'׳״]", "", s or "")
    return re.sub(r"[\s\-–—,.]+", " ", s).strip().lower()


def _load_case_index(downloads_dir: Path) -> dict:
    try:
        p = downloads_dir / _CASE_INDEX_NAME
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_case_index(downloads_dir: Path, index: dict) -> None:
    try:
        (downloads_dir / _CASE_INDEX_NAME).write_text(
            json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _known_clients(downloads_dir: Path) -> dict[str, int]:
    """Client folders already created by any portal → how many cases each holds.

    The count is what tells a real client from an opposing party: the office's
    own client recurs across many cases, the other side usually appears once."""
    out: dict[str, int] = {}
    try:
        for d in downloads_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            subs = [c for c in d.iterdir() if c.is_dir()]
            if not subs:            # a client folder holds case folders, not PDFs
                continue
            # portal folders (e.g. 'הוצאה לפועל') nest the cases one level deeper
            n = 0
            for s in subs:
                inner = [x for x in s.iterdir() if x.is_dir()]
                n += len(inner) if inner else 1
            out[d.name] = n
    except Exception:
        pass
    return out


def _resolve_client(case: dict, parties: list[dict], downloads_dir: Path) -> str:
    """Pick the client folder this case belongs under.

    The carousel card shows the OPPOSING side, so trusting it filed cases under
    the other party (case 529310-10-24 landed under the bank instead of under
    the חייב the office represents). Instead: of the case's real parties, take
    the one this installation already knows best — the client with the most
    existing cases wins. Falls back to the card name on a clean install."""
    card_party = (case.get("party") or "").strip()
    known = _known_clients(downloads_dir)
    by_norm: dict[str, tuple[str, int]] = {}
    for name, count in known.items():
        k = _norm_name(name)
        if count > by_norm.get(k, ("", -1))[1]:
            by_norm[k] = (name, count)

    # real parties only — 'בא כוח …' rows are the lawyers, never the client
    names = [(p.get("name") or "").strip() for p in (parties or [])
             if "בא כוח" not in (p.get("role") or "")]
    names = [n for n in names if n]

    best_name, best_count = "", -1
    for n in names:
        hit = by_norm.get(_norm_name(n))
        if hit and hit[1] > best_count:
            best_name, best_count = hit

    if best_name:
        if _norm_name(best_name) != _norm_name(card_party):
            _log(f"  ↳ לקוח זוהה: {best_name} ({best_count} תיקים קיימים) — "
                 f"ולא '{card_party}' שמופיע בכרטיס (הצד שכנגד)")
        return best_name

    # nothing known yet (clean install) — keep the card name, but prefer a real
    # party over an empty card so the folder is never 'ללא שם' for no reason.
    return card_party or (names[0] if names else "ללא שם")


def _process_case(page, case: dict, output_dir: Path, by_client: bool = False,
                  should_cancel=None, on_case_done=None) -> None:
    case_num = case["number"]

    _log(f"\n{'='*60}")
    _log(f"תיק: {case_num}  ({case.get('type', '')})")

    # Enter THIS case (by its own /he/caseinfo/<case> URL) before reading
    # anything — otherwise we would harvest the previous case's data.
    if not _open_motions_tab(page, case_num):
        _log(f"  ✗ דילוג על תיק {case_num}")
        return

    # Parties decide which client folder the case belongs to, so they must be
    # known BEFORE case_dir exists. Cache them per case in a single index at the
    # downloads root so repeat runs don't re-open the גורמים tab every time.
    index = _load_case_index(output_dir)
    entry = index.get(case_num) or {}
    parties = entry.get("parties") or []
    if not parties:
        parties = _harvest_parties(page)
        if parties:
            _log("  ✓ צדדים: " + ", ".join(
                f"{p.get('role','')}: {p.get('name','')}" for p in parties))
        # _harvest_parties left us on the גורמים tab — go back to the motions tab
        if not _open_motions_tab(page, case_num):
            _log(f"  ✗ דילוג על תיק {case_num} (לא חזר לטאב הבקשות)")
            return

    if by_client:
        client = _sanitize(entry.get("client")
                           or _resolve_client(case, parties, output_dir))
        case_dir = output_dir / client / "הוצאה לפועל" / _sanitize(case_num)
    else:
        client = ""
        case_dir = output_dir / _sanitize(case_num)

    index[case_num] = {"client": client, "parties": parties,
                       "type": case.get("type", ""), "dir": str(case_dir)}
    _save_case_index(output_dir, index)

    if parties and not (case_dir / "case_info.json").exists():
        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            (case_dir / "case_info.json").write_text(
                json.dumps({"parties": parties, "client": client,
                            "case_number": case_num}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            pass

    _maximize_paginator(page)
    time.sleep(1)
    _expand_all_rows(page)

    rows_data = _extract_rows(page)
    _log(f"  {len(rows_data)} שורות נמצאו")

    # Two rows can produce the very same "{הליך} - {תאריך} - {סוג} - {מגיש}"
    # name. _download_doc skips a path that already exists, so without a
    # disambiguating suffix the second document is silently never downloaded and
    # the case still reports as fully synced. Number the repeats instead.
    used: set[str] = set()

    def _unique(path: Path) -> Path:
        key = str(path).lower()
        if key not in used:
            used.add(key)
            return path
        for n in range(2, 100):
            cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
            if str(cand).lower() not in used:
                used.add(str(cand).lower())
                return cand
        return path

    for rd in rows_data:
        if should_cancel and should_cancel():
            _log(f"  ⏹ עצירת תיק {case_num} באמצע (בקשת המשתמש)")
            break
        proc = rd["process"]
        applicant = _sanitize(rd["applicant"] or "")
        date = _sanitize(rd["date"] or "")
        dec_date = _sanitize(rd["dec_date"] or "")
        proc_dir = case_dir / _sanitize(proc.split(".")[0])

        if rd["has_motion"]:
            target = _unique(proc_dir / _make_filename(proc, date, "בקשה", applicant))
            _log(f"    [{proc}] בקשה ({date}) — {target.name}")
            row = _row_locator(page, proc, rd["date"])
            _stats_tick(_download_doc(page, row, "motionDocumentID", target))

        if rd["has_decision"]:
            target = _unique(proc_dir / _make_filename(proc, dec_date, "החלטה", applicant))
            _log(f"    [{proc}] החלטה ({dec_date}) — {target.name}")
            row = _row_locator(page, proc, rd["date"])
            _stats_tick(_download_doc(page, row, "decisionDocumentID", target))

        time.sleep(0.3)

    _write_case_manifest(case_dir, case_num)
    # Hand the finished case to the importer straight away so it shows up in the
    # dashboard now, rather than only when the whole run ends.
    if on_case_done:
        try:
            on_case_done(case_dir, case_num)
        except Exception as _e:
            _log(f"  ⚠ ייבוא מיידי נכשל ({_e}) — ייובא בסוף הריצה")


def _write_case_manifest(case_dir: Path, case_num: str) -> None:
    """Write summary.csv + sync_history.csv in the LIAS manifest format so the
    dashboard importer indexes ECA documents like any other portal."""
    import csv as _csv
    if not case_dir.exists():
        return
    rows = []
    for pdf in sorted(case_dir.rglob("*.pdf")):
        rel = pdf.relative_to(case_dir)
        stem = pdf.stem
        parts = [x.strip() for x in stem.split(" - ")]
        # "{proc} - {date} - {סוג} - {מגיש}"
        date = parts[1] if len(parts) > 1 else ""
        if re.match(r"\d{2}\.\d{2}\.\d{2}$", date):
            d, m, y = date.split(".")
            date = f"{d}/{m}/20{y}"
        rows.append({
            "שם מסמך (מהטבלה)": stem,
            "שם קובץ פיזי בדיסק": str(rel),
            "סוג קובץ": parts[2] if len(parts) > 2 else "",
            "מגיש": parts[3] if len(parts) > 3 else "",
            "תאריך מסמך": date,
            "סטטוס הורדה": "Success",
            "גודל (KB)": str(round(pdf.stat().st_size / 1024, 1)),
        })
    if not rows:
        return
    with open(case_dir / "summary.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(case_dir / "sync_history.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["תאריך ריצה", "פורטל", "תיק"])
        w.writeheader()
        w.writerow({"תאריך ריצה": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "פורטל": "ECA", "תיק": case_num})


# ---------------------------------------------------------------------------
# Sync entry point (called from LIAS jobs on an already-open page)
# ---------------------------------------------------------------------------

def run_eca_download(page, root_output_dir: Path, cases_filter: list[str] | None = None,
                     progress=None, should_cancel=None, job_id=None,
                     on_case_done=None) -> str:
    """Download all ECA cases into root_output_dir/downloads/{client}/הוצאה לפועל/{case}.

    Assumes the caller provides a Playwright page; handles ECA login itself
    (reuses gov.il auto-login). `progress(fraction, message)` — optional
    callback so the job bar / tasks balloon reflect per-case progress.
    `should_cancel(case_num=None)` — return True to stop everything, or True for
    a specific case_num to skip just that case (per-case stop).
    Returns a short summary string.
    """
    install_eca_asset_fix(page)
    downloads_dir = Path(root_output_dir) / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(0.1, "מתחבר להוצאה לפועל…")
    if not _login_eca(page):
        raise RuntimeError("ההתחברות להוצאה לפועל נכשלה")

    if "/home/OpenCase" not in (page.url or ""):
        page.goto(OPEN_CASES_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

    cases = _extract_cases(page)
    if cases_filter:
        wanted = set(cases_filter)
        cases = [c for c in cases if c["number"] in wanted]
    if not cases:
        return "לא נמצאו תיקי הוצאה לפועל"

    def _cancel(case_num=None) -> bool:
        if not should_cancel:
            return False
        try:
            return bool(should_cancel(case_num))
        except TypeError:            # older callers pass a zero-arg callable
            return bool(should_cancel())

    _STATS.update(done=0, errors=0, total=0, case_idx=0,
                  cases_total=len(cases), t0=time.time(), job_id=job_id,
                  current_case="", current_name="",
                  output_dir=str(downloads_dir),
                  cases_detail=[{"id": c["number"],
                                 "name": c.get("party", ""),
                                 "type": c.get("type", ""),
                                 "status": "pending"} for c in cases])
    _by_id = {c["id"]: c for c in _STATS["cases_detail"]}
    ok = 0
    skipped = 0
    for case in cases:
        if _cancel():                                    # stop-all
            _log("⏹ ההורדה נעצרה על ידי המשתמש")
            return f"נעצר: {ok}/{len(cases)} תיקים ({_STATS['done']} מסמכים)"
        cnum = case["number"]
        if _cancel(cnum):                                # skip just this case
            _log(f"⏭ דילוג על תיק {cnum} (בוטל על ידי המשתמש)")
            if cnum in _by_id:
                _by_id[cnum]["status"] = "skipped"
            skipped += 1
            _STATS["case_idx"] += 1
            _broadcast_eca_stats()
            continue
        _STATS["case_idx"] += 1
        _STATS["current_case"] = cnum
        _STATS["current_name"] = case.get("party", "")
        if cnum in _by_id:
            _by_id[cnum]["status"] = "downloading"
        _broadcast_eca_stats()
        if progress:
            # spread cases across 0.15..0.85 of the bar
            frac = 0.15 + 0.70 * (_STATS["case_idx"] - 1) / max(len(cases), 1)
            progress(frac, f"תיק {_STATS['case_idx']}/{len(cases)}: {cnum}")
        try:
            _process_case(page, case, downloads_dir, by_client=True,
                          should_cancel=lambda: _cancel() or _cancel(cnum),
                          on_case_done=on_case_done)
            ok += 1
            if cnum in _by_id:
                _by_id[cnum]["status"] = "done"
        except Exception as e:
            _log(f"✗ שגיאה בתיק {cnum}: {e}")
            if cnum in _by_id:
                _by_id[cnum]["status"] = "failed"
        _broadcast_eca_stats()
        time.sleep(1)
    tail = f" ({skipped} דולגו)" if skipped else ""
    return f"הוצל\"פ: {ok}/{len(cases)} תיקים ({_STATS['done']} מסמכים, {_STATS['errors']} כשלו){tail}"


# ---------------------------------------------------------------------------
# Main
# NOTE: no __main__ / CLI entry point on purpose.
# ECA is an integrated part of the LIAS application — it runs through
# LIAS/collector_bridge.py (jobs: eca_list / eca_sync) on the app's shared
# BrowserManager. Run the app:  python3 app.py
