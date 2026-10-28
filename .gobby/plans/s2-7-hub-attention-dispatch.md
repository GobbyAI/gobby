Plan artifact: `.gobby/plans/s2-7-hub-attention-dispatch.md`

# Gobby 1.0 Stage 2.7 (hub half): attention, roster and response routing

**Plan ID:** s2-7-hub-attention-dispatch

## Overview
`kind: framing`

Implementation root: #24149. Planning assignment: #25257. Rust takes ownership of
the machine-scoped attention state: episode transitions on `attention_states`,
the (epoch, seq) ordering cursor, the in-memory attention metadata store, and
publication of the `attention_changed` and `attention_metadata_changed` agent
events. It also serves the four attention HTTP routes: the roster read model, seen,
metadata and respond. Respond routes the answer to the pane's own machine:
local panes go through the gterminals write coordinator and remote panes through
the node command channel.

The work lives in the agents family crate, `crates/gagents` (package
`gobby-agents`), which is statically linked into gdaemon under ROADMAP decision
16. It is registered as a RouteFamily the same way `crates/gruntime` is. The
hub's Python writers keep running. Each one becomes a client of a loopback
internal transition route, so Python stops holding any ordering state of its own.

The work is needed because the ledger still names Python for all four HTTP rows,
and every attention event is ordered by a Python in-memory cursor. The plan
reuses the existing pieces instead of adding new ones:

- the front-door family seams;
- the native WebSocket broker;
- gdaemon pane detection;
- the gterminals write coordinator;
- the node command registry.

The plan adds no schema change, no second cursor, no new queue and no new
WebSocket transport. Each deliverable below is one independently verifiable
outcome.

## Decision Record
`kind: framing`

The Orchestrator ruled on these questions on 2026-10-28, relayed by Triage
gobby#17480:

- **Q1, dispatcher scope.** The task phrase "dispatch gate" is obsolete. Commit
  `d8dd689d8d` retired the dispatcher drivers, and research #24782
  (`.gobby/plans/research/moot-tasks-after-astra-2026-10-21.md`) flagged it.
  Spawn eligibility and remote cancel go with their only consumers:
  - `src/gobby/hub/machine_routing.py::resolve_target_machine`, called from
    `src/gobby/mcp_proxy/tools/spawn_agent/_remote_routing.py`;
  - `src/gobby/hub/remote_cancel.py::cancel`, called from
    `src/gobby/agents/kill.py` and `src/gobby/hub/remote_runtime.py`.

  Those consumers are the spawn_agent and kill/stop rows, which #21564 owns
  behind the #21570 MCP flip. This plan ports only the remote write that respond
  uses. The Orchestrator corrects the task text at expansion.
- **Q2, machine scope.** Port the run/session join that gives each entry its
  machine and project. There is no schema change: `attention_states` has no
  machine column (`crates/gcore/assets/schema/baseline.sql`). A run entry takes
  `agent_runs.machine_id`. Its project is
  `COALESCE(child.project_id, parent.project_id)`.
- **Q3, write ownership.** Rust owns attention transitions, ordering, the
  metadata store and event publication. Every Python writer becomes a client of a
  loopback internal route. That route is guarded the same way as
  `crates/gdaemon/src/nodes/internal_api.rs::handle`: loopback peer only, plus a
  check of the `x-gobby-backend-secret` header. The Python pane lifecycle monitor
  keeps running and writes through that client. Retiring the monitor (option b)
  goes with #21564's run-lifecycle rows. The complete writer list is under
  4.3 below.
- **Q3 follow-up, reducer transaction boundary.**
  `TurnLifecycleReducer._apply` and `TurnLifecycleReducer.block_provider_failure`
  in `src/gobby/sessions/turn_lifecycle.py` currently do three things inside one
  transaction:
  1. `SELECT ... FOR UPDATE` on the attention row;
  2. the attention transition;
  3. the `sessions.status` write.

  A loopback call made while holding that row lock would deadlock against the
  native store, which writes on its own connection. The reducer therefore:
  - drops `FOR UPDATE` and reads the row plainly;
  - sends no transition when the mutation leaves the attention row unchanged
    (state, reason, kind, fingerprint and payload). A sent transition therefore
    always changes the row, and a not-applied result always means a conflict,
    so the store's no-op result and seq rules stay as they are;
  - otherwise sends a compare-and-set (CAS) transition carrying
    `expected_attention_id` and a new `expected_payload` (an absent row
    compares as `{}`);
  - retries at most three times, re-reading the row after each conflict;
  - writes `sessions.status` only through the status fence below.

  The atomicity this gives up is recorded as waiver
  `attention.get-roster.session-status-repair`.

  **Status fence.** Without a fence, two reducers can apply their CAS calls in
  one order and their status writes in the other. For example, A opens a turn,
  B enters a wait and writes `awaiting_input`, and then A writes `active`. So
  the status write runs in a short local transaction:
  1. lock the session row (`SELECT ... FROM sessions WHERE id = %s FOR UPDATE`);
  2. re-read the attention row in a new statement;
  3. write the status only when the committed `turn_lifecycle` still equals the
     lifecycle this reducer applied, or read when it sent no transition.

  Otherwise a later transition has superseded this one, so the reducer skips
  its write and the later reducer's own fenced write sets the status. Every
  reducer status write takes the session row lock first, and each reducer's
  CAS commits before its fence starts, so status writes land in CAS order. The
  fence never locks the attention row and makes no client call, so the native
  store never waits on Python.

  **Crash repair.** A crash between the CAS and the status write leaves the
  stored `turn_lifecycle` ahead of `sessions.status`. The session's next
  status-writing transition repairs it, because every non-duplicate mutation
  derives its own status. Replaying the same evidence repairs it through these
  duplicate paths:
  - `begin_turn` with the open turn's provider key returns
    `_status_for_waits(waits)`, or `active` when there are no waits. It does
    this only while the observed status still equals the lifecycle's
    `prior_status`, so a status another writer set since then, such as
    `awaiting_handoff`, stands.
  - `enter_wait` with a known token already returns `_status_for_waits(waits)`.
  - `record_provider_failure` for a failure already recorded in this
    generation returns `paused` when the observed status is `active`.
  - `end_turn` and `resumed_work` already return their own status on replay.
  - `resolve_wait` for a token that is already resolved keeps the observed
    status. From the stored lifecycle it cannot tell a stale awaiting status
    from one another writer set, so the turn's next status-writing transition
    repairs it.

  A crash with no later evidence for that session leaves the status stale until
  such evidence arrives. The waiver records this.

  If the three attempts are exhausted, the reducer returns a not-applied result
  with reason `attention_conflict`. It writes no status and adds no fallback
  path.
- **Q4, roster read model.** The roster stays one read-model query in gagents.
  This is recorded as a ROADMAP decision-16 exception. The reason is that the
  projection snapshot must come from one transaction under the cursor lock. The
  exception covers the joined reads of five other families' tables: `agent_runs`,
  `sessions`, `session_variables`, `tasks`, `terminals`, plus
  `provider_model_capabilities`. There is no #21562 edge. The Orchestrator flags
  the exception to Josh.
- **Q5, debt.** This plan carries its own "Debt not to port" section, which the
  adversary checks (memory 9066f1eb).
- **Q6, ownership of ledger rows.**
  - This plan implements the four HTTP rows.
  - CLI `gobby nodes` and `gobby nodes list` belong to #21571. Their service
    contract is the existing native `/api/machines` route in
    `crates/gdaemon/src/nodes/machines_api.rs`, documented in 4.4.
  - The roughly 130 rows naming #21564 are not in this plan.
- **Q7, pane capture.** Add a read-only `TmuxWrite::capture` in
  `crates/gterminals/src/tmux_write.rs` that reuses `live_target`'s generation
  check. Respond depends on #24573, which gives native terminal rows a runtime
  snapshot, and on #24580, which makes the terminal family native so Rust becomes
  the write owner.
- **Q8, ledger retention after removal.** Leaf 4.1 keeps a stale `delegated`
  ledger row the way recorded drops are kept, and passes the retained keys to
  `load_waivers`. A stale `proxied` row still fails. This plan owns the rule;
  other family plans bind 4.1 or its landed proof as a cross-plan edge.
