# Neo4j Correctness, API, and Graph UI Cleanup Plan

## Summary

Fix Neo4j as a pair of derived projections (memory knowledge graph, code graph) so the data is correct, project-scoped where required, and rebuildable from source-of-truth stores. The plan covers backend, API contracts, and graph UIs because current frontend behavior depends on unstable name-based identity.

There is no backward-compatibility requirement for existing Neo4j data or current route quirks. After backend changes land, clear and rebuild the Neo4j-derived graphs.

## Scope and Non-Goals

In scope:

- Memory knowledge-graph identity, lifecycle, routes, deletion, and UI
- Code-graph symbol identity, sync truth, read queries, and UI
- Config cleanup of legacy `neo4j_*` attribute access
- Project-deletion cascade for both projections

Out of scope:

- Migration to a different graph backend (Neo4j is the chosen graph backend)
- Crossref-based memory graph (`/api/memories/graph`, `MemoryGraph.tsx`) — shape and behavior preserved
- Fully-qualified symbol names from the code analyzer (deferred; ambiguous same-name promotion is handled defensively)

## 1. Memory KG: canonical identity and project scope

### Identity contract

Every entity node carries one deterministic identity property `entity_key`. Memory nodes use the existing `memory_id`. Names are display labels only.

New helper module `src/gobby/memory/identity.py`:

```python
import unicodedata
import uuid

# Fixed namespace — never changes after the first deploy. Required for entity_key stability.
GOBBY_KG_NAMESPACE = uuid.UUID("8b0e6b6e-1f4f-4d2a-9d4f-7a5d3c1e8b9a")

GLOBAL_PROJECT_SENTINEL = "_global"  # reserved; no real project may use this id


def normalize_entity_name(name: str) -> str:
    """NFKC-normalize and casefold. Punctuation preserved (C++ ≠ C)."""
    return unicodedata.normalize("NFKC", name).strip().casefold()


def entity_key(project_id: str | None, entity_type: str, name: str) -> str:
    """Deterministic per-(project, type, normalized_name) UUIDv5 string."""
    if project_id == GLOBAL_PROJECT_SENTINEL:
        raise ValueError(f"project_id may not equal reserved sentinel {GLOBAL_PROJECT_SENTINEL!r}")
    scope = project_id or GLOBAL_PROJECT_SENTINEL
    et = entity_type.strip().casefold()
    nn = normalize_entity_name(name)
    return str(uuid.uuid5(GOBBY_KG_NAMESPACE, f"{scope}|{et}|{nn}"))
```

Why UUIDv5 over a composite MERGE key:

- Single string id usable as React key, URL segment, and Cypher MERGE target
- Re-extracting the same content yields the same key — rebuilds are idempotent
- One indexed property instead of three — simpler index, cheaper writes

### Entity node properties

| property      | type     | notes                                       |
| ------------- | -------- | ------------------------------------------- |
| `entity_key`  | string   | unique constraint, indexed; identity        |
| `name`        | string   | display only; never used in MERGE/MATCH     |
| `entity_type` | string   |                                             |
| `project_id`  | string   | nullable; null = truly global               |
| `created_at`  | datetime |                                             |
| `updated_at`  | datetime |                                             |
| `embedding`   | vector   | unchanged; vector index stays on `_Entity`  |

Constraints (one-time migration in Neo4j):

```cypher
CREATE CONSTRAINT entity_key_unique IF NOT EXISTS FOR (n:_Entity) REQUIRE n.entity_key IS UNIQUE;
CREATE INDEX entity_project_type IF NOT EXISTS FOR (n:_Entity) ON (n.project_id, n.entity_type);
CREATE CONSTRAINT memory_id_unique IF NOT EXISTS FOR (m:Memory) REQUIRE m.memory_id IS UNIQUE;
```

### Neo4jClient write/read changes

`src/gobby/memory/neo4j_client.py`:

