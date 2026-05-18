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
    configured model name changes between calls (unlikely but defensive).

    Smart defaults: when IRIS_WHISPER_DEVICE=cuda is set but no
    IRIS_WHISPER_MODEL is specified, default to ``large-v3`` (best
    quality, multilingual, ~3 GB VRAM, ~5× realtime on a GTX 1080 Ti
    class GPU). On CPU we stay with ``base`` since large-v3 on CPU is
    impractically slow (~0.3× realtime).
    """
    global _model, _model_name
    device = os.environ.get("IRIS_WHISPER_DEVICE", "cpu").strip() or "cpu"
    if env_model := os.environ.get("IRIS_WHISPER_MODEL", "").strip():
        desired = env_model
    elif device == "cuda":
        desired = "large-v3"  # GPU can handle it
    else:
        desired = "base"  # sane CPU default
    if _model is not None and _model_name == desired:
        return _model
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed — rebuild the container with "
            "the updated pyproject.toml (Phase 2.1 added the dep)"
        ) from e
    # int8 is the right default on CPU (4× faster than float32, near-identical
    # accuracy for speech). float16 is the right default on CUDA.
    default_compute = "float16" if device == "cuda" else "int8"
    compute = os.environ.get("IRIS_WHISPER_COMPUTE", default_compute).strip()
    log.info(
        "voice: loading Whisper model %r on %s (compute=%s)",
        desired, device, compute,
    )
    t0 = time.monotonic()
    try:
        _model = WhisperModel(desired, device=device, compute_type=compute)
    except Exception as e:
        # CUDA load failures (no GPU visible, libs missing) fall back to CPU
        # rather than crashing the bot. Same pattern as the Piper loader.
        if device == "cuda":
            log.warning(
                "voice: Whisper CUDA load failed (%s) — falling back to CPU. "
                "Check onnxruntime-gpu install + GPU passthrough.", e,
            )
            # Pick a CPU-feasible model since large-v3 is too slow there.
            fallback_model = "base" if desired == "large-v3" else desired
            _model = WhisperModel(fallback_model, device="cpu", compute_type="int8")
            desired = fallback_model
        else:
            raise
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
# voice-channel speaker. Only language codes Piper ACTUALLY publishes are
# included; ja/ko are NOT in the rhasspy/piper-voices repo (would 404). For
# Japanese / Korean TTS see the system prompt — those need a different
# engine (Edge TTS / MeloTTS / Coqui XTTS) which isn't wired up here yet.
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
    # German (10 voices in repo; Thorsten is the canonical neutral one).
    "de_DE-thorsten-medium":  "de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    "de_DE-thorsten-high":    "de/de_DE/thorsten/high/de_DE-thorsten-high",
    "de_DE-kerstin-low":      "de/de_DE/kerstin/low/de_DE-kerstin-low",
    # Chinese (Mandarin). Useful if Hyun-Min ever practices zh.
    "zh_CN-huayan-medium":    "zh/zh_CN/huayan/medium/zh_CN-huayan-medium",
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


def _piper_use_cuda() -> bool:
    """True if IRIS_PIPER_USE_CUDA env says to use GPU. Requires:
      1. NVIDIA GPU + nvidia-container-toolkit set up on the host
      2. onnxruntime-gpu installed in the container (replaces onnxruntime).
         Add a build-layer like:
             RUN pip uninstall -y onnxruntime && pip install onnxruntime-gpu
         to your Dockerfile, OR set IRIS_PIPER_USE_CUDA=1 + a custom
         requirements file.
      3. `deploy.resources.reservations.devices` block in docker-compose
         exposing the GPU to the container.

    Honest note: Piper is already ~10× realtime on CPU, so GPU saves
    maybe 0.5s on a typical reply. The voice QUALITY won't change —
    same model, same architecture, just faster inference. The quality
    jump comes from switching engine (Kokoro / XTTS) rather than GPU-
    accelerating Piper.
    """
    return os.environ.get("IRIS_PIPER_USE_CUDA", "0").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


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
    use_cuda = _piper_use_cuda()
    t0 = time.monotonic()
    log.info(
        "piper: loading voice %r from %s (device=%s)",
        desired, onnx_path, "cuda" if use_cuda else "cpu",
    )
    try:
        _piper_voice = PiperVoice.load(str(onnx_path), use_cuda=use_cuda)
    except Exception as e:
        # If CUDA was requested but onnxruntime-gpu isn't installed (or no
        # GPU visible), fall back to CPU instead of crashing the bot.
        if use_cuda:
            log.warning(
                "piper: CUDA load failed (%s) — falling back to CPU. "
                "Check that onnxruntime-gpu is installed + GPU is passthrough'd.",
                e,
            )
            _piper_voice = PiperVoice.load(str(onnx_path), use_cuda=False)
        else:
            raise
    log.info("piper: voice loaded in %.1fs", time.monotonic() - t0)
    _piper_voice_name = desired
    return _piper_voice


# =============================================================================
# Kokoro TTS — 82M-param multilingual neural TTS, much better than Piper
# =============================================================================
# Single model handles English (US/GB), Japanese, Korean, Chinese, French,
# Spanish, Italian, Portuguese, Hindi. Apache 2.0. ~310 MB ONNX + ~25 MB
# voices.bin. Runs ~3× realtime CPU, ~30× realtime GPU. Quality is closer
# to ElevenLabs/OpenAI than to Piper.

_KOKORO_RELEASE_BASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
_KOKORO_MODEL_FILE = "kokoro-v1.0.onnx"
_KOKORO_VOICES_FILE = "voices-v1.0.bin"

# A representative subset of available voices. The actual model bundle ships
# many more (~50+). Names follow Kokoro's convention:
#   <a|b><f|m>_<name>  →  (American|British)(Female|Male)_<name>
#   j<f|m>_*           →  Japanese
#   z<f|m>_*           →  Mandarin
#   k<f|m>_*           →  Korean
# Set IRIS_KOKORO_VOICE to any name in voices-v1.0.bin (it accepts more
# than what's listed here — this is just the curated default set).
_KOKORO_VOICES_HINT = {
    # American English
    "af_sarah":   "American female (warm, conversational) — recommended default",
    "af_bella":   "American female (clear)",
    "am_michael": "American male (mid)",
    "am_adam":    "American male (deeper)",
    # British English
    "bf_emma":    "British female",
    "bm_george":  "British male",
    # Japanese
    "jf_alpha":   "Japanese female",
    "jm_kumo":    "Japanese male",
    # Korean
    "kf_001":     "Korean female",
    "km_001":     "Korean male",
    # Mandarin
    "zf_xiaoxiao": "Mandarin female",
    "zm_yunjian": "Mandarin male",
}

# Language hint codes accepted by Kokoro.create(lang=...). Auto-detected per-
# voice but explicit is faster (skips the phonemizer probe).
_KOKORO_LANG_BY_PREFIX = {
    "a": "en-us", "b": "en-gb", "j": "ja", "k": "ko",
    "z": "zh", "f": "fr-fr", "e": "es", "i": "it", "p": "pt-br", "h": "hi",
}

_kokoro_inst = None  # type: ignore[var-annotated]


def _kokoro_use_cuda() -> bool:
    """Same flag pattern as the Piper version — opt-in via env."""
    return os.environ.get("IRIS_KOKORO_USE_CUDA", "0").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def _ensure_kokoro_models() -> tuple[Path, Path]:
    """Fetch the Kokoro ONNX model + voices.bin into the persistent cache
    if not already present. Returns (model_path, voices_path)."""
    cache = _piper_cache_dir().parent / "kokoro"
    cache.mkdir(parents=True, exist_ok=True)
    model_path = cache / _KOKORO_MODEL_FILE
    voices_path = cache / _KOKORO_VOICES_FILE
    if model_path.exists() and voices_path.exists():
        return model_path, voices_path
    import httpx  # noqa: PLC0415
    for fname, dst in [(_KOKORO_MODEL_FILE, model_path),
                       (_KOKORO_VOICES_FILE, voices_path)]:
        url = f"{_KOKORO_RELEASE_BASE}/{fname}"
        log.info("kokoro: downloading %s → %s", url, dst)
        with httpx.stream(
            "GET", url,
            timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10),
            follow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            tmp = dst.with_suffix(dst.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
            tmp.replace(dst)
    return model_path, voices_path


def _load_kokoro():
    """Lazy-load + module-cache the Kokoro model. Reads provider preference
    from IRIS_KOKORO_USE_CUDA — Kokoro doesn't accept a ``providers`` kwarg,
    it reads the ``ONNX_PROVIDER`` env var at construction time and uses
    that. We set it explicitly here so the choice is deterministic
    regardless of what onnxruntime-gpu auto-detects.
    """
    global _kokoro_inst
    if _kokoro_inst is not None:
        return _kokoro_inst
    try:
        from kokoro_onnx import Kokoro  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "kokoro-onnx is not installed — rebuild the container with the "
            "updated pyproject.toml"
        ) from e
    model_path, voices_path = _ensure_kokoro_models()
    use_cuda = _kokoro_use_cuda()
    # Force the desired provider via the env var Kokoro reads on init.
    prev_provider = os.environ.get("ONNX_PROVIDER")
    os.environ["ONNX_PROVIDER"] = (
        "CUDAExecutionProvider" if use_cuda else "CPUExecutionProvider"
    )
    log.info(
        "kokoro: loading model from %s (provider=%s)",
        model_path.name, os.environ["ONNX_PROVIDER"],
    )
    t0 = time.monotonic()
    try:
        _kokoro_inst = Kokoro(str(model_path), str(voices_path))
    except Exception as e:
        if use_cuda:
            log.warning(
                "kokoro: CUDA init failed (%s) — falling back to CPU. "
                "Check onnxruntime-gpu is installed + GPU passthrough.", e,
            )
            os.environ["ONNX_PROVIDER"] = "CPUExecutionProvider"
            _kokoro_inst = Kokoro(str(model_path), str(voices_path))
        else:
            # Restore env before re-raising so we don't poison future calls.
            if prev_provider is None:
                os.environ.pop("ONNX_PROVIDER", None)
            else:
                os.environ["ONNX_PROVIDER"] = prev_provider
            raise
    log.info("kokoro: model loaded in %.1fs", time.monotonic() - t0)
    return _kokoro_inst


def _kokoro_lang_for_voice(voice: str) -> str:
    """Pick a sensible `lang=` hint from a Kokoro voice name's prefix."""
    if not voice:
        return "en-us"
    prefix = voice[:1].lower()
    return _KOKORO_LANG_BY_PREFIX.get(prefix, "en-us")


