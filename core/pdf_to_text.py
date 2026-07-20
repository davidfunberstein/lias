"""PDF → Hebrew text via Gemini Vision (handles scanned documents).

Integrates with the download pipeline: after a PDF is saved,
call `ocr_if_needed(pdf_path, session_settings)` to extract text.
Results are saved as <pdf_stem>.txt beside the PDF and cached in
ocr_cache.json (same folder) so re-runs are free.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

_MODEL_NAME = "gemini-2.0-flash-lite"
_MIN_EMBEDDED_CHARS = 80   # pages with more chars than this are not scanned
_DPI = 150
_RATE_PAUSE = 0.35          # seconds between Gemini calls

# Set to True after any quota/billing/network error so we stop retrying this session
_gemini_unavailable: bool = False

def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("429", "quota", "billing", "credit", "resource_exhausted",
                                   "payment", "prepayment", "rate limit", "too many"))


# ── dependency check ────────────────────────────────────────────────────────

def _check_deps() -> tuple[bool, bool]:
    try:
        import fitz  # noqa: F401
        fitz_ok = True
    except ImportError:
        fitz_ok = False
    try:
        import google.generativeai  # noqa: F401
        genai_ok = True
    except ImportError:
        genai_ok = False
    return fitz_ok, genai_ok


# ── cache helpers ────────────────────────────────────────────────────────────

def _page_hash(img_bytes: bytes) -> str:
    return hashlib.sha256(img_bytes).hexdigest()[:20]


def _load_cache(cache_path: Path) -> dict:
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(cache_path: Path, data: dict) -> None:
    try:
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass


# ── public API ───────────────────────────────────────────────────────────────

def is_ocr_needed(pdf_path: Path) -> bool:
    """Return True if the PDF is scanned (lacks sufficient embedded text)."""
    fitz_ok, _ = _check_deps()
    if not fitz_ok:
        return False
    import fitz
    try:
        doc = fitz.open(str(pdf_path))
        pages = len(doc)
        total_chars = sum(len(p.get_text().strip()) for p in doc)
        doc.close()
        if pages == 0:
            return False
        return (total_chars / pages) < _MIN_EMBEDDED_CHARS
    except Exception:
        return False


_OCR_PROMPT = (
    "This is a page from an Israeli legal document. "
    "Extract all text exactly as written, preserving Hebrew RTL order, "
    "paragraph breaks, section numbers, and bullet points. "
    "Return only the extracted text — no commentary."
)

_GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_XAI_URL = "https://api.x.ai/v1/chat/completions"
_XAI_MODEL = "grok-4-fast"


def _chat_endpoint(api_key: str) -> tuple[str, str]:
    """Route by key prefix: xai-… keys belong to xAI (Grok), anything else
    goes to Groq. Both speak the OpenAI chat format."""
    if (api_key or "").startswith("xai-"):
        return _XAI_URL, _XAI_MODEL
    return _GROQ_URL, _GROQ_MODEL


def resolve_ocr_provider(session_settings: dict) -> tuple[str, str]:
    """Return (provider, api_key). Groq is preferred (free tier); Gemini is
    the fallback. Keys come from settings, keychain, or environment.
    Groq עדיף (חינמי); Gemini נסיגה. מפתח מההגדרות / Keychain / סביבה."""
    import os
    groq_key = (session_settings.get("groq_api_key")
                or os.environ.get("GROQ_API_KEY", ""))
    if not groq_key:
        try:
            import keyring
            groq_key = keyring.get_password("gov-il-connect", "groq_api_key") or ""
        except Exception:
            pass
    if groq_key:
        return "groq", groq_key
    gem_key = session_settings.get("gemini_api_key", "")
    if not gem_key:
        try:
            import keyring
            gem_key = keyring.get_password("gov-il-connect", "gemini_api_key") or ""
        except Exception:
            pass
    if gem_key:
        return "gemini", gem_key
    return "", ""


def _groq_ocr_page(img_bytes: bytes, api_key: str) -> str:
    """OCR one page image via an OpenAI-compatible vision endpoint
    (Groq, or xAI/Grok when the key starts with xai-)."""
    import urllib.request
    url, model = _chat_endpoint(api_key)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(img_bytes).decode()}},
                {"type": "text", "text": _OCR_PROMPT},
            ],
        }],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return (data["choices"][0]["message"]["content"] or "").strip()


def groq_text_completion(prompt: str, api_key: str, timeout: int = 60) -> str:
    """Plain text completion via Groq/xAI (no image) — used for metadata
    extraction and for the /api/ocr/test ping."""
    import urllib.request
    url, model = _chat_endpoint(api_key)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return (data["choices"][0]["message"]["content"] or "").strip()


_META_PROMPT = (
    "לפניך טקסט של מסמך משפטי ישראלי. החזר JSON בלבד (ללא הסברים) עם השדות:\n"
    '{"subject": "נושא הבקשה/המסמך במשפט אחד", '
    '"topics": ["רשימת נושאים"], '
    '"attachments": ["רשימת נספחים שמוזכרים, אם יש"], '
    '"pages": <מספר עמודים אם ידוע אחרת null>, '
    '"decision_type": "מדבקה" או "החלטה כתובה" או null}\n\n'
    "הטקסט:\n"
)


def extract_doc_metadata(text: str, session_settings: dict) -> dict:
    """One extra LLM call: pull structured metadata out of the extracted text.
    Returns {} when no provider/key is configured or parsing fails."""
    provider, key = resolve_ocr_provider(session_settings)
    if provider != "groq" or not key or not text.strip():
        return {}
    try:
        raw = groq_text_completion(_META_PROMPT + text[:12000], key)
        # tolerate ```json fences
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        meta = json.loads(raw)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def extract_text_from_pdf(
    pdf_path: Path,
    api_key: str,
    logger: "Logger | None" = None,
    cache_path: Path | None = None,
    provider: str = "gemini",
) -> str:
    """Convert a PDF to Hebrew text using Gemini Vision for scanned pages.

    * Text-layer pages: extracted directly (free, instant).
    * Scanned pages: sent to Gemini as PNG images.
    * Each page result is cached by image hash — identical pages are never
      re-sent to the API.
    * Output saved as <pdf_stem>.txt beside the source PDF.

    Returns the full text as a string.
    """
    fitz_ok, genai_ok = _check_deps()
    if not fitz_ok:
        raise RuntimeError("pymupdf missing: pip install pymupdf")
    if not api_key:
        raise ValueError("OCR API key not set (Groq/Gemini)")

    import fitz

    model = None
    if provider == "gemini":
        if not genai_ok:
            raise RuntimeError("google-generativeai missing: pip install google-generativeai")
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(_MODEL_NAME)

    if cache_path is None:
        cache_path = pdf_path.parent / "ocr_cache.json"
    cache = _load_cache(cache_path)

    doc = fitz.open(str(pdf_path))
    pages_text: list[str] = []
    api_calls = 0

    for page_num, page in enumerate(doc):
        embedded = page.get_text().strip()
        if len(embedded) >= _MIN_EMBEDDED_CHARS:
            pages_text.append(embedded)
            continue

        mat = fitz.Matrix(_DPI / 72, _DPI / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        h = _page_hash(img_bytes)

        if h in cache:
            pages_text.append(cache[h])
            if logger:
                logger.info(f"OCR cache hit: {pdf_path.name} page {page_num + 1}")
            continue

        if logger:
            logger.info(f"OCR {provider}: {pdf_path.name} page {page_num + 1}")
        print(f"  [OCR/{provider}] page {page_num + 1}/{len(doc)}: {pdf_path.name}")

        try:
            if provider == "groq":
                text = _groq_ocr_page(img_bytes, api_key)
            else:
                resp = model.generate_content([
                    {
                        "mime_type": "image/png",
                        "data": base64.b64encode(img_bytes).decode(),
                    },
                    _OCR_PROMPT,
                ])
                text = resp.text.strip()
        except Exception as exc:
            if logger:
                logger.warn(f"OCR page {page_num + 1} error: {exc}")
            raise  # bubble up to ocr_if_needed which handles silently

        cache[h] = text
        _save_cache(cache_path, cache)
        pages_text.append(text)
        api_calls += 1
        time.sleep(_RATE_PAUSE)

    doc.close()

    full_text = "\n\n---\n\n".join(pages_text)

    txt_path = pdf_path.with_suffix(".txt")
    txt_path.write_text(full_text, encoding="utf-8")

    if logger:
        logger.ok(
            f"OCR done: {pdf_path.name} "
            f"({len(pages_text)} pages, {api_calls} API calls) -> {txt_path.name}"
        )
    return full_text


def ocr_if_needed(
    pdf_path: Path,
    session_settings: dict,
    logger: "Logger | None" = None,
) -> None:
    """Hook called after every successful PDF download.

    AUTOMATIC for everyone: runs whenever an OCR API key exists (Groq is
    preferred — free tier; Gemini fallback). No user click required.
    אוטומטי לכולם — רץ ברגע שיש מפתח (Groq עדיף, Gemini נסיגה), בלי לחיצה.

    Skips when the PDF has a text layer, a .txt already exists, or after a
    quota error (until next session). All failures are silent — OCR never
    interrupts the download pipeline.
    """
    global _gemini_unavailable

    if _gemini_unavailable:
        return
    provider, api_key = resolve_ocr_provider(session_settings)
    if not api_key:
        return
    # legacy opt-out: gemini_enabled=False only blocks the gemini path
    if provider == "gemini" and session_settings.get("gemini_enabled") is False:
        return
    if pdf_path.suffix.lower() != ".pdf":
        return
    txt_path = pdf_path.with_suffix(".txt")
    if txt_path.exists():
        return
    if not is_ocr_needed(pdf_path):
        return

    try:
        extract_text_from_pdf(pdf_path, api_key, logger=logger, provider=provider)
    except Exception as exc:
        if logger:
            logger.warn(f"OCR skipped ({pdf_path.name}): {exc}")
        if _is_quota_error(exc):
            _gemini_unavailable = True
            if logger:
                logger.warn(f"OCR disabled for this session: {provider} quota/billing issue")
