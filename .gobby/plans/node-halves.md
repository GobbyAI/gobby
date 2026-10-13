Plan artifact: `.gobby/plans/node-halves.md`

# S2.7 and S2.11 node halves: the node's hook ledger and agent duties in gdaemon

**Plan ID:** node-halves

## Overview
`kind: framing`

This plan covers two Stage 2 node halves. P1 is #24150 (S2.11 node half: a
gdaemon node-local envelope ledger that forwards hooks to the hub), and its
leaves expand under #24150. P2 is #24148 (S2.7 node half: node-local agent
spawn and worktrees in gdaemon), and its leaves expand under #24148.

A node is `gdaemon` in node mode (ROADMAP decision 17). It holds no datastore
credential, runs no Python daemon, and today relays every HTTP request to its hub
(`crates/gdaemon/src/front_door/relay.rs::HubRelay`). `ghook` posts each hook to
the local front door, so on a node a hook reaches the hub with no ledger. The
node channel (#24090) connects and pings but ignores every inbound frame. The
hub-node command contract (#24268, `docs/contracts/node-commands.md`) is landed
and binding: this plan implements its node side and changes none of it.

Scope is `RunMode::Node` only. Hub and standalone daemons keep the Python ledger
and the Python spawn path until the hub halves, #24151 and #24149.

## Decision Record
`kind: framing`

Orchestrator rulings (gobby#15977, 2026-10-13), operator-approved planning
(`stage2-plan:now`):

1. **Roots and order.** P1's leaves (1.1 to 1.6) expand under #24150 and P2's
   leaves (2.1 to 2.9) under #24148. Neither epic is a child of #21579, which
   both block, so leaves under #21579 could never become ready. Cross-epic
   order is expressed as leaf dependencies: 2.1 depends on 1.6, and 2.9 on
   1.5. (Amended by gobby#15977 on its 2026-10-13 return.)
2. **Receipt route.** This plan owns the hub route `POST /api/nodes/receipts`,
   key-bound in the style of hub-mode 1.4.
3. **Providers.** Node v1 launches claude, codex and grok. Droid and agy are
   deferred section D1.
4. **Pane detection.** Node pane detection and its reporting route, which
   hub-mode Decision 7 assigns to #24148's planning, are deferred section D2.
5. **Placement.** Node duties live in `crates/gdaemon/src/nodes/`, following the
   node-channel plan's Decision 8. Family crates (ROADMAP decision 16) are for
   route families.
6. **Seats.** No Plan Enhancer. The adversary is Grok seat gobby#16791.
7. **Replay decisions.** This plan owns the hub route
   `POST /api/nodes/hooks/replay`. It shares one decision function with the
   local drain.
8. **SRT runtime.** Node machines keep the Python CLI for `gobby auth login` and
   `gobby install`. `gobby install` places the pinned SRT runtime. gdaemon only
   verifies and runs it.
9. **v1 limits** (accepted; listed under Constraints).

Design decisions (writer, from the code):

10. **The ledger wraps the relay.** The node front door gains one native arm,
    `POST /api/hooks/execute`. It claims the envelope locally, forwards the
    request unchanged through `HubRelay`, and finalizes on 2xx. The hub's Python
    route keeps its own filesystem claim. The two ledgers live on different
    machines and do not conflict: the node's ledger answers ghook's retries,
    and the hub's answers the node's retries. Rejected: a node-side SQLite or
    PostgreSQL store, because decision 9 keeps the spool and its ledger on the
    filesystem and a node has no datastore.
11. **Marker format is unchanged.** Markers stay JSON files under
    `<gobby_home>/hooks/inbox/processed/<envelope_id>` with the Python field
    names and the `.locks/` flock shards. A machine that switches between node
    and standalone mode keeps a valid ledger.
12. **One heartbeat host.** Node timers (spool drain, marker prune, reports,
    removal retry) register on the gdaemon heartbeat host (ROADMAP decision 18).
    `serve_doors` takes the host as an argument so the node branch can register
    jobs before it starts.
13. **Fake hub in tests.** Every node test drives a fake hub (an axum server on
    `127.0.0.1:0` that records requests and returns scripted responses). The
    live hub-to-node proof is deferred section D3, gated on hub-mode D4.
14. **Node agent state is one JSON file.** Accepted runs, preparation and
    workspace records, pending removals, cancel records, exits, the report
    revision, the adopted `hub_epoch` and the host epoch counter live in
    `<gobby_home>/node/agents.json`, written atomically under one mutex. The
    contract bounds it at 1,000 runs and 1,000 reported exits per report.
    Rejected: SQLite, because a node holds no datastore and one file under a
    lock covers the load.
15. **The SRT chokepoint is ported to Rust.** gdaemon renders the policy,
    verifies the pinned install and wraps the provider argv itself. Rejected:
    calling the Python CLI at spawn time, because S4.1b (#21579) closes only
    when a node runs `gdaemon` alone and ruling 8 limits the node's Python CLI
    to `gobby auth login` and `gobby install`. Python stays the oracle through
    a recorded corpus (2.4).
16. **The node terminal host is gterminals' `HostSupervisor`** with a
    node-local `EpochAuthority`. Reports use the native locator shape from
    `crates/gterminals/src/reconcile.rs`, so the hub's `promote_to_live` sees
    the same identity a local native terminal has.
17. **Checkouts register on the first hook from a root.** hub-mode 2.2 refuses
    a node session start that has no pre-created row until #24151 lands, and a
    spawn reaches a node only after its checkout is registered. A trigger on a
    session-start 2xx would therefore never fire for an operator's session.
    The node registers the project root of any forwarded hook's `cwd`,
    whatever the hub answers to the hook.
18. **A run that has not launched does not survive a node daemon restart.** At
    start, every accepted or preparing run becomes a `prepare_failed` exit with
    `launched: false`, and its preparation record becomes a pending removal.
    The contract allows this ("a failure before launch removes what it
    prepared"), and resuming preparation would need the payload stored.
19. **Fixed node-side limits.** The preparation deadline is 300 seconds, from
    acceptance to the host's `spawn_commit`. Cancel records are kept for 24
    hours: the hub's longest pending command is 600 seconds (the internal
    route's `timeout_ms` ceiling), so no spawn for a cancelled run can arrive
    later.

## Constraints
`kind: framing`

- **v1 limits (Q9).** The `agent.spawn` payload carries only
  `sandbox {enabled, network, extra_write_paths}`. Node runs therefore use the
  built-in `agent_sandbox` defaults, widened by the vendored Trusted seed for
  `network: "trusted"`. Hub-configured `agent_sandbox` overrides do not reach
  node runs. The payload also carries no `api_base`, resume metadata, reviewer
  auto-approve flag or pre-commit prewarm flag, so node v1 has no credential
  injection, no resume, no reviewer home and no pre-commit store prewarm.
  Spawned agents always auto-approve (`src/gobby/agents/spawn_models.py`,
  `auto_approve: bool = True`), so the payload needs no flag for it. Each limit
  lifts only with a v2 payload field.
- **Node launch steps that need hub state stay out.** The node launches the
  payload's `prompt` verbatim; prompt composition, including
  `_agent_prompt_prefix`, belongs to whoever builds the payload. The node skips
  the managed code-index preflight (`_prepare_managed_code_index`), resume
  bookkeeping (`_record_resume_launch_details`) and the best-effort isolated
  Python environment seed (`preseed_isolated_python_environment`).
- **`render_srt_settings` may move.** The open gmcp-stdio plan
  (`.gobby/plans/gmcp-stdio.md`) moves `render_srt_settings` from
  `src/gobby/agents/srt_runtime.py` to `src/gobby/agents/srt_settings.py`. This
  plan's oracle is the function, imported from whichever module defines it
  when 2.4 is implemented.
- **Hub STOP epoch.** The hub route's `.stop_replay_epoch` stays one file per
  hub until #24151, so any machine's session start bumps it. The node replays
  its own stored responses under its own epoch, so the hub's epoch matters only
  when the node lost a response.
- **Node session registration.** hub-mode 2.2 refuses a remote session start
  that misses its pre-created row, and #24151 owns registering it. This plan
  does not change hub session binding.
- **Monolith ceiling.** `crates/gterminals/src/host.rs` (916 lines) and
  `src/gobby/servers/routes/mcp/hooks.py` (845 lines) stay out of Targets. The
  node uses `gterminals`' public API (`HostSupervisor`, `HostOptions`,
  `EpochAuthority`, `ControlClient`) from new gdaemon files. New node tests go
  in new test files; `crates/gdaemon/tests/nodes.rs` (81.6K) only changes its
  `serve_doors` call sites.
- **S2.8 overlap.** The open S2.8 leaves #23211 (`crates/gterminals/src/write.rs`),
  #23213 (Python spawn executor) and #23214 (`crates/gterminals/src/family.rs`,
  `tmux_runtime.rs`) share no Target with this plan.

## P1: Node hook ingress (#24150)
`kind: framing`

**Goal**: on a node, every hook passes through a node-local ledger that dedupes
ghook's retries, replays stored responses, drains the spool, forwards delivery
receipts, and asks the hub for the replay decisions that need hub state.

### 1.1 Freeze the hook ingress contract [category: test]
`kind: deliverable`

Targets:
- `docs/contracts/hook-ingress.md`
- `tests/contracts/http/hooks_execute_ok.json`
- `tests/contracts/http/hooks_execute_replay.json`
- `tests/contracts/http/hooks_execute_below_floor.json`
- `tests/contracts/http/hooks_execute_bad_schema.json`
- `tests/contracts/http/manifest.json::*` — scope-reason: adds the `hooks` family and its four case entries
- `tests/contracts/http/README.md`

**Research context:**
- #24150 requires freezing `POST /api/hooks/execute` before the node forwards
  into it, and #24151 (hub half) ports the route against that freeze.
- The route is `execute_hook`, nested in
  `src/gobby/servers/routes/mcp/hooks.py::create_hooks_router`.
  `_normalize_hook_request` accepts only a JSON object whose `schema_version`
  equals `SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION` and answers 400 otherwise.
  It reads `hook_type`, `input_data`, `source`, `critical`, `enqueued_at` and
  `response_capability`.
- Below the `hook-response.v1` floor
  (`src/gobby/hooks/runtime_compat.py::envelope_has_hook_response_capability`)
  the route answers `_graceful_error_response` before any claim.
- With `X-Gobby-Envelope-Id` the route claims the envelope. On a lost claim it
  replays the stored response (`envelope_terminal_response`), reclaims a
  vanished or stale marker, or answers 409 with
  `{"status": "processing" | "processed", "reason": ...}` or
  `{"status": "malformed_marker", "reason": "duplicate envelope marker malformed"}`.
- Retryable failures answer 503 `{"status": "retry", "retry_kind": ...}` with
  `retry_kind` `preflight_timeout`, `ingress_backpressure` or
  `adapter_timeout`, and release the claim.
- A processed response carries `_gobby_delivery_receipt`
  (`src/gobby/hooks/receipt_redelivery.py::attach_delivery_receipt`).
  `ReceiptGuardedJSONResponse` releases the receipt when the response cannot be
  written.
- ghook spools `<gobby_home>/hooks/inbox/{c|n}-<ts13>-<uuid>.json`
  (`crates/ghook/src/transport.rs::envelope_filename`). The envelope id is the
  file stem. ghook sends `Authorization` from `gobby_core::local_token`, which
  on a node is the node's machine-bound key in `bootstrap.yaml`.
  After a 2xx with a receipt, `crates/ghook/src/dispatch.rs::settle_delivered_inbox`
  overwrites the spool file with
  `{"kind":"delivery-receipt","receipt_id","original_envelope_id","delivery_generation"}`;
  without a receipt it deletes the file.
- Existing Python tests already pin the 409 and 503 bodies:
  `tests/servers/routes/mcp/test_hook_session_metadata.py::test_envelope_id_malformed_marker_returns_clear_conflict`,
  `tests/servers/routes/test_hooks_agy_dispatch.py` and
  `tests/servers/test_mcp_routes.py`.
- The corpus (`tests/contracts/http/README.md`) records one request per case
  from the e2e daemon at `schema_version` 2. Cases replay in manifest order
  against one daemon, so a second case can reuse the first case's envelope id.
  `tests/contracts/test_http_corpus.py::test_case_replays_equal` replays every
  `up` case. The Rust harness `crates/gdaemon/tests/http_contracts.rs` replays
  every case through a stub backend.
- The corpus freezes the hub route's wire contract. The node arm (1.5) forwards
  to a hub, which the corpus does not model, so 1.5 and 1.6 prove node
  behavior against a fake hub. No node parity is claimed here.

**Implementation:**
- `docs/contracts/hook-ingress.md` states the frozen contract:
  - request: envelope v1 fields, `X-Gobby-Envelope-Id`, `X-Gobby-Session-Id`,
    bearer key;
  - the response-capability floor and its graceful response;
  - 200 with the hook response and optional `_gobby_delivery_receipt`;
  - replay of a stored response, including the STOP and turn-end block rule
    (120-second grace, processed after `.stop_replay_epoch`);
  - the 409 bodies, the 503 retry bodies and the 400 schema rejection;
  - the receipt-ack file body;
  - a "Node forwarding" section pointing at this plan's 1.5 rules.
- Add a `hooks` family to `manifest.json` with `parity: "proxy"` and
  `origin: "python"`, and four cases with `credential: "operator"`, recorded by
  `test_record_http_contracts`:
  - `hooks_execute_ok`: an envelope v1 hook with a fixed envelope id that the
    e2e daemon answers 200 without a session row. Volatile fields are masked.
  - `hooks_execute_replay`: the same envelope id again, answered with the same
    stored body.
  - `hooks_execute_below_floor`: `response_capability` below the floor,
    answered with the graceful response.
  - `hooks_execute_bad_schema`: a flat body without `schema_version`, answered
    400.
- README lists the family and the doc.

Verification planned:
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -v`.
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(http_contracts)'`.

**Acceptance:**

- 1.1.1 - `docs/contracts/hook-ingress.md` states the request, every response shape listed above, the replay rule, the receipt-ack file body and the node forwarding pointer. file: `docs/contracts/hook-ingress.md`.
- 1.1.2 - The four `hooks` cases replay equal against the e2e daemon. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.
- 1.1.3 - The manifest lists the `hooks` family and its cases, and the manifest contract still holds. test: `tests/contracts/test_http_corpus.py::test_manifest_contract`.

### 1.2 Node envelope ledger [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares the new `hooks` submodule
- `crates/gdaemon/src/nodes/hooks/mod.rs`
- `crates/gdaemon/src/nodes/hooks/ledger.rs`
- `crates/gdaemon/tests/node_hooks.rs`

**Research context:**
- The oracle is `src/gobby/hooks/envelope_dedupe.py` (784 lines). Port, by
  function:
  - `claim_envelope_processing`: O_EXCL create of
    `<processed>/<envelope_id>` under the marker lock, writing
    `{envelope_id, claimed_at, status:"processing", owner_token, owner_pid,
    owner_create_time, renewed_at, lease_expires_at}` (sorted keys, trailing
    newline). Lease 15 s (`ENVELOPE_PROCESSING_LEASE_TTL_SECONDS`).
  - `renew_envelope_processing_lease`, `finalize_envelope_processed` and
    `release_envelope_processing_claim`: CAS on `owner_token` through
    `_cas_mutate_processing_marker`. Finalize writes
    `{envelope_id, processed_at, status:"processed", response?, hook_type?}`.
  - `read_envelope_marker`, `is_envelope_processed`,
    `is_envelope_processing_active`, `clear_stale_envelope_processing_marker`
    and `mark_envelope_processed`. A `processing` marker stays active while
    its owner process is live (pid and create time within 1 s), even past
    `lease_expires_at`, or while its lease is unexpired
    (`_is_active_processing_record`). It is stale only when the owner is dead
    and the lease is expired. A non-empty unreadable marker counts as
    processed; an empty one counts as absent.
  - `envelope_terminal_response` with `_should_replay_processed_response`: a
    stored block (`decision` `block` or `deny`) for `stop`, `subagent_stop`,
    `subagent_end`, `session_end`, `turn_end` or `stop_failure`, or one whose
    reason carries `[block-terminal-validation-failure]`, replays only within
    120 s and when processed at or after `.stop_replay_epoch`. Every other
    stored response replays.
  - `bump_stop_replay_epoch` and `read_stop_replay_epoch`.
  - `prune_processed_envelope_markers`: delete markers older than 24 h, at most
    100,000 entries per pass, 64 per lock hold.
  - `is_inbox_envelope_fresh` and `envelope_timestamp_ms_from_inbox_path`
    (filename `^[nc]-<ms>-.+$`).
- The lock is `src/gobby/utils/durable_file.py::exclusive_file_lock` on
  `<processed>/.locks/<sha256(name)[:2]>`, which flocks the sidecar
  `.<shard>.lock` with mode 0600. The Rust port uses the same path and
  `libc::flock`, as `crates/gdaemon/src/lifecycle/pid_file.rs` does. File
  work runs under `tokio::task::spawn_blocking`.
- The owner identity is the gdaemon process id and its start time. A boot
  sweep (1.6) relies on the node daemon singleton (`NodeServices::prepare`
  claims `gobby.pid`).
- The inbox root is `<gobby_home>/hooks/inbox`, where `gobby_home` is the
  bootstrap file's parent directory (`serve::run` derives it the same way).
- Rejected: a new marker schema. Decision 11 keeps the Python format.

**Implementation:**
- `nodes/mod.rs` adds `pub mod hooks;`. `nodes/hooks/mod.rs` (new) declares
  `pub mod ledger;`.
- `nodes/hooks/ledger.rs` (new) defines `Ledger { inbox: PathBuf }` with
  `claim`, `renew`, `finalize`, `release`, `marker`, `is_processed`,
  `is_active`, `clear_stale`, `mark_processed`, `terminal_response`,
  `bump_stop_epoch`, `is_fresh` and `prune`, mirroring the Python functions
  above, and the
  constants with their Python values.
- `crates/gdaemon/tests/node_hooks.rs` (new) holds this plan's P1 tests.
  Ledger tests write the marker files Python writes and read Python-format
  fixtures.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_hooks)'`.

**Acceptance:**

- 1.2.1 - A claim creates a Python-format processing marker and a second claim of the same id fails while the first holds it. test: `crates/gdaemon/tests/node_hooks.rs::ledger_claim_is_exclusive`.
- 1.2.2 - Renew, finalize and release succeed only for the owner token that holds the lease. test: `crates/gdaemon/tests/node_hooks.rs::ledger_mutations_require_owner_token`.
- 1.2.3 - A processing marker is stale only when its owner process is dead and its lease is expired; a live owner past its lease stays active, and a non-empty unreadable marker counts as processed. test: `crates/gdaemon/tests/node_hooks.rs::ledger_stale_and_malformed_markers`.
- 1.2.4 - A stored STOP or turn-end block replays only within 120 seconds and only when processed at or after the STOP epoch; any other stored response always replays. test: `crates/gdaemon/tests/node_hooks.rs::ledger_stop_block_replay_window`.
- 1.2.5 - Pruning deletes markers older than 24 hours, keeps newer ones and stops at 100,000 entries. test: `crates/gdaemon/tests/node_hooks.rs::ledger_prune_retention`.
- 1.2.6 - A marker written by the Python ledger reads back with the same status and response. test: `crates/gdaemon/tests/node_hooks.rs::ledger_reads_python_markers`.

### 1.3 Hub routes for node replay decisions and receipts [category: code]
`kind: deliverable`

Targets:
- `src/gobby/hooks/replay_decision.py`
- `src/gobby/hooks/inbox.py::_drain_hook_inbox_once_locked`
- `src/gobby/hooks/inbox.py::_consume_inbox_delivery_receipt`
- `src/gobby/hooks/inbox.py::_terminalize_below_floor_receipts`
- `src/gobby/hooks/replay_fence.py::archive_superseded_hook`
- `src/gobby/hooks/grok_pending_context.py::handle_ack_pending_inbox_envelope`
- `src/gobby/servers/routes/node_hooks.py`
- `src/gobby/servers/_app_routes.py::register_routes`
- `tests/servers/test_node_hook_routes.py`
- `tests/hooks/test_replay_decision.py`

**Granularity:** one leaf across six production files and two test files,
with five acceptance items. `decide_replay` is a
single decision function that the local drain and both routes share; splitting
the extraction from its routes would leave a leaf whose only consumer lands
elsewhere, and 1.3.3 must prove the local drain unchanged in the same commit
that extracts it.

**Research context:**
- The Python drain (`src/gobby/hooks/inbox.py::_drain_hook_inbox_once_locked`)
  takes four per-envelope decisions from hub state. A node cannot take them,
  and hub-mode 2.2 covers none of them:
  1. Grok pending delivery
     (`src/gobby/hooks/grok_pending_context.py::handle_ack_pending_inbox_envelope`),
     before the processed check and again after a 2xx, only for an envelope
     with an id at or above the floor. If the session's
     delivery variable names the envelope, it retains the file for
     `GROK_DELIVERY_RETENTION_SECONDS` (3,600) from the file's mtime, then
     requeues the briefing, unlinks the file and removes the marker.
  2. Superseded-hook fence
     (`src/gobby/hooks/replay_fence.py::archive_superseded_hook`). A known
     terminal session, an envelope without a valid `enqueued_at`, or one that
     predates the latest turn start is archived as `superseded_hook`. A failed
     lookup retains it. An unknown session is admissible.
  3. Below the response-capability floor: release the claim, run
     `_terminalize_below_floor_receipts` (`terminalize_receipts_for_envelope`),
     and quarantine the file as `below_floor_response_capability`.
  4. Delivery-receipt acks (`_consume_inbox_delivery_receipt`):
     `acknowledge_receipt` CAS, then `apply_acknowledged_receipt`
     (`src/gobby/hooks/receipt_effects.py`, which marks P2P messages
     delivered, commits startup context and applies session variables), then
     `_mark_carrying_envelopes_processed` for the receipt's
     `original_envelope_id` and `current_envelope_id`.
- Between the Grok check and the fence, the drain unlinks a processed
  envelope and then skips a live one
  (`src/gobby/hooks/replay_fence.py::defer_live_hook`: a fresh file or an
  active marker, only for an envelope at or above the floor). A live file
  still gets the Grok check and is never fenced
  (`src/gobby/hooks/inbox.py` lines 453-479).
- `src/gobby/hooks/receipt_redelivery.py::release_receipt_for_response` calls
  `release_receipt(db, receipt_id=, delivery_generation=)` when a response
  cannot be emitted.
- `src/gobby/storage/hook_receipts.py` rows carry `session_id`, so the route
  can check the receipt's session machine.
- `server.auth_service.verified_front_door_identity(request)` returns
  `FrontDoorIdentity(user_id, machine_id, key_id)` only for a key-authenticated
  request through gdaemon. Hub-mode 1.4 (`src/gobby/servers/routes/nodes.py`,
  not landed) uses it with 403 `node_identity_required` and 403
  `machine_is_local`. This plan's routes use the same rules in their own file,
  so they share no Target with hub-mode 1.4. `register_routes` is the shared
  registration point and imports each router from its own module.
- `src/gobby/sessions/machine_scope.py::is_local_machine_owner(owner, local)`
  is the existing machine predicate.

**Implementation:**
- `src/gobby/hooks/replay_decision.py` (new) defines
  `decide_replay(app, envelope_id, envelope, *, age_seconds, processed, live, machine_id) -> ReplayDecision`.
  `ReplayDecision` has `disposition` (`retain`, `requeued`, `unlink`,
  `archived`, `below_floor` or `execute`) and `detail`. Order, matching the
  drain's:
  1. Grok pending check, only for an envelope with an id at or above the
     floor, as the drain gates it today (`src/gobby/hooks/inbox.py` lines
     453-456): `retain` inside retention, `requeued` after requeueing. A
     below-floor envelope skips it.
  2. `processed` true: `unlink`.
  3. `live` true for an envelope at or above the floor: `retain` with detail
     `live`.
  4. Fence: `archived` with the fence detail, or `retain` on a failed lookup.
  5. Floor: `below_floor` after terminalizing the envelope's receipts.
  6. Otherwise `execute`.
  With `machine_id` set, a session row on another machine counts as unknown
  in both the Grok and fence lookups.
- `handle_ack_pending_inbox_envelope` splits into a pure decision on
  `(envelope_id, envelope, age_seconds)` and the file effects.
  `archive_superseded_hook` gains a keyword `machine_id`. The local drain
  keeps its restart-residue skip, then calls `decide_replay` with
  `live=defer_live_hook(path, envelope_id, processed_dir, include_fresh)`
  and `machine_id=None`, and keeps its file effects. `defer_live_hook` only
  reads the file's age and the marker, so reading it before the Grok check
  leaves the outcomes unchanged. `_consume_inbox_delivery_receipt` and
  `_terminalize_below_floor_receipts` call the shared helpers.
- `replay_decision.py` also defines
  `acknowledge_and_apply_receipt(db, *, receipt_id, delivery_generation, message_manager, variable_manager)`,
  which calls `acknowledge_receipt` and then the unchanged
  `src/gobby/hooks/receipt_effects.py::apply_acknowledged_receipt`. It returns
  the committed receipt or `None`, and the drain uses it too.
- `src/gobby/servers/routes/node_hooks.py` (new) defines
  `create_node_hooks_router(server)` with two routes. Both read the verified
  identity: none answers 403 `node_identity_required`, and the hub's own
  machine answers 403 `machine_is_local`. Bodies reject unknown fields; an
  unknown `v` answers 422 `unsupported_version`, and any other parse failure
  answers 422 `invalid_body`.
  - `POST /api/nodes/hooks/replay`: body
    `{v, envelope_id, envelope, age_seconds, processed, live}`. It answers
    `{disposition, detail}` from `decide_replay` with
    `machine_id=identity.machine_id`.
  - `POST /api/nodes/receipts`: body
    `{v, op: "ack" | "release", receipt_id, delivery_generation}`. A receipt
    whose session row is on another machine answers `{acknowledged: false}`
    or `{released: false}`. `ack` answers `{acknowledged, carrying_envelope_ids}`;
    `release` answers `{released}`.
- `register_routes` includes the router.

Consumers unchanged:
- `src/gobby/hooks/inbox_maintenance.py` — no-edit-reason: it calls `drain_hook_inbox_once`, whose signature is unchanged.
- `tests/hooks/test_inbox.py` — no-edit-reason: the drain's outcomes and its functions' signatures are unchanged (1.3.3).
- `tests/hooks/test_inbox_barrier_deadline.py` — no-edit-reason: `_drain_hook_inbox_once_locked` keeps its signature and outcomes.
- `tests/hooks/test_replay_fence.py` — no-edit-reason: `archive_superseded_hook`'s new `machine_id` keyword defaults to `None`, the current behavior.
- `tests/workflows/test_session_variable_mutation_scope.py` — no-edit-reason: `handle_ack_pending_inbox_envelope` keeps its signature and effects.
- `src/gobby/servers/app_factory.py` — no-edit-reason: it calls `register_routes`, whose signature is unchanged.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: it inspects `register_routes`, which only gains one router include.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_node_hook_routes.py tests/hooks/test_replay_decision.py tests/hooks/ -k "inbox or replay or receipt or grok_pending" -v`.

**Acceptance:**

- 1.3.1 - `decide_replay` returns `retain`, `requeued`, `unlink`, `archived`, `below_floor` and `execute` in the drain's order for the matching hub state, with a live envelope at or above the floor retained after the processed check and before the fence, and a below-floor envelope with a matching Grok delivery answered `below_floor`. test: `tests/hooks/test_replay_decision.py::test_decision_order`.
- 1.3.2 - With a machine id, a session row on another machine is treated as unknown by the Grok and fence lookups. test: `tests/hooks/test_replay_decision.py::test_foreign_rows_are_unknown`.
- 1.3.3 - The local drain's outcomes are unchanged after the extraction, and a fresh live file still receives the Grok check and is not fenced. test: `tests/hooks/test_replay_decision.py::test_local_drain_unchanged`.
- 1.3.4 - The replay route answers the decision for a node key, including the node's `live` flag, 403 `node_identity_required` without one, 403 `machine_is_local` for the hub's own key, and 422 for an unknown `v` or field. test: `tests/servers/test_node_hook_routes.py::test_replay_route`.
- 1.3.5 - The receipts route acknowledges a node's receipt, applies its effects once, returns the carrying envelope ids, releases on `release`, and refuses another machine's receipt. test: `tests/servers/test_node_hook_routes.py::test_receipts_route`.

### 1.4 Node receipt forwarding [category: code] (depends: 1.2, 1.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/hooks/mod.rs`
- `crates/gdaemon/src/nodes/hooks/receipts.rs`
- `crates/gdaemon/tests/node_support/mod.rs`
- `crates/gdaemon/tests/node_support/fake_hub.rs`
- `crates/gdaemon/tests/node_hooks.rs`

**Research context:**
- On a node the receipt-ack files sit on node disk, and no hub sweep sees them.
  Without forwarding, receipts never terminalize and P2P messages are
  re-injected on every hook.
- The Python consumer (`src/gobby/hooks/inbox.py::_consume_inbox_delivery_receipt`)
  claims an ack file by renaming it to `<name>.<owner>.claimed.tmp`, where
  `owner` is one process-wide hex token. The rename lets exactly one consumer
  take it, and no sweep lists a `.tmp` name.
  `_restore_orphaned_receipt_claims` renames a claim whose owner is not the
  current process back to `<name>` at drain start. After the ack the consumer
  marks the ack file's own envelope id processed and deletes the claim.
- The hub route `POST /api/nodes/receipts` (1.3) acknowledges and answers
  `{acknowledged, carrying_envelope_ids}`. The node marks each carrying id
  processed in its own ledger (1.2).
- Python sweeps receipts before every hook
  (`src/gobby/servers/routes/mcp/hooks.py`, the
  `consume_pending_delivery_receipts` call ahead of the hook): an ack must land
  before the next response's carry-forward bumps the generation. The node keeps
  that order in 1.5.
- Forwarding uses the node's key from `bootstrap.yaml`
  (`gobby_core::local_token::read_api_key_at`) over `HubRelay`.

**Implementation:**
- `nodes/hooks/receipts.rs` (new) defines `sweep_receipts(node, ledger)`:
  1. Restore orphaned claims.
  2. List spool files whose JSON `kind` is `delivery-receipt`.
  3. Claim each by rename.
  4. Post `op: "ack"`.
  5. On a 2xx, mark the carrying ids and the ack's own id processed and delete
     the claim.
  6. On any other outcome, rename the claim back so the next sweep retries.
- It also defines `release_receipt(node, receipt)`, which posts
  `op: "release"` for 1.5.
- `nodes/hooks/mod.rs` declares `pub mod receipts;`.
- `crates/gdaemon/tests/node_support/` (new) is the shared node test support
  that `node_hooks.rs` and `node_agents.rs` include with
  `#[path = "node_support/mod.rs"] mod node_support;`. `fake_hub.rs` is decision
  13's fake hub: an axum server on `127.0.0.1:0` that records each request's
  method, path, headers and JSON body and answers from a per-path script of
  status and body. `mod.rs` declares it and builds a node home (bootstrap with
  an `api_key` and `hub_daemon_url` pointing at the fake hub) for
  `NodeServices::prepare`.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_hooks)'`.

**Acceptance:**

- 1.4.1 - A receipt-ack file is claimed once, posted with the node key, and on 2xx its carrying envelopes and its own id are marked processed and the claim is removed. test: `crates/gdaemon/tests/node_hooks.rs::receipts_ack_forwarded_and_terminalized`.
- 1.4.2 - A failed post restores the ack file for the next sweep, and an orphaned claim from a previous process is restored at sweep start. test: `crates/gdaemon/tests/node_hooks.rs::receipts_failed_post_and_orphan_restore`.

### 1.5 Node hook ingress arm [category: code] (depends: 1.1, 1.2, 1.4)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gdaemon/src/nodes/hooks/mod.rs`
- `crates/gdaemon/src/nodes/hooks/ingress.rs`
- `crates/gdaemon/tests/node_hooks.rs`

**Granularity:** one leaf with seven acceptance items. They are the outcomes of
one request path (claim, forward, finalize or release, receipt release) owned
by `forward_claimed` and its HTTP wrapper `execute`; each item is a branch of
that path, and none is testable without the others' claim and forward steps.

**Research context:**
- `FrontDoor::handle`'s `Door::Node` arm answers
  `POST /api/runtime/handshake/challenge` locally and relays everything else
  (`node.relay.forward` or `node.relay.splice`). `HubRelay::forward` keeps the
  request's `Authorization` header, so ghook's node key reaches the hub, and
  hub-mode 2.2 binds the envelope machine to that key.
- The route rules to mirror are in 1.1's research context: floor check before
  the claim, claim or replay or reclaim or 409, lease renewal, finalize on
  success, release on retry.
- ghook waits 30 s (115 s for permission requests)
  (`crates/ghook/src/transport.rs::post_timeout`). axum drops a handler future
  when its client disconnects, so the forward runs in a spawned task.
- A response the node cannot write must release its receipt, as
  `ReceiptGuardedJSONResponse` does on the hub.

**Implementation:**
- `nodes/hooks/ingress.rs` (new) defines two entry points:
  - `forward_claimed(node, ledger, envelope_id, request) -> HubOutcome`, the
    core that 1.6's drain also calls:
    1. Claim. On a lost claim:
       - replay the stored response with 200;
       - reclaim a stale marker (owner dead and lease expired) or a vanished
         one;
       - otherwise answer 409 with the Python bodies.
    2. Start a renewal task that renews the lease every 5 s, concurrent with
       the next two steps, and cancel it when the forward finishes.
    3. Run `sweep_receipts`.
    4. Forward through `HubRelay::forward` and buffer the response.
    5. Hub 2xx: finalize with the response JSON and the request's
       `hook_type`. When `hook_type` is `session_start` or `SessionStart`,
       bump the STOP epoch.
    6. Hub 409 with `status: "processed"`: mark processed without a response.
       Any other 409, a 503, another status or a relay failure releases the
       claim.
    It never posts a receipt `release`.
  - `execute(node: Arc<NodeServices>, ledger, request) -> Response`, the HTTP
    path. `FrontDoor::handle` calls it for `POST /api/hooks/execute` in the
    `Door::Node` arm, before the relay:
    1. Buffer the request body. Without `X-Gobby-Envelope-Id`, or when
       `response_capability` is below the floor, forward unchanged with no
       claim.
    2. Run `forward_claimed` in a spawned task and answer its response: the
       hub's bytes, the replay, or the 409.
    3. If the handler's receiver is gone when the task finishes, or writing
       the body fails, post `release` for an attached
       `_gobby_delivery_receipt`. The receiver check uses a `oneshot`; the
       write check wraps the response body in a stream whose drop before the
       last frame posts `release`. Only this path releases a receipt.
- `nodes/hooks/mod.rs` declares `pub mod ingress;`. `serve.rs` builds
  `FrontDoor::node` and calls `handle` through `serve_connection`; neither
  signature changes in this section.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_hooks) + binary(nodes) + binary(front_door)'`.

**Acceptance:**

- 1.5.1 - A hook with an envelope id is claimed, forwarded with ghook's key, finalized with the hub's response, and answered with the hub's bytes. test: `crates/gdaemon/tests/node_hooks.rs::ingress_forwards_and_finalizes`.
- 1.5.2 - A retry of a processed envelope replays the stored response without reaching the hub, and during a forward that outlasts the 15-second lease the marker's `lease_expires_at` moves forward before the forward returns and a retry answers 409 `processing`. test: `crates/gdaemon/tests/node_hooks.rs::ingress_dedupes_retries`.
- 1.5.3 - Hub 503 retry, hub 409 `processing` and relay failure release the claim and pass the hub's response through; hub 409 `processed` marks the envelope processed. test: `crates/gdaemon/tests/node_hooks.rs::ingress_failure_dispositions`.
- 1.5.4 - Pending receipt acks reach the hub before the hook they precede. test: `crates/gdaemon/tests/node_hooks.rs::ingress_sweeps_receipts_first`.
- 1.5.5 - A session start answered 2xx bumps the node's STOP epoch. test: `crates/gdaemon/tests/node_hooks.rs::ingress_session_start_bumps_epoch`.
- 1.5.6 - A client that disconnects mid-forward still gets its envelope finalized, and its attached receipt is released. test: `crates/gdaemon/tests/node_hooks.rs::ingress_disconnect_finalizes_and_releases`.
- 1.5.7 - A hook without an envelope id, or below the floor, is forwarded unchanged with no marker. test: `crates/gdaemon/tests/node_hooks.rs::ingress_unledgered_passthrough`.

### 1.6 Node spool drain, boot release and retention [category: code] (depends: 1.3, 1.5)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: changes `serve_doors`, `serve_with_runtime` and the unix `run`; two cfg-gated functions share the name `run`
- `crates/gdaemon/src/nodes/hooks/mod.rs`
- `crates/gdaemon/src/nodes/hooks/drain.rs`
- `crates/gdaemon/tests/nodes.rs::*` — scope-reason: its three `serve_doors` call sites pass the new heartbeat argument
- `crates/gdaemon/tests/node_hooks.rs`

**Research context:**
- The Python drain loop
  (`src/gobby/hooks/inbox_maintenance.py::drain_hook_inbox_loop`) runs once at
  start, then every 60 s with up to 5 s of jitter, and prunes hourly: expired
  markers (24 h) plus orphaned `.tmp` files older than an hour
  (`prune_orphaned_inbox_temp_files`, 100,000 per pass).
- The drain's per-file order (`_drain_hook_inbox_once_locked`):
  1. Restore orphaned receipt claims, then consume receipt files.
  2. Take the hub-state decisions in 1.3's order: Grok check, processed
     unlink, live skip (`defer_live_hook`: a fresh file, or an active
     processing marker), fence, floor.
  3. Clear a stale marker and post.
  4. On 2xx, mark processed and unlink. 409 retains the file. 401
     quarantines it and releases the claim.
- `crates/gdaemon/src/serve.rs::serve_doors` creates `HeartbeatHost::new()`
  and starts it with no jobs. `HeartbeatHost::register` refuses after start,
  and the base cadence is 5 s. Callers: `serve::run` (node branch),
  `serve_with_runtime`, and three in `crates/gdaemon/tests/nodes.rs` (the
  `node` and `channel_door` helpers and one test).
- Quarantine follows ghook's `quarantine_malformed_at`
  (`crates/ghook/src/transport.rs`): move to `<inbox>/quarantine/` with a
  reason.

**Implementation:**
- `serve_doors` takes `heartbeat: HeartbeatHost` (not yet started) and starts
  and stops it as today. `serve_with_runtime` and the test helpers pass
  `HeartbeatHost::new()`.
- `serve::run`'s node branch:
  1. Builds the ledger.
  2. Runs the boot release: remove every `processing` marker whose owner is
     not this process.
  3. Registers `node-hook-drain` (60 s) and `node-hook-prune` (3,600 s).
  4. Calls `serve_doors`.
- `nodes/hooks/drain.rs` (new) defines `drain_once(node, ledger)`:
  1. `sweep_receipts`.
  2. For each hook file oldest first, live or not, compute `live` as
     `Ledger::is_fresh` or `Ledger::is_active`.
  3. Post `/api/nodes/hooks/replay` with `age_seconds` from the file mtime,
     `processed` from the ledger and `live`, then act on the disposition:
     - `retain`: keep the file.
     - `requeued`: unlink the file and remove the marker.
     - `unlink`: unlink the file.
     - `archived`: quarantine as `superseded_hook`.
     - `below_floor`: release and quarantine as
       `below_floor_response_capability`.
     - `execute`: call 1.5's `forward_claimed`, never `execute`, so no
       receipt `release` is posted. On 2xx mark processed and ask the replay
       route once more, with `processed: true`, to apply the post-execute
       Grok check.
  4. A 401 from either call quarantines the file as `replay_auth_rejected`.
- It also defines `prune(ledger)`.
- `nodes/hooks/mod.rs` declares `pub mod drain;`.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_hooks) + binary(nodes) + binary(heartbeat)'`.

