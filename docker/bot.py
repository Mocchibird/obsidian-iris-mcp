"""Iris Discord bot — Phase 1 (text).

A thin Discord ⇄ Claude bridge:

    Discord message ──► claude-agent-sdk query
                         │  Iris MCP server attached
                         │  Sonnet 4.6 (or whatever IRIS_DISCORD_MODEL says)
                         ▼
                       streamed reply
                         │
                         ▼
                   Discord (edited message)

Design notes (Phase 2 will reuse these):
- One Claude session per Discord channel/thread (resumed across turns), so
  context persists across the conversation without us having to manage memory.
- Streaming-first — we edit a single Discord message as content arrives,
  rather than waiting for the whole reply. Keeps perceived latency low and
  lays the groundwork for sentence-by-sentence TTS in Phase 2.
- ACL: optional CSV env vars to limit who/where the bot listens.
- Auth: uses the user's Claude subscription via `claude` CLI mounted under
  CLAUDE_CONFIG_DIR. No Anthropic API key needed.

Env vars (all optional unless noted):
    DISCORD_BOT_TOKEN              required
    IRIS_DISCORD_MODEL             default: claude-sonnet-4-6
    IRIS_DISCORD_ALLOWED_CHANNELS  CSV of channel IDs; empty = any channel
    IRIS_DISCORD_ALLOWED_USERS     CSV of user IDs; empty = anyone
    IRIS_DISCORD_SYSTEM_PROMPT     inline system prompt (overrides file)
    IRIS_DISCORD_SYSTEM_PROMPT_PATH  path to a markdown file with system prompt
    IRIS_VAULT_ROOT                default: /vault  (passed to Iris MCP server)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import discord

# claude-agent-sdk wraps the `claude` CLI and handles session resumption,
# MCP wiring, streaming. See https://github.com/anthropics/claude-agent-sdk-python
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    TextBlock,
)


# ── Config ───────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
MODEL = os.environ.get("IRIS_DISCORD_MODEL", "claude-sonnet-4-6").strip()
VAULT_ROOT = os.environ.get("IRIS_VAULT_ROOT", "/vault").strip()
SYSTEM_PROMPT = os.environ.get("IRIS_DISCORD_SYSTEM_PROMPT", "").strip()
SYSTEM_PROMPT_PATH = os.environ.get("IRIS_DISCORD_SYSTEM_PROMPT_PATH", "").strip()


def _parse_csv_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if piece.isdigit():
            out.add(int(piece))
    return out


ALLOWED_CHANNELS = _parse_csv_ids(os.environ.get("IRIS_DISCORD_ALLOWED_CHANNELS", ""))
ALLOWED_USERS = _parse_csv_ids(os.environ.get("IRIS_DISCORD_ALLOWED_USERS", ""))
# Channels where Iris responds to every human message (no @-mention needed).
# Use this for dedicated Iris channels like #iris-tasks, #iris-notes, etc.
# DMs are always treated this way regardless of this setting.
LISTEN_ALWAYS_CHANNELS = _parse_csv_ids(
    os.environ.get("IRIS_DISCORD_LISTEN_ALWAYS_CHANNELS", "")
)

# ── Proactive notifications ─────────────────────────────────────────────────
# When IRIS_DISCORD_NOTIFY_CHANNEL is set, a background loop scans the vault
# every IRIS_NOTIFY_INTERVAL_SECS and posts pings for:
#   - calendar events starting in <= IRIS_NOTIFY_LEAD_MIN minutes
#   - reminders whose remind_on date is today (with optional HH:MM prefix in
#     the reminder text → same lead-time logic as events)
# Sent messages are deduped via a small JSON file under /claude-auth so
# restarts don't double-ping.
try:
    NOTIFY_CHANNEL = int(os.environ.get("IRIS_DISCORD_NOTIFY_CHANNEL", "0") or "0")
except ValueError:
    NOTIFY_CHANNEL = 0
NOTIFY_INTERVAL_SECS = int(os.environ.get("IRIS_NOTIFY_INTERVAL_SECS", "300"))
NOTIFY_LEAD_MIN = int(os.environ.get("IRIS_NOTIFY_LEAD_MIN", "15"))
NOTIFIED_PATH = Path("/claude-auth/discord-notified.json")
_HHMM_PREFIX_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[—\-–]?\s*")


# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("IRIS_DISCORD_LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("iris.discord")


# ── System prompt ────────────────────────────────────────────────────────────

def _load_system_prompt() -> str | None:
    if SYSTEM_PROMPT:
        return SYSTEM_PROMPT
    if SYSTEM_PROMPT_PATH:
        p = Path(SYSTEM_PROMPT_PATH)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8").strip()
    # Built-in default — keep this short so users can override without
    # reading 300 lines of preset prose.
    return (
        "You are Iris, a personal-vault assistant talking to Hyun-Min "
        "(handle: Mocchibird) through Discord. You have access to his "
        "Obsidian vault via the `iris` MCP server — use those tools to "
        "look up notes, manage tasks, schedule events, etc. Keep replies "
        "concise enough for chat; long structured output is fine when "
        "asked for it. Use Discord-flavored markdown."
    )


# ── Claude Agent SDK wiring ──────────────────────────────────────────────────

def _build_options() -> ClaudeAgentOptions:
    """Per-turn options. The MCP server config tells Claude how to launch Iris."""
    return ClaudeAgentOptions(
        model=MODEL,
        cwd="/opt/iris",
        system_prompt=_load_system_prompt(),
        mcp_servers={
            "iris": {
                "type": "stdio",
                "command": "python",
                "args": ["/opt/iris/obsidian_memory_mcp.py"],
                "env": {"IRIS_VAULT_ROOT": VAULT_ROOT, **os.environ},
            }
        },
        # Trust all Iris tools. Iris's own write tools have validation
        # baked in; this is a personal-use bot in a private Discord.
        permission_mode="bypassPermissions",
    )


# ── Discord-side streaming helper ────────────────────────────────────────────
# Discord rate-limits message edits to ~5 per 5 seconds per channel. We
# coalesce streamed chunks and flush at most every ~0.8 s so the UI feels
# live without tripping the limit.

DISCORD_EDIT_INTERVAL = 0.8       # seconds between edits during streaming
DISCORD_MESSAGE_LIMIT = 1900       # leave headroom under the 2000-char cap


class StreamingReply:
    """Wraps a Discord message and edits it as more text arrives."""

    def __init__(self, sent_message: discord.Message):
        self._messages: list[discord.Message] = [sent_message]
        self._buffer = ""
        self._last_edit = time.monotonic()

    async def append(self, chunk: str) -> None:
        if not chunk:
            return
        self._buffer += chunk
        if time.monotonic() - self._last_edit >= DISCORD_EDIT_INTERVAL:
            await self._flush()

    async def finalize(self) -> None:
        await self._flush(force=True)

    async def _flush(self, force: bool = False) -> None:
        content = self._buffer
        # Discord caps single-message size at 2000 chars. Split across messages
        # if we've blown past that during the stream.
        if len(content) <= DISCORD_MESSAGE_LIMIT:
            try:
                await self._messages[-1].edit(content=content or "…")
            except discord.HTTPException as e:
                log.warning("edit failed: %s", e)
        else:
            head, tail = content[:DISCORD_MESSAGE_LIMIT], content[DISCORD_MESSAGE_LIMIT:]
            try:
                await self._messages[-1].edit(content=head)
                new_msg = await self._messages[-1].channel.send(tail or "…")
                self._messages.append(new_msg)
                self._buffer = tail
            except discord.HTTPException as e:
                log.warning("split failed: %s", e)
        self._last_edit = time.monotonic()


# ── Per-channel session memory ───────────────────────────────────────────────
# We keep one ClaudeSDKClient per channel/thread so conversations persist.

_sessions: dict[int, ClaudeSDKClient] = {}
_session_locks: dict[int, asyncio.Lock] = {}


def _session_key(message: discord.Message) -> int:
    return message.channel.id


async def _get_lock(key: int) -> asyncio.Lock:
    if key not in _session_locks:
        _session_locks[key] = asyncio.Lock()
    return _session_locks[key]


async def _get_or_create_client(key: int) -> ClaudeSDKClient:
    if key not in _sessions:
        client = ClaudeSDKClient(options=_build_options())
        await client.connect()
        _sessions[key] = client
        log.info("opened new Claude session for channel %s", key)
    return _sessions[key]


# ── Discord bot ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True

client = discord.Client(intents=intents)


def _is_allowed(message: discord.Message) -> bool:
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return False
    if ALLOWED_USERS and message.author.id not in ALLOWED_USERS:
        return False
    return True


@client.event
async def on_ready() -> None:
    log.info("Iris connected as %s (model=%s, vault=%s)",
             client.user, MODEL, VAULT_ROOT)
    if ALLOWED_CHANNELS:
        log.info("restricted to channels: %s", sorted(ALLOWED_CHANNELS))
    if ALLOWED_USERS:
        log.info("restricted to users: %s", sorted(ALLOWED_USERS))
    if LISTEN_ALWAYS_CHANNELS:
        log.info("always-listen channels: %s", sorted(LISTEN_ALWAYS_CHANNELS))
    if NOTIFY_CHANNEL:
        log.info(
            "proactive notifications → channel %s every %ss, lead %s min",
            NOTIFY_CHANNEL, NOTIFY_INTERVAL_SECS, NOTIFY_LEAD_MIN,
        )
        client.loop.create_task(_notification_loop())


# ── Proactive notification loop ─────────────────────────────────────────────
# Persisted dedupe — JSON file under /claude-auth so restarts don't re-ping
# events/reminders we already sent.

def _load_notified() -> set[str]:
    if not NOTIFIED_PATH.exists():
        return set()
    try:
        data = json.loads(NOTIFIED_PATH.read_text())
        return set(data.get("keys", []))
    except Exception:
        return set()


def _save_notified(keys: set[str]) -> None:
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Keep only the most recent 1000 keys to bound the file size
    recent = list(keys)[-1000:]
    NOTIFIED_PATH.write_text(json.dumps({
        "keys": recent,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }))


_notified: set[str] = set()


async def _notification_loop() -> None:
    """Scan vault every NOTIFY_INTERVAL_SECS for events/reminders to ping."""
    global _notified
    _notified = _load_notified()
    # Brief delay so on_ready finishes before the first scan
    await asyncio.sleep(5)
    while not client.is_closed():
        try:
            await _check_upcoming()
        except Exception:
            log.exception("notification check failed")
        await asyncio.sleep(NOTIFY_INTERVAL_SECS)


async def _check_upcoming() -> None:
    if NOTIFY_CHANNEL == 0:
        return
    # Local import so the bot still boots if Iris's package isn't yet on path
    import iris_config as cfg
    db_path = cfg.vault_db_path()
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        await _check_events(conn)
        await _check_reminders(conn)
    finally:
        conn.close()


def _minutes_until(target_time: str, today_iso: str) -> float | None:
    """Parse 'HH:MM' for today; return minutes from now (negative if past)."""
    if not target_time or ":" not in target_time:
        return None
    try:
        hh, mm = target_time.split(":")[:2]
        target = datetime.now().replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0
        )
    except ValueError:
        return None
    delta = (target - datetime.now()).total_seconds() / 60
    return delta


async def _check_events(conn: sqlite3.Connection) -> None:
    today = datetime.now().date().isoformat()
    rows = conn.execute(
        "SELECT date, time, end_time, title, location FROM events "
        "WHERE date = ? AND time != '' AND time NOT IN ('00:00', '0:00')",
        (today,),
    ).fetchall()
    for r in rows:
        lead = _minutes_until(r["time"], today)
        if lead is None or lead <= 0 or lead > NOTIFY_LEAD_MIN:
            continue
        key = f"event:{r['date']}:{r['time']}:{r['title']}"
        if key in _notified:
            continue
        _notified.add(key)
        loc = f" @ {r['location']}" if r["location"] else ""
        end = f"–{r['end_time']}" if r["end_time"] else ""
        await _send_notification(
            f"⏰ **{r['title']}** in {int(round(lead))} min — "
            f"{r['time']}{end}{loc}"
        )
    _save_notified(_notified)


async def _check_reminders(conn: sqlite3.Connection) -> None:
    today = datetime.now().date().isoformat()
    rows = conn.execute(
        "SELECT text, remind_on, repeat, note_path FROM reminders "
        "WHERE checked = 0 AND remind_on = ?",
        (today,),
    ).fetchall()
    for r in rows:
        text = r["text"] or ""
        m = _HHMM_PREFIX_RE.match(text)
        if m:
            hhmm = f"{m.group(1)}:{m.group(2)}"
            lead = _minutes_until(hhmm, today)
            if lead is None or lead <= 0 or lead > NOTIFY_LEAD_MIN:
                continue
            clean = _HHMM_PREFIX_RE.sub("", text).strip() or "(reminder)"
            key = f"rem:{r['remind_on']}:{hhmm}:{clean}"
            if key in _notified:
                continue
            _notified.add(key)
            await _send_notification(
                f"🔔 **Reminder** in {int(round(lead))} min — {clean}"
            )
        else:
            # No time embedded — single all-day ping at first check of the day
            key = f"rem-allday:{r['remind_on']}:{text}"
            if key in _notified:
                continue
            _notified.add(key)
            await _send_notification(f"🔔 **Reminder today** — {text}")
    _save_notified(_notified)


async def _send_notification(content: str) -> None:
    channel = client.get_channel(NOTIFY_CHANNEL)
    if channel is None:
        try:
            channel = await client.fetch_channel(NOTIFY_CHANNEL)
        except discord.HTTPException as e:
            log.warning("could not fetch notify channel %s: %s",
                        NOTIFY_CHANNEL, e)
            return
    try:
        await channel.send(content)  # type: ignore[union-attr]
        log.info("notification → #%s: %s",
                 getattr(channel, "name", NOTIFY_CHANNEL), content[:80])
    except discord.HTTPException as e:
        log.warning("notification send failed: %s", e)


def _is_reply_to_bot(message: discord.Message) -> bool:
    """True if this message is a Discord 'reply' to one of the bot's messages."""
    ref = message.reference
    if ref is None or ref.resolved is None:
        return False
    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == (client.user.id if client.user else 0)
    return False


