"""Import helpers; Quick capture / inbox; File upload

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
from .calendar import _run_vault_cron


# ─── from original L2420-2902: Import helpers ───
# =============================================================================
# Import helpers and resumable jobs
# =============================================================================


def ensure_under_inbox(path: Path) -> None:
    rel = relative_to_vault(path).replace("\\", "/")
    if not (rel == "90_Inbox" or rel.startswith("90_Inbox/")):
        raise ValueError("Mass import sources must be under 90_Inbox for safety.")


def build_import_note_mapping(source_folder: Path, target_folder: Path) -> dict[str, str]:
    root = get_vault_root()
    mapping: dict[str, str] = {}
    for src in source_folder.rglob("*.md"):
        if is_ignored_path(src):
            continue
        rel_inside_import = src.relative_to(source_folder)
        dst = target_folder / rel_inside_import
        old_vault_target = normalize_note_target(str(src.relative_to(root)))
        old_import_target = normalize_note_target(str(rel_inside_import))
        old_basename = src.stem
        new_target = normalize_note_target(str(dst.relative_to(root)))
        mapping[old_vault_target] = new_target
        mapping[old_import_target] = new_target
        mapping[old_basename] = new_target
    return mapping


def rewrite_wikilinks_for_import(text: str, mapping: dict[str, str]) -> str:
    pattern = re.compile(r"\[\[([^\]|#]+(?:#[^\]|]+)?)(?:\\?\|([^\]]+))?\]\]")

    def repl(match: re.Match) -> str:
        raw_target = match.group(1).strip().rstrip("\\")
        display = match.group(2).strip() if match.group(2) else ""
        if "#" in raw_target:
            note_part, anchor = raw_target.split("#", 1)
            anchor = "#" + anchor
        else:
            note_part, anchor = raw_target, ""
        normalized = normalize_note_target(note_part)
        basename = Path(normalized).name
        new_base = mapping.get(normalized) or mapping.get(basename)
        if not new_base:
            return match.group(0)
        new_target = new_base + anchor
        sep = r"\|"  # table-safe separator
        if display:
            return f"[[{new_target}{sep}{display}]]"
        # Auto-derive display text so links stay human-readable
        base_display = new_target.rsplit("/", 1)[-1] if "/" in new_target else ""
        if base_display:
            return f"[[{new_target}{sep}{base_display}]]"
        return f"[[{new_target}]]"

    return pattern.sub(repl, text)


def transform_imported_markdown(
    text: str,
    source_rel: str,
    imported_at: str,
    mapping: dict[str, str],
    strip_old_tags: bool,
    rewrite_links: bool,
) -> str:
    data, body = split_frontmatter(text)

    old_aliases = data.get("aliases", [])
    aliases = [old_aliases] if isinstance(old_aliases, str) else [str(a) for a in old_aliases] if isinstance(old_aliases, list) else []

    if strip_old_tags:
        data["tags"] = ["imported", "needs-review"]
    else:
        old_tags = data.get("tags", [])
        tags = [old_tags] if isinstance(old_tags, str) else [str(t) for t in old_tags] if isinstance(old_tags, list) else []
        data["tags"] = unique_preserve_order(tags + ["imported", "needs-review"])

    data["type"] = "imported_note"
    data["status"] = "needs-review"
    data["import_source"] = source_rel
    data["imported_at"] = imported_at
    if aliases:
        data["aliases"] = unique_preserve_order(aliases)

    new_text = dump_frontmatter(data, body)
    if rewrite_links:
        new_text = rewrite_wikilinks_for_import(new_text, mapping)
    return new_text


def job_root() -> Path:
    root = safe_path(".ai_memory_jobs")
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_path(job_id: str) -> Path:
    if not re.match(r"^[A-Za-z0-9_.-]+$", job_id):
        raise ValueError("Invalid job_id")
    return job_root() / f"{job_id}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_job(job_id: str) -> dict[str, Any]:
    path = job_path(job_id)
    if not path.exists():
        raise ValueError(f"Job not found: {job_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def make_job_id(prefix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}_{uuid.uuid4().hex[:8]}"


@mcp.tool()
def mass_import_vault(
    import_folder: str,
    target_folder: str = "40_Raw_Logs/imported",
    dry_run: bool = True,
    overwrite: bool = False,
    strip_old_tags: bool = True,
    rewrite_links: bool = True,
    copy_attachments: bool = True,
    limit: int = 5000,
) -> str:
    """
    Mass-import an old Obsidian vault/folder from 90_Inbox into a target folder.
    """
    source_folder = safe_path(import_folder)
    target_root = safe_path(target_folder)

    try:
        ensure_under_inbox(source_folder)
    except ValueError as exc:
        return str(exc)

    if not source_folder.exists():
        return f"Import folder not found: {import_folder}"
    if not source_folder.is_dir():
        return f"Import path is not a folder: {import_folder}"
    if is_ignored_path(target_root):
        return "Refusing to import into ignored/internal path."

    all_files = [p for p in source_folder.rglob("*") if p.is_file() and not is_ignored_path(p)]
    all_files = all_files[: max(1, min(limit, 100000))]
    mapping = build_import_note_mapping(source_folder, target_root)
    imported_at = datetime.now().isoformat(timespec="seconds")

    actions: list[str] = []
    skipped: list[str] = []

    for src in all_files:
        suffix = vault_suffix(src)
        if suffix != ".md" and suffix != ".excalidraw.md" and not copy_attachments:
            continue
        if suffix != ".md" and suffix != ".excalidraw.md":
            try:
                ensure_allowed_vault_file(src)
            except ValueError:
                skipped.append(f"unsupported: {relative_to_vault(src)}")
                continue

        rel_inside = src.relative_to(source_folder)
        dst = target_root / rel_inside
        if dst.exists() and not overwrite:
            skipped.append(f"exists: {relative_to_vault(dst)}")
            continue

        if suffix == ".md" or suffix == ".excalidraw.md":
            text = read_text(src)
            source_rel = relative_to_vault(src)
            text = transform_imported_markdown(text, source_rel, imported_at, mapping, strip_old_tags, rewrite_links)
            actions.append(f"note: {relative_to_vault(src)} -> {relative_to_vault(dst)}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(text, encoding="utf-8")
        else:
            actions.append(f"file: {relative_to_vault(src)} -> {relative_to_vault(dst)}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    lines = [
        "Mass import vault result:",
        f"Dry run: {dry_run}",
        f"Source: {relative_to_vault(source_folder)}",
        f"Target: {relative_to_vault(target_root)}",
        f"Actions: {len(actions)}",
        f"Skipped: {len(skipped)}",
        "",
        "First actions:",
    ]
    if not dry_run and _vault_index is not None:
        _vault_index.sync()
    return f"{'dry-run' if dry_run else 'ok'} actions:{len(actions)} skipped:{len(skipped)}"


@mcp.tool()
def create_vault_import_job(
    import_folder: str,
    target_folder: str = "40_Raw_Logs/imported",
    strip_old_tags: bool = True,
    rewrite_links: bool = True,
    copy_attachments: bool = True,
    overwrite: bool = False,
    limit: int = 20000,
) -> str:
    source_folder = safe_path(import_folder)
    target_root = safe_path(target_folder)
    try:
        ensure_under_inbox(source_folder)
    except ValueError as exc:
        return str(exc)
    if not source_folder.exists() or not source_folder.is_dir():
        return f"Import folder not found or not a folder: {import_folder}"

    all_files = [p for p in source_folder.rglob("*") if p.is_file() and not is_ignored_path(p)]
    all_files = all_files[: max(1, min(limit, 100000))]
    mapping = build_import_note_mapping(source_folder, target_root)

    steps = []
    for src in all_files:
        suffix = vault_suffix(src)
        if suffix in {".md", ".excalidraw.md"} or copy_attachments:
            try:
                ensure_allowed_vault_file(src)
            except ValueError:
                continue
            rel_inside = src.relative_to(source_folder)
            dst = target_root / rel_inside
            steps.append(
                {
                    "type": "import_markdown" if suffix in {".md", ".excalidraw.md"} else "copy_file",
                    "source": relative_to_vault(src),
                    "target": relative_to_vault(dst),
                    "status": "pending",
                    "error": "",
                }
            )

    job_id = make_job_id("import")
    job = {
        "job_id": job_id,
        "kind": "vault_import",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source_folder": relative_to_vault(source_folder),
        "target_folder": relative_to_vault(target_root),
        "options": {
            "strip_old_tags": strip_old_tags,
            "rewrite_links": rewrite_links,
            "copy_attachments": copy_attachments,
            "overwrite": overwrite,
        },
        "mapping": mapping,
        "next_index": 0,
        "steps": steps,
    }
    atomic_write_json(job_path(job_id), job)
    return (
        "Created vault import job.\n"
        f"Job ID: {job_id}\n"
        f"Source: {job['source_folder']}\n"
        f"Target: {job['target_folder']}\n"
        f"Steps: {len(steps)}\n\n"
        f"Run with: run_vault_job(job_id=\"{job_id}\", max_steps=50)"
    )


@mcp.tool()
def run_vault_job(job_id: str, max_steps: int = 50) -> str:
    job = load_job(job_id)
    steps = job.get("steps", [])
    next_index = int(job.get("next_index", 0))
    max_steps = max(1, min(max_steps, 500))
    imported_at = job.get("created_at", datetime.now().isoformat(timespec="seconds"))
    options = job.get("options", {})
    mapping = job.get("mapping", {})
    overwrite = bool(options.get("overwrite", False))

    processed = 0
    errors = []
    while next_index < len(steps) and processed < max_steps:
        step = steps[next_index]
        try:
            src = safe_path(step["source"])
            dst = safe_path(step["target"])
            if dst.exists() and not overwrite:
                step["status"] = "skipped"
                step["error"] = "target exists and overwrite=false"
            elif step["type"] == "import_markdown":
                text = read_text(src)
                transformed = transform_imported_markdown(
                    text=text,
                    source_rel=step["source"],
                    imported_at=imported_at,
                    mapping=mapping,
                    strip_old_tags=bool(options.get("strip_old_tags", True)),
                    rewrite_links=bool(options.get("rewrite_links", True)),
                )
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(transformed, encoding="utf-8")
                step["status"] = "done"
                step["error"] = ""
            elif step["type"] == "copy_file":
                ensure_allowed_vault_file(src)
                ensure_allowed_vault_file(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                step["status"] = "done"
                step["error"] = ""
            else:
                step["status"] = "error"
                step["error"] = f"unknown step type: {step.get('type')}"
                errors.append(step["error"])
        except Exception as exc:
            step["status"] = "error"
            step["error"] = str(exc)
            errors.append(f"{step.get('source')}: {exc}")

        next_index += 1
        processed += 1
        job["next_index"] = next_index
        job["updated_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(job_path(job_id), job)

    done = sum(1 for s in steps if s.get("status") == "done")
    skipped = sum(1 for s in steps if s.get("status") == "skipped")
    error_count = sum(1 for s in steps if s.get("status") == "error")
    pending = sum(1 for s in steps if s.get("status") == "pending")

    lines = [
        f"Job: {job_id}",
        f"Processed this run: {processed}",
        f"Done: {done}",
        f"Skipped: {skipped}",
        f"Errors: {error_count}",
        f"Pending: {pending}",
    ]
    if processed > 0 and _vault_index is not None:
        _vault_index.sync()
    return f"done:{done} skipped:{skipped} errors:{error_count} pending:{pending}"


@mcp.tool()
def get_vault_job_status(job_id: str) -> str:
    job = load_job(job_id)
    steps = job.get("steps", [])
    done = sum(1 for s in steps if s.get("status") == "done")
    skipped = sum(1 for s in steps if s.get("status") == "skipped")
    error_count = sum(1 for s in steps if s.get("status") == "error")
    pending = sum(1 for s in steps if s.get("status") == "pending")
    return f"{job_id}|done:{done}|skip:{skipped}|err:{error_count}|pending:{pending}"


@mcp.tool()
def list_vault_jobs(limit: int = 50) -> str:
    jobs = []
    for path in job_root().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            jobs.append((path.stat().st_mtime, data))
        except Exception:
            continue
    jobs.sort(reverse=True, key=lambda x: x[0])
    jobs = jobs[: max(1, min(limit, 200))]
    if not jobs:
        return "No vault jobs found."

    out = []
    for _, job in jobs:
        steps = job.get("steps", [])
        total = len(steps)
        done = sum(1 for s in steps if s.get("status") == "done")
        pending = sum(1 for s in steps if s.get("status") == "pending")
        error = sum(1 for s in steps if s.get("status") == "error")
        state = "completed" if total > 0 and pending == 0 and error == 0 else "active"
        out.append(f"{job.get('job_id')}|{job.get('kind')}|{state}|{done}/{total}|err:{error}|{job.get('updated_at')}")
    return "\n".join(out)


@mcp.tool()
def cleanup_jobs(
    older_than_days: int = 7,
    include_active: bool = False,
    dry_run: bool = True,
) -> str:
    """Remove completed (or abandoned) job state files from .ai_memory_jobs/. Default removes completed jobs older than 7 days. Set include_active=True to also remove abandoned/active jobs."""
    cutoff = datetime.now() - timedelta(days=max(0, older_than_days))
    removed: list[str] = []
    kept: list[str] = []
    freed_bytes = 0
    for path in sorted(job_root().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            kept.append(f"{path.name}|err:unreadable")
            continue
        steps = data.get("steps", [])
        total = len(steps)
        pending = sum(1 for s in steps if s.get("status") == "pending")
        error = sum(1 for s in steps if s.get("status") == "error")
        is_done = total > 0 and pending == 0 and error == 0
        updated = data.get("updated_at", "")
        try:
            updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00").split("+")[0]) if updated else None
        except Exception:
            updated_dt = None
        too_old = updated_dt is not None and updated_dt < cutoff

        should_remove = (is_done and too_old) or (include_active and too_old)
        if should_remove:
            size = path.stat().st_size
            freed_bytes += size
            if not dry_run:
                path.unlink()
            removed.append(f"{path.name}|{'done' if is_done else 'active'}|{size//1024}KB")
        else:
            kept.append(f"{path.name}|{'done' if is_done else 'active'}|kept")

    prefix = "dry-run " if dry_run else "ok "
    summary = f"{prefix}removed:{len(removed)} kept:{len(kept)} freed:{freed_bytes//1024}KB"
    lines = [summary]
    lines.extend(removed)
    if kept and dry_run:
        lines.extend(kept)
    return "\n".join(lines)


@mcp.tool()
def get_note_summary(path: str) -> str:
    """Get a note's title and summary from SQLite (no file read). Returns path|title|type|status|words|source:auto|manual|summary."""
    idx = get_vault_index()
    rel = relative_to_vault(safe_path(path))
    row = idx.conn.execute(
        "SELECT title, type, status, word_count, summary, summary_source FROM notes WHERE path = ?",
        (rel,),
    ).fetchone()
    if not row:
        return f"err: note not indexed: {rel}"
    return (
        f"{rel}|{row['title']}|type:{row['type']}|status:{row['status']}|"
        f"words:{row['word_count']}|source:{row['summary_source'] or 'none'}|{row['summary']}"
    )


