#!/usr/bin/env python3
"""
vault_cron.py — Lightweight automation for the Obsidian AI_Memory vault.

Runs independently of Claude Code / MCP. No LLM required.
Also callable from MCP tools as a subprocess.

Subcommands:
  python3 vault_cron.py sync              # bidirectional Obsidian ↔ Apple Reminders/Calendar
  python3 vault_cron.py sync --dry-run    # preview, no changes
  python3 vault_cron.py pull-calendar     # Apple Calendar → Obsidian daily notes
  python3 vault_cron.py pull-calendar --date 2026-05-15
  python3 vault_cron.py wrapup            # evening daily note summary
  python3 vault_cron.py focus             # show current Focus mode context
  python3 vault_cron.py shortcut <name>   # run an Apple Shortcut by name
  python3 vault_cron.py morning           # full morning routine (sync + pull-calendar + daily note)
  python3 vault_cron.py import-drop-zone  # process files in 90_Inbox/inbox/
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

# ── Configuration ──────────────────────────────────────────────────────────────
# All config (vault root, Apple list names, focus mapping, etc.) lives in
# iris_config.py — sibling module, zero deps. Override via env vars or
# ~/.config/iris/config.toml.

import iris_config as cfg

VAULT_ROOT = cfg.VAULT_ROOT
DB_PATH = cfg.vault_db_path()
LOG_PATH = cfg.vault_cache_dir() / "vault_cron.log"
SYNC_STATE_PATH = cfg.vault_cache_dir() / "sync_state.json"

REMINDERS_LIST = cfg.REMINDERS_LIST
CALENDAR_NAME = cfg.CALENDAR_NAME
CALENDAR_EXCLUDE = cfg.CALENDAR_EXCLUDE
FOCUS_CONTEXT = cfg.FOCUS_CONTEXT

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vault_cron")

# ── Sync state ─────────────────────────────────────────────────────────────────
# Tracks which items have been synced so we can detect completions from either
# side and avoid duplicates.
#
# Structure:
# {
#   "items": {
#     "<sync_key>": {
#       "apple_name": "...",
#       "note_path": "...",
#       "text": "...",
#       "type": "reminder" | "task",
#       "section": "Reminders" | "Tasks",
#       "due": "YYYY-MM-DD",
#       "repeat": "",
#       "synced_at": "YYYY-MM-DDTHH:MM:SS"
#     }
#   },
#   "last_sync": "YYYY-MM-DDTHH:MM:SS"
# }


def load_sync_state() -> dict:
    if SYNC_STATE_PATH.exists():
        try:
            return json.loads(SYNC_STATE_PATH.read_text("utf-8"))
        except (json.JSONDecodeError, KeyError):
            log.warning("Corrupt sync_state.json, starting fresh")
    return {"items": {}, "last_sync": ""}


def save_sync_state(state: dict) -> None:
    state["last_sync"] = datetime.now().isoformat(timespec="seconds")
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), "utf-8")


def sync_key(text: str, note_path: str) -> str:
    """Unique key for a synced item."""
    return f"{text.strip()}|{note_path.strip()}"


# ── AppleScript helpers ────────────────────────────────────────────────────────

def _run_applescript(script: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0 and r.stderr.strip():
            log.warning(f"AppleScript stderr: {r.stderr.strip()}")
        return r.stdout.strip()
    except Exception as e:
        log.warning(f"AppleScript failed: {e}")
        return ""


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── macOS Notifications ────────────────────────────────────────────────────────

def notify(title: str, body: str, sound: str = "default") -> None:
    script = f'display notification "{_esc(body)}" with title "{_esc(title)}" sound name "{sound}"'
    _run_applescript(script)
    log.info(f"Notification: {title} — {body[:80]}")


# ── Apple Reminders — read/write ──────────────────────────────────────────────

def ensure_reminders_list() -> None:
    exists = _run_applescript(
        'tell application "Reminders" to get name of every list'
    )
    if REMINDERS_LIST in exists:
        return
    _run_applescript(
        f'tell application "Reminders" to make new list with properties '
        f'{{name:"{_esc(REMINDERS_LIST)}"}}'
    )
    log.info(f"Created Reminders list: {REMINDERS_LIST}")


def get_apple_reminders() -> list[dict]:
    """Get all reminders in the Vault list with name + completed status."""
    raw = _run_applescript(f'''
        tell application "Reminders"
            set output to ""
            try
                set theList to list "{_esc(REMINDERS_LIST)}"
                repeat with r in (every reminder of theList)
                    set rName to name of r
                    set rDone to completed of r
                    if rDone then
                        set doneStr to "true"
                    else
                        set doneStr to "false"
                    end if
                    set output to output & rName & "<<>>" & doneStr & "||"
                end repeat
            end try
            return output
        end tell
    ''')
    if not raw:
        return []
    results = []
    for entry in raw.split("||"):
        entry = entry.strip()
        if "<<>>" not in entry:
            continue
        name, done_str = entry.rsplit("<<>>", 1)
        results.append({
            "name": name.strip(),
            "completed": done_str.strip() == "true",
        })
    return results


def add_reminder_to_apple(name: str, due_date: str = "") -> None:
    """Add a reminder to the Vault list. due_date can be empty for undated tasks."""
    if due_date and len(due_date) >= 10:
        y, m, d = int(due_date[:4]), int(due_date[5:7]), int(due_date[8:10])
        script = f'''
            tell application "Reminders"
                tell list "{_esc(REMINDERS_LIST)}"
                    set d to current date
                    set year of d to {y}
                    set month of d to {m}
                    set day of d to {d}
                    set hours of d to 9
                    set minutes of d to 30
                    set seconds of d to 0
                    make new reminder with properties {{name:"{_esc(name)}", due date:d}}
                end tell
            end tell
        '''
        _run_applescript(script)
        log.info(f"Apple ← added: {name} (due {due_date})")
    else:
        script = f'''
            tell application "Reminders"
                tell list "{_esc(REMINDERS_LIST)}"
                    make new reminder with properties {{name:"{_esc(name)}"}}
                end tell
            end tell
        '''
        _run_applescript(script)
        log.info(f"Apple ← added: {name} (no due date)")


def complete_apple_reminder(name: str) -> None:
    """Mark a reminder as completed in Apple Reminders."""
    _run_applescript(f'''
        tell application "Reminders"
            tell list "{_esc(REMINDERS_LIST)}"
                repeat with r in (every reminder whose name is "{_esc(name)}" and completed is false)
                    set completed of r to true
                end repeat
            end tell
        end tell
    ''')
    log.info(f"Apple ← completed: {name}")


def delete_apple_reminder(name: str) -> None:
    """Delete a completed reminder from Apple Reminders (cleanup)."""
    _run_applescript(f'''
        tell application "Reminders"
            tell list "{_esc(REMINDERS_LIST)}"
                set toDelete to (every reminder whose name is "{_esc(name)}" and completed is true)
                repeat with r in toDelete
                    delete r
                end repeat
            end tell
        end tell
    ''')


# ── Apple Calendar ────────────────────────────────────────────────────────────

def ensure_calendar() -> None:
    exists = _run_applescript(
        'tell application "Calendar" to get name of every calendar'
    )
    if CALENDAR_NAME in exists:
        return
    _run_applescript(f'''
        tell application "Calendar"
            make new calendar with properties {{name:"{_esc(CALENDAR_NAME)}"}}
        end tell
    ''')
    log.info(f"Created calendar: {CALENDAR_NAME}")


def get_existing_events_today(today_str: str) -> set[str]:
    y, m, d = int(today_str[:4]), int(today_str[5:7]), int(today_str[8:10])
    raw = _run_applescript(f'''
        tell application "Calendar"
            set output to ""
            try
                set startD to current date
                set year of startD to {y}
                set month of startD to {m}
                set day of startD to {d}
                set hours of startD to 0
                set minutes of startD to 0
                set seconds of startD to 0
                set endD to startD + (1 * days)
                set theEvents to (every event of calendar "{_esc(CALENDAR_NAME)}" whose start date >= startD and start date < endD)
                repeat with ev in theEvents
                    set output to output & summary of ev & "||"
                end repeat
            end try
            return output
        end tell
    ''')
    if not raw:
        return set()
    return {name.strip() for name in raw.split("||") if name.strip()}


def add_event_to_calendar(title: str, event_date: str, time_str: str,
                          end_time: str = "", location: str = "") -> None:
    y, m, d = int(event_date[:4]), int(event_date[5:7]), int(event_date[8:10])
    if time_str:
        parts = time_str.split(":")
        h, mi = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    else:
        h, mi = 9, 0
    if end_time:
        eparts = end_time.split(":")
        eh, emi = int(eparts[0]), int(eparts[1]) if len(eparts) > 1 else 0
    else:
        eh, emi = h + 1, mi
    loc_prop = f', location:"{_esc(location)}"' if location else ""
    script = f'''
        tell application "Calendar"
            tell calendar "{_esc(CALENDAR_NAME)}"
                set startD to current date
                set year of startD to {y}
                set month of startD to {m}
                set day of startD to {d}
                set hours of startD to {h}
                set minutes of startD to {mi}
                set seconds of startD to 0
                set endD to current date
                set year of endD to {y}
                set month of endD to {m}
                set day of endD to {d}
                set hours of endD to {eh}
                set minutes of endD to {emi}
                set seconds of endD to 0
                make new event with properties {{summary:"{_esc(title)}", start date:startD, end date:endD{loc_prop}}}
            end tell
        end tell
    '''
    _run_applescript(script)
    log.info(f"Calendar ← added: {title} ({event_date} {time_str}–{end_time})")


# ── Database Queries ───────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        log.error(f"Database not found: {DB_PATH}")
        log.error("Run the MCP server at least once to build the index.")
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def query_due_reminders(conn: sqlite3.Connection, today: str) -> list[dict]:
    """Reminders due today or overdue (for notifications)."""
    rows = conn.execute(
        "SELECT text, remind_on, repeat, note_path FROM reminders "
        "WHERE checked = 0 AND remind_on != '' AND remind_on <= ? ORDER BY remind_on",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_all_open_reminders(conn: sqlite3.Connection) -> list[dict]:
    """ALL unchecked reminders with dates (for Apple sync — push future ones too)."""
    rows = conn.execute(
        "SELECT text, remind_on, repeat, note_path FROM reminders "
        "WHERE checked = 0 AND remind_on != '' ORDER BY remind_on",
    ).fetchall()
    return [dict(r) for r in rows]


def query_all_open_tasks(conn: sqlite3.Connection) -> list[dict]:
    """ALL unchecked tasks — tasks are actionable immediately, due date is a deadline."""
    rows = conn.execute(
        "SELECT text, due, note_path FROM tasks "
        "WHERE checked = 0 ORDER BY due",
    ).fetchall()
    return [dict(r) for r in rows]


def query_due_tasks(conn: sqlite3.Connection, today: str) -> list[dict]:
    """Tasks due today or overdue (for notifications only)."""
    rows = conn.execute(
        "SELECT text, due, note_path FROM tasks "
        "WHERE checked = 0 AND due != '' AND due <= ? ORDER BY due",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_today_events(conn: sqlite3.Connection, today: str) -> list[dict]:
    rows = conn.execute(
        "SELECT title, time, end_time, location FROM events "
        "WHERE date = ? ORDER BY time",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Obsidian Markdown editing (standalone, no MCP needed) ─────────────────────

_TASK_RE = re.compile(r"^(?P<indent>\s*)- \[(?P<box>[ xX])\]\s+(?P<rest>.*)$")
_KNOWN_META = {"due", "priority", "done", "remind_on", "repeat", "id"}


def parse_task_bullet(line: str) -> dict[str, Any] | None:
    m = _TASK_RE.match(line.rstrip())
    if not m:
        return None
    rest = m.group("rest")
    parts = [p.strip() for p in rest.split("—")]
    text = parts[0]
    meta: dict[str, str] = {}
    for p in parts[1:]:
        if ":" not in p:
            continue
        key, value = p.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key in _KNOWN_META:
            meta[key] = value
    return {
        "checked": m.group("box").lower() == "x",
        "indent": m.group("indent"),
        "text": text,
        "due": meta.get("due", ""),
        "priority": meta.get("priority", ""),
        "done": meta.get("done", ""),
        "remind_on": meta.get("remind_on", ""),
        "repeat": meta.get("repeat", ""),
        "id": meta.get("id", ""),
        "raw": line,
    }


def format_task_bullet(
    text: str, due: str = "", priority: str = "", done: str = "",
    remind_on: str = "", repeat: str = "", task_id: str = "",
    checked: bool = False, indent: str = "",
) -> str:
    box = "[x]" if checked else "[ ]"
    parts: list[str] = [text.strip()]
    if due.strip():
        parts.append(f"due: {due.strip()}")
    if priority.strip():
        parts.append(f"priority: {priority.strip()}")
    if remind_on.strip():
        parts.append(f"remind_on: {remind_on.strip()}")
    if repeat.strip():
        parts.append(f"repeat: {repeat.strip()}")
    if task_id.strip():
        parts.append(f"id: {task_id.strip()}")
    if done.strip():
        parts.append(f"done: {done.strip()}")
    return f"{indent}- {box} " + " — ".join(parts)


def advance_date(remind_on: str, repeat: str) -> str:
    """Advance a date by the repeat interval. Returns '' if can't."""
    try:
        dt = datetime.strptime(remind_on[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return ""
    repeat = (repeat or "").strip().lower()
    if repeat == "daily":
        nxt = dt + timedelta(days=1)
    elif repeat == "weekdays":
        nxt = dt + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    elif repeat == "weekly":
        nxt = dt + timedelta(days=7)
    elif repeat == "biweekly":
        nxt = dt + timedelta(days=14)
    elif repeat == "monthly":
        year = dt.year + (1 if dt.month == 12 else 0)
        month = 1 if dt.month == 12 else dt.month + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        nxt = dt.replace(year=year, month=month, day=day)
    elif repeat == "quarterly":
        month = dt.month + 3
        year = dt.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        nxt = dt.replace(year=year, month=month, day=day)
    elif repeat == "yearly":
        year = dt.year + 1
        day = min(dt.day, calendar.monthrange(year, dt.month)[1])
        nxt = dt.replace(year=year, day=day)
    else:
        return ""
    return nxt.strftime("%Y-%m-%d")


def _find_section_bounds(text: str, section: str) -> tuple[int, int] | None:
    escaped = re.escape(section.strip())
    heading_re = re.compile(rf"^(?P<hashes>#+)\s+{escaped}\s*$", re.MULTILINE)
    match = heading_re.search(text)
    if not match:
        return None
    level = len(match.group("hashes"))
    start = match.start()
    next_heading_re = re.compile(r"^(?P<hashes>#+)\s+.+$", re.MULTILINE)
    for next_match in next_heading_re.finditer(text, match.end()):
        if len(next_match.group("hashes")) <= level:
            return (start, next_match.start())
    return (start, len(text))


def complete_in_obsidian(
    note_path: str, text: str, section: str, done_date: str, repeat: str = ""
) -> bool:
    """Mark a task/reminder as done in the Obsidian markdown file.

    For recurring reminders, also appends a new bullet with the advanced date.
    Returns True if the file was modified.
    """
    full_path = VAULT_ROOT / note_path
    if not full_path.exists():
        log.warning(f"File not found: {note_path}")
        return False

    file_text = full_path.read_text("utf-8")
    bounds = _find_section_bounds(file_text, section)
    if bounds is None:
        log.warning(f"Section '{section}' not found in {note_path}")
        return False

    start, end = bounds
    section_text = file_text[start:end]

    # Find the matching unchecked bullet
    found_offset = None
    found_raw = None
    found_parsed = None
    cursor = start
    for line in section_text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        parsed = parse_task_bullet(stripped)
        if parsed and not parsed["checked"] and parsed["text"].strip() == text.strip():
            found_offset = cursor
            found_raw = stripped
            found_parsed = parsed
            break
        cursor += len(line)

    if found_offset is None or found_parsed is None:
        log.warning(f"Could not find unchecked '{text}' in {section} of {note_path}")
        return False

    # Build the completed line
    new_line = format_task_bullet(
        found_parsed["text"],
        due=found_parsed["due"],
        priority=found_parsed["priority"],
        done=done_date,
        remind_on=found_parsed["remind_on"],
        repeat=found_parsed["repeat"],
        task_id=found_parsed["id"],
        checked=True,
        indent=found_parsed["indent"],
    )

    # Replace old line with completed line
    line_end = found_offset + len(found_raw)
    file_text = file_text[:found_offset] + new_line + file_text[line_end:]

    # For recurring reminders, append a new unchecked bullet with advanced date
    rolled_date = ""
    if repeat:
        next_date = advance_date(found_parsed["remind_on"], repeat)
        if next_date:
            rolled_date = next_date
            rolled_bullet = format_task_bullet(
                found_parsed["text"],
                remind_on=next_date,
                repeat=repeat,
                checked=False,
            )
            # Re-find section bounds (text shifted after replacement)
            bounds2 = _find_section_bounds(file_text, section)
            if bounds2:
                s2, e2 = bounds2
                sec2 = file_text[s2:e2].rstrip()
                if rolled_bullet not in sec2:
                    file_text = file_text[:s2] + sec2 + f"\n{rolled_bullet}" + "\n" + file_text[e2:]

    full_path.write_text(file_text, "utf-8")
    if rolled_date:
        log.info(f"Obsidian ← completed '{text}' in {note_path}, rolled → {rolled_date}")
    else:
        log.info(f"Obsidian ← completed '{text}' in {note_path}")
    return True


def is_checked_in_obsidian(note_path: str, text: str, section: str) -> bool:
    """Check if a task/reminder is already marked done in the markdown file."""
    full_path = VAULT_ROOT / note_path
    if not full_path.exists():
        return False
    file_text = full_path.read_text("utf-8")
    bounds = _find_section_bounds(file_text, section)
    if bounds is None:
        return False
    start, end = bounds
    section_text = file_text[start:end]
    for line in section_text.splitlines():
        parsed = parse_task_bullet(line.rstrip())
        if parsed and parsed["text"].strip() == text.strip():
            return parsed["checked"]
    return False


# ── Daily Note Creation ────────────────────────────────────────────────────────

DAILY_TEMPLATE = """\
---
date: {iso_date}
tags:
  - daily
type: daily
---
# {iso_date} — {day_name}

## Schedule

## Tasks

## Reminders

## Notes
"""


def ensure_daily_note(today: date, dry_run: bool = False) -> bool:
    year_dir = VAULT_ROOT / "30_Episodic" / str(today.year)
    note_path = year_dir / f"{today.isoformat()}.md"
    if note_path.exists():
        log.info(f"Daily note already exists: {note_path.name}")
        return False
    if dry_run:
        log.info(f"[DRY RUN] Would create: {note_path.name}")
        return True
    year_dir.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        DAILY_TEMPLATE.format(iso_date=today.isoformat(), day_name=today.strftime("%A")),
        "utf-8",
    )
    log.info(f"Created daily note: {note_path.name}")
    return True


# ── Bidirectional Sync ─────────────────────────────────────────────────────────

def sync_bidirectional(dry_run: bool = False) -> dict[str, int]:
    """Run the full bidirectional sync. Returns counters for logging."""
    today_str = date.today().isoformat()
    counts = {"pushed": 0, "pulled": 0, "pushed_complete": 0, "events": 0}

    conn = get_db()
    state = load_sync_state()
    items = state.setdefault("items", {})

    # ── Gather Obsidian data ───────────────────────────────────────────────
    # Reminders (## Reminders) = time-triggered → only push on due date.
    # Tasks (## Tasks) = actionable items → push ALL immediately, due date is a deadline.
    reminders = query_due_reminders(conn, today_str)
    all_tasks = query_all_open_tasks(conn)
    events = query_today_events(conn, today_str)
    conn.close()

    # Build a set of all current obsidian items (for push)
    obsidian_items: dict[str, dict] = {}
    for r in reminders:
        key = sync_key(r["text"], r["note_path"])
        obsidian_items[key] = {
            "text": r["text"],
            "note_path": r["note_path"],
            "due": r["remind_on"],
            "repeat": r["repeat"],
            "type": "reminder",
            "section": "Reminders",
        }
    for t in all_tasks:
        key = sync_key(t["text"], t["note_path"])
        obsidian_items[key] = {
            "text": t["text"],
            "note_path": t["note_path"],
            "due": t["due"],
            "repeat": "",
            "type": "task",
            "section": "Tasks",
        }

    # ── Gather Apple Reminders data ────────────────────────────────────────
    if not dry_run:
        ensure_reminders_list()
    apple_reminders = get_apple_reminders() if not dry_run else []
    apple_by_name: dict[str, dict] = {}
    for ar in apple_reminders:
        apple_by_name[ar["name"]] = ar

    # ── STEP 1: Apple → Obsidian (pull completions) ────────────────────────
    # If an item exists in sync state AND is completed in Apple but not in
    # Obsidian, mark it done in Obsidian.
    for key, info in list(items.items()):
        apple_name = info.get("apple_name", info["text"])
        apple_entry = apple_by_name.get(apple_name)

        if apple_entry and apple_entry["completed"]:
            # Completed in Apple — check if still unchecked in Obsidian
            if not is_checked_in_obsidian(info["note_path"], info["text"], info["section"]):
                if dry_run:
                    log.info(f"[DRY RUN] Would complete in Obsidian: {info['text']}")
                else:
                    ok = complete_in_obsidian(
                        info["note_path"], info["text"], info["section"],
                        done_date=today_str, repeat=info.get("repeat", ""),
                    )
                    if ok:
                        counts["pulled"] += 1
                        # If recurring, the markdown now has a new unchecked bullet
                        # with the advanced date. We update sync state to track it.
                        if info.get("repeat"):
                            next_date = advance_date(info["due"], info["repeat"])
                            if next_date:
                                # Remove old completed item from Apple
                                delete_apple_reminder(apple_name)
                                # Add new one with advanced date
                                add_reminder_to_apple(info["text"], next_date)
                                # Update sync state
                                items[key]["due"] = next_date
                                items[key]["synced_at"] = datetime.now().isoformat(timespec="seconds")
                                continue
                # Remove completed non-recurring from sync state
                if not info.get("repeat"):
                    delete_apple_reminder(apple_name)
                    del items[key]
                continue

    # ── STEP 2: Obsidian → Apple (push completions) ────────────────────────
    # If an item in sync state is now checked in Obsidian but not in Apple,
    # mark it complete in Apple.
    for key, info in list(items.items()):
        if is_checked_in_obsidian(info["note_path"], info["text"], info["section"]):
            apple_name = info.get("apple_name", info["text"])
            apple_entry = apple_by_name.get(apple_name)
            if apple_entry and not apple_entry["completed"]:
                if dry_run:
                    log.info(f"[DRY RUN] Would complete in Apple: {info['text']}")
                else:
                    complete_apple_reminder(apple_name)
                    counts["pushed_complete"] += 1

                    if info.get("repeat"):
                        next_date = advance_date(info["due"], info["repeat"])
                        if next_date:
                            delete_apple_reminder(apple_name)
                            add_reminder_to_apple(info["text"], next_date)
                            items[key]["due"] = next_date
                            items[key]["synced_at"] = datetime.now().isoformat(timespec="seconds")
                            continue

                    if not info.get("repeat"):
                        delete_apple_reminder(apple_name)
                        del items[key]

    # ── STEP 3: Obsidian → Apple (push new items) ─────────────────────────
    for key, obs in obsidian_items.items():
        if key in items:
            continue  # already synced
        apple_name = obs["text"]
        if apple_name in apple_by_name:
            # Already in Apple (maybe manually created) — just track it
            items[key] = {
                **obs,
                "apple_name": apple_name,
                "synced_at": datetime.now().isoformat(timespec="seconds"),
            }
            continue
        if dry_run:
            log.info(f"[DRY RUN] Would push to Apple: {obs['text']} (due {obs['due']})")
        else:
            add_reminder_to_apple(apple_name, obs["due"])
            items[key] = {
                **obs,
                "apple_name": apple_name,
                "synced_at": datetime.now().isoformat(timespec="seconds"),
            }
        counts["pushed"] += 1

    # ── STEP 4: Sync events to Calendar ────────────────────────────────────
    if events and not dry_run:
        ensure_calendar()
        existing_ev = get_existing_events_today(today_str)
        for ev in events:
            if ev["title"] not in existing_ev:
                add_event_to_calendar(
                    ev["title"], today_str,
                    ev["time"], ev["end_time"], ev["location"],
                )
                counts["events"] += 1
    elif events and dry_run:
        log.info(f"[DRY RUN] Would sync {len(events)} events to Calendar")
        counts["events"] = len(events)

    # ── Save state ─────────────────────────────────────────────────────────
    if not dry_run:
        save_sync_state(state)

    return counts


# ── Apple Calendar → Obsidian (reverse sync) ──────────────────────────────────

def pull_calendar_events(target_date: str | None = None, dry_run: bool = False) -> str:
    """Pull events from ALL Apple Calendars into the daily note's ## Schedule.

    Handles cross-day events (end date != start date) and all-day events.
    Skips calendars in CALENDAR_EXCLUDE.  Deduplicates by title+time.
    Returns a human-readable summary.
    """
    d = date.fromisoformat(target_date) if target_date else date.today()
    d_str = d.isoformat()
    day_name = d.strftime("%A")
    y, m, dd = d.year, d.month, d.day

    log.info(f"Pulling Apple Calendar events for {d_str} ({day_name})")

    # Query events that OVERLAP with the target day:
    # - Events starting on this day
    # - Events that started before but end on/after this day (cross-day)
    # - All-day events spanning this day
    # The AppleScript fetches both: events starting today AND events whose
    # end date is after the start of today (catches cross-day carry-overs).
    raw = _run_applescript(f'''
        tell application "Calendar"
            set output to ""
            set startD to current date
            set year of startD to {y}
            set month of startD to {m}
            set day of startD to {dd}
            set hours of startD to 0
            set minutes of startD to 0
            set seconds of startD to 0
            set endD to startD + (1 * days)
            repeat with cal in calendars
                set calName to name of cal
                try
                    -- Events that overlap this day: started before endD AND end after startD
                    set theEvents to (every event of cal whose start date < endD and end date > startD)
                    repeat with ev in theEvents
                        set evStart to start date of ev
                        set evEnd to end date of ev
                        set evTitle to summary of ev
                        set evLoc to ""
                        try
                            set evLoc to location of ev
                        end try
                        -- Format start date as YYYY-MM-DD
                        set sy to year of evStart
                        set sm to month of evStart as integer
                        set sd to day of evStart
                        set startDateStr to (sy as text) & "-" & (text -2 thru -1 of ("0" & sm)) & "-" & (text -2 thru -1 of ("0" & sd))
                        -- Format end date as YYYY-MM-DD
                        set ey to year of evEnd
                        set em to month of evEnd as integer
                        set ed to day of evEnd
                        set endDateStr to (ey as text) & "-" & (text -2 thru -1 of ("0" & em)) & "-" & (text -2 thru -1 of ("0" & ed))
                        -- Times
                        set h1 to hours of evStart
                        set m1 to minutes of evStart
                        set h2 to hours of evEnd
                        set m2 to minutes of evEnd
                        set timeStr to (text -2 thru -1 of ("0" & h1)) & ":" & (text -2 thru -1 of ("0" & m1))
                        set endStr to (text -2 thru -1 of ("0" & h2)) & ":" & (text -2 thru -1 of ("0" & m2))
                        -- Detect all-day: starts at 00:00 and duration is exact multiple of 24h
                        set isAllDay to "0"
                        if h1 = 0 and m1 = 0 and h2 = 0 and m2 = 0 and startDateStr is not equal to endDateStr then
                            set isAllDay to "1"
                        end if
                        set output to output & calName & "<<>>" & timeStr & "<<>>" & endStr & "<<>>" & evTitle & "<<>>" & evLoc & "<<>>" & startDateStr & "<<>>" & endDateStr & "<<>>" & isAllDay & "||"
                    end repeat
                end try
            end repeat
            return output
        end tell
    ''', timeout=60)

    events: list[dict] = []
    seen = set()
    if raw:
        for entry in raw.split("||"):
            entry = entry.strip()
            if "<<>>" not in entry:
                continue
            parts = entry.split("<<>>")
            if len(parts) < 4:
                continue
            cal_name = parts[0].strip()
            time_str = parts[1].strip()
            end_str = parts[2].strip()
            title = parts[3].strip()
            location = parts[4].strip() if len(parts) > 4 else ""
            ev_start_date = parts[5].strip() if len(parts) > 5 else d_str
            ev_end_date = parts[6].strip() if len(parts) > 6 else ""
            is_all_day = parts[7].strip() == "1" if len(parts) > 7 else False

            if cal_name in CALENDAR_EXCLUDE:
                continue

            dedup_key = f"{time_str}|{title}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            events.append({
                "calendar": cal_name,
                "time": time_str,
                "end_time": end_str,
                "title": title,
                "location": location,
                "start_date": ev_start_date,
                "end_date": ev_end_date,
                "all_day": is_all_day,
            })

    # Sort: all-day first, then by time
    events.sort(key=lambda e: ("1" if not e["all_day"] else "0", e["time"]))

    if not events:
        log.info("No calendar events found for this date.")
        return f"No events on {d_str}."

    # Format as schedule bullets
    bullets: list[str] = []
    for ev in events:
        if ev["all_day"]:
            line = f"- all-day {ev['title']}"
        else:
            line = f"- {ev['time']}"
            if ev["end_time"] and ev["end_time"] != ev["time"]:
                line += f"–{ev['end_time']}"
                # Cross-day marker: end date differs from start date
                if ev["end_date"] and ev["start_date"] and ev["end_date"] != ev["start_date"]:
                    try:
                        ds = date.fromisoformat(ev["start_date"])
                        de = date.fromisoformat(ev["end_date"])
                        plus = (de - ds).days
                        if plus > 0:
                            line += f" (+{plus}d)"
                    except ValueError:
                        pass
            line += f" {ev['title']}"
        if ev["location"] and ev["location"] != "missing value":
            line += f" @ {ev['location']}"
        bullets.append(line)

    if dry_run:
        summary = f"[DRY RUN] Would add {len(bullets)} events to {d_str}.md:\n" + "\n".join(bullets)
        log.info(summary)
        return summary

    # Ensure daily note exists
    ensure_daily_note(d)
    note_path = VAULT_ROOT / "30_Episodic" / str(d.year) / f"{d_str}.md"
    file_text = note_path.read_text("utf-8")

    # Get existing schedule content to avoid duplicates
    bounds = _find_section_bounds(file_text, "Schedule")
    if bounds:
        start, end = bounds
        existing_section = file_text[start:end]
    else:
        existing_section = ""

    added = 0
    for bullet in bullets:
        # Check if this event (by title) is already in the schedule
        # Extract title: skip time prefix, strip location suffix
        bullet_parts = bullet.lstrip("- ")
        if bullet_parts.startswith("all-day "):
            title_in_bullet = bullet_parts[8:].split(" @ ")[0].strip()
        else:
            title_in_bullet = bullet_parts.split(" ", 1)[-1] if " " in bullet_parts else bullet_parts
            # Strip (+Nd) marker for dedup check
            title_in_bullet = re.sub(r"\s*\(\+\d+d\)\s*", " ", title_in_bullet).strip()
            title_in_bullet = title_in_bullet.split(" @ ")[0].strip()
        if title_in_bullet in existing_section:
            continue
        # Append to schedule section
        if bounds:
            section_text = file_text[bounds[0]:bounds[1]].rstrip()
            file_text = file_text[:bounds[0]] + section_text + f"\n{bullet}" + "\n" + file_text[bounds[1]:]
            # Re-find bounds since text shifted
            bounds = _find_section_bounds(file_text, "Schedule")
        else:
            file_text = file_text.rstrip() + f"\n\n## Schedule\n{bullet}\n"
            bounds = _find_section_bounds(file_text, "Schedule")
        added += 1

    if added:
        note_path.write_text(file_text, "utf-8")
        log.info(f"Added {added} calendar events to {d_str}.md")

    summary = f"Pulled {added} events into {d_str}.md"
    if added:
        summary += ":\n" + "\n".join(bullets)
    return summary


