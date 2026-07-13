# SETUP NOTES — what YOU need to do (once, after the code)
# הערות התקנה — מה שאתה צריך לעשות (פעם אחת, אחרי הקוד)

> EN: Everything below is deliberately left for you — per your instruction,
> nothing here was expected in real time.
> HE: כל מה שלמטה הושאר לך בכוונה — לפי ההנחיה שלך, שום דבר כאן לא נדרש בזמן אמת.

## 1. Install dependencies / התקנת תלויות

```bash
# EN: NOTHING is required for the UI — it now runs on a zero-dependency
#     built-in server (httpd.py). fastapi/uvicorn are OPTIONAL upgrades.
# HE: לא נדרש כלום בשביל ה-UI — הוא רץ עכשיו על שרת מובנה בלי תלויות
#     (httpd.py). fastapi/uvicorn הם שדרוג אופציונלי בלבד.
pip3 install fastapi uvicorn    # optional / אופציונלי
# playwright needed only for browser jobs / נדרש רק למשימות דפדפן:
pip3 install playwright && playwright install chromium
```

Verified end-to-end in sandbox: server start, every REST endpoint, SSE live
stream (7 events during a background job), clean job completion.
נבדק מקצה לקצה: עליית שרת, כל נקודות ה-REST, זרם SSE חי (7 אירועים תוך כדי
משימת רקע), והשלמת משימות נקייה.

## 2. First run / הרצה ראשונה

```bash
python -m LIAS.migrate_csv   # already ran once here: 1,550 docs imported
                             # כבר רץ פעם אחת כאן: 1,550 מסמכים יובאו
python -m LIAS.run           # opens API+UI on http://localhost:8400
```

Then in the UI / ואז ב-UI:
1. Click **פתח נט המשפט** — the shared browser opens; log in once (smart
   card / OTP). The persistent profile remembers it for next times.
   לחץ **פתח נט המשפט** — הדפדפן המשותף נפתח; התחבר פעם אחת. הפרופיל
   המתמשך זוכר להבא.
2. Navigate to a case in the browser, then click **סנכרן תיק פתוח (NET)** —
   the proven legacy download flow runs as a background job; files and
   progress appear live in the dashboard.
   נווט לתיק בדפדפן ולחץ **סנכרן תיק פתוח** — זרימת ההורדה הבדוקה רצה
   כמשימת רקע; קבצים והתקדמות מופיעים חיים בדשבורד.

## 3. Known assumptions I made / הנחות שהנחתי

- **DB journal mode:** your court_documents may sit on a filesystem where
  SQLite WAL fails; db.py auto-falls back to TRUNCATE. If lias.db ever
  misbehaves, delete `LIAS/lias.db*` and re-run migrate (CSVs are still the
  parallel source — nothing is lost).
  אם lias.db משתבש — מחק את `LIAS/lias.db*` והרץ שוב migrate (ה-CSV עדיין
  מקור מקביל — שום דבר לא הולך לאיבוד).
- **net_scan** assumes a NET case page is open in the shared browser and reads
  `#PresentDocumentGridArrayStore` (same store the legacy code uses).
  מניח שדף תיק נט פתוח בדפדפן וקורא את אותו מחסן DOM כמו הקוד הישן.
- **Restore after browser relaunch** currently returns to the NET home page.
  Deep restore (reopening the exact case) needs case URLs in the DB — Phase 1.
  השחזור אחרי הרמה מחדש חוזר כרגע לדף הבית של נט. שחזור עמוק דורש כתובות
  תיקים ב-DB — שלב 1.
- Job workers poll every 1s; SSE hides the latency. Fine at this scale.
  ה-Workers דוגמים כל שנייה; ה-SSE מסתיר את ההשהיה. מצוין בסקייל הזה.

## 4. What exists vs. the master plan / מה קיים מול התוכנית הראשית

| Master-plan item | Status |
|---|---|
| Phase 0: SQLite source of truth, Pydantic models, jobs table | ✅ built + tested / נבנה ונבדק |
| Phase 0: CSV migration (dual-source) | ✅ ran on real data: 8 clients, 37 sub-cases, 1,550 docs |
| Phase 1: snapshot diff engine (step 6) | ✅ built + tested / נבנה ונבדק |
| Phase 1: resilient browser thread + watchdog + reconnect + persistent profile | ✅ built; timeout path tested (live browser test = on your machine) |
| Phase 1: HumanPace anti-bot pacing | ✅ built (wire into legacy clicks later / לחבר ללחיצות הישנות בהמשך) |
| UI: live RTL dashboard (tree, filtered docs, jobs, SSE) | ✅ built (interim UI; React app = Phase 5) |
| Legacy bridge: NET/BDR full sync as background jobs | ✅ built (uses run_net_download / run_bdr_download) |
| Sticky decisions, hidden timestamps, retry integration into legacy flow | ⏭ next task (Phase 1 continuation) |
| Organizer / Analyzer / Intelligence / citations | ⏭ per master plan Phases 2–6 |

## 5. Next coding session starts with / הסשן הבא מתחיל ב:

1. Sticky-decision filter inside the legacy NET scan (step 7).
2. Hidden-timestamp deep-parse on new documents (step 5).
3. Case-URL tracking → deep restore after browser relaunch.
4. Wire HumanPace into core/net_navigation.py clicks.
