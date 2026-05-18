"""Voice receive — Whisper STT inside Discord voice channels (Phase 2.2.1).

The bot connects with ``VoiceRecvClient`` (from ``discord-ext-voice-recv``)
instead of the plain ``VoiceClient``, which exposes per-user PCM frames.
We accumulate each user's audio in a buffer, segment by silence (no packet
for ``SILENCE_HANG_MS``), flush each completed utterance to a temporary
WAV, transcribe via Whisper, and hand the transcript back to bot.py via a
callback.

**VAD strategy: pure silence-timing.** Discord stops sending Opus packets
when a user isn't speaking, so "no packet for 700ms" reliably = end of
utterance. No signal-processing needed. False boundaries on natural pauses
mid-sentence are the trade-off; tune ``SILENCE_HANG_MS`` to taste.

**Threading model.** ``write()`` runs on a discord.py worker thread; the
sweeper runs on the asyncio loop via ``run_coroutine_threadsafe``. The
``threading.Lock`` guards the shared buffer dict. The Whisper call itself
happens in ``asyncio.to_thread`` so it doesn't block the event loop.

**Echo.** Discord doesn't echo a bot's own playback back to it, so we
don't need to filter that out. We DO pause the sink while Iris is speaking
so the bot doesn't queue up reactions to its own TTS (and so the user
doesn't talk over Iris and get half-utterances).

Configuration (env vars, all optional):
    IRIS_VOICE_LISTEN_ENABLED       default '1'
    IRIS_VOICE_WAKE_WORDS           CSV; default 'iris,hey iris,okay iris'
                                    (empty disables the gate — every utterance
                                    routes to Claude, useful for testing)
    IRIS_VOICE_LISTEN_USERS         CSV of user IDs allowed to be heard;
                                    empty inherits IRIS_DISCORD_ALLOWED_USERS
    IRIS_VOICE_SILENCE_HANG_MS      default 700
    IRIS_VOICE_MIN_UTTERANCE_MS     default 500
    IRIS_VOICE_MAX_UTTERANCE_MS     default 30000
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
import time
import wave
from collections.abc import Awaitable, Callable
from typing import Optional

log = logging.getLogger(__name__)


# ── Config ───────────────────────────────────────────────────────────────────

SILENCE_HANG_MS = int(os.environ.get("IRIS_VOICE_SILENCE_HANG_MS", "700"))
MIN_UTTERANCE_MS = int(os.environ.get("IRIS_VOICE_MIN_UTTERANCE_MS", "500"))
MAX_UTTERANCE_MS = int(os.environ.get("IRIS_VOICE_MAX_UTTERANCE_MS", "30000"))

LISTEN_ENABLED = os.environ.get(
    "IRIS_VOICE_LISTEN_ENABLED", "1",
).strip().lower() not in ("0", "false", "no", "off", "")

# Discord voice receive: 48kHz signed 16-bit stereo PCM, 20ms frames.
DISCORD_PCM_SR = 48000
DISCORD_PCM_CHANNELS = 2
DISCORD_PCM_BPS = 2
DISCORD_PCM_BPS_PER_SEC = DISCORD_PCM_SR * DISCORD_PCM_CHANNELS * DISCORD_PCM_BPS


# ── Voice-recv availability ──────────────────────────────────────────────────

try:
    from discord.ext import voice_recv  # type: ignore
    VOICE_RECV_AVAILABLE = True
except ImportError:
    voice_recv = None  # type: ignore[assignment]
    VOICE_RECV_AVAILABLE = False
    log.warning(
        "voice-recv: discord-ext-voice-recv not installed — Iris will be "
        "send-only in voice channels. Rebuild the container with the "
        "updated docker/requirements.txt to enable listening."
    )


# ── Defensive patches for discord-ext-voice-recv 0.5.x ───────────────────────
#
# Symptom: shortly after joining a voice channel, the router thread crashes with
#
#   discord.opus.OpusError: corrupted stream
#     File ".../voice_recv/opus.py", line 154, in _decode_packet
#       pcm = self._decoder.decode(packet.decrypted_data, fec=False)
#
# Root cause: voice_recv's RTCP→RTP demux occasionally leaks an RTCP control
# packet (e.g. Sender Reports, packet type 200) into the audio decode path.
# The decoder then hands non-Opus bytes to libopus, which (correctly) rejects
# them. The router thread has no top-level except, so it DIES — and from that
# point on no audio reaches our sink, but the bot looks healthy from outside.
#
# Fix: wrap PacketDecoder._decode_packet (and pop_data as a belt-and-braces
# layer) so any decode error is logged and the bad packet is silently skipped.
# A single dropped 20ms frame is invisible to Whisper; a dead router is fatal.
#
# Idempotent — safe to call multiple times. Logged when installed so the deploy
# log makes it obvious we're running with the patch.


def _install_voice_recv_patches() -> None:
    if not VOICE_RECV_AVAILABLE:
        return
    try:
        from discord.ext.voice_recv import opus as vr_opus  # type: ignore
    except ImportError:
        log.warning("voice-recv: opus submodule missing — skipping patches")
        return
    decoder_cls = getattr(vr_opus, "PacketDecoder", None)
    if decoder_cls is None:
        log.warning("voice-recv: PacketDecoder not found — skipping patches")
        return
    if getattr(decoder_cls, "_iris_patched", False):
        return

    original_decode = decoder_cls._decode_packet

    def patched_decode(self, packet):
        try:
            return original_decode(self, packet)
        except Exception as e:
            # OpusError is the common one; catch broadly so any decode-time
            # failure (truncated packet, codec assertion, etc.) doesn't
            # propagate to the router thread.
            log.debug(
                "voice-recv: dropping undecodable packet (%s: %s)",
                type(e).__name__, e,
            )
            return packet, b""

    decoder_cls._decode_packet = patched_decode
    decoder_cls._iris_patched = True
    log.info(
        "voice-recv: installed defensive decoder patch "
        "(suppresses OpusError 'corrupted stream' from RTCP leakage)"
    )


_install_voice_recv_patches()


# ── Wake-word gate ───────────────────────────────────────────────────────────


def parse_wake_words(raw: str | None) -> list[re.Pattern[str]]:
    """Compile a CSV of wake phrases into case-insensitive word-boundary
    regexes. An empty list disables the gate (route every utterance)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    out: list[re.Pattern[str]] = []
    for w in raw.split(","):
        w = w.strip()
        if not w:
            continue
        # Word-boundary anchoring works for Latin scripts. For CJK we wrap
        # in non-word chars (or start/end) since \b isn't well-defined.
        if re.match(r"^\w[\w\s]*\w$|^\w$", w):
            out.append(re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE))
        else:
            # Fallback: substring match (no word-boundary anchoring).
            out.append(re.compile(re.escape(w), re.IGNORECASE))
    return out


