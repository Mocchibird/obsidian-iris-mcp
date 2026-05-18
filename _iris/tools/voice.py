"""Voice helpers — Whisper STT + Edge TTS.

Two concerns, sharply separated:

1. **STT** (Discord voice messages → transcript text) via
   ``faster-whisper`` running locally on CPU. The Discord bot calls
   ``transcribe_audio_internal`` on every .ogg attachment. Voice
   messages are auto-deleted after a successful transcription — the
   transcript is the durable record.

2. **TTS** (Iris's text reply → spoken audio in a voice channel) via
   Microsoft Edge TTS (Azure Neural voices, free, no API key). The
   bot streams Edge's MP3 output directly through ffmpeg → Discord
   so playback starts ~300 ms after the request.

Multi-language replies (e.g. "English: foo. 日本語: bar.") get
segmented by language and each segment is synthesized with the
matching Edge voice. The segments are concatenated via ffmpeg before
playback (for the buffered path) or piped sequentially (for the
streaming path implemented in ``docker/bot.py``).

Configuration (env vars):
    IRIS_WHISPER_MODEL    default 'base' (74M params, multilingual)
    IRIS_WHISPER_DEVICE   default 'cpu' (faster-whisper supports CUDA
                          via CTranslate2 if you wire it up yourself)
    IRIS_WHISPER_COMPUTE  default 'int8' on cpu, 'float16' on cuda
    IRIS_EDGE_VOICE       default 'en-US-AvaNeural' (used when no
                          per-language override applies)
    IRIS_EDGE_RATE        default '+0%'  (e.g. '+10%', '-15%')
    IRIS_EDGE_PITCH       default '+0Hz' (e.g. '+50Hz', '-30Hz')
    IRIS_TTS_VOICE_EN     default 'edge:en-US-AvaNeural'
    IRIS_TTS_VOICE_JA     default 'edge:ja-JP-NanamiNeural'
    IRIS_TTS_VOICE_KO     default 'edge:ko-KR-SunHiNeural'
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

from .. import mcp
from ..core import get_vault_root

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STT — Whisper (voice messages)
# ─────────────────────────────────────────────────────────────────────────────

# Audio extensions Whisper + ffmpeg can decode. Discord voice messages are
# .ogg/Opus; manual uploads might be .mp3/.wav/.m4a/.flac/.webm.
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".webm", ".aac"}

# Module-level model cache. faster-whisper's WhisperModel is thread-safe for
# `transcribe()` per the docs, so one instance shared across all turns is fine.
_whisper_model = None
_whisper_model_name = ""


def is_audio_file(filename: str) -> bool:
    """True if the filename's extension is one we can transcribe."""
    return Path(filename).suffix.lower() in _AUDIO_EXTS


def _load_whisper():
    """Lazy-load + module-cache the Whisper model."""
    global _whisper_model, _whisper_model_name
    device = os.environ.get("IRIS_WHISPER_DEVICE", "cpu").strip() or "cpu"
    desired = (
        os.environ.get("IRIS_WHISPER_MODEL", "").strip()
        or ("large-v3" if device == "cuda" else "base")
    )
    if _whisper_model is not None and _whisper_model_name == desired:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed — rebuild the container with "
            "the updated pyproject.toml"
        ) from e
    default_compute = "float16" if device == "cuda" else "int8"
    compute = os.environ.get("IRIS_WHISPER_COMPUTE", default_compute).strip()
    log.info(
        "whisper: loading model %r on %s (compute=%s)",
        desired, device, compute,
    )
    t0 = time.monotonic()
    try:
        _whisper_model = WhisperModel(desired, device=device, compute_type=compute)
    except Exception as e:
        if device == "cuda":
            log.warning(
                "whisper: CUDA load failed (%s) — falling back to CPU. "
                "Check CTranslate2 CUDA libs + GPU passthrough.", e,
            )
            fallback_model = "base" if desired == "large-v3" else desired
            _whisper_model = WhisperModel(
                fallback_model, device="cpu", compute_type="int8",
            )
            desired = fallback_model
        else:
            raise
    log.info("whisper: model loaded in %.1fs", time.monotonic() - t0)
    _whisper_model_name = desired
    return _whisper_model


