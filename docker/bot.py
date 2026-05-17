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
import contextlib
import fcntl
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
# When True, after Iris finishes a streamed reply, the bot sends a tiny new
# message (default "✓") so Discord plays its normal new-message notification
# sound. The placeholder reply is edit-only during streaming, which Discord
# does NOT notify on — this is the only way to get a "done" ding. The little
# completion message auto-deletes after IRIS_DISCORD_COMPLETION_PING_TTL secs.
_completion_on = os.environ.get("IRIS_DISCORD_COMPLETION_PING", "on").strip().lower()
COMPLETION_PING_ENABLED = _completion_on in ("1", "on", "true", "yes")
COMPLETION_PING_EMOJI = os.environ.get("IRIS_DISCORD_COMPLETION_PING_EMOJI", "✓")
COMPLETION_PING_TTL = int(os.environ.get("IRIS_DISCORD_COMPLETION_PING_TTL", "4"))
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
NOTIFY_INTERVAL_SECS = int(os.environ.get("IRIS_NOTIFY_INTERVAL_SECS", "60"))
NOTIFY_LEAD_MIN = int(os.environ.get("IRIS_NOTIFY_LEAD_MIN", "15"))
NOTIFY_MORNING_AT = os.environ.get("IRIS_NOTIFY_MORNING_AT", "08:00").strip() or "off"
NOTIFY_EVENING_AT = os.environ.get("IRIS_NOTIFY_EVENING_AT", "22:00").strip() or "off"
# Catch-up grace window: if the bot restarts AFTER the scheduled time but
# within this many minutes of it, still fire the briefing (once). Past the
# grace window, suppress entirely — better than getting yesterday's morning
# brief at 3pm. Defaults: 3h morning (08:00 → fires up to 11:00), 1h evening
# (22:00 → fires up to 23:00). Tune via IRIS_NOTIFY_MORNING_GRACE_MIN /
# IRIS_NOTIFY_EVENING_GRACE_MIN.
NOTIFY_MORNING_GRACE_MIN = int(os.environ.get("IRIS_NOTIFY_MORNING_GRACE_MIN", "180"))
NOTIFY_EVENING_GRACE_MIN = int(os.environ.get("IRIS_NOTIFY_EVENING_GRACE_MIN", "60"))
# How often (in minutes) to pull from IRIS_DEFAULT_ICAL_URLS in the
# background. 0 = disabled (sync only happens via the morning brief or
# when explicitly asked). Default 60 = hourly. Lower to 15-30 if your
# calendars change frequently and you want event pings to catch
# last-minute additions; raise (or set 0) if your feeds are slow / you
# don't care about same-day freshness.
ICAL_SYNC_INTERVAL_MIN = int(os.environ.get("IRIS_ICAL_SYNC_INTERVAL_MIN", "60"))
# How often (in minutes) to snapshot the vault SQLite DB to a sync-safe
# copy. The live vault.db uses WAL mode (multiple files, mid-transaction
# states) which doesn't play well with syncthing replicating to read-only
# viewers on Mac/Windows. The snapshot is produced via `VACUUM INTO`
# (atomic, single file, committed-state-only) — safe to sync. Point your
# Obsidian SQLite-DB plugin on the read-only devices at vault-snapshot.db
# instead of vault.db. 0 = disabled (no snapshot file produced).
VAULT_SNAPSHOT_INTERVAL_MIN = int(os.environ.get("IRIS_VAULT_SNAPSHOT_INTERVAL_MIN", "10"))
# How often (in minutes) to re-render ```sqlite code blocks in vault notes
# into plain markdown tables. The Obsidian SQLite-DB plugin doesn't work
# on iOS/iPadOS — pre-rendering server-side makes the same data readable
# on every device. The injected markdown is wrapped in HTML comments so
# refreshes replace the previous output instead of duplicating it.
# Default 15 min. 0 = disabled (only refreshed when explicitly asked).
SQL_VIEW_REFRESH_MIN = int(os.environ.get("IRIS_SQL_VIEW_REFRESH_MIN", "15"))
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
# Loud diagnostic at import time — printed to stdout BEFORE logging is even
# configured, so it survives any log-level juggling. If you ever see
# `TZ resolved to UTC` here when you expected Zurich/Seoul/..., zoneinfo
# can't find your IANA name and that's the root cause of every "Iris thinks
# it's still yesterday" / "morning brief never fired" bug.
print(
    f"[iris.boot] TZ env={os.environ.get('TZ')!r}  "
    f"IRIS_TIMEZONE={os.environ.get('IRIS_TIMEZONE')!r}  "
    f"HOME_TZ_NAME={HOME_TZ_NAME!r}  resolved={HOME_TZ.key}",
    flush=True,
)
# Make sure libc / Python's `time.localtime()` (which `logging` uses for log
# timestamps and which `datetime.now()` without a tz uses) agrees with what
# we just resolved. Without this, a freshly-set TZ env var won't take effect
# in an already-running process, and you get log lines stamped in UTC while
# the bot internally thinks it's on Europe/Zurich — exactly the confusion
# Iris ran into when asked "what time is it?".
os.environ["TZ"] = HOME_TZ_NAME
try:
    time.tzset()
except AttributeError:
    pass  # Windows — no-op
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


