"""Generated index helpers; Project status; Vault index management; Hotness & revisions

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


# ─── from original L3044-3168: Generated index helpers ───
# =============================================================================
# Generated index helpers
# =============================================================================


def all_markdown_notes(include_index: bool = True) -> list[Path]:
    root = get_vault_root()
    notes = []
    for path in root.rglob("*.md"):
        if is_ignored_path(path):
            continue
        rel = relative_to_vault(path)
        if not include_index and rel.startswith("00_Index/"):
            continue
        notes.append(path)
    notes.sort(key=lambda p: relative_to_vault(p).lower())
    return notes


def note_modified_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def get_note_summary_for_index(path: Path) -> dict[str, Any]:
    text = read_text(path)
    data, _ = split_frontmatter(text)
    rel = relative_to_vault(path)
    tags_raw = data.get("tags", [])
    aliases_raw = data.get("aliases", [])
    tags = [tags_raw] if isinstance(tags_raw, str) else [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
    aliases = [aliases_raw] if isinstance(aliases_raw, str) else [str(a) for a in aliases_raw] if isinstance(aliases_raw, list) else []
    return {
        "path": rel,
        "target": normalize_note_target(rel),
        "title": title_from_text(text, path.stem),
        "type": str(data.get("type", "")).strip(),
        "status": str(data.get("status", "")).strip(),
        "priority": str(data.get("priority", "")).strip(),
        "tags": unique_preserve_order(tags),
        "aliases": unique_preserve_order(aliases),
        "modified": note_modified_date(path),
    }


def write_generated_index(path: str, content: str) -> str:
    note = safe_path(path)
    if vault_suffix(note) != ".md":
        return "Generated index path must end in .md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(content.rstrip() + "\n", encoding="utf-8")
    return f"Wrote generated index: {path}"



@mcp.tool()
def rebuild_dashboard(output_path: str = "00_Index/dashboard.md") -> str:
    now = datetime.now().isoformat(timespec="seconds")
    profile_notes = []
    active_projects = []
    for path in all_markdown_notes(include_index=False):
        info = get_note_summary_for_index(path)
        if info["path"].startswith("10_Profile/"):
            profile_notes.append(info)
        elif info["path"].startswith("20_Projects/") and info["status"].lower() in {"active", "ongoing", ""}:
            active_projects.append(info)
    profile_notes.sort(key=lambda x: x["path"].lower())
    active_projects.sort(key=lambda x: (x["priority"] != "high", x["path"].lower()))

    lines = [
        "---",
        "type: dashboard",
        f"last_updated: {now}",
        "tags:",
        "  - index",
        "  - dashboard",
        "---",
        "",
        "# AI Memory Dashboard",
        "",
        "> **Start here →** [[00_Index/Central Hub|Central Hub]]",
        "",
        "## Main Indexes",
        "",
        "- [[00_Index/memory_index|Memory Index]]",
        "- [[00_Index/folder_index|Folder Index]]",
        "- [[00_Index/concept_index|Concept Index]]",
        "- [[00_Index/alias_index|Alias Index]]",
        "- [[00_Index/active_projects|Active Projects]]",
        "- [[00_Index/property_schema|Property Schema]]",
        "",
        "> [!info] SQLite-backed queries",
        "> File index, tag index, type index, recently updated, needs review, tasks, and reminders are now served directly from the SQLite database (`rebuild_vault_index`). No flat-file indexes needed.",
        "",
        "## Core Profile",
        "",
    ]
    lines.extend(f"- [[{i['target']}|{i['title']}]]" for i in profile_notes)
    if not profile_notes:
        lines.append("- No profile notes found.")
    lines.extend(["", "## Active Projects", ""])
    lines.extend(f"- [[{i['target']}|{i['title']}]]" for i in active_projects)
    if not active_projects:
        lines.append("- No active project notes found.")
    lines.extend(
        [
            "",
            "## Maintenance",
            "",
            "- [ ] Run `find_issues(counts_only=True)` for a health summary.",
            "- [ ] Run `rebuild_vault_index` to refresh the SQLite database.",
            "- [ ] Use `search_vault_files` for non-Markdown retrieval.",
        ]
    )
    return write_generated_index(output_path, "\n".join(lines))


@mcp.tool()
def rebuild_all_generated_indexes() -> str:
    """Rebuild the SQLite vault index and regenerate the dashboard."""
    results = []
    results.append(rebuild_vault_index(force=True))
    results.append(rebuild_dashboard())
    return "\n".join(results)



# ─── from original L8144-8278: Project status ───
# =============================================================================
# Project status overview
# =============================================================================


@mcp.tool()
def project_status(project: str = "") -> str:
    """
    Overview of active projects with open tasks, questions, and decisions.

    If ``project`` is given, show details for that project only.
    Otherwise show a summary of all active projects.
    """
    idx = get_vault_index()
    c = idx.conn
    root = get_vault_root()

    if project.strip():
        # Find the specific project
        query = project.strip()
        # Try alias resolution
        alias_paths = idx.query_aliases(query)
        # Try title match
        title_rows = c.execute(
            "SELECT path, title FROM notes WHERE title LIKE ? COLLATE NOCASE AND type = 'project' LIMIT 5",
            (f"%{query}%",),
        ).fetchall()
        # Try path match
        path_rows = c.execute(
            "SELECT path, title FROM notes WHERE path LIKE ? AND type = 'project' LIMIT 5",
            (f"%{query}%",),
        ).fetchall()

        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for p in alias_paths:
            if p not in seen:
                seen.add(p)
                row = c.execute("SELECT title FROM notes WHERE path = ?", (p,)).fetchone()
                candidates.append((p, row["title"] if row else p))
        for r in title_rows:
            if r["path"] not in seen:
                seen.add(r["path"])
                candidates.append((r["path"], r["title"]))
        for r in path_rows:
            if r["path"] not in seen:
                seen.add(r["path"])
                candidates.append((r["path"], r["title"]))

        if not candidates:
            return f"No project found matching '{query}'."
        if len(candidates) > 1:
            return "Multiple matches:\n" + "\n".join(
                f"- {make_wikilink(p, t)}" for p, t in candidates
            )

        proj_path, proj_title = candidates[0]
        lines = [f"# Project: {proj_title}", f"Path: {proj_path}"]

        # Open tasks for this project
        tasks = c.execute(
            "SELECT text, due, priority FROM tasks WHERE note_path = ? AND checked = 0 ORDER BY due",
            (proj_path,),
        ).fetchall()
        lines.append(f"\n## Open Tasks ({len(tasks)})")
        for t in tasks:
            due_info = f" (due {t['due']})" if t["due"] else ""
            pri = f" [{t['priority']}]" if t["priority"] else ""
            lines.append(f"- [ ] {t['text']}{due_info}{pri}")

        # Pending reminders
        reminders = c.execute(
            "SELECT text, remind_on FROM reminders WHERE note_path = ? AND checked = 0 ORDER BY remind_on",
            (proj_path,),
        ).fetchall()
        if reminders:
            lines.append(f"\n## Pending Reminders ({len(reminders)})")
            for r in reminders:
                lines.append(f"- {r['text']} (on {r['remind_on']})")

        # Upcoming events mentioning this project
        events = c.execute(
            "SELECT date, time, title FROM events WHERE note_path = ? ORDER BY date, time",
            (proj_path,),
        ).fetchall()
        if events:
            lines.append(f"\n## Events ({len(events)})")
            for ev in events:
                lines.append(f"- {ev['date']} {ev['time']} {ev['title']}")

        # Read the note body for any ## Decisions or ## Open Questions
        note_file = root / proj_path
        if note_file.exists():
            proj_text = read_text(note_file)
            for section_name in ("Decisions", "Open Questions", "Next Actions"):
                bounds = find_section_bounds(proj_text, section_name)
                if bounds:
                    sec_text = proj_text[bounds[0]:bounds[1]].strip()
                    if sec_text:
                        lines.append(f"\n## {section_name}")
                        lines.append(sec_text)

        return "\n".join(lines)

    # --- All projects overview ---
    project_rows = c.execute(
        "SELECT path, title, status FROM notes WHERE type = 'project' ORDER BY status DESC, path"
    ).fetchall()
    if not project_rows:
        return "No project notes found."

    lines = [f"# Projects Overview ({len(project_rows)})"]
    active = [r for r in project_rows if r["status"] == "active"]
    other = [r for r in project_rows if r["status"] != "active"]

    if active:
        lines.append(f"\n## Active ({len(active)})")
        for pr in active:
            # Count open tasks
            task_count = c.execute(
                "SELECT COUNT(*) AS cnt FROM tasks WHERE note_path = ? AND checked = 0",
                (pr["path"],),
            ).fetchone()["cnt"]
            task_info = f" — {task_count} open task(s)" if task_count > 0 else ""
            lines.append(f"- {make_wikilink(pr['path'], pr['title'])}{task_info}")

    if other:
        lines.append(f"\n## Other ({len(other)})")
        for pr in other:
            status = f" [{pr['status']}]" if pr["status"] else ""
            lines.append(f"- {make_wikilink(pr['path'], pr['title'])}{status}")

    return "\n".join(lines)



# ─── from original L8279-8297: Vault index management ───
# =============================================================================
# Vault index management
# =============================================================================


@mcp.tool()
def rebuild_vault_index(force: bool = False) -> str:
    """Rebuild SQLite vault index. force=True for full rebuild."""
    global _vault_index
    _vault_index = VaultIndex(get_vault_root())
    stats = _vault_index.sync(force=force)
    db = _vault_index.db_stats()

    parts = [f"ok scanned:{stats['scanned']} added:{stats['added']} updated:{stats['updated']} removed:{stats['removed']}"]
    if stats.get("errors"):
        parts.append(f"errors:{stats['errors']}")
    return " ".join(parts)



# ─── from original L8836-8897: Hotness & revisions ───
# =============================================================================
# Hotness & revision tracking tools
# =============================================================================


@mcp.tool()
def note_history(path: str, limit: int = 20) -> str:
    """
    View revision history and access stats for a note.

    Shows when the note was modified, word count at each revision,
    and revision IDs you can pass to ``read_revision`` to view old content.
    """
    limit = max(1, min(limit, 50))
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    rel = relative_to_vault(note)
    idx = get_vault_index()
    revs = idx.get_revisions(rel, limit=limit)
    stats = idx.get_access_stats(rel)

    lines = [f"[revisions:{len(revs)}|reads:{stats['access_count']}|last_read:{stats['last_accessed'] or 'never'}]"]
    if not revs:
        lines.append("No revision history yet. Revisions are saved when notes are overwritten via write_note.")
    else:
        for r in revs:
            lines.append(f"rev:{r['id']}|{r['saved_at']}|{r['word_count']}w|hash:{r['content_hash']}")
    return "\n".join(lines)


@mcp.tool()
def read_revision(revision_id: int) -> str:
    """Read the full content of a historical revision of a note."""
    idx = get_vault_index()
    rev = idx.get_revision_content(revision_id)
    if rev is None:
        return f"Revision {revision_id} not found."
    return (
        f"--- revision {rev['id']} of {rev['path']} "
        f"({rev['saved_at']}, {rev['word_count']}w) ---\n{rev['content']}"
    )


# @mcp.tool()  # removed — use sqlite_query instead
def top_notes(limit: int = 20) -> str:
    """
    Show the most frequently accessed notes (hotness ranking).

    Useful for understanding which notes are most important / referenced.
    """
    limit = max(1, min(limit, 100))
    idx = get_vault_index()
    top = idx.top_accessed(limit=limit)
    if not top:
        return "No access data yet. Notes are tracked when read via read_note."
    lines = [f"[top:{len(top)}]"]
    for r in top:
        lines.append(f"{r['path']}|reads:{r['access_count']}|last:{r['last_accessed']}")
    return "\n".join(lines)


