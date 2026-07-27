#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  LIAS — איפוס מלא (התחלה מ-0)
#  מוחק את כל התיקים שהורדו, את בסיס הנתונים ואת המטמונים,
#  כדי שאפשר יהיה להתחיל בדיקה נקייה לגמרי.
#
#  Run:  bash reset_all.sh              # מוחק תיקים + DB (שומר הגדרות)
#        bash reset_all.sh --with-login # מוחק גם סשן דפדפן/גוב.איי.אל
#        bash reset_all.sh --everything # מוחק גם הגדרות ומיילים
#        bash reset_all.sh --dry-run    # רק מציג מה יימחק
#
#  לא נמחק אף פעם: הסיסמאות ב-Keychain של macOS
#  (למחיקה: Keychain Access → חפש "gov-il-connect")
# ═══════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

DRY=0; WITH_LOGIN=0; EVERYTHING=0
for a in "$@"; do
  case "$a" in
    --dry-run)    DRY=1 ;;
    --with-login) WITH_LOGIN=1 ;;
    --everything) WITH_LOGIN=1; EVERYTHING=1 ;;
    *) echo "דגל לא מוכר: $a"; exit 1 ;;
  esac
done

# מה נמחק — לפי רמות
TARGETS=(
  "court_documents/downloads"      # כל התיקים והמסמכים שהורדו
  "lias.db" "lias.db-shm" "lias.db-wal"   # בסיס הנתונים של הדשבורד
  "הוצאה_לפועל"                    # שארית מהסקריפט העצמאי הישן של הוצל"פ
  "lias_engine.log"
  "annotations.json"
)
if [ "$WITH_LOGIN" = "1" ]; then
  TARGETS+=(".gov_session.json" "browser_profile" "browser_profile_bdr" "browser_profile_eca")
fi
if [ "$EVERYTHING" = "1" ]; then
  TARGETS+=("session_defaults.json" "email_config.json" "google_login.json" "login_audit.log")
fi

echo "═══════════════════════════════════════════"
echo "  LIAS — איפוס"
echo "═══════════════════════════════════════════"
echo "ימחקו הפריטים הבאים:"
FOUND=0
for t in "${TARGETS[@]}"; do
  if [ -e "$t" ]; then
    SIZE=$(du -sh "$t" 2>/dev/null | cut -f1)
    echo "   • $t   ($SIZE)"
    FOUND=1
  fi
done
[ "$FOUND" = "0" ] && { echo "   (אין מה למחוק — כבר נקי)"; exit 0; }

if [ "$EVERYTHING" != "1" ]; then
  echo
  echo "יישמרו: ההגדרות, המייל, הסיסמאות ב-Keychain, התמלולים"
fi

if [ "$DRY" = "1" ]; then
  echo
  echo "(--dry-run — לא נמחק דבר)"
  exit 0
fi

echo
read -p "למחוק? הפעולה בלתי הפיכה. הקלד DELETE לאישור: " ANS
[ "$ANS" = "DELETE" ] || { echo "בוטל."; exit 1; }

echo
for t in "${TARGETS[@]}"; do
  if [ -e "$t" ]; then
    rm -rf "$t"
    echo "   ✓ נמחק: $t"
  fi
done

mkdir -p court_documents/downloads

echo
echo "═══════════════════════════════════════════"
echo "  ✓ האיפוס הושלם — המערכת נקייה"
echo "═══════════════════════════════════════════"
echo
echo "השלבים הבאים:"
echo "  1. הפעל את האפליקציה:   python3 app.py"
echo "  2. פתח:                 http://localhost:8500"
echo "  3. נקה את מטמון הדפדפן:  בקונסול (F12) הקלד"
echo "        localStorage.clear(); sessionStorage.clear(); location.reload()"
echo "     (מוחק את רשימות התיקים השמורות ואת סימוני 'כבר הורד')"
echo "  4. התחבר לפורטל והתחל סנכרון נקי."
echo
if [ "$EVERYTHING" = "1" ]; then
  echo "שים לב: נמחקו גם ההגדרות — הזן מחדש מייל, סיסמת אפליקציה והגדרות גוב.איי.אל."
fi
echo "הסיסמאות ב-Keychain לא נמחקו. למחיקה ידנית:"
echo "  Keychain Access → חיפוש 'gov-il-connect' → מחק"
