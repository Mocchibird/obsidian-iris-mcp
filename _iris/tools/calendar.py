"""Calendar/scheduling; Apple integration

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
from ..core import *  # noqa: F401, F403  — all helpers, VaultIndex accessor,
                       # and the task/event parsing helpers (parse_event_line,
                       # parse_iso_date, parse_schedule_section, …)
# Underscore-prefixed names are excluded by `import *`, so we import them
# explicitly.
from ..core import _notify_index_of_write, _notify_index_of_delete
from .tasks import _daily_note_path, format_event_bullet, _ensure_daily_note


# ─── from original L7333-7579: Calendar/scheduling ───
# =============================================================================
# Calendar / scheduling tools
# =============================================================================


@mcp.tool()
def schedule_event(
    date: str,
    time: str,
    title: str,
    end_time: str = "",
    end_date: str = "",
    location: str = "",
    description: str = "",
    all_day: bool = False,
) -> str:
    """
    Add event to a daily note's Schedule section. Creates note if needed.

    ``date`` accepts natural language: "today", "tomorrow", "next monday",
    "in 3 days", or a literal YYYY-MM-DD.

    ``end_date``: for cross-day events, the date the event ends (YYYY-MM-DD
    or natural language). The ``(+Nd)`` marker is computed automatically.

    ``all_day``: if True, formats as ``- all-day Title`` with no time.
    """
    resolved = resolve_natural_date(date)
    if resolved is None:
        return f"Cannot parse date: {date}. Use 'today', 'tomorrow', 'next monday', or YYYY-MM-DD."
    date = resolved
    if not all_day:
        if not re.match(r"^\d{1,2}:\d{2}$", time.strip()):
            return f"time must be HH:MM, got: {time}"
        if end_time.strip() and not re.match(r"^\d{1,2}:\d{2}$", end_time.strip()):
            return f"end_time must be HH:MM, got: {end_time}"
    if not title.strip():
        return "title must not be empty."

    # Resolve end_date for cross-day events
    plus_days = 0
    resolved_end_date = ""
    if end_date.strip():
        resolved_end_date = resolve_natural_date(end_date)
        if resolved_end_date is None:
            return f"Cannot parse end_date: {end_date}"
        try:
            d_start = datetime.strptime(date, "%Y-%m-%d").date()
            d_end = datetime.strptime(resolved_end_date, "%Y-%m-%d").date()
            plus_days = (d_end - d_start).days
            if plus_days < 0:
                return f"end_date ({resolved_end_date}) is before start date ({date})."
        except ValueError:
            pass

    note = _ensure_daily_note(date)
    text = read_text(note)
    bullet = format_event_bullet(
        time=time.strip() if not all_day else "",
        title=title.strip(),
        end_time=end_time.strip() if not all_day else "",
        location=location.strip(),
        description=description.strip(),
        all_day=all_day,
        plus_days=plus_days,
    )

    # Insert event into ## Schedule in sorted order by time
    text = _insert_schedule_bullet(text, bullet, time.strip() if not all_day else "")

    note.write_text(text, encoding="utf-8")
    _notify_index_of_write(note, text=text)
    rel = relative_to_vault(note)
    result = f"ok {rel} {'all-day' if all_day else time} {title}"
    if resolved_end_date and resolved_end_date != date:
        result += f" (→{resolved_end_date})"
    return result


def _insert_schedule_bullet(text: str, bullet: str, time_str: str) -> str:
    """Insert a bullet into the ## Schedule section in sorted order by time."""
    bounds = find_section_bounds(text, "Schedule")
    if bounds is None:
        return text.rstrip() + "\n\n## Schedule\n\n" + bullet + "\n"

    start, end = bounds
    section = text[start:end]
    lines = section.splitlines(keepends=True)
    insert_idx = len(lines)  # default: append at end
    if time_str:
        time_val = time_str.zfill(5)
        for i, line in enumerate(lines):
            ev = parse_event_line(line)
            if ev and ev.get("time", "").zfill(5) > time_val:
                insert_idx = i
                break
    else:
        # All-day events go at the top (after the heading line)
        for i, line in enumerate(lines):
            if parse_event_line(line):
                insert_idx = i
                break
    lines.insert(insert_idx, bullet + "\n")
    return text[:start] + "".join(lines) + text[end:]


