# Replace Gobby's Memory System with Vendored mem0 Core

## Overview

Gobby's current memory system is a layered stack: PostgreSQL storage, TF-IDF/embedding search backends, a `SearchCoordinator` that routes between them, and an optional HTTP-client-to-Docker-service integration with mem0 (3 containers: mem0 API + PostgreSQL/pgvector + Neo4j).

Replace all of that with a single coherent memory system built from vendored mem0 core logic:
- **Qdrant embedded** (on-disk, zero Docker) for vector search
- **LLM-based fact extraction + dedup** as the default `create_memory` behavior
- **Knowledge graph** (Neo4j) built automatically when configured
- **Remove**: TF-IDF, SearchCoordinator, UnifiedSearcher, search backend selection, mem0 HTTP client, mem0 Docker service, PostgreSQL, background sync processor, importance scoring, decay

All `mem0` references removed from code. Vendored prompts carry Apache 2.0 attribution in YAML frontmatter.

## Constraints

- **PostgreSQL is source of truth** for memory records. Qdrant is a search index — rebuildable from PostgreSQL if lost.
- **Fire-and-forget async**: `create_memory` returns fast. LLM dedup + graph building run as background `asyncio.Task`s (same pattern as current `EmbeddingService`). Tasks tracked in `_background_tasks: set[asyncio.Task]` with auto-cleanup callbacks. Failures logged but never fail the caller.
- **No importance scoring or decay**: Vector similarity handles relevance. LLM dedup DELETE handles staleness. User-created memories (`source_type="user"`) get a similarity score boost (×1.2) in search.
- **Prompts as templates**: All LLM prompts stored as markdown in `install/shared/prompts/memory/`, loaded via `PromptLoader` (`src/gobby/prompts/loader.py`), user-overridable at project/global/bundled tiers.
- **Method names match MCP tools**: `remember()` → `create_memory()`, `recall()` → `search_memories()`, `forget()` → `delete_memory()`.
- **Haiku by default** for all background LLM calls (configurable via feature configs in `src/gobby/config/features.py`).
- **No explicit test tasks** — TDD expansion handles those.

## Phase 1: Qdrant Vector Store + Simplified Search

**Goal**: Replace the entire search layer (TF-IDF, SearchCoordinator, UnifiedSearcher, memory_embeddings BLOBs) with Qdrant embedded.

**Tasks:**
- [ ] Add qdrant-client dependency and remove mem0ai/scikit-learn from pyproject.toml `(category: config)`
  - Add `"qdrant-client>=1.12.0"` to `dependencies`
  - Remove `mem0 = ["mem0ai"]` from `[project.optional-dependencies]`
  - Remove CVE comments about protobuf/mem0ai
  - Check if `scikit-learn` is used elsewhere before removing (was for TF-IDF)

- [ ] Create VectorStore class in `src/gobby/memory/vectorstore.py` `(category: code)`
  - Class wrapping `qdrant_client.QdrantClient(path=...)` or `QdrantClient(url=..., api_key=...)`
  - Constructor: `path: str | None`, `url: str | None`, `api_key: str | None`, `collection_name: str = "memories"`, `embedding_dim: int = 1536`
  - Methods:
    - `async initialize()` — create collection if not exists (cosine distance)
    - `async upsert(memory_id: str, embedding: list[float], payload: dict)` — insert/update a point
    - `async search(query_embedding: list[float], limit: int, filters: dict | None) -> list[tuple[str, float]]` — similarity search returning `(memory_id, score)` pairs
    - `async delete(memory_id: str)` — remove a point
    - `async batch_upsert(items: list) -> int` — bulk insert for rebuild
    - `async rebuild(memories: list[tuple[str, str]], embed_fn) -> int` — re-embed all content and reindex
    - `async count() -> int`
    - `async close()`
  - Filter support for `project_id` via Qdrant `FieldCondition`
  - All qdrant-client calls wrapped in `asyncio.to_thread()`
  - Reference: `/tmp/mem0-inspect/mem0/vector_stores/qdrant.py` for patterns

