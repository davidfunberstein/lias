#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  LIAS — עבודה מול GitHub מהטרמינל, בלי כלים חיצוניים
#
#  אין צורך להתקין gh. הסקריפט משתמש באותו אישור GitHub שכבר שמור
#  אצלך ב-Keychain (זה שמשמש את git push), דרך ה-API הרשמי.
#  הטוקן לא מודפס, לא נשמר ולא נכתב לשום קובץ.
#
#  ── פקודות ────────────────────────────────────────────────────
#    bash pr.sh sync                 משיכת העדכונים האחרונים ל-main
#    bash pr.sh new <שם-ענף>         ענף חדש מ-main מעודכן
#    bash pr.sh open "<כותרת>"       קומיט + פוש + פתיחת PR
#    bash pr.sh status               מצב ה-PR של הענף הנוכחי
#    bash pr.sh merge                מיזוג ה-PR, מחיקת הענף, חזרה ל-main
#    bash pr.sh ship "<כותרת>"       הכל בפקודה אחת: קומיט+פוש+PR+מיזוג
#
#  ── תהליך עבודה טיפוסי ────────────────────────────────────────
#    bash pr.sh new fix/eca-login    # פותחים ענף
#    ... עורכים קבצים ...
#    bash pr.sh open "תיקון התחברות להוצל\"פ"
#    bash pr.sh merge                # אחרי שבדקת שהכל עובד
#
#  ── או פקודה אחת שעושה הכל ─────────────────────────────────────
#    bash pr.sh ship "תיקון התחברות"   # קומיט → פוש → PR → מיזוג
# ═══════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

CMD="${1:-help}"

# ── עזרים ─────────────────────────────────────────────────────────
_repo() {   # davidfunberstein/lias  מתוך כתובת ה-origin
  git remote get-url origin 2>/dev/null \
    | sed -E 's#^git@github.com:#https://github.com/#' \
    | sed -E 's#^https://github.com/##; s#\.git$##'
}
_token() {  # נשאב מה-Keychain אל תוך המשתנה בלבד
  printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null \
    | sed -n 's/^password=//p'
}
_api() {    # _api METHOD PATH [json-file]
  local m="$1" p="$2" f="${3:-}" t; t=$(_token)
  [ -z "$t" ] && { echo "✗ לא נמצא אישור GitHub. הרץ פעם אחת: git push"; exit 1; }
  if [ -n "$f" ]; then
    curl -s -X "$m" "https://api.github.com/repos/$(_repo)$p" \
      -H "Authorization: token $t" -H "Accept: application/vnd.github+json" -d @"$f"
  else
    curl -s -X "$m" "https://api.github.com/repos/$(_repo)$p" \
      -H "Authorization: token $t" -H "Accept: application/vnd.github+json"
  fi
}
_branch() { git rev-parse --abbrev-ref HEAD; }
_jq() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# ── sync ──────────────────────────────────────────────────────────
if [ "$CMD" = "sync" ]; then
  echo "→ מושך את העדכונים האחרונים…"
  git fetch origin
  if [ -n "$(git status --porcelain)" ]; then
    echo "  ⚠ יש שינויים לא שמורים — שומר בצד (git stash)"
    git stash push -m "pr.sh sync $(date +%F_%H%M)" >/dev/null
    echo "    לשחזור: git stash pop"
  fi
  git checkout main >/dev/null 2>&1 || true
  git pull --ff-only origin main
  echo "✓ main מעודכן"
  exit 0
fi

# ── new ───────────────────────────────────────────────────────────
if [ "$CMD" = "new" ]; then
  NAME="${2:-}"
  [ -z "$NAME" ] && { echo "שימוש: bash pr.sh new <שם-ענף>"; exit 1; }
  echo "→ מרענן את main ופותח ענף '$NAME'…"
  git fetch origin
  git checkout -b "$NAME" origin/main
  echo "✓ אתה על הענף '$NAME'. ערוך קבצים ואז:  bash pr.sh open \"כותרת\""
  exit 0
fi