@mcp.tool()
def remove_event(date: str, match: str) -> str:
    """Remove a matching event from a daily note."""
    if parse_iso_date(date) is None:
        return f"date must be YYYY-MM-DD, got: {date}"
    rel = _daily_note_path(date)
    note = safe_path(rel)
    if not note.exists():
        return f"No daily note for {date}."

    text = read_text(note)
    bounds = find_section_bounds(text, "Schedule")
    if bounds is None:
        return f"No ## Schedule section in {rel}."

    start, end = bounds
    section = text[start:end]
    needle = match.strip().lower()
    lines = section.splitlines(keepends=True)
    matches = [(i, line) for i, line in enumerate(lines) if parse_event_line(line) and needle in line.lower()]

    if not matches:
        return f"No event matching '{match}' found in {date}."
    if len(matches) > 1:
        preview = "; ".join(l.strip() for _, l in matches[:5])
        return f"{len(matches)} events match '{match}'. Be more specific: {preview}"

    idx, _ = matches[0]
    del lines[idx]
    text = text[:start] + "".join(lines) + text[end:]
    note.write_text(text, encoding="utf-8")
    _notify_index_of_write(note, text=text)
    return f"ok {date}"


@mcp.tool()
def daily_agenda(date: str = "today", days: int = 1) -> str:
    """
    Show agenda: schedule, tasks, reminders for a date or range.

    ``date`` accepts:
      - Single dates: "today", "tomorrow", "next monday", "in 3 days", YYYY-MM-DD
      - Ranges: "this week", "next week", "this month", "next 3 days",
        "next 2 weeks", "7 days"

    When a range expression is used, ``days`` is auto-calculated.
    Otherwise ``days`` controls how many days to show (default 1).
    """
    # Try range expression first  (e.g. "this week", "next 3 days")
    range_result = _resolve_date_range(date)
    if range_result is not None:
        resolved, days = range_result
    else:
        resolved = resolve_natural_date(date)
        if resolved is None:
            return (
                f"Cannot parse date: {date!r}. "
                "Use 'today', 'tomorrow', 'this week', 'next 3 days', "
                "'next monday', or YYYY-MM-DD."
            )

    start_date = datetime.strptime(resolved, "%Y-%m-%d").date()
    today = datetime.now().date()

    end_date = start_date + timedelta(days=max(1, days) - 1)
    date_from = start_date.isoformat()
    date_to = end_date.isoformat()

    idx = get_vault_index()

    # Events
    events = idx.query_events(date_from=date_from, date_to=date_to)

    # Tasks due in this window
    all_tasks = idx.query_tasks(checked=False, limit=500)
    tasks_in_range = []
    tasks_overdue = []
    for t in all_tasks:
        due_dt = parse_iso_date(t["due"])
        if due_dt is None:
            continue  # skip no-date tasks for agenda
        due_date = due_dt.date()
        if due_date < start_date:
            tasks_overdue.append(t)
        elif start_date <= due_date <= end_date:
            tasks_in_range.append(t)

    # Reminders due in this window
    all_reminders = idx.query_reminders(checked=False, limit=500)
    reminders_in_range = []
    reminders_overdue = []
    for r in all_reminders:
        r_dt = parse_iso_date(r["remind_on"])
        if r_dt is None:
            continue
        r_date = r_dt.date()
        if r_date < start_date:
            reminders_overdue.append(r)
        elif start_date <= r_date <= end_date:
            reminders_in_range.append(r)

    # Format output
    if days == 1:
        header = f"📅 Agenda for {date_from}"
        if start_date == today:
            header += " (today)"
        elif start_date == today + timedelta(days=1):
            header += " (tomorrow)"
    else:
        header = f"📅 Agenda: {date_from} → {date_to}"

    lines = [header]
    if events:
        lines.append(f"[events:{len(events)}]")
        for ev in events:
            if ev.get("all_day"):
                t = "all-day"
            else:
                t = ev["time"] + (f"-{ev['end_time']}" if ev["end_time"] else "")
            label = f"{ev['date']}|{t}|{ev['title']}"
            end_d = ev.get("end_date", "")
            if end_d and end_d != ev["date"]:
                label += f" (→{end_d})"
            lines.append(label)
    if tasks_overdue:
        lines.append(f"[overdue-tasks:{len(tasks_overdue)}]")
        lines.extend(f"{t['text']}|{t['due']}" for t in tasks_overdue)
    if tasks_in_range:
        lines.append(f"[tasks:{len(tasks_in_range)}]")
        lines.extend(f"{t['text']}|{t['due']}" for t in tasks_in_range)
    if reminders_overdue:
        lines.append(f"[overdue-reminders:{len(reminders_overdue)}]")
        lines.extend(f"{r['text']}|{r['remind_on']}" for r in reminders_overdue)
    if reminders_in_range:
        lines.append(f"[reminders:{len(reminders_in_range)}]")
        lines.extend(f"{r['text']}|{r['remind_on']}" for r in reminders_in_range)
    if not events and not tasks_in_range and not tasks_overdue and not reminders_in_range and not reminders_overdue:
        lines.append("clear")
    return "\n".join(lines)



