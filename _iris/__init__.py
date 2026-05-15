"""Iris — Obsidian vault memory MCP server.

Package layout:
    _iris/
        __init__.py         # FastMCP instance (this file)
        config.py           # constants, ignored patterns
        helpers.py          # safe_path, read_text, split_frontmatter, …
        vault_index.py      # VaultIndex class + global accessor
        tools/
            __init__.py     # imports every tool module → triggers @mcp.tool() registration
            sqlite.py
            files.py
            notes.py
            search.py
            tasks.py
            calendar.py
            links.py
            analysis.py
            people.py
            anime.py
            vocab.py
            warranties.py
            import_export.py
            routines.py
            web.py

The entry-point `obsidian_memory_mcp.py` at the project root is a thin shim that
imports this package and calls ``mcp.run()``.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("obsidian-memory")
