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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# When creating a NEW Claude session for a channel (cold-start or post-restart),
# the bot fetches the last N minutes of channel messages and injects them into
# the system prompt as "recent context". This way conversations feel continuous
# across bot restarts, without persisting Claude sessions to disk — the vault
# stays the canonical long-term memory; Discord history covers the short term.
# Set to 0 to disable (each restart = clean slate).
CONTEXT_MINUTES = int(os.environ.get("IRIS_DISCORD_CONTEXT_MINUTES", "60"))
# Soft token budget for the injection. ~3 chars/token for mixed-language
# content (CJK is denser than the English-typical 4 chars/token), so a 2000-
# token budget is ~6000 chars. The actual selection is "fuzzy" — see below.
CONTEXT_TOKEN_BUDGET = int(os.environ.get("IRIS_DISCORD_CONTEXT_TOKEN_BUDGET", "2000"))
# How much we'll overshoot the budget to keep a coherent "burst" of related
# messages intact rather than chopping it mid-conversation. 1.5 means the
# absolute hard ceiling is 1.5 × budget.
CONTEXT_FUZZ_FACTOR = float(os.environ.get("IRIS_DISCORD_CONTEXT_FUZZ_FACTOR", "1.5"))
# Time gap (minutes) that ends one conversation burst and starts another.
# Messages within this gap of each other are treated as one topic block;
# trimming snaps to burst boundaries, never mid-block.
CONTEXT_BURST_GAP_MIN = int(os.environ.get("IRIS_DISCORD_CONTEXT_BURST_GAP_MIN", "10"))
# Rough conservative char-per-token estimate for budget math (CJK-friendly).
_CHARS_PER_TOKEN = 3

# Per-channel JSONL log of every message we see. Iris can read this on
# demand via the `fetch_discord_history` MCP tool when a conversation
# references something older than the cold-start context window.
HISTORY_DIR = Path(
    os.environ.get("IRIS_DISCORD_HISTORY_DIR", "/claude-auth/discord-channels")
)

# ── Proactive notifications ─────────────────────────────────────────────────
# When IRIS_DISCORD_PING_CHANNEL (legacy alias: IRIS_DISCORD_NOTIFY_CHANNEL)
# is set, Iris posts to that channel unprompted, in three flavours:
#
#   1. Upcoming-event/reminder pings    every IRIS_NOTIFY_INTERVAL_SECS
#                                       within IRIS_NOTIFY_LEAD_MIN
#   2. Morning briefing                 once a day at IRIS_NOTIFY_MORNING_AT
#   3. Evening wrap-up                  once a day at IRIS_NOTIFY_EVENING_AT
#
# Reactions on Iris's own pings act as snooze controls:
#   ⏰ = +5 min     🛏️ = +15 min     💤 = +1 hr
#
# Persistence (deduped sends, snoozed resends) lives under /claude-auth.
try:
    PING_CHANNEL = int(
        os.environ.get("IRIS_DISCORD_PING_CHANNEL")
        or os.environ.get("IRIS_DISCORD_NOTIFY_CHANNEL")
        or "0"
    )
except ValueError:
    PING_CHANNEL = 0
NOTIFY_INTERVAL_SECS = int(os.environ.get("IRIS_NOTIFY_INTERVAL_SECS", "300"))
NOTIFY_LEAD_MIN = int(os.environ.get("IRIS_NOTIFY_LEAD_MIN", "15"))
NOTIFY_MORNING_AT = os.environ.get("IRIS_NOTIFY_MORNING_AT", "08:00").strip() or "off"
NOTIFY_EVENING_AT = os.environ.get("IRIS_NOTIFY_EVENING_AT", "22:00").strip() or "off"
NOTIFIED_PATH = Path("/claude-auth/discord-notified.json")
SNOOZE_PATH = Path("/claude-auth/discord-snoozed.json")
_HHMM_PREFIX_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[—\-–]?\s*")
SNOOZE_EMOJI_MINUTES = {"⏰": 5, "🛏️": 15, "💤": 60}
# Per-event lead override — parsed from the event's `description` field or
# the reminder text. Examples that match: "lead: 2h", "lead:30m", "lead 120".
# Bare number = minutes. Suffix h = hours, m = minutes.
_LEAD_HINT_RE = re.compile(r"\blead\s*:?\s*(\d+)\s*([hm]?)\b", re.IGNORECASE)


