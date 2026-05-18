"""People

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


# ─── from original L10274-10407: People ───
# =============================================================================
# People (family, friends, colleagues)
# =============================================================================

_PEOPLE_VALID_CATEGORIES = {"family", "friends", "colleagues", "other"}


def _parse_birthday(s: str) -> tuple[int | None, int | None, int | None]:
    """Parse a birthday string to (day, month, year). Accepts DD.MM.YYYY, DD.MM, YYYY-MM-DD, or MM-DD."""
    s = (s or "").strip()
    if not s:
        return (None, None, None)
    # DD.MM.YYYY or DD.MM
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), (int(m.group(3)) if m.group(3) else None)
        return (d, mo, y)
    # YYYY-MM-DD or MM-DD
    m = re.match(r"^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})$", s)
    if m:
        y = int(m.group(1)) if m.group(1) else None
        mo, d = int(m.group(2)), int(m.group(3))
        return (d, mo, y)
    return (None, None, None)


@mcp.tool()
def people_upsert(
    name: str,
    category: str = "",
    subcategory: str = "",
    relationship: str = "",
    birthday: str = "",
    location: str = "",
    badge: str = "",
    note: str = "",
    page_link: str = "",
    occupation: str = "",
    employer: str = "",
    team: str = "",
    nicknames: str = "",
    email: str = "",
    phone: str = "",
    socials: str = "",
    reload_db: bool = True,
) -> str:
    """Insert or update a person.

    Update semantics: only fields you pass non-empty overwrite the existing
    row. Passing an empty string for an optional field on an UPDATE leaves
    the current value alone — that way you can correct one piece of info
    without having to re-supply everything. (To explicitly clear a field,
    pass a single space " " — it's stripped, but tracked as "user wants
    blank". For now, simpler: edit via sqlite_query when you really need
    to clear.)

    Args:
        name: Required, unique per row.
        category: family|friends|colleagues|other.
        subcategory: free-form (e.g. "uni", "high-school", "church").
        relationship: free-form ("Mom", "Girlfriend", "Colleague").
        birthday: 'DD.MM.YYYY' or 'DD.MM' (year optional).
        location: free-form ("Zurich, CH").
        badge: emoji or short tag shown in views.
        note: free-text catch-all for anything not covered by structured
            fields. Historically used for job titles + nicknames — those
            now have proper columns (`occupation`, `nicknames`) and should
            move there over time.
        page_link: optional path to a dedicated note for this person; if
            empty, auto-detected from 10_Profile/People/<name>.md when
            that note exists.
        occupation: Job title (e.g. "AI Research Intern", "Mother").
        employer: Company / institution (e.g. "Huawei", "ETH Zurich").
        team: Team within employer (e.g. "Algorithm Team", "PTO Kernels").
        nicknames: Comma-separated nicknames (e.g. "Bu, Schwabebe"). For
            full-text alias lookup the existing aliases table on the note
            is more powerful; this is for quick view-table display.
        email: Primary contact email.
        phone: Primary contact phone (free-format, no validation).
        socials: Comma-separated handles, free-format
            (e.g. "@handle on IG, discord:foo#123, signal:+41...").
        reload_db: When True (default), signals Obsidian's SQLite DB
            Plugin to refresh. Pass False for bulk writes and reload once.
    """
    if not name.strip():
        return "err: name required"
    if category and category not in _PEOPLE_VALID_CATEGORIES:
        return f"err: category must be one of {sorted(_PEOPLE_VALID_CATEGORIES)}"
    bd_day, bd_month, bd_year = _parse_birthday(birthday)
    idx = get_vault_index()
    c = idx.conn
    now = datetime.now().isoformat(timespec="seconds")
    existing = c.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()

    # Auto-detect page_link from conventional path if not supplied.
    if not page_link.strip():
        candidate = f"10_Profile/People/{name}.md"
        if safe_path(candidate).exists():
            page_link = candidate
        elif existing and existing["page_link"]:
            page_link = existing["page_link"]

    # Per-column merge: empty string from caller = keep existing value (on
    # UPDATE) or use empty (on INSERT). This makes partial updates safe.
    def merge(passed: str, col: str) -> str:
        if passed.strip():
            return passed.strip()
        if existing is not None:
            try:
                return existing[col] or ""
            except (KeyError, IndexError):
                return ""
        return ""

    if existing:
        c.execute(
            "UPDATE people SET category = ?, subcategory = ?, relationship = ?, "
            "birthday_day = ?, birthday_month = ?, birthday_year = ?, "
            "location = ?, badge = ?, note = ?, page_link = ?, "
            "occupation = ?, employer = ?, team = ?, nicknames = ?, "
            "email = ?, phone = ?, socials = ?, updated_at = ? "
            "WHERE id = ?",
            (merge(category, "category"),
             merge(subcategory, "subcategory"),
             merge(relationship, "relationship"),
             bd_day if bd_day is not None else existing["birthday_day"],
             bd_month if bd_month is not None else existing["birthday_month"],
             bd_year if bd_year is not None else existing["birthday_year"],
             merge(location, "location"),
             merge(badge, "badge"),
             merge(note, "note"),
             page_link,
             merge(occupation, "occupation"),
             merge(employer, "employer"),
             merge(team, "team"),
             merge(nicknames, "nicknames"),
             merge(email, "email"),
             merge(phone, "phone"),
             merge(socials, "socials"),
             now, existing["id"]),
        )
        c.commit()
        if reload_db: maybe_reload_db_plugin()
        return f"ok updated id:{existing['id']}|{name}"
    cur = c.execute(
        "INSERT INTO people (name, category, subcategory, relationship, "
        "birthday_day, birthday_month, birthday_year, "
        "location, badge, note, page_link, "
        "occupation, employer, team, nicknames, email, phone, socials, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, category, subcategory, relationship, bd_day, bd_month, bd_year,
         location, badge, note, page_link,
         occupation.strip(), employer.strip(), team.strip(),
         nicknames.strip(), email.strip(), phone.strip(), socials.strip(),
         now, now),
    )
    c.commit()
    if reload_db: maybe_reload_db_plugin()
    return f"ok inserted id:{cur.lastrowid}|{name}"


@mcp.tool()
def people_remove(name: str, reload_db: bool = True) -> str:
    """Delete a person by name."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM people WHERE name = ?", (name,))
    c.commit()
    if reload_db: maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} {name}"


