"""In-process LIAS engine — the merge of the old 8400 server into the app.

EN: Runs the SAME engine (browsers, worker pool, FastAPI routes) inside the
    app.py process. No subprocess, no port 8400, no proxy over HTTP — API
    calls are dispatched straight into the FastAPI app via an in-memory
    ASGI client (httpx/TestClient), and SSE events are read directly from
    jobs.subscribe().
HE: אותו מנוע בדיוק, בתוך תהליך האפליקציה: בלי subprocess, בלי פורט 8400,
    בלי proxy — הקריאות עוברות ישירות ל-FastAPI בזיכרון.
"""
from __future__ import annotations

import threading

_state: dict = {"started": False, "starting": False, "client": None,
                "browser": None, "bdr": None, "pool": None, "err": "",
                "fatal": False}
_lock = threading.Lock()


def alive() -> bool:
    return _state["started"]


def start() -> dict:
    """Start the engine in-process (idempotent). Mirrors LIAS/run.py main()
    minus uvicorn: settings → db → browser threads → worker pool → ASGI client."""
    with _lock:
        if _state["started"]:
            return {"ok": True, "already": True}
        if _state["starting"]:
            return {"ok": True, "starting": True}
        if _state.get("fatal"):
            return {"ok": False, "error": _state["err"], "fatal": True}
        _state["starting"] = True

    try:
        from LIAS import config, db, jobs
        from LIAS import collector_bridge  # noqa: F401 — registers handlers

        # Tee engine stdout/stderr into court_documents/logs/latest.log so the
        # in-app live-log window (which reads /api/log → latest.log) actually
        # updates. Without this, engine output only hit app.py's stdout and the
        # log window showed stale content from the old separate-process engine.
        try:
            from LIAS.run import _init_lias_logger
            _init_lias_logger()
        except Exception as _le:
            print(f"[engine] log init skipped: {_le}")

        try:
            from core.download import SESSION_SETTINGS
            SESSION_SETTINGS["lias_mode"] = True
            from core.runner import _load_persistent_settings
            _load_persistent_settings()
        except Exception:
            pass

        db.init_db()

        browser = bdr_browser = eca_browser = None
        try:
            from LIAS.browser_manager import BrowserManager
            import playwright  # noqa: F401
            from LIAS.run import _restore
            # Start BOTH browsers HEADLESS so opening the app never pops windows.
            # A portal's window appears only when you actually sync it (each
            # portal job calls browser.show() when the "browser_visible"
            # setting is on) — so you see login + download for the one you run,
            # not two stray windows at startup.
            _headless = True
            browser = BrowserManager(headless=_headless, restore=_restore)
            bdr_browser = BrowserManager(
                headless=_headless, restore=_restore,
                profile_dir=config.BROWSER_PROFILE_BDR_DIR,
                log=lambda m: print(f"[BDR] {m}"),
            )
            # Dedicated ECA browser → ECA runs in parallel with NET and BDR.
            # Seed its profile from the main one (cold copy) so the existing
            # gov.il session carries over instead of forcing a fresh OTP.
            try:
                import shutil as _sh
                if not config.BROWSER_PROFILE_ECA_DIR.exists() and \
                   config.BROWSER_PROFILE_DIR.exists():
                    _sh.copytree(config.BROWSER_PROFILE_DIR,
                                 config.BROWSER_PROFILE_ECA_DIR,
                                 ignore=_sh.ignore_patterns("Singleton*"))
                    print("[engine] seeded ECA profile from main profile")
            except Exception as _pe:
                print(f"[engine] ECA profile seed skipped: {_pe}")
            eca_browser = BrowserManager(
                headless=_headless, restore=_restore,
                profile_dir=config.BROWSER_PROFILE_ECA_DIR,
                log=lambda m: print(f"[ECA] {m}"),
            )
            print("[engine] browser managers ready (NET+BDR+ECA) — "
                  "each Chrome starts on first use")
        except ImportError:
            print("[engine] playwright missing — browser jobs disabled")

        pool = jobs.WorkerPool(browser=browser, bdr_browser=bdr_browser,
                               eca_browser=eca_browser)
        pool.start()
        jobs._pool = pool

        # In-memory ASGI client to the SAME FastAPI app the old server used.
        # Check httpx ourselves: starlette raises a RuntimeError naming a
        # package that does not always match what actually needs installing
        # (one version told users to "pip install httpx2", which does not exist
        # on PyPI — so following the advice failed too).
        try:
            import httpx  # noqa: F401
        except ModuleNotFoundError:
            raise RuntimeError(
                "חסרה הספרייה httpx — המנוע לא יכול לעלות בלעדיה.\n"
                "        התקן:  python3 -m pip install httpx\n"
                "        או פשוט הרץ:  bash start.sh  (מתקין הכל לבד)"
            ) from None
        from LIAS.api import app as fastapi_app
        from starlette.testclient import TestClient
        client = TestClient(fastapi_app, base_url="http://engine.internal")

        _state.update(started=True, starting=False, client=client,
                      browser=browser, bdr=bdr_browser, eca=eca_browser,
                      pool=pool, err="")
        print("[engine] in-process engine up — port 8400 is GONE")
        return {"ok": True, "starting": True}
    except Exception as exc:  # pragma: no cover
        # A missing dependency cannot fix itself, but request() calls start()
        # whenever the engine is down — so every UI poll retried and reprinted
        # the same failure, thousands of times, burying it. Remember a fatal
        # error and report it without re-attempting.
        msg = str(exc)
        fatal = isinstance(exc, (ModuleNotFoundError, ImportError)) or "httpx" in msg
        _state.update(starting=False, err=msg, fatal=fatal)
        print(f"[engine] start failed: {msg}")
        if fatal:
            print("[engine] ⛔ שגיאה שלא תיפתר מעצמה — לא ינוסה שוב עד להפעלה מחדש.")
        return {"ok": False, "error": msg}


def stop() -> bool:
    with _lock:
        if not _state["started"]:
            return False
        _state["started"] = False
    try:
        _state["pool"] and _state["pool"].stop()
    except Exception:
        pass
    for key in ("browser", "bdr", "eca"):
        try:
            _state[key] and _state[key].shutdown()
        except Exception:
            pass
    _state.update(client=None, browser=None, bdr=None, pool=None)
    print("[engine] in-process engine stopped")
    return True


def restart() -> dict:
    stop()
    _state["fatal"] = False        # a restart is the user's "I fixed it" signal
    import time
    time.sleep(1.0)
    return start()


def request(method: str, path: str, body: bytes = b"",
            content_type: str = "", timeout: float = 300.0) -> tuple[int, bytes, str]:
    """Dispatch an HTTP-style request straight into the engine's FastAPI app.
    Returns (status, body, content_type). Starts the engine on first use."""
    if not _state["started"]:
        start()
    client = _state["client"]
    if client is None:
        import json
        return 502, json.dumps({"error": "engine offline",
                                "detail": _state["err"]}).encode(), "application/json"
    headers = ({"Content-Type": content_type} if content_type
               else {"Content-Type": "application/json"} if body else {})
    try:
        r = client.request(method, path, content=body or None, headers=headers)
        return r.status_code, r.content, r.headers.get("content-type",
                                                       "application/json")
    except Exception as exc:
        import json
        return 502, json.dumps({"error": str(exc)}).encode(), "application/json"


def events_queue():
    """Direct subscription to engine events (for SSE) — no HTTP hop."""
    if not _state["started"]:
        start()
    from LIAS import jobs
    return jobs
