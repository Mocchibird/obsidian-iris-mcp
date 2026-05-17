# Iris Discord brain — Docker deployment

Run Iris as a Discord bot you can chat with from anywhere. The container ships:

- **Iris MCP server** (this repo)
- **Claude Code CLI** authenticated against *your subscription* — no Anthropic API key needed
- **Discord bot** that bridges Discord ⇄ Claude via the Claude Agent SDK

Designed for TrueNAS SCALE but works on any Docker host.

## Architecture

```
┌─────────────┐   message       ┌──────────────────┐    Iris MCP tools
│  Discord    │ ──────────────► │  bot.py          │ ──────────────►   ┌────────────┐
│  (you)      │ ◄────────────── │  (this image)    │ ◄──────────────  │  vault     │
└─────────────┘   stream reply  │                  │   tool results    │  (markdown)│
                                │  uses your       │                   └────────────┘
                                │  Claude sub via  │
                                │  claude CLI      │
                                └──────────────────┘
```

## Setup with Dockge (recommended for TrueNAS)

This is the path that works cleanest if the repo + vault are both syncthing-replicated to TrueNAS (so the Mac and TrueNAS share the same source tree).

### 1. Make sure syncthing carries the repo but excludes per-device files

In `<sync-root>/.stignore` (i.e. `~/obsidian-vaults/.stignore` on the Mac):

```
// Vault — only the SQLite cache is per-device
AI_Memory/.obsidian/plugins/sqlite-db/data.json
AI_Memory/.ai_memory_cache

// Repo — code syncs, build artifacts and secrets do not
obsidian-iris-mcp/.venv
obsidian-iris-mcp/**/__pycache__
obsidian-iris-mcp/*.egg-info
obsidian-iris-mcp/build
obsidian-iris-mcp/dist
obsidian-iris-mcp/docker/.env
obsidian-iris-mcp/docker/claude-auth
```

### 2. Create the Dockge stack

In Dockge → **+ Compose** → name it `iris-discord`. Paste in the contents of [`docker-compose.yml`](docker-compose.yml). Don't deploy yet.

### 3. Fill in the stack's `.env`

In the Dockge stack editor, switch to the **Environment** tab (or `.env` tab depending on Dockge version) and paste:

```dotenv
# Where syncthing replicates the repo on TrueNAS — adjust to your dataset path
IRIS_REPO_DIR=/mnt/<pool>/<dataset>/obsidian-vaults/obsidian-iris-mcp
IRIS_VAULT_DIR=/mnt/<pool>/<dataset>/obsidian-vaults/AI_Memory

# Keep Claude auth OUT of the synced repo so it doesn't replicate to the Mac.
# Pick any TrueNAS-only path; Dockge will create it on first deploy.
IRIS_AUTH_DIR=/mnt/<pool>/iris-discord/claude-auth

# From the Discord Developer Portal
DISCORD_BOT_TOKEN=your-token-here
```

> **Tip:** to find the real `IRIS_REPO_DIR`/`IRIS_VAULT_DIR`, SSH into TrueNAS and run `find /mnt -maxdepth 5 -name AI_Memory -type d 2>/dev/null`.

### 4. Deploy the stack

Dockge will build the image from `${IRIS_REPO_DIR}` and start the container. First build takes ~3–5 min (Node + claude CLI + pip installs).

### 5. Authenticate Claude (once)

In Dockge → stack → **Terminal** (or `docker exec -it iris-discord bash`):

```bash
claude login
```

Open the URL it prints, log into your Claude subscription, paste the code back. Token persists in `IRIS_AUTH_DIR`.

### 6. Set up the Discord bot

