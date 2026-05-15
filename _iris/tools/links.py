"""Health checks (find_issues); Duplicate/conflict detection

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
from .analysis import all_markdown_notes
from .notes import extract_markdown_file_references, resolve_reference_to_existing_file


# ─── from original L3169-3313: Health checks (find_issues) ───
# =============================================================================
# Health checks
# =============================================================================


def note_has_section(text: str, section: str) -> bool:
    return find_section_bounds(text, section) is not None


@mcp.tool()
def find_issues(
    checks: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    max_words: int = 3000,
    folder: str = "",
    include_markdown: bool = False,
    counts_only: bool = False,
) -> str:
    """Scan vault for issues. checks: broken-links|link-mismatches|false-wikilinks|duplicate-basenames|orphan-excalidraw|no-frontmatter|no-related|empty|large|unreferenced. None=all. counts_only for summary counts. Use offset for pagination."""
    ALL_CHECKS = ("broken-links", "link-mismatches", "false-wikilinks", "duplicate-basenames", "orphan-excalidraw", "no-frontmatter", "no-related", "empty", "large", "unreferenced")
    active = [c for c in (checks or ALL_CHECKS) if c in ALL_CHECKS]
    if not active:
        return f"invalid checks. valid: {' '.join(ALL_CHECKS)}"
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    sections: list[str] = []

    if "broken-links" in active:
        idx = get_vault_index()
        broken, broken_total = idx.find_broken_wikilinks_db(
            limit=limit, offset=offset, folder=folder,
        )
        if counts_only:
            sections.append(f"broken-links:{broken_total}")
        else:
            hdr = f"[broken-links:{broken_total}" + (f" showing {offset+1}-{offset+len(broken)}" if offset or len(broken) < broken_total else "") + "]"
            sections.append(hdr + ("" if not broken else "\n" + "\n".join(
                f"{item['source']}|{item['link']}|{item['target']}" for item in broken)))

    if "link-mismatches" in active:
        result = find_link_mismatches(fix=False, limit=limit)
        # Parse the count from the first line
        match = re.match(r"link-mismatches found: (\d+)", result)
        mm_count = int(match.group(1)) if match else 0
        if counts_only:
            sections.append(f"link-mismatches:{mm_count}")
        else:
            sections.append(f"[link-mismatches:{mm_count}]" + ("" if mm_count == 0 else "\n" + "\n".join(result.split("\n")[1:])))

    if "false-wikilinks" in active:
        result = find_false_wikilinks(fix=False, limit=limit)
        match = re.match(r"false-wikilinks found: (\d+)", result)
        fw_count = int(match.group(1)) if match else 0
        if counts_only:
            sections.append(f"false-wikilinks:{fw_count}")
        else:
            sections.append(f"[false-wikilinks:{fw_count}]" + ("" if fw_count == 0 else "\n" + "\n".join(result.split("\n")[1:])))

    if "duplicate-basenames" in active:
        result = find_duplicate_basenames(folder=folder, limit=limit)
        match = re.match(r"duplicate basenames: (\d+)", result)
        db_count = int(match.group(1)) if match else 0
        if counts_only:
            sections.append(f"duplicate-basenames:{db_count}")
        else:
            sections.append(f"[duplicate-basenames:{db_count}]" + ("" if db_count == 0 else "\n" + "\n".join(result.split("\n")[1:])))

    if "orphan-excalidraw" in active:
        result = find_orphan_excalidraw(limit=limit)
        match = re.match(r"orphan Excalidraw files: (\d+)", result)
        oe_count = int(match.group(1)) if match else 0
        if counts_only:
            sections.append(f"orphan-excalidraw:{oe_count}")
        else:
            sections.append(f"[orphan-excalidraw:{oe_count}]" + ("" if oe_count == 0 else "\n" + "\n".join(result.split("\n")[1:])))

    md_checks = set(active) & {"no-frontmatter", "no-related", "empty", "large"}
    if md_checks:
        no_fm: list[str] = []
        no_rel: list[str] = []
        empties: list[str] = []
        large: list[tuple[int, str]] = []
        mw = max(500, min(max_words, 50000))
        folder_prefix = folder.rstrip("/") + "/" if folder else ""
        for path in all_markdown_notes(include_index=False):
            rel = relative_to_vault(path)
            if folder_prefix and not rel.startswith(folder_prefix):
                continue
            text = read_text(path)
            if "no-frontmatter" in md_checks and not note_has_frontmatter(text):
                no_fm.append(rel)
            if "no-related" in md_checks and not rel.startswith("00_Index/") and not note_has_section(text, "Related Notes"):
                no_rel.append(rel)
            if "empty" in md_checks:
                _, body = split_frontmatter(text)
                if len(re.sub(r"\s+", " ", body).strip()) < 40:
                    empties.append(rel)
            if "large" in md_checks:
                words = count_words(text)
                if words > mw:
                    large.append((words, rel))
        large.sort(reverse=True, key=lambda x: x[0])

        for name, items in [("no-frontmatter", no_fm), ("no-related", no_rel), ("empty", empties)]:
            if name not in active:
                continue
            trimmed = items[:limit]
            if counts_only:
                sections.append(f"{name}:{len(items)}")
            else:
                hdr = f"[{name}:{len(items)}]"
                sections.append(hdr + ("" if not trimmed else "\n" + "\n".join(trimmed)))
        if "large" in active:
            trimmed_l = large[:limit]
            if counts_only:
                sections.append(f"large:{len(large)}")
            else:
                hdr = f"[large:{len(large)}]"
                sections.append(hdr + ("" if not trimmed_l else "\n" + "\n".join(
                    f"{rel}|{w}" for w, rel in trimmed_l)))

    if "unreferenced" in active:
        files = all_vault_files(include_binary=True, include_indexable_only=False, folder=folder)
        all_files_by_rel = {relative_to_vault(p): p for p in all_vault_files(include_binary=True, include_indexable_only=False)}
        referenced_rels: set[str] = set()
        for note in all_markdown_notes(include_index=True):
            for ref in extract_markdown_file_references(read_text(note)):
                resolved = resolve_reference_to_existing_file(ref, all_files_by_rel)
                if resolved:
                    referenced_rels.add(relative_to_vault(resolved))
        unreferenced = sorted(
            rel for file in files
            if (rel := relative_to_vault(file)) not in referenced_rels
            and (include_markdown or vault_suffix(file) not in {".md", ".excalidraw.md"})
        )[:limit]
        if counts_only:
            sections.append(f"unreferenced:{len(unreferenced)}")
        else:
            hdr = f"[unreferenced:{len(unreferenced)}]"
            sections.append(hdr + ("" if not unreferenced else "\n" + "\n".join(unreferenced)))

    return "\n".join(sections)



# ─── from original L5375-5539: Duplicate/conflict detection ───
# =============================================================================
# Duplicate and conflict detection
# =============================================================================


# @mcp.tool()  # removed — use sqlite_query instead
def find_duplicate_titles(limit: int = 200) -> str:
    """Find notes with duplicate H1 titles or filename stems."""
    limit = max(1, min(limit, 2000))
    idx = get_vault_index()
    duplicates = idx.find_duplicate_titles_db(limit=limit)

    if not duplicates:
        return "none"
    return "\n".join(f"{title}|{','.join(paths)}" for title, paths in duplicates.items())


# @mcp.tool()  # removed — use sqlite_query instead
def find_alias_conflicts(limit: int = 200) -> str:
    """Find aliases used by multiple notes."""
    limit = max(1, min(limit, 2000))
    idx = get_vault_index()
    conflicts = idx.find_alias_conflicts_db(limit=limit)

    if not conflicts:
        return "none"
    return "\n".join(f"{alias}|{','.join(paths)}" for alias, paths in conflicts.items())


# @mcp.tool()  # removed — use sqlite_query instead
def find_duplicate_basenames(folder: str = "", limit: int = 200) -> str:
    """Find files (including non-Markdown) that share the same basename across
    different folders.  Duplicate basenames cause ambiguous wikilink resolution
    in Obsidian — ``![[Foo]]`` cannot decide which ``Foo.md`` to use when two
    exist.  Returns lines of ``basename|path1,path2,...``."""
    limit = max(1, min(limit, 2000))
    files = all_vault_files(include_binary=True, include_indexable_only=False, folder=folder)
    from collections import defaultdict
    by_basename: dict[str, list[str]] = defaultdict(list)
    for f in files:
        by_basename[f.name.lower()].append(relative_to_vault(f))
    dupes: list[str] = []
    for basename_lower, paths in sorted(by_basename.items()):
        if len(paths) > 1:
            dupes.append(f"{paths[0].rsplit('/', 1)[-1]}|{','.join(sorted(paths))}")
            if len(dupes) >= limit:
                break
    if not dupes:
        return "no duplicate basenames found"
    return f"duplicate basenames: {len(dupes)}\n" + "\n".join(dupes)


# @mcp.tool()  # removed — use sqlite_query instead
def find_orphan_excalidraw(limit: int = 100) -> str:
    """Find Excalidraw drawings not embedded or linked from any content note.

    Handles both legacy ``.excalidraw.md`` files and newer ``.md`` files that
    contain ``excalidraw-plugin: parsed`` in their frontmatter.  Returns a list
    of orphan paths."""
    limit = max(1, min(limit, 500))
    excalidraw_dir = safe_path("40_Attachments/Excalidraw")
    if not excalidraw_dir.exists():
        return "no Excalidraw directory"

    # Gather all excalidraw files
    excalidraw_files: list[tuple[str, str]] = []  # (rel_path, stem)
    for f in excalidraw_dir.iterdir():
        if not f.is_file() or not f.name.endswith(".md"):
            continue
        rel = relative_to_vault(f)
        stem = f.name
        if stem.endswith(".excalidraw.md"):
            stem = stem[: -len(".excalidraw.md")]
        else:
            stem = stem[: -len(".md")]
        excalidraw_files.append((rel, stem))

    if not excalidraw_files:
        return "no Excalidraw files found"

    # Collect all wikilink targets and embeds from content notes
    all_refs: set[str] = set()
    for note_path in all_markdown_notes(include_index=False):
        if relative_to_vault(note_path).startswith("40_Attachments/"):
            continue
        if relative_to_vault(note_path).startswith("90_Inbox/"):
            continue
        text = read_text(note_path)
        for ref in extract_markdown_file_references(text):
            all_refs.add(ref.lower())

    orphans: list[str] = []
    for rel, stem in excalidraw_files:
        stem_lower = stem.lower()
        # Check if stem matches any reference (with or without extension)
        found = False
        for ref in all_refs:
            ref_clean = ref.replace("\\", "/").split("/")[-1]
            ref_no_ext = ref_clean[:-3] if ref_clean.endswith(".md") else ref_clean
            if ref_clean.endswith(".excalidraw.md"):
                ref_no_ext = ref_clean[: -len(".excalidraw.md")]
            elif ref_clean.endswith(".excalidraw"):
                ref_no_ext = ref_clean[: -len(".excalidraw")]
            if stem_lower == ref_no_ext or stem_lower == ref_clean:
                found = True
                break
        if not found:
            orphans.append(rel)
            if len(orphans) >= limit:
                break

    if not orphans:
        return "no orphan Excalidraw files"
    return f"orphan Excalidraw files: {len(orphans)}\n" + "\n".join(orphans)


@mcp.tool()
def resolve_wikilink(target: str) -> str:
    """Resolve a wikilink target to the actual file on disk.

    Use this BEFORE bulk-rewriting links to verify the correct filename.
    Returns the resolved vault-relative path, or an error if not found or
    ambiguous.  Example: ``resolve_wikilink('PTO Kernels')`` →
    ``20_Projects/PTO Kernels.md``."""
    all_files_by_rel = {
        relative_to_vault(p): p  # relative_to_vault already NFC-normalizes
        for p in all_vault_files(include_binary=True, include_indexable_only=False)
    }
    resolved = resolve_reference_to_existing_file(target, all_files_by_rel)
    if resolved:
        return f"resolved: {relative_to_vault(resolved)}"

    # Try case-insensitive basename match
    target_lower = unicodedata.normalize("NFC", target).lower()
    if not target_lower.endswith(".md"):
        target_lower_md = target_lower + ".md"
    else:
        target_lower_md = target_lower
    matches = []
    for rel, path in all_files_by_rel.items():
        basename = Path(rel).name.lower()
        stem = Path(rel).stem.lower()
        if basename == target_lower_md or stem == target_lower:
            matches.append(relative_to_vault(path))
    if len(matches) == 1:
        return f"resolved: {matches[0]}"
    if len(matches) > 1:
        return f"ambiguous ({len(matches)} matches):\n" + "\n".join(sorted(matches))

    # Check aliases
    idx = get_vault_index()
    rows = idx.conn.execute(
        "SELECT note_path FROM aliases WHERE alias = ? COLLATE NOCASE",
        (target,),
    ).fetchall()
    if len(rows) == 1:
        return f"resolved via alias: {rows[0]['note_path']}"
    if len(rows) > 1:
        return f"ambiguous alias ({len(rows)} matches):\n" + "\n".join(
            r["note_path"] for r in rows
        )

    return f"not found: {target}"




@mcp.tool()
def find_link_mismatches(fix: bool = False, limit: int = 200) -> str:
    """
    Find wikilinks that target underscore-delimited names when space-delimited
    files actually exist (e.g. [[Some_Note]] when 'Some Note.md' is the real file).

    Also detects folder-path wikilinks that point to a folder instead of the note
    inside it (e.g. [[60_Knowledge/Spatial_Audio]] instead of [[Spatial Audio]]).

    With fix=True, automatically rewrite the links in-place.
    Returns a summary of mismatches found (and fixed, if fix=True).
    """
    import re
    root = get_vault_root()

    # 1. Build map: stem (no .md) -> list of relative paths
    stem_to_paths: dict[str, list[str]] = {}
    for path in root.rglob("*.md"):
        if is_ignored_path(path):
            continue
        rel = relative_to_vault(path)
        stem = path.stem
        stem_to_paths.setdefault(stem, []).append(rel)

    # 2. Build set of stems with spaces (the canonical names)
    space_stems = {s for s in stem_to_paths if " " in s}

    # 3. Scan all notes for mismatched wikilinks
    wl_pat = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(\|[^\]]+?)?\]\]")

    mismatches: list[dict[str, str]] = []
    files_to_fix: dict[str, list[tuple[str, str]]] = {}  # path -> [(old, new)]

    for path in root.rglob("*.md"):
        if is_ignored_path(path):
            continue
        rel = relative_to_vault(path)
        try:
            content = read_text(path)
        except Exception:
            continue

        for m in wl_pat.finditer(content):
            target = m.group(0)
            target_raw = m.group(1).strip()
            display = m.group(2)  # includes the | if present

            # Get the basename of the target (could be a full path)
            target_base = Path(target_raw).name if "/" in target_raw else target_raw

            # Check: target has underscores and a space version exists?
            if "_" not in target_base:
                continue
            space_version = target_base.replace("_", " ")
            if space_version not in stem_to_paths:
                continue
            if target_base in stem_to_paths:
                continue  # Both exist — not a mismatch, just a real target

            # It's a mismatch. Build the fix.
            if display:
                new_link = f"[[{space_version}{display}]]"
            else:
                new_link = f"[[{space_version}]]"

            mismatches.append({
                "source": rel,
                "old_link": target,
                "new_link": new_link,
            })

            if fix:
                files_to_fix.setdefault(str(path), []).append(
                    (target, new_link)
                )

            if len(mismatches) >= limit:
                break
        if len(mismatches) >= limit:
            break

    # 4. Apply fixes if requested
    fixed_count = 0
    if fix:
        for fpath, replacements in files_to_fix.items():
            p = Path(fpath)
            content = read_text(p)
            for old, new in replacements:
                content = content.replace(old, new)
                fixed_count += 1
            write_text(p, content)

    # 5. Build report
    lines = [f"link-mismatches found: {len(mismatches)}"]
    if fix:
        lines[0] += f" | fixed: {fixed_count}"

    for item in mismatches[:50]:
        lines.append(f"  {item['source']}: {item['old_link']} → {item['new_link']}")
    if len(mismatches) > 50:
        lines.append(f"  ... and {len(mismatches) - 50} more")

    return "\n".join(lines)


@mcp.tool()
def find_false_wikilinks(fix: bool = False, limit: int = 200) -> str:
    """
    Find wikilinks that are actually vector/array notation (e.g. [[1,2,3]] or
    [[[4.2,2.4,3],[-1.1,0]]]) rather than real note links.

    These appear in technical notes containing coordinate data, bounding boxes,
    or matrix representations where nested brackets [[...]] are falsely parsed
    by Obsidian as wikilinks to non-existent notes.

    With fix=True, wraps the full line's array expression in backticks (inline
    code) so Obsidian renders it as code, not a link.

    Excludes:
    - Date-format links like [[2021-10-18]] (intentional daily note links)
    - Matches already inside backtick spans
    - Excalidraw diagram files

    Returns a summary of false wikilinks found (and fixed, if fix=True).
    """
    import re
    root = get_vault_root()

    # Match [[<number-start>...]] that contains commas (vectors/arrays).
    # Commas distinguish vectors [[1,2,3]] from dates [[2021-10-18]].
    false_wl_pat = re.compile(
        r"(\[*\[\[[-0-9][0-9.,\s\-\[\]]*,[-0-9.,\s\-\[\]]*\]\](?:\]*))"
    )

    # Backtick span pattern to check if match falls inside inline code
    backtick_spans: list[tuple[int, int]] = []

    def _build_backtick_spans(text: str) -> list[tuple[int, int]]:
        """Return list of (start, end) for all backtick-delimited spans."""
        spans = []
        i = 0
        while i < len(text):
            if text[i] == '`':
                end = text.find('`', i + 1)
                if end != -1:
                    spans.append((i, end + 1))
                    i = end + 1
                else:
                    break
            else:
                i += 1
        return spans

    def _in_backtick_span(pos: int, spans: list[tuple[int, int]]) -> bool:
        for start, end in spans:
            if start <= pos < end:
                return True
            if start > pos:
                break
        return False

    results: list[dict[str, str]] = []
    files_to_fix: dict[str, list[tuple[str, str]]] = {}

    for path in root.rglob("*.md"):
        if is_ignored_path(path):
            continue
        if ".excalidraw" in path.name:
            continue
        rel = relative_to_vault(path)
        try:
            content = read_text(path)
        except Exception:
            continue

        bt_spans = _build_backtick_spans(content)

        for m in false_wl_pat.finditer(content):
            matched = m.group(0)
            start = m.start()

            # Skip if inside backtick span
            if _in_backtick_span(start, bt_spans):
                continue

            results.append({"source": rel, "match": matched})

            if fix:
                # Find the full array expression on this line and wrap it
                # Get the line containing the match
                line_start = content.rfind('\n', 0, start) + 1
                line_end = content.find('\n', start)
                if line_end == -1:
                    line_end = len(content)
                line = content[line_start:line_end]

                # Find the outermost array on this line (starts with [ before [[)
                # and wrap the whole expression in backticks
                arr_start = line.find('[')
                if arr_start >= 0:
                    # Find matching close — count brackets
                    depth = 0
                    arr_end = arr_start
                    for ci in range(arr_start, len(line)):
                        if line[ci] == '[':
                            depth += 1
                        elif line[ci] == ']':
                            depth -= 1
                            if depth == 0:
                                arr_end = ci + 1
                                break
                    array_expr = line[arr_start:arr_end]
                    if '[[' in array_expr and '`' not in array_expr:
                        files_to_fix.setdefault(str(path), []).append(
                            (array_expr, f"`{array_expr}`")
                        )

            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    fixed_count = 0
    if fix:
        for fpath, replacements in files_to_fix.items():
            p = Path(fpath)
            content = read_text(p)
            for old, new in replacements:
                content = content.replace(old, new)
                fixed_count += 1
            write_text(p, content)

    lines = [f"false-wikilinks found: {len(results)}"]
    if fix:
        lines[0] += f" | fixed: {fixed_count}"

    for item in results[:50]:
        lines.append(f"  {item['source']}: {item['match'][:80]}")
    if len(results) > 50:
        lines.append(f"  ... and {len(results) - 50} more")

    return "\n".join(lines)