def _looks_like_url(s: str) -> bool:
    """True if the string already starts with http(s)://. Used to skip
    auto-generating a maps link when the user's `location` field is itself
    a URL (e.g. they pasted a Maps link directly)."""
    return bool(re.match(r"^\s*https?://", s or ""))


def _parse_lead_min(text: str | None, default: int) -> int:
    """Pull a per-item lead override from arbitrary text. Falls back to default."""
    if not text:
        return default
    m = _LEAD_HINT_RE.search(text)
    if not m:
        return default
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "h":
        n *= 60
    # Cap so a typo like "lead: 99999" doesn't permanently silence the ping
    return max(1, min(n, 24 * 60))

# ── Timezone resolution ───────────────────────────────────────────────────
# Default zone for "what time is it for the user right now". Override per-day
# by setting `timezone: <IANA>` in the daily note's frontmatter — useful when
# travelling so morning briefings still fire at 08:00 *local time* in Korea
# instead of 08:00 your home zone.
HOME_TZ_NAME = (os.environ.get("IRIS_TIMEZONE")
                or os.environ.get("TZ")
                or "Europe/Zurich").strip()


def _safe_zoneinfo(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None


HOME_TZ = _safe_zoneinfo(HOME_TZ_NAME) or ZoneInfo("UTC")
# Match `timezone: Asia/Seoul` or `timezone: "Asia/Seoul"` in YAML frontmatter
_TZ_FRONTMATTER_RE = re.compile(
    r'^\s*timezone\s*:\s*[\'"]?([A-Za-z_/+\-0-9]+)[\'"]?\s*$',
    re.MULTILINE,
)


def _resolve_active_tz() -> ZoneInfo:
    """Return the timezone to use for "now" right now.

    Reads the daily note for *today in HOME_TZ* and honours its
    ``timezone:`` frontmatter if present, otherwise falls back to HOME_TZ.
    """
    try:
        import iris_config as cfg
    except Exception:
        return HOME_TZ
    today_iso = datetime.now(HOME_TZ).date().isoformat()
    daily = cfg.VAULT_ROOT / "30_Episodic" / today_iso[:4] / f"{today_iso}.md"
    if not daily.exists():
        return HOME_TZ
    try:
        head = daily.read_text(encoding="utf-8", errors="ignore")[:2000]
    except OSError:
        return HOME_TZ
    # Only honour frontmatter (between the first two `---` lines)
    if not head.startswith("---"):
        return HOME_TZ
    end = head.find("\n---", 4)
    if end == -1:
        return HOME_TZ
    m = _TZ_FRONTMATTER_RE.search(head[:end])
    if not m:
        return HOME_TZ
    tz = _safe_zoneinfo(m.group(1).strip())
    return tz or HOME_TZ


def _now_local() -> datetime:
    """`datetime.now()` in the currently active timezone (may shift mid-day
    when you transition into a travel daily note with a different `timezone:` set)."""
    return datetime.now(_resolve_active_tz())


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
        "asked for it. Use Discord-flavored markdown.\n\n"
        "Timezone convention: when Hyun-Min plans a trip to another "
        "timezone (e.g. Korea), set `timezone: <IANA name>` (e.g. "
        "`Asia/Seoul`) in the frontmatter of each daily note for the "
        "travel days, using `set_frontmatter_field`. The Discord bot "
        "reads this and shifts the morning briefing and evening wrap-up "
        "to fire at 08:00 / 22:00 *local* time wherever he is.\n\n"
        "Per-event ping lead time: the Discord bot pings 15 minutes "
        "before an event by default. For events that need a longer "
        "head-start (travel to another city, packing before a trip, "
        "etc.), include `lead: 2h` (or `lead: 30m`, `lead: 90`) in the "
        "event's `description` field when calling `schedule_event`. The "
        "bot parses that and uses it as the per-event lead window. So "
        "'meeting in Basel at 14:00' → description=`lead: 2h` → "
        "ping at 12:00. Reminders can carry the same hint in their text.\n\n"
        "Useful links in chat: Discord auto-renders URLs (Google Maps "
        "shows a preview card, YouTube embeds the player, etc.). When a "
        "location, address, restaurant, or venue comes up, drop a Google "
        "Maps URL like https://www.google.com/maps/search/?api=1&query=<URL-encoded address>. "
        "Same for transit (SBB/Trainline/etc.), flight status pages, "
        "Wikipedia, recipe links, anything useful — Discord renders them "
        "inline and saves him from having to search. Don't ask permission, "
        "just include the link.\n\n"
        "Extended Discord memory: your in-context view of this channel is "
        "the most recent burst of conversation. If Hyun-Min references "
        "something said earlier that isn't in your immediate context — and "
        "checking the vault for it doesn't help — call the "
        "`fetch_discord_history(hours_back=N)` tool to pull the relevant "
        "older messages from the bot's stored log. Don't pre-fetch on every "
        "turn; only when a reference clearly points past your current window.\n\n"
        "File uploads via Discord: when Hyun-Min attaches a file, the bot "
        "automatically saves it under `90_Inbox/inbox/<timestamp>_<name>` "
        "and surfaces the saved path(s) in your prompt under a "
        "`[Files just saved to the vault inbox: ...]` block. Treat that as "
        "a task: figure out what each file is and route it. Quick playbook:\n"
        " - Photo of a whiteboard / screenshot → `import_drop_zone` files "
        "   it under `40_Attachments/Images/` and creates an inbox note "
        "   that embeds it; you then decide if it belongs to an existing "
        "   project page and `move_files` accordingly.\n"
        " - PDF (receipt, document) → `extract_pdf_text` to read it, then "
        "   route. Receipts/invoices often belong with warranties.\n"
        " - Image you can see directly → use the Read tool to view the "
        "   image and infer what it is, then file appropriately.\n"
        " - When unsure where it goes, ask in chat rather than guessing.\n\n"
        "When to write to the vault on your own initiative:\n"
        " - **Concrete facts with a time/place** — calendar invites, "
        "   appointments, meetings, travel bookings — write them IMMEDIATELY "
        "   via `schedule_event` / `add_reminder` / etc. without asking. "
        "   These aren't speculative; they belong in the vault by definition. "
        "   Don't bury the calendar event under brainstorming about side "
        "   activities (dinner after the ceremony, etc.). Anchor first, "
        "   then chat.\n"
        " - **Decisions** — when Hyun-Min commits to a choice ('let's go "
        "   with Sonnenberg', 'I'll fly KLM'), write that decision down "
        "   via `add_decision` on the relevant project / daily note, or "
        "   append to its `## Notes`.\n"
        " - **Explicit requests** — 'save this', 'add to my project notes', "
        "   etc. Always honour these.\n"
        "What NOT to auto-save:\n"
        " - **Speculative options** — restaurant shortlists, brainstorms, "
        "   'what if' scenarios. Present in chat, let him pick, then save "
        "   the selection. Bombarding the vault with 4 unselected restaurants "
        "   creates noise.\n"
        " - **Pure chitchat** — small talk, jokes, status pings."
    )


