# Migrate Knowledge Graph Backend from Neo4j to FalkorDB

## Overview

`kind: framing`

Replace Neo4j (HTTP Query API v2 + JVM + APOC) with FalkorDB (Redis RESP + native vector indexes + single binary) across the Python daemon (`gobby`), the Rust read client (`gobby-cli`), and the web UI (browser components + Ink onboarding wizard). Targets the 0.4.0 ship.

The architectural fact that shapes this plan: **the Rust crate is read-only** and **all graph writes happen in the Python daemon** — both for the memory knowledge graph (`KnowledgeGraphService` + `Neo4jClient`) and the code knowledge graph (`code_index/CodeGraph`). Dialect translation is concentrated in Python; Rust just needs a transport swap. Both repos must land in lockstep because the Rust crate reads the graph the daemon writes.

## Constraints

`kind: framing`

- **No compatibility shim, no dual-backend abstraction.** Full rip-and-replace; pick FalkorDB and commit.
- **Both repos land in one coordinated cut.** The admin payload key rename, frontend hook, setup wizard CLI flag, and Rust config-store keys must all flip together. CI goes red the moment the backend renames if the frontend lags.
- **Docker-only for 0.4.0.** A native local-install path was scoped, then dropped after verifying that FalkorDB ships no Homebrew formula and only raw `.so` Redis modules via GitHub releases. Native-mode support is deferred to a follow-up release. The current Neo4j experience is also Docker-only, so this is parity, not a regression.
- **Onboarding wizard is in scope for 0.4.0.** The Ink-based setup wizard at `web/src/setup/` must be functional after this migration — manual end-to-end verification is required before merge.
- **Data is wiped, not migrated.** Both graphs are derived from data already stored elsewhere (memories in SQLite + Qdrant, code graph from SQLite code index). Existing rebuild commands are idempotent. No export/import script.
- **Drop the vestigial 1536 dim default** in `ensure_vector_index`. The replacement requires `dimension` as a positional kwarg sourced from `EmbeddingsConfig.dim`. Closes a footgun.

## P1 Phase 1: Python — FalkorClient and Config

`kind: framing`

**Goal**: Stand up `FalkorClient` and `FalkorConfig` so subsequent phases can wire them in.

### 1.1 Replace Neo4jConfig with FalkorConfig and add falkordb dep [category: config]

`kind: deliverable`

Targets: `src/gobby/config/persistence.py`, `pyproject.toml`, `uv.lock` (R30-F1 — `uv add falkordb` (or `uv sync` after editing `pyproject.toml`) rewrites this file; it MUST be staged and committed in the same task so the reproducible-install lockfile stays in lockstep with `pyproject.toml`. Without this, expansion can close the §1.1 task with a stale `uv.lock`, and downstream tasks running `uv sync` in fresh checkouts would either fail to install `falkordb` or pin different versions than the author tested. The §1.1 acceptance below names the file explicitly so a closing diff that touches `pyproject.toml` without touching `uv.lock` cannot satisfy the task.)

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

**Canonical activation predicate (R16-F4 — single source of truth):** add `is_falkordb_enabled` to this same module so every downstream consumer (§ 2.1, § 2.2, § 3.5, § 4.1, § 4.3) imports it from one place. Define it directly under `DatabasesConfig`:

```python
def is_falkordb_enabled(databases: DatabasesConfig) -> bool:
    """Whether the FalkorDB knowledge-graph backend is active.

    Activation signal: the installer (§ 3.1) wrote `databases.falkordb.requirepass`
    into config_store and `load_config(config_store=..., secret_resolver=...)`
    successfully resolved it. Default `FalkorConfig.requirepass = None` so the
    truthy check distinguishes installed-and-resolved from unconfigured.

    Pass a `DatabasesConfig` instance (e.g. `runner.config.databases`), NOT the
    top-level config — `config` has no top-level `falkordb` attribute.
    """
    return bool(databases.falkordb.requirepass)
```

Every downstream consumer imports it as `from gobby.config.persistence import is_falkordb_enabled`. No local re-implementations; no wrapper modules. The function lives next to `DatabasesConfig` because it operates on that exact type.

**Password validator (R25-F1) — single source of truth for charset constraints:**

Add `validate_falkordb_password(value: str) -> str` to this same module. The Docker compose template (§ 3.2) interpolates the password through shell-expanded `REDIS_ARGS=--requirepass ${GOBBY_FALKORDB_PASSWORD}` and a `redis-cli -a "$$GOBBY_FALKORDB_PASSWORD" PING` healthcheck — both forms break (or worse, silently misauth) on whitespace, control characters, and unquoted shell metachars even after R25-F1 quotes the healthcheck. The Rust crate (§ 7.2) percent-encodes for `falkor://` URLs, so it accepts strictly more characters than Docker can round-trip — the migration's password contract must be the intersection.

```python
def validate_falkordb_password(value: str) -> str:
    """Reject FalkorDB passwords whose Docker boundary cannot round-trip.

    Permitted: printable ASCII excluding whitespace and control characters.
    Rejected: empty/None, any whitespace (space, tab, newline, etc.), any
    ASCII control character (0x00-0x1F, 0x7F), and high-bit/non-ASCII characters
    (Docker shell expansion is byte-oriented; safer to ban non-ASCII outright
    than to debug edge cases per locale).

    Raises ``ValueError`` with an operator-actionable message naming the rule
    that failed. CLI / wizard / /api/config / gobby-config all funnel through
    this single validator so the rule is consistent across ingress points.
    """
    if not value:
        raise ValueError("FalkorDB password must not be empty")
    if any(ch.isspace() for ch in value):
        raise ValueError("FalkorDB password must not contain whitespace")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("FalkorDB password must not contain ASCII control characters")
    if any(ord(ch) > 0x7E for ch in value):
        raise ValueError("FalkorDB password must use printable ASCII only (Docker round-trip constraint)")
    return value
```

Wire it as a `@field_validator("requirepass")` on `FalkorConfig` (only when `value is not None` — the default-None case is the unconfigured state, not a validation failure), AND call it explicitly from:

- `src/gobby/cli/installers/falkor.py::_resolve_falkordb_password` (§ 3.1) — validate the user's `--falkordb-password` value AND any auto-generated password (the generator must produce values that pass validation by construction; add a unit test asserting 100/100 generated passwords validate clean).
- The Ink wizard's `[p]` custom-password handler (§ 6.x) — surface the `ValueError` message inline so the operator can pick a different value without dropping out of the wizard.
- `/api/config/values` PUT for `databases.falkordb.requirepass` (§ 4.4) — return HTTP 422 with the validator's message when validation fails; do NOT persist a partially-validated value.
- `gobby-config set_config` (§ 4.4) — same surface, MCP error response.

The validator's tests live in `tests/config/test_persistence.py` (already owned by § 1.1 per R22-F4): cover empty, whitespace, tab, newline, control char, high-bit char, and at least three accepted punctuation passwords (`Pa$$w0rd!`, `aB-3.7=z`, `xyz_123-ABC`).

**Tests (R21-F1 + R22-F4) — owned by this task:**

