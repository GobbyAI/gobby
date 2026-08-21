# FalkorDB Timeouts: Root Cause and Fix

**Plan ID:** falkordb-timeouts-root-cause

Plan artifact: `.gobby/plans/falkordb-timeouts-root-cause.md`

> Canonical plan artifact (Lightweight Gobby). Drafted via CLI plan mode on 2026-08-20.
> Validation: `uv run gobby plans validate .gobby/plans/falkordb-timeouts-root-cause.md`
> Lightweight process: no enhancement, adversary, or build phases unless opted in.

## Overview
`kind: framing`

Daemon logs show two FalkorDB failure clusters: `gcode graph sync-file` failing with
`FalkorDB graph query failed: Resource temporarily unavailable (os error 35)` (2026-08-19
21:47, retried and exhausted), and the memory knowledge graph logging
`FalkorDB connection failed: Timeout reading from 127.0.0.1:16379` across merge_node,
set_node_vector, MENTIONED_IN and orphan cleanup (2026-08-20 02:17–02:21). Today's
`a536c8f3e8` (#20601) was live for the first incident and did not help. Both are
client-side socket read timeouts against a healthy server whose `gobby_code` write
batches take 15–42 s because every MERGE key except `project` is unindexed; FalkorDB
holds the Redis GIL for the whole write query, so each batch freezes every graph and
client. This plan removes the slow path, makes server-side timeouts authoritative for
writes, fixes timeout classification in the Python client, and fixes an unrelated hourly
prune self-deadlock found during the investigation.

## Constraints
`kind: framing`

- No backward compatibility obligations (0.5.0 unshipped).
- Crate changes are live only after `cargo build --release` and a new-inode install
  into `~/.gobby/bin/` (`cp` to a dotfile, `mv -f` over the name).
- Python and compose changes take effect on `uv run gobby restart`; the compose
  reconcile recreates the falkordb container, which is a brief outage while its RDB
  reloads.
- Named defaults: Python KG `socket_timeout` stays 15 s; per-query server timeout is
  socket minus one second (floor 1 ms) on both clients; `TIMEOUT_DEFAULT 30000`,
  `TIMEOUT_MAX 0` (no cap), `MAX_QUEUED_QUERIES 25` and `RESULTSET_SIZE 10000` restated
  unchanged; Redis `save 3600 1 300 100`; `GRAPH_SYNC_BATCH_SIZE` stays 500.
- Non-goals: no query-phase retry in `gcore/falkor.rs` (retrying an in-flight write
  doubles server load; the server-side bound replaces it); no change to
  `RESULTSET_SIZE` semantics (it silently truncates reads over 10k rows — flagged
  for a separate decision); no Docker VM sizing; no change to `cleanup_orphans`,
  which only runs under `graph rebuild` / `graph cleanup-orphans`.
- Live FalkorDB checks use the container's own env so the password never leaves it:
  `docker exec services-falkordb-1 sh -c 'redis-cli --no-auth-warning -a "$GOBBY_FALKORDB_PASSWORD" <cmd>'`.

## Investigation Findings
`kind: framing`

- FalkorDB never went down: container `services-falkordb-1` (redis 8.6.3, module 41807)
  has `RestartCount 0`, `OOMKilled false`, passing healthcheck, `rejected_connections 0`;
  host has 47 GiB free; `pmset -g log` shows no sleep/wake near either incident.
- `GRAPH.SLOWLOG gobby_code`: `ADD_SYMBOL_CALLS_CYPHER` batch 42,057 ms (sync token
  `12921:tests/co…`, the poison-pill file), `ADD_UNRESOLVED_CALLS_CYPHER` 24,780 ms,
  `ADD_DEFINITIONS_CYPHER` 18,106 ms, `ADD_EXTERNAL_CALLS_CYPHER` 15,461 ms, per-file stale
  CALLS sweep 1,736 ms.
- `CALL db.indexes()` on `gobby_code`: one indexed property per label — `project`.
  `GRAPH.EXPLAIN` of the calls batch shows `Node By Index Scan | (caller:CodeSymbol)`
  feeding a `Filter` — the `project` index yields all 401,217 symbols of this project,
  then filters on `id`, per UNWIND row, twice per row, 500 rows per batch. Graph size:
  579,458 CodeSymbol, 31,363 CodeFile, 1,709,652 CALLS, 1.1 GB.
- FalkorDB `QueryCtx_AcquireWriteLock` takes the Redis GIL plus the graph write lock at
  a write query's first mutation and releases both only in `QueryCtx_ReleaseLock` at
  query end (2PL). One 42 s batch therefore stalls every graph and client. The nightly
  full reindex (cron `0 2 * * *`, 02:00:47–03:00:58) starved the memory-dream run (also
  `0 2 * * *`) whose `gobby_kg` writes use a 15 s socket timeout.
- Incident 1 needed no contention: the file's own CALLS batch exceeds the 30 s socket
  budget, the Python retry re-submits the same write twice while the server is still
  executing it, then the file cools off for 300 s and repeats.
- `GRAPH.CONFIG GET *`: `TIMEOUT 1000 TIMEOUT_DEFAULT 0 TIMEOUT_MAX 0` — stock image
  defaults; the compose template sets no `FALKORDB_ARGS`. In FalkorDB's dispatcher
  `timeout_rw` is true only when `TIMEOUT_DEFAULT` or `TIMEOUT_MAX` is non-zero and
  `cmd_query.c` enforces `timeout != 0 && !index_op && (readonly || timeout_rw)`, so the
  `timeout 29000` argument added by #20601 never applies to `graph sync-file` writes.
- Hourly global prune: `~/.gobby/logs/code-index-maintenance.log` shows
  `gcode prune --force --retention-days 30` killed at 120 s on 65 consecutive runs since
  2026-08-17 10:12Z (it failed fast with `daemon_required` before). `prune_all_projects`
  holds `_global_lock` while awaiting the subprocess; that binary is a pure daemon client
  (`POST /api/code-index/prune`) whose handler calls `run_operator_global_prune`, which
  needs the same lock, so it only proceeds when the kill releases it — the 0.2 s
  `targeted_prune` rows one second after every kill.
- Python KG client collapses `redis.exceptions.TimeoutError` into "connection failed";
  the traversal breaker resets on socket timeouts; a server `Query timed out` maps to
  `FalkorQueryError`, which the writer treats as deterministic.
- Persistence: default `save 3600 1 300 100 60 10000` snapshots ~360 MB every 1–5 min
  during write bursts; 164 GB written in five days; one fork-GC collision logged.
  Write amplification, not the stall source — fixed in 2.1 by dropping the `60 10000`
  rule (`--save 3600 1 300 100`), which caps snapshots at one per 5 minutes under load
  and removes the BGSAVE-vs-fork-GC collisions; 1.1 shrinks the burst itself.

## P1: Code graph write path
`kind: framing`

**Goal**: Per-file graph sync costs milliseconds instead of tens of seconds so no single
batch can exceed a client budget or freeze the server.

### 1.1 Index every MERGE key and anchor per-file sweeps in gcode [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/graph/code_graph/write.rs::ensure_project_indexes`
- `crates/gcode/src/graph/code_graph/write/deletion.rs::delete_stale_file_graph_queries`
- `crates/gcode/src/graph/code_graph/write/deletion.rs::delete_file_graph_queries`
- `crates/gcode/src/graph/code_graph/tests.rs::*` — scope-reason: Cypher-text assertions change and new index/sweep/parity tests are added

Load the `rust` skill first. `gobby_code` indexes only `project` per label, so every
`MERGE (x:Label {key, project})` in the sync batches is a 401k-node scan per row.

1. In `write.rs`, replace the label-only constant consumed by `ensure_project_indexes`
   with `(label, properties)` pairs covering every MERGE key used by the batch
   constants in `write/mutation.rs` (`ADD_IMPORTS_CYPHER`, `ADD_DEFINITIONS_CYPHER`,
   `ADD_*_CALLS_CYPHER`, `ADD_INHERITS|EXTENDS|IMPLEMENTS_*_CYPHER`):

   ```rust
   const PROJECT_INDEXED_PROPERTIES: &[(&str, &[&str])] = &[
       ("CodeFile", &["project", "path"]),
       ("CodeSymbol", &["project", "id", "file_path"]),
       ("CodeModule", &["project", "name"]),
       ("UnresolvedCallee", &["project", "id"]),
       ("ExternalSymbol", &["project", "id"]),
   ];

   pub fn ensure_project_indexes(&mut self) -> anyhow::Result<()> {
       for (label, properties) in PROJECT_INDEXED_PROPERTIES {
           for property in *properties {
               self.client.ensure_exact_node_index(label, property)?;
           }
       }
       Ok(())
   }
   ```

   `GraphClient::ensure_exact_node_index` in `crates/gcore/src/falkor.rs` is reused as
   is: it issues `CREATE INDEX ON :Label(prop)` and swallows the already-indexed error.
   FalkorDB keeps one index per label, each call adds a field, and the planner
   intersects all indexed equality filters on a node. Index population is asynchronous
   (`CALL db.indexes()` reports `PENDING` → `OPERATIONAL`); existing graphs pick the
   fields up on the next `graph sync-file` / `index --sync-projections` because
   `with_code_graph` calls `ensure_project_indexes` on every invocation.

2. In `deletion.rs`, rewrite the project-wide relationship sweeps so they start from the
   now-indexed source node instead of `MATCH (s:CodeSymbol {project: $project})` plus a
   relationship-property filter. In `delete_stale_file_graph_queries`:

   ```cypher
   MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->(n {project: $project})
   WHERE r.content_hash = $content_hash
     AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
   DELETE r
   ```

   Every CALLS caller is a symbol defined in the synced file, `ADD_DEFINITIONS_CYPHER`
   sets `s.file_path` before the call batches run (`plan_sync_batches` order in
   `write/sync_plan.rs`: header, imports, definitions, calls, inheritance), and `r.file`
   equals `r.source_file_path` equals `call.file_path` in every mutation constant, so the
   anchored form matches the same edges as the old `r.file = $file_path OR
   r.source_file_path = $file_path` predicate.

   Split the `INHERITS|EXTENDS|IMPLEMENTS` sweep into two queries: CodeSymbol-sourced
   (same `{project, file_path}` anchor) and External/Unresolved-sourced:

   ```cypher
   MATCH (s {project: $project})-[r:INHERITS|EXTENDS|IMPLEMENTS]->(n {project: $project})
   WHERE (s:ExternalSymbol OR s:UnresolvedCallee)
     AND r.source_file_path = $file_path
     AND r.content_hash = $content_hash
     AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
   DELETE r
   ```

   (~19k External/Unresolved nodes instead of 401k symbols.) Apply the same anchoring
   to the matching sweeps in `delete_file_graph_queries` (its CALLS sweep already uses
   `{project, file_path}`; its inheritance sweep does not). Keep the final token-only
   symbol sweep unchanged — it is served by the new `file_path` index.

   Contingency, decided by the verification step rather than deferred: if
   `GRAPH.EXPLAIN` of any rewritten sweep still shows a `project`-only
   `Node By Index Scan`, add relationship indexes via a new
   `GraphClient::ensure_exact_relationship_index` (`CREATE INDEX FOR ()-[r:CALLS]-() ON
   (r.source_file_path)`, likewise for the inheritance types) and filter the sweep on
   `r.source_file_path` directly.

3. In `tests.rs`, update the Cypher-text assertions that pin the old sweep shapes and
   add: `project_indexes_cover_every_merge_key` (every `MERGE (x:L {…})` key in the
   mutation constants appears in `PROJECT_INDEXED_PROPERTIES`),
   `stale_sweeps_anchor_on_indexed_file_path` (no `delete_stale_file_graph_queries`
   relationship sweep matches a `CodeSymbol` by `project` alone),
   `file_delete_sweeps_anchor_on_indexed_file_path`, and
   `mutation_edges_keep_file_and_source_file_path_in_parity` (every constant that sets
   `r.file` sets `r.source_file_path` from the same row field).

4. Validate with `cargo fmt -p gobby-code -p gobby-core -- --check`,
   `cargo clippy -p gobby-code -p gobby-core`, `cargo test -p gobby-code graph`,
   `cargo test -p gobby-core falkor`; then `cargo build --release -p gobby-code -p
   gobby-wiki` and new-inode install both binaries (`gwiki` links gcore). No daemon
   restart is needed; the sync worker spawns the binary per file.

**Acceptance:**

- 1.1.1 - `ensure_project_indexes` creates `project` plus `path`/`id`/`file_path`/`name` fields per label as listed. symbol: `ensure_project_indexes`.
- 1.1.2 - Stale CALLS and inheritance sweeps anchor on `CodeSymbol {project, file_path}` (plus the External/Unresolved variant) and no longer filter on `r.file`. symbol: `delete_stale_file_graph_queries`.
- 1.1.3 - File-delete inheritance sweep uses the same anchoring. symbol: `delete_file_graph_queries`.
- 1.1.4 - Index coverage, sweep anchoring, and `r.file`/`r.source_file_path` parity are pinned. test: `crates/gcode/src/graph/code_graph/tests.rs::project_indexes_cover_every_merge_key`.
- 1.1.5 - Updated sweep-shape assertions pass. test: `crates/gcode/src/graph/code_graph/tests.rs::stale_sweeps_anchor_on_indexed_file_path`.
- 1.1.6 - Rebuilt `gcode` and `gwiki` are installed as new inodes under `~/.gobby/bin/`. file: `crates/gcode/src/graph/code_graph/write.rs`.

## P2: Server configuration and Python clients
`kind: framing`

**Goal**: The server bounds writes with rollback, the memory client reports and reacts
to timeouts correctly, and the hourly prune no longer deadlocks.

### 2.1 Set FalkorDB module and persistence arguments in the compose template [category: config]
`kind: deliverable`

Targets:
- `src/gobby/data/docker-compose.services.yml::falkordb`
- `tests/cli/installers/test_falkordb_installer.py::TestDockerComposeFalkorDB.test_falkordb_service_has_profile_ports_auth_and_browser`
- `tests/cli/installers/test_qdrant_installer.py::TestDockerComposeServices.test_falkordb_service_contract`

`src/gobby/data/docker-compose.services.yml` is the single template
(`tests/install/test_daemon_runtime_pins.py` asserts the gcore copy does not exist). The
falkordb service currently sets only `REDIS_ARGS=--requirepass …` and inherits the
image's `FALKORDB_ARGS=MAX_QUEUED_QUERIES 25 TIMEOUT 1000 RESULTSET_SIZE 10000`.

Change the `environment` block to:

```yaml
    environment:
      # Redis AUTH - REDIS_ARGS is the documented entry point for redis-server flags.
      # --save drops the stock `60 10000` rule: graph projections are rebuildable and
      # that rule rewrote ~360 MB every minute under write bursts.
      - REDIS_ARGS=--requirepass ${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor} --save 3600 1 300 100
      # Pass-through so the healthcheck below can read the same value.
      - GOBBY_FALKORDB_PASSWORD=${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor}
      # FALKORDB_ARGS is reserved for module options; do not put auth there. Setting it
      # replaces the image default wholesale, so the unchanged values are restated.
      # TIMEOUT_DEFAULT (not the deprecated read-only TIMEOUT) makes per-query
      # `timeout` bound write queries too, with rollback; TIMEOUT_MAX 0 = no cap.
      - FALKORDB_ARGS=MAX_QUEUED_QUERIES 25 TIMEOUT_DEFAULT 30000 TIMEOUT_MAX 0 RESULTSET_SIZE 10000
```

`--requirepass` stays first (`tests/cli/hub_backup/test_verify.py` checks the
`REDIS_ARGS=--requirepass ` prefix). Redis parses `--save 3600 1 300 100` as one `save`
directive with two rules. Extend the two compose assertions named in Targets to the new
`REDIS_ARGS` string and to the presence of the `FALKORDB_ARGS` line;
`tests/cli/test_compose_bind_address.py` and
`tests/cli/installers/test_postgres_compose_template.py` read the template and must
keep passing.

Rollout: `uv run gobby restart` → `reconcile_unified_compose` copies the changed
template and `_run_compose_up` runs `docker compose up -d`, which recreates the
falkordb container. Confirm with `GRAPH.CONFIG GET TIMEOUT_DEFAULT` → `30000` and
`CONFIG GET save` → `3600 1 300 100`.

**Acceptance:**

- 2.1.1 - The falkordb service declares `FALKORDB_ARGS` with `TIMEOUT_DEFAULT 30000 TIMEOUT_MAX 0` and the restated queue/result-set values, and `REDIS_ARGS` carries `--save 3600 1 300 100` after `--requirepass`. file: `src/gobby/data/docker-compose.services.yml`.
- 2.1.2 - Compose contract test pins the new environment lines. test: `tests/cli/installers/test_falkordb_installer.py::TestDockerComposeFalkorDB::test_falkordb_service_has_profile_ports_auth_and_browser`.
- 2.1.3 - Unified compose test pins the same lines. test: `tests/cli/installers/test_qdrant_installer.py::TestDockerComposeServices::test_falkordb_service_contract`.

### 2.2 Classify timeouts and bound queries server-side in the memory KG client [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/falkor_client.py::FalkorClient.__init__`
- `src/gobby/memory/falkor_client.py::FalkorClient.query`
- `src/gobby/memory/falkor_client.py::_raise_mapped_response_error`
- `src/gobby/memory/services/knowledge_graph/reader.py::KnowledgeGraphReader._is_query_timeout_error`
- `src/gobby/memory/services/knowledge_graph/reader.py::KnowledgeGraphReader.find_related_memory_ids`
- `tests/memory/test_falkor_client.py::*` — scope-reason: the `FakeGraph.query` fake gains a `timeout` kwarg and new timeout-mapping tests are added
- `tests/memory/test_graph_search_integration.py::TestFindRelatedMemoryIds.test_circuit_breaker_skips_after_repeated_query_timeouts`

Load the `python` skill first. `FalkorClient` (falkordb 1.6.1 asyncio, redis 7.4.0) is
built once per daemon with the default `timeout=15.0` and shared by the writer,
maintenance, reader, and code-linker services.

1. Add a transient subclass so existing `except FalkorConnectionError` paths keep
   requeueing without change, and a helper mirroring Rust's `query_timeout_from_socket`:

   ```python
   class FalkorTimeoutError(FalkorConnectionError):
       """Socket read or server-side query timeout; transient and retry-safe."""


   def _query_timeout_ms(socket_timeout: float) -> int:
       return max(1, int((socket_timeout - 1.0) * 1000))
   ```

2. `FalkorClient.__init__` stores `self._timeout = timeout` and
   `self._query_timeout_ms = _query_timeout_ms(timeout)`. `FalkorClient.query` passes
   the per-query timeout (`AsyncGraph.query(..., timeout=<ms>)` appends `timeout <ms>`
   to `GRAPH.QUERY`) so the server aborts and rolls back before the socket gives up, and
   maps errors distinctly; apply the same mapping in `ensure_unique_constraint`, which
   wraps the same exception pair:

   ```python
   try:
       result = await self._graph.query(cypher, params, timeout=self._query_timeout_ms)
   except redis.exceptions.TimeoutError as exc:
       raise FalkorTimeoutError(
           f"FalkorDB read timed out after {self._timeout:g}s: {exc}"
       ) from exc
   except redis.exceptions.ConnectionError as exc:
       raise FalkorConnectionError(f"FalkorDB connection failed: {exc}") from exc
   except redis.exceptions.ResponseError as exc:
       _raise_mapped_response_error(exc)
   ```

   `_raise_mapped_response_error` raises `FalkorTimeoutError(f"FalkorDB query timed
   out: {exc}")` when the response message contains `query timed out` (case-insensitive),
   after the existing auth check and before the `FalkorQueryError` fallback.

3. `KnowledgeGraphReader._is_query_timeout_error` returns `True` for
   `isinstance(error, FalkorTimeoutError)` before the existing message match. With that,
   the `except FalkorConnectionError` branch in `find_related_memory_ids` records a
   traversal timeout (breaker trips) for socket timeouts instead of calling
   `_record_traversal_success()`; no other change to that method.

4. Tests: extend `test_constructor_uses_async_falkordb_client` to assert `query`
   forwards `timeout=8500` for `socket_timeout=9.5`; add
   `test_query_maps_redis_timeout_error_to_falkor_timeout_error` (uses the existing
   `FakeRedisTimeoutError` from `tests/memory/conftest.py`, asserts the
   `read timed out after` wording and `isinstance(exc, FalkorConnectionError)`),
   `test_query_maps_query_timed_out_response_to_falkor_timeout_error`, and in
   `TestFindRelatedMemoryIds` a `test_circuit_breaker_counts_socket_timeouts` that
   reaches `consecutive_timeouts == 1` after a socket timeout. `FakeGraph.query` accepts
   `timeout=None`. `TestGracefulDegradation.test_add_to_graph_handles_falkordb_down` in
   `tests/memory/test_knowledge_graph.py` must keep passing unchanged.

Validate with `GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_falkor_client.py
tests/memory/test_graph_search_integration.py tests/memory/test_knowledge_graph.py -q`,
`uv run ruff format src/`, `uv run ruff check src/`, `uv run mypy src/`.

**Acceptance:**

- 2.2.1 - `FalkorClient.query` passes a server-side timeout of socket minus one second and raises `FalkorTimeoutError` for socket read timeouts and `Query timed out` responses. symbol: `FalkorClient.query`.
- 2.2.2 - `_raise_mapped_response_error` maps `query timed out` responses to `FalkorTimeoutError` ahead of `FalkorQueryError`. symbol: `_raise_mapped_response_error`.
- 2.2.3 - The traversal breaker counts socket timeouts. symbol: `KnowledgeGraphReader._is_query_timeout_error`.
- 2.2.4 - Socket-timeout mapping and wording are pinned. test: `tests/memory/test_falkor_client.py::test_query_maps_redis_timeout_error_to_falkor_timeout_error`.
- 2.2.5 - Breaker increments on a socket timeout. test: `tests/memory/test_graph_search_integration.py::TestFindRelatedMemoryIds::test_circuit_breaker_counts_socket_timeouts`.

### 2.3 Run the hourly global prune in-process instead of through gcode [category: code]
`kind: deliverable`

Targets:
- `src/gobby/code_index/prune.py::CodeIndexPruner.prune_all_projects`
- `src/gobby/code_index/prune.py::CodeIndexPruner._run_operator_global_prune_locked`
- `src/gobby/code_index/prune.py::_retry_dirty_projects`
- `src/gobby/code_index/prune.py::_failed_indexed_projects`
- `src/gobby/code_index/prune.py::_failed_project_ids`
- `src/gobby/code_index/prune.py::_collect_project_ids`
- `src/gobby/code_index/gcode_gateway.py::GcodeGateway.prune_all_projects`
- `tests/code_index/test_prune.py::*` — scope-reason: the global-prune tests move from subprocess/stdout fakes to the structured in-process outcome

Load the `python` skill first. The hourly cron handler (`create_code_index_prune_handler`
→ `CodeIndexPruner.prune_all_projects`) holds `self._global_lock` while it spawns
`gcode prune --force --retention-days N`; that binary only POSTs
`/api/code-index/prune`, whose handler (`global_prune` in
`src/gobby/servers/routes/code_index.py`) calls `run_operator_global_prune`, which takes
the same lock. The request therefore waits until the 120 s subprocess kill releases it.
The operator CLI path through the route is unchanged by this task.

1. Rewrite `CodeIndexPruner.prune_all_projects` to do the work in-process under the
   same lock, preserving the skip-when-locked behaviour, the `global_prune`
   maintenance-log event, `_clear_dirty_projects` on success, and the
   `CodeIndexPruneResult` shape:

   ```python
   async def prune_all_projects(self) -> CodeIndexPruneResult:
       if self._global_lock.locked():
           return {..., "status": "skipped", "message": "Code index prune skipped: global_locked", ...}
       async with self._global_lock:
           run_id = uuid4().hex
           started_at = _utc_now_iso()
           outcome = await self._run_operator_global_prune_locked(
               force=True,
               retention_days=self._context.config.content_retention_days,
           )
           status: Literal["completed", "failed"] = "failed" if outcome["failed"] else "completed"
           stdout = json.dumps(outcome, sort_keys=True)
           log_gcode_maintenance_event(
               log_file=_maintenance_log_file(self._context),
               event="global_prune",
               run_id=run_id,
               project_id=None,
               root_path=None,
               result=GcodeCommandResult(
                   command=("in-process", "global_prune"),
                   returncode=0 if status == "completed" else 1,
                   stdout=stdout,
                   stderr="",
                   started_at=started_at,
                   completed_at=_utc_now_iso(),
                   duration_seconds=<elapsed>,
                   timeout_seconds=None,
               ),
               status=status,
           )
           if status == "completed":
               await self._clear_dirty_projects()
           return {
               "success": status == "completed",
               "status": status,
               "run_id": run_id,
               "message": f"Code index prune completed: run_id={run_id} global:{status} "
                          f"failed={len(outcome['failed'])} skipped={len(outcome['skipped'])}",
               "stdout": stdout,
               "stderr": "",
               "retried_projects": 0,
           }
   ```

   `force=True` preserves today's behaviour (the cron passed `--force`, so the route ran
   with `force=True` and `_held_project_lock` waited up to
   `CODE_INDEX_PRUNE_LOCK_TIMEOUT_SECONDS` instead of skipping). Failed entries are
   already marked dirty inside `_handle` (`mark_prune_dirty(..., "operator_prune_failed")`)
   and retried by the next cycle, so the old post-failure retry loop is redundant.