# ── Claude Agent SDK wiring ──────────────────────────────────────────────────

def _build_options(
    system_prompt: str | None = None,
    channel_id: int | None = None,
) -> ClaudeAgentOptions:
    """Per-session options. The MCP server config tells Claude how to launch
    Iris. ``system_prompt`` overrides the default (used to inject recent
    Discord history into a cold-start session). ``channel_id`` is passed via
    env so the ``fetch_discord_history`` MCP tool knows which channel's log
    to read.
    """
    iris_env: dict[str, str] = {
        **os.environ,
        "IRIS_VAULT_ROOT": VAULT_ROOT,
        "IRIS_DISCORD_HISTORY_DIR": str(HISTORY_DIR),
    }
    if channel_id is not None:
        iris_env["IRIS_DISCORD_CHANNEL_ID"] = str(channel_id)
    return ClaudeAgentOptions(
        model=MODEL,
        cwd="/opt/iris",
        system_prompt=system_prompt if system_prompt is not None else _load_system_prompt(),
        mcp_servers={
            "iris": {
                "type": "stdio",
                "command": "python",
                "args": ["/opt/iris/obsidian_memory_mcp.py"],
                "env": iris_env,
            }
        },
        # Trust all Iris tools. Iris's own write tools have validation
        # baked in; this is a personal-use bot in a private Discord.
        permission_mode="bypassPermissions",
    )


