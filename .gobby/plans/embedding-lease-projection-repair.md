# Repair vectors after embedding lease recovery

Plan artifact: `.gobby/plans/embedding-lease-projection-repair.md`.

## Overview
`kind: framing`

`#20294` (`36e2272b3`) already recovers a fenced serving lease: `_renew_embedding_lease` routes `EmbeddingGenerationLeaseLost` into `_reacquire_lease`, which re-acks a matching generation or rebuilds `memory_services`. Fenced Qdrant calls are classified as recoverable availability failures. That work is closed and must not be re-implemented.

A create or supersede that hits a fenced store still persists in Postgres with `vector_needs_reindex = TRUE` and a ledger tombstone. `IndexingService.reconcile_stores` already upserts those ids and deletes Qdrant orphans. Nothing calls it when a lease comes back in-process. The daemon loop is daily; a restart runs one startup pass. After an in-process re-ack, missed points wait up to 24 hours.

This plan only kicks that existing healer after a successful re-ack or a healthy `memory_services` rebuild.

## Constraints
`kind: framing`

- Do not change `_renew_embedding_lease`, `EmbeddingServingLease.renew`, or `is_recoverable_vector_store_error`.
- Do not add `vector_serving` to MCP payloads.
- Do not add `is_embedding_serving_fenced` or retune fenced-search log wording. `#20294` already maps `EmbeddingGenerationLeaseLost` onto the recoverable fallback.
- Do not fail `create_memory` when Qdrant is fenced.
- Do not add a new healer, change `memory_reconcile_loop`'s 24h interval, or rewrite projection-ledger schema.
- Call `IndexingService.reconcile_stores(dry_run=False)` on the *live* `memory_services` bundle from `config_runtime.capture()`. Do not close over the bundle being built.
- Call repair only after serving is usable: successful same-generation re-ack, or a rebuild whose `memory_services` has no `failed_live_keys` entry. A mismatch that only requests rebuild must not repair yet.
- Repair-pass exceptions log and must not kill the renew thread.
- No schema, migration, or compatibility shim.

## P1: Kick existing reconcile on recovery
`kind: framing`

**Goal:** The first successful return to serving repairs pending vectors and Qdrant orphans without waiting for the daily loop.

### 1.1 Schedule reconcile_stores after re-ack and rebuild [category: code]
`kind: deliverable`

Targets:
- `src/gobby/runner_init/embedding_lease.py::_ManagedEmbeddingLease`
- `src/gobby/runner_init/embedding_lease.py::_ManagedEmbeddingLease.__init__`
- `src/gobby/runner_init/embedding_lease.py::_reacquire_lease`
- `src/gobby/runner_init/services.py::_build_memory_services`
- `src/gobby/runner_init/services.py::_request_memory_services_rebuild`
- `src/gobby/runner_init/services.py::MemoryServiceBundle.memory_manager`
- `src/gobby/memory/services/indexing.py::IndexingService.reconcile_stores`
- `tests/ai/test_runner_lease_lifecycle.py::test_reacquire_mismatch_requests_rebuild_and_stays_fenced`

Add `request_projection_repair: Callable[[], None] | None = None` to `_ManagedEmbeddingLease.__init__`. Default `None` so existing tests need no constructor churn. Store it on the handle. Invoke only when it is not `None`. Wire it in `_build_memory_services` next to the existing rebuild lambda.

Call `IndexingService.reconcile_stores(dry_run=False)` on the live captured `MemoryServiceBundle` after a successful same-generation re-ack and after a healthy `memory_services` rebuild. Do not call it on mismatch rebuild requests.

**Acceptance:**

- 1.1.1 - Successful same-generation re-ack schedules one live `IndexingService.reconcile_stores(dry_run=False)` and does not request rebuild. symbol: `_reacquire_lease`.
- 1.1.2 - A successful `memory_services` rebuild schedules one live reconcile after the subscriber is healthy. symbol: `_request_memory_services_rebuild`.
- 1.1.3 - A generation mismatch requests rebuild and does not schedule reconcile while serving stays fenced. test: `tests/ai/test_runner_lease_lifecycle.py::test_reacquire_mismatch_requests_rebuild_and_stays_fenced`.

## Verification
`kind: verification`

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_runner_lease_lifecycle.py -v
uv run ruff check src/gobby/runner_init/embedding_lease.py src/gobby/runner_init/services.py
uv run ruff format src/gobby/runner_init/embedding_lease.py src/gobby/runner_init/services.py
```

## V1 Plan Changelog
`kind: verification`

Draft. `#20294` lease recovery, MCP `vector_serving`, and fenced-log reclassification are out of scope.