# ── Evening Wrapup ─────────────────────────────────────────────────────────────

def evening_wrapup(target_date: str | None = None, dry_run: bool = False) -> str:
    """Generate an end-of-day summary and append to the daily note's ## Notes.

    Summarizes: events attended, tasks completed, reminders done, files modified.
    """
    d = date.fromisoformat(target_date) if target_date else date.today()
    d_str = d.isoformat()
    day_name = d.strftime("%A")
    log.info(f"Evening wrapup for {d_str} ({day_name})")

    note_path = VAULT_ROOT / "30_Episodic" / str(d.year) / f"{d_str}.md"
    if not note_path.exists():
        return f"No daily note for {d_str}."

    lines: list[str] = [f"### Daily Summary ({day_name})"]

    # 1. Events from schedule section
    file_text = note_path.read_text("utf-8")
    bounds = _find_section_bounds(file_text, "Schedule")
    if bounds:
        sched_text = file_text[bounds[0]:bounds[1]]
        event_lines = [l.strip() for l in sched_text.splitlines() if l.strip().startswith("- ") and not l.strip().startswith("- [ ]")]
        if event_lines:
            lines.append(f"\n**Events** ({len(event_lines)})")
            for el in event_lines:
                lines.append(el)

    # 2. Completed tasks (from DB or file)
    completed_tasks = []
    completed_reminders = []
    for section_name, target_list in [("Tasks", completed_tasks), ("Reminders", completed_reminders)]:
        sec_bounds = _find_section_bounds(file_text, section_name)
        if sec_bounds:
            sec_text = file_text[sec_bounds[0]:sec_bounds[1]]
            for line in sec_text.splitlines():
                parsed = parse_task_bullet(line.strip())
                if parsed and parsed["checked"]:
                    target_list.append(parsed["text"])

    # Also check the SQLite DB for tasks completed today across ALL notes
    try:
        conn = get_db()
        db_tasks = conn.execute(
            "SELECT text, note_path FROM tasks WHERE checked = 1 AND done = ?", (d_str,)
        ).fetchall()
        for row in db_tasks:
            if row["text"] not in completed_tasks:
                completed_tasks.append(row["text"])
        db_reminders = conn.execute(
            "SELECT text, note_path FROM reminders WHERE checked = 1 AND done = ?", (d_str,)
        ).fetchall()
        for row in db_reminders:
            if row["text"] not in completed_reminders:
                completed_reminders.append(row["text"])
        conn.close()
    except Exception:
        pass

    if completed_tasks:
        lines.append(f"\n**Tasks completed** ({len(completed_tasks)})")
        for t in completed_tasks:
            lines.append(f"- ~~{t}~~")

    if completed_reminders:
        lines.append(f"\n**Reminders done** ({len(completed_reminders)})")
        for r in completed_reminders:
            lines.append(f"- ~~{r}~~")

    # 3. Files modified today
    try:
        modified: list[str] = []
        for p in VAULT_ROOT.rglob("*.md"):
            if ".ai_memory_cache" in str(p) or ".trash" in str(p):
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime).date()
                if mtime == d:
                    rel = str(p.relative_to(VAULT_ROOT))
                    if rel != f"30_Episodic/{d.year}/{d_str}.md":  # skip the daily note itself
                        modified.append(rel)
            except OSError:
                continue
        if modified:
            lines.append(f"\n**Notes modified** ({len(modified)})")
            for m in sorted(modified)[:15]:
                name = Path(m).stem
                lines.append(f"- [[{m[:-3]}|{name}]]" if m.endswith(".md") else f"- {m}")
            if len(modified) > 15:
                lines.append(f"- ...and {len(modified) - 15} more")
    except Exception as e:
        log.warning(f"Error scanning modified files: {e}")

    if len(lines) <= 1:
        lines.append("\nQuiet day — nothing tracked.")

    summary_text = "\n".join(lines)

    if dry_run:
        log.info(f"[DRY RUN] Would append wrapup to {d_str}.md")
        return summary_text

    # Append to ## Notes section
    bounds = _find_section_bounds(file_text, "Notes")
    if bounds:
        start, end = bounds
        sec = file_text[start:end].rstrip()
        # Don't duplicate if already has a summary
        if "### Daily Summary" in sec:
            log.info("Daily summary already exists, skipping")
            return "Summary already exists in daily note."
        file_text = file_text[:start] + sec + f"\n\n{summary_text}\n" + file_text[end:]
    else:
        file_text = file_text.rstrip() + f"\n\n## Notes\n\n{summary_text}\n"

    note_path.write_text(file_text, "utf-8")
    log.info(f"Appended evening wrapup to {d_str}.md")
    if not dry_run:
        notify("🌙 Daily Wrapup", f"Summary added to {d_str}.md")
    return summary_text


