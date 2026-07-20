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

# ── native install ──────────────────────────────────────────
command -v python3 >/dev/null || { echo "✗ Python 3 לא מותקן"; exit 1; }
echo "→ Python: $(python3 --version)"

# Homebrew deps (Mac) — ffmpeg for transcription
if command -v brew >/dev/null; then
    command -v ffmpeg >/dev/null || { echo "→ מתקין ffmpeg…"; brew install ffmpeg; }
    [ -d "/Applications/LibreOffice.app" ] || echo "⚠ מומלץ להתקין LibreOffice לתצוגת Word: brew install --cask libreoffice"
else
    command -v ffmpeg >/dev/null || echo "⚠ התקן ffmpeg ידנית (נדרש לתמלול)"
fi

echo "→ מתקין חבילות Python…"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements.txt
python3 -m pip install --quiet playwright faster-whisper

echo "→ מתקין דפדפן אוטומציה (Chromium)…"
python3 -m playwright install chromium

echo ""
echo "✓ ההתקנה הושלמה!"
echo "  הרצה:   python3 app.py"
echo "  כתובת:  http://localhost:8500"
echo ""
echo "  אופציונלי:"
echo "  • Google Drive: שים credentials.json (OAuth) בתיקייה זו"
echo "  • OCR/AI: הזן מפתח Groq או xAI בהגדרות ⚙ באפליקציה"
