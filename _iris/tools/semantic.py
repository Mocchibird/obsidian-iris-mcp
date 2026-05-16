"""Semantic search over vault notes.

Embeds notes via an OpenAI-compatible /v1/embeddings endpoint (Ollama by default)
and ranks them against a query embedding by cosine similarity.

See _iris/embeddings.py for endpoint/model env vars.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .. import mcp
from ..core import (
    extract_wikilinks,
    get_vault_index,
    get_vault_root,
    is_ignored_path,
    note_target_to_relative_md,
    read_text,
    relative_to_vault,
    safe_path,
    split_frontmatter,
    title_from_text,
)
from .. import embeddings as emb


def _note_text_for_embedding(path: Path) -> str:
    """Build the text fed to the embedding model: title + body (no frontmatter).

    Skips Excalidraw files (.excalidraw.md) since their content is raw JSON
    that produces useless embeddings and explodes chunk counts. Use the
    ``extract_excalidraw_text`` tool to pull their labels if you want them
    searchable later.
    """
    # Skip Excalidraw files — both the .excalidraw.md double-extension form and
    # the single-extension form Obsidian uses (frontmatter has
    # ``excalidraw-plugin: parsed``; in practice these always live in an
    # /Excalidraw/ folder).
    if path.name.endswith(".excalidraw.md") or "/Excalidraw/" in str(path):
        return ""
    raw = read_text(path)
    if not raw:
        return ""
    fm, body = split_frontmatter(raw)
    if fm.get("excalidraw-plugin"):
        return ""
    title = title_from_text(body, fallback=path.stem)
    return f"{title}\n\n{body}".strip()


# Chunking target: ~2000 chars ≈ 500–700 tokens, comfortably under nomic-embed's
# 2048-token context window. Overlap preserves a bit of cross-chunk context so
# paragraphs near a chunk boundary don't lose their neighbours.
_CHUNK_TARGET = 2000
_CHUNK_OVERLAP = 200


def _chunk_text(text: str, target: int = _CHUNK_TARGET,
                overlap: int = _CHUNK_OVERLAP) -> list[tuple[int, int, str]]:
    """Split text into (start, end, chunk) tuples, paragraph-aware.

    Greedy fill up to ``target`` chars by paragraph (``\\n\\n``). Single
    oversized paragraphs are hard-split by char count. Each subsequent chunk
    starts with the trailing ``overlap`` chars of the previous chunk so
    boundary content isn't lost.
    """
    if not text:
        return []
    if len(text) <= target:
        return [(0, len(text), text)]

    # Walk char by char tracking paragraph boundaries.
    paragraphs: list[tuple[int, str]] = []  # (start_offset, paragraph_text)
    i = 0
    while i < len(text):
        # Find end of this paragraph (next \n\n+)
        end = text.find("\n\n", i)
        if end == -1:
            paragraphs.append((i, text[i:]))
            break
        # Include the trailing blank lines so offsets line up cleanly
        sep_end = end
        while sep_end < len(text) and text[sep_end] == "\n":
            sep_end += 1
        paragraphs.append((i, text[i:sep_end]))
        i = sep_end

    chunks: list[tuple[int, int, str]] = []
    buf_start: int | None = None
    buf_parts: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf_start, buf_parts, buf_len
        if buf_start is not None and buf_parts:
            chunk_text = "".join(buf_parts)
            chunks.append((buf_start, buf_start + len(chunk_text), chunk_text))
        buf_start, buf_parts, buf_len = None, [], 0

    for start, para in paragraphs:
        # Oversized single paragraph — hard-split by char count
        if len(para) > target:
            flush()
            pos = 0
            while pos < len(para):
                sub = para[pos : pos + target]
                chunks.append((start + pos, start + pos + len(sub), sub))
                pos += target - overlap
            continue
        if buf_len + len(para) > target and buf_parts:
            flush()
        if buf_start is None:
            buf_start = start
        buf_parts.append(para)
        buf_len += len(para)
    flush()

    # Add overlap from the tail of each previous chunk to the head of the next.
    if overlap > 0 and len(chunks) > 1:
        prefixed: list[tuple[int, int, str]] = [chunks[0]]
        for prev, cur in zip(chunks, chunks[1:]):
            prev_text = prev[2]
            tail = prev_text[-overlap:] if len(prev_text) > overlap else prev_text
            new_start = max(0, cur[0] - len(tail))
            new_text = tail + cur[2]
            prefixed.append((new_start, cur[1], new_text))
        chunks = prefixed

    return chunks


@mcp.tool()
def embedding_status() -> str:
    """Report semantic-search index health and embedding-endpoint config.

    Shows the configured embed endpoint and model, the total number of indexed
    markdown notes, how many have an embedding for the current model, and how
    many are stale (content_hash differs from the embedded version).

    Run ``reindex_embeddings`` to populate or refresh the index.
    """
    idx = get_vault_index()
    c = idx.conn
    md_total = c.execute(
        "SELECT COUNT(*) FROM files WHERE suffix = '.md'"
    ).fetchone()[0]
    embedded = c.execute(
        "SELECT COUNT(DISTINCT note_path) FROM note_embeddings WHERE model = ?",
        (emb.EMBED_MODEL,),
    ).fetchone()[0]
    stale = c.execute(
        "SELECT COUNT(DISTINCT ne.note_path) FROM note_embeddings ne "
        "JOIN files f ON ne.note_path = f.path "
        "WHERE ne.model = ? AND ne.content_hash != f.content_hash",
        (emb.EMBED_MODEL,),
    ).fetchone()[0]
    missing = md_total - embedded
    return (
        f"=== Embedding config ===\n{emb.config_summary()}\n\n"
        f"=== Index status ===\n"
        f"markdown notes: {md_total}\n"
        f"embedded:       {embedded}\n"
        f"missing:        {missing}\n"
        f"stale:          {stale}\n"
    )


@mcp.tool()
def reindex_embeddings(force: bool = False, limit: int = 0) -> str:
    """Build or refresh the semantic-search index for vault notes.

    Embeds markdown notes whose content_hash has changed since their last
    embedding (or all of them if ``force=True``). Skips empty/ignored files.

    First-time indexing for a vault with ~600 notes typically takes 1–5 minutes
    against a local Ollama running ``nomic-embed-text``. Subsequent runs only
    re-embed notes that actually changed, so they're near-instant.

    Args:
        force: Re-embed everything, ignoring content_hash.
        limit: Cap the number of notes embedded (0 = no cap). Useful for testing.
    """
    idx = get_vault_index()
    c = idx.conn

    # Pick notes to (re)embed
    if force:
        rows = c.execute(
            "SELECT path, content_hash FROM files WHERE suffix = '.md' "
            "ORDER BY path"
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT f.path, f.content_hash FROM files f "
            "LEFT JOIN note_embeddings ne "
            "  ON ne.note_path = f.path AND ne.model = ? "
            "WHERE f.suffix = '.md' "
            "  AND (ne.note_path IS NULL OR ne.content_hash != f.content_hash) "
            "ORDER BY f.path",
            (emb.EMBED_MODEL,),
        ).fetchall()

    if limit > 0:
        rows = rows[:limit]

    if not rows:
        return f"Index up to date for model '{emb.EMBED_MODEL}' (no notes to embed)."

    root = get_vault_root()
    # (path, content_hash, [(chunk_id, start, end, text), ...])
    to_embed: list[tuple[str, str, list[tuple[int, int, int, str]]]] = []
    for r in rows:
        full = root / r["path"]
        if not full.exists() or is_ignored_path(full):
            continue
        text = _note_text_for_embedding(full)
        if not text.strip():
            continue
        pieces = _chunk_text(text)
        chunk_rows = [(i, start, end, body) for i, (start, end, body) in enumerate(pieces)]
        to_embed.append((r["path"], r["content_hash"], chunk_rows))

    if not to_embed:
        return "No embeddable notes found (all empty or ignored)."

    # Flatten to a single list of (path, content_hash, chunk_id, start, end, text)
    # so we can batch across notes. We delete-and-replace per-note so chunk
    # counts can shrink without leaving orphan rows.
    flat: list[tuple[str, str, int, int, int, str]] = []
    for path, content_hash, chunks_for_note in to_embed:
        for chunk_id, start, end, body in chunks_for_note:
            flat.append((path, content_hash, chunk_id, start, end, body))

    # Wipe existing chunks for all notes we're about to re-embed
    seen_paths = {p for p, _, _ in to_embed}
    for path in seen_paths:
        c.execute(
            "DELETE FROM note_embeddings WHERE note_path = ? AND model = ?",
            (path, emb.EMBED_MODEL),
        )
    c.commit()

    batch_size = 8
    done_notes = 0
    chunks_written = 0
    failed = 0
    now = datetime.now().isoformat(timespec="seconds")
    notes_with_chunks_written: set[str] = set()
    for i in range(0, len(flat), batch_size):
        batch = flat[i : i + batch_size]
        texts = [t for *_, t in batch]
        try:
            vecs = emb.embed_batch(texts, batch_size=batch_size)
        except emb.EmbeddingError as e:
            return (
                f"Embedding failed after {chunks_written} chunks "
                f"({len(notes_with_chunks_written)} notes): {e}\n\n"
                f"Config:\n{emb.config_summary()}"
            )
        if len(vecs) != len(batch):
            failed += len(batch)
            continue
        for (path, content_hash, chunk_id, start, end, _body), vec in zip(batch, vecs):
            c.execute(
                "INSERT OR REPLACE INTO note_embeddings "
                "(note_path, chunk_id, model, content_hash, chunk_start, chunk_end, "
                " dim, embedding, embedded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (path, chunk_id, emb.EMBED_MODEL, content_hash, start, end,
                 len(vec), emb.pack(vec), now),
            )
            notes_with_chunks_written.add(path)
            chunks_written += 1
        c.commit()

    done_notes = len(notes_with_chunks_written)
    return (
        f"Embedded {done_notes} note(s) as {chunks_written} chunk(s) "
        f"with model '{emb.EMBED_MODEL}'"
        + (f" ({failed} failed)" if failed else "")
        + f". Endpoint: {emb.EMBED_URL}"
    )


# Note types that aggregate links to other notes rather than carry primary
# content (hubs, MOCs, dashboards). Excluded from semantic_search by default
# because they tend to dominate every query — they literally mention everything.
_HUB_TYPES = ("hub", "index", "dashboard", "moc")


@mcp.tool()
def semantic_search(
    query: str,
    top_k: int = 10,
    filter_folder: str = "",
    include_hubs: bool = False,
    show_snippets: bool = False,
) -> str:
    """Find notes semantically related to ``query`` using cosine similarity.

    Embeds the query with the same model used for the index and ranks all
    indexed notes by similarity. Returns the top_k matches with scores.

    Per-note ranking uses the best-matching chunk (notes are split into
    ~2000-char paragraph-aware chunks at index time).

    By default, aggregator notes (``type: hub``, ``index``, ``dashboard``,
    ``moc``) are excluded — they're MOCs that link to everything and otherwise
    drown out primary content. Pass ``include_hubs=True`` to opt them back in.

    If the index is empty for the current model, prompts the user to run
    ``reindex_embeddings`` first.

    Args:
        query: Natural-language question or phrase (e.g. "stressed about deadlines").
        top_k: Number of results to return (1–50).
        filter_folder: Only return notes whose path starts with this folder
                       (e.g. "30_Episodic/" or "20_Projects/").
        include_hubs: If True, do NOT filter out hub/index/dashboard/moc notes.
        show_snippets: If True, include a ~200-char preview of the matching
                       chunk under each result. Helpful for triaging hits.
    """
    query = query.strip()
    if not query:
        return "err: empty query"
    top_k = max(1, min(int(top_k), 50))

    idx = get_vault_index()
    c = idx.conn

    where = "WHERE ne.model = ?"
    params: list = [emb.EMBED_MODEL]
    if filter_folder.strip():
        where += " AND ne.note_path LIKE ?"
        params.append(f"{filter_folder.strip().rstrip('/')}/%")
    if not include_hubs:
        placeholders = ",".join("?" * len(_HUB_TYPES))
        where += (
            f" AND (n.type IS NULL OR n.type NOT IN ({placeholders}))"
        )
        params.extend(_HUB_TYPES)

    rows = c.execute(
        f"SELECT ne.note_path, ne.chunk_id, ne.chunk_start, ne.chunk_end, "
        f"       ne.embedding, n.title, n.type "
        f"FROM note_embeddings ne "
        f"LEFT JOIN notes n ON n.path = ne.note_path "
        f"{where}",
        params,
    ).fetchall()
    if not rows:
        return (
            f"No embeddings found for model '{emb.EMBED_MODEL}'"
            f"{' in folder ' + filter_folder if filter_folder else ''}. "
            f"Run `reindex_embeddings` first."
        )

    try:
        q_vec = emb.embed_one(query)
    except emb.EmbeddingError as e:
        return f"Embedding query failed: {e}\n\n{emb.config_summary()}"

    # Score every chunk, keep the best chunk per note.
    best_per_note: dict[str, tuple[float, str, int, int, int]] = {}
    for r in rows:
        vec = emb.unpack(r["embedding"])
        score = emb.cosine(q_vec, vec)
        title = r["title"] or Path(r["note_path"]).stem
        prev = best_per_note.get(r["note_path"])
        if prev is None or score > prev[0]:
            best_per_note[r["note_path"]] = (
                score, title, r["chunk_id"], r["chunk_start"], r["chunk_end"],
            )

    scored = sorted(
        ((score, path, title, chunk_id, cs, ce)
         for path, (score, title, chunk_id, cs, ce) in best_per_note.items()),
        reverse=True,
    )

    out = [f"Semantic search for: {query!r}   (model: {emb.EMBED_MODEL})"]
    root = get_vault_root() if show_snippets else None
    for score, path, title, _chunk_id, cs, ce in scored[:top_k]:
        link = f"[[{path[:-3]}|{title}]]" if path.endswith(".md") else path
        out.append(f"{score:+.3f}  {link}")
        if show_snippets and root is not None:
            try:
                full = root / path
                raw = read_text(full)
                if raw:
                    _, body = split_frontmatter(raw)
                    snippet = body[cs:ce] if ce > cs else body[:400]
                    snippet = snippet.strip().replace("\n", " ")
                    if len(snippet) > 220:
                        snippet = snippet[:217] + "…"
                    out.append(f"        ↳ {snippet}")
            except OSError:
                pass
    return "\n".join(out)


def rank_link_candidates_from_text(
    body_text: str,
    self_path: str,
    top_k: int = 10,
    min_score: float = 0.45,
    include_hubs: bool = False,
) -> list[tuple[float, str, str]] | None:
    """Core ranking used by both the MCP tool and the write_note auto-trigger.

    Takes raw body text + the note's vault-relative path, embeds the text,
    cosine-ranks against the existing note_embeddings rows (excluding self,
    excluding wikilinks already in the body, excluding hub-type notes by
    default), and returns ``[(score, path, title), ...]`` sorted desc.

    Returns ``None`` if the embedding endpoint isn't reachable — callers
    should treat that as "skip silently, no suggestions today".
    """
    if not body_text or not body_text.strip():
        return []
    # Parse existing wikilinks from the body so we don't re-suggest them
    existing: set[str] = set()
    for link in extract_wikilinks(body_text):
        target_md = note_target_to_relative_md(link.get("target", ""))
        if target_md:
            existing.add(target_md)

    idx = get_vault_index()
    c = idx.conn
    where = "WHERE ne.model = ? AND ne.note_path != ?"
    params: list = [emb.EMBED_MODEL, self_path]
    if not include_hubs:
        placeholders = ",".join("?" * len(_HUB_TYPES))
        where += f" AND (n.type IS NULL OR n.type NOT IN ({placeholders}))"
        params.extend(_HUB_TYPES)
    rows = c.execute(
        f"SELECT ne.note_path, ne.chunk_id, ne.embedding, n.title, n.type "
        f"FROM note_embeddings ne "
        f"LEFT JOIN notes n ON n.path = ne.note_path "
        f"{where}",
        params,
    ).fetchall()
    if not rows:
        return []

    try:
        q_vec = emb.embed_one(body_text)
    except emb.EmbeddingError:
        return None  # endpoint unreachable; caller should skip silently

    best: dict[str, tuple[float, str]] = {}
    for r in rows:
        vec = emb.unpack(r["embedding"])
        score = emb.cosine(q_vec, vec)
        title = r["title"] or Path(r["note_path"]).stem
        prev = best.get(r["note_path"])
        if prev is None or score > prev[0]:
            best[r["note_path"]] = (score, title)

    out: list[tuple[float, str, str]] = []
    for p, (score, title) in best.items():
        if score < min_score:
            continue
        if p in existing:
            continue
        out.append((score, p, title))
    out.sort(reverse=True)
    return out[:top_k]


@mcp.tool()
def suggest_links_for(
    path: str,
    top_k: int = 10,
    min_score: float = 0.45,
    include_hubs: bool = False,
) -> str:
    """Suggest wikilinks for ``path`` based on semantic similarity.

    Reads the note, embeds it, and ranks all other indexed notes by cosine
    similarity. Filters out:
      - the note itself
      - notes already wikilinked from this note (extracted from its body)
      - hub/index/dashboard/moc notes (unless include_hubs=True)
      - results below ``min_score``

    Use this to grow the vault graph after writing a note — surfaces related
    content you forgot to link. Pairs nicely with ``add_wikilink``.

    Args:
        path: Vault-relative path to a Markdown note.
        top_k: Maximum suggestions to return.
        min_score: Cosine threshold (0–1). Default 0.45 filters weak matches.
        include_hubs: If True, allow hub/index notes in the suggestions.
    """
    note = safe_path(path)
    if not note.exists():
        return f"err: note not found: {path}"
    raw = read_text(note)
    if not raw:
        return f"err: note empty: {path}"
    _, body = split_frontmatter(raw)

    # Existing wikilinks targets (normalize to relative .md form for set membership)
    existing: set[str] = set()
    for link in extract_wikilinks(body):
        target_md = note_target_to_relative_md(link.get("target", ""))
        if target_md:
            existing.add(target_md)

    idx = get_vault_index()
    c = idx.conn
    rel = relative_to_vault(note)

    where = "WHERE ne.model = ? AND ne.note_path != ?"
    params: list = [emb.EMBED_MODEL, rel]
    if not include_hubs:
        placeholders = ",".join("?" * len(_HUB_TYPES))
        where += f" AND (n.type IS NULL OR n.type NOT IN ({placeholders}))"
        params.extend(_HUB_TYPES)

    rows = c.execute(
        f"SELECT ne.note_path, ne.chunk_id, ne.embedding, n.title, n.type "
        f"FROM note_embeddings ne "
        f"LEFT JOIN notes n ON n.path = ne.note_path "
        f"{where}",
        params,
    ).fetchall()
    if not rows:
        return ("No embeddings to compare against. "
                "Run `reindex_embeddings` first.")

    # Build a query vector from the note itself (use the same chunked text as
    # at index time — the *whole* note's average might dilute signal, but for
    # link suggestions whole-note feels right since we want "what else might
    # belong here as a link" not "where is this passage from").
    query_text = _note_text_for_embedding(note)
    if not query_text:
        return f"err: note has no embeddable text: {path}"
    try:
        q_vec = emb.embed_one(query_text)
    except emb.EmbeddingError as e:
        return f"Embedding the source note failed: {e}\n\n{emb.config_summary()}"

    best_per_note: dict[str, tuple[float, str]] = {}
    for r in rows:
        vec = emb.unpack(r["embedding"])
        score = emb.cosine(q_vec, vec)
        title = r["title"] or Path(r["note_path"]).stem
        prev = best_per_note.get(r["note_path"])
        if prev is None or score > prev[0]:
            best_per_note[r["note_path"]] = (score, title)

    # Drop already-linked + below-threshold
    candidates = []
    for p, (score, title) in best_per_note.items():
        if score < min_score:
            continue
        if p in existing:
            continue
        candidates.append((score, p, title))
    candidates.sort(reverse=True)

    if not candidates:
        return ("No new link candidates above threshold "
                f"({min_score:+.2f}). All similar notes are either already "
                "linked or below the score cutoff.")

    out = [
        f"Link suggestions for: {rel}",
        f"(score ≥ {min_score:+.2f}, excluding {len(existing)} already-linked)",
    ]
    for score, p, title in candidates[:top_k]:
        link = f"[[{p[:-3]}|{title}]]" if p.endswith(".md") else p
        out.append(f"{score:+.3f}  {link}")
    return "\n".join(out)