**Acceptance:**

- 1.6.1 - The drain applies every replay disposition to the spool file as specified, and an `execute` envelope is forwarded, marked processed and rechecked without posting a receipt `release`. test: `crates/gdaemon/tests/node_hooks.rs::drain_applies_dispositions`.
- 1.6.2 - A live file is posted with `live: true`, gets the Grok disposition, and is otherwise kept; receipt files are swept before hook files. test: `crates/gdaemon/tests/node_hooks.rs::drain_skips_live_and_sweeps_receipts`.
- 1.6.3 - At node start, processing markers from a previous process are removed and markers held by the current process are kept. test: `crates/gdaemon/tests/node_hooks.rs::boot_releases_dead_processing_markers`.
- 1.6.4 - A node daemon registers the drain and prune jobs on its heartbeat host. test: `crates/gdaemon/tests/node_hooks.rs::node_registers_hook_jobs`.
- 1.6.5 - Pruning removes expired markers and abandoned `.tmp` files older than an hour. test: `crates/gdaemon/tests/node_hooks.rs::prune_markers_and_temp_files`.

## P2: Node agent duties (#24148)
`kind: framing`

**Goal**: a node answers the hub's `agent.spawn`, `agent.cancel` and
`terminal.write` commands, prepares worktrees and clones whose path and branch
embed `run_id`, launches claude, codex and grok through one SRT chokepoint in
its own terminal host, reports accepted runs, live agents and exits, and
registers its checkouts, all as `docs/contracts/node-commands.md` specifies.

