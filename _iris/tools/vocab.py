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


# ─── Spaced repetition (SM-2) ───────────────────────────────────────────────
# Vocabulary review scheduling using the SM-2 algorithm.
#
#   grade scale (per review):
#     5 = perfect recall                  → interval grows
#     4 = correct, minor hesitation
#     3 = correct with effort             → minimum passing grade
#     2 = wrong but answer felt familiar  → reset
#     1 = wrong, but remembered seeing it
#     0 = total blank                     → reset
#
#   New entries default to due_at='' which we treat as "due now" so they
#   appear in the first session.

def _sm2_update(grade: int, interval_days: int, ease_factor: float,
                reps: int, lapses: int) -> tuple[int, float, int, int]:
    """Return updated (interval_days, ease_factor, reps, lapses) for SM-2."""
    grade = max(0, min(int(grade), 5))
    if grade < 3:
        # Failure — reset interval, increment lapses, keep ease reduced
        new_interval = 1
        new_reps = 0
        new_lapses = lapses + 1
    else:
        if reps == 0:
            new_interval = 1
        elif reps == 1:
            new_interval = 6
        else:
            new_interval = max(1, round(interval_days * ease_factor))
        new_reps = reps + 1
        new_lapses = lapses
    # Ease factor adjustment (clamped at 1.3 minimum per SM-2)
    new_ease = ease_factor + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    new_ease = max(1.3, new_ease)
    return new_interval, new_ease, new_reps, new_lapses


@mcp.tool()
def vocab_due(language: str = "", limit: int = 20) -> str:
    """List vocabulary cards due for review today (or earlier).

    A card is "due" if its ``due_at`` is on or before today's date, OR if it
    has never been reviewed (``due_at`` empty). Newest words come first within
    a session so you don't forget what you just learned.

    Pair with ``vocab_review(language, word, grade)`` to grade each card.

    Args:
        language: Filter to a single language (e.g. "ko", "ja"). Empty = all.
        limit: Maximum cards to return.
    """
    idx = get_vault_index()
    c = idx.conn
    today_iso = datetime.now().date().isoformat()
    params: list = [today_iso, today_iso]
    where_lang = ""
    if language.strip():
        where_lang = " AND language = ?"
        params.append(language.strip())
    params.append(int(limit))
    rows = c.execute(
        f"SELECT id, language, word, reading, meaning, category, "
        f"       interval_days, ease_factor, reps, due_at, last_reviewed "
        f"FROM vocab "
        f"WHERE (due_at = '' OR due_at <= ?) "
        f"      AND (last_reviewed = '' OR last_reviewed <= ?)"
        f"      {where_lang} "
        f"ORDER BY (due_at = '') DESC, due_at ASC, updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    if not rows:
        return f"No vocab cards due (language={language or 'any'}). 🎉"
    out = [f"{len(rows)} card(s) due:"]
    for r in rows:
        reading = f" [{r['reading']}]" if r['reading'] else ""
        meaning = f" — {r['meaning']}" if r['meaning'] else ""
        cat = f" ({r['category']})" if r['category'] else ""
        new = "  ← new" if r["reps"] == 0 else f"  (reps:{r['reps']}, ef:{r['ease_factor']:.2f})"
        out.append(f"{r['language']}: {r['word']}{reading}{meaning}{cat}{new}")
    return "\n".join(out)


@mcp.tool()
def vocab_review(language: str, word: str, grade: int) -> str:
    """Record a review of a vocabulary card and schedule the next one.

    Updates the card's SM-2 state and computes ``due_at = today + interval``.

    Args:
        language: ISO-639-1 code (e.g. "ko").
        word: The card's canonical written form (kanji / hangul / etc.).
        grade: 0–5 self-assessment of recall.
            5 perfect · 4 correct with hesitation · 3 correct with effort
            2 wrong but familiar · 1 saw it before · 0 total blank.
            ≥3 advances the card; <3 resets the interval.
    """
    if not isinstance(grade, int):
        try:
            grade = int(grade)
        except (TypeError, ValueError):
            return "err: grade must be an integer 0–5"
    if grade < 0 or grade > 5:
        return "err: grade must be 0–5"
    idx = get_vault_index()
    c = idx.conn
    row = c.execute(
        "SELECT id, interval_days, ease_factor, reps, lapses FROM vocab "
        "WHERE language = ? AND word = ?",
        (language, word),
    ).fetchone()
    if not row:
        return f"err: vocab not found: {language}:{word}"
    new_interval, new_ease, new_reps, new_lapses = _sm2_update(
        grade, row["interval_days"], row["ease_factor"],
        row["reps"], row["lapses"],
    )
    today = datetime.now().date()
    due = (today + timedelta(days=new_interval)).isoformat()
    now_iso = datetime.now().isoformat(timespec="seconds")
    c.execute(
        "UPDATE vocab SET interval_days = ?, ease_factor = ?, reps = ?, "
        "  lapses = ?, due_at = ?, last_reviewed = ?, updated_at = ? "
        "WHERE id = ?",
        (new_interval, new_ease, new_reps, new_lapses, due, now_iso, now_iso,
         row["id"]),
    )
    c.commit()
    verdict = "passed" if grade >= 3 else "failed (reset)"
    return (
        f"ok {language}:{word} — {verdict}. Next due {due} "
        f"(interval {new_interval}d, ef {new_ease:.2f}, reps {new_reps})."
    )


@mcp.tool()
def vocab_review_stats(language: str = "") -> str:
    """Summary of the spaced-repetition queue: due today, learning, mature, etc.

    Args:
        language: Filter to one language. Empty = all.
    """
    idx = get_vault_index()
    c = idx.conn
    today_iso = datetime.now().date().isoformat()
    params: list = [today_iso]
    where_lang = ""
    if language.strip():
        where_lang = " AND language = ?"
        params.append(language.strip())
    base = f"FROM vocab WHERE 1=1{where_lang}"
    total = c.execute(f"SELECT COUNT(*) AS n {base}", params[1:]).fetchone()["n"]
    new_cnt = c.execute(
        f"SELECT COUNT(*) AS n {base} AND reps = 0", params[1:]
    ).fetchone()["n"]
    learning = c.execute(
        f"SELECT COUNT(*) AS n {base} AND reps > 0 AND interval_days < 21",
        params[1:],
    ).fetchone()["n"]
    mature = c.execute(
        f"SELECT COUNT(*) AS n {base} AND interval_days >= 21",
        params[1:],
    ).fetchone()["n"]
    due_now = c.execute(
        f"SELECT COUNT(*) AS n {base} AND (due_at = '' OR due_at <= ?)",
        params[1:] + [today_iso],
    ).fetchone()["n"]
    lapsed = c.execute(
        f"SELECT COUNT(*) AS n {base} AND lapses > 0", params[1:]
    ).fetchone()["n"]
    label = f" ({language})" if language else ""
    return (
        f"=== Vocab SR stats{label} ===\n"
        f"total:    {total}\n"
        f"due now:  {due_now}\n"
        f"new:      {new_cnt}\n"
        f"learning: {learning}  (reps > 0, interval < 21d)\n"
        f"mature:   {mature}    (interval ≥ 21d)\n"
        f"lapsed:   {lapsed}    (failed at least once)\n"
    )