- `upsert_entity()` (currently around line 334): take `entity_key`, `name`, `entity_type`, `project_id`. Use `MERGE (n:_Entity {entity_key: $entity_key}) ON CREATE SET ... ON MATCH SET name = $name, updated_at = $now`. Never MERGE on `{name: $name}`.
- `add_relationship()` (line 377): take `(source_key, target_key, rel_type)`. Replace `MATCH (a {name: $source_name}), (b {name: $target_name})` with `MATCH (a:_Entity {entity_key: $source_key}), (b:_Entity {entity_key: $target_key})`.
- `get_entity_graph()` (line 166): include `entity_key` in the projection.
- `get_entity_neighbors(name)` → rename to `get_entity_neighbors(entity_key)` (line 238). Match by `{entity_key: $entity_key}`. Return both ids and names.
- `find_entity_by_name(name, project_id)` — new helper for the search path; returns `entity_key`.
- `delete_orphan_entities(project_id)` — new helper; see Section 5.

`_Entity` label remains as the vector-index target and an "is this an entity vs a Memory?" disambiguator. It is never an identity surface.

### KnowledgeGraphService changes

`src/gobby/memory/services/knowledge_graph.py`:

- LLM extraction output passes through `entity_key()` for every entity before any Cypher write.
- Relationship writes carry `(source_key, target_key)` rather than names.
- `add_to_graph()` signature change is covered in Section 2.

## 2. Memory KG lifecycle: truthful success/failure

Replace silent-failure with a structured result.

```python
# src/gobby/memory/services/knowledge_graph.py
from dataclasses import dataclass
from typing import Literal

KnowledgeGraphStatus = Literal[
    "success",            # entities + relationships + memory link all wrote
    "extraction_failed",  # LLM extraction errored or returned nothing usable
    "write_failed",       # Cypher writes errored before any node landed
    "partial",            # some entities/relationships wrote, others did not
]


@dataclass(frozen=True)
class KnowledgeGraphResult:
    status: KnowledgeGraphStatus
    entities_written: int
    entities_failed: int
    relationships_written: int
    relationships_failed: int
    memory_linked: bool
    error: str | None = None

    @property
    def is_fully_processed(self) -> bool:
        """Caller may mark the source memory graph_processed iff this is True."""
        return self.status == "success"
```

`add_to_graph()` returns `KnowledgeGraphResult`. Transient failures (Neo4j unavailable, timeout) raise so the existing retry/backoff in the lifecycle path keeps working — `KnowledgeGraphResult` is for *deterministic* outcomes.

### Lifecycle gating

`src/gobby/sessions/lifecycle.py:193-198` currently calls `add_to_graph(...)` and then `mark_graph_processed(...)` unconditionally. New behavior:

```python
result = await kg_service.add_to_graph(memory.content, memory_id=memory.id, project_id=memory.project_id)
if result.is_fully_processed:
    await asyncio.to_thread(self.memory_manager.mark_graph_processed, memory.id)
else:
    logger.warning(
        "kg.partial_or_failed memory_id=%s status=%s wrote=%d failed=%d error=%s",
        memory.id, result.status, result.entities_written, result.entities_failed, result.error,
    )
    # No mark_graph_processed; the periodic sweep will retry.
```

### Reporting

`reconcile_stores()` and rebuild paths report counts using `KnowledgeGraphResult` aggregates rather than "did not throw".

## 3. Memory KG routes: tighten contracts, fix rebuild

`src/gobby/servers/routes/memory.py`:

