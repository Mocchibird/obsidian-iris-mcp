# Iris — Obsidian Memory MCP Server

> Turn your Obsidian vault into long-term memory for Claude (or any MCP client).

**About the name.** Iris is both:

- An acronym — **I**ntelligent **R**ecall & **I**ndexing **S**ystem.
- A reference to Greek mythology — Iris (Ἶρις) is the messenger goddess and the personification of the rainbow, the bridge that lets gods and humans speak to each other. This project plays the same role: the bridge between you and your vault, carrying messages, looking things up, and tying the world above (Claude, your conversations) to the world below (your notes on disk).

## What Iris does

Iris is an [MCP](https://modelcontextprotocol.io) server that indexes an Obsidian vault into SQLite and exposes ~180 tools for searching, editing, linking, and analysing it. Highlights:

- Full-text search, semantic search, backlinks, tag co-occurrence
- Add and complete tasks and reminders inside your `.md` notes
- Schedule calendar events (including cross-day, all-day, per-event ping lead-times) in daily notes
- Generate morning briefings, evening wrap-ups, weekly summaries
- Find broken links, duplicate notes, orphan attachments, merge candidates
- Render live SQL queries inside Obsidian via the SQLite DB plugin (also re-renderable server-side for iOS/iPadOS)
- Run as a **Discord bot** through Docker — chat with Iris from anywhere using your Claude subscription
- **Image vision** through Discord: drop a photo and Iris analyses it inline (food calorie estimation, whiteboard OCR, screenshot triage, etc.)
- **Health tracking**: meal/weight logging, Mifflin-St Jeor BMR/TDEE math, target-intake recommendations, scheduled daily + weekly health-channel cards, auto-routing of food photos into a dated archive
- **Training + injury tracking**: skill goals (handstand, pull-ups, muscle-up, asian squat, etc.) with cached progression plans, session log, injury records with restriction lists that gate Iris's training recommendations (so the shoulder-rehab phase doesn't get a "do overhead pressing" suggestion)
- **Habit tracker**: daily check-offs with GitHub-style 🟩⬜⬛ heatmaps, cadence-aware reminders (Iris pings the bot's PING_CHANNEL once when a habit's target time passes without being logged), per-day idempotent logging, optional cross-links to skill goals or injuries (so a "shoulder rehab" habit auto-clears when the injury is healed)
- **Matplotlib chart embeds**: line / bar / pie charts rendered server-side and posted to Discord with the PNG attached inline (weight trend, daily kcal vs target, macro split, habit duration over time, habit consistency, and a generic SQL-driven escape hatch). PNGs archive under `40_Attachments/Charts/YYYY-MM/` so they're browsable in Obsidian too.
- **Voice messages** (Phase 2.1): Discord voice messages (🎙️) are auto-transcribed via local `faster-whisper` STT and treated as text input — no audio data leaves the host. The .ogg blob is auto-deleted after successful transcription (the transcript is the durable record); set `IRIS_DISCORD_VOICE_AUTO_DELETE=0` to keep voice files for journaling. Model + device + compute type configurable via `IRIS_WHISPER_MODEL` / `IRIS_WHISPER_DEVICE` / `IRIS_WHISPER_COMPUTE`.
- **Voice channel TTS**: say "join voice" in any allowed text channel (while you're in a voice channel) and Iris joins it. Her text replies are then also spoken aloud via [Microsoft Edge TTS](https://pypi.org/project/edge-tts/) (Azure Neural voices, free, no API key). Streaming playback — audio starts ~300 ms after the request, no waiting for full synth. Per-sentence language detection routes EN → `en-US-AvaNeural`, JA → `ja-JP-NanamiNeural`, KO → `ko-KR-SunHiNeural`; mixed-language replies synthesize each segment with its language's voice and concatenate. Override defaults via `IRIS_TTS_VOICE_EN` / `_JA` / `_KO`. Idle auto-leave after 10 min via `IRIS_DISCORD_VOICE_IDLE_SEC`. Phase 2.2.1 will add voice receive + Whisper STT for full duplex. (TTS text leaves the host to Microsoft; the rest of Iris stays local.)
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
    ├── discord.py              # fetch_discord_history, schedule_pingback, embed_* (morning_brief / evening_wrapup / daily_agenda / event / project_status / note / callout / query / custom / health_daily / health_weekly)
    ├── links.py                # find_issues, link_candidates, duplicates
    ├── analysis.py             # vault_overview, note_context, merge_candidates
    ├── import_export.py        # import_file, mass_import, triage_inbox, summarize_note_with_llm
    ├── routines.py             # morning_briefing, weekly_review
    ├── health.py               # meals + weights logging, BMR/TDEE math, target intake, daily/weekly summaries, auto-routes food photos into 40_Attachments/Food Log/
    ├── training.py             # skill_goals + injuries + training_sessions — skill-coach role with injury-aware recommendations
    ├── habits.py               # daily habits + idempotent done-logging + GitHub-style heatmap renderer + cadence-aware reminders
    ├── charts.py               # matplotlib PNG chart embeds (weight / kcal / macros / habit duration / consistency / generic SQL)
    ├── voice.py                # faster-whisper STT for Discord voice messages (Phase 2.1)
    ├── people.py               # people_upsert (occupation, employer, team, nicknames, email, phone, socials)
    ├── anime.py                # anime list + full MAL OAuth sync (search, ranking, seasonal, user list, push/pull)
    ├── vocab.py                # vocab_upsert, vocab_review (SM-2 spaced repetition)
    ├── warranties.py           # warranty tracking with expiry alerts
    └── web.py                  # web_search, fetch_url, search_reddit
vault_cron.py                   # standalone CLI: capture, weekly-summary, wrapup, morning, drop-zone import
docker/                         # Discord bot deployment (compose + Dockerfile)
plugins/sqlite-db-reload/       # Obsidian companion plugin (copy into your vault)
```

The MCP server keeps **`.md` files as the source of truth**. SQLite is a disposable cache rebuilt from disk; you can delete `vault.db` at any time and Iris re-indexes on next startup.

## Tool overview

Iris exposes ~180 tools. Some highlights:

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
| `refresh_sql_views(path?, all_notes?)` | Re-render ` ```sqlite ` code blocks in vault notes as markdown tables (for iOS/iPadOS where the SQLite-DB plugin doesn't run) |
| `vault_snapshot()` | On-demand atomic `VACUUM INTO` of the live vault.db → vault-snapshot.db. Periodic loop runs every 10 min; call this for instant freshness. |
| `schedule_event(...)` | Add a calendar event to a daily note's `## Schedule` |
| `daily_agenda(date)` | Tasks + reminders + events for a date, including cross-day events |
| `pull_ical_subscription(url, link_to_person?)` | Sync events from a `webcal://` or `https://` iCal feed (iCloud, Google, Outlook). RRULE-aware. Dedupes by UID AND by `(date, time, title)` so same-event-in-two-calendars doesn't double-import. Optional `link_to_person` adds `with: [[path]]` backlink to a contact. |
| `sync_all_calendars()` | Sync every feed configured in `IRIS_DEFAULT_ICAL_URLS` (pipe-separated, per-feed tags + person-link). |
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
| `log_meal(description, kcal, photo_path?, ...) / log_weight(kg) / daily_calories / weight_trend` | Calorie + weight logging. Food photos passed via `photo_path` auto-route from `90_Inbox/inbox/` to `40_Attachments/Food Log/YYYY-MM/<descriptive-filename>`. |
| `health_profile_set / tdee_estimate / target_intake` | Mifflin-St Jeor BMR + activity-multiplier TDEE, deficit-adjusted intake recommendation with safety floor at BMR. |
| `embed_health_daily / embed_health_weekly` | Scheduled (or on-demand) recap cards posted to `IRIS_DISCORD_HEALTH_CHANNEL`. Daily fires at 08:30 by default; weekly Mondays at 09:00. Times + grace windows configurable. |
| `skill_upsert / skill_list / skill_remove` | Long-running physical-skill goals (handstand, pull-ups, muscle-up, planche…) with cached `progression` plans + `constraint_ref_ids` linking to gating injuries. |
| `injury_upsert / injury_list / injury_remove` | Injury records with free-text `restrictions` field that Iris reads before recommending any training session. Status: active / managing / healed. |
| `log_training / recent_training / remove_training` | Lightweight session log (kind, duration, RPE, summary, `skill_ids` worked). Raw set/rep detail stays in your Gym.md note via `note_path`. |
| `habit_upsert / habit_done / habit_undo / habit_list / habit_streak` | Daily habit tracker. `habit_done` is idempotent — re-marking the same day is a safe no-op update. |
| `habit_heatmap(habit_id, weeks=10)` | GitHub-style 🟩⬜⬛ heatmap as a markdown block. 7 rows (Mon-Sun) × N columns (each column = 1 week, rightmost = this week). Cadence-aware (off-days render as inactive). |
| `habit_pending_today / habit_status_today` | "What's left to do today" — also drives the bot's once-per-day-per-habit reminder pings. |
| `embed_weight_chart / embed_kcal_chart / embed_macro_pie` | Matplotlib PNG charts attached inline to Discord embeds. Weight = line + target dashed line; kcal = bars coloured by target alignment + target line; macro = pie of P/C/F by kcal contribution. PNGs archive under `40_Attachments/Charts/YYYY-MM/`. |
| `embed_habit_duration / embed_habit_consistency` | Per-habit duration over time (good for asian squat hold, meditation length) + daily across-habit completion bars (coloured by adherence). |
| `embed_chart(sql, chart_kind="line", x, y, title, ...)` | Generic SQL-driven chart — line / bar / pie. Auto-detects date columns for nicer time axes. Same read-only safety rules as `sqlite_query`. |

## Why the SQLite-backed approach

- **The model can navigate a vault it doesn't fit in its context.** A 1000-note vault is far too big to read end-to-end, but the SQLite index lets the model answer "what notes are tagged `<X>` AND mention `<Y>`?" or "which `type: project` notes were touched in the last 30 days?" in one query.
- **Live SQL views inside Obsidian.** Domain tables render as live tables in `.md` notes via the [SQLite DB plugin](https://github.com/stfrigerio/sqliteDB). With the companion plugin, name cells become clickable internal links.
- **Write tools are validated.** Each `*_upsert` enforces schema constraints. Generic `sqlite_query` is read-only — no `INSERT/UPDATE/DELETE/DROP/ALTER`.

## Privacy

Everything runs locally — vault data never leaves your machine unless you point an external API (e.g. OpenAI embeddings) at it.

## Platform support

- **macOS / Linux / Docker**: full core MCP and the Discord bot.
- **Windows**: core MCP works; some OS-specific helpers are untested.

## Optional integrations

### MyAnimeList sync

Mirror your MAL watch list into the vault, sync changes back. One-time OAuth setup:

1. Register an app at <https://myanimelist.net/apiconfig> (type Other, redirect `http://localhost:8765/callback`).
2. Save credentials JSON to the vault's `.ai_memory_cache/mal_auth.json`.
3. Call the `mal_auth_start` MCP tool from your client to complete OAuth.

After that, `anime_pull_from_mal`, `anime_push_to_mal`, `mal_search`, `mal_seasonal` are all available.

## License

MIT — see [LICENSE](LICENSE).
