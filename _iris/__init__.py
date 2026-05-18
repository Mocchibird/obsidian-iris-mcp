"""Iris — Obsidian vault memory MCP server.

Package layout:
    _iris/
        __init__.py         # FastMCP instance (this file)
        core.py             # all shared helpers + VaultIndex (SQLite schema/sync/queries)
        embeddings.py       # OpenAI-compatible embedding client
        llm.py              # OpenAI-compatible chat client
        tools/
            __init__.py     # auto-discovers + imports every sibling module
            analysis.py     # vault_overview, note_context, merge_candidates
            anime.py        # anime_list table + full MAL OAuth sync
            calendar.py     # schedule_event, daily_agenda, evening_wrapup, ical sync
            discord.py      # fetch_discord_history, embed_* cards, pingbacks
            files.py        # file CRUD, bulk replace, smart move
            health.py       # meal/weight logging, BMR/TDEE, daily/weekly summaries
            training.py     # skill goals, injuries, training sessions — injury-aware coaching
            habits.py       # daily habit tracker + GitHub-style heatmap + reminder loop
            import_export.py# import_file, mass_import, triage_inbox, summarize_note_with_llm
            links.py        # find_issues, link_candidates, duplicates
            notes.py        # note CRUD, frontmatter, tags, templates
            people.py       # people_upsert with occupation/employer/contact columns
            routines.py     # morning_briefing, weekly_review
            search.py       # search_vault, find_similar, semantic_search dispatcher
            semantic.py     # semantic_search, suggest_links_for, reindex_embeddings
            sqlite.py       # sqlite_query, sqlite_schema, refresh_sql_views, vault_snapshot
            tasks.py        # tasks + reminders, carry-forward
            vocab.py        # vocab_upsert, vocab_review (SM-2 spaced repetition)
            warranties.py   # warranty tracking with expiry alerts
            web.py          # web_search, fetch_url, search_reddit

The entry-point `obsidian_memory_mcp.py` at the project root is a thin shim that
imports this package and calls ``mcp.run()``.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("obsidian-memory")