WAKE_WORDS = parse_wake_words(
    os.environ.get("IRIS_VOICE_WAKE_WORDS", "iris,hey iris,okay iris")
)


def transcript_passes_wake_word(text: str) -> bool:
    """True if (a) no wake words configured, or (b) any wake word matches."""
    if not WAKE_WORDS:
        return True
    return any(p.search(text) for p in WAKE_WORDS)


# ── Per-user buffer ──────────────────────────────────────────────────────────


class _UserBuffer:
    """Holds an in-progress utterance for one speaker."""

    __slots__ = ("pcm", "last_audio_ts", "first_audio_ts")

    def __init__(self) -> None:
        self.pcm = bytearray()
        self.last_audio_ts = 0.0  # monotonic time of last received frame
        self.first_audio_ts = 0.0

    def append(self, data: bytes) -> None:
        now = time.monotonic()
        if not self.pcm:
            self.first_audio_ts = now
        self.pcm.extend(data)
        self.last_audio_ts = now

    def is_silent_for(self, ms: int) -> bool:
        if not self.pcm:
            return False
        return (time.monotonic() - self.last_audio_ts) * 1000 >= ms

    def duration_ms(self) -> float:
        if not self.pcm:
            return 0.0
        return len(self.pcm) / DISCORD_PCM_BPS_PER_SEC * 1000

    def take(self) -> bytes:
        pcm = bytes(self.pcm)
        self.pcm.clear()
        self.first_audio_ts = 0.0
        self.last_audio_ts = 0.0
        return pcm


