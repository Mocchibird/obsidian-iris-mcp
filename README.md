# Iris — Obsidian Memory MCP Server

> Turn your Obsidian vault into long-term memory for Claude (or any MCP client).

Iris is an [MCP](https://modelcontextprotocol.io) server that indexes an Obsidian vault into SQLite and exposes ~130 tools for searching, editing, linking, and analysing it. It can:

- Full-text search notes (FTS5), backlinks, tag co-occurrence
- Add / complete tasks and reminders inside `.md` notes
- Schedule calendar events (incl. cross-day and all-day) in daily notes
- Sync tasks/reminders/events with Apple Reminders & Calendar (macOS)
- Track anime watch lists with MyAnimeList sync
- Manage people/contacts, vocabulary, warranties, etc., in dedicated SQLite tables
- Find broken links, duplicate notes, orphan attachments, merge candidates
- Render live SQL queries inside Obsidian via the SQLite DB plugin (with optional clickable wikilinks)

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
        "OBSIDIAN_VAULT_PATH": "/absolute/path/to/your/Obsidian/vault"
      }
    }
  }
}
```

Restart Claude Desktop. Iris will index the vault on first launch and write a SQLite cache to `<vault>/.ai_memory_cache/vault.db`.

### 3. (Optional) Install the Obsidian companion plugin

The companion plugin gives you two things inside Obsidian:

- A `Reload SQLite DB` command (assignable to a hotkey) — used by the MCP to refresh live SQL views after writes.
- Clickable wikilinks inside SQL table cells (e.g. names in `People.md` link to each person's note).

```bash
cp -r plugins/sqlite-db-reload <your-vault>/.obsidian/plugins/
```

Then in Obsidian: **Settings → Community Plugins → Installed Plugins**, enable **SQLite DB Companion**. Requires the upstream [SQLite DB plugin](https://github.com/stfrigerio/sqliteDB) for the SQL rendering itself.

### 4. (Optional) Apple Reminders / Calendar sync

The `vault_cron.py` script (run from a launchd job or manually) syncs tasks and reminders bidirectionally with Apple Reminders, and pulls Calendar events into daily notes' `## Schedule` sections. macOS only.

```bash
./vault_cron.py --sync
```

### 5. (Optional) Semantic search

Iris can rank notes by meaning, not just keyword. It calls any OpenAI-compatible `/v1/embeddings` endpoint — so the same code works against [Ollama](https://ollama.com/), LM Studio, or the OpenAI API.

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

## Configuration

All Iris config — vault path, Apple list names, focus mappings, embedding/LLM endpoints — lives in [`iris_config.py`](iris_config.py). Precedence: **env var > `~/.config/iris/config.toml` > built-in default**.

Per-device overrides go in `~/.config/iris/config.toml`. Keep this file *outside* your synced vault folder so each device can point at its own paths:

```toml
[vault]
root = "~/obsidian-vaults/AI_Memory"

[apple]
reminders_list = "Vault"
calendar_name  = "Vault"

[embed]
url   = "http://localhost:11434/v1/embeddings"
model = "nomic-embed-text"

[llm]
url   = "http://localhost:11434/v1/chat/completions"
model = "gemma3:4b"   # unset = LLM-using features (prose summaries, etc.) are disabled
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
└── tools/
    ├── sqlite.py               # sqlite_query, sqlite_schema, reload_sqlite_db_plugin
    ├── files.py                # file CRUD, bulk replace, smart move
    ├── notes.py                # note CRUD, frontmatter, tags, templates
    ├── search.py               # search_vault, search_vault_text, find_similar
    ├── tasks.py                # tasks + reminders
    ├── calendar.py             # schedule_event, daily_agenda, Apple Calendar
    ├── links.py                # find_issues, link_candidates, duplicates
    ├── analysis.py             # vault_overview, note_context, merge_candidates
    ├── people.py / vocab.py / warranties.py / anime.py
    ├── import_export.py        # import_file, mass_import, triage_inbox
    ├── routines.py             # morning_briefing, evening_wrapup, weekly_review
    └── web.py                  # web_search, fetch_url, search_reddit
vault_cron.py                   # Apple Reminders/Calendar bidirectional sync
plugins/sqlite-db-reload/       # Obsidian companion plugin (copy into your vault)
```

The MCP server keeps **`.md` files as the source of truth**. SQLite is a disposable cache rebuilt from disk; you can delete `vault.db` at any time and Iris re-indexes on next startup. A handful of domain tables (`people`, `anime_list`, `vocab`, `warranties`) are SQLite-native — they're rendered in `.md` via the SQLite DB plugin instead of being parsed back.

## Tool overview

Iris exposes ~130 tools. Some highlights:

| Tool | Purpose |
|---|---|
| `search_vault(query)` | Unified search (FTS + alias + title + tag + tag-cooccurrence expansion) |
| `read_note(path)` | Read a note; tracks access for "hotness" ranking |
| `write_note(path, content)` | Write a note; snapshots prior content as a revision |
| `vault_overview()` | Structural map: folders, tags, recent, hot, stale |
| `note_context(path)` | Full neighborhood for a note: backlinks, tag siblings, revisions |
| `find_issues(checks=…)` | Broken links, duplicates, orphan attachments, link mismatches |
| `find_merge_candidates(folder)` | Vault-wide similarity scoring for potential duplicate notes |
| `sqlite_query(sql)` | Read-only SELECT against any table/view |
| `sqlite_schema(table?)` | Discover columns of any table or view |
| `schedule_event(...)` | Add a calendar event to a daily note's `## Schedule` |
| `daily_agenda(date)` | Tasks + reminders + events for a date, including cross-day events |
| `morning_briefing()` | Compact "what's on today" summary |
| `evening_wrapup()` | Capture-then-archive flow for the day |
| `weekly_review()` | 7-day overview with stale tasks, missed reminders, capture stats |

## Why the SQLite-backed approach

- **The model can navigate a vault it doesn't fit in its context.** A 1000-note vault is far too big to read end-to-end, but the SQLite index lets the model answer "what notes are tagged `huawei` AND mention `kernel`?" or "which `type: project` notes were touched in the last 30 days?" in one query.
- **Live SQL views inside Obsidian.** Domain data (people, anime, warranties) renders as live tables in `.md` notes via the [SQLite DB plugin](https://github.com/stfrigerio/sqliteDB). With the companion plugin, name cells become clickable internal links.
- **Write tools are validated.** Each `*_upsert` enforces schema constraints (e.g. anime upsert verifies the MAL ID matches the supplied title before writing). Generic `sqlite_query` is read-only — no `INSERT/UPDATE/DELETE/DROP/ALTER`.

## Privacy

- Everything runs locally. Vault data never leaves your machine.
- The MAL OAuth credentials, if you enable that integration, are stored at `<vault>/.ai_memory_cache/mal_auth.json` — outside this repo.
- The SQLite cache (`<vault>/.ai_memory_cache/vault.db`) is gitignored.

## Platform support

- **macOS 13+**: full feature set including Apple Reminders/Calendar sync
- **Linux/Windows**: core MCP works; Apple integration and OS URI handlers (for the companion plugin's reload feature) are stubbed but untested

## License

MIT — see [LICENSE](LICENSE).
