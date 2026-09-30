# Wake Delivery Recovery

Plan artifact: `.gobby/plans/wake-delivery-recovery.md`

> **Plan ID:** wake-delivery-recovery
>
> Epic: #22651. Source implementation tasks: #22653 and #22654.

## C1 Context
`kind: framing`

Two durable mailbox messages were stranded by different lifecycle gaps:

- #22653: message `4a734994-a77c-4994-8cfe-e2a67b25b31b` was persisted at
  2026-09-21T05:53:35Z while its recipient was mid-turn. The turn ended at
  05:53:41Z, but the row remained `delivered_at IS NULL` until the manual resend
  `112bc887-60ca-4f1a-8f42-1807d1bd4eb9` woke the recipient at 05:56:38Z.
- #22654: session `af5f5c4a-...` retained `status='active'` across the daemon
  restart. Messages `7aaafce1-...` and `1f942888-...` were classified as
  mid-turn until manual pane input at 06:07Z. A later Codex resume created native
  terminal `3eb7116d-...` while its row had `session_id IS NULL`, the old exited
  terminal remained bound, and wake plus `send_keys` fell through to
  `no_tmux_pane`.

Current code establishes these boundaries:

- `MailboxService.send` commits `inter_session_messages` before live dispatch.
  `WakeDispatcher._dispatch_live_wake_unlocked` declines a non-urgent active
  recipient, but no committed transition listener replays the row when
  `TurnLifecycleReducer.end_turn` commits `paused`.
- `IdleDetector.composer_read` and
  `IdleDetector.turn_in_flight_fingerprint` classify different evidence. An
  empty or draft composer can coexist with a live provider turn, so recovery
  must derive both values from the same terminal snapshot.
- `_run_agent_hook_replay_barrier` currently reduces its result to a boolean.
  `HookInboxBarrierResult` already carries unresolved ordinary-session and
  agent-run identities; restart reconciliation needs that typed evidence rather
  than treating barrier completion as universal settlement.
- `handle_pre_created_session` backfills `gobby_terminal_id` but bypasses the
  discovery/binding order used by `activate_materialized_session`. Both wake and
  `send_keys` prefer a bound managed row, so the missing bind causes native
  sessions to fall through to tmux-only metadata.
- The WriteCoordinator latch is not the cause. Preserve its drain-before-settle
  ordering, typed automatic-write declines, and the separate acknowledged
  completion-notification contract.

## D1 Decision Record
`kind: framing`

- **Existence — restraint rung 1:** the work is required. Durable storage alone
  does not replay an actionable wake, and terminal context alone does not bind a
  resumed native session.
- **Durable wake intent — restraint rung 2:** reserve
  `metadata_json.wake_requested` in `MailboxService.send`. Every direct and
  fanout row writes the actual boolean supplied by the `wake` argument, overriding
  caller metadata. Non-mailbox producers remain intentionally unmarked. No table,
  column, migration, compatibility reader, or configuration flag is added.
- **Correlation boundary — restraint rung 2:** dispatcher single/batch APIs keep
  their current signatures and return only transport/session outcomes with stable
  `decline_reason` values. `mailbox_delivery` zips committed messages, resolved
  recipients, and ordered results, attaches message IDs to sender diagnostics,
  and emits the sole message-correlated INFO record. No message ID flows through
  generic dispatcher or fallback signatures.
- **Replay owner — restraint rung 2:** reuse committed
  `SessionStatusTransition` notifications. A `WakeReplayCoordinator` owns one
  task per recipient and queries marked undelivered rows. It never updates
  `delivered_at`; normal context injection plus acknowledged receipt remains the
  only delivery authority.
- **Startup gate — restraint rung 6:** the replay coordinator is constructed
  closed. Before readiness, callbacks only retain recipient intent. The gate
  opens and drains after terminal-host adoption, a typed hook-barrier decision,
  and the initial restart reconciliation pass. Direct mailbox dispatch retains
  existing policy; only replay work is gated.
- **Cancellation-safe coalescing — restraint rung 3:** use coordinator-owned
  `asyncio.Task` objects and `asyncio.shield` for joiners. Every terminal outcome
  is observed; cleanup removes a slot only when it still contains that exact
  task. Cancellation, exception, or a designed decline leaves durable rows
  pending for a later boundary or startup pass.
- **Restart proof — restraint rung 2:** capture `runner.http_bound_at_ms` once as
  an integer Unix-millisecond horizon. A row is eligible only when both
  `last_activity` and `updated_at`, floored to integer Unix milliseconds, are
  non-null and `<=` that horizon; equality is eligible. A shared terminal
  snapshot must show no turn-in-flight fingerprint and an `empty` or `draft`
  composer. Unknown evidence, any in-flight fingerprint, post-horizon activity,
  or an unresolved barrier identity preserves `active`.
- **Race closure — restraint rung 6:** immediately before the private status
  compare-and-set, take a second shared terminal snapshot and re-read the row.
  Recheck the complete predicate, then update only `status='active'` with the
  exact observed `updated_at`. The durable pending row predates the status write;
  gate-open drain queries the database again, so a callback gap cannot lose wake
  intent.
