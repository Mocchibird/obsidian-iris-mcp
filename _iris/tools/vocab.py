"""Vocabulary

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


# ─── from original L10153-10273: Vocabulary ───
# =============================================================================
# Vocabulary (Japanese / Korean / future languages)
# =============================================================================

_VOCAB_VALID_LANGS = {"ja", "ko", "en", "de", "fr", "es", "it", "pt", "zh"}


@mcp.tool()
def vocab_upsert(
    language: str,
    word: str,
    reading: str = "",
    meaning: str = "",
    category: str = "",
    source: str = "",
    note: str = "",
    reload_db: bool = True,
) -> str:
    """Insert or update a vocabulary entry. language: ja|ko|en|de|... (ISO-639-1). word: the canonical written form (kanji, hangul, etc). reading: phonetic (hiragana, romanization). meaning: gloss in any language(s). category: thematic grouping (e.g. 'Numbers', 'Verbs'). reload_db=True (default) signals Obsidian's SQLite DB Plugin to refresh — pass False for bulk writes."""
    if language not in _VOCAB_VALID_LANGS:
        return f"err: language must be one of {sorted(_VOCAB_VALID_LANGS)}"
    if not word.strip():
        return "err: word required"
    idx = get_vault_index()
    c = idx.conn
    now = datetime.now().isoformat(timespec="seconds")
    existing = c.execute("SELECT id FROM vocab WHERE language = ? AND word = ?", (language, word)).fetchone()
    if existing:
        c.execute(
            "UPDATE vocab SET reading = ?, meaning = ?, category = ?, source = ?, note = ?, updated_at = ? "
            "WHERE id = ?",
            (reading or "", meaning or "", category or "", source or "", note or "", now, existing["id"]),
        )
        c.commit()
        if reload_db: maybe_reload_db_plugin()
        return f"ok updated id:{existing['id']}|{language}:{word}"
    cur = c.execute(
        "INSERT INTO vocab (language, word, reading, meaning, category, source, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (language, word, reading, meaning, category, source, note, now, now),
    )
    c.commit()
    if reload_db: maybe_reload_db_plugin()
    return f"ok inserted id:{cur.lastrowid}|{language}:{word}"


@mcp.tool()
def vocab_remove(language: str, word: str, reload_db: bool = True) -> str:
    """Delete a vocabulary entry by language+word."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM vocab WHERE language = ? AND word = ?", (language, word))
    c.commit()
    if reload_db: maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} {language}:{word}"


# @mcp.tool()  # removed — use sqlite_query instead
def vocab_search(query: str, language: str = "", limit: int = 20) -> str:
    """Search vocabulary by word/reading/meaning (substring, case-insensitive). language='' for all. Returns id|language|word|reading|meaning|category per line."""
    idx = get_vault_index()
    c = idx.conn
    pat = f"%{query.strip()}%"
    sql = (
        "SELECT id, language, word, reading, meaning, category FROM vocab "
        "WHERE (word LIKE ? OR reading LIKE ? OR meaning LIKE ? COLLATE NOCASE)"
    )
    params: list = [pat, pat, pat]
    if language:
        if language not in _VOCAB_VALID_LANGS:
            return f"err: language must be one of {sorted(_VOCAB_VALID_LANGS)}"
        sql += " AND language = ?"
        params.append(language)
    sql += " ORDER BY language, word LIMIT ?"
    params.append(max(1, min(limit, 200)))
    rows = c.execute(sql, params).fetchall()
    if not rows:
        return "none"
    return "\n".join(
        f"{r['id']}|{r['language']}|{r['word']}|{r['reading']}|{r['meaning']}|{r['category']}"
        for r in rows
    )


# @mcp.tool()  # removed — use sqlite_query instead
def vocab_random(language: str = "", category: str = "", count: int = 10) -> str:
    """Return random vocabulary entries — useful for quick review. language and category filter optionally. count up to 50."""
    if language and language not in _VOCAB_VALID_LANGS:
        return f"err: language must be one of {sorted(_VOCAB_VALID_LANGS)}"
    idx = get_vault_index()
    c = idx.conn
    sql = "SELECT id, language, word, reading, meaning, category FROM vocab"
    conds: list[str] = []
    params: list = []
    if language:
        conds.append("language = ?")
        params.append(language)
    if category:
        conds.append("category = ?")
        params.append(category)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY RANDOM() LIMIT ?"
    params.append(max(1, min(count, 50)))
    rows = c.execute(sql, params).fetchall()
    if not rows:
        return "none"
    return "\n".join(
        f"{r['language']}:{r['word']}  [{r['reading']}]  → {r['meaning']}  ({r['category']})"
        for r in rows
    )


# @mcp.tool()  # removed — use sqlite_query instead
def vocab_stats() -> str:
    """Counts of vocabulary entries by language + category."""
    idx = get_vault_index()
    c = idx.conn
    total = c.execute("SELECT COUNT(*) AS n FROM vocab").fetchone()["n"]
    out = [f"total|{total}"]
    for r in c.execute("SELECT language, COUNT(*) AS n FROM vocab GROUP BY language ORDER BY n DESC").fetchall():
        out.append(f"lang:{r['language']}|{r['n']}")
    return "\n".join(out)


