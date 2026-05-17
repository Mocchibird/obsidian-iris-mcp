# Iris — Obsidian Memory MCP Server

> Turn your Obsidian vault into long-term memory for Claude (or any MCP client).

**About the name.** Iris is both:

- An acronym — **I**ntelligent **R**ecall & **I**ndexing **S**ystem.
- A reference to Greek mythology — Iris (Ἶρις) is the messenger goddess and the personification of the rainbow, the bridge that lets gods and humans speak to each other. This project plays the same role: the bridge between you and your vault, carrying messages, looking things up, and tying the world above (Claude, your conversations) to the world below (your notes on disk).

## What Iris does

Iris is an [MCP](https://modelcontextprotocol.io) server that indexes an Obsidian vault into SQLite and exposes ~140 tools for searching, editing, linking, and analysing it. Highlights:

- Full-text search, semantic search, backlinks, tag co-occurrence
- Add and complete tasks and reminders inside your `.md` notes
- Schedule calendar events (including cross-day, all-day, per-event ping lead-times) in daily notes
- Generate morning briefings, evening wrap-ups, weekly summaries
- Find broken links, duplicate notes, orphan attachments, merge candidates
- Render live SQL queries inside Obsidian via the SQLite DB plugin
- Run as a **Discord bot** through Docker — chat with Iris from anywhere using your Claude subscription
- _(optional)_ Track anime watch lists with MyAnimeList sync

---

## Quick start

The minimum to talk to Iris through any MCP client (Claude Desktop, Claude Code, LM Studio, …).

### Step 1 — Clone and install

```bash
git clone https://github.com/Mocchibird/obsidian-iris-mcp.git
cd obsidian-iris-mcp
python3.11 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .
```

This installs Iris into a local virtualenv. Note the **absolute paths** to:

- the venv's Python: usually `<your-clone>/.venv/bin/python` (Windows: `…\.venv\Scripts\python.exe`)
- the entry-point script: `<your-clone>/obsidian_memory_mcp.py`
- your Obsidian vault: wherever it lives on disk

You'll plug these into your MCP client in step 2.

### Step 2 — Add Iris to your MCP client

Every MCP client speaks the same JSON config shape. The fragment you'll add is:

```json
{
  "command": "/absolute/path/to/.venv/bin/python",
  "args": ["/absolute/path/to/obsidian_memory_mcp.py"],
  "env": {
    "IRIS_VAULT_ROOT": "/absolute/path/to/your/Obsidian/vault"
  }
}
```

Where to put that fragment depends on the client:

<details>
<summary><strong>Claude Desktop</strong></summary>

Open Settings → Developer → Edit Config, or edit the file directly. Wrap the fragment in the `mcpServers` block:

```json
{
  "mcpServers": {
    "iris": { /* fragment from above */ }
  }
}
```

Config file location (managed for you by the Settings UI):
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Restart Claude Desktop after saving.
</details>

<details>
<summary><strong>Claude Code (CLI)</strong></summary>

```bash
claude mcp add iris \
  --command /absolute/path/to/.venv/bin/python \
  --args /absolute/path/to/obsidian_memory_mcp.py \
  --env IRIS_VAULT_ROOT=/absolute/path/to/your/Obsidian/vault
```

Or edit `~/.claude/settings.json` directly.
</details>

<details>
<summary><strong>LM Studio</strong></summary>

Settings → Developer → MCP Servers → Add. Paste the same JSON fragment. The "name" field becomes the server's key (`iris`).
</details>

<details>
<summary><strong>Other MCP clients</strong></summary>

Iris is a standard MCP stdio server. Any MCP-compatible client can launch it via the `command` + `args` + `env` fragment. Check your client's MCP documentation for where to put it.
</details>

### Step 3 — Verify

Restart your MCP client. Iris should show up in the tool list. On the first request that touches the vault, she'll build a SQLite cache at `<vault>/.ai_memory_cache/vault.db`. Subsequent requests are fast.

A good "is it working?" first prompt:

```
Use Iris to list the top 5 most-recently-modified notes in my vault.
```

### Step 4 *(optional)* — Install the Obsidian companion plugin

Adds a `Reload SQLite DB` hotkey and clickable wikilinks inside the SQLite DB plugin's rendered tables.

```bash
cp -r plugins/sqlite-db-reload <your-vault>/.obsidian/plugins/
```

In Obsidian: **Settings → Community Plugins → Installed Plugins** → enable **SQLite DB Companion**. Needs the upstream [SQLite DB plugin](https://github.com/stfrigerio/sqliteDB) for the SQL rendering itself.

### Step 5 *(optional)* — Semantic search

Iris can rank notes by meaning, not just keyword. She talks to any OpenAI-compatible `/v1/embeddings` endpoint — works against [Ollama](https://ollama.com/), LM Studio, OpenAI, etc.

```bash
# Free, fully local with Ollama
brew install ollama
ollama serve &
ollama pull nomic-embed-text
```

Then in your MCP client:

```
reindex_embeddings()            # one-time bulk index, ~1–5 min for ~600 notes
semantic_search("what notes touch on stress and deadlines?", top_k=5)
embedding_status()              # health check
```

Default endpoint is `http://localhost:11434/v1/embeddings`; override with `IRIS_EMBED_URL`.

### Step 6 *(optional)* — Run as a Discord bot via Docker

Talk to Iris from anywhere using your **Claude subscription** (no Anthropic API key needed). Proactive features: morning briefings, evening wrap-ups, event/reminder pings with per-event lead times, snooze reactions, per-day timezone overrides for travel, auto Google Maps links.

See [`docker/README.md`](docker/README.md) for the full deployment guide.

---

## Configuration

All Iris config lives in [`iris_config.py`](iris_config.py). Precedence: **env var > `~/.config/iris/config.toml` > built-in default**.

Per-device overrides go in `~/.config/iris/config.toml` — keep this file *outside* your synced vault so each device can have its own paths:

```toml
[vault]
root = "~/obsidian-vaults/AI_Memory"

[embed]
url   = "http://localhost:11434/v1/embeddings"
model = "nomic-embed-text"

[llm]
url   = "http://localhost:11434/v1/chat/completions"
model = "gemma3:4b"     # unset = LLM-using features (prose summaries, etc.) disabled
```

Or as env vars:

| Variable | Default | Purpose |
|---|---|---|
| `IRIS_VAULT_ROOT` | `~/obsidian-vaults/AI_Memory` | Path to the vault |
| `IRIS_EMBED_URL` | `http://localhost:11434/v1/embeddings` | OpenAI-compatible embedding endpoint |
| `IRIS_EMBED_MODEL` | `nomic-embed-text` | |
| `IRIS_EMBED_API_KEY` | _(unset)_ | Set for OpenAI / hosted providers |
| `IRIS_LLM_URL` | `http://localhost:11434/v1/chat/completions` | Optional chat endpoint for prose features |
| `IRIS_LLM_MODEL` | _(unset → disabled)_ | |
| `IRIS_LLM_API_KEY` | _(unset)_ | |
| `IRIS_CONFIG` | `~/.config/iris/config.toml` | TOML file path override |

Legacy env vars `OBSIDIAN_VAULT_PATH` and `VAULT_ROOT` still work as fallbacks.

## Architecture

```
obsidian_memory_mcp.py          # MCP entry-point — the script your MCP client launches
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
    ├── calendar.py             # schedule_event, daily_agenda, evening_wrapup, weekly_summary, morning_routine, pull_ical_subscription
    ├── discord.py              # fetch_discord_history, schedule_pingback (bot context)
    ├── links.py                # find_issues, link_candidates, duplicates
    ├── analysis.py             # vault_overview, note_context, merge_candidates
    ├── import_export.py        # import_file, mass_import, triage_inbox, summarize_note_with_llm
    ├── routines.py             # morning_briefing, evening_wrapup, weekly_review
    └── web.py                  # web_search, fetch_url, search_reddit
vault_cron.py                   # standalone CLI: capture, weekly-summary, wrapup, morning, drop-zone import
docker/                         # Discord bot deployment (compose + Dockerfile)
plugins/sqlite-db-reload/       # Obsidian companion plugin (copy into your vault)
```

The MCP server keeps **`.md` files as the source of truth**. SQLite is a disposable cache rebuilt from disk; you can delete `vault.db` at any time and Iris re-indexes on next startup.

## Tool overview

Iris exposes ~140 tools. Some highlights:

| Tool | Purpose |
|---|---|
| `search_vault(query)` | Unified search (FTS + alias + title + tag + tag-cooccurrence expansion) |
| `semantic_search(query)` | Embedding-based ranking; finds notes by meaning |
| `read_note(path)` | Read a note; tracks access for "hotness" ranking |
| `write_note(path, content)` | Write a note; snapshots prior content as a revision |
| `vault_overview()` | Structural map: folders, tags, recent, hot, stale |
| `note_context(path)` | Backlinks, tag siblings, revisions for a note |
| `suggest_links_for(path)` | Semantic-ranked wikilink suggestions |
| `find_issues(checks=…)` | Broken links, duplicates, orphan attachments, link mismatches |
| `find_merge_candidates(folder)` | Vault-wide similarity scoring for potential duplicate notes |
| `sqlite_query(sql)` | Read-only SELECT against any table/view |
| `schedule_event(...)` | Add a calendar event to a daily note's `## Schedule` |
| `daily_agenda(date)` | Tasks + reminders + events for a date, including cross-day events |
| `pull_ical_subscription(url)` | Sync events from a `webcal://` or `https://` iCal feed (iCloud, Google, Outlook). RRULE-aware, dedupes by UID. |
| `list_unfinished_tasks()` | What's still open from recent days |
| `carry_forward_tasks()` | Move missed items to today's daily note |
| `morning_briefing()` | "What's on today" summary |
| `evening_wrapup()` | End-of-day capture-and-archive flow |
| `weekly_review()` / `weekly_summary()` | 7-day overview and persisted weekly note |
| `schedule_pingback(when, message)` | Precise-time Discord ping (bot context). Accepts `HH:MM`, `+15m`, ISO 8601. |
| `list_pingbacks()` / `cancel_pingback(id)` | Inspect / cancel pending precise-time pingbacks. |
| `fetch_discord_history(hours_back)` | On-demand recall of past Discord messages in the active channel. |
| `embed_morning_brief / embed_evening_wrapup / embed_daily_agenda(date)` | Rich Discord embed cards for the corresponding routines. Blue / indigo / blue sidebars, structured fields. |
| `embed_event(date, title_match)` | Single calendar event as an embed — yellow normally, red if imminent. |
| `embed_project_status(path)` | Project dashboard embed — violet sidebar, sections per category. |
| `embed_note(path)` | Render a vault note as a card — title, excerpt, type/tags/mtime fields. |
| `embed_callout(kind, title, body)` | Semantic info box: info / success / warning / error / tip / question. Color + icon chosen for you. |
| `embed_query(sql, title, mode)` | Run a SELECT and render as embed: `mode="table"` (monospace code-block) or `"fields"` (one row → one field). Auto-`LIMIT 10`. |
| `embed_custom(title, description, fields, color)` | Escape hatch — fully custom embed (8 named colors or `#rrggbb`, max 25 fields). |
| `vocab_due / vocab_review(grade) / vocab_review_stats` | SM-2 spaced repetition. `vocab_review` accepts `"correct"` / `"close"` / `"wrong"` strings or 0-5 ints. |

## Why the SQLite-backed approach

- **The model can navigate a vault it doesn't fit in its context.** A 1000-note vault is far too big to read end-to-end, but the SQLite index lets the model answer "what notes are tagged `<X>` AND mention `<Y>`?" or "which `type: project` notes were touched in the last 30 days?" in one query.
- **Live SQL views inside Obsidian.** Domain tables render as live tables in `.md` notes via the [SQLite DB plugin](https://github.com/stfrigerio/sqliteDB). With the companion plugin, name cells become clickable internal links.
- **Write tools are validated.** Each `*_upsert` enforces schema constraints. Generic `sqlite_query` is read-only — no `INSERT/UPDATE/DELETE/DROP/ALTER`.

## Privacy

Everything runs locally — vault data never leaves your machine unless you point an external API (e.g. OpenAI embeddings) at it.

## Platform support

- **macOS / Linux / Docker**: full core MCP and the Discord bot.
- **Windows**: core MCP works; some OS-specific helpers are untested.

---

## Optional integrations

### MyAnimeList sync

Mirror your MAL watch list into the vault, sync changes back. One-time OAuth setup:

1. Register an app at <https://myanimelist.net/apiconfig> (type Other, redirect `http://localhost:8765/callback`).
2. Save credentials JSON to the vault's `.ai_memory_cache/mal_auth.json`.
3. Call the `mal_auth_start` MCP tool from your client to complete OAuth.

After that, `anime_pull_from_mal`, `anime_push_to_mal`, `mal_search`, `mal_seasonal` are all available.

## License

MIT — see [LICENSE](LICENSE).