- **Native routing — restraint rung 2:** factor the existing materialized-session
  discovery/bind sequence and reuse it for pre-created resumes. Resolve a guarded
  live native row by `gobby_terminal_id` after ordinary session binding and
  before raw tmux fallback. Preserve tmux-inner precedence and all project,
  session-type, state, agent-terminal, and live-owner guards.
- **Task split — restraint rung 6:** #22653 remains one durable replay leaf;
  #22654 is narrowed to restart reconciliation. Native resume binding/routing is
  a separate leaf filed by the orchestrator because it is independently
  closeable. Runtime readiness and task dependencies keep startup replay behind
  the native route.
- **Required decomposition — restraint rung 6:** direct-extract cohesive
  responsibilities while changing the current 983-line `wake.py`, 910-line
  `mailbox.py`, 857-line `runner_init/orchestration.py`, and 853-line
  `runner_lifecycle_agents.py`. No forwarding shims or parallel compatibility
  paths are introduced.

No open questions remain. No backward-compatibility shim is needed because 0.5.0
has not shipped.

## C2 Deliverable-to-task Map
`kind: framing`

- **1.1 -> existing #22653.** Implements durable mid-turn wake intent, sender
  diagnostics, committed-boundary replay, startup gating, and end-to-end receipt.
- **2.1 -> new backend bug leaf under #22651, filed by the orchestrator.** Title:
  "Bind pre-created native resumes and route wake/send_keys by gobby_terminal_id."
  It takes #22654 acceptance obligations 4-5 and depends on 1.1 because both edit
  the wake dispatcher.
- **2.2 -> existing #22654, narrowed to its restart obligations 1-3.** It depends
  on 1.1 and 2.1 so initial startup replay opens only after durable replay and the
  provider-neutral native route are present. The orchestrator records the
  corresponding task dependency when it files the 2.1 leaf.

## P1: Durable Turn-Boundary Replay
`kind: framing`

**Goal:** Complete #22653 by making a mid-turn `wake=true` request durable,
observable to its sender, and replayable through normal context receipt after the
recipient's turn commits as paused.

### 1.1 Persist, report, and safely replay declined mailbox wakes [category: code] [domain: backend]
`kind: deliverable`

Targets:
- `src/gobby/sessions/mailbox.py::*` — scope-reason: extract mailbox wake orchestration while preserving public send/target APIs
- `src/gobby/sessions/mailbox_delivery.py` — new module: ordered mailbox correlation, normalization, and message-scoped decline logging
- `src/gobby/storage/inter_session_messages.py::*` — scope-reason: add marked-undelivered queries without changing delivery acknowledgement
- `src/gobby/events/wake.py::*` — scope-reason: normalize public dispatcher outcomes and extract completion persistence without signature churn
- `src/gobby/events/wake_notifications.py` — new module: existing completion-notification persistence extracted intact
- `src/gobby/events/wake_recovery.py` — new module: closed startup gate, committed-transition listener, and cancellation-safe per-recipient tasks
- `src/gobby/events/wake_batch.py::dispatch_live_wakes`
- `src/gobby/events/live_wake.py::*` — scope-reason: normalize every designed live-wake decline through one result contract
- `src/gobby/mcp_proxy/tools/agent_messaging.py::send_message`
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: open the initial replay gate only after terminal-host adoption and the existing hook barrier
- `tests/events/test_wake.py::*` — scope-reason: stable decline outcomes and dispatcher log ownership
- `tests/events/test_wake_native_terminal.py::*` — scope-reason: batch/native decline normalization and existing latch behavior
- `tests/events/test_wake_recovery.py` — new isolated coordinator and end-to-end delivery suite
- `tests/agents/test_terminal_delivery.py::*` — scope-reason: completion acknowledgement remains independent from mailbox replay
- `tests/sessions/test_mailbox.py::*` — scope-reason: reserved marker and direct/fanout message correlation
- `tests/sessions/test_turn_lifecycle.py::*` — scope-reason: committed active-to-paused replay boundary
- `tests/storage/test_inter_session_messages.py::*` — scope-reason: ordered marked-undelivered recipient and row queries
- `tests/mcp_proxy/tools/test_agent_messaging.py::*` — scope-reason: brief structured decline response
- `tests/mcp_proxy/tools/test_agent_messaging_broadcast.py::*` — scope-reason: fanout correlation and sender diagnostics
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: no replay write before startup gate readiness

Production-size decomposition: move completion persistence from
`src/gobby/events/wake.py` into the new
`src/gobby/events/wake_notifications.py`, and move mailbox wake orchestration
from `src/gobby/sessions/mailbox.py` into the new
`src/gobby/sessions/mailbox_delivery.py`.

Consumers unchanged:
- `src/gobby/agents/resume_finalization.py` — no-edit-reason: direct lifecycle messages remain intentionally unmarked and outside mailbox replay.
- `src/gobby/mcp_proxy/tools/tasks/_stage_review.py` — no-edit-reason: stage-review notifications remain intentionally unmarked.
- `src/gobby/sessions/compact_continuation.py` — no-edit-reason: compact-continuation messages retain their separate delivery contract.
- `tests/servers/routes/test_tasks_routes.py` — no-edit-reason: dispatcher signatures do not gain message IDs, so the route fake remains valid.
- `src/gobby/runner_lifecycle.py` — no-edit-reason: owner-loop binding and subsystem-task launch keep their current signatures.
- `tests/test_runner_shutdown.py` — no-edit-reason: subsystem initialization keeps its public call signature.