- **Crate seam.** Pane detection, the node registry and the broadcast broker all
  live in gdaemon, so gagents cannot depend on gdaemon without a Cargo cycle.
  Instead, gagents defines one injected trait, `AttentionHost`, and gdaemon
  implements it once in `crates/gdaemon/src/attention_host.rs`. The trait covers
  publish, capture, detect, local write, remote write and machine ownership. The
  precedent is the `NodeEpochAuthority` injection in
  `crates/gdaemon/src/serve.rs`. Rejected alternatives:
  - putting the family in gdaemon, which breaks decision 16;
  - moving detection, the registry and the broker into shared crates, which is a
    wider blast radius for one consumer.

  gagents may depend directly on gobby-terminals' public API for
  `inventory::attach_locator` (#24575). Machine ownership and detection manifests
  belong to other families and are reached only through the host.
- **No-edit Python strategy.** Two Python names stay:
  - the public `AttentionStateManager` API, including the `(db, ...)` constructor;
  - the `AttentionMetadataStore` class name.

  Both get client bodies. Writer call sites and the large plumbing files
  therefore stay byte-identical: `src/gobby/agents/lifecycle_monitor.py` (974
  lines), `src/gobby/events/wake.py` (994), `src/gobby/cli/sessions.py` (886),
  `src/gobby/hooks/terminal_handoff_delivery.py` (903) and
  `src/gobby/servers/websocket/server.py` (828). The direct reads `get`,
  `list_blocked` and `snapshot` stay as plain database reads, because a read
  needs no ordering.
- **Rollout and rollback.** Native construction stays passive in production
  through P3: the family's default backend is `proxy`, and Python remains the
  sole writer and publisher. Only 4.3 activates native ownership, as one
  restart-bound release. That release changes four things together:
  1. the family default becomes `native`;
  2. Python writers become clients;
  3. the reducer moves to CAS;
  4. the Python cursor, publishers and routes are deleted.

  After 4.3 there is no Python owner left to fall back to. Rolling back means
  reinstalling the prior coherent release. Setting the family back to
  `proxy`/`compare` is not a supported rollback. Because an explicit
  `front_door.routes.attention` entry overrides the default in both directions,
  the takeover restart and any rollback reinstall both require that key to be
  absent (4.3's cutover preflight).
- macOS and Linux only. Windows is #25249. This plan has no latency gates.

## Pre-expansion capability bindings
`kind: framing`

These are implementation prerequisites carried as heading edges. Plan approval
alone does not satisfy any of them. Dispatch needs landed proof.

- **#21560, the Stage 2 phase gate** that blocks #24149. 1.1 binds it.
- **#21559, runtime family seams**: RouteFamily claims, `check_proxied`, the
  configuration snapshot, the gcore PostgreSQL pool and compare reporting. 2.1
  binds it.
- **#24963, shared parity waivers and readiness**, which is closed. It owns
  `docs/contracts/parity-waivers.json`, the loader in `scripts/parity_waivers.py`,
  corpus and compare classification, and the rule that readiness rejects
  all-Skipped results. 1.1 and 3.5 bind it.
- **#24970, shared domain errors and transport adapters**, which is closed.
  gagents maps its errors onto those seven classes and keeps the intentional
  Python envelopes through explicit adapters. 1.1 binds it.
- **#24964, the shared credentialed CORS/preflight boundary.** 3.1 binds it.
- **#24967, the shared gobby_session cookie verifier.** The web UI calls these
  routes from `web/src/hooks/useSessionAttention.ts`. 3.1 binds it.
- **#24575, the terminal attach locator** (`inventory::attach_locator` in
  gterminals) that the roster's terminal block uses. 3.1 binds it.
- **#24573, the runtime snapshot route** used to capture native terminal rows.
  3.4 binds it.
- **#24580, terminal family native activation**, which makes the gterminals
  coordinator the local write owner. 3.4 binds it.

The Orchestrator cycle-checks the cross-plan graph before expansion. #21564 may
later extend `crates/gagents`. This plan creates the crate in 2.1, and the
Orchestrator orders any competing creation edge when it does that cross-plan
check.

## Debt not to port
`kind: framing`

Each item was observed on the 0.5.0 checkout on 2026-10-28. Only items a parity
consumer can observe on a ledger surface get a #24963 waiver. IDs follow
`<family>.<surface-slug>.<behaviour-slug>`. The waiver loader accepts only exact
ledger keys, and outbound WebSocket events have no inventory key. So the event
ordering corrections cite the roster surface, which is where consumers reconcile
(epoch, seq) against those events.

- **Roster response cache.** `ATTENTION_ROSTER_CACHE_TTL_SECONDS = 1.0` in
  `src/gobby/servers/routes/attention.py` serves a payload keyed by cursor for up
  to one second. During that second, changes to runs, tasks or terminals do not
  appear. Not ported. Waiver `attention.get-roster.no-response-cache`, decision
  `.gobby/plans/s2-7-hub-attention-dispatch.md#3.1`. Replacement: every read
  recomputes the projection under the cursor.
- **Roster profiling.** `_RosterProfile` and `_log_roster_profile` only log
  internally. Dropped with no waiver.
- **Publish before commit.** `AttentionStateManager._publish` runs inside the
  transition, before the caller's outer transaction commits. A rolled-back
  transition can therefore emit a sequence number whose state the roster never
  returns. Not ported. Waiver `attention.get-roster.publish-after-commit`,
  decision `.gobby/plans/s2-7-hub-attention-dispatch.md#2.1`. Replacement: the
  sequence number is assigned and the event published only after the commit
  applies.
- **Independent cursorless managers.** Fallback constructions build managers
  that each have their own random epoch:
  - `src/gobby/servers/websocket/server.py` (the `TurnLifecycleReducer` wiring);
  - the `TurnLifecycleReducer.__init__` fallback;
  - `_attention_manager` in `src/gobby/hooks/terminal_handoff_delivery.py`.

  None of them publishes today, so their transitions emit no event. After 4.3
  all of them are clients of the one native cursor, so every transition
  publishes. The source stays unchanged. No waiver: the correction adds events
  no parity case records, and 4.3 asserts it.
- **Unconsumed WebSocket ordering properties.** `attention_epoch`,
  `attention_seq` and `attention_ordering_lock` in
  `src/gobby/servers/websocket/broadcast.py` have no consumers. Deleted with no
  waiver.
- **Unwired notification publisher.** The `notification_publisher` hook on
  `AttentionStateManager` is set only by tests: `tests/agents/test_attention_state.py`
  and `tests/events/test_wake_composer_attention.py`. Not ported. No waiver.
- **Whole-roster load to find one provider.** `_resolve_prompt_detector` loads
  the entire roster to learn a single provider. Native respond reads only that
  entry's row. No waiver: the response is unchanged.
- **Inject-then-clear test seam.** The respond `injector` test seam reverses the
  production order. Production local respond claims (clears by CAS) before it
  injects. The native port keeps the production order and drops the seam.
  Remote respond keeps its production order, which is inject first and then
  clear by CAS. No waiver.
- **Full catalog read per provider.** `_model_display_names` reads the whole
  `provider_model_capabilities` catalog for every provider. Native reads only
  the rows matching the providers in the roster. No waiver: the output is
  unchanged.
- **Reducer atomicity (Q3 follow-up).** This is the deliberate loss of the
  single-transaction attention and status write. Waiver
  `attention.get-roster.session-status-repair`, decision
  `.gobby/plans/s2-7-hub-attention-dispatch.md#4.3`. Replacement: CAS first, then
  a status write fenced on the committed lifecycle. A crash between them is
  repaired by the next status-writing transition, or by replay through the
  duplicate paths the Decision Record names.
- **Obsolete dispatcher phrase (Q1).** Recorded in the Decision Record. No
  surface.

## Constraints
`kind: framing`

- **File size.** Every production file stays below 1,000 lines. Inline Rust
  test modules count against that limit, so tests go in external
  `crates/gagents/tests/*.rs` files. `src/gobby/runner_init/orchestration.py`
  (896 lines) is relieved by the move in 4.3. No other targeted production file
  is at or above 850 lines.
- **Releases.** Each production Rust leaf bumps every affected shipped binary by
  one patch from its executor's starting tip, and updates `Cargo.lock` and
  `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`. It validates
  them with `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
  - Root workspace changes (2.1) affect gcode, gdaemon and ghook.
  - Changes confined to gagents, gterminals or gdaemon affect gdaemon only.
  - 2.2 changes no linked binary until 2.3 adds the dependency, so it gets no
    bump.
  - Unpublished gclient, gmcp and gterm get no bump.
- **Live installs.** The release owner promotes through
  `promote_workspace_binary_set`. Only the Orchestrator delegates a live
  cutover. 4.3 is restart-bound: announce it globally, wait for quiet, then
  promote the coherent set. This plan's author runs no cutover.
- **Commands.** Python runs through uv. Pytest uses the isolated hub:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <focused-path>`.
  Never run the full suite. Cargo runs set `CARGO_BUILD_JOBS=3`, name the
  package and use `cargo nextest run -p <package>`, since #24949 is closed.
  - gagents database tests use their own `crates/gagents/tests/common/mod.rs`.
    It mirrors `crates/gruntime/tests/common/mod.rs`: it reads `DATABASE_URL`,
    skips when that is unset, and requires `GOBBY_TEST_PROTECT=1`.
- **Topology.** The loopback attention routes are served on the hub and
  standalone doors only. The node door returns not-found for them, as
  `crate::nodes::internal_api::is_route` already does for the node routes. Every
  named Python writer runs inside the hub or standalone Python process, because
  node mode runs no Python daemon (`.gobby/plans/node-mode.md`). Any node-side
  lifecycle writes belong to #21564 under Q3(b).
- **Security.** Authentication, project authorization, the respond ownership
  check (`x-gobby-user-id` against `machines.owner_user_id`) and answer
  validation are all preserved. The internal routes trust no forwarded peer
  header, API key or cookie. Remote respond writes only through the existing node
  command channel.
- **Workflow events.** No workflow event names are invented. Any rule
  integration uses semantic events such as `turn_start`/`turn_end`.
- **Literal sweeps** observed on 0.5.0 (2026-10-28):
  - `gcode grep -w AttentionMetadataStore src/ -m 30`
  - `gcode grep -F 'Backend-Secret' src/gobby -m 20`
  - a file list of every test naming `AttentionStateManager`,
    `AttentionMetadataStore`, `configure_attention_*`, `attention_roster` or
    `routes.attention` (22 files, each listed in 4.3)
  - a check of every Python writer's transition call site and its exception
    handling (named in 4.3)

## Parity ownership
`kind: framing`

| Ledger row | Outcome owner |
| --- | --- |
| HTTP GET /api/attention/roster | 3.1.1-3.1.3 |
| HTTP POST /api/attention/{entry_id}/seen | 3.2.1 |
| HTTP POST /api/attention/{entry_id}/metadata | 3.2.2 |
| HTTP POST /api/attention/{entry_id}/respond | 3.4.1-3.4.7, 3.4.9 |
| CLI gobby nodes | #21571; 4.4 service contract over native /api/machines |
| CLI gobby nodes list | #21571; 4.4 service contract over native /api/machines |

The four HTTP rows in `docs/contracts/parity-ledger-status.d/sessions-agents.json`
change from `proxied` to `delegated` in 4.3, in the same commit that deletes
their Python handlers. 4.1 is what keeps those delegated rows in the ledger after
the Python surface is gone. The two CLI rows in
`docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json` stay `proxied`
with #21571 owner notes. Statuses are exactly `proxied`, `delegated` or
`dropped by decision #N`.

## P1: Characterize before porting
`kind: framing`

### 1.1 Characterize attention behavior before takeover [category: test] (depends: #21560, #24963, #24970)
`kind: deliverable`

Targets:
- `tests/contracts/attention/transitions.json`
- `tests/contracts/attention/events.json`
- `tests/contracts/attention/roster.json`
- `tests/contracts/attention/respond.json`
- `tests/contracts/http/attention.json`
- `tests/contracts/http/manifest.json::*` — scope-reason: register the attention corpus with Python origin and proxy parity
- `tests/contracts/test_attention_oracle.py`
- `docs/contracts/parity-waivers.json::*` — scope-reason: append the three attention waiver entries

**Research context.** These are the oracles:
- `src/gobby/storage/attention.py::AttentionStateManager`, in particular
  `_transition_locked`, `_sweep_empty_rows_locked`, `snapshot` and
  `load_roster_rows`;
- `src/gobby/agents/attention_metadata.py::AttentionMetadataStore`;
- `src/gobby/servers/routes/attention.py::create_attention_router`, covering
  roster, mark_seen, set_metadata, respond and answer_remote;
- `src/gobby/servers/routes/attention_roster.py::_load_roster_entries`;
- the event closures in `src/gobby/runner_init/orchestration.py`, together with
  the metadata enrichment in `src/gobby/servers/websocket/broadcast.py`.

Record vectors with a frozen clock and fixed UUIDs, using the isolated hub.

Transition vectors cover:
- an absent row inserting only when the state is blocked or the payload is
  non-empty;
- `ON CONFLICT DO NOTHING`;
- `turn_lifecycle` carried from the existing payload, and a clear that keeps
  only `turn_lifecycle`;
- unchanged no-ops;
- a new episode getting a new `attention_id` and `since`;
- `expected_attention_id`, `expected_fingerprint` and `mark_seen` mismatches;
- fingerprint retention on clear;
- the empty-row sweep (256 rows, 60 s).

Add the new `expected_payload` vectors that 2.1 implements: an absent row
compares as `{}`, and a mismatch applies nothing. These have no Python oracle
and are marked native expectations.

Event vectors record the exact agent-event message: `type`, `event`, `run_id`
(the run id, or the entry id), `parent_session_id` as `""`, `timestamp`,
`session_id`, null `mode`/`provider`/`pid`, the spread `event_payload` fields,
and `metadata` when the store has it. They also record the metadata event keyed
by `entry_id`.

Roster vectors cover:
- run and session halves, and the skip rules;
- seat titles and claimed tasks;
- model display names (canonical or alias match, ordered
  `is_default DESC, display_name, canonical_model`);
- local terminal blocks and null remote terminal blocks;
- tmux blocks, metadata, and sorting by `entry_id`.

Respond vectors cover answer validation and every error branch:
- 503 `attention_unavailable`;
- 404 `attention_not_found`;
- 409 `stale`, `not_actionable` and `prompt_changed`;
- 422 `invalid_option` carrying the options;
- 403 `machine_not_owned` and `machine_not_found`;
- 502 `injection_failed` and `injection_indeterminate`, with their stages;
- 503 node-refused `{code, stage: "none"}` and `node_offline`.

Remote cases use the `transport` parameter of
`src/gobby/hub/node_client.py::NodeCommandClient`. Also record pane fingerprint
vectors for `prompt_changed`, so 3.4 can prove the Rust fingerprint matches
Python's. Record the local injection stage for each write failure.

Normalize only generated ids, clock fields and measured durations. Statuses,
errors, ownership and stages are never normalized.

Add the three waivers from "Debt not to port" to the shared manifest. Each
entry has exactly `id`, `surface` (`HTTP GET /api/attention/roster`),
`behaviour`, `decision` and `replacement`. The Python observations stay as
recorded.

Observed: source inspection only. These tests have not run.
Planned: isolated pytest of `test_attention_oracle` and HTTP corpus recording.

**Acceptance:**
- 1.1.1 - Transition vectors reproduce every `_transition_locked` branch and the bounded sweep, and mark the native-only `expected_payload` expectations. test: `tests/contracts/test_attention_oracle.py::test_transition_vectors_match_python`.
- 1.1.2 - Event vectors pin both agent-event messages byte for byte, including the empty and null fields and the metadata enrichment. test: `tests/contracts/test_attention_oracle.py::test_event_vectors_match_python`.
- 1.1.3 - Roster vectors reproduce the projection, skip rules, display names, terminal and tmux blocks, metadata and ordering. test: `tests/contracts/test_attention_oracle.py::test_roster_vectors_match_python`.
- 1.1.4 - Respond vectors cover answer validation, every local and remote error branch with its stage, ownership for both local and remote panes, and the pane fingerprint vectors. test: `tests/contracts/test_attention_oracle.py::test_respond_vectors_match_python`.
- 1.1.5 - The HTTP corpus records the four routes and their error branches with Python origin. file: `tests/contracts/http/attention.json`.
- 1.1.6 - The shared manifest carries the three attention waivers, each with an exact ledger surface, behaviour, decision and replacement. file: `docs/contracts/parity-waivers.json`.

## P2: Native store, cursor and publication
`kind: framing`

### 2.1 Native attention store, episode transitions and ordering cursor [category: code] (depends: 1.1, #21559)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gagents/Cargo.toml`
- `crates/gagents/src/lib.rs`
- `crates/gagents/src/store.rs`
- `crates/gagents/src/cursor.rs`
- `crates/gagents/tests/common/mod.rs`
- `crates/gagents/tests/store.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pins for the workspace-member addition

**Research context.** Create `crates/gagents` (`publish = false`, inheriting the
workspace version) and add it to the root `members` list. Port the
`AttentionState` row type and the `event_payload` field set (epoch, seq,
entry_id, run_id, session_id, attention_id, state, reason, kind, fingerprint,
payload, since, seen_at). Port `_transition_locked` onto the gcore async pool
and transaction seam, matching 1.1's vectors, and add `expected_payload` to the
UPDATE predicate. An absent row compares as `{}`.

The cursor holds one process epoch (a v4 UUID) and a sequence number behind one
async mutex. A transition runs in this order:
1. take the mutex;
2. run the transaction (bounded sweep, then the conditional insert or update);
3. commit;
4. if the transition applied, increment seq and hand the event to the
   publisher;
5. release the mutex.

Holding the mutex across the database I/O serializes transitions exactly as
Python's `AttentionOrderingCoordinator` does. It is also what lets a roster
snapshot taken under the mutex match the states it returns. Publishing after
commit is what `attention.get-roster.publish-after-commit` cites.

The publisher is a constructor parameter. 2.3 wires the native broker, and tests
record events. Apply the 256-row, 60-second empty-row sweep inside the same
transaction, as Python does.

There is no in-memory state cache, retry loop or second lock.

Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test store`.

**Granularity:** The episode state machine and the ordering cursor are separate
owners, but the cursor orders only this store's committed transitions and has no
other caller. Splitting them would mean a seq contract with nothing to apply it
to.

**Acceptance:**
- 2.1.1 - Every 1.1 transition vector produces Python's row and applied result. test: `crates/gagents/tests/store.rs::transition_vectors_match_python`.
- 2.1.2 - `expected_payload` CAS is exact: an absent row compares as `{}`, and a mismatch writes nothing and assigns no seq. test: `crates/gagents/tests/store.rs::expected_payload_cas_is_exact`.
- 2.1.3 - Seq increments only for applied transitions, after commit, under the cursor mutex, and a failed transaction publishes nothing. test: `crates/gagents/tests/store.rs::cursor_assigns_after_commit`.
- 2.1.4 - The empty-row sweep deletes at most 256 rows per 60 seconds, in the transition's transaction. test: `crates/gagents/tests/store.rs::empty_row_sweep_is_bounded`.
- 2.1.5 - gcode, gdaemon and ghook receive this leaf's patch bumps, lock and pins. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.2 Native attention metadata store [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gagents/src/metadata.rs`
- `crates/gagents/src/lib.rs`
- `crates/gagents/tests/metadata.rs`

**Research context.** Port
`src/gobby/agents/attention_metadata.py::AttentionMetadataStore`:
- `set` and `clear`;
- `get` with time-to-live expiry;
- `snapshot`, which the roster uses;
- the validators `validate_metadata_text` (at most 120 characters, a string)
  and `validate_metadata_ttl_ms` (an integer, not a bool, at most 600,000).

The store is in memory and shares 2.1's cursor. Each published change takes the
next seq under the same mutex. A clear publishes only when an entry existed.
Event bodies follow 1.1's metadata vectors. No linked binary changes until 2.3,
so this leaf has no release targets.

Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test metadata`.

**Acceptance:**
- 2.2.1 - Set, get, clear, expiry and validation match the Python metadata vectors. test: `crates/gagents/tests/metadata.rs::metadata_vectors_match_python`.
- 2.2.2 - Metadata changes take the next shared cursor seq and publish 1.1's event shape. A clear of an absent entry publishes nothing. test: `crates/gagents/tests/metadata.rs::metadata_changes_take_cursor_seq`.

### 2.3 Broker publication and loopback writer routes [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gagents/src/host.rs`
- `crates/gagents/src/events.rs`
- `crates/gagents/src/internal.rs`
- `crates/gagents/src/lib.rs`
- `crates/gagents/tests/internal.rs`
- `crates/gdaemon/src/attention_host.rs`
- `crates/gdaemon/src/lib.rs::*` — scope-reason: declare the attention host module
- `crates/gdaemon/src/serve.rs::*` — scope-reason: create the broadcast broker before the family list and construct the attention service and host
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: add the attention service to FrontDoorState and dispatch loopback attention routes on hub and standalone doors only
- `crates/gdaemon/tests/attention_internal.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the gdaemon attention host

**Research context.** Define `AttentionHost` in gagents. In this leaf it has one
method, `publish(message)`. 3.4 adds capture, detect, local write, remote write,
local machine id and machine ownership.

`events.rs` builds 1.1's exact agent-event message:
- `parent_session_id` is `""`;
- `mode`, `provider` and `pid` are null;
- `run_id` is the run id, or the entry id when there is none;
- `metadata` is included when the metadata store has the entry.

That last point ports `broadcast.py`'s enrichment.

gdaemon's `DaemonAttentionHost::publish` calls
`crate::websocket::broker::BroadcastBroker::publish`, with the message as the
filter and its JSON as the frame, matching how
`src/gobby/servers/websocket/broadcast.py` sends to `/_gobby/ws/publish`. In
`serve.rs`, build the `BroadcastBroker` before the family list and pass the same
broker to `NativeWs::new`. Construct the attention service only when a pool
exists, the same way `RuntimeFamily` is constructed.

`internal.rs` serves these routes:
- `POST /api/internal/attention/transitions`. The body is the
  `AttentionStateManager.transition` keyword set plus `expected_payload`. The
  response is `{"applied": bool, "current": <state or null>}`.
- `PUT /api/internal/attention/metadata/{entry_id}`, with body `{text, ttl_ms}`,
  returning `{"metadata": {...}}`.
- `DELETE /api/internal/attention/metadata/{entry_id}`, returning
  `{"cleared": bool}`.

`front_door/mod.rs` dispatches these routes next to
`crate::nodes::internal_api::is_route`. Admission happens before any path or
body parsing: the peer must be loopback and `x-gobby-backend-secret` must match
`AuthState::backend_secret_matches`. Otherwise the route returns not-found. The
node door also returns not-found.

These routes are passive: no Python caller uses them until 4.3. Validation errors
use #24970's validation class.

Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test internal`
and `cargo nextest run -p gobby-daemon --test attention_internal`.

**Granularity:** Publication, service construction and the guarded loopback
routes are one outcome. The routes apply changes through the same constructed
service that publishes them, so one end-to-end test proves all three. Splitting
them would land either a publisher that no writer reaches or a route whose
changes reach no broker, and neither half would have a consumer to verify it.

**Acceptance:**
- 2.3.1 - Applied transitions and metadata changes publish 1.1's exact event messages through the native broker, in seq order. test: `crates/gdaemon/tests/attention_internal.rs::events_reach_the_native_broker_in_seq_order`.
- 2.3.2 - The loopback routes apply transitions and metadata changes and return the documented bodies. Invalid bodies get the shared validation error. test: `crates/gagents/tests/internal.rs::internal_routes_apply_and_validate`.
- 2.3.3 - Non-loopback peers, a missing or wrong backend secret, and the node door all receive not-found before body parsing. test: `crates/gdaemon/tests/attention_internal.rs::internal_routes_admit_only_loopback_secret`.
- 2.3.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P3: HTTP family
`kind: framing`

### 3.1 Attention family and roster read route [category: code] (depends: 2.3, #24575, #24964, #24967)
`kind: deliverable`

Targets:
- `crates/gagents/src/family.rs`
- `crates/gagents/src/roster.rs`
- `crates/gagents/src/lib.rs`
- `crates/gagents/Cargo.toml`
- `crates/gagents/tests/roster.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: push the attention family into the family list
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the attention family

**Research context.** `AttentionFamily` implements
`crates/gcore/src/route_family.rs::RouteFamily`. Its bootstrap key is
`front_door.routes.attention` and its default backend is `proxy` until 4.3. It
claims `GET /api/attention/roster` here, and 3.2 and 3.4 extend the claims. It
reuses #21559's principal admission, #24967's verifier for both gobby_session
cookie classes, and #24964's outer CORS boundary. It adds no family-local
verifier or CORS code.

`roster.rs` ports `load_roster_rows` and `_load_roster_entries` as one read
transaction taken under the 2.1 cursor mutex. The response's epoch and seq
therefore always equal the states it returns. The query reads:
- non-null and `provider_error` states;
- the run half: queued, running and pending `agent_runs`, the child and parent
  sessions, the `session_variables` seat title, the lateral claimed task, and
  the terminal;
- the session half: live sessions.

Model display names come from `provider_model_capabilities`, limited to the
roster's providers. The terminal block comes from gobby-terminals
`inventory::attach_locator` (#24575) and is null for a remote machine. The tmux
block comes from the terminal context. Metadata comes from 2.2's snapshot.
Entries are sorted by `entry_id`, and a session entry is skipped when its run
already appears or it has neither a terminal nor a tmux pane.

The roster has no response cache and no profiling. The no-cache behaviour cites
`attention.get-roster.no-response-cache`.

Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test roster`.

**Acceptance:**
- 3.1.1 - GET /api/attention/roster reproduces every 1.1 roster vector. test: `crates/gagents/tests/roster.rs::roster_matches_python`.
- 3.1.2 - Under concurrent transitions, the returned epoch and seq always equal the states returned. test: `crates/gagents/tests/roster.rs::roster_snapshot_is_consistent`.
- 3.1.3 - A run, task or terminal change is visible on the very next read, asserting `attention.get-roster.no-response-cache`. test: `crates/gagents/tests/roster.rs::roster_reads_are_uncached`.
- 3.1.4 - Family claims, key and cookie principals, and CORS are bounded to the claimed routes, and the default backend is proxy. test: `crates/gagents/tests/roster.rs::claims_and_auth_are_bounded`.
- 3.1.5 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.2 Seen and metadata routes [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gagents/src/http_updates.rs`
- `crates/gagents/src/family.rs`
- `crates/gagents/src/lib.rs`
- `crates/gagents/tests/updates.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the seen and metadata routes

**Research context.** Seen ports `mark_seen`:
- get the entry, returning 404 when it is missing;
- check the episode identity, returning 409 `stale` on mismatch;
- run a blocked transition with `mark_seen` and `expected_attention_id`;
- a mark that is already seen is idempotent.

Metadata ports `set_metadata`:
- strict body types;
- the 2.2 validators, with 422 shapes from 1.1;
- the response `{"status": "updated", "entry_id", "metadata"}`.

Both routes publish through the 2.1/2.2 cursor.

Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test updates`.

**Acceptance:**
- 3.2.1 - POST /api/attention/{entry_id}/seen matches the Python vectors for found, missing, stale and already-seen entries, and publishes one event per applied mark. test: `crates/gagents/tests/updates.rs::seen_matches_python`.
- 3.2.2 - POST /api/attention/{entry_id}/metadata matches the Python validation, response and event vectors. test: `crates/gagents/tests/updates.rs::metadata_matches_python`.
- 3.2.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.3 Read-only tmux pane capture [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gterminals/src/tmux_write.rs::*` — scope-reason: add read-only TmuxWrite::capture beside the write path, reusing live_target
- `crates/gterminals/tests/tmux_capture.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the gterminals capture addition

**Research context.** Add `TmuxWrite::capture(&self, terminal) ->
Result<Option<String>, WriteFailure>`:
1. resolve the pane through the existing private `live_target`, including its
   generation check;
2. run `capture-pane -p` for the last 15 lines through the existing
   `TmuxCommands::run` seam.

A stale or missing target returns `None` and runs no capture. The argv contains
no write, resize or control command. The fake `TmuxCommands` in the existing
tests is the model for the test double.

Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-terminals --test tmux_capture`.

**Acceptance:**
- 3.3.1 - Capture reads the last 15 lines only after `live_target` confirms the generation. A stale or missing target returns `None` without running capture. test: `crates/gterminals/tests/tmux_capture.rs::capture_reads_only_a_live_target`.
- 3.3.2 - Capture issues only the read-only `capture-pane` argv. test: `crates/gterminals/tests/tmux_capture.rs::capture_is_read_only`.
- 3.3.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.4 Respond route with local and remote delivery [category: code] (depends: 3.3, #24573, #24580)
`kind: deliverable`

Targets:
- `crates/gagents/src/respond.rs`
- `crates/gagents/src/host.rs`
- `crates/gagents/src/family.rs`
- `crates/gagents/src/lib.rs`
- `crates/gagents/tests/respond.rs`
- `crates/gdaemon/src/attention_host.rs`
- `crates/gdaemon/src/pane_detection/prompt.rs::*` — scope-reason: widen pane_fingerprint to crate visibility only; its existing callers stay unchanged
- `crates/gdaemon/src/pane_detection/mod.rs::*` — scope-reason: re-export pane_fingerprint at crate visibility for the attention host
- `crates/gdaemon/tests/attention_host.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for native respond

**Research context.** Port `respond` and `answer_remote` from
`src/gobby/servers/routes/attention.py`.

**Answer.** Exactly one of:
- `option`: an integer, not a bool;
- `text`: at most 2,048 UTF-8 bytes, with no control characters other than
  `\n`, and no DEL;
- `key`: one of `enter`, `escape`, `tab`, `up` or `down`.

Unknown fields are rejected. A text answer writes with submit; a key answer
writes the key kind.

**Steps, under a per-entry async lock:**
1. 503 `attention_unavailable` when there is no store.
2. 404 when the entry is missing.
3. 409 `stale` when the identity does not match.
4. 409 `not_actionable` when the kind is not actionable or the reason is
   `composer_blocked`.
5. 422 `invalid_option` with the options when the option is not offered.
6. Resolve the pane: the session's live terminal first, otherwise the run's
   terminal. With no pane, return 502 `injection_failed` at stage `none`.
7. Check machine ownership through the host for every resolved pane, local or
   remote, before any capture, detection or CAS (403 `machine_not_owned` or
   `machine_not_found`). This matches Python, which calls `_require_pane_owned`
   before it branches. A pane on another machine then goes through remote
   delivery.

**Remote delivery.** Write through the node registry's `terminal.write` command,
then clear the episode by CAS:
- a node refusal at stage `none` with a code returns 503 `{code, stage: "none"}`
  and leaves the episode open;
- an offline node returns 503 `node_offline`;
- a partial write clears the episode and returns 502 `injection_indeterminate`.

**Local delivery.**
1. Capture the pane: `TmuxWrite::capture` for tmux rows, and #24573's snapshot
   for native rows. If nothing is captured, return 502 at stage `none`.
2. Re-read the entry and re-check its identity.
3. Run the provider's detector. The provider comes from the entry's own row,
   not the whole roster, and the host loads only
   `SELECT provider_id, content FROM detection_manifests WHERE provider_id = $1`.
   With no detector, return 503.
4. If the pane fingerprint does not match, retire and redetect, then return
   409 `prompt_changed` with the moved identity.
5. Claim the episode by CAS clear.
6. Inject through the gterminals `Coordinator::write` with
   `Origin::Attention` and action key `attention-respond:<attention_id>`.

Outcome mapping: Delivered returns `{"status": "accepted", "entry_id"}`.
Uncertain is stage `partial` and returns 502 `injection_indeterminate`.
Suppressed, Quarantined and Refusal are stage `none` and return 502
`injection_failed`.

After a local failure, redetect the retired episode, following 1.1's recorded
stages.

**Fingerprint.** `pane_fingerprint` becomes `pub(crate)` and is re-exported from
`pane_detection`. It must equal Python's fingerprint vectors, or every respond
would 409.

**Host methods added here.** `capture`, `detect`, `write_local`, `write_remote`,
`local_machine_id` and `require_machine_owned`. Ownership checks the
`x-gobby-user-id` header against `machines.owner_user_id`, the same way
`crates/gdaemon/src/nodes/machines_api.rs` resolves its caller.

Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test respond`
and `cargo nextest run -p gobby-daemon --test attention_host`.

**Granularity:** Respond is one route, but it has a long error contract. Each
acceptance item covers one distinct failure class, so a regression points to
its own branch. The host adapters live in gdaemon because of the Cargo seam.
They are tested with this route because respond is their only consumer.

**Acceptance:**
- 3.4.1 - Answer validation matches the 1.1 vectors for option, text and key, including the byte and control-character bounds and unknown fields. test: `crates/gagents/tests/respond.rs::answer_validation_matches_python`.
- 3.4.2 - Unavailable, missing, stale, not-actionable and invalid-option errors match the 1.1 bodies and statuses. test: `crates/gagents/tests/respond.rs::identity_errors_match_python`.
- 3.4.3 - Local respond captures, re-checks identity and the fingerprint, and claims by CAS before it injects with the attention origin and action key. test: `crates/gagents/tests/respond.rs::local_respond_claims_before_inject`.
- 3.4.4 - Local failures map write outcomes to the recorded stages and redetect the retired episode. A fingerprint mismatch returns `prompt_changed` with the moved identity. test: `crates/gagents/tests/respond.rs::local_failures_redetect_and_map_stages`.
- 3.4.5 - Remote respond enforces machine ownership, writes through the node registry, and matches the refused, offline and indeterminate vectors. test: `crates/gagents/tests/respond.rs::remote_respond_matches_python`.
- 3.4.6 - Concurrent responds to one entry deliver at most one write. test: `crates/gagents/tests/respond.rs::concurrent_respond_delivers_once`.
- 3.4.7 - Host adapters capture tmux and native rows, load only the provider's detection manifest, and reproduce Python's pane fingerprint vectors. test: `crates/gdaemon/tests/attention_host.rs::host_adapters_match_python`.
- 3.4.8 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 3.4.9 - A local pane on a machine the caller does not own, or on a missing machine, gets the 1.1 403 body with no capture, detection, CAS clear or injection. test: `crates/gagents/tests/respond.rs::local_respond_requires_machine_ownership`.

### 3.5 Side-effect-safe comparison and native corpus replay [category: code] (depends: 3.4, #24963)
`kind: deliverable`

Targets:
- `crates/gagents/src/compare.rs`
- `crates/gagents/src/family.rs`
- `crates/gagents/src/lib.rs`
- `crates/gagents/tests/compare.rs`
- `tests/contracts/http/manifest.json::*` — scope-reason: mark the proven attention cases native with Python origin
- `tests/e2e/test_attention_family.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the attention compare verifier

**Research context.** Implement `RouteFamily::check_proxied` for the roster.
The comparison normalizes only:
- epoch and seq, which come from different processes in compare mode;
- `since` and `seen_at`;
- generated ids.

Everything else must be equal, or must be a cited #24963 waiver reported as
Waived.

Mutations (seen, metadata and respond) are never sent to the native leg in
compare mode. Python stays the sole writer and publisher until 4.3, so the native
parity of those mutations is proven by isolated native replay of the 1.1 corpus
instead.

The e2e test starts an isolated daemon and replays the attention corpus against
the native family. Readiness needs at least one ordinary Passed case per route.
A report that is entirely Skipped fails.

The same e2e then restarts the isolated daemon against the same database and
asserts:
- the roster reports a new epoch, with seq starting over under it;
- the roster still returns the durable rows the replay left;
- the first post-restart event carries the new epoch. A roster read after it,
  which is the reconciliation that `web/src/hooks/useSessionAttention.ts`
  performs on an epoch change, returns that epoch and includes the event's
  change.

Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-agents --test compare`
and isolated pytest of `tests/e2e/test_attention_family.py`.

**Acceptance:**
- 3.5.1 - Roster comparison normalizes only the cursor, clock fields and generated ids, and reports any other difference unless it cites a waiver. test: `crates/gagents/tests/compare.rs::roster_compare_normalizes_cursor_only`.
- 3.5.2 - In compare mode, mutations reach Python only and are never applied twice. test: `crates/gagents/tests/compare.rs::compare_mutations_call_python_only`.
- 3.5.3 - Native replay of the four routes passes with ordinary Passed cases per route, and asserts the cited waivers. test: `tests/e2e/test_attention_family.py::test_native_attention_corpus_matches`.
- 3.5.4 - After an isolated daemon restart on the same database, the roster reports a new epoch with seq starting over and keeps the durable rows, and the first post-restart event reconciles through an epoch-change roster read. test: `tests/e2e/test_attention_family.py::test_restart_resets_cursor_and_keeps_rows`.
- 3.5.5 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P4: Native takeover and Python retirement
`kind: framing`

### 4.1 Keep delegated ledger rows after Python surface removal [category: code] (depends: 3.5)
`kind: deliverable`

Targets:
- `scripts/generate_parity_ledger.py::*` — scope-reason: retain recorded delegated rows whose Python surface was removed, as recorded drops are retained
- `tests/test_parity_ledger.py::*` — scope-reason: cover delegated-row retention and waiver surfaces that remain valid after removal

**Research context.** The renderer in `scripts/generate_parity_ledger.py` raises
"Mapping references absent surfaces" for any recorded status whose surface left
the live Python inventory, unless that status is `dropped by decision`.
`scripts/parity_waivers.py::load_waivers` likewise requires a behaviour waiver
to cite a live inventory key. Deleting `src/gobby/servers/routes/attention.py`
in 4.3 removes the four HTTP surfaces, which would break both the ledger and the
three attention waivers.

Retain a stale surface whose recorded status is `delegated`, exactly as drops are
retained. Pass the retained keys to `load_waivers` as part of its `surfaces`. A
stale `proxied` row still fails. This is a shared-tool rule that every family's
deletion leaf needs, so it changes no family-specific behaviour. The Orchestrator
ruled on 2026-10-28 (Q8) that this plan owns the rule, and other family plans
bind 4.1 or its landed proof as a cross-plan edge.

Planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/test_parity_ledger.py`.

**Acceptance:**
- 4.1.1 - A delegated row whose Python surface is removed stays in the rendered ledger, while a stale proxied row still fails. test: `tests/test_parity_ledger.py::test_delegated_rows_survive_python_removal`.
- 4.1.2 - A behaviour waiver that cites a retained delegated surface loads, and one that cites an unknown surface fails. test: `tests/test_parity_ledger.py::test_waivers_accept_retained_delegated_surfaces`.

### 4.2 Loopback attention client and contract-verified test transport [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/attention_client.py`
- `tests/storage/attention_fake.py`
- `tests/storage/test_attention_client.py`

**Research context.** `AttentionServiceClient` posts to 2.3's routes. It
follows `src/gobby/hub/node_client.py::NodeCommandClient`:
- base URL `http://127.0.0.1:<load_bootstrap().daemon_port>`;
- header `X-Gobby-Backend-Secret: front_door_secret()`;
- `trust_env=False`, no redirects, and an optional injected httpx transport.

It provides sync and async `transition`, `set_metadata` and `clear_metadata`.
A connection failure or a missing secret raises `AttentionServiceUnavailable`.
For transitions, that error propagates exactly as a database error from the old
transition did, since every transition call site either lets it propagate or
catches `Exception`. Metadata is different: it is advisory and bounded by its
time-to-live, and its call sites do not expect a service error.
`hooks/event_handlers/_agent.py::_set_attention_metadata` catches only
`TypeError` and `ValueError`, and `attention_tracker.py` has no handler at all.
So the client's metadata `set` and `clear` log a warning and drop the change
when the service is unavailable, in the same shape as the existing "Dropped
invalid attention metadata self-report" warning. Validation errors still raise.
There is no retry or fallback write.

`tests/storage/attention_fake.py` is an httpx transport that applies the
transition contract to the test database and keeps metadata in memory, for
Python tests that need real attention rows. It is built from the current
`_transition_locked` body plus `expected_payload`, and it records requests. It
must pass every 1.1 transition vector, so it cannot drift from the native
store.

Nothing is wired to the client in this leaf.

Planned: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_attention_client.py`.

**Acceptance:**
- 4.2.1 - The client sends each operation to its loopback route with the backend secret and no proxy environment, maps the responses, and raises `AttentionServiceUnavailable` from a transition when the connection fails or the secret is missing. test: `tests/storage/test_attention_client.py::test_client_wire_contract`.
- 4.2.2 - The fake transport passes every 1.1 transition vector, including `expected_payload`. test: `tests/storage/test_attention_client.py::test_fake_transport_matches_transition_vectors`.
- 4.2.3 - When the service is unavailable, metadata set and clear log a warning and drop the change without raising, while invalid metadata still raises its validation error. test: `tests/storage/test_attention_client.py::test_metadata_unavailable_is_dropped`.

### 4.3 Native takeover and Python attention retirement [category: code] (depends: 4.2, #24967)
`kind: deliverable`

Targets:
- `crates/gagents/src/family.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the native attention default
- `src/gobby/storage/attention.py::*` — scope-reason: replace the ordering coordinator, transition core, sweep, roster loader and publisher with client bodies while keeping the direct reads
- `src/gobby/agents/attention_metadata.py::*` — scope-reason: make set and clear client calls and delete the in-memory store and publisher
- `src/gobby/sessions/turn_lifecycle.py::*` — scope-reason: drop FOR UPDATE, apply the bounded CAS and fenced status write in _apply and block_provider_failure, and repair status on duplicate replay
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: move attention client construction into the split module and delete both publisher closures
- `src/gobby/runner_init/attention_clients.py`
- `src/gobby/runner_init/servers.py::*` — scope-reason: remove the configure_attention_ordering and configure_attention_metadata wiring
- `src/gobby/servers/websocket/broadcast.py::*` — scope-reason: remove the attention ordering properties and metadata enrichment now done natively
- `src/gobby/servers/_app_routes.py::*` — scope-reason: remove the attention router registration
- `src/gobby/servers/routes/attention.py::*` — operation: delete — scope-reason: the four routes are native
- `src/gobby/servers/routes/attention_roster.py::*` — operation: delete — scope-reason: the roster projection is native
- `docs/contracts/parity-ledger-status.d/sessions-agents.json::*` — scope-reason: delegate the four proven attention HTTP rows with evidence and waiver citations
- `docs/contracts/parity-ledger.md`
- `tests/conftest.py::*` — scope-reason: autouse fixture binding attention clients to the fake transport so no test reaches a live daemon
- `tests/agents/test_attention_state.py::*` — scope-reason: keep direct-read and client tests; drop core, ordering and notification tests now covered natively
- `tests/agents/test_attention_metadata.py::*` — scope-reason: test client set and clear and the validators; drop in-memory store tests now covered natively
- `tests/events/test_wake_composer_attention.py::*` — scope-reason: drop the notification_publisher cases and the epoch argument
- `tests/agents/test_native_spawn.py::*` — scope-reason: drop the removed epoch constructor argument
- `tests/hub/test_remote_pane.py::*` — scope-reason: drop the removed epoch constructor argument
- `tests/servers/test_node_routes.py::*` — scope-reason: assert the transitions sent instead of Python-published events
- `tests/sessions/test_turn_lifecycle.py::*` — scope-reason: drop the removed epoch constructor argument
- `tests/sessions/test_turn_lifecycle_cas.py`
- `tests/storage/test_attention_client.py` — edit the file created by 4.2 to assert Python keeps no attention ordering state
- `tests/mcp_proxy/tools/sessions/test_registration_terminal_binding.py::*` — scope-reason: replace load_roster_rows assertions with bound-terminal row assertions
- `tests/storage/test_terminal_bindings.py::*` — scope-reason: replace load_roster_rows assertions with bound-terminal row assertions
- `tests/config/test_config_authority_audit.py::*` — scope-reason: remove the deleted attention route path
- `tests/terminals/test_no_direct_tmux_consumers.py::*` — scope-reason: remove the deleted attention route path
- `tests/servers/test_attention_native_roster.py::*` — operation: delete — scope-reason: native roster tests and corpus cover it
- `tests/servers/test_attention_remote_respond.py::*` — operation: delete — scope-reason: native respond tests cover it
- `tests/servers/test_attention_respond.py::*` — operation: delete — scope-reason: native respond tests cover it
- `tests/servers/test_attention_roster.py::*` — operation: delete — scope-reason: native roster tests cover it
- `tests/servers/test_attention_roster_machines.py::*` — operation: delete — scope-reason: native roster tests cover machine scope
- `tests/servers/test_attention_routes_wiring.py::*` — operation: delete — scope-reason: the Python router no longer exists
- `tests/e2e/test_attention_family.py` — edit the file created by 3.5 to verify the Python writers through the native store after deletion

**Research context.** This leaf is the single restart-bound flip. It changes the
following, in order of the targets above.

**1. Native default.** `AttentionFamily::default_backend` returns `native`.

**2. Python client.** The `AttentionStateManager` constructor keeps `(db, ...)`
and drops `epoch`, `event_publisher` and `notification_publisher`.
- `transition` and `transition_async` call `AttentionServiceClient`.
- `get`, `list_blocked` and `snapshot` stay direct reads. `snapshot` keeps only
  `.states`.
- `epoch`, `seq`, `ordering`, `_transition_locked`, `_sweep_empty_rows_locked`,
  `load_roster_rows`, `_publish`, the roster row types and
  `AttentionOrderingCoordinator` are deleted.

The client comes from a module-level `_client_factory(db)`, which returns
`AttentionServiceClient.from_environment()` in production. The autouse fixture
in `tests/conftest.py` replaces it with a factory that builds clients over
`tests/storage/attention_fake.py` for the given test database.

`AttentionMetadataStore()` takes no arguments and keeps `set` and `clear`. The
validators stay, and the server validates again.

**3. Complete writer list.** All of these become clients with unchanged call
sites:
- transitions:
  - `src/gobby/agents/attention_tracker.py` (three `transition_async` calls)
  - `src/gobby/agents/interactive_attention_monitor.py` (three)
  - `src/gobby/agents/terminal_cleanup.py` (one)
  - `src/gobby/agents/watchdog/interactive_capacity.py` (two)
  - `src/gobby/hooks/terminal_handoff_delivery.py` (two sync `transition`
    calls, plus its `_attention_manager` fallback)
  - `src/gobby/terminals/composer_attention.py` (two)
  - `src/gobby/sessions/turn_lifecycle.py` (the reducer)
  - the `TurnLifecycleReducer` built in `src/gobby/servers/websocket/server.py`
- metadata:
  - `src/gobby/agents/attention_tracker.py` (set and clear)
  - `src/gobby/hooks/event_handlers/_agent.py::_set_attention_metadata` (set)

The former route writer, `src/gobby/servers/routes/attention.py`, is deleted.
The readers `agent_health.py`, `continuation_retry.py` and
`src/gobby/cli/sessions.py` use the direct reads and stay unchanged.

**4. Reducer.** In `TurnLifecycleReducer._apply` and `block_provider_failure`,
replace the `FOR UPDATE` transaction with this order:
1. Read the attention row plainly, with no row lock.
2. If the mutation leaves the row unchanged (state, reason, kind, fingerprint
   and payload), send no transition and go to step 4. Otherwise send a CAS
   transition carrying `expected_attention_id` and `expected_payload`, where
   an absent row compares as `{}`. A sent transition always changes the row,
   so a not-applied result always means a conflict.
3. On a conflict, re-read and retry, up to three attempts in all. Then return
   a not-applied result with reason `attention_conflict`, writing no status.
4. Write status through the fence, in one short local transaction:
   1. `SELECT ... FROM sessions WHERE id = %s FOR UPDATE`;
   2. re-read the attention row in a separate statement, which takes a fresh
      READ COMMITTED snapshot;
   3. if the committed `turn_lifecycle` equals the lifecycle this reducer
      applied, or the one it read when it sent nothing, call
      `update_session_status` inside the same transaction, as `_apply` does
      today; otherwise skip the write, because a later reducer's fence owns
      the status.

   The fence never locks the attention row and makes no client call, because
   the native store holds its cursor mutex across its own update.

Replay repair changes these duplicate paths:
- `begin_turn` with the open turn's provider key returns
  `_status_for_waits(waits)`, or `active` when there are no waits, but only
  while the observed status equals the lifecycle's `prior_status`. Otherwise it
  keeps the observed status.
- `record_provider_failure` for a failure already recorded in this generation
  returns `paused` when the observed status is `active`.

These paths already repair and stay unchanged:
- `enter_wait` with a known token;
- `end_turn`;
- `resumed_work`.

`resolve_wait` for an already-resolved token keeps the observed status
deliberately. The stored lifecycle cannot tell a stale awaiting status from one
another writer set, so the turn's next status-writing transition repairs it.

`block_provider_failure` writes no status, so it needs only steps 1-3. The
crash tests inject a failure between the CAS and the status write. They then
assert repair by the next status-writing transition, and by replay of the same
`begin_turn`, `enter_wait` and `record_provider_failure` evidence. A
deterministic interleaving test runs A's CAS, then B's CAS and fenced write,
then A's fenced write, and asserts that B's status stands.

**5. Wiring.** Move the attention client construction out of
`src/gobby/runner_init/orchestration.py` and into the new split module
`src/gobby/runner_init/attention_clients.py`, as `init_attention_clients(runner)`.
It sets `runner.attention_manager` and `runner.attention_metadata_store` before
their consumers: the `attention_manager=` argument to the `WakeDispatcher`
construction, and the `attention_manager=` and `attention_metadata_store=`
arguments to the lifecycle monitor construction. The
`publish_attention_event` and `publish_attention_metadata` closures are deleted.
This shrinks orchestration.py, which is at 896 lines.

Also delete:
- the `configure_attention_ordering` and `configure_attention_metadata` calls
  in `src/gobby/runner_init/servers.py`;
- the matching methods and properties in `broadcast.py`, along with its
  metadata enrichment;
- the router registration in `_app_routes.py`.

**6. Ledger.** In the same commit, move the four HTTP rows in
`sessions-agents.json` to `delegated` with the native evidence and waiver
citations, then regenerate `parity-ledger.md`. 4.1 keeps those rows once their
Python surfaces are gone.

**7. Tests.** Each consumer test listed above is updated as its scope-reason
says. Tests that construct `AttentionStateManager(temp_db)` and need real rows
use the autouse fake and stay unchanged:
- `tests/agents/test_interactive_capacity_recovery.py`
- `tests/hooks/test_provider_error_recovery.py`
- `tests/hooks/test_terminal_handoff_delivery.py`
- `tests/agents/test_pane_detection_corpus.py`
- `tests/cli/test_cli_sessions.py`

Sweep the removed symbols across `src/` and `tests/`, and migrate any further
consumer the sweep finds.

**Rollback.** After this leaf, rollback requires the prior coherent release.

**Cutover preflight.** `crates/gdaemon/src/front_door/routes.rs::RouteTable::new`
gives an explicit `front_door.routes.attention` entry precedence over
`default_backend`. A leftover `proxy` or `compare` entry would send requests to
the deleted Python handlers. A leftover `native` entry would survive a rollback
and override the prior release's `proxy` default. So the cutover that activates
this leaf, and any rollback reinstall, first confirms that the live
`~/.gobby/bootstrap.yaml` has no `attention` key under `front_door.routes`. If
the key is present, the cutover stops before the restart. This is a
configuration check only: it adds no backend and no fallback.

Planned:
- focused isolated pytest of every target test file;
- `tests/e2e/test_attention_family.py`;
- `uv run ruff check src/`, `uv run ruff format --check src/` and
  `uv run mypy src/`;
- the scoped test-types audit over the changed tests;
- `cargo nextest run -p gobby-agents`.

**Granularity:** These targets retire one lifecycle owner, the Python attention
cursor, atomically. Any split would leave Python routes reading a cursor that no
longer exists, Python writers publishing alongside the native store, or ledger
rows pointing at deleted surfaces. The reducer CAS has to land with the client:
under the old `FOR UPDATE` it deadlocks against the native store. Native behavior
was already delivered and tested in P2 and P3. This leaf verifies the flip, the
writers and absence.

**Acceptance:**
- 4.3.1 - The attention family defaults to native, and gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 4.3.2 - Every named Python writer transitions and sets metadata only through the client, and no Python module keeps an attention cursor or publisher. test: `tests/storage/test_attention_client.py::test_python_holds_no_attention_ordering_state`.
- 4.3.3 - The reducer reads without a row lock, sends no transition for an unchanged row, applies a change by CAS within three attempts, writes status only through the fence, and returns `attention_conflict` once the attempts are exhausted. test: `tests/sessions/test_turn_lifecycle_cas.py::test_reducer_writes_status_after_cas`.
- 4.3.4 - A failure injected between the CAS and the status write is repaired by the next status-writing transition, and by replay of the same `begin_turn`, `enter_wait` and `record_provider_failure` evidence, while a status another writer set after `begin_turn` stands, asserting `attention.get-roster.session-status-repair`. test: `tests/sessions/test_turn_lifecycle_cas.py::test_crash_between_cas_and_status_is_repaired`.
- 4.3.5 - After deletion, Python writer transitions publish ordered events through the native broker, and the roster serves them with no Python attention route. test: `tests/e2e/test_attention_family.py::test_python_writers_publish_through_native_store`.
- 4.3.6 - The four attention HTTP rows are delegated with evidence and waiver citations, and the ledger regenerates cleanly. file: `docs/contracts/parity-ledger.md`.
- 4.3.7 - When reducer A's CAS is followed by reducer B's CAS and fenced status write, A's later fenced write is skipped and B's status stands. test: `tests/sessions/test_turn_lifecycle_cas.py::test_stale_status_write_is_fenced`.

### 4.4 Attention service contract and nodes CLI handoff [category: docs] (depends: 4.3)
`kind: deliverable`

Targets:
- `docs/contracts/attention-services.md`
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json::*` — scope-reason: give CLI gobby nodes and gobby nodes list their #21571 owner notes
- `docs/contracts/parity-ledger.md`

**Research context.** `attention-services.md` documents:
- the loopback routes, their admission rule and their bodies;
- the event messages;
- the cursor and roster consistency rule;
- the complete writer list;
- the reducer CAS order, its status fence and its crash repair;
- the decision-16 roster exception;
- the respond error contract;
- the post-takeover rollback boundary;
- 4.3's cutover preflight, which requires `front_door.routes.attention` to be
  absent before both the takeover restart and a rollback reinstall.

It also gives #21571 the service contract for CLI `gobby nodes` and
`gobby nodes list` over the existing native `/api/machines`
(`crates/gdaemon/src/nodes/machines_api.rs`). Those two rows stay `proxied`,
with owner notes citing this document. Regenerate the ledger with the existing
tooling. No expansion and no operator action happens here.

Planned: `uv run pytest tests/test_parity_ledger.py` (isolated) and a docs
review.

**Acceptance:**
- 4.4.1 - The service contract documents the routes, events, cursor, writer list, reducer repair, roster exception, respond errors, rollback boundary and the route-override cutover preflight. file: `docs/contracts/attention-services.md`.
- 4.4.2 - CLI gobby nodes and gobby nodes list stay proxied, with #21571 owner notes citing the contract. file: `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json`.

## E1 Verification
`kind: verification`

Authoring validation:
`uv run gobby plans validate .gobby/plans/s2-7-hub-attention-dispatch.md -p /Users/josh/Projects/gobby`.
The final adversary-rendered bytes must pass both base and `--mode expansion`
validation, and the stamped handoff records the commit and the exit results.
This narrative draft has no M1, so expansion validation has not run.

Implementation checks are all planned, none run:
- the isolated Python oracle, client and reducer tests;
- package-scoped `cargo nextest` suites for gobby-agents, gobby-daemon and
  gobby-terminals;
- isolated e2e corpus replay, plus 3.5's same-database restart test
  `tests/e2e/test_attention_family.py::test_restart_resets_cursor_and_keeps_rows`;
- 4.3's cutover preflight, confirming `front_door.routes.attention` is absent
  from the live bootstrap before the takeover restart;
- the version-pin test on every Rust leaf;
- Ruff, mypy src and the scoped test-types audit on 4.3.

A passing validation parse does not attest to future behavior. Josh approves the
stamped plan before any expansion. This planning seat neither expands nor
dispatches work.

## V1 Plan Changelog
`kind: framing`

2026-10-28: The Plan Writer drafted the narrative from the Orchestrator's Q1-Q7
rulings and the Q3 transaction-boundary follow-up. It is ready for the
enhancer's pass, then adversarial review. There is no M1.

2026-10-28: Consensus between the Plan Writer (gobby#18276) and the Plan
Adversary (gobby#18280). The Orchestrator accepted the enhancer's
ENH-BETTER-001 (4.3 cutover preflight on `front_door.routes.attention`),
ENH-BETTER-002 (3.5 same-database restart test) and ENH-BETTER-003 (2.3
Granularity note). It also ruled Q8 (a): 4.1 owns ledger retention of delegated
rows. All four are applied. The adversary raised four blocking findings, and
all four are resolved:
- ADV-001: replay repair is now defined per duplicate path, with the
  `resolve_wait` boundary, and an unchanged row skips the CAS to reach the
  fence;
- ADV-002: status writes are fenced by a session-row lock and a fresh
  lifecycle read, with a deterministic interleaving test in 4.3.7;
- ADV-003: respond checks machine ownership for every pane before it branches,
  tested in 3.4.9;
- ADV-004: 4.3 carries the full reducer algorithm standalone.

The adversary's final recheck of c82ebba passed base validation with no open
decisions.
