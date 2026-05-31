# code_index: Hand code graph to gcode; keep memory and orchestration in Python

## Overview
`kind: framing`

The code graph subsystem (FalkorDB writes/reads for IMPORTS/DEFINES/CALLS, report compute) has moved to gcode under `crates/gcode/src/graph/` (see gobby-cli plan `gcode-graph-enhancements.md`). This plan rewires the daemon to call gcode via subprocess for code graph operations, deletes the Python `src/gobby/code_index/graph.py`, updates `/api/code-index/graph/*` HTTP routes to subprocess shims, adds provenance display to both the code-graph and memory knowledge-graph web UIs, adds a project graph report tab, and updates the Python `KnowledgeGraphCodeLinker` to land provenance metadata on `RELATES_TO_CODE` bridge edges (small in-place edit; bridge writes stay Python). Orchestration files (`maintenance.py`, `sync_worker.py`, `trigger.py`, `summarizer.py`, `context.py`) are untouched apart from import rewiring.

## Constraints
`kind: framing`

- Depends on gobby-cli plan `gcode-graph-enhancements.md` shipping P1-P3 first (specifically: `gcode graph report` and `gcode graph sync-file` subcommands, plus all graph read paths).
- Phase 7 contract test framework at `tests/code_index/test_gcode_phase7_contract.py` continues running as cross-repo validation harness.
- Subprocess shims for `/api/code-index/graph/*` HTTP routes must preserve response shapes for the web UI. No UI breakage during cutover.
- Per-call subprocess latency is acceptable (~10ms typical). If any route exceeds 200ms p95, file a follow-up for `gcode serve` sidecar — do not block this plan.
- Memory↔code bridge writes (`RELATES_TO_CODE`) stay in `src/gobby/memory/services/knowledge_graph/code_linker.py`. gcode owns code only. Bridge writer just adds provenance properties to its existing Cypher.
- `maintenance.py`, `sync_worker.py`, `trigger.py`, `summarizer.py`, `context.py` are untouched except where they imported the deleted `graph.py`; those imports rewire through the gateway.
- Test isolation: all tests start an isolated test daemon. Prefix pytest with `GOBBY_TEST_PROTECT=1`. Never run the full pytest suite.

## P1: Subprocess gateway
`kind: framing`

**Goal**: Single Python module owning all gcode subprocess calls.

### 1.1 Add gcode subprocess gateway module [category: code]
`kind: deliverable`

Targets:
- `src/gobby/gcode_gateway.py`

```python
"""Single entry point for all gcode subprocess calls."""

class GcodeGateway:
    def __init__(self, gcode_path: Path, project_id: str): ...

    async def graph_sync_file(self, file_path: str) -> None: ...

    async def graph_report(self) -> dict: ...
    async def graph_clear(self) -> None: ...
    async def graph_rebuild(self) -> None: ...
    async def graph_get_full(self) -> dict: ...
    async def graph_get_file(self, file_path: str) -> dict: ...
    async def graph_get_neighbors(self, symbol_id: str, depth: int = 1) -> dict: ...
    async def graph_get_blast_radius(self, symbol_id: str, depth: int = 3) -> dict: ...
    async def graph_search(self, query: str, limit: int = 50) -> dict: ...
```

Each method spawns `gcode <command> --format json` via `asyncio.create_subprocess_exec`, parses stdout, raises `GcodeSubprocessError` on non-zero exit with stderr captured. Configurable timeouts. Structured logging per call.

**Acceptance:**

- 1.1.1 - `GcodeGateway` exists with listed methods. file: `src/gobby/gcode_gateway.py`.
- 1.1.2 - Each method round-trips through a real gcode binary (uses sibling `gobby-cli` via `GOBBY_CLI_REPO`). test: `tests/test_gcode_gateway.py::test_all_methods_roundtrip`.
- 1.1.3 - Non-zero exit raises `GcodeSubprocessError` with stderr. test: `tests/test_gcode_gateway.py::test_error_propagation`.

## P2: Delete Python graph.py
`kind: framing`

**Goal**: Remove `src/gobby/code_index/graph.py`. Rewire every importer through the gateway.

### 2.1 Rewire sync_worker through gateway [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/code_index/sync_worker.py`

