"""sqlite_query, sqlite_schema

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


# ─── from original L536-633: sqlite_query, sqlite_schema ───
# =============================================================================
# Generic SQLite tools (read-only query + schema introspection)
# =============================================================================

_SQL_WRITE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|REINDEX|VACUUM)\b"
    r"|\bREPLACE\s+INTO\b"
    r"|\bINSERT\s+OR\s+REPLACE\b"
    r"|\bPRAGMA\s+\w+\s*=",
    re.IGNORECASE,
)


@mcp.tool()
def sqlite_query(sql: str, limit: int = 200) -> str:
    """Run a read-only SQL query against the vault database.

    The database contains these table groups:
      • Vault index: notes, files, frontmatter, tags, aliases, wikilinks, tasks,
        reminders, events, note_access, revisions, fts (FTS5)
      • Domain: people, anime_list, vocab, warranties
      • Views: people_upcoming_birthdays, warranties_active, warranties_expired,
        anime_status_counts, anime_score_counts

    Only SELECT queries are allowed. Write operations are blocked.
    Results are returned as pipe-delimited rows (header|row1|row2|...).
    Use ``sqlite_schema`` to discover table columns.
    """
    sql = sql.strip().rstrip(";")
    if not sql:
        return "err: empty query"
    if _SQL_WRITE_RE.search(sql):
        return "err: write operations are not allowed — only SELECT queries"
    if not sql.upper().lstrip().startswith("SELECT") and not sql.upper().lstrip().startswith("WITH"):
        return "err: only SELECT (and WITH … SELECT) queries are allowed"

    limit = max(1, min(limit, 2000))
    idx = get_vault_index()
    try:
        c = idx.conn
        rows = c.execute(sql).fetchmany(limit + 1)
    except Exception as exc:
        return f"err: {exc}"

    if not rows:
        return "none (0 rows)"

    truncated = len(rows) > limit
    rows = rows[:limit]
    keys = rows[0].keys()
    out = ["|".join(keys)]
    for r in rows:
        out.append("|".join(str(v) if v is not None else "" for v in (r[k] for k in keys)))
    if truncated:
        out.append(f"[truncated at {limit} rows — use LIMIT/OFFSET or increase limit param]")
    return "\n".join(out)


@mcp.tool()
def sqlite_schema(table: str = "") -> str:
    """Show database schema. No args → list all tables/views. Pass a table name → show its columns.

    Useful for discovering column names before writing a sqlite_query.
    """
    idx = get_vault_index()
    c = idx.conn

    if not table.strip():
        # List all tables and views
        rows = c.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        if not rows:
            return "no tables"
        out: list[str] = []
        for r in rows:
            col_info = c.execute(f"PRAGMA table_info({r['name']})").fetchall()
            col_names = [ci["name"] for ci in col_info]
            out.append(f"{r['type']}:{r['name']}|{','.join(col_names)}")
        return "\n".join(out)
    else:
        # Show columns for a specific table
        table_name = table.strip()
        cols = c.execute(f"PRAGMA table_info({table_name})").fetchall()
        if not cols:
            return f"err: table '{table_name}' not found"
        out = []
        for col in cols:
            nullable = "" if col["notnull"] else "nullable"
            pk = "PK" if col["pk"] else ""
            default = f"default={col['dflt_value']}" if col["dflt_value"] is not None else ""
            flags = " ".join(f for f in [col["type"], pk, nullable, default] if f)
            out.append(f"{col['name']}|{flags}")
        return "\n".join(out)




# =============================================================================
# Reload the SQLite DB Plugin in Obsidian
# =============================================================================
#
# The Obsidian SQLite DB Plugin reads vault.db into memory once on plugin load
# (via sql.js wasm). After the MCP writes to vault.db, the plugin's in-memory
# copy is stale until the plugin is reloaded.
#
# The companion plugin at .obsidian/plugins/sqlite-db-reload/ registers a
# protocol handler at obsidian://sqlite-db-reload that forces a re-read. This
# tool opens that URI from the OS level, asynchronously waking Obsidian.


@mcp.tool()
def reload_sqlite_db_plugin(notify: bool = True) -> str:
    """Tell Obsidian to reload the SQLite DB Plugin's in-memory database.

    Use after writing to vault.db (people_upsert, anime_upsert, vocab_upsert,
    warranty_upsert) when the user has Obsidian open and is viewing a note
    that renders ```sql blocks — the plugin caches the DB and won't see your
    write until reloaded.

    Requires the companion plugin at
    .obsidian/plugins/sqlite-db-reload/ to be enabled (Settings → Community
    Plugins). Falls back gracefully with an error string if the URI scheme
    can't be triggered.

    notify=True shows a toast in Obsidian; notify=False is silent.
    """
    uri = "obsidian://sqlite-db-reload"
    if notify:
        uri += "?notify=1"

    try:
        if sys.platform == "darwin":
            cmd = ["open", uri]
        elif sys.platform.startswith("linux"):
            cmd = ["xdg-open", uri]
        elif sys.platform == "win32":
            cmd = ["cmd", "/c", "start", "", uri]
        else:
            return f"err: unsupported platform {sys.platform}"

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return f"err: {' '.join(cmd)!r} exit={result.returncode} stderr={result.stderr.strip()}"
        return "ok — reload signal sent (Obsidian must be running with the companion plugin enabled)"
    except subprocess.TimeoutExpired:
        return "err: timeout waiting for OS to dispatch the URI"
    except FileNotFoundError as exc:
        return f"err: command not found — {exc}"
    except Exception as exc:
        return f"err: {exc}"