Every P2 section adds to `crates/gdaemon/src/nodes/agents/mod.rs` and
`crates/gdaemon/tests/node_agents.rs`, so the sections form one dependency
chain from 2.1 to 2.9.

### 2.1 Durable node agent state [category: code] (depends: 1.6)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares the new `agents` submodule
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/state.rs`
- `crates/gdaemon/src/state.rs::NodeServices`
- `crates/gdaemon/src/state.rs::NodeServices::prepare`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- The contract's node duties (`docs/contracts/node-commands.md`, "Node
  duties") require node-local durable state for:
  - accepted runs, which leave `accepted` only by moving to `agents` or
    `exits`;
  - cancel records, written before the cancel reply and surviving a node
    daemon restart, so a later `agent.spawn` answers `cancelled`;
  - preparation records, written before the first filesystem effect and
    naming the workspace path and branch, kept until removal succeeds;
  - launched workspace records, kept until a report carrying the run's exit
    gets 200;
  - exits, oldest first, repeated until a report carrying them gets 200;
  - `revision`, a positive integer that never repeats across a restart;
  - `hub_epoch`, adopted from a `stale_report` refusal.
- The report's `host_epoch` is a number (`crates/gdaemon/src/node_commands.rs::ReportBody`),
  while the terminal host's epoch is a string
  (`crates/gterminals/src/host.rs::HostInfo`). The node keeps a counter that
  2.2 bumps on each new epoch string.
- `crates/gdaemon/src/state.rs::NodeServices` holds `bootstrap_path` and
  `relay`. `NodeServices::prepare(bootstrap, path, home)` checks the API key,
  builds the relay and claims `gobby.pid`. It is the node's only shared
  handle: the front door (`FrontDoor::node`), `run_client` and `serve::run`
  all hold the same `Arc<NodeServices>`. The PID claim makes the node daemon
  a singleton, so one process owns the state file.
- The node's capacity rule counts accepted and live runs together: while they
  total 1,000, `agent.spawn` answers `command_failed` with `stage` `"none"`.
- The fsync-then-rename pattern is in
  `crates/gcore/src/grant/cache.rs` (write a temp file, `sync_all`, `rename`).
  No shared helper exists.

**Implementation:**
- `nodes/mod.rs` adds `pub mod agents;`. `nodes/agents/mod.rs` (new) declares
  `pub mod state;` and defines `NodeAgents { home: PathBuf, state: tokio::sync::Mutex<AgentState>, changed: tokio::sync::Notify }`
  with `NodeAgents::open(home)`. Later sections add their fields here.
- `nodes/agents/state.rs` (new) defines `AgentState`, serialized to
  `<gobby_home>/node/agents.json` (directory 0700, file 0600):
  - `revision: u64`, `hub_epoch: Option<u64>`, `host_epoch: u64`,
    `host_epoch_id: Option<String>`;
  - `runs: BTreeMap<run_id, RunRecord>`, where `RunRecord` holds
    `terminal_id`, `phase` (`accepted`, `preparing` or `launched`),
    `accepted_at_ms`, `workspace: Option<WorkspaceRecord>` and
    `host_terminal_id: Option<String>`;
  - `WorkspaceRecord { mode, path, branch_name, base_branch, source_root }`;
  - `removals: BTreeMap<run_id, WorkspaceRecord>`;
  - `cancelled: BTreeMap<run_id, cancelled_at_ms>`;
  - `exits: Vec<ExitRecord>`, oldest first, each a `ReportExit` with its
    `ended_at_ms`.
- Every mutation goes through `AgentState::commit`, which writes
  `agents.json.tmp`, calls `sync_all`, renames it over `agents.json`, syncs the
  directory, and then calls `changed.notify_one()`.
- `next_revision()` increments `revision` and commits before it returns the
  value, so a crash after the send cannot reuse it.
- `live_and_accepted()` counts every run record.
- `prune_cancelled(now)` drops cancel records older than 24 hours (Decision 19).
- `recover_after_restart()` runs once in `open` (Decision 18): each run in
  `accepted` or `preparing` becomes a `prepare_failed` exit with
  `launched: false` and no workspace, and its workspace record, if any, moves
  to `removals`.
- `open` creates an empty state when the file is absent. When the file exists
  but does not parse, it fails with the path and "move it aside to reset", and
  the node does not start, because a fresh state would repeat revisions.
- `NodeServices` gains `pub agents: Arc<NodeAgents>`, opened in `prepare` from
  `home`.

`FrontDoor::node`, `connect_client`, `run_client` and `serve::run` keep
reading `relay` and `bootstrap_path`, and `serve::run` calls
`NodeServices::prepare` with the same arguments, so this section changes none
of them.

Consumers unchanged:
- `crates/gdaemon/tests/run_modes.rs` — no-edit-reason: it calls `NodeServices::prepare`, whose signature is unchanged.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents) + binary(run_modes) + binary(nodes)'`.

