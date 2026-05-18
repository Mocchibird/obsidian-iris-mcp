"""Voice helpers — Phase 2.1 STT + Phase 2.2.0 TTS.

Phase 2.1: turn an audio file on disk into a transcript string via
``faster-whisper`` (used for Discord voice messages).

Phase 2.2.0: turn a string into a PCM WAV via ``piper-tts`` (used for
speaking Iris's replies into a Discord voice channel). Piper loads an
ONNX neural-vocoder model + a JSON config; we auto-fetch them from
HuggingFace on first use and cache to disk so subsequent restarts are
instant.

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
import re
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


# =============================================================================
# Phase 2.2.0 — Piper TTS for voice channel replies
# =============================================================================

# HuggingFace serves Piper voice models at predictable URLs. We auto-fetch on
# first use into a persistent cache dir so the (~60 MB) download only happens
# once per voice model.
_PIPER_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Voice slugs Piper publishes that we support out of the box. The slug encodes
# language + region + speaker + quality (low/medium/high). For Discord voice
# we default to medium quality — high is slower without obvious gain on a
# voice-channel speaker.
_PIPER_VOICE_PATHS = {
    # English, US
    "en_US-lessac-medium":    "en/en_US/lessac/medium/en_US-lessac-medium",
    "en_US-amy-medium":       "en/en_US/amy/medium/en_US-amy-medium",
    "en_US-ryan-medium":      "en/en_US/ryan/medium/en_US-ryan-medium",
    "en_US-libritts-high":    "en/en_US/libritts/high/en_US-libritts-high",
    # English, GB
    "en_GB-alan-medium":      "en/en_GB/alan/medium/en_GB-alan-medium",
    "en_GB-southern_english_female-low":
        "en/en_GB/southern_english_female/low/en_GB-southern_english_female-low",
    # German — useful given the user's CH/DE context
    "de_DE-thorsten-medium":  "de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    # Japanese
    "ja_JP-jcommon-medium":   "ja/ja_JP/jcommon/medium/ja_JP-jcommon-medium",
}

_piper_voice = None  # type: ignore[var-annotated]
_piper_voice_name = ""


def _piper_cache_dir() -> Path:
    """Where to store downloaded ONNX models. Lives next to the Whisper cache
    on the /claude-auth persistent volume so it survives container restarts.
    """
    base = Path(os.environ.get("IRIS_PIPER_CACHE_DIR", "/claude-auth/piper-voices"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ensure_piper_model(voice_name: str) -> tuple[Path, Path]:
    """Return (onnx_path, json_config_path) for the requested voice, fetching
    them from HuggingFace if not already cached. Raises on unknown voice or
    download failure.
    """
    if voice_name not in _PIPER_VOICE_PATHS:
        raise ValueError(
            f"unknown Piper voice {voice_name!r}; supported: "
            f"{sorted(_PIPER_VOICE_PATHS)}"
        )
    base_path = _PIPER_VOICE_PATHS[voice_name]
    cache = _piper_cache_dir()
    onnx_path = cache / f"{voice_name}.onnx"
    json_path = cache / f"{voice_name}.onnx.json"

    # Local import: httpx is already an Iris dep (pyproject.toml). Used here
    # instead of urllib.request so we get connection pooling + sensible defaults.
    if not onnx_path.exists() or not json_path.exists():
        import httpx  # noqa: PLC0415
        for suffix, dst in [(".onnx", onnx_path), (".onnx.json", json_path)]:
            url = f"{_PIPER_HF_BASE}/{base_path}{suffix}"
            log.info("piper: downloading %s → %s", url, dst)
            with httpx.stream(
                "GET", url,
                timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
                follow_redirects=True,
            ) as resp:
                resp.raise_for_status()
                tmp = dst.with_suffix(dst.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                        f.write(chunk)
                tmp.replace(dst)
    return onnx_path, json_path


def _load_piper() -> "object":
    """Lazy-load + module-cache the Piper voice. Same pattern as the
    Whisper loader: pay cold-start cost once per process, then reuse.
    """
    global _piper_voice, _piper_voice_name
    desired = (os.environ.get("IRIS_PIPER_VOICE") or "en_US-lessac-medium").strip()
    if _piper_voice is not None and _piper_voice_name == desired:
        return _piper_voice
    try:
        from piper import PiperVoice  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "piper-tts is not installed — rebuild the container with the "
            "updated pyproject.toml (Phase 2.2.0 added the dep)"
        ) from e
    onnx_path, _json_path = _ensure_piper_model(desired)
    t0 = time.monotonic()
    log.info("piper: loading voice %r from %s", desired, onnx_path)
    _piper_voice = PiperVoice.load(str(onnx_path))
    log.info("piper: voice loaded in %.1fs", time.monotonic() - t0)
    _piper_voice_name = desired
    return _piper_voice


def synthesize_to_wav(text: str, out_path: str) -> dict:
    """Synthesize ``text`` to a 16-bit mono WAV file at ``out_path``.

    Returns a dict ``{"sample_rate": int, "duration_sec": float,
    "byte_count": int}``. Raises on synthesis failure (caller handles).

    The output is the format Piper emits natively (typically 22050 Hz mono
    int16). The Discord voice-send pipeline pipes this through ffmpeg to
    upsample to Discord's 48 kHz stereo Opus.
    """
    import wave  # noqa: PLC0415

    voice = _load_piper()
    # Piper streams audio chunks per sentence — we concatenate them into
    # one WAV. The sample rate is fixed per voice model.
    sample_rate = voice.config.sample_rate
    total_bytes = bytearray()
    t0 = time.monotonic()
    for chunk in voice.synthesize(text):
        total_bytes.extend(chunk.audio_int16_bytes)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(total_bytes))
    duration_sec = len(total_bytes) / (2 * sample_rate)
    elapsed = time.monotonic() - t0
    log.info(
        "piper: synth %d chars → %s (%.1fs audio, %.1fs synth, %.1fx realtime)",
        len(text), Path(out_path).name, duration_sec, elapsed,
        (duration_sec / elapsed) if elapsed > 0 else 0,
    )
    return {
        "sample_rate": sample_rate,
        "duration_sec": duration_sec,
        "byte_count": len(total_bytes),
    }


@mcp.tool()
def synthesize_speech(
    text: str,
    out_path: str = "",
    voice: str = "",
) -> str:
    """Synthesize speech via Piper TTS and write a WAV to disk.

    Args:
        text: What to say.
        out_path: Vault-relative or absolute path for the output WAV.
            When empty, defaults to ``40_Attachments/Voice/<slug>.wav``.
        voice: Voice name (e.g. ``en_US-lessac-medium``). Empty = use
            ``IRIS_PIPER_VOICE`` env or fall back to lessac.

    Returns a status string with the output path + duration. Useful as
    an MCP tool for explicit "save this as audio" requests. The bot's
    voice-channel playback path uses ``synthesize_to_wav()`` directly.
    """
    text_clean = (text or "").strip()
    if not text_clean:
        return "err: text required"
    if voice.strip():
        os.environ["IRIS_PIPER_VOICE"] = voice.strip()
    # Default out path under the vault for archive + Obsidian browsability.
    if not out_path.strip():
        slug = re.sub(r"[^a-z0-9]+", "-", text_clean.lower())[:50].strip("-") or "speech"
        ts = time.strftime("%Y-%m-%d_%H%M%S")
        out_path = f"40_Attachments/Voice/{ts}_{slug}.wav"
    p = Path(out_path)
    if not p.is_absolute():
        p = get_vault_root() / out_path
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        meta = synthesize_to_wav(text_clean, str(p))
    except Exception as e:
        log.exception("piper: synthesis failed")
        return f"err: synthesis failed — {e}"
    rel = str(p.relative_to(get_vault_root())).replace("\\", "/") if p.is_relative_to(get_vault_root()) else str(p)
    return (
        f"ok synthesized · path: {rel} · "
        f"duration: {meta['duration_sec']:.1f}s · "
        f"sample_rate: {meta['sample_rate']}Hz"
    )