**Task ownership:** existing bug #22653. **Dependencies:** none.

**Granularity:** Eight acceptance items and more than six production targets
trigger inspection. The section owns one independently testable state machine:
persist mailbox wake intent, report its immediate outcome, and replay the same
intent at a committed boundary without acknowledging delivery. Splitting before
that boundary would leave either an unconsumed marker or a replay path with no
durable source. New modules are direct extractions needed for the line ceiling.

**Research context:**

- `gcode grep -F '.create_message(' src tests -m 200` found every production
  producer. `MailboxService.send` is the only producer that receives a caller
  `wake` boolean. Completion notifications, resume finalization, stage review,
  and compact continuation remain unmarked by design.
- `MailboxService.send` creates messages in recipient order inside one
  transaction, then calls `_wake_many`. `mailbox_delivery` can use
  `zip(..., strict=True)` over committed messages, recipients, and ordered
  results; length mismatch becomes a typed mailbox failure rather than silent
  mis-correlation.
- `gcode grep -F '.dispatch_live_wake(' -m 100` and
  `gcode grep -F '.dispatch_live_wakes(' -m 100` show mailbox, completion, and
  direct test consumers. No non-mailbox consumer needs message IDs, so the
  dispatcher single/batch signatures stay unchanged.
- `InterSessionMessageManager.get_undelivered_messages` orders by `sent_at, id`.
  New focused queries select only rows whose JSON metadata contains
  `wake_requested=true`, preserve that order, and expose distinct recipients for
  startup drain.
- SessionManager status listeners run after the committed status write. The
  coordinator registers with the dispatcher owner loop, begins closed, and
  records recipient IDs until `init_subsystems` has adopted the terminal host and
  awaited the current hook barrier.
- Context injection and receipt are existing read-only dependencies:
  `EventEnricher._inject_pending_messages` stages IDs and
  `apply_acknowledged_receipt` calls `mark_delivered_batch`. Replay itself never
  claims delivery.

**Implementation:**

1. Make `MailboxService._metadata_json` own the reserved `wake_requested`
   boolean. Caller metadata cannot override it; direct and every fanout row use
   the same path.
2. Move `WakeDispatcherProtocol`, wake normalization, and `_wake_many` mechanics
   into `mailbox_delivery.py`. Correlate results with committed message IDs only
   there. Emit exactly one INFO record per designed decline containing recipient
   session ID, message ID, and `decline_reason`.
3. Normalize designed non-delivery at the public dispatcher single/batch boundary
   to `decline_reason` values including `session_active`, `composer_occupied`,
   `debounced`, and typed automatic-write refusal reasons. Remove branch-local
   INFO duplicates. Unexpected failures keep `error_code` plus WARNING/traceback;
   indeterminate writes retain their existing semantics.
4. In `send_message(brief=true)`, return correlated designed declines under
   `wake_declines` with `delivery_status='sent_with_declined_wakes'`. Operational
   failures remain `wake_failures`; durable persistence still returns
   `success=true`.
5. `WakeReplayCoordinator` registers one committed-status listener, keeps a
   closed/open gate, and owns `dict[session_id, Task]`. Closed-gate callbacks add
   only to a pending-recipient set. Open-gate requests create at most one owner
   task per recipient; joiners await `asyncio.shield(task)`.
6. The owner task queries all marked undelivered rows, chooses urgent priority if
   any row is urgent, and dispatches one coalesced wake without passing message
   IDs. Its completion callback always retrieves the result, logs success or a
   designed decline at DEBUG and unexpected exception/owned cancellation at
   WARNING, and removes the map slot only if the stored object is that exact
   task. All non-success outcomes leave rows eligible for retry.
7. Wire the first-stage gate opening in `init_subsystems` after terminal-host
   adoption and the awaited hook barrier. Section 2.2 moves the same open/drain
   call behind session-aware restart reconciliation; no second gate is added.
8. Direct-extract `_send_ism` and completion-id deduplication into
   `wake_notifications.py`; preserve #22613 acknowledgement behavior and the
   WriteCoordinator drain/latch order.

