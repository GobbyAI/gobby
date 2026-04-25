# Migrate Knowledge Graph Backend from Neo4j to FalkorDB

## Overview

Replace Neo4j (HTTP Query API v2 + JVM + APOC) with FalkorDB (Redis RESP + native vector indexes + single binary) across the Python daemon (`gobby`), the Rust read client (`gobby-cli`), and the web UI (browser components + Ink onboarding wizard). Targets the 0.4.0 ship.

The architectural fact that shapes this plan: **the Rust crate is read-only** and **all graph writes happen in the Python daemon** — both for the memory knowledge graph (`KnowledgeGraphService` + `Neo4jClient`) and the code knowledge graph (`code_index/CodeGraph`). Dialect translation is concentrated in Python; Rust just needs a transport swap. Both repos must land in lockstep because the Rust crate reads the graph the daemon writes.

## Constraints

- **No compatibility shim, no dual-backend abstraction.** Full rip-and-replace; pick FalkorDB and commit.
- **Both repos land in one coordinated cut.** The admin payload key rename, frontend hook, setup wizard CLI flag, and Rust config-store keys must all flip together. CI goes red the moment the backend renames if the frontend lags.
- **Local install must work without Docker.** FalkorDB ships as a Homebrew formula (`falkordb/falkordb/falkordb`); the installer must support both local-binary and Docker paths.
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
        description="FalkorDB host. Local install: 127.0.0.1. Docker: 127.0.0.1 (port-mapped).",
    )
    port: int = Field(
        default=6379,
        description=(
            "FalkorDB port. Local install: 6379. Docker install: 16379 (host-side, "
            "remapped from container 6379 to avoid system Redis conflicts)."
        ),
    )
    password: str | None = Field(
        default=None,
        description=(
            "FalkorDB password (Redis AUTH). Must be provided when FalkorDB is enabled. "
            "Supports ${ENV_VAR} pattern for env var expansion at load time."
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

Target: `src/gobby/memory/services/knowledge_graph.py`, `src/gobby/memory/manager.py`

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

In `src/gobby/memory/manager.py:106-110`, swap the `Neo4jClient(...)` instantiation for `FalkorClient(host=..., port=..., password=..., graph_name="gobby_kg")`. Read these from `config.databases.falkordb.{host,port,password,graph_name}`. Update `self._neo4j_client` attribute to `self._falkor_client` and update the `KnowledgeGraphService(neo4j_client=...)` constructor call to pass `falkor_client=...` (matching the renamed `__init__` parameter on the service).

In `KnowledgeGraphService.__init__`, rename the `neo4j_client` parameter to `falkor_client` and the `self._neo4j` attribute to `self._falkor`. Update every `self._neo4j.X(…)` call site within the service.

The `add_to_graph` method's dynamic multi-label MERGE (≈line 184): `MERGE (n:Capitalize(entity_type):_Entity {entity_key: $key})` — FalkorDB supports multi-label MERGE but the parser is strict about whitespace. Smoke-test against realistic entity-type inputs.

### 2.2 Translate CodeGraph Cypher and wire CodeIndexContext [category: code] (depends: 1.2)

Target: `src/gobby/code_index/graph.py`, `src/gobby/code_index/context.py`

Apply the same dialect translations from 2.1 to every Cypher string in `src/gobby/code_index/graph.py`. The methods touching Cypher:

- `sync_file` (≈line 95) — bulk MERGE of nodes/relationships from a file's parsed AST; this is the primary write path triggered by gobby-cli's `/api/code-index/invalidate` POST
- `add_relationships` (≈line 258)
- `_ensure_node` (≈line 628) — node upsert helper
- `_cleanup_orphans` (≈line 64)
- `clear_project` (≈line 859), `delete_file` (≈line 874)
- `find_callers`, `find_usages`, `get_imports`, `get_import_chain`, `find_blast_radius`, `get_file_graph`, `get_file_symbols`, `get_symbol_neighbors`, `get_blast_radius_graph` — read methods that the daemon uses for HTTP route responses (not the Rust client)

Schema setup in CodeGraph likely creates indexes/constraints on `CodeSymbol.id`, `CodeFile.path`, etc. — apply the FalkorDB DDL dialect from the 2.1 table.

In `src/gobby/code_index/context.py`, swap the `Neo4jClient` instantiation for `FalkorClient(host=..., port=..., password=..., graph_name="gobby_code")` (note the **different graph_name** — code graph and memory KG are separate FalkorDB graphs in the same instance). Update `CodeGraph.__init__(neo4j_client=...)` parameter to `falkor_client=...`.

Verify `src/gobby/code_index/sync_worker.py` (the worker that consumes `/api/code-index/invalidate` POSTs from gobby-cli) only goes through `CodeGraph` — it should not hold a separate `Neo4jClient` reference. If it does, swap that too.

The blast-radius variable-length path query interpolates depth into the Cypher (depth clamped to 1-5); FalkorDB supports this pattern.

## Phase 3: Python — Installer, Bootstrap, and CLI Flags

**Goal**: Replace the Neo4j installer with a FalkorDB installer that supports both local Homebrew install and Docker; rename CLI flags; migrate bootstrap and config_store keys.

### 3.1 Implement FalkorDB installer with local and Docker modes [category: code] (depends: 1.1)

Target: `src/gobby/cli/installers/falkor.py` (new file), modeled on `src/gobby/cli/installers/qdrant.py`

Create `src/gobby/cli/installers/falkor.py` exposing two public functions: `install_falkordb(mode: Literal["local", "docker"], password: str | None, gobby_home: Path) -> dict[str, Any]` and `uninstall_falkordb(mode: Literal["local", "docker", "auto"], gobby_home: Path) -> dict[str, Any]`. Mirror the qdrant installer's overall shape (compose-yaml ensure, subprocess invocation, config update, health wait).

**Local mode** (`mode="local"`):

1. Detect `brew` binary; abort with a clear error message if missing
2. Run `brew install falkordb/falkordb/falkordb` via subprocess
3. Detect install path with `brew --prefix falkordb` to locate `falkordb.so`
4. Write `~/.gobby/services/falkordb/redis.conf` with:
   ```
   port 6379
   requirepass <password>
   loadmodule <brew_prefix>/lib/falkordb.so
   dir ~/.gobby/services/falkordb/data
   appendonly yes
   ```
5. Spawn `falkordb-server <conf_path>` as daemonized process; persist PID to `~/.gobby/services/falkordb/falkordb.pid`
6. Health check: `redis-cli -p 6379 -a <password> PING` returns `PONG` (poll up to 30s)
7. `_update_config(host="127.0.0.1", port=6379, password=password, mode="local")`

**Docker mode** (`mode="docker"`):

1. Ensure `~/.gobby/docker-compose.services.yml` exists by copying the bundled template (3.2 updates that template)
2. Run `docker compose --profile falkordb up -d` with `GOBBY_FALKORDB_PASSWORD=<password>` env. The compose template (Phase 3.2) maps that env into `REDIS_ARGS="--requirepass $GOBBY_FALKORDB_PASSWORD"` for the container — Redis auth lives on `REDIS_ARGS`, not `FALKORDB_ARGS` (the latter is reserved for FalkorDB module options like `MAX_QUEUED_QUERIES`).
3. Health check: `docker compose exec falkordb redis-cli -a "$GOBBY_FALKORDB_PASSWORD" PING` must return `PONG` (poll up to 60s; treat `NOAUTH`/`WRONGPASS` as a hard failure, not a transient health miss)
4. `_update_config(host="127.0.0.1", port=16379, password=password, mode="docker")`

**FalkorDB Browser:** the official `falkordb/falkordb` image bundles the browser on container port 3000. The compose template (3.2) maps it to host port 13000. After install, surface the browser URL `http://localhost:13000` in the success message — only for Docker mode.

`_update_config` writes the same keys regardless of mode, only differing on host/port:

```python
def _update_config(*, host: str, port: int, password: str, mode: str) -> None:
    db = LocalDatabase(...)
    store = ConfigStore(db)
    store.set("databases.falkordb.host", host, source="install")
    store.set("databases.falkordb.port", port, source="install")
    store.set("databases.falkordb.mode", mode, source="install")  # for uninstall routing
    secret_store = SecretStore(db)
    store.set_secret("databases.falkordb.password", password, secret_store, source="install")
```

`uninstall_falkordb(mode="auto", ...)` reads `databases.falkordb.mode` to know whether to stop the local process (kill PID + remove conf dir + `brew uninstall falkordb`) or run `docker compose --profile falkordb down -v`.

The success message printed to the terminal must mention: "for graph exploration outside the gobby web UI, choose the Docker install (FalkorDB Browser is bundled in the image)."

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
def is_falkordb_installed(*, gobby_home: Path | None = None) -> bool:
    """True if the local-binary install marker exists OR the docker compose service is up."""
    home = gobby_home or Path("~/.gobby").expanduser()
    if (home / "services" / "falkordb").exists():
        return True
    # Docker mode: check for compose profile via subprocess (best-effort)
    return False  # Refined below

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
    *, gobby_home: Path | None = None,
    host: str | None = None, port: int | None = None, password: str | None = None,
) -> dict[str, Any]:
    installed = is_falkordb_installed(gobby_home=gobby_home)
    healthy = await is_falkordb_healthy(host, port, password) if installed else False
    return {
        "installed": installed,
        "healthy": healthy,
        "url": f"redis://{host}:{port}" if host and port else None,
    }
```

Update every caller of the old `is_neo4j_*` / `get_neo4j_status` functions in this file and elsewhere (find via `grep -rn 'is_neo4j_\|get_neo4j_status' src/gobby/`). The admin `_health.py` route (covered in Phase 4) is the largest consumer.

### 3.4 Rename --neo4j CLI flags to --falkordb across install/uninstall commands [category: code] (depends: 3.1, 3.3)

Target: `src/gobby/cli/install.py` (or wherever `install` / `uninstall` Click commands are defined), `src/gobby/cli/_install_prompts.py`

In the `install` command, replace:

```python
@click.option("--neo4j", is_flag=True, help="Install Neo4j knowledge graph")
@click.option("--neo4j-password", help="Neo4j password (auto-generated if not provided)")
```

with:

```python
@click.option("--falkordb", is_flag=True, help="Install FalkorDB knowledge graph")
@click.option(
    "--falkordb-mode",
    type=click.Choice(["local", "docker"]),
    default=None,
    help="FalkorDB install mode (local Homebrew binary or Docker). Prompts if omitted.",
)
@click.option("--falkordb-password", help="FalkorDB password (auto-generated if not provided)")
```

Same for the `uninstall` command (single `--falkordb` flag).

In `_install_prompts.py`, replace `_run_neo4j_install` / `_run_neo4j_uninstall` with `_run_falkordb_install` / `_run_falkordb_uninstall`. The install prompt asks the user to pick local vs docker if `--falkordb-mode` was not passed; the prompt mentions the FalkorDB Browser availability tradeoff (Docker only).

### 3.5 Add bootstrap migration for neo4j_password → falkordb_password [category: code] (depends: 1.1)

Target: `src/gobby/config/bootstrap.py`

The bootstrap loader at `~/.gobby/bootstrap.yaml` carries a `neo4j_password` field today. Add a migration that runs on bootstrap load:

```python
def _migrate_falkordb_password(data: dict[str, Any]) -> dict[str, Any]:
    """Rename neo4j_password → falkordb_password (one-shot, idempotent)."""
    if "neo4j_password" in data and "falkordb_password" not in data:
        # Don't carry the value over — the password belongs to a different service.
        # Drop the old key; the user will be prompted on next FalkorDB install.
        data.pop("neo4j_password")
    elif "neo4j_password" in data:
        # Both present (shouldn't happen, but be safe): drop the stale one.
        data.pop("neo4j_password")
    return data
```

Call this from the bootstrap loader before validation. Do **not** copy the neo4j password value into `falkordb_password` — the password is for a different running service and will not authenticate against FalkorDB. Drop it; the user runs `gobby install --falkordb` to set up fresh credentials.

If the bootstrap.yaml file has a schema version field, bump it. If not, no version bump needed; the migration is keyed on key presence and is idempotent.

### 3.6 Migrate config_store keys databases.neo4j.* → databases.falkordb.* [category: code] (depends: 1.1)

Target: `src/gobby/config/migrations/` (or wherever DB migrations live), new migration file

Add a one-shot migration that runs on first daemon start after the upgrade:

```python
def migrate_neo4j_to_falkordb_config_keys(db: LocalDatabase) -> None:
    """Rename databases.neo4j.* config_store keys to databases.falkordb.* equivalents.

    Old: databases.neo4j.url, databases.neo4j.auth, databases.neo4j.database
    New: databases.falkordb.host, databases.falkordb.port, databases.falkordb.password, databases.falkordb.mode

    The values do not survive — Neo4j URL/auth aren't valid FalkorDB credentials.
    Just delete the old keys. User runs `gobby install --falkordb` to seed new values.
    """
    with db.transaction() as conn:
        conn.execute("DELETE FROM config_store WHERE key LIKE 'databases.neo4j.%'")
        # Also wipe any encrypted secret entries
        conn.execute("DELETE FROM secret_store WHERE name = 'NEO4J_AUTH'")
```

Register this migration in the migration runner. Idempotent because it's a `DELETE WHERE LIKE`.

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
    host=falkor_cfg.host,
    port=falkor_cfg.port,
    password=falkor_cfg.password,
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

**Goal**: Update the Ink-based setup wizard so `gobby setup` invokes the new CLI flags, surfaces the local-vs-Docker mode picker, and persists state under the new field names. Manual end-to-end verification required before merge.

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
falkordb_mode: 'local' | 'docker' | null;  // captures the user's mode choice
// initial state:
falkordb_installed: false,
falkordb_password_set: false,
falkordb_mode: null,
```

Add a one-shot migrator inside `loadState()` that detects the old field names and rewrites them:

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
  if (!('falkordb_mode' in raw)) {
    raw.falkordb_mode = null
  }
  return raw as SetupState
}
```

The persisted state file is `~/.gobby/setup_state.json` (underscore, not hyphen — verified against `web/src/setup/utils/state.ts`'s `loadState`/`saveState` and the daemon's setup-state admin route).

### 6.2 Update Services.tsx CLI flags and add local/docker mode picker [category: code] (depends: 6.1)

Target: `web/src/setup/steps/Services.tsx`

Replace every `neo4j` reference and add a mode picker between the Y/N/P prompt and the password phase:

```typescript
type Phase = "prompt" | "mode" | "password" | "installing" | "done";
```

Phase flow:

1. **prompt** — "Install FalkorDB knowledge graph?" with [y]/[n]/[p] (yes / no / yes-with-custom-password). Copy mentions FalkorDB, not Neo4j; mentions either Docker or Homebrew is required.
2. **mode** (new) — "Local (Homebrew) or Docker?" with [l]/[d]. Default [l] for parity with other native installs the user runs. Note that FalkorDB Browser is Docker-only.
3. **password** — only entered if user picked [p] in step 1. Same mask behavior as today.
4. **installing** — "Installing FalkorDB ({mode === 'local' ? 'via Homebrew' : 'via Docker'})..."
5. **done** — same.

The install invocation:

```typescript
const args = ["install", "--falkordb", "--falkordb-mode", mode];
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
    falkordb_mode: installed ? mode : null,
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
4. Diff the regenerated bundle against the prior version — should show string changes for the renamed flags/labels and the new mode picker phase
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
- FalkorDB: ${state.falkordb_installed ? `installed (${state.falkordb_mode})` : "not installed"}
- FalkorDB password: ${state.falkordb_password_set ? "custom" : state.falkordb_installed ? "auto-generated" : "n/a"}
```

Manual verification (this is the `[category: manual]` work — observe results, do not just code-edit):

0. **Bundle freshness check:** `git status src/gobby/install/shared/setup/setup.mjs` — confirm the regenerated bundle from 6.3 is staged. If it is not, the wizard will run the OLD bundle and none of the 6.1/6.2 changes will be observable. Re-run the build script before continuing.
1. `rm ~/.gobby/setup_state.json && gobby setup` — wizard runs cleanly from cold (note underscore in filename)
2. Walk through the Services step, choose **local mode** → wizard advances to Launch with summary "FalkorDB: installed (local)"; verify `redis-cli -p 6379 -a <pw> PING` returns `PONG`; verify `gobby status` reports FalkorDB healthy
3. `gobby uninstall --falkordb` to clean up
4. `rm ~/.gobby/setup_state.json && gobby setup` again, choose **docker mode** → wizard advances to Launch with "FalkorDB: installed (docker)"; verify `docker compose ps` shows healthy; verify the FalkorDB Browser at `http://localhost:13000` loads
5. Repeat with the **--password** path; verify the custom password is accepted and persisted
6. **Migration test:** create a `~/.gobby/setup_state.json` with the old `neo4j_installed: true, neo4j_password_set: false` fields; rerun `gobby setup`; verify state.ts migrator rewrites it to `falkordb_*` fields and the wizard does not crash
7. **Bundle parity check:** rebuild the bundle one more time after all manual fixes; confirm `git diff src/gobby/install/shared/setup/setup.mjs` shows no further changes (i.e., the committed bundle matches what the build produces from the committed sources)

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
    let password_raw = read_config_key(&conn, "databases.falkordb.password");

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
- The current `with_neo4j` wrapper hands out `&Neo4jClient` (shared); the FalkorDB version must hand out `&mut FalkorClient`.