**Acceptance:**

- 2.1.1 - Agent state round-trips through `agents.json` across a reopen, and the file is written by rename with mode 0600. test: `crates/gdaemon/tests/node_agents.rs::agent_state_round_trips`.
- 2.1.2 - `next_revision` persists each value before returning it, so revisions never repeat across a reopen. test: `crates/gdaemon/tests/node_agents.rs::revision_never_repeats_after_restart`.
- 2.1.3 - An unparsable `agents.json` makes `NodeServices::prepare` fail with the file's path. test: `crates/gdaemon/tests/node_agents.rs::prepare_refuses_unreadable_agent_state`.
- 2.1.4 - Cancel records survive a reopen and are pruned after 24 hours. test: `crates/gdaemon/tests/node_agents.rs::cancel_records_survive_restart_and_expire`.
- 2.1.5 - On reopen, accepted and preparing runs become `prepare_failed` exits with `launched: false`, and their workspace records become pending removals. test: `crates/gdaemon/tests/node_agents.rs::restart_fails_unlaunched_runs`.

### 2.2 Node terminal host [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/host.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the unix `run` starts the terminal host; two cfg-gated functions share the name `run`
- `crates/gdaemon/tests/node_support/mod.rs`
- `crates/gdaemon/tests/node_support/fake_gterm.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- A node has no terminal host today. Hub and standalone daemons start one in
  `crates/gdaemon/src/state_services.rs::ActiveServices::start` with
  `HostOptions::from_pool` and `PostgresEpochAuthority`, both database-backed.
- `crates/gterminals/src/host.rs` is public and needs no edit:
  - `HostSupervisor::new(options, Arc<dyn EpochAuthority>)`, `start()` (adopts
    a running host or starts one, returning `HostInfo { pid, epoch, version, adopted }`),
    `subscribe()` and `epoch_snapshot()`.
  - `HostOptions { socket_dir, binary_path, machine_id, gobby_home, health_interval, retry_ceiling, shutdown_grace, spawn_args }`.
    For an empty `config_store`, `from_pool` yields `socket_dir = home`,
    `binary_path = home/bin/gterm`, `gobby_home = Some(home)`, a 5-second
    health interval, a 30-second retry ceiling, a 10-second shutdown grace,
    and the tmux attach-history flags at their defaults.
  - `EpochAuthority`: only `bindings` is required; `observe_epoch`,
    `observe_exit` and `epoch_snapshot` have defaults. `bindings` returns the
    epochs bound to terminal rows; a node has no rows.
- `crates/gterminals/src/control.rs::ControlClient` speaks the JSON-lines
  control protocol over `<socket_dir>/gterm-control.sock`, authenticated by
  `hello` with the token in `<socket_dir>/gterm-control.token`. It offers
  `list`, `spawn`, `spawn_commit`, `spawn_abort`, `kill`, `write` and
  `subscribe_events`/`next_event`.
- The machine id comes from `gobby_core::machine::read_machine_id_from_home(home)`,
  as `RuntimeState::prepare` reads it.
- Test doubles to copy from: `crates/gterminals/tests/host_lifecycle.rs::HostFixture::new`
  (a fake host the supervisor adopts) and
  `crates/gterminals/tests/protocol_contract.rs::scripted_host`, with the wire
  goldens in `crates/gterminal/tests/fixtures/wire_golden/`.

**Implementation:**
- `nodes/agents/host.rs` (new):
  - `NodeEpochAuthority { agents: Arc<NodeAgents> }` implements
    `EpochAuthority`. `bindings` returns an empty list. `observe_epoch(epoch)`
    increments `host_epoch` and stores `host_epoch_id` when `epoch` differs
    from the stored id, and commits.
  - `node_host_options(home, machine_id)` builds the options listed above.
  - `HostControl` holds one `ControlClient<UnixStream>` behind a mutex. It
    connects and runs `hello` on first use, and reconnects once when a call
    fails with an I/O error.
- `NodeAgents` gains `host: HostControl`.
- `serve::run`'s node branch creates `HostSupervisor::new(node_host_options(..), NodeEpochAuthority)`
  and calls `start()` before `serve_doors`. A start failure is logged and
  retried by the supervisor; hook ingress keeps working. At shutdown the node
  drops the supervisor without `stop` or `drain`, so live agents outlive a node
  daemon restart, as the report rules expect.
- `crates/gdaemon/tests/node_support/fake_gterm.rs` (new) is a fake host on a
  temp home: it writes the token and PID files the supervisor adopts, answers
  `hello`, `ping`, `list`, `spawn`, `spawn_commit`, `spawn_abort`, `kill` and
  `write` from a scripted inventory, records each request, and emits
  `terminal_exited` events on demand. `node_support/mod.rs` declares it.

Consumers unchanged:
- `crates/gdaemon/src/main.rs` — no-edit-reason: it calls `serve::run`, whose signature is unchanged.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents) + binary(nodes)'`.

