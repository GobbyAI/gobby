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

When P2 closes, `gcode` on a node runs every database-backed read command through
the hub: search, grep, symbol and graph navigation, `gcode graph` and evidence. Until
P4 a node's query embedding goes through `/api/embeddings`, which the node front
door relays to the hub.

When P4 closes, a node whose bootstrap names an embeddings endpoint embeds on that
endpoint. Its gdaemon serves `/api/embeddings` with the hub's model, dimension and
query prefix. A node heartbeat job projects the node's own versions into the hub's
Qdrant. A node without that endpoint keeps relaying `/api/embeddings` to the hub.

When P7 closes, the hub's gdaemon can serve every `/api/code-index` route except
`/prune` natively for API-key callers. The `code_index` family defaults to `proxy`
until P9, so Python serves them until `front_door.routes.code_index` is set to
`native` or `compare`. Cookie and agent-bearer callers always reach Python (D15).

When P8 closes, the hub's gdaemon runs the code-index background work: the sync job,
the maintenance pass, summaries and labels, the hourly prune with
`POST /api/code-index/prune`, and content GC for projects only nodes index. A node
releases an agent workspace's rows when it removes the workspace. The edit trigger and
nightly repair entry points are in place for #24151 and #21582 to call (D18).

This revision carries P1 to P8. Under Orchestrator #15977's 2026-10-17 16:47 ruling,
Plan Adversary 16993 reviews each phase while the writer drafts the remaining phases
in this file, in the order #15977 accepted on 2026-10-17 (D14):

- P2: node read parity for the remaining database-backed commands (D7);
- P3: projection sync for a given machine, over the content-hash keys already in
  place (D6, D11);
- P4: node-local embedding (D12, D13);
- P5 to P7: the `/api/code-index` family seam, graph read routes and mutation routes
  (D15, D16);
- P8: heartbeat work (sync worker, trigger, maintenance, summarizer) and
  `/api/code-index/prune` (D10, D17);
- P9: the native default and the deletion of `src/gobby/code_index/`. That deletion
  waits on #24151 (S2.11 hub half: hook ingress and rule engine in Rust) and #21582
  (Cron and scheduler route family). They call 8.7's and 8.8's entry points and delete
  `trigger.py` and `nightly_repair.py` (D18). `maintenance_launch.py` outlives P8,
  because `src/gobby/projects/purge.py` imports it.

The critical path for node completeness is P1, then P3 (after #23834, Vector and
graph pin scope), then P4. Josh approves the complete plan before expansion.

## Constraints
`kind: framing`

- **Stage gate.** #21586 has no #21568 gate. P1's chain to #24380 is: #24446
  (gobby-runtime crate), then #24447 (Grant signing and presentation in gcore), then
  1.9. #24448 (Secret-store port) and #24449 (Native interactive handshake) are not on
  it. Only 1.10 waits on #24449. It sits after the last bumping leaf, now 8.6, so no
  P2 to P8 leaf waits on it.
- **No backward compatibility.** 0.5.0 has not shipped.
- **No Python counterpart for the node routes.** Python has no
  `/api/code-index/node/*`. The `code_index_node` family defaults to `native`.
  - `front_door.routes.code_index_node: proxy` sends the routes to Python, which
    answers 404. That switches node code-index off.
  - `compare` does the same. For a POST, the front door's existing compare path
    serves the proxied response (`crates/gdaemon/src/front_door/routes.rs::compare`).
  - No leaf adds code for either setting.
- **Concurrent editors.** These open leaves of other plans edit files that P1, P3, P4,
  P7 or P8 targets, without changing a seam those phases build on. Apart from 3.1's edge
  on #23834, 8.5's on #23835 and 8.6's on #24372, no leaf takes a dependency edge on
  them. Each executor re-resolves its Targets against the tree it starts from.
  - workspace-index-pin:
    - #23833 (Scoped PostgreSQL and BM25 reads): `commands/search.rs`, `db/queries.rs`, `visibility.rs`;
    - #23834 (Vector and graph pin scope): `commands/graph/lifecycle.rs`, `commands/vector.rs`, `db/queries.rs`;
    - #23835 (Pin-aware retention): `index/indexer/lifecycle.rs`, `commands/status/content_gc.rs` and its `tests.rs`, `commands/status/prune/tests.rs`;
    - #23836 (Candidate-only reconciliation): `index/indexer/overlay.rs`, `freshness.rs`, `local_imports.rs`.
  - code-index-vector-reconciliation:
    - #23579 and #23580 (`gcode vector reconcile`): `contract.rs`, `dispatch.rs`, `commands/vector.rs`, both contract JSON copies;
    - #23582 (Current-config eligibility): `index/indexer/pipeline.rs`, `index/indexer/overlay.rs`;
    - #23578 (Graph CLI honors the completion compare-and-set): `commands/graph/lifecycle.rs`.
  - gdaemon-key-cutover 1.2 and 1.3, and gdaemon-node-channel 1.6:
    `src/gobby/runtime_grants/handshake.py`.
  - node-halves: #24372 (Worktree and clone preparation, repair and removal) creates
    `crates/gdaemon/src/nodes/agents/workspace.rs`.

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
  - Every bumping leaf edits `Cargo.lock` and `version_pins.py`, so the bumping
    leaves form one dependency chain (docs/contracts/plan-coverage.md, shared-target
    ordering). 1.10 sits after the last bumping leaf, 8.6, because it alone waits on
    #24449.
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
  - Refinement (#15977, ruling E5(a), 2026-10-17): P4's
    `/api/code-index/node/vector/roots` takes no grant, because the node gdaemon that
    calls it holds a node key and no grant. The condition: it trusts only the
    `x-gobby-machine-id` the front door sets from the verified node key, never a
    client-supplied value, and returns only that machine's rows. #15977 puts the route
    in the Security Reviewer's scope.
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

#15977 ruled D10 to D14 later on 2026-10-17. They settle D6 and where embedding runs.

- **D10, node graph projection.** The native sync worker projects node machines'
  graph facts. It runs on gdaemon's `HeartbeatHost` (`crates/gdaemon/src/heartbeat.rs`)
  and reads as each machine through `Context::for_machine`. It lands in P8, after
  #23834 (Vector and graph pin scope) and #23576 (Sync worker leaves projection
  completion to gcode). Rejected: projecting node machines from the Python sync
  worker first, which P9 deletes.
- **D11, projection keys.** Graph and vector projections are keyed by content hash
  (P3). A sync removes only versions that no machine's selector or pin names, and
  reads filter with #23834's machine predicate.
- **D12, node embedding endpoint.** A node's gdaemon serves `/api/embeddings`
  natively (P4). It takes the hub's effective embeddings config and calls the node's
  own endpoint. When the model or dimension the endpoint reports differs from the
  hub's, it returns the typed 503 `embedding_identity_mismatch`. Josh's goal: nodes
  run local embedding among the first multi-machine fixes, and P6 does not gate it.
  - Endpoint source (#15977, ruling E4(a), 2026-10-17): `/api/config/effective` never
    serves `ai.embeddings.api_base` or the key. The endpoint therefore comes from an
    `embeddings: {api_base, api_key}` block in the node's 0600 `bootstrap.yaml`. The
    model, dimension and query prefix come from the hub's served config. A node
    without the block relays to the hub.
  - gcore retries a 503 twice, so a refused call costs about 0.75 s. #15977 accepted
    that cost.
- **D13, node vector sync.** A node pulls its own vector work: a node heartbeat job
  runs `gcode vector sync-node`, which reads the node's pending versions from the hub
  and applies the vectors it embeds (P4). The job finds its projects through the roots
  route (D4).
- **D14, phase order.** P1 to P9 run in the order the Overview lists. P4 depends only
  on P1 and P3.

#15977 ruled D15 to D17 later on 2026-10-17, on the writer's packet 5b6906f0. They
settle who the native `/api/code-index` routes serve.

- **D15, callers (ruling E6(a)).** P5 to P7's routes claim API-key callers only, as
  gruntime's family does. Cookie, agent-bearer, break-glass and unauthenticated
  requests reach Python. The web UI's code-graph view signs in with a session cookie
  (`web/src/hooks/useCodeGraph.ts`), and only Python verifies that cookie.
  - P8 and P9 delete the Python orchestration: the sync worker, maintenance,
    summarizer and pruner. #24151 and #21582 delete the trigger and nightly repair
    (D18).
  - Deleting the Python read routes the web UI uses waits on a browser cookie-verifier
    task that #15977 files under hub-node planning. P9 carries that edge as a named
    placeholder until the task exists.
- **D16, Brokered projection operations (ruling E7(a)).** No grant-verified native path
  exists for `clear_projection`, `rebuild_projection` or `invalidate_projection`,
  because no client presents them. P9 removes them from
  `src/gobby/runtime_grants/service.py`, from `src/gobby/servers/grant_auth.py`'s
  grant routes and from `tests/runtime_grants/golden/brokered_datastores.json`. 1.10's
  Rust issuer never adds them.
- **D17, refinements (accepted).**
  - `POST /api/code-index/prune` moves to P8 with the pruner it calls
    (`src/gobby/code_index/prune.py::run_operator_global_prune`).
  - The mutation routes get no lease fence. Python's `admission_required` has no
    callers, and native HTTP admission belongs to the lifecycle cutover (S1.3).

#15977 ruled the edit trigger and nightly repair at 21:46 on 2026-10-17 (E8(a), E9(a))
and replaced both rulings at 22:29 with option (b).

- **D18, edit trigger and nightly repair (rulings E8(a) and E9(a), replaced 22:29 by
  option (b)).** 8.7 ships `gobby_codeindex::jobs::trigger` and 8.8 ships
  `gobby_codeindex::jobs::nightly_repair`, each tested without a caller. Neither leaf
  takes an edge to #24151 or #21582, and neither deletes Python.
  - #24151 calls the trigger from the native hook path and deletes
    `src/gobby/code_index/trigger.py`. #21582 schedules the nightly repair and deletes
    `src/gobby/code_index/nightly_repair.py`. #15977 adds each obligation to its
    epic's description.
  - P9's deletion of `src/gobby/code_index/` waits on both epics.
  - Rejected: the 21:46 shape, where each leaf waited on its epic and deleted the
    Python file. P8 would then wait on the hook and cron ports.

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
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
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
  `/api/code-index/node` and the four route paths (P1 framing), and
  `ROUTE_PATHS: &[&str]`, which lists every route path. 1.9's `claims` reads it, and
  each P2 route leaf appends its paths.
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
  recovery reads "this command needs direct database access; run it on the hub". It
  names no command list, so P2's added commands leave it unchanged. Every `Context`
  resolution under a Brokered grant
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
- `crates/gcode/src/daemon.rs::*` — scope-reason: `DaemonRequestKind` gains `NodeCodeIndex`, and it and the transport helpers `join_daemon_url` and `classify_transport` become `pub(crate)`
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
  It uses `daemon.rs`'s `join_daemon_url` and `classify_transport`.
  - It decodes a 2xx body with `ureq::Response::into_json`, which reads through
    `into_reader` with no size cap, and reads a non-2xx body with `into_string`.
  - It does not use `read_json`, whose `into_string` refuses a body over 10 MiB. A
    node grep or evidence reply can exceed that.
  - It does not use `status_error`, which sends every 401 and 403 through gcore's
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
- `--quiet` keeps its meaning. A node runs no freshness check until 2.2, so
  `--allow-stale` has nothing to skip until then.

Research context:
- `daemon.rs` already wraps `ureq` with typed transport and status errors
  (`classify_transport`, `status_error`, `join_daemon_url`, `read_json`). The enum and
  the helpers are private today (`daemon.rs:18`, `:207`, `:217`, `:239`, `:267`).
- `read_json` (`daemon.rs:217`) reads the body with `into_string`. ureq 2.12.1 caps
  that at `INTO_STRING_LIMIT`, 10 MiB (`src/response.rs:33` and `:456`). `into_json`
  (`:532`) deserializes from `into_reader` with no cap, and gcode's `ureq` enables the
  `json` feature (`crates/gcode/Cargo.toml`). On the request side, 1.9's
  `DefaultBodyLimit::max(16 * 1024 * 1024)` admits the 8 MiB apply batches.
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
- `crates/gcode/src/dispatch_navigation.rs` — no-edit-reason: passes `&Context` to `resolve_path_input`, which accepts any `ProjectView`.
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
- `claims(parts)` holds for a `POST` to a path in `gobby_code::node::ROUTE_PATHS` when
  the request carries `x-gobby-key-id` (an API-key caller), mirroring gruntime's
  `Caller::Operator`.

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

### 1.10 Native handshake issues the remote grant shape [category: code] (depends: 1.6, 8.6, #24449)
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

## P2: Node read parity
`kind: framing`

P2 gives a node every database-backed read command that P1 refuses (D7). Each one
works the way P1's `search-content` does. The hub runs gcode's library as the
principal's machine and returns typed results that depend on no file on the hub's
disk. The node checks its own files, pages and renders with the local code, so it
prints what a local run against the same index prints.

- 2.1 makes the remaining read paths take the machine from the context.
- 2.2 gives node reads the freshness check that local reads run.
- 2.3 to 2.8 add the routes, one leaf per command group.
- 2.9 proves the commands end to end.

`projects` needs no route. It reads `GET /api/projects` through the daemon, which the
node front door relays (`commands/status/projects.rs::projects`). The write and
maintenance commands (`init`, `repair`, `retire-files`, `prune`, the `graph` and
`vector` mutations and `embeddings doctor`) keep 1.7's refusal, whose recovery sends
them to the hub. 8.6 serves `invalidate` for a node's own checkout, so a node can
release its rows when it removes a workspace.

Wire names. P2 changes no grant: the one `node_code_index` operation authorizes every
path under `/api/code-index/node` (D3). Each leaf appends its paths to
`gobby_code::node::ROUTE_PATHS`, which 1.9's `claims` reads. Every route is `POST`
with a JSON body:

- freshness: `/api/code-index/node/freshness` (2.2);
- symbol search: `/api/code-index/node/search`, `/api/code-index/node/search-symbol`
  and `/api/code-index/node/search-text` (2.3);
- grep: `/api/code-index/node/grep` (2.4);
- symbol navigation: `/api/code-index/node/symbols/file`,
  `/api/code-index/node/symbols/ids`, `/api/code-index/node/symbols/kinds` and
  `/api/code-index/node/symbols/tree` (2.5);
- graph navigation: `/api/code-index/node/nav/callers`, `/nav/callees`,
  `/nav/usages`, `/nav/imports`, `/nav/path` and `/nav/blast-radius` under the same
  prefix (2.6);
- `gcode graph`: `/api/code-index/node/graph/report`, `/graph/overview`,
  `/graph/file`, `/graph/neighbors`, `/graph/blast-radius` and `/graph/view` under the
  same prefix (2.7);
- evidence: `/api/code-index/node/evidence/facts` (2.8).

File layout. Each route leaf adds one group `<group>` in new files:

- `crates/gcode/src/node/<group>.rs` holds the wire types and path constants. It is
  declared in `node/mod.rs`.
- `crates/gcode/src/node/hub/<group>.rs` holds the hub entry points, declared in
  `node/hub.rs`.
- `crates/gcode/src/node/client/<group>.rs` holds the node commands, declared in
  `node/client.rs`.
- `crates/gcodeindex/src/node/<group>.rs` holds the handlers. It is declared in
  `crates/gcodeindex/src/node.rs`, and its routes are registered in `lib.rs`'s
  `router`.
- The tests go in `crates/gcode/src/node/<group>_tests.rs` and
  `crates/gcodeindex/tests/node_<group>.rs`.

Rules every route leaf follows:

- `dispatch.rs::run` sends the group's commands through `resolve_access`. `Direct`
  runs today's code. `Node` calls `node::client::<group>` with the clap fields
  `dispatch_navigation.rs` reads.
- Before posting, the node client runs the freshness scope that local dispatch runs
  for the command (2.2), unless `--allow-stale` is set. It validates arguments with
  the same local functions, such as glob compilation and path resolution through
  `ProjectView`, so input errors print as they do today.
- Each handler runs 1.9's verification (steps 1 to 5), runs its library call under
  `spawn_blocking`, and maps errors with 1.9's step 7. An outcome that is a result,
  such as an unresolved symbol or an unavailable graph, travels as data in the
  response, so the node prints the local message.
- The hub builds its `Context` through `HubScope::context`, with the services the
  command needs (2.3).
- Until P3, P4 and P8 project node versions (D6), the hub's Falkor and Qdrant data
  hold only versions projected from the hub's machine. A node's graph and semantic
  results pass through the node machine's visibility filter, so they contain only
  versions that machine sees. The BM25 and exact lanes are complete.

Release ownership (Constraints): from 1.9 on, a gcode library change bumps `gcode` and
`gdaemon`, and every route leaf changes both crates.

Ordering. The P2 leaves form one chain, 2.1 to 2.9. They share `dispatch.rs::run`,
the `node` module files, gcodeindex's `lib.rs` and `node.rs`, and the release files.

### 2.1 Machine-explicit read paths [category: refactor] (depends: 1.9)
`kind: deliverable`

Targets:
- `crates/gcode/src/visibility.rs::local_machine_uuid`
- `crates/gcode/src/visibility.rs::local_machine_uuid_or_invisible`
- `crates/gcode/src/visibility.rs::local_active_content_hash`
- `crates/gcode/src/visibility.rs::indexed_file_exists`
- `crates/gcode/src/visibility.rs::content_chunks_exist`
- `crates/gcode/src/visibility.rs::symbol_is_visible`
- `crates/gcode/src/visibility.rs::project_path_is_visible`
- `crates/gcode/src/visibility.rs::project_file_is_visible`
- `crates/gcode/src/visibility.rs::overlay_has_row`
- `crates/gcode/src/visibility.rs::indexed_file_states`
- `crates/gcode/src/visibility.rs::query_symbols_for_files`
- `crates/gcode/src/visibility.rs::query_overlay_symbols_for_files`
- `crates/gcode/src/visibility.rs::filter_visible_symbols`
- `crates/gcode/src/visibility.rs::visible_symbols_for_files`
- `crates/gcode/src/visibility/catalog.rs::visible_kinds`
- `crates/gcode/src/visibility/catalog.rs::visible_tree`
- `crates/gcode/src/visibility/catalog.rs::tombstone_count`
- `crates/gcode/src/visibility/graph.rs::visible_graph_paths`
- `crates/gcode/src/search/fts/common.rs::push_visible_project_file_filter`
- `crates/gcode/src/search/fts/common.rs::query_symbols_by_conditions`
- `crates/gcode/src/search/fts/symbols.rs::search_symbols_fts`
- `crates/gcode/src/search/fts/symbols.rs::search_symbols_by_name`
- `crates/gcode/src/search/fts/symbols.rs::query_visible_symbols_by_conditions`
- `crates/gcode/src/search/fts/content.rs::visible_files_sql`
- `crates/gcode/src/search/fts/graph.rs::*` — scope-reason: `exact_symbol_matches_result`, `resolve_graph_symbol` and `resolve_graph_symbol_by_id` take the machine
- `crates/gcode/src/commands/grep.rs::load_indexed_chunks`
- `crates/gcode/src/commands/grep.rs::indexed_files_cover_filters`
- `crates/gcode/src/commands/scope.rs::other_project_for_path`
- `crates/gcode/src/commands/graph/reads.rs::*` — scope-reason: `resolve_symbol_with_connection` takes the `Context`, and the inline tests move to `crates/gcode/src/commands/graph/reads/tests.rs`
- `crates/gcode/src/commands/graph/reads/tests.rs`
- `crates/gcode/src/commands/graph/view/mod.rs::local_machine_id`
- `crates/gcode/src/commands/graph/view/mod.rs::visible_map_for_candidates`
- `crates/gcode/src/commands/graph/view/mod.rs::resolve_view_seed`
- `crates/gcode/src/commands/graph/view/fcg.rs::fetch_fcg_hop`
- `crates/gcode/src/commands/graph/view/mcg/fetch.rs::fetch_mcg_hop`
- `crates/gcode/src/commands/graph/view/class_hierarchy/fetch.rs::falkor_page`
- `crates/gcode/src/communities.rs::read_for_context`
- `crates/gcode/src/communities.rs::partition_refreshed`
- `crates/gcode/src/graph/code_graph/read/relationships.rs::get_imports`
- `crates/gcode/src/visibility/tests.rs::*` — scope-reason: adds the foreign-machine symbol, kind and tree read tests
- `crates/gcode/src/search/fts/tests.rs::*` — scope-reason: the `local_active_content_hash` callers pass a context, and the `search_symbols_fts`, `resolve_graph_symbol` and `resolve_graph_symbol_by_id` callers pass the fixture machine
- `crates/gcode/src/commands/grep/db_tests.rs::*` — scope-reason: adds the foreign-machine grep test
- `crates/gcode/src/communities/refresh_tests.rs::*` — scope-reason: adds the foreign-machine community and graph-visibility read test
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): gdaemon links `gobby-code` from 1.9 on, so this leaf
bumps `gcode` and `gdaemon` by 0.0.1.

1.1 moved the machine onto `Context` for the three P1 paths. Every other read still
takes it from the process, so on the hub it would read the hub's own selectors. This
leaf moves the machine to the context on every read path P2's routes call. Local
behavior does not change.

- `visibility.rs`'s `local_machine_uuid` and `local_machine_uuid_or_invisible` take
  `&Context`, parse `ctx.machine_id`, and become `machine_uuid` and
  `machine_uuid_or_invisible`. A context whose machine does not parse sees nothing,
  as an unreadable local machine does today.
- These call the renamed helpers with their context:
  - in `visibility.rs`: `local_active_content_hash` (which now takes `&Context`),
    `content_chunks_exist`, `symbol_is_visible`, and 1.1's `indexed_file_exists`;
  - in `visibility/catalog.rs`: `visible_kinds`, `visible_tree`, and 1.1's
    `tombstone_count`;
  - `visibility/graph.rs::visible_graph_paths`;
  - `search/fts/common.rs::push_visible_project_file_filter` and 1.1's
    `search/fts/content.rs::visible_files_sql`;
  - in `commands/grep.rs`: `load_indexed_chunks` and `indexed_files_cover_filters`;
  - `communities.rs`'s `read_for_context` and `partition_refreshed`;
  - `graph/code_graph/read/relationships.rs::get_imports`, through
    `local_active_content_hash`.
- `project_file_is_visible` and `overlay_has_row` take the machine as a parameter.
  `project_path_is_visible` passes its context's machine.
- In `visibility.rs`, `indexed_file_states`, `query_symbols_for_files` and
  `query_overlay_symbols_for_files` have no context, so they take the machine
  (`Uuid`) as a parameter. `filter_visible_symbols` passes
  `machine_uuid_or_invisible(ctx)` and returns no symbols when it is `None`.
  `visible_symbols_for_files` passes `machine_uuid(ctx)?`. Both keep today's result
  for an unreadable machine.
- The graph symbol resolver has no context below its command entry points. These take
  the machine (`Uuid`) as a parameter: `search/fts/common.rs::query_symbols_by_conditions`,
  `search/fts/symbols.rs`'s `search_symbols_fts` and `search_symbols_by_name`, and
  `search/fts/graph.rs`'s `exact_symbol_matches_result`, `resolve_graph_symbol` and
  `resolve_graph_symbol_by_id`.
  - `search/fts/symbols.rs::query_visible_symbols_by_conditions` reads
    `machine_uuid_or_invisible(ctx)` and returns no symbols when it is `None`.
  - `commands/graph/reads.rs::resolve_symbol_with_connection` takes the `Context` in
    place of its project id. It resolves in `ctx.project_id` and returns no symbol and
    no suggestions when `machine_uuid_or_invisible(ctx)` is `None`.
    `resolve_symbol` and `view/mod.rs::resolve_view_seed` pass their context.
  - That keeps today's result for an unreadable machine: `query_symbols_by_conditions`
    and `exact_symbol_matches_result` return no rows for it now.
- `commands/graph/reads.rs` (967 lines, 282 of them its inline test module) moves that
  module to `crates/gcode/src/commands/graph/reads/tests.rs`, declared with
  `#[cfg(test)] #[path = "reads/tests.rs"] mod tests;`. Its three resolver tests build
  a `Context` for their fixture project, so the file stays under the ceiling.
- `commands/scope.rs::other_project_for_path` reads `ctx.machine_id`.
- In the graph views, `view/mod.rs::local_machine_id()` becomes `machine_id(&Context)`.
  `visible_map_for_candidates` and `class_hierarchy/fetch.rs::falkor_page` pass their
  context. `fcg.rs::fetch_fcg_hop` and `mcg/fetch.rs::fetch_mcg_hop` pass
  `facts.context()`.

Write and maintenance paths keep their local reads, because no node route calls them:
`status/content_gc.rs`, `status/retire_files.rs`, `status/invalidate.rs`,
`commands/repair.rs`, `vector/code_symbols/repository.rs`, `status/shared.rs` (used
by prune), and `db/queries.rs`'s projection readers and `mark_*` writers (P3 and P8).

Research context:
- The read sites were listed with
  `gcode grep -n -E "local_machine_uuid(_or_invisible)?\(|read_local_machine_id\(|local_machine_id\(\)" crates/gcode/src`
  and traced from the P2 commands' entry points in `dispatch_navigation.rs` and
  `dispatch.rs`.
- `project_file_is_visible` and `overlay_has_row` take a project id instead of a
  context, because `project_path_is_visible` calls them for an overlay and its parent.
  Nothing outside `visibility.rs` calls them.
- `fetch_fcg_hop` and `fetch_mcg_hop` receive a `CodewikiFacts`, whose `context()` is
  the command's `Context`.
- `local_active_content_hash` has two test callers in `search/fts/tests.rs`.
- These read the process machine and have no context today:
  `query_symbols_by_conditions` (`search/fts/common.rs:351`), which only
  `search/fts/symbols.rs` calls; `exact_symbol_matches_result`
  (`search/fts/graph.rs:17`); and `indexed_file_states`, `query_symbols_for_files` and
  `query_overlay_symbols_for_files` (`visibility.rs:505`, `:625`, `:639`).
  `resolve_graph_symbol` and `resolve_graph_symbol_by_id` are reached only through
  `commands/graph/reads.rs::resolve_symbol_with_connection`, whose production callers
  `resolve_symbol` and `view/mod.rs::resolve_view_seed` hold a `Context`.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(visibility) | test(grep) | test(communities) | test(fts) | test(graph)'`;
  - `cargo clippy -p gobby-code`.

**Granularity:** one outcome across the read paths. The machine source moves from
the process to the context, and the helpers every path shares change signature in
the same commit. Splitting by command would leave the renamed helpers half-adopted
between leaves.

