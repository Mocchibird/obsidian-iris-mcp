# Iris Discord brain — Phase 1 (text)
#
# Runs:
#   - Iris MCP server (this repo)
#   - Claude Code CLI (uses YOUR Claude subscription, not the API)
#   - A Discord bot that bridges Discord <-> Claude via the Agent SDK
#
# The bot itself is a thin layer: each Discord message becomes a Claude
# session turn, with Iris's MCP tools available to the model. Sonnet 4.6
# is the default but configurable via env.
#
# Designed to run on TrueNAS SCALE or any Docker host. The vault is
# expected to be mounted in via docker-compose (typically a syncthing
# replica or NFS mount from the Mac).
#
# Phase 2 (voice — Whisper + Piper) will extend this image; the runtime
# packages (ffmpeg, libopus, libsndfile1) are pre-installed so we don't
# rebuild the base when adding voice.

FROM python:3.13-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ── System packages ──────────────────────────────────────────────────────────
# Node.js for the `claude` CLI; git for any pip-from-git deps; ffmpeg+libopus
# pre-installed for Phase 2 voice work (cheap to include now).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
        git \
        ffmpeg libopus0 libsndfile1 \
        sqlite3 \
        tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Claude Code CLI ──────────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code \
    && claude --version

# ── Iris ─────────────────────────────────────────────────────────────────────
WORKDIR /opt/iris
COPY pyproject.toml ./
COPY iris_config.py obsidian_memory_mcp.py vault_cron.py ./
COPY _iris ./_iris
COPY plugins ./plugins
COPY docker/requirements.txt ./docker/requirements.txt

# Install Iris itself (editable) + Discord bot deps in one step.
RUN pip install -e . \
    && pip install -r docker/requirements.txt

# ── Discord bot ──────────────────────────────────────────────────────────────
COPY docker/bot.py ./docker/bot.py
COPY docker/bot_embeds.py ./docker/bot_embeds.py
COPY docker/iris_mcp.example.json ./docker/iris_mcp.example.json

# Conventional volume mount points (docker-compose sets the real source paths)
VOLUME ["/vault", "/claude-auth"]

# Discord bot listens for messages; no inbound ports needed for text Phase 1.
# Phase 2 voice will need UDP for Discord voice servers — handled via
# host networking or explicit ports in compose.

ENV IRIS_VAULT_ROOT=/vault \
    CLAUDE_CONFIG_DIR=/claude-auth \
    IRIS_DISCORD_MODEL=claude-sonnet-4-6 \
    TZ=Europe/Zurich

CMD ["python", "/opt/iris/docker/bot.py"]