See [step 5 below](#5-create-a-discord-bot-application) — same as the manual setup.

### 7. Restart the stack

In Dockge → stack → **Restart**. The bot should now connect.

To follow logs: Dockge → stack → **Logs** (or `docker logs -f iris-discord`).

---

## Setup (one-time, manual / non-Dockge)

### 1. Mount your vault into TrueNAS

Use whichever you prefer:

- **Syncthing**: pair the TrueNAS syncthing app to your Mac's, sync the `AI_Memory` folder to a TrueNAS dataset. Recommended.
- **NFS / SMB**: share the Mac's vault folder, mount it on TrueNAS.

Then in [`docker-compose.yml`](docker-compose.yml), set the host-side path:

```yaml
volumes:
  - /mnt/tank/vaults/AI_Memory:/vault
```

### 2. Tell syncthing NOT to sync the SQLite cache

Each device has its own derived DB. Syncing it causes corruption. In `~/obsidian-vaults/.stignore` add:

```
AI_Memory/.ai_memory_cache
```

### 3. Build the image

```bash
cd /path/to/obsidian-iris-mcp
docker compose -f docker/docker-compose.yml build
```

### 4. Authenticate Claude

Run the CLI once inside the container — it'll print a URL to paste into your browser:

```bash
docker compose -f docker/docker-compose.yml run --rm iris claude login
```

The token persists in `./docker/claude-auth/` (mounted as `/claude-auth`).

### 5. Create a Discord bot application

1. Go to https://discord.com/developers/applications
2. New Application → Bot → Reset Token, copy it.
3. Bot tab → enable **Message Content Intent**.
4. OAuth2 → URL Generator → scopes: `bot` + `applications.commands`; permissions: `Send Messages`, `Read Message History`, `Embed Links`, `Attach Files`. (For Phase 2 voice: also `Connect`, `Speak`, `Use Voice Activity`.)
5. Invite to your server using the generated URL.

### 6. Configure env

```bash
cd docker
cp .env.example .env
# edit .env — set DISCORD_BOT_TOKEN at minimum
```

Optional restrictions in `.env`:

```
IRIS_DISCORD_ALLOWED_CHANNELS=123456789012345678
IRIS_DISCORD_ALLOWED_USERS=987654321098765432
```

### 7. Run

```bash
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f
```

In Discord: `@Iris what notes did I touch last week?`

## How conversations work

- One **session per Discord channel/thread**. Iris remembers context within a channel across messages, but channels don't bleed into each other. Open a thread for a fresh slate.
- Sessions live **in-process only**. On bot restart they're discarded — but the bot fetches recent channel messages and injects them into the new session's system prompt as background context. The model treats them as memory and continues naturally. Net effect: a restart is mostly invisible from the user's side, and you don't need to persist Claude sessions to disk. The vault stays the canonical long-term memory; Discord history is the short-term buffer.

  Selection is **fuzzy and burst-aware**. The bot groups recent messages into conversation bursts (gaps longer than `IRIS_DISCORD_CONTEXT_BURST_GAP_MIN` start a new burst) and walks newest-to-oldest including whole bursts up to the soft token budget. To avoid amputating a coherent topic that spans several messages, it'll overshoot the budget by up to `IRIS_DISCORD_CONTEXT_FUZZ_FACTOR` × budget rather than cut a burst in half. So if you discussed a government appointment 20 minutes ago and the budget would only cover half those messages, the bot keeps the whole appointment thread intact.

- **Extended on-demand recall** via `fetch_discord_history(hours_back=N)`. The bot logs every message it sees to a per-channel JSONL log at `IRIS_DISCORD_HISTORY_DIR`. If Iris needs to reach further back than the cold-start window — e.g. the user says "what did I tell you yesterday about the appointment?" — she can call this MCP tool to pull older messages on demand. By default it filters out her own proactive pings (event reminders, briefings) since those aren't conversational.

- **Precise-time pingbacks** via `schedule_pingback(when, message)`. For "ping me at 00:30", "remind me in 15 minutes", "message me at 14:00 tomorrow". Unlike `add_reminder` (which is date-granular and fires within a 15-minute lead window), pingbacks fire at the exact wall-clock minute via a 30-second poll loop in the bot. Pending entries persist across restarts in `/claude-auth/pending_pings.jsonl`. Accepts `HH:MM` (today, or tomorrow if past), `+15m` / `+2h` relative offsets, or full ISO 8601. Inspect or cancel via `list_pingbacks` / `cancel_pingback(id)`.

- **Wall-clock context per turn.** Every user message gets a `[Now: 2026-05-17 00:30 Europe/Zurich (Sunday)]` line prepended invisibly to Iris's input, using the active timezone (which respects today's daily-note `timezone:` frontmatter override). Without this, Claude only sees a UTC date from its system context and can be a day behind in the evening local time.