**Acceptance:**

- 2.2.1 - The node's epoch authority bumps and persists the numeric host epoch exactly once per new host epoch string. test: `crates/gdaemon/tests/node_agents.rs::host_epoch_counter_follows_epoch`.
- 2.2.2 - A node daemon adopts a running terminal host and completes `hello` with the control token. test: `crates/gdaemon/tests/node_agents.rs::node_adopts_terminal_host`.
- 2.2.3 - A dropped control connection is reopened and re-authenticated on the next call. test: `crates/gdaemon/tests/node_agents.rs::host_control_reconnects`.

### 2.3 Worktree and clone preparation and removal [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the unix `run` runs the startup removal retry; two cfg-gated functions share the name `run`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/workspace.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- Contract rules (`docs/contracts/node-commands.md`):
  - `workspace.mode` `"none"` runs the agent in `source_root`.
  - `"worktree"` and `"clone"` prepare a workspace from `source_root` whose
    path and branch embed `run_id`. A null `base_branch` means
    `source_root`'s current branch.
  - The preparation record is written before the first filesystem effect.
  - Removal is idempotent: a path and branch already gone count as removed.
  - A failed removal, or a restart during preparation or removal, leaves the
    record. The node retries at startup and on each report interval, and
    deletes the record only when the path is gone.
- The Python oracle for preparation:
  - `src/gobby/agents/isolation_worktree.py` (worktree add);
  - `src/gobby/agents/isolation_clone.py` (clone and branch);
  - `src/gobby/agents/isolation_repair.py::repair_isolation_environment`:
    - `_copy_cli_hooks` copies `.claude` (claude) or `.codex` (codex) from the
      source; grok has no directory to copy.
    - `ensure_project_json_for_isolation` (`src/gobby/utils/project_context.py`).
    - `_patch_mcp_config_for_isolation`.
    - `apply_isolation_git_hygiene` (`src/gobby/agents/isolation_git_hygiene.py`),
      which excludes `.mcp.json`, `.claude/`, `.codex/`,
      `.factory/hooks/hooks.json` and `.gobby/isolation.json`.
  - The Python environment pre-seed is skipped (Constraints).
- Local Python workspaces live under `~/.gobby/worktrees/<project>/<branch>`.
  hub-mode Decision 15 needs node paths that no other machine's row names.

**Implementation:**
- `nodes/agents/workspace.rs` (new):
  - `plan_workspace(payload, home)`: with `"none"`, no record and the cwd is
    `source_root`. Otherwise:
    - path `<home>/worktrees/<project_id>/<run_id>` or
      `<home>/clones/<project_id>/<run_id>`;
    - branch `branch_name` when it already contains `run_id`, else
      `<branch_name>-<run_id>`;
    - base `base_branch`, or `git -C <source_root> rev-parse --abbrev-ref HEAD`.
  - `prepare(agents, run_id, plan, cancel)`:
    1. Commit the run's `preparing` phase with the workspace record.
    2. Worktree: `git -C <source_root> worktree add -b <branch> <path> <base>`.
       Clone: the clone and branch steps of `isolation_clone.py`.
    3. Repair for the provider as above.
    4. Check `cancel` between steps.
    Git runs through `tokio::process::Command` with its exit status checked;
    a failure returns the step and stderr.
  - `remove(record)`: worktree `git worktree remove --force <path>` then
    `git branch -D <branch>`, clone `remove_dir_all`. A missing path or
    branch counts as removed.
  - `retry_removals(agents)` tries each pending removal and deletes the
    record of each one that succeeds. 2.8's report job calls it on every
    interval.
