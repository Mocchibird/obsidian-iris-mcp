"""LLM chat-completions client.

Mirror of ``_iris/embeddings.py`` but for /v1/chat/completions. Talks to any
OpenAI-compatible endpoint — Ollama (default), LM Studio, hosted OpenAI, etc.
All config lives in ``iris_config``.

The module itself doesn't drive any user-facing feature; it's plumbing for
future tools that want LLM prose (auto-summaries, smart triage, weekly digest
narratives, etc.). Importing it is cheap and has no side effects.

Typical use:

    from _iris.llm import chat
    reply = chat([
        {"role": "system", "content": "Summarize in one paragraph."},
        {"role": "user",   "content": "..."},
    ])

If no model is configured (IRIS_LLM_MODEL / [llm].model is unset) ``chat`` raises
``LLMNotConfigured`` — callers can catch and skip prose-generation gracefully.
"""
from __future__ import annotations

import httpx

import iris_config as cfg


# Re-exported so call sites don't need to import iris_config
LLM_URL = cfg.LLM_URL
LLM_MODEL = cfg.LLM_MODEL
LLM_API_KEY = cfg.LLM_API_KEY
LLM_MAX_TOKENS = cfg.LLM_MAX_TOKENS
LLM_TEMPERATURE = cfg.LLM_TEMPERATURE
LLM_TIMEOUT = cfg.LLM_TIMEOUT


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    """Raised when no chat model is configured (IRIS_LLM_MODEL unset)."""


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        h["Authorization"] = f"Bearer {LLM_API_KEY}"
    return h


def is_configured() -> bool:
    return bool(LLM_MODEL)


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: int | None = None,
) -> str:
    """Send chat completion. Returns the assistant's reply text.

    Raises LLMNotConfigured if no model is set; LLMError on transport/parse issues.
    """
    use_model = model or LLM_MODEL
    if not use_model:
        raise LLMNotConfigured(
            "No LLM model configured. Set IRIS_LLM_MODEL or [llm].model in "
            "~/.config/iris/config.toml (e.g. 'gemma3:4b' for Ollama or LM Studio)."
        )
    payload: dict = {
        "model": use_model,
        "messages": messages,
        "max_tokens": max_tokens if max_tokens is not None else LLM_MAX_TOKENS,
        "temperature": temperature if temperature is not None else LLM_TEMPERATURE,
        "stream": False,
    }
    try:
        r = httpx.post(LLM_URL, json=payload, headers=_headers(),
                       timeout=timeout if timeout is not None else LLM_TIMEOUT)
    except httpx.HTTPError as e:
        raise LLMError(
            f"Could not reach chat endpoint {LLM_URL}: {e}. "
            f"Is Ollama / LM Studio running? Configured model: {use_model!r}."
        ) from e
    if r.status_code >= 400:
        raise LLMError(f"Chat request failed ({r.status_code}): {r.text[:400]}")
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Unexpected chat response shape: {data}") from e


def config_summary() -> str:
    return (
        f"endpoint:    {LLM_URL}\n"
        f"model:       {LLM_MODEL or '(unset — LLM features disabled)'}\n"
        f"api_key:     {'set' if LLM_API_KEY else 'unset'}\n"
        f"max_tokens:  {LLM_MAX_TOKENS}\n"
        f"temperature: {LLM_TEMPERATURE}\n"
        f"timeout:     {LLM_TIMEOUT}s"
    )
