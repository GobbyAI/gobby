# Node halves (#24148 + #24150) working context — Plan Writer gobby#16767

Live working notes for drafting ONE plan covering #24148 (S2.7 node half: node-local
agent spawn and worktrees in gdaemon) and #24150 (S2.11 node half: gdaemon node-local
envelope ledger forwarding hooks to the hub). Not plan authority; refresh in place.

## Assignment (Orchestrator gobby#15977, 2026-10-13)

- Lane 2 Stage 2 planning; operator approved `stage2-plan:now`. Planning only: no
  implementation until Stage 1 closes.
- Write in main checkout on `0.5.0` under `.gobby/plans/` (proposed path
  `.gobby/plans/node-halves.md`, Plan ID `node-halves` — not yet created).
- Adversary is Grok seat #16768 (headless, cannot wait until #24284 cutover). When bare
  `uv run gobby plans validate <plan>` exits 0: commit, then send 15977 AND 16768 the
  plan path, commit SHA and file hash. 15977 resumes 16768 if it ended.
- `plans validate` may time out on 5 s daemon calls under load hold (#24292): retry when
  load eases; never commit before it passes.
- Decisions not resolvable from code go to 15977 as a short list.
- Enhancer seat: none named yet in the assignment.

## Epic scope (task text)

- #24148: node machine-local agent duties in gdaemon, driven by hub over node channel:
  agent spawn at the `TerminalRuntime` spawn seam with SRT sandbox chokepoint (one
  chokepoint, ROADMAP decision 6), worktree and clone ops. Every semantic call forwarded
  to hub as authenticated HTTP with machine API key; node holds no datastore credential
  (decision 17). Out of scope (hub half #24149): machine_id attention, roster
  aggregation, response routing, Rust dispatch gate. Depends only on #21555. Blocks
  #21579, #21564.
- #24150: gdaemon-owned node-local claim/dedupe/replay/STOP-epoch ledger (decision 9)
  draining `ghook` spool, forwarding each envelope to hub `POST /api/hooks/execute`.
  Decision D: ghook posts through local node. Carry unchanged: atomic
  claim/renew/finalize/release on 15 s lease; 409 and 503 `{"status":"retry"}`; per-machine
  STOP replay epoch; 24 h retention; boot release of this machine's `processing` rows
  under daemon singleton; batched drain lookups. ghook spool + 3 frozen schemas
  untouched. Replaces `src/gobby/hooks/inbox.py`, `envelope_dedupe.py` on nodes. Rule
  engine stays on hub; FREEZE `POST /api/hooks/execute` as a contract first.
  #24151 (hub half) says the node half freezes that contract.

## Landed baseline (0.5.0 @ 97b1bbe055)

- Node channel landed: #24090 (`crates/gdaemon/src/nodes/channel.rs` hub handler +
  node `run_client`/`connect_client`; `registry.rs` Registry), #24091 machines API,
  #24268 hub-mode 1.1 contract: `docs/contracts/node-commands.md`,
  `crates/gdaemon/src/node_commands.rs` (serde types, deny_unknown_fields),
  `crates/gdaemon/tests/fixtures/node_commands/corpus.json`,
  `src/gobby/hub/node_commands.py`.
- Node channel client today: hello/ack then only ping loop; ignores inbound frames
  (`run_client` select loop just resets idle). S2.7/S2.8 add commands.
- `state.rs::NodeServices { bootstrap_path, relay: HubRelay }`,
  `NodeServices::prepare` (needs api_key, hub_daemon_url, hub_cert for https; PID claim).
  Node front door relays every request to hub (`front_door/relay.rs`), so today a
  node-local ghook POST /api/hooks/execute is relayed straight to the hub (no ledger).
- `state_services.rs::ActiveServices::start` (hub/standalone, lease-active) starts the
  terminal host family `gobby_terminals::family::TerminalFamily` with
  `HostOptions::from_pool` + `PostgresEpochAuthority` — DB-backed. `EpochAuthority`
  trait in `crates/gterminals/src/host.rs` (line ~169); test authorities exist
  (`EmptyAuthority` in state_services tests). A node has NO terminal host today and no
  DB: needs a node-local epoch authority (open design question; S2.8 M1 #23205 is a
  separate #21579 edge, mostly closed, #23211/#23213/#23214 open).
- No SRT / provider-command code exists in Rust (only gclient display of SRT marks).
  Python owns: `src/gobby/agents/spawn_executor_providers.py::ProviderSpawnPlan`,
  SRT pinned `@anthropic-ai/sandbox-runtime` (`src/gobby/utils/dependency_requirements.py`,
  `src/gobby/install/srt-package-lock.json`), sandbox reaping
  (`reap_terminal_sandbox_run`), `prepare_claude_spawn` (.mcp.json, provider dir
  pre-approval), `get_terminal_env_vars` (cargo target + spawn cache dirs).

## hub-mode.md (#21575, stamped M1) — what the hub expects from the node

- Commands v1 hub to node: `agent.spawn`, `agent.cancel`, `terminal.write`; frames,
  stage rule, error codes in `docs/contracts/node-commands.md` (read it; it lists the
  node duties verbatim incl. durable preparation record, cancel records, 1,000-run
  refusal, per-run_id serialization, report rules, and the #24148 test-case list).
- Node bodies node to hub (hub routes in hub-mode 1.4, Python):
  `POST /api/nodes/checkouts {v, project_id, root_path}` and
  `POST /api/nodes/report {v, hub_epoch, revision, host_epoch, accepted, agents, exits}`;
  `stale_report` 409 `{code, hub_epoch}`.
- Internal gdaemon route (hub side) is hub-mode deferred D2 (not mine).
- hub-mode Decision 7: "Node pane detection and its reporting route are planned with
  #24148" (story B uses hook-driven attention only) — must address (likely typed
  deferral or explicit non-goal; ask 15977).
- hub-mode Decision 13/14: node sets 7 identity env vars + GOBBY_DAEMON_URL (own front
  door) + GOBBY_PROMPT_FILE (node-local file); hub issues no managed credential.
- hub-mode 2.2: hub binds envelope machine to caller key; a forwarded hook must carry
  the node's key identity (node ledger forwards with node api_key).
- hub-mode Decision 15: worktree/clone path and branch embed run_id; hub inserts
  workspace rows from reports; D1 (merge/remove remote workspace) deferred there.
- hub-mode D4 live proof depends on #24148 and #24150.

## Python hook ledger facts (to port to gdaemon on nodes)

- `src/gobby/hooks/envelope_dedupe.py`: filesystem markers under
  `~/.gobby/hooks/inbox/processed/<id>` JSON; `claim_envelope_processing` (O_EXCL create,
  owner_token, owner pid+create_time, lease 15 s), `renew_...` (CAS by owner_token),
  `finalize_envelope_processed` (stores response + hook_type), `release_...`,
  `envelope_terminal_response` (replay stored response; STOP/turn_end blocks only within
  ENVELOPE_REPLAY_GRACE_SECONDS=120 and processed after `.stop_replay_epoch`),
  `bump_stop_replay_epoch` (on session_start), `mark_envelope_processed`,
  `prune_processed_envelope_markers` (24 h retention, bounded 100k entries/pass),
  malformed marker = processed. Header `X-Gobby-Envelope-Id`.
- Route `src/gobby/servers/routes/mcp/hooks.py::create_hooks_router.execute_hook`:
  claim; on no claim -> replay stored response, else reclaim if marker gone/stale,
  else 409 `{status: processing|processed, reason}` (409 malformed_marker); lease
  renewal task; adapter run; 503 `{status: retry, retry_kind}` on preflight/adapter
  timeout (releases claim); finalize with response; receipts attached
  (`attach_delivery_receipt`, `_gobby_delivery_receipt`).
- `src/gobby/hooks/inbox.py`: drain loop (60 s) replays spool files via HTTP POST to
  own daemon with operator key; receipt acks: ghook overwrites envelope file with
  `{"kind":"delivery-receipt", receipt_id, delivery_generation}`; consumer claims by
  rename to `<name>.<owner>.claimed.tmp`, CAS `acknowledge_receipt` in hub DB,
  `apply_acknowledged_receipt` (marks P2P messages delivered, commits startup context,
  session variables), marks carrying envelopes processed; orphaned-claim restore;
  per-hook receipt sweep before carry-forward re-prepare (load-bearing). Drain also uses
  hub-DB/hub-state helpers: `grok_pending_context.handle_ack_pending_inbox_envelope`,
  `replay_fence.defer_live_hook` / `archive_superseded_hook`,
  `_terminalize_below_floor_receipts`, startup `drain_hook_inbox_barrier`.
- GAP: receipt acks on a node live on node disk; hub has no HTTP route to accept them.
  Without forwarding, receipts never terminalize and P2P messages re-inject forever
  (memory 1a2a5733). Needs a hub route (Python) + node forwarding — decide owner.
- Memory facts: f091327c (lease/409/503/STOP grace), 0ff9950e (STOP epoch),
  24214c83 (response_capability floor), c3abb06e (ghook dequeue semantics),
  1a2a5733 (receipt path), de3911bd (full envelope schema v1).

## Coordinator questions SENT to 15977 (msg da970ead, 2026-10-13) — awaiting answer

1. Expansion root (default proposed: root #21579, #24150 = P1, #24148 = P2).
2. Receipt-ack hub route (`POST /api/nodes/receipts`, key-bound Python, hub-mode 1.4
   style) mine or typed deferral to #21575/#24151? Recommended: mine.
3. Provider coverage for node v1: all five (claude, codex, grok, droid, agy) or subset?
4. Pane detection: planned as `kind: deferred` (object if active wanted).
5. FYI: node duties in `crates/gdaemon/src/nodes/...` modules (node-channel Decision
   8 precedent; `crates/AGENTS.md` family crates are for route families), scope
   `RunMode::Node` only.
6. Enhancer seat ref? None named.
DO NOT draft P2's spawn/SRT deliverable until Q3 answered and `ProviderSpawnPlan` read.

## Advisor plan shape (2026-10-13) — record in Decision Record

- Scope `RunMode::Node` only; hub/standalone keep Python spawn + ledger until
  #24149/#24151 (rung-1 boundary).
- Ledger wraps the relay; hub route unchanged. Node front door gains a native arm for
  `POST /api/hooks/execute` in node mode: claim -> forward via existing `HubRelay` with
  node key -> finalize on 2xx; release + retain on 503 retry; 409 passthrough/keep.
  Hub Python route still runs its own filesystem claim: separate machines, separate
  markers — explain why double-ledgering is consistent. ghook untouched (posts to
  GOBBY_DAEMON_URL = node front door).
- Marker storage = existing filesystem marker format under `hooks/inbox/processed/`
  (ghook lives there, `--diagnose` reads it). Rejected: SQLite/Postgres node store.
- STOP epoch: node ledger bumps `.stop_replay_epoch` when a forwarded session_start
  gets 2xx.
- Timers on the heartbeat host (decision 18; `crates/gdaemon/src/heartbeat.rs`): 60 s
  spool drain, 24 h marker prune, 30 s report, preparation-record retry.
- Freeze `POST /api/hooks/execute` via the S1.5 corpus `tests/contracts/http/` (not a
  new corpus): envelope schema v1 body (flat rejected), headers (X-Gobby-Envelope-Id,
  session/run ids, bearer), response_capability floor, 409 `{status, reason}` +
  malformed_marker, 503 `{status: retry, retry_kind}`, graceful 200 on client
  disconnect, `_gobby_delivery_receipt` on responses, receipt-ack file body.
- Terminal host on node = real deliverable: node-local `EpochAuthority` impl
  (`crates/gterminals/src/host.rs` ~169). Check overlap with open S2.8 leaves #23211,
  #23213, #23214 (shared Targets need ordering / Consumers unchanged).
- Tests drive a fake hub (`crates/gdaemon/tests/nodes.rs` channel harness). Live proof
  = typed deferral on hub-mode D2 (internal route) and D4.
- Skeleton: P1 hook ledger (contract freeze; node intercept claim/forward/finalize;
  spool drain + boot release of dead-pid processing markers + prune; STOP epoch;
  receipt forwarding + hub route if approved). P2 node agent duties (channel command
  dispatch in `run_client`; durable node state: accepted/cancel/preparation records +
  revision; workspace prep worktree/clone embedding run_id; provider command + SRT
  chokepoint; node terminal host + epoch authority; reports + checkout registration;
  cancel; terminal.write). Deferred: pane detection; live hub-to-node proof.
- Use Write/Edit for the plan file (Bash heredoc writes to .gobby/plans/*.md were
  blocked by the task gate for the hub-mode writer).

## Orchestrator rulings (#15977, 2026-10-13) — binding

Q1 root #21579, #24150 = P1, #24148 = P2. Q2 receipt route mine: `POST /api/nodes/receipts`
(key-bound, hub-mode 1.4 style). Q3 node v1 providers: claude, codex, grok; droid + agy =
kind:deferred section. Q4 pane detection kind:deferred. Q5 accepted. Q6 NO enhancer seat;
adversary Grok #16768 (resumed when plan validates). Q7 mine: replay-decision + receipts
routes sharing one decision function with the local drain. Q8 nodes keep Python CLI for
auth login + install; gdaemon only verifies and runs pinned SRT. Q9 accepted as v1 limits;
LIST them in the plan.

## Epoch-2 facts (2026-10-13, post-compaction reads)

- No answer yet to da970ead. Follow-up 475b2889 sent (Q7 hub replay/receipt routes
  mine, Q8 SRT install by `gobby install`, Q9 sandbox defaults FYI).
- ghook: spool `~/.gobby/hooks/inbox/{c|n}-<ts13>-<uuid>.json`; envelope id = file
  stem; `post_and_cleanup` sends Authorization from `gcore::local_token`
  (`read_api_key_for`: managed token, agent env token, else bootstrap.yaml api_key, so
  on a node = node key) + `X-Gobby-Envelope-Id`; 2xx Delivered (file kept, then
  `settle_delivered_inbox` writes receipt or deletes); typed 503 `{status:unavailable}`
  = Connect. `inject_machine_identity` puts machine_id in input_data.
- Node front door `FrontDoor::handle` Door::Node arm (front_door/mod.rs, 242 lines):
  challenge intercept then `node.relay.forward` / `splice`. HubRelay.forward keeps
  Authorization as-is (prepare_headers sets Host, strips break-glass).
- `serve::run` node branch builds doors + spawns `run_client`; `serve_doors` creates
  `HeartbeatHost::new()` internally and `start()`s it with no jobs ("Stage 1 has no
  timer-driven Rust jobs yet"); `register` frozen after start, 5 s BASE_CADENCE.
  serve_doors callers: serve.rs:116, :341; tests/nodes.rs:184, :811, :1942.
- File sizes: host.rs 916 (>850, keep out of Targets), nodes.rs test 81.6K (new test
  files instead), channel.rs 418, state.rs 184, serve.rs 504, front_door/mod.rs 242.
- gterminals pub API usable from gdaemon without edits: `HostSupervisor::new(options,
  Arc<dyn EpochAuthority>)`, `HostOptions` (pub fields + Default), `EpochAuthority`
  (only `bindings` required; authenticate refuses any binding whose machine_id differs),
  `ControlClient::new/hello/spawn/spawn_commit/kill/write/list/subscribe_events`
  (socket `<socket_dir>/gterm-control.sock`, token file `gterm-control.token`),
  `HostEventKind::TerminalExited{exit_code}`. Report `host_epoch` is a NUMBER; host
  epoch is a string -> node keeps a durable counter bumped on each new host epoch.
- S2.8 overlap: #23211 targets gterminals write.rs (DB coordinator), #23214 family.rs +
  tmux_runtime.rs, #23213 Python spawn executor. Node code in gdaemon/nodes/ shares none.
- hub-mode 2.2: hub refuses `hook_machine_mismatch` unless input_data.machine_id ==
  key machine; node session that misses its pre-created row is refused; #24151 owns
  registering it. 2.2 does NOT cover the drain fence.
- Python drain hub-state steps: `archive_superseded_hook` (replay_fence.py),
  `grok_pending_context.handle_ack_pending_inbox_envelope` (before processed check and
  after 2xx), `_terminalize_below_floor_receipts`, receipt `acknowledge_receipt` +
  `apply_acknowledged_receipt` + `_mark_carrying_envelopes_processed`
  (original/current envelope ids). Route `execute_hook` sweeps receipts before each hook
  (hooks.py:369) — node must forward its receipts before forwarding each hook.
- SRT Python: `srt_runtime.py` 956 + `sandbox_policy.py` 958 + `sandbox.py` 421 +
  resolvers; wrap argv = [node, runner, --settings, policy, --violations, viol, --,
  provider_exe, ...]; `render_srt_settings`; `prepare_sandbox_launch` (run paths,
  grok home, claude reviewer home, droid ps shim, cargo caches, verify install,
  preflight). Network profile: `definition_sandbox_config` — trusted = base +
  trusted_domains() seed + allow_git_network + allow_package_registries.
- `build_cli_command` (command_builder.py) per-provider flags; spawned agents always
  auto_approve=True. Worktree path Python: ~/.gobby/worktrees/<project>/<branch>;
  `repair_isolation_environment` copies CLI hooks, droid hooks, patches .mcp.json;
  hygiene excludes .mcp.json, .claude/, .codex/, .factory/hooks/hooks.json,
  .gobby/isolation.json.
- HTTP corpus: tests/contracts/http/*.json schema_version 2, manifest families
  parity/origin; one request per case; Python records from e2e daemon; Rust replays.
  No hooks family yet. Hub node routes (hub-mode 1.4 `routes/nodes.py`) not landed.

## Draft status (2026-10-13)

`.gobby/plans/node-halves.md` written: Overview, Decision Record (rulings 1-9 + writer
decisions 10-13), Constraints (v1 limits etc.), P1 sections 1.1-1.6 complete (1.1 corpus
freeze; 1.2 ledger `nodes/hooks/ledger.rs`; 1.3 Python `replay_decision.py` +
`routes/node_hooks.py` with `/api/nodes/hooks/replay` + `/api/nodes/receipts`; 1.4
`nodes/hooks/receipts.rs`; 1.5 `nodes/hooks/ingress.rs` + FrontDoor::handle; 1.6
`nodes/hooks/drain.rs` + serve_doors(heartbeat param) + serve::run). NOT yet validated.
MISSING: P2, D1 (droid+agy), D2 (pane detection), D3 (live proof, gated hub-mode D4),
V1 changelog, V2 verification.

## P2 design (to write)

Modules under `crates/gdaemon/src/nodes/agents/` (new mod.rs; nodes/mod.rs::* adds
`pub mod agents;` -> shared with 1.2, so P2 first section depends on 1.6 chain end).
Tests `crates/gdaemon/tests/node_agents.rs` (new). serve.rs::run shared with 1.6 ->
depend on 1.6.
- 2.1 durable node state `agents/state.rs`: one JSON file `<home>/node/agents.json`
  (atomic temp+rename+fsync, mutex): accepted, cancelled, preparation/workspace records,
  exits (oldest first), revision (never repeats across restart: persist before use),
  hub_epoch, host_epoch counter + last host epoch string. 1,000 accepted+live refusal.
- 2.2 command dispatch: extend `channel.rs::run_client` select loop: text frames parse
  `node_commands.rs` CommandFrame; unknown v -> unsupported_version; unknown command;
  invalid_payload; per-run_id serialization (map run_id -> mutex/queue); reply frames.
  agent.spawn acceptance (validate, 1,000 cap, cancel-record check -> `cancelled`,
  record accepted, reply {accepted:true} before prep). agent.cancel (record cancel
  before reply; not_found; stop prep). terminal.write via ControlClient::write
  (WriteKind text/key/input/paste; failure after write -> command_failed partial).
- 2.3 terminal host: `agents/terminal_host.rs`: HostOptions{socket_dir=home,
  binary_path=home/bin/gterm, machine_id, gobby_home, defaults} (rejected: config_store
  — node has no DB); `NodeEpochAuthority`: bindings -> Ok(vec![]) (single machine);
  observe_epoch -> bump host_epoch counter in state when epoch string changes;
  epoch_snapshot from state. Control connect: UnixStream `<home>/gterm-control.sock` +
  token `gterm-control.token` + ControlClient::hello. Started in serve::run node branch.
- 2.4 workspaces `agents/workspace.rs`: git CLI worktree/clone; path
  `<home>/worktrees/<project_id>/<run_id>` / `<home>/clones/<project_id>/<run_id>`;
  branch = payload branch_name if contains run_id else `<branch_name>-<run_id>`; base =
  payload base or source_root current branch; preparation record before first fs
  effect; idempotent removal; retry at startup + each report interval; repair: copy
  `.gobby/project.json`, provider hooks/MCP config per Python
  `isolation_repair.py::repair_isolation_environment` (claude/codex/grok only) +
  hygiene excludes (`isolation_git_hygiene.py`).
- 2.5 SRT policy+render `agents/sandbox_policy.rs`: port compute_sandbox_paths subset +
  `render_srt_settings` for claude/codex/grok; Trusted seed file shared (find path of
  `trusted_domains()` asset); golden test vs Python render for fixed inputs.
- 2.6 SRT launch `agents/srt.rs`: verify pinned install (content manifest sha256,
  `srt_install_root`), write settings.json + violations.jsonl 0600 under run dir,
  wrap argv [node, runner, --settings, policy, --violations, viol, --, exe, ...],
  grok home (`grok_sandbox_home.py`), preflight. Single chokepoint fn.
- 2.7 provider launch `agents/launch.rs`: build argv per provider (claude
  `--session-id`, model, `--effort`, `--dangerously-skip-permissions`, SRT `--settings
  {"sandbox":{"enabled":false}}`; codex `--ask-for-approval never --disable
  guardian_approval -C <dir> ... -c check_for_update_on_startup=false`; grok
  `--always-approve --no-alt-screen --cwd --model --reasoning-effort --single`),
  provider_args, prompt; env: 7 identity vars + GOBBY_DAEMON_URL (own front door) +
  GOBBY_PROMPT_FILE (node-local file) + GOBBY_MACHINE_ID; spawn via
  ControlClient::spawn + spawn_commit; timeout_seconds -> kill; TerminalExited ->
  exit record (exit_code), close pane; prep deadline -> prepare_timeout; failures ->
  launched:false exits; cancel of launched run -> kill TERM/KILL, close_terminal.
- 2.8 reports + checkouts `agents/reports.rs`: POST /api/nodes/report every 30 s
  (heartbeat) + on change; one at a time; fresh build, next revision; stale_report ->
  adopt hub_epoch, resend; exits until 200, max 1,000/report oldest first; agents from
  terminal host inventory (ControlClient::list). Checkouts: on hook ingress session_start
  2xx, resolve cwd's `.gobby/project.json` root -> POST /api/nodes/checkouts once per
  root (cache) -> edits ingress.rs (depends 1.5).
- Contract test list (#24148): out-of-order + retried reports, stale_report, >1,000
  pending exits, spawn at 1,000, failed removal, restart at each prep/removal boundary.
- Deferred: D1 droid+agy providers (Q3), D2 pane detection (Q4, hub-mode Decision 7),
  D3 live proof (gated hub-mode D4 / D2 internal route). Placeholder task_refs
  "TBD-...", owner "orchestrator".
- Granularity decisions needed on any section >6 items/files.

## Next steps

1. Next-epoch reads (extract only what's named): `crates/ghook/src/transport.rs`,
   `dispatch.rs` (spool filename grammar, 409/503 handling, receipt-file write);
   `.gobby/plans/agent-sandbox-profiles.md` + per-run `assets/settings.json` shape (if
   SRT policy is a materialized JSON file, Rust generates it and shells to pinned SRT);
   `src/gobby/agents/spawn_executor_providers.py::ProviderSpawnPlan` (per-provider
   argv/env table); `crates/gterminals/src/host.rs` HostOptions + pane-create path
   (Rust TerminalRuntime spawn seam); `crates/gdaemon/src/heartbeat.rs`;
   `tests/contracts/http/` corpus layout; `inbox_envelopes.py`, `replay_fence.py`,
   `grok_pending_context.py`, `storage/hook_receipts.py` (decide node-side handling).
2. Draft P1 + P2 contract-side pieces in `.gobby/plans/node-halves.md` (Write tool);
   then P2 spawn after Q3 answer.
3. Validate bare `uv run gobby plans validate .gobby/plans/node-halves.md -p .`;
   commit `[gobby-#24148] docs: ...` (task ref per root answer); send 15977 + 16768
   path, SHA, file hash.