2. Remove `GcodeGateway.prune_all_projects` (its only caller was this method) and the
   stdout-parsing helpers `_retry_dirty_projects`, `_failed_indexed_projects`,
   `_failed_project_ids`, `_collect_project_ids` once nothing references them
   (`gcode usages` to confirm). Keep `prune_project_for_maintenance` — targeted prunes
   still shell out per project.

3. Tests in `tests/code_index/test_prune.py`: replace the fake gateway's
   `prune_all_projects` stub and the eight `test_global_prune_*` cases (L206–L422) with
   cases that drive the in-process path through the existing operator fakes
   (`test_operator_prune_*` show the pattern): completed run clears dirty projects and
   logs a `global_prune` event with `exit_status 0`; a failed project yields
   `status == "failed"`, leaves the project dirty, and logs `exit_status 1`; a held lock
   returns the skip result; `force=True` and the configured retention days reach
   `prune_project`. Keep `test_register_code_index_prune_cron_*` unchanged.

Validate with `GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_prune.py
tests/code_index/test_gcode_gateway.py -q`, `uv run ruff check src/`, `uv run mypy src/`.
After `uv run gobby restart`, the next hourly `global_prune` row in
`~/.gobby/logs/code-index-maintenance.log` is `completed` in seconds with `command`
`["in-process", "global_prune"]`.