**Focused validation:**

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/events/test_wake.py tests/events/test_wake_native_terminal.py tests/events/test_wake_recovery.py tests/agents/test_terminal_delivery.py tests/sessions/test_mailbox.py tests/sessions/test_turn_lifecycle.py tests/storage/test_inter_session_messages.py tests/mcp_proxy/tools/test_agent_messaging.py tests/mcp_proxy/tools/test_agent_messaging_broadcast.py tests/test_runner_lifecycle_subsystems.py -q
```

**Acceptance:**

- 1.1.1 - Direct and fanout mailbox sends persist the caller's actual `wake` value as a reserved boolean, reject caller forgery, and leave every enumerated non-mailbox producer unmarked and excluded. test: `tests/sessions/test_mailbox.py::test_send_reserves_wake_requested_for_direct_fanout_and_nonwake_rows`.
- 1.1.2 - Mailbox correlation attaches each committed message ID to its ordered outcome and emits exactly one INFO record containing session, message, and reason; dispatcher APIs and fallbacks carry no message IDs. test: `tests/sessions/test_mailbox.py::test_mailbox_boundary_correlates_and_logs_one_decline_per_message`.
- 1.1.3 - Active, composer-occupied, debounced, and automatic-write policy outcomes expose stable `decline_reason` values; brief responses separate `wake_declines` from operational `wake_failures`. test: `tests/mcp_proxy/tools/test_agent_messaging.py::test_brief_response_reports_correlated_wake_decline`.
- 1.1.4 - A committed `active -> paused` transition observed before readiness causes zero terminal writes, is retained once, and drains only after the startup gate opens. test: `tests/events/test_wake_recovery.py::test_gate_holds_committed_transition_until_ready`.
- 1.1.5 - Coordinator-owned tasks survive a cancelled joiner; dispatch exception, owned-task cancellation, and designed decline are observed, leave the row pending, permit a later retry, and stale cleanup cannot remove a replacement task. test: `tests/events/test_wake_recovery.py::test_replay_task_ownership_and_retry_matrix`.
- 1.1.6 - An isolated fake clock sends two `wake=true` messages exactly five seconds apart while the first turn is active; the second send returns a correlated decline and one INFO record, committed turn end causes one coalesced wake, normal context injection/receipt runs, and both rows finish with non-null `delivered_at`. test: `tests/events/test_wake_recovery.py::test_two_mid_turn_messages_replay_through_acknowledged_receipt`.
- 1.1.7 - Replay never writes `delivered_at`; `wake=false`, unmarked lifecycle messages, completion notifications, urgent policy, indeterminate writes, and the established wake-latch ordering retain their existing behavior. test: `tests/events/test_wake_native_terminal.py::test_latched_wake_is_settled_after_delivered_drain`.
- 1.1.8 - Direct extraction leaves `src/gobby/events/wake.py` and `src/gobby/sessions/mailbox.py` below 850 lines with no forwarding shim, and every production file in this section stays below 1,000 lines. file: `src/gobby/sessions/mailbox_delivery.py`.

## P2: Native Resume and Restart Recovery
`kind: framing`

**Goal:** Complete #22654 without combining two lifecycle owners: first restore a
provider-neutral native route, then reconcile restart-stale activity and open
startup replay only after that route and the session-aware barrier are ready.

### 2.1 Bind pre-created native resumes and route terminal actions [category: code] [domain: backend] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/terminal_runtime.py::*` — scope-reason: share guarded external-terminal discovery and native bind/rebind order
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::activate_materialized_session`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_pre_created_session`
- `src/gobby/storage/terminals.py::*` — scope-reason: guarded context-ID resolution and stale exited-binding cleanup
- `src/gobby/events/wake.py::*` — scope-reason: consume provider-neutral terminal resolution before raw tmux fallback
- `src/gobby/events/wake_terminal_resolution.py` — new module: provider-neutral managed/context/raw fallback selection moved out of WakeDispatcher
- `src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py::register_send_keys_tool`
- `tests/events/test_wake_native_terminal.py::*` — scope-reason: native context-ID route and tmux fallback parity
- `tests/hooks/test_session_materialize.py::*` — scope-reason: materialized clear-successor binding remains intact
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: pre-created Codex native resume regression
- `tests/storage/test_terminal_bindings.py::*` — scope-reason: guarded bind, lookup, and exited-owner cleanup
- `tests/terminals/fakes.py::*` — scope-reason: keep shared terminal test stores aligned with the new resolver contract
- `tests/mcp_proxy/test_sessions_terminal_tools.py::*` — scope-reason: provider-neutral send_keys routing
- `tests/mcp_proxy/tools/sessions/test_terminal.py::*` — scope-reason: raw tmux send_keys fallback remains intact

Production-size decomposition: move managed/context/raw terminal selection from
`src/gobby/events/wake.py` into the new
`src/gobby/events/wake_terminal_resolution.py`; WakeDispatcher consumes that
single resolver instead of growing another routing branch.