# pykakasi instance for kanji → hiragana preprocessing. Lazy-loaded; cached
# at module level so we don't pay the dictionary-load cost per synth call.
_kakasi = None


def _kanji_to_kana(text: str) -> str:
    """Convert any kanji in Japanese text to hiragana. Hiragana, katakana,
    punctuation, and non-Japanese chars pass through unchanged.

    Why: Kokoro's Japanese voice uses espeak for phonemization, which has
    NO kanji → reading lookup table. When espeak hits an unknown CJK char
    it falls back to outputting "Chinese letter" or the Unicode description
    as English words, which the voice then reads literally. Pre-converting
    kanji to hiragana sidesteps this entirely — espeak handles kana fine.

    Note: pykakasi picks one reading per kanji from its dictionary. For
    homographs (e.g. 一日 → "ついたち" first-of-the-month vs "いちにち" one-day)
    it's not always context-aware. Way better than the espeak fallback
    though — you get correct Japanese audio instead of "japanese letter
    japanese letter chinese letter chinese letter".
    """
    global _kakasi
    if not text:
        return text
    try:
        if _kakasi is None:
            import pykakasi  # type: ignore
            _kakasi = pykakasi.kakasi()
        result = _kakasi.convert(text)
        return "".join(item["hira"] for item in result)
    except Exception:
        log.exception("kanji→kana preprocessing failed — sending raw text to Kokoro")
        return text


