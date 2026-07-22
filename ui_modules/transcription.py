"""Transcription -- whisper-based audio transcription."""
from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

CHUNK_DURATION_S = 300
CHUNK_OVERLAP_S = 15
MAX_PARALLEL_CHUNKS = 4

# Module-level shared state
_transcription_jobs: dict[str, dict] = {}
_transcription_lock = threading.Lock()
_whisper_model = None
_whisper_model_lock = threading.Lock()


def _get_whisper_model(language: str):
    """Load (or reuse) the whisper model -- thread-safe singleton."""
    global _whisper_model
    with _whisper_model_lock:
        if _whisper_model is not None:
            return _whisper_model
    from faster_whisper import WhisperModel
    model_id = ("ivrit-ai/whisper-large-v3-turbo-ct2" if language == "he"
                else "large-v3-turbo")
    m = WhisperModel(model_id, device="auto", compute_type="int8")
    with _whisper_model_lock:
        _whisper_model = m
    return m


def _split_audio(audio_path: str) -> list[str]:
    """Split audio into ~10-min chunks using ffmpeg."""
    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", audio_path],
        capture_output=True, text=True, timeout=30)
    try:
        total_sec = float(probe.stdout.strip())
    except (ValueError, AttributeError):
        return [audio_path]

    if total_sec <= CHUNK_DURATION_S + 60:
        return [audio_path]

    chunk_dir = audio_path + ".chunks"
    os.makedirs(chunk_dir, exist_ok=True)
    chunks = []
    start = 0.0
    idx = 0
    while start < total_sec:
        # 16 kHz mono WAV — whisper's native input format. Re-encoding here
        # (instead of "-c copy") shrinks decode time inside the model and lets
        # any source format (m4a/ogg/opus) work uniformly.
        out = os.path.join(chunk_dir, f"chunk_{idx:03d}.wav")
        actual_start = max(0, start - (CHUNK_OVERLAP_S if idx > 0 else 0))
        duration = CHUNK_DURATION_S + (CHUNK_OVERLAP_S if idx > 0 else 0)
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ss", str(actual_start),
             "-t", str(duration), "-ar", "16000", "-ac", "1",
             "-loglevel", "error", out],
            timeout=300, capture_output=True)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            chunks.append(out)
        start += CHUNK_DURATION_S
        idx += 1
    return chunks if chunks else [audio_path]


def _transcribe_chunk(model, chunk_path: str, time_offset: float,
                      language: str, job_id: str, chunk_idx: int,
                      total_chunks: int):
    """Transcribe one chunk -- returns list of (start, end, text)."""
    segments, info = model.transcribe(chunk_path, language=language,
                                      vad_filter=True, beam_size=5)
    results = []
    for seg in segments:
        abs_start = seg.start + time_offset
        abs_end = seg.end + time_offset
        text = seg.text.strip()
        if text:
            results.append((abs_start, abs_end, text))
            with _transcription_lock:
                job = _transcription_jobs.get(job_id)
                if job is not None:
                    job.setdefault("partial_lines", []).append(
                        f"[{_fmt_ts(abs_start)} → {_fmt_ts(abs_end)}] {text}")
    with _transcription_lock:
        job = _transcription_jobs.get(job_id)
        if job is not None:
            done = job.get("chunks_done", 0) + 1
            job["chunks_done"] = done
            pct = 0.15 + 0.80 * (done / total_chunks)
            job.update(progress=pct,
                       message=f"מתמלל… {done}/{total_chunks} חלקים")
            # Auto-save partial after each chunk
            partial = job.get("partial_lines", [])
            if partial:
                try:
                    tr_dir = job.get("transcriptions_dir", "")
                    orig = job.get("original_name", "")
                    if tr_dir and orig:
                        stem = Path(orig).stem
                        p = os.path.join(tr_dir, f"{stem}_partial.md")
                        partial_text = "\n\n".join(partial)
                        with open(p, "w", encoding="utf-8") as fh:
                            fh.write(f"---\nsource: {orig}\nstatus: in-progress ({done}/{total_chunks})\n---\n\n{partial_text}\n")
                except Exception:
                    pass
    return results


