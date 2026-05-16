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
import logging
import os
import sys
import time
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


@client.event
async def on_message(message: discord.Message) -> None:
    # Ignore self and other bots
    if message.author.bot:
        return

    # If we're in a guild and the bot is NOT mentioned and NOT in a DM, ignore.
    # In DMs (and threads where the bot was explicitly invited), respond to all
    # human messages. Adjust to taste.
    in_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = client.user in message.mentions
    if not in_dm and not is_mention:
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
