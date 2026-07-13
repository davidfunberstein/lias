"""Engine control -- start/stop/restart the full LIAS system."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from urllib.parse import quote


def _start_engine(here: str, full_ui_port: int,
                  engine_proc_holder: dict,
                  full_ui_alive_fn) -> dict:
    """Launch the full LIAS system in the background."""
    if full_ui_alive_fn():
        return {"ok": True, "already": True}
    proc = engine_proc_holder.get("proc")
    if proc is not None and proc.poll() is None:
        return {"ok": True, "starting": True}
    import subprocess
    log = open(os.path.join(here, "lias_engine.log"), "ab")
    env = dict(os.environ, LIAS_NO_TAB="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "LIAS.run"],
        cwd=here, stdout=log, stderr=log, env=env,
        start_new_session=True,
    )
    engine_proc_holder["proc"] = proc
    return {"ok": True, "starting": True}


def _stop_engine(full_ui_port: int, engine_proc_holder: dict) -> bool:
    """Stop the full LIAS engine gracefully."""
    import signal
    import subprocess
    import time

    pids: set[int] = set()
    proc = engine_proc_holder.get("proc")
    if proc is not None and proc.poll() is None:
        pids.add(proc.pid)
    try:
        out = subprocess.check_output(["lsof", "-ti", f":{full_ui_port}"], text=True)
        pids.update(int(p) for p in out.split())
    except Exception:
        pass
    if not pids:
        return False

    def _kill(pid: int, sig) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    for pid in pids:
        _kill(pid, signal.SIGTERM)

    deadline = time.time() + 10.0
    alive: list[int] = []
    while time.time() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        if not alive:
            break
        time.sleep(0.4)
    for pid in alive:
        _kill(pid, signal.SIGKILL)

    try:
        subprocess.run(["pkill", "-f", "browser_profile"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    return True


def _shutdown_all(reason: str, full_ui_port: int,
                  engine_proc_holder: dict,
                  server_ref: dict,
                  shutting_down_flag: dict) -> None:
    """Safe shutdown: stop engine + this server."""
    if shutting_down_flag.get("value"):
        return
    shutting_down_flag["value"] = True
    print(f"\n[shutdown] {reason} — closing engine and server / סוגר מנוע ושרת…")
    stopped = _stop_engine(full_ui_port, engine_proc_holder)
    print(f"[shutdown] engine {'stopped' if stopped else 'was not running'}")
    server = server_ref.get("server")
    if server is not None:
        threading.Thread(target=server.shutdown, daemon=True).start()


def _restart_engine(here: str, full_ui_port: int,
                    engine_proc_holder: dict,
                    full_ui_alive_fn) -> dict:
    """Restart ONLY the engine component."""
    _stop_engine(full_ui_port, engine_proc_holder)
    import time
    time.sleep(1.0)
    return _start_engine(here, full_ui_port, engine_proc_holder, full_ui_alive_fn)


def _autoreload_watcher(shutting_down_flag: dict) -> None:
    """Re-exec server when run_ui_demo.py changes on disk."""
    import time
    me = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "run_ui_demo.py"))
    try:
        last = os.path.getmtime(me)
    except OSError:
        return
    while not shutting_down_flag.get("value"):
        time.sleep(2.0)
        try:
            cur = os.path.getmtime(me)
        except OSError:
            continue
        if cur != last:
            print("[autoreload] run_ui_demo.py changed — restarting server / "
                  "הקובץ השתנה — השרת מתחלף")
            argv = [sys.executable, me] + [a for a in sys.argv[1:] if a != "--no-browser"]
            argv.append("--no-browser")
            os.execv(sys.executable, argv)


def _watchdog(full_ui_port: int, shutting_down_flag: dict,
              heartbeat_holder: dict,
              shutdown_all_fn) -> None:
    """If all UI tabs disappear, shut everything down."""
    import time
    if os.environ.get("LIAS_KEEP_ALIVE") == "1":
        return
    HEARTBEAT_GRACE = 300.0

    def _engine_busy() -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{full_ui_port}/api/jobs?limit=10", timeout=5) as r:
                jobs_ = json.loads(r.read())
            return any(j.get("state") == "RUNNING" for j in jobs_)
        except Exception:
            return False

    while not shutting_down_flag.get("value"):
        time.sleep(5.0)
        last_hb = heartbeat_holder.get("value", 0.0)
        if last_hb and (time.time() - last_hb) > HEARTBEAT_GRACE:
            if _engine_busy():
                continue
            shutdown_all_fn("UI closed (no heartbeat)")
            return


def _proxy_post(path: str, body: bytes, full_ui_port: int) -> tuple[int, bytes]:
    """Forward an action to the full system."""
    import urllib.request
    raw_path, _, query = path.partition("?")
    if not re.match(r"^actions/[\w/]+$", raw_path):
        return 403, b'{"error":"forbidden"}'
    url = f"http://127.0.0.1:{full_ui_port}/api/{quote(raw_path, safe='/')}"
    if query:
        url += "?" + query
    try:
        req = urllib.request.Request(url, data=body or b"", method="POST",
                                     headers={"Content-Type": "application/json"} if body else {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except Exception as exc:
        return 502, json.dumps({"error": str(exc)}, ensure_ascii=False).encode()


def _proxy_settings(method: str, body: bytes, full_ui_port: int) -> tuple[int, bytes]:
    """Read/write engine settings."""
    import urllib.request
    url = f"http://127.0.0.1:{full_ui_port}/api/settings"
    try:
        req = urllib.request.Request(url, data=body if method == "POST" else None,
                                     method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except Exception as exc:
        return 502, json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