@mcp.tool()
def set_note_summary(path: str, summary: str) -> str:
    """Set a manual summary for a note in SQLite. Manual summaries survive re-indexing. Pass empty string to revert to auto-generated."""
    idx = get_vault_index()
    rel = relative_to_vault(safe_path(path))
    row = idx.conn.execute("SELECT path FROM notes WHERE path = ?", (rel,)).fetchone()
    if not row:
        return f"err: note not indexed: {rel}. Write the note first or rebuild_vault_index."
    summary = summary.strip()
    if not summary:
        # Revert: re-derive from current note body
        note = safe_path(path)
        if note.exists() and note.is_file():
            text = read_text(note)
            _, body = split_frontmatter(text)
            auto = idx._auto_summary(body)
            idx.conn.execute(
                "UPDATE notes SET summary = ?, summary_source = ? WHERE path = ?",
                (auto, "auto" if auto else "", rel),
            )
            idx.conn.commit()
            return f"ok reverted to auto: {rel}|{auto[:80]}"
    idx.conn.execute(
        "UPDATE notes SET summary = ?, summary_source = ? WHERE path = ?",
        (summary[:500], "manual", rel),
    )
    idx.conn.commit()
    return f"ok {rel}|manual|{summary[:80]}"


@mcp.tool()
def summarize_note_with_llm(
    path: str,
    save: bool = True,
    max_tokens: int = 300,
    force: bool = False,
) -> str:
    """Generate an LLM-written summary of a note and (optionally) persist it.

    Reads the note's body (frontmatter stripped) and asks the configured LLM
    (``IRIS_LLM_MODEL``) for a concise 2–4 sentence summary capturing the
    main ideas. Useful for context-priming on long notes, or for backfilling
    summaries during reindex.

    Persistence: when ``save=True`` (default), the summary is stored in
    SQLite with ``summary_source='llm'``. Existing ``manual`` summaries are
    preserved unless ``force=True``.

    Requires an LLM endpoint to be configured (Ollama/LM Studio/OpenAI).
    Returns an error string if not.

    Args:
        path: Vault-relative path to a Markdown note.
        save: Store the result in SQLite (overwrites existing llm/auto summary).
        max_tokens: Upper bound on LLM output length.
        force: Overwrite even a manual summary.
    """
    try:
        from .. import llm
    except ImportError:
        return "err: LLM module not available"
    if not llm.is_configured():
        return ("err: no LLM model configured. Set IRIS_LLM_MODEL or "
                "[llm].model in ~/.config/iris/config.toml to enable.")

    note = safe_path(path)
    if not note.exists():
        return f"err: note not found: {path}"
    if vault_suffix(note) not in {".md", ".excalidraw.md"}:
        return "err: only Markdown notes can be summarized"

    raw = read_text(note)
    if not raw or not raw.strip():
        return f"err: note is empty: {path}"
    _, body = split_frontmatter(raw)
    if len(body.strip()) < 80:
        return f"note too short to summarize: {path} ({len(body.strip())} chars)"

    idx = get_vault_index()
    rel = relative_to_vault(note)

    if save and not force:
        existing = idx.conn.execute(
            "SELECT summary_source FROM notes WHERE path = ?", (rel,)
        ).fetchone()
        if existing and existing["summary_source"] == "manual":
            return ("skipped: manual summary exists. Pass force=True to "
                    "overwrite, or use get_note_summary to view it.")

    title = idx.conn.execute(
        "SELECT title FROM notes WHERE path = ?", (rel,)
    ).fetchone()
    title_hint = (title["title"] if title else rel) or rel

    system = (
        "You are summarizing an Obsidian note for later context-priming. "
        "Output 2–4 sentences capturing the main ideas, decisions, and any "
        "open questions. No bullet points, no preamble like 'This note is "
        "about'. Write in third person, present tense."
    )
    user = f"Note title: {title_hint}\n\n---\n\n{body[:8000]}"
    try:
        summary = llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.4,
            think=False,   # note summary — direct output, no reasoning needed
        ).strip()
    except llm.LLMError as e:
        return f"err: LLM call failed: {e}"

    if not summary:
        return "err: LLM returned empty summary"

    if save:
        row = idx.conn.execute("SELECT path FROM notes WHERE path = ?", (rel,)).fetchone()
        if row:
            idx.conn.execute(
                "UPDATE notes SET summary = ?, summary_source = ? WHERE path = ?",
                (summary[:500], "llm", rel),
            )
            idx.conn.commit()

    return summary