@client.event
async def on_message(message: discord.Message) -> None:
    # Ignore self and other bots
    if message.author.bot:
        return

    # Iris responds in any of these situations:
    #   1. The message is a DM to the bot
    #   2. The message @-mentions the bot
    #   3. The message replies to one of the bot's previous messages
    #   4. The channel is in the always-listen list (e.g. dedicated #iris-* rooms)
    in_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = client.user in message.mentions
    is_reply_to_bot = _is_reply_to_bot(message)
    is_always_listen = message.channel.id in LISTEN_ALWAYS_CHANNELS
    if not (in_dm or is_mention or is_reply_to_bot or is_always_listen):
        return

    if not _is_allowed(message):
        log.info("ignored message from %s in #%s (ACL)",
                 message.author, getattr(message.channel, "name", message.channel.id))
        return

    # Strip @mention from the user's text
    content = message.content.strip()
    if client.user:
        content = content.replace(f"<@{client.user.id}>", "").replace(
            f"<@!{client.user.id}>", "").strip()
    if not content:
        return

    key = _session_key(message)
    lock = await _get_lock(key)
    if lock.locked():
        # Don't queue parallel queries in the same channel; tell the user.
        await message.reply("⏳ Still working on the previous message — give me a sec.")
        return

    async with lock:
        async with message.channel.typing():
            placeholder = await message.reply("…")
            stream = StreamingReply(placeholder)
            try:
                agent = await _get_or_create_client(key)
                await agent.query(content)
                async for msg in agent.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                await stream.append(block.text)
                await stream.finalize()
            except Exception as e:
                log.exception("query failed")
                try:
                    await placeholder.edit(content=f"❌ Error: {e}")
                except discord.HTTPException:
                    pass


# ── Graceful shutdown ────────────────────────────────────────────────────────

async def _shutdown() -> None:
    log.info("shutting down — closing %d Claude session(s)", len(_sessions))
    for c in _sessions.values():
        try:
            await c.disconnect()
        except Exception:
            pass


def main() -> None:
    if not TOKEN:
        log.error("DISCORD_BOT_TOKEN is not set.")
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(client.start(TOKEN))
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        loop.run_until_complete(_shutdown())
        loop.close()


if __name__ == "__main__":
    main()
