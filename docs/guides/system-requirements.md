# System Requirements

Gobby runs as a local Python daemon backed by a required Docker Compose stack:
PostgreSQL, Qdrant, and FalkorDB. The daemon is small; the managed datastore
stack drives most hardware and Docker requirements.

Use this guide to decide what has to be installed before running Gobby.

## Quick Matrix

| Setup | Required | Good default |
|-------|----------|--------------|
| Gobby daemon | Python 3.13+, `uv`, Docker with Compose v2, the managed PostgreSQL/Qdrant/FalkorDB stack, and the listed local ports | 4+ CPU cores, 16 GB RAM, SSD/NVMe storage |
| Daemon + web UI | Daemon requirements; installed UI is served on port 60887 | 16 GB RAM |
| Local embedding model | LM Studio with `lms` or Ollama with `ollama` | 16 GB RAM, GPU or unified memory when also running a chat model |
| Local generation (LM Studio, Ollama, vLLM / vllm-metal) | Operator-managed runtime plus a named `ai.generation.endpoints` entry; Gobby does not start the server | 16+ GB RAM or unified memory; GPU or Apple Silicon for chat models |

An embedding provider remains optional. Choosing `None` disables semantic
embedding work, but does not skip installation or startup of the managed
datastore stack.

## Platform Notes

Gobby is packaged as a Python 3.13+ application and is developed for local developer machines.
The practical platform requirements are mostly set by Python, Docker, and whichever embedding
provider you choose.

| Platform | Notes |
|----------|-------|
| macOS | Docker Desktop provides the Linux VM for the PostgreSQL hub, Qdrant, and FalkorDB. Apple Silicon is a practical target for the local full stack. |
| Linux | Use Docker Engine plus the Docker Compose plugin. Linux avoids the Docker Desktop VM memory allocation step. |
| Windows | Use Windows 10/11 with WSL2 for Docker-based services. Local shell tooling and filesystem paths should be verified in the target environment. |

Docker Compose v2 is a hard installation and startup requirement. Gobby does
not support an external PostgreSQL server or a production daemon without the
managed Qdrant and FalkorDB services.

## Daemon

The daemon is a native Python process with a local PostgreSQL hub, FastAPI HTTP
server, WebSocket server, and optional UI dev server.

| Resource | Current value |
|----------|---------------|
| Python | 3.13+ (`requires-python = ">=3.13"`) |
| Package runner | `uv` for source/development commands such as `uv run gobby start` |
| Database | Local PostgreSQL hub configured through `bootstrap.yaml` `database_url` |
| HTTP API | `localhost:60887` by default |
| WebSocket | `localhost:60888` by default |
| Installed Web UI | `localhost:60887` by default |
| Dev Web UI | `localhost:60889` when `gobby ui dev` is running |
| Bind host | `localhost` by default |
| Disk | 1 GB for code/dependencies plus PostgreSQL data and transcript growth |
| RAM | Hundreds of MB idle; plan 1-2 GB when many sessions, hooks, or MCP calls are active |

Bootstrap settings live in `src/gobby/install/shared/config/bootstrap.yaml` and are copied into
the user configuration area during setup. Most runtime configuration then moves into the local
database and is managed by the UI or `gobby-config` MCP tools.

## Managed Datastore Stack

Gobby ships a unified Docker Compose service template with three required local
profiles: PostgreSQL, Qdrant, and FalkorDB. A full install provisions all three,
independent of the selected embedding provider.

| Service | Image | Default endpoint | Purpose |
|---------|-------|------------------|---------|
| PostgreSQL | `gobby-postgres-local:18-pgsearch` | `127.0.0.1:60891` | Authoritative relational storage and `pg_search` indexing |
| Qdrant | `qdrant/qdrant:latest` | `http://localhost:6333` | Vector storage for semantic search and code-symbol embeddings |
| FalkorDB | `falkordb/falkordb:latest` | Redis protocol `127.0.0.1:16379`, Browser `http://localhost:13000` | Graph storage for graph-augmented search and memory relationships |

The installer writes the Compose file under `~/.gobby/services/docker-compose.yml`.
`gobby start` starts all profiles and waits for their health checks before
launching the daemon.

Run the default installer to configure hooks, an embedding provider, and the
required managed services:

```bash
uv run gobby install
```

Use `--config-only` to configure and repair the required stack without touching
CLI or Git hooks:

