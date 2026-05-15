#!/usr/bin/env python3
"""Iris MCP server — thin entry-point shim.

All the actual code lives in the `_iris/` package. This file exists so the
existing Claude Desktop config (which points at obsidian_memory_mcp.py) keeps
working without any change.

Package layout:
    _iris/__init__.py       — FastMCP instance
    _iris/core.py           — helpers + VaultIndex
    _iris/tools/*.py        — @mcp.tool() definitions, one file per domain
"""
from __future__ import annotations

from _iris import mcp
from _iris import tools  # noqa: F401  — import triggers @mcp.tool() registration


if __name__ == "__main__":
    mcp.run()
