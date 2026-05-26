# Plan: Gobby Memory Upgrade — Embeddings + Mem0 Integration

## Context

Gobby's memory system has rich lifecycle features (decay, tags, crossrefs, project binding) but only TF-IDF/text search. We want semantic search as the default, plus optional mem0 integration for enhanced capabilities.

**Two modes:**
- **gobby-memory standalone**: Semantic search via existing `UnifiedSearcher`/`EmbeddingBackend` infra (LiteLLM embeddings). Works without Docker.
- **gobby-memory + mem0**: `gobby install mem0` adds Docker containers. Gobby routes storage/search through mem0's REST API. Enhanced with graph memory, mem0 UI, and mem0's embedding pipeline. `gobby uninstall mem0` reverts to standalone.

When mem0 is installed locally, `gobby start/stop/restart` manages its Docker containers (same pattern as the web UI).

---

## Part 1: Add Embedding Search to Gobby-Memory (Standalone)

### 1.1 Wire `UnifiedSearcher` into memory search

**File: `src/gobby/memory/search/coordinator.py`**
- Replace current TF-IDF/text-only backend init with `UnifiedSearcher` from `src/gobby/search/unified.py`
- Supports modes: `tfidf`, `embedding`, `auto` (try embeddings, fallback to TF-IDF), `hybrid`
- Map memory config fields to `SearchConfig` from `src/gobby/search/models.py`
- Keep text-search as last-resort fallback

### 1.2 Add `memory_embeddings` table

**File: `src/gobby/storage/migrations.py`**
- Follow `tool_embeddings` pattern:
  ```sql
  CREATE TABLE memory_embeddings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
      embedding BLOB NOT NULL,
      embedding_model TEXT NOT NULL,
      embedding_dim INTEGER NOT NULL,
      text_hash TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(memory_id)
  );
  ```

### 1.3 Persist embeddings on CRUD

**File: `src/gobby/storage/memories.py`** (or new `src/gobby/storage/memory_embeddings.py`)
- `create_memory`: generate embedding via `src/gobby/search/embeddings.py`, store in `memory_embeddings`
- `update_memory`: regenerate if content changed (check `text_hash`)
- `delete_memory`: FK cascade handles cleanup
- Batch generation for reindex

### 1.4 Update memory config

**File: `src/gobby/config/persistence.py`**
- Expand `search_backend` options: `tfidf`, `embedding`, `auto`, `hybrid`
- Add: `embedding_model`, `embedding_weight`, `tfidf_weight` (from `SearchConfig`)
- Default: `auto` (tries embeddings, falls back to TF-IDF if no API key)

**Existing infra to reuse:**
- `src/gobby/search/embeddings.py` — LiteLLM embedding generation (OpenAI, Ollama, etc.)
- `src/gobby/search/backends/embedding.py` — `EmbeddingBackend` with cosine similarity
- `src/gobby/search/unified.py` — `UnifiedSearcher` with auto/hybrid modes
- `src/gobby/search/models.py` — `SearchConfig` model

---

## Part 2: Mem0 Integration (Optional Install)

### 2.1 `gobby install mem0` / `gobby uninstall mem0`

**New file: `src/gobby/cli/install.py`** (or add to existing CLI group)

`gobby install mem0`:
1. Check Docker is available (`docker compose version`)
2. Copy bundled `docker-compose.mem0.yml` to `~/.gobby/services/mem0/`
3. Create `.env` with defaults (postgres creds, neo4j creds) + prompt for `OPENAI_API_KEY` if not in env
4. Run `docker compose -f ... up -d`
5. Wait for health check (`GET http://localhost:8888/docs`)
6. Update `~/.gobby/config.yaml`: set `memory.mem0_url: http://localhost:8888`
7. Print success + URL

`gobby install mem0 --remote <url>`:
1. Skip Docker setup
2. Verify remote is reachable (`GET <url>/docs`)
3. Update config: `memory.mem0_url: <url>`, `memory.mem0_api_key: <key>` if provided

