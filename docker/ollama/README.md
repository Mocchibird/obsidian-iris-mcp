# Ollama sibling stack (TrueNAS + NVIDIA GPU)

Runs Ollama on TrueNAS with GPU passthrough so the Iris Discord container — and any other host on your LAN — can hit `/v1/embeddings` and `/v1/chat/completions` without needing the Mac awake.

## Prerequisites

- **NVIDIA driver enabled on TrueNAS**: Apps → Configuration → Settings → toggle on NVIDIA Drivers. Reboot if prompted.
- Verify from TrueNAS shell:
  ```bash
  nvidia-smi
  ```
  Should print your GPU. If `command not found`, the driver isn't installed yet — finish that step before deploying.

## Deploy

### 1. New Dockge stack

Dockge → **+ Compose** → name `ollama`.

Paste the contents of [`compose.yaml`](compose.yaml) into the editor.

### 2. `.env`

Switch to the `.env` tab and paste:

```dotenv
# Adjust <pool> to your actual pool name
OLLAMA_DATA_DIR=/mnt/HDDs/Applications/ollama
```

The directory will be created on first start; models land there (each is multi-GB so pick a path with space).

### 3. Deploy

First boot pulls the image (~1 GB). After it's up, you should see in logs:

```
NVIDIA GPU detected: ... GeForce GTX 1080 Ti, 11GB
```

If you see "running with CPU only" instead, the GPU passthrough didn't take — re-check the NVIDIA driver step.

### 4. Pull models

In Dockge → ollama stack → terminal (or `docker exec -it ollama bash`):

```bash
# Embedding model — small, fast, used by Iris semantic_search
ollama pull nomic-embed-text

# Chat model — pick one based on your VRAM budget
ollama pull gemma4              # 26B MoE, ~9.6 GB on disk, ~10 GB VRAM — fits 1080Ti
# OR
ollama pull gemma3:4b           # 4B dense, ~2.5 GB VRAM — lighter, plenty fast for prose
```

You can list installed models with `ollama list`.

### 5. Wire Iris to it

In your `iris-discord` stack's `.env`, add:

```dotenv
# If both stacks are on the host's default bridge, use the container hostname:
IRIS_EMBED_URL=http://ollama:11434/v1/embeddings
IRIS_LLM_URL=http://ollama:11434/v1/chat/completions

# Or just point at the TrueNAS host IP (works if container DNS doesn't resolve):
# IRIS_EMBED_URL=http://192.168.1.x:11434/v1/embeddings
# IRIS_LLM_URL=http://192.168.1.x:11434/v1/chat/completions

IRIS_EMBED_MODEL=nomic-embed-text
IRIS_LLM_MODEL=gemma4
```

Restart the iris stack. From Discord:

```
@Iris reindex_embeddings        # first time only, ~30s
@Iris embedding_status          # should show 639 indexed
@Iris semantic_search "homelab" # try a search
```

## Resource notes

| Component | RAM | CPU | VRAM |
|---|---|---|---|
| Ollama (nomic + gemma4) | ~4 GB | 2 cores | ~10 GB on 1080Ti |
| Iris-discord | ~2 GB | 2 cores | — |
| **Total** | ~6 GB | 4 cores | ~10 GB |

Idle, Ollama drops to ~300 MB RAM once models unload after 5 min. To keep them warm permanently (faster first request, more idle RAM), set `OLLAMA_KEEP_ALIVE=-1` in the compose `environment:` block.

## Troubleshooting

- **`could not select device driver "nvidia"`** → driver not installed or not registered with Docker. Re-check Apps → Configuration → Settings on TrueNAS.
- **Container starts but `running with CPU only`** → driver visible to host but not to container. Confirm `runtime: nvidia` is still in the compose file and the NVIDIA Container Toolkit is installed.
- **Model file taking forever to download** → check disk space on `OLLAMA_DATA_DIR`. gemma4 is 9.6 GB.
- **From Iris container `connection refused`** → on the same Docker network? `docker network ls` and check both stacks share one, or fall back to using the TrueNAS host IP in `IRIS_EMBED_URL`.