- [ ] Add Qdrant config fields to MemoryConfig in `src/gobby/config/persistence.py` `(category: code)`
  - Add fields:
    ```python
    qdrant_path: str | None = None    # Local embedded mode (default: ~/.gobby/qdrant/)
    qdrant_url: str | None = None     # Remote mode (e.g., "https://xyz.qdrant.io:6333")
    qdrant_api_key: str | None = None # Remote API key (supports ${ENV_VAR} expansion)
    ```
  - Logic: if `qdrant_url` is set → remote mode; else → embedded at `qdrant_path`
  - Validator: `qdrant_url` and `qdrant_path` are mutually exclusive
  - Remove: `search_backend`, `embedding_weight`, `tfidf_weight`, `max_index_memories`
  - Remove: `importance_threshold`, `decay_enabled`, `decay_rate`, `decay_floor`
  - Keep: `embedding_model`, `enabled`, `backend`, `auto_crossref`, `crossref_*`
  - Keep: `neo4j_url`, `neo4j_auth`, `neo4j_database` (already support remote Neo4j)
  - Keep (removed in Phase 3): `mem0_url`, `mem0_api_key`, `mem0_timeout`, `mem0_sync_interval`, `mem0_sync_max_backoff`

- [ ] Remove importance from memory extraction prompt and extractor `(category: code)`
  - `src/gobby/install/shared/prompts/memory/extract.md`:
    - Remove `importance` field from output JSON format
    - Remove importance scoring guidelines (0.8/0.9/1.0 scale, 5-minute rule)
    - Keep HIGH-VALUE vs LOW-VALUE quality guidelines (these do the real filtering work)
    - Remove `min_importance` from required/optional variables
  - `src/gobby/memory/extractor.py`:
    - Remove importance parsing/validation from extracted memories
    - Remove `min_importance` parameter from `_filter_and_dedupe()`
  - `src/gobby/storage/memories.py`:
    - Keep `importance` column (harmless, avoids breaking migration) but make it optional/ignored
    - Remove importance from `create_memory` parameter list in manager.py

- [ ] Rename MemoryManager methods to match MCP tools `(category: refactor)`
  - `remember()` → `create_memory()`
  - `recall()` → `search_memories()`
  - `recall_as_context()` → `search_memories_as_context()`
  - `forget()` → `delete_memory()`
  - `update_memory()` already matches

- [ ] Update all 7 callers of renamed methods `(depends: Rename MemoryManager methods)` `(category: refactor)`
  - `src/gobby/mcp_proxy/tools/memory.py`
  - `src/gobby/workflows/context_actions.py`
  - `src/gobby/cli/memory.py`
  - `src/gobby/workflows/memory_actions.py`
  - `src/gobby/servers/routes/memory.py`
  - `src/gobby/memory/extractor.py`
  - `src/gobby/memory/protocol.py` (interface definitions)

- [ ] Rewrite MemoryManager to use VectorStore `(depends: Create VectorStore class, Rename MemoryManager methods)` `(category: code)`
  - Remove: `SearchService`, `SearchCoordinator`, `EmbeddingService`, `MemoryEmbeddingManager`, `Mem0Client`, `Mem0Service` imports and attributes
  - Add: `VectorStore` attribute, `_embed_fn` callable
  - `create_memory(content, ...)`:
    1. Store in PostgreSQL via `LocalMemoryManager.create_memory()`
    2. Generate embedding via `generate_embedding(content, model)` (keep `src/gobby/search/embeddings.py`)
    3. Upsert into Qdrant with `payload={project_id, tags}`
    4. Create crossrefs if enabled
  - `search_memories(query, ...)`:
    1. Generate query embedding
    2. `vector_store.search(embedding, limit, filters={project_id})`
    3. Resolve memory IDs to `Memory` objects via `LocalMemoryManager`
    4. Apply source boost: `source_type="user"` memories get `score * 1.2`
    5. Filter by tags/type if specified
    6. Return re-ranked results
  - `delete_memory(memory_id)`:
    1. Delete from PostgreSQL
    2. Delete from Qdrant
  - `update_memory(memory_id, content, ...)`:
    1. Update in PostgreSQL
    2. Re-embed, upsert into Qdrant

- [ ] Initialize VectorStore in runner.py and remove Mem0SyncProcessor `(depends: Rewrite MemoryManager)` `(category: code)`
  - Initialize `VectorStore` before `MemoryManager`:
    ```python
    qdrant_path = config.memory.qdrant_path or str(Path("~/.gobby/qdrant").expanduser())
    vector_store = VectorStore(path=qdrant_path)
    await vector_store.initialize()
    ```
  - Pass to `MemoryManager`
  - On startup: if Qdrant empty but PostgreSQL has memories, trigger `rebuild()`
  - On shutdown: `await vector_store.close()`
  - Remove: `Mem0SyncProcessor` import, `self.mem0_sync` attribute, start/stop logic