- `rebuild_knowledge_graph` (line 243): change `await kg.add_to_graph(memory.content)` → `await kg.add_to_graph(memory.content, memory_id=memory.id, project_id=memory.project_id)`. Aggregate `KnowledgeGraphResult`s into the response payload (totals + per-status counts).
- `GET /api/memories/graph/entities` (line 153): response items include `entity_key` (string), `name` (string), `entity_type` (string), `project_id` (string | null). Edges reference source/target by `entity_key`.
- `GET /api/memories/graph/entities/{entity_name}/neighbors` → replace with `GET /api/memories/graph/entities/{entity_key}/neighbors`. Path param is now the stable id. The route returns the same shape as `entities` for the neighbor set.
- All knowledge-graph routes accept and honor a `project_id` query param. When omitted, behavior is documented as "all projects + globals". Cross-project reads stay possible but explicit.
- Remove the current mixed entity/Memory visualization that returns Memory nodes from the entities route; that path queried Memory nodes but discarded them. The entities route returns `_Entity` nodes only. A separate route (already exists) returns Memory-centric graphs.

### Response schema

```json
{
  "entities": [
    {"entity_key": "uuid", "name": "Alice", "entity_type": "person", "project_id": "p_123"}
  ],
  "relationships": [
    {"source_key": "uuid", "target_key": "uuid", "type": "KNOWS"}
  ]
}
```

## 4. Memory graph UI: stable entity identity

`web/src/hooks/useMemory.ts`:

- Entity type gains `entity_key: string`. `name` becomes display-only.
- Relationship type uses `source_key` / `target_key`.
- Neighbor-fetch hook signature: `useEntityNeighbors(entityKey: string)`.

`web/src/components/memory/KnowledgeGraph.tsx`:

- Node `id` field is `entity_key`; node `label` is `name`.
- Merge logic for incremental loads dedupes on `entity_key`.
- Click-to-expand passes `entity_key` to the neighbor fetch.
- Visual presentation otherwise unchanged.

`web/src/components/memory/MemoryGraph.tsx` and `/api/memories/graph` are crossref-based — not touched by this plan.

## 5. Memory deletion and project deletion

### Single-memory deletion

After deleting a `Memory` node, sweep orphaned entities scoped to the deleted memory's project:

```cypher
MATCH (e:_Entity {project_id: $project_id})
WHERE NOT (e)<-[:MENTIONS]-(:Memory)
DETACH DELETE e
```

For globals (`project_id = null`), the same sweep runs with a null match. New method `Neo4jClient.delete_orphan_entities(project_id: str | None) -> int` invoked from `MemoryManager.delete_memory()` after the `Memory` node delete.

### Project deletion

New method `KnowledgeGraphService.delete_project_subgraph(project_id: str)`:

```cypher
MATCH (n) WHERE n.project_id = $project_id DETACH DELETE n;
```

Wire into the project-deletion API path (find via `git grep delete_project src/gobby/servers/routes/`). After the SQLite project delete commits, fire-and-await this method. Failure to delete the subgraph logs an error but does not roll back the SQLite delete — the periodic reconcile sweep is the safety net.

Globals are never project-scoped and survive project deletion.

### Reconcile vs clear+rebuild — operator runbook

| Tool                 | When to use                                                            |
| -------------------- | ---------------------------------------------------------------------- |
| `reconcile_stores()` | Suspected drift; targeted repair; safe to run anytime                  |
| `clear + rebuild`    | After identity/schema changes (this plan), or when reconcile loops     |

Both remain available post-refactor. `reconcile_stores()` is *not* the primary correctness path — it is a safety net for transient failures.

## 6. Code graph: unify symbol identity and sync truth

### The bug

`src/gobby/code_index/graph.py:59-60`:

```cypher
MERGE (caller:CodeSymbol {id: $caller_id, project: $project})
MERGE (callee:CodeSymbol {name: $callee_name, project: $project})
```

Caller is keyed by id, callee by name. When the callee's defining file is later indexed, DEFINES creates a *second* `CodeSymbol` with the same name but with `id` set. Two nodes per symbol; `affected_by` and `blast_radius` walks miss half the call graph.

### Three node kinds, one CALLS edge

