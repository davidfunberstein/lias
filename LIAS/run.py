"""LIAS entry point / נקודת הכניסה של LIAS.

EN: Wires everything: DB init → browser thread (its OWN thread, never the
    main one) → worker pool → FastAPI+UI on the main thread. Ctrl+C shuts
    down cleanly: workers stop, browser closes through its command queue.
HE: מחבר הכל: אתחול DB ← Thread הדפדפן (Thread משלו, לעולם לא הראשי) ←
    בריכת ה-Workers ← FastAPI+UI על ה-Thread הראשי. Ctrl+C סוגר נקי:
    ה-Workers נעצרים, הדפדפן נסגר דרך תור הפקודות שלו.

Run / הרצה (from gov-il-connect-v2 root / מתיקיית השורש):
    python3 -m LIAS.run          →  http://localhost:8400
Or directly from anywhere / או ישירות מכל מקום:
    python3 LIAS/run.py
"""
from __future__ import annotations

import sys

# EN: allow direct execution (python3 run.py) — put the project root on the
#     path and restart as a proper package module.
# HE: מאפשר הרצה ישירה (python3 run.py) — מוסיף את שורש הפרויקט לנתיב
#     ומתחיל מחדש כמודול חבילה תקין.
if __package__ in (None, ""):
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import runpy
    runpy.run_module("LIAS.run", run_name="__main__")
    sys.exit(0)

from . import config, db, jobs
from . import collector_bridge  # noqa: F401  registers handlers / רושם מטפלים


def _restore(page) -> None:
    """After a browser relaunch: close any stale login.gov.il tabs (they would
    trigger an unwanted auto-login), then navigate the main page to a neutral
    blank so the persistent profile's cookies are preserved without forcing
    any portal navigation."""
    try:
        for p in page.context.pages:
            if not p.is_closed() and "login.gov.il" in (p.url or ""):
                try:
                    p.close()
                except Exception:
                    pass
    except Exception:
        pass
    # Navigate to blank — avoids login redirect on stale sessions.
    try:
        page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
    except Exception:
        pass