# @mcp.tool()  # removed — use sqlite_query instead
def people_list(category: str = "", limit: int = 100) -> str:
    """List people, optionally filtered by category. Returns name|category|relationship|birthday|location|note per line."""
    if category and category not in _PEOPLE_VALID_CATEGORIES:
        return f"err: category must be one of {sorted(_PEOPLE_VALID_CATEGORIES)}"
    idx = get_vault_index()
    c = idx.conn
    sql = "SELECT * FROM people"
    params: list = []
    if category:
        sql += " WHERE category = ?"
        params.append(category)
    sql += " ORDER BY category, name COLLATE NOCASE LIMIT ?"
    params.append(max(1, min(limit, 500)))
    rows = c.execute(sql, params).fetchall()
    if not rows:
        return "none"
    out = []
    for r in rows:
        bday = ""
        if r["birthday_day"] and r["birthday_month"]:
            if r["birthday_year"]:
                bday = f"{r['birthday_day']:02d}.{r['birthday_month']:02d}.{r['birthday_year']}"
            else:
                bday = f"{r['birthday_day']:02d}.{r['birthday_month']:02d}"
        out.append(f"{r['name']}|{r['category']}|{r['relationship']}|{bday}|{r['location']}|{r['note'][:80]}")
    return "\n".join(out)


# @mcp.tool()  # removed — use sqlite_query instead
def people_birthdays_upcoming(days: int = 31) -> str:
    """List upcoming birthdays within the next N days (default 31). Returns name|relationship|next_birthday|days_until|turning_age per line."""
    days = max(1, min(days, 365))
    idx = get_vault_index()
    c = idx.conn
    rows = c.execute(
        "SELECT * FROM people_upcoming_birthdays WHERE days_until <= ? ORDER BY days_until",
        (days,),
    ).fetchall()
    if not rows:
        return f"none — no birthdays in the next {days} days"
    out = []
    for r in rows:
        age = ""
        if r["birthday_year"]:
            # next_birthday is YYYY-MM-DD; pull the year off and subtract
            next_year = int(r["next_birthday"][:4])
            age = f" (turning {next_year - r['birthday_year']})"
        out.append(
            f"{r['name']}|{r['relationship']}|{r['next_birthday']}|{r['days_until']}d{age}"
        )
    return "\n".join(out)


