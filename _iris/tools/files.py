"""File CRUD operations; Markdown movement with link rewrite; Bulk replace; Folder cleanup; PDF extraction; Smart move helper

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


# ─── from original L634-1228: File CRUD operations ───
# =============================================================================
# Vault file operations
# =============================================================================


@mcp.tool()
def list_vault_files(
    folder: str = "",
    pattern: str = "*",
    include_binary: bool = True,
    indexable_only: bool = False,
    limit: int = 200,
) -> str:
    """
    List files in the Obsidian vault, including non-Markdown files.
    """
    files = all_vault_files(
        include_binary=include_binary,
        include_indexable_only=indexable_only,
        folder=folder,
    )

    pattern_clean = pattern.strip() or "*"
    matched = []
    for path in files:
        rel = relative_to_vault(path)
        if fnmatch(rel.lower(), pattern_clean.lower()) or fnmatch(path.name.lower(), pattern_clean.lower()):
            matched.append(path)

    matched = matched[: max(1, min(limit, 2000))]

    if not matched:
        return f"No vault files found for folder={folder!r}, pattern={pattern_clean!r}."

    return "\n".join(relative_to_vault(p) for p in matched)


@mcp.tool()
def read_vault_file(path: str, max_chars: int = 50000) -> str:
    """
    Read or summarize a vault file by relative path.
    """
    file = safe_path(path)

    if not file.exists():
        return f"File not found: {path}"
    if not file.is_file():
        return f"Path is not a file: {path}"

    try:
        text = read_indexable_file_text(file, max_chars=max_chars)
    except ValueError as exc:
        return str(exc)

    if len(text) >= max_chars:
        return text[:max_chars] + "\n\n[TRUNCATED]"
    return text


@mcp.tool()
def search_vault_files(
    query: str,
    folder: str = "",
    extensions: list[str] = [],
    include_binary_metadata: bool = True,
    limit: int = 10,
    max_chars_per_file: int = 50000,
) -> str:
    """
    Search across Markdown and non-Markdown vault files.
    """
    root = get_vault_root()
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return "Empty query."

    wanted_exts = {str(e).lower().strip() for e in extensions if str(e).strip()}
    files = all_vault_files(
        include_binary=include_binary_metadata,
        include_indexable_only=not include_binary_metadata,
        folder=folder,
    )

    results: list[dict[str, Any]] = []
    for path in files:
        suffix = vault_suffix(path)
        if wanted_exts and suffix not in wanted_exts:
            continue
        try:
            text = read_indexable_file_text(path, max_chars=max_chars_per_file)
        except Exception:
            text = ""
        score = score_vault_file(path, text, terms, root)
        if score <= 0:
            continue
        results.append(
            {
                "score": score,
                "path": relative_to_vault(path),
                "type": suffix,
                "title": file_title(path, text),
                "snippet": compact_snippet(text, terms, max_chars=700),
                "indexable": is_text_indexable_file(path),
            }
        )

    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[: max(1, min(limit, 50))]

    if not results:
        return f"No vault files found for query: {query}"

    return "\n".join(f"{r['path']}|{r['snippet'][:200]}" for r in results)


@mcp.tool()
def write_vault_text_file(path: str, content: str, overwrite: bool = False) -> str:
    """
    Create or overwrite a text-like vault file.
    """
    file = safe_path(path)
    suffix = vault_suffix(file)

    if suffix != ".excalidraw.md" and suffix not in TEXT_INDEXABLE_EXTENSIONS:
        return f"Refusing to write non-text file type: {suffix or '(no extension)'}"

    try:
        ensure_allowed_vault_file(file)
    except ValueError as exc:
        return str(exc)

    if file.exists() and not overwrite:
        return f"File already exists and overwrite=false: {path}"

    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    _notify_index_of_write(file)
    return f"ok {relative_to_vault(file)}"


@mcp.tool()
def append_to_vault_text_file(path: str, content: str) -> str:
    """
    Append text to a text-like vault file.
    """
    file = safe_path(path)
    suffix = vault_suffix(file)

    if suffix != ".excalidraw.md" and suffix not in TEXT_INDEXABLE_EXTENSIONS:
        return f"Refusing to append to non-text file type: {suffix or '(no extension)'}"

    try:
        ensure_allowed_vault_file(file)
    except ValueError as exc:
        return str(exc)

    file.parent.mkdir(parents=True, exist_ok=True)
    old = file.read_text(encoding="utf-8", errors="ignore") if file.exists() else ""
    separator = "\n" if old and not old.endswith("\n") else ""
    new_text = old + separator + content
    file.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(file)
    return f"ok {relative_to_vault(file)}"


def _resolve_file_list(
    source: str = "",
    sources: list[str] | None = None,
) -> tuple[list[Path], bool, str | None]:
    """
    Resolve source/sources into a list of (file) Paths.

    Returns (paths, is_folder_mode, error).
    - If source is a directory, returns all files inside it and is_folder_mode=True.
    - If sources is a list, validates each and returns them.
    - If source is a single file, returns [file].
    """
    if sources:
        resolved: list[Path] = []
        for p in sources:
            sp = safe_path(p)
            if not sp.exists():
                return [], False, f"Path not found: {p}"
            if sp.is_dir():
                # Collect all files inside the directory
                for child in sp.rglob("*"):
                    if child.is_file() and not is_ignored_path(child):
                        resolved.append(child)
            elif sp.is_file():
                resolved.append(sp)
            else:
                return [], False, f"Path is neither file nor folder: {p}"
        return resolved, False, None

    if not source.strip():
        return [], False, "source or sources must be provided."

    sp = safe_path(source)
    if not sp.exists():
        return [], False, f"Path not found: {source}"

    if sp.is_dir():
        files = [p for p in sp.rglob("*") if p.is_file() and not is_ignored_path(p)]
        return files, True, None

    if sp.is_file():
        return [sp], False, None

    return [], False, f"Path is neither file nor folder: {source}"


def _update_links_for_moves(move_pairs: list[tuple[str, str]]) -> list[str]:
    """
    After moving .md files, rewrite wikilinks across the vault in a single
    pass. Returns a list of note paths that had links updated.
    """
    root = get_vault_root()
    md_pairs = [
        (old, new)
        for old, new in move_pairs
        if old.endswith(".md") or old.endswith(".excalidraw.md")
    ]
    if not md_pairs:
        return []

    changed: list[str] = []
    for note in root.rglob("*.md"):
        if is_ignored_path(note):
            continue
        text = read_text(note)
        new_text = text
        for old_rel, new_rel in md_pairs:
            new_text = rewrite_links_for_move_in_text(new_text, old_rel, new_rel)
        if new_text != text:
            note.write_text(new_text, encoding="utf-8")
            changed.append(relative_to_vault(note))
    return changed


@mcp.tool()
def move_files(
    source: str = "",
    sources: list[str] | None = None,
    target: str = "",
    update_links: bool = True,
    overwrite: bool = False,
    dry_run: bool = False,
) -> str:
    """Move files/folders. Updates wikilinks for .md files."""
    if not target.strip():
        return "target must be provided."

    root = get_vault_root()
    files, is_folder_mode, err = _resolve_file_list(source, sources)
    if err:
        return err
    if not files:
        return "No files found to move."

    target_path = safe_path(target)
    is_multi = len(files) > 1 or is_folder_mode
    source_base = safe_path(source) if source.strip() else None

    if is_multi and source_base and source_base.is_dir():
        # Folder mode: preserve relative structure under source
        pairs: list[tuple[Path, Path]] = []
        for f in files:
            rel_inside = f.relative_to(source_base)
            dst = target_path / rel_inside
            pairs.append((f, dst))
    elif is_multi:
        # Multi-file mode: all go into target as a folder
        pairs = []
        for f in files:
            dst = target_path / f.name
            pairs.append((f, dst))
    else:
        # Single file: target is exact destination path
        f = files[0]
        if target_path.is_dir() or target.endswith("/"):
            dst = target_path / f.name
        else:
            dst = target_path
        pairs = [(f, dst)]

    # Validate
    for src, dst in pairs:
        try:
            ensure_allowed_vault_file(src)
        except ValueError as exc:
            return f"Source validation failed for {relative_to_vault(src)}: {exc}"
        if dst.exists() and not overwrite:
            return f"Target already exists and overwrite=false: {relative_to_vault(dst)}"
        if is_ignored_path(dst):
            return f"Refusing to move into ignored path: {relative_to_vault(dst)}"

    move_pairs_for_links: list[tuple[str, str]] = []
    actions: list[str] = []

    for src, dst in pairs:
        old_rel = relative_to_vault(src)
        new_rel = relative_to_vault(dst) if dst.parent.exists() else str(dst.relative_to(root)).replace("\\", "/")
        actions.append(f"{old_rel} -> {new_rel}")
        if vault_suffix(src) in {".md", ".excalidraw.md"}:
            move_pairs_for_links.append((old_rel, new_rel))

    if dry_run:
        lines = [f"dry-run move {len(pairs)}"]
        lines.extend(actions[:80])
        return "\n".join(lines)

    # Execute moves
    if is_folder_mode and source_base and source_base.is_dir():
        # Move as a directory operation to cleanly remove the source folder
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_base), str(target_path))
    else:
        for src, dst in pairs:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

    # Update wikilinks
    link_changes: list[str] = []
    if update_links and move_pairs_for_links:
        link_changes = _update_links_for_moves(move_pairs_for_links)

    # Update index: remove old paths, index new destinations
    for src, dst in pairs:
        old_rel = str(src.relative_to(root)).replace("\\", "/")
        _notify_index_of_delete(old_rel)
        if dst.exists():
            _notify_index_of_write(dst)
    # Re-index notes whose wikilinks were rewritten
    for changed_path in link_changes:
        p = root / changed_path
        if p.exists():
            _notify_index_of_write(p)

    lc = f" links:{len(link_changes)}" if link_changes else ""
    return f"moved {len(pairs)}{lc}"


@mcp.tool()
def copy_files(
    source: str = "",
    sources: list[str] | None = None,
    target: str = "",
    overwrite: bool = False,
    dry_run: bool = False,
) -> str:
    """Copy files/folders inside the vault."""
    if not target.strip():
        return "target must be provided."

    files, is_folder_mode, err = _resolve_file_list(source, sources)
    if err:
        return err
    if not files:
        return "No files found to copy."

    target_path = safe_path(target)
    is_multi = len(files) > 1 or is_folder_mode
    source_base = safe_path(source) if source.strip() else None

    if is_multi and source_base and source_base.is_dir():
        pairs = [(f, target_path / f.relative_to(source_base)) for f in files]
    elif is_multi:
        pairs = [(f, target_path / f.name) for f in files]
    else:
        f = files[0]
        if target_path.is_dir() or target.endswith("/"):
            dst = target_path / f.name
        else:
            dst = target_path
        pairs = [(f, dst)]

    for src, dst in pairs:
        try:
            ensure_allowed_vault_file(src)
        except ValueError as exc:
            return f"Source validation failed for {relative_to_vault(src)}: {exc}"
        if dst.exists() and not overwrite:
            return f"Target already exists and overwrite=false: {relative_to_vault(dst)}"

    root = get_vault_root()

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(root)).replace("\\", "/")
        except ValueError:
            return str(p)

    actions = [f"{relative_to_vault(s)} -> {_rel(d)}" for s, d in pairs]

    if dry_run:
        lines = [f"dry-run copy {len(pairs)}"]
        lines.extend(actions[:80])
        return "\n".join(lines)

    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    return f"copied {len(pairs)}"


@mcp.tool()
def rename_file(
    path: str,
    new_name: str,
    update_links: bool = True,
    overwrite: bool = False,
) -> str:
    """Rename a file in place. Updates wikilinks for .md files."""
    source = safe_path(path)
    if not source.exists():
        return f"File not found: {path}"
    if not source.is_file():
        return f"Path is not a file: {path}"

    clean_name = new_name.strip().replace("\\", "/")
    if not clean_name or clean_name in {".", ".."}:
        return "new_name must be a filename."
    if "/" in clean_name:
        return "new_name must not contain path separators. Use move_files instead."

    target = source.with_name(clean_name)

    try:
        ensure_allowed_vault_file(source)
    except ValueError as exc:
        return str(exc)

    if target.exists() and not overwrite:
        return f"Target already exists and overwrite=false: {relative_to_vault(target)}"

    old_rel = relative_to_vault(source)
    shutil.move(str(source), str(target))
    new_rel = relative_to_vault(target)

    # Update index
    _notify_index_of_delete(old_rel)
    _notify_index_of_write(target)

    if update_links and vault_suffix(source) in {".md", ".excalidraw.md"}:
        changed = _update_links_for_moves([(old_rel, new_rel)])
        if changed:
            root = get_vault_root()
            for cp in changed:
                p = root / cp
                if p.exists():
                    _notify_index_of_write(p)

    lc = f" links:{len(changed)}" if update_links and vault_suffix(source) in {".md", ".excalidraw.md"} and changed else ""
    return f"ok {old_rel} -> {new_rel}{lc}"


@mcp.tool()
def delete_files(
    path: str = "",
    paths: list[str] | None = None,
    trash: bool = True,
    confirm: bool = False,
) -> str:
    """Delete files. trash=True moves to _trash. Requires confirm=true."""
    if not confirm:
        return "Refusing to delete without confirm=true."

    files, is_folder_mode, err = _resolve_file_list(path, paths)
    if err:
        return err
    if not files:
        return "No files found to delete."

    for f in files:
        try:
            ensure_allowed_vault_file(f)
        except ValueError as exc:
            return f"Validation failed for {relative_to_vault(f)}: {exc}"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    deleted: list[str] = []
    trashed: list[str] = []

    for f in files:
        rel = relative_to_vault(f)
        if trash:
            dst = safe_path(f"90_Inbox/_trash/{timestamp}/{rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dst))
            trashed.append(rel)
        else:
            f.unlink()
            deleted.append(rel)
        _notify_index_of_delete(rel)

    n = len(trashed) if trash else len(deleted)
    return f"{'trashed' if trash else 'deleted'} {n}"


@mcp.tool()
def empty_trash(
    older_than_days: int = 0,
    confirm: bool = False,
    dry_run: bool = False,
) -> str:
    """Permanently delete entries from the vault's trash (``90_Inbox/_trash/``).

    The trash is populated by ``delete_files(trash=True)`` and by the
    drop-zone importer when it routes binaries elsewhere. Both create a
    timestamped subdirectory like ``90_Inbox/_trash/20260516_143022/...``.
    Use this tool to free that space for real.

    Safety:
    - Only touches paths beneath ``90_Inbox/_trash/`` (resolved via safe_path).
      Nothing else in the vault can be deleted by this tool.
    - Requires ``confirm=True`` to actually delete. Otherwise returns a
      preview with counts + total size.
    - ``dry_run=True`` always shows the preview, even if confirmed.

    Args:
        older_than_days: Only delete entries whose top-level timestamped
            folder is older than this many days. ``0`` (default) = all.
            Useful for "trim everything older than 30 days" sweeps.
        confirm: Must be True to actually delete. Defaults to False (preview).
        dry_run: If True, never delete — always preview. Equivalent to
            ``confirm=False`` but more explicit when scripted.
    """
    trash_root = safe_path("90_Inbox/_trash")
    if not trash_root.exists() or not trash_root.is_dir():
        return "Trash is already empty (90_Inbox/_trash/ doesn't exist)."

    # Collect top-level timestamp dirs to evaluate. Each is named YYYYMMDD_HHMMSS;
    # we use that to compute age. Anything not matching the pattern is treated
    # as age-unknown and only deleted when older_than_days == 0.
    cutoff = None
    if older_than_days > 0:
        cutoff = datetime.now() - timedelta(days=older_than_days)

    targets: list[Path] = []
    skipped_recent: list[str] = []
    total_files = 0
    total_bytes = 0

    for entry in sorted(trash_root.iterdir()):
        if not entry.exists():  # race
            continue
        # Age check
        if cutoff is not None:
            try:
                ts = datetime.strptime(entry.name[:15], "%Y%m%d_%H%M%S")
            except ValueError:
                # Non-timestamped — use mtime as fallback
                ts = datetime.fromtimestamp(entry.stat().st_mtime)
            if ts >= cutoff:
                skipped_recent.append(entry.name)
                continue
        # Tally
        if entry.is_file():
            try:
                total_bytes += entry.stat().st_size
                total_files += 1
            except OSError:
                pass
        else:
            for p in entry.rglob("*"):
                if p.is_file():
                    try:
                        total_bytes += p.stat().st_size
                        total_files += 1
                    except OSError:
                        pass
        targets.append(entry)

    if not targets:
        msg = "Nothing to delete."
        if skipped_recent:
            msg += f" (Skipped {len(skipped_recent)} newer than {older_than_days}d.)"
        return msg

    size_mb = total_bytes / (1024 * 1024)
    preview = (
        f"{'[DRY RUN] ' if (dry_run or not confirm) else ''}"
        f"{len(targets)} trash entr{'y' if len(targets) == 1 else 'ies'}, "
        f"{total_files} file(s), {size_mb:.1f} MB"
    )
    if skipped_recent:
        preview += f" (skipped {len(skipped_recent)} newer than {older_than_days}d)"

    if dry_run or not confirm:
        listing = "\n".join(f"  - {p.name}" for p in targets[:20])
        if len(targets) > 20:
            listing += f"\n  - …and {len(targets) - 20} more"
        return (
            f"{preview}\n\n"
            f"Would delete:\n{listing}\n\n"
            f"Re-call with confirm=True to actually delete."
        )

    # Actually delete
    failed: list[str] = []
    for entry in targets:
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
            else:
                shutil.rmtree(entry)
        except OSError as e:
            failed.append(f"{entry.name}: {e}")

    result = f"Permanently deleted {preview}"
    if failed:
        result += f"\nFailed:\n" + "\n".join(f"  - {f}" for f in failed)
    return result


@mcp.tool()
def backup_files(
    path: str = "",
    paths: list[str] | None = None,
) -> str:
    """Backup files to .ai_memory_backups/<timestamp>/."""
    files, is_folder_mode, err = _resolve_file_list(path, paths)
    if err:
        return err
    if not files:
        return "No files found to backup."

    for f in files:
        try:
            ensure_allowed_vault_file(f)
        except ValueError as exc:
            return f"Validation failed for {relative_to_vault(f)}: {exc}"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backed: list[str] = []

    for f in files:
        rel = relative_to_vault(f)
        dst = safe_path(f".ai_memory_backups/{timestamp}/{rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        backed.append(rel)

    return f"backed-up {len(backed)}"


@mcp.tool()
def replace_in_vault_text_file(
    path: str,
    pattern: str,
    replacement: str,
    mode: str = "text",
    case_sensitive: bool = True,
    dry_run: bool = True,
) -> str:
    """
    Find-and-replace inside one text-like vault file.
    """
    if not pattern:
        return "pattern must not be empty."

    file = safe_path(path)
    if not file.exists():
        return f"File not found: {path}"
    if not file.is_file():
        return f"Path is not a file: {path}"

    suffix = vault_suffix(file)
    if suffix != ".excalidraw.md" and suffix not in TEXT_INDEXABLE_EXTENSIONS:
        return f"Refusing replace in non-text file type: {suffix or '(no extension)'}"

    try:
        ensure_allowed_vault_file(file)
    except ValueError as exc:
        return str(exc)

    text = file.read_text(encoding="utf-8", errors="ignore")
    mode = mode.strip().lower()
    if mode not in {"text", "regex"}:
        return "mode must be 'text' or 'regex'."

    if mode == "regex":
        normalized_replacement = _normalize_replacement_backrefs(replacement)
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return f"Invalid regex: {exc}"
        try:
            regex.sub(normalized_replacement, "")
        except re.error as exc:
            return f"Invalid replacement string: {exc}"
    else:
        normalized_replacement = replacement
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(re.escape(pattern), flags)

    matches = list(regex.finditer(text))
    if not matches:
        return f"No matches in {path}."

    new_text = regex.sub(normalized_replacement, text)

    if not dry_run:
        file.write_text(new_text, encoding="utf-8")
        _notify_index_of_write(file)

    return f"{'dry-run ' if dry_run else 'ok '}{path} matches:{len(matches)}"



# ─── from original L2381-2419: Markdown movement with link rewrite ───
# =============================================================================
# Markdown movement with link rewrite
# =============================================================================


def rewrite_links_for_move_in_text(text: str, old_target: str, new_target: str) -> str:
    old_norm = normalize_note_target(old_target)
    new_norm = normalize_note_target(new_target)
    old_base = Path(old_norm).name
    pattern = re.compile(r"\[\[([^\]|#]+(?:#[^\]|]+)?)(?:\\?\|([^\]]+))?\]\]")

    def repl(match: re.Match) -> str:
        raw_target = match.group(1).strip().rstrip("\\")
        display = match.group(2).strip() if match.group(2) else ""
        if "#" in raw_target:
            note_part, anchor = raw_target.split("#", 1)
            anchor = "#" + anchor
        else:
            note_part, anchor = raw_target, ""
        note_norm = normalize_note_target(note_part)
        if note_norm != old_norm and Path(note_norm).name != old_base:
            return match.group(0)
        final_target = new_norm + anchor
        sep = r"\|"  # table-safe separator
        if display:
            return f"[[{final_target}{sep}{display}]]"
        # Auto-derive display text so links stay human-readable
        basename = final_target.rsplit("/", 1)[-1] if "/" in final_target else ""
        if basename:
            return f"[[{final_target}{sep}{basename}]]"
        return f"[[{final_target}]]"

    return pattern.sub(repl, text)


# move_note_and_update_links and rename_note_and_update_links have been
# consolidated into move_files(update_links=True) and rename_file(update_links=True).



# ─── from original L3602-3738: Bulk replace ───
# =============================================================================
# Bulk replace across text-like files
# =============================================================================


def _select_text_files(
    folder: str = "",
    paths: list[str] | None = None,
    extensions: list[str] | None = None,
) -> tuple[list[Path], str | None]:
    wanted_exts = {str(e).lower().strip() for e in (extensions or []) if str(e).strip()}

    if paths:
        resolved = []
        for p in paths:
            sp = safe_path(p)
            if not sp.exists():
                return [], f"Path not found: {p}"
            if not sp.is_file():
                return [], f"Path is not a file: {p}"
            if not is_text_indexable_file(sp):
                return [], f"Not a text-indexable file: {p}"
            if wanted_exts and vault_suffix(sp) not in wanted_exts:
                continue
            resolved.append(sp)
        return resolved, None

    candidates = all_vault_files(include_binary=False, include_indexable_only=True, folder=folder)
    selected = []
    for p in candidates:
        suffix = vault_suffix(p)
        if suffix in OPTIONAL_TEXT_EXTRACT_EXTENSIONS:
            continue  # do not bulk-edit extracted binary document formats
        if wanted_exts and suffix not in wanted_exts:
            continue
        selected.append(p)
    return selected, None


def _normalize_replacement_backrefs(replacement: str) -> str:
    """
    Translate $1..$99 and ${name} backreferences to Python re.sub syntax.

    $0 -> \\g<0>  (whole match)
    $1 -> \\1     (numbered group)
    ${foo} -> \\g<foo>  (named group)

    Leaves already-correct \\1 syntax untouched.
    """
    # ${name} -> \g<name>
    out = re.sub(r"\$\{(\w+)\}", r"\\g<\1>", replacement)
    # $0 -> \g<0>
    out = re.sub(r"\$0\b", r"\\g<0>", out)
    # $1..$99 -> \1..\99
    out = re.sub(r"\$([1-9][0-9]?)\b", r"\\\1", out)
    return out


@mcp.tool()
def bulk_replace_in_vault_text_files(
    pattern: str,
    replacement: str,
    mode: str = "text",
    folder: str = "",
    paths: list[str] | None = None,
    extensions: list[str] = [],
    case_sensitive: bool = True,
    dry_run: bool = True,
    max_preview_per_file: int = 3,
) -> str:
    """Bulk find-and-replace across vault text files. dry_run=True for preview."""
    if not pattern:
        return "pattern must not be empty."
    mode = (mode or "text").strip().lower()
    if mode not in {"text", "regex"}:
        return f"Unknown mode {mode!r}. Choose 'text' or 'regex'."

    targets, err = _select_text_files(folder=folder, paths=paths, extensions=extensions)
    if err:
        return err
    if not targets:
        return "No text-like files matched selector."

    if mode == "regex":
        normalized_replacement = _normalize_replacement_backrefs(replacement)
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Invalid regex: {e}"
        # Validate replacement syntax early with a dummy match
        try:
            regex.sub(normalized_replacement, "")
        except re.error as e:
            return f"Invalid replacement string: {e}"
    else:
        normalized_replacement = replacement
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(re.escape(pattern), flags)

    total_matches = 0
    total_files = 0
    preview_lines: list[str] = []

    for path in targets:
        text = read_text(path)
        matches = list(regex.finditer(text))
        if not matches:
            continue
        new_text = regex.sub(normalized_replacement, text)
        if new_text == text:
            continue

        total_matches += len(matches)
        total_files += 1
        rel = relative_to_vault(path)
        preview_lines.append(f"- {rel} — {len(matches)} match{'es' if len(matches) != 1 else ''}")

        shown = 0
        for m in matches:
            if shown >= max_preview_per_file:
                break
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            before_snippet = text[start:end].replace("\n", " ⏎ ")
            after_snippet = (text[start:m.start()] + m.expand(normalized_replacement) + text[m.end():end]).replace("\n", " ⏎ ")
            preview_lines.append(f"    before: … {before_snippet} …")
            preview_lines.append(f"    after:  … {after_snippet} …")
            shown += 1

        if not dry_run:
            path.write_text(new_text, encoding="utf-8")
            _notify_index_of_write(path)

    return f"{'dry-run' if dry_run else 'ok'} matches:{total_matches} files:{total_files}"



# ─── from original L5028-5091: Folder cleanup ───
# =============================================================================
# Folder cleanup
# =============================================================================


@mcp.tool()
def delete_folder(path: str, confirm: bool = False, recursive: bool = False) -> str:
    """Delete a folder. Requires confirm=true. recursive=true for non-empty."""
    if not confirm:
        return "Refusing to delete without confirm=true."

    folder = safe_path(path)
    if not folder.exists():
        return f"Folder not found: {path}"
    if not folder.is_dir():
        return f"Path is not a folder: {path}"

    rel = relative_to_vault(folder)
    if rel in {"", ".", ".."} or folder == get_vault_root():
        return "Refusing to delete vault root."
    if any(part in IGNORED_VAULT_PARTS for part in folder.parts):
        return "Refusing to delete internal/ignored folder."

    if recursive:
        count = sum(1 for _ in folder.rglob("*") if _.is_file())
        shutil.rmtree(str(folder))
        return f"ok deleted {rel} ({count} files)"
    else:
        children = list(folder.iterdir())
        if children:
            names = [c.name for c in children[:10]]
            return f"Folder not empty ({len(children)} item(s): {', '.join(names)}). Use recursive=true to force."
        folder.rmdir()
        return f"Deleted empty folder: {rel}"


@mcp.tool()
def cleanup_empty_folders(folder: str = "", dry_run: bool = True) -> str:
    """Find/delete empty folders. dry_run=True for preview."""
    base = safe_path(folder) if folder.strip() else get_vault_root()
    if not base.exists() or not base.is_dir():
        return f"Folder not found: {folder}"

    # Walk bottom-up
    removed: list[str] = []
    for dirpath in sorted(
        [d for d in base.rglob("*") if d.is_dir()],
        key=lambda d: str(d),
        reverse=True,
    ):
        if is_ignored_path(dirpath):
            continue
        if dirpath == get_vault_root():
            continue
        children = list(dirpath.iterdir())
        if not children:
            rel = relative_to_vault(dirpath)
            if not dry_run:
                dirpath.rmdir()
            removed.append(rel)

    return f"{'dry-run' if dry_run else 'ok'} empty-folders:{len(removed)}"



# ─── from original L5540-5562: PDF extraction ───
# =============================================================================
# PDF text extraction tool
# =============================================================================


@mcp.tool()
def extract_pdf_text(path: str, max_chars: int = 50000) -> str:
    """Extract text from a PDF file."""
    file = safe_path(path)
    if not file.exists():
        return f"File not found: {path}"
    if vault_suffix(file) != ".pdf":
        return f"Not a PDF file: {path}"

    max_chars = max(1000, min(max_chars, 200000))
    text = read_pdf_text(file, max_chars)

    if not text.strip() or text.startswith("[PDF"):
        return text

    return text



# ─── from original L7969-8107: Smart move helper ───
# =============================================================================
# Smart move helper
# =============================================================================

# Folder → note type mapping
_FOLDER_TYPE_MAP: dict[str, str] = {
    "10_Profile": "profile",
    "20_Projects": "project",
    "30_Episodic": "episodic",
    "60_Knowledge": "knowledge",
    "90_Inbox/inbox": "capture",
    "90_Inbox/wip": "note",
    "90_Inbox/review": "note",
    "00_Index": "index",
}

# Folder → default tags mapping
_FOLDER_TAG_MAP: dict[str, list[str]] = {
    "10_Profile": ["profile"],
    "20_Projects": ["project"],
    "60_Knowledge": ["knowledge"],
}


def _infer_type_for_folder(dest_folder: str) -> str:
    """Infer note type from destination folder."""
    for prefix, ntype in _FOLDER_TYPE_MAP.items():
        if dest_folder == prefix or dest_folder.startswith(prefix + "/"):
            return ntype
    return ""


def _infer_tags_for_folder(dest_folder: str) -> list[str]:
    """Infer default tags from destination folder."""
    for prefix, tags in _FOLDER_TAG_MAP.items():
        if dest_folder == prefix or dest_folder.startswith(prefix + "/"):
            return list(tags)
    return []


@mcp.tool()
def move_to_folder(
    source_path: str,
    destination_folder: str,
    note_type: str = "",
    extra_tags: list[str] | None = None,
    update_links: bool = True,
    remove_inbox_tags: bool = True,
) -> str:
    """
    Smart move: relocate a note and auto-set type/tags based on destination.

    Moves ``source_path`` into ``destination_folder``, then updates frontmatter:
    - Sets ``type`` based on folder (or explicit ``note_type``).
    - Adds folder-appropriate tags, plus any ``extra_tags``.
    - Removes inbox/needs-review/imported tags when moving out of 90_Inbox.
    - Updates wikilinks across the vault.
    """
    src = safe_path(source_path)
    if not src.exists():
        return f"Source not found: {source_path}"
    if vault_suffix(src) not in {".md", ".excalidraw.md"}:
        return "Only Markdown notes can be smart-moved."

    dest_folder = destination_folder.strip().rstrip("/")
    dest_dir = safe_path(dest_folder)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / src.name
    dest_rel = str(dest_path.relative_to(get_vault_root())).replace("\\", "/")

    if dest_path.exists():
        return f"Target already exists: {dest_rel}"

    # Determine type and tags
    target_type = note_type.strip() or _infer_type_for_folder(dest_folder)
    target_tags = _infer_tags_for_folder(dest_folder)
    if extra_tags:
        target_tags.extend(t.strip() for t in extra_tags if t.strip())

    # Tags to remove when leaving inbox
    inbox_tags = {"inbox", "needs-review", "imported", "capture"}

    # Read and update frontmatter
    text = read_text(src)
    data, body = split_frontmatter(text)

    if target_type:
        data["type"] = target_type

    # Update tags
    existing_tags = data.get("tags", [])
    if isinstance(existing_tags, str):
        existing_tags = [existing_tags]
    elif isinstance(existing_tags, list):
        existing_tags = [str(t) for t in existing_tags]
    else:
        existing_tags = []

    if remove_inbox_tags and not dest_folder.startswith("90_Inbox"):
        existing_tags = [t for t in existing_tags if t.strip().lower() not in inbox_tags]

    # Merge with target tags
    merged_tags: list[str] = []
    seen_lower: set[str] = set()
    for t in existing_tags + target_tags:
        tl = t.strip().lower()
        if tl and tl not in seen_lower:
            seen_lower.add(tl)
            merged_tags.append(t.strip())
    data["tags"] = merged_tags

    # Update status
    old_status = str(data.get("status", ""))
    if old_status in ("inbox", "needs-review", "imported") and not dest_folder.startswith("90_Inbox"):
        data["status"] = "active"

    # Remove import artifacts
    if not dest_folder.startswith("90_Inbox"):
        for key in ("import_source", "imported_at"):
            data.pop(key, None)

    data["last_updated"] = today_iso()

    # Write updated frontmatter to source before moving
    new_text = dump_frontmatter(data, body)
    src.write_text(new_text, encoding="utf-8")

    # Move the file
    result = move_files(
        source=source_path,
        target=dest_rel,
        update_links=update_links,
        overwrite=False,
        dry_run=False,
    )

    return f"ok {dest_rel} type={target_type or '(unchanged)'} tags={merged_tags} | {result}"


