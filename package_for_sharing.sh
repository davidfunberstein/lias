#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  LIAS — אריזה לשליחה
#  יוצר קובץ ZIP אחד לשליחה למישהו אחר, בשתי גרסאות:
#
#    bash package_for_sharing.sh          → קוד בלבד (התחלה מ-0, בלי הנתונים שלך)
#    bash package_for_sharing.sh --with-data  → כולל התיקים והמסמכים שלך
#
#  מה שלעולם לא נכנס לחבילה: סיסמאות (Keychain), credentials.json, token.json,
#  browser_profile (סשנים מחוברים ל-gov.il). אלה אישיים ולא לשיתוף.
# ═══════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

WITH_DATA=0
[ "$1" = "--with-data" ] && WITH_DATA=1

STAMP=$(date +%Y-%m-%d_%H%M)
NAME="LIAS_${STAMP}"
[ "$WITH_DATA" = 1 ] && NAME="LIAS_with_data_${STAMP}"
OUT="/tmp/${NAME}.zip"

# תמיד להחריג — אישי/רגיש/כבד
EXCLUDES=(
  -x "*/browser_profile/*" -x "browser_profile/*"
  -x "*/browser_profile_bdr/*" -x "browser_profile_bdr/*"
  -x "*/.git/*" -x "*/__pycache__/*" -x "*.pyc"
  -x "credentials.json" -x "token.json" -x "email_config.json"
  -x "*/logs/*" -x "*.log"
  -x "*/node_modules/*"
)

if [ "$WITH_DATA" = 0 ]; then
  # קוד בלבד — בלי DB ובלי מסמכים
  EXCLUDES+=( -x "lias.db*" -x "*/lias.db*" -x "*/court_documents/*" -x "court_documents/*"
             -x "*/transcriptions/*" -x "transcriptions/*" )
  echo "→ אורז קוד בלבד (התחלה מ-0)…"
else
  echo "→ אורז כולל הנתונים שלך (תיקים + מסמכים + DB)…"
fi

rm -f "$OUT"
zip -r -q "$OUT" . "${EXCLUDES[@]}"

SIZE=$(du -h "$OUT" | cut -f1)
echo ""
echo "✓ נוצר: $OUT  ($SIZE)"
echo ""
echo "  לשליחה: שלח את הקובץ. אצל המקבל:"
echo "    1. פותחים את ה-ZIP"
echo "    2. bash install.sh        (מתקין הכל אוטומטית)"
if [ "$WITH_DATA" = 1 ]; then
echo "    3. python3 rebuild_db.py  (בונה את ה-DB מהמסמכים ששלחת)"
fi
echo "    $([ "$WITH_DATA" = 1 ] && echo 4 || echo 3). python3 app.py           → http://localhost:8500"
echo ""
echo "  שים לב: אישורי gov.il לא נשלחים — המקבל יזין את שלו בהגדרות ⚙."
