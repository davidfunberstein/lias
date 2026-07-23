#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  LIAS — עדכון לגרסה האחרונה
#  הלקוח מריץ סקריפט אחד ומקבל את כל העדכונים, בלי לגעת בנתונים שלו.
#
#    bash update.sh                       ← מושך מהריפו המוגדר
#    bash update.sh https://github.com/USER/REPO.git   ← חיבור ראשוני לריפו
#
#  הנתונים (lias.db, court_documents, transcriptions, הגדרות, פרופילים)
#  לעולם לא נדרסים — רק קבצי הקוד מתעדכנים.
# ═══════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

REPO_URL="$1"

# חיבור ראשוני לריפו (פעם אחת) — אם התיקייה עוד לא מחוברת לגיט
if [ ! -d ".git" ]; then
    if [ -z "$REPO_URL" ]; then
        echo "✗ התיקייה לא מחוברת לגיט. הרץ פעם אחת עם כתובת הריפו:"
        echo "    bash update.sh https://github.com/USER/REPO.git"
        exit 1
    fi
    echo "→ מתחבר לריפו בפעם הראשונה…"
    git init -q
    git remote add origin "$REPO_URL"
elif [ -n "$REPO_URL" ]; then
    git remote set-url origin "$REPO_URL"
fi

command -v git >/dev/null || { echo "✗ git לא מותקן (במק: xcode-select --install)"; exit 1; }
git remote get-url origin >/dev/null 2>&1 || { echo "✗ אין ריפו מוגדר — הרץ: bash update.sh <repo-url>"; exit 1; }

echo "→ בודק עדכונים…"
git fetch origin

BRANCH=$(git remote show origin 2>/dev/null | sed -n 's/.*HEAD branch: //p')
BRANCH=${BRANCH:-main}
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "none")
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "✓ כבר בגרסה האחרונה ($(git log -1 --format='%h · %s' | head -c 70))"
    exit 0
fi

# גיבוי קטן של הקוד הנוכחי לפני עדכון (בלי הנתונים)
STAMP=$(date +%Y-%m-%d_%H%M)
echo "→ שומר גיבוי קוד קצר ל-.update_backup_$STAMP.tar.gz"
tar -czf ".update_backup_$STAMP.tar.gz" \
    --exclude='./court_documents' --exclude='./transcriptions' \
    --exclude='./browser_profile*' --exclude='./.git' --exclude='*.db*' \
    --exclude='.update_backup_*' \
    *.py *.sh *.yml Dockerfile requirements.txt core LIAS ui_demo ui_modules 2>/dev/null || true

echo "→ מושך עדכונים (הנתונים שלך לא נגעים)…"
# checkout only CODE paths from the new version — data dirs are untouched
git checkout "origin/$BRANCH" -- \
    "*.py" "*.sh" "*.yml" "Dockerfile" "requirements.txt" \
    core LIAS ui_demo ui_modules 2>/dev/null \
  || git reset --hard "origin/$BRANCH"
git update-ref HEAD "$REMOTE" 2>/dev/null || true

echo "→ מעדכן חבילות Python (אם צריך)…"
python3 -m pip install --quiet -r requirements.txt 2>/dev/null || true

echo ""
echo "✓ עודכן לגרסה: $(git log -1 --format='%h · %s' "$REMOTE" | head -c 70)"
echo "  הפעל מחדש:  python3 app.py"