Consumers unchanged:
- `src/gobby/hooks/event_handlers/_session_start/__init__.py` — no-edit-reason: wrapper signatures continue forwarding to the two extracted implementations unchanged.
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py` — no-edit-reason: registration continues passing the existing TerminalManager dependency to `register_send_keys_tool`.

**Task ownership:** new backend bug leaf under #22651, filed by the orchestrator
with #22654 acceptance obligations 4-5. **Dependencies:** 1.1.

**Granularity:** The production target count triggers inspection, but this section
owns one independently testable outcome: establish a valid native terminal owner
and consume the same provider-neutral resolver from wake and `send_keys`. Restart
status mutation, barrier interpretation, and startup replay remain entirely in
2.2.

**Research context:**

- `gcode grep -w activate_materialized_session src tests -m 100` found the
  `SessionStartMixin` wrapper and `test_session_materialize.py` direct consumers.
  `gcode grep -w handle_pre_created_session src tests -m 100` found its wrapper.
  Both wrappers retain their signatures and are recorded above as unchanged.
- The materialized path discovers an external row, preserves tmux-inner
  precedence, attempts native binding, transfers clear-successor state, expires
  stale same-context sessions, then retries only the refused native bind. The
  pre-created path currently updates status and terminal context but performs no
  discovery or bind.
- `TerminalManager.get_live_for_session` is the bound-row primary route.
  `gobby_terminal_id` is a UUID-bearing context locator, not ownership proof; the
  guarded resolver must still verify state, project, agent-terminal flag, and
  unbound-or-self-bound ownership.
- `gcode grep -w register_send_keys_tool src tests -m 100` found only the stable
  `_terminal.py` wrapper. The raw-tmux behavior is covered in
  `tests/mcp_proxy/tools/sessions/test_terminal.py` and must remain the final
  fallback.

**Implementation:**

1. Move the materialized path's external discovery, initial bind, and post-expiry
   retry into `terminal_runtime.py`. Call the helper from both materialized and
   pre-created flows after terminal-context persistence. Preserve the existing
   clear-successor ordering and tmux-inner precedence.
2. On successful binding to the incoming native row, release only other
   gobby-owned, non-agent rows for the same session whose state is neither
   `pending` nor `live`. Never steal a live row from another active/paused
   session and never rewrite agent-terminal history.
3. Add `TerminalManager.resolve_live_for_session(session)`: first return the
   ordinary bound live/pending row; otherwise parse `gobby_terminal_id` and
   accept that row only when it is UUID-valid, live/pending, same-project,
   non-agent, and unbound or self-bound. Missing, malformed, foreign,
   agent-owned, exited, and other-session values resolve to no managed target.
4. Move WakeDispatcher's managed/context/raw selection into
   `wake_terminal_resolution.py`, reuse `TerminalManager.resolve_live_for_session`
   there and in `send_keys`, and keep raw `tmux_pane`/`tmux_session` routing as
   the final tmux-only fallback. Extend `MemoryTerminalStore` with the same
   observable contract so shared terminal tests do not silently bypass the new
   path.

**Focused validation:**

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/events/test_wake_native_terminal.py tests/hooks/test_session_materialize.py tests/hooks/test_session_start_handlers.py tests/storage/test_terminal_bindings.py tests/mcp_proxy/test_sessions_terminal_tools.py tests/mcp_proxy/tools/sessions/test_terminal.py -q
```

**Acceptance:**

- 2.1.1 - A pre-created Codex session resumed with a new native `gobby_terminal_id` discovers and binds that row after context persistence, matching the observed `af5f5c4a`/`3eb7116d` shape. test: `tests/hooks/test_session_start_handlers.py::test_pre_created_native_resume_rebinds_new_terminal`.
- 2.1.2 - Materialized clear-successor handling retains claim/handoff transfer, stale-context expiry, tmux-inner precedence, and the bind retry after the old holder expires. test: `tests/hooks/test_session_materialize.py::test_native_bind_retries_after_clear_predecessor_expiry`.
- 2.1.3 - Bound, unbound-self, malformed, foreign-project, agent-owned, exited, and other-session `gobby_terminal_id` shapes have explicit resolver outcomes; only the first two eligible live/pending shapes resolve. test: `tests/storage/test_terminal_bindings.py::test_resolve_live_for_session_context_id_guard_matrix`.
- 2.1.4 - Wake and `send_keys` use the eligible native row without tmux metadata, while raw tmux delivery still works when no managed row is eligible. test: `tests/mcp_proxy/tools/sessions/test_terminal.py::test_send_keys_preserves_raw_tmux_fallback_after_native_lookup`.
- 2.1.5 - Successful rebinding releases only stale exited/orphaned gobby-owned rows for that same session and preserves every live/pending or agent-owned row. test: `tests/storage/test_terminal_bindings.py::test_rebind_releases_only_stale_non_agent_owner_rows`.
- 2.1.6 - All native binding, lookup, wake, and send_keys regressions use isolated managers and fakes; no test connects to the live daemon or terminal host. file: `tests/terminals/fakes.py`.

### 2.2 Reconcile restart-stale sessions and open startup replay [category: code] [domain: backend] (depends: 1.1, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/events/wake_recovery.py` — new module shared from 1.1: add startup reconciliation and authoritative gate-open drain
- `src/gobby/events/wake_active_recovery.py` — new module: shared-snapshot predicate, final recheck, and active-status reconciliation
- `src/gobby/events/wake.py::*` — scope-reason: reuse active-recovery policy before the normal non-urgent active decline
- `src/gobby/events/live_wake.py::*` — scope-reason: replace composer-only probe typing with one shared terminal-activity result
- `src/gobby/runner_init/orchestration.py::_probe_composer`
- `src/gobby/runner_init/wake_activity.py` — new module: moved shared terminal snapshot capture and idle-detector classification
- `src/gobby/storage/sessions/_field_update.py::*` — scope-reason: add one private guarded status compare-and-set beside existing writers
- `src/gobby/runner_lifecycle_agents.py::_run_agent_hook_replay_barrier`
- `src/gobby/runner_hook_replay.py` — new module: moved typed hook-barrier outcome and unresolved-session mapping
- `src/gobby/runner_lifecycle_reconcile.py::_reclassify_reconciliation_pending_runs`
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: order terminal adoption, typed barrier, initial reconciliation, then gate open/drain
- `tests/events/test_wake_recovery.py` — new suite shared from 1.1: predicate matrix, races, barrier exclusions, and post-restart delivery
- `tests/sessions/test_turn_lifecycle.py::*` — scope-reason: genuine turns remain active until their committed end boundary
- `tests/storage/sessions/test_storage_sessions_lifecycle.py::*` — scope-reason: private active/updated-at compare-and-set contract
- `tests/storage/sessions/test_lifecycle_expiry.py::*` — scope-reason: existing lifecycle status writers remain compatible with the private compare-and-set
- `tests/runner_init/test_probe_composer.py::*` — scope-reason: composer and in-flight evidence derive from one snapshot
- `tests/test_bm25_startup.py::*` — scope-reason: reduced runner startup fixture includes the new reconciliation/gate step
- `tests/test_runner_lifecycle.py::*` — scope-reason: daemon startup binding and subsystem scheduling remain ordered
- `tests/test_runner_lifecycle_restart_replay.py::*` — scope-reason: typed barrier outcomes and unresolved ordinary/agent-run branches
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: host, barrier, reconciliation, and gate-open order