def _synthesize_kokoro(text: str, out_path: str) -> dict:
    """Kokoro synth path — produces a WAV via soundfile.

    For Japanese (``lang=ja``) we run text through pykakasi first to
    convert any kanji to hiragana. Without this, Kokoro reads kanji as
    literal "chinese letter" English words (espeak has no kanji→yomi
    lookup). Pre-processing once at the input is way cheaper than
    fixing the phonemizer downstream.
    """
    import soundfile as sf  # noqa: PLC0415

    voice_name = (os.environ.get("IRIS_KOKORO_VOICE") or "af_sarah").strip()
    speed = float(os.environ.get("IRIS_KOKORO_SPEED", "1.0") or "1.0")
    lang = (os.environ.get("IRIS_KOKORO_LANG") or _kokoro_lang_for_voice(voice_name)).strip()

    # Japanese-specific preprocessing — kanji to hiragana.
    synth_text = text
    if lang == "ja":
        synth_text = _kanji_to_kana(text)
        if synth_text != text:
            log.info(
                "kokoro: japanese preprocess %d→%d chars (kanji→hiragana)",
                len(text), len(synth_text),
            )

    kokoro = _load_kokoro()
    t0 = time.monotonic()
    samples, sample_rate = kokoro.create(synth_text, voice=voice_name, speed=speed, lang=lang)
    sf.write(out_path, samples, sample_rate)
    elapsed = time.monotonic() - t0
    duration_sec = len(samples) / sample_rate if sample_rate else 0
    log.info(
        "kokoro: synth %d chars · voice=%s lang=%s → %s "
        "(%.1fs audio, %.1fs synth, %.1fx realtime)",
        len(synth_text), voice_name, lang, Path(out_path).name,
        duration_sec, elapsed,
        (duration_sec / elapsed) if elapsed > 0 else 0,
    )
    return {
        "sample_rate": int(sample_rate),
        "duration_sec": float(duration_sec),
        "byte_count": int(len(samples) * 2),  # int16
        "engine": "kokoro",
        "voice": voice_name,
        "lang": lang,
    }