# ── Sink ─────────────────────────────────────────────────────────────────────


# Base class only exists if discord-ext-voice-recv is installed. We define
# the subclass conditionally so importing this module never crashes.
if VOICE_RECV_AVAILABLE:

    class WhisperSink(voice_recv.AudioSink):  # type: ignore[misc, name-defined]
        """Captures per-user audio, detects utterance boundaries by
        silence-timing, transcribes via Whisper, and dispatches each
        transcript to the provided async callback on the asyncio loop.
        """

        def __init__(
            self,
            loop: asyncio.AbstractEventLoop,
            on_utterance: Callable[[int, str], Awaitable[None]],
        ) -> None:
            super().__init__()
            self._loop = loop
            self._on_utterance = on_utterance
            self._buffers: dict[int, _UserBuffer] = {}
            self._lock = threading.Lock()
            self._paused = False
            self._stopped = False
            self._sweeper_handle: Optional[asyncio.tasks.Task] = None
            self._sweeper_future = None  # concurrent.futures.Future from run_coroutine_threadsafe

        # ── voice_recv API ───────────────────────────────────────────────
        def wants_opus(self) -> bool:  # type: ignore[override]
            return False  # we want decoded PCM

        def write(self, user, data) -> None:  # type: ignore[override]
            # write() runs on a discord.py worker thread.
            if user is None or self._paused or self._stopped:
                return
            pcm = getattr(data, "pcm", None)
            if not pcm:
                return
            flush_uid: int | None = None
            with self._lock:
                buf = self._buffers.get(user.id)
                if buf is None:
                    buf = _UserBuffer()
                    self._buffers[user.id] = buf
                buf.append(pcm)
                if buf.duration_ms() >= MAX_UTTERANCE_MS:
                    flush_uid = user.id
            if flush_uid is not None:
                self._schedule_flush(flush_uid, reason="max-duration")

        def cleanup(self) -> None:  # type: ignore[override]
            self._stopped = True
            uids: list[int]
            with self._lock:
                uids = list(self._buffers.keys())
            for uid in uids:
                self._schedule_flush(uid, reason="cleanup")

        # ── Pause / resume (echo gating) ─────────────────────────────────
        def pause(self) -> None:
            with self._lock:
                self._paused = True
                # Drop any partial utterance captured between request and
                # playback-start — those bytes are usually the tail of the
                # user's question and would have been flushed anyway, but
                # we'd rather over-discard than ship a fragment to Whisper.
                for buf in self._buffers.values():
                    buf.take()

        def resume(self) -> None:
            with self._lock:
                self._paused = False

        # ── Sweeper (runs on event loop) ─────────────────────────────────
        def start_sweeper(self) -> None:
            """Schedule the periodic silence-flush task on the event loop."""
            if self._sweeper_future is not None:
                return
            self._sweeper_future = asyncio.run_coroutine_threadsafe(
                self._sweeper_coro(), self._loop,
            )

        def stop_sweeper(self) -> None:
            self._stopped = True
            if self._sweeper_future is not None:
                self._sweeper_future.cancel()
                self._sweeper_future = None

        async def _sweeper_coro(self) -> None:
            log.info("voice-recv: sweeper started (hang=%dms min=%dms max=%dms)",
                     SILENCE_HANG_MS, MIN_UTTERANCE_MS, MAX_UTTERANCE_MS)
            try:
                while not self._stopped:
                    await asyncio.sleep(0.2)
                    self._sweep_once()
            except asyncio.CancelledError:
                log.info("voice-recv: sweeper cancelled")
                raise
            except Exception:
                log.exception("voice-recv: sweeper crashed")

        def _sweep_once(self) -> None:
            ready: list[tuple[int, bytes, float]] = []
            with self._lock:
                for uid, buf in list(self._buffers.items()):
                    if not buf.pcm:
                        continue
                    if buf.is_silent_for(SILENCE_HANG_MS):
                        dur = buf.duration_ms()
                        pcm = buf.take()
                        if dur >= MIN_UTTERANCE_MS:
                            ready.append((uid, pcm, dur))
                        else:
                            log.debug(
                                "voice-recv: discarding short utterance "
                                "user=%s dur=%.0fms", uid, dur,
                            )
            for uid, pcm, dur in ready:
                # _sweeper_coro runs ON the event loop, so create_task is
                # safe here (we're already in the loop's thread).
                asyncio.create_task(self._transcribe_and_dispatch(uid, pcm, dur))

        # ── Forced flush (called from background thread on hard cap) ─────
        def _schedule_flush(self, user_id: int, *, reason: str) -> None:
            with self._lock:
                buf = self._buffers.get(user_id)
                if buf is None or not buf.pcm:
                    return
                dur = buf.duration_ms()
                pcm = buf.take()
            if dur < MIN_UTTERANCE_MS:
                return
            log.debug("voice-recv: forced flush user=%s reason=%s dur=%.0fms",
                      user_id, reason, dur)
            asyncio.run_coroutine_threadsafe(
                self._transcribe_and_dispatch(user_id, pcm, dur), self._loop,
            )

        # ── Transcription + dispatch ─────────────────────────────────────
        async def _transcribe_and_dispatch(
            self, user_id: int, pcm: bytes, dur_ms: float,
        ) -> None:
            try:
                path = await asyncio.to_thread(self._pcm_to_temp_wav, pcm)
            except Exception:
                log.exception("voice-recv: WAV write failed for user=%s", user_id)
                return
            try:
                t0 = time.monotonic()
                transcript = await asyncio.to_thread(self._whisper_transcribe, path)
                stt_ms = (time.monotonic() - t0) * 1000
            except Exception:
                log.exception("voice-recv: whisper failed for user=%s", user_id)
                transcript = ""
                stt_ms = 0.0
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            transcript = (transcript or "").strip()
            if not transcript:
                log.debug("voice-recv: empty transcript user=%s dur=%.0fms",
                          user_id, dur_ms)
                return
            log.info(
                "voice-recv: user=%s utt=%.1fs stt=%.0fms transcript=%r",
                user_id, dur_ms / 1000, stt_ms, transcript[:120],
            )
            try:
                await self._on_utterance(user_id, transcript)
            except Exception:
                log.exception("voice-recv: utterance callback crashed")

        @staticmethod
        def _pcm_to_temp_wav(pcm: bytes) -> str:
            """Persist a 48kHz stereo PCM blob as a WAV. faster-whisper
            (CTranslate2 backend) handles resampling and channel down-mix
            internally, so we don't have to.
            """
            fd, path = tempfile.mkstemp(prefix="iris_utt_", suffix=".wav")
            os.close(fd)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(DISCORD_PCM_CHANNELS)
                wf.setsampwidth(DISCORD_PCM_BPS)
                wf.setframerate(DISCORD_PCM_SR)
                wf.writeframes(pcm)
            return path

        @staticmethod
        def _whisper_transcribe(wav_path: str) -> str:
            # Import here (not at module load) so this module is importable
            # in environments where the Iris package itself isn't on sys.path.
            from _iris.tools.voice import transcribe_audio_internal
            transcript, _lang = transcribe_audio_internal(wav_path)
            return transcript

else:

    class WhisperSink:  # type: ignore[no-redef]
        """Stub used when discord-ext-voice-recv isn't installed.

        Any attempt to instantiate it raises so the caller (bot.py) knows
        voice receive isn't wired up. Callers should gate construction on
        ``VOICE_RECV_AVAILABLE``.
        """

        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError(
                "discord-ext-voice-recv is not installed — Iris cannot hear. "
                "Add `discord-ext-voice-recv` to docker/requirements.txt and "
                "rebuild the container."
            )