# ─── from original L8405-8533: Apple integration ───
# =============================================================================
# Apple Integration (delegates to vault_cron.py)
# =============================================================================

_VAULT_CRON = str(Path(__file__).resolve().parent.parent.parent / "vault_cron.py")


def _run_vault_cron(*args: str, timeout: int = 30) -> str:
    """Run a vault_cron.py subcommand and return its stdout."""
    cmd = [sys.executable, _VAULT_CRON, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "VAULT_ROOT": str(get_vault_root())})
        output = r.stdout.strip()
        if r.returncode != 0:
            err = r.stderr.strip() or "unknown error"
            return f"vault_cron error: {err}\n{output}"
        return output or "Done (no output)."
    except subprocess.TimeoutExpired:
        return f"vault_cron timed out after {timeout}s"
    except Exception as e:
        return f"vault_cron failed: {e}"


@mcp.tool()
def sync_apple(dry_run: bool = False) -> str:
    """Trigger bidirectional sync between Obsidian and Apple Reminders/Calendar.

    - Pushes due tasks and reminders to the Apple Reminders "Vault" list
    - Pulls completions from Apple (e.g. checked off on iPhone) back into Obsidian
    - Syncs vault events to the Apple "Vault" calendar
    - Creates today's daily note if missing

    This is the same sync that runs automatically at 09:30 via launchd.
    Call it anytime to force an immediate sync.
    """
    args = ["sync"]
    if dry_run:
        args.append("--dry-run")
    return _run_vault_cron(*args)


@mcp.tool()
def pull_apple_calendar(date: str = "today") -> str:
    """Pull events from ALL Apple Calendars into the daily note's ## Schedule.

    Reads events from Home, Work, Proton Calendar, ETHZ, Gmail, Church, etc.
    and inserts them as schedule bullets in the daily note.
    Deduplicates — safe to call multiple times.

    Args:
        date: Target date — "today", "tomorrow", or YYYY-MM-DD.
    """
    resolved = resolve_natural_date(date)
    if resolved is None:
        return f"Cannot parse date: {date}"
    return _run_vault_cron("pull-calendar", "--date", resolved)


@mcp.tool()
def evening_wrapup(date: str = "today") -> str:
    """Generate an end-of-day summary and append to the daily note.

    Summarizes: calendar events attended, tasks completed, reminders done,
    and notes modified today.  Appends a ### Daily Summary block to ## Notes.

    This runs automatically at 22:00 via launchd, but can be triggered manually.

    Args:
        date: Target date — "today" or YYYY-MM-DD.
    """
    resolved = resolve_natural_date(date)
    if resolved is None:
        return f"Cannot parse date: {date}"
    return _run_vault_cron("wrapup", "--date", resolved)