`sync_worker.py` currently imports `CodeGraph` from `graph.py` and calls `code_graph.sync_file(file_path)` per file. Replace those calls with `await gateway.graph_sync_file(file_path)`. The orchestration logic (debouncing, batching, Qdrant coordination, embedding generation) stays untouched.

**Acceptance:**

- 2.1.1 - `sync_worker.py` no longer imports `CodeGraph`. file: `src/gobby/code_index/sync_worker.py`.
- 2.1.2 - Sync pass invokes `gateway.graph_sync_file` for each changed file. test: `tests/code_index/test_sync_worker.py::test_sync_pass_uses_gateway`.

### 2.2 Delete graph.py [category: refactor] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/code_index/graph.py`
- `tests/code_index/test_graph.py`
- `tests/code_index/test_falkor_phase2_graph.py`

Delete these files. `git grep` for `from gobby.code_index.graph` or `from gobby.code_index import.*graph` after delete must return zero hits.

**Acceptance:**

- 2.2.1 - `src/gobby/code_index/graph.py` no longer exists. behavior: "file does not exist" in `src/gobby/code_index/`.
- 2.2.2 - No remaining imports of `gobby.code_index.graph`. behavior: "grep returns zero hits" in `src/gobby/`.

## P3: HTTP route migration
`kind: framing`

**Goal**: Rewrite `/api/code-index/graph/*` routes as subprocess shims. UI sees identical response shapes. New `/report` endpoint added.

### 3.1 Rewrite code_index routes as gateway calls [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/code_index.py`

Replace every route handler with a `GcodeGateway` call:
- `GET /api/code-index/graph` → `gateway.graph_get_full()`
- `GET /api/code-index/graph/file/{file_path}` → `gateway.graph_get_file(file_path)`
- `GET /api/code-index/graph/symbol/{symbol_id}/neighbors` → `gateway.graph_get_neighbors(symbol_id)`
- `GET /api/code-index/graph/blast-radius` → `gateway.graph_get_blast_radius(symbol_id, depth)`
- `GET /api/code-index/graph/search` → `gateway.graph_search(query, limit)`
- `POST /api/code-index/graph/clear` → `gateway.graph_clear()`
- `POST /api/code-index/graph/rebuild` → `gateway.graph_rebuild()`
- `POST /api/code-index/invalidate` → stays in Python (calls `CodeIndexTrigger.notify_file_changed`)
- `GET /api/code-index/graph/report` → `gateway.graph_report()` (NEW)

**Acceptance:**

- 3.1.1 - All graph routes call the gateway; response shapes unchanged. file: `src/gobby/servers/routes/code_index.py`.
- 3.1.2 - Route integration tests pass without modifying UI fixtures. test: `tests/servers/routes/test_code_index_routes.py`.
- 3.1.3 - New `/api/code-index/graph/report` endpoint returns ProjectGraphReport shape. test: `tests/servers/routes/test_code_index_routes.py::test_report_route`.

## P4: Memory graph provenance
`kind: framing`

**Goal**: Provenance metadata on memory KG writes — both the entity-to-entity relationships and the RELATES_TO_CODE bridge edges. Pure Python edits; no subprocess.

### 4.1 Add provenance metadata to bridge writes [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/services/knowledge_graph/code_linker.py`

Extend the existing Cypher in `link_entities_to_code` to set provenance properties on each `RELATES_TO_CODE` edge:

```python
await self._falkor.query(
    "UNWIND $links AS link "
    "MATCH (e:_Entity {entity_key: link.entity_key}) "
    "MATCH (c:CodeSymbol {id: link.symbol_id, project: $project_id}) "
    "MERGE (e)-[r:RELATES_TO_CODE]->(c) "
    "SET r.score = link.score, "
    "    r.updated_at = timestamp(), "
    "    r.provenance = 'INFERRED', "
    "    r.confidence = link.score, "
    "    r.source_system = 'gobby-memory', "
    "    r.matching_method = 'entity_embedding_to_code_symbol'",
    {"links": links, "project_id": project_id},
)
```

**Acceptance:**

- 4.1.1 - `link_entities_to_code` Cypher sets provenance, confidence, source_system, matching_method on RELATES_TO_CODE edges. file: `src/gobby/memory/services/knowledge_graph/code_linker.py`.
- 4.1.2 - Integration test asserts all metadata properties land on the edge. test: `tests/memory/services/knowledge_graph/test_code_linker.py::test_bridge_metadata`.