`gobby uninstall mem0`:
1. Stop containers: `docker compose -f ... down -v` (with confirmation for `-v`)
2. Remove `~/.gobby/services/mem0/`
3. Reset config: remove `mem0_url` / `mem0_api_key`
4. Memory system reverts to standalone

### 2.2 Bundle docker-compose file

**New file: `src/gobby/data/docker-compose.mem0.yml`** (packaged with the Python distribution)
- mem0 API service (port 8888)
- Postgres + pgvector (port 8432)
- Neo4j (ports 8474, 8687)
- Volumes for persistence
- Based on `/tmp/mem0/server/docker-compose.yaml` but adapted (use published image instead of dev build)
- All services use `restart: unless-stopped` so they persist across reboots

### 2.3 Lifecycle — containers run independently

Mem0 Docker containers are **persistent services**, not tied to gobby daemon lifecycle. They run independently and survive `gobby stop/restart`, machine reboots, etc.

- `gobby install mem0` → starts containers with `restart: unless-stopped` policy. They stay running.
- `gobby start/stop/restart` → does NOT touch mem0 containers
- `gobby status` → shows mem0 health (running / unreachable / not installed)
- `gobby uninstall mem0` → only command that stops/removes containers

**File: `src/gobby/cli/daemon.py`** — modify `start`, `stop`, `restart`, `status`
- Add `--mem0` flag to `start`, `stop`, `restart`:
  - `gobby start --mem0` → start daemon AND mem0 containers
  - `gobby stop --mem0` → stop daemon AND mem0 containers
  - `gobby restart --mem0` → restart both
  - Without `--mem0` → mem0 containers untouched (default)
- Add mem0 status line to `gobby status` output (installed? healthy?)

**New file: `src/gobby/cli/services.py`** (utility functions)
- `is_mem0_installed() -> bool` — check `~/.gobby/services/mem0/` exists
- `is_mem0_healthy() -> bool` — HTTP health check to configured `mem0_url`
- `get_mem0_status() -> dict` — container status info for `gobby status`

### 2.4 Mem0 REST client

**New file: `src/gobby/memory/mem0_client.py`**
- Async HTTP client using `httpx.AsyncClient`
- Wraps mem0 REST API endpoints (based on actual OpenAPI spec at `/tmp/mem0/server/main.py`):
  - `POST /memories` — create with `messages`, `user_id="gobby"`, `metadata={"project_id": "...", "memory_type": "..."}`
  - `POST /search` — semantic search with `query`, `filters={"project_id": "..."}`
  - `GET /memories/{memory_id}` — get single memory
  - `PUT /memories/{memory_id}` — update content
  - `DELETE /memories/{memory_id}` — delete single
  - `GET /memories?user_id=gobby` — list all
  - `GET /memories/{memory_id}/history` — version history
- Project scoping: `metadata.project_id` on create, `filters.project_id` on search
- `user_id="gobby"` as namespace (keeps user_id/agent_id free for future multi-user)
- Connection pooling, retry logic, timeout handling via `httpx.AsyncClient`

### 2.5 Dual-mode memory manager

**File: `src/gobby/memory/manager.py`**

**PostgreSQL is always the source of truth.** Mem0 is a search enhancement layer.

When mem0 is configured (`mem0_url` in config) AND reachable:
- `remember()`: Store content + metadata in PostgreSQL, THEN index in mem0 (with `metadata={"project_id": ...}`). Set `mem0_id` in PostgreSQL on success.
- `recall()`: Query mem0 for semantic results → enrich with local metadata (tags, decay) → apply gobby filters
- `forget()`: Delete from PostgreSQL + delete from mem0
- `update()`: Update in PostgreSQL + update in mem0

When mem0 is NOT configured:
- Use standalone mode (PostgreSQL + UnifiedSearcher embeddings, from Part 1)