- **Rich Discord embeds**, not walls of markdown. Proactive pings (morning brief, evening wrap-up, event reminders, time-based reminders) and many of Iris's chat replies render as native Discord cards with a colored sidebar, title, fields, and footer. Iris reaches for these via a family of `embed_*` MCP tools:
  - `embed_morning_brief / embed_evening_wrapup / embed_daily_agenda(date)` — daily routine cards (blue / indigo / blue).
  - `embed_event(date, title_match)` — single event card; yellow normally, red if imminent.
  - `embed_project_status(path)` — project dashboard, violet.
  - `embed_note(path)` — show a vault note as a card (title, excerpt, type/tags/mtime).
  - `embed_callout(kind, title, body)` — semantic info box. `kind` ∈ info/success/warning/error/tip/question; color + icon chosen for you.
  - `embed_query(sql, title, mode)` — SELECT against vault DB, render as `"table"` (monospace) or `"fields"` (one row per field). Auto-`LIMIT 10`.
  - `embed_custom(title, description, fields, color)` — fully custom escape hatch.
  
  These tools queue an embed-request JSON line to `/claude-auth/pending_embeds.jsonl`. The bot polls every ~1 s and renders to a real `discord.Embed`. Embeds produced during a chat reply are flushed AT THE END of the reply, between the streamed text and the completion ping, so the order in chat is text → embed card → ✓. Snoozed pings preserve the original embed when replayed (`💤` prefix added to the title).

- **Message queue with FIFO order.** When you send Iris a new message while she's still streaming a reply, the new one isn't dropped — it gets a 📥 reaction (queued ack), and `asyncio.Lock`'s FIFO waiter ordering processes them in arrival order. The reaction is removed when Iris picks it up. Cap: `IRIS_DISCORD_MAX_QUEUE_DEPTH=5` (configurable); beyond that, you get a clear "queue full" reply rather than silent loss.

- **Catch-up grace windows** for scheduled briefings. If the bot restarts at 08:30 after a power blip, the 08:00 morning brief still fires (within the 3-hour `IRIS_NOTIFY_MORNING_GRACE_MIN` window). Restart at 11:30 and it suppresses — you don't want yesterday's brief at lunch. Evening default: 60 min grace.

