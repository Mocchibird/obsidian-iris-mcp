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
# `from M import *` skips underscore-prefixed names, so we import these
# explicitly (used by refresh_sql_views to commit edits back to the index).
from ..core import _notify_index_of_write


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


# ── In-note SQL view rendering ──────────────────────────────────────────────
# The Obsidian SQLite-DB plugin doesn't work on iOS/iPadOS (uses native Node
# modules; Apple blocks them). This MCP tool walks notes, finds ```sqlite
# code blocks, runs them against the vault DB, and injects the rendered
# result as a markdown table right below. Mobile reads the static markdown;
# desktop sees both (or hides our static one via CSS — see plugin settings).

_SQL_BLOCK_RE = re.compile(
    # Capture the fenced code block (sqlite or sql tag), then optionally
    # consume an existing iris-sql-result block right after so we can
    # replace it in place. Whitespace between blocks is preserved by the
    # explicit \n* in the replacement.
    r"```(?:sqlite|sql)\n(.*?)\n```"                                 # 1: query
    r"(?:\s*<!--\s*iris-sql-result.*?<!--\s*/iris-sql-result\s*-->)?",
    re.DOTALL | re.IGNORECASE,
)

_SQL_VIEW_DEFAULT_LIMIT = 50
_SQL_CELL_MAX_CHARS = 200


def _render_sql_to_md_table(sql: str, limit: int = _SQL_VIEW_DEFAULT_LIMIT) -> str:
    """Run ``sql`` against the vault DB and return a markdown table string.

    Always wrapped in ``<!-- iris-sql-result … -->`` brackets so the
    refresher can find + replace it on the next pass. Errors land in the
    same wrapper so a broken query doesn't silently disappear.
    """
    now_iso = datetime.now().isoformat(timespec="seconds")

    # SQL safety — same rules as sqlite_query.
    s = sql.strip().rstrip(";")
    if not s:
        body = "_(empty query)_"
        return _wrap_result(body, now_iso, rows=0)
    if _SQL_WRITE_RE.search(s):
        body = "❌ write operations not allowed — only SELECT / WITH"
        return _wrap_result(body, now_iso, rows=0)
    upper = s.upper().lstrip()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        body = "❌ only SELECT / WITH queries allowed"
        return _wrap_result(body, now_iso, rows=0)

    # Auto-cap unbounded queries.
    if not re.search(r"\blimit\s+\d+\b", s, re.IGNORECASE):
        s = s + f" LIMIT {limit}"

    try:
        rows = get_vault_index().conn.execute(s).fetchmany(limit + 1)
    except Exception as exc:
        body = f"❌ {exc}"
        return _wrap_result(body, now_iso, rows=0)

    if not rows:
        return _wrap_result("_(no rows)_", now_iso, rows=0)

    truncated = len(rows) > limit
    rows = rows[:limit]
    cols = list(rows[0].keys())

    def _cell(v) -> str:
        s = "" if v is None else str(v)
        # Escape pipes + newlines so markdown table doesn't break.
        s = s.replace("|", "\\|").replace("\n", " ").replace("\r", "")
        if len(s) > _SQL_CELL_MAX_CHARS:
            s = s[: _SQL_CELL_MAX_CHARS - 1] + "…"
        return s

    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join(["---"] * len(cols)) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(_cell(r[c]) for c in cols) + " |")
    if truncated:
        lines.append(f"_…+more rows (limit {limit})_")
    body = "\n".join(lines)
    return _wrap_result(body, now_iso, rows=len(rows))


def _wrap_result(body: str, timestamp: str, rows: int) -> str:
    """Wrap a rendered table body in iris-sql-result HTML comments."""
    return (f"<!-- iris-sql-result generated:{timestamp} rows:{rows} -->\n"
            f"{body}\n"
            f"<!-- /iris-sql-result -->")


