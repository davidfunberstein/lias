#!/usr/bin/env python3
"""
export_session.py — מיועד ללקוח / לחבר שרוצה לשלוח גישה לפורטל.

הסקריפט פותח דפדפן Chrome נקי (לא נוגע בפרופיל האישי שלך),
נותן לך להתחבר ידנית ל-gov.il, ומייצא רק את תעודת הסשן (עוגיות).
שום סיסמה לא נשמרת. הקובץ תקף בדרך כלל לשעה-שעתיים.

שימוש:
    python export_session.py            # ברירת מחדל: BDR
    python export_session.py bdr
    python export_session.py net
    python export_session.py eca

התוצאה: session_<portal>.json — שלח אותו לעורך הדין.
"""
import sys, json, time, subprocess, textwrap


PORTALS = {
    "bdr": {
        "url":   "https://sides.rbc.gov.il/Pages/FilesList.aspx",
        "label": "בית הדין הרבני (BDR)",
        "hint":  "נווט לרשימת תיקיך ►► סמל 'התיקים שלי'",
    },
    "net": {
        "url":   "https://www.court.gov.il/ngcs.web.site/homepage.aspx",
        "label": "נט המשפט (NET)",
        "hint":  "לחץ על 'הכניסה לתיקים שלי' ווודא שאתה רואה תיקים",
    },
    "eca": {
        "url":   "https://publicsso.eca.gov.il/he/home/OpenCase",
        "label": "הוצאה לפועל (ECA)",
        "hint":  "וודא שאתה רואה את רשימת תיקי ההוצאה לפועל",
    },
}

BORDER = "═" * 58


def _ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        pass
    print("  playwright לא מותקן — מתקין כעת (דקה אחת)…")
    subprocess.run([sys.executable, "-m", "pip", "install", "playwright", "-q"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"], check=True)
    return True


def main() -> int:
    portal = (sys.argv[1] if len(sys.argv) > 1 else "bdr").lower()
    if portal not in PORTALS:
        print(f"⚠  פורטל לא מוכר: '{portal}'")
        print(f"   בחר מתוך: {', '.join(PORTALS.keys())}")
        return 2

    p = PORTALS[portal]
    out = f"session_{portal}.json"

    print(BORDER)
    print(f"  ייצוא סשן — {p['label']}")
    print(BORDER)
    print(textwrap.dedent(f"""
  1. דפדפן Chrome ייפתח עוד רגע.
  2. התחבר ל-gov.il (שם משתמש + סיסמה + קוד חד-פעמי אם נדרש).
  3. {p['hint']}.
  4. כשרואים את התיקים — חזור לחלון הזה ולחץ Enter.

  ⚡ לא נשמרת שום סיסמה — רק "תעודת כניסה" זמנית.
""").rstrip())

    _ensure_playwright()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )
        ctx = browser.new_context(no_viewport=True)
        page = ctx.new_page()
        try:
            page.goto(p["url"], wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass

        input("\n>> הגעת לתיקים? לחץ Enter לייצוא…  ")

        state = ctx.storage_state()
        payload = {
            "portal":       portal,
            "url":          p["url"],
            "exported_at":  time.time(),
            "exported_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "storage_state": state,
        }

        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        n = len(state.get("cookies", []))
        print(f"\n{BORDER}")
        print(f"  ✓ נשמר: {out}")
        print(f"  עוגיות שיוצאו: {n}")
        print(f"  תקף עד: כשעה-שעתיים מהרגע הזה")
        print(f"{BORDER}")
        print(f"\n  שלח את הקובץ  {out}  לעורך הדין.")
        print(f"  הוא יעלה אותו דרך הגדרות ► ייבוא סשן.\n")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
