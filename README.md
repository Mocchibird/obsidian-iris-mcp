# Iris — Obsidian Memory MCP Server

> Turn your Obsidian vault into long-term memory for Claude (or any MCP client).

Iris is an [MCP](https://modelcontextprotocol.io) server that indexes an Obsidian vault into SQLite and exposes ~130 tools for searching, editing, linking, and analysing it. It can:

- Full-text search notes (FTS5), backlinks, tag co-occurrence, semantic search
- Add and complete tasks and reminders inside `.md` notes
- Schedule calendar events (including cross-day and all-day) in daily notes
- Generate morning briefings, evening wrap-ups, and weekly summaries
- Find broken links, duplicate notes, orphan attachments, merge candidates
- Render live SQL queries inside Obsidian via the SQLite DB plugin
- Run as a Discord bot through Docker — chat with Iris from anywhere using your Claude subscription
- _(macOS only)_ Sync tasks, reminders, and calendar events with Apple Reminders & Calendar
- _(optional)_ Track anime watch lists with MyAnimeList sync

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/Mocchibird/obsidian-iris-mcp.git
cd obsidian-iris-mcp
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Point Claude Desktop at it

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "iris": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/obsidian_memory_mcp.py"],
      "env": {
        "IRIS_VAULT_ROOT": "/absolute/path/to/your/Obsidian/vault"
      }
    }
  }
}
```

Restart Claude Desktop. Iris will index the vault on first launch and write a SQLite cache to `<vault>/.ai_memory_cache/vault.db`.

### 3. (Optional) Install the Obsidian companion plugin

The companion plugin adds two things inside Obsidian:

- A `Reload SQLite DB` command (assignable to a hotkey) — used by Iris to refresh live SQL views after writes.
- Clickable wikilinks inside SQL table cells.

```bash
cp -r plugins/sqlite-db-reload <your-vault>/.obsidian/plugins/
```

Then in Obsidian: **Settings → Community Plugins → Installed Plugins**, enable **SQLite DB Companion**. Requires the upstream [SQLite DB plugin](https://github.com/stfrigerio/sqliteDB) for the SQL rendering itself.

### 4. (Optional) Semantic search

Iris can rank notes by meaning, not just keyword. She calls any OpenAI-compatible `/v1/embeddings` endpoint, so the same code works against [Ollama](https://ollama.com/), LM Studio, or the OpenAI API.

```bash
# one-time setup with Ollama (free, fully local)
brew install ollama
ollama serve &
ollama pull nomic-embed-text
```

In Claude:

```
> reindex_embeddings()        # one-time bulk index (~1–5 min for ~600 notes)
> semantic_search("stressed about deadlines", top_k=5)
> embedding_status()          # health check
```

### 5. (Optional) Run Iris as a Discord bot via Docker

You can run Iris in a Docker container that connects to Discord and uses **your Claude subscription** (no Anthropic API key needed). The bot supports proactive features: morning briefings, evening wrap-ups, event/reminder pings, snooze reactions, and per-day timezone overrides for travel. See [`docker/README.md`](docker/README.md) for full setup.

## Configuration

All Iris config — vault path, embedding/LLM endpoints, etc. — lives in [`iris_config.py`](iris_config.py). Precedence: **env var > `~/.config/iris/config.toml` > built-in default**.

Per-device overrides go in `~/.config/iris/config.toml`. Keep this file *outside* your synced vault folder so each device can point at its own paths:

```toml
[vault]
root = "~/obsidian-vaults/AI_Memory"

[embed]
url   = "http://localhost:11434/v1/embeddings"
model = "nomic-embed-text"

[llm]
url   = "http://localhost:11434/v1/chat/completions"
model = "gemma3:4b"   # unset = LLM-using features (prose summaries, etc.) disabled
```

Or as env vars:

| Variable                  | Default                                          |
|---------------------------|--------------------------------------------------|
| `IRIS_VAULT_ROOT`         | `~/obsidian-vaults/AI_Memory`                    |
| `IRIS_EMBED_URL`          | `http://localhost:11434/v1/embeddings`           |
| `IRIS_EMBED_MODEL`        | `nomic-embed-text`                               |
| `IRIS_EMBED_API_KEY`      | _(unset; set for OpenAI)_                        |
| `IRIS_LLM_URL`            | `http://localhost:11434/v1/chat/completions`     |
| `IRIS_LLM_MODEL`          | _(unset — LLM features disabled until set)_      |
| `IRIS_LLM_API_KEY`        | _(unset)_                                        |
| `IRIS_CONFIG`             | `~/.config/iris/config.toml` _(TOML file path)_  |

Legacy env vars `OBSIDIAN_VAULT_PATH` and `VAULT_ROOT` still work as fallbacks.

## Architecture