_INBOX_REL = "90_Inbox/inbox"
# Sanitize attachment filenames — strip anything outside a safe character set
# so a hostile or weird name can't escape the inbox directory.
_SAFE_FILENAME_RE = re.compile(r"[^\w\-. ]+")


async def _save_attachments_to_inbox(message: discord.Message) -> list[str]:
    """Download any Discord attachments on the message into the vault's
    90_Inbox/inbox/ folder. Returns a list of vault-relative paths saved.

    Filenames are sanitized + timestamped to avoid collisions. The vault
    is mounted at /vault inside the container, but we use the host-side
    IRIS_VAULT_ROOT path so the paths Iris sees match what the iris MCP
    tools (run inside the same container) operate on.
    """
    if not message.attachments:
        return []
    inbox = Path(VAULT_ROOT) / _INBOX_REL
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("could not create inbox dir %s: %s", inbox, e)
        return []
    saved: list[str] = []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for att in message.attachments:
        safe = _SAFE_FILENAME_RE.sub("_", att.filename).strip("._ ") or "file"
        target = inbox / f"{ts}_{safe}"
        counter = 1
        while target.exists():
            target = inbox / f"{ts}_{counter}_{safe}"
            counter += 1
        try:
            await att.save(target)
        except (discord.HTTPException, OSError) as e:
            log.warning("attachment save failed for %s: %s", att.filename, e)
            continue
        rel = f"{_INBOX_REL}/{target.name}"
        saved.append(rel)
        log.info("attachment → %s (%d bytes)", rel, att.size)
    return saved