- `tests/config/test_persistence.py` — replace every `Neo4jConfig` import and assertion with `FalkorConfig`; add coverage for `is_falkordb_enabled(databases)` returning True only when `requirepass` is set (and False on default/unconfigured); update `graph_min_score` field-validator tests to point at the new host class. Without this, the test file fails to import the moment § 1.1 deletes `Neo4jConfig`.
- `tests/config/test_memory_config.py` — rename `Neo4jConfig` references to `FalkorConfig`; update `databases.neo4j` → `databases.falkordb` field-path assertions; cover the secret-name derivation `config_key_to_secret_name('databases.falkordb.requirepass') == 'requirepass'` (this drives row #19 of § 8.3's validation matrix; getting it wrong would ship a config that the secret store can't round-trip).

These updates land with the config-class rename — leaving them stale would block § 8.3 row #4's expanded `pytest tests/config/` scope (R22-F4) on the very first import.

**Acceptance:**

- 1.1.1 — `FalkorConfig` Pydantic class replaces `Neo4jConfig` in persistence config. symbol: `gobby.config.persistence.FalkorConfig`.
- 1.1.2 — `falkordb` Python package added to `pyproject.toml` dependencies; `neo4j` dropped. file: `pyproject.toml`.
- 1.1.3 — `uv.lock` regenerated and committed alongside the `pyproject.toml` change so reproducible installs resolve the new dep set. file: `uv.lock`.

### 1.2 Implement FalkorClient mirroring Neo4jClient surface [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/memory/falkor_client.py` (new file), `src/gobby/memory/neo4j_client.py` (the file this client supersedes — body and §1.3 cite the deletion), `tests/memory/test_falkor_client.py`, `tests/memory/test_falkor_write_methods.py`, `tests/memory/test_falkor_vector_search.py`. **No `src/gobby/memory/__init__.py` exists** — memory is a namespace package; consumers import directly from `gobby.memory.falkor_client` rather than through a package init (see §1.3 for the verified namespace-package statement and the live import sites).

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
- `async def ensure_unique_constraint(self, label: str, prop: str) -> None` — sends `GRAPH.CONSTRAINT CREATE <graph_name> UNIQUE NODE Label PROPERTIES 1 prop` as a Redis command (out-of-band, not via `query()`). Then polls constraint status by running the **Cypher procedure** `CALL db.constraints()` against the selected graph (NOT a client-side `self._db.constraints()` method — `falkordb-py` does not expose constraints as a `FalkorDB` attribute, R25-F4). Concretely: `await self._graph.ro_query("CALL db.constraints()")` (or `await self.query("CALL db.constraints()")` since `query()` already wraps the same plumbing) and parse the returned rows by `type`, `label`, `properties`, `entitytype`, `status`. Loop with a sleep until the row matching `(type=UNIQUE, label, properties=[prop], entitytype=NODE)` reports `status=OPERATIONAL`, with a 30s timeout. Raise `FalkorQueryError` on `status=FAILED` (signals pre-existing data violates the constraint). The literal-method form (`self._db.constraints()`) would AttributeError on the first startup tick before any writes; pin the Cypher form so this hard startup gate cannot silently turn off.
- `async def merge_node`, `async def merge_relationship`, `async def set_node_vector` — same signatures as Neo4jClient counterparts
- `async def get_entity_graph`, `async def get_entity_neighbors`, `async def vector_search`, `async def execute_read`, `async def execute_write`, `async def ping` — same signatures
- `_validate_cypher_identifier(value: str, kind: str)` — keep the existing identifier-validation helper for safe Cypher interpolation

**Constraint readiness gates writes.** `ensure_memory_graph_schema` (Phase 2.1) and the equivalent in `code_index/graph.py` (Phase 2.2) MUST complete — including all constraint polling — before any `merge_node`/`merge_relationship` calls fire. FalkorDB constraint creation is asynchronous; firing writes against `PENDING` or `UNDER CONSTRUCTION` constraints yields silent inconsistency. Treat schema readiness as a hard startup gate.

Connection error mapping: catch `redis.exceptions.ConnectionError`, `redis.exceptions.TimeoutError` and raise `FalkorConnectionError`. Query error mapping (R22-F2): the `falkordb` Python package does NOT define a `falkordb.exceptions.QueryError` class; Cypher failures surface through the underlying redis client as `redis.exceptions.ResponseError` (the same class that carries `WRONGPASS` / `NOAUTH`, so handlers must inspect `exc.args[0]` to disambiguate auth-vs-query when the same `query()` call could hit either). Catch `redis.exceptions.ResponseError` and re-raise as `FalkorQueryError(message=str(exc), response_body=exc.args)`. `tests/memory/test_falkor_client.py` (owned by § 1.2) MUST include a focused case that drives a deliberate Cypher syntax error (e.g. `MATCH (n:`) through `query()` and asserts the resulting exception type is `FalkorQueryError` — not the underlying `redis.exceptions.ResponseError`. Without that test the mapping silently regresses on a future falkordb-py / redis upgrade.

The `query()` parser must collapse FalkorDB's response into the same `list[dict[str, Any]]` shape that callers expect — the dict keys are the column aliases from the Cypher RETURN clause. This keeps `KnowledgeGraphService` and `CodeGraph` consumers oblivious to the transport change.

Deletion of `src/gobby/memory/neo4j_client.py` is owned exclusively by §1.3 and runs AFTER Phase 2 has rewired the live consumers (`src/gobby/memory/manager.py` and `src/gobby/memory/services/knowledge_graph.py` — see §1.3's import-site enumeration). §1.2 must NOT delete or rename the legacy client; doing so before Phase 2 would break those imports at module-load time.

**Tests (R21-F1) — owned by this task:**

- `tests/memory/test_neo4j_client.py` → rename to `tests/memory/test_falkor_client.py`. Rewrite assertions that reach into Neo4j HTTP transport details (auth header shape, /db/`<db>`/query/v2 endpoint, etc.) to target the FalkorDB redis-asyncio path; preserve coverage of every public method on the FalkorClient surface added here.
- `tests/memory/test_neo4j_write_methods.py` → rename to `tests/memory/test_falkor_write_methods.py`. Update the asserted Cypher strings to FalkorDB dialect per § 2.1's translation table (e.g. `timestamp()` instead of `datetime()`, `vecf32(...)` instead of `db.create.setNodeVectorProperty`).
- `tests/memory/test_neo4j_vector_search.py` → rename to `tests/memory/test_falkor_vector_search.py`. Update the vector-index DDL assertions to `CREATE VECTOR INDEX FOR ... OPTIONS {dimension, similarityFunction}` and `db.idx.vector.queryNodes(label, prop, k, vecf32(emb))` (NOT `db.index.vector.queryNodes(idx_name, k, emb)`).

These renames must land in the same PR as `falkor_client.py` itself — leaving the old test files in place would block § 8.3 row #4's `pytest tests/memory/` run with import errors against the deleted `gobby.memory.neo4j_client` module.

**Acceptance:**

- 1.2.1 — `FalkorClient` exposes a `Neo4jClient`-equivalent surface for graph reads and writes. symbol: `gobby.memory.falkor_client.FalkorClient`.
- 1.2.2 — `ensure_vector_index` requires `dimension` as a positional kwarg sourced from `EmbeddingsConfig.dim`; the 1536 default footgun is removed. symbol: `gobby.memory.falkor_client.FalkorClient.ensure_vector_index`.

### 1.3 Delete neo4j_client.py and rewrite remaining Neo4j-named imports [category: refactor] (depends: 1.2, Phase 2)

`kind: deliverable`

Targets: `src/gobby/memory/neo4j_client.py` (delete), `src/gobby/memory/manager.py` (remove the live `from gobby.memory.neo4j_client import Neo4jClient` import at line 12 and any remaining `Neo4jClient` references), `src/gobby/memory/services/knowledge_graph.py` (remove the live `from gobby.memory.neo4j_client import Neo4jConnectionError` at line 19 and the TYPE_CHECKING `from gobby.memory.neo4j_client import Neo4jClient` at line 25; route to the `FalkorClient` / `FalkorConnectionError` surface from § 1.2)

Delete `src/gobby/memory/neo4j_client.py` outright.

The `src/gobby/memory/` package has no package-init file today (memory is a namespace package — verified by `ls src/gobby/memory/`), so there are no package-level re-exports to rewrite. Every consumer of `Neo4jClient` / `Neo4jConnectionError` imports from `gobby.memory.neo4j_client` directly — verified via `grep -rn 'from gobby.memory.neo4j_client' src/gobby/` showing exactly two consumer files: `src/gobby/memory/manager.py` (line 12) and `src/gobby/memory/services/knowledge_graph.py` (lines 19 + 25). These two import sites must be rewritten to use the FalkorClient surface from § 1.2.

This task depends on Phase 2 because `KnowledgeGraphService` and `CodeGraph` still import from `neo4j_client` until that phase finishes the wire-up. Run last in this ordering — after § 2.1 has switched `MemoryManager` to take the FalkorDB-shaped kwargs and § 2.2 has switched `CodeGraph` to a `FalkorClient`.

**Acceptance:**

- 1.3.1 — `src/gobby/memory/neo4j_client.py` is deleted. file: `src/gobby/memory/neo4j_client.py`.
- 1.3.2 — `src/gobby/memory/manager.py` no longer imports `Neo4jClient`. file: `src/gobby/memory/manager.py`.
- 1.3.3 — `src/gobby/memory/services/knowledge_graph.py` no longer imports `Neo4jClient` or `Neo4jConnectionError`. file: `src/gobby/memory/services/knowledge_graph.py`.

## P2 Phase 2: Python — Cypher Dialect Translation and Wire-Up

`kind: framing`

**Goal**: Translate every Cypher statement to FalkorDB dialect and wire the new client into `KnowledgeGraphService`, `MemoryManager`, `CodeGraph`, and `CodeIndexContext`.

### 2.1 Translate KnowledgeGraphService Cypher and wire MemoryManager [category: code] (depends: 1.2)

`kind: deliverable`

Targets: `src/gobby/memory/services/knowledge_graph.py`, `src/gobby/memory/falkor_client.py` (R39-F2 — schema/vector helpers own the dialect-table row 1-3 acceptance items), `src/gobby/memory/manager.py`, `src/gobby/memory/services/search.py` (clear_graph_clients touches `_search_service._kg_service` — R28-F4), `src/gobby/memory/services/indexing.py` (clear_graph_clients touches `_indexing_service._kg_service` — R28-F4), `src/gobby/runner_init/services.py` (the actual call site that constructs `MemoryManager` with the Neo4j-shaped kwargs), `runner_init/services.py` (bare path cited in body), `neo4j_client.py` (transport reference cited in body), `tests/memory/test_falkor_client.py`, `tests/memory/test_manager.py` (clear_graph_clients unit test — R28-F4)

Apply these dialect translations to every Cypher string in `src/gobby/memory/services/knowledge_graph.py` and `src/gobby/memory/falkor_client.py`'s schema/vector helpers:

| Neo4j construct | FalkorDB equivalent |
| --- | --- |
| `CREATE CONSTRAINT name IF NOT EXISTS FOR (n:Label) REQUIRE n.prop IS UNIQUE` | **Two-step required:** (1) `ensure_supporting_index(label, prop)` first — FalkorDB constraints require a backing exact-match index. (2) `GRAPH.CONSTRAINT CREATE <graph_name> UNIQUE NODE Label PROPERTIES 1 prop` as a Redis command (not via `query()`). (3) **Poll `db.constraints()` until status is `OPERATIONAL`** with a 30s timeout. Status sequence: `PENDING` → `UNDER CONSTRUCTION` → `OPERATIONAL` (or `FAILED` if pre-existing data violates uniqueness). Swallow "constraint already exists" only when the polled status is `OPERATIONAL`; raise on `FAILED`. |
| `CREATE INDEX name IF NOT EXISTS FOR (n:Label) ON (n.a, n.b)` | `CREATE INDEX FOR (n:Label) ON (n.a, n.b)` (no `IF NOT EXISTS`; catch "already indexed" errors) |
| `CREATE VECTOR INDEX name IF NOT EXISTS FOR (n:Label) ON (n.embedding) OPTIONS {indexConfig: {…}}` | `CREATE VECTOR INDEX FOR (n:Label) ON (n.embedding) OPTIONS {dimension: $dim, similarityFunction: 'cosine'}` |
| `CALL db.create.setNodeVectorProperty(n, 'embedding', $emb)` | `SET n.embedding = vecf32($emb)` (drop the procedure call entirely) |
| `CALL db.index.vector.queryNodes('idx_name', $k, $emb) YIELD node, score` | `CALL db.idx.vector.queryNodes('Label', 'embedding', $k, vecf32($emb)) YIELD node, score` (signature: label + property, not index name) |
| `datetime()` | `timestamp()` (returns Unix epoch ms; downstream code that read these as ISO strings must be updated to handle integers) |
| `apoc.*` procedures | None used in current code — verified clean |

Specific Cypher rewrites in `src/gobby/memory/services/knowledge_graph.py`:

- `_delete_outdated_relations` (≈line 447) — pure MATCH/DELETE, no dialect change beyond `datetime()` if present
- `_fetch_existing_relations` (≈line 508) — no change
- `_link_entities_to_memory` (≈line 521) — change `datetime()` → `timestamp()` in MERGE ... ON CREATE/MATCH
- `remove_memory_from_graph`, `remove_memories_from_graph`, `get_all_memory_node_ids`, `remove_orphaned_entities`, `clear_graph` (≈lines 544-660) — pure DETACH DELETE / counts; no dialect change
- `_link_entities_to_code` (≈line 725) — change `datetime()` → `timestamp()`
- `find_related_memory_ids` (≈line 868) — variable-length path traversal; FalkorDB supports `[*1..N]` up to depth 5, current clamp is already ≤3
- `search_graph` substring fallback (≈line 948) — `toLower` and `CONTAINS` both supported; no change

**Source-edit changes in `src/gobby/memory/services/knowledge_graph.py` (R22-F3 — these are NOT Cypher rewrites; they update Python imports and call sites that today reference Neo4j surfaces):**

- Replace the Neo4j exception import with the FalkorDB equivalents: `from gobby.memory.neo4j_client import Neo4jConnectionError, Neo4jQueryError` → `from gobby.memory.falkor_client import FalkorConnectionError, FalkorQueryError`. Update every `except Neo4jConnectionError` to `except FalkorConnectionError` and every `except Neo4jQueryError` to `except FalkorQueryError` inside this file. Without this edit, `knowledge_graph.py` fails to import the moment § 1.3 deletes `neo4j_client.py` — and the broad residual sweep in § 4.3 fires too late to save Phase 2 from an intermediate import-time crash.
- Update the `_ensure_vector_index` call site (today calls `await self._neo4j.ensure_vector_index(dimensions=self._embedding_dim)` — plural kwarg). Rename the kwarg to singular `dimension=` to match § 1.2's `FalkorClient.ensure_vector_index(dimension: int, ...)` signature. The rename must happen in the SAME task that does the `self._neo4j` → `self._falkor` attribute rename — otherwise the call hits a TypeError immediately on the first vector-index ensure pass at startup.

**Canonical activation predicate — owned by § 1.1 (R17-F5 — import only, do NOT redefine):**

`is_falkordb_enabled(databases: DatabasesConfig) -> bool` lives in `src/gobby/config/persistence.py` (added in § 1.1 alongside `DatabasesConfig`). Import it; do NOT define a local helper:

```python
from gobby.config.persistence import is_falkordb_enabled
```

Call shapes (the predicate accepts a `DatabasesConfig`, NOT the top-level config):

- `runner_init/services.py` already binds `db_cfg = runner.config.databases` (verified live at line 56 of `src/gobby/runner_init/services.py`) → call as `is_falkordb_enabled(db_cfg)`. Inside the body, read `db_cfg.falkordb.requirepass` — NOT `db_cfg.databases.falkordb.requirepass` (which would AttributeError because `db_cfg` is already the inner object).
- `_services_start` in `cli/daemon.py` holds a top-level `config: DaemonConfig` → call as `is_falkordb_enabled(config.databases)`.

Returns True only when the user has explicitly configured FalkorDB by seeding a non-empty `requirepass` value (default `FalkorConfig.requirepass = None` so the truthy check distinguishes installed-and-resolved from unconfigured). Default `host`/`port` values alone never enable the backend — that protects fresh installs and post-uninstall config from accidentally instantiating an unauthenticated FalkorClient or wiring CodeGraph against a missing service.

Every runtime gate in the daemon — `runner_init/services.py`'s `MemoryManager`/`CodeGraph` construction (2.1, 2.2), `_services_start`'s docker-compose profile selection (3.5), the admin payload's `configured` flag (4.1), and the daemon-wide sweep (4.3) — imports this same predicate from `gobby.config.persistence`. No local re-implementations; no wrapper modules.

`is_falkordb_installed()` (3.3) remains a separate, narrower function (did the installer ever write host/port keys?) for the specific question of whether `gobby status` should display install state — it intentionally returns True for an installed-but-unconfigured edge case so the operator sees the difference between not-installed and installed-but-credentials-missing.

The two predicates and where each is used:

- `is_falkordb_enabled(databases)` — runtime construction gate, imported from `gobby.config.persistence` (the daemon must NOT instantiate FalkorClient unless this is true). Argument is a `DatabasesConfig`, not a top-level config object.
- `is_falkordb_installed(db)` — status-payload-only (did the installer run?), drives `installed=true/false` in `gobby status` and `/api/admin/status`.

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

These kwargs are passed in by `src/gobby/runner_init/services.py` (the daemon startup wiring) which reads them off the loaded `DaemonConfig`. There is no top-level `config` object inside `MemoryManager` that carries `databases.falkordb.*`. Both files must move together — renaming the kwargs without updating the caller leaves a TypeError.

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
3. **`runner_init/services.py`:** the call site that constructs `MemoryManager(...)` already binds `db_cfg = runner.config.databases` (verified live at line 56 of `src/gobby/runner_init/services.py`) — so `db_cfg` is a `DatabasesConfig` instance, not a top-level config wrapper. Wrap the entire FalkorDB construction in `if is_falkordb_enabled(db_cfg):` (the canonical predicate defined at the top of this section — it accepts a `DatabasesConfig` directly); inside the branch, read `db_cfg.falkordb.{host, port, requirepass, graph_name, graph_search, graph_min_score, rrf_k}` (NOT `db_cfg.databases.falkordb.*` — that would AttributeError because `db_cfg` is already the inner object) and pass under the new kwarg names. **Note `requirepass`, not `password`** — the FalkorConfig field name from 1.1. When `is_falkordb_enabled(db_cfg)` is False, `MemoryManager` is constructed with `falkordb_host=None` (or whatever sentinel matches the existing "no graph backend" path) so `KnowledgeGraphService` and `CodeGraph` are not wired in. The same predicate gates the `CodeGraph(falkor_client=...)` construction in 2.2.
4. **`MemoryManager.clear_graph_clients()` (R24-F2 + R28-F4 — new method on `MemoryManager` in manager.py):** add `def clear_graph_clients(self) -> None` that nulls EVERY graph-bearing reference the manager owns or has handed out. Verified live: `MemoryManager` constructs `KnowledgeGraphService` and then passes that same instance into `SearchService(kg_service=...)` and `IndexingService(kg_service=...)`; both downstream services keep their own `self._kg_service` attribute (`src/gobby/memory/services/search.py:48` and `src/gobby/memory/services/indexing.py:41`) and route graph search / graph indexing through it. Zeroing only `MemoryManager._falkor_client` and `MemoryManager._kg_service` leaves the downstream service references alive, so health-check failure would still drive graph-augmented search through a stale KG wrapper. The implementation must null the full set:
   - `self._falkor_client = None`
   - `self._kg_service = None`
   - `self._search_service._kg_service = None` (the manager owns the SearchService instance — direct attribute write is acceptable; do NOT introduce a new public setter on SearchService because the lifecycle hook is the sole legitimate caller and a public setter would invite drift)
   - `self._indexing_service._kg_service = None` (same as above on IndexingService)
   This is the single entry point for § 4.3's health-check failure path. Do NOT just zero out `_falkor_client` from the lifecycle code — that catches only the surface that `MemoryManager` exposes directly and misses the `_kg_service` reference plus the downstream SearchService / IndexingService references, producing the partial-degradation state R24-F2 / R28-F4 calls out. Idempotent: calling it twice or calling it when every reference is already None is a no-op.

   **Test (R28-F4 addendum to the R24-F2 coverage):** the `tests/test_runner_lifecycle.py` post-health-check-failure case (owned by § 4.3) MUST also assert `runner.memory_manager._search_service._kg_service is None` AND `runner.memory_manager._indexing_service._kg_service is None`, plus a focused unit test in `tests/memory/test_manager.py` that constructs a `MemoryManager` with non-None `_search_service` and `_indexing_service` (each carrying a `_kg_service` reference), calls `clear_graph_clients()`, and asserts every one of the four references is None.

In `KnowledgeGraphService.__init__`, rename the `neo4j_client` parameter to `falkor_client` and the `self._neo4j` attribute to `self._falkor`. Update every `self._neo4j.X(…)` call site within the service.

The `add_to_graph` method's dynamic multi-label MERGE (≈line 184): `MERGE (n:Capitalize(entity_type):_Entity {entity_key: $key})` — FalkorDB supports multi-label MERGE but the parser is strict about whitespace. Smoke-test against realistic entity-type inputs.

**Tests (R21-F1 + R23-F2) — owned by this task:**

- `tests/memory/test_knowledge_graph.py` — update every Cypher-string assertion to FalkorDB dialect; replace `Neo4jClient` fixtures with `FalkorClient` (or a shared fake from `tests/memory/test_falkor_client.py`); update vector-index + secret-name assertions per the § 2.1 translation table.
- `tests/memory/test_graph_search_integration.py` — update RRF-merge expectations to use FalkorDB graph + Qdrant vector results; rename any `neo4j` fixture identifiers; ensure `is_falkordb_enabled` gating is exercised end-to-end.
- `tests/memory/test_manager.py`, `tests/memory/test_manager_graph_search.py`, `tests/memory/test_manager_knowledge_graph_wiring.py` — update `MemoryManager.__init__` kwargs from `neo4j_*` to `falkordb_*` (host, port, password, graph_name, graph_search, graph_min_score, rrf_k); replace any `_neo4j_client` attribute checks with `_falkor_client`; update the `runner_init/services.py` wiring tests to assert the `is_falkordb_enabled(db_cfg)` gate is checked AND that the daemon imports the predicate from `gobby.config.persistence` (R17-F5) rather than redefining locally.
- `tests/memory/test_dedup_wiring.py` (R23-F2: line 18 sets `config.neo4j_url = None` on the dedup-wiring fixture). Update to set `config.falkordb_host = None` or remove the legacy attribute entirely. The dedup tests don't exercise graph queries directly, but they build the manager surface — a stale attribute alone breaks pytest collection once `MemoryManager` no longer accepts `neo4j_url`.
- `tests/memory/test_vectorstore_init.py` (R23-F2: line 63 sets `config.neo4j_url = None` on the manager-init fixture) — switch to the new `falkordb_*` config attributes; verify `is_falkordb_enabled(db_cfg)` False keeps `_falkor_client is None` AND the vectorstore still initializes successfully.

These updates run as part of this task — the `neo4j_*` kwargs and `_neo4j_client` attribute disappear here, so leaving the tests stale would block § 8.3 row #4 immediately.

**Acceptance:**

- 2.1.1 — `KnowledgeGraphService` Cypher dialect is FalkorDB-compatible. symbol: `gobby.memory.services.knowledge_graph.KnowledgeGraphService`.
- 2.1.2 — `MemoryManager` constructs `KnowledgeGraphService` against `FalkorClient`. symbol: `gobby.memory.manager.MemoryManager`.
- 2.1.3 — Dialect-table row 1 (R39-F2): `CREATE CONSTRAINT ... IS UNIQUE` is replaced by the two-step `ensure_supporting_index(label, prop)` + `GRAPH.CONSTRAINT CREATE` Redis-command flow with `db.constraints()` polling to `OPERATIONAL` (30s timeout). file: `src/gobby/memory/falkor_client.py`.
- 2.1.4 — Dialect-table row 2: `CREATE INDEX ... IF NOT EXISTS` rewritten to `CREATE INDEX ... ON (...)` with "already indexed" error catch. file: `src/gobby/memory/falkor_client.py`.
- 2.1.5 — Dialect-table row 3: vector-index DDL rewritten to `CREATE VECTOR INDEX FOR (n:Label) ON (n.embedding) OPTIONS {dimension: $dim, similarityFunction: 'cosine'}` with no 1536 default. file: `src/gobby/memory/falkor_client.py`.
- 2.1.6 — Dialect-table row 4: `db.create.setNodeVectorProperty(...)` procedure call replaced by inline `SET n.embedding = vecf32($emb)`. file: `src/gobby/memory/services/knowledge_graph.py`.
- 2.1.7 — Dialect-table row 5: vector-query `CALL db.idx.vector.queryNodes('Label', 'embedding', $k, vecf32($emb)) YIELD node, score` (label + property signature, not index name). file: `src/gobby/memory/services/knowledge_graph.py`.
- 2.1.8 — Dialect-table row 6: every `datetime()` call site rewritten to `timestamp()` (Unix epoch ms); downstream readers updated to handle integers. file: `src/gobby/memory/services/knowledge_graph.py`.
- 2.1.9 — Dialect-table row 7: `rg "apoc\."` against the daemon Python source returns zero hits AFTER this task closes (sweep verification — current code already has zero `apoc.*` references, but the row is owned here so a future code reintroduction does not silently bypass the FalkorDB-compat contract). behavior: "no `apoc.*` Cypher fragments remain in daemon source" in `src/gobby/`.

### 2.2 Translate CodeGraph Cypher and wire CodeGraph construction at runner_init [category: code] (depends: 1.2, 2.1)

`kind: deliverable`

Target: `src/gobby/code_index/graph.py`, `src/gobby/runner_init/services.py` (where `CodeGraph(neo4j_client=...)` is actually constructed and injected into `CodeIndexContext`), `src/gobby/code_index/context.py` (only for the `CodeGraph | None` typing reference — the context itself does not instantiate the client)

**Self-contained context (R16-F2 — implementing agent receives only this section):**

This task runs AFTER § 2.1 (explicit dependency above). After § 2.1 closes, the following preconditions hold and this task's edits build on them:

- `is_falkordb_enabled(databases: DatabasesConfig) -> bool` exists in `src/gobby/config/persistence.py` (defined in § 1.1, R16-F4) and is the canonical activation predicate. Import it as `from gobby.config.persistence import is_falkordb_enabled`. Pass `databases` (a `DatabasesConfig`), NOT the top-level config — `db_cfg.falkordb.host` resolves; `config.databases.falkordb.host` would AttributeError if used by mistake (`config` does not have a top-level `falkordb` attribute, only `config.databases.falkordb`).
- `runner_init/services.py` already has the FalkorDB-gated MemoryManager construction in place (§ 2.1). The CodeGraph construction at the same startup site must follow the SAME gating pattern.
- `FalkorClient` (Python) is the `src/gobby/memory/falkor_client.py` module from § 1.2; constructor is `FalkorClient(host: str, port: int, password: str | None, graph_name: str)`.

**FalkorDB Cypher dialect translations (mirror § 2.1's table — applied verbatim to `src/gobby/code_index/graph.py`):**

- Vector index DDL (R17-F1 — aligns with § 2.1's canonical form): `CREATE VECTOR INDEX FOR (n:Label) ON (n.embedding) OPTIONS {dimension: <dim>, similarityFunction: 'cosine'}`. `dimension` is required (no 1536 default — pass `EmbeddingsConfig.dim` interpolated into the DDL or via a numeric param). Catch already-indexed / duplicate-index errors so re-runs are idempotent. DO NOT use `CALL db.idx.vector.createNodeIndex(...)` — that procedure-call form was a draft error; FalkorDB's canonical vector-index DDL is `CREATE VECTOR INDEX FOR ... OPTIONS {...}` per § 2.1's translation table.
- Constraints: FalkorDB does not support Neo4j's `CREATE CONSTRAINT ... IS UNIQUE` syntax; uniqueness is enforced via application-layer MERGE patterns. Drop constraint-creation Cypher; rely on existing MERGE-based upsert.
- `datetime()` Cypher function: not supported in FalkorDB. Replace with `timestamp()` (millisecond Unix epoch) at every call site, OR pass `time.time() * 1000` from the caller and interpolate.
- Variable-length paths (`[:CALLS*1..N]`) work in FalkorDB; depth interpolation pattern stays the same as in § 7.3.
- Label disjunction in WHERE (`WHERE target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol`) is supported.

Apply the dialect translations to every Cypher string in `src/gobby/code_index/graph.py`. The methods touching Cypher:

- `sync_file` (≈line 95) — bulk MERGE of nodes/relationships from a file's parsed AST; this is the primary write path triggered by gobby-cli's `/api/code-index/invalidate` POST
- `add_relationships` (≈line 258)
- `_ensure_node` (≈line 628) — node upsert helper
- `_cleanup_orphans` (≈line 64)
- `clear_project` (≈line 859), `delete_file` (≈line 874)
- `find_callers`, `find_usages`, `get_imports`, `get_import_chain`, `find_blast_radius`, `get_file_graph`, `get_file_symbols`, `get_symbol_neighbors`, `get_blast_radius_graph` — read methods that the daemon uses for HTTP route responses (not the Rust client)

Schema setup in CodeGraph likely creates indexes/constraints on `CodeSymbol.id`, `CodeFile.path`, etc. — apply the FalkorDB DDL dialect from the 2.1 table.

**Construction site — verified live, not assumed:** `src/gobby/code_index/context.py` does NOT construct the graph client; `CodeIndexContext.graph` is just a property returning `self._graph`. The actual `CodeGraph(neo4j_client=...)` call lives in `src/gobby/runner_init/services.py` (alongside the MemoryManager construction in 2.1) and the resulting instance is injected into `CodeIndexContext`. Make the runtime swap there:

1. **`src/gobby/runner_init/services.py`:** wrap the construction in `if is_falkordb_enabled(db_cfg):` (canonical predicate — see 2.1; `db_cfg` is the `runner.config.databases` binding from `runner_init/services.py:56`, a `DatabasesConfig` instance). Inside the branch, replace the `Neo4jClient(...)` + `CodeGraph(neo4j_client=client)` construction with `FalkorClient(host=db_cfg.falkordb.host, port=db_cfg.falkordb.port, password=db_cfg.falkordb.requirepass, graph_name="gobby_code")` + `CodeGraph(falkor_client=client)` (NOT `db_cfg.databases.falkordb.*` — that would AttributeError; see 2.1's call-site note). When the predicate is False, leave `CodeIndexContext._graph = None` so reads degrade gracefully (matches the existing "no graph" path). Note the **different `graph_name`** — code graph (`gobby_code`) and memory KG (`gobby_kg`) are separate FalkorDB graphs in the same instance.
2. **`src/gobby/code_index/graph.py` (`CodeGraph.__init__`):** rename the `neo4j_client` constructor parameter to `falkor_client` and update internal attributes (`self._neo4j` → `self._falkor`) and every internal use site.
3. **`src/gobby/code_index/context.py`:** if the typing imports reference `Neo4jClient`, update to `FalkorClient`. Do not change construction logic — there is none in this file. Add `async def close_graph_client(self) -> None` on `CodeIndexContext` that awaits `self._graph.close()` when `self._graph is not None`, then sets `self._graph = None` (idempotent — safe when graph is already None or already closed). § 4.3's shutdown ordering (R23-F1) calls this before `LocalDatabase.close()`. Also add `def clear_graph_client(self) -> None` (synchronous, no await) that ONLY sets `self._graph = None` so the health-check failure path can null the reference without an await context. **Naming caveat (R24-F1):** the live `CodeIndexContext` already exposes `async def clear_graph(self, project_id: str) -> dict[str, Any]` — used by `/api/code-index/graph/clear` to clear and requeue the projection for one project. The lifecycle hook MUST be a distinctly named method (`clear_graph_client()` and `close_graph_client()` were chosen for that reason); reusing `clear_graph` would either silently overwrite the route handler or attribute-error at health-check time. Likewise, prefer the explicit `close_graph_client()` over a bare `close()` to keep the lifecycle surface unambiguous (CodeIndexContext is the daemon's code-index container; a plain `close()` could collide with future container-shutdown plumbing).
4. **`src/gobby/code_index/graph.py` (`CodeGraph.close()`):** add an `async def close(self) -> None` method that delegates to `self._falkor.close()` (the FalkorClient method from § 1.2 that closes the underlying redis-asyncio connection). This is the close hook that runner shutdown invokes via `CodeIndexContext.close()` — without it, daemon shutdown leaks the code-graph FalkorDB connection (R23-F1). The method is idempotent: closing twice or closing when `self._falkor is None` is a no-op (catch and swallow `RuntimeError` from already-closed redis pool too).

Verify `src/gobby/code_index/sync_worker.py` (the worker that consumes `/api/code-index/invalidate` POSTs from gobby-cli) only goes through `CodeGraph` — it should not hold a separate `Neo4jClient` reference. If it does, swap that too.

The blast-radius variable-length path query interpolates depth into the Cypher (depth clamped to 1-5); FalkorDB supports this pattern.

**Tests (R21-F1) — owned by this task:**

- `tests/code_index/test_graph.py` — update every Cypher-string assertion to FalkorDB dialect; replace `Neo4jClient` fixtures with `FalkorClient`; rename `CodeGraph(neo4j_client=...)` ctor calls to `CodeGraph(falkor_client=...)`; assert the `is_falkordb_enabled(db_cfg)` gate is what the runner_init code path checks (imported from `gobby.config.persistence`, NOT a local helper).

The CodeGraph constructor signature changes here, so the test must move with the source.

**Acceptance:**

- 2.2.1 — `CodeGraph` Cypher dialect is FalkorDB-compatible. symbol: `gobby.code_index.graph.CodeGraph`.
- 2.2.2 — Runner init constructs `CodeGraph` with `FalkorClient`. file: `src/gobby/runner_init/services.py`.

## P3 Phase 3: Python — Installer, Bootstrap, and CLI Flags

`kind: framing`

**Goal**: Replace the Neo4j installer with a Docker-only FalkorDB installer; rename CLI flags; migrate bootstrap and config_store keys.

### 3.1 Implement FalkorDB installer (Docker-only) [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/cli/installers/falkor.py` (new file, modeled on `src/gobby/cli/installers/neo4j.py`); `src/gobby/cli/installers/__init__.py` (R20-F2 + R32-F1 — ADD `install_falkordb` / `uninstall_falkordb` exports ONLY; DO NOT remove `install_neo4j` / `uninstall_neo4j` from `__init__.py` here. The removal of the Neo4j exports plus the deletion of `installers/neo4j.py` is owned exclusively by § 3.4 (which renames the live callsites in `cli/install.py` and `cli/_install_prompts.py` before the file deletion). Removing them in § 3.1 — before § 3.4 has rewired the callsites — would leave the old callsites importing missing names mid-Phase-3 and break the test suite between § 3.1 closing and § 3.4 closing); `.gobby/bootstrap.yaml`, `~/.gobby/bootstrap.yaml` (bootstrap-password seed file written by `_write_bootstrap_password`); `docker-compose.yml` (compose file at `<services_dir>/docker-compose.yml` referenced by the R31-F2 anchored compose-down args)

Create `src/gobby/cli/installers/falkor.py` exposing two public functions: `install_falkordb(*, password: str | None, gobby_home: Path | None = None) -> dict[str, Any]` and `uninstall_falkordb(*, gobby_home: Path | None = None, purge: bool = False) -> dict[str, Any]`. Mirror the existing Neo4j installer's overall shape (compose-yaml ensure, subprocess invocation, config update, health wait).

**`gobby_home` normalization contract (R28-F5):** at the top of every public entry point (`install_falkordb`, `uninstall_falkordb`, `_resolve_falkordb_password`), normalize the parameter exactly once:

```python
from gobby.cli.utils import get_gobby_home
home: Path = gobby_home if gobby_home is not None else get_gobby_home()
```

From that point on, **every internal helper receives `home: Path` as a non-Optional argument**, NOT `gobby_home: Path | None`. `_update_config(*, host, port, password, gobby_home: Path)` and `is_falkordb_installed(*, db, gobby_home: Path | None = None)` keep their parameter names, but the call sites inside `install_falkordb` / `uninstall_falkordb` always pass the normalized `home` value, never `None`. This avoids a `TypeError` if a future contributor passes `None` through.

**DB-path resolution when `bootstrap.yaml` does not yet exist (R28-F5 — the load-bearing case):** live `load_bootstrap(path)` (verified at `src/gobby/config/bootstrap.py:50-76`) returns `BootstrapConfig()` defaults when the file is missing — and the default `BootstrapConfig.database_path` resolves to the operator's real `~/.gobby/gobby-hub.db`, NOT a tmp home. That means a naive `load_bootstrap(home / "bootstrap.yaml")` would silently route a test-fixture install to the user's production DB on a fresh `home` directory. The contract for the Falkor installer is therefore:

- **If `home / "bootstrap.yaml"` exists,** call `load_bootstrap(str(home / "bootstrap.yaml"))` and resolve the DB from `Path(bootstrap.database_path).expanduser()`. The Pydantic default would also be returned for callers that didn't seed bootstrap, but the explicit-load path is correct when the file is present.
- **If `home / "bootstrap.yaml"` does NOT exist,** resolve the DB from `home / "gobby-hub.db"` directly — do NOT call `load_bootstrap()` because that would return the production default. This matches the policy that `gobby_home` is the authoritative root: when no bootstrap seed exists, the DB lives next to the home, not in `~/.gobby/`.

Encapsulate this rule in a helper inside the installer module (or as a private helper in `src/gobby/cli/utils.py` if it ends up being reused by `_services_start`, but DO NOT introduce `_default_db_path()` in `src/gobby/cli/services.py` per R4-F1's pinned strategy):

```python
def _resolve_falkordb_db_path(home: Path) -> Path:
    bootstrap_file = home / "bootstrap.yaml"
    if bootstrap_file.exists():
        from gobby.config.bootstrap import load_bootstrap
        bootstrap = load_bootstrap(str(bootstrap_file))
        return Path(bootstrap.database_path).expanduser()
    return home / "gobby-hub.db"
```

`_update_config(host, port, password, gobby_home=home)` calls `_resolve_falkordb_db_path(gobby_home)` for the LocalDatabase. `is_falkordb_installed(gobby_home=home)` does the same. `uninstall_falkordb(gobby_home=home)` uses the resolved DB path for its config_store / secrets cleanup.

**Docker-only decision (in scope for 0.4.0):** FalkorDB does not ship a Homebrew formula and the GitHub releases ship raw `.so` Redis modules that require manual `redis-server` setup. A reliable native-install path requires either Homebrew tap upstream work or a custom `.so`-download installer — both are non-trivial and out of scope for this migration. 0.4.0 ships **Docker-only** for FalkorDB (parity with the current Neo4j experience, which is also Docker-only). A native local mode is a follow-up release item.

**Password resolution (mirrors `_resolve_neo4j_password` in `src/gobby/cli/installers/neo4j.py`):**

Add a `_resolve_falkordb_password(password: str | None, *, gobby_home: Path | None = None) -> ResolvedFalkorPassword` helper. The structured return is load-bearing — a string-only return CANNOT carry the `password_source` signal that the result contract above requires (R14-F5):

```python
from typing import Literal, NamedTuple

class ResolvedFalkorPassword(NamedTuple):
    value: str
    source: Literal["generated", "provided", "reused"]
```

`gobby_home` defaults to the standard `~/.gobby` resolution but is overridable for tests and non-default homes — the helper uses it to construct the `LocalDatabase` + `SecretStore` for the existing-secret read in step 2. Bootstrap is **not** in this chain (it is a write target, not a read source — see "Bootstrap split-brain fix" below). Precedence:

1. **Explicit `password` argument** (passed in from `--falkordb-password` or the wizard's `[p]` path) → `source='provided'`
2. **Existing config_store secret** at `databases.falkordb.requirepass` (read via `SecretStore` constructed from `gobby_home`); if present, reuse the same value so re-running `gobby install --falkordb` is idempotent and does not lock the user out of an existing data dir → `source='reused'`
3. **Generated value** — `secrets.token_urlsafe(24)` if neither of the above is set → `source='generated'`

**Charset validation (R25-F1 + R27-F1 — owned executable instructions):** before returning the resolved value, call `validate_falkordb_password(resolved_value)` (defined in `src/gobby/config/persistence.py`, see § 1.1). Import as `from gobby.config.persistence import validate_falkordb_password`. Apply to ALL three precedence branches:

- **Provided** path: a `ValueError` from the validator surfaces as a `click.UsageError` from `install` whose message is the validator's exception text — non-zero exit, stderr, no Docker container started, no config_store / bootstrap writes (verified by inspecting both stores after the failed call). The CLI integration test in `tests/cli/test_cli_install.py` (owned by § 8.1) MUST cover at least one rejected sample (e.g. `gobby install --falkordb-password "has space"` exits 1, prints the "must not contain whitespace" message, leaves config_store unchanged).
- **Reused** path: by definition the value already passed validation when first persisted (the field_validator on `FalkorConfig.requirepass` ran on the original write), so the call is essentially a defense-in-depth re-check. If it does fail (e.g. a hand-edited DB), abort the install with the same operator-actionable error rather than reusing a now-invalid value.
- **Generated** path: `secrets.token_urlsafe(24)` produces URL-safe base64 (alphabet `[A-Za-z0-9_-]`), which always passes the validator by construction. The call is a smoke check; add a unit test that generates 100 passwords and asserts every one validates clean (catches a future drift in the generator that drops outside the validator's accepted charset).

`install_falkordb` consumes the structured return verbatim into the result dict per the contract above:

```python
result["password_source"] = resolved.source
result["password"] = resolved.value if resolved.source == "generated" else None
```

**Reused-path test (R14-F5):** call `install_falkordb(gobby_home=<tmp>)` against a tmp DB seeded with `databases.falkordb.requirepass = '$secret:requirepass'` and the matching encrypted `secrets` row. Assert `result['password_source'] == 'reused'` AND `result['password'] is None`. Validates the `gobby_home` override path AND the reuse semantics in one fixture.

After resolution, `install_falkordb` writes the resolved value to **both** stores. **Persistence ordering is fixed and matches the ordered Docker install steps below — credential writes happen AFTER the health check passes, not before:**

1. `_update_config(...)` — encrypts the value into the `secrets` table, references it from `config_store` under `databases.falkordb.requirepass` (step 5 in the install sequence below)
2. `_write_bootstrap_password(resolved, gobby_home)` — persists into `~/.gobby/bootstrap.yaml` under `falkordb_password` so `_services_start` (3.5) can read it on `gobby start` for the docker-compose env injection (step 6 in the install sequence below)

**Failure semantics — pinned per step (do NOT copy the live Neo4j installer's `bootstrap_ok` warning-on-failure pattern):**

The current `install_neo4j` at `src/gobby/cli/installers/neo4j.py:153-165` writes config first, then runs `bootstrap_ok = _write_bootstrap_password(...)` and **returns `success: True` even when `bootstrap_ok is False`** — only attaching a `warning` field. That pattern is the exact split-brain this section is designed to prevent: config_store would hold the new `databases.falkordb.requirepass` secret while `~/.gobby/bootstrap.yaml` lacks `falkordb_password`, so the next `gobby start` would inject the stale-or-default bootstrap password into the docker-compose env while daemon clients authenticate with the config_store secret. Different passwords on the same running service.

The FalkorDB installer must NOT inherit that pattern. Failure semantics for each ordered step:

- **Steps 1-4 (Docker check, compose refresh, `compose up`, health check):** if any fails, neither persistence write happens — the resolved password lives only in memory and is discarded. The user retries `gobby install --falkordb` from scratch; password resolution starts over (existing config_store secret is still empty, generates a fresh value if no flag passed). This avoids the half-installed-with-credentials state where a future install picks up a stale secret pointing to a service that never came up.
- **Step 5 (`_update_config`):** if this fails (e.g., disk full mid-encrypt), the running container is left up but `_write_bootstrap_password` does NOT execute. The install returns `{"success": False, "error": "Failed to persist FalkorDB credentials to config_store; run 'gobby uninstall --falkordb' to clean up the running container, then retry."}`. The operator must run uninstall (which is now safe because steps 5-6 wrote nothing) before retrying.
- **Step 6 (`_write_bootstrap_password`):** if this fails after step 5 succeeded, the install MUST NOT report success — that is the split-brain. One acceptable behavior must be chosen and applied consistently:
  - **(A) Fail loudly with cleanup instruction** (recommended; matches step 5 semantics): return `{"success": False, "error": "FalkorDB is running and credentials are persisted to config_store, but the bootstrap.yaml write failed. Run 'gobby uninstall --falkordb' to roll back the container + config_store, then retry.", "compose_running": True}`. Operator runs uninstall + retry. The `compose_running: True` flag is for the wizard/CLI to surface the "container is up but uninstall is required" state clearly.

**Validation matrix coverage:** Phase 8.3 must include a row that exercises the step-6-failure path explicitly — e.g., make `~/.gobby/bootstrap.yaml` read-only after the installer launches the container, then verify the install returns `success: False` (NOT `success: True` with a warning) AND the operator-facing error message names `gobby uninstall --falkordb` as the cleanup verb.

**Bootstrap split-brain fix (R5-F1):** the bootstrap file is a *write target* of the installer (so the daemon's `_services_start` can find a password on cold start), but is **never read** by the installer's password-resolution chain. This means `uninstall_falkordb` must clear `falkordb_password` from `~/.gobby/bootstrap.yaml` alongside the config_store entries — otherwise a subsequent install picks up a generated value (because config_store is empty), but the daemon picks up the old stale bootstrap value, and the docker-compose container comes up authenticating against the new value while `_services_start`'s env injection uses the old. They desynchronize silently.

**Installer result contract (R13-F3):** the installer returns a result dict with the following load-bearing fields. `_run_falkordb_install` (Phase 3.4) consumes this contract verbatim — pin the schema here so a future contributor cannot accidentally swap one disclosure path for another:

```python
{
    "success": bool,
    "password_source": Literal["generated", "provided", "reused"],
    # Populated ONLY when password_source == "generated"; None on "provided" and
    # "reused" so secrets supplied by the operator (CLI flag) or pulled from
    # existing config_store are never echoed back to the terminal.
    "password": str | None,
    # Browser URL surfaced on success (consumed in 3.4; do not hardcode the literal in two places).
    "browser_url": str,
    # Failure-path fields per the per-step semantics above
    "error": str | None,
    "compose_running": bool,  # True iff step 5 wrote credentials but step 6 failed
}
```

`password_source` is the load-bearing signal `_run_falkordb_install` uses to decide whether to echo the password — DO NOT let the consumer infer it by checking whether `password` is non-None. The installer's resolution chain can pick a value from any source; the contract is that `password` is `None` on the `provided` and `reused` paths.

**Test coverage (R13-F3):** add three focused installer tests, one per `password_source` enum value, asserting the exact result-dict shape:

- `generated` — no `--falkordb-password` flag, no existing config_store secret → `password_source == "generated"` AND `password` is the generated string
- `provided` — `--falkordb-password foo` passed in → `password_source == "provided"` AND `password is None`
- `reused` — existing config_store secret present, no flag → `password_source == "reused"` AND `password is None`

**Docker install steps:**

1. Check Docker is available; abort with `Docker not found. Install Docker to use FalkorDB.` (mirrors the existing Neo4j installer's Docker check verbatim)
2. **Refresh the compose file** (NOT just "ensure exists" — this catches the existing-install upgrade path): unconditionally overwrite `~/.gobby/services/docker-compose.yml` from the bundled template. The current `_ensure_unified_compose` in `src/gobby/cli/installers/qdrant.py` is a `if not dest.exists(): copy` — that helper would leave a stale Neo4j-era compose file in place on upgrade, so `docker compose --profile falkordb up` would find no `falkordb` profile and silently fail. Add a sibling helper `_refresh_unified_compose(services_dir)` that always copies, AND first stops any running profiles from the old file (R31-F2 — anchor the cleanup to the OLD compose file path explicitly: `subprocess.run(["docker", "compose", "-f", str(old_compose_file), "--profile", "neo4j", "down"], cwd=str(services_dir), check=False)` — do NOT use bare `docker compose --profile neo4j down`, which depends on CWD-relative discovery of `docker-compose.yml` and silently no-ops when the installer is invoked from any other directory) so the upgrade does not orphan the Neo4j container.
3. Run `subprocess.run(["docker", "compose", "-f", str(compose_file), "--profile", "falkordb", "up", "-d", "--remove-orphans"], cwd=str(services_dir), env={**os.environ, "GOBBY_FALKORDB_PASSWORD": resolved.value}, check=True, capture_output=True, text=True)` (R33-F1 — same anchoring policy as the cleanup paths in step 2 and the uninstall step: bare `docker compose --profile falkordb up -d` would depend on CWD-relative compose discovery and silently target the wrong compose project or fail to find the refreshed unified compose file when the installer is invoked from any directory other than `~/.gobby/services/` — e.g., from `/tmp` in § 8.3 row 18). The compose template (Phase 3.2) maps the `GOBBY_FALKORDB_PASSWORD` env into `REDIS_ARGS="--requirepass $GOBBY_FALKORDB_PASSWORD"` for the container — Redis auth lives on `REDIS_ARGS`, not `FALKORDB_ARGS` (the latter is reserved for FalkorDB module options like `MAX_QUEUED_QUERIES`).
4. Health check: pin the executable command shape so password resolution is unambiguous —
   `subprocess.run(["docker", "compose", "-f", str(compose_file), "exec", "-T", "falkordb", "redis-cli", "-a", resolved.value, "PING"], cwd=str(services_dir), capture_output=True, text=True, timeout=5)` must return stdout `PONG` (poll up to 60s; treat `NOAUTH`/`WRONGPASS` in stderr as a hard failure, not a transient health miss). On failure, return early — do NOT proceed to credential persistence (steps 5-6).
   **Why list-style with `resolved.value` directly (R33-F2):** an earlier draft wrote `redis-cli -a "$GOBBY_FALKORDB_PASSWORD"` here, but the installer drives this through `subprocess.run(..., capture_output=True, ...)` with NO shell. `subprocess` does not perform `$VAR` expansion, so the literal eight-character string `$GOBBY_FALKORDB_PASSWORD` would be sent to redis-cli as the password — health check fails even when the container is healthy. Passing `resolved.value` as a list element makes the substitution unambiguous AND keeps the password out of any logged shell command. If a future contributor needs container-side env expansion specifically, the correct form is `["docker", "compose", "-f", str(compose_file), "exec", "-T", "falkordb", "sh", "-lc", 'redis-cli -a "$GOBBY_FALKORDB_PASSWORD" PING']` — note the explicit `sh -lc` shell invocation — but that is unnecessary here because `resolved.value` is already in scope.
   **Why `-T` is required:** `docker compose exec` allocates a TTY by default, and the installer runs this through `subprocess.run(..., capture_output=True, ...)` which has no TTY — without `-T`, exec fails with `the input device is not a TTY` before Redis is even pinged, making a healthy container look like a failed install.
   **Logging caveat:** never echo the executed command list verbatim to stdout/stderr (it contains the resolved password). If install logs need a redacted command for operator debugging, emit it as `docker compose -f <compose_file> exec -T falkordb redis-cli -a <redacted> PING`.
   Same `-f <compose_file>` + `cwd=str(services_dir)` anchoring as steps 2-3 so the exec hits the unified compose at `~/.gobby/services/docker-compose.yml`, not whatever might exist in the daemon's CWD.
5. `_update_config(host="127.0.0.1", port=16379, password=<resolved_password>)` — port 16379 is the host-side mapping from the compose template (avoids collision with system Redis on 6379). On failure, return early — do NOT proceed to step 6 (the operator must `gobby uninstall --falkordb` to clean up the running container before retry).
6. `_write_bootstrap_password(<resolved_password>, gobby_home)` — only after `_update_config` succeeds. Persists into `~/.gobby/bootstrap.yaml` under `falkordb_password` so `_services_start` (3.5) can find the value for compose-env injection on subsequent `gobby start` invocations.

**FalkorDB Browser:** the official `falkordb/falkordb` image bundles the browser on container port 3000. The compose template (3.2) maps it to host port 13000. The success message surfaces `http://localhost:13000` as the browser URL.

`_update_config` writes the persisted state:

```python
def _update_config(*, host: str, port: int, password: str, gobby_home: Path) -> None:
    # R19-F5: host, port, and the requirepass secret MUST persist atomically.
    # Without `db.transaction()`, each `store.set(...)` autocommits — a failure
    # in `store.set_secret(...)` after host/port have been written leaves
    # `databases.falkordb.host`/`port` in config_store while the secret is
    # absent, making `is_falkordb_installed()` return True for an install that
    # cannot authenticate. The transaction wraps all three writes so a failure
    # rolls back the whole _update_config step (matches the per-step failure
    # semantics above: step 5 failure means NO credentials are persisted).
    #
    # gobby_home threading (R28-F1): the DB path comes from
    # `bootstrap.database_path` resolved relative to the caller's gobby_home, NOT
    # from a default `~/.gobby/` lookup. Without this, `install_falkordb(gobby_home=<tmp>)`
    # would write to the operator's real DB instead of the test tmpdir. There is no
    # `_default_db_path()` helper in `src/gobby/cli/services.py` today; do not
    # introduce one — flow the path explicitly through the installer call chain.
    db_path = _resolve_falkordb_db_path(gobby_home)  # see the helper defined above
    db = LocalDatabase(db_path)
    store = ConfigStore(db)
    secret_store = SecretStore(db)
    with db.transaction():
        store.set("databases.falkordb.host", host, source="install")
        store.set("databases.falkordb.port", port, source="install")
        store.set_secret("databases.falkordb.requirepass", password, secret_store, source="install")
```

After the entry-point normalization (`home = gobby_home if gobby_home is not None else get_gobby_home()`), `install_falkordb` calls every internal helper with the normalized `home` value, NOT the original `gobby_home` parameter — so a future contributor cannot reintroduce a `None` value mid-chain. Concretely:

- `resolved = _resolve_falkordb_password(password, gobby_home=home)` (step 2 of password resolution; the helper consumes `home` for the existing-secret read via the same `_resolve_falkordb_db_path(home)`).
- `_update_config(host=..., port=..., password=resolved.value, gobby_home=home)` (step 5 of the install sequence).
- `_write_bootstrap_password(resolved.value, home)` (step 6).
- `is_falkordb_installed(gobby_home=home)` for any post-install status check the installer drives.

`uninstall_falkordb` performs the same normalization at its entry point and threads `home` through `_resolve_falkordb_db_path(home)` → LocalDatabase → `ConfigStore.clear_secret(...)` → `DELETE FROM config_store WHERE key IN (...)` plus the `home / "bootstrap.yaml"` rewrite that removes `falkordb_password`. There is no callsite — public or internal — that passes the raw `gobby_home: Path | None` parameter forward; `None` is normalized exactly once at the public boundary.

The `databases.falkordb.mode` key is **dropped** — there is only one mode (Docker), so no routing needed. `is_falkordb_installed` (3.3) keys off the presence of the host/port keys instead.

**Uninstall** (`uninstall_falkordb`):

1. Run `subprocess.run(["docker", "compose", "-f", str(compose_file), "--profile", "falkordb", "down"], cwd=str(services_dir), check=False)` (R31-F2 — pin `-f <compose_file>` and `cwd=str(services_dir)` explicitly; bare `docker compose --profile falkordb down` depends on CWD-relative compose discovery and would silently leave the container running when uninstall is invoked from any directory other than `~/.gobby/services/`). Append `"-v"` to the args only when the operator passes `--purge` to drop the data volume.
2. Clear ONLY the connection/auth keys — preserve the user-tuned behavior keys (`graph_search`, `graph_min_score`, `rrf_k`, `graph_name`) so they survive a reinstall:
   - `ConfigStore.clear_secret("databases.falkordb.requirepass", secret_store)` for the secret
   - Delete only `host`, `port` (and any other strictly connection-level keys): `DELETE FROM config_store WHERE key IN ('databases.falkordb.host', 'databases.falkordb.port')`
   - Do NOT issue a blanket `DELETE WHERE key LIKE 'databases.falkordb.%'` — that would silently clobber user-tuned behavior keys (matches the migration policy in 3.6)
3. **Clear `falkordb_password` from `~/.gobby/bootstrap.yaml`** — load the YAML, pop the key if present, write back (preserving every other key). This closes the bootstrap split-brain identified in R5-F1.

**Acceptance:**

- 3.1.1 — Installer wires the FalkorDB Docker setup end-to-end (image pull, compose profile, healthcheck). file: `src/gobby/cli/installers/falkor.py`.
- 3.1.2 — Installer writes `falkordb_password` to `bootstrap.yaml` and `databases.falkordb.requirepass` to config_store atomically. behavior: "installer writes password to both bootstrap and config_store" in `src/gobby/cli/installers/falkor.py`.
- 3.1.3 — `install_falkordb(gobby_home=<tmp>)` writes host/port/secret to a DB resolved via `_resolve_falkordb_db_path(home)` — `home / "gobby-hub.db"` when no `bootstrap.yaml` exists in the tmp home, or `Path(bootstrap.database_path).expanduser()` when one is present — rather than the operator's default `~/.gobby/` DB. `install_falkordb(gobby_home=None)` normalizes to `get_gobby_home()` at the entry point so no internal helper ever receives `None` for the home value. behavior: "gobby_home parameter is normalized via get_gobby_home() and threaded as a non-Optional Path through `_resolve_falkordb_password`, `_resolve_falkordb_db_path`, `_update_config`, `is_falkordb_installed`, and uninstall cleanup; the no-bootstrap-yet path routes the DB to `home / 'gobby-hub.db'` rather than the production default" in `src/gobby/cli/installers/falkor.py`.

### 3.2 Replace neo4j service block in docker-compose.services.yml [category: config] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/data/docker-compose.services.yml`, `test_falkordb_installer.py` (test-file path cited in body)

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
    # R25-F1: quote the password expansion via YAML single-quoted scalar so the inner
    # double-quotes pass through to the shell verbatim. The shell then sees
    # redis-cli -a "$GOBBY_FALKORDB_PASSWORD" PING (one quoted argument); without the
    # inner quotes a whitespace-bearing password would word-split into multiple argv
    # entries and silently misauth. The validator in § 1.1 already rejects whitespace
    # + control chars, but the quoted form is defense-in-depth against future
    # relaxations and matches Docker's recommended pattern for any variable-substituted
    # argument.
    test:
      - CMD-SHELL
      - 'redis-cli -a "$$GOBBY_FALKORDB_PASSWORD" PING | grep -q PONG'
    interval: 10s
    timeout: 5s
    retries: 5
  restart: unless-stopped
  profiles: [falkordb, all]
```

In the `volumes:` section at the bottom of the file, remove `gobby_neo4j_data` and `gobby_neo4j_logs`, and add `gobby_falkordb_data:`.

The `neo4j` profile name is replaced by `falkordb`. The `all` profile remains so `docker compose --profile all up -d` brings up everything.

**Tests (R21-F1) — owned by this task:**

- `tests/cli/installers/test_qdrant_installer.py` — update the compose-template assertions that currently reference the `neo4j` service block, the `gobby_neo4j_data` / `gobby_neo4j_logs` volumes, and the `neo4j` profile name. Replace with the new `falkordb` service (image `falkordb/falkordb:latest`, ports `16379:6379` + `13000:3000`, REDIS_ARGS auth, `gobby_falkordb_data` volume, `falkordb` + `all` profiles). Rename or split into a sibling `test_falkordb_installer.py` if the assertions become unwieldy.

The compose template is rewritten here; the test must move with it.

**Acceptance:**

- 3.2.1 — `falkordb` service block replaces the prior `neo4j` block. file: `src/gobby/data/docker-compose.services.yml`.

### 3.3 Replace services.py status helpers with FalkorDB equivalents [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/cli/services.py`, `_health.py` (admin-health module touched by §3.3 wiring)

Replace `is_neo4j_installed`, `is_neo4j_healthy`, and `get_neo4j_status` (lines 100-153) with FalkorDB equivalents:

```python
def is_falkordb_installed(
    *,
    db: LocalDatabase | None = None,
    gobby_home: Path | None = None,
) -> bool:
    """True if the installer has recorded FalkorDB host/port in config_store.

    Source of truth: presence of `databases.falkordb.host` AND `databases.falkordb.port`
    keys in config_store. The installer (3.1) writes both during _update_config; the
    uninstaller (3.1) clears the connection keys (`host`, `port`) and the secret
    (`requirepass`) via targeted DELETE on those specific keys, preserving user-tuned
    behavior tunables (`graph_search`, `graph_min_score`, `rrf_k`, `graph_name`) — see
    § 3.1's uninstall step 2 and § 8.3 row #17. (R16-F5 corrected the prior DELETE-WHERE-LIKE
    wording, which contradicted the explicit tunable-preservation policy.)
    No filesystem marker — config_store is the single source of truth, which lets the
    daemon admin payload (4.1) and `gobby status` agree on installation state without
    filesystem coordination.

    Path threading (R28-F1): `gobby_home` is the canonical entry point when `db`
    is not pre-resolved. There is no `_default_db_path()` helper in this file —
    derive the DB path from `bootstrap.database_path` under the caller's gobby_home
    so test fixtures and non-default-home installs route to the right DB.
    """
    if db is None:
        from gobby.cli.utils import get_gobby_home
        from gobby.cli.installers.falkor import _resolve_falkordb_db_path
        home = gobby_home if gobby_home is not None else get_gobby_home()
        db = LocalDatabase(_resolve_falkordb_db_path(home))
    store = ConfigStore(db)
    return store.get("databases.falkordb.host") is not None and \
           store.get("databases.falkordb.port") is not None

async def is_falkordb_healthy(host: str | None, port: int | None, password: str | None) -> bool:
    """PING the FalkorDB host/port; return True on PONG.

    Resource-safety contract (R34-F2): `client` MUST be closed on every exit path,
    including the timeout / refused-connection / WRONGPASS / NOAUTH failure paths
    that this helper is specifically meant to handle. § 4.1 routes the admin
    status payload through `get_falkordb_status`, and § 4.3's health-failure
    lifecycle path calls this helper every tick — a redis client/pool leak on
    each unhealthy iteration would accumulate inside a long-running daemon.

    The `try/finally` shape below guarantees `aclose()` runs even when `ping()`
    raises. The inner `try/except` around `aclose()` suppresses any close error
    so a misbehaving close path does not mask the original failure or leak the
    underlying connection on the success path either.
    """
    if not host or not port:
        return False
    import redis.asyncio as redis
    client: redis.Redis | None = None
    try:
        client = redis.Redis(host=host, port=port, password=password, socket_timeout=5)
        result = await client.ping()
        return bool(result)
    except Exception:
        return False
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

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

**Tests (R21-F1) — owned by this task:**

- `tests/cli/test_services.py` — replace every `is_neo4j_installed`, `is_neo4j_healthy`, `get_neo4j_status` callsite with the FalkorDB equivalents; update the two-tier `installed`/`healthy` semantics assertions (presence of `databases.falkordb.host` + `databases.falkordb.port` in config_store → installed; PING success → healthy).

This task replaces those helpers in source; the test must move with them.

**Acceptance:**

- 3.3.1 — `is_falkordb_installed`, `is_falkordb_healthy`, and `get_falkordb_status` replace the `neo4j` equivalents. `is_falkordb_installed` accepts an optional `gobby_home: Path | None`; when `db` is not supplied, it normalizes via `home = gobby_home if gobby_home is not None else get_gobby_home()` and then calls `_resolve_falkordb_db_path(home)` (defined in `src/gobby/cli/installers/falkor.py`) which routes to `home / "gobby-hub.db"` when no `bootstrap.yaml` exists in `home` and to `Path(bootstrap.database_path).expanduser()` when it does. No `_default_db_path()` helper is introduced in `cli/services.py`. The no-bootstrap-yet branch must be covered by a focused test: call `is_falkordb_installed(gobby_home=<tmp_home_with_no_bootstrap_yaml>)` against a tmp DB seeded with `databases.falkordb.host`+`port`; assert it returns True AND that the resolved DB path was `<tmp_home>/gobby-hub.db`, not the operator's production `~/.gobby/gobby-hub.db`. file: `src/gobby/cli/services.py`.

### 3.4 Rename Neo4j CLI flags to FalkorDB and add service-targeting flag [category: code] (depends: 3.1, 3.3)

`kind: deliverable`

Targets: `src/gobby/cli/install.py`, `cli/install.py` (bare path cited in body), `src/gobby/cli/_install_prompts.py`, `cli/_install_prompts.py` (bare path cited in body), `src/gobby/cli/daemon.py`, `src/gobby/cli/installers/neo4j.py` (legacy installer being removed), `src/gobby/cli/installers/__init__.py` (Neo4j-export removal owned here per R32-F1; § 3.1 only adds the FalkorDB exports), `__init__.py` (installers package init bare path), `tests/cli/installers/test_falkordb_installer.py`, `tests/cli/test_cli_falkordb.py`

**Current state — verified against the actual code, not assumed:**

`gobby install` today has these flags that touch the graph backend (verified via `gobby install --help` and `grep -n` on `src/gobby/cli/install.py`):

- `--no-ext-services` (line ~129, name `no_ext_services_flag`) opts out of Docker service installs (Qdrant + Neo4j). **Note the actual flag name is `--no-ext-services`, not `--no-services`** — earlier draft had this wrong.
- `--neo4j-password` (line ~135, option) overrides the auto-generated Neo4j password
- There is **no `--neo4j`** flag on `gobby install`
- The auto-install block (line ~361: `if not no_ext_services_flag and embedding_provider != "none":`) calls `_run_qdrant_install` then `_run_neo4j_install`
- `gobby uninstall --neo4j` (line ~425, flag) DOES exist for uninstall

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

- Update `--no-ext-services` help text (line ~129) to read: `Skip Docker service installation (Qdrant, FalkorDB)`
- Update the `Skipping Qdrant/Neo4j install (embeddings disabled)` echo (near the auto-install block ~line 365) to say `FalkorDB`
- Replace the `_run_neo4j_install(install_neo4j, neo4j_password, results)` call (near line ~363) with `_run_falkordb_install(install_falkordb, falkordb_password, results)`
- Update the function-signature parameters (rename `neo4j_password: str | None` → `falkordb_password: str | None`; add `falkordb_flag: bool` — `no_ext_services_flag: bool` stays)
- **Service-targeting branch:** at the top of the install body (right after the targeting-flag short-circuits for `--claude` / `--gemini` / etc.), add an early return when `falkordb_flag` is set: skip every other install step and call only `_run_falkordb_install(install_falkordb, falkordb_password, results)`, then echo the per-component summary and exit. Follow the exact pattern the existing CLI-targeting flags use.

**Changes to `gobby uninstall`:**

- Replace `--neo4j` flag (line ~425, name `neo4j_flag`) with `--falkordb` flag (name `falkordb_flag`)
- Update the help text on the `--volumes` flag (line ~430) from `(use with --neo4j)` to `(use with --falkordb)`
- Update `_run_neo4j_uninstall(uninstall_neo4j, volumes_flag, results)` call (line ~592) → `_run_falkordb_uninstall(uninstall_falkordb, volumes_flag, results)`

**Changes to `_install_prompts.py`:**

Rename `_run_neo4j_install` → `_run_falkordb_install` and `_run_neo4j_uninstall` → `_run_falkordb_uninstall`. The new install invoker:

- Takes `(installer, password, results)`
- Calls `installer(password=password)`
- Echoes the password disclosure based on the installer's `password_source` field (R13-F3 contract pinned in 3.1):
  - `generated` → echo `Generated FalkorDB password: <result["password"]>` exactly once
  - `provided` → echo `Using provided FalkorDB password (not displayed)` — never reflect the operator's input back
  - `reused` → echo `Reusing existing FalkorDB password from config_store`
- Surfaces the FalkorDB Browser URL by consuming `result["browser_url"]` (do not hardcode `http://localhost:13000` here AND in 3.1 — single source of truth)
- Includes a `Restart the daemon to apply: gobby restart` line, mirroring the current Neo4j invoker

The uninstall invoker mirrors the existing `_run_neo4j_uninstall` shape and passes the volumes flag through to `uninstall_falkordb` as the `purge` argument.

**Wizard wiring (cross-references Phase 6.2):** the wizard's `Services.tsx` invokes `runGobby(["install", "--falkordb", ...optional --falkordb-password])`. This is the exact reason the `--falkordb` service-targeting flag exists — without it, the wizard would have to either re-run the full installer (re-doing CLI hooks etc.) or shell out to a Python entry point that bypasses Click. Phase 6.2 keeps using the args list shown above; this section guarantees the flag actually exists.

**Cleanup after callsite rename (R20-F2 + R21-F1 + R32-F1):** once `_run_falkordb_install` / `_run_falkordb_uninstall` are wired and the install/uninstall surface tests pass, perform the Neo4j cleanup as a single coupled change OWNED BY THIS TASK (not § 3.1):

1. Remove the `install_neo4j` / `uninstall_neo4j` exports from `src/gobby/cli/installers/__init__.py` (§ 3.1 only ADDED the `install_falkordb` / `uninstall_falkordb` exports — it intentionally left the Neo4j exports in place so callsites between § 3.1 closing and this section closing remain importable).
2. DELETE `src/gobby/cli/installers/neo4j.py`.

Both edits land in this task's diff. Running them in this order (export removal first, then file delete) keeps the intermediate state of the diff buildable. The combined effect is that `cli/install.py` and `cli/_install_prompts.py` import only the FalkorDB names after this task closes; nothing else in the tree still imports the deleted module.

**Tests owned by this task (R21-F1):**

- `tests/cli/installers/test_neo4j_installer.py` (NOT `test_neo4j.py` — R21-F1 corrected the prior filename) → rename to `tests/cli/installers/test_falkordb_installer.py`. Update assertions for the new install steps (Docker check, compose refresh + `--profile falkordb up`, `redis-cli -a $PW PING` health check with `-T`, `_update_config` atomicity per R19-F5, `_write_bootstrap_password` step-6 failure semantics per R11-F2 + R20 testing). Cover the three `password_source` paths from R13-F3 (`generated`, `provided`, `reused`).
- `tests/cli/test_cli_neo4j.py` → rename to `tests/cli/test_cli_falkordb.py`. Update the Click-flag assertions for the new flag surface (`--falkordb`, `--falkordb-password`, `--no-ext-services`, `gobby uninstall --falkordb`). Also assert that the hidden `--neo4j-password` and `--neo4j` flags raise `click.UsageError` with the migration message from the deprecation block below (R20-F4 — these literal flag-name strings live in source by design and are allowlisted in § 8.3 row #20).

Without these renames, § 4.3's residual `rg` sweep AND § 8.3 row #4's pytest run both fail on the still-present Neo4j-named test files.

**Hidden deprecation handlers (R39-F1 — implementation moved into this task from former § 8.1):** as part of the same `install.py` / `_install_prompts.py` edits, REGISTER `--neo4j-password` on `install` and `--neo4j` on `uninstall` as `click.option(... hidden=True)` whose handler immediately raises `click.UsageError` with the migration message:

```text
Error: --neo4j / --neo4j-password has been removed in this release.

The knowledge graph backend has been replaced with FalkorDB.
- Install (auto-runs as part of gobby install; tune with): gobby install [--falkordb-password <pw>] (or service-only: gobby install --falkordb)
- Uninstall: gobby uninstall --falkordb
- Migration notes: see CHANGELOG.md for the full upgrade path.
```

Implementation lives here (NOT in § 8.1, which is now policy-only) so the `test_cli_falkordb.py` and `test_install_coverage.py` test cases that owned-by-this-task assertions reference handlers that exist in the same commit. § 8.1 still owns the policy decision and CHANGELOG cross-reference, but no longer ships the Click handler code itself.

The hidden registration is required for the `gobby install --neo4j-password foo` → `click.UsageError` shape: without `click.option(... hidden=True)` Click would emit its built-in `Error: No such option: --neo4j-password` message instead, which does not name the FalkorDB equivalents and is the wrong user experience. The `hidden=True` flag keeps the deprecated names out of `--help` while still routing the call into the custom handler.

**Acceptance:**

- 3.4.1 — CLI flags renamed `--neo4j-*` → `--falkordb-*` with a service-targeting flag added. file: `src/gobby/cli/install.py`.
- 3.4.2 — Hidden `--neo4j-password` / `--neo4j` Click options registered with handlers that raise `click.UsageError` carrying the migration message; tests assert the migration message text and exit status. file: `src/gobby/cli/install.py`.

### 3.5 Rename bootstrap neo4j_password to falkordb_password end-to-end [category: code] (depends: 1.1, 3.1)

`kind: deliverable`

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
config = load_config()                       # bootstrap/Pydantic defaults only — config_store invisible
env["GOBBY_NEO4J_PASSWORD"] = bootstrap.neo4j_password
if config.databases.neo4j.url:
    profiles.append("neo4j")

# NEW:
# CRITICAL: load_config() with no arguments reads only bootstrap.yaml + Pydantic
# defaults — it does NOT see the installer-written `databases.falkordb.requirepass`
# secret in config_store. Without explicitly passing the ConfigStore + SecretStore,
# `is_falkordb_enabled(config.databases)` always returns False after install,
# and `gobby start` never appends the `falkordb` compose profile. Wire both stores
# from the daemon's LocalDatabase before the predicate check.
# R20-F1 + R29-F1: `_services_start(gobby_home: Path)` (verified at
# `src/gobby/cli/daemon.py:46`) receives `gobby_home` as a non-Optional Path
# from its caller, so no normalization is needed here. Resolve the DB path
# using the same rule the installer uses (`_resolve_falkordb_db_path(home)`
# from § 3.1): if `gobby_home / "bootstrap.yaml"` exists, derive the DB
# from `Path(bootstrap.database_path).expanduser()`; otherwise fall back
# to `gobby_home / "gobby-hub.db"`. Do NOT call the bare `load_bootstrap()`
# default — when the file is absent, live `load_bootstrap` returns
# `BootstrapConfig()` whose default `database_path` resolves to the
# operator's production `~/.gobby/gobby-hub.db`, which would silently
# break test-fixture daemons started against a tmp `gobby_home`. Mirror
# the installer's helper so `_services_start` and `install_falkordb`
# agree on the DB location for a given home.
from gobby.cli.installers.falkor import _resolve_falkordb_db_path
db_path = _resolve_falkordb_db_path(gobby_home)
db = LocalDatabase(db_path)
config_store = ConfigStore(db)
secret_store = SecretStore(db)
config = load_config(
    config_store=config_store,
    secret_resolver=secret_store.get,      # MUST be `.get`, NOT `.resolve` — see resolver-contract note below
)
# R28-F1: `load_config` accepts `config_file: str | None = None` (verified in
# `src/gobby/config/app.py:797`). The live `_services_start` does not resolve a
# config-file path before this call, so pass nothing — `load_config` falls back
# to its standard `~/.gobby/config.yaml` resolution from the environment, and
# the explicit `config_store` + `secret_resolver` wiring above is what gives
# this call visibility into the installer-written `databases.falkordb.requirepass`
# secret regardless of any on-disk config.yaml.
# Source of truth for compose-env injection (R13-F2): use the resolved
# `config.databases.falkordb.requirepass` so a `/api/config` or MCP-driven update
# to the secret on a running daemon takes effect on the next `gobby restart`.
# Bootstrap is only a pre-install / cold-start fallback for the case where the
# installer has not yet written config_store. Reading `bootstrap.falkordb_password`
# directly here would silently desynchronize the container password from the
# daemon's auth source after any in-place secret update through the runtime
# config surface (see 4.4's restart-semantics note + 8.3 row #22).
env["GOBBY_FALKORDB_PASSWORD"] = (
    config.databases.falkordb.requirepass
    or bootstrap.falkordb_password
)
if is_falkordb_enabled(config.databases):  # canonical predicate (2.1) — pass DatabasesConfig, not the top-level config
    profiles.append("falkordb")
```

**Resolver-contract note (verified live, R12-F1):** `load_config(secret_resolver=...)` at `src/gobby/config/app.py:240-245` invokes the resolver as `value = secret_resolver(name)` where `name` is the secret-store key extracted from a `$secret:NAME` reference. The contract is `Callable[[str], str | None]`. The daemon's main load path at `src/gobby/runner_init/storage.py:91` passes `runner.secret_store.get` — that is the correct method. `SecretStore.resolve` is a different method that returns the literal name string back, so passing it would silently return the string `"requirepass"` instead of the decrypted FalkorDB password — `is_falkordb_enabled(config.databases)` would then be True for the wrong reason and the daemon would attempt to authenticate against FalkorDB with the literal string `"requirepass"`. Mirror the `runner_init/services.py` pattern exactly.

**Test coverage required:** seed `config_store` with `databases.falkordb.requirepass = '$secret:requirepass'`, seed the `secrets` row with the encrypted plaintext, call `load_config(config_store=cs, secret_resolver=ss.get)`, assert `config.databases.falkordb.requirepass == <plaintext>`. Without this test, a future contributor switching `.get` ↔ `.resolve` won't get a CI signal.

This is the consumer the reviewer flagged as half-migrated — without this rewrite, `gobby start` would still try to inject `GOBBY_NEO4J_PASSWORD` into the docker-compose env and read the dead `bootstrap.neo4j_password` field. The 4.3 daemon-wide sweep should have caught this if missed; this section calls it out explicitly so 3.5's scope is unambiguous.

**4. `_write_bootstrap_password` (added in 3.1's installer):**

The helper writes back to `bootstrap.yaml` under the key `falkordb_password`. It must rewrite the YAML preserving any other keys (database_path, daemon_port, etc.) — copy the existing-file-merge pattern from `installers/neo4j.py::_write_bootstrap_password`.

**No bootstrap schema version bump needed** — the field rename is keyed on key presence and Pydantic ignores unknown keys, so the loader is forward+backward compatible against both old (`neo4j_password`) and new (`falkordb_password`) YAML files. The old value is simply discarded on first load.

**Acceptance:**

- 3.5.1 — `BootstrapConfig.falkordb_password` replaces `neo4j_password`. symbol: `gobby.config.bootstrap.BootstrapConfig.falkordb_password`.
- 3.5.2 — `is_falkordb_enabled(config.databases)` is wired through the daemon's loaded ConfigStore + SecretStore in `cli/daemon.py`. file: `src/gobby/cli/daemon.py`.

### 3.6 Migrate config_store keys `databases.neo4j.*` → `databases.falkordb.*` [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/storage/migrations.py` (the inline `MIGRATIONS` list — append a new entry here; there is no separate `_migration_registry.py`), `src/gobby/storage/config_store.py` (the JSON-encoded `value` column whose rows the migration rewrites)

**Real schema — verified before writing this task (R16-F1 corrects the prior draft):**

- `config_store.value` holds JSON-encoded strings. Every write goes through `json.dumps(...)` in `ConfigStore.set` and `ConfigStore.set_secret` (verified at `src/gobby/storage/config_store.py:81, 166`). For a secret reference, the JSON-encoded form of `f"$secret:<name>"` is the literal `'"$secret:<name>"'` with embedded quotes — NOT the bare string. Higher-level `ConfigStore.get(...)` decodes via `json.loads`; raw-SQL `WHERE value = ...` comparisons MUST account for the JSON encoding (use `json_quote('$secret:<name>')` or `json_extract(value, '$') = '$secret:<name>'`).
- `is_secret=1` flags the row as a secret reference; the secret's natural name is `config_key_to_secret_name(key)` — i.e., the LAST segment of the dotted key.
- `secrets` table holds the encrypted plaintext keyed by that natural name.
- `ConfigStore.clear_secret(key, secret_store)` deletes both rows in one transaction BUT deletes `secrets.name = <natural_name>` UNCONDITIONALLY — never use it for orphan-safe cleanup (R14-F1). For runtime cleanup that must preserve secrets still referenced by other config rows, use the raw-SQL three-step pattern in § 8.2 (migrate tunables → drop config rows → orphan-guarded secret delete with `value = json_quote('$secret:<name>')`).
- `SecretStore.delete(name)` removes the encrypted entry alone — same orphan-unsafety concern.
- The current Neo4j config writes `databases.neo4j.auth` → secret name `auth` (per `config_key_to_secret_name`).

**Migration strategy:**

The SQLite migration surface lives entirely in `src/gobby/storage/migrations.py`: the inline `MIGRATIONS` tuple at the top of the module is the declarative registry, and the runner code in the same module iterates over it. **There is no separate `_migration_registry.py` file** (verified — `src/gobby/storage/` contains only `migrations.py`, `migration_helpers.py`, `config_store.py`, and the `migrations/` SQL directory; no registry module exists). Add a new entry to the `MIGRATIONS` tuple in `migrations.py` — versioned one above the current highest entry. The runner picks it up automatically; no runner changes.

Migrations are registered as `MigrationAction = str | Callable[[LocalDatabase], None]` (R19-F2 — verified live in the registry contract). The runner calls `action(db)` for callables and expects `db.execute(...)` calls; the runner wraps each migration in its own transaction. Use raw SQL against the actual table names. **Critical:** preserve backend-agnostic tunables (`graph_search`, `graph_min_score`, `rrf_k`, `graph_name`) that should survive the backend swap — they describe KG behavior, not backend connection details.

```python
# Connection/auth keys — drop on migration (these are Neo4j-specific)
NEO4J_CONNECTION_KEYS = ("url", "auth", "database", "host", "port")

# Behavior tunables — migrate from databases.neo4j.* to databases.falkordb.* if user-overridden
NEO4J_TUNABLE_KEYS = ("graph_search", "graph_min_score", "rrf_k", "graph_name")

def migrate_neo4j_to_falkordb_config_keys(db: LocalDatabase) -> None:
    """Migrate user-tuned graph behavior; drop Neo4j-specific connection/auth keys.

    R19-F2: signature is `Callable[[LocalDatabase], None]` per the live
    MigrationAction contract — NOT `(conn: sqlite3.Connection)`. The runner
    calls `action(db)` and provides transaction semantics around the call;
    use `db.execute(...)` for every write.

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
        db.execute(
            "INSERT OR IGNORE INTO config_store (key, value, source, is_secret) "
            "SELECT REPLACE(key, 'databases.neo4j.', 'databases.falkordb.'), value, source, is_secret "
            "FROM config_store WHERE key = ?",
            (f"databases.neo4j.{key}",),
        )
    # 2. Drop ALL databases.neo4j.* keys (tunables already copied above; connection
    #    keys + the `$secret:auth` reference do not survive)
    db.execute("DELETE FROM config_store WHERE key LIKE 'databases.neo4j.%'")
    # 3. Drop the orphaned encrypted secret if and only if nothing else references the
    #    secret name. CRITICAL (R13-F1): config_store.value holds JSON-encoded strings —
    #    ConfigStore.set_secret writes `json.dumps(f"$secret:{name}")` (verified at
    #    src/gobby/storage/config_store.py:166), so the stored value is the literal
    #    `'"$secret:auth"'` with embedded quotes. A bare `value = '$secret:auth'` guard
    #    matches nothing and the orphan deletion always fires — including when a
    #    legitimate non-Neo4j key still resolves to last-segment `auth`. Use
    #    `json_quote(...)` (or `json_extract(value, '$') = '$secret:auth'`) to compare
    #    against the JSON-encoded form.
    db.execute(
        "DELETE FROM secrets WHERE name = 'auth' "
        "AND NOT EXISTS (SELECT 1 FROM config_store WHERE value = json_quote('$secret:auth'))"
    )
```

Idempotent. Safe. Preserves user tuning across the backend swap.

**Regression test (R13-F1):** before running the migration, seed `config_store` with two secret rows that BOTH reference the `auth` secret — `databases.neo4j.auth` (the migration target) AND a synthetic non-Neo4j key whose last segment is `auth` (e.g., `mock.test.auth`) inserted with value `json.dumps('$secret:auth')`. After the migration runs, assert: (a) the `databases.neo4j.auth` row is gone, (b) the synthetic row remains, AND (c) the `auth` row in `secrets` is preserved because the orphan guard correctly detected the surviving JSON-encoded reference. Without this test, a future contributor swapping `json_quote` back to a bare string would not get a CI signal.

**8.2 cross-reference (R16-F1):** the startup-time stale-config warning in § 8.2 ALSO clears `databases.neo4j.*` config_store rows AND the orphaned `auth` secret if no surviving row references it. § 8.2 uses the SAME raw-SQL three-step orphan-safe pattern as this section (migrate tunables → drop `databases.neo4j.*` config rows → `DELETE FROM secrets WHERE name = 'auth' AND NOT EXISTS (SELECT 1 FROM config_store WHERE value = json_quote('$secret:auth'))`). DO NOT use `ConfigStore.clear_secret(...)` in either path — it deletes the secret unconditionally and would remove a live non-Neo4j secret that happens to resolve to last-segment `auth`.

This task is critical to running before Phase 7 (Rust) lands, because gobby-cli reads `databases.falkordb.*` from the config_store. If the daemon hasn't migrated, the Rust client reads nothing and falls back to "graph unavailable."

**Acceptance:**

- 3.6.1 — One-shot migration preserves backend-agnostic tunables (`graph_search`, `graph_min_score`, `rrf_k`, `graph_name`) under `databases.falkordb.*` when user-overridden, drops every `databases.neo4j.*` connection/auth key (`url`, `auth`, `database`, `host`, `port`) including the `$secret:auth` config reference, and removes the orphaned `auth` secret only when no remaining JSON-encoded `$secret:auth` reference survives. file: `src/gobby/storage/migrations.py`.

## P4 Phase 4: Python — Admin Payload and Memory Routes

`kind: framing`

**Goal**: Update the `/api/admin/status` endpoint to emit `memory.falkordb` (matching the new frontend hook) and rename `_neo4j_client` references in memory routes.

### 4.1 Update admin _health.py to emit memory.falkordb status payload [category: code] (depends: Phase 2, 3.3)

`kind: deliverable`

Target: `src/gobby/servers/routes/admin/_health.py:258-278`

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
from gobby.config.persistence import is_falkordb_enabled  # R26-F1: required import for the predicate-driven `configured`

falkor_cfg = server.config.databases.falkordb
status = await get_falkordb_status(
    db=server.services.database,  # MUST be `.services.database`, NOT `.db` — verified live (R12-F2)
    host=falkor_cfg.host,
    port=falkor_cfg.port,
    password=falkor_cfg.requirepass,  # FalkorConfig field renamed to avoid secret-name collision; see 1.1
)
memory_stats["falkordb"] = {
    # R25-F3 + R26-F1: drive `configured` from the config predicate, not the live client. After a
    # health-check failure § 4.3 nulls `_falkor_client` (via `clear_graph_clients()`), so
    # a `_falkor_client is not None` check would render `{installed: True, healthy: False,
    # configured: False}` — the dashboard pill (5.2) maps that triple to "FalkorDB not
    # configured" / status `unknown`, hiding the real disconnected/unhealthy state from
    # operators. `is_falkordb_enabled(databases)` keys off `requirepass` which survives
    # health-check clears, so `configured` stays True until the operator runs uninstall.
    # The pre-R25-F3 draft also bound a `falkor_client = getattr(server.memory_manager, "_falkor_client", None)`
    # local that has been removed (R26-F1) — it was unused after the predicate switch and would
    # have tripped Ruff F841 (unused variable) at lint time.
    "configured": is_falkordb_enabled(server.config.databases),
    "installed": status["installed"],
    "healthy": status["healthy"],
    "url": status["url"],
}
```

**HTTPServer attribute note (verified live, R12-F2):** the live `HTTPServer` does NOT expose `server.db`. The daemon's `LocalDatabase` is reached through `server.services.database`, matching the existing access pattern in the same `_health.py` file (lines 272 and 361 both use `server.services.database`). Writing `server.db` would raise `AttributeError`, the route would fall into the broad exception path, and `memory.falkordb` would emit `{"configured": False, "installed": False, "healthy": False, "url": None}` even when FalkorDB is correctly installed and running — the dashboard pill (5.2) would silently show "not configured" for every user. Test coverage: add an admin status route test that uses a server mock exposing only `services.database` and verifies `memory.falkordb.installed` reads through `get_falkordb_status` rather than the fallback path.

Replace the empty-fallback path (line 259) similarly: `memory_stats["falkordb"] = {"configured": False, "installed": False, "healthy": False, "url": None}`.

The dict key change from `neo4j` → `falkordb` is the load-bearing contract change for Phase 5 (frontend).

**Tests (R21-F1) — owned by this task:**

- `tests/servers/routes/test_admin.py` (R22-F4 — explicit ownership; this file currently asserts the `memory.neo4j` admin payload). Update the asserted payload key from `memory.neo4j` to `memory.falkordb`. Verify (R26-F1 — corrected from the earlier `_falkor_client`-driven assertion which is the exact behavior R25-F3 removed): `configured` is driven by `is_falkordb_enabled(server.config.databases)` — a payload built when `requirepass` is set returns `configured=True` regardless of `_falkor_client`'s state. Add a focused regression case where `server.memory_manager._falkor_client is None`, `server.memory_manager._kg_service is None` (the post-health-check-failure state from § 4.3.a), `server.config.databases.falkordb.requirepass` is set, `installed=True`, `healthy=False`, AND assert the payload still carries `configured=True` — that's the exact triple that drives `SystemHealthCard` to render disconnected/unhealthy instead of the misleading not-configured/unknown state. Verify `installed` / `healthy` come through `get_falkordb_status`. Add the R12-F2 coverage requirement: a server mock exposing only `services.database` (NOT `db`) so a future regression to `server.db` would be caught.
- `tests/utils/test_utils_status.py` (R22-F4) — update the daemon-status helpers' Neo4j references to FalkorDB; assert the new `is_falkordb_installed` / `get_falkordb_status` paths are what the status output threads through; cover the installed-but-unconfigured edge case (3.3) so the status output distinguishes that state from "not installed" (the two-tier semantics from § 3.3 are observable here).

This task introduces the `memory.falkordb` payload key (the load-bearing contract for Phase 5's frontend rename) — the test must move with the source.

**Acceptance:**

- 4.1.1 — `/api/admin/status` emits `memory.falkordb` status (no `memory.neo4j`). The lightweight `/api/admin/health` endpoint continues to return only `{"status": "ok"}` and is NOT required to include the memory payload. file: `src/gobby/servers/routes/admin/_health.py`.

### 4.2 Rename _neo4j_client references in memory routes [category: refactor] (depends: Phase 2)

`kind: deliverable`

Target: `src/gobby/servers/routes/memory.py:277, 301`

Find every `getattr(server.memory_manager, "_neo4j_client", None)` and replace with `getattr(server.memory_manager, "_falkor_client", None)`. Also rename any local variable named `neo4j_client` to `falkor_client` in this file. The endpoint paths (`/api/memories/graph/entities`, `/api/memories/graph/entities/{key}/neighbors`) stay the same — frontend continues calling them with no change.

Search broadly for any other `_neo4j_client` references with `grep -rn '_neo4j_client' src/gobby/` and rename all hits.

**Tests (R21-F1) — owned by this task:**

- `tests/servers/routes/test_memory_routes.py` — replace every `_neo4j_client` attribute-access assertion with `_falkor_client`; rename any local fixture variables; the route paths (`/api/memories/graph/entities`, `/api/memories/graph/entities/{key}/neighbors`) stay the same — frontend continues calling them with no change, so test the routes still respond with the new client wired in.

This rename happens here in source; the route test must move with it.

**Acceptance:**

- 4.2.1 — Memory route handlers reference the renamed `_falkor_client` (no `_neo4j_client`). file: `src/gobby/servers/routes/memory.py`.

### 4.3 Sweep daemon-wide for residual Neo4j references [category: refactor] (depends: 1.3, 3.4, 3.5, 4.1, 4.2, Phase 2, 3.3, 3.6)

`kind: deliverable`

Targets: `src/gobby/runner_init/services.py`, `src/gobby/runner_lifecycle_subsystems.py` (live health-check + code-index start path — `_check_external_services` at line 69, `_start_code_index_tasks` at line 274), `src/gobby/runner_lifecycle_shutdown.py` (live shutdown path — `_close_managers_and_storage` at line 286), `src/gobby/code_index/sync_worker.py` (sync worker signature change for stale-graph-client handling), `src/gobby/runner_maintenance.py`, `src/gobby/cli/daemon.py`, `src/gobby/cli/memory.py`, `src/gobby/cli/pack.py` (live file with Neo4j-named Docker volume `gobby_neo4j_data` at line 47 and "Neo4j + Qdrant" help text at lines 151/232/239/386/397 — fully owned by this sweep, NOT audit-only), `src/gobby/utils/status.py`, `src/gobby/config/code_index.py`, `src/gobby/mcp_proxy/tools/memory.py`, `tests/runner_helpers.py`, `tests/code_index/test_sync_worker.py` (R29-F2 vector-independence regression tests added in the sync-worker subsection below), `~/.gobby/services/docker-compose.yml` (R32-F2 — the live FalkorDB compose location referenced in the pack-fallback removal rationale), `.gobby/services/docker-compose.yml` (project-local compose location bare path), `docker-compose.services.yml` (pre-migration legacy compose filename mentioned in the pack-fallback removal rationale; verified gone after § 3.1's `_refresh_unified_compose` runs). Verify each; edit any that touches Neo4j. `tests/runner_helpers.py` is the R22-F4 conftest cascade target.

These call sites still read `db_cfg.neo4j.*`, inject `GOBBY_NEO4J_PASSWORD` into subprocess env, key reports/payloads under `neo4j`, expose Neo4j-branded MCP tool descriptions, or print Neo4j-named status output. The earlier tasks (1.1, 2.1, 2.2, 3.3, 3.4, 3.6, 4.1, 4.2) only touched the core wiring — these surfaces are the long tail.

For each file:

1. `grep -n 'neo4j\|Neo4j\|NEO4J' <file>` to enumerate hits
2. Replace `db_cfg.neo4j.*` → `db_cfg.falkordb.*` (host/port/password/graph_name) using the new `FalkorConfig` shape from 1.1
3. Replace `GOBBY_NEO4J_PASSWORD` → `GOBBY_FALKORDB_PASSWORD` in any subprocess env injection (audit the daemon's subprocess spawns via `rg GOBBY_NEO4J_PASSWORD src/`). **`src/gobby/cli/pack.py` requires concrete edits in this same sweep:** (a) rename the Docker volume entry `gobby_neo4j_data` to `gobby_falkordb_data` in the `DOCKER_VOLUMES` list at line 47; (b) rewrite the user-facing help/docstring text at lines 151, 232, 239, 386, 397 — change "Neo4j + Qdrant" to "FalkorDB + Qdrant" and "Stop Docker services (Qdrant, Neo4j)" to "Stop Docker services (Qdrant, FalkorDB)"; (c) **REMOVE the legacy Neo4j compose fallback at line 155** (R32-F2 — pinned decision, no open-ended "decide"). Rationale: the upgrade path is exclusively owned by § 3.1's `_refresh_unified_compose` (which unconditionally overwrites `~/.gobby/services/docker-compose.yml` with the FalkorDB template AND tears down the Neo4j profile via the R31-F2 anchored compose-down BEFORE the install completes). § 8.2's startup stale-config warning operates on `config_store` keys, NOT on legacy compose files. § 8.3 row 18 exercises the install-time upgrade path through `_refresh_unified_compose`, not through `pack.py`. After § 3.1 closes, no operator-reachable code path can observe a Neo4j-era `docker-compose.services.yml` on disk, so a fallback in `pack.py` would never fire on a freshly-installed 0.4.0 daemon — and leaving it in place reintroduces a dead Neo4j reference under § 4.3's residual sweep. The pack/unpack flow is the canonical user-facing backup surface, so leaving Neo4j-named volumes or help text behind would surface to operators every time they pack or unpack a project.
4. Rename payload/report keys: `report["neo4j"]` → `report["falkordb"]`, log keys `neo4j_url` → `falkordb_url`, etc.
5. For `mcp_proxy/tools/memory.py`: the tool descriptions on `search_knowledge_graph`, `rebuild_knowledge_graph`, `clear_knowledge_graph` may mention Neo4j by name — rewrite to mention FalkorDB or the neutral phrase "knowledge graph backend"
6. For `cli/memory.py`: any user-facing `gobby memory` subcommand text mentioning Neo4j must be updated
7. For `utils/status.py`: the status formatter likely has a Neo4j row — replace with FalkorDB

**Lifecycle semantics (R23-F1) — owned by this task, edits land in the live lifecycle subsystem files: `src/gobby/runner_lifecycle_subsystems.py` for health-check and code-index-start; `src/gobby/runner_lifecycle_shutdown.py` for shutdown; `src/gobby/code_index/sync_worker.py` for the loop signature change. `_check_external_services`, `_start_code_index_tasks`, and `_close_managers_and_storage` all live in the subsystem modules listed above. Verify via `grep -n '_check_external_services\|_close_managers_and_storage' src/gobby/runner_lifecycle*.py` before editing:**

The daemon owns TWO independent FalkorDB clients: `runner.memory_manager._falkor_client` (graph_name `gobby_kg`, from § 2.1) and the CodeGraph client wired into `runner.code_indexer.context._graph` (graph_name `gobby_code`, from § 2.2). The pre-migration Neo4j lifecycle path only degrades and closes the MemoryManager client; without explicit handling, an implementation that follows the rename-only sweep above leaves CodeGraph holding a dead client after a failed health check AND leaks the code-graph connection at shutdown.

Two specific edits across the live lifecycle subsystem files (per the file-layout note above):

a. **Health-check failure path:** the periodic FalkorDB health check (currently named for Neo4j; rename per the sweep above) MUST, on unhealthy return, clear BOTH backend reference paths — call `runner.memory_manager.clear_graph_clients()` (the new method added in § 2.1 step 4; per R24-F2 + R28-F4 it nulls `_falkor_client`, `_kg_service`, `_search_service._kg_service`, AND `_indexing_service._kg_service` in a single shot because both downstream services hold their own KG reference at construction time — clearing only the manager-level attributes would leave the SearchService / IndexingService graph paths alive and the partial-degradation state would persist) AND call `runner.code_indexer.clear_graph_client()` (the new sync method added in § 2.2 — note `runner.code_indexer` IS the `CodeIndexContext` instance, R24-F1, NOT a wrapper with a `.context` property; using `runner.code_indexer.context.clear_graph_client()` would AttributeError at first health-check tick). This makes `/api/memories/...` and `/api/code-index/...` routes fall back to the "graph unavailable" path simultaneously, matching the user-visible single-backend semantics — partial degradation (memory works, code-index doesn't, or vice versa) is a confusing operator UX and a frequent source of false bug reports.
b. **Shutdown ordering:** in the runner's shutdown sequence, await `runner.code_indexer.close_graph_client()` (the new async method added in § 2.2 — same `runner.code_indexer` direct attribute, no `.context` indirection per R24-F1) BEFORE calling `LocalDatabase.close()`. The MemoryManager already has its own `close()` plumbing; ensure both close paths fire even if one raises (use `try/except/finally` around the pair so one client's close failure does not strand the other connection open).
c. **Sync worker reference (R25-F2 + R26-F2 + R29-F2 — pinned to a single executable approach):** the live `sync_worker_loop` (`src/gobby/code_index/sync_worker.py`) is started with `graph=runner.code_indexer.graph` resolved at startup time and held as a local for the loop's lifetime. Calling `clear_graph_client()` only nulls `CodeIndexContext._graph` — the running loop still owns the stale `CodeGraph` reference and would keep writing to a dead FalkorDB client every pass.

**The vector / graph independence contract (R29-F2 — load-bearing):** live `sync_worker_loop` (verified at `src/gobby/code_index/sync_worker.py:25-200`) processes TWO orthogonal sync paths per file — vectors (Qdrant) and graph (CodeGraph) — and the module docstring says "Each file's vector and graph sync are independent — one can succeed even if the other fails." A short-circuit on `graph is None` that sleeps-and-continues would STARVE the vector path during any health-flap, since vector sync has nothing to do with FalkorDB health. The replacement must preserve the independence: if the graph client is None for an iteration, the vector sync MUST still run for every file with `vectors_synced=0`, and only the graph-write portion of `_sync_pass` (the `await graph.sync_file(...)` and friends gated on `config.graph_enabled`) is skipped for that iteration.

**The implementation:**

- Change `sync_worker_loop`'s signature from `graph: CodeGraph | None` to `context: CodeIndexContext` and re-read `context.graph` at the top of EVERY iteration. Bind to a local `graph = context.graph` immediately so the rest of the iteration sees a stable view (no torn reads if a concurrent `clear_graph_client()` lands mid-iteration).
- Thread that per-iteration `graph` local — INCLUDING when it is `None` — down through `_sync_pass(..., graph=graph, vector_store=vector_store, ...)`. The live `_sync_pass` already accepts `graph: CodeGraph | None` (line 92) and the live `_sync_file` already has independent branches for vector vs graph work (vector at line 167 gated on `config.embedding_enabled`, graph elsewhere gated on `config.graph_enabled` + `graph is not None`). Do NOT short-circuit the entire iteration when `graph is None`; let the existing per-file branches do their independent thing.
- Update the runner-start callsite that constructs the task to pass `context=runner.code_indexer` instead of `graph=runner.code_indexer.graph`.

Add TWO regression tests in `tests/code_index/test_sync_worker.py`:

1. Clears the graph client mid-run, marks one file pending with `graph_synced=0 AND vectors_synced=0`, drives a sync tick, and asserts (a) the OLD `CodeGraph` test double's write methods were NOT called after the unhealthy tick, AND (b) `vector_store.upsert(...)` WAS called for that file AND `storage.mark_vectors_synced(file.id)` was invoked. Proves vector starvation does NOT happen during graph downtime.
2. Clears the graph client mid-run, restores it on the next tick (the next iteration re-reads `context.graph` and sees the restored value), marks the same file pending again, drives a tick, and asserts the new `CodeGraph` write methods WERE called for the graph sync. Proves graph recovery picks up automatically without any worker restart.

This naturally tracks `clear_graph_client()` without any additional coordination; the loop's degradation is per-path, not all-or-nothing, and matches the route-level "graph unavailable" path while keeping vector indexing live.

**(Rejected alternative, recorded for context only:** cancel/restart the sync worker on health-failure via `runner.code_index_sync_task.cancel()` plus re-spawn on recovery — rejected because it requires demoting the periodic health-check to startup-only, churns the asyncio task tree on every flap, and produces noisy logs. NOT executable plan content.)

**Tests (R21-F1 + R22-F4 + R23-F2) — owned by this task:**

- `tests/test_runner_lifecycle.py` (R23-F2: Neo4j references at lines 151, 226, 298, 358 — import / fixture-patch / assertion sites). Update the fixture-level `Neo4jClient` patches to `FalkorClient`; add a focused case where the FalkorDB health check returns unhealthy and assert FIVE properties after the next health-check tick (R23-F1.a + R24-F2 + R28-F4): `runner.memory_manager._falkor_client is None`, `runner.memory_manager._kg_service is None`, `runner.memory_manager._search_service._kg_service is None`, `runner.memory_manager._indexing_service._kg_service is None`, AND `runner.code_indexer.graph is None`. Note: `runner.code_indexer` IS the `CodeIndexContext` directly — no `.context` property (R24-F1); a test that writes `runner.code_indexer.context.graph` would AttributeError before reaching the assertion. Also drive one HTTP request to `/api/memories/graph/entities` AND one to `/api/code-index/...` after the unhealthy tick and assert both fall back to the "graph unavailable" path — proves the full-clear gates both routes simultaneously, not just at the attribute level.
- `tests/test_runner_shutdown.py` (R23-F2: Neo4j refs at lines 351, 401) — replace Neo4j-named teardown assertions with FalkorDB equivalents; add a case where `runner.code_indexer.close_graph_client()` is invoked (direct attribute, no `.context.` indirection per R24-F1) and the underlying CodeGraph FalkorClient connection close fires exactly once. Verify shutdown ordering: code-index `close_graph_client()` runs BEFORE `LocalDatabase.close()` (R23-F1.b).
- `tests/conftest.py` (R23-F2: Neo4j-config setup at lines 223-224) — update the shared runner/server fixtures that today set `databases.neo4j.*` values to instead provide a `FalkorClient` test double with the same `query()` / `close()` surface. This file is consumed by everything in `tests/`, so a stale import here cascades into a full collection failure (R22-F4 documented this risk for `tests/runner_helpers.py`; conftest is the larger blast radius).
- `tests/mcp_proxy/test_memory_tools_kg.py` (R23-F2: lines 13, 26-27, 48) — replace `Neo4jClient` imports/patches with FalkorClient; update the MCP tool descriptions assertions in line with § 4.3 step 5 (Neo4j → FalkorDB or "knowledge graph backend").

These updates close the remaining import-time and runtime gaps that § 4.3's `rg` sweep would otherwise discover at the very end of the migration. The pytest scope in § 8.3 row #4 expands to match.

After the sweep, run `rg -l 'neo4j\|Neo4j\|NEO4J' src/gobby/` and verify the only hits remaining are intentional (e.g., the bootstrap migration in 3.5 that detects `neo4j_password`, the config_store migration in 3.6 that deletes `databases.neo4j.*`, and any CHANGELOG entry from Phase 9). Everything else must be gone.

This task gates Phase 5 (frontend) and Phase 9 (docs) — neither of those should be touched until the daemon-side sweep is clean, otherwise frontend types and doc rg sweeps will catch ghost references.

**Acceptance:**

- 4.3.1 — Daemon-wide sweep removes residual `Neo4j`/`neo4j` references from the runtime code path (config wiring, lifecycle, MCP/CLI surfaces, status helpers, memory routes, pack/unpack flow). The remaining intentional refs after this sweep are the § 8.1 hidden deprecation handlers in `src/gobby/cli/install.py` and the § 3.5 / § 3.6 / § 8.2 stale-config migration helpers; both are explicitly preserved here and re-checked under the § 8.3 row 20 final repository-wide allowlist sweep. behavior: "ripgrep `Neo4j|neo4j` over `src/gobby/` returns only the § 8.1 deprecation-handler block and § 3.5 / § 3.6 / § 8.2 migration helpers" in `src/gobby/`.

### 4.4 Teach config secret-detection that requirepass is a secret [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/storage/config_store.py` (the `_SECRET_SUFFIXES` constant + `is_secret_key_name`), `src/gobby/servers/routes/configuration.py` (covers `/api/config/values` PUT/GET, `GET/PUT /api/config/template`, `POST /api/config/import`), `src/gobby/mcp_proxy/tools/config.py`, `tests/mcp_proxy/tools/test_config.py::TestIsSecretKeyName`, `mcp_proxy/tools/test_config.py` (bare path matched by validator), `tests/servers/routes/test_configuration_routes.py`, `pack.py` (R36-F1 cross-reference — pack flow stays unchanged but the bare name is cited in body for the cross-reference; not edited by this task, real pack edits live in §4.3).

The `requirepass` field name is intentional (chosen in 1.1 to avoid the `password` last-segment collision with `auth.password`), but `src/gobby/storage/config_store.py:is_secret_key_name()` only recognizes keys whose last segment ends in suffixes from `_SECRET_SUFFIXES` (currently `password`, `_secret`, `_auth`, etc.). `requirepass` does not end in any of those, so:

- `/api/config/values` GET would return the value plaintext instead of masked
- `/api/config/values` PUT would write to plain `config_store` instead of routing through `SecretStore`
- The config MCP tools (`get_config`, `set_config` in `mcp_proxy/tools/config.py`) would do the same
- Config import/export would round-trip the password as plain text in YAML

**Implementation (R26-F3 + R28-F3 — pinned to a single executable approach):**

**Step A — config_store.py:** add the literal string `"requirepass"` to the `_SECRET_SUFFIXES` tuple in `src/gobby/storage/config_store.py`. Because `is_secret_key_name` checks `last_part.endswith(suffix)` against each entry, this catches `databases.falkordb.requirepass` AND any future `*.requirepass` keys automatically without per-key allowlist maintenance. The plain string `"requirepass"` is unique enough across the existing config namespace (verified — no other dotted key ends in this segment) that the `endswith` match is safe.

**Step B — mcp_proxy/tools/config.py (REQUIRED, not optional):** live `src/gobby/mcp_proxy/tools/config.py::set_config` only routes through `ConfigStore.set_secret(...)` when the caller explicitly passes `is_secret=True`; with the default `is_secret=False`, plaintext is persisted and `get_config` returns it raw. Adding `"requirepass"` to `_SECRET_SUFFIXES` alone does NOT change this — `set_config` does not consult `is_secret_key_name` today. The `gobby-config` callers in §8.3 rows 19, 22, 23 use the plain `set_config` form (no `is_secret=True`), so without this Step B change those scenarios would persist the password as plaintext, leak it on `get_config`, skip the `validate_falkordb_password` pre-check, and miss the restart hint.

Rewrite EVERY config MCP read/write surface so they auto-detect secret keys — single-key AND multi-key paths (R35-F1):

- `set_config(key, value, is_secret=False)` — at the top of the body, set `effective_is_secret = is_secret or is_secret_key_name(key)`. From that point on, treat the call exactly as today's `is_secret=True` path: run `validate_falkordb_password(str(value))` (imported from `gobby.config.persistence`) before persistence if the key is `databases.falkordb.requirepass`, persist via `ConfigStore.set_secret(...)`, and include `requires_restart: True` + the human-readable restart hint in the success response when the changed key is `databases.falkordb.requirepass`. Keep the explicit `is_secret=True` parameter for backward compatibility — it stays an additive override, not the only entry point.
- `get_config(key)` — when `is_secret_key_name(key)` is True, return the masked sentinel (`********` or whatever string the existing `is_secret=True` path returns) instead of the raw `flat[key]` value. This closes the secret-leak surface on the read side that exists today even for keys with `is_secret=True` persisted entries.
- `get_config_section(prefix)` (R35-F1 — live tool at `src/gobby/mcp_proxy/tools/config.py:84`) — before returning the nested section from `_flat_config()`, walk the flat key set and mask every entry whose fully-qualified dotted key satisfies `is_secret_key_name(key)`. Otherwise `gobby-config get_config_section databases.falkordb` would expose the resolved `requirepass` in plaintext even after the single-key `get_config` path is masked.
- `set_config_batch(entries)` (R35-F1 + R36-F2 — live tool at `src/gobby/mcp_proxy/tools/config.py:184` whose body calls `config_store.set_many(flat_updates, source="mcp")` and is documented as atomically persisting multiple keys) — the batch path MUST honor the same secret contract as `set_config` AND preserve the live atomic-persistence guarantee. Two acceptable shapes; pick the SPLIT-IN-TRANSACTION shape:
  - **Split secret vs non-secret entries, persist inside one transaction:** before the persistence step, partition `flat_updates` into `secret_updates` (where `is_secret_key_name(key)` is True or the entry's explicit `is_secret` flag is True) and `plain_updates`. For each entry in `secret_updates`, run `validate_falkordb_password(str(value))` if the key is `databases.falkordb.requirepass`; on validation failure, abort the WHOLE batch (do not partially persist) and return the validator's error. Wrap BOTH writes in one outer `with db.transaction():` block — call `ConfigStore.set_secret(...)` per secret key and `config_store.set_many(plain_updates, source="mcp")` for the plain set, all inside the same transaction (R36-F2). Without the outer transaction, a write failure between the per-key `set_secret` calls and the `set_many` call (e.g., disk full mid-encrypt, SQLite contention, or a write that violates a future CHECK constraint) would leave a partial mixed-batch persisted — breaking the live atomic contract the operator depends on. If any persisted secret key matches the restart-required set (`databases.falkordb.requirepass`), include `requires_restart: True` and the restart hint in the response.
  - **(Rejected alternative, recorded for context only:** explicitly reject batches containing secret keys with a clear error directing callers to `set_config`. NOT executable plan content because it makes the batch tool less useful for legitimate mixed updates that include a password change.)
  - **Transaction regression test (R36-F2):** drive a mixed `[rrf_k=80, requirepass=<valid>]` batch through `set_config_batch` with a fixture that forces `config_store.set_many(...)` to raise mid-batch (e.g., a monkeypatched method that succeeds during the secret-write step then raises on `set_many`). Assert: (a) the request returns failure, (b) `SELECT value FROM config_store WHERE key='databases.falkordb.rrf_k'` is unchanged from its prior state (no partial plain write), (c) `SELECT value FROM config_store WHERE key='databases.falkordb.requirepass'` is unchanged AND `SELECT * FROM secrets WHERE name='requirepass'` is unchanged (no partial secret write). The outer transaction must roll back BOTH legs.

Without Step B covering ALL four tools, `_SECRET_SUFFIXES` alone covers the `/api/config/values` route path (which already consults `is_secret_key_name` via the `_SECRETS_MAP` flow in `src/gobby/servers/routes/configuration.py`) but leaves the MCP read-section + batch-write surfaces leaky. All four surfaces are explicitly in scope for §8.3 rows 19, 22, 23, so all four must close.

**(Rejected alternative, recorded for context only:** an explicit allowlist for `databases.falkordb.requirepass` in `is_secret_key_name` would be more targeted but requires updating the allowlist for every new non-suffix-matching secret, scattering policy across both data and code. NOT executable plan content.)

After the change:

1. Update `tests/mcp_proxy/tools/test_config.py::TestIsSecretKeyName` to cover `databases.falkordb.requirepass` returning `True`
2. Add a route-level test in `tests/servers/routes/test_configuration_routes.py` that round-trips the value via PUT then GET and verifies the GET response is masked (`********` or whatever convention the existing `auth.password` tests follow)
3. Add config-MCP-tool tests covering ALL four surfaces (R35-F1):
   (a) `set_config` (no `is_secret=True`) persists `databases.falkordb.requirepass` through `ConfigStore.set_secret`; raw DB shows `$secret:requirepass` and an encrypted `secrets` row.
   (b) `get_config` for the same key returns the masked sentinel.
   (c) `get_config_section("databases.falkordb")` returns the nested section with `requirepass` masked; non-secret tunables (`graph_search`, `rrf_k`) round-trip plaintext.
   (d) `set_config_batch` with a mixed batch — one plain entry (`databases.falkordb.rrf_k`) and one secret entry (`databases.falkordb.requirepass`) — persists the plain entry via `set_many` and the secret entry via `ConfigStore.set_secret`; the response includes `requires_restart: True` plus the hint. A second batch test with an INVALID `requirepass` (whitespace) aborts the WHOLE batch (verify the plain entry is NOT persisted) and returns the validator's error.
   (No `gobby config` CLI exists today — verified via `gobby --help`. The `/api/config/values` route + the four `gobby-config` MCP tools (`set_config`, `get_config`, `get_config_section`, `set_config_batch`) are the only config surfaces in scope.)
4. **Config template + import surfaces (R36-F1 — REQUIRED, not conditional):** the live `src/gobby/servers/routes/configuration.py` exposes three additional config surfaces that bypass the single-key + batch MCP path: `GET /api/config/template` (handler reads `config_store.get_all()` at line 303 / 606 and returns the full unflattened tree), `PUT /api/config/template` (`save_config_template`, line 311; persists via `config_store.set_many(diff, source="user")` at line 338), and `POST /api/config/import` (`import_config`, line 652; persists via `config_store.set_many(...)` at lines 672/689). Without explicit Step B coverage, a FalkorDB password supplied through template/import would be emitted in the template GET response, persisted as plaintext on template PUT or import POST, and miss both the validator and the restart hint. These are not pack.py concerns — they are runtime HTTP routes that ship in 0.4.0 and are exercised by the web settings UI.

Apply the same auto-detect contract to all three:

- **`GET /api/config/template`:** before serializing the returned tree, walk the flat key set from `config_store.get_all()` and mask every key satisfying `is_secret_key_name(key)` (return either the `********` sentinel or the literal stored reference `$secret:<name>` plus a `secret_keys: [...]` metadata field — pick the masked-sentinel form to match `get_config` and `get_config_section`). The response MUST NOT contain plaintext for `databases.falkordb.requirepass` under any code path, including the unflattened nested object form.
- **`PUT /api/config/template` (`save_config_template`):** at the top of the body, partition the incoming `diff` into `secret_updates` and `plain_updates` via `is_secret_key_name(key)`. **Mask-preservation rule (R38-F1):** for every entry in `secret_updates` whose incoming value equals the masked sentinel `"********"` (the same string `GET /api/config/template` returns for that key — verified via the live `/api/config/values` PUT path which already treats masked values as unchanged), REMOVE the entry from the persisted diff entirely. Do NOT call `validate_falkordb_password("********")` (which would fail the charset rule and falsely surface HTTP 422 on a no-op template save), do NOT call `ConfigStore.set_secret(...)` with the sentinel as the password (which would encrypt the literal `********` as the FalkorDB password and corrupt authentication on the next restart), and do NOT include the key in `requires_restart` accounting. The existing `config_store` `$secret:<name>` reference and the encrypted `secrets` row remain untouched. After that filter, run `validate_falkordb_password(str(value))` on any REMAINING `databases.falkordb.requirepass` secret entry BEFORE persistence; on failure return HTTP 422 (same shape as the `/api/config/values` PUT failure) and persist nothing. Wrap the dual write (`ConfigStore.set_secret(...)` for each secret + `config_store.set_many(plain_updates, source="user")`) in a single `db.transaction()` per R36-F2 below. If any secret in the persisted set is `databases.falkordb.requirepass`, include `requires_restart: True` in the success response with the same human-readable hint as `/api/config/values` PUT. Add a route-level regression test that seeds `databases.falkordb.requirepass`, calls `GET /api/config/template`, submits the returned template unchanged to `PUT /api/config/template`, and asserts the `secrets` row and `config_store` reference are byte-identical to the pre-call state AND `validate_falkordb_password` was never invoked with `"********"`.
- **`POST /api/config/import` (`import_config`):** apply the secret contract carefully because the live import format already distinguishes secret REFERENCES from plaintext secret VALUES (R37-F3). Verified live: `ImportConfigRequest` at `configuration.py:101` carries `config_store: dict[str, Any] | None` AND `config_secret_keys: list[str] | None` (line 105); the export side at lines 637-643 emits `config_secret_keys = config_store.get_secret_keys()` and serializes secret values as `$secret:<name>` sentinels rather than plaintext. An exported bundle therefore contains entries like `config_store["databases.falkordb.requirepass"] = "$secret:requirepass"` paired with `config_secret_keys = [..., "databases.falkordb.requirepass", ...]`, NOT the literal password. Treating that sentinel as a plaintext password — running `validate_falkordb_password("$secret:requirepass")` (which would fail the charset rule on `$`/`:`) and persisting via `ConfigStore.set_secret(...)` — would encrypt the SENTINEL as the password and irreversibly corrupt the imported config. The contract MUST be:

  1. **Partition the incoming `config_store` dict into three buckets:**
     (a) `secret_reference_keys` — keys whose incoming value matches the literal `$secret:<name>` pattern (verify via `str(value).startswith("$secret:")`) AND which appear in `request.config_secret_keys` OR satisfy `is_secret_key_name(key)`. These are export-format references; preserve them through `config_store.set_many({k: v}, source="import")` AND restore the `is_secret=1` flag plus the entry in the secrets-key registry via the existing import path at lines 692-696 (`if request.config_secret_keys: for key in request.config_secret_keys: config_store.mark_secret(key)` — read the exact live API name there; do not invent `mark_secret` if the live method is different). Do NOT call `set_secret`, do NOT call `validate_falkordb_password`, and do NOT encrypt anything for this bucket — the operator must re-supply the actual password value through `/api/config/secrets` after import, which is the documented round-trip path.
     (b) `secret_value_keys` (R38-F2 — broadened to close the legacy-plaintext-no-metadata path) — keys whose fully-qualified name satisfies `is_secret_key_name(key)` OR which appear in `request.config_secret_keys`, AND whose incoming value is NOT a `$secret:<name>` reference. This covers both the modern path (key flagged via `config_secret_keys` metadata) AND the legacy/nested-import path where `request.config_secret_keys` is absent but the key's suffix matches the secret-detection rule from step 1 (e.g., a flat import of `databases.falkordb.requirepass` with a plaintext value but no accompanying metadata). Run `validate_falkordb_password(str(value))` when the key is `databases.falkordb.requirepass`; on failure abort the whole import with HTTP 422 (no partial write — verify via raw DB: NEITHER the `config_store` row for `requirepass` NOR any `plain_keys` entry from the same batch is persisted, because both legs share the outer transaction). Persist via `ConfigStore.set_secret(...)` inside the outer `db.transaction()` per F2/R36-F2.
     (c) `plain_keys` — everything else (NOT matching `is_secret_key_name` AND NOT listed in `config_secret_keys`). Persist via `config_store.set_many(plain_keys, source="import")` inside the same outer transaction.
  2. The whole-replace and diff-import branches at lines 672 and 689 MUST both apply this three-bucket rule; the difference between them is only the pre-existing-rows handling, not the secret semantics.
  3. Include `requires_restart: True` in the success response whenever the persisted set TOUCHES `databases.falkordb.requirepass` (either as a re-encrypted plaintext value via bucket b, OR as a refreshed reference via bucket a — the live container still authenticates against whatever password it was originally launched with, so any change to the secret-store row needs a restart to flow to the docker-compose env).
  4. **Round-trip test (R37-F3 + R38-F3 — pinned to the live export surface):** export the full config bundle via `POST /api/config/export` (verified live at `src/gobby/servers/routes/configuration.py:600` — returns the full bundle including `config_store` plus `config_secret_keys`; the `GET /api/config/template` surface is a DIFFERENT route that returns masked YAML under a `content` field and is exercised by the F1 mask-preservation test instead). Feed the returned bundle directly to `POST /api/config/import` and assert that (a) `config_store.get("databases.falkordb.requirepass")` after the round-trip returns the SAME `$secret:requirepass` sentinel and `is_secret=1` flag, (b) the `secrets` table row for `requirepass` is byte-identical to the pre-export state (NOT re-encrypted with the sentinel as plaintext), and (c) `validate_falkordb_password` is never invoked during the round-trip. Add a parallel test for the legacy plaintext-import branch where `config_secret_keys` is missing AND the value is a literal password — assert the password is routed through validate + `set_secret` (per R38-F2's broadened bucket b), the raw DB shows `$secret:requirepass` + an encrypted `secrets` row, and the same invalid-password path returns HTTP 422 with no partial write.

Add route-level tests in `tests/servers/routes/test_configuration_routes.py` covering: (a) `GET /template` masks `requirepass` in the returned tree; (b) `PUT /template` with a `requirepass` value persists via `set_secret` (raw DB: `$secret:requirepass` + encrypted `secrets` row); (c) `POST /import` with a `requirepass` entry does the same; (d) `PUT /template` with whitespace `requirepass` returns 422 AND persists nothing; (e) all three success paths set `requires_restart: true` when `requirepass` is in the persisted set. Pack/unpack flow remains unchanged (R32-F2 removed the only pack.py Neo4j reference); the secret-detection contract for pack lives in the config_store-level guarantees set up by step 1 above.

**Pre-persist password validation (R27-F1) — owned executable instructions for routes + MCP:**

Both write surfaces (`/api/config/values` PUT in `src/gobby/servers/routes/configuration.py` AND `set_config` in `src/gobby/mcp_proxy/tools/config.py`) MUST call `validate_falkordb_password(value)` (imported as `from gobby.config.persistence import validate_falkordb_password`) BEFORE invoking `ConfigStore.set_secret(...)` whenever the key being written is `databases.falkordb.requirepass`. On `ValueError`:

- **Route handler:** return HTTP 422 (Unprocessable Entity) with body `{"detail": "<validator message>", "key": "databases.falkordb.requirepass"}`. Do NOT call `set_secret`. Do NOT include `requires_restart` on the failure response (that signal is reserved for successful changes that need a container restart). Add a route-level test that submits each rejected sample from § 1.1's test list (whitespace, tab, newline, control char, non-ASCII), asserts HTTP 422 with the exact validator message, and verifies `SELECT value FROM config_store WHERE key = 'databases.falkordb.requirepass'` is unchanged from its prior value (or absent if never set).
- **MCP tool:** return the standard `{"success": False, "error": "<validator message>"}` shape that the rest of `mcp_proxy/tools/config.py` uses for input-validation failures. Same pre-persist guarantee: nothing reaches `ConfigStore.set_secret` when validation fails. Add an `mcp_proxy/tools/test_config.py` test mirroring the route-level coverage. (This validation lives at the route + MCP boundary specifically — the FalkorConfig field_validator from § 1.1 also runs on `load_config(...)`, but failing at load time would surface a Pydantic ValidationError with no HTTP shape; the explicit validator call gives the operator a clean 422/MCP-error before any persistence happens.)

**Restart semantics (R13-F2):** updating `databases.falkordb.requirepass` via `/api/config/values` PUT or `gobby-config set_config` changes the daemon's auth source on the next `load_config(...)`, but the running FalkorDB Docker container is NOT auto-recreated — it continues to authenticate against the previous `--requirepass` value (set when it started). The operator MUST `gobby restart` (or `gobby start` after `gobby stop`) for `_services_start` (3.5) to recreate the container with the new password from `config.databases.falkordb.requirepass`. The route handler and MCP tool MUST surface this in the success response — set `requires_restart: true` on the response body (matching the existing live success field at `src/gobby/servers/routes/configuration.py:259`/`285`/`343`, NOT a new `restart_required` field) and include a human-readable hint like ``Run `gobby restart` for the new FalkorDB password to take effect on the running container.`` Add a route-level test that asserts both fields are present in the response body when the changed key is `databases.falkordb.requirepass`, and a parallel test for the `gobby-config set_config` MCP tool. Validation matrix #22 (8.3) exercises the end-to-end flow.

This task is a **prerequisite for any UI / API change that exposes the FalkorDB password value**. Without it, the new field is exposed as plaintext via every config surface.

**Acceptance:**

- 4.4.1 — Config secret-detection treats `requirepass` as a secret key alongside existing detected secrets (R38-F4: `_SECRET_SUFFIXES` includes the literal `"requirepass"`; `is_secret_key_name("databases.falkordb.requirepass")` returns True). file: `src/gobby/storage/config_store.py`.
- 4.4.2 — `/api/config/values` PUT validates and persists `databases.falkordb.requirepass` as a secret (route HTTP 422 on invalid passwords with no partial write; success response includes `requires_restart: true` and the restart hint). GET masks the value. file: `src/gobby/servers/routes/configuration.py`.
- 4.4.3 — `gobby-config set_config` and `get_config` MCP tools auto-detect secret keys via `is_secret_key_name` (no `is_secret=True` required); password validator runs before persistence; `get_config` masks secret keys on read. file: `src/gobby/mcp_proxy/tools/config.py`.
- 4.4.4 — `gobby-config get_config_section` masks every key satisfying `is_secret_key_name(key)` before returning the nested section; non-secret keys round-trip plaintext. file: `src/gobby/mcp_proxy/tools/config.py`.
- 4.4.5 — `gobby-config set_config_batch` partitions secret vs plain entries, validates `databases.falkordb.requirepass`, persists secrets via `ConfigStore.set_secret` and plain entries via `config_store.set_many`, all inside one `db.transaction()`; mid-batch failure rolls back both legs. file: `src/gobby/mcp_proxy/tools/config.py`.
- 4.4.6 — `GET /api/config/template` masks every `is_secret_key_name(key)` entry; `PUT /api/config/template` preserves the `"********"` sentinel as a no-op for secret entries (no validator call, no `set_secret` re-encryption, no `requires_restart` accounting) and otherwise applies the validate + transactional dual-write contract. file: `src/gobby/servers/routes/configuration.py`.
- 4.4.7 — `POST /api/config/import` partitions incoming entries into secret_reference / secret_value / plain buckets per R37-F3 and R38-F2 (broadened to include legacy plaintext-no-metadata path via `is_secret_key_name`); all three buckets persist inside the outer transaction. file: `src/gobby/servers/routes/configuration.py`.
- 4.4.8 — Export-then-import round-trip via `POST /api/config/export` -> `POST /api/config/import` leaves the `secrets` row for `requirepass` byte-identical and never invokes `validate_falkordb_password` on a `$secret:<name>` sentinel. behavior: "export-then-import round-trip preserves the requirepass secrets row unchanged" in `tests/servers/routes/test_configuration_routes.py`.

## P5 Phase 5: Web UI — Browser Components

`kind: framing`

**Goal**: Update the browser-side hooks, types, components, and tests to consume the renamed admin payload and reflect FalkorDB branding.

### 5.1 Rename Neo4jStatus to FalkorStatus and update useMemory hook + tests [category: code] (depends: 4.1)

`kind: deliverable`

Targets: `web/src/hooks/useMemory.ts:351-382`, `web/src/hooks/__tests__/useMemory.test.ts:258-264`

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

**Acceptance:**

- 5.1.1 — `FalkorStatus` TypeScript type and `useFalkorStatus` hook replace the Neo4j-named pair in `useMemory`. file: `web/src/hooks/useMemory.ts`.
- 5.1.2 — `useMemory` hook tests assert the renamed `FalkorStatus`-shaped payload. file: `web/src/hooks/__tests__/useMemory.test.ts`.

### 5.2 Update useDashboard type and SystemHealthCard pill [category: code] (depends: 4.1)

`kind: deliverable`

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

**Acceptance:**

- 5.2.1 — `useDashboard` type emits a `falkordb` status field; `SystemHealthCard` renders the new pill. file: `web/src/hooks/useDashboard.ts`.

### 5.3 Update MemoryPage hook ref and KnowledgeGraph empty-state copy [category: code] (depends: 5.1)

`kind: deliverable`

Targets: `web/src/components/memory/MemoryPage.tsx:2, 109, 137-148`, `web/src/components/memory/KnowledgeGraph.tsx:439`

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

**Acceptance:**

- 5.3.1 — `MemoryPage` references the renamed hook; KnowledgeGraph empty-state copy mentions FalkorDB. file: `web/src/components/memory/MemoryPage.tsx`.

## P6 Phase 6: Web UI — Ink Setup Wizard (in scope for 0.4.0)

`kind: framing`

**Goal**: Update the Ink-based setup wizard so `gobby setup` invokes the new CLI flags and persists state under the new field names. Manual end-to-end verification required before merge.

### 6.1 Update setup state.ts schema fields with one-shot migration [category: code] (depends: 3.4)

`kind: deliverable`

Targets: `web/src/setup/utils/state.ts`

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

**Acceptance:**

- 6.1.1 — Setup wizard `state.ts` schema renames `neo4j*` fields to `falkordb*` with a one-shot migration of existing persisted state. file: `web/src/setup/utils/state.ts`.

### 6.2 Update Services.tsx CLI flags (Docker-only) [category: code] (depends: 6.1)

`kind: deliverable`

Targets: `web/src/setup/steps/Services.tsx` (password guard lives here — see executable instructions below), `web/src/setup/steps/Launch.tsx` (summary edit co-located in this task — see "Launch.tsx summary edit" subsection), `web/src/setup/steps/__tests__/Services.test.tsx` (new test file owned by this task — see "Wizard test" subsection below)

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

**Custom-password validation flow (R27-F1) — owned executable instructions for the `[p]` branch:**

When the user picks `[p]` and types a custom password, the wizard MUST mirror the same charset rule that § 1.1's `validate_falkordb_password` enforces (printable ASCII, no whitespace / control / non-ASCII). Implement it as a TS-side guard `validateFalkorPassword(value: string): string | null` (returns the rejection message or null) inside `web/src/setup/steps/Services.tsx` itself — pin to a single co-located helper rather than a new utility module so this task's deliverable surface is deterministic. Extracting to a sibling utility is explicitly out of scope for §6.2; if a second wizard step ever needs the same guard, that's a follow-up refactor. On invalid input:

- Stay in the `password` phase. Do NOT advance to `installing`. Do NOT call `runGobby(...)`. Do NOT call `finish(...)`.
- Render the rejection message inline above the input field so the operator can edit and resubmit without leaving the wizard.
- Track the failure as wizard state (e.g. `passwordError: string | null`) so the message clears when the operator types a new value.

When the rule does pass, the wizard proceeds to `installing` and runs `runGobby(args)`. If the underlying CLI (§ 3.1) ALSO rejects the value (defense in depth — for instance the operator's environment somehow lets a non-ASCII paste through the TS layer), parse the `validate_falkordb_password` ValueError text from the CLI stderr, transition back to `password` with the same `passwordError` surface, and DO NOT call `finish(...)` or set `falkordb_password_set=true`. The wizard is the operator's primary install path, so a silent "done" on a rejected password is the worst possible UX — keep the user in the loop until either a valid password lands AND the install succeeds, or they explicitly back out via `[n]`.

**Wizard test (R28-F2) — owned by this task, concrete target/acceptance:**

Add a new test file at `web/src/setup/steps/__tests__/Services.test.tsx`. The Ink/React wizard codebase has no existing tests directory under the setup tree, so this is genuinely net-new — use the same test harness the rest of the web codebase already uses (the build script defined under `web/` runs it; do not introduce a parallel harness). Cover three cases:

- (a) `[p]` + a whitespace password keeps the user in the `password` phase, surfaces the rejection message inline, and does NOT call `runGobby(...)` or `finish(...)`.
- (b) `[p]` + a valid punctuation password proceeds to `installing` and then `done` with `falkordb_password_set=true` persisted.
- (c) The CLI-side rejection path (mock `runGobby` to return a `ValueError`-shaped stderr) bounces back to `password` without calling `finish(...)`.

The end-to-end manual verification in § 6.4 covers the live wizard run; this test pins the per-component logic so a future refactor cannot regress the silent-success path.

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

**Launch.tsx summary edit — co-located here (R13-F4):**

The Launch step's summary text references the same renamed state fields (`falkordb_installed`, `falkordb_password_set`) that this step writes, so the code edit lives here — NOT in 6.4 (which is `[test]` for end-to-end verification only). Co-locating ensures both TypeScript sources are present BEFORE the bundle regenerates in 6.3; the previous draft sequenced the Launch.tsx edit after bundle regen, leaving the bundle stale relative to the final TS sources.

Target: `web/src/setup/steps/Launch.tsx:211-212`

```typescript
// OLD:
- Neo4j: ${state.neo4j_installed ? "installed (Docker)" : "not installed"}
- Neo4j password: ${state.neo4j_password_set ? "custom" : state.neo4j_installed ? "auto-generated" : "n/a"}

// NEW:
- FalkorDB: ${state.falkordb_installed ? "installed (Docker)" : "not installed"}
- FalkorDB password: ${state.falkordb_password_set ? "custom" : state.falkordb_installed ? "auto-generated" : "n/a"}
```

**Acceptance:**

- 6.2.1 — `Services.tsx` emits `--falkordb-*` flags on the Docker-only path; the inline `validateFalkorPassword` guard rejects whitespace/control/non-ASCII input and keeps the user in the `password` phase until a valid value is supplied. file: `web/src/setup/steps/Services.tsx`.
- 6.2.2 — `Launch.tsx` summary uses the renamed `falkordb_installed` / `falkordb_password_set` state fields. file: `web/src/setup/steps/Launch.tsx`.
- 6.2.3 — `Services.test.tsx` covers the three password-validation branches (whitespace rejected stays in `password` phase; valid password reaches `done` with `falkordb_password_set=true`; mocked CLI rejection bounces back to `password` without `finish(...)`). file: `web/src/setup/steps/__tests__/Services.test.tsx`.

### 6.3 Regenerate the bundled setup.mjs artifact [category: code] (depends: 6.2)

`kind: deliverable`

Target: `src/gobby/install/shared/setup/setup.mjs` (regenerate), `web/package.json` (build script verification)

`gobby setup` does **not** execute `web/src/setup/**` directly — it runs the bundled artifact at `src/gobby/install/shared/setup/setup.mjs`. Editing the TypeScript sources in 6.1 + 6.2 is necessary but not sufficient; the bundle must be regenerated and checked in for the wizard changes to actually ship.

Steps:

1. Read `web/package.json` and identify the build script that produces `src/gobby/install/shared/setup/setup.mjs` (likely a `build:setup` or `bundle:setup` script using esbuild/tsup/rollup)
2. Run that build script: `cd web && npm run build:setup` (or whatever the actual command is — verify before running)
3. Confirm the output landed at `src/gobby/install/shared/setup/setup.mjs` (the path the daemon's setup launcher reads)
4. Diff the regenerated bundle against the prior version — should show string changes for the renamed flags/labels (Neo4j → FalkorDB) and any updated copy
5. Commit the regenerated bundle alongside the TS source edits in the same PR

If `web/package.json` does not have a script that produces `src/gobby/install/shared/setup/setup.mjs`, add one. The build must be reproducible from `npm run …` so future contributors do not have to reverse-engineer the bundling steps.

**Acceptance:**

- 6.3.1 — `setup.mjs` bundle regenerated to reflect the renamed schema and flags. file: `src/gobby/install/shared/setup/setup.mjs`.

### 6.4 Verify wizard end-to-end [category: test] (depends: 6.3)

`kind: deliverable`

Targets: `.gobby/setup_state.json`, `~/.gobby/setup_state.json` (wizard state files observed during verification), `state.ts` (bare-path reference cited in body)

**Verification only (R13-F4).** The Launch.tsx code edit moved into 6.2 so the bundle regenerated in 6.3 already contains the final TypeScript sources. This task observes results — do not code-edit here.

Manual verification:

0. **Bundle freshness check:** `git status src/gobby/install/shared/setup/setup.mjs` — confirm the regenerated bundle from 6.3 is staged. If it is not, the wizard will run the OLD bundle and none of the 6.1/6.2 changes will be observable. Re-run the build script before continuing.
1. `rm ~/.gobby/setup_state.json && gobby setup` — wizard runs cleanly from cold (note underscore in filename)
2. Walk through the Services step, accept the install → wizard advances to Launch with summary "FalkorDB: installed"; verify `docker compose -f ~/.gobby/services/docker-compose.yml ps` shows the FalkorDB container healthy; verify the FalkorDB Browser at `http://localhost:13000` loads; verify `gobby status` reports FalkorDB healthy
3. `gobby uninstall --falkordb` to clean up
4. Repeat the wizard with the **--password** path (`[p]` in the Y/N/P prompt); verify the custom password is accepted and persisted
5. **Migration test:** create a `~/.gobby/setup_state.json` with the old `neo4j_installed: true, neo4j_password_set: false` fields; rerun `gobby setup`; verify state.ts migrator rewrites it to `falkordb_*` fields and the wizard does not crash
6. **Bundle parity check:** rebuild the bundle one more time after all manual fixes; confirm `git diff src/gobby/install/shared/setup/setup.mjs` shows no further changes (i.e., the committed bundle matches what the build produces from the committed sources)

If wizard bitrot independent of this migration is uncovered during this verification (e.g., other steps fail), fix it as part of this task. The plan is to ship 0.4.0 with a working onboarding wizard.

**Acceptance:**

- 6.4.1 — Manual end-to-end run completes a fresh install through the Ink setup wizard against FalkorDB. behavior: "Ink setup wizard completes a fresh install end-to-end against FalkorDB" in `web/src/setup/`.

## P7 Phase 7: Rust gobby-cli — FalkorDB Read Client

`kind: framing`

**Goal**: Replace the Neo4j HTTP client in `crates/gcode/src/neo4j.rs` with a FalkorDB client using the official `falkordb` Rust crate; rename config; preserve all 8 read function signatures.

### 7.1 Replace Neo4jConfig with FalkorConfig in gobby-cli config.rs [category: code] (depends: 3.6)

`kind: deliverable`

Targets: `../gobby-cli/crates/gcode/src/config.rs:18-22, 51, 79, 431+`, `crates/gcode/src/config.rs` (bare path cited in body), `crates/gcode/src/secrets.rs` (secrets module touched by config rename), `neo4j.rs` (R36-F3 cross-reference — `mod neo4j;`, `Neo4jConfig`, `Context.neo4j`, and `resolve_neo4j_config` are PRESERVED by this task and deleted by §7.4; bare name cited in body to make the staged-rollout contract explicit)

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

In the `Context` struct, ADD `pub falkordb: Option<FalkorConfig>` ALONGSIDE the existing `pub neo4j: Option<Neo4jConfig>` field — DO NOT remove the `neo4j` field, the `Neo4jConfig` type, or `resolve_neo4j_config` yet (R36-F3). Removal lands in § 7.4 together with the `neo4j.rs` module deletion and the full callsite sweep.

In `Context::resolve`, ADD `let falkordb = resolve_falkordb_config(&db_path, quiet);` alongside the existing `let neo4j = resolve_neo4j_config(&db_path, quiet);` and update the struct literal to populate BOTH fields. The `neo4j` field remains read-only consumer territory until § 7.4 — § 7.2 compile-verification, § 7.3 query porting (writes through `Context.falkordb`), and all transitional reads (e.g. `with_neo4j` / `graph_boost` callers) continue to compile because both fields exist.

ADD the `resolve_falkordb_config` function alongside `resolve_neo4j_config`. **The helper names and patterns must match the live `gobby-cli` codebase, not invented ones** — the prior draft referenced `read_config_key` and `resolve_secrets`, neither of which exist. Verified live (via `grep` against `crates/gcode/src/config.rs` and `crates/gcode/src/secrets.rs`):

- `read_config_value(conn: &rusqlite::Connection, key: &str) -> Option<String>` — defined in `config.rs:413`. Returns the raw config_store value or `None` if missing.
- `secrets::resolve_config_value(value: &str, db_path: &Path) -> anyhow::Result<String>` — defined in `secrets.rs:94`. Resolves both `$secret:<name>` references (decrypts via Fernet) AND `${ENV_VAR}` patterns. The existing `resolve_neo4j_config` uses it via `match secrets::resolve_config_value(&v, db_path) { ... }` with a warning-on-error fallback; mirror that pattern.
- The existing `resolve_neo4j_config` builds raw values as `std::env::var(...).ok().or_else(|| read_config_value(&conn, "databases.neo4j.<key>"))` — env var precedence is enforced by short-circuiting `or_else` BEFORE the `?` operator. The prior draft inverted this (read config_store with `?` first, then applied env), which broke the env-only configuration path.

Correct shape:

```rust
fn resolve_falkordb_config(db_path: &Path, quiet: bool) -> Option<FalkorConfig> {
    // R19-F3: there is no `open_db_readonly` helper in the live config.rs.
    // Mirror the existing `resolve_neo4j_config` exactly — open SQLite read-only
    // via `rusqlite::Connection::open_with_flags(...)` and set a busy_timeout
    // so a long-running daemon writer does not deadlock the read client.
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ).ok()?;
    let _ = conn.busy_timeout(std::time::Duration::from_millis(500));

    // Env var > config_store > None. Use `or_else` so env wins WITHOUT short-circuiting
    // the config_store fallback; do NOT apply `?` until after env+config_store have both
    // been checked, otherwise env-only configuration silently fails.
    let host_raw: Option<String> = std::env::var("GOBBY_FALKORDB_HOST")
        .ok()
        .or_else(|| read_config_value(&conn, "databases.falkordb.host"));
    let port_raw: Option<String> = std::env::var("GOBBY_FALKORDB_PORT")
        .ok()
        .or_else(|| read_config_value(&conn, "databases.falkordb.port"));
    let password_raw: Option<String> = std::env::var("GOBBY_FALKORDB_PASSWORD")
        .ok()
        .or_else(|| read_config_value(&conn, "databases.falkordb.requirepass"));

    // Resolve $secret: / ${VAR} references for any value that came from config_store.
    // Mirror the existing resolve_neo4j_config error-handling: log + fall back to None
    // on resolve failure rather than aborting the whole client.
    let host = match host_raw {
        Some(raw) => match secrets::resolve_config_value(&raw, db_path) {
            Ok(v) => v,
            Err(e) => {
                if !quiet { eprintln!("warning: failed to resolve FalkorDB host: {e}"); }
                return None;
            }
        },
        None => return None,  // host is required; no graph backend without it
    };
    let port: u16 = match port_raw {
        Some(raw) => match secrets::resolve_config_value(&raw, db_path) {
            Ok(v) => v.parse().unwrap_or(16379),  // 16379 matches the docker-compose host-side mapping (3.2)
            Err(_) => 16379,
        },
        None => 16379,
    };
    let password = match password_raw {
        Some(raw) => match secrets::resolve_config_value(&raw, db_path) {
            Ok(v) => Some(v),
            Err(e) => {
                if !quiet { eprintln!("warning: failed to resolve FalkorDB password: {e}"); }
                None
            }
        },
        None => None,
    };

    Some(FalkorConfig {
        host,
        port,
        password,
        graph_name: "gobby_code".to_string(),  // Rust crate reads the code graph
    })
}
```

The Rust crate reads the **code** graph (`gobby_code`), not the memory KG (`gobby_kg`). Hardcode `graph_name = "gobby_code"`. Default port is `16379` (host-side mapping from the compose template in 3.2), not `6379` — the prior draft used `6379`, which would break against the actual installer.

**Test coverage** (mirror the existing Neo4j resolver tests in `config.rs`):

1. config_store-only host/port/password → resolved values returned
2. env-only `GOBBY_FALKORDB_HOST` / `GOBBY_FALKORDB_PORT` / `GOBBY_FALKORDB_PASSWORD` (no config_store rows) → env values used (this is the path the prior draft broke)
3. env override of an existing config_store host → env wins
4. config_store value of `$secret:requirepass` (with the encrypted secret seeded in `secrets`) → `resolve_config_value` decrypts to plaintext
5. Missing host → returns `None` (no FalkorDB client)

**Acceptance:**

- 7.1.1 — `FalkorConfig` Rust struct is ADDED alongside the existing `Neo4jConfig` in the `gobby-code` binary crate's config module. `Context` resolves and stores BOTH `falkordb: Option<FalkorConfig>` and `neo4j: Option<Neo4jConfig>` until § 7.4 removes the legacy surfaces; this additive-only contract is load-bearing because § 7.2's compile-verification step keeps `mod neo4j;` plus its callsites alive, and removing `Neo4jConfig` here would break that compile. file: `crates/gcode/src/config.rs`. (The crate is bin-only — `crates/gcode/Cargo.toml` declares `name = "gobby-code"` with a single `[[bin]] name = "gcode"` and `path = "src/main.rs"`; no `lib.rs` exists, so there is no `gobby_cli::*` library symbol path to bind acceptance to. The full Neo4j-side removal acceptance belongs in § 7.4, not here.)

### 7.2 Pin FalkorClient API shape and result-conversion contract [category: code] (depends: 7.1)

`kind: deliverable`

Targets: `../gobby-cli/crates/gcode/src/falkor.rs` (new — skeleton with real parser body), `crates/gcode/src/falkor.rs` (bare path cited in body), `../gobby-cli/crates/gcode/src/main.rs` (add `mod falkor;` ALONGSIDE existing `mod neo4j;` so the new file enters the module tree at this stage — R16-F3; do not remove `mod neo4j;` yet, that lives in § 7.4), `../gobby-cli/crates/gcode/Cargo.toml`, `crates/gcode/Cargo.toml` (bare path cited in acceptance), `../gobby-cli/Cargo.lock`, `Cargo.lock` (bare path cited in acceptance) (R30-F1 — `cargo add falkordb urlencoding` (or `cargo build` after editing `Cargo.toml`) rewrites this workspace lockfile; it MUST be staged and committed in the same task so the reproducible-build lockfile stays in lockstep with `Cargo.toml`. Without this, expansion can close §7.2 with a stale `Cargo.lock`, and `cargo build --release -p gobby-code` in fresh checkouts could either fail to resolve the new deps or pin different versions than the author tested. The §7.2 acceptance below names the file explicitly.), `../gobby-cli/crates/gcode/src/search/graph_boost.rs` (mutability change to `with_neo4j`), `secrets.rs` (bare path cited in body)

Pin the wrapper contract before porting any queries. The `falkordb` Rust crate (v0.2.x) does not match the loose `params: Option<serde_json::Value>` shape from the prior draft. Real constraints (verified against docs.rs for `falkordb` 0.2 — R12-F3):

- `SyncGraph` is **not thread-safe** and is used **mutably** in the official examples. The wrapper must hold `&mut SyncGraph`.
- `QueryBuilder::with_params(self, params: &'a HashMap<String, String>)` takes a **borrowed** map, not an owned one. The map MUST live until `execute()` is called — the borrow extends through the builder chain. Binding the params to a local `let params: HashMap<String, String> = ...;` BEFORE constructing the builder, then passing `&params`, is the correct pattern. Constructing the map inline as `with_params(p)` (consuming) does not compile against the v0.2 signature.
- All param values must be stringified at the call site (the map's value type is `String`, not arbitrary `serde_json::Value`). **Numeric values** (e.g., `$offset`, `$limit`, blast-radius depth) are NOT supported as typed parameters and must be interpolated as Cypher numeric literals into the query string after clamping at the call site (see 7.3 for the exact handling per query).
- `QueryBuilder::execute` returns `QueryResult<LazyResultSet<'_>>` — note the `'_` is a **lifetime**, not a type parameter. `QueryResult.header` is `Vec<String>` (column-name strings directly — NOT a Vec of structs with `.name` fields; the prior draft's `result.header.iter().map(|h| h.name.clone())` does not compile). Iterate as `result.header.iter().cloned()` or `result.header.clone()`. Iterating `result.data` yields `Vec<FalkorValue>` per record (positionally aligned with the header). `FalkorValue` is an enum (`String`, `I64`, `F64`, `Bool`, `Null`, `Array`, `Map`, `Node`, `Edge`, `Path`, `Point`).
- The current `with_neo4j` wrapper hands out `&Neo4jClient` (shared); the FalkorDB version hands out `&mut FalkorClient` to the closure. **The mutability lives on the local `FalkorClient` binding inside `with_falkor`, NOT on `Context`** — `Context` continues to store the immutable config (`FalkorConfig`) and `with_falkor` constructs a fresh client per call from that config (mirroring how the current `with_neo4j` constructs a `Neo4jClient` per call). Callers continue to pass `&Context`; no `&mut Context` cascade through `commands/graph.rs` or `search/graph_boost.rs`.

In `Cargo.toml`:

```toml
# Add:
falkordb = "0.2"
urlencoding = "2"  # R17-F2 — needed for password-bearing falkor:// URL construction in falkor.rs

# Keep reqwest (used by other code paths for embedding API)
# Keep base64 (used by crates/gcode/src/secrets.rs for Fernet key derivation
#   via base64::engine::general_purpose::URL_SAFE — verified live, not removable
#   as part of this transport swap)
```

The dependency diff for this task is **add only** — `falkordb` + `urlencoding`. Do NOT drop `base64`; despite being a Neo4j-era addition in spirit, the live secret-resolution module (`secrets.rs`) imports it for Fernet key derivation. Removing it breaks the gcode build before any FalkorDB code lands.

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
        // R17-F2: falkordb 0.2's FalkorConnectionInfo does NOT expose a public
        // new(host, port, password) constructor — its public construction paths
        // are TryFrom<&str> (URL parsing) and the Redis(redis::ConnectionInfo)
        // variant. Build via TryFrom on a percent-encoded `falkor://` URL so
        // the password is carried end-to-end through the connection string.
        // The installer's generated default password is secrets.token_urlsafe(24)
        // (URL-safe by construction); operator-supplied --falkordb-password
        // values may contain arbitrary characters, so encode unconditionally.
        let password = config.password.as_deref().unwrap_or_default();
        let url = format!(
            "falkor://:{}@{}:{}",
            urlencoding::encode(password),
            config.host,
            config.port,
        );
        let conn_info: FalkorConnectionInfo = url.as_str().try_into()?;
        let client = FalkorClientBuilder::new()
            .with_connection_info(conn_info)
            .build()?;
        let graph = client.select_graph(&config.graph_name);
        Ok(Self { graph })
    }

    /// Execute a Cypher statement. ALL string values placed in `params` MUST be
    /// pre-wrapped with `cypher_string_literal(s)` by the caller (R17-F3). The
    /// `with_params(map)` substitution is a textual `CYPHER key=value` prepend,
    /// NOT a typed parameter binding — passing a raw `gobby` value produces
    /// `CYPHER project=gobby` which Cypher parses as an identifier expression,
    /// not a string literal. Numeric and list params must be interpolated into
    /// the Cypher query string at the call site (the 8 read queries in § 7.3
    /// already do this for `$offset`, `$limit`, `$ids`, and blast-radius depth).
    ///
    /// Implementation note: `QueryBuilder::with_params` borrows the map (`&'a HashMap<String, String>`),
    /// so the map MUST live until `execute()` is called. Binding to a local before
    /// constructing the builder (rather than passing inline) is REQUIRED for the
    /// borrow to outlive the builder chain.
    pub fn query(
        &mut self,
        cypher: &str,
        params: Option<HashMap<String, String>>,
    ) -> anyhow::Result<Vec<Row>> {
        let result = match params.as_ref() {
            Some(p) => self.graph.query(cypher).with_params(p).execute()?,
            None => self.graph.query(cypher).execute()?,
        };
        Ok(parse_falkor_result(result))
    }
}

/// Encode a Rust string as a Cypher string literal: single-quoted, with
/// backslash and single-quote escaped. Required before adding any string value
/// to the `params` map handed to `FalkorClient::query` (R17-F3 — without this,
/// `with_params`'s textual `CYPHER key=value` substitution produces a Cypher
/// identifier instead of a string literal).
///
/// Built char-by-char (no Rust string-literal escapes in the body) to keep the
/// implementation explicit about what gets escaped: only the backslash and
/// the single-quote. Every other character passes through unchanged.
pub fn cypher_string_literal(s: &str) -> String {
    let mut out = String::new();
    out.push('\'');
    for ch in s.chars() {
        if ch == '\\' || ch == '\'' {
            out.push('\\');
        }
        out.push(ch);
    }
    out.push('\'');
    out
}

/// Map FalkorDB QueryResult records into Row dicts keyed by column alias.
/// Header order = record value order. `result.header` is `Vec<String>` (column
/// names directly — NOT structs with `.name` fields; verified against falkordb
/// 0.2 docs).
///
/// R16-F3 / R17-F2 carryover: this is the REAL compiling body (the prior
/// comments-only draft would not compile once `mod falkor;` joined the module
/// tree per § 7.2's updated target list).
fn parse_falkor_result(
    result: falkordb::QueryResult<falkordb::LazyResultSet<'_>>,
) -> Vec<Row> {
    let header: Vec<String> = result.header.clone();
    let mut rows: Vec<Row> = Vec::new();
    for record in result.data {
        let row: Row = header
            .iter()
            .zip(record.into_iter())
            .map(|(name, val)| (name.clone(), falkor_value_to_json(val)))
            .collect();
        rows.push(row);
    }
    rows
}

/// FalkorValue → serde_json::Value. Scalars map directly; Array/Map recurse;
/// Node/Edge/Path arms are filled in § 7.3 when porting actual queries (each
/// query knows the expected return shape, so the conversion is pinned to the
/// structural fields the caller actually consumes — `.labels()`, `.properties()`,
/// `.relationship_type()`, `.nodes()`, `.relationships()`).
fn falkor_value_to_json(v: FalkorValue) -> Value {
    match v {
        FalkorValue::String(s) => Value::String(s),
        FalkorValue::I64(i) => Value::Number(i.into()),
        FalkorValue::F64(f) => serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number),
        FalkorValue::Bool(b) => Value::Bool(b),
        // R22-F1: falkordb 0.2.x exposes the unit null variant as `FalkorValue::None`,
        // not `FalkorValue::Null`. Using the wrong identifier fails to compile against
        // the pinned crate version, breaking § 7.2's compile-pin guarantee for § 7.3.
        FalkorValue::None => Value::Null,
        FalkorValue::Array(items) => Value::Array(items.into_iter().map(falkor_value_to_json).collect()),
        FalkorValue::Map(entries) => {
            let map: serde_json::Map<String, Value> = entries
                .into_iter()
                .map(|(k, val)| (k, falkor_value_to_json(val)))
                .collect();
            Value::Object(map)
        }
        // Node/Edge/Path: § 7.3 fills these arms when porting actual queries.
        // Return Null until then so this module compiles standalone after § 7.2.
        _ => Value::Null,
    }
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

**Acceptance:**

- 7.2.1 — Rust `FalkorClient` API surface and result-conversion contract pinned (read-only client) in the `gobby-code` bin crate's falkor module. file: `crates/gcode/src/falkor.rs`. (Same bin-only crate caveat as 7.1.1 — `crates/gcode/` has no `lib.rs`; acceptance evidence is the file diff, not a library symbol path.)
- 7.2.2 — `falkordb` and `urlencoding` crates added to `crates/gcode/Cargo.toml`; `neo4j`-related Rust deps removed where they're no longer used. file: `crates/gcode/Cargo.toml`.
- 7.2.3 — `Cargo.lock` regenerated and committed alongside the `Cargo.toml` change so reproducible builds resolve the new dep set. file: `Cargo.lock`.

### 7.3 Port 8 read queries to FalkorClient [category: code] (depends: 7.2)

`kind: deliverable`

Targets: `../gobby-cli/crates/gcode/src/falkor.rs` (fill in query bodies), `crates/gcode/src/falkor.rs` (bare path cited in body). **Do NOT delete `crates/gcode/src/neo4j.rs` in this task (R14-F2)** — § 7.4 still references `mod neo4j;` and the callsites; deleting the file here would break `cargo build` before § 7.4 runs. The deletion + cargo verification belong at the END of § 7.4 after every callsite is rewritten.

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

**Param marshaling — explicit handling per query:**

The Falkor crate's `with_params(HashMap<String, String>)` only carries string values. Every existing query that uses non-string params must be rewritten to interpolate those values into the Cypher string at the call site (after clamping/validation, since these are query-shape values, not user data — no SQL-injection surface). The exact handling per query:

- **String IDs** (`$id`, `$project`, `$path`): wrap each value with `cypher_string_literal(s)` (the helper in § 7.2 — single-quotes and backslash-escapes) BEFORE inserting into the `with_params` map (R17-F3). `with_params` does a textual `CYPHER key=value` prepend, so a raw `gobby` value would produce `CYPHER project=gobby` (Cypher identifier), not `CYPHER project='gobby'` (string literal); pre-quoting at the call site makes the substitution a valid string-literal expression. For `$ids` lists in `find_callers_batch` and `find_callees_batch`, interpolate as a Cypher list literal directly into the query string after wrapping each id with `cypher_string_literal`. Example: `format!("target.id IN [{}]", ids.iter().map(|i| cypher_string_literal(i)).collect::<Vec<_>>().join(", "))` produces `target.id IN ['id1', 'id2', 'id3']`. Do NOT split into N queries — that defeats the batch-call performance goal.
- **`$offset` / `$limit`** in `find_callers`, `find_usages`, `find_callers_batch`, `find_callees_batch`: clamp to `[0, MAX_LIMIT]` (use the same constants the Neo4j path uses today), then interpolate directly into the Cypher as `SKIP <n> LIMIT <m>`. Drop the `$offset` / `$limit` Cypher param names entirely.
- **Blast-radius depth + limit** in `blast_radius_query(depth, limit)` (R19-F4): depth is already interpolated (clamped 1-5). Update the function to ALSO take a `limit` argument (clamp to `[1, MAX_LIMIT]`) and interpolate it as `LIMIT <n>` into the query string. DROP the `$limit` Cypher param name from the blast-radius query — leaving it unbound after numeric params are removed from `with_params` would fail at runtime. After this and the `$offset` / `$limit` interpolation above, NO `$offset`, `$limit`, or `$ids` placeholder may remain in any of the 8 ported FalkorDB read queries; add a query-string assertion in the unit tests that scans for those placeholder names and fails if any survive.
- **String project/id params**: pre-wrapped via `cypher_string_literal` at the call site (see the String IDs bullet above) — `with_params` then carries the already-quoted Cypher literal as-is.

After this change, `with_params` is only ever called with string entries (or omitted when there are no string params). Numeric clauses do not flow through param binding at all.

Keep `row_to_graph_result` unchanged (still maps `Row -> GraphResult` with field-precedence fallbacks). The `Row = HashMap<String, serde_json::Value>` type stays.

Keep all 7 existing unit tests in spirit — replace the `parse_v2_response` tests with equivalent tests for `parse_falkor_result` from 7.2. The `test_blast_radius_query_targets_stable_ids_and_all_target_labels` assertion logic stays in spirit (still verifies the query string contains the right interpolated bits), but the test caller MUST be updated (R22-F1) to pass the new `limit` argument: `blast_radius_query(depth, limit)` per the § 7.3 signature change above. Add one assertion verifying the resulting query string contains the interpolated `LIMIT <n>` clause AND that no `$limit` placeholder survives in the output — that pair pins both halves of the R19-F4 / R22-F1 contract so a regression to typed `$limit` binding is caught at unit-test time.

**File deletion deferred to § 7.4 (R14-F2):** `crates/gcode/src/neo4j.rs` stays in place until § 7.4 has rewritten every `mod neo4j;`, `use crate::neo4j;`, and `neo4j::` callsite. Deleting here would break `cargo build` for any agent that completes § 7.3 without § 7.4 immediately following.

**Acceptance:**

- 7.3.1 — All 8 Rust read queries are ported from Neo4j to `FalkorClient`. file: `crates/gcode/src/falkor.rs`.

### 7.4 Update gobby-cli callsites + full Rust source sweep [category: refactor] (depends: 7.3)

`kind: deliverable`

Targets: `../gobby-cli/crates/gcode/src/main.rs`, `main.rs`, `crates/gcode/src/search/graph_boost.rs`, `crates/gcode/src/search/semantic.rs:154`, `crates/gcode/src/commands/graph.rs`, `crates/gcode/src/index/indexer.rs`, `crates/gcode/src/commands/search.rs`, `crates/gcode/src/neo4j.rs` (the module deleted at the end of this section), `crates/gcode/src/config.rs` (R36-F3 — Context struct cleanup, resolve_neo4j_config + Neo4jConfig removal; preserved alongside FalkorConfig until this task). Enumerated callsites — known; the closing `rg` sweep below is authoritative, do not treat this enumeration as exhaustive.

**`Context` cleanup (R36-F3 — owned exclusively by this task):** the `Context.neo4j` field, the `Neo4jConfig` struct, and the `resolve_neo4j_config` function were kept ALONGSIDE the new `Context.falkordb` / `FalkorConfig` / `resolve_falkordb_config` surfaces in § 7.1 so § 7.2's compile verification could pass against the still-present `neo4j.rs` module and its callsites. This task removes the Neo4j-side surfaces in a single coordinated change:

1. REMOVE `pub neo4j: Option<Neo4jConfig>` from the `Context` struct.
2. REMOVE the `let neo4j = resolve_neo4j_config(&db_path, quiet);` binding from `Context::resolve` and drop the `neo4j: ...` entry from the struct literal.
3. DELETE the entire `resolve_neo4j_config` function (≈lines 292-351 of `crates/gcode/src/config.rs` against the pre-cleanup state).
4. DELETE the `Neo4jConfig` struct itself if no callsite outside `neo4j.rs` still references it after the per-callsite rewrites below land.

Run these four removals in this order, then proceed with the per-file rewrites below. The order matters because the per-callsite rewrites read from `ctx.falkordb` — after the field/function removals the type system catches any stale `ctx.neo4j` use as a compile error, which is the intended forcing function for the sweep.

In `crates/gcode/src/main.rs`, REMOVE `mod neo4j;` (R17-F4 — `mod falkor;` was already added in § 7.2 alongside `mod neo4j;` for compile verification; do NOT add a second `mod falkor;` here or you'll get a duplicate-mod compile error). After this edit, `main.rs` declares `mod falkor;` only. Also sweep this file's CLI help text, banner strings, and any user-facing `--help` output for `neo4j|Neo4j|NEO4J` and rewrite to `FalkorDB` — the binary's help output ships to operators.

In `crates/gcode/src/search/graph_boost.rs`:

- `use crate::neo4j;` → `use crate::falkor;`
- `with_neo4j` → `with_falkor` everywhere
- `&Context` parameters on `graph_boost` and `graph_expand` stay `&Context` (no mutability cascade — see 7.2)
- Test functions: `test_graph_boost_no_neo4j` → `test_graph_boost_no_falkor`, `test_graph_expand_no_neo4j` → `test_graph_expand_no_falkor`
- Test fixture `Context { neo4j: None, ... }` → `Context { falkordb: None, ... }`

In `crates/gcode/src/search/semantic.rs:154`: `neo4j: None` → `falkordb: None`.

In `crates/gcode/src/commands/graph.rs`: every `neo4j::` call → `falkor::`. The 6 callsites (lines ≈266, 267, 316, 317, 357, 392) keep the same function names so this is purely a path swap. The `ctx: &Context` parameters on `callers`, `usages`, `imports`, `blast_radius` stay unchanged — `with_falkor` accepts `&Context` and constructs the mutable client locally. Also rename every `ctx.neo4j.is_none()` / `.is_some()` availability check to `ctx.falkordb.is_none()` / `.is_some()` — these gate the user-facing "graph unavailable" hints, so missing one means a user with a healthy FalkorDB connection still sees the unavailable banner.

In `crates/gcode/src/index/indexer.rs` and `crates/gcode/src/commands/search.rs`: rename module references, type names, field accesses, log keys, and user-facing strings. Exact line set is enumerated by the closing `rg` sweep — do NOT trust this enumeration as exhaustive.

**Closing source sweep (R13-F5) — required before this task closes:**

```bash
rg -n 'neo4j|Neo4j|NEO4J' crates/gcode/src/
```

Every hit must be addressed. **Acceptable terminal state: zero hits under `crates/gcode/src/`** (R14-F3 — aligns with Phase 8.3 row #20 which mandates Rust-side residuals appear in `CHANGELOG` only). Historical context belongs in the Rust repo's `CHANGELOG.md` (Phase 9.2), not in `src/` comments — a "// renamed from neo4j" comment in source would still trip row #20 and block release.

Unacceptable: any leftover field, type, module path, log key, help string, availability check, OR historical comment under `crates/gcode/src/`. The `cargo` test suite catches typed/structural errors; the `rg` sweep catches stringly-typed hits the compiler will not.

**Final cargo verification (R14-F2):** after every callsite is rewritten and the residual sweep is clean, delete `crates/gcode/src/neo4j.rs` (deferred from § 7.3 to here so the file survives long enough for the callsite rewrites to compile against it). Then run:

```bash
cargo test -p gobby-code
cargo build --release -p gobby-code
```

Both must exit 0. If either fails, a `mod neo4j;`, `use crate::neo4j;`, or `neo4j::` reference was missed — find and rewrite it, then re-run.

**Acceptance:**

- 7.4.1 — All `gobby-cli` callsites use `FalkorClient`; full Rust source sweep removes residual Neo4j references. behavior: "ripgrep `Neo4j|neo4j` over Rust source returns zero hits" in `crates/`.

## P8 Phase 8: Cross-Repo Cutover Choreography

`kind: framing`

**Goal**: Define the operational dance for landing both repos atomically — merge ordering, validation matrix, CLI flag deprecation policy, runtime warnings for users on upgrade.

### 8.1 Document CLI flag deprecation policy [category: docs] (depends: 3.4, 4.3)

`kind: deliverable`

Targets: `CHANGELOG.md` (0.4.0 upgrade-notes section that documents the removed flags), `src/gobby/cli/install.py` (audit-only — verify the § 3.4 hidden-handler implementation matches the policy stated below; this section does NOT modify install.py code per R39-F1), `test_cli_falkordb.py` (audit-only cross-reference — § 3.4 owns this test file's deprecation-handler assertions; bare name cited in body), `test_install_coverage.py` (audit-only cross-reference — § 3.4 owns this file too; bare name cited in body)

**Policy decision: do not preserve `--neo4j-password` (install) or `--neo4j` (uninstall) as aliases.** Hard-fail with a clear migration error. Aliases obscure the cutover, leave dead code paths, and create confusion when users see both flags work but only one matches the running service.

The hidden Click-option handlers that implement this policy live in `src/gobby/cli/install.py` and are OWNED BY § 3.4 (see § 3.4's "Hidden deprecation handlers" subsection — R39-F1 moved the implementation there so § 3.4's test_cli_falkordb.py and test_install_coverage.py cases reference handlers that exist in the same commit). This section is policy + CHANGELOG documentation; it does not ship code.

When a user passes either deprecated flag, the § 3.4 handler emits:

```text
Error: --neo4j / --neo4j-password has been removed in this release.

The knowledge graph backend has been replaced with FalkorDB.
- Install (auto-runs as part of gobby install; tune with): gobby install [--falkordb-password <pw>] (or service-only: gobby install --falkordb)
- Uninstall: gobby uninstall --falkordb
- Migration notes: see CHANGELOG.md for the full upgrade path.
```

The actual deprecated surfaces (verified live in § 3.4's edit set):

- `gobby install --neo4j-password <value>` — was a Click option on `install`
- `gobby uninstall --neo4j` — was a Click flag on `uninstall`

**CHANGELOG entry — required (this section's deliverable):** add an upgrade-notes block to `CHANGELOG.md`'s 0.4.0 section that:

1. Names both removed flags verbatim (`--neo4j-password`, `--neo4j`).
2. Includes the exact replacement verbs (`gobby install [--falkordb-password <pw>]`, `gobby install --falkordb`, `gobby uninstall --falkordb`).
3. Explains the hard-fail rationale (aliases obscure the cutover; no silent backward compatibility).
4. Cross-references the FalkorDB migration notes elsewhere in the changelog.

**Residual-sweep allowlist (R20-F4 cross-reference):** the literal `neo4j` strings that § 3.4's hidden hard-fail handlers introduce are required (Click must register the old option names to produce the migration error). § 8.3 row #20's `rg` sweep allowlist therefore explicitly includes `src/gobby/cli/install.py`'s deprecation handlers — these refs are intentional, not stale. § 4.3's daemon-wide sweep also skips this file's deprecation block for the same reason.

**Tests (R21-F1 + R22-F4) — owned by this task:**

- `tests/cli/test_install_coverage.py` — add cases that pass `--neo4j-password <value>` to `install` and assert it raises `click.UsageError` containing the migration message above (replacement install verb: `gobby install --falkordb`; uninstall verb: `gobby uninstall --falkordb`). Mirror with `gobby uninstall --neo4j` raising the same shape of error. Replace any positive-path Neo4j install assertions with the FalkorDB equivalents.
- `tests/cli/test_install_prompts.py` — update prompt-string fixtures that mention Neo4j to reflect the FalkorDB language; cover the deprecation-error path through any wizard fallback so a user typing the old flag in an interactive prompt still hits the migration message.
- `tests/cli/test_cli_install.py` — replace `neo4j` flag references in CliRunner invocations with `falkordb`; preserve a small set of inverted test cases that pass the old flag names to verify the hidden options ARE still registered AND fire the migration error (otherwise § 8.3 row #3 silently regresses).
- `tests/cli/test_daemon_coverage.py` — drop assertions about `neo4j_password` plumbing on `start`/`restart`; add the FalkorDB equivalents, and cover the restart-required behavior from § 3.5 (R13-F2: setting `databases.falkordb.requirepass` post-install requires `gobby restart` for `_services_start` to pick up the new value).
- `tests/runner_helpers.py` — update fixtures that fabricate runner state to swap any `neo4j_*` kwargs/attributes for the `falkordb_*` equivalents. This is a fixture file, not a test file — its imports flow into everything in `tests/cli/`, `tests/servers/`, and `tests/utils/`, so a stale import here breaks the entire pytest collection step before any test body runs.

These are the tests covered by § 8.3 row #4's expanded `tests/cli/` scope per R22-F4 — without them, the collection step fails on import errors against the deleted `Neo4jConfig` / `_neo4j_client` / `is_neo4j_installed` surfaces.

**Acceptance:**

- 8.1.1 — CHANGELOG.md 0.4.0 upgrade-notes block documents the hard-fail deprecation of `--neo4j-password` (install) and `--neo4j` (uninstall), names the FalkorDB replacement verbs (`gobby install [--falkordb-password <pw>]`, `gobby install --falkordb`, `gobby uninstall --falkordb`), and cross-references the FalkorDB migration notes. file: `CHANGELOG.md`. The Click handler implementation itself lives in `src/gobby/cli/install.py` and is owned by § 3.4 (R39-F1 — moved out of § 8.1 so § 3.4's test cases can reference handlers that exist in the same commit).

### 8.2 Add startup-time stale-config warning [category: code] (depends: 3.6, 4.3)

`kind: deliverable`

Targets: `src/gobby/runner_init/storage.py` (R34-F1 — `_check_stale_neo4j_config(runner.database)` is called from `init_storage_and_config` BEFORE the final `load_config` at line 89, so the migrated in-memory `runner.config` reflects the cleaned-up state by the time `_init_memory_stack` constructs `MemoryManager` / `CodeGraph`), `src/gobby/runner_init/services.py` (legacy target — `_check_stale_neo4j_config` is NOT called from here; preserved in Targets only for the body's references to surrounding services-init prose), `src/gobby/runner.py` (warning surface cited in acceptance)

After the config_store migration in 3.6 has run, add a startup check that detects leftover Neo4j-shaped config and surfaces it. The migration in 3.6 deletes `databases.neo4j.*` keys, but defensive in-depth: if for any reason those keys are still present (e.g., user restored an old DB backup, ran an out-of-band tool), the daemon should warn.

```python
def _check_stale_neo4j_config(db: LocalDatabase) -> None:
    """Detect and clear stale Neo4j config_store + secret entries at startup.

    Defense-in-depth against the 3.6 migration being skipped (e.g., user restored
    an old DB backup). Mirrors the migration's three-step orphan-safe cleanup:
      1. Migrate behavior tunables (preserve user tuning across the swap)
      2. Drop `databases.neo4j.*` config_store rows (including the $secret:auth ref)
      3. Drop `secrets.name = 'auth'` IFF no remaining config row references it

    R14-F1: do NOT use `ConfigStore.clear_secret("databases.neo4j.auth", ...)` here.
    `clear_secret` deletes `secrets.name = 'auth'` unconditionally — if a non-Neo4j
    config key still resolves to last-segment `auth` (its `config_store.value` JSON-
    encoded as `'"$secret:auth"'`), runtime cleanup would silently delete the live
    secret. Use the same `json_quote('$secret:auth')` orphan guard as 3.6's R13-F1
    fix; the runtime path is raw-SQL for symmetry with the migration path.
    """
    # R19-F1: use the live LocalDatabase API. `db.connect()` does not exist;
    # the read path is `db.fetchall(...)` and the write path is `db.execute(...)`
    # inside a `with db.transaction():` block (which provides BEGIN/COMMIT/ROLLBACK
    # semantics; all `db.execute` calls inside it participate in one transaction).
    rows = db.fetchall(
        "SELECT key FROM config_store WHERE key LIKE 'databases.neo4j.%'"
    )
    if not rows:
        return
    keys = ", ".join(r[0] for r in rows)
    logger.warning(
        "Detected stale Neo4j config keys (%s) — these are no longer used. "
        "Run `gobby install --falkordb` to set up FalkorDB. "
        "Cleaning them up now.",
        keys,
    )
    NEO4J_TUNABLE_KEYS = ("graph_search", "graph_min_score", "rrf_k", "graph_name")
    with db.transaction():
        # 1. Migrate user-tuned behavior values (mirror 3.6's policy)
        for key in NEO4J_TUNABLE_KEYS:
            db.execute(
                "INSERT OR IGNORE INTO config_store (key, value, source, is_secret) "
                "SELECT REPLACE(key, 'databases.neo4j.', 'databases.falkordb.'), value, source, is_secret "
                "FROM config_store WHERE key = ?",
                (f"databases.neo4j.{key}",),
            )
        # 2. Drop all stale databases.neo4j.* keys (including the $secret:auth reference row)
        db.execute(
            "DELETE FROM config_store WHERE key LIKE 'databases.neo4j.%'"
        )
        # 3. Drop orphaned `auth` secret only when no surviving config row references it.
        #    Same JSON-encoded guard as 3.6 (R13-F1) — bare `value = '$secret:auth'` matches nothing.
        db.execute(
            "DELETE FROM secrets WHERE name = 'auth' "
            "AND NOT EXISTS (SELECT 1 FROM config_store WHERE value = json_quote('$secret:auth'))"
        )
```

**Call site — pinned to `init_storage_and_config` BEFORE the final `load_config` (R34-F1):** call `_check_stale_neo4j_config(runner.database)` inside `src/gobby/runner_init/storage.py::init_storage_and_config(...)` AFTER `runner.database = init_hub_database(runner.config)` runs the migration registry (which includes the § 3.6 one-shot migration) but BEFORE the final `runner.config = load_config(config_file=..., secret_resolver=runner.secret_store.get, config_store=runner.config_store)` call at `init_storage_and_config:89`. Placing the cleanup after the migration runner ensures the § 3.6 migration has already had its first-pass shot at the rows; placing it before the final `load_config` ensures the in-memory `runner.config` reflects the cleaned-up state for everything downstream (`_init_memory_stack`, `MemoryManager`, `CodeGraph`). Calling this AFTER `load_config` would leave the running daemon constructing services from the pre-cleanup config — the "preserve user tuning" guarantee would only take effect after the next restart. The `SecretStore` parameter is intentionally absent — the orphan-safe cleanup runs in raw SQL against the `secrets` table directly, mirroring 3.6's migration-time pattern. (Note: do NOT add a `runner.config = load_config(...)` reload after the cleanup either; placing the cleanup BEFORE the existing final `load_config` is sufficient and avoids a second config-load round-trip per startup.)

**Sequencing regression test (R34-F1):** seed a DB with `databases.neo4j.rrf_k='80'` (legacy user-tuned) AND `databases.falkordb.host`/`port`/`requirepass` set (valid FalkorDB credentials). Drive `init_storage_and_config(runner, ...)` end-to-end and assert: (a) `runner.config.databases.falkordb.rrf_k == 80` (the migrated tunable is visible in the in-memory config BEFORE `_init_memory_stack` runs), AND (b) the `databases.neo4j.rrf_k` row is gone from `config_store`. Without this sequencing check, a regression that moves the cleanup back to AFTER `load_config` would silently restore the "two-restart problem" the warning was meant to close.

**Regression test (R14-F1):** seed the DB with `databases.neo4j.auth` referencing `$secret:auth` AND a synthetic non-Neo4j key (e.g., `mock.test.auth`) ALSO holding JSON-encoded `'"$secret:auth"'`. Call `_check_stale_neo4j_config(db)` and assert: (a) `databases.neo4j.auth` row is gone, (b) the synthetic row is preserved, (c) `secrets.name = 'auth'` is preserved (orphan guard correctly detected the surviving reference). Mirrors the migration regression test from 3.6.

**Acceptance:**

- 8.2.1 — Daemon emits a startup stale-config warning when `databases.neo4j.*` keys remain in config_store after the migration. file: `src/gobby/runner.py`.

### 8.3 Define cross-repo validation matrix and merge ordering [category: test] (depends: 8.1, 8.2, Phase 5, 6.4, Phase 7)

`kind: deliverable`

Targets: `CHANGELOG.md` (the validation matrix is documented in the Python repo's 0.4.0 changelog entry — Phase 9.1), `.gobby/services/docker-compose.yml` (compose file observed during the matrix runs), `tests/cli/test_cli_install.py`, `tests/cli/test_install_coverage.py`, `tests/cli/test_install_prompts.py` (pytest scopes covered by row #4 of the matrix). This task is operational/manual work; it observes and documents — it does not code-edit.

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
| --- | --- | --- |
| 1 | `gobby install` from clean `~/.gobby` (FalkorDB auto-installs alongside Qdrant per 3.4) | Exits 0; `docker compose -f ~/.gobby/services/docker-compose.yml ps` shows the `falkordb` profile healthy; FalkorDB Browser at `http://localhost:13000` loads; `gobby status` reports FalkorDB healthy |
| 2 | `gobby install --falkordb` (service-targeting only) from clean `~/.gobby` | Exits 0; only the FalkorDB container is started (no CLI hooks/git/embedding/voice run); `docker compose -f ~/.gobby/services/docker-compose.yml ps` shows the falkordb profile healthy |
| 3 | `gobby install --neo4j-password foo` AND `gobby uninstall --neo4j` (deprecated flags) | Both hard-fail with the migration message from 8.1 |
| 4 | `GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/ tests/cli/ tests/code_index/ tests/config/ tests/utils/ tests/servers/routes/ tests/mcp_proxy/ tests/test_runner_lifecycle.py tests/test_runner_shutdown.py --cov=gobby --cov-fail-under=80 --cov-report=term-missing` (the `GOBBY_TEST_PROTECT=1` prefix is mandatory per CLAUDE.md — it isolates the test suite from the user's running daemon and local daemon state) | Exit 0 (R22-F4 + R23-F2 + R31-F1: scope expanded again — added `tests/mcp_proxy/` (covers `test_memory_tools_kg.py`'s Neo4j patches), plus the two top-level runner test files where the dual-client clear (R23-F1.a) and shutdown ordering (R23-F1.b) are observable. Test ownership: § 1.1, § 2.1, § 4.1, § 4.3, § 8.1 — each section explicitly owns its assigned files; `tests/conftest.py` and `tests/runner_helpers.py` are upstream-fixture concerns and bring the rest of the matrix up cleanly. The `--cov-fail-under=80` flag enforces the threshold; without it `pytest` only reports coverage when explicitly requested per this repo's `pyproject.toml`.) |
| 5 | `uv run mypy src/gobby/memory/ src/gobby/code_index/ src/gobby/cli/installers/` | Exit 0 |
| 6 | `uv run ruff check src/` | Exit 0 |
| 7 | `cd web && npm run type-check && npm run test && npm run build` | All exit 0 (script names per `web/package.json`: `type-check`, `test`, `build`) |
| 8 | `gobby setup` end-to-end (clean state) | Wizard completes; FalkorDB installed via Docker; `setup_state.json` shows `falkordb_*` fields populated |
| 9 | `gobby setup` end-to-end with `[p]` (custom password) | Wizard accepts password; persisted in config_store + bootstrap; `docker compose -f ~/.gobby/services/docker-compose.yml exec -T falkordb redis-cli -a <pw> PING` returns `PONG` (use `exec -T` not host-side `-p 16379` so the test cannot be misled by an unrelated host-side Redis on port 16379; `-T` disables TTY allocation so the command works under noninteractive shells / CI / scripts) |
| 10 | `gobby setup` migration test (pre-existing `neo4j_*` state file) | Wizard rewrites to `falkordb_*` without crashing |
| 11 | `cargo test -p gobby-code && cargo build --release -p gobby-code` | Exit 0 |
| 12 | `gcode index .` against a known fixture project, then `gcode callers <known_function>` | Returns expected callers; results match a saved fixture diff |
| 13 | `gcode blast-radius <known_function>` | Returns expected transitive callers |
| 14 | `gcode search "<query>"` with graph-boost enabled | Returns ranked results with graph-boosted entries |
| 15 | Browser memory page loads, 3D graph renders, dashboard shows "FalkorDB connected" pill | Visual confirmation in browser |
| 16 | Daemon restart with stale `databases.neo4j.*` keys present in config_store | Logs the warning from 8.2 and deletes the keys |
| 17 | Pre-seed user-tuned values: `INSERT INTO config_store (key, value) VALUES ('databases.falkordb.rrf_k', '80')` (and `graph_min_score`=0.7). Then run `gobby uninstall --falkordb` from a non-services-dir CWD (e.g., `cd /tmp && gobby uninstall --falkordb` — exercises the R31-F2 anchored-compose-down path), and check `~/.gobby/bootstrap.yaml` plus `SELECT key FROM config_store WHERE key LIKE 'databases.falkordb.%'` plus `docker compose -f ~/.gobby/services/docker-compose.yml ps`. | `falkordb_password` key is removed from bootstrap (R5-F1 split-brain fix). Connection/auth keys removed: `databases.falkordb.host`, `databases.falkordb.port`, `databases.falkordb.requirepass`. **Secret name (R14-F4):** `config_key_to_secret_name('databases.falkordb.requirepass')` resolves to `requirepass` (NOT `auth` — `auth` is the Neo4j-era secret name). Verify with `SELECT name FROM secrets WHERE name = 'requirepass'` returning zero rows after uninstall. **Tunables preserved (R7-F3 contract):** `databases.falkordb.rrf_k` still equals `80`, `databases.falkordb.graph_min_score` still equals `0.7` — uninstall does NOT use a blanket `DELETE WHERE key LIKE 'databases.falkordb.%'`. **Container actually stopped (R31-F2):** `docker compose -f ~/.gobby/services/docker-compose.yml ps` shows zero rows for the `falkordb` service (or rows with state `exited`/no rows at all if the project was fully torn down). A bare `docker compose --profile falkordb down` from a non-services-dir CWD would silently no-op, leaving the container running while config/bootstrap is cleared — this check pins the explicit-`-f` path. |
| 18 | **Upgrade path:** seed `~/.gobby/services/docker-compose.yml` with the OLD Neo4j-era template (or use a real existing-install fixture), bring up the `neo4j` profile against that OLD compose file so a Neo4j container is actually running, then run `gobby install` from a non-services-dir CWD (e.g., `cd /tmp && gobby install`). Verify with `docker compose -f ~/.gobby/services/docker-compose.yml ps` (against the REFRESHED compose) plus an inspection of any container originally on the `neo4j` profile, and `docker compose -f ~/.gobby/services/docker-compose.yml exec -T falkordb redis-cli -a <pw> PING` (the `-T` flag disables TTY allocation so this works under noninteractive subprocess execution — the install path will fail without it). | Compose file is refreshed to the FalkorDB template (R6-F1 fix); the previously-running Neo4j container is stopped via the R31-F2 anchored-compose-down (verified by `docker ps -a --filter "name=neo4j"` showing the container in `exited` state, NOT `running`); no orphans remain; FalkorDB container starts on the `falkordb` profile and reaches healthy state; `redis-cli ... PING` returns `PONG`. The non-services-dir CWD is load-bearing: a bare `docker compose --profile neo4j down` in `_refresh_unified_compose` from `/tmp` would silently no-op against the absent CWD-relative `docker-compose.yml`, leaving the Neo4j container running alongside the new FalkorDB container. The explicit-`-f` form in R31-F2 is what makes this row pass. |
| 19 | Set `databases.falkordb.requirepass` via every config write surface and read it back via every config read surface: (a) `/api/config/values` PUT then GET; (b) `gobby-config set_config` (no `is_secret=True`) then `get_config`; (c) `gobby-config set_config_batch` with a mixed `[rrf_k, requirepass]` batch (no per-entry `is_secret`) then `get_config_section("databases.falkordb")`. All four surfaces exercise the §4.4 Step B auto-detect path. | All surfaces return the value masked (`********`); `get_config_section` returns the requirepass entry masked while `rrf_k` round-trips plaintext; raw DB inspection (`sqlite3 ~/.gobby/gobby-hub.db`) shows `$secret:requirepass` in `config_store` and the encrypted value in `secrets` regardless of which write surface persisted it (R6-F2 + R35-F1 verify `requirepass` is treated as a secret across all four supported MCP/HTTP surfaces). |
| 20 | `rg -l 'neo4j\|Neo4j\|NEO4J' src/gobby/ web/src/ tests/` (Python repo, R22-F4: `tests/` added) AND `rg -l 'neo4j\|Neo4j\|NEO4J' crates/` (run from gobby-cli repo root) | Only intentional refs remain. Python source allowlist: bootstrap migration helper (3.5), storage migration that drops old keys (3.6), `src/gobby/cli/install.py`'s hidden `--neo4j` / `--neo4j-password` deprecation handlers (R20-F4 — Click must register the literal old flag names to produce § 8.1's hard-fail migration error), CHANGELOG. Tests allowlist (R22-F4): `tests/cli/test_install_coverage.py`, `tests/cli/test_install_prompts.py`, `tests/cli/test_cli_install.py` may legitimately reference `--neo4j` / `--neo4j-password` to exercise the deprecation hard-fail path (§ 8.1's tests own those refs); migration-test fixtures in `tests/storage/` may seed the OLD `databases.neo4j.*` keys to verify the 3.6 + 8.2 cleanup. All other `tests/` references must be gone. Rust allowlist: CHANGELOG only. |
| 21 | **Step-6 failure path** (R11-F2): chmod `~/.gobby/bootstrap.yaml` to read-only after staging the install (or move the parent dir to read-only mid-flight via a test fixture), then run `gobby install --falkordb`. The container is up and `_update_config` succeeds, but `_write_bootstrap_password` fails. | Installer returns `success: False` (NOT `success: True` with a warning) AND the error message names `gobby uninstall --falkordb` as the cleanup verb AND `compose_running: True` is set in the result dict so the wizard/CLI can surface the right operator action. Verifies the installer does not inherit the `bootstrap_ok` warning-on-failure pattern from the live Neo4j installer. |
| 22 | **Password update + restart** (R13-F2): with FalkorDB installed and running, set `databases.falkordb.requirepass` to a NEW value via `/api/config/values` PUT. WITHOUT restarting, run `docker compose -f ~/.gobby/services/docker-compose.yml exec -T falkordb redis-cli -a <NEW_pw> PING` — must return `WRONGPASS`/`NOAUTH` (container still on old password). Then `gobby restart`, repeat — must return `PONG` (container recreated by `_services_start` with the new value from `config.databases.falkordb.requirepass`). Repeat the same flow via `gobby-config set_config` (no `is_secret=True`). | First PING returns `WRONGPASS`/`NOAUTH`; restart succeeds; second PING returns `PONG`. The `/api/config/values` PUT response and the `gobby-config set_config` response BOTH include `requires_restart: true` plus a human-readable hint when the changed key is `databases.falkordb.requirepass`. Verifies `_services_start` (3.5) sources the resolved `config.databases.falkordb.requirepass` rather than the stale `bootstrap.falkordb_password`, and that the runtime config surface tells the operator to restart. |
| 23 | **Password charset validator (R25-F1):** drive every ingress with one accepted password (e.g. `Pa$$w0rd!`) and one rejected password from each banned class — empty string, whitespace (`hunter 2`), tab (`a\tb`), control char (`a\x01b`), high-bit non-ASCII (`café`). For each rejected sample, attempt the value via `gobby install --falkordb-password <value>`, the wizard `[p]` custom-password handler, `/api/config/values` PUT for `databases.falkordb.requirepass`, and `gobby-config set_config` (no `is_secret=True`). | All accepted samples persist successfully; the round-trip in row #19 confirms the masked retrieval. All rejected samples surface the validator's `ValueError` message at the appropriate layer (CLI exit 1 with stderr text, wizard inline re-prompt, HTTP 422 from `/api/config/values`, MCP error from `gobby-config`); none of the rejected values are persisted (verify `SELECT value FROM config_store WHERE key = 'databases.falkordb.requirepass'` after each rejected attempt — value must be unchanged from the prior accepted state). Additionally, with the accepted punctuation password installed, `docker compose ... exec -T falkordb redis-cli -a "<pw>" PING` returns `PONG` — confirms the quoted-healthcheck change in § 3.2 round-trips operator-realistic passwords through the shell boundary. |

If any check fails, that branch does not merge. Fix and re-run the full matrix.

**Operator messaging:** the 0.4.0 release notes (Phase 9.1 CHANGELOG) must lead with the upgrade steps and a clear statement that Neo4j data is not migrated. Do not bury this in a "Notes" section.

**Acceptance:**

Per Plan-Coverage Contract table-row decomposition: the validation matrix above enumerates 23 independent rows of work; each gets a stable acceptance item below (8.3.1 documents the matrix and merge ordering themselves; 8.3.2 through 8.3.24 cover one matrix row each, indexed against the row number in the table). The acceptance kind on each row item is `behavior` — these are manual operator checks observed against a live FalkorDB instance, not file diffs.

- 8.3.1 — Cross-repo validation matrix and merge ordering documented inline in this plan. behavior: "validation matrix and merge ordering documented" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.2 — Matrix row 1 passes: `gobby install` from clean `~/.gobby` exits 0; `docker compose -f ~/.gobby/services/docker-compose.yml ps` shows the FalkorDB profile healthy; the FalkorDB Browser at `http://localhost:13000` loads; `gobby status` reports FalkorDB healthy. behavior: "matrix row 1 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.3 — Matrix row 2 passes: `gobby install --falkordb` from clean `~/.gobby` exits 0 with only the FalkorDB container started (no CLI hooks/git/embedding/voice). behavior: "matrix row 2 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.4 — Matrix row 3 passes: `gobby install --neo4j-password foo` and `gobby uninstall --neo4j` both hard-fail with the § 8.1 migration message. behavior: "matrix row 3 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.5 — Matrix row 4 passes: `GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/ tests/cli/ tests/code_index/ tests/config/ tests/utils/ tests/servers/routes/ tests/mcp_proxy/ tests/test_runner_lifecycle.py tests/test_runner_shutdown.py --cov=gobby --cov-fail-under=80 --cov-report=term-missing` exits 0 (the `GOBBY_TEST_PROTECT=1` prefix is mandatory per CLAUDE.md). behavior: "matrix row 4 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.6 — Matrix row 5 passes: `uv run mypy src/gobby/memory/ src/gobby/code_index/ src/gobby/cli/installers/` exits 0. behavior: "matrix row 5 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.7 — Matrix row 6 passes: `uv run ruff check src/` exits 0. behavior: "matrix row 6 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.8 — Matrix row 7 passes: `cd web && npm run type-check && npm run test && npm run build` all exit 0. behavior: "matrix row 7 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.9 — Matrix row 8 passes: `gobby setup` end-to-end (clean state) completes; FalkorDB installed via Docker; `setup_state.json` shows `falkordb_*` fields populated. behavior: "matrix row 8 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.10 — Matrix row 9 passes: `gobby setup` end-to-end with `[p]` (custom password) accepts the password; persists in config_store + bootstrap; `docker compose ... exec -T falkordb redis-cli -a <pw> PING` returns `PONG`. behavior: "matrix row 9 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.11 — Matrix row 10 passes: `gobby setup` migration test (pre-existing `neo4j_*` state file) rewrites to `falkordb_*` without crashing. behavior: "matrix row 10 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.12 — Matrix row 11 passes: `cargo test -p gobby-code && cargo build --release -p gobby-code` exits 0. behavior: "matrix row 11 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.13 — Matrix row 12 passes: `gcode index .` against a fixture project then `gcode callers <known_function>` returns expected callers matching the saved fixture diff. behavior: "matrix row 12 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.14 — Matrix row 13 passes: `gcode blast-radius <known_function>` returns expected transitive callers. behavior: "matrix row 13 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.15 — Matrix row 14 passes: `gcode search "<query>"` with graph-boost enabled returns ranked results with graph-boosted entries. behavior: "matrix row 14 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.16 — Matrix row 15 passes: browser memory page loads, 3D graph renders, dashboard shows "FalkorDB connected" pill (visual confirmation). behavior: "matrix row 15 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.17 — Matrix row 16 passes: daemon restart with stale `databases.neo4j.*` keys present logs the § 8.2 warning and deletes the keys. behavior: "matrix row 16 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.18 — Matrix row 17 passes: pre-seeded user-tuned `databases.falkordb.rrf_k=80` and `graph_min_score=0.7` survive `gobby uninstall --falkordb`; connection/auth keys (`host`, `port`, `requirepass`) plus `falkordb_password` in bootstrap are removed; `secrets.name = 'requirepass'` returns zero rows. behavior: "matrix row 17 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.19 — Matrix row 18 passes: seeded OLD Neo4j-era compose file followed by `gobby install` refreshes the compose file to the FalkorDB template; the Neo4j container is stopped; the FalkorDB container starts and `redis-cli -a <pw> PING` returns `PONG`. behavior: "matrix row 18 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.20 — Matrix row 19 passes across ALL four MCP/HTTP config surfaces (R37-F2): `databases.falkordb.requirepass` set via (a) `/api/config/values` PUT, (b) `gobby-config set_config` (no `is_secret=True`), (c) `gobby-config set_config_batch` with a mixed `[rrf_k, requirepass]` batch (no per-entry `is_secret`), then read via (d) `/api/config/values` GET, (e) `gobby-config get_config`, and (f) `gobby-config get_config_section("databases.falkordb")`. All read surfaces return the `requirepass` value masked (`********`) while `rrf_k` round-trips plaintext under `get_config_section`. After EACH of the three write surfaces, raw DB inspection (`sqlite3 ~/.gobby/gobby-hub.db`) shows `$secret:requirepass` in `config_store` and the encrypted value in `secrets`. behavior: "matrix row 19 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.21 — Matrix row 20 passes: `rg -l 'neo4j|Neo4j|NEO4J' src/gobby/ web/src/ tests/` (Python) and `rg -l 'neo4j|Neo4j|NEO4J' crates/` (gobby-cli) yield only the documented allowlist entries (bootstrap migration helper, storage migration, hidden deprecation handlers in `install.py`, CHANGELOG, and the migration-fixture tests under `tests/cli/` and `tests/storage/`). behavior: "matrix row 20 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.22 — Matrix row 21 passes: chmod read-only `~/.gobby/bootstrap.yaml` after staging the install, then `gobby install --falkordb` returns `success: False` (NOT True-with-warning); the error message names `gobby uninstall --falkordb` as the cleanup verb; `compose_running: True` is set in the result dict. behavior: "matrix row 21 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.23 — Matrix row 22 passes: setting `databases.falkordb.requirepass` to a NEW value via `/api/config/values` PUT without restart yields `WRONGPASS`/`NOAUTH` on `redis-cli ... PING`; after `gobby restart`, the second PING returns `PONG`; both responses (`/api/config/values` PUT and `gobby-config set_config`) include `requires_restart: true` plus a hint. behavior: "matrix row 22 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.
- 8.3.24 — Matrix row 23 passes: password charset validator round-trip — accepted password (`Pa$$w0rd!`) persists through `gobby install --falkordb-password`, the wizard `[p]` path, `/api/config/values` PUT, and `gobby-config set_config`; rejected samples (empty, whitespace, tab, control, non-ASCII) all surface the `ValueError` at the appropriate layer (CLI exit 1, wizard inline re-prompt, HTTP 422 from `/api/config/values`, MCP error) and DO NOT persist; with the accepted password installed, `redis-cli -a "<pw>" PING` returns `PONG` confirming the quoted-healthcheck change in § 3.2. behavior: "matrix row 23 passes against live FalkorDB" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`.

## P9 Phase 9: Documentation

`kind: framing`

**Goal**: Update all user-facing and developer-facing documentation in both repos.

### 9.1 Update Python repo documentation [category: docs] (depends: Phase 1, Phase 2, Phase 3, Phase 4, Phase 5, Phase 6, Phase 8)

`kind: deliverable`

Targets: `README.md`, `CLAUDE.md`, `CHANGELOG.md`, any `docs/**/*.md` mentioning Neo4j, `docker-compose.yml` (bare path cited in body sweep), `.gobby/services/docker-compose.yml`, `~/.gobby/services/docker-compose.yml` (operator-installed compose paths referenced by the docs sweep)

Sweep with `rg -l Neo4j .` (excluding source code already covered in earlier phases) and update each doc file:

- `README.md` — replace Neo4j references in the architecture/installation sections
- `CLAUDE.md` — update the development guidance section if it mentions Neo4j
- `CHANGELOG.md` — add a 0.4.0 entry under **"Breaking changes"** with the following content:
  - Lead: "Replaced Neo4j with FalkorDB as the knowledge graph backend. FalkorDB is Docker-only in 0.4.0 (a native local-install path is planned for a follow-up release). The `--neo4j-password` install option and the `--neo4j` uninstall flag have been **removed** (not aliased) and will hard-fail with a migration error pointing to `--falkordb`."
  - Upgrade steps:
    1. `gobby uninstall --neo4j` is no longer available. Manually stop and remove the Neo4j Docker service: `docker compose -f ~/.gobby/services/docker-compose.yml --profile neo4j down -v` if previously installed (the gobby installer keeps the unified compose file at `~/.gobby/services/docker-compose.yml` — the `-f` flag is required because there is no `docker-compose.yml` in the working directory).
    2. `gobby install` (auto-installs FalkorDB alongside Qdrant) or `gobby install --falkordb` (service-only); pass `--falkordb-password <value>` for a custom password.
    3. Knowledge graph and code graph data are not migrated; re-run `rebuild_knowledge_graph` (MCP tool) for memory and `gcode index <project>` for the code graph.
    4. The `gobby-cli` Rust crate must be upgraded to a matching FalkorDB-era version (`gcode 0.7.0+`) — see the cross-repo validation matrix in Phase 8.
  - Embed the validation matrix from 8.3 as a "Verification" sub-section so operators can self-check after upgrading.
- `docs/guides/*.md` — sweep for Neo4j references; update install/setup sections to use the `--falkordb` flag and document the Docker-only constraint.

**Follow-up note:** native local-install support (without Docker) is deferred to 0.4.1 or later. Filing a follow-up task is part of the 0.4.0 release punch list — see the migration plan task tree under #12746 for the deferred-work item.

**Acceptance:**

- 9.1.1 — Python repo documentation updated for the FalkorDB swap (README, install docs, MEMORY.md references). file: `README.md`.

### 9.2 Update Rust repo documentation [category: docs] (depends: Phase 7, Phase 8)

`kind: deliverable`

Target: `../gobby-cli/README.md`, `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`, `crates/gcode/README.md`

Sweep with `rg -l Neo4j ../gobby-cli` and update:

- Top-level `README.md` — architecture section
- `CLAUDE.md`, `AGENTS.md` — replace developer-facing Neo4j references
- `crates/gcode/README.md` — command examples (`gcode callers`, `gcode usages`, etc. don't change syntactically; only the underlying graph backend description)
- `CHANGELOG.md` — add an entry naming the FalkorDB transition. Include a hard compatibility note: `gcode 0.7.0+` requires `gobby 0.4.0+`. Mismatched versions (gcode reading the new config_store keys against an old daemon that still writes the Neo4j keys) silently degrade to "graph unavailable" — surface this in the changelog entry so operators know to upgrade both at once. Reference Phase 8.3's validation matrix.

**Acceptance:**

- 9.2.1 — Rust repo documentation updated for the FalkorDB swap. file: `crates/gcode/README.md`.

## Task Mapping

`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## M1 Task Manifest

`kind: manifest`

```yaml
- title: "Replace Neo4jConfig with FalkorConfig and add falkordb dep"
  category: "config"
  task_type: "feature"
  depends_on: []
  validation_criteria: "gobby.config.persistence.FalkorConfig"
  labels:
    - "covers:12746:1.1:1.1.1"
    - "covers:12746:1.1:1.1.2"
    - "covers:12746:1.1:1.1.3"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "1.1"
- title: "Implement FalkorClient mirroring Neo4jClient surface"
  category: "code"
  task_type: "feature"
  depends_on:
    - "1.1"
  validation_criteria: "gobby.memory.falkor_client.FalkorClient"
  labels:
    - "covers:12746:1.2:1.2.1"
    - "covers:12746:1.2:1.2.2"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "1.2"
- title: "Delete neo4j_client.py and rewrite remaining Neo4j-named imports"
  category: "refactor"
  task_type: "feature"
  depends_on:
    - "1.2"
    - "2.1"
    - "2.2"
  validation_criteria: "src/gobby/memory/neo4j_client.py"
  labels:
    - "covers:12746:1.3:1.3.1"
    - "covers:12746:1.3:1.3.2"
    - "covers:12746:1.3:1.3.3"
  assigned_agent: "backend-developer"
  tdd: false
  source_section: "1.3"
- title: "Translate KnowledgeGraphService Cypher and wire MemoryManager"
  category: "code"
  task_type: "feature"
  depends_on:
    - "1.2"
  validation_criteria: "gobby.memory.services.knowledge_graph.KnowledgeGraphService"
  labels:
    - "covers:12746:2.1:2.1.1"
    - "covers:12746:2.1:2.1.2"
    - "covers:12746:2.1:2.1.3"
    - "covers:12746:2.1:2.1.4"
    - "covers:12746:2.1:2.1.5"
    - "covers:12746:2.1:2.1.6"
    - "covers:12746:2.1:2.1.7"
    - "covers:12746:2.1:2.1.8"
    - "covers:12746:2.1:2.1.9"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "2.1"
- title: "Translate CodeGraph Cypher and wire CodeGraph construction at runner_init"
  category: "code"
  task_type: "feature"
  depends_on:
    - "1.2"
    - "2.1"
  validation_criteria: "gobby.code_index.graph.CodeGraph"
  labels:
    - "covers:12746:2.2:2.2.1"
    - "covers:12746:2.2:2.2.2"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "2.2"
- title: "Implement FalkorDB installer (Docker-only)"
  category: "code"
  task_type: "feature"
  depends_on:
    - "1.1"
  validation_criteria: "src/gobby/cli/installers/falkor.py"
  labels:
    - "covers:12746:3.1:3.1.1"
    - "covers:12746:3.1:3.1.2"
    - "covers:12746:3.1:3.1.3"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "3.1"
- title: "Replace neo4j service block in docker-compose.services.yml"
  category: "config"
  task_type: "feature"
  depends_on:
    - "1.1"
  validation_criteria: "src/gobby/data/docker-compose.services.yml"
  labels:
    - "covers:12746:3.2:3.2.1"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "3.2"
- title: "Replace services.py status helpers with FalkorDB equivalents"
  category: "code"
  task_type: "feature"
  depends_on:
    - "1.1"
  validation_criteria: "call `is_falkordb_installed(gobby_home=<tmp_home_with_no_bootstrap_yaml>)` against a tmp DB seeded with `databases.falkordb.host`+`port`; assert it returns True AND that the resolved DB path was `<tmp_home>/gobby-hub.db`, not the operator's production `~/.gobby/gobby-hub.db`"
  labels:
    - "covers:12746:3.3:3.3.1"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "3.3"
- title: "Rename Neo4j CLI flags to FalkorDB and add service-targeting flag"
  category: "code"
  task_type: "feature"
  depends_on:
    - "3.1"
    - "3.3"
  validation_criteria: "src/gobby/cli/install.py"
  labels:
    - "covers:12746:3.4:3.4.1"
    - "covers:12746:3.4:3.4.2"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "3.4"
- title: "Rename bootstrap neo4j_password to falkordb_password end-to-end"
  category: "code"
  task_type: "feature"
  depends_on:
    - "1.1"
    - "3.1"
  validation_criteria: "gobby.config.bootstrap.BootstrapConfig.falkordb_password"
  labels:
    - "covers:12746:3.5:3.5.1"
    - "covers:12746:3.5:3.5.2"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "3.5"
- title: "Migrate config_store keys databases.neo4j.* \u2192 databases.falkordb.*"
  category: "code"
  task_type: "feature"
  depends_on:
    - "1.1"
  validation_criteria: "src/gobby/storage/migrations.py"
  labels:
    - "covers:12746:3.6:3.6.1"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "3.6"
- title: "Update admin _health.py to emit memory.falkordb status payload"
  category: "code"
  task_type: "feature"
  depends_on:
    - "2.1"
    - "2.2"
    - "3.3"
  validation_criteria: "src/gobby/servers/routes/admin/_health.py"
  labels:
    - "covers:12746:4.1:4.1.1"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "4.1"
- title: "Rename _neo4j_client references in memory routes"
  category: "refactor"
  task_type: "feature"
  depends_on:
    - "2.1"
    - "2.2"
  validation_criteria: "src/gobby/servers/routes/memory.py"
  labels:
    - "covers:12746:4.2:4.2.1"
  assigned_agent: "backend-developer"
  tdd: false
  source_section: "4.2"
- title: "Sweep daemon-wide for residual Neo4j references"
  category: "refactor"
  task_type: "feature"
  depends_on:
    - "1.3"
    - "3.4"
    - "3.5"
    - "4.1"
    - "4.2"
    - "2.1"
    - "2.2"
    - "3.3"
    - "3.6"
  validation_criteria: "\"ripgrep `Neo4j|neo4j` over `src/gobby/` returns only the \u00a7 8.1 deprecation-handler block and \u00a7 3.5 / \u00a7 3.6 / \u00a7 8.2 migration helpers\" in `src/gobby/`"
  labels:
    - "covers:12746:4.3:4.3.1"
  assigned_agent: "backend-developer"
  tdd: false
  source_section: "4.3"
- title: "Teach config secret-detection that requirepass is a secret"
  category: "code"
  task_type: "feature"
  depends_on:
    - "1.1"
  validation_criteria: "src/gobby/storage/config_store.py"
  labels:
    - "covers:12746:4.4:4.4.1"
    - "covers:12746:4.4:4.4.2"
    - "covers:12746:4.4:4.4.3"
    - "covers:12746:4.4:4.4.4"
    - "covers:12746:4.4:4.4.5"
    - "covers:12746:4.4:4.4.6"
    - "covers:12746:4.4:4.4.7"
    - "covers:12746:4.4:4.4.8"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "4.4"
- title: "Rename Neo4jStatus to FalkorStatus and update useMemory hook + tests"
  category: "code"
  task_type: "feature"
  depends_on:
    - "4.1"
  validation_criteria: "web/src/hooks/useMemory.ts"
  labels:
    - "covers:12746:5.1:5.1.1"
    - "covers:12746:5.1:5.1.2"
  assigned_agent: "frontend-developer"
  tdd: true
  source_section: "5.1"
- title: "Update useDashboard type and SystemHealthCard pill"
  category: "code"
  task_type: "feature"
  depends_on:
    - "4.1"
  validation_criteria: "web/src/hooks/useDashboard.ts"
  labels:
    - "covers:12746:5.2:5.2.1"
  assigned_agent: "frontend-developer"
  tdd: true
  source_section: "5.2"
- title: "Update MemoryPage hook ref and KnowledgeGraph empty-state copy"
  category: "code"
  task_type: "feature"
  depends_on:
    - "5.1"
  validation_criteria: "web/src/components/memory/MemoryPage.tsx"
  labels:
    - "covers:12746:5.3:5.3.1"
  assigned_agent: "frontend-developer"
  tdd: true
  source_section: "5.3"
- title: "Update setup state.ts schema fields with one-shot migration"
  category: "code"
  task_type: "feature"
  depends_on:
    - "3.4"
  validation_criteria: "web/src/setup/utils/state.ts"
  labels:
    - "covers:12746:6.1:6.1.1"
  assigned_agent: "frontend-developer"
  tdd: true
  source_section: "6.1"
- title: "Update Services.tsx CLI flags (Docker-only)"
  category: "code"
  task_type: "feature"
  depends_on:
    - "6.1"
  validation_criteria: "web/src/setup/steps/Services.tsx"
  labels:
    - "covers:12746:6.2:6.2.1"
    - "covers:12746:6.2:6.2.2"
    - "covers:12746:6.2:6.2.3"
  assigned_agent: "frontend-developer"
  tdd: true
  source_section: "6.2"
- title: "Regenerate the bundled setup.mjs artifact"
  category: "code"
  task_type: "feature"
  depends_on:
    - "6.2"
  validation_criteria: "src/gobby/install/shared/setup/setup.mjs"
  labels:
    - "covers:12746:6.3:6.3.1"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "6.3"
- title: "Verify wizard end-to-end"
  category: "test"
  task_type: "feature"
  depends_on:
    - "6.3"
  validation_criteria: "\"Ink setup wizard completes a fresh install end-to-end against FalkorDB\" in `web/src/setup/`"
  labels:
    - "covers:12746:6.4:6.4.1"
  assigned_agent: "frontend-developer"
  tdd: false
  source_section: "6.4"
- title: "Replace Neo4jConfig with FalkorConfig in gobby-cli config.rs"
  category: "code"
  task_type: "feature"
  depends_on:
    - "3.6"
  validation_criteria: "`crates/gcode/src/config.rs`. (The crate is bin-only \u2014 `crates/gcode/Cargo.toml` declares `name = \"gobby-code\"` with a single `[[bin]] name = \"gcode\"` and `path = \"src/main.rs\"`; no `lib.rs` exists, so there is no `gobby_cli::*` library symbol path to bind acceptance to. The full Neo4j-side removal acceptance belongs in \u00a7 7.4, not here.)"
  labels:
    - "covers:12746:7.1:7.1.1"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "7.1"
- title: "Pin FalkorClient API shape and result-conversion contract"
  category: "code"
  task_type: "feature"
  depends_on:
    - "7.1"
  validation_criteria: "`crates/gcode/src/falkor.rs`. (Same bin-only crate caveat as 7.1.1 \u2014 `crates/gcode/` has no `lib.rs`; acceptance evidence is the file diff, not a library symbol path.)"
  labels:
    - "covers:12746:7.2:7.2.1"
    - "covers:12746:7.2:7.2.2"
    - "covers:12746:7.2:7.2.3"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "7.2"
- title: "Port 8 read queries to FalkorClient"
  category: "code"
  task_type: "feature"
  depends_on:
    - "7.2"
  validation_criteria: "crates/gcode/src/falkor.rs"
  labels:
    - "covers:12746:7.3:7.3.1"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "7.3"
- title: "Update gobby-cli callsites + full Rust source sweep"
  category: "refactor"
  task_type: "feature"
  depends_on:
    - "7.3"
  validation_criteria: "\"ripgrep `Neo4j|neo4j` over Rust source returns zero hits\" in `crates/`"
  labels:
    - "covers:12746:7.4:7.4.1"
  assigned_agent: "backend-developer"
  tdd: false
  source_section: "7.4"
- title: "Document CLI flag deprecation policy"
  category: "docs"
  task_type: "feature"
  depends_on:
    - "3.4"
    - "4.3"
  validation_criteria: "`CHANGELOG.md`. The Click handler implementation itself lives in `src/gobby/cli/install.py` and is owned by \u00a7 3.4 (R39-F1 \u2014 moved out of \u00a7 8.1 so \u00a7 3.4's test cases can reference handlers that exist in the same commit)"
  labels:
    - "covers:12746:8.1:8.1.1"
  assigned_agent: "tech-writer"
  tdd: false
  source_section: "8.1"
- title: "Add startup-time stale-config warning"
  category: "code"
  task_type: "feature"
  depends_on:
    - "3.6"
    - "4.3"
  validation_criteria: "src/gobby/runner.py"
  labels:
    - "covers:12746:8.2:8.2.1"
  assigned_agent: "backend-developer"
  tdd: true
  source_section: "8.2"
- title: "Define cross-repo validation matrix and merge ordering"
  category: "test"
  task_type: "feature"
  depends_on:
    - "8.1"
    - "8.2"
    - "5.1"
    - "5.2"
    - "5.3"
    - "6.4"
    - "7.1"
    - "7.2"
    - "7.3"
    - "7.4"
  validation_criteria: "\"validation matrix and merge ordering documented\" in `.gobby/plans/task-12746-neo4j-falkordb-swap.md`"
  labels:
    - "covers:12746:8.3:8.3.1"
    - "covers:12746:8.3:8.3.2"
    - "covers:12746:8.3:8.3.3"
    - "covers:12746:8.3:8.3.4"
    - "covers:12746:8.3:8.3.5"
    - "covers:12746:8.3:8.3.6"
    - "covers:12746:8.3:8.3.7"
    - "covers:12746:8.3:8.3.8"
    - "covers:12746:8.3:8.3.9"
    - "covers:12746:8.3:8.3.10"
    - "covers:12746:8.3:8.3.11"
    - "covers:12746:8.3:8.3.12"
    - "covers:12746:8.3:8.3.13"
    - "covers:12746:8.3:8.3.14"
    - "covers:12746:8.3:8.3.15"
    - "covers:12746:8.3:8.3.16"
    - "covers:12746:8.3:8.3.17"
    - "covers:12746:8.3:8.3.18"
    - "covers:12746:8.3:8.3.19"
    - "covers:12746:8.3:8.3.20"
    - "covers:12746:8.3:8.3.21"
    - "covers:12746:8.3:8.3.22"
    - "covers:12746:8.3:8.3.23"
    - "covers:12746:8.3:8.3.24"
  assigned_agent: "backend-developer"
  tdd: false
  source_section: "8.3"
- title: "Update Python repo documentation"
  category: "docs"
  task_type: "feature"
  depends_on:
    - "1.1"
    - "1.2"
    - "1.3"
    - "2.1"
    - "2.2"
    - "3.1"
    - "3.2"
    - "3.3"
    - "3.4"
    - "3.5"
    - "3.6"
    - "4.1"
    - "4.2"
    - "4.3"
    - "4.4"
    - "5.1"
    - "5.2"
    - "5.3"
    - "6.1"
    - "6.2"
    - "6.3"
    - "6.4"
    - "8.1"
    - "8.2"
    - "8.3"
  validation_criteria: "README.md"
  labels:
    - "covers:12746:9.1:9.1.1"
  assigned_agent: "tech-writer"
  tdd: false
  source_section: "9.1"
- title: "Update Rust repo documentation"
  category: "docs"
  task_type: "feature"
  depends_on:
    - "7.1"
    - "7.2"
    - "7.3"
    - "7.4"
    - "8.1"
    - "8.2"
    - "8.3"
  validation_criteria: "crates/gcode/README.md"
  labels:
    - "covers:12746:9.2:9.2.1"
  assigned_agent: "tech-writer"
  tdd: false
  source_section: "9.2"
```
