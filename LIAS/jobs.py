"""Job queue + workers — guide step 18 / תור משימות ו-Workers — שלב 18.

EN: Two kinds of workers:
    * N general workers (threads) for CPU/API work — they run in parallel.
    * The browser is NOT a worker: browser-bound jobs submit commands to
      BrowserManager and wait. So many jobs can be in flight, but portal
      actions are naturally serialized through the browser's command queue —
      the browser never gets contended, and nothing else ever blocks on it.
    Every state change is broadcast to subscribers (the API's SSE stream),
    which is how files/jobs appear live in the UI.
HE: שני סוגי Workers:
    * N עובדים כלליים (Threads) לעבודת CPU/API — רצים במקביל.
    * הדפדפן אינו Worker: משימות שצריכות דפדפן שולחות פקודות ל-BrowserManager
      וממתינות. כך הרבה משימות באוויר, אבל פעולות הפורטל מסודרות טבעית דרך
      תור הפקודות של הדפדפן — הדפדפן לא נחנק, ושום דבר אחר לא נתקע עליו.
    כל שינוי מצב משודר למנויים (זרם ה-SSE של ה-API) — כך קבצים ומשימות
    מופיעים חיים ב-UI.
"""
from __future__ import annotations

import json
import queue
import threading
import traceback
from typing import Any, Callable, Optional

from . import config, db

# registry: kind -> handler(payload, ctx) / רישום: סוג ← מטפל
_HANDLERS: dict[str, Callable[[dict, "JobContext"], Any]] = {}

# global reference to the running WorkerPool (set by run.py)
_pool: "Optional[WorkerPool]" = None

# OTP handshake — browser thread waits, UI submits the code
_otp_event = threading.Event()
_otp_value: str = ""


def request_otp_from_ui(timeout: float = 180.0) -> str:
    """Block the calling thread until the user submits an OTP via the UI."""
    global _otp_value
    _otp_value = ""
    _otp_event.clear()
    broadcast({"type": "otp_required", "message": "הזן קוד OTP שנשלח לאימייל / טלפון"})
    if _otp_event.wait(timeout=timeout):
        return _otp_value
    return ""


def submit_otp(code: str) -> None:
    global _otp_value
    _otp_value = code.strip()
    _otp_event.set()

# subscribers for live events (SSE) / מנויים לאירועים חיים
_subscribers: list["queue.Queue[dict]"] = []
_sub_lock = threading.Lock()


def handler(kind: str):
    """Decorator to register a job handler / דקורטור לרישום מטפל."""
    def deco(fn):
        _HANDLERS[kind] = fn
        return fn
    return deco


def broadcast(event: dict) -> None:
    """Push event to all SSE subscribers / דחיפת אירוע לכל מנויי ה-SSE."""
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def subscribe() -> "queue.Queue[dict]":
    q: "queue.Queue[dict]" = queue.Queue(maxsize=500)
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: "queue.Queue[dict]") -> None:
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


class JobContext:
    """EN: passed to every handler — progress reporting + shared services.
    HE: מועבר לכל מטפל — דיווח התקדמות + שירותים משותפים."""

    def __init__(self, job_id: int, browser=None, bdr_browser=None):
        self.job_id = job_id
        self.browser = browser            # BrowserManager or None
        self.bdr_browser = bdr_browser    # separate BDR BrowserManager or None

    def progress(self, fraction: float, message: str = "") -> None:
        """Update DB + notify UI in one call / עדכון DB והודעה ל-UI בקריאה אחת."""
        db.update_job(self.job_id, progress=round(fraction, 3), message=message)
        broadcast({"type": "job", "job_id": self.job_id,
                   "progress": round(fraction, 3), "message": message})

    def file_event(self, document_id: int, status: str, name: str = "") -> None:
        """EN: per-file live update — this is the 'smart file management in the
            UI' hook: every download/skip/error appears instantly.
        HE: עדכון חי לכל קובץ — זה הוו של 'ניהול קבצים חכם ב-UI':
            כל הורדה/דילוג/שגיאה מופיעים מיידית."""
        broadcast({"type": "file", "document_id": document_id,
                   "status": status, "name": name})