async def _log_channel_message(message: discord.Message) -> None:
    """Append a single message to the per-channel JSONL log.

    Called from on_message for every message we observe in any channel the
    bot can see — including the bot's own messages so the history is
    complete. Iris reads these later via `fetch_discord_history`.
    """
    if client.user is None:
        return
    content = (message.content or "")
    if not content.strip():
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{message.channel.id}.jsonl"
    is_iris = message.author.id == client.user.id
    entry = {
        "ts": message.created_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "author_id": message.author.id,
        "author": (message.author.display_name or message.author.name),
        "is_iris": is_iris,
        "is_proactive": _is_proactive_ping(content.strip(), is_iris),
        "content": content,
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("channel-log write failed: %s", e)


_PROACTIVE_PING_LEADS = ("⏰", "🔔", "🌅", "🌙", "💤", "🗺️")


def _is_proactive_ping(content: str, author_is_iris: bool) -> bool:
    if not author_is_iris:
        return False
    return any(content.startswith(lead) for lead in _PROACTIVE_PING_LEADS)


def _group_into_bursts(
    msgs: list[tuple[datetime, str]], gap_minutes: int,
) -> list[list[tuple[datetime, str]]]:
    """Split (timestamp, formatted-line) tuples into conversation bursts.

    A "burst" is a run of messages where consecutive items are <= gap_minutes
    apart. Trimming will snap to burst boundaries, never mid-burst.
    """
    if not msgs:
        return []
    bursts: list[list[tuple[datetime, str]]] = [[msgs[0]]]
    for prev, current in zip(msgs, msgs[1:]):
        gap = (current[0] - prev[0]).total_seconds() / 60
        if gap > gap_minutes:
            bursts.append([current])
        else:
            bursts[-1].append(current)
    return bursts


def _select_bursts_within_budget(
    bursts: list[list[tuple[datetime, str]]],
    char_budget: int,
    fuzz_factor: float,
) -> list[list[tuple[datetime, str]]]:
    """Walk newest-burst to oldest. Include each burst whose addition keeps
    total chars under ``char_budget * fuzz_factor``. The first burst is
    always included even if oversized (newest = most relevant). If a single
    burst is itself bigger than the hard ceiling, it's still returned in
    full — the caller will then truncate from the OLDER end of that burst
    so the most recent messages survive.
    """
    if not bursts:
        return []
    hard_cap = int(char_budget * fuzz_factor)
    selected: list[list[tuple[datetime, str]]] = []
    total = 0
    for burst in reversed(bursts):
        burst_chars = sum(len(line) + 1 for _, line in burst)  # +1 for newline
        if not selected:
            # Always include the newest burst, even if huge
            selected.append(burst)
            total += burst_chars
            continue
        # Fuzz rule: include if the resulting total is still within the
        # hard cap. This implicitly allows overshoot up to fuzz_factor when
        # an upcoming burst would push us past the soft budget — we keep
        # the full burst together.
        if total + burst_chars <= hard_cap:
            selected.insert(0, burst)
            total += burst_chars
            # If we're past the soft budget AND the next burst is far back
            # in time, stop. Otherwise keep going up to the hard cap.
            if total >= char_budget:
                continue
        else:
            break
    return selected


async def _fetch_recent_history(message: discord.Message) -> str:
    """Pull recent channel history for cold-start context injection.

    Selection strategy:
      1. Fetch up to CONTEXT_MINUTES of past messages (outer time bound).
      2. Drop empty + bot proactive pings (event reminders, briefings).
      3. Group surviving messages into bursts (gaps > CONTEXT_BURST_GAP_MIN
         start a new burst).
      4. Walk newest-burst to oldest, accumulating until the soft token
         budget is hit. Allow overshoot up to CONTEXT_FUZZ_FACTOR × budget
         so a coherent burst is never truncated mid-conversation. If a
         single burst is itself bigger than the hard cap, truncate its
         OLDER end to fit.

    Returns an empty string if disabled or nothing useful in the window.
    """
    if CONTEXT_MINUTES <= 0 or client.user is None:
        return ""

    after_ts = datetime.now(timezone.utc) - timedelta(minutes=CONTEXT_MINUTES)
    formatted: list[tuple[datetime, str]] = []  # (created_at, "[hh:mm] Author: line")
    try:
        async for m in message.channel.history(
            before=message, after=after_ts, limit=400, oldest_first=True
        ):
            content = (m.content or "").strip()
            if not content:
                continue
            author_is_iris = (m.author.id == client.user.id)
            if _is_proactive_ping(content, author_is_iris):
                continue
            author = ("Iris" if author_is_iris
                      else (m.author.display_name or m.author.name))
            ts = m.created_at.astimezone().strftime("%H:%M")
            # Per-line cap so a single pasted wall doesn't dominate
            line_body = content if len(content) <= 800 else content[:797] + "…"
            formatted.append((m.created_at, f"[{ts}] {author}: {line_body}"))
    except discord.HTTPException as e:
        log.warning("fetch_recent_history: %s", e)
        return ""

    if not formatted:
        return ""

    char_budget = CONTEXT_TOKEN_BUDGET * _CHARS_PER_TOKEN
    bursts = _group_into_bursts(formatted, CONTEXT_BURST_GAP_MIN)
    chosen = _select_bursts_within_budget(bursts, char_budget, CONTEXT_FUZZ_FACTOR)

    # Flatten back to lines
    flat_lines: list[str] = []
    for i, burst in enumerate(chosen):
        if i > 0:
            flat_lines.append("…")  # visual marker between non-adjacent bursts
        flat_lines.extend(line for _, line in burst)
    joined = "\n".join(flat_lines)

    # Hard ceiling — only relevant if a single burst exceeded fuzz × budget.
    # Truncate from the older end so the newest stuff survives.
    hard_cap = int(char_budget * CONTEXT_FUZZ_FACTOR)
    if len(joined) > hard_cap:
        joined = "…(earlier portion of this burst truncated)…\n" + joined[-hard_cap:]

    total_msgs = sum(len(b) for b in chosen)
    log.info(
        "history: %d messages in %d burst(s), %d chars "
        "(soft budget %d, hard cap %d)",
        total_msgs, len(chosen), len(joined), char_budget, hard_cap,
    )
    return joined


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


async def _get_or_create_client(
    key: int,
    seed_message: discord.Message | None = None,
) -> ClaudeSDKClient:
    """Return an existing in-process Claude session or open a new one.

    On a cold start (no session for this channel), fetch the last
    CONTEXT_MINUTES of Discord history and inject it into the new session's
    system prompt as "recent context". Keeps conversations feeling continuous
    across bot restarts without persisting Claude sessions to disk.
    """
    if key in _sessions:
        return _sessions[key]

    sys_prompt = _load_system_prompt() or ""
    if seed_message is not None and CONTEXT_MINUTES > 0:
        recent = await _fetch_recent_history(seed_message)
        if recent:
            sys_prompt = (
                sys_prompt
                + "\n\n## Recent Discord history in this channel\n\n"
                + "Below are the last "
                + f"{CONTEXT_MINUTES} minutes of messages in this channel "
                + "for context. Treat them as background memory; don't reply "
                + "to them, just remember.\n\n"
                + recent
            )
            log.info("seeded channel %s with %d chars of recent history",
                     key, len(recent))

    sdk_client = ClaudeSDKClient(
        options=_build_options(system_prompt=sys_prompt, channel_id=key)
    )
    await sdk_client.connect()
    _sessions[key] = sdk_client
    log.info("opened new Claude session for channel %s", key)
    return sdk_client


# ── Discord bot ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.reactions = True  # for snooze emoji

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
    log.info(
        "cold-start context: ≤%d min, soft %d tokens, fuzz ×%.1f, "
        "burst-gap %d min (IRIS_DISCORD_CONTEXT_MINUTES=0 disables)",
        CONTEXT_MINUTES, CONTEXT_TOKEN_BUDGET,
        CONTEXT_FUZZ_FACTOR, CONTEXT_BURST_GAP_MIN,
    )
    if PING_CHANNEL:
        active_tz = _resolve_active_tz()
        log.info(
            "ping channel: %s — events/reminders every %ss (lead %s min)",
            PING_CHANNEL, NOTIFY_INTERVAL_SECS, NOTIFY_LEAD_MIN,
        )
        log.info("home TZ: %s — active TZ now: %s", HOME_TZ_NAME, active_tz)
        if NOTIFY_MORNING_AT != "off":
            log.info("morning briefing at %s daily (in active TZ)", NOTIFY_MORNING_AT)
        if NOTIFY_EVENING_AT != "off":
            log.info("evening wrap-up at %s daily (in active TZ)", NOTIFY_EVENING_AT)
        client.loop.create_task(_notification_loop())
        client.loop.create_task(_scheduled_briefings_loop())
        client.loop.create_task(_snooze_replay_loop())


# ── Proactive notification loop ─────────────────────────────────────────────
# Persisted dedupe — JSON file under /claude-auth so restarts don't re-ping
# events/reminders we already sent.

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _load_notified() -> set[str]:
    return set(_load_json(NOTIFIED_PATH, {}).get("keys", []))


def _save_notified(keys: set[str]) -> None:
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    recent = list(keys)[-1000:]
    NOTIFIED_PATH.write_text(json.dumps({
        "keys": recent,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }))


# Snooze persistence: list of {"resend_at": iso, "content": str} entries.
def _load_snoozed() -> list[dict]:
    return _load_json(SNOOZE_PATH, {"items": []}).get("items", [])


def _save_snoozed(items: list[dict]) -> None:
    SNOOZE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNOOZE_PATH.write_text(json.dumps({
        "items": items,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }))


_notified: set[str] = set()
# Track last-fire dates for scheduled briefings so they don't repeat on the
# same day if the bot restarts.
_last_morning_fired: str = ""
_last_evening_fired: str = ""


async def _notification_loop() -> None:
    """Scan vault every NOTIFY_INTERVAL_SECS for events/reminders to ping."""
    global _notified
    _notified = _load_notified()
    await asyncio.sleep(5)  # let on_ready finish
    while not client.is_closed():
        try:
            await _check_upcoming()
        except Exception:
            log.exception("notification check failed")
        await asyncio.sleep(NOTIFY_INTERVAL_SECS)


async def _check_upcoming() -> None:
    if PING_CHANNEL == 0:
        return
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
    if not target_time or ":" not in target_time:
        return None
    try:
        hh, mm = target_time.split(":")[:2]
        now = _now_local()
        target = now.replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0
        )
    except ValueError:
        return None
    return (target - now).total_seconds() / 60


