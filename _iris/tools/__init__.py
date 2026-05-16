"""Tool registry — importing this package registers every @mcp.tool() with the FastMCP server.

Adding a new tool module:
    1. Create _iris/tools/<name>.py
    2. Import the shared mcp instance: ``from .. import mcp``
    3. Define your @mcp.tool() functions

That's it. This file auto-discovers every sibling module via pkgutil and imports
it, which triggers the @mcp.tool() decorators at import time. No registry edit
needed when adding tools.

Skipped: names starting with ``_`` (treated as private/internal).
"""
from __future__ import annotations

import importlib
import pkgutil


_AUTOLOAD_SKIP = set()  # add names here if a module ever needs to be excluded


def _autoload_tools() -> list[str]:
    loaded: list[str] = []
    for mod_info in pkgutil.iter_modules(__path__):  # noqa: F821 — pkg __path__
        name = mod_info.name
        if name.startswith("_") or name in _AUTOLOAD_SKIP:
            continue
        importlib.import_module(f"{__name__}.{name}")
        loaded.append(name)
    return loaded


_LOADED_TOOL_MODULES = _autoload_tools()