In `Cargo.toml`:

```toml
# Drop:
base64 = "0.22"

# Add:
falkordb = "0.2"

# Keep reqwest (used by other code paths for embedding API)
```

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

/// Graceful-degradation wrapper. Note &mut FalkorClient (was &Neo4jClient).
pub fn with_falkor<T>(
    ctx: &mut Context,
    default: T,
    f: impl FnOnce(&mut FalkorClient) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    // Implementation calls FalkorClient::from_config and invokes f.
    // ...
}
```

**Mutability change ripple effect:** `with_neo4j` currently takes `&Context` (shared). `with_falkor` takes `&mut Context` because it constructs a `FalkorClient` that holds a `&mut SyncGraph`. Callers in `crates/gcode/src/search/graph_boost.rs` and `crates/gcode/src/commands/graph.rs` will need their `&Context` parameters changed to `&mut Context`. Cascade this in 7.4 (callsite updates), but enumerate the impacted call sites here so the porting task in 7.3 has the call graph already mapped:

- `crates/gcode/src/commands/graph.rs::callers` — currently `fn callers(ctx: &Context, …)` → must become `&mut Context`
- `crates/gcode/src/commands/graph.rs::usages` — same
- `crates/gcode/src/commands/graph.rs::imports` — same
- `crates/gcode/src/commands/graph.rs::blast_radius` — same
- `crates/gcode/src/search/graph_boost.rs::graph_boost` — same
- `crates/gcode/src/search/graph_boost.rs::graph_expand` — same

If any of these are themselves called with `&Context` from `main.rs` or another root, the cascade continues. Trace with `gcode callers` against each before starting 7.3.

Verify the `parse_falkor_result` skeleton compiles against `falkordb 0.2.x` (build with no query implementations yet, just the type plumbing) before declaring 7.2 complete. The whole point of this task is to lock down types before pouring 8 query implementations through them.

### 7.3 Port 8 read queries to FalkorClient [category: code] (depends: 7.2)

Target: `/Users/josh/Projects/gobby-cli/crates/gcode/src/falkor.rs` (fill in query bodies), `/Users/josh/Projects/gobby-cli/crates/gcode/src/neo4j.rs` (delete after queries land)

With the API shape pinned in 7.2, port all 8 public read functions. Preserve the function names (`count_callers`, `count_usages`, `find_callers`, `find_usages`, `find_callers_batch`, `find_callees_batch`, `get_imports`, `blast_radius`) — only their bodies and the `&mut Context` parameter change.

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
- **Change `&Context` parameters to `&mut Context`** on `graph_boost` and `graph_expand` (cascade from 7.2's mutability change)
- Test functions: `test_graph_boost_no_neo4j` → `test_graph_boost_no_falkor`, `test_graph_expand_no_neo4j` → `test_graph_expand_no_falkor`
- Test fixture `Context { neo4j: None, ... }` → `Context { falkordb: None, ... }`

In `crates/gcode/src/search/semantic.rs:154`: `neo4j: None` → `falkordb: None`

In `crates/gcode/src/commands/graph.rs`: every `neo4j::` call → `falkor::`. The 6 callsites (lines ≈266, 267, 316, 317, 357, 392) keep the same function names so this is purely a path swap. **Also change every `ctx: &Context` parameter to `ctx: &mut Context`** on `callers`, `usages`, `imports`, `blast_radius` to satisfy the `&mut FalkorClient` requirement from 7.2. Cascade further into `main.rs` if any of these are invoked with a shared `&Context` from the CLI dispatch root.

Run `cargo test -p gobby-code` and `cargo build --release -p gobby-code` to verify the rename + mutability cascade is complete and the new transport compiles cleanly.

## Phase 8: Cross-Repo Cutover Choreography

**Goal**: Define the operational dance for landing both repos atomically — merge ordering, validation matrix, CLI flag deprecation policy, runtime warnings for users on upgrade.

### 8.1 Decide CLI flag deprecation policy and add hard-fail messaging [category: code] (depends: 3.4, 4.3)

Target: `src/gobby/cli/install.py` (the install/uninstall commands modified in 3.4)

**Decision: do not preserve `--neo4j` / `--neo4j-password` as aliases.** Hard-fail with a clear migration error. Aliases obscure the cutover, leave dead code paths, and create confusion when users see both flags work but only one matches the running service.

When a user runs `gobby install --neo4j` or `gobby uninstall --neo4j` on the FalkorDB-era release, fail fast with:

```
Error: --neo4j has been removed in 0.4.0.