```bash
uv run gobby install --config-only
printf '%s' 'your-password' | uv run gobby install --config-only --falkordb-password-stdin
```

## Embeddings

Semantic search needs an embedding endpoint. Gobby uses OpenAI-compatible embedding APIs and
stores vectors in Qdrant when the local vector stack is enabled.

| Provider path | Typical model/config | Notes |
|---------------|----------------------|-------|
| Ollama | `nomic-embed-text`, `http://localhost:11434/v1` | Uses the `ollama` CLI, 768 dimensions |
| LM Studio | `text-embedding-nomic-embed-text-v1.5@f16`, `http://localhost:1234/v1` | Uses the `lms` CLI, 768 dimensions |
| OpenAI | `text-embedding-3-small` with an OpenAI API key | Hosted embeddings, 1536 dimensions |
| None | No embedding provider | Managed datastores still install and start; semantic embedding work is disabled |

The default configuration model is `nomic-embed-text` with 768 dimensions. Installer-selected
OpenAI embeddings set dimensions to 1536. If you change models, make sure `embeddings.dim`
matches the model output.

For provider details, see [search.md](./search.md).

## Local generation runtimes

Gobby does not install, launch, or stop local chat servers. Start the runtime
yourself, then add a named endpoint under `ai.generation.endpoints`. Canonical
vLLM and vllm-metal share the Gobby protocol value `vllm`.

