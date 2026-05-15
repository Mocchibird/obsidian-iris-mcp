"""search_vault_text grep; Unified vault search; Related-note suggestions

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
from .files import _select_text_files


# ─── from original L5233-5324: search_vault_text grep ───
# =============================================================================
# Grep with context — search_vault_text
# =============================================================================


@mcp.tool()
def search_vault_text(
    pattern: str,
    mode: str = "text",
    folder: str = "",
    extensions: list[str] = [],
    case_sensitive: bool = False,
    context_lines: int = 3,
    limit: int = 50,
    max_matches_per_file: int = 5,
) -> str:
    """Grep-like search with context lines. mode: text|regex."""
    if not pattern.strip():
        return "pattern must not be empty."

    mode = (mode or "text").strip().lower()
    if mode not in {"text", "regex"}:
        return f"Unknown mode {mode!r}. Choose 'text' or 'regex'."

    context_lines = max(0, min(context_lines, 10))
    limit = max(1, min(limit, 200))
    max_matches_per_file = max(1, min(max_matches_per_file, 20))

    if mode == "regex":
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Invalid regex: {e}"
    else:
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(re.escape(pattern), flags)

    targets, err = _select_text_files(folder=folder, extensions=extensions or [".md"])
    if err:
        return err

    total_matches = 0
    total_files = 0
    output_lines: list[str] = []

    for path in targets:
        if total_files >= limit:
            break

        text = read_text(path)
        lines_list = text.splitlines()
        file_matches: list[int] = []

        for i, line in enumerate(lines_list):
            if regex.search(line):
                file_matches.append(i)

        if not file_matches:
            continue

        total_files += 1
        total_matches += len(file_matches)
        rel = relative_to_vault(path)
        output_lines.append(f"--- {rel} ({len(file_matches)} match{'es' if len(file_matches) != 1 else ''}) ---")

        shown = 0
        for match_line_idx in file_matches:
            if shown >= max_matches_per_file:
                if len(file_matches) > max_matches_per_file:
                    output_lines.append(f"  ... +{len(file_matches) - max_matches_per_file} more matches in this file")
                break

            start_ctx = max(0, match_line_idx - context_lines)
            end_ctx = min(len(lines_list), match_line_idx + context_lines + 1)

            if shown > 0:
                output_lines.append("  ...")

            for idx in range(start_ctx, end_ctx):
                marker = ">>>" if idx == match_line_idx else "   "
                output_lines.append(f"  {marker} {idx + 1:>4}: {lines_list[idx]}")
            shown += 1

        output_lines.append("")

    if not total_files:
        return "none"
    header = [f"files:{total_files} matches:{total_matches}"]
    return "\n".join(header + output_lines).rstrip()



# ─── from original L7580-7650: Unified vault search ───
# =============================================================================
# Unified vault search
# =============================================================================


@mcp.tool()
def search_vault(query: str, limit: int = 10) -> str:
    """Unified search: full-text, aliases, titles, tags."""
    if not query.strip():
        return "Query must not be empty."
    limit = max(1, min(limit, 50))
    idx = get_vault_index()
    seen_paths: set[str] = set()
    results: list[dict[str, str]] = []

    # 1. Alias match (exact)
    alias_paths = idx.query_aliases(query.strip())
    for path in alias_paths:
        if path not in seen_paths:
            seen_paths.add(path)
            results.append({"path": path, "match": "alias", "snippet": ""})

    # 2. Title match (case-insensitive substring)
    c = idx.conn
    title_rows = c.execute(
        "SELECT path, title FROM notes WHERE title LIKE ? COLLATE NOCASE ORDER BY path LIMIT ?",
        (f"%{query.strip()}%", limit),
    ).fetchall()
    for row in title_rows:
        if row["path"] not in seen_paths:
            seen_paths.add(row["path"])
            results.append({"path": row["path"], "match": "title", "snippet": row["title"]})

    # 3. Tag match (exact)
    tag_query = query.strip().lower().replace(" ", "-")
    tag_paths = idx.query_tags(tag_query, limit=limit)
    for path in tag_paths:
        if path not in seen_paths:
            seen_paths.add(path)
            results.append({"path": path, "match": f"tag:{tag_query}", "snippet": ""})

    # 3b. Tag co-occurrence expansion: if the query matched a tag, also search
    # for notes with frequently co-occurring tags to broaden recall.
    # E.g. searching "benchmarking" also surfaces notes tagged "performance".
    if tag_paths and len(results) < limit:
        co_tags = idx.find_cooccurring_tags(tag_query, limit=5)
        for co_tag, _cnt in co_tags:
            if len(results) >= limit:
                break
            co_paths = idx.query_tags(co_tag, limit=limit)
            for path in co_paths:
                if path not in seen_paths:
                    seen_paths.add(path)
                    results.append({"path": path, "match": f"co-tag:{co_tag}←{tag_query}", "snippet": ""})
                    if len(results) >= limit:
                        break

    # 4. Full-text search
    fts_results = idx.search_fts(query.strip(), limit=limit)
    for r in fts_results:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            results.append({"path": r["path"], "match": "content", "snippet": r.get("snippet", "")})

    if not results:
        return "none"
    return "\n".join(
        f"{r['path']}|{r['match']}|{r['snippet'][:120]}" for r in results[:limit]
    )



# ─── from original L7905-7968: Related-note suggestions ───
# =============================================================================
# Related-note suggestions
# =============================================================================


@mcp.tool()
def suggest_related_notes(path: str, limit: int = 8) -> str:
    """
    Suggest related notes for a given note using FTS similarity.

    Uses the note's title and key terms from its body to find similar
    notes. Returns ranked suggestions the assistant can offer as links.
    """
    limit = max(1, min(limit, 30))
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"

    text = read_text(note)
    data, body = split_frontmatter(text)
    title = title_from_text(text, note.stem)

    # Build search query from title + first few significant words of body
    body_words = [
        w for w in re.findall(r"[A-Za-z]{4,}", body[:2000])
        if w.lower() not in {
            "this", "that", "with", "from", "have", "will",
            "been", "were", "they", "their", "about", "which",
            "when", "what", "there", "each", "other", "into",
            "also", "than", "more", "some", "only", "such",
            "note", "notes", "related", "section",
        }
    ]
    # Take top words by frequency
    word_counts: dict[str, int] = {}
    for w in body_words:
        wl = w.lower()
        word_counts[wl] = word_counts.get(wl, 0) + 1
    top_words = sorted(word_counts, key=lambda w: word_counts[w], reverse=True)[:8]
    query_parts = title.split() + top_words
    query = " ".join(query_parts[:12])

    if not query.strip():
        return "Cannot generate suggestions — note has no searchable content."

    idx = get_vault_index()
    fts_results = idx.search_fts(query, limit=limit + 5)

    # Filter out the source note itself
    rel = relative_to_vault(note)
    source_norm = normalize_note_target(rel)
    suggestions: list[str] = []
    for r in fts_results:
        if normalize_note_target(r["path"]) == source_norm:
            continue
        suggestions.append(f"{r['path']}|{r.get('title', '')}|{r.get('snippet', '')[:80]}")
        if len(suggestions) >= limit:
            break

    if not suggestions:
        return "No related notes found."
    return "\n".join(suggestions)