### 4.2 Add provenance metadata to entity-to-entity relationships [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/services/knowledge_graph/writer.py`
- `src/gobby/memory/services/knowledge_graph/service.py`
- `src/gobby/memory/types.py`

LLM-extracted entity relationships currently land in FalkorDB with no provenance. Three changes:

1. Extend the `Relationship` dataclass with optional fields: `confidence: float | None`, `source_memory_ids: list[str]`, `provenance: Literal['INFERRED', 'AMBIGUOUS']` (default `'INFERRED'`). Extraction pipeline marks partial-recovery edges as `AMBIGUOUS`.

2. `KnowledgeGraphService` populates the new fields from extraction output: `confidence` from the prompt response when present (default `0.6` when omitted), `source_memory_ids` listing the memory IDs the relationship was extracted from.

3. `KnowledgeGraphWriter.merge_relationship` builds a properties dict and passes it through to `falkor.merge_relationship(properties=...)` (the underlying client already accepts it). Properties set: `provenance`, `confidence`, `source_system='gobby-memory'`, `source_memory_ids`, `updated_at=timestamp()`.

```python
async def merge_relationship(self, relationship: Relationship) -> None:
    props = {
        "provenance": relationship.provenance,
        "confidence": relationship.confidence if relationship.confidence is not None else 0.6,
        "source_system": "gobby-memory",
        "source_memory_ids": relationship.source_memory_ids,
    }
    await self._falkor.merge_relationship(
        source_key=relationship.source,
        target_key=relationship.target,
        rel_type=relationship.relationship,
        properties=props,
    )
```

**Acceptance:**

- 4.2.1 - `Relationship` dataclass carries `confidence`, `source_memory_ids`, `provenance` fields. file: `src/gobby/memory/types.py`.
- 4.2.2 - `KnowledgeGraphWriter.merge_relationship` passes the properties dict through to the FalkorDB client. file: `src/gobby/memory/services/knowledge_graph/writer.py`.
- 4.2.3 - Integration test asserts entity-to-entity edges land with provenance, confidence, source_memory_ids, source_system. test: `tests/memory/services/knowledge_graph/test_writer.py::test_entity_relationship_metadata`.
- 4.2.4 - Partial-extraction relationships land with `provenance='AMBIGUOUS'`. test: `tests/memory/services/knowledge_graph/test_service.py::test_ambiguous_extraction`.

## P5: UI surfaces
`kind: framing`

**Goal**: Display provenance/confidence on edges in both graph views; add project graph report tab.

### 5.1 Provenance display in code-graph view [category: code]
`kind: deliverable`

Targets:
- `web/src/components/code-graph/`
- `web/src/hooks/useCodeGraph.ts`

Extend the code-graph edge rendering to display a provenance badge (extracted / inferred / ambiguous) and a confidence indicator. Render `source_system` when present. Update `useCodeGraph.ts` types to include the new optional fields on edge results. Follow the impeccable design system: grayscale-legible badge state (lightness + icon, not just hue), AA contrast, 44px touch targets, no border-left/border-right wider than one pixel.

**Acceptance:**

- 5.1.1 - Code-graph edges display provenance, confidence, source_system. file: `web/src/components/code-graph/`.
- 5.1.2 - Provenance badge legible in grayscale. behavior: "edge badge survives grayscale screenshot" in `web/src/components/code-graph/`.

### 5.2 Provenance display in memory knowledge-graph view [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `web/src/components/memory/KnowledgeGraph.tsx`
- `web/src/hooks/useMemory.ts`

Extend `KnowledgeGraph.tsx` to display provenance and confidence on each relationship (entity→entity edges, RELATES_TO_CODE bridges to code symbols). Reuse the badge component pattern from 5.1. Update `KnowledgeRelationship` type in `useMemory.ts` to include the new optional fields.

**Acceptance:**

- 5.2.1 - Memory graph edges display provenance, confidence, source_system. file: `web/src/components/memory/KnowledgeGraph.tsx`.
- 5.2.2 - RELATES_TO_CODE bridges show INFERRED provenance and the Qdrant similarity confidence. behavior: "bridge edges display INFERRED + score" in `web/src/components/memory/KnowledgeGraph.tsx`.

