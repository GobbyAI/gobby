# Embedded Kùzu + Qdrant + Session Search

## Context

Gobby currently depends on Docker-managed Neo4j and Qdrant servers for graph storage and vector search. This conflicts with the local-first philosophy — users must run Docker containers just to get knowledge graph and semantic search features.

**Goals:**
1. Replace Neo4j with Kùzu (embedded graph DB, no server process)
2. Make Qdrant embedded-first (already partially supported via `path` mode)
3. Build session search combining FTS5 + Qdrant + graph traversal
4. Keep optional Docker/remote support for scaled deployments

**Approach: Strangler fig.** All existing Neo4j/Qdrant code stays in place. New code is added alongside. Backend is switchable via config (`graph_backend: kuzu | neo4j`). Old code is removed only after the new path is verified end-to-end.

**Key discovery:** gcode already defers Neo4j/Qdrant **writes** to the daemon via `graph_synced`/`vectors_synced` flags. Only **reads** (graph_boost, blast_radius, vector_search) still call Neo4j/Qdrant directly and need daemon API routes.

---

## Phase 1: GraphDatabaseProtocol + Vector Decoupling

**Goal:** Abstract Neo4j behind a protocol. Move vector operations from Neo4j proprietary procedures to Qdrant.

### 1.1 Define storage protocols

- Create `src/gobby/storage/protocols.py` with two protocols:

**GraphDatabaseProtocol:**
- `query()`, `execute_read()`, `execute_write()`, `merge_node()`, `merge_relationship()`, `get_entity_graph()`, `get_entity_neighbors()`, `ping()`, `close()`
- Implementations: `Neo4jClient`, `KuzuClient` (Phase 2)

**VectorStoreProtocol:**
- `upsert()`, `search()`, `delete()`, `ensure_collection()`, `collection_exists()`, `ping()`, `close()`
- Implementations: `QdrantVectorStore` (wraps existing `VectorStore`, both embedded and remote modes)
- Future: Milvus, ChromaDB, etc.

Both protocols are backend-agnostic — consumers never know which implementation they're talking to.

### 1.2 Neo4jClient implements protocol (no changes to Neo4jClient itself)

- Verify `Neo4jClient` (`src/gobby/memory/neo4j_client.py`) structurally satisfies the protocol
- Minor signature adjustments if needed
- **Neo4jClient stays fully intact** — it's still the default backend until Kùzu is verified

### 1.3 Move entity vectors from Neo4j to Qdrant

- In `KnowledgeGraphService` (`src/gobby/memory/services/knowledge_graph.py`):
  - Replace `self._neo4j.set_node_vector()` → `self._vector_store.upsert()` into `kg_entities` collection
  - Replace `self._neo4j.vector_search()` → `self._vector_store.search()` on `kg_entities`
  - Remove `self._neo4j.ensure_vector_index()` call
- Eliminates all Neo4j proprietary procedure calls

### 1.4 Refactor consumers to accept protocols

- `KnowledgeGraphService.__init__`: `neo4j_client: Neo4jClient` → `graph_client: GraphDatabaseProtocol`
- `CodeGraph.__init__` (`src/gobby/code_index/graph.py`): `neo4j_client: Any` → `graph_client: GraphDatabaseProtocol | None`
- `MemoryManager`: accept `GraphDatabaseProtocol` + `VectorStoreProtocol` instead of concrete classes
- `memory.py` routes: replace `server.memory_manager._neo4j_client` with `._graph_client`
- `VectorStore` usage sites: accept `VectorStoreProtocol` instead of `VectorStore` directly

### 1.5 Update tests

- Tests mocking `Neo4jClient` → mock `GraphDatabaseProtocol`
- Tag integration tests needing real Neo4j with `@pytest.mark.neo4j`

**Files modified:**
- `src/gobby/memory/graph_protocol.py` (new)
- `src/gobby/memory/neo4j_client.py`
- `src/gobby/memory/services/knowledge_graph.py`
- `src/gobby/memory/manager.py`
- `src/gobby/code_index/graph.py`
- `src/gobby/servers/routes/memory.py`
- ~27 test files referencing neo4j

---

## Phase 2: Kùzu Implementation + Config

**Goal:** Add Kùzu as embedded graph backend, make it the default.

### 2.1 KuzuClient

- Create `src/gobby/memory/kuzu_client.py` implementing `GraphDatabaseProtocol`
- Database path: `~/.gobby/kuzu/`
- Schema: node tables (`_Entity`, `Memory`, `CodeFile`, `CodeSymbol`, `CodeModule`), relationship tables (`CALLS`, `IMPORTS`, `DEFINES`, `MENTIONED_IN`, `RELATES_TO_CODE`)
- openCypher translation notes:
  - `labels(n)` → use table name
  - `properties(n)` → explicit property listing
  - `MERGE ... ON CREATE SET` → `OPTIONAL MATCH` + conditional `CREATE`/`SET` (Kùzu MERGE is more limited)
  - Variable-length paths `[*1..N]` → supported but returned differently

### 2.2 Config updates

- `src/gobby/config/persistence.py` — unified, symmetric config model:

```yaml
storage:
  graph_backend: neo4j         # neo4j | kuzu (strangler fig: neo4j default initially)
  vector_backend: qdrant       # qdrant (future: milvus, chromadb...)
  use_docker: true             # true → backends run via Docker Compose (current default)

  kuzu:
    path: ~/.gobby/kuzu/

  neo4j:
    url: http://localhost:8474
    auth: neo4j:password
    database: neo4j

  qdrant:
    path: ~/.gobby/qdrant/     # used when use_docker: false
    url: http://localhost:6333  # used when use_docker: true
    api_key: null
    collection_prefix: code_symbols_
```

- `graph_backend` selects which `GraphDatabaseProtocol` implementation to instantiate
- `vector_backend` selects which `VectorStoreProtocol` implementation to instantiate
- `use_docker` determines whether embedded (path) or remote (url) mode is used
- Adding a new backend = new protocol implementation + config block + enum value

### 2.3 Factory wiring

- Daemon init reads `graph_backend` → instantiates the right `GraphDatabaseProtocol`
- Daemon init reads `vector_backend` + `use_docker` → instantiates the right `VectorStoreProtocol`
- **Cutover:** Once verified, flip defaults: `graph_backend: kuzu`, `use_docker: false`

### 2.4 Migration CLI

- `gobby graph migrate` command: reads Neo4j → writes to Kùzu, migrates entity vectors to `kg_entities` Qdrant collection

**Files modified:**
- `src/gobby/memory/kuzu_client.py` (new)
- `src/gobby/config/persistence.py`
- `src/gobby/runner.py` or wherever MemoryManager is constructed
- `src/gobby/cli/` (new migrate command)

---

## Phase 3: gcode → Daemon API for Graph/Vector (FTS5-Only Standalone)

**Goal:** gcode routes graph/vector reads through the daemon API. Standalone mode uses FTS5 only (already works). This avoids embedded DB concurrency issues and minimizes throwaway code before the Rust gobby merge.

### 3.1 Daemon graph/vector API routes (gobby side)

Create `src/gobby/servers/routes/graph.py`:
- `GET /api/graph/callers?symbol=&project_id=&skip=&limit=`
- `GET /api/graph/usages?symbol=&project_id=&skip=&limit=`
- `POST /api/graph/callers/batch` → `{names, project_id, limit}`
- `POST /api/graph/callees/batch` → `{names, project_id, limit}`
- `GET /api/graph/imports?file=&project_id=`
- `GET /api/graph/blast-radius?symbol=&project_id=&depth=`

Create `src/gobby/servers/routes/vector.py`:
- `POST /api/vector/search` → `{collection, vector, limit}`
- `POST /api/vector/upsert` → `{collection, points}`
- `POST /api/vector/delete` → `{collection, ids}`
- `POST /api/vector/ensure-collection` → `{collection, dim}`

Route implementations delegate to `CodeGraph` (graph ops) and `VectorStore` (vector ops).

### 3.2 gcode migration — minimal changes, same patterns

gcode already uses HTTP clients for Neo4j and Qdrant. The migration is re-pointing URLs:

| Current | New | What changes |
|---------|-----|-------------|
| `neo4j.rs` → `POST http://localhost:7474/db/neo4j/query/v2` | `daemon.rs` → `GET http://localhost:60887/api/graph/callers` etc. | URL + JSON format, same `reqwest` client |
| `semantic.rs` → `POST http://localhost:6333/collections/.../points/search` | `daemon.rs` → `POST http://localhost:60887/api/vector/search` | URL + JSON format, same `reqwest` client |

**What stays the same:**
- `embed_text`/`embed_texts` (local GGUF embedding) — unchanged
- Write deferral via `graph_synced`/`vectors_synced` — unchanged
- Graceful degradation pattern (`with_neo4j` → `with_daemon`) — same logic, different target
- `resolve_daemon_url()` — already exists in gcode

### 3.3 gcode only talks to the daemon API

- gcode should never call graph/vector databases directly — the daemon API is the contract
- If the daemon changes backends, gcode doesn't break
- Delete `neo4j.rs` and direct Qdrant REST code from gcode as part of this phase (not deferred)
- **Standalone (no daemon):** FTS5 only — graph/vector are daemon features
- **With daemon:** FTS5 + semantic + graph boost via daemon API, RRF merge