- [ ] Add migration to drop memory_embeddings table in `src/gobby/storage/migrations.py` `(category: code)`
  - Keep `mem0_id` column on `memories` for now (harmless, removed in Phase 3)
  - Keep `importance` column on `memories` (harmless, ignored)

- [ ] Update maintenance.py `(depends: Rewrite MemoryManager)` `(category: code)`
  - Remove: mem0 sync stats, `decay_memories()` function
  - Add: Qdrant stats (vector count)

- [ ] Delete legacy search files `(depends: Rewrite MemoryManager)` `(category: refactor)`
  - `src/gobby/memory/search/` — entire directory (coordinator.py, __init__.py, text.py)
  - `src/gobby/memory/components/search.py` — SearchService wrapper
  - `src/gobby/search/unified.py` — UnifiedSearcher
  - `src/gobby/search/tfidf.py` — TFIDFSearcher
  - `src/gobby/search/protocol.py` — SearchBackend protocol
  - `src/gobby/search/models.py` — SearchConfig
  - `src/gobby/search/backends/` — embedding backend
  - `src/gobby/storage/memory_embeddings.py` — MemoryEmbeddingManager (PostgreSQL BLOB storage)
  - `src/gobby/memory/services/embeddings.py` — old EmbeddingService
  - Note: Keep `src/gobby/search/embeddings.py` (the `generate_embedding()` function itself)

## Phase 2: LLM-Based Fact Extraction + Dedup

**Goal**: When an LLM is available, `create_memory` extracts atomic facts and intelligently deduplicates against existing memories via fire-and-forget background tasks. Without LLM, falls back to simple embed-and-store.

**Tasks:**
- [ ] Add generate_json() to LLM providers `(category: code)`
  - `src/gobby/llm/base.py` — add abstract method:
    ```python
    async def generate_json(
        self, prompt: str, system_prompt: str | None = None, model: str | None = None,
    ) -> dict[str, Any]:
    ```
  - `src/gobby/llm/litellm.py` — implement `generate_json()`:
    - Same as `generate_text()` but passes `response_format={"type": "json_object"}`
    - Parses response as JSON dict, returns it

- [ ] Create fact extraction prompt template at `src/gobby/install/shared/prompts/memory/fact_extraction.md` `(category: code)`
  - Apache 2.0 attribution in YAML frontmatter: `attribution: "Derived from mem0 (https://github.com/mem0ai/mem0)"`
  - Instructs LLM to extract atomic facts as JSON `{"facts": [...]}`
  - Jinja2 template with `{{ content }}` variable
  - Users can override at `~/.gobby/prompts/memory/fact_extraction.md` or `.gobby/prompts/memory/fact_extraction.md`

- [ ] Create dedup decision prompt template at `src/gobby/install/shared/prompts/memory/dedup_decision.md` `(category: code)`
  - Apache 2.0 attribution in YAML frontmatter
  - Given existing memories + new facts, output JSON with `{"memory": [{"event": "ADD|UPDATE|DELETE|NOOP", "text": "...", "id": "..."}]}`
  - Jinja2 template with `{{ new_facts }}` and `{{ existing_memories }}` variables

- [ ] Add feature configs for memory LLM calls in `src/gobby/config/features.py` `(category: config)`
  - `memory_fact_extraction`: provider=claude, model=claude-haiku-4-5 (cheap/fast)
  - `memory_dedup_decision`: provider=claude, model=claude-haiku-4-5
  - `memory_entity_extraction`: provider=claude, model=claude-haiku-4-5
  - All configurable per the existing feature config pattern

- [ ] Create DedupService in `src/gobby/memory/services/dedup.py` `(depends: Phase 1, Add generate_json(), Create fact extraction prompt, Create dedup decision prompt)` `(category: code)`
  - Class `DedupService`
  - Constructor: `llm_provider`, `vector_store: VectorStore`, `storage: LocalMemoryManager`, `embed_fn`, `prompt_loader: PromptLoader`
  - Core method:
    ```python
    async def process(
        self, content: str, project_id: str | None,
        memory_type: str, tags: list[str] | None,
        source_type: str, source_session_id: str | None,
    ) -> DedupResult:
    ```
  - Pipeline:
    1. `_extract_facts(content) -> list[str]` — render fact_extraction.md prompt, LLM JSON call
    2. For each fact: embed, `vector_store.search()` for top-5 similar
    3. `_decide_actions(new_facts, existing_memories) -> list[Action]` — render dedup_decision.md prompt, LLM JSON call
    4. Execute: create new / update existing / delete obsolete via storage + vector_store
  - Returns `DedupResult(added: list[Memory], updated: list[Memory], deleted: list[str])`
  - `Action = dataclass(event: Literal["ADD", "UPDATE", "DELETE", "NOOP"], text: str, memory_id: str | None)`
  - Graceful fallback: if LLM call fails, store content directly (simple mode)

