"""LIAS configuration / קונפיגורציית LIAS.

EN: Single place for every path and tunable. Assumptions (per David's instruction
    to proceed without asking): the package lives inside gov-il-connect-v2, the
    downloaded files live in ./court_documents, and the persistent browser
    profile is ./browser_profile — all as in the existing project.
HE: מקום אחד לכל נתיב ופרמטר. הנחות (לפי ההנחיה להתקדם בלי לשאול):
    החבילה יושבת בתוך gov-il-connect-v2, הקבצים שהורדו ב-./court_documents,
    ופרופיל הדפדפן המתמשך ב-./browser_profile — הכל כמו בפרויקט הקיים.
"""
from __future__ import annotations

from pathlib import Path

# --- Paths / נתיבים -------------------------------------------------------
LIAS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = LIAS_DIR.parent                      # gov-il-connect-v2
BROWSER_PROFILE_DIR = PROJECT_ROOT / "browser_profile"  # shared with terminal mode
BROWSER_PROFILE_BDR_DIR = PROJECT_ROOT / "browser_profile_bdr"  # separate BDR browser
BROWSER_PROFILE_ECA_DIR = PROJECT_ROOT / "browser_profile_eca"  # separate ECA browser (full parallelism)
DB_PATH = PROJECT_ROOT / "lias.db"
UI_DIR = LIAS_DIR / "ui"

# COURT_DOCS_DIR — overridable via session_defaults.json ("court_docs_dir" key)
# so LIAS can point to the actual download folder on this machine.
_defaults_path = PROJECT_ROOT / "court_documents" / "session_defaults.json"
if not _defaults_path.exists():
    _defaults_path = PROJECT_ROOT / "session_defaults.json"
_override: str = ""
try:
    import json as _json
    _d = _json.loads(_defaults_path.read_text(encoding="utf-8"))
    _override = _d.get("court_docs_dir", "")
except Exception:
    pass
COURT_DOCS_DIR: Path = Path(_override).expanduser().resolve() if _override else (PROJECT_ROOT / "court_documents")

# --- Portals / פורטלים ----------------------------------------------------
NET_HOME_URL = "https://www.court.gov.il/ngcs.web.site/homepage.aspx"
BDR_FILES_URL = "https://sides.rbc.gov.il/Pages/FilesList.aspx"
ECA_URL = "https://publicsso.eca.gov.il/he/home/OpenCase"

# --- Browser thread / Thread הדפדפן ---------------------------------------
# EN: command timeout — if the browser doesn't answer within this window the
#     watchdog declares it dead and relaunches the persistent profile.
# HE: זמן המתנה לפקודה — אם הדפדפן לא עונה בחלון הזה ה-Watchdog מכריז
#     שהוא מת ומרים מחדש את הפרופיל המתמשך.
BROWSER_CMD_TIMEOUT_SEC = 900          # ECA SSO alone can take ~2-4 min; downloads are slow / התחברות הוצל"פ + הורדות איטיות
BROWSER_PING_INTERVAL_SEC = 20         # watchdog ping / פינג של ה-Watchdog
BROWSER_PING_TIMEOUT_SEC = 15
BROWSER_RELAUNCH_BACKOFF_SEC = [2, 5, 15, 30, 60]  # escalating / מדורג

# --- Human pacing (anti-bot) / קצב אנושי (אנטי-בוט) -----------------------
JITTER_MIN_SEC = 1.5
JITTER_MAX_SEC = 4.0
LONG_PAUSE_EVERY_N_ACTIONS = 25        # occasional "reading" pause / הפסקת "קריאה"
LONG_PAUSE_RANGE_SEC = (8.0, 20.0)

# --- Downloads / הורדות ----------------------------------------------------
MAX_PARALLEL_TABS = 5                  # keep 4–7, never more / להשאיר 4–7
DOWNLOAD_MAX_RETRIES = 3

# --- Jobs / משימות ---------------------------------------------------------
JOB_WORKERS = 3                        # non-browser workers / שאינם דפדפן

# --- API / UI ---------------------------------------------------------------
API_HOST = "127.0.0.1"
API_PORT = 8400
