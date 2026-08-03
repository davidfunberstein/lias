#!/usr/bin/env python3
"""
import_session.py — אתה מריץ אצלך.

טוען את storage_state שהלקוח ייצא, פותח דפדפן עם אותן עוגיות,
ובודק אמפירית אם הפורטל מזהה אותך כמחובר — או קופץ למסך התחברות.
זה מכריע האם הפורטל קשור ל-IP (pass-the-cookie בין מכונות עובד / לא עובד).

שימוש:
    python tools/import_session.py session_bdr.json

התוצאה:
    ✓ התיקים נטענו  -> הפורטל אינו IP-bound, המודל עובד.
    ✗ מסך התחברות  -> הפורטל IP-bound, צריך proxy אצל הלקוח.
"""
import sys
import json
import time

# חתימות שמעידות על "עדיין מחובר" מול "נדרשת התחברות", לכל פורטל.
LOGIN_MARKERS = ["login", "signin", "sso", "התחבר", "הזדהות", "כניסה למערכת"]
OK_MARKERS = {
    "bdr": ["FilesList", "התיקים שלי", "שם תיק"],
    "net": ["homepage", "תיקים", "לשכה"],
    "eca": ["OpenCase", "תיקים", "הוצאה לפועל"],
}


def _looks_logged_in(page, portal: str) -> bool:
    try:
        url = (page.url or "").lower()
        body = ""
        try:
            body = page.inner_text("body", timeout=5000)
        except Exception:
            pass
        hay = url + " " + body
        # אם URL הופנה חזרה למסך הזדהות — לא מחובר.
        if any(m in url for m in ("login", "signin", "sso", "idp")):
            return False
        oks = OK_MARKERS.get(portal, [])
        return any(m.lower() in hay.lower() for m in oks)
    except Exception:
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print("שימוש: python tools/import_session.py session_<portal>.json")
        return 2
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    portal = payload["portal"]
    url = payload["url"]
    state = payload["storage_state"]
    age_min = (time.time() - payload.get("exported_at", 0)) / 60.0
    print(f"פורטל: {portal.upper()} · גיל הסשן: {age_min:.1f} דק׳ · "
          f"{len(state.get('cookies', []))} עוגיות")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False,
                                     args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(storage_state=state)
        page = ctx.new_page()
        print("טוען את הפורטל עם העוגיות שיובאו...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)  # להניח ל-SSO/redirect להתייצב

        ok = _looks_logged_in(page, portal)
        print("─" * 56)
        if ok:
            print("✓ מחובר — הפורטל קיבל את העוגיות מ-IP שלך.")
            print("  => pass-the-cookie בין מכונות עובד לפורטל הזה.")
        else:
            print("✗ לא מחובר — הופנית להזדהות מחדש.")
            print("  => הפורטל כנראה IP-bound. צריך proxy אצל הלקוח.")
        print(f"  URL נוכחי: {page.url}")
        print("─" * 56)
        input(">> בדוק ידנית בחלון, ואז לחץ Enter לסגירה... ")
        browser.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