def _now_context_block() -> str:
    """A short wall-clock context line to prepend to each user message.

    Claude only sees the system context's date (UTC-based) and has no clock,
    so when Hyun-Min asks "what time is it" or talks about "now" / "tonight"
    Claude would otherwise be a day behind in the evening local time. This
    anchors every turn to the active local timezone (which itself respects
    today's daily-note `timezone:` frontmatter override)."""
    now = _now_local()
    tz_name = now.tzinfo.key if hasattr(now.tzinfo, "key") else str(now.tzinfo)
    return f"[Now: {now.strftime('%Y-%m-%d %H:%M')} {tz_name} ({now.strftime('%A')})]"


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
        "Rich Discord embeds: for structured/visual output, PREFER the "
        "`embed_*` MCP tools over a wall-of-markdown reply. They render as "
        "Discord cards with a colored sidebar, fields, and footer.\n\n"
        "  Canned tools (DB-driven, deterministic):\n"
        "    - `embed_morning_brief(date)` — daily agenda card, blue.\n"
        "    - `embed_evening_wrapup(date)` — wrap-up card, indigo.\n"
        "    - `embed_daily_agenda(date, days)` — events+tasks+reminders for "
        "      a date or range, blue.\n"
        "    - `embed_event(date, title_match)` — single event with time/"
        "      location/map, yellow (red if imminent).\n"
        "    - `embed_project_status(path)` — project dashboard card, violet.\n"
        "    - `embed_note(path)` — show a vault note as a card (title, "
        "      excerpt, type/tags/mtime fields, footer = path). Use when "
        "      referencing a note in conversation rather than pasting its "
        "      raw markdown.\n"
        "    - `embed_callout(kind, title, body)` — semantic info box. "
        "      `kind` ∈ info / success / warning / error / tip / question. "
        "      Color + icon chosen for you. Great for short confirmations "
        "      ('✅ saved your ETH ceremony'), warnings ('⚠️ 3 broken links'), "
        "      tips, error reports.\n"
        "    - `embed_query(sql, title, color, mode)` — run a SELECT against "
        "      the vault DB and render the result as a card. `mode=\"table\"` "
        "      (default) → monospace code-block table; `mode=\"fields\"` → one "
        "      field per row. For ad-hoc dashboards like 'tasks per project', "
        "      'anime this season'. Same SQL-safety as `sqlite_query`.\n"
        "    - `embed_custom(title, description, fields, color)` — last "
        "      resort when no canned tool fits. `fields` is a list of "
        "      `{name, value, inline}` dicts (max 25). `color` accepts names "
        "      (blue/indigo/green/yellow/red/violet/gray/pink) or `#rrggbb`.\n\n"
        "  When to reach for an embed (non-tabular cases):\n"
        "    - Confirming a vault write you just did → `embed_callout(\"success\", ...)`.\n"
        "    - Reporting a warning or list of issues → `embed_callout(\"warning\", ...)`.\n"
        "    - Reporting an error you can't fix → `embed_callout(\"error\", ...)`.\n"
        "    - Surfacing or referencing a note → `embed_note(path)`.\n"
        "    - Presenting a recommendation or decision you'd like Hyun-Min to "
        "      make → `embed_custom(title=\"Pick one\", fields=[{name:option, "
        "      value:reasoning} ...])`.\n"
        "    - Anything that has clear sections / labelled values → "
        "      `embed_custom` with one field per section.\n"
        "    - Plain chitchat replies → just text, no embed.\n\n"
        "  Mechanics:\n"
        "    Embeds are queued and flushed at the END of your reply (before "
        "    the completion ping). So: call the tool, then add a short text "
        "    line like 'Here's today's agenda 👇' or '✅ done'. Don't ALSO "
        "    paste the same content as markdown — that's duplicate noise.\n\n"
        "  Clickable note links (IMPORTANT — use these freely):\n"
        "    Bot messages — INCLUDING your streamed text replies — render "
        "    masked markdown links `[label](url)`. To save you from "
        "    constructing `obsidian://open?vault=...&file=...` URLs by hand, "
        "    the bot has a renderer that auto-converts Obsidian wikilink "
        "    syntax into the right masked link. So:\n"
        "      - To reference a note anywhere — chat text, embed bodies, "
        "        field values — write `[[path/Note]]` or "
        "        `[[path/Note|Display Name]]`. The bot rewrites those to "
        "        clickable links automatically.\n"
        "      - NEVER paste a raw path like `60_Knowledge/Finance/Foo.md` "
        "        in prose — it'll show as plain text. Wrap it in `[[ ]]`.\n"
        "      - When you've just CREATED or UPDATED a specific note and "
        "        want to surface it visually, also call `embed_note(path)` "
        "        — that gives you a clickable card with title, excerpt, "
        "        type/tags/mtime fields, on top of the inline link.\n"
        "      - Tools `embed_note`, `embed_project_status`, and `embed_event` "
        "        already set the embed's `url` so the card's TITLE itself "
        "        is the click target.\n"
        "      - Inside `embed_callout` / `embed_custom` field values, "
        "        wikilink syntax works identically — go ahead and write "
        "        `Replaced the framework in [[60_Knowledge/Finance/Foo|Foo]]` "
        "        and trust the renderer.\n\n"
        "Discord markdown limits & how to handle them:\n\n"
        "  TABLES — Discord does NOT render `| col | col |` pipe tables; they "
        "show as literal pipes and look broken. NEVER use the `| pipe | "
        "syntax |`. You have two options for tabular data, depending on the "
        "shape — pick carefully, the wrong choice looks terrible:\n\n"
        "  → For SHORT, narrow, plain-ASCII tables (≤ 4 columns of short "
        "cells, ≤ 40 chars per row), use a fenced code block with manually "
        "aligned columns:\n"
        "```\n"
        "Tool             Status\n"
        "---------------  -----------------------------------------\n"
        "daily_agenda     wrong arg count\n"
        "semantic_search  Ollama unreachable\n"
        "weekly_summary   ok\n"
        "```\n"
        "  CRITICAL — do NOT put emoji inside a code-block table. Emoji are "
        "rendered with variable width and BREAK the monospace alignment, "
        "ruining the table. Plain ASCII only inside code blocks. Same goes "
        "for stars (⭐), check-marks (✅❌), arrows, etc. If you want to "
        "include emoji in tabular data, use the embed path below instead.\n\n"
        "  → For LONG-CELLED, MULTI-LINE, OPINIONATED, OR EMOJI-CONTAINING "
        "tables — rankings, comparisons, pros/cons lists, anything where a "
        "row has more than ~30 chars of free-form prose — DO NOT use a "
        "code block. Use `embed_custom` with one field per row instead:\n"
        "    title = 'Honest take: AI semis ranking'\n"
        "    fields = [\n"
        "      {name: '⭐⭐⭐⭐⭐  SK Hynix (HY9H)',\n"
        "       value: '**Why:** 6x fwd P/E + 72% margins + ...\\n"
        "               **Skip if:** you hate KRX/Frankfurt access'},\n"
        "      {name: '⭐⭐⭐⭐⭐  TSMC (TSM)',\n"
        "       value: '**Why:** Best fundamental biz in the stack ...\\n"
        "               **Skip if:** Taiwan stress keeps you up'},\n"
        "      ...\n"
        "    ]\n"
        "Discord embed fields handle multi-line values, emoji, and bold "
        "labels gracefully — they render as a proper card with each row in "
        "its own section. Use this whenever a row has rich content. Up to "
        "25 fields per embed.\n\n"
        "  HEADINGS — `#`/`##`/`###` work and render bigger; deeper levels "
        "fall back to plain bold.\n"
        "  IMAGES — no `![](url)` syntax; just paste the URL on its own line "
        "and Discord auto-embeds a preview.\n"
        "  WHAT WORKS — **bold**, *italic*, ~~strike~~, `inline`, "
        "```fenced code``` (with language tag for syntax highlighting), "
        "> blockquote, `- ` bullets (nest with 2-space indent), `1.` numbered "
        "lists, `[text](url)` links, `||spoiler||`.\n\n"
        "Wall-clock context: each user message is prefixed with a "
        "`[Now: YYYY-MM-DD HH:MM <IANA-tz> (<weekday>)]` line — that's the "
        "real local time on Hyun-Min's end at the moment he sent the message. "
        "Trust it over any date you might infer from your own system context "
        "(which is UTC-ish and will be a day behind in his evening). Use it "
        "when you reason about 'today', 'tonight', 'tomorrow', etc.\n\n"
        "Timezone convention: when Hyun-Min plans a trip to another "
        "timezone (e.g. Korea), set `timezone: <IANA name>` (e.g. "
        "`Asia/Seoul`) in the frontmatter of each daily note for the "
        "travel days, using `set_frontmatter_field`. The Discord bot "
        "reads this and shifts the morning briefing and evening wrap-up "
        "to fire at 08:00 / 22:00 *local* time wherever he is.\n\n"
        "Vocab quiz flow (fast, self-graded): when Hyun-Min says 'let's do "
        "vocab' / 'quiz me' / 'review' / similar, run a session like this — "
        "NOT one-tool-call-per-answer ping-pong:\n"
        "  1. Call `vocab_due(language, limit=20)` ONCE at the start. The "
        "     result has each card's word + reading + meaning. Keep that "
        "     list in your working memory for the whole session.\n"
        "  2. Pick the next card. Show ONLY the prompt side (e.g. the word "
        "     in Korean/Japanese script). Hide the reading + meaning.\n"
        "  3. Wait for Hyun-Min's answer in chat.\n"
        "  4. Compare his answer to the stored meaning yourself. Decide:\n"
        "       - **✅ correct** — exact match, valid synonym, or trivial "
        "         typo. Even partial-but-clearly-right counts.\n"
        "       - **❓ close** — right idea but missing nuance, wrong "
        "         particle/form, or one of multiple meanings.\n"
        "       - **❌ wrong** — no match, blank, or 'idk'.\n"
        "  5. Call `vocab_review(language, word, grade=\"correct\"|\"close\""
        "     |\"wrong\")` — the string form is fine, don't bother with the "
        "     0-5 SM-2 number.\n"
        "  6. Reply with: the emoji verdict on its own line (✅ / ❓ / ❌), "
        "     then the correct answer if not perfect, then immediately the "
        "     next prompt. Example:\n"
        "       ✅\n"
        "       Next: **사과**\n"
        "     or\n"
        "       ❓ — close, full meaning: *to be tired (verb 피곤하다)*\n"
        "       Next: **학교**\n"
        "  7. Continue until the cached list is empty, then call "
        "     `vocab_due` again or wrap up with a quick `vocab_review_stats` "
        "     summary.\n"
        "Don't ask 'should I grade that as correct?' — judge it yourself "
        "and move on. Speed is the whole point. If Hyun-Min disagrees with "
        "your grade he'll say so and you can re-call `vocab_review` with a "
        "different grade.\n\n"
        "SQL views in notes: notes can contain ```sqlite (or ```sql) code "
        "blocks with queries like `SELECT title FROM notes WHERE type='project'`. "
        "Desktop Obsidian renders these via the SQLite-DB plugin, but iOS "
        "and iPadOS can't run that plugin. The bot auto-refreshes these "
        "every 15 min: `refresh_sql_views(all_notes=True)` walks the vault, "
        "runs each query, and injects a plain markdown table beneath the "
        "code block wrapped in `<!-- iris-sql-result ... -->` comments so "
        "mobile devices see the same data. If Hyun-Min asks you to refresh "
        "a specific note's SQL views NOW, call `refresh_sql_views(path=...)`. "
        "If he asks 'refresh all views', call it with `all_notes=True`. "
        "Result blocks are SELECT-only, capped at 50 rows + 200 chars/cell. "
        "Use `list_sql_views(note_path?)` to inspect what's tracked — the "
        "vault index keeps a `sql_views` table populated every time a note "
        "is re-scanned, so 'which notes have SQL views?' is one tool call.\n\n"
        "Read-only DB snapshot (companion to the SQL view feature): the "
        "live vault.db uses WAL mode which can't safely sync via syncthing. "
        "The bot snapshots it to `vault-snapshot.db` every 10 min for "
        "Mac/Windows SQLite-DB plugin readers. To take a snapshot NOW (e.g. "
        "after a bulk import + before the user opens the plugin on the "
        "Mac), call `vault_snapshot()`. Returns a tiny ok/err line.\n\n"
        "Calendar sync from external feeds: when Hyun-Min says 'sync my "
        "calendar', 'pull events from iCloud', 'import my work calendar':\n"
        " - If `IRIS_DEFAULT_ICAL_URLS` is set (multi-feed config in .env), "
        "   use `sync_all_calendars(days_ahead, days_back)` — it walks "
        "   every configured feed and applies the per-feed source tag and "
        "   optional person-link automatically.\n"
        " - NOTE: `embed_morning_brief` auto-syncs ALL configured feeds "
        "   before building the card (controlled by `sync_calendars=True`, "
        "   the default). The proactive 08:00 morning brief does the same. "
        "   So you don't need to manually call `sync_all_calendars` right "
        "   before generating a morning brief — it's already covered.\n"
        " - Otherwise (one-off URL the user just gave you), use "
        "   `pull_ical_subscription(url, days_ahead, days_back, "
        "   source_tag, link_to_person, cross_calendar_dedupe)`. "
        "   `webcal://` is auto-rewritten.\n"
        "Re-syncing is safe — there's a two-layer dedupe (by iCal UID for "
        "same-calendar re-imports, by `(date, time, title)` for the SAME "
        "event appearing in multiple calendars with different UIDs). "
        "Recurring events (RRULEs) expand into individual occurrences in "
        "the window. When syncing a person-specific shared calendar (e.g. "
        "a partner's iCloud), pass `link_to_person=\"10_Profile/People/"
        "Name\"` — each event gets a `with: [[…]]` backlink so you can "
        "later answer 'what's coming up with Marimo?' by querying the "
        "vault. After importing, confirm with `embed_callout('success', "
        "'Calendar synced', body)`.\n\n"
        "Precise-time pingbacks: when Hyun-Min says 'ping me at 00:30', "
        "'remind me in 15 minutes', 'message me at 14:00 tomorrow' — use "
        "the `schedule_pingback(when, message)` tool, NOT `add_reminder`. "
        "`add_reminder` is date-granular and fires within a lead window; "
        "`schedule_pingback` fires at the exact minute via a 30-second poll "
        "loop in the bot. Accepts `HH:MM`, `+15m`, `+2h`, or ISO 8601. "
        "If you have a `[Now: ...]` line on the message, use it as ground "
        "truth for resolving relative times — don't guess from your "
        "internal date sense. Use `list_pingbacks` / `cancel_pingback` to "
        "inspect or cancel pending ones.\n\n"
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
        "Updating an existing note — append vs edit-in-place (IMPORTANT):\n"
        "When Hyun-Min asks you to add information to a note, your default "
        "should NOT be `append_to_note` — that tool only makes sense for "
        "chronological / log-style notes. For everything else, find the "
        "right section and update it in place. Concrete rules:\n\n"
        "  Append-to-end is correct only for:\n"
        "    - Daily notes (`30_Episodic/YYYY/YYYY-MM-DD.md`).\n"
        "    - Weekly notes (`30_Episodic/YYYY/Weekly/...`).\n"
        "    - Stream-of-consciousness logs / journals where order = time.\n\n"
        "  For knowledge / reference / project / research notes:\n"
        "    1. `read_note(path)` first to see the existing structure.\n"
        "    2. Decide where the new info belongs in that structure:\n"
        "        - Refining an existing section's data (e.g. updating "
        "          numbers in a paragraph, adding rows to an existing "
        "          table, replacing an outdated table with the new one) → "
        "          `update_section(path, heading, new_body, mode=\"replace\")`.\n"
        "        - Adding a new paragraph to a section that's already there → "
        "          `update_section(path, heading, new_body, mode=\"append\")`.\n"
        "        - Adding a genuinely new top-level section that belongs in "
        "          the middle (e.g. a new analysis between two existing "
        "          sections) → `read_note`, edit in memory at the right "
        "          location, `write_note` with `overwrite=True`.\n"
        "        - Replacing a specific verbatim string → "
        "          `replace_in_vault_text_file`.\n"
        "    3. NEVER produce duplicate sections. If the note already has "
        "       `## Final Ranking` and Hyun-Min asks for an updated ranking, "
        "       REPLACE the existing section's body — don't add `## Updated "
        "       Ranking` further down. Two versions of the same thing is "
        "       worse than one good version.\n"
        "    4. Tables specifically: locate the existing table in the note, "
        "       use `update_section` with `mode=\"replace\"` to swap it for "
        "       the refined version. Don't write a parallel \"updated\" "
        "       table elsewhere.\n"
        "    5. `## Related Notes` is ALWAYS the terminal section. Never "
        "       add content AFTER it. New facts, tickets, decisions, etc. "
        "       belong in an existing section higher up (Details, Logistics, "
        "       Notes, …) or as a new section inserted BEFORE Related Notes "
        "       — not appended at the very end of the file. Same rule "
        "       applies to `## Sources` and `## Tasks` if they are the "
        "       structural footer of a note.\n"
        "    6. New facts about a person → integrate into the existing "
        "       `## Details` / `## Notes` / `## Background` section of "
        "       their profile via `update_section(mode=\"append\")`. Don't "
        "       create a new `## Facts` block at the bottom.\n\n"
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
    # If a message has no text content BUT has embeds (e.g. one of Iris's
    # proactive ping cards), synthesise a content line from the embed so
    # fetch_discord_history can surface it later. Without this, every
    # embed-based message is invisible to the history tool.
    if not content.strip():
        if not message.embeds:
            return
        e0 = message.embeds[0]
        synth_parts: list[str] = []
        if e0.title:
            synth_parts.append(str(e0.title))
        if e0.description:
            synth_parts.append(str(e0.description))
        for f in (e0.fields or [])[:3]:
            synth_parts.append(f"{f.name}: {f.value}")
        content = " · ".join(p.strip() for p in synth_parts if p)
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
        "is_proactive": _is_proactive_ping(content.strip(), is_iris)
                        or bool(message.embeds and is_iris),
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
        # Rewrite Obsidian wikilinks `[[path|alias]]` into clickable masked
        # links `[alias](obsidian://...)`. Discord renders masked markdown
        # links in BOT messages (not user messages), so they're clickable
        # inline. The regex only matches complete `[[…]]` pairs, so partial
        # links in mid-stream content stay untouched until they're finished.
        content = strip_wikilinks(self._buffer)
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


