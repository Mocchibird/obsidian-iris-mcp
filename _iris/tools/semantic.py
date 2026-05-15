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
    get_vault_index,
    get_vault_root,
    is_ignored_path,
    read_text,
    split_frontmatter,
    title_from_text,
)
from .. import embeddings as emb


def _note_text_for_embedding(path: Path) -> str:
    """Build the text fed to the embedding model: title + body (no frontmatter)."""
    raw = read_text(path)
    if not raw:
        return ""
    _, body = split_frontmatter(raw)
    title = title_from_text(body, fallback=path.stem)
    return f"{title}\n\n{body}".strip()


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
        "SELECT COUNT(*) FROM note_embeddings WHERE model = ?", (emb.EMBED_MODEL,)
    ).fetchone()[0]
    stale = c.execute(
        "SELECT COUNT(*) FROM note_embeddings ne "
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
    to_embed: list[tuple[str, str, str]] = []  # (path, content_hash, text)
    for r in rows:
        full = root / r["path"]
        if not full.exists() or is_ignored_path(full):
            continue
        text = _note_text_for_embedding(full)
        if not text.strip():
            continue
        to_embed.append((r["path"], r["content_hash"], text))

    if not to_embed:
        return "No embeddable notes found (all empty or ignored)."

    # Embed in batches; commit per-batch so partial progress is durable.
    batch_size = 16
    done = 0
    failed = 0
    now = datetime.now().isoformat(timespec="seconds")
    for i in range(0, len(to_embed), batch_size):
        chunk = to_embed[i : i + batch_size]
        texts = [t for _, _, t in chunk]
        try:
            vecs = emb.embed_batch(texts, batch_size=batch_size)
        except emb.EmbeddingError as e:
            return (
                f"Embedding failed after {done} notes: {e}\n\n"
                f"Config:\n{emb.config_summary()}"
            )
        if len(vecs) != len(chunk):
            failed += len(chunk)
            continue
        for (path, content_hash, _), vec in zip(chunk, vecs):
            c.execute(
                "INSERT OR REPLACE INTO note_embeddings "
                "(note_path, model, content_hash, dim, embedding, embedded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (path, emb.EMBED_MODEL, content_hash, len(vec), emb.pack(vec), now),
            )
        c.commit()
        done += len(chunk)

    return (
        f"Embedded {done} note(s) with model '{emb.EMBED_MODEL}'"
        + (f" ({failed} failed)" if failed else "")
        + f". Endpoint: {emb.EMBED_URL}"
    )


@mcp.tool()
def semantic_search(query: str, top_k: int = 10, filter_folder: str = "") -> str:
    """Find notes semantically related to ``query`` using cosine similarity.

    Embeds the query with the same model used for the index and ranks all
    indexed notes by similarity. Returns the top_k matches with scores.

    If the index is empty for the current model, prompts the user to run
    ``reindex_embeddings`` first.

    Args:
        query: Natural-language question or phrase (e.g. "stressed about deadlines").
        top_k: Number of results to return (1–50).
        filter_folder: Only return notes whose path starts with this folder
                       (e.g. "30_Episodic/" or "20_Projects/").
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

    rows = c.execute(
        f"SELECT ne.note_path, ne.embedding, n.title "
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

    scored: list[tuple[float, str, str]] = []
    for r in rows:
        vec = emb.unpack(r["embedding"])
        score = emb.cosine(q_vec, vec)
        title = r["title"] or Path(r["note_path"]).stem
        scored.append((score, r["note_path"], title))
    scored.sort(reverse=True)

    out = [f"Semantic search for: {query!r}   (model: {emb.EMBED_MODEL})"]
    for score, path, title in scored[:top_k]:
        link = f"[[{path[:-3]}|{title}]]" if path.endswith(".md") else path
        out.append(f"{score:+.3f}  {link}")
    return "\n".join(out)