# ─── from original L7230-7332: Quick capture / inbox ───
# =============================================================================
# Quick capture / inbox tools
# =============================================================================


@mcp.tool()
def quick_capture(thought: str, title: str = "", tags: list[str] = []) -> str:
    """Capture a thought to 90_Inbox/inbox/ for later triage."""
    if not thought.strip():
        return "Nothing to capture — thought is empty."
    now = datetime.now()
    date_str = now.date().isoformat()
    time_slug = now.strftime("%H%M%S")
    if title.strip():
        slug_chars = [ch.lower() if ch.isalnum() else "_" for ch in title.strip()]
        slug = "_".join(p for p in "".join(slug_chars).split("_") if p)
    else:
        slug = "capture"
    filename = f"{date_str}_{time_slug}_{slug}.md"
    rel_path = f"90_Inbox/inbox/{filename}"
    target = safe_path(rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    tag_list = ["inbox"] + [t.strip() for t in tags if t.strip()]
    data: dict[str, object] = {
        "type": "capture",
        "created": date_str,
        "status": "inbox",
        "tags": tag_list,
    }
    body = f"# {title.strip() or 'Quick Capture'}\n\n{thought.strip()}\n"
    new_text = dump_frontmatter(data, body)
    target.write_text(new_text, encoding="utf-8")
    _notify_index_of_write(target, text=new_text)
    return f"ok {rel_path}"


@mcp.tool()
def triage_inbox(limit: int = 50, auto_import_binaries: bool = False) -> str:
    """List everything awaiting triage in 90_Inbox/inbox/ — markdown notes AND binary files. Markdown title+summary come from SQLite (no file reads). Set auto_import_binaries=True to route non-md files to 40_Attachments/ and create inbox notes."""
    root = get_vault_root()
    inbox_dir = root / "90_Inbox" / "inbox"
    if not inbox_dir.is_dir():
        return "none (no 90_Inbox/inbox/ folder)"

    idx = get_vault_index()
    c = idx.conn

    items: list[str] = []
    bin_files: list[Path] = []

    for f in sorted(inbox_dir.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        rel = relative_to_vault(f)
        if f.suffix.lower() == ".md":
            row = c.execute(
                "SELECT title, summary FROM notes WHERE path = ?", (rel,)
            ).fetchone()
            if row:
                title = row["title"] or f.stem
                summary = (row["summary"] or "")[:120]
            else:
                # Note not indexed yet — fall back to a cheap read
                try:
                    text = read_text(f)
                    title = title_from_text(text, f.stem)
                    _, body = split_frontmatter(text)
                    summary = body.strip()[:120].replace("\n", " ")
                except Exception:
                    title = f.stem
                    summary = ""
            items.append(f"{rel}|md|{title}|{summary}")
        else:
            subfolder = _route_attachment(f.name)
            type_label = subfolder.lower().rstrip("s")
            size_kb = f.stat().st_size // 1024
            bin_files.append(f)
            items.append(f"{rel}|{type_label}|{f.name}|{size_kb}KB")
        if len(items) >= limit:
            break

    if auto_import_binaries and bin_files:
        imported: list[str] = []
        for f in bin_files:
            try:
                import_file(str(f), description=f"Auto-imported from 90_Inbox/inbox/ on {datetime.now().date().isoformat()}")
                # Move original to vault trash
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                trash_dir = root / "90_Inbox" / "_trash" / ts
                trash_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(trash_dir / f.name))
                imported.append(f"moved: {f.name}")
            except Exception as e:
                imported.append(f"err {f.name}: {str(e)[:80]}")
        items.append(f"[auto-imported:{len(imported)}]")
        items.extend(imported)

    if not items:
        return "none"
    return "\n".join(items)



# ─── from original L8534-8683: File upload ───
# =============================================================================
# File import — upload / drop zone → vault
# =============================================================================

# Extension → attachment subfolder mapping
_ATTACHMENT_ROUTING: dict[str, str] = {
    # Images
    ".png": "Images", ".jpg": "Images", ".jpeg": "Images",
    ".gif": "Images", ".webp": "Images", ".svg": "Images",
    ".bmp": "Images", ".ico": "Images", ".heic": "Images",
    # PDFs
    ".pdf": "PDFs",
    # Excalidraw (handled specially)
    ".excalidraw": "Excalidraw",
    # Everything else → Attachments
}

def _route_attachment(filename: str) -> str:
    """Return the 40_Attachments/ subfolder for a given filename."""
    ext = Path(filename).suffix.lower()
    return _ATTACHMENT_ROUTING.get(ext, "Attachments")


@mcp.tool()
def import_file(
    source_path: str,
    description: str = "",
    target_note: str = "",
    create_note: bool = True,
) -> str:
    """
    Import a file into the vault from any local path (Downloads, Desktop, temp, etc).

    The file is copied to the correct ``40_Attachments/`` subfolder based on type:
      - Images (.png, .jpg, .heic, …) → ``40_Attachments/Images/``
      - PDFs → ``40_Attachments/PDFs/``
      - Everything else → ``40_Attachments/Attachments/``

    If ``create_note`` is True (default), creates an inbox note at
    ``90_Inbox/inbox/<filename>.md`` that embeds the file and includes
    the description. You can then triage it to the right location.

    If ``target_note`` is given, the embed is appended to that existing
    note instead of creating a new inbox note.

    Args:
        source_path: Absolute path to the file to import.
        description: Optional description / context for the file.
        target_note: If set, append the embed to this note instead of creating inbox note.
        create_note: Whether to create/update a note referencing the file (default True).
    """
    src = Path(source_path).expanduser().resolve()
    if not src.exists():
        return f"File not found: {source_path}"
    if not src.is_file():
        return f"Not a file: {source_path}"

    # Determine destination
    subfolder = _route_attachment(src.name)
    dest_dir = get_vault_root() / "40_Attachments" / subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Handle name conflicts
    dest = dest_dir / src.name
    if dest.exists():
        stem = src.stem
        ext = src.suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{ext}"
            counter += 1

    # Copy file
    import shutil as _shutil
    _shutil.copy2(str(src), str(dest))
    rel_dest = str(dest.relative_to(get_vault_root())).replace("\\", "/")
    vault_name = dest.name

    lines: list[str] = [f"Imported: {rel_dest}"]

    if not create_note:
        return "\n".join(lines)

    # Build embed syntax
    is_image = subfolder == "Images"
    embed = f"![[{vault_name}]]" if is_image else f"[[{vault_name}]]"

    if target_note:
        # Append to existing note
        note = safe_path(target_note)
        if not note.exists():
            return f"Target note not found: {target_note}\nFile was imported to: {rel_dest}"
        old = read_text(note)
        separator = "\n\n" if old and not old.endswith("\n\n") else ""
        append_text = f"{embed}"
        if description:
            append_text = f"{description}\n\n{append_text}"
        note.write_text(old + separator + append_text + "\n", encoding="utf-8")
        _notify_index_of_write(note, text=old + separator + append_text)
        lines.append(f"Embedded in: {relative_to_vault(note)}")
    else:
        # Create inbox note
        inbox_dir = get_vault_root() / "90_Inbox" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        note_name = f"{src.stem}.md"
        note_path = inbox_dir / note_name
        counter = 1
        while note_path.exists():
            note_path = inbox_dir / f"{src.stem}_{counter}.md"
            counter += 1

        today = datetime.now().date().isoformat()
        desc_line = f"\n{description}\n" if description else ""
        note_content = (
            f"---\n"
            f"type: inbox\n"
            f"status: needs-review\n"
            f"tags:\n"
            f"  - imported\n"
            f"last_updated: {today}\n"
            f"---\n"
            f"# {src.stem}\n"
            f"{desc_line}\n"
            f"{embed}\n"
            f"\n"
            f"## Related Notes\n"
        )
        note_path.write_text(note_content, encoding="utf-8")
        _notify_index_of_write(note_path, text=note_content)
        lines.append(f"Inbox note: {str(note_path.relative_to(get_vault_root()))}")

    return "\n".join(lines)


@mcp.tool()
def import_drop_zone() -> str:
    """
    Process anything dropped into the vault's inbox folder (``90_Inbox/inbox/``).

    Scans the folder, imports binary files to the correct ``40_Attachments/``
    subfolder, creates inbox notes that embed them, and removes the original
    binary from ``90_Inbox/inbox/``. Markdown notes already in place stay
    where they are (frontmatter is added if missing).

    Drop files into ``90_Inbox/inbox/`` from Finder, AirDrop, iPhone, or any
    app, then call this tool (or ``triage_inbox(auto_import_binaries=True)``).
    """
    return _run_vault_cron("import-drop-zone", timeout=30)