- `serve::run`'s node branch calls `retry_removals` once at start, after
  2.1's `open` has run `recover_after_restart`, and before `serve_doors`.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.3.1 - Worktree preparation commits its record before the first filesystem effect and creates a path and branch that embed `run_id` from the requested or current base. test: `crates/gdaemon/tests/node_agents.rs::worktree_prepared_with_record_first`.
- 2.3.2 - Clone preparation commits its record first and creates the clone and branch under the clones root. test: `crates/gdaemon/tests/node_agents.rs::clone_prepared_with_record_first`.
- 2.3.3 - Repair copies the provider's hook directory, writes `.gobby/project.json`, patches the MCP config and applies the git hygiene excludes for claude, codex and grok. test: `crates/gdaemon/tests/node_agents.rs::workspace_repair_per_provider`.
- 2.3.4 - A failed removal keeps its record, a later retry that succeeds deletes it, and a path and branch already gone count as removed. test: `crates/gdaemon/tests/node_agents.rs::failed_removal_keeps_record_and_retries`.
- 2.3.5 - A node daemon restart after the record, after the worktree add, and during removal each ends with the workspace removed and the record deleted by the startup retry. test: `crates/gdaemon/tests/node_agents.rs::restart_at_each_boundary_cleans_up`.

### 2.4 SRT policy for node runs [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/mod.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/tables.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/paths.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/settings.rs`
- `crates/gdaemon/tests/fixtures/node_srt/corpus.json`
- `tests/agents/test_node_srt_corpus.py`
- `crates/gdaemon/tests/node_agents.rs`

**Granularity:** one leaf across five production files. They are one pure
function, from policy inputs to `settings.json` bytes, split only to keep each
file small; the corpus test proves them together, and no subset produces a
checkable output.

**Research context:**
- The node's sandbox config (Constraints, v1 limits) is
  `src/gobby/agents/sandbox.py::agent_sandbox_config(None)`: `enabled` from
  the payload, backend `srt`, mode `permissive`, `allow_network` false, and
  the payload's `extra_write_paths`. For `network: "trusted"`,
  `src/gobby/agents/sandbox_network.py::definition_sandbox_config` adds the
  vendored seed (`src/gobby/agents/sandbox_domains.py::trusted_domains`,
  reading `src/gobby/data/sandbox/trusted_domains.json`), `allow_git_network`
  and `allow_package_registries`.
- The oracle chain, called by `src/gobby/agents/srt_runtime.py::prepare_sandbox_launch`:
  - `src/gobby/agents/sandbox_policy.py::prepare_sandbox_run_paths` and
    `src/gobby/agents/sandbox_run_environment.py::SandboxRunPaths` (run root,
    writable caches, cargo home and target, and `environment(provider)`).
  - `src/gobby/agents/sandbox.py::compute_sandbox_paths`, which combines:
    - from `sandbox_policy.py`: `default_write_paths`,
      `gcode_runtime_write_exceptions`, `provider_write_exceptions`,
      `gobby_read_exceptions`, `toolchain_read_roots`,
      `mcp_config_read_exceptions`, `provider_read_exceptions`,
      `sensitive_roots`, `credential_read_roots`,
      `toolchain_credential_paths`, `sensitive_write_roots`,
      `allowed_domains` and `credential_env_vars`;
    - `_git_metadata_write_paths`;
    - `src/gobby/agents/sandbox_control_paths.py::provider_control_write_paths`
      and `assert_credential_read_contract`;
    - `src/gobby/integrations/rtk.py::sandbox_paths`, which grants RTK paths
      only when its binary or state directory exists.
  - `render_srt_settings` (see Constraints for its module).
- `_existing_home_roots`, `toolchain_read_roots` and the RTK lookup probe the
  filesystem, and `provider_read_exceptions` resolves the provider executable
  from `PATH`. A corpus must pin the home tree, the environment and the
  executable path to be deterministic.
- Corpus precedent: `crates/gdaemon/tests/fixtures/node_commands/corpus.json`
  holds golden messages that Python and Rust both check. A Rust production file
  can embed a repository file with `include_str!`, which keeps one copy of the
  Trusted seed.

**Implementation:**
- `nodes/agents/sandbox/mod.rs` (new) declares the three modules and defines
  `PolicyInputs { provider, network, extra_write_paths, workspace, home, gobby_home, env, provider_executable, daemon_port, websocket_port, run_id }`.
- `sandbox/tables.rs` (new) holds the static tables from `sandbox_policy.py`,
  `sandbox_domains.py` and `sandbox_control_paths.py` for claude, codex and
  grok, and the Trusted seed through
  `include_str!` of `src/gobby/data/sandbox/trusted_domains.json`.
- `sandbox/paths.rs` (new) ports run-path preparation without the pre-commit
  store, and `compute_sandbox_paths`, with every filesystem probe resolved
  against `PolicyInputs.home` and `PolicyInputs.env`, never the process
  environment. It ports `assert_sensitive_path_contract` and
  `assert_credential_read_contract` and fails with their messages.
- `sandbox/settings.rs` (new) ports `render_srt_settings` and serializes it
  with sorted keys and no spaces, as `prepare_sandbox_launch` hashes it.
- `crates/gdaemon/tests/fixtures/node_srt/corpus.json` (new) holds cases, each
  with its inputs, a home tree (relative paths to create), and the expected
  `settings.json` bytes and run environment, with `{home}`, `{gobby_home}`,
  `{workspace}` and `{run_root}` placeholders. Cases: each of claude, codex
  and grok with `network` `"none"` and `"trusted"`, one with
  `extra_write_paths`, and one with an RTK state directory.
- `tests/agents/test_node_srt_corpus.py` (new) builds each case's tree under
  `tmp_path`, runs the Python oracle with that home and environment, and
  compares the placeholder form with the corpus. With
  `GOBBY_RECORD_NODE_SRT_CORPUS=1` it rewrites the corpus instead.

Verification planned:
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_node_srt_corpus.py -v`.
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.4.1 - The Python oracle's output for every corpus case equals the committed corpus. test: `tests/agents/test_node_srt_corpus.py::test_corpus_matches_python`.
- 2.4.2 - The Rust policy produces each case's `settings.json` bytes and run environment exactly. test: `crates/gdaemon/tests/node_agents.rs::srt_settings_match_python_corpus`.
- 2.4.3 - `network: "trusted"` adds the embedded Trusted seed and the git and package-registry domains, and `network: "none"` adds neither. test: `crates/gdaemon/tests/node_agents.rs::srt_network_profiles`.
- 2.4.4 - An `extra_write_paths` entry inside a sensitive or credential root is refused with the Python contract's message. test: `crates/gdaemon/tests/node_agents.rs::srt_policy_refuses_sensitive_grants`.

### 2.5 SRT install verification and the launch chokepoint [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/mod.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/runtime.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/grok_home.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- ROADMAP decision 6: SRT wrapping has one chokepoint.
- `src/gobby/agents/srt_runtime.py::prepare_sandbox_launch`, after the policy
  (2.4), with `backend` `srt`:
  - It refuses grok on Linux and refuses `allow_network`.
  - For grok it prepares a private `GROK_HOME`
    (`src/gobby/agents/grok_sandbox_home.py::prepare_grok_sandbox_home`) and
    adds its runtime write paths and protected deny paths.
  - It copies `GOBBY_PROMPT_FILE` into `<run_root>/assets/prompt.md` (0600)
    and points the variable there.
  - It checks that the cargo cache grants resolve inside the sandbox cache, and
    that no grant exposes another run under the managed execution root.
  - On Linux it adds the SRT install root to the read paths.
  - It hashes the policy and verifies the install
    (`verify_srt_installation`, `verify_srt_installation_locked`): the
    install root is `<gobby_home>/tools/srt/<SRT_RELEASE.version>`
    (`srt_install_root`, with the pin in
    `src/gobby/utils/dependency_requirements.py::SRT_RELEASE`), and
    `content-manifest.json` maps each file under `runner.mjs`,
    `package-lock.json` and `node_modules/` to its SHA-256, or to
    `link:<target>` for a symlink (`build_srt_content_manifest`). A mismatch
    is a lockout.
  - It writes `<run_root>/assets/settings.json` and an empty
    `<run_root>/logs/violations.jsonl`, both 0600 (`_write_private_file`).
  - It runs a preflight (`_preflight_srt`, 90-second timeout).
  - It sets `GOBBY_SRT_TMPDIR` (`srt_mux_tmpdir`).
- `SandboxLaunch.wrap` replaces `argv[0]` with the resolved provider executable
  and returns `[node, runner, "--settings", <settings>, "--violations", <violations>, "--", ...argv]`.
  With the sandbox disabled it returns the argv unchanged.
- Ruling 8: `gobby install` places the pinned runtime; gdaemon verifies and
  runs it and never installs.

**Implementation:**
- `sandbox/runtime.rs` (new) defines the chokepoint
  `prepare_launch(inputs, prompt_file) -> Result<SandboxLaunch, SandboxError>`
  and `SandboxLaunch::wrap(argv)`. 2.6 builds no other launch path. With
  `enabled` false it returns an unenforced launch whose `wrap` is the
  identity. Otherwise it performs the steps above in the Python order, and
  each refusal names the Python lockout reason.
- `SRT_VERSION` is a constant. A test reads
  `src/gobby/utils/dependency_requirements.py` with `include_str!` and asserts
  that `SRT_RELEASE` pins the same version.
- Verification hashes the install off the async runtime
  (`spawn_blocking`) under the same lock file the Python verifier uses
  (`srt_install_lock`).
- `sandbox/grok_home.rs` (new) ports `prepare_grok_sandbox_home` and
  `_declare_gobby_mcp_server`.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.5.1 - A verified install yields the wrapped argv `[node, runner, --settings, <settings>, --violations, <violations>, --, <provider executable>, ...]` with both files written 0600, and a disabled sandbox leaves the argv unchanged. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_wraps_once`.
- 2.5.2 - A content-manifest mismatch, a missing install or a failed preflight refuses the launch. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_refuses_unverified_install`.
- 2.5.3 - Grok gets a private `GROK_HOME` with its write and deny paths, and is refused on Linux. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_grok_home`.
- 2.5.4 - The Rust SRT version pin equals the Python `SRT_RELEASE` version. test: `crates/gdaemon/tests/node_agents.rs::srt_version_matches_python_pin`.

