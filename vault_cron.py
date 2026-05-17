#!/usr/bin/env python3
"""
vault_cron.py — Lightweight automation for the Obsidian AI_Memory vault.

Runs independently of Claude Code / MCP. No LLM required.
Also callable from MCP tools as a subprocess.

Subcommands:
  python3 vault_cron.py wrapup            # evening daily note summary
  python3 vault_cron.py morning           # full morning routine (daily note + drop-zone import)
  python3 vault_cron.py import-drop-zone  # process files in 90_Inbox/inbox/
  python3 vault_cron.py weekly-summary    # generate weekly summary note (ISO week of today)
  python3 vault_cron.py weekly-summary --end-date 2026-05-17 --force
  python3 vault_cron.py capture <text>    # drop a thought into 90_Inbox/inbox/
"""

from __future__ import annotations

import argparse
import calendar
import logging
import re
import sqlite3
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

# ── Configuration ──────────────────────────────────────────────────────────────
# All config (vault root, etc.) lives in iris_config.py — sibling module, zero
# deps. Override via env vars or ~/.config/iris/config.toml.

import iris_config as cfg

VAULT_ROOT = cfg.VAULT_ROOT
DB_PATH = cfg.vault_db_path()
LOG_PATH = cfg.vault_cache_dir() / "vault_cron.log"

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
    return summary_text


# ── Weekly Summary ─────────────────────────────────────────────────────────────