def submit(kind: str, payload: Optional[dict] = None) -> int:
    """Create job + wake workers / יצירת משימה והערת ה-Workers.

    Dedupe: an identical job already PENDING/RUNNING is returned instead of
    queued again — repeated clicks must not pile up browser jobs.
    כפילות: משימה זהה שכבר ממתינה/רצה מוחזרת במקום להיערם בתור."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT job_id FROM jobs WHERE kind=? AND state IN ('PENDING','RUNNING') "
        "AND COALESCE(payload_json,'{}')=? ORDER BY job_id DESC LIMIT 1",
        (kind, json.dumps(payload or {}, ensure_ascii=False))).fetchone()
    if row:
        broadcast({"type": "job", "job_id": row["job_id"], "state": "PENDING",
                   "kind": kind, "message": "כבר בתור — לא נוספה משימה כפולה"})
        return row["job_id"]
    job_id = db.create_job(kind, payload)
    broadcast({"type": "job", "job_id": job_id, "state": "PENDING", "kind": kind})
    return job_id


class WorkerPool:
    def __init__(self, browser=None, n_workers: int = config.JOB_WORKERS,
                 log: Callable[[str], None] = print, bdr_browser=None):
        self._browser = browser
        self._bdr_browser = bdr_browser
        self._log = log
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._loop, name=f"lias-worker-{i}", daemon=True)
            for i in range(n_workers)
        ]

    def start(self) -> None:
        # EN: jobs that were RUNNING when the process died are marked INTERRUPTED
        #     (not PENDING) so they do NOT auto-resume on startup — the user
        #     decides whether to restart them.
        # HE: משימות שהיו RUNNING כשהתהליך מת מסומנות INTERRUPTED (לא PENDING)
        #     כדי שלא יחזרו לרוץ אוטומטית — המשתמש מחליט אם להפעיל שוב.
        conn = db.get_conn()
        # PENDING jobs from a previous run must not fire on startup either —
        # that is what re-opened gov.il login (and sent an OTP) after a kill.
        conn.execute(
            "UPDATE jobs SET state='ERROR', error='הופסק — הפעלה מחדש' "
            "WHERE state IN ('RUNNING', 'PENDING')"
        )
        conn.commit()
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        import time
        while not self._stop.is_set():
            row = db.claim_next_job(list(_HANDLERS.keys()))
            if row is None:
                time.sleep(1.0)               # simple poll; SSE hides the latency / פולינג פשוט
                continue
            job_id, kind = row["job_id"], row["kind"]
            payload = json.loads(row["payload_json"] or "{}")
            ctx = JobContext(job_id, browser=self._browser, bdr_browser=self._bdr_browser)
            broadcast({"type": "job", "job_id": job_id, "state": "RUNNING", "kind": kind})
            try:
                result = _HANDLERS[kind](payload, ctx)
                db.update_job(job_id, state="COMPLETED", progress=1.0,
                              message=str(result)[:300] if result else "done")
                broadcast({"type": "job", "job_id": job_id, "state": "COMPLETED",
                           "kind": kind})
                # EN: self-healing cache — after every sync-type job, silently
                #     purge DB records whose files vanished from disk, so the
                #     user never has to press "refresh" or clean manually.
                # HE: קאש שמרפא את עצמו — אחרי כל משימת סנכרון, ניקוי שקט של
                #     רשומות שהקובץ שלהן נעלם מהדיסק. בלי ריענון ידני.
                _SYNC_KINDS = {"net_scan", "net_sync_current", "bdr_sync_current",
                               "net_auto_update", "bdr_batch", "net_date_search",
                               "reimport_csv"}
                if kind in _SYNC_KINDS:
                    try:
                        submit("purge_stale", {"mode": "missing"})
                    except Exception:
                        pass
            except Exception as e:
                # EN: one bad job never kills the worker — log, mark, continue.
                # HE: משימה רעה אחת לא הורגת את ה-Worker — לוג, סימון, ממשיכים.
                err = f"{type(e).__name__}: {e}"
                self._log(f"[job {job_id}] failed / נכשלה: {err}\n" +
                          traceback.format_exc(limit=2))
                db.update_job(job_id, state="ERROR", error=err[:500])
                broadcast({"type": "job", "job_id": job_id, "state": "ERROR",
                           "kind": kind, "error": err[:200]})
