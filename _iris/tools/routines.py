"""Morning/weekly; Session context

@mcp.tool() definitions live here. The shared FastMCP instance is imported
from the package __init__.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
import uuid
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Optional

from .. import mcp
from ..core import *  # noqa: F401, F403  — includes parse_iso_date


# ─── from original L7651-7829: Morning/weekly ───
# =============================================================================
# Morning briefing & weekly review
# =============================================================================


def _llm_prose_summary(
    role: str,
    structured_data: str,
    *,
    max_tokens: int = 200,
) -> str:
    """Optionally generate a leading prose paragraph for a routine summary.

    Returns empty string when no LLM is configured — callers should treat
    prose as an enhancement, not a requirement.

    ``role``: short label of what we're summarizing ("morning briefing",
    "weekly review", "evening wrapup"). Goes into the system prompt.
    """
    try:
        from .. import llm
    except ImportError:
        return ""
    if not llm.is_configured():
        return ""
    system = (
        f"You are Iris, a friendly personal-vault assistant writing a {role} "
        "for the user (Hyun-Min). Given the structured data below, write a "
        "single short paragraph (2–4 sentences) that captures the highlights "
        "in a natural voice. Don't enumerate everything — pick what matters. "
        "Don't use bullet points; the structured list follows separately."
    )
    try:
        return llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": structured_data},
            ],
            max_tokens=max_tokens,
            temperature=0.6,
        ).strip()
    except llm.LLMError:
        return ""


@mcp.tool()
def morning_briefing(date: str = "today") -> str:
    """
    Comprehensive daily overview: schedule, tasks, reminders, inbox, projects.

    If an LLM is configured (IRIS_LLM_MODEL), a short prose summary is
    prepended above the structured sections. Otherwise the structured
    output is returned alone.

    ``date`` accepts natural language: "today", "tomorrow", etc.
    """
    resolved = resolve_natural_date(date)
    if resolved is None:
        return f"Cannot parse date: {date}"
    d = datetime.strptime(resolved, "%Y-%m-%d").date()
    today = datetime.now().date()
    idx = get_vault_index()

    lines: list[str] = []

    # Header
    day_name = d.strftime("%A")
    if d == today:
        lines.append(f"# Good morning! {day_name}, {resolved}")
    else:
        lines.append(f"# Briefing for {day_name}, {resolved}")

    # 1. Schedule
    events = idx.query_events(date_from=resolved, date_to=resolved)
    lines.append(f"\n## Schedule ({len(events)} events)")
    if events:
        for ev in events:
            t = ev["time"] + (f"–{ev['end_time']}" if ev["end_time"] else "")
            loc = f" @ {ev['location']}" if ev["location"] else ""
            lines.append(f"- {t} {ev['title']}{loc}")
    else:
        lines.append("- No events scheduled.")

    # 2. Overdue & today tasks
    all_tasks = idx.query_tasks(checked=False, limit=500)
    overdue: list[dict] = []
    due_today: list[dict] = []
    upcoming: list[dict] = []
    for t in all_tasks:
        due_dt = parse_iso_date(t["due"])
        if due_dt is None:
            continue
        due_date = due_dt.date()
        if due_date < d:
            overdue.append(t)
        elif due_date == d:
            due_today.append(t)
        elif due_date <= d + timedelta(days=3):
            upcoming.append(t)

    if overdue:
        lines.append(f"\n## Overdue Tasks ({len(overdue)})")
        for t in overdue:
            lines.append(f"- [ ] {t['text']} (due {t['due']}) — {t['note_path']}")
    if due_today:
        lines.append(f"\n## Today's Tasks ({len(due_today)})")
        for t in due_today:
            lines.append(f"- [ ] {t['text']} — {t['note_path']}")
    if upcoming:
        lines.append(f"\n## Upcoming Tasks ({len(upcoming)})")
        for t in upcoming[:10]:
            lines.append(f"- [ ] {t['text']} (due {t['due']})")

    # 3. Reminders
    all_reminders = idx.query_reminders(checked=False, limit=500)
    remind_overdue: list[dict] = []
    remind_today: list[dict] = []
    for r in all_reminders:
        r_dt = parse_iso_date(r["remind_on"])
        if r_dt is None:
            continue
        r_date = r_dt.date()
        if r_date < d:
            remind_overdue.append(r)
        elif r_date == d:
            remind_today.append(r)

    if remind_overdue or remind_today:
        lines.append(f"\n## Reminders")
        for r in remind_overdue:
            lines.append(f"- ⚠️ OVERDUE: {r['text']} (was {r['remind_on']})")
        for r in remind_today:
            lines.append(f"- 🔔 {r['text']}")

    # 4. Unfinished items from recent daily notes (only when briefing TODAY)
    if d == today:
        try:
            from .tasks import _collect_unfinished_in_daily_notes
            unfinished = _collect_unfinished_in_daily_notes(days_back=7)
            if unfinished:
                lines.append(f"\n## Unfinished from Recent Days ({len(unfinished)})")
                for item in unfinished[:10]:
                    p = item["parsed"]
                    lines.append(f"- {item['date']} {item['section'][:1]}| {p['text']}")
                if len(unfinished) > 10:
                    lines.append(f"- _…and {len(unfinished) - 10} more_")
                lines.append(
                    "→ Say _\"roll them forward\"_ and I'll move these to today "
                    "with `carry_forward_tasks` (originals stay unchecked and get "
                    "a `rolled:` marker)."
                )
        except ImportError:
            pass

    # 5. Inbox count
    root = get_vault_root()
    inbox_dir = root / "90_Inbox" / "inbox"
    inbox_count = len(list(inbox_dir.glob("*.md"))) if inbox_dir.is_dir() else 0
    if inbox_count > 0:
        lines.append(f"\n## Inbox")
        lines.append(f"- {inbox_count} item(s) awaiting triage")

    # 5. Active projects summary
    c = idx.conn
    project_rows = c.execute(
        "SELECT path, title FROM notes WHERE type = 'project' AND status = 'active' ORDER BY path"
    ).fetchall()
    if project_rows:
        lines.append(f"\n## Active Projects ({len(project_rows)})")
        for pr in project_rows:
            lines.append(f"- {make_wikilink(pr['path'], pr['title'])}")

    structured = "\n".join(lines)
    prose = _llm_prose_summary("morning briefing", structured, max_tokens=180)
    if prose:
        return f"{lines[0]}\n\n_{prose}_\n\n" + "\n".join(lines[1:])
    return structured


@mcp.tool()
def weekly_review(date: str = "today") -> str:
    """
    Summarize the past 7 days: events attended, tasks completed/added,
    notes created/modified.

    ``date`` accepts natural language or YYYY-MM-DD. The review covers
    the 7 days ending on that date (inclusive).
    """
    resolved = resolve_natural_date(date)
    if resolved is None:
        return f"Cannot parse date: {date}"
    end_date = datetime.strptime(resolved, "%Y-%m-%d").date()
    start_date = end_date - timedelta(days=6)
    date_from = start_date.isoformat()
    date_to = end_date.isoformat()

    idx = get_vault_index()
    lines: list[str] = [f"# Weekly Review: {date_from} → {date_to}"]

    # Events
    events = idx.query_events(date_from=date_from, date_to=date_to)
    lines.append(f"\n## Events ({len(events)})")
    if events:
        current_day = ""
        for ev in events:
            if ev["date"] != current_day:
                current_day = ev["date"]
                lines.append(f"### {current_day}")
            t = ev["time"] + (f"–{ev['end_time']}" if ev["end_time"] else "")
            lines.append(f"- {t} {ev['title']}")
    else:
        lines.append("- No events this week.")

    # Completed tasks
    done_tasks = idx.query_tasks(checked=True, limit=500)
    week_done = [
        t for t in done_tasks
        if t.get("done") and date_from <= t["done"] <= date_to
    ]
    lines.append(f"\n## Completed Tasks ({len(week_done)})")
    if week_done:
        for t in week_done[:30]:
            lines.append(f"- [x] {t['text']} (done {t['done']})")
    else:
        lines.append("- None completed this week.")

    # Open tasks remaining
    open_tasks = idx.query_tasks(checked=False, limit=500)
    overdue = [t for t in open_tasks if t["due"] and t["due"] < date_from]
    lines.append(f"\n## Open Tasks ({len(open_tasks)} total, {len(overdue)} overdue)")

    # Recently modified notes
    c = idx.conn
    # Use mtime_ns from files table to find recently updated notes
    cutoff_ns = int(datetime.combine(start_date, datetime.min.time()).timestamp() * 1e9)
    recent_rows = c.execute(
        "SELECT f.path, n.title FROM files f JOIN notes n ON f.path = n.path "
        "WHERE f.mtime_ns >= ? ORDER BY f.mtime_ns DESC LIMIT 30",
        (cutoff_ns,),
    ).fetchall()
    lines.append(f"\n## Notes Modified ({len(recent_rows)})")
    for r in recent_rows[:20]:
        lines.append(f"- {make_wikilink(r['path'], r['title'])}")

    structured = "\n".join(lines)
    prose = _llm_prose_summary("weekly review", structured, max_tokens=220)
    if prose:
        return f"{lines[0]}\n\n_{prose}_\n\n" + "\n".join(lines[1:])
    return structured



# ─── from original L8298-8404: Session context ───
# =============================================================================
# Session context — lightweight boot info for every conversation
# =============================================================================


@mcp.tool()
def get_session_context() -> str:
    """Return essential context for the current session.

    **Call this at the start of EVERY conversation** before doing anything else.
    It gives you the current date, time, timezone, user basics, and a quick
    snapshot of today's agenda so you never have to guess or infer these.

    Returns a structured block you can reference throughout the conversation.
    """
    import time as _time

    now = datetime.now()
    today = now.date()
    tz_name = _time.tzname[_time.daylight] if _time.daylight else _time.tzname[0]
    try:
        utc_offset_h = -(_time.timezone if _time.daylight == 0 else _time.altzone) / 3600
        utc_str = f"UTC{'+' if utc_offset_h >= 0 else ''}{utc_offset_h:g}"
    except Exception:
        utc_str = ""

    lines = [
        "## Session Context",
        f"date: {today.isoformat()} ({today.strftime('%A')})",
        f"time: {now.strftime('%H:%M')} {tz_name} ({utc_str})",
    ]

    # ── User basics (read from profile note if available) ──
    idx = get_vault_index()
    profile_path = "10_Profile/User Profile.md"
    try:
        root = get_vault_root()
        pf = (root / profile_path).read_text(encoding="utf-8")
        # Extract name from first H1
        m = re.search(r"^#\s+(.+)", pf, re.MULTILINE)
        if m:
            lines.append(f"user: {m.group(1).strip()}")
        # Extract location
        m = re.search(r"\*\*Location\*\*:\s*(.+)", pf)
        if m:
            lines.append(f"location: {m.group(1).strip()}")
        # Extract current role
        m = re.search(r"\*\*Current Role\*\*:\s*(.+)", pf)
        if m:
            lines.append(f"role: {m.group(1).strip()}")
    except Exception:
        pass

    # ── Today's quick snapshot ──
    events = idx.query_events(date_from=today.isoformat(), date_to=today.isoformat())
    all_tasks = idx.query_tasks(checked=False, limit=500)
    overdue_tasks = 0
    today_tasks = 0
    for t in all_tasks:
        due_dt = parse_iso_date(t["due"])
        if due_dt is None:
            continue
        d = due_dt.date()
        if d < today:
            overdue_tasks += 1
        elif d == today:
            today_tasks += 1

    all_reminders = idx.query_reminders(checked=False, limit=500)
    today_reminders = 0
    overdue_reminders = 0
    for r in all_reminders:
        r_dt = parse_iso_date(r["remind_on"])
        if r_dt is None:
            continue
        d = r_dt.date()
        if d < today:
            overdue_reminders += 1
        elif d == today:
            today_reminders += 1

    snap_parts = []
    if events:
        snap_parts.append(f"{len(events)} events")
    if today_tasks:
        snap_parts.append(f"{today_tasks} tasks due")
    if overdue_tasks:
        snap_parts.append(f"{overdue_tasks} overdue tasks")
    if today_reminders:
        snap_parts.append(f"{today_reminders} reminders")
    if overdue_reminders:
        snap_parts.append(f"{overdue_reminders} overdue reminders")
    lines.append(f"today: {', '.join(snap_parts) if snap_parts else 'clear schedule'}")

    # ── Next event (if any today) ──
    future_events = [
        ev for ev in events
        if ev["time"] and ev["time"] > now.strftime("%H:%M")
    ]
    if future_events:
        nxt = future_events[0]
        t = nxt["time"] + (f"–{nxt['end_time']}" if nxt["end_time"] else "")
        lines.append(f"next_event: {t} {nxt['title']}")

    return "\n".join(lines)