@mcp.tool()
def pull_health_snapshot(date: str = "today", dry_run: bool = False) -> str:
    """Pull an Apple Health snapshot and write it into the daily note.

    Runs a user-defined Apple Shortcut (default name: ``Iris Health``,
    configurable via ``IRIS_HEALTH_SHORTCUT`` env or ``[apple].health_shortcut``
    in ``~/.config/iris/config.toml``). The shortcut can return any text —
    newline-delimited metrics, JSON, prose — and Iris drops the output verbatim
    into today's daily note's ``## Health`` section. Re-running replaces the
    existing section (idempotent).

    To set up: in macOS Shortcuts.app create a shortcut named ``Iris Health``
    that fetches Health Samples (sleep, steps, HRV, weight, workouts, etc.) and
    returns them as text. macOS only.

    Args:
        date: Target date — "today" or YYYY-MM-DD.
        dry_run: Show what would be written without modifying the note.
    """
    resolved = resolve_natural_date(date)
    if resolved is None:
        return f"Cannot parse date: {date}"
    args = ["health", "--date", resolved]
    if dry_run:
        args.append("--dry-run")
    return _run_vault_cron(*args, timeout=60)


@mcp.tool()
def weekly_summary(date: str = "today", force: bool = False, dry_run: bool = False) -> str:
    """Generate and save a weekly summary note for the ISO week containing the given date.

    Writes to 30_Episodic/{iso_year}/Weekly/{iso_year}-W{NN}.md.
    Summarizes: tasks completed, reminders done, calendar events,
    notes touched, and open tasks carried over.

    Skips if the file already exists unless ``force=True``.

    Args:
        date: Any day in the target ISO week — "today" or YYYY-MM-DD.
        force: Overwrite the file if it already exists.
        dry_run: Build the summary but do not write or notify.
    """
    resolved = resolve_natural_date(date)
    if resolved is None:
        return f"Cannot parse date: {date}"
    args = ["weekly-summary", "--end-date", resolved]
    if force:
        args.append("--force")
    if dry_run:
        args.append("--dry-run")
    return _run_vault_cron(*args, timeout=60)


@mcp.tool()
def get_focus_context(mode: str = "") -> str:
    """Get the current macOS Focus mode and show relevant vault context.

    Maps Focus modes to projects and tasks:
      - Work → PTO Kernels, Ascend-related tasks
      - Personal → Homelab, TrueNAS, MochiMind
      - Study → Japanese Study, Languages, ETHZ

    Args:
        mode: Override Focus mode instead of auto-detecting.
              Use "Work", "Personal", or "Study". Leave empty to auto-detect.
    """
    args = ["focus"]
    if mode.strip():
        args.extend(["--mode", mode.strip()])
    return _run_vault_cron(*args)


@mcp.tool()
def run_apple_shortcut(name: str) -> str:
    """Run an Apple Shortcut by name and return its output.

    Use this to trigger automations the user has set up in the Shortcuts app.
    Pass name="" or use list=True to see all available shortcuts.

    Args:
        name: The shortcut name. Pass empty string to list all shortcuts.
    """
    if not name.strip():
        return _run_vault_cron("shortcut", "--list")
    return _run_vault_cron("shortcut", name.strip(), timeout=60)


@mcp.tool()
def morning_routine(dry_run: bool = False) -> str:
    """Run the full morning routine: daily note + calendar pull + Apple sync.

    Equivalent to what runs automatically at 09:30 via launchd:
      1. Creates today's daily note
      2. Pulls ALL Apple Calendar events into ## Schedule
      3. Syncs tasks/reminders bidirectionally with Apple Reminders
      4. Sends a macOS notification summary

    Call this if you missed the 09:30 run or want a fresh sync.
    """
    args = ["morning"]
    if dry_run:
        args.append("--dry-run")
    return _run_vault_cron(*args, timeout=60)


