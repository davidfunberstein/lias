# LIAS — legal case-management app
# Build:  docker build -t lias .
# Run:    docker compose up   (see docker-compose.yml)
FROM python:3.11-slim

# System deps: ffmpeg (transcription), LibreOffice (Word→PDF preview),
# and the libraries Playwright's Chromium needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libreoffice-writer fonts-dejavu fonts-freefont-ttf \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir playwright faster-whisper \
    && playwright install --with-deps chromium

COPY . .

# Data lives on mounted volumes (see docker-compose.yml):
#   /app/court_documents  — downloaded files
#   /app/lias.db          — database
EXPOSE 8500
ENV LIAS_HOST=0.0.0.0
CMD ["python3", "app.py", "--no-browser"]
