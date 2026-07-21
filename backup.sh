#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  LIAS — גיבוי מלא
#  יוצר גיבוי של כל מה שחשוב: קוד + DB + מסמכים + תמלולים + הגדרות.
#  לא כולל: סיסמאות (ב-Keychain), browser_profile, credentials/token.
#
#  Run:  bash backup.sh  [יעד-אופציונלי]
#  ברירת מחדל: ~/LIAS_backups/
# ═══════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

DEST="${1:-$HOME/LIAS_backups}"
mkdir -p "$DEST"
STAMP=$(date +%Y-%m-%d_%H%M)
OUT="$DEST/LIAS_backup_${STAMP}.tar.gz"

echo "→ יוצר גיבוי מלא ל: $OUT"

tar --exclude='./browser_profile' --exclude='./browser_profile_bdr' \
    --exclude='./.git' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='./credentials.json' --exclude='./token.json' \
    --exclude='*/logs/*' \
    -czf "$OUT" \
    app.py eca_download.py rebuild_db.py requirements.txt install.sh \
    core LIAS ui_demo ui_modules \
    lias.db session_defaults.json annotations.json \
    court_documents transcriptions 2>/dev/null || true

SIZE=$(du -h "$OUT" | cut -f1)
echo "✓ הגיבוי נוצר: $OUT  ($SIZE)"
echo ""
echo "  לשחזור במחשב אחר:"
echo "    mkdir legal-AI-app && tar -xzf $OUT -C legal-AI-app"
echo "    cd legal-AI-app && bash install.sh && python3 app.py"