**Acceptance:**

- 2.3.1 - `prune_all_projects` runs `_run_operator_global_prune_locked` in-process under `_global_lock`, logs a `global_prune` event from the structured outcome, and clears dirty projects on success. symbol: `CodeIndexPruner.prune_all_projects`.
- 2.3.2 - `GcodeGateway.prune_all_projects` and the stdout-parsing helpers are gone. file: `src/gobby/code_index/gcode_gateway.py`.
- 2.3.3 - In-process global prune outcomes are pinned. test: `tests/code_index/test_prune.py::test_global_prune_runs_in_process_and_clears_dirty_projects`.
- 2.3.4 - A failed project leaves the project dirty and logs `exit_status 1`. test: `tests/code_index/test_prune.py::test_global_prune_failure_leaves_project_dirty`.

## V2 Rollout and Live Verification
`kind: verification`

Order: 1.1 first (build + new-inode install; no restart), then 2.1–2.3 together with a
single `uv run gobby restart`. Save one durable memory at execution time: FalkorDB write
queries hold the Redis GIL for their whole duration, and per-query `timeout` bounds
writes only when `TIMEOUT_DEFAULT`/`TIMEOUT_MAX` is configured.

Live checks, all read-only via the container's env (see Constraints):

1. `GRAPH.QUERY gobby_code "CALL db.indexes() YIELD label, properties, status RETURN label, properties, status"` → `CodeSymbol [project, id, file_path]`, `CodeFile [project, path]`, `CodeModule [project, name]`, `UnresolvedCallee`/`ExternalSymbol [project, id]`, all `OPERATIONAL`.
2. `GRAPH.EXPLAIN gobby_code` of the calls batch (dummy params) and of each rewritten sweep shows no `project`-only scan (this triggers the relationship-index contingency in 1.1 if it does). `GRAPH.PROFILE gobby_code "MATCH (s:CodeSymbol {id: '<real id>', project: 'd45545c5-ded5-4335-b115-0245752edacf'}) RETURN count(s)"` → the index scan produces 1 record in under a millisecond.
3. `~/.gobby/bin/gcode graph sync-file --file tests/communications/test_communications_manager.py --project /Users/josh/Projects/gobby --format json` completes in well under 5 s (was >30 s × 3); `GRAPH.SLOWLOG gobby_code` gains no entry above 1,000 ms; `~/.gobby/logs/daemon.log` shows no new `os error 35` / `graph sync retries exhausted`.
4. `GRAPH.CONFIG GET TIMEOUT_DEFAULT` → 30000 and `CONFIG GET save` → `3600 1 300 100`. `GRAPH.QUERY gobby_code "UNWIND range(1, 5000000) AS i CREATE (:__TimeoutProbe {i: i})" timeout 100` returns `Query timed out` and `MATCH (n:__TimeoutProbe) RETURN count(n)` is unchanged afterwards (rollback); clean up with `MATCH (n:__TimeoutProbe) DELETE n`.
5. One memory write through the daemon (`create_memory` on gobby-memory) succeeds; the unit tests pin `FalkorDB read timed out` wording for socket timeouts.
6. Next hourly `global_prune` maintenance-log row is `completed` in seconds; no further 120 s `timed_out` rows.
7. Next nightly 02:00 window: no `FalkorDB unreachable` warnings in `daemon.log`; `nightly_full_reindex` duration for gobby drops well below the current ~3,610 s.
8. Persistence churn: `docker logs services-falkordb-1 --since 24h` shows `Background saving started` entries at least 300 s apart during the nightly reindex (previously every 60 s) and no `Can't fork for module` lines; `docker stats --no-stream services-falkordb-1` block-write growth over a day is a small fraction of the prior ~33 GB/day.

## V1 Plan Changelog
`kind: verification`

No enhancement or adversarial review rounds recorded (Lightweight draft).

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