@mcp.tool()
def transcribe_audio(
    path: str,
    language: str = "",
    beam_size: int = 5,
) -> str:
    """Transcribe an audio file at ``path`` to text via Whisper.

    Args:
        path: Vault-relative or absolute path. Discord voice messages land
            at ``90_Inbox/inbox/<timestamp>_<name>.ogg`` via the normal
            attachment-save flow.
        language: ISO 639-1 hint ('en', 'de', 'ko', 'ja', …). Empty (the
            default) lets Whisper auto-detect — works well for clean audio
            but slower. Pass a hint when you know the language.
        beam_size: Beam-search width. 5 (default) is the standard
            quality/speed trade-off; 1 is greedy decode.

    Returns:
        On success: the transcript text. Empty string for silence/noise.
        On failure: ``err: ...`` string.
    """
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
        model = _load_whisper()
    except RuntimeError as e:
        return f"err: {e}"
    lang = language.strip().lower() or None
    t0 = time.monotonic()
    try:
        segments, info = model.transcribe(
            str(p),
            language=lang,
            beam_size=max(1, min(int(beam_size), 10)),
            vad_filter=True,
        )
        parts = [s.text.strip() for s in segments if s.text.strip()]
    except Exception as e:
        log.exception("whisper: transcription failed for %s", p)
        return f"err: transcription failed — {e}"
    elapsed = time.monotonic() - t0
    transcript = " ".join(parts).strip()
    log.info(
        "whisper: transcribed %s in %.1fs (lang=%s, prob=%.2f, dur=%.1fs, chars=%d)",
        p.name, elapsed, info.language, info.language_probability,
        info.duration, len(transcript),
    )
    return transcript  # empty string on silence is intentional


def transcribe_audio_internal(
    abs_path: str,
    language: str = "",
) -> tuple[str, str]:
    """Internal call path used by bot.py — returns (transcript, lang).
    Raises on failure so the caller can degrade gracefully.
    """
    model = _load_whisper()
    segments, info = model.transcribe(
        abs_path,
        language=(language.strip().lower() or None),
        beam_size=5,
        vad_filter=True,
    )
    parts = [s.text.strip() for s in segments if s.text.strip()]
    return " ".join(parts).strip(), info.language


# ─────────────────────────────────────────────────────────────────────────────
# TTS — Edge (text → spoken audio in voice channel)
# ─────────────────────────────────────────────────────────────────────────────