async def _check_events(conn: sqlite3.Connection) -> None:
    today = _now_local().date().isoformat()
    rows = conn.execute(
        "SELECT date, time, end_time, title, location, description FROM events "
        "WHERE date = ? AND time != '' AND time NOT IN ('00:00', '0:00')",
        (today,),
    ).fetchall()
    for r in rows:
        minutes_to_go = _minutes_until(r["time"], today)
        if minutes_to_go is None or minutes_to_go <= 0:
            continue
        # Per-event lead override from description ("lead: 2h", "lead:30m", etc.)
        lead_window = _parse_lead_min(r["description"], NOTIFY_LEAD_MIN)
        if minutes_to_go > lead_window:
            continue
        key = f"event:{r['date']}:{r['time']}:{r['title']}"
        if key in _notified:
            continue
        _notified.add(key)
        loc = f" @ {r['location']}" if r["location"] else ""
        end = f"–{r['end_time']}" if r["end_time"] else ""
        msg = (
            f"⏰ **{r['title']}** in {int(round(minutes_to_go))} min — "
            f"{r['time']}{end}{loc}"
        )
        if r["location"] and not _looks_like_url(r["location"]):
            maps_url = (
                "https://www.google.com/maps/search/?api=1&query="
                + quote_plus(r["location"])
            )
            # No angle brackets — let Discord render the embed/preview card.
            msg += f"\n🗺️ {maps_url}"
        await _send_ping(msg)
    _save_notified(_notified)


