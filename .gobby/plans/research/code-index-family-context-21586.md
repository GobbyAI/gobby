# Research context: code-index family plan (#21586 + #23276, for #24380)

Plan Writer gobby#16767, 2026-10-17. Assignment from Orchestrator #15977 (15:47 CDT):
one plan at `.gobby/plans/code-index-family.md` covering #21586 (Migrate code-index
orchestration and shared dependencies; epic, no children, now under Stage 2 #21544,
no #21568 gate) and #23276 (gcode index writes from a node need hub HTTP routes;
`needs-planning`, deferred-from gdaemon-api-keys-nodes:D4, D2.5). Scope to what
#24380 (Node code-index preflight, node-halves 2.11) needs first; cover the whole
family. Plan adversary 16993 (after #23275 refresh with 16490). Josh approves before
expansion. Commits `[gobby-#21586] docs: ...`; #21586 claimed by this seat.
Verify #21586's real upstream edges (Orchestrator guesses #24447 Grant signing).

## Task facts
- #21586 description: migrate gcode gateway, index lifecycle and pruning, graph/vector
  projection coordination, scheduling, grants, shared AI execution for surviving
  code-index services; preserve canonical session summary generation and hub file
  forwarding owners; gcode binary behavior unchanged; no wiki (retired #21771).
  Blocks #24380 and #23276.
- #23276 blocked by #23271 (closed) and #21586; blocks #24380. Original D4 text:
  "Decision 17 removed the datastore tunnel, so `gcode index` on a node cannot write
  PostgreSQL directly and needs hub HTTP routes (was S4.5). Owned by S2.7 and S4.1b."
- #24380 (node-halves 2.11) runs, on the node with GOBBY_DAEMON_URL = node front
  door, no managed credential: `gcode status --quiet --format json --allow-stale
  --project <ws>` (5 s), `gcode index --quiet --project <ws>` (rest of 120 s),
  `gcode search-content __gobby_code_index_smoke__ --limit 1 --quiet --allow-stale
  --project <ws>` (10 s). Python `src/gobby/agents/code_index.py::ensure_isolation_code_index`
  runs them sequentially; any failure raises and becomes a prompt warning. So #24380
  needs read routes (status, search-content) as well as index writes.
- #21577 (S4.5 Configure gcode on nodes) closed 2026-09-10 after decision 17 dropped
  the tunnel; nothing reusable beyond confirming a daemon-API adapter is now needed.

## Architecture facts
- ROADMAP decision 17: no tunnel, no machine-scoped role; datastores never leave the
  hub; per-invocation binaries (`gcode`, `gobby`) call the hub over HTTP with the
  machine's key. Stage 4 text: "`gcode` on a node writes its index through hub HTTP
  routes owned by whichever Stage 2 family absorbs the code-index routes."
- Decision 16: family crates `crates/g<family>` -> `gobby-<family>`, publish=false,
  static `RouteFamily`, Proxy|Native|Compare per family from bootstrap. `crates/gcode`
  is already package `gobby-code` (lib `gobby_code`, bin `gcode`). Template plan:
  `.gobby/plans/gdaemon-runtime-family.md` (S2.3 #21559, crate `crates/gruntime`).
- Decision 18: timer-driven periodic work moves to the gdaemon heartbeat host.
- Memory 3e6e1927: code facts keyed by project, file path, content hash (shareable
  across machines); project and file selector state keyed by machine.
- gcode gets datastores only via signed v2 grants (`crates/gcore/src/grant/`):
  handshake `/api/runtime/handshake/challenge` + `/api/runtime/handshake`, config
  `/api/runtime/config`. Bundle capabilities (`crates/gcore/src/grant/bundle.rs`):
  `PostgresCapability::{Direct{dsn,role_name,credential_generation,valid_until},
  Brokered{operations: Vec<BrokerOperation{name,method,path}>}, Unavailable{}}`; same
  three arms for Falkor and Qdrant; `AiCapability::{Daemon, Unavailable}`;
  `broker_operations` list. `AcquiredGrant::permits_datastore` checks only expiry.
- KEY SEAM: Python `src/gobby/runtime_grants/service.py` already issues `Brokered`
  for Falkor (`FALKOR_OPERATIONS`: clear_projection POST /api/code-index/graph/clear,
  rebuild_projection POST /api/code-index/graph/rebuild) and Qdrant
  (`QDRANT_OPERATIONS`: invalidate_projection POST /api/code-index/invalidate) when
  secrets are unresolved or non-loopback without credentials; `EMBED_OPERATION`
  POST /api/embeddings. gcode `crates/gcode/src/db/resolution.rs::postgres_dsn_from_grant`
  errors on `PostgresCapability::Brokered`; Falkor/Qdrant Brokered -> None.
  Natural design: hub issues `PostgresCapability::Brokered` (operations = code-index
  routes) to principals on a remote machine; gcode honors it by calling those routes.
- gcode's indexer already has a write seam: `crates/gcode/src/index/indexer/sink.rs`
  trait `CodeFactSink` (delete_file_non_symbol_facts, delete_stale_file_symbols,
  upsert_symbols, upsert_file, upsert_imports, upsert_calls, upsert_inheritance,
  upsert_content_chunks) with `PostgresCodeFactSink` (new() also runs
  `api::upsert_project_seed` and reads local machine id; upsert_file also writes
  `api::file_state::upsert_file_state`). `crates/gcode/src/index/api.rs` has
  `CodeFactWriteRequest`/`CodeFactWriteSummary` (serde). An HTTP sink posting fact
  batches to a hub route is the least-mechanism node write path.
- gcode is sync `postgres` 0.19 throughout; no storage traits besides CodeFactSink and
  `commands/graph/lifecycle.rs::LifecycleBackend`.
- gcode daemon HTTP calls today (`crates/gcode/src/daemon.rs`): GET /api/projects,
  POST /api/code-index/prune; lifecycle.rs: /api/code-index/graph/clear|rebuild;
  embeddings_doctor: /api/embeddings/doctor.
- Python code-index routes `src/gobby/servers/routes/code_index.py` (465 lines,
  prefix /api/code-index): GET /graph, /graph/file/{path}, /graph/symbol/{id}/neighbors,
  /graph/blast-radius, /graph/path, /graph/search; POST /graph/clear, /graph/rebuild,
  /prune, /invalidate.
- Python package `src/gobby/code_index/`: bm25_health, cleanup, community_label_safety,
  community_labeler, context, eligibility, gcode_gateway (33K), maintenance (20K),
  maintenance_launch, maintenance_log, models, nightly_repair, prune (19K), storage,
  summarizer, summary_safety, sync_breaker, sync_worker (27K), trigger (17K),
  `_storage/`. GcodeGateway consumers: code_index/{__init__,context,sync_worker,trigger},
  runner_init/{project_purge,services}, servers/routes/code_index.py.
- Node front door (`crates/gdaemon/src/front_door/challenge.rs::answer`): interactive
  challenge is answered on the node with an HMAC proof derived from the node
  bootstrap `api_key`; managed challenges pass through. Relay forwards bytes and
  bearer to `hub_daemon_url`. `front_door/auth.rs` resolves `api_keys` rows to
  user_id + machine id; `gobby-agent-v1.` bearers are separate.

## Open decisions for the packet to #15977 (options + recommendation)
1. Node read path: (a, rec) Postgres `Brokered` grant + gcode forwards read commands
   to hub route(s) executed by the hub with the gcode library scoped to the caller's
   machine selector; (b) per-command typed JSON routes; (c) SQL over HTTP (rejected by
   decision 17).
2. Node write path: (a, rec) HTTP `CodeFactSink` posting per-file fact batches to a hub
   route, hub dedupes by content hash and runs graph/vector projection hub-side;
   (b) upload changed file bytes, hub parses.
3. Grant shape for remote-machine principals: hub issues `PostgresCapability::Brokered`
   (+ Falkor/Qdrant Brokered) for principals whose machine is not the hub's.
4. Family crate name/layering: new `crates/gcodeindex`? vs routes inside gdaemon
   depending on `gobby-code` lib; does the family crate depend on `gobby_code`.
5. Structure: plan root #21586; #23276 as a phase sub-epic `## P1: ... (#23276)`?
   (Orchestrator's call.)

## Post-compaction findings (2026-10-17, packet sent to #15977 as D1-D8)
- gcode Context needs a Postgres Client: `config/layers.rs::ServiceSource::new(conn, ..)`
  in both ConfigMode::Daemon (unless HubConfigCapture::DeferredBestEffort) and Hub;
  `Context.database_url: String`. DB-opening commands: status/current.rs::run (reads
  code_indexed_project_states by local machine id + overlay), search.rs (4), symbols.rs
  (4), grep.rs, symbol_at.rs, index.rs, init.rs, repair.rs, scope.rs, vector.rs,
  embeddings_doctor.rs.
- `gcode index` syncs projections only with `--sync-projections` (default false).
  Projection reads facts from DB only (no disk reads outside tests).
- Python `sync_worker.py::_sync_pass` uses `_storage/projects.py::list_indexed_projects`
  (WHERE machine_id = require_machine_id()) and skips roots not on hub disk.
- Broker calls present `X-Gobby-Runtime-Grant` (gcore grant/handshake.rs::GRANT_HEADER).
- gdaemon `front_door/routes.rs::RouteTable::dispatch`: Proxy backend forwards the
  whole family prefix; native handlers only under Native/Compare; unmatched -> proxy
  fallback. `gcore/src/route_family.rs::RouteFamily` has `default_backend()`.
- Runtime family: #24436 P1 and #24437 P2 closed; #24446 claimed; #24447 (grant
  signing/presentation, native verification) <- #24448 <- #24449 (native interactive
  handshake + capabilities.rs port of capabilities_from_snapshot; always issues
  PostgresDirect) <- #24450 compare.
- #23276 task_type task, category planning, blocked by #23271 (closed) and #21586.
- Shared targets: sink.rs, commands/search.rs, sync_worker.py, _storage/files.py with
  workspace-index-pin.md (root #23433, claimed by 933802ac, in flight; B3 edits
  search.rs::*, B4 edits files.py/sync_worker.py) and code-index-vector-reconciliation.md
  (root #23570, expanded, unclaimed; 1.1/1.2 edit files.py/sync_worker.py).
  runtime_grants/service.py + handshake.py shared with gdaemon-runtime-family,
  gdaemon-node-channel, gdaemon-key-cutover, local-inference-runtime-foundation.
  version_pins.py, crates/gcode/Cargo.toml, crates/gdaemon/Cargo.toml, routes.rs shared
  widely (release bumps).

## Rulings (Orchestrator #15977, 2026-10-17)
- 16:08: D1(a), D2(a), D3(a), D6(a), D7(a).
- 16:15: D9(a): only overlay-reading commands require the parent; status reports a
  missing parent as unindexed; `gcode index` on an overlay with a missing parent
  indexes the parent first in Primary mode via content-hash adoption; no node-halves
  edit. D2 refinement accepted: plan then apply; hub holds the project advisory lock
  for the apply request only, re-derives adoption under the lock, one reparse retry,
  project_checkouts fence kept.
- 16:23: Josh chose ci-routes:native-narrow (button 3e38baa5): node routes in Rust,
  native-default family at /api/code-index/node, grant verification from #24447's
  gcore presentation. D3 issuance a Python edit until #24449; remote-principal
  Brokered grant goes into the shared capability vectors in the same leaf; explicit
  acceptance: nodes need no update while the hub's Python is ported (16:24 binding
  condition). D5: writer picks placement (sent rec: crates/gcodeindex). D8 open.
- 16:31 reply sent: verification needs no #24448 (fence reads deployment_runtime,
  plan :1220-1221; lease_view.py:37-44); Brokered grants have no generation so
  revocation is skipped as servers.py:573-582 does; route binds x-gobby-machine-id
  (front_door/auth.rs:115-161) to principal.machine; route leaf makes gruntime
  fence/interactive_revoked pub.

## Index pipeline DB touchpoints (pipeline.rs)
- index_files -> connect_readwrite; index_overlay_files (overlay.rs:86);
  index_discovered_files: upsert_project_seed, get_orphan_files + delete_file_state,
  adopt_file_state (Primary: needs project_checkouts row for (machine, project,
  root); adopts only graph_synced AND vectors_synced facts), index_content_only,
  resolve_local_import_calls / resolve_project_local_import_calls,
  resolve_local_import_inheritance, refresh_communities (lifecycle.rs:61).
- index_lock.rs: PostgreSQL session advisory locks (pg_try_advisory_lock polling).
- read_local_machine_id call sites (non-test): db/queries.rs 11, communities.rs 3,
  pipeline.rs 2, freshness.rs 2, db/local_callee.rs 2, status/invalidate.rs 2,
  graph/reads.rs 2, and one each in visibility.rs, vector/code_symbols/repository.rs,
  sink.rs, overlay.rs, lifecycle.rs, config/context.rs, status/shared.rs,
  status/retire_files.rs, status/current.rs, status/content_gc.rs, scope.rs, repair.rs.
- Context::resolve_from_root (context.rs:368) takes config from the acquired grant
  settings; opens Postgres only for managed_scope::validate and
  validate_parent_code_index (context.rs:680, machine-keyed).
- Overlay scope comes from `.gobby/isolation.json` parent_project_path/id
  (context.rs:570 resolve_project_identity; Python writer
  utils/project_context.py ~395-431).
- gdaemon does not depend on gobby-code today; root members lack gruntime (in #24446).

## Final rulings (16:29): D8 (a) chosen by Josh; #15977 dropped #21586 blocks #23276 and
#21586 blocks #24380. D5 (a) accepted: crates/gcodeindex, package gobby-codeindex,
linked into gdaemon. #24447/#24448 check accepted. All D1-D9 resolved; no open questions.
Hand the stamped plan back to #15977.

## P1 design to draft (`## P1: Node code-index routes (#23276)`)
Facts for sections:
- Python handshake: `src/gobby/runtime_grants/handshake.py::HandshakeService.issue_for_operator`
  rejects machine_id != forwarded_machine_id (claims_mismatch), builds interactive
  principal, calls `_issue` (handshake.py:194) -> `self.issue_postgres(principal)` then
  `ttl = min(ttl, postgres.valid_until - now)` (Brokered has no valid_until), then
  `GrantService.issue(principal, postgres, now, ttl_seconds)` (service.py:243), which
  calls `capabilities_from_snapshot(snapshot, postgres)` (service.py:181) and only
  min()s expiry for PostgresDirect. `runner_init/servers.py::issue_grant_postgres`
  (:408) interactive branch issues interactive credential.
- Shared vectors today: `tests/runtime_grants/golden/*.json` (brokered_datastores.json
  already has postgres {mode: brokered, operations: []} for agent_run) checked by
  `tests/runtime_grants/test_golden_vectors.py` (REQUIRED_MODES, round trip, checksum
  pinned); Rust tests read the same dir (gcode vector/code_symbols/embedding.rs test).
- Fact models (crates/gcode/src/models.rs): Symbol, IndexedFile, ContentChunk derive
  Serialize/Deserialize; ImportRelation (:306), CallRelation (:341),
  InheritanceRelation (:503) derive only Debug, Clone -> add serde.
- Sink callers: indexer/file.rs::index_file (:16, PostgresCodeFactSink::new at :57,
  :148), overlay.rs (:653); tests/facts.rs RecordingCodeFactSink.
- axum Json default body limit 2 MB; node relay streams bodies (proxy::forward).
Sections:
1.1 refactor: explicit machine on gcode library paths the routes call (Context.machine_id;
   machine param for PostgresCodeFactSink::new, pipeline.rs, overlay.rs, lifecycle.rs,
   communities.rs refresh, db/local_callee.rs, db/queries.rs list_indexed_file_paths /
   read_active_imports / read_local_import_inheritance / read_project_local_import_inheritance,
   visibility.rs::local_machine_uuid, status/current.rs, context.rs::validate_parent_code_index).
   Acceptance: apply with a principal machine != local writes no local-machine rows.
2. 1.2 D9 parent-optional overlay (depends 1.1).
3. 1.3 `gobby_code::node` wire contract + library entry points: status, search_content,
   plan_index, apply_index (batches: present set + parsed facts + final flag; lock per
   request; reparse list; checkout fence). Node operation names in grant: node_status,
   node_search_content, node_index_plan, node_index_apply; POST JSON under
   /api/code-index/node/*; gcode resolves paths by operation name from the grant.
4. 1.4 Python D3: remote interactive principal (machine != hub) -> PostgresBrokered(node ops),
   Falkor/Qdrant Brokered (FALKOR_OPERATIONS/QDRANT_OPERATIONS), TTL without
   valid_until; new golden `tests/runtime_grants/golden/remote_interactive_brokered.json`;
   acceptance: nodes need no update during hub port (grant byte-identical golden).
5. 1.5 gcode brokered Context + typed `brokered_unsupported` error for every other
   DB-opening command (69 `.database_url` sites; make Context expose
   `database_url()` -> Result) (depends 1.1).
6. 1.6 gcode client: status/search-content/index over routes (depends 1.3, 1.5).
7. 1.7 crates/gcodeindex family: RouteFamily name code_index_node, prefix
   /api/code-index/node, default_backend Native; verify grant (gruntime fence,
   verify_static, Brokered -> no revocation lookup, x-gobby-machine-id ==
   principal.machine else 403 claims_mismatch); spawn_blocking sync postgres from
   bootstrap DSN; register in gdaemon front_door/routes.rs FAMILIES; root members;
   make gruntime fence/interactive_revoked pub; bump gdaemon (depends: #24447, 1.3).
8. 1.8 native handshake reproduces remote golden (depends: #24449, 1.4).
Later phases: P2 projection for node machines (sync_worker.py/_storage/projects.py,
order after workspace-index-pin B4 #23433 and code-index-vector-reconciliation 1.1/1.2
#23570); P3 node read parity (D7); P4+ Python src/gobby/code_index port to gcodeindex
(gateway, lifecycle, prune, sync worker, trigger, maintenance, summarizer, routes) with
Compare and native flip. Needs task numbers of overlapping leaves at draft time.

## Rulings 16:47 (#15977) and post-compaction facts
- Scope (b): adversary 16993 reviews P1 as soon as it validates and commits; I draft
  P2, P3 and the port phases to the same depth meanwhile; 16993 re-reviews the delta.
- Cross-plan edges accepted: 1.1 (depends: #23831 B1), fact wire contract (depends:
  #23577 vector 1.3), remote golden (depends: #23831). Other overlaps (#23833 B3,
  #23834 B4, #23835 B5, #23836 C1, #23582 vector 3.2) = Constraints concurrent editors.
- Task map: workspace-index-pin B1 #23831 (claimed) B2 #23832 B3 #23833 B4 #23834
  B5 #23835 C1 #23836 C3 #23838; vector-recon 1.1 #23575 (landed) 1.2 #23576 1.3 #23577
  1.4 #23578 2.1 #23579 2.2 #23580 3.1 #23581 3.2 #23582 D1 #23584.
- Family registration pattern (runtime 3.1): family crate exports `XFamily::new(pool, home)`,
  `router<S>()`, table-driven `claims`; `serve::run` builds it on the shared pool,
  `Box::leak`s it, composes after `FAMILIES`. Hub-only (node mode has no native set).
- Grant principal: project_id = `.gobby/project.json` id (parent/main), and
  code_overlay_project_id = `code_index_id_for_root(root)` for an isolation root
  (gcore acquisition.rs:190-197). Routes derive project ids from the principal.
- Python capability check: `_capability_matches` (service.py:215) brokered ->
  `required.operation in capability.operations`. No production PostgresBrokered today.
- Goldens: Python `_golden_paths` and Rust `gcore/src/grant/tests.rs::golden_vectors_match_python`
  enumerate the directory; B1 #23831 rewrites every golden's schema identity.
- Machine reads on node paths: index (IndexTarget types.rs:125, sink new, pipeline
  index_discovered_files/index_explicit_files_with_connection, overlay index_overlay_files,
  local_imports.rs, queries list_indexed_file_paths/read_active_imports/
  read_local_import_inheritance/read_project_local_import_inheritance, local_callee x2,
  communities refresh_with_partition_builder, communities/identity.rs import_rows);
  status (current.rs run + overlay_status_json, catalog tombstone_count); search
  (search.rs search_content, fts/content.rs visible_files_sql, scope.rs other_project_for_path).
- Node checkouts registered by node-halves 2.9 (POST /api/nodes/checkouts on first hook);
  worktrees/clones never registered -> Overlay mode has no fence; parent Primary is fenced.

## Still to do
- Read plan-coverage.md lines 730-908 (Table-Row Decomposition: one acceptance item
  per table data row).
- Read gdaemon-runtime-family.md phase structure (RouteFamily, Compare, Python
  rollback) as template.
- Run scratchpad overlap.py against code-index-vector-reconciliation.md and
  workspace-index-pin.md (and node-halves.md) for shared-target ordering.
- Check gcode index inline projection behavior and status/search-content read paths.
- Verify #21586 upstream edges (#24447 Grant signing).

## Session 2 (after the second compaction, 2026-10-17)
- P1 committed at 351cd85b5d (validation clean). 16993 reviewing P1 (mode b).
- 1.6 change: remote decision lives in `HandshakeService._issue` (has
  `local_machine_id`), not `issue_grant_postgres`; servers.py and its tests unchanged.
- Node agents run gcode as interactive principals: node spawns have no managed
  bootstrap (`gcore grant/acquisition.rs::acquire_with_reachability` picks
  interactive without `managed_bootstrap_path`; node-halves 2.11 "no managed
  credential"). Add this boundary sentence to D3 with the P1 review fixes.
- #15977 rulings (P2/D6): Q1(a) node projection in the native sync worker on
  gdaemon `HeartbeatHost` (`crates/gdaemon/src/heartbeat.rs`) via
  `Context::for_machine`, ordered after #23834 and #23576, no throwaway Python.
  Q2(a) content-hash-keyed projections: a sync removes only versions no machine's
  selector or pin names; reads use #23834's machine predicate. Phase order accepted.
  Josh 16:00 goal: nodes must run local embedding (among the first multi-machine
  fixes); P6 must not gate it; plan states which phase delivers it.
- Facts: vector sync deletes all (project, path) points except kept ids
  (`vector/code_symbols/qdrant.rs::delete_vectors_for_filter_excluding_ids`);
  collection per project (`lifecycle.rs` `collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id)`);
  Falkor uses one configured `graph_name`. Embed capability is always `daemon`
  (`service.py::capabilities_from_snapshot`, `EMBED_OPERATION` POST /api/embeddings);
  node front door relays it to the hub today.
- Sent #15977 (pending ruling) embedding packet: E1(a) node gdaemon serves
  `/api/embeddings` natively from hub effective embeddings config against the node's
  own endpoint, typed 503 `embedding_identity_mismatch`; E2(a) node-driven pull:
  node heartbeat job runs `gcode vector sync-node` -> `node_vector_pending` /
  `node_vector_apply`; E3 phases: P2 read parity, P3 content-hash projections,
  P4 node-local embedding, P5 family seam, P6 graph read routes, P7 mutation routes,
  P8 heartbeat work (sync worker, trigger, maintenance, summarizer), P9 native
  default + Python deletion.
- #15977 RULED (22:2x): E1(a), E2(a), E3 accepted, P1-P9 as proposed; node-local
  embedding in P4 after P1 and P3 only. State the interim behaviour (query embedding
  goes to the hub until P4) in the plan's scope section (Overview). These settle D6
  and embedding placement. Finish the draft; 16993 reviews and stamps; #15977
  presents the whole plan to Josh.
- 16993 P1 review at 351cd85b5d: message id 255639b7-e8b6-4116-86f9-cb450af2dee8
  (get_inter_session_message). B1 1.8 client classifies non-2xx by body `code` first
  (grant codes -> GrantError via from_presentation_http, one re-acquire+retry on
  is_retryable_presentation; other RouteError -> CliError with that code; add
  acceptance: 409 index_busy no refresh, 409 stale_epoch exactly one re-acquire).
  B2 Target `crates/gcode/src/daemon.rs::*` (variant + transport helpers pub(crate)).
  B3 1.4 uses `with_project_lock(ctx, IndexLockPolicy::Wait{max_wait: 60s})`, Busy ->
  index_busy; drop new variant and `index_lock.rs::IndexLockPolicy` Target.
  B4 move 1.9.6 to a gdaemon test (e.g. crates/gdaemon/tests/front_door.rs) and
  Target it; state in 1.9 that composing outside FAMILIES with Native default is
  deliberate (routes.rs:701-709 asserts FAMILIES default Proxy).
  N1 Python model is `BrokeredCapability` (schema.py:49-53), not PostgresBrokered.
  N2 root Cargo.toml is in every binary's source identity: say gclient/gterm are
  not released by 1.9 (bump at next release) or bump them. N3 1.8 search-content
  paths resolve relative to project root via NodeAccess; token_budget/verbose are
  render-only. N4 state the rule for which codes the contract lists (node codes).
  Also add D3 sentence: node agents are interactive principals (no managed bootstrap).
  16993's successor re-reviews the repair SHA.
- Validator lessons this session: one scope form per file plan-wide (`::*` with
  scope-reason when new symbols are added); split target paragraph needs literal
  `move`; Consumers unchanged must name every consumer the validator lists.

## Session 3 (after the third compaction, 2026-10-17/18)
- P1 repair committed cf06c8dd17 (B1-B4, N1-N4, D3 sentence) and sent to 16993
  (msg f07d6876) and #15977 (msg e3f1cf9e). Found while repairing: hub-side
  search_content dropped node results (disk check in `scope::current_indexed_path_is_valid`
  = `visibility::indexed_file_exists` + `scope::path_exists_in_current_project`);
  1.3 now splits `content_candidates` (hub) / `page_content(exists predicate)` (node).
  `visibility.rs::indexed_file_exists` (local machine read :143) joined 1.1;
  `other_project_for_path` only called by symbols.rs (off node paths, removed from 1.1).
- Uncommitted (goes in the P2 commit): 1.8 grant refresh now calls
  `gobby_core::grant::rehandshake` directly (re-exported in grant/mod.rs, returns
  AcquiredGrant) and keeps the new bundle; `present_bundle_with_single_retry` returns
  only T.
- `ProjectView` trait (context.rs; project_root, project_id, index_scope, quiet)
  defined in 1.3, NodeAccess impl in 1.7; scope.rs resolve_path_input/scope_roots/
  path_exists_in_current_project generic in 1.8.
- Contract rule (N4): every code a P1 path produces is listed; pre-existing omissions
  (capability_unavailable, daemon_required, payload_skew, project_request,
  prune_request) stay.
- Advisor plan for P2-P9: commit per phase, send 16993 each delta, don't wait. P2 commit
  also carries Decision Record D10-D14 (Q1, Q2, E1, E2, E3), Overview interim note
  (query embedding on hub until P4) and critical path P1 -> P3 (#23834) -> P4.
  P2: 2.1 node freshness first (plan call as probe; rewrites 1.7 recovery string, so
  cli_error.rs ordering on 1.7); one typed route per command, leaves grouped by shared
  result model; commands that read source bytes split like 1.3; graph commands need
  routes (Brokered Falkor -> None) and share gobby_code graph entry points with P6;
  NODE_CODE_INDEX_OPERATIONS grows -> regenerate 1.6 golden (P1 nodes keep working:
  lookup by name, `_capability_matches` membership); new per-group bare-path files
  (node/hub/<group>.rs, gcodeindex/src/node/<group>.rs). Check whether Projects
  resolves a Context (it uses GET /api/projects).
- Before P5/P9: check RouteTable prefix matching (first-match vs longest) for
  /api/code-index/node under /api/code-index; #21586 says preserve canonical session
  summary generation and hub file forwarding owners (exclude from P9 deletion).
  Fetch memories da52a076 (V2/E1 present tense) and 09ad0f77 (migration numbers).
- gcode commands (cli.rs:65 `Command`): read = Search, SearchSymbol, SearchText,
  SearchContent(P1), Grep, Outline, Symbol, SymbolAt, Symbols, Kinds, Tree, Callers,
  Callees, Usages, Imports, Path, BlastRadius, RepoOutline, Evidence, Graph{Report,
  Overview, File, Neighbors, BlastRadius, View}, Status(P1), Projects; write = Init,
  Index(P1), Repair, Invalidate, RetireFiles, Prune, Graph{SyncFile, Clear, Rebuild,
  CleanupOrphans}, Vector{SyncFile, Clear, Rebuild, CleanupOrphans},
  Embeddings{Doctor}; no DB = Contract, SchemaIdentity.
- Entry points (dispatch_navigation.rs): search::{search, search_symbol, search_text,
  search_content}, grep::run, symbols::{outline, symbol, symbols, kinds, tree},
  symbol_at::run (+ requested_file_for_freshness), graph::{callers, callees, usages,
  imports, path, blast_radius} (graph/reads.rs 967 lines; Falkor via
  code_graph::find_*/count_*, require_graph_reads), status::repo_outline.
  Sizes: search.rs 899, symbols.rs 930, grep.rs 953, graph/reads.rs 967, symbol_at.rs
  696, evidence.rs 299 -> splits need new files + "move" paragraphs.
- Freshness: dispatch.rs:46 ensure_project_fresh -> freshness::ensure_fresh(ctx,
  FreshnessScope::Project).
- 16993 re-review of cf06c8dd17 (msg in context 2026-10-18): P1 B1-B4/N1-N4 resolved,
  two extra defects confirmed; nit N5 (1.1 consumers unchanged: grep.rs:380 and
  search.rs:663/:707) FOLDED (uncommitted, goes in P2 commit). No blocking P1 findings;
  consensus + M1 wait for complete plan. Tell 16993 the 1.8 grant-refresh wording
  changed to direct `rehandshake` (it had checked the stash pattern).
- P2 facts: `projects` takes no Context (`commands/status/projects.rs::projects` ->
  `daemon::list_projects` GET /api/projects, relayed by node front door) -> no route.
  Disk reads in read commands: `symbol_at.rs:88` (source), `symbols.rs::read_symbol_source`
  (:345). Evidence: `commands/evidence.rs::run` builds `EvidenceLibrary::new(project_root,
  binding, Arc<CodewikiFacts>)` + `with_hybrid(NativeHybridSearch)`; seams
  `evidence/mod.rs::EvidenceFacts` (9 methods: project_id, files, search_symbols,
  search_content, grep, symbols_for_file, symbol_by_id, graph_edges,
  project_communities) and `HybridSearch` (effective_identity, search_symbol_ids) ->
  node impls over routes; git/working tree stay on node.
- Hub services for graph/hybrid: Context has falkordb/qdrant/embedding Option configs
  (context.rs:198-202); hub resolves through `config/layers.rs::ServiceSource::hub(
  capture_hub_snapshot(conn))` (ConfigMode::Hub path). P1 HubScope sets services off.
- Search: `search.rs::semantic_lane` -> `vector/code_symbols/search.rs::semantic_search(ctx,
  query, limit)` embeds via `embedding.rs::embedding_source_from_context` /
  `embed_query_with_source`. DESIGN (chosen): P2 search route takes optional
  `query_vector`; node embeds via its daemon URL (relayed to hub until P4, native after),
  so P4 needs no search change and "query embedding on hub until P4" holds.
  Symbol-search filters also disk-check (search.rs:663/:707) -> same candidates/page split.
- Graph reads: `commands/graph/reads.rs` (967 lines) callers/callees/usages/imports/
  path/blast_radius via `code_graph::{count_*,find_*}` + `require_graph_reads` (Falkor).
- COMMITTED 5364daf: P1 refined to ONE grant operation `node_code_index` (POST
  /api/code-index/node); route paths = `gobby_code::node` wire contract; D3 carries the
  rejected per-route alternative (grant churn + 1.10/#24449 ordering hazard). Also in
  5364daf: 1.8 calls `gobby_core::grant::rehandshake` directly; N5 (grep.rs consumer;
  search.rs rejected by validator because 1.3 targets it). Sent 16993 (msg 127c6504)
  and #15977 (msg 0e80af26). So P2/P4 route additions need NO grant/golden/Python
  change: they only add paths in gobby_code::node and routes in gcodeindex.
- RouteTable (`front_door/routes.rs::RouteTable::lookup`) picks the LONGEST matching
  prefix, so /api/code-index/node beats a later /api/code-index family (P5 safe).
- P2 leaf plan (draft next): 2.1 node freshness (ensure_fresh on NodeAccess: plan call
  as probe, non-empty parse or parent_indexed false = stale -> P1 node index unless
  --allow-stale; also make 1.7's brokered_unsupported recovery text generic: "run it on
  the hub"; cli_error.rs shared with 1.7). Then one typed route per command, grouped:
  2.2 symbol search (search/search-symbol/search-text; hub candidates without disk
  checks; node disk filter + page; search request carries optional query_vector PLUS
  embedding identity (model, dimensions) -> hub returns semantic_identity_mismatch on
  mismatch; hub context services via ServiceSource::hub), 2.3 grep, 2.4 symbol
  navigation (outline/symbol/symbols/kinds/tree/repo-outline; symbol source read on
  node via read_symbol_source), 2.5 symbol-at (source on node), 2.6 graph navigation
  (callers/callees/usages/imports/path/blast-radius; Falkor on hub; share
  gobby_code graph entry points with P6), 2.7 `gcode graph` report/overview/file/
  neighbors/blast-radius/view, 2.8 evidence (node EvidenceFacts + HybridSearch impls
  over routes; git + working tree stay on node). projects needs no route. New bare-path
  files per group: crates/gcode/src/node/hub/<group>.rs, node/client/<group>.rs,
  crates/gcodeindex/src/node/<group>.rs. Big files need split + "move" paragraph:
  search.rs 899, symbols.rs 930, grep.rs 953, graph/reads.rs 967.
- P2 commit also carries: Decision Record D10-D14 (Q1, Q2, E1, E2, E3), Overview interim
  note (query embedding on hub until P4: node /api/embeddings relays to hub), critical
  path P1 -> P3 (#23834) -> P4.
- Seat rule reminder: write repo files with Edit/Write only (one Bash heredoc append
  happened this session; no harm, don't repeat).

## Session 4 (2026-10-18): P2 design settled, not yet written into the plan
- DONE uncommitted in plan: N6 fixed (1.1 grep.rs / install_setup_gcode.py consumer lines split).
- Memories read: da52a076 (V2/E1 gates: open with "These are completion gates... Except for
  plan validation, none has run yet"; must-statements only); 09ad0f77 (migration number:
  `git log --all --diff-filter=A --name-only --format= -- 'crates/gcore/assets/schema/migrations/NNN_*'`
  across all refs, plus "executor takes the next free number" fallback).
- Validator size rule (plan-coverage.md:254 production-size-growth): Rust line count STOPS at
  first `#[cfg(test)]` line. Effective counts: search.rs 892 (split needed, `move`),
  symbols.rs 17, grep.rs 8, graph/reads.rs 684, class_hierarchy.rs 43, models.rs 717 (no
  validator split). BUT Constraints' 1,000-line ceiling counts inline tests: grep.rs 953
  real (no inline tests; move hub half to new `commands/grep/candidates.rs`), symbols.rs 930
  (inline tests 532-926 -> move to new `commands/symbols/tests.rs`), graph/reads.rs 967
  (inline tests 685-965 -> new `commands/graph/reads_tests.rs`), search.rs 899 (move the
  three symbol-search bodies to new `commands/search/symbol_page.rs`).
- Advisor checklist (apply all): P1 text P2 touches -> list in 16993 delta: 1.8 sentence
  "--allow-stale and --quiet keep their meaning. A node has no local freshness check to
  skip." -> "`--quiet` keeps its meaning; a node runs no freshness check until 2.2";
  remove 1.1 consumers grep.rs + symbols.rs and 1.8 consumers symbol_at.rs + symbols.rs
  (P2 targets them; keep dispatch_navigation.rs untouched, node goes through
  dispatch.rs::run -> node::client); Overview line "This revision carries P1" ; E1 must
  gain P2 gates (rewrite opener per da52a076). Scope forms match P1: `::*` for
  context.rs, vector/code_symbols/search.rs, graph_boost.rs, daemon.rs, contract JSONs,
  test files; exact for visibility.rs, commands/scope.rs, commands/search.rs,
  search/output.rs, freshness.rs, cli_error.rs, contract.rs, models.rs, db/queries.rs,
  visibility/catalog.rs, communities.rs. Every route leaf: Granularity line,
  version_pins Target, bump acceptance (gcode + gdaemon, post-1.9), pin consumers boilerplate
  (install_setup_gcode.py, install_setup_gdaemon.py, gcode_gateway.py, test_version_pins.py).
- P2 per-group files (bare paths): `crates/gcode/src/node/<g>.rs` (wire types + route path
  consts, declared in node/mod.rs), `node/hub/<g>.rs` (declared in node/hub.rs),
  `node/client/<g>.rs` (declared in node/client.rs), `crates/gcodeindex/src/node/<g>.rs`
  (declared in gcodeindex node.rs; registered in gcodeindex lib.rs `router`), tests
  `crates/gcode/src/node/<g>_tests.rs` and `crates/gcodeindex/tests/node_<g>.rs`. Shared
  per leaf -> linear chain 2.2->2.3->...->2.8: node/mod.rs, node/hub.rs, node/client.rs,
  gcodeindex/src/node.rs, gcodeindex/src/lib.rs, dispatch.rs::run. 2.2 changes
  `CodeIndexNodeFamily::claims` to hold for POST paths in `gobby_code::node::ROUTE_PATHS`
  (const slice) so later leaves only add paths. No grant change (one op).
- P2 LEAVES:
  2.1 Machine-explicit read paths [refactor] (depends: 1.1). `visibility.rs`
  `local_machine_uuid`/`local_machine_uuid_or_invisible` take `&Context`, read
  `ctx.machine_id` (rename `machine_uuid`/`machine_uuid_or_invisible`); no-ctx fns gain a
  machine param. Exact targets: visibility.rs::{local_machine_uuid,
  local_machine_uuid_or_invisible, local_active_content_hash, content_chunks_exist,
  symbol_is_visible, project_path_is_visible, project_file_is_visible, overlay_has_row,
  indexed_file_states, query_symbols_for_files, query_overlay_symbols_for_files};
  visibility/catalog.rs::{visible_kinds, visible_tree}; visibility/graph.rs::visible_graph_paths;
  search/fts/common.rs::{push_visible_project_file_filter, query_symbols_by_conditions};
  search/fts/graph.rs::* (exact_symbol_matches_result takes machine; resolve_graph_symbol(_by_id)
  pass it); commands/grep.rs::{load_indexed_chunks, indexed_files_cover_filters};
  commands/scope.rs::other_project_for_path; commands/graph/view/mod.rs::{local_machine_id,
  visible_map_for_candidates}; graph/view/fcg.rs::fetch_fcg_hop; graph/view/mcg/fetch.rs::fetch_mcg_hop;
  graph/view/class_hierarchy/fetch.rs::falkor_page; communities.rs::{read_for_context,
  partition_refreshed}; graph/code_graph/read/relationships.rs::get_imports (passes machine to
  local_active_content_hash). Tests: visibility/tests.rs::* foreign-machine reads.
  EXCLUDE write/maintenance: content_gc, retire_files, invalidate, repair,
  vector/repository.rs:95, status/shared.rs collect_projects_from (prune only), db/queries
  read_graph_file_facts + mark_* (projection, P3/P8). Acceptance: no read path calls the
  local reader; foreign-machine tests per helper group.
  2.2 Node freshness (depends: 1.8, 2.1). Route `/api/code-index/node/freshness`:
  `FreshnessRequest{scope}` -> `FreshnessFacts::{Refresh, Compare{last_indexed_at, indexed_paths}}`
  (Refresh = never indexed for machine, or overlay parent indexed later). freshness.rs
  `project_needs_refresh` splits into `freshness_facts(conn, ctx)` (hub) +
  `facts_need_refresh(root, &facts, &indexing)` -> `api::project_changed_since`
  (index/indexer/freshness_probe.rs; SystemTime last_indexed_at). Node project scope: facts,
  then node index run (1.8 flow) when stale. Files scope: index run with path_filter = files.
  `IndexApplyRequest` gains `freshness: bool` -> hub uses `IndexLockPolicy::brief_freshness_try()`
  (150 ms) instead of Wait 60 s; `index_busy` during freshness -> `FreshnessStatus::SkippedBusy(None)`;
  other failure -> Degraded(detail). dispatch.rs `freshness_warning`, `freshness_skip_notice`
  become pub(crate) for node warnings. Node keeps INFLIGHT_ENV check. Symbol scope
  (ensure_symbol_fresh) lands in 2.5 (needs /symbols/ids). Also rewrite 1.7's
  `brokered_unsupported` recovery generic ("needs direct database access; run it on the
  hub"), cli_error.rs::CliError. Why not plan-as-probe: plan omits adoptable paths, so a
  never-indexed machine reads fresh, and it hashes the whole tree per read; local pre-gate is
  mtime-based.
  2.3 Symbol search (search, search-symbol, search-text) (depends: 2.2). Routes `/search`,
  `/search-symbol`, `/search-text`. Hub returns sorted unpaged candidates filtered by kind/
  language/path patterns + `indexed_file_exists` (no disk); node drops by
  `path_exists_in_current_project`, totals after filter, pages, computes hints
  (search_lane_hint, path hint) and renders. search-text keeps db_total like 1.3. Request
  carries `query_vector: Option<QueryVector{values, model, dimension}>`; node embeds via
  daemon (`embedding.rs` new `daemon_query_embedding(project_id, &GrantAiRuntime, query)`, like
  audited_query_embedding's daemon branch over `ai_source_without_primary`); grant outage ->
  node adds `semantic_lane_from_grant_outage()` warning, sends no vector. Hub: vector present
  -> model == hub ctx.embedding.model and dimension == code_vectors.vector_dim (when set) else
  `SearchWarning{lane: Semantic, cause: EmbeddingIdentityMismatch}` (degrade, not error; so
  no contract code); `vector/code_symbols/search.rs` splits `semantic_search` into embed +
  `semantic_search_with_vector`. models.rs: SearchWarning/SearchWarningLane gain Deserialize,
  SearchWarningCause gains EmbeddingIdentityMismatch. Hub services: `Context::for_machine`
  gains `ServiceConfigSelection`; `config/layers.rs` adds `ConfigLayers::hub()` (Hub mode ->
  `ServiceSource::hub(capture_hub_snapshot(conn))`); P1 routes pass database_only().
  search.rs split -> `commands/search/symbol_page.rs` (move). graph_boost on hub (Falkor).
  2.4 Grep (depends: 2.3). Route `/grep`. Hub `grep_candidates` = load chunks (indexed_file_exists
  only) + scan all matches + coverage gap; node drops matches whose file is missing, pages
  (max_count/offset/files_with_matches), renders. GrepMatch/GrepSpan/GrepContextLine gain
  Deserialize. Move hub half to `commands/grep/candidates.rs` (grep.rs 953).
  2.5 Symbol navigation + symbol-at (depends: 2.4). Routes `/symbols/file` -> `FileSymbols{
  symbols, indexed, chunks_exist, owner}` (outline + symbol-at; node computes
  outline_missing_diagnostic with its disk check), `/symbols/ids` (symbol, symbols, node
  ensure_symbol_fresh: fetch, symbol_slice check on node, Files freshness, refetch),
  `/symbols/kinds`, `/symbols/tree` (tree + repo-outline). Source reads + savings on node
  (`read_symbol_source`, symbol_at source). VisibleFile gains Serialize/Deserialize.
  ProjectView gains `grant_bundle()` for savings::report_savings. symbols.rs inline tests ->
  `commands/symbols/tests.rs`.
  2.6 Graph navigation (callers, callees, usages, imports, path, blast-radius) (depends: 2.5).
  Routes `/nav/callers|callees|usages|imports|path|blast-radius`. Hub returns
  `GraphRead<T>::{Unavailable{hint}, Unresolved{suggestions}, Found{symbol, total, results, hint}}`;
  pages on hub (no disk reads); node renders with existing format fns. ResolvedGraphSymbol
  gains serde. graph/reads.rs inline tests -> `commands/graph/reads_tests.rs`. P6 reuses
  these hub entry points.
  2.7 `gcode graph` report/overview/file/neighbors/blast-radius/view (depends: 2.6). Routes
  `/graph/report|overview|file|neighbors|blast-radius|view`. ProjectGraphReport and
  GraphPayload already Deserialize; ViewPayload/ViewSeed/ViewNode/ViewEdge/ViewCommunity
  gain Deserialize + pub(crate). view sub-runs (mcg::run, fcg::run, class_hierarchy::run,
  communities::run_list/run_detail) return ViewPayload; view::run prints (print_view).
  2.8 Evidence (depends: 2.7). One route `/evidence/facts` with `FactsCall` enum (one arm per
  EvidenceFacts method: project_id, files, search_symbols, search_content, grep,
  symbols_for_file, symbol_by_id, graph_edges, project_communities; plus HybridSearch
  effective_identity, search_symbol_ids with query vector). Node `NodeEvidenceFacts` +
  `NodeHybridSearch` forward; git + working tree stay on node. codewiki_facts types
  (FileFact, SymbolFact, ContentFact, SearchQuery, GrepQuery, GrepOutcome, ScopedGraph,
  ProjectCommunities, ScopeSelector, GraphScopeMode, GraphEdgeKind, bounds) and
  evidence/mod.rs FactPage gain serde. Node hybrid identity check = model + dimension.
  2.9 E2E node read parity [test] (depends: 2.8) in tests/e2e/test_hub_node_pair.py.
- Decision Record to add in the P2 commit: D10 Q1(a), D11 Q2(a), D12 E1(a), D13 E2(a),
  D14 E3 order; Overview interim note (query embedding on hub until P4: node
  /api/embeddings relays); critical path P1 -> P3 (#23834) -> P4.

## Session 5 (2026-10-18): P2 WRITTEN into the plan (uncommitted), P1 edits pending
- DONE in plan file (uncommitted): `## P2: Node read parity` framing + 2.1-2.9, between
  1.11 and `## E1`. Chain: 2.1 (depends: 1.9) -> 2.2 -> ... -> 2.8; 2.9 (depends: 2.8, 1.11).
  Design deltas vs Session 4: 2.1 depends on 1.9 (shared-target-ordering rule,
  plan-coverage.md:253: every pair of owners of a path, incl. Cargo.lock/version_pins.py,
  needs a dependency path; `(depends: P<N>)` expands to all deliverables). 1.7's
  recovery text made generic in P1 itself (not 2.2). gcode grep: hub scans with
  accept-all predicate, returns `GrepCandidates{result, chunks_by_file}`, node drops
  missing files and corrects scanned_chunks/matched_lines; `grep_repo_with(ctx, conn,
  options, exists)` new; `CodewikiFacts::for_hub` uses accept-all. Evidence: `run`
  splits into `run_with(view, prepared, facts, hybrid, debug)`; errors travel as
  `FactsReply::Failed{message}` (no new contract code). 2.2 halves live in new
  `freshness/facts.rs` (freshness.rs 972 lines). symbols.rs and graph/reads.rs inline
  tests move to `commands/symbols/tests.rs`, `commands/graph/reads/tests.rs`.
- REMAINING P1/plan edits for the same commit (list them all in the 16993 delta):
  1. 1.10 heading: `(depends: 1.6, 2.8, #24449)` so P2 never waits on #24449 (release
     files force one order over bumping leaves); add a Constraints/Release-ownership
     sentence saying 1.10 sits after the last bumping leaf drafted so far.
  2. 1.3 wire contract: add `ROUTE_PATHS` const slice; 1.9 `claims` holds for POST
     paths in `gobby_code::node::ROUTE_PATHS` (P2 framing already says this).
  3. 1.7 recovery text -> "this command needs direct database access; run it on the hub".
  4. 1.8 sentence (line ~956) -> "`--quiet` keeps its meaning. A node runs no freshness
     check until 2.2, so `--allow-stale` has nothing to skip until then."
  5. Remove consumers: 1.1 grep.rs + symbols.rs lines; 1.8 symbol_at.rs + symbols.rs.
  6. Overview: "This revision carries P1." -> P1 and P2; add interim note (query
     embedding runs on the hub until P4: node front door relays /api/embeddings) and
     critical path P1 -> P3 (after #23834) -> P4.
  7. Decision Record D10-D14 (Q1(a) native sync worker on gdaemon HeartbeatHost via
     Context::for_machine after #23834/#23576; Q2(a) content-hash-keyed projections;
     E1(a) node gdaemon serves /api/embeddings natively, typed 503
     embedding_identity_mismatch; E2(a) `gcode vector sync-node`; E3 phase order, P4
     after only P1 and P3).
  8. E1: "These are completion gates for P1 and P2. Except for plan validation ... none
     has run yet." Add P2 gates (cargo nextest gobby-code/gobby-codeindex, e2e
     test_node_read_commands_through_hub, live node read commands) per da52a076.
  9. V1 changelog line for P2 draft.
- Then: validate bare, load code-review skill, `ocr delegate rule --format json
  .gobby/plans/code-index-family.md` in its own call, commit
  `[gobby-#21586] docs: draft code-index-family P2 (node read parity)`, send 16993 SHA +
  the P1 edit list above (no wait). Then P3-P9.

## Session 6 (2026-10-17, after cutover 779e424ea5): P2 COMMITTED 02da675d0d
- All nine Session 5 edits done. Validation forced: full paths in scope-reasons;
  2.4 dropped the codewiki_facts/text.rs consumer line (2.8 targets it).
- 2.1 WIDENED (these have no Context today, verified with gcode): machine (Uuid) param on
  `query_symbols_by_conditions` (fts/common.rs:351), fts/symbols.rs `search_symbols_fts`
  and `search_symbols_by_name`, fts/graph.rs `exact_symbol_matches_result`,
  `resolve_graph_symbol`, `resolve_graph_symbol_by_id`, and visibility.rs
  `indexed_file_states`, `query_symbols_for_files`, `query_overlay_symbols_for_files`.
  `reads.rs::resolve_symbol_with_connection(conn, ctx, input)`; `filter_visible_symbols`,
  `visible_symbols_for_files`, `query_visible_symbols_by_conditions`,
  `view/mod.rs::resolve_view_seed` pass ctx. reads.rs inline tests move in 2.1 (not 2.6).
  New 2.1.4 resolver test; bump renumbered 2.1.5. Consumers unchanged added:
  fts/counts.rs, search/fts.rs, graph/report/loading.rs.
- Delta sent to 16993 (queued: dbba79f4 paused, no_live_wake_channel); #15977 told.
- NEXT: P3 (content-hash-keyed projections, D6/D11) after #23834 + check migration
  numbers across refs (memory 09ad0f77); then P4..P9, one commit + delta each.

## Session 6b: P3 research (projections are ALREADY content-hash keyed; rung 1 applies)
- #23834 (Vector and graph pin scope with background recovery; unclaimed, blocked by
  #23833) adds `projection/sync/pinned.rs`, exact-version `read_graph_file_facts` and
  `fetch_symbols_for_file` variants, child-namespace projection keyed by content hash,
  and read admission = versions named by a live pin or owned selector. Targets incl.
  commands/vector.rs::*, graph/lifecycle.rs::*, graph/reads.rs::*,
  relationships.rs::*, projection/sync.rs::*, db/queries.rs::*,
  vector/code_symbols/repository.rs::*, sync_worker.py, _storage/files.py.
- Facts already per version: `code_indexed_files` (project, path, content_hash) holds
  graph_synced/vectors_synced + *_attempted_at; `code_indexed_file_states` is the
  per-machine selector (machine_id, project, path, content_hash).
- Qdrant: point id = symbol.id = uuid5(project:path:file_content_hash:name:kind:byte)
  (models.rs:169). `sync_file_symbols` only upserts (lifecycle.rs:77); nothing deletes
  other versions at sync. `delete_vectors_for_filter_excluding_ids` is only called with
  `&[]`. Collection per project.
- Falkor: relationships carry `content_hash`, CodeSymbol carries `file_content_hash`;
  `delete_stale_file_graph_queries` (write/deletion.rs:103) deletes only rows of the
  SAME content hash with another sync_token. CodeFile node is per (path, project) with
  last-writer `symbol_count`/`sync_token` (sync_plan.rs header). `sync_no_fact_file`
  deletes stale same-hash rows + a bare CodeFile only.
- Orphan cleanups already keep every machine's paths: graph
  `lifecycle.rs::cleanup_deleted_project_graph` and vector `vector.rs::cleanup_orphans`
  use `db::list_all_machine_indexed_file_paths` (queries.rs:58).
- clear/rebuild: `reset_vectors_sync_for_project` / `reset_graph_sync_for_project`
  reset ALL machines' selected versions; rebuild re-projects local versions only
  (`fetch_symbols_for_project`, `mark_project_vector_sync_attempted/_synced` read the
  local machine), so node versions stay pending for P4/P8 workers. OK.
- Content GC (`status/content_gc.rs::discover_content_gc`, :48) reads the local machine
  id: CHECK what for (lines 75+) before claiming GC keeps node versions.
- Still process-machine (P3 must make machine-explicit for P4 sync-node + P8 worker):
  db/queries.rs `list_indexed_file_paths`(:34), `read_graph_file_facts`(:88),
  `mark_graph_sync_attempted`(:118), `mark_graph_synced`(:146),
  `dirty_graph_sync_for_file`(:198), `mark_vector_sync_attempted`(:242),
  `mark_vectors_synced`(:270), `mark_project_vector_sync_attempted`(:319),
  `mark_project_vectors_synced`(:337), local-import readers (:380, :506, :574);
  `vector/code_symbols/repository.rs::fetch_symbols_where`(:95); projection/sync.rs
  `sync_graph_file`(:606) and `VectorProjectionState::sync_file`(:646) pass
  `ctx.project_id` only. #23834 also targets queries.rs/repository.rs/sync.rs ->
  P3 depends on #23834 (shared files; executor re-resolves).
- Likely P3 shape (to decide, then ask advisor): one leaf "machine-explicit projection
  selection" (the list above takes the machine from Context; sync.rs passes ctx) +
  acceptance that a foreign machine's version projects without deleting the hub's
  (Qdrant + Falkor coexist), + Decision Record note that keys already include the
  content hash so P3 adds no key migration. Possibly no Falkor schema change. Verify
  CodeFile symbol_count consumers (graph report?) before claiming it is harmless.

## Session 7 (2026-10-17): P3 + #16993 P2 fixes COMMITTED 1f1f29ecce
- P3 = framing + 3.1 Machine-explicit projection selection (depends 2.8, #23834):
  queries.rs read_graph_file_facts + 6 mark_*; repository.rs fetch_symbols_for_file/
  _for_project/_where take machine_id: &str; callers sync.rs sync_graph_file/sync_file,
  lifecycle.rs sync_file_graph/rebuild_project_graph, vector.rs sync_file/rebuild.
  Acceptance 3.1.1-3.1.4 (bump gcode+gdaemon). 1.10 now depends on 3.1.
- Checked: content GC reads the local machine only for the hub root_path (git history);
  it skips versions any selector names. CodeFile.symbol_count is write-only.
- #16993 P2 review (msg 43129823): B5 (ureq into_string 10 MiB cap; 1.8 uses into_json,
  new 2.4.4), N7 (2.8 node grep drops missing files, new 2.8.5), N8 (2.2 accepted
  project-lock difference, D2). 2.7 research corrected. Delta sent (msg a00f6233).
- FOUND WORK sent to #15977 (msg 9fe3aea8), decision pending: graph_payloads.rs and
  graph/report/loading.rs admit every retained Falkor version (live: graph file
  daemon.rs 142 nodes, 37 dup names). D11 makes it #23834's; its Targets omit both and
  its research is false. Options (a) extend #23834 [recommended], (b) 3.2 here, (c)
  separate task. If (b): write 3.2 after #23834, bumping, then move 1.10 after it.
- FOR P8 (maintenance): content GC joins code_indexed_project_states on the HUB machine
  for root_path, so a project only a node has indexed yields no GC candidates and its
  unselected versions are retained forever. Plan node-project GC in P8.
- NEXT: P4 (node-local embedding: D12 node gdaemon /api/embeddings native with hub
  config + node endpoint, 503 embedding_identity_mismatch; D13 heartbeat job
  `gcode vector sync-node` reading pending versions from the hub and applying vectors;
  P4 depends only on P1 and P3 (D14)). Bumping leaves continue the chain after 3.1,
  then 1.10 moves after the last.

## Session 8 (2026-10-17 eve): P4 researched + RULED, NOT YET WRITTEN
Messages: #15977 20:04 admission drain (no cargo/pytest; edits/commits OK); #15977
graph admission option (a) done in #23834 (P3 needs no change; update P3 framing
sentence "reported to #15977" -> "#15977 extended #23834's Targets (option a)").
#16993 msg 5a3bfe56: P2 repairs + P3 accepted, no blockers; N9 nit: extend 3.1.2 so
fetch_symbols_for_project under the foreign machine returns B's symbols not A's.
Sent #15977 msg 253bdb9c (E4/E5). RULED 20:06: E4(a) node bootstrap.yaml
`embeddings: {api_base, api_key}` block; model/dim/query_prefix from hub served
config; no block -> relay to hub; leaf moves gcore bootstrap tests out and bumps
gcode+gdaemon+ghook. E5(a) roots route, CONDITION: x-gobby-machine-id set by front
door from verified node credential, never client-supplied; returns only that
machine's rows; Security Reviewer scope at plan review (#15977 handles). 503 retry
cost note accepted.
Facts:
- gcode embeds ONLY via daemon: embedding.rs:216 `embedding_source_from_resolved_ai_context`
  -> AiRouting::Daemon -> gcore `embed_via_daemon` (operations.rs:354) POST
  /api/embeddings {input,is_query,project_id,provider?,model?}; parse_daemon_embeddings
  expects {embeddings, model, dim}. Bearer = read_api_key() (= node key on node).
- Node door: front_door/mod.rs:99 FrontDoor::handle Door::Node arm special-cases POST
  /api/hooks/execute and handshake challenge; else relay.forward. Hook ingress
  (nodes/hooks/ingress.rs:91) relays when `!has_node_key` (:109, bearer vs
  local_token::read_api_key_at(home), constant-time). Embeddings: same pattern.
- NodeServices (state.rs:25) {bootstrap_path, relay: HubRelay, hook_ledger}; prepare
  :32 reads bootstrap.hub_daemon_url/hub_cert.
- Python route embeddings.py:77 generate_embedding_batch: EmbeddingService.from_config,
  returns {embeddings, model: payload.model or config model, dim: config.dim}; errors
  400. Python prefix rule ai/embeddings.py:166 `_apply_prefix`: is_query and
  query_prefix -> f"{query_prefix}{text}"; elif "nomic" in model.lower(): is_query ->
  "search_query: ", else "search_document: "; else text. `_strip_local_embedding_prefix`
  (:65), api_key or "unused" (:99), dim validated (:362). Rust Direct path
  (embedding.rs:79) prefixes differently ("{prefix} {text}") and is unused -> node
  must port Python's rule exactly (new fn, parity test).
- gcore ai/embeddings.rs:42 embed_batch(client, &EmbeddingConfig, inputs) POSTs
  {model,input}, returns vectors only (no model). EmbeddingConfig (config/types.rs:23)
  {api_base, model, api_key, query_prefix, timeout_seconds}. gdaemon gcore features
  today postgres,postgres-pool,route-family; needs `ai` (reqwest blocking ->
  spawn_blocking); reqwest already in Cargo.lock.
- /api/config/effective (configuration_effective.py:82) auth: verified front-door
  identity (node key ok); serves machine-exported keys only: ai.embeddings.model,
  dim (default 768), query_prefix, routing, timeout_seconds. NOT api_base
  (machineExport false), NOT api_key (secret). gcore effective_config.rs
  fetch_config_body (:279, ureq) / fetch_daemon_served_config_at(:153).
- gcore/bootstrap.rs is 976 lines with inline tests (:414-); HubDatabaseBootstrap
  (:44) {mode, hub_daemon_url, hub_cert, database_url, api_key, daemon_url, bind_host,
  daemon_port, websocket_port, front_door}; parse_hub_database_bootstrap :215.
  Python bootstrap.py ignores unknown top-level keys.
- gcore ai retry: MAX_RETRIES 2, BASE_BACKOFF 250ms; 5xx retryable; Retry-After only 429.
- Heartbeat: serve.rs:456 serve_doors builds HeartbeatHost locally, no jobs ("Strangler
  ports register here"); node branch serve.rs:194-221 (NodeServices::prepare,
  channel::run_client, serve_doors). HeartbeatHost::register(name, interval, cb)
  (heartbeat.rs:60) frozen after start; supervise (:126) runs jobs sequentially,
  awaits each (no overlap); BASE_CADENCE 5s. tests crates/gdaemon/tests/heartbeat.rs,
  node_hooks.rs, nodes.rs, front_door.rs (valid_key_forwards_identity_headers :444,
  forged() :1433; auth.rs IDENTITY_HEADERS :156 stripped :116).
- Pending selection exists only in Python: _storage/files.py:170 get_pending_sync_files
  (cifs join on machine, vectors_synced IS FALSE AND (attempted IS NULL OR < now-300s),
  ORDER BY indexed_at LIMIT). SYNC_FAILURE_COOLOFF_SECONDS=300. Rust needs new query in
  db/queries.rs (706 lines) -> P8 reuses.
- Vector path: projection/sync.rs:646 VectorProjectionState::sync_file (attempt ->
  fetch_symbols_for_file -> lifecycle.sync_file_symbols -> mark_vectors_synced CAS);
  lifecycle.rs:321 points_for_symbols embeds batches of VECTOR_SYNC_BATCH_SIZE
  (= gcore qdrant DEFAULT_UPSERT_BATCH_SIZE), checks len == schema.size, builds
  UpsertRequest{id: symbol.id, vector, payload_map(CodeSymbolVectorPayload::from_symbol)};
  expected_schema (:170) uses settings.vector_dim (= ai.embeddings.dim via
  config/services.rs:427) else PROBES by embedding (hub must not probe on apply).
  vector_text_for_symbol embedding.rs:401. VectorSyncAttempt{content_hash,
  attempted_at: SystemTime} queries.rs:18.
- code_indexed_project_states(machine_id, project_id, root_path, ...) PK
  (machine_id, project_id) baseline.sql:2197 -> roots route source.
- gcode CLI: cli.rs:523 VectorCommand {SyncFile, Clear, Rebuild, CleanupOrphans};
  dispatch.rs:492-524; contract.rs vector entries :495-531, contract_version 12;
  copies crates/gcode/contract/gcode.contract.json + tests/contracts/gcode.contract.json;
  crates/gcode/tests/contract.rs; cli/tests/projection.rs parse tests;
  docs/reference-audit/code-index.json (vector entries :624-680, reference graphs.md);
  tests/skills/test_reference_library.py::test_reference_contract_3_2_1 requires every
  native command audited; graphs.md:41 lists vector commands. #23579/#23580 concurrent
  on contract.rs/dispatch.rs/commands/vector.rs/both JSON copies (version -> 13).
P4 DESIGN (advisor-reviewed):
- 4.1 Node-local /api/embeddings [code] (depends 3.1): gdaemon nodes/embeddings.rs
  (new); FrontDoor::handle Door::Node POST /api/embeddings branch; with node key and
  an `embeddings` block -> local; else relay. gcore bootstrap: HubDatabaseBootstrap
  gains `embeddings: Option<NodeEmbeddingEndpoint{api_base, api_key}>` (redacted in
  fmt), tests move to bootstrap/tests.rs. Hub config fetched from hub_daemon_url
  /api/config/effective with node key (cache 30s like gcore share TTL). Request: model
  override other than hub model -> mismatch. Prefix port of _apply_prefix +
  strip local prefix. embed_batch on spawn_blocking with EmbeddingConfig{api_base,
  model (stripped), api_key or None, timeout from served timeout_seconds or 10s}.
  Every vector len must == served dim else 503 embedding_identity_mismatch; endpoint
  non-2xx/transport -> 503 embedding_unavailable (body detail). Response {embeddings,
  model: hub model, dim}. State 3-attempt retry cost. Bumps gcode, gdaemon, ghook.
  Tests crates/gdaemon/tests/node_embeddings.rs (stub hub config + stub endpoint).
- 4.2 Node vector sync routes + `gcode vector sync-node` [code] (depends 4.1):
  group `vector`: node/vector.rs wire, node/hub/vector.rs, node/client/vector.rs,
  gcodeindex/src/node/vector.rs, tests node/vector_tests.rs +
  gcodeindex/tests/node_vector.rs. db/queries.rs new `pending_vector_files(conn,
  project, machine, limit, cooloff)` (P8 reuses). pending: per file
  mark_vector_sync_attempted(machine) -> {path, content_hash, attempted_at,
  symbols:[{id,text}]}, cap files per call. apply (chunked per file, VECTOR_SYNC_BATCH_SIZE
  symbols/request, `complete` + total on last): model/dim vs hub embedding model and
  vector_dim (no probe; vector_dim unset -> existing collection size else refuse);
  ids must be symbols of machine's version else stale (409 vector_version_moved);
  split points_for_symbols -> points_from_vectors; mark_vectors_synced(machine) CAS on
  complete when total == symbol count; CAS false reported. No project lock (CAS fences).
  CLI VectorCommand::SyncNode; Direct access -> refuse (node-only). Contract entry +
  error codes; both JSON copies; audit entry; graphs.md mention. Bumps gcode+gdaemon.
- 4.3 Node heartbeat vector sync [code] (depends 4.2): roots route POST
  /api/code-index/node/vector/roots in ROUTE_PATHS, key-only (no grant; requires
  x-gobby-key-id + x-gobby-machine-id set by front door auth, stripped from clients);
  hub query code_indexed_project_states for machine with pending vectors -> [{project_id,
  root_path}]. gdaemon nodes/vector_sync.rs job: interval 60s (multiple of BASE_CADENCE);
  calls roots through HubRelay with node key; skips roots missing on disk; runs
  installed ~/.gobby/bin/gcode `vector sync-node --project <root> --quiet` one at a
  time with timeout; absent gcode -> log once, skip. serve_doors gains heartbeat param
  (hub passes empty host). Mismatch costs one failed batch per project per cooloff.
  Bumps gcode+gdaemon.
- Then: 1.10 depends on 4.3; Constraints stage gate/release bullets -> 4.3;
  Concurrent editors add #23579/#23580 on contract files for 4.2 (already listed);
  Overview "carries P1 to P4", replace "Until P4 ... relays to the hub" with
  after-state; D12 text add E4 ruling, D4 add E5 refinement; N9 into 3.1.2; P3
  framing sentence update; E1 gates for P4; V1 entry. Validate, ocr, commit
  "[gobby-#21586] docs: draft code-index-family P4 (node-local embedding)", send
  #16993 SHA+delta.

## Session 27 (2026-10-18): STAMPED. M1 committed 1803c39160 (rendered hash 239291133b...); sent #15977
- Consensus 0eab6b1691 (#16993 EVENT=CONSENSUS). B12/N20/N21 fixed at 6fa16bca27.
- ROADMAP all done: 72780d3, 2c8ab68 (S2.4 split), c1b8a73 (S2.6/S2.10 on S2.4a), fd7f4fd
  (tasks MCP server into S2.12; S2.4b <- S2.4a, S2.5; no cycle).
- Waiting on Josh approval via #15977. No plan edits (would invalidate M1).

## Session 26 (2026-10-18): P9 COMMITTED af7983fd2f; migration 470 at 798132b; ROADMAP 72780d3
- Sent #16993 the P9 delta (review from 798132b). #15977: migration 470, no reservations,
  next free at landing (stated in 9.5 Research context).
- ROADMAP edits 2 (line 290 per-family S2.13 edges) and 3 (S4.8 on #21585 only) committed
  72780d3. Edit 1 "split S2.4 around S2.5" is undefined anywhere (graph: #21562 still
  blocked by all of #21560; #21560 has one child #21561). Asked #15977 ONCE; await answer.
- Next: await #16993 review, consensus, V1 consensus entry, M1 by #16993, commit rendered.

## Session 25 (2026-10-18 after DAEMON BACK 02:58): P9 edits IN TREE (uncommitted)
- #15977 02:4x: ROADMAP edits ARE mine (S2.4 split around S2.5; line 290 F5 per-family
  edges; F4 note #21576 waits only on #21585). Do after P9 draft commit.
- DONE in plan: 8.1/8.3 ghook bump fix (gcore asset/src in ghook identity; writer-found,
  tell #16993), Constraints gcore bullet + chain bullet + stage gate, Overview P9 para,
  D10 "8.1 deletes", D15/D16/D18, 1.10 depends 1.6, 9.6, #24449, P5 framing.
- FINAL NUMBERING (9.3 split): 9.1 flip; 9.2 corpus (dep #24475); 9.3 delete pruner
  (prune.py, maintenance.py, _storage/prune_dirty.py, projection_cleanup.py, storage
  mixins, models ProjectionCleanupStore/ProjectionCleanupPending/CodeIndexPruneDirtyProject,
  __init__ export, global_prune+GlobalPruneRequest, grant_auth _OPERATOR_EFFECTFUL_ROUTES+
  admission_required, wiring runner/orchestration/app_context ServiceContainer/servers
  init_servers, tests test_prune, test_code_index_prune_route, test_code_index_maintenance,
  test_orphan_cleanup_logging, storage tests :1008 :1027 :1058; ledger prune key; docs);
  9.4 delete path/clear/rebuild/invalidate routes + _scoped_project_id +
  InvalidateIndexRequest + context methods + InvalidateStoreOutcome + grant_auth 3 rows +
  auth_service :102-104 + tests (routes :182 :233 :239 :253 :276 :324 :333 :342 :353;
  context :45 :62 :76 :158; auth_service :912-913; break_glass :20 -> POST
  /api/voice/transcribe; wiki_retirement :54 -> /api/code-index/graph/search) + ledger 4
  keys removed + 5 UI notes rewritten + reference-audit 5 entries repointed to
  crates/gcodeindex/src/family/{path,lifecycle,invalidate,prune}.rs verification
  rust-capability (helper checks only .rs existence) + impact.md; 9.5 migration 472 (drop
  2 tables; carriers per runtime-family 4.1 :1754; 5 goldens + runtime_handshake.json
  re-record (schema_identity unmasked); managed_postgres_privileges.json (gcode src);
  test_gcode_privilege_manifest :71; test_postgres_agent_authorization :272 :475;
  runner_tests.rs :68 :201; catalog_manifest_freshness.rs
  baseline_supports_machine_owned_attachments_and_prune_rows; bumps gcode gdaemon ghook);
  9.6 D16 issuance (service.py _falkor_capability/_qdrant_capability operations=(),
  constants gone, capabilities_from_snapshot extends dropped; brokered_datastores.json +
  remote_interactive_brokered.json re-sign; capabilities.rs, capability_vectors.json,
  test_capability_vectors.py bare; gcore accepts empty Vec; dep 9.5, #24449; gdaemon);
  9.7 remnants (dep 1.10, #24151, #21582; test_runner_code_index_shutdown deleted by 8.3,
  NOT targeted); 9.8 deferred.

## Session 24: 51f3fdff8a COMMITTED (B11 D, N18, N19); #16993 CLOSED all three (P1-P8 no open findings); P9 designed, NOT written
- #15977 AGREED P9 scope (orchestration remnants only; storage/context/gateway/bm25/models/
  maintenance_launch stay). Rewrite Overview :76-80, D15 :296-301, D18 :325, changelog.
  #15977 msg also listed Josh 02:06 ROADMAP doc edits (S2.4 split; ROADMAP line 290 F5
  per-family edges; F4 #21576 waits only on #21585). UNCLEAR if assigned to me: ask ONCE.
- SRT denial reported: `ps` -> "operation not permitted". Report "none" further unless new.
- P9 LEAF DESIGN (chain; Python-only leaves bump nothing):
  9.1 `code_index` defaults to native [code] (depends: 8.6): crates/gcodeindex/src/family.rs
    (bare) default_backend Native; crates/gdaemon/tests/front_door.rs::* 5.1.2 test
    code_index_defaults_to_proxy_and_honors_native -> ..._native_and_honors_proxy; gdaemon bump
    (crates/gdaemon/Cargo.toml, Cargo.lock, version_pins.py::* scope-reason form). Landing
    constraint like runtime 6.1 (:6548): compare before the activating cutover. Template:
    gdaemon-runtime-family.md 6.1 :6528, 6.2 :6597.
  9.2 Corpus [test] (depends: 9.1, #24475 "Corpus: runtime family cases replayed natively",
    open; it adds e2e_pre_daemon_setup recording routes + Rust harness DB family on lazy pool).
    manifest families add code_index {parity: native, origin: python}; cases (credential
    operator = bootstrap API key -> claimed): code_index_search_disabled GET
    /api/code-index/graph/search?project_id=00000000-0000-0000-0000-000000000e2e&q=fixture ->
    503 {"detail":"Code indexer not available"}; code_index_overview_disabled GET
    /api/code-index/graph?project_id=...e2e -> 503 {"detail":"Code graph not available"}
    (e2e sets code_index.enabled False, conftest :609/:1136; Python code_indexer None when
    disabled, services.py:751). Only UI-read routes (they survive in Python). Record with
    routes code_index: proxy in e2e_pre_daemon_setup. http_contracts.rs parity_violations
    (:434, test :523) list adds CodeIndexFamily on lazy pool. README note.
  9.3 Python pruner + non-UI routes [code] (depends: 9.2): DELETE whole: prune.py,
    maintenance.py (only remnants left after 8.3; consumers prune.py:19,
    tests/code_index/test_orphan_cleanup_logging.py:11, test_code_index_maintenance.py),
    _storage/prune_dirty.py, _storage/projection_cleanup.py (+ storage.py mixins);
    routes/code_index.py: graph_path :256, clear_graph :351, rebuild_graph :377, global_prune
    :403, invalidate_index :441, _scoped_project_id :51, InvalidateIndexRequest,
    GlobalPruneRequest; context.py graph_path/clear_graph/rebuild_graph/invalidate (+
    InvalidateStoreOutcome if unused - CHECK purge/services); grant_auth.py _GRANT_ROUTES 3
    rows (:129-167); auth_service.py _AGENT_CAPABILITY_MATRIX :102-104; wiring runner.py:37,
    :211, orchestration.py:703-722, app_context.py:133, runner_init/servers.py:193.
    Tests: test_prune.py, test_code_index_prune_route.py (delete), test_code_index_routes.py
    (non-UI tests :182 :233 :239 :253 :276 :324-353), test_auth_service.py :378 :912-913,
    test_break_glass_admission.py :20 (use another grant route), test_wiki_retirement_contract
    .py :54 (assert a surviving route), test_code_index_storage.py :1020, test_code_index_
    context.py. Parity ledger: remove the 5 keys from docs/contracts/parity-ledger-status.d/
    git-workspaces.json, regenerate docs/contracts/parity-ledger.md (`uv run python
    scripts/generate_parity_ledger.py`; statuses only proxied|delegated|dropped by decision
    #N; keys must name registered surfaces, tests/test_parity_ledger.py). Docs:
    http-endpoints.md, code-index.md, skills .../code-index/impact.md, reference-audit json.
    After 9.3 `routes.code_index: proxy` serves only UI reads (state it). UI-read rows stay
    proxied (cookie callers). CHECK callers of storage methods record_projection_cleanup_
    failure/clear_/list_projection_cleanup_pending, mark_prune_dirty/record_prune_failure/
    clear_prune_dirty/list_prune_dirty_projects outside prune/maintenance.
    GATEWAY NOT TOUCHED in P9 deliverables: gcode_gateway.py 969 lines; delete-lines proof
    impossible (1.1 also targets it and would have to depend on the proof leaf); split only
    for lint is unjustified. Dead-after-P8/9.3 gateway methods (graph_sync_file,
    vector_sync_file, maintenance_index, vector_rebuild, prune, checked_version, graph_clear,
    graph_rebuild, symbol_path, vector_clear, prune_project_for_maintenance) + floor
    MIN_GCODE_PRUNE_BUDGET_VERSION & GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION + AGENTS.md
    floor wording go to the DEFERRED section (which retires the gateway's route-only parts).
  9.4 Migration dropping code_index_prune_dirty_projects + code_index_projection_cleanup_
    pending [code] (depends: 9.3): gcore asset -> bumps gcode, gdaemon, ghook (+gcore crate
    ver; gclient changed identity; see commit 39eb1255af for file set: migrations/470_*.sql,
    catalog.manifest.json, gcore/src/schema/assets.rs, gcore/tests/schema_contract.rs,
    schema_expected_identity.json, runtime_grants golden/*.json re-sign (test_golden_vectors
    .py header), crates/gcode/security/managed_postgres_privileges.json,
    tests/code_index/test_gcode_privilege_manifest.py, tests/storage/test_postgres_agent_
    authorization.py, crates/gcore/src/schema/runner_tests.rs, crates/gcore/tests/catalog_
    manifest_freshness.rs, re-record tests/contracts/http/runtime_handshake.json).
  9.5 Grants stop listing projection ops (D16 issuance side) [code] (depends: 9.4, #24449):
    service.py FALKOR_OPERATIONS/QDRANT_OPERATIONS removed -> BrokeredCapability(operations=())
    (:36-42, :139-158, broker_operations :195-198); goldens brokered_datastores.json +
    remote_interactive_brokered.json (1.6) re-sign; #24449's Rust capabilities.rs ports
    falkor/qdrant ops (runtime-family :1538) so edit crates/gruntime/src/capabilities.rs +
    tests/fixtures/capability_vectors.json + tests/runtime_grants/test_capability_vectors.py
    (bare paths, created by #24449); gdaemon bump. 1.10 then depends on 9.5 (was 8.6) so
    "1.10's Rust issuer never adds them" holds. Update Constraints release bullets (:88-92,
    :151-154) and 1.10 heading.
  9.6 Remnants after #24151/#21582 [code] (depends: 1.10, #24151, #21582): sync_breaker.py
    (consumers after trigger gone: context.py daemon_config_breaker param/property;
    _app_lifecycle.py:84 wiring removed by #24151; tests test_sync_worker_breaker.py,
    tests/agents/test_isolation_project_json.py:16, test_runner_code_index_shutdown.py),
    maintenance_log.py (+test_maintenance_log.py), eligibility.py (+test_eligibility.py,
    tests/worktrees/test_creation.py:9 code_index_id_for_root), config keys
    code_index.nightly_repair_timeout_seconds + maintenance_log_file (src/gobby/config/
    code_index.py, crates/gcore/assets/config/runtime_config_contract.json -> bumps gcode,
    gdaemon, ghook; web/src/components/settings/sections/RuntimeInfrastructureSection.tsx +
    __tests__; docs configuration.md, code-index.md; tests/config/test_app_config.py,
    tests/runner_helpers.py; re-record corpus config_schema.json, config_values.json).
    sync_worker_breaker_* keys STAY (native 8.1 reads them). maintenance_launch STAYS.
  9.7 kind: deferred, no category, (depends: 9.3): Python UI read routes (graph :106,
    graph/file, neighbors, blast-radius, search), context route methods, gateway dead methods
    + floors + AGENTS.md floor line, UI parity rows. deferral: task_ref placeholder (non-#N),
    reason (cookie verifier task #15977 files; no task exists, search_tasks 02:3x), owner
    #15977, original_acceptance_items.
  E1: add gate 8 (live hub on native default: web UI graph view loads via cookie->Python;
  API-key reads native; gcode prune native). Deferred format: contract plan-coverage.md
  :354-420. P9 heading `## P9: Native default and Python retirement` kind: framing.

## Session 23: #16993 review of c49f9b8e42 handled IN WORKING TREE (uncommitted); P9 research
- #16993 msg 79741d35: B11 blocking, N18, N19 nits; 8.7/8.8 otherwise verified; N16, N17 closed.
- N19 DONE: 8.7 step 4 names every IndexRequest field (project_root ctx.project_root.clone(),
  require_cpp_semantics false). N18 DONE: 8.8 run step 1 drop guard owned by work thread;
  step 3 "Code index nightly repair failed: worker ended without a result".
- B11 (floor MIN_GCODE_PRUNE_BUDGET_VERSION = "1.9.39" literal, gcode_gateway.py:30; tests
  test_managed_gcode_pin_matches_prune_budget_floor :41, test_prune_budget_version_matches_
  gcode_crate_version :50). #16993's per-leaf fix FAILS validation: production-size-growth
  fires on any Target of gcode_gateway.py (969 lines >= 850) without a split in that leaf.
  Advisor: shape D. DONE in tree: 1.1 moves MIN_GCODE_GRAPH_VERSION, MIN_GCODE_PRUNE_BUDGET_
  VERSION, GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION to NEW src/gobby/code_index/gcode_versions.py
  (gateway imports back), prune floor = MANAGED_BIN_VERSION_PINS["gcode"]; 1.1 Targets
  gateway ::* + new bare path; split paragraph after Release ownership; 1.1.5 item (test
  test_prune_budget_version_matches_gcode_crate_version); Constraints bullet rewritten; all
  32 gateway consumers-unchanged lines removed; other leaves have NO gateway target/item.
  Only reader of the floor: GcodeGateway.prune_project_for_maintenance (prune.py:367).
  Fallback C if #16993 insists: per-leaf Target on gcode_versions.py + item.
- VALIDATE blocked: index stale for Cargo.lock, version_pins.py, crates/gdaemon/Cargo.toml
  (another session's index run holds the file locks). Monitor bspkazqrd retries
  `gcode index --files <3> --skip-if-locked` every 20 s. Then: bare validate, code-review
  skill, `ocr delegate rule --format json .gobby/plans/code-index-family.md` alone, commit
  "[gobby-#21586] docs: repair code-index-family B11 floor, N18, N19", send #16993 SHA
  (say per-leaf shape failed the size lint; D chosen; C fallback), tell #15977 briefly.
- P9 FACTS: no cookie-verifier task exists (search_tasks) -> kind: deferred section, non-#N
  placeholder task_ref, no category, (depends: ...). Heading `(depends:)` accepts only
  sections/phases/#N (semantic_lint._lint_dependency_resolution).
  #21586 description: "Preserve shared platform dependencies with independent consumers".
  Outside importers that keep src/gobby/code_index alive (NOT deletable in P9):
  CodeIndexStorage (cli/plans.py, cli/tasks/expand.py, hooks _session_start/agents.py,
  mcp_proxy plans/__init__, tasks _close_evaluation_support, _expansion_registry,
  plans/review_evidence.py, tasks/expansion/_compile.py); models.CODE_INDEX_UUID_NAMESPACE
  (plans/symbol_targets.py, storage/project_checkouts.py); bm25_health (cli/postgres.py,
  cli/installers/postgres.py, runner_startup_code_index.py); CodeIndexContext (agents_context,
  agents_registry, spawn -> tasks/expansion/_plan_gate.validate_plan_for_agent_spawn,
  services.py, servers/http.py:353); GcodeGateway (runner_init/project_purge.py:58,
  services.py); maintenance_launch (projects/purge.py _maintenance_env).
  => Rewrite Overview/D15/D18 "deletion of src/gobby/code_index/" to: P9 deletes the
  family's orchestration remnants; modules with independent consumers stay. Flag to #15977.
  Python routes (servers/routes/code_index.py): web UI (web/src/hooks/useCodeGraph.ts) calls
  only graph overview, file, neighbors, blast-radius, search. path/clear/rebuild/prune/
  invalidate have no cookie client; agent-bearer clear/rebuild/invalidate go with D16.
  Module imports: trigger.py <- eligibility, gcode_gateway, maintenance_launch, sync_breaker,
  context; nightly_repair.py <- eligibility, maintenance_launch, maintenance_log, context;
  prune.py <- eligibility, gateway, maintenance._reconcile_stale_selector, launch, log, context.
  runner.py:36-37 imports nightly/prune; orchestration.py:707 prune, :726 nightly.
  9.1 flip: crates/gcodeindex/src/family.rs default_backend Proxy->Native; corpus
  tests/contracts/http/manifest.json (families map parity proxy|native, origin) + case files;
  crates/gdaemon/tests/http_contracts.rs FAMILIES/parity_violations :531; pattern =
  .gobby/plans/gdaemon-runtime-family.md 6.1 (:6528) and 6.2 (:6597). #24537 owns the
  runtime_handshake 468/469 corpus failure (don't fix).
  P9 SKETCH: 9.1 native default + corpus (bumps gdaemon; before 1.10?); 9.2 D16 Brokered ops
  removal (service.py, grant_auth.py, golden; 1.10 must depend on it if shared golden);
  9.3 delete prune.py + maintenance remnants + _storage/prune_dirty + projection_cleanup +
  non-UI Python routes (path/clear/rebuild/prune/invalidate) + floor + migration dropping
  both tables (depends 9.1, 9.2); 9.4 (depends 9.3, #24151, #21582) sync_breaker,
  maintenance_log, config keys nightly_repair_timeout_seconds + maintenance_log_file
  (CHECK: contract JSON is a gcore asset -> bump closure?), eligibility if unused, runner
  wiring; 9.5 deferred (placeholder cookie-verifier): UI read routes + gateway/context
  route-only parts.

## Session 22: 8.7 + 8.8 COMMITTED c49f9b8e42 (validate clean); delta sent #16993, #15977
- Done: 22:29 correction (D18 added; no E8/E9 existed in plan), P8 framing/ordering,
  chain 8.1-8.4 -> 8.7 -> 8.8 -> 8.5 (depends 8.8, #23835) -> 8.6; 1.10 stays on 8.6.
  N17; Overview after-P8 + P9 waits on #24151/#21582 + maintenance_launch via purge.py;
  D15 bullet; 5.1 freshness bullet; framing key activation (8.3 keys restart_required;
  enabled/nightly_repair_concurrency live); maintenance_log_file stays until P9/#21582;
  every-job stderr difference; E1 gate 7; V1 P8 entry.
- 8.7: EditTrigger (HubJobs::edit_trigger, tick_at/finish_at, per-root batch, backoff
  1-30 s, suspension) + gcode job_trigger::index_files. 8.8: nightly_repair::run ->
  String; gcode job_repair::repair_row; commands/repair.rs::* pub(crate) repair (wait()).
- NEXT: await #16993 review of c49f9b8e42; draft P9 (native default for code_index
  family; delete src/gobby/code_index/ except what waits on #24151/#21582; remove
  nightly_repair_timeout_seconds after #21582 (operator decision), maintenance_log_file,
  dead tables code_index_prune_dirty_projects + code_index_projection_cleanup_pending,
  D16 Brokered ops, cookie-verifier placeholder edge (D15)); then consensus + V1 entry.

## Session 21b: #16993 + #15977 ACCEPTED 8.6 (01:30); CORRECTIONS OWED before 8.7
- #16993: B10 CLOSED, no blocking findings. N17 [non-blocking]: add to 8.5's 4th
  Deliberate-difference bullet: an `unauthorized` from a revoked node key or rejected
  claims while the parent stays live removes the workspace, and its overlay rows then
  skip as skipped_overlay indefinitely (no mechanism change).
- #15977 01:30: 8.6 accepted; corner handling stands; proceed 8.7, 8.8, bookkeeping, P9.
- WRONG IN 6b9c677de8 (fix in 8.7 commit, tell both seats): #15977's 22:29 ruling verbatim
  "(2) 8.6 and 8.7: option (b). Ship and test the native entry points
  gobby_codeindex::jobs::trigger and jobs::nightly_repair, and drop the edges to #24151
  and #21582. I'll add each epic's obligation to its description: call the entry point,
  and delete trigger.py or nightly_repair.py when it replaces the Python hook path or
  cron executor. State in the plan that P9's removal of src/gobby/code_index/ waits on
  those two epics, and that maintenance_launch.py outlives P8 because purge.py imports
  it. Write E8(a) and E9(a) to match." => 8.7 trigger and 8.8 nightly repair take NO
  external edges and delete no Python. Fix P8 framing list ("8.7: the edit trigger's
  native entry point, which #24151 calls"; same for 8.8/#21582), the "Each leaf deletes
  the Python job it replaces" sentence (except 8.7/8.8), Ordering ("two leaves gated on
  other plans", 8.5 #23835 and 8.6 #24372; 8.7, 8.8 follow; 1.10 depends on 8.8).
- 8.7 research (src/gobby/code_index/trigger.py CodeIndexTrigger): notify_file_changed
  (thread-safe) -> _schedule_file: root_key = Path(root).resolve(strict=False); path
  normalized relative to root (abs if outside); ignore "." and directories; pending set
  per root + project id; suspension (after checkout_required/checkout_mismatch) holds
  while overlay id (overlay_project_id_for_root), both _CHECKOUT_MARKERS
  (.gobby/isolation.json, .gobby/project.json) and the file's stat version
  (dev, ino, size, mtime_ns, ctime_ns) are unchanged; else clear suspension + backoff.
  _start_batch: files = pending - active files (parallel batches per root on disjoint
  files). _flush: daemon_config_breaker (subprocess-only: drop, P8 framing), runs
  `gcode index --project <root> --files=<f>... --quiet --skip-if-locked --format json`
  under open_launch_async(project_id, overlay) with 30 s timeout (constant). success:
  clear backoff+suspension, requeue stdout `busy_files` (filtered to requested) with
  backoff; exit 3 (lock busy): requeue busy files or all; exit 2 typed error
  checkout_required|checkout_mismatch -> suspend; else requeue with backoff (base 1 s,
  doubling, max 30 s; delay per root). _batch_done: exceptions requeue; edits during the
  run schedule the next batch. Only caller hooks/event_handlers/_tool.py::_notify_code_index.
- 8.7 design sketch: crates/gcodeindex/src/jobs/trigger.rs `EditTrigger` (Mutex state)
  with pub notify_file_changed(file, project_id, root); 5 s HubJobs tick starts due roots
  on std::thread via new gcode `node/hub/job_trigger.rs::index_files(database_url,
  hub_machine, root, files)` (in-process files-scope index with --skip-if-locked
  semantics, returns busy files / checkout refusal code). Deliberate differences: <=5 s
  debounce, no kill timeout, no breaker. Find the gcode `index --files` entry point first.
- 8.8: commands/repair.rs::run (`gcode repair --project root --format json`, lock wait
  1800 s) -> native try policy; run_locked private; reads read_local_machine_id.
  nightly_repair.py::register_code_index_nightly_repair_cron "0 2 * * *" + tz.
  Native entry `jobs::nightly_repair` handler fn only (#21582 schedules it).

## Session 21: 8.6 COMMITTED 6b9c677de8 (validate clean); delta to #16993, info to #15977
- 8.6 as Session 20c, plus: unauthorized stderr JSON continues removal; no child without
  .gobby/project.json, path gone, or gcode absent (log once); no deadline (client 10 s
  timeout bounds it). Rejected key-only gdaemon route (widens E5(a) to a write).
- 8.5 AMENDED (advisor): missing-registry overlay whose root ends worktrees|clones/<uuid>/
  <run_id> and whose parent uuid is soft-deleted/missing -> leftover branch. 8.5.2 extended.
- Renumbered: 8.7 edit trigger (#24151), 8.8 nightly repair (#21582); 1.10 -> 8.6;
  Constraints last-bumping-leaf now 8.6 (bookkeeping item done); #24372 edge + entry.
- NEXT: 8.7, 8.8, remaining bookkeeping (Decision Record E8(a)/E9(a), Overview, 5.1
  trigger line, restart-required keys sentence, P8 E1 gate, V1 B6-B10/N10-N16), P9.

## Session 20c: #15977 RULED (a) 01:03; #16993 B10 gates consensus (same gap)
- #15977 01:03: "(a), node-side release. Write it into the plan, not as a filed task. Add
  an 8.5 item where the node runs gcode invalidate for an overlay or checkout when it
  removes the checkout or agent worktree. Own acceptance criteria: the removal path calls
  it, and the hub rows for that checkout are gone afterward. Name the owning node lifecycle
  path, so expansion produces the leaf in the hub-node lane. Drop the Deliberate-difference
  wording for this gap. No age rule."
- #16993 on c4aafade3a: no design defects; accepts ripples; B10 [blocking at consensus]:
  8.5's 4th Deliberate-difference bullet is an unchosen either/or -> record as a leaf
  (ruling (a) does this). Also: plan/apply race vs by-age GC covered by 1.4 reparse.
- PLAN: NEW deliverable 8.6 "Node workspace removal releases its code-index rows"
  (depends: 8.5, #24372 Worktree and clone preparation, repair and removal = node-halves
  2.3, which creates crates/gdaemon/src/nodes/agents/workspace.rs; under #24148, hub-node
  lane). RENUMBER: trigger 8.6->8.7, nightly repair 8.7->8.8; fix P8 framing list,
  Ordering paragraph ("8.1 to 8.8", "four leaves gated"?), Operator decision "(8.7)" at
  ~line 3724 -> 8.8, 1.10 -> depends 8.6 now (N16 lands on 8.8; tell #16993).
  Replace 8.5's 4th Deliberate-difference bullet with "8.6 closes it".
- 8.6 pieces (P2 route-leaf layout, plan lines 1334-1366):
  - node/invalidate.rs (wire: InvalidateRequest{scope}), node/hub/invalidate.rs (hub entry:
    lock_project_by_id(maintenance_try) on the scope's project = overlay id when the scope
    has one else project id; indexer::invalidate(conn, scope.machine_id, that id); busy ->
    409 index_busy), node/client/invalidate.rs (confirmation prompt as local unless
    --force, then POST), gcodeindex node/invalidate.rs handler + lib.rs router + node.rs,
    ROUTE_PATHS gains /api/code-index/node/invalidate; dispatch.rs::run sends invalidate's
    Node access to the client instead of 1.7's refusal (P2 framing line 1310 lists
    invalidate as refused -> edit that sentence).
  - Local invalidate: commands/status/invalidate.rs invalidate_project(ctx) locks by id,
    invalidate_project_locked -> indexer::invalidate (7.2 gives machine param).
  - gdaemon workspace.rs::remove(record): before `git worktree remove`/remove_dir_all, run
    `<home>/bin/gcode --project <path> invalidate --force`; on failure keep record + path,
    retry_removals retries each report interval (node-halves 2.3 contract: record deleted
    only when path gone). OPEN: a project the hub no longer admits (soft-deleted/purged:
    handshake 403 claims_mismatch, gcore grant/mod.rs:175 -> Unauthorized) would block
    removal forever -> rule: Unauthorized from the grant lets removal proceed, because
    8.2 reconcile_deleted / 8.5 leftover step release those rows. Decide + state.
  - After invalidate the overlay has no selector and no registry row -> 8.2 orphan sweep
    clears projections and purges; a registered project's node checkout rows go and its
    versions age out via 8.4/8.5.
  - Main checkouts registered by node-halves 2.9 have no node removal path; state that.
  - Tests: gcode node/invalidate_tests.rs (hub entry deletes only principal machine rows,
    overlay id used; busy -> index_busy), gcodeindex tests/node_invalidate.rs (route),
    gdaemon tests/node_agents.rs (removal calls gcode invalidate before path removal with a
    stub gcode; failure keeps record; Unauthorized proceeds), E2E? hub rows gone.
  - Targets also: gdaemon Cargo.toml, gcode Cargo.toml, Cargo.lock, version_pins; docs
    guides code-index.md. workspace.rs is bare (new file).

## Session 20b: 8.5 COMMITTED c4aafade3a (validate clean), delta sent to #16993
- As designed below, except per advisor: discover_content_gc gains check_git_history: bool
  (no new symbol in content_gc.rs); ripples done (1.10 -> 8.5, P8 ordering "three leaves",
  "8.1 to 8.4", Constraints concurrent editors P8 + #23835 paths + 8.5's edge).
- Dead node rows sent to #15977 as decision (a) node-side invalidate [recommended] /
  (b) hub age rule = 8.5b / (c) leave documented. Await disposition; ask once.
- NEXT: 8.6 edit trigger (depends 8.5, #24151), then 8.7 nightly repair (depends 8.6,
  #21582; cron_job_names.py for its row), 1.10 -> 8.7, then bookkeeping.

## Session 20 PROGRESS (8.4 COMMITTED b0b5eee998, #16993 ACCEPTED, no findings)
- Sent #16993 + #15977 the SHA, delta, derived decision. Restart hold arrived 00:53 CDT
  while researching 8.5; nothing uncommitted in the plan.
- 8.5 DESIGN (researched, NOT written):
  - Why depends #23835: it makes pins a content-GC root in content_gc.rs's reachability
    predicate; node overlays pin parent versions, so by-age GC must share that SELECT.
  - content_gc.rs (symbol form, 8.4 used ::prune_content_versions): target
    ::discover_content_gc; its candidate SELECT moves to a private helper; NEW
    discover_content_gc_by_age(database_url, retention, project_id) = same SELECT without
    the local-machine state JOIN and without the git-history check (hub has no checkout).
    Consumers of discover_content_gc: prune.rs (target ::*) and content_gc/tests.rs (1.1
    edits it with ::*, so it must be an edit Target, ::*, adds the by-age test).
  - prune.rs: NEW pub(crate) prune_content_by_age(ctx, retention, deadline): discovery with
    no stale records + by-age candidates -> apply_project_prune(print false, |_| ctx).
    status.rs re-exports (bare path, already 8.4's).
  - job_prune.rs prune_all: after hub rows, node-only projects (sync_rows grouped by
    project, no hub-machine row), sync_rows order: pending (any machine, limit 1) ->
    completed; registry_state: live -> by-age GC (HubScope hub machine, empty root,
    projection_cleanup, deadline now+90 s); deleted -> reconcile_deleted(its machines);
    missing -> overlay iff some row's root_path hashes as
    Uuid::new_v5(&crate::models::CODE_INDEX_UUID_NAMESPACE, root_path) == id -> skipped
    "skipped_overlay" (hub overlays skip too, Python parity); else purged leftover ->
    reconcile_deleted(its machines); 8.2's orphan sweep then purges it.
  - Why the hash tells them apart: node writes need an interactive grant, admitted only for
    a live registry row (runner_init/servers.py `_project_admitted`: deleted_at IS NULL);
    overlay writes carry code_overlay_project_id = code_index_id_for_root(canonical root)
    (gcore project.rs:135); node roots are canonical (config/context.rs
    detect_project_root_from). Registered ids are uuid4 (storage/projects.py:359,430).
    String hash, not code_index_id_for_root: canonicalize would read the hub's disk.
  - Purge leftovers arise when reconcile_deleted never succeeded before purge (Falkor down,
    code_index disabled); purge.py `_invalidate_code` removes only the hub machine's rows.
  - Content GC never reads ctx.project_root (prune_content_versions_with: lock lease per
    candidate, content_is_unreferenced recheck, store-unconfigured retain).
  - Open concern to raise with #16993 (not 8.5 scope?): dead node overlays (node deleted
    the worktree) keep their state rows forever; hub cannot see node disks; worktree code
    has no gcode invalidate call. Decide: note it or file via #15977.
  - Ripples: 1.10 -> depends 8.5; P8 framing ordering "8.1 to 8.5 wait on nothing outside"
    is wrong (8.5 depends #23835) -> "8.1 to 8.4"; Constraints concurrent editors: add
    #23835's content_gc.rs (8.4 edits it; 8.5 takes the edge), and the bullet's "P1, P3,
    P4 or P7" gains P8; 8.2's line 4125-4128 "8.5 owns those rows" stays true.
  - Tests (job_prune/tests.rs): by-age GC deletes a node-only live project's version past
    retention with no hub checkout, keeps one a node selector names and one inside
    retention; overlay-hash row skipped; purged leftover reconciles (no Falkor in CI ->
    failed '<id>:failed', selectors kept; lock held -> skipped_locked); content_gc/tests.rs:
    discover_content_gc_by_age returns a version the local machine does not select.
    Version bump gcode + gdaemon.

## Session 19 PROGRESS (8.4 DRAFTED, validate CLEAN, NOT committed)
- 8.4 written after 8.3 (nine items). 1.10 heading -> (depends: 1.6, 8.4, #24449).
- Ripples done: 5.1.1 unlisted path -> `/api/code-index/unlisted`; 8.2/8.3 "until 8.4" ->
  "until P9" (scope-reasons, deletion bullet, tests bullet, 8.2 table sentence, 8.2 overlap
  sentence); removed consumers-unchanged prune.py lines from 8.2/8.3 (validator: conflicts
  with 8.4's edit Target), and 8.4's runner.py / prune/tests.rs / content_gc/tests.rs lines.
- Design changes vs Session 18 notes (tell #16993):
  - content GC: `prune_content_versions` gains a resolver param (its grant-based per-candidate
    resolver can't run in gdaemon); hub passes `|_| Ok(ctx.clone())`. prune.rs gets private
    `apply_project_prune(&discovery, deadline, resolve, print)` + pub(crate)
    `prune_project_unattended(ctx, retention, deadline)`, re-exported from commands/status.rs.
  - snapshot reuses `job_sync::sync_rows` (hub rows) — no new indexed_projects query.
  - `job_maintenance::reconcile_stale` factored out of maintain_project.
  - cron row: REMOVED_AUTOMATION_JOB_NAMES (scheduler deletes row+runs, refuses run/enable),
    not RETIRED_SYSTEM_CRON_JOBS. storage/cron.py is 867 lines -> split: constants +
    is_removed_automation_job move to new src/gobby/storage/cron_job_names.py; scheduler.py and
    routes/cron.py import from it. orchestration.py only drops the register call.
  - test_prune.py 274-315 deleted whole (duplicates operator tests).
  - route: std::thread + oneshot, Wait entry; 422 bodies recorded; 500 "Code index prune failed".
- NEXT: load code-review skill, `ocr delegate rule --format json .gobby/plans/code-index-family.md`
  (own call), commit `[gobby-#21586] docs: draft code-index-family 8.4 hourly prune and prune
  route`, send #16993 SHA + delta (above list + ripples), flag derived decision (Python's
  operator half stays until P9; 8.4 retires only the hourly cron) to #16993 AND #15977.

## Session 18 PROGRESS
- 8.3 COMMITTED 91891786dd (validate clean, 11 items); delta sent to #16993.
- 1.10 heading -> (depends: 1.6, 8.3, #24449): interim; move forward with each P8 draft
  (shared gdaemon/Cargo.lock/version_pins), lands on 8.7 (N16).
- test_maintenance_config_capture.py TRIMMED, not deleted (keeps the schema-sweep test).
- Extra 8.3 facts: empty candidates -> generate_via_daemon_with_max_tokens(profile);
  fresh AiContext per call (AiLimiter default 1); profile w/ candidates only fills
  reasoning (auto everywhere). test_code_index_storage 1532 + 1628 tests and
  test_gcode_storage_conformance.py:269 also touch summaries (covered).
- _repair_code_index_bm25 keeps bool (decided; answer if #16993 probes).
- #16993 ACCEPTED 8.3 (no findings); accepts 1.10 interim; N16 + 8.5 carry-forward tracked.
- 8.4 DESIGN (researched, NOT written; derive-from-rulings, flag to #16993 + #15977):
  - KEY: P5 keeps code_index family on Proxy until P9 (1.9 invariant), D15 says P9 deletes
    the pruner -> Python's POST /prune keeps CodeIndexPruner through P8. 8.4 retires ONLY
    the hourly cron half: prune.py loses prune_all_projects, _clear_dirty_projects,
    create_code_index_prune_handler, register_code_index_prune_cron, CronRegistrationProtocol,
    CodeIndexPruneResult, CODE_INDEX_PRUNE_JOB_NAME/HANDLER/INTERVAL/DESCRIPTION (names only
    they use). orchestration.py (811 lines): RETIRED_SYSTEM_CRON_JOBS += "gobby:code-index-prune"
    (literal), registration block keeps `runner.code_index_pruner = CodeIndexPruner(...)`,
    drops register call. tests/code_index/test_prune.py drops tests calling
    prune_all_projects (201-239, 242-271, 274-315 uses both -> rewrite to operator only?,
    318-337) and register tests (610-720). Route + its test file unchanged.
  - RIPPLE EDITS: 8.2 "Until 8.4, Python's hourly prune and native pass both reconcile" ->
    "Until P9, Python's operator prune (POST /prune while the family proxies)"; 8.2 "After 8.4
    deletes prune.py the table has no writer, and P9 drops it" -> "P9 deletes prune.py, the
    table's last writer, and drops the table"; 8.2 Targets/Python changes + 8.3 Targets
    scope-reason + 8.3 Python deletion bullet + 8.3 test_code_index_maintenance bullet:
    "until 8.4" -> "until P9". Also 8.2 consumers-unchanged prune.py/projection_cleanup.py
    lines say "until 8.4" -> "until P9".
  - gcode: commands/status/prune.rs::* (275 lines): split prune_project_scoped into
    pub(crate) discover_project_scoped_records + new pub(crate) apply_project_prune(&discovery,
    deadline) -> totals (no printing; CLI prints + bails). GlobalPruneDiscovery pub(crate).
    status/shared.rs + content_gc.rs read local machine = hub in gdaemon (2.1 left them).
  - node/hub/job_prune.rs(+tests): static GLOBAL_PRUNE Mutex; prune_all(database_url,
    hub_machine, retention_days, entry Wait|Skip) -> Option<GlobalPruneOutcome{completed,
    failed[{project_id,reason="<id>:failed"}], skipped[{project_id,reason}]}>, serde = Python
    JSON. Snapshot = new db::hub_maintenance::indexed_projects(conn, machine) (project_id,
    root_path) ORDER BY last_indexed_at DESC (Python list_indexed_projects). Per project in
    order: empty root -> skipped "missing_root"; pending (pending_graph_files or
    pending_vector_files limit 1, hub machine, cooloff) -> completed (deferred_pending_sync);
    registry_state + eligibility: overlay -> skipped "skipped_overlay"; soft-deleted ->
    reconcile_deleted(machines from deleted_selected_projects) Reconciled->completed,
    Busy->skipped "skipped_locked", Failed->failed; unregistered/missing_root/
    identity_mismatch -> job_maintenance::reconcile_stale (8.2's reconcile step made
    pub(crate)) Reconciled->completed, Busy->skipped_locked, Failed->failed; active ->
    HubScope::context(projection_cleanup) + discover + apply_project_prune deadline 90 s
    (CODE_INDEX_PRUNE_TIME_BUDGET_SECONDS const) Ok&no failures->completed(pruned) else failed.
  - gcodeindex: jobs/prune.rs job `code_index_prune` interval const 3600, Skip entry,
    retention = code_index.content_retention_days (live, default 1; JobConfig gains it), stderr
    line with counts or "skipped: global_locked". family/prune.rs + family.rs row + tests/
    family_prune.rs: POST /api/code-index/prune; enabled false -> 503 {"detail":"Code index
    pruner not available"}; body optional, {force: bool, retention_days: int>=1|null};
    bad JSON/types/<1 -> Python 422 bodies recorded via TestClient; work on std::thread +
    oneshot (NOT spawn_blocking: long prune would hold shutdown); Wait entry; DB failure ->
    500 {"detail":"Code index prune failed"} logged; 200 outcome compact JSON.
  - Deliberate diffs: force ignored (Python force = 5 s wait on in-process per-project
    asyncio lock; native prunes serialize under the global lock, PG lock tried as gcode prune
    does); no 504 (no subprocess timeout); no 400 GcodeInputValidationError; no
    pruned+missing-root hub delete (eligibility checked root; vanished root -> missing_root
    next maintenance pass); no prune-dirty bookkeeping (no reader); lax pydantic coercion of
    string bools/ints -> 422 natively; disabling the cron row no longer stops the hourly
    prune (code_index.enabled does); stderr instead of maintenance_log_file; soft-deleted
    rows reconciled for every machine.
  - Acceptance ideas: hub_maintenance indexed_projects order; job_prune outcomes per branch
    (fake? use real DB: pending defers, overlay skipped, soft-deleted, busy lock skipped,
    active prunes content past retention); global lock Wait vs Skip; route gates/bodies;
    job plan interval; config default content_retention_days; Python: orchestration retires
    the row (tests/test_runner_init.py or test_prune); version bump.

## Session 17 PROGRESS
- COMMITTED a5bde993ec (P8 framing, 8.1, 8.2, P4-P7 fixes) and fb5e01d65a (#16993's B8, B9,
  N13-N15 on 8.1/8.2/4.3). #15977 accepted the timeout correction (maintenance_log_file
  treated the same: P9 removal waits on #21582).
- B9 design: SyncRow.registry_deleted (LEFT JOIN projects); plan_row skips; 4.3 roots skip;
  8.2 step 2 `reconcile_deleted` (all machines, DeletedOutcome, no ProjectOutcome).
  OWED: P8 E1 gate must check both reconciles' success path (no Falkor in CI, #21045).
- N14: deliberate difference; 8.1 targets config/code_index.py::* + contract json ::*;
  8.1.13 contract test. N16: 1.10 -> 8.7 at the end.
- 8.3 facts gathered this session: label JSON is provider-structured in Python
  (generate_json with json_schema); native parses text locally. /api/llm/generate records
  caller `llm-generate-route`. Candidates-only body omits profile
  (operations.rs generate_text_via_daemon). Contract default candidates non-empty list.
  Python summaries read source from disk at the hub root (`_read_symbol_source`,
  1-indexed lines, missing file -> skipped, not stamped).
- #16993 VERIFIED fb5e01d65a closed (B8, B9, N13-N15); no new 8.1/8.2 findings. CARRY-FORWARD
  8.5: cover node rows with no registry row after purge, and state how the hub tells a
  post-purge node row from a node overlay (or why it need not); 8.1 plans graph sync for
  them until 8.5 lands (acceptable only because 8.5 is in the chain).
- 8.3 DESIGN (decided, write it):
  - gcode: `db/hub_summaries.rs`(+tests) and `db/hub_labels.rs`(+tests) port
    `_storage/summaries.py` / `_storage/communities.py` SQL with explicit hub machine;
    `node/hub/job_summaries.rs`(+tests) and `node/hub/job_labels.rs`(+tests), declared in
    node/hub.rs. Generator injected as `&(dyn Fn(&str) -> anyhow::Result<String> + Sync)`;
    production closure = `generate_via_daemon_with_candidates(&AiContext::resolve(Some(pid),
    &mut LocalAiConfigSource::empty()), prompt, None, Some(max_tokens), &candidates,
    GenerationBudget::Interactive)`. Up to max_concurrency calls via std::thread::scope.
    regex + serde_json already gcode deps. Count/cut in chars (Python str).
  - Summaries: read root/file_path lines line_start..=line_end (from_utf8_lossy, str::lines);
    missing file or empty -> skipped, NOT stamped; generation fail or sanitized None ->
    stamped attempted; success -> update (guarded by content_hash). Prompt verbatim
    `_SUMMARY_PROMPT` + truncation marker; `_MAX_SOURCE_CHARS` 2000; sanitizers verbatim
    (summary_safety.py lines 8-18: source[:max] ` -> ', drop NUL; summary cut at ```, ` -> ',
    drop NUL, collapse ws, [:500].rstrip, empty -> None).
  - Labels: prompt verbatim `_LABEL_PROMPT` (asks JSON), data block per `_label_prompt`
    (representatives then sorted others, [:40], sanitize data max 8000); parse text locally:
    strip fences, JSON object, name str 3-40 chars, rationale str <=120 else
    schema_rejected; transport/other error -> generation_failed; name in deterministic or
    candidates -> deterministic source; else community_label_safety port (regexes lines
    8-24) -> model or sanitation_rejected; non-written outcomes stamped; write losing the
    signature race skipped. label_model = profile config value; label_confidence NULL.
  - Deliberate diffs: no provider json_schema (route has none); caller recorded as
    `llm-generate-route`; Interactive budget (30 s candidate, 60 s CLI, route caps total);
    transport failure = failed generation (stamped), as Python's in-process failure.
  - Keys symbol_summary.* / community_label.* are restart_required: active_values keeps the
    start value (runtime_activation.py:123-133 activates only live keys) -> native sees
    changes after Python restarts, same as today. JobConfig gains them (8.1.8 test covers).
  - gcodeindex jobs/maintenance.rs: after Indexed/IndexBusy/IndexFailed, when enabled,
    summarize then label (Python order, test asserts order). jobs/config.rs.
  - Python deletes: summarizer.py, community_labeler.py, summary_safety.py,
    community_label_safety.py, _storage/summaries.py, _storage/communities.py (storage.py
    drops both mixins), models.py::StoredCommunity (287), maintenance.py loop +
    _run_maintenance + summary/label helpers (keep _registry_state,
    _reconcile_stale_selector, _sweep_orphaned_index_projects for prune.py until 8.4),
    runner_startup_code_index.py `_start_code_index_tasks` (keep `_repair_code_index_bm25`,
    bool return kept: tests/config/test_runtime_loop_consumers.py:152, test_bm25_startup.py
    42/70 assert it; reword its line 70 log: no worker named), runner.py lines 160-163
    (annotation-only attrs, 679 lines, `::*`?), runner_lifecycle_subsystems.py :31 import
    drops _start_code_index_tasks, :816 `code_index_bm25_ready =` -> bare await, :827-832 go;
    SPLIT: move `_check_embedding_service` (99-137) + `_initialize_vector_store` (168-218) to
    NEW `src/gobby/runner_startup_vectors.py` (logger name "gobby.runner_lifecycle"),
    re-imported by subsystems so patch.object sites keep working (tests 1079/1081/1280/1282,
    subsystems tests 35/38/234/353/363, test_bm25_startup 91/94, test_runner_backlog 57/60).
  - Shutdown proof: `src/gobby/runner_lifecycle_shutdown.py::*` — operation: delete-lines —
    base-blob: 0909c6a910d75e89971a4591ead22e00713c371d — lines: 242-259, 261, 817-819 —
    scope-reason: plain text, NO backticks. (HEAD blob verified this session.)
  - Tests: delete whole tests/code_index/test_summarizer.py, test_community_labeler.py,
    test_maintenance_config_capture.py, tests/test_runner_code_index_shutdown.py;
    test_code_index_storage.py drops summary/community tests (1303-1440, 1487-1620) and
    imports StoredCommunity/SUMMARY_MAX_CHARS, and its two upsert tests (1455, 1477) set the
    summary with SQL; test_code_index_maintenance.py drops _run_maintenance/summary tests
    (keeps reconcile+sweep until 8.4); test_bm25_startup.py deletes
    test_init_subsystems_skips_code_index_workers_after_failed_repair (vacuous);
    test_runner_lifecycle.py 1089/1294 and test_runner_lifecycle_subsystems.py 397/468 drop
    `_start_code_index_tasks`; tests/terminals/test_host_manager.py:2016 drops the
    `_stop_code_index_workers` patch.
  - Docs: docs/guides/code-index.md (lines 68-94, 299-333 maintenance/summary text),
    docs/guides/llm-features.md row 172 + lines 176-178 (and add community_label row),
    docs/contracts/truncation.md:54 names `summary_safety`.
  - Owed in bookkeeping: P8 E1 gate covers both reconciles' success path + summaries.

## Session 16 PROGRESS (after DAEMON BACK 22:51; still uncommitted)
- Validate fixed and clean: 8.1 targets `src/gobby/runner_startup_code_index.py::*` (no exact
  symbol, so no consumer-coverage), dropped runner.py + runner_lifecycle_shutdown.py Targets
  and the two test consumers-unchanged lines (8.3 targets those tests); new 8.1.11 test
  `test_start_code_index_tasks_starts_only_the_maintenance_loop`; 8.2 runner_startup line
  removed; 1.10 depends on 8.2 (move to 8.7 at the end).
- FACT: `maintenance_index_timeout_seconds` is also the BM25 repair timeout
  (runner_startup_code_index.py:39 `_repair_code_index_bm25`, cli/postgres.py:73
  `repair_code_index_cmd`, recovery.md:42) -> framing now: key STAYS; operator decision is
  only `nightly_repair_timeout_seconds` (last reader nightly_repair.py, so waits on #21582).
  `maintenance_log_file` also read by nightly_repair.py -> P9 removal waits on #21582.
  TELL #15977 (correction to the 22:29 ruling's premise).
- BM25 gate: framing deliberate difference written (no gate; XX000-XX002 failure logged,
  retried; Python's trigger/CLI never gated). Python BM25 failure log line edited in 8.1.
- 8.3 split needed: runner_lifecycle_subsystems.py is 883 lines; edits :31 (import),
  :816 (`code_index_bm25_ready =` goes, F841), :827-831 -> move `_check_embedding_service`
  (99-137) + `_initialize_vector_store` (168-218) to NEW `src/gobby/runner_startup_vectors.py`,
  re-imported by subsystems so patch.object sites keep working (tests: test_runner_lifecycle.py
  1079/1081/1280/1282, test_runner_lifecycle_subsystems.py 35/38/234/353/363,
  test_bm25_startup.py 91/94, test_runner_backlog.py 57/60). Tests patching
  `_start_code_index_tasks`: test_runner_lifecycle.py 1089/1294, test_runner_lifecycle_subsystems.py
  397/468.
- 8.3 shutdown delete-lines proof: function 242-257 + its trailing blanks, call 261, comment +
  call 817-819; tests/terminals/test_host_manager.py:2016 patches `_stop_code_index_workers`
  (Target it). Recompute blob with `git hash-object --no-filters` right before commit.
  tests/test_runner_code_index_shutdown.py deleted whole in 8.3.
- Python summarizes + labels EVERY active project whatever the reindex did (busy, failed,
  gateway missing) -> 8.2 ProjectOutcome split: Overlay, Reconciled, ReconcileFailed,
  ReconcileBusy, Indexed, IndexBusy, IndexFailed; 8.3 runs after Indexed/IndexBusy/IndexFailed.
- 8.3 SQL (src/gobby/code_index/_storage/summaries.py, communities.py): unsummarized = JOIN
  code_indexed_file_states fs ON project, file_path, fs.content_hash = s.file_content_hash,
  machine = hub, summary IS NULL, attempted NULL or < now-300 s, kind IN (function, class,
  method), ORDER BY s.updated_at DESC LIMIT n; update summary SET summary, attempted NULL
  WHERE id AND content_hash (symbol hash); mark attempted WHERE id, content_hash, summary IS
  NULL. Communities: machine, project, labeled_signature IS DISTINCT FROM member_signature,
  attempted cooloff 300 s, ORDER BY member_count DESC LIMIT n; update label (label,
  label_source, label_confidence NULL, label_model = profile, labeled_signature, labeled_at
  NOW(), attempted NULL) WHERE machine, project, community_id, member_signature; mark
  attempted guarded by signature + not yet labeled. All Python-only (no gcode writer).
- Prompts verbatim: summarizer.py `_SUMMARY_PROMPT`, `_MAX_SOURCE_CHARS` 2000 + truncation
  marker; community_labeler.py `_LABEL_PROMPT`, `_LABEL_SCHEMA` (name 3-40, rationale <=120,
  required), `_MAX_PROMPT_MEMBERS` 40, `_MAX_PROMPT_DATA_CHARS` 8000, representatives first
  then sorted others. Outcomes: schema_rejected, sanitation_rejected, generation_failed,
  written_deterministic (name in deterministic or candidates -> source deterministic),
  written_model (sanitize_community_label). Non-written outcomes are marked attempted; a
  write losing the signature race is skipped (re-queued).
- Sanitizers: summary_safety.py (source: [:max] replace ` with ', drop NUL; summary: cut at
  ```, replace `, drop NUL, collapse whitespace, [:500], rstrip, empty -> None);
  community_label_safety.py (strip fences/quotes, 2-5 words, <=40 chars, charset
  [A-Za-z0-9 /&+.-], no source extension, not a member path).
- AI: Python always passes candidates (config fills them from the profile default) ->
  native `generate_via_daemon_with_candidates(&AiContext, prompt, None, Some(max_tokens),
  &candidates, GenerationBudget::Interactive)`; `FeatureCandidate` deserializes the config
  value; AiContext::resolve(Some(project), &mut LocalAiConfigSource::empty()) as
  `audited_query_embedding` does. gcode has NO generation caller yet; feature `ai` default.
  Keys `symbol_summary.*` / `community_label.*` (enabled, batch_size 20/10, candidates,
  max_concurrency 2, max_tokens 100, profile feature_low) are restart_required.
- Storage mixins (summaries.py, communities.py) lose every caller in 8.3 -> delete with
  their tests in tests/code_index/test_code_index_storage.py; summary_safety.py goes too
  unless another consumer (only _storage/summaries.py + tests). models.py StoredCommunity.
- #24318 1.2 proof drift (from #15977 23:10): ANSWERED exempt (fully executed #23846,
  acad860266; _validate.py::_completed_plan_sections line 286); no edit; sent #16993.

## Session 15 PROGRESS (uncommitted; validate FAILS on items below)
- DONE: B7 finish (7.2 family_invalidate sentence + lock research line), B6 research
  line in 4.3, 8.2 written (maintenance reconcile+reindex; gcode db/hub_maintenance.rs +
  node/hub/job_maintenance.rs; no pending-cleanup retries, no project-not-found purge;
  Python maintenance.py edited `::*`, cleanup.py deleted).
- #15977 RULED 22:29: choice 1 = keep sync_worker_projection_timeout_seconds; P9 removes
  nightly_repair_timeout_seconds + maintenance_index_timeout_seconds (Josh at approval).
  8.6/8.7 = option (b): NO edges to #24151/#21582; ship+test native entry points
  `gobby_codeindex::jobs::trigger` (debounced per-root flush on HeartbeatHost) and
  `jobs::nightly_repair` handler fn; #24151/#21582 call them and delete trigger.py /
  nightly_repair.py (#15977 adds obligations to their descriptions). Plan must state P9's
  removal of src/gobby/code_index/ waits on #24151 and #21582, and maintenance_launch.py
  outlives P8 (projects/purge.py imports it). Rewrite E8(a)/E9(a) Decision Record to match.
- VALIDATE ERRORS TO FIX: (1) 8.1 production-size-growth on
  src/gobby/runner_lifecycle_shutdown.py (916 lines): plan = 8.1 drops runner.py and
  runner_lifecycle_shutdown.py Targets; `_start_code_index_tasks` keeps
  `runner._sync_worker_task = None` and stops creating the task (drain tolerates None);
  rewrite 8.1.11 (test in tests/test_runner_code_index_shutdown.py: only the maintenance
  loop starts); 8.3 later deletes `_stop_code_index_workers` (lines 242-257, calls 261,
  819) with a delete-lines proof (base-blob 0909c6a910d75e89971a4591ead22e00713c371d at
  HEAD 96252bb7ac; recompute) plus runner.py attrs 160-163. (2) 8.2 consumers-unchanged
  line for runner_startup_code_index.py conflicts (8.1 targets it): delete the line.
  (3) 1.10 must depend on the last bumping leaf (now 8.2; finally 8.7).
- Advisor notes for 8.3/8.4: labels need local JSON parse (no json_schema on
  /api/llm/generate or gcore text_request_body; prompt asks JSON; fence-strip; name 3-40,
  rationale <=120; unparseable -> SCHEMA_REJECTED); summary_safety.py stays (imported by
  _storage/summaries.py); eligibility.py/maintenance_log.py last consumers trigger.py and
  nightly_repair.py (stay past P8 under (b)). 8.4: route WAITS on the global lock like
  Python (`async with self._global_lock`); hourly job SKIPS when locked; per-project
  deadline = constant 90 s (CODE_INDEX_PRUNE_TIME_BUDGET_SECONDS) in jobs/prune.rs; route
  work must run on a std::thread + channel (not spawn_blocking). gcode prune.rs privates:
  prune_project_scoped, discover_project_scoped_records, mutate_project_scoped_stale
  (resolve own Context; local machine = hub). Retire cron via
  orchestration.py RETIRED_SYSTEM_CRON_JOBS (add "gobby:code-index-prune") and remove
  init_orchestration's prune registration (lines ~703-723); runner.code_index_pruner
  attr consumers (route uses server.services.code_index_pruner). prune-dirty table has
  no reader (prune_dirty_projects uncalled) -> drop bookkeeping; P9 drops both dead
  tables (code_index_prune_dirty_projects, code_index_projection_cleanup_pending).
  maintenance_log_file key: native logs to stderr; P9 removes the key.
- Scope-form trap: commands/vector.rs is exact-scoped elsewhere (1.1, 3.1) -> never add
  symbols there; 8.2 reuses lifecycle_from_context. maintenance.py `::*` in 8.2/8.3/8.4.
- Nightly repair: `gcode repair --project root --format json` = commands/repair.rs::run
  (lock wait 1800 s) -> native uses try policy; run_locked private; reads
  read_local_machine_id. Trigger: CodeIndexTrigger index timeout is a constant 30 s.

## Session 14 PROGRESS (uncommitted edits in the plan, validate NOT yet run)
- WRITTEN: P8 framing + 8.1 (before `## E1`). 8.1 uses per-file `sync_file_graph`
  (pub(crate), CLI path skips cleanup_orphans) + `sync_vector_files(ctx,&[path],None)`,
  per-batch `lock_project_files(brief_index_flush_try)`, own stall runner
  `job_sync::run_files_bounded`, graph+vector breakers (stall opens at once), std::thread
  kick, gdaemon `crates/gdaemon/src/code_index_jobs.rs::register`. Choice 1 narrowed in
  framing (only maintenance_index_timeout_seconds dropped).
- FIXED #16993 review msg dfb3c8d5 (P4-P7): B6 (4.3 tokio Command kill_on_drop +
  timeout; 4.3.3 extended), B7 (7.2 lock_project_by_id maintenance_try via new
  `node/hub/family_invalidate.rs`; 7.2.3 extended), N10 (4.1 TTL pub + pub mod embeddings,
  fns pub), N11 (4.3 notes node host's only job), N12 (7.1.1 reworded).
  STILL TODO for B7: 7.2 gcode visibility sentence + research context; B6 research line.
- NEXT: 8.2-8.7 (prune/maintenance/content-GC must take project locks, per B7), then
  bookkeeping (1.10 -> 8.7, Constraints 7.2 mentions, Overview, D18+ entries, 5.1
  trigger line, V1 entry incl. B6/B7/N10-N12), validate, ocr, commit, reply #16993 with
  SHA + fixes + P8 delta, #15977 status naming the choice-1 narrowing FIRST.

## Session 14 (2026-10-18, after compaction): P8 drafting facts
- Work runs on `std::thread` (not spawn_blocking): tokio's Runtime Drop "waits forever"
  for spawn_blocking work, and serve::run drops its runtime after block_on; std threads
  die at process exit. Config is read async in the tick and passed to the thread.
- `projection/sync.rs::sync_after_index_bounded(ctx, graph, vectors, stall, progress)`
  (pub; `commands/index.rs:223` uses it) abandons a no-progress worker after `stall`;
  per-file progress events => native sync job keeps `sync_worker_projection_timeout_seconds`
  as its stall timeout. Choice 1 NARROWS to dropping only
  `maintenance_index_timeout_seconds` (tell #15977).
- `sync_after_index(ctx, graph_paths, vector_paths, progress)` is pub; runs
  `cleanup_orphans` after a graph batch with synced files.
- sync_breaker.py has other consumers: context.py (daemon_config_breaker), trigger.py,
  tests/agents/test_isolation_project_json.py, tests/code_index/test_code_index_maintenance.py
  => 8.1 deletes only sync_worker.py; sync_breaker.py goes with the last consumer (P9).
- 8.1 Python sweep: runner_startup_code_index.py::_start_code_index_tasks (sync half),
  runner.py attrs `_sync_worker_task/_sync_worker_shutdown` (:162-163),
  runner_lifecycle_shutdown.py:244/254, tests/code_index/test_sync_worker.py,
  tests/code_index/test_sync_worker_breaker.py (keep TestSyncCircuitBreakerUnit + helpers),
  tests/test_runner_code_index_shutdown.py (imports _sync_pass, sync_worker_loop).
- Python requeue_vector_sync/requeue_graph_sync clear the attempt after transient
  exhaustion (retry next pass); per-file retries 1 s, 2 s. Native: cooloff only.
- `_file_needs_graph_sync` = symbol_count > 0 and language in _GRAPH_SYNC_LANGUAGES.
- Embedding in-process: `embed_via_daemon` presents `read_api_key()` (hub local key) to
  `daemon_url(/api/embeddings)` with no grant (ctx.grant_ai None).
- `ServiceConfigSelection::{all, falkordb_only, vectors, ...}` exist (context.rs:86-147).
- Contract JSON entries carry `"default"` (e.g. sync_worker_batch_size 50) =>
  defaults test can read the JSON. Extra key: `code_index.maintenance_log_file`.
- serve.rs 742 lines; `serve()` (pub) and serve::run call `serve_with_runtime`.

## Session 13 (2026-10-17/18): P8 research (not yet written)
- Packet 8b2bdb0f -> #15977 ruled 21:46: P5-P7 choices (no freshness prelude;
  invalidate deletes communities) ACCEPTED ("freshness comes from the P8 trigger and
  maintenance pass, so a GET never indexes"). E8 (a): trigger = P8 leaf (depends:
  #24151), deletes trigger.py, no interim route. E9 (a): nightly repair = P8 leaf
  (depends: #21582) native scheduled job, deletes nightly_repair.py. P8 choices 2
  (kick + busy flag; 5 s cadence, live interval), 3 (each leaf deletes its Python job),
  4 (node-only content GC by age) ACCEPTED. Choice 1 (drop
  maintenance_index_timeout_seconds + sync_worker_projection_timeout_seconds): keep in
  plan as a NAMED OPERATOR DECISION; #15977 puts it to Josh with the stamped plan.
  #16993 being resumed by #15977.
- Python loops: runner_startup_code_index.py::_start_code_index_tasks starts
  maintenance.py::code_index_maintenance_loop (interval 3600, summarizer + labeler) and
  sync_worker.py::sync_worker_loop (5 s). orchestration.py:703-748 registers Python cron
  handlers: prune.py::register_code_index_prune_cron (hourly interval, system row) and
  nightly_repair.py::register_code_index_nightly_repair_cron ("0 2 * * *" + tz).
- Trigger: only caller is hooks/event_handlers/_tool.py::_notify_code_index (Python hook
  handler; node deliveries skip per hook-envelope-file-facts 2.6). Hooks stay Python
  until #24151 (S2.11 hub half: hook ingress and rule engine in Rust). Cron family is
  #21582 (Cron and scheduler route family, no plan). No Rust cron evaluator; Cargo.lock
  has chrono and jiff only.
- HeartbeatHost::supervise runs due jobs SERIALLY and awaits each; register freezes
  interval; SHUTDOWN_GRACE 5 s aborts the tokio task (spawn_blocking cannot abort).
- Python launches every gcode child under maintenance_launch.py (managed per-project
  grant so writes pass RLS). In-process lib on the hub DSN (D5) needs no grant.
- Prune: gcode prune --global -> daemon.rs::post_code_index_prune POST
  {force, retention_days} (PRUNE_TIMEOUT), parses GlobalPruneOutcome {completed,
  failed[{project_id,reason}], skipped[...]}. Python route 503 "Code index pruner not
  available", 504 "Code index prune timed out", 500 "Code index prune failed".
  prune.py::_run_operator_global_prune_locked loops list_indexed_projects (HUB machine)
  -> prune_project (lock, defer if get_pending_sync_files(project,1,vectors,graph),
  eligibility, `gcode prune --force --project root --retention-days N --max-seconds`);
  gcode lib: commands/status/prune.rs::prune_project_scoped(force, root, quiet,
  retention, deadline) (private) = discover + stale mutate + content GC with deadline.
- Content GC (status/content_gc.rs::discover_content_gc:42) JOINs
  code_indexed_project_states on the LOCAL machine for root_path (git history check:
  recent_content_hashes_in_git_history; unavailable -> retain). Node-only projects:
  never candidates. #23835 (Pin-aware retention) targets content_gc.rs::* -> edge.
- Maintenance pass: _retry_pending_projection_cleanups (table
  code_index_projection_cleanup_pending, Python-only writer/reader),
  _sweep_orphaned_index_projects (projects no machine selects + no registry row; clears
  graph+vector then purge_index_project), per hub project eligibility
  (eligibility.py::resolve_indexed_project: overlay/unregistered/missing_root/
  identity_mismatch/active; Rust gobby_core::project::code_index_id_for_root exists),
  stale -> _reconcile_stale_selector (graph+vector clear, delete_project_index),
  active -> `gcode index --skip-if-locked` (exit 3 busy; project-not-found -> purge),
  then summarize (get_unsummarized_symbols, source read from root, sanitize, update or
  mark attempted) and label communities.
- AI: gcore::ai::daemon::operations::generate_via_daemon_with_candidates posts
  /api/llm/generate (Python) with candidates; summary readers search/fts/symbols.rs,
  graph/report.
- #23576 (Sync worker leaves projection completion to gcode): Python-only, blocked by
  #23575 and #23578; semantics: gcode owns completion CAS, sync_superseded keeps pending,
  no-graph shortcut for non-graph languages (sync_worker.py:50 _GRAPH_SYNC_LANGUAGES: c
  cpp csharp dart elixir go java javascript javascriptreact jsx php python ruby rust tsx
  typescript typescriptreact; files.py::get_pending_sync_files cooloff 300 s
  constants.py SYNC_FAILURE_COOLOFF_SECONDS, ORDER BY indexed_at).
- More facts: gcode projection/sync.rs private `sync_graph_files(ctx, paths, None)` and
  `sync_vector_files(ctx, paths, None) -> ProjectionSyncReport` (mark attempted + CAS
  complete per file, SkippedMissingIndexedFile reconciles); error kinds
  (typed_projection_error: falkordb_unreachable/_query_failed, embedding_http,
  qdrant_http, ...). ServiceConfigSelection::{all, database_only, falkordb_only,
  vectors, projection_cleanup} (config/context.rs). Rust contract parser has no
  defaults (gcore runtime_contract.rs ExactKeySpec has no default) -> gcodeindex typed
  job config with constant defaults + a test comparing them to the contract JSON.
  Python breakers: vector (embedding transport) + gateway (daemon config, subprocess
  only); Falkor errors only requeue (cooloff). serve::run hub branch builds families
  when the pool exists (serve.rs:162-168); serve_with_runtime -> serve_doors (4.3 adds
  the HeartbeatHost param). Hourly Python cron handler = prune_all_projects (global,
  force=True, retention = content_retention_days, global lock); prune_dirty_projects
  has NO caller; code_index_prune_dirty_projects is written only by prune.py.
  code_index_projection_cleanup_pending written/read only by maintenance.py (port it
  as-is; no migration). Context at 250k when written.
DESIGN TO WRITE (P8), each leaf deletes the Python job it replaces (choice 3):
- Framing: jobs in crates/gcodeindex/src/jobs.rs (+ jobs/<job>.rs), gcode reach via pub
  crates/gcode/src/node/hub/job_<job>.rs; hub branch of serve::run registers them on a
  HeartbeatHost passed through serve_with_runtime to serve_doors; each job registered at
  BASE_CADENCE, reads active config per tick (code_index.enabled false -> skip), runs
  when its interval elapsed since last start, hands work to a spawned task behind a
  busy flag (kick) because supervise is serial; work runs spawn_blocking with own
  connection from hub DSN (D5), as the hub machine or the row's machine. Named operator
  decision (choice 1, Josh at approval): P9 drops maintenance_index_timeout_seconds and
  sync_worker_projection_timeout_seconds. Deliberate differences: no gateway breaker,
  no disk checks, subprocess error classes collapse.
- 8.1 seam + sync worker (depends: 7.2, #23576): every code_indexed_project_states row;
  hub machine graph+vectors, other machines graph only (nodes own vectors, D13);
  pending_graph_files new in crates/gcode/src/db/graph_pending.rs (mirror of 4.2's
  pending_vector_files); no-graph shortcut CAS for non-graph languages; vector breaker
  (threshold/backoff/max keys) armed on embedding_http/qdrant_http; up to
  sync_worker_concurrency batches at once; batch = sync_worker_batch_size; deletes
  sync_worker.py, sync_breaker.py, runner_startup wiring + tests.
- 8.2 maintenance pass (depends: 8.1): interval maintenance_interval_seconds; pending
  projection cleanup retries; orphan sweep (graph+vector clear then purge, only when
  graph and embedding enabled); per hub project eligibility port (code_index_id_for_root,
  project.json id, isolation marker) -> stale reconcile (clear graph+vector, delete
  selector; failure records pending) / active reindex in-process with try-lock (busy
  skip) / project-not-found purge; missing_root_purge_observations semantics; deletes
  maintenance.py pieces, cleanup.py, eligibility.py if no other consumer.
- 8.3 summaries + labels (depends: 8.2): get_unsummarized/update/mark attempted,
  sanitize (summary_safety, community_label_safety ports), prompts verbatim, AI via
  gcore generate_via_daemon_with_candidates (profile/candidates from config) posting
  Python /api/llm/generate; deletes summarizer.py, community_labeler.py, safety modules.
- 8.4 prune job + POST /api/code-index/prune (depends: 8.3): hourly global prune
  (in-process prune_project_scoped via node::hub entry, deadline, defer if pending
  sync, eligibility, global lock shared with the route), route body {force,
  retention_days} -> {completed, failed, skipped}, 503/504/500 bodies; retire cron row
  code_index_prune via orchestration.py retired-jobs list; deletes prune.py,
  prune_dirty storage. Node-only project content GC by age (choice 4) in content_gc.rs
  -> (depends: #23835) or separate 8.5.
- 8.5 node-only content GC (depends: 8.4, #23835) if split.
- 8.6 trigger (depends: 8.5, #24151); 8.7 nightly repair (depends: 8.6, #21582).
- 1.10 moves to depend on the last P8 bumping leaf; then P9.

## Session 12 (2026-10-17 night): P5-P7 COMMITTED da8efcb05b (validate clean)
- Written as designed below, plus: freshness prelude skipped (writer's choice, flagged
  to #15977 and #16993); invalidate made machine-explicit (`indexer::invalidate(conn,
  machine_id, project_id) -> u64`) and keeps gcode's community deletion (watermark,
  gcode-import-communities F-02); gruntime gains `pub fn claims_native`; gcode reach is
  only via pub `node::hub::family_<group>.rs` (gcode `commands`, `search`, `config` are
  private); invalidate failures use Python's global-handler bodies
  (exception_handlers.py: 503 {"status":"error","message":"Hub temporarily
  unavailable"}, 500 {"status":"error","message":"Internal server error",
  "error_logged":true}).
- Deltas: #16993 msg d978a99a (session EXPIRED, queued; P4 delta 24ef0ec7 also
  queued); #15977 status 1980e4c2 and expiry notice d8fb746e.
- P9 facts found: parity ledger (docs/contracts/parity-ledger.md, generated by
  scripts/generate_parity_ledger.py from docs/contracts/parity-ledger-status.d/
  git-workspaces.json:138-226) lists all 10 code-index routes `proxied`; exit rule needs
  `delegated` or `dropped by decision #N`; keys for removed Python surfaces must go
  unless dropped. docs/guides/code-index.md:352-355 and http-endpoints.md:771-773 list
  the routes; src/gobby/servers/auth_service.py:102-104 `_AgentRoute` rows for clear,
  rebuild, invalidate.
- NEXT: P8 (heartbeat work + POST /prune + node-project content GC), then P9.

## Session 11 (2026-10-17 late): P5-P7 RESEARCHED + RULED, NOT YET WRITTEN
Rulings (#15977 msg after my packet 5b6906f0):
- E6(a): P6/P7 native routes claim API-key callers only (gruntime `Caller::Operator`,
  `x-gobby-key-id`). Cookie (web UI `gobby_session`, useAuth.ts credentials include),
  agent bearer, break-glass, unauthenticated stay unclaimed -> Python. P9 deletes the
  Python ORCHESTRATION (sync worker, trigger, maintenance, summarizer, pruner); deleting
  the cookie read path (routes/code_index.py handlers + gateway graph-read subset) waits
  on a browser cookie-verifier task #15977 will file under hub-node planning and send me
  the ref; until then write that edge as a NAMED PLACEHOLDER leaf in P9.
- E7(a): no grant-verified native path for Brokered ops clear_projection
  (POST /graph/clear), rebuild_projection (/graph/rebuild), invalidate_projection
  (/invalidate). P9 removes them from src/gobby/runtime_grants/service.py:37-41,
  src/gobby/servers/grant_auth.py `_GRANT_ROUTES` rows (:129-167) and golden
  tests/runtime_grants/golden/brokered_datastores.json; 1.10's Rust issuer never adds
  them (keep 1.10 LAST; when P9 drafted, 1.10 depends on the P9 removal leaf and says so).
  Evidence: gcode graph clear/rebuild run locally (commands/graph/lifecycle.rs::clear/
  rebuild under index_lock::with_exclusive_project_lock); HTTP client
  graph/code_graph/lifecycle.rs::run_lifecycle_action is test-only; gcode invalidate
  ignores daemon_url (index/indexer/lifecycle.rs:34 `_daemon_url`).
- Accepted refinements: POST /api/code-index/prune moves to P8 with the pruner
  (prune.py run_operator_global_prune loops prune_project; gcode prune --global posts it
  with API key via daemon.rs::post_code_index_prune). No lease fence: Python
  grant_auth.py::admission_required/_OPERATOR_EFFECTFUL_ROUTES have NO callers; Rust
  lease/standby.rs says native HTTP admission belongs to the lifecycle cutover (S1.3).
Facts:
- Python routes src/gobby/servers/routes/code_index.py (465 lines), prefix /api/code-index:
  GET /graph (project_id, limit=200) -> gcode graph overview; GET /graph/file/{file_path:path};
  GET /graph/symbol/{symbol_id}/neighbors (limit=50); GET /graph/blast-radius
  (symbol_id|file_path exactly one else 400 "Provide exactly one of symbol_id or
  file_path", depth=3, limit=100); GET /graph/path (symbol_a, symbol_b required,
  max_depth=6 ge=1) -> gcode path; GET /graph/search (q required, limit=25) -> Python
  storage search_symbols_fts then search_symbols_by_name fallback, result dict keys
  id,name,type(=kind),kind,file_path,line_start,signature; POST /graph/clear,
  /graph/rebuild (query project_id, `_scoped_project_id`), /prune, /invalidate (body
  {project_id}; not indexed -> 200 {"status":"ok","project_id","note":"not indexed"};
  else CodeIndexContext.invalidate -> storage.delete_project_index (this machine's
  selector) -> {"status":"ok","project_id","stores":{graph skipped, vector skipped, hub
  ok deleted=counts},"failed_stores":[]}; 503 is JSONResponse {"error":"Code indexer not
  available"}).
- Errors (FastAPI {"detail":...}): 503 "Code graph not available" (no code_indexer or
  graph_enabled false or no gateway; search/clear/rebuild say "Code indexer not
  available" when code_indexer None); 400 "project_id is required"; 404 str(error)
  "Code index project not found: <id>" (no hub-machine state row) or gcode project not
  found; 400 GcodeInputValidationError "Invalid <param>: value must not be an absolute
  path" / "value must not contain '..' segments" (file_path, symbol_id) / "value must not
  start with '-'" (symbol_a/b only); 500 "Code graph request failed"; ValueError -> 400
  str. clear/rebuild: payload success false -> 400 detail=payload error. Default FastAPI
  422 bodies for missing/invalid query (no custom RequestValidationError handler).
- Project root: Python get_project_stats = code_indexed_project_states WHERE machine_id =
  hub machine AND project_id (projects.py:65). Hub machine id in Rust:
  gobby_core::machine::read_machine_id_from_home(home).
- Gates: code_index.enabled / code_index.graph_enabled (defaults true, keys in
  crates/gcore/assets/config/runtime_config_contract.json:1029/1040); gcode reads hub
  config via config/runtime_contract.rs::capture_hub_snapshot (pub(super)) ->
  HubConfigSnapshot::value(key).
- Web UI consumers: web/src/hooks/useCodeGraph.ts (overview, file, neighbors,
  blast-radius, search). Agents doc: skills/gobby/references/code-index/impact.md:28
  (blast-radius, path).
- Family pattern: gruntime lib.rs RuntimeFamily (NATIVE_ROUTES rows {method,path,
  callers,gate}, claims = row match + Caller::of). RouteTable::lookup longest prefix
  wins (routes.rs:130) so /api/code-index family (Proxy default) + /api/code-index/node
  (Native) coexist. http_contracts.rs parity_violations checks only FAMILIES (static);
  do NOT register code_index there until P9 flips. Corpus (tests/contracts/http) has no
  code_index family; record Python cases at the P9 flip like runtime 6.2 (record with
  bootstrap routes {code_index: proxy}; credential operator; Rust harness has no key
  resolver so it replays via proxy stub; Python harness proves native parity).
- Reuse for P6: 2.7 hub entry points (graph report/overview/file/neighbors/blast-radius
  payloads, GraphPayload JSON == what Python relays from `gcode graph ... --format json`);
  2.6 nav path (GraphPathResponse). HubScope{database_url, machine_id, project_id,
  overlay_project_id} + Context::for_machine with ServiceConfigSelection (Falkor). For
  search: gcode search/fts/symbols.rs search_symbols_fts_visible / _by_name_visible.
- P7 reuse: commands/graph/lifecycle.rs clear_project_graph / rebuild_project_graph
  (private fn -> make pub(crate) payload fns wrapped in with_exclusive_project_lock);
  payload = what Python relays. gcode invalidate lib: index::indexer::invalidate(conn,
  project_id, daemon_url).
- Shared-target chain: 4.3 is last on lib.rs/serve.rs/Cargo/version_pins; 5.1 depends
  on 4.3; move 1.10 to depend on the last new leaf each phase.
DESIGN TO WRITE (P5-P7):
- P5 "`/api/code-index` family seam": 5.1 (depends 4.3): CodeIndexFamily in new
  crates/gcodeindex/src/family.rs (name code_index, prefix /api/code-index, Proxy
  default until P9, NATIVE_ROUTES table, claims = API-key caller + row), composed in
  serve::run next to node family (hub only); shared plumbing: hub machine id, project
  root lookup (404 body), code_index.enabled/graph_enabled gate (503 bodies), FastAPI
  {"detail"} envelope + pydantic-v2 422 bodies for missing/invalid query; first route
  GET /graph/search (Postgres only) proves the seam.
- P6 graph reads: 6.1 overview/file/neighbors/blast-radius via 2.7 entry points; 6.2
  path via 2.6. Input validation 400s as above.
- P7 mutations: 7.1 graph clear+rebuild (API-key only, Python statuses incl. 400 on
  success false), 7.2 invalidate.

## Session 10: P4 COMMITTED 120b7a6f24 (validate clean)
- 4.1/4.2/4.3 written per Sessions 8-9; bookkeeping done (1.10 -> 4.3, Constraints,
  Overview, D4/D12/D13, N9 in 3.1.2, P3 framing, E1 gate 5, V1). Delta sent to #16993
  (msg 24ef0ec7; #16993 paused after cutover, queued) and status to #15977 (ccaea237).
- Validator lesson: bare filenames like `heartbeat.rs` or short paths like
  `bootstrap/tests.rs` in a deliverable body must match a Target; use full paths or
  reword.
- NEXT: P5 (family seam), P6 (graph read routes), P7 (mutation routes). Research
  needed from scratch: Python /api/code-index routes (src/gobby/servers/routes/
  code_index*.py), which are graph reads vs mutations, gruntime family pattern.

## Session 9 (2026-10-17, after cutover a7f83905e6): extra P4 facts, still not written
- Reuse for node EmbeddingConfig: gcore config/resolve.rs:318
  `resolve_capability_binding(&mut served, AiCapability::Embed)`, set binding.api_base/api_key
  from bootstrap block, then :255 `resolve_embedding_config_from_binding(&mut served, &binding)`
  (model default, query_prefix, timeout default 10). Same pattern gcode embedding.rs:242.
  DaemonServedConfig implements ConfigSource (config/daemon_source.rs:25).
- Hub config fetch: through node.relay.forward (handles hub_cert TLS); gcore
  effective_config.rs:216 `parse_served_body` is PRIVATE -> make pub. ureq
  fetch_daemon_served_config_at can't pin hub cert.
- has_node_key (ingress.rs:109) private -> pub(crate) for embeddings.
- NodeServices (state.rs:25) {bootstrap_path, relay, hook_ledger}; prepare (:32) reads
  bootstrap.api_key; add `embeddings` (endpoint + 30s served-config cache) there, not a
  static (tests run several nodes in one process).
- Python prefix: `_apply_prefix` uses UNSTRIPPED model; `_needs_nomic_prefix` = "nomic" in
  lower; strip only "local:lm-studio/" and "local:ollama/" for wire model; api_key or
  "unused". Python tests tests/search/test_nomic_prefix.py (cases to mirror). No Rust port
  exists (no search_document/local:lm-studio in crates). Python ignores provider/project_id.
- embed_via_daemon (operations.rs:354) sends binding.model (= served hub model on a node),
  so "model override == hub model" is the normal case.
- vector_dim: hub snapshot = stored config_store rows only (runtime_contract.rs:28), so
  vector_dim is None unless operator set ai.embeddings.dim. lifecycle.rs ensure_collection
  (:65) -> expected_schema (:170) uses vector_dim else probed_vector_size else probe.
  DECISION: apply seeds probed_vector_size with the request dim when vector_dim unset ->
  existing collection must match (DimensionMismatch) or it is created at that dim (node
  checked dim vs hub served ai.embeddings.dim, default 768, same as Python expected_dim).
  Avoids starving node-only projects. Map to 409 embedding_identity_mismatch.
- serve_doors (serve.rs:456) callers: serve.rs:217 (node), :452 serve_with_runtime, and
  tests/nodes.rs:184, :812, :1978 -> param change touches tests/nodes.rs.
- mark_vector_sync_attempted(conn, project, path) -> Option<VectorSyncAttempt> (queries.rs:242);
  mark_vectors_synced(conn, project, path, content_hash, attempted_at) (:270); 3.1 adds machine.
  attempted_at on the wire as Unix microseconds (CAS compares exactly).
- Audit entries docs/reference-audit/code-index.json:624-680 (surface cli, reference
  graphs.md, implementation path+symbol, verification [source-audit, rust-capability]).
  graphs.md:39-44 vector sentence; docs/guides/code-index.md:192 vector table row.
- Plan scope forms: serve.rs `::*`; contract.rs `::contract`; dispatch.rs `::run`;
  queries.rs exact; contract JSON copies and crates/gcode/tests/contract.rs `::*`;
  docs/contracts/gcode-cli.md bare. 1.7 lists cli/tests.rs + cli/tests/top_level.rs as
  consumers-unchanged (4.2 must not target them).