### 2.6 Provider launch, exits and host reconciliation [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/launch.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- The Python provider plans are the oracle, minus the steps listed under
  Constraints:
  - `src/gobby/agents/spawn_executor_providers.py::prepare_claude_spawn`:
    - argv from `src/gobby/agents/spawners/command_builder.py::build_cli_command`:
      `claude --session-id <session_id>`, then `--model` and `--effort` when
      set, then `--dangerously-skip-permissions`;
    - `--mcp-config <cwd>/.mcp.json --strict-mcp-config` when that file
      exists;
    - environment from `CLAUDE_MANAGED_AGENT_ENV`, `MCP_TIMEOUT=120000` and,
      with strict MCP, `MCP_CONNECT_TIMEOUT_MS` (all in
      `src/gobby/agents/constants.py`), plus `GOBBY_MACHINE_ID`;
    - under SRT, `--settings '{"sandbox": {"enabled": false}}'`;
    - then the payload's `provider_args`, then the prompt;
    - `src/gobby/agents/trust.py::pre_approve_directory("claude", cwd)`.
  - `prepare_codex_spawn`: `--ask-for-approval never --disable guardian_approval -C <cwd>`
    and `-c check_for_update_on_startup=false`, as `build_cli_command` emits
    them.
  - `prepare_grok_spawn`: `--always-approve --no-alt-screen --cwd <cwd>`,
    `--model`, `--reasoning-effort` and `--single`.
- The contract's environment: the seven identity variables, `GOBBY_DAEMON_URL`
  set to the node's own front door, and `GOBBY_PROMPT_FILE` set to a
  node-local file holding `prompt`.
- Host spawn (`crates/gterminals/src/control.rs`):
  `spawn(SpawnRequest { terminal_id, spawn_key, argv, cwd, env, cols, rows, commit_deadline_ms, .. })`
  returns `SpawnPrepared { host_terminal_id, pgid, .. }`, then `spawn_commit`
  starts the child or `spawn_abort` kills it. Python uses a 30,000 ms commit
  deadline (`src/gobby/terminals/native_runtime.py`) and 80 by 24 as the
  initial size (`src/gobby/terminals/frame_client.py`).
- When the leader exits, the host removes the pane itself and emits
  `terminal_exited` (`crates/gterminal/src/host/native_ops.rs`,
  `settle_leader_exit`). `HostEventKind::TerminalExited` carries
  `terminal_id` and `exit_code`.
- `list()` returns `Inventory { epoch, seq, terminals }`. Each entry has
  `terminal_id`, `spawn_key`, `host_terminal_id`, `commit_state` and `pgid`
  (`crates/gterminals/src/reconcile.rs::HostEntry`). An epoch change alone
  does not prove the panes are gone: 2.2 never stops the host, and
  `crates/gterminals/src/host.rs::EpochSnapshot` requires the consumer to
  check the live host against `current_epoch` before settling anything.

**Implementation:**
- `nodes/agents/launch.rs` (new):
  - `build_command(payload, cwd, front_door_url, prompt_file) -> (argv, env)`
    for claude, codex and grok as above. `front_door_url` is the node's own
    front door from its bootstrap (`bind_host`, `daemon_port`, and `https`
    when node TLS is on). The prompt file is
    `<gobby_home>/node/prompts/<run_id>.md` (0600).
  - `launch(agents, run_id, payload, cwd)`:
    1. Build the command.
    2. Call 2.5's `prepare_launch` and wrap the argv.
    3. `spawn` with `spawn_key = run_id`, then `spawn_commit` with 30,000 ms.
    4. Commit the run's `launched` phase with `host_terminal_id`.
    5. Arm a `timeout_seconds` timer that sends `kill` with `TERM`.
    A failure before `spawn_commit` calls `spawn_abort` when a child was
    prepared and returns `launch_failed`.
  - `watch_exits(agents)` subscribes to host events. On `terminal_exited`
    for a launched run, it commits an exit `{launched: true, exit_code, reason, workspace}`
    and removes the run. The reason is `cancelled` when a cancel record exists
    for the run, else `exited`. It reconciles once when it subscribes, and
    again on an event gap, a reconnect or a host epoch change.
  - `reconcile(agents)` lists the inventory from the live host. A launched
    run becomes an `exited` exit with a null `exit_code` only when its
    `terminal_id` is absent from that inventory; a present run stays live.
- `serve::run` starts `watch_exits` through 2.8's registration, so this
  section adds no serve change.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.6.1 - Each of claude, codex and grok gets the Python provider flags, the payload's `provider_args` and prompt, the seven identity variables, `GOBBY_DAEMON_URL` and `GOBBY_PROMPT_FILE`. test: `crates/gdaemon/tests/node_agents.rs::provider_commands_match_python`.
- 2.6.2 - A launch spawns through the chokepoint with `spawn_key` equal to `run_id`, commits, and records the run as launched with its host terminal id. test: `crates/gdaemon/tests/node_agents.rs::launch_spawns_and_commits`.
- 2.6.3 - A host exit event produces an `exited` exit with the exit code and workspace, or `cancelled` when a cancel record exists. test: `crates/gdaemon/tests/node_agents.rs::exit_events_record_exits`.
- 2.6.4 - `timeout_seconds` sends `TERM` to the agent. test: `crates/gdaemon/tests/node_agents.rs::timeout_kills_agent`.
- 2.6.5 - `reconcile` turns a launched run whose terminal is absent from the live host's inventory into an `exited` exit with a null exit code and keeps a present one live, both for runs reloaded from durable state after a restart and after a host epoch change. test: `crates/gdaemon/tests/node_agents.rs::reconcile_after_restart`.

### 2.7 Command dispatch over the node channel [category: code] (depends: 2.6)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/channel.rs::run_client`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Granularity:** one leaf with eight acceptance items. They are one command
state machine: each item is a transition of a run under the per-run queue that
`commands.rs` owns, and splitting spawn, cancel and write would separate the
queue from the commands that need it.

**Research context:**
- `crates/gdaemon/src/nodes/channel.rs::run_client` connects
  (`connect_client`), sends hello, then loops on pings and resets its idle
  timer on any inbound frame. It ignores frame contents.
- `crates/gdaemon/src/node_commands.rs` holds the frame types:
  `CommandFrame` (spawn, cancel, write), `ReplyFrame`, `OkReply`, `ErrReply`,
  `ErrorBody { code, message, stage }`, `AcceptedResult`, `StoppedResult` and
  `EmptyResult`. The models accept any `v` of at least 1, so the handler
  refuses unknown versions.
- Contract rules for each command:
  - `agent.spawn`:
    - Validate, record the run as accepted, and reply `{accepted: true}`
      before preparing anything. `stage` is `"none"` only when nothing was
      recorded.
    - At 1,000 accepted and live runs, answer `command_failed` with `"none"`
      and record nothing.
    - A run with a cancel record answers `cancelled` with `"none"`.
    - A failure before launch removes what was prepared and reports an exit
      with `launched: false` (`prepare_failed`, `prepare_timeout` or
      `launch_failed`).
  - `agent.cancel`:
    - Record the cancel before replying.
    - For an accepted run that has not launched, stop preparation, remove
      under the retry rule, answer `{stopped: true}` and report a `cancelled`
      exit with `launched: false`.
    - With nothing accepted or live, answer `not_found`.
  - `terminal.write`: answer `{}`. A failure after the write may have reached
    the pane is `command_failed` with `"partial"`.
  - Commands that name the same `run_id` run one at a time in arrival order.

**Implementation:**
- `run_client` routes each text frame whose `type` is `"command"` to
  `commands::dispatch` and sends the reply frame through the socket it already
  writes pings to (one writer task fed by an mpsc channel). Other frames keep
  today's behavior.
- `nodes/agents/commands.rs` (new):
  - `dispatch` parses `request_id`, `v` and `command` first, so every refusal
    can name its request. An unknown `v` answers `unsupported_version`, an
    unknown command `unknown_command`, and a payload that fails to parse,
    names a provider other than claude, codex or grok, or names a
    `source_root` that is not a directory answers `invalid_payload`. All
    three carry `"none"`.
  - Per-run queues: `NodeAgents` gains `queues: Mutex<HashMap<run_id, mpsc::Sender<Job>>>`.
    The first command for a run starts its worker, and the worker drains jobs
    in order and exits when its queue is idle. `terminal.write` resolves its
    `terminal_id` to a run first.
  - `agent.spawn`:
    1. A run with a cancel record answers `cancelled`, even while the run is
       still known.
    2. A known run with no cancel record answers `{accepted: true}` again and
       records nothing.
    3. A run at capacity answers `command_failed`.
    4. Otherwise commit the run as `accepted` and reply.
    5. Start the preparation task under a 300-second deadline (Decision 19)
       with a cancellation token: 2.3's `prepare`, then 2.6's `launch`.
       Failures map to `prepare_failed` (workspace or policy),
       `prepare_timeout` (deadline) or `launch_failed` (host). Each moves the
       workspace record to `removals`, runs `remove` once, and commits a
       `launched: false` exit.
  - `agent.cancel`:
    1. Commit the cancel record.
    2. An unlaunched run: trigger its token, wait for the task to stop, commit
       the `cancelled` exit with removal as above, and answer
       `{stopped: true}`.
    3. A launched run: `kill(host_terminal_id, signal)`. Answer
       `{stopped: true}` when the host reports the group gone and
       `{stopped: false}` otherwise. 2.6's exit watcher records the
       `cancelled` exit.
    4. No run: `not_found`.
  - `terminal.write`: an unknown terminal answers `not_found`, and an
    unavailable host answers `command_failed` with `"none"`. Otherwise call
    `write(WriteRequest { host_terminal_id, kind, data, submit })`. A refusal
    before the request was sent is `"none"`, and a failure after sending is
    `"partial"`.

`serve.rs` spawns `run_client(node, timing)`, whose signature is unchanged,
and the channel tests in `crates/gdaemon/tests/nodes.rs` send no `command`
frames, which are the only frames whose handling changes.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents) + binary(nodes)'`.

**Acceptance:**

- 2.7.1 - Unknown versions, unknown commands and invalid payloads answer their codes with `stage` `"none"` and the frame's `request_id`, and record nothing. test: `crates/gdaemon/tests/node_agents.rs::dispatch_rejects_bad_frames`.
- 2.7.2 - A spawn is recorded and answered `{accepted: true}` before preparation starts, then prepared and launched; a repeated spawn for a known run with no cancel record answers the same and records nothing new. test: `crates/gdaemon/tests/node_agents.rs::spawn_accepts_before_preparing`.
- 2.7.3 - A spawn at 1,000 accepted and live runs answers `command_failed` with `"none"` and records nothing. test: `crates/gdaemon/tests/node_agents.rs::spawn_refused_at_capacity`.
- 2.7.4 - A cancel before launch records the cancel, stops preparation, removes the workspace, reports a `cancelled` exit with `launched: false` and answers `{stopped: true}`; a later spawn answers `cancelled`, and a cancel for an unknown run answers `not_found`. test: `crates/gdaemon/tests/node_agents.rs::cancel_before_launch`.
- 2.7.5 - A cancel of a launched run sends the requested signal and reports a `cancelled` exit when the agent ends, and a spawn for that run before its exit answers `cancelled`. test: `crates/gdaemon/tests/node_agents.rs::cancel_launched_run`.
- 2.7.6 - A preparation failure, the preparation deadline and a host launch failure report `prepare_failed`, `prepare_timeout` and `launch_failed` with `launched: false`, and remove the workspace. test: `crates/gdaemon/tests/node_agents.rs::preparation_failures_report_exits`.
- 2.7.7 - `terminal.write` reaches the host for a known terminal, answers `not_found` for an unknown one, and `command_failed` with `"partial"` when the host fails after the request was sent. test: `crates/gdaemon/tests/node_agents.rs::terminal_write_dispositions`.
- 2.7.8 - Commands for the same run run one at a time in arrival order, and commands for different runs do not wait on each other. test: `crates/gdaemon/tests/node_agents.rs::same_run_commands_serialize`.

