"""Calendar/scheduling

@mcp.tool() definitions live here. The shared FastMCP instance is imported
from the package __init__.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .. import mcp
from ..core import *  # noqa: F401, F403  — all helpers, VaultIndex accessor,
                       # and the task/event parsing helpers (parse_event_line,
                       # parse_iso_date, parse_schedule_section, …)
# Underscore-prefixed names are excluded by `import *`, so we import them
# explicitly.
from ..core import _notify_index_of_write, _resolve_date_range
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



# ─── from original L8405-8533: vault_cron delegation ───
# =============================================================================
# vault_cron.py delegation (evening wrapup, weekly summary, morning routine)
# =============================================================================

import os
import subprocess
import sys
from pathlib import Path

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
def evening_wrapup(date: str = "today") -> str:
    """Generate an end-of-day summary and append to the daily note.

    Summarizes: calendar events attended, tasks completed, reminders done,
    and notes modified today.  Appends a ### Daily Summary block to ## Notes.

    Args:
        date: Target date — "today" or YYYY-MM-DD.
    """
    resolved = resolve_natural_date(date)
    if resolved is None:
        return f"Cannot parse date: {date}"
    return _run_vault_cron("wrapup", "--date", resolved)


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
        dry_run: Build the summary but do not write.
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
def morning_routine(dry_run: bool = False) -> str:
    """Run the morning routine: daily note + drop-zone import.

      1. Creates today's daily note
      2. Imports any files dropped into 90_Inbox/inbox/
    """
    args = ["morning"]
    if dry_run:
        args.append("--dry-run")
    return _run_vault_cron(*args, timeout=60)




# ── iCal subscription puller ───────────────────────────────────────────────
# Replacement for the deleted Apple-Calendar AppleScript path. Pulls from any
# public iCal feed (iCloud shared calendar, Google's "secret iCal URL",
# Outlook public link, etc.) without needing OS-level integration.


@mcp.tool()
def pull_ical_subscription(
    url: str,
    days_ahead: int = 30,
    days_back: int = 0,
    dry_run: bool = False,
    source_tag: str = "ical",
) -> str:
    """Sync events from a ``webcal://`` or ``https://`` iCalendar feed.

    Works with any source that exposes a public iCal feed:
      * **iCloud shared calendars** — Calendar.app → right-click calendar →
        Share Calendar → Public Calendar. Copy the ``webcal://`` URL.
      * **Google Calendar** — Settings → Settings for my calendars → pick
        one → "Secret address in iCal format" (long random URL).
      * **Outlook / Microsoft 365** — Calendar settings → Shared calendars
        → Publish → choose "ICS — anyone with the link".

    Each event is written into the appropriate daily note's ``## Schedule``
    section via ``schedule_event``. Re-running the same URL deduplicates by
    iCal UID (embedded as ``[ical-uid:...]`` in the event description), so
    repeated syncs don't create duplicates.

    Recurring events (RRULE) are expanded over the import window — you get
    one row per occurrence in the date range.

    Args:
        url: Calendar feed URL. ``webcal://`` is auto-rewritten to ``https://``.
        days_ahead: Days into the future to import (default 30, max 365).
        days_back: Days into the past (default 0, max 365). Useful for
            backfilling.
        dry_run: List what WOULD be imported without writing.
        source_tag: Marker for filtering later, e.g. ``icloud-personal``,
            ``google-work``. Stored in the event description.

    Returns a summary like ``📅 12 added, 3 skipped (already synced), 0 errors``.
    """
    try:
        import httpx
        import recurring_ical_events
        from icalendar import Calendar
    except ImportError as exc:
        return (f"err: missing dep — {exc}. Add `icalendar` + "
                "`recurring-ical-events` to pyproject.toml and rebuild.")

    url = (url or "").strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith(("http://", "https://")):
        return f"err: URL must be http(s):// or webcal://, got {url!r}"

    days_ahead = max(0, min(int(days_ahead), 365))
    days_back = max(0, min(int(days_back), 365))

    # Fetch
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return f"err: could not fetch feed: {exc}"

    # Parse
    try:
        cal = Calendar.from_ical(resp.content)
    except Exception as exc:  # icalendar raises generic exceptions
        return f"err: invalid iCalendar feed: {exc}"

    today = datetime.now().date()
    start_dt = datetime.combine(today - timedelta(days=days_back), datetime.min.time())
    end_dt = datetime.combine(today + timedelta(days=days_ahead), datetime.max.time())

    # Expand recurring events into individual occurrences in the window.
    try:
        events_iter = recurring_ical_events.of(cal).between(start_dt, end_dt)
    except Exception as exc:
        return f"err: failed to expand recurring events: {exc}"

    # Pre-load existing UIDs from the vault index for O(1) dedupe.
    from ..core import get_vault_index
    idx = get_vault_index()
    existing_uids: set[str] = set()
    try:
        rows = idx.conn.execute(
            "SELECT description FROM events WHERE description LIKE '%[ical-uid:%'"
        ).fetchall()
        for r in rows:
            desc = r["description"] if hasattr(r, "keys") else r[0]
            for m in re.finditer(r"\[ical-uid:([^\]]+)\]", desc or ""):
                existing_uids.add(m.group(1))
    except Exception:
        pass  # if events table doesn't exist yet, just skip dedupe

    added = 0
    skipped = 0
    errors: list[str] = []
    previews: list[str] = []

    for ev in events_iter:
        try:
            dtstart_field = ev.get("DTSTART")
            if dtstart_field is None:
                continue
            dtstart = dtstart_field.dt
            dtend_field = ev.get("DTEND")
            dtend = dtend_field.dt if dtend_field is not None else None

            summary = str(ev.get("SUMMARY") or "(no title)").strip()
            location = str(ev.get("LOCATION") or "").strip()
            ical_desc = str(ev.get("DESCRIPTION") or "").strip()
            uid = str(ev.get("UID") or "").strip()

            # Distinguish all-day (datetime.date) vs timed (datetime.datetime)
            is_timed = hasattr(dtstart, "hour")
            if is_timed:
                date_iso = dtstart.date().isoformat()
                time_str = dtstart.strftime("%H:%M")
                if dtend and hasattr(dtend, "hour"):
                    end_time_str = dtend.strftime("%H:%M")
                    end_date_str = (dtend.date().isoformat()
                                    if dtend.date() != dtstart.date() else "")
                else:
                    end_time_str = ""
                    end_date_str = ""
                all_day = False
            else:
                # iCal all-day events: DTEND is exclusive (the day AFTER).
                date_iso = dtstart.isoformat()
                if dtend:
                    last_day = dtend - timedelta(days=1)
                    end_date_str = (last_day.isoformat()
                                    if last_day != dtstart else "")
                else:
                    end_date_str = ""
                time_str = ""
                end_time_str = ""
                all_day = True

            if uid and uid in existing_uids:
                skipped += 1
                continue

            marker = f"[ical-uid:{uid}][source:{source_tag}]" if uid else f"[source:{source_tag}]"
            full_desc = (ical_desc + "\n\n" if ical_desc else "") + marker

            if dry_run:
                previews.append(f"  {date_iso} {time_str or 'all-day'} — {summary}"
                                + (f" @ {location}" if location else ""))
                added += 1
                continue

            result = schedule_event(
                date=date_iso,
                time=time_str,
                title=summary,
                end_time=end_time_str,
                end_date=end_date_str,
                location=location,
                description=full_desc,
                all_day=all_day,
            )
            if result.startswith("ok") or result.startswith("✅"):
                added += 1
                if uid:
                    existing_uids.add(uid)
            else:
                errors.append(f"{date_iso} {summary[:40]}: {result[:120]}")
        except Exception as exc:
            errors.append(f"parse failed: {exc}")

    summary_lines: list[str] = []
    verb = "would add" if dry_run else "added"
    summary_lines.append(
        f"📅 iCal sync ({source_tag}): {added} {verb}, "
        f"{skipped} skipped (already synced)"
    )
    if dry_run and previews:
        summary_lines.append(f"Preview (first {min(15, len(previews))}):")
        summary_lines.extend(previews[:15])
        if len(previews) > 15:
            summary_lines.append(f"  ... +{len(previews) - 15} more")
    if errors:
        summary_lines.append(f"⚠️ {len(errors)} error(s):")
        for e in errors[:5]:
            summary_lines.append(f"  - {e}")
        if len(errors) > 5:
            summary_lines.append(f"  - ... +{len(errors) - 5} more")
    return "\n".join(summary_lines)