### 5.3 Project graph report tab [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `web/src/components/activity/GraphReportTab.tsx`
- `web/src/components/activity/GraphReportTabData.ts`
- `web/src/components/activity/GraphReportTabDetailPanel.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

File family per `docs/guides/one-surface-tab-recipe.md`. Last three are registration touches (tab union, validity array, render switch). Tab fetches `/api/code-index/graph/report`, renders structured fields (high-degree nodes, blast-radius hotspots, bridge stats) with the rendered markdown as a copy-friendly fallback view.

**Acceptance:**

- 5.3.1 - Graph Report tab exists, fetches report, renders structured fields. file: `web/src/components/activity/GraphReportTab.tsx`.
- 5.3.2 - Tab visible in activity panel dropdown after three-file registration. behavior: "tab visible in dropdown" in `web/src/components/activity/ActivityPanelTabs.tsx`.

## P6: Slim code_index module
`kind: framing`

**Goal**: After graph.py is gone and consumers cut over, slim `storage.py` to read-only and `models.py` to what orchestration still needs. Files that stay: `maintenance.py`, `sync_worker.py`, `trigger.py`, `summarizer.py`, `context.py`.

### 6.1 Slim storage.py to reads [category: refactor] (depends: 2.2, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/code_index/storage.py`

gcode now owns Postgres writes via `gcode index` and `gcode graph sync-file`. Remove duplicate write methods from `CodeIndexStorage`. Keep read methods that orchestration (sync_worker, maintenance) needs to query sync state, file lists, pending vector syncs.

**Acceptance:**

- 6.1.1 - `storage.py` exposes only read methods plus `mark_vectors_synced` / `get_pending_sync_files` (vector sync coordination stays Python). file: `src/gobby/code_index/storage.py`.
- 6.1.2 - `tests/code_index/test_storage.py` adjusted: write-path tests deleted, read-path tests kept. test: `tests/code_index/test_storage.py`.

### 6.2 Slim models.py [category: refactor] (depends: 6.1)
`kind: deliverable`

Targets:
- `src/gobby/code_index/models.py`

Keep dataclasses still referenced by orchestration (`Symbol`, `IndexedFile`, `ContentChunk` if sync_worker reads them). Delete ones only consumed by the deleted Python graph module (`ParseResult`, `IndexResult` if not used elsewhere). Verify with grep before deleting.

**Acceptance:**

- 6.2.1 - `models.py` contains only types orchestration still uses. file: `src/gobby/code_index/models.py`.
- 6.2.2 - `grep` for each deleted dataclass returns zero hits in `src/gobby/`. behavior: "no remaining references to deleted dataclasses" in `src/gobby/`.

## Test Plan
`kind: framing`

- `tests/test_gcode_gateway.py` covers all gateway methods with real gcode binaries.
- `tests/servers/routes/test_code_index_routes.py` passes against subprocess shims, including new `/report` route.
- `tests/code_index/test_sync_worker.py::test_sync_pass_uses_gateway` confirms sync_worker uses gateway.
- `tests/memory/services/knowledge_graph/test_code_linker.py` asserts new bridge metadata.
- `tests/code_index/test_gcode_phase7_contract.py` passes against gcode binary.
- Web UI: `cd web && npm run type-check && npm run test -- --run` green for affected components. chrome-devtools MCP verifies edge badges in both graph views and report tab.
- End-to-end: start daemon, trigger a file change, verify sync_worker → gateway → gcode → FalkorDB chain works and edge metadata lands.

## Acceptance Criteria
`kind: framing`

- `src/gobby/code_index/graph.py` deleted; no remaining imports.
- All `/api/code-index/graph/*` routes work via subprocess shims with identical response shapes.
- New `/api/code-index/graph/report` endpoint live.
- `sync_worker` uses gateway for FalkorDB writes; orchestration logic untouched.
- `KnowledgeGraphCodeLinker` writes RELATES_TO_CODE with provenance metadata (small Python edit; bridge writes stay Python).
- Web UI renders provenance/confidence on edges in both code-graph and memory knowledge-graph views.
- Activity panel exposes Graph Report tab.
- `maintenance.py`, `sync_worker.py`, `trigger.py`, `summarizer.py`, `context.py` still present and functional.
- Phase 7 contract test passes.

## Plan Changelog
`kind: framing`

<!-- Updated by adversary review rounds -->