### 2.8 Node reports [category: code] (depends: 2.7)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the unix `run` registers the report job; two cfg-gated functions share the name `run`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/reports.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- Contract report rules (`docs/contracts/node-commands.md`, "Reports"):
  - Post every 30 seconds and immediately after any change.
  - `accepted` and `agents` are complete in every report.
  - One report at a time, each built fresh with the next `revision`; a retry
    is a new report, and a restart never repeats a revision.
  - A `stale_report` 409 body `{code, hub_epoch}` is adopted and followed by
    a fresh report.
  - `agents` comes from the terminal host, so agents that outlive a node
    daemon restart stay listed.
  - `exits` is oldest first, at most 1,000 per report, each repeated until a
    report carrying it gets 200. A `launched: true` exit names its
    workspace; a `launched: false` exit names none.
- `ReportBody`, `ReportAgent { run_id, terminal_id, pid, locator, locator_key, workspace }`,
  `ReportExit` and `StaleReport` are in `crates/gdaemon/src/node_commands.rs`.
- The native locator shape (`crates/gterminals/src/reconcile.rs`): the locator
  is the host terminal id and the key is `native:<host epoch>:<host terminal id>`.
  The inventory's `pgid` is the agent's process group leader.
- The hub route is hub-mode 1.4's `POST /api/nodes/report`, reached through
  `HubRelay` with the node's key.
- The heartbeat host registers jobs before start (1.6). `serve::run` already
  registers the hook jobs there.

**Implementation:**
- `nodes/agents/reports.rs` (new):
  - `report_once(node)`, under a `report_lock` that `NodeAgents` gains:
    1. Build the body: `v: 1`, the stored `hub_epoch`, `next_revision()`,
       `host_epoch`, `accepted` from runs in `accepted` or `preparing`,
       `agents` from launched runs present in a fresh `list()` (with `pid`
       from `pgid`, the native locator and key, and the workspace), and the
       first 1,000 exits.
    2. Post it.
    3. On 200, drop the exits it carried and their launched workspace
       records.
    4. On a `stale_report` 409, commit the hub's epoch and notify `changed`,
       so `report_loop` sends a fresh report as soon as this one releases
       `report_lock`. `report_once` never calls itself.
    5. On any other outcome, change nothing; the next report retries.
  - `report_loop(node)` waits on `changed` or the 30-second job tick. A change
    that arrives while a report is in flight queues exactly one more report.
- `serve::run`'s node branch:
  - registers `node-agent-report` (30 s), whose tick also calls 2.3's
    `retry_removals` and `prune_cancelled`;
  - at start, starts 2.6's `watch_exits` and `report_loop`. 2.3 owns the
    start-time `retry_removals` call.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents) + binary(heartbeat)'`.

**Acceptance:**

- 2.8.1 - Reports go out one at a time, each with a fresh higher revision, after every change and on the 30-second tick; a failed report is followed by a new revision, never the same bytes, including across a restart. test: `crates/gdaemon/tests/node_agents.rs::reports_are_fresh_and_serial`.
- 2.8.2 - A `stale_report` refusal makes the node adopt the hub's epoch and send a fresh report carrying it. test: `crates/gdaemon/tests/node_agents.rs::stale_report_adopts_epoch`.
- 2.8.3 - With more than 1,000 pending exits, each report carries the oldest 1,000, and an exit leaves only after a report carrying it gets 200. test: `crates/gdaemon/tests/node_agents.rs::exits_page_oldest_first_until_acknowledged`.
- 2.8.4 - A report lists every accepted run and every live agent from the host inventory with its pid, native locator, locator key and workspace. test: `crates/gdaemon/tests/node_agents.rs::report_lists_accepted_and_live_agents`.
- 2.8.5 - Each report tick retries pending removals and prunes expired cancel records. test: `crates/gdaemon/tests/node_agents.rs::report_tick_retries_removals`.
- 2.8.6 - A node daemon registers the report job and starts the exit watcher at start. test: `crates/gdaemon/tests/node_agents.rs::node_registers_report_job`.

### 2.9 Checkout registration [category: code] (depends: 1.5, 2.8)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/hooks/ingress.rs`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/checkouts.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- Contract: the node registers each checkout it serves before the hub can
  route a spawn of that project to it, with `POST /api/nodes/checkouts {v, project_id, root_path}`
  answering `{root_path, created}`; a repeat answers `created: false`
  (hub-mode 1.4).
- Decision 17 sets the trigger: the first forwarded hook from a root, whatever
  the hub answers to the hook.
- Hooks carry `input_data.cwd`. A project root is the nearest ancestor with
  `.gobby/project.json`, whose `id` is the project id. Node workspaces get a
  copied `.gobby/project.json` (2.3), so a root under
  `<gobby_home>/worktrees/` or `<gobby_home>/clones/` is a node workspace,
  never a checkout.
- 1.5's `nodes/hooks/ingress.rs::execute` handles every forwarded hook.

**Implementation:**
- `nodes/agents/checkouts.rs` (new) defines `observe_hook_cwd(node, cwd)`:
  1. Resolve the project root and id, skipping node workspace roots.
  2. If the root is not in the `registered` set that `NodeAgents` gains,
     spawn a task that posts the registration through `HubRelay` with the
     node key.
  3. On a 2xx, add the root to the set. A failure leaves it out, so the next
     hook retries.
- `ingress::execute` calls `observe_hook_cwd` before it forwards a hook, and
  does not wait for it.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents) + binary(node_hooks)'`.

**Acceptance:**

- 2.9.1 - The first hook from a project root registers that root once with the project id, even when the hub refuses the hook itself, and later hooks from it post nothing. test: `crates/gdaemon/tests/node_agents.rs::checkout_registered_on_first_hook`.
- 2.9.2 - A failed registration is retried on the next hook, and roots under the node's worktrees and clones are never registered. test: `crates/gdaemon/tests/node_agents.rs::checkout_registration_retries_and_skips_workspaces`.

## D1 Droid and agy providers on nodes (depends: 2.6)
`kind: deferred`

Ruling 3 limits node v1 to claude, codex and grok. Droid needs its generated
isolation hooks (`_copy_droid_hooks_for_isolation`) and the `ps` shim
workaround in `prepare_sandbox_launch` (#22407). Agy needs its support record
check (`agy_support_refusal`). Until this lands, 2.7 answers `invalid_payload`
for both.

Original acceptance:
- D1.1: a node launches droid and agy agents through 2.5's chokepoint with the Python provider plans' flags, hooks and environment.

```yaml
deferral:
  task_ref: "TBD-node-droid-agy-providers"
  reason: "Ruling 3 (gobby#15977, 2026-10-13) limits node v1 to claude, codex and grok; droid and agy need provider-specific isolation and sandbox steps."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

## D2 Node pane detection and its reporting route (depends: 2.8)
`kind: deferred`

hub-mode Decision 7 assigns node pane detection and its reporting route to
#24148's planning. Story B uses hook-driven attention only, and ruling 4
defers detection.

Original acceptance:
- D2.1: a node detects attention states in its agents' panes and reports them to the hub through a node route.

```yaml
deferral:
  task_ref: "TBD-node-pane-detection"
  reason: "Ruling 4 (gobby#15977, 2026-10-13) defers node pane detection; story B uses hook-driven attention only (hub-mode Decision 7)."
  owner: "orchestrator"
  original_acceptance_items:
    - D2.1
```

## D3 Live hub-to-node proof (depends: 1.6, 2.9)
`kind: deferred`

Every active section drives a fake hub and a fake terminal host (Decision 13).
The live path needs hub-mode's node routes (1.4), its internal command route
(D2) and its remote spawn (2.5), and is proven by hub-mode D4.

Original acceptance:
- D3.1: on a live hub and node, a node-local hook passes through the node ledger to the hub, a hub spawn launches an agent on the node, and the node's reports and checkout registration reach the hub.

```yaml
deferral:
  task_ref: "TBD-node-halves-live-proof"
  reason: "Needs hub-mode 1.4, D2 and 2.5 (#21575), not landed; hub-mode D4 runs the live proof once both sides exist."
  owner: "orchestrator"
  original_acceptance_items:
    - D3.1
```

## V1: Plan Changelog
`kind: framing`

- 2026-10-13: First draft from the Orchestrator's rulings 1 to 9
  (gobby#15977).
- 2026-10-13: Revision for Plan Adversary gobby#16791 findings NH-1 to NH-8:
  the ledger's stale rule is the owner-dead-and-lease-expired conjunction
  with concurrent renewal (1.2, 1.5); `decide_replay` takes `live` in the
  drain's slot and the node drain posts every file (1.3, 1.6); the drain
  calls `forward_claimed`, which never releases a receipt (1.5, 1.6);
  `reconcile` exits only runs absent from the live inventory (2.6); a cancel
  record wins over an idempotent spawn (2.7); 2.3 owns the start-time
  removal retry; 1.5 depends on 1.1; and a `stale_report` notifies `changed`
  instead of re-entering `report_once` (2.8).
- 2026-10-13: Consensus with Plan Adversary gobby#16791 after NH-9 (the Grok
  check runs only at or above the response-capability floor, 1.3) and NH-10
  (1.5.2 pins the lease renewal write).
- 2026-10-13: Coordinator return from gobby#15977: P1 expands under #24150
  and P2 under #24148, with cross-epic order as leaf dependencies (Decision
  1); the adversary is gobby#16791; the Stage 1 gate prose is dropped because
  the #21555 edge on both epics enforces it. Consensus with gobby#16791
  renewed on these bytes.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation,
none has run yet. Each must pass after its leaves land and before #24150 and
#24148 close.

```bash
CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_hooks) + binary(node_agents) + binary(nodes) + binary(front_door) + binary(heartbeat) + binary(http_contracts) + binary(run_modes)'
CARGO_BUILD_JOBS=3 cargo clippy -p gobby-daemon --all-targets -- -D warnings
cargo fmt -p gobby-daemon -- --check
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py tests/servers/test_node_hook_routes.py tests/hooks/test_replay_decision.py tests/agents/test_node_srt_corpus.py -v
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/node-halves.md -p .
```

- The plan validation command must pass at every revision; it is the only
  check that runs during planning.
- These gates cover the active sections, which drive a fake hub and a fake
  terminal host. D3's live proof runs in hub-mode D4.
- Every cargo run is a heavy command and needs the Release Manager's grant.
  Run these from the worktree root, and never run the full pytest suite.