def _replace_sql_blocks(text: str) -> tuple[str, int, int]:
    """Walk a note's text, render every ```sqlite block.

    Returns (new_text, blocks_processed, errors). Blocks are processed in
    order. An existing iris-sql-result immediately following each query
    block (with only whitespace between) is replaced; otherwise a fresh
    result block is inserted.
    """
    block_count = [0]
    error_count = [0]

    def _replace(m: re.Match) -> str:
        block_count[0] += 1
        query = m.group(1)
        rendered = _render_sql_to_md_table(query)
        if "❌" in rendered:
            error_count[0] += 1
        # Two newlines between code block and result for clean rendering.
        return f"```sqlite\n{query}\n```\n\n{rendered}"

    new_text = _SQL_BLOCK_RE.sub(_replace, text)
    return new_text, block_count[0], error_count[0]


@mcp.tool()
def refresh_sql_views(path: str = "", all_notes: bool = False) -> str:
    """Re-run every ```sqlite code block in a note (or the whole vault) and
    inject the rendered markdown table beneath each.

    Why: the Obsidian SQLite-DB plugin uses native Node modules which don't
    work on mobile (iOS / iPadOS). Pre-rendering results as plain markdown
    tables makes the same queries readable on every device, with desktop
    still getting live plugin rendering on top.

    Format of injected blocks:
      ```sqlite
      SELECT title FROM notes WHERE …
      ```
      <!-- iris-sql-result generated:2026-05-18T08:30 rows:5 -->
      | title |
      | --- |
      | Foo |
      …
      <!-- /iris-sql-result -->

    Re-running is idempotent — the wrapper comments let the refresher find
    and replace its own previous output without duplicating it.

    Args:
        path: Vault-relative path to a single note. If given, only that
            note is refreshed.
        all_notes: If True, walk the entire vault and refresh every note
            containing a ```sqlite or ```sql code block. Either ``path``
            or ``all_notes=True`` must be set.

    Limits: per-block query auto-`LIMIT 50` if no LIMIT specified. Cell
    contents truncated to 200 chars. Pipes and newlines escaped so the
    markdown table doesn't break.

    Returns a per-note summary like ``ok 12 notes / 18 blocks / 0 errors``.
    """
    if not path and not all_notes:
        return "err: pass `path` (single note) or `all_notes=True` (whole vault)"

    vault_root = get_vault_root()
    targets: list[Path] = []
    if path:
        p = safe_path(path)
        if not p.exists() or not p.is_file():
            return f"err: note not found: {path}"
        targets.append(p)
    else:
        for md in vault_root.rglob("*.md"):
            # Cheap filter — skip files with no sqlite codefence.
            try:
                head = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "```sqlite" in head or "```sql" in head:
                targets.append(md)

    if not targets:
        return ("ok 0 notes scanned (no ```sqlite / ```sql blocks found)"
                if all_notes else f"ok no ```sqlite blocks in {path}")

    notes_changed = 0
    blocks_total = 0
    errors_total = 0
    sample_errors: list[str] = []

    for note in targets:
        try:
            old_text = note.read_text(encoding="utf-8")
        except OSError as exc:
            sample_errors.append(f"{note.name}: read failed: {exc}")
            errors_total += 1
            continue
        new_text, n_blocks, n_errors = _replace_sql_blocks(old_text)
        blocks_total += n_blocks
        errors_total += n_errors
        if new_text != old_text:
            try:
                note.write_text(new_text, encoding="utf-8")
                _notify_index_of_write(note, text=new_text)
                notes_changed += 1
            except OSError as exc:
                sample_errors.append(f"{note.name}: write failed: {exc}")
                errors_total += 1

    summary = (f"ok {notes_changed}/{len(targets)} note(s) updated · "
               f"{blocks_total} SQL block(s) processed · {errors_total} error(s)")
    if sample_errors:
        summary += "\nerrors:\n  - " + "\n  - ".join(sample_errors[:5])
        if len(sample_errors) > 5:
            summary += f"\n  - …+{len(sample_errors) - 5} more"
    return summary