Consumers unchanged:
- `crates/gcode/src/search/fts/counts.rs` — no-edit-reason: passes its `ctx` to `push_visible_project_file_filter`, which keeps its signature.
- `crates/gcode/src/search/fts.rs` — no-edit-reason: re-exports `resolve_graph_symbol` and `resolve_graph_symbol_by_id` and imports `search_symbols_fts` by name.
- `crates/gcode/src/graph/report/loading.rs` — no-edit-reason: passes its `ctx` to `communities::read_for_context`, which keeps its signature.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.1.1 - Symbol-by-file, symbol-by-id, kinds and tree reads under a `Context` whose `machine_id` is a fresh UUID return the rows visible to that machine's selectors and none visible only to the local machine. test: `crates/gcode/src/visibility/tests.rs::read_paths_use_the_context_machine`.
- 2.1.2 - Grep's chunk load and coverage check under a foreign-machine context see only that machine's files. test: `crates/gcode/src/commands/grep/db_tests.rs::grep_reads_the_context_machine`.
- 2.1.3 - `communities::read_for_context` and `visible_graph_paths` under a foreign-machine context read that machine's partition and paths. test: `crates/gcode/src/communities/refresh_tests.rs::community_and_graph_reads_use_the_context_machine`.
- 2.1.4 - `resolve_symbol_with_connection` under a foreign-machine context resolves a name and an id to the symbol that machine sees, and resolves nothing visible only to the local machine. test: `crates/gcode/src/commands/graph/reads/tests.rs::graph_symbol_resolution_uses_the_context_machine`.
- 2.1.5 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.2 Node freshness [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/freshness.rs::project_needs_refresh`
- `crates/gcode/src/freshness.rs::INFLIGHT_ENV`
- `crates/gcode/src/freshness/facts.rs`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/dispatch.rs::warn_if_busy`
- `crates/gcode/src/dispatch.rs::warn_if_freshness_skipped`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/freshness.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/freshness.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/freshness.rs`
- `crates/gcode/src/node/freshness_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/freshness.rs`
- `crates/gcodeindex/tests/node_freshness.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

A local read checks freshness first (`dispatch.rs::ensure_project_fresh` and
`ensure_file_fresh`). When files changed since the last index, it indexes the changes
under the project lock, or skips with a warning when the lock is busy. A node gets the
same behavior: it reads its tree on the node and writes the index through P1's routes.

- `freshness::project_needs_refresh` splits at its disk read. The halves go in the new
  `crates/gcode/src/freshness/facts.rs`:
  - `freshness_facts(conn, ctx) -> FreshnessFacts` reads only the database. It
    returns `Refresh` when the context's machine has no `last_indexed_at` for the
    project, or when an overlay's parent was indexed after the overlay. Otherwise it
    returns `Compare { last_indexed_at, indexed_paths }`, with the paths
    `project_needs_refresh` builds today: `list_indexed_file_paths` for a single
    project, `visible_tree` for an overlay.
  - `facts_need_refresh(root, &facts, &indexing) -> bool` is true for `Refresh`.
    Otherwise it calls `api::project_changed_since` with the root, `last_indexed_at`,
    `indexed_paths`, `extra_excludes` and `respect_gitignore`.
  - `project_needs_refresh` runs both halves, so local reads behave as before.
  - `FreshnessFacts` derives `Serialize, Deserialize`. `last_indexed_at` travels as
    Unix-epoch microseconds, the precision of PostgreSQL `timestamptz`, so the mtime
    comparison does not change.
- Route `/api/code-index/node/freshness`: `FreshnessRequest { scope: NodeScope }`
  returns `FreshnessFacts`. `node::hub::freshness` opens a read-only connection and
  calls `freshness_facts` for the principal's machine.
- `IndexApplyRequest` (1.4) gains `freshness: bool`. When it is set,
  `node::hub::apply_index` takes the project lock with
  `IndexLockPolicy::brief_freshness_try()`, the policy local freshness uses, instead of
  1.4's 60-second wait. A busy lock still returns `index_busy`. The hub takes the
  project lock for a files-scope apply too.
  - Local files-scope freshness locks only the files (`lock_project_files`). The hub
    cannot, because its one apply path re-derives adoption and keeps the
    `project_checkouts` fence under the project lock (D2).
  - Accepted difference: while a long project index holds the lock, node `outline`,
    `symbol-at` and `symbol` freshness report `SkippedBusy` where a local run might
    refresh. The read then serves the existing index, as a busy local project
    freshness does.
- `node::client::freshness` mirrors `freshness::ensure_fresh` for a `NodeAccess`:
  - With `GCODE_FRESHNESS_INFLIGHT` set (`freshness::INFLIGHT_ENV`, made `pub(crate)`),
    it returns `Checked`.
  - Project scope posts `/freshness`. When `facts_need_refresh` is true, it runs 1.8's
    index sequence with `freshness` set and no path filter.
  - Files scope runs the index sequence with `path_filter` set to the files, without
    the facts call, as local `ensure_fresh` skips its pre-gate for files.
  - `index_busy` from any request maps to `FreshnessStatus::SkippedBusy(None)`. Any
    other failure maps to `FreshnessStatus::Degraded` with the error text. Otherwise the
    result is `Checked`.
  - It never calls `Context::can_refresh_index`, which restricts only managed
    principals. A node's grant is interactive (D3).
- `dispatch.rs`'s `warn_if_busy` and `warn_if_freshness_skipped` take
  `&impl ProjectView` and become `pub(crate)`, so the node prints the local warnings.
- `dispatch.rs::run` runs project freshness before a node `search-content`, unless
  `--allow-stale` is set, as `dispatch_navigation.rs` does locally. Node `status` and
  `index` run none, as locally.
- Symbol freshness needs the symbol route and lands in 2.5.

Research context:
- `ensure_fresh` (`freshness.rs:48`) returns `Checked` under `GCODE_FRESHNESS_INFLIGHT`,
  and when `can_refresh_index` refuses a managed principal's scope. Project scope
  pre-gates with `project_needs_refresh`, then refreshes under
  `with_project_lock(ctx, IndexLockPolicy::brief_freshness_try(), …)` (150 ms). Files
  scope locks the files with `lock_project_files` and refreshes the ones it acquired.
- `project_needs_refresh` (`:135`) returns true when the machine has no
  `last_indexed_at`, or when an overlay's parent was indexed later. Otherwise it
  compares disk mtimes and deletions with `api::project_changed_since`
  (`index/indexer/freshness_probe.rs`), which takes `last_indexed_at` as a
  `SystemTime`.
- `dispatch_navigation.rs` runs project freshness for `search-content`, `search`,
  `search-symbol`, `search-text`, `grep`, `symbols`, `kinds`, `tree`, the six graph
  navigation commands and `repo-outline`. It runs file freshness for `outline` and
  `symbol-at`, and symbol freshness for `symbol`.
- `freshness.rs` has 972 lines, 718 of them inline tests. Putting the halves in
  `freshness/facts.rs` shrinks it.
- Rejected alternative: using 1.4's plan call as the probe. The plan omits adoptable
  paths, so a never-indexed machine whose files were all adoptable would read as
  fresh. It also hashes the whole tree on every read, where the mtime pre-gate reads
  only metadata.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(freshness)'`;
  - `cargo nextest run -p gobby-codeindex`.

**Granularity:** seven acceptance items and nine new files. The facts split, the route
and the node check are one freshness behavior. The route and its client cannot be
tested apart.

Consumers unchanged:
- `crates/gcode/src/dispatch_navigation.rs` — no-edit-reason: calls `ensure_project_fresh` and `ensure_file_fresh`, whose behavior is unchanged.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.2.1 - The local overlay pre-gate behaves as before the split: it counts inherited paths and trips on a parent reindex or an edit. test: `crates/gcode/src/freshness.rs::overlay_pre_gate_counts_inherited_paths_and_trips_on_parent_reindex_or_edits`.
- 2.2.2 - `node::hub::freshness` for a foreign machine returns `Refresh` when that machine never indexed the project, even when the local machine has, and otherwise `Compare` with that machine's paths. test: `crates/gcode/src/node/freshness_tests.rs::hub_freshness_facts_are_per_machine`.
- 2.2.3 - A node project read after an edit indexes the edited file through plan and apply with `freshness` set, and a read of an unchanged tree posts only the facts call. test: `crates/gcode/src/node/freshness_tests.rs::node_project_freshness_indexes_changes_only`.
- 2.2.4 - With the project lock held, a freshness apply returns `index_busy` after the brief try, and the node prints the local busy warning and reads the existing index. test: `crates/gcode/src/node/freshness_tests.rs::busy_lock_skips_node_freshness_with_the_local_warning`.
- 2.2.5 - A node `search-content` runs project freshness first, and `--allow-stale` skips it with the local notice. test: `crates/gcode/src/node/freshness_tests.rs::node_search_content_runs_freshness_unless_allow_stale`.
- 2.2.6 - The freshness route serves the principal's machine. test: `crates/gcodeindex/tests/node_freshness.rs::freshness_route_serves_the_principal_machine`.
- 2.2.7 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.3 Node search, search-symbol and search-text [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/search.rs::search`
- `crates/gcode/src/commands/search.rs::search_symbol`
- `crates/gcode/src/commands/search.rs::search_symbol_with_graph`
- `crates/gcode/src/commands/search.rs::search_text`
- `crates/gcode/src/commands/search.rs::semantic_lane`
- `crates/gcode/src/commands/search.rs::resolve_hybrid_symbols`
- `crates/gcode/src/commands/search.rs::symbol_matches_filters`
- `crates/gcode/src/commands/search.rs::symbol_matches_local_filters`
- `crates/gcode/src/commands/search.rs::search_result_matches_filters`
- `crates/gcode/src/commands/search/symbol_page.rs`
- `crates/gcode/src/commands/search/output.rs::print_search_hint`
- `crates/gcode/src/vector/code_symbols/search.rs::*` — scope-reason: `semantic_search` splits into its query embedding and `semantic_search_with_vector`
- `crates/gcode/src/vector/code_symbols/embedding.rs::*` — scope-reason: adds `daemon_query_embedding`
- `crates/gcode/src/models.rs::SearchWarning`
- `crates/gcode/src/models.rs::SearchWarningCause`
- `crates/gcode/src/config/context.rs::*` — scope-reason: `Context::for_machine` takes a `ServiceConfigSelection`, and `NodeAccess` gains the grant's AI state
- `crates/gcode/src/config/layers.rs::*` — scope-reason: adds `ConfigLayers::hub()`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/search.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/search.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/search.rs`
- `crates/gcode/src/node/search_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/search.rs`
- `crates/gcodeindex/tests/node_search.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

The three commands split at their disk check, as 1.3 split `search_content`:

- The hub halves read no file on disk:
  - `hybrid_candidates(conn, ctx, query, options, semantic) -> SymbolCandidates` (for
    `search`) runs the exact, BM25, semantic and graph lanes and the RRF merge. It
    applies the kind, language and path-pattern filters and
    `visibility::indexed_file_exists`, then sorts as `search` does today.
  - `exact_candidates(conn, ctx, query, options) -> SymbolCandidates` (for
    `search-symbol`, with or without `--with-graph`) does the same for the exact lane
    and its graph variant.
  - `text_candidates(conn, ctx, query, options) -> TextCandidates` (for `search-text`)
    runs the FTS fetch, the language and path filters and `indexed_file_exists`. It
    reports `db_total` from `count_text_visible` when no path filter is set, and
    `fetch_capped` when a filtered fetch reached `FILTERED_FETCH_CAP`.
- `SymbolCandidates { ranked: Vec<RankedSymbol>, warnings: Vec<SearchWarning> }` and
  `RankedSymbol { symbol: Symbol, rrf_score: Option<f64>, sources: Option<Vec<String>> }`.
  The RRF fields are absent for exact-only `search-symbol`.
  `TextCandidates { results: Vec<SearchResult>, db_total: Option<usize>, fetch_capped: bool }`.
- The page halves are `page_symbols(candidates, query, options, exists)` and
  `page_text(candidates, options, exists)`. Each drops the results whose file `exists`
  rejects and takes the total (`db_total`, or else the filtered count). Each then
  pages, scores (`final_rank_score` for ranked results, `exact_tier_score` for exact
  ones) and computes the hints, which read only the request: `search_lane_hint`, the
  path post-filter hint and `search-text`'s fetch-cap hint.
- `search`, `search_symbol` and `search_text` run their candidates then their page with
  `scope::path_exists_in_current_project` as `exists`, then print. Filtering after the
  sort keeps the order of the remaining results, so output is unchanged.
- These bodies move out of `crates/gcode/src/commands/search.rs` into the new
  `crates/gcode/src/commands/search/symbol_page.rs`: the three command bodies,
  `search_symbol_with_graph`, `resolve_hybrid_symbols` and the three filter helpers.
  `commands/search.rs` (892 lines before its tests) shrinks instead of growing.
- `output.rs::print_search_hint` takes `&impl ProjectView`, like 1.3's two printers.

Semantic lane:

- `vector/code_symbols/search.rs::semantic_search` splits into its query embedding and
  `semantic_search_with_vector(ctx, &[f32], limit)`, which runs the Qdrant search and
  the visibility post-filter.
- `hybrid_candidates` takes `SemanticSource`:
  - `Embed` is the local path: today's `semantic_lane`, with its grant-outage check
    and `semantic_search`.
  - `Vector(QueryVector)` is the hub path when the request carries a vector.
  - `Skipped` is the hub path without one. It yields no hits and no warning.
- `QueryVector { values: Vec<f32>, model: String, dimension: usize }`. The hub compares
  `model` with its context's `embedding.model`, and `dimension` with
  `code_vectors.vector_dim` when that is set. On a mismatch it skips the lane and adds
  `SearchWarning { lane: Semantic, cause: EmbeddingIdentityMismatch, message }`.
  `SearchWarningCause` gains that variant (`embedding_identity_mismatch`). It is a
  degradation, like `DaemonUnreachable`, so the contract's error codes do not change.
- The node embeds the query before posting with the new
  `embedding.rs::daemon_query_embedding(project_id, grant, query) -> Result<QueryVector>`.
  It posts `/api/embeddings` through the daemon with no model override, as
  `audited_query_embedding`'s daemon branch does, and returns the embedding with the
  response's `model` and `dim`. Until P4 the node front door relays that request to
  the hub, so query embedding runs on the hub (Overview).
- When the grant reports the daemon unreachable, the node adds
  `semantic_lane_from_grant_outage()`'s warning and sends no vector, as local
  `semantic_lane` does. Any other embedding failure is logged and sends no vector, as
  local `semantic_search` does.
- `SearchWarning` gains `Deserialize`. `SearchWarningLane` and `SearchWarningCause`
  already derive it.

Hub services:

- `Context::for_machine` gains a `ServiceConfigSelection`. It resolves the selected
  services with `resolve_services` over the new `ConfigLayers::hub()`, which is Hub
  mode with no daemon snapshot. That reads the hub's PostgreSQL configuration snapshot
  (`ServiceSource::hub(capture_hub_snapshot(conn))`).
- `HubScope::context` takes the selection. P1's routes pass
  `ServiceConfigSelection::database_only()`. The search routes select Falkor, Qdrant,
  embedding and code vectors.
- `NodeAccess` gains the grant's AI state, built from the acquired grant the way
  `resolve_from_root` builds `Context::grant_ai`. `daemon_query_embedding` and the
  outage check read it.

Wire (`crates/gcode/src/node/search.rs`):

- `/search` and `/search-symbol` take
  `SymbolSearchRequest { scope, query, kind, language, paths, with_graph, fetch_limit, query_vector: Option<QueryVector> }`
  and return `SymbolCandidates`. `search-symbol` sends no vector.
- `/search-text` takes `TextSearchRequest { scope, query, language, paths, fetch_limit }`
  and returns `TextCandidates`.
- The node computes `fetch_limit` as the commands do today, and resolves `paths` to
  project-root-relative filters (1.8).

Research context:
- `search` (`commands/search.rs:100`), `search_symbol` (`:259`),
  `search_symbol_with_graph` (`:358`) and `search_text` (`:463`) fetch, merge, filter,
  page and print. Their filters end in a disk check: `symbol_matches_local_filters`
  calls `scope::path_exists_in_current_project`, and `search_result_matches_filters`
  calls `scope::current_indexed_path_is_valid`.
- `semantic_lane` (`:89`) returns the grant-outage warning when the grant is unexpired
  and the daemon unreachable. Otherwise `code_symbols::semantic_search` embeds through
  `embedding_source_from_context` and `embed_query_with_source`.
- `audited_query_embedding` (`vector/code_symbols/embedding.rs:162`) resolves an
  `AiContext`, attaches the grant and calls `gobby_core`'s `embed_via_daemon`, whose
  response carries `model` and `dim`.
- `resolve_services` (`config/context.rs:236`) builds the Falkor, Qdrant, embedding and
  code-vector settings from a `ServiceSource`. In Hub mode, `ServiceSource::new` reads
  `capture_hub_snapshot(conn)` (`config/layers.rs:108`).
- Rejected alternative: the hub embedding the query with its own configuration. That
  works until P4. But P4 points a node's embeddings at the node's own endpoint, and a
  node's query would then meet hub vectors from another model unless the identity
  travels with the vector. With the vector and its identity in the request, P4
  changes no search code.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(search)'`;
  - `cargo nextest run -p gobby-codeindex`.

**Granularity:** seven acceptance items. The three commands share the candidates and
page split, the wire module and the hub services, and splitting them would put three
leaves on `commands/search.rs` and `symbol_page.rs`.

Consumers unchanged:
- `crates/gcode/src/dispatch_navigation.rs` — no-edit-reason: calls `search`, `search_symbol` and `search_text` with the same arguments, and they still print.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.3.1 - Local `search`, `search-symbol` (with and without `--with-graph`) and `search-text` output is byte-identical before and after the split, including a result whose file was deleted on disk after indexing. test: `crates/gcode/src/node/search_tests.rs::symbol_search_split_matches_the_cli_output`.
- 2.3.2 - Under the remote golden grant, `gcode search` with a relative path filter sends the project-root-relative path and a query vector with its model and dimension, and prints the page of the returned candidates whose files exist on the node. test: `crates/gcode/src/node/search_tests.rs::node_search_renders_hub_candidates`.
- 2.3.3 - A query vector whose model or dimension differs from the hub's yields the BM25 and graph results with one `embedding_identity_mismatch` semantic warning. test: `crates/gcode/src/node/search_tests.rs::identity_mismatch_degrades_the_semantic_lane`.
- 2.3.4 - With the daemon unreachable, a node `search` sends no vector and prints the local grant-outage warning. test: `crates/gcode/src/node/search_tests.rs::grant_outage_sends_no_vector`.
- 2.3.5 - `Context::for_machine` resolves the selected services from the hub's PostgreSQL configuration snapshot, and P1's routes still resolve none. test: `crates/gcode/src/node/search_tests.rs::hub_context_resolves_selected_services_from_the_hub_snapshot`.
- 2.3.6 - The three routes serve the principal's machine. test: `crates/gcodeindex/tests/node_search.rs::search_routes_serve_the_principal_machine`.
- 2.3.7 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.4 Node grep [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/grep.rs::run`
- `crates/gcode/src/commands/grep.rs::grep_repo`
- `crates/gcode/src/commands/grep.rs::load_indexed_chunks`
- `crates/gcode/src/commands/grep.rs::attach_coverage_gap`
- `crates/gcode/src/commands/grep.rs::indexed_files_cover_filters`
- `crates/gcode/src/commands/grep.rs::GrepMatch`
- `crates/gcode/src/commands/grep.rs::GrepSpan`
- `crates/gcode/src/commands/grep.rs::GrepContextLine`
- `crates/gcode/src/commands/grep/candidates.rs`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/grep.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/grep.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/grep.rs`
- `crates/gcode/src/node/grep_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/grep.rs`
- `crates/gcodeindex/tests/node_grep.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

Grep scans indexed chunks on the hub and pages on the node.

- `grep_candidates(conn, ctx, options, exists) -> GrepCandidates` is the hub half. It
  loads the indexed chunks, scans every match with no `max_count` or offset, and
  attaches the coverage gap.
  - `load_indexed_chunks` takes `exists` in place of its
    `scope::current_indexed_path_is_valid` call. It keeps
    `visibility::indexed_file_exists` and calls `exists` for the disk half. The hub
    passes a predicate that accepts every path.
  - `GrepCandidates { result: GrepResult, chunks_by_file: BTreeMap<String, usize> }`
    counts the scanned chunks of each file.
- The node half drops each match whose file `path_exists_in_current_project` rejects.
  It subtracts those files' chunks from `scanned_chunks`, recounts `matched_lines` from
  the remaining matches, then pages and prints with `print_file_page` or
  `print_match_page`, as `run` does today.
- `run` calls `grep_candidates` with `scope::path_exists_in_current_project`, then
  pages, so local output is unchanged. `grep_repo` keeps its signature and calls the
  new `grep_repo_with(ctx, conn, options, exists)`, which holds its body, with the
  same disk check. 2.8's hub facts call `grep_repo_with` with the accept-all
  predicate.
- The hub half moves into the new `crates/gcode/src/commands/grep/candidates.rs`:
  `load_indexed_chunks`, `attach_coverage_gap`, `indexed_files_cover_filters` and the
  SQL prefilter helpers they call. `commands/grep.rs` (953 lines) shrinks instead of
  growing.
- `GrepMatch`, `GrepSpan` and `GrepContextLine` gain `Deserialize`.
- Route `/api/code-index/node/grep`: `GrepRequest { scope, pattern, fixed_strings, ignore_case, word, context, before_context, after_context, paths, globs }`
  returns `GrepCandidates`. The node compiles the globs and resolves the paths before
  posting (1.8).

Research context:
- `run` (`commands/grep.rs:116`) loads chunks, scans with `max_count: None` and offset
  0, attaches the coverage gap, then pages. `grep_repo` (`:254`) does the same for
  `codewiki_facts/text.rs::grep_with`.
- `load_indexed_chunks` (`:267`) filters each row with
  `scope::current_indexed_path_is_valid` (`:380`), which is `indexed_file_exists` plus
  the disk check. It stops at `GREP_SQL_SAFETY_LIMIT` (100,000) loaded chunks.
- A scan with no `max_count` keeps every match, so `matched_lines` equals the number of
  matches (`grep_chunks_with_filters`).
- `GrepResponse` prints `scanned_chunks` and `matched_lines` in JSON, so the node
  corrects both.
- The safety limit counts the chunks the hub loads. When indexed files are missing on
  the node's disk, the hub can reach the limit with chunks the node then drops. That
  happens only on an index that freshness did not refresh (`--allow-stale` or a busy
  lock), and the response reports `truncated`.
- Rejected alternative: returning the chunks for the node to scan, which would send
  up to 100,000 chunk bodies in each response where the hub's scan sends only the
  matches.
- A broad pattern can still return a `GrepCandidates` body over 10 MiB. 1.8's client
  decodes 2xx bodies with `into_json`, which has no size cap.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(grep)'`;
  - `cargo nextest run -p gobby-codeindex`.

**Granularity:** one command, with its library split, route and client. The
hand-maintained Target files exceed six because each route adds one file per layer.

Consumers unchanged:
- `crates/gcode/src/dispatch_navigation.rs` — no-edit-reason: calls `grep::run` with the same arguments.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.4.1 - Local `grep` output (matches, `-l`, context lines and JSON counts) is byte-identical before and after the split, including a match in a file deleted on disk after indexing. test: `crates/gcode/src/node/grep_tests.rs::grep_split_matches_the_cli_output`.
- 2.4.2 - Under the remote golden grant, `gcode grep` prints the page of the returned matches whose files exist on the node, with `scanned_chunks` and `matched_lines` excluding files missing there. test: `crates/gcode/src/node/grep_tests.rs::node_grep_pages_and_corrects_counts`.
- 2.4.3 - The grep route serves the principal's machine. test: `crates/gcodeindex/tests/node_grep.rs::grep_route_serves_the_principal_machine`.
- 2.4.4 - Under the remote golden grant, a node `gcode grep` whose `GrepCandidates` reply exceeds 10 MiB prints the page that local paging of the same candidates prints. test: `crates/gcode/src/node/grep_tests.rs::node_grep_reads_a_reply_over_ten_mib`.
- 2.4.5 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.5 Node symbol navigation and symbol-at [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/symbols.rs::*` — scope-reason: `outline`, `symbol`, `kinds` and `tree` split into a fetch and a render, `outline_missing_diagnostic` splits at its disk check, and the inline tests move to `crates/gcode/src/commands/symbols/tests.rs`
- `crates/gcode/src/commands/symbols/tests.rs`
- `crates/gcode/src/commands/symbols/batch.rs::symbols`
- `crates/gcode/src/commands/symbol_at.rs::run`
- `crates/gcode/src/commands/status/repo_outline.rs::repo_outline`
- `crates/gcode/src/commands/scope.rs::ProjectMatch`
- `crates/gcode/src/visibility.rs::VisibleFile`
- `crates/gcode/src/freshness.rs::symbol_slice_is_current`
- `crates/gcode/src/config/context.rs::*` — scope-reason: `ProjectView` gains `grant_bundle()`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/symbols.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/symbols.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/symbols.rs`
- `crates/gcode/src/node/symbols_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/symbols.rs`
- `crates/gcodeindex/tests/node_symbols.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

`outline`, `symbol`, `symbols`, `kinds`, `tree`, `repo-outline` and `symbol-at` read
symbol rows on the hub. Source bytes and file checks stay on the node.

- Each command splits into a database fetch and a render that takes
  `&impl ProjectView` and the fetched rows. The local command runs both.
- Routes:
  - `/symbols/file`: `FileSymbolsRequest { scope, file }` returns
    `FileSymbols { symbols: Vec<Symbol>, indexed: bool, chunks_exist: bool, owner: Option<ProjectMatch> }`.
    That is `visible_symbols_for_file` plus, when the list is empty, the database half
    of `outline_missing_diagnostic`: `indexed_file_exists`, `content_chunks_exist` and
    `other_project_for_path`. `outline` and `symbol-at` use it.
  - `/symbols/ids`: `SymbolIdsRequest { scope, ids }` returns the visible symbols
    (`visible_symbols_by_ids`). `symbol` sends one id, and `symbols` sends its list.
  - `/symbols/kinds` returns `visible_kinds`.
  - `/symbols/tree` returns `visible_tree`'s `Vec<VisibleFile>`. `tree` filters it with
    `filter_tree_files` on the node, and `repo-outline` summarizes it there.
- `outline_missing_diagnostic` becomes a function of `FileSymbols`, the file's disk
  existence and the path. Locally the existence comes from
  `path_exists_in_current_project`, as today. On a node it is computed on the node.
- The node reads source bytes from its own root: `read_symbol_source` for `symbol` and
  `symbols`, and `symbol_at::run`'s file read for `symbol-at`. Savings reports go
  through `savings::report_savings` with `ProjectView::grant_bundle()`, which `Context`
  answers from `grant_ai` and `NodeAccess` from its acquired grant.
- Node symbol freshness mirrors `freshness::ensure_symbol_fresh`. It fetches the
  symbol and checks `symbol_slice_is_current` (made `pub(crate)`) against the node's
  file. When the slice is stale, it runs files freshness (2.2) for that file and
  fetches the symbol again. `outline` and `symbol-at` run files freshness for their
  file, as locally.
- `VisibleFile` and `ProjectMatch` gain `Serialize, Deserialize`.
- `commands/symbols.rs` (930 lines, 395 of them its inline test module) moves that
  module to `crates/gcode/src/commands/symbols/tests.rs`, declared with
  `#[cfg(test)] #[path = "symbols/tests.rs"] mod tests;` (`crates/AGENTS.md`).

Research context:
- `outline` (`commands/symbols.rs:24`) reads `visible_symbols_for_file`, prints
  `outline_missing_diagnostic` for an empty result, and reports savings from the file's
  size on disk. `outline_missing_diagnostic` (`:216`) checks
  `path_exists_in_current_project`, `indexed_file_exists`, `other_project_for_path`
  and `content_chunks_exist`.
- `symbol` (`:291`) reads `visible_symbol_by_id` and the source through
  `read_symbol_source` (`:341`, a `std::fs::read` under the project root).
  `symbols/batch.rs::symbols` reads `visible_symbols_by_ids`. `kinds` and `tree` read
  `visible_kinds` and `visible_tree`. `status/repo_outline.rs::repo_outline` reads
  `visible_tree`.
- `symbol_at::run` (`:71`) reads `visible_symbols_for_file`, then the file's bytes
  (`:88`) to map the line to a byte offset.
- `savings::report_savings(base_url, original, actual, grant)` posts to the daemon,
  and the callers pass `ctx.grant_ai`'s bundle.
- `freshness::ensure_symbol_fresh` (`:198`) refreshes the symbol's file when the stored
  slice hash differs from the bytes on disk.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(symbols) | test(symbol_at)'`;
  - `cargo nextest run -p gobby-codeindex`.

**Granularity:** seven commands over four routes. They share the symbol row fetches
and the `ProjectView` render split, and splitting them would put several leaves on
`commands/symbols.rs` and the same route module.

Consumers unchanged:
- `crates/gcode/src/dispatch_navigation.rs` — no-edit-reason: calls the symbol commands with the same arguments.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.5.1 - Local `outline`, `symbol`, `symbols`, `kinds`, `tree`, `repo-outline` and `symbol-at` output is byte-identical before and after the split, including the missing-outline diagnostic for an unindexed file. test: `crates/gcode/src/node/symbols_tests.rs::symbol_navigation_split_matches_the_cli_output`.
- 2.5.2 - Under the remote golden grant, `gcode symbol` and `gcode symbol-at` print source read from the node's file, and `gcode outline` of a file missing on the node prints the local diagnostic. test: `crates/gcode/src/node/symbols_tests.rs::node_symbol_reads_use_node_source`.
- 2.5.3 - A node `symbol` whose stored slice no longer matches the node's file refreshes that file before printing. test: `crates/gcode/src/node/symbols_tests.rs::node_symbol_freshness_refreshes_a_stale_slice`.
- 2.5.4 - The four routes serve the principal's machine. test: `crates/gcodeindex/tests/node_symbols.rs::symbol_routes_serve_the_principal_machine`.
- 2.5.5 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.6 Node graph navigation [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/reads.rs::*` — scope-reason: `callers`, `callees`, `usages`, `imports`, `path` and `blast_radius` split into a read and a render
- `crates/gcode/src/search/fts/common.rs::ResolvedGraphSymbol`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/graph_nav.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/graph_nav.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/graph_nav.rs`
- `crates/gcode/src/node/graph_nav_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/graph_nav.rs`
- `crates/gcodeindex/tests/node_graph_nav.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

`callers`, `callees`, `usages`, `imports`, `path` and `blast-radius` read Falkor and
PostgreSQL only, so the hub also pages them.

- Each command splits into a read that returns `GraphRead<T>` and a render with the
  existing format functions. The local command runs both.
- `GraphRead<T>` has three cases:
  - `Unavailable { hint }`: the graph backend is off or unreachable
    (`graph_read_unavailable`, `hint_for`);
  - `Unresolved { suggestions }`: the symbol did not resolve;
  - `Found { symbol: ResolvedGraphSymbol, total, results: Vec<T>, hint }`: one page.

  `T` is `GraphResult` for the four list reads and blast radius. `path` returns its
  `GraphPathResponse`, which gains `Deserialize`.
- Routes `/nav/callers`, `/nav/callees`, `/nav/usages`, `/nav/imports`, `/nav/path` and
  `/nav/blast-radius` take the command's arguments with `offset` and `limit`, and
  return `GraphRead<T>`. Their hub context selects Falkor.
- `ResolvedGraphSymbol` gains `Serialize, Deserialize`.
- 2.1 already moved the inline tests out of `commands/graph/reads.rs`, which leaves
  about 685 production lines for this split.
- P6 serves the same reads to `/api/code-index` clients through these hub entry points.

Research context:
- `commands/graph/reads.rs` resolves the symbol with `resolve_symbol_with_connection`
  (`search/fts/graph.rs::resolve_graph_symbol`). It counts and pages with
  `code_graph::{count_*, find_*}` through `read_paged_symbol_graph_results`, and maps an
  unavailable backend to an empty page with a hint (`graph_read_or_empty`). None of it
  reads a file on disk.
- `GraphResult` already derives `Serialize, Deserialize`.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(graph)'`;
  - `cargo nextest run -p gobby-codeindex`.

**Granularity:** six commands over one result model, `GraphRead<T>`, in one module.
Splitting them would put six leaves on `commands/graph/reads.rs`.

Consumers unchanged:
- `crates/gcode/src/dispatch_navigation.rs` — no-edit-reason: calls the graph navigation commands with the same arguments.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.6.1 - Local output of the six commands is byte-identical before and after the split, including the unresolved-symbol suggestions and the unavailable-graph hint. test: `crates/gcode/src/node/graph_nav_tests.rs::graph_navigation_split_matches_the_cli_output`.
- 2.6.2 - Under the remote golden grant, each command prints the page the stubbed hub returns, and an `Unavailable` read prints the local hint. test: `crates/gcode/src/node/graph_nav_tests.rs::node_graph_navigation_renders_hub_reads`.
- 2.6.3 - The six routes serve the principal's machine. test: `crates/gcodeindex/tests/node_graph_nav.rs::graph_navigation_routes_serve_the_principal_machine`.
- 2.6.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.7 Node `gcode graph` reports and views [category: code] (depends: 2.6)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/payload.rs::report`
- `crates/gcode/src/commands/graph/payload.rs::overview`
- `crates/gcode/src/commands/graph/payload.rs::file`
- `crates/gcode/src/commands/graph/payload.rs::neighbors`
- `crates/gcode/src/commands/graph/payload.rs::graph_blast_radius`
- `crates/gcode/src/commands/graph/view/mod.rs::run`
- `crates/gcode/src/commands/graph/view/render.rs::*` — scope-reason: `ViewPayload`, `ViewSeed`, `ViewNode`, `ViewEdge` and `ViewCommunity` gain `Deserialize` and become `pub(crate)`
- `crates/gcode/src/commands/graph/view/fcg.rs::run`
- `crates/gcode/src/commands/graph/view/mcg/fetch.rs::run`
- `crates/gcode/src/commands/graph/view/class_hierarchy/fetch.rs::run`
- `crates/gcode/src/commands/graph/view/communities.rs::run_list`
- `crates/gcode/src/commands/graph/view/communities.rs::run_detail`
- `crates/gcode/src/cli/graph_view.rs::*` — scope-reason: `GraphViewArgs`, `GraphViewSeed` and `GraphViewKind` gain `Serialize, Deserialize`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/graph_views.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/graph_views.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/graph_views.rs`
- `crates/gcode/src/node/graph_views_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/graph_views.rs`
- `crates/gcodeindex/tests/node_graph_views.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

`gcode graph report`, `overview`, `file`, `neighbors`, `blast-radius` and `view` build
a serializable payload and print it. The hub builds the payload and the node prints.

- `report`, `overview`, `file`, `neighbors` and `graph_blast_radius` each split into the
  library call and the print. The routes `/graph/report`, `/graph/overview`,
  `/graph/file`, `/graph/neighbors` and `/graph/blast-radius` return the call's
  `ProjectGraphReport` or `GraphPayload`, and the node prints with
  `format_report_text` or `print_graph_payload`.
- `view`'s sub-runs end in `render::print_view` today:
  - `mcg::run` (`mcg/fetch.rs`), `fcg::run`, `class_hierarchy::run`
    (`class_hierarchy/fetch.rs`), and `communities::run_list` and `run_detail` return
    their `ViewPayload` instead.
  - `view::run` splits into `view::payload(ctx, args) -> ViewPayload` and the print.
  - `/graph/view` takes `GraphViewRequest { scope, args: GraphViewArgs }` and returns
    the payload. The node resolves a file seed against its project root before posting,
    as `dispatch.rs` does today.
- The view types become `pub(crate)` and gain `Deserialize`. `GraphViewArgs`,
  `GraphViewSeed` and `GraphViewKind` gain `Serialize, Deserialize`.
- Each route's hub context selects Falkor, as the local commands do.

Research context:
- `commands/graph/payload.rs` calls `graph::report::generate_report_with_options`,
  `code_graph::project_overview_graph`, `file_graph`, `symbol_neighbors` and
  `blast_radius_graph`. `ProjectGraphReport` and `GraphPayload` already derive
  `Serialize, Deserialize`.
- `view::run` (`commands/graph/view/mod.rs:411`) routes to the five sub-runs, and every
  one builds a `ViewPayload` with `build_view_payload` and prints it with
  `print_view` (`render.rs:307`).
- None of these reads a file on disk.
- `view`'s sub-runs and `report`'s community read take the machine from the context
  after 2.1. `overview`, `file`, `neighbors` and `blast-radius` read Falkor through
  `graph/code_graph/read/graph_payloads.rs`, and `report`'s snapshot loaders
  (`graph/report/loading.rs`) do too. Neither filters rows by version, so both admit
  every version Falkor retains (P3's framing has the evidence). A node prints what a
  local run against the same index prints, including those rows.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(graph)'`;
  - `cargo nextest run -p gobby-codeindex`.

**Granularity:** six subcommands over two payload types. They share the
payload-then-print split and the route module.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.7.1 - Local output of the six subcommands, including every view and seed combination, is byte-identical before and after the payload split. test: `crates/gcode/src/node/graph_views_tests.rs::graph_payload_split_matches_the_cli_output`.
- 2.7.2 - Under the remote golden grant, each subcommand prints the payload the stubbed hub returns, in JSON and text. test: `crates/gcode/src/node/graph_views_tests.rs::node_graph_subcommands_render_hub_payloads`.
- 2.7.3 - The six routes serve the principal's machine. test: `crates/gcodeindex/tests/node_graph_views.rs::graph_routes_serve_the_principal_machine`.
- 2.7.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.8 Node evidence [category: code] (depends: 2.7)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/evidence.rs::*` — scope-reason: `run` takes the facts and hybrid search from the caller, and the node supplies `NodeEvidenceFacts` and `NodeHybridSearch`
- `crates/gcode/src/evidence/mod.rs::FactPage`
- `crates/gcode/src/vector/code_symbols/search.rs::*` — scope-reason: `audited_semantic_search` splits into its query embedding and a vector search
- `crates/gcode/src/codewiki_facts/mod.rs::*` — scope-reason: adds `CodewikiFacts::for_hub`
- `crates/gcode/src/codewiki_facts/scope.rs::*` — scope-reason: `ScopeSelector`, `FileFact` and the types they contain gain `Serialize, Deserialize`
- `crates/gcode/src/codewiki_facts/search.rs::*` — scope-reason: `ContentFact` and `SearchQuery` gain `Serialize, Deserialize`
- `crates/gcode/src/codewiki_facts/symbols.rs::*` — scope-reason: `SymbolFact` gains `Serialize, Deserialize`
- `crates/gcode/src/codewiki_facts/text.rs::*` — scope-reason: `GrepQuery`, `GrepOutcome` and the hit types gain `Serialize, Deserialize`
- `crates/gcode/src/codewiki_facts/graph.rs::*` — scope-reason: `ScopedGraph` and the types it contains gain `Serialize, Deserialize`
- `crates/gcode/src/codewiki_facts/graph_query.rs::*` — scope-reason: `GraphEdgeKind`, `GraphBounds` and `GraphScopeMode` gain `Serialize, Deserialize`
- `crates/gcode/src/codewiki_facts/communities.rs::*` — scope-reason: `ProjectCommunities` and the types it contains gain `Serialize, Deserialize`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/evidence.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/evidence.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/evidence.rs`
- `crates/gcode/src/node/evidence_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/evidence.rs`
- `crates/gcodeindex/tests/node_evidence.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

`gcode evidence` builds an `EvidenceLibrary` over two seams: `EvidenceFacts`, which
reads index facts, and `HybridSearch`, which runs the audited semantic search. Git and
the working tree stay on the node. The node supplies implementations of both seams
that forward to one route.

- `commands/evidence.rs::run` splits. `run_with(&impl ProjectView, prepared, facts, hybrid, output_debug_files)`
  builds the library over `Arc<dyn EvidenceFacts>`, adds the hybrid search only for a
  hybrid request, queries and prints. Local `run` runs `ensure_fresh` and calls it with
  `CodewikiFacts::from_context` and `NativeHybridSearch`, as today. The node client
  runs project freshness (2.2) and calls it with `NodeEvidenceFacts` and
  `NodeHybridSearch`.
- Route `/api/code-index/node/evidence/facts`: `FactsRequest { scope, call: FactsCall }`.
  `FactsCall` has one case per seam method:
  - `EvidenceFacts`: `Files`, `SearchSymbols(SearchQuery)`, `SearchContent(SearchQuery)`,
    `Grep(GrepQuery)`, `SymbolsForFile(path)`, `SymbolById(id)`,
    `GraphEdges { seed, kind, bounds, mode }` and `ProjectCommunities`;
  - `HybridSearch`: `EffectiveIdentity` and
    `SearchSymbolIds { selector, query_vector: QueryVector }`.

  `project_id` needs no call, because the node has it.
- The hub runs `CodewikiFacts::from_context` and `NativeHybridSearch` over its context
  for the call, with the services `evidence::service_config_selection` selects for the
  request. It returns `FactsReply`, with one success case per call and
  `Failed { message }`. The node turns `Failed` back into the seam's error type with
  the same text, so `EvidenceLibrary` reports the error it reports locally. No new
  error code enters the contract.
- Hybrid search:
  - `NodeHybridSearch::effective_identity` returns the hub's `HybridIdentity`.
  - `search_symbol_ids` embeds the selector's query with 2.3's
    `daemon_query_embedding`. It requires the returned model and dimension to equal the
    identity's, else it returns the identity-mismatch error text
    `audited_query_embedding` uses. It then posts `SearchSymbolIds`.
  - The hub runs the new `audited_semantic_search_with_vector`, the vector half of
    `audited_semantic_search`.
  - The node cannot compare the endpoint, because until P4 the query embedding is
    relayed to the hub.
- `CodewikiFacts` gains `for_hub(ctx)`, whose `grep_with` calls 2.4's
  `grep_repo_with` with the accept-all predicate. `from_context` keeps the disk check.
  The hub builds its facts with `for_hub`. Project freshness on the node removes
  deleted files from the index, but it skips on a busy lock (2.2's `SkippedBusy`).
  `NodeEvidenceFacts::grep` therefore drops each hit whose file
  `path_exists_in_current_project` rejects over `NodeAccess`, as 2.4's node half does.
- `FactPage` and the codewiki fact types in Targets gain `Serialize, Deserialize`.
  `SearchSelector` and `HybridIdentity` already derive both.

Research context:
- `commands/evidence.rs::run` (`:136`) runs `ensure_fresh` for the project, builds
  `EvidenceLibrary::new(&ctx.project_root, binding, Arc<CodewikiFacts>)`, adds the
  working tree unless the request is commit-bound, and adds `NativeHybridSearch` for a
  hybrid search.
- `EvidenceFacts` (`evidence/mod.rs:189`) has nine methods: `project_id`, `files`,
  `search_symbols`, `search_content`, `grep`, `symbols_for_file`, `symbol_by_id`,
  `graph_edges` and `project_communities`. `HybridSearch` (`:265`) has
  `effective_identity` and `search_symbol_ids`.
- `NativeHybridSearch::resolve_identity` (`commands/evidence.rs:185`) reads the
  embedding, vector dimension and Qdrant collection schema. `search_symbol_ids` calls
  `audited_semantic_search` with that identity.
- The only codewiki fact that reads disk is `grep_with`, through `grep_repo`
  (`codewiki_facts/text.rs:142`).
- `preflight` (`:26`) reads the git binding on the node and rejects `--allow-stale`.
- Rejected alternative: one route per seam method. Every method takes and returns
  types this leaf serializes anyway, and one enum keeps the hub dispatch in one
  place.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(evidence) | test(codewiki)'`;
  - `cargo nextest run -p gobby-codeindex`.

**Granularity:** one command and one route over two seams. The fact types' serde,
the forwarding seam implementations and the hub dispatch are one wire contract.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.8.1 - Local `gcode evidence` output is byte-identical before and after `run` takes its seams from the caller, for a lookup, a content search, a grep, a graph walk and a hybrid search. test: `crates/gcode/src/node/evidence_tests.rs::evidence_seam_injection_matches_the_cli_output`.
- 2.8.2 - Under the remote golden grant, `gcode evidence` answers each request through `/evidence/facts` with the node's git binding and working tree, and prints what a local run against the same index prints. test: `crates/gcode/src/node/evidence_tests.rs::node_evidence_forwards_facts_to_the_hub`.
- 2.8.3 - A node hybrid search whose query embedding's model or dimension differs from the hub's identity fails with the local identity-mismatch error, and a `Failed` reply surfaces as the local fact error. test: `crates/gcode/src/node/evidence_tests.rs::node_evidence_keeps_local_errors`.
- 2.8.4 - Every `FactsCall` case round-trips through serde_json, and the route serves the principal's machine. test: `crates/gcodeindex/tests/node_evidence.rs::evidence_route_serves_every_facts_call`.
- 2.8.5 - Under the remote golden grant, an evidence grep omits a hit whose file is missing on the node, as a local run does. test: `crates/gcode/src/node/evidence_tests.rs::node_evidence_grep_drops_files_missing_on_the_node`.
- 2.8.6 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.9 E2E: node read commands through the hub routes [category: test] (depends: 2.8, 1.11)
`kind: deliverable`

Targets:
- `tests/e2e/test_hub_node_pair.py::*` — scope-reason: adds the node read-parity test beside 1.11's preflight test

Release ownership (Constraints): test files only, so no binary bumps.

On 1.11's indexed node overlay, run every P2 read command with the node's `gcode`
against the node front door, then edit a file on the node and check that freshness
reindexes it.

Research context:
- 1.11 leaves an enrolled node with an indexed overlay and parent.
- Planned check: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_pair.py -v -m e2e`.

**Acceptance:**

- 2.9.1 - On an enrolled node, `search`, `search-symbol`, `search-text`, `grep`, `outline`, `symbol`, `symbols`, `kinds`, `tree`, `repo-outline`, `symbol-at`, `callers`, `graph report` and `evidence` exit 0 with results, and a file edited on the node shows up in `search-text` without a manual `index`. test: `tests/e2e/test_hub_node_pair.py::test_node_read_commands_through_hub`.

## P3: Projection sync per machine
`kind: framing`

D6 and D11 key graph and vector projections by content hash. The keys already
include it, so P3 adds no key migration:

- A Qdrant point's id is its symbol's id,
  `uuid5(project:path:file_content_hash:name:kind:byte_start)`
  (`crates/gcode/src/models.rs:169`). `vector/code_symbols/lifecycle.rs::sync_file_symbols`
  only upserts.
- Falkor `CodeSymbol` nodes carry `file_content_hash`, and every `DEFINES`, `IMPORTS`,
  `CALLS` and inheritance edge carries `content_hash`.
  `graph/code_graph/write/deletion.rs::delete_stale_file_graph_queries` deletes only
  rows of the synced hash that carry an older sync token.
- The sync flags are per version, on `code_indexed_files (project, path, content_hash)`.
  `code_indexed_file_states` is each machine's selector.

D11's deletion rule already holds, so P3 changes no deletion:

- Orphan cleanup keeps every path any machine has indexed.
  `commands/graph/lifecycle.rs::cleanup_deleted_project_graph` and
  `commands/vector.rs::cleanup_orphans` read `db::list_all_machine_indexed_file_paths`.
- Content GC (`status/content_gc.rs::discover_content_gc`) skips every version that
  any machine's selector names. It reads the local machine only to find the hub
  checkout's root for its git-history check.
- The `CodeFile` node is one per path, and its `symbol_count` is the last writer's.
  Nothing reads that property: `project_overview_files_query` counts `DEFINES` edges.

What remains is selection. `read_graph_file_facts`, the projection `mark_*` writers
and `fetch_symbols_where` read the process machine. P4's node vector sync and P8's
sync worker project for other machines, so 3.1 moves that read to the caller.

Read-side admission is #23834's under D11. Its acceptance B4.1 admits only versions
named by pins or owned selectors. Its Targets omitted two read paths, and its research
said projections "hold only a source project's current per-path version":

- Falkor keeps every version until content GC retires it, and two read paths filter
  no rows by version: `graph/code_graph/read/graph_payloads.rs` (`gcode graph
  overview`, `file`, `neighbors` and `blast-radius`) and `graph/report/loading.rs`
  (`gcode graph report`).
- On the hub on 2026-10-17,
  `gcode graph file --file crates/gcode/src/daemon.rs --format json` returned 142
  nodes, and 37 symbol names appeared more than once.

The writer reported the gap to #15977 on 2026-10-17. #15977 extended #23834's Targets
to both read paths (option a), so P3 adds no leaf for it.

### 3.1 Machine-explicit projection selection [category: refactor] (depends: 2.8, #23834)
`kind: deliverable`

Targets:
- `crates/gcode/src/db/queries.rs::read_graph_file_facts`
- `crates/gcode/src/db/queries.rs::mark_graph_sync_attempted`
- `crates/gcode/src/db/queries.rs::mark_graph_synced`
- `crates/gcode/src/db/queries.rs::mark_vector_sync_attempted`
- `crates/gcode/src/db/queries.rs::mark_vectors_synced`
- `crates/gcode/src/db/queries.rs::mark_project_vector_sync_attempted`
- `crates/gcode/src/db/queries.rs::mark_project_vectors_synced`
- `crates/gcode/src/vector/code_symbols/repository.rs::fetch_symbols_for_file`
- `crates/gcode/src/vector/code_symbols/repository.rs::fetch_symbols_for_project`
- `crates/gcode/src/vector/code_symbols/repository.rs::fetch_symbols_where`
- `crates/gcode/src/projection/sync.rs::sync_graph_file`
- `crates/gcode/src/projection/sync.rs::sync_file`
- `crates/gcode/src/commands/graph/lifecycle.rs::sync_file_graph`
- `crates/gcode/src/commands/graph/lifecycle.rs::rebuild_project_graph`
- `crates/gcode/src/commands/vector.rs::sync_file`
- `crates/gcode/src/commands/vector.rs::rebuild`
- `crates/gcode/src/db/queries_cas_tests.rs::*` — scope-reason: the callers pass the fixture machine; adds the foreign-machine graph and project-vector tests
- `crates/gcode/src/index/api_tests.rs::*` — scope-reason: the `mark_*` callers pass the fixture machine
- `crates/gcode/src/projection/sync/tests.rs::*` — scope-reason: adds the two-machine vector projection test
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

`Context` carries `machine_id` from 1.1, but the projection paths still read the
process machine with `gobby_core::machine::read_local_machine_id()`. This leaf moves
that read to the caller. Local behavior does not change.

- These take a `machine_id: &str` parameter:
  - the graph reader and writers: `read_graph_file_facts`, `mark_graph_sync_attempted`
    and `mark_graph_synced`;
  - the vector writers: `mark_vector_sync_attempted`, `mark_vectors_synced`,
    `mark_project_vector_sync_attempted` and `mark_project_vectors_synced`;
  - the symbol fetches: `fetch_symbols_for_file`, `fetch_symbols_for_project` and
    `fetch_symbols_where`.
- Every caller passes `ctx.machine_id`:
  - `projection/sync.rs::sync_graph_file` and `VectorProjectionState::sync_file`;
  - `commands/graph/lifecycle.rs::sync_file_graph` and `rebuild_project_graph`;
  - `commands/vector.rs::sync_file` and `rebuild`.
- #23834 lands first and adds exact-version variants of `read_graph_file_facts` and
  `fetch_symbols_for_file`. They take a content hash and read no selector, so they
  take no machine.
- #23578 adds `commands/graph/lifecycle.rs::complete_graph_sync`, which calls
  `mark_graph_synced`. Whichever of #23578 and this leaf lands second passes the
  machine through it.

Research context:
- The process-machine reads are at `db/queries.rs:93`, `:123`, `:153`, `:247`, `:277`,
  `:323` and `:341`, and at `vector/code_symbols/repository.rs:95`
  (`gcode grep -n -w read_local_machine_id crates/gcode/src`).
- Callers:
  `gcode grep -E "\b(fetch_symbols_for_(file|project)|read_graph_file_facts|mark_(graph_sync_attempted|graph_synced|vector_sync_attempted|vectors_synced|project_vector_sync_attempted|project_vectors_synced))\(" crates/`
  finds the six production callers above and the tests in `db/queries_cas_tests.rs`
  and `index/api_tests.rs`. No caller exists under `crates/gcode/tests` or
  `crates/gcode/src/cli`.
- Each function joins `code_indexed_file_states` on the machine. `mark_graph_synced`
  uses it in both its compare-and-set and its dirty branch (`db/queries.rs:146`). Two
  machines that select the same hash share one `code_indexed_files` row, so one
  projection serves both.
- CI has no FalkorDB lane (#21045). Graph coexistence rests on
  `delete_stale_file_graph_queries`, which scopes every delete to the synced hash.
  `content_version_delete_is_project_path_and_hash_scoped` and
  `plan_sync_batches_merges_inheritance_with_content_hash_and_sync_token`
  (`graph/code_graph/tests.rs`) already cover that scoping. This leaf's graph test
  stops at PostgreSQL selection and marking.
- `projection/sync/tests.rs::vector_sync_file_skips_when_state_moves_during_upsert`
  serves a fake Qdrant over a `TcpListener` (`serve_until_upsert`) and seeds versions
  with `seed_version`. The vector test reuses both.
- Rejected alternative: a `Context` parameter on the `db` functions. 1.1 gave the
  `db` readers a `machine_id: &str`, and these follow it.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(cas) | test(projection) | test(api_tests)'`;
  - `cargo clippy -p gobby-code`.

**Granularity:** one outcome, the machine source for projection selection. Five
production files and the pin file change in one commit, because the signatures
and their callers cannot compile apart.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 3.1.1 - With version A selected by the local machine and version B by a foreign machine at one path, `read_graph_file_facts` returns each machine's own version's facts, and `mark_graph_sync_attempted` then `mark_graph_synced` under the foreign machine set B's `graph_synced` and leave A's flag and attempt unchanged. test: `crates/gcode/src/db/queries_cas_tests.rs::graph_sync_selects_the_context_machine_version`.
- 3.1.2 - Under the same fixture, `fetch_symbols_for_project` under the foreign machine returns B's symbols and not A's, and `mark_project_vector_sync_attempted` and `mark_project_vectors_synced` under the foreign machine mark B's row and leave A's unchanged. test: `crates/gcode/src/db/queries_cas_tests.rs::project_vector_reads_and_marks_select_the_context_machine`.
- 3.1.3 - Against the fake Qdrant, the local machine's context projects A and a foreign machine's context projects B of the same path through `VectorProjectionState::sync_file`. Both rows end `vectors_synced`, the two upserts carry A's and B's symbol ids, and neither run sends a delete. test: `crates/gcode/src/projection/sync/tests.rs::vector_sync_projects_each_machine_version`.
- 3.1.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P4: Node-local embedding
`kind: framing`

D12 and D13 move a node's embedding onto the node. P4 has three leaves:

- 4.1: the node's gdaemon serves `/api/embeddings` from an endpoint named in the
  node's bootstrap, with the hub's model, dimension and query prefix.
- 4.2: hub routes that hand a node its pending vector work and apply the vectors it
  returns, and `gcode vector sync-node`, which drives them.
- 4.3: a roots route and a node heartbeat job that runs `gcode vector sync-node` for
  each of the node's projects with pending work.

Where the endpoint comes from (#15977, ruling E4(a), 2026-10-17):

- `/api/config/effective` serves `ai.embeddings.model`, `dim`, `query_prefix`,
  `routing` and `timeout_seconds`. It never serves `api_base`, which is registered
  `machineExport: false`, or the secret `api_key`.
- The node's endpoint is therefore machine-local: an `embeddings: {api_base, api_key}`
  block in the node's 0600 `bootstrap.yaml`.
- A node without the block keeps relaying `/api/embeddings` to the hub. Its query
  embedding and its vectors then come from the hub's endpoint, and 4.3's job still
  projects its versions.

Wire names. Each leaf appends its paths to `gobby_code::node::ROUTE_PATHS`, and the
`vector` group follows P2's file layout. Every route is `POST` with a JSON body:

- `/api/code-index/node/vector/pending` and `/api/code-index/node/vector/apply` (4.2)
  verify the grant as every node route does (1.9, steps 1 to 5).
- `/api/code-index/node/vector/roots` (4.3) takes no grant (D4, ruling E5(a)).

Three checks keep a node's vectors in the hub's vector space:

1. 4.1 returns 503 `embedding_identity_mismatch` for a vector whose length differs
   from the hub's served `ai.embeddings.dim`, and for a request whose model differs
   from the hub's model.
2. 4.2's apply returns 409 `embedding_identity_mismatch` for a model other than the
   hub context's embedding model, and for a dimension other than
   `code_vectors.vector_dim` or the collection's size.
3. 2.3's search already skips the semantic lane for a query vector of another
   identity.

Retry cost. gcore's AI client retries a 5xx twice (`MAX_RETRIES` 2, `BASE_BACKOFF`
250 ms), so each refused call costs about 0.75 s before gcode sees the error. #15977
accepted that cost on 2026-10-17. A misconfigured node's sync run stops at its first
refused batch, so it pays the cost once per project per tick.

Release ownership (Constraints): 4.1 changes gcore, so it bumps `gcode`, `gdaemon` and
`ghook`. 4.2 and 4.3 bump `gcode` and `gdaemon`.

Ordering. The P4 leaves form one chain, 4.1 to 4.3, after 3.1. They share the release
files, `serve.rs`, the `node` module files and gcodeindex's `lib.rs`. P4 depends only
on P1 and P3 (D14), and 4.1's edge on 3.1 is the release chain.

### 4.1 Node-local `/api/embeddings` [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: `HubDatabaseBootstrap` gains a parsed, redacted `embeddings` block, and the inline tests move to `crates/gcore/src/bootstrap/tests.rs`
- `crates/gcore/src/bootstrap/tests.rs`
- `crates/gcore/src/ai/effective_config.rs::parse_served_body`
- `crates/gcore/src/ai/effective_config.rs::EFFECTIVE_CONFIG_SHARE_TTL`
- `crates/gdaemon/src/nodes/embeddings.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares `pub mod embeddings`
- `crates/gdaemon/src/nodes/hooks/ingress.rs::has_node_key`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: `FrontDoor::handle`'s node arm sends `POST /api/embeddings` to `nodes::embeddings`
- `crates/gdaemon/src/state.rs::*` — scope-reason: `NodeServices` gains `embeddings`, which `prepare` builds from the bootstrap block
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the fallback `HubDatabaseBootstrap` literal sets `embeddings: None`
- `crates/gdaemon/tests/node_embeddings.rs`
- `docs/guides/configuration.md`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): this leaf changes gcore, so it bumps `gdaemon`, `gcode`
and `ghook` by 0.0.1. `gclient` and `gterm` change identity too. They are promoted
separately and this leaf does not release them; the promotion guard requires their
bump at their next release.

Bootstrap (`gobby_core::bootstrap`):

- `HubDatabaseBootstrap` gains `embeddings: Option<NodeEmbeddingEndpoint>`, where
  `NodeEmbeddingEndpoint { api_base: String, api_key: Option<String> }`. It is read
  from:

  ```yaml
  embeddings:
    api_base: "http://127.0.0.1:1234/v1"
    api_key: "<optional>"
  ```

- `parse_hub_database_bootstrap` requires a mapping with a non-empty `api_base`
  string and an optional `api_key` string. Only a `datastore_mode: remote` bootstrap
  may carry the block. Any other mode returns an error that names `embeddings`.
- `Debug` redacts the block's `api_key`, as it redacts the bootstrap's own `api_key`.
- `bootstrap.rs` is 976 lines including its inline tests, which start at `:413`. The
  tests move unchanged to `crates/gcore/src/bootstrap/tests.rs`, and the block's tests
  join them there.

Node services:

- `NodeServices` gains `embeddings: Option<NodeEmbeddings>`. `prepare` builds it when
  the bootstrap has the block.
- `NodeEmbeddings` holds the endpoint, the hub's served config with its fetch time,
  and a `reqwest::blocking::Client` built on first use inside `spawn_blocking`.
- The cache lives on `NodeServices`. A process-wide cache would be shared by the
  nodes that `crates/gdaemon/tests/nodes.rs` runs in one process.

`nodes::embeddings::serve(node, request)`:

1. `FrontDoor::handle`'s node arm sends `POST /api/embeddings` here, next to the hook
   ingress branch.
2. Without the block, or when the bearer is not the node key, it relays the request
   unchanged, as today. The key check is `nodes/hooks/ingress.rs::has_node_key`, now
   `pub(crate)`.
3. It reads the body with the hook ingress bounds (16 MiB, 10 s) and parses Python's
   `EmbeddingsPayload`:
   - `input`, a string or a list of strings;
   - `is_query`, default false;
   - optional `model`, `project_id` and `provider`.

   `project_id` and `provider` are ignored, as Python ignores them. A malformed body
   returns 400 with a `detail` message.
4. It reads the hub's config at most once every 30 s, gcore's
   `EFFECTIVE_CONFIG_SHARE_TTL`, which becomes `pub`:
   - It sends `GET /api/config/effective` through `node.relay` with the node key
     (`gobby_core::local_token::read_api_key_at`) as bearer. `HubRelay::forward`
     passes the caller's headers through, so gdaemon sets the bearer itself.
   - It parses the body with `gobby_core::ai::effective_config::parse_served_body`,
     which becomes `pub`.
   - A failed fetch returns 503 `embedding_unavailable`, and the next request fetches
     again.
5. A request `model` other than the served `ai.embeddings.model` returns 503
   `embedding_identity_mismatch`. gcode sends its binding's model, and on a node that
   is the served model, so ordinary requests match.
6. It builds the endpoint config the way
   `crates/gcode/src/vector/code_symbols/embedding.rs::resolve_embedding_ai_context`
   overrides a binding:
   1. `gobby_core::config::resolve_capability_binding(served, AiCapability::Embed)`
      gives the binding.
   2. The block's `api_base` and `api_key` are set on it.
   3. `resolve_embedding_config_from_binding` returns the config with the served model,
      query prefix and timeout (default 10 s).
7. It ports Python's text prefix and model rules exactly (`src/gobby/ai/embeddings.py`):
   - `prefixed_input(text, is_query, model, query_prefix)` follows `_apply_prefix`:
     - a query with a query prefix gets that prefix with no separator;
     - otherwise, a model whose lowercase name contains `nomic` gets
       `search_query: ` or `search_document: `;
     - otherwise the text is unchanged.

     `model` is the served model before stripping, as in Python.
   - `wire_model(model)` follows `_strip_local_embedding_prefix`. It strips a leading
     `local:lm-studio/` or `local:ollama/`.

   gcode's direct path prefixes as `"{prefix} {text}"`, which differs, so it is not
   reused. Both functions are `pub`, and `nodes/mod.rs` declares `pub mod embeddings`,
   because 4.1.5's integration test sees only the library's pub API.
8. It runs `gobby_core::ai::embeddings::embed_batch` under `spawn_blocking`. An
   endpoint error, a non-2xx response or a transport failure returns 503
   `embedding_unavailable`, with the error text in `detail`.
9. Every vector's length must equal the served `ai.embeddings.dim`. A different length
   returns 503 `embedding_identity_mismatch`. A served config with no dimension returns
   503 `embedding_unavailable`, because the node cannot check identity without it.
10. Success returns Python's shape: `{embeddings, model: <served model>, dim: <served dim>}`.

Error bodies are `{"code": ..., "detail": ...}`. gdaemon enables gcore's `ai` feature.
`reqwest` is already locked, so no crate enters `Cargo.lock`.

`docs/guides/configuration.md`'s node section documents the block, where the model,
dimension and query prefix come from, and the relay when the block is absent.

Research context:
- gcode always embeds through the daemon:
  - `embedding.rs::embedding_source_from_resolved_ai_context` returns
    `AiRouting::Daemon`.
  - gcore's `embed_via_daemon` (`crates/gcore/src/ai/daemon/operations.rs:354`) posts
    `/api/embeddings` with `{input, is_query, project_id, provider, model}` and the
    local API key, which on a node is the node key. It parses `{embeddings, model, dim}`.
- `FrontDoor::handle` (`front_door/mod.rs:99`) handles `POST /api/hooks/execute` and the
  handshake challenge itself on a node, and relays everything else.
  `nodes/hooks/ingress.rs::execute` relays when `has_node_key` (`:109`) fails.
  `has_node_key` compares SHA-256 digests of the bearer and
  `local_token::read_api_key_at` in constant time.
- The Python route (`src/gobby/servers/routes/embeddings.py:77`) returns
  `{embeddings, model: payload.model or config model, dim: config dim}`.
  `_apply_prefix` (`src/gobby/ai/embeddings.py:166`) and
  `_strip_local_embedding_prefix` (`:65`) are the rules this leaf ports, and
  `tests/search/test_nomic_prefix.py` holds their cases.
- `/api/config/effective` (`src/gobby/servers/routes/configuration_effective.py`)
  admits any verified front-door identity, including a node key. It serves
  machine-exported keys only.
- `resolve_capability_binding` (`crates/gcore/src/config/resolve.rs:318`) and
  `resolve_embedding_config_from_binding` (`:255`) read `ConfigSource`, which
  `DaemonServedConfig` implements (`crates/gcore/src/config/daemon_source.rs:25`).
- `parse_hub_database_bootstrap` (`bootstrap.rs:215`) reads only the keys it knows.
  `HubDatabaseBootstrap` has one literal outside it, `serve.rs:316`'s fallback.
- Python ignores bootstrap keys it does not know. `gobby auth login` rewrites
  `bootstrap.yaml` from the mapping it read (`src/gobby/cli/auth_login.py`), so the
  block survives re-enrollment.
- Rejected alternatives:
  - an endpoint from the hub's config, which has no node endpoint (E4);
  - `fetch_daemon_served_config_at`, whose `ureq` client cannot pin the hub
    certificate the relay already pins.
- Planned checks:
  - `cargo nextest run -p gobby-daemon --test node_embeddings`;
  - `cargo nextest run -p gobby-core -E 'test(bootstrap)'`;
  - `cargo clippy -p gobby-daemon -p gobby-core`.

**Granularity:** seven acceptance items. The bootstrap block, the node services and
the handler are one route's configuration, state and behavior. Each is untestable
without the others.

Consumers unchanged:
- `crates/gdaemon/tests/run_modes.rs` — no-edit-reason: builds bootstraps through `parse_hub_database_bootstrap`, so the new field needs no literal.
- `src/gobby/cli/auth_login.py` — no-edit-reason: rewrites `bootstrap.yaml` from the mapping it read, so the block survives re-enrollment.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.1.1 - With the block and the node key, a query for a nomic model sends `search_query: <text>` and the stripped model to the endpoint, and returns `{embeddings, model, dim}` with the hub's model and dimension. A second request within 30 s does not fetch the hub config again. test: `crates/gdaemon/tests/node_embeddings.rs::node_embeds_locally_with_the_hub_identity`.
- 4.1.2 - A vector whose length differs from the served dimension, and a request model other than the served model, each return 503 `embedding_identity_mismatch`. test: `crates/gdaemon/tests/node_embeddings.rs::identity_mismatch_returns_503`.
- 4.1.3 - An endpoint 500, an unreachable endpoint and a failed hub config fetch each return 503 `embedding_unavailable`. test: `crates/gdaemon/tests/node_embeddings.rs::endpoint_failure_returns_embedding_unavailable`.
- 4.1.4 - Without the block, or with a bearer other than the node key, the request reaches the hub unchanged. test: `crates/gdaemon/tests/node_embeddings.rs::without_the_block_or_key_the_request_relays`.
- 4.1.5 - `prefixed_input` and `wire_model` return Python's outputs for the cases in `tests/search/test_nomic_prefix.py`, for a query prefix, and for both local model prefixes. test: `crates/gdaemon/tests/node_embeddings.rs::prefix_rules_match_python`.
- 4.1.6 - The block parses in a remote-mode bootstrap, is refused in local mode, and its key prints redacted. test: `crates/gcore/src/bootstrap/tests.rs::node_embeddings_block_parses_only_in_remote_mode`.
- 4.1.7 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.2 Node vector sync routes and `gcode vector sync-node` [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/db/vector_pending.rs`
- `crates/gcode/src/db/mod.rs::*` — scope-reason: declares and re-exports `vector_pending`
- `crates/gcode/src/vector/code_symbols/lifecycle.rs::*` — scope-reason: `points_for_symbols` splits into its embedding and `points_from_vectors`, and `apply_file_vectors` seeds the collection size from a reported dimension
- `crates/gcode/src/vector/code_symbols/embedding.rs::*` — scope-reason: adds `daemon_embeddings`, which 2.3's `daemon_query_embedding` wraps
- `crates/gcode/src/cli.rs::VectorCommand`
- `crates/gcode/src/cli_error.rs::CliError`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/contract.rs::contract`
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: adds the `vector sync-node` entry and its error codes
- `tests/contracts/gcode.contract.json::*` — scope-reason: the test copy gains the same entry and codes
- `crates/gcode/tests/contract.rs::*` — scope-reason: asserts the `vector sync-node` entry and its codes
- `docs/contracts/gcode-cli.md`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/vector.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/vector.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/vector.rs`
- `crates/gcode/src/node/vector_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/vector.rs`
- `crates/gcodeindex/tests/node_vector.rs`
- `docs/reference-audit/code-index.json::*` — scope-reason: adds the `gcode vector sync-node` audit entry
- `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`
- `docs/guides/code-index.md`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

Pending query (`crates/gcode/src/db/vector_pending.rs`, re-exported from `db`):

- `pending_vector_files(conn, project_id, machine_id, limit) -> Vec<String>` returns
  paths. It selects the machine's selected versions (`code_indexed_file_states` joined
  to `code_indexed_files`) where `vectors_synced` is false and no vector attempt falls
  within `VECTOR_SYNC_COOLOFF` (300 s), ordered by `indexed_at`, then path.
- It is Python's `get_pending_sync_files` for one machine. P8's native sync worker
  reuses it.

Wire (`crates/gcode/src/node/vector.rs`):

- `/vector/pending` takes `VectorPendingRequest { scope: NodeScope, limit: usize }`
  and returns `VectorPending { files: Vec<PendingVectorFile> }`, where
  `PendingVectorFile { path, content_hash, attempted_at_us: i64, symbols: Vec<VectorSymbolText> }`
  and `VectorSymbolText { id, text }`. The hub caps `limit` at 50.
- `/vector/apply` takes
  `VectorApplyRequest { scope, path, content_hash, attempted_at_us, model, dim, points: Vec<VectorPoint>, complete: bool, total: usize }`,
  where `VectorPoint { id, vector: Vec<f32> }` and `total` is the file's symbol count.
  It returns `VectorApplyOutcome { upserted: usize, synced: Option<bool> }`.
- `attempted_at_us` is Unix microseconds. `mark_vectors_synced` compares the attempt
  time exactly, and PostgreSQL stores microseconds.

Hub entry points (`crates/gcode/src/node/hub/vector.rs`) build their context through
`HubScope::context` with `ServiceConfigSelection::vectors()`:

- `pending(&HubScope, VectorPendingRequest)`:
  1. Opens a read-write connection and calls `pending_vector_files` for the
     principal's machine.
  2. For each path, `mark_vector_sync_attempted` under that machine (3.1) returns the
     attempt. A `None` drops the path.
  3. The symbols come from #23834's exact-version variant of `fetch_symbols_for_file`,
     for the attempt's hash. Each text comes from `embedding.rs::vector_text_for_symbol`.
- `apply(&HubScope, VectorApplyRequest)`:
  1. A `model` other than the context's `embedding.model`, or a vector whose length is
     not `dim`, returns `embedding_identity_mismatch`.
  2. It reads the exact-version symbols for `(path, content_hash)`. A point id that is
     not among them returns `vector_version_moved`.
  3. It builds the lifecycle as `commands/vector.rs::sync_file` does, then calls
     `apply_file_vectors(dim, &symbols, points)`:
     - When `vector_dim` is unset, it seeds `probed_vector_size` with `dim`, so
       `ensure_collection` never embeds a probe.
     - `ensure_collection` then checks the collection. A size mismatch
       (`DimensionMismatch`) maps to `embedding_identity_mismatch`.
     - `points_from_vectors` builds the points, and `upsert_points` writes them.
  4. With `complete`, `total` must equal the version's symbol count, or the call
     returns `vector_version_moved`. It then runs `mark_vectors_synced` under the
     machine with the attempt, and `synced` reports the compare-and-set.

  The route holds no project lock. The points are keyed by content hash, and the
  compare-and-set fences the flag (D11).
- `points_for_symbols` becomes its embedding plus `points_from_vectors(symbols, vectors)`.
  Local sync still calls `points_for_symbols`, so its output is unchanged.
- Seeding. When `vector_dim` is unset and no collection exists, the collection is
  created at the reported size. The node checked that size against the hub's served
  `ai.embeddings.dim` (4.1), the dimension the hub's Python embedder also enforces.
  Refusing instead would leave a project that only nodes index without vectors until
  the operator sets `ai.embeddings.dim`.
- 1.9's step 7 maps `embedding_identity_mismatch` and `vector_version_moved` to 409.

Node command:

- `VectorCommand::SyncNode` adds `gcode vector sync-node`, with `--format`.
  `dispatch.rs::run` resolves it through `resolve_access`:
  - `Direct` returns `node_access_required`, whose recovery reads "`vector sync-node`
    runs on a node; the hub's own versions sync through the sync worker".
  - `Node` runs `node::client::vector::sync_node(&NodeAccess)`.
- `sync_node`:
  1. Posts pending with `limit` 10, repeating until it returns no files.
  2. For each file, embeds the texts in batches of
     `gobby_core::qdrant::DEFAULT_UPSERT_BATCH_SIZE`, the size local sync uses
     (`VECTOR_SYNC_BATCH_SIZE`). It posts one apply per batch, and the last carries
     `complete`. A file with no symbols posts one empty apply with `complete`.
  3. A 409 `vector_version_moved`, or `synced: false`, counts the file as moved, and
     the run continues.
  4. An embedding failure ends the run with `embedding_unavailable`, carrying the
     daemon's message. Any other route error ends the run with its code (1.8).
  5. Prints `{project_id, files_synced, files_moved, symbols_upserted}`.
- `embedding.rs::daemon_embeddings(project_id, grant, inputs, is_query) -> Result<DaemonEmbeddings>`,
  with `DaemonEmbeddings { vectors, model, dimension }`, resolves the AI context as
  2.3's `daemon_query_embedding` does. `daemon_query_embedding` becomes a one-input
  wrapper over it. `sync_node` passes `is_query` false.
- `sync-node` reads only hub rows, so it runs no freshness check.

Contract and docs:

- `contract()` gains `vector sync-node` (`daemon_consumed: true`, `--format`, and the
  four output keys).
- The codes `node_access_required`, `vector_version_moved`,
  `embedding_identity_mismatch` and `embedding_unavailable` join `contract().error_codes`,
  both contract JSON copies and `docs/contracts/gcode-cli.md`.
- `contract_version` stays as the executor finds it. The additions are additive, as
  #22353's and 1.7's were.
- `docs/reference-audit/code-index.json` gains the `gcode vector sync-node` entry, with
  reference `graphs.md` and implementation `crates/gcode/src/node/client/vector.rs`,
  symbol `sync_node`. `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`
  requires an entry for every native command.
- `graphs.md` and `docs/guides/code-index.md` name `vector sync-node` with the other
  vector commands. They state that it runs on a node and that 4.3's job runs it.

Research context:
- `VectorProjectionState::sync_file` (`projection/sync.rs:646`) marks the attempt, reads
  the symbols, calls `sync_file_symbols`, then runs the `mark_vectors_synced`
  compare-and-set.
- `points_for_symbols` (`vector/code_symbols/lifecycle.rs:321`):
  - embeds `VECTOR_SYNC_BATCH_SIZE` symbols per batch;
  - checks each length against the schema;
  - builds each `UpsertRequest` with `payload_map`.
- `ensure_collection` (`:65`) takes `expected_schema` (`:170`), which uses
  `vector_dim`, else `probed_vector_size`, else embeds a probe.
- `vector_dim` comes from `ai.embeddings.dim` (`config/services.rs:427`). The hub's
  configuration snapshot reads stored `config_store` rows only
  (`config/runtime_contract.rs:28`), so `vector_dim` is unset unless the operator set
  it.
- `mark_vector_sync_attempted` (`db/queries.rs:242`) returns
  `VectorSyncAttempt { content_hash, attempted_at }`. `mark_vectors_synced` (`:270`)
  compares `attempted_at`.
- `embed_via_daemon` sends the binding's model and `is_query`, and parses
  `{embeddings, model, dim}`.
- Python's pending query is `src/gobby/code_index/_storage/files.py:170`, with
  `SYNC_FAILURE_COOLOFF_SECONDS` 300.
- `VectorCommand` is at `cli.rs:523`, and `run` dispatches it at `dispatch.rs:492`.
  `service_config_selection`'s `Command::Vector { .. }` arm already covers the new
  variant.
- The contract's vector entries are at `contract.rs:495-531`, and the audit's at
  `docs/reference-audit/code-index.json:624-680`.
- #23579 and #23580 edit the same contract files and `dispatch.rs` (Constraints).
- `projection/sync/tests.rs::serve_until_upsert` serves a fake Qdrant over a
  `TcpListener`. The apply tests use the same approach.
- Rejected alternatives:
  - the hub embedding node versions with its own endpoint, which D12 moves to the
    node;
  - pending without marking the attempt, which would let two runs embed one file;
  - apply without the attempt, which would drop the compare-and-set's fence.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(vector) | test(contract)'`;
  - `cargo nextest run -p gobby-codeindex`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_reference_library.py -v`.

**Granularity:** nine acceptance items. The routes and the command are one wire
contract's two halves, as in each P2 route leaf, and the contract and audit entries
describe that command.

Consumers unchanged:
- `crates/gcode/src/cli/tests.rs` — no-edit-reason: compares `contract()` command entries with clap, so it checks `vector sync-node` with no edit.
- `crates/gcode/src/cli/tests/top_level.rs` — no-edit-reason: reads the `outline` command entry, which this leaf leaves unchanged.
- `crates/gcode/src/cli/tests/projection.rs` — no-edit-reason: builds the existing `VectorCommand` variants, which do not change.
- `tests/skills/test_reference_library.py` — no-edit-reason: checks that every native command has an audit entry, which this leaf adds.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.2.1 - With version A selected by the local machine and version B by a foreign machine at one path, and C at a second path selected only by the foreign machine, pending under the foreign machine returns B and C with their symbol texts and attempt stamps, honors `limit`, and returns neither again within 300 s. test: `crates/gcode/src/node/vector_tests.rs::pending_claims_the_machine_versions_once_per_cooloff`.
- 4.2.2 - Against a fake Qdrant, a file's apply chunks upsert its points, and the `complete` chunk marks its version `vectors_synced`. A file with no symbols is marked by one empty `complete` chunk. test: `crates/gcode/src/node/vector_tests.rs::apply_upserts_and_marks_the_version`.
- 4.2.3 - A model other than the hub's, a dimension other than `vector_dim`, and a dimension other than an existing collection's size each return `embedding_identity_mismatch` with no upsert. With `vector_dim` unset and no collection, the collection is created at the reported size and no probe is embedded. test: `crates/gcode/src/node/vector_tests.rs::apply_checks_the_embedding_identity`.
- 4.2.4 - A point id outside the version, or a `total` other than its symbol count, returns `vector_version_moved`. A selector that moved after the attempt makes `synced` false. test: `crates/gcode/src/node/vector_tests.rs::apply_refuses_moved_versions`.
- 4.2.5 - Under the remote golden grant, `gcode vector sync-node` against a stub hub and daemon embeds with `is_query` false in batches of `DEFAULT_UPSERT_BATCH_SIZE`, marks each file's last chunk `complete`, counts a moved file and continues, and prints the four counts. Under a direct grant it exits `node_access_required`. test: `crates/gcode/src/node/vector_tests.rs::sync_node_embeds_and_applies_in_batches`.
- 4.2.6 - The pending and apply routes serve the principal's machine. test: `crates/gcodeindex/tests/node_vector.rs::vector_routes_serve_the_principal_machine`.
- 4.2.7 - The contract lists `vector sync-node` and its four codes, and both JSON copies match `contract()`. test: `crates/gcode/tests/contract.rs::contract_lists_vector_sync_node`.
- 4.2.8 - The reference audit covers `gcode vector sync-node`. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.
- 4.2.9 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.3 Node heartbeat vector sync [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/db/vector_pending.rs`
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/vector.rs`
- `crates/gcode/src/node/hub/vector.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node/vector.rs`
- `crates/gcodeindex/tests/node_vector.rs`
- `crates/gdaemon/src/nodes/vector_sync.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares `vector_sync`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `serve_doors` takes the `HeartbeatHost` it starts, the node branch registers the vector sync job, and `serve_with_runtime` passes an empty host
- `crates/gdaemon/tests/nodes.rs::*` — scope-reason: the three `serve_doors` calls pass an empty `HeartbeatHost`
- `crates/gdaemon/tests/node_vector_sync.rs`
- `docs/guides/configuration.md`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

Roots route (`/api/code-index/node/vector/roots`, appended to `ROUTE_PATHS`):

- It takes no grant (D4, ruling E5(a)). gdaemon calls it, and gdaemon holds the node
  key but no grant.
- The handler requires `x-gobby-key-id` and `x-gobby-machine-id`, and returns 403
  `claims_mismatch` when either is missing. The front door strips both from every
  client request and sets them only from a verified API key
  (`front_door/auth.rs::AuthState::authenticate`, `IDENTITY_HEADERS`). The machine is
  therefore the key's, never a value the caller supplied.
- The body is `{}`. The route returns
  `VectorRoots { roots: Vec<VectorRoot { project_id, root_path }> }` from the new
  `db::pending_vector_roots(conn, machine_id)`. That query returns the machine's
  `code_indexed_project_states` rows with at least one file that `pending_vector_files`
  would return, and shares its predicate. It skips a project whose registry row is
  soft-deleted (`projects.deleted_at` set), because 8.2 reconciles that project for
  every machine.
- The response names only the calling machine's project ids and roots. #15977 puts the
  route in the Security Reviewer's scope at plan review.

Heartbeat job (`crates/gdaemon/src/nodes/vector_sync.rs`):

- `serve_doors` takes a `HeartbeatHost` and starts it, instead of building an empty
  one. The node branch of `serve::run` registers the job before passing its host.
  `serve_with_runtime` and the tests pass `HeartbeatHost::new()`.
- `register(host, node, VectorSyncTiming { interval, run_deadline })` registers
  `node_vector_sync`. Production passes 60 s, a multiple of `BASE_CADENCE`, and
  10 minutes. Tests pass shorter values.
- Each tick:
  1. Posts `{}` to the roots route through `node.relay`, with the node key
     (`local_token::read_api_key_at`) as bearer. A failed call fails the tick, which
     the heartbeat host's `report` logs.
  2. Skips roots that are not directories on this machine.
  3. Runs `<home>/bin/gcode vector sync-node --project <root> --format json --quiet`
     for each remaining root, one at a time. `<home>` is the bootstrap's directory, and
     the child gets `GOBBY_HOME=<home>`. The child is a `tokio::process::Command` with
     `kill_on_drop(true)`.
  4. Waits for a run under `tokio::time::timeout(run_deadline, …)`, then kills a run
     that passes it. Its claimed files cool off, and a later tick continues the
     project.
  5. Logs a failed run's stderr line and moves to the next root.
  6. Logs once per process when `<home>/bin/gcode` is absent, and skips the tick.
- Shutdown. `HeartbeatHost::stop` waits `SHUTDOWN_GRACE`, then aborts the tick's
  task. Dropping the aborted future drops the child, and `kill_on_drop` kills it, so
  no `gcode` run outlives its daemon through a restart or cutover. A `std::process`
  child under `spawn_blocking` would survive the abort.
- Heartbeat jobs run one at a time and never overlap (the host's `supervise`), so
  two syncs never run at once on a node. A run can hold the node's host for up to
  `run_deadline` per root. This is the node host's only job: P8's jobs register on the
  hub's host and hand their work to threads (P8 framing).
- A node without an embeddings block still runs the job. Its `/api/embeddings` relays
  to the hub (4.1), so the hub's endpoint embeds the node's versions.

`docs/guides/configuration.md`'s node section describes the job and its cadence.

Research context:
- `serve_doors` (`serve.rs:456`) builds and starts an empty `HeartbeatHost`, commented
  "Strangler ports register here". Its callers are `serve.rs:217` (node),
  `serve_with_runtime` (`:452`) and `crates/gdaemon/tests/nodes.rs:184`, `:812` and
  `:1978`.
- `HeartbeatHost::register` refuses registration after start.
  `BASE_CADENCE` is 5 s.
- `code_indexed_project_states` has primary key `(machine_id, project_id)` and holds
  `root_path` (`baseline.sql:2197`).
- `crates/gdaemon/tests/front_door.rs` covers identity header stripping and setting
  (`valid_key_forwards_identity_headers` at `:444` and `forged()` at `:1433`).
- `crates/gterminals/src/host.rs` resolves its binary as `home.join("bin/gterm")`.
- A `tokio::process::Child` keeps running after it is dropped unless its `Command` set
  `kill_on_drop(true)`. `HeartbeatHost::stop` aborts a job that outlives
  `SHUTDOWN_GRACE`, and the abort only drops the future.
- Rejected alternatives:
  - a grant for the job, which E5(a) rules out and for which gdaemon has no handshake
    client;
  - one job per project, because registration freezes at start.
- Planned checks:
  - `cargo nextest run -p gobby-daemon --test node_vector_sync --test nodes --test heartbeat`;
  - `cargo nextest run -p gobby-codeindex`.

Consumers unchanged:
- `crates/gdaemon/tests/heartbeat.rs` — no-edit-reason: builds its own `HeartbeatHost`, whose API does not change.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.3.1 - With pending files for the local machine and a foreign machine, the roots route under the foreign machine's key headers returns only the foreign machine's project with pending work, and omits a project whose pending files are all cooling off and a soft-deleted project. A request missing either header returns 403 `claims_mismatch`. test: `crates/gcodeindex/tests/node_vector.rs::roots_serve_only_the_key_machine`.
- 4.3.2 - With a stub hub that returns two roots, one missing on disk, and a stub `gcode` script, one tick runs the script once, with `vector sync-node --project <root> --format json --quiet` and `GOBBY_HOME` set, and skips the missing root. test: `crates/gdaemon/tests/node_vector_sync.rs::vector_sync_runs_gcode_for_each_present_root`.
- 4.3.3 - A run past its deadline is killed and the next root still runs, and an absent `gcode` logs once over two ticks. A stub `gcode` that runs `exec sleep` is running when `HeartbeatHost::stop` runs, and within 5 s after `stop` returns its pid is gone or a zombie. test: `crates/gdaemon/tests/node_vector_sync.rs::vector_sync_kills_late_runs_and_tolerates_missing_gcode`.
- 4.3.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P5: `/api/code-index` family seam
`kind: framing`

P5 to P7 port Python's `/api/code-index` routes
(`src/gobby/servers/routes/code_index.py`) into a second gcodeindex family,
`code_index`. P5 builds the family and its first route, P6 adds the graph reads, and
P7 adds graph clear, graph rebuild and invalidate. `POST /api/code-index/prune` moves
to P8 with the pruner it calls (D17).

Who the family serves (D15, ruling E6(a)):

- `claims` holds only for an API-key caller, whose request carries `x-gobby-key-id`.
  The front door sets that header only for a resolved key
  (`crates/gdaemon/src/front_door/auth.rs::AuthState::authenticate`). A node
  machine's key counts too. The family acts as the hub machine for every caller, as
  Python does.
- The web UI's cookie requests (`web/src/hooks/useCodeGraph.ts`), agent bearers,
  break-glass and unauthenticated requests are never claimed and reach Python.

Backend. The family defaults to `Proxy` until P9, as every family with live Python
routes does (1.9 has the invariant). A claimed request runs natively only under
`front_door.routes.code_index: native` or `compare`, and `compare` serves the proxied
response. P5 to P7 tests set `native`. Three things wait for P9's flip:

- recording code_index cases in the parity corpus (`tests/contracts/http/`), as
  gdaemon-runtime-family 6.2 recorded runtime's;
- adding the family to `crates/gdaemon/tests/http_contracts.rs`'s static `FAMILIES`
  list;
- changing the parity ledger rows from `proxied`.

Request order. Every route checks in Python's order, so each failure returns Python's
status and body:

1. the `code_index.enabled` gate (503);
2. checks Python makes before `project_id`: blast radius's exactly-one check (6.1);
3. `project_id`, absent or empty: 400 `{"detail":"project_id is required"}`;
4. the `code_index.graph_enabled` gate, for graph reads, clear and rebuild: 503
   `{"detail":"Code graph not available"}`;
5. the hub machine's project row, where Python reads it: 404
   `{"detail":"Code index project not found: <id>"}`;
6. the argument checks Python's gateway makes (400 `Invalid <param>: …`);
7. the library call. Any error returns 500 `{"detail":"Code graph request failed"}`
   and is logged to gdaemon's stderr, unless a route states otherwise.

Gates. 5.1's shared code reads both keys on every request from Python's published
active state: `gobby_runtime::config::active_state(pool,
&gobby_core::grant::deployment_token(home))`, then
`document.active_values["code_index.enabled"]` and `["code_index.graph_enabled"]`.

- A key absent from `active_values` takes its contract default, `true`
  (`crates/gcore/assets/config/runtime_config_contract.json`).
- `gruntime::config::machine_values` is not used. It drops both keys, which are
  `machineExport: false`.
- Python reads `startup_config.code_index` once at startup
  (`src/gobby/runner_init/services.py::_init_code_indexer`). Both keys are
  `activation: live`, so the active state holds the value Python serves after a live
  change.
- With no published state the route returns its `enabled` 503 body. A failed read
  returns the route's 500.

The 503 bodies differ by route:

- `GET /graph/search`: 503 `{"detail":"Code indexer not available"}` when `enabled` is
  false. It does not check `graph_enabled`.
- The graph reads: 503 `{"detail":"Code graph not available"}` for either gate.
- Clear and rebuild: `Code indexer not available` for `enabled` and `Code graph not
  available` for `graph_enabled`.
- Invalidate: 503 `{"error":"Code indexer not available"}`, with no `detail` envelope.
  It does not check `graph_enabled`.

Project row. The hub machine id comes from
`gobby_core::machine::read_machine_id_from_home(home)`, read per request. The row is
`code_indexed_project_states` for that machine and the request's project, read on the
shared pool. A `project_id` that is not a UUID matches no row.

Query parameters follow FastAPI's rules:

- a missing required parameter returns 422 `missing`;
- a value that is not an integer returns 422 `int_parsing`;
- `max_depth` below 1 returns 422 `greater_than_equal`.

The 422 bodies equal the Python route's byte for byte. The executor records them once
from the Python route under `TestClient` into the test. Python passes some integers
through to gcode or SQL, which rejects them: a negative `limit` or `depth`, or a value
above `usize`. Those return 500.

Deliberate differences from Python. The P9 corpus records none of these cases.

- No subprocess. These Python outcomes have no native counterpart:
  - 503 for a missing or old `gcode` binary;
  - 500 for a gcode timeout, including a rebuild past the gateway's rebuild timeout;
  - 404 `gcode exited N: <stderr>` when gcode cannot resolve the project root.

  The only native 404 is the missing project row.
- No freshness prelude. Python runs gcode without `--allow-stale`, so each read first
  runs gcode's freshness check on the hub's checkout and may index changed files
  inside the request (`crates/gcode/src/dispatch.rs::ensure_project_fresh`). Native
  reads serve the current index, as `--allow-stale` does. The hub's checkout is
  indexed by gcode runs on the hub, by the edit trigger (Python's until #24151 calls
  8.7's) and by the maintenance pass (8.2).
- A `project_id` that is not a UUID gets the 404 body. Python returns 500 from the
  failed cast on reads, and 400 or 404 from gcode on clear.
- Search applies visibility inside the query (5.1), so invisible hits never shorten a
  page. Python fetches a multiple of the limit, then filters.
- A `./`-prefixed file path resolves against the checkout root (6.1). Python's gcode
  resolves it against the daemon's working directory.
- Invalidate also deletes the hub machine's `code_communities` rows, as
  `gcode invalidate` does (7.2).

File layout. Each route group `<group>` adds:

- `crates/gcodeindex/src/family/<group>.rs` for the handlers, declared in
  `crates/gcodeindex/src/family.rs`, which lists the group's rows in `NATIVE_ROUTES`;
- `crates/gcode/src/node/hub/family_<group>.rs` for any gcode entry point the group
  needs beyond P2's, declared in `node/hub.rs`;
- `crates/gcodeindex/tests/family_<group>.rs` for the tests.

Every gcode entry point builds its context through `HubScope::context` (1.3), with
`HubScope { machine_id: <hub machine>, overlay_project_id: None, .. }` and
`NodeScope { root: <row root_path>, parent_root: None }`. It runs under
`tokio::task::spawn_blocking`, as 1.9's handlers do.

Release ownership (Constraints): 5.1, 6.1, 7.1 and 7.2 change gcode, so they bump
`gcode` and `gdaemon`. 6.2 changes only gcodeindex and bumps `gdaemon`.

Ordering. The P5 to P7 leaves form one chain after 4.3: 5.1, 6.1, 6.2, 7.1, 7.2. They
share the release files, `crates/gcodeindex/src/family.rs` and `node/hub.rs`. 1.10
depends on 7.2.

### 5.1 `code_index` family and `GET /graph/search` [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/family.rs`
- `crates/gcodeindex/src/family/search.rs`
- `crates/gcodeindex/tests/family_search.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/family_search.rs`
- `crates/gruntime/src/lib.rs::*` — scope-reason: `RuntimeFamily::claims`'s row match moves into a new `pub fn claims_native`, which `claims` calls
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` builds and leaks `CodeIndexFamily` beside the runtime family
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: adds the `code_index` default and native test
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

The family (`crates/gcodeindex/src/family.rs`, re-exported from `lib.rs`):

- `CodeIndexFamily::new(pool, home, database_url)`. `name()` returns `"code_index"`,
  `prefixes()` returns `&["/api/code-index"]` and `default_backend()` returns `Proxy`.
- `NATIVE_ROUTES: &[gobby_runtime::NativeRoute]` lists each served method and path
  with `callers: &[Caller::Operator]`. Requests matching no row reach Python.
- `claims(parts)` is `gobby_runtime::claims_native(NATIVE_ROUTES, parts)`.
  `claims_native` is `RuntimeFamily::claims`'s row match, moved into a `pub fn` that
  `RuntimeFamily::claims` now calls. It reads `Caller::of` and `path_matches` in place.
- `router()` registers each row's handler, as gruntime's `handler` does.
- Shared code for every route, as P5's framing states: the gates and their bodies, the
  hub machine id, the project row, the `{"detail": …}` error body, and query parsing
  with Python's 422 bodies.

`serve::run` builds the family on the shared pool, with the home and the hub DSN,
next to the runtime family. It leaks it once and pushes it onto the composed list.
`RouteTable` picks the longest matching prefix
(`crates/gdaemon/src/front_door/routes.rs::RouteTable::lookup`), so
`/api/code-index/node/*` still reaches `code_index_node`.

`GET /api/code-index/graph/search` (`crates/gcodeindex/src/family/search.rs`):

- Query: `project_id`, `q` (required) and `limit` (default 25).
- Order: the `enabled` gate (503 `Code indexer not available`), then `project_id`
  (400). Python checks neither `graph_enabled` nor the project row.
- A project with no row for the hub machine returns 200 `{"results": []}`. Python's
  visibility filter finds no rows for it either.
- Otherwise `node::hub::family_search::graph_search(&HubScope, &NodeScope, q, limit)`
  runs under `spawn_blocking`:
  - It builds the context with `ServiceConfigSelection::database_only()` and opens a
    read-only connection.
  - It calls `search::fts::search_symbols_fts_visible(conn, q, ctx, None, None, &[],
    limit)`. An error there is logged and treated as no rows, as Python's
    `search_symbols_fts` returns `[]` when its keyword backend fails.
  - With no rows it calls `search_symbols_by_name_visible` with the same arguments.
    An error there is the route's 500.
  - It returns `Vec<SymbolHit { id, name, kind, file_path, line_start, signature }>`.
- The body is `{"results": [...]}`. Each result has the keys `id`, `name`, `type`,
  `kind`, `file_path`, `line_start` and `signature`, in that order, with `type` equal
  to `kind`.

Research context:
- Python's route (`src/gobby/servers/routes/code_index.py::create_code_index_router`,
  `graph_search`) runs `search_symbols_fts`, then `search_symbols_by_name`, both from
  `src/gobby/code_index/_storage/symbols.py`. They filter by the hub machine's
  `code_indexed_file_states`.
- `src/gobby/search/keyword.py` searches `code_symbols` over `name`,
  `qualified_name`, `signature`, `docstring` and `summary`. Those are the five columns
  `crates/gcode/src/search/fts/symbols.rs::search_symbols_fts_visible` matches with
  `@@@`. Its query sanitizer is gcode's `sanitize_pg_search_query`.
- gcode's `search` module is private to the crate, so gcodeindex reaches it only
  through a `node::hub` entry point.
- gruntime's family (`crates/gruntime/src/lib.rs`) is the pattern: `NativeRoute` rows,
  `Caller::Operator` for `x-gobby-key-id`, and `path_matches` for axum 0.8 placeholders.
- `serve::run` builds the runtime family only when the hub pool exists
  (`crates/gdaemon/src/serve.rs`), and the code_index family joins it there.
- Planned checks:
  - `cargo nextest run -p gobby-codeindex -p gobby-runtime`;
  - `cargo nextest run -p gobby-daemon --test front_door`;
  - `cargo clippy -p gobby-codeindex -p gobby-runtime`.

**Granularity:** five acceptance items and more than six production files. The seam
and its first route are one deliverable, for the same reason as 1.9: a family with no
route cannot be tested.

Consumers unchanged:
- `crates/gruntime/tests/config.rs` — no-edit-reason: drives `RuntimeFamily::claims`, whose behavior does not change.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.1.1 - `claims` holds for an API-key `GET /api/code-index/graph/search`. It does not hold without `x-gobby-key-id`, for a `POST` to that path, or for an unlisted path such as `/api/code-index/unlisted`. test: `crates/gcodeindex/tests/family_search.rs::family_claims_api_key_callers_on_listed_routes`.
- 5.1.2 - With `FAMILIES` and the leaked runtime, node and code-index families composed as `serve::run` composes them, and `code_index` absent from `front_door.routes`, an API-key search request reaches the backend stub. With `native` it gets the family's 500 body on an unreachable pool. An `/api/code-index/node/*` request still reaches the node family. test: `crates/gdaemon/tests/front_door.rs::code_index_defaults_to_proxy_and_honors_native`.
- 5.1.3 - With symbols indexed for the hub machine and for a foreign machine, search returns only hub-visible symbols in Python's seven-key shape. It falls back to name search when BM25 finds none, and returns `{"results": []}` for a project with no hub-machine row. test: `crates/gcodeindex/tests/family_search.rs::search_returns_hub_visible_symbols_in_python_shape`.
- 5.1.4 - `enabled` false returns 503 `Code indexer not available`, and `graph_enabled` false still serves. An absent or empty `project_id` returns 400. A missing `q` and a non-integer `limit` return Python's 422 bodies, and a negative `limit` returns 500 `Code graph request failed`. test: `crates/gcodeindex/tests/family_search.rs::search_follows_python_gates_and_errors`.
- 5.1.5 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P6: Graph read routes
`kind: framing`

P6 serves Python's five graph read routes from the hub entry points P2 built for nodes:

- 6.1: overview, file, neighbors and blast radius, through 2.7's
  `node::hub::graph_views` entry points;
- 6.2: path, through 2.6's `/nav/path` entry point.

Each route returns the JSON that the matching gcode command prints with
`--format json --allow-stale` for the hub machine's checkout. That JSON is what Python
relays from `src/gobby/code_index/gcode_gateway.py::GcodeGateway`, minus the
freshness prelude (P5).

Python's argument checks (`src/gobby/code_index/gcode_gateway.py::_validate_user_gcode_value`)
apply to `file_path`, `symbol_id`, `symbol_a` and `symbol_b`:

- an absolute POSIX or Windows path returns 400
  `Invalid <param>: value must not be an absolute path`;
- a `..` part under either path syntax returns 400
  `Invalid <param>: value must not contain '..' segments`.

Release ownership (Constraints): 6.1 bumps `gcode` and `gdaemon`, and 6.2 bumps
`gdaemon`.

### 6.1 Overview, file, neighbors and blast-radius routes [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gcodeindex/src/family.rs`
- `crates/gcodeindex/src/family/graph.rs`
- `crates/gcodeindex/tests/family_graph.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/family_graph.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

Routes, with Python's defaults and the gcode command each matches:

- `GET /api/code-index/graph`, with `limit` default 200: `gcode graph overview`.
- `GET /api/code-index/graph/file/{*file_path}`: `gcode graph file --file=<file_path>`.
  An empty remainder is not claimed and reaches Python.
- `GET /api/code-index/graph/symbol/{symbol_id}/neighbors`, with `limit` default 50:
  `gcode graph neighbors --symbol-id=<symbol_id>`.
  - Starlette matches routes on the decoded path. A segment that decodes to a value
    containing `/` therefore gets 404 `{"detail":"Not Found"}` from Python, and the
    handler returns the same.
- `GET /api/code-index/graph/blast-radius`, with `depth` default 3 and `limit` default
  100: `gcode graph blast-radius` with `--symbol-id` or `--file`.
  - It requires exactly one of `symbol_id` and `file_path`. A present but empty value
    counts as absent, as Python's `bool()` test does.
  - Otherwise it returns 400 `Provide exactly one of symbol_id or file_path`. This
    check runs before the `project_id` check (P5's step 2).

Each handler runs P5's order. Argument checks come after the project row, as in
Python's `src/gobby/code_index/context.py::CodeIndexContext`. Then:

- `node::hub::family_graph::resolve_file(&HubScope, &NodeScope, &str)` resolves a
  `file_path` the way the CLI resolves an exact file: `commands::scope::resolve_path_input`
  with `ScopedPathInput::ExactFile`, with the checkout root as the working directory.
  A path that function rejects returns 500, as Python's failed gcode run does.
- The handler calls 2.7's entry point for the route with 2.7's request type, under
  `spawn_blocking`. The body is the returned `GraphPayload`.
- A `RouteError` or library error returns 500 `Code graph request failed`. That
  includes an unreachable Falkor, for which Python's gateway raises
  `GcodeFalkorTransportError` and returns 500.

Research context:
- `crates/gcode/src/dispatch.rs::run` resolves `graph file` and `graph blast-radius
  --file` with `resolve_exact_file`, then runs freshness and the payload command.
  `resolve_path_input` uses the project root as the base for a relative path that does
  not start with `./`.
- `print_graph_payload` prints the `GraphPayload` as JSON
  (`crates/gcode/src/commands/graph/payload.rs`), and Python's `_run_json` parses
  that stdout.
- An unindexed file returns an empty payload. Neither path errors.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node)'`;
  - `cargo nextest run -p gobby-codeindex`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 6.1.1 - With Python's defaults, each of the four routes returns the JSON that `gcode graph overview`, `file`, `neighbors` or `blast-radius` prints with `--format json --allow-stale` for the hub machine's checkout. test: `crates/gcodeindex/tests/family_graph.rs::graph_reads_return_the_cli_payload_for_the_hub_machine`.
- 6.1.2 - Python's order and bodies hold:
  - either gate false returns 503 `Code graph not available`;
  - blast radius with both or neither target returns 400 before the `project_id` check;
  - an absent `project_id` returns 400;
  - an unknown project returns 404;
  - an absolute or `..` value returns 400 with Python's messages.

  test: `crates/gcodeindex/tests/family_graph.rs::graph_reads_follow_python_order_and_bodies`.
- 6.1.3 - `a.rs` and `./a.rs` select the same file, and an encoded `/` in `symbol_id` returns 404 `Not Found`. test: `crates/gcodeindex/tests/family_graph.rs::graph_read_paths_resolve_against_the_checkout_root`.
- 6.1.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 6.2 Path route [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `crates/gcodeindex/src/family.rs`
- `crates/gcodeindex/src/family/path.rs`
- `crates/gcodeindex/tests/family_path.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gdaemon` by 0.0.1.

`GET /api/code-index/graph/path` takes `symbol_a` and `symbol_b` (both required) and
`max_depth` (default 6, at least 1). It matches `gcode path <symbol_a> <symbol_b>
--max-depth <max_depth>`. In order, after P5's steps 1 to 5:

1. A `symbol_a` or `symbol_b` that starts with `-` returns 400
   `Invalid <param>: value must not start with '-'`. Python checks this before the
   path checks, because these values are positional gcode arguments.
2. P6's absolute-path and `..` checks.
3. The handler calls 2.6's `/nav/path` entry point under `spawn_blocking` and
   returns the `GraphPathResponse` JSON that `gcode path … --format json
   --allow-stale` prints.
   - An unresolved endpoint gives `found: false` inside that response.
   - A graph that is off gives the same response with its `hint`.
   - The CLI clamps `max_depth` to `code_graph::MAX_SYMBOL_PATH_DEPTH`, and the body
     shows the clamped value.
   - Where the CLI exits non-zero, as on a Falkor transport error, the route returns
     500 `Code graph request failed`.

Research context:
- `crates/gcode/src/commands/graph/reads.rs::path` resolves both endpoints, reads
  `shortest_symbol_path` when both resolve, and prints `GraphPathResponse`. 2.6 splits
  it into a read returning `GraphRead<GraphPathResponse>` and a render. This leaf maps
  the read's cases to the JSON the render prints.
- Python's route uses `Query(6, ge=1)`, so `max_depth=0` returns FastAPI's
  `greater_than_equal` 422.
- Planned check: `cargo nextest run -p gobby-codeindex`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 6.2.1 - A found path, an unresolved endpoint and a graph that is off each return the JSON that `gcode path … --format json --allow-stale` prints for the hub machine's checkout. test: `crates/gcodeindex/tests/family_path.rs::path_returns_the_cli_response`.
- 6.2.2 - A leading `-` and an absolute value return Python's 400 bodies. A missing `symbol_b` and `max_depth=0` return Python's 422 bodies. An unknown project returns 404. test: `crates/gcodeindex/tests/family_path.rs::path_follows_python_order_and_bodies`.
- 6.2.3 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P7: Graph lifecycle and invalidate routes
`kind: framing`

P7 ports Python's three remaining code-index mutations:

- 7.1: `POST /api/code-index/graph/clear` and `POST /api/code-index/graph/rebuild`;
- 7.2: `POST /api/code-index/invalidate`.

No production client calls these routes today. They stay public API
(`docs/guides/code-index.md`), and P7 ports them for API-key callers under ruling
E6(a).

- `gcode graph clear` and `gcode graph rebuild` run locally
  (`crates/gcode/src/commands/graph/lifecycle.rs::clear` and `rebuild`).
- gcode's HTTP lifecycle client
  (`crates/gcode/src/graph/code_graph/lifecycle.rs::run_lifecycle_action`) has only
  test callers.
- `gcode invalidate` ignores its daemon URL.
- The web UI calls only the reads.

Two things P7 leaves out:

- A grant-verified path (D16, ruling E7(a)). The three Brokered operations
  `clear_projection`, `rebuild_projection` and `invalidate_projection` have no
  presenting client, and P9 removes them. Agent-bearer callers stay on Python until
  then.
- A lease fence (D17). Python's `src/gobby/servers/grant_auth.py::admission_required`
  has no callers, and native HTTP admission belongs to the lifecycle cutover (S1.3,
  `crates/gdaemon/src/lease/standby.rs`).

`project_id`. Clear and rebuild take the `project_id` query parameter. Invalidate takes
the JSON body `{"project_id": "<id>"}`, where `project_id` is a required string. For an
API-key caller, Python's `_scoped_project_id` reduces to `_require_project_id`, because
agent claims exist only for agent bearers.

Release ownership (Constraints): 7.1 and 7.2 bump `gcode` and `gdaemon`.

### 7.1 Graph clear and rebuild routes [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/lifecycle.rs::clear_project_graph`
- `crates/gcode/src/commands/graph/lifecycle.rs::rebuild_project_graph`
- `crates/gcode/src/commands/graph.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/family_lifecycle.rs`
- `crates/gcodeindex/src/family.rs`
- `crates/gcodeindex/src/family/lifecycle.rs`
- `crates/gcodeindex/tests/family_lifecycle.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

gcode:

- `clear_project_graph` and `rebuild_project_graph` become `pub(crate)`, and
  `crates/gcode/src/commands/graph.rs` re-exports them beside `clear` and `rebuild`.
- `node::hub::family_lifecycle::clear` and `rebuild` take `(&HubScope, &NodeScope)`
  and return `Result<serde_json::Value, RouteError>`:
  - They build the context with the Falkor selection 2.6's routes use.
  - They run the function under `index_lock::with_exclusive_project_lock`, as the
    CLI's `clear` and `rebuild` do.
  - They return `GraphLifecycleOutput.payload`, the JSON that
    `gcode graph clear|rebuild --format json` prints and Python relays.

Routes, in order:

1. The `enabled` gate: 503 `Code indexer not available`.
2. `project_id`: 400.
3. The `graph_enabled` gate: 503 `Code graph not available`.
4. The project row: 404.
   - Rebuild reads the row in Python too.
   - Python's clear passes the id to gcode and gets gcode's own 404. The native
     context needs the row's root.
5. The call under `spawn_blocking`.
6. A payload with `success` false returns 400 `{"detail": <payload error>}`. Rebuild
   reports per-file failures this way. Otherwise the route returns 200 with the
   payload.

A project lock still busy after the wait is an error, so it returns 500, as Python's
failed gcode run does.

Research context:
- `clear_project_graph` resets the project's graph sync state and clears its Falkor
  projection. `rebuild_project_graph` re-projects every indexed file and sets
  `success` to `errors.is_empty()`. Both are private to
  `crates/gcode/src/commands/graph/lifecycle.rs`,
  and `commands` is private to the crate.
- Python: `src/gobby/servers/routes/code_index.py::create_code_index_router`
  (`clear_graph`, `rebuild_graph`), then `src/gobby/code_index/context.py::CodeIndexContext`
  (`clear_graph`, `rebuild_graph`) and the gateway's `graph_clear` and
  `graph_rebuild`.
- #23834 (Vector and graph pin scope) and #23578 (Graph CLI honors the completion
  compare-and-set) edit `crates/gcode/src/commands/graph/lifecycle.rs` (Constraints, concurrent
  editors). This leaf changes only visibility there.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(graph)'`;
  - `cargo nextest run -p gobby-codeindex`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 7.1.1 - Clear marks every machine's files of the project graph-pending, a foreign machine's row included, as `reset_graph_sync_for_project` does. It clears the project's projection and returns the payload `gcode graph clear --format json` prints. test: `crates/gcodeindex/tests/family_lifecycle.rs::clear_returns_the_cli_payload`.
- 7.1.2 - Rebuild returns the CLI payload, and a rebuild with a per-file failure returns 400 with the payload's `error` as `detail`. test: `crates/gcodeindex/tests/family_lifecycle.rs::rebuild_returns_the_cli_payload_and_400_on_failure`.
- 7.1.3 - Python's order and bodies hold:
  - `enabled` false returns 503 `Code indexer not available`;
  - `graph_enabled` false returns 503 `Code graph not available`;
  - an absent `project_id` returns 400;
  - an unknown project returns 404.

  test: `crates/gcodeindex/tests/family_lifecycle.rs::lifecycle_routes_follow_python_order_and_bodies`.
- 7.1.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.2 Invalidate route [category: code] (depends: 7.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/index/indexer/lifecycle.rs::invalidate`
- `crates/gcode/src/commands/status/invalidate.rs::*` — scope-reason: `invalidate_project_locked` passes `ctx.machine_id` to `indexer::invalidate`
- `crates/gcode/src/freshness.rs::invalidate_test_project`
- `crates/gcode/src/communities/refresh_tests.rs::*` — scope-reason: `invalidate_removes_community_rows_with_project_state` passes the context's machine
- `crates/gcodeindex/src/family.rs`
- `crates/gcodeindex/src/family/invalidate.rs`
- `crates/gcodeindex/tests/family_invalidate.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/family_invalidate.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

gcode: `indexer::invalidate(conn, machine_id, project_id) -> anyhow::Result<u64>`.

- It drops the unused `_daemon_url` parameter.
- It takes the machine from its caller instead of `read_local_machine_id`.
- It returns the number of `code_indexed_project_states` rows deleted.
- It still deletes the machine's `code_communities`, `code_indexed_file_states` and
  `code_indexed_project_states` rows in one transaction, and still prints its stderr
  line.
- Local callers pass `ctx.machine_id`, which 1.1 adds.

`POST /api/code-index/invalidate`, in order:

1. The `enabled` gate: 503 `{"error":"Code indexer not available"}`.
2. The body:
   - A missing body, invalid JSON, a missing `project_id` or a non-string value
     returns Python's 422 bodies.
   - An empty string returns 400 `project_id is required`.
3. With no row for the hub machine, the route returns 200
   `{"status":"ok","project_id":"<id>","note":"not indexed"}`.
4. Otherwise `node::hub::family_invalidate::invalidate(database_url, machine_id, project_id)`
   runs under `spawn_blocking`. It takes the project lock by id with
   `index_lock::lock_project_by_id(…, IndexLockPolicy::maintenance_try())`, as
   `gcode invalidate` and prune do, then runs `indexer::invalidate` for the hub machine
   on a read-write connection from the hub DSN. Without the lock, an index run or
   community refresh holding it could write communities from its in-memory watermark
   after the project-state row is gone, which is the id reissue the deletion prevents.
   A busy lock returns step 5's 500 body. Otherwise the route returns 200:
   `{"status":"ok","project_id":"<id>","stores":{"graph":{"status":"skipped"},"vector":{"status":"skipped"},"hub":{"status":"ok","deleted":{"symbols":0,"files":0,"imports":0,"calls":0,"content_chunks":0,"projects":<n>}}},"failed_stores":[]}`.
   Python's 207 for `partial_failure` never occurs, because its `invalidate` always
   returns `ok`.
5. Python's route has no `try`, so its failures reach the global handler
   (`src/gobby/servers/exception_handlers.py::register_exception_handlers`). The
   native route returns the same bodies:
   - an unreachable hub database returns 503
     `{"status":"error","message":"Hub temporarily unavailable"}`;
   - any other failure returns 500
     `{"status":"error","message":"Internal server error","error_logged":true}`.

`crates/gcode/src/node/hub/family_invalidate.rs` is new and declared in
`crates/gcode/src/node/hub.rs`. It lives inside gcode, so it calls the crate-private
`index_lock::lock_project_by_id` and `indexer::invalidate` without changing their
visibility.

Why the native route deletes communities:

- Python's `delete_project_index` (`src/gobby/code_index/_storage/projects.py`)
  deletes only the project-state row. The file states go with it through
  `code_indexed_file_states_project_state_fkey`'s cascade.
- `code_communities` references `code_indexed_projects`, so its rows survive. The
  project-state row holds `community_id_watermark`, so its deletion resets the
  watermark while community ids remain, and those ids can then be reissued.
- `gcode invalidate` deletes the communities for that reason
  (`crates/gcode/src/communities/refresh_tests.rs::invalidate_removes_community_rows_with_project_state`).
  The native route follows gcode.

Research context:
- `invalidate`'s callers are `crates/gcode/src/commands/status/invalidate.rs`
  (`invalidate_project_locked`, which prune also calls), the
  `crates/gcode/src/freshness.rs` test helper and the communities test.
- `index_lock::lock_project_by_id(database_url, project_id, policy, diagnostics)`
  (`crates/gcode/src/index_lock.rs`) opens its own read-write connection and returns
  `Busy` while another session holds the project lock. `IndexLockPolicy::maintenance_try`
  waits at most 150 ms. `gcode invalidate`
  (`crates/gcode/src/commands/status/invalidate.rs`) and prune
  (`crates/gcode/src/commands/status/prune.rs`) take the lock the same way.
- #23835 (Pin-aware retention) edits `crates/gcode/src/index/indexer/lifecycle.rs` (Constraints,
  concurrent editors). The executor applies this signature to the tree it starts
  from.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(invalidate)'`;
  - `cargo nextest run -p gobby-codeindex`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 7.2.1 - Local `gcode invalidate` deletes the context machine's communities, file states and project state, and leaves another machine's rows. test: `crates/gcode/src/communities/refresh_tests.rs::invalidate_removes_community_rows_with_project_state`.
- 7.2.2 - For an indexed project, the route deletes only the hub machine's rows and returns Python's body with `projects` equal to 1. For a project with no hub-machine row it returns the `not indexed` body. test: `crates/gcodeindex/tests/family_invalidate.rs::invalidate_returns_python_bodies_for_the_hub_machine`.
- 7.2.3 - `enabled` false returns 503 `{"error":"Code indexer not available"}`. A missing `project_id` returns Python's 422 body, an empty one returns 400, and an unreachable hub database returns Python's 503 body. With the project lock held by another connection, the route deletes nothing and returns the 500 body. test: `crates/gcodeindex/tests/family_invalidate.rs::invalidate_follows_python_gates_and_errors`.
- 7.2.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P8: Hub code-index jobs
`kind: framing`

P8 moves Python's code-index background work into gdaemon on the hub:

- 8.1: the job seam and the sync worker;
- 8.2: the maintenance pass;
- 8.3: symbol summaries and community labels;
- 8.4: the hourly prune and `POST /api/code-index/prune` (D17);
- 8.5: content GC for projects only nodes index, after #23835 (Pin-aware retention);
- 8.6: node workspace removal releases its rows, after #24372 (Worktree and clone
  preparation, repair and removal);
- 8.7: the edit trigger's native entry point, which #24151 (S2.11 hub half: hook
  ingress and rule engine in Rust) calls;
- 8.8: the nightly repair handler, which #21582 (Cron and scheduler route family)
  schedules.

Each leaf deletes the Python job it replaces, so the two never run together (ruling,
2026-10-17 21:46, writer's choice 3). 8.7 and 8.8 delete nothing: nothing calls their
entry points until #24151 and #21582 call them and delete the Python files (D18).

Where the jobs live:

- `crates/gcodeindex/src/jobs.rs` holds `HubJobs`, the tick model and `ticks()`, which
  lists every job as a `(name, tick)` pair. Each job's code is in
  `crates/gcodeindex/src/jobs/<job>.rs`.
- gcodeindex reaches gcode only through pub entry points in
  `crates/gcode/src/node/hub/job_<job>.rs`, declared in `node/hub.rs`, as P5's route
  groups do.
- `crates/gdaemon/src/code_index_jobs.rs::register(host, jobs)` registers every tick on
  the hub's `HeartbeatHost` (`crates/gdaemon/src/heartbeat.rs`). `serve::run` calls it
  on its non-node branch when the pool exists. Python runs these loops in every
  non-node daemon that has a code indexer
  (`src/gobby/runner_startup_code_index.py::_start_code_index_tasks`), so standalone
  mode gets them too.

Tick model (writer's choice 2, accepted 2026-10-17 21:46):

- Each job registers at `BASE_CADENCE` (5 s).
- A tick reads the published active config. With `code_index.enabled` false it does
  nothing.
- It starts work only when the job's interval, read from live config, has passed since
  its last start, and no run of that job is in flight. The busy flag is the job's own.
- It hands the work to a named `std::thread` and returns. `HeartbeatHost::supervise`
  awaits each due job in turn, so work done inside a tick would stall every other job:
  a 15-minute reindex would hold back the 5 s sync.
- A `std::thread`, never `spawn_blocking`. `serve::run` drops its tokio runtime on
  shutdown, and a dropped runtime waits for every `spawn_blocking` task to return. A
  plain thread ends with the process.
- Work cut off at process exit is safe to lose. A file whose projection stopped midway
  keeps its attempt stamp, stays pending and retries after the cooloff. The
  compare-and-set never marks a version the run did not finish. An interrupted index
  run is redone by the next maintenance pass.

How the work runs:

- In process, on the hub's bootstrap DSN with its own connections (D5), as the hub
  machine or as a row's machine, through `HubScope::context` (1.3) with the services it
  needs.
- Python launches each `gcode` child under
  `src/gobby/code_index/maintenance_launch.py`, which supplies a managed per-project
  grant so the child's writes pass RLS. gdaemon has no grant issuer before #24449
  (Native interactive handshake), so the jobs run gcode's library instead.
- In-process embedding and generation go through gcore's daemon client. It presents
  the hub's local API key (`gobby_core::local_token::read_api_key`) to the hub's own
  front door, and `/api/embeddings` and `/api/llm/generate` stay Python. With Python
  down the call fails as `embedding_http`, and the vector breaker pauses vectors (8.1).
  The jobs therefore need no wait for Python's startup.

Config. `crates/gcodeindex/src/jobs/config.rs::JobConfig::read(pool, home)` reads the
published active state as 5.1's gates do
(`gobby_runtime::config::active_state(pool, &gobby_core::grant::deployment_token(home))`):

- Each `code_index.*` key comes from `document.active_values`. An absent key, or a
  value of the wrong type, takes the contract default, kept as a constant in
  `jobs/config.rs`. A test compares every constant with the key's `default` in
  `crates/gcore/assets/config/runtime_config_contract.json`.
- The keys are `machineExport: false`. Most are `activation: live`, so the active
  state holds the value Python serves after a live change. 8.3's
  `code_index.symbol_summary.*` and `code_index.community_label.*` keys are
  `restart_required`, and their active value changes when Python restarts (8.3).
- With no published state the tick does nothing. A failed read logs once to gdaemon's
  stderr and stays quiet until a read succeeds, as Python throttles its pool-outage
  line.

Operator decision (writer's choice 1; #15977 puts it to Josh with the stamped plan):

- P9 removes `code_index.nightly_repair_timeout_seconds`. Python kills a `gcode repair`
  child that passes it, and the in-process handler (8.8) cannot be killed. The key's
  last reader is `src/gobby/code_index/nightly_repair.py`, which #21582 deletes, so the
  removal waits on #21582.

Keys that stay, which the native jobs read or leave alone:

- `code_index.sync_worker_projection_timeout_seconds` becomes the sync job's per-file
  stall timeout (8.1).
- `code_index.maintenance_index_timeout_seconds` stays the BM25 repair timeout, for
  `src/gobby/runner_startup_code_index.py::_repair_code_index_bm25` and
  `src/gobby/cli/postgres.py::repair_code_index_cmd`. The native maintenance pass (8.2)
  does not read it. #15977's 22:29 ruling put this key in the operator decision. The
  ruling assumed the maintenance loop was its only reader.
- `code_index.maintenance_log_file` stays for the Python that outlives P8:
  `nightly_repair.py`, and `prune.py`'s operator half with
  `maintenance.py::_sweep_orphaned_index_projects`. The native jobs log to gdaemon's
  stderr. The key loses its last reader when P9 and #21582 delete those files.

Deliberate differences from Python, for every job:

- No gateway breaker. `src/gobby/code_index/context.py`'s `daemon_config_breaker` guards
  the daemon-config fetch each `gcode` child makes, and in-process work makes none.
- No disk checks. The hub cannot see a node's root, and projections read facts from
  the database. 8.2 owns missing roots.
- Subprocess-only failures (a missing binary, exit codes, kill timeouts) have no
  counterpart. Library errors take their place.
- Outcomes go to gdaemon's stderr. Python also wrote `gcode` runs to
  `code_index.maintenance_log_file` (`src/gobby/code_index/maintenance_log.py`).
- No BM25 gate. Python starts no worker when `_repair_code_index_bm25` finds a damaged
  BM25 index. A native write to `code_symbols` or `code_content_chunks` through a
  damaged index fails with its corruption SQLSTATE (`bm25_health.rs`'s `XX000` to
  `XX002`). The run logs the failure and retries on the next pass. The repair stays
  Python's, at startup and in `gobby postgres repair-code-index`. Edit-triggered and
  CLI index runs never had the gate.

Release ownership (Constraints): every P8 leaf bumps `gdaemon`, and a leaf that changes
gcode also bumps `gcode`.

Ordering. The P8 leaves form one chain after 7.2, because they share the release
files, `crates/gcodeindex/src/jobs.rs` and `node/hub.rs`. The chain runs 8.1 to 8.4,
then 8.7 and 8.8, then the two leaves gated on other plans: 8.5 (#23835) and 8.6
(#24372). So no other leaf waits on those gates, and #24151 and #21582 get their entry
points without them. 1.10 depends on 8.6.

### 8.1 Hub job seam and sync worker [category: code] (depends: 7.2, #23576)
`kind: deliverable`

Targets:
- `crates/gcode/src/db/sync_pending.rs`
- `crates/gcode/src/db/sync_pending/tests.rs`
- `crates/gcode/src/db/mod.rs::*` — scope-reason: declares and re-exports `sync_pending`
- `crates/gcode/src/commands/graph/lifecycle.rs::sync_file_graph`
- `crates/gcode/src/commands/graph.rs`
- `crates/gcode/src/projection/sync.rs::sync_vector_files`
- `crates/gcode/src/projection/sync.rs::typed_projection_error`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/job_sync.rs`
- `crates/gcode/src/node/hub/job_sync/tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/jobs.rs`
- `crates/gcodeindex/src/jobs/config.rs`
- `crates/gcodeindex/src/jobs/breaker.rs`
- `crates/gcodeindex/src/jobs/sync.rs`
- `crates/gcodeindex/src/jobs/tests.rs`
- `crates/gcodeindex/tests/job_sync.rs`
- `crates/gdaemon/src/code_index_jobs.rs`
- `crates/gdaemon/src/lib.rs::*` — scope-reason: declares `code_index_jobs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `serve_with_runtime` takes the `HeartbeatHost` it passes to `serve_doors`, `serve` passes an empty one, and `run`'s non-node branch registers the code-index jobs when the pool exists
- `crates/gdaemon/tests/code_index_jobs.rs`
- `src/gobby/code_index/sync_worker.py::*` — operation: delete — scope-reason: the native sync job replaces the Python worker
- `src/gobby/runner_startup_code_index.py::*` — scope-reason: `_start_code_index_tasks` stops creating the sync task, and `_repair_code_index_bm25`'s failure line names only the maintenance worker
- `tests/code_index/test_sync_worker.py::*` — operation: delete — scope-reason: tests the deleted worker
- `tests/code_index/test_sync_worker_breaker.py::*` — scope-reason: keeps only `TestSyncCircuitBreakerUnit` and its helpers, because `SyncCircuitBreaker` outlives the worker
- `tests/test_runner_code_index_shutdown.py::*` — scope-reason: adds the start check, and the drain test drops the deleted worker
- `src/gobby/config/code_index.py::*` — scope-reason: rewords `sync_worker_concurrency`'s description, because the native job bounds rows with it
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated by `scripts/generate_runtime_config_contract.py` for the new description
- `docs/guides/code-index.md`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

The seam (P8 framing):

- `HubJobs::new(pool, home, database_url)` holds the shared pool, the home and the hub
  DSN. Each job keeps its busy flag and last start time.
- `HubJobs::ticks(self: &Arc<Self>) -> Vec<(&'static str, Tick)>`, where `Tick` is a
  boxed `Fn() -> BoxFuture<'static, anyhow::Result<()>> + Send + Sync`. 8.1 lists
  `code_index_sync`. Later leaves append their jobs here and do not edit gdaemon.
- `code_index_jobs::register(host, jobs)` registers each pair at `BASE_CADENCE`.
  `serve::run` builds `HubJobs` next to the code_index family and calls `register`
  before passing the host to `serve_with_runtime`. `serve` and the tests pass
  `HeartbeatHost::new()`.
- The hub machine id comes from `gobby_core::machine::read_machine_id_from_home(home)`,
  read per tick.

Sync job (`crates/gcodeindex/src/jobs/sync.rs`), interval
`code_index.sync_worker_interval_seconds` (5.0):

1. `node::hub::job_sync::sync_rows(database_url)` returns every
   `code_indexed_project_states` row as
   `SyncRow { machine_id, project_id, root_path, registry_deleted }`, from
   `db::sync_rows`. `registry_deleted` comes from a `LEFT JOIN` on `projects` and holds
   when `deleted_at` is set. A project with no registry row reads `false`, so overlays
   still sync. Python lists only the hub machine's rows.
2. `plan_row(hub_machine, row, config, breakers)` decides each row's work:
   - nothing for an empty root, or for a soft-deleted project, which 8.2 reconciles for
     every machine;
   - graph when `code_index.graph_enabled` holds and the graph breaker allows;
   - vectors only for the hub machine's rows, when `code_index.embedding_enabled`
     holds and the vector breaker allows. Nodes embed and apply their own vectors
     (D13, 4.3).
   - A breaker due for a probe allows one file for its target.
3. Up to `code_index.sync_worker_concurrency` rows run at once, each on its own thread
   and connections. A row projects its files one at a time. Python runs up to that many
   files of one project at once (Deliberate differences below).
4. Each row runs `job_sync::sync_row(&HubScope, root, SyncRequest { graph, vectors, batch_size, stall })`,
   with `batch_size` from `code_index.sync_worker_batch_size` and `stall` from
   `code_index.sync_worker_projection_timeout_seconds`.
5. Each row's `SyncRowReport` feeds the breakers (below). Errors are logged to stderr
   with the row's machine and project, and the job moves to the next row.

`job_sync::sync_row` builds the row's context through `HubScope::context` with
`ServiceConfigSelection::all()` when vectors run, else
`ServiceConfigSelection::falkordb_only()`. Then, per target:

- Graph:
  1. `db::pending_graph_files(conn, project_id, machine_id, batch_size)` returns
     `PendingGraphFile { path, language, symbol_count }`.
  2. Files with no symbols, or whose lowercased language is not in
     `db::sync_pending::GRAPH_SYNC_LANGUAGES`, take the shortcut:
     `mark_graph_sync_attempted`, then `mark_graph_synced` with that attempt, and no
     Falkor call. The language list is Python's
     (`src/gobby/code_index/sync_worker.py::_GRAPH_SYNC_LANGUAGES`).
  3. The rest are projected one file at a time by
     `commands::graph::sync_file_graph(ctx, path, true)`.
- Vectors: `db::pending_vector_files(conn, project_id, machine_id, limit)` (4.2), with
  `limit` 1 for a probe. Each file is projected by
  `projection::sync::sync_vector_files(ctx, &[path], None)`.

Both targets:

- `job_sync::run_files_bounded(ctx, paths, stall, per_file)` runs the batch on a worker
  thread. The worker takes
  `index_lock::lock_project_files(ctx, paths, IndexLockPolicy::brief_index_flush_try())`,
  as `gcode graph sync-file` and `gcode vector sync-file` do, and holds the lock set
  until it exits. Busy files are skipped without an attempt stamp and come back on a
  later tick. Python leaves them for the cooloff.
- The worker sends one message per file and checks a stop flag before each file. The
  row waits for each message with `recv_timeout(stall)`, as
  `projection/sync.rs::run_projection_phase_bounded` does.
- On a stall the row sets the stop flag, stops waiting and reports the target
  `stalled`. The worker finishes only the blocked file, then exits and drops its
  locks. An exclusive project lock (7.1's clear, 7.2's invalidate, 8.2's reconcile
  and sweep) therefore waits for that file, as it waits for a live one. gcore's
  `falkor.rs` sets a 30 s read and write timeout on every Falkor connection
  (`DEFAULT_SOCKET_TIMEOUT`), so a blocked query ends within that. The compare-and-set
  keeps a late write from marking a version that moved.
- Each failed file's kind comes from `typed_projection_error`. The file keeps its
  attempt stamp and waits out the 300 s cooloff.
- `SyncRowReport` holds, per target: files synced, skipped and failed, the first error
  kind, and whether it stalled.

gcode visibility changes: `commands/graph/lifecycle.rs::sync_file_graph` becomes
`pub(crate)`, re-exported from `crates/gcode/src/commands/graph.rs`.
`projection/sync.rs::sync_vector_files` and `typed_projection_error` become
`pub(crate)`.

`db::sync_pending` (`crates/gcode/src/db/sync_pending.rs`):

- `pending_graph_files` mirrors 4.2's `pending_vector_files`. It selects the machine's
  selected versions where `graph_synced` is false and no graph attempt falls within the
  300 s cooloff, ordered by `indexed_at`, then path.
- `sync_rows` reads `code_indexed_project_states`.

Breakers (`crates/gcodeindex/src/jobs/breaker.rs`) port
`src/gobby/code_index/sync_breaker.py::SyncCircuitBreaker`. There is one for each
target, both on the `code_index.sync_worker_breaker_*` keys, re-read every tick as
Python's `apply_limits` does:

- The breaker is closed, open or half-open:
  - closed allows all work;
  - open allows none until its backoff passes, then allows one probe file and turns
    half-open;
  - a probe's success closes the breaker and resets the backoff;
  - a probe's transport failure doubles the backoff, up to the maximum, and reopens;
  - a probe that was busy or failed for another reason reopens without doubling.
- While closed, consecutive transport failures open the breaker at
  `sync_worker_breaker_failure_threshold`.
- The vector breaker's transport failures are `embedding_http` and `qdrant_http`, as
  Python's `_is_transient_vector_error` maps them.
- A stall opens the target's breaker at once, past the threshold, because every stall
  may leave a blocked thread. The graph breaker counts only stalls. Python's graph
  errors never touch a breaker.

Deliberate differences from Python's sync worker:

- Node machines' rows sync their graph (D10).
- A transient failure leaves the file on the 300 s cooloff. Python retries twice in
  the call (1 s, then 2 s) and then clears the stamp (`requeue_vector_sync`,
  `requeue_graph_sync`). The breaker still stops attempts during an outage.
- A stall stops waiting for the file's worker, which ends after that file. Python
  kills the `gcode` child at the same timeout.
- `code_index.sync_worker_concurrency` bounds rows, and each row projects one file at
  a time, so a hub with one project syncs one file at a time where Python ran four.
  In process, a file costs no `gcode` child start, which Python's concurrency
  overlapped. Concurrent files within a row would each need their own stall watch,
  stop flag and lock set. The key's description
  becomes "Maximum machine-project rows the sync job projects at once", in
  `src/gobby/config/code_index.py` and the regenerated contract.
- Rows of a soft-deleted project do not sync. Python never syncs a node machine's row,
  and 8.2 removes every machine's row of a soft-deleted project.

Python deletion:

- `src/gobby/code_index/sync_worker.py` goes.
- `_start_code_index_tasks` stops starting it and still sets `runner._sync_worker_task`
  to `None`. `_repair_code_index_bm25`'s failure line says the maintenance worker will
  not start, and no longer names a sync worker.
- `GobbyRunner`'s sync worker attributes and
  `src/gobby/runner_lifecycle_shutdown.py::_stop_code_index_workers` stay until 8.3.
  The drain's sync half is then a no-op: it reads the event with a `getattr` default,
  and `_cancel_runner_task` skips a `None` task. The shutdown module is 916 lines, so
  8.3 removes the whole drain under one `delete-lines` proof.
- `tests/code_index/test_sync_worker.py` goes. `tests/code_index/test_sync_worker_breaker.py`
  keeps `TestSyncCircuitBreakerUnit`, because `context.py`, `trigger.py` and the
  maintenance tests still use `SyncCircuitBreaker`. P9 deletes the breaker with its
  last consumer.

`docs/guides/code-index.md` says gdaemon's sync job projects pending files, for the
hub's rows and node machines' graph facts. It no longer names a daemon sync worker.

Research context:
- Python's worker: `src/gobby/code_index/sync_worker.py::sync_worker_loop` (breaker
  limits re-read per pass) and `_sync_pass`. `_sync_pass` lists the hub's projects,
  skips roots that are not directories, reads `get_pending_sync_files`
  (`src/gobby/code_index/_storage/files.py`) and syncs files under a semaphore of
  `sync_worker_concurrency`. While any breaker is open it runs files one at a time.
  `_sync_file` checks the file on disk, then runs `gcode vector sync-file` and
  `gcode graph sync-file` through the gateway.
- `sync_file_graph` skips project-wide orphan cleanup on purpose: the comment at
  `crates/gcode/src/commands/graph/lifecycle.rs:248` says it timed out the daemon's
  per-file sync on large graphs. `projection/sync.rs::sync_graph_files`, the index
  path, runs `cleanup_orphans` after every batch, so the job does not use it.
- `commands/vector.rs::sync_file` prints its output and exits the process with code 3
  when the file lock is busy. The job therefore calls `sync_vector_files`, whose
  per-file `VectorProjectionState::sync_file` marks the attempt, upserts and runs the
  compare-and-set.
- `index_lock::lock_project_files` takes PostgreSQL advisory locks: the project lock
  shared and each file's lock exclusive. It checks `is_dir` under the root only to drop
  directories, so a node root absent from the hub's disk keeps its files. The returned
  `ProjectFileLockSet` owns the `postgres::Client` that holds the locks
  (`ProjectFileLocks`), and that client is `Send`, so the worker thread can own it.
- `code_indexed_project_states` has no deletion column
  (`crates/gcore/assets/schema/baseline.sql`). `projects.deleted_at` marks a
  soft-deleted project, as 8.2's `registry_state` reads it.
- `embed_via_daemon` (`crates/gcore/src/ai/daemon/operations.rs`) calls
  `require_granted`, which passes when the context has no grant, then posts with the
  local API key.
- `HeartbeatHost::supervise` awaits each due job's spawned task before the next.
- #23576 (Sync worker leaves projection completion to gcode) makes gcode own
  completion. This leaf replaces the worker it edits, so it lands after it.
- Rejected alternatives:
  - `projection::sync::sync_after_index_bounded`, which runs `cleanup_orphans` on every
    graph batch;
  - running work inside the tick, which stalls the other jobs;
  - `spawn_blocking`, which would hold gdaemon's shutdown behind a long run;
  - one heartbeat job per project, because registration freezes at start.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(sync_pending) | test(job_sync)'`;
  - `cargo nextest run -p gobby-codeindex -p gobby-daemon`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_sync_worker_breaker.py tests/test_runner_code_index_shutdown.py tests/test_runner_lifecycle.py tests/config/test_runtime_config_contract.py -v`.

**Granularity:** thirteen acceptance items. The seam, the sync job and the worker's
deletion are one outcome: the leaf that starts the native job must stop the Python
one (writer's choice 3), and the seam has no job to run without it.

Consumers unchanged:
- `tests/runner_helpers.py` — no-edit-reason: sets the `sync_worker_*` config defaults, and those keys stay.
- `web/src/api/runtimeConfigCodecVectors.gen.ts` — no-edit-reason: the generator writes only codec vectors there, and a description is not one.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 8.1.1 - With version A selected by the local machine and version B by a foreign machine at one path, `pending_graph_files` under the foreign machine returns B with its language and symbol count, honors `limit`, orders by `indexed_at`, and skips B within 300 s of a graph attempt. `sync_rows` returns both machines' rows. test: `crates/gcode/src/db/sync_pending/tests.rs::pending_graph_files_select_the_machine_versions`.
- 8.1.2 - With no Falkor configured, `sync_row` marks a zero-symbol file and a file in a language outside `GRAPH_SYNC_LANGUAGES` `graph_synced` through the compare-and-set, and leaves a Rust file with symbols pending with `missing_falkordb_config` as its error kind. test: `crates/gcode/src/node/hub/job_sync/tests.rs::shortcut_marks_files_without_graph_facts`.
- 8.1.3 - While another connection holds the project's index lock exclusively, `sync_row` skips every file and stamps no attempt. test: `crates/gcode/src/node/hub/job_sync/tests.rs::busy_files_stay_pending_without_an_attempt`.
- 8.1.4 - `run_files_bounded` with a per-file function that blocks on its second file returns the first file's outcome and `stalled` within the stall timeout plus one second, and never runs the third file. Until the blocked function is released, another connection's exclusive project lock stays busy. After its release that lock succeeds. test: `crates/gcode/src/node/hub/job_sync/tests.rs::a_stall_abandons_the_worker`.
- 8.1.5 - `plan_row` gives the hub machine's row graph and vectors, a foreign machine's row graph only, nothing for an empty root or a soft-deleted project's row of either machine, and one probe file to a target whose breaker is due. `embedding_enabled` false and `graph_enabled` false drop their targets. test: `crates/gcodeindex/src/jobs/tests.rs::rows_plan_targets_per_machine_and_breaker`.
- 8.1.6 - Under a fake clock, the breaker opens after the threshold's consecutive transport failures, and a stall opens it at once. A failed probe doubles the backoff up to the maximum, a busy probe reopens without doubling, and a successful probe closes it and resets the backoff. test: `crates/gcodeindex/src/jobs/tests.rs::breaker_follows_the_python_state_machine`.
- 8.1.7 - A tick with `enabled` false starts nothing. A tick before the interval passes starts nothing. A tick while a run is in flight starts nothing, and returns at once. The tick after the run ends and the interval passes starts the next run. test: `crates/gcodeindex/src/jobs/tests.rs::ticks_start_one_run_per_interval`.
- 8.1.8 - Every `JobConfig` default equals the `default` of its key in `crates/gcore/assets/config/runtime_config_contract.json`, and an absent key or a mistyped value reads as that default. test: `crates/gcodeindex/src/jobs/tests.rs::config_defaults_match_the_runtime_contract`.
- 8.1.9 - With pending shortcut files for the hub machine and a foreign machine, and pending vector files for both, one sync run marks both machines' shortcut files `graph_synced` and stamps no vector attempt on the foreign machine's files. test: `crates/gcodeindex/tests/job_sync.rs::sync_job_covers_every_machine_row`.
- 8.1.10 - `code_index_jobs::register` adds every `HubJobs::ticks()` name, so a second registration of the same jobs on that host fails as a duplicate. The started host stops within `SHUTDOWN_GRACE`. test: `crates/gdaemon/tests/code_index_jobs.rs::registers_each_hub_job_once`.
- 8.1.11 - With a code indexer, `_start_code_index_tasks` creates only the `code-index-maintenance` task and leaves `_sync_worker_task` `None`. test: `tests/test_runner_code_index_shutdown.py::test_start_code_index_tasks_starts_only_the_maintenance_loop`.
- 8.1.12 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 8.1.13 - `code_index.sync_worker_concurrency`'s description reads "Maximum machine-project rows the sync job projects at once", and the checked-in contract equals fresh generator output. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.

### 8.2 Maintenance pass: reconcile and reindex [category: code] (depends: 8.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/db/hub_maintenance.rs`
- `crates/gcode/src/db/hub_maintenance/tests.rs`
- `crates/gcode/src/db/mod.rs::*` — scope-reason: declares `hub_maintenance`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/job_maintenance.rs`
- `crates/gcode/src/node/hub/job_maintenance/tests.rs`
- `crates/gcodeindex/src/jobs.rs`
- `crates/gcodeindex/src/jobs/config.rs`
- `crates/gcodeindex/src/jobs/maintenance.rs`
- `crates/gcodeindex/src/jobs/tests.rs`
- `crates/gcodeindex/tests/job_maintenance.rs`
- `src/gobby/code_index/maintenance.py::*` — scope-reason: `_run_maintenance` stops reconciling, sweeping and reindexing, and the pending-cleanup retry functions go; `_reconcile_stale_selector`, `_sweep_orphaned_index_projects` and `_registry_state` stay for `prune.py`'s operator prune until P9
- `src/gobby/code_index/cleanup.py::*` — operation: delete — scope-reason: its last consumer was the reindex path this leaf removes
- `tests/code_index/test_code_index_maintenance.py::*` — scope-reason: drops the tests of the removed steps and checks that the pass only summarizes and labels
- `docs/guides/code-index.md`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

The maintenance job (`crates/gcodeindex/src/jobs/maintenance.rs`) is
`code_index_maintenance` in `HubJobs::ticks()`. Its interval is
`code_index.maintenance_interval_seconds`, which `JobConfig` gains. Each run, on its
own thread:

1. When `code_index.graph_enabled` and `code_index.embedding_enabled` both hold, it
   sweeps orphans with `node::hub::job_maintenance::sweep_orphans(database_url, hub_machine)`.
   Python skips the sweep when either store is disabled, because the projections could
   not be cleared.
2. For each soft-deleted project that `db::hub_maintenance::deleted_selected_projects`
   returns, it runs
   `job_maintenance::reconcile_deleted(database_url, hub_machine, project_id, machines)`
   and logs the outcome with the project to stderr.
3. It takes the hub machine's rows from `job_sync::sync_rows` (8.1), leaving out rows
   whose `registry_deleted` holds, which step 2 owns. Python lists only the hub
   machine's rows here, and their roots are on the hub's disk.
4. For each row it runs `job_maintenance::maintain_project(&HubScope, root)` and logs
   the outcome with the project to stderr. A failure moves on to the next row.

`node::hub::job_maintenance` (new, declared in `node/hub.rs`):

- `eligibility(registry, root, project_id)` ports
  `src/gobby/code_index/eligibility.py::resolve_indexed_project`:
  - with no registry row, or a deleted one: `overlay` when the row is not deleted,
    the root is a directory and `gobby_core::project::code_index_id_for_root(root)`
    equals the id, else `unregistered`;
  - an empty root, or one that is not a directory: `missing_root`;
  - a `.gobby/project.json` whose `id` differs from the project id:
    `identity_mismatch`;
  - otherwise `active`.
- `maintain_project` reads the registry state with `db::hub_maintenance::registry_state`
  and acts on the eligibility:
  - `overlay`: nothing. The edit trigger owns an overlay's freshness (#20889).
  - `unregistered`, `missing_root` or `identity_mismatch`: it reconciles. Under `index_lock::lock_project_by_id(…, IndexLockPolicy::maintenance_try(), LockDiagnostics::Silent)`
    it clears the projections, then runs `indexer::invalidate(conn, hub_machine, project_id)`
    (7.2), which deletes the hub machine's communities, file states and project
    state. The projections are cleared only for a project with a registry row, as
    Python does:
    - the graph through `commands::graph::clear_project_graph` (7.1);
    - vectors through `db::reset_vectors_sync_for_project` and
      `commands::vector::lifecycle_from_context(ctx)?.clear_project_vectors()`, the
      steps `gcode vector clear` takes.

    The context comes from `HubScope::context` with
    `ServiceConfigSelection::projection_cleanup()`. A failed clear keeps the selector,
    and the next pass reconciles it again. A busy lock skips the project for this
    pass. After an `unregistered` reconcile it sweeps orphans, as Python does, so a
    path-derived project's shared content goes in the same pass.
  - `active`: it reindexes. Under
    `index_lock::with_project_lock(ctx, IndexLockPolicy::brief_index_flush_try(), …)`
    it runs `commands::index::run_index_locked(ctx, IndexRequest { full: false, sync_projections: false, explicit_files: vec![], path_filter: None, .. })`,
    which is what `gcode index --project <root> --skip-if-locked` runs. The sync job
    (8.1) projects the files the run marks pending. A busy lock skips the project. The
    context uses `ServiceConfigSelection::database_only()`.
  - It returns `ProjectOutcome`: `Overlay`, `Reconciled`, `ReconcileBusy`,
    `ReconcileFailed`, `Indexed`, `IndexBusy` or `IndexFailed`, with the error where
    there is one. 8.3 runs summaries and labels after `Indexed`, `IndexBusy` and
    `IndexFailed`. Python summarizes and labels every active project, whatever its
    index run did.
- `sweep_orphans` lists `db::hub_maintenance::orphaned_index_projects` (indexed
  projects that no machine selects and no registry row owns). Each orphan, under its
  project lock, has its graph cleared and its vector collection dropped
  (`drop_project_collection`, as `gcode vector clear --drop-collection` does). Then
  `db::hub_maintenance::purge_index_project` deletes its `code_indexed_projects` row,
  whose foreign keys cascade to its files, symbols, calls, chunks and imports. A
  failed clear keeps the row for the next pass. An orphan has no state row and so no
  root. Its context takes an empty root, which neither clear reads.
- `reconcile_deleted` reconciles a soft-deleted project for every machine that selects
  it. Under `lock_project_by_id(…, IndexLockPolicy::maintenance_try(), LockDiagnostics::Silent)`
  it clears the graph and vectors as a reconcile does, then runs
  `indexer::invalidate(conn, machine, project_id)` for each machine in `machines`. Its
  context takes an empty root, as the sweep's does.
  - The clears reset every machine's files (`reset_graph_sync_for_project`,
    `reset_vectors_sync_for_project`). Removing only the hub machine's selector would
    leave the node machines' rows pending, and 8.1 and 4.3 would project them again.
  - It returns `DeletedOutcome`: `Reconciled`, `Busy`, or `Failed` with the error. A
    failed clear keeps every selector for the next pass.
  - It returns no `ProjectOutcome`, so 8.3 never summarizes or labels a soft-deleted
    project.
  - The registry row is purged after its soft-delete window
    (`src/gobby/projects/purge.py`). A node row written after that has no registry row,
    and the hub cannot tell it from a node's overlay. 8.5 (content GC for projects only
    nodes index) owns those rows.

`db::hub_maintenance` (`crates/gcode/src/db/hub_maintenance.rs`) holds
`registry_state` (`SELECT deleted_at FROM projects WHERE id = $1`),
`orphaned_index_projects` and `purge_index_project`, with the SQL of
`src/gobby/code_index/_storage/projects.py` (`get_registry_project`,
`list_orphaned_index_projects`, `purge_index_project`). It also holds
`deleted_selected_projects`: each project whose registry row has `deleted_at` set and
that some `code_indexed_project_states` row selects, with its selecting machines.

Locks: both reconciles and the sweep take the project lock exclusively, so a sync batch,
which holds it shared, or an index run cannot write the project while its selector or
row goes. That is 7.2's reason for the lock.

Deliberate differences from Python's pass:

- No `code_index_projection_cleanup_pending` retries. Python's reconcile records a
  failure there, with the store always `graph`, and retries only the graph clear.
  The selector stays after a failed reconcile, so the next pass retries the whole
  reconcile anyway. Python's operator prune still writes the table through
  `_reconcile_stale_selector` until P9, which deletes `prune.py` and drops the table.
- No purge on `GcodeProjectNotFoundError`. That error comes from a `gcode index`
  child resolving its project. In process, eligibility has already required the
  registry row and a matching `.gobby/project.json`, so a project that vanishes
  mid-pass is `unregistered` on the next one.
- Python's `missing_root_observations` map is never incremented, so it has no
  counterpart.
- In-process indexing cannot be killed, so the native pass does not read
  `code_index.maintenance_index_timeout_seconds`. The key stays the BM25 repair timeout
  (P8 framing).
- A soft-deleted project is reconciled for every machine that selects it. Python
  removes only the hub machine's selector, and it never syncs a node machine's row.

Python changes:

- `_run_maintenance` lists the hub machine's projects, keeps only the `active` ones,
  and summarizes and labels them. It no longer retries pending cleanups, sweeps
  orphans, reconciles or reindexes. The pending-cleanup retry functions go.
- `_reconcile_stale_selector`, `_sweep_orphaned_index_projects` and `_registry_state`
  stay, because `src/gobby/code_index/prune.py::CodeIndexPruner.prune_project` calls
  the first, which calls the other two. Until 8.4, Python's hourly prune and the native
  pass can both reconcile one stale selector, and until P9 Python's operator prune
  can. After 8.4 the native prune reconciles through this leaf's code too. Every
  reconcile is idempotent.
- `src/gobby/code_index/cleanup.py` goes.

`docs/guides/code-index.md` says gdaemon's maintenance job reconciles stale
selectors, purges orphaned projects and reindexes the hub's projects.

Research context:
- Python's pass: `src/gobby/code_index/maintenance.py::_run_maintenance`,
  `_reconcile_stale_selector`, `_sweep_orphaned_index_projects` and
  `_retry_pending_projection_cleanups`. The gateway's `maintenance_index` runs
  `gcode index --project <root> --skip-if-locked`. That run omits
  `--sync-projections`, so it writes PostgreSQL only.
- `commands/index.rs::run_index_locked(ctx, IndexRequest)` is `pub(crate)`, and
  `commands/repair.rs` already builds an `IndexRequest` in the crate.
  `commands/vector.rs::lifecycle_from_context` is `pub(crate)`. 7.1 makes
  `clear_project_graph` `pub(crate)`. None of the three needs an edit here.
- `code_indexed_projects` has only `id`, `created_at` and `updated_at`
  (`crates/gcore/assets/schema/baseline.sql`).
- `src/gobby/code_index/cleanup.py::purge_missing_project` had two callers:
  `sync_worker.py` (deleted in 8.1) and `_run_maintenance`'s purge branch.
- `code_index_projection_cleanup_pending` is read and written only by
  `src/gobby/code_index/maintenance.py` through
  `src/gobby/code_index/_storage/projection_cleanup.py`.
- `src/gobby/projects/purge.py::ProjectPurgeService._invalidate_code` runs
  `gcode invalidate` by id, which removes only the hub machine's rows.
- CI has no FalkorDB lane (#21045), so the tests reach a reconcile's clears only
  through their failure. P8's E1 gate checks the success path of both reconciles.
- Rejected alternatives:
  - a `gcode index` child, which needs the managed grant gdaemon cannot issue (P8
    framing);
  - porting the pending-cleanup retries, which repeat the next reconcile;
  - one leaf for the whole Python loop, because summaries and labels are an
    independent outcome with their own AI path (8.3).
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(hub_maintenance) | test(job_maintenance)'`;
  - `cargo nextest run -p gobby-codeindex`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_code_index_maintenance.py -v`.

**Granularity:** nine acceptance items. Reconcile, the orphan sweep and reindexing
are one outcome: Python's pass decides between them per project, and the leaf that
stops Python's half must start the native one (writer's choice 3).

Consumers unchanged:
- `src/gobby/code_index/_storage/projection_cleanup.py` — no-edit-reason: `_reconcile_stale_selector` still records failures through it until P9.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 8.2.1 - `orphaned_index_projects` returns an indexed project with no machine's selector and no registry row, and omits one with a registry row and one that a foreign machine selects. `purge_index_project` deletes the row and its files and symbols. `registry_state` reports a missing, a live and a deleted project. `deleted_selected_projects` returns a soft-deleted project with both machines that select it, and omits a live project and one with no registry row. test: `crates/gcode/src/db/hub_maintenance/tests.rs::orphans_have_no_selector_and_no_registry_row`.
- 8.2.2 - `eligibility` returns `overlay` for an unregistered directory whose derived id matches, `unregistered` when it does not match, `missing_root` for an empty or absent root, `identity_mismatch` for a differing `.gobby/project.json` id, and `active` otherwise. test: `crates/gcode/src/node/hub/job_maintenance/tests.rs::eligibility_follows_resolve_indexed_project`.
- 8.2.3 - For an unregistered project, `maintain_project` deletes the hub machine's communities, file states and project state and leaves a foreign machine's. With the project lock held by another connection it deletes nothing and returns `ReconcileBusy`. For a `missing_root` project with no Falkor configured it returns `ReconcileFailed` and keeps the selector. test: `crates/gcode/src/node/hub/job_maintenance/tests.rs::stale_selectors_reconcile_under_the_project_lock`.
- 8.2.4 - For an active project with a new file, `maintain_project` returns `Indexed` and leaves the file `graph_synced` and `vectors_synced` false. With the project lock held it returns `IndexBusy`. test: `crates/gcode/src/node/hub/job_maintenance/tests.rs::active_projects_reindex_without_projections`.
- 8.2.5 - The maintenance plan sweeps orphans only when `graph_enabled` and `embedding_enabled` both hold, keeps only the hub machine's rows, and runs at `maintenance_interval_seconds`, whose default equals the contract's. test: `crates/gcodeindex/src/jobs/tests.rs::maintenance_plans_the_sweep_and_hub_rows`.
- 8.2.6 - One maintenance run over an unregistered hub row, an active hub row and a foreign machine's row reconciles the first, indexes the second, and leaves the foreign row. test: `crates/gcodeindex/tests/job_maintenance.rs::maintenance_run_reconciles_and_reindexes_hub_rows`.
- 8.2.7 - `_run_maintenance` summarizes and labels active projects and calls no reconcile, sweep, pending-cleanup retry or gcode index. test: `tests/code_index/test_code_index_maintenance.py::test_run_maintenance_only_summarizes_and_labels`.
- 8.2.8 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 8.2.9 - For a soft-deleted project that the hub machine and a foreign machine select, `reconcile_deleted` returns `Busy` and deletes nothing while another connection holds the project lock. With no Falkor configured it returns `Failed` and keeps both selectors. test: `crates/gcode/src/node/hub/job_maintenance/tests.rs::deleted_projects_reconcile_for_every_machine`.

### 8.3 Symbol summaries and community labels [category: code] (depends: 8.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/db/hub_summaries.rs`
- `crates/gcode/src/db/hub_summaries/tests.rs`
- `crates/gcode/src/db/hub_labels.rs`
- `crates/gcode/src/db/hub_labels/tests.rs`
- `crates/gcode/src/db/mod.rs::*` — scope-reason: declares `hub_summaries` and `hub_labels`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/job_summaries.rs`
- `crates/gcode/src/node/hub/job_summaries/tests.rs`
- `crates/gcode/src/node/hub/job_labels.rs`
- `crates/gcode/src/node/hub/job_labels/tests.rs`
- `crates/gcodeindex/src/jobs/config.rs`
- `crates/gcodeindex/src/jobs/maintenance.rs`
- `crates/gcodeindex/src/jobs/tests.rs`
- `crates/gcodeindex/tests/job_maintenance.rs`
- `src/gobby/code_index/summarizer.py::*` — operation: delete — scope-reason: the native summary job replaces it
- `src/gobby/code_index/community_labeler.py::*` — operation: delete — scope-reason: the native label job replaces it
- `src/gobby/code_index/summary_safety.py::*` — operation: delete — scope-reason: ported to `job_summaries`, and its last importers go
- `src/gobby/code_index/community_label_safety.py::*` — operation: delete — scope-reason: ported to `job_labels`, and its last importer goes
- `src/gobby/code_index/_storage/summaries.py::*` — operation: delete — scope-reason: ported to `db::hub_summaries`
- `src/gobby/code_index/_storage/communities.py::*` — operation: delete — scope-reason: ported to `db::hub_labels`
- `src/gobby/code_index/storage.py::*` — scope-reason: `CodeIndexStorage` drops the summary and community mixins
- `src/gobby/code_index/models.py::*` — scope-reason: deletes `StoredCommunity`, whose last readers go
- `src/gobby/code_index/maintenance.py::*` — scope-reason: deletes the loop, `_run_maintenance` and the summary and label helpers; `_registry_state`, `_reconcile_stale_selector` and `_sweep_orphaned_index_projects` stay for `prune.py`'s operator prune until P9
- `src/gobby/runner_startup_code_index.py::*` — scope-reason: deletes `_start_code_index_tasks`, and `_repair_code_index_bm25`'s docstring and failure line name no worker
- `src/gobby/runner.py::*` — scope-reason: drops `GobbyRunner`'s four code-index task and event annotations
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: `init_subsystems` stops starting the code-index tasks, and `_check_embedding_service` and `_initialize_vector_store` move out
- `src/gobby/runner_startup_vectors.py`
- `src/gobby/runner_lifecycle_shutdown.py::*` — operation: delete-lines — base-blob: 0909c6a910d75e89971a4591ead22e00713c371d — lines: 242-259, 261, 817-819 — scope-reason: removes the code-index drain and its two calls, because no code-index task is left to drain
- `tests/code_index/test_summarizer.py::*` — operation: delete — scope-reason: tests the deleted summarizer
- `tests/code_index/test_community_labeler.py::*` — operation: delete — scope-reason: tests the deleted labeler
- `tests/test_runner_code_index_shutdown.py::*` — operation: delete — scope-reason: tests the deleted start and drain
- `tests/code_index/test_maintenance_config_capture.py::*` — scope-reason: drops the maintenance-loop test and its imports, and the schema-sweep test stays
- `tests/code_index/test_code_index_maintenance.py::*` — scope-reason: drops the tests of the deleted loop, `_run_maintenance` and summary helpers
- `tests/code_index/test_code_index_storage.py::*` — scope-reason: drops the summary and community storage tests, and the remaining tests set summaries and attempts with SQL
- `tests/code_index/test_gcode_storage_conformance.py::*` — scope-reason: sets the fixture's summary with SQL
- `tests/test_bm25_startup.py::*` — scope-reason: deletes the worker-gate test, because the gate goes
- `tests/test_runner_lifecycle.py::*` — scope-reason: two init tests stop patching `_start_code_index_tasks`
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: the phase recorder and the every-phase test drop `_start_code_index_tasks`
- `tests/terminals/test_host_manager.py::*` — scope-reason: a shutdown test stops patching `_stop_code_index_workers`
- `docs/guides/code-index.md`
- `docs/guides/llm-features.md`
- `docs/contracts/truncation.md`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

Where it runs. The maintenance job (8.2) gains two steps for each hub row whose
`maintain_project` returns `Indexed`, `IndexBusy` or `IndexFailed`, in Python's order:

1. when `code_index.symbol_summary.enabled` holds,
   `node::hub::job_summaries::summarize_project(&HubScope, root, SummaryRequest { batch_size, max_concurrency }, generate)`;
2. when `code_index.community_label.enabled` holds,
   `node::hub::job_labels::label_project(&HubScope, LabelRequest { batch_size, max_concurrency, profile }, generate)`.

Each logs its report or error with the project to stderr, and the run moves on. A
soft-deleted project gets neither, because `reconcile_deleted` returns no
`ProjectOutcome` (8.2).

`JobConfig` gains `code_index.symbol_summary.*` and `code_index.community_label.*`:
`enabled`, `batch_size`, `max_concurrency`, `max_tokens`, `profile` and `candidates`.
These keys are `activation: restart_required`, unlike the live keys of the P8 framing.
`active_values` keeps a restart-required key at the value Python started with
(`src/gobby/config/runtime_activation.py`), so the native job sees a change after
Python restarts, as Python's loop does today.

Generation. Both jobs take
`generate: &(dyn Fn(&str) -> anyhow::Result<String> + Sync)`, so tests inject a fake.
The production closure is `job_summaries::daemon_generator(project_id, candidates, profile, max_tokens)`,
compiled under gcode's `ai` feature, which gcodeindex's default `gobby-code` dependency
enables. For each prompt it:

- resolves a fresh `AiContext::resolve(Some(project_id), &mut LocalAiConfigSource::empty())`,
  as `crates/gcode/src/vector/code_symbols/embedding.rs` does. One shared context would
  run every call one at a time, because its `AiLimiter` holds `ai.max_concurrency`,
  which an empty source leaves at 1;
- with a non-empty `candidates` list, calls
  `generate_via_daemon_with_candidates(&ctx, prompt, None, Some(max_tokens), &candidates, GenerationBudget::Interactive)`,
  each candidate as `FeatureCandidate { candidate, reasoning_effort: None }`;
- with an empty list, calls
  `generate_via_daemon_with_max_tokens(&ctx, prompt, None, Some(max_tokens), Some(profile), GenerationBudget::Interactive)`,
  because Python falls back to the profile's candidates then;
- returns the result's `text`.

Each job runs up to its `max_concurrency` generations at once on `std::thread::scope`
threads. Python bounds its calls with a semaphore of that size.

Summaries (`node::hub::job_summaries`):

1. `db::hub_summaries::unsummarized_symbols(conn, machine_id, project_id, batch_size)`
   returns the hub machine's queue.
2. Each symbol's source is lines `line_start..=line_end` (1-indexed) of the file at the
   hub root, read with `String::from_utf8_lossy` and split at `\n`. A missing or
   unreadable file, or an empty source, skips the symbol without a stamp, as Python's
   `summarize_batch` does.
3. The prompt is Python's `_SUMMARY_PROMPT`, verbatim, with kind, name, signature (empty
   when absent) and source. The source is cut to `_MAX_SOURCE_CHARS` (2000). A longer
   source adds Python's truncation marker:
   `\n[truncated: rendered <n> chars from first 2000 of <len>; full source: <file>:<start>-<end>]`.
4. A success writes the sanitized summary through
   `db::hub_summaries::update_symbol_summary`, which matches the content hash. A
   generation error, or an output that sanitizes to nothing, is stamped through
   `mark_summaries_attempted`.
5. `SummaryReport` counts written, skipped and failed symbols.

The sanitizers port `src/gobby/code_index/summary_safety.py` into `job_summaries`:

- `sanitize_source_for_summary_prompt(source, max_chars)` keeps the first `max_chars`
  characters, turns each backtick into `'` and drops NUL.
- `sanitize_symbol_summary(text)` keeps the text before the first ```` ``` ````, turns
  backticks into `'`, drops NUL, collapses whitespace runs to one space, cuts to 500
  characters and trims the end. An empty result is `None`.

Counts and cuts are in `char`s, as Python's `str` counts code points.

Labels (`node::hub::job_labels`):

1. `db::hub_labels::unlabeled_communities(conn, machine_id, project_id, batch_size)`
   returns the hub machine's queue.
2. The prompt is Python's `_LABEL_PROMPT`, verbatim, which asks for JSON. Its data block
   follows `_label_prompt`: `member_count`, `deterministic_label`, `label_candidates`,
   `representatives`, then `members:` with the representatives, then the other members
   sorted, cut to 40. The block passes through `sanitize_source_for_summary_prompt` at
   8000 characters.
3. `parse_label(text)` strips one surrounding code fence and parses a JSON object. It
   needs a string `name` of 3 to 40 characters and a string `rationale` of at most 120,
   the bounds of Python's `_LABEL_SCHEMA`. Anything else is `SchemaRejected`. A
   generation error is `GenerationFailed`.
4. A name equal to the community's deterministic label or one of its candidates is
   written with source `deterministic`. Any other name passes
   `sanitize_community_label(name, members)`, a port of
   `src/gobby/code_index/community_label_safety.py`:
   - strip a fence with `\A```[^\n]*\n` and `\n?```\s*\Z`, then surrounding quotes;
   - 2 to 5 words and at most 40 characters;
   - only `[A-Za-z0-9 /&+.-]`;
   - no source extension (`\.(py|pyi|ts|tsx|js|mjs|cjs|css|rs|sh)\b`, ignoring case);
   - not a member path.

   A name that passes is written with source `model`, and one that fails is
   `SanitationRejected`.
5. Writes go through `db::hub_labels::update_community_label` with `label_model` set to
   the profile and `label_confidence` NULL, as Python writes them. The write matches the
   member signature. A write that loses to a newer partition is skipped without a
   stamp, because the new row is queued afresh.
6. Every outcome other than a write is stamped through `mark_labels_attempted`.
   `LabelReport` counts each outcome, and the job logs it as Python's
   `code_index.community_label.batch` line does.

`db::hub_summaries` and `db::hub_labels` hold the SQL of
`src/gobby/code_index/_storage/summaries.py` and `_storage/communities.py` with an
explicit machine:

- `unsummarized_symbols`: `function`, `class` and `method` symbols of the machine's
  selected file versions with no summary and no attempt within the 300 s cooloff,
  newest `updated_at` first;
- `update_symbol_summary` and `mark_summaries_attempted`, both matching the content
  hash;
- `unlabeled_communities`: the machine's communities whose `labeled_signature` differs
  from `member_signature`, outside the cooloff, largest first;
- `update_community_label` and `mark_labels_attempted`, both matching the member
  signature.

Deliberate differences from Python:

- No provider JSON schema. `/api/llm/generate` takes none, so the job parses the text
  and applies the schema's bounds itself. Python's provider-structured call rejects the
  same shapes.
- Python's text service records the callers `code_index.symbol_summary` and
  `code_index.community_label`. The route records `llm-generate-route` for both.
- The calls use the `Interactive` budget: 30 s per candidate and 60 s per CLI candidate,
  with the route capping the total.
- With Python down, a call fails at the transport, and the symbol or community is
  stamped and retried after the cooloff. Python's in-process failure is stamped the same
  way.
- Source lines split at `\n`, as the indexer counts them. Python's `splitlines` also
  splits at `\r`, form feeds and Unicode separators.

Python deletion:

- `summarizer.py`, `community_labeler.py`, `summary_safety.py`,
  `community_label_safety.py`, `_storage/summaries.py` and `_storage/communities.py`
  go. `CodeIndexStorage` drops both mixins, and `models.py` drops `StoredCommunity`.
- `maintenance.py` loses `code_index_maintenance_loop`, `_run_maintenance`,
  `_label_unlabeled_communities`, `_summarize_unsummarized`, `_read_symbol_sources`,
  `_update_symbol_summaries`, `_read_symbol_source` and `_SUMMARY_DB_WRITE_CONCURRENCY`.
  `_registry_state`, `_reconcile_stale_selector` and `_sweep_orphaned_index_projects`
  stay for `prune.py`'s operator prune until P9.
- `runner_startup_code_index.py` loses `_start_code_index_tasks`, Python's last
  code-index task. `_repair_code_index_bm25` keeps its `bool` return, which
  `tests/config/test_runtime_loop_consumers.py` and `tests/test_bm25_startup.py`
  assert. Its failure line reads "Code-index BM25 recovery failed: %s. Run `gobby
  postgres repair-code-index`, then restart Gobby." and its docstring names no worker.
- `init_subsystems` awaits the BM25 phase without keeping its result, and the
  `_start_code_index_tasks` call and its import go.
- `GobbyRunner` drops the `_code_index_task`, `_code_index_shutdown`,
  `_sync_worker_task` and `_sync_worker_shutdown` annotations. Nothing else sets or
  reads them once the start and the drain go.
- `runner_lifecycle_shutdown.py` loses `_stop_code_index_workers` (lines 242-259 with
  its trailing blank lines), its call in `_cancel_periodic_tasks` (261), and its call
  and comment in `shutdown_daemon_services` (817-819). The file is 916 lines, and this
  is a pure deletion, so it carries the `delete-lines` proof in Targets.

Split. `src/gobby/runner_lifecycle_subsystems.py` is 883 lines. This leaf moves
`_check_embedding_service` and `_initialize_vector_store` (lines 99-137 and 168-218),
with the imports only they use, to the new `src/gobby/runner_startup_vectors.py`. The
new module's logger is `logging.getLogger("gobby.runner_lifecycle")`, so log capture
in tests still matches. `runner_lifecycle_subsystems.py` imports both names, so
`init_subsystems` calls them through its module globals, and every
`patch.object(runner_lifecycle_subsystems, …)` site keeps working.

Tests:

- `test_summarizer.py`, `test_community_labeler.py` and
  `tests/test_runner_code_index_shutdown.py` go.
- `test_maintenance_config_capture.py` keeps
  `test_schema_sweep_applies_captured_database_url_on_the_next_pass`.
- `test_code_index_storage.py` drops the community tests and the
  `get_unsummarized_symbols`, `update_symbol_summary` and
  `mark_symbol_summaries_attempted` tests, with the `StoredCommunity` and
  `SUMMARY_MAX_CHARS` imports. `test_upsert_nulls_summary_on_hash_change`,
  `test_upsert_preserves_summary_on_same_hash` and
  `test_upsert_symbols_resets_summary_attempt_on_content_change` set the summary or
  the attempt with SQL. `test_stale_content_hash_rejects_sync_marks_and_summary` drops
  its summary assertion, which `db::hub_summaries` now covers.
- `test_gcode_storage_conformance.py` sets its fixture's summary with SQL.
- `test_code_index_maintenance.py` keeps only the reconcile and sweep tests, until P9.
- `test_bm25_startup.py` deletes
  `test_init_subsystems_skips_code_index_workers_after_failed_repair`.
- `test_runner_lifecycle.py`, `test_runner_lifecycle_subsystems.py` and
  `tests/terminals/test_host_manager.py` drop their `_start_code_index_tasks` and
  `_stop_code_index_workers` patches. `test_runner_lifecycle.py`'s ordered events drop
  `"code-index"`.

Docs:

- `docs/guides/code-index.md` says gdaemon's maintenance job writes symbol summaries
  and community labels through the hub's `/api/llm/generate`.
- `docs/guides/llm-features.md`'s `code_index.symbol_summary` row and the paragraph
  under the table name the native job. A `code_index.community_label` row is added.
- `docs/contracts/truncation.md` names `job_summaries::sanitize_symbol_summary` for the
  500-character summary bound.

Research context:
- Python's summaries: `src/gobby/code_index/maintenance.py::_summarize_unsummarized`
  reads sources with `_read_symbol_source` (1-indexed lines, `None` for a missing file)
  and stamps only symbols that had a source.
  `src/gobby/code_index/summarizer.py::SymbolSummarizer.summarize_one` sends the
  profile and candidates with caller `code_index.symbol_summary`, and returns `None` on
  any error.
- Python's labels: `src/gobby/code_index/community_labeler.py::CommunityLabeler`.
  `_generate_one` calls `generate_json` with `_LABEL_SCHEMA`. `_write_label` returns
  `None` when the signature-guarded write loses, and `label_batch` stamps every outcome
  that wrote nothing.
- `src/gobby/ai/_text_generation_service.py::_candidate_requests` uses the request's
  candidates when the list is non-empty, else the profile's. With candidates, the
  profile only fills a missing reasoning effort (`src/gobby/config/feature_base.py::candidate_runtime_entries`),
  and `_DEFAULT_PROFILE_REASONING` is `auto` for every profile. The adapters treat a
  missing effort as `auto`, so dropping the profile changes nothing.
- `crates/gcore/src/ai/daemon/operations.rs::generate_via_daemon_with_candidates`
  sends no profile, and `generate_via_daemon_with_max_tokens` sends one. Both return a
  `TextResult` whose `text` is the output.
- `crates/gcore/src/ai_context.rs`: `AiContext::resolve` builds its `AiLimiter` from
  `ai.max_concurrency`, which defaults to 1 (`config/resolve.rs::AI_DEFAULT_MAX_CONCURRENCY`).
- The contract's default `candidates` lists are non-empty
  (`crates/gcore/assets/config/runtime_config_contract.json`).
- `src/gobby/servers/routes/llm.py` records caller `llm-generate-route` and accepts no
  JSON schema.
- The jobs run on the maintenance job's own thread (P8 framing), so the blocking HTTP
  client never runs on a tokio worker.
- Rejected alternatives:
  - adding a schema field to `/api/llm/generate`, a Python route outside this plan,
    when local parsing gives the same bounds;
  - one shared `AiContext`, whose limiter serializes the calls;
  - one leaf per feature: both run in one Python loop through one generator seam, and
    the loop, the start function and the drain go once.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(hub_summaries) | test(hub_labels) | test(job_summaries) | test(job_labels)'`;
  - `cargo nextest run -p gobby-codeindex`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_code_index_storage.py tests/code_index/test_gcode_storage_conformance.py tests/code_index/test_code_index_maintenance.py tests/code_index/test_maintenance_config_capture.py tests/test_bm25_startup.py tests/test_runner_lifecycle.py tests/test_runner_lifecycle_subsystems.py tests/test_runner_backlog.py tests/terminals/test_host_manager.py tests/config/test_runtime_loop_consumers.py -v`.

**Granularity:** eleven acceptance items. Summaries and labels are one outcome: one
Python loop runs both through one generator seam, and the leaf that stops it must
start both native halves (writer's choice 3). Deleting the loop removes Python's last
code-index task, so the start function, the runner attributes and the drain go in the
same leaf.

Consumers unchanged:
- `tests/test_runner_backlog.py` — no-edit-reason: patches `_check_embedding_service` and `_initialize_vector_store` on `runner_lifecycle_subsystems`, which still imports both.
- `tests/config/test_runtime_loop_consumers.py` — no-edit-reason: asserts `_repair_code_index_bm25`'s `bool` return, which stays.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 8.3.1 - `unsummarized_symbols` returns the machine's selected versions' function, class and method symbols with no summary, skips a symbol within 300 s of an attempt and one of a foreign machine's version, orders by newest `updated_at` and honors `limit`. `update_symbol_summary` and `mark_summaries_attempted` change nothing under a stale content hash. test: `crates/gcode/src/db/hub_summaries/tests.rs::summary_queue_follows_the_python_storage`.
- 8.3.2 - `unlabeled_communities` returns the machine's communities whose labeled signature differs from the member signature, outside the cooloff, largest first. `update_community_label` and `mark_labels_attempted` change nothing under a stale member signature. test: `crates/gcode/src/db/hub_labels/tests.rs::label_queue_and_writes_are_signature_guarded`.
- 8.3.3 - On fixtures with backticks, NUL, a fence, whitespace runs, a 600-character summary and a multi-byte character at the cut, `sanitize_source_for_summary_prompt` and `sanitize_symbol_summary` return Python's `summary_safety` results, and an all-whitespace summary is `None`. test: `crates/gcode/src/node/hub/job_summaries/tests.rs::sanitizers_match_summary_safety`.
- 8.3.4 - With a fake generator, `summarize_project` sends nothing for a symbol whose file is missing and leaves it unstamped, stamps a symbol whose call fails and one whose output sanitizes to nothing, writes the sanitized summary on success, adds the truncation marker to a source over 2000 characters, and never runs more than `max_concurrency` calls at once. test: `crates/gcode/src/node/hub/job_summaries/tests.rs::summaries_stamp_failures_and_skip_missing_files`.
- 8.3.5 - `parse_label` accepts a fenced JSON object, and rejects non-JSON text, a name of 2 or 41 characters, a 121-character rationale and a non-string field. `sanitize_community_label` returns Python's `community_label_safety` results on fixtures for quotes, word count, charset, a source extension and a member path. test: `crates/gcode/src/node/hub/job_labels/tests.rs::label_parsing_and_safety_match_python`.
- 8.3.6 - With a fake generator, `label_project` writes a candidate name with source `deterministic`, writes a new safe name with source `model`, the profile as `label_model` and NULL confidence, stamps an unsafe name, a rejected shape and a failed call, and neither writes nor stamps a community whose member signature changed before the write. test: `crates/gcode/src/node/hub/job_labels/tests.rs::label_outcomes_follow_the_python_labeler`.
- 8.3.7 - The maintenance plan summarizes and then labels after `Indexed`, `IndexBusy` and `IndexFailed`, does neither after `Overlay` or any reconcile outcome, and drops each step when its `enabled` key is false. test: `crates/gcodeindex/src/jobs/tests.rs::maintenance_summarizes_then_labels_indexed_rows`.
- 8.3.8 - Every `symbol_summary` and `community_label` default in `JobConfig` equals the contract's, and the default `candidates` lists are non-empty. test: `crates/gcodeindex/src/jobs/tests.rs::config_defaults_match_the_runtime_contract`.
- 8.3.9 - One maintenance run over an active hub row with a queued symbol and no daemon to reach stamps that symbol attempted and leaves its summary NULL. test: `crates/gcodeindex/tests/job_maintenance.rs::maintenance_run_stamps_summaries_when_generation_fails`.
- 8.3.10 - In standalone and hub mode, `init_subsystems` runs every phase and starts no code-index task. test: `tests/test_runner_lifecycle_subsystems.py::test_standalone_and_hub_run_every_phase`.
- 8.3.11 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 8.4 Hourly prune and `POST /api/code-index/prune` [category: code] (depends: 8.3)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/status/content_gc.rs::prune_content_versions`
- `crates/gcode/src/commands/status/prune.rs::*` — scope-reason: `prune_project_scoped`'s steps after authorization move into `apply_project_prune`, and the new `prune_project_unattended` runs them for the hub
- `crates/gcode/src/commands/status.rs`
- `crates/gcode/src/daemon.rs::*` — scope-reason: `GlobalPruneOutcome` derives `Serialize`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/job_maintenance.rs`
- `crates/gcode/src/node/hub/job_prune.rs`
- `crates/gcode/src/node/hub/job_prune/tests.rs`
- `crates/gcodeindex/src/jobs.rs`
- `crates/gcodeindex/src/jobs/config.rs`
- `crates/gcodeindex/src/jobs/prune.rs`
- `crates/gcodeindex/src/jobs/tests.rs`
- `crates/gcodeindex/src/family.rs`
- `crates/gcodeindex/src/family/prune.rs`
- `crates/gcodeindex/tests/family_prune.rs`
- `src/gobby/code_index/prune.py::*` — scope-reason: deletes the hourly half (`prune_all_projects`, `_clear_dirty_projects`, `create_code_index_prune_handler`, `register_code_index_prune_cron`, `CronRegistrationProtocol`, `CodeIndexPruneResult` and the job's name, handler, interval and description constants), and the operator half stays for Python's route until P9
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: `init_orchestration` still builds `CodeIndexPruner` for the route and registers no prune cron handler
- `src/gobby/storage/cron.py::*` — scope-reason: the job-name constants and `is_removed_automation_job` move to `cron_job_names.py`
- `src/gobby/storage/cron_job_names.py`
- `src/gobby/scheduler/scheduler.py::*` — scope-reason: imports `is_removed_automation_job` from `gobby.storage.cron_job_names`
- `src/gobby/servers/routes/cron.py::*` — scope-reason: imports `is_removed_automation_job` from `gobby.storage.cron_job_names`
- `tests/code_index/test_prune.py::*` — scope-reason: drops the `prune_all_projects` and cron registration tests and the imports only they use
- `tests/storage/test_cron.py::*` — scope-reason: adds the removed-row test for `gobby:code-index-prune`
- `docs/guides/code-index.md`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

Scope. Python's pruner has two halves. The hourly half is the cron row
`gobby:code-index-prune`, whose handler runs `CodeIndexPruner.prune_all_projects`. The
operator half is `run_operator_global_prune`, which Python's `POST /api/code-index/prune`
calls. The `code_index` family proxies until P9 (P5 framing), so Python's route keeps
answering by default through P8. This leaf therefore deletes only the hourly half. It
adds the native hourly job and the native route, which serves under
`front_door.routes.code_index: native` or `compare`. P9 deletes the operator half with
the rest of `src/gobby/code_index/` (D15).

`node::hub::job_prune` (new, declared in `node/hub.rs`):

- A process-wide `static GLOBAL_PRUNE: Mutex<()>` serializes the hourly job and the
  route, as Python's `_global_lock` does. The guard holds no data, so a poisoned lock is
  taken over.
- `prune_all(database_url, hub_machine, retention_days, entry) -> anyhow::Result<Option<GlobalPruneOutcome>>`.
  With `PruneEntry::Skip` it returns `None` at once while another prune holds the
  lock, as `prune_all_projects` does. With `PruneEntry::Wait` it waits, as
  `run_operator_global_prune` does. A failure to list the rows is the `Err`.
- `GlobalPruneOutcome` is `crate::daemon::GlobalPruneOutcome`, re-exported here. It
  gains `Serialize`, so the route's body and `gcode prune`'s parser share one type:
  `completed` lists project ids, and `failed` and `skipped` list
  `{"project_id": …, "reason": …}` objects.

`prune_all` takes the hub machine's rows from `job_sync::sync_rows` (8.1) and calls
`db::hub_maintenance::deleted_selected_projects` (8.2) once for the soft-deleted
projects' machines. Then, row by row, in Python's order:

1. An empty root is skipped with reason `missing_root`.
2. A file pending for the hub machine, from `db::pending_graph_files` (8.1) or
   `db::pending_vector_files` (4.2) with `limit` 1, puts the project in `completed`
   with nothing pruned. Python returns `deferred_pending_sync` here and counts it
   completed.
3. `db::hub_maintenance::registry_state` and `job_maintenance::eligibility` (8.2)
   classify the project:
   - `overlay`: skipped with reason `skipped_overlay`;
   - a soft-deleted registry row: `job_maintenance::reconcile_deleted` for every
     machine that selects it (8.2). `Reconciled` is completed, `Busy` is skipped with
     reason `skipped_locked`, and `Failed` is failed;
   - `unregistered`, `missing_root` or `identity_mismatch`:
     `job_maintenance::reconcile_stale` (below). `Reconciled` is completed,
     `ReconcileBusy` is skipped with reason `skipped_locked`, and `ReconcileFailed`
     is failed;
   - `active`: the prune. Its context comes from `HubScope::context` at the row's
     root with `ServiceConfigSelection::projection_cleanup()`, and it runs
     `commands::status::prune_project_unattended(&ctx, retention_days, Some(now + 90 s))`.
     The 90 s deadline is Python's `CODE_INDEX_PRUNE_TIME_BUDGET_SECONDS`. `Ok` is
     completed, and an error is failed.
4. A failed project's entry is `{"project_id": id, "reason": "<id>:failed"}`, the
   string Python's `prune_project` returns. Its error goes to stderr with the project.
   A query error on one row fails that row, and the next row runs.

`job_maintenance` gains `pub(crate) fn reconcile_stale(scope, root, eligibility, registry) -> ProjectOutcome`.
It is `maintain_project`'s reconcile branch, moved unchanged, and `maintain_project`
now calls it with the registry state it already read. It returns `Reconciled`,
`ReconcileBusy` or `ReconcileFailed`. Python's prune also reconciles through
`_reconcile_stale_selector`, the function its maintenance pass uses.

gcode's prune:

- `prune_content_versions(services, candidates, deadline, resolve_project_services)`
  takes the per-candidate context resolver that it now builds itself. That resolver
  is `Context::resolve_for_project_id_with_services`, which acquires a grant, and
  gdaemon has no grant issuer (P8 framing). `prune_content_versions_with` already
  takes a resolver, so only the wrapper changes.
- `prune_project_scoped`'s steps after authorization move into
  `apply_project_prune(&discovery, deadline, resolve, print)`. It reconciles the
  project's stale records, runs content GC, prints the two totals lines only when
  `print` holds, and bails with the CLI's message when either step failed. The CLI
  passes `print` true and the grant resolver, so its output does not change.
- `pub(crate) fn prune_project_unattended(ctx, retention_days, deadline)` runs
  `discover_project_scoped_records(ctx, retention_days)`, then
  `apply_project_prune` with `print` false and `|_| Ok(ctx.clone())` as the resolver.
  Discovery keeps only the context's project, so every candidate shares its context.
  `crates/gcode/src/commands/status.rs` re-exports it.

The hourly job (`crates/gcodeindex/src/jobs/prune.rs`) is `code_index_prune` in
`HubJobs::ticks()`:

- Its interval is the constant `PRUNE_INTERVAL` (3600 s), Python's
  `CODE_INDEX_PRUNE_INTERVAL_SECONDS`. The cron row's interval had no config key.
- Each run, on its own thread, calls
  `prune_all(database_url, hub_machine, config.content_retention_days, PruneEntry::Skip)`.
  It logs `code_index_prune: completed=<n> failed=<n> skipped=<n>` to stderr, or
  `code_index_prune: skipped: global_locked`, or the error.
- `JobConfig` gains `code_index.content_retention_days` (live, default 1). Python's
  hourly run also takes it from config and passes `force=True`.

`POST /api/code-index/prune` (`crates/gcodeindex/src/family/prune.rs`), a new
`NATIVE_ROUTES` row with `callers: &[Caller::Operator]`, in Python's order:

1. The `enabled` gate: 503 `{"detail":"Code index pruner not available"}`. Python's
   pruner is `None` exactly when the daemon has no code indexer.
2. The body, optional, as Python's `GlobalPruneRequest | None`:
   - an absent body or `null` takes the defaults;
   - `force` is a boolean and `retention_days` an integer of at least 1 or `null`;
   - invalid JSON, a value of the wrong type and a `retention_days` below 1 return
     Python's 422 bodies, recorded once from the Python route under `TestClient`, as
     7.2's body checks are.
3. `retention_days` defaults to `code_index.content_retention_days`, read through
   `jobs::config::JobConfig::read`, as the job reads it.
4. The work runs on a named `std::thread` with `PruneEntry::Wait`, and the handler
   awaits a `tokio::sync::oneshot`. A global prune can run for minutes, and
   `serve::run`'s runtime waits for every `spawn_blocking` task when it drops (P8
   framing). This route alone departs from P5's `spawn_blocking` rule.
5. An outcome returns 200 with its compact JSON, which is Python's `JSONResponse`
   body. An `Err` returns 500 `{"detail":"Code index prune failed"}` and is logged to
   gdaemon's stderr.

Python changes:

- `prune.py` loses the hourly half and the imports only it uses. `CodeIndexPruner`,
  `run_operator_global_prune`, `prune_project` and their helpers stay for Python's
  route until P9.
- `init_orchestration` still builds `runner.code_index_pruner`, and it no longer
  registers the cron handler. Its log and degraded-service lines name the pruner.
- `REMOVED_AUTOMATION_JOB_NAMES` gains `gobby:code-index-prune`, and
  `CRON_JOB_NAME_PRIORITIES` drops it. The scheduler deletes a removed system row and
  its runs when it starts (`delete_removed_automation_jobs`), skips it when due,
  rejects a manual run and refuses to enable it, and the cron listings hide it.
  `RETIRED_SYSTEM_CRON_JOBS` would only disable the row, which would still list a
  control whose run has no handler.

Split. `src/gobby/storage/cron.py` is 867 lines. This leaf moves its job-name
constants (`REMOVED_AUTOMATION_JOB_NAMES`, `CODEWIKI_NIGHTLY_JOB_PREFIX`,
`RETIRED_AUTOMATION_JOB_NAME_PREFIXES`, `CRON_JOB_NAME_PRIORITIES`,
`DEFAULT_CRON_JOB_PRIORITY`) and `is_removed_automation_job` to the new
`src/gobby/storage/cron_job_names.py`. `cron.py` imports what it uses.
`src/gobby/scheduler/scheduler.py` and `src/gobby/servers/routes/cron.py` import
`is_removed_automation_job` from the new module. No test imports the moved names.

Tests:

- `test_prune.py` drops `test_global_prune_runs_in_process_and_clears_dirty_projects`,
  `test_global_prune_failure_leaves_project_dirty`,
  `test_global_prune_force_and_retention_reach_prune_project`,
  `test_global_prune_held_lock_returns_successful_skip` and the three
  `test_register_code_index_prune_cron_*` tests. The forced half of the third
  repeats `test_operator_prune_skips_locked_unless_forced` and
  `test_operator_prune_forwards_retention_days`, which stay.
- `tests/storage/test_cron.py` adds the removed-row test.

`docs/guides/code-index.md` says gdaemon's hourly job runs the global prune on the
hub. Its route section still describes Python's route, which serves until P9.

Deliberate differences from Python:

- `force` is accepted and ignored. Python's `force` waits 5 s on an in-process
  per-project lock that only another Python prune holds. Native prunes serialize under
  `GLOBAL_PRUNE`, and a busy PostgreSQL project lock skips the project, as `gcode
  prune` does.
- No 504 and no 400. Both come from the `gcode` child: its timeout and its input
  validation. The 120 s kill has no counterpart, and content GC stops at the 90 s
  deadline between versions.
- No hub delete for a pruned project whose root has gone. Python deletes the project
  state when the root vanished during the run. Natively, eligibility has just required
  the root, and the next maintenance pass reconciles a vanished root as
  `missing_root` (8.2).
- No prune-dirty bookkeeping. Python records a failed project in
  `code_index_prune_dirty_projects` and clears the table after a clean hourly run.
  `prune_dirty_projects`, its only reader, has no caller. P9 drops the table with
  `prune.py`.
- Python's model coerces string booleans and numeric strings. The native route returns
  422 for them. `gcode prune` sends a JSON boolean and integer.
- Disabling the cron row no longer stops the hourly prune, and `code_index.enabled`
  does.
- The job logs to gdaemon's stderr. Python wrote `global_prune` events to
  `~/.gobby/logs/code-index-maintenance.log`.
- A soft-deleted project is reconciled for every machine that selects it (8.2).
- Rows run in `sync_rows` order. Python lists them by `last_indexed_at` descending, so
  only the order inside the response's lists can differ. `gcode prune` counts them.
- Until P9, Python's operator prune and the native hourly job can run at once, under
  separate global locks. Each takes the project's PostgreSQL lock for its writes, so
  one of them skips a project the other holds.

Research context:
- Python's hourly half: `src/gobby/code_index/prune.py::CodeIndexPruner.prune_all_projects`
  returns a `skipped` result while `_global_lock` is held, else runs the operator
  prune with `force=True` and the configured retention, logs a `global_prune` event
  and clears the prune-dirty rows after a clean run.
  `register_code_index_prune_cron` registers the system row `gobby:code-index-prune`
  (handler `code-index:prune`, interval 3600 s).
- Python's operator half: `_run_operator_global_prune_locked` snapshots
  `list_indexed_projects` (the hub machine's rows, `last_indexed_at` descending) and
  maps each `prune_project` suffix: `pruned`, `deferred_pending_sync` and
  `reconciled` are completed; `skipped_locked`, `skipped_missing_root` and
  `skipped_overlay` are skipped; anything else is failed with the whole outcome as
  its reason, and the project is marked prune-dirty.
- `prune_project` takes the per-project lock, defers on `get_pending_sync_files`
  (limit 1, both targets, 300 s cooloff), resolves eligibility, reconciles a stale
  project through `_reconcile_stale_selector`, and runs
  `gcode --project <root> prune --force` through the gateway with 90 s of work and a
  120 s timeout.
- `src/gobby/servers/routes/code_index.py::create_code_index_router`'s `global_prune`
  takes `GlobalPruneRequest { force: bool = False, retention_days: int | None (ge=1) }`
  and returns 503 when `code_index_pruner` is `None` or `gcode` is unusable, 504 on a
  gcode timeout, 400 on gcode input validation and 500 otherwise.
- `crates/gcode/src/commands/status/prune.rs::prune_project_scoped` resolves its
  context, discovers the project's stale records and content GC candidates,
  authorizes, reconciles, runs content GC, prints both totals and bails on failures.
  `content_gc.rs::prune_content_versions` is its only caller of content GC, and
  `content_gc/tests.rs` calls `prune_content_versions_with` with injected closures.
- `status/shared.rs::collect_projects_from` and `content_gc.rs::discover_content_gc`
  read `read_local_machine_id`, which in gdaemon is the hub's own machine. The tests
  use the local machine as the hub machine.
- Content GC keeps a version whose synced projection's store is not configured
  (`content_gc.rs`'s "retaining content version" branch). CI has no Falkor or Qdrant
  (#21045), so the tests' pruned versions have `graph_synced` and `vectors_synced`
  false.
- `crates/gcode/src/daemon.rs::post_code_index_prune` posts `force` and
  `retention_days` to this route, and `prune_global_with` counts the three lists and
  fails on any `failed` entry.
- `src/gobby/storage/cron.py::delete_removed_automation_jobs` runs from
  `src/gobby/scheduler/scheduler.py` at start. `RETIRED_SYSTEM_CRON_JOBS`
  (`src/gobby/runner_init/orchestration.py`) only disables a row.
- Rejected alternatives:
  - a `gcode --project <root> prune` child per project, which needs the managed grant
    gdaemon cannot issue (P8 framing);
  - deleting the operator half now, while Python still serves the route by default;
  - disabling the row through `RETIRED_SYSTEM_CRON_JOBS`, which leaves a listed row
    whose manual run fails;
  - a new hub query ordered by `last_indexed_at`, which would change only the order
    inside the response's lists;
  - one context per content GC candidate, when discovery keeps only the row's
    project.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(job_prune) | test(prune) | test(content_gc)'`;
  - `cargo nextest run -p gobby-codeindex`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_prune.py tests/storage/test_cron.py tests/scheduler/test_cron_scheduler.py tests/servers/test_http_cron.py tests/servers/routes/test_code_index_prune_route.py tests/reports/test_publishing_retirement.py -v`;
  - on an isolated hub with `front_door.routes.code_index: native`, `gcode prune`
    prints `Global prune: completed=<n>, failed=0, skipped=<n>`.

**Granularity:** nine acceptance items and more than six production files. The hourly
job and the route are one outcome: both enter `prune_all` under one global lock, one
skipping and one waiting. The leaf that starts the native hourly prune deletes
Python's (writer's choice 3), and the cron split goes with the row's removal.

Consumers unchanged:
- `src/gobby/servers/routes/code_index.py` — no-edit-reason: `global_prune` still calls `CodeIndexPruner.run_operator_global_prune`, which stays until P9.
- `tests/servers/routes/test_code_index_prune_route.py` — no-edit-reason: drives Python's route over a stub pruner, which does not change.
- `src/gobby/runner_init/servers.py` — no-edit-reason: still passes `code_index_pruner` to the HTTP server's services.
- `src/gobby/app_context.py` — no-edit-reason: still holds `code_index_pruner` for the route.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 8.4.1 - Over the hub machine's rows, `prune_all` skips an empty root with reason `missing_root`, completes a project with a graph-pending file outside the cooloff and leaves its content, skips a live overlay with reason `skipped_overlay`, and neither lists nor touches a foreign machine's row. test: `crates/gcode/src/node/hub/job_prune/tests.rs::prune_all_defers_skips_and_ignores_foreign_rows`.
- 8.4.2 - `prune_all` completes an unregistered project and removes its hub selector. With no Falkor configured it fails a registered `missing_root` project with reason `<id>:failed` and keeps its selector. With the project lock held by another connection it skips a stale project and a soft-deleted project with reason `skipped_locked`. test: `crates/gcode/src/node/hub/job_prune/tests.rs::stale_and_deleted_rows_reconcile_or_skip`.
- 8.4.3 - For an active project, `prune_all` deletes an unsynced content version unreferenced for longer than `retention_days`, keeps a referenced one and one inside the retention, and lists the project as completed. test: `crates/gcode/src/node/hub/job_prune/tests.rs::active_projects_prune_content_past_retention`.
- 8.4.4 - While `GLOBAL_PRUNE` is held, `prune_all` with `Skip` returns `None` at once, and with `Wait` returns its outcome after the release. test: `crates/gcode/src/node/hub/job_prune/tests.rs::the_global_lock_skips_or_waits`.
- 8.4.5 - The prune job runs at 3600 s with `PruneEntry::Skip` and the published `content_retention_days`, and does nothing with `enabled` false. test: `crates/gcodeindex/src/jobs/tests.rs::prune_runs_hourly_with_live_retention`.
- 8.4.6 - An API-key `POST /api/code-index/prune` is claimed. Over an active, an overlay and a failing project it returns 200 with Python's compact body for the three lists. With `retention_days` 5 a version unreferenced for 3 days stays, an absent body deletes it under the published retention of 1, and `force` changes nothing. test: `crates/gcodeindex/tests/family_prune.rs::prune_route_returns_the_python_outcome`.
- 8.4.7 - `enabled` false and no published state return 503 `{"detail":"Code index pruner not available"}`. Invalid JSON, a string `force`, a string `retention_days` and `retention_days` 0 return Python's 422 bodies. An unreachable hub database returns 500 `{"detail":"Code index prune failed"}`. test: `crates/gcodeindex/tests/family_prune.rs::prune_route_follows_python_gates_and_errors`.
- 8.4.8 - `delete_removed_automation_jobs` deletes a system `gobby:code-index-prune` row and its runs, and `list_jobs(exclude_removed_automation=True)` omits the row before the deletion. test: `tests/storage/test_cron.py::test_removed_automation_drops_the_code_index_prune_row`.
- 8.4.9 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 8.5 Content GC for projects only nodes index [category: code] (depends: 8.8, #23835)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/status/content_gc.rs::discover_content_gc`
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: existing discovery calls pass `true`, and adds the discovery test without git history
- `crates/gcode/src/commands/status/prune.rs::*` — scope-reason: discovery passes `true`, and the new `prune_content_by_age` runs content GC alone for the hub
- `crates/gcode/src/commands/status.rs`
- `crates/gcode/src/node/hub/job_prune.rs`
- `crates/gcode/src/node/hub/job_prune/tests.rs`
- `docs/guides/code-index.md`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

Content GC finds its candidates through the hub machine's own state row, because it
reads that checkout's git history. A project that only nodes index has no such row,
so its old content versions are never collected. This leaf collects them by age
(writer's choice 4, accepted 2026-10-17 21:46). It also settles the node rows that
8.2 leaves to it: rows whose registry row was purged.

The leaf deletes no Python code. Python's prune lists only the hub machine's rows, so
it never collected these projects.

gcode's content GC:

- `discover_content_gc` gains `check_git_history: bool`. With `true` it behaves as
  today. With `false` it drops the local machine's state join and the git-history
  check, so it returns every version of the filtered project that no selector names and
  that has gone unreferenced past the retention. Both modes keep one SELECT, so #23835's
  pin predicate (below) applies to both.
- `prune_project_scoped` and 8.4's `prune_project_unattended` pass `true`.
- `pub(crate) fn prune_content_by_age(ctx, retention_days, deadline)` (new, in
  `prune.rs`) discovers with `false` and no stale records. It then runs 8.4's
  `apply_project_prune` with `print` false and `|_| Ok(ctx.clone())` as the
  resolver. `crates/gcode/src/commands/status.rs` re-exports it.

Why the leaf waits on #23835 (Pin-aware retention). A node overlay pins its parent's
versions. A node-only parent's by-age GC must never collect a version a live overlay
still pins, and #23835 makes pins a content-GC reference in `content_gc.rs`.

`prune_all` (8.4) gains a node-only step after the hub rows. A node-only project is one
that some machine selects in `job_sync::sync_rows` and the hub machine does not. The
step handles these projects in `sync_rows` order:

1. A file pending for any of its machines, from `db::pending_graph_files` or
   `db::pending_vector_files` with `limit` 1, puts the project in `completed` with
   nothing collected, as 8.4's step 2 does for a hub row.
2. `db::hub_maintenance::registry_state` classifies the project:
   - live: collection by age;
   - soft-deleted: `job_maintenance::reconcile_deleted` for the machines that
     `deleted_selected_projects` returns, with 8.4's outcome mapping;
   - missing, with some row whose `root_path` hashes to the project id: a node overlay.
     A node agent workspace's root ends in `worktrees/<uuid>/<run_id>` or
     `clones/<uuid>/<run_id>`, and the UUID is its parent project (node-halves 2.3).
     When `registry_state` reports that parent soft-deleted or missing, the overlay is
     a leftover, handled as the next bullet says. Any other overlay is skipped with
     reason `skipped_overlay`, as 8.4 skips a hub overlay;
   - missing, with no row whose root hashes to the id: a purged project's leftover.
     `reconcile_deleted` runs for every machine whose row selects it, with 8.4's
     outcome mapping. 8.2's orphan sweep then purges the `code_indexed_projects` row,
     which no machine selects and no registry row owns.
3. Collection by age takes `HubScope::context` for the hub machine at an empty root,
   as 8.2's sweep does, with `ServiceConfigSelection::projection_cleanup()`. It runs
   `commands::status::prune_content_by_age(&ctx, retention_days, Some(now + 90 s))`.
   `Ok` is completed. An error is failed with reason `<id>:failed`, and the error goes
   to stderr.

The root hash is `uuid::Uuid::new_v5(&crate::models::CODE_INDEX_UUID_NAMESPACE, root_path.as_bytes())`,
computed over the stored text:

- An overlay's id is `gobby_core::project::code_overlay_project_id(root)`, which is
  `code_index_id_for_root` over the canonical root. A node stores that same canonical
  root (`config/context.rs::detect_project_root_from`).
- `code_index_id_for_root` itself would canonicalize against the hub's disk, where the
  node's path is absent or names another directory.
- A node writes its rows under an interactive grant (D3). The handshake admits that
  grant only for a live registry row (the admission query in
  `src/gobby/runner_init/servers.py`, `deleted_at IS NULL`). Registered ids are random
  (`src/gobby/storage/projects.py`), so a row with no registry row and no matching hash
  was written before its registry row was purged.

The job and `POST /api/code-index/prune` call `prune_all` unchanged, so both cover
node-only projects, and `gcode prune --global` counts them.

`docs/guides/code-index.md` says the hourly prune also collects content for projects
that only nodes index, by age, and reconciles a purged project's node rows and the rows
of a node workspace whose project is gone.

Deliberate differences from Python:

- Python never collects a project the hub machine does not select. The native prune
  collects it, and the response lists it.
- No git-history protection for node-only projects. The hub has no checkout to read.
  A node that returns to a version older than the retention re-parses and re-projects
  it, and no fact is lost.
- The pending check covers every machine's rows, for vectors too, although a node
  pulls its own vector work (D13). A project whose node has unsynced vectors waits for
  that node.
- The hub cannot see a node's disk, so it never reconciles a node row whose checkout
  is gone, where 8.2's `missing_root` heals a hub row. 8.6 releases a node agent
  workspace's rows when the node removes it, and step 2 releases those of a workspace
  whose parent project is gone. Two node overlays keep their rows, because #15977's
  ruling (2026-10-18 01:03) adds no age rule:
  - one deleted on a node outside the node's own removal;
  - one that 8.6 removed after an `unauthorized` from a revoked node key or rejected
    claims, while its parent project stays live. Step 2 reports it `skipped_overlay`
    on every run.

Research context:
- `crates/gcode/src/commands/status/content_gc.rs::discover_content_gc` joins
  `code_indexed_project_states` on the local machine for `root_path`, reads one git
  history batch per root, and retains every version when history cannot be read. Its
  `NOT EXISTS` on `code_indexed_file_states` has no machine filter, so it never selects
  a version any machine's selector names. Its only production caller is
  `prune.rs::discover_project_scoped_records`.
- `prune_content_versions_with` never reads the project root. It leases the project
  lock per candidate, re-checks `content_is_unreferenced`, and retains a version whose
  projection store is not configured. A node mid-index holds the lock, so its project
  counts as busy and is skipped.
- `crates/gcode/src/index/api/file_state.rs::upsert_file_state` stamps
  `last_referenced_at` in both write modes for the writing machine, and P1's hub apply
  writes node rows through it (1.4). Age means the same for node rows.
- A node's vectors are applied to the hub's Qdrant (4.2), and graph facts project into
  the hub's Falkor (8.1). The `projection_cleanup()` context reaches both.
- `src/gobby/projects/purge.py::ProjectPurgeService._invalidate_code` removes only the
  hub machine's rows. `reconcile_deleted` (8.2) is the only step that removes node
  selectors. A clear that keeps failing, or `code_index.enabled` false, leaves them in
  place through the purge.
- #23835's Targets are `content_gc.rs::*`, `content_gc/tests.rs::*`,
  `prune/tests.rs::*` and `index/indexer/lifecycle.rs::invalidate` (Constraints,
  concurrent editors).
- Rejected alternatives:
  - `code_index_id_for_root` on the hub, which canonicalizes against the hub's disk;
  - a second discovery SELECT, which would need #23835's pin predicate copied into it;
  - collecting node overlays by age, when 8.4 skips hub overlays and the edit trigger
    owns an overlay's freshness;
  - reconciling every node row with no registry row, which would delete live overlays;
  - a new hub query for node-only projects, when `sync_rows` already lists them.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(job_prune) | test(content_gc) | test(prune)'`;
  - `cargo nextest run -p gobby-codeindex`.

**Granularity:** four acceptance items. Collection by age needs each node-only
project classified first. The purged-leftover branch is the class that collection must
not run on, and reconciling it is 8.2's carry-forward.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 8.5.1 - For a live registered project that only a foreign machine selects, at a root absent from the hub, `prune_all` deletes an unsynced version unreferenced past `retention_days`, keeps the version the foreign selector names and one inside the retention, and lists the project completed. A second such project with a graph-pending file on its machine is completed with its content left. test: `crates/gcode/src/node/hub/job_prune/tests.rs::node_only_projects_prune_content_by_age`.
- 8.5.2 - A foreign row with no registry row whose `root_path` hashes to its project id is skipped with reason `skipped_overlay` and keeps its selector and content, and so is one at `<dir>/worktrees/<live parent>/<run_id>`. One at `<dir>/worktrees/<soft-deleted parent>/<run_id>` is a leftover: with no Falkor configured it fails with reason `<id>:failed` and keeps its selector. A project with no registry row, selected by two foreign machines at roots that do not hash to it, fails with reason `<id>:failed` and keeps both selectors when no Falkor is configured. With the project lock held by another connection it is skipped with reason `skipped_locked`, as a soft-deleted node-only project is. test: `crates/gcode/src/node/hub/job_prune/tests.rs::unregistered_node_rows_skip_overlays_and_reconcile_purged_projects`.
- 8.5.3 - `discover_content_gc` with `check_git_history` false returns a past-retention version of a project that only a foreign machine selects and omits the version its selector names. With `true` it returns nothing for that project. test: `crates/gcode/src/commands/status/content_gc/tests.rs::discovery_without_git_history_needs_no_local_checkout`.
- 8.5.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 8.6 Node workspace removal releases its code-index rows [category: code] (depends: 8.5, #24372)
`kind: deliverable`

Targets:
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/commands/status/invalidate.rs::*` — scope-reason: the confirmation prompt moves into `confirm_invalidate`, which the node client also calls
- `crates/gcode/src/node/mod.rs`
- `crates/gcode/src/node/invalidate.rs`
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/invalidate.rs`
- `crates/gcode/src/node/client.rs`
- `crates/gcode/src/node/client/invalidate.rs`
- `crates/gcode/src/node/invalidate_tests.rs`
- `crates/gcodeindex/src/lib.rs`
- `crates/gcodeindex/src/node.rs`
- `crates/gcodeindex/src/node/invalidate.rs`
- `crates/gcodeindex/tests/node_invalidate.rs`
- `crates/gdaemon/src/nodes/agents/workspace.rs`
- `crates/gdaemon/tests/node_workspace_release.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

A node row outlives its checkout, because the hub cannot see a node's disk. #15977
ruled (2026-10-18 01:03, option (a)) that the node releases its own rows when it
removes a checkout or agent worktree, with no hub age rule. The node's removal path is
node-halves 2.3's `crates/gdaemon/src/nodes/agents/workspace.rs::remove` (#24372,
Worktree and clone preparation, repair and removal, under #24148, S2.7 node half), so
this leaf belongs to the hub-node lane. `remove` runs `gcode invalidate` in the
workspace before it removes the path.

Node `gcode invalidate`, following P2's route-leaf layout:

- `dispatch.rs::run` sends `invalidate` without `--project-id` through
  `resolve_access`. `Direct` runs today's code. `Node` calls
  `node::client::invalidate`. `invalidate --project-id` keeps 1.7's refusal, because it
  names no root and a node grant needs one.
- The prompt in `commands/status/invalidate.rs` moves into
  `pub(crate) fn confirm_invalidate(view: &impl ProjectView, force: bool) -> anyhow::Result<bool>`.
  Local `invalidate` and the node client both call it, so a node prints the local
  prompt and `Aborted.`.
- `node::client::invalidate` posts `InvalidateRequest { scope: NodeScope }` to
  `/api/code-index/node/invalidate` under 1.8's request rules, with a 10-second
  timeout as `search-content` has. It then prints the local
  `Invalidated local code index state for project <id>` line with the id the hub
  returns. It runs no freshness check, as local `invalidate` runs none.
- `node::hub::invalidate(&HubScope, InvalidateRequest) -> Result<InvalidateReport, RouteError>`
  checks the scope's overlay shape with `HubScope::context` (1.3). It then calls 7.2's
  `node::hub::family_invalidate::invalidate` for the principal's machine and its
  overlay id, or its project id when it has no overlay. A busy project lock returns
  `index_busy`. `InvalidateReport { project_id, projects }` carries that id and the
  number of project-state rows deleted.
- gcodeindex's handler runs 1.9's steps 1 to 7, and `ROUTE_PATHS` gains the path.

Removal. Before `git worktree remove` or `remove_dir_all`, `workspace.rs::remove`
runs `<home>/bin/gcode invalidate --project <path> --force --quiet` when the
workspace holds `.gobby/project.json`:

- `<home>` is `NodeAgents::home`, and the child gets `GOBBY_HOME=<home>`. It is a
  `tokio::process::Command` with `kill_on_drop(true)`, as 4.3's child is. The client's
  timeout bounds it, so it needs no deadline of its own.
- The order is forced. gcode reads the overlay's identity from the workspace's
  `.gobby/isolation.json` (`config/context.rs::resolve_project_identity`), which goes
  with the path.
- Exit 0 continues the removal. A rerun after a later step failed deletes nothing and
  exits 0.
- A child whose stderr JSON line has `"error":"unauthorized"` also continues. Such a
  refusal is permanent for the workspace: the handshake refuses a project the hub no
  longer admits ("project is not admitted"), or the hub rejected the node's key or
  claims. Keeping the workspace would only leak its disk, and 8.5 releases the rows of
  a workspace whose project is gone.
- Any other failure fails the removal as a failed `git worktree remove` does. The
  record and the path stay, and `retry_removals` tries again on the next report
  interval.
- No child runs in three cases, and the removal continues as 2.3 has it:
  - without `.gobby/project.json`, because a node grant needs that id, so the
    workspace has no hub rows;
  - for a path already gone, which counts as removed;
  - with no `<home>/bin/gcode`, which logs once per process as 4.3 does, because
    nothing on the node can release the rows.
- `serve::run` runs `retry_removals` once before `serve_doors` (2.3). The child
  reaches the hub through the node's front door, which is not serving yet, so it fails,
  and the first report tick's retry removes the workspace.

What the hub keeps afterwards:

- The workspace's `code_communities`, `code_indexed_file_states` and
  `code_indexed_project_states` rows for the node's machine go at once.
- The overlay's `code_indexed_projects` row, its content and its Falkor and Qdrant
  projections go at 8.2's next orphan sweep, because no machine selects it and no
  registry row owns it. 7.2's route leaves the stores to the same sweep.
- The node's main checkout (node-halves 2.9, Checkout registration) keeps its rows. The
  node never removes that checkout, and `gcode invalidate` there stays an explicit
  command.
- A run with `workspace.mode` `"none"` runs in `source_root` and has nothing to remove.
  A `reused` record is never removed (`to_removals`).

Research context:
- Both workspace modes run repair's `.gobby/project.json` step, which writes
  `.gobby/isolation.json` naming the parent
  (`src/gobby/utils/project_context.py::ensure_project_json_for_isolation`). gcode
  resolves each workspace as an overlay whose id is `code_index_id_for_root` of its
  canonical root.
- `invalidate --project-id` resolves in `dispatch.rs::dispatch_early_command`, before
  `run`.
- `CliError::print` writes one JSON line to stderr, with `error`, `message` and an
  optional `recovery`. `GrantError::Unauthorized` prints `"error":"unauthorized"` and
  exits 2 (`crates/gcode/src/cli_error.rs`). gcore maps a 403 `claims_mismatch`, and a
  401 whose code it maps to no other error, to it
  (`crates/gcore/src/grant/mod.rs::from_presentation_http`).
- The handshake answers an unadmitted project with 403 `claims_mismatch` and "project
  is not admitted" (`src/gobby/runtime_grants/handshake.py`). Node routes skip
  revocation (1.9 step 3), so a grant cached before the project's deletion still clears
  the rows.
- 7.2's `family_invalidate::invalidate` takes the project lock with
  `IndexLockPolicy::maintenance_try()` and runs `indexer::invalidate` for the machine it
  is given, on a read-write connection.
- Rejected alternatives:
  - a hub age rule, which the ruling refused;
  - a key-only route that gdaemon calls itself, which would widen ruling E5(a)'s
    exception to a write;
  - keeping the workspace on `unauthorized`, which would keep it forever.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(node) | test(invalidate)'`;
  - `cargo nextest run -p gobby-codeindex`;
  - `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test node_workspace_release`.

**Granularity:** six acceptance items and eight new files. The command, its route and
the removal call are one release path. The removal test runs a stub `gcode`, and the
client and route tests prove what the real one does.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 8.6.1 - `node::hub::invalidate` for a foreign machine's overlay deletes that machine's communities, file states and project state for the overlay id, and keeps the parent's rows and the hub machine's rows for the same overlay. A scope whose overlay shape disagrees with the principal returns `claims_mismatch`. With the project lock held by another connection it returns `index_busy` and deletes nothing. test: `crates/gcode/src/node/invalidate_tests.rs::hub_invalidate_deletes_only_the_principal_machine_rows`.
- 8.6.2 - Under the remote golden grant, against the stub hub, `gcode invalidate --force` on a node overlay deletes the node machine's rows for it and prints the local line. Without `--force`, an answer other than `y` prints `Aborted.` and posts nothing. `invalidate --project-id` fails with `brokered_unsupported`. test: `crates/gcode/src/node/invalidate_tests.rs::node_invalidate_clears_the_hub_rows`.
- 8.6.3 - With a valid remote grant, the route deletes the principal machine's rows and returns the report, and a held project lock returns 409 `index_busy`. test: `crates/gcodeindex/tests/node_invalidate.rs::invalidate_route_serves_the_principal_machine`.
- 8.6.4 - `remove` of a worktree record and of a clone record whose workspace holds `.gobby/project.json` runs a stub `gcode` once, with `invalidate --project <path> --force --quiet` and `GOBBY_HOME` set, while the path still exists. It then removes the path and deletes the record. A workspace without `.gobby/project.json`, a path already gone and an absent `gcode` run no stub and remove as 2.3 does. test: `crates/gdaemon/tests/node_workspace_release.rs::removal_invalidates_before_the_path_goes`.
- 8.6.5 - A stub that exits 1 keeps the record and the path until a later retry succeeds. A stub whose stderr is an `unauthorized` JSON line lets the removal finish. test: `crates/gdaemon/tests/node_workspace_release.rs::removal_failures_keep_the_workspace_except_unauthorized`.
- 8.6.6 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 8.7 Edit trigger entry point [category: code] (depends: 8.4)
`kind: deliverable`

Targets:
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/job_trigger.rs`
- `crates/gcode/src/node/hub/job_trigger/tests.rs`
- `crates/gcodeindex/src/jobs.rs`
- `crates/gcodeindex/src/jobs/trigger.rs`
- `crates/gcodeindex/src/jobs/tests.rs`
- `crates/gcodeindex/tests/job_trigger.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

`gobby_codeindex::jobs::trigger::EditTrigger` ports
`src/gobby/code_index/trigger.py::CodeIndexTrigger` (D18). `HubJobs` holds one, and
`HubJobs::edit_trigger(&self) -> Arc<EditTrigger>` hands it out. #24151 takes it where
`serve::run` builds `HubJobs` and calls `notify_file_changed` from the native hook
path. Until then nothing calls it, and Python's trigger keeps indexing edits.

It serves the hub's own checkouts. It runs as the hub machine on the hub's disk, and
8.1 registers the jobs on the non-node branch only. A node's checkouts index through
`gcode index` over the node routes (1.8).

Notification. `notify_file_changed(&self, root: &Path, file: &Path)` is synchronous
and takes the trigger's `Mutex`, so the hook path calls it inline:

- The root key is the canonical root, or the root as given when canonicalization
  fails, as Python's `_root_key` resolves it.
- The file resolves as `_normalize_file_path` resolves it: a relative path joins the
  root, and the result is kept relative to the root. The root itself, a directory and
  a path outside the root are dropped.
- The file joins its root's pending set.
- A suspended root (below) stays suspended when the file was in the suspended batch
  and its version, both markers' versions and the root's overlay id are unchanged.
  Otherwise the suspension and the root's backoff clear.

Tick. `HubJobs::ticks()` gains `code_index_trigger`, which works on every
`BASE_CADENCE` tick with no interval:

- With `code_index.enabled` false it drops every pending file.
- Otherwise each root with pending files, no run in flight, no suspension and its
  retry time passed hands its pending set to a run on a named `std::thread`. Roots run
  in parallel, and a root runs one batch at a time.
- A guard clears the root's in-flight flag when the thread ends, so a panicking run
  returns its files to pending instead of wedging the root.
- `EditTrigger::tick_at(now, start)` takes the time and the function that starts a
  run, and `finish_at(now, root, files, outcome)` applies a run's outcome. The tests
  drive both with no thread or database. The registered tick passes `Instant::now()`
  and the thread spawn.

The run calls
`node::hub::job_trigger::index_files(database_url, hub_machine, root, files) -> Result<Vec<PathBuf>, TriggerError>`
(new, declared in `node/hub.rs`), which returns the busy files:

1. `config::resolve_project_identity(root)` gives the project id and index scope, as
   a `gcode index --project <root>` child resolves them.
2. The context comes from `HubScope::context` (1.3) with
   `ServiceConfigSelection::database_only()`. An overlay's `HubScope` has the parent id
   as `project_id` and the overlay id as `overlay_project_id`, and its `NodeScope`
   carries both roots.
3. `index_lock::lock_project_files(ctx, files, IndexLockPolicy::brief_index_flush_try())`
   takes the locks. With every file busy it returns them all without indexing.
4. `commands::index::run_index_locked(ctx, IndexRequest { explicit_files: <acquired>, full: false, sync_projections: false, path_filter: None, .. })`
   indexes the acquired files. That is what
   `gcode index --project <root> --files=… --skip-if-locked` runs. The sync job (8.1)
   projects them.
5. A `CliError` whose code is `checkout_required` or `checkout_mismatch` returns
   `TriggerError::Checkout(code)`. Any other error returns `TriggerError::Failed`.

Outcomes, as `CodeIndexTrigger._flush` and its helpers handle them:

- A run that indexed a file clears the root's backoff and suspension.
- Busy files return to pending with the root's next retry delay. The delay starts at
  1 s, doubles and caps at 30 s, Python's defaults kept as constants. The first tick
  after it starts the retry.
- `Failed` returns every file to pending with the next retry delay and logs the error
  with the root to stderr.
- `Checkout` returns the files to pending. When no file arrived during the run, the
  root suspends with a snapshot of its overlay id, both markers' versions and each
  pending file's version, and its backoff clears. When a file arrived during the run,
  the root stays unsuspended for one more batch.
- A file's version is its `(dev, ino, size, mtime_ns, ctime_ns)`, or none when it is
  missing. The markers are `.gobby/isolation.json` and `.gobby/project.json`. The
  overlay id is the one `resolve_project_identity` gives an overlay scope, or none.

Deliberate differences from Python's trigger:

- A batch starts at the next tick, up to 5 s after the edit. Python starts it on the
  event loop's next turn.
- A root runs one batch at a time. Python runs disjoint batches of one root at once,
  which share its project lock.
- No 30 s kill. In-process indexing cannot be killed, as in 8.2.
- No project id argument. Identity comes from the root's markers, as the child
  resolves it. Python passes the hook's project id only to scope the child's managed
  grant.
- A path outside the root is dropped. Python passes it to `gcode`, whose `--files`
  resolution refuses the whole batch, so the batch fails on every retry.
- `code_index.enabled` false drops pending files. Python builds no trigger without a
  code indexer.

Research context:
- `src/gobby/code_index/trigger.py`: `notify_file_changed` hands off with
  `call_soon_threadsafe` to `_schedule_file`. `_start_batch`, `_batch_done`,
  `_suspend_checkout_failure`, `_requeue_for_retry` and `_flush` hold the state
  machine. The defaults are `retry_base_seconds` 1.0, `retry_max_seconds` 30.0 and
  `index_timeout_seconds` 30.0. `_busy_files_from_result` keeps only requested paths
  from stdout's `busy_files`.
- The only caller is `src/gobby/hooks/event_handlers/_tool.py::_notify_code_index`. It
  resolves the project id with `ProjectIdResolver.resolve_for_index` and passes a
  repo-relative path. `src/gobby/servers/_app_lifecycle.py` builds the trigger only
  when a code indexer exists.
- `commands/index.rs::run` with `--files` resolves each path through
  `commands/scope.rs::resolve_path_input`, locks with `lock_project_files`, exits 3
  when every file is busy and otherwise prints `busy_files`. `run_index_locked` and
  `lock_project_files` are `pub(crate)`.
- `config/context.rs::resolve_project_identity` reads `.gobby/isolation.json` and
  returns an overlay scope with the parent's id and root, or a single scope. A root
  with no identity fails with `checkout_required`.
- The checkout fence (`index/checkout_fence.rs`, called from
  `index/api.rs::upsert_project_seed`) raises `checkout_required` and
  `checkout_mismatch` as `CliError`s.
- Rejected alternatives:
  - a `gcode index` child per batch, which needs the managed grant gdaemon cannot
    issue (P8 framing);
  - starting the run from `notify_file_changed`, which would need its own timer for
    retries;
  - waiting on #24151, which D18 rules out.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(job_trigger)'`;
  - `cargo nextest run -p gobby-codeindex`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 8.7.1 - `index_files` on a registered checkout indexes only the named files for the hub machine, leaves them `graph_synced` and `vectors_synced` false, and returns no busy files. With another connection holding one named file's lock it returns that file and indexes the other. With every named file's lock held it indexes nothing and returns them all. test: `crates/gcode/src/node/hub/job_trigger/tests.rs::index_files_indexes_named_files_and_returns_busy_ones`.
- 8.7.2 - `index_files` returns `Checkout("checkout_required")` for a root with no Gobby identity and `Checkout("checkout_mismatch")` for a checkout of a project the hub machine registers at another root. It indexes an isolated overlay's file under the overlay id. test: `crates/gcode/src/node/hub/job_trigger/tests.rs::checkout_refusals_are_typed_and_overlays_index`.
- 8.7.3 - `notify_file_changed` keys a root by its canonical path, keeps a file relative to it, and drops the root itself, a directory and a path outside the root. A tick starts one run for each root with pending files and none for a root whose run is in flight. A file notified during a run starts at the first tick after the run ends. With `enabled` false a tick drops pending files and starts nothing. test: `crates/gcodeindex/src/jobs/tests.rs::trigger_batches_each_root_once`.
- 8.7.4 - Under a fake clock, a `Failed` run's files retry after 1 s, then 2 s, doubling to 30 s. A run that indexed a file clears the backoff, and its busy files retry after 1 s. A `Checkout` refusal suspends the root: a second notice of an unchanged file starts nothing, and a changed marker, a changed file or a new file lifts the suspension. A refusal for a batch during which a file arrived does not suspend. test: `crates/gcodeindex/src/jobs/tests.rs::trigger_backs_off_and_suspends_like_python`.
- 8.7.5 - Through `HubJobs::edit_trigger()`, a notified file of a registered checkout reaches the index for the hub machine after one trigger tick. test: `crates/gcodeindex/tests/job_trigger.rs::a_notified_edit_reaches_the_index`.
- 8.7.6 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 8.8 Nightly repair handler [category: code] (depends: 8.7)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/repair.rs::*` — scope-reason: `run`'s locked repair moves into `pub(crate) fn repair`, `RepairOutput` becomes `pub(crate)`, and `run` prints the result
- `crates/gcode/src/node/hub.rs`
- `crates/gcode/src/node/hub/job_repair.rs`
- `crates/gcode/src/node/hub/job_repair/tests.rs`
- `crates/gcodeindex/src/jobs.rs`
- `crates/gcodeindex/src/jobs/config.rs`
- `crates/gcodeindex/src/jobs/nightly_repair.rs`
- `crates/gcodeindex/src/jobs/tests.rs`
- `crates/gcodeindex/tests/job_nightly_repair.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Release ownership (Constraints): bumps `gcode` and `gdaemon` by 0.0.1.

`gobby_codeindex::jobs::nightly_repair::run(jobs: &Arc<HubJobs>) -> String` ports
`src/gobby/code_index/nightly_repair.py::CodeIndexNightlyRepairer.run_once` (D18).
#21582 schedules it from the native cron executor, which takes `HubJobs` where
`serve::run` builds it. No tick registers it, and until #21582 Python's cron job keeps
running the nightly repair.

`run`:

1. A call while a run is in flight returns
   `Code index nightly repair skipped: already running`, from the job's busy flag in
   `HubJobs`.
2. It reads `JobConfig` (8.1), which gains `code_index.nightly_repair_concurrency`.
   With no published state it returns
   `Code index nightly repair skipped: no published config`. With `code_index.enabled`
   false it returns `Code index nightly repair skipped: code index disabled`.
3. The work runs on a named `std::thread` (P8 tick model), and `run` awaits its result
   over a `tokio::sync::oneshot` channel, so the caller's task never blocks.
4. The thread takes the hub machine's rows from `job_sync::sync_rows` (8.1), the rows
   Python's `list_indexed_projects` lists. It repairs up to
   `nightly_repair_concurrency` rows at once, each on its own thread with its own
   connections.
5. It returns
   `Code index nightly repair completed: completed=<n> failed=<n> skipped=<n>`, and
   logs each failure with its project to stderr.

`node::hub::job_repair::repair_row(database_url, hub_machine, row: &SyncRow) -> RepairStatus`
(new, declared in `node/hub.rs`):

- `Skipped("deleted")` for a row whose `registry_deleted` holds, which 8.2 reconciles;
- `Skipped("missing_root")` for an empty root or one that is not a directory;
- `Skipped("overlay")` when `code_index_id_for_root(root)` equals the row's project
  id, as Python skips it (#20889);
- otherwise `Completed`, or `Failed` with the error, from `commands::repair::repair`
  on a context from `HubScope::context` with `ServiceConfigSelection::all()`. A full
  reindex syncs projections, and the graph check reads Falkor.

`commands::repair::repair(ctx) -> anyhow::Result<RepairOutput>` is `run` without the
printing: `index_lock::with_project_lock(ctx, IndexLockPolicy::wait(), …)` around
`run_locked`.

- The wait policy is `gcode repair`'s. A repair thread can wait up to
  `DEFAULT_WAIT_LOCK_TIMEOUT` (1800 s) behind an index run, as Python's child waited.
  A lock still busy after the wait is `Failed`.
- `run_locked` reads `read_local_machine_id`, which in gdaemon is the hub's own
  machine, as 8.4 notes for prune. The tests use the local machine as the hub machine.

Deliberate differences from Python's nightly repair:

- No per-project timeout. In-process repair cannot be killed, so
  `code_index.nightly_repair_timeout_seconds` has no native reader (P8's operator
  decision).
- No run id. Python's `run_id` ties a run's maintenance-log events together, and the
  native handler writes none.
- A soft-deleted project's row is skipped, because 8.2 reconciles it. Python lists it
  and launches a repair.
- Without published config, or with `code_index.enabled` false, it returns a skip
  line, as the P8 ticks do nothing.

Research context:
- `src/gobby/code_index/nightly_repair.py::CodeIndexNightlyRepairer`: `run_once`
  guards with `_running`. `_run_locked` lists `storage.list_indexed_projects`, skips
  `skipped_missing_root` and `skipped_overlay` rows, and runs
  `gcode repair --project <root> --format json` under `open_launch_async` with
  `nightly_repair_timeout_seconds`. It logs a `nightly_repair` event and returns the
  summary line with `run_id`.
- `register_code_index_nightly_repair_cron` registers the system row
  `gobby:code-index-nightly-repair` on `code_index.nightly_repair_cron` (`0 2 * * *`)
  and its timezone. The schedule belongs to #21582.
- `commands/repair.rs::run` waits with `IndexLockPolicy::wait()`. `run_locked` is
  private. When the stored indexer version differs it runs a full reindex with
  `sync_projections: true`. Otherwise it resolves local-import calls and inheritance
  and marks promotion owners and graph drift unsynced.
- Rejected alternatives:
  - a native tick on `nightly_repair_cron`, which would duplicate #21582's scheduler
    and run beside Python's cron job until #21582 lands;
  - a `gcode repair` child, which needs the managed grant gdaemon cannot issue (P8
    framing);
  - `IndexLockPolicy::maintenance_try()`, which would skip a busy project for a whole
    night where Python's child waited.
- Planned checks:
  - `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code -E 'test(job_repair) | test(repair)'`;
  - `cargo nextest run -p gobby-codeindex`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 8.8.1 - `repair_row` returns `Skipped("missing_root")` for an empty and an absent root, `Skipped("overlay")` for a root whose derived id equals the row's, `Skipped("deleted")` for a soft-deleted project's row, and `Completed` for an indexed project. For a project whose stored indexer version differs, it returns `Completed` and the project state holds the current indexer version. test: `crates/gcode/src/node/hub/job_repair/tests.rs::repair_row_skips_and_repairs_like_gcode_repair`.
- 8.8.2 - `run` over a hub machine's active row, an empty-root row, an overlay row and a foreign machine's row returns `Code index nightly repair completed: completed=1 failed=0 skipped=2` and leaves the foreign row unchanged. test: `crates/gcodeindex/tests/job_nightly_repair.rs::nightly_repair_returns_the_python_summary`.
- 8.8.3 - A `run` while another is in flight returns the `already running` line, `enabled` false returns the `code index disabled` line, and `nightly_repair_concurrency`'s default equals the contract's. test: `crates/gcodeindex/src/jobs/tests.rs::nightly_repair_skips_when_running_or_disabled`.
- 8.8.4 - `gcode` and `gdaemon` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## E1 End-to-end verification
`kind: verification`

These are completion gates for P1 to P8. Except for plan validation
(`uv run gobby plans validate .gobby/plans/code-index-family.md -p /Users/josh/Projects/gobby`),
none has run yet. Each must hold on `0.5.0` with freshly built and installed binaries.
Gates 1 to 3 must hold before #23276 closes, gates 1, 2 and 4 must hold again
before P2's phase epic closes, gates 1 and 2 must hold again before P3's phase
epic closes, gates 1, 2 and 5 must hold again before P4's phase epic closes, and
gates 1, 2 and 6 must hold again before each of P5's, P6's and P7's phase epics
closes, and gates 1, 2 and 7 must hold again before P8's phase epic closes:

1. `GCODE_POSTGRES_TEST_DATABASE_URL=… cargo nextest run -p gobby-code`, then
   `cargo nextest run -p gobby-codeindex -p gobby-runtime -p gobby-daemon -p gobby-core`,
   must pass.
2. `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/ tests/install/test_version_pins.py tests/e2e/test_hub_node_pair.py`
   must pass. After P2 that file also holds `test_node_read_commands_through_hub`.
3. On a live hub and node pair on the operator's machines, #24380's preflight on the
   node must warn about nothing, and `gcode status` on the hub's own checkout must be
   unchanged.
4. On the same live pair, every command 2.9.1 lists must exit 0 with results on the
   node. The same commands on the hub's own checkout must print what they printed
   before P2.
5. On the same live pair, with an `embeddings` block on the node, every file of a
   project indexed only on the node must reach `vectors_synced` within three heartbeat
   ticks, and the node's `gcode search` must print semantic results with no
   `embedding_identity_mismatch` warning. With the block removed and the node gdaemon
   restarted, the node's `/api/embeddings` must reach the hub. P4 has no automated
   end-to-end test: the pair fixtures run no Qdrant or embedding endpoint, and 4.1 to
   4.3 test each half against stubs.
6. On the live hub with `front_door.routes.code_index: compare`, an API-key request to
   each `/api/code-index` GET route the closing phase has added, for an indexed
   project, must record a match in the front door's compare record. The web UI's
   code-graph view must still load, because its cookie requests reach Python. Restore
   the setting afterwards. P7's POST routes serve the proxied response under
   `compare`, so P7's gate 6 sets `native`. It then checks that clear and rebuild
   return the CLI payload and that invalidate of a scratch project returns Python's
   body.
7. On the live hub, which runs FalkorDB and Qdrant where CI does not (#21045):
   - one maintenance pass (8.2) must reconcile a registered scratch project whose root
     was deleted, and a soft-deleted scratch project that the hub and the node select.
     Each must lose its graph, its vectors and every reconciled selector;
   - within one maintenance interval, an active project must gain symbol summaries and
     community labels (8.3);
   - on the live pair, after a node agent run's workspace is removed, the hub must hold
     no `code_indexed_project_states` row for its overlay (8.6).

## V1 Plan Changelog
`kind: framing`

- 2026-10-17: First draft of P1 by Plan Writer gobby#16767. Decisions D1 to D9 were
  ruled by Orchestrator #15977 and Josh on 2026-10-17. The remaining phases follow in
  this file under #15977's 16:47 ruling.
- 2026-10-17: Draft of P2 (2.1 to 2.9) by Plan Writer gobby#16767, with D10 to D14
  from #15977's later rulings. P1 edits that P2 needs: 1.3 adds `ROUTE_PATHS`; 1.9's
  `claims` reads it; 1.7's recovery text names no command list; 1.8's freshness
  sentence points at 2.2; 1.10 depends on 2.8; and 1.1 and 1.8 drop consumer lines
  for files P2 targets.
- 2026-10-17: Draft of P3 (3.1) by Plan Writer gobby#16767. The content-hash keys and
  D11's deletion rule are already in place, so P3 is one leaf; 1.10 now depends on
  3.1. Edits for Plan Adversary #16993's P2 review:
  - B5: 1.8's client decodes 2xx bodies with `into_json`, and 2.4.4 is new;
  - N7: 2.8's node grep drops files missing on the node, and 2.8.5 is new;
  - N8: 2.2 states the project-lock difference.

  2.7's research context no longer says graph payloads filter by version.
- 2026-10-17: Draft of P4 (4.1 to 4.3) by Plan Writer gobby#16767, under #15977's
  rulings E4(a) (the node's endpoint comes from its bootstrap, recorded in D12) and
  E5(a) (the grant-free roots route, recorded in D4). 4.2's apply creates a missing
  collection at the reported size when `vector_dim` is unset. Other edits:
  - 1.10 now depends on 4.3, and the Constraints name 4.3 as the last bumping leaf.
  - The Overview states the after-P4 embedding behavior.
  - Constraints' concurrent-editors bullet names 3.1's edge on #23834.
  - N9 (Plan Adversary #16993): 3.1.2 also checks `fetch_symbols_for_project` under
    the foreign machine.
  - P3's framing records that #15977 extended #23834's Targets (option a).
  - E1 adds gate 5 and `gobby-core` to gate 1.
- 2026-10-17: Draft of P5 to P7 (5.1, 6.1, 6.2, 7.1, 7.2) by Plan Writer gobby#16767,
  under #15977's rulings E6(a), E7(a) and the accepted refinements, recorded as D15 to
  D17. Other edits:
  - 1.10 now depends on 7.2, and the Constraints name 7.2 as the last bumping leaf.
  - The concurrent-editors bullet covers P7.
  - The Overview states the after-P7 behavior.
  - E1 adds gate 6.

  P5's framing lists the writer's deliberate differences from Python for review. The
  main ones: native reads skip gcode's freshness prelude, and invalidate also deletes
  the hub machine's communities, as `gcode invalidate` does.
- 2026-10-18: Draft of P8 (8.1 to 8.8) by Plan Writer gobby#16767, under #15977's
  rulings of 2026-10-17 21:46 (the tick model, each leaf deleting its Python job, and
  the operator decision), 22:29 (D18) and 2026-10-18 01:03 (8.6's node-side release).
  Other edits:
  - The P8 chain runs 8.1 to 8.4, 8.7, 8.8, 8.5 and 8.6, so only 8.5 and 8.6 wait on
    other plans. 1.10 depends on 8.6, and the Constraints name it the last bumping leaf.
  - The Overview states the after-P8 behavior, and that P9's deletion of
    `src/gobby/code_index/` waits on #24151 and #21582. D15's deletion bullet follows
    D18.
  - P2's framing lets a node run `invalidate` for its own checkout (8.6).
  - 5.1's freshness bullet names the edit trigger and the maintenance pass.
  - The concurrent-editors bullet covers P8 and names 8.5's edge on #23835 and 8.6's on
    #24372.
  - E1 adds gate 7.
  - Edits for Plan Adversary #16993's reviews of P4 to P8:
    - B6: 4.3's `gcode` child is killed on drop and bounded by a deadline;
    - B7: 7.2 invalidates under the project lock, through
      `node/hub/family_invalidate.rs`;
    - B8, B9 and N13 to N15 (8.1, 8.2 and 4.3): a stalled sync worker stops after the
      blocked file and then drops its locks; a soft-deleted project is reconciled for
      every machine, and the sync job and 4.3's roots route skip it;
      `sync_worker_concurrency` bounds rows;
    - B10: the node releases a removed workspace's rows (8.6), and 8.5 releases those
      of a workspace whose parent project is gone;
    - N10 to N12: 4.1's embedding entry points are `pub`, 4.3 notes the node host's
      only job, and 7.1.1 is reworded;
    - N16: 1.10 sits after the chain's last leaf;
    - N17: 8.5 names the overlay rows a removal after `unauthorized` leaves.