When mem0 is configured but UNREACHABLE:
- `remember()`: Store in PostgreSQL only. `mem0_id` stays NULL (marks it as unsynced).
- `recall()`: Fall back to standalone search (Part 1 embeddings/TF-IDF). Log warning once per session, not per call.
- `forget()`/`update()`: Apply to PostgreSQL. Queue mem0 operation for later.
- **Lazy sync**: On next successful mem0 connection, background-sync memories where `mem0_id IS NULL` to mem0. No manual intervention needed.

### 2.6 Config changes

**File: `src/gobby/config/persistence.py`**
```python
class MemoryConfig:
    # Mem0 connection (None = standalone mode)
    mem0_url: str | None = None
    mem0_api_key: str | None = None  # Supports env var expansion: ${MEM0_API_KEY}

    # Search config (used in standalone mode)
    search_backend: str = "auto"
    embedding_model: str = "text-embedding-3-small"
    tfidf_weight: float = 0.4
    embedding_weight: float = 0.6

    # Lifecycle features (used in both modes)
    importance_threshold: float = 0.7
    decay_enabled: bool = True
    decay_rate: float = 0.05
    decay_floor: float = 0.1
    auto_crossref: bool = False
    crossref_threshold: float = 0.3
```

---

## Part 3: Clean Up Old Code

### 3.1 Remove old backends

**Delete:**
- `src/gobby/memory/backends/openmemory.py` — replaced by mem0 client
- `src/gobby/memory/backends/PostgreSQL.py` — no longer a separate backend (PostgreSQL metadata stays in `storage/memories.py`)

**Refactor:**
- `src/gobby/memory/backends/mem0.py` — replace with new `mem0_client.py`

**Keep:**
- `src/gobby/memory/backends/null.py` — testing

### 3.2 Remove from config

- Remove `OpenMemoryConfig` from `src/gobby/config/persistence.py`
- Remove old `Mem0Config` (replaced by `mem0_url`/`mem0_api_key` fields)
- Remove old `backend` field (no longer choosing between PostgreSQL/mem0/openmemory)
- Remove `search_backend` valid options restriction to just `tfidf`/`text`

---

## Part 4: Memory Tab Web UI

### 4.1 Core components

| Component | Description |
|-----------|------------|
| `MemoryPage.tsx` | Top-level: stats bar + search + memory list. Shows different features based on mem0 availability. |
| `MemoryTable.tsx` | Table: content, type, importance, tags, source, date, actions |
| `MemoryDetail.tsx` | Detail view: content, metadata, access log, related memories |
| `MemoryFilters.tsx` | Search bar + filters (type, tags, importance, project) |
| `MemoryForm.tsx` | Create/edit dialog |
| `MemoryStats.tsx` | Stats: total, by type, avg importance |
| `MemoryGraph.tsx` | vis.js knowledge graph (existing `memory/viz.py`) |

### 4.2 Mode-dependent UI

**Without mem0:**
- Memory list, search, CRUD, tag management, importance editing
- Knowledge graph visualization
- Stats panel

**With mem0 (additional features):**
- "Powered by mem0" indicator with link to mem0 UI at configured URL
- Graph memory visualization (Neo4j data via mem0 API)
- Richer search results (semantic similarity scores)
- Category auto-assignment display

### 4.3 Hook + integration

**New file: `web/src/hooks/useMemory.ts`**
- CRUD via gobby HTTP API
- Debounced search, filter/pagination state
- WebSocket for real-time updates
- `useMem0Status()` — check if mem0 is configured + healthy

**File: `web/src/App.tsx`**
- Replace `<ComingSoonPage title="Memory" />` with `<MemoryPage />`

---

## Key Files

