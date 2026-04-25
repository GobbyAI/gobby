# Migrate Knowledge Graph Backend from Neo4j to FalkorDB

## Overview

Replace Neo4j (HTTP Query API v2 + JVM + APOC) with FalkorDB (Redis RESP + native vector indexes + single binary) across the Python daemon (`gobby`), the Rust read client (`gobby-cli`), and the web UI (browser components + Ink onboarding wizard). Targets the 0.4.0 ship.

The architectural fact that shapes this plan: **the Rust crate is read-only** and **all graph writes happen in the Python daemon** — both for the memory knowledge graph (`KnowledgeGraphService` + `Neo4jClient`) and the code knowledge graph (`code_index/CodeGraph`). Dialect translation is concentrated in Python; Rust just needs a transport swap. Both repos must land in lockstep because the Rust crate reads the graph the daemon writes.

## Constraints

- **No compatibility shim, no dual-backend abstraction.** Full rip-and-replace; pick FalkorDB and commit.
- **Both repos land in one coordinated cut.** The admin payload key rename, frontend hook, setup wizard CLI flag, and Rust config-store keys must all flip together. CI goes red the moment the backend renames if the frontend lags.
- **Docker-only for 0.4.0.** A native local-install path was scoped, then dropped after verifying that FalkorDB ships no Homebrew formula and only raw `.so` Redis modules via GitHub releases. Native-mode support is deferred to a follow-up release. The current Neo4j experience is also Docker-only, so this is parity, not a regression.
- **Onboarding wizard is in scope for 0.4.0.** The Ink-based setup wizard at `web/src/setup/` must be functional after this migration — manual end-to-end verification is required before merge.
- **Data is wiped, not migrated.** Both graphs are derived from data already stored elsewhere (memories in SQLite + Qdrant, code graph from SQLite code index). Existing rebuild commands are idempotent. No export/import script.
- **Drop the vestigial 1536 dim default** in `ensure_vector_index`. The replacement requires `dimension` as a positional kwarg sourced from `EmbeddingsConfig.dim`. Closes a footgun.

## Phase 1: Python — FalkorClient and Config

**Goal**: Stand up `FalkorClient` and `FalkorConfig` so subsequent phases can wire them in.

### 1.1 Replace Neo4jConfig with FalkorConfig and add falkordb dep [category: config]

Target: `src/gobby/config/persistence.py`, `pyproject.toml`

Replace the `Neo4jConfig` class (lines 64-108 of `src/gobby/config/persistence.py`) with `FalkorConfig`:

```python
class FalkorConfig(BaseModel):
    """FalkorDB graph database connection configuration."""

    model_config = {"extra": "ignore"}

    host: str = Field(
        default="127.0.0.1",
        description="FalkorDB host. Docker default: 127.0.0.1 (port-mapped).",
    )
    port: int = Field(
        default=16379,
        description=(
            "FalkorDB port. Docker host-side: 16379 (remapped from container 6379 to "
            "avoid system Redis conflicts). 0.4.0 supports Docker only — see 3.1's mode decision."
        ),
    )
    requirepass: str | None = Field(
        default=None,
        description=(
            "FalkorDB password (Redis AUTH; named `requirepass` to match the Redis config "
            "directive AND to avoid secret-name collision with `auth.password`). "
            "`config_key_to_secret_name` derives the secret-store name from the LAST segment "
            "of the dotted config key — `databases.falkordb.requirepass` resolves to secret "
            "name `requirepass`, which is unique across the existing config namespace. Naming "
            "this field `password` would resolve to secret name `password`, which collides "
            "with the existing `auth.password` web-login secret. Must be provided when "
            "FalkorDB is enabled. Supports ${ENV_VAR} pattern for env var expansion at load time."
        ),
    )
    graph_name: str = Field(
        default="gobby_kg",
        description=(
            "FalkorDB graph key. Memory KG uses 'gobby_kg', code graph uses 'gobby_code'. "
            "Set per consumer; this is the default for the memory KG client."
        ),
    )
    graph_search: bool = Field(
        default=True,
        description="Enable graph-augmented search (entity vector search merged via RRF)",
    )
    graph_min_score: float = Field(
        default=0.5,
        description="Minimum entity vector similarity score for graph search (0.0-1.0)",
    )
    rrf_k: int = Field(
        default=60,
        description="RRF constant for merging Qdrant and graph results (higher = more uniform weighting)",
    )

    @field_validator("graph_min_score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("Value must be between 0.0 and 1.0")
        return v
```

Update `DatabasesConfig` (lines 111-123):

```python
class DatabasesConfig(BaseModel):
    model_config = {"extra": "ignore"}

    qdrant: QdrantConfig = Field(default_factory=QdrantConfig, description="Qdrant vector database connection")
    falkordb: FalkorConfig = Field(default_factory=FalkorConfig, description="FalkorDB graph database connection")
```

In `pyproject.toml`, add to `[project.dependencies]`:

```toml
"falkordb>=1.1.0",
```

Delete every reference to `Neo4jConfig` in this file. Do not leave a `neo4j` field on `DatabasesConfig` — full rip.

### 1.2 Implement FalkorClient mirroring Neo4jClient surface [category: code] (depends: 1.1)

Target: `src/gobby/memory/falkor_client.py` (new file), `src/gobby/memory/__init__.py` (export update)

Create `src/gobby/memory/falkor_client.py` exposing the same public surface as `src/gobby/memory/neo4j_client.py` so `KnowledgeGraphService` and `CodeGraph` only need a constructor swap, not a behavioral rewrite. Use the official `falkordb` Python package (async API, built on `redis.asyncio`).

Required exception classes:

```python
class FalkorConnectionError(Exception):
    """Raised when unable to connect to FalkorDB."""

class FalkorQueryError(Exception):
    """Raised when a Cypher query returns an error."""

    def __init__(self, message: str, response_body: Any = None):
        super().__init__(message)
        self.response_body = response_body
```

Required client class:

```python
class FalkorClient:
    """Async FalkorDB client for the knowledge graph."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str | None = None,
        graph_name: str = "gobby_kg",
        timeout: float = 15.0,
    ) -> None:
        from falkordb.asyncio import FalkorDB
        self._host = host
        self._port = port
        self._graph_name = graph_name
        self._db = FalkorDB(host=host, port=port, password=password, socket_timeout=timeout)
        self._graph = self._db.select_graph(graph_name)
```

All methods must keep the **same names and parameter shapes** as `Neo4jClient`:

- `async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]` — execute a Cypher statement; return rows as flat dicts (use `_parse_falkor_result` to map FalkorDB's `(header, records, statistics)` envelope into the existing dict shape)
- `async def close(self) -> None` — close underlying connection
- `base_url` property returning `f"redis://{host}:{port}"` for status display compatibility
- `async def ensure_memory_graph_schema(self) -> None` — see Phase 2.1 for the FalkorDB-dialect Cypher
- `async def ensure_vector_index(self, dimension: int, similarity: str = "cosine", index_name: str = "entity_embedding_index") -> None` — **`dimension` is required, not defaulted** (closes the 1536-default footgun)
- `async def ensure_supporting_index(self, label: str, prop: str) -> None` — issues `CREATE INDEX FOR (n:Label) ON (n.prop)`, swallows "already indexed" errors. **Must be called before every `ensure_unique_constraint(label, prop)` call** — FalkorDB requires a supporting exact-match index to back any unique constraint.
- `async def ensure_unique_constraint(self, label: str, prop: str) -> None` — sends `GRAPH.CONSTRAINT CREATE <graph_name> UNIQUE NODE Label PROPERTIES 1 prop` as a Redis command (out-of-band, not via `query()`). Then **polls `db.constraints()` until the constraint reaches `OPERATIONAL` status**, with a 30s timeout. Raise `FalkorQueryError` on `FAILED` status (signals pre-existing data violates the constraint).
- `async def merge_node`, `async def merge_relationship`, `async def set_node_vector` — same signatures as Neo4jClient counterparts
- `async def get_entity_graph`, `async def get_entity_neighbors`, `async def vector_search`, `async def execute_read`, `async def execute_write`, `async def ping` — same signatures
- `_validate_cypher_identifier(value: str, kind: str)` — keep the existing identifier-validation helper for safe Cypher interpolation

**Constraint readiness gates writes.** `ensure_memory_graph_schema` (Phase 2.1) and the equivalent in `code_index/graph.py` (Phase 2.2) MUST complete — including all constraint polling — before any `merge_node`/`merge_relationship` calls fire. FalkorDB constraint creation is asynchronous; firing writes against `PENDING` or `UNDER CONSTRUCTION` constraints yields silent inconsistency. Treat schema readiness as a hard startup gate.

Connection error mapping: catch `redis.exceptions.ConnectionError`, `redis.exceptions.TimeoutError` and raise `FalkorConnectionError`; catch `falkordb.exceptions.QueryError` (or whatever the package raises) and raise `FalkorQueryError`.

The `query()` parser must collapse FalkorDB's response into the same `list[dict[str, Any]]` shape that callers expect — the dict keys are the column aliases from the Cypher RETURN clause. This keeps `KnowledgeGraphService` and `CodeGraph` consumers oblivious to the transport change.

Do **not** leave `src/gobby/memory/neo4j_client.py` in place; that file is removed in 1.3.

### 1.3 Delete neo4j_client.py and update memory module exports [category: refactor] (depends: 1.2, Phase 2)

Target: `src/gobby/memory/neo4j_client.py` (delete), `src/gobby/memory/__init__.py`

Delete `src/gobby/memory/neo4j_client.py` outright. Update `src/gobby/memory/__init__.py` to remove any `from .neo4j_client import …` re-exports and add `from .falkor_client import FalkorClient, FalkorConnectionError, FalkorQueryError`.

This task depends on Phase 2 because `KnowledgeGraphService` and `CodeGraph` still import from `neo4j_client` until that phase finishes. Run last in this ordering.

## Phase 2: Python — Cypher Dialect Translation and Wire-Up

**Goal**: Translate every Cypher statement to FalkorDB dialect and wire the new client into `KnowledgeGraphService`, `MemoryManager`, `CodeGraph`, and `CodeIndexContext`.

### 2.1 Translate KnowledgeGraphService Cypher and wire MemoryManager [category: code] (depends: 1.2)

Target: `src/gobby/memory/services/knowledge_graph.py`, `src/gobby/memory/manager.py`, `src/gobby/runner_init.py` (the actual call site that constructs `MemoryManager` with the Neo4j-shaped kwargs)

Apply these dialect translations to every Cypher string in `src/gobby/memory/services/knowledge_graph.py` and `src/gobby/memory/falkor_client.py`'s schema/vector helpers:

| Neo4j construct | FalkorDB equivalent |
|---|---|
| `CREATE CONSTRAINT name IF NOT EXISTS FOR (n:Label) REQUIRE n.prop IS UNIQUE` | **Two-step required:** (1) `ensure_supporting_index(label, prop)` first — FalkorDB constraints require a backing exact-match index. (2) `GRAPH.CONSTRAINT CREATE <graph_name> UNIQUE NODE Label PROPERTIES 1 prop` as a Redis command (not via `query()`). (3) **Poll `db.constraints()` until status is `OPERATIONAL`** with a 30s timeout. Status sequence: `PENDING` → `UNDER CONSTRUCTION` → `OPERATIONAL` (or `FAILED` if pre-existing data violates uniqueness). Swallow "constraint already exists" only when the polled status is `OPERATIONAL`; raise on `FAILED`. |
| `CREATE INDEX name IF NOT EXISTS FOR (n:Label) ON (n.a, n.b)` | `CREATE INDEX FOR (n:Label) ON (n.a, n.b)` (no `IF NOT EXISTS`; catch "already indexed" errors) |
| `CREATE VECTOR INDEX name IF NOT EXISTS FOR (n:Label) ON (n.embedding) OPTIONS {indexConfig: {…}}` | `CREATE VECTOR INDEX FOR (n:Label) ON (n.embedding) OPTIONS {dimension: $dim, similarityFunction: 'cosine'}` |
| `CALL db.create.setNodeVectorProperty(n, 'embedding', $emb)` | `SET n.embedding = vecf32($emb)` (drop the procedure call entirely) |
| `CALL db.index.vector.queryNodes('idx_name', $k, $emb) YIELD node, score` | `CALL db.idx.vector.queryNodes('Label', 'embedding', $k, vecf32($emb)) YIELD node, score` (signature: label + property, not index name) |
| `datetime()` | `timestamp()` (returns Unix epoch ms; downstream code that read these as ISO strings must be updated to handle integers) |
| `apoc.*` procedures | None used in current code — verified clean |

Specific Cypher rewrites in `src/gobby/memory/services/knowledge_graph.py`:

- `_delete_outdated_relations` (≈line 494) — pure MATCH/DELETE, no dialect change beyond `datetime()` if present
- `_fetch_existing_relations` (≈line 510) — no change
- `_link_entities_to_memory` (≈line 528) — change `datetime()` → `timestamp()` in MERGE ... ON CREATE/MATCH
- `remove_memory_from_graph`, `remove_memories_from_graph`, `get_all_memory_node_ids`, `remove_orphaned_entities`, `clear_graph`, `clear_project_graph` (≈lines 544-707) — pure DETACH DELETE / counts; no dialect change
- `_link_entities_to_code` (≈line 769) — change `datetime()` → `timestamp()`
- `find_related_memory_ids` (≈line 891) — variable-length path traversal; FalkorDB supports `[*1..N]` up to depth 5, current clamp is already ≤3
- `search_graph` substring fallback (≈line 980) — `toLower` and `CONTAINS` both supported; no change

**`MemoryManager` constructor surface — verified live, not assumed:**

`MemoryManager.__init__` (in `src/gobby/memory/manager.py`) currently takes flat Neo4j-shaped kwargs:

```python
def __init__(
    self,
    db, config, llm_service=None, vector_store=None, embed_fn=None,
    *,
    neo4j_url: str | None = None,
    neo4j_auth: str | None = None,
    neo4j_database: str = "neo4j",
    neo4j_graph_search: bool = True,
    neo4j_graph_min_score: float = 0.5,
    neo4j_rrf_k: int = 60,
    embedding_dim: int = 768,
    collection_prefix: str = "code_symbols_",
):
```

These kwargs are passed in by `src/gobby/runner_init.py` (the daemon startup wiring) which reads them off the loaded `DaemonConfig`. There is no top-level `config` object inside `MemoryManager` that carries `databases.falkordb.*`. Both files must move together — renaming the kwargs without updating the caller leaves a TypeError.

**Required changes:**

1. **`MemoryManager.__init__` (manager.py):** rename the `neo4j_*` kwargs end-to-end:
   - `neo4j_url` → `falkordb_host` (and add a sibling `falkordb_port: int`); split URL into host/port at the call site (no URL parsing inside the manager)
   - `neo4j_auth` → `falkordb_password` (the resolved value from `FalkorConfig.requirepass` at the call site)
   - `neo4j_database` → `falkordb_graph_name` (default `"gobby_kg"` to match 1.2)
   - `neo4j_graph_search` → `falkordb_graph_search`
   - `neo4j_graph_min_score` → `falkordb_graph_min_score`
   - `neo4j_rrf_k` → `falkordb_rrf_k`
   - Update internal attributes (`self._neo4j_graph_search` → `self._falkordb_graph_search`, etc.) and every internal use site
2. **Inside `MemoryManager`:** swap the `Neo4jClient(...)` construction for `FalkorClient(host=falkordb_host, port=falkordb_port, password=falkordb_password, graph_name=falkordb_graph_name)`. Update `self._neo4j_client` attribute → `self._falkor_client`. Update the `KnowledgeGraphService(neo4j_client=...)` ctor call to pass `falkor_client=...` (matching the renamed param on the service).
3. **`runner_init.py`:** the call site that constructs `MemoryManager(...)` reads the legacy Neo4j config off `db_cfg`. Update it to read `db_cfg.databases.falkordb.{host, port, requirepass, graph_name, graph_search, graph_min_score, rrf_k}` and pass under the new kwarg names. **Note `requirepass`, not `password`** — the FalkorConfig field name from 1.1.

In `KnowledgeGraphService.__init__`, rename the `neo4j_client` parameter to `falkor_client` and the `self._neo4j` attribute to `self._falkor`. Update every `self._neo4j.X(…)` call site within the service.

The `add_to_graph` method's dynamic multi-label MERGE (≈line 184): `MERGE (n:Capitalize(entity_type):_Entity {entity_key: $key})` — FalkorDB supports multi-label MERGE but the parser is strict about whitespace. Smoke-test against realistic entity-type inputs.

### 2.2 Translate CodeGraph Cypher and wire CodeGraph construction at runner_init [category: code] (depends: 1.2)

Target: `src/gobby/code_index/graph.py`, `src/gobby/runner_init.py` (where `CodeGraph(neo4j_client=...)` is actually constructed and injected into `CodeIndexContext`), `src/gobby/code_index/context.py` (only for the `CodeGraph | None` typing reference — the context itself does not instantiate the client)

Apply the same dialect translations from 2.1 to every Cypher string in `src/gobby/code_index/graph.py`. The methods touching Cypher:

- `sync_file` (≈line 95) — bulk MERGE of nodes/relationships from a file's parsed AST; this is the primary write path triggered by gobby-cli's `/api/code-index/invalidate` POST
- `add_relationships` (≈line 258)
- `_ensure_node` (≈line 628) — node upsert helper
- `_cleanup_orphans` (≈line 64)
- `clear_project` (≈line 859), `delete_file` (≈line 874)
- `find_callers`, `find_usages`, `get_imports`, `get_import_chain`, `find_blast_radius`, `get_file_graph`, `get_file_symbols`, `get_symbol_neighbors`, `get_blast_radius_graph` — read methods that the daemon uses for HTTP route responses (not the Rust client)

Schema setup in CodeGraph likely creates indexes/constraints on `CodeSymbol.id`, `CodeFile.path`, etc. — apply the FalkorDB DDL dialect from the 2.1 table.

**Construction site — verified live, not assumed:** `src/gobby/code_index/context.py` does NOT construct the graph client; `CodeIndexContext.graph` is just a property returning `self._graph`. The actual `CodeGraph(neo4j_client=...)` call lives in `src/gobby/runner_init.py` (alongside the MemoryManager construction in 2.1) and the resulting instance is injected into `CodeIndexContext`. Make the runtime swap there:

1. **`src/gobby/runner_init.py`:** replace the `Neo4jClient(...)` + `CodeGraph(neo4j_client=client)` construction with `FalkorClient(host=db_cfg.databases.falkordb.host, port=db_cfg.databases.falkordb.port, password=db_cfg.databases.falkordb.requirepass, graph_name="gobby_code")` + `CodeGraph(falkor_client=client)`. Note the **different `graph_name`** — code graph (`gobby_code`) and memory KG (`gobby_kg`) are separate FalkorDB graphs in the same instance.
2. **`src/gobby/code_index/graph.py` (`CodeGraph.__init__`):** rename the `neo4j_client` constructor parameter to `falkor_client` and update internal attributes (`self._neo4j` → `self._falkor`) and every internal use site.
3. **`src/gobby/code_index/context.py`:** if the typing imports reference `Neo4jClient`, update to `FalkorClient`. Do not change construction logic — there is none in this file.

Verify `src/gobby/code_index/sync_worker.py` (the worker that consumes `/api/code-index/invalidate` POSTs from gobby-cli) only goes through `CodeGraph` — it should not hold a separate `Neo4jClient` reference. If it does, swap that too.

The blast-radius variable-length path query interpolates depth into the Cypher (depth clamped to 1-5); FalkorDB supports this pattern.

## Phase 3: Python — Installer, Bootstrap, and CLI Flags

**Goal**: Replace the Neo4j installer with a Docker-only FalkorDB installer; rename CLI flags; migrate bootstrap and config_store keys.

### 3.1 Implement FalkorDB installer (Docker-only) [category: code] (depends: 1.1)

Target: `src/gobby/cli/installers/falkor.py` (new file), modeled on `src/gobby/cli/installers/neo4j.py`

Create `src/gobby/cli/installers/falkor.py` exposing two public functions: `install_falkordb(*, password: str | None, gobby_home: Path | None = None) -> dict[str, Any]` and `uninstall_falkordb(*, gobby_home: Path | None = None, purge: bool = False) -> dict[str, Any]`. Mirror the existing Neo4j installer's overall shape (compose-yaml ensure, subprocess invocation, config update, health wait).

**Docker-only decision (in scope for 0.4.0):** FalkorDB does not ship a Homebrew formula and the GitHub releases ship raw `.so` Redis modules that require manual `redis-server` setup. A reliable native-install path requires either Homebrew tap upstream work or a custom `.so`-download installer — both are non-trivial and out of scope for this migration. 0.4.0 ships **Docker-only** for FalkorDB (parity with the current Neo4j experience, which is also Docker-only). A native local mode is a follow-up release item.

**Password resolution (mirrors `_resolve_neo4j_password` in `src/gobby/cli/installers/neo4j.py`):**

Add a `_resolve_falkordb_password(password: str | None) -> str` helper with the following precedence. Bootstrap is **not** in this chain (it is a write target, not a read source — see "Bootstrap split-brain fix" below):

1. **Explicit `password` argument** (passed in from `--falkordb-password` or the wizard's `[p]` path)
2. **Existing config_store secret** at `databases.falkordb.requirepass` (read via `SecretStore`); if present, reuse the same value so re-running `gobby install --falkordb` is idempotent and does not lock the user out of an existing data dir
3. **Generated value** — `secrets.token_urlsafe(24)` if neither of the above is set

After resolution, `install_falkordb` writes the resolved value to **both**:
1. `_update_config(...)` — encrypts the value into the `secrets` table, references it from `config_store` under `databases.falkordb.requirepass`
2. `_write_bootstrap_password(resolved, gobby_home)` — persists into `~/.gobby/bootstrap.yaml` under `falkordb_password` so `_services_start` (3.5) can read it on `gobby start` for the docker-compose env injection

Both writes must complete before health-check failure cleanup, otherwise a half-installed install loses credentials.

**Bootstrap split-brain fix (R5-F1):** the bootstrap file is a *write target* of the installer (so the daemon's `_services_start` can find a password on cold start), but is **never read** by the installer's password-resolution chain. This means `uninstall_falkordb` must clear `falkordb_password` from `~/.gobby/bootstrap.yaml` alongside the config_store entries — otherwise a subsequent install picks up a generated value (because config_store is empty), but the daemon picks up the old stale bootstrap value, and the docker-compose container comes up authenticating against the new value while `_services_start`'s env injection uses the old. They desynchronize silently.

The resolved value is returned in the installer's result dict as `password` so `_run_falkordb_install` (Phase 3.4) can echo it once on first install (`Generated FalkorDB password: <value>` — only on the generated path, never when reusing an existing one).

**Docker install steps:**

1. Check Docker is available; abort with `Docker not found. Install Docker to use FalkorDB.` (mirrors the existing Neo4j installer's Docker check verbatim)
2. **Refresh the compose file** (NOT just "ensure exists" — this catches the existing-install upgrade path): unconditionally overwrite `~/.gobby/services/docker-compose.yml` from the bundled template. The current `_ensure_unified_compose` in `src/gobby/cli/installers/qdrant.py` is a `if not dest.exists(): copy` — that helper would leave a stale Neo4j-era compose file in place on upgrade, so `docker compose --profile falkordb up` would find no `falkordb` profile and silently fail. Add a sibling helper `_refresh_unified_compose(services_dir)` that always copies, AND first stops any running profiles from the old file (`docker compose --profile neo4j down` if the old file exists, ignoring failure) so the upgrade does not orphan the Neo4j container.
3. Run `docker compose --profile falkordb up -d --remove-orphans` with `GOBBY_FALKORDB_PASSWORD=<resolved_password>` in the env. The compose template (Phase 3.2) maps that env into `REDIS_ARGS="--requirepass $GOBBY_FALKORDB_PASSWORD"` for the container — Redis auth lives on `REDIS_ARGS`, not `FALKORDB_ARGS` (the latter is reserved for FalkorDB module options like `MAX_QUEUED_QUERIES`).
4. Health check: `docker compose exec falkordb redis-cli -a "$GOBBY_FALKORDB_PASSWORD" PING` must return `PONG` (poll up to 60s; treat `NOAUTH`/`WRONGPASS` as a hard failure, not a transient health miss)
5. `_update_config(host="127.0.0.1", port=16379, password=<resolved_password>)` — port 16379 is the host-side mapping from the compose template (avoids collision with system Redis on 6379)

**FalkorDB Browser:** the official `falkordb/falkordb` image bundles the browser on container port 3000. The compose template (3.2) maps it to host port 13000. The success message surfaces `http://localhost:13000` as the browser URL.

`_update_config` writes the persisted state:

```python
def _update_config(*, host: str, port: int, password: str) -> None:
    db = LocalDatabase(...)
    store = ConfigStore(db)
    store.set("databases.falkordb.host", host, source="install")
    store.set("databases.falkordb.port", port, source="install")
    secret_store = SecretStore(db)
    store.set_secret("databases.falkordb.requirepass", password, secret_store, source="install")
```

The `databases.falkordb.mode` key is **dropped** — there is only one mode (Docker), so no routing needed. `is_falkordb_installed` (3.3) keys off the presence of the host/port keys instead.

**Uninstall** (`uninstall_falkordb`):

1. Run `docker compose --profile falkordb down` (no `-v` by default; `-v` only when the operator passes `--purge` to drop the data volume)
2. Clear ONLY the connection/auth keys — preserve the user-tuned behavior keys (`graph_search`, `graph_min_score`, `rrf_k`, `graph_name`) so they survive a reinstall:
   - `ConfigStore.clear_secret("databases.falkordb.requirepass", secret_store)` for the secret
   - Delete only `host`, `port` (and any other strictly connection-level keys): `DELETE FROM config_store WHERE key IN ('databases.falkordb.host', 'databases.falkordb.port')`
   - Do NOT issue a blanket `DELETE WHERE key LIKE 'databases.falkordb.%'` — that would silently clobber user-tuned behavior keys (matches the migration policy in 3.6)
3. **Clear `falkordb_password` from `~/.gobby/bootstrap.yaml`** — load the YAML, pop the key if present, write back (preserving every other key). This closes the bootstrap split-brain identified in R5-F1.

### 3.2 Replace neo4j service block in docker-compose.services.yml [category: config] (depends: 1.1)

Target: `src/gobby/data/docker-compose.services.yml`

Remove the `neo4j` service block (lines 6-26) and the `gobby_neo4j_data` / `gobby_neo4j_logs` volumes. Add:

```yaml
falkordb:
  image: falkordb/falkordb:latest
  ports:
    - "16379:6379"
    - "13000:3000"
  environment:
    # Redis AUTH — REDIS_ARGS is the documented entry point for redis-server flags.
    # FALKORDB_ARGS is reserved for module options (e.g. MAX_QUEUED_QUERIES); do not put auth there.
    - REDIS_ARGS=--requirepass ${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor}
    # Pass-through so the healthcheck below can read the same value.
    - GOBBY_FALKORDB_PASSWORD=${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor}
  volumes:
    - gobby_falkordb_data:/data
  healthcheck:
    test: ["CMD-SHELL", "redis-cli -a $$GOBBY_FALKORDB_PASSWORD PING | grep -q PONG"]
    interval: 10s
    timeout: 5s
    retries: 5
  restart: unless-stopped
  profiles: [falkordb, all]
```

In the `volumes:` section at the bottom of the file, remove `gobby_neo4j_data` and `gobby_neo4j_logs`, and add `gobby_falkordb_data:`.

The `neo4j` profile name is replaced by `falkordb`. The `all` profile remains so `docker compose --profile all up -d` brings up everything.

### 3.3 Replace services.py status helpers with FalkorDB equivalents [category: code] (depends: 1.1)

Target: `src/gobby/cli/services.py`

Replace `is_neo4j_installed`, `is_neo4j_healthy`, and `get_neo4j_status` (lines 95-143) with FalkorDB equivalents:

```python
def is_falkordb_installed(*, db: LocalDatabase | None = None) -> bool:
    """True if the installer has recorded FalkorDB host/port in config_store.

    Source of truth: presence of `databases.falkordb.host` AND `databases.falkordb.port`
    keys in config_store. The installer (3.1) writes both during _update_config; the
    uninstaller (3.1) clears them via DELETE WHERE LIKE. No filesystem marker — config_store
    is the single source of truth, which lets the daemon admin payload (4.1) and
    `gobby status` agree on installation state without filesystem coordination.
    """
    db = db or LocalDatabase(_default_db_path())
    store = ConfigStore(db)
    return store.get("databases.falkordb.host") is not None and \
           store.get("databases.falkordb.port") is not None

async def is_falkordb_healthy(host: str | None, port: int | None, password: str | None) -> bool:
    """PING the FalkorDB host/port; return True on PONG."""
    if not host or not port:
        return False
    try:
        import redis.asyncio as redis
        client = redis.Redis(host=host, port=port, password=password, socket_timeout=5)
        result = await client.ping()
        await client.aclose()
        return bool(result)
    except Exception:
        return False

async def get_falkordb_status(
    *, db: LocalDatabase | None = None,
    host: str | None = None, port: int | None = None, password: str | None = None,
) -> dict[str, Any]:
    installed = is_falkordb_installed(db=db)
    healthy = await is_falkordb_healthy(host, port, password) if installed else False
    return {
        "installed": installed,
        "healthy": healthy,
        "url": f"redis://{host}:{port}" if host and port else None,
    }
```

The two-tier model — `installed` from config_store, `healthy` from a live `PING` — gives the admin payload (4.1) and `gobby status` the right granularity: a freshly installed Docker container that has not yet finished starting reads `installed=true, healthy=false` so the operator sees an actionable status; a truly absent install reads `installed=false, healthy=false` and is silently skipped from the status payload.

Update every caller of the old `is_neo4j_*` / `get_neo4j_status` functions in this file and elsewhere (find via `grep -rn 'is_neo4j_\|get_neo4j_status' src/gobby/`). The admin `_health.py` route (covered in Phase 4) is the largest consumer.

### 3.4 Rename Neo4j CLI flags to FalkorDB and add service-targeting flag [category: code] (depends: 3.1, 3.3)

Target: `src/gobby/cli/install.py`, `src/gobby/cli/_install_prompts.py`

**Current state — verified against the actual code, not assumed:**

`gobby install` today has these flags that touch the graph backend (verified via `gobby install --help` and `grep -n` on `src/gobby/cli/install.py`):

- `--no-ext-services` (line ~119, name `no_ext_services_flag`) opts out of Docker service installs (Qdrant + Neo4j). **Note the actual flag name is `--no-ext-services`, not `--no-services`** — earlier draft had this wrong.
- `--neo4j-password` (line ~125, option) overrides the auto-generated Neo4j password
- There is **no `--neo4j`** flag on `gobby install`
- The auto-install block (line ~310: `if not no_ext_services_flag and embedding_provider != "none":`) calls `_run_qdrant_install` then `_run_neo4j_install`
- `gobby uninstall --neo4j` (line ~368, flag) DOES exist for uninstall

The current `web/src/setup/steps/Services.tsx` calls `runGobby(["install", "--neo4j", ...])` — that argument list does not match a real flag, which means the wizard's Services step is already broken (Click rejects unknown flags). The migration both fixes that and renames the surface.

**Decision for the cutover:**

1. **Auto-install behavior is preserved.** When the user runs plain `gobby install` (no targeting flags) with embeddings enabled, FalkorDB is auto-installed alongside Qdrant — exactly as Neo4j is today.
2. **Add a `--falkordb` service-targeting flag** to `gobby install`. This mirrors the existing CLI-targeting flags (`--claude`, `--gemini`, `--codex`, `--qwen`) which scope the install down to a single component. When `--falkordb` is set, ONLY the FalkorDB service install runs — no CLI hooks, no git hooks, no embedding setup, no voice. This is what the wizard needs (it already configured CLIs / embeddings in earlier steps; the Services step is service-only). Without this flag, the wizard cannot invoke a service-only install through `gobby install`.

**Changes to `gobby install` (`src/gobby/cli/install.py`):**

Add the new flag and rename the password option:

```python
# OLD (delete):
@click.option("--neo4j-password", "neo4j_password", default=None,
              help="Set a custom Neo4j password (default: auto-generated)")

# NEW:
@click.option("--falkordb", "falkordb_flag", is_flag=True, default=False,
              help="Install only the FalkorDB service (service-targeting; skips CLI hooks/git/embedding/voice).")
@click.option("--falkordb-password", "falkordb_password", default=None,
              help="Set a custom FalkorDB password (default: auto-generated or reused from existing config)")
```

No `--falkordb-mode` flag — FalkorDB is Docker-only in 0.4.0 (see 3.1's mode decision).

- Update `--no-ext-services` help text (line ~119) to read: `Skip Docker service installation (Qdrant, FalkorDB)`
- Update the `Skipping Qdrant/Neo4j install (embeddings disabled)` echo (line ~314) to say `FalkorDB`
- Replace the `_run_neo4j_install(install_neo4j, neo4j_password, results)` call (line ~312) with `_run_falkordb_install(install_falkordb, falkordb_password, results)`
- Update the function-signature parameters (line ~163-164): `no_ext_services_flag: bool` stays; rename `neo4j_password: str | None` → `falkordb_password: str | None`; add `falkordb_flag: bool`
- **Service-targeting branch:** at the top of the install body (right after the targeting-flag short-circuits for `--claude` / `--gemini` / etc.), add an early return when `falkordb_flag` is set: skip every other install step and call only `_run_falkordb_install(install_falkordb, falkordb_password, results)`, then echo the per-component summary and exit. Follow the exact pattern the existing CLI-targeting flags use.

**Changes to `gobby uninstall`:**

- Replace `--neo4j` flag (line ~368, name `neo4j_flag`) with `--falkordb` flag (name `falkordb_flag`)
- Update the help text on the `--volumes` flag (line ~377) from `(use with --neo4j)` to `(use with --falkordb)`
- Update `_run_neo4j_uninstall(uninstall_neo4j, volumes_flag, results)` call (line ~524) → `_run_falkordb_uninstall(uninstall_falkordb, volumes_flag, results)`

**Changes to `_install_prompts.py`:**

Rename `_run_neo4j_install` → `_run_falkordb_install` and `_run_neo4j_uninstall` → `_run_falkordb_uninstall`. The new install invoker:

- Takes `(installer, password, results)`
- Calls `installer(password=password)`
- Echoes the resolved values from the result dict — including the generated password ONLY if the result indicates the password was newly generated (the installer returns this signal)
- Mentions the FalkorDB Browser URL `http://localhost:13000` in the success echo
- Includes a `Restart the daemon to apply: gobby restart` line, mirroring the current Neo4j invoker

The uninstall invoker mirrors the existing `_run_neo4j_uninstall` shape and passes the volumes flag through to `uninstall_falkordb` as the `purge` argument.

**Wizard wiring (cross-references Phase 6.2):** the wizard's `Services.tsx` invokes `runGobby(["install", "--falkordb", ...optional --falkordb-password])`. This is the exact reason the `--falkordb` service-targeting flag exists — without it, the wizard would have to either re-run the full installer (re-doing CLI hooks etc.) or shell out to a Python entry point that bypasses Click. Phase 6.2 keeps using the args list shown above; this section guarantees the flag actually exists.

### 3.5 Rename bootstrap neo4j_password to falkordb_password end-to-end [category: code] (depends: 1.1, 3.1)

Target: `src/gobby/config/bootstrap.py` (BootstrapConfig + load_bootstrap), `src/gobby/cli/daemon.py` (`_services_start` consumer), `src/gobby/cli/installers/falkor.py` (`_write_bootstrap_password` writer added in 3.1)

The bootstrap surface has THREE coupled pieces that must move together — touching only the loader (as the prior draft did) leaves stale consumers reading a dead field:

**1. `BootstrapConfig` field rename (`src/gobby/config/bootstrap.py`):**

```python
# OLD (delete):
neo4j_password: str = "gobbyneo4j"

# NEW:
falkordb_password: str = "gobbyfalkor"
```

The default string `gobbyfalkor` matches the docker-compose template default in 3.2 (`${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor}`). Default exists for the same reason the current Neo4j default exists — to let `_services_start` bring up the compose stack with a deterministic value before the installer has run end-to-end.

**2. `load_bootstrap` rewrite — read new key, drop old key, swap env-var name:**

```python
return BootstrapConfig(
    database_path=str(data.get("database_path", BootstrapConfig.database_path)),
    daemon_port=int(data.get("daemon_port", BootstrapConfig.daemon_port)),
    bind_host=str(data.get("bind_host", BootstrapConfig.bind_host)),
    websocket_port=int(data.get("websocket_port", BootstrapConfig.websocket_port)),
    ui_port=int(data.get("ui_port", BootstrapConfig.ui_port)),
    falkordb_password=str(
        data.get(
            "falkordb_password",
            os.environ.get("GOBBY_FALKORDB_PASSWORD", BootstrapConfig.falkordb_password),
        )
    ),
)
```

The old `neo4j_password` YAML key is silently ignored (Pydantic-style — unrecognized keys do not raise). Do **not** copy the old value over: it belongs to a different running service and will not authenticate against FalkorDB. The user runs `gobby install --falkordb` to seed new credentials, which `_write_bootstrap_password` then writes back to `bootstrap.yaml` under the new key.

**3. `_services_start` consumer rewrite (`src/gobby/cli/daemon.py`):**

```python
# OLD (delete):
env["GOBBY_NEO4J_PASSWORD"] = bootstrap.neo4j_password
if config.databases.neo4j.url:
    profiles.append("neo4j")

# NEW:
env["GOBBY_FALKORDB_PASSWORD"] = bootstrap.falkordb_password
if config.databases.falkordb.requirepass:  # presence indicates configured/enabled
    profiles.append("falkordb")
```

This is the consumer the reviewer flagged as half-migrated — without this rewrite, `gobby start` would still try to inject `GOBBY_NEO4J_PASSWORD` into the docker-compose env and read the dead `bootstrap.neo4j_password` field. The 4.3 daemon-wide sweep should have caught this if missed; this section calls it out explicitly so 3.5's scope is unambiguous.

**4. `_write_bootstrap_password` (added in 3.1's installer):**

The helper writes back to `bootstrap.yaml` under the key `falkordb_password`. It must rewrite the YAML preserving any other keys (database_path, daemon_port, etc.) — copy the existing-file-merge pattern from `installers/neo4j.py::_write_bootstrap_password`.

**No bootstrap schema version bump needed** — the field rename is keyed on key presence and Pydantic ignores unknown keys, so the loader is forward+backward compatible against both old (`neo4j_password`) and new (`falkordb_password`) YAML files. The old value is simply discarded on first load.

### 3.6 Migrate config_store keys databases.neo4j.* → databases.falkordb.* [category: code] (depends: 1.1)

Target: `src/gobby/storage/_migration_registry.py` (register the new migration here), `src/gobby/storage/migrations.py` (the runner — no changes needed; it iterates the registry)

**Real schema — verified before writing this task:**

- `config_store` table holds dotted keys → values. For secrets, the value is the literal string `$secret:<name>` (where `<name>` is `config_key_to_secret_name(key)` — i.e., the LAST segment of the dotted key) and `is_secret=1`.
- `secrets` table holds the encrypted plaintext keyed by that natural name (NOT a `secret_store` table — the prior draft had the table name wrong).
- `ConfigStore.clear_secret(key, secret_store)` deletes both rows in one transaction. `SecretStore.delete(name)` removes the encrypted entry alone.
- The current Neo4j config writes `databases.neo4j.auth` → secret name `auth` (per `config_key_to_secret_name`).

**Migration strategy:**

The SQLite migration surface lives at `src/gobby/storage/_migration_registry.py` (declarative list of migration entries) plus `src/gobby/storage/migrations.py` (runner). Add a new entry to the registry list — versioned one above the current highest entry. The runner picks it up automatically; no runner changes.

Migrations run with a raw `sqlite3.Connection`. `ConfigStore.clear_secret` requires both a `db` and a `SecretStore`, which is heavier than the migration context. Use raw SQL against the actual table names. **Critical:** preserve backend-agnostic tunables (`graph_search`, `graph_min_score`, `rrf_k`, `graph_name`) that should survive the backend swap — they describe KG behavior, not backend connection details.

```python
# Connection/auth keys — drop on migration (these are Neo4j-specific)
NEO4J_CONNECTION_KEYS = ("url", "auth", "database", "host", "port")

# Behavior tunables — migrate from databases.neo4j.* to databases.falkordb.* if user-overridden
NEO4J_TUNABLE_KEYS = ("graph_search", "graph_min_score", "rrf_k", "graph_name")

def migrate_neo4j_to_falkordb_config_keys(conn: sqlite3.Connection) -> None:
    """Migrate user-tuned graph behavior; drop Neo4j-specific connection/auth keys.

    Tunables (graph_search, graph_min_score, rrf_k, graph_name) describe KG behavior
    that is backend-agnostic — these survive the backend swap. The user's tuning of
    rrf_k=80 (for example) should not silently revert to the FalkorConfig default of 60.

    Connection keys (url, auth, database, host, port) describe Neo4j-specific
    transport — these are dropped because the values cannot authenticate against
    FalkorDB. The user runs `gobby install --falkordb` to seed new values.

    Schema notes:
    - config_store rows for databases.neo4j.auth hold a `$secret:auth` reference.
    - The `auth` secret in the `secrets` table backs `databases.neo4j.auth` exclusively
      in the pre-migration codebase (verified — no other config key resolves to last-segment `auth`).
    - The `auth` secret is deleted only when no remaining config_store row references it.
    """
    # 1. Migrate behavior tunables (rename databases.neo4j.<key> → databases.falkordb.<key>
    #    when they exist AND the falkordb counterpart does not already exist — i.e., user
    #    has overridden the default and we don't want to clobber any value that was somehow
    #    already written under the new key)
    for key in NEO4J_TUNABLE_KEYS:
        conn.execute(
            "INSERT OR IGNORE INTO config_store (key, value, source, is_secret) "
            "SELECT REPLACE(key, 'databases.neo4j.', 'databases.falkordb.'), value, source, is_secret "
            "FROM config_store WHERE key = ?",
            (f"databases.neo4j.{key}",),
        )
    # 2. Drop ALL databases.neo4j.* keys (tunables already copied above; connection
    #    keys + the `$secret:auth` reference do not survive)
    conn.execute("DELETE FROM config_store WHERE key LIKE 'databases.neo4j.%'")
    # 3. Drop the orphaned encrypted secret if and only if nothing else references the name
    conn.execute(
        "DELETE FROM secrets WHERE name = 'auth' "
        "AND NOT EXISTS (SELECT 1 FROM config_store WHERE value = '$secret:auth')"
    )
```

Idempotent. Safe. Preserves user tuning across the backend swap.

**8.2 cross-reference:** the startup-time stale-config warning in 8.2 should ALSO clear any matching `databases.neo4j.*` secret references it finds at runtime, in case the migration has been skipped (e.g., a user pinning to an older daemon version that did not include this migration). 8.2 already covers warnings; extend it to call this same cleanup logic via `ConfigStore.clear_secret("databases.neo4j.auth", secret_store)` (the helper IS available at runtime, unlike during raw-SQL migration time).

This task is critical to running before Phase 7 (Rust) lands, because gobby-cli reads `databases.falkordb.*` from the config_store. If the daemon hasn't migrated, the Rust client reads nothing and falls back to "graph unavailable."

## Phase 4: Python — Admin Payload and Memory Routes

**Goal**: Update the `/api/admin/status` endpoint to emit `memory.falkordb` (matching the new frontend hook) and rename `_neo4j_client` references in memory routes.

### 4.1 Update admin _health.py to emit memory.falkordb status payload [category: code] (depends: Phase 2, 3.3)

Target: `src/gobby/servers/routes/admin/_health.py:245-259`

Replace the neo4j status assembly:

```python
# OLD (delete):
from gobby.cli.services import is_neo4j_healthy, is_neo4j_installed
neo4j_client = getattr(server.memory_manager, "_neo4j_client", None)
neo4j_url = neo4j_client.base_url if neo4j_client else None
installed = is_neo4j_installed()
healthy = await is_neo4j_healthy(neo4j_url) if neo4j_url else False
memory_stats["neo4j"] = {
    "configured": neo4j_client is not None,
    "installed": installed,
    "healthy": healthy,
    "url": neo4j_url,
}
```

with:

```python
from gobby.cli.services import get_falkordb_status

falkor_client = getattr(server.memory_manager, "_falkor_client", None)
falkor_cfg = server.config.databases.falkordb
status = await get_falkordb_status(
    db=server.db,  # config_store source-of-truth lives in the daemon's LocalDatabase
    host=falkor_cfg.host,
    port=falkor_cfg.port,
    password=falkor_cfg.requirepass,  # FalkorConfig field renamed to avoid secret-name collision; see 1.1
)
memory_stats["falkordb"] = {
    "configured": falkor_client is not None,
    "installed": status["installed"],
    "healthy": status["healthy"],
    "url": status["url"],
}
```

Replace the empty-fallback path (line 259) similarly: `memory_stats["falkordb"] = {"configured": False, "installed": False, "healthy": False, "url": None}`.

The dict key change from `neo4j` → `falkordb` is the load-bearing contract change for Phase 5 (frontend).

### 4.2 Rename _neo4j_client references in memory routes [category: refactor] (depends: Phase 2)

Target: `src/gobby/servers/routes/memory.py:277, 301`

Find every `getattr(server.memory_manager, "_neo4j_client", None)` and replace with `getattr(server.memory_manager, "_falkor_client", None)`. Also rename any local variable named `neo4j_client` to `falkor_client` in this file. The endpoint paths (`/api/memories/graph/entities`, `/api/memories/graph/entities/{key}/neighbors`) stay the same — frontend continues calling them with no change.

Search broadly for any other `_neo4j_client` references with `grep -rn '_neo4j_client' src/gobby/` and rename all hits.

### 4.3 Sweep daemon-wide for residual Neo4j references [category: refactor] (depends: Phase 2, 3.3, 3.6)

Target (verify each, edit any that touches Neo4j):

- `src/gobby/runner_init.py`
- `src/gobby/runner_lifecycle.py`
- `src/gobby/runner_maintenance.py`
- `src/gobby/cli/daemon.py`
- `src/gobby/cli/memory.py`
- `src/gobby/cli/pack.py`
- `src/gobby/utils/status.py`
- `src/gobby/config/code_index.py`
- `src/gobby/mcp_proxy/tools/memory.py`

These call sites still read `db_cfg.neo4j.*`, inject `GOBBY_NEO4J_PASSWORD` into subprocess env, key reports/payloads under `neo4j`, expose Neo4j-branded MCP tool descriptions, or print Neo4j-named status output. The earlier tasks (1.1, 2.1, 2.2, 3.3, 3.4, 3.6, 4.1, 4.2) only touched the core wiring — these surfaces are the long tail.

For each file:

1. `grep -n 'neo4j\|Neo4j\|NEO4J' <file>` to enumerate hits
2. Replace `db_cfg.neo4j.*` → `db_cfg.falkordb.*` (host/port/password/graph_name) using the new `FalkorConfig` shape from 1.1
3. Replace `GOBBY_NEO4J_PASSWORD` → `GOBBY_FALKORDB_PASSWORD` in any subprocess env injection (e.g., `cli/pack.py` likely passes this into a child process for snapshotting)
4. Rename payload/report keys: `report["neo4j"]` → `report["falkordb"]`, log keys `neo4j_url` → `falkordb_url`, etc.
5. For `mcp_proxy/tools/memory.py`: the tool descriptions on `search_knowledge_graph`, `rebuild_knowledge_graph`, `clear_knowledge_graph` may mention Neo4j by name — rewrite to mention FalkorDB or the neutral phrase "knowledge graph backend"
6. For `cli/memory.py`: any user-facing `gobby memory` subcommand text mentioning Neo4j must be updated
7. For `utils/status.py`: the status formatter likely has a Neo4j row — replace with FalkorDB

After the sweep, run `rg -l 'neo4j\|Neo4j\|NEO4J' src/gobby/` and verify the only hits remaining are intentional (e.g., the bootstrap migration in 3.5 that detects `neo4j_password`, the config_store migration in 3.6 that deletes `databases.neo4j.*`, and any CHANGELOG entry from Phase 9). Everything else must be gone.

This task gates Phase 5 (frontend) and Phase 9 (docs) — neither of those should be touched until the daemon-side sweep is clean, otherwise frontend types and doc rg sweeps will catch ghost references.

### 4.4 Teach config secret-detection that requirepass is a secret [category: code] (depends: 1.1)

Target: `src/gobby/storage/config_store.py` (the `_SECRET_SUFFIXES` constant + `is_secret_key_name`), `src/gobby/servers/routes/configuration.py`, `src/gobby/mcp_proxy/tools/config.py`, and the corresponding tests in `tests/mcp_proxy/tools/test_config.py::TestIsSecretKeyName`.

The `requirepass` field name is intentional (chosen in 1.1 to avoid the `password` last-segment collision with `auth.password`), but `src/gobby/storage/config_store.py:is_secret_key_name()` only recognizes keys whose last segment ends in suffixes from `_SECRET_SUFFIXES` (currently `password`, `_secret`, `_auth`, etc.). `requirepass` does not end in any of those, so:

- `/api/config` GET would return the value plaintext instead of masked
- `/api/config` PUT would write to plain `config_store` instead of routing through `SecretStore`
- The config MCP tools (`get_config`, `set_config` in `mcp_proxy/tools/config.py`) would do the same
- Config import/export would round-trip the password as plain text in YAML

Two equally valid fixes — pick one and apply consistently:

**Option A (preferred):** Add `requirepass` to `_SECRET_SUFFIXES`. One-line change in `config_store.py`. Catches any future `*.requirepass` keys automatically.

**Option B:** Add an explicit allowlist for `databases.falkordb.requirepass` in `is_secret_key_name`. More targeted, less risk of false positives, but requires updating the allowlist for each new non-suffix-matching secret.

After the change:

1. Update `tests/mcp_proxy/tools/test_config.py::TestIsSecretKeyName` to cover `databases.falkordb.requirepass` returning `True`
2. Add a route-level test in `tests/servers/routes/test_configuration_routes.py` that round-trips the value via PUT then GET and verifies the GET response is masked (`********` or whatever convention the existing `auth.password` tests follow)
3. Add a config-MCP-tool test that `call_tool("gobby-config", "get_config", {"key": "databases.falkordb.requirepass"})` masks the value, matching the existing `auth.password` MCP-tool mask behavior. (No `gobby config` CLI exists today — verified via `gobby --help`. The `/api/config` route + the `gobby-config` MCP tools are the only config surfaces in scope.)
4. If config import/export goes through a separate code path (check `src/gobby/cli/pack.py` or wherever YAML round-tripping lives), add a test that the exported YAML masks or omits the secret value rather than emitting plaintext.

This task is a **prerequisite for any UI / API change that exposes the FalkorDB password value**. Without it, the new field is exposed as plaintext via every config surface.

## Phase 5: Web UI — Browser Components

**Goal**: Update the browser-side hooks, types, components, and tests to consume the renamed admin payload and reflect FalkorDB branding.

### 5.1 Rename Neo4jStatus to FalkorStatus and update useMemory hook + tests [category: code] (depends: 4.1)

Target: `web/src/hooks/useMemory.ts:351-382`, `web/src/hooks/__tests__/useMemory.test.ts:258-264`

In `web/src/hooks/useMemory.ts`:

```typescript
// OLD:
export interface Neo4jStatus {
  configured: boolean
  url?: string
}

export function useNeo4jStatus() {
  const [neo4jStatus, setNeo4jStatus] = useState<Neo4jStatus | null>(null)
  // ...
  const neo4j = data.memory?.neo4j
  if (neo4j) {
    setNeo4jStatus(neo4j)
  }
  // ...
}
```

```typescript
// NEW:
export interface FalkorStatus {
  configured: boolean
  url?: string
}

export function useFalkorStatus() {
  const [falkorStatus, setFalkorStatus] = useState<FalkorStatus | null>(null)
  // ...
  const falkordb = data.memory?.falkordb
  if (falkordb) {
    setFalkorStatus(falkordb)
  }
  // ...
}
```

Update the warning log message: `'Failed to fetch falkordb status:'`.

In `web/src/hooks/__tests__/useMemory.test.ts:258-264`:

```typescript
// OLD:
import { useMemory, useNeo4jStatus } from '../useMemory'
describe('useNeo4jStatus', () => {
  it('fetches neo4j status from admin endpoint', async () => {
    const mockResponse = {
      memory: { neo4j: { configured: true, url: 'bolt://localhost:7687' } },
    }
    // ...
    const { result } = renderHook(() => useNeo4jStatus())
```

```typescript
// NEW:
import { useMemory, useFalkorStatus } from '../useMemory'
describe('useFalkorStatus', () => {
  it('fetches falkordb status from admin endpoint', async () => {
    const mockResponse = {
      memory: { falkordb: { configured: true, url: 'redis://localhost:6379' } },
    }
    // ...
    const { result } = renderHook(() => useFalkorStatus())
```

### 5.2 Update useDashboard type and SystemHealthCard pill [category: code] (depends: 4.1)

Target: `web/src/hooks/useDashboard.ts:29`, `web/src/components/dashboard/SystemHealthCard.tsx:32, 81-84`

In `web/src/hooks/useDashboard.ts:29`:

```typescript
// OLD:
memory: { count: number; by_type: Record<string, number>; recent_count: number; neo4j?: { configured: boolean; installed: boolean; healthy: boolean }; qdrant?: { configured: boolean; healthy: boolean } }

// NEW:
memory: { count: number; by_type: Record<string, number>; recent_count: number; falkordb?: { configured: boolean; installed: boolean; healthy: boolean }; qdrant?: { configured: boolean; healthy: boolean } }
```

In `web/src/components/dashboard/SystemHealthCard.tsx:32, 81-84`:

```typescript
// OLD:
const neo4j = memory?.neo4j
// ... in items array:
neo4j && {
  id: 'neo4j',
  label: `Neo4j ${neo4j.healthy ? 'connected' : neo4j.configured ? 'disconnected' : 'not configured'}`,
  status: neo4j.healthy ? 'healthy' : neo4j.configured ? 'unhealthy' : 'unknown',
}

// NEW:
const falkordb = memory?.falkordb
// ... in items array:
falkordb && {
  id: 'falkordb',
  label: `FalkorDB ${falkordb.healthy ? 'connected' : falkordb.configured ? 'disconnected' : 'not configured'}`,
  status: falkordb.healthy ? 'healthy' : falkordb.configured ? 'unhealthy' : 'unknown',
}
```

### 5.3 Update MemoryPage hook ref and KnowledgeGraph empty-state copy [category: code] (depends: 5.1)

Target: `web/src/components/memory/MemoryPage.tsx:3, 104, 132-146`, `web/src/components/memory/KnowledgeGraph.tsx:408`

In `web/src/components/memory/MemoryPage.tsx`:

```typescript
// Line 3:
import { useMemory, useFalkorStatus } from '../../hooks/useMemory'

// Line 104:
const falkorStatus = useFalkorStatus()

// Line 132-146 — auto-switch logic comment + variable references:
// Default to knowledge view when FalkorDB is configured and no saved preference
useEffect(() => {
  if (falkorStatus?.configured && viewMode === 'list' && !autoSwitchedRef.current) {
    // ...
  }
}, [falkorStatus?.configured, viewMode])
```

In `web/src/components/memory/KnowledgeGraph.tsx:408`:

```typescript
// OLD:
Connect a Neo4j instance to explore knowledge graph entities and relationships.

// NEW:
Connect a FalkorDB instance to explore knowledge graph entities and relationships.
```

Behavior is unchanged across both components — only identifier names and user-facing copy.

## Phase 6: Web UI — Ink Setup Wizard (in scope for 0.4.0)

**Goal**: Update the Ink-based setup wizard so `gobby setup` invokes the new CLI flags and persists state under the new field names. Manual end-to-end verification required before merge.

### 6.1 Update setup state.ts schema fields with one-shot migration [category: code] (depends: 3.4)

Target: `web/src/setup/utils/state.ts`

Rename schema fields:

```typescript
// OLD (lines 19-20, 40-41):
neo4j_installed: boolean;
neo4j_password_set: boolean;
// initial state:
neo4j_installed: false,
neo4j_password_set: false,
```

```typescript
// NEW:
falkordb_installed: boolean;
falkordb_password_set: boolean;
// initial state:
falkordb_installed: false,
falkordb_password_set: false,
```

No `falkordb_mode` field — FalkorDB is Docker-only in 0.4.0 (see 3.1's mode decision). Add a one-shot migrator inside `loadState()` that detects the old field names and rewrites them:

```typescript
function migrateState(raw: any): SetupState {
  if ('neo4j_installed' in raw) {
    raw.falkordb_installed = false  // value doesn't transfer; user re-runs install
    delete raw.neo4j_installed
  }
  if ('neo4j_password_set' in raw) {
    raw.falkordb_password_set = false
    delete raw.neo4j_password_set
  }
  return raw as SetupState
}
```

The persisted state file is `~/.gobby/setup_state.json` (underscore, not hyphen — verified against `web/src/setup/utils/state.ts`'s `loadState`/`saveState` and the daemon's setup-state admin route).

### 6.2 Update Services.tsx CLI flags (Docker-only) [category: code] (depends: 6.1)

Target: `web/src/setup/steps/Services.tsx`

The Services step's existing Docker-only gate in `web/src/setup/App.tsx` (`skipIf: (s) => !s.detected_tools?.docker`) is unchanged — FalkorDB is Docker-only in 0.4.0, so the existing gate is correct. No `detect.ts` changes needed.

Replace every `neo4j` reference. Phase flow stays Y/N/P (no mode picker):

1. **prompt** — "Install FalkorDB knowledge graph?" with [y]/[n]/[p] (yes / no / yes-with-custom-password). Copy mentions FalkorDB, not Neo4j; mentions Docker is required.
2. **password** — only entered if user picked [p] in step 1. Same mask behavior as today.
3. **installing** — "Installing FalkorDB via Docker..."
4. **done** — same.

The install invocation:

```typescript
const args = ["install", "--falkordb"];
if (password) {
  args.push("--falkordb-password", password);
}
```

`finish()` writes:

```typescript
setState((prev) => {
  const next = {
    ...prev,
    falkordb_installed: installed,
    falkordb_password_set: passwordSet,
    completed_step_id: "services" as const,
  };
  saveState(next);
  return next;
});
```

Update all user-facing strings in this file: "Neo4j" → "FalkorDB" everywhere.

### 6.3 Regenerate the bundled setup.mjs artifact [category: code] (depends: 6.2)

Target: `src/gobby/install/shared/setup/setup.mjs` (regenerate), `web/package.json` (build script verification)

`gobby setup` does **not** execute `web/src/setup/**` directly — it runs the bundled artifact at `src/gobby/install/shared/setup/setup.mjs`. Editing the TypeScript sources in 6.1 + 6.2 is necessary but not sufficient; the bundle must be regenerated and checked in for the wizard changes to actually ship.

Steps:

1. Read `web/package.json` and identify the build script that produces `src/gobby/install/shared/setup/setup.mjs` (likely a `build:setup` or `bundle:setup` script using esbuild/tsup/rollup)
2. Run that build script: `cd web && npm run build:setup` (or whatever the actual command is — verify before running)
3. Confirm the output landed at `src/gobby/install/shared/setup/setup.mjs` (the path the daemon's setup launcher reads)
4. Diff the regenerated bundle against the prior version — should show string changes for the renamed flags/labels (Neo4j → FalkorDB) and any updated copy
5. Commit the regenerated bundle alongside the TS source edits in the same PR

If `web/package.json` does not have a script that produces `src/gobby/install/shared/setup/setup.mjs`, add one. The build must be reproducible from `npm run …` so future contributors do not have to reverse-engineer the bundling steps.

### 6.4 Update Launch.tsx summary and verify wizard end-to-end [category: manual] (depends: 6.3)

Target: `web/src/setup/steps/Launch.tsx:211-212`

Replace summary lines:

```typescript
// OLD:
- Neo4j: ${state.neo4j_installed ? "installed (Docker)" : "not installed"}
- Neo4j password: ${state.neo4j_password_set ? "custom" : state.neo4j_installed ? "auto-generated" : "n/a"}

// NEW:
- FalkorDB: ${state.falkordb_installed ? "installed (Docker)" : "not installed"}
- FalkorDB password: ${state.falkordb_password_set ? "custom" : state.falkordb_installed ? "auto-generated" : "n/a"}
```

Manual verification (this is the `[category: manual]` work — observe results, do not just code-edit):

0. **Bundle freshness check:** `git status src/gobby/install/shared/setup/setup.mjs` — confirm the regenerated bundle from 6.3 is staged. If it is not, the wizard will run the OLD bundle and none of the 6.1/6.2 changes will be observable. Re-run the build script before continuing.
1. `rm ~/.gobby/setup_state.json && gobby setup` — wizard runs cleanly from cold (note underscore in filename)
2. Walk through the Services step, accept the install → wizard advances to Launch with summary "FalkorDB: installed"; verify `docker compose ps` shows the FalkorDB container healthy; verify the FalkorDB Browser at `http://localhost:13000` loads; verify `gobby status` reports FalkorDB healthy
3. `gobby uninstall --falkordb` to clean up
4. Repeat the wizard with the **--password** path (`[p]` in the Y/N/P prompt); verify the custom password is accepted and persisted
5. **Migration test:** create a `~/.gobby/setup_state.json` with the old `neo4j_installed: true, neo4j_password_set: false` fields; rerun `gobby setup`; verify state.ts migrator rewrites it to `falkordb_*` fields and the wizard does not crash
6. **Bundle parity check:** rebuild the bundle one more time after all manual fixes; confirm `git diff src/gobby/install/shared/setup/setup.mjs` shows no further changes (i.e., the committed bundle matches what the build produces from the committed sources)

If wizard bitrot independent of this migration is uncovered during this verification (e.g., other steps fail), fix it as part of this task. The plan is to ship 0.4.0 with a working onboarding wizard.

## Phase 7: Rust gobby-cli — FalkorDB Read Client

**Goal**: Replace the Neo4j HTTP client in `crates/gcode/src/neo4j.rs` with a FalkorDB client using the official `falkordb` Rust crate; rename config; preserve all 8 read function signatures.

### 7.1 Replace Neo4jConfig with FalkorConfig in gobby-cli config.rs [category: code] (depends: 3.6)

Target: `/Users/josh/Projects/gobby-cli/crates/gcode/src/config.rs:15-21, 49-50, 79, 292-351`

In `crates/gcode/src/config.rs`:

```rust
// OLD:
pub struct Neo4jConfig {
    pub url: String,
    pub auth: Option<String>,
    pub database: String,
}

// NEW:
#[derive(Debug, Clone)]
pub struct FalkorConfig {
    pub host: String,
    pub port: u16,
    pub password: Option<String>,
    pub graph_name: String,
}
```

In the `Context` struct, replace `pub neo4j: Option<Neo4jConfig>` with `pub falkordb: Option<FalkorConfig>`.

In `Context::resolve`, replace `let neo4j = resolve_neo4j_config(&db_path, quiet);` with `let falkordb = resolve_falkordb_config(&db_path, quiet);` and update the struct literal accordingly.

Replace the `resolve_neo4j_config` function (≈lines 292-351) with `resolve_falkordb_config` reading the new config_store keys:

```rust
fn resolve_falkordb_config(db_path: &Path, quiet: bool) -> Option<FalkorConfig> {
    // Env var overrides take priority
    let env_host = std::env::var("GOBBY_FALKORDB_HOST").ok();
    let env_port = std::env::var("GOBBY_FALKORDB_PORT").ok().and_then(|s| s.parse().ok());
    let env_password = std::env::var("GOBBY_FALKORDB_PASSWORD").ok();

    // Load from config_store
    let conn = open_db_readonly(db_path)?;
    let host_raw = read_config_key(&conn, "databases.falkordb.host")?;
    let port_raw = read_config_key(&conn, "databases.falkordb.port")?;
    let password_raw = read_config_key(&conn, "databases.falkordb.requirepass");

    let host = env_host.unwrap_or_else(|| resolve_secrets(&host_raw));
    let port: u16 = env_port.unwrap_or_else(|| resolve_secrets(&port_raw).parse().unwrap_or(6379));
    let password = env_password.or_else(|| password_raw.map(|raw| resolve_secrets(&raw)));

    Some(FalkorConfig {
        host,
        port,
        password,
        graph_name: "gobby_code".to_string(),  // Rust crate reads the code graph
    })
}
```

The Rust crate reads the **code** graph (`gobby_code`), not the memory KG (`gobby_kg`). Hardcode `graph_name = "gobby_code"`.

### 7.2 Pin FalkorClient API shape and result-conversion contract [category: code] (depends: 7.1)

Target: `/Users/josh/Projects/gobby-cli/crates/gcode/src/falkor.rs` (new — skeleton only), `/Users/josh/Projects/gobby-cli/crates/gcode/Cargo.toml`, `/Users/josh/Projects/gobby-cli/crates/gcode/src/search/graph_boost.rs` (mutability change to `with_neo4j`)

Pin the wrapper contract before porting any queries. The `falkordb` Rust crate (v0.2.x) does not match the loose `params: Option<serde_json::Value>` shape from the prior draft. Real constraints:

- `SyncGraph` is **not thread-safe** and is used **mutably** in the official examples. The wrapper must hold `&mut SyncGraph`.
- `QueryBuilder::with_params` accepts `HashMap<String, String>` (or types that implement `IntoIterator<Item=(impl ToString, impl ToString)>`), **not** arbitrary `serde_json::Value`. All param values must be stringified at the call site.
- `QueryBuilder::execute` returns `QueryResult<LazyResultSet<FalkorValue>>`. Iteration yields `Vec<FalkorValue>` per record (positionally aligned with the result header). `FalkorValue` is an enum (`String`, `I64`, `F64`, `Bool`, `Null`, `Array`, `Map`, `Node`, `Edge`, `Path`, `Point`).
- The current `with_neo4j` wrapper hands out `&Neo4jClient` (shared); the FalkorDB version hands out `&mut FalkorClient` to the closure. **The mutability lives on the local `FalkorClient` binding inside `with_falkor`, NOT on `Context`** — `Context` continues to store the immutable config (`FalkorConfig`) and `with_falkor` constructs a fresh client per call from that config (mirroring how the current `with_neo4j` constructs a `Neo4jClient` per call). Callers continue to pass `&Context`; no `&mut Context` cascade through `commands/graph.rs` or `search/graph_boost.rs`.

In `Cargo.toml`:

```toml
# Add:
falkordb = "0.2"

# Keep reqwest (used by other code paths for embedding API)
# Keep base64 (used by crates/gcode/src/secrets.rs for Fernet key derivation
#   via base64::engine::general_purpose::URL_SAFE — verified live, not removable
#   as part of this transport swap)
```

The dependency diff for this task is **add only** — `falkordb`. Do NOT drop `base64`; despite being a Neo4j-era addition in spirit, the live secret-resolution module (`secrets.rs`) imports it for Fernet key derivation. Removing it breaks the gcode build before any FalkorDB code lands.

Create `crates/gcode/src/falkor.rs` with **skeleton only** (no query bodies yet — those land in 7.3):

```rust
use std::collections::HashMap;
use serde_json::Value;
use falkordb::{FalkorClientBuilder, FalkorConnectionInfo, FalkorValue, SyncGraph};
use crate::config::{Context, FalkorConfig};
use crate::models::GraphResult;

const CALL_TARGET_PREDICATE: &str =
    "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol";

pub type Row = HashMap<String, Value>;

pub struct FalkorClient {
    graph: SyncGraph,
}

impl FalkorClient {
    pub fn from_config(config: &FalkorConfig) -> anyhow::Result<Self> {
        let conn_info = FalkorConnectionInfo::new(
            &config.host,
            config.port,
            config.password.as_deref(),
        );
        let client = FalkorClientBuilder::new()
            .with_connection_info(conn_info)
            .build()?;
        let graph = client.select_graph(&config.graph_name);
        Ok(Self { graph })
    }

    /// Execute a Cypher statement. `params` values are stringified for the
    /// FalkorDB crate's HashMap<String, String> contract; numeric/list values
    /// must be encoded as Cypher literals before reaching this layer (the 8 read
    /// queries already do this — no JSON values in their params).
    pub fn query(
        &mut self,
        cypher: &str,
        params: Option<HashMap<String, String>>,
    ) -> anyhow::Result<Vec<Row>> {
        let mut builder = self.graph.query(cypher);
        if let Some(p) = params {
            builder = builder.with_params(p);
        }
        let result = builder.execute()?;
        Ok(parse_falkor_result(result))
    }
}

/// Map FalkorDB QueryResult records into Row dicts keyed by column alias.
/// Header order = record value order. FalkorValue → serde_json::Value:
/// String/I64/F64/Bool/Null map directly; Array/Map recurse; Node/Edge/Path
/// flatten to {labels|type, properties} maps via their .properties() / .labels().
fn parse_falkor_result(
    result: falkordb::QueryResult<falkordb::LazyResultSet<FalkorValue>>,
) -> Vec<Row> {
    // Implementation: extract .header() column names, iterate .data() records,
    // zip header names with record values, convert each FalkorValue to serde_json::Value.
    // ...
}

/// Graceful-degradation wrapper. Hands out &mut FalkorClient to the closure
/// while keeping ctx as a shared reference — the mutability lives on the
/// local `client` binding, not on Context.
pub fn with_falkor<T>(
    ctx: &Context,
    default: T,
    f: impl FnOnce(&mut FalkorClient) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let Some(config) = ctx.falkordb.as_ref() else {
        return Ok(default);
    };
    let mut client = match FalkorClient::from_config(config) {
        Ok(c) => c,
        Err(_) => return Ok(default),
    };
    f(&mut client)
}
```

**No `Context` mutability cascade.** Callers in `crates/gcode/src/search/graph_boost.rs` and `crates/gcode/src/commands/graph.rs` keep their `&Context` parameters unchanged. The pattern is exactly the current `with_neo4j(ctx: &Context, ...) -> { let client = Neo4jClient::from_config(ctx)?; f(&client) }` shape, with the only difference being `let mut client = FalkorClient::from_config(ctx.falkordb.as_ref()?)?; f(&mut client)`. If `Context` ever grows to own a long-lived `FalkorClient` instance (a real architectural change for connection pooling), that decision and its lifetime/borrow plan would be a separate task — out of scope here.

Verify the `parse_falkor_result` skeleton compiles against `falkordb 0.2.x` (build with no query implementations yet, just the type plumbing) before declaring 7.2 complete. The whole point of this task is to lock down types before pouring 8 query implementations through them.

### 7.3 Port 8 read queries to FalkorClient [category: code] (depends: 7.2)

Target: `/Users/josh/Projects/gobby-cli/crates/gcode/src/falkor.rs` (fill in query bodies), `/Users/josh/Projects/gobby-cli/crates/gcode/src/neo4j.rs` (delete after queries land)

With the API shape pinned in 7.2, port all 8 public read functions. Preserve the function names (`count_callers`, `count_usages`, `find_callers`, `find_usages`, `find_callers_batch`, `find_callees_batch`, `get_imports`, `blast_radius`) and their `ctx: &Context` parameter — only their bodies change.

Apply Cypher dialect translations to the 8 statements. The translations needed for the read queries are minimal because they don't use vector indexes, constraints, or `datetime()`:

- All 8 use `MATCH ... WHERE target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol` — label disjunction in WHERE, supported by FalkorDB
- `blast_radius` uses variable-length `[:CALLS*1..{depth}]` with depth interpolated (clamped 1-5) — supported
- `count_*` queries return `count(...)` — supported
- `find_*` queries use `SKIP $offset LIMIT $limit` — supported

Verbatim queries to port (from `crates/gcode/src/neo4j.rs:184-393`):

1. `count_callers`: `MATCH (caller:CodeSymbol {project: $project})-[:CALLS]->(target {id: $id, project: $project}) WHERE {CALL_TARGET_PREDICATE} RETURN count(caller) AS cnt`
2. `count_usages`: same pattern, count(source)
3. `find_callers`: same, RETURN caller fields, SKIP/LIMIT
4. `find_usages`: same, RETURN source fields
5. `find_callers_batch`: `WHERE {CALL_TARGET_PREDICATE} AND target.id IN $ids`
6. `find_callees_batch`: `WHERE src.id IN $ids AND ({CALL_TARGET_PREDICATE})`
7. `get_imports`: `MATCH (f:CodeFile {path: $path, project: $project})-[:IMPORTS]->(m:CodeModule) RETURN m.name AS module_name`
8. `blast_radius` (built by `blast_radius_query(depth)`): variable-length path with `OPTIONAL MATCH` for file path

**Param marshaling:** every `params` value must be a `String` per the `with_params(HashMap<String, String>)` contract from 7.2. For `find_callers_batch`/`find_callees_batch`, the `$ids` list parameter cannot be passed as a Rust `Vec<String>` directly — interpolate it as a Cypher list literal in the query string instead (e.g., `target.id IN ['id1', 'id2', 'id3']`), or split into N separate single-id queries. Pick one and document the choice.

Keep `row_to_graph_result` unchanged (still maps `Row -> GraphResult` with field-precedence fallbacks). The `Row = HashMap<String, serde_json::Value>` type stays.

Keep all 7 existing unit tests in spirit — replace the `parse_v2_response` tests with equivalent tests for `parse_falkor_result` from 7.2. The `test_blast_radius_query_targets_stable_ids_and_all_target_labels` test stays as-is (just verifies the query string contains the right interpolated bits).

Delete `crates/gcode/src/neo4j.rs` after all 8 queries are ported and `cargo test -p gobby-code` passes.

### 7.4 Update gobby-cli callsites and module declarations [category: refactor] (depends: 7.3)

Target: `/Users/josh/Projects/gobby-cli/crates/gcode/src/main.rs`, `crates/gcode/src/search/graph_boost.rs`, `crates/gcode/src/search/semantic.rs:154`, `crates/gcode/src/commands/graph.rs`

In `crates/gcode/src/main.rs`, replace `mod neo4j;` with `mod falkor;`.

In `crates/gcode/src/search/graph_boost.rs`:
- `use crate::neo4j;` → `use crate::falkor;`
- `with_neo4j` → `with_falkor` everywhere
- `&Context` parameters on `graph_boost` and `graph_expand` stay `&Context` (no mutability cascade — see 7.2)
- Test functions: `test_graph_boost_no_neo4j` → `test_graph_boost_no_falkor`, `test_graph_expand_no_neo4j` → `test_graph_expand_no_falkor`
- Test fixture `Context { neo4j: None, ... }` → `Context { falkordb: None, ... }`

In `crates/gcode/src/search/semantic.rs:154`: `neo4j: None` → `falkordb: None`

In `crates/gcode/src/commands/graph.rs`: every `neo4j::` call → `falkor::`. The 6 callsites (lines ≈266, 267, 316, 317, 357, 392) keep the same function names so this is purely a path swap. The `ctx: &Context` parameters on `callers`, `usages`, `imports`, `blast_radius` stay unchanged — `with_falkor` accepts `&Context` and constructs the mutable client locally.

Run `cargo test -p gobby-code` and `cargo build --release -p gobby-code` to verify the rename is complete and the new transport compiles cleanly.

## Phase 8: Cross-Repo Cutover Choreography

**Goal**: Define the operational dance for landing both repos atomically — merge ordering, validation matrix, CLI flag deprecation policy, runtime warnings for users on upgrade.

### 8.1 Decide CLI flag deprecation policy and add hard-fail messaging [category: code] (depends: 3.4, 4.3)

Target: `src/gobby/cli/install.py` (the install/uninstall commands modified in 3.4)

**Decision: do not preserve `--neo4j-password` (install) or `--neo4j` (uninstall) as aliases.** Hard-fail with a clear migration error. Aliases obscure the cutover, leave dead code paths, and create confusion when users see both flags work but only one matches the running service.

The actual deprecated surfaces (per 3.4):
- `gobby install --neo4j-password <value>` — was a Click option on `install`
- `gobby uninstall --neo4j` — was a Click flag on `uninstall`

When a user passes either, fail fast with:

```
Error: --neo4j / --neo4j-password has been removed in 0.4.0.

The knowledge graph backend has been replaced with FalkorDB.
- Install (auto-runs as part of gobby install; tune with): gobby install [--falkordb-password <pw>] (or service-only: gobby install --falkordb)
- Uninstall: gobby uninstall --falkordb
- Migration notes: see CHANGELOG.md for the full upgrade path.
```

Implement by registering `--neo4j-password` on `install` and `--neo4j` on `uninstall` as `click.option(... hidden=True)` whose handler immediately raises `click.UsageError` with the message above. This way `--help` does not advertise them, but typo-tolerant users (and anyone running an old script) get a real explanation instead of "no such option".

### 8.2 Add startup-time stale-config warning [category: code] (depends: 3.6, 4.3)

Target: `src/gobby/runner_init.py` (or wherever the daemon's startup health/sanity checks fire)

After the config_store migration in 3.6 has run, add a startup check that detects leftover Neo4j-shaped config and surfaces it. The migration in 3.6 deletes `databases.neo4j.*` keys, but defensive in-depth: if for any reason those keys are still present (e.g., user restored an old DB backup, ran an out-of-band tool), the daemon should warn.

```python
def _check_stale_neo4j_config(db: LocalDatabase, secret_store: SecretStore) -> None:
    """Detect and clear stale Neo4j config_store + secret entries at startup.

    Defense-in-depth against the 3.6 migration being skipped (e.g., user restored
    an old DB backup). Mirrors the migration's two-step cleanup:
      1. Drop `databases.neo4j.*` config_store keys
      2. Drop the orphaned `auth` secret if no other config references it
    Uses `ConfigStore.clear_secret` for the auth secret since the helper IS available
    at runtime (unlike during raw-SQL migration time).
    """
    config_store = ConfigStore(db)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key FROM config_store WHERE key LIKE 'databases.neo4j.%'"
        ).fetchall()
    if not rows:
        return
    keys = ", ".join(r[0] for r in rows)
    logger.warning(
        "Detected stale Neo4j config keys (%s) — these are no longer used. "
        "Run `gobby install --falkordb` to set up FalkorDB. "
        "Cleaning them up now.",
        keys,
    )
    # Use clear_secret for the auth key (handles config_store + secrets in one txn)
    config_store.clear_secret("databases.neo4j.auth", secret_store)
    # Migrate behavior tunables before deleting (mirror 3.6's policy — preserve user tuning)
    NEO4J_TUNABLE_KEYS = ("graph_search", "graph_min_score", "rrf_k", "graph_name")
    with db.transaction() as conn:
        for key in NEO4J_TUNABLE_KEYS:
            conn.execute(
                "INSERT OR IGNORE INTO config_store (key, value, source, is_secret) "
                "SELECT REPLACE(key, 'databases.neo4j.', 'databases.falkordb.'), value, source, is_secret "
                "FROM config_store WHERE key = ?",
                (f"databases.neo4j.{key}",),
            )
        conn.execute(
            "DELETE FROM config_store WHERE key LIKE 'databases.neo4j.%'"
        )
```

Call this from `runner_init.py`'s startup sequence, after the migration runner but before the memory manager initializes. Pass both the `LocalDatabase` and the `SecretStore` (both are already constructed earlier in the startup sequence).

### 8.3 Define cross-repo validation matrix and merge ordering [category: manual] (depends: 8.1, 8.2, Phase 5, 6.4, Phase 7)

Target: this is operational/manual work, not a code edit. Document the matrix below in the Python repo's `CHANGELOG.md` 0.4.0 entry (Phase 9.1) and execute it before either repo merges.

**Merge ordering:**

1. **Python feature branch (`gobby:falkordb-migration`)** lands first
   - All Phase 1-6 + Phase 8.1-8.2 tasks complete
   - Pre-release version cut: `0.4.0-rc1`
   - Published to whatever distribution channel the team uses (PyPI test index, internal artifact store, etc.)
2. **Rust feature branch (`gobby-cli:falkordb-migration`)** reads against `gobby 0.4.0-rc1`
   - All Phase 7 tasks complete
   - Pre-release version cut: `gcode 0.7.0-rc1`
3. **Joint validation** (matrix below) must all pass before either branch merges to main
4. **Coordinated bump:** both repos bump from `-rc1` to non-RC versions in lockstep PRs that land within minutes of each other. The window must be small enough that no user pulls one but not the other.

**Validation matrix** (must all pass against a single live Docker FalkorDB instance — 0.4.0 ships Docker-only per 3.1):

| # | Check | Pass criterion |
|---|---|---|
| 1 | `gobby install` from clean `~/.gobby` (FalkorDB auto-installs alongside Qdrant per 3.4) | Exits 0; `docker compose -f ~/.gobby/services/docker-compose.yml ps` shows the `falkordb` profile healthy; FalkorDB Browser at `http://localhost:13000` loads; `gobby status` reports FalkorDB healthy |
| 2 | `gobby install --falkordb` (service-targeting only) from clean `~/.gobby` | Exits 0; only the FalkorDB container is started (no CLI hooks/git/embedding/voice run); `docker compose -f ~/.gobby/services/docker-compose.yml ps` shows the falkordb profile healthy |
| 3 | `gobby install --neo4j-password foo` AND `gobby uninstall --neo4j` (deprecated flags) | Both hard-fail with the migration message from 8.1 |
| 4 | `uv run pytest tests/memory/ tests/cli/installers/ tests/code_index/ --cov=gobby --cov-fail-under=80 --cov-report=term-missing` | Exit 0 (the `--cov-fail-under=80` flag is what enforces the threshold; without it `pytest` only reports coverage when explicitly requested per this repo's `pyproject.toml`) |
| 5 | `uv run mypy src/gobby/memory/ src/gobby/code_index/ src/gobby/cli/installers/` | Exit 0 |
| 6 | `uv run ruff check src/` | Exit 0 |
| 7 | `cd web && npm run type-check && npm run test && npm run build` | All exit 0 (script names per `web/package.json`: `type-check`, `test`, `build`) |
| 8 | `gobby setup` end-to-end (clean state) | Wizard completes; FalkorDB installed via Docker; `setup_state.json` shows `falkordb_*` fields populated |
| 9 | `gobby setup` end-to-end with `[p]` (custom password) | Wizard accepts password; persisted in config_store + bootstrap; `docker compose -f ~/.gobby/services/docker-compose.yml exec falkordb redis-cli -a <pw> PING` returns `PONG` (use `exec` not host-side `-p 16379` so the test cannot be misled by an unrelated host-side Redis on port 16379) |
| 10 | `gobby setup` migration test (pre-existing `neo4j_*` state file) | Wizard rewrites to `falkordb_*` without crashing |
| 11 | `cargo test -p gobby-code && cargo build --release -p gobby-code` | Exit 0 |
| 12 | `gcode index .` against a known fixture project, then `gcode callers <known_function>` | Returns expected callers; results match a saved fixture diff |
| 13 | `gcode blast-radius <known_function>` | Returns expected transitive callers |
| 14 | `gcode search "<query>"` with graph-boost enabled | Returns ranked results with graph-boosted entries |
| 15 | Browser memory page loads, 3D graph renders, dashboard shows "FalkorDB connected" pill | Visual confirmation in browser |
| 16 | Daemon restart with stale `databases.neo4j.*` keys present in config_store | Logs the warning from 8.2 and deletes the keys |
| 17 | `gobby uninstall --falkordb` then check `~/.gobby/bootstrap.yaml` | `falkordb_password` key is removed (R5-F1 split-brain fix); `databases.falkordb.*` config_store entries cleared |
| 18 | **Upgrade path:** seed `~/.gobby/services/docker-compose.yml` with the OLD Neo4j-era template (or use a real existing-install fixture), then run `gobby install`. Verify with `docker compose -f ~/.gobby/services/docker-compose.yml ps` and `docker compose -f ~/.gobby/services/docker-compose.yml exec falkordb redis-cli -a <pw> PING`. | Compose file is refreshed to the FalkorDB template (R6-F1 fix); previously-running Neo4j container is stopped (no orphans); FalkorDB container starts on the `falkordb` profile and reaches healthy state; `redis-cli ... PING` returns `PONG` |
| 19 | Set `databases.falkordb.requirepass` to a value via `/api/config` PUT, then GET it back via `/api/config`. Repeat with the `gobby-config` MCP tool (`set_config` then `get_config`). | Both surfaces return the value masked (`********`); raw DB inspection (`sqlite3 ~/.gobby/gobby-hub.db`) shows `$secret:requirepass` in `config_store` and the encrypted value in `secrets` (R6-F2 fix verifies `requirepass` is treated as a secret across both supported surfaces — there is no `gobby config` CLI to test) |
| 20 | `rg -l 'neo4j\|Neo4j\|NEO4J' src/gobby/ web/src/` (Python repo) AND `rg -l 'neo4j\|Neo4j\|NEO4J' crates/` (run from gobby-cli repo root) | Only intentional refs remain — Python: bootstrap migration helper, storage migration that drops old keys, CHANGELOG; Rust: CHANGELOG only |

If any check fails, that branch does not merge. Fix and re-run the full matrix.

**Operator messaging:** the 0.4.0 release notes (Phase 9.1 CHANGELOG) must lead with the upgrade steps and a clear statement that Neo4j data is not migrated. Do not bury this in a "Notes" section.

## Phase 9: Documentation

**Goal**: Update all user-facing and developer-facing documentation in both repos.

### 9.1 Update Python repo documentation [category: docs] (depends: Phase 1, 2, 3, 4, 5, 6, 8)

Target: `README.md`, `CLAUDE.md`, `CHANGELOG.md`, any `docs/**/*.md` mentioning Neo4j

Sweep with `rg -l Neo4j /Users/josh/Projects/gobby` (excluding source code already covered in earlier phases) and update each doc file:

- `README.md` — replace Neo4j references in the architecture/installation sections
- `CLAUDE.md` — update the development guidance section if it mentions Neo4j
- `CHANGELOG.md` — add a 0.4.0 entry under **"Breaking changes"** with the following content:
  - Lead: "Replaced Neo4j with FalkorDB as the knowledge graph backend. FalkorDB is Docker-only in 0.4.0 (a native local-install path is planned for a follow-up release). The `--neo4j-password` install option and the `--neo4j` uninstall flag have been **removed** (not aliased) and will hard-fail with a migration error pointing to `--falkordb`."
  - Upgrade steps:
    1. `gobby uninstall --neo4j` is no longer available. Manually stop and remove the Neo4j Docker service: `docker compose --profile neo4j down -v` if previously installed.
    2. `gobby install` (auto-installs FalkorDB alongside Qdrant) or `gobby install --falkordb` (service-only); pass `--falkordb-password <value>` for a custom password.
    3. Knowledge graph and code graph data are not migrated; re-run `rebuild_knowledge_graph` (MCP tool) for memory and `gcode index <project>` for the code graph.
    4. The `gobby-cli` Rust crate must be upgraded to a matching FalkorDB-era version (`gcode 0.7.0+`) — see the cross-repo validation matrix in Phase 8.
  - Embed the validation matrix from 8.3 as a "Verification" sub-section so operators can self-check after upgrading.
- `docs/guides/*.md` — sweep for Neo4j references; update install/setup sections to use the `--falkordb` flag and document the Docker-only constraint.

**Follow-up note:** native local-install support (without Docker) is deferred to 0.4.1 or later. Filing a follow-up task is part of the 0.4.0 release punch list — see the migration plan task tree under #12746 for the deferred-work item.

### 9.2 Update Rust repo documentation [category: docs] (depends: Phase 7, 8)

Target: `/Users/josh/Projects/gobby-cli/README.md`, `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`, `crates/gcode/README.md`

Sweep with `rg -l Neo4j /Users/josh/Projects/gobby-cli` and update:

- Top-level `README.md` — architecture section
- `CLAUDE.md`, `AGENTS.md` — replace developer-facing Neo4j references
- `crates/gcode/README.md` — command examples (`gcode callers`, `gcode usages`, etc. don't change syntactically; only the underlying graph backend description)
- `CHANGELOG.md` — add an entry naming the FalkorDB transition. Include a hard compatibility note: `gcode 0.7.0+` requires `gobby 0.4.0+`. Mismatched versions (gcode reading the new config_store keys against an old daemon that still writes the Neo4j keys) silently degrade to "graph unavailable" — surface this in the changelog entry so operators know to upgrade both at once. Reference Phase 8.3's validation matrix.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