# ── open ──────────────────────────────────────────────────────────
if [ "$CMD" = "open" ]; then
  TITLE="${2:-}"
  BR=$(_branch)
  [ "$BR" = "main" ] && { echo "✗ אתה על main. פתח ענף:  bash pr.sh new <שם>"; exit 1; }
  [ -z "$TITLE" ] && { echo "שימוש: bash pr.sh open \"כותרת ה-PR\""; exit 1; }

  # git diff misses UNTRACKED files, so a brand-new file looked like
  # "nothing to commit" and the push created an empty branch.
  if [ -n "$(git status --porcelain)" ]; then
    echo "→ מקמט את השינויים…"
    git add -A
    git commit -q -m "$TITLE"
  else
    echo "→ אין שינויים חדשים לקמט"
  fi

  echo "→ דוחף את '$BR'…"
  git push -q -u origin "$BR"

  EXIST=$(_api GET "/pulls?head=$(_repo | cut -d/ -f1):$BR&state=open" | _jq "d[0]['html_url'] if d else ''")
  if [ -n "$EXIST" ]; then
    echo "✓ כבר קיים PR פתוח — העדכון נדחף אליו:"
    echo "  $EXIST"; exit 0
  fi

  TMP=$(mktemp)
  BODY=$(git log origin/main.."$BR" --pretty='- %s' | head -30)
  python3 - "$TITLE" "$BR" "$BODY" > "$TMP" <<'PY'
import json,sys
title,head,body=sys.argv[1],sys.argv[2],sys.argv[3]
json.dump({"title":title,"head":head,"base":"main",
           "body":"## מה השתנה\n\n"+(body or "- "+title)}, sys.stdout, ensure_ascii=False)
PY
  URL=$(_api POST "/pulls" "$TMP" | _jq "d.get('html_url') or d")
  rm -f "$TMP"
  echo "✓ נפתח PR:"; echo "  $URL"
  exit 0
fi

# ── status ────────────────────────────────────────────────────────
if [ "$CMD" = "status" ]; then
  BR=$(_branch)
  _api GET "/pulls?head=$(_repo | cut -d/ -f1):$BR&state=open" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if not d: print('אין PR פתוח לענף הזה. פתח עם:  bash pr.sh open \"כותרת\"')
else:
    p=d[0]
    print(f\"#{p['number']}  {p['title']}\")
    print(f\"  {p['head']['ref']} → {p['base']['ref']}   מצב: {p['state']}\")
    print(f\"  {p['html_url']}\")"
  exit 0
fi

# ── merge ─────────────────────────────────────────────────────────
if [ "$CMD" = "merge" ]; then
  BR=$(_branch)
  [ "$BR" = "main" ] && { echo "✗ אתה על main — אין מה למזג"; exit 1; }
  NUM=$(_api GET "/pulls?head=$(_repo | cut -d/ -f1):$BR&state=open" | _jq "d[0]['number'] if d else ''")
  [ -z "$NUM" ] && { echo "✗ לא נמצא PR פתוח לענף '$BR'"; exit 1; }

  echo "→ ממזג PR #$NUM ($BR → main)…"
  read -p "  לאשר מיזוג? [y/N] " OK
  [ "$OK" = "y" ] || { echo "בוטל."; exit 1; }

  TMP=$(mktemp); printf '{"merge_method":"squash"}' > "$TMP"
  RES=$(_api PUT "/pulls/$NUM/merge" "$TMP"); rm -f "$TMP"
  echo "$RES" | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('✓ '+d['message'] if d.get('merged') else '✗ '+str(d.get('message')))"

  echo "$RES" | grep -q '\"merged\": *true' || exit 1
  echo "→ מנקה: חוזר ל-main ומוחק את הענף…"
  git checkout -q main
  git pull -q --ff-only origin main
  git branch -q -D "$BR" 2>/dev/null || true
  git push -q origin --delete "$BR" 2>/dev/null || true
  echo "✓ מוזג. אתה על main מעודכן."
  exit 0
fi

# ── ship: open + merge, no second command ─────────────────────────
if [ "$CMD" = "ship" ]; then
  TITLE="${2:-}"
  [ -z "$TITLE" ] && { echo "שימוש: bash pr.sh ship \"כותרת\""; exit 1; }
  bash "$0" open "$TITLE" || exit 1
  echo
  echo "y" | bash "$0" merge
  exit $?
fi

# ── help ──────────────────────────────────────────────────────────
# print the header comment block only (stops at the first non-comment line)
awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"