def _tlog(msg: str) -> None:
    """Write a transcription line to the shared live log (latest.log) so it
    shows in the app's log window (תמלול tab), plus stdout."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [תמלול] {msg}"
    print(line, flush=True)
    try:
        from LIAS import config as _cfg
        logp = _cfg.COURT_DOCS_DIR / "logs" / "latest.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        with open(logp, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _transcribe_worker(job_id: str, audio_path: str, language: str,
                       original_name: str, transcriptions_dir: str):
    """Split -> parallel transcribe -> merge -> save MD."""
    def _update(state: str, **kw):
        with _transcription_lock:
            _transcription_jobs[job_id].update(state=state, **kw)
        if kw.get("message"):
            _tlog(kw["message"])

    with _transcription_lock:
        _transcription_jobs[job_id]["transcriptions_dir"] = transcriptions_dir
        _transcription_jobs[job_id]["original_name"] = original_name
    _tlog(f"▶ מתחיל תמלול: {original_name}")
    _update("loading_model", progress=0.05, message="טוען מודל תמלול…")
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        _update("error", message="faster-whisper לא מותקן — pip install faster-whisper")
        return

    try:
        model = _get_whisper_model(language)
    except Exception as exc:
        _update("error", message=f"שגיאה בטעינת מודל: {exc}")
        return

    _update("splitting", progress=0.08, message="מפצל את ההקלטה…")
    try:
        chunks = _split_audio(audio_path)
    except Exception:
        chunks = [audio_path]

    total_chunks = len(chunks)
    _update("transcribing", progress=0.15,
            message=f"מתמלל… 0/{total_chunks} חלקים",
            total_chunks=total_chunks, chunks_done=0)

    all_results: list[list] = [None] * total_chunks  # type: ignore
    errors = []

    if total_chunks == 1:
        try:
            all_results[0] = _transcribe_chunk(
                model, chunks[0], 0.0, language, job_id, 0, total_chunks)
        except Exception as exc:
            errors.append(str(exc))
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        futures = {}
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CHUNKS, total_chunks)) as pool:
            for ci, cp in enumerate(chunks):
                time_offset = ci * CHUNK_DURATION_S
                fut = pool.submit(_transcribe_chunk, model, cp, time_offset,
                                  language, job_id, ci, total_chunks)
                futures[fut] = ci
            for fut in as_completed(futures):
                ci = futures[fut]
                try:
                    all_results[ci] = fut.result()
                except Exception as exc:
                    errors.append(f"חלק {ci + 1}: {exc}")

    if all(r is None for r in all_results):
        # Save partial results if any exist
        with _transcription_lock:
            job = _transcription_jobs.get(job_id, {})
            partial = job.get("partial_lines", [])
        if partial:
            stem = Path(original_name).stem
            md_path = os.path.join(transcriptions_dir, f"{stem}_partial.md")
            partial_text = "\n\n".join(partial)
            md_content = (f"---\nsource: {original_name}\nlanguage: {language}\n"
                          f"status: partial (errors during transcription)\n"
                          f"transcribed_at: {datetime.now().isoformat(timespec='seconds')}\n"
                          f"---\n\n# תמלול חלקי — {stem}\n\n{partial_text}\n")
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write(md_content)
            _update("error", message="שגיאה בתמלול (נשמר חלקי): " + "; ".join(errors),
                    md_path=md_path, md_name=f"{stem}_partial.md")
        else:
            _update("error", message="שגיאה בתמלול: " + "; ".join(errors))
        return

    _update("merging", progress=0.96, message="ממזג תוצאות…")

    merged = []
    for chunk_results in all_results:
        if chunk_results is None:
            continue
        for start, end, text in chunk_results:
            if merged and abs(start - merged[-1][0]) < 2.0 and text == merged[-1][2]:
                continue
            merged.append((start, end, text))
    merged.sort(key=lambda x: x[0])

    deduped = []
    for s, e, t in merged:
        if deduped and abs(s - deduped[-1][0]) < 1.5 and t == deduped[-1][2]:
            continue
        deduped.append((s, e, t))

    lines = [f"[{_fmt_ts(s)} → {_fmt_ts(e)}] {t}" for s, e, t in deduped]
    transcript = "\n\n".join(lines)

    stem = Path(original_name).stem
    md_path = os.path.join(transcriptions_dir, f"{stem}.md")
    total_dur = deduped[-1][1] if deduped else 0
    md_content = f"""---
source: {original_name}
language: {language}
duration: {_fmt_ts(total_dur)}
chunks: {total_chunks}
transcribed_at: {datetime.now().isoformat(timespec='seconds')}
---

# תמלול — {stem}

{transcript}
"""
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md_content)

    _update("done", progress=1.0, message="הושלם ✓",
            md_path=md_path, md_name=f"{stem}.md",
            duration=total_dur)
    # Remove partial file
    partial_path = os.path.join(transcriptions_dir, f"{stem}_partial.md")
    try:
        os.unlink(partial_path)
    except OSError:
        pass

    try:
        os.unlink(audio_path)
    except OSError:
        pass
    chunk_dir = audio_path + ".chunks"
    if os.path.isdir(chunk_dir):
        import shutil
        shutil.rmtree(chunk_dir, ignore_errors=True)


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _list_transcriptions(transcriptions_dir: str) -> list[dict]:
    """List every transcript with its status so the UI shows one folder of
    completed and partial/stopped transcripts. A "{stem}_partial.md" is one
    that was stopped mid-way (chunked) — its audio is kept so it can resume."""
    out = []
    seen_stems = set()
    files = sorted(Path(transcriptions_dir).glob("*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    # completed first (so a completed stem hides its leftover partial)
    for f in files:
        is_partial = f.name.endswith("_partial.md")
        stem = f.name[:-len("_partial.md")] if is_partial else f.name[:-3]
        if not is_partial:
            seen_stems.add(stem)
    for f in files:
        is_partial = f.name.endswith("_partial.md")
        stem = f.name[:-len("_partial.md")] if is_partial else f.name[:-3]
        if is_partial and stem in seen_stems:
            continue  # a completed version exists — skip the leftover partial
        status = "partial" if is_partial else "done"
        # partials keep their audio for playback/resume
        audio = None
        for ext in (".mp3", ".m4a", ".wav", ".ogg", ".webm"):
            cand = Path(transcriptions_dir) / f"{stem}{ext}"
            if cand.exists():
                audio = cand.name; break
        out.append({"name": f.name, "stem": stem, "status": status,
                    "has_audio": audio is not None, "audio_name": audio,
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")})
    return out