```
obsidian_memory_mcp.py          # entry-point shim (Claude Desktop launches this)
_iris/
├── __init__.py                 # FastMCP instance
├── core.py                     # helpers + VaultIndex (SQLite schema/sync/queries)
├── embeddings.py               # OpenAI-compatible embedding client
├── llm.py                      # OpenAI-compatible chat client
└── tools/
    ├── sqlite.py               # sqlite_query, sqlite_schema, reload_sqlite_db_plugin
    ├── files.py                # file CRUD, bulk replace, smart move
    ├── notes.py                # note CRUD, frontmatter, tags, templates
    ├── search.py               # search_vault, search_vault_text, find_similar
    ├── semantic.py             # semantic_search, suggest_links_for, reindex_embeddings
    ├── tasks.py                # tasks + reminders, carry-forward
    ├── calendar.py             # schedule_event, daily_agenda
    ├── links.py                # find_issues, link_candidates, duplicates
    ├── analysis.py             # vault_overview, note_context, merge_candidates
    ├── import_export.py        # import_file, mass_import, triage_inbox, summarize_note_with_llm
    ├── routines.py             # morning_briefing, evening_wrapup, weekly_review
    └── web.py                  # web_search, fetch_url, search_reddit
vault_cron.py                   # standalone CLI: capture, weekly-summary, (macOS) Apple sync
docker/                         # Discord bot deployment
plugins/sqlite-db-reload/       # Obsidian companion plugin (copy into your vault)
```

The MCP server keeps **`.md` files as the source of truth**. SQLite is a disposable cache rebuilt from disk; you can delete `vault.db` at any time and Iris re-indexes on next startup.

## Tool overview

Iris exposes ~130 tools. Some highlights:

| Tool | Purpose |
|---|---|
| `search_vault(query)` | Unified search (FTS + alias + title + tag + tag-cooccurrence expansion) |
| `semantic_search(query)` | Embedding-based ranking; finds notes by meaning |
| `read_note(path)` | Read a note; tracks access for "hotness" ranking |
| `write_note(path, content)` | Write a note; snapshots prior content as a revision |
| `vault_overview()` | Structural map: folders, tags, recent, hot, stale |
| `note_context(path)` | Full neighborhood for a note: backlinks, tag siblings, revisions |
| `suggest_links_for(path)` | Semantic-ranked wikilink suggestions for a note |
| `find_issues(checks=…)` | Broken links, duplicates, orphan attachments, link mismatches |
| `find_merge_candidates(folder)` | Vault-wide similarity scoring for potential duplicate notes |
| `sqlite_query(sql)` | Read-only SELECT against any table/view |
| `schedule_event(...)` | Add a calendar event to a daily note's `## Schedule` |
| `daily_agenda(date)` | Tasks + reminders + events for a date, including cross-day events |
| `list_unfinished_tasks()` | What's still open from recent days |
| `carry_forward_tasks()` | Move missed items to today's daily note |
| `morning_briefing()` | "What's on today" summary |
| `evening_wrapup()` | End-of-day capture-and-archive flow |
| `weekly_review()` / `weekly_summary()` | 7-day overview and persisted weekly note |

## Why the SQLite-backed approach

- **The model can navigate a vault it doesn't fit in its context.** A 1000-note vault is far too big to read end-to-end, but the SQLite index lets the model answer "what notes are tagged `<X>` AND mention `<Y>`?" or "which `type: project` notes were touched in the last 30 days?" in one query.
- **Live SQL views inside Obsidian.** Domain tables render as live tables in `.md` notes via the [SQLite DB plugin](https://github.com/stfrigerio/sqliteDB). With the companion plugin, name cells become clickable internal links.
- **Write tools are validated.** Each `*_upsert` enforces schema constraints. Generic `sqlite_query` is read-only — no `INSERT/UPDATE/DELETE/DROP/ALTER`.

## Privacy

- Everything runs locally — vault data never leaves your machine unless you point an external API (e.g. OpenAI embeddings) at it.

## Platform support

- **macOS 13+**: full feature set including the optional Apple integrations.
- **Linux / Docker**: core MCP and the Discord bot work; Apple integrations are macOS-only.
- **Windows**: core MCP works; OS-specific helpers are untested.

---

## Optional integrations

### Apple Reminders / Calendar / Health (macOS only)

`vault_cron.py` syncs tasks/reminders bidirectionally with Apple Reminders, pulls Calendar events into daily notes' `## Schedule`, and (with a user-defined Apple Shortcut) writes a daily Health snapshot into each daily note. macOS only — uses `osascript` under the hood.

```bash
./vault_cron.py sync                     # bidirectional with Apple Reminders/Calendar
./vault_cron.py pull-calendar            # Apple Calendar → daily note
./vault_cron.py health                   # run the user-defined "Iris Health" Shortcut
./vault_cron.py morning                  # full morning routine
```

Schedule via launchd if you want it to run automatically (e.g. 09:30 every day).

### MyAnimeList sync

Iris can mirror your MAL watch list into the vault and sync changes back. One-time OAuth setup:

1. Register an app at https://myanimelist.net/apiconfig (type Other, redirect `http://localhost:8765/callback`).
2. Save `{"client_id": "...", "client_secret": "..."}` to `<vault>/.ai_memory_cache/mal_auth.json`.
3. Call the `mal_auth_start` MCP tool from Claude to complete the OAuth flow.

After that, `anime_pull_from_mal`, `anime_push_to_mal`, `mal_search`, `mal_seasonal`, etc. are all available.

## License

MIT — see [LICENSE](LICENSE).