| Label              | MERGE key             | Created by                                               | Project-scoped |
| ------------------ | --------------------- | -------------------------------------------------------- | -------------- |
| `CodeSymbol`       | `{id, project}`       | DEFINES from `_sync_file()`                              | yes            |
| `UnresolvedCallee` | `{name, project}`     | CALLS write when callee not yet indexed                  | yes            |
| `ExternalSymbol`   | `{module, name}`      | CALLS write when callee resolves to non-project import   | no             |

Resolution rule at CALLS write time (in `graph.py`):

1. Analyzer resolved callee to a project symbol id → `MERGE (callee:CodeSymbol {id: $callee_id, project: $project})`
2. Else import target is outside the project → `MERGE (callee:ExternalSymbol {module: $module, name: $callee_name})`
3. Else (same project, callee not yet indexed) → `MERGE (callee:UnresolvedCallee {name: $callee_name, project: $project})`

Constraints:

```cypher
CREATE CONSTRAINT codesymbol_id_project IF NOT EXISTS FOR (s:CodeSymbol) REQUIRE (s.id, s.project) IS UNIQUE;
CREATE CONSTRAINT unresolved_name_project IF NOT EXISTS FOR (u:UnresolvedCallee) REQUIRE (u.name, u.project) IS UNIQUE;
CREATE CONSTRAINT external_module_name IF NOT EXISTS FOR (e:ExternalSymbol) REQUIRE (e.module, e.name) IS UNIQUE;
```

### Promotion sweep

After `_sync_file()` writes all DEFINES for file F, run one Cypher pass to promote matching `UnresolvedCallee` nodes to the canonical `CodeSymbol`:

```cypher
MATCH (s:CodeSymbol {project: $project})
WHERE (:CodeFile {path: $file_path, project: $project})-[:DEFINES]->(s)
WITH collect(s) AS defined_syms, $project AS project
UNWIND defined_syms AS s
MATCH (u:UnresolvedCallee {name: s.name, project: project})
// Ambiguity guard: only promote when exactly one CodeSymbol in the project owns this name
WITH u, s, [(s2:CodeSymbol {name: s.name, project: project}) | s2] AS candidates
WHERE size(candidates) = 1
MATCH (caller)-[old:CALLS]->(u)
CREATE (caller)-[new:CALLS]->(s)
SET new = old
DELETE old
WITH u
DETACH DELETE u
```

Same-name collisions: when two symbols in the same project share a name, the guard `size(candidates) = 1` skips promotion. The unresolved node persists; a structured warning logs `project`, `name`, and the candidate symbol ids. Resolving these correctly requires fully-qualified names from the analyzer (out of scope).

### Sync truth

`src/gobby/code_index/sync_worker.py::_sync_file()`:

- All graph write methods in `graph.py` raise on Cypher failure (today they swallow and log).
- `_sync_file()` wraps DEFINES + CALLS + IMPORTS + promotion-sweep in one try/except.
- `graph_synced=1` is set only after every step in that block succeeds.
- On failure: structured error logged with file path; `graph_synced` stays 0; retry on next sync cycle.

### Read query updates

All read queries in `graph.py` that match by callee name must union over labels:

```cypher
MATCH (caller:CodeSymbol)-[:CALLS]->(target)
WHERE (target:CodeSymbol AND target.id = $id AND target.project = $project)
   OR (target:UnresolvedCallee AND target.name = $name AND target.project = $project)
```

Variable-length walks (`[:CALLS*1..n]`) follow edges regardless of target label — only the leaf match needs the union.

Affected query methods (cite lines in `graph.py`):

- `get_callers` (line 104)
- `get_usages` (line 124)
- `get_blast_radius` (line 196, 219)
- `get_file_graph` (line 281, 297, 329, 366)
- `expand_symbol` (line 459)

## 7. Code graph UI: additive node-kind field

Backend response gains `kind: "symbol" | "unresolved" | "external"` per node. Existing `nodes` / `links` shape preserved.

`web/src/hooks/useCodeGraph.ts`: node type gains `kind`.

