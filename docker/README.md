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

Apple-integration tools (`sync_apple`, `pull_health_snapshot`, `get_focus_context`, etc.) are present but no-op on Linux — keep running `vault_cron.py` on your Mac for those, and the data lands in the vault for Iris-in-Discord to read.

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
- In **DMs**, Iris responds to every human message. In **guild channels**, you must @-mention the bot (so it doesn't reply to everything).
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
| `IRIS_VAULT_ROOT` | `/vault` | Should match the docker-compose volume target |
| `CLAUDE_CONFIG_DIR` | `/claude-auth` | Where `claude login` stores its token |

## Updating

```bash
git pull
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d
```

The Claude auth token and your vault stay put (separate volumes).

## Troubleshooting

- **"DISCORD_BOT_TOKEN is not set"** → fill in `docker/.env`.
- **Bot connects but doesn't reply** → check `Message Content Intent` is enabled in the Discord Developer Portal.
- **`claude` errors with "not authenticated"** → run step 4 again. The token directory must be writable by the container.
- **Tool calls hang / time out** → check that the vault mount is correct: `docker exec -it iris ls /vault` should list your note folders.

## Phase 2 — voice (planned)

Voice is a separate add-on that will:

- Join a Discord voice channel
- Stream audio in → Whisper (STT) → Claude → Piper / Coqui (TTS) → audio out
- Target ~1–2 s round-trip latency (mic → speaker)
- Sentence-by-sentence streaming so audio starts before the full reply is done

Whisper / TTS models live in the container; no API keys involved. The image's `ffmpeg`/`libopus`/`libsndfile1` packages are pre-installed for this.