- **File uploads** to any channel Iris listens in get auto-saved to `<vault>/90_Inbox/inbox/<timestamp>_<sanitized-name>`. Iris sees the saved paths in her prompt and decides how to route them — she might call `import_drop_zone` (binaries → `40_Attachments/<type>/` + auto-created inbox notes), `extract_pdf_text` to read a PDF, or just move the file into a specific project page. Filenames are sanitized (`[^\w\-. ]+` → `_`) and de-duplicated so two attachments named `screenshot.png` don't collide. Works for PDFs, images, audio, etc. — anything Discord lets you attach.
- **Long sessions auto-compact.** Claude Code (which powers the SDK) summarizes earlier turns when the context window fills up, replacing them with a recap. You don't manage this; it just happens.
- Iris responds when **any** of these is true:
  - You're DMing the bot
  - You @-mention the bot in a guild channel
  - You reply (Discord's reply feature) to one of the bot's earlier messages
  - The channel ID is in `IRIS_DISCORD_LISTEN_ALWAYS_CHANNELS` — good for dedicated rooms like `#iris-tasks`, `#iris-notes`, where you don't want to type `@Iris` every time
- Replies **stream** — you see the text appear as Claude generates it. The bot edits a single message up to Discord's 2000-char limit, then continues in a new message.

## Configuration knobs

| Env var | Default | Notes |
|---|---|---|
| `DISCORD_BOT_TOKEN` | _(required)_ | From Discord Developer Portal |
| `IRIS_DISCORD_MODEL` | `claude-sonnet-4-6` | Any model your subscription has |
| `IRIS_DISCORD_ALLOWED_CHANNELS` | _(unset)_ | CSV of channel IDs |
| `IRIS_DISCORD_ALLOWED_USERS` | _(unset)_ | CSV of user IDs |
| `IRIS_DISCORD_SYSTEM_PROMPT` | _(built-in)_ | Inline prompt override |
| `IRIS_DISCORD_SYSTEM_PROMPT_PATH` | _(unset)_ | Path to markdown file inside container (e.g. `/vault/00_Index/iris_system_prompt.md`) |
| `IRIS_DISCORD_CONTEXT_MINUTES` | `60` | Outer time bound for recent-history injection. `0` disables. |
| `IRIS_DISCORD_CONTEXT_TOKEN_BUDGET` | `2000` | Soft token budget for the injection. Fuzzy — see below. |
| `IRIS_DISCORD_CONTEXT_FUZZ_FACTOR` | `1.5` | How much to overshoot the budget to keep a topic burst intact. |
| `IRIS_DISCORD_CONTEXT_BURST_GAP_MIN` | `10` | Minutes between messages that end one burst and start another. |
| `IRIS_DISCORD_HISTORY_DIR` | `/claude-auth/discord-channels` | Where the bot writes per-channel JSONL logs (read on-demand by `fetch_discord_history`). |
| `IRIS_DISCORD_COMPLETION_PING` | `on` | Send a tiny new message after streaming finishes so Discord plays the normal new-message notification sound. Edits don't notify. |
| `IRIS_DISCORD_COMPLETION_PING_EMOJI` | `✓` | Content of the completion ping. |
| `IRIS_DISCORD_COMPLETION_PING_TTL` | `4` | Seconds before the ping auto-deletes. `0` = keep forever. |
| `IRIS_VAULT_ROOT` | `/vault` | Should match the docker-compose volume target |
| `CLAUDE_CONFIG_DIR` | `/claude-auth` | Where `claude login` stores its token |
| `IRIS_DISCORD_PING_CHANNEL` | _(unset)_ | Channel ID for proactive pings. Blank = all proactive output disabled. Legacy alias `IRIS_DISCORD_NOTIFY_CHANNEL` still works. |
| `IRIS_NOTIFY_INTERVAL_SECS` | `60` | How often the notification loop scans the vault (≈ minute precision for event/reminder pings) |
| `IRIS_NOTIFY_LEAD_MIN` | `15` | Lead time before an event/reminder for the ping (override per event with `lead: 2h` in the description) |
| `IRIS_NOTIFY_MORNING_AT` | `08:00` | Daily morning briefing time (HH:MM, 24 h, in *active* TZ). `off` to skip. |
| `IRIS_NOTIFY_EVENING_AT` | `22:00` | Daily evening wrap-up time (in active TZ). `off` to skip. |
| `IRIS_NOTIFY_MORNING_GRACE_MIN` | `180` | If the bot starts AFTER the scheduled time but within this many minutes, still fire today's morning brief. Past it, suppress until tomorrow. |
| `IRIS_NOTIFY_EVENING_GRACE_MIN` | `60` | Same idea for the evening wrap-up. |
| `IRIS_DISCORD_MAX_QUEUE_DEPTH` | `5` | Max queued user messages per channel while Iris is still working. Beyond this, new messages are rejected with a "queue full" reply. |
| `TZ` | `Europe/Zurich` | Container's system timezone — Iris uses this as the home zone. Set in `.env`, NOT in the compose `environment:` block (Dockge's shell `TZ=Etc/UTC` will shadow `${TZ:-...}` substitution there). |
| `IRIS_TIMEZONE` | _(falls back to `TZ`)_ | Optional override for Iris's home TZ if different from container TZ |

## Proactive notifications

Set `IRIS_DISCORD_PING_CHANNEL` to a channel ID, and Iris will post to it on her own in four flavours:

### 1. Upcoming event / reminder pings

Every `IRIS_NOTIFY_INTERVAL_SECS` (default 60 s ≈ minute precision) the bot scans the vault for:

- **Calendar events** today whose `time` is within their lead window (default `IRIS_NOTIFY_LEAD_MIN` minutes, override per event with `lead: <duration>` in the event's `description`)
- **Reminders** whose `remind_on` is today
  - `HH:MM —` prefix in the text → same lead-window rules
  - No time prefix → one all-day ping at first scan

**Per-event lead-time override.** The default is 15 min before the event, fine for most things. For events that need a longer head-start (travel, packing) include a `lead:` hint:

| In the description / reminder text | Meaning |
|---|---|
| `lead: 2h` | 2 hours before |
| `lead: 30m` or `lead 30m` | 30 min before |
| `lead:90` | 90 min before (bare number = minutes) |

Iris knows this convention from her system prompt — saying *"meeting in Basel at 14:00, ping me 2h before"* in Discord gets her to schedule the event with `description="lead: 2h"`.

### 2. Morning briefing

Once a day at `IRIS_NOTIFY_MORNING_AT` (default 08:00), Iris posts the same content as the `morning_briefing` MCP tool — schedule, overdue tasks, today's tasks, unfinished from recent days, inbox count, active projects.

### 3. Evening wrap-up

Once a day at `IRIS_NOTIFY_EVENING_AT` (default 22:00), Iris posts the same content as the `evening_wrapup` MCP tool — events attended, tasks completed, reminders done, notes modified.

### 4. Snooze reactions

React to any of Iris's pings with:

| Emoji | Snooze |
|---|---|
| ⏰ | +5 min |
| 🛏️ | +15 min |
| 💤 | +1 hr |

Iris will mark the message with ✅ to confirm and resend the same content after the delay (prefixed with 💤). Snoozes persist across bot restarts (stored at `/claude-auth/discord-snoozed.json`).

### Persistence

- Sent pings dedupe via `/claude-auth/discord-notified.json` (last 1000 keys)
- Snoozes via `/claude-auth/discord-snoozed.json`

Pick any channel — `#iris-alerts`, `#general`, or one of your dedicated iris channels. Clear the env var and restart to disable everything.

### Travel: per-day timezone override

Briefings and event/reminder pings fire in your local time. If you're travelling to a different timezone, set `timezone: <IANA name>` in the daily note's frontmatter for the relevant dates — the bot reads it at each check and shifts the schedule accordingly. Example:

```markdown
---
type: daily
date: 2026-07-15
timezone: Asia/Seoul
---
# 2026-07-15 — Wednesday (Seoul)
...
```

While that note is "today" from your home TZ's perspective, the bot uses `Asia/Seoul` so 08:00 means 08:00 in Korea, not Zurich. Iris is also told about this convention in her system prompt so she'll set the field herself when you tell her you're going somewhere.

## Updating

```bash
git pull
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d
```

The Claude auth token and your vault stay put (separate volumes).

## Troubleshooting

- **"DISCORD_BOT_TOKEN is not set"** → fill in `docker/.env`.
- **Bot connects but doesn't reply** → check `Message Content Intent` is enabled in the Discord Developer Portal. Also check the bot has permission to see the channel (right-click channel → Edit → Permissions).
- **`claude` errors with "not authenticated"** → run the `claude login` step again. Inside the interactive prompt, use the slash command `/login` (not `claude login` at the shell — newer CLI versions only authenticate via the in-prompt slash command).
- **`--dangerously-skip-permissions cannot be used with root/sudo privileges`** → the compose file already sets `IS_SANDBOX: "1"` to opt out of this check. If you're using an older version of this repo, add `IS_SANDBOX=1` to your `.env`.
- **TrueNAS app: `unable to prepare context: path ... not found`** → Dockge's container can't see the host path. Edit the Dockge TrueNAS app and add the synced repo's parent dir as a Host Path (Source = Target = the same absolute path so compose references match host paths).
- **Tool calls hang / time out** → check that the vault mount is correct: `docker exec -it iris-discord ls /vault` should list your note folders.

## Companion: Ollama sibling stack

For semantic search and LLM-using features without depending on the Mac being awake, run [`docker/ollama/`](ollama/README.md) as a sibling Dockge stack. With NVIDIA GPU passthrough on TrueNAS (1080Ti and similar), it'll hold both an embedding model and a chat model on-GPU with low latency.

Both stacks join a shared docker network called `ai-models` so the iris container can reach Ollama at `http://ollama:11434`. **One-time setup**, before deploying either stack:

```bash
docker network create ai-models
```

Both compose files then join it as `external: true`. Order of stack startup doesn't matter.

## Phase 2 — voice (planned)

Voice is a separate add-on that will:

- Join a Discord voice channel
- Stream audio in → Whisper (STT) → Claude → Piper / Coqui (TTS) → audio out
- Target ~1–2 s round-trip latency (mic → speaker)
- Sentence-by-sentence streaming so audio starts before the full reply is done

Whisper / TTS models live in the container; no API keys involved. The image's `ffmpeg`/`libopus`/`libsndfile1` packages are pre-installed for this.
