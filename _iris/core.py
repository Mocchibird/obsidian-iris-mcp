"""Iris core — all shared helpers + the VaultIndex class.

This module has NO @mcp.tool() registrations — it's pure utility code that the
tool modules import. Splitting helpers and VaultIndex into separate files was
considered but they're tightly coupled (the helpers shape what the VaultIndex
consumes), so one core module is the pragmatic choice.
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


# =============================================================================
# Auto-reload helper for Obsidian's SQLite DB Plugin
# =============================================================================
#
# The Obsidian SQLite DB Plugin caches vault.db in memory on plugin load. After
# the MCP writes (people_upsert, anime_upsert, etc.) the plugin's view is stale.
# The companion plugin at .obsidian/plugins/sqlite-db-reload/ registers a
# protocol handler that forces a re-read. This helper fires that URI — but ONLY
# if Obsidian is already running, so vault writes don't accidentally launch it.


def maybe_reload_db_plugin(notify: bool = False) -> None:
    """Best-effort signal to Obsidian's SQLite DB Plugin to re-read vault.db.

    Behavior:
      • If Obsidian isn't running → no-op (we don't auto-launch it on writes).
      • If the companion plugin (sqlite-db-reload) isn't enabled → no-op.
      • Errors are swallowed; this is a UX nicety, not critical path.

    Called automatically after the *_upsert / *_remove tools.  Pass
    ``reload_db=False`` on those tools to suppress when doing bulk writes;
    then call ``reload_sqlite_db_plugin()`` once at the end.
    """
    try:
        # 1. Is Obsidian running?  Cheap pgrep — bail if no.
        if sys.platform == "darwin":
            check = subprocess.run(
                ["pgrep", "-f", "Obsidian.app"],
                capture_output=True, timeout=2,
            )
            if check.returncode != 0:
                return
        elif sys.platform.startswith("linux"):
            check = subprocess.run(
                ["pgrep", "-fi", "obsidian"],
                capture_output=True, timeout=2,
            )
            if check.returncode != 0:
                return
        # Windows / other: skip the running check, the open call below will
        # spawn Obsidian if not running — accepted trade-off.

        # 2. Fire the URI.
        uri = "obsidian://sqlite-db-reload"
        if notify:
            uri += "?notify=1"

        if sys.platform == "darwin":
            cmd = ["open", uri]
        elif sys.platform.startswith("linux"):
            cmd = ["xdg-open", uri]
        elif sys.platform == "win32":
            cmd = ["cmd", "/c", "start", "", uri]
        else:
            return

        subprocess.run(cmd, capture_output=True, timeout=3)
    except Exception:
        return  # silent


# ─── from original L27-535: Basic vault safety + generic file support ───
# =============================================================================
# Basic vault safety helpers
# =============================================================================


def get_vault_root() -> Path:
    vault = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vault:
        raise RuntimeError("OBSIDIAN_VAULT_PATH is not set")

    root = Path(vault).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"Vault path does not exist or is not a directory: {root}")

    return root


def safe_path(relative_path: str) -> Path:
    root = get_vault_root()
    candidate = (root / relative_path).resolve()

    if root not in candidate.parents and candidate != root:
        raise ValueError("Refusing to access path outside vault")

    return candidate


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def relative_to_vault(path: Path) -> str:
    # NFC-normalize to match wikilink text (macOS APFS stores filenames in NFD)
    return unicodedata.normalize("NFC", str(path.relative_to(get_vault_root())).replace("\\", "/"))


def today_iso() -> str:
    return datetime.now().date().isoformat()


# -- Natural-language date resolution -----------------------------------------

_WEEKDAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def resolve_natural_date(text: str) -> str | None:
    """
    Resolve a natural-language date expression to YYYY-MM-DD.

    Supports: "today", "tomorrow", "yesterday",
              "next monday", "this friday", "monday",
              "in 3 days", "in 2 weeks",
              "end of month", "end of week",
              or a literal "YYYY-MM-DD" passthrough.

    Returns None if the text can't be parsed.
    """
    s = text.strip().lower()
    today = datetime.now().date()

    # Passthrough ISO date
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s

    # Simple keywords
    if s == "today":
        return today.isoformat()
    if s == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if s == "yesterday":
        return (today - timedelta(days=1)).isoformat()

    # "in N days/weeks/months"
    m = re.match(r"in\s+(\d+)\s+(day|days|week|weeks|month|months)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("day"):
            return (today + timedelta(days=n)).isoformat()
        elif unit.startswith("week"):
            return (today + timedelta(weeks=n)).isoformat()
        elif unit.startswith("month"):
            # Approximate: add n*30 days
            year = today.year
            month = today.month + n
            while month > 12:
                month -= 12
                year += 1
            day = min(today.day, calendar.monthrange(year, month)[1])
            return f"{year:04d}-{month:02d}-{day:02d}"

    # "end of week" (next Sunday)
    if s in ("end of week", "eow"):
        days_until_sunday = (6 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_until_sunday)).isoformat()

    # "end of month" / "eom"
    if s in ("end of month", "eom"):
        last_day = calendar.monthrange(today.year, today.month)[1]
        return f"{today.year:04d}-{today.month:02d}-{last_day:02d}"

    # "next <weekday>" — the NEXT occurrence (skips this week if today)
    m = re.match(r"(?:next\s+)?(\w+)", s)
    if m:
        day_name = m.group(1)
        if day_name in _WEEKDAY_NAMES:
            target_wd = _WEEKDAY_NAMES[day_name]
            current_wd = today.weekday()
            if "next" in s:
                # Always go to next week's occurrence
                delta = (target_wd - current_wd) % 7
                if delta == 0:
                    delta = 7
            else:
                # "monday" / "this monday" — nearest future
                delta = (target_wd - current_wd) % 7
                if delta == 0:
                    delta = 7  # if today is Monday, "monday" means next Monday
            return (today + timedelta(days=delta)).isoformat()

    return None


def _resolve_date_range(text: str) -> tuple[str, int] | None:
    """
    Parse a natural-language *range* expression into (start_iso, num_days).

    Returns None when the text is a single-date expression (let
    ``resolve_natural_date`` handle those).
    """
    s = text.strip().lower()
    today = datetime.now().date()

    # ── this week / next week ───────────────────────────────────────────
    if s in ("this week", "week", "rest of week", "rest of the week"):
        days_left = 7 - today.weekday()           # Mon=0 → 7, Sun=6 → 1
        return (today.isoformat(), days_left)

    if s == "next week":
        next_monday = today + timedelta(days=(7 - today.weekday()))
        return (next_monday.isoformat(), 7)

    # ── this month / next month ─────────────────────────────────────────
    if s in ("this month", "month", "rest of month", "rest of the month"):
        last_day = calendar.monthrange(today.year, today.month)[1]
        days_left = last_day - today.day + 1
        return (today.isoformat(), days_left)

    if s == "next month":
        m = today.month + 1
        y = today.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        first = today.replace(year=y, month=m, day=1)
        return (first.isoformat(), calendar.monthrange(y, m)[1])

    # ── "next N days/weeks/months" ──────────────────────────────────────
    match = re.match(r"next\s+(\d+)\s+(day|days|week|weeks|month|months)", s)
    if match:
        n = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("day"):
            return (today.isoformat(), n)
        if unit.startswith("week"):
            return (today.isoformat(), n * 7)
        if unit.startswith("month"):
            return (today.isoformat(), n * 30)

    # ── bare "N days / N weeks" ─────────────────────────────────────────
    match = re.match(r"(\d+)\s+(day|days|week|weeks|month|months)", s)
    if match:
        n = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("day"):
            return (today.isoformat(), n)
        if unit.startswith("week"):
            return (today.isoformat(), n * 7)
        if unit.startswith("month"):
            return (today.isoformat(), n * 30)

    return None


IGNORED_VAULT_PARTS = {
    ".obsidian",
    ".ai_memory_jobs",
    ".ai_memory_cache",
    ".git",
    "_trash",  # 90_Inbox/_trash/ — visible vault trash
}


def is_ignored_path(path: Path) -> bool:
    return any(part in IGNORED_VAULT_PARTS for part in path.parts)


# =============================================================================
# Generic file support: indexing, reading, writing, moving, deleting
# =============================================================================


ALLOWED_VAULT_FILE_EXTENSIONS = {
    ".canvas",
    ".md",
    ".txt",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".svg",
    ".excalidraw",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".zip",
}


TEXT_INDEXABLE_EXTENSIONS = {
    ".canvas",
    ".md",
    ".txt",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".svg",
    ".excalidraw",
}


OPTIONAL_TEXT_EXTRACT_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
}


def vault_suffix(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".excalidraw.md"):
        return ".excalidraw.md"
    return path.suffix.lower()


def ensure_allowed_vault_file(path: Path) -> None:
    if is_ignored_path(path):
        raise ValueError("Refusing to access ignored/internal vault path.")

    suffix = vault_suffix(path)
    if suffix == ".excalidraw.md":
        return

    if suffix not in ALLOWED_VAULT_FILE_EXTENSIONS:
        allowed = sorted(ALLOWED_VAULT_FILE_EXTENSIONS | {".excalidraw.md"})
        raise ValueError(
            f"Refusing unsupported file type: {suffix or '(no extension)'}. "
            f"Allowed extensions: {', '.join(allowed)}"
        )


def is_text_indexable_file(path: Path) -> bool:
    suffix = vault_suffix(path)
    return (
        suffix == ".excalidraw.md"
        or suffix in TEXT_INDEXABLE_EXTENSIONS
        or suffix in OPTIONAL_TEXT_EXTRACT_EXTENSIONS
    )


def all_vault_files(
    include_binary: bool = True,
    include_indexable_only: bool = False,
    folder: str = "",
) -> list[Path]:
    root = get_vault_root()
    base = safe_path(folder) if folder.strip() else root

    if not base.exists():
        return []

    candidates = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
    files: list[Path] = []

    for path in candidates:
        if is_ignored_path(path):
            continue
        try:
            ensure_allowed_vault_file(path)
        except ValueError:
            continue
        if include_indexable_only and not is_text_indexable_file(path):
            continue
        if not include_binary and not is_text_indexable_file(path):
            continue
        files.append(path)

    files.sort(key=lambda p: relative_to_vault(p).lower())
    return files


def compact_snippet(text: str, query_terms: list[str], max_chars: int = 500) -> str:
    lower = text.lower()
    first_pos: Optional[int] = None

    for term in query_terms:
        pos = lower.find(term.lower())
        if pos >= 0:
            first_pos = pos if first_pos is None else min(first_pos, pos)

    if first_pos is None:
        snippet = text[:max_chars]
    else:
        start = max(0, first_pos - max_chars // 3)
        snippet = text[start : start + max_chars]

    return re.sub(r"\s+", " ", snippet).strip()


def read_pdf_text(path: Path, max_chars: int) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        return (
            "[PDF text extraction unavailable. Install with `pip install pypdf`. "
            f"Import error: {exc}]"
        )

    try:
        reader = PdfReader(str(path))
        chunks: list[str] = []
        total = 0
        for page in reader.pages:
            if total >= max_chars:
                break
            txt = page.extract_text() or ""
            chunks.append(txt)
            total += len(txt)
        return "\n\n".join(chunks).strip()[:max_chars]
    except Exception as exc:
        return f"[PDF text extraction failed: {exc}]"


def read_docx_text(path: Path, max_chars: int) -> str:
    try:
        import docx
    except Exception as exc:
        return (
            "[DOCX text extraction unavailable. Install with `pip install python-docx`. "
            f"Import error: {exc}]"
        )

    try:
        doc = docx.Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs).strip()
        return text[:max_chars]
    except Exception as exc:
        return f"[DOCX text extraction failed: {exc}]"


def read_xlsx_text(path: Path, max_chars: int) -> str:
    try:
        import openpyxl
    except Exception as exc:
        return (
            "[XLSX text extraction unavailable. Install with `pip install openpyxl`. "
            f"Import error: {exc}]"
        )

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        chunks: list[str] = []
        total = 0
        for sheet in wb.worksheets:
            if total >= max_chars:
                break
            header = f"# Sheet: {sheet.title}"
            chunks.append(header)
            total += len(header)
            for row in sheet.iter_rows(values_only=True):
                if total >= max_chars:
                    break
                values = ["" if v is None else str(v) for v in row]
                if any(v.strip() for v in values):
                    line = "\t".join(values)
                    chunks.append(line)
                    total += len(line)
        return "\n".join(chunks).strip()[:max_chars]
    except Exception as exc:
        return f"[XLSX text extraction failed: {exc}]"


def read_indexable_file_text(path: Path, max_chars: int = 50000) -> str:
    ensure_allowed_vault_file(path)
    max_chars = max(1000, min(max_chars, 200000))
    suffix = vault_suffix(path)

    if suffix == ".excalidraw.md" or suffix in TEXT_INDEXABLE_EXTENSIONS:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]

    if suffix == ".pdf":
        return read_pdf_text(path, max_chars)

    if suffix == ".docx":
        return read_docx_text(path, max_chars)

    if suffix == ".xlsx":
        return read_xlsx_text(path, max_chars)

    stat = path.stat()
    return (
        "[Binary/non-text file]\n"
        f"Name: {path.name}\n"
        f"Type: {suffix or '(no extension)'}\n"
        f"Size bytes: {stat.st_size}\n"
        f"Modified: {datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')}\n"
    )


def title_from_text(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def file_title(path: Path, text: str = "") -> str:
    suffix = vault_suffix(path)

    if suffix in {".md", ".excalidraw.md"}:
        return title_from_text(text, path.stem)

    if suffix in {".json", ".excalidraw"}:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("title", "name"):
                    if key in data and str(data[key]).strip():
                        return str(data[key]).strip()
        except Exception:
            pass

    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def score_vault_file(path: Path, text: str, terms: list[str], root: Path) -> int:
    rel = str(path.relative_to(root)).replace("\\", "/").lower()
    name = path.name.lower()
    lower = text.lower()
    score = 0

    for term in terms:
        t = term.lower().strip()
        if not t:
            continue
        if t in name:
            score += 80
        if t in rel:
            score += 40
        score += lower.count(t) * 10
        for line in lower.splitlines()[:2000]:
            if t in line:
                if line.startswith("#"):
                    score += 25
                if "tags:" in line or "type:" in line or "status:" in line:
                    score += 10

    if is_text_indexable_file(path):
        score += 3
    return score



# ─── from original L1229-1455: Markdown link helpers + Frontmatter helpers ───
# =============================================================================
# Markdown / Obsidian link helpers
# =============================================================================


def count_words(text: str) -> int:
    return len(re.findall(r"\b\S+\b", text))


def normalize_note_target(path: str) -> str:
    p = path.strip().replace("\\", "/")
    if p.endswith(".md"):
        p = p[:-3]
    return unicodedata.normalize("NFC", p.strip("/"))


def make_wikilink(target_path: str, display_text: Optional[str] = None,
                  table_safe: bool = True) -> str:
    target = normalize_note_target(target_path)
    # Use \| instead of | so the link is safe inside Markdown tables.
    # Obsidian treats [[path\|display]] and [[path|display]] identically.
    sep = r"\|" if table_safe else "|"
    if display_text and display_text.strip():
        return f"[[{target}{sep}{display_text.strip()}]]"
    # Auto-derive display text from the path to keep links human-readable.
    # e.g. "60_Knowledge/Computer_Science/Finite Automata" → "Finite Automata"
    basename = target.rsplit("/", 1)[-1] if "/" in target else target
    # Strip common extensions that might remain
    for ext in (".excalidraw", ):
        if basename.endswith(ext):
            basename = basename[: -len(ext)]
    if basename != target:
        return f"[[{target}{sep}{basename}]]"
    return f"[[{target}]]"


def extract_wikilinks(text: str) -> list[dict[str, str]]:
    # Match both [[path|display]] and [[path\|display]] (table-safe escaped pipe)
    pattern = re.compile(r"\[\[([^\]|#]+(?:#[^\]|]+)?)(?:\\?\|([^\]]+))?\]\]")
    links: list[dict[str, str]] = []
    for match in pattern.finditer(text):
        raw_target = match.group(1).strip().rstrip("\\")  # strip trailing \ from table-safe links
        display = match.group(2).strip() if match.group(2) else ""
        note_target = raw_target.split("#", 1)[0].strip()
        links.append(
            {
                "raw": match.group(0),
                "target": raw_target,
                "note_target": note_target,
                "display_text": display,
            }
        )
    return links


def note_target_to_relative_md(target: str) -> str:
    clean = target.strip().replace("\\", "/").split("#", 1)[0].strip("/")
    if not clean.endswith(".md"):
        clean += ".md"
    return clean


def find_section_bounds(text: str, section: str) -> tuple[int, int] | None:
    escaped = re.escape(section.strip())
    heading_re = re.compile(rf"^(?P<hashes>#+)\s+{escaped}\s*$", re.MULTILINE)
    match = heading_re.search(text)
    if not match:
        return None

    level = len(match.group("hashes"))
    start = match.start()
    next_heading_re = re.compile(r"^(?P<hashes>#+)\s+.+$", re.MULTILINE)
    for next_match in next_heading_re.finditer(text, match.end()):
        next_level = len(next_match.group("hashes"))
        if next_level <= level:
            return (start, next_match.start())
    return (start, len(text))


def ensure_section(text: str, section: str) -> str:
    if find_section_bounds(text, section) is not None:
        return text
    return text.rstrip() + f"\n\n## {section}\n\n"


def append_bullet_to_section(text: str, section: str, bullet: str) -> str:
    text = ensure_section(text, section)
    bounds = find_section_bounds(text, section)
    if bounds is None:
        return text.rstrip() + f"\n\n## {section}\n\n{bullet}\n"
    start, end = bounds
    section_text = text[start:end].rstrip()
    if bullet in section_text:
        return text
    section_text += f"\n{bullet}"
    return text[:start] + section_text + "\n" + text[end:]


def append_unique_line_to_section(text: str, section: str, line: str) -> str:
    text = ensure_section(text, section)
    bounds = find_section_bounds(text, section)
    if bounds is None:
        return text.rstrip() + f"\n\n## {section}\n\n{line}\n"
    start, end = bounds
    section_text = text[start:end].rstrip()
    if line in section_text:
        return text
    section_text += f"\n{line}"
    return text[:start] + section_text + "\n" + text[end:]


def append_table_row_to_section(text: str, section: str, header: str, separator: str, row: str) -> str:
    text = ensure_section(text, section)
    bounds = find_section_bounds(text, section)
    if bounds is None:
        return text.rstrip() + f"\n\n## {section}\n\n{header}\n{separator}\n{row}\n"
    start, end = bounds
    section_text = text[start:end].rstrip()
    if header not in section_text:
        section_text += f"\n\n{header}\n{separator}\n{row}"
    elif row not in section_text:
        section_text += f"\n{row}"
    return text[:start] + section_text + "\n" + text[end:]


def escape_table_cell(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value.replace("|", "\\|")


# =============================================================================
# Frontmatter helpers
# =============================================================================


SAFE_FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text

    end = text.find("\n---", 4)
    if end == -1:
        return {}, text

    raw = text[4:end]
    body = text[end + len("\n---"):]
    if body.startswith("\n"):
        body = body[1:]

    data: dict[str, object] = {}
    current_key: str | None = None

    for line in raw.splitlines():
        if not line.strip():
            continue

        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(line.strip()[2:].strip().strip('"').strip("'"))
            continue

        if ":" not in line:
            current_key = None
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key

        if not SAFE_FRONTMATTER_KEY_RE.match(key):
            continue

        if value == "":
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if inner:
                data[key] = [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]
            else:
                data[key] = []
        else:
            data[key] = value.strip('"').strip("'")
            current_key = None

    return data, body


def dump_frontmatter(data: dict[str, object], body: str) -> str:
    lines = ["---"]
    for key in sorted(data.keys()):
        if not SAFE_FRONTMATTER_KEY_RE.match(key):
            continue
        value = data[key]
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                s = str(item).strip()
                if s:
                    lines.append(f"  - {s}")
        elif value is None:
            continue
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body.lstrip("\n")


def unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        clean = str(item).strip()
        if clean and clean not in seen:
            out.append(clean)
            seen.add(clean)
    return out


def note_has_frontmatter(text: str) -> bool:
    return text.startswith("---\n") and "\n---" in text[4:]



# ─── from original L5593-7229: VaultIndex class ───
# =============================================================================
# SQLite vault index — disposable cache, Markdown files remain source of truth
# =============================================================================


class VaultIndex:
    """
    SQLite-backed index of the Obsidian vault.

    The database is a **disposable cache**: all data is derived from the
    Markdown files and can be fully rebuilt at any time.  The DB lives at
    ``<vault>/.ai_memory_cache/vault.db`` (ignored by Obsidian and the MCP
    tool's own ``is_ignored_path`` check).

    Tables
    ------
    files      – every allowed vault file (path, suffix, size, mtime_ns, content_hash)
    notes      – Markdown-specific metadata (title, type, status, word_count)
    frontmatter – key/value pairs extracted from YAML front matter
    tags       – per-note tags (from frontmatter ``tags`` list)
    aliases    – per-note aliases (from frontmatter ``aliases`` list)
    wikilinks  – directed edges (source → target, with display text)
    tasks      – task bullets from ``## Tasks`` sections
    reminders  – reminder bullets from ``## Reminders`` sections
    events     – calendar events from ``## Schedule`` sections
    fts        – FTS5 full-text search over note body text
    """

    SCHEMA_VERSION = 7

    def __init__(self, vault_root: Path):
        self._root = vault_root
        cache_dir = vault_root / ".ai_memory_cache"
        cache_dir.mkdir(exist_ok=True)
        self._db_path = cache_dir / "vault.db"
        self._conn: sqlite3.Connection | None = None

    # -- connection management ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._ensure_schema()
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._connect()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- schema ---------------------------------------------------------------

    def _ensure_schema(self):
        c = self._conn
        assert c is not None

        # Check schema version
        c.execute(
            "CREATE TABLE IF NOT EXISTS _meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        row = c.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
        current = int(row["value"]) if row else 0

        if current < 3:
            # Fresh install or pre-v3 — full rebuild
            for tbl in [
                "fts", "events", "tasks", "reminders", "wikilinks",
                "aliases", "tags", "frontmatter", "notes", "files",
                "note_access", "revisions",
            ]:
                c.execute(f"DROP TABLE IF EXISTS {tbl}")
            c.execute("DELETE FROM _meta")

        # -- files: all vault files
        c.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path       TEXT PRIMARY KEY,
                suffix     TEXT NOT NULL,
                size       INTEGER NOT NULL,
                mtime_ns   INTEGER NOT NULL,
                content_hash TEXT NOT NULL DEFAULT ''
            )
        """)

        # -- notes: Markdown-specific enrichment
        c.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                path           TEXT PRIMARY KEY REFERENCES files(path) ON DELETE CASCADE,
                title          TEXT NOT NULL DEFAULT '',
                type           TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT '',
                word_count     INTEGER NOT NULL DEFAULT 0,
                summary        TEXT NOT NULL DEFAULT '',
                summary_source TEXT NOT NULL DEFAULT ''
            )
        """)
        # v5 additive migration: add summary columns to existing tables
        for col, typedef in [("summary", "TEXT NOT NULL DEFAULT ''"),
                             ("summary_source", "TEXT NOT NULL DEFAULT ''")]:
            try:
                c.execute(f"ALTER TABLE notes ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # -- frontmatter key/value
        c.execute("""
            CREATE TABLE IF NOT EXISTS frontmatter (
                note_path  TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                PRIMARY KEY (note_path, key, value)
            )
        """)

        # -- tags
        c.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                note_path  TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                tag        TEXT NOT NULL,
                PRIMARY KEY (note_path, tag)
            )
        """)

        # -- aliases
        c.execute("""
            CREATE TABLE IF NOT EXISTS aliases (
                note_path  TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                alias      TEXT NOT NULL,
                PRIMARY KEY (note_path, alias)
            )
        """)

        # -- wikilinks (source → target)
        c.execute("""
            CREATE TABLE IF NOT EXISTS wikilinks (
                source_path   TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                target        TEXT NOT NULL,
                display_text  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source_path, target, display_text)
            )
        """)

        # -- tasks from ## Tasks sections
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                note_path  TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                text       TEXT NOT NULL,
                checked    INTEGER NOT NULL DEFAULT 0,
                due        TEXT NOT NULL DEFAULT '',
                priority   TEXT NOT NULL DEFAULT '',
                done       TEXT NOT NULL DEFAULT ''
            )
        """)

        # -- reminders from ## Reminders sections
        c.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                note_path   TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                text        TEXT NOT NULL,
                checked     INTEGER NOT NULL DEFAULT 0,
                remind_on   TEXT NOT NULL DEFAULT '',
                repeat      TEXT NOT NULL DEFAULT '',
                done        TEXT NOT NULL DEFAULT ''
            )
        """)

        # -- events from ## Schedule sections (calendar/agenda)
        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                note_path   TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                date        TEXT NOT NULL DEFAULT '',
                time        TEXT NOT NULL DEFAULT '',
                end_time    TEXT NOT NULL DEFAULT '',
                title       TEXT NOT NULL,
                location    TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                end_date    TEXT NOT NULL DEFAULT '',
                all_day     INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Additive migration: add end_date/all_day columns to existing tables
        for col, typedef in [("end_date", "TEXT NOT NULL DEFAULT ''"),
                             ("all_day", "INTEGER NOT NULL DEFAULT 0")]:
            try:
                c.execute(f"ALTER TABLE events ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # -- FTS5 full-text search (body text of Markdown notes)
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                path, title, body,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)

        # -- v4: hotness / access tracking
        c.execute("""
            CREATE TABLE IF NOT EXISTS note_access (
                path           TEXT PRIMARY KEY,
                access_count   INTEGER NOT NULL DEFAULT 0,
                last_accessed  TEXT NOT NULL DEFAULT ''
            )
        """)

        # -- v4: revision history
        c.execute("""
            CREATE TABLE IF NOT EXISTS revisions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                path           TEXT NOT NULL,
                content        TEXT NOT NULL,
                content_hash   TEXT NOT NULL DEFAULT '',
                saved_at       TEXT NOT NULL DEFAULT '',
                word_count     INTEGER NOT NULL DEFAULT 0
            )
        """)

        # -- v6+: anime list mirror, synced with MAL. The DB is the source of truth;
        # the Anime hub markdown is a live view via the SQLite DB plugin.
        c.execute("""
            CREATE TABLE IF NOT EXISTS anime_list (
                mal_id        INTEGER PRIMARY KEY,
                title         TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT '',
                score         INTEGER,
                start_date    TEXT NOT NULL DEFAULT '',
                end_date      TEXT NOT NULL DEFAULT '',
                eps_watched   INTEGER,
                eps_total     INTEGER,
                priority      TEXT NOT NULL DEFAULT '',
                note          TEXT NOT NULL DEFAULT '',
                updated_at    TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_anime_status ON anime_list(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_anime_title ON anime_list(title COLLATE NOCASE)")

        # Pre-aggregated views for chart blocks (the SQLite DB plugin can't COUNT(*)
        # itself — it just sums valueColumn raw, so we need pre-grouped views).
        c.execute("""
            CREATE VIEW IF NOT EXISTS anime_status_counts AS
            SELECT status, COUNT(*) AS cnt
            FROM anime_list
            GROUP BY status
        """)
        c.execute("""
            CREATE VIEW IF NOT EXISTS anime_score_counts AS
            SELECT score, COUNT(*) AS cnt
            FROM anime_list
            WHERE score IS NOT NULL
            GROUP BY score
            ORDER BY score
        """)
        # Range views — the plugin only does equality filters, so for range queries
        # ("completed score < 7", etc.) we pre-filter via views.
        c.execute("""
            CREATE VIEW IF NOT EXISTS anime_completed_below_7 AS
            SELECT * FROM anime_list
            WHERE status = 'completed' AND score IS NOT NULL AND score < 7
        """)
        c.execute("""
            CREATE VIEW IF NOT EXISTS anime_completed_unrated AS
            SELECT * FROM anime_list
            WHERE status = 'completed' AND score IS NULL
        """)

        # Pending holding-pen no longer used (markdown isn't parsed anymore).
        c.execute("DROP TABLE IF EXISTS anime_list_pending")

        # -- v8: vocabulary (Japanese + Korean + future languages)
        c.execute("""
            CREATE TABLE IF NOT EXISTS vocab (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                language      TEXT NOT NULL,
                word          TEXT NOT NULL,
                reading       TEXT NOT NULL DEFAULT '',
                meaning       TEXT NOT NULL DEFAULT '',
                category      TEXT NOT NULL DEFAULT '',
                source        TEXT NOT NULL DEFAULT '',
                note          TEXT NOT NULL DEFAULT '',
                last_reviewed TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT '',
                updated_at    TEXT NOT NULL DEFAULT '',
                UNIQUE(language, word)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_vocab_lang ON vocab(language)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vocab_word ON vocab(word)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vocab_category ON vocab(category)")

        # -- v8: people (family, friends, colleagues — anyone with birthday/contact info)
        c.execute("""
            CREATE TABLE IF NOT EXISTS people (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL,
                category       TEXT NOT NULL DEFAULT '',
                subcategory    TEXT NOT NULL DEFAULT '',
                relationship   TEXT NOT NULL DEFAULT '',
                birthday_day   INTEGER,
                birthday_month INTEGER,
                birthday_year  INTEGER,
                location       TEXT NOT NULL DEFAULT '',
                badge          TEXT NOT NULL DEFAULT '',
                note           TEXT NOT NULL DEFAULT '',
                page_link      TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL DEFAULT '',
                updated_at     TEXT NOT NULL DEFAULT '',
                UNIQUE(name)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_people_category ON people(category)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_people_bday ON people(birthday_month, birthday_day)")

        # View: upcoming birthdays — recreated each startup so the name_link
        # column (wikilink markup, picked up by the sqlite-db-companion plugin)
        # stays in sync with schema changes.
        c.execute("DROP VIEW IF EXISTS people_upcoming_birthdays")
        c.execute("""
            CREATE VIEW people_upcoming_birthdays AS
            SELECT
                name, category, relationship, birthday_day, birthday_month, birthday_year,
                CAST(strftime('%Y','now') AS INTEGER) AS this_year,
                printf('%04d-%02d-%02d',
                    CASE
                        WHEN strftime('%m-%d','now') > printf('%02d-%02d', birthday_month, birthday_day)
                        THEN CAST(strftime('%Y','now') AS INTEGER) + 1
                        ELSE CAST(strftime('%Y','now') AS INTEGER)
                    END,
                    birthday_month, birthday_day
                ) AS next_birthday,
                CAST(
                    julianday(printf('%04d-%02d-%02d',
                        CASE
                            WHEN strftime('%m-%d','now') > printf('%02d-%02d', birthday_month, birthday_day)
                            THEN CAST(strftime('%Y','now') AS INTEGER) + 1
                            ELSE CAST(strftime('%Y','now') AS INTEGER)
                        END,
                        birthday_month, birthday_day
                    )) - julianday('now') AS INTEGER
                ) AS days_until,
                CASE
                    WHEN page_link != '' AND page_link IS NOT NULL
                    THEN '[[' || page_link || '|' || name || ']]'
                    ELSE name
                END AS name_link
            FROM people
            WHERE birthday_month IS NOT NULL AND birthday_day IS NOT NULL
            ORDER BY days_until
        """)

        # View: all people, with a name_link column that resolves to an internal
        # wikilink when page_link is set. The sqlite-db-companion plugin parses
        # these `[[…]]` strings into clickable internal-link anchors at render.
        c.execute("DROP VIEW IF EXISTS people_linked")
        c.execute("""
            CREATE VIEW people_linked AS
            SELECT
                id, name, category, subcategory, relationship,
                birthday_day, birthday_month, birthday_year,
                location, badge, note, page_link, created_at, updated_at,
                CASE
                    WHEN page_link != '' AND page_link IS NOT NULL
                    THEN '[[' || page_link || '|' || name || ']]'
                    ELSE name
                END AS name_link
            FROM people
        """)

        # -- v8: warranties (purchased items with warranty expiry)
        c.execute("""
            CREATE TABLE IF NOT EXISTS warranties (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product         TEXT NOT NULL,
                warranty_until  TEXT NOT NULL DEFAULT '',
                purchase_date   TEXT NOT NULL DEFAULT '',
                receipt_path    TEXT NOT NULL DEFAULT '',
                vendor          TEXT NOT NULL DEFAULT '',
                price           TEXT NOT NULL DEFAULT '',
                note            TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL DEFAULT '',
                updated_at      TEXT NOT NULL DEFAULT '',
                UNIQUE(product, receipt_path)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_warranty_until ON warranties(warranty_until)")
        # Warranties views — recreated each startup so product_link stays in
        # sync. product_link wraps the product name in a wikilink that points
        # to the receipt PDF (or any path stored in receipt_path).
        c.execute("DROP VIEW IF EXISTS warranties_active")
        c.execute("""
            CREATE VIEW warranties_active AS
            SELECT *,
                CAST(julianday(warranty_until) - julianday('now') AS INTEGER) AS days_left,
                CASE
                    WHEN receipt_path != '' AND receipt_path IS NOT NULL
                    THEN '[[' || receipt_path || '|' || product || ']]'
                    ELSE product
                END AS product_link
            FROM warranties
            WHERE warranty_until != '' AND julianday(warranty_until) >= julianday('now')
            ORDER BY warranty_until
        """)
        c.execute("DROP VIEW IF EXISTS warranties_expired")
        c.execute("""
            CREATE VIEW warranties_expired AS
            SELECT *,
                CAST(julianday('now') - julianday(warranty_until) AS INTEGER) AS days_since_expiry,
                CASE
                    WHEN receipt_path != '' AND receipt_path IS NOT NULL
                    THEN '[[' || receipt_path || '|' || product || ']]'
                    ELSE product
                END AS product_link
            FROM warranties
            WHERE warranty_until != '' AND julianday(warranty_until) < julianday('now')
            ORDER BY warranty_until DESC
        """)

        # -- useful indexes
        c.execute("CREATE INDEX IF NOT EXISTS idx_files_suffix ON files(suffix)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notes_title ON notes(title COLLATE NOCASE)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_fm_key ON frontmatter(key)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_fm_key_value ON frontmatter(key, value)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias COLLATE NOCASE)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_wl_target ON wikilinks(target)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_checked ON tasks(checked)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_reminders_remind ON reminders(remind_on)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_datetime ON events(date, time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_end_date ON events(end_date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_revisions_path ON revisions(path)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_revisions_saved ON revisions(saved_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_access_count ON note_access(access_count)")

        c.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(self.SCHEMA_VERSION),),
        )
        c.commit()

    # -- content hashing ------------------------------------------------------

    @staticmethod
    def _hash_content(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]

    @staticmethod
    def _auto_summary(body: str, max_chars: int = 300) -> str:
        """Heuristic summary from a note's body. Skips top heading, callouts, code fences, embeds/wikilink-only lines."""
        if not body:
            return ""
        lines = body.split("\n")
        paragraph: list[str] = []
        in_code = False
        for raw in lines:
            line = raw.rstrip()
            stripped = line.lstrip()
            if stripped.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            if not stripped:
                if paragraph:
                    break
                continue
            # Skip any heading (H1-H6), frontmatter remnants, image embeds, pure wikilink-only lines, callouts
            if re.match(r"^#{1,6}\s", stripped):
                continue
            if stripped.startswith("> [!"):
                continue
            if stripped.startswith("![[") or stripped.startswith("![]("):
                continue
            if re.fullmatch(r"!?\[\[[^\]]+\]\]", stripped):
                continue
            if stripped.startswith("---") or stripped.startswith("==="):
                continue
            paragraph.append(stripped)
            if sum(len(p) + 1 for p in paragraph) >= max_chars:
                break
        text = " ".join(paragraph).strip()
        # Strip leading list markers / quote markers
        text = re.sub(r"^[-*>]\s+", "", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text)
        return text[:max_chars].rstrip()

    # -- single-file indexing -------------------------------------------------

    def _index_file(self, path: Path, text: str | None = None):
        """Index (or re-index) a single vault file into the database."""
        c = self.conn
        rel = unicodedata.normalize("NFC", str(path.relative_to(self._root)).replace("\\", "/"))
        suffix = vault_suffix(path)
        stat = path.stat()
        size = stat.st_size
        mtime_ns = stat.st_mtime_ns

        is_md = suffix in {".md", ".excalidraw.md"}

        if text is None and is_md:
            text = read_text(path)

        content_hash = self._hash_content(text) if text else ""

        # Upsert files row
        c.execute(
            "INSERT OR REPLACE INTO files (path, suffix, size, mtime_ns, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (rel, suffix, size, mtime_ns, content_hash),
        )

        if not is_md:
            # Non-Markdown files only go into the files table
            return

        # -- Markdown note enrichment --
        assert text is not None

        data, body = split_frontmatter(text)

        title = title_from_text(text, path.stem)
        note_type = str(data.get("type", "")).strip()
        note_status = str(data.get("status", "")).strip()
        wc = count_words(body)

        # Preserve manually-set summaries; otherwise auto-generate from body
        prev = c.execute(
            "SELECT summary, summary_source FROM notes WHERE path = ?", (rel,)
        ).fetchone()
        if prev and prev["summary_source"] == "manual":
            summary = prev["summary"]
            summary_source = "manual"
        else:
            summary = self._auto_summary(body)
            summary_source = "auto" if summary else ""

        # Upsert notes row
        c.execute(
            "INSERT OR REPLACE INTO notes (path, title, type, status, word_count, summary, summary_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rel, title, note_type, note_status, wc, summary, summary_source),
        )

        # -- clear old derived data for this note
        for tbl in ("frontmatter", "tags", "aliases", "wikilinks", "tasks", "reminders"):
            c.execute(f"DELETE FROM {tbl} WHERE {'note_path' if tbl != 'wikilinks' else 'source_path'} = ?", (rel,))

        # Delete old FTS entry
        c.execute("DELETE FROM fts WHERE path = ?", (rel,))

        # -- frontmatter key/value pairs
        fm_rows: list[tuple[str, str, str]] = []
        for key, val in data.items():
            if key in ("tags", "aliases"):
                continue  # handled separately
            if isinstance(val, list):
                for v in val:
                    fm_rows.append((rel, key, str(v).strip()))
            else:
                fm_rows.append((rel, key, str(val).strip()))
        if fm_rows:
            c.executemany(
                "INSERT OR IGNORE INTO frontmatter (note_path, key, value) VALUES (?, ?, ?)",
                fm_rows,
            )

        # -- tags
        tags_raw = data.get("tags", [])
        tag_list = [tags_raw] if isinstance(tags_raw, str) else [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        tag_rows = [(rel, t.strip()) for t in tag_list if t.strip()]
        if tag_rows:
            c.executemany(
                "INSERT OR IGNORE INTO tags (note_path, tag) VALUES (?, ?)", tag_rows
            )

        # -- aliases
        aliases_raw = data.get("aliases", [])
        alias_list = [aliases_raw] if isinstance(aliases_raw, str) else [str(a) for a in aliases_raw] if isinstance(aliases_raw, list) else []
        alias_rows = [(rel, a.strip()) for a in alias_list if a.strip()]
        if alias_rows:
            c.executemany(
                "INSERT OR IGNORE INTO aliases (note_path, alias) VALUES (?, ?)",
                alias_rows,
            )

        # -- wikilinks
        links = extract_wikilinks(text)
        link_rows = [
            (rel, normalize_note_target(lnk["note_target"]), lnk["display_text"])
            for lnk in links
        ]
        if link_rows:
            c.executemany(
                "INSERT OR IGNORE INTO wikilinks (source_path, target, display_text) "
                "VALUES (?, ?, ?)",
                link_rows,
            )

        # -- tasks from ## Tasks section
        task_lines = find_task_lines_in_section(text, "Tasks")
        task_rows = [
            (rel, p["text"], int(p["checked"]), p["due"], p["priority"], p["done"])
            for _, _, p in task_lines
        ]
        if task_rows:
            c.executemany(
                "INSERT INTO tasks (note_path, text, checked, due, priority, done) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                task_rows,
            )

        # -- reminders from ## Reminders section
        reminder_lines = find_task_lines_in_section(text, "Reminders")
        reminder_rows = [
            (rel, p["text"], int(p["checked"]), p.get("remind_on", ""), p.get("repeat", ""), p["done"])
            for _, _, p in reminder_lines
        ]
        if reminder_rows:
            c.executemany(
                "INSERT INTO reminders (note_path, text, checked, remind_on, repeat, done) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                reminder_rows,
            )

        # -- events from ## Schedule section
        event_entries = parse_schedule_section(text)
        if event_entries:
            # Try to derive date from the note filename (YYYY-MM-DD pattern)
            date_from_name = ""
            m_date = re.search(r"(\d{4}-\d{2}-\d{2})", rel)
            if m_date:
                date_from_name = m_date.group(1)
            event_rows = []
            for ev in event_entries:
                ev_date = ev.get("date", "") or date_from_name
                # Compute end_date for cross-day events
                ev_end_date = ""
                plus_days_str = ev.get("plus_days", "")
                if plus_days_str and ev_date:
                    try:
                        plus_days_int = int(plus_days_str)
                        base = datetime.strptime(ev_date, "%Y-%m-%d").date()
                        ev_end_date = (base + timedelta(days=plus_days_int)).isoformat()
                    except (ValueError, TypeError):
                        pass
                is_all_day = 1 if ev.get("all_day") == "1" else 0
                event_rows.append((
                    rel,
                    ev_date,
                    ev.get("time", ""),
                    ev.get("end_time", ""),
                    ev.get("title", ""),
                    ev.get("location", ""),
                    ev.get("description", ""),
                    ev_end_date,
                    is_all_day,
                ))
            c.executemany(
                "INSERT INTO events "
                "(note_path, date, time, end_time, title, location, description, end_date, all_day) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                event_rows,
            )

        # -- FTS
        # Strip frontmatter delimiters from the body for cleaner search
        fts_body = body.strip()
        c.execute(
            "INSERT INTO fts (path, title, body) VALUES (?, ?, ?)",
            (rel, title, fts_body),
        )

    def _remove_file(self, rel_path: str):
        """Remove a file from the index (cascading deletes handle child rows)."""
        c = self.conn
        c.execute("DELETE FROM fts WHERE path = ?", (rel_path,))
        c.execute("DELETE FROM note_access WHERE path = ?", (rel_path,))
        c.execute("DELETE FROM revisions WHERE path = ?", (rel_path,))
        c.execute("DELETE FROM files WHERE path = ?", (rel_path,))

    # -- bulk sync ------------------------------------------------------------

    def sync(self, force: bool = False) -> dict[str, int]:
        """
        Synchronize the database with the vault filesystem.

        Uses mtime_ns for incremental updates.  Pass ``force=True`` to
        re-index every file regardless of mtime.

        Returns a summary dict: {scanned, added, updated, removed, unchanged, errors}.
        """
        c = self.conn
        root = self._root
        stats = {"scanned": 0, "added": 0, "updated": 0, "removed": 0, "unchanged": 0, "errors": 0}

        # Build set of currently-on-disk files (NFC-normalized to match _index_file)
        disk_files: dict[str, Path] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if is_ignored_path(path):
                continue
            try:
                ensure_allowed_vault_file(path)
            except ValueError:
                continue
            rel = unicodedata.normalize("NFC", str(path.relative_to(root)).replace("\\", "/"))
            disk_files[rel] = path
            stats["scanned"] += 1

        # Build set of currently-indexed files {path: mtime_ns}
        indexed: dict[str, int] = {}
        for row in c.execute("SELECT path, mtime_ns FROM files").fetchall():
            indexed[row["path"]] = row["mtime_ns"]

        # Detect removed files
        removed = set(indexed.keys()) - set(disk_files.keys())
        for rel in removed:
            self._remove_file(rel)
            stats["removed"] += 1

        # On force rebuild, purge all derived data first so stale entries
        # from previous index runs (or silently-failed _index_file calls)
        # can never survive.  The files/notes tables are kept because
        # _index_file will INSERT OR REPLACE them; removed files were
        # already cleaned above.
        if force:
            for tbl in ("frontmatter", "tags", "aliases", "wikilinks",
                        "tasks", "reminders", "fts"):
                c.execute(f"DELETE FROM {tbl}")

        # Index new and changed files
        batch_count = 0
        for rel, path in disk_files.items():
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                continue

            if not force and rel in indexed and indexed[rel] == mtime_ns:
                stats["unchanged"] += 1
                continue

            try:
                self._index_file(path)
            except Exception as exc:
                # Log but don't crash the whole sync
                import logging
                logging.getLogger("obsidian_memory_mcp").warning(
                    "sync: failed to index %s: %s", rel, exc,
                )
                stats["errors"] += 1
                continue

            if rel in indexed:
                stats["updated"] += 1
            else:
                stats["added"] += 1

            batch_count += 1
            if batch_count % 200 == 0:
                c.commit()

        c.commit()
        return stats

    def sync_file(self, path: Path):
        """Re-index a single file after a write/update.  Commits immediately."""
        if not path.exists():
            rel = unicodedata.normalize("NFC", str(path.relative_to(self._root)).replace("\\", "/"))
            self._remove_file(rel)
            self.conn.commit()
            return
        self._index_file(path)
        self.conn.commit()

    def sync_note_text(self, path: Path, text: str):
        """Re-index a note whose text you already have in memory (avoids re-read)."""
        self._index_file(path, text=text)
        self.conn.commit()

    def remove_path(self, rel_path: str):
        """Remove an entry by relative path and commit."""
        self._remove_file(rel_path)
        self.conn.commit()

    # -- query helpers --------------------------------------------------------

    def search_fts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search.  Returns [{path, title, snippet, rank}]."""
        c = self.conn
        # Escape special FTS5 characters in user query
        safe_q = re.sub(r'["\'\(\)\*\-]', " ", query).strip()
        if not safe_q:
            return []
        # Convert multi-word query to prefix-match tokens
        tokens = safe_q.split()
        fts_query = " ".join(f'"{t}"' for t in tokens if t)
        if not fts_query:
            return []
        try:
            rows = c.execute(
                "SELECT path, title, snippet(fts, 2, '»', '«', '…', 40) AS snippet, "
                "rank FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        results = [dict(r) for r in rows]
        for r in results:
            if r.get("snippet"):
                r["snippet"] = r["snippet"].replace("»", "").replace("«", "")
        return results

    def find_backlinks_db(self, target_path: str, limit: int = 100) -> list[dict[str, str]]:
        """Find notes linking to *target_path* via wikilinks."""
        c = self.conn
        target = normalize_note_target(target_path)
        target_basename = Path(target).name
        rows = c.execute(
            "SELECT source_path, target, display_text FROM wikilinks "
            "WHERE target = ? OR target LIKE ? LIMIT ?",
            (target, f"%/{target_basename}", limit),
        ).fetchall()
        seen: set[str] = set()
        results: list[dict[str, str]] = []
        for r in rows:
            sp = r["source_path"]
            if sp not in seen:
                seen.add(sp)
                results.append({"path": sp, "link": f"[[{r['target']}{'|' + r['display_text'] if r['display_text'] else ''}]]"})
        return results

    def query_frontmatter(
        self, field: str, value: str = "", missing: bool = False,
        folder: str = "", limit: int = 500,
    ) -> list[str]:
        """Query notes by frontmatter field/value.  Returns list of paths."""
        c = self.conn

        if field == "tags":
            tbl, col = "tags", "tag"
        elif field == "aliases":
            tbl, col = "aliases", "alias"
        else:
            tbl, col = None, None

        if tbl:
            if missing:
                sql = f"SELECT n.path FROM notes n WHERE NOT EXISTS (SELECT 1 FROM {tbl} t WHERE t.note_path = n.path)"
                params: list[Any] = []
                if folder:
                    sql += " AND n.path LIKE ?"
                    params.append(f"{folder}/%")
            elif value:
                sql = f"SELECT note_path AS path FROM {tbl} WHERE {col} = ?"
                params = [value]
                if folder:
                    sql += " AND note_path LIKE ?"
                    params.append(f"{folder}/%")
            else:
                sql = f"SELECT DISTINCT note_path AS path FROM {tbl}"
                params = []
                if folder:
                    sql += " WHERE note_path LIKE ?"
                    params.append(f"{folder}/%")
            sql += " ORDER BY path LIMIT ?"
            params.append(limit)
            return [r["path"] for r in c.execute(sql, params).fetchall()]

        if missing:
            sql = (
                "SELECT n.path FROM notes n "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM frontmatter f WHERE f.note_path = n.path AND f.key = ?"
                ")"
            )
            params = [field]
            if folder:
                sql += " AND n.path LIKE ?"
                params.append(f"{folder}/%")
            sql += " ORDER BY n.path LIMIT ?"
            params.append(limit)
            return [r["path"] for r in c.execute(sql, params).fetchall()]

        if value:
            sql = (
                "SELECT f.note_path AS path FROM frontmatter f "
                "WHERE f.key = ? AND f.value = ?"
            )
            params = [field, value]
        else:
            sql = (
                "SELECT DISTINCT f.note_path AS path FROM frontmatter f "
                "WHERE f.key = ?"
            )
            params = [field]

        if folder:
            sql += " AND f.note_path LIKE ?"
            params.append(f"{folder}/%")
        sql += " ORDER BY path LIMIT ?"
        params.append(limit)
        return [r["path"] for r in c.execute(sql, params).fetchall()]

    def list_frontmatter_values_db(
        self, field: str, folder: str = "", limit: int = 500,
    ) -> list[tuple[str, int]]:
        """List unique values for a frontmatter key with counts."""
        c = self.conn
        if field == "tags":
            sql = "SELECT tag AS value, COUNT(*) AS cnt FROM tags"
            params: list[Any] = []
            if folder:
                sql += " WHERE note_path LIKE ?"
                params.append(f"{folder}/%")
            sql += " GROUP BY tag ORDER BY cnt DESC, tag LIMIT ?"
            params.append(limit)
        elif field == "aliases":
            sql = "SELECT alias AS value, COUNT(*) AS cnt FROM aliases"
            params = []
            if folder:
                sql += " WHERE note_path LIKE ?"
                params.append(f"{folder}/%")
            sql += " GROUP BY alias ORDER BY cnt DESC, alias LIMIT ?"
            params.append(limit)
        else:
            sql = (
                "SELECT value, COUNT(*) AS cnt FROM frontmatter "
                "WHERE key = ?"
            )
            params = [field]
            if folder:
                sql += " AND note_path LIKE ?"
                params.append(f"{folder}/%")
            sql += " GROUP BY value ORDER BY cnt DESC, value LIMIT ?"
            params.append(limit)
        return [(r["value"], r["cnt"]) for r in c.execute(sql, params).fetchall()]

    def query_tasks(
        self, checked: bool | None = False, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Query tasks.  checked=None means all, False=open, True=done."""
        c = self.conn
        sql = "SELECT note_path, text, checked, due, priority, done FROM tasks"
        params: list[Any] = []
        if checked is not None:
            sql += " WHERE checked = ?"
            params.append(int(checked))
        sql += " ORDER BY due, note_path LIMIT ?"
        params.append(limit)
        return [dict(r) for r in c.execute(sql, params).fetchall()]

    def query_reminders(
        self, checked: bool | None = False, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Query reminders.  checked=None means all, False=pending, True=done."""
        c = self.conn
        sql = "SELECT note_path, text, checked, remind_on, repeat, done FROM reminders"
        params: list[Any] = []
        if checked is not None:
            sql += " WHERE checked = ?"
            params.append(int(checked))
        sql += " ORDER BY remind_on, note_path LIMIT ?"
        params.append(limit)
        return [dict(r) for r in c.execute(sql, params).fetchall()]

    def query_events(
        self, date_from: str = "", date_to: str = "", limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Query events that overlap with a date range (inclusive).

        An event overlaps the window [date_from, date_to] if:
          - its start date falls within the window, OR
          - it has an end_date and the window falls between start and end.
        """
        c = self.conn
        cols = "note_path, date, time, end_time, title, location, description, end_date, all_day"
        sql = f"SELECT {cols} FROM events"
        clauses: list[str] = []
        params: list[Any] = []
        if date_from and date_to:
            # Event overlaps window if: event.date <= window.end AND max(event.date, event.end_date) >= window.start
            clauses.append(
                "(date <= ? AND (CASE WHEN end_date != '' THEN end_date ELSE date END) >= ?)"
            )
            params.extend([date_to, date_from])
        elif date_from:
            clauses.append(
                "((CASE WHEN end_date != '' THEN end_date ELSE date END) >= ?)"
            )
            params.append(date_from)
        elif date_to:
            clauses.append("date <= ?")
            params.append(date_to)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY date, time, title LIMIT ?"
        params.append(limit)
        return [dict(r) for r in c.execute(sql, params).fetchall()]

    def query_tags(self, tag: str, limit: int = 500) -> list[str]:
        """Find note paths having a specific tag."""
        c = self.conn
        return [
            r["note_path"]
            for r in c.execute(
                "SELECT note_path FROM tags WHERE tag = ? ORDER BY note_path LIMIT ?",
                (tag, limit),
            ).fetchall()
        ]

    def query_aliases(self, alias: str) -> list[str]:
        """Find note paths having a specific alias (case-insensitive)."""
        c = self.conn
        return [
            r["note_path"]
            for r in c.execute(
                "SELECT note_path FROM aliases WHERE alias = ? COLLATE NOCASE",
                (alias,),
            ).fetchall()
        ]

    def find_duplicate_titles_db(self, limit: int = 200) -> dict[str, list[str]]:
        """Find groups of notes sharing the same title."""
        c = self.conn
        rows = c.execute(
            "SELECT title, GROUP_CONCAT(path, '||') AS paths "
            "FROM notes WHERE path NOT LIKE '00_Index/%' "
            "GROUP BY title COLLATE NOCASE HAVING COUNT(*) > 1 "
            "ORDER BY COUNT(*) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {r["title"]: r["paths"].split("||") for r in rows}

    def find_alias_conflicts_db(self, limit: int = 200) -> dict[str, list[str]]:
        """Find aliases shared by multiple notes."""
        c = self.conn
        rows = c.execute(
            "SELECT alias, GROUP_CONCAT(note_path, '||') AS paths "
            "FROM aliases "
            "GROUP BY alias COLLATE NOCASE HAVING COUNT(*) > 1 "
            "ORDER BY COUNT(*) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {r["alias"]: r["paths"].split("||") for r in rows}

    def find_merge_candidates_db(
        self,
        limit: int = 30,
        min_score: int = 15,
        folder: str = "",
        exclude_folders: tuple[str, ...] = ("00_Index", "50_Templates", "40_Attachments"),
    ) -> list[dict[str, Any]]:
        """Find pairs of notes that may be candidates for merging.

        Uses multiple signals from the SQLite index — no file reads needed:
          - Tag overlap (Jaccard similarity)
          - Shared wikilink targets
          - Title word overlap
          - Same type+status metadata
          - FTS term overlap (top terms per note)

        Returns a list of dicts sorted by score descending:
          [{path_a, path_b, score, reasons: [str]}, ...]
        """
        c = self.conn

        # -- 1. Load note metadata from DB --
        notes: dict[str, dict[str, Any]] = {}
        folder_prefix = folder.rstrip("/") + "/" if folder else ""
        for row in c.execute("SELECT path, title, type, status FROM notes").fetchall():
            p = row["path"]
            if any(p.startswith(f"{ef}/") for ef in exclude_folders):
                continue
            if folder_prefix and not p.startswith(folder_prefix):
                continue
            notes[p] = {
                "title": row["title"],
                "type": row["type"],
                "status": row["status"],
                "title_words": set(w.lower() for w in row["title"].split() if len(w) >= 3),
            }

        if len(notes) < 2:
            return []

        # -- 2. Build tag sets per note --
        tags_by_note: dict[str, set[str]] = {p: set() for p in notes}
        for row in c.execute("SELECT note_path, tag FROM tags").fetchall():
            if row["note_path"] in tags_by_note:
                tags_by_note[row["note_path"]].add(row["tag"])

        # -- 3. Build wikilink target sets per note --
        links_by_note: dict[str, set[str]] = {p: set() for p in notes}
        for row in c.execute("SELECT source_path, target FROM wikilinks").fetchall():
            if row["source_path"] in links_by_note:
                links_by_note[row["source_path"]].add(row["target"].lower())

        # -- 4. Build FTS term sets (top N terms per note body) --
        terms_by_note: dict[str, set[str]] = {p: set() for p in notes}
        for path in notes:
            try:
                # Use FTS5 to get the indexed body text snippet and extract terms
                row = c.execute(
                    "SELECT body FROM fts WHERE path = ?", (path,)
                ).fetchone()
                if row and row["body"]:
                    body = row["body"]
                    word_counts: dict[str, int] = {}
                    _stop = {"the", "and", "for", "with", "that", "this", "from",
                             "are", "was", "were", "you", "your", "have", "has",
                             "not", "but", "can", "will", "use", "using", "into",
                             "about", "note", "notes", "file", "files", "also",
                             "when", "which", "there", "their", "been", "more",
                             "than", "each", "other", "some", "would", "should"}
                    for w in re.findall(r"[a-z0-9_-]{3,}", body.lower()):
                        if w not in _stop:
                            word_counts[w] = word_counts.get(w, 0) + 1
                    top_terms = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:40]
                    terms_by_note[path] = {w for w, _ in top_terms}
            except Exception:
                pass

        # -- 5. Build candidate pairs via inverted index (avoids O(n²)) --
        # Only compare notes that share at least one tag, link target,
        # or title word — the vast majority of pairs share nothing.
        candidate_pairs: set[tuple[str, str]] = set()

        def _add_pair(a: str, b: str) -> None:
            if a < b:
                candidate_pairs.add((a, b))
            else:
                candidate_pairs.add((b, a))

        # Invert tags → notes that share a tag
        tag_to_notes: dict[str, list[str]] = {}
        for p, tset in tags_by_note.items():
            for t in tset:
                tag_to_notes.setdefault(t, []).append(p)
        for group in tag_to_notes.values():
            if 2 <= len(group) <= 50:  # skip very common tags (too noisy)
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        _add_pair(group[i], group[j])

        # Invert link targets → notes that link to the same thing
        target_to_notes: dict[str, list[str]] = {}
        for p, lset in links_by_note.items():
            for t in lset:
                target_to_notes.setdefault(t, []).append(p)
        for group in target_to_notes.values():
            if 2 <= len(group) <= 30:
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        _add_pair(group[i], group[j])

        # Invert title words → notes that share a title word
        word_to_notes: dict[str, list[str]] = {}
        for p, meta in notes.items():
            for w in meta["title_words"]:
                word_to_notes.setdefault(w, []).append(p)
        for group in word_to_notes.values():
            if 2 <= len(group) <= 20:
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        _add_pair(group[i], group[j])

        # -- 6. Score only the candidate pairs --
        candidates: list[dict[str, Any]] = []

        for pa, pb in candidate_pairs:
            if pa not in notes or pb not in notes:
                continue
            na, nb = notes[pa], notes[pb]
            score = 0
            reasons: list[str] = []

            # Tag Jaccard
            ta, tb = tags_by_note[pa], tags_by_note[pb]
            if ta and tb:
                overlap = ta & tb
                jaccard = len(overlap) / len(ta | tb)
                if jaccard >= 0.5:
                    tag_score = int(jaccard * 20)
                    score += tag_score
                    reasons.append(f"tags({len(overlap)}/{len(ta | tb)})")

            # Shared wikilink targets
            la, lb = links_by_note[pa], links_by_note[pb]
            if la and lb:
                shared = la & lb
                if len(shared) >= 2:
                    link_score = min(len(shared) * 3, 15)
                    score += link_score
                    reasons.append(f"links({len(shared)})")

            # Title word overlap
            tw_a, tw_b = na["title_words"], nb["title_words"]
            if tw_a and tw_b:
                title_overlap = tw_a & tw_b
                if title_overlap:
                    t_score = min(len(title_overlap) * 6, 18)
                    score += t_score
                    reasons.append(f"title({','.join(sorted(title_overlap))})")

            # Same type bonus (if both are same non-empty type)
            if na["type"] and na["type"] == nb["type"]:
                score += 3
                reasons.append(f"type={na['type']}")

            # FTS term overlap (Jaccard on top terms)
            fts_a, fts_b = terms_by_note[pa], terms_by_note[pb]
            if fts_a and fts_b:
                fts_overlap = fts_a & fts_b
                union_size = len(fts_a | fts_b)
                if union_size > 0:
                    fts_jaccard = len(fts_overlap) / union_size
                    if fts_jaccard >= 0.2:
                        fts_score = int(fts_jaccard * 25)
                        score += fts_score
                        reasons.append(f"content({len(fts_overlap)}/{union_size})")

            if score >= min_score:
                candidates.append({
                    "path_a": pa,
                    "path_b": pb,
                    "score": score,
                    "reasons": reasons,
                })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:limit]

    def find_broken_wikilinks_db(
        self, limit: int = 500, offset: int = 0, folder: str = "",
    ) -> tuple[list[dict[str, str]], int]:
        """Find wikilinks whose target doesn't match any existing note path, basename, or alias.

        Returns (items, total_count) so callers can paginate.
        """
        c = self.conn
        # Build sets of known targets from notes (markdown) …
        existing_targets: set[str] = set()
        existing_basenames: set[str] = set()
        for row in c.execute("SELECT path FROM notes").fetchall():
            target = normalize_note_target(row["path"])
            existing_targets.add(target)
            existing_basenames.add(Path(target).name)
        # … and from all vault files (images, PDFs, etc.)
        for row in c.execute("SELECT path FROM files").fetchall():
            fpath = unicodedata.normalize("NFC", row["path"].strip().replace("\\", "/").strip("/"))
            existing_targets.add(fpath)
            existing_basenames.add(Path(fpath).name)
        # … and from aliases (case-insensitive to match Obsidian behavior)
        existing_aliases: set[str] = set()
        for row in c.execute("SELECT alias FROM aliases").fetchall():
            existing_aliases.add(row["alias"].strip().lower())

        # Optionally restrict to sources under a folder prefix
        if folder:
            folder_prefix = folder.rstrip("/") + "/"
            wikilink_rows = c.execute(
                "SELECT source_path, target, display_text FROM wikilinks "
                "WHERE source_path LIKE ?",
                (f"{folder_prefix}%",),
            ).fetchall()
        else:
            wikilink_rows = c.execute(
                "SELECT source_path, target, display_text FROM wikilinks"
            ).fetchall()

        broken: list[dict[str, str]] = []
        for row in wikilink_rows:
            target = normalize_note_target(row["target"])
            basename = Path(target).name
            if (
                target not in existing_targets
                and basename not in existing_basenames
                and basename.lower() not in existing_aliases
                and target.lower() not in existing_aliases
            ):
                display = row["display_text"]
                raw_link = f"[[{row['target']}{'|' + display if display else ''}]]"
                broken.append({
                    "source": row["source_path"],
                    "link": raw_link,
                    "target": target,
                })
        total = len(broken)
        return broken[offset:offset + limit], total

    def count_notes_missing_field(self, field: str, folder: str = "") -> int:
        """Count notes that do NOT have a particular frontmatter key."""
        c = self.conn
        if field == "tags":
            sub = "SELECT 1 FROM tags t WHERE t.note_path = n.path"
        elif field == "aliases":
            sub = "SELECT 1 FROM aliases a WHERE a.note_path = n.path"
        else:
            sub = "SELECT 1 FROM frontmatter f WHERE f.note_path = n.path AND f.key = ?"
        sql = f"SELECT COUNT(*) AS cnt FROM notes n WHERE NOT EXISTS ({sub})"
        params: list[Any] = [] if field in ("tags", "aliases") else [field]
        if folder:
            sql += " AND n.path LIKE ?"
            params.append(f"{folder}/%")
        row = c.execute(sql, params).fetchone()
        return row["cnt"] if row else 0

    def count_notes(self, folder: str = "") -> int:
        """Count total indexed notes."""
        c = self.conn
        if folder:
            row = c.execute(
                "SELECT COUNT(*) AS cnt FROM notes WHERE path LIKE ?",
                (f"{folder}/%",),
            ).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) AS cnt FROM notes").fetchone()
        return row["cnt"] if row else 0

    def db_stats(self) -> dict[str, int]:
        """Return counts of rows in each table."""
        c = self.conn
        stats: dict[str, int] = {}
        for tbl in ("files", "notes", "frontmatter", "tags", "aliases", "wikilinks",
                    "tasks", "reminders", "events", "note_access", "revisions"):
            try:
                row = c.execute(f"SELECT COUNT(*) AS cnt FROM {tbl}").fetchone()
                stats[tbl] = row["cnt"] if row else 0
            except sqlite3.OperationalError:
                stats[tbl] = 0
        return stats

    # -- access tracking (hotness scoring) ------------------------------------

    def record_access(self, rel_path: str):
        """Increment access counter for a note."""
        c = self.conn
        now = datetime.now().isoformat(timespec="seconds")
        c.execute(
            "INSERT INTO note_access (path, access_count, last_accessed) "
            "VALUES (?, 1, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "access_count = access_count + 1, last_accessed = ?",
            (rel_path, now, now),
        )
        c.commit()

    def get_access_stats(self, rel_path: str) -> dict[str, Any]:
        """Get access count and last accessed time for a note."""
        row = self.conn.execute(
            "SELECT access_count, last_accessed FROM note_access WHERE path = ?",
            (rel_path,),
        ).fetchone()
        if row:
            return {"access_count": row["access_count"], "last_accessed": row["last_accessed"]}
        return {"access_count": 0, "last_accessed": ""}

    def top_accessed(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get most frequently accessed notes."""
        rows = self.conn.execute(
            "SELECT path, access_count, last_accessed FROM note_access "
            "ORDER BY access_count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- vault overview -------------------------------------------------------

    def vault_overview_data(self) -> dict[str, Any]:
        """Compile a compact structural overview of the vault from the DB.

        Returns a dict with:
          - folder_summary: [{folder, note_count, top_tags}]
          - recent_notes: recently modified notes
          - hot_notes: most accessed notes
          - tag_cloud: top tags by usage count
          - type_breakdown: note counts by type
          - stale_active: notes marked active but not modified in 60+ days
          - totals: overall counts
        """
        c = self.conn

        # -- Folder summary with note counts and top tags --
        folder_data: dict[str, dict[str, Any]] = {}
        for row in c.execute("SELECT path, type FROM notes").fetchall():
            parts = row["path"].split("/", 1)
            folder = parts[0] if len(parts) > 1 else "(root)"
            if folder not in folder_data:
                folder_data[folder] = {"count": 0, "types": {}}
            folder_data[folder]["count"] += 1
            t = row["type"] or "(none)"
            folder_data[folder]["types"][t] = folder_data[folder]["types"].get(t, 0) + 1

        # Top tags per folder
        folder_tags: dict[str, dict[str, int]] = {}
        for row in c.execute(
            "SELECT n.path, t.tag FROM notes n JOIN tags t ON n.path = t.note_path"
        ).fetchall():
            folder = row["path"].split("/", 1)[0]
            if folder not in folder_tags:
                folder_tags[folder] = {}
            folder_tags[folder][row["tag"]] = folder_tags[folder].get(row["tag"], 0) + 1

        folder_summary = []
        for f in sorted(folder_data.keys()):
            fd = folder_data[f]
            top_tags_raw = folder_tags.get(f, {})
            top_tags = sorted(top_tags_raw, key=lambda t: top_tags_raw[t], reverse=True)[:5]
            folder_summary.append({
                "folder": f,
                "note_count": fd["count"],
                "types": fd["types"],
                "top_tags": top_tags,
            })

        # -- Recently modified notes (from files table mtime) --
        cutoff_ns = int((datetime.now().timestamp() - 7 * 86400) * 1e9)
        recent = [
            {"path": r["path"], "mtime": datetime.fromtimestamp(r["mtime_ns"] / 1e9).strftime("%Y-%m-%d %H:%M")}
            for r in c.execute(
                "SELECT path, mtime_ns FROM files WHERE suffix = '.md' "
                "AND mtime_ns > ? ORDER BY mtime_ns DESC LIMIT 15",
                (cutoff_ns,),
            ).fetchall()
        ]

        # -- Hot notes (most accessed) --
        hot = self.top_accessed(limit=10)

        # -- Tag cloud (top 25 tags by usage) --
        tag_cloud = [
            {"tag": r["tag"], "count": r["cnt"]}
            for r in c.execute(
                "SELECT tag, COUNT(*) AS cnt FROM tags "
                "GROUP BY tag ORDER BY cnt DESC LIMIT 25"
            ).fetchall()
        ]

        # -- Type breakdown --
        type_breakdown = {
            r["type"] or "(none)": r["cnt"]
            for r in c.execute(
                "SELECT type, COUNT(*) AS cnt FROM notes GROUP BY type ORDER BY cnt DESC"
            ).fetchall()
        }

        # -- Stale active notes (active but not modified in 60+ days) --
        stale_cutoff_ns = int((datetime.now().timestamp() - 60 * 86400) * 1e9)
        stale_rows = c.execute(
            "SELECT n.path, n.title FROM notes n "
            "JOIN files f ON n.path = f.path "
            "WHERE n.status = 'active' AND f.mtime_ns < ? "
            "ORDER BY f.mtime_ns ASC LIMIT 15",
            (stale_cutoff_ns,),
        ).fetchall()
        stale_active = [{"path": r["path"], "title": r["title"]} for r in stale_rows]

        # -- Totals --
        stats = self.db_stats()

        return {
            "folder_summary": folder_summary,
            "recent_notes": recent,
            "hot_notes": hot,
            "tag_cloud": tag_cloud,
            "type_breakdown": type_breakdown,
            "stale_active": stale_active,
            "totals": stats,
        }

    def note_context_data(self, rel_path: str) -> dict[str, Any]:
        """Aggregate a note's full neighborhood from the DB in one call.

        Returns:
          - metadata: title, type, status, tags, aliases, word_count
          - forward_links: notes this note links to
          - backlinks: notes that link to this note
          - tag_siblings: other notes sharing the most tags (top 8)
          - access_stats: access count and last accessed
          - recent_revisions: last 5 revisions (id, saved_at, word_count)
        """
        c = self.conn

        # -- Metadata --
        note_row = c.execute(
            "SELECT title, type, status, word_count FROM notes WHERE path = ?",
            (rel_path,),
        ).fetchone()
        if not note_row:
            return {"error": f"Note not found in index: {rel_path}"}

        tags = [r["tag"] for r in c.execute(
            "SELECT tag FROM tags WHERE note_path = ?", (rel_path,)
        ).fetchall()]
        aliases = [r["alias"] for r in c.execute(
            "SELECT alias FROM aliases WHERE note_path = ?", (rel_path,)
        ).fetchall()]

        metadata = {
            "title": note_row["title"],
            "type": note_row["type"],
            "status": note_row["status"],
            "word_count": note_row["word_count"],
            "tags": tags,
            "aliases": aliases,
        }

        # -- Forward links --
        forward = [
            {"target": r["target"], "display": r["display_text"]}
            for r in c.execute(
                "SELECT target, display_text FROM wikilinks WHERE source_path = ?",
                (rel_path,),
            ).fetchall()
        ]

        # -- Backlinks --
        target = normalize_note_target(rel_path)
        target_basename = Path(target).name
        backlink_rows = c.execute(
            "SELECT DISTINCT source_path FROM wikilinks "
            "WHERE target = ? OR target LIKE ?",
            (target, f"%/{target_basename}"),
        ).fetchall()
        backlinks = [r["source_path"] for r in backlink_rows if r["source_path"] != rel_path]

        # -- Tag siblings (notes sharing the most tags) --
        if tags:
            placeholders = ",".join("?" * len(tags))
            sibling_rows = c.execute(
                f"SELECT note_path, COUNT(*) AS shared "
                f"FROM tags WHERE tag IN ({placeholders}) AND note_path != ? "
                f"GROUP BY note_path ORDER BY shared DESC LIMIT 8",
                (*tags, rel_path),
            ).fetchall()
            tag_siblings = [
                {"path": r["note_path"], "shared_tags": r["shared"]}
                for r in sibling_rows
            ]
        else:
            tag_siblings = []

        # -- Access stats --
        access = self.get_access_stats(rel_path)

        # -- Recent revisions --
        rev_rows = c.execute(
            "SELECT id, saved_at, word_count FROM revisions "
            "WHERE path = ? ORDER BY id DESC LIMIT 5",
            (rel_path,),
        ).fetchall()
        revisions = [dict(r) for r in rev_rows]

        return {
            "metadata": metadata,
            "forward_links": forward,
            "backlinks": backlinks,
            "tag_siblings": tag_siblings,
            "access_stats": access,
            "recent_revisions": revisions,
        }

    def find_cooccurring_tags(self, tag: str, limit: int = 10) -> list[tuple[str, int]]:
        """Find tags that frequently co-occur with the given tag.

        Returns [(co_tag, count)] sorted by count descending.
        """
        c = self.conn
        rows = c.execute(
            "SELECT t2.tag, COUNT(*) AS cnt "
            "FROM tags t1 JOIN tags t2 ON t1.note_path = t2.note_path "
            "WHERE t1.tag = ? AND t2.tag != ? "
            "GROUP BY t2.tag ORDER BY cnt DESC LIMIT ?",
            (tag, tag, limit),
        ).fetchall()
        return [(r["tag"], r["cnt"]) for r in rows]

    # -- revision tracking ----------------------------------------------------

    def save_revision(self, rel_path: str, content: str, content_hash: str = ""):
        """Save a snapshot of note content before it is overwritten."""
        c = self.conn
        if not content_hash:
            content_hash = self._hash_content(content)

        # Skip if content hasn't changed since last revision
        last = c.execute(
            "SELECT content_hash FROM revisions WHERE path = ? ORDER BY id DESC LIMIT 1",
            (rel_path,),
        ).fetchone()
        if last and last["content_hash"] == content_hash:
            return

        now = datetime.now().isoformat(timespec="seconds")
        wc = count_words(content)
        c.execute(
            "INSERT INTO revisions (path, content, content_hash, saved_at, word_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (rel_path, content, content_hash, now, wc),
        )

        # Keep max 20 revisions per note — prune oldest
        c.execute(
            "DELETE FROM revisions WHERE path = ? AND id NOT IN "
            "(SELECT id FROM revisions WHERE path = ? ORDER BY id DESC LIMIT 20)",
            (rel_path, rel_path),
        )
        c.commit()

    def get_revisions(self, rel_path: str, limit: int = 20) -> list[dict[str, Any]]:
        """List revision history for a note (most recent first)."""
        rows = self.conn.execute(
            "SELECT id, path, content_hash, saved_at, word_count FROM revisions "
            "WHERE path = ? ORDER BY id DESC LIMIT ?",
            (rel_path, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_revision_content(self, revision_id: int) -> dict[str, Any] | None:
        """Get the full content of a specific revision."""
        row = self.conn.execute(
            "SELECT id, path, content, content_hash, saved_at, word_count FROM revisions "
            "WHERE id = ?",
            (revision_id,),
        ).fetchone()
        return dict(row) if row else None



# Singleton — initialized lazily on first use
_vault_index: VaultIndex | None = None


def get_vault_index() -> VaultIndex:
    """Get (or create) the singleton VaultIndex, auto-syncing on first call."""
    global _vault_index
    if _vault_index is None:
        _vault_index = VaultIndex(get_vault_root())
        _vault_index.sync()
    return _vault_index


def _notify_index_of_write(path: Path, text: str | None = None):
    """Call after writing/updating a file so the index stays current."""
    if _vault_index is None:
        return  # index not initialized yet, nothing to update
    if text is not None:
        _vault_index.sync_note_text(path, text)
    else:
        _vault_index.sync_file(path)


def _notify_index_of_delete(rel_path: str):
    """Call after deleting a file so the index drops it."""
    if _vault_index is None:
        return
    _vault_index.remove_path(rel_path)




# =============================================================================
# Task / event line parsing helpers (used by VaultIndex.sync + the tool modules)
# =============================================================================
_TASK_BULLET_RE = re.compile(r"^(?P<indent>\s*)- \[(?P<box>[ xX])\]\s+(?P<rest>.*)$")
_KNOWN_META_KEYS = {"due", "priority", "done", "remind_on", "repeat", "id"}


def parse_task_bullet(line: str) -> dict[str, Any] | None:
    m = _TASK_BULLET_RE.match(line.rstrip())
    if not m:
        return None

    rest = m.group("rest")
    parts = [p.strip() for p in rest.split("—")]
    text = parts[0]
    meta: dict[str, str] = {}
    extra: dict[str, str] = {}

    for p in parts[1:]:
        if ":" not in p:
            continue
        key, value = p.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key in _KNOWN_META_KEYS:
            meta[key] = value
        else:
            extra[key] = value

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
        "extra": extra,
        "raw": line,
    }


def format_task_bullet(
    text: str,
    due: str = "",
    priority: str = "",
    done: str = "",
    remind_on: str = "",
    repeat: str = "",
    task_id: str = "",
    extra: dict[str, str] | None = None,
    checked: bool = False,
    indent: str = "",
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
    if extra:
        for k, v in extra.items():
            if v.strip():
                parts.append(f"{k}: {v.strip()}")
    return f"{indent}- {box} " + " — ".join(parts)


def parse_iso_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def find_task_lines_in_section(text: str, section: str) -> list[tuple[int, str, dict[str, Any]]]:
    bounds = find_section_bounds(text, section)
    if bounds is None:
        return []
    start, end = bounds
    section_text = text[start:end]
    results: list[tuple[int, str, dict[str, Any]]] = []
    cursor = start
    for line in section_text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        parsed = parse_task_bullet(stripped)
        if parsed:
            results.append((cursor, stripped, parsed))
        cursor += len(line)
    return results


# -- Schedule / event parsing --------------------------------------------------

# Format: - HH:MM[-HH:MM] Title [@ Location] [— description]
_EVENT_RE = re.compile(
    r"^-\s+"
    r"(?P<time>\d{1,2}:\d{2})"
    r"(?:\s*[-–]\s*(?P<end>\d{1,2}:\d{2})"
    r"(?:\s*\(\+(?P<plus>\d+)d\))?"  # optional (+Nd) cross-day marker
    r")?"
    r"\s+(?P<rest>.+)$"
)

_ALLDAY_RE = re.compile(
    r"^-\s+all[- ]day\s+(?P<rest>.+)$", re.IGNORECASE,
)


def parse_event_line(line: str) -> dict[str, str] | None:
    """Parse a single schedule bullet into an event dict.

    Supported formats:
      - 14:00–16:00 Meeting @ Office — weekly sync
      - 22:00–06:00 (+1d) Flight ZRH→ICN        (cross-day)
      - all-day Conference                        (all-day event)
      - 9:30 Standup
    """
    stripped = line.strip()

    # Try all-day pattern first
    m_ad = _ALLDAY_RE.match(stripped)
    if m_ad:
        rest = m_ad.group("rest")
        location = ""
        description = ""
        if " — " in rest:
            rest, description = rest.split(" — ", 1)
            description = description.strip()
        elif " -- " in rest:
            rest, description = rest.split(" -- ", 1)
            description = description.strip()
        if " @ " in rest:
            rest, location = rest.split(" @ ", 1)
            location = location.strip()
        return {
            "time": "",
            "end_time": "",
            "title": rest.strip(),
            "location": location,
            "description": description,
            "all_day": "1",
            "plus_days": "",
        }

    m = _EVENT_RE.match(stripped)
    if not m:
        return None
    time_str = m.group("time")
    end_str = m.group("end") or ""
    plus_days = m.group("plus") or ""
    rest = m.group("rest")

    # Split off location (@ ...) and description (— ...)
    location = ""
    description = ""
    # Check for — description first
    if " — " in rest:
        rest, description = rest.split(" — ", 1)
        description = description.strip()
    elif " -- " in rest:
        rest, description = rest.split(" -- ", 1)
        description = description.strip()

    # Check for @ location
    if " @ " in rest:
        rest, location = rest.split(" @ ", 1)
        location = location.strip()

    title = rest.strip()
    return {
        "time": time_str,
        "end_time": end_str,
        "title": title,
        "location": location,
        "description": description,
        "all_day": "",
        "plus_days": plus_days,
    }


def parse_schedule_section(text: str) -> list[dict[str, str]]:
    """Extract all events from the ## Schedule section of a note."""
    bounds = find_section_bounds(text, "Schedule")
    if bounds is None:
        return []
    start, end = bounds
    section_text = text[start:end]
    events: list[dict[str, str]] = []
    for line in section_text.splitlines():
        ev = parse_event_line(line)
        if ev:
            events.append(ev)
    return events