# ── Focus Mode ─────────────────────────────────────────────────────────────────

def get_focus_mode() -> str:
    """Detect the current macOS Focus mode.

    Returns the mode name (e.g. 'Work', 'Personal', 'Do Not Disturb') or 'None'.
    """
    # Method: read the DND assertion store (macOS 12+)
    import plistlib
    dnd_path = Path.home() / "Library" / "DoNotDisturb" / "DB" / "Assertions.json"
    mode_config_path = Path.home() / "Library" / "DoNotDisturb" / "DB" / "ModeConfigurations.json"

    active_mode = "None"

    # Try reading assertions
    try:
        if dnd_path.exists():
            data = json.loads(dnd_path.read_text("utf-8"))
            # The structure varies by macOS version
            store = data.get("data", [{}])
            if isinstance(store, list) and store:
                assertions = store[0].get("storeAssertionRecords", [])
                for rec in assertions:
                    details = rec.get("assertionDetails", {})
                    mode_id = details.get("assertionDetailsModeIdentifier", "")
                    if mode_id and "com.apple.donotdisturb.mode" not in mode_id:
                        # Custom focus mode
                        active_mode = mode_id.split(".")[-1] if "." in mode_id else mode_id
                        break
                    elif mode_id:
                        active_mode = "Do Not Disturb"
                        break
    except Exception:
        pass

    # Try to map mode IDs to friendly names via ModeConfigurations
    try:
        if mode_config_path.exists() and active_mode not in ("None", "Do Not Disturb"):
            config = json.loads(mode_config_path.read_text("utf-8"))
            modes = config.get("data", [{}])
            if isinstance(modes, list) and modes:
                for mode_def in modes[0].get("modeConfigurations", {}).values():
                    if active_mode in str(mode_def.get("identifier", "")):
                        active_mode = mode_def.get("name", active_mode)
                        break
    except Exception:
        pass

    return active_mode