- [ ] Wire DedupService into MemoryManager as fire-and-forget background task `(depends: Create DedupService)` `(category: code)`
  - Initialize `DedupService` when LLM provider is available
  - `create_memory` flow:
    1. Store in PostgreSQL + embed + upsert to Qdrant (immediate, same as Phase 1)
    2. Return the `Memory` to caller (fast)
    3. Fire background `asyncio.Task` for `dedup.process(memory, ...)`:
       - LLM extracts facts from content
       - For each fact: search Qdrant for similar existing memories
       - LLM decides ADD/UPDATE/DELETE/NOOP for each fact
       - Execute actions against PostgreSQL + Qdrant
       - Task tracked in `_background_tasks` set, auto-cleaned on completion
    4. If no LLM available: skip step 3 (simple mode, same as Phase 1)
  - Default behavior when LLM available, not opt-in

## Phase 3: Knowledge Graph + Full Cleanup

**Goal**: Build knowledge graph from memories into Neo4j. Remove all legacy mem0 code and naming.

**Tasks:**
- [ ] Create entity extraction prompt at `src/gobby/install/shared/prompts/memory/extract_entities.md` `(category: code)`
  - Apache 2.0 attribution in YAML frontmatter
  - Instructs LLM to extract entities as JSON `{"entities": [{"entity": "...", "entity_type": "..."}]}`
  - Jinja2 template with `{{ content }}` variable

- [ ] Create relationship extraction prompt at `src/gobby/install/shared/prompts/memory/extract_relations.md` `(category: code)`
  - Apache 2.0 attribution in YAML frontmatter
  - Instructs LLM to extract relationships as JSON `{"relations": [{"source": "...", "relationship": "...", "destination": "..."}]}`
  - Jinja2 template with `{{ content }}` and `{{ entities }}` variables

- [ ] Create delete relations prompt at `src/gobby/install/shared/prompts/memory/delete_relations.md` `(category: code)`
  - Apache 2.0 attribution in YAML frontmatter
  - Instructs LLM to identify outdated relationships for deletion as JSON
  - Jinja2 template with `{{ existing_relations }}` and `{{ new_relations }}` variables

- [ ] Add write convenience methods to `src/gobby/memory/neo4j_client.py` `(category: code)`
  - `async merge_node(name: str, labels: list[str], properties: dict, embedding: list[float])`
  - `async merge_relationship(source: str, target: str, rel_type: str, properties: dict)`
  - `async set_node_vector(node_name: str, embedding: list[float])` — `db.create.setNodeVectorProperty` via Cypher

- [ ] Create KnowledgeGraphService in `src/gobby/memory/services/knowledge_graph.py` `(depends: Create entity extraction prompt, Create relationship extraction prompt, Add write convenience methods)` `(category: code)`
  - Class `KnowledgeGraphService`
  - Constructor: `neo4j_client: Neo4jClient`, `llm_provider`, `embed_fn`, `prompt_loader: PromptLoader`
  - Write path — `async add_to_graph(content: str, user_id: str | None)`:
    1. LLM extracts entities with types → JSON (via extract_entities.md prompt)
    2. LLM extracts relationships between entities → JSON (via extract_relations.md prompt)
    3. For each entity: fuzzy-match in Neo4j via `vector.similarity.cosine()` on node embeddings
    4. LLM identifies outdated relationships to delete → JSON (via delete_relations.md prompt)
    5. Execute deletes via Cypher
    6. MERGE nodes with embeddings, MERGE relationships with mention counters
    7. Return `{added_entities, deleted_entities}`
  - Read path — absorbs existing `GraphService` (`src/gobby/memory/services/graph.py`):
    - `async get_entity_graph(limit: int = 500) -> dict | None`
    - `async get_entity_neighbors(name: str) -> dict | None`
    - `async search_graph(query: str, limit: int = 10) -> list[dict]` — extract entities from query, find neighbors, return context
  - All methods return `None`/empty if Neo4j is down (graceful degradation)
  - Dataclasses: `Entity(name: str, entity_type: str)`, `Relationship(source: str, target: str, relationship: str)`