| File | Action |
|------|--------|
| `src/gobby/memory/search/coordinator.py` | Wire UnifiedSearcher |
| `src/gobby/storage/migrations.py` | Add memory_embeddings table |
| `src/gobby/storage/memories.py` | Persist embeddings on CRUD |
| `src/gobby/config/persistence.py` | Simplify config + add mem0 fields |
| `src/gobby/memory/mem0_client.py` | New — async REST client |
| `src/gobby/memory/manager.py` | Dual-mode (standalone / mem0) |
| `src/gobby/cli/install.py` | New — `gobby install/uninstall mem0` |
| `src/gobby/cli/services.py` | New — mem0 Docker lifecycle utils |
| `src/gobby/cli/daemon.py` | Add mem0 to start/stop/restart/status |
| `src/gobby/data/docker-compose.mem0.yml` | New — bundled compose file |
| `src/gobby/memory/backends/openmemory.py` | Delete |
| `src/gobby/memory/backends/mem0.py` | Delete (replaced by mem0_client) |
| `src/gobby/memory/backends/PostgreSQL.py` | Delete |
| `web/src/App.tsx` | Wire MemoryPage |
| `web/src/components/MemoryPage.tsx` | New |
| `web/src/hooks/useMemory.ts` | New |
| `docs/guides/memory.md` | Major update — search modes, mem0, config |
| `docs/guides/mem0-integration.md` | New — dedicated mem0 setup guide |
| `docs/plans/memory-v4.md` | Move from abandoned, update with this plan |

## Part 5: Documentation

### 5.1 Update existing docs

**`docs/guides/memory.md`** — Major update:
- Add "Search Modes" section: `tfidf`, `embedding`, `auto`, `hybrid`
- Add "Mem0 Integration" section: what it adds, how to install/uninstall
- Update "Configuration" section with new fields (`search_backend`, `embedding_model`, `mem0_url`, etc.)
- Update "Backends" section: remove openmemory, explain standalone vs mem0 modes
- Document env var expansion for `mem0_api_key`: `${MEM0_API_KEY}`

### 5.2 New docs

**`docs/guides/mem0-integration.md`** — Dedicated mem0 guide:
- Prerequisites (Docker)
- `gobby install mem0` walkthrough (local)
- `gobby install mem0 --remote <url>` for remote/self-hosted
- Project scoping (how `project_id` maps to mem0 `user_id`)
- Accessing the mem0 UI
- Lifecycle management (`gobby start/stop` manages containers)
- `gobby uninstall mem0` and data cleanup
- Troubleshooting (port conflicts, Docker not running, API key issues)

### 5.3 Update plan docs

- Move `docs/plans/abandoned/memory-v4.md` → `docs/plans/memory-v4.md` (this plan supersedes the abandoned research)

### 5.4 Config documentation

Ensure `config.yaml` examples show env var expansion pattern:
```yaml
memory:
  mem0_url: http://localhost:8888
  mem0_api_key: ${MEM0_API_KEY}  # Expanded via expand_env_vars() at load time
```

`expand_env_vars()` in `src/gobby/config/app.py:123` already handles `${VAR}` and `${VAR:-default}` syntax — no code changes needed, just documentation.

---

## Verification

1. **Standalone semantic search**: Create memory → `gobby memory recall "what theme"` finds "user prefers dark mode" (via embeddings)
2. **Standalone fallback**: No API key → `auto` mode falls back to TF-IDF gracefully
3. **Install mem0**: `gobby install mem0` → containers start, config updated, health check passes
4. **Mem0 routing**: After install, create memory → visible in mem0 UI at localhost:8888
5. **Project scoping**: Memory in project A not returned from project B search
6. **Independence**: `gobby stop` && `gobby start` — mem0 containers unaffected, still healthy
7. **Uninstall**: `gobby uninstall mem0` → containers removed, config reset, standalone mode works
8. **Degradation**: Stop mem0 Docker manually → gobby logs warning, falls back to standalone search
9. **Web UI**: Memory tab loads, different features shown with/without mem0
10. **Round-trip**: Create in web UI → visible in CLI → visible in mem0 UI (when installed)
