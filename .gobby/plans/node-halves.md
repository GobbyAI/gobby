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
hub-node command contract (#24268, `docs/contracts/node-commands.md`) is
landed. This plan implements its node side and amends it to version 2 (P3):
`agent.spawn` carries every launch input a node needs, node bodies report the
launch outcomes and terminations that need hub state, and each report carries
its agents' pane state. The node watches its agents' panes with a Rust port of
the lifecycle monitor's detectors (Decision 4). P3 also owns the hub's
`agent.spawn` builder, the hub routes for those bodies, the detection-manifest
route, and the hub consumers of the reported pane state.

Scope is `RunMode::Node` only. Hub and standalone daemons keep the Python
ledger, the Python spawn path and the Python lifecycle monitor until the hub
halves, #24151 and #24149.

## Decision Record
`kind: framing`

Orchestrator rulings (gobby#15977, 2026-10-13), operator-approved planning
(`stage2-plan:now`):

1. **Roots and order.** P1's leaves (1.1 to 1.6) expand under #24150, and
   P2's and P3's leaves under #24148. Neither epic is a child of #21579, which
   both block, so leaves under #21579 could never become ready. Cross-epic
   order is expressed as leaf dependencies: 2.1 depends on 1.6 and 3.1, 2.9
   on 1.5, 3.5 on 1.3, 2.10 and 2.11 on 3.5, and 2.14 on 3.6 and 3.7, the
   hub routes they post to. (Amended by gobby#15977 on its 2026-10-13
   return.)
2. **Receipt route.** This plan owns the hub route `POST /api/nodes/receipts`,
   key-bound in the style of hub-mode 1.4.
3. **Providers.** Nodes launch all five providers: claude, codex, grok, droid
   and agy (Josh, 2026-10-13: "We're not deferring grok or agy").
4. **Pane monitoring on nodes.** Josh, 2026-10-13: "rust on the node now".
   hub-mode Decision 7 assigns node pane detection and its reporting route to
   #24148's planning. The node ports the lifecycle monitor's five named
   surfaces to Rust at full parity: the prompt detector, the idle detector,
   the stall classifier, health and auto-enter. It runs in gdaemon beside the
   node's terminal host, acts on its own panes, and reports attention and idle
   state to the hub in its report (2.8, 3.1). The detectors are one module,
   `crates/gdaemon/src/pane_detection/`, which #24149 (S2.7 hub half) reuses
   when the hub's Python monitor retires (2.12, 2.13). Each name maps to code
   as follows:
   - Prompt detector and auto-enter: `src/gobby/agents/prompt_detector.py`,
     `src/gobby/agents/loop_tracker.py` and
     `src/gobby/agents/terminal_prompt_monitor.py` (trust, loop, approval,
     queued continuation and periodic Enter). The node runs them and sends the
     keys itself (2.14).
   - Idle detector: `src/gobby/agents/idle_detector.py`, plus the other pane
     facts `IdleCheckHandler._handle_idle_check` reads: the quota message
     (`src/gobby/agents/watchdog/quota.py`) and the codex capacity banner. The
     node reports them, and the hub's idle policy consumes them for node runs
     (3.8).
   - Stall classifier: `src/gobby/agents/stall_classifier.py`, for attention
     and for the provider-stall check.
   - Health: `AgentHealthMonitor`'s three checks. The node runs the
     provider-stall check. It times the run timeout and the initialization
     timeout, and the hub decides each with the database conditions the local
     check applies (3.6). A dead terminal or process is 2.6's exit watcher,
     which hub-mode 2.6 settles.
   - Under all of them, the manifest engine: `src/gobby/agents/detection/`
     (`schema.py`, `matcher.py`, `safe_regex.py`) and the five bundled
     manifests in `src/gobby/install/shared/detection/`.

   The ruling does not name the rest of the monitor, which stays hub-side
   under hub-mode's machine scoping (hub-mode 2.1, 2.4 and 2.6):
   - Transcript watchdog recovery (`src/gobby/agents/watchdog/`) reads
     provider transcripts, not panes. For a node run the hub takes its
     existing branch for a session on another machine:
     `IdleCheckHandler._read_local_transcript` returns no snapshot.
   - The memory watchdog and `src/gobby/agents/interactive_attention_monitor.py`.
   - The database sweeps: reconciliation, pending reaping, session expiry,
     completed task agents, dispatch mutexes, and
     `check_autonomous_stuck_agents`, whose live-pane deferral reads a pane
     inside a task-state sweep.
5. **Placement.** Node duties live in `crates/gdaemon/src/nodes/`, following the
   node-channel plan's Decision 8. Family crates (ROADMAP decision 16) are for
   route families. The pane detectors are the one exception: they live in
   `crates/gdaemon/src/pane_detection/`, outside `nodes/`, so the hub branch
   of the same binary reuses them (Decision 4).
6. **Seats.** No Plan Enhancer. The adversary is Grok seat gobby#16791.
7. **Replay decisions.** This plan owns the hub route
   `POST /api/nodes/hooks/replay`. It shares one decision function with the
   local drain.
8. **SRT runtime.** Node machines keep the Python CLI for `gobby auth login` and
   `gobby install`. `gobby install` places the pinned SRT runtime. gdaemon only
   verifies and runs it.
9. **Contract version 2.** Josh's "No deferrals" (2026-10-13) lifts the
   version 1 payload limits. This plan owns
   `src/gobby/hub/spawn_payload.py::build_spawn_payload` with full tests, and
   the amendment ships `v: 2` on both sides (P3). In the same change,
   hub-mode 2.5 calls the builder and stops refusing remote resume.
   `credential_manager=None` stays in hub-mode 2.5: the managed Postgres
   credential never reaches a node (ROADMAP decision 17).

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
    live hub-to-node proof belongs to hub-mode D4 (#24283 Live hub-to-node
    proof), Decision 20.
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

Josh's rulings on the no-deferrals return (2026-10-13, relayed by
gobby#15977):

20. **Live proof.** hub-mode D4 owns the live hub-to-node proof. This plan
    carries no proof leaf.
21. **Node code-index preflight.** The node runs the managed code-index
    preflight in its own leaf (2.11), planned against the hub code-index
    routes that #21586 (Migrate code-index orchestration and shared
    dependencies) adds. 2.11 is blocked at expansion by #21586's route
    leaves; the edge is set then, because the coverage grammar has no
    cross-plan dependency.
22. **v2 sandbox.** The payload's `sandbox` is the hub's fully resolved
    `SandboxConfig` (`resolve_spawn_sandbox`), and v1's `network` key is
    dropped. The node adds only node-local spawn-time paths (2.4).
23. **Remote resume.** A resume is routed automatically to the resumed
    session's machine, overriding `machine` and the origin (hub-mode 2.5).
    As a local resume returns to its worktree, a remote resume runs in the
    workspace the session's earlier run used on that node: the payload names
    it as an `existing` workspace (3.1), the builder finds it from the hub's
    workspace row (3.2), and the node runs there in place and never removes
    it (2.1, 2.3).
24. **MCP command on nodes.** Node launches write `~/.gobby/bin/gmcp` as the
    Gobby MCP server. The leaves that write MCP config (2.3 and 2.6) are
    blocked at expansion by gmcp-stdio 1.6 (#23275), set then like 21.

Writer decisions for the no-deferrals revision:

25. **Loopback generation endpoints stay on the hub.** A remote spawn whose
    resolved generation endpoint is loopback (`is_local`, from
    `is_loopback_model_endpoint`) is refused before any row with
    `remote_loopback_endpoint`, `stage: "none"` (hub-mode 2.5). The hub's
    local-model preflight (`ensure_local_model`,
    `refresh_local_model_context`) probes the hub's own loopback server, so it
    cannot vouch for a model server on the node, and a loopback `api_base`
    names a different server on each machine. A non-loopback endpoint is
    carried in the payload and reaches the node unchanged.
26. **The provider token lives only in memory and the provider's
    environment.** `api_token` crosses the key-authenticated node channel
    inside `agent.spawn`. The node keeps it in the in-memory launch job, puts
    it only in the provider process environment, and never writes it to
    `agents.json`, the prompt file, `settings.json`, a report, a hub body or a
    log line. `agents.json` keeps none of them: an unlaunched run does not
    survive a restart (Decision 18), and a launched run keeps only 2.14's
    watch record, so a restart never needs the token.
27. **Detection manifests reach the node by a hub route.**
    `GET /api/nodes/detection-manifests` (3.7) serves the hub's
    `detection_manifests` rows. The node refetches on its 30-second report
    tick with the last `ETag`. The hub's own registry rereads the table every
    30 seconds (`DEFAULT_STALENESS_SECONDS`), so node runs see a manifest edit
    as soon as hub runs do. The node runs no pane check until its first
    successful fetch.
28. **Monitor settings ride the payload.** The hub's monitor reads
    `TmuxConfig` once, at construction (`AgentLifecycleMonitor.__init__`), so
    the four settings the node needs travel in `agent.spawn`'s `monitor`
    block (3.1) and carry the values the hub applies to its own runs.
29. **The regex engine is fancy-regex.** `droid.toml` uses a negative
    lookahead, which the `regex` crate rejects. fancy-regex 0.11 is already in
    `Cargo.lock`; gdaemon takes it as a direct dependency. A rule with a
    pattern longer than 2,048 characters or one that fails to compile is
    dropped with an `invalid_pattern` issue, as `_compile_fingerprint` drops
    it. A search that exceeds the backtrack limit is a `pattern_timeout`
    issue and reads as no match, as in Python. Python bounds a search by CPU time; Rust bounds
    it by backtracking steps, so the bound no longer depends on host load.
30. **The node observes; the hub decides and kills.** For the four causes
    the node observes (a doom loop, a provider stall, the run timeout and the
    initialization timeout), the node posts `POST /api/nodes/runs/terminate`
    (3.6). The hub applies the database conditions the local check applies.
    When they hold, it kills the run inside the route with `agent.cancel`,
    through 3.4's remote terminal runtime and hub-mode 2.4's
    `remote_cancel`, settles the run, and answers. 2.10's prompt-delivery
    failure follows the same rule (3.5). The node never kills on a reply:
    `retry: true` makes it post again on its next tick, and `retry: false`
    ends its asks for that cause. While the hub is unreachable, the node
    posts again on each tick and the run keeps running, as a local run does
    while the hub daemon is down. Every hub-side decision about a node run,
    including the idle policy's (3.8), reaches the node through the same
    runtime: `TerminalServices.runtime_for` returns 3.4's
    `RemoteTerminalRuntime` for a terminal on another machine. Kills go
    through hub-mode 2.4's `remote_cancel`, and writes through hub-mode 2.3's
    `_dispatch` guard.
31. **Checkpoints name the run.** Before it posts a doom-loop or
    provider-stall terminate, the node runs the checkpoint git plumbing in
    the run's workspace and names the hidden ref
    `refs/gobby/ckpt/<task_id>/<run_id>`. The terminate route (3.6) records
    the `checkpoints` row with that ref. The local ref's sequence number is a
    database count the node cannot read, and nothing reads a checkpoint ref
    except through its row.

## Constraints
`kind: framing`

- **Launch inputs split by where their state lives.** The hub resolves every
  input that needs hub state and carries it in version 2 of `agent.spawn`
  (3.1, 3.2): the resolved sandbox definition, the generation endpoint
  (`model`, `api_base`, `api_token`, `extra_env` and the codex endpoint
  fields), reasoning, the provider resume id and execution mode, the
  reviewer's `agent_name`, the droid mode, the pre-commit prewarm flag, the
  code-index preflight mode, and the prompt with its persona prefix. The node
  computes every input that needs node state: the workspace, its repair and
  Python environment seed (2.3), spawn-time sandbox paths and caches (2.4),
  provider argv and environment (2.6), typed codex prompt delivery (2.10) and
  the code-index preflight (2.11). Two outcomes after preparation need hub
  state, and the node posts them to 3.5's routes. Spawned agents always
  auto-approve (`src/gobby/agents/spawn_models.py`, `auto_approve: bool = True`),
  so no field carries it. 3.1 lists each `SpawnRequest` field the payload
  does not carry and why.
- **Cross-plan edges, set at expansion.** The coverage grammar has no
  cross-plan dependency, so the Orchestrator sets these edges when the leaves
  exist:
  - gmcp-stdio 1.6 blocks 2.3 and 2.6, and gmcp-stdio 1.8 blocks 2.4
    (Decision 24). gmcp-stdio (#23275 `gobby-mcp` crate) is not expanded;
    until its leaves exist the edges sit on #23275.
  - #21586 (Migrate code-index orchestration and shared dependencies) blocks
    2.11 (Decision 21). It has no leaves yet; the edge sits on the epic until
    its gcode gateway leaves exist.
  - 3.1 blocks hub-mode 1.3 (#24269 Python node-command client), 1.4 (#24270
    Node routes) and D2 (#24281 gdaemon internal node-command route), which
    build or check `v` against `CONTRACT_VERSION`.
  - 3.2 blocks hub-mode 2.5 (#24275 Remote spawn goes through the node),
    which calls `build_spawn_payload`.
  - hub-mode 1.4 (#24270 Node routes) blocks 3.3, which adds one call to
    the report route that 1.4 creates in `src/gobby/servers/routes/nodes.py`.
  - hub-mode 2.3 (#24273 Remote terminal writes go through the node) and 2.4
    (#24274 Remote cancel goes through the node) block 3.4, whose runtime
    calls 2.4's `remote_cancel` and relies on 2.3's write guard.
  - hub-mode 2.6 (#24276 The hub settles remote runs from the node's
    reports) blocks 3.6, so a run the terminate route kills settles through
    2.6's reconciler when the node's exit arrives.
  - hub-mode D4 (#24283 Live hub-to-node proof) proves this plan's sections
    on a live hub and node (Decision 20).
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
  `EpochAuthority`, `ControlClient`) from new gdaemon files.
  `crates/gterminals/src/control.rs` (733 lines) gains one method in 2.10 and
  stays under 850. `src/gobby/agents/spawn_executor_providers.py` (626 lines)
  gains one extracted function in 3.2. On the hub side,
  `src/gobby/agents/agent_health.py` (587 lines) gains two extracted module
  functions in 3.6, and `src/gobby/agents/idle_check_handler.py` (580 lines)
  gains the pane-facts split in 3.8; both stay under 850.
  `src/gobby/agents/capture.py` (649 lines) and
  `src/gobby/agents/watchdog/recovery.py` (990 lines) are not edited: 3.4's runtime
  reaches their kill paths through `TerminalServices.runtime_for`. New node tests go in new test files;
  `crates/gdaemon/tests/nodes.rs` (81.6K) only changes its `serve_doors` call
  sites.
- **S2.8 overlap.** The open S2.8 leaves #23211 (fail-closed write
  coordinator, `crates/gterminals/src/write.rs`), #23213 (bounded tmux
  fallback: `spawn_executor_terminal.py`, `spawn_executor.py`,
  `spawn_executor_runtime.py`, `web_spawn.py`) and #23214 (Rust tmux runtime
  parity: `crates/gterminals/src/family.rs`, `tmux_runtime.rs`) share no
  Target with this plan, including 2.10's `control.rs` and 3.2's
  `spawn_executor_providers.py`.

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
embed `run_id`, launches claude, codex, grok, droid and agy through one SRT
chokepoint in its own terminal host, types codex's first prompt, runs the
managed code-index preflight, watches its agents' panes with the Rust
detectors, reports accepted runs, live agents, their pane state and exits,
and registers its checkouts, all as version 2 of
`docs/contracts/node-commands.md` (3.1) specifies.

Every node-duty section adds to `crates/gdaemon/src/nodes/agents/mod.rs` and
`crates/gdaemon/tests/node_agents.rs`, so 2.1 to 2.11 and 2.14 form one
dependency chain. 2.1 depends on 3.1 for the version 2 types, 2.10 and 2.11
depend on 3.5 for the hub routes they post to, and 2.14 depends on 3.6 and
3.7. 2.12 and 2.13 build the detector module outside `nodes/` (Decision 5)
and depend on nothing in P2.

### 2.1 Durable node agent state [category: code] (depends: 1.6, 3.1)
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
  - `WorkspaceRecord { mode, path, branch_name, base_branch, source_root, reused }`,
    where `reused` marks a workspace a resume runs in place (2.3,
    Decision 23);
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
- `to_removals(run_id)` takes the run's workspace record and moves it to
  `removals`, or drops it when it is `reused`, so the node never removes a
  workspace it did not prepare. It returns the moved record.
- `recover_after_restart()` runs once in `open` (Decision 18): each run in
  `accepted` or `preparing` becomes a `prepare_failed` exit with
  `launched: false` and no workspace, and its workspace record, if any, goes
  through `to_removals`.
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
- 2.1.5 - On reopen, accepted and preparing runs become `prepare_failed` exits with `launched: false`, and their workspace records become pending removals, except a `reused` record, which is dropped and never becomes a removal. test: `crates/gdaemon/tests/node_agents.rs::restart_fails_unlaunched_runs`.

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

### 2.3 Worktree and clone preparation, repair and removal [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the unix `run` runs the startup removal retry; two cfg-gated functions share the name `run`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/workspace.rs`
- `crates/gdaemon/src/nodes/agents/repair.rs`
- `crates/gdaemon/tests/fixtures/node_isolation/droid_hooks.json`
- `tests/agents/test_node_isolation_golden.py`
- `crates/gdaemon/tests/node_agents.rs`

**Granularity:** one leaf with ten acceptance items across four production
files. Preparation, repair and removal are one workspace lifecycle: the
preparation record written in step 1 is the record removal deletes, and
repair runs between them on the same path. A resume's existing workspace
takes the same record and repair path. Splitting repair out would leave a
prepared workspace no provider can run in.

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
  - `src/gobby/agents/isolation_repair.py::repair_isolation_environment`
    runs five steps in this order:
    1. `_copy_cli_hooks` copies `.claude` (claude) or `.codex` (codex) from
       the source with a warning on failure. For droid it calls
       `_copy_droid_hooks_for_isolation`, which writes
       `.factory/hooks/hooks.json` from
       `src/gobby/install/droid/hooks-template.json`, rewritten by
       `src/gobby/cli/installers/hook_commands.py::rewrite_hook_template_commands`
       (`cli_name="droid"`, `hooks_dir` from `GOBBY_HOOKS_DIR` or
       `~/.gobby/hooks`) and merged over an existing file by
       `_merge_droid_isolation_hooks` with `merge_gobby_hook_groups` across
       `DROID_PASCAL_HOOK_NAMES` (`src/gobby/adapters/droid_contract.py`).
       A failure only warns. Grok and agy have no hook directory to copy.
    2. `ensure_project_json_for_isolation`
       (`src/gobby/utils/project_context.py`).
    3. `src/gobby/agents/python_env_seed.py::preseed_isolated_python_environment`:
       - It skips unless the workspace holds `pyproject.toml` and
         `uv.lock`, and skips when `uv` is not on `PATH`.
       - The child environment drops `_UV_ENV_TO_CLEAR` (`UV_CACHE_DIR`,
         `UV_PROJECT_ENVIRONMENT`, `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`) and
         sets `UV_PYTHON_DOWNLOADS=never`.
       - `uv cache dir` (10-second timeout, falling back to `~/.cache/uv`)
         names the host cache, and a missing cache directory skips the seed.
       - It runs `uv sync --offline --frozen --no-progress --link-mode copy --cache-dir <dir>`
         with a 180-second timeout. A failure only warns.
    4. `_patch_mcp_config_for_isolation` writes `.mcp.json` and, for claude,
       registers the workspace's MCP servers in `~/.claude.json` through
       `src/gobby/claude_config.py::update_claude_config`, which holds the
       `<path>.lock` mkdir lock with proper-lockfile's 10-second stale rule.
       gmcp-stdio 1.6 changes the server entry to
       `{"command": "<gobby_home>/bin/gmcp", "args": []}` (Decision 24).
    5. `apply_isolation_git_hygiene`
       (`src/gobby/agents/isolation_git_hygiene.py`), which excludes
       `.mcp.json`, `.claude/`, `.codex/`, `.factory/hooks/hooks.json` and
       `.gobby/isolation.json`.
- Local Python workspaces live under `~/.gobby/worktrees/<project>/<branch>`.
  hub-mode Decision 15 needs node paths that no other machine's row names.
- A local resume returns to its worktree:
  `src/gobby/mcp_proxy/tools/spawn_agent/_worktree_reuse.py::prepare_resumed_worktree`
  runs `repair_isolation_environment` on the existing path and runs the
  provider there "exactly as the seat left it", with no clean-tree check and
  no rebase.
- A Rust production file can embed a repository file with `include_str!`,
  which keeps one copy of the droid template.

**Implementation:**
- `nodes/agents/workspace.rs` (new):
  - `plan_workspace(payload, home)`: with `"none"`, no record and the cwd is
    `source_root`. With `"existing"` (a resume, Decision 23), the
    canonicalized `workspace.path` must be a directory directly under
    `<home>/worktrees/<project_id>/` or `<home>/clones/<project_id>/` for the
    payload's `project_id`; the record is that path with the mode its parent
    gives, the payload's `branch_name`, a null `base_branch` and
    `reused: true`. A path that is missing or outside those roots fails
    preparation (`prepare_failed`, naming the path). Otherwise:
    - path `<home>/worktrees/<project_id>/<run_id>` or
      `<home>/clones/<project_id>/<run_id>`;
    - branch `branch_name` when it already contains `run_id`, else
      `<branch_name>-<run_id>`;
    - base `base_branch`, or `git -C <source_root> rev-parse --abbrev-ref HEAD`.
  - `prepare(agents, run_id, plan, provider, cancel)`:
    1. Commit the run's `preparing` phase with the workspace record.
    2. Worktree: `git -C <source_root> worktree add -b <branch> <path> <base>`.
       Clone: the clone and branch steps of `isolation_clone.py`. A `reused`
       record skips this step.
    3. Run `repair::repair(source_root, path, provider, home)`.
    4. Check `cancel` between steps.
    Git runs through `tokio::process::Command` with its exit status checked;
    a failure returns the step and stderr.
  - `remove(record)`: worktree `git worktree remove --force <path>` then
    `git branch -D <branch>`, clone `remove_dir_all`. A missing path or
    branch counts as removed.
  - `retry_removals(agents)` tries each pending removal and deletes the
    record of each one that succeeds. 2.8's report job calls it on every
    interval.
- `nodes/agents/repair.rs` (new) ports the five repair steps in the Python
  order, for all five providers:
  - Hooks: claude and codex copy their directory with a warning on failure.
    Droid renders the template, embedded with `include_str!` of
    `src/gobby/install/droid/hooks-template.json`: each
    `__GOBBY_HOOK_COMMAND__` placeholder becomes the droid hook command for
    its key, with the node's `~/.gobby/bin/ghook` and `hooks_dir` from
    `GOBBY_HOOKS_DIR` or `<home>/hooks`. The rendered groups merge over an
    existing `.factory/hooks/hooks.json` across the droid hook names as
    `merge_gobby_hook_groups` merges them. A failure only warns.
  - `.gobby/project.json` as `ensure_project_json_for_isolation` writes it.
  - The Python environment seed with the steps, environment, timeouts and
    skip reasons above. A failure logs a warning and repair continues.
  - The MCP config: `.mcp.json` names `<gobby_home>/bin/gmcp` with no
    arguments as the `gobby` server. For claude, the same server is merged
    into `~/.claude.json`'s `projects.<workspace>.mcpServers` under the
    `~/.claude.json.lock` mkdir lock, with the same stale rule, and written
    through a temp file and rename. A failure only warns, as in Python.
  - The git hygiene excludes as `apply_isolation_git_hygiene` writes them.
- `crates/gdaemon/tests/fixtures/node_isolation/droid_hooks.json` (new) is
  the golden droid file for a fixed `hooks_dir`, `ghook` path and existing
  user file. `tests/agents/test_node_isolation_golden.py` (new) produces it
  with the Python functions and compares; with
  `GOBBY_RECORD_NODE_ISOLATION_GOLDEN=1` it rewrites the golden.
- `serve::run`'s node branch calls `retry_removals` once at start, after
  2.1's `open` has run `recover_after_restart`, and before `serve_doors`.

Verification planned:
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_node_isolation_golden.py -v`.
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.3.1 - Worktree preparation commits its record before the first filesystem effect and creates a path and branch that embed `run_id` from the requested or current base. test: `crates/gdaemon/tests/node_agents.rs::worktree_prepared_with_record_first`.
- 2.3.2 - Clone preparation commits its record first and creates the clone and branch under the clones root. test: `crates/gdaemon/tests/node_agents.rs::clone_prepared_with_record_first`.
- 2.3.3 - Repair runs its five steps in the Python order for each of claude, codex, grok, droid and agy: claude and codex get their hook directory, every provider gets `.gobby/project.json` and the git hygiene excludes, and grok and agy get no hook directory. test: `crates/gdaemon/tests/node_agents.rs::workspace_repair_per_provider`.
- 2.3.4 - A failed removal keeps its record, a later retry that succeeds deletes it, and a path and branch already gone count as removed. test: `crates/gdaemon/tests/node_agents.rs::failed_removal_keeps_record_and_retries`.
- 2.3.5 - A node daemon restart after the record, after the worktree add, and during removal each ends with the workspace removed and the record deleted by the startup retry. test: `crates/gdaemon/tests/node_agents.rs::restart_at_each_boundary_cleans_up`.
- 2.3.6 - The droid hooks file the node writes over an existing user file equals the committed golden. test: `crates/gdaemon/tests/node_agents.rs::droid_hooks_match_python_golden`.
- 2.3.7 - The Python template rewrite and merge produce the committed droid golden. test: `tests/agents/test_node_isolation_golden.py::test_droid_hooks_golden_matches_python`.
- 2.3.8 - The Python environment seed runs `uv sync --offline --frozen --no-progress --link-mode copy --cache-dir <dir>` with the uv variables cleared and `UV_PYTHON_DOWNLOADS=never`, skips without `pyproject.toml`, `uv.lock`, `uv` or the cache directory, and a failed or timed-out sync leaves repair running. test: `crates/gdaemon/tests/node_agents.rs::python_seed_matches_python_steps`.
- 2.3.9 - `.mcp.json` names `<gobby_home>/bin/gmcp` with no arguments, and for claude `~/.claude.json` gains the workspace's `mcpServers` under the mkdir lock while other projects' entries are kept. test: `crates/gdaemon/tests/node_agents.rs::mcp_config_names_gmcp`.
- 2.3.10 - An `existing` workspace under the project's worktrees or clones root is used in place: its uncommitted files survive, no git command creates anything, repair runs, the record is `reused`, and a missing path or one outside those roots fails preparation naming the path. test: `crates/gdaemon/tests/node_agents.rs::existing_workspace_used_in_place`.

### 2.4 SRT policy for node runs [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/mod.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/tables.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/paths.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/caches.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/prewarm.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/settings.rs`
- `crates/gdaemon/tests/fixtures/node_srt/corpus.json`
- `tests/agents/test_node_srt_corpus.py`
- `crates/gdaemon/tests/node_agents.rs`

**Granularity:** one leaf across seven production files. They are one pure
function, from policy inputs to the sandbox settings bytes and the run
environment, split only to keep each file small; the corpus test proves them
together, and no subset produces a checkable output. The pre-commit prewarm
is the one side effect, and it writes into the run root the same function
lays out.

**Research context:**
- The payload's `sandbox` is the hub's fully resolved `SandboxConfig`
  (Decision 22, 3.1): `enabled`, `backend` (`srt` or `provider-native`),
  `mode`, `allow_network`, the four extra path lists, `allowed_domains`,
  `denied_domains`, `allow_git_network`, `allow_package_registries` and
  `allow_unix_sockets`. The node applies only node-local spawn-time steps on
  top of it.
- The spawn-time steps, in `src/gobby/agents/spawn_executor_providers.py::_prepare_provider_sandbox`
  and `src/gobby/agents/spawn_cache_policy.py::sandbox_config_for_spawn`:
  - The nextest grant: when `<main checkout>/.gobby/project.json`'s sandbox
    config lists `<main checkout>/.nextest` in `extra_write_paths`, that
    path is appended. On a node the main checkout is `source_root`.
  - With the sandbox enabled, `apply_spawn_cache_policy` fills `UV_CACHE_DIR`
    and `CARGO_HOME` (`src/gobby/agents/constants.py::ensure_agent_uv_cache_dir`,
    `ensure_agent_cargo_home_dir`) and merges `PATH` (`merge_spawn_path`,
    which keeps the managed tool bin), and `_apply_sandbox_cache_policy`
    fills the 15 `SANDBOX_CACHE_POLICY` variables, each a per-session
    directory under `get_agent_session_cache_dir(session_id, "gobby", <name>)`.
  - `extra_write_paths` gains the project's own `extra_write_paths` (from
    `get_project_sandbox_config(project_path)`) and `sandbox_write_paths(env)`:
    the cache variables above, `CARGO_TARGET_DIR` and the hook inbox
    `~/.gobby/hooks/inbox`.
  - `allow_package_registries` is forced true.
  - `CARGO_TARGET_DIR` comes from
    `src/gobby/agents/cargo_target.py::ensure_agent_cargo_target_dir(checkout, project_id)`.
    hub-mode 2.5 does not compute the spawn environment for a remote run, so
    the node computes it.
- The oracle chain, called by `src/gobby/agents/srt_runtime.py::prepare_sandbox_launch`:
  - `src/gobby/agents/sandbox_policy.py::prepare_sandbox_run_paths` and
    `src/gobby/agents/sandbox_run_environment.py::SandboxRunPaths` (run root,
    writable caches, cargo home and target, and `environment(provider)`).
    The run root uses a short temp root when the backend is `srt`, the run
    may use unix sockets (`allow_run_unix_sockets=True` for every spawn) and
    the host is macOS.
  - With `prewarm_pre_commit_store`,
    `sandbox_policy.py::_prewarm_pre_commit_store` fills
    `<XDG_CACHE_HOME of the run>/pre-commit` when the workspace has
    `.pre-commit-config.yaml` and the operator store exists
    (`_operator_pre_commit_store`: `PRE_COMMIT_HOME`, then
    `XDG_CACHE_HOME/pre-commit`, then `~/.cache/pre-commit`). It renames the
    spare (`pre_commit_store_spare_paths`) into place, or clones the store
    (`/bin/cp -c -R` on macOS, a symlink-preserving copy otherwise) and makes
    it owner-writable. It then starts one background replenisher of the
    spare. Every failure only warns.
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
  - `sandbox_policy.py` holds per-provider rows for all five providers,
    including droid's and agy's.
  - With backend `srt`: `render_srt_settings` (see Constraints for its
    module), with the run's temp root added to `allow_unix_sockets` on macOS.
  - With backend `provider-native`: only claude proves the sensitive-root
    contract (`src/gobby/agents/provider_capabilities.py`,
    `sensitive_path_enforcement`). Any other provider is refused with
    "<provider> cannot prove the sensitive-root contract". For claude,
    `src/gobby/agents/sandbox_resolvers.py::ClaudeSandboxResolver.build_settings`
    builds the `--settings` JSON (deny lists, `_external_write_paths`, and
    loopback domains only with `allow_network`), and
    `preflight_provider_native_settings` checks it.
- `_existing_home_roots`, `toolchain_read_roots` and the RTK lookup probe the
  filesystem, and `provider_read_exceptions` resolves the provider executable
  from `PATH`. A corpus must pin the home tree, the environment and the
  executable path to be deterministic.
- Corpus precedent: `crates/gdaemon/tests/fixtures/node_commands/corpus.json`
  holds golden messages that Python and Rust both check.

**Implementation:**
- `nodes/agents/sandbox/mod.rs` (new) declares the modules and defines
  `PolicyInputs { provider, sandbox, source_root, project_id, session_id, workspace, home, gobby_home, env, provider_executable, daemon_port, websocket_port, run_id, prewarm_pre_commit_store }`,
  where `sandbox` is the payload's v2 sandbox.
- `sandbox/caches.rs` (new) ports the spawn-time steps above: the nextest
  grant from `<source_root>/.gobby/project.json`, the spawn and sandbox
  cache variables with their directories, `CARGO_TARGET_DIR`, the project's
  own write paths, `sandbox_write_paths`, and `allow_package_registries`.
  It returns the effective config and the environment additions, which 2.6
  merges into the provider environment.
- `sandbox/tables.rs` (new) holds the static tables from `sandbox_policy.py`
  and `sandbox_control_paths.py` for all five providers.
- `sandbox/paths.rs` (new) ports run-path preparation and
  `compute_sandbox_paths`, with every filesystem probe resolved against
  `PolicyInputs.home` and `PolicyInputs.env`, never the process
  environment. It ports `assert_sensitive_path_contract` and
  `assert_credential_read_contract` and fails with their messages.
- `sandbox/prewarm.rs` (new) ports the pre-commit prewarm and its spare,
  with the spare under the node's managed execution root and one
  replenisher at a time. It runs only when the payload's
  `prewarm_pre_commit_store` is true.
- `sandbox/settings.rs` (new) ports `render_srt_settings` for `srt`, and
  `ClaudeSandboxResolver.build_settings` with
  `preflight_provider_native_settings` for `provider-native`. It serializes
  both with sorted keys and no spaces, as `prepare_sandbox_launch` hashes
  them.
- `crates/gdaemon/tests/fixtures/node_srt/corpus.json` (new) holds cases,
  each with its inputs, a home tree (relative paths to create), and the
  expected settings bytes and run environment, with `{home}`,
  `{gobby_home}`, `{workspace}` and `{run_root}` placeholders. Cases: each of
  claude, codex, grok, droid and agy on `srt` with an empty domain list; one
  with `allowed_domains`, `allow_git_network` and `allow_package_registries`;
  one with `extra_write_paths`; one with a declared nextest store; one with
  an RTK state directory; and claude on `provider-native`.
- `tests/agents/test_node_srt_corpus.py` (new) builds each case's tree under
  `tmp_path`, runs the Python oracle (`sandbox_config_for_spawn`, then the
  path and settings chain) with that home and environment, and compares the
  placeholder form with the corpus. With `GOBBY_RECORD_NODE_SRT_CORPUS=1` it
  rewrites the corpus instead.

Verification planned:
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_node_srt_corpus.py -v`.
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.4.1 - The Python oracle's output for every corpus case equals the committed corpus. test: `tests/agents/test_node_srt_corpus.py::test_corpus_matches_python`.
- 2.4.2 - The Rust policy produces each case's settings bytes and run environment exactly, for all five providers on `srt` and for claude on `provider-native`. test: `crates/gdaemon/tests/node_agents.rs::srt_settings_match_python_corpus`.
- 2.4.3 - The payload's sandbox domains and capabilities pass through unchanged, `allow_package_registries` is forced true, the nextest store is granted only when the source checkout declares it, and the cache variables, `CARGO_TARGET_DIR` and the hook inbox become write paths. test: `crates/gdaemon/tests/node_agents.rs::srt_spawn_time_grants`.
- 2.4.4 - An `extra_write_paths` entry inside a sensitive or credential root is refused with the Python contract's message, and `provider-native` for any provider other than claude is refused. test: `crates/gdaemon/tests/node_agents.rs::srt_policy_refuses_sensitive_grants`.
- 2.4.5 - With the flag set and a `.pre-commit-config.yaml`, the run gets a writable pre-commit store from the spare or a clone, a failed clone only warns, and without the flag or the config nothing is copied. test: `crates/gdaemon/tests/node_agents.rs::pre_commit_store_prewarm`.

### 2.5 SRT install verification and the launch chokepoint [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/mod.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/runtime.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/grok_home.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/claude_home.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/ps_shim.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Granularity:** one leaf with seven acceptance items. They are the steps of
one chokepoint (ROADMAP decision 6) in the Python order. The provider homes
and the `ps` shim each add paths to the policy the chokepoint hashes and
verifies, so none of them can ship apart from the hash it changes.

**Research context:**
- ROADMAP decision 6: SRT wrapping has one chokepoint.
- `src/gobby/agents/srt_runtime.py::prepare_sandbox_launch`, after the policy
  (2.4):
  - It refuses `allow_network` on `srt` ("SRT does not accept unrestricted
    network access"), and refuses grok on Linux.
  - `provider-native` returns the provider's own arguments: claude's
    `--settings <json>` from 2.4, checked by `_preflight_provider_native`,
    with the run environment and no wrapping (`enforced: true`).
  - For grok on `srt` it prepares a private `GROK_HOME`
    (`src/gobby/agents/grok_sandbox_home.py::prepare_grok_sandbox_home`) and
    adds its runtime write paths and protected deny paths.
  - For claude on `srt` with `agent_name` `task-close-reviewer`, it prepares
    a private reviewer home
    (`src/gobby/agents/claude_sandbox_home.py::prepare_claude_reviewer_home`,
    with `auto_approve` true because spawned agents always auto-approve):
    - `<XDG_CACHE_HOME of the run>/claude` (0700) holds a seeded
      `.claude.json` with onboarding done, bypass accepted, the user's
      `mcpServers` (each `headersHelper` wrapped to run from the user's
      config dir), and the workspace's project entry with trust accepted;
    - copies of `settings.json` and `settings.local.json` (0600);
    - a `projects/<encoded workspace>` symlink to the host transcript
      directory, which must stay under `~/.claude/projects`;
    - `CLAUDE_CONFIG_DIR`, `XDG_STATE_HOME` and
      `CLAUDE_SECURESTORAGE_CONFIG_DIR` in the environment, with the control
      files as protected deny paths. Any symlinked source is refused.
  - For droid on `srt` on macOS it adds the `ps` shim
    (`src/gobby/agents/droid_ps_shim.py`, TODO(#22407)): a copy of `/bin/ps`
    at `<gobby_home>/runtime/droid-ps-shim/<sha256 of /bin/ps>/ps`, mode
    0755, ad-hoc signed with `codesign -f -s -` (30-second timeout), staged
    and renamed into place. Its directory joins the read paths and is
    prepended to `PATH`. A failure only warns and the launch goes on without
    it.
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
  With the sandbox disabled, or on `provider-native`, it returns the argv
  unchanged.
- Ruling 8: `gobby install` places the pinned runtime; gdaemon verifies and
  runs it and never installs.

**Implementation:**
- `sandbox/runtime.rs` (new) defines the chokepoint
  `prepare_launch(inputs, prompt_file) -> Result<SandboxLaunch, SandboxError>`
  and `SandboxLaunch::wrap(argv)`. 2.6 builds no other launch path. With
  `enabled` false it returns an unenforced launch whose `wrap` is the
  identity. On `provider-native` it returns claude's arguments and
  environment after the preflight. Otherwise it performs the `srt` steps
  above in the Python order, and each refusal names the Python lockout
  reason.
- `SRT_VERSION` is a constant. A test reads
  `src/gobby/utils/dependency_requirements.py` with `include_str!` and asserts
  that `SRT_RELEASE` pins the same version.
- Verification hashes the install off the async runtime
  (`spawn_blocking`) under the same lock file the Python verifier uses
  (`srt_install_lock`).
- `sandbox/grok_home.rs` (new) ports `prepare_grok_sandbox_home` and
  `_declare_gobby_mcp_server`.
- `sandbox/claude_home.rs` (new) ports `prepare_claude_reviewer_home`.
- `sandbox/ps_shim.rs` (new) ports `droid_ps_shim_dir` and
  `ps_shim_path_env`, with the #22407 removal note.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.5.1 - A verified install yields the wrapped argv `[node, runner, --settings, <settings>, --violations, <violations>, --, <provider executable>, ...]` with both files written 0600, and a disabled sandbox leaves the argv unchanged. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_wraps_once`.
- 2.5.2 - A content-manifest mismatch, a missing install, a failed preflight or `allow_network` on `srt` refuses the launch. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_refuses_unverified_install`.
- 2.5.3 - Grok gets a private `GROK_HOME` with its write and deny paths, and is refused on Linux. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_grok_home`.
- 2.5.4 - The Rust SRT version pin equals the Python `SRT_RELEASE` version. test: `crates/gdaemon/tests/node_agents.rs::srt_version_matches_python_pin`.
- 2.5.5 - A claude `task-close-reviewer` on `srt` gets a private config home with the seeded `.claude.json`, the copied settings, the transcript symlink and the three variables, and a symlinked source is refused. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_claude_reviewer_home`.
- 2.5.6 - Droid on `srt` on macOS gets the signed `ps` copy first on `PATH` and in the read paths, and a failed copy or signature leaves the launch running without it. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_droid_ps_shim`.
- 2.5.7 - Claude on `provider-native` launches unwrapped with its `--settings` JSON after the preflight, and a policy missing a sensitive root is refused. test: `crates/gdaemon/tests/node_agents.rs::chokepoint_provider_native_claude`.

### 2.6 Provider launch, exits and host reconciliation [category: code] (depends: 2.5, 2.12)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/launch.rs`
- `crates/gdaemon/src/nodes/agents/provider_command.rs`
- `crates/gdaemon/src/nodes/agents/provider_env.rs`
- `crates/gdaemon/src/nodes/agents/trust.rs`
- `crates/gdaemon/src/nodes/agents/agy_gate.rs`
- `crates/gdaemon/tests/fixtures/node_launch/corpus.json`
- `tests/agents/test_node_launch_corpus.py`
- `crates/gdaemon/tests/node_agents.rs`

**Granularity:** one leaf with nine acceptance items across six production
files. 2.6 owned the provider command and the launch at the first consensus;
this revision widens the command to five providers and the version 2 inputs.
The command, its environment, the trust seed and the agy gate have one
consumer, `launch`, and the exit watcher and reconcile act on the launched
record `launch` commits. The corpus proves the pure parts against Python.
`Cargo.toml` gains `toml_edit` for the codex trust store, after 2.12 adds
its own dependencies to the same file.

**Research context:**
- The Python provider plans are the oracle
  (`src/gobby/agents/spawn_executor_providers.py`: `prepare_claude_spawn`,
  `prepare_codex_spawn`, `prepare_grok_spawn`, `prepare_droid_spawn`,
  `prepare_agy_spawn`), with argv from
  `src/gobby/agents/spawners/command_builder.py::build_cli_command`:
  - claude: `--resume <id>` or `--session-id <session_id>`, `--model`,
    `--effort`, `--dangerously-skip-permissions`; `--mcp-config <cwd>/.mcp.json --strict-mcp-config`
    when that file exists; under SRT, `--settings '{"sandbox": {"enabled": false}}'`;
    then the payload's `provider_args`, then the prompt.
  - codex: `exec` when headless, else `resume` with a resume id; `--oss --local-provider <provider>`
    and `-m` with `codex_oss_provider`, else `--model`;
    `-c model_reasoning_effort="<effort>"`; headless uses
    `--dangerously-bypass-approvals-and-sandbox`, which requires an
    enforced sandbox, and the terminal UI uses
    `--ask-for-approval never --disable guardian_approval`; `-C <cwd>`; each
    config override as `-c`; `-c check_for_update_on_startup=false`; the
    resume id last. Only a headless launch puts the prompt on the argv
    (`src/gobby/agents/provider_capabilities.py::codex_launches_headless`:
    agent `task-close-reviewer` on an enforced `srt` sandbox). Every other
    codex launch gets its prompt typed by 2.10.
  - grok: `--always-approve --no-alt-screen --cwd <cwd>`, `--model`,
    `--reasoning-effort`, `--resume <id>`, and `--single` unless the mode is
    `terminal`, which `execution_mode` `"interactive"` selects.
  - droid: `exec` unless `droid_mode` is `"interactive"`; `--session-id`
    (exec) or `--resume` (interactive) with a resume id; `--cwd` (exec);
    `--model`; `--reasoning-effort`; `--auto high` (exec); the prompt last.
  - agy: `--conversation <id>`, `--dangerously-skip-permissions`,
    `--add-dir <cwd>`, `--model`, `--effort`, `--print <prompt>`; and
    `--sandbox=false` under an enforced `srt` sandbox.
- Codex config overrides, in order: `src/gobby/agents/spawn_executor_support.py::_codex_mcp_config_overrides`
  (the `gobby` MCP server, which gmcp-stdio 1.6 turns into
  `<gobby_home>/bin/gmcp` with no arguments (Decision 24), `required=true`,
  `startup_timeout_sec=120`, the tool timeout, `_codex_runtime_config_overrides`
  for `TMPDIR`, `TMPPREFIX`, `OCR_NO_UPDATE`, `GOBBY_HOME`,
  `GOBBY_DAEMON_URL`, `GOBBY_MACHINE_ID`, the identity variables, the spawn
  cache variables and `CARGO_TARGET_DIR`, and the preapproved tools); then
  the payload's `codex_config_overrides`; then, for `task-close-reviewer`,
  `features.plugins=false`, `features.remote_plugin=false`, the computer-use
  server disabled and `mcp_servers.node_repl.enabled=false`; then
  `sandbox_mode="danger-full-access"` under an enforced `srt` sandbox.
- Provider environment:
  - The generation endpoint: claude `ANTHROPIC_BASE_URL` and
    `ANTHROPIC_AUTH_TOKEN`, codex `OPENAI_BASE_URL` and `OPENAI_API_KEY`,
    grok `GROK_API_BASE` and `XAI_API_KEY`, droid `FACTORY_API_BASE_URL` and
    `FACTORY_API_KEY`, from `api_base` and `api_token`; agy takes none.
  - `_apply_extra_env` applies the payload's `extra_env` after removing
    `GOBBY_AGENT_API_TOKEN`: `PATH` merges through `merge_spawn_path_env`, and
    keys in `_RESERVED_EXTRA_ENV_KEYS` (`ALL_TERMINAL_ENV_VARS`,
    `SPAWN_CACHE_ENV_VARS`, `GOBBY_MACHINE_ID`) are skipped with a warning.
  - claude: `CLAUDE_MANAGED_AGENT_ENV`, `MCP_TIMEOUT=120000` and, with strict
    MCP, `MCP_CONNECT_TIMEOUT_MS` (`src/gobby/agents/constants.py`); a
    `task-close-reviewer` under SRT adds
    `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and
    `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1`.
  - The contract's environment: the seven identity variables,
    `GOBBY_DAEMON_URL` set to the node's own front door, `GOBBY_PROMPT_FILE`
    set to a node-local file holding `prompt`, and 2.4's cache variables.
  - The native host applies `src/gobby/terminals/native_env_policy.py::apply_native_env_policy`
    to every spawn: allowlisted ambient values
    (`src/gobby/agents/spawners/auth_env.py`, `UNIVERSAL_ALLOWLIST` and
    `CLI_ENV_ALLOWLIST`) are added when absent, the provider's denied ambient
    keys (`src/gobby/agents/credential_inventory.py::denied_ambient_keys`)
    are removed and unset through `/usr/bin/env -u <key> --`,
    `VIRTUAL_ENV` and `VIRTUAL_ENV_PROMPT` are blanked, and a managed
    bootstrap also unsets `GOBBY_AGENT_API_TOKEN`.
- Trust: `src/gobby/agents/trust.py::pre_approve_directory` seeds the
  workspace and its real path: claude in `~/.claude.json`
  (`_pre_approve_claude`, under the `update_claude_config` lock), codex as
  `[projects."<path>"] trust_level = "trusted"` in `~/.codex/config.toml`
  (`_seed_codex_trust`, which prunes stale generated entries and keeps the
  rest of the file). Droid, grok and agy have no trust store.
- The agy gate (`src/gobby/providers/version_gate.py::ensure_agy_support`):
  the `agy` on `PATH` is probed with `agy --version` (10-second timeout,
  `src/gobby/servers/provider_model_discovery.py::get_cli_version`), the
  first `v?(\d+\.\d+\.\d+)` is the installed version, and it must be at
  least `AGY_REQUIRED_VERSION` (1.1.18). The record is cached by the
  binary's real path, device, inode, size and modification time, and a
  binary that changes during the probe is probed again. An unsupported
  record refuses the spawn with its reason
  (`agy_support_refusal`), for example "Installed Antigravity version none
  does not meet required version 1.1.18.". After this change hub-mode 2.5
  applies the hub's gate only to a local spawn, so the node gates its own
  binary.
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
- The run timeout and the initialization timeout are observed by 2.14 and
  decided by the hub (Decision 30), so the launch arms no timer.

**Implementation:**
- `crates/gdaemon/Cargo.toml` adds `toml_edit` (already in `Cargo.lock`).
- `nodes/agents/provider_command.rs` (new): `build_command(payload, cwd, sandbox_launch)`
  ports `build_cli_command` for the five providers with the payload's
  `model`, `reasoning`, `resume_session_id`, `execution_mode`, `droid_mode`,
  `codex_oss_provider` and `agent_name`, the codex overrides above, and the
  prompt rules above.
- `nodes/agents/provider_env.rs` (new): `build_env(payload, front_door_url, prompt_file, cache_env, sandbox_launch)`
  assembles the environment above in the Python order and applies the
  native env policy, returning the final argv prefix and environment.
  `api_token` enters only this environment (Decision 26), and no log line
  prints the environment.
- `nodes/agents/trust.rs` (new) ports the claude and codex trust seeds. A
  failure only warns, as in Python.
- `nodes/agents/agy_gate.rs` (new) ports the agy gate with its cache and
  reasons.
- `nodes/agents/launch.rs` (new): `launch(agents, run_id, payload, cwd)`:
  1. For agy, check the gate; an unsupported record is a `prepare_failed`
     exit whose reason is the record's reason.
  2. Seed trust for the provider.
  3. Write the prompt file `<gobby_home>/node/prompts/<run_id>.md` (0600).
  4. Build the command and environment, call 2.5's `prepare_launch` and
     wrap the argv.
  5. `spawn` with `spawn_key = run_id`, then `spawn_commit` with 30,000 ms.
  6. Commit the run's `launched` phase with `host_terminal_id`.
  A failure before `spawn_commit` calls `spawn_abort` when a child was
  prepared and returns `launch_failed`.
- `watch_exits(agents)` subscribes to host events. On `terminal_exited` for
  a launched run, it commits an exit `{launched: true, exit_code, reason, workspace}`
  and removes the run. The reason is `cancelled` when a cancel record exists
  for the run, else `exited`. It reconciles once when it subscribes, and
  again on an event gap, a reconnect or a host epoch change.
- `reconcile(agents)` lists the inventory from the live host. A launched
  run becomes an `exited` exit with a null `exit_code` only when its
  `terminal_id` is absent from that inventory; a present run stays live.
- `crates/gdaemon/tests/fixtures/node_launch/corpus.json` (new) holds cases
  for `build_cli_command`, the codex overrides and `apply_native_env_policy`:
  each provider in each of its modes, with and without a resume id, codex
  headless and with `codex_oss_provider`, and a fixed ambient environment.
  `tests/agents/test_node_launch_corpus.py` (new) runs the Python functions
  on each case and compares; with `GOBBY_RECORD_NODE_LAUNCH_CORPUS=1` it
  rewrites the corpus.
- `serve::run` starts `watch_exits` through 2.8's registration, so this
  section adds no serve change.

Verification planned:
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_node_launch_corpus.py -v`.
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.6.1 - The Python command builder, codex overrides and native env policy produce the committed launch corpus. test: `tests/agents/test_node_launch_corpus.py::test_corpus_matches_python`.
- 2.6.2 - The Rust command and environment for each corpus case equal the corpus, for claude, codex, grok, droid and agy. test: `crates/gdaemon/tests/node_agents.rs::provider_commands_match_python`.
- 2.6.3 - Each launch carries the seven identity variables, `GOBBY_DAEMON_URL`, `GOBBY_PROMPT_FILE`, 2.4's cache variables and the provider's endpoint variables, applies `extra_env` with `PATH` merged and reserved keys skipped, and never writes `api_token` to disk or a log. test: `crates/gdaemon/tests/node_agents.rs::provider_env_layers`.
- 2.6.4 - A codex launch puts its prompt on the argv only as a `task-close-reviewer` on an enforced `srt` sandbox, and otherwise launches the terminal UI with no prompt for 2.10 to type. test: `crates/gdaemon/tests/node_agents.rs::codex_prompt_placement`.
- 2.6.5 - An agy binary below 1.1.18, absent or unparseable refuses the spawn as `prepare_failed` with the gate's reason, and a changed binary is probed again. test: `crates/gdaemon/tests/node_agents.rs::agy_version_gate`.
- 2.6.6 - Claude and codex launches seed trust for the workspace and its real path, keeping the rest of each store, and a failed seed only warns. test: `crates/gdaemon/tests/node_agents.rs::launch_seeds_provider_trust`.
- 2.6.7 - A launch spawns through the chokepoint with `spawn_key` equal to `run_id`, commits, and records the run as launched with its host terminal id. test: `crates/gdaemon/tests/node_agents.rs::launch_spawns_and_commits`.
- 2.6.8 - A host exit event produces an `exited` exit with the exit code and workspace, or `cancelled` when a cancel record exists. test: `crates/gdaemon/tests/node_agents.rs::exit_events_record_exits`.
- 2.6.9 - `reconcile` turns a launched run whose terminal is absent from the live host's inventory into an `exited` exit with a null exit code and keeps a present one live, both for runs reloaded from durable state after a restart and after a host epoch change. test: `crates/gdaemon/tests/node_agents.rs::reconcile_after_restart`.

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
  `EmptyResult`. The models accept any `v` of at least 1, so a refusal can
  name its request. 3.1 adds `CONTRACT_VERSION` (2), and the handler
  refuses every other `v`.
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
    can name its request. A `v` other than `CONTRACT_VERSION` answers
    `unsupported_version`, an unknown command `unknown_command`, and a
    payload that fails to parse, names a provider other than claude, codex,
    grok, droid or agy, or names a
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
       `prepare_timeout` (deadline) or `launch_failed` (host). Each passes
       the workspace record through 2.1's `to_removals`, runs `remove` once
       on a record it moved, and commits a `launched: false` exit, so a
       resume's reused workspace stays on disk.
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

- 2.7.1 - A `v` other than `CONTRACT_VERSION`, unknown commands and invalid payloads, including an unknown provider, answer their codes with `stage` `"none"` and the frame's `request_id`, and record nothing. test: `crates/gdaemon/tests/node_agents.rs::dispatch_rejects_bad_frames`.
- 2.7.2 - A spawn is recorded and answered `{accepted: true}` before preparation starts, then prepared and launched; a repeated spawn for a known run with no cancel record answers the same and records nothing new. test: `crates/gdaemon/tests/node_agents.rs::spawn_accepts_before_preparing`.
- 2.7.3 - A spawn at 1,000 accepted and live runs answers `command_failed` with `"none"` and records nothing. test: `crates/gdaemon/tests/node_agents.rs::spawn_refused_at_capacity`.
- 2.7.4 - A cancel before launch records the cancel, stops preparation, removes a prepared workspace, reports a `cancelled` exit with `launched: false` and answers `{stopped: true}`; a later spawn answers `cancelled`, and a cancel for an unknown run answers `not_found`. test: `crates/gdaemon/tests/node_agents.rs::cancel_before_launch`.
- 2.7.5 - A cancel of a launched run sends the requested signal and reports a `cancelled` exit when the agent ends, and a spawn for that run before its exit answers `cancelled`. test: `crates/gdaemon/tests/node_agents.rs::cancel_launched_run`.
- 2.7.6 - A preparation failure, the preparation deadline and a host launch failure report `prepare_failed`, `prepare_timeout` and `launch_failed` with `launched: false`, and remove a prepared workspace while a reused one stays on disk. test: `crates/gdaemon/tests/node_agents.rs::preparation_failures_report_exits`.
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
    1. Build the body: `v: CONTRACT_VERSION`, the stored `hub_epoch`, `next_revision()`,
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
     spawn a task that posts the registration, with `v` equal to
     `CONTRACT_VERSION`, through `HubRelay` with the node key.
  3. On a 2xx, add the root to the set. A failure leaves it out, so the next
     hook retries.
- `ingress::execute` calls `observe_hook_cwd` before it forwards a hook, and
  does not wait for it.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents) + binary(node_hooks)'`.

**Acceptance:**

- 2.9.1 - The first hook from a project root registers that root once with the project id, even when the hub refuses the hook itself, and later hooks from it post nothing. test: `crates/gdaemon/tests/node_agents.rs::checkout_registered_on_first_hook`.
- 2.9.2 - A failed registration is retried on the next hook, and roots under the node's worktrees and clones are never registered. test: `crates/gdaemon/tests/node_agents.rs::checkout_registration_retries_and_skips_workspaces`.

### 2.10 Codex prompt delivery [category: code] (depends: 2.9, 3.5)
`kind: deliverable`

Targets:
- `crates/gterminals/src/control.rs::*` — scope-reason: adds `ControlClient::snapshot` and its `Snapshot` reply type; the index lists `ControlClient`'s methods unqualified
- `crates/gterminals/tests/control_snapshot.rs`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/codex_prompt.rs`
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- Codex 0.147 and later cancels MCP startup when a first turn begins
  (#19953), so a codex terminal launch carries no prompt and the daemon
  types it once the composer renders
  (`src/gobby/agents/spawn_executor_support.py::_deliver_codex_prompt`,
  scheduled by `schedule_codex_prompt_delivery`). 2.6 puts the prompt on the
  argv only for a headless reviewer.
- The constants in `spawn_executor_support.py`: the composer marker `›`, a
  40-line capture, a 1-second poll, a 60-second ready window followed by a
  45-second retry window, a 5-second probe timeout, a 1-second settle, and a
  3-second delay before Enter.
- The flow:
  - Poll a 40-line text snapshot until the marker shows. Between polls,
    check that the terminal is live.
  - A terminal that stops being live fails with
    `codex_terminal_exited_before_composer`. Passing the deadline fails with
    `codex_composer_not_ready`, whose detail counts captures, snapshot errors
    and liveness errors.
  - After the marker, wait the settle second, write the prompt as text, wait
    3 seconds, and send the `enter` key, under one action key
    `codex-prompt:<run_id>`.
  - An indeterminate write only logs, because the prompt may have landed. A
    refused or failed write fails with `codex_prompt_delivery_failed`.
- The failure (`_fail_codex_prompt_delivery`) calls
  `fail_uninitialized_prompt_delivery(run_id, error=)`. With no transition
  (the child registered first) it stops. Otherwise it terminates the
  terminal with a 5-second grace and calls the cleanup handler with
  `preterminalized_run`. If the failure cannot be persisted, it only logs,
  and the initialization timeout later covers the run.
- The error text is `_codex_prompt_failure_reason(pane, code=, detail=)`: the
  pane redacted by `redact_terminal_output`, cut to a 1,024-character tail
  that starts with `[truncated]\n`. No Rust terminal redactor exists, so the
  node sends the raw pane and the hub builds the error (3.5).
- The Rust `ControlClient` (`crates/gterminals/src/control.rs`) has `list`,
  `spawn`, `spawn_commit`, `spawn_abort`, `kill` and `write`, and no
  `snapshot`. The host serves `snapshot`
  (`crates/gterminal/src/host/control.rs`): the request is
  `{host_terminal_id, mode, max_bytes, max_lines}` and the reply is
  `{ok, mode, text, truncated, dropped_bytes, total_bytes}`. The Python
  client (`src/gobby/terminals/host_client.py`, `snapshot`) refuses a reply
  whose `mode` differs from the request with `snapshot_mode_mismatch`.

**Implementation:**
- `control.rs` adds `ControlClient::snapshot(host_terminal_id, mode, max_bytes, max_lines) -> Result<Snapshot, ControlError>`
  and `Snapshot { mode, text, truncated, dropped_bytes, total_bytes }`. A
  reply whose `mode` differs from the request is refused with
  `snapshot_mode_mismatch`, as in Python.
- `nodes/agents/codex_prompt.rs` (new) defines `deliver(node, run_id, prompt)`.
  It ports the flow above with the same constants:
  - Snapshots use `text` mode, 40 lines and 262,144 bytes.
  - Liveness is the run's `terminal_id` present in the host's `list()`.
  - The prompt goes through `ControlClient::write`, first as `text` with
    `submit` false, then as the `enter` key.
  - A write that fails after the request was sent is indeterminate and only
    logs. A refusal before sending is `codex_prompt_delivery_failed`.
  - A cancel record for the run stops delivery with no post.
- On a failure the node posts
  `POST /api/nodes/runs/prompt-delivery-failure {v, run_id, code, detail, pane}`
  (3.1, 3.5) through `HubRelay` with the node key. `pane` is the last
  non-empty snapshot text. The node never signals the run: the hub fails
  it and kills it through `agent.cancel` (Decision 30). A transport failure
  or a non-2xx answer only logs, as Python does when it cannot persist the
  failure, and 2.14's initialization timeout then covers the run.
- `commands.rs`: after the launched commit (2.6), a codex run whose command
  carries no prompt starts `deliver` as its own task, outside the
  300-second preparation deadline, so a cancel takes the launched branch.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-terminals -E 'binary(control_snapshot)'`.
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.10.1 - `ControlClient::snapshot` sends the host request and returns its reply, and a reply whose `mode` differs from the request is refused with `snapshot_mode_mismatch`. test: `crates/gterminals/tests/control_snapshot.rs::snapshot_round_trips_and_checks_mode`.
- 2.10.2 - Once the composer marker shows, the node waits the settle second, writes the prompt as text, waits 3 seconds and sends Enter. test: `crates/gdaemon/tests/node_agents.rs::codex_prompt_typed_after_composer`.
- 2.10.3 - A pane that never shows the marker within the 105-second window posts `codex_composer_not_ready`, a terminal that leaves the inventory first posts `codex_terminal_exited_before_composer`, and a refused write posts `codex_prompt_delivery_failed`, each with the last non-empty pane, and no hub answer makes the node signal the run. test: `crates/gdaemon/tests/node_agents.rs::codex_prompt_failures_post_to_hub`.
- 2.10.4 - An indeterminate write only logs, a cancel record stops delivery with no post, and a headless codex launch or another provider starts no delivery. test: `crates/gdaemon/tests/node_agents.rs::codex_prompt_skips`.

### 2.11 Node code-index preflight [category: code] (depends: 2.10, 3.5)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/code_index.rs`
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Research context:**
- The hub resolves the mode with
  `src/gobby/mcp_proxy/tools/spawn_agent/_code_index.py::code_index_preflight_mode`:
  `"best_effort"` for a worktree or clone whose task category is not
  `docs`, otherwise null. It reaches the node as the payload's
  `code_index_preflight` (3.1, 3.2).
- `src/gobby/agents/code_index.py::ensure_isolation_code_index` with no
  managed credential, which a node never holds (ROADMAP decision 17), runs
  three gcode commands with the identity environment
  (`GOBBY_AGENT_RUN_ID`, `GOBBY_PROJECT_ID`, `GOBBY_SESSION_ID`,
  `GOBBY_MACHINE_ID`) and `GOBBY_AGENT_API_TOKEN` removed, inside a
  120-second budget:
  - `gcode status --quiet --format json --allow-stale --project <workspace>`
    (5 seconds);
  - `gcode index --quiet --project <workspace>` (the rest of the budget);
  - `gcode search-content __gobby_code_index_smoke__ --limit 1 --quiet --allow-stale --project <workspace>`
    (10 seconds).
- Its errors are `gcode_not_installed`, `gcode_index_workspace_missing:<path>`,
  `gcode_index_timeout:deadline_exceeded`, `<timeout code>:<n>s` and
  `<failure code>:<exit code>:<preview>`. The codes per step are
  `gcode_index_unavailable` and `gcode_index_unavailable_timeout` (status),
  `gcode_index_failed` and `gcode_index_timeout` (index), and
  `gcode_search_content_failed` and `gcode_search_content_timeout` (smoke).
  `_process_detail` and `_preview_process_detail` redact PostgreSQL URLs,
  collapse whitespace and cut a 500-character preview with `[truncated]\n`.
- On failure, `src/gobby/agents/spawn_executor_providers.py::_prepare_managed_code_index`
  appends `_append_code_index_warning`'s text to the prompt and merges the
  session variables: `additional_skills` without
  `gobby:references/code-index/overview.md`,
  `code_index_preflight_warning`, and the new prompt. A failed merge fails
  the spawn.
- On a node, gcode reaches the hub's code-index routes, which #21586
  (Migrate code-index orchestration and shared dependencies) adds, through
  the node's front door relay, because `GOBBY_DAEMON_URL` names the node's
  front door (Decision 21).

**Implementation:**
- `nodes/agents/code_index.rs` (new) defines
  `preflight(workspace, identity, front_door_url) -> Result<(), CodeIndexWarning>`.
  It ports the three runs, their deadlines, the error strings and the detail
  preview. `CodeIndexWarning { preflight, cwd, message }` carries the
  Python warning fields.
- `commands.rs`: when the payload's `code_index_preflight` is
  `"best_effort"`, the preparation task runs `preflight` after 2.3's
  `prepare` and before 2.6's `launch`, inside the 300-second deadline. On a
  failure it appends the warning text to the prompt, as
  `_append_code_index_warning` does, and posts
  `POST /api/nodes/runs/code-index-warning {v, run_id, warning, prompt}`
  (3.5). A transport failure or a non-2xx answer is `prepare_failed`, as a
  failed merge fails a Python spawn. The persona prefix is already at the
  front of the prompt (3.2), and the warning goes at the end, so the result
  matches the Python order.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.11.1 - With the mode set, the three gcode runs use the workspace, the identity environment and the node's front door within their timeouts and the 120-second budget, with no managed credential. test: `crates/gdaemon/tests/node_agents.rs::code_index_preflight_runs_gcode`.
- 2.11.2 - Each failure produces the Python error string, appends the warning to the prompt the provider receives and posts it with that prompt, and a refused post fails the spawn as `prepare_failed`. test: `crates/gdaemon/tests/node_agents.rs::code_index_failure_warns_and_posts`.
- 2.11.3 - Without the mode, no gcode command runs. test: `crates/gdaemon/tests/node_agents.rs::code_index_preflight_skipped_without_mode`.

### 2.12 Detection manifest engine [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/pane_detection/mod.rs`
- `crates/gdaemon/src/pane_detection/manifest.rs`
- `crates/gdaemon/src/pane_detection/matcher.rs`
- `crates/gdaemon/tests/fixtures/pane_detection/corpus.json`
- `crates/gdaemon/tests/pane_detection_corpus.rs`
- `tests/agents/test_pane_detection_corpus.py`

**Research context:**
- The five bundled manifests are
  `src/gobby/install/shared/detection/{agy,claude,codex,droid,grok}.toml`.
  They sync to the `detection_manifests(provider_id, content)` table, and
  `src/gobby/agents/detection/registry.py::DetectionManifestRegistry`
  rereads it every 30 seconds.
- The engine is `src/gobby/agents/detection/`:
  - `schema.py`: engine version 1; states `blocked`, `idle`, `working` and
    `stall`; the region `bottom_non_empty_lines(N)` with N at most 200
    (`MAX_BOTTOM_NON_EMPTY_LINES`).
  - `matcher.py`: the issues `invalid_pattern` and `pattern_timeout`, the
    composer region, `_last_prompt_box`, and the codex composer patterns.
  - `safe_regex.py`: `MAX_PATTERN_SIZE` 2,048; Python's `regex` with
    `MULTILINE` and a CPU budget.
- `droid.toml` uses a negative lookahead, so the Rust engine is fancy-regex
  (Decision 29). fancy-regex 0.11 and `toml` are in `Cargo.lock`.
- Python tests: `tests/agents/detection/test_matcher.py` loads
  `tests/agents/fixtures/detection_manifest.toml` and `pane_approval.txt`;
  `tests/agents/detection/test_agents_detection_schema.py` and
  `test_safe_regex.py` cover the schema and the regex limits;
  `tests/agents/detection_test_support.py::BundledDetectionRegistry` loads
  the bundled manifests.
- Corpus precedent: `crates/gdaemon/tests/fixtures/node_commands/corpus.json`
  holds golden messages that Python and Rust both check.
- Decision 5 places the module outside `nodes/` so the hub branch of the
  same binary can reuse it (#24149).

**Implementation:**
- `crates/gdaemon/Cargo.toml` adds `fancy-regex = "0.11"` and `toml`.
- `crates/gdaemon/src/lib.rs` declares `pub mod pane_detection;`.
- `pane_detection/manifest.rs` (new) parses and validates a manifest as
  `schema.py` does, refusing with the Python messages.
- `pane_detection/matcher.rs` (new) compiles each rule, dropping a pattern
  over 2,048 characters or one that fails to compile with an
  `invalid_pattern` issue, extracts regions, and matches. A search past the
  backtrack limit is a `pattern_timeout` issue and reads as no match
  (Decision 29).
- `pane_detection/mod.rs` (new) defines `ManifestSet::from_rows(rows)` over
  `(provider_id, content)` pairs, which 2.13 and 2.14 use.
- `crates/gdaemon/tests/fixtures/pane_detection/corpus.json` (new) holds
  matcher cases: a provider or a fixture manifest, a pane, and the expected
  state, rule id and issues. It includes the cases of `test_matcher.py`, the
  `pane_approval.txt` fixture, one positive and one negative pane for each
  bundled rule, each schema refusal, an oversize pattern and a
  backtracking pattern.
- `tests/agents/test_pane_detection_corpus.py` (new) runs the Python matcher
  on each case with the bundled registry and compares. With
  `GOBBY_RECORD_PANE_DETECTION_CORPUS=1` it rewrites the corpus instead.

Verification planned:
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_pane_detection_corpus.py -v`.
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(pane_detection_corpus)'`.

**Acceptance:**

- 2.12.1 - The Python matcher's output for every matcher case equals the committed corpus. test: `tests/agents/test_pane_detection_corpus.py::test_matcher_cases_match_python`.
- 2.12.2 - The Rust matcher produces each case's state, rule id and issues exactly. test: `crates/gdaemon/tests/pane_detection_corpus.rs::matcher_matches_python_corpus`.
- 2.12.3 - Every rule in the five bundled manifests compiles under fancy-regex, including droid's lookahead. test: `crates/gdaemon/tests/pane_detection_corpus.rs::bundled_manifests_compile`.
- 2.12.4 - Schema refusals carry the Python messages, an oversize or uncompilable pattern drops its rule with `invalid_pattern`, and a backtracking search reports `pattern_timeout` as no match. test: `crates/gdaemon/tests/pane_detection_corpus.rs::manifest_refusals_and_issues`.

### 2.13 Pane detectors [category: code] (depends: 2.12)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/pane_detection/mod.rs`
- `crates/gdaemon/src/pane_detection/prompt.rs`
- `crates/gdaemon/src/pane_detection/idle.rs`
- `crates/gdaemon/src/pane_detection/stall.rs`
- `crates/gdaemon/src/pane_detection/loop_tracker.rs`
- `crates/gdaemon/src/pane_detection/quota.rs`
- `crates/gdaemon/src/pane_detection/attention.rs`
- `crates/gdaemon/tests/fixtures/pane_detection/corpus.json`
- `crates/gdaemon/tests/pane_detection_corpus.rs`
- `tests/agents/test_pane_detection_corpus.py`

**Granularity:** one leaf across seven production files with five
acceptance items. The files are the pure detector layer over 2.12's
engine, ported module for module. One corpus and its two parity tests
prove them together, and 2.14 is their only consumer, so a split would
share every test target across leaves.

**Research context:**
- `src/gobby/agents/prompt_detector.py` (319 lines):
  `detect_trust_prompt`, `detect_loop_prompt`, `detect_approval_prompt`,
  `detect_queued_message_prompt`, `detect_queued_continuation_prompt` and
  `detect_prompt`, the dismiss keys (`trust_dismiss_keys`), and the
  per-run `was_dismissed` and `mark_dismissed` state.
- `src/gobby/agents/idle_detector.py` (389 lines): `detect()` is stateless
  and reads the status (`unknown`, `context_full`, `idle` or `active`),
  `turn_in_flight`, `unsubmitted_input`, `queued_message_prompt` and the
  composer (`ComposerRead`, probing 40 lines). A pane read is `empty`,
  `draft` or `unknown`; only the hub's composer ledger reads `held`. The
  counters (`reset_idle`, `should_reprompt`, `should_fail`) stay hub-side
  (3.8).
- `src/gobby/agents/stall_classifier.py` (227 lines): attention
  classification and the provider-stall verdict with per-run
  `consecutive_hits`.
- `src/gobby/agents/loop_tracker.py` (44 lines): `LoopTracker(threshold=3)`.
- `src/gobby/agents/watchdog/quota.py` (52 lines) parses codex's quota
  message and reset time, and `src/gobby/agents/watchdog/codex.py` defines
  `CODEX_MODEL_CAPACITY_MESSAGE`.
- `src/gobby/agents/attention_tracker.py::AgentAttentionTracker.sync`
  classifies a pane with the prompt detector and the stall classifier into
  a reason, kind, fingerprint, payload and dismissed flag, then persists it.
  3.3 splits the two halves on the hub.
- Python tests with inline panes: `tests/agents/test_prompt_detector.py`
  (44 tests), `test_idle_detector.py` (75), `test_stall_classifier.py` (17),
  `test_loop_tracker.py` and `test_watchdog_quota.py`.

**Implementation:**
- Each new module ports its Python counterpart over a `ManifestSet` (2.12)
  and a pane string: `prompt.rs` (prompt detector and dismiss keys),
  `idle.rs` (idle detector and composer read), `stall.rs` (stall
  classifier), `loop_tracker.rs`, `quota.rs` (quota message and capacity
  banner), and `attention.rs` (the classify half of
  `AgentAttentionTracker.sync`). State Python keeps per run, such as
  dismissed prompts, loop counts and stall hits, lives in a `RunDetectState`
  that the caller owns (2.14).
- The corpus gains `detectors`: cases lifted from the inline panes of the
  five Python test files, each naming a provider, a pane, a detector
  function, its state before the call, and the expected result.
- `tests/agents/test_pane_detection_corpus.py` runs the Python detector for
  each case and compares, and the record flag rewrites these cases too.

Verification planned:
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_pane_detection_corpus.py tests/agents/test_prompt_detector.py tests/agents/test_idle_detector.py tests/agents/test_stall_classifier.py -v`.
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(pane_detection_corpus)'`.

**Acceptance:**

- 2.13.1 - The Python detectors produce every committed detector case. test: `tests/agents/test_pane_detection_corpus.py::test_detector_cases_match_python`.
- 2.13.2 - The Rust prompt detector's trust, loop, approval, queued-message and queued-continuation verdicts and dismiss keys equal the corpus for all five providers. test: `crates/gdaemon/tests/pane_detection_corpus.rs::prompt_detector_matches_python`.
- 2.13.3 - The Rust idle detector's status, turn, unsubmitted-input, queued-message and composer reads equal the corpus. test: `crates/gdaemon/tests/pane_detection_corpus.rs::idle_detector_matches_python`.
- 2.13.4 - The Rust stall classifier's verdicts with consecutive hits, and the attention classification's reason, kind, fingerprint, payload and dismissed flag, equal the corpus. test: `crates/gdaemon/tests/pane_detection_corpus.rs::stall_and_attention_match_python`.
- 2.13.5 - The loop tracker trips on the third dismissal, and the quota and capacity parsers equal the corpus. test: `crates/gdaemon/tests/pane_detection_corpus.rs::loop_and_quota_match_python`.

### 2.14 Node pane monitor [category: code] (depends: 2.11, 2.13, 3.6, 3.7)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the unix `run`'s `node-agent-report` tick also runs the monitor pass; two cfg-gated functions share the name `run`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/src/nodes/agents/state.rs`
- `crates/gdaemon/src/nodes/agents/launch.rs`
- `crates/gdaemon/src/nodes/agents/reports.rs`
- `crates/gdaemon/src/nodes/agents/manifests.rs`
- `crates/gdaemon/src/nodes/agents/pane_monitor.rs`
- `crates/gdaemon/src/nodes/agents/checkpoint.rs`
- `crates/gdaemon/tests/node_agents.rs`

**Granularity:** one leaf with seven acceptance items across eight
production files. The monitor is one 30-second pass over each launched run,
the pane half of the local `_check_loop`. Every item is a step of that pass
or a field it fills. The watch record and the manifest set are its inputs,
and the report and the terminate posts are its only outputs, so no step
ships alone.

**Research context:**
- The local loop (`src/gobby/agents/lifecycle_monitor.py`, `_check_loop`)
  runs every 30 seconds after a 5-second start delay. Its pane steps, in
  order: trust, loop, approval, queued continuation, periodic Enter,
  attention, initialization timeout, idle, provider stalls. Its other steps
  stay hub-side (Decision 4).
- Pane reads: 15 lines for trust, loop, approval and attention (the idle
  check reuses the attention read); 30 for queued continuation; 40 in ANSI
  mode for periodic Enter; 8 for provider stalls; 50 for the timeout error
  tail.
- `src/gobby/agents/terminal_prompt_monitor.py::TerminalPromptMonitor`
  detects each prompt and sends its keys through `_send_keys`, one action
  key per prompt (`trust-dismiss:<run_id>` and its siblings), with an
  indexed action key per key in a multi-key sequence. After a send it calls
  `mark_enter_sent`, `mark_dismissed` and `_notify_prompt_injected`, which
  clears attention after the injection. A loop prompt is dismissed at most
  once per `LOOP_PROMPT_DISMISS_MIN_INTERVAL_SECONDS` (60), and
  `LoopTracker` trips at 3, which runs `_checkpoint_and_kill_looping_agent`.
  Approval prompts are answered only with `auto_enter_approval_prompts`,
  and the periodic Enter runs only with `auto_enter_agent_terminals`, every
  `auto_enter_agent_interval_seconds`, skipped for a dialog
  (`_should_skip_periodic_enter_for_dialog`).
- The hub's automatic-write rule (`src/gobby/terminals/write_coordinator.py`,
  `_blocked_automatic`): a terminal quarantined for another action key
  refuses an automatic write, and an action key with an unresolved write is
  suppressed. Direct input the host accepts lifts the quarantine.
- `AgentHealthMonitor.check_provider_stalls`
  (`src/gobby/agents/agent_health.py`) reads 8 lines, classifies, and on a
  provider stall checkpoints the work and terminates the run with
  `Provider stall: <reason> (provider=<p>, consecutive_hits=<n>)` followed by
  the pane.
- The run timeout is the payload's `timeout_seconds`, and the
  initialization timeout is the monitor block's `init_timeout_seconds`
  (Decision 28). The hub decides both from the run's database row (3.6).
- `src/gobby/agents/checkpoint_manager.py::create_checkpoint` runs
  `git status --porcelain`, `write-tree`, `add -u`, `write-tree`,
  `rev-parse HEAD`, `commit-tree -m "gobby: auto-checkpoint for task <task_id> (run <run_id[:8]>)"`,
  `update-ref` and a `read-tree` restore. `task_id` must match `^[\w-]+$`.
- 2.1's `RunRecord` holds no provider, task or monitor settings, and an
  unlaunched run keeps no payload (Decision 18).

**Implementation:**
- `state.rs`: `RunRecord` gains `watch: Option<WatchRecord>`, with
  `provider`, `session_id`, `task_id`, `timeout_seconds` and the `monitor`
  block. `launch.rs` commits it with the launched phase, so a run that
  outlives a node daemon restart is still watched. It holds no prompt,
  endpoint or token (Decision 26).
- `manifests.rs` (new): `refresh(node)` gets
  `GET /api/nodes/detection-manifests` (3.7) through `HubRelay` with the
  last `ETag` as `If-None-Match`. A 200 rebuilds the `ManifestSet` (2.12)
  and keeps the new `ETag`; a 304 or a failure keeps the current set. No
  pane check runs before the first successful fetch (Decision 27).
- `pane_monitor.rs` (new): `tick(node)` visits each launched run that has a
  watch record and whose terminal is in the host inventory:
  1. Reads its pane with `ControlClient::snapshot` (2.10) at the line counts
     above.
  2. Handles trust, loop, approval and queued-continuation prompts and the
     periodic Enter with 2.13's detectors and the settings above. It sends
     the Python key sequences through `ControlClient::write` with the Python
     action keys. Each send raises the run's `injections` count.
  3. Applies the automatic-write rule per terminal: a write that fails after
     the request was sent latches its action key, and the run gets no
     automatic write under any other key until a hub `terminal.write` (2.7)
     to that terminal succeeds, which lifts the latch.
  4. On the loop tracker's trip, or a provider-stall verdict on the 8-line
     read, checkpoints the workspace and posts a `doom_loop` or
     `provider_stall` terminate.
  5. Classifies attention and reads the idle facts and the composer on the
     15-line read.
  6. Past `timeout_seconds` from `accepted_at_ms`, posts a `timeout`
     terminate with the 50-line read as its tail. Past
     `init_timeout_seconds`, posts an `init_timeout` terminate. The node
     knows only the time; the hub decides.
  7. Stores the run's `pane` block: attention, injections, composer, idle,
     and the last 15-line read cut to 2,000 characters.
- Terminate posts are `POST /api/nodes/runs/terminate {v, run_id, cause, detail, pane_tail, checkpoint}`
  (3.1, 3.6) through `HubRelay`. `retry: true` posts again on the next
  tick, `retry: false` ends the asks for that cause and run, and a transport
  failure or non-2xx posts again on the next tick. The node never signals
  the run on any answer (Decision 30).
- `checkpoint.rs` (new) ports the checkpoint plumbing with the ref
  `refs/gobby/ckpt/<task_id>/<run_id>` (Decision 31) and returns
  `{ref_name, commit_sha, parent_sha, files_changed, message}`. A run with
  no `task_id`, or one that fails the pattern, gets no checkpoint, and a
  failed checkpoint only warns. Either way the post carries
  `checkpoint: null`.
- `reports.rs`: each agent entry carries the run's latest `pane`, null
  before its first pass.
- `serve.rs`: the `node-agent-report` tick (2.8) runs `refresh`, then
  `tick`, then notifies `changed`, so the report carries the fresh pane
  state. The first pass runs 5 seconds after start.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_agents)'`.

**Acceptance:**

- 2.14.1 - The node fetches manifests with the last `ETag` on each tick, keeps its set on a 304 or a failure, and runs no pane check before its first successful fetch. test: `crates/gdaemon/tests/node_agents.rs::pane_monitor_fetches_manifests`.
- 2.14.2 - Trust, approval (only with its flag) and queued-continuation prompts, and the periodic Enter (only with its flag, at its interval, skipped for a dialog), send the Python key sequences with the Python action keys, and each send raises the run's `injections` in the next report. test: `crates/gdaemon/tests/node_agents.rs::pane_monitor_auto_enter`.
- 2.14.3 - A write that fails after sending latches its action key and blocks automatic writes under other keys, and a later successful hub `terminal.write` to that terminal lifts the latch. test: `crates/gdaemon/tests/node_agents.rs::pane_monitor_automatic_write_latch`.
- 2.14.4 - The third loop dismissal checkpoints the workspace to `refs/gobby/ckpt/<task_id>/<run_id>` and posts `doom_loop`, and a provider stall checkpoints and posts `provider_stall` with its pane tail. test: `crates/gdaemon/tests/node_agents.rs::pane_monitor_doom_loop_and_stall_post`.
- 2.14.5 - A run past `timeout_seconds` posts `timeout` with the 50-line tail and one past `init_timeout_seconds` posts `init_timeout`; `retry: true` and an unreachable hub repeat the post on the next tick, `retry: false` ends it for that cause, and no answer makes the node signal the run. test: `crates/gdaemon/tests/node_agents.rs::pane_monitor_timeouts_ask_the_hub`.
- 2.14.6 - Each report's agent entry carries the run's attention, injections, composer, idle facts and tail. test: `crates/gdaemon/tests/node_agents.rs::report_carries_pane_state`.
- 2.14.7 - The watch record survives a node daemon restart, so a run that outlives it is still watched, and `agents.json` holds no prompt, endpoint or token. test: `crates/gdaemon/tests/node_agents.rs::watch_record_survives_restart`.

## P3: Contract version 2 and its hub consumers (#24148)
`kind: framing`

**Goal**: version 2 of `docs/contracts/node-commands.md` carries every
launch input a node needs and each agent's pane state. The hub builds
`agent.spawn` from its own resolvers, answers the node's launch-outcome and
terminate posts, serves the detection manifests, and applies a node run's
pane state through the attention, health and idle code its own runs use.

P3's leaves expand under #24148 (Decision 1). 3.1 depends on nothing. 3.2
and 3.3 depend on 3.1, 3.4 on 3.3, 3.5 on 1.3 and 3.4, 3.6 on 3.5, 3.7 on
3.6, and 3.8 on 3.4. Constraints lists the hub-mode tasks that block 3.3,
3.4 and 3.6.

### 3.1 Node-commands contract version 2 [category: code]
`kind: deliverable`

Targets:
- `docs/contracts/node-commands.md`
- `crates/gdaemon/src/node_commands.rs::*` — scope-reason: adds `CONTRACT_VERSION`, replaces `Sandbox` and removes `SandboxNetwork`, widens `SpawnPayload` and `ReportAgent`, and adds the new body types
- `src/gobby/hub/node_commands.py::*` — scope-reason: the same model changes on the Python side, plus `_PARSERS` entries for the new bodies
- `crates/gdaemon/tests/fixtures/node_commands/corpus.json::*` — scope-reason: every message changes to `v: 2`, and the new bodies and pane block gain messages
- `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`
- `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`
- `tests/hub/test_node_command_corpus.py::test_node_bodies_name_no_machine`

**Granularity:** one leaf with five acceptance items across two production
model files and the contract document. The Rust and Python models must
change in one commit, because the shared corpus fails on either side alone.

**Research context:**
- The landed contract (#24268) gives frames and node bodies a `v`. The
  models accept any `v` of at least 1 (`positive_u64` in Rust). The
  `agent.spawn` payload carries `sandbox` as `{enabled, network, extra_write_paths}`,
  the hub-flow table lists `remote_resume_unsupported`, and the node duties
  say the node "enforces `timeout_seconds`".
- The corpus is `{"messages": [{name, kind, json}]}`. Python's
  `parse_message(kind, raw)` dispatches through `_PARSERS`, and both sides
  round-trip every message and refuse an unknown field.
- `test_node_bodies_name_no_machine` refuses a `machine_id` in each node
  body, an `env` or `cwd` in the spawn payload, and a 1,001-entry report.
- `src/gobby/agents/sandbox.py::SandboxConfig` fields: `enabled`,
  `backend` (`"srt"` or `"provider-native"`), `mode` (`"permissive"` or
  `"restrictive"`), `allow_network`, `extra_read_paths`,
  `extra_write_paths`, `extra_deny_read_paths`, `extra_deny_write_paths`,
  `allowed_domains`, `denied_domains`, `allow_git_network`,
  `allow_package_registries` and `allow_unix_sockets`.
- `src/gobby/agents/spawn_models.py::SpawnRequest` holds the executor's
  inputs. The field closure below accounts for each one.

**Implementation:**
- Both sides define `CONTRACT_VERSION = 2` (a Rust `pub const` and a Python
  module constant). The models keep accepting any `v` of at least 1, so a
  refusal can name its request. Every handler and route refuses a `v` other
  than `CONTRACT_VERSION` with `unsupported_version`. Every corpus message
  carries `v: 2`, and both corpus tests assert it.
- The `agent.spawn` payload keeps the version 1 fields and adds, in order:
  - `sandbox`: the resolved `SandboxConfig` with all thirteen fields above,
    replacing version 1's object. `network` and `SandboxNetwork` are removed
    (Decision 22).
  - `agent_name`, `api_base`, `api_token`, `codex_oss_provider`,
    `resume_session_id`, `execution_mode`, `code_index_preflight` and
    `task_id`: string or null.
  - `extra_env`: an object of strings. `codex_config_overrides`: an array
    of strings. `droid_mode`: `"exec"` or `"interactive"`.
    `prewarm_pre_commit_store`: a boolean.
  - `monitor`: `{auto_enter_agent_terminals, auto_enter_agent_interval_seconds, auto_enter_approval_prompts, init_timeout_seconds}`
    (Decision 28).
  - `workspace` gains the mode `"existing"` and a `path`, string or null.
    `"existing"` names, by `path`, a workspace the node prepared for an
    earlier run of the resumed session, which the node runs in place
    (Decision 23, 2.3). `path` is required with `"existing"` and null
    with every other mode.
- `ReportAgent` gains `pane`, null before the node's first pass on the run,
  otherwise `{attention, injections, composer, idle, tail}`:
  - `attention`: `{reason, kind, fingerprint, payload, dismissed}` or null;
  - `injections`: a number, the run's automatic sends so far;
  - `composer`: `{state, line}`, where `state` is `"empty"`, `"draft"` or
    `"unknown"`, because a pane read never reads `"held"`;
  - `idle`: `{status, turn_in_flight, unsubmitted_input, queued_message_prompt, capacity_message, quota}`
    or null, where `status` is `"unknown"`, `"context_full"`, `"idle"` or
    `"active"` and `quota` is `{provider, reset_time}` or null;
  - `tail`: a string, the last 15-line read, at most 2,000 characters.
- New node bodies, sent to the hub's front door with the node key and
  carrying no `machine_id`:
  - `POST /api/nodes/runs/prompt-delivery-failure`: `{v, run_id, code, detail, pane}`,
    answering `{failed}`.
  - `POST /api/nodes/runs/code-index-warning`: `{v, run_id, warning, prompt}`,
    where `warning` is `{preflight, cwd, message}`, answering `{}`.
  - `POST /api/nodes/runs/terminate`: `{v, run_id, cause, detail, pane_tail, checkpoint}`,
    answering `{terminated, retry}`. `cause` is `"doom_loop"`,
    `"provider_stall"`, `"timeout"` or `"init_timeout"`. `checkpoint` is
    `{ref_name, commit_sha, parent_sha, files_changed, message}` or null.
  - `GET /api/nodes/detection-manifests`: answers
    `{manifests: [{provider_id, content}]}` with an `ETag`, or 304 when
    `If-None-Match` matches.
- The contract document:
  - "v1 commands" becomes "Commands", and the version 2 payload table
    replaces the version 1 table;
  - the hub-flow table drops `remote_resume_unsupported` and adds
    `remote_loopback_endpoint` (Decision 25), and the hub route table adds
    `run_not_found` (404);
  - the node duties say the node observes `timeout_seconds` and its pane
    states and asks the hub to terminate (Decision 30), that each report
    carries pane state, and that the node never removes an `existing`
    workspace;
  - a new "Fields not carried" section states the field closure below.
- Field closure, each `SpawnRequest` field the payload does not carry:
  - `cwd`, `project_path`, `worktree_id` and `clone_id`: the node computes
    its workspace from `source_root` and `workspace` (2.3), and a resume
    names its earlier workspace by `workspace.path` (3.2). The hub's
    worktree and clone rows belong to hub-mode D1 (Remote workspace
    lifecycle).
  - `agent_run_id` equals `run_id`. `workflow` is `workflow_name`.
    `branch_name` is inside `workspace`. `retry_terminal_id` is the
    payload's `terminal_id`, which a hub retry repeats.
  - `initial_variables`: the hub writes them to the child session in
    `prepare_terminal_spawn` before it sends, and agy's second merge is
    redundant on that path.
  - `claimed_session_id`, `machine_id` and `is_local`: run-row attributes
    the hub records. A loopback endpoint is refused (Decision 25), and node
    bodies name no machine.
  - `requested_reasoning_effort`, `reasoning_required`, `reasoning_status`
    and `reasoning_message`: the hub resolves reasoning and records it;
    the node gets the effective effort as `reasoning_effort`.
  - `auto_approve`: always true for spawned agents.
  - `sandbox_args` and `sandbox_env`: only the resume path reads them, and
    a resume reaches the node as a fresh `agent.spawn` (Decision 23).
  - `resume_metadata_json`: launch bookkeeping that follows from the
    payload, which the node does not report.
  - `code_index_preflight_warning`: the node produces it (2.11) and posts
    it.
  - `terminal_lifetime`: the executor does not read it.
  - `session_manager`, `run_manager`, `cleanup_agent`, `daemon_config`,
    `terminal_manager`, `terminal_runtime_registry`, `write_coordinator`,
    `prepared_spawn`, `phase_timings_ms`, `spawn_started_at`, `cancel_event`
    and `placement_binder`: in-process handles and timings, not data.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus)'`.
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_command_corpus.py -v`.

**Acceptance:**

- 3.1.1 - Every corpus message, including one of each new body and answer, a spawn with an `existing` workspace and a report agent with a `pane`, round-trips through the Rust models with `v` equal to `CONTRACT_VERSION`, and an unknown field fails. test: `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`.
- 3.1.2 - The same corpus round-trips through the Python models with `v` equal to `CONTRACT_VERSION`, and an unknown field fails. test: `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`.
- 3.1.3 - Each new body refuses a `machine_id`, a spawn payload with version 1's `sandbox.network` or with `env` or `cwd` fails, an `existing` workspace without a `path` or a `path` with another mode fails, and a 1,001-entry report fails. test: `tests/hub/test_node_command_corpus.py::test_node_bodies_name_no_machine`.
- 3.1.4 - The contract document specifies version 2's payload with the `existing` workspace, the pane block, the four new bodies, `run_not_found` and `remote_loopback_endpoint`, and no longer lists `remote_resume_unsupported`. file: `docs/contracts/node-commands.md`.
- 3.1.5 - The contract document's "Fields not carried" section names each `SpawnRequest` field the payload omits, with its reason. file: `docs/contracts/node-commands.md`.

### 3.2 Hub spawn payload builder [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/hub/spawn_payload.py`
- `src/gobby/agents/spawn_executor_providers.py::_agent_prompt_prefix`
- `tests/hub/test_spawn_payload.py`

**Granularity:** one leaf with eight acceptance items. The builder is one
function, and each item proves one group of the fields it produces:
every contract field needs a producer item. A partial builder cannot
produce a valid payload, so no field group ships alone.

**Research context:**
- No code builds `agent.spawn` today; `src/gobby/hub/` holds only
  `node_commands.py`.
- hub-mode 2.5's first step runs the local resolvers for a remote spawn:
  `resolve_spawn_generation_endpoint` gives `model`, `is_local`,
  `codex_oss_provider`, `codex_config_overrides`, `api_base`, `api_token`
  and `child_env`; `resolve_spawn_reasoning` gives the effective effort;
  `resolve_spawn_sandbox` gives the `SandboxConfig`;
  `code_index_preflight_mode` gives the preflight mode; the resume metadata
  gives the provider session id (only with `resume_existing_session`) and
  `execution_mode`; and the agent body gives `agent_name`, `droid_mode` and
  `prewarm_pre_commit_store`. For a local spawn,
  `src/gobby/mcp_proxy/tools/spawn_agent/_request.py::build_spawn_request`
  takes the same objects.
- `_agent_prompt_prefix` (`src/gobby/agents/spawn_executor_providers.py`)
  resolves the agent and renders its `agent` prompt with the run's
  variables. `prepare_codex_spawn` in the same module prepends it to a
  codex prompt, and
  `src/gobby/agents/spawn_executor_codex.py::_spawn_codex_terminal` merges
  `_agent_context_injected: True` into the child session's variables when
  the plan injects the persona.
- The hub's monitor reads `TmuxConfig` once, at construction
  (`AgentLifecycleMonitor.__init__`). The four settings a node needs are
  `auto_enter_agent_terminals`, `auto_enter_agent_interval_seconds`,
  `auto_enter_approval_prompts` and `init_timeout_seconds` (Decision 28).
- `AgentRun.task_id` (`src/gobby/storage/agents/_models.py`) names the
  run's task.
- hub-mode 2.5's fourth step maps the request's isolation to the payload's
  `workspace`. This plan moves that mapping into the builder.
- A local resume whose request names no isolation returns to the worktree
  or clone its last run used: `spawn_agent_impl` takes the ids from
  `src/gobby/mcp_proxy/tools/spawn_agent/_resume.py::resume_isolation`. A
  node run's hub row carries neither id. hub-mode 1.4's
  `record_remote_workspaces` records each node workspace as an `active`
  `worktrees` or `clones` row on the node's machine, with the run's child
  session as `agent_session_id` and the node's path and `branch_name`
  (hub-mode Decision 15). A resume keeps its session, so that row names the
  resumed session. `LocalWorktreeManager` and `LocalCloneManager` bind this
  machine, so they cannot read it.

**Implementation:**
- `spawn_executor_providers.py` gains the module function
  `agent_prompt_prefix(db, agent_name, initial_variables, project_id) -> str | None`,
  holding `_agent_prompt_prefix`'s body. `_agent_prompt_prefix` calls it,
  so local spawns are unchanged.
- `src/gobby/hub/spawn_payload.py` (new) defines
  `build_spawn_payload(db, *, run, request, endpoint, reasoning, sandbox_config, resume_metadata, code_index_mode, tmux_config) -> SpawnPayload`.
  `request` is hub-mode 2.5's validated spawn input. The builder:
  1. copies the version 1 typed fields;
  2. sets `sandbox` to the resolved config (Decision 22);
  3. sets `model`, `api_base`, `api_token`, `codex_oss_provider` and
     `codex_config_overrides` from the endpoint, and `extra_env` from its
     `child_env`;
  4. sets `resume_session_id` only when `resume_existing_session` is true,
     and `execution_mode` from the resume metadata;
  5. for codex, prepends `agent_prompt_prefix(...)` to the prompt and,
     when a prefix was added, merges `_agent_context_injected: True` into
     the child session's variables before it returns;
  6. sets `monitor` from `tmux_config`, and `task_id` from `run.task_id`;
  7. maps the workspace as hub-mode 2.5's fourth step did, except for a
     resume (`resume_existing_session`) whose request names no isolation:
     `resumed_workspace(db, run.machine_id, run.child_session_id)` reads the
     most recent `active` `worktrees` row, else `clones` row, with that
     machine and `agent_session_id` (a read-only query in this module), and
     a row gives `{mode: "existing", path, branch_name}`. With no row the
     mapping above applies, as a local resume with no recorded workspace
     does;
  8. returns a validated `SpawnPayload` (3.1), so a field outside the
     contract fails before anything is sent.
- `api_token` appears only in the returned payload and never in a log line
  (Decision 26).

Consumers unchanged:
- `src/gobby/agents/spawn_executor_codex.py` — no-edit-reason: it reads the codex plan's `inject_persona`, which `prepare_codex_spawn` still sets from `_agent_prompt_prefix`'s unchanged result.
- `tests/agents/test_spawn_executor_providers.py` — no-edit-reason: it imports `_agent_prompt_prefix` and asserts its rendered text, which the extraction leaves unchanged; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_spawn_payload.py tests/agents/test_spawn_executor_providers.py -v`.

**Acceptance:**

- 3.2.1 - `sandbox` carries the resolved `SandboxConfig` unchanged and has no `network` key. test: `tests/hub/test_spawn_payload.py::test_sandbox_is_resolved_config`.
- 3.2.2 - `model`, `api_base`, `api_token`, `codex_oss_provider`, `codex_config_overrides` and `extra_env` come from the endpoint resolution, and no log line carries the token. test: `tests/hub/test_spawn_payload.py::test_endpoint_fields`.
- 3.2.3 - `resume_session_id` is set only when the resume metadata resumes an existing session, and `execution_mode` passes through. test: `tests/hub/test_spawn_payload.py::test_resume_fields`.
- 3.2.4 - A codex payload's prompt starts with the persona prefix and the child session gains `_agent_context_injected`, other providers' prompts are unchanged, and `_agent_prompt_prefix` returns the same text as before. test: `tests/hub/test_spawn_payload.py::test_codex_persona_prefix`.
- 3.2.5 - `monitor` carries the hub's `TmuxConfig` values for its four settings. test: `tests/hub/test_spawn_payload.py::test_monitor_from_tmux_config`.
- 3.2.6 - `task_id` is the run's task id, and null for a run without one. test: `tests/hub/test_spawn_payload.py::test_task_id_from_run`.
- 3.2.7 - The workspace mapping, `agent_name`, `droid_mode`, `prewarm_pre_commit_store` and `code_index_preflight` match the inputs, and the result validates as `SpawnPayload`. test: `tests/hub/test_spawn_payload.py::test_launch_fields_and_workspace`.
- 3.2.8 - A resume with no requested isolation names the resumed session's active worktree or clone row on its machine as an `existing` workspace; a row on another machine, an inactive row or a requested isolation is ignored, and with no row the workspace maps as for a new spawn. test: `tests/hub/test_spawn_payload.py::test_resume_uses_existing_workspace`.

### 3.3 Node pane state on the hub [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/hub/remote_pane.py`
- `src/gobby/agents/attention_tracker.py::AgentAttentionTracker`
- `src/gobby/servers/routes/nodes.py`
- `tests/hub/test_remote_pane.py`

**Research context:**
- `AgentAttentionTracker.sync(run, pane_output)`
  (`src/gobby/agents/attention_tracker.py`) classifies the pane with the
  prompt detector and the stall classifier, then persists the result:
  `AttentionStateManager.transition_async`, the lifecycle wait enter and
  resolve (`_enter_lifecycle_wait`, `_resolve_lifecycle_wait`), and the
  metadata the attention surfaces read. `clear_after_injection(run)` runs
  after a trust or approval injection.
- hub-mode 1.4 (#24270 Node routes) creates `POST /api/nodes/report` in
  `src/gobby/servers/routes/nodes.py`, which stores each report under the
  caller's machine and refuses a revision at or below the stored one. The
  file does not exist yet, so it is a bare Target.
- hub-mode 2.1 scopes the hub's pane loops to its own machine, so no hub
  loop reads a node's pane.

**Implementation:**
- `src/gobby/hub/remote_pane.py` (new):
  - `RemotePaneStore` keeps entries keyed by `terminal_id`, each with
    `machine_id`, `run_id`, `pane` and a `stopped` flag, plus the last
    revision per machine. `remote_pane_store()` returns the one
    process-wide store.
  - `apply_report(machine_id, revision, agents)` ignores a revision at or
    below the machine's last, then replaces that machine's entries with the
    report's agents. `mark_stopped(terminal_id)` and `get(terminal_id)`
    serve 3.4.
  - `apply_pane_state(db, tracker, previous, agents)`: for each agent with
    a `pane`, it loads the run. When `injections` rose since the previous
    entry, it calls `tracker.clear_after_injection(run)`. Then it calls
    `tracker.apply(run, classification)` with the reported attention.
- `AgentAttentionTracker.sync` splits into
  `classify(run, pane_output) -> AttentionClassification | None` and
  `apply(run, classification)`. `sync` calls both, so local runs are
  unchanged. `AttentionClassification` holds `reason`, `kind`,
  `fingerprint`, `payload` and `dismissed`, the shape of the report's
  `attention` block.
- `nodes.py`: after the report route's `put` succeeds, it calls
  `apply_report` and `apply_pane_state`. A failure there is logged, and the
  route still answers `{}`, because the report itself was accepted.

Consumers unchanged:
- `tests/agents/test_attention_metadata.py` — no-edit-reason: it constructs the tracker and calls `sync` and `clear_after_injection`, whose signatures and transitions are unchanged; re-run as verification.
- `tests/agents/test_attention_state.py` — no-edit-reason: it constructs the tracker and calls `sync` and `clear_after_injection`, whose signatures and transitions are unchanged; re-run as verification.
- `tests/agents/test_native_spawn.py` — no-edit-reason: it calls `sync` on a native snapshot and `clear_after_injection`, both unchanged; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_pane.py tests/agents/test_attention_state.py tests/agents/test_attention_metadata.py tests/agents/test_native_spawn.py -v`.

**Acceptance:**

- 3.3.1 - The store applies a newer revision, ignores one at or below the machine's last, and replaces that machine's entries with the report's agents. test: `tests/hub/test_remote_pane.py::test_store_follows_report_revisions`.
- 3.3.2 - A reported attention block enters the same attention state and lifecycle wait that a local classification of the same pane enters, and a null block resolves it. test: `tests/hub/test_remote_pane.py::test_node_attention_applies`.
- 3.3.3 - A rise in `injections` clears attention as a local injection does. test: `tests/hub/test_remote_pane.py::test_injection_clears_attention`.
- 3.3.4 - `sync` on a local run produces the same transitions through `classify` and `apply` as before the split. test: `tests/hub/test_remote_pane.py::test_local_sync_unchanged`.

### 3.4 Remote terminal runtime [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `src/gobby/hub/remote_runtime.py`
- `src/gobby/terminals/services.py::TerminalServices.runtime_for`
- `src/gobby/agents/watchdog/composer_gate.py::composer_refuses_automation`
- `tests/hub/test_remote_runtime.py`

**Research context:**
- `src/gobby/terminals/runtime.py::TerminalRuntime` has thirteen members:
  `backend`, `prepare_spawn`, `commit_spawn`, `is_live`,
  `session_present`, `snapshot(terminal, lines, *, mode)`, `snapshot_full`,
  `write_text`, `write_key`, `write_input`, `write_paste`, `resize`,
  `terminate(terminal, grace_seconds)` and `attach_locator`.
  `SnapshotResult` is `{text, truncated, dropped_bytes, total_bytes}`.
- `TerminalServices.runtime_for(terminal)` returns
  `registry.resolve(terminal.backend)`. `TerminalServices.is_live`,
  `snapshot`, `snapshot_full` and `terminate` resolve through it, and so do
  the kill paths: `src/gobby/agents/capture.py::terminate_managed_runtime_async`
  (`snapshot_full`, `terminate`, then `session_present`),
  `AgentHealthMonitor._terminate_tmux_run`, the doom-loop kill
  (`AgentLifecycleMonitor._checkpoint_and_kill_looping_agent`), and the
  recovery and terminal-cleanup kills.
- hub-mode 2.3 routes a remote terminal's writes in
  `WriteCoordinator._dispatch` before `runtime_for`. hub-mode 2.4's
  `remote_cancel.cancel(run, db, signal_name=, close_terminal=, terminal_action=, terminal_reason=)`
  sends `agent.cancel`. `not_found` counts as stopped, and `{stopped: false}`
  returns `error_code: not_stopped`.
- `Terminal` (`src/gobby/storage/terminals.py`) has `machine_id` and
  `agent_run_id`. `TerminalServices.manager` is a `TerminalManager` whose
  `db` is the hub database. `require_machine_id()`
  (`src/gobby/utils/machine_id.py`) names the hub's machine.
- `composer_refuses_automation(services, run)` reads the hub's composer
  ledger (`read_composer`) and lets `empty` and `held` through.

**Implementation:**
- `src/gobby/hub/remote_runtime.py` (new) defines
  `RemoteTerminalRuntime(db)` with `backend` `"native"`:
  - `is_live` and `session_present`: the terminal has an entry in 3.3's
    store that is not stopped.
  - `snapshot(terminal, lines, mode=...)`: the last `lines` lines of the
    stored tail, as `SnapshotResult(text, truncated=False, dropped_bytes=None, total_bytes=None)`.
    `snapshot_full` returns the whole tail. A missing entry returns empty
    text.
  - `terminate(terminal, grace_seconds)`: loads the run named by
    `terminal.agent_run_id` and calls `remote_cancel.cancel` with `TERM` and
    `close_terminal=True`. On `not_stopped` it waits `grace_seconds` and
    sends `KILL`, as a local terminate escalates. A success marks the entry
    stopped. Any other failure is logged and leaves the entry live, so
    `terminate_managed_runtime_async` reports the kill as failed.
  - The four write methods raise `TerminalWriteError(stage="none")`,
    because hub-mode 2.3's guard sends remote writes before any runtime is
    resolved. `prepare_spawn`, `commit_spawn`, `resize` and
    `attach_locator` raise, because a remote spawn goes through hub-mode
    2.5.
- `TerminalServices.runtime_for` returns `RemoteTerminalRuntime(self.manager.db)`
  when `terminal.machine_id` is not `require_machine_id()`, and the
  registry's runtime otherwise. This one guard covers every kill path
  above.
- `composer_refuses_automation`: for a terminal on another machine it reads
  the store's `composer`. Only `"empty"` lets a write through, and a missing
  entry reads as `"unknown"`. Local terminals keep the ledger read.

Consumers unchanged:
- `src/gobby/agents/capture.py` — no-edit-reason: `terminate_managed_runtime_async` takes its runtime from `runtime_for`, and the remote runtime answers `snapshot_full`, `terminate` and `session_present`.
- `src/gobby/agents/watchdog/recovery.py` — no-edit-reason: its kills resolve the runtime through `runtime_for`.
- `src/gobby/terminals/write_coordinator.py` — no-edit-reason: hub-mode 2.3's guard sends remote writes before `runtime_for`.
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: its auto-enter calls `composer_refuses_automation(services, run)`, whose signature is unchanged, for runs hub-mode 2.1 scopes to the hub's machine.
- `tests/agents/watchdog/test_composer_gate.py` — no-edit-reason: hub-mode 2.3 (which blocks this leaf) makes `make_memory_terminal` default to the local machine, so its terminals keep the ledger read; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_runtime.py tests/agents/test_capture.py tests/agents/watchdog/test_composer_gate.py -v`.

**Acceptance:**

- 3.4.1 - `runtime_for` returns the remote runtime for a terminal on another machine and the registry's runtime for a local one. test: `tests/hub/test_remote_runtime.py::test_runtime_for_routes_by_machine`.
- 3.4.2 - Liveness and snapshots of a node terminal come from the store, and a missing or stopped entry reads as not live with empty text. test: `tests/hub/test_remote_runtime.py::test_liveness_and_snapshot_from_store`.
- 3.4.3 - `terminate` sends `agent.cancel` with `TERM`, then `KILL` after the grace on `not_stopped`, and marks the entry stopped, so `terminate_managed_runtime_async` on a node run captures the stored tail and reports success; an offline node reports failure. test: `tests/hub/test_remote_runtime.py::test_terminate_goes_through_node`.
- 3.4.4 - For a node terminal the composer gate lets only `"empty"` through and refuses `"draft"`, `"unknown"` and a missing entry, and local terminals keep the ledger read. test: `tests/hub/test_remote_runtime.py::test_remote_composer_gate`.
- 3.4.5 - The write, spawn, resize and attach methods of the remote runtime raise. test: `tests/hub/test_remote_runtime.py::test_remote_runtime_refuses_local_operations`.

### 3.5 Hub routes for launch outcomes [category: code] (depends: 1.3, 3.4)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/node_runs.py`
- `src/gobby/servers/_app_routes.py::register_routes`
- `tests/servers/test_node_run_routes.py`

**Research context:**
- 1.3's `src/gobby/servers/routes/node_hooks.py` sets the identity rules:
  no verified node identity answers 403 `node_identity_required`, the hub's
  own machine answers 403 `machine_is_local`, and a body that fails to parse
  answers 422 `invalid_body`.
- `AgentRun.machine_id` names the run's machine.
- `src/gobby/agents/spawn_executor_support.py::_fail_codex_prompt_delivery`:
  `fail_uninitialized_prompt_delivery(run_id, error=)`
  (`src/gobby/storage/agents/_lifecycle.py`) returns the transitioned run
  or None. On a transition, the terminal is terminated with a 5-second grace
  and the cleanup handler runs with `terminal_payload=error` and
  `preterminalized_run`. `_codex_prompt_failure_reason(pane, code=, detail=)`
  redacts the pane and keeps a 1,024-character tail.
- `AgentLifecycleMonitor.get_cleanup_agent()` returns the cleanup handler,
  and the server holds the monitor as
  `server.services.agent_lifecycle_monitor` and the terminal services as
  `server.services.terminal_services`.
- `_prepare_managed_code_index`'s failure branch
  (`src/gobby/agents/spawn_executor_providers.py`) merges the session
  variables with `SessionVariableManager`: `additional_skills` without
  `gobby:references/code-index/overview.md`,
  `code_index_preflight_warning`, and the new prompt.

**Implementation:**
- `src/gobby/servers/routes/node_runs.py` (new) defines
  `create_node_runs_router(server)`, registered in `register_routes`. Each
  route applies 1.3's identity rules, answers 422 `unsupported_version` for
  a `v` other than `CONTRACT_VERSION`, and loads the run by `run_id`. A
  missing run, or one whose `machine_id` is not the caller's, answers 404
  `run_not_found`.
- `POST /api/nodes/runs/prompt-delivery-failure` follows
  `_fail_codex_prompt_delivery`'s steps: it builds the error with
  `_codex_prompt_failure_reason(pane, code=code, detail=detail)` and calls
  `fail_uninitialized_prompt_delivery`. With no transition it answers
  `{failed: false}`. Otherwise it terminates the terminal through
  `terminal_services.runtime_for(terminal).terminate(terminal, grace_seconds=5.0)`,
  which is 3.4's `agent.cancel`, runs the cleanup handler with the
  transitioned run, and answers `{failed: true}`.
- `POST /api/nodes/runs/code-index-warning` merges the session variables as
  the failure branch does and answers `{}`.

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: it calls `register_routes(app, server)`, whose signature is unchanged.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: it calls `register_routes` and asserts retired wiki paths, which the added router does not serve; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_node_run_routes.py tests/test_wiki_retirement_contract.py -v`.

**Acceptance:**

- 3.5.1 - Both routes refuse a missing node identity, the hub's own key, another `v` and an invalid body with 1.3's codes, and answer 404 `run_not_found` for an unknown run or another machine's run. test: `tests/servers/test_node_run_routes.py::test_node_run_routes_identity`.
- 3.5.2 - A prompt-delivery failure on an uninitialized run stores the redacted error, kills the run through `agent.cancel`, runs cleanup and answers `{failed: true}`, and on an initialized run answers `{failed: false}` and changes nothing. test: `tests/servers/test_node_run_routes.py::test_prompt_delivery_failure_route`.
- 3.5.3 - A code-index warning merges the session variables as the local failure branch does. test: `tests/servers/test_node_run_routes.py::test_code_index_warning_route`.

### 3.6 Terminate route [category: code] (depends: 3.5)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/node_runs.py`
- `src/gobby/agents/agent_health.py::*` — scope-reason: extracts `session_never_initialized` from `AgentHealthMonitor.check_initialization_timeout` and `bootstrap_accounting_stall_error` from `AgentHealthMonitor._bootstrap_accounting_stall_error` as module functions that both methods call
- `tests/servers/test_node_run_routes.py`

**Research context:**
- `AgentHealthMonitor.check_unhealthy_agents`
  (`src/gobby/agents/agent_health.py`) skips every run that
  `is_recovery_protected` (`src/gobby/agents/recovery_state.py`), and skips
  a non-interactive run when `cooperative_close_handoff_pending` or
  `bound_task_is_closed` (`src/gobby/agents/run_completion.py`). A run is
  past its timeout when its age from `started_at` exceeds
  `timeout_seconds`. The reason is `Agent exceeded <timeout_seconds>s timeout`.
  When `_bootstrap_accounting_stall_error` finds a bootstrap stall, its text
  replaces the reason and the action becomes `fail`; otherwise the action is
  `timeout`. The error is the reason, then
  `\n\n--- Last terminal output ---\n`, then the last 2,000 characters of a
  50-line read.
- `check_initialization_timeout` skips protected runs and runs younger than
  `init_timeout_seconds`, and needs the child session with its timestamps.
  A session counts as initialized when its `external_id` differs from its
  `id`, `context_injected` is set, any message, turn or tool counter is
  nonzero, the run has turns or tool calls, or `last_activity` is later than
  `created_at`. The reason is
  `Provider connection timed out: agent never initialized after <age>s (provider=<provider>)`,
  and the action is `fail`.
- The provider stall error is
  `Provider stall: <reason> (provider=<p>, consecutive_hits=<n>)\n\n<pane>`,
  and the doom-loop reason is `doom loop: dismissed loop prompt 3+ times`.
  Both use `fail`, after a checkpoint.
- Each kill calls `terminate_managed_runtime_async(storage=run manager, run, terminal, runtime=runtime_for(terminal), action, reason, terminalize=...)`,
  whose `terminalize` runs the cleanup handler (`_terminate_tmux_run`,
  `_checkpoint_and_kill_looping_agent`).
- `src/gobby/storage/checkpoints.py::LocalCheckpointManager.create`
  inserts a `Checkpoint` with `id`, `task_id`, `session_id`, `run_id`,
  `ref_name`, `commit_sha`, `parent_sha`, `files_changed` and `message`.

**Implementation:**
- `agent_health.py` gains the module functions
  `session_never_initialized(run, session) -> bool` and
  `bootstrap_accounting_stall_error(run, session, *, age_seconds, pane_snapshot) -> str | None`,
  holding the bodies of the conditions above. The two methods call them, so
  local runs are unchanged.
- `POST /api/nodes/runs/terminate` applies 3.5's identity and run lookup.
  Then:
  1. A run that is no longer pending or running answers
     `{terminated: false, retry: false}`. A protected run, or a
     non-interactive run with a pending cooperative close or a closed bound
     task, answers `{terminated: false, retry: true}`.
  2. `timeout`: the hub recomputes the age from the run's `started_at`. A
     run within `timeout_seconds` answers `{terminated: false, retry: true}`.
     Otherwise the reason, the bootstrap-stall swap and the error are built
     as above, with `pane_tail` as the 50-line read.
  3. `init_timeout`: a run younger than the hub's `init_timeout_seconds`,
     or one whose session is missing, answers `{terminated: false, retry: true}`.
     An initialized session answers `{terminated: false, retry: false}`.
     Otherwise the reason is the initialization message.
  4. `provider_stall` and `doom_loop`: the error is the provider-stall text
     with the node's `detail` and `pane_tail`, or the doom-loop reason.
  5. A non-null `checkpoint` is recorded with
     `LocalCheckpointManager(db).create`, filling `task_id`, `session_id`
     and `run_id` from the run.
  6. The route stores `pane_tail` as the terminal's tail in 3.3's store, so
     the capture records it, then calls `terminate_managed_runtime_async`
     with the terminal's 3.4 runtime, the action, the error, and a
     `terminalize` that runs the cleanup handler as
     `_terminate_tmux_run` does.
  7. It answers `{terminated: true, retry: false}` on success and
     `{terminated: false, retry: true}` when the kill failed.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_node_run_routes.py tests/agents/test_lifecycle_monitor.py -v -k "terminate or Initialization or unhealthy"`.

**Acceptance:**

- 3.6.1 - A `timeout` for a run past its database age kills it with the local error text and `timeout` action, a bootstrap stall swaps in its reason with `fail`, and a younger run answers `retry: true` and stays running. test: `tests/servers/test_node_run_routes.py::test_terminate_timeout`.
- 3.6.2 - An `init_timeout` for a never-initialized session kills the run with the local message, and an initialized one answers `retry: false` and stays running. test: `tests/servers/test_node_run_routes.py::test_terminate_init_timeout`.
- 3.6.3 - `provider_stall` and `doom_loop` kill the run with the local error texts and record the node's checkpoint row with the run's task, session and ref. test: `tests/servers/test_node_run_routes.py::test_terminate_stall_and_loop_record_checkpoint`.
- 3.6.4 - A protected run, a non-interactive run with a pending cooperative close or a closed bound task, and a failed kill answer `retry: true`, and an ended run answers `retry: false`. test: `tests/servers/test_node_run_routes.py::test_terminate_refusals`.
- 3.6.5 - The local health checks give the same results through the extracted functions. test: `tests/servers/test_node_run_routes.py::test_extracted_health_conditions_match_local`.

### 3.7 Detection manifest route [category: code] (depends: 3.6)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/node_runs.py`
- `tests/servers/test_node_run_routes.py`

**Research context:**
- The `detection_manifests(provider_id, content)` table holds the synced
  manifests, and `server.services.detection_registry`
  (`src/gobby/app_context.py`) is the hub's `DetectionManifestRegistry`,
  which rereads it every 30 seconds.
- Decision 27 sets the node's refresh: its 30-second tick sends the last
  `ETag`.

**Implementation:**
- `GET /api/nodes/detection-manifests` applies 3.5's identity rules and
  reads `SELECT provider_id, content FROM detection_manifests ORDER BY provider_id`.
  The `ETag` is the SHA-256 over each `provider_id` and the SHA-256 of its
  `content`, in order. A matching `If-None-Match` answers 304 with no body;
  otherwise the route answers `{manifests: [{provider_id, content}]}`.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_node_run_routes.py -v -k manifests`.

**Acceptance:**

- 3.7.1 - A node gets every manifest row in provider order with an `ETag`, gets 304 when its `If-None-Match` matches, and gets the new rows and a new `ETag` after a row changes; the identity rules apply. test: `tests/servers/test_node_run_routes.py::test_detection_manifests_route`.

### 3.8 Hub idle policy for node runs [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `src/gobby/agents/idle_check_handler.py::IdleCheckHandler`
- `tests/hub/test_remote_idle_policy.py`

**Research context:**
- `IdleCheckHandler.check_idle_agents`
  (`src/gobby/agents/idle_check_handler.py`) visits the hub's own active
  terminal runs (`_get_active_terminal_runs`) and calls
  `_handle_idle_check(run, pane_output=, attention_synced=)` with the pane
  the attention scan read.
- `_handle_idle_check` reads these facts from the pane: the codex quota
  message (`src/gobby/agents/watchdog/quota.py`), the codex capacity banner,
  the idle status (`unknown`, `context_full`, `idle` or `active`),
  `turn_in_flight`, `unsubmitted_input` and `queued_message_prompt`. It
  keeps the counters `reset_idle`, `should_reprompt` and `should_fail` in
  the hub's idle detector, reprompts through `TerminalServices.write`, and
  fails a run through the kill paths.
- `_read_local_transcript` returns `(None, None)` for a session on another
  machine, so the transcript recoveries (`recover_completed_turn`,
  `_recover_capacity_error`) do not apply to a node run.
- Remote writes go through hub-mode 2.3, and kills and liveness through
  3.4's runtime. The composer gate reads the store for a node terminal
  (3.4).

**Implementation:**
- `_handle_idle_check(run, *, pane_output=None, attention_synced=False)`
  keeps its signature and reads the pane through a `PaneFacts` value with
  the quota, the capacity candidate, the status, `turn_in_flight`,
  `unsubmitted_input`, `queued_message_prompt` and the tail. For a local
  run, `PaneFacts` is computed from `pane_output` as today. For a node run
  (its `machine_id` is not `require_machine_id()`), it comes from the
  store's `idle` block and tail (3.3). A node run with no entry, or a null
  `idle`, is skipped for that pass, with no attention sync.
- `check_idle_agents` adds the active runs on machines that have store
  entries (`_get_remote_runs`) to the runs it visits and prunes recovery
  state against. `check_attention_agents` stays local, because node
  attention arrives in reports (3.3).
- The counters and decisions stay in the hub, so a node run is reprompted
  and failed by the same rules as a local one.

Consumers unchanged:
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: it constructs `IdleCheckHandler` and calls `check_idle_agents`, whose constructor and signatures are unchanged; the store is the process-wide `remote_pane_store()`.
- `tests/agents/test_attention_state.py` — no-edit-reason: it constructs the handler and calls `check_idle_agents` on local runs, which take the unchanged path; re-run as verification.
- `tests/agents/test_idle_check_transcript_paths.py` — no-edit-reason: it patches `_handle_idle_check` and calls `check_idle_agents` with no store entries, so the visited and pruned runs are unchanged; re-run as verification.
- `tests/agents/test_lifecycle_monitor_extra.py` — no-edit-reason: it calls `_handle_idle_check(db_run)` on a local run, which the kept signature accepts; re-run as verification.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_remote_idle_policy.py tests/agents/test_idle_check_transcript_paths.py tests/agents/test_attention_state.py tests/agents/test_lifecycle_monitor_extra.py -v`.

**Acceptance:**

- 3.8.1 - An idle node run with an empty composer is reprompted through a node `terminal.write`. test: `tests/hub/test_remote_idle_policy.py::test_idle_node_run_reprompted`.
- 3.8.2 - A node run still idle after the maximum reprompts is failed through 3.4's runtime. test: `tests/hub/test_remote_idle_policy.py::test_node_run_failed_after_max_reprompts`.
- 3.8.3 - A reported quota message or a `context_full` status fails the node run as it fails a local run. test: `tests/hub/test_remote_idle_policy.py::test_quota_and_context_full_fail_node_run`.
- 3.8.4 - A node run whose composer holds a draft is not reprompted, and one with no store entry is skipped. test: `tests/hub/test_remote_idle_policy.py::test_draft_and_missing_entry_hold`.
- 3.8.5 - Local runs take the same idle decisions as before, with their facts read from the pane. test: `tests/hub/test_remote_idle_policy.py::test_local_idle_unchanged`.

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
- 2026-10-13: Return from gobby#15977 with Josh's ruling "No deferrals":
  droid and agy, node pane detection and the live proof move into scope, and
  the v1 payload limits are lifted by an additive contract amendment. The M1
  manifest stamped at 32d8aa5dde is withdrawn; its bytes stay in Git
  history. A full adversary re-review and a fresh M1 follow the revision.
- 2026-10-13: No-deferrals revision for Josh's rulings relayed by
  gobby#15977 ("We're not deferring grok or agy"; "No deferrals"; "Complete
  plan"; "No second version. Plan fully or explain why you can't.") and his
  ITEM D ruling ("rust on the node now"), closing Plan Adversary
  gobby#16791's NH-11 bounce. D1 to D3 and their placeholder task refs are
  gone; planned leaves replace them. 2.10 delivers codex's first prompt, 2.11
  runs the node's code-index preflight, and 2.12 to 2.14 port the lifecycle
  monitor's detectors to one Rust module with parity tests against a corpus
  the Python detectors record, then run it as the node pane monitor. P3 (3.1
  to 3.8) adds contract version 2 with its field closure, the hub payload
  builder, node pane state on the hub, the remote terminal runtime, and the
  hub's launch-outcome, terminate, manifest and idle-policy consumers.
  Decisions 3, 4 and 13 now carry all five providers, full monitor parity
  and hub-mode D4's live proof (Decision 20), and Decisions 20 to 31 record
  the rulings and writer decisions. 2.7 accepts all five providers and only
  `CONTRACT_VERSION`, and Decision 26 keeps only 2.14's watch record on
  disk. A remote resume runs in place in the workspace its session's
  earlier run used on the node, which the node never removes (Decision 23;
  2.1, 2.3, 2.7, 3.1, 3.2). The same commit amends hub-mode.md so a resume
  routes to the session's machine and resumes that session there, and 2.5
  builds its payload with 3.2.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation,
none has run yet. Each must pass after its leaves land and before #24150 and
#24148 close.

```bash
CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_hooks) + binary(node_agents) + binary(nodes) + binary(front_door) + binary(heartbeat) + binary(http_contracts) + binary(run_modes) + binary(pane_detection_corpus) + binary(node_command_corpus)'
CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-terminals -E 'binary(control_snapshot)'
CARGO_BUILD_JOBS=3 cargo clippy -p gobby-daemon -p gobby-terminals --all-targets -- -D warnings
cargo fmt -p gobby-daemon -p gobby-terminals -- --check
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py tests/servers/test_node_hook_routes.py tests/hooks/test_replay_decision.py tests/agents/test_node_srt_corpus.py tests/agents/test_node_isolation_golden.py tests/agents/test_node_launch_corpus.py -v
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/ -k "inbox or replay or receipt or grok_pending" -v
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_pane_detection_corpus.py tests/agents/test_prompt_detector.py tests/agents/test_idle_detector.py tests/agents/test_stall_classifier.py -v
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_command_corpus.py tests/hub/test_spawn_payload.py tests/hub/test_remote_pane.py tests/hub/test_remote_runtime.py tests/hub/test_remote_idle_policy.py tests/servers/test_node_run_routes.py -v
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_spawn_executor_providers.py tests/agents/test_attention_state.py tests/agents/test_attention_metadata.py tests/agents/test_native_spawn.py tests/agents/test_capture.py tests/agents/watchdog/test_composer_gate.py tests/agents/test_lifecycle_monitor.py tests/agents/test_lifecycle_monitor_extra.py tests/agents/test_idle_check_transcript_paths.py tests/test_wiki_retirement_contract.py -v
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new
uv run gobby plans validate .gobby/plans/node-halves.md -p .
```

- The plan validation command must pass at every revision; it is the only
  check that runs during planning.
- These gates must pass against a fake hub and a fake terminal host
  (Decision 13). The live hub-to-node proof of these sections must pass in
  hub-mode D4 (#24283 Live hub-to-node proof), which owns it (Decision 20).
- Every cargo run is a heavy command and must hold the Release Manager's
  grant. These gates must run from the worktree root, and the full pytest
  suite must never run.