Production-size decomposition: move the active-session recovery branch from
`src/gobby/events/wake.py` into the new
`src/gobby/events/wake_active_recovery.py`; move terminal activity capture from
`src/gobby/runner_init/orchestration.py` into the new
`src/gobby/runner_init/wake_activity.py`; and move the hook replay barrier from
`src/gobby/runner_lifecycle_agents.py` into the new
`src/gobby/runner_hook_replay.py`.

Consumers unchanged:
- `src/gobby/runner_lifecycle.py` — no-edit-reason: `run_daemon` already captures `http_bound_at_ms`, binds the owner loop, and launches the same subsystem entry point.
- `tests/storage/test_sessions_import.py` — no-edit-reason: the compare-and-set is private and does not change SessionManager's public import/signature contract.
- `tests/test_runner_shutdown.py` — no-edit-reason: the `init_subsystems` call signature remains unchanged.

**Task ownership:** existing bug #22654, narrowed to restart acceptance obligations
1-3 after the orchestrator files 2.1. **Dependencies:** 1.1 and 2.1.

**Granularity:** Eight acceptance items and more than six production targets
trigger inspection. This section owns one lifecycle outcome: classify only
restart-stale active sessions from a safe snapshot, commit the guarded status
transition, then open and drain the already-built replay gate. Native ownership
and routing are complete in 2.1. The two runner extractions are required because
the touched source files already exceed the 850-line decomposition threshold.

**Research context:**

- `IdleDetector.composer_read` and
  `IdleDetector.turn_in_flight_fingerprint` were verified with `gcode symbol-at`.
  They must consume the same ANSI snapshot: composer visibility is not evidence
  that no provider turn is in flight.
- `runner.http_bound_at_ms` is captured once immediately after HTTP bind. It is
  the restart horizon already supplied to hook replay. Comparing integer Unix
  milliseconds avoids mixing datetime precision; the exact `updated_at` value is
  still retained for compare-and-set race detection.
- `gcode grep -w _run_agent_hook_replay_barrier -m 50` found the subsystem and
  fenced-run reconciliation consumers plus the reduced BM25 fixture, lifecycle
  tests, and restart-replay suite. Move the barrier implementation to
  `runner_hook_replay.py`, return a typed outcome, and update every direct
  consumer; do not leave a forwarding shim in the 853-line source file.
- The typed outcome carries `settled`, `session_recovery_safe`, and the union of
  unresolved ordinary session IDs plus child session IDs mapped from active or
  unclassified unresolved agent runs. If services cannot produce a complete
  exclusion set, startup active-status mutation is skipped for all sessions.
- The gate-open drain re-queries marked undelivered recipients after all status
  compare-and-sets. It therefore covers a crash/callback boundary between the
  committed status update and in-memory scheduling without adding another
  durable state model.

**Implementation:**

1. Move `_probe_composer` from `src/gobby/runner_init/orchestration.py` to
   `runner_init/wake_activity.py` and return one immutable activity result derived
   from one terminal snapshot: composer state/text and turn-in-flight
   fingerprint. Wake's ordinary draft protection consumes the same result type.
2. Move `_run_agent_hook_replay_barrier` to `runner_hook_replay.py` and return the
   typed outcome above. Preserve current agent-run fencing semantics; update
   `_reclassify_reconciliation_pending_runs` to branch on `outcome.settled`.
3. Capture `restart_horizon_ms` once from `runner.http_bound_at_ms`. Snapshot the
   active terminal-session candidate set before yielding. Exclude every session
   in the barrier outcome and any row with null, invalid, or post-horizon
   `last_activity`/`updated_at`.
4. For each remaining candidate, take one activity snapshot. Any in-flight
   fingerprint wins over `empty` or `draft`; `unknown` also preserves `active`.
   Only no-in-flight plus `empty`/`draft` proceeds.
5. Immediately before mutation, take a second activity snapshot and re-read the
   row. Reapply the full predicate. Then call a private SessionManager operation
   whose transaction updates only an exact unchanged `status='active'` row with
   exact observed `updated_at` and both timestamps still at-or-before the
   millisecond horizon. It preserves `last_activity` and emits the ordinary
   post-commit transition.