`web/src/components/code-graph/CodeGraphExplorer.tsx`:

- `kind === "symbol"` — current rendering
- `kind === "unresolved"` — dashed grey outline
- `kind === "external"` — solid grey, label prefixed with module (e.g., `requests.get`)

No redesign of the explorer. Goal is trustworthy data, not new UI.

## 8. Config cleanup

Three legacy `getattr` accesses to remove from `src/gobby/memory/manager.py`:

- Line 656: `getattr(self.config, "neo4j_graph_search", True)`
- Line 660: `getattr(self.config, "neo4j_graph_min_score", 0.5)`
- Lines 661, 739: `getattr(self.config, "neo4j_rrf_k", 60)`

Replace with reads from the canonical `databases.neo4j` config model. Confirm the canonical model exposes `graph_search`, `graph_min_score`, and `rrf_k` (or rename them — settle on the canonical names in the same change).

Test fixtures under `tests/memory/` that patch `config.neo4j_*` attributes are removed or rewritten to populate `config.databases.neo4j`.

## 9. Deploy ordering

This is internal — no external API consumers. Backend and frontend ship in the same release:

- Add a single feature flag `kg_stable_id_routes` in `bootstrap.yaml`. Default `false` during development of the backend.
- When set, routes return `entity_key` and accept `entity_key` in the path; when unset, routes preserve current name-based behavior.
- Frontend reads the flag from `/api/health` (already exposes feature flags) and switches both contracts together.
- On release, set the flag to `true` by default and remove the legacy branches in the next minor version.

The flag exists *only* to keep the development branch deployable in CI; it is not a long-lived rollout mechanism.

## 10. Implementation order

Discrete units, top-down. Each is one task and one commit.

1. `src/gobby/memory/identity.py` + unit tests for `entity_key` / `normalize_entity_name`
2. `KnowledgeGraphResult` dataclass in `knowledge_graph.py`
3. Neo4j constraints/indexes migration (Cypher `CREATE CONSTRAINT ...`)
4. `Neo4jClient` writes (`upsert_entity`, `add_relationship`) + reads (`get_entity_graph`, `get_entity_neighbors`, `find_entity_by_name`)
5. `KnowledgeGraphService.add_to_graph` returns `KnowledgeGraphResult`, computes `entity_key` for all entities
6. `lifecycle.py` gates `mark_graph_processed` on `result.is_fully_processed`
7. `Neo4jClient.delete_orphan_entities` + wire into `MemoryManager.delete_memory`
8. `KnowledgeGraphService.delete_project_subgraph` + wire into project-delete API
9. Routes (`/graph/entities`, `/graph/entities/{entity_key}/neighbors`, `rebuild_knowledge_graph`) + feature flag
10. Frontend `useMemory.ts` types + `KnowledgeGraph.tsx` rewrite
11. Code graph: introduce `UnresolvedCallee` and `ExternalSymbol` labels + constraints
12. Code graph: CALLS write resolution rule + promotion sweep
13. Code graph: read query unions across labels
14. `_sync_file()` fail-closed wrapping
15. Frontend `useCodeGraph.ts` + `CodeGraphExplorer.tsx` for `kind` field
16. Config cleanup: replace `neo4j_*` `getattr`s, update fixtures
17. Operator runbook entries: `gobby memory neo4j clear && gobby memory neo4j rebuild`
18. Flip `kg_stable_id_routes` default to `true`; remove legacy branches in the same change

## 11. Test plan

### Memory KG identity

- `entity_key("p1", "person", "Alice") == entity_key("p1", "person", "alice")` (casefold)
- `entity_key("p1", "person", "Café") == entity_key("p1", "person", "Cafe\u0301")` (NFKC)
- `entity_key("p1", "person", "Alice") != entity_key("p2", "person", "Alice")` (project scope)
- `entity_key(None, "person", "Alice") != entity_key("p1", "person", "Alice")` (global vs project)
- `entity_key(GLOBAL_PROJECT_SENTINEL, ...)` raises `ValueError`
- Re-running ingestion on identical content twice produces the same `entity_key`s and zero duplicate nodes

