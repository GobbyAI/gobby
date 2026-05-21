# System Requirements

Gobby runs as a local Python daemon with optional local services for semantic search and
graph-augmented memory. The daemon is small; the optional stack is what drives most hardware
and Docker requirements.

Use this guide to decide what has to be installed before running Gobby 0.4.0.

## Quick Matrix

| Setup | Required | Good default |
|-------|----------|--------------|
| Daemon only | Python 3.13+, `uv`, 1 GB free disk, localhost ports 60887 and 60888 | 4+ CPU cores, 8 GB RAM |
| Daemon + web UI | Daemon requirements; installed UI is served on port 60887 | 8 GB RAM |
| Full local search stack | Docker with Compose v2, Qdrant, Neo4j, embedding endpoint | 16 GB RAM minimum, 32 GB RAM preferred, SSD/NVMe storage |
| Local embedding model | LM Studio with `lms` or Ollama with `ollama` | 16 GB RAM, GPU or unified memory when also running a chat model |

If you need the daemon and hooks without installer-managed Qdrant or Neo4j, skip external
services during install:

```bash
uv run gobby install --no-ext-services
```

Semantic search still needs an embedding provider and a reachable vector backend. Choosing
`None` in the embedding provider prompt disables semantic search and also skips Qdrant/Neo4j
installation.

## Platform Notes

Gobby is packaged as a Python 3.13+ application and is developed for local developer machines.
The practical platform requirements are mostly set by Python, Docker, and whichever embedding
provider you choose.

| Platform | Notes |
|----------|-------|
| macOS | Docker Desktop provides the Linux VM for Qdrant and Neo4j. Apple Silicon is a practical target for the local full stack. |
| Linux | Use Docker Engine plus the Docker Compose plugin. Linux avoids the Docker Desktop VM memory allocation step. |
| Windows | Use Windows 10/11 with WSL2 for Docker-based services. Local shell tooling and filesystem paths should be verified in the target environment. |

The daemon can run without Docker. Docker is only required for the local Qdrant and Neo4j
services installed by the Gobby installer.

## Daemon

The daemon is a native Python process with a local PostgreSQL hub, FastAPI HTTP
server, WebSocket server, and optional UI dev server.

| Resource | Current value |
|----------|---------------|
| Python | 3.13+ (`requires-python = ">=3.13"`) |
| Package runner | `uv` for source/development commands such as `uv run gobby start` |
| Database | Local PostgreSQL hub configured through bootstrap/keyring `database_url` |
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

## Optional Services

Gobby 0.4.0 ships a unified Docker Compose service template with two local datastore profiles.
The default interactive installer offers an embedding provider first. When the selected provider
is not `none` and Docker is available, the installer configures Qdrant and Neo4j; `--no-ext-services`
skips this Docker step.

| Service | Image | Default endpoint | Purpose |
|---------|-------|------------------|---------|
| Qdrant | `qdrant/qdrant:latest` | `http://localhost:6333` | Vector storage for semantic search and code-symbol embeddings |
| Neo4j | `neo4j:latest` | HTTP `http://localhost:8474`, Bolt `localhost:8687` | Graph storage for graph-augmented search and memory relationships |

The installer writes the Compose file under `~/.gobby/services/docker-compose.yml`. `gobby start`
uses Docker Compose profiles to start installed services.

Run the default installer to configure hooks, embedding provider, and optional services:

```bash
uv run gobby install
```

Relevant installer flags:

```bash
uv run gobby install --no-ext-services
uv run gobby install --neo4j-password 'your-password'
uv run gobby qdrant install --port 6333
```

## Embeddings

Semantic search needs an embedding endpoint. Gobby uses OpenAI-compatible embedding APIs and
stores vectors in Qdrant when the local vector stack is enabled.

| Provider path | Typical model/config | Notes |
|---------------|----------------------|-------|
| Ollama | `nomic-embed-text`, `http://localhost:11434/v1` | Uses the `ollama` CLI, 768 dimensions |
| LM Studio | `text-embedding-nomic-embed-text-v1.5@f16`, `http://localhost:1234/v1` | Uses the `lms` CLI, 768 dimensions |
| OpenAI | `text-embedding-3-small` with an OpenAI API key | Hosted embeddings, 1536 dimensions |
| None | No embedding provider | Installer skips Qdrant/Neo4j setup when embeddings are disabled |

The default configuration model is `nomic-embed-text` with 768 dimensions. Installer-selected
OpenAI embeddings set dimensions to 1536. If you change models, make sure `embeddings.dim`
matches the model output.

For provider details, see [search.md](./search.md).

## Memory And Search Footprint

The full local search footprint depends on your data volume.

| Component | Working requirement |
|-----------|---------------------|
| PostgreSQL | Grows with sessions, tasks, transcripts, memories, and task history |
| Qdrant | Grows with vector count and dimensions; SSD/NVMe storage is strongly preferred |
| Neo4j | Grows with extracted entities and relationships |
| Embedding provider | Small embedding models can run on CPU; local chat models loaded alongside them dominate RAM/VRAM |

Docker Desktop users should allocate enough VM memory for both Qdrant and Neo4j. A practical
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
| 6333 | Qdrant HTTP |
| 6334 | Qdrant gRPC |
| 8474 | Neo4j HTTP, mapped from container port 7474 |
| 8687 | Neo4j Bolt, mapped from container port 7687 |

If a port is already in use, change the matching bootstrap/config value before starting the
daemon or pass the relevant installer flag where one exists. Qdrant exposes `--port` on
`gobby qdrant install`; Neo4j's shipped Compose mapping uses the fixed 8474/8687 host ports.

## Storage

Use SSD or NVMe storage for any full-stack local install. PostgreSQL, Qdrant,
and Neo4j all suffer on slow or networked filesystems under write-heavy use.

Prefer Docker named volumes for Qdrant and Neo4j data. The shipped Compose template uses named
volumes:

| Volume | Purpose |
|--------|---------|
| `gobby_qdrant_data` | Qdrant vector storage |
| `gobby_neo4j_data` | Neo4j graph data |
| `gobby_neo4j_logs` | Neo4j logs |

## Troubleshooting

### `uv run gobby install` Cannot Start Docker Services

Verify Docker is installed, running, and provides the `docker compose` command. Re-run with
`--no-ext-services` when you only need the daemon and cloud-hosted providers.

### Qdrant Is Not Healthy

Check that port 6333 is free and that the `gobby_qdrant_data` volume is writable. You can install
or reinstall Qdrant with:

```bash
uv run gobby qdrant install --port 6333
```

### Neo4j Authentication Fails

Set or rotate the password during install:

```bash
uv run gobby install --neo4j-password 'your-password'
```

The configured auth value is stored in Gobby configuration, and the Compose container receives
the password through `GOBBY_NEO4J_PASSWORD`.

### Local Embeddings Are Slow

Use a smaller embedding model, switch to a hosted embedding endpoint, or disable embeddings when
semantic search is not required. If a local chat model is also loaded, it usually consumes far
more RAM or VRAM than the embedding model.

## See Also

- [configuration.md](./configuration.md) - Daemon and project configuration
- [search.md](./search.md) - Search and embedding configuration
- [memory.md](./memory.md) - Memory backend configuration
- [CONTRIBUTING.md](../../CONTRIBUTING.md) - Development environment setup

_Last verified: 2026-05-07_
