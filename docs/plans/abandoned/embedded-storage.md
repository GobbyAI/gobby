# Embedded Kuzu + Qdrant + Session Search

## Status

Archived in `abandoned/` on 2026-04-19.

This document is kept for design history, not as an active roadmap. The idea
was reconsidered, but it never reached the level of migration safety,
operational detail, or search-compatibility needed to schedule implementation.
The earlier draft left too many technical concerns unresolved.

## Why This Stays Abandoned

- The storage config shape coupled graph and vector deployment modes too
  tightly.
- The migration story for `gobby graph migrate` was underspecified and unsafe
  to rerun.
- The standalone/gcode behavior description removed too much functionality
  instead of documenting graceful fallback.
- The draft did not give reviewers a clear checklist for what had been resolved
  and what still needed validation.

## Reconsidered Design Direction

If this work is revived later, the minimum acceptable design is:

### 1. Storage config must be backend-local

Do not use a single `use_docker` flag plus top-level `graph_backend` and
`vector_backend` toggles. The backend choice and deployment mode must be
selected independently for graph and vector storage.

Use a shape like:

```yaml
storage:
  graph:
    backend: neo4j   # neo4j | kuzu
    kuzu:
      mode: embedded # embedded | remote
      path: ~/.gobby/kuzu/
    neo4j:
      mode: remote   # embedded | remote
      url: http://localhost:8474
      auth: neo4j:password
      database: neo4j

  vector:
    backend: qdrant  # qdrant
    qdrant:
      mode: embedded # embedded | remote
      path: ~/.gobby/qdrant/
      url: http://localhost:6333
      api_key: null
      collection_prefix: code_symbols_
```

Rules:

- `storage.graph.backend` selects the graph implementation
- `storage.vector.backend` selects the vector implementation
- each backend's `mode` decides whether `path` or `url` is used
- there is no global `use_docker`

### 2. `gobby graph migrate` must be explicitly safe to rerun

The migration command cannot just be "copy Neo4j into Kuzu and vectors into
`kg_entities`". It must include:

- pre-migration validation:
  - verify Neo4j connectivity before copying anything
  - verify Kuzu path exists or can be created
  - verify Kuzu schema/table requirements
  - verify Qdrant connectivity or embedded store accessibility
  - verify the `kg_entities` collection exists or can be created with the
    required vector dimension
- resumable progress tracking:
  - persist migration state in a local state store
  - checkpoint progress by entity/relationship batch
  - resume without duplicating partially written graph/vector data
- idempotent writes:
  - use deterministic upsert keys for Kuzu/Qdrant records
  - use stable transaction markers or checkpoint metadata so a second run can
    skip or safely overwrite already-migrated rows
  - specifically prevent duplicate writes into the `kg_entities` Qdrant
    collection
- dry-run mode:
  - validate prerequisites and report planned actions
  - write no graph or vector data
- post-migration verification:
  - compare source/destination entity counts
  - compare relationship counts
  - run sampled integrity checks on known entities and relationships
  - verify vector count and sample nearest-neighbor results in `kg_entities`
- rollback procedure:
  - configuration toggle back to Neo4j for graph reads/writes
  - cleanup commands for partially migrated Kuzu rows
  - cleanup commands for partial `kg_entities` vector entries
  - failure logging with enough context to troubleshoot the failed batch

Representative command shape:

```bash
gobby graph migrate \
  --source neo4j \
  --target kuzu \
  --vector-target qdrant \
  [--resume] \
  [--dry-run]
```

### 3. Standalone behavior must degrade, not amputate

The earlier "Standalone (no daemon): FTS5 only" wording was too blunt. If this
work returns, the standalone contract should be:

- prefer the daemon API whenever the daemon is available
- standalone gcode may use embedded Kuzu/Qdrant/embedding engines directly when
  they are actually present and supported
- if graph services are unavailable, graph commands return `[]`
- if semantic/vector services are unavailable, semantic search returns `[]`
- FTS5 keyword search remains available regardless

That means references to `neo4j.rs` removal and direct Qdrant REST cleanup stay
conditional. Delete those code paths only after the Rust migration or after the
embedded alternatives are fully supported end to end.

### 4. Kuzu concurrency guidance must match the real async model

Use Kuzu's `AsyncConnection` for concurrent reads.

Writes still need explicit serialization. The acceptable patterns are:

- a shared `asyncio.Lock` around writes, or
- a dedicated writer task / queue

Do not describe `asyncio.to_thread()` as the write-serialization strategy.

## Migration-Specific Verification Required Before Scheduling

If this work is ever revived, reviewers should expect these explicit checks:

1. Run `gobby graph migrate` against a representative sample dataset that
   includes known entities, known relationships, and representative vectors.
2. Query Neo4j and Kuzu for the same sample entities/relationships and compare
   results directly.
3. Verify all expected node kinds and relationship types are present after
   migration.
4. Verify vectors landed in the Qdrant collection named `kg_entities`.
5. Run equivalent vector searches before and after migration and compare the
   top results for representative queries.
6. Run `gobby graph migrate` twice and confirm:
   - no duplicate entities
   - no duplicate relationships
   - no conflicting vector entries in `kg_entities`

## Technical Concerns That Must Be Closed Before Revival

These were the review blockers on the abandoned draft. Any future proposal must
close them explicitly:

| Concern | Required Resolution |
| --- | --- |
| Data consistency | Deterministic entity/relationship identifiers plus checkpointed migration state |
| Embedding storage format | Stable vector dimension and payload schema for `kg_entities` |
| Query latency | Benchmarks for Kuzu graph traversals and Qdrant vector lookups on representative datasets |
| Session indexing | Concrete schema and backfill plan for session nodes, edges, and embeddings |
| Backup / restore | Documented backup/restore flow for embedded Kuzu and embedded/remote Qdrant |
| Security / isolation | Clear daemon-owned access boundaries and standalone fallback behavior |

## Next Steps If Reopened

- Reopen this as a fresh active plan outside `abandoned/`.
- Replace this archive note with a new, implementation-ready plan.
- Prove config, migration, and fallback behavior with a small spike first.
- Validate search parity and idempotent migration on a representative dataset.
- Only schedule implementation after the checklist above is green.
