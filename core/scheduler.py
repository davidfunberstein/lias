"""Auto-sync scheduler.

Reads session_defaults.json every minute. When auto_sync_enabled is True
and enough time has elapsed since the last sync, it submits download jobs
for every enabled portal.

Started once from LIAS/run.py.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

_POLL_SEC  = 60      # how often we check
_LAST_SYNC: datetime | None = None
_lock = threading.Lock()


def _load_settings(project_root: Path) -> dict:
    p = project_root / "session_defaults.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_last_sync(project_root: Path, ts: datetime) -> None:
    p = project_root / "session_defaults.json"
    d: dict = {}
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    d["auto_sync_last_run"] = ts.isoformat()
    try:
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run(project_root: Path) -> None:
    global _LAST_SYNC

    while True:
        time.sleep(_POLL_SEC)
        try:
            d = _load_settings(project_root)
            if not d.get("auto_sync_enabled"):
                continue

            interval_h = float(d.get("auto_sync_interval_hours", 4))
            last_str   = d.get("auto_sync_last_run", "")

            with _lock:
                if last_str:
                    try:
                        last_dt = datetime.fromisoformat(last_str)
                    except Exception:
                        last_dt = None
                else:
                    last_dt = _LAST_SYNC

                if last_dt and datetime.now() - last_dt < timedelta(hours=interval_h):
                    continue  # not yet due

                # Due — trigger download
                _LAST_SYNC = datetime.now()
                _save_last_sync(project_root, _LAST_SYNC)

            from LIAS import jobs

            submitted = []
            if d.get("portal_net_enabled", True):
                submitted.append(jobs.submit("net_auto_update"))
            if d.get("portal_bdr_enabled", True):
                submitted.append(jobs.submit("bdr_batch", {}))
            if d.get("portal_eca_enabled", True) and "eca_sync" in jobs._HANDLERS:
                submitted.append(jobs.submit("eca_sync", {}))

            print(f"[scheduler] auto-sync triggered — {len(submitted)} jobs at {_LAST_SYNC.strftime('%H:%M')}")

            # Send log email after sync jobs complete (give them 10 min)
            if d.get("log_email_enabled") and d.get("log_email_to"):
                threading.Thread(
                    target=_send_log_after_delay,
                    args=(project_root, d.get("log_email_to", ""), 600),
                    daemon=True,
                ).start()

        except Exception as e:
            print(f"[scheduler] error: {e}")


def _send_log_after_delay(project_root: Path, to_addr: str, delay_sec: int) -> None:
    time.sleep(delay_sec)
    try:
        send_log_email(project_root, to_addr, subject="LIAS — auto-sync log")
    except Exception as e:
        print(f"[scheduler] log email failed: {e}")


def send_log_email(project_root: Path, to_addr: str, subject: str = "LIAS — log") -> None:
    """Send the last 300 lines of latest.log to to_addr.

    Credentials are read from session_defaults.json keys:
      log_smtp_user     — sender Gmail address
      log_smtp_password — Gmail App Password (16-char)
    Falls back to gov-il-connect keychain if those are absent.
    """
    import smtplib
    from email.mime.text import MIMEText

    log_path = project_root / "court_documents" / "logs" / "latest.log"
    if not log_path.exists():
        raise FileNotFoundError(f"קובץ לוג לא נמצא: {log_path}")

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
    body  = "\n".join(lines)

    # 1) Try dedicated log-email credentials from session_defaults.json
    d = _load_settings(project_root)
    email_addr = (d.get("log_smtp_user") or "").strip()
    email_pw   = (d.get("log_smtp_password") or "").strip()

    # 2) Fall back to gov-il-connect keychain credentials
    if not email_addr or not email_pw:
        try:
            import keyring
            email_addr = email_addr or keyring.get_password("gov-il-connect", "email_address") or ""
            email_pw   = email_pw   or keyring.get_password("gov-il-connect", "email_password") or ""
        except Exception:
            pass

    if not email_addr or not email_pw:
        raise RuntimeError(
            "אין פרטי SMTP — הזן כתובת Gmail וסיסמת אפליקציה בהגדרות › לוג (שליחת לוג)"
        )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = email_addr
    msg["To"]      = to_addr

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.ehlo()
        s.starttls()
        s.login(email_addr, email_pw)
        s.sendmail(email_addr, [to_addr], msg.as_string())
    print(f"[scheduler] log email sent to {to_addr}")


def start(project_root: Path) -> None:
    """Launch the scheduler daemon thread."""
    t = threading.Thread(
        target=_run,
        args=(project_root,),
        name="lias-scheduler",
        daemon=True,
    )
    t.start()
    print("[scheduler] auto-sync scheduler started")
