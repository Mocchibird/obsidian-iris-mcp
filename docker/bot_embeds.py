"""Discord embed helpers — pure, stateless.

Everything in here is independent of the live ``discord.Client`` instance and
the bot's logger. The "send" / queue-drain logic stays in ``bot.py`` because
it needs the running client; this module exists to keep colour constants,
section parsing, and the ``dict → discord.Embed`` builder out of the main
file.

Mirrors the same constants and parser used by ``_iris.tools.discord`` on the
MCP side, so visuals stay identical whether an embed was produced
proactively (bot process) or via an MCP tool call (Claude subprocess).
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import discord


# Obsidian vault display name — used to build deep-links in embeds. The
# value must match what Obsidian sees as the vault name (typically the
# folder basename of the host-side vault path).
OBSIDIAN_VAULT_NAME = os.environ.get("IRIS_OBSIDIAN_VAULT_NAME", "").strip()

# Discord doesn't render `obsidian://...` URLs as clickable — neither as
# bare auto-detected URLs nor as `[label](obsidian://...)` masked links.
# Its scheme allowlist is http/https/ftp/discord/skype + a handful.
# To get truly clickable note links, set IRIS_OBSIDIAN_URL_PREFIX to a
# user-owned https URL prefix that 302-redirects to obsidian://, e.g.
#   IRIS_OBSIDIAN_URL_PREFIX=https://o.example.com/
# Then the bot generates `<prefix>10_Profile/People/Foo` URLs which Discord
# WILL render as clickable, and your server redirects them. Caddy config:
#   o.example.com {
#       redir "obsidian://open?vault=AI_Memory&file={path}" 302
#   }
# (Trailing slash on the prefix is optional — the bot normalises.)
OBSIDIAN_URL_PREFIX = os.environ.get("IRIS_OBSIDIAN_URL_PREFIX", "").strip().rstrip("/")


# Wikilink → plain text. Obsidian's `[[20_Projects/Foo|Foo]]` renders as a
# clickable link in Obsidian but shows as raw bracket syntax in Discord. For
# embed display we collapse to the alias (after `|`) if present, otherwise
# the file's basename without `.md`. Source briefings still have full
# wikilinks — only the rendering layer strips them.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _wikilink_to_text(match: re.Match) -> str:
    """Render a wikilink as either a clickable masked link (when an https
    redirector prefix is configured) or plain display text (fallback).

    Discord blocks ``obsidian://`` in masked links — only http(s) schemes
    are clickable — so we only emit a real link when the user has set up an
    https redirector via ``IRIS_OBSIDIAN_URL_PREFIX``."""
    target, display = match.group(1), match.group(2)
    raw_target = target.strip()
    # Drop .md so the display label looks natural.
    label_target = raw_target.rsplit("/", 1)[-1]
    if label_target.endswith(".md"):
        label_target = label_target[:-3]
    label = display.strip() if display else label_target
    if not OBSIDIAN_URL_PREFIX:
        # No redirector configured → no point emitting an obsidian:// URL
        # that won't render as clickable. Fall back to plain display name.
        return label
    file_path = raw_target
    if file_path.endswith(".md"):
        file_path = file_path[:-3]
    url = OBSIDIAN_URL_PREFIX + "/" + quote(file_path, safe="/")
    return f"[{label}]({url})"


_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})", re.MULTILINE)


def strip_wikilinks(text: str) -> str:
    """Rewrite ``[[path|alias]]`` / ``[[path]]`` into either:

      * a Discord markdown link to ``obsidian://open?vault=...&file=...`` —
        when ``IRIS_OBSIDIAN_VAULT_NAME`` is set; the link is clickable and
        opens the note in Obsidian on devices where the app is installed; OR
      * plain display text — when the vault name env var is unset.

    Skips spans inside fenced code blocks (``` or ~~~) so literal wikilink
    syntax in code samples isn't mangled. Pure function. Idempotent on text
    without ``[[``.
    """
    if not text or "[[" not in text:
        return text
    # Walk the text segment by segment, toggling on every fence marker. Apply
    # the wikilink substitution only to non-fenced segments.
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


# ── Color palette ───────────────────────────────────────────────────────────
# Keep these in sync with `_iris/tools/discord.py`'s constants of the same
# name. Both sides hand the bot integers via the JSON queue, so the visual
# identity is determined here.
COLOR_BLUE   = 0x3B82F6   # info / morning brief
COLOR_INDIGO = 0x6366F1   # evening wrap-up
COLOR_GREEN  = 0x10B981   # ok / completed
COLOR_YELLOW = 0xF59E0B   # warning / due-soon
COLOR_RED    = 0xEF4444   # error / imminent
COLOR_VIOLET = 0x8B5CF6   # project status
COLOR_GRAY   = 0x6B7280   # neutral
COLOR_PINK   = 0xEC4899   # reminder


# ── Section name → icon mapping ─────────────────────────────────────────────
# Used by the markdown-to-embed parser. The first substring match wins, so
# more-specific keys must come before more-general ones (e.g. "today's task"
# before "task").
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


def section_icon(name: str) -> str:
    """Pick a topical emoji for a markdown ``## Section`` heading."""
    low = name.lower()
    for key, icon in _SECTION_ICONS:
        if key in low:
            return icon
    return "•"


def parse_md_sections(md: str) -> tuple[str, str, list[dict]]:
    """Split a markdown brief into ``(h1_title, intro, fields)``.

    Walks line by line. The first ``# `` heading becomes the title; everything
    between that and the first ``## `` becomes the intro (used as the embed
    description). Each ``## `` heading starts a new field with the section
    body as its value. Trims overlong field values to 1020 chars (Discord's
    limit is 1024) and intro to 4000 (limit 4096).
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
        body = "\n".join(current_body).strip() or "—"
        # Strip Obsidian wikilink syntax — Discord won't render it, and the
        # bracketed paths are noisy. Done here (not in the source briefing)
        # so the same morning_briefing text still renders cleanly in vault.
        body = strip_wikilinks(body)
        if len(body) > 1020:
            body = body[:1017] + "…"
        fields.append({
            "name": f"{section_icon(current_name)} {strip_wikilinks(current_name)}",
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

    title = strip_wikilinks(title)
    intro = "\n".join(intro_lines).strip()
    intro = strip_wikilinks(intro)
    if len(intro) > 4000:
        intro = intro[:3997] + "…"
    return title, intro, fields


def dict_to_embed(payload: dict) -> discord.Embed:
    """Inverse of ``_iris.tools.discord._build_embed_dict``.

    Accepts the JSON shape that the MCP-side embed tools write to the queue
    and reconstructs a ``discord.Embed`` ready to send. All field values are
    clipped to Discord's per-attribute limits before being passed in, so any
    over-budget payloads degrade gracefully instead of being rejected by the
    Discord API.
    """
    # Belt-and-braces truncation. The MCP-side builder already clamps these,
    # but the proactive in-process paths (e.g. _fire_event_embed) hand us
    # untrimmed strings from vault rows. Discord rejects the whole embed if
    # any limit is exceeded, so we clamp here too. ``str()`` coerces any
    # weird non-string value (int, list, whatever a malformed queue entry
    # held) so the slicing can't blow up.
    # Also: wikilink-rewrite every text-bearing attribute. The MCP-side
    # _build_embed_dict already runs the same transform, but proactive
    # in-process payloads (event pings, reminder pings) hand us raw vault
    # content that hasn't been through that builder. Applying the transform
    # here makes it idempotent (it's a no-op on text without `[[`) and
    # ensures EVERY embed gets clickable Obsidian links regardless of which
    # code path constructed the payload.
    raw_title = payload.get("title")
    raw_desc = payload.get("description")
    # Discord rejects the WHOLE embed if `url` has a non-http(s) scheme —
    # e.g. obsidian:// returns 400 Bad Request with:
    #   "In embeds.0.url: Scheme 'obsidian' is not supported.
    #    Scheme must be one of ('http', 'https')."
    # Defense-in-depth: silently drop non-http URLs here so a bad payload
    # from a future caller doesn't break the message. Obsidian deep-links
    # should be exposed as masked links inside fields/description instead
    # — those API paths accept any URI scheme.
    raw_url = payload.get("url")
    safe_url = raw_url if (
        isinstance(raw_url, str)
        and raw_url.lower().startswith(("http://", "https://"))
    ) else None
    e = discord.Embed(
        title=(strip_wikilinks(str(raw_title))[:256] if raw_title else None),
        description=(strip_wikilinks(str(raw_desc))[:4096] if raw_desc else None),
        color=int(payload.get("color") or COLOR_GRAY),
        url=safe_url,
    )
    ts = payload.get("timestamp")
    if ts:
        try:
            e.timestamp = datetime.fromisoformat(ts)
        except ValueError:
            pass
    for field in (payload.get("fields") or [])[:25]:
        e.add_field(
            name=strip_wikilinks(str(field.get("name") or "—"))[:256],
            value=strip_wikilinks(str(field.get("value") or "—"))[:1024],
            inline=bool(field.get("inline", False)),
        )
    footer = payload.get("footer")
    if footer:
        e.set_footer(text=strip_wikilinks(str(footer))[:2048])
    return e


def embed_queue_path() -> Path:
    """Path to the cross-process embed queue (MCP tools → bot).

    Derived from ``IRIS_DISCORD_HISTORY_DIR`` so it lives in the same
    persistent volume as channel logs and surviving restarts is free.
    """
    history_dir = os.environ.get("IRIS_DISCORD_HISTORY_DIR",
                                 "/claude-auth/discord-channels")
    return Path(history_dir).parent / "pending_embeds.jsonl"