def _synthesize_piper(text: str, out_path: str) -> dict:
    """Piper synth path — original implementation, kept as a stable fallback."""
    import wave  # noqa: PLC0415

    voice = _load_piper()
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
        "engine": "piper",
        "voice": _piper_voice_name,
    }


# =============================================================================
# Multilingual auto-routing — IRIS_TTS_ENGINE=auto
# =============================================================================
# Detects Iris's reply language and picks the right engine+voice combo. The
# detection is restricted to three languages (EN/KO/JA — all served by
# Kokoro). German was previously routed to Piper, but the engine-switch
# mid-sentence + Piper's lower quality made it not worth it; sentences in
# German now get the English voice (acceptable: rare in the user's actual
# Discord usage). Short snippets (<10 chars) also force English to avoid
# false positives on "Ah!" / "Ja!" / "Si!" type interjections.
#
# Default voice mapping (override per-language via env):
#   en → kokoro:af_sarah          (warm American female)
#   ko → kokoro:kf_001
#   ja → kokoro:jf_alpha
#
# Override examples in .env:
#   IRIS_TTS_VOICE_EN=kokoro:af_bella
#   IRIS_TTS_VOICE_KO=kokoro:km_001
#   IRIS_TTS_VOICE_JA=kokoro:jm_kumo

_DEFAULT_VOICE_BY_LANG = {
    "en": "kokoro:af_sarah",
    "ko": "kokoro:kf_001",
    "ja": "kokoro:jf_alpha",
}

# Below this many characters, language detection is unreliable — force
# English (the lingua-franca fallback) so e.g. "Ah!" / "Ja!" / "Si!" don't
# get routed to the wrong voice. Empirically 10 chars catches most short
# interjections without losing useful Japanese/Korean sentences (those
# rarely fit in fewer than ~5 chars including the actual meaningful
# content + the script's character density).
_LANG_DETECT_MIN_CHARS = 10

