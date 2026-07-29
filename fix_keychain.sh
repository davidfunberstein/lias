#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  LIAS — שחרור חסימת Keychain
#
#  מתי להריץ: macOS מבקש שוב ושוב
#      "python3.x wants to use your confidential information
#       stored in gov-il-connect… enter the login keychain password"
#  והאפליקציה לא מצליחה לשמור אישורים או לסנכרן.
#
#  למה זה קורה: פריט ב-Keychain זוכר איזה בינארי יצר אותו. אם
#  הפרויקט הותקן מחדש או שהפייתון התחלף, הבינארי החדש אינו ברשימת
#  ההרשאות (ACL) — ו-macOS דורש אישור בכל גישה.
#
#  מה הסקריפט עושה: מוחק את הפריטים התקועים כדי שהאפליקציה תיצור
#  אותם מחדש עם הבינארי הנוכחי. מחיקה אינה דורשת סיסמה — היא לא
#  מפענחת את התוכן. אחר כך פשוט מזינים מחדש בהגדרות ⚙.
#
#  הרצה:  bash fix_keychain.sh          (מציג מה יימחק ושואל)
#         bash fix_keychain.sh --govil  (רק אישורי gov.il)
# ═══════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

GOVIL="gov-il-connect"
EMAIL="gov-il-connect-email"
TOTP="gov-il-connect-totp"

ONLY_GOVIL=0
[ "${1:-}" = "--govil" ] && ONLY_GOVIL=1

echo "════════════════════════════════════════════"
echo "  LIAS — שחרור חסימת Keychain"
echo "════════════════════════════════════════════"

# מציגים מטא-דאטה בלבד. `security find-generic-password` בלי הדגל -w
# לא מפענח את הסוד, ולכן אינו מפעיל את בקשת האישור שתקועה.
show() {
  local svc="$1" label="$2"
  if security find-generic-password -s "$svc" >/dev/null 2>&1; then
    local created
    # cdat comes back as 20260710120014Z\000 — trim the NUL and reformat
    created=$(security find-generic-password -s "$svc" 2>&1 \
              | awk -F'"' '/"cdat"/{print $4; exit}' \
              | sed 's/\\000//' \
              | sed -E 's/^([0-9]{4})([0-9]{2})([0-9]{2}).*/\3\/\2\/\1/')
    echo "   • $label   ($svc)${created:+   נוצר: $created}"
    return 0
  fi
  return 1
}

echo "נמצאו הפריטים הבאים:"
FOUND=0
show "$GOVIL" "ת.ז. + סיסמת gov.il" && FOUND=1
if [ "$ONLY_GOVIL" = "0" ]; then
  show "$EMAIL" "סיסמת אפליקציה של המייל (לקריאת קוד OTP)" && FOUND=1
  show "$TOTP"  "סוד Google Authenticator" && FOUND=1
fi

if [ "$FOUND" = "0" ]; then
  echo "   (אין פריטים — אין מה לשחרר)"
  echo
  echo "אם עדיין מופיעה בקשת סיסמה, כנראה שהיא שייכת לאפליקציה אחרת."
  exit 0
fi

echo
echo "מה שיימחק תצטרך להזין מחדש בהגדרות ⚙ של האפליקציה:"
echo "   • ת.ז. וסיסמת gov.il — אתה יודע אותן."
if [ "$ONLY_GOVIL" = "0" ]; then
  echo "   • סיסמת אפליקציה של Gmail — ⚠ לא ניתנת לשחזור."
  echo "     תצטרך ליצור חדשה: myaccount.google.com/apppasswords"
  echo "     כדי לדלג עליה:  bash fix_keychain.sh --govil"
fi

echo
read -p "למחוק ולשחרר? הקלד YES לאישור: " ANS
[ "$ANS" = "YES" ] || { echo "בוטל — לא נמחק דבר."; exit 1; }

echo
del() {
  local svc="$1"
  if security delete-generic-password -s "$svc" >/dev/null 2>&1; then
    echo "   ✓ שוחרר: $svc"
    # ייתכנו כמה פריטים באותו שם — מוחקים עד שלא נשאר
    while security delete-generic-password -s "$svc" >/dev/null 2>&1; do :; done
  fi
}
del "$GOVIL"
if [ "$ONLY_GOVIL" = "0" ]; then
  del "$EMAIL"
  del "$TOTP"
fi

echo
echo "════════════════════════════════════════════"
echo "  ✓ החסימה שוחררה"
echo "════════════════════════════════════════════"
echo
echo "השלבים הבאים:"
echo "  1. הפעל מחדש:        bash start.sh"
echo "  2. פתח הגדרות ⚙  →  לשונית gov.il  →  הזן ת.ז. וסיסמה"
echo "  3. אם יקפוץ חלון אישור — לחץ  Always Allow  (לא Allow)"
echo
echo "'Always Allow' מוסיף את הפייתון הנוכחי לרשימת ההרשאות לצמיתות,"
echo "ולכן הבקשה לא תחזור. 'Allow' מאשר פעם אחת בלבד — ומכאן הלולאה."