### Memory KG lifecycle

- `add_to_graph` raises on Neo4j unavailable (transient → retry path triggers)
- `add_to_graph` returns `status="extraction_failed"` when LLM returns no entities; memory not marked processed
- `add_to_graph` returns `status="partial"` when one of N entity writes fails; memory not marked processed
- `add_to_graph` returns `status="success"` on full write; memory marked processed
- Periodic sweep retries memories left unprocessed by `partial`/`extraction_failed`

### Memory KG routes/UI

- `/graph/entities` returns `entity_key` and `name` per entity; relationships reference keys
- `/graph/entities/{entity_key}/neighbors` accepts an id, returns id-keyed neighbors
- Same display name in two projects → distinct entity_keys, both visible in UI without merging
- `KnowledgeGraph.tsx` dedupes by `entity_key`; clicking an entity expands the correct node when a same-name entity exists
- Crossref `/api/memories/graph` and `MemoryGraph.tsx` shape unchanged (regression test on a snapshot)
- `rebuild_knowledge_graph` produces the same node/edge counts as a full re-ingestion of the same memories (idempotency)

### Memory KG deletion

- Deleting a memory removes orphan entities scoped to its project; entities still mentioned by other memories survive
- Deleting a global memory removes orphan globals only
- Deleting a project removes all `_Entity` and `Memory` nodes with that `project_id`; globals untouched
- Project-delete subgraph failure logs an error and is recovered by `reconcile_stores()`

### Code graph identity

- DEFINES + CALLS to a same-project, indexed callee resolve to one `CodeSymbol` node (no duplicate)
- CALLS to an unindexed callee creates an `UnresolvedCallee`; later DEFINES of that name promotes and rewrites the edge; unresolved node detached and deleted
- Two project files defining symbols with the same name → promotion sweep skips, `UnresolvedCallee` persists, structured warning logged
- CALLS to a non-project import creates `ExternalSymbol`; never confused with project symbols
- Renaming a symbol produces a new `CodeSymbol` node; old node orphans (acceptable; rebuild recovers)

### Code graph sync truth

- Cypher write failure inside `_sync_file()` leaves `graph_synced=0`
- Successful write sets `graph_synced=1` exactly once
- `graph.py` write methods raise on driver error (no swallow)

### Code graph UI

- Explorer renders `kind="symbol"` as today
- Explorer renders `kind="unresolved"` with dashed outline
- Explorer renders `kind="external"` with module-prefixed label
- `expand-file`, `expand-symbol`, `blast-radius` views merge correctly across the three kinds

### Config

- `MemoryManager` reads `graph_search`, `graph_min_score`, `rrf_k` from `config.databases.neo4j`
- No remaining `getattr(self.config, "neo4j_*", ...)` calls (verified via grep in CI)
- Fixtures populating legacy attrs are removed; new fixtures populate `config.databases.neo4j`

## 12. Operational notes

After this lands, run once:

```
gobby memory neo4j clear
gobby memory neo4j rebuild
gobby code-index neo4j clear
gobby code-index neo4j rebuild
```

(Confirm exact CLI commands during implementation; add them if absent.)

Reconcile remains available for future drift, not for cutover.

## 13. Assumptions

- Existing Neo4j data is cleared and rebuilt — no data migration logic needed.
- The canonical `databases.neo4j` config model exists or will be added in the same change as Section 8.
- The code analyzer can distinguish "import target inside project" from "import target outside project" (required for Section 6 step 2). If not, `ExternalSymbol` is created only when the analyzer is confident; ambiguous cases fall through to `UnresolvedCallee`.
- `GOBBY_KG_NAMESPACE` is set once at first deploy and never rotated. Rotating it would orphan every entity_key in the database.
