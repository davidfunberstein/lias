# LIAS — Legal Intelligence Agent System

> Phase 0–1 implementation living inside gov-il-connect-v2.
> מימוש שלבים 0–1 בתוך gov-il-connect-v2.

## What this package does / מה החבילה עושה

**EN:** LIAS adds the data foundation and orchestration layer on top of the existing
scraper: a single SQLite source of truth, a job queue, a resilient browser thread
(auto-reconnect, remembers previous sessions), a snapshot diff engine, and a live
RTL web UI (FastAPI + SSE) that shows files and jobs in real time.

**HE:** LIAS מוסיפה את תשתית הנתונים ושכבת התיאום מעל הסקרייפר הקיים:
מקור אמת יחיד ב-SQLite, תור משימות, Thread דפדפן עמיד (מתחבר מחדש לבד,
זוכר חיבורים קודמים), מנוע Diff על Snapshot, ו-UI ווב חי ב-RTL
(FastAPI + SSE) שמציג קבצים ומשימות בזמן אמת.

## Architecture / ארכיטקטורה

```
run.py
 ├── db.py            SQLite (WAL) — source of truth / מקור האמת
 ├── models.py        Pydantic data models / מודלי נתונים
 ├── jobs.py          Job queue + worker threads / תור משימות
 ├── browser_manager.py  Dedicated browser thread + watchdog / דפדפן ב-Thread ייעודי
 ├── snapshot.py      Snapshot diff engine / מנוע השוואת מצבים
 ├── collector_bridge.py Bridge to existing core/ scrapers / גשר לקוד הקיים
 ├── migrate_csv.py   One-time CSV → SQLite import / ייבוא חד-פעמי
 ├── api.py           FastAPI + SSE / שרת ה-API
 └── ui/index.html    RTL live dashboard / דשבורד חי
```

## Key design: the browser thread / עיצוב מרכזי: Thread הדפדפן

**EN:** Playwright's sync API must be owned by exactly one thread. LIAS gives the
browser its own thread with a command queue. Every other thread (jobs, API) sends
commands and waits on a reply queue with a timeout. A watchdog pings the browser;
if it is unresponsive or crashed, the manager closes it safely and relaunches the
**persistent profile** (`browser_profile/`), so cookies and the court-portal login
survive — this is the "remember previous connections" requirement.

**HE:** ה-API הסינכרוני של Playwright חייב להיות בבעלות Thread אחד בדיוק.
LIAS נותנת לדפדפן Thread משלו עם תור פקודות. כל Thread אחר (משימות, API)
שולח פקודה וממתין לתשובה עם timeout. Watchdog מבצע פינג לדפדפן; אם אין
תגובה או שהוא קרס — המנהל סוגר בעדינות ומרים מחדש את **הפרופיל המתמשך**
(`browser_profile/`), כך שהעוגיות וההתחברות לפורטל שורדות — זו הדרישה של
"זוכרים חיבורים קודמים".

## Run / הרצה

```bash
pip install fastapi uvicorn pydantic
python -m LIAS.migrate_csv        # one-time import / ייבוא חד-פעמי
python -m LIAS.run                # starts API+UI on http://localhost:8400
```

See `SETUP_NOTES.md` for everything that needs configuration.
ראו `SETUP_NOTES.md` לכל מה שדורש הגדרה.