async def _check_reminders(conn: sqlite3.Connection) -> None:
    today = _now_local().date().isoformat()
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
            minutes_to_go = _minutes_until(hhmm, today)
            if minutes_to_go is None or minutes_to_go <= 0:
                continue
            lead_window = _parse_lead_min(text, NOTIFY_LEAD_MIN)
            if minutes_to_go > lead_window:
                continue
            clean = _HHMM_PREFIX_RE.sub("", text).strip() or "(reminder)"
            key = f"rem:{r['remind_on']}:{hhmm}:{clean}"
            if key in _notified:
                continue
            _notified.add(key)
            await _send_ping(
                f"🔔 **Reminder** in {int(round(minutes_to_go))} min — {clean}"
            )
        else:
            key = f"rem-allday:{r['remind_on']}:{text}"
            if key in _notified:
                continue
            _notified.add(key)
            await _send_ping(f"🔔 **Reminder today** — {text}")
    _save_notified(_notified)


# ── Scheduled morning briefing + evening wrap-up ────────────────────────────

async def _scheduled_briefings_loop() -> None:
    """Once a minute, check whether morning/evening briefing should fire.

    Uses the *active* timezone (the daily note's `timezone:` frontmatter or
    HOME_TZ) so e.g. 08:00 always means 08:00 in your current location.
    """
    global _last_morning_fired, _last_evening_fired
    await asyncio.sleep(10)
    while not client.is_closed():
        try:
            now = _now_local()
            now_hm = now.strftime("%H:%M")
            today = now.date().isoformat()
            if (NOTIFY_MORNING_AT != "off"
                    and now_hm >= NOTIFY_MORNING_AT
                    and _last_morning_fired != today):
                await _fire_morning_briefing()
                _last_morning_fired = today
            if (NOTIFY_EVENING_AT != "off"
                    and now_hm >= NOTIFY_EVENING_AT
                    and _last_evening_fired != today):
                await _fire_evening_wrapup()
                _last_evening_fired = today
        except Exception:
            log.exception("scheduled briefings loop")
        await asyncio.sleep(60)


async def _fire_morning_briefing() -> None:
    try:
        from _iris.tools.routines import morning_briefing
        text = await asyncio.to_thread(morning_briefing, "today")
    except Exception as e:
        log.warning("morning_briefing failed: %s", e)
        return
    await _send_ping(f"🌅 **Good morning!** Here's your day:\n\n{text}")


async def _fire_evening_wrapup() -> None:
    try:
        from _iris.tools.calendar import evening_wrapup
        text = await asyncio.to_thread(evening_wrapup, "today")
    except Exception as e:
        log.warning("evening_wrapup failed: %s", e)
        return
    await _send_ping(f"🌙 **Evening wrap-up** — \n\n{text}")


# ── Snooze: reactions on Iris's pings re-send after a delay ────────────────