_lang_detector = None  # lazy-init


def _detect_language(text: str) -> str:
    """Detect EN/KO/JA from text. Falls back to 'en' on errors,
    indeterminate input (very short / pure punctuation), or text below
    ``_LANG_DETECT_MIN_CHARS``. Restricted to three candidate languages so
    detection is fast + accurate on short Discord-reply-length input.

    German was previously a candidate but dropped — it routed to Piper
    (lower quality) and engine-switching mid-sentence sounded worse than
    just speaking German text with the English voice. Iris can still
    *understand and reply in* German; only the TTS layer ignores it.
    """
    global _lang_detector
    if not text or len(text.strip()) < _LANG_DETECT_MIN_CHARS:
        return "en"
    try:
        if _lang_detector is None:
            from lingua import Language, LanguageDetectorBuilder  # type: ignore
            _lang_detector = LanguageDetectorBuilder.from_languages(
                Language.ENGLISH, Language.KOREAN, Language.JAPANESE,
            ).build()
        from lingua import Language  # type: ignore
        detected = _lang_detector.detect_language_of(text)
        if detected is None:
            return "en"
        return {
            Language.ENGLISH: "en",
            Language.KOREAN: "ko",
            Language.JAPANESE: "ja",
        }.get(detected, "en")
    except Exception:
        log.exception("language detection failed — defaulting to 'en'")
        return "en"


def _resolve_voice_spec(lang: str) -> tuple[str, str]:
    """Return (engine, voice) for a detected language. Reads
    IRIS_TTS_VOICE_<LANG> from env first, falls back to the default map.

    Spec format: ``engine:voice_slug`` (e.g. ``kokoro:af_sarah``,
    ``piper:de_DE-thorsten-medium``). Bare voice name (no colon) is
    treated as Kokoro by default.
    """
    env_key = f"IRIS_TTS_VOICE_{lang.upper()}"
    spec = (os.environ.get(env_key) or _DEFAULT_VOICE_BY_LANG.get(lang)
            or "kokoro:af_sarah").strip()
    if ":" in spec:
        engine, voice = spec.split(":", 1)
    else:
        engine, voice = "kokoro", spec
    return engine.strip().lower(), voice.strip()


# Sentence splitter — handles English + CJK punctuation. The split keeps the
# delimiter on the preceding sentence so prosody isn't chopped mid-pause.
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[.!?。！？])\s+|"        # English / Japanese / Chinese end-of-sentence
    r"(?<=[\n])\s*",                # Line breaks (markdown lists, "Japanese: …\nKorean: …")
)