# Background-task pinboard. asyncio.create_task() only keeps a WEAK reference,
# so fire-and-forget tasks can be garbage-collected mid-execution if we don't
# pin them somewhere. We add to this set on launch and remove on completion.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> asyncio.Task:
    """Spawn a background task that won't get GC'd before completion.
    Logs unhandled exceptions instead of swallowing them silently."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.warning("background task raised: %r", exc)

    task.add_done_callback(_on_done)
    return task


async def _completion_ping(channel: discord.abc.Messageable) -> None:
    """Send a tiny new message so Discord plays its new-message notification
    sound, then auto-delete after TTL. Discord doesn't notify on edits, so
    this is the only way to ding the user when a streamed reply completes.
    Failures are logged but never raised."""
    try:
        msg = await channel.send(COMPLETION_PING_EMOJI)
        log.info("completion ping sent to channel %s", getattr(channel, "id", "?"))
    except discord.HTTPException as e:
        log.warning("completion ping send failed: %s", e)
        return
    if COMPLETION_PING_TTL <= 0:
        return  # leave the marker permanently if TTL is 0/negative
    await asyncio.sleep(COMPLETION_PING_TTL)
    try:
        await msg.delete()
    except discord.HTTPException:
        pass  # already gone, channel locked, etc. — not worth complaining


# ── Per-channel session memory ───────────────────────────────────────────────
# We keep one ClaudeSDKClient per channel/thread so conversations persist.

_sessions: dict[int, ClaudeSDKClient] = {}
_session_locks: dict[int, asyncio.Lock] = {}
# Per-channel queue-depth counter (including the one currently being
# processed). Used so we can show a "queued" reaction on the user's message
# when their turn isn't right now, and cap runaway spam.
_session_pending: dict[int, int] = {}
# Hard cap on how many messages can stack up per channel before we start
# rejecting new ones. Beyond this Iris would be hopelessly behind and the
# user is probably mashing the keyboard. Reject loudly, don't silently lose.
MAX_QUEUE_DEPTH = int(os.environ.get("IRIS_DISCORD_MAX_QUEUE_DEPTH", "5"))


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
    # Visibility: surface the resolved home timezone and what `now` currently
    # looks like. If HOME_TZ silently fell back to UTC because the IANA name
    # was unknown, this is the line that'll make it obvious.
    if HOME_TZ.key != HOME_TZ_NAME:
        log.warning(
            "TZ env was %r but resolved to %s — tzdata may be missing or the name is invalid",
            HOME_TZ_NAME, HOME_TZ.key,
        )
    log.info("home TZ: %s — now=%s", HOME_TZ.key, _now_local().isoformat(timespec="seconds"))
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
            log.info("morning briefing at %s daily (grace %d min, active TZ)",
                     NOTIFY_MORNING_AT, NOTIFY_MORNING_GRACE_MIN)
        if NOTIFY_EVENING_AT != "off":
            log.info("evening wrap-up at %s daily (grace %d min, active TZ)",
                     NOTIFY_EVENING_AT, NOTIFY_EVENING_GRACE_MIN)
        client.loop.create_task(_notification_loop())
        client.loop.create_task(_scheduled_briefings_loop())
        client.loop.create_task(_snooze_replay_loop())
        client.loop.create_task(_pingback_loop())
        client.loop.create_task(_embed_queue_loop())
        client.loop.create_task(_ical_sync_loop())
        client.loop.create_task(_vault_snapshot_loop())
        client.loop.create_task(_sql_view_refresh_loop())


# ── Proactive notification loop ─────────────────────────────────────────────
# Persisted dedupe — JSON file under /claude-auth so restarts don't re-ping
# events/reminders we already sent.

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # Don't silently swallow — corruption in _notified.json would cause
        # every today-event to re-ping on the next scan, and the symptom
        # ("Iris pinged me 12 times") would be miles from the root cause.
        log.warning("corrupt JSON at %s (%s) — resetting to default", path, e)
        return default


def _load_notified() -> dict[str, None]:
    """Load the dedupe set as an order-preserving dict (key→None).

    Backed by dict because Python guarantees insertion order, so FIFO
    eviction in ``_save_notified`` actually keeps the *most recent* 1000
    entries — vs the previous ``set`` whose iteration order was unspecified,
    which could evict a just-added key and re-ping the same event."""
    keys = _load_json(NOTIFIED_PATH, {}).get("keys", [])
    return dict.fromkeys(keys)


def _save_notified(keys: dict[str, None]) -> None:
    """Persist the dedupe state. Trims to the most recent 1000 entries by
    insertion order (since ``keys`` is a dict, not a set)."""
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Order-preserving — last 1000 keys inserted survive.
    recent = list(keys.keys())[-1000:]
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


# Order-preserving dedupe map (key → None). Dict, not set, so trimming to
# 1000 entries in _save_notified keeps the most-recent insertions and doesn't
# silently evict a key we just added.
_notified: dict[str, None] = {}
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


def _minutes_past_target(target_time: str, now: datetime) -> float | None:
    """Minutes elapsed since ``HH:MM`` today (negative if still in the future).

    Returns None if ``target_time`` is malformed.
    """
    if not target_time or ":" not in target_time:
        return None
    try:
        hh, mm = target_time.split(":")[:2]
        target = now.replace(hour=int(hh), minute=int(mm),
                             second=0, microsecond=0)
    except ValueError:
        return None
    return (now - target).total_seconds() / 60


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
        # Persist the key BEFORE awaiting the fire — at-most-once semantics.
        # If the fire crashes we accept losing that ping; better than the
        # alternative where a mid-fire crash leaves the in-memory set holding
        # a key that never made it to disk, so the next scan re-pings.
        _notified[key] = None
        _save_notified(_notified)
        await _fire_event_embed(r, int(round(minutes_to_go)))


async def _fire_event_embed(row, minutes_to_go: int) -> None:
    """Build + send a rich embed for an upcoming event ping. Color shifts
    red as the event gets closer, yellow further out."""
    if PING_CHANNEL == 0:
        return
    color = COLOR_RED if minutes_to_go <= 15 else COLOR_YELLOW
    end = row["end_time"] or ""
    when_line = f"{row['time']}" + (f"–{end}" if end else "")
    fields: list[dict] = [
        {"name": "🕐 In", "value": f"**{minutes_to_go} min** ({when_line})", "inline": True},
    ]
    if row["location"]:
        fields.append({"name": "📍 Where", "value": row["location"], "inline": True})
        if not _looks_like_url(row["location"]):
            maps_url = (
                "https://www.google.com/maps/search/?api=1&query="
                + quote_plus(row["location"])
            )
            fields.append({"name": "🗺️ Map", "value": maps_url, "inline": False})
    if row["description"]:
        desc = row["description"]
        if len(desc) > 1020:
            desc = desc[:1017] + "…"
        fields.append({"name": "📝 Notes", "value": desc, "inline": False})
    embed = {
        "title": f"⏰ {row['title']}",
        "color": color,
        "fields": fields,
        "footer": f"event · {row['date']}",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    await _send_embed_payload(PING_CHANNEL, embed)


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
            _notified[key] = None
            _save_notified(_notified)
            await _fire_reminder_embed(
                clean, int(round(minutes_to_go)),
                at_time=hhmm, note_path=r["note_path"],
            )
        else:
            key = f"rem-allday:{r['remind_on']}:{text}"
            if key in _notified:
                continue
            _notified[key] = None
            _save_notified(_notified)
            await _fire_reminder_embed(
                text, minutes_to_go=None, at_time=None,
                note_path=r["note_path"],
            )


async def _replay_snoozed(item: dict) -> None:
    """Resend a snoozed item. Preserves the original embed (if any) and
    prefixes the title with 💤 so it's visually marked as a replay."""
    if PING_CHANNEL == 0:
        return
    embed_dict = item.get("embed")
    content = item.get("content") or ""
    if embed_dict:
        # Annotate the title so the user sees this is a snoozed replay.
        orig_title = embed_dict.get("title") or ""
        if not orig_title.startswith("💤"):
            embed_dict = dict(embed_dict)
            embed_dict["title"] = f"💤 {orig_title}".strip()[:256]
        await _send_embed_payload(PING_CHANNEL, embed_dict)
    elif content:
        await _send_ping(f"💤 (snoozed) {content}")
    else:
        await _send_ping("💤 (snoozed reminder)")


