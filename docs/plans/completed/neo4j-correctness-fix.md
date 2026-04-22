# Neo4j Correctness Cutover Plan

## Summary

  Fix Neo4j as two derived projections, not source-of-truth storage:

  - Memory KG: stabilize entity identity, scope it correctly, make processing truthful, and update the KG UI to use stable ids.
  - Code graph: remove the DEFINES vs CALLS identity mismatch, add explicit operator surfaces, and coordinate the schema change with gcode in the
    separate `gobby-cli` repository.

  No backward compatibility for existing Neo4j contents is required. After the changes land, clear and rebuild both projections. Do not add long-lived
  compatibility branches or feature flags.

## 1. Memory KG identity, constraints, and add_to_graph() contract

  Add src/gobby/memory/identity.py with explicit normalization and key generation.

  Normalization rules:

  - NFKC normalize
  - trim outer whitespace
  - collapse internal whitespace runs to one space
  - casefold
  - preserve punctuation to avoid collisions such as C vs C++

  Exports:

  - normalize_entity_name(name: str) -> str
  - entity_key(project_id: str | None, name: str) -> str

  Identity rules:

  - key is based on project scope + normalized name
  - entity_type is metadata, not identity

  Entity nodes carry:

  - entity_key
  - name
  - entity_type
  - project_id
  - timestamps
  - embedding

  Neo4j constraints/indexes:

  - _Entity.entity_key UNIQUE
  - Memory.memory_id UNIQUE
  - _Entity(project_id) or _Entity(project_id, entity_type) index for scoped reads

  Change KnowledgeGraphService.add_to_graph() in src/gobby/memory/services/knowledge_graph.py:81 to return KnowledgeGraphResult instead of None.

  Statuses:

  - success
  - noop_no_entities
  - partial_failure
  - retryable_failure
  - deterministic_failure

  Neo4j writes and reads in src/gobby/memory/neo4j_client.py must MERGE and MATCH by entity_key, never by name.

## 2. Memory KG lifecycle, routes, invalidation, and reconcile

  Update src/gobby/sessions/lifecycle.py:171 so graph_processed is set only for:

  - success
  - noop_no_entities

  Fix src/gobby/servers/routes/memory.py:243 rebuild flow to call:

  - add_to_graph(content, memory_id=..., project_id=...)

  Tighten KG route contracts:

  - /api/memories/graph/entities returns entity nodes only
  - nodes include entity_key, name, entity_type, project_id
  - edges reference source_key / target_key
  - /api/memories/graph/entities/{entity_key}/neighbors uses stable ids in the path
  - project_id query param is accepted and honored consistently

  Keep crossref graph behavior unchanged:

  - /api/memories/graph
  - web/src/components/memory/KnowledgeGraph.tsx

  Invalidation and repair:

  - keep bundled /api/memories/invalidate behavior
  - add a Neo4j-only clear/rebuild operator path for KG cutover and targeted repair
  - keep reconcile_stores() as a repair tool, not the primary cutover path
  - update reconcile logic to remain correct under the new identity model
  - orphan cleanup during reconcile should use the new scoped/entity-key-aware helpers

## 3. Memory KG deletion and scoped orphan cleanup

  Extend existing orphan cleanup instead of hand-waving a new behavior.

  In src/gobby/memory/services/knowledge_graph.py:384:

  - change remove_orphaned_entities() to accept scope, project_id: str | None = None

  Behavior:

  - project_id=<id> sweeps that specific project
  - project_id=None sweeps global entries
  - keep a separate all-project maintenance path if needed

  Wire scoped cleanup into:

  - remove_memory_from_graph()
  - remove_memories_from_graph()
  - clear_project_graph()

  Routine memory deletion should leave Neo4j truthful without waiting for later reconcile runs.

## 4. Memory KG UI

  Update:

  - web/src/hooks/useMemory.ts
  - web/src/components/memory/KnowledgeGraph.tsx

  Changes:

  - add entity_key to frontend KG types
  - use entity_key as node identity
  - use source_key / target_key in relationships
  - merge graph data by stable id, not name
  - fetch neighbors by entity_key
  - keep name as display text only

## 5. Code graph: target model and relationship coverage

  The concrete bug to remove is:

  - DEFINES writes canonical project symbols keyed by id + project
  - CALLS currently creates or matches callee nodes by name + project

  Canonical project symbol identity:

  - CodeSymbol {id, project}

  This plan keeps IMPORTS as:

  - CodeFile -> CodeModule {name, project}

  This plan fixes:

  - RELATES_TO_CODE must target canonical CodeSymbol {id, project}
  - MENTIONED_IN remains memory-KG-only and is not part of the code-graph cutover

  Node kinds:

  - CodeSymbol {id, project}
  - UnresolvedCallee {...} for unresolved same-project call targets
  - ExternalSymbol {...} for external call targets when analyzer confidence exists

  Required gobby changes in src/gobby/code_index/graph.py and src/gobby/code_index/sync_worker.py:

  - stop creating name-keyed CodeSymbol callees
  - rewrite CALLS against canonical/unresolved/external targets
  - leave IMPORTS targeting CodeModule
  - update reads for:
      - find_callers
      - find_usages
      - find_blast_radius
      - get_file_graph
      - get_symbol_neighbors
  - make graph writes raise on failure
  - only set graph_synced=1 after the full sync succeeds

