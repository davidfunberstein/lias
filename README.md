# LIAS — Legal Intelligence Automation System

> מערכת אוטומציה משפטית לעורכי דין בישראל  
> Legal automation platform for Israeli law firms

---

## What It Does

LIAS downloads, organizes, and analyzes legal documents from Israeli government court portals automatically. It runs a local FastAPI server that drives real browser sessions (Playwright) to log in to court portals, download case documents, extract text via OCR, and analyze content with an LLM.

**Three portals supported:**
| Code | Portal | Hebrew |
|------|--------|--------|
| NET | Israeli Courts (Netz HaMishpat) | נט המשפט |
| BDR | Rabbinical Court | בית הדין הרבני |
| ECA | Enforcement & Collection Authority | הוצאה לפועל |

---

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js (for UI dev, optional)
- macOS (tested) / Linux

### Install

```bash
git clone <repo>
cd legal-AI-app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### Run

```bash
python app.py
# Server starts at http://localhost:8500
# Open http://localhost:8500 in your browser
```

### First-time login
1. Open the app → Settings → select portal (NET / BDR / ECA)
2. Click "פתח דפדפן" — a Chrome window opens
3. Log in manually to the government portal
4. Return to the app — it is now authenticated

---

## Architecture

```
http://localhost:8500
        │
        ▼
   LIAS/api.py          FastAPI — all endpoints
        │
   LIAS/collector_bridge.py   download orchestrator
        ├── NET loop     Playwright (browser_profile/)
        ├── BDR runner   Playwright (browser_profile_bdr/)
        └── ECA runner   Playwright (browser_profile_eca/)
        │
        ▼
   core/doc_pipeline.py  LLM analysis (background thread)
        │
   core/vector_store.py  SQLite FTS5 index
        │
   lias.db               SQLite — all data
```

### Key files

| File | Purpose |
|------|---------|
| `LIAS/api.py` | FastAPI server — all HTTP endpoints |
| `LIAS/collector_bridge.py` | Download orchestrator, pause/resume, import hooks |
| `core/doc_pipeline.py` | Background LLM analysis pipeline |
| `core/vector_store.py` | SQLite FTS5 full-text search index |
| `core/notebook_bridge.py` | Google NotebookLM integration (optional) |
| `core/pdf_to_text.py` | PDF → text via OCR (Groq / Gemini) |
| `tools/session_server.py` | Standalone cookie export server (port 7777) |
| `tools/import_session.py` | Import exported cookies into LIAS browser |
| `ui_demo/engine.js` | Download bubble UI, SSE listener |
| `ui_demo/views.js` | Case views, viewers panel |

---

## LLM Analysis

Documents are analyzed automatically after download. No action required — a background worker processes the queue.

### Requirements
A **Groq API key** must be set (free tier is sufficient):

```bash
# Option 1 — macOS keychain (recommended, persists across restarts):
python3 -c "import keyring; keyring.set_password('gov-il-connect', 'groq_api_key', 'gsk_...')"

# Option 2 — environment variable:
export GROQ_API_KEY=gsk_...

# Option 3 — in the app Settings UI under "Groq API Key"
```

### What gets extracted per document
`doc_category` · `subject` · `summary` · `topics[]` · `submitter` · `respondent` · `dates_mentioned[]` · `next_hearing` · `legal_citations[]` · `relief_requested` · `decision_outcome` · `keywords[]`

### Test the pipeline manually

```bash
# Analyze a specific case (replace 17 with a real sub_case_id):
curl -X POST http://localhost:8500/api/analyze/case/17

# Check progress:
curl http://localhost:8500/api/vector/stats

# Search across all analyzed documents:
curl "http://localhost:8500/api/vector/search?q=חוזה&limit=5"
```

---

## API Reference

Base URL: `http://localhost:8500`