- [ ] Wire KnowledgeGraphService into MemoryManager `(depends: Create KnowledgeGraphService, Phase 2)` `(category: code)`
  - Replace `GraphService` with `KnowledgeGraphService`
  - `create_memory` now fires two chained background tasks (fire-and-forget):
    1. Dedup task (Phase 2) — fact extraction + dedup against existing memories
    2. Graph task — after dedup completes (or immediately if no LLM), fire `knowledge_graph.add_to_graph(content)` if Neo4j configured
    Both tracked in `_background_tasks`, failures logged but don't fail caller
  - In `search_memories()`: optionally merge graph context with vector results

- [ ] Add search_knowledge_graph MCP tool and remove export_memory_graph tool `(depends: Create KnowledgeGraphService)` `(category: code)`
  - `src/gobby/mcp_proxy/tools/memory.py`:
    - Add `search_knowledge_graph` tool for graph-based search
    - Remove `export_memory_graph` tool (legacy vis.js export; frontend has its own Dagre/SVG graph UI)
    - `memory_stats` tool: update stats format (no more mem0_sync, add vector_count)

- [ ] Create standalone docker-compose.neo4j.yml and neo4j installer `(category: code)`
  - `src/gobby/data/docker-compose.neo4j.yml` (~20 lines):
    - Standalone Neo4j container (ports 8474:7474, 8687:7687)
    - APOC plugin, volume `gobby_neo4j_data`
  - `src/gobby/cli/installers/neo4j.py` (~120 lines):
    - `install_neo4j(gobby_home)` — docker compose up + health check + config update
    - `uninstall_neo4j(gobby_home, remove_volumes)` — teardown
    - Follows same pattern as existing `mem0.py` installer

- [ ] Replace --mem0 with --neo4j in CLI `(depends: Create standalone docker-compose.neo4j.yml)` `(category: code)`
  - `src/gobby/cli/install.py`: replace `--mem0` with `--neo4j`
  - `src/gobby/cli/services.py`: replace `is_mem0_installed` / `get_mem0_status` with `is_neo4j_installed` / `get_neo4j_status`

- [ ] Remove mem0 config fields from MemoryConfig `(category: config)`
  - `src/gobby/config/persistence.py`: remove `mem0_url`, `mem0_api_key`, `mem0_timeout`, `mem0_sync_interval`, `mem0_sync_max_backoff`

- [ ] Add migration to drop mem0_id column from memories table `(category: code)`
  - `src/gobby/storage/migrations.py`: migration to drop `mem0_id` column (or leave nullable, no references)

- [ ] Delete all legacy mem0 and viz files `(depends: Wire KnowledgeGraphService, Replace --mem0 with --neo4j)` `(category: refactor)`
  - `src/gobby/memory/mem0_client.py`
  - `src/gobby/memory/mem0_sync.py`
  - `src/gobby/memory/services/mem0_sync.py`
  - `src/gobby/memory/services/graph.py` (replaced by `knowledge_graph.py`)
  - `src/gobby/memory/viz.py` (legacy vis.js HTML export — frontend uses Dagre/SVG)
  - `src/gobby/data/docker-compose.mem0.yml`
  - `src/gobby/data/Dockerfile.mem0`
  - `src/gobby/cli/installers/mem0.py`
  - `tests/memory/test_viz.py`
  - `tests/memory/test_mem0_client.py`
  - `tests/memory/test_mem0_sync.py`
  - `tests/cli/test_install_mem0.py`
  - `tests/cli/test_daemon_mem0.py`

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Search engine | Qdrant embedded only (no TF-IDF) | One engine, zero config, HNSW-indexed vector search |
| Storage model | PostgreSQL (source of truth) + Qdrant (search index) | PostgreSQL is authoritative, Qdrant rebuildable; consistent with rest of gobby |
| Async strategy | Fire-and-forget background tasks | `create_memory` returns fast; LLM dedup + graph build run as tracked `asyncio.Task`s |
| Importance scoring | Removed | LLM time estimates are arbitrary; vector similarity handles relevance, LLM DELETE handles staleness |
| Ranking boost | User-created memories boosted in search | `source_type="user"` gets similarity score × 1.2 — binary signal, not arbitrary scores |
| Decay | Removed | LLM dedup DELETE replaces time-based decay for handling outdated facts |
| LLM integration | `generate_json()` on provider, no tool-calling | Simpler, works with all providers |
| LLM model | Haiku by default (configurable via feature config) | Cheap/fast for high-frequency background tasks |
| Naming | No "mem0" anywhere in code | Attribution only in prompt YAML frontmatter |
| Prompts | Markdown templates via PromptLoader | User-overridable at project/global/bundled tiers |
| Neo4j driver | Keep existing `Neo4jClient` HTTP API | Avoids driver dependency bloat |