6. In `init_subsystems`, keep the coordinator closed while terminal-host adoption
   and typed hook replay run. Perform the bounded initial reconciliation (or a
   logged no-mutation pass when the barrier cannot provide safe exclusions), add
   newly paused recipients to pending intent, then call one `open_and_drain` seam.
   That seam queries all marked undelivered recipients again before scheduling.
7. A non-urgent live wake arriving after startup may use the same reconciliation
   predicate only with the captured daemon horizon; activity created after that
   horizon is never repaired as restart residue. Urgent behavior remains
   unchanged. A draft may be reconciled to paused but terminal submission still
   declines as `composer_occupied`, preserving operator input and pending intent.

**Focused validation:**

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/events/test_wake_recovery.py tests/sessions/test_turn_lifecycle.py tests/storage/sessions/test_storage_sessions_lifecycle.py tests/storage/sessions/test_lifecycle_expiry.py tests/runner_init/test_probe_composer.py tests/test_bm25_startup.py tests/test_runner_lifecycle.py tests/test_runner_lifecycle_restart_replay.py tests/test_runner_lifecycle_subsystems.py -q
```

**Acceptance:**

- 2.2.1 - For `empty`, `draft`, and `unknown` composer states crossed with in-flight present/absent, every in-flight or unknown case preserves `active`; only no-in-flight `empty`/`draft` may advance. test: `tests/events/test_wake_recovery.py::test_restart_activity_snapshot_predicate_matrix`.
- 2.2.2 - Null timestamps preserve `active`; pre-cutoff and exact-equality integer-millisecond timestamps are eligible; either post-cutoff timestamp is ineligible; a changed row or second-snapshot race defeats the exact `updated_at` compare-and-set. test: `tests/events/test_wake_recovery.py::test_restart_horizon_and_final_recheck_matrix`.
- 2.2.3 - Timed-out barrier results exclude unresolved ordinary sessions and child sessions mapped from active/unclassified unresolved agent runs; an incomplete exclusion set skips all startup status mutation while retaining durable wake intent. test: `tests/test_runner_lifecycle_restart_replay.py::test_barrier_outcome_preserves_every_unresolved_session_class`.
- 2.2.4 - A committed transition between owner-loop binding and barrier completion causes zero terminal writes; startup order is host adoption, typed barrier decision, initial reconciliation, then one gate open/drain. test: `tests/test_runner_lifecycle_subsystems.py::test_wake_replay_gate_opens_after_reconciliation`.
- 2.2.5 - An isolated stale-active session with a marked pending row is reconciled, replayed through the native route, injected, and acknowledged with non-null `delivered_at` without `send_keys`, pane input, or any manual resend. test: `tests/events/test_wake_recovery.py::test_restart_reconciliation_reaches_delivered_without_manual_input`.
- 2.2.6 - If status compare-and-set commits but callback scheduling fails or the owner task is cancelled, authoritative gate-open/startup drain still finds the durable marked row and a later attempt delivers it exactly once. test: `tests/events/test_wake_recovery.py::test_committed_reconciliation_survives_callback_and_task_failure`.
- 2.2.7 - Genuine activity whose lifecycle timestamp is after the restart horizon is never reset, even when the composer looks empty/draft; it replays only after its real committed end boundary. test: `tests/sessions/test_turn_lifecycle.py::test_post_restart_genuine_turn_is_not_reconciled`.
- 2.2.8 - Barrier and activity-probe moves leave no forwarding shim, keep every touched production file below 1,000 lines, and preserve existing agent-run fencing plus runner startup fixtures. file: `src/gobby/runner_hook_replay.py`.

## Q1 Verification
`kind: verification`

Each implementation leaf runs its focused command above. After all three leaves
are integrated, run this isolated union and the repository gates:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/events/test_wake.py tests/events/test_wake_native_terminal.py tests/events/test_wake_recovery.py tests/agents/test_terminal_delivery.py tests/sessions/test_mailbox.py tests/sessions/test_turn_lifecycle.py tests/storage/test_inter_session_messages.py tests/hooks/test_session_materialize.py tests/hooks/test_session_start_handlers.py tests/storage/test_terminal_bindings.py tests/storage/sessions/test_storage_sessions_lifecycle.py tests/storage/sessions/test_lifecycle_expiry.py tests/terminals/fakes.py tests/mcp_proxy/test_sessions_terminal_tools.py tests/mcp_proxy/tools/sessions/test_terminal.py tests/mcp_proxy/tools/test_agent_messaging.py tests/mcp_proxy/tools/test_agent_messaging_broadcast.py tests/runner_init/test_probe_composer.py tests/test_bm25_startup.py tests/test_runner_lifecycle.py tests/test_runner_lifecycle_restart_replay.py tests/test_runner_lifecycle_subsystems.py -q
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run mypy src/
uv run gobby test-quality audit tests/events/test_wake.py tests/events/test_wake_native_terminal.py tests/events/test_wake_recovery.py tests/agents/test_terminal_delivery.py tests/sessions/test_mailbox.py tests/sessions/test_turn_lifecycle.py tests/storage/test_inter_session_messages.py tests/hooks/test_session_materialize.py tests/hooks/test_session_start_handlers.py tests/storage/test_terminal_bindings.py tests/storage/sessions/test_storage_sessions_lifecycle.py tests/storage/sessions/test_lifecycle_expiry.py tests/terminals/fakes.py tests/mcp_proxy/test_sessions_terminal_tools.py tests/mcp_proxy/tools/sessions/test_terminal.py tests/mcp_proxy/tools/test_agent_messaging.py tests/mcp_proxy/tools/test_agent_messaging_broadcast.py tests/runner_init/test_probe_composer.py tests/test_bm25_startup.py tests/test_runner_lifecycle.py tests/test_runner_lifecycle_restart_replay.py tests/test_runner_lifecycle_subsystems.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low
uv run gobby test-types audit tests/events/test_wake.py tests/events/test_wake_native_terminal.py tests/events/test_wake_recovery.py tests/agents/test_terminal_delivery.py tests/sessions/test_mailbox.py tests/sessions/test_turn_lifecycle.py tests/storage/test_inter_session_messages.py tests/hooks/test_session_materialize.py tests/hooks/test_session_start_handlers.py tests/storage/test_terminal_bindings.py tests/storage/sessions/test_storage_sessions_lifecycle.py tests/storage/sessions/test_lifecycle_expiry.py tests/terminals/fakes.py tests/mcp_proxy/test_sessions_terminal_tools.py tests/mcp_proxy/tools/sessions/test_terminal.py tests/mcp_proxy/tools/test_agent_messaging.py tests/mcp_proxy/tools/test_agent_messaging_broadcast.py tests/runner_init/test_probe_composer.py tests/test_bm25_startup.py tests/test_runner_lifecycle.py tests/test_runner_lifecycle_restart_replay.py tests/test_runner_lifecycle_subsystems.py --baseline .gobby/test-types-baseline.json --fail-on-new
uv run gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json
```

