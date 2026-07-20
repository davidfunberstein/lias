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
                "browser": None, "bdr": None, "pool": None, "err": ""}
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
        _state["starting"] = True

    try:
        from LIAS import config, db, jobs
        from LIAS import collector_bridge  # noqa: F401 — registers handlers

        try:
            from core.download import SESSION_SETTINGS
            SESSION_SETTINGS["lias_mode"] = True
            from core.runner import _load_persistent_settings
            _load_persistent_settings()
        except Exception:
            pass

        db.init_db()

        browser = bdr_browser = None
        try:
            from LIAS.browser_manager import BrowserManager
            import playwright  # noqa: F401
            import os as _os
            from LIAS.run import _restore
            # Headless by DEFAULT — two Chrome windows popping open (NET+BDR)
            # confused users. The 🖥 toggle / WAF ladder shows them on demand.
            _headless = _os.environ.get("LIAS_HEADFUL") != "1"
            browser = BrowserManager(headless=_headless, restore=_restore)
            browser.start()
            bdr_browser = BrowserManager(
                headless=_headless, restore=_restore,
                profile_dir=config.BROWSER_PROFILE_BDR_DIR,
                log=lambda m: print(f"[BDR] {m}"),
            )
            bdr_browser.start()
            print("[engine] browser threads up (NET+BDR)")
        except ImportError:
            print("[engine] playwright missing — browser jobs disabled")

        pool = jobs.WorkerPool(browser=browser, bdr_browser=bdr_browser)
        pool.start()
        jobs._pool = pool

        # In-memory ASGI client to the SAME FastAPI app the old server used
        from LIAS.api import app as fastapi_app
        from starlette.testclient import TestClient
        client = TestClient(fastapi_app, base_url="http://engine.internal")

        _state.update(started=True, starting=False, client=client,
                      browser=browser, bdr=bdr_browser, pool=pool, err="")
        print("[engine] in-process engine up — port 8400 is GONE")
        return {"ok": True, "starting": True}
    except Exception as exc:  # pragma: no cover
        _state.update(starting=False, err=str(exc))
        print(f"[engine] start failed: {exc}")
        return {"ok": False, "error": str(exc)}


def stop() -> bool:
    with _lock:
        if not _state["started"]:
            return False
        _state["started"] = False
    try:
        _state["pool"] and _state["pool"].stop()
    except Exception:
        pass
    for key in ("browser", "bdr"):
        try:
            _state[key] and _state[key].shutdown()
        except Exception:
            pass
    _state.update(client=None, browser=None, bdr=None, pool=None)
    print("[engine] in-process engine stopped")
    return True


def restart() -> dict:
    stop()
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
