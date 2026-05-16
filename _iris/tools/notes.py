"""Core note read/write/append; Index / routing tools; Advanced vault tools; Frontmatter removal; Frontmatter audit; Templates; Batch operations; Tag management

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
from ..core import _notify_index_of_write, _notify_index_of_delete  # underscore-prefixed names are excluded by `import *` so import them explicitly
from .analysis import all_markdown_notes
from .files import rewrite_links_for_move_in_text, search_vault_files


# ─── from original L1456-1714: Core note read/write/append ───
# =============================================================================
# Core Markdown note tools
# =============================================================================


# @mcp.tool()  # removed — use sqlite_query instead
def search_notes(query: str, limit: int = 10) -> str:
    """Full-text search on Markdown notes."""
    limit = max(1, min(limit, 50))
    idx = get_vault_index()
    results = idx.search_fts(query, limit=limit)

    if not results:
        return f"No notes found for query: {query}"

    return "\n".join(f"{r['path']}|{r['snippet'][:200]}" for r in results)


@mcp.tool()
def read_note(path: str, max_chars: int = 12000) -> str:
    """Read a Markdown note by path."""
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if not note.is_file():
        return f"Path is not a file: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return f"Refusing to read non-Markdown note. Use read_vault_file instead: {path}"

    text = read_text(note)

    # Track access for hotness scoring
    try:
        rel = relative_to_vault(note)
        idx = get_vault_index()
        idx.record_access(rel)
    except Exception:
        pass  # never fail read_note because of access tracking

    max_chars = max(1000, min(max_chars, 50000))
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[TRUNCATED]"
    return text


# @mcp.tool()  # removed — use sqlite_query instead
def list_notes(folder: str = "", limit: int = 100) -> str:
    """
    List Markdown notes under a folder.
    """
    root = get_vault_root()
    target = safe_path(folder)
    if not target.exists():
        return f"Folder not found: {folder}"
    if not target.is_dir():
        return f"Path is not a folder: {folder}"

    notes = []
    for path in target.rglob("*.md"):
        if is_ignored_path(path):
            continue
        notes.append(str(path.relative_to(root)).replace("\\", "/"))

    notes.sort()
    notes = notes[: max(1, min(limit, 500))]
    return "\n".join(notes) if notes else "No Markdown notes found."


def _fix_inline_tags(content: str) -> str:
    """Convert inline YAML tag arrays to multiline format.

    ``tags: [a, b, c]`` → ``tags:\\n  - a\\n  - b\\n  - c``

    This is a vault schema requirement — inline arrays break tag indexing
    and violate the property_schema.  Called automatically by write_note.
    """
    def _replace(m: re.Match) -> str:
        indent = m.group(1)
        raw = m.group(2)
        items = [t.strip().strip("'\"") for t in raw.split(",") if t.strip()]
        if not items:
            return f"{indent}tags:"
        lines = [f"{indent}tags:"]
        for item in items:
            lines.append(f"{indent}  - {item}")
        return "\n".join(lines)

    return re.sub(
        r'^(\s*)tags:\s*\[([^\]]*)\]',
        _replace,
        content,
        count=1,
        flags=re.MULTILINE,
    )


def _check_naming_convention(path: str) -> str | None:
    """Return a warning string if the filename violates naming conventions."""
    p = Path(path)
    stem = p.stem
    parent = str(p.parent)
    # Index files in 00_Index/ may use snake_case
    if "00_Index" in parent or "50_Templates" in parent:
        return None
    # Daily notes are YYYY-MM-DD — skip
    if re.match(r"^\d{4}-\d{2}-\d{2}", stem):
        return None
    # Content files should use Title Case with spaces, not underscores
    if "_" in stem:
        suggested = stem.replace("_", " ").title()
        return (
            f"⚠️ Filename '{stem}.md' uses underscores. "
            f"Content files should use Title Case with spaces: '{suggested}.md'. "
            f"Note was written, but consider renaming with rename_file()."
        )
    return None


# Folders where auto-link-suggestions on new notes don't add value
# (inbox = stubs; daily/weekly = chronological, low concept-density).
_AUTOLINK_SKIP_PREFIXES = ("90_Inbox/", "30_Episodic/")


def _auto_link_suggestions(path: str, content: str) -> str:
    """Best-effort link suggestions for a freshly created note.

    Returns a markdown fragment to append to write_note's result, or ""
    when there's nothing worth surfacing or embeddings aren't reachable.
    Always silent on failure — never breaks the write itself.
    """
    rel = path.replace("\\", "/")
    if any(rel.startswith(p) for p in _AUTOLINK_SKIP_PREFIXES):
        return ""
    try:
        _, body = split_frontmatter(content)
    except Exception:
        body = content
    # Only run for substantive notes — short stubs aren't connectable yet
    if len(body.strip()) < 500:
        return ""
    try:
        from .semantic import rank_link_candidates_from_text
        suggestions = rank_link_candidates_from_text(
            body_text=body,
            self_path=rel,
            top_k=4,
            min_score=0.5,
        )
    except Exception:
        return ""
    if not suggestions:
        return ""
    lines = ["", "Possible links to add (semantic match):"]
    for score, p, title in suggestions:
        link = f"[[{p[:-3]}|{title}]]" if p.endswith(".md") else p
        lines.append(f"  {score:+.2f}  {link}")
    lines.append("(use add_wikilink to insert any you like)")
    return "\n".join(lines)


@mcp.tool()
def write_note(path: str, content: str, overwrite: bool = False) -> str:
    """
    Create or overwrite a Markdown note.

    NAMING: Content files MUST use Title Case with spaces (``User Profile.md``,
    ``PTO Kernels.md``). Never underscores. The tool warns if you get this wrong.

    FRONTMATTER (auto-enforced where possible):
    - Tags MUST use multiline YAML list, not inline arrays.
      Auto-converts ``tags: [a, b]`` → multiline, but prefer writing correctly.
    - Required fields: ``type``, ``status``, ``tags``, ``last_updated`` (YYYY-MM-DD).
    - See property_schema for allowed type/status values and field order.

    WIKILINKS inside content:
    - Never include ``.md`` extension: ``[[User Profile]]`` not ``[[User Profile.md]]``.
    - Use display text: ``[[10_Profile/User Profile|Your Name]]``.

    On creation of a substantive new note (≥500 chars body, not under
    ``90_Inbox/`` or ``30_Episodic/``), Iris also suggests up to 4 semantic
    wikilinks she'd recommend adding. The write itself never fails because
    of suggestion errors — they're best-effort.
    """
    content = _fix_inline_tags(content)
    note = safe_path(path)
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to write non-Markdown note. Use write_vault_text_file instead."
    was_new = not note.exists()
    if not was_new and not overwrite:
        return f"Note already exists, and overwrite=false: {path}"

    # Save revision of existing content before overwrite
    if not was_new and overwrite:
        try:
            old_text = read_text(note)
            rel = relative_to_vault(note)
            idx = get_vault_index()
            idx.save_revision(rel, old_text)
        except Exception:
            pass  # never fail write_note because of revision tracking

    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(content, encoding="utf-8")
    _notify_index_of_write(note, text=content)
    rel_path = relative_to_vault(note)
    result = f"ok {rel_path}"
    warning = _check_naming_convention(path)
    if warning:
        result += f"\n{warning}"
    # Auto-link suggestions on creation of a substantive note
    if was_new:
        suggestions = _auto_link_suggestions(rel_path, content)
        if suggestions:
            result += suggestions
    return result


@mcp.tool()
def append_to_note(path: str, content: str) -> str:
    """
    Append text to a Markdown note.

    IMPORTANT: Before appending, use ``read_note`` to check the existing content.
    Do NOT create duplicate sections (e.g. two ``## Notes`` in a daily note).
    Append content under the existing section instead of recreating it.
    """
    note = safe_path(path)
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to append to non-Markdown note. Use append_to_vault_text_file instead."
    note.parent.mkdir(parents=True, exist_ok=True)
    old = read_text(note) if note.exists() else ""
    separator = "\n\n" if old and not old.endswith("\n\n") else ""
    new_text = old + separator + content
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {relative_to_vault(note)}"


@mcp.tool()
def add_wikilink(
    source_path: str,
    target_path: str,
    display_text: str = "",
    section: str = "Related Notes",
    bidirectional: bool = False,
) -> str:
    """
    Add an Obsidian wikilink from one note to another.
    """
    source = safe_path(source_path)
    target = safe_path(note_target_to_relative_md(target_path))

    if vault_suffix(source) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown source file."
    if vault_suffix(target) not in {".md", ".excalidraw.md"}:
        return "Refusing to link to non-Markdown target file."
    if not source.exists():
        return f"Source note not found: {source_path}"
    if not target.exists():
        return f"Target note not found: {note_target_to_relative_md(target_path)}"

    link = make_wikilink(relative_to_vault(target), display_text)
    bullet = f"- {link}"
    text = read_text(source)

    added = 0
    if link in text:
        pass
    else:
        text = append_bullet_to_section(text, section, bullet)
        source.write_text(text, encoding="utf-8")
        _notify_index_of_write(source, text=text)
        added += 1

    if bidirectional:
        reverse_display = source.stem.replace("_", " ").replace("-", " ").title()
        reverse_link = make_wikilink(relative_to_vault(source), reverse_display)
        reverse_bullet = f"- {reverse_link}"
        target_text = read_text(target)
        if reverse_link not in target_text:
            target_text = append_bullet_to_section(target_text, section, reverse_bullet)
            target.write_text(target_text, encoding="utf-8")
            _notify_index_of_write(target, text=target_text)
            added += 1

    return f"ok added:{added}"


# @mcp.tool()  # removed — use sqlite_query instead
def list_wikilinks(path: str) -> str:
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return f"Refusing to read non-Markdown file: {path}"

    links = extract_wikilinks(read_text(note))
    if not links:
        return f"No wikilinks in {path}"
    return "\n".join(f"{l['target']}|{l['display_text'] or ''}" for l in links)


# @mcp.tool()  # removed — use sqlite_query instead
def find_backlinks(target_path: str, limit: int = 100) -> str:
    limit = max(1, min(limit, 500))
    idx = get_vault_index()
    matches = idx.find_backlinks_db(target_path, limit=limit)

    if not matches:
        return f"No backlinks for {target_path}"
    return "\n".join(m['path'] for m in matches)



# ─── from original L2903-3043: Index / routing tools ───
# =============================================================================
# Index / routing tools
# =============================================================================


INDEX_FILES = {
    "memory": "00_Index/memory_index.md",
    "folder": "00_Index/folder_index.md",
    "concept": "00_Index/concept_index.md",
    "alias": "00_Index/alias_index.md",
    "active_projects": "00_Index/active_projects.md",
    "property_schema": "00_Index/property_schema.md",
    "dashboard": "00_Index/dashboard.md",
    "assistant_rules": "00_Index/assistant_rules.md",
}


def markdown_heading_sections(text: str) -> list[dict[str, Any]]:
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
    matches = list(heading_re.finditer(text))
    sections = []
    for i, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append({"level": level, "heading": heading, "content": text[start:end].strip()})
    return sections


def score_index_section(section: dict[str, Any], terms: list[str]) -> int:
    heading = section.get("heading", "").lower()
    content = section.get("content", "").lower()
    score = 0
    for term in terms:
        t = term.lower().strip()
        if not t:
            continue
        if t in heading:
            score += 50
        score += content.count(t) * 10
        if f"- {t}" in content:
            score += 20
        if f"[[{t}" in content:
            score += 20
    return score


@mcp.tool()
def read_index(index_name: str = "memory", max_chars: int = 20000) -> str:
    key = index_name.strip().lower()
    if key not in INDEX_FILES:
        return f"Unknown index: {index_name}\nValid indexes: {', '.join(sorted(INDEX_FILES.keys()))}"

    path = INDEX_FILES[key]
    note = safe_path(path)
    if not note.exists():
        return f"Index file not found: {path}"

    text = read_text(note)
    max_chars = max(1000, min(max_chars, 50000))
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[TRUNCATED]"
    return text


# @mcp.tool()  # removed — use sqlite_schema instead
def list_indexes() -> str:
    return "\n".join(f"{name}|{path}|{'ok' if safe_path(path).exists() else 'missing'}" for name, path in sorted(INDEX_FILES.items()))


@mcp.tool()
def find_concept(query: str, limit: int = 5) -> str:
    concept_path = safe_path(INDEX_FILES["concept"])
    if not concept_path.exists():
        return "concept_index.md not found."

    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return "Empty query."

    text = read_text(concept_path)
    sections = markdown_heading_sections(text)
    results = []
    for section in sections:
        score = score_index_section(section, terms)
        if score > 0:
            results.append({"score": score, **section})

    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[: max(1, min(limit, 20))]
    if not results:
        return f"No concept match found for query: {query}"

    return "\n".join(
        f"{r['heading']}|{r['score']}|{re.sub(r'\\s+', ' ', r['content']).strip()[:200]}"
        for r in results
    )


@mcp.tool()
def resolve_alias(query: str, limit: int = 10) -> str:
    alias_path = safe_path(INDEX_FILES["alias"])
    if not alias_path.exists():
        return "alias_index.md not found."

    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return "Empty query."

    text = read_text(alias_path)
    scored = []
    q_lower = query.lower().strip()
    for line in text.splitlines():
        clean = line.strip()
        if not clean.startswith("- "):
            continue
        lower = clean.lower()
        score = 100 if q_lower and q_lower in lower else 0
        for term in terms:
            if term.lower() in lower:
                score += 20
        if score > 0:
            scored.append((score, clean))

    scored.sort(key=lambda x: x[0], reverse=True)
    scored = scored[: max(1, min(limit, 50))]
    if not scored:
        return f"No alias match found for query: {query}"

    return "\n".join(line for _, line in scored)


@mcp.tool()
def route_query(query: str, limit: int = 5) -> str:
    parts = [f"[alias] {resolve_alias(query, limit=limit)}"]
    parts.append(f"[concept] {find_concept(query, limit=limit)}")
    parts.append(f"[files] {search_vault_files(query, limit=limit)}")
    return "\n".join(parts)



# ─── from original L3739-4950: Advanced vault tools ───
# =============================================================================
# Iris advanced vault tools
# =============================================================================

NL = chr(10)
BS = chr(92)


def _clean_ref(value: str) -> str:
    out = value.strip().replace(BS, "/")
    out = out.split("#", 1)[0].split("?", 1)[0].strip().strip("/")
    return unicodedata.normalize("NFC", out)


def extract_markdown_file_references(text: str) -> list[str]:
    refs: list[str] = []
    for link in extract_wikilinks(text):
        target = link.get("note_target", "").strip()
        if target:
            refs.append(target)

    # Minimal Markdown link parser for [label](target) and ![alt](target).
    cursor = 0
    while True:
        start = text.find("](", cursor)
        if start < 0:
            break
        target_start = start + 2
        target_end = text.find(")", target_start)
        if target_end < 0:
            break
        target = text[target_start:target_end].strip()
        if target and "://" not in target and not target.startswith("mailto:"):
            refs.append(target)
        cursor = target_end + 1

    return unique_preserve_order([_clean_ref(r) for r in refs if _clean_ref(r)])


def file_ref_matches_path(ref: str, rel_path: str) -> bool:
    ref_norm = _clean_ref(ref)
    rel_norm = _clean_ref(rel_path)
    if not ref_norm:
        return False
    ref_no_md = ref_norm[:-3] if ref_norm.endswith(".md") else ref_norm
    rel_no_md = rel_norm[:-3] if rel_norm.endswith(".md") else rel_norm
    return (
        ref_norm == rel_norm
        or ref_no_md == rel_no_md
        or Path(ref_norm).name == Path(rel_norm).name
        or Path(ref_no_md).name == Path(rel_no_md).name
    )


def resolve_reference_to_existing_file(ref: str, all_files_by_rel: dict[str, Path]) -> Path | None:
    _nfc = lambda s: unicodedata.normalize("NFC", s)
    ref_norm = _clean_ref(ref)  # already NFC via _clean_ref
    if not ref_norm:
        return None

    # NFC-normalize dict keys for comparison (filesystem may store NFD on macOS,
    # but callers using relative_to_vault() already produce NFC keys).
    nfc_lookup: dict[str, Path] = {_nfc(k): v for k, v in all_files_by_rel.items()}

    if ref_norm in nfc_lookup:
        return nfc_lookup[ref_norm]
    if not ref_norm.endswith(".md") and f"{ref_norm}.md" in nfc_lookup:
        return nfc_lookup[f"{ref_norm}.md"]

    basename = Path(ref_norm).name
    basename_no_md = basename[:-3] if basename.endswith(".md") else basename
    matches = []
    for rel, path in nfc_lookup.items():
        rel_base = Path(rel).name
        rel_base_no_md = rel_base[:-3] if rel_base.endswith(".md") else rel_base
        if basename == rel_base or basename_no_md == rel_base_no_md:
            matches.append(path)
    return matches[0] if len(matches) == 1 else None


@mcp.tool()
def inspect_vault_file(path: str, preview_lines: int = 20, max_preview_chars: int = 4000) -> str:
    """
    Inspect one vault file without reading the whole content.
    """
    file = safe_path(path)
    if not file.exists():
        return f"File not found: {path}"
    if not file.is_file():
        return f"Path is not a file: {path}"
    try:
        ensure_allowed_vault_file(file)
    except ValueError as exc:
        return str(exc)

    stat = file.stat()
    suffix = vault_suffix(file)
    info = f"{relative_to_vault(file)}|{suffix}|{stat.st_size}b"
    if suffix in TEXT_INDEXABLE_EXTENSIONS or suffix == ".excalidraw.md":
        text = file.read_text(encoding="utf-8", errors="ignore")
        info += f"|{text.count(NL)+1}L"
        if preview_lines > 0:
            preview = NL.join(text.splitlines()[: max(1, min(preview_lines, 100))])[: max(500, min(max_preview_chars, 20000))]
            info += NL + (preview if preview else "(empty)")
    return info


@mcp.tool()
def rewrite_wikilink_targets(old_target: str, new_target: str, dry_run: bool = True, limit: int = 500) -> str:
    """
    Rewrite wikilinks across Markdown notes without moving files.
    """
    root = get_vault_root()
    old_norm = normalize_note_target(old_target)
    new_norm = normalize_note_target(new_target)
    changed: list[str] = []
    for note in root.rglob("*.md"):
        if is_ignored_path(note):
            continue
        text = read_text(note)
        new_text = rewrite_links_for_move_in_text(text, old_norm, new_norm)
        if new_text != text:
            changed.append(relative_to_vault(note))
            if not dry_run:
                note.write_text(new_text, encoding="utf-8")
                _notify_index_of_write(note, text=new_text)
        if len(changed) >= max(1, min(limit, 5000)):
            break

    return f"{'dry-run' if dry_run else 'ok'} {old_norm}->{new_norm} changed:{len(changed)}"


@mcp.tool()
def find_file_references(path: str, limit: int = 100) -> str:
    """Find notes referencing a given file."""
    target = safe_path(path)
    if not target.exists():
        return f"File not found: {path}"
    if not target.is_file():
        return f"Path is not a file: {path}"
    target_rel = relative_to_vault(target)
    matches: list[dict[str, str]] = []
    for note in all_markdown_notes(include_index=True):
        text = read_text(note)
        refs = extract_markdown_file_references(text)
        hit_refs = [ref for ref in refs if file_ref_matches_path(ref, target_rel)]
        if hit_refs:
            matches.append({"note": relative_to_vault(note), "refs": ", ".join(hit_refs[:10])})
        if len(matches) >= max(1, min(limit, 1000)):
            break
    if not matches:
        return "none"
    return "\n".join(item['note'] for item in matches)


# backup_vault_file and trash_vault_file have been consolidated into
# backup_files(path/paths) and delete_files(trash=True).


# @mcp.tool()  # removed — use sqlite_query on files table instead
def search_by_filename(query: str, folder: str = "", limit: int = 50) -> str:
    """
    Search only file and folder names, not file contents.
    """
    terms = [t.lower() for t in query.strip().split() if t.strip()]
    if not terms:
        return "Empty query."
    matches = []
    for path in all_vault_files(include_binary=True, include_indexable_only=False, folder=folder):
        rel = relative_to_vault(path)
        if all(t in rel.lower() for t in terms):
            matches.append(rel)
    matches.sort()
    matches = matches[: max(1, min(limit, 1000))]
    if not matches:
        return "none"
    return "\n".join(matches)


@mcp.tool()
def find_recent_files(days: int = 7, folder: str = "", limit: int = 100) -> str:
    """
    List recently modified vault files, including non-Markdown files.
    """
    days = max(0, min(days, 3650))
    cutoff = datetime.now().timestamp() - days * 86400
    matches = []
    for path in all_vault_files(include_binary=True, include_indexable_only=False, folder=folder):
        mtime = path.stat().st_mtime
        if mtime >= cutoff:
            matches.append((mtime, path))
    matches.sort(reverse=True, key=lambda x: x[0])
    matches = matches[: max(1, min(limit, 1000))]
    if not matches:
        return "none"
    return "\n".join(relative_to_vault(path) for _, path in matches)


def extract_keywords_for_similarity(text: str, max_terms: int = 80) -> set[str]:
    stop = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "you", "your", "have", "has", "not", "but", "can", "will", "use", "using", "into", "about", "note", "notes", "file", "files"}
    cleaned = []
    for ch in text.lower():
        cleaned.append(ch if ch.isalnum() or ch in {"_", "-"} else " ")
    counts: dict[str, int] = {}
    for word in "".join(cleaned).split():
        if len(word) < 3 or word in stop:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return {w for w, _ in ranked[:max_terms]}


@mcp.tool()
def find_similar_notes(path: str, limit: int = 10) -> str:
    """Find notes similar to a given note."""
    source = safe_path(path)
    if not source.exists():
        return f"Note not found: {path}"
    if vault_suffix(source) not in {".md", ".excalidraw.md"}:
        return "Can only find similar Markdown notes."
    source_text = read_text(source)
    source_data, _ = split_frontmatter(source_text)
    source_terms = extract_keywords_for_similarity(source_text)
    tags_raw = source_data.get("tags", [])
    aliases_raw = source_data.get("aliases", [])
    source_tags = set(str(t).lower() for t in tags_raw) if isinstance(tags_raw, list) else set()
    source_aliases = set(str(a).lower() for a in aliases_raw) if isinstance(aliases_raw, list) else set()
    source_links = set(normalize_note_target(l["note_target"]).lower() for l in extract_wikilinks(source_text))
    source_title = title_from_text(source_text, source.stem).lower()
    results = []
    for note in all_markdown_notes(include_index=False):
        if note.resolve() == source.resolve():
            continue
        text = read_text(note)
        data, _ = split_frontmatter(text)
        terms = extract_keywords_for_similarity(text)
        note_tags_raw = data.get("tags", [])
        note_aliases_raw = data.get("aliases", [])
        tags = set(str(t).lower() for t in note_tags_raw) if isinstance(note_tags_raw, list) else set()
        aliases = set(str(a).lower() for a in note_aliases_raw) if isinstance(note_aliases_raw, list) else set()
        links = set(normalize_note_target(l["note_target"]).lower() for l in extract_wikilinks(text))
        title = title_from_text(text, note.stem).lower()
        score = len(source_terms & terms) + len(source_tags & tags) * 10 + len(source_aliases & aliases) * 10 + len(source_links & links) * 5
        for word in source_title.split():
            if len(word) >= 3 and word in title:
                score += 8
        if score > 0:
            results.append((score, relative_to_vault(note), title_from_text(text, note.stem)))
    results.sort(reverse=True, key=lambda x: x[0])
    results = results[: max(1, min(limit, 50))]
    if not results:
        return "none"
    return "\n".join(f"{rel}|{score}" for score, rel, _ in results)


@mcp.tool()
def find_link_candidates(path: str, limit: int = 20) -> str:
    """For one note, find DB-indexed notes that look like cross-link candidates: notes that mention this note's title/aliases but don't currently link to it, AND notes this one mentions/shares-tags with but doesn't link to. Pure SQLite — no file reads. Returns missing_inbound|<path>|<why> and missing_outbound|<path>|<why> lines."""
    note = safe_path(path)
    rel = relative_to_vault(note)

    idx = get_vault_index()
    c = idx.conn
    row = c.execute("SELECT path, title FROM notes WHERE path = ?", (rel,)).fetchone()
    if not row:
        return f"err: note not indexed: {rel}"
    title = row["title"] or note.stem
    aliases = [a["alias"] for a in c.execute("SELECT alias FROM aliases WHERE note_path = ?", (rel,)).fetchall()]
    basename = Path(rel).stem
    # Min 5 chars to avoid matches on generic short aliases like "Home"
    names = {n.strip() for n in [title, basename] + list(aliases) if n.strip() and len(n.strip()) >= 5}

    target_norm = normalize_note_target(rel)
    target_basename = Path(target_norm).name
    # Notes already linking TO this note (any link form: full path, basename, alias)
    inbound_sources: set[str] = set()
    for r in c.execute("SELECT source_path, target FROM wikilinks").fetchall():
        t = normalize_note_target(r["target"])
        if t == target_norm or Path(t).name == target_basename or t.lower() in {a.lower() for a in aliases}:
            inbound_sources.add(r["source_path"])
    # Notes this note links TO
    outbound_targets: set[str] = set()
    for r in c.execute("SELECT target FROM wikilinks WHERE source_path = ?", (rel,)).fetchall():
        outbound_targets.add(normalize_note_target(r["target"]).lower())

    # Missing INBOUND: notes whose FTS body contains this note's title/aliases but which don't link here
    candidates_inbound: list[tuple[str, str]] = []
    seen_in: set[str] = set()
    for name in names:
        if len(candidates_inbound) >= limit:
            break
        try:
            fts_q = f'"{name}"'
            hits = c.execute(
                "SELECT path, snippet(fts, 2, '«', '»', '…', 16) AS snip "
                "FROM fts WHERE fts MATCH ? LIMIT ?",
                (fts_q, limit * 3),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for h in hits:
            p = h["path"]
            if p == rel or p in inbound_sources or p in seen_in:
                continue
            seen_in.add(p)
            snip = (h["snip"] or "").replace("«", "").replace("»", "")[:80].replace("\n", " ")
            candidates_inbound.append((p, f"mentions \"{name}\": {snip}"))
            if len(candidates_inbound) >= limit:
                break

    # Missing OUTBOUND via shared tags (cheap signal — same tag = related topic)
    my_tags = [t["tag"] for t in c.execute("SELECT tag FROM tags WHERE note_path = ?", (rel,)).fetchall()]
    candidates_outbound: list[tuple[str, str]] = []
    if my_tags:
        seen_out: set[str] = set()
        placeholders = ",".join("?" * len(my_tags))
        sql = (
            f"SELECT DISTINCT t.note_path AS p, GROUP_CONCAT(t.tag, ',') AS shared "
            f"FROM tags t WHERE t.tag IN ({placeholders}) AND t.note_path != ? "
            f"GROUP BY t.note_path"
        )
        for r in c.execute(sql, [*my_tags, rel]).fetchall():
            p = r["p"]
            p_norm = normalize_note_target(p).lower()
            p_base = Path(p).stem.lower()
            if p_norm in outbound_targets or p_base in outbound_targets:
                continue
            if p in seen_out:
                continue
            seen_out.add(p)
            candidates_outbound.append((p, f"shares tags: {r['shared']}"))
            if len(candidates_outbound) >= limit:
                break

    out: list[str] = [f"[inbound:{len(candidates_inbound)}|outbound:{len(candidates_outbound)}]"]
    for p, why in candidates_inbound[:limit]:
        out.append(f"missing_inbound|{p}|{why}")
    for p, why in candidates_outbound[:limit]:
        out.append(f"missing_outbound|{p}|{why}")
    if len(out) == 1:
        return "none"
    return "\n".join(out)


@mcp.tool()
def find_hub_link_gaps(limit: int = 200) -> str:
    """Vault-wide scan: for every hub note (type=hub), list notes that mention the hub's title/aliases but don't link to it. Pure SQLite. Returns hub|missing_note|name_matched per line."""
    idx = get_vault_index()
    c = idx.conn
    hubs = c.execute("SELECT path, title FROM notes WHERE type = 'hub'").fetchall()
    if not hubs:
        return "none (no hub notes found)"

    out: list[str] = []
    for hub in hubs:
        hub_path = hub["path"]
        hub_title = (hub["title"] or "").strip()
        hub_basename = Path(hub_path).stem
        aliases = [a["alias"] for a in c.execute("SELECT alias FROM aliases WHERE note_path = ?", (hub_path,)).fetchall()]
        # Min 5 chars to filter generic aliases like "Home"
        names = {n for n in [hub_title, hub_basename] + aliases if n and len(n.strip()) >= 5}
        target_norm = normalize_note_target(hub_path)
        target_basename = Path(target_norm).name
        # Existing inbound sources
        inbound = set()
        for r in c.execute("SELECT source_path, target FROM wikilinks").fetchall():
            t = normalize_note_target(r["target"])
            if t == target_norm or Path(t).name == target_basename or t.lower() in {a.lower() for a in aliases}:
                inbound.add(r["source_path"])
        for name in names:
            try:
                hits = c.execute(
                    "SELECT path FROM fts WHERE fts MATCH ? LIMIT 50",
                    (f'"{name}"',),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for h in hits:
                p = h["path"]
                if p == hub_path or p in inbound:
                    continue
                # Only report if the candidate is itself indexed as a note (skip non-md hits via files-only)
                if c.execute("SELECT 1 FROM notes WHERE path = ?", (p,)).fetchone() is None:
                    continue
                out.append(f"{hub_path}|{p}|{name}")
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    if not out:
        return "none — every hub appears to be reached by all notes mentioning it"
    return f"[gaps:{len(out)}]\n" + "\n".join(out)


@mcp.tool()
def link_inline_mentions(
    target_path: str,
    first_only: bool = True,
    dry_run: bool = True,
    source_paths: list[str] | None = None,
) -> str:
    """For a freshly created or existing note, find inline mentions of its title/aliases in OTHER notes and wrap them in wikilinks. Skips notes that already link to the target, code blocks, frontmatter, and text that's already inside a wikilink. By default touches only the first occurrence per source note (so paragraphs aren't over-linked). dry_run=True previews; set False to write. Returns updated|path|name|context per line."""
    target = safe_path(target_path)
    target_rel = relative_to_vault(target)
    if not target.exists() or vault_suffix(target) != ".md":
        return f"err: target note not found or not markdown: {target_rel}"

    idx = get_vault_index()
    c = idx.conn

    row = c.execute("SELECT title FROM notes WHERE path = ?", (target_rel,)).fetchone()
    if not row:
        return f"err: target not indexed: {target_rel}"
    title = (row["title"] or target.stem).strip()
    aliases = [a["alias"] for a in c.execute(
        "SELECT alias FROM aliases WHERE note_path = ?", (target_rel,)
    ).fetchall()]
    # Names to look for: title + aliases (≥3 chars to avoid noise)
    names = sorted({n for n in [title, target.stem] + aliases if n and len(n.strip()) >= 3},
                   key=len, reverse=True)  # longest first so "GPQA Diamond" beats "GPQA"
    if not names:
        return "err: target has no usable title or aliases"

    target_basename = Path(target_rel).stem

    # Build the set of source paths to scan
    if source_paths:
        candidate_paths = [p for p in source_paths if p != target_rel]
    else:
        # Use find_link_candidates' logic: notes that mention this one but don't link
        target_norm = normalize_note_target(target_rel)
        target_b = Path(target_norm).name
        # Notes already linking to target
        already_linked: set[str] = set()
        for r in c.execute("SELECT source_path, target FROM wikilinks").fetchall():
            t = normalize_note_target(r["target"])
            if t == target_norm or Path(t).name == target_b or t.lower() in {a.lower() for a in aliases}:
                already_linked.add(r["source_path"])
        # FTS search for the names
        candidates: set[str] = set()
        for name in names:
            try:
                hits = c.execute(
                    "SELECT path FROM fts WHERE fts MATCH ? LIMIT 200",
                    (f'"{name}"',),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for h in hits:
                p = h["path"]
                if p == target_rel or p in already_linked:
                    continue
                candidates.add(p)
        candidate_paths = sorted(candidates)

    out: list[str] = []
    updated_count = 0
    skipped_count = 0

    # Patterns to skip: inside fenced code, frontmatter block, inside existing wikilinks/inline links
    def _skip_spans(text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        # Frontmatter
        m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
        if m:
            spans.append((m.start(), m.end()))
        # Fenced code blocks ```
        for m in re.finditer(r"```.*?```", text, re.DOTALL):
            spans.append((m.start(), m.end()))
        # Inline code
        for m in re.finditer(r"`[^`\n]+`", text):
            spans.append((m.start(), m.end()))
        # Wikilinks
        for m in re.finditer(r"\[\[[^\]]+\]\]", text):
            spans.append((m.start(), m.end()))
        # Markdown links
        for m in re.finditer(r"\[[^\]]*\]\([^)]+\)", text):
            spans.append((m.start(), m.end()))
        return spans

    def _in_skip(pos: int, spans: list[tuple[int, int]]) -> bool:
        return any(s <= pos < e for s, e in spans)

    for sp in candidate_paths:
        source = safe_path(sp)
        if not source.exists():
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        spans = _skip_spans(text)
        new_text = text
        offset_delta = 0
        per_file_updates = 0
        for name in names:
            # Word-boundary, case-sensitive (avoid matching "gpqa" inside "magpqa")
            # Use a fresh search on the current new_text
            pattern = r"\b" + re.escape(name) + r"\b"
            for m in re.finditer(pattern, new_text):
                # Recompute skip spans on new_text once at the start; we use stale spans
                # but adjust by offset_delta. Cheaper: just recompute spans each loop.
                cur_spans = _skip_spans(new_text)
                if _in_skip(m.start(), cur_spans):
                    continue
                # Wrap in wikilink
                if target_basename == name:
                    replacement = f"[[{name}]]"
                else:
                    replacement = f"[[{target_basename}|{name}]]"
                new_text = new_text[:m.start()] + replacement + new_text[m.end():]
                # Capture context (~60 chars around)
                ctx_start = max(0, m.start() - 30)
                ctx_end = min(len(new_text), m.start() + len(replacement) + 30)
                context = new_text[ctx_start:ctx_end].replace("\n", " ")
                out.append(f"updated|{sp}|{name}|…{context}…")
                per_file_updates += 1
                if first_only:
                    break
            if first_only and per_file_updates:
                break
        if per_file_updates:
            updated_count += 1
            if not dry_run:
                source.write_text(new_text, encoding="utf-8")
                _notify_index_of_write(source)
        else:
            skipped_count += 1

    prefix = "dry-run " if dry_run else "ok "
    header = f"{prefix}target:{target_rel} scanned:{len(candidate_paths)} updated:{updated_count} no_match:{skipped_count}"
    return header + ("\n" + "\n".join(out) if out else "")


@mcp.tool()
def find_merge_candidates(
    folder: str = "",
    limit: int = 30,
    min_score: int = 15,
) -> str:
    """Find pairs of notes across the vault that might be candidates for merging.

    Compares all notes using SQLite-indexed signals (no file reads):
      - Tag overlap (Jaccard similarity)
      - Shared wikilink targets
      - Title word overlap
      - Matching note type
      - FTS body content similarity

    Use ``folder`` to scope to a specific folder (e.g. "60_Knowledge").
    Use ``min_score`` to tune sensitivity (lower = more results, noisier).
    Default 15 is a good starting point.

    Returns ranked pairs with a score and human-readable reasons.
    Use ``read_note`` on promising pairs to decide whether to merge.
    """
    idx = get_vault_index()
    candidates = idx.find_merge_candidates_db(
        limit=limit, min_score=min_score, folder=folder,
    )
    if not candidates:
        return "No merge candidates found above the score threshold."
    lines = [f"merge-candidates:{len(candidates)}"]
    for c in candidates:
        reasons = ", ".join(c["reasons"])
        lines.append(f"{c['score']}|{c['path_a']}|{c['path_b']}|{reasons}")
    return "\n".join(lines)


@mcp.tool()
def vault_overview() -> str:
    """Get a compact structural overview of the entire vault.

    Returns folder tree with note counts and top tags, recently modified notes,
    most accessed (hot) notes, tag cloud, type breakdown, and stale active notes.

    Call this at session start to understand the vault's shape, or when you need
    to decide where to look for information. Much faster and more complete than
    reading memory_index.md.
    """
    idx = get_vault_index()
    data = idx.vault_overview_data()
    lines: list[str] = []

    # Folder tree
    lines.append("[folders]")
    for f in data["folder_summary"]:
        tags_str = ", ".join(f["top_tags"]) if f["top_tags"] else ""
        lines.append(f"{f['folder']}|{f['note_count']}|{tags_str}")

    # Type breakdown
    lines.append(f"\n[types]")
    for t, cnt in data["type_breakdown"].items():
        lines.append(f"{t}:{cnt}")

    # Tag cloud
    lines.append(f"\n[top-tags]")
    lines.append(", ".join(f"{t['tag']}({t['count']})" for t in data["tag_cloud"]))

    # Recent notes
    if data["recent_notes"]:
        lines.append(f"\n[recent:{len(data['recent_notes'])}]")
        for n in data["recent_notes"]:
            lines.append(f"{n['mtime']}|{n['path']}")

    # Hot notes
    if data["hot_notes"]:
        lines.append(f"\n[hot:{len(data['hot_notes'])}]")
        for n in data["hot_notes"]:
            lines.append(f"{n['path']}|reads:{n['access_count']}")

    # Stale active
    if data["stale_active"]:
        lines.append(f"\n[stale-active:{len(data['stale_active'])}]")
        for n in data["stale_active"]:
            lines.append(f"{n['path']}|{n['title']}")

    # Totals
    t = data["totals"]
    lines.append(f"\n[totals] notes:{t.get('notes', 0)} files:{t.get('files', 0)} "
                 f"tags:{t.get('tags', 0)} wikilinks:{t.get('wikilinks', 0)}")

    return "\n".join(lines)


@mcp.tool()
def note_context(path: str) -> str:
    """Get the full neighborhood of a note in one call.

    Returns metadata (title, type, status, tags, aliases, word count),
    forward links, backlinks, tag siblings (notes sharing the most tags),
    access stats, and recent revisions.

    Use this instead of making separate calls to read_note + find_backlinks +
    find_similar_notes + note_history when you need to understand a note's
    place in the vault.
    """
    idx = get_vault_index()
    rel = relative_to_vault(safe_path(path))
    data = idx.note_context_data(rel)

    if "error" in data:
        return data["error"]

    lines: list[str] = []
    m = data["metadata"]
    lines.append(f"[{m['title']}] type={m['type']} status={m['status']} words={m['word_count']}")
    if m["tags"]:
        lines.append(f"tags: {', '.join(m['tags'])}")
    if m["aliases"]:
        lines.append(f"aliases: {', '.join(m['aliases'])}")

    # Access
    a = data["access_stats"]
    if a["access_count"]:
        lines.append(f"reads: {a['access_count']} (last: {a['last_accessed']})")

    # Forward links
    if data["forward_links"]:
        lines.append(f"\n[links-out:{len(data['forward_links'])}]")
        for lnk in data["forward_links"][:15]:
            lines.append(f"→ {lnk['target']}" + (f" ({lnk['display']})" if lnk["display"] else ""))

    # Backlinks
    if data["backlinks"]:
        lines.append(f"\n[backlinks:{len(data['backlinks'])}]")
        for bl in data["backlinks"][:15]:
            lines.append(f"← {bl}")

    # Tag siblings
    if data["tag_siblings"]:
        lines.append(f"\n[tag-siblings:{len(data['tag_siblings'])}]")
        for s in data["tag_siblings"]:
            lines.append(f"{s['path']}|shared:{s['shared_tags']}")

    # Revisions
    if data["recent_revisions"]:
        lines.append(f"\n[revisions:{len(data['recent_revisions'])}]")
        for r in data["recent_revisions"]:
            lines.append(f"rev:{r['id']}|{r['saved_at']}|words:{r['word_count']}")

    return "\n".join(lines)


@mcp.tool()
def extract_excalidraw_text(path: str) -> str:
    """Extract text from .excalidraw or .canvas files."""
    file = safe_path(path)
    if not file.exists():
        return f"File not found: {path}"
    if not file.is_file():
        return f"Path is not a file: {path}"
    suffix = vault_suffix(file)
    if suffix not in {".excalidraw", ".excalidraw.md", ".canvas"}:
        return "This tool only supports .excalidraw, .excalidraw.md, or .canvas files."
    text = read_text(file)
    labels: list[str] = []
    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first:last + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if suffix == ".canvas":
                for node in data.get("nodes", []):
                    for key in ("text", "label"):
                        value = node.get(key)
                        if isinstance(value, str) and value.strip():
                            labels.append(value.strip())
            else:
                for el in data.get("elements", []):
                    if isinstance(el.get("text"), str) and el.get("text", "").strip():
                        labels.append(el["text"].strip())
            break
        except Exception:
            continue
    if not labels:
        marker = '"text"'
        cursor = 0
        while True:
            pos = text.find(marker, cursor)
            if pos < 0:
                break
            colon = text.find(":", pos)
            q1 = text.find('"', colon + 1)
            q2 = text.find('"', q1 + 1)
            if colon >= 0 and q1 >= 0 and q2 > q1:
                labels.append(text[q1 + 1:q2])
                cursor = q2 + 1
            else:
                break
    labels = unique_preserve_order([x for x in labels if x.strip()])
    if not labels:
        return "none"
    return "\n".join(labels)


@mcp.tool()
def list_canvas_files(folder: str = "", limit: int = 100) -> str:
    """
    List Obsidian Canvas files.
    """
    files = [p for p in all_vault_files(include_binary=False, include_indexable_only=True, folder=folder) if vault_suffix(p) == ".canvas"]
    files = files[: max(1, min(limit, 1000))]
    if not files:
        return "none"
    return "\n".join(relative_to_vault(p) for p in files)


@mcp.tool()
def search_canvas(query: str, folder: str = "", limit: int = 10) -> str:
    """
    Search Obsidian Canvas files.
    """
    return search_vault_files(query=query, folder=folder, extensions=[".canvas"], include_binary_metadata=False, limit=limit)


@mcp.tool()
def set_inline_field(path: str, key: str, value: str) -> str:
    """
    Set or add a Dataview-style inline field: key:: value.
    """
    if not SAFE_FRONTMATTER_KEY_RE.match(key):
        return "Invalid inline field key."
    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."
    text = read_text(note)
    prefix = key + "::"
    lines = text.splitlines()
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{key}:: {value}"
            changed = True
            break
    if not changed:
        lines.append(f"{key}:: {value}")
    new_text = NL.join(lines) + NL
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"


@mcp.tool()
def capture_memory(target_path: str, fact: str, source: str = "", confidence: str = "high", date: str = "", section: str = "Facts") -> str:
    """
    Add one durable memory/fact to a canonical note.
    """
    target = safe_path(target_path)
    if not target.exists():
        return f"Target note not found: {target_path}"
    if vault_suffix(target) not in {".md", ".excalidraw.md"}:
        return "Target must be a Markdown note."
    date = date.strip() or today_iso()
    confidence = confidence.strip() or "high"
    line = f"- {date} — {fact.strip()} — confidence: {confidence}"
    if source.strip():
        line += f" — source: {source.strip()}"
    text = read_text(target)
    new_text = append_unique_line_to_section(text, section, line)
    target.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(target, text=new_text)
    return f"ok {target_path}"


@mcp.tool()
def record_conversation_summary(title: str, summary: str, decisions: list[str] = [], tasks: list[str] = [], related_notes: list[str] = [], folder: str = "30_Episodic", overwrite: bool = False) -> str:
    """
    Create a dated episodic note from a conversation summary.
    """
    clean_title = title.strip() or "Conversation Summary"
    slug_chars = [ch.lower() if ch.isalnum() else "_" for ch in clean_title]
    slug = "_".join(part for part in "".join(slug_chars).split("_") if part) or "conversation_summary"
    date = today_iso()
    target_path = f"{folder.rstrip('/')}/{date}_{slug}.md"
    target = safe_path(target_path)
    if target.exists() and not overwrite:
        return f"Summary note already exists and overwrite=false: {target_path}"
    related_lines = []
    for note in related_notes:
        note = str(note).strip()
        if note:
            display = Path(normalize_note_target(note)).name.replace("_", " ").replace("-", " ").title()
            related_lines.append(f"- {make_wikilink(note, display)}")
    if not related_lines:
        related_lines = ["- "]
    body_lines = [f"# {clean_title}", "", "## Summary", "", summary.strip() or "- ", "", "## Decisions", ""]
    body_lines.extend(f"- {d}" for d in decisions if str(d).strip())
    if not any(str(d).strip() for d in decisions):
        body_lines.append("- ")
    body_lines.extend(["", "## Tasks", ""])
    body_lines.extend(f"- [ ] {t}" for t in tasks if str(t).strip())
    if not any(str(t).strip() for t in tasks):
        body_lines.append("- ")
    body_lines.extend(["", "## Related Notes", "", *related_lines, ""])
    data: dict[str, object] = {"type": "episodic_memory", "status": "active", "created": date, "tags": ["conversation-summary", "episodic"]}
    target.parent.mkdir(parents=True, exist_ok=True)
    new_text = dump_frontmatter(data, NL.join(body_lines))
    target.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(target, text=new_text)
    return f"ok {relative_to_vault(target)}"


@mcp.tool()
def mark_note_superseded(old_path: str, new_path: str, reason: str = "", date: str = "") -> str:
    """
    Mark an old/imported note as superseded by a canonical note.
    """
    old_note = safe_path(old_path)
    new_note = safe_path(new_path)
    if not old_note.exists():
        return f"Old note not found: {old_path}"
    if not new_note.exists():
        return f"New/canonical note not found: {new_path}"
    if vault_suffix(old_note) not in {".md", ".excalidraw.md"} or vault_suffix(new_note) not in {".md", ".excalidraw.md"}:
        return "Both old and new notes must be Markdown notes."
    date = date.strip() or today_iso()
    data, body = split_frontmatter(read_text(old_note))
    data["status"] = "superseded"
    data["superseded_by"] = relative_to_vault(new_note)
    data["superseded_at"] = date
    if reason.strip():
        data["superseded_reason"] = reason.strip()
    tags = data.get("tags", [])
    tag_list = [tags] if isinstance(tags, str) else [str(t) for t in tags] if isinstance(tags, list) else []
    data["tags"] = unique_preserve_order(tag_list + ["superseded"])
    notice_link = make_wikilink(relative_to_vault(new_note), title_from_text(read_text(new_note), new_note.stem))
    notice = f"> [!warning] Superseded{NL}> This note was superseded by {notice_link} on {date}."
    if reason.strip():
        notice += f"{NL}> Reason: {reason.strip()}"
    if "This note was superseded by" not in body:
        body = notice + NL + NL + body.lstrip()
    new_text = dump_frontmatter(data, body)
    old_note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(old_note, text=new_text)
    return f"ok {relative_to_vault(old_note)}"


@mcp.tool()
def promote_imported_note(source_path: str, canonical_path: str, summary: str, facts: list[str] = [], decisions: list[str] = [], tasks: list[str] = [], mark_superseded: bool = True) -> str:
    """
    Extract durable content from an imported/raw note into a canonical note.
    """
    source = safe_path(source_path)
    target = safe_path(canonical_path)
    if not source.exists():
        return f"Source note not found: {source_path}"
    if vault_suffix(source) not in {".md", ".excalidraw.md"}:
        return "Source must be a Markdown note."
    if target.exists() and vault_suffix(target) not in {".md", ".excalidraw.md"}:
        return "Canonical target must be a Markdown note."
    source_title = title_from_text(read_text(source), source.stem)
    source_link = make_wikilink(relative_to_vault(source), source_title)
    if not target.exists():
        data: dict[str, object] = {"type": "project_memory", "status": "active", "last_updated": today_iso(), "source_notes": [relative_to_vault(source)], "tags": ["canonical"]}
        body = f"# {Path(canonical_path).stem.replace('_', ' ').replace('-', ' ').title()}" + NL + NL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_frontmatter(data, body), encoding="utf-8")
    text = read_text(target)
    if summary.strip():
        text = append_unique_line_to_section(text, "Current State", f"- {summary.strip()} — source: {source_link}")
    for fact in facts:
        if str(fact).strip():
            text = append_unique_line_to_section(text, "Facts", f"- {today_iso()} — {str(fact).strip()} — source: {source_link}")
    if decisions:
        header = "| Date | Decision | Reason | Source |"
        sep = "|---|---|---|---|"
        for decision in decisions:
            if str(decision).strip():
                row = f"| {today_iso()} | {escape_table_cell(str(decision))} |  | {escape_table_cell(source_link)} |"
                text = append_table_row_to_section(text, "Decisions", header, sep, row)
    for task in tasks:
        if str(task).strip():
            text = append_bullet_to_section(text, "Tasks", f"- [ ] {str(task).strip()} — source: {source_link}")
    text = append_unique_line_to_section(text, "Source Notes", f"- {source_link}")
    target.write_text(text, encoding="utf-8")
    _notify_index_of_write(target, text=text)
    if mark_superseded:
        mark_note_superseded(relative_to_vault(source), relative_to_vault(target), reason="Promoted")
    return f"ok {relative_to_vault(target)}"


# move_folder has been consolidated into move_files(source="folder", target="new_folder").


@mcp.tool()
def validate_vault_paths(limit: int = 500) -> str:
    """
    Check for path, extension, symlink, duplicate filename, and filename issues.
    """
    root = get_vault_root()
    unsupported: list[str] = []
    symlinks: list[str] = []
    large_files: list[str] = []
    suspicious_names: list[str] = []
    duplicate_map: dict[str, list[str]] = {}
    bad_chars = set('<>:"|?*')
    for path in root.rglob("*"):
        if is_ignored_path(path):
            continue
        rel = relative_to_vault(path)
        if path.is_symlink():
            symlinks.append(rel)
            continue
        if not path.is_file():
            continue
        duplicate_map.setdefault(path.name.lower(), []).append(rel)
        try:
            ensure_allowed_vault_file(path)
        except ValueError:
            unsupported.append(rel)
        if path.stat().st_size > 50 * 1024 * 1024:
            large_files.append(f"{rel} ({path.stat().st_size} bytes)")
        if any(ch in bad_chars for ch in path.name) or path.name.startswith("."):
            suspicious_names.append(rel)
    duplicates = {name: paths for name, paths in duplicate_map.items() if len(paths) > 1}
    return f"unsupported:{len(unsupported)} symlinks:{len(symlinks)} large:{len(large_files)} suspicious:{len(suspicious_names)} duplicates:{len(duplicates)}"



# ─── from original L4951-5027: Frontmatter removal ───
# =============================================================================
# Frontmatter field removal (single + bulk)
# =============================================================================


@mcp.tool()
def remove_frontmatter_field(path: str, field: str) -> str:
    """Remove a frontmatter key from a note."""
    if not field.strip():
        return "field must not be empty."

    note = safe_path(path)
    if not note.exists():
        return f"Note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "Refusing to edit non-Markdown file."

    text = read_text(note)
    data, body = split_frontmatter(text)
    clean_field = field.strip()

    if clean_field not in data:
        return f"Field {clean_field!r} not present in {path}."

    del data[clean_field]
    new_text = dump_frontmatter(data, body)
    note.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(note, text=new_text)
    return f"ok {path}"


@mcp.tool()
def bulk_remove_frontmatter_field(
    field: str,
    folder: str = "",
    paths: list[str] | None = None,
    dry_run: bool = True,
) -> str:
    """Remove a frontmatter key from matching notes. dry_run=True for preview."""
    if not field.strip():
        return "field must not be empty."

    clean_field = field.strip()

    if paths:
        targets = []
        for p in paths:
            sp = safe_path(p)
            if not sp.exists():
                return f"Path not found: {p}"
            if vault_suffix(sp) not in {".md", ".excalidraw.md"}:
                return f"Not a Markdown file: {p}"
            targets.append(sp)
    elif folder.strip():
        base = safe_path(folder)
        if not base.exists() or not base.is_dir():
            return f"Folder not found: {folder}"
        targets = [p for p in base.rglob("*.md") if not is_ignored_path(p)]
    else:
        targets = all_markdown_notes(include_index=False)

    changed: list[str] = []
    for path in targets:
        text = read_text(path)
        data, body = split_frontmatter(text)
        if clean_field not in data:
            continue
        del data[clean_field]
        if not dry_run:
            new_text = dump_frontmatter(data, body)
            path.write_text(new_text, encoding="utf-8")
            _notify_index_of_write(path, text=new_text)
        changed.append(relative_to_vault(path))

    return f"{'dry-run' if dry_run else 'ok'} {clean_field} removed-from:{len(changed)}"



# ─── from original L5092-5232: Frontmatter audit ───
# =============================================================================
# Frontmatter auditing / querying
# =============================================================================


# @mcp.tool()  # removed — use sqlite_query instead
def list_frontmatter_values(field: str, folder: str = "", limit: int = 500) -> str:
    """List unique values of a frontmatter field with counts."""
    if not field.strip():
        return "field must not be empty."

    clean_field = field.strip()
    clean_folder = folder.strip()
    limit = max(1, min(limit, 5000))

    idx = get_vault_index()
    values = idx.list_frontmatter_values_db(clean_field, folder=clean_folder, limit=limit)
    total = idx.count_notes(folder=clean_folder)
    missing_count = idx.count_notes_missing_field(clean_field, folder=clean_folder)

    return "\n".join(f"{value}|{count}" for value, count in values) or "none"


# @mcp.tool()  # removed — use sqlite_query instead
def find_notes_by_frontmatter(
    field: str,
    value: str = "",
    folder: str = "",
    missing: bool = False,
    limit: int = 200,
) -> str:
    """Find notes by frontmatter field value. missing=True for notes lacking the field."""
    if not field.strip():
        return "field must not be empty."

    clean_field = field.strip()
    clean_value = value.strip()
    limit = max(1, min(limit, 5000))

    idx = get_vault_index()
    results = idx.query_frontmatter(
        field=clean_field,
        value=clean_value,
        missing=missing,
        folder=folder.strip(),
        limit=limit,
    )

    if missing:
        desc = f"Notes missing field {clean_field!r}"
    elif clean_value:
        desc = f"Notes where {clean_field}={clean_value!r}"
    else:
        desc = f"Notes with field {clean_field!r}"

    if not results:
        return "none"
    return "\n".join(results)


@mcp.tool()
def validate_frontmatter(
    schema_path: str = "00_Index/property_schema.md",
    folder: str = "",
    limit: int = 200,
) -> str:
    """Validate frontmatter against a schema note (Field|Required|Allowed Values table)."""
    schema_file = safe_path(schema_path)
    if not schema_file.exists():
        return f"Schema note not found: {schema_path}"

    schema_text = read_text(schema_file)
    rules: list[dict[str, Any]] = []

    for line in schema_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped or "Field" in stripped:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        field_name = cells[0].strip()
        required = cells[1].strip().lower() in {"yes", "true", "required", "y"}
        allowed_str = cells[2].strip()
        allowed_values: list[str] = []
        if allowed_str and allowed_str.lower() not in {"any", "*", ""}:
            allowed_values = [v.strip() for v in allowed_str.split(",") if v.strip()]
        rules.append({
            "field": field_name,
            "required": required,
            "allowed": allowed_values,
        })

    if not rules:
        return f"No field rules found in {schema_path}. Expected a table with | Field | Required | Allowed Values |"

    if folder.strip():
        base = safe_path(folder)
        if not base.exists() or not base.is_dir():
            return f"Folder not found: {folder}"
        notes = [p for p in base.rglob("*.md") if not is_ignored_path(p)]
    else:
        notes = all_markdown_notes(include_index=False)

    violations: list[dict[str, Any]] = []

    for path in notes:
        text = read_text(path)
        data, _ = split_frontmatter(text)
        rel = relative_to_vault(path)
        note_issues: list[str] = []

        for rule in rules:
            field = rule["field"]
            has_field = field in data
            val = data.get(field, "")

            if rule["required"] and not has_field:
                note_issues.append(f"missing required field: {field}")
                continue

            if has_field and rule["allowed"]:
                if isinstance(val, list):
                    for v in val:
                        if str(v).strip() not in rule["allowed"]:
                            note_issues.append(f"{field}: value {str(v)!r} not in {rule['allowed']}")
                elif str(val).strip() not in rule["allowed"]:
                    note_issues.append(f"{field}: value {str(val)!r} not in {rule['allowed']}")

        if note_issues:
            violations.append({"path": rel, "issues": note_issues})

    violations = violations[:limit]

    if not violations:
        return f"ok checked:{len(notes)} violations:0"
    return "\n".join(
        f"{v['path']}|{'; '.join(v['issues'])}" for v in violations
    )



# ─── from original L5325-5374: Templates ───
# =============================================================================
# Template-based note creation
# =============================================================================


@mcp.tool()
def create_note_from_template(
    template_path: str,
    target_path: str,
    variables: dict[str, str] = {},
    overwrite: bool = False,
) -> str:
    """Create a note from a template. Replaces {{variable}} placeholders."""
    tpl = safe_path(template_path)
    if not tpl.exists():
        return f"Template not found: {template_path}"
    if vault_suffix(tpl) not in {".md", ".excalidraw.md"}:
        return "Template must be a Markdown file."

    target = safe_path(target_path)
    if vault_suffix(target) not in {".md", ".excalidraw.md"}:
        return "Target must be a Markdown file."
    if target.exists() and not overwrite:
        return f"Target already exists and overwrite=false: {target_path}"

    text = read_text(tpl)

    # Built-in variables
    builtins = {
        "date": today_iso(),
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "target_name": target.stem.replace("_", " ").replace("-", " "),
    }
    all_vars = {**builtins, **{str(k): str(v) for k, v in variables.items()}}

    for key, value in all_vars.items():
        text = text.replace("{{" + key + "}}", value)

    # Check for unreplaced variables
    remaining = re.findall(r"\{\{(\w+)\}\}", text)
    warning = ""
    if remaining:
        warning = f"\nWarning: unreplaced variables: {', '.join(set(remaining))}"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _notify_index_of_write(target, text=text)
    return f"ok {relative_to_vault(target)}{warning}"



# ─── from original L5563-5592: Batch operations ───
# =============================================================================
# Batch operations
# =============================================================================


@mcp.tool()
def batch(ops: list[dict]) -> str:
    """Run multiple tool calls in one request. Each op: {tool: str, args: dict}. Returns idx:result per line."""
    if not ops or len(ops) > 50:
        return "err: 1-50 ops required"
    results: list[str] = []
    tm = mcp._tool_manager
    for i, op in enumerate(ops):
        name = op.get("tool", "")
        args = op.get("args", {})
        if name == "batch":
            results.append(f"{i}:err recursion")
            continue
        tool_obj = tm.get_tool(name)
        if tool_obj is None:
            results.append(f"{i}:err unknown {name}")
            continue
        try:
            result = tool_obj.fn(**args)
            results.append(f"{i}:{result}")
        except Exception as e:
            results.append(f"{i}:err {str(e)[:200]}")
    return "\n".join(results)



# ─── from original L7830-7904: Tag management ───
# =============================================================================
# Tag management
# =============================================================================


@mcp.tool()
def rename_tag(old_tag: str, new_tag: str, dry_run: bool = True) -> str:
    """
    Rename a tag across all notes. Updates frontmatter ``tags`` lists.

    Use ``dry_run=True`` (default) to preview changes before applying.
    """
    old_tag = old_tag.strip().lower()
    new_tag = new_tag.strip().lower()
    if not old_tag or not new_tag:
        return "Both old_tag and new_tag must be non-empty."
    if old_tag == new_tag:
        return "Tags are identical — nothing to do."

    idx = get_vault_index()
    paths = idx.query_tags(old_tag, limit=5000)
    if not paths:
        return f"No notes found with tag '{old_tag}'."

    root = get_vault_root()
    changed: list[str] = []
    errors: list[str] = []

    for rel_path in paths:
        note_path = root / rel_path
        if not note_path.exists():
            errors.append(f"missing: {rel_path}")
            continue
        try:
            text = read_text(note_path)
            data, body = split_frontmatter(text)
            raw_tags = data.get("tags", [])
            if isinstance(raw_tags, str):
                tag_list = [raw_tags]
            elif isinstance(raw_tags, list):
                tag_list = [str(t) for t in raw_tags]
            else:
                continue

            # Replace old tag with new tag
            new_tags: list[str] = []
            found = False
            for t in tag_list:
                if t.strip().lower() == old_tag:
                    if new_tag not in [x.strip().lower() for x in new_tags]:
                        new_tags.append(new_tag)
                    found = True
                else:
                    new_tags.append(t)
            if not found:
                continue

            changed.append(rel_path)
            if not dry_run:
                data["tags"] = new_tags
                new_text = dump_frontmatter(data, body)
                note_path.write_text(new_text, encoding="utf-8")
                _notify_index_of_write(note_path, text=new_text)
        except Exception as exc:
            errors.append(f"error {rel_path}: {exc}")

    status = "dry-run" if dry_run else "ok"
    result = f"{status} renamed:{len(changed)}"
    if errors:
        result += f" errors:{len(errors)}"
    if dry_run and changed:
        result += "\n" + "\n".join(changed[:50])
    return result