Run the complete production-file ceiling check, then the stricter thresholds for
the two decomposed coordinators:

```bash
uv run python - <<'PY'
from pathlib import Path

files = [
    "src/gobby/sessions/mailbox.py",
    "src/gobby/sessions/mailbox_delivery.py",
    "src/gobby/storage/inter_session_messages.py",
    "src/gobby/events/wake.py",
    "src/gobby/events/wake_notifications.py",
    "src/gobby/events/wake_recovery.py",
    "src/gobby/events/wake_active_recovery.py",
    "src/gobby/events/wake_batch.py",
    "src/gobby/events/live_wake.py",
    "src/gobby/events/wake_terminal_resolution.py",
    "src/gobby/mcp_proxy/tools/agent_messaging.py",
    "src/gobby/runner_lifecycle_subsystems.py",
    "src/gobby/hooks/event_handlers/_session_start/terminal_runtime.py",
    "src/gobby/hooks/event_handlers/_session_start/materialize.py",
    "src/gobby/hooks/event_handlers/_session_start/flow.py",
    "src/gobby/storage/terminals.py",
    "src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py",
    "src/gobby/runner_init/orchestration.py",
    "src/gobby/runner_init/wake_activity.py",
    "src/gobby/storage/sessions/_field_update.py",
    "src/gobby/runner_lifecycle_agents.py",
    "src/gobby/runner_hook_replay.py",
    "src/gobby/runner_lifecycle_reconcile.py",
]
violations = {path: len(Path(path).read_text().splitlines()) for path in files if len(Path(path).read_text().splitlines()) >= 1000}
assert not violations, violations
assert len(Path("src/gobby/events/wake.py").read_text().splitlines()) < 850
assert len(Path("src/gobby/sessions/mailbox.py").read_text().splitlines()) < 850
PY
```

Coordinator-only live proof runs from the main checkout after all commits are
integrated; implementation workers never restart the daemon:

1. Send one targetless `global` coordination message announcing the restart and
   wait for a quiet window with no live spawned worker or close validator.
2. Run `uv run gobby restart`, verify `uv run gobby status`, then send one
   targetless `global` restart-complete message.
3. Choose a paused Codex native-terminal session. Send message A with
   `wake=true`, wait five seconds while its turn is active, then send message B
   with `wake=true`. Record both message IDs, B's correlated structured decline,
   and the single daemon INFO record containing B's session/message/reason.
4. Provide no `send_keys` or pane input. After the committed end boundary and
   replay, read both rows with `get_inter_session_message` and record non-null
   `delivered_at` values no later than one minute after message B was sent.
5. Attach the two IDs, timestamps, decline payload, one-log evidence, restart
   announcement/completion evidence, and quiet-window check to the close summary.

Planner validation remains narrative-only; expansion validation is intentionally
not run in this stage:

```bash
uv run gobby plans validate .gobby/plans/wake-delivery-recovery.md -p /Users/josh/Projects/gobby
```

## V1 Plan Changelog
`kind: framing`

- 2026-09-21: Initial decision-complete draft covers durable turn-boundary replay, structured single-log declines, restart-safe status reconciliation, and resumed-native-terminal rebinding for #22653/#22654.
- 2026-09-21: Round 1 resolves all eight adversary findings with a shared-snapshot restart predicate, startup gate, cancellation-safe replay ownership, mailbox-boundary correlation, split #22654 leaves, complete consumers, end-to-end delivery proof, and coordinated live validation.