| Runtime | Typical `api_base` | Gobby protocol | Notes |
|---------|--------------------|----------------|-------|
| LM Studio | `http://localhost:1234` | `lmstudio` | `lms` CLI; web chat via Codex OSS |
| Ollama | `http://localhost:11434` | `ollama` | `ollama` CLI; web chat via Codex OSS |
| vLLM | `http://localhost:8000/v1` | `vllm` | CUDA/ROCm hosts; web chat via Codex config-override (`wire_api="chat"`), not `--oss`. Tool calling requires `--enable-auto-tool-choice --tool-call-parser <parser>` (`hermes` for Qwen) |
| vllm-metal | `http://localhost:8000/v1` | `vllm` | Apple Silicon/MLX plugin; same protocol as canonical vLLM, including the `--enable-auto-tool-choice --tool-call-parser <parser>` tool-calling flags. Install: [vllm-metal installation](https://docs.vllm.ai/projects/vllm-metal/en/latest/installation/) |

Copy-pasteable endpoint YAML, the `model: auto` exactly-one rule, and API-key
handling live in [llm-features.md](./llm-features.md).

## Memory And Search Footprint

The full local search footprint depends on your data volume.

| Component | Working requirement |
|-----------|---------------------|
| PostgreSQL | Grows with sessions, tasks, transcripts, memories, and task history |
| Qdrant | Grows with vector count and dimensions; SSD/NVMe storage is strongly preferred |
| FalkorDB | Grows with extracted entities and relationships |
| Embedding provider | Small embedding models can run on CPU; local chat models loaded alongside them dominate RAM/VRAM |

Docker Desktop users should allocate enough VM memory for both Qdrant and FalkorDB. A practical
floor is 8 GB assigned to Docker for small local use; 12-16 GB gives better headroom once vector
collections and graph data grow. Leave at least 4 GB for the host OS.

On Linux, Docker Engine uses host memory directly, so there is no Docker Desktop allocation
slider. You still need enough RAM for the daemon, Docker services, and any local embedding/chat
models running at the same time.

## Ports

Default ports are chosen to avoid common development-server conflicts.

| Port | Owner |
|------|-------|
| 60887 | Gobby HTTP API and installed web UI |
| 60888 | Gobby WebSocket server |
| 60889 | Gobby dev web UI |
| 60891 | Gobby PostgreSQL hub (Docker, mapped from container port 5432) |
| 6333 | Qdrant HTTP |
| 6334 | Qdrant gRPC |
| 13000 | FalkorDB Browser |
| 16379 | FalkorDB Redis protocol, mapped from container port 6379 |

If a port is already in use, change the matching bootstrap/config value before starting the
daemon or pass the relevant installer flag where one exists. Qdrant exposes
`--port` on `gobby qdrant install`; FalkorDB's shipped Compose mapping uses the
fixed 13000/16379 host ports.

## Storage

Use SSD or NVMe storage for any full-stack local install. PostgreSQL, Qdrant,
and FalkorDB all suffer on slow or networked filesystems under write-heavy use.

Prefer Docker named volumes for PostgreSQL, Qdrant, and FalkorDB data. The
shipped Compose template uses named volumes:

| Volume | Purpose |
|--------|---------|
| `gobby_qdrant_data` | Qdrant vector storage |
| `gobby_falkordb_data` | FalkorDB graph data |
| `gobby_postgres_data` | PostgreSQL hub data |
| `gobby_pgaudit_log` | pgaudit logs |

Hub-owned `USER.md`, `_personal`, and wiki files are not those volumes. They
live in a host bind directory. See [Hub-owned files home](#hub-owned-files-home).

## Hub-owned files home

Wiki vaults, the `_personal` tree, and the working profile are hub semantics.
There is one copy, on the hub host. The hub-local profile path is
`<files_home>/USER.md`. It is not `$GOBBY_HOME/personal/USER.md`.

Writers never create the `files_home` root. Provision that existing
absolute directory on the hub first, then install, then migrate, then start.

### Hub host

1. Create the bind directory yourself. On a standalone or laptop hub use
   `$GOBBY_HOME/files` (typically `~/.gobby/files`). A dedicated server may
   use `/var/lib/gobby/files`. It must already exist, must not be a
   filesystem root, and must be disjoint from `$GOBBY_HOME/personal`,
   `$GOBBY_HOME/projects`, and `~/wiki/topics`.
2. Install against that directory:

   ```bash
   uv run gobby install --files-home "$HOME/.gobby/files"
   ```

3. Upgrade or stop every remote before migrate. Copy leftover node-local
   `USER.md`, personal tree, wiki, and project attachments onto the hub's
   legacy source locations first (`$GOBBY_HOME/personal`, `~/wiki/topics`,
   `$GOBBY_HOME/projects/<id>/attachments`). This campaign does not collect
   files from other machines.
4. With the hub daemon stopped, migrate, then start:

   ```bash
   uv run gobby files migrate
   uv run gobby start
   ```

Do not run `gobby start` until migrate has finished. Migrate holds the
maintenance singleton while the daemon is stopped.

### Remote node

Remote bootstrap requires `hub_daemon_url` (the hub owner's HTTP origin,
not `daemon_url`) and refuses `files_home`. Copy the hub's existing
`local_cli_token` to the node. Remote `gobby install` authenticates with
that token; it does not generate or rotate one.

```yaml
datastore_mode: "remote"
hub_daemon_url: "http://<hub-host>:60887"
```

```bash
scp <hub>:~/.gobby/local_cli_token ~/.gobby/local_cli_token
chmod 600 ~/.gobby/local_cli_token
uv run gobby install
```

Remote intro and profile writes use `PUT /api/files/user-md` on
`hub_daemon_url`. They do not create `~/.gobby/personal`.

The owner contract lives in
[hub-owned-files-home.md](../architecture/hub-owned-files-home.md).

## Troubleshooting

### `uv run gobby install` Cannot Start Docker Services

Verify Docker is installed, running, and provides the `docker compose` command. Re-run with
the daemon stopped after resolving the Docker or Compose health error. The
managed stack cannot be skipped.

### Qdrant Is Not Healthy

Check that port 6333 is free and that the `gobby_qdrant_data` volume is writable. You can install
or reinstall Qdrant with:

```bash
uv run gobby qdrant install --port 6333
```

### FalkorDB Authentication Fails

Set or rotate the password during install:

```bash
printf '%s' 'your-password' | uv run gobby install --config-only --falkordb-password-stdin
```

The configured auth value is stored in Gobby configuration, and the Compose
container receives the password through `GOBBY_FALKORDB_PASSWORD`.

### Local Embeddings Are Slow

Use a smaller embedding model, switch to a hosted embedding endpoint, or disable embeddings when
semantic search is not required. If a local chat model is also loaded, it usually consumes far
more RAM or VRAM than the embedding model.

## See Also

- [configuration.md](./configuration.md) - Daemon and project configuration
- [llm-features.md](./llm-features.md) - Generation endpoint candidates, vLLM, and vllm-metal
- [providers-and-models.md](./providers-and-models.md) - Provider catalogs and web-chat transports
- [search.md](./search.md) - Search and embedding configuration
- [memory.md](./memory.md) - Memory backend configuration
- [hub-owned-files-home.md](../architecture/hub-owned-files-home.md) - Hub files owner contract
- [CONTRIBUTING.md](../../CONTRIBUTING.md) - Development environment setup

_Last verified: 2026-08-20_