### Cases
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/cases` | List all cases |
| GET | `/api/cases/{id}` | Case details |
| GET | `/api/cases/{id}/viewers` | Who viewed a decision (NET only) |
| GET | `/api/browser/status` | Status of all 3 browser instances |
| GET | `/api/events` | SSE stream — live download updates |

### Downloads
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/download/start` | Start a download job |
| POST | `/api/download/pause/{job_id}` | Pause download |
| POST | `/api/download/resume/{job_id}` | Resume download |

### Analysis
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/analyze/case/{id}` | Trigger LLM analysis for full case |
| GET | `/api/analyze/case/{id}` | Retrieve existing analysis |
| GET | `/api/analyze/doc/{id}` | Single document analysis |
| POST | `/api/analyze/notebook/{id}` | Run NotebookLM pipeline for case |

### Search
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/vector/search?q=...&limit=10` | Full-text search (FTS5, Hebrew) |
| GET | `/api/vector/stats` | Index stats: analyzed docs, chunks, categories |

### Tools
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tools/export_session?portal=NET` | Export session cookies from internal browser |
| POST | `/api/actions/reorganize_by_client` | Re-assign cases to clients |
| POST | `/api/actions/reorganize_folders` | Reorganize folders by court type |

---

## Cookie Export Tool

Lets a lawyer export their government portal session cookies and share them with a colleague or send to a client.

```bash
# Run locally:
python tools/session_server.py
# Opens browser + web UI at http://localhost:7777

# Expose publicly via ngrok (so a remote colleague can use it):
brew install ngrok
ngrok config add-authtoken <TOKEN>   # one-time setup
python tools/session_server.py &
ngrok http 7777
# Send the https://abc.ngrok-free.app URL to your colleague
```

The colleague opens the link, logs into the government portal, clicks Done — cookies are available for download or are emailed automatically.

---

## Full-Text Search (FTS5)

All analyzed documents are indexed in SQLite FTS5 with Hebrew tokenization (unicode61, diacritics removed — searches work with or without nikud).

**Schema:**
- `doc_analysis` — one row per document, structured fields
- `doc_chunks` — text split into ~600-char overlapping chunks
- `doc_chunks_fts` — virtual FTS5 table over doc_chunks (auto-maintained via triggers)

**Reserved:** `doc_chunks.embedding BLOB` — slot for future float32 vector embeddings (sentence-transformers / OpenAI embed API). Currently empty; FTS5 BM25 is used for ranking.

---

## NotebookLM Integration (Optional)

Deep case analysis via Google NotebookLM. Each case gets its own notebook; all documents are uploaded as sources; 7 structured questions are asked.

**Setup:**
1. `pip install notebooklm-py`
2. Export Google cookies from Chrome → save as `google_notebook_cookies.json` beside `lias.db`
3. `POST /api/analyze/notebook/{sub_case_id}`

Output saved to `case_analysis/{sub_case_id}.json`.

---

## Database

SQLite at `lias.db`. Main tables:

| Table | Contents |
|-------|----------|
| `clients` | Client records |
| `cases` | Top-level cases |
| `sub_cases` | Individual court instances |
| `documents` | Downloaded documents |
| `jobs` | Download job queue |
| `doc_analysis` | LLM analysis results per document |
| `doc_chunks` | Raw text chunks for search |
| `doc_chunks_fts` | FTS5 virtual table |

---

## Documentation

All guides and specs are in [`docs/`](docs/):

| File | Language | Contents |
|------|----------|----------|
| `LIAS_System_Documentation.docx` | HE + EN | Full technical reference |
| `LIAS_User_Guide_EN.docx` | EN | User guide |
| `LIAS_מדריך_משתמש.docx` | HE | מדריך משתמש |
| `LIAS_מדריך_מקיף.docx` | HE | מדריך מקיף |
| `LIAS_פרזנטציה.docx` | HE | פרזנטציה |
| `LIAS_הדרכה_מעוצבת.pdf` | HE | הדרכה מעוצבת |

---

## Development

```bash
# Run tests:
bash run_tests.sh

# Rebuild DB schema (non-destructive):
python rebuild_db.py

# Reset everything (destructive!):
bash reset_all.sh
```

---

## License

Private — internal use only.
