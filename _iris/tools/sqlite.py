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
    # String-literal-aware safety check so `WHERE title = 'insert into junk'`
    # isn't falsely blocked. `_strip_sql_strings_and_comments` lives further
    # down in this module — see _render_sql_to_md_table for the same pattern.
    sql_clean = _strip_sql_strings_and_comments(sql)
    if _SQL_WRITE_RE.search(sql_clean):
        return "err: write operations are not allowed — only SELECT queries"
    upper_clean = sql_clean.lstrip().upper()
    if not (upper_clean.startswith("SELECT") or upper_clean.startswith("WITH")):
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
    # Fenced code block with `sqlite` or `sql` language tag. Inner body
    # excludes literal backticks so we can't accidentally swallow the next
    # fence's content. The lang tag is captured so we preserve whatever
    # the author wrote (don't rename ```sql → ```sqlite silently).
    r"```(?P<lang>sqlite|sql)\r?\n"
    r"(?P<query>(?:[^`]|`(?!``))+?)\r?\n"
    r"```"
    # Optional existing result wrapper. MUST immediately follow the closing
    # fence (only whitespace + a single blank line between). Body uses
    # [^`] to refuse to cross into the next fence — defensive against a
    # half-broken wrapper consuming the next query block.
    r"(?:[ \t]*\r?\n[ \t]*\r?\n"
    r"<!--\s*iris-sql-result[^\n]*-->\r?\n"
    r"(?P<oldresult>(?:[^`]|`(?!``))*?)"
    r"<!--\s*/iris-sql-result\s*-->)?",
    re.IGNORECASE,  # NOTE: not DOTALL — relies on explicit \r?\n
)

_SQL_VIEW_DEFAULT_LIMIT = 50
_SQL_CELL_MAX_CHARS = 200


