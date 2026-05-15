"""Iris configuration — single source of truth for env vars + per-device overrides.

Precedence (highest first):
    1. Environment variable (e.g. IRIS_EMBED_URL)
    2. Per-device TOML at ~/.config/iris/config.toml (override path via IRIS_CONFIG=)
    3. Built-in default

Zero external deps — importable from both ``_iris/`` (the MCP server) and
``vault_cron.py`` (the standalone launchd runner) without dragging in mcp/httpx.

Example ~/.config/iris/config.toml:

    [vault]
    root = "~/obsidian-vaults/AI_Memory"

    [apple]
    reminders_list = "Vault"
    calendar_name  = "Vault"
    calendar_exclude = ["Vault", "Scheduled Reminders"]

    [focus.Work]
    projects = ["PTO Kernels"]
    tags     = ["huawei", "ascend"]

    [embed]
    url   = "http://localhost:11434/v1/embeddings"
    model = "nomic-embed-text"

    [llm]
    url   = "http://localhost:11434/v1/chat/completions"
    model = "gemma3:4b"
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any


_TOML_PATH = Path(os.environ.get("IRIS_CONFIG", "~/.config/iris/config.toml")).expanduser()


def _load_toml() -> dict:
    if _TOML_PATH.exists():
        try:
            with open(_TOML_PATH, "rb") as f:
                return tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            print(f"[iris_config] WARN: could not read {_TOML_PATH}: {e}", file=sys.stderr)
    return {}


_toml = _load_toml()


def _toml_get(path: tuple[str, ...], default: Any) -> Any:
    d: Any = _toml
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d if d is not None else default


def _str(env: str, toml_path: tuple[str, ...], default: str) -> str:
    v = os.environ.get(env)
    if v is not None and v != "":
        return v
    return str(_toml_get(toml_path, default))


def _int(env: str, toml_path: tuple[str, ...], default: int) -> int:
    v = os.environ.get(env)
    if v is not None and v != "":
        try:
            return int(v)
        except ValueError:
            pass
    return int(_toml_get(toml_path, default))


def _float(env: str, toml_path: tuple[str, ...], default: float) -> float:
    v = os.environ.get(env)
    if v is not None and v != "":
        try:
            return float(v)
        except ValueError:
            pass
    return float(_toml_get(toml_path, default))


# ── Vault ──────────────────────────────────────────────────────────────────
# Canonical env: IRIS_VAULT_ROOT. Legacy fallbacks: OBSIDIAN_VAULT_PATH (MCP side),
# VAULT_ROOT (cron side). Kept so existing setups don't break.
VAULT_ROOT: Path = Path(
    os.environ.get("IRIS_VAULT_ROOT")
    or os.environ.get("OBSIDIAN_VAULT_PATH")
    or os.environ.get("VAULT_ROOT")
    or _toml_get(("vault", "root"), "~/obsidian-vaults/AI_Memory")
).expanduser()


def vault_cache_dir() -> Path:
    return VAULT_ROOT / ".ai_memory_cache"


def vault_db_path() -> Path:
    return vault_cache_dir() / "vault.db"


# ── Apple integration ──────────────────────────────────────────────────────
REMINDERS_LIST: str = _str("IRIS_REMINDERS_LIST", ("apple", "reminders_list"), "Vault")
CALENDAR_NAME: str = _str("IRIS_CALENDAR_NAME", ("apple", "calendar_name"), "Vault")
CALENDAR_EXCLUDE: set[str] = set(_toml_get(
    ("apple", "calendar_exclude"),
    ["Vault", "Scheduled Reminders", "Siri Suggestions",
     "Schweizerische Feiertage", "Birthdays"],
))

FOCUS_CONTEXT: dict[str, dict[str, list[str]]] = _toml_get("focus", {  # type: ignore[arg-type]
    "Work":     {"projects": ["PTO Kernels"], "tags": ["huawei", "ascend"]},
    "Personal": {"projects": ["Homelab", "TrueNAS Migration", "MochiMind"], "tags": ["personal"]},
    "Study":    {"projects": ["Japanese Study", "Languages"], "tags": ["ethz", "japanese"]},
}) if isinstance(_toml.get("focus"), dict) else {
    "Work":     {"projects": ["PTO Kernels"], "tags": ["huawei", "ascend"]},
    "Personal": {"projects": ["Homelab", "TrueNAS Migration", "MochiMind"], "tags": ["personal"]},
    "Study":    {"projects": ["Japanese Study", "Languages"], "tags": ["ethz", "japanese"]},
}


# ── Embeddings ─────────────────────────────────────────────────────────────
EMBED_URL: str = _str("IRIS_EMBED_URL", ("embed", "url"), "http://localhost:11434/v1/embeddings")
EMBED_MODEL: str = _str("IRIS_EMBED_MODEL", ("embed", "model"), "nomic-embed-text")
EMBED_API_KEY: str = _str("IRIS_EMBED_API_KEY", ("embed", "api_key"), "")
EMBED_MAX_CHARS: int = _int("IRIS_EMBED_MAX_CHARS", ("embed", "max_chars"), 6000)
EMBED_TIMEOUT: int = _int("IRIS_EMBED_TIMEOUT", ("embed", "timeout"), 60)


# ── LLM (chat completions) ─────────────────────────────────────────────────
# No model default — set IRIS_LLM_MODEL or [llm].model to enable LLM-using features.
LLM_URL: str = _str("IRIS_LLM_URL", ("llm", "url"), "http://localhost:11434/v1/chat/completions")
LLM_MODEL: str = _str("IRIS_LLM_MODEL", ("llm", "model"), "")
LLM_API_KEY: str = _str("IRIS_LLM_API_KEY", ("llm", "api_key"), "")
LLM_MAX_TOKENS: int = _int("IRIS_LLM_MAX_TOKENS", ("llm", "max_tokens"), 1024)
LLM_TEMPERATURE: float = _float("IRIS_LLM_TEMPERATURE", ("llm", "temperature"), 0.7)
LLM_TIMEOUT: int = _int("IRIS_LLM_TIMEOUT", ("llm", "timeout"), 120)


def config_summary() -> str:
    """Human-readable dump of the active config — useful in status tools."""
    loaded = f"loaded from {_TOML_PATH}" if _toml else f"no TOML found at {_TOML_PATH}"
    return (
        f"=== iris_config ({loaded}) ===\n"
        f"vault.root       = {VAULT_ROOT}\n"
        f"apple.reminders  = {REMINDERS_LIST}\n"
        f"apple.calendar   = {CALENDAR_NAME}\n"
        f"apple.exclude    = {sorted(CALENDAR_EXCLUDE)}\n"
        f"focus.modes      = {sorted(FOCUS_CONTEXT.keys())}\n"
        f"embed.url        = {EMBED_URL}\n"
        f"embed.model      = {EMBED_MODEL}\n"
        f"embed.api_key    = {'set' if EMBED_API_KEY else 'unset'}\n"
        f"llm.url          = {LLM_URL}\n"
        f"llm.model        = {LLM_MODEL or '(unset)'}\n"
        f"llm.api_key      = {'set' if LLM_API_KEY else 'unset'}"
    )


def config_path() -> Path:
    return _TOML_PATH


def is_config_loaded() -> bool:
    return bool(_toml)