## Dependencies

| Add | Remove |
|-----|--------|
| `qdrant-client>=1.12.0` | `mem0ai` (optional dep) |
| | `scikit-learn` (if only used for TF-IDF) |

## Edge Cases

- **No LLM configured**: Simple mode — embed content, store in PostgreSQL + Qdrant, no fact extraction. Search still works via Qdrant.
- **No Neo4j configured**: Graph features silently disabled. Memory storage and search work fine.
- **Qdrant data lost**: `VectorStore.rebuild()` re-embeds all content from `memories` table. Triggered automatically on startup if collection is empty but PostgreSQL has memories.
- **Embedding API down**: Store in PostgreSQL, skip Qdrant upsert. Memory exists but won't appear in vector search until re-embedded.

## Verification

After each phase:
1. `uv run pytest tests/memory/ -v` — all memory tests pass
2. `uv run ruff check src/gobby/memory/` — no lint errors
3. `uv run mypy src/gobby/memory/` — no type errors

End-to-end after all phases:
1. `uv run gobby start --verbose` — daemon starts, Qdrant initializes at `~/.gobby/qdrant/`
2. `create_memory` via MCP — stored in PostgreSQL + Qdrant
3. `search_memories` via MCP — returns results from Qdrant
4. Create duplicate-ish memory — LLM detects similarity, updates instead of creating
5. `gobby install --neo4j` — standalone Neo4j container starts
6. Create memory with Neo4j configured — entities appear in graph
7. `search_knowledge_graph` — returns graph-based context

## Critical Files

| File | Role |
|------|------|
| `src/gobby/memory/manager.py` | Central orchestrator — rewritten in every phase |
| `src/gobby/memory/vectorstore.py` | Qdrant wrapper — the new search engine |
| `src/gobby/install/shared/prompts/memory/fact_extraction.md` | Fact extraction prompt (from mem0, Apache 2.0) |
| `src/gobby/install/shared/prompts/memory/dedup_decision.md` | Dedup decision prompt (from mem0, Apache 2.0) |
| `src/gobby/install/shared/prompts/memory/extract_entities.md` | Entity extraction prompt (from mem0, Apache 2.0) |
| `src/gobby/install/shared/prompts/memory/extract_relations.md` | Relationship extraction prompt (from mem0, Apache 2.0) |
| `src/gobby/memory/services/dedup.py` | LLM-based smart dedup pipeline |
| `src/gobby/memory/services/knowledge_graph.py` | Neo4j graph builder (replaces GraphService) |
| `src/gobby/memory/neo4j_client.py` | Extend with write methods |
| `src/gobby/config/persistence.py` | Simplified MemoryConfig |
| `src/gobby/runner.py` | Daemon startup — Qdrant init, remove mem0 sync |
| `src/gobby/llm/litellm.py` | Add `generate_json()` |
| `src/gobby/search/embeddings.py` | Keep — `generate_embedding()` still needed |
| `src/gobby/prompts/loader.py` | PromptLoader — loads all new prompt templates |
| `src/gobby/config/features.py` | Feature configs for memory LLM calls |

## Post-Implementation Notes

All three phases are complete. A post-implementation cleanup addressed residual issues:

- **`src/gobby/search/`** — Retained. While TF-IDF memory search was removed (replaced by Qdrant), `src/gobby/search/` is still used by task search (`TFIDFSearcher` in task matching) and skill search. `scikit-learn` remains as a dependency for these consumers.
- **`CrossrefService` deleted** — `src/gobby/memory/services/crossref.py` was dead code. `MemoryManager._create_crossrefs()` replaced it with VectorStore-based crossrefs in Phase 1.
- **`importance` field removed** — The plan specified "no importance scoring" but the field persisted across all layers. Migration 105 drops the `importance` column from `memories`. Removed from: `MemoryRecord`, `MemoryQuery`, `MemoryBackendProtocol`, `Memory` dataclass, `MemoryManager`, MCP tools, CLI, HTTP routes, workflow actions, sync/backup, and all tests.
- **`scikit-learn`** — Comment in `pyproject.toml` updated to reflect actual usage (task search and skill search, not memory search).