def _synthesize_edge(text: str, out_path: str) -> dict:
    """Synthesize speech via Microsoft Edge TTS to a WAV at ``out_path``.

    Saves as MP3 internally (Edge's native output) then converts to mono
    16-bit WAV via ffmpeg so the playback pipeline doesn't need to care
    about the encoding.

    Voice name from IRIS_EDGE_VOICE env (default: en-US-AvaNeural).
    Browse the catalogue at:
      https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support
    """
    import asyncio  # noqa: PLC0415
    try:
        import edge_tts  # type: ignore  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError(
            "edge-tts is not installed — rebuild the container with the "
            "updated pyproject.toml"
        ) from e

    voice = (os.environ.get("IRIS_EDGE_VOICE") or "en-US-AvaNeural").strip()
    rate = (os.environ.get("IRIS_EDGE_RATE") or "+0%").strip()
    pitch = (os.environ.get("IRIS_EDGE_PITCH") or "+0Hz").strip()

    mp3_path = out_path + ".edge.mp3"
    t0 = time.monotonic()

    async def _save() -> None:
        c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        await c.save(mp3_path)

    try:
        asyncio.run(_save())
    except Exception as e:
        log.warning("edge-tts request failed: %s", e)
        raise

    # MP3 → mono 16-bit WAV (24 kHz; ffmpeg upsamples to 48 kHz later for Discord)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", mp3_path,
            "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
            out_path,
        ],
        check=True,
    )
    try:
        os.unlink(mp3_path)
    except OSError:
        pass

    import wave  # noqa: PLC0415
    with wave.open(out_path, "rb") as wf:
        sample_rate = wf.getframerate()
        frames = wf.getnframes()
        duration = frames / float(sample_rate)
    elapsed = time.monotonic() - t0
    log.info(
        "edge-tts: synth %d chars · voice=%s → %s "
        "(%.1fs audio, %.1fs round-trip, %.1fx realtime)",
        len(text), voice, Path(out_path).name,
        duration, elapsed,
        (duration / elapsed) if elapsed > 0 else 0,
    )
    return {
        "sample_rate": sample_rate,
        "duration_sec": duration,
        "byte_count": int(frames * 2),
        "engine": "edge",
        "voice": voice,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Language detection + per-language voice routing
# ─────────────────────────────────────────────────────────────────────────────

# Default voice per language (override via IRIS_TTS_VOICE_<LANG> env).
# All on Edge — consistent quality, no GPU needed, sub-second latency
# with streaming playback (see _speak_via_edge_streaming in bot.py).
_DEFAULT_VOICE_BY_LANG = {
    "en": "edge:en-US-AvaNeural",
    "ko": "edge:ko-KR-SunHiNeural",
    "ja": "edge:ja-JP-NanamiNeural",
}

# Latin-only text below this many chars is detection-unreliable — force
# English. CJK script is always trusted via the script-based fast path.
_LANG_DETECT_MIN_CHARS = 10

# Script-detection regexes. Hangul / kana presence is unambiguous; kanji
# alone is ambiguous (could be Japanese or Chinese) so we leave that to
# the statistical detector.
_HANGUL_RE = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")
_HIRAGANA_KATAKANA_RE = re.compile(r"[぀-ゟ゠-ヿｦ-ﾟ]")

_lang_detector = None  # lazy-init


def _script_based_detect(text: str) -> str | None:
    """Unambiguous language from script alone, or None when ambiguous."""
    if _HANGUL_RE.search(text):
        return "ko"
    if _HIRAGANA_KATAKANA_RE.search(text):
        return "ja"
    return None


def _detect_language(text: str) -> str:
    """Detect EN / KO / JA. Two-stage:
      1. Script-based: hangul → ko, kana → ja (no min-length needed).
      2. Statistical via lingua, guarded by _LANG_DETECT_MIN_CHARS for
         Latin-only text to avoid misrouting 3-char interjections like "Ah!".
    """
    if not text or not text.strip():
        return "en"
    script_guess = _script_based_detect(text)
    if script_guess is not None:
        return script_guess
    if len(text.strip()) < _LANG_DETECT_MIN_CHARS:
        return "en"
    try:
        global _lang_detector
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
    """Return (engine, voice) for a detected language. Edge is the only
    supported engine in this revision, but the function returns the engine
    tag so the existing bot dispatch (which switches on engine name) keeps
    working unchanged.

    Spec format: ``engine:voice_slug`` (e.g. ``edge:en-US-AvaNeural``).
    Bare voice name is treated as Edge.
    """
    env_key = f"IRIS_TTS_VOICE_{lang.upper()}"
    spec = (
        os.environ.get(env_key)
        or _DEFAULT_VOICE_BY_LANG.get(lang)
        or "edge:en-US-AvaNeural"
    ).strip()
    if ":" in spec:
        engine, voice = spec.split(":", 1)
    else:
        engine, voice = "edge", spec
    return engine.strip().lower(), voice.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Multi-language reply segmentation
# ─────────────────────────────────────────────────────────────────────────────

_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[.!?。！？])\s+|"
    r"(?<=[\n])\s*",
)