## 6. gcode coordinated cutover

  Update gobby-cli:crates/gcode/src/neo4j.rs so:

  - project symbol lookup ultimately targets CodeSymbol {id, project}
  - name resolution happens before graph queries, not as graph identity
  - callers/usages/batch/blast-radius queries understand canonical, unresolved, and external call targets
  - imports continue to read CodeModule

  Update any docs/help text in gobby-cli that describe the old CodeSymbol {name, project} contract.

  Cutover sequence:

  1. finalize the new code-graph schema in gobby
  2. update gcode readers to that schema
  3. validate gcode callers, usages, imports, and blast-radius
  4. add code-graph clear/rebuild operator surface in gobby
  5. land gobby code-graph write/read changes
  6. clear and rebuild the Neo4j code graph
  7. cut over to the new gcode binary

  No compatibility layer and no dual-read rollout.

## 7. Code graph UI

  Keep the existing nodes / links shape if possible.

  Update:

  - web/src/hooks/useCodeGraph.ts
  - web/src/components/code-graph/CodeGraphExplorer.tsx

  Only add minimal node metadata if needed to render unresolved/external node kinds intentionally.

## 8. Config cleanup

  Remove legacy fallbacks in src/gobby/memory/manager.py for:

  - neo4j_graph_search
  - neo4j_graph_min_score
  - neo4j_rrf_k

  Use canonical config only under databases.neo4j:

  - graph_search: bool
  - graph_min_score: float
  - rrf_k: int

  If missing from the canonical config model, add them in the same change. Update fixtures and tests to stop patching legacy attrs.

## 9. Operator surfaces and persistent failure signal

  These are required work.

  Add:

  - Neo4j-only memory KG clear/rebuild path
  - project-scoped code-graph clear/rebuild path

  For code-graph failure state, pin a minimal persistent model:

  - keep graph_synced as success flag
  - use graph_sync_attempted_at as the lightweight persistent failure marker
  - set graph_sync_attempted_at when a graph sync is attempted; leave it NULL when no graph sync has been attempted yet or the project has been explicitly reset for rebuild
  - goal is to distinguish “attempted and failed” from “not yet attempted”
  - do not expand this into a larger telemetry project

## 10. Implementation order

  1. Add src/gobby/memory/identity.py and unit tests
  2. Add KG Neo4j constraints/indexes
  3. Add KnowledgeGraphResult and change add_to_graph() return contract
  4. Rewrite memory KG Neo4j client/service writes and reads to canonical entity_key
  5. Update lifecycle processing to honor terminal vs retryable outcomes
  6. Fix memory KG rebuild route and stable-id route contracts
  7. Update memory KG frontend to stable ids
  8. Extend scoped orphan cleanup and wire it into normal memory deletion and reconcile flows
  9. Add Neo4j-only memory KG clear/rebuild operator path
  10. Finalize code-graph canonical symbol model and relationship coverage
  11. Update gcode readers to the new code-graph schema
  12. Validate gcode graph commands
  13. Rewrite gobby code-graph writes and reads
  14. Add code-graph clear/rebuild operator surface
  15. Add minimal persistent code-graph failure marker and fail-closed sync behavior
  16. Update code-graph frontend for additive node metadata if needed
  17. Remove legacy Neo4j config fallbacks and update tests
  18. Clear and rebuild Neo4j projections for cutover validation

## 11. Test plan

  - Memory KG identity:
      - same normalized name in same project -> same entity_key
      - same name in different projects -> different entity_key
      - global vs project-scoped entity -> different entity_key
      - type drift updates metadata, not identity
      - repeated ingestion does not duplicate nodes
  - Memory KG lifecycle:
      - noop_no_entities is terminal
      - transient Neo4j failure leaves memory unprocessed
      - partial write leaves memory unprocessed
      - full success marks processed
      - rebuild/reporting aggregates structured outcomes correctly
  - Memory KG routes/UI:
      - entity routes return stable ids and keyed edges
      - neighbor route accepts entity_key
      - same-name entities do not collapse in UI
      - crossref memory graph remains unchanged
  - Memory KG deletion/invalidation/reconcile:
      - deleting a memory triggers scoped orphan cleanup
      - project-scoped cleanup does not delete another project’s entities
      - Neo4j-only clear/rebuild path works without touching Qdrant/FTS
      - reconcile remains correct under the new identity model
  - Code graph correctness:
      - DEFINES and CALLS resolve to the same canonical symbol identity
      - unresolved and external call targets follow the explicit node model
      - IMPORTS remains correct with CodeModule
      - RELATES_TO_CODE targets canonical CodeSymbol
      - graph write failure leaves graph_synced=0
  - gcode cutover:
      - callers, usages, imports, and blast-radius work against rebuilt graph
      - output remains understandable for unresolved/external cases
  - Code graph UI:
      - existing nodes / links shape still works
      - additive node metadata, if introduced, renders correctly
  - Config:
      - only canonical databases.neo4j.* knobs are used
      - no legacy neo4j_* access remains in MemoryManager

## Assumptions

  - Existing Neo4j projection contents will be cleared and rebuilt
  - gobby-cli only needs coordinated changes for direct code-graph reads
  - gobby and gobby-cli can be cut over together for the code graph
  - no long-lived compatibility flags or dual-route rollout are needed
