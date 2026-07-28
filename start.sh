#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  LIAS — פקודה אחת: משיכת עדכונים + התקנה + הפעלה
#
#  זו הפקודה היחידה שצריך להריץ, גם בפעם הראשונה וגם בכל פעם אחר כך:
#      bash start.sh
#
#  היא עושה הכל: מושכת את הגרסה האחרונה מגיט, מתקינה כל מה שחסר
#  (פייתון, ספריות, דפדפן אוטומציה), בונה DB אם צריך, ומפעילה.
#
#  דגלים:
#      bash start.sh --no-pull    אל תמשוך מגיט (עבודה מקומית)
#      bash start.sh --reinstall  התקן מחדש את כל הספריות
# ═══════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

PULL=1; REINSTALL=0
for a in "$@"; do
  case "$a" in
    --no-pull)   PULL=0 ;;
    --reinstall) REINSTALL=1 ;;
    *) echo "דגל לא מוכר: $a"; exit 1 ;;
  esac
done

echo "════════════════════════════════════════════"
echo "  LIAS — הפעלה"
echo "════════════════════════════════════════════"

# ── 1. משיכת הגרסה האחרונה ────────────────────────────────────
if [ "$PULL" = "1" ] && [ -d .git ]; then
  echo "→ מושך את הגרסה האחרונה…"
  if ! git diff --quiet 2>/dev/null; then
    echo "  ⚠ יש שינויים מקומיות שלא נשמרו — שומר אותם בצד (git stash)"
    git stash push -m "auto-stash by start.sh $(date +%F_%H%M)" >/dev/null 2>&1 || true
  fi
  git pull --ff-only 2>/dev/null && echo "  ✓ מעודכן" \
    || echo "  ⚠ לא הצלחתי למשוך (אין רשת או יש התנגשות) — ממשיך עם הגרסה המקומית"
fi

# ── 2. איתור פייתון מתאים ─────────────────────────────────────
PY=""
for cand in python3.12 python3.11 python3.10 python3.9 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    VER=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")
    MAJ=${VER%%.*}; MIN=${VER##*.}
    if [ "$MAJ" -ge 3 ] && [ "$MIN" -ge 9 ]; then PY="$cand"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "✗ לא נמצא Python 3.9 ומעלה."
  echo "  התקן מ: https://www.python.org/downloads/  ואז הרץ שוב את הפקודה."
  exit 1
fi
echo "→ פייתון: $PY ($("$PY" -c 'import sys;print(sys.version.split()[0])'))"

# ── 3. ספריות ─────────────────────────────────────────────────
NEED=0
"$PY" -c "import playwright" 2>/dev/null || NEED=1
[ "$REINSTALL" = "1" ] && NEED=1
if [ "$NEED" = "1" ]; then
  echo "→ מתקין ספריות… (פעם אחת, עשוי לקחת כמה דקות)"
  "$PY" -m pip install --quiet --upgrade pip 2>/dev/null || true
  if [ -f requirements.txt ]; then
    "$PY" -m pip install --quiet -r requirements.txt || {
      echo "✗ התקנת הספריות נכשלה."
      echo "  נסה ידנית:  $PY -m pip install -r requirements.txt"; exit 1; }
  else
    "$PY" -m pip install --quiet playwright faster-whisper || {
      echo "✗ התקנת הספריות נכשלה."; exit 1; }
  fi
  echo "→ מתקין דפדפן אוטומציה…"
  "$PY" -m playwright install chromium || \
    echo "  ⚠ התקנת Chromium נכשלה — המערכת תנסה להשתמש ב-Chrome המותקן"
  echo "  ✓ הספריות מוכנות"
else
  echo "→ הספריות כבר מותקנות ✓"
fi

# ── 4. DB ─────────────────────────────────────────────────────
if [ -d "court_documents/downloads" ] && [ ! -f "lias.db" ]; then
  echo "→ נמצאו מסמכים ללא DB — בונה…"
  "$PY" rebuild_db.py || echo "  ⚠ בנייה נכשלה — הרץ ידנית: $PY rebuild_db.py"
fi

# ── 5. שחרור הפורט אם תקוע מהרצה קודמת ────────────────────────
if command -v lsof >/dev/null 2>&1; then
  OLD=$(lsof -ti:8500 2>/dev/null || true)
  if [ -n "$OLD" ]; then
    echo "→ סוגר הרצה קודמת שנתקעה על פורט 8500…"
    kill -9 $OLD 2>/dev/null || true
    sleep 1
  fi
fi

# ── 6. הפעלה ──────────────────────────────────────────────────
echo ""
echo "✓ מפעיל את LIAS…"
echo "  פתח בדפדפן:  http://localhost:8500"
echo "  לעצירה:      Ctrl+C"
echo ""
exec "$PY" app.py
