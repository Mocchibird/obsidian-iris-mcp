"""Voice helpers — Phase 2.1 (STT for Discord voice messages).

Single concern: turn an audio file on disk into a transcript string,
using ``faster-whisper`` (CTranslate2-backed Whisper, CPU-friendly).

Public surface:
    transcribe_audio(path, language='') → dict with 'text' and metadata
    is_audio_file(path) → True for the extensions Whisper handles via ffmpeg

The Whisper model is loaded lazily on first call and cached at module
level so subsequent calls are fast. The model and device are
configurable via env:
    IRIS_WHISPER_MODEL   default 'base' (75M params, multilingual)
    IRIS_WHISPER_DEVICE  default 'cpu' (set to 'cuda' if your TrueNAS
                         box has GPU passthrough configured)
    IRIS_WHISPER_COMPUTE default 'int8' on cpu, 'float16' on cuda

Notes on model choice:
    tiny   — 39M params, ~1× realtime CPU, lowest quality
    base   — 74M params, ~2× realtime CPU, good for clear speech
    small  — 244M params, ~6× realtime CPU, recommended for noisy or
             multilingual input
    medium — 769M params, ~15× realtime CPU, much better Korean /
             Japanese / German but slow on CPU
    large  — 1.5B params, GPU recommended

Discord voice messages encode to .ogg/Opus. ffmpeg (already in the
image for Phase 2) handles the decode under the hood.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .. import mcp
from ..core import get_vault_root

log = logging.getLogger(__name__)


# Audio file extensions Whisper + ffmpeg can decode. Discord voice messages
# are .ogg/Opus; manual uploads might be .mp3 / .wav / .m4a / .flac / .webm.
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".webm", ".aac"}

# Module-level model cache. faster-whisper's WhisperModel is thread-safe for
# `transcribe()` calls per their docs, so one instance shared across all
# voice-message turns is fine.
_model = None
_model_name = ""


def is_audio_file(filename: str) -> bool:
    """True if the filename's extension is something we can transcribe."""
    return Path(filename).suffix.lower() in _AUDIO_EXTS


def _load_model():
    """Lazy-load the Whisper model on first call. Reloads if the env-
    configured model name changes between calls (unlikely but defensive)."""
    global _model, _model_name
    desired = os.environ.get("IRIS_WHISPER_MODEL", "base").strip() or "base"
    if _model is not None and _model_name == desired:
        return _model
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed — rebuild the container with "
            "the updated pyproject.toml (Phase 2.1 added the dep)"
        ) from e
    device = os.environ.get("IRIS_WHISPER_DEVICE", "cpu").strip() or "cpu"
    # int8 is the right default on CPU (4× faster than float32, near-identical
    # accuracy for speech). float16 is the right default on CUDA.
    default_compute = "float16" if device == "cuda" else "int8"
    compute = os.environ.get("IRIS_WHISPER_COMPUTE", default_compute).strip()
    log.info(
        "voice: loading Whisper model %r on %s (compute=%s)",
        desired, device, compute,
    )
    t0 = time.monotonic()
    _model = WhisperModel(desired, device=device, compute_type=compute)
    log.info("voice: model loaded in %.1fs", time.monotonic() - t0)
    _model_name = desired
    return _model


@mcp.tool()
def transcribe_audio(
    path: str,
    language: str = "",
    beam_size: int = 5,
) -> str:
    """Transcribe an audio file at ``path`` to text via Whisper.

    Args:
        path: Vault-relative OR absolute path to an audio file. Discord
            voice messages land at ``90_Inbox/inbox/<timestamp>_<name>.ogg``
            via the normal attachment-save flow.
        language: ISO 639-1 hint ('en', 'de', 'ko', 'ja', …). Empty (the
            default) lets Whisper auto-detect — works well for clean
            audio but slower. Pass a hint when you know the language.
        beam_size: Beam-search width. 5 (default) is the standard
            quality/speed trade-off; 1 is greedy decode, ~2× faster but
            slightly worse on noisy audio.

    Returns:
        On success: the transcript text (single line, may be multi-
        sentence). Empty string when Whisper produced no segments
        (silence or noise).
        On failure: ``err: ...`` string.

    Detected language + duration + speed are logged but not returned to
    keep the surface area tight — call directly via Python for the
    full ``faster_whisper`` segment list if you need that.
    """
    # Resolve vault-relative paths against the vault root.
    p = Path(path)
    if not p.is_absolute():
        p = get_vault_root() / path
    if not p.exists():
        return f"err: audio file not found: {p}"
    if not is_audio_file(p.name):
        return (
            f"err: not a recognised audio file ({p.suffix}). "
            f"Supported: {sorted(_AUDIO_EXTS)}"
        )
    try:
        model = _load_model()
    except RuntimeError as e:
        return f"err: {e}"
    lang = language.strip().lower() or None
    t0 = time.monotonic()
    try:
        segments, info = model.transcribe(
            str(p),
            language=lang,
            beam_size=max(1, min(int(beam_size), 10)),
            vad_filter=True,  # skip silence — speeds things up + cleaner output
        )
        text_parts: list[str] = []
        for seg in segments:
            t = seg.text.strip()
            if t:
                text_parts.append(t)
    except Exception as e:
        log.exception("voice: transcription failed for %s", p)
        return f"err: transcription failed — {e}"
    elapsed = time.monotonic() - t0
    transcript = " ".join(text_parts).strip()
    log.info(
        "voice: transcribed %s in %.1fs (lang=%s, prob=%.2f, dur=%.1fs, chars=%d)",
        p.name, elapsed, info.language, info.language_probability,
        info.duration, len(transcript),
    )
    if not transcript:
        return ""  # silence / pure noise — caller decides what to do
    return transcript


def transcribe_audio_internal(
    abs_path: str,
    language: str = "",
) -> tuple[str, str]:
    """Internal call path used by bot.py — same as the MCP tool but
    returns (transcript, detected_lang) and never an error string.

    Errors bubble up as exceptions so the bot can log them and degrade
    gracefully (e.g. fall back to "voice message — couldn't transcribe").
    """
    model = _load_model()
    segments, info = model.transcribe(
        abs_path,
        language=(language.strip().lower() or None),
        beam_size=5,
        vad_filter=True,
    )
    parts = [s.text.strip() for s in segments if s.text.strip()]
    return " ".join(parts).strip(), info.language