The knowledge graph backend has been replaced with FalkorDB.
- Install: gobby install --falkordb [--falkordb-mode local|docker]
- Uninstall: gobby uninstall --falkordb
- Migration notes: see CHANGELOG.md for the full upgrade path.
```

Implement by registering `--neo4j` and `--neo4j-password` as `click.option(... hidden=True)` flags whose handler immediately raises `click.UsageError` with the message above. This way `--help` does not advertise them, but typo-tolerant users get a real explanation instead of "no such option".

Same treatment for `gobby uninstall --neo4j`.

### 8.2 Add startup-time stale-config warning [category: code] (depends: 3.6, 4.3)

Target: `src/gobby/runner_init.py` (or wherever the daemon's startup health/sanity checks fire)

After the config_store migration in 3.6 has run, add a startup check that detects leftover Neo4j-shaped config and surfaces it. The migration in 3.6 deletes `databases.neo4j.*` keys, but defensive in-depth: if for any reason those keys are still present (e.g., user restored an old DB backup, ran an out-of-band tool), the daemon should warn.

```python
def _check_stale_neo4j_config(db: LocalDatabase) -> None:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key FROM config_store WHERE key LIKE 'databases.neo4j.%'"
        ).fetchall()
        if rows:
            keys = ", ".join(r[0] for r in rows)
            logger.warning(
                "Detected stale Neo4j config keys (%s) — these are no longer used. "
                "Run `gobby install --falkordb` to set up FalkorDB. "
                "Old keys will be deleted on next daemon restart.",
                keys,
            )
            # Also delete on detection so the warning self-clears
            conn.execute("DELETE FROM config_store WHERE key LIKE 'databases.neo4j.%'")