def _strip_sql_strings_and_comments(sql: str) -> str:
    """Best-effort removal of SQL string literals and comments.

    Used so a keyword check (e.g. ``\\bLIMIT\\s+\\d+\\b``) doesn't get
    false-matched against text inside a quoted value or a comment. Not a
    full parser — handles single-quoted strings (with doubled-quote
    escapes), ``--`` line comments, and ``/* ... */`` block comments.
    Good enough for "is there a real LIMIT keyword" checks.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # Line comment
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
            continue
        # Block comment
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        # Single-quoted string (with '' escape)
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2  # escaped quote
                        continue
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _is_sqlitedb_config(text: str) -> bool:
    """Detect the SQLite-DB plugin's YAML-style config syntax.

    The plugin parses blocks like::

        table: people
        columns: name, category
        filterColumn: category
        filterValue: vendors
        orderBy: name

    instead of raw SQL. We detect by the presence of a leading ``table:``
    declaration on its own line (with optional leading whitespace).
    """
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("--") or s.startswith("#"):
            continue
        # First non-comment line: is it `table:` ?
        return bool(re.match(r"^table\s*:\s*\S", s, re.IGNORECASE))
    return False


def _sqlitedb_config_to_sql(config: str) -> str:
    """Translate the SQLite-DB plugin's YAML-ish config into a SELECT.

    Recognises ``table``, ``columns``, ``filterColumn`` / ``filterValue``,
    ``orderBy`` (supports multi-column ``a, b DESC, c`` — unlike the
    plugin's own implementation), ``limit``. Ignores ``displayFormat``
    and any unknown keys silently. Returns the SQL string; the caller
    runs it through the normal safety + execution pipeline.

    Multi-line values are not supported (single-line key: value only).
    Identifier quoting: wraps each in double-quotes so dashes /
    SQL-reserved column names round-trip safely.
    """
    parsed: dict[str, str] = {}
    multi_filters: list[tuple[str, str]] = []
    for line in config.splitlines():
        s = line.strip()
        if not s or s.startswith("--") or s.startswith("#"):
            continue
        if ":" not in s:
            continue
        key, _, value = s.partition(":")
        key = key.strip().lower()
        value = value.strip()
        # Strip trailing inline comments.
        for marker in ("--", "#"):
            idx = value.find(marker)
            if idx > 0 and not value[:idx].count("'") % 2:
                # outside a quoted string
                value = value[:idx].rstrip()
        if key == "filtercolumn" or key == "filterkey":
            multi_filters.append((value, ""))
        elif key == "filtervalue":
            if multi_filters and not multi_filters[-1][1]:
                multi_filters[-1] = (multi_filters[-1][0], value)
            else:
                multi_filters.append(("__pending__", value))
        else:
            parsed[key] = value

    table = parsed.get("table") or ""
    if not table:
        raise ValueError("config has no `table:` line")

    cols_raw = parsed.get("columns", "*")
    if cols_raw == "*":
        select_clause = "*"
    else:
        cols = [c.strip() for c in cols_raw.split(",") if c.strip()]
        select_clause = ", ".join(f'"{c}"' for c in cols)

    where_parts: list[str] = []
    for col, val in multi_filters:
        if not col or col == "__pending__" or not val:
            continue
        # Wrap value in single-quotes; escape inner ' by doubling.
        val_esc = val.replace("'", "''")
        where_parts.append(f'"{col}" = \'{val_esc}\'')
    where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    order_raw = parsed.get("orderby", "")
    if order_raw:
        # Multi-column: "a, b DESC, c" → "a", "b" DESC, "c"
        pieces: list[str] = []
        for raw in order_raw.split(","):
            raw = raw.strip()
            if not raw:
                continue
            # Detect trailing ASC/DESC.
            m = re.match(r'^(.+?)\s+(ASC|DESC)\s*$', raw, re.IGNORECASE)
            if m:
                pieces.append(f'"{m.group(1).strip()}" {m.group(2).upper()}')
            else:
                pieces.append(f'"{raw}"')
        order_clause = " ORDER BY " + ", ".join(pieces) if pieces else ""
    else:
        order_clause = ""

    limit_raw = parsed.get("limit", "").strip()
    limit_clause = f" LIMIT {int(limit_raw)}" if limit_raw.isdigit() else ""

    return (f'SELECT {select_clause} FROM "{table}"'
            f"{where_clause}{order_clause}{limit_clause}")


def _render_sql_to_md_table(sql: str, limit: int = _SQL_VIEW_DEFAULT_LIMIT) -> str:
    """Run ``sql`` against the vault DB and return a markdown table string.

    Always wrapped in ``<!-- iris-sql-result … -->`` brackets so the
    refresher can find + replace it on the next pass. Errors land in the
    same wrapper so a broken query doesn't silently disappear.
    """
    now_iso = datetime.now().isoformat(timespec="seconds")

    # SQL safety — same write-keyword rules as sqlite_query, but more
    # lenient about HOW the read is expressed. Allow SELECT / WITH /
    # EXPLAIN [QUERY PLAN] / read-only PRAGMA (table_info, index_list,
    # etc. but NOT the assignment form `PRAGMA foo=bar` which is already
    # blocked by _SQL_WRITE_RE). Leading comments and whitespace are
    # stripped before keyword check so e.g. `-- doc comment\nSELECT ...`
    # works.
    s = sql.strip().rstrip(";")
    if not s:
        body = "_(empty query)_"
        return _wrap_result(body, now_iso, rows=0)
    # Translate the SQLite-DB plugin's declarative YAML-ish config into
    # real SQL. Notes typically use this format inside ```sql blocks
    # (the plugin parses it; raw SQL is the exception). We handle BOTH
    # so the same note renders correctly on desktop (via plugin) AND on
    # mobile (via Iris's pre-rendered markdown).
    if _is_sqlitedb_config(s):
        try:
            s = _sqlitedb_config_to_sql(s)
        except Exception as exc:
            body = f"❌ config parse failed: {exc}"
            return _wrap_result(body, now_iso, rows=0)
    # Strip strings + comments ONCE; both safety checks use the result.
    # Without this, `WHERE title = 'insert into junk'` falsely triggers
    # the write-keyword regex.
    s_clean = _strip_sql_strings_and_comments(s)
    if _SQL_WRITE_RE.search(s_clean):
        body = "❌ write operations not allowed — only SELECT / WITH"
        return _wrap_result(body, now_iso, rows=0)
    upper = s_clean.strip().upper()
    _READ_STARTS = ("SELECT", "WITH", "EXPLAIN", "PRAGMA")
    if not any(upper.startswith(kw) for kw in _READ_STARTS):
        body = (f"❌ only read queries allowed (SELECT / WITH / EXPLAIN / "
                f"PRAGMA). Got: {upper[:40]!r}")
        return _wrap_result(body, now_iso, rows=0)

    # Auto-cap unbounded queries. We also enforce an effective MAX even if
    # the user wrote `LIMIT 999999` — the per-block refresher runs every
    # 15 min across the whole vault, so an unbounded query in any one note
    # would OOM the bot. fetchmany() honours whichever is smaller of the
    # SQL-side LIMIT and the python-side fetch size.
    _EFFECTIVE_MAX = 500
    fetch_n = min(limit, _EFFECTIVE_MAX) + 1
    # Strip SQL string literals (and line/block comments) before testing
    # for a LIMIT keyword — `WHERE title = 'rate limit 10'` should NOT be
    # treated as having a LIMIT clause.
    s_stripped = _strip_sql_strings_and_comments(s)
    if not re.search(r"\blimit\s+\d+\b", s_stripped, re.IGNORECASE):
        s = s + f" LIMIT {fetch_n - 1}"

    try:
        rows = get_vault_index().conn.execute(s).fetchmany(fetch_n)
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


def _data_portion(rendered_block: str) -> str:
    """Strip the iris-sql-result wrapper + the changing `generated:` line,
    returning just the table body. Used to compare what's already in a note
    against what we'd write — so we only touch disk when the actual data
    rows differ, not when only the timestamp changed.
    """
    # Drop both opening and closing wrapper comments. The opening line has
    # the variable `generated:...` timestamp, which we MUST NOT compare on.
    lines = rendered_block.splitlines()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("<!--") and ("iris-sql-result" in s or "/iris-sql-result" in s):
            continue
        out.append(line.rstrip())
    # Collapse trailing blank lines so trailing-newline noise doesn't
    # cause a false-positive diff.
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def _replace_sql_blocks(text: str, newline: str = "\n") -> tuple[str, int, int]:
    """Walk a note's text, render every ```sqlite block.

    Returns (new_text, blocks_processed, errors). Blocks are processed in
    order. An existing iris-sql-result immediately following each query
    block (with only whitespace between) is replaced ONLY if the actual
    table data differs — the volatile `generated:` timestamp is ignored
    when comparing. This means static queries (whose data doesn't change)
    produce zero disk writes across refreshes, even though they still
    re-execute. Only queries whose displayed data changed actually trigger
    a write + syncthing replication.

    Newline style (``\\n`` vs ``\\r\\n``) is preserved per-note via the
    ``newline`` argument so we never silently flip line endings on
    Windows-edited notes.
    """
    block_count = [0]
    error_count = [0]

    def _replace(m: re.Match) -> str:
        block_count[0] += 1
        query = m.group("query")
        lang = m.group("lang")  # preserve `sql` vs `sqlite` — don't rename
        rendered = _render_sql_to_md_table(query)
        if "❌" in rendered:
            error_count[0] += 1

        # If the existing wrapper's data is identical to what we'd write,
        # keep the existing block verbatim (preserves its old timestamp).
        # This is the data-aware diff: lets us run queries every 15 min
        # without writing notes — which would otherwise burn syncthing
        # traffic + churn mtimes for zero data benefit on static queries.
        old_match_text = m.group(0)
        if "iris-sql-result" in old_match_text:
            existing_data = _data_portion(old_match_text)
            new_data = _data_portion(rendered)
            if existing_data == new_data and existing_data:
                return old_match_text  # unchanged — no write needed

        # Build with the note's native newline style so writing back is
        # a no-op on identical content (and doesn't trigger spurious
        # mtime updates or re-sync churn).
        parts = [
            f"```{lang}",
            query,
            "```",
            "",
            rendered,
        ]
        return newline.join(parts)

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
            # Read raw bytes first so we can detect newline style WITHOUT
            # Python normalising CRLF→LF on us (which would happen with the
            # default universal-newlines mode).
            raw_bytes = note.read_bytes()
        except OSError as exc:
            sample_errors.append(f"{note.name}: read failed: {exc}")
            errors_total += 1
            continue
        try:
            old_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            sample_errors.append(f"{note.name}: not valid UTF-8: {exc}")
            errors_total += 1
            continue
        newline = "\r\n" if b"\r\n" in raw_bytes else "\n"
        new_text, n_blocks, n_errors = _replace_sql_blocks(old_text, newline=newline)
        blocks_total += n_blocks
        errors_total += n_errors
        if new_text != old_text:
            try:
                # Write bytes directly so the newline style we built is the
                # newline style on disk — Python won't second-guess us.
                note.write_bytes(new_text.encode("utf-8"))
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


@mcp.tool()
def vault_snapshot() -> str:
    """Atomically snapshot the live vault.db → vault-snapshot.db.

    On-demand companion to the bot's periodic snapshot loop. Use when:
      * You just made a bunch of vault changes and want them visible on
        Mac / Windows / mobile (via syncthing → SQLite-DB plugin pointed
        at the snapshot) without waiting for the next 10-min tick.
      * You're about to demo / present and want the readers' DB fresh.

    The snapshot is built via `VACUUM INTO` (committed-state only, single
    file, DELETE journal mode regardless of source mode), written to a
    unique tmp filename, then atomically renamed over the existing
    snapshot. Old snapshot stays readable for any open plugin until the
    rename completes, so readers never see a half-written DB.

    Returns the snapshot path + size on success, or an error string.
    """
    import os as _os
    import uuid as _uuid
    from pathlib import Path as _Path

    db_path = _Path(get_vault_root()) / ".ai_memory_cache" / "vault.db"
    if not db_path.exists():
        return ("err: live vault.db not found yet — "
                "VaultIndex hasn't built the index. Run "
                "`rebuild_vault_index()` first.")
    snap_path = db_path.with_name("vault-snapshot.db")

    # Best-effort cleanup of any stale .tmp from a previous crashed run.
    for stale in db_path.parent.glob("vault-snapshot.*.tmp"):
        try:
            stale.unlink()
        except OSError:
            pass

    # Unique tmp name per attempt — VACUUM INTO refuses to overwrite, so
    # a leftover .tmp would otherwise starve the call.
    tmp_path = db_path.with_name(
        f"vault-snapshot.{_os.getpid()}.{_uuid.uuid4().hex[:8]}.tmp"
    )
    # SQLite's VACUUM INTO can't bind parameters; escape single-quotes
    # the standard way. VAULT_ROOT is operator-controlled so this is
    # hardening rather than untrusted-input defense.
    escaped_target = str(tmp_path).replace("'", "''")
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        try:
            conn.execute(f"VACUUM INTO '{escaped_target}'")
        finally:
            conn.close()
        _os.replace(str(tmp_path), str(snap_path))
    except Exception as exc:
        # Clean up tmp on failure so we don't leak.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return f"err: snapshot failed: {exc}"

    size_kb = snap_path.stat().st_size // 1024
    return f"ok vault-snapshot.db updated ({size_kb} KB)"
