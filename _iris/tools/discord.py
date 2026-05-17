"""Discord-context tools.

These only do anything when the MCP server is launched by the Discord bot
(``docker/bot.py``), which writes a rolling per-channel JSONL log to
``IRIS_DISCORD_HISTORY_DIR`` and passes the active channel ID via env. From
Claude Desktop / other MCP clients they no-op gracefully.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import mcp


def _history_path() -> Path | None:
    channel_id = os.environ.get("IRIS_DISCORD_CHANNEL_ID")
    if not channel_id:
        return None
    history_dir = os.environ.get("IRIS_DISCORD_HISTORY_DIR",
                                 "/claude-auth/discord-channels")
    return Path(history_dir) / f"{channel_id}.jsonl"


def _pingback_queue_path() -> Path:
    history_dir = os.environ.get("IRIS_DISCORD_HISTORY_DIR",
                                 "/claude-auth/discord-channels")
    # Sibling of the per-channel logs so it lives in the same persistent volume.
    return Path(history_dir).parent / "pending_pings.jsonl"


def _embed_queue_path() -> Path:
    history_dir = os.environ.get("IRIS_DISCORD_HISTORY_DIR",
                                 "/claude-auth/discord-channels")
    return Path(history_dir).parent / "pending_embeds.jsonl"


@contextlib.contextmanager
def _flocked(path: Path):
    """Advisory file lock to coordinate queue appends with the bot's drain.

    Both writers (this module, when Iris appends an entry) and the reader
    (``docker/bot.py``'s ``_drain_embed_queue`` / ``_process_pingback_queue``)
    must take this same lock or the read-then-rewrite-empty pattern on the
    bot side can silently nuke an entry written in between."""
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


# ── Color palette (matches docker/bot.py constants) ──────────────────────────
# Same hex values as on the bot side so colors are consistent regardless of
# which process builds the embed dict.
COLOR_BLUE        = 0x3B82F6   # info / morning brief
COLOR_INDIGO      = 0x6366F1   # evening wrap-up
COLOR_GREEN       = 0x10B981   # ok / completed
COLOR_YELLOW      = 0xF59E0B   # warning / due-soon
COLOR_RED         = 0xEF4444   # error / imminent
COLOR_VIOLET      = 0x8B5CF6   # project status
COLOR_GRAY        = 0x6B7280   # neutral
COLOR_PINK        = 0xEC4899   # reminders


_COLOR_ALIASES = {
    "blue": COLOR_BLUE,
    "indigo": COLOR_INDIGO,
    "green": COLOR_GREEN,
    "yellow": COLOR_YELLOW,
    "red": COLOR_RED,
    "violet": COLOR_VIOLET,
    "purple": COLOR_VIOLET,
    "gray": COLOR_GRAY,
    "grey": COLOR_GRAY,
    "pink": COLOR_PINK,
}


def _resolve_color(c) -> int:
    """Accept hex int, ``"#rrggbb"`` string, or color name → int."""
    if c is None:
        return COLOR_GRAY
    if isinstance(c, int):
        return c
    s = str(c).strip().lower()
    if s.startswith("#"):
        try:
            return int(s[1:], 16)
        except ValueError:
            return COLOR_GRAY
    if s.startswith("0x"):
        try:
            return int(s, 16)
        except ValueError:
            return COLOR_GRAY
    return _COLOR_ALIASES.get(s, COLOR_GRAY)


# Section-name → icon mapping used when parsing markdown briefings into
# embed fields. Anything not matched falls through to a neutral bullet.
_SECTION_ICONS = (
    ("schedule",       "📅"),
    ("overdue task",   "⏰"),
    ("today's task",   "✅"),
    ("today task",     "✅"),
    ("upcoming task",  "⏭️"),
    ("reminder",       "🔔"),
    ("inbox",          "📥"),
    ("active project", "📋"),
    ("project",        "📋"),
    ("note",           "📝"),
    ("decision",       "📌"),
    ("question",       "❓"),
    ("focus",          "🎯"),
    ("recent",         "🕒"),
    ("done",           "✔️"),
    ("completed",      "✔️"),
    ("captured",       "📥"),
    ("summary",        "📖"),
    ("wrap",           "🌙"),
)


def _section_icon(name: str) -> str:
    low = name.lower()
    for key, icon in _SECTION_ICONS:
        if key in low:
            return icon
    return "•"


# Obsidian wikilink → either a plain display name OR a clickable
# ``obsidian://open?vault=...&file=...`` markdown link, when
# ``IRIS_OBSIDIAN_VAULT_NAME`` is set. Same rules as the bot-side
# ``docker/bot_embeds.py`` implementation; the two copies have to stay
# aligned because this module runs in the MCP subprocess and bot_embeds
# runs in the bot process — neither can import from the other.
import re as _re_wl  # noqa: E402 — kept close to its only use
_WIKILINK_RE = _re_wl.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")

_OBSIDIAN_VAULT_NAME = os.environ.get("IRIS_OBSIDIAN_VAULT_NAME", "").strip()
# https redirector prefix — see docker/bot_embeds.py for the full rationale.
# Discord blocks obsidian:// in clickable contexts; only https URLs render.
_OBSIDIAN_URL_PREFIX = os.environ.get("IRIS_OBSIDIAN_URL_PREFIX", "").strip().rstrip("/")


def _wikilink_to_text(m) -> str:
    target, display = m.group(1), m.group(2)
    raw_target = target.strip()
    label_target = raw_target.rsplit("/", 1)[-1]
    if label_target.endswith(".md"):
        label_target = label_target[:-3]
    label = display.strip() if display else label_target
    if not _OBSIDIAN_URL_PREFIX:
        return label
    file_path = raw_target
    if file_path.endswith(".md"):
        file_path = file_path[:-3]
    url = _OBSIDIAN_URL_PREFIX + "/" + quote(file_path, safe="/")
    return f"[{label}]({url})"


_FENCE_RE = _re_wl.compile(r"^(\s*)(`{3,}|~{3,})", _re_wl.MULTILINE)


def _strip_wikilinks(text: str) -> str:
    """Same fence-aware wikilink rewriter as the bot side. Skips spans inside
    fenced code blocks so ``[[literal]]`` samples in code aren't mangled."""
    if not text or "[[" not in text:
        return text
    out: list[str] = []
    pos = 0
    in_fence = False
    fence_char = ""
    for m in _FENCE_RE.finditer(text):
        segment = text[pos:m.start()]
        out.append(_WIKILINK_RE.sub(_wikilink_to_text, segment)
                   if not in_fence else segment)
        marker = m.group(2)
        out.append(text[m.start():m.end()])
        if not in_fence:
            in_fence = True
            fence_char = marker[0]
        elif marker[0] == fence_char:
            in_fence = False
            fence_char = ""
        pos = m.end()
    tail = text[pos:]
    out.append(_WIKILINK_RE.sub(_wikilink_to_text, tail)
               if not in_fence else tail)
    return "".join(out)


def _obsidian_url_for(path: str) -> str | None:
    """Return a CLICKABLE-IN-DISCORD URL for a vault-relative path.

    Only emits a URL when ``IRIS_OBSIDIAN_URL_PREFIX`` is set — that's the
    user's https redirector (e.g. ``https://o.example.com``) which 302s to
    ``obsidian://open?vault=...&file=<path>``. Discord blocks the bare
    ``obsidian://`` scheme everywhere clickable, so emitting it would
    render as plain text and confuse users. Returns None when no prefix is
    configured."""
    if not _OBSIDIAN_URL_PREFIX:
        return None
    rel = (path or "").strip().lstrip("/")
    if not rel:
        return None
    if rel.endswith(".md"):
        rel = rel[:-3]
    return _OBSIDIAN_URL_PREFIX + "/" + quote(rel, safe="/")


def _parse_markdown_sections(md: str) -> tuple[str, str, list[dict]]:
    """Split a markdown brief into (h1_title, intro, fields).

    Walks line by line. The first `# ` heading becomes the title; everything
    between that and the first `## ` becomes the intro (used as embed
    description). Each `## ` heading starts a new field.
    """
    lines = (md or "").splitlines()
    title = ""
    intro_lines: list[str] = []
    fields: list[dict] = []
    current_name: str | None = None
    current_body: list[str] = []

    def _flush() -> None:
        if current_name is None:
            return
        body = "\n".join(current_body).strip()
        if not body:
            body = "—"
        # Strip Obsidian wikilink syntax — Discord won't render `[[foo|bar]]`
        # and the bracketed path is noise. Keep the source briefing intact.
        body = _strip_wikilinks(body)
        # Discord field value limit: 1024 chars
        if len(body) > 1020:
            body = body[:1017] + "…"
        icon = _section_icon(current_name)
        fields.append({
            "name": f"{icon} {_strip_wikilinks(current_name)}",
            "value": body,
            "inline": False,
        })

    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            continue
        if stripped.startswith("## "):
            _flush()
            current_name = stripped[3:].strip()
            current_body = []
            continue
        if current_name is None:
            intro_lines.append(line)
        else:
            current_body.append(line)
    _flush()

    title = _strip_wikilinks(title)
    intro = "\n".join(intro_lines).strip()
    intro = _strip_wikilinks(intro)
    if len(intro) > 4000:
        intro = intro[:3997] + "…"
    return title, intro, fields


def _unescape_literal_escapes(s: str) -> str:
    """Convert literal escape sequences like ``"\\n"`` (2 chars: backslash +
    n) into the actual control characters they represent.

    Why: when an LLM constructs JSON arguments for a tool call, it sometimes
    over-escapes — emitting the 4-char sequence ``\\\\n`` in the source so
    the deserialised string contains the 2 chars ``\\n`` instead of a real
    newline. Discord then renders the backslash and the n verbatim. We
    normalise on the way in so embeds always show real line breaks.
    Idempotent on strings that already contain real newlines."""
    if not s or "\\" not in s:
        return s
    return (s
            .replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t"))


def _embed_text_clean(s) -> str:
    """Combined pipeline for any text we send into an embed: unescape
    over-escaped sequences, then apply wikilink rewriting. Safe on None /
    non-string (coerces via str())."""
    if s is None:
        return ""
    return _strip_wikilinks(_unescape_literal_escapes(str(s)))


def _build_embed_dict(
    *,
    title: str,
    description: str = "",
    color: int = COLOR_GRAY,
    fields: list[dict] | None = None,
    footer: str | None = None,
    timestamp: bool = True,
    url: str | None = None,
) -> dict:
    """Generic constructor for the JSON shape the bot expects on the queue.

    Centralised cleanup pipeline: every text-bearing attribute (title,
    description, field name, field value, footer) is run through
    ``_embed_text_clean`` which:

    1. **Unescapes literal escape sequences** like ``\\n`` → actual newline.
       LLMs occasionally over-escape when constructing tool-call JSON, so
       multi-line content arrives with visible backslash-n's. We normalise
       these so Discord renders real line breaks.
    2. **Rewrites Obsidian wikilinks** ``[[path]]`` into clickable masked
       links (when ``IRIS_OBSIDIAN_URL_PREFIX`` is set) or plain display
       names (when not). Callers don't have to remember to rewrite.
    """
    out: dict = {
        "title": _embed_text_clean(title)[:256] if title else None,
        "color": int(color),
    }
    if description:
        out["description"] = _embed_text_clean(description)[:4096]
    if fields:
        # Discord caps at 25 fields per embed.
        clean_fields: list[dict] = []
        for f in fields[:25]:
            clean_fields.append({
                "name": _embed_text_clean(f.get("name") or "—")[:256],
                "value": _embed_text_clean(f.get("value") or "—")[:1024],
                "inline": bool(f.get("inline", False)),
            })
        out["fields"] = clean_fields
    if footer:
        out["footer"] = _embed_text_clean(footer)[:2048]
    if timestamp:
        out["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if url:
        out["url"] = url
    return out


def _resolve_target_channel(channel_id: int | None) -> int | None:
    if channel_id is not None:
        return int(channel_id)
    for env in ("IRIS_DISCORD_CHANNEL_ID",
                "IRIS_DISCORD_PING_CHANNEL",
                "IRIS_DISCORD_NOTIFY_CHANNEL"):
        v = os.environ.get(env)
        if v:
            try:
                return int(v)
            except ValueError:
                continue
    return None


def _enqueue_embed(channel_id: int | None, embed: dict, content: str = "") -> str:
    target = _resolve_target_channel(channel_id)
    if not target:
        return ("err: no channel to send to (not inside bot context and "
                "IRIS_DISCORD_PING_CHANNEL not set).")
    entry = {
        "id": uuid.uuid4().hex[:12],
        "channel_id": target,
        "embed": embed,
        "content": (content or "").strip()[:2000],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = _embed_queue_path()
    try:
        with _flocked(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        return f"err: could not write embed queue {path}: {e}"
    return f"ok: embed queued (id={entry['id']})"


def _resolve_home_tz() -> ZoneInfo:
    name = (os.environ.get("IRIS_TIMEZONE")
            or os.environ.get("TZ")
            or "UTC").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return ZoneInfo("UTC")


def _parse_when(when: str) -> datetime | None:
    """Coerce a few common ``when`` shapes into an aware datetime.

    Accepts:
      * ISO 8601 with offset (``2026-05-17T00:30:00+02:00``)
      * Naive ISO (``2026-05-17T00:30:00``) — interpreted in home TZ
      * ``HH:MM`` — today at HH:MM home-local (or tomorrow if already past)
      * ``+Nm`` / ``+Nh`` — N minutes / hours from now
    """
    s = (when or "").strip()
    if not s:
        return None
    tz = _resolve_home_tz()
    now = datetime.now(tz)

    # Relative: +15m / +2h
    if s.startswith("+") and len(s) >= 3 and s[-1] in "mh":
        try:
            n = int(s[1:-1])
        except ValueError:
            return None
        delta = timedelta(minutes=n) if s[-1] == "m" else timedelta(hours=n)
        return now + delta

    # HH:MM today (or tomorrow if past)
    if len(s) in (4, 5) and ":" in s:
        try:
            hh, mm = s.split(":")
            target = now.replace(hour=int(hh), minute=int(mm),
                                 second=0, microsecond=0)
        except ValueError:
            return None
        if target <= now:
            target = target + timedelta(days=1)
        return target

    # ISO (with or without offset)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


@mcp.tool()
def fetch_discord_history(
    hours_back: int = 24,
    limit: int = 100,
    include_proactive: bool = False,
) -> str:
    """Read the bot's stored log of past Discord messages in *this* channel.

    Use this when the user references something said earlier that's outside
    your current context (e.g. "what did I say yesterday about the
    government appointment?"). The bot logs every message — yours and the
    user's — to a per-channel JSONL file; this tool reads that file filtered
    by time.

    Only works when this MCP server was launched by the Discord bot
    (``IRIS_DISCORD_CHANNEL_ID`` env var must be set). Returns an
    instructive error otherwise.

    Args:
        hours_back: How far back to look, in hours (1–720).
        limit: Cap on returned messages (most-recent kept).
        include_proactive: If True, include Iris's own event/reminder/briefing
            pings. Default False — those are noise, not conversation.
    """
    path = _history_path()
    if path is None:
        return (
            "err: this tool only works inside the Discord bot context "
            "(IRIS_DISCORD_CHANNEL_ID env not set)."
        )
    if not path.exists():
        return f"no Discord history file yet at {path} — bot hasn't logged anything in this channel"

    hours_back = max(1, min(int(hours_back), 720))   # ≤ 30 days
    limit = max(1, min(int(limit), 1000))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not include_proactive and e.get("is_proactive"):
                    continue
                try:
                    ts = datetime.fromisoformat(e["ts"])
                except (KeyError, ValueError):
                    continue
                if ts < cutoff:
                    continue
                entries.append(e)
    except OSError as exc:
        return f"err: could not read history: {exc}"

    if not entries:
        return f"no messages in the last {hours_back}h"

    entries = entries[-limit:]

    out_lines: list[str] = [
        f"=== last {hours_back}h ({len(entries)} message(s)) ==="
    ]
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["ts"]).astimezone().strftime("%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            ts = "?"
        author = "Iris" if e.get("is_iris") else (e.get("author") or "?")
        content = (e.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 800:
            content = content[:797] + "…"
        out_lines.append(f"[{ts}] {author}: {content}")
    return "\n".join(out_lines)


@mcp.tool()
def schedule_pingback(when: str, message: str, channel_id: int | None = None) -> str:
    """Schedule a one-shot precise-time Discord ping.

    Unlike ``add_reminder`` (which is date-granular and only fires within a
    lead window), this fires at the exact wall-clock minute requested. The
    Discord bot polls a queue file every ~30 seconds.

    Args:
        when: When to fire. Accepts:
          * ``HH:MM`` — today at that home-local time (or tomorrow if already past).
          * ``+15m`` / ``+2h`` — relative offset from now.
          * ISO 8601 — ``2026-05-17T00:30:00+02:00`` or naive (assumed home TZ).
        message: Text to send. Discord markdown OK. Will be prefixed with 🔔.
        channel_id: Override Discord channel ID. Defaults to the current
            channel (when called from inside the bot) or the configured
            ``IRIS_DISCORD_PING_CHANNEL`` otherwise.

    Returns a confirmation with the resolved fire time, or an error.
    """
    dt = _parse_when(when)
    if dt is None:
        return (f"err: could not parse when={when!r}. "
                "Use HH:MM, +Nm/+Nh, or ISO 8601.")
    msg = (message or "").strip()
    if not msg:
        return "err: message is empty."

    target_channel = channel_id
    if target_channel is None:
        env_cid = (os.environ.get("IRIS_DISCORD_CHANNEL_ID")
                   or os.environ.get("IRIS_DISCORD_PING_CHANNEL")
                   or os.environ.get("IRIS_DISCORD_NOTIFY_CHANNEL"))
        if env_cid:
            try:
                target_channel = int(env_cid)
            except ValueError:
                pass
    if not target_channel:
        return ("err: no channel to ping (not running inside bot context "
                "and IRIS_DISCORD_PING_CHANNEL not set).")

    entry = {
        "id": uuid.uuid4().hex[:12],
        "at": dt.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "channel_id": int(target_channel),
        "message": msg,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = _pingback_queue_path()
    try:
        with _flocked(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        return f"err: could not write queue file {path}: {exc}"

    local = dt.astimezone(_resolve_home_tz())
    return (f"ok: ping scheduled for {local.strftime('%Y-%m-%d %H:%M %Z')} "
            f"(id={entry['id']})")


@mcp.tool()
def list_pingbacks() -> str:
    """List pending precise-time pingbacks queued via ``schedule_pingback``."""
    path = _pingback_queue_path()
    if not path.exists():
        return "no pending pingbacks."
    tz = _resolve_home_tz()
    rows: list[str] = []
    # Take the flock for the read: prevents reading a half-written line that
    # another process is mid-appending. Cancel/append/process_queue all use
    # the same lock; staying consistent.
    try:
        with _flocked(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        local = datetime.fromisoformat(e["at"]).astimezone(tz)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
                    msg = (e.get("message") or "").strip()
                    if len(msg) > 100:
                        msg = msg[:97] + "…"
                    rows.append(
                        f"[{e.get('id', '?')}] {local.strftime('%Y-%m-%d %H:%M %Z')} "
                        f"→ #{e.get('channel_id', '?')}: {msg}"
                    )
    except OSError as exc:
        return f"err: could not read queue: {exc}"
    return "\n".join(rows) if rows else "no pending pingbacks."


@mcp.tool()
def cancel_pingback(pingback_id: str) -> str:
    """Cancel a pending pingback by its id (from ``schedule_pingback`` or
    ``list_pingbacks``). Returns ok/not-found."""
    path = _pingback_queue_path()
    if not path.exists():
        return "not found (queue is empty)."
    target = (pingback_id or "").strip()
    if not target:
        return "err: pingback_id required."
    kept: list[str] = []
    found = False
    try:
        with _flocked(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("id") == target:
                        found = True
                        continue
                    kept.append(json.dumps(e, ensure_ascii=False))
            if found:
                tmp = path.with_suffix(".jsonl.tmp")
                tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
                tmp.replace(path)
    except OSError as exc:
        return f"err: rewrite failed: {exc}"
    return f"ok: cancelled {target}" if found else f"not found: {target}"


# ── Embed tools (canned + generic) ───────────────────────────────────────────
# Each tool writes an embed-request JSON line to the queue file. The Discord
# bot's _embed_queue_loop polls it (~1s) and renders to a real discord.Embed.

@mcp.tool()
def embed_custom(
    title: str,
    description: str = "",
    fields: list[dict] | None = None,
    color: str = "gray",
    footer: str = "",
    content: str = "",
    channel_id: int | None = None,
) -> str:
    """Send a fully-custom Discord embed. Escape hatch for ad-hoc visuals
    that no canned tool covers.

    Args:
        title: Embed title (≤ 256 chars).
        description: Body text shown under the title (≤ 4096 chars).
            Markdown supported.
        fields: List of ``{"name": str, "value": str, "inline": bool}`` dicts.
            Max 25, name ≤ 256, value ≤ 1024. ``inline`` lets Discord render
            up to 3 fields side-by-side on wide screens.
        color: One of ``blue / indigo / green / yellow / red / violet / gray
            / pink``, or ``"#rrggbb"`` hex.
        footer: Small text at the bottom (≤ 2048 chars).
        content: Optional chat text shown above the embed (≤ 2000 chars).
        channel_id: Override channel. Defaults to current Discord channel.
    """
    embed = _build_embed_dict(
        title=title,
        description=description,
        color=_resolve_color(color),
        fields=fields or [],
        footer=footer or None,
    )
    return _enqueue_embed(channel_id, embed, content)


@mcp.tool()
def embed_morning_brief(
    date: str = "today",
    color: str = "blue",
    channel_id: int | None = None,
    sync_calendars: bool = True,
) -> str:
    """Render ``morning_briefing(date)`` as a Discord embed and post it.

    Uses the same data assembly as the plain-text routine but parses each
    ``## Section`` heading into its own embed field with a topical icon. Wide
    sections are truncated to fit Discord's 1024-char-per-field limit.

    Args:
        date: ``"today"`` / ``"tomorrow"`` / ``YYYY-MM-DD``.
        color: Embed sidebar color.
        channel_id: Override Discord channel.
        sync_calendars: When True (default) AND ``IRIS_DEFAULT_ICAL_URLS``
            is set in the env, sync all configured iCal feeds BEFORE
            building the brief, so today's freshly-added external events
            are included. No-op when no feeds configured. Pass False to
            skip if you just synced.
    """
    if sync_calendars and os.environ.get("IRIS_DEFAULT_ICAL_URLS", "").strip():
        try:
            # Lazy import to avoid a circular when `calendar` imports from us.
            from .calendar import sync_all_calendars
            sync_all_calendars(days_ahead=7, days_back=0, dry_run=False)
        except Exception:
            pass  # Don't fail the brief if a feed is unreachable.
    from .routines import morning_briefing
    md = morning_briefing(date)
    title, intro, fields = _parse_markdown_sections(md)
    if not title:
        title = "🌅 Morning brief"
    elif not title.startswith(("🌅", "Good", "Briefing")):
        title = f"🌅 {title}"
    embed = _build_embed_dict(
        title=title,
        description=intro,
        color=_resolve_color(color),
        fields=fields,
        footer="morning_briefing",
    )
    return _enqueue_embed(channel_id, embed)


@mcp.tool()
def embed_evening_wrapup(
    date: str = "today",
    color: str = "indigo",
    channel_id: int | None = None,
) -> str:
    """Render ``evening_wrapup(date)`` as a Discord embed and post it.

    Sections (Completed, Captured, Open, Recap) become embed fields. Same
    1024-char-per-field truncation as ``embed_morning_brief``.
    """
    from .calendar import evening_wrapup
    md = evening_wrapup(date)
    title, intro, fields = _parse_markdown_sections(md)
    if not title:
        title = "🌙 Evening wrap-up"
    elif not title.startswith(("🌙", "Evening", "Wrap")):
        title = f"🌙 {title}"
    embed = _build_embed_dict(
        title=title,
        description=intro,
        color=_resolve_color(color),
        fields=fields,
        footer="evening_wrapup",
    )
    return _enqueue_embed(channel_id, embed)


@mcp.tool()
def embed_daily_agenda(
    date: str = "today",
    days: int = 1,
    color: str = "blue",
    channel_id: int | None = None,
) -> str:
    """Render events + tasks + reminders for a date (or range) as a Discord
    embed card with one field per category.

    Queries the vault DB directly (NOT the ``daily_agenda`` text output,
    which uses a pipe-delimited internal format that doesn't parse cleanly).

    Args:
        date: ``"today"``, ``"tomorrow"``, ``"this week"``, ``"next 3 days"``,
            an ISO date, etc.
        days: Number of days to include. Auto-derived for range expressions.
        color: ``blue`` (default), ``yellow`` if anything is overdue.
        channel_id: Override Discord channel.
    """
    from datetime import datetime as _dt, timedelta as _td
    from ..core import (
        get_vault_index, resolve_natural_date, _resolve_date_range,
        parse_iso_date,
    )

    range_result = _resolve_date_range(date)
    if range_result is not None:
        resolved, days = range_result
    else:
        resolved = resolve_natural_date(date)
        if resolved is None:
            return f"err: cannot parse date={date!r}"
    start_date = _dt.strptime(resolved, "%Y-%m-%d").date()
    end_date = start_date + _td(days=max(1, days) - 1)
    date_from = start_date.isoformat()
    date_to = end_date.isoformat()
    today = _dt.now().date()

    idx = get_vault_index()
    events = idx.query_events(date_from=date_from, date_to=date_to)
    all_tasks = idx.query_tasks(checked=False, limit=500)
    all_rems = idx.query_reminders(checked=False, limit=500)

    # Bucket tasks + reminders by date relative to window.
    tasks_overdue: list[dict] = []
    tasks_in_range: list[dict] = []
    for t in all_tasks:
        dd = parse_iso_date(t.get("due") or "")
        if dd is None:
            continue
        if dd.date() < start_date:
            tasks_overdue.append(t)
        elif start_date <= dd.date() <= end_date:
            tasks_in_range.append(t)
    rems_overdue: list[dict] = []
    rems_in_range: list[dict] = []
    for r in all_rems:
        rd = parse_iso_date(r.get("remind_on") or "")
        if rd is None:
            continue
        if rd.date() < start_date:
            rems_overdue.append(r)
        elif start_date <= rd.date() <= end_date:
            rems_in_range.append(r)

    def _fmt_event(ev: dict) -> str:
        when = ev.get("time") or ""
        end = ev.get("end_time") or ""
        if ev.get("all_day"):
            stamp = "all-day"
        elif when and end:
            stamp = f"{when}–{end}"
        elif when:
            stamp = when
        else:
            stamp = ""
        loc = f" @ {ev['location']}" if ev.get("location") else ""
        prefix = f"**{ev['date']}**" + (f" {stamp}" if stamp else "")
        return f"- {prefix} — {ev.get('title', '?')}{loc}"

    def _fmt_task(t: dict) -> str:
        due = t.get("due") or ""
        note = t.get("note_path") or ""
        note_part = f" — [[{note[:-3]}]]" if note.endswith(".md") else ""
        return f"- [ ] {t.get('text', '?')} (due {due}){note_part}"

    def _fmt_reminder(r: dict) -> str:
        when = r.get("remind_on") or ""
        note = r.get("note_path") or ""
        note_part = f" — [[{note[:-3]}]]" if note.endswith(".md") else ""
        return f"- {r.get('text', '?')} (📅 {when}){note_part}"

    fields: list[dict] = []
    has_overdue = bool(tasks_overdue or rems_overdue)

    if events:
        body = "\n".join(_fmt_event(e) for e in events[:15])
        if len(events) > 15:
            body += f"\n_…+{len(events) - 15} more_"
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({"name": f"📅 Events ({len(events)})",
                       "value": body, "inline": False})

    if tasks_overdue:
        body = "\n".join(_fmt_task(t) for t in tasks_overdue[:10])
        if len(tasks_overdue) > 10:
            body += f"\n_…+{len(tasks_overdue) - 10} more_"
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({"name": f"⏰ Overdue Tasks ({len(tasks_overdue)})",
                       "value": body, "inline": False})

    if tasks_in_range:
        body = "\n".join(_fmt_task(t) for t in tasks_in_range[:15])
        if len(tasks_in_range) > 15:
            body += f"\n_…+{len(tasks_in_range) - 15} more_"
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({"name": f"✅ Tasks Due ({len(tasks_in_range)})",
                       "value": body, "inline": False})

    if rems_overdue:
        body = "\n".join(_fmt_reminder(r) for r in rems_overdue[:10])
        if len(rems_overdue) > 10:
            body += f"\n_…+{len(rems_overdue) - 10} more_"
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({"name": f"🔔 Overdue Reminders ({len(rems_overdue)})",
                       "value": body, "inline": False})

    if rems_in_range:
        body = "\n".join(_fmt_reminder(r) for r in rems_in_range[:15])
        if len(rems_in_range) > 15:
            body += f"\n_…+{len(rems_in_range) - 15} more_"
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({"name": f"🔔 Reminders ({len(rems_in_range)})",
                       "value": body, "inline": False})

    if not fields:
        fields = [{"name": "🎉 Clear", "value": "Nothing scheduled.",
                   "inline": False}]

    # Title shows the range; description shows a relative-date hint.
    if days == 1:
        title = f"📅 Agenda — {date_from}"
        if start_date == today:
            title += " (today)"
        elif start_date == today + _td(days=1):
            title += " (tomorrow)"
        intro = None
    else:
        title = f"📅 Agenda · {date_from} → {date_to}"
        intro = f"_{days} days · {len(events)} events, " \
                f"{len(tasks_in_range)} tasks, {len(rems_in_range)} reminders_"

    chosen_color = "yellow" if has_overdue else color
    embed = _build_embed_dict(
        title=title,
        description=intro,
        color=_resolve_color(chosen_color),
        fields=fields,
        footer=f"daily_agenda · {date_from}" + (f" → {date_to}" if days > 1 else ""),
    )
    return _enqueue_embed(channel_id, embed)


@mcp.tool()
def embed_project_status(
    project_path: str,
    color: str = "violet",
    channel_id: int | None = None,
) -> str:
    """Render a one-page project dashboard as a Discord embed.

    Pulls open tasks, recent activity, and basic metadata for the given vault
    note. ``project_path`` is the path inside the vault (e.g.
    ``20_Projects/Homelab.md``).
    """
    from .analysis import project_status
    md = project_status(project_path)
    title, intro, fields = _parse_markdown_sections(md)
    if not title:
        title = f"📋 {Path(project_path).stem}"
    elif not title.startswith(("📋", "Project")):
        title = f"📋 {title}"
    # Title is clickable when we have an https redirector configured —
    # _obsidian_url_for now only returns https URLs (never obsidian://),
    # which Discord accepts on the embed `url` parameter.
    embed = _build_embed_dict(
        title=title,
        description=intro,
        color=_resolve_color(color),
        fields=fields,
        footer=project_path,
        url=_obsidian_url_for(project_path),
    )
    return _enqueue_embed(channel_id, embed)


@mcp.tool()
def embed_event(
    date: str,
    title_match: str = "",
    color: str = "yellow",
    channel_id: int | None = None,
) -> str:
    """Render a single calendar event's full details as a Discord embed.

    Args:
        date: ISO date (``2026-05-17``) or natural-language (``today`` /
            ``tomorrow``) — used to look up the event row.
        title_match: Optional substring to disambiguate when the date has
            multiple events. Case-insensitive; matches the event's title.
        color: Defaults to yellow (upcoming). Use ``red`` for imminent.
    """
    from ..core import get_vault_index, resolve_natural_date
    date_from = resolve_natural_date(date)
    if date_from is None:
        return f"err: could not parse date={date!r}."
    idx = get_vault_index()
    events = idx.query_events(date_from=date_from, date_to=date_from)
    if not events:
        return f"no events found on {date_from}."
    if title_match:
        needle = title_match.lower().strip()
        events = [e for e in events if needle in (e.get("title") or "").lower()]
        if not events:
            return f"no events matching {title_match!r} on {date_from}."
    ev = events[0]
    when = ev.get("time") or ""
    end = ev.get("end_time") or ""
    if when and end:
        when_line = f"**{when}–{end}**"
    elif when:
        when_line = f"**{when}**"
    else:
        when_line = "_all-day_"
    fields: list[dict] = [
        {"name": "🕐 When", "value": f"{date_from}\n{when_line}", "inline": True},
    ]
    if ev.get("location"):
        fields.append({"name": "📍 Where", "value": ev["location"], "inline": True})
    if ev.get("description"):
        desc = ev["description"]
        if len(desc) > 1020:
            desc = desc[:1017] + "…"
        fields.append({"name": "📝 Notes", "value": desc, "inline": False})
    note_path = ev.get("note_path") or ""
    if note_path:
        # Show as clickable link inside the field when we have a vault name,
        # plain code-formatted path otherwise.
        url = _obsidian_url_for(note_path)
        value = f"[{Path(note_path).stem}]({url})" if url else f"`{note_path}`"
        fields.append({"name": "🔗 Source", "value": value, "inline": False})
    embed = _build_embed_dict(
        title=f"📅 {ev.get('title', '(no title)')}",
        color=_resolve_color(color),
        fields=fields,
        footer="event" + (f" · {note_path}" if note_path else ""),
        url=_obsidian_url_for(note_path) if note_path else None,
    )
    return _enqueue_embed(channel_id, embed)


@mcp.tool()
def embed_query(
    sql: str,
    title: str,
    color: str = "blue",
    mode: str = "table",
    footer: str = "",
    channel_id: int | None = None,
) -> str:
    """Run a read-only SQL query against the vault DB and render the result
    as a Discord embed.

    Use this when no canned ``embed_*`` tool fits and you want a quick visual
    dashboard for an ad-hoc query — e.g. "today's events by location",
    "tasks per project this week", "anime watching this season".

    Args:
        sql: A SELECT statement. Write operations (INSERT/UPDATE/etc.) are
            blocked, same as ``sqlite_query``. Use ``sqlite_schema`` to
            discover columns first if you don't know the schema.
        title: Embed title (≤ 256 chars). Pick something descriptive — this
            is what the user will see in their notification.
        color: ``blue / indigo / green / yellow / red / violet / gray / pink``
            or ``"#rrggbb"`` hex. Default blue.
        mode: How to render rows:
            - ``"table"`` (default): all rows go into the description as a
              monospace code-block table with aligned columns. Best for
              wide/many-column results, single-glance scanning.
            - ``"fields"``: each row becomes an embed field. First column =
              field name, remaining columns = value (joined " · "). Best for
              ≤ 25 rows where each row is an entity with attributes (e.g.
              one row per project, one row per event).
        footer: Optional footer text (≤ 2048 chars).
        channel_id: Override Discord channel. Defaults to current.

    Limits: queries without an explicit ``LIMIT`` clause get ``LIMIT 10``
    appended automatically — pass your own ``LIMIT 50`` etc. if you need
    more. Render caps: 25 rows in fields mode; table mode rebuilds line-by-
    line to fit Discord's 4096-char description and adds ``…truncated`` if
    it had to drop rows.
    """
    # SQL safety — same rules as sqlite_query.
    import re as _re
    s = sql.strip().rstrip(";")
    if not s:
        return "err: empty query"
    write_re = _re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|REINDEX|VACUUM)\b"
        r"|\bREPLACE\s+INTO\b|\bINSERT\s+OR\s+REPLACE\b|\bPRAGMA\s+\w+\s*=",
        _re.IGNORECASE,
    )
    if write_re.search(s):
        return "err: only SELECT queries allowed in embed_query"

    # Cap unbounded queries hard. A SELECT without LIMIT against a big table
    # would materialise every row into memory; in embed-output context only
    # the top handful are visible anyway. 10 matches typical "top N" use
    # cases — pass an explicit LIMIT if you need more. We strip SQL string
    # literals + comments before testing so `WHERE x = 'rate limit 10'`
    # isn't false-matched.
    from .sqlite import _strip_sql_strings_and_comments
    s_for_check = _strip_sql_strings_and_comments(s)
    if not _re.search(r"\blimit\s+\d+\b", s_for_check, _re.IGNORECASE):
        s = s + " LIMIT 10"

    from .. import core as _core
    idx = _core.get_vault_index()
    try:
        with sqlite3.connect(str(idx.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(s).fetchall()
    except sqlite3.Error as exc:
        return f"err: SQL failed: {exc}"

    if not rows:
        embed = _build_embed_dict(
            title=title,
            description="_(no rows)_",
            color=_resolve_color(color),
            footer=footer or None,
        )
        return _enqueue_embed(channel_id, embed)

    cols = list(rows[0].keys())
    mode = (mode or "table").strip().lower()

    if mode == "fields":
        fields: list[dict] = []
        for r in rows[:25]:
            name_val = str(r[cols[0]]) if cols else "—"
            rest = [str(r[c]) for c in cols[1:]]
            value = " · ".join(v for v in rest if v) or "—"
            if len(value) > 1020:
                value = value[:1017] + "…"
            fields.append({"name": name_val[:256], "value": value, "inline": False})
        suffix = f" (+{len(rows) - 25} more rows)" if len(rows) > 25 else ""
        embed = _build_embed_dict(
            title=title,
            description=f"`{len(rows)} row(s)`{suffix}" if suffix else None,
            color=_resolve_color(color),
            fields=fields,
            footer=footer or None,
        )
        return _enqueue_embed(channel_id, embed)

    # "table" mode: render as monospace code-block table inside description.
    widths = [len(c) for c in cols]
    sample = rows[:30]
    for r in sample:
        for i, c in enumerate(cols):
            widths[i] = max(widths[i], min(40, len(str(r[c]))))
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    rule = "  ".join("-" * w for w in widths)
    body_lines: list[str] = []
    for r in sample:
        cells = []
        for i, c in enumerate(cols):
            val = str(r[c])
            if len(val) > widths[i]:
                val = val[: widths[i] - 1] + "…"
            cells.append(val.ljust(widths[i]))
        body_lines.append("  ".join(cells))
    table_text = "```\n" + header + "\n" + rule + "\n" + "\n".join(body_lines) + "\n```"
    if len(rows) > 30:
        table_text += f"\n_…+{len(rows) - 30} more rows_"
    # If too long, truncate body lines and re-build so we never cut off the
    # trailing ``` fence (Discord would render everything after as code).
    if len(table_text) > 4090:
        # Reserve a fixed budget for header, rule, fences, and the truncation
        # suffix WITH the dropped-row count so the user knows what's missing.
        # We compute the suffix lazily once we know how many we kept.
        keep_lines: list[str] = []
        suffix_template = "\n```\n_…truncated (+{n} rows)_"
        # Pessimistic suffix size (3 digits' worth of N) for budget purposes.
        suffix_budget = len(suffix_template.format(n="000"))
        running = len("```\n") + len(header) + 1 + len(rule) + 1 + suffix_budget
        for line in body_lines:
            if running + len(line) + 1 > 4090:
                break
            keep_lines.append(line)
            running += len(line) + 1
        dropped = (len(body_lines) - len(keep_lines)) + max(0, len(rows) - len(body_lines))
        table_text = (
            "```\n" + header + "\n" + rule + "\n"
            + "\n".join(keep_lines)
            + suffix_template.format(n=dropped)
        )
    embed = _build_embed_dict(
        title=title,
        description=table_text,
        color=_resolve_color(color),
        footer=footer or None,
    )
    return _enqueue_embed(channel_id, embed)


@mcp.tool()
def embed_note(
    path: str,
    color: str = "gray",
    excerpt_chars: int = 600,
    channel_id: int | None = None,
) -> str:
    """Render a vault note as a Discord embed card.

    Use this when you want to *show* a note to Hyun-Min visually — when
    referencing one in conversation, presenting a summary, or surfacing
    something you found. Pulls:
      * Title from frontmatter ``title:`` or first H1 or filename.
      * Frontmatter-driven metadata fields (type, tags, mtime).
      * Either the stored ``summary:`` from frontmatter, or a body excerpt.

    Args:
        path: Vault-relative path (e.g. ``20_Projects/Homelab.md``).
        color: gray (default), blue, violet, etc. See ``embed_custom`` for
            the full palette.
        excerpt_chars: When no ``summary:`` frontmatter exists, how many
            chars of body to show as the description. ≤ 4096.
        channel_id: Override channel. Defaults to current Discord channel.
    """
    from ..core import safe_path
    rel = (path or "").strip().lstrip("/")
    if not rel:
        return "err: path is required."
    try:
        full = safe_path(rel)
    except (ValueError, FileNotFoundError) as exc:
        return f"err: bad path: {exc}"
    if not full.exists() or not full.is_file():
        return f"err: note not found: {rel}"
    try:
        text = full.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return f"err: could not read note: {exc}"

    # Pull frontmatter (between the first two `---` lines).
    fm: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 4)
        if end != -1:
            head = text[4:end]
            body = text[end + 4:].lstrip("\n")
            import re as _re
            for m in _re.finditer(
                r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.+?)\s*$",
                head, flags=_re.MULTILINE,
            ):
                fm[m.group(1).lower()] = m.group(2).strip()

    # Title: frontmatter > first H1 > filename
    title = fm.get("title")
    if not title:
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                break
    if not title:
        title = full.stem
    title = title[:240]

    # Description: summary > body excerpt
    summary = fm.get("summary")
    if summary:
        description = summary[: min(excerpt_chars, 4096)]
    else:
        excerpt = body.strip()
        # Strip headings/markdown noise lightly for a cleaner preview
        excerpt = excerpt.replace("\r", "")
        if len(excerpt) > excerpt_chars:
            excerpt = excerpt[:excerpt_chars - 1] + "…"
        description = excerpt

    fields: list[dict] = []
    if fm.get("type"):
        fields.append({"name": "📂 Type", "value": fm["type"][:64], "inline": True})
    if fm.get("status"):
        fields.append({"name": "🚦 Status", "value": fm["status"][:64], "inline": True})
    # tags can be "[a, b, c]" or "#a #b" — render as-is, capped.
    tags_raw = fm.get("tags")
    if tags_raw:
        tags = tags_raw.strip("[]").strip()
        fields.append({"name": "🏷️ Tags", "value": tags[:200], "inline": True})
    # File mtime is cheaper + always available — no need to round-trip through
    # the index for the modified-date field.
    try:
        mtime = datetime.fromtimestamp(full.stat().st_mtime).strftime("%Y-%m-%d")
        fields.append({"name": "🕒 Modified", "value": mtime, "inline": True})
    except OSError:
        pass

    # Title is clickable when https redirector configured.
    embed = _build_embed_dict(
        title=f"📝 {title}",
        description=description,
        color=_resolve_color(color),
        fields=fields,
        footer=rel,
        url=_obsidian_url_for(rel),
    )
    return _enqueue_embed(channel_id, embed)


@mcp.tool()
def embed_callout(
    kind: str,
    title: str,
    body: str = "",
    channel_id: int | None = None,
) -> str:
    """A simple semantic callout card — info / warning / error / success / tip.

    Use this for short, attention-getting messages where a plain-text reply
    wouldn't have enough visual weight. The colour and icon are chosen for
    you from ``kind``; the title is what the user sees at a glance; ``body``
    is the explanation.

    Args:
        kind: One of:
            - ``"info"``    — blue, ℹ️
            - ``"success"`` — green, ✅
            - ``"warning"`` — yellow, ⚠️
            - ``"error"``   — red, ❌
            - ``"tip"``     — violet, 💡
            - ``"question"``— indigo, ❓
        title: Short headline (≤ 240 chars).
        body: Longer explanation as the embed description (≤ 4096 chars).
        channel_id: Override channel. Defaults to current Discord channel.

    Examples of when to use this instead of plain text:
      * "✅ Saved your ETH ceremony to the calendar" (success).
      * "⚠️ This note has 3 broken wikilinks — want me to fix them?" (warning).
      * "💡 Tip: try setting `timezone: Asia/Seoul` for your travel days" (tip).
    """
    palette = {
        "info":     ("ℹ️", COLOR_BLUE),
        "success":  ("✅", COLOR_GREEN),
        "warning":  ("⚠️", COLOR_YELLOW),
        "error":    ("❌", COLOR_RED),
        "tip":      ("💡", COLOR_VIOLET),
        "question": ("❓", COLOR_INDIGO),
    }
    k = (kind or "info").strip().lower()
    icon, color = palette.get(k, palette["info"])
    t = (title or "").strip() or "(callout)"
    embed = _build_embed_dict(
        title=f"{icon} {t}"[:256],
        description=(body or "").strip() or None,
        color=color,
    )
    return _enqueue_embed(channel_id, embed)