```

Call this from `runner_init.py`'s startup sequence, after the migration runner but before the memory manager initializes.

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

**Validation matrix** (must all pass against a single live FalkorDB instance, both local and Docker modes):

| # | Check | Pass criterion |
|---|---|---|
| 1 | `gobby install --falkordb --falkordb-mode local` from clean `~/.gobby` | Exits 0; `redis-cli -p 6379 -a <pw> PING` returns `PONG`; `gobby status` reports FalkorDB healthy |
| 2 | `gobby install --falkordb --falkordb-mode docker` from clean `~/.gobby` | Exits 0; `docker compose ps` shows healthy; FalkorDB Browser at `http://localhost:13000` loads |
| 3 | `gobby install --neo4j` (deprecated flag) | Hard-fails with the migration message from 8.1 |
| 4 | `uv run pytest tests/memory/ tests/cli/installers/ tests/code_index/` | Exit 0; ≥80% coverage maintained |
| 5 | `uv run mypy src/gobby/memory/ src/gobby/code_index/ src/gobby/cli/installers/` | Exit 0 |
| 6 | `uv run ruff check src/` | Exit 0 |
| 7 | `cd web && npm run typecheck && npm run test && npm run build` | All exit 0 |
| 8 | `gobby setup` end-to-end on local mode (clean state) | Wizard completes; FalkorDB installed locally; `setup_state.json` shows `falkordb_*` fields populated |
| 9 | `gobby setup` end-to-end on docker mode | Same, with mode=docker |
| 10 | `gobby setup` migration test (pre-existing `neo4j_*` state file) | Wizard rewrites to `falkordb_*` without crashing |
| 11 | `cargo test -p gobby-code && cargo build --release -p gobby-code` | Exit 0 |
| 12 | `gcode index .` against a known fixture project, then `gcode callers <known_function>` | Returns expected callers; results match a saved fixture diff |
| 13 | `gcode blast-radius <known_function>` | Returns expected transitive callers |
| 14 | `gcode search "<query>"` with graph-boost enabled | Returns ranked results with graph-boosted entries |
| 15 | Browser memory page loads, 3D graph renders, dashboard shows "FalkorDB connected" pill | Visual confirmation in browser |
| 16 | Daemon restart with stale `databases.neo4j.*` keys present in config_store | Logs the warning from 8.2 and deletes the keys |
| 17 | `rg -l 'neo4j\|Neo4j\|NEO4J' src/gobby/ web/src/ crates/` | Only intentional refs remain (bootstrap/migration code, CHANGELOG) |

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
  - Lead: "Replaced Neo4j with FalkorDB as the knowledge graph backend. The `--neo4j` / `--neo4j-password` install flags have been **removed** (not aliased) and will hard-fail with a migration error pointing to `--falkordb`."
  - Upgrade steps:
    1. `gobby uninstall --neo4j` is no longer available. Manually stop and remove the Neo4j Docker service: `docker compose --profile neo4j down -v` if previously installed.
    2. `gobby install --falkordb [--falkordb-mode local|docker]` to set up FalkorDB
    3. Knowledge graph and code graph data are not migrated; re-run `rebuild_knowledge_graph` (MCP tool) for memory and `gcode index <project>` for the code graph.
    4. The `gobby-cli` Rust crate must be upgraded to a matching FalkorDB-era version (`gcode 0.7.0+`) — see the cross-repo validation matrix in Phase 8.
  - Embed the validation matrix from 8.3 as a "Verification" sub-section so operators can self-check after upgrading.
- `docs/guides/*.md` — sweep for Neo4j references; update install/setup sections to reference `--falkordb` flag and the local-vs-Docker choice

Document the local-binary limitation: "FalkorDB Browser is bundled with the Docker image at port 3000 (mapped to host 13000). The Homebrew local install ships only the engine — for graph exploration outside the gobby web UI, choose Docker."

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