def _cleanup_stale() -> None:
    """Kill any stale process on our port and remove a stale SingletonLock."""
    import os
    import signal
    import socket
    import subprocess
    import time
    from pathlib import Path

    # Remove stale Chromium SingletonLock (left when process crashed)
    lock = Path(__file__).parent / "browser_profile" / "SingletonLock"
    if lock.exists():
        try:
            lock.unlink()
            print("removed stale SingletonLock / הוסר SingletonLock ישן")
        except OSError:
            pass

    # Kill any process still listening on our port, then poll until free
    port = config.API_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return  # port is free, nothing to do

    try:
        result = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], text=True
        ).strip()
        for pid_str in result.splitlines():
            try:
                os.kill(int(pid_str), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        print(f"killed stale process on port {port} / הרגנו process ישן על פורט {port}")
    except Exception:
        pass

    # Wait until the port is actually free (up to 5 s)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.2)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return
    print(f"warning: port {port} still in use after 5 s / אזהרה: פורט עדיין תפוס")


def _open_ui_when_ready(url: str) -> None:
    """Open the system browser to the LIAS UI once the server is up."""
    import os
    import socket
    import time
    import webbrowser

    # Launched from the NEW dashboard? everything is managed there — do not
    # open the old UI tab. / הופעל מהדשבורד החדש — לא פותחים טאב לישן.
    if os.environ.get("LIAS_NO_TAB") == "1":
        return

    deadline = time.time() + 15.0
    while time.time() < deadline:
        time.sleep(0.3)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", config.API_PORT)) == 0:
                webbrowser.open_new_tab(url)
                return


def _init_lias_logger() -> None:
    """Redirect all print() output to both terminal and latest.log so the
    LIAS UI log tab shows auth/browser messages in real-time."""
    import sys
    from pathlib import Path
    from . import config

    logs_dir = config.COURT_DOCS_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "latest.log"

    # Rotate previous log
    if log_path.exists() and log_path.stat().st_size > 0:
        for idx in range(19, 0, -1):
            cur = logs_dir / f"log_{idx}.log"
            nxt = logs_dir / f"log_{idx + 1}.log"
            if cur.exists():
                if idx == 19 and nxt.exists():
                    try: nxt.unlink()
                    except Exception: pass
                try: cur.rename(nxt)
                except Exception: pass
        try: log_path.rename(logs_dir / "log_1.log")
        except Exception: pass

    _log_fh = open(log_path, "a", encoding="utf-8", buffering=1)

    class _Tee:
        def __init__(self, *streams): self._s = streams
        def write(self, data):
            for s in self._s:
                try: s.write(data)
                except Exception: pass
        def flush(self):
            for s in self._s:
                try: s.flush()
                except Exception: pass
        def fileno(self): return self._s[0].fileno()
        def isatty(self): return self._s[0].isatty()
        def __getattr__(self, name): return getattr(self._s[0], name)

    sys.stdout = _Tee(sys.__stdout__, _log_fh)
    sys.stderr = _Tee(sys.__stderr__, _log_fh)


def main() -> int:
    import threading

    print("LIAS starting / LIAS עולה…")
    _cleanup_stale()
    _init_lias_logger()
    # Mark running context so all input() prompts are suppressed.
    try:
        from core.download import SESSION_SETTINGS
        SESSION_SETTINGS["lias_mode"] = True
        # load saved settings (otp_method, login_method, share_email…) so the
        # engine honors them from the first job / טעינת ההגדרות השמורות מיד
        from core.runner import _load_persistent_settings
        _load_persistent_settings()
    except Exception:
        pass
    db.init_db()

    # 1) browser — dedicated thread, optional / דפדפן — Thread ייעודי, אופציונלי
    browser = None
    bdr_browser = None
    try:
        from .browser_manager import BrowserManager
        import playwright  # noqa: F401 — presence check / בדיקת נוכחות
        # Visible by default for now — the user follows the automation live.
        # Set LIAS_HEADLESS=1 to hide once the flows are trusted.
        import os as _os
        _headless = _os.environ.get("LIAS_HEADLESS") == "1"
        # Lazy: the manager exists, but Chrome does not launch until a job for
        # this portal actually runs (BrowserManager.run -> ensure_started).
        # Launching all three at boot exhausted the macOS file-descriptor limit
        # and put every browser thread into a crash/relaunch loop.
        browser = BrowserManager(headless=_headless, restore=_restore)
        print("browser manager ready (NET) — Chrome starts on first use")
        # Separate BDR browser with its own profile
        bdr_browser = BrowserManager(
            headless=_headless, restore=_restore,
            profile_dir=config.BROWSER_PROFILE_BDR_DIR,
            log=lambda m: print(f"[BDR] {m}"),
        )
        print("browser manager ready (BDR) — Chrome starts on first use")
    except ImportError:
        print("!! playwright not installed — browser jobs disabled, UI/DB still work")
        print("!! playwright לא מותקן — משימות דפדפן כבויות, ה-UI וה-DB עובדים")

    # 2) workers / עובדים
    pool = jobs.WorkerPool(browser=browser, bdr_browser=bdr_browser)
    pool.start()
    jobs._pool = pool
    print(f"{config.JOB_WORKERS} workers up / עובדים למעלה")

    # 3) open UI in system browser once server is ready / פתח UI בדפדפן המערכת
    ui_url = f"http://{config.API_HOST}:{config.API_PORT}"
    t = threading.Thread(target=_open_ui_when_ready, args=(ui_url,), daemon=True)
    t.start()

    # 4) API + UI (main thread) / שרת ו-UI (ה-Thread הראשי)
    # EN: prefer FastAPI/uvicorn when installed; otherwise fall back to the
    #     zero-dependency stdlib server (httpd.py).
    # HE: מעדיפים FastAPI/uvicorn; אחרת נסיגה לשרת ספריית-התקן.
    try:
        import uvicorn
        from .api import app
        use_fastapi = True
    except ImportError:
        use_fastapi = False
        print("fastapi/uvicorn not found — using built-in server / לא נמצאו — שרת מובנה")
    # Clean shutdown on SIGTERM too (ui_demo stops the engine that way) —
    # otherwise the Playwright browser is orphaned and the port stays busy.
    import signal
    _closed = {"done": False}

    def _graceful_close(*_a):
        if _closed["done"]:
            return
        _closed["done"] = True
        print("shutting down / נסגר…")
        try:
            pool.stop()
        except Exception:
            pass
        try:
            if browser:
                browser.shutdown()
        except Exception:
            pass
        try:
            if bdr_browser:
                bdr_browser.shutdown()
        except Exception:
            pass

    for _sig_name in ("SIGTERM", "SIGHUP"):
        try:
            signal.signal(getattr(signal, _sig_name),
                          lambda *_: (_graceful_close(), sys.exit(0)))
        except Exception:
            pass
    import atexit as _atexit
    _atexit.register(_graceful_close)
    import atexit
    atexit.register(_graceful_close)

    try:
        if use_fastapi:
            print(f"UI: {ui_url}")
            uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level="info")
        else:
            from . import httpd
            httpd.serve()
    except KeyboardInterrupt:
        pass
    finally:
        _graceful_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