WEEKLY_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _iso_week_bounds(end_date: date) -> tuple[date, date, int, int]:
    """Return (monday, sunday, iso_year, iso_week) for the ISO week containing end_date."""
    iso_year, iso_week, iso_weekday = end_date.isocalendar()
    monday = end_date - timedelta(days=iso_weekday - 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday, iso_year, iso_week


def _weekly_note_path(iso_year: int, iso_week: int) -> Path:
    return VAULT_ROOT / "30_Episodic" / str(iso_year) / "Weekly" / f"{iso_year}-W{iso_week:02d}.md"


def weekly_summary(
    end_date: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Generate a weekly summary note covering the ISO week containing end_date.

    Writes to 30_Episodic/{iso_year}/Weekly/{iso_year}-W{NN}.md.
    Skips if the file already exists unless force=True.
    """
    d = date.fromisoformat(end_date) if end_date else date.today()
    monday, sunday, iso_year, iso_week = _iso_week_bounds(d)
    start_str = monday.isoformat()
    end_str = sunday.isoformat()
    week_label = f"{iso_year}-W{iso_week:02d}"

    log.info(f"Weekly summary for {week_label} ({start_str} → {end_str})")

    note_path = _weekly_note_path(iso_year, iso_week)
    if note_path.exists() and not force and not dry_run:
        log.info(f"Weekly note already exists: {note_path.name} (use --force to overwrite)")
        return f"Already exists: {note_path.relative_to(VAULT_ROOT)} (use --force to overwrite)"

    conn = get_db()

    # Tasks completed in the window
    completed_tasks = conn.execute(
        "SELECT text, done, note_path FROM tasks "
        "WHERE checked = 1 AND done != '' AND done BETWEEN ? AND ? "
        "ORDER BY done, text",
        (start_str, end_str),
    ).fetchall()

    # Reminders done in the window
    completed_reminders = conn.execute(
        "SELECT text, done, note_path FROM reminders "
        "WHERE checked = 1 AND done != '' AND done BETWEEN ? AND ? "
        "ORDER BY done, text",
        (start_str, end_str),
    ).fetchall()

    # Events in the window
    events = conn.execute(
        "SELECT date, time, end_time, title, location FROM events "
        "WHERE date BETWEEN ? AND ? "
        "ORDER BY date, time",
        (start_str, end_str),
    ).fetchall()

    # Open tasks (still incomplete) — surface ones overdue or due soon
    open_tasks = conn.execute(
        "SELECT text, due, note_path FROM tasks "
        "WHERE checked = 0 AND due != '' AND due <= ? "
        "ORDER BY due, text",
        (end_str,),
    ).fetchall()

    # Notes modified during the week (excluding daily notes, weekly notes, and caches)
    cutoff_start_ns = int(datetime.combine(monday, datetime.min.time()).timestamp() * 1e9)
    cutoff_end_ns = int(datetime.combine(sunday + timedelta(days=1), datetime.min.time()).timestamp() * 1e9)
    try:
        recent_rows = conn.execute(
            "SELECT path, mtime_ns FROM files "
            "WHERE mtime_ns >= ? AND mtime_ns < ? AND path LIKE '%.md' "
            "ORDER BY mtime_ns DESC",
            (cutoff_start_ns, cutoff_end_ns),
        ).fetchall()
    except sqlite3.OperationalError:
        # `files` table might not exist in older indexes — fall back to filesystem scan
        recent_rows = []
        for p in VAULT_ROOT.rglob("*.md"):
            if ".ai_memory_cache" in str(p) or ".trash" in str(p):
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime).date()
                if monday <= mtime <= sunday:
                    recent_rows.append({"path": str(p.relative_to(VAULT_ROOT)), "mtime_ns": 0})
            except OSError:
                continue

    conn.close()

    def _is_periodic_note(path: str) -> bool:
        """Skip daily notes and the weekly note itself when listing notes-touched."""
        return (
            re.search(r"30_Episodic/\d{4}/\d{4}-\d{2}-\d{2}\.md$", path) is not None
            or "30_Episodic/" in path and "/Weekly/" in path
        )

    modified_notes = [
        dict(r) if not isinstance(r, dict) else r
        for r in recent_rows
        if not _is_periodic_note(r["path"] if not isinstance(r, dict) else r["path"])
    ]

    # ── Build markdown ─────────────────────────────────────────────────────
    pretty_start = monday.strftime("%b %d")
    pretty_end = sunday.strftime("%b %d, %Y")

    lines: list[str] = [
        "---",
        f"week: {week_label}",
        f"start: {start_str}",
        f"end: {end_str}",
        "tags:",
        "  - weekly",
        "type: weekly",
        f"generated: {datetime.now().isoformat(timespec='seconds')}",
        "---",
        f"# {week_label} — {pretty_start} → {pretty_end}",
        "",
        "## Highlights",
        f"- **{len(completed_tasks)}** tasks completed",
        f"- **{len(completed_reminders)}** reminders done",
        f"- **{len(events)}** calendar events",
        f"- **{len(modified_notes)}** notes touched",
    ]
    if open_tasks:
        lines.append(f"- **{len(open_tasks)}** open tasks carried over")

    # Tasks completed
    lines.append("")
    lines.append(f"## Tasks Completed ({len(completed_tasks)})")
    if completed_tasks:
        for t in completed_tasks:
            note_link = f" — [[{t['note_path'][:-3]}]]" if t["note_path"].endswith(".md") else ""
            lines.append(f"- [x] {t['text']} _(done {t['done']})_{note_link}")
    else:
        lines.append("_None this week._")

    # Reminders done
    if completed_reminders:
        lines.append("")
        lines.append(f"## Reminders Done ({len(completed_reminders)})")
        for r in completed_reminders:
            lines.append(f"- ~~{r['text']}~~ _(done {r['done']})_")

    # Events — grouped by day
    lines.append("")
    lines.append(f"## Events ({len(events)})")
    if events:
        current_day = ""
        for ev in events:
            if ev["date"] != current_day:
                current_day = ev["date"]
                try:
                    weekday = WEEKLY_DAY_NAMES[date.fromisoformat(current_day).weekday()]
                except ValueError:
                    weekday = ""
                lines.append(f"### {weekday} {current_day}")
            t = ev["time"] or ""
            if ev["end_time"]:
                t = f"{t}–{ev['end_time']}"
            line = f"- {t} {ev['title']}".rstrip()
            if ev["location"]:
                line += f" @ {ev['location']}"
            lines.append(line)
    else:
        lines.append("_No events._")

    # Notes touched
    lines.append("")
    lines.append(f"## Notes Touched ({len(modified_notes)})")
    if modified_notes:
        for n in modified_notes[:30]:
            path = n["path"]
            if path.endswith(".md"):
                lines.append(f"- [[{path[:-3]}]]")
            else:
                lines.append(f"- {path}")
        if len(modified_notes) > 30:
            lines.append(f"- _…and {len(modified_notes) - 30} more_")
    else:
        lines.append("_No notes modified._")

    # Still open
    if open_tasks:
        lines.append("")
        lines.append(f"## Still Open ({len(open_tasks)})")
        for t in open_tasks[:20]:
            due_str = f" _(due {t['due']})_" if t["due"] else ""
            note_link = f" — [[{t['note_path'][:-3]}]]" if t["note_path"].endswith(".md") else ""
            lines.append(f"- [ ] {t['text']}{due_str}{note_link}")
        if len(open_tasks) > 20:
            lines.append(f"- _…and {len(open_tasks) - 20} more_")

    content = "\n".join(lines) + "\n"

    if dry_run:
        log.info(f"[DRY RUN] Would write weekly note to {note_path.relative_to(VAULT_ROOT)}")
        return content

    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content, "utf-8")
    log.info(f"Wrote weekly summary: {note_path.relative_to(VAULT_ROOT)}")
    return f"Wrote {note_path.relative_to(VAULT_ROOT)}\n\n{content}"


# ── Main ───────────────────────────────────────────────────────────────────────

def cmd_wrapup(args) -> None:
    """Subcommand: evening daily note summary."""
    result = evening_wrapup(target_date=args.date, dry_run=args.dry_run)
    print(result)


def cmd_weekly_summary(args) -> None:
    """Subcommand: generate weekly summary note."""
    result = weekly_summary(
        end_date=args.end_date,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(result)


def cmd_morning(args) -> None:
    """Subcommand: morning routine (daily note + drop-zone import)."""
    today = date.today()
    today_str = today.isoformat()
    day_name = today.strftime("%A")
    log.info(f"=== Morning routine: {today_str} ({day_name}) ===")

    # 1. Create daily note
    ensure_daily_note(today, dry_run=args.dry_run)

    # 2. Import drop zone files
    import_drop_zone(dry_run=args.dry_run)

    log.info("=== Morning routine done ===")


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Obsidian vault automation (MocchiMind)")
    sub = parser.add_subparsers(dest="command")

    # wrapup
    p_wrap = sub.add_parser("wrapup", help="Evening daily note summary")
    p_wrap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p_wrap.add_argument("--dry-run", action="store_true")
    p_wrap.set_defaults(func=cmd_wrapup)

    # morning
    p_morning = sub.add_parser("morning", help="Morning routine (daily note + drop-zone import)")
    p_morning.add_argument("--dry-run", action="store_true")
    p_morning.set_defaults(func=cmd_morning)

    # import-drop-zone
    p_import = sub.add_parser("import-drop-zone", help="Process files in 90_Inbox/inbox/")
    p_import.add_argument("--dry-run", action="store_true")
    p_import.set_defaults(func=cmd_import_drop_zone)

    # capture — standalone (no MCP needed) for hotkeys / pipelines / stdin
    p_cap = sub.add_parser(
        "capture",
        help="Drop a thought into 90_Inbox/inbox/ (CLI / piped stdin)",
    )
    p_cap.add_argument("text_args", nargs="*", help="Positional capture text")
    p_cap.add_argument("--text", default="", help="The thought text")
    p_cap.add_argument("--title", default="", help="Optional title (becomes # heading)")
    p_cap.add_argument("--tag", action="append", default=[],
                       help="Tag to add (repeatable). 'inbox' is always added.")
    p_cap.add_argument("--stdin", action="store_true",
                       help="Read text from stdin (auto-detected when piped)")
    p_cap.set_defaults(func=cmd_capture)

    # weekly-summary
    p_weekly = sub.add_parser("weekly-summary", help="Generate weekly summary note")
    p_weekly.add_argument("--end-date", default=None,
                          help="YYYY-MM-DD (any day in target week; default: today)")
    p_weekly.add_argument("--force", action="store_true",
                          help="Overwrite existing weekly note")
    p_weekly.add_argument("--dry-run", action="store_true")
    p_weekly.set_defaults(func=cmd_weekly_summary)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()
