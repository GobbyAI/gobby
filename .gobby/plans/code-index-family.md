Plan artifact: `.gobby/plans/code-index-family.md`

# Gobby 1.0 Stage 2: code-index family (node code-index routes, then the code-index port)

**Plan ID:** code-index-family

## Overview
`kind: framing`

Epic #21586 (Migrate code-index orchestration and shared dependencies) is the plan
root. It sits under Stage 2 (#21544). The family moves the Python code-index services
(`src/gobby/code_index/` and the `/api/code-index` routes) into a statically linked
family crate, `crates/gcodeindex` (package `gobby-codeindex`). It also lets `gcode` on
a node use the hub's index.

P1 is anchored on #23276 (gcode index writes from a node need hub HTTP routes). It
delivers what #24380 (Node code-index preflight, node-halves 2.11) needs. That
preflight runs on a node whose `GOBBY_DAEMON_URL` is the node front door and which
has no managed credential. It runs these commands in order, and any failure becomes
a prompt warning (`src/gobby/agents/code_index.py::ensure_isolation_code_index`):

1. `gcode status --quiet --format json --allow-stale --project <ws>` (5 s);
2. `gcode index --quiet --project <ws>` (the rest of 120 s);
3. `gcode search-content __gobby_code_index_smoke__ --limit 1 --quiet --allow-stale --project <ws>` (10 s).

None of them works on a node today. ROADMAP decision 17 keeps every datastore on the
hub. The hub nevertheless issues an interactive principal on a node a
`PostgresDirect` grant whose DSN the node cannot reach.

When P1 closes:

- The hub issues an interactive principal whose machine is not the hub's a
  `Brokered` Postgres capability. The capability names one operation,
  `node_code_index`, and Falkor and Qdrant are `Brokered` too.
- `gcode` under that grant runs `status`, `search-content` and `index` through
  `/api/code-index/node/*`.
- gdaemon serves those routes natively. It verifies the grant and binds it to the
  calling machine, then runs gcode's own library against the hub database as that
  machine.
- Every other database-backed `gcode` command exits with the typed error
  `brokered_unsupported`.

This revision carries P1. Under Orchestrator #15977's 2026-10-17 16:47 ruling, Plan
Adversary 16993 reviews P1 while the writer drafts the remaining phases in this file,
in the order #15977 accepted on 2026-10-17:

- P2: node read parity for the remaining database-backed commands (D7);
- P3: content-hash-keyed graph and vector projections (D6);
- P4: node-local embedding;
- P5 to P7: the `/api/code-index` family seam, graph read routes and mutation routes;
- P8: heartbeat work (sync worker, trigger, maintenance, summarizer);
- P9: the native default and the deletion of `src/gobby/code_index/`.

Josh approves the complete plan before expansion.

## Constraints
`kind: framing`

- **Stage gate.** #21586 has no #21568 gate. P1's chain to #24380 is: #24446
  (gobby-runtime crate), then #24447 (Grant signing and presentation in gcore), then
  1.9. #24448 (Secret-store port) and #24449 (Native interactive handshake) are not on
  it. Only 1.10 waits on #24449.
- **No backward compatibility.** 0.5.0 has not shipped.
- **No Python counterpart for the node routes.** Python has no
  `/api/code-index/node/*`. The `code_index_node` family defaults to `native`.
  - `front_door.routes.code_index_node: proxy` sends the routes to Python, which
    answers 404. That switches node code-index off.
  - `compare` does the same. For a POST, the front door's existing compare path
    serves the proxied response (`crates/gdaemon/src/front_door/routes.rs::compare`).
  - No leaf adds code for either setting.
- **Concurrent editors.** These open leaves of other plans edit P1 files without
  changing a seam P1 builds on. P1 takes no dependency edge on them. Each executor
  re-resolves its Targets against the tree it starts from.
  - workspace-index-pin:
    - #23833 (Scoped PostgreSQL and BM25 reads): `commands/search.rs`, `db/queries.rs`, `visibility.rs`;
    - #23834 (Vector and graph pin scope): `commands/graph/lifecycle.rs`, `commands/vector.rs`, `db/queries.rs`;
    - #23835 (Pin-aware retention): `index/indexer/lifecycle.rs`;
    - #23836 (Candidate-only reconciliation): `index/indexer/overlay.rs`, `freshness.rs`, `local_imports.rs`.
  - code-index-vector-reconciliation:
    - #23579 and #23580 (`gcode vector reconcile`): `contract.rs`, `dispatch.rs`, `commands/vector.rs`, both contract JSON copies;
    - #23582 (Current-config eligibility): `index/indexer/pipeline.rs`, `index/indexer/overlay.rs`.
  - gdaemon-key-cutover 1.2 and 1.3, and gdaemon-node-channel 1.6:
    `src/gobby/runtime_grants/handshake.py`.

  A leaf that lands after P1's plan/apply split (1.4, 1.5) applies its change to
  the split functions.
- **Seams P1 waits on.** These are the dependency edges P1 does take:
  - 1.1 depends on #23831 (Pin schema, marker, and clean Git blob selectors). It is in
    flight and edits the selector writers in `pipeline.rs`, `overlay.rs`, `file.rs` and
    `file_state.rs`. It also adds the per-path Git blob ids that 1.4's apply request
    carries.
  - 1.4 depends on #23577 (Re-parse projection reset). It adds `CodeFactSink` methods
    and edits the derives on the relation structs that 1.4 serializes.
  - 1.6 depends on #23831, which rewrites every golden vector's schema identity.
- **Release ownership.** Each leaf bumps by 0.0.1, from the executor's starting tip,
  every shipped binary whose crate or recursive path dependencies it changes. It
  updates `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS` and
  `Cargo.lock` with the bump
  (`tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`).
  - Before 1.9, a change confined to `crates/gcode` bumps `gcode` only.
  - 1.9 adds `crates/gcodeindex` to the root `members` and links `gobby-code` into
    gdaemon, so it bumps `gdaemon`, `gcode` and `ghook`. From 1.9 on, a gcode library
    change also bumps `gdaemon`. The root `Cargo.toml` is in every binary's source
    identity (`src/gobby/install/bin_release_identity.py`), so `gclient` and `gterm`
    change identity too. They are promoted separately, no leaf here releases them,
    and the promotion guard requires their bump at their next release.
  - A change confined to gruntime or gcodeindex bumps `gdaemon` only.
  - Python-only leaves and leaves that change only paths with a `tests` component
    bump nothing (`src/gobby/install/bin_release_identity.py::workspace_source_identity`).
  - `src/gobby/code_index/gcode_gateway.py` derives `MIN_GCODE_GRAPH_VERSION` from the
    `gcode` pin, so the gcode maintenance floor follows each bump with no edit.
- **Running daemon.** A crate change goes live only after `uv run gobby cutover`,
  which gets a `global` announcement and a quiet window first. A node runs the new
  `gcode` only after its own binaries are reinstalled.
- **Heavy work.** Every granted cargo run sets `CARGO_BUILD_JOBS=3` and scopes with
  `-p` (`gobby-code`, `gobby-codeindex`, `gobby-daemon`, `gobby-runtime`). gcode
  DB tests need
  `GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test`
  at build time.
- **Dependencies.** No new external crate enters `Cargo.lock`. gcodeindex uses
  `axum` 0.8 (`default-features = false`), `tokio`, `serde`, `serde_json` and
  `postgres` 0.19 at the versions already locked.
- **Monolith ceiling.** Every targeted production file stays under 1,000 lines,
  counting inline `#[cfg(test)]` modules. New Rust tests go in `tests/` files or in
  `<module>/tests.rs`.

## Decision Record
`kind: framing`

Orchestrator #15977 ruled these on 2026-10-17. Entries marked "operator" are Josh's
own answers.

- **D1, node reads.** `status` and `search-content` run on the hub over typed routes.
  The hub runs gcode's library as the principal's machine, and the principal comes
  from the verified grant. Rejected:
  - SQL over HTTP, which decision 17 rules out;
  - one generic "run any command" route, whose request shape would be the whole CLI.
- **D2, node writes.** The node parses and the hub writes, in two calls:
  - `POST /api/code-index/node/index/plan` takes paths and content hashes and returns
    the paths to parse.
  - `POST /api/code-index/node/index/apply` takes parsed facts in batches and a final
    batch with the present set.

  The hub holds the project advisory lock for each apply request only. It re-derives
  adoption under that lock and keeps the `project_checkouts` fence. It returns a
  reparse list, which the node answers once. Rejected:
  - uploading file bytes for the hub to parse, which needs the node's tree for import
    resolution;
  - a per-fact HTTP sink, which costs a round trip per file and holds no lock.
- **D3, grant shape.** An interactive principal whose machine differs from the hub's
  gets `PostgresCapability::Brokered` with one operation and no DSN, plus Falkor and
  Qdrant `Brokered`. The operation is `node_code_index` (`POST`,
  `/api/code-index/node`). It authorizes the whole node family, and the route paths
  come from the wire contract in `gobby_code::node`. Python issuance changes now (1.6). The shape becomes a
  shared golden vector in the same leaf. The native handshake reproduces it after
  #24449 (1.10). Binding condition (#15977, 16:24): nodes stay fully functional with
  no node update while the hub's Python is ported (1.6.4, 1.10.2).
  - Rejected: one operation per route. P2 and P4 add routes, so every addition would
    change the grant, its golden and the Python and Rust issuers. Until 1.10 lands
    the Rust list would have no fixed owner, and whichever of 1.10 and a route
    addition landed second would have to edit the other's code. With one operation
    the grant never changes after P1.
  - Node agents reach this branch too. A node spawn carries no managed bootstrap, so
    `crates/gcore/src/grant/acquisition.rs::acquire_with_reachability` picks the
    interactive handshake, and the hub issues through `issue_for_operator` for the
    node's machine.
- **D4, route home (operator, button 3e38baa5, native-narrow).** The node routes are
  Rust, in a native-default family at `/api/code-index/node`. They verify
  `X-Gobby-Runtime-Grant` with #24447's `gcore::grant::presentation`. Rejected:
  - Python routes first, which would add a Python surface the port then deletes;
  - waiting for #24448 and #24449, which verification does not need.
- **D5, crate.** `crates/gcodeindex`, package `gobby-codeindex`, linked into gdaemon.
  It depends on `gobby-code` (lib), `gobby-runtime` (for the grant fence) and
  `gobby-core`.
  - gcode's library is synchronous `postgres` 0.19 throughout. The family therefore
    runs each request on a blocking thread with its own connection from the hub's
    bootstrap DSN, instead of the async pool `crates/AGENTS.md` names for family row
    types.
  - Rejected: routes inside gdaemon itself, which would make gdaemon own a family.
- **D6, projection.** P1 leaves node machines out of graph and vector projection. P3
  keys projections by content hash. Nodes embed their own versions' vectors (P4), and
  the native sync worker projects node machines' graph facts (P8).
- **D7, other commands.** Node read parity comes in P2. Until then, every other
  database-backed command exits under a Brokered grant with the typed error
  `brokered_unsupported`.
- **D8, structure (operator).** P1 is anchored as `## P1: Node code-index routes (#23276)`.
  #15977 removed the #21586→#23276 and #21586→#24380 edges.
- **D9, missing parent.** Only commands that read through the overlay need the
  parent index. `status` reports an overlay whose parent is unindexed as unindexed.
  `gcode index` on such an overlay indexes the parent first, in Primary mode, using
  content-hash adoption. node-halves needs no edit.
- **Route verification (accepted 16:31).**
  - The signing secret and fencing epoch come from `deployment_runtime` through
    gruntime's `fence`, so #24448 is not needed.
  - `verify_static` authenticates the grant.
  - A Brokered grant carries no `credential_generation`, so no revocation lookup runs.
    This mirrors `src/gobby/runner_init/servers.py`'s `_principal_revoked`.
  - The grant TTL is at most 3600 s.
  - The route requires `x-gobby-machine-id` (set by
    `crates/gdaemon/src/front_door/auth.rs::AuthState::authenticate`) to equal the
    principal's machine. Otherwise it returns 403 `claims_mismatch`.
  - A revoked node API key fails front-door authentication on every request.
- **Project binding.** Each route derives its project ids from the grant principal:
  - the primary or parent project is `principal.project_id`;
  - the overlay project is `principal.code_overlay_project_id`.

  The request carries only roots. A request with a parent root but no overlay claim,
  or the reverse, returns 403 `claims_mismatch`.

## P1: Node code-index routes (#23276)
`kind: framing`

P1 makes the three preflight commands work on a node:

- 1.1 to 1.5 make gcode's library runnable for another machine, and split `gcode index`
  into a node half and a hub half.
- 1.6 issues the grant.
- 1.7 and 1.8 teach `gcode` to use it.
- 1.9 serves the routes.
- 1.10 keeps the grant shape when the handshake goes native.
- 1.11 proves the preflight end to end.

Wire names. The grant names one operation, `node_code_index` (`POST`,
`/api/code-index/node`), shared by 1.6, 1.7, 1.9 and 1.10 (D3). The route paths are
the wire contract in `gobby_code::node`, shared by 1.3, 1.4, 1.8 and 1.9. Every route
is `POST` with a JSON body:

- status: `/api/code-index/node/status`;
- search-content: `/api/code-index/node/search-content`;
- index plan: `/api/code-index/node/index/plan`;
- index apply: `/api/code-index/node/index/apply`.

### 1.1 Machine-explicit index, status and content-search paths [category: refactor] (depends: #23831)
`kind: deliverable`

Targets:
- `crates/gcode/src/config/context.rs::*` — scope-reason: `Context` gains `machine_id`, which every constructor and literal in the file sets, and `validate_parent_code_index` takes the machine
- `crates/gcode/src/index/indexer/types.rs::IndexTarget`
- `crates/gcode/src/index/indexer/sink.rs::*` — scope-reason: `PostgresCodeFactSink::new` takes the machine from the target
- `crates/gcode/src/index/indexer/file.rs::index_file`
- `crates/gcode/src/index/indexer/file.rs::index_content_only`
- `crates/gcode/src/index/indexer/pipeline.rs::index_discovered_files`
- `crates/gcode/src/index/indexer/pipeline.rs::index_explicit_files_with_connection`
- `crates/gcode/src/index/indexer/overlay.rs::index_overlay_files`
- `crates/gcode/src/index/indexer/lifecycle.rs::refresh_communities`
- `crates/gcode/src/index/indexer/local_imports.rs::*` — scope-reason: each resolver passes the target's machine to the callee and inheritance readers it calls
- `crates/gcode/src/db/queries.rs::list_indexed_file_paths`
- `crates/gcode/src/db/queries.rs::read_active_imports`
- `crates/gcode/src/db/queries.rs::read_local_import_inheritance`
- `crates/gcode/src/db/queries.rs::read_project_local_import_inheritance`
- `crates/gcode/src/db/local_callee.rs::resolve_local_callee_symbol_id`
- `crates/gcode/src/db/local_callee.rs::resolve_local_type_member_symbol_id`
- `crates/gcode/src/communities.rs::refresh_with_partition_builder`
- `crates/gcode/src/communities/identity.rs::import_rows`
- `crates/gcode/src/commands/status/current.rs::run`
- `crates/gcode/src/commands/status/current.rs::overlay_status_json`
- `crates/gcode/src/visibility/catalog.rs::tombstone_count`
- `crates/gcode/src/visibility.rs::indexed_file_exists`
- `crates/gcode/src/search/fts/content.rs::visible_files_sql`
- `crates/gcode/src/commands/graph/lifecycle.rs::rebuild_project_graph`
- `crates/gcode/src/commands/vector.rs::rebuild`
- `crates/gcode/src/freshness.rs::project_needs_refresh`
- `crates/gcode/src/vector/code_symbols/search.rs::*` — scope-reason: `Context` literals gain `machine_id`
- `crates/gcode/src/search/graph_boost.rs::*` — scope-reason: `Context` literals gain `machine_id`
- `crates/gcode/src/commands/status/invalidate.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/init.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/index.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/index/indexer/tests/serial_db.rs::*` — scope-reason: `Context` literals gain `machine_id`; adds the foreign-machine index and status tests
- `crates/gcode/src/search/fts/tests.rs::*` — scope-reason: `Context` literals gain `machine_id`; adds the foreign-machine content-search test
- `crates/gcode/src/communities/refresh_tests.rs::*` — scope-reason: `Context` literals gain `machine_id`
- `crates/gcode/src/commands/grep/db_tests.rs::*` — scope-reason: `Context` literals gain `machine_id`
- `crates/gcode/src/visibility/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/vector/code_symbols/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/projection/sync/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/index_lock/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/index/indexer/tests/api_contract.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/index/indexer/stale_cleanup_tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/graph/report/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/graph/code_graph/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/dispatch/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/status/retire_files/tests/serial_db.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/status/prune/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/search_unit_tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/search_regression_tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/graph/view/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/graph/view/communities/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/commands/graph/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/src/codewiki_facts/tests.rs::*` — scope-reason: `Context` literal gains `machine_id`
- `crates/gcode/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): this leaf changes `crates/gcode` only, so it bumps
`gcode` by 0.0.1.

The hub routes run gcode's library for a machine other than the hub's own. Today
every function on the three node paths reads the machine from the process with
`gobby_core::machine::read_local_machine_id()`. This leaf moves that read to the
context or the target. Local behavior does not change.

- `Context` gains `machine_id: String`. `Context::resolve_from_root` and every other
  constructor in `context.rs` set it from `read_local_machine_id()`. Every `Context`
  literal sets it: the test literals use the local machine id unless the test needs
  another.
- `IndexTarget` gains `machine_id: &'a str`, taken from `ctx.machine_id` where the
  target is built. `PostgresCodeFactSink::new` takes the machine from the target
  instead of reading it.
- These read the machine from their `Context` or `IndexTarget`:
  - the indexers: `index_discovered_files`, `index_explicit_files_with_connection`
    and `index_overlay_files`, with `refresh_communities`;
  - the community refresh: `communities.rs::refresh_with_partition_builder`, with
    `communities/identity.rs::import_rows`;
  - the status report: `current.rs::run` and `overlay_status_json`, with
    `catalog.rs::tombstone_count`;
  - content search: `content.rs::visible_files_sql`;
  - the indexed-file check on search results: `visibility.rs::indexed_file_exists`.
- These take a `machine_id: &str` parameter, and every caller passes its context's or
  target's machine:
  - the readers: `list_indexed_file_paths`, `read_active_imports`,
    `read_local_import_inheritance` and `read_project_local_import_inheritance`;
  - the resolvers: `resolve_local_callee_symbol_id` and
    `resolve_local_type_member_symbol_id`;
  - `validate_parent_code_index`.

  Three callers outside the node paths pass `ctx.machine_id` too:
  `rebuild_project_graph`, `vector.rs::rebuild` and `project_needs_refresh`.
- Functions off the three paths keep their local-machine reads. P2 moves them.

Research context:
- Each call site listed above was found with
  `gcode grep -n -w read_local_machine_id crates/gcode/src`, and
  `gcode grep -n -E "local_machine_uuid(_or_invisible)?\(" crates/gcode/src`.
  Each site was then traced from `pipeline.rs::index_files`,
  `commands/status/current.rs::run` and `commands/search.rs::search_content`.
- `search_content` filters results through
  `scope::current_indexed_path_is_valid`, which calls
  `visibility::indexed_file_exists` (local machine read at `visibility.rs:143`) and
  `scope::path_exists_in_current_project` (a disk check, split off in 1.3).
  `scope::other_project_for_path` is called only by `commands/symbols.rs`, which is
  off the three paths.
- `refresh_communities` is DB-only. It calls
  `communities::refresh_project_communities`, which makes no network call.
- `get_stale_files`, `get_orphan_files`, `refresh_project_stats`, `upsert_project_seed`
  and the `api::file_state` writers already take a machine id.
- `Context` literals: `rg -l runtime_config_capture_degraded crates/gcode` lists the
  31 files in Targets. `context.rs` has nine literals, the rest one to five each.
- Rejected alternatives:
  - a thread-local machine override, which a rayon worker or a later refactor could
    silently bypass;
  - a `machine_id` parameter on every read function instead of a `Context` field. P3
    needs the machine on every visibility read, so the field avoids a second sweep.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(serial_db) | test(fts)'`;
  - `cargo clippy -p gobby-code`.

**Granularity:** one outcome, three paths. The machine source moves from the process
to the context or target. That change lands in one commit: splitting it by path
would put several leaves on `Context`, its 31 literal files and the same signatures.
The test-only Targets carry only the new field.

Consumers unchanged:
- `crates/gcode/src/index/api/file_state.rs` — no-edit-reason: its writers already take `machine_id`.
- `crates/gcode/src/commands/symbols.rs` — no-edit-reason: passes its `ctx` to `indexed_file_exists`, which now reads that context's machine.
- `crates/gcode/src/commands/grep.rs` — no-edit-reason: passes its `ctx` to `current_indexed_path_is_valid`, whose `indexed_file_exists` call now reads that context's machine.- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped value flows through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.1.1 - Indexing a fixture project with a `Context` whose `machine_id` is a fresh UUID writes `code_indexed_project_states` and `code_indexed_file_states` rows only under that UUID, and none under the local machine. test: `crates/gcode/src/index/indexer/tests/serial_db.rs::foreign_machine_index_writes_only_that_machine`.
- 1.1.2 - `current::run` for a foreign-machine context reports that machine's project state, and reports the project unindexed when only the local machine has indexed it. test: `crates/gcode/src/index/indexer/tests/serial_db.rs::status_reads_the_context_machine`.
- 1.1.3 - Content search under a foreign-machine context returns chunks visible to that machine's selectors and none visible only to the local machine, and `indexed_file_exists` answers for that machine. test: `crates/gcode/src/search/fts/tests.rs::content_search_reads_the_context_machine`.
- 1.1.4 - `gcode` is one patch version above the executor's starting tip in `crates/gcode/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.2 Parent-optional overlay status and index [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/config/context.rs::*` — scope-reason: `ServiceConfigSelection` gains `require_parent_index`, which `Context::resolve_from_root` reads
- `crates/gcode/src/dispatch.rs::service_config_selection`
- `crates/gcode/src/commands/status/current.rs::run`
- `crates/gcode/src/commands/index.rs::*` — scope-reason: `resolve_index_context` stops validating the parent
- `crates/gcode/src/index/indexer/overlay.rs::index_overlay_files`
- `crates/gcode/src/index/indexer/tests/overlay.rs::*` — scope-reason: adds the parent-first overlay index test
- `crates/gcode/src/index/indexer/tests/serial_db.rs::*` — scope-reason: adds the missing-parent status test
- `crates/gcode/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` by 0.0.1.

D9 makes the parent index a requirement only of commands that read through the
overlay.

- `ServiceConfigSelection` gains `require_parent_index: bool`. It is true in `all()`,
  `database_only()` and `default()`. `dispatch.rs::service_config_selection` sets it
  false for `status` and `index`.
- `Context::resolve_from_root` calls `validate_parent_code_index` only when the
  selection requires the parent index.
- `current::run` checks the parent with `validate_parent_code_index` for the context's
  machine. When it is missing, the overlay reports exactly what an unindexed project
  reports: the same JSON and text as when the overlay itself has no project state.
  The preflight treats that as "index next".
- `index_overlay_files` replaces its `validate_parent_code_index` call with a
  parent-first index. When the parent has no file states for the context's machine,
  it does the following before the overlay pass:
  1. builds a Primary `IndexTarget` for `parent_project_id` and `parent_root`;
  2. runs `index_discovered_files` over the parent root, with `full` false, so every
     unchanged file whose facts are fully projected is adopted rather than parsed;
  3. continues with the overlay.

  A missing `project_checkouts` row for the parent root fails with the existing typed
  `checkout_required`.
- `commands/index.rs::resolve_index_context` stops validating the parent, because
  `index_overlay_files` now owns it.

Research context:
- `validate_parent_code_index` is called from `context.rs:397`,
  `commands/index.rs:439` and `overlay.rs:102`. It checks for any
  `code_indexed_file_states` row of the parent project under the machine.
- Adoption in Primary mode (`api::file_state::adopt_file_state`) requires the
  `project_checkouts` row and adopts only `graph_synced AND vectors_synced` facts.
- On a node, node-halves 2.9 registers the main checkout on its first hook, and spawns
  reach the node only after that registration. Worktrees and clones are never
  registered, which matches Overlay mode's missing fence.
- The overlay's `.gobby/isolation.json` names `parent_project_path` and
  `parent_project_id` (`config/context.rs::resolve_project_identity`).
- Planned checks: `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(overlay) | test(serial_db)'`.

Consumers unchanged:
- `src/gobby/agents/code_index.py` — no-edit-reason: the preflight already runs `index` after `status` reports unindexed, and its commands and timeouts stay the same.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.2.1 - `gcode index` on an overlay whose parent has no file states indexes the parent in Primary mode first, adopting fully projected unchanged files, then indexes the overlay. test: `crates/gcode/src/index/indexer/tests/overlay.rs::overlay_index_indexes_a_missing_parent_first`.
- 1.2.2 - `status` on an overlay whose parent is unindexed returns the unindexed report with exit 0, and `search-content` on it still fails with the parent-missing error. test: `crates/gcode/src/index/indexer/tests/serial_db.rs::missing_parent_reads_as_unindexed_for_status_only`.
- 1.2.3 - `gcode` is one patch version above the executor's starting tip in `crates/gcode/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.3 Node wire contract and hub read entry points [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/lib.rs::*` — scope-reason: declares `pub mod node`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/tests.rs`
- `crates/gcode/src/config/context.rs::*` — scope-reason: adds the `Context::for_machine` constructor and the `ProjectView` trait, implemented for `Context`
- `crates/gcode/src/commands/status/current.rs::run`
- `crates/gcode/src/commands/search.rs::search_content`
- `crates/gcode/src/commands/search/content_page.rs`
- `crates/gcode/src/commands/search/output.rs::print_search_warning`
- `crates/gcode/src/commands/search/output.rs::print_empty_diagnostic`
- `crates/gcode/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` by 0.0.1.

`gobby_code::node` (`crates/gcode/src/node/mod.rs`) is the wire contract both halves compile against:

- Constants for the operation name `node_code_index`, the family prefix
  `/api/code-index/node` and the four route paths (P1 framing).
- `NodeScope { root: String, parent_root: Option<String> }`. `parent_root` is present
  exactly when the node's project is an overlay.
- `StatusRequest { scope: NodeScope }` and `StatusReport`. The status JSON object and
  the overlay text both render from `StatusReport`.
- `SearchContentRequest { scope, query, language: Option<String>, paths: Vec<String>, limit, offset }`
  and `ContentCandidates { results, db_total: Option<usize>, hint }`, the hub half of
  a content search. `paths` arrive resolved to project-root-relative filters.
- `RouteError { code, error, message }`, the body every node route error uses.

The read commands are split so both halves print the same bytes:

- `current::run` becomes `current::collect(conn, ctx) -> StatusReport` plus a render
  step for `Format::Json` and `Format::Text`. `run` calls both. Status reads no file
  on disk.
- `search::search_content` splits at its disk check, because the hub cannot see a
  node's files:
  - `content_candidates(conn, ctx, query, options) -> ContentCandidates` runs the FTS
    fetch, the language and path-pattern filters and `visibility::indexed_file_exists`,
    and counts the database total when no path filter is set. It reads no file on
    disk.
  - `page_content(candidates, offset, limit, exists: impl Fn(&str) -> bool) -> SearchContentPage`
    drops each result whose file `exists` rejects, takes the total from `db_total` or
    else the filtered count, and pages.
  - `search_content` runs both with `scope::path_exists_in_current_project` as
    `exists`, then prints. This is the order `search_content` filters and pages in
    today.

  The query and paging body move out of `crates/gcode/src/commands/search.rs` into
  `content_candidates` and `page_content` in the new
  `crates/gcode/src/commands/search/content_page.rs`, so `commands/search.rs`
  (892 lines) shrinks instead of growing.
- `ProjectView` (`config/context.rs`) exposes `project_root()`, `project_id()`,
  `index_scope()` and `quiet()`. `Context` implements it, and 1.7's `NodeAccess` does
  too. The page render and `output.rs`'s `print_search_warning` and
  `print_empty_diagnostic` take `&impl ProjectView`, so their `&Context` callers keep
  compiling and the node renders with the same code.

`node/hub.rs` holds what the hub runs:

- `HubScope { database_url, machine_id, project_id, overlay_project_id: Option<String> }`
  comes from the verified principal.
- `HubScope::context(&self, &NodeScope) -> Result<Context, RouteError>` checks that
  `parent_root.is_some() == overlay_project_id.is_some()`, else `claims_mismatch`. It
  then builds a `Context` through the new constructor
  `Context::for_machine(database_url, machine_id, project_id, root, index_scope)`:
  - `index_scope` is `Single`, or `Overlay` with the principal's two project ids and
    the request's two roots;
  - services are off, matching a grant with no Falkor, Qdrant or embedding capability;
  - `quiet` is true.
- `status(&HubScope, StatusRequest) -> Result<StatusReport, RouteError>` and
  `search_content(&HubScope, SearchContentRequest) -> Result<ContentCandidates, RouteError>`
  open a read-only connection with `db::connect_readonly`, then call `collect` or
  `content_candidates`. A missing parent on search returns `RouteError` code
  `parent_index_missing`. Neither reads the node's root on the hub's disk.

Research context:
- `current::run` (131-line file) reads `code_indexed_project_states`,
  `overlay_status_json` and `bm25_health::verify`, then prints JSON or text.
- `search_content` (`commands/search.rs:560`) runs `fts::search_content_visible`,
  filters with `scope::current_indexed_path_is_valid`, pages and prints. That filter
  is `visibility::indexed_file_exists` and `scope::path_exists_in_current_project`,
  which checks the file under `ctx.project_root` (and the overlay's two roots) on
  local disk. Run on the hub for a node root, it would drop every result.
- The render inputs: `output.rs::print_empty_diagnostic` reads `quiet` and
  `project_root` (`has_identity_file`), `print_search_warning` reads `quiet`, and
  `SearchPageMeta` reads `project_id`. `token_budget` and `verbose` only shape the
  rendered output.
- The principal's `project_id` is the `.gobby/project.json` id.
  `code_overlay_project_id` is `gobby_core::project::code_overlay_project_id(root)`
  (`crates/gcore/src/grant/acquisition.rs`). Python's
  `src/gobby/runtime_grants/handshake.py::_normalized_overlay_claim` keeps the two
  distinct.
- Planned checks: `cargo nextest run -p gobby-code -E 'test(node)'`.

Consumers unchanged:
- `crates/gcode/src/dispatch_navigation.rs` — no-edit-reason: calls `search_content` with the same arguments, and it still prints.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.3.1 - `status` JSON and text output is byte-identical before and after the collect/render split. test: `crates/gcode/src/node/tests.rs::status_render_matches_the_cli_output`.
- 1.3.2 - `node::hub::status` and `node::hub::search_content` for a foreign machine whose roots do not exist on the hub's disk return that machine's report and candidates, and a scope whose overlay shape disagrees with the principal returns `claims_mismatch`. test: `crates/gcode/src/node/tests.rs::hub_reads_run_as_the_principal_machine`.
- 1.3.3 - Local `search-content` output is byte-identical before and after the candidates and page split, including a result whose file was deleted on disk after indexing. test: `crates/gcode/src/node/tests.rs::content_search_split_matches_the_cli_output`.
- 1.3.4 - Every wire type round-trips through serde_json unchanged. test: `crates/gcode/src/node/tests.rs::wire_types_round_trip`.
- 1.3.5 - `gcode` is one patch version above the executor's starting tip in `crates/gcode/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.4 Primary-mode index plan and apply [category: code] (depends: 1.3, #23577)
`kind: deliverable`

Targets:
- `crates/gcode/src/models.rs::ImportRelation`
- `crates/gcode/src/models.rs::CallRelation`
- `crates/gcode/src/models.rs::CallTargetKind`
- `crates/gcode/src/models.rs::InheritanceRelation`
- `crates/gcode/src/models.rs::HeritageKind`
- `crates/gcode/src/index/indexer/file.rs::index_file`
- `crates/gcode/src/index/indexer/file.rs::index_content_only`
- `crates/gcode/src/index/indexer/file.rs::write_parsed_file_facts`
- `crates/gcode/src/index/indexer/file.rs::write_content_only_file_facts`
- `crates/gcode/src/index/indexer/pipeline.rs::index_discovered_files`
- `crates/gcode/src/index/indexer/pipeline.rs::index_explicit_files_with_connection`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/tests.rs`
- `crates/gcode/src/node/index_tests.rs`
- `crates/gcode/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` by 0.0.1.

Split the Primary pipeline into a node half and a DB half. Local `gcode index` then
runs both halves in one process, and the hub runs the DB half for a node.

- `ParsedFileFacts` (wire, `crates/gcode/src/node/mod.rs`) holds everything the DB half writes for one
  file: path, language, content hash, byte size, the Git blob id #23831 records for
  clean tracked files, and the facts:
  - `Parsed { symbols, imports, calls, inheritance, chunks }`; or
  - `ContentOnly { chunks }`.

  `ImportRelation`, `CallRelation`, `CallTargetKind`, `InheritanceRelation` and
  `HeritageKind` gain `Serialize, Deserialize` on their derive lines.
- `file.rs` splits `index_file` and `index_content_only` into `parse_file_facts` and
  `parse_content_only_facts`, which make no DB call, and `apply_file_facts(conn, target, &ParsedFileFacts)`.
  - `apply_file_facts` opens the per-file transaction and the `PostgresCodeFactSink`.
  - It calls `write_parsed_file_facts` or `write_content_only_file_facts`, with chunks
    already built, so #23577's before/after projection reset runs on both halves.
  - `index_file` and `index_content_only` become parse then apply.
- `pipeline.rs` splits `index_discovered_files` at its DB boundary:
  - node half: discovery, `current_file_state` hashing, import context and parsing;
  - DB half: `plan_primary(conn, target, &PlanInput) -> PlanOutput` and
    `apply_primary(conn, target, &ApplyInput) -> ApplyOutput`.

  `PlanOutput` lists the stale paths that cannot be adopted (all present paths when
  `full`). `index_explicit_files_with_connection` gets the same split.
- Wire (`crates/gcode/src/node/mod.rs`):
  - `IndexPlanRequest { scope, full, path_filter, files: Vec<FileHash { path, content_hash, git_blob }> }`
    returns `IndexPlanResponse { parse: Vec<String>, parent_indexed: Option<bool> }`.
    `parent_indexed` is used by 1.5.
  - `IndexApplyRequest { scope, full, path_filter, files: Vec<ParsedFileFacts>, final_batch: Option<FinalBatch> }`
    returns `IndexApplyResponse { reparse: Vec<String>, outcome: Option<IndexOutcome> }`.
  - `FinalBatch { present: Vec<FileHash>, indexed_paths: Vec<String>, eligible_files, scanned_files, unsupported_file_types }`.
- `node::hub::plan_index(&HubScope, IndexPlanRequest)` opens a read-only connection
  and runs `plan_primary` without the lock.
- `node::hub::apply_index(&HubScope, IndexApplyRequest)` opens a read-write
  connection and runs inside
  `index_lock::with_project_lock(&ctx, IndexLockPolicy::Wait { max_wait: Duration::from_secs(60) }, …)`
  for the request's project. `IndexLockResult::Busy` becomes `RouteError` code
  `index_busy`. Under the lock it:
  1. writes every `files` entry with `apply_file_facts`, where each sink seeds the
     project and enforces the `project_checkouts` fence (`checkout_required` and
     `checkout_mismatch` pass through as route error codes);
  2. on the final batch, deletes orphans against `present` (whole-project runs only),
     re-derives adoption under the lock for every present path that is stale and not
     in `indexed_paths`, and lists each path that cannot be adopted in `reparse`;
  3. when `reparse` is empty, runs the existing post-pass: `resolve_local_import_calls`
     over `indexed_paths`, `resolve_project_local_import_calls` for full whole-project
     runs, `resolve_local_import_inheritance` over indexed plus adopted paths,
     `refresh_project_stats` and `refresh_communities`;
  4. returns the `IndexOutcome`.

  A non-empty `reparse` skips the post-pass. The node parses those paths and sends one
  more final batch (1.8).
- A request whose serialized body exceeds the route limit (16 MiB, 1.9) never reaches
  the library. The node keeps each batch at or below 8 MiB of JSON.

Research context:
- `index_discovered_files` runs these steps in order:
  1. `upsert_project_seed`;
  2. discovery, then `current_file_state`;
  3. `get_stale_files`, then the orphan delete (whole-project runs only);
  4. per file, `adopt_file_state`, else `index_file` or `index_content_only`;
  5. the local-import resolvers;
  6. `refresh_project_stats`;
  7. `refresh_communities`;
  8. `attach_projection_sync`.
- `index_file` hashes the captured parse source, opens a transaction and calls
  `write_parsed_file_facts` through `PostgresCodeFactSink::new`, which seeds the
  project and runs the checkout fence.
- `IndexOutcome` (`index/indexer/types.rs`) already derives `Serialize, Deserialize`.
- `index_lock.rs` provides `with_project_lock(ctx, policy, f)`, which returns
  `IndexLockResult::{Acquired, Busy}`, over PostgreSQL session advisory locks.
  `IndexLockPolicy::Wait { max_wait }` takes any bound. `with_exclusive_project_lock`
  hard-codes `IndexLockPolicy::wait()` (1800 s), which is too long for a request the
  node times out at 120 s.
- After #23577 lands, `write_parsed_file_facts` reads the version's facts before
  writing and dirties sync flags that changed. Keeping it as the one write path
  preserves that on the hub.
- Planned checks: `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(serial_db) | test(facts)'`.

**Granularity:** six acceptance items. The plan call, the apply call and the local
recomposition are one Primary pipeline split at one boundary. Splitting them would put
three leaves on `index_discovered_files`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.4.1 - Local `gcode index` of a fixture writes the same file states, facts and outcome counts before and after the split. test: `crates/gcode/src/node/index_tests.rs::local_index_is_unchanged_by_the_split`.
- 1.4.2 - Plan then apply for a foreign machine, with the node half run in-process, yields the same rows under that machine as a local index under it. test: `crates/gcode/src/node/index_tests.rs::plan_and_apply_match_a_local_index`.
- 1.4.3 - Plan returns only stale paths that cannot be adopted, and every path when `full` is set. test: `crates/gcode/src/node/index_tests.rs::plan_lists_only_paths_to_parse`.
- 1.4.4 - When a path's adoptable facts disappear between plan and apply, the final batch returns it in `reparse` and skips the post-pass. A second final batch that carries its facts completes. test: `crates/gcode/src/node/index_tests.rs::apply_returns_reparse_and_completes_on_retry`.
- 1.4.5 - Apply against a Primary root with no `project_checkouts` row returns `checkout_required` and writes nothing. A held project lock returns `index_busy` after the bounded wait. test: `crates/gcode/src/node/index_tests.rs::apply_keeps_the_checkout_fence_and_bounded_lock`.
- 1.4.6 - `gcode` is one patch version above the executor's starting tip in `crates/gcode/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.5 Overlay-mode index plan and apply [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `crates/gcode/src/index/indexer/overlay.rs::index_overlay_files`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/index_tests.rs`
- `crates/gcode/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` by 0.0.1.

Apply 1.4's split to `index_overlay_files`:

- The node half walks, hashes and parses the overlay root.
- `plan_overlay` and `apply_overlay` hold every step that takes `conn`: the overlay
  seed, overlay-owned selector writes, tombstones for parent files the overlay deleted,
  and the overlay's import resolution.
- `plan_overlay` returns `parent_indexed`, which 1.2's check computes for the
  principal's machine.
- When `parent_indexed` is false, the node runs a Primary plan and apply for the
  parent root before the overlay (D9). Local `index_overlay_files` composes the same
  sequence in-process (1.2).
- `node::hub::plan_index` and `apply_index` dispatch on `NodeScope.parent_root`:
  - absent: Primary;
  - present: Overlay, with project ids from the principal.

  Overlay mode has no checkout fence, matching `upsert_project_seed`.

Research context:
- `index_overlay_files` (`overlay.rs:86`) reads the local machine once (`:114`, made
  explicit by 1.1). It builds import candidates from `list_indexed_file_paths` of both
  projects (`:150`). It writes overlay selectors through
  `api::file_state::adopt_file_state` and `upsert_file_state` in
  `IndexWriteMode::Overlay`.
- `upsert_project_seed` writes Overlay project state without a `project_checkouts` row.
- Planned checks: `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(overlay)'`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.5.1 - Plan then apply of an overlay for a foreign machine yields the same overlay rows and tombstones as a local overlay index under that machine. test: `crates/gcode/src/node/index_tests.rs::overlay_plan_and_apply_match_a_local_index`.
- 1.5.2 - `plan_index` for an overlay whose parent is unindexed reports `parent_indexed: false`, and a Primary pass over the parent root followed by the overlay pass leaves both indexed. test: `crates/gcode/src/node/index_tests.rs::overlay_plan_reports_a_missing_parent`.
- 1.5.3 - `gcode` is one patch version above the executor's starting tip in `crates/gcode/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.6 Remote interactive principals get Brokered datastores [category: code] (depends: #23831)
`kind: deliverable`

Targets:
- `src/gobby/runtime_grants/service.py::*` — scope-reason: adds `NODE_CODE_INDEX_OPERATIONS` beside `FALKOR_OPERATIONS`, and `capabilities_from_snapshot` brokers Falkor and Qdrant under a Brokered Postgres
- `src/gobby/runtime_grants/handshake.py::HandshakeService._issue`
- `tests/runtime_grants/golden/remote_interactive_brokered.json`
- `tests/runtime_grants/test_remote_principal_grants.py`

Release ownership (Constraints): Python and test files only, so no binary bumps.

- `NODE_CODE_INDEX_OPERATIONS` in `service.py` is a one-element tuple,
  `BrokerOperation(name="node_code_index", method="POST", path="/api/code-index/node")`
  (D3). It sits beside `FALKOR_OPERATIONS` and `QDRANT_OPERATIONS`.
- `HandshakeService._issue` decides by machine. An `interactive` principal whose
  `machine_id` differs from `self.local_machine_id` gets
  `BrokeredCapability(operations=NODE_CODE_INDEX_OPERATIONS)`
  (`src/gobby/runtime_grants/schema.py`, an arm of the `PostgresCapability` union).
  `_issue` does not call
  `issue_postgres` for it, so no interactive credential is minted. A principal on the
  hub's machine, and every agent, maintenance and tool-chat principal, still goes
  through `issue_postgres` and gets `PostgresDirect`.
- `_issue` caps the TTL at `postgres.valid_until` only for `PostgresDirect`. A
  Brokered grant keeps `GRANT_TTL_SECONDS` (3600) or the token bound.
- `capabilities_from_snapshot` makes Falkor and Qdrant `Brokered` with
  `FALKOR_OPERATIONS` and `QDRANT_OPERATIONS` whenever `postgres` is a
  `BrokeredCapability`. A principal without direct Postgres gets no direct datastore.
  No other issuance path produces a Brokered Postgres today, so only remote
  principals change.
  `broker_operations` still lists embed, Falkor and Qdrant operations as before.
- `remote_interactive_brokered.json` is a signed grant that Python issuance produces
  for a remote interactive principal, with fixed inputs. It embeds the current schema
  identity like the other goldens, so later migrations update it in their all-ref
  sweep. Both directory readers pick it up:
  - Python: `tests/runtime_grants/test_golden_vectors.py::_golden_paths`;
  - Rust: `crates/gcore/src/grant/tests.rs::golden_vectors_match_python`.

Research context:
- `_issue` (`handshake.py:194`) calls `self.issue_postgres` for every principal, then
  runs `ttl = min(ttl, max(1, postgres.valid_until - now))` unconditionally, which
  would raise for a Brokered capability.
- The production `issue_postgres` is `servers.py::_bind_runtime_grants`'s
  `_issue_postgres`, which calls `issue_grant_postgres` (`servers.py:408`) and rejects
  anything but `PostgresDirect`. `issue_grant_postgres` mints an interactive
  credential for every interactive principal.
- Rejected alternative: deciding inside `issue_grant_postgres`. It would need the
  local machine passed in, widen its return type, the wrapper's check and the
  `issue_postgres` field, and edit the call sites in
  `tests/runner_init/test_grant_issuance.py` and
  `tests/storage/test_managed_credentials.py`. `_issue` already holds
  `local_machine_id`.
- `GrantService.issue` already caps `expires_at` only for `PostgresDirect`.
- `_capability_matches` (`service.py:215`) accepts a Brokered capability only for an
  operation in its `operations`. That is the rule 1.9 mirrors natively.
- `issue_for_operator` rejects a principal whose machine differs from the
  front-door-authenticated machine, so the remote branch cannot be claimed for another
  machine.
- `src/gobby/runtime_grants/service.py` is also targeted by gdaemon-runtime-family
  2.2 (closed with #24437). `handshake.py` is targeted by gdaemon-key-cutover 1.2 and
  1.3 and by gdaemon-node-channel 1.6 (Constraints, Concurrent editors).
- Planned checks: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/ tests/servers/routes/test_runtime_handshake.py tests/runner_init/test_grant_issuance.py -v`.

Consumers unchanged:
- `src/gobby/servers/grant_auth.py` — no-edit-reason: its route requirements name embed and AI capabilities, which this leaf leaves unchanged.
- `src/gobby/runtime_grants/revocation.py` — no-edit-reason: `_principal_revoked` already returns false for a capability without `credential_generation`.
- `src/gobby/runner_init/servers.py` — no-edit-reason: `issue_grant_postgres` and its `_issue_postgres` wrapper run only for principals that `_issue` keeps on `PostgresDirect`.
- `src/gobby/agents/code_index.py` — no-edit-reason: passes a `PostgresDirect` to `capabilities_from_snapshot`, whose result for a direct Postgres is unchanged.
- `tests/runner_init/test_grant_issuance.py` — no-edit-reason: its handshakes issue agent principals, and its direct `issue_grant_postgres` calls are unchanged.
- `tests/storage/test_managed_credentials.py` — no-edit-reason: calls `issue_grant_postgres`, whose signature and result are unchanged.
- `tests/servers/routes/test_runtime_handshake.py` — no-edit-reason: every interactive principal it issues is on `LOCAL_MACHINE_ID`, so it keeps `PostgresDirect`.

**Acceptance:**

- 1.6.1 - A handshake for an interactive principal on another machine returns Postgres `brokered` with exactly the `node_code_index` operation, and Falkor and Qdrant `brokered`, without calling `issue_postgres`. The same principal on the hub's machine still gets `direct`. test: `tests/runtime_grants/test_remote_principal_grants.py::test_remote_interactive_principal_gets_brokered_datastores`.
- 1.6.2 - A Brokered grant's expiry is the TTL bound and never reads `valid_until`. test: `tests/runtime_grants/test_remote_principal_grants.py::test_brokered_grant_ttl_ignores_valid_until`.
- 1.6.3 - The new golden round-trips and its checksum is pinned (the directory readers include it). test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.
- 1.6.4 - Binding condition: Python issuance for the golden's fixed inputs reproduces `remote_interactive_brokered.json` byte for byte, so the shape nodes depend on is pinned while the hub's Python is ported. test: `tests/runtime_grants/test_remote_principal_grants.py::test_remote_issuance_reproduces_the_golden`.

### 1.7 Brokered access resolution and the typed refusal [category: code] (depends: 1.5)
`kind: deliverable`

Targets:
- `crates/gcode/src/db/resolution.rs::postgres_dsn_from_grant`
- `crates/gcode/src/cli_error.rs::CliError`
- `crates/gcode/src/config/context.rs::*` — scope-reason: adds `NodeAccess` (which implements `ProjectView`), `resolve_access` and `Access`, sharing `resolve_from_root`'s identity and settings steps
- `crates/gcode/src/contract.rs::contract`
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: `error_codes` gains the codes P1's paths produce
- `tests/contracts/gcode.contract.json::*` — scope-reason: the test copy gains the same error codes
- `docs/contracts/gcode-cli.md`
- `crates/gcode/tests/contract.rs::*` — scope-reason: asserts the added error codes
- `crates/gcode/src/config/access_tests.rs`
- `crates/gcode/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` by 0.0.1.

- `postgres_dsn_from_grant` maps `PostgresCapability::Brokered` to the new
  `CliError::brokered_unsupported()`. Its code is `brokered_unsupported` and its
  recovery reads "this command needs direct database access; on a node only status,
  search-content and index run". Every `Context` resolution under a Brokered grant
  therefore fails with that typed error before any DSN use, which satisfies D7 at one
  point.
- `config::resolve_access(project_override, quiet, services) -> Result<Access>`
  acquires the grant once, with `Access::{Direct(Context), Node(NodeAccess)}`:
  - Direct carries the `Context` exactly as `resolve_with_services` builds it.
  - Node carries `NodeAccess`, built from the same identity steps, with no database:
    - project root, project id and index scope;
    - `quiet`;
    - `indexing` from `services_from_acquired_settings`;
    - the daemon URL and the acquired grant.

    A Brokered grant whose Postgres `operations` lacks `node_code_index` fails with
    `capability_unavailable("postgres")`. The route paths come from
    `gobby_code::node`.

  `NodeAccess` implements 1.3's `ProjectView`, so the node renders pages and resolves
  path filters with the local code.
- The contract's `error_codes` has never been exhaustive: `crates/gcode/src/cli_error.rs`
  alone raises `capability_unavailable`, `daemon_required` and `payload_skew`, and
  `daemon.rs` raises `project_request` and `prune_request`, none of them listed. The
  rule this plan follows is that every code a P1 path produces is listed:
  - `brokered_unsupported` (this leaf);
  - `node_code_index_failed` and `node_index_conflict`, which 1.8's client raises;
  - the node route codes 1.8 relays from a `RouteError` body: `claims_mismatch`,
    `wrong_capability`, `checkout_required`, `checkout_mismatch`,
    `parent_index_missing`, `index_busy` and `internal_error`.

  Codes from before this plan stay as they are. Listing 1.8's codes here keeps 1.8
  off the contract files. The codes join
  `contract().error_codes`, both contract JSON copies and
  `docs/contracts/gcode-cli.md`. `contract_version` stays 12, following #22353's
  addition of `path_not_found`.

Research context:
- `postgres_dsn_from_grant` (`db/resolution.rs:17`) maps Brokered to
  `GrantError::DaemonRequired` today. `resolve_from_root` calls it through
  `database_url_from_acquired` before opening any connection.
- `services_from_acquired_settings` reads machine settings from the grant's config
  revision without the database.
- Commit `7be50bed97` (#22353) added an error code to `contract.rs`, both contract JSON
  copies, `docs/contracts/gcode-cli.md` and `crates/gcode/tests/contract.rs` without a
  version bump.
- Planned checks: `cargo nextest run -p gobby-code -E 'test(access) | test(contract)'`.

Consumers unchanged:
- `crates/gcode/src/cli/tests.rs` — no-edit-reason: compares `contract()` command entries with clap, and this leaf adds no command.
- `crates/gcode/src/cli/tests/top_level.rs` — no-edit-reason: reads the `outline` command entry, which this leaf leaves unchanged.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.7.1 - Under a Brokered grant, `Context` resolution fails with `brokered_unsupported` and its recovery text, before any connection attempt. test: `crates/gcode/src/config/access_tests.rs::brokered_context_resolution_is_refused`.
- 1.7.2 - `resolve_access` returns `Node` for the remote golden grant, `Direct` for a direct grant, and `capability_unavailable` when the Brokered grant lacks `node_code_index`. test: `crates/gcode/src/config/access_tests.rs::resolve_access_reads_the_remote_golden`.
- 1.7.3 - The contract lists `brokered_unsupported`, `node_code_index_failed`, `node_index_conflict` and the seven node route codes, and both JSON copies match `contract()`. test: `crates/gcode/tests/contract.rs::contract_lists_the_node_facing_codes`.
- 1.7.4 - `gcode` is one patch version above the executor's starting tip in `crates/gcode/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.8 gcode node client for status, search-content and index [category: code] (depends: 1.6, 1.7)
`kind: deliverable`

Targets:
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/daemon.rs::*` — scope-reason: `DaemonRequestKind` gains `NodeCodeIndex`, and it and the transport helpers `join_daemon_url`, `read_json` and `classify_transport` become `pub(crate)`
- `crates/gcode/src/commands/scope.rs::resolve_path_input`
- `crates/gcode/src/commands/scope.rs::scope_roots`
- `crates/gcode/src/commands/scope.rs::path_exists_in_current_project`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client_tests.rs`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` by 0.0.1.

- `dispatch.rs::run` resolves `status`, `search-content` and `index` through
  `resolve_access`. `Direct` runs today's code. `Node` passes the parsed `Command` to
  `node::client`, which reads the same clap fields `dispatch_navigation.rs` reads.
  Every other command keeps its resolution and gets 1.7's refusal.
- `node::client` posts JSON with `ureq` (already a gcode dependency), the
  `X-Gobby-Runtime-Grant` header (`gobby_core::grant::GRANT_HEADER`, encoded as
  `crates/gcode/src/graph/code_graph/lifecycle.rs` does) and the command's timeout.
  It uses `daemon.rs`'s `join_daemon_url`, `read_json` and `classify_transport`.
  It does not use `status_error`, which sends every 401 and 403 through gcore's
  grant mapping.
- The client classifies a non-2xx response by the JSON body's `code` first, because
  `GrantError::from_presentation_http` (`crates/gcore/src/grant/mod.rs`) maps any 409
  to `stale_epoch`:
  - `revoked`, `stale_epoch`, `invalid_signature`, `expired` or `capability_expired`,
    or any 401: a `GrantError` through `from_presentation_http`;
  - any other code: a `CliError` with that code;
  - a body with no `code`: `node_code_index_failed`, from the new
    `DaemonRequestKind::NodeCodeIndex`. Transport failures use the same kind.
- Each request presents the client's current grant. On a retryable grant error
  (`is_retryable_presentation`: a stale epoch or a rotated signature), the client
  calls `gobby_core::grant::rehandshake(&AcquireRequest::from_process(&root)?)` once,
  keeps the new bundle for this and every later request in the run, and resends the
  same body. A second grant error on the resend is returned. This is
  `present_bundle_with_single_retry`'s rule. The client calls `rehandshake` itself
  because that helper does not return the refreshed bundle, and an `index` run sends
  many requests.
- Path filters resolve on the node. `resolve_path_input`, `scope_roots` and
  `path_exists_in_current_project` take `&impl ProjectView` instead of `&Context`, so
  their 27 `&Context` call sites keep compiling. The client resolves each
  `search-content` filter against `NodeAccess`'s project root (and an overlay's two
  roots) and sends project-root-relative paths. The hub never checks them on disk.
- The three commands:
  - `status` posts `StatusRequest` and renders `StatusReport` with 1.3's renderer.
  - `search-content` posts `SearchContentRequest`, pages the returned
    `ContentCandidates` with `page_content` and `path_exists_in_current_project` over
    `NodeAccess`, and renders the page with 1.3's renderer. `token_budget` and
    `verbose` stay on the node, because they only shape rendering.
  - `index`:
    1. For an overlay, posts the overlay plan. When `parent_indexed` is false, runs
       the Primary sequence for the parent root first.
    2. For each scope, posts the plan, parses the returned paths with 1.4's node half,
       and posts apply batches of at most 8 MiB of JSON. The last batch carries
       `FinalBatch`.
    3. When the final response lists `reparse`, parses those paths and sends one more
       final batch. A second non-empty `reparse` exits with `node_index_conflict`.
    4. Prints the returned `IndexOutcome` as local `index` does.
- `--allow-stale` and `--quiet` keep their meaning. A node has no local freshness
  check to skip.

Research context:
- `daemon.rs` already wraps `ureq` with typed transport and status errors
  (`classify_transport`, `status_error`, `join_daemon_url`, `read_json`). The enum and
  the helpers are private today (`daemon.rs:18`, `:207`, `:217`, `:239`, `:267`).
- `status_error` (`daemon.rs:239`) sends every 401 and 403 through
  `from_presentation_http`, which maps any 409 to `stale_epoch` and 403
  `claims_mismatch` to `Unauthorized`. Node routes return 409 for `index_busy` and
  the checkout and parent codes (1.9), so the client reads the body code first.
- `present_bundle_with_single_retry` (`crates/gcore/src/grant/acquisition.rs`)
  re-handshakes once on a retryable presentation error through `rehandshake`, which
  `crates/gcore/src/grant/mod.rs` re-exports and which returns the `AcquiredGrant`.
- `resolve_path_input` (`commands/scope.rs:37`), `scope_roots` and
  `path_exists_in_current_project` read only `project_root` and `index_scope` from
  the context. `dispatch_navigation.rs` resolves `search-content` filters through
  them before searching.
- The preflight's budgets are 5 s for status, the rest of 120 s for index and 10 s for
  search-content (`src/gobby/agents/code_index.py`).
- The node front door relays `/api/*` with the node's API key to the hub
  (`crates/gdaemon/src/front_door/challenge.rs::answer` answers the interactive
  challenge locally).
- Planned checks: `cargo nextest run -p gobby-code -E 'test(node) | test(scope)'`. The
  client tests run against an in-process HTTP stub that serves 1.3 to 1.5's hub entry
  points and answers the interactive handshake, so a re-handshake is observable.

Consumers unchanged:
- `crates/gcode/src/commands/symbol_at.rs` — no-edit-reason: passes `&Context` to `resolve_path_input`, which accepts any `ProjectView`.
- `crates/gcode/src/dispatch_navigation.rs` — no-edit-reason: passes `&Context` to `resolve_path_input`, which accepts any `ProjectView`.
- `crates/gcode/src/commands/symbols.rs` — no-edit-reason: passes `&Context` to `path_exists_in_current_project`, which accepts any `ProjectView`.
- `src/gobby/agents/code_index.py` — no-edit-reason: the preflight's argv and exit-code handling are unchanged.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.8.1 - Under the remote golden grant, `gcode status` prints exactly what the stubbed hub's report renders to, and `gcode search-content` with a relative path filter sends the project-root-relative path and prints the page of the returned candidates whose files exist on the node. test: `crates/gcode/src/node/client_tests.rs::node_reads_render_hub_results`.
- 1.8.2 - `gcode index` on a node overlay with an unindexed parent runs parent plan and apply, then overlay plan and apply, splitting facts into batches of at most 8 MiB. test: `crates/gcode/src/node/client_tests.rs::node_index_indexes_parent_then_overlay_in_batches`.
- 1.8.3 - A final response with `reparse` triggers exactly one retry, and a second `reparse` exits `node_index_conflict`. test: `crates/gcode/src/node/client_tests.rs::node_index_retries_reparse_once`.
- 1.8.4 - Route error bodies surface as typed errors with their codes (`checkout_required`, `index_busy`, `claims_mismatch`), and a non-2xx response whose body has no `code` surfaces as `node_code_index_failed`. test: `crates/gcode/src/node/client_tests.rs::route_errors_keep_their_codes`.
- 1.8.5 - A 409 `index_busy` surfaces as `index_busy` with no re-handshake, and a 409 `stale_epoch` triggers exactly one re-handshake and resend of the same body. test: `crates/gcode/src/node/client_tests.rs::grant_rejections_refresh_once_and_route_codes_do_not`.
- 1.8.6 - `gcode` is one patch version above the executor's starting tip in `crates/gcode/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.9 `gobby-codeindex` family serving the node routes [category: code] (depends: 1.8, #24447)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gcodeindex/Cargo.toml`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/tests/node_routes.rs`
- `crates/gruntime/src/grant.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the `cfg(unix)` `run` builds the node family and composes it after `FAMILIES`; the non-unix `run` stays a bail
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: adds the `code_index_node` default and proxy test
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): this leaf changes the root `members`, so it bumps
`gdaemon`, `gcode` and `ghook` by 0.0.1. The root `Cargo.toml` is in every binary's
source identity, so `gclient` and `gterm` change identity too. They are promoted
separately and this leaf does not release them; the promotion guard requires their
bump at their next release.

Create the crate the way gdaemon-runtime-family 3.1 created gruntime:

- `crates/gcodeindex`, package `gobby-codeindex`, lib `gobby_codeindex`,
  `publish = false`. It inherits `version`, `edition`, `rust-version`, `license` and
  `repository` from `[workspace.package]`, with `[lints] workspace = true`.
- Add it to the root `members`. gdaemon depends on it by path.
- Dependencies:
  - `gobby-code` (lib);
  - `gobby-runtime`;
  - `gobby-core` with `postgres`, `postgres-pool` and `route-family`;
  - `axum` 0.8 (`default-features = false`);
  - `tokio`, `serde`, `serde_json`;
  - `postgres` 0.19.

`lib.rs` exports `CodeIndexNodeFamily`:

- `new(pool, home, database_url)`;
- `name()` returns `"code_index_node"`, and `prefixes()` returns
  `&["/api/code-index/node"]`;
- `default_backend()` returns `Native`;
- `router<S>()` registers the four `POST` routes with
  `DefaultBodyLimit::max(16 * 1024 * 1024)`;
- `claims(parts)` holds for those four method and path pairs when the request carries
  `x-gobby-key-id` (an API-key caller), mirroring gruntime's `Caller::Operator`.

`serve::run` builds it on the shared pool next to the runtime family, leaks it once,
and composes it after `FAMILIES` for `unimplemented_families` and every listener's
`RouteTable::new`. A node-mode gdaemon builds no native families.

Composing the family outside `FAMILIES` with a `Native` default is deliberate.
`crates/gdaemon/src/front_door/routes.rs`'s
`absent_route_takes_the_family_default` asserts that every `FAMILIES` entry defaults
to `Proxy` ("Every shipped family stays proxied until bootstrap opts it in"). That
invariant protects families whose Python routes still serve. `code_index_node` has
no Python counterpart (Constraints), so a Proxy default would ship the routes
switched off.

`node.rs` runs each request in this order, mirroring
`src/gobby/runtime_grants/service.py::GrantService.present`:

1. Decode `X-Gobby-Runtime-Grant` with `gcore::grant::presentation::decode_grant_header`.
   A missing or malformed header returns 401 or 400 with Python's bodies (gruntime
   3.2).
2. Read the fencing epoch and signing secret with `gruntime::grant::fence`, which this
   leaf makes `pub`, then run `verify_static`.
3. Skip revocation. A Brokered Postgres capability carries no `credential_generation`.
4. Require principal kind `interactive`, and
   `x-gobby-machine-id == principal.machine_id`. Otherwise return 403
   `claims_mismatch`.
5. Require Postgres `Brokered` whose `operations` contains `node_code_index` with
   method `POST` and path `/api/code-index/node` exactly. Otherwise return 403
   `wrong_capability`.
6. Build `HubScope` from the principal and the hub DSN, then run the matching
   `gobby_code::node::hub` call under `tokio::task::spawn_blocking`.
7. Map a `RouteError` to its status: `claims_mismatch` 403; `checkout_required`,
   `checkout_mismatch` and `parent_index_missing` 409; `index_busy` 409. Any other
   error returns 500 `{"code":"internal_error"}` with no body detail.

Research context:
- `RouteTable::dispatch` serves a claimed request natively only under `Native` or
  `Compare`, and proxies everything else
  (`crates/gdaemon/src/front_door/routes.rs`).
- gruntime 3.1 shows the composition pattern: `Box::leak`, `FAMILIES` plus the leaked
  family, and the `x-gobby-key-id` caller rule.
- gruntime 3.2 (#24447) shows `fence`, `verify_static` and `Rejection::{status, body}`.
- The hub DSN is `HubDatabaseBootstrap.database_url`, which `serve::run` already loads.
- `front_door/auth.rs::AuthState::authenticate` strips client identity headers and sets
  `x-gobby-machine-id` only for a resolved API key (`IDENTITY_HEADERS`).
- `postgres` 0.19 blocks on its own runtime. It runs on `spawn_blocking` threads, not
  on async workers.
- Planned checks:
  - `cargo nextest run -p gobby-codeindex`, which is DB-backed through the test DSN
    gruntime's tests use;
  - `cargo nextest run -p gobby-daemon -E 'test(routes)'` and
    `cargo nextest run -p gobby-daemon --test front_door`;
  - `cargo clippy -p gobby-codeindex`.
- `crates/gdaemon/tests/front_door.rs` already builds `RouteTable::new` over
  `FrontDoorState` against a backend socket. `crates/gruntime/tests/config.rs` builds
  a family on `unreachable_pool()`, and a request with no grant header fails before
  any database access, so the default and proxy test needs no database.

**Granularity:** seven acceptance items and a new crate. Registration, verification
and the four handlers are one family's first routes. A family crate with no routes
cannot be tested on its own, and splitting the verification from the handlers would
put two leaves on `node.rs`.

Consumers unchanged:
- `crates/gdaemon/src/front_door/routes.rs` — no-edit-reason: `RouteTable` and `unimplemented_families` take the composed family list `serve::run` passes.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.9.1 - `crates/gcodeindex` builds as workspace member `gobby-codeindex` (publish false, workspace version) and gdaemon links it. file: `crates/gcodeindex/Cargo.toml`.
- 1.9.2 - With a valid remote grant and a matching machine header, each of the four routes returns what the library call returns for that machine. test: `crates/gcodeindex/tests/node_routes.rs::node_routes_serve_the_principal_machine`.
- 1.9.3 - A grant whose machine differs from `x-gobby-machine-id`, a managed principal, and a grant without the `node_code_index` operation return 403 `claims_mismatch`, `claims_mismatch` and `wrong_capability`. test: `crates/gcodeindex/tests/node_routes.rs::node_routes_bind_machine_and_capability`.
- 1.9.4 - A stale epoch returns 409, an expired grant 401, a bad signature 403, and a missing or malformed header Python's 401 or 400. test: `crates/gcodeindex/tests/node_routes.rs::node_routes_follow_python_presentation_order`.
- 1.9.5 - A 15 MiB apply body is accepted and a 17 MiB body returns 413. test: `crates/gcodeindex/tests/node_routes.rs::apply_body_limit_is_sixteen_mib`.
- 1.9.6 - With `FAMILIES` plus the leaked `CodeIndexNodeFamily` composed as `serve::run` composes them, and `code_index_node` absent from `front_door.routes`, a node route request with no grant header gets the native 401 body; with `proxy` it reaches the backend stub. test: `crates/gdaemon/tests/front_door.rs::code_index_node_defaults_to_native_and_honors_proxy`.
- 1.9.7 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.10 Native handshake issues the remote grant shape [category: code] (depends: 1.6, 1.9, #24449)
`kind: deliverable`

Targets:
- `crates/gruntime/src/handshake.rs`
- `crates/gruntime/src/capabilities.rs`
- `crates/gruntime/tests/handshake.rs`
- `crates/gruntime/tests/fixtures/capability_vectors.json`
- `tests/runtime_grants/test_capability_vectors.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): gruntime is in gdaemon's release closure alone, so
this leaf bumps `gdaemon` by 0.0.1.

#24449 ports the interactive handshake and `capabilities_from_snapshot`, and it
always issues `PostgresDirect`. This leaf ports 1.6's two rules:

- The handshake issues `Brokered` with the `node_code_index` operation to an interactive
  principal whose machine differs from the daemon machine
  (`gobby_core::machine::read_machine_id_from_home`). It skips credential issuance and
  caps expiry only for `Direct`.
- `capabilities.rs` makes Falkor and Qdrant `Brokered` whenever Postgres is
  `Brokered`.
- `capability_vectors.json` gains a Brokered-Postgres input, regenerated from Python.

Research context:
- The gruntime files and the Python capability-vector test are created by #24449
  (gdaemon-runtime-family 3.4). Their Targets here are bare paths because they do not
  exist in this checkout yet.
- The golden `tests/runtime_grants/golden/remote_interactive_brokered.json` (1.6) is
  the reference shape.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test handshake --test capabilities`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_capability_vectors.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.10.1 - The Rust capability port matches the Python vector with Brokered Postgres. test: `tests/runtime_grants/test_capability_vectors.py::test_vectors_match_python_capabilities`.
- 1.10.2 - Binding condition: a native handshake for the golden's fixed remote principal returns capabilities and expiry rules equal to `remote_interactive_brokered.json`, so a node built at P1 keeps working after the handshake goes native. test: `crates/gruntime/tests/handshake.rs::remote_principal_handshake_matches_the_golden`.
- 1.10.3 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.11 E2E: node preflight through the hub routes [category: test] (depends: 1.9)
`kind: deliverable`

Targets:
- `tests/e2e/test_hub_node_pair.py::*` — scope-reason: adds the node code-index preflight test beside the existing pair fixtures

Release ownership (Constraints): test files only, so no binary bumps.

Using the existing `pair_hub` and `pair_node` fixtures, register the node's checkout,
create an overlay worktree on the node, and run
`src/gobby/agents/code_index.py::ensure_isolation_code_index`'s three commands with
the node's `gcode` against the node front door.

Research context:
- `tests/e2e/test_hub_node_pair.py` provides `pair_hub`, `pair_node` and
  `test_node_enrolls_and_relays`.
- Planned check: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_pair.py -v -m e2e`.

**Acceptance:**

- 1.11.1 - On an enrolled node with no managed credential, the preflight's status reports unindexed, index indexes the parent then the overlay, and search-content exits 0. The hub's `code_indexed_file_states` rows carry the node's machine id. test: `tests/e2e/test_hub_node_pair.py::test_node_code_index_preflight_through_hub`.

## E1 End-to-end verification
`kind: verification`

These are completion gates for P1. Except for plan validation
(`uv run gobby plans validate .gobby/plans/code-index-family.md -p /Users/josh/Projects/gobby`),
none has run yet. Each must hold on `0.5.0` with freshly built and installed binaries
before #23276 closes:

1. `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code`, then
   `cargo nextest run -p gobby-codeindex -p gobby-runtime -p gobby-daemon`.
2. `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/ tests/install/test_version_pins.py tests/e2e/test_hub_node_pair.py`.
3. A live hub and node pair on the operator's machines: #24380's preflight on the
   node warns about nothing, and `gcode status` on the hub's own checkout is
   unchanged.

## V1 Plan Changelog
`kind: framing`

- 2026-10-17: First draft of P1 by Plan Writer gobby#16767. Decisions D1 to D9 were
  ruled by Orchestrator #15977 and Josh on 2026-10-17. The remaining phases follow in
  this file under #15977's 16:47 ruling.