def focus_context(mode: str | None = None) -> str:
    """Return vault context for the current (or given) Focus mode.

    Returns a structured summary of relevant projects, tags, and suggested actions.
    """
    if mode is None:
        mode = get_focus_mode()

    log.info(f"Focus mode: {mode}")

    if mode == "None" or mode not in FOCUS_CONTEXT:
        # Default: show all active projects
        lines = [f"Focus mode: {mode} (no specific context mapped)", ""]
        lines.append("Active projects (all):")
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT path, title FROM notes WHERE type='project' AND status='active' ORDER BY path"
            ).fetchall()
            conn.close()
            for r in rows:
                lines.append(f"  - {r['title']} ({r['path']})")
        except Exception:
            lines.append("  (could not query DB)")
        return "\n".join(lines)

    ctx = FOCUS_CONTEXT[mode]
    lines = [f"Focus mode: **{mode}**", ""]

    # Show relevant projects
    if ctx.get("projects"):
        lines.append("Relevant projects:")
        for p in ctx["projects"]:
            lines.append(f"  - [[{p}]]")

    # Show tasks for those projects
    try:
        conn = get_db()
        today_str = date.today().isoformat()
        all_tasks = conn.execute(
            "SELECT text, due, note_path FROM tasks WHERE checked=0 ORDER BY due"
        ).fetchall()
        relevant = []
        for t in all_tasks:
            for tag in ctx.get("tags", []):
                if tag in t["note_path"].lower():
                    relevant.append(t)
                    break
            else:
                for proj in ctx.get("projects", []):
                    if proj.lower() in t["note_path"].lower() or proj.lower() in t["text"].lower():
                        relevant.append(t)
                        break
        conn.close()

        if relevant:
            lines.append(f"\nOpen tasks ({len(relevant)}):")
            for t in relevant[:10]:
                due_str = f" (due {t['due']})" if t["due"] else ""
                lines.append(f"  - [ ] {t['text']}{due_str}")
    except Exception:
        pass

    return "\n".join(lines)