async def _fire_reminder_embed(
    text: str,
    minutes_to_go: int | None,
    at_time: str | None,
    note_path: str | None,
) -> None:
    if PING_CHANNEL == 0:
        return
    if minutes_to_go is not None and at_time:
        title = f"🔔 Reminder in {minutes_to_go} min"
        fields = [
            {"name": "🕐 At", "value": at_time, "inline": True},
            {"name": "📝 What", "value": text or "(no text)", "inline": False},
        ]
        color = COLOR_RED if minutes_to_go <= 15 else COLOR_PINK
    else:
        title = "🔔 Reminder today"
        fields = [
            {"name": "📝 What", "value": text or "(no text)", "inline": False},
        ]
        color = COLOR_PINK
    if note_path:
        fields.append({"name": "🔗 Source", "value": f"`{note_path}`", "inline": False})
    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": "reminder",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    await _send_embed_payload(PING_CHANNEL, embed)


# ── Scheduled morning briefing + evening wrap-up ────────────────────────────

async def _scheduled_briefings_loop() -> None:
    """Once a minute, check whether morning/evening briefing should fire.

    Uses the *active* timezone (the daily note's `timezone:` frontmatter or
    HOME_TZ) so e.g. 08:00 always means 08:00 in your current location.
    """
    global _last_morning_fired, _last_evening_fired
    await asyncio.sleep(10)
    # On startup: if a briefing's scheduled time has already passed today AND
    # we're past the grace window, mark it as "already fired" so we don't
    # replay the morning brief at 3pm. Within the grace window, leave the
    # state untouched — the main loop will fire it on the next tick.
    try:
        startup_now = _now_local()
        startup_today = startup_now.date().isoformat()
        for label, at, grace, last_var in (
            ("morning", NOTIFY_MORNING_AT, NOTIFY_MORNING_GRACE_MIN, "_last_morning_fired"),
            ("evening", NOTIFY_EVENING_AT, NOTIFY_EVENING_GRACE_MIN, "_last_evening_fired"),
        ):
            if at == "off":
                continue
            past_min = _minutes_past_target(at, startup_now)
            if past_min is None or past_min < 0:
                # Hasn't happened yet today — normal scheduled flow handles it.
                continue
            if past_min <= grace:
                log.info(
                    "%s briefing within grace window (%.0f min past %s, grace=%d) "
                    "— will fire on next tick",
                    label, past_min, at, grace,
                )
                # Don't touch _last_*_fired — main loop will trigger it.
            else:
                if last_var == "_last_morning_fired":
                    _last_morning_fired = startup_today
                else:
                    _last_evening_fired = startup_today
                log.info(
                    "%s briefing missed (%.0f min past %s > grace %d) — suppressed for today",
                    label, past_min, at, grace,
                )
    except Exception:
        log.exception("scheduled briefings startup-suppress")
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
    if PING_CHANNEL == 0:
        return
    # Auto-sync external calendars FIRST so today's freshly-added iCloud /
    # work / shared-with-partner events flow into the brief. Only runs when
    # IRIS_DEFAULT_ICAL_URLS is configured; otherwise this is a no-op.
    # We log the result but don't include it in the brief embed — keeps the
    # card clean. A 7-day window is enough for what's-on-today + upcoming.
    if os.environ.get("IRIS_DEFAULT_ICAL_URLS", "").strip():
        try:
            from _iris.tools.calendar import sync_all_calendars
            sync_result = await asyncio.to_thread(
                sync_all_calendars, days_ahead=7, days_back=0, dry_run=False,
            )
            # Trim noisy multi-line output to a single info line per feed.
            head = sync_result.splitlines()[0] if sync_result else ""
            log.info("pre-brief iCal sync: %s", head)
        except Exception as e:
            log.warning("pre-brief iCal sync failed: %s", e)
    try:
        from _iris.tools.routines import morning_briefing
        text = await asyncio.to_thread(morning_briefing, "today")
    except Exception as e:
        log.warning("morning_briefing failed: %s", e)
        return
    title, intro, fields = _parse_md_sections(text)
    if not title or not title.startswith(("🌅", "Good", "Briefing")):
        title = f"🌅 {title or 'Morning brief'}"
    embed = {
        "title": title[:256],
        "description": intro[:4096] if intro else None,
        "color": COLOR_BLUE,
        "fields": fields,
        "footer": "morning_briefing",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    await _send_embed_payload(PING_CHANNEL, embed)


async def _fire_evening_wrapup() -> None:
    if PING_CHANNEL == 0:
        return
    try:
        from _iris.tools.calendar import evening_wrapup
        text = await asyncio.to_thread(evening_wrapup, "today")
    except Exception as e:
        log.warning("evening_wrapup failed: %s", e)
        return
    title, intro, fields = _parse_md_sections(text)
    if not title or not title.startswith(("🌙", "Evening", "Wrap")):
        title = f"🌙 {title or 'Evening wrap-up'}"
    embed = {
        "title": title[:256],
        "description": intro[:4096] if intro else None,
        "color": COLOR_INDIGO,
        "fields": fields,
        "footer": "evening_wrapup",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    await _send_embed_payload(PING_CHANNEL, embed)


# ── Embed builders + queue (rich Discord embeds for pings + tool output) ───
# Two paths feed into _send_embed_payload:
#   1. Proactive (this process): _fire_morning_briefing etc. build a payload
#      directly and skip the queue.
#   2. MCP-tool-driven (Iris's session subprocess): the embed_* MCP tools
#      write a JSON line to the queue file; this loop polls + sends.
# Same builder is used by both so the visual stays identical.


@contextlib.contextmanager
def _flocked(path: Path):
    """Exclusive file lock on ``path`` (creates if missing). Used to
    serialise the read-then-rewrite of queue files between the bot process
    and any MCP subprocess that's appending. Without this, an append between
    our read and our rewrite-with-empty disappears silently — losing the
    embed/pingback entry. ``fcntl.flock`` is advisory; both sides have to
    take the lock for it to help. The MCP-side ``_enqueue_embed`` / pingback
    writer use the same helper (see ``_iris/tools/discord.py``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

# Pure embed helpers live in bot_embeds.py — colors, section parser, the
# dict→discord.Embed builder. Anything that needs the live `client` (sending,
# queue drain, queue loop) stays below in this file.
from bot_embeds import (
    COLOR_BLUE,
    COLOR_INDIGO,
    COLOR_GREEN,
    COLOR_YELLOW,
    COLOR_RED,
    COLOR_VIOLET,
    COLOR_GRAY,
    COLOR_PINK,
    parse_md_sections as _parse_md_sections,
    dict_to_embed as _dict_to_embed,
    embed_queue_path as _embed_queue_path,
    strip_wikilinks,
)


_EMBED_QUEUE = _embed_queue_path()


async def _send_embed_payload(
    channel_id: int,
    embed_dict: dict,
    content: str = "",
) -> None:
    """Build a discord.Embed and send it. Used by both proactive + queue paths."""
    if not channel_id:
        return
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException as e:
            log.warning("embed: could not fetch channel %s: %s", channel_id, e)
            return
    try:
        embed = _dict_to_embed(embed_dict)
        await channel.send(content=content or None, embed=embed)  # type: ignore[union-attr]
    except discord.HTTPException as e:
        log.warning("embed: send failed: %s", e)


async def _embed_queue_loop() -> None:
    """Poll the embed queue every ~1 s. Faster than pingbacks because users
    are usually waiting on these (they're triggered mid-conversation)."""
    await asyncio.sleep(5)
    while not client.is_closed():
        try:
            if _EMBED_QUEUE.exists():
                await _drain_embed_queue()
        except Exception:
            log.exception("embed queue loop")
        await asyncio.sleep(1)


async def _drain_embed_queue() -> int:
    """Read every entry, send each, rewrite empty. Returns count sent.

    The read-then-rewrite is guarded by an advisory file lock so an MCP
    subprocess appending mid-drain doesn't get its line silently wiped out
    when we rewrite-with-empty. The lock is released before we await on
    Discord HTTP so we don't block tool calls during a slow send.
    """
    entries: list[dict] = []
    try:
        with _flocked(_EMBED_QUEUE):
            if not _EMBED_QUEUE.exists():
                return 0
            try:
                raw_lines = _EMBED_QUEUE.read_text(encoding="utf-8").splitlines()
            except OSError as e:
                log.warning("embed: queue read failed: %s", e)
                return 0
            for line in raw_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if not entries:
                return 0
            # Rewrite empty INSIDE the lock so any subprocess appending after
            # our read but before the truncate has to wait (its append goes
            # into a fresh file after we release).
            try:
                tmp = _EMBED_QUEUE.with_suffix(".jsonl.tmp")
                tmp.write_text("", encoding="utf-8")
                tmp.replace(_EMBED_QUEUE)
            except OSError as e:
                log.warning("embed: queue rewrite failed: %s", e)
                return 0
    except OSError as e:
        log.warning("embed: queue lock failed: %s", e)
        return 0
    sent = 0
    for entry in entries:
        try:
            await _send_embed_payload(
                int(entry.get("channel_id") or 0),
                entry.get("embed") or {},
                entry.get("content") or "",
            )
            sent += 1
            log.info("embed fired: id=%s → #%s", entry.get("id"), entry.get("channel_id"))
        except Exception:
            log.exception("embed: send failed for %s", entry.get("id"))
    return sent


# ── Precise-time pingbacks (queued via the MCP schedule_pingback tool) ─────
# Iris writes one JSON line per scheduled ping into this file; the loop below
# fires anything whose `at` is due and rewrites the file without those entries.
# Lives next to the per-channel history logs so it shares the persistent volume.

_PINGBACK_QUEUE = HISTORY_DIR.parent / "pending_pings.jsonl"


async def _pingback_loop() -> None:
    """Every ~30 s: read pending pingbacks, fire due ones, drop them from the file.

    The queue file is the canonical state — surviving restarts since it's on
    the /claude-auth volume. We treat I/O failures as transient and retry on
    the next tick.
    """
    await asyncio.sleep(8)
    while not client.is_closed():
        try:
            if _PINGBACK_QUEUE.exists():
                await _process_pingback_queue()
        except Exception:
            log.exception("pingback loop")
        await asyncio.sleep(30)


async def _ical_sync_loop() -> None:
    """Periodic background sync of every feed in IRIS_DEFAULT_ICAL_URLS.

    Sleeps ICAL_SYNC_INTERVAL_MIN between passes (default 60 min). Skipped
    entirely when the interval is 0 OR no feeds are configured. First sync
    happens ~2 min after startup so we don't pile work onto the cold-start
    window — the morning brief's pre-sync covers immediate freshness anyway.
    """
    if ICAL_SYNC_INTERVAL_MIN <= 0:
        log.info("iCal background sync disabled (IRIS_ICAL_SYNC_INTERVAL_MIN=0)")
        return
    if not os.environ.get("IRIS_DEFAULT_ICAL_URLS", "").strip():
        log.info("iCal background sync skipped — IRIS_DEFAULT_ICAL_URLS not set")
        return
    log.info("iCal background sync: every %d min", ICAL_SYNC_INTERVAL_MIN)
    # Initial delay so we don't race the bot's other startup work + the
    # 08:00 brief's pre-sync (which already pulls fresh).
    await asyncio.sleep(120)
    while not client.is_closed():
        try:
            from _iris.tools.calendar import sync_all_calendars
            result = await asyncio.to_thread(
                sync_all_calendars, days_ahead=30, days_back=0, dry_run=False,
            )
            # Just log the header line per feed — drop the verbose preview.
            for line in (result or "").splitlines():
                if line.startswith("📅") or line.startswith("──"):
                    log.info("ical-sync: %s", line)
        except Exception as e:
            log.warning("background iCal sync failed: %s", e)
        await asyncio.sleep(ICAL_SYNC_INTERVAL_MIN * 60)


async def _vault_snapshot_loop() -> None:
    """Periodically VACUUM INTO a sync-safe snapshot of the vault SQLite DB.

    The live vault.db uses WAL mode → three coordinated files (.db / .db-wal
    / .db-shm) that aren't safe to replicate via syncthing as the writer
    process can be mid-transaction at any moment. `VACUUM INTO` produces an
    atomic single-file snapshot of the committed state, which IS safe to
    sync. Read-only viewers (Obsidian SQLite-DB plugin on Mac / Windows)
    point at vault-snapshot.db instead of vault.db and get consistent reads.

    The snapshot is built in-place via a .tmp file + atomic rename so even
    a mid-VACUUM crash leaves the previous snapshot intact for readers.
    """
    if VAULT_SNAPSHOT_INTERVAL_MIN <= 0:
        log.info("vault snapshot disabled (IRIS_VAULT_SNAPSHOT_INTERVAL_MIN=0)")
        return
    log.info("vault snapshot: every %d min", VAULT_SNAPSHOT_INTERVAL_MIN)
    # Initial delay so we don't run before VaultIndex's first sync settles.
    await asyncio.sleep(90)
    while not client.is_closed():
        try:
            await asyncio.to_thread(_take_vault_snapshot)
        except Exception as e:
            log.warning("vault snapshot failed: %s", e)
        await asyncio.sleep(VAULT_SNAPSHOT_INTERVAL_MIN * 60)


def _take_vault_snapshot() -> None:
    """Thin wrapper around the `vault_snapshot` MCP tool's implementation.

    Keeping a single source of truth for the snapshot logic (in
    ``_iris/tools/sqlite.py``) means the periodic loop and Iris's
    on-demand calls produce byte-identical output, and any future bug
    fix only needs one edit.
    """
    from _iris.tools.sqlite import vault_snapshot as _vault_snapshot_impl
    result = _vault_snapshot_impl()
    log.info("vault snapshot: %s", result)


async def _sql_view_refresh_loop() -> None:
    """Periodic re-rendering of ```sqlite code blocks across the vault.

    Lets iOS / iPadOS Obsidian (which can't run the SQLite-DB plugin)
    still read the same SQL views — Iris renders them server-side into
    plain markdown tables wrapped in HTML comments. Re-runs are
    idempotent thanks to the wrapper.

    First pass runs ~3 min after startup so we don't compete with the
    vault snapshot loop and other cold-start work. Set
    IRIS_SQL_VIEW_REFRESH_MIN=0 to disable; defaults to 15 min.
    """
    if SQL_VIEW_REFRESH_MIN <= 0:
        log.info("SQL view refresh disabled (IRIS_SQL_VIEW_REFRESH_MIN=0)")
        return
    log.info("SQL view refresh: every %d min", SQL_VIEW_REFRESH_MIN)
    await asyncio.sleep(180)
    while not client.is_closed():
        try:
            from _iris.tools.sqlite import refresh_sql_views
            result = await asyncio.to_thread(
                refresh_sql_views, path="", all_notes=True,
            )
            head = result.splitlines()[0] if result else ""
            log.info("sql-view-refresh: %s", head)
        except Exception as e:
            log.warning("background SQL view refresh failed: %s", e)
        await asyncio.sleep(SQL_VIEW_REFRESH_MIN * 60)


async def _process_pingback_queue() -> None:
    # Guarded read-then-rewrite (see _drain_embed_queue rationale).
    due: list[dict] = []
    try:
        with _flocked(_PINGBACK_QUEUE):
            if not _PINGBACK_QUEUE.exists():
                return
            try:
                raw_lines = _PINGBACK_QUEUE.read_text(encoding="utf-8").splitlines()
            except OSError as e:
                log.warning("pingback: queue read failed: %s", e)
                return
            now = datetime.now(timezone.utc)
            keep: list[str] = []
            for line in raw_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    at = datetime.fromisoformat(entry["at"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue  # corrupt line, drop it
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                if at <= now:
                    due.append(entry)
                else:
                    keep.append(line)
            if not due:
                return
            try:
                tmp = _PINGBACK_QUEUE.with_suffix(".jsonl.tmp")
                tmp.write_text(("\n".join(keep) + "\n") if keep else "", encoding="utf-8")
                tmp.replace(_PINGBACK_QUEUE)
            except OSError as e:
                log.warning("pingback: queue rewrite failed: %s", e)
                return
    except OSError as e:
        log.warning("pingback: queue lock failed: %s", e)
        return
    for entry in due:
        try:
            await _send_pingback(entry)
        except Exception:
            log.exception("pingback: send failed for %s", entry.get("id"))


async def _send_pingback(entry: dict) -> None:
    channel_id = int(entry.get("channel_id") or 0)
    message = (entry.get("message") or "").strip()
    if not channel_id or not message:
        return
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException as e:
            log.warning("pingback: could not fetch channel %s: %s", channel_id, e)
            return
    body = f"🔔 {message}"
    try:
        await channel.send(body)  # type: ignore[union-attr]
        log.info("pingback fired: id=%s → #%s", entry.get("id"), channel_id)
    except discord.HTTPException as e:
        log.warning("pingback: send failed for %s: %s", entry.get("id"), e)


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
                    await _replay_snoozed(item)
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
    # Capture both content AND the first embed (if any) so the replay
    # preserves the visual. Embeds-based pings have empty .content; we'd
    # otherwise replay "💤 (snoozed) (no content)" which is useless.
    embed_dict: dict | None = None
    if message.embeds:
        try:
            embed_dict = message.embeds[0].to_dict()
        except Exception:  # noqa: BLE001 — discord.py rarely throws here
            embed_dict = None
    items = _load_snoozed()
    items.append({
        "resend_at": (_now_local() + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        "content": message.content,
        "embed": embed_dict,
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

    # Don't abandon remaining chunks if one fails — a transient 429 / network
    # blip on chunk 2 of 5 shouldn't lose chunks 3-5 forever. Track failures
    # and report at the end so the log reflects what actually happened.
    sent_ok = 0
    failed = 0
    for i, chunk in enumerate(chunks):
        try:
            await channel.send(chunk)  # type: ignore[union-attr]
            sent_ok += 1
        except discord.HTTPException as e:
            failed += 1
            log.warning("ping chunk %d/%d send failed: %s — continuing",
                        i + 1, len(chunks), e)
    if failed:
        log.warning("ping → #%s: %d sent, %d failed of %d chunks",
                    getattr(channel, "name", PING_CHANNEL),
                    sent_ok, failed, len(chunks))
    else:
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

    # Prepend a wall-clock anchor so Claude always knows the actual local
    # time + day-of-week, not just the UTC date from its system context.
    # Cheap (~30 tokens) and prevents the "Iris thinks it's still yesterday
    # late at night" bug.
    content = f"{_now_context_block()}\n{content}"

    key = _session_key(message)
    lock = await _get_lock(key)

    # Per-channel message queue. asyncio.Lock is FIFO since Python 3.7, so we
    # can rely on the natural waiter ordering to process messages in arrival
    # order. We just need to (a) cap how deep the queue can get to prevent
    # abuse, and (b) give the user a visual cue that their message is queued
    # so they don't think it got dropped.
    pending = _session_pending.get(key, 0)
    if pending >= MAX_QUEUE_DEPTH:
        # Hard reject — the queue is already full enough that processing all
        # of it would take a long time. Better to drop loudly than silently.
        await message.reply(
            f"⏳ Queue is full ({pending} pending) — give me a moment to "
            "catch up, then try again."
        )
        return
    # Increment INSIDE the try block below so the finally always decrements,
    # even if add_reaction or the lock await raises a non-HTTPException
    # (e.g. CancelledError on bot shutdown).
    queued = lock.locked()

    try:
        _session_pending[key] = pending + 1
        if queued:
            # Visual ack so the user knows the message wasn't lost.
            try:
                await message.add_reaction("📥")
            except discord.HTTPException:
                pass
        async with lock:
            if queued:
                # We've reached the front of the queue — replace the
                # "queued" hourglass with a "now processing" mark.
                try:
                    await message.remove_reaction("📥", client.user)
                except discord.HTTPException:
                    pass
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
                    # Drain any embed-queue entries Iris produced during this turn
                    # BEFORE the completion ping fires, so visual order is:
                    #   [text reply]  →  [embed cards]  →  [✓ completion ping]
                    try:
                        if _EMBED_QUEUE.exists():
                            await _drain_embed_queue()
                    except Exception:
                        log.exception("embed drain on reply finalize")
                    # Fire-and-forget completion ping so Discord plays its
                    # normal new-message notification sound. The placeholder
                    # was only edited during streaming, which Discord doesn't
                    # notify on. Auto-deletes after TTL to keep the channel clean.
                    # _fire_and_forget pins the task so it isn't GC'd mid-flight.
                    if COMPLETION_PING_ENABLED:
                        _fire_and_forget(_completion_ping(message.channel))
                except Exception as e:
                    log.exception("query failed")
                    try:
                        await placeholder.edit(content=f"❌ Error: {e}")
                    except discord.HTTPException:
                        pass
    finally:
        # Always decrement, even if processing raised. Otherwise a single bad
        # turn would leak depth and eventually hit MAX_QUEUE_DEPTH forever.
        _session_pending[key] = max(0, _session_pending.get(key, 1) - 1)


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
