"""Discord-context tools.

These only do anything when the MCP server is launched by the Discord bot
(``docker/bot.py``), which writes a rolling per-channel JSONL log to
``IRIS_DISCORD_HISTORY_DIR`` and passes the active channel ID via env. From
Claude Desktop / other MCP clients they no-op gracefully.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import mcp


def _history_path() -> Path | None:
    channel_id = os.environ.get("IRIS_DISCORD_CHANNEL_ID")
    if not channel_id:
        return None
    history_dir = os.environ.get("IRIS_DISCORD_HISTORY_DIR",
                                 "/claude-auth/discord-channels")
    return Path(history_dir) / f"{channel_id}.jsonl"


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