def _segment_text_by_language(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into runs of (language, segment_text) tuples. Adjacent
    same-language sentences are merged so the synth gets natural prosody
    rather than choppy sentence-by-sentence audio.

    A single-language input returns a single tuple — no segmentation
    overhead. This is the common case (Iris replies in one language).
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        return []
    if len(sentences) == 1:
        return [(_detect_language(sentences[0]), sentences[0])]
    runs: list[tuple[str, str]] = []
    for sent in sentences:
        lang = _detect_language(sent)
        if runs and runs[-1][0] == lang:
            runs[-1] = (lang, runs[-1][1] + " " + sent)
        else:
            runs.append((lang, sent))
    return runs


def _concat_wavs_via_ffmpeg(input_paths: list[str], out_path: str) -> None:
    """Concatenate WAV files into one via ffmpeg's concat filter. Handles
    sample-rate mismatches automatically (Piper outputs 22050 Hz, Kokoro
    outputs 24000 Hz — both upsampled to 48 kHz mono int16 for Discord).
    """
    import subprocess  # noqa: PLC0415

    if len(input_paths) == 1:
        # No concat needed — just copy/rename.
        import shutil  # noqa: PLC0415
        shutil.copy(input_paths[0], out_path)
        return
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in input_paths:
        cmd.extend(["-i", p])
    inputs = "".join(f"[{i}:a]" for i in range(len(input_paths)))
    cmd.extend([
        "-filter_complex", f"{inputs}concat=n={len(input_paths)}:v=0:a=1[out]",
        "-map", "[out]",
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
        out_path,
    ])
    subprocess.run(cmd, check=True)


def _synthesize_segment(text: str, lang: str, out_path: str) -> dict:
    """Synthesize one same-language segment using the right engine + voice."""
    chosen_engine, voice = _resolve_voice_spec(lang)
    env_key = "IRIS_KOKORO_VOICE" if chosen_engine == "kokoro" else "IRIS_PIPER_VOICE"
    prev = os.environ.get(env_key)
    os.environ[env_key] = voice
    try:
        if chosen_engine == "kokoro":
            try:
                return _synthesize_kokoro(text, out_path)
            except Exception:
                log.exception("kokoro segment synth failed — falling back to Piper")
                return _synthesize_piper(text, out_path)
        return _synthesize_piper(text, out_path)
    finally:
        if prev is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = prev


def synthesize_to_wav(text: str, out_path: str) -> dict:
    """Synthesize ``text`` to a 16-bit mono WAV at ``out_path``.

    Engine selection (via ``IRIS_TTS_ENGINE``):
        ``piper``  — always use Piper. Lightweight, robotic-ish, ~10× realtime.
        ``kokoro`` — always use Kokoro. 82M-param neural, much better, ~3×
                     realtime CPU / ~30× realtime GPU. Multilingual EN/JA/
                     KO/ZH/+ but NOT German.
        ``auto``   — detect language per sentence, route per
                     ``IRIS_TTS_VOICE_<LANG>`` env (defaults: EN/JA/KO →
                     Kokoro, DE → Piper). Multi-language replies are
                     split + synthesized per segment + concatenated via
                     ffmpeg so a "Japanese: …\\nKorean: …" reply doesn't
                     get spoken in one English voice.

    The Discord voice-send pipeline pipes the WAV through ffmpeg to upsample
    to Discord's 48 kHz stereo Opus, so per-engine sample rates differ but
    the playback is consistent.
    """
    engine = (os.environ.get("IRIS_TTS_ENGINE") or "piper").strip().lower()
    if engine == "auto":
        segments = _segment_text_by_language(text)
        if not segments:
            raise ValueError("empty text for synthesis")
        if len(segments) == 1:
            # Fast path — single language, no concat.
            lang, seg_text = segments[0]
            chosen_engine, voice = _resolve_voice_spec(lang)
            log.info(
                "tts auto-route: lang=%s engine=%s voice=%s",
                lang, chosen_engine, voice,
            )
            meta = _synthesize_segment(seg_text, lang, out_path)
            meta["detected_lang"] = lang
            return meta
        # Multi-language path: synth each segment to its own WAV, then concat.
        log.info(
            "tts auto-route (multilingual): %d segments — %s",
            len(segments),
            ", ".join(f"{lang}({len(t)}c)" for lang, t in segments),
        )
        tmp_paths: list[str] = []
        try:
            for i, (lang, seg_text) in enumerate(segments):
                tmp = f"{out_path}.seg{i}.wav"
                _synthesize_segment(seg_text, lang, tmp)
                tmp_paths.append(tmp)
            _concat_wavs_via_ffmpeg(tmp_paths, out_path)
            return {
                "sample_rate": 48000,  # ffmpeg upsampled to 48 kHz for concat
                "duration_sec": 0.0,   # not computed for concat; caller doesn't use it
                "byte_count": 0,
                "engine": "auto (multi-segment)",
                "voice": "per-segment",
                "lang": "mixed",
                "segments": [{"lang": l, "len": len(t)} for l, t in segments],
            }
        finally:
            for p in tmp_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    if engine == "kokoro":
        try:
            return _synthesize_kokoro(text, out_path)
        except Exception:
            log.exception("kokoro synth failed — falling back to Piper")
    return _synthesize_piper(text, out_path)


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