def _segment_text_by_language(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into runs of (language, segment_text). Adjacent
    same-language sentences are merged for natural prosody.
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
    """Concatenate WAV files via ffmpeg's concat filter — handles sample-
    rate differences automatically and emits a single 48 kHz mono WAV."""
    if len(input_paths) == 1:
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
    """Synthesize one same-language segment with the right Edge voice.
    Pushes the resolved voice into IRIS_EDGE_VOICE for this call so the
    underlying _synthesize_edge picks it up. Env restored in finally.
    """
    _engine, voice = _resolve_voice_spec(lang)
    prev = os.environ.get("IRIS_EDGE_VOICE")
    os.environ["IRIS_EDGE_VOICE"] = voice
    try:
        return _synthesize_edge(text, out_path)
    finally:
        if prev is None:
            os.environ.pop("IRIS_EDGE_VOICE", None)
        else:
            os.environ["IRIS_EDGE_VOICE"] = prev


# ─────────────────────────────────────────────────────────────────────────────
# Top-level TTS entrypoint
# ─────────────────────────────────────────────────────────────────────────────


def synthesize_to_wav(text: str, out_path: str) -> dict:
    """Synthesize ``text`` to a 16-bit mono WAV at ``out_path``.

    Behaviour:
      - Single-language reply → one Edge call, fast path.
      - Multi-language reply → segment per language, synthesize each
        with the matching Edge voice, concatenate via ffmpeg.

    The Discord voice-send pipeline pipes the WAV through ffmpeg to
    upsample to Discord's 48 kHz stereo Opus, so per-segment sample
    rates don't matter for the final playback.
    """
    segments = _segment_text_by_language(text)
    if not segments:
        raise ValueError("empty text for synthesis")
    if len(segments) == 1:
        lang, seg_text = segments[0]
        _engine, voice = _resolve_voice_spec(lang)
        log.info("tts: lang=%s voice=%s", lang, voice)
        meta = _synthesize_segment(seg_text, lang, out_path)
        meta["detected_lang"] = lang
        return meta
    log.info(
        "tts (multilingual): %d segments — %s",
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
            "sample_rate": 48000,
            "duration_sec": 0.0,  # not computed for concat; caller doesn't use it
            "byte_count": 0,
            "engine": "edge (multi-segment)",
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


@mcp.tool()
def synthesize_speech(
    text: str,
    out_path: str = "",
    voice: str = "",
) -> str:
    """Synthesize speech via Edge TTS and write a WAV to disk.

    Args:
        text: What to say.
        out_path: Vault-relative or absolute path for the output WAV.
            When empty, defaults to ``40_Attachments/Voice/<slug>.wav``.
        voice: Edge voice name (e.g. ``en-US-AvaNeural``). Empty = use
            ``IRIS_EDGE_VOICE`` env or fall back to ``en-US-AvaNeural``.

    Useful as an explicit "save this as audio" MCP tool, e.g. for
    archiving snippets or testing voices. The bot's voice channel
    playback uses ``synthesize_to_wav`` (or the streaming path in
    bot.py) directly.
    """
    text_clean = (text or "").strip()
    if not text_clean:
        return "err: text required"
    if voice.strip():
        os.environ["IRIS_EDGE_VOICE"] = voice.strip()
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
        log.exception("tts: synthesis failed")
        return f"err: synthesis failed — {e}"
    rel = (
        str(p.relative_to(get_vault_root())).replace("\\", "/")
        if p.is_relative_to(get_vault_root()) else str(p)
    )
    return (
        f"ok synthesized · path: {rel} · "
        f"duration: {meta.get('duration_sec', 0):.1f}s · "
        f"sample_rate: {meta.get('sample_rate', 0)}Hz · "
        f"engine: {meta.get('engine', 'edge')}"
    )