**Prompt for gcode modifications** (to be provided to a session working on gobby-cli):
> gcode must never call graph or vector databases directly — the daemon HTTP API is the only contract. Delete neo4j.rs entirely. Remove all direct Qdrant REST calls from semantic.rs. Replace graph_boost.rs calls with GET/POST /api/graph/* daemon endpoints. Replace semantic.rs vector operations (vector_search, ensure_collection, upsert_vectors, delete_vectors) with POST /api/vector/* daemon endpoints. Use resolve_daemon_url() to find the daemon. If daemon is unreachable, graph/vector operations return empty results — FTS5 search still works standalone. Keep local GGUF embedding (embed_text/embed_texts) in gcode. Write deferral via graph_synced/vectors_synced flags stays unchanged. Remove Neo4jConfig and QdrantConfig from config.rs (gcode no longer needs to know about storage backends).

**Files modified:**
- `src/gobby/servers/routes/graph.py` (new)
- `src/gobby/servers/routes/vector.py` (new)
- gcode: `neo4j.rs` (deleted), `semantic.rs`, `graph_boost.rs`, `config.rs`

---

## Phase 4: Session Search

**Goal:** FTS5 + Qdrant + graph traversal session search with RRF hybrid scoring.

### 4.1 Session FTS5 table

- Add `sessions_fts` to migrations (`src/gobby/storage/migrations.py`)
- Content-synced with `sessions` table via triggers
- Columns: `title`, `original_prompt`, `summary_markdown`, `digest_markdown`, `last_assistant_content`
- BM25 weights: title (10.0), original_prompt (5.0), summary (3.0), digest (2.0), last_assistant (1.0)

### 4.2 Session embeddings

- On digest update (existing lifecycle event), embed `title + summary_markdown` into Qdrant `session_embeddings` collection
- Point ID = session UUID, payload = `{project_id, source, status, created_at}`

### 4.3 Session-entity graph links

- Add `Session` node table to Kùzu schema
- `PRODUCED` relationship: `Session → Memory`
- Enables traversal: query entity → related entities → memories → sessions

### 4.4 Hybrid search endpoint

- `GET /api/sessions/search?q=&project_id=&limit=10`
- Three sources merged via RRF (K=60):
  1. FTS5 on `sessions_fts` (keyword)
  2. Qdrant on `session_embeddings` (semantic)
  3. Kùzu traversal: embed query → find matching entities → traverse to sessions (graph)
- Response: `{query, results: [{session_id, title, score, sources, snippet, created_at}]}`

### 4.5 Backfill CLI

- `gobby sessions reindex`: populates FTS5, embeds all sessions, creates Session nodes + PRODUCED edges

**Files modified:**
- `src/gobby/storage/migrations.py`
- `src/gobby/search/session_search.py` (new)
- `src/gobby/servers/routes/sessions/search.py` (new, or extend existing sessions routes)
- `src/gobby/sessions/lifecycle.py` (hook embedding on digest update)
- `src/gobby/cli/sessions.py` (reindex command)

---

## Phase 5: Cutover + Cleanup (after verification)

**Only execute after Phases 1-4 are verified end-to-end with `graph_backend: kuzu` + `qdrant.mode: embedded`.**

- Flip config defaults: `graph_backend: kuzu`, `qdrant.mode: embedded`
- Update `gobby install` default → embedded (no Docker required)
- Add `gobby install --docker-services` → installs Neo4j + Qdrant via Docker (current behavior, now opt-in)
- Remove direct Neo4j/Qdrant code from gcode (`neo4j.rs`, Qdrant REST in `semantic.rs`)
- **Keep Neo4j as a first-class option** — `Neo4jClient` remains as a `GraphDatabaseProtocol` implementation. Users who want Docker isolation set `graph_backend: neo4j` in config.
- **Keep remote Qdrant as a first-class option** — `qdrant.mode: remote` + `qdrant.url` for Docker/cloud Qdrant.

### Install matrix

| Command | graph_backend | vector_backend | use_docker |
|---------|--------------|----------------|------------|
| `gobby install` (default) | kuzu | qdrant | false |
| `gobby install --docker-services` | neo4j | qdrant | true |
| Custom config | any supported | any supported | true/false |

Adding a new backend (e.g., FalkorDB, Milvus) requires:
1. Implement `GraphDatabaseProtocol` or `VectorStoreProtocol`
2. Add config model (e.g., `FalkorDBConfig`)
3. Add enum value to `graph_backend` or `vector_backend`
4. Wire into factory — no consumer changes needed

---

## Verification

1. **Phase 1:** All existing tests pass. `grep -r "Neo4jClient" src/` shows only `neo4j_client.py` itself
2. **Phase 2:** `gobby start --verbose` → Kùzu initializes. Knowledge graph UI renders entities from Kùzu
3. **Phase 3:** `gcode search "query"` with daemon running → graph boost and semantic search work. `gcode search` without daemon → graceful fallback to FTS5 only
4. **Phase 4:** `GET /api/sessions/search?q=auth+middleware` returns relevant sessions ranked by hybrid score
5. **Phase 5:** Fresh `gobby install && gobby start` → no Docker, full functionality

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Kùzu openCypher gaps (MERGE syntax, labels()) | Each protocol method gets a Kùzu-native implementation, not raw Cypher passthrough |
| gcode standalone loses graph/vector search | Acceptable tradeoff — FTS5 still works standalone. Graph/semantic are daemon-tier features. Resolves cleanly when gobby moves to Rust (single binary, shared embedded DBs) |
| Embedded DB concurrency (Kùzu/Qdrant are single-process) | Only the daemon opens the embedded DBs. gcode accesses them via daemon HTTP API. No multi-process file locking issues |
| 27 test files need updates | Phase 1 addresses this with protocol mocks before any backend swap |
| Kùzu concurrent access from daemon threads | Kùzu supports multi-threaded read; writes serialized via `asyncio.to_thread()` |
