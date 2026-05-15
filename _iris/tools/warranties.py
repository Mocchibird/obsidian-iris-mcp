"""Warranties

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


# ─── from original L10408-10526: Warranties ───
# =============================================================================
# Warranties
# =============================================================================


def _normalize_warranty_date(s: str) -> str:
    """Accept DD.MM.YYYY or YYYY-MM-DD or DD/MM/YYYY → return YYYY-MM-DD (or empty if unparseable)."""
    s = (s or "").strip()
    if not s:
        return ""
    # YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # DD.MM.YYYY or DD/MM/YYYY
    m = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


@mcp.tool()
def warranty_upsert(
    product: str,
    warranty_until: str = "",
    purchase_date: str = "",
    receipt_path: str = "",
    vendor: str = "",
    price: str = "",
    note: str = "",
    reload_db: bool = True,
) -> str:
    """Insert or update a warranty entry. product: item name. warranty_until / purchase_date: 'DD.MM.YYYY' or 'YYYY-MM-DD' (stored as ISO). receipt_path: vault-relative path or PDF wikilink target. reload_db=True (default) signals Obsidian's SQLite DB Plugin to refresh."""
    if not product.strip():
        return "err: product required"
    until = _normalize_warranty_date(warranty_until)
    purchased = _normalize_warranty_date(purchase_date)
    if warranty_until and not until:
        return f"err: warranty_until must be DD.MM.YYYY or YYYY-MM-DD (got '{warranty_until}')"
    if purchase_date and not purchased:
        return f"err: purchase_date must be DD.MM.YYYY or YYYY-MM-DD (got '{purchase_date}')"

    idx = get_vault_index()
    c = idx.conn
    now = datetime.now().isoformat(timespec="seconds")
    existing = c.execute(
        "SELECT id FROM warranties WHERE product = ? AND receipt_path = ?",
        (product, receipt_path),
    ).fetchone()
    if existing:
        c.execute(
            "UPDATE warranties SET warranty_until = ?, purchase_date = ?, "
            "vendor = ?, price = ?, note = ?, updated_at = ? WHERE id = ?",
            (until, purchased, vendor, price, note, now, existing["id"]),
        )
        c.commit()
        if reload_db: maybe_reload_db_plugin()
        return f"ok updated id:{existing['id']}|{product}"
    cur = c.execute(
        "INSERT INTO warranties (product, warranty_until, purchase_date, receipt_path, vendor, price, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (product, until, purchased, receipt_path, vendor, price, note, now, now),
    )
    c.commit()
    if reload_db: maybe_reload_db_plugin()
    return f"ok inserted id:{cur.lastrowid}|{product}"


@mcp.tool()
def warranty_remove(warranty_id: int, reload_db: bool = True) -> str:
    """Delete a warranty entry by its row id."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM warranties WHERE id = ?", (warranty_id,))
    c.commit()
    if reload_db: maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} id:{warranty_id}"


# @mcp.tool()  # removed — use sqlite_query instead
def warranty_list(include_expired: bool = False, limit: int = 100) -> str:
    """List warranties ordered by expiry (soonest first). include_expired=False (default) hides items already expired. Returns id|product|warranty_until|days_left|receipt per line."""
    idx = get_vault_index()
    c = idx.conn
    if include_expired:
        rows = c.execute(
            "SELECT id, product, warranty_until, receipt_path, "
            "CAST(julianday(warranty_until) - julianday('now') AS INTEGER) AS days_left "
            "FROM warranties WHERE warranty_until != '' ORDER BY warranty_until LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT id, product, warranty_until, receipt_path, days_left "
            "FROM warranties_active LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    if not rows:
        return "none"
    return "\n".join(
        f"{r['id']}|{r['product']}|{r['warranty_until']}|{r['days_left']}d|{r['receipt_path']}"
        for r in rows
    )


# @mcp.tool()  # removed — use sqlite_query instead
def warranty_expiring_within(days: int = 365) -> str:
    """List warranties expiring in the next N days. Returns product|warranty_until|days_left per line, soonest first."""
    days = max(1, min(days, 3650))
    idx = get_vault_index()
    c = idx.conn
    rows = c.execute(
        "SELECT product, warranty_until, days_left FROM warranties_active WHERE days_left <= ? ORDER BY days_left",
        (days,),
    ).fetchall()
    if not rows:
        return f"none — no warranties expiring in the next {days} days"
    return "\n".join(
        f"{r['product']}|{r['warranty_until']}|{r['days_left']}d"
        for r in rows
    )


