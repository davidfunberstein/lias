#!/usr/bin/env python3
"""
import_session.py — טוען את הסשן ובודק את כתובת ה-IP הציבורית של הריצה.

משתמש ב-storage_state שהתקבל, מציג את ה-IP הציבורי הנוכחי
ורץ דרך דפדפן Firefox (או דרך Proxy מוגדר) לבדיקה אמפירית.

שימוש:
    python tools/import_session.py session_bdr.json
"""
import sys
import json
import time

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
        # אפשרות: הגדרת Proxy ספציפי ל-Playwright (אם תרצה להשתמש בשרת פרוקסי)
        # אם אתה משתמש ב-Hotspot סלולרי/איפוס ראוטר, השאר את proxy=None
        proxy_config = None
        # proxy_config = {"server": "http://<PROXY_IP>:<PORT>"}

        print("פותח דפדפן...")
        browser = pw.firefox.launch(
            headless=False,
            proxy=proxy_config
        )

        ctx = browser.new_context(
            storage_state=state,
            locale="he-IL",
            timezone_id="Asia/Jerusalem"
        )

        # 1. בדיקה והדפסה של כתובת ה-IP הציבורית הנוכחית
        ip_page = ctx.new_page()
        try:
            ip_page.goto("https://api.ipify.org", timeout=10000)
            public_ip = ip_page.inner_text("body").strip()
            print(f"\n[i] כתובת ה-IP הציבורית בריצה הזו: {public_ip}\n")
        except Exception as e:
            print(f"\n[!] לא ניתן היה לאחזר IP ציבורי: {e}\n")
        finally:
            ip_page.close()

        # 2. טעינת הפורטל הנבדק
        page = ctx.new_page()
        print("טוען את הפורטל עם העוגיות שיובאו...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)  # זמן המתנה להתייצבות ה-SSO והפניות

        ok = _looks_logged_in(page, portal)
        print("─" * 56)
        if ok:
            print("✓ מחובר — הפורטל אישר את הסשן!")
            print("  => pass-the-cookie עובד בהצלחה.")
        else:
            print("✗ לא מחובר — הופנית להזדהות מחדש.")
            print("  => הפורטל דחה את הסשן (ייתכן עקב שינוי IP/סביבה או תפוגה).")
        print(f"  URL נוכחי: {page.url}")
        print("─" * 56)
        input(">> בדוק ידנית בחלון, ואז לחץ Enter לסגירה... ")
        browser.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())