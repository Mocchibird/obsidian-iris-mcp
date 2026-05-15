"""Tasks + frontmatter editing; Reminders + dashboards; Spaced repetition

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
from ..core import *  # noqa: F401, F403  — all helpers and VaultIndex accessor


# ─── from original L1715-2380: Tasks + frontmatter editing ───
# =============================================================================
# Frontmatter and task tools
# =============================================================================


@mcp.tool()
def set_note_tags(path: str, tags: list[str], merge: bool = True) -> str:
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    text = read_text(note)
    data, body = split_frontmatter(text)
    new_tags = unique_preserve_order([str(t).lstrip("#") for t in tags])

    if merge:
        old = data.get("tags", [])
        old_tags = [old] if isinstance(old, str) else [str(t) for t in old] if isinstance(old, list) else []
        data["tags"] = unique_preserve_order(old_tags + new_tags)
    else:
        data["tags"] = new_tags

    new_text = dump_frontmatter(data, body)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"


@mcp.tool()
def add_note_aliases(path: str, aliases: list[str]) -> str:
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    text = read_text(note)
    data, body = split_frontmatter(text)
    old = data.get("aliases", [])
    old_aliases = [old] if isinstance(old, str) else [str(a) for a in old] if isinstance(old, list) else []
    data["aliases"] = unique_preserve_order(old_aliases + [str(a) for a in aliases])
    new_text = dump_frontmatter(data, body)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"


@mcp.tool()
def set_frontmatter_field(path: str, key: str, value: str) -> str:
    if not SAFE_FRONTMATTER_KEY_RE.match(key):
        return "Invalid frontmatter key."

    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    text = read_text(note)
    data, body = split_frontmatter(text)
    data[key] = value
    new_text = dump_frontmatter(data, body)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"




def format_event_bullet(
    time: str, title: str, end_time: str = "",
    location: str = "", description: str = "",
    all_day: bool = False, plus_days: int = 0,
) -> str:
    """Format an event as a schedule bullet line.

    ``plus_days``: how many days after start the end_time falls.
    E.g. a flight departing 22:00 and arriving next day at 06:00 → plus_days=1.
    """
    if all_day:
        line = f"- all-day {title.strip()}"
    else:
        line = f"- {time}"
        if end_time:
            line += f"–{end_time}"
            if plus_days:
                line += f" (+{plus_days}d)"
        line += f" {title.strip()}"
    if location:
        line += f" @ {location.strip()}"
    if description:
        line += f" — {description.strip()}"
    return line


def _daily_note_path(date: str) -> str:
    """Return the vault-relative path for a daily note. Creates parent dirs."""
    year = date[:4]
    return f"30_Episodic/{year}/{date}.md"


def _ensure_daily_note(date: str) -> Path:
    """Get or create the daily note for a given date."""
    rel = _daily_note_path(date)
    note = safe_path(rel)
    if not note.exists():
        # Determine day of week
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            day_name = dt.strftime("%A")
        except ValueError:
            day_name = ""
        data: dict[str, object] = {
            "type": "daily",
            "date": date,
            "tags": ["daily"],
        }
        body_lines = [
            f"# {date}" + (f" — {day_name}" if day_name else ""),
            "",
            "## Schedule",
            "",
            "## Tasks",
            "",
            "## Reminders",
            "",
            "## Notes",
            "",
        ]
        note.parent.mkdir(parents=True, exist_ok=True)
        new_text = dump_frontmatter(data, NL.join(body_lines))
        note.write_text(new_text, encoding="utf-8")
        _notify_index_of_write(note, text=new_text)
    return note


def replace_line_at(text: str, line_start: int, old_line: str, new_line: str) -> str:
    line_end = line_start + len(old_line)
    return text[:line_start] + new_line + text[line_end:]


def _find_unique_task_match(text: str, section: str, match: str) -> tuple[int, str, dict[str, Any]] | str:
    if not match.strip():
        return "match must not be empty."
    needle = match.strip().lower()
    candidates = [item for item in find_task_lines_in_section(text, section) if needle in item[2]["text"].lower()]
    if not candidates:
        return f"No task in ## {section} contains: {match}"
    if len(candidates) > 1:
        preview = "; ".join(c[2]["text"] for c in candidates[:5])
        return f"{len(candidates)} tasks in ## {section} match {match!r}. Use a longer substring. Matches: {preview}"
    return candidates[0]


@mcp.tool()
def add_task(path: str, task: str, due: str = "", priority: str = "") -> str:
    """Add a task to a note's ## Tasks section.

    Args:
        path: Relative vault path to the note.
              For project tasks use the project note (e.g. ``20_Projects/PTO Kernels.md``).
              For personal to-dos use ``10_Profile/Personal/General To-Do 2026.md``
              or today's daily note (``30_Episodic/YYYY/YYYY-MM-DD.md``).
        task: The task description.
        due: Optional due date — accepts ``YYYY-MM-DD`` or natural language
             (``tomorrow``, ``next Friday``, ``in 3 days``).
        priority: Optional priority level (``high``, ``medium``, ``low``).
    """
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    if due.strip():
        resolved = resolve_natural_date(due)
        if resolved:
            due = resolved

    bullet = format_task_bullet(task, due=due, priority=priority, checked=False)
    text = read_text(note)
    new_text = append_bullet_to_section(text, "Tasks", bullet)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"


@mcp.tool()
def complete_task(path: str, match: str, done: str = "") -> str:
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    text = read_text(note)
    found = _find_unique_task_match(text, "Tasks", match)
    if isinstance(found, str):
        return found

    line_start, raw, parsed = found
    if parsed["checked"]:
        return f"Task already complete: {parsed['text']}"

    new_line = format_task_bullet(
        parsed["text"],
        due=parsed["due"],
        priority=parsed["priority"],
        done=done.strip() or today_iso(),
        remind_on=parsed["remind_on"],
        repeat=parsed["repeat"],
        task_id=parsed["id"],
        extra=parsed["extra"],
        checked=True,
        indent=parsed["indent"],
    )
    updated_text = replace_line_at(text, line_start, raw, new_line)
    note.write_text(updated_text, encoding="utf-8")
    _notify_index_of_write(note, text=updated_text)
    return f"ok {path}"


@mcp.tool()
def update_task(
    path: str,
    match: str,
    new_text: str = "",
    due: str = "",
    priority: str = "",
    clear_due: bool = False,
    clear_priority: bool = False,
) -> str:
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    text = read_text(note)
    found = _find_unique_task_match(text, "Tasks", match)
    if isinstance(found, str):
        return found

    line_start, raw, parsed = found
    next_due = "" if clear_due else (due.strip() or parsed["due"])
    next_priority = "" if clear_priority else (priority.strip() or parsed["priority"])
    next_text = new_text.strip() or parsed["text"]

    new_line = format_task_bullet(
        next_text,
        due=next_due,
        priority=next_priority,
        done=parsed["done"],
        remind_on=parsed["remind_on"],
        repeat=parsed["repeat"],
        task_id=parsed["id"],
        extra=parsed["extra"],
        checked=parsed["checked"],
        indent=parsed["indent"],
    )
    if new_line == raw:
        return f"No change for task: {parsed['text']}"

    updated_text = replace_line_at(text, line_start, raw, new_line)
    note.write_text(updated_text, encoding="utf-8")
    _notify_index_of_write(note, text=updated_text)
    return f"ok {path}"


# @mcp.tool()  # removed — use sqlite_query instead
def list_tasks(limit: int = 100) -> str:
    limit = max(1, min(limit, 1000))
    idx = get_vault_index()
    results = idx.query_tasks(checked=False, limit=limit)

    if not results:
        return "No unchecked tasks found."

    return "\n".join(
        f"{item['note_path']}|{item['text']}|{item.get('due','')}|{item.get('priority','')}"
        for item in results
    )


# ── Window normalization (shared by list_tasks_due / list_reminders_due) ──

_WINDOW_ALIASES: dict[str, tuple[str, int | None]] = {
    # canonical values (no days override)
    "today":      ("today", None),
    "overdue":    ("overdue", None),
    "upcoming":   ("upcoming", None),
    "all":        ("all", None),
    "no-date":    ("no-date", None),
    "no date":    ("no-date", None),
    "undated":    ("no-date", None),
    "everything": ("all", None),
    # natural-language aliases → upcoming + days override
    "week":           ("upcoming", 7),
    "this week":      ("upcoming", 7),
    "next week":      ("upcoming", 14),
    "next 7 days":    ("upcoming", 7),
    "7 days":         ("upcoming", 7),
    "7days":          ("upcoming", 7),
    "3 days":         ("upcoming", 3),
    "next 3 days":    ("upcoming", 3),
    "3days":          ("upcoming", 3),
    "tomorrow":       ("upcoming", 2),
    "2 weeks":        ("upcoming", 14),
    "next 2 weeks":   ("upcoming", 14),
    "14 days":        ("upcoming", 14),
    "month":          ("upcoming", 30),
    "this month":     ("upcoming", 30),
    "next month":     ("upcoming", 60),
    "next 30 days":   ("upcoming", 30),
    "30 days":        ("upcoming", 30),
    "30days":         ("upcoming", 30),
    "quarter":        ("upcoming", 90),
    "next quarter":   ("upcoming", 90),
    "90 days":        ("upcoming", 90),
}


def _normalize_window(raw: str, default_days: int = 7) -> tuple[str, int]:
    """
    Normalize a window parameter into (canonical_window, days).

    Accepts any value from ``_WINDOW_ALIASES`` plus patterns like
    ``"N days"``, ``"N weeks"``, ``"next N months"``.
    """
    val = (raw or "today").strip().lower()

    # Direct match
    if val in _WINDOW_ALIASES:
        w, d = _WINDOW_ALIASES[val]
        return (w, d if d is not None else default_days)

    # "N days/weeks/months" or "next N days/weeks/months"
    m = re.match(r"(?:next\s+)?(\d+)\s*(day|days|week|weeks|month|months)", val)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("day"):
            return ("upcoming", n)
        if unit.startswith("week"):
            return ("upcoming", n * 7)
        if unit.startswith("month"):
            return ("upcoming", n * 30)

    # "today+overdue", "today+upcoming" — combo shorthand
    if "+" in val:
        parts = [p.strip() for p in val.split("+")]
        valid = {"today", "overdue", "upcoming", "no-date"}
        if all(p in valid for p in parts):
            return ("all", default_days)   # show everything

    # Fallback — return as-is; caller checks validity
    valid = {"today", "overdue", "upcoming", "all", "no-date"}
    if val in valid:
        return (val, default_days)

    # Best-effort: unknown → treat as 'all' so the LLM gets data
    return ("all", default_days)


@mcp.tool()
def list_tasks_due(window: str = "today", days: int = 7, limit: int = 200) -> str:
    """
    List unchecked tasks filtered by time window.

    ``window`` accepts natural language:
      today, overdue, upcoming, all, no-date,
      week, this week, month, this month, 7 days,
      next 3 days, next 2 weeks, or "N days/weeks/months".

    ``days`` sets the look-ahead for 'upcoming' (default 7).
    A natural-language window like "month" overrides days automatically.
    """
    window_norm, days_eff = _normalize_window(window, default_days=days)

    limit = max(1, min(limit, 5000))
    days_eff = max(0, min(days_eff, 365))
    today = datetime.now().date()
    horizon = today + timedelta(days=days_eff) if days_eff > 0 else datetime.max.date()

    idx = get_vault_index()
    all_tasks = idx.query_tasks(checked=False, limit=limit)

    buckets: dict[str, list[dict[str, str]]] = {"overdue": [], "today": [], "upcoming": [], "no-date": []}
    for t in all_tasks:
        item = {"path": t["note_path"], "task": t["text"], "due": t["due"], "priority": t["priority"]}
        due_dt = parse_iso_date(t["due"])
        if due_dt is None:
            buckets["no-date"].append(item)
        else:
            due_date = due_dt.date()
            if due_date < today:
                buckets["overdue"].append(item)
            elif due_date == today:
                buckets["today"].append(item)
            elif due_date <= horizon:
                buckets["upcoming"].append(item)

    show = {
        "today": ["today"],
        "overdue": ["overdue"],
        "upcoming": ["upcoming"],
        "no-date": ["no-date"],
        "all": ["overdue", "today", "upcoming", "no-date"],
    }[window_norm]

    lines: list[str] = []
    for bucket in show:
        items = buckets[bucket]
        items.sort(key=lambda x: (x.get("due", ""), x["path"]))
        lines.append(f"[{bucket}:{len(items)}]")
        for item in items:
            lines.append(f"{item['path']}|{item['task']}|{item.get('due','')}|{item.get('priority','')}")
    return "\n".join(lines).rstrip() or "none"


@mcp.tool()
def add_decision(path: str, decision: str, reason: str = "", source: str = "", date: str = "") -> str:
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    date = date.strip() or today_iso()
    header = "| Date | Decision | Reason | Source |"
    separator = "|---|---|---|---|"
    row = f"| {date} | {escape_table_cell(decision)} | {escape_table_cell(reason)} | {escape_table_cell(source)} |"
    text = read_text(note)
    new_text = append_table_row_to_section(text, "Decisions", header, separator, row)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"


@mcp.tool()
def add_open_question(path: str, question: str, status: str = "open", related_note: str = "", date: str = "") -> str:
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    date = date.strip() or today_iso()
    related = related_note.strip()
    if related:
        related = make_wikilink(related, Path(normalize_note_target(related)).name.replace("_", " ").title())
    header = "| Date | Question | Status | Related |"
    separator = "|---|---|---|---|"
    row = f"| {date} | {escape_table_cell(question)} | {escape_table_cell(status)} | {escape_table_cell(related)} |"
    text = read_text(note)
    new_text = append_table_row_to_section(text, "Open Questions", header, separator, row)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"



# ─── from original L3314-3601: Reminders + dashboards ───
# =============================================================================
# Reminders and dashboards
# =============================================================================


_VALID_REPEAT = {"", "daily", "weekdays", "weekly", "biweekly", "monthly", "quarterly", "yearly"}

# Fuzzy aliases so any LLM (or human) can pass natural-language repeat values.
# Keys are lowercased, stripped, with non-alphanumeric chars removed for matching.
_REPEAT_ALIASES: dict[str, str] = {
    # daily
    "daily": "daily",
    "everyday": "daily",
    "every day": "daily",
    "each day": "daily",
    "once a day": "daily",
    "1 day": "daily",
    "every 1 day": "daily",
    # weekdays
    "weekdays": "weekdays",
    "weekday": "weekdays",
    "monday to friday": "weekdays",
    "mon-fri": "weekdays",
    "mon to fri": "weekdays",
    "business days": "weekdays",
    "work days": "weekdays",
    "workdays": "weekdays",
    "every weekday": "weekdays",
    # weekly
    "weekly": "weekly",
    "every week": "weekly",
    "once a week": "weekly",
    "each week": "weekly",
    "every 7 days": "weekly",
    "7 days": "weekly",
    "1 week": "weekly",
    "every 1 week": "weekly",
    # biweekly
    "biweekly": "biweekly",
    "bi-weekly": "biweekly",
    "biweekly": "biweekly",
    "every 2 weeks": "biweekly",
    "every two weeks": "biweekly",
    "every other week": "biweekly",
    "every 14 days": "biweekly",
    "14 days": "biweekly",
    "2 weeks": "biweekly",
    "two weeks": "biweekly",
    "fortnightly": "biweekly",
    "fortnight": "biweekly",
    "once every 2 weeks": "biweekly",
    "once every two weeks": "biweekly",
    # monthly
    "monthly": "monthly",
    "every month": "monthly",
    "once a month": "monthly",
    "each month": "monthly",
    "1 month": "monthly",
    "every 1 month": "monthly",
    "every 30 days": "monthly",
    "30 days": "monthly",
    # quarterly
    "quarterly": "quarterly",
    "every quarter": "quarterly",
    "every 3 months": "quarterly",
    "every three months": "quarterly",
    "3 months": "quarterly",
    "once a quarter": "quarterly",
    "every 90 days": "quarterly",
    # yearly
    "yearly": "yearly",
    "annually": "yearly",
    "annual": "yearly",
    "every year": "yearly",
    "once a year": "yearly",
    "each year": "yearly",
    "1 year": "yearly",
    "every 1 year": "yearly",
    "every 12 months": "yearly",
    "12 months": "yearly",
    "every 365 days": "yearly",
}


def normalize_repeat(raw: str) -> str:
    """Normalize a repeat value, accepting fuzzy aliases.

    Returns the canonical repeat string (e.g. "biweekly") or "" if empty.
    Raises ValueError with a helpful message if unrecognized.
    """
    val = (raw or "").strip().lower()
    if not val:
        return ""
    # Direct match
    if val in _VALID_REPEAT:
        return val
    # Alias lookup
    if val in _REPEAT_ALIASES:
        return _REPEAT_ALIASES[val]
    # Fuzzy: strip all non-alphanumeric/space, try again
    cleaned = re.sub(r"[^a-z0-9 ]", "", val).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if cleaned in _REPEAT_ALIASES:
        return _REPEAT_ALIASES[cleaned]
    # Try without spaces
    no_space = cleaned.replace(" ", "")
    for alias_key, canonical in _REPEAT_ALIASES.items():
        if alias_key.replace(" ", "") == no_space:
            return canonical
    raise ValueError(
        f"Unknown repeat: {raw!r}. "
        f"Use: daily, weekdays, weekly, biweekly (every 2 weeks), monthly, quarterly, yearly, or leave empty."
    )


def _advance_remind_date(remind_on: str, repeat: str) -> str:
    dt = parse_iso_date(remind_on)
    if dt is None:
        return ""
    repeat = (repeat or "").strip().lower()
    if repeat == "daily":
        nxt = dt + timedelta(days=1)
    elif repeat == "weekdays":
        nxt = dt + timedelta(days=1)
        # Skip to Monday if landing on weekend
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    elif repeat == "weekly":
        nxt = dt + timedelta(days=7)
    elif repeat == "biweekly":
        nxt = dt + timedelta(days=14)
    elif repeat == "monthly":
        import calendar
        year = dt.year + (1 if dt.month == 12 else 0)
        month = 1 if dt.month == 12 else dt.month + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        nxt = dt.replace(year=year, month=month, day=day)
    elif repeat == "quarterly":
        import calendar
        month = dt.month + 3
        year = dt.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        nxt = dt.replace(year=year, month=month, day=day)
    elif repeat == "yearly":
        import calendar
        year = dt.year + 1
        day = min(dt.day, calendar.monthrange(year, dt.month)[1])
        nxt = dt.replace(year=year, day=day)
    else:
        return ""
    return nxt.date().isoformat()


@mcp.tool()
def add_reminder(path: str, reminder: str, remind_on: str, repeat: str = "") -> str:
    """Add a reminder to a note's ## Reminders section.

    Args:
        path: Relative vault path to the note.
              For **recurring** reminders, use a persistent profile/project note
              (NOT a daily note) so it survives across days.
              Example: ``10_Profile/Personal/Mediservice.md``
        reminder: The reminder text.
        remind_on: Due date — accepts ``YYYY-MM-DD`` or natural language
                   (``tomorrow``, ``in 2 weeks``, ``next Monday``).
        repeat: Recurrence interval. Accepts natural language:
                ``daily``, ``weekdays`` (Mon–Fri), ``weekly``,
                ``biweekly`` / ``every 2 weeks`` / ``fortnightly``,
                ``monthly``, ``quarterly``, ``yearly`` / ``annually``.
                Leave empty for one-shot reminders.
    """
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."
    resolved = resolve_natural_date(remind_on)
    if resolved:
        remind_on = resolved
    if parse_iso_date(remind_on) is None:
        return f"remind_on must be YYYY-MM-DD or natural date, got: {remind_on}"
    try:
        repeat_norm = normalize_repeat(repeat)
    except ValueError as e:
        return str(e)

    bullet = format_task_bullet(reminder, remind_on=remind_on, repeat=repeat_norm, checked=False)
    text = read_text(note)
    new_text = append_bullet_to_section(text, "Reminders", bullet)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"


@mcp.tool()
def complete_reminder(path: str, match: str, done: str = "") -> str:
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    text = read_text(note)
    found = _find_unique_task_match(text, "Reminders", match)
    if isinstance(found, str):
        return found
    line_start, raw, parsed = found
    if parsed["checked"]:
        return f"Reminder already complete: {parsed['text']}"

    new_line = format_task_bullet(
        parsed["text"],
        done=done.strip() or today_iso(),
        remind_on=parsed["remind_on"],
        repeat=parsed["repeat"],
        checked=True,
        indent=parsed["indent"],
    )
    text = replace_line_at(text, line_start, raw, new_line)
    rolled_msg = ""
    if parsed["repeat"]:
        next_date = _advance_remind_date(parsed["remind_on"], parsed["repeat"])
        if next_date:
            rolled = format_task_bullet(parsed["text"], remind_on=next_date, repeat=parsed["repeat"], checked=False)
            text = append_bullet_to_section(text, "Reminders", rolled)
            rolled_msg = f" Rolled forward to {next_date}."
    note.write_text(text, encoding="utf-8")
    _notify_index_of_write(note, text=text)
    return f"ok {path}{rolled_msg}"


@mcp.tool()
def list_reminders_due(window: str = "today", days: int = 7, limit: int = 200) -> str:
    """
    List unchecked reminders filtered by time window.

    ``window`` accepts natural language:
      today, overdue, upcoming, all, no-date,
      week, this week, month, this month, 7 days,
      next 3 days, next 2 weeks, or "N days/weeks/months".

    ``days`` sets the look-ahead for 'upcoming' (default 7).
    A natural-language window like "month" overrides days automatically.
    """
    window_norm, days_eff = _normalize_window(window, default_days=days)

    limit = max(1, min(limit, 5000))
    days_eff = max(0, min(days_eff, 365))
    today = datetime.now().date()
    horizon = today + timedelta(days=days_eff) if days_eff > 0 else datetime.max.date()

    idx = get_vault_index()
    all_reminders = idx.query_reminders(checked=False, limit=limit)

    buckets: dict[str, list[dict[str, str]]] = {"overdue": [], "today": [], "upcoming": [], "no-date": []}
    for r in all_reminders:
        item = {"path": r["note_path"], "reminder": r["text"], "remind_on": r["remind_on"], "repeat": r["repeat"]}
        dt = parse_iso_date(r["remind_on"])
        if dt is None:
            buckets["no-date"].append(item)
        else:
            d = dt.date()
            if d < today:
                buckets["overdue"].append(item)
            elif d == today:
                buckets["today"].append(item)
            elif d <= horizon:
                buckets["upcoming"].append(item)

    show = {
        "today": ["today"],
        "overdue": ["overdue"],
        "upcoming": ["upcoming"],
        "no-date": ["no-date"],
        "all": ["overdue", "today", "upcoming", "no-date"],
    }[window_norm]

    lines: list[str] = []
    for bucket in show:
        items = buckets[bucket]
        items.sort(key=lambda x: (x.get("remind_on", ""), x["path"]))
        lines.append(f"[{bucket}:{len(items)}]")
        for item in items:
            lines.append(f"{item['path']}|{item['reminder']}|{item.get('remind_on','')}|{item.get('repeat','')}")
    return "\n".join(lines).rstrip() or "none"



# ─── from original L8108-8143: Spaced repetition ───
# =============================================================================
# Spaced repetition / review scheduling
# =============================================================================


@mcp.tool()
def schedule_review(path: str, in_days: int = 7, reason: str = "") -> str:
    """
    Schedule a reminder to review a note in ``in_days`` days.

    Useful for spaced repetition: review imported/new notes after a delay
    to reinforce learning or verify information.
    """
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Only Markdown notes can have review reminders."

    in_days = max(1, min(in_days, 365))
    review_date = (datetime.now().date() + timedelta(days=in_days)).isoformat()
    rel = relative_to_vault(note)
    reminder_text = f"Review {make_wikilink(rel)}"
    if reason.strip():
        reminder_text += f" — {reason.strip()}"

    # Add the reminder to the note itself
    text = read_text(note)
    bullet = format_task_bullet(reminder_text, remind_on=review_date, checked=False)
    new_text = append_bullet_to_section(text, "Reminders", bullet)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)

    return f"ok {rel} review on {review_date}"


