#!/bin/bash
# LIAS — one-shot installer / התקנה אוטומטית מלאה
# Usage:  bash install.sh          (native install — recommended on Mac)
#         bash install.sh docker   (containerized — requires Docker Desktop)
set -e
cd "$(dirname "$0")"

echo "════════════════════════════════════════════"
echo "  LIAS — התקנה אוטומטית"
echo "════════════════════════════════════════════"

if [ "$1" = "docker" ]; then
    command -v docker >/dev/null || { echo "✗ Docker לא מותקן — התקן Docker Desktop קודם: https://docker.com"; exit 1; }
    echo "→ בונה ומריץ עם Docker…"
    docker compose up --build -d
    echo "✓ מוכן! פתח: http://localhost:8500"
    exit 0
fi

# ── locate a suitable Python (3.9+) ──────────────────────────
PY=""
for cand in python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        VER=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")
        MAJ=${VER%%.*}; MIN=${VER##*.}
        if [ "$MAJ" = "3" ] && [ "$MIN" -ge 9 ] 2>/dev/null; then PY="$cand"; break; fi
    fi
done
if [ -z "$PY" ]; then
    echo "✗ לא נמצא Python 3.9 ומעלה. התקן מ- https://python.org ונסה שוב."
    echo "  (במק:  brew install python@3.12)"
    exit 1
fi
echo "→ משתמש ב-$PY ($($PY --version))"

# ── system deps ──────────────────────────────────────────────
if command -v brew >/dev/null 2>&1; then
    command -v ffmpeg >/dev/null 2>&1 || { echo "→ מתקין ffmpeg…"; brew install ffmpeg || echo "  ⚠ התקנת ffmpeg נכשלה — התקן ידנית"; }
    [ -d "/Applications/LibreOffice.app" ] || echo "  ℹ (אופציונלי) לתצוגת Word: brew install --cask libreoffice"
elif command -v apt-get >/dev/null 2>&1; then
    echo "→ מתקין ffmpeg + libreoffice (apt)…"
    sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg libreoffice-writer || echo "  ⚠ בדוק הרשאות sudo"
else
    command -v ffmpeg >/dev/null 2>&1 || echo "  ⚠ התקן ffmpeg ידנית (נדרש לתמלול)"
fi

# ── Python packages ──────────────────────────────────────────
echo "→ מתקין חבילות Python…"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt
"$PY" -m pip install --quiet playwright faster-whisper

echo "→ מתקין דפדפן אוטומציה (Chromium)…"
"$PY" -m playwright install chromium

# ── rebuild DB if documents shipped without an up-to-date lias.db ──
if [ -d "court_documents/downloads" ] && [ ! -f "lias.db" ]; then
    echo "→ נמצאו מסמכים ללא DB — בונה את lias.db מהמסמכים…"
    "$PY" rebuild_db.py || echo "  ⚠ בניית DB נכשלה — הרץ ידנית: $PY rebuild_db.py"
fi

echo ""
echo "✓ ההתקנה הושלמה!"
echo "  הרצה:   $PY app.py       →  http://localhost:8500"
echo ""
echo "  אופציונלי:"
echo "  • Google Drive: שים credentials.json (OAuth) בתיקייה זו"
echo "  • OCR/AI: הזן מפתח Groq/xAI בהגדרות ⚙ באפליקציה"
echo "  • אישורי gov.il: הזן ת\"ז וסיסמה בהגדרות ⚙ (נשמר ב-Keychain בלבד)"
