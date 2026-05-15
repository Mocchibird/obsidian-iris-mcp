"""Embedding client and helpers for semantic search.

Talks to any OpenAI-compatible /v1/embeddings endpoint. Configurable via env:

    IRIS_EMBED_URL       default http://localhost:11434/v1/embeddings  (Ollama)
    IRIS_EMBED_MODEL     default nomic-embed-text
    IRIS_EMBED_API_KEY   default ""  (set for OpenAI proper)
    IRIS_EMBED_MAX_CHARS default 16000   (~4K tokens, well under nomic's 8K limit)
    IRIS_EMBED_TIMEOUT   default 60      (seconds)

LM Studio: set IRIS_EMBED_URL=http://localhost:1234/v1/embeddings.
OpenAI:    set IRIS_EMBED_URL=https://api.openai.com/v1/embeddings + IRIS_EMBED_API_KEY.

Vectors are stored as packed float32 BLOBs and cosine is computed in pure Python.
At <10K notes this is fast enough that a vector extension would be premature.
"""
from __future__ import annotations

import array
import math
import os

import httpx


EMBED_URL = os.environ.get("IRIS_EMBED_URL", "http://localhost:11434/v1/embeddings")
EMBED_MODEL = os.environ.get("IRIS_EMBED_MODEL", "nomic-embed-text")
EMBED_API_KEY = os.environ.get("IRIS_EMBED_API_KEY", "")
EMBED_MAX_CHARS = int(os.environ.get("IRIS_EMBED_MAX_CHARS", "16000"))
EMBED_TIMEOUT = int(os.environ.get("IRIS_EMBED_TIMEOUT", "60"))


class EmbeddingError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if EMBED_API_KEY:
        h["Authorization"] = f"Bearer {EMBED_API_KEY}"
    return h


def _post(payload: dict) -> dict:
    try:
        r = httpx.post(EMBED_URL, json=payload, headers=_headers(), timeout=EMBED_TIMEOUT)
    except httpx.HTTPError as e:
        raise EmbeddingError(
            f"Could not reach embed endpoint {EMBED_URL}: {e}. "
            f"Is Ollama running? Try `ollama serve` and `ollama pull {EMBED_MODEL}`."
        ) from e
    if r.status_code >= 400:
        raise EmbeddingError(f"Embed request failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def embed_one(text: str) -> list[float]:
    """Embed a single text. Truncates to EMBED_MAX_CHARS."""
    payload = {"model": EMBED_MODEL, "input": text[:EMBED_MAX_CHARS]}
    data = _post(payload)
    try:
        return data["data"][0]["embedding"]
    except (KeyError, IndexError) as e:
        raise EmbeddingError(f"Unexpected embed response: {data}") from e


def embed_batch(texts: list[str], batch_size: int = 16) -> list[list[float]]:
    """Embed many texts in batches. Returns vectors in input order."""
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = [t[:EMBED_MAX_CHARS] for t in texts[i : i + batch_size]]
        data = _post({"model": EMBED_MODEL, "input": chunk})
        try:
            out.extend(item["embedding"] for item in data["data"])
        except (KeyError, TypeError) as e:
            raise EmbeddingError(f"Unexpected batch response: {data}") from e
    return out


def pack(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def unpack(blob: bytes) -> list[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def config_summary() -> str:
    masked_key = "set" if EMBED_API_KEY else "unset"
    return (
        f"endpoint: {EMBED_URL}\n"
        f"model:    {EMBED_MODEL}\n"
        f"api_key:  {masked_key}\n"
        f"max_chars: {EMBED_MAX_CHARS}\n"
        f"timeout:   {EMBED_TIMEOUT}s"
    )
