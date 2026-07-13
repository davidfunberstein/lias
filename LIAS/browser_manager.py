"""Resilient browser thread — the heart of David's requirement.
Thread דפדפן עמיד — לב הדרישה של דוד.

EN: Playwright's sync API is single-threaded by design: the thread that
    launches the browser is the ONLY thread allowed to touch it. In the old
    code the browser occupied the main thread and the terminal. Here the
    browser gets a dedicated daemon thread with a command queue:

        any thread ──(BrowserCommand)──▶ command queue
                                            │  browser thread executes
        any thread ◀──(result/error)──── reply queue (per command)

    Resilience contract / the three guarantees:
    1. NO RESPONSE → RECONNECT: a watchdog pings the browser every
       PING_INTERVAL seconds; a hung or crashed browser is force-closed and
       relaunched automatically with escalating backoff.
    2. REMEMBER PREVIOUS CONNECTIONS: we always launch a *persistent context*
       on browser_profile/ (same folder the existing project uses), so
       cookies + court-portal session survive every relaunch. After a
       relaunch we also re-run an optional `restore` callback (e.g. navigate
       back to the case page) supplied by the caller.
    3. CLEAN CLOSURE: closing goes through the same command queue, so it can
       never deadlock with a running command; a hard kill is the fallback.

HE: ה-API הסינכרוני של Playwright חד-Thread-י בתכנון: ה-Thread שהרים את
    הדפדפן הוא היחיד שמותר לו לגעת בו. בקוד הישן הדפדפן תפס את ה-Thread
    הראשי ואת הטרמינל. כאן הדפדפן מקבל Thread דמון ייעודי עם תור פקודות:

        כל Thread ──(BrowserCommand)──▶ תור פקודות
                                           │  Thread הדפדפן מבצע
        כל Thread ◀──(תוצאה/שגיאה)──── תור תשובה (לכל פקודה)

    חוזה העמידות / שלוש ההבטחות:
    1. אין תגובה ← מתחברים מחדש: Watchdog מבצע פינג כל PING_INTERVAL שניות;
       דפדפן תקוע או קרוס נסגר בכוח ומורם מחדש אוטומטית עם backoff מדורג.
    2. זוכרים חיבורים קודמים: תמיד מרימים *Persistent Context* על
       browser_profile/ (אותה תיקייה של הפרויקט הקיים), כך שהעוגיות וסשן
       הפורטל שורדים כל הרמה מחדש. אחרי הרמה מחדש מריצים גם callback
       'restore' אופציונלי (למשל ניווט חזרה לדף התיק) שסיפק הקורא.
    3. סגירה נקייה: הסגירה עוברת דרך אותו תור פקודות, ולכן לעולם לא תיתקע
       מול פקודה רצה; הריגה קשה היא הגיבוי.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config


# ---------------------------------------------------------------------------
# Command envelope / מעטפת פקודה
# ---------------------------------------------------------------------------

@dataclass
class BrowserCommand:
    """EN: fn receives the live Playwright page and returns any picklable result.
    HE: fn מקבלת את ה-page החי של Playwright ומחזירה כל תוצאה."""
    name: str
    fn: Callable[[Any], Any]                       # fn(page) -> result
    reply: "queue.Queue[tuple[bool, Any]]" = field(default_factory=lambda: queue.Queue(1))
    timeout: float = config.BROWSER_CMD_TIMEOUT_SEC


class BrowserDead(RuntimeError):
    """EN: raised to callers when the browser could not serve the command.
    HE: נזרק לקוראים כשהדפדפן לא הצליח לשרת את הפקודה."""


# ---------------------------------------------------------------------------
# The manager / המנהל
# ---------------------------------------------------------------------------

class BrowserManager:
    def __init__(
        self,
        headless: bool = False,
        restore: Optional[Callable[[Any], None]] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self._headless = headless
        self._restore = restore          # re-navigation after relaunch / ניווט מחדש
        self._log = log
        self._cmd_q: "queue.Queue[BrowserCommand]" = queue.Queue()
        self._alive = threading.Event()  # browser is usable / הדפדפן שמיש
        self._stop = threading.Event()
        self._last_pong = 0.0
        self._relaunch_count = 0
        self._busy = False               # a command is executing right now / פקודה רצה כרגע
        self._last_url = ""              # cached — readable without the queue / נקרא בלי התור
        self._thread = threading.Thread(target=self._browser_loop, name="lias-browser", daemon=True)
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="lias-watchdog", daemon=True)

    # ---- public API / ממשק ציבורי ----------------------------------------

    def start(self) -> None:
        self._thread.start()
        self._watchdog.start()

    def run(self, name: str, fn: Callable[[Any], Any], timeout: Optional[float] = None) -> Any:
        """EN: execute fn(page) on the browser thread; blocks the caller only,
            never the browser loop. Raises BrowserDead on failure/timeout —
            the caller's job goes to ERROR and will be retried; meanwhile the
            watchdog relaunches the browser.
        HE: מריץ fn(page) על Thread הדפדפן; חוסם רק את הקורא, לעולם לא את
            לולאת הדפדפן. זורק BrowserDead בכשל/פסק זמן — המשימה של הקורא
            עוברת ל-ERROR ותנוסה שוב; בינתיים ה-Watchdog מרים את הדפדפן מחדש.
        """
        cmd = BrowserCommand(name=name, fn=fn, timeout=timeout or config.BROWSER_CMD_TIMEOUT_SEC)
        self._cmd_q.put(cmd)
        try:
            ok, payload = cmd.reply.get(timeout=cmd.timeout)
        except queue.Empty:
            # No response → declare dead; watchdog will relaunch.
            # אין תגובה ← מכריזים מת; ה-Watchdog ירים מחדש.
            self._alive.clear()
            raise BrowserDead(f"command '{name}' timed out / פקודה נתקעה")
        if not ok:
            raise BrowserDead(f"command '{name}' failed / נכשלה: {payload}")
        return payload

    def is_alive(self) -> bool:
        return self._alive.is_set()

    def shutdown(self) -> None:
        """Clean closure — guarantee #3 / סגירה נקייה — הבטחה 3."""
        self._stop.set()
        self._cmd_q.put(BrowserCommand(name="__quit__", fn=lambda p: None))
        self._thread.join(timeout=20)

    def show(self) -> None:
        """Relaunch as visible (non-headless). Interrupts any running job."""
        import threading as _threading
        if not self._headless:
            if self._alive.is_set():
                self.bring_to_front()
                return
            # Browser died while visible — fall through to relaunch
        self._headless = False
        self.shutdown()
        self._stop.clear()
        self._alive.clear()
        self._relaunch_count = 0
        self._thread = _threading.Thread(target=self._browser_loop, name="lias-browser", daemon=True)
        self._watchdog = _threading.Thread(target=self._watchdog_loop, name="lias-watchdog", daemon=True)
        self.start()
        self._alive.wait(timeout=15)

    # The automation browser is real Google Chrome (channel="chrome"); only the
    # bundled fallback is "Chromium". Try both app names.
    _APP_NAMES = ("Google Chrome", "Chromium")

    def _osascript(self, verb: str) -> None:
        import subprocess
        for app in self._APP_NAMES:
            try:
                r = subprocess.run(
                    ["osascript", "-e", f'tell application "{app}" to {verb}'],
                    timeout=3, capture_output=True,
                )
                if r.returncode == 0:
                    return
            except Exception:
                continue

    def hide(self) -> None:
        """Minimize the browser window without interrupting downloads (macOS)."""
        if self._headless:
            return
        self._osascript("set miniaturized of every window to true")

    def bring_to_front(self) -> None:
        """Bring the automation browser window to front — un-minimize + activate.
        Also uses Playwright's page.bring_to_front() via the command queue so the
        correct tab is focused even if multiple Chrome windows are open."""
        self._osascript("set miniaturized of every window to false")
        self._osascript("activate")
        # Focus the actual automation page (best-effort, non-blocking)
        try:
            self.run("__front__", lambda p: p.bring_to_front(), timeout=5)
        except Exception:
            pass

    @property
    def headless(self) -> bool:
        return self._headless

    @property
    def busy(self) -> bool:
        """A long command is running right now / פקודה ארוכה רצה כרגע."""
        return self._busy

    @property
    def last_url(self) -> str:
        """Last known page URL — safe to read from any thread (cached)."""
        return self._last_url

    # ---- browser thread / ה-Thread של הדפדפן ------------------------------

    def _launch(self, p) -> Any:
        """EN: persistent context = cookies survive → guarantee #2.
            Headless uses Chromium's NEW headless mode (--headless=new):
            the court portals' WAF resets connections from the legacy
            headless (ERR_CONNECTION_RESET), while new headless carries a
            real-Chrome network fingerprint.
        HE: Persistent Context = העוגיות שורדות ← הבטחה 2.
            ב-headless משתמשים במצב החדש — ה-WAF של הפורטלים חותך את
            ה-headless הישן; החדש נראה כמו Chrome אמיתי."""
        config.BROWSER_PROFILE_DIR.mkdir(exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        kwargs: dict = dict(
            user_data_dir=str(config.BROWSER_PROFILE_DIR),
            accept_downloads=True,
            headless=False,
        )
        if self._headless:
            args.append("--headless=new")    # new headless — real fingerprint

        # EN: NET's WAF resets connections from Playwright's bundled Chromium
        #     (ERR_CONNECTION_RESET). Real Google Chrome has a genuine TLS
        #     fingerprint and passes — prefer it, fall back to bundled.
        # HE: ה-WAF של נט חותך את ה-Chromium של Playwright; Chrome אמיתי
        #     עובר — מעדיפים אותו, עם נסיגה ל-Chromium המובנה.
        try:
            ctx = p.chromium.launch_persistent_context(
                channel="chrome", args=args, **kwargs)
            self._log("[browser] using real Google Chrome / משתמש ב-Chrome אמיתי")
        except Exception as exc:
            self._log(f"[browser] Chrome channel unavailable ({exc}) — bundled Chromium")
            ctx = p.chromium.launch_persistent_context(args=args, **kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return ctx, page

    def _browser_loop(self) -> None:
        """EN: owns Playwright exclusively. Outer loop = relaunch cycle;
            inner loop = serve commands. Any crash falls out to the outer
            loop, which backs off and relaunches the persistent profile.
        HE: הבעלים הבלעדי של Playwright. הלולאה החיצונית = מחזור הרמה מחדש;
            הפנימית = שירות פקודות. כל קריסה נופלת ללולאה החיצונית, שממתינה
            מדורג ומרימה מחדש את הפרופיל המתמשך.
        """
        import asyncio
        from playwright.sync_api import sync_playwright

        while not self._stop.is_set():
            # Playwright's sync API creates its own event loop per-thread.
            # After a crash the old loop may still be registered — replace it
            # so the next sync_playwright() call can create a clean one.
            try:
                loop = asyncio.get_event_loop_policy().get_event_loop()
                if not loop.is_running():
                    asyncio.set_event_loop(asyncio.new_event_loop())
            except Exception:
                pass
            try:
                with sync_playwright() as p:
                    ctx, page = self._launch(p)
                    self._log(f"[browser] up (relaunch #{self._relaunch_count}) / הדפדפן למעלה")
                    if self._relaunch_count and self._restore:
                        # Return to where we were / חזרה לאיפה שהיינו
                        try:
                            self._restore(page)
                        except Exception as e:
                            self._log(f"[browser] restore failed / שחזור נכשל: {e}")
                    self._alive.set()
                    self._last_pong = time.time()
                    self._serve(page)         # blocks until dead/quit / חוסם עד מוות/סגירה
                    try:
                        ctx.close()
                    except Exception:
                        pass
                if self._stop.is_set():
                    return
            except Exception:
                self._log("[browser] crashed / קרס:\n" + traceback.format_exc(limit=3))
            # escalating backoff before relaunch / המתנה מדורגת לפני הרמה מחדש
            self._alive.clear()
            delay = config.BROWSER_RELAUNCH_BACKOFF_SEC[
                min(self._relaunch_count, len(config.BROWSER_RELAUNCH_BACKOFF_SEC) - 1)
            ]
            self._relaunch_count += 1
            # EN: too many crashes while VISIBLE = windows popping at the user
            #     every few seconds. Flip back to headless and slow way down.
            # HE: יותר מדי קריסות במצב גלוי = חלונות קופצים למשתמש. חוזרים
            #     ל-headless ומאטים מאוד.
            if self._relaunch_count >= 3 and not self._headless:
                self._headless = True
                self._log("[browser] too many visible relaunches — back to headless / "
                          "יותר מדי חלונות — חוזרים למצב סמוי")
            if self._relaunch_count >= 3:
                delay = max(delay, 60)
            self._log(f"[browser] relaunch in {delay}s / הרמה מחדש בעוד {delay} שניות")
            if self._stop.wait(delay):
                return

    def _serve(self, page) -> None:
        """Serve commands until quit or failure / שירות פקודות עד סגירה או כשל."""
        while not self._stop.is_set():
            try:
                cmd = self._cmd_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if cmd.name == "__quit__":
                cmd.reply.put((True, None))
                return
            if cmd.name == "__ping__":
                try:
                    page.evaluate("1")            # cheap liveness probe / בדיקת חיים זולה
                    self._last_pong = time.time()
                    try:
                        self._last_url = page.url or ""
                    except Exception:
                        pass
                    cmd.reply.put((True, "pong"))
                except Exception as e:
                    cmd.reply.put((False, str(e)))
                    return                        # fall to relaunch / נופלים להרמה מחדש
                continue
            try:
                self._busy = True
                result = cmd.fn(page)
                cmd.reply.put((True, result))
            except Exception as e:
                # EN: command failed — report to caller; if the page itself is
                #     gone, exit to relaunch. Otherwise keep serving.
                # HE: הפקודה נכשלה — מדווחים לקורא; אם הדף עצמו מת, יוצאים
                #     להרמה מחדש. אחרת ממשיכים לשרת.
                cmd.reply.put((False, f"{type(e).__name__}: {e}"))
                if page.is_closed():
                    return
            finally:
                self._busy = False
                try:
                    self._last_url = page.url or ""
                except Exception:
                    pass

    # ---- watchdog / כלב שמירה ---------------------------------------------

    def _watchdog_loop(self) -> None:
        """EN: pings via the same command queue. No pong within timeout →
            marks dead; the serve loop exits and the outer loop relaunches.
        HE: מפנג דרך אותו תור פקודות. אין pong בזמן ← מסמן מת; לולאת
            השירות יוצאת והלולאה החיצונית מרימה מחדש.
        """
        while not self._stop.wait(config.BROWSER_PING_INTERVAL_SEC):
            if not self._alive.is_set():
                continue                          # already relaunching / כבר בהרמה מחדש
            if self._busy:
                # EN: a long job is running — pings would queue behind it and
                #     falsely declare the browser dead. Skip until it's done.
                # HE: משימה ארוכה רצה — פינג ייתקע בתור ויכריז מוות שקרי. מדלגים.
                continue
            cmd = BrowserCommand(name="__ping__", fn=lambda p: None,
                                 timeout=config.BROWSER_PING_TIMEOUT_SEC)
            self._cmd_q.put(cmd)
            try:
                ok, _ = cmd.reply.get(timeout=config.BROWSER_PING_TIMEOUT_SEC)
                if not ok:
                    self._alive.clear()
            except queue.Empty:
                self._log("[watchdog] no pong — declaring browser dead / אין תגובה — הדפדפן מוכרז מת")
                self._alive.clear()