# ── Apple Shortcuts ────────────────────────────────────────────────────────────

def run_shortcut(name: str) -> str:
    """Run an Apple Shortcut by name and return its output."""
    log.info(f"Running Shortcut: {name}")
    try:
        r = subprocess.run(
            ["shortcuts", "run", name],
            capture_output=True, text=True, timeout=30,
        )
        output = r.stdout.strip()
        if r.returncode != 0:
            err = r.stderr.strip() or "unknown error"
            log.warning(f"Shortcut '{name}' failed: {err}")
            return f"Shortcut '{name}' failed: {err}"
        log.info(f"Shortcut '{name}' completed")
        return output or f"Shortcut '{name}' completed (no output)."
    except subprocess.TimeoutExpired:
        return f"Shortcut '{name}' timed out after 30s."
    except FileNotFoundError:
        return "shortcuts CLI not found. Is macOS Shortcuts installed?"


def list_shortcuts() -> str:
    """List all available Apple Shortcuts."""
    try:
        r = subprocess.run(
            ["shortcuts", "list"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() or "No shortcuts found."
    except Exception as e:
        return f"Error listing shortcuts: {e}"


# ── Main ───────────────────────────────────────────────────────────────────────

def cmd_sync(args) -> None:
    """Subcommand: bidirectional sync + daily note."""
    today = date.today()
    today_str = today.isoformat()
    day_name = today.strftime("%A")
    log.info(f"=== vault_cron sync: {today_str} ({day_name}) ===")

    counts = sync_bidirectional(dry_run=args.dry_run)

    parts = []
    if counts["pushed"]:
        parts.append(f"{counts['pushed']} → Apple")
    if counts["pulled"]:
        parts.append(f"{counts['pulled']} ← Apple (done)")
    if counts["pushed_complete"]:
        parts.append(f"{counts['pushed_complete']} → Apple (done)")
    if counts["events"]:
        parts.append(f"{counts['events']} events")

    if parts and not args.dry_run:
        notify(f"🔄 {day_name} Vault Sync", " · ".join(parts))
    elif not parts and not args.dry_run:
        conn = get_db()
        r_count = len(query_due_reminders(conn, today_str))
        t_count = len(query_due_tasks(conn, today_str))
        conn.close()
        if r_count or t_count:
            notify(f"📋 {day_name}", f"{r_count} reminder(s), {t_count} task(s) due — all synced")

    ensure_daily_note(today, dry_run=args.dry_run)
    log.info("=== vault_cron sync done ===")


def cmd_pull_calendar(args) -> None:
    """Subcommand: pull Apple Calendar → daily note."""
    result = pull_calendar_events(target_date=args.date, dry_run=args.dry_run)
    print(result)


def cmd_wrapup(args) -> None:
    """Subcommand: evening daily note summary."""
    result = evening_wrapup(target_date=args.date, dry_run=args.dry_run)
    print(result)


def cmd_focus(args) -> None:
    """Subcommand: show Focus mode context."""
    if args.mode:
        result = focus_context(mode=args.mode)
    else:
        result = focus_context()
    print(result)


def cmd_shortcut(args) -> None:
    """Subcommand: run an Apple Shortcut."""
    if args.list:
        print(list_shortcuts())
    else:
        result = run_shortcut(args.name)
        print(result)


def cmd_morning(args) -> None:
    """Subcommand: full morning routine."""
    today = date.today()
    today_str = today.isoformat()
    day_name = today.strftime("%A")
    log.info(f"=== Morning routine: {today_str} ({day_name}) ===")

    # 1. Create daily note
    ensure_daily_note(today, dry_run=args.dry_run)

    # 2. Pull calendar events into daily note
    pull_calendar_events(target_date=today_str, dry_run=args.dry_run)

    # 3. Bidirectional sync
    counts = sync_bidirectional(dry_run=args.dry_run)

    # 4. Import drop zone files
    drop_results = import_drop_zone(dry_run=args.dry_run)
    drop_count = sum(1 for r in drop_results if not r.startswith("Drop zone") and not r.startswith("Created"))

    parts = []
    if drop_count:
        parts.append(f"{drop_count} files imported")
    if counts["pushed"]:
        parts.append(f"{counts['pushed']} → Reminders")
    if counts["pulled"]:
        parts.append(f"{counts['pulled']} completed")
    if counts["events"]:
        parts.append(f"{counts['events']} vault events")

    conn = get_db()
    r_count = len(query_due_reminders(conn, today_str))
    t_count = len(query_due_tasks(conn, today_str))
    conn.close()

    if not args.dry_run:
        body = f"{r_count} reminder(s), {t_count} task(s)"
        if parts:
            body += " · " + " · ".join(parts)
        notify(f"☀️ Good morning! {day_name}", body)

    log.info("=== Morning routine done ===")


def run(dry_run: bool = False, notify_only: bool = False) -> None:
    today = date.today()
    today_str = today.isoformat()
    day_name = today.strftime("%A")
    log.info(f"=== vault_cron run: {today_str} ({day_name}) ===")

    if not notify_only:
        counts = sync_bidirectional(dry_run=dry_run)
    else:
        counts = {"pushed": 0, "pulled": 0, "pushed_complete": 0, "events": 0}

    # ── Summary notification ───────────────────────────────────────────────
    parts = []
    if counts["pushed"]:
        parts.append(f"{counts['pushed']} → Apple")
    if counts["pulled"]:
        parts.append(f"{counts['pulled']} ← Apple (done)")
    if counts["pushed_complete"]:
        parts.append(f"{counts['pushed_complete']} → Apple (done)")
    if counts["events"]:
        parts.append(f"{counts['events']} events")

    if parts and not dry_run:
        notify(f"🔄 {day_name} Vault Sync", " · ".join(parts))
    elif not parts and not dry_run:
        # Still check if there are items due for a heads-up
        conn = get_db()
        r_count = len(query_due_reminders(conn, today_str))
        t_count = len(query_due_tasks(conn, today_str))
        conn.close()
        if r_count or t_count:
            notify(f"📋 {day_name}", f"{r_count} reminder(s), {t_count} task(s) due — all synced")
        else:
            notify(f"☀️ {day_name}", "Nothing due today. Enjoy!")

    # ── Daily note ─────────────────────────────────────────────────────────
    if not notify_only:
        ensure_daily_note(today, dry_run=dry_run)

    log.info("=== vault_cron done ===")


# =============================================================================
# Drop zone file import
# =============================================================================

def _drop_zone() -> Path:
    """The drop zone is the vault's 90_Inbox/inbox/ folder."""
    return VAULT_ROOT / "90_Inbox" / "inbox"

# Extension → 40_Attachments/ subfolder
_ATTACH_ROUTING: dict[str, str] = {
    ".png": "Images", ".jpg": "Images", ".jpeg": "Images",
    ".gif": "Images", ".webp": "Images", ".svg": "Images",
    ".bmp": "Images", ".ico": "Images", ".heic": "Images",
    ".pdf": "PDFs",
    ".excalidraw": "Excalidraw",
}


def import_drop_zone(dry_run: bool = False) -> list[str]:
    """
    Process anything dropped into 90_Inbox/inbox/ (the vault's universal drop zone).

    - Markdown files: stay in 90_Inbox/inbox/; missing frontmatter is added.
    - Images → copied to 40_Attachments/Images/ + inbox note embeds them.
    - PDFs → copied to 40_Attachments/PDFs/ + inbox note.
    - Other binaries → 40_Attachments/Attachments/ + inbox note.

    After a binary is copied to its attachment location, the original is deleted
    from 90_Inbox/inbox/ (the file lives at 40_Attachments/ now). Returns a list
    of human-readable result lines.
    """
    import shutil

    drop = _drop_zone()
    if not drop.exists():
        drop.mkdir(parents=True, exist_ok=True)
        log.info(f"Created drop zone: {drop}")
        return [f"Created drop zone at {drop.relative_to(VAULT_ROOT)} — drop files there."]

    files = sorted(f for f in drop.iterdir() if f.is_file() and not f.name.startswith("."))
    if not files:
        return ["Drop zone empty — nothing to import."]

    results: list[str] = []
    today = date.today().isoformat()
    inbox_dir = drop  # alias for clarity below

    for src in files:
        ext = src.suffix.lower()
        name = src.name
        stem = src.stem

        if dry_run:
            results.append(f"[dry-run] Would process: {name}")
            continue

        if ext == ".md":
            # Already in place — just ensure frontmatter
            content = src.read_text(encoding="utf-8", errors="replace")
            if not content.startswith("---"):
                content = (
                    f"---\n"
                    f"type: inbox\n"
                    f"status: needs-review\n"
                    f"tags:\n"
                    f"  - imported\n"
                    f"last_updated: {today}\n"
                    f"---\n"
                    f"{content}"
                )
                src.write_text(content, encoding="utf-8")
                results.append(f"Stamped frontmatter: {src.relative_to(VAULT_ROOT)}")
            else:
                results.append(f"In place (already has frontmatter): {src.relative_to(VAULT_ROOT)}")
            continue

        # Binary/attachment file
        subfolder = _ATTACH_ROUTING.get(ext, "Attachments")
        attach_dir = VAULT_ROOT / "40_Attachments" / subfolder
        attach_dir.mkdir(parents=True, exist_ok=True)

        dest = attach_dir / name
        counter = 1
        while dest.exists():
            dest = attach_dir / f"{stem}_{counter}{ext}"
            counter += 1

        shutil.copy2(str(src), str(dest))
        vault_name = dest.name
        rel_dest = str(dest.relative_to(VAULT_ROOT))

        # Create inbox note referencing the file
        is_image = subfolder == "Images"
        embed = f"![[{vault_name}]]" if is_image else f"[[{vault_name}]]"

        note_path = inbox_dir / f"{stem}.md"
        note_counter = 1
        while note_path.exists():
            note_path = inbox_dir / f"{stem}_{note_counter}.md"
            note_counter += 1

        note_content = (
            f"---\n"
            f"type: inbox\n"
            f"status: needs-review\n"
            f"tags:\n"
            f"  - imported\n"
            f"last_updated: {today}\n"
            f"---\n"
            f"# {stem}\n\n"
            f"{embed}\n\n"
            f"## Related Notes\n"
        )
        note_path.write_text(note_content, encoding="utf-8")
        results.append(f"Imported {subfolder.lower()}: {rel_dest} → {note_path.relative_to(VAULT_ROOT)}")

        # Move original to vault trash (so it's recoverable from Obsidian)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_dir = VAULT_ROOT / "90_Inbox" / "_trash" / ts
        trash_dir.mkdir(parents=True, exist_ok=True)
        trash_dest = trash_dir / src.name
        try:
            shutil.move(str(src), str(trash_dest))
        except Exception as e:
            log.warning(f"Could not trash original {src.name}: {e}")

    log.info(f"Drop zone import: {len(results)} files processed")
    return results


def cmd_import_drop_zone(args: argparse.Namespace) -> None:
    results = import_drop_zone(dry_run=getattr(args, "dry_run", False))
    for line in results:
        print(line)


# ── Quick capture (standalone, MCP-free) ─────────────────────────────────────
# Write a thought to 90_Inbox/inbox/ without going through Claude/MCP. Designed
# for iOS Shortcuts, hotkey scripts, terminal one-liners, etc.

def quick_capture_cli(thought: str, title: str = "", tags: list[str] | None = None) -> str:
    if not thought.strip():
        return "err: nothing to capture (empty thought)"
    now = datetime.now()
    date_str = now.date().isoformat()
    time_slug = now.strftime("%H%M%S")
    if title.strip():
        slug_chars = [ch.lower() if ch.isalnum() else "_" for ch in title.strip()]
        slug = "_".join(p for p in "".join(slug_chars).split("_") if p) or "capture"
    else:
        slug = "capture"
    filename = f"{date_str}_{time_slug}_{slug}.md"
    target = VAULT_ROOT / "90_Inbox" / "inbox" / filename
    target.parent.mkdir(parents=True, exist_ok=True)

    tag_list = ["inbox"] + [t.strip() for t in (tags or []) if t.strip()]
    fm_lines = [
        "---",
        "type: capture",
        f"created: {date_str}",
        "status: inbox",
        "tags:",
        *(f"  - {t}" for t in tag_list),
        "---",
        "",
        f"# {title.strip() or 'Quick Capture'}",
        "",
        thought.strip(),
        "",
    ]
    target.write_text("\n".join(fm_lines), encoding="utf-8")
    log.info(f"Quick capture → {target.relative_to(VAULT_ROOT)}")
    return str(target.relative_to(VAULT_ROOT))


def cmd_capture(args: argparse.Namespace) -> None:
    # Source priority: --text > positional text > stdin (if --stdin or piped)
    text = ""
    if getattr(args, "text", None):
        text = args.text
    elif getattr(args, "text_args", None):
        text = " ".join(args.text_args)
    if not text and (args.stdin or not sys.stdin.isatty()):
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        print("err: nothing to capture — pass text as arg, --text, or via stdin")
        sys.exit(1)
    tags = [t.strip() for t in (args.tag or []) if t.strip()]
    rel = quick_capture_cli(text, title=args.title or "", tags=tags)
    print(rel)
    if args.notify:
        notify("📝 Captured to inbox", rel)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Obsidian vault automation (MocchiMind)")
    sub = parser.add_subparsers(dest="command")

    # sync
    p_sync = sub.add_parser("sync", help="Bidirectional Obsidian ↔ Apple Reminders/Calendar sync")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    # pull-calendar
    p_cal = sub.add_parser("pull-calendar", help="Pull Apple Calendar events into daily note")
    p_cal.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p_cal.add_argument("--dry-run", action="store_true")
    p_cal.set_defaults(func=cmd_pull_calendar)

    # wrapup
    p_wrap = sub.add_parser("wrapup", help="Evening daily note summary")
    p_wrap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p_wrap.add_argument("--dry-run", action="store_true")
    p_wrap.set_defaults(func=cmd_wrapup)

    # focus
    p_focus = sub.add_parser("focus", help="Show Focus mode context")
    p_focus.add_argument("--mode", default=None, help="Override Focus mode (Work, Personal, Study)")
    p_focus.set_defaults(func=cmd_focus)

    # shortcut
    p_short = sub.add_parser("shortcut", help="Run an Apple Shortcut")
    p_short.add_argument("name", nargs="?", default="", help="Shortcut name")
    p_short.add_argument("--list", action="store_true", help="List all shortcuts")
    p_short.set_defaults(func=cmd_shortcut)

    # morning
    p_morning = sub.add_parser("morning", help="Full morning routine (daily note + calendar + sync)")
    p_morning.add_argument("--dry-run", action="store_true")
    p_morning.set_defaults(func=cmd_morning)

    # import-drop-zone
    p_import = sub.add_parser("import-drop-zone", help="Process files in 90_Inbox/inbox/")
    p_import.add_argument("--dry-run", action="store_true")
    p_import.set_defaults(func=cmd_import_drop_zone)

    # capture — standalone (no MCP needed) for iOS Shortcuts / hotkeys / pipelines
    p_cap = sub.add_parser(
        "capture",
        help="Drop a thought into 90_Inbox/inbox/ (iOS Shortcuts / CLI / piped stdin)",
    )
    p_cap.add_argument("text_args", nargs="*", help="Positional capture text")
    p_cap.add_argument("--text", default="", help="The thought text")
    p_cap.add_argument("--title", default="", help="Optional title (becomes # heading)")
    p_cap.add_argument("--tag", action="append", default=[],
                       help="Tag to add (repeatable). 'inbox' is always added.")
    p_cap.add_argument("--stdin", action="store_true",
                       help="Read text from stdin (auto-detected when piped)")
    p_cap.add_argument("--notify", action="store_true",
                       help="Show a macOS notification when done")
    p_cap.set_defaults(func=cmd_capture)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    elif args.command is None:
        # Backward compat: no subcommand → run legacy sync
        # Also handle --dry-run --notify-only for launchd compat
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--notify-only", action="store_true")
        args = parser.parse_args()
        run(dry_run=args.dry_run, notify_only=args.notify_only)
    else:
        parser.print_help()