async def _snooze_replay_loop() -> None:
    """Check the snooze list every 30 s; resend any items whose time has come."""
    await asyncio.sleep(15)
    while not client.is_closed():
        try:
            items = _load_snoozed()
            now = _now_local()
            still_pending: list[dict] = []
            for item in items:
                try:
                    resend_at = datetime.fromisoformat(item["resend_at"])
                    # Stored as naive ISO; pin to active TZ for comparison
                    if resend_at.tzinfo is None:
                        resend_at = resend_at.replace(tzinfo=now.tzinfo)
                except (KeyError, ValueError):
                    continue
                if now >= resend_at:
                    await _send_ping(
                        f"💤 (snoozed) {item.get('content', '(no content)')}"
                    )
                else:
                    still_pending.append(item)
            if len(still_pending) != len(items):
                _save_snoozed(still_pending)
        except Exception:
            log.exception("snooze replay")
        await asyncio.sleep(30)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    # Only act on reactions to messages WE sent in the ping channel
    if payload.channel_id != PING_CHANNEL:
        return
    if payload.user_id == (client.user.id if client.user else 0):
        return
    emoji = str(payload.emoji)
    minutes = SNOOZE_EMOJI_MINUTES.get(emoji)
    if minutes is None:
        return
    # Verify the message author is Iris
    try:
        channel = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)  # type: ignore[union-attr]
    except discord.HTTPException as e:
        log.warning("snooze: could not fetch message %s: %s", payload.message_id, e)
        return
    if message.author.id != (client.user.id if client.user else 0):
        return
    items = _load_snoozed()
    items.append({
        "resend_at": (_now_local() + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        "content": message.content,
        "snoozed_by": payload.user_id,
        "original_message_id": payload.message_id,
    })
    _save_snoozed(items)
    log.info("snoozed message %s for %s min (emoji=%s)",
             payload.message_id, minutes, emoji)
    try:
        await message.add_reaction("✅")  # ack
    except discord.HTTPException:
        pass


# ── Discord send helper (chunks at 2000-char limit) ────────────────────────

async def _send_ping(content: str) -> None:
    if PING_CHANNEL == 0:
        return
    channel = client.get_channel(PING_CHANNEL)
    if channel is None:
        try:
            channel = await client.fetch_channel(PING_CHANNEL)
        except discord.HTTPException as e:
            log.warning("could not fetch ping channel %s: %s", PING_CHANNEL, e)
            return
    # Split at 1900 chars (room for code-fence wrappers etc.) and split at line
    # boundaries when possible.
    LIMIT = 1900
    chunks: list[str] = []
    remaining = content
    while len(remaining) > LIMIT:
        cut = remaining.rfind("\n", 0, LIMIT)
        if cut <= 0:
            cut = LIMIT
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)

    for i, chunk in enumerate(chunks):
        try:
            await channel.send(chunk)  # type: ignore[union-attr]
        except discord.HTTPException as e:
            log.warning("ping send failed: %s", e)
            return
    log.info("ping → #%s (%s chunks): %s",
             getattr(channel, "name", PING_CHANNEL), len(chunks),
             content[:80].replace("\n", " "))


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
    # Log Iris's own messages to the per-channel history JSONL so the
    # fetch_discord_history MCP tool can include them. Then exit — Iris
    # doesn't respond to herself.
    is_iris_self = client.user and message.author.id == client.user.id
    if is_iris_self:
        await _log_channel_message(message)
        return
    # Other bots — ignore (don't log to keep noise out of Iris's view)
    if message.author.bot:
        return
    # Real human message — log first, then decide whether to respond.
    await _log_channel_message(message)

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

    # Save any attached files to the vault's inbox before invoking Iris.
    # She'll see the saved paths in her prompt and can read / route them
    # via the iris MCP tools (extract_pdf_text, extract_excalidraw_text,
    # import_drop_zone, move_files, etc.).
    saved_paths = await _save_attachments_to_inbox(message)
    if saved_paths:
        attachments_block = "\n\n[Files just saved to the vault inbox:\n" + \
            "\n".join(f"- {p}" for p in saved_paths) + \
            "\nLook at them and decide what to do — file the binaries via " \
            "`import_drop_zone`, or move them somewhere else if you've " \
            "already inferred where they belong.]"
        content = (content + attachments_block).strip() if content else \
                  "(no text — see attachments below)" + attachments_block

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
                agent = await _get_or_create_client(key, seed_message=message)
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
